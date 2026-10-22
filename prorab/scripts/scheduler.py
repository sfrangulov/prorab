#!/usr/bin/env python3
"""Scheduler: how many crews may run at once, in what order to issue work orders, which
5-hour-limit thresholds apply by day and by night.

``plan`` is what the foreman calls before issuing (the estimates in pp, checked); ``cap`` prints the current thresholds.
"""
import argparse
import datetime
import json
import math
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402
import journal  # noqa: E402


# ---- critical path ----------------------------------------------------------------

def critical_order(graph, durations):
    """Ids sorted by remaining path (own estimate + longest path of what they block)."""
    ids = set(graph)
    for blockers in graph.values():
        ids.update(blockers)
    blocks = {i: [] for i in ids}
    for i, blockers in graph.items():
        for b in blockers:
            blocks[b].append(i)
    memo = {}

    def remaining(i, seen=()):
        if i in memo:
            return memo[i]
        tail = [remaining(j, seen + (i,)) for j in blocks[i] if j not in seen and j != i]
        memo[i] = durations.get(i, 0.0) + (max(tail) if tail else 0.0)
        return memo[i]

    return sorted(ids, key=lambda i: (-remaining(i), i))


def duration(stage, history, d):
    """Stage duration: its own, else the median of the kind's history, else the default."""
    own = stage.get("duration_min")
    if own is not None:
        return float(own)
    past = history.get(stage.get("kind")) or []
    if past:
        return float(statistics.median(past))
    return float(d["schedule"]["default_duration_min"])


# ---- monitor log and burn rate ---------------------------------------------------------

def log_path():
    base = os.environ.get("PRORAB_LOG_DIR") or os.path.expanduser("~/projects/prorab/logs")
    return os.path.join(base, "monitor.log")


def readings(path=None, since=None):
    """Parsed monitor.log entries (one JSON object per line); bad lines are skipped."""
    out = []
    try:
        with open(path or log_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or not isinstance(r.get("ts"), (int, float)):
                    continue
                if since is not None and r["ts"] < since:
                    continue
                out.append(r)
    except OSError:
        return []
    return out


def burn_points(entries):
    """``[(ts, five_hour)]`` from monitor entries that carry a 5-hour reading."""
    return [(r["ts"], r["five_hour"]) for r in entries
            if isinstance(r.get("five_hour"), (int, float))]


def burn_rate(points, d=None):
    """Percentage points per hour since the last reset (drop); None when unknown."""
    min_span = (d or _defaults.load())["schedule"]["burn_min_span_s"]
    start = 0
    for i in range(1, len(points)):
        if points[i][1] < points[i - 1][1]:
            start = i
    pts = points[start:]
    if len(pts) < 2:
        return None
    span = pts[-1][0] - pts[0][0]
    if span < min_span:
        return None
    return (pts[-1][1] - pts[0][1]) / (span / 3600.0)


# ---- caps -------------------------------------------------------------------------

def _next_morning(now, morning):
    hh, mm = (int(x) for x in morning.split(":"))
    cur = datetime.datetime.fromtimestamp(now)
    target = cur.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += datetime.timedelta(days=1)
    return target.timestamp()


def cap_for(now, resets_at, morning, d, night=False, week_known=True):
    lim = d["limits"]
    if not night or resets_at is None:
        day = lim["five_hour"]
        no_new = 0 if (night and not week_known) else day["no_new_mass"]
        return {"mode": "day", "no_new_mass": no_new, "stop": day["stop"]}
    night_cfg = lim["five_hour_night"]
    if resets_at <= _next_morning(now, morning):
        cap = {"mode": "night_before_morning", "no_new_mass": None,
               "stop": night_cfg["stop_before_morning"]}
    else:
        win = night_cfg["morning_window"]
        cap = {"mode": "night_morning_window", "no_new_mass": win["no_new_mass"],
               "stop": win["stop"]}
    if not week_known:
        cap["no_new_mass"] = 0  # the week is unknown at night: no new mass runs
    return cap


# ---- parallelism ----------------------------------------------------------------------

def allowed_parallel(ready, burn_pp_per_h, used_pct, cap_pct, resets_in_s, active, load1,
                     ncpu, d):
    """Total crews allowed at once (the active ones included)."""
    ceiling = d["machine"]["parallel_ceiling"]
    if used_pct is None:  # unknown usage is not zero: no growth, but one crew may run
        limit_bound = max(1, active)
    elif used_pct >= cap_pct:
        limit_bound = 0
    elif burn_pp_per_h is None or active == 0:
        limit_bound = ceiling
    else:
        hours = resets_in_s / 3600.0 if resets_in_s is not None else 5.0
        per_crew = (burn_pp_per_h / active) * hours
        limit_bound = ceiling if per_crew <= 0 else math.floor((cap_pct - used_pct) / per_crew)
    load_bound = active if load1 > ncpu else ceiling
    return max(0, min(active + ready, limit_bound, load_bound, ceiling))


def machine_load():
    """``(load1, ncpu)`` from sysctl; falls back to os.getloadavg / cpu_count."""
    try:
        load = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True,
                              text=True, check=True, timeout=10).stdout
        load1 = float(load.replace("{", " ").replace("}", " ").split()[0])
        ncpu = int(subprocess.run(["sysctl", "-n", "hw.ncpu"], capture_output=True,
                                  text=True, check=True, timeout=10).stdout.strip())
        return load1, ncpu
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return os.getloadavg()[0], os.cpu_count() or 1


# ---- plan ----------------------------------------------------------------------------

def work_order_durations(j, d, ids):
    """Estimate of a work order = the sum over its open stages."""
    history = {}
    out = {}
    for n in ids:
        total = 0.0
        for s in j.open_stages(n):
            info = j.stage_info(s)
            kind = info.get("kind")
            if kind not in history:
                history[kind] = j.durations(kind) if kind else []
            total += duration(info, history, d)
        out[n] = total
    return out


def fits(estimate_usd, budget_left_usd, rates, used_pct, cap_stop, week_rise_pp,
         week_budget_pp, override=False):
    """Whether a work order's estimate fits the budget left and what is left before the
    thresholds in force; advisory, the thresholds themselves stay on measured percentages.

    An unknown rate is never guessed: without a week rate the week is guarded only by its
    measured rise. "Ignore limits and finish" lifts every check (R42)."""
    import limits  # lazy, as in gather_limits
    pp_window = limits.to_pp(estimate_usd, rates["five_hour"])
    pp_week = limits.to_pp(estimate_usd, rates["seven_day"])
    budget = estimate_usd <= budget_left_usd
    window = used_pct is None or pp_window is None or used_pct + pp_window <= cap_stop
    week = pp_week is None or week_rise_pp is None or week_rise_pp + pp_week <= week_budget_pp
    if override:
        budget = window = week = True
    return {"pp_window": pp_window, "pp_week": pp_week, "budget": budget, "window": window,
            "week": week, "ok": budget and window and week}


def build_plan(j, d, now, lim, log, load, *, spend=None, rates=None):
    night = j.kv_get("shift.night") == "1"
    ov = j.override()
    resets_at = lim.get("five_hour_resets_at")
    cap = cap_for(now, resets_at, d["night"]["morning"], d, night=night,
                  week_known=lim.get("seven_day") is not None)
    cap_pct = 100.0 if ov else cap["stop"]
    entries = readings(log, since=now - 6 * 3600)
    used = lim.get("five_hour")
    if used is None:  # the log only counts while it is as fresh as a sensor reading
        fresh = [p for p in burn_points(entries)
                 if now - p[0] <= d["limits"]["sensor_max_age_s"]]
        used = fresh[-1][1] if fresh else None
    unknown = used is None
    if unknown and ov:
        used = 0.0  # the customer's command 'ignore limits and finish': usage does not bound the crews
    graph = j.graph()
    ready = [n for n in j.ready_work_orders()]
    order = [n for n in critical_order(graph, work_order_durations(j, d, list(graph)))
             if n in ready]
    order += [n for n in ready if n not in order]
    active = len(j.crews())
    load1, ncpu = load
    allowed = allowed_parallel(len(order), burn_rate(burn_points(entries), d), used, cap_pct,
                               lim.get("five_hour_resets_in_s"), active, load1, ncpu,
                               dict(d, machine=j.machine()))
    hold = None
    nnm = cap["no_new_mass"]
    if not ov and nnm is not None and ((unknown and nnm == 0) or (not unknown and used >= nnm)):
        hold = "no_new_mass"  # from this threshold on nothing new is issued
        allowed = min(allowed, active)
    if spend is None:
        spend = j.shift_spend(d, now)
    if rates is None:
        import limits
        rates = limits.rates(readings(log), d, now)
    b = j.budget()
    left = b["usd"] - spend["usd"]
    week_now, week_start = lim.get("seven_day"), j.kv_get("shift.week_start")
    rise = None if week_now is None or week_start in (None, "") \
        else float(week_now) - float(week_start)
    plan_fits = {}
    for n in order:
        est = j.estimate(n)["usd"]
        plan_fits[n] = ({"ok": False, "reason": "no_estimate"} if est is None
                        else fits(est, left, rates, used, cap["stop"], rise, b["week_pp"],
                                  bool(ov)))
    return {"order": order, "allowed": allowed, "active": active,
            "new": max(0, allowed - active), "cap": cap, "override": ov,
            "hold": hold, "limits_unknown": unknown,
            "budget": {"usd": b["usd"], "spent_usd": spend["usd"], "left_usd": left,
                       "week_pp": b["week_pp"], "week_rise_pp": rise},
            "rates": rates, "fits": plan_fits}


def live_pane_texts():
    """Visible text of every herdr agent pane; [] outside herdr or when it fails."""
    if os.environ.get("HERDR_ENV") != "1":
        return []
    try:
        import herdr_cli
        return [herdr_cli.pane_visible(a["pane_id"]) for a in herdr_cli.agents()]
    except Exception:  # herdr is only a fallback source: never fail the plan on it
        return []


def gather_limits(d, now, pane_texts=None):
    """Sensor first, then the max over all panes' status lines; resets_at filled from the countdown."""
    import limits
    lim = limits.get_limits(pane_texts or live_pane_texts, d, now)  # lazy: no pane read when the sensor is fresh
    if lim.get("five_hour_resets_at") is None and lim.get("five_hour_resets_in_s") is not None:
        lim = dict(lim, five_hour_resets_at=now + lim["five_hour_resets_in_s"])
    return lim


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    p = sub.add_parser("cap")
    p.add_argument("--now", type=float)
    p.add_argument("--resets-at", type=float)
    a = ap.parse_args(argv)
    d = _defaults.load()
    now = a.now if a.now is not None else time.time()
    if a.cmd == "cap":
        lim = gather_limits(d, now)
        resets_at = a.resets_at if a.resets_at is not None else lim.get("five_hour_resets_at")
        night = journal.kv_get("shift.night") == "1"
        cap = cap_for(now, resets_at, d["night"]["morning"], d, night=night,
                      week_known=lim.get("seven_day") is not None)
        print(json.dumps(cap))
        return 0
    journal.ensure_db()
    plan = build_plan(journal, d, now, gather_limits(d, now), log_path(), machine_load())
    print(json.dumps(plan, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
