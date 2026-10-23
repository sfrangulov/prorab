#!/usr/bin/env python3
"""Crew hooks: the settings file a crew session is started with, the guard and the event tap.

`settings` writes the crew settings file (in the cache, never in the repo or the customer's
config); `guard` is the PreToolUse hook of Bash calls; `tap` records Stop, Notification,
PreCompact and SessionStart events for the monitor. Hooks only forbid and record: they never
answer a consent prompt. The emergency-stop check of every tool is a one-line shell test in
the settings file itself, so it starts no Python.
"""
import argparse
import json
import os
import re
import shlex
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402

STOP_REASON = ("prorab emergency stop: the customer stopped all crews. Do not retry; end your "
               "turn and wait for the foreman.")
TAP_EVENTS = ("Stop", "Notification", "PreCompact", "SessionStart")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MERGE = re.compile(r"\bgit\b.*\b(?:merge|rebase)\b")
_HEX_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
# the branch a merge crew may merge (the same shape `crew.py start --main-checkout` accepts)
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_OFFICE_FORMS = ("~/projects/prorab/.beads", "$HOME/projects/prorab/.beads",
                 "${HOME}/projects/prorab/.beads")


def safe_session_id(value):
    """Whether `value` can be a file name of the events directory."""
    return isinstance(value, str) and bool(_SAFE_ID.match(value))


# ---- the settings file ----------------------------------------------------------------

def _command(*words):
    return "python3 -I " + " ".join(shlex.quote(w) for w in words)


def _stop_check(stop_path):
    return "[ ! -e %s ] || { echo %s >&2; exit 2; }" % (
        shlex.quote(stop_path), shlex.quote(STOP_REASON))


def _merge_branch_checked(branch):
    if not isinstance(branch, str) or not _BRANCH.match(branch):
        raise ValueError("merge branch %r is not a plain branch name" % (branch,))
    return branch


def settings_dict(d=None, merge_branch=None):
    """The crew settings: the hooks, no survey, and the suggestions setting. Nothing about
    permissions or MCP servers. With `merge_branch` the guard runs as `guard --merge-branch
    BRANCH` (the settings of a main-checkout merge crew); otherwise nothing differs."""
    d = d or _defaults.load()
    guard_words = ["guard"]
    if merge_branch is not None:
        guard_words += ["--merge-branch", _merge_branch_checked(merge_branch)]
    script = os.path.abspath(__file__)

    def entry(kind, matcher=None, *more):
        item = {"hooks": [{"type": "command", "command": _command(script, kind, *more)}]}
        if matcher:
            item["matcher"] = matcher
        return item
    stop_check = {"matcher": "*", "hooks": [
        {"type": "command", "command": _stop_check(d["emergency"]["stop_path"])}]}
    hooks = {"PreToolUse": [stop_check, entry("guard", "Bash", *guard_words[1:])]}
    for event in TAP_EVENTS:
        hooks[event] = [entry("tap")]
    out = {"hooks": hooks, "feedbackSurveyRate": 0}
    out.update(d["hooks"]["suggestions_setting"])
    return out


def write_settings(d=None, merge_branch=None, work_order=None):
    """Write `hooks.settings_path` atomically (mode 0600, its directory 0700); returns it.
    With `merge_branch` and `work_order` (both or neither) write the settings of that work
    order's merge crew to `crew-settings-merge-<work order>.json` beside it instead; the shared
    file is not touched."""
    d = d or _defaults.load()
    path = d["hooks"]["settings_path"]
    folder = os.path.dirname(path)
    if (merge_branch is None) != (work_order is None):
        raise ValueError("a merge crew's settings need both the merge branch and the work order")
    if merge_branch is not None:
        if not safe_session_id(work_order):
            raise ValueError("work order %r cannot be used in a file name" % (work_order,))
        _merge_branch_checked(merge_branch)
        path = os.path.join(folder, "crew-settings-merge-%s.json" % work_order)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings_dict(d, merge_branch), fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


# ---- the guard ------------------------------------------------------------------------

def _office_journal_forms():
    forms = list(_OFFICE_FORMS)
    forms.append(os.path.expanduser("~/projects/prorab/.beads"))
    forms.append(os.environ.get("PRORAB_BEADS_DIR") or "")
    return [f for f in forms if f]


def _dash_c_dir(command):
    """The directory of the last `-C <dir>` in the command, else None."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    found = None
    for i, w in enumerate(words[:-1]):
        if w == "-C":
            found = words[i + 1]
    return found


def _find_dot_git(start):
    """The first `.git` found from `start` upward, or None."""
    cur = os.path.abspath(start)
    while True:
        cand = os.path.join(cur, ".git")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def checked_out_branch(start):
    """The branch checked out at `start` (read from files, no git process): its name, "" for
    a detached HEAD, None when it cannot be told."""
    dot_git = _find_dot_git(start)
    if dot_git is None:
        return None
    try:
        git_dir = dot_git
        if os.path.isfile(dot_git):
            with open(dot_git, encoding="utf-8") as fh:
                first = next((ln for ln in fh.read().splitlines()
                              if ln.startswith("gitdir:")), None)
            if first is None:
                return None
            git_dir = first[len("gitdir:"):].strip()
            if not os.path.isabs(git_dir):
                git_dir = os.path.join(os.path.dirname(dot_git), git_dir)
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
    except (OSError, ValueError):
        return None
    if head.startswith("ref: refs/heads/") and len(head) > len("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    if _HEX_ID.match(head):
        return ""
    return None


# ---- the guard of a main-checkout merge crew ---------------------------------------------
# `guard --merge-branch BRANCH`: in a checkout on main/master of such a crew git is allowed
# only as the one merge it was started for, `git merge --abort` and read-only subcommands.

_GIT_WORD = re.compile(r"(?<![\w.-])git(?![\w.-])")
_READ_ONLY_GIT = ("status", "log", "diff", "show", "rev-parse", "merge-base", "ls-files")
_MERGE_FLAGS = ("--no-ff", "--ff-only")


def _segments(command):
    """The command as a list of simple commands (token lists), split at `;`, `&&`, `||`, `|`,
    `&`, newlines and parentheses; None when it cannot be read. A redirection is kept as a
    token holding `<` or `>`."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    out, cur = [], []
    for t in tokens:
        if t and all(c in ";&|()" for c in t):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _is_git(token):
    return token == "git" or token.endswith("/git")


def _same_checkout(a, b):
    ga, gb = _find_dot_git(a), _find_dot_git(b)
    return ga is not None and gb is not None and os.path.realpath(ga) == os.path.realpath(gb)


def _merge_crew_git(tokens, cwd):
    """None when this git command is allowed, else the reason. `tokens[0]` is git."""
    words, target, i = tokens[1:], None, 0
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "-C" and target is None and i + 1 < len(words):
            target = words[i + 1]
            i += 2
        elif words[i] == "--no-pager":
            i += 1
        else:
            return "git option %s" % words[i]
    if target is not None:
        target = os.path.expanduser(target)
        target = target if os.path.isabs(target) else os.path.join(cwd, target)
        if not _same_checkout(target, cwd):
            return "git -C outside this checkout"
    words = words[i:]
    if not words:
        return "a bare git command"
    sub, rest = words[0], words[1:]
    if sub in _READ_ONLY_GIT:
        if any(w.startswith("--output") for w in rest):
            return "git %s --output" % sub
        return None
    if sub == "branch":
        return None if all(w in ("--list", "--show-current") for w in rest) \
            else "git branch with arguments"
    return "git %s" % sub


def _merge_crew_forms_ok(tokens, branch):
    """Whether a `git ... merge` segment is exactly an allowed merge form."""
    rest = tokens
    if rest == ["--abort"]:
        return True
    if rest and rest[0] in _MERGE_FLAGS:
        rest = rest[1:]
    if rest and rest[0] == "--no-edit":
        rest = rest[1:]
    return rest == [branch]


def _merge_crew_verdict(command, cwd, branch):
    """None when the checkout is not main/master (the ordinary rules apply), else
    `(0, "")` or the refusal (2, message)."""
    def refuse(what):
        return 2, ("prorab guard: %s is forbidden in the main checkout of a merge crew; "
                   "ask the foreman" % what)
    checked_out = checked_out_branch(cwd)
    if checked_out is None:
        return 2, "prorab guard: cannot tell the checked-out branch; ask the foreman"
    if checked_out not in ("main", "master"):
        return None
    if re.search(r"\$\(|`|<\(|>\(", command):
        return refuse("a command substitution around git")
    segments = _segments(command)
    if segments is None:
        return refuse("a command that cannot be read")
    merges = 0
    for tokens in segments:
        if not _is_git(tokens[0]):
            if any(_GIT_WORD.search(t) for t in tokens):
                return refuse("git run through another command")
            continue
        if any(re.search(r"[<>]", t) for t in tokens):
            return refuse("git with a redirection")
        words, i = tokens[1:], 0
        # a merge is `git [-C dir] merge ...`; find the subcommand the way _merge_crew_git does
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] == "-C" else 1
        sub = words[i] if i < len(words) else None
        if sub == "merge":
            merges += 1
            problem = _merge_crew_git(tokens[:1] + words[:i] + ["status"], cwd)
            if problem is None and not _merge_crew_forms_ok(words[i + 1:], branch):
                problem = "this merge"
        else:
            problem = _merge_crew_git(tokens, cwd)
        if problem is not None:
            return refuse(problem)
    if merges and len(segments) > 1:
        return refuse("a merge in a compound command")
    return 0, ""


def guard_decision(raw, d=None, merge_branch=None):
    """(exit code, message) for the PreToolUse input `raw` (the JSON text). Any unexpected
    error denies: an exit code other than 2 would let the call run."""
    try:
        return _decide(raw, d, merge_branch)
    except Exception:
        return 2, "prorab guard: internal error; ask the foreman"


def _decide(raw, d, merge_branch=None):
    d = d or _defaults.load()
    unreadable = (2, "prorab guard: unreadable hook input")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return unreadable
    if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str):
        return unreadable
    if data["tool_name"] != "Bash":
        return 0, ""
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return unreadable
    folded = " ".join(command.split())
    for entry in d["hooks"]["deny"]:
        if entry in folded:
            return 2, "prorab guard: %s is forbidden for crews; ask the foreman" % entry.strip()
    if any(form in folded for form in _office_journal_forms()):
        return 2, ("prorab guard: the foreman's journal is off limits for crews; "
                   "ask the foreman")
    def cwd_of():
        return data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") \
            else os.getcwd()
    if merge_branch is not None:
        try:
            _merge_branch_checked(merge_branch)
        except ValueError:
            return 2, "prorab guard: the merge branch of this crew's settings is not usable"
        if _GIT_WORD.search(command):
            verdict = _merge_crew_verdict(command, cwd_of(), merge_branch)
            if verdict is not None:
                return verdict
    if _MERGE.search(folded):
        cwd = cwd_of()
        target = _dash_c_dir(folded)
        if target:
            target = os.path.expanduser(target)
            target = target if os.path.isabs(target) else os.path.join(cwd, target)
        branch = checked_out_branch(target or cwd)
        if branch is None:
            return 2, "prorab guard: cannot tell the checked-out branch; ask the foreman"
        if branch in ("main", "master"):
            return 2, ("prorab guard: merge or rebase on %s is forbidden for crews; "
                       "ask the foreman" % branch)
    return 0, ""


def guard(stdin=None, merge_branch=None):
    try:
        raw = (stdin or sys.stdin).read()
    except Exception:
        raw = None   # unreadable input is decided as such
    code, message = guard_decision(raw, merge_branch=merge_branch)
    if message:
        print(message, file=sys.stderr)
    return code


# ---- the tap --------------------------------------------------------------------------

def tap_record(data, now=None):
    """The event line for a hook input, or None when nothing is to be written."""
    if not isinstance(data, dict):
        return None
    event, sid = data.get("hook_event_name"), data.get("session_id")
    if event not in TAP_EVENTS or not safe_session_id(sid):
        return None
    rec = {"ts": time.time() if now is None else now, "event": event, "session_id": sid}
    if event == "Stop":
        rec["background_tasks"] = data.get("background_tasks")
        rec["last_assistant_message"] = str(data.get("last_assistant_message") or "")[:200]
    elif event == "Notification":
        rec["notification_type"] = data.get("notification_type")
    elif event == "PreCompact":
        rec["trigger"] = data.get("trigger")
    else:
        rec["source"] = data.get("source")
    return rec


def tap(stdin=None):
    """Append the input's event to `<events_dir>/<session_id>.jsonl`. Always 0."""
    try:
        data = json.loads((stdin or sys.stdin).read())
        rec = tap_record(data)
        if rec is not None:
            folder = _defaults.load()["hooks"]["events_dir"]
            os.makedirs(folder, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(folder, rec["session_id"] + ".jsonl"),
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # a tap must never disturb the crew
        pass
    return 0


# ---- CLI ------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=("settings", "guard", "tap"))
    ap.add_argument("--merge-branch", help="guard only: the one branch a main-checkout merge "
                    "crew may merge (git there is otherwise read-only)")
    args = ap.parse_args(argv)
    cmd = args.cmd
    if cmd == "guard":
        return guard(merge_branch=args.merge_branch)
    if cmd == "tap":
        return tap()
    print(write_settings())
    return 0


if __name__ == "__main__":
    sys.exit(main())
