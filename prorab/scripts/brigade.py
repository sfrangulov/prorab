#!/usr/bin/env python3
"""Brigade lifecycle: a Claude brigade in a herdr pane inside a per-наряд git worktree.

start, send, state, summary, handover, evidence, merge check, answer, pause, stop, cleanup.
The prorab never merges, never pushes and never answers a trust/consent prompt.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402
import herdr_cli  # noqa: E402
import journal  # noqa: E402
import transcript  # noqa: E402

REFERENCES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "references")
HANDOVER_MARKER = "HANDOVER READY"

# Screen fragments of consent prompts: the prorab never answers them.
CONSENT_MARKERS = (
    "trust this folder",
    "Do you trust the files",
    "MCP server",
    "Do you want to proceed",
    "Do you want to make this edit",
    "Do you want to create",
    "Do you want to allow",
    "don't ask again",
)
TRUST_MARKER = CONSENT_MARKERS[0]


def is_consent(visible):
    low = visible.lower()
    return any(m.lower() in low for m in CONSENT_MARKERS)


def _kv(name, field):
    return "brigade.%s.%s" % (name, field)


def _template(heading):
    """First fenced block under `## heading` in references/acts.md."""
    with open(os.path.join(REFERENCES, "acts.md"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"^## %s\s*\n+```[^\n]*\n(.*?)\n```" % re.escape(heading), text,
                  re.S | re.M)
    if not m:
        raise ValueError("no block under heading %r in acts.md" % heading)
    return m.group(1)


def _git(args, cwd=None, check=False):
    cmd = ["git"] + (["-C", cwd] if cwd else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p


def _worktrees(obj_path):
    """[{path, branch}] from `git worktree list --porcelain`."""
    out, cur = [], None
    for line in _git(["worktree", "list", "--porcelain"], obj_path).stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "branch": None}
            out.append(cur)
        elif line.startswith("branch ") and cur is not None:
            b = line[len("branch "):]
            cur["branch"] = b[len("refs/heads/"):] if b.startswith("refs/heads/") else b
    return out


def _find_worktree(obj_path, naryad):
    for w in _worktrees(obj_path):
        if os.path.basename(w["path"].rstrip("/")) == "prorab-%s" % naryad:
            return w
    return None


def _under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# ---- start ------------------------------------------------------------------

def _split_direction(caller_pane):
    try:
        layout = herdr_cli.run(["pane", "layout", "--pane", caller_pane])["result"]["layout"]
        for p in layout["panes"]:
            if p["pane_id"] == caller_pane:
                r = p["rect"]
                return "right" if r["width"] >= 2 * r["height"] else "down"
    except (herdr_cli.HerdrError, KeyError, TypeError):
        pass
    return "down"


def _same_object(name, obj_path):
    obj = journal.kv_get(_kv(name, "object"))
    if obj is None:
        a = herdr_cli.agent(name)
        obj = a.get("cwd") if a else None
    return bool(obj) and _under(obj, obj_path)


def _files_overlap(mine, theirs):
    return not mine or not theirs or bool(set(mine) & set(theirs))


def start(obj_path, naryad, name, caller_pane):
    active = journal.brigades()
    if len(active) >= journal.budget()["parallel_ceiling"]:
        return {"status": "parallel_limit"}
    mine = journal.naryad_files(naryad)
    clash = []
    for other in active:
        if other == name or not _same_object(other, obj_path):
            continue
        other_naryad = journal.kv_get(_kv(other, "naryad"))
        theirs = journal.naryad_files(other_naryad) if other_naryad else []
        if _files_overlap(mine, theirs):
            clash.append(other)
    if clash:
        return {"status": "files_overlap", "with": clash}

    direction = _split_direction(caller_pane)
    pane = herdr_cli.run(["pane", "split", caller_pane, "--direction", direction,
                          "--no-focus", "--cwd", obj_path])["result"]["pane"]["pane_id"]
    try:
        started = herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane,
                                 "--", "-w", "prorab-%s" % naryad], timeout_s=90)
    except herdr_cli.HerdrError as e:
        if e.code != "agent_not_ready":
            raise
        if TRUST_MARKER in herdr_cli.pane_visible(pane).lower():
            return {"status": "needs_trust", "pane_id": pane}
        return {"status": "not_ready", "pane_id": pane}

    a = (started.get("result") or {}).get("agent") if isinstance(started, dict) else None
    if not a or not a.get("agent_session"):
        a = herdr_cli.agent(name) or {}
    session_id = (a.get("agent_session") or {}).get("value")

    wt = _find_worktree(obj_path, naryad)
    worktree = wt["path"] if wt else None
    branch = wt["branch"] if wt else None
    want = "prorab/%s" % naryad
    if wt and branch != want:
        if _git(["branch", "-m", want], worktree).returncode == 0:
            branch = want

    journal.register_brigade(name)
    for field, value in (("naryad", naryad), ("object", obj_path), ("pane", pane),
                         ("worktree", worktree or ""), ("branch", branch or "")):
        journal.kv_set(_kv(name, field), value)
    result = {"status": "started", "pane_id": pane, "name": name,
              "session_id": session_id, "worktree": worktree, "branch": branch}
    journal.add_act(naryad, "ответ", "бригада %s запущена: панель %s, ветка %s, сессия %s"
                    % (name, pane, branch, session_id))
    return result


# ---- sending, state ---------------------------------------------------------

def send_naryad(name, text, retry_delay=5):
    r = herdr_cli.send(name, text)
    if r == "stalled":
        time.sleep(retry_delay)
        r = herdr_cli.send(name, text)
    return r


def _transcript_path(a):
    sid = (a.get("agent_session") or {}).get("value")
    if not sid or not a.get("cwd"):
        return None
    return transcript.find_transcript(a["cwd"], sid)


def state(name, a=None):
    a = a or herdr_cli.agent(name)
    if a is None:
        return {"status": "gone", "bg_shells": 0, "ctx": None, "session_id": None,
                "cwd": None, "pane_id": None}
    path = _transcript_path(a)
    return {"status": a.get("agent_status"),
            "bg_shells": herdr_cli.bg_shells(herdr_cli.agent_visible(name)),
            "ctx": transcript.context_status(path, _defaults.load()) if path else None,
            "session_id": (a.get("agent_session") or {}).get("value"),
            "cwd": a.get("cwd"), "pane_id": a.get("pane_id")}


def summary(name):
    a = herdr_cli.agent(name)
    path = _transcript_path(a) if a else None
    return transcript.last_text(path) if path else ""


def _tokens_of(a):
    path = _transcript_path(a) if a else None
    if not path:
        return 0
    t = transcript.usage_totals(path)
    return t["input"] + t["cache_creation"] + t["output"]


def _exit(name):
    """Send /exit; the agent vanishing is success."""
    try:
        herdr_cli.send(name, "/exit")
    except herdr_cli.HerdrError:
        pass


# ---- handover ---------------------------------------------------------------

def request_handover(name):
    return herdr_cli.send(name, _template("Запрос пересменки"))


def pause(name):
    return herdr_cli.send(name, _template("Пауза"))


def _wait(cond, timeout_s, poll_s):
    deadline = time.monotonic() + timeout_s
    while True:
        if cond():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def _pane(pane_id):
    return herdr_cli.run(["pane", "get", pane_id])["result"]["pane"]


def _same_dir(a, b):
    return bool(a) and bool(b) and (a == b or os.path.realpath(a) == os.path.realpath(b))


def complete_handover(name, naryad, naryad_text, timeout_s=60, poll_s=1):
    a = herdr_cli.agent(name)
    if a is None:
        return {"status": "gone"}
    st = state(name, a)
    if st["bg_shells"] > 0:
        return {"status": "bg_shells", "bg_shells": st["bg_shells"]}
    pane = st["pane_id"]
    worktree = journal.kv_get(_kv(name, "worktree")) or st["cwd"]
    path = _transcript_path(a)
    last = transcript.last_text(path) if path else ""
    act = "\n".join(ln for ln in last.splitlines() if ln.strip() != HANDOVER_MARKER).strip()
    tokens = _tokens_of(a)
    journal.add_act(naryad, "пересменка", act)

    _exit(name)
    gone = _wait(lambda: herdr_cli.agent(name) is None
                 and not _pane(pane).get("agent"), timeout_s, poll_s)
    if not gone:
        return {"status": "restart_failed", "reason": "agent did not exit"}
    herdr_cli.run(["pane", "run", pane, "cd '%s'" % worktree])
    if not _wait(lambda: _same_dir(_pane(pane).get("foreground_cwd"), worktree),
                 timeout_s, poll_s):
        return {"status": "restart_failed", "reason": "shell did not enter the worktree"}
    herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane], timeout_s=90)

    text = "\n\n".join([naryad_text] + journal.acts(naryad, "пересменка"))
    sent = send_naryad(name, text)
    journal.spend(naryad, sessions=1, tokens=tokens)
    return {"status": "restarted", "sent": sent, "tokens": tokens, "pane_id": pane}


# ---- git facts --------------------------------------------------------------

def evidence(worktree, base="main"):
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree).stdout.strip()
    commits = _git(["log", "--oneline", "%s..HEAD" % base], worktree).stdout.splitlines()
    diffstat = _git(["diff", "--stat", "%s...HEAD" % base], worktree).stdout.strip()
    dirty = bool(_git(["status", "--porcelain"], worktree).stdout.strip())
    return {"branch": branch, "commits": commits, "diffstat": diffstat, "dirty": dirty}


def merge_check(worktree, accepted):
    """Read-only: accepted branches not yet in HEAD and the ones that would conflict."""
    missing, conflicts, errors = [], [], []
    for b in accepted:
        if _git(["merge-base", "--is-ancestor", b, "HEAD"], worktree).returncode == 0:
            continue
        missing.append(b)
        rc = _git(["merge-tree", "--write-tree", "HEAD", b], worktree).returncode
        if rc == 1:
            conflicts.append(b)
        elif rc != 0:
            errors.append(b)
    return {"missing": missing, "conflicts": conflicts, "errors": errors}


# ---- answering a dialog -----------------------------------------------------

def _record(naryad, text):
    if naryad:
        journal.add_act(naryad, "ответ", text)


def _status(name):
    a = herdr_cli.agent(name)
    return a.get("agent_status") if a else "gone"


def answer(name, choice=None, text=None):
    naryad = journal.kv_get(_kv(name, "naryad"))
    visible = herdr_cli.agent_visible(name)
    if is_consent(visible):
        return "consent"
    dialog = herdr_cli.choice_state(visible)
    if dialog is None:
        if text is None:
            raise ValueError("no dialog is open: text is required")
        herdr_cli.send(name, text)
        _record(naryad, "ответ бригаде %s: %s" % (name, text))
        return _status(name)
    labels, current = dialog
    if choice is not None:
        if not 1 <= choice <= len(labels):
            raise ValueError("choice %d out of 1..%d" % (choice, len(labels)))
        target = choice - 1
    elif text is not None:
        typed = [i for i, lb in enumerate(labels) if lb.lower().startswith("type something")]
        if not typed:
            raise ValueError("the dialog has no free-text option")
        target = typed[0]
    else:
        raise ValueError("a dialog is open: choice or text is required")
    delta = target - current
    keys = ["down"] * delta if delta > 0 else ["up"] * -delta
    if choice is not None:
        herdr_cli.send_keys(name, keys + ["enter"])
        _record(naryad, "выбор бригаде %s: %d. %s" % (name, choice, labels[target]))
    else:
        if keys:
            herdr_cli.send_keys(name, keys)
        pane = (herdr_cli.agent(name) or {}).get("pane_id") or journal.kv_get(_kv(name, "pane"))
        herdr_cli.run(["pane", "send-text", pane, text])
        herdr_cli.send_keys(name, ["enter"])
        _record(naryad, "ответ бригаде %s (свой текст): %s" % (name, text))
    return _status(name)


# ---- stop, cleanup ----------------------------------------------------------

def stop(name, naryad):
    a = herdr_cli.agent(name)
    tokens = _tokens_of(a)
    if a is not None:
        _exit(name)
    journal.unregister_brigade(name)
    journal.spend(naryad, tokens=tokens)
    for field in ("naryad", "object", "pane", "worktree", "branch"):
        journal.kv_clear(_kv(name, field))


def _default_branch(obj_path):
    for b in ("main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % b], obj_path).returncode == 0:
            return b
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], obj_path).stdout.strip()


def cleanup(obj_path, naryad):
    wt = _find_worktree(obj_path, naryad)
    if wt is None:
        return {"status": "absent"}
    branch = wt["branch"]
    dropped = any("drop" in a.lower() for a in journal.acts(naryad, "ответ"))
    merged = False
    if branch:
        out = _git(["branch", "--merged", _default_branch(obj_path)], obj_path).stdout
        merged = branch in [ln.strip().lstrip("*+ ").strip() for ln in out.splitlines()]
    if not (merged or dropped):
        return {"status": "kept", "reason": "unmerged"}
    rm = ["worktree", "remove"] + (["--force"] if dropped and not merged else []) + [wt["path"]]
    p = _git(rm, obj_path)
    if p.returncode != 0:
        return {"status": "kept", "reason": "dirty", "detail": p.stderr.strip()}
    if branch:
        _git(["branch", "-D" if dropped and not merged else "-d", branch], obj_path)
    return {"status": "removed", "worktree": wt["path"], "branch": branch}


# ---- CLI --------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(n, *args, **opts):
        p = sub.add_parser(n)
        for a in args:
            p.add_argument(a)
        for o, kw in opts.items():
            p.add_argument("--" + o.replace("_", "-"), **kw)
        return p

    cmd("start", "obj_path", "naryad", "name", "caller_pane")
    cmd("send_naryad", "name", "text")
    cmd("state", "name")
    cmd("summary", "name")
    cmd("request_handover", "name")
    cmd("complete_handover", "name", "naryad", "naryad_text")
    cmd("evidence", "worktree", base={"default": "main"})
    cmd("merge_check", "worktree", accepted={"default": ""})
    cmd("answer", "name", choice={"type": int}, text={})
    cmd("pause", "name")
    cmd("stop", "name", "naryad")
    cmd("cleanup", "obj_path", "naryad")
    args = vars(ap.parse_args(argv))
    c = args.pop("cmd")
    if c == "merge_check":
        args["accepted"] = [x for x in args["accepted"].split(",") if x]
    try:
        result = globals()[c](**args)
    except (herdr_cli.HerdrError, journal.JournalError, ValueError, RuntimeError) as e:
        print(json.dumps({"error": str(e), "code": getattr(e, "code", None)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
