#!/usr/bin/env python3
"""Thin wrapper over the herdr CLI (>= 0.9.5): JSON calls, env check, reliable send,
screen markers (consent prompts, choice dialogs, composer line)."""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402

MIN_VERSION = (0, 9, 5)
NAME_RULE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class HerdrError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def _exec(args, timeout_s):
    if timeout_s is None:
        timeout_s = _defaults.load()["timeouts"]["herdr_s"]
    try:
        p = subprocess.run(["herdr"] + list(args), capture_output=True, text=True,
                           timeout=timeout_s)
    except FileNotFoundError:
        raise HerdrError("herdr_not_found", "herdr binary not on PATH")
    except subprocess.TimeoutExpired:
        raise HerdrError("cli_timeout", "herdr %s timed out after %s s"
                         % (" ".join(args), timeout_s))
    return p


def _error_from(p):
    """Build a HerdrError from a failed process (JSON error or raw text)."""
    for text in (p.stderr, p.stdout):
        try:
            err = json.loads(text).get("error")
        except (ValueError, AttributeError):
            continue
        if isinstance(err, dict) and err.get("code"):
            return HerdrError(err["code"], err.get("message") or err["code"])
    msg = (p.stderr or p.stdout or "").strip() or "exit %d" % p.returncode
    return HerdrError("cli_error", msg)


def _text(args, timeout_s=None):
    p = _exec(args, timeout_s)
    if p.returncode != 0:
        raise _error_from(p)
    return p.stdout


def run(args, timeout_s=None):
    p = _exec(args, timeout_s)
    if p.returncode != 0:
        raise _error_from(p)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        raise HerdrError("cli_error", "non-JSON output: %s" % p.stdout.strip()[:200])
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        raise HerdrError(data["error"].get("code", "cli_error"),
                         data["error"].get("message", ""))
    return data


def run_ack(args, timeout_s=None):
    """`run` for herdr commands whose answer the scripts do not read: any answer on exit 0
    is a success (empty -> {}, JSON without an `error` object -> the parsed JSON, other text
    -> {"text": first 200 characters}). Raises only on a non-zero exit or a JSON `error`
    object; the message names the command, the exit code and stderr (first 200 characters)."""
    p = _exec(args, timeout_s)
    where = "herdr %s: rc=%d, stderr=%s" % (" ".join(args), p.returncode,
                                           (p.stderr or "").strip()[:200])
    if p.returncode != 0:
        e = _error_from(p)
        raise HerdrError(e.code, "%s: %s" % (where, e))
    out = (p.stdout or "").strip()
    if not out:
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {"text": out[:200]}
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = data["error"]
        raise HerdrError(err.get("code", "cli_error"),
                         "%s: %s" % (where, err.get("message") or err.get("code", "")))
    return data


def check_env():
    if os.environ.get("HERDR_ENV") != "1":
        raise HerdrError("not_in_herdr", "HERDR_ENV is not 1: run inside a herdr pane")
    out = _text(["--version"], 10)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m or tuple(int(x) for x in m.groups()) < MIN_VERSION:
        raise HerdrError("herdr_too_old", "need herdr >= 0.9.5, got: %s" % out.strip())


def agents():
    return run(["agent", "list"])["result"]["agents"]


def agent(name):
    for a in agents():
        if a.get("name") == name:
            return a
    return None


def agent_by_pane(pane_id, agents_list=None):
    """The agent living in `pane_id`. Pane numbers are reused and names can be reset,
    so after a handover a crew is found by its pane; its name is only compared."""
    for a in (agents() if agents_list is None else agents_list):
        if a.get("pane_id") == pane_id:
            return a
    return None


def own_session():
    """The session of the agent in $HERDR_PANE_ID (the foreman's own), else None: no pane,
    no agent in it, no session shown, or herdr fails."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return None
    try:
        a = agent_by_pane(pane)
    except HerdrError:
        return None
    return ((a or {}).get("agent_session") or {}).get("value")


def valid_name(name):
    """herdr's agent name rule."""
    return isinstance(name, str) and NAME_RULE.match(name) is not None


def pane_visible(pane_id):
    return _text(["pane", "read", pane_id, "--source", "visible"])


def agent_visible(name):
    return _text(["agent", "read", name, "--source", "visible"])


def send_keys(name, keys):
    run(["agent", "send-keys", name] + list(keys))


def bottom_block(visible, n):
    """The last `n` non-empty lines of a screen. Screen markers are searched only
    there: old output above may contain the same words."""
    lines = [ln for ln in visible.splitlines() if ln.strip()]
    return "\n".join(lines[-n:] if n > 0 else [])


# Consent prompts the foreman never answers: (kind, case-insensitive markers); first match
# wins. Markers stay broad: a false positive only stops the crew, a miss could let a
# consent prompt be answered as a question.
CONSENT_KINDS = (
    ("api_key", ("use this api key",)),
    ("trust", ("trust this folder", "quick safety check", "do you trust the files",
               "workspace trust not yet accepted")),
    ("bypass_warning", ("bypass permissions mode",)),
    ("claude_md_imports", ("claude.md file imports", "external claude.md")),
    ("mcp", ("mcp server",)),
    ("tool_permission", ("esc to cancel · tab to amend", "do you want to proceed",
                         "do you want to make this edit", "do you want to create",
                         "do you want to allow", "don't ask again")),
)
CONSENT_NAMES = {
    "trust": "folder trust",
    "mcp": "the project's MCP servers",
    "claude_md_imports": "external CLAUDE.md imports",
    "bypass_warning": "bypass-mode warning",
    "api_key": "custom API key",
    "tool_permission": "tool permission",
}


def _tail_lines(tail_lines):
    if tail_lines is not None:
        return tail_lines
    return _defaults.load()["screen"]["tail_lines"]


def consent_dialog(visible, tail_lines=None):
    """The kind of consent prompt in the bottom block of the screen, else None."""
    low = bottom_block(visible, _tail_lines(tail_lines)).lower()
    for kind, markers in CONSENT_KINDS:
        if any(m in low for m in markers):
            return kind
    return None


# Claude Code 2.1.307 renders a no-break space (U+00A0) after the marker, older ones a space
_COMPOSER = re.compile(r"^[❯>](?:[ \u00a0](.*))?$")
_COMPOSER_RULE = re.compile(r"^\s*[─━]{3,}\s*$")
_CHOICE_ROW = re.compile(r"^\d+\.\s")


def composer_text(visible, tail_lines=None):
    """Text of Claude Code's input line in the bottom block, "" for an empty composer,
    None when none is visible. The composer is the last line starting with the prompt
    marker (`❯`, or `>` in older versions) that sits directly between two horizontal
    rules; a choice-dialog row such as `❯ 1. Yes` is never a composer."""
    lines = bottom_block(visible, _tail_lines(tail_lines)).splitlines()
    for i in range(len(lines) - 2, 0, -1):
        if not (_COMPOSER_RULE.match(lines[i - 1]) and _COMPOSER_RULE.match(lines[i + 1])):
            continue
        m = _COMPOSER.match(lines[i].strip())
        if m and not _CHOICE_ROW.match(m.group(1) or ""):
            return (m.group(1) or "").strip()
    return None


_OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.*)$")
_HIGHLIGHT = re.compile(r"^\s*❯\s*\d+\.\s")
_RULE = re.compile(r"^\s*[─━-]{8,}\s*$")


def choice_state(visible):
    """(labels, highlighted_index) of the open choice dialog, or None.

    Only the dialog block counts: the numbered lines contiguous with the line
    marked with the pointer (options, their indented descriptions and the rule
    before the last option), up to the footer hint. Numbered lines printed above
    the dialog do not become options.
    """
    if consent_dialog(visible) is not None:
        return None
    if "Enter to select" not in visible or "↑/↓ to navigate" not in visible:
        return None
    lines = visible.splitlines()
    footer = next((i for i, ln in enumerate(lines) if "Enter to select" in ln), len(lines))
    marked = [i for i, ln in enumerate(lines[:footer]) if _HIGHLIGHT.match(ln)]
    if not marked:
        return None
    start = marked[-1]
    while start > 0 and (lines[start - 1].startswith("  ") or _RULE.match(lines[start - 1])):
        start -= 1
    labels, highlighted = [], 0
    for ln in lines[start:footer]:
        m = _OPTION.match(ln)
        if m and (ln.startswith("  ") or ln.startswith("❯")):
            if _HIGHLIGHT.match(ln):
                highlighted = len(labels)
            labels.append(m.group(2).strip())
    return (labels, highlighted) if labels else None


def choice_dialog(visible):
    st = choice_state(visible)
    return st[0] if st else None


def bg_shells(visible):
    found = [0]
    for pat in (r"· (\d+) shells?", r"(\d+) shells?(?:, \d+ monitors?)? still running"):
        found += [int(x) for x in re.findall(pat, visible)]
    return max(found)


def _send_timeout_ms(timeout_ms):
    return timeout_ms or _defaults.load()["send"]["timeout_ms"]


STATUSES = ("idle", "working", "blocked", "done", "unknown")


def _status_in(data):
    """A known agent status in a herdr JSON answer (result.agent or result), else None."""
    res = data.get("result") if isinstance(data, dict) else None
    if not isinstance(res, dict):
        return None
    for node in (res.get("agent"), res):
        if isinstance(node, dict):
            st = node.get("agent_status") or node.get("status")
            if st in STATUSES:
                return st
    return None


def _settled_status(data, name):
    """The status in a wait/prompt answer; without one, ask `agent get` (None if that fails)."""
    st = _status_in(data)
    if st:
        return st
    try:
        return _status_in(run(["agent", "get", name]))
    except HerdrError:
        return None


def _wait_args(timeout_ms, untils=("working", "done", "blocked")):
    args = []
    for u in untils:
        args += ["--until", u]
    return args + ["--timeout", str(timeout_ms)]


def send(name, text, timeout_ms=None):
    """Send one line to a crew (multi-line text raises ValueError). "accepted" | "blocked" | "stalled" | "unconfirmed".

    The text is sent once and never re-sent; nothing is pressed afterwards. There is no
    `--until idle`: the agent is idle before the prompt, so a swallowed Enter would read
    as delivery.
    """
    if "\n" in text:
        raise ValueError("send takes one line; a multi-line message goes by file")
    ms = _send_timeout_ms(timeout_ms)
    try:
        data = run(["agent", "prompt", name, text, "--wait"] + _wait_args(ms),
                   timeout_s=ms // 1000 + 30)
    except HerdrError as e:
        if e.code == "agent_prompt_stalled":
            return "stalled"
        if e.code == "agent_blocked":
            return "blocked"
        if e.code == "timeout":
            a = agent(name)
            return "accepted" if a and a.get("agent_status") == "working" else "unconfirmed"
        raise
    st = _settled_status(data, name)
    if st == "blocked":
        return "blocked"
    return "accepted" if st in ("working", "done") else "unconfirmed"


def recover_stalled(name, text, timeout_ms=None):
    """After "stalled": press Enter exactly once, and only when the agent is idle or done
    and the visible composer line holds the start of our text. Never on an empty composer
    (a grey prompt suggestion there could be submitted), never over a dialog.
    Afterwards it waits only for `working` or `blocked`: the wait matches the current
    state, so `done`/`idle` would not prove the Enter was taken.
    Returns "accepted" | "blocked" | "stalled" (nothing pressed) | "unconfirmed"."""
    ms = _send_timeout_ms(timeout_ms)
    st = _status_in(run(["agent", "get", name]))
    if st not in ("idle", "done"):
        return "stalled"
    shown = composer_text(agent_visible(name))
    n = min(_defaults.load()["send"]["composer_match_chars"], len(text))
    if not shown or n == 0 or shown[:n] != text[:n]:
        return "stalled"
    send_keys(name, ["enter"])
    try:
        data = run(["agent", "wait", name] + _wait_args(ms, ("working", "blocked")),
                   timeout_s=ms // 1000 + 30)
    except HerdrError as e:
        if e.code in ("timeout", "agent_prompt_stalled"):
            return "unconfirmed"
        if e.code == "agent_blocked":
            return "blocked"
        raise
    st = _settled_status(data, name)
    return {"working": "accepted", "blocked": "blocked"}.get(st, "unconfirmed")


def wait_ready(name, timeout_ms=None):
    """Wait for the first prompt: the reached status ("idle" or "done"), or "timeout"."""
    ms = _send_timeout_ms(timeout_ms)
    try:
        data = run(["agent", "wait", name, "--until", "idle", "--until", "done",
                    "--timeout", str(ms)], timeout_s=ms // 1000 + 30)
    except HerdrError as e:
        if e.code == "timeout":
            return "timeout"
        raise
    return _status_in(data) or _status_in(run(["agent", "get", name])) or "timeout"
