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

def create_naryad(obj, title, body, smeta):
    nid = bd(["create", title, "-d", body, "-l", "%s,obj:%s" % (NARYAD_LABEL, obj),
              "--silent"], json_out=False).strip()
    if not nid:
        raise JournalError("bd create returned no id")
    for k, v in (smeta or {}).items():
        kv_set("smeta.%s.%s" % (nid, k), v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    return nid


def create_stage(parent, title, body, blocked_by=()):
    args = ["create", title, "-d", body, "--parent", parent, "--silent"]
    if blocked_by:
        args += ["--deps", ",".join("blocked-by:%s" % b for b in blocked_by)]
    return bd(args, json_out=False).strip()


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
    p = sub.add_parser("stage")
    p.add_argument("parent"); p.add_argument("title")
    p.add_argument("--body", default=""); p.add_argument("--blocked-by", action="append", default=[])
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
        return _emit({"id": create_naryad(a.obj, a.title, a.body, _pairs(a.smeta))})
    if a.cmd == "stage":
        return _emit({"id": create_stage(a.parent, a.title, a.body, a.blocked_by)})
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
