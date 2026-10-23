#!/usr/bin/env python3
"""The foreman's journal on beads: work orders, stages, acts, kv, budget, the active-foreman lock.

Every bd call runs with BEADS_DIR set (default ~/projects/prorab/.beads, override
with PRORAB_BEADS_DIR) and with the journal's parent directory as cwd.
"""
import argparse
import contextlib
import datetime
import fcntl
import functools
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402

ACT_KINDS = ("acceptance", "handover", "shift", "answer")
WORK_ORDER_LABEL = "prorab:work-order"
SPENT_KEYS = ("sessions", "runs", "tokens")
ESTIMATE_KEYS = ("usd", "heavy_runs_per_stage")


_THREAD_LOCK = threading.RLock()
_HELD = [0, None]  # how deep this process holds the file lock, and its descriptor


def lock_path():
    base = os.environ.get("PRORAB_CACHE") or os.path.expanduser("~/.cache/prorab")
    return os.path.join(base, "journal.lock")


@contextlib.contextmanager
def _locked():
    """Serialise the journal's read-modify-write sequences across scripts (an `fcntl` lock
    on a file in the cache dir; re-entrant within a process)."""
    with _THREAD_LOCK:
        if _HELD[0] == 0:
            path = lock_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                raise
            _HELD[1] = fd
        _HELD[0] += 1
        try:
            yield
        finally:
            _HELD[0] -= 1
            if _HELD[0] == 0:
                fd, _HELD[1] = _HELD[1], None
                os.close(fd)  # closing releases the flock


def _serialized(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _locked():
            return func(*args, **kwargs)
    return wrapper


class JournalError(Exception):
    closed = ()  # close_work_order: the beads it closed before it failed


def beads_dir():
    return os.environ.get("PRORAB_BEADS_DIR") or os.path.expanduser("~/projects/prorab/.beads")


def _run(args, check=True):
    d = beads_dir()
    env = dict(os.environ, BEADS_DIR=d)
    cwd = os.path.dirname(d.rstrip(os.sep)) or "."
    limit = _defaults.load()["timeouts"]["bd_s"]
    try:
        p = subprocess.run(["bd"] + list(args), capture_output=True, text=True, env=env,
                           cwd=cwd if os.path.isdir(cwd) else None, timeout=limit)
    except FileNotFoundError:
        raise JournalError("bd binary not on PATH")
    except subprocess.TimeoutExpired:
        raise JournalError("bd %s timed out after %s s" % (" ".join(args), limit))
    if check and p.returncode != 0:
        raise JournalError("bd %s failed (%d): %s" % (
            " ".join(args), p.returncode, (p.stderr or p.stdout).strip()))
    return p


def bd(args, json_out=True):
    """Run bd; with json_out append --json and return the parsed value, else raw stdout."""
    p = _run(list(args) + (["--json"] if json_out else []))
    if not json_out:
        return p.stdout
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise JournalError("bd %s: non-JSON output: %s" % (" ".join(args), p.stdout[:200]))


def ensure_db():
    d = beads_dir()
    if os.path.isdir(d):
        return
    parent = os.path.dirname(d.rstrip(os.sep))
    os.makedirs(parent, exist_ok=True)
    # --skip-agents/--skip-hooks: do not drop AGENTS.md, .claude/.codex/.cursor files or git hooks
    _run(["init", "--non-interactive", "--skip-agents", "--skip-hooks", "--prefix", "prorab"])


# ---- kv -------------------------------------------------------------------

def kv_get(key):
    p = _run(["kv", "get", key, "--json"], check=False)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        raise JournalError("bd kv get %s failed: %s" % (key, (p.stderr or p.stdout).strip()))
    if not data.get("found") or data.get("value") in (None, ""):
        return None
    return data["value"]


def kv_all():
    data = bd(["kv", "list"])
    data.pop("schema_version", None)
    return data


def kv_set(key, value):
    _run(["kv", "set", key, str(value)])


def kv_clear(key):
    _run(["kv", "clear", key])


def _kv_int(key, default=0):
    v = kv_get(key)
    return int(v) if v not in (None, "") else default


# ---- work orders and stages ------------------------------------------------------

def _check_estimate(estimate):
    est = dict(estimate or {})
    unknown = sorted(set(est) - set(ESTIMATE_KEYS))
    if unknown:
        raise ValueError("unknown estimate key %s (expected %s)" % (
            ", ".join(unknown), ", ".join(ESTIMATE_KEYS)))
    usd = est.get("usd")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not usd > 0:
        raise ValueError("estimate needs usd > 0")
    runs = est.get("heavy_runs_per_stage")
    if runs is not None and (isinstance(runs, bool) or not isinstance(runs, int) or runs <= 0):
        raise ValueError("estimate heavy_runs_per_stage must be an integer > 0")
    return est


def create_work_order(site, title, body, estimate, goal=None):
    """Create a work order; `estimate` holds `usd` (> 0) and may hold `heavy_runs_per_stage`."""
    est = _check_estimate(estimate)
    labels = "%s,site:%s" % (WORK_ORDER_LABEL, site)
    if goal:
        labels += ",goal:%s" % goal
    nid = bd(["create", title, "-d", body, "-l", labels,
              "--silent"], json_out=False).strip()
    if not nid:
        raise JournalError("bd create returned no id")
    kv_set("estimate.%s.usd" % nid, float(est["usd"]))
    if "heavy_runs_per_stage" in est:
        kv_set("estimate.%s.heavy_runs_per_stage" % nid, est["heavy_runs_per_stage"])
    return nid


def estimate(work_order, kv=None):
    """``{"usd": float | None, "heavy_runs_per_stage": int}`` of a work order."""
    kv = kv_all() if kv is None else kv
    usd = _kv_value(kv, "estimate.%s.usd" % work_order)
    runs = _kv_value(kv, "estimate.%s.heavy_runs_per_stage" % work_order)
    return {"usd": None if usd is None else float(usd),
            "heavy_runs_per_stage": int(runs) if runs is not None
            else machine(kv)["heavy_runs_per_stage"]}


def create_stage(parent, title, body, blocked_by=(), files=(), duration_min=None, kind=None):
    """Create a stage under a work order.

    Storage (bd 1.3.1): files / duration_min / started_at / actual_min live in the
    bead's `metadata` JSON object (bd update merges keys); kind is the label `type:<kind>`.
    """
    if _show(parent).get("status") == "closed":  # a stage added to a closed work order reopens it
        _run(["reopen", parent, "--reason", "a stage was added"])
    meta = {"files": list(files or [])}
    if duration_min is not None:
        meta["duration_min"] = float(duration_min)
    args = ["create", title, "-d", body, "--parent", parent, "--silent",
            "--metadata", json.dumps(meta, ensure_ascii=False)]
    if kind:
        args += ["-l", "type:%s" % kind]
    if blocked_by:
        args += ["--deps", ",".join("blocked-by:%s" % b for b in blocked_by)]
    return bd(args, json_out=False).strip()


def _show(bead):
    items = bd(["show", bead])
    if not items:
        raise JournalError("no such bead: %s" % bead)
    return items[0]


def _set_meta(bead, **kw):
    _run(["update", bead, "--metadata", json.dumps(kw, ensure_ascii=False)])


def _info(item):
    meta = item.get("metadata") or {}
    kind = None
    for lab in item.get("labels") or []:
        if lab.startswith("type:"):
            kind = lab[len("type:"):]
    return {"files": list(meta.get("files") or []),
            "duration_min": meta.get("duration_min"), "kind": kind,
            "started_at": meta.get("started_at"), "actual_min": meta.get("actual_min")}


def stage_info(stage):
    return _info(_show(stage))


def work_order_files(work_order):
    """Union of the files of the work order's stages, in first-seen order."""
    out = []
    for child in bd(["list", "--parent", work_order, "--all", "--limit", "0"]):
        for f in _info(child)["files"]:
            if f not in out:
                out.append(f)
    return out


def start_stage(stage):
    _run(["update", stage, "--status", "in_progress", "--metadata",
          json.dumps({"started_at": time.time()})])


def accept_stage(stage, act):
    """Record the acceptance act and the actual duration (minutes), close the stage."""
    started = stage_info(stage)["started_at"]
    if started is None:
        raise JournalError("stage %s was never started" % stage)
    minutes = (time.time() - float(started)) / 60.0
    add_act(stage, "acceptance", act)
    _set_meta(stage, actual_min=minutes)
    _run(["close", stage])
    return minutes


STAGE_CLOSED_MARKER = "[stage closed without acceptance]"


def close_stage_unaccepted(stage, act):
    """Record the act (kind `acceptance`, starting with `STAGE_CLOSED_MARKER`) on a stage
    someone else did the work of and close it. Refused unless the bead is an open stage of a
    work order; the stage need not have been started."""
    if not act.startswith(STAGE_CLOSED_MARKER):
        raise JournalError("the act must start with %s" % STAGE_CLOSED_MARKER)
    item = _show(stage)
    parent = item.get("parent") or next(
        (d.get("id") for d in item.get("dependencies") or []
         if d.get("dependency_type") == "parent-child"), None)
    if not parent or WORK_ORDER_LABEL not in (_show(parent).get("labels") or []):
        raise JournalError("%s is not a stage of a work order" % stage)
    if item.get("status") == "closed":
        raise JournalError("stage %s is already closed" % stage)
    if _open_gate(item):
        raise JournalError("stage %s has an open gate: the customer answers it first" % stage)
    add_act(stage, "acceptance", act)
    _run(["close", stage])


def durations(kind):
    """Actual durations (minutes) of closed stages of the kind, oldest first."""
    items = bd(["list", "--label", "type:%s" % kind, "--status", "closed", "--limit", "0"])
    items = sorted(items, key=lambda i: i.get("closed_at") or "")
    return [i["metadata"]["actual_min"] for i in items
            if (i.get("metadata") or {}).get("actual_min") is not None]


# ---- the ignore-limits override (customer's words only) --------------------------

OVERRIDE_KEY = "override.ignore_limits"


OVERRIDE_HISTORY_KEY = "override.history"
OVERRIDE_HISTORY_MAX = 20


def override_history(kv=None):
    """Every override ever set, oldest first: ``{"goal", "since", "until", "source"}``;
    `until` is None while it holds. Survives the record's expiry (the morning report reads it)."""
    v = kv_get(OVERRIDE_HISTORY_KEY) if kv is None else _kv_value(kv, OVERRIDE_HISTORY_KEY)
    try:
        data = json.loads(v) if v else []
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _save_history(history):
    kv_set(OVERRIDE_HISTORY_KEY, json.dumps(history[-OVERRIDE_HISTORY_MAX:], ensure_ascii=False))


def _close_history(goal, until):
    history = override_history()
    for rec in history:
        if rec.get("goal") == goal and rec.get("until") is None:
            rec["until"] = until
    _save_history(history)


def set_override(goal):
    since = int(time.time())
    source = "customer's words"
    kv_set(OVERRIDE_KEY, json.dumps({"goal": goal, "since": since, "source": source},
                                    ensure_ascii=False))
    _save_history(override_history() + [{"goal": goal, "since": since, "until": None,
                                         "source": source}])


def clear_override(data=None):
    """Remove the record (the customer cancelled it, or it expired) and close its history entry."""
    if data is None:
        v = kv_get(OVERRIDE_KEY)
        try:
            data = json.loads(v) if v else None
        except ValueError:
            data = None
    kv_clear(OVERRIDE_KEY)
    if isinstance(data, dict) and data.get("goal"):
        _close_history(data["goal"], int(time.time()))


def _open_gate(item):
    return any(d.get("issue_type") == "gate" and d.get("status") != "closed"
               for d in item.get("dependencies") or [])


def has_open_gate(bead):
    return _open_gate(_show(bead))


def close_work_order(work_order, reason):
    """Close the work order's open stages (creation order), then the work order itself; a
    closed bead is skipped. ``{"work_order", "closed": [ids closed now], "already": bool}``;
    a gate on the work order stays as it is."""
    already = _show(work_order).get("status") == "closed"
    closed = []
    for bead in open_stages(work_order) + ([] if already else [work_order]):
        try:
            _run(["close", bead, "--reason", reason])
        except JournalError as e:
            e.closed = closed  # what this call closed before the failure
            raise
        closed.append(bead)
    return {"work_order": work_order, "closed": closed, "already": already}


def _work_order_settled(work_order_id):
    """An open work order is settled when it, or all of its open work, waits for the customer."""
    if _open_gate(_show(work_order_id)):
        return True
    stages = [c for c in bd(["list", "--parent", work_order_id, "--all", "--limit", "0"])
              if c.get("status") != "closed"]
    if not stages:
        return False
    shown = {c["id"]: _show(c["id"]) for c in stages}
    waiting = {i for i, it in shown.items() if _open_gate(it)}
    changed = True
    while changed:  # a stage blocked only by waiting stages waits too
        changed = False
        for i, it in shown.items():
            if i in waiting:
                continue
            blockers = [d["id"] for d in it.get("dependencies") or []
                        if d.get("dependency_type") != "parent-child" and d["id"] in shown]
            if blockers and all(b in waiting for b in blockers):
                waiting.add(i)
                changed = True
    return len(waiting) == len(shown)


def _goal_done(goal):
    """True when the goal has work orders and each is closed or waits for the customer."""
    work_orders = bd(["list", "--label", WORK_ORDER_LABEL, "--label", "goal:%s" % goal,
                  "--no-parent", "--all", "--limit", "0"])
    if not work_orders:
        return False
    return all(n.get("status") == "closed" or _work_order_settled(n["id"]) for n in work_orders)


def _kv_value(kv, key):
    """Value of `key` from a kv_all() snapshot, with the kv_get convention (empty is None)."""
    v = kv.get(key)
    return None if v in (None, "") else v


def override(kv=None):
    """The stored override, or None; expires (and is cleared) once its goal is done.

    With a kv_all() snapshot kv is not read through bd; the goal's work orders are
    still listed, but only while the override is set.
    """
    v = kv_get(OVERRIDE_KEY) if kv is None else _kv_value(kv, OVERRIDE_KEY)
    if v is None:
        return None
    try:
        data = json.loads(v)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get("goal"):
        clear_override()
        return None
    if _goal_done(data["goal"]):
        clear_override(data)
        return None
    return data


def ready():
    out = []
    for it in bd(["ready", "-n", "0"]):  # bd ready lists 100 at most by default
        if WORK_ORDER_LABEL in (it.get("labels") or []) or it.get("parent"):
            out.append(it)
    return out


# ---- the scheduler's view: work orders as units of issue ---------------------------------

def _open_work_orders():
    return [n for n in bd(["list", "--label", WORK_ORDER_LABEL, "--no-parent", "--limit", "0"])
            if n.get("status") != "closed"]


def graph():
    """``{open work order: [open work orders that block it]}`` (stage-level edges are not included)."""
    items = _open_work_orders()
    ids = {n["id"] for n in items}
    out = {}
    for n in items:
        out[n["id"]] = [d["id"] for d in _show(n["id"]).get("dependencies") or []
                        if d.get("dependency_type") != "parent-child" and d["id"] in ids]
    return out


def _creation_order(item):
    """Sort key: bd's `created_at` (whole seconds), then the number after the id's last dot."""
    tail = item["id"].rsplit(".", 1)[-1]
    return (item.get("created_at") or "", int(tail) if tail.isdigit() else 0, item["id"])


def open_stages(work_order):
    """Ids of the work order's stages that are not closed, in creation order (bd lists the
    newest first); the first one is the current stage."""
    items = bd(["list", "--parent", work_order, "--all", "--limit", "0"])
    return [c["id"] for c in sorted(items, key=_creation_order) if c.get("status") != "closed"]


def all_stages(work_order):
    """Ids of all the work order's stages, closed or not, in creation order."""
    items = bd(["list", "--parent", work_order, "--all", "--limit", "0"])
    return [c["id"] for c in sorted(items, key=_creation_order)]


def working_work_orders():
    """Work orders a registered crew is working on."""
    out = []
    for name in crews():
        n = kv_get("crew.%s.work_order" % name)
        if n and n not in out:
            out.append(n)
    return out


def ready_work_orders():
    """Work orders with a ready stage (or ready themselves) and no crew working on them."""
    busy = set(working_work_orders())
    out = []
    for it in ready():
        n = it.get("parent") or it["id"]
        if n not in busy and n not in out:
            out.append(n)
    return out


# ---- acts -------------------------------------------------------------------

def add_act(bead, kind, text):
    if kind not in ACT_KINDS:
        raise ValueError("unknown act kind %r (expected one of %s)" % (kind, ", ".join(ACT_KINDS)))
    bd(["comments", "add", bead, "[act:%s] %s" % (kind, text)])


def act_records(bead, kind=None):
    """Acts of a bead, oldest first: ``[{"kind", "text", "ts"}]`` (`text` without the tag,
    `ts` unix time or None)."""
    comments = bd(["comments", bead]) or []
    comments = sorted(comments, key=lambda c: c.get("created_at", ""))
    out = []
    for c in comments:
        m = re.match(r"\[act:(\w+)\] ?(.*)", c.get("text", ""), re.S)
        if m and (kind is None or m.group(1) == kind):
            out.append({"kind": m.group(1), "text": m.group(2), "ts": _iso_ts(c.get("created_at"))})
    return out


def acts(bead, kind=None):
    """Acts of a bead, oldest first. With `kind`: bodies without the tag; without: full texts."""
    comments = bd(["comments", bead]) or []
    comments = sorted(comments, key=lambda c: c.get("created_at", ""))
    out = []
    for c in comments:
        t = c.get("text", "")
        if not t.startswith("[act:"):
            continue
        if kind is None:
            out.append(t)
            continue
        tag = "[act:%s]" % kind
        if t.startswith(tag):
            out.append(t[len(tag):].lstrip(" "))
    return out


def amend(work_order, text):
    """Append a "## Amendment <n> (<UTC time>)" section with `text` to an open work order's
    body and record an `answer` act "[amendment <n>] <first line>". Criteria are read from the
    whole body, so an amended criterion keeps its id and a new one adds one. n counts the
    amendments so far. Returns ``{"work_order", "amendment", "body"}``."""
    text = (text or "").strip()
    if not text:
        raise JournalError("the amendment is empty")
    item = _show(work_order)
    if WORK_ORDER_LABEL not in (item.get("labels") or []) or item.get("parent"):
        raise JournalError("%s is not a work order" % work_order)
    if item.get("status") == "closed":
        raise JournalError("work order %s is closed" % work_order)
    body = item.get("description") or ""
    n = 1 + max([int(m) for m in re.findall(r"^## Amendment (\d+) \(", body, re.M)] or [0])
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    body = "%s\n\n## Amendment %d (%s)\n%s" % (body, n, stamp, text)
    _run(["update", work_order, "-d", body])
    add_act(work_order, "answer", "[amendment %d] %s" % (n, text.splitlines()[0]))
    return {"work_order": work_order, "amendment": n, "body": body}


# ---- labels -----------------------------------------------------------------

def add_label(bead, label):
    bd(["label", "add", bead, label])


def labels(bead):
    return list(_show(bead).get("labels") or [])


# ---- budget -----------------------------------------------------------------

def budget(kv=None):
    """``{"usd", "week_pp"}`` (floats): the defaults overlaid with kv ``budget.<key>``."""
    b = {k: float(v) for k, v in _defaults.load()["budget"].items()}
    kv = kv_all() if kv is None else kv
    for k in list(b):
        v = kv.get("budget.%s" % k)
        if v not in (None, ""):
            b[k] = float(v)
    return b


def set_budget(**kw):
    known = _defaults.load()["budget"]
    unknown = sorted(set(kw) - set(known))
    if unknown:
        raise ValueError("unknown budget key %s (expected %s)" % (
            ", ".join(unknown), ", ".join(known)))
    for k, v in kw.items():
        kv_set("budget.%s" % k, float(v))


def machine(kv=None):
    """Machine protection (counts, not budget): defaults overlaid with kv ``machine.<key>``."""
    m = dict(_defaults.load()["machine"])
    kv = kv_all() if kv is None else kv
    for k in list(m):
        v = kv.get("machine.%s" % k)
        if v not in (None, ""):
            m[k] = int(v)
    return m


def set_machine(**kw):
    for k, v in kw.items():
        kv_set("machine.%s" % k, v)


@_serialized
def spend(work_order, sessions=0, runs=0, tokens=0):
    for name, n in (("sessions", sessions), ("runs", runs), ("tokens", tokens)):
        if not n:
            continue
        kv_set("spent.%s.%s" % (work_order, name), _kv_int("spent.%s.%s" % (work_order, name)) + n)
        kv_set("shift.spent.%s" % name, _kv_int("shift.spent.%s" % name) + n)


def set_spent(work_order, usd):
    """Record the latest computed total dollars of a work order (never an increment)."""
    kv_set("spent.%s.usd" % work_order, float(usd))


def shift_spent(kv=None):
    kv = kv_all() if kv is None else kv
    out = {k: int(kv.get("shift.spent.%s" % k) or 0) for k in SPENT_KEYS}
    out["usd"] = float(kv.get("shift.spent.usd") or 0)
    return out


def over_budget(week_pp_now, spend=None, kv=None):
    """Items over budget: ``usd``, ``week_pp``, ``estimate:<id>``, ``research:<key>``.

    Without `spend` (the result of `shift_spend`) only ``week_pp`` can appear.
    """
    b = budget(kv=kv)
    over = []  # counts never count as budget
    if spend is not None and spend["usd"] > b["usd"]:
        over.append("usd")
    start = kv_get("shift.week_start") if kv is None else _kv_value(kv, "shift.week_start")
    if week_pp_now is not None and start is not None:
        if float(week_pp_now) - float(start) > b["week_pp"]:
            over.append("week_pp")
    if spend is not None:
        over += ["estimate:%s" % n for n, w in spend["work_orders"].items() if w["over"]]
        over += ["research:%s" % k for k, r in spend["research"].items() if r["over"]]
    return over


def _iso_ts(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _last_acceptance_ts(work_order):
    """Time of the last `acceptance` act on the work order or on any of its stages."""
    beads = [work_order] + [c["id"] for c in bd(["list", "--parent", work_order, "--all",
                                                 "--limit", "0"])]
    best = None
    for bead in beads:
        for c in bd(["comments", bead]) or []:
            if c.get("text", "").startswith("[act:acceptance]"):
                ts = _iso_ts(c.get("created_at"))
                if ts is not None and (best is None or ts > best):
                    best = ts
    return best


def _closed_at(work_order):
    """When the work-order bead was closed (unix time): bd's `closed_at`, else the time of
    its last acceptance act; None when it is open or no time is known."""
    item = _show(work_order)
    if item.get("status") != "closed":
        return None
    ts = _iso_ts(item.get("closed_at"))
    return ts if ts is not None else _last_acceptance_ts(work_order)


def begin_shift(week_pct, foreman_session=None):
    for k in SPENT_KEYS + ("usd",):
        kv_set("shift.spent.%s" % k, 0)
    if week_pct is None:
        kv_clear("shift.week_start")
    else:
        kv_set("shift.week_start", week_pct)
    kv_set("shift.budget", json.dumps(budget()))
    started = time.time()
    kv_set("shift.started_at", started)
    if foreman_session:
        kv_set("shift.foreman_sessions", foreman_session)
    else:
        kv_clear("shift.foreman_sessions")
    # R44: read once per shift, so the monitor's spend recomputation never starts bd
    closed = []
    for n, rec in issues().items():
        if rec.get("worktree"):
            at = _closed_at(n)
            if at is not None and at < started:
                closed.append(n)
    if closed:
        kv_set("shift.closed_before", ",".join(sorted(closed)))
    else:
        kv_clear("shift.closed_before")


def _foreman_sessions(kv):
    v = _kv_value(kv, "shift.foreman_sessions")
    return [x for x in v.split(",") if x] if v else []


@_serialized
def add_foreman_session(session_id):
    """A foreman handover adds the new session; the shift's spend is kept."""
    names = _foreman_sessions(kv_all())
    if session_id not in names:
        kv_set("shift.foreman_sessions", ",".join(names + [session_id]))


# ---- research items and spend in dollars --------------------------------------------

def research_begin(key, estimate_usd, session_id, run_cwd=None):
    if isinstance(estimate_usd, bool) or not isinstance(estimate_usd, (int, float)) \
            or not estimate_usd > 0:
        raise ValueError("research estimate needs usd > 0")
    kv_set("research.%s" % key, json.dumps(
        {"estimate_usd": float(estimate_usd), "since": time.time(),
         "session": session_id, "run_cwd": run_cwd}, ensure_ascii=False))


def research_items(kv=None):
    kv = kv_all() if kv is None else kv
    out = {}
    for k, v in kv.items():
        if not k.startswith("research."):
            continue
        try:
            rec = json.loads(v)
        except (TypeError, ValueError):
            continue
        if isinstance(rec, dict):
            out[k[len("research."):]] = rec
    return out


def _first_ts(path):
    import transcript
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                ts = transcript._parse_ts(entry.get("timestamp")) if isinstance(entry, dict) else None
                if ts is not None:
                    return ts
    except OSError:
        pass
    return None


def _attribute_research(items, foreman_subagents, foreman_files, home):
    """``{key: [files]}``: each candidate file goes to the latest item registered before the
    file's first entry. Candidates: a foreman subagent file for the items of its session; for
    an item with `run_cwd`, a session file of that directory that is not a foreman file."""
    import transcript
    files = {k: [] for k in items}
    by_since = sorted(items.items(), key=lambda kv_: kv_[1].get("since") or 0)

    def give(path, eligible):
        ts = _first_ts(path)
        if ts is None:
            return
        owner = None
        for key, rec in by_since:
            if key in eligible and (rec.get("since") or 0) <= ts:
                owner = key
        if owner is not None:
            files[owner].append(path)
    for session, paths in foreman_subagents.items():
        eligible = {k for k, rec in items.items() if rec.get("session") == session}
        for path in paths:
            give(path, eligible)
    for cwd in {rec.get("run_cwd") for rec in items.values() if rec.get("run_cwd")}:
        eligible = {k for k, rec in items.items() if rec.get("run_cwd") == cwd}
        for path in transcript.worktree_files(cwd, home):
            if path not in foreman_files:
                give(path, eligible)
    return files


def shift_spend(d, now=None, kv=None, home=None):
    """Dollars of the shift: work orders, research items and the foreman; no `bd` process
    when `kv` is given. A message counts once, in the first category that holds it."""
    import transcript
    kv = kv_all() if kv is None else kv
    start = _kv_value(kv, "shift.started_at")
    since = float(start) if start is not None else None
    skipped = set((_kv_value(kv, "shift.closed_before") or "").split(","))
    seen = {}
    out = {"since": since, "usd": 0.0, "work_orders": {}, "research": {}, "foreman_usd": 0.0,
           "at": now}
    for n, rec in sorted(issues(kv=kv).items()):
        if not rec.get("worktree") or n in skipped:
            continue
        files = transcript.work_order_files(rec["worktree"], rec.get("sessions") or [], home,
                                            own_only=bool(rec.get("main_checkout")))
        usd_shift = transcript.cost(files, d, since=since, seen=seen)["usd"]
        before = 0.0 if since is None else transcript.cost(files, d, until=since)["usd"]
        est = estimate(n, kv)["usd"]
        total = usd_shift + before
        out["work_orders"][n] = {"usd_shift": usd_shift, "usd_total": total,
                                 "estimate_usd": est, "over": est is not None and total > est}
        out["usd"] += usd_shift
    foreman_main, foreman_subs = [], {}
    for session in _foreman_sessions(kv):
        path = transcript.find_transcript("", session, home)
        if path is None:
            continue
        foreman_main.append(path)
        foreman_subs[session] = transcript.session_cost_files(path)[1:]
    items = research_items(kv)
    foreman_files = set(foreman_main) | {p for ps in foreman_subs.values() for p in ps}
    taken = _attribute_research(items, foreman_subs, foreman_files, home)
    for key, rec in sorted(items.items()):
        usd = transcript.cost(taken[key], d, since=since, seen=seen)["usd"]
        est = rec.get("estimate_usd")
        out["research"][key] = {"usd": usd, "estimate_usd": est,
                                "over": est is not None and usd > est}
        out["usd"] += usd
    claimed = {p for ps in taken.values() for p in ps}
    rest = foreman_main + [p for ps in foreman_subs.values() for p in ps if p not in claimed]
    out["foreman_usd"] = transcript.cost(rest, d, since=since, seen=seen)["usd"]
    out["usd"] += out["foreman_usd"]
    return out


def rate_text(rate):
    per = rate.get("usd_per_pp")
    if per is None:
        return "unknown"
    if rate.get("source") == "estimated":
        return "%.2f $/pp (estimated over %d intervals)" % (per, rate.get("intervals", 0))
    return "%.2f $/pp (%s)" % (per, rate.get("source", "default"))


def spend_block(spend, rates, work_order=None, kv=None):
    """The `Spend:` block appended to stage acts and the foreman's handover act."""
    import limits
    kv = kv_all() if kv is None else kv
    lines = ["Spend:"]
    if work_order:
        w = spend.get("work_orders", {}).get(work_order) or {}
        usd = w.get("usd_total", 0.0)
        est = w.get("estimate_usd", estimate(work_order, kv)["usd"])
        line = "- work order %s: $%.2f" % (work_order, usd)
        if est is not None:
            line += " of $%.2f estimated" % est
        parts = []
        for label, key in (("window", "five_hour"), ("week", "seven_day")):
            pp = limits.to_pp(usd, rates[key])
            if pp is not None:
                parts.append("%.2f pp of the %s" % (pp, label))
        if parts:
            line += " (%s)" % ", ".join(parts)
        lines.append(line)
    lines.append("- shift: $%.2f of $%.2f budget" % (spend.get("usd", 0.0), budget(kv)["usd"]))
    lines.append("- window rate: " + rate_text(rates["five_hour"]))
    lines.append("- week rate: " + rate_text(rates["seven_day"]))
    return "\n".join(lines)


# ---- cycle counters, one push per gate, site passport, issue records ---------

def _cycle_limits(d=None):
    return (d if d is not None else _defaults.load())["cycles"]


def _iter_key(stage, kind):
    if kind not in _cycle_limits():
        raise ValueError("unknown cycle kind %r (expected one of %s)" % (
            kind, ", ".join(_cycle_limits())))
    return "iterations.%s.%s" % (stage, kind)


def iterations(stage, kind):
    return _kv_int(_iter_key(stage, kind))


@_serialized
def bump_iterations(stage, kind):
    key = _iter_key(stage, kind)
    n = _kv_int(key) + 1
    kv_set(key, n)
    return n


def cycle_exceeded(stage, kind, d=None):
    """True when the count passed the limit: the 7th approval request means a gate."""
    _iter_key(stage, kind)
    return iterations(stage, kind) > _cycle_limits(d)[kind]


def mark_notified(key):
    """True the first time for a gate id or incident key (and records it), False after."""
    k = "notified.%s" % key
    if kv_get(k) is not None:
        return False
    kv_set(k, int(time.time()))
    return True


def notified():
    return {k[len("notified."):]: v for k, v in kv_all().items() if k.startswith("notified.")}


PASSPORT_LABEL = "prorab:passport"


def passport(site):
    """The site passport, ``{"id", "text"}``, or None. It lives in the journal only."""
    items = bd(["list", "--label", PASSPORT_LABEL, "--label", "site:%s" % site,
                "--all", "--limit", "0"])
    if not items:
        return None
    return {"id": items[0]["id"], "text": items[0].get("description") or ""}


def set_passport(site, text):
    found = passport(site)
    if found:
        _run(["update", found["id"], "-d", text])
        return found["id"]
    nid = bd(["create", "Passport: %s" % site, "-d", text,
              "-l", "%s,site:%s" % (PASSPORT_LABEL, site), "--silent"], json_out=False).strip()
    if not nid:
        raise JournalError("bd create returned no id")
    return nid


ISSUE_STATES = ("issuing", "issued", "not_issued")


def issue(work_order):
    v = kv_get("issue.%s" % work_order)
    try:
        data = json.loads(v) if v else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@_serialized
def issue_set(work_order, state, **fields):
    """Record the issue state of a work order; fields merge into the existing record."""
    if state not in ISSUE_STATES:
        raise ValueError("unknown issue state %r (expected one of %s)" % (
            state, ", ".join(ISSUE_STATES)))
    rec = issue(work_order) or {}
    rec.update(fields)
    rec["state"] = state
    rec["ts"] = time.time()
    kv_set("issue.%s" % work_order, json.dumps(rec, ensure_ascii=False))


def issues(state=None, kv=None):
    out = {}
    for k, v in (kv_all() if kv is None else kv).items():
        if not k.startswith("issue."):
            continue
        try:
            rec = json.loads(v)
        except (TypeError, ValueError):
            continue
        if isinstance(rec, dict) and (state is None or rec.get("state") == state):
            out[k[len("issue."):]] = rec
    return out


# ---- active-foreman lock -----------------------------------------------------

HANDOFF_KEY = "prorab:handoff"


def handoff():
    """The pending handoff, ``{"from", "to", ...}``, or None; an older record that is just the
    new pane's id reads as ``{"from": None, "to": <pane>}``."""
    v = kv_get(HANDOFF_KEY)
    if v is None:
        return None
    try:
        rec = json.loads(v)
    except ValueError:
        rec = None
    if isinstance(rec, dict) and rec.get("to"):
        return rec
    return {"from": None, "to": v}


def handoff_set(from_pane, to_pane, **fields):
    kv_set(HANDOFF_KEY, json.dumps(dict(fields, **{"from": from_pane, "to": to_pane}),
                                   ensure_ascii=False))


@_serialized
def acquire_active(pane_id):
    """Take the active-foreman lock if free, own, handed off to us by its current holder, or
    held by a dead pane. A handoff whose `from` is not the current holder is stale (the
    holder changed since) and is not honoured."""
    holder = kv_get("prorab:active")
    if holder is None or holder == pane_id:
        kv_set("prorab:active", pane_id)
        return True
    h = handoff()
    if h is not None and h["to"] == pane_id and h.get("from") in (None, holder):
        kv_set("prorab:active", pane_id)
        kv_clear(HANDOFF_KEY)
        return True
    import herdr_cli  # lazy: only lock checks need herdr
    if holder not in {a.get("pane_id") for a in herdr_cli.agents()}:
        kv_set("prorab:active", pane_id)
        return True
    return False


# ---- waiting for the customer, restarts, crews ------------------------------

def _new_gate(bead, reason):
    gate = bd(["gate", "create", "--type", "human", "--blocks", bead, "--reason", reason])
    add_act(bead, "answer", reason)
    return gate["id"]


_CONSENT_REASON = re.compile(r"^consent prompt: (\w+)")


def consent_kind(reason):
    """The kind word of a reason of the form ``consent prompt: <kind>``, else None."""
    m = _CONSENT_REASON.match(reason or "")
    return m.group(1) if m else None


def wait_customer(bead, reason):
    kind = consent_kind(reason)
    if kind is not None:
        return record_consent(bead, kind, reason)["gate"]
    return _new_gate(bead, reason)


# ---- promised times: timers in the office journal ------------------------------------------

DUE_LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


def due_at(clock, now, tz=None):
    """The next time the local clock shows `clock` (HH:MM): that wall time on `now`'s local
    date when it is not before `now`, else on the next date. Built from the local wall time,
    so a daylight-saving change never moves it."""
    m = _CLOCK.match(clock or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("expected HH:MM between 00:00 and 23:59, got %r" % (clock,))
    hh, mm = int(m.group(1)), int(m.group(2))
    local = datetime.datetime.fromtimestamp(now, tz)
    day = local.date()
    for _ in range(2):
        wall = datetime.datetime(day.year, day.month, day.day, hh, mm)
        ts = (wall.replace(tzinfo=tz) if tz is not None else wall).timestamp()
        if ts >= now:
            return ts
        day += datetime.timedelta(days=1)
    return ts


def _at_local(at):
    return datetime.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M")


def set_due(label, at, text=""):
    """Write the timer `due.<label>`; a second call of a label replaces it."""
    if not isinstance(label, str) or not DUE_LABEL.match(label):
        raise ValueError("bad timer label %r (expected %s)" % (label, DUE_LABEL.pattern))
    rec = {"at": float(at), "text": text, "set_at": time.time()}
    kv_set("due.%s" % label, json.dumps(rec, ensure_ascii=False))
    return dict(rec, label=label, at_local=_at_local(rec["at"]))


def dues(kv=None, now=None):
    """Every timer by `at`, with `label`, `at_local` and `overdue`; an unreadable one is
    ``{"label", "error": "unreadable"}``."""
    kv = kv_all() if kv is None else kv
    now = time.time() if now is None else now
    good, bad = [], []
    for k, v in kv.items():
        if not k.startswith("due."):
            continue
        label = k[len("due."):]
        try:
            rec = json.loads(v)
            at = float(rec["at"])
            if not isinstance(rec, dict):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            bad.append({"label": label, "error": "unreadable"})
            continue
        good.append(dict(rec, label=label, at=at, at_local=_at_local(at), overdue=at <= now))
    return sorted(good, key=lambda r: r["at"]) + sorted(bad, key=lambda r: r["label"])


def clear_due(label):
    if kv_get("due.%s" % label) is None:
        return False
    kv_clear("due.%s" % label)
    return True


def _monitor_alive():
    """True when a monitor mark of the log directory names a pid that is alive."""
    import glob
    import monitor  # lazy: only this check needs it
    for path in glob.glob(os.path.join(monitor.log_dir(), "monitor-*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                pid = int(json.load(fh)["pid"])
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


# ---- consent gates: records, watches and their outcome -------------------------------------

def _json_record(value):
    try:
        rec = json.loads(value) if value else None
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _work_order_of(bead, kv):
    """`bead` when it has an issue record, else its parent when that has one, else None."""
    if _kv_value(kv, "issue.%s" % bead):
        return bead
    item = _show(bead)
    parent = item.get("parent") or next(
        (d.get("id") for d in item.get("dependencies") or []
         if d.get("dependency_type") == "parent-child"), None)
    if parent and _kv_value(kv, "issue.%s" % parent):
        return parent
    return None


def record_consent(bead, kind, gate_reason):
    """Gate a consent prompt and, when the journal can place it, arm its watch.
    Returns ``{"gate", "record", "no_watch", "reused"}``."""
    import herdr_cli  # lazy
    kv = kv_all()
    work_order = _work_order_of(bead, kv)
    if work_order is None:
        return {"gate": _new_gate(bead, gate_reason), "record": None,
                "no_watch": "no_issue_record", "reused": False}
    if kind not in herdr_cli.CONSENT_NAMES:
        return {"gate": _new_gate(bead, gate_reason), "record": None,
                "no_watch": "unknown_kind", "reused": False}
    key = "consent.%s.%s" % (work_order, kind)
    open_rec = _json_record(_kv_value(kv, key))
    if open_rec is not None and open_rec.get("gate"):
        open_rec.update(watch=False, no_watch="repeat")
        kv_set(key, json.dumps(open_rec, ensure_ascii=False))
        return {"gate": open_rec["gate"], "record": open_rec, "no_watch": "repeat",
                "reused": True}
    gate = _new_gate(bead, gate_reason)
    issue_rec = _json_record(_kv_value(kv, "issue.%s" % work_order)) or {}
    name = issue_rec.get("name")
    pane = issue_rec.get("pane")
    if name and name in crews(kv):
        pane = _kv_value(kv, "crew.%s.pane" % name) or pane
    site = issue_rec.get("site") or (_kv_value(kv, "crew.%s.site" % name) if name else None)
    started = _kv_value(kv, "shift.started_at")
    seen = []
    try:
        seen = json.loads(_kv_value(kv, "consent_seen.%s" % work_order) or "[]")
    except ValueError:
        pass
    seen = [x for x in seen if isinstance(x, dict)] if isinstance(seen, list) else []
    repeat = any(x.get("kind") == kind and (started is None or float(x.get("at") or 0) >= float(started))
                 for x in seen)
    no_watch = None
    if kind == "api_key":
        no_watch = "api_key"
    elif repeat:
        no_watch = "repeat"
    elif not pane:
        no_watch = "no_pane"
    elif kind == "trust" and not site:
        no_watch = "no_site"
    now = time.time()
    rec = {"kind": kind, "gate": gate, "bead": bead, "pane": pane, "name": name, "site": site,
           "since": now, "watch": no_watch is None, "no_watch": no_watch}
    if no_watch is None:
        kv_set("consent_seen.%s" % work_order,
               json.dumps(seen + [{"kind": kind, "at": now}], ensure_ascii=False))
    kv_set(key, json.dumps(rec, ensure_ascii=False))
    return {"gate": gate, "record": rec, "no_watch": no_watch, "reused": False}


def consents(kv=None):
    """``{(work order, kind): record}`` for every ``consent.<work order>.<kind>`` that parses."""
    kv = kv_all() if kv is None else kv
    out = {}
    for k, v in kv.items():
        if not k.startswith("consent."):
            continue
        work_order, _, kind = k[len("consent."):].rpartition(".")
        rec = _json_record(v)
        if work_order and kind and rec is not None:
            out[(work_order, kind)] = rec
    return out


def _resolve_gate(gate):
    """True when this call closed the gate; False when it was closed already or bd refuses."""
    p = _run(["show", gate, "--json"], check=False)
    try:
        if json.loads(p.stdout)[0].get("status") == "closed":
            return False
    except (ValueError, IndexError, AttributeError, TypeError):
        return False
    return _run(["gate", "resolve", gate], check=False).returncode == 0


def consent_done(work_order, kind, by="monitor", gate=None):
    """The customer's part of a consent gate is done: resolve the gate, drop the watch, record
    the act. A gate without a record is resolved only with `gate` and `by="customer"`."""
    if by not in ("monitor", "customer"):
        raise ValueError("by must be monitor or customer")
    if gate is not None and by != "customer":
        raise ValueError("--gate needs --by customer")
    key = "consent.%s.%s" % (work_order, kind)
    rec = _json_record(kv_get(key))
    if rec is None and gate is None:
        return {"work_order": work_order, "kind": kind, "found": False}
    if rec is not None:
        gate_id, bead = rec.get("gate"), rec.get("bead") or work_order
        where = " in pane %s" % rec["pane"] if rec.get("pane") else ""
    else:
        gate_id, bead, where = gate, work_order, ""
    resolved = _resolve_gate(gate_id) if gate_id else False
    if rec is not None or kv_get(key) is not None:
        kv_clear(key)
    add_act(bead, "answer", "consent done: %s answered by the customer%s (seen by %s)"
            % (kind, where, by))
    return {"work_order": work_order, "kind": kind, "found": True, "gate": gate_id,
            "resolved": resolved}


@_serialized
def bump_restarts(work_order):
    n = _kv_int("restarts.%s" % work_order) + 1
    kv_set("restarts.%s" % work_order, n)
    return n


def crews(kv=None):
    v = kv_get("crews.active") if kv is None else _kv_value(kv, "crews.active")
    return [x for x in v.split(",") if x] if v else []


def _save_crews(names):
    if names:
        kv_set("crews.active", ",".join(names))
    else:
        kv_clear("crews.active")


@_serialized
def register_crew(name):
    names = crews()
    if name not in names:
        _save_crews(names + [name])


@_serialized
def unregister_crew(name):
    _save_crews([n for n in crews() if n != name])


# ---- CLI --------------------------------------------------------------------

def _pairs(items):
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit("expected key=value, got %r" % it)
        out[k] = v
    return out


def _emit(value):
    print(json.dumps(value, ensure_ascii=False))
    return 0


# The only matters that justify a push notification (the spec's urgent list).
URGENT_KINDS = ("limit", "all_waiting", "foreman_failure", "api_key")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure")
    p = sub.add_parser("work-order")
    p.add_argument("site"); p.add_argument("title")
    p.add_argument("--body", default=""); p.add_argument("--estimate-usd", type=float, required=True)
    p.add_argument("--heavy-runs", type=int); p.add_argument("--goal")
    p = sub.add_parser("stage")
    p.add_argument("parent"); p.add_argument("title")
    p.add_argument("--body", default=""); p.add_argument("--blocked-by", action="append", default=[])
    p.add_argument("--file", action="append", default=[])
    p.add_argument("--duration", type=float); p.add_argument("--kind")
    for name in ("stage-info", "stage-start", "work-order-files"):
        sub.add_parser(name).add_argument("bead")
    p = sub.add_parser("stage-accept")
    p.add_argument("stage"); p.add_argument("act")
    p = sub.add_parser("stage-close")
    p.add_argument("stage"); p.add_argument("--act-file", required=True)
    sub.add_parser("durations").add_argument("kind")
    p = sub.add_parser("override")
    osub = p.add_subparsers(dest="ovcmd", required=True)
    osub.add_parser("get"); osub.add_parser("clear")
    osub.add_parser("set").add_argument("goal")
    p = sub.add_parser("close-work-order")
    p.add_argument("work_order"); p.add_argument("--reason", required=True)
    sub.add_parser("ready")
    sub.add_parser("graph")
    sub.add_parser("ready-work-orders")
    sub.add_parser("open-stages").add_argument("work_order")
    p = sub.add_parser("label")
    lsub = p.add_subparsers(dest="lcmd", required=True)
    g = lsub.add_parser("add"); g.add_argument("bead"); g.add_argument("label")
    lsub.add_parser("list").add_argument("bead")
    p = sub.add_parser("act")
    p.add_argument("bead"); p.add_argument("kind"); p.add_argument("text")
    p = sub.add_parser("acts")
    p.add_argument("bead"); p.add_argument("--kind")
    p = sub.add_parser("amend")
    p.add_argument("work_order"); p.add_argument("--file", required=True)
    p = sub.add_parser("budget")
    p.add_argument("--set", action="append", default=[])
    p = sub.add_parser("machine")
    p.add_argument("--set", action="append", default=[])
    p = sub.add_parser("spend")
    p.add_argument("work_order")
    for k in SPENT_KEYS:
        p.add_argument("--" + k, type=int, default=0)
    sub.add_parser("spent")
    p = sub.add_parser("over")
    p.add_argument("--week-pp", type=float)
    p = sub.add_parser("shift-begin")
    p.add_argument("--week-pct", type=float); p.add_argument("--foreman-session")
    sub.add_parser("foreman-session").add_argument("session")
    p = sub.add_parser("research")
    p.add_argument("key"); p.add_argument("--estimate-usd", type=float, required=True)
    p.add_argument("--session"); p.add_argument("--run-cwd")
    p = sub.add_parser("shift-spend")
    p.add_argument("--week-pp", type=float)
    p = sub.add_parser("lock")
    p.add_argument("pane_id")
    p = sub.add_parser("wait")
    p.add_argument("bead"); p.add_argument("reason")
    p = sub.add_parser("due")
    dsub = p.add_subparsers(dest="duecmd", required=True)
    g = dsub.add_parser("set"); g.add_argument("label")
    grp = g.add_mutually_exclusive_group(required=True)
    grp.add_argument("--at"); grp.add_argument("--in", dest="in_min", type=float)
    g.add_argument("--text", default="")
    dsub.add_parser("list")
    dsub.add_parser("clear").add_argument("label")
    p = sub.add_parser("consent")
    csub = p.add_subparsers(dest="concmd", required=True)
    csub.add_parser("list")
    g = csub.add_parser("done"); g.add_argument("work_order"); g.add_argument("kind")
    g.add_argument("--by", choices=("monitor", "customer"), default="monitor")
    g.add_argument("--gate")
    p = sub.add_parser("restarts")
    p.add_argument("work_order"); p.add_argument("--bump", action="store_true")
    p = sub.add_parser("crews")
    p.add_argument("--add"); p.add_argument("--remove")
    p = sub.add_parser("iterations")
    p.add_argument("stage"); p.add_argument("kind"); p.add_argument("--bump", action="store_true")
    p = sub.add_parser("notify")
    p.add_argument("key"); p.add_argument("--kind", required=True, choices=URGENT_KINDS)
    p = sub.add_parser("passport")
    p.add_argument("site"); p.add_argument("--set-file")
    p = sub.add_parser("issues")
    p.add_argument("--state")
    p = sub.add_parser("kv")
    ksub = p.add_subparsers(dest="kvcmd", required=True)
    g = ksub.add_parser("get"); g.add_argument("key")
    s = ksub.add_parser("set"); s.add_argument("key"); s.add_argument("value")
    a = ap.parse_args(_defaults.cli_argv(sys.argv[1:] if argv is None else argv))
    if a.cmd == "due" and a.duecmd == "set":
        if a.in_min is not None and not a.in_min > 0:
            ap.error("--in needs minutes above 0")
        if a.at is not None:
            try:
                due_at(a.at, time.time())
            except ValueError as e:
                ap.error(str(e))
        if not DUE_LABEL.match(a.label):
            ap.error("bad timer label %r (expected %s)" % (a.label, DUE_LABEL.pattern))
    if a.cmd == "consent" and a.concmd == "done" and a.gate and a.by != "customer":
        ap.error("--gate needs --by customer")

    if a.cmd == "ensure":
        ensure_db()
        return _emit({"ok": True})
    if a.cmd == "research" and not a.session:
        import herdr_cli  # lazy, like the lock checks
        a.session = herdr_cli.own_session()
        if not a.session:
            _emit({"error": "foreman session unknown: pass --session"})
            return 1
    ensure_db()
    if a.cmd == "work-order":
        est = {"usd": a.estimate_usd}
        if a.heavy_runs is not None:
            est["heavy_runs_per_stage"] = a.heavy_runs
        try:
            nid = create_work_order(a.site, a.title, a.body, est, goal=a.goal)
        except ValueError as e:
            raise SystemExit(str(e))
        return _emit({"id": nid})
    if a.cmd == "stage":
        return _emit({"id": create_stage(
            a.parent, a.title, a.body, a.blocked_by, a.file, a.duration, a.kind)})
    if a.cmd == "stage-info":
        return _emit(stage_info(a.bead))
    if a.cmd == "work-order-files":
        return _emit(work_order_files(a.bead))
    if a.cmd == "stage-start":
        start_stage(a.bead)
        return _emit({"ok": True})
    if a.cmd == "stage-accept":
        accept_stage(a.stage, a.act)
        return _emit(stage_info(a.stage))
    if a.cmd == "stage-close":
        try:
            with open(a.act_file, encoding="utf-8") as fh:
                act = fh.read().strip()
        except (OSError, ValueError) as e:
            raise SystemExit("cannot read the act file %s: %s" % (a.act_file, e))
        try:
            close_stage_unaccepted(a.stage, act)
        except JournalError as e:
            raise SystemExit(str(e))
        return _emit({"ok": True, "stage": a.stage})
    if a.cmd == "durations":
        return _emit(durations(a.kind))
    if a.cmd == "override":
        if a.ovcmd == "set":
            set_override(a.goal)
        elif a.ovcmd == "clear":
            clear_override()
        return _emit({"override": override()})
    if a.cmd == "close-work-order":
        return _emit(close_work_order(a.work_order, a.reason))
    if a.cmd == "ready":
        return _emit(ready())
    if a.cmd == "graph":
        return _emit(graph())
    if a.cmd == "ready-work-orders":
        return _emit(ready_work_orders())
    if a.cmd == "open-stages":
        return _emit(open_stages(a.work_order))
    if a.cmd == "label":
        if a.lcmd == "add":
            add_label(a.bead, a.label)
        return _emit(labels(a.bead))
    if a.cmd == "act":
        add_act(a.bead, a.kind, a.text)
        return _emit({"ok": True})
    if a.cmd == "acts":
        return _emit(acts(a.bead, a.kind))
    if a.cmd == "amend":
        try:
            with open(a.file, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, ValueError) as e:
            raise SystemExit("cannot read the amendment file %s: %s" % (a.file, e))
        try:
            return _emit(amend(a.work_order, text))
        except JournalError as e:
            raise SystemExit(str(e))
    if a.cmd == "budget":
        kw = {k: float(v) for k, v in _pairs(a.set).items()}
        if kw:
            try:
                set_budget(**kw)
            except ValueError as e:
                raise SystemExit(str(e))
        return _emit(budget())
    if a.cmd == "machine":
        kw = {k: int(v) for k, v in _pairs(a.set).items()}
        if kw:
            set_machine(**kw)
        return _emit(machine())
    if a.cmd == "spend":
        spend(a.work_order, a.sessions, a.runs, a.tokens)
        return _emit(shift_spent())
    if a.cmd == "spent":
        return _emit(shift_spent())
    if a.cmd == "over":
        return _emit(over_budget(a.week_pp))
    if a.cmd == "shift-begin":
        begin_shift(a.week_pct, a.foreman_session)
        return _emit({"ok": True})
    if a.cmd == "foreman-session":
        add_foreman_session(a.session)
        return _emit({"ok": True})
    if a.cmd == "shift-spend":
        sp = shift_spend(_defaults.load())
        sp["over"] = over_budget(a.week_pp, sp)
        return _emit(sp)
    if a.cmd == "research":
        kv = kv_all()
        left = max(0.0, budget(kv)["usd"] - shift_spend(_defaults.load(), kv=kv)["usd"])
        lifted = override(kv=kv) is not None  # R42: "ignore limits and finish" lifts the budget
        if a.estimate_usd > left and not lifted:
            _emit({"fits": False, "left_usd": left})
            return 1
        research_begin(a.key, a.estimate_usd, a.session, a.run_cwd)
        res = {"fits": True, "left_usd": left}
        if lifted:
            res["override"] = True
        return _emit(res)
    if a.cmd == "lock":
        got = acquire_active(a.pane_id)
        _emit({"acquired": got})
        return 0 if got else 1
    if a.cmd == "wait":
        kind = consent_kind(a.reason)
        if kind is None:
            return _emit({"gate": wait_customer(a.bead, a.reason), "watch": None,
                          "no_watch": None, "reused": False})
        res = record_consent(a.bead, kind, a.reason)
        rec = res["record"]
        return _emit({"gate": res["gate"], "watch": rec if rec and rec.get("watch") else None,
                      "no_watch": res["no_watch"], "reused": res["reused"]})
    if a.cmd == "due":
        if a.duecmd == "set":
            now = time.time()
            at = due_at(a.at, now) if a.at is not None else now + a.in_min * 60
            return _emit(dict(set_due(a.label, at, a.text), monitor=_monitor_alive()))
        if a.duecmd == "list":
            return _emit(dues())
        return _emit({"label": a.label, "cleared": clear_due(a.label)})
    if a.cmd == "consent":
        if a.concmd == "list":
            return _emit([dict(r, work_order=w) for (w, _k), r in sorted(consents().items())])
        return _emit(consent_done(a.work_order, a.kind, by=a.by, gate=a.gate))
    if a.cmd == "restarts":
        n = bump_restarts(a.work_order) if a.bump else _kv_int("restarts.%s" % a.work_order)
        return _emit({"restarts": n})
    if a.cmd == "crews":
        if a.add:
            register_crew(a.add)
        if a.remove:
            unregister_crew(a.remove)
        return _emit(crews())
    if a.cmd == "iterations":
        if a.bump:
            bump_iterations(a.stage, a.kind)
        return _emit({"n": iterations(a.stage, a.kind), "limit": _cycle_limits()[a.kind],
                      "exceeded": cycle_exceeded(a.stage, a.kind)})
    if a.cmd == "notify":
        return _emit({"push": mark_notified(a.key)})
    if a.cmd == "passport":
        if a.set_file:
            with open(a.set_file, encoding="utf-8") as fh:
                set_passport(a.site, fh.read())
        return _emit(passport(a.site))
    if a.cmd == "issues":
        return _emit(issues(a.state))
    if a.cmd == "kv":
        if a.kvcmd == "get":
            return _emit({"key": a.key, "value": kv_get(a.key)})
        kv_set(a.key, a.value)
        return _emit({"key": a.key, "value": a.value})
    return 2


if __name__ == "__main__":
    sys.exit(main())
