#!/usr/bin/env python3
"""Crew lifecycle: a Claude crew in a herdr pane inside a per-work-order git worktree.

new_site, start, deliver, state, summary, handover, restart, limit wait, report check, evidence,
merge check, answer, pause, spend, acceptance, stop, cleanup.
The foreman never merges, never pushes and never answers a trust/consent prompt, save the
folder trust of the site `new_site` has just made empty.
While the emergency-stop file exists, every command that starts a crew or sends to one is
refused (`emergency_stop`); `pause` and the read-only commands stay available.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402
import herdr_cli  # noqa: E402
import hooks  # noqa: E402
import journal  # noqa: E402
from journal import JournalError  # noqa: E402  (stays real when tests patch `journal`)
import limits  # noqa: E402
import scheduler  # noqa: E402
import slots  # noqa: E402
import transcript  # noqa: E402

REFERENCES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "references")
HANDOVER_MARKER = "HANDOVER READY"

# The only drop decision: an act `answer` starting with this line (see acts.md).
DROP_MARKER = "[customer decision] drop"

REPORT_SECTIONS = ("Summary", "Behaviour", "Changed files", "Verification", "Stubs",
                   "For the passport", "Residual risks")
CRITERION_STATUSES = ("done", "not applicable", "deferred", "blocked", "stub")
OUTCOMES = ("delivered", "no changes", "blocked", "abandoned")
# `limit_died` and `login` (a restart that stopped at a login screen) are never counted
RESTART_REASONS = ("gone", "hung", "limit_died", "login")
UNCOUNTED_REASONS = ("limit_died", "login")


def _consent_result(visible, pane):
    """The start result for a consent or login screen (`needs_login`, `needs_trust`, or
    `needs_consent` with the kind), else None. Never sends keys."""
    if herdr_cli.not_logged_in(visible):
        return {"status": "needs_login", "pane_id": pane}
    kind = herdr_cli.consent_dialog(visible)
    if kind is None:
        return None
    if kind == "trust":
        return {"status": "needs_trust", "pane_id": pane}
    return {"status": "needs_consent", "kind": kind, "pane_id": pane}


def _kv(name, field):
    return "crew.%s.%s" % (name, field)


def _fingerprint(text):
    """The SHA-256 of a work-order text (stripped): what `crew.<name>.delivered` records."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


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
    limit = _defaults.load()["timeouts"]["git_s"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        raise RuntimeError("git %s timed out after %s s" % (" ".join(args), limit))
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p


def _git_soft(args, cwd=None):
    """`_git` for callers that read the return code: a timeout becomes a failed process
    (return code 124, the message in stderr), so each caller keeps its failing-git path."""
    try:
        return _git(args, cwd)
    except RuntimeError as e:
        return subprocess.CompletedProcess(["git"] + list(args), 124, "", str(e))


def _worktrees(site_path):
    """[{path, branch}] from `git worktree list --porcelain`."""
    out, cur = [], None
    for line in _git_soft(["worktree", "list", "--porcelain"], site_path).stdout.splitlines():
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


def _recorded_base(worktree, work_order=None):
    """The commit the crew's worktree started from, as `start` recorded it (kv of the crew
    working there, else the issue record with that worktree); None for older records. The
    records of main-checkout work orders share the site as their "worktree": they are found by
    `work_order` only."""
    try:
        return _recorded_base_of(os.path.realpath(worktree), journal.kv_all(), work_order)
    except journal.JournalError:  # no journal to ask: the caller falls back to the default branch
        return None


def _recorded_base_of(real, kv, work_order=None):
    for name in journal.crews(kv):
        wt, base = _kv_of(kv, _kv(name, "worktree")), _kv_of(kv, _kv(name, "base_commit"))
        if wt and base and os.path.realpath(wt) == real:
            if work_order is None or _kv_of(kv, _kv(name, "work_order")) in (None, work_order):
                return base
    for wo, rec in journal.issues(kv=kv).items():
        if rec.get("base_commit") and rec.get("worktree") \
                and os.path.realpath(rec["worktree"]) == real \
                and (wo == work_order if rec.get("main_checkout") else True):
            return rec["base_commit"]
    return None


def _resolve_base(worktree, base=None, work_order=None):
    """`base` when it names a commit; else the commit the worktree started from (recorded by
    `start`); else, for records made before that, the site's default branch (main, master, or
    the branch checked out in the main worktree)."""
    base = base or _recorded_base(worktree, work_order)
    if base and _git_soft(["rev-parse", "--verify", "--quiet", "%s^{commit}" % base],
                     worktree).returncode == 0:
        return base
    for b in ("main", "master"):
        if _git_soft(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % b],
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


def _screen_refusal(visible, exits=True):
    """What on the bottom block of a crew screen forbids typing there: "login", "consent:<kind>",
    "exit_dialog" (unless `exits` is false) or "limit_screen"; else None."""
    if herdr_cli.not_logged_in(visible):
        return "login"
    kind = herdr_cli.consent_dialog(visible)
    if kind is not None:
        return "consent:%s" % kind
    if exits and exit_dialog(visible) is not None:
        return "exit_dialog"
    if limits.limit_screen(visible, _defaults.load()) is not None:
        return "limit_screen"
    return None


def deliver(name, text, work_order=None):
    """The only way a message reaches a crew. One line goes by `send`, several lines by a
    file named in one line. "stalled" is recovered without a second `prompt` (Enter only
    when the composer holds our line). Returns "accepted" | "blocked" | "stalled" |
    "unconfirmed" | "consent:<kind>" | "login" | "exit_dialog" | "limit_screen". The screen is
    read first: a dialog herdr does not report as blocked still gets no text and no Enter."""
    work_order = work_order or journal.kv_get(_kv(name, "work_order"))
    text = text.strip("\n")
    if "\n" in text:
        text = _template("Message by file").format(path=message_file(work_order, text))
    refused = _screen_refusal(herdr_cli.agent_visible(name))
    if refused:
        return refused
    result = herdr_cli.send(name, text)
    if result == "stalled":
        result = herdr_cli.recover_stalled(name, text)
    if result in ("blocked", "stalled", "unconfirmed"):
        # a consent prompt may be why the line did not land: name its kind (nothing is
        # pressed, nothing is re-sent)
        visible = herdr_cli.agent_visible(name)
        if herdr_cli.not_logged_in(visible):
            return "login"
        kind = herdr_cli.consent_dialog(visible)
        if kind:
            return "consent:%s" % kind
    return result


# herdr waits 30 s for the agent's first prompt by default; the subprocess bound is 90 s
_AGENT_START_TIMEOUT = ["--timeout", "90000"]


def _claude_args(*pairs):
    return ["--"] + [x for pair in pairs for x in pair]


def emergency_stop():
    """Whether the customer's emergency-stop file exists."""
    return os.path.exists(_defaults.load()["emergency"]["stop_path"])


_STOPPED = "emergency_stop"


def _settings_pairs(work_order=None):
    """`(("--settings", <path>),)` while kv `hooks.enabled` is `1` (the settings file is
    written first), else no pair at all: the crew then runs without hooks. A work order whose
    record says `main_checkout` gets the settings of its own (`crew-settings-merge-<id>.json`,
    its guard naming the merge branch) and never runs without them: `ValueError` when the
    hooks are off or the record names no usable branch."""
    rec = (journal.issue(work_order) or {}) if work_order else {}
    on = journal.kv_get("hooks.enabled") == "1"
    if rec.get("main_checkout"):
        if not on:
            raise ValueError("the crew hooks are off: a main-checkout crew needs its guard")
        return (("--settings", hooks.write_settings(
            merge_branch=rec.get("merge_branch"), work_order=work_order)),)
    if not on:
        return ()
    return (("--settings", hooks.write_settings()),)


def _set_session(name, worktree, session_id, work_order):
    """Record the crew's current session; the work order's issue record lists every one."""
    rec = journal.issue(work_order)
    if rec is not None:
        sessions = list(rec.get("sessions") or [])
        if session_id not in sessions:
            journal.issue_set(work_order, rec.get("state", "issuing"),
                              sessions=sessions + [session_id])
    journal.kv_set(_kv(name, "session"), session_id)
    if worktree:
        journal.kv_set(_kv(name, "transcript"), transcript.transcript_path(worktree, session_id))


# ---- start ------------------------------------------------------------------

def site_trust(site_path, retry=True):
    """Whether Claude Code has the folder trust of exactly this site: True, False, or None
    when `crew.trust_path` cannot be parsed twice in a row (unknown, not "untrusted"); with
    `retry=False` one failed parse is enough and nothing sleeps (the monitor's call).
    The file is only read; of its content only the one flag is looked at. A parent folder's
    entry never counts; the path as given or its real path does."""
    cfg = _defaults.load()["crew"]
    keys = [site_path.rstrip("/") or "/"]
    real = os.path.realpath(site_path)
    if real not in keys:
        keys.append(real)
    for attempt in (0, 1):
        try:
            with open(cfg["trust_path"], encoding="utf-8") as fh:
                projects = json.load(fh).get("projects")
        except FileNotFoundError:
            return False
        except (OSError, ValueError, AttributeError):
            if attempt == 0 and retry:
                time.sleep(cfg["trust_retry_s"])  # Claude Code rewrites the file often
                continue
            return None
        if not isinstance(projects, dict):
            return False
        return any(isinstance(projects.get(k), dict)
                   and projects[k].get("hasTrustDialogAccepted") is True for k in keys)


def _identity_of(info):
    """The identity herdr gives a pane: its terminal and tab ids (None when not shown)."""
    return {"terminal": info.get("terminal_id"), "tab": info.get("tab_id")}


def _open_pane(caller_pane, cwd):
    """Split a new pane next to `caller_pane`; returns (pane id, identity). A field the
    split answer lacks is read from `pane get` right after it; what stays unknown is None,
    and a record without a terminal never matches (`_same_pane`)."""
    info = herdr_cli.run(["pane", "split", caller_pane, "--direction",
                          _split_direction(caller_pane), "--no-focus", "--cwd",
                          cwd])["result"]["pane"]
    pane = info["pane_id"]
    if not (info.get("terminal_id") and info.get("tab_id")):
        got = _pane_info(pane) or {}
        info = dict(info, terminal_id=info.get("terminal_id") or got.get("terminal_id"),
                    tab_id=info.get("tab_id") or got.get("tab_id"))
    return pane, _identity_of(info)


def _pane_info(pane_id):
    """`pane get` as a dict, None when herdr cannot show the pane (gone)."""
    try:
        return _pane(pane_id)
    except (herdr_cli.HerdrError, KeyError, TypeError):
        return None


def _same_pane(rec, info):
    """Whether `info` (a `pane get` answer, None if it failed) is the pane that `rec`
    ({"terminal", "tab"} of the record) was opened as. Pane ids are reused, so the id alone
    proves nothing; a record without a terminal id never matches."""
    return (bool(info) and bool(rec.get("terminal")) and bool(rec.get("tab"))
            and info.get("terminal_id") == rec["terminal"]
            and info.get("tab_id") == rec["tab"])


def _has_agent(pane_id, info):
    """Whether an agent runs in the pane; when herdr cannot say, True (the pane is left alone)."""
    try:
        return herdr_cli.agent_by_pane(pane_id) is not None or bool(info.get("agent"))
    except herdr_cli.HerdrError:
        return True


def _close_pane(pane_id):
    """`pane close`, best-effort: False when herdr fails (the pane stays recorded)."""
    try:
        herdr_cli.run_ack(["pane", "close", pane_id])
        return True
    except herdr_cli.HerdrError:
        return False


def _spares_of(rec):
    return [x for x in (rec.get("spare") or []) if isinstance(x, dict) and x.get("pane")]


def _record_panes(work_order, rec, current, spares):
    """Write the panes of a start into the issue record; `current` is
    {"pane", "terminal", "tab"} or None."""
    journal.issue_set(work_order, rec.get("state") or "not_issued",
                      pane=current["pane"] if current else None,
                      pane_identity=({"terminal": current["terminal"], "tab": current["tab"]}
                                     if current else None),
                      spare=spares)


def _take_pane(work_order, site_path, caller_pane):
    """The pane a start works in. The issue record's pane when it is the same pane (identity),
    holds no agent and stands in the site (reused). Otherwise: a pane with an agent (the
    customer's `claude`) becomes a spare and stays open; a bare shell elsewhere is closed; a
    gone or recycled pane is forgotten. Then a new split, recorded with its identity. The
    decision rests on the pane's identity and on the agents in it, never on a session id."""
    rec = journal.issue(work_order) or {}
    spares = _spares_of(rec)
    cur = None
    if rec.get("pane"):
        ident = rec.get("pane_identity") or {}
        cur = {"pane": rec["pane"], "terminal": ident.get("terminal"), "tab": ident.get("tab")}
    if cur:
        info = _pane_info(cur["pane"])
        if not _same_pane(cur, info):
            cur = None  # gone, or the id was recycled: forgotten, never reused or closed
        elif _has_agent(cur["pane"], info) or not info.get("foreground_cwd"):
            spares.append(cur)  # an agent, or a cwd herdr does not show: left alone
            cur = None
        elif _same_dir(info.get("foreground_cwd"), site_path):
            _record_panes(work_order, rec, cur, spares)
            return cur["pane"]
        else:
            if not _close_pane(cur["pane"]):
                spares.append(cur)
            cur = None
    pane, ident = _open_pane(caller_pane, site_path)
    _record_panes(work_order, rec, dict(ident, pane=pane), spares)
    return pane


def close_start_panes(work_order):
    """Close the panes a start left (the issue record's `pane` and every `spare`) that are
    still the recorded pane (identity), hold no agent and show a working directory; a pane with an agent stays
    recorded, a gone or recycled one is dropped from the record, never closed. Returns the
    closed pane ids."""
    rec = journal.issue(work_order)
    if not rec:
        return []
    entries = []
    if rec.get("pane"):
        ident = rec.get("pane_identity") or {}
        entries.append({"pane": rec["pane"], "terminal": ident.get("terminal"),
                        "tab": ident.get("tab")})
    entries += _spares_of(rec)
    closed, kept = [], []
    for e in entries:
        info = _pane_info(e["pane"])
        if not _same_pane(e, info):
            continue
        if _has_agent(e["pane"], info) or not info.get("foreground_cwd"):
            kept.append(e)  # an agent, or a cwd herdr does not show: identity is no proof of idleness
            continue
        if _close_pane(e["pane"]):
            closed.append(e["pane"])
        else:
            kept.append(e)
    if closed or len(kept) != len(entries):
        was_current = bool(rec.get("pane")) and entries[0] in kept
        _record_panes(work_order, rec, entries[0] if was_current else None,
                      kept[1:] if was_current else kept)
    return closed


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


MERGE_MARKER = "[customer decision] merge"
_MERGE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _merge_decision(work_order):
    """The branch named by the latest `[customer decision] merge <branch> ...` answer act of
    the work order: "" when that act names none, None when there is no such act."""
    found = None
    for text in journal.acts(work_order, "answer"):
        if text.startswith(DROP_MARKER):  # a later drop decision withdraws an earlier merge
            found = None
            continue
        rest = text[len(MERGE_MARKER):] if text.startswith(MERGE_MARKER) else None
        if rest is not None and (not rest or rest[0].isspace()):
            found = rest.split()[0] if rest.split() else ""
    return found


def _porcelain_path(entry):
    """The paths of one `git status --porcelain` line (two for a rename or a copy)."""
    paths = entry[3:].split(" -> ")
    return [p[1:-1] if len(p) > 1 and p.startswith('"') and p.endswith('"') else p
            for p in paths]


def _main_checkout_refusal(site_path, work_order):
    """`(refusal result, None)` or `(None, the branch to merge)` for `start --main-checkout`;
    the checks in order: hooks_off, no_merge_decision, no_branch, not_default_branch,
    main_dirty, main_busy. Reads only; nothing is started."""
    if journal.kv_get("hooks.enabled") != "1":
        return {"status": "hooks_off"}, None
    branch = _merge_decision(work_order)
    if branch is None:
        return {"status": "no_merge_decision"}, None
    current = _git_soft(["branch", "--show-current"], site_path).stdout.strip()
    if not _MERGE_BRANCH.match(branch) or branch in ("main", "master", current) \
            or _git_soft(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % branch],
                         site_path).returncode != 0:
        return {"status": "no_branch", "branch": branch}, None
    if current not in ("main", "master"):
        return {"status": "not_default_branch", "branch": current}, None
    p = _git_soft(["status", "--porcelain", "--untracked-files=all"], site_path)
    if p.returncode != 0:
        return {"status": "main_dirty", "detail": p.stderr.strip()}, None
    dirty = [e for e in p.stdout.splitlines() if e.strip()
             and not all(path.startswith(".claude/worktrees/") for path in _porcelain_path(e))]
    if dirty:
        return {"status": "main_dirty", "entries": dirty[:10]}, None
    real = os.path.realpath(site_path)
    busy = [c for c in journal.crews()
            if (journal.kv_get(_kv(c, "worktree")) or "")
            and os.path.realpath(journal.kv_get(_kv(c, "worktree"))) == real]
    # a merge start still waiting at a consent prompt holds the checkout too
    busy += [wo for wo, rec in journal.issues().items()
             if wo != work_order and rec.get("main_checkout") and rec.get("state") == "issuing"
             and rec.get("site") and os.path.realpath(rec["site"]) == real]
    if busy:
        return {"status": "main_busy", "with": busy}, None
    return None, branch


def start(site_path, work_order, name, caller_pane, main_checkout=False):
    """Start a crew for a work order: in a new worktree (`claude -w`), or with `main_checkout`
    in the site's own main checkout to merge the branch the customer decided on (see
    `_main_checkout_refusal` for what it refuses). Its record, `main_checkout` and
    `merge_branch`, is what a consent-answered start, a restart and a handover rebuild from."""
    if emergency_stop():
        return {"status": _STOPPED}
    if not herdr_cli.valid_name(name):
        return {"status": "bad_name"}
    rec = journal.issue(work_order)
    if rec and rec.get("state") in ("issuing", "issued") and rec.get("pane"):
        there = herdr_cli.agent_by_pane(rec["pane"])
        theirs = None
        if there is not None and rec.get("state") == "issuing" and rec.get("name") == name \
                and rec.get("session_id"):
            seen = (there.get("agent_session") or {}).get("value")
            if seen == rec["session_id"]:
                # our own session in this start's pane: the customer answered a consent
                # prompt there (or is still to); no new pane, no new agent
                pane = rec["pane"]
                return (_consent_result(herdr_cli.pane_visible(pane), pane)
                        or _register_started(site_path, work_order, name, pane,
                                             rec["session_id"]))
            theirs = bool(seen)  # someone else's session (the customer's own `claude`)
        if there is not None and not theirs:
            return {"status": "already_issued", "name": rec.get("name"),
                    "pane_id": rec["pane"]}
        # a foreign session in the pane is left alone: start anew in a new pane
    merge_branch = None
    if main_checkout:
        refusal, merge_branch = _main_checkout_refusal(site_path, work_order)
        if refusal:
            return refusal
    active = journal.crews()
    if len(active) >= journal.machine()["parallel_ceiling"]:
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

    trust = site_trust(site_path)
    pane = _take_pane(work_order, site_path, caller_pane)
    if trust is False:
        # `claude -w` refuses a site whose own path has no folder trust; the customer
        # accepts it in this pane once, so no agent, no keys, no session id is spent
        journal.issue_set(work_order, "not_issued", name=name, pane=pane, reason="needs_trust",
                          site=site_path)
        return {"status": "needs_trust", "pane_id": pane, "site": site_path, "trust": "missing"}
    session_id = str(uuid.uuid4())
    earlier_rec = journal.issue(work_order) or {}
    earlier = list(earlier_rec.get("sessions") or [])
    # the record is what a consent-answered start, a restart and a handover rebuild the
    # session arguments from, so it is written before the agent starts (and a plain start
    # forgets a merge record an earlier attempt left)
    mode = {"main_checkout": True, "merge_branch": merge_branch} if main_checkout \
        else ({"main_checkout": False, "merge_branch": None}
              if earlier_rec.get("main_checkout") else {})
    journal.issue_set(work_order, "issuing", name=name, pane=pane, session_id=session_id,
                      sessions=earlier + [session_id], site=site_path, **mode)
    try:
        herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane]
                      + _AGENT_START_TIMEOUT
                      + _claude_args(("--session-id", session_id), ("--add-dir", _files_dir()),
                                     *(() if main_checkout
                                       else (("-w", "prorab-%s" % work_order),)),
                                     *_settings_pairs(work_order)),
                      timeout_s=90)
    except herdr_cli.HerdrError as e:
        if e.code not in ("agent_not_ready", "timeout"):
            raise
        return (_consent_result(herdr_cli.pane_visible(pane), pane)
                or {"status": "not_ready", "pane_id": pane})
    result = _register_started(site_path, work_order, name, pane, session_id)
    if trust is None:
        result["trust"] = "unknown"
    return result


def _locate_worktree(site_path, work_order):
    """The work order's worktree in the site, its branch (renamed to `prorab/<id>` when it can
    be) and the commit it starts from: (path, branch, commit), each None when unknown."""
    wt = _find_worktree(site_path, work_order)
    worktree = wt["path"] if wt else None
    branch = wt["branch"] if wt else None
    want = "prorab/%s" % work_order
    if wt and branch != want:
        if _git_soft(["branch", "-m", want], worktree).returncode == 0:
            branch = want
    base_commit = None
    if worktree:
        p = _git_soft(["rev-parse", "HEAD"], worktree)
        base_commit = p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    return worktree, branch, base_commit


def _locate_main_checkout(site_path):
    """(site path, the branch checked out there, its HEAD): the "worktree" of a main-checkout
    crew; each None when git cannot tell."""
    branch = _git_soft(["branch", "--show-current"], site_path).stdout.strip() or None
    p = _git_soft(["rev-parse", "HEAD"], site_path)
    head = p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    return site_path, branch, head


def _register_started(site_path, work_order, name, pane, session_id):
    """The tail of `start` once Claude Code runs in `pane`: find the worktree, name the
    branch, record the issue, register the crew. For a main-checkout work order (its record
    says so) the "worktree" is the site itself, the branch the one checked out there and the
    base the HEAD of the site now, before the crew has the work order."""
    main = (journal.issue(work_order) or {}).get("main_checkout")
    if main:
        worktree, branch, base_commit = _locate_main_checkout(site_path)
    else:
        worktree, branch, base_commit = _locate_worktree(site_path, work_order)
    if worktree:  # so the spend stays attributable after the crew is gone
        extra = {"base_commit": base_commit} if base_commit else {}
        journal.issue_set(work_order, (journal.issue(work_order) or {}).get("state", "issuing"),
                          worktree=worktree, **extra)
    journal.register_crew(name)
    fields = [("work_order", work_order), ("site", site_path), ("pane", pane)]
    ident = (journal.issue(work_order) or {}).get("pane_identity")
    if ident:
        fields.append(("pane_identity", json.dumps(ident)))
    if worktree:
        fields += [("worktree", worktree), ("branch", branch or "")]
        if base_commit:
            fields.append(("base_commit", base_commit))
    for field, value in fields:
        journal.kv_set(_kv(name, field), value)
    _set_session(name, worktree, session_id, work_order)
    result = {"status": "started" if worktree else "started_no_worktree", "pane_id": pane, "name": name,
              "session_id": session_id, "worktree": worktree, "branch": branch}
    if main:
        result.update(main_checkout=True,
                      merge_branch=(journal.issue(work_order) or {}).get("merge_branch"))
    journal.add_act(work_order, "answer", "crew %s started: pane %s, branch %s, session %s"
                    % (name, pane, branch, session_id))
    return result


# ---- the folder trust of a site this shift made -------------------------------
# The one place where a script presses a key on a consent dialog. It is narrow on purpose:
# only the folder `new_site` has just made empty, only Claude Code's own trust dialog for
# exactly that path with "Yes, I trust this folder" selected, one Enter, every condition
# checked again right before it, nothing pressed after it. `crew.trust_path` is only read.

_TRUST_YES = "1. Yes, I trust this folder"
_SELECTED_ROW = re.compile(r"^[>❯] +(.*)$")


def _site_key(real):
    """The kv key of a site `new_site` made: the first 16 hex of the sha1 of its real path."""
    return "site.created.%s" % hashlib.sha1(real.encode("utf-8")).hexdigest()[:16]


def _site_record(site_path):
    """The kv record `new_site` wrote for this site (a dict), else None."""
    raw = journal.kv_get(_site_key(os.path.realpath(site_path.rstrip("/") or "/")))
    try:
        rec = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _new_site_eligible(site_path, work_order):
    """None when the folder is one `new_site` made for this work order and is still exactly
    what it made: no link, directly under the projects root, a repository of its own with
    the one recorded commit, no remote, nothing untracked or changed. Else the reason."""
    path = site_path.rstrip("/") or "/"
    if os.path.islink(path):
        return "the site path is a link"
    real = os.path.realpath(path)
    try:
        rec = _site_record(path)
    except JournalError:
        return "the journal cannot be read"
    if rec is None:
        return "no record that new_site made this folder"
    recorded = rec.get("path")
    if not isinstance(recorded, str) or os.path.realpath(recorded) != real:
        return "the record is for another path"
    if rec.get("work_order") != work_order:
        return "the record is for another work order"
    commit = rec.get("commit")
    if not isinstance(commit, str) or not commit.strip():
        return "the record holds no commit"
    root = os.path.realpath(_defaults.load()["crew"]["projects_dir"])
    if os.path.dirname(real) != root:
        return "the folder is not directly under the projects folder"
    top = _git_soft(["rev-parse", "--show-toplevel"], real)
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != real:
        return "the folder is not a repository of its own"
    commits = _git_soft(["rev-list", "--all"], real)
    if commits.returncode != 0 or commits.stdout.split() != [commit.strip()]:
        return "the repository holds more than the recorded commit"
    remotes = _git_soft(["remote"], real)
    if remotes.returncode != 0 or remotes.stdout.strip():
        return "the repository has a remote"
    # ignored files count too: an exclude rule must not hide a hooks or MCP config
    status = _git_soft(["status", "--porcelain", "--untracked-files=all", "--ignored"], real)
    if status.returncode != 0 or status.stdout.strip():
        return "the repository has changes, untracked or ignored files"
    return None


def _trust_name(site_path):
    """The agent name of the trust session: `<base>-trust`, base as `name_for` builds it."""
    base = name_for(site_path, 0)[:-len("-0")][:32 - len("-trust")].rstrip("-")
    return "%s-trust" % (base or "c")


def _trust_screen_problem(screen, site_path):
    """None when the screen is Claude Code's folder-trust dialog for exactly this site with
    "Yes, I trust this folder" selected, else why not. Read in the bottom block only; the
    path must be a whole line of it; no marker of another dialog may share the block."""
    if herdr_cli.not_logged_in(screen):
        return "the screen is a login screen"
    if herdr_cli.consent_dialog(screen) != "trust":
        return "the screen is not the folder trust dialog"
    block = herdr_cli.bottom_block(screen, _defaults.load()["screen"]["tail_lines"])
    low = block.lower()
    for kind, markers in herdr_cli.CONSENT_KINDS:
        if kind != "trust" and any(m in low for m in markers):
            return "the block also shows a %s dialog" % kind
    lines = [ln.replace(" ", " ").strip() for ln in block.splitlines()]
    path = site_path.rstrip("/") or "/"
    if not any(ln in (path, os.path.realpath(path)) for ln in lines):
        return "the dialog does not show the site path as a line of its own"
    selected = [m.group(1).strip() for m in map(_SELECTED_ROW.match, lines) if m]
    if selected != [_TRUST_YES]:
        return "'Yes, I trust this folder' is not the selected option"
    return None


def _trust_needs(pane, reason):
    return {"status": "needs_trust", "pane_id": pane, "reason": reason}


def _record_trust_act(work_order, text):
    """The act of a pressed Enter; a journal failure never hides the result that follows."""
    try:
        journal.add_act(work_order, "answer", text)
    except JournalError:
        pass


def _new_site_trust(site_path, work_order, pane):
    """Accept the folder trust of a site `new_site` just made, in `pane`, by its own dialog.
    "trusted" | "needs_trust" (with a reason) | the consent result of another dialog |
    `_STOPPED`. One Enter at most; see the section comment. No CLI of its own."""
    if emergency_stop():
        return {"status": _STOPPED}
    reason = _new_site_eligible(site_path, work_order)
    if reason:
        return _trust_needs(pane, reason)
    name = _trust_name(site_path)
    if not herdr_cli.valid_name(name):
        return _trust_needs(pane, "no valid agent name for the trust session")
    commit = (_site_record(site_path) or {}).get("commit")
    session_id = str(uuid.uuid4())
    rec = journal.issue(work_order) or {}
    journal.issue_set(work_order, rec.get("state") or "not_issued",
                      sessions=list(rec.get("sessions") or []) + [session_id])
    try:
        herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane]
                      + _AGENT_START_TIMEOUT + _claude_args(("--session-id", session_id)),
                      timeout_s=90)
    except herdr_cli.HerdrError as e:
        if e.code not in ("agent_not_ready", "timeout"):
            raise
        screen = _screen_of(pane)
        if herdr_cli.not_logged_in(screen) or herdr_cli.consent_dialog(screen) != "trust":
            return (_consent_result(screen, pane) or {"status": "not_ready", "pane_id": pane})
        problem = _trust_screen_problem(screen, site_path)
        if problem:
            return _trust_needs(pane, problem)
        # everything again, as close to the key as it can be: the screen, the repository,
        # the stop file
        screen = _screen_of(pane)
        problem = (_trust_screen_problem(screen, site_path)
                   or _new_site_eligible(site_path, work_order)
                   or ("the emergency stop is set" if emergency_stop() else None))
        if problem:
            return _trust_needs(pane, problem)
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            herdr_cli.run_ack(["pane", "send-keys", pane, "enter"])
        except herdr_cli.HerdrError as err:
            # a failed or timed-out send may still have delivered the Enter: record it as
            # sent, press nothing more, and leave the pane to the customer
            herdr_cli.log_keys(pane, ["enter"], "new-site trust", crew=name, ok=False,
                               error=str(err))
            reason = ("Enter sent to %s for %s but its delivery is not confirmed (%s)"
                      % (pane, site_path, err))
            _record_trust_act(work_order, "[site] folder trust: %s; created empty by new_site "
                              "(commit %s), %s" % (reason, commit, now))
            return _trust_needs(pane, reason)
        herdr_cli.log_keys(pane, ["enter"], "new-site trust", crew=name)
        _record_trust_act(work_order,
                          "[site] folder trust accepted by the foreman: %s, created empty by "
                          "new_site (commit %s), pane %s, %s" % (site_path, commit, pane, now))
        try:
            ready = herdr_cli.wait_ready(name)
        except herdr_cli.HerdrError:
            ready = "timeout"
        if ready not in ("idle", "done"):
            after = _screen_of(pane)  # nothing more is pressed, whatever it shows
            return _consent_result(after, pane) or {"status": "not_ready", "pane_id": pane}
    left = _exit(name, pane, 60, 1)
    journal.kv_clear(_kv(name, "exiting"))
    if left != "exited":
        return _trust_needs(pane, "the trust session did not exit: %s" % left)
    if site_trust(site_path, retry=True) is not True:
        return _trust_needs(pane, "trust not recorded")
    return {"status": "trusted", "pane_id": pane}


_SITE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def new_site(site_path, work_order, caller_pane):
    """Make a site that does not exist yet: the folder (`os.mkdir` without `exist_ok`, so the
    call itself proves it is new), `git init -b main`, one empty commit "Start the site" in the
    customer's git identity, the kv record and the act `_new_site_eligible` and the trust read,
    the start pane, then the folder trust (`_new_site_trust`). Refuses under the emergency stop,
    outside `crew.projects_dir` and for a bad name; a git failure removes the folder this call
    made. Writes no file in the site."""
    if emergency_stop():
        return {"status": _STOPPED}
    path = os.path.abspath(site_path.rstrip("/") or "/")
    root = os.path.realpath(_defaults.load()["crew"]["projects_dir"])
    if os.path.realpath(os.path.dirname(path)) != root:
        return {"status": "outside_projects", "projects_dir": root}
    if not _SITE_NAME.match(os.path.basename(path)):
        return {"status": "bad_name", "name": os.path.basename(path)}
    try:
        shown = journal.bd(["show", work_order])
    except JournalError:
        shown = None
    if not shown or shown[0].get("status") == "closed":
        return {"status": "no_work_order", "work_order": work_order}
    try:
        os.mkdir(path)
    except FileExistsError:
        return {"status": "exists", "site": path}
    except OSError as e:
        return {"status": "mkdir_failed", "detail": str(e)}
    # from here the folder is this call's: any failure before the trust step removes it (and
    # its record), so a rerun can succeed
    key = _site_key(os.path.realpath(path))
    step = "git"
    try:
        commit = ""
        for args in (["init", "-q", "-b", "main"],
                     ["commit", "-q", "--allow-empty", "-m", "Start the site"],
                     ["rev-parse", "HEAD"]):
            p = _git_soft(args, path)
            if p.returncode != 0:
                raise RuntimeError("git %s: %s" % (args[0], (p.stderr or p.stdout).strip()))
            commit = p.stdout.strip()
        step = "record"
        journal.kv_set(key, json.dumps({"path": path, "commit": commit,
                                        "work_order": work_order,
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}))
        step = "act"
        journal.add_act(work_order, "answer",
                        "[site] created %s empty: git init -b main, commit %s" % (path, commit))
        step = "pane"
        pane = _take_pane(work_order, path, caller_pane)
    except Exception as e:  # noqa: BLE001  (every failure leaves nothing behind)
        shutil.rmtree(path, ignore_errors=True)
        try:
            journal.kv_clear(key)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "%s_failed" % step, "detail": str(e)}
    return {"status": "created", "site": path, "commit": commit,
            "trust": _new_site_trust(path, work_order, pane), "pane_id": pane}


# ---- sending, state ---------------------------------------------------------

def send_work_order(name, text, work_order=None):
    """Put the work order (with the site passport the caller appended) into its file and send
    the crew the one line naming it. A delivered order marks the issue record `issued`."""
    if emergency_stop():
        return _STOPPED
    work_order = work_order or journal.kv_get(_kv(name, "work_order"))
    path = message_file(work_order, text, name=work_order)
    fp = _fingerprint(text)
    given = journal.kv_get(_kv(name, "delivered"))  # an earlier version was read: say it changed
    heading = "Changed work order" if given is not None and given != fp else "Message by file"
    result = deliver(name, _template(heading).format(path=path), work_order)
    if result == "accepted":
        journal.kv_set(_kv(name, "delivered"), fp)
        if work_order:
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


def _context(session_id, path, d):
    """The session's context: the status-line tap's file first, else the transcript."""
    return limits.sensor_context(session_id, d) or (
        transcript.context_status(path, d) if path else None)


def gone_state():
    """The state of a crew whose agent is not listed."""
    return {"status": "gone", "name_ok": False, "bg_shells": 0, "consent": None,
            "limit_screen": None, "provider_error": None, "exit_dialog": None, "login": None,
            "ctx": None, "session_id": None, "cwd": None, "pane_id": None,
            "transcript": None}


def state(name, a=None, *, read_screen=True, screen=None, kv=None):
    """The crew's agent (found by its pane, the name only as a fallback) with its status,
    context and, with `read_screen`, the markers of one screen read; without it those are
    None (unknown, not 0). `screen` is a screen text already read (None: read it here when
    `read_screen`); an empty text is a read screen with no markers."""
    if a is None:
        pane = _kv_of(kv, _kv(name, "pane"))
        listed = herdr_cli.agents()
        a = herdr_cli.agent_by_pane(pane, listed) if pane else None
        if a is None:
            a = next((x for x in listed if x.get("name") == name), None)
    if a is None:
        return gone_state()
    path = _transcript_for(name, a, kv)
    session_id = (a.get("agent_session") or {}).get("value")
    out = {"status": a.get("agent_status"), "name_ok": a.get("name") == name,
           "bg_shells": None, "consent": None, "limit_screen": None, "provider_error": None,
           "exit_dialog": None, "login": None,
           "ctx": _context(session_id, path, _defaults.load()),
           "session_id": session_id, "transcript": path,
           "cwd": a.get("cwd"), "pane_id": a.get("pane_id")}
    if screen is not None or read_screen:
        if screen is None:
            screen = (herdr_cli.agent_visible(a["name"]) if a.get("name")
                      else herdr_cli.pane_visible(a["pane_id"]))
        d = _defaults.load()
        out.update(bg_shells=herdr_cli.bg_shells(screen),
                   consent=herdr_cli.consent_dialog(screen),
                   limit_screen=limits.limit_screen(screen, d),
                   provider_error=limits.provider_error(screen, d),
                   exit_dialog=exit_dialog(screen),
                   login=herdr_cli.not_logged_in(screen))
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


# ---- spend in API dollars -----------------------------------------------------

def _issue_worktree(work_order):
    wt = (journal.issue(work_order) or {}).get("worktree")
    if not wt:
        raise ValueError("worktree of work order %s is unknown" % work_order)
    return wt


def _spend_of(work_order, worktree, d=None, home=None):
    rec = journal.issue(work_order) or {}
    sessions = rec.get("sessions") or []
    d = d or _defaults.load()
    # a main-checkout work order's "worktree" is the site: only its own sessions are its spend
    files = transcript.work_order_files(worktree, sessions, home,
                                        own_only=bool(rec.get("main_checkout")))
    usd = transcript.cost(files, d)["usd"]
    est = journal.estimate(work_order)
    rates = limits.rates(scheduler.readings(), d, time.time(), home=home)
    return {"usd": usd, "estimate_usd": est["usd"],
            "over": est["usd"] is not None and usd > est["usd"],
            "heavy_runs_per_stage": est["heavy_runs_per_stage"],
            "pp_window": limits.to_pp(usd, rates["five_hour"]),
            "pp_week": limits.to_pp(usd, rates["seven_day"]), "rates": rates}


def work_order_spend(work_order, d=None, home=None):
    """Dollars of a work order (every run in its worktree: crew sessions, subagents, asked
    predecessors) against its estimate, in pp of the window and of the week by the rate."""
    return _spend_of(work_order, _issue_worktree(work_order), d, home)


def _spend_block(work_order, spend, d=None):
    """The `Spend:` block of an act: this work order's dollars and the shift's."""
    shift = journal.shift_spend(d or _defaults.load(), time.time())
    mine = {"usd_total": spend["usd"], "estimate_usd": spend["estimate_usd"],
            "over": spend["over"]}
    shift = dict(shift, work_orders=dict(shift.get("work_orders", {}), **{work_order: mine}))
    return journal.spend_block(shift, spend["rates"], work_order)


def accept(work_order, stage, act_text):
    """Accept a stage: the work order's spend goes into the act and the journal. `over`
    means the foreman stops the work order after its current step and raises a gate, unless
    the customer's command "ignore limits and finish" holds."""
    spend = work_order_spend(work_order)
    journal.set_spent(work_order, spend["usd"])
    act = "%s\n\n%s" % (act_text.rstrip(), _spend_block(work_order, spend))
    minutes = journal.accept_stage(stage, act)
    return {"minutes": minutes, "usd": spend["usd"], "estimate_usd": spend["estimate_usd"],
            "over": spend["over"]}


_OPTION_ONE = re.compile(r"^\s*(?:\u276f\s*)?1\.\s")  # the first row of a numbered dialog


_EXIT_RULE = "\u2594"  # the heavy rule above Claude Code's dialogs
_EXIT_FOOTER = "enter to confirm"


def exit_dialog(visible):
    """The dialog Claude Code shows after `/exit`, read in the bottom block only: "worktree"
    (the keep-or-remove question of a `-w` session), "bg_work" (background work is running),
    else None. Words above the bottom block are no dialog, nor are words without the dialog's
    `\u2594` rule or its "Enter to confirm" footer."""
    d = _defaults.load()["screen"]
    low = herdr_cli.bottom_block(visible, d["tail_lines"]).lower()
    if not any(_EXIT_FOOTER in ln or ln.lstrip().startswith(_EXIT_RULE * 3)
               for ln in low.splitlines()):
        return None  # words alone are no dialog: a crew may quote them
    wt = d["worktree_exit"]
    if wt["marker"] in low and any(wt["keep"] in ln for ln in low.splitlines()):
        return "worktree"
    if d["exit_bg_work"] in low and any(_OPTION_ONE.match(ln) for ln in low.splitlines()):
        return "bg_work"
    return None


def _screen_of(pane):
    try:
        return herdr_cli.pane_visible(pane)
    except herdr_cli.HerdrError:
        return ""


def _press(pane, key, why, name):
    """One key into the pane, by the pane (the agent that owns the dialog is leaving), logged
    in the keys log with its outcome. A failed press is not retried: the exit then runs out
    of time."""
    try:
        herdr_cli.run_ack(["pane", "send-keys", pane, key])
    except herdr_cli.HerdrError as e:
        herdr_cli.log_keys(pane, [key], why, crew=name, ok=False, error=str(e))
        return
    herdr_cli.log_keys(pane, [key], why, crew=name)


def _agent_left(pane):
    info = _pane_info(pane)
    return herdr_cli.agent_by_pane(pane) is None and not (info or {}).get("agent")


def _exit(name, pane, timeout_s, poll_s):
    """Exit the crew's agent in `pane`: "exited" | "bg_shells" | "bg_work" | "not_exited".
    With background shells nothing is sent. The worktree exit dialog is answered once with
    `1` (Keep worktree), never Enter and never `2`; the background-work dialog is cancelled
    once with Esc. The kv mark `crew.<name>.exiting` (the monitor's quiet window) is set for
    the exit and cleared here on every result but "exited": the caller clears it after
    its follow-up."""
    mark = _kv(name, "exiting")
    stalled = False
    if not pane:
        return "not_exited"
    screen = _screen_of(pane)
    if herdr_cli.bg_shells(screen) > 0:
        return "bg_shells"
    refused = _screen_refusal(screen, exits=False)  # an exit dialog is answered by the loop below
    if refused:
        return refused  # "/exit" would end in Enter on someone's prompt: nothing is typed
    journal.kv_set(mark, time.time())
    result = "not_exited"
    try:
        if exit_dialog(screen) is None:  # an open exit dialog is answered below, never typed onto
            try:
                stalled = herdr_cli.send(name, "/exit") == "stalled"
            except herdr_cli.HerdrError:
                pass
        deadline = time.monotonic() + timeout_s
        pressed = False
        while True:
            if _agent_left(pane):
                result = "exited"
                break
            dialog = exit_dialog(_screen_of(pane))
            if dialog == "worktree" and not pressed:
                pressed = True
                _press(pane, "1", "worktree_exit", name)
            elif dialog == "bg_work":
                _press(pane, "esc", "bg_work", name)
                result = "bg_work"
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
    finally:
        if result != "exited":
            journal.kv_clear(mark)
    if stalled and result == "not_exited":
        # the typed `/exit` stays in the composer: the next delivery would land behind it
        if (herdr_cli.composer_text(_screen_of(pane)) or "").startswith("/exit"):
            _press(pane, "ctrl+u", "stalled_exit", name)
    return result


# ---- handover, restart ------------------------------------------------------

def request_handover(name):
    if emergency_stop():
        return _STOPPED
    return deliver(name, _template("Handover request"))


def pause(name):
    if emergency_stop():
        return deliver(name, _template("Emergency pause"))
    return deliver(name, _template("Pause"))


def resume(name):
    """After a limit reset or a `--resume`: tell the crew to carry on."""
    if emergency_stop():
        return _STOPPED
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
    herdr_cli.run_ack(["pane", "run", pane, "cd %s" % shlex.quote(worktree)])
    return _wait(lambda: _same_dir(_pane(pane).get("foreground_cwd"), worktree),
                 timeout_s, poll_s)


def _cd_failure(pane, worktree, timeout_s, poll_s):
    """None when the shell in `pane` entered the worktree, else a restart_failed result."""
    try:
        if _enter_worktree(pane, worktree, timeout_s, poll_s):
            return None
        reason = "shell did not enter the worktree"
    except herdr_cli.HerdrError as e:
        reason = str(e)
    return {"status": "restart_failed", "step": "cd", "reason": reason, "pane_id": pane}


def _enter_for_restart(name, work_order, pane, worktree, timeout_s, poll_s):
    """Put the shell in `pane` where the new session starts: (failed result or None, new).
    The worktree normally; once Claude Code has removed it on exit (nothing in it was
    changed) the site, and `new` is the `-w` pair that makes the new session create it again."""
    if os.path.isdir(worktree):
        return _cd_failure(pane, worktree, timeout_s, poll_s), ()
    site = journal.kv_get(_kv(name, "site")) or (journal.issue(work_order) or {}).get("site")
    if not site:
        return {"status": "restart_failed", "step": "cd", "pane_id": pane,
                "reason": "the worktree is gone and the site is unknown"}, ()
    return (_cd_failure(pane, site, timeout_s, poll_s),
            (("-w", "prorab-%s" % work_order),))


def _adopt_new_worktree(name, work_order, worktree, new):
    """After a session started with `-w`: find the worktree it made (and rename its branch),
    record it as `_register_started` does; returns the worktree path the crew works in."""
    if not new:
        return worktree
    site = journal.kv_get(_kv(name, "site")) or (journal.issue(work_order) or {}).get("site")
    found, branch, base_commit = _locate_worktree(site, work_order)
    if not found:
        return worktree
    rec = journal.issue(work_order)
    if rec:
        extra = {"base_commit": base_commit} if base_commit else {}
        journal.issue_set(work_order, rec.get("state", "issued"), worktree=found, **extra)
    for field, value in (("worktree", found), ("branch", branch or ""),
                         ("base_commit", base_commit)):
        if value is not None:
            journal.kv_set(_kv(name, field), value)
    return found


def _crews_work_order(name, work_order):
    """The work order the crew is recorded with (kv), else the one given: a restart under a
    mistyped id must not pick the wrong settings."""
    return journal.kv_get(_kv(name, "work_order")) or work_order


def _settings_failure(name, work_order, pane=None):
    """A `restart_failed` result (step `settings`) when this crew's session cannot get its
    settings (a merge crew with the hooks off), else None. Checked before a crew is exited."""
    try:
        _settings_pairs(_crews_work_order(name, work_order))
    except ValueError as e:
        return {"status": "restart_failed", "step": "settings", "reason": str(e),
                "pane_id": pane}
    return None


def _start_session(name, pane, claude_args, want_screen=False, work_order=None):
    """`agent start` in `pane`. Returns (error result or None, screen text). A consent
    screen maps as in `start`; the screen is read only when it is needed. The settings are
    the shared ones, or the merge crew's own when the work order's record says so; a merge
    crew with the hooks off is not started."""
    failed = _settings_failure(name, work_order, pane)
    if failed:
        return failed, ""
    settings = _settings_pairs(_crews_work_order(name, work_order))
    try:
        herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane]
                      + _AGENT_START_TIMEOUT + _claude_args(*claude_args, *settings),
                      timeout_s=90)
    except herdr_cli.HerdrError as e:
        failed = {"status": "restart_failed", "step": "agent_start", "reason": str(e),
                  "pane_id": pane}
        if e.code not in ("agent_not_ready", "timeout"):
            return failed, ""
        screen = herdr_cli.pane_visible(pane)
        consent = _consent_result(screen, pane)
        if consent:
            return consent, screen
        if e.code == "timeout" and herdr_cli.agent(name):
            # herdr gave up waiting but the agent is there: go on, as `start` would
            return None, screen if want_screen else ""
        return failed, screen
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
    base = _resolve_base(worktree, base, work_order)

    def out(args, empty):
        p = _git_soft(args, worktree)
        if p.returncode != 0:
            return "(git failed: %s)" % (p.stderr.strip() or "exit %d" % p.returncode)
        return p.stdout.strip() or empty
    return "\n".join([
        "## Facts at handover",
        "git status --short:", out(["status", "--short"], "(clean)"), "",
        "git log --oneline %s..HEAD:" % base,
        out(["log", "--oneline", "%s..HEAD" % base], "(no commits)"), "",
        "Open stages (bd show):", _open_stage_statuses(work_order)])


ACT_FILE_MAX = 64 * 1024


def _named_paths(line):
    return re.findall(r"`([^`\n]+)`", line) + re.findall(r"[^\s`]+\.md\b", line)


def _other_words(line):
    """The words of a line besides its named paths."""
    rest = re.sub(r"`[^`\n]+`", " ", line)
    return re.findall(r"\w+", re.sub(r"[^\s`]+\.md\b", " ", rest))


def _is_pointer(message):
    """A last message that names a path and says at most 3 other words on each line."""
    return bool(_named_paths(message)) and all(
        len(_other_words(ln)) <= 3 for ln in message.splitlines() if ln.strip())


def _act_file(message, worktree):
    """The act a pointer message names (see `_is_pointer`): the first
    path in backticks or word ending in .md whose real path (links resolved) lies inside the
    worktree or the work-orders directory and is a regular file of at most ACT_FILE_MAX
    bytes. Else None."""
    roots = [os.path.realpath(r) for r in (worktree, _files_dir())]
    for raw in _named_paths(message):
        path = os.path.expanduser(raw.strip())
        try:
            real = os.path.realpath(path if os.path.isabs(path) else os.path.join(worktree, path))
        except ValueError:  # an embedded NUL
            continue
        if not any(real == r or real.startswith(r + os.sep) for r in roots):
            continue
        try:
            if not os.path.isfile(real) or os.path.getsize(real) > ACT_FILE_MAX:
                continue
            with open(real, encoding="utf-8") as fh:
                text = fh.read(ACT_FILE_MAX + 1).strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return "%s\n\n(read from %s)" % (text, real)
    return None


def complete_handover(name, work_order, work_order_text, timeout_s=60, poll_s=1, base=None):
    if emergency_stop():
        return {"status": _STOPPED}
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
    act = "\n".join(ln for ln in lines if ln.strip() != HANDOVER_MARKER).strip()
    if not act:
        return {"status": "act_missing"}
    if _is_pointer(act):  # a file is the act when one resolves; else the message stays the act
        found = _act_file(act, worktree)
        if found is not None:
            act = found
        elif not any(_other_words(ln) for ln in act.splitlines()):
            return {"status": "act_missing"}  # nothing but paths
    failed = _settings_failure(name, work_order, pane)  # before the act is saved, before /exit
    if failed:
        return failed
    previous = journal.kv_get("handover.%s.last" % work_order)
    try:
        looping = previous is not None and \
            time.time() - float(previous) < _defaults.load()["handover"]["min_interval_s"]
    except ValueError:
        looping = False
    if looping:
        journal.wait_customer(work_order, "handover loop")
        return {"status": "handover_loop"}
    act = "%s\n\n%s" % (act, _facts(worktree, work_order, base))
    spend = _spend_of(work_order, worktree)
    act = "%s\n\n%s" % (act, _spend_block(work_order, spend))
    pending = _kv(name, "handover_act")  # the act is saved once, however often the exit is refused
    if journal.kv_get(pending) is None:
        try:
            journal.add_act(work_order, "handover", act)
        except Exception as e:  # without a saved act the crew must not be exited
            return {"status": "act_not_saved", "reason": str(e)}
        journal.kv_set(pending, "1")

    left = _exit(name, pane, timeout_s, poll_s)
    if left in ("bg_shells", "bg_work"):  # nothing but the saved act is recorded
        return {"status": "bg_shells", "bg_shells": herdr_cli.bg_shells(_screen_of(pane))}
    if left != "exited":
        return {"status": "restart_failed", "step": "exit", "pane_id": pane,
                "reason": left if left.startswith(("consent:", "login", "limit_")) \
                else "agent did not exit"}
    journal.kv_clear(pending)
    journal.kv_set("handover.%s.last" % work_order, time.time())
    journal.set_spent(work_order, spend["usd"])  # the predecessor's dollars are in the worktree
    journal.spend(work_order, sessions=1)
    try:
        return _restart_after_exit(name, work_order, work_order_text, worktree, pane, spend,
                                   timeout_s, poll_s)
    finally:
        journal.kv_clear(_kv(name, "exiting"))


def _restart_after_exit(name, work_order, work_order_text, worktree, pane, spend, timeout_s,
                        poll_s):
    failed, new = _enter_for_restart(name, work_order, pane, worktree, timeout_s, poll_s)
    if failed:
        return failed
    session_id = str(uuid.uuid4())
    failed, _screen = _start_session(
        name, pane, (("--session-id", session_id), ("--add-dir", _files_dir())) + new,
        work_order=work_order)
    if failed:
        return failed
    worktree = _adopt_new_worktree(name, work_order, worktree, new)
    _set_session(name, worktree, session_id, work_order)
    try:
        sent = deliver(name, _handover_text(work_order, work_order_text), work_order)
    except herdr_cli.HerdrError as e:
        return {"status": "prompt_not_accepted", "reason": str(e), "pane_id": pane}
    if sent != "accepted":
        return {"status": "prompt_not_accepted", "sent": sent, "pane_id": pane}
    journal.kv_set(_kv(name, "delivered"), _fingerprint(work_order_text))
    return {"status": "restarted", "sent": sent, "usd": spend["usd"], "pane_id": pane,
            "session_id": session_id}


def restart(name, work_order, work_order_text, reason, caller_pane=None,
            timeout_s=60, poll_s=1):
    """Bring a crew back in its worktree: `--resume` the old session while its context is
    below soft, else a new session with the work order and every handover act. `limit_died`
    (the session ended while waiting for a limit reset) and `login` (the restart stopped at
    a login screen) are not counted as restarts."""
    if emergency_stop():
        return {"status": _STOPPED}
    if reason not in RESTART_REASONS:
        raise ValueError("unknown restart reason %r (expected one of %s)"
                         % (reason, ", ".join(RESTART_REASONS)))
    counted = reason not in UNCOUNTED_REASONS
    if counted:
        n = int(journal.kv_get("restarts.%s" % work_order) or 0) + 1
        if n > _defaults.load()["monitor"]["max_restarts"]:
            journal.wait_customer(work_order, "crew %s was restarted %d times; the customer "
                                  "decides how to go on" % (name, n))
            return {"status": "waits_customer"}
    worktree = journal.kv_get(_kv(name, "worktree"))
    if not worktree:
        return {"status": "restart_failed", "reason": "worktree of the crew is unknown"}
    failed = _settings_failure(name, work_order, journal.kv_get(_kv(name, "pane")))
    if failed:  # a merge crew without its guard is not restarted, and not exited either
        return failed
    try:
        return _restart(name, work_order, work_order_text, worktree, caller_pane,
                        timeout_s, poll_s, counted)
    except herdr_cli.HerdrError as e:  # before the agent was started again: nothing changed
        return {"status": "restart_failed", "reason": str(e)}
    finally:
        journal.kv_clear(_kv(name, "exiting"))
        journal.kv_clear(_kv(name, "handover_act"))  # a later handover saves its own act


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
            left = _exit(a.get("name") or name, pane, timeout_s, poll_s)
            if left != "exited":
                reason = {"bg_shells": "background shells are running",
                          "bg_work": "background work is running"}.get(
                              left, left if left.startswith(("consent:", "login", "limit_"))
                              else "agent did not exit")
                return {"status": "restart_failed", "reason": reason, "step": "exit",
                        "pane_id": pane}
    else:
        if not caller_pane:
            return {"status": "restart_failed", "reason": "pane is gone and no caller pane"}
        try:
            pane, ident = _open_pane(caller_pane, worktree if os.path.isdir(worktree)
                                     else journal.kv_get(_kv(name, "site")) or worktree)
        except herdr_cli.HerdrError as e:
            return {"status": "restart_failed", "step": "split", "reason": str(e),
                    "pane_id": caller_pane}
        journal.kv_set(_kv(name, "pane"), pane)
        journal.kv_set(_kv(name, "pane_identity"), json.dumps(ident))
        rec = journal.issue(work_order)
        if rec:
            journal.issue_set(work_order, rec["state"], pane=pane, pane_identity=ident)
    failed, new = _enter_for_restart(name, work_order, pane, worktree, timeout_s, poll_s)
    if failed:
        return failed

    old = journal.kv_get(_kv(name, "session"))
    path = _transcript_for(name)
    ctx = _context(old, path, _defaults.load())
    files = ("--add-dir", _files_dir())
    if old and not new and not (ctx and ctx["soft"]):  # a new worktree has no session to resume
        failed, screen = _start_session(name, pane, (("--resume", old), files), True,
                                        work_order=work_order)
        marker = _defaults.load()["screen"]["resume_failed"].lower()
        shown = herdr_cli.bottom_block(screen, _defaults.load()["screen"]["tail_lines"]).lower()
        if marker not in shown:
            if failed:
                return failed
            if counted:
                journal.bump_restarts(work_order)
            fp = _fingerprint(work_order_text)
            changed = journal.kv_get(_kv(name, "delivered")) != fp
            try:
                if changed:  # the work order differs from what the crew read: send it again
                    path = message_file(work_order, work_order_text, name=work_order)
                    sent = deliver(name, _template("Changed work order").format(path=path),
                                   work_order)
                else:
                    sent = resume(name)
            except herdr_cli.HerdrError as e:
                return {"status": "prompt_not_accepted", "reason": str(e), "pane_id": pane}
            if sent != "accepted":
                return {"status": "prompt_not_accepted", "sent": sent, "pane_id": pane}
            if changed:
                journal.kv_set(_kv(name, "delivered"), fp)
            return {"status": "resumed", "pane_id": pane, "session_id": old,
                    "work_order_sent": changed}
    session_id = str(uuid.uuid4())
    failed, _screen = _start_session(name, pane, (("--session-id", session_id), files) + new,
                                     work_order=work_order)
    if failed:
        return failed
    worktree = _adopt_new_worktree(name, work_order, worktree, new)
    _set_session(name, worktree, session_id, work_order)
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
    journal.kv_set(_kv(name, "delivered"), _fingerprint(work_order_text))
    return {"status": "restarted", "pane_id": pane, "session_id": session_id}


# ---- git facts --------------------------------------------------------------

def evidence(worktree, base=None, work_order=None):
    base = _resolve_base(worktree, base, work_order)
    branch = _git_soft(["rev-parse", "--abbrev-ref", "HEAD"], worktree).stdout.strip()
    commits = _git_soft(["log", "--oneline", "%s..HEAD" % base], worktree).stdout.splitlines()
    diffstat = _git_soft(["diff", "--stat", "%s...HEAD" % base], worktree).stdout.strip()
    dirty = bool(_git_soft(["status", "--porcelain"], worktree).stdout.strip())
    return {"branch": branch, "commits": commits, "diffstat": diffstat, "dirty": dirty}


def merge_check(worktree, accepted):
    """Read-only: accepted branches not yet in HEAD and the ones that would conflict."""
    missing, conflicts, errors = [], [], []
    for b in accepted:
        if _git_soft(["merge-base", "--is-ancestor", b, "HEAD"], worktree).returncode == 0:
            continue
        missing.append(b)
        rc = _git_soft(["merge-tree", "--write-tree", "HEAD", b], worktree).returncode
        if rc == 1:
            conflicts.append(b)
        elif rc != 0:
            errors.append(b)
    return {"missing": missing, "conflicts": conflicts, "errors": errors}


# ---- answering a dialog -----------------------------------------------------

def _record(work_order, text):
    if work_order:
        journal.add_act(work_order, "answer", text)


def _record_answer(work_order, text):
    """An answer to a crew goes on the stage bead (R56): the work order's current open stage,
    the first in creation order; on the work order itself only when no stage is open."""
    if work_order:
        stages = journal.open_stages(work_order)
        journal.add_act(stages[0] if stages else work_order, "answer", text)


def _status(name):
    a = herdr_cli.agent(name)
    return a.get("agent_status") if a else "gone"


def answer(name, choice=None, text=None):
    """Answer a choice dialog, or send text. A consent prompt returns "consent:<kind>" and a
    limit screen "limit_screen": nothing is sent (`wait_limit` is the only way to act on a
    limit screen). Without a dialog the text goes through `deliver`. The answer is recorded
    as an `answer` act on the current open stage (`_record_answer`)."""
    if emergency_stop():
        return _STOPPED
    work_order = journal.kv_get(_kv(name, "work_order"))
    visible = herdr_cli.agent_visible(name)
    # logging in is the customer's; the script that sent /exit answers its dialog, never a
    # work order: nothing is sent or pressed
    refused = _screen_refusal(visible)
    if refused:
        return refused
    dialog = herdr_cli.choice_state(visible)
    if dialog is None:
        if text is None:
            raise ValueError("no dialog is open: text is required")
        result = deliver(name, text, work_order)
        if result == "accepted":  # anything else was not delivered: nothing to record
            _record_answer(work_order, "answer to crew %s: %s" % (name, text))
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
        herdr_cli.send_keys(name, keys + ["enter"], "choice")
        _record_answer(work_order, "choice for crew %s: %d. %s" % (name, choice, labels[target]))
    else:
        if keys:
            herdr_cli.send_keys(name, keys, "free_text")
        pane = (herdr_cli.agent(name) or {}).get("pane_id") or journal.kv_get(_kv(name, "pane"))
        herdr_cli.run_ack(["pane", "send-text", pane, text])
        herdr_cli.send_keys(name, ["enter"], "free_text")
        _record_answer(work_order, "answer to crew %s (free text): %s" % (name, text))
    return _status(name)


def wait_limit(name):
    """On a limit screen: choose "Stop and wait" (never "Upgrade") and note the reset time.
    Moves first, re-reads the screen, and presses Enter alone only when "Stop and wait" is
    highlighted; otherwise "unknown_limit_dialog". Never counts as a restart."""
    if emergency_stop():
        return {"status": _STOPPED}
    d = _defaults.load()
    visible = herdr_cli.agent_visible(name)
    found = limits.limit_screen(visible, d)
    if found is None:
        return {"status": "no_limit_screen"}
    if found["kind"] == "limit_choice":
        delta = found["wait_option"] - found["highlighted"]
        keys = ["down"] * delta if delta > 0 else ["up"] * -delta
        if keys:
            herdr_cli.send_keys(name, keys, "limit_choice")
        # Enter only after the screen shows "Stop and wait" highlighted: a move that landed
        # elsewhere must never confirm "Upgrade"
        again = limits.limit_screen(herdr_cli.agent_visible(name), d)
        wait = d["limits"]["limit_choice"]["wait"].lower()
        if (again is None or again["kind"] != "limit_choice"
                or again["highlighted"] != again["wait_option"]
                or not again["options"][again["highlighted"]].lower().startswith(wait)):
            return {"status": "unknown_limit_dialog"}
        herdr_cli.send_keys(name, ["enter"], "limit_choice")
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

def _close_crew_pane(name):
    """Close the crew's own pane (kv `pane`) after its agent left: only when it is still the
    pane the crew was opened in (identity), shows a working directory and holds no agent,
    the crew's or another. Best-effort; returns whether the pane was closed."""
    pane = journal.kv_get(_kv(name, "pane"))
    if not pane:
        return False
    try:
        rec = json.loads(journal.kv_get(_kv(name, "pane_identity")) or "{}")
    except ValueError:
        return False
    info = _pane_info(pane)
    if not isinstance(rec, dict) or not _same_pane(rec, info):
        return False
    if _has_agent(pane, info) or not info.get("foreground_cwd"):
        return False
    return _close_pane(pane)


def _close_work_order(work_order, reason):
    """The fields a result gains from closing the work order; a refused close is reported,
    never raised (the effects before it are already done)."""
    try:
        closed = journal.close_work_order(work_order, reason) or {}
    except JournalError as e:
        return {"work_order_closed": False, "close_error": str(e),
                "closed": list(e.closed or [])}  # the beads closed before the failure
    return {"work_order_closed": "already" if closed.get("already") else True}


def _ask_end_shells(name, work_order, path):
    """Ask the crew to end its background shells: the `End background shells` line, once per
    call; not again while it is the crew's last delivered text and the crew has not worked
    since ("already_asked")."""
    line = _template("End background shells")
    text, worked = transcript.last_delivered(path) if path else (None, False)
    if text is not None and text.strip() == line.strip() and not worked:
        return "already_asked"
    return deliver(name, line, work_order)


def stop(name, work_order, timeout_s=60, poll_s=1):
    """Exit the crew and unregister it: {"status": "stopped", ...}. When the agent does not
    leave ("bg_shells", "bg_work", "not_exited") the result is {"status": <that>}; with shells or
    background work the crew is asked to end them ({"shells": <n>, "asked": <delivery>}). The
    crew stays registered with its kv in place: nothing is unregistered while its agent runs."""
    if emergency_stop():
        return {"status": _STOPPED}
    a = herdr_cli.agent(name)
    tokens = _tokens_of(a, name)
    worktree = journal.kv_get(_kv(name, "worktree")) or \
        (journal.issue(work_order) or {}).get("worktree")
    if a is not None:
        pane = a.get("pane_id") or journal.kv_get(_kv(name, "pane"))
        left = _exit(name, pane, timeout_s, poll_s)
        if left in ("bg_shells", "bg_work"):
            return {"status": left, "shells": herdr_cli.bg_shells(_screen_of(pane)),
                    "asked": _ask_end_shells(name, work_order, _transcript_for(name, a))}
        if left != "exited":
            return {"status": left}
    journal.unregister_crew(name)
    if worktree:
        journal.set_spent(work_order, _spend_of(work_order, worktree)["usd"])
    journal.spend(work_order, tokens=tokens)
    pane_closed = _close_crew_pane(name)
    close_start_panes(work_order)
    for field in ("work_order", "site", "pane", "pane_identity", "worktree", "branch", "session",
                  "transcript", "limit_until", "exiting", "handover_act", "delivered"):
        journal.kv_clear(_kv(name, field))
    res = {"status": "stopped", "tokens": tokens, "pane_closed": pane_closed,
           "work_order_closed": False}
    if not journal.open_stages(work_order) and not journal.has_open_gate(work_order):
        res.update(_close_work_order(work_order, "every stage accepted or closed"
                                     if journal.all_stages(work_order) else "no stages"))
    return res


def _default_branch(site_path):
    for b in ("main", "master"):
        if _git_soft(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % b], site_path).returncode == 0:
            return b
    return _git_soft(["rev-parse", "--abbrev-ref", "HEAD"], site_path).stdout.strip()


def _is_merged(site_path, branch):
    out = _git_soft(["branch", "--merged", _default_branch(site_path)], site_path).stdout
    return branch in [ln.strip().lstrip("*+ ").strip() for ln in out.splitlines()]


def _gone_branch(site_path, work_order):
    """The work order's branch when its worktree is gone (Claude Code removes a no-change
    worktree on exit and leaves the branch): the recorded one, else `prorab/<id>`, else
    `worktree-prorab-<id>`, the first that exists. Never the default branch or one checked
    out anywhere."""
    default = _default_branch(site_path)
    checked_out = {w["branch"] for w in _worktrees(site_path)}
    own = ("prorab/%s" % work_order, "worktree-prorab-%s" % work_order)
    recorded = (journal.issue(work_order) or {}).get("branch")   # only a name of its own counts
    for b in ((recorded,) if recorded in own else ()) + own:
        if not b or b in ("main", "master", default) or b in checked_out:
            continue
        if _git_soft(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % b],
                     site_path).returncode == 0:
            return b
    return None


def _cleanup_gone_worktree(site_path, work_order, dropped):
    """`cleanup` for a work order without a worktree: its branch is removed when merged (or
    dropped by the customer) and the work order closed; None when there is no branch."""
    _git_soft(["worktree", "prune"], site_path)
    branch = _gone_branch(site_path, work_order)
    if branch is None:
        return None
    # "merged" needs a default branch to be merged into: without main or master nothing is
    # decided against whatever happens to be checked out
    named = any(_git_soft(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % b],
                          site_path).returncode == 0 for b in ("main", "master"))
    merged = named and _is_merged(site_path, branch)
    if not (merged or dropped):
        return {"status": "kept", "reason": "unmerged", "work_order_closed": False}
    p = _git_soft(["branch", "-D" if dropped and not merged else "-d", branch], site_path)
    if p.returncode != 0:
        return {"status": "kept", "reason": "branch", "detail": p.stderr.strip(),
                "work_order_closed": False}
    rec = journal.issue(work_order)
    if rec:
        journal.issue_set(work_order, rec.get("state") or "issued",
                          decided="merged" if merged else "dropped")
    res = {"status": "removed", "worktree": None, "branch": branch}
    res.update(_close_work_order(
        work_order, "the branch merged" if merged else "dropped by the customer"))
    return res


def cleanup(site_path, work_order):
    if emergency_stop():
        return {"status": _STOPPED}
    if work_order in journal.working_work_orders():   # a live crew's branch is not merged work
        return {"status": "kept", "reason": "crew_running", "work_order_closed": False}
    close_start_panes(work_order)
    for key in list(journal.kv_all()):   # a dropped work order leaves no consent watch behind
        if key.startswith("consent.%s." % work_order):
            journal.kv_clear(key)
    # a main-checkout work order has no worktree and no branch of its own: nothing to remove
    main_checkout = (journal.issue(work_order) or {}).get("main_checkout")
    wt = None if main_checkout else _find_worktree(site_path, work_order)
    dropped = any(a.startswith(DROP_MARKER) for a in journal.acts(work_order, "answer"))
    if wt is not None and not os.path.isdir(wt["path"]):
        wt = None   # git still lists a directory that is gone: the prune below forgets it
    if wt is None:
        if not main_checkout:
            res = _cleanup_gone_worktree(site_path, work_order, dropped)
            if res is not None:
                return res
        res = {"status": "absent", "work_order_closed": False}
        if dropped:
            res.update(_close_work_order(work_order, "dropped by the customer"))
        return res
    branch = wt["branch"]
    merged = bool(branch) and _is_merged(site_path, branch)
    if not (merged or dropped):
        return {"status": "kept", "reason": "unmerged", "work_order_closed": False}
    rm = ["worktree", "remove"] + (["--force"] if dropped and not merged else []) + [wt["path"]]
    p = _git_soft(rm, site_path)
    if p.returncode != 0:
        return {"status": "kept", "reason": "dirty", "detail": p.stderr.strip(),
                "work_order_closed": False}
    if branch:
        _git_soft(["branch", "-D" if dropped and not merged else "-d", branch], site_path)
    decided = "merged" if merged else "dropped"
    rec = journal.issue(work_order)
    if rec:
        journal.issue_set(work_order, rec.get("state") or "issued", decided=decided)
    res = {"status": "removed", "worktree": wt["path"], "branch": branch}
    res.update(_close_work_order(
        work_order, "the branch merged" if merged else "dropped by the customer"))
    return res


# ---- stage report check -------------------------------------------------------

_CRITERION_ID = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(?:\|[ \t]*)?\**(C\d+)\b", re.M)
_LIVE_CRITERION = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?(?:\|[ \t]*)?\**(C\d+)\**[ \t]*\(live\)", re.M | re.I)
_CID = re.compile(r"(?<![\w./\\-])C\d+\b(?![./\\-]?\w)")   # a whole token, not `src/C3.py`
_HEADING = re.compile(r"^ {0,3}#{1,4}\s+(.+?)\s*#*\s*$")
_ROW = re.compile(r"^\s*\|\s*(C\d+)\s*\|\s*([^|]*?)\s*(?:\|\s*([^|]*?)\s*)?\|?\s*$")
_RESULT = re.compile(r"^[\s>*_-]*Result[\s*_]*:[\s*_]*(.+?)[\s*_`.]*$", re.I | re.M)
_COMMIT = re.compile(r"^[\s>*_-]*Commit[\s*_]*:[\s*_`]*([0-9a-f]{7,40})\b", re.I | re.M)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)(?:\s+(.*))?$")
_TICKS = re.compile(r"`([^`]+)`")
_SKIP = re.compile(r"\bSKIP:\s*(\d+)", re.I)
_HEAVY = re.compile(r"^[\s>*_-]*Heavy runs[\s*_]*:[\s*_`]*(\d+)\b", re.I | re.M)
_GUARD = re.compile(r"^[\s>*_-]*Guard refusals[\s*_]*:[\s*_`]*(\d+)\b", re.I | re.M)


def criteria_ids(work_order_text):
    """The `C<n>` criterion ids at line starts of a work order, in order."""
    out = []
    for cid in _CRITERION_ID.findall(work_order_text):
        if cid not in out:
            out.append(cid)
    return out


def live_criteria(work_order_text):
    """The `C<n>` ids marked `(live)` right after the id: `- C2 (live): ...`, `| C2 (live) |`."""
    return [m.group(1) for m in _LIVE_CRITERION.finditer(work_order_text)]


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


def _blocks(lines):
    """List items with their continuation: lines indented deeper than the item's marker
    (sub-bullets, wrapped text, blank lines between them) belong to it; the next line at its
    indent or lower ends it. ``[[first line, continuation lines...]]``, markers stripped."""
    out, indent = [], None
    for ln in lines:
        m = _ITEM.match(ln)
        ln = ln.expandtabs(4)
        depth = len(ln) - len(ln.lstrip())
        if indent is not None and ln.strip() and depth > indent:
            out[-1].append(((m.group(1) or "") if m else ln).strip())
        elif m:
            out.append([(m.group(1) or "").strip()])
            indent = depth
        elif ln.strip():
            indent = None
    return out


def _listed_path(item):
    ticks = _TICKS.search(item)
    return (ticks.group(1) if ticks else item.split()[0]).strip()


def _covered(path, files):
    return any(path.startswith(f) if f.endswith("/") else fnmatch.fnmatch(path, f)
               for f in files)


def _check_heavy(text, heavy_limit, cleared, heavy_counted, unverifiable=None):
    m = _HEAVY.search(text)
    if not m:
        return ["no Heavy runs line"]
    n = int(m.group(1))
    problems = []
    if unverifiable:  # acceptance is by evidence, never by the crew's word (R52)
        problems.append("heavy runs not verifiable: %s" % unverifiable)
    if heavy_counted is not None and n != heavy_counted:
        problems.append("Heavy runs: reported %d, the slots log counts %d" % (n, heavy_counted))
    worst = n if heavy_counted is None else max(n, heavy_counted)
    if heavy_limit is not None and worst > heavy_limit and not cleared:
        problems.append("Heavy runs: %d above the limit of %d per stage" % (worst, heavy_limit))
    return problems


def _check_guard(text, counted, unverifiable=None):
    """The reported refusals of Claude Code's worktree guard against the transcript's count."""
    m = _GUARD.search(text)
    if not m:
        return ["no Guard refusals line"], []
    n = int(m.group(1))
    problems, warnings = [], []
    if unverifiable:  # by evidence, never by the crew's word
        problems.append("guard refusals not verifiable: %s" % unverifiable)
    elif counted is not None and n != counted:
        problems.append("Guard refusals: reported %d, the transcript counts %d" % (n, counted))
    if max(n, counted or 0) > 0:
        warnings.append("%d refusals of the worktree guard; read each under Residual risks "
                        "for what the crew did then" % max(n, counted or 0))
    return problems, warnings


def _live_field(item, cid):
    """True when a line of the Verification item (a block: see `_blocks`) ends in the field
    `LIVE <cid>`: after a ` - ` on its first line, alone or after one on a later line."""
    lines = [item] if isinstance(item, str) else item
    for i, line in enumerate(lines):
        fields = line.split(" - ")
        if (len(fields) > 1 or i > 0) and fields[-1].strip(" *_`.") == "LIVE %s" % cid:
            return True
    return False


def _placeholder(item):
    """A template placeholder left in a report: `<...>` as the whole item."""
    item = item.strip(" *_`")
    return item.startswith("<") and item.endswith(">")


def _check_passport(sections):
    """`(problems, notes)` of the section "For the passport": the single item `none`, or the
    notes a newcomer to the site would trip over, which the foreman writes into the passport."""
    if "For the passport" not in sections:
        return [], []  # "missing section" is already a problem
    lines = sections["For the passport"]
    items = _items(lines)
    bare = " ".join(ln.strip() for ln in lines if ln.strip())
    if not items and bare.strip(" *_`.").lower() == "none":
        items = ["none"]
    if not items:
        return ["For the passport: write none or list each note"], []
    problems = ["For the passport: a template placeholder, not a note: %s" % it
                for it in items if _placeholder(it)]
    return problems, [it for it in items
                      if it.strip(" *_`.").lower() != "none" and not _placeholder(it)]


def _check_stubs(sections, rows, criteria, live):
    """No stub passes as working: the Stubs section against the criteria table, and a `(live)`
    criterion's `done` against a `LIVE C<n>` item under Verification."""
    problems, backed = [], {}
    if "Stubs" in sections:
        lines = sections["Stubs"]
        items = _items(lines)
        bare = " ".join(ln.strip() for ln in lines if ln.strip())
        if not items and bare.strip(" *_`.").lower() == "none":
            items = ["none"]
        if not items:
            problems.append("Stubs: write none or list each stub")
        elif not (len(items) == 1 and items[0].strip(" *_`.").lower() == "none"):
            for item in items:
                if _placeholder(item):
                    problems.append("Stubs: a template placeholder, not a stub: %s" % item)
                    continue
                ids = _CID.findall(item)
                if not ids:
                    problems.append("stub item names no criterion: %s" % item)
                if any(cid not in criteria for cid in ids):
                    problems.append("stub item names an unknown criterion: %s" % item)
                for cid in ids:
                    backed.setdefault(cid, item)
    for cid in criteria:
        status = rows[cid][0] if cid in rows else None
        if status == "done" and cid in backed:
            problems.append("criterion %s: done but backed by a stub: %s" % (cid, backed[cid]))
        elif status == "stub" and cid not in backed and "Stubs" in sections:
            problems.append("criterion %s: status stub but no Stubs item names it" % cid)
    checks = _blocks(sections.get("Verification", []))
    for cid in live:
        if cid in rows and rows[cid][0] == "done" and not any(
                _live_field(it, cid) for it in checks):
            problems.append("criterion %s (live): done without a LIVE %s check under "
                            "Verification" % (cid, cid))
    return problems


def check_report(text, criteria, files, worktree, base=None, heavy_limit=None, cleared=False,
                 heavy_counted=None, unverifiable=None, live=(), work_order=None,
                 guard_counted=None, guard_unverifiable=None):
    """Read-only check of a stage report against its work order and the worktree's git.
    `work_order` finds the recorded base of a main-checkout work order (see `_recorded_base`);
    `guard_counted` is the transcript's count of the worktree guard's refusals in the stage."""
    problems, warnings = [], []
    problems += _check_heavy(text, heavy_limit, cleared, heavy_counted, unverifiable)
    more, notes = _check_guard(text, guard_counted, guard_unverifiable)
    problems += more
    warnings += notes
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
        elif status == "stub":
            warnings.append("criterion %s: stub — accept only if the work order allows it" % cid)
    problems += _check_stubs(sections, rows, criteria, live)
    more, passport_notes = _check_passport(sections)
    problems += more
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
    for block in _blocks(sections.get("Verification", [])):
        item = " ".join(block)  # the command, SKIP: <n> and LIVE are looked for in the whole block
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
        problems += _check_delivery(sha, changed_items, files, worktree, base, work_order)
    return {"ok": not problems, "problems": problems, "warnings": warnings,
            "outcome": outcome, "sha": sha, "passport_notes": passport_notes}


def _check_delivery(sha, changed_items, files, worktree, base, work_order=None):
    """Git facts for a `delivered` report: problems only; any git failure is a refusal."""
    base = _resolve_base(worktree, base, work_order)
    if not sha:
        return ["delivered: no Commit SHA"]

    def failed(p):
        return ["delivered: git failed: %s" % (p.stderr.strip() or "exit %d" % p.returncode)]
    problems = []
    p = _git_soft(["merge-base", "--is-ancestor", sha, "HEAD"], worktree)
    if p.returncode == 1:
        problems.append("delivered: commit %s is not an ancestor of HEAD" % sha)
    elif p.returncode != 0:
        return failed(p)
    p = _git_soft(["rev-list", "%s..HEAD" % base], worktree)
    if p.returncode != 0:
        return problems + failed(p)
    branch_commits = p.stdout.split()
    if not branch_commits:
        problems.append("delivered: no commit in %s..HEAD" % base)
    else:
        full = _git_soft(["rev-parse", "--verify", "--quiet", "%s^{commit}" % sha], worktree)
        if full.returncode != 0:
            return problems + failed(full)
        if full.stdout.strip() not in branch_commits:
            problems.append("delivered: commit %s is not in %s..HEAD (not on the crew's branch)"
                            % (sha, base))
    p = _git_soft(["diff", "--name-only", "%s...HEAD" % base], worktree)
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
    rec = (journal.issue(work_order) or {}) if work_order else {}
    if rec.get("main_checkout"):
        problems += _check_merge_only(rec.get("merge_branch"), base, worktree)
    return problems


def _check_merge_only(branch, base, worktree):
    """The backstop of the merge crew's string guard, in git: since `base`, the first-parent
    chain of the main checkout is exactly one merge commit (first parent the base, second
    parent the tip of `branch`), or a fast-forward to exactly that tip."""
    if not branch:
        return ["main-checkout work order without a recorded merge branch"]
    moved = ["main moved by more than the merge of %s" % branch]

    def rev(*args):
        p = _git_soft(list(args), worktree)
        return p.stdout.split() if p.returncode == 0 else None
    tip = rev("rev-parse", "--verify", "--quiet", "refs/heads/%s^{commit}" % branch)
    start = rev("rev-parse", "--verify", "--quiet", "%s^{commit}" % base)
    head = rev("rev-parse", "HEAD")
    chain = rev("rev-list", "--first-parent", "%s..HEAD" % base)
    if not (tip and start and head and chain is not None):
        return ["cannot read main and %s to check that only the merge moved it" % branch]
    tip, start, head = tip[0], start[0], head[0]
    if head == tip:
        ancestor = _git_soft(["merge-base", "--is-ancestor", start, tip], worktree)
        return [] if ancestor.returncode == 0 else moved
    if len(chain) == 1:
        parents = rev("rev-list", "--parents", "-n", "1", chain[0])
        if parents and len(parents) == 3 and parents[1] == start and parents[2] == tip:
            return []
    return moved


def report_fix(name, work_order, stage, problems):
    """Ask the crew to fix a failed stage report; the 4th request opens a gate."""
    if emergency_stop():
        return {"status": _STOPPED}
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
    changed. Costs one run of the work order; `cwd` must lie in its worktree, so the run's
    transcript counts in the work order's dollars. Refused under the emergency stop."""
    if emergency_stop():
        return _STOPPED
    if not _under(cwd, _issue_worktree(work_order)):
        raise ValueError("cwd %s is outside the worktree of work order %s" % (cwd, work_order))
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


def _check_report_cli(name, work_order, stage=None, base=None):
    worktree = journal.kv_get(_kv(name, "worktree"))
    if not worktree:
        raise ValueError("worktree of the crew %s is unknown" % name)
    shown = journal.bd(["show", work_order])
    description = (shown[0].get("description") or "") if shown else ""
    cleared, counted, guard_counted = False, None, None
    missing = guard_missing = "no stage given (--stage)" if not stage else None
    if stage:
        labels = ((journal.bd(["show", stage]) or [{}])[0].get("labels")) or []
        cleared = "clearance:granted" in labels
        started = journal.stage_info(stage)["started_at"]
        if started is not None:
            counted = slots.runs("heavy", worktree, since=float(started))
            # every session of the work order (a restarted crew has several), the crew's own
            # files only: the work order's template says the count excludes its subagents
            sessions = (journal.issue(work_order) or {}).get("sessions") or []
            paths = transcript.session_paths(worktree, sessions)
            if not paths and _transcript_for(name):
                paths = [_transcript_for(name)]
            try:
                if not paths:
                    raise OSError("no transcript of the crew")
                guard_counted = sum(transcript.guard_refusals(p, since=float(started))
                                    for p in paths)
            except OSError as e:
                guard_missing = "transcript unreadable (%s)" % e
        else:
            missing = guard_missing = "stage %s has no start time" % stage
    return check_report(summary(name), criteria_ids(description),
                        journal.work_order_files(work_order), worktree, base,
                        heavy_limit=journal.estimate(work_order)["heavy_runs_per_stage"],
                        cleared=cleared, heavy_counted=counted, unverifiable=missing,
                        live=live_criteria(description), work_order=work_order,
                        guard_counted=guard_counted, guard_unverifiable=guard_missing)


def _accept_cli(work_order, stage, act_file):
    with open(act_file, encoding="utf-8") as fh:
        return accept(work_order, stage, fh.read())


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

    cmd("start", "site_path", "work_order", "name", "caller_pane",
        main_checkout={"action": "store_true"})
    cmd("new_site", "site_path", "work_order", "caller_pane")
    cmd("name_for", "site", "n")
    cmd("send_work_order", "name", "text", work_order={})
    cmd("state", "name")
    cmd("summary", "name")
    cmd("request_handover", "name")
    cmd("complete_handover", "name", "work_order", "work_order_text")
    cmd("evidence", "worktree", base={}, work_order={})
    cmd("merge_check", "worktree", accepted={"default": ""})
    cmd("answer", "name", choice={"type": int}, text={})
    cmd("pause", "name")
    cmd("resume", "name")
    cmd("wait_limit", "name")
    cmd("restart", "name", "work_order", "work_order_text", "reason", caller_pane={})
    cmd("check_report", "name", "work_order", stage={}, base={})
    cmd("spend", "work_order")
    cmd("accept", "work_order", "stage", act_file={"required": True})
    cmd("report_fix", "name", "work_order", "stage", problems_file={"required": True})
    cmd("ask_predecessor", "work_order", "session_id", "cwd", "question")
    cmd("stop", "name", "work_order")
    cmd("cleanup", "site_path", "work_order")
    args = vars(ap.parse_args(_defaults.cli_argv(sys.argv[1:] if argv is None else argv)))
    c = args.pop("cmd")
    if c == "merge_check":
        args["accepted"] = [x for x in args["accepted"].split(",") if x]
    try:
        if c == "name_for":
            args["n"] = int(args["n"])
        func = {"check_report": _check_report_cli, "report_fix": _report_fix_cli,
                "accept": _accept_cli, "spend": work_order_spend}.get(c)
        result = (func or globals()[c])(**args)
    except (herdr_cli.HerdrError, journal.JournalError, ValueError, RuntimeError,
            OSError, KeyError) as e:
        print(json.dumps({"error": str(e), "code": getattr(e, "code", None)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
