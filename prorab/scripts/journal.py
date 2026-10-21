#!/usr/bin/env python3
"""The prorab's journal on beads: наряды, stages, acts, kv, budget, the active-prorab lock.

Every bd call runs with BEADS_DIR set (default ~/projects/prorab/.beads, override
with PRORAB_BEADS_DIR) and with the journal's parent directory as cwd.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402

ACT_KINDS = ("приёмка", "пересменка", "смена", "ответ")
NARYAD_LABEL = "prorab:naryad"
SPENT_KEYS = ("sessions", "runs", "tokens")


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

def create_naryad(obj, title, body, smeta, goal=None):
    labels = "%s,obj:%s" % (NARYAD_LABEL, obj)
    if goal:
        labels += ",goal:%s" % goal
    nid = bd(["create", title, "-d", body, "-l", labels,
              "--silent"], json_out=False).strip()
    if not nid:
        raise JournalError("bd create returned no id")
    for k, v in (smeta or {}).items():
        kv_set("smeta.%s.%s" % (nid, k), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    return nid


def create_stage(parent, title, body, blocked_by=(), files=(), estimate_min=None, kind=None):
    """Create a stage under a наряд.

    Storage (bd 1.3.1): files / estimate_min / started_at / actual_min live in the
    bead's `metadata` JSON object (bd update merges keys); kind is the label `type:<kind>`.
    """
    meta = {"files": list(files or [])}
    if estimate_min is not None:
        meta["estimate_min"] = float(estimate_min)
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
            "estimate_min": meta.get("estimate_min"), "kind": kind,
            "started_at": meta.get("started_at"), "actual_min": meta.get("actual_min")}


def stage_info(stage):
    return _info(_show(stage))


def naryad_files(naryad):
    """Union of the files of the наряд's stages, in first-seen order."""
    out = []
    for child in bd(["list", "--parent", naryad, "--all", "--limit", "0"]):
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
    add_act(stage, "приёмка", act)
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
                                     "source": "слова заказчика"}, ensure_ascii=False))


def clear_override():
    kv_clear(OVERRIDE_KEY)


def _open_gate(item):
    return any(d.get("issue_type") == "gate" and d.get("status") != "closed"
               for d in item.get("dependencies") or [])


def _naryad_settled(naryad_id):
    """An open наряд is settled when it, or all of its open work, waits for the customer."""
    if _open_gate(_show(naryad_id)):
        return True
    stages = [c for c in bd(["list", "--parent", naryad_id, "--all", "--limit", "0"])
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
    naryads = bd(["list", "--label", NARYAD_LABEL, "--label", "goal:%s" % goal,
                  "--no-parent", "--all", "--limit", "0"])
    if not naryads:
        return False
    return all(n.get("status") == "closed" or _naryad_settled(n["id"]) for n in naryads)


def override():
    """The stored override, or None; expires (and is cleared) once its goal is done."""
    v = kv_get(OVERRIDE_KEY)
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
        if NARYAD_LABEL in (it.get("labels") or []) or it.get("parent"):
            out.append(it)
    return out


# ---- acts -------------------------------------------------------------------

def add_act(bead, kind, text):
    if kind not in ACT_KINDS:
        raise ValueError("unknown act kind %r (expected one of %s)" % (kind, ", ".join(ACT_KINDS)))
    bd(["comments", "add", bead, "[акт:%s] %s" % (kind, text)])


def acts(bead, kind=None):
    """Acts of a bead, oldest first. With `kind`: bodies without the tag; without: full texts."""
    comments = bd(["comments", bead]) or []
    comments = sorted(comments, key=lambda c: c.get("created_at", ""))
    out = []
    for c in comments:
        t = c.get("text", "")
        if not t.startswith("[акт:"):
            continue
        if kind is None:
            out.append(t)
            continue
        tag = "[акт:%s]" % kind
        if t.startswith(tag):
            out.append(t[len(tag):].lstrip(" "))
    return out


# ---- budget -----------------------------------------------------------------

def budget():
    b = dict(_defaults.load()["budget"])
    kv = kv_all()
    for k in list(b):
        v = kv.get("budget.%s" % k)
        if v not in (None, ""):
            b[k] = int(v)
    return b


def set_budget(**kw):
    for k, v in kw.items():
        kv_set("budget.%s" % k, v)


def spend(naryad, sessions=0, runs=0, tokens=0):
    for name, n in (("sessions", sessions), ("runs", runs), ("tokens", tokens)):
        if not n:
            continue
        kv_set("spent.%s.%s" % (naryad, name), _kv_int("spent.%s.%s" % (naryad, name)) + n)
        kv_set("shift.spent.%s" % name, _kv_int("shift.spent.%s" % name) + n)


def shift_spent():
    kv = kv_all()
    return {k: int(kv.get("shift.spent.%s" % k) or 0) for k in SPENT_KEYS}


def over_budget(week_pp_now):
    b = budget()
    spent = shift_spent()
    over = [k for k in ("sessions", "runs") if spent[k] > b[k]]
    start = kv_get("shift.week_start")
    if week_pp_now is not None and start is not None:
        if float(week_pp_now) - float(start) > b["week_pp"]:
            over.append("week_pp")
    return over


def begin_shift(week_pct):
    for k in SPENT_KEYS:
        kv_set("shift.spent.%s" % k, 0)
    if week_pct is None:
        kv_clear("shift.week_start")
    else:
        kv_set("shift.week_start", week_pct)
    kv_set("shift.budget", json.dumps(budget()))


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


# ---- waiting for the customer, restarts, brigades ------------------------------

def wait_customer(bead, reason):
    gate = bd(["gate", "create", "--type", "human", "--blocks", bead, "--reason", reason])
    add_act(bead, "ответ", reason)
    return gate["id"]


def bump_restarts(naryad):
    n = _kv_int("restarts.%s" % naryad) + 1
    kv_set("restarts.%s" % naryad, n)
    return n


def brigades():
    v = kv_get("brigades.active")
    return [x for x in v.split(",") if x] if v else []


def _save_brigades(names):
    if names:
        kv_set("brigades.active", ",".join(names))
    else:
        kv_clear("brigades.active")


def register_brigade(name):
    names = brigades()
    if name not in names:
        _save_brigades(names + [name])


def unregister_brigade(name):
    _save_brigades([n for n in brigades() if n != name])


# ---- CLI --------------------------------------------------------------------

def _pairs(items):
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit("expected key=value, got %r" % it)
        out[k] = v
    return out


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure")
    p = sub.add_parser("naryad")
    p.add_argument("obj"); p.add_argument("title")
    p.add_argument("--body", default=""); p.add_argument("--smeta", action="append", default=[])
    p.add_argument("--goal")
    p = sub.add_parser("stage")
    p.add_argument("parent"); p.add_argument("title")
    p.add_argument("--body", default=""); p.add_argument("--blocked-by", action="append", default=[])
    p.add_argument("--file", action="append", default=[])
    p.add_argument("--estimate", type=float); p.add_argument("--kind")
    for name in ("stage-info", "stage-start", "naryad-files"):
        sub.add_parser(name).add_argument("bead")
    p = sub.add_parser("stage-accept")
    p.add_argument("stage"); p.add_argument("act")
    sub.add_parser("durations").add_argument("kind")
    p = sub.add_parser("override")
    osub = p.add_subparsers(dest="ovcmd", required=True)
    osub.add_parser("get"); osub.add_parser("clear")
    osub.add_parser("set").add_argument("goal")
    sub.add_parser("ready")
    p = sub.add_parser("act")
    p.add_argument("bead"); p.add_argument("kind"); p.add_argument("text")
    p = sub.add_parser("acts")
    p.add_argument("bead"); p.add_argument("--kind")
    p = sub.add_parser("budget")
    p.add_argument("--set", action="append", default=[])
    p = sub.add_parser("spend")
    p.add_argument("naryad")
    for k in SPENT_KEYS:
        p.add_argument("--" + k, type=int, default=0)
    sub.add_parser("spent")
    p = sub.add_parser("over")
    p.add_argument("--week-pp", type=float)
    p = sub.add_parser("shift-begin")
    p.add_argument("--week-pct", type=float)
    p = sub.add_parser("lock")
    p.add_argument("pane_id")
    p = sub.add_parser("wait")
    p.add_argument("bead"); p.add_argument("reason")
    p = sub.add_parser("restarts")
    p.add_argument("naryad")
    p = sub.add_parser("brigades")
    p.add_argument("--add"); p.add_argument("--remove")
    p = sub.add_parser("kv")
    ksub = p.add_subparsers(dest="kvcmd", required=True)
    g = ksub.add_parser("get"); g.add_argument("key")
    s = ksub.add_parser("set"); s.add_argument("key"); s.add_argument("value")
    a = ap.parse_args(argv)

    if a.cmd == "ensure":
        ensure_db()
        return _emit({"ok": True})
    ensure_db()
    if a.cmd == "naryad":
        return _emit({"id": create_naryad(a.obj, a.title, a.body, _pairs(a.smeta), goal=a.goal)})
    if a.cmd == "stage":
        return _emit({"id": create_stage(
            a.parent, a.title, a.body, a.blocked_by, a.file, a.estimate, a.kind)})
    if a.cmd == "stage-info":
        return _emit(stage_info(a.bead))
    if a.cmd == "naryad-files":
        return _emit(naryad_files(a.bead))
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
    if a.cmd == "act":
        add_act(a.bead, a.kind, a.text)
        return _emit({"ok": True})
    if a.cmd == "acts":
        return _emit(acts(a.bead, a.kind))
    if a.cmd == "budget":
        kw = {k: int(v) for k, v in _pairs(a.set).items()}
        if kw:
            set_budget(**kw)
        return _emit(budget())
    if a.cmd == "spend":
        spend(a.naryad, a.sessions, a.runs, a.tokens)
        return _emit(shift_spent())
    if a.cmd == "spent":
        return _emit(shift_spent())
    if a.cmd == "over":
        return _emit(over_budget(a.week_pp))
    if a.cmd == "shift-begin":
        begin_shift(a.week_pct)
        return _emit({"ok": True})
    if a.cmd == "lock":
        got = acquire_active(a.pane_id)
        _emit({"acquired": got})
        return 0 if got else 1
    if a.cmd == "wait":
        return _emit({"gate": wait_customer(a.bead, a.reason)})
    if a.cmd == "restarts":
        return _emit({"restarts": bump_restarts(a.naryad)})
    if a.cmd == "brigades":
        if a.add:
            register_brigade(a.add)
        if a.remove:
            unregister_brigade(a.remove)
        return _emit(brigades())
    if a.cmd == "kv":
        if a.kvcmd == "get":
            return _emit({"key": a.key, "value": kv_get(a.key)})
        kv_set(a.key, a.value)
        return _emit({"key": a.key, "value": a.value})
    return 2


if __name__ == "__main__":
    sys.exit(main())
