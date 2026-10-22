#!/usr/bin/env python3
"""Monitor: polls crews, limits, budget and the foreman's own context.

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
import herdr_cli  # noqa: E402
import scheduler  # noqa: E402

SETTLED = ("idle", "done", "blocked")
SKIP_DIRS = frozenset((".git", "node_modules", ".venv", "__pycache__"))
EVENT_ORDER = ("gone", "consent", "provider_error", "limit_screen", "limit", "blind",
               "blind_week", "budget", "prorab_context", "context_hard", "context_soft",
               "hung", "hung_unclear", "settled", "error")


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


def _own_context(d, listed=None):
    import transcript
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return None
    agents = herdr_cli.agents() if listed is None else listed
    me = next((a for a in agents if a.get("pane_id") == pane), None)
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


def _newest_age(worktree, now, max_files):
    """Seconds since the newest file in `worktree` changed, skipping .git, node_modules,
    .venv and __pycache__. None when the tree is unreadable or has more than `max_files`
    entries."""
    newest, seen = None, 0

    def fail(err):
        raise err
    try:
        for root, dirs, files in os.walk(worktree, onerror=fail):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            seen += len(dirs) + len(files)
            if seen > max_files:
                return None
            for name in files:
                try:
                    m = os.lstat(os.path.join(root, name)).st_mtime
                except FileNotFoundError:  # gone between listing and stat
                    continue
                if newest is None or m > newest:
                    newest = m
        if newest is None:  # an empty tree: its own mtime
            newest = os.stat(worktree).st_mtime
    except OSError:
        return None
    return max(0.0, now - newest)


def real_deps():
    import crew
    import journal
    import limits
    import slots
    return {
        "state": lambda name, a, **kw: (crew.state(name, a, **kw) if a is not None
                                        else crew.gone_state()),
        "agents": herdr_cli.agents,
        "kv_all": journal.kv_all,
        "visible": herdr_cli.pane_visible,
        "slot_holders": slots.holders,
        "newest_age": _newest_age,
        "transcript_size": _transcript_size,
        "statusline_ctx": limits.statusline_context,
        "limits": scheduler.gather_limits,
        "over_budget": journal.over_budget,
        "budget": journal.budget,
        # both read files in this process (transcripts, the log); neither starts a process
        "shift_spend": journal.shift_spend,
        "rates": lambda d, now: limits.rates(readings(), d, now),
        "override": journal.override,
        "own_context": _own_context,
        "crews": journal.crews,
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


def _budget_item(item, spend, budget, week, kv):
    """`<item>=<spent>/<limit>` in dollars (or pp); the bare name when a figure is missing."""
    try:
        if item == "usd":
            return "usd=%.2f/%.2f" % (spend["usd"], budget["usd"])
        if item == "week_pp":
            rise = float(week) - float(kv["shift.week_start"])
            return "week_pp=%g/%g" % (rise, budget["week_pp"])
        kind, _, key = item.partition(":")
        if kind == "estimate":
            w = spend["work_orders"][key]
            return "%s=%.2f/%.2f" % (item, w["usd_total"], w["estimate_usd"])
        if kind == "research":
            r = spend["research"][key]
            return "%s=%.2f/%.2f" % (item, r["usd"], r["estimate_usd"])
    except (KeyError, TypeError, ValueError):
        pass
    return item


def _kv_value(kv, key):
    v = kv.get(key)
    return None if v in (None, "") else v


def _find_agent(name, listed, kv):
    """The crew's agent: by its pane in kv first, the name only as a fallback."""
    pane = _kv_value(kv, "crew.%s.pane" % name)
    a = next((x for x in listed if pane and x.get("pane_id") == pane), None)
    return a or next((x for x in listed if x.get("name") == name), None)


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def poll(crews, d, mem, now, *, deps=None):
    """Event lines in priority order; `mem` carries the dedupe state between polls.

    `crews` is a list of names or a callable taking the kv snapshot. The state is updated
    on a copy and committed only when the whole poll returns, so a failure in the middle
    never swallows an event. A failing source is isolated: it is recorded in
    ``mem["poll_errors"]`` and the other sources are still polled.
    """
    dp = real_deps() if deps is None else deps
    work = copy.deepcopy(mem)
    out = _poll(crews, d, work, now, dp)
    mem.clear()
    mem.update(work)
    return out


def _poll(crews, d, mem, now, dp):
    ev = {k: [] for k in EVENT_ORDER}
    errors = {}

    def guard(source, fn, *args, default=None, **kw):
        try:
            return fn(*args, **kw)
        except Exception as e:  # one broken source must not stop the others
            errors.setdefault(source, "%s: %s" % (type(e).__name__, e))
            return default

    # one agent list and one kv snapshot per poll; everything below reads these
    kv_snap = guard("kv", dp["kv_all"])
    kv = kv_snap if isinstance(kv_snap, dict) else {}
    kv_arg = kv if isinstance(kv_snap, dict) else None  # None: the callee reads kv itself
    listed = guard("agents", dp["agents"])

    def kv_get(key):
        return _kv_value(kv, key)

    if callable(crews):
        crews = crews(kv_arg)

    mon = d["monitor"]
    last = mem.setdefault("last", {})
    gone, settled, hung = (mem.setdefault(k, {}) for k in ("gone", "settled", "hung"))
    ep = {k: mem.setdefault(k, {}) for k in ("consent", "provider_error", "limit_screen")}
    ctx_mem = mem.setdefault("ctx", {})
    screens = {}        # pane id -> screen text read during this poll
    seen_crews = []     # (pane id, status) of every crew seen this poll
    active = False
    holders = []

    def read(pane):
        if pane not in screens:
            text = guard("screen.%s" % pane, dp["visible"], pane)
            if text is None:
                return None
            screens[pane] = text
        return screens[pane]

    def limit_texts():
        """The screens read this poll, plus working crews' screens until `limit_panes`
        of the screens belong to working crews."""
        working = sum(1 for p, s in seen_crews if s == "working" and p in screens)
        for pane, status in seen_crews:
            if working >= mon["limit_panes"]:
                break
            if status == "working" and pane not in screens:
                if read(pane) is not None:
                    working += 1
        return list(screens.values())

    def slot_holders():
        if not holders:
            holders.extend(guard("slots", dp["slot_holders"], default=[]) or [])
        return holders

    override = guard("override", dp["override"], kv=kv_arg) is not None

    # spend and rate: recomputed now and then, by reading files in this process; the cached
    # figures are what every poll in between uses
    extras = {}
    def due(key, every):
        return key not in mem or not 0 <= now - mem[key]["at"] < every

    if due("spend", mon["spend_every_s"]):
        fresh = guard("shift_spend", dp["shift_spend"], d, now, kv=kv_arg)
        if fresh is not None:
            mem["spend"] = {"at": now, "value": fresh}
            extras["spend_usd"] = fresh["usd"]
    if due("rate", d["rate"]["every_s"]):
        fresh = guard("rates", dp["rates"], d, now)
        if fresh is not None:
            mem["rate"] = {"at": now}
            extras["rate"] = fresh
    spend = (mem.get("spend") or {}).get("value")

    for name in (crews if listed is not None else ()):
        a = _find_agent(name, listed, kv)
        prev = last.get(name) or {}
        screen = None
        if a is not None:
            pane = a.get("pane_id")
            need = (a.get("agent_status") != prev.get("status") or prev.get("shells")
                    or prev.get("statusline"))
            if need and pane:
                screen = read(pane)
        st = guard("state.%s" % name, dp["state"], name, a,
                   read_screen=False, screen=screen, kv=kv_arg)
        if st is None:
            continue
        status = st.get("status")
        if status == "gone":
            if not gone.get(name):
                ev["gone"].append("gone %s" % name)
            gone[name] = True
            last.pop(name, None)
            for episodes in ep.values():
                episodes.pop(name, None)
            continue
        gone[name] = False
        pane = st.get("pane_id") or (a or {}).get("pane_id")
        if st.get("ctx") is None and screen is None and pane:
            screen = read(pane)  # the context can only come from the status line
            if screen is not None:
                st = guard("state.%s" % name, dp["state"], name, a, read_screen=False, screen=screen,
                           kv=kv_arg) or st
        seen_crews.append((pane, status))
        if status != prev.get("status") or status == "working":
            active = True

        # what the screen shows: read only now and then, remembered between reads
        read_now = screen is not None
        bg = st.get("bg_shells") if read_now else prev.get("bg")
        if read_now:
            for key, field in (("consent", "consent"), ("provider_error", "provider_error"),
                               ("limit_screen", "limit_screen")):
                val = st.get(field)
                if not val:
                    ep[key].pop(name, None)
                    continue
                if name in ep[key]:
                    continue
                if key == "limit_screen":
                    at = val.get("resets_at") if isinstance(val, dict) else None
                    ep[key][name] = "-" if at is None else "%d" % round(at)
                    ev[key].append("limit_screen %s %s" % (name, ep[key][name]))
                else:
                    ep[key][name] = val
                    ev[key].append("%s %s %s" % (key, name, val))

        c, suffix, from_statusline = st.get("ctx"), "", False
        if c is None and read_now:
            pct = guard("statusline.%s" % name, dp["statusline_ctx"], screen)
            if pct is not None:
                c, suffix, from_statusline = _pct_ctx(pct, d), " (statusline)", True
        if c:
            lv = _level(c)
            if lv > ctx_mem.get(name, 0):
                kind = "context_hard" if lv == 2 else "context_soft"
                ev[kind].append("%s %s %s%s" % (kind, name, c["pct"], suffix))
            ctx_mem[name] = lv
        last[name] = {"status": status, "bg": bg,
                      "shells": status in ("idle", "done") and bool(bg),
                      "statusline": from_statusline or (prev.get("statusline", False)
                                                        and not read_now)}

        on_limit_screen = name in ep["limit_screen"]
        if status == "working":
            settled.pop(name, None)
            size = guard("transcript.%s" % name, dp["transcript_size"], st)
            h = hung.get(name)
            if h is None or h["size"] != size:
                hung[name] = {"size": size, "since": now, "fired": False}
            elif (not h["fired"] and not on_limit_screen
                  and now - h["since"] >= mon["hung_s"]):
                wt = st.get("cwd")
                if wt and any(_inside(x["cwd"], wt) for x in slot_holders() if x.get("cwd")):
                    pass  # a heavy command of this crew holds a slot: not hung
                elif size is None:
                    h["fired"] = True
                    ev["hung_unclear"].append("hung_unclear %s no_transcript" % name)
                else:
                    age = (guard("newest_age.%s" % name, dp["newest_age"], wt, now,
                                 mon["hung_scan_max_files"]) if wt else None)
                    if age is None:
                        h["fired"] = True
                        reason = "scan_capped" if wt and os.path.isdir(wt) else "no_worktree"
                        ev["hung_unclear"].append("hung_unclear %s %s" % (name, reason))
                    elif age >= mon["hung_s"]:
                        h["fired"] = True
                        ev["hung"].append("hung %s" % name)
        else:
            hung.pop(name, None)
            if (status in SETTLED and (status == "blocked" or not bg)
                    and name not in ep["consent"] and name not in ep["provider_error"]
                    and not on_limit_screen):
                if settled.get(name) != status:
                    ev["settled"].append("settled %s %s" % (name, status))
                settled[name] = status

    # the limits come after the crews: a stale sensor falls back on the screens just read
    lim = guard("limits", dp["limits"], d, now, pane_texts=limit_texts)
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
        mem["last_reading"] = dict({"ts": now, "five_hour": five, "seven_day": week,
                                    "source": source, "resets_at": resets_at,
                                    "seven_day_resets_at": lim.get("seven_day_resets_at")},
                                   **extras)
    elif extras:
        mem["last_reading"] = dict({"ts": now}, **extras)
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

    # blind: the source is unknown twice in a row, but not right after a 5h reset: then the
    # fresh sensor files lack five_hour for a couple of minutes while the older ones are stale
    if resets_at is not None:
        mem["last_resets_at"] = resets_at
    last_reset = mem.get("last_resets_at")
    in_grace = (last_reset is not None
                and 0 <= now - last_reset <= d["monitor"]["reset_grace_s"])
    if not have_lim:
        pass
    elif source == "unknown" and in_grace:
        mem["unknown_streak"] = 0
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

    # budget: the set of exceeded budget items changed
    over = (None if (override or not have_lim)
            else guard("over_budget", dp["over_budget"], week, spend, kv=kv_arg))
    if over is not None:
        over = list(over)
        if over and over != mem.get("budget"):
            limits_in_force = guard("budget", dp["budget"], kv=kv_arg) or {}
            ev["budget"].append("budget %s" % ",".join(
                _budget_item(i, spend, limits_in_force, week, kv) for i in over))
        mem["budget"] = over

    # the foreman's own context
    own = guard("own_context", dp["own_context"], d, listed=listed)
    if own:
        lv = _level(own)
        if lv > ctx_mem.get("_prorab", 0):
            ev["prorab_context"].append("prorab_context %s" % own["pct"])
        ctx_mem["_prorab"] = lv

    # errors: `error <source>` after error_polls consecutive failing polls, once per streak
    streak, fired = mem.setdefault("err_streak", {}), mem.setdefault("err_fired", [])
    for src in list(streak):
        if src not in errors:
            del streak[src]
            if src in fired:
                fired.remove(src)
    for src in errors:
        streak[src] = streak.get(src, 0) + 1
        if streak[src] >= mon["error_polls"] and src not in fired:
            fired.append(src)
            ev["error"].append("error %s" % src)
    mem["poll_errors"] = ["%s: %s" % (k, v) for k, v in errors.items()]
    if errors:
        mem["last_error"] = list(errors)[-1]
    else:
        mem.pop("last_error", None)
    lines = [line for k in EVENT_ORDER for line in ev[k]]
    mem["active"] = bool(active or lines)  # read by main: backoff resets while it is set
    return lines


# ---- main ---------------------------------------------------------------------------

def main(argv=None, *, deps=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--crew", action="append", default=[])
    ap.add_argument("--interval", type=float)
    ap.add_argument("--heartbeat", type=float)
    a = ap.parse_args(argv)
    d = _defaults.load()
    mon = d["monitor"]
    base = a.interval if a.interval is not None else mon["interval_s"]
    heartbeat = a.heartbeat if a.heartbeat is not None else mon["heartbeat_s"]
    dp = real_deps() if deps is None else deps
    names = list(a.crew)
    list_err = None
    mem = load_mem()
    started = time.monotonic()

    def crew_names(kv):
        """The crew list from the journal on every poll; on failure the previous list."""
        nonlocal names, list_err
        try:
            names = list(dp["crews"](kv=kv))
            list_err = None
        except Exception as e:
            list_err = "crews: %s" % e
        return names

    while True:
        now = time.time()
        list_err = None
        sleep_for = mem.get("interval") or base
        try:
            events = poll(names if a.crew else crew_names, d, mem, now, deps=dp)
            if list_err:
                mem["poll_errors"] = mem.get("poll_errors", []) + [list_err]
                mem["last_error"] = "crews"
        except Exception as e:  # herdr/bd hiccup: keep watching, the heartbeat still wakes us
            print("monitor: poll failed: %s" % e, file=sys.stderr)
            events = []
            mem["last_error"] = "poll"
            append_log({"ts": now, "events": [], "errors": ["poll: %s" % e]})
        else:
            if a.interval is not None:
                sleep_for = a.interval
            elif mem.get("active"):  # a status change, an event or a working crew
                sleep_for = mem["interval"] = base
            else:  # quiet: sleep as planned, then back off for the next time
                mem["interval"] = min(sleep_for * mon["backoff_factor"], mon["interval_max_s"])
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
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
