#!/usr/bin/env python3
"""The prorab's journal on beads: наряды, stages, acts, kv, budget, the active-prorab lock.

Every bd call runs with BEADS_DIR set (default ~/projects/prorab/.beads, override
with PRORAB_BEADS_DIR) and with the journal's parent directory as cwd.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402

ACT_KINDS = ("acceptance", "handover", "shift", "answer")
WORK_ORDER_LABEL = "prorab:work-order"
SPENT_KEYS = ("sessions", "runs", "tokens")
ESTIMATE_KEYS = ("usd", "heavy_runs_per_stage")


class JournalError(Exception):
    pass


def beads_dir():
    return os.environ.get("PRORAB_BEADS_DIR") or os.path.expanduser("~/projects/prorab/.beads")


def _run(args, check=True):
    d = beads_dir()
    env = dict(os.environ, BEADS_DIR=d)
    cwd = os.path.dirname(d.rstrip(os.sep)) or "."
    try:
        p = subprocess.run(["bd"] + list(args), capture_output=True, text=True, env=env,
                           cwd=cwd if os.path.isdir(cwd) else None)
    except FileNotFoundError:
        raise JournalError("bd binary not on PATH")
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


# ---- наряды and stages ------------------------------------------------------

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
    """Create a stage under a наряд.

    Storage (bd 1.3.1): files / duration_min / started_at / actual_min live in the
    bead's `metadata` JSON object (bd update merges keys); kind is the label `type:<kind>`.
    """
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
    """Union of the files of the наряд's stages, in first-seen order."""
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
    """Record the приёмка act and the actual duration (minutes), close the stage."""
    started = stage_info(stage)["started_at"]
    if started is None:
        raise JournalError("stage %s was never started" % stage)
    minutes = (time.time() - float(started)) / 60.0
    add_act(stage, "acceptance", act)
    _set_meta(stage, actual_min=minutes)
    _run(["close", stage])
    return minutes


def durations(kind):
    """Actual durations (minutes) of closed stages of the kind, oldest first."""
    items = bd(["list", "--label", "type:%s" % kind, "--status", "closed", "--limit", "0"])
    items = sorted(items, key=lambda i: i.get("closed_at") or "")
    return [i["metadata"]["actual_min"] for i in items
            if (i.get("metadata") or {}).get("actual_min") is not None]


# ---- the ignore-limits override (customer's words only) --------------------------

OVERRIDE_KEY = "override.ignore_limits"


def set_override(goal):
    kv_set(OVERRIDE_KEY, json.dumps({"goal": goal, "since": int(time.time()),
                                     "source": "customer's words"}, ensure_ascii=False))


def clear_override():
    kv_clear(OVERRIDE_KEY)


def _open_gate(item):
    return any(d.get("issue_type") == "gate" and d.get("status") != "closed"
               for d in item.get("dependencies") or [])


def _work_order_settled(work_order_id):
    """An open наряд is settled when it, or all of its open work, waits for the customer."""
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
    """True when the goal has наряды and each is closed or waits for the customer."""
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
        clear_override()
        return None
    return data


def ready():
    out = []
    for it in bd(["ready"]):
        if WORK_ORDER_LABEL in (it.get("labels") or []) or it.get("parent"):
            out.append(it)
    return out


# ---- the scheduler's view: наряды as units of issue ---------------------------------

def _open_work_orders():
    return [n for n in bd(["list", "--label", WORK_ORDER_LABEL, "--no-parent", "--limit", "0"])
            if n.get("status") != "closed"]


def graph():
    """``{open наряд: [open наряды that block it]}`` (stage-level edges are not included)."""
    items = _open_work_orders()
    ids = {n["id"] for n in items}
    out = {}
    for n in items:
        out[n["id"]] = [d["id"] for d in _show(n["id"]).get("dependencies") or []
                        if d.get("dependency_type") != "parent-child" and d["id"] in ids]
    return out


def open_stages(work_order):
    """Ids of the наряд's stages that are not closed."""
    return [c["id"] for c in bd(["list", "--parent", work_order, "--all", "--limit", "0"])
            if c.get("status") != "closed"]


def working_work_orders():
    """Наряды a registered crew is working on."""
    out = []
    for name in crews():
        n = kv_get("crew.%s.work_order" % name)
        if n and n not in out:
            out.append(n)
    return out


def ready_work_orders():
    """Наряды with a ready stage (or ready themselves) and no crew working on them."""
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
        files = transcript.worktree_files(rec["worktree"], home)
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


def _rate_text(rate):
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
    lines.append("- window rate: " + _rate_text(rates["five_hour"]))
    lines.append("- week rate: " + _rate_text(rates["seven_day"]))
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


# ---- active-prorab lock -----------------------------------------------------

def acquire_active(pane_id):
    """Take the active-prorab lock if free, own, handed off to us, or held by a dead pane."""
    holder = kv_get("prorab:active")
    if holder is None or holder == pane_id:
        kv_set("prorab:active", pane_id)
        return True
    if kv_get("prorab:handoff") == pane_id:
        kv_set("prorab:active", pane_id)
        kv_clear("prorab:handoff")
        return True
    import herdr_cli  # lazy: only lock checks need herdr
    if holder not in {a.get("pane_id") for a in herdr_cli.agents()}:
        kv_set("prorab:active", pane_id)
        return True
    return False


# ---- waiting for the customer, restarts, crews ------------------------------

def wait_customer(bead, reason):
    gate = bd(["gate", "create", "--type", "human", "--blocks", bead, "--reason", reason])
    add_act(bead, "answer", reason)
    return gate["id"]


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


def register_crew(name):
    names = crews()
    if name not in names:
        _save_crews(names + [name])


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
    sub.add_parser("durations").add_argument("kind")
    p = sub.add_parser("override")
    osub = p.add_subparsers(dest="ovcmd", required=True)
    osub.add_parser("get"); osub.add_parser("clear")
    osub.add_parser("set").add_argument("goal")
    sub.add_parser("ready")
    sub.add_parser("graph")
    sub.add_parser("ready-work-orders")
    sub.add_parser("open-stages").add_argument("work_order")
    p = sub.add_parser("act")
    p.add_argument("bead"); p.add_argument("kind"); p.add_argument("text")
    p = sub.add_parser("acts")
    p.add_argument("bead"); p.add_argument("--kind")
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
    p.add_argument("--session", required=True); p.add_argument("--run-cwd")
    p = sub.add_parser("shift-spend")
    p.add_argument("--week-pp", type=float)
    p = sub.add_parser("lock")
    p.add_argument("pane_id")
    p = sub.add_parser("wait")
    p.add_argument("bead"); p.add_argument("reason")
    p = sub.add_parser("restarts")
    p.add_argument("work_order")
    p = sub.add_parser("crews")
    p.add_argument("--add"); p.add_argument("--remove")
    p = sub.add_parser("iterations")
    p.add_argument("stage"); p.add_argument("kind"); p.add_argument("--bump", action="store_true")
    sub.add_parser("notify").add_argument("key")
    p = sub.add_parser("passport")
    p.add_argument("site"); p.add_argument("--set-file")
    p = sub.add_parser("issues")
    p.add_argument("--state")
    p = sub.add_parser("kv")
    ksub = p.add_subparsers(dest="kvcmd", required=True)
    g = ksub.add_parser("get"); g.add_argument("key")
    s = ksub.add_parser("set"); s.add_argument("key"); s.add_argument("value")
    a = ap.parse_args(argv)

    if a.cmd == "ensure":
        ensure_db()
        return _emit({"ok": True})
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
    if a.cmd == "durations":
        return _emit(durations(a.kind))
    if a.cmd == "override":
        if a.ovcmd == "set":
            set_override(a.goal)
        elif a.ovcmd == "clear":
            clear_override()
        return _emit({"override": override()})
    if a.cmd == "ready":
        return _emit(ready())
    if a.cmd == "graph":
        return _emit(graph())
    if a.cmd == "ready-work-orders":
        return _emit(ready_work_orders())
    if a.cmd == "open-stages":
        return _emit(open_stages(a.work_order))
    if a.cmd == "act":
        add_act(a.bead, a.kind, a.text)
        return _emit({"ok": True})
    if a.cmd == "acts":
        return _emit(acts(a.bead, a.kind))
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
        return _emit({"gate": wait_customer(a.bead, a.reason)})
    if a.cmd == "restarts":
        return _emit({"restarts": bump_restarts(a.work_order)})
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
