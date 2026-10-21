#!/usr/bin/env python3
"""Crew lifecycle: a Claude crew in a herdr pane inside a per-work-order git worktree.

start, deliver, state, summary, handover, restart, limit wait, report check, evidence,
merge check, answer, pause, stop, cleanup.
The foreman never merges, never pushes and never answers a trust/consent prompt.
"""
import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402
import herdr_cli  # noqa: E402
import journal  # noqa: E402
import limits  # noqa: E402
import transcript  # noqa: E402

REFERENCES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "references")
HANDOVER_MARKER = "HANDOVER READY"

# The only drop decision: an act `ответ` starting with this line (see acts.md).
DROP_MARKER = "[customer decision] drop"

REPORT_SECTIONS = ("Summary", "Behaviour", "Changed files", "Verification", "Residual risks")
CRITERION_STATUSES = ("done", "not applicable", "deferred", "blocked")
OUTCOMES = ("delivered", "no changes", "blocked", "abandoned")
RESTART_REASONS = ("gone", "hung", "limit_died")


def _consent_result(visible, pane):
    """The start result for a consent screen (`needs_trust`, or `needs_consent` with the
    kind), else None. Never sends keys."""
    kind = herdr_cli.consent_dialog(visible)
    if kind is None:
        return None
    if kind == "trust":
        return {"status": "needs_trust", "pane_id": pane}
    return {"status": "needs_consent", "kind": kind, "pane_id": pane}


def _kv(name, field):
    return "crew.%s.%s" % (name, field)


def _template(heading):
    """First fenced block under `## heading` in references/acts.md."""
    with open(os.path.join(REFERENCES, "acts.md"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"^## %s\s*\n(?:(?!^## ).*?)```[^\n]*\n(.*?)\n```" % re.escape(heading), text,
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


def _worktrees(site_path):
    """[{path, branch}] from `git worktree list --porcelain`."""
    out, cur = [], None
    for line in _git(["worktree", "list", "--porcelain"], site_path).stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "branch": None}
            out.append(cur)
        elif line.startswith("branch ") and cur is not None:
            b = line[len("branch "):]
            cur["branch"] = b[len("refs/heads/"):] if b.startswith("refs/heads/") else b
    return out


def _find_worktree(site_path, work_order):
    for w in _worktrees(site_path):
        if os.path.basename(w["path"].rstrip("/")) == "prorab-%s" % work_order:
            return w
    return None


def _under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _kv_of(kv, key):
    """kv_get, or the same lookup in a kv_all() snapshot (empty is None)."""
    if kv is None:
        return journal.kv_get(key)
    v = kv.get(key)
    return None if v in (None, "") else v


def _resolve_base(worktree, base):
    """`base` when it names a commit; else the site's default branch (main, master, or the
    branch checked out in the main worktree)."""
    if base and _git(["rev-parse", "--verify", "--quiet", "%s^{commit}" % base],
                     worktree).returncode == 0:
        return base
    for b in ("main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % b],
                worktree).returncode == 0:
            return b
    wts = _worktrees(worktree)
    return wts[0]["branch"] if wts and wts[0]["branch"] else base


# ---- names, files, delivery ---------------------------------------------------

def name_for(site, n):
    """A herdr-valid agent name `<base>-<n>` of at most 32 characters from a site path."""
    base = re.sub(r"[^a-z0-9_-]", "-", os.path.basename(site.rstrip("/")).lower())
    base = re.sub(r"-{2,}", "-", base)
    if not re.match(r"[a-z]", base):
        base = re.sub(r"-{2,}", "-", "c-" + base)
    base = base.rstrip("-")[:max(1, 32 - 1 - len(str(n)))].rstrip("-") or "c"
    return "%s-%d" % (base, n)


def _files_dir():
    path = _defaults.load()["send"]["files_dir"]
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _safe_id(value, what):
    if not value or value != os.path.basename(value) or value.startswith(".") or "\x00" in value:
        raise ValueError("%s %r cannot be used in a file name" % (what, value))
    return value


def message_file(work_order, text, name=None):
    """Write a message into the work-order files directory (0700, file 0600) and return the
    path. `name` -> `<name>.md`, replaced in place; else `<work_order>-<k>.md` with the next
    free k, or `msg-<unix time>.md` without a work order."""
    d = _files_dir()
    if name is not None:
        fname = _safe_id(name, "name") + ".md"
    elif work_order:
        wo, k = _safe_id(work_order, "work order"), 1
        while os.path.exists(os.path.join(d, "%s-%d.md" % (wo, k))):
            k += 1
        fname = "%s-%d.md" % (wo, k)
    else:
        t = int(time.time())
        while os.path.exists(os.path.join(d, "msg-%d.md" % t)):
            t += 1
        fname = "msg-%d.md" % t
    path = os.path.join(d, fname)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def deliver(name, text, work_order=None):
    """The only way a message reaches a crew. One line goes by `send`, several lines by a
    file named in one line. "stalled" is recovered without a second `prompt` (Enter only
    when the composer holds our line). Returns "accepted" | "blocked" | "stalled" |
    "unconfirmed" | "consent:<kind>"."""
    work_order = work_order or journal.kv_get(_kv(name, "work_order"))
    text = text.strip("\n")
    if "\n" in text:
        text = _template("Message by file").format(path=message_file(work_order, text))
    result = herdr_cli.send(name, text)
    if result == "stalled":
        result = herdr_cli.recover_stalled(name, text)
    if result == "blocked":
        kind = herdr_cli.consent_dialog(herdr_cli.agent_visible(name))
        return "consent:%s" % kind if kind else "blocked"
    return result


def _claude_args(*pairs):
    return ["--"] + [x for pair in pairs for x in pair]


def _set_session(name, worktree, session_id):
    journal.kv_set(_kv(name, "session"), session_id)
    if worktree:
        journal.kv_set(_kv(name, "transcript"), transcript.transcript_path(worktree, session_id))


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


def _same_site(name, site_path):
    site = journal.kv_get(_kv(name, "site"))
    if site is None:
        a = herdr_cli.agent(name)
        site = a.get("cwd") if a else None
    return bool(site) and _under(site, site_path)


def _files_overlap(mine, theirs):
    return not mine or not theirs or bool(set(mine) & set(theirs))


def start(site_path, work_order, name, caller_pane):
    if not herdr_cli.valid_name(name):
        return {"status": "bad_name"}
    rec = journal.issue(work_order)
    if rec and rec.get("state") in ("issuing", "issued") and rec.get("pane") \
            and herdr_cli.agent_by_pane(rec["pane"]) is not None:
        return {"status": "already_issued", "name": rec.get("name"), "pane_id": rec["pane"]}
    active = journal.crews()
    if len(active) >= journal.budget()["parallel_ceiling"]:
        return {"status": "parallel_limit"}
    mine = journal.work_order_files(work_order)
    clash = []
    for other in active:
        if other == name or not _same_site(other, site_path):
            continue
        other_work_order = journal.kv_get(_kv(other, "work_order"))
        theirs = journal.work_order_files(other_work_order) if other_work_order else []
        if _files_overlap(mine, theirs):
            clash.append(other)
    if clash:
        return {"status": "files_overlap", "with": clash}

    direction = _split_direction(caller_pane)
    pane = herdr_cli.run(["pane", "split", caller_pane, "--direction", direction,
                          "--no-focus", "--cwd", site_path])["result"]["pane"]["pane_id"]
    session_id = str(uuid.uuid4())
    journal.issue_set(work_order, "issuing", name=name, pane=pane, session_id=session_id)
    try:
        herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane]
                      + _claude_args(("--session-id", session_id), ("--add-dir", _files_dir()),
                                     ("-w", "prorab-%s" % work_order)), timeout_s=90)
    except herdr_cli.HerdrError as e:
        if e.code != "agent_not_ready":
            raise
        return (_consent_result(herdr_cli.pane_visible(pane), pane)
                or {"status": "not_ready", "pane_id": pane})

    wt = _find_worktree(site_path, work_order)
    worktree = wt["path"] if wt else None
    branch = wt["branch"] if wt else None
    want = "prorab/%s" % work_order
    if wt and branch != want:
        if _git(["branch", "-m", want], worktree).returncode == 0:
            branch = want

    journal.register_crew(name)
    fields = [("work_order", work_order), ("site", site_path), ("pane", pane)]
    if worktree:
        fields += [("worktree", worktree), ("branch", branch or "")]
    for field, value in fields:
        journal.kv_set(_kv(name, field), value)
    _set_session(name, worktree, session_id)
    result = {"status": "started" if worktree else "started_no_worktree", "pane_id": pane, "name": name,
              "session_id": session_id, "worktree": worktree, "branch": branch}
    journal.add_act(work_order, "answer", "crew %s started: pane %s, branch %s, session %s"
                    % (name, pane, branch, session_id))
    return result


# ---- sending, state ---------------------------------------------------------

def send_work_order(name, text, work_order=None):
    """Put the work order (with the site passport the caller appended) into its file and send
    the crew the one line naming it. A delivered order marks the issue record `issued`."""
    work_order = work_order or journal.kv_get(_kv(name, "work_order"))
    path = message_file(work_order, text, name=work_order)
    result = deliver(name, _template("Message by file").format(path=path), work_order)
    if result == "accepted" and work_order:
        journal.issue_set(work_order, "issued", name=name)
    return result


def _transcript_path(a):
    sid = (a.get("agent_session") or {}).get("value")
    if not sid or not a.get("cwd"):
        return None
    return transcript.find_transcript(a["cwd"], sid)


def _transcript_for(name, a=None, kv=None):
    """The crew's transcript: kv `crew.<name>.transcript` first, else found by the agent."""
    path = _kv_of(kv, _kv(name, "transcript"))
    if path and os.path.isfile(path):
        return path
    return _transcript_path(a) if a else None


def gone_state():
    """The state of a crew whose agent is not listed."""
    return {"status": "gone", "name_ok": False, "bg_shells": 0, "consent": None,
            "limit_screen": None, "provider_error": None, "ctx": None,
            "session_id": None, "cwd": None, "pane_id": None}


def state(name, a=None, *, read_screen=True, kv=None):
    """The crew's agent (found by its pane, the name only as a fallback) with its status,
    context and, with `read_screen`, the markers of one screen read; without it those are
    None (unknown, not 0). `read_screen` may also be the screen text already read."""
    if a is None:
        pane = _kv_of(kv, _kv(name, "pane"))
        listed = herdr_cli.agents()
        a = herdr_cli.agent_by_pane(pane, listed) if pane else None
        if a is None:
            a = next((x for x in listed if x.get("name") == name), None)
    if a is None:
        return gone_state()
    path = _transcript_for(name, a, kv)
    out = {"status": a.get("agent_status"), "name_ok": a.get("name") == name,
           "bg_shells": None, "consent": None, "limit_screen": None, "provider_error": None,
           "ctx": transcript.context_status(path, _defaults.load()) if path else None,
           "session_id": (a.get("agent_session") or {}).get("value"),
           "cwd": a.get("cwd"), "pane_id": a.get("pane_id")}
    if read_screen:
        if isinstance(read_screen, str):
            screen = read_screen
        else:
            screen = (herdr_cli.agent_visible(a["name"]) if a.get("name")
                      else herdr_cli.pane_visible(a["pane_id"]))
        d = _defaults.load()
        out.update(bg_shells=herdr_cli.bg_shells(screen),
                   consent=herdr_cli.consent_dialog(screen),
                   limit_screen=limits.limit_screen(screen, d),
                   provider_error=limits.provider_error(screen, d))
    return out


def summary(name):
    a = herdr_cli.agent(name)
    path = _transcript_for(name, a) if a else None
    return transcript.last_text(path) if path else ""


def _tokens_of(a, name=None):
    path = _transcript_for(name, a) if a and name else (_transcript_path(a) if a else None)
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


# ---- handover, restart ------------------------------------------------------

def request_handover(name):
    return deliver(name, _template("Handover request"))


def pause(name):
    return deliver(name, _template("Pause"))


def resume(name):
    """After a limit reset or a `--resume`: tell the crew to carry on."""
    return deliver(name, _template("Continue"))


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


def _enter_worktree(pane, worktree, timeout_s, poll_s):
    herdr_cli.run(["pane", "run", pane, "cd %s" % shlex.quote(worktree)])
    return _wait(lambda: _same_dir(_pane(pane).get("foreground_cwd"), worktree),
                 timeout_s, poll_s)


def _start_session(name, pane, claude_args, want_screen=False):
    """`agent start` in `pane`. Returns (error result or None, screen text). A consent
    screen maps as in `start`; the screen is read only when it is needed."""
    try:
        herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane]
                      + _claude_args(*claude_args), timeout_s=90)
    except herdr_cli.HerdrError as e:
        failed = {"status": "restart_failed", "reason": str(e), "pane_id": pane}
        if e.code != "agent_not_ready":
            return failed, ""
        screen = herdr_cli.pane_visible(pane)
        return (_consent_result(screen, pane) or failed), screen
    return None, (herdr_cli.pane_visible(pane) if want_screen else "")


def _handover_text(work_order, work_order_text):
    return "\n\n".join([work_order_text] + journal.acts(work_order, "handover"))


def _open_stage_statuses(work_order):
    try:
        lines = []
        for stage in journal.open_stages(work_order):
            status = (journal.bd(["show", stage]) or [{}])[0].get("status") or "unknown"
            lines.append("- %s: %s" % (stage, status))
        return "\n".join(lines) or "(no open stages)"
    except Exception as e:  # read only; a missing fact must not stop the handover
        return "(unavailable: %s)" % e


def _facts(worktree, work_order, base):
    base = _resolve_base(worktree, base)

    def out(args, empty):
        p = _git(args, worktree)
        if p.returncode != 0:
            return "(git failed: %s)" % (p.stderr.strip() or "exit %d" % p.returncode)
        return p.stdout.strip() or empty
    return "\n".join([
        "## Facts at handover",
        "git status --short:", out(["status", "--short"], "(clean)"), "",
        "git log --oneline %s..HEAD:" % base,
        out(["log", "--oneline", "%s..HEAD" % base], "(no commits)"), "",
        "Open stages (bd show):", _open_stage_statuses(work_order)])


def complete_handover(name, work_order, work_order_text, timeout_s=60, poll_s=1, base="main"):
    a = herdr_cli.agent(name)
    if a is None:
        return {"status": "gone"}
    st = state(name, a)
    if st["bg_shells"] > 0:
        return {"status": "bg_shells", "bg_shells": st["bg_shells"]}
    pane = st["pane_id"]
    path = _transcript_for(name, a)
    last = transcript.last_text(path) if path else ""
    lines = last.splitlines()
    if not any(ln.strip() == HANDOVER_MARKER for ln in lines):
        return {"status": "act_missing"}
    worktree = journal.kv_get(_kv(name, "worktree"))
    if not worktree:
        return {"status": "restart_failed", "reason": "worktree of the crew is unknown"}
    previous = journal.kv_get("handover.%s.last" % work_order)
    try:
        looping = previous is not None and \
            time.time() - float(previous) < _defaults.load()["handover"]["min_interval_s"]
    except ValueError:
        looping = False
    if looping:
        journal.wait_customer(work_order, "handover loop")
        return {"status": "handover_loop"}
    act = "\n".join(ln for ln in lines if ln.strip() != HANDOVER_MARKER).strip()
    act = "%s\n\n%s" % (act, _facts(worktree, work_order, base))
    tokens = _tokens_of(a, name)
    try:
        journal.add_act(work_order, "handover", act)
    except Exception as e:  # without a saved act the crew must not be exited
        return {"status": "act_not_saved", "reason": str(e)}
    journal.kv_set("handover.%s.last" % work_order, time.time())
    journal.spend(work_order, sessions=1, tokens=tokens)

    _exit(name)
    gone = _wait(lambda: herdr_cli.agent(name) is None
                 and not _pane(pane).get("agent"), timeout_s, poll_s)
    if not gone:
        return {"status": "restart_failed", "reason": "agent did not exit"}
    if not _enter_worktree(pane, worktree, timeout_s, poll_s):
        return {"status": "restart_failed", "reason": "shell did not enter the worktree"}
    session_id = str(uuid.uuid4())
    failed, _screen = _start_session(
        name, pane, (("--session-id", session_id), ("--add-dir", _files_dir())))
    if failed:
        return failed
    _set_session(name, worktree, session_id)
    try:
        sent = deliver(name, _handover_text(work_order, work_order_text), work_order)
    except herdr_cli.HerdrError as e:
        return {"status": "prompt_not_accepted", "reason": str(e), "pane_id": pane}
    if sent != "accepted":
        return {"status": "prompt_not_accepted", "sent": sent, "pane_id": pane}
    return {"status": "restarted", "sent": sent, "tokens": tokens, "pane_id": pane,
            "session_id": session_id}


def restart(name, work_order, work_order_text, reason, caller_pane=None,
            timeout_s=60, poll_s=1):
    """Bring a crew back in its worktree: `--resume` the old session while its context is
    below soft, else a new session with the work order and every handover act. `limit_died`
    (the session ended while waiting for a limit reset) is not counted as a restart."""
    if reason not in RESTART_REASONS:
        raise ValueError("unknown restart reason %r (expected one of %s)"
                         % (reason, ", ".join(RESTART_REASONS)))
    counted = reason != "limit_died"
    if counted:
        n = int(journal.kv_get("restarts.%s" % work_order) or 0) + 1
        if n > _defaults.load()["monitor"]["max_restarts"]:
            journal.wait_customer(work_order, "crew %s was restarted %d times; the customer "
                                  "decides how to go on" % (name, n))
            return {"status": "waits_customer"}
    worktree = journal.kv_get(_kv(name, "worktree"))
    if not worktree:
        return {"status": "restart_failed", "reason": "worktree of the crew is unknown"}
    try:
        return _restart(name, work_order, work_order_text, worktree, caller_pane,
                        timeout_s, poll_s, counted)
    except herdr_cli.HerdrError as e:  # before the agent was started again: nothing changed
        return {"status": "restart_failed", "reason": str(e)}


def _restart(name, work_order, work_order_text, worktree, caller_pane, timeout_s, poll_s,
             counted):
    """The restart point is a successful `agent start`: from then on the session kv names the
    session and the restart is counted, whatever happens to the delivery."""
    pane = journal.kv_get(_kv(name, "pane"))
    alive = False
    if pane:
        try:
            _pane(pane)
            alive = True
        except herdr_cli.HerdrError:
            pass
    if alive:
        a = herdr_cli.agent_by_pane(pane)
        if a is not None:
            _exit(a.get("name") or name)
            if not _wait(lambda: herdr_cli.agent_by_pane(pane) is None, timeout_s, poll_s):
                return {"status": "restart_failed", "reason": "agent did not exit",
                        "pane_id": pane}
    else:
        if not caller_pane:
            return {"status": "restart_failed", "reason": "pane is gone and no caller pane"}
        try:
            pane = herdr_cli.run(["pane", "split", caller_pane, "--direction",
                                  _split_direction(caller_pane), "--no-focus", "--cwd",
                                  worktree])["result"]["pane"]["pane_id"]
        except herdr_cli.HerdrError as e:
            return {"status": "restart_failed", "reason": str(e)}
        journal.kv_set(_kv(name, "pane"), pane)
        rec = journal.issue(work_order)
        if rec:
            journal.issue_set(work_order, rec["state"], pane=pane)
    if not _enter_worktree(pane, worktree, timeout_s, poll_s):
        return {"status": "restart_failed", "reason": "shell did not enter the worktree",
                "pane_id": pane}

    old = journal.kv_get(_kv(name, "session"))
    path = _transcript_for(name)
    ctx = transcript.context_status(path, _defaults.load()) if path else None
    files = ("--add-dir", _files_dir())
    if old and not (ctx and ctx["soft"]):
        failed, screen = _start_session(name, pane, (("--resume", old), files), True)
        marker = _defaults.load()["screen"]["resume_failed"].lower()
        shown = herdr_cli.bottom_block(screen, _defaults.load()["screen"]["tail_lines"]).lower()
        if marker not in shown:
            if failed:
                return failed
            if counted:
                journal.bump_restarts(work_order)
            try:
                sent = resume(name)
            except herdr_cli.HerdrError as e:
                return {"status": "prompt_not_accepted", "reason": str(e), "pane_id": pane}
            if sent != "accepted":
                return {"status": "prompt_not_accepted", "sent": sent, "pane_id": pane}
            return {"status": "resumed", "pane_id": pane, "session_id": old}
    session_id = str(uuid.uuid4())
    failed, _screen = _start_session(name, pane, (("--session-id", session_id), files))
    if failed:
        return failed
    _set_session(name, worktree, session_id)
    if counted:
        journal.bump_restarts(work_order)
    try:
        sent = deliver(name, _handover_text(work_order, work_order_text), work_order)
    except herdr_cli.HerdrError as e:
        return {"status": "prompt_not_accepted", "reason": str(e), "pane_id": pane,
                "session_id": session_id}
    if sent != "accepted":
        return {"status": "prompt_not_accepted", "sent": sent, "pane_id": pane,
                "session_id": session_id}
    return {"status": "restarted", "pane_id": pane, "session_id": session_id}


# ---- git facts --------------------------------------------------------------

def evidence(worktree, base="main"):
    base = _resolve_base(worktree, base)
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

def _record(work_order, text):
    if work_order:
        journal.add_act(work_order, "answer", text)


def _status(name):
    a = herdr_cli.agent(name)
    return a.get("agent_status") if a else "gone"


def answer(name, choice=None, text=None):
    """Answer a choice dialog, or send text. A consent prompt returns "consent:<kind>" and a
    limit screen "limit_screen": nothing is sent (`wait_limit` is the only way to act on a
    limit screen). Without a dialog the text goes through `deliver`."""
    work_order = journal.kv_get(_kv(name, "work_order"))
    visible = herdr_cli.agent_visible(name)
    kind = herdr_cli.consent_dialog(visible)
    if kind is not None:
        return "consent:%s" % kind
    if limits.limit_screen(visible, _defaults.load()) is not None:
        return "limit_screen"
    dialog = herdr_cli.choice_state(visible)
    if dialog is None:
        if text is None:
            raise ValueError("no dialog is open: text is required")
        result = deliver(name, text, work_order)
        _record(work_order, "answer to crew %s: %s" % (name, text))
        return result
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
        _record(work_order, "choice for crew %s: %d. %s" % (name, choice, labels[target]))
    else:
        if keys:
            herdr_cli.send_keys(name, keys)
        pane = (herdr_cli.agent(name) or {}).get("pane_id") or journal.kv_get(_kv(name, "pane"))
        herdr_cli.run(["pane", "send-text", pane, text])
        herdr_cli.send_keys(name, ["enter"])
        _record(work_order, "answer to crew %s (own text): %s" % (name, text))
    return _status(name)


def wait_limit(name):
    """On a limit screen: choose "Stop and wait" (never "Upgrade") and note the reset time.
    Never counts as a restart."""
    d = _defaults.load()
    visible = herdr_cli.agent_visible(name)
    found = limits.limit_screen(visible, d)
    if found is None:
        return {"status": "no_limit_screen"}
    if found["kind"] == "limit_choice":
        delta = found["wait_option"] - found["highlighted"]
        keys = ["down"] * delta if delta > 0 else ["up"] * -delta
        herdr_cli.send_keys(name, keys + ["enter"])
    elif herdr_cli.choice_state(herdr_cli.bottom_block(visible, d["screen"]["tail_lines"])):
        return {"status": "unknown_limit_dialog"}
    resets_at = found["resets_at"]
    if resets_at is None:
        journal.kv_clear(_kv(name, "limit_until"))
        until = "the reset (time not shown)"
    else:
        journal.kv_set(_kv(name, "limit_until"), resets_at)
        until = time.strftime("%H:%M", time.localtime(resets_at))
    _record(journal.kv_get(_kv(name, "work_order")),
            "crew waits for the limit reset until %s" % until)
    return {"status": "waiting", "resets_at": resets_at}


# ---- stop, cleanup ----------------------------------------------------------

def stop(name, work_order):
    a = herdr_cli.agent(name)
    tokens = _tokens_of(a, name)
    if a is not None:
        _exit(name)
    journal.unregister_crew(name)
    journal.spend(work_order, tokens=tokens)
    for field in ("work_order", "site", "pane", "worktree", "branch", "session",
                  "transcript", "limit_until"):
        journal.kv_clear(_kv(name, field))


def _default_branch(site_path):
    for b in ("main", "master"):
        if _git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % b], site_path).returncode == 0:
            return b
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], site_path).stdout.strip()


def cleanup(site_path, work_order):
    wt = _find_worktree(site_path, work_order)
    if wt is None:
        return {"status": "absent"}
    branch = wt["branch"]
    dropped = any(a.startswith(DROP_MARKER) for a in journal.acts(work_order, "answer"))
    merged = False
    if branch:
        out = _git(["branch", "--merged", _default_branch(site_path)], site_path).stdout
        merged = branch in [ln.strip().lstrip("*+ ").strip() for ln in out.splitlines()]
    if not (merged or dropped):
        return {"status": "kept", "reason": "unmerged"}
    rm = ["worktree", "remove"] + (["--force"] if dropped and not merged else []) + [wt["path"]]
    p = _git(rm, site_path)
    if p.returncode != 0:
        return {"status": "kept", "reason": "dirty", "detail": p.stderr.strip()}
    if branch:
        _git(["branch", "-D" if dropped and not merged else "-d", branch], site_path)
    return {"status": "removed", "worktree": wt["path"], "branch": branch}


# ---- stage report check -------------------------------------------------------

_CRITERION_ID = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(?:\|[ \t]*)?\**(C\d+)\b", re.M)
_HEADING = re.compile(r"^ {0,3}#{1,4}\s+(.+?)\s*#*\s*$")
_ROW = re.compile(r"^\s*\|\s*(C\d+)\s*\|\s*([^|]*?)\s*(?:\|\s*([^|]*?)\s*)?\|?\s*$")
_RESULT = re.compile(r"^[\s>*_-]*Result[\s*_]*:[\s*_]*(.+?)[\s*_`.]*$", re.I | re.M)
_COMMIT = re.compile(r"^[\s>*_-]*Commit[\s*_]*:[\s*_`]*([0-9a-f]{7,40})\b", re.I | re.M)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)(?:\s+(.*))?$")
_TICKS = re.compile(r"`([^`]+)`")
_SKIP = re.compile(r"\bSKIP:\s*(\d+)", re.I)


def criteria_ids(work_order_text):
    """The `C<n>` criterion ids at line starts of a work order, in order."""
    out = []
    for cid in _CRITERION_ID.findall(work_order_text):
        if cid not in out:
            out.append(cid)
    return out


def _sections(text):
    found, cur = {}, None
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            title = m.group(1).strip().rstrip(":").strip().lower()
            cur = next((s for s in REPORT_SECTIONS if s.lower() == title), None)
            if cur:
                found.setdefault(cur, [])
        elif cur:
            found[cur].append(line)
    return found


def _items(lines):
    return [(m.group(1) or "").strip() for m in (_ITEM.match(ln) for ln in lines) if m]


def _listed_path(item):
    ticks = _TICKS.search(item)
    return (ticks.group(1) if ticks else item.split()[0]).strip()


def _covered(path, files):
    return any(path.startswith(f) if f.endswith("/") else fnmatch.fnmatch(path, f)
               for f in files)


def check_report(text, criteria, files, worktree, base="main"):
    """Read-only check of a stage report against its work order and the worktree's git."""
    problems, warnings = [], []
    sections = _sections(text)
    for sec in REPORT_SECTIONS:
        if sec not in sections:
            problems.append("missing section: %s" % sec)
    rows = {}
    for line in text.splitlines():
        m = _ROW.match(line)
        if m and m.group(1) not in rows:
            rows[m.group(1)] = (m.group(2).strip("*` ").lower(), (m.group(3) or "").strip())
    for cid in criteria:
        if cid not in rows:
            problems.append("missing criterion %s" % cid)
            continue
        status, reason = rows[cid]
        if status not in CRITERION_STATUSES:
            problems.append("criterion %s: unknown status %r (expected one of %s)"
                            % (cid, status, ", ".join(CRITERION_STATUSES)))
        elif status != "done" and not reason:
            problems.append("criterion %s: status %r needs a reason" % (cid, status))
    m = _RESULT.search(text)
    outcome = m.group(1).strip().lower() if m else None
    if outcome is None:
        problems.append("no Result line")
    elif outcome not in OUTCOMES:
        problems.append("unknown result %r (expected one of %s)" % (outcome, ", ".join(OUTCOMES)))
        outcome = None
    m = _COMMIT.search(text)
    sha = m.group(1) if m else None

    checks = 0
    for item in _items(sections.get("Verification", [])):
        if not _TICKS.search(item):
            problems.append("verification item without a command in backticks: %s" % item)
            continue
        checks += 1
        skip = _SKIP.search(item)
        if not skip:
            problems.append("check command without SKIP: <n>: %s" % item)
        elif int(skip.group(1)) > 0:
            warnings.append("SKIP: %s in %s; the foreman judges the skipped tests and says so "
                            "in the act" % (skip.group(1), _TICKS.search(item).group(0)))
    changed_items = _items(sections.get("Changed files", []))
    if any(not it.strip(" `") for it in changed_items):
        problems.append("empty list item under Changed files")
        changed_items = [it for it in changed_items if it.strip(" `")]
    if outcome == "delivered":
        if checks == 0:
            problems.append("delivered: no check command in Verification")
        problems += _check_delivery(sha, changed_items, files, worktree, base)
    return {"ok": not problems, "problems": problems, "warnings": warnings,
            "outcome": outcome, "sha": sha}


def _check_delivery(sha, changed_items, files, worktree, base):
    """Git facts for a `delivered` report: problems only; any git failure is a refusal."""
    base = _resolve_base(worktree, base)
    if not sha:
        return ["delivered: no Commit SHA"]

    def failed(p):
        return ["delivered: git failed: %s" % (p.stderr.strip() or "exit %d" % p.returncode)]
    problems = []
    p = _git(["merge-base", "--is-ancestor", sha, "HEAD"], worktree)
    if p.returncode == 1:
        problems.append("delivered: commit %s is not an ancestor of HEAD" % sha)
    elif p.returncode != 0:
        return failed(p)
    p = _git(["rev-list", "%s..HEAD" % base], worktree)
    if p.returncode != 0:
        return problems + failed(p)
    branch_commits = p.stdout.split()
    if not branch_commits:
        problems.append("delivered: no commit in %s..HEAD" % base)
    else:
        full = _git(["rev-parse", "--verify", "--quiet", "%s^{commit}" % sha], worktree)
        if full.returncode != 0:
            return problems + failed(full)
        if full.stdout.strip() not in branch_commits:
            problems.append("delivered: commit %s is not in %s..HEAD (not on the crew's branch)"
                            % (sha, base))
    p = _git(["diff", "--name-only", "%s...HEAD" % base], worktree)
    if p.returncode != 0:
        return problems + failed(p)
    changed = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    if not changed:
        problems.append("delivered: empty diff %s...HEAD" % base)
    listed = [_listed_path(it) for it in changed_items if it.strip()]
    if set(listed) != set(changed):
        problems.append("changed files differ from git diff: listed but not changed: %s; "
                        "changed but not listed: %s"
                        % (", ".join(sorted(set(listed) - set(changed))) or "-",
                           ", ".join(sorted(set(changed) - set(listed))) or "-"))
    if files:
        outside = sorted(f for f in set(listed) | set(changed) if not _covered(f, files))
        if outside:
            problems.append("changed files outside the work order's files: %s"
                            % ", ".join(outside))
    return problems


def report_fix(name, work_order, stage, problems):
    """Ask the crew to fix a failed stage report; the 4th request opens a gate."""
    attempt = journal.bump_iterations(stage, "report_fix")
    if journal.cycle_exceeded(stage, "report_fix"):
        journal.wait_customer(stage, "the stage report failed the check %d times; the "
                              "customer decides how to go on" % attempt)
        return {"status": "waits_customer"}
    path = message_file(work_order, "\n".join("- %s" % p for p in problems) + "\n")
    sent = deliver(name, _template("Fix the report").format(path=path), work_order)
    if sent == "accepted":
        return {"status": "sent", "attempt": attempt}
    return {"status": "not_delivered", "attempt": attempt, "delivery": sent}


def ask_predecessor(work_order, session_id, cwd, question):
    """Manual tool: ask a fork of a finished session a question; the session itself is not
    changed. Costs one run of the work order."""
    journal.spend(work_order, runs=1)
    try:
        p = subprocess.run(["claude", "--resume", session_id, "--fork-session", "-p", question],
                           cwd=cwd, capture_output=True, text=True,
                           timeout=_defaults.load()["crew"]["ask_timeout_s"])
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("claude did not answer: %s" % e)
    if p.returncode != 0:
        raise RuntimeError("claude exited %d: %s" % (p.returncode, p.stderr.strip()))
    return p.stdout


def _check_report_cli(name, work_order, base="main"):
    worktree = journal.kv_get(_kv(name, "worktree"))
    if not worktree:
        raise ValueError("worktree of the crew %s is unknown" % name)
    shown = journal.bd(["show", work_order])
    description = (shown[0].get("description") or "") if shown else ""
    return check_report(summary(name), criteria_ids(description),
                        journal.work_order_files(work_order), worktree, base)


def _report_fix_cli(name, work_order, stage, problems_file):
    with open(problems_file, encoding="utf-8") as fh:
        problems = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
    return report_fix(name, work_order, stage, problems)


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

    cmd("start", "site_path", "work_order", "name", "caller_pane")
    cmd("name_for", "site", "n")
    cmd("send_work_order", "name", "text", work_order={})
    cmd("state", "name")
    cmd("summary", "name")
    cmd("request_handover", "name")
    cmd("complete_handover", "name", "work_order", "work_order_text")
    cmd("evidence", "worktree", base={"default": "main"})
    cmd("merge_check", "worktree", accepted={"default": ""})
    cmd("answer", "name", choice={"type": int}, text={})
    cmd("pause", "name")
    cmd("resume", "name")
    cmd("wait_limit", "name")
    cmd("restart", "name", "work_order", "work_order_text", "reason", caller_pane={})
    cmd("check_report", "name", "work_order", base={"default": "main"})
    cmd("report_fix", "name", "work_order", "stage", problems_file={"required": True})
    cmd("ask_predecessor", "work_order", "session_id", "cwd", "question")
    cmd("stop", "name", "work_order")
    cmd("cleanup", "site_path", "work_order")
    args = vars(ap.parse_args(argv))
    c = args.pop("cmd")
    if c == "merge_check":
        args["accepted"] = [x for x in args["accepted"].split(",") if x]
    try:
        if c == "name_for":
            args["n"] = int(args["n"])
        func = {"check_report": _check_report_cli, "report_fix": _report_fix_cli}.get(c)
        result = (func or globals()[c])(**args)
    except (herdr_cli.HerdrError, journal.JournalError, ValueError, RuntimeError) as e:
        print(json.dumps({"error": str(e), "code": getattr(e, "code", None)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
