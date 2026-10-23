#!/usr/bin/env python3
"""Monitor: polls crews, limits, budget and the foreman's own context.

Runs as a background command of the prorab: prints ``EVENT <line>`` for every event of the
first poll that has any (or ``EVENT heartbeat`` after a quiet ``heartbeat_s``) and exits 0;
the prorab handles the events and restarts it. Under the customer's emergency stop (the file
at ``emergency.stop_path``) a poll pauses every crew once and raises only ``emergency_stop`` and
``emergency_released``; those pauses are the only deliveries a poll ever makes. Because every run exits on its first event,
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
import hooks  # noqa: E402
import limits  # noqa: E402
import scheduler  # noqa: E402

SETTLED = ("idle", "done", "blocked")
SKIP_DIRS = frozenset((".git", "node_modules", ".venv", "__pycache__"))
EVENT_ORDER = ("emergency_stop", "emergency_released", "gone", "login", "consent",
               "provider_error", "limit_screen", "limit", "blind", "blind_week", "budget", "due", "prorab_context",
               "compacted", "context_hard", "context_soft", "hung", "hung_unclear",
               "consent_done", "settled", "error")
# What a poll under the emergency stop keeps of its own state: readings and spend are still
# logged; every dedupe mark is dropped, so what happened meanwhile is raised after the release.
STOP_KEEPS = ("last_reading", "spend", "rate", "week_burn", "poll_errors", "last_error",
              "emergency", "due", "next_due", "awake", "awake_missing")


# ---- log and state files --------------------------------------------------------------

def log_dir():
    return os.environ.get("PRORAB_LOG_DIR") or os.path.expanduser("~/projects/prorab/logs")


def state_path():
    return os.path.join(log_dir(), "monitor-state.json")


def mark_path(pane):
    """The file a running monitor leaves for its pane: ``monitor-<pane>.json``."""
    safe = (pane or "").replace(":", "_").replace("/", "_")
    return os.path.join(log_dir(), "monitor-%s.json" % safe)


def write_mark(pane):
    """Record this monitor (pid, pane, start time) for the foreman's handover check. A file
    write only; returns the path, or None when there is no pane."""
    if not pane:
        return None
    path = mark_path(pane)
    os.makedirs(log_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "pane": pane, "started_at": time.time()}, fh)
    os.replace(tmp, path)
    return path


def remove_mark(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


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
    if me and sid:
        ctx = limits.sensor_context(sid, d)
        if ctx:
            return ctx
    if me and sid and me.get("cwd"):
        path = transcript.find_transcript(me["cwd"], sid)
        if path:
            ctx = transcript.context_status(path, d)
            if ctx:
                return ctx
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


def _hold_awake(seconds):
    import shift    # `shift` imports `monitor`
    return shift.start_caffeinate(timeout_s=seconds)


def real_deps():
    import crew
    import journal
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
        "week_burn": lambda d, now: limits.week_burn(readings(), d, now),
        "override": journal.override,
        "own_context": _own_context,
        "crews": journal.crews,
        # the only deliveries a poll ever makes: the emergency stop's pauses
        "pause": crew.pause,
        # a timed `caffeinate` (it ends by itself, nothing is ever killed)
        "hold_awake": _hold_awake,
        # a file read in this process (the monitor's next poll reads again): no process
        "site_trust": lambda path: crew.site_trust(path, retry=False),
    }


# ---- poll ---------------------------------------------------------------------------

def _fmt(v):
    return "-" if v is None else "%g" % v


def _level(ctx):
    return 2 if ctx.get("hard") else 1 if ctx.get("soft") else 0


def _limit_state(lim, cap):
    """(band, reached): band 0 none, 1 no_new_mass, 2 stop. The week has its own notice."""
    five = lim.get("five_hour")
    band = 0
    if five is not None:
        if five >= cap["stop"]:
            band = 2
        elif cap["no_new_mass"] is not None and five >= cap["no_new_mass"]:
            band = 1
    return [band, bool(lim.get("reached"))]


def _week_notice(mem, proj):
    """True when the week's level rose above the highest one reported for the current week.
    The week is keyed by the projection's reset time: more than the tolerance away from the
    stored one it is a new week; an unknown one keeps the stored week."""
    notice = mem.get("week_notice")
    if not isinstance(notice, dict):
        notice = {"resets_at": None, "level": 0}
    at = proj["resets_at"]
    if at is not None:
        if notice["resets_at"] is None:
            notice["resets_at"] = at
        elif abs(at - notice["resets_at"]) > limits._WINDOW_TOLERANCE_S:
            notice = {"resets_at": at, "level": 0}
    fire = proj["level"] > notice["level"]
    if fire:
        notice["level"] = proj["level"]
    mem["week_notice"] = notice
    return fire


def _off_hours(mem, kv_get, week, get_budget):
    """N for the projection from what the poll already holds: the kv snapshot, the cached
    spend and the cached seven-day rate. Nothing is read here."""
    night = kv_get("shift.night") == "1"
    budget = get_budget() if night else None
    if budget is None:  # no night shift, or its budget unreadable
        return limits.off_hours(False, {}, None, 0.0, None)
    start = kv_get("shift.week_start")
    rise = None if week is None or start in (None, "") else float(week) - float(start)
    spent = ((mem.get("spend") or {}).get("value") or {}).get("usd", 0.0)
    return limits.off_hours(night, budget, rise, spent,
                            (mem.get("rate") or {}).get("week_usd_per_pp"))


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


_FAILED = object()


def _find_agent(name, listed, kv):
    """The crew's agent: by its pane in kv first, the name only as a fallback."""
    pane = _kv_value(kv, "crew.%s.pane" % name)
    a = next((x for x in listed if pane and x.get("pane_id") == pane), None)
    return a or next((x for x in listed if x.get("name") == name), None)


def _exiting(mark, now, window_s):
    """Whether a kv mark `crew.<name>.exiting` (epoch seconds) is younger than the window."""
    try:
        return 0 <= now - float(mark) < window_s
    except (TypeError, ValueError):
        return False


def _pid_alive(pid):
    """Whether process `pid` still exists. A child of this process that has exited is reaped
    here (a zombie would still answer the signal)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        pass        # not ours: ask the signal
    except OSError:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:     # no permission to signal it: it exists
        return True
    return True


def _awake_void(rec, now, min_left_s):
    """Whether the awake hold recorded in `rec` must be started again: no usable record,
    recorded before a reboot (its uptime clock is ahead of ours), little time left, or its
    holder process is gone."""
    if not isinstance(rec, dict):
        return True
    until, mono = rec.get("until"), rec.get("mono")
    if not isinstance(until, (int, float)) or not isinstance(mono, (int, float)):
        return True
    if mono > time.monotonic() or until - now <= min_left_s:
        return True
    pid = rec.get("pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and not _pid_alive(pid)


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# ---- hook events ------------------------------------------------------------------------

def _read_events(path, entry, sid):
    """The complete JSON lines appended to `path` since the offset kept in `entry`
    ({"sid", "offset"} or None), and the new entry. The first sight of a session starts at the
    end of an existing file (older lines are not replayed); a file that appears later is read
    from its start; a half-written last line waits for its newline."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], {"sid": sid, "offset": 0}
    if not isinstance(entry, dict) or entry.get("sid") != sid:
        return [], {"sid": sid, "offset": size}
    offset = entry.get("offset", 0)
    if not isinstance(offset, int) or not 0 <= offset <= size:
        offset = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b"\n") + 1
    records = []
    for line in data[:end].splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, {"sid": sid, "offset": offset + end}


def _prune_events(folder, now, retention_s):
    """Remove the event files not touched for `retention_s`."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        try:
            if name.endswith(".jsonl") and now - os.path.getmtime(path) > retention_s:
                os.remove(path)
        except OSError:
            pass


def _event_facts(records):
    """What a crew's new hook events say: whether it stopped with no background task, asked
    for a tool permission, or compacted its context by itself."""
    return {
        "stop": any(r.get("event") == "Stop" and r.get("background_tasks", 1) is not None
                    and not r.get("background_tasks", 1) for r in records),
        "permission": any(r.get("event") == "Notification"
                          and r.get("notification_type") == "permission_prompt"
                          for r in records),
        "compacted": any(r.get("event") == "PreCompact" and r.get("trigger") == "auto"
                         for r in records),
    }


def poll(crews, d, mem, now, *, deps=None):
    """Event lines in priority order; `mem` carries the dedupe state between polls.

    `crews` is a list of names or a callable taking the kv snapshot. The state is updated
    on a copy and committed only when the whole poll returns, so a failure in the middle
    never swallows an event. A failing source is isolated: it is recorded in
    ``mem["poll_errors"]`` and the other sources are still polled.
    """
    dp = real_deps() if deps is None else deps
    work = copy.deepcopy(mem)
    stopping = os.path.exists(d["emergency"]["stop_path"])   # the one file test of a poll
    out = _poll(crews, d, work, now, dp, stopping)
    if stopping:
        # nothing but the emergency events is raised, and no dedupe mark is advanced
        kept = {k: work[k] for k in STOP_KEEPS if k in work}
        work = copy.deepcopy(mem)
        work.update(kept)
        # keeping a promise means telling the customer what stands: `due` is still raised
        out = [line for line in out if line.startswith(("emergency_stop", "due "))]
        work["active"] = bool(out)
    mem.clear()
    mem.update(work)
    return out


def _poll(crews, d, mem, now, dp, stopping=False):
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

    # the emergency stop: every registered crew with a listed agent is paused once; the
    # release is announced once
    emergency = mem.get("emergency") if isinstance(mem.get("emergency"), dict) else None
    if stopping and listed is not None:
        if emergency is None:
            emergency = mem["emergency"] = {"since": now, "paused": []}
            first = True
        else:
            first = False
        newly = []
        for name in crews:
            if name in emergency["paused"] or _find_agent(name, listed, kv) is None:
                continue
            if guard("pause.%s" % name, dp["pause"], name, default=_FAILED) is not _FAILED:
                emergency["paused"].append(name)
                newly.append(name)
        if first:
            ev["emergency_stop"].append("emergency_stop %s" % (",".join(newly) or "-"))
    elif not stopping and emergency is not None:
        ev["emergency_released"].append(
            "emergency_released %s" % (",".join(emergency["paused"]) or "-"))
        del mem["emergency"]

    hooks_on = kv_get("hooks.enabled") == "1"
    event_files = mem.setdefault("events", {})
    mon = d["monitor"]
    last = mem.setdefault("last", {})
    gone, settled, hung = (mem.setdefault(k, {}) for k in ("gone", "settled", "hung"))
    ep = {k: mem.setdefault(k, {})
          for k in ("consent", "provider_error", "limit_screen", "login")}
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
            mem["rate"] = {"at": now,
                           "week_usd_per_pp": (fresh.get("seven_day") or {}).get("usd_per_pp")}
            extras["rate"] = fresh
    if due("week_burn", d["rate"]["every_s"]):
        fresh = guard("week_burn", dp["week_burn"], d, now)
        if fresh is not None:
            mem["week_burn"] = {"at": now, "value": fresh}
            extras["week_burn"] = fresh
    spend = (mem.get("spend") or {}).get("value")
    if hooks_on and due("events_prune", d["rate"]["every_s"]):
        mem["events_prune"] = {"at": now}
        guard("events_prune", _prune_events, d["hooks"]["events_dir"], now,
              d["hooks"]["events_retention_s"])

    for name in (crews if listed is not None else ()):
        if _exiting(kv_get("crew.%s.exiting" % name), now, d["crew"]["exit_window_s"]):
            continue  # a script is exiting this crew: no event, the episode state stays as it was
        a = _find_agent(name, listed, kv)
        prev = last.get(name) or {}
        screen = None
        facts = {}
        sid = kv_get("crew.%s.session" % name)
        if hooks_on and a is not None and hooks.safe_session_id(sid):
            path = os.path.join(d["hooks"]["events_dir"], sid + ".jsonl")
            got = guard("events.%s" % name, _read_events, path, event_files.get(name), sid)
            if got is not None:
                records, event_files[name] = got
                facts = _event_facts(records)
        hook_stop = facts.get("stop") and a.get("agent_status") in ("idle", "done")
        if a is not None:
            pane = a.get("pane_id")
            # an open consent episode is read again on each poll once herdr no longer shows the
            # crew blocked (the dialog can still be up), until a read shows no prompt
            consent_open = (name in ep["consent"]
                            and a.get("agent_status") in ("idle", "done", "working"))
            need = consent_open or (not hook_stop
                                    and (a.get("agent_status") != prev.get("status")
                                         or prev.get("shells") or prev.get("statusline")))
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

        if facts.get("permission") and name not in ep["consent"]:
            ep["consent"][name] = "tool_permission"
            ev["consent"].append("consent %s tool_permission" % name)
        if facts.get("compacted"):
            ev["compacted"].append("compacted %s" % name)

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

        # login: the error line (idle/done) or the login dialog (blocked), read on the status
        # change; one event per episode, which a working crew ends
        if status == "working":
            ep["login"].pop(name, None)
        elif read_now:
            if st.get("login"):
                if name not in ep["login"]:
                    ep["login"][name] = True
                    ev["login"].append("login %s" % name)
            else:
                ep["login"].pop(name, None)

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
            if (status in SETTLED and (status == "blocked" or not bg or hook_stop)
                    and name not in ep["consent"] and name not in ep["provider_error"]
                    and name not in ep["login"] and not on_limit_screen):
                if hook_stop:   # the crew's own Stop event: no screen was needed
                    ev["settled"].append("settled %s done" % name)
                elif settled.get(name) != status:
                    ev["settled"].append("settled %s %s" % (name, status))
                settled[name] = status

    # the machine stays awake while a crew is listed: a timed `caffeinate` the monitor renews
    # when little is left, whichever process the monitor is (it ends on every event)
    if seen_crews and not mem.get("awake_missing") and _awake_void(
            mem.get("awake"), now, mon["awake_min_left_s"]):
        try:
            pid = dp["hold_awake"](mon["awake_hold_s"])
        except FileNotFoundError as e:   # no caffeinate on this host: said once, never retried
            errors.setdefault("awake", "%s: %s" % (type(e).__name__, e))
            if not stopping:    # under the stop the line would be dropped: try again after it
                mem["awake_missing"] = True
                ev["error"].append("error awake")
        except Exception as e:
            errors.setdefault("awake", "%s: %s" % (type(e).__name__, e))
        else:
            mem["awake"] = {"since": now, "until": now + mon["awake_hold_s"],
                            "mono": time.monotonic(),
                            "pid": pid if isinstance(pid, int) else None}

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
        cur = _limit_state(lim, cap)
        prev = mem.get("limit")
        if not isinstance(prev, list) or len(prev) != len(cur):
            prev = [0, False]  # the state of an older form reads as empty
        proj = guard("week", limits.week_projection, lim,
                     (mem.get("week_burn") or {}).get("value"), d, now,
                     off_hours=_off_hours(mem, kv_get, week,
                                          lambda: guard("budget", dp["budget"], kv=kv_arg)))
        notice = proj is not None and _week_notice(mem, proj)
        if notice or any(c > p for c, p in zip(cur, prev)):
            line = "limit %s %s %s %s" % (_fmt(five), _fmt(week), source, cap["mode"])
            ev["limit"].append(line + (" week:%s" % proj["allows"] if notice else ""))
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

    # promised times and consent outcomes: from the kv snapshot only
    lock = kv_get("prorab:active")
    if isinstance(kv_snap, dict):
        _timers(mem, kv, now, lock, errors, ev)
        if listed is not None and not stopping:
            _consent_watches(mem, kv, listed, screens, now, lock, d, dp, errors, ev, guard)

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


# ---- promised times and consent outcomes ------------------------------------------------

def _timers(mem, kv, now, lock, errors, ev):
    """Raise `due <label> <text>` once per timer and foreman pane; remember the next one."""
    fired = mem.setdefault("due", {})
    labels, upcoming = set(), []
    for key, value in sorted(kv.items()):
        if not key.startswith("due.") or value in (None, ""):
            continue
        label = key[len("due."):]
        try:
            rec = json.loads(value)
            at = float(rec["at"])
            text = rec.get("text") or ""
        except (TypeError, ValueError, KeyError, AttributeError):
            errors.setdefault(key, "unreadable timer record")
            continue
        labels.add(label)
        mark = [at, lock]
        if at <= now:
            if fired.get(label) != mark:
                first = str(text).splitlines()[0][:80] if str(text).strip() else ""
                ev["due"].append(("due %s %s" % (label, first)).rstrip())
                fired[label] = mark
        elif fired.get(label) != mark:
            upcoming.append(at)
    for label in list(fired):
        if label not in labels:
            del fired[label]
    mem["next_due"] = min(upcoming) if upcoming else None


def _consent_watches(mem, kv, listed, screens, now, lock, d, dp, errors, ev, guard):
    """Raise `consent_done <work order> <kind>` when the signs of an armed watch hold. Nothing
    is read from a screen that was not read this poll, and nothing is pressed or written."""
    done = mem.setdefault("consent_done", {})
    present, trust_seen = set(), {}
    for key, value in sorted(kv.items()):
        if not key.startswith("consent.") or value in (None, ""):
            continue
        work_order, _, kind = key[len("consent."):].rpartition(".")
        try:
            rec = json.loads(value)
            if not isinstance(rec, dict):
                raise ValueError
        except ValueError:
            errors.setdefault(key, "unreadable consent record")
            continue
        label = "%s %s" % (work_order, kind)
        present.add(label)
        if rec.get("watch") is not True:
            continue
        if _exiting(_kv_value(kv, "crew.%s.exiting" % rec.get("name")), now,
                    d["crew"]["exit_window_s"]):
            continue
        pane, name = rec.get("pane"), rec.get("name")
        if kind == "trust":
            site = rec.get("site")
            if not (pane and site) or any(a.get("pane_id") == pane for a in listed):
                continue
            if site not in trust_seen:
                trust_seen[site] = guard("site_trust.%s" % site, dp["site_trust"], site)
            holds = trust_seen[site] is True
        elif kind in ("mcp", "claude_md_imports", "bypass_warning", "tool_permission"):
            agent = next((a for a in listed if pane and a.get("pane_id") == pane
                          and a.get("name") == name), None)
            holds = (agent is not None
                     and agent.get("agent_status") in ("idle", "done", "working")
                     and not (pane in screens and herdr_cli.consent_dialog(screens[pane]))
                     and name not in mem.get("consent", {}))   # no episode open for the crew
        else:
            holds = False   # api_key and unknown kinds are never inferred
        mark = [rec.get("since"), lock]
        if holds and done.get(label) != mark:
            ev["consent_done"].append("consent_done %s %s" % (work_order, kind))
            done[label] = mark
    for label in list(done):
        if label not in present:
            del done[label]


# ---- main ---------------------------------------------------------------------------

def main(argv=None, *, deps=None):
    mark = write_mark(os.environ.get("HERDR_PANE_ID"))
    try:
        return _run(argv, deps)
    finally:
        remove_mark(mark)


def _stamped(entry, mono, mem):
    """A log row with the uptime clock of its poll (a gap that this clock does not show is a
    sleep) and, while an awake hold is recorded, its end."""
    entry["mono"] = mono
    rec = mem.get("awake")
    if isinstance(rec, dict) and "until" in rec:
        entry["awake_until"] = rec["until"]
    return entry


def _run(argv, deps):
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
        now, mono = time.time(), time.monotonic()
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
            append_log(_stamped({"ts": now, "events": [], "errors": ["poll: %s" % e]},
                                mono, mem))
        else:
            if a.interval is not None:
                sleep_for = a.interval
            elif mem.get("active"):  # a status change, an event or a working crew
                sleep_for = mem["interval"] = base
            else:  # quiet: sleep as planned, then back off for the next time
                mem["interval"] = min(sleep_for * mon["backoff_factor"], mon["interval_max_s"])
            if mem.get("next_due") is not None:   # an exact wake-up for the next timer
                sleep_for = min(sleep_for, max(0.0, mem["next_due"] - time.time()))
            save_mem(mem)
            entry = dict(mem.get("last_reading") or {"ts": now}, events=events)
            if mem.get("poll_errors"):
                entry["errors"] = mem["poll_errors"]
            append_log(_stamped(entry, mono, mem))
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
