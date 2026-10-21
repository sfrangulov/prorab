#!/usr/bin/env python3
"""Monitor: polls brigades, limits, budget and the prorab's own context.

Runs as a background command of the prorab: prints ``EVENT <line>`` for every event of the
first poll that has any (or ``EVENT heartbeat`` after a quiet ``heartbeat_s``) and exits 0;
the prorab handles the events and restarts it. Because every run exits on its first event,
the dedupe state lives in ``$PRORAB_LOG_DIR/monitor-state.json`` and level conditions fire
on change, not on every poll.
"""
import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402
import scheduler  # noqa: E402

SETTLED = ("idle", "done", "blocked")


# ---- log and state files --------------------------------------------------------------

def log_dir():
    return os.environ.get("PRORAB_LOG_DIR") or os.path.expanduser("~/projects/prorab/logs")


def state_path():
    return os.path.join(log_dir(), "monitor-state.json")


def readings(since=None):
    """Limit readings from monitor.log (same reader as the scheduler's burn rate)."""
    return scheduler.readings(since=since)


def load_mem():
    try:
        with open(state_path(), encoding="utf-8") as fh:
            mem = json.load(fh)
        return mem if isinstance(mem, dict) else {}
    except (OSError, ValueError):
        return {}


def save_mem(mem):
    os.makedirs(log_dir(), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mem, fh)
    os.replace(tmp, state_path())


def append_log(entry):
    os.makedirs(log_dir(), exist_ok=True)
    with open(scheduler.log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---- real dependencies ---------------------------------------------------------------

def _pct_ctx(pct, d):
    """Context dict from a bare status-line percent (window unknown: 200k default)."""
    c = d["context"]
    soft_pct = min(c["soft_pct"], c["soft_max_tokens"] * 100.0 / c["unknown_window"])
    return {"pct": pct, "soft": pct >= soft_pct, "hard": pct >= c["hard_pct"]}


def _own_context(d):
    import herdr_cli
    import transcript
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return None
    me = next((a for a in herdr_cli.agents() if a.get("pane_id") == pane), None)
    sid = ((me or {}).get("agent_session") or {}).get("value")
    if me and sid and me.get("cwd"):
        path = transcript.find_transcript(me["cwd"], sid)
        if path:
            ctx = transcript.context_status(path, d)
            if ctx:
                return ctx
    import limits
    pct = limits.statusline_context(herdr_cli.pane_visible(pane))
    if pct is None:
        return None
    return dict(_pct_ctx(pct, d), statusline=True)


def _transcript_size(st):
    import transcript
    if not st.get("session_id") or not st.get("cwd"):
        return None
    path = transcript.find_transcript(st["cwd"], st["session_id"])
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


def real_deps():
    import brigade
    import herdr_cli
    import journal
    import limits
    return {
        "state": brigade.state,
        "transcript_size": _transcript_size,
        "statusline_ctx": lambda n: limits.statusline_context(herdr_cli.agent_visible(n)),
        "limits": scheduler.gather_limits,
        "kv_get": journal.kv_get,
        "over_budget": journal.over_budget,
        "override": journal.override,
        "own_context": _own_context,
        "brigades": journal.brigades,
    }


# ---- poll ---------------------------------------------------------------------------

def _fmt(v):
    return "-" if v is None else "%g" % v


def _level(ctx):
    return 2 if ctx.get("hard") else 1 if ctx.get("soft") else 0


def _limit_state(lim, cap, d):
    """(band, week_over, reached): band 0 none, 1 no_new_mass, 2 stop."""
    five = lim.get("five_hour")
    band = 0
    if five is not None:
        if five >= cap["stop"]:
            band = 2
        elif cap["no_new_mass"] is not None and five >= cap["no_new_mass"]:
            band = 1
    wk = lim.get("seven_day")
    week_over = wk is not None and wk >= d["limits"]["seven_day"]["notify"]
    return [band, bool(week_over), bool(lim.get("reached"))]


def poll(brigades, d, mem, now, *, deps=None):
    """Event lines in priority order; `mem` carries the dedupe state between polls.

    The state is updated on a copy and committed only when the whole poll returns, so a
    failure in the middle never swallows an event. A failing source is isolated: it is
    recorded in ``mem["poll_errors"]`` and the other sources are still polled.
    """
    dp = real_deps() if deps is None else deps
    work = copy.deepcopy(mem)
    out = _poll(brigades, d, work, now, dp)
    mem.clear()
    mem.update(work)
    return out


def _poll(brigades, d, mem, now, dp):
    ev = {k: [] for k in ("gone", "limit", "blind", "blind_week", "smeta", "prorab_context",
                          "context_hard", "context_soft", "hung", "settled", "error")}
    errors = {}

    def guard(source, fn, *args, default=None):
        try:
            return fn(*args)
        except Exception as e:  # one broken source must not stop the others
            errors.setdefault(source, "%s: %s" % (type(e).__name__, e))
            return default

    def kv_get(key):
        return guard("kv", dp["kv_get"], key)

    override = guard("override", dp["override"]) is not None
    lim = guard("limits", dp["limits"], d, now)
    if lim is None:
        lim = {}
        have_lim = False
    else:
        have_lim = True
    resets_at = lim.get("five_hour_resets_at")
    if resets_at is None and lim.get("five_hour_resets_in_s") is not None:
        resets_at = now + lim["five_hour_resets_in_s"]
    five, week, source = lim.get("five_hour"), lim.get("seven_day"), lim.get("source")
    if have_lim:
        mem["last_reading"] = {"ts": now, "five_hour": five, "seven_day": week,
                               "source": source, "resets_at": resets_at}
    else:
        mem.pop("last_reading", None)

    # limit: fires when a component of the state gets worse; falling back re-arms it
    cap = scheduler.cap_for(now, resets_at, d["night"]["morning"], d,
                            night=kv_get("shift.night") == "1", week_known=week is not None)
    if have_lim and not override:
        cur = _limit_state(lim, cap, d)
        prev = mem.get("limit") or [0, False, False]
        if any(c > p for c, p in zip(cur, prev)):
            ev["limit"].append("limit %s %s %s %s" % (_fmt(five), _fmt(week), source, cap["mode"]))
        mem["limit"] = cur

    # blind: the source is unknown twice in a row
    if not have_lim:
        pass
    elif source == "unknown":
        mem["unknown_streak"] = mem.get("unknown_streak", 0) + 1
        if (mem["unknown_streak"] >= 2 and not mem.get("blind_fired")
                and not kv_get("limits.blind_ack")):
            mem["blind_fired"] = True
            ev["blind"].append("blind")
    else:
        mem["unknown_streak"], mem["blind_fired"] = 0, False

    # blind_week: the weekly figure is missing for longer than week_unknown_blind_s
    if not have_lim:
        pass
    elif week is None:
        since = mem.get("week_unknown_since")
        if since is None:
            since = mem["week_unknown_since"] = now
        if (now - since > d["limits"]["week_unknown_blind_s"] and not mem.get("blind_week_fired")
                and not kv_get("limits.blind_week_ack")):
            mem["blind_week_fired"] = True
            ev["blind_week"].append("blind_week")
    else:
        mem["week_unknown_since"], mem["blind_week_fired"] = None, False

    # smeta: the set of exceeded budget items changed
    over = None if (override or not have_lim) else guard("over_budget", dp["over_budget"], week)
    if over is not None:
        over = list(over)
        if over and over != mem.get("smeta"):
            ev["smeta"].append("smeta %s" % ",".join(over))
        mem["smeta"] = over

    # the prorab's own context
    own = guard("own_context", dp["own_context"], d)
    ctx_mem = mem.setdefault("ctx", {})
    if own:
        lv = _level(own)
        if lv > ctx_mem.get("_prorab", 0):
            ev["prorab_context"].append("prorab_context %s" % own["pct"])
        ctx_mem["_prorab"] = lv

    gone, settled, hung = (mem.setdefault(k, {}) for k in ("gone", "settled", "hung"))
    for name in brigades:
        st = guard("state.%s" % name, dp["state"], name)
        if st is None:
            continue
        status = st.get("status")
        if status == "gone":
            if not gone.get(name):
                ev["gone"].append("gone %s" % name)
            gone[name] = True
            continue
        gone[name] = False

        c, suffix = st.get("ctx"), ""
        if c is None:
            pct = guard("statusline.%s" % name, dp["statusline_ctx"], name)
            if pct is not None:
                c, suffix = _pct_ctx(pct, d), " (statusline)"
        if c:
            lv = _level(c)
            if lv > ctx_mem.get(name, 0):
                kind = "context_hard" if lv == 2 else "context_soft"
                ev[kind].append("%s %s %s%s" % (kind, name, c["pct"], suffix))
            ctx_mem[name] = lv

        if status == "working":
            settled.pop(name, None)
            size = guard("transcript.%s" % name, dp["transcript_size"], st)
            h = hung.get(name)
            if size is None:  # no transcript to watch: never call a brigade hung blindly
                hung.pop(name, None)
            elif h is None or h["size"] != size:
                hung[name] = {"size": size, "since": now, "fired": False}
            elif not h["fired"] and now - h["since"] >= d["monitor"]["hung_s"]:
                h["fired"] = True
                ev["hung"].append("hung %s" % name)
        else:
            hung.pop(name, None)
            if status in SETTLED and (status == "blocked" or st.get("bg_shells") == 0):
                if settled.get(name) != status:
                    ev["settled"].append("settled %s %s" % (name, status))
                settled[name] = status

    # errors: `error <source>` after error_polls consecutive failing polls, once per streak
    streak, fired = mem.setdefault("err_streak", {}), mem.setdefault("err_fired", [])
    for src in list(streak):
        if src not in errors:
            del streak[src]
            if src in fired:
                fired.remove(src)
    for src in errors:
        streak[src] = streak.get(src, 0) + 1
        if streak[src] >= d["monitor"]["error_polls"] and src not in fired:
            fired.append(src)
            ev["error"].append("error %s" % src)
    mem["poll_errors"] = ["%s: %s" % (k, v) for k, v in errors.items()]
    if errors:
        mem["last_error"] = list(errors)[-1]
    else:
        mem.pop("last_error", None)
    return [line for k in ev for line in ev[k]]


# ---- main ---------------------------------------------------------------------------

def main(argv=None, *, deps=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brigade", action="append", default=[])
    ap.add_argument("--interval", type=float)
    ap.add_argument("--heartbeat", type=float)
    a = ap.parse_args(argv)
    d = _defaults.load()
    interval = a.interval if a.interval is not None else d["monitor"]["interval_s"]
    heartbeat = a.heartbeat if a.heartbeat is not None else d["monitor"]["heartbeat_s"]
    dp = real_deps() if deps is None else deps
    names = list(a.brigade)
    mem = load_mem()
    started = time.monotonic()
    while True:
        now = time.time()
        list_err = None
        try:
            if not a.brigade:  # no explicit list: follow the journal on every poll
                try:
                    names = list(dp["brigades"]())
                    list_err = None
                except Exception as e:  # keep the previous list
                    list_err = "brigades: %s" % e
            events = poll(names, d, mem, now, deps=dp)
            if list_err:
                mem["poll_errors"] = mem.get("poll_errors", []) + [list_err]
                mem["last_error"] = "brigades"
        except Exception as e:  # herdr/bd hiccup: keep watching, the heartbeat still wakes us
            print("monitor: poll failed: %s" % e, file=sys.stderr)
            events = []
            mem["last_error"] = "poll"
            append_log({"ts": now, "events": [], "errors": ["poll: %s" % e]})
        else:
            save_mem(mem)
            entry = dict(mem.get("last_reading") or {"ts": now}, events=events)
            if mem.get("poll_errors"):
                entry["errors"] = mem["poll_errors"]
            append_log(entry)
        if events:
            for line in events:
                print("EVENT %s" % line, flush=True)
            return 0
        if time.monotonic() - started >= heartbeat:
            last = mem.get("last_error")
            print("EVENT heartbeat" + (" last_error=%s" % last if last else ""), flush=True)
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
