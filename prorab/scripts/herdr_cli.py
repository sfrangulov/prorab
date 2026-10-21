#!/usr/bin/env python3
"""Thin wrapper over the herdr CLI (>= 0.9.5): JSON calls, env check, reliable send."""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIN_VERSION = (0, 9, 5)


class HerdrError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def _exec(args, timeout_s):
    try:
        p = subprocess.run(["herdr"] + list(args), capture_output=True, text=True,
                           timeout=timeout_s)
    except FileNotFoundError:
        raise HerdrError("herdr_not_found", "herdr binary not on PATH")
    except subprocess.TimeoutExpired:
        raise HerdrError("cli_timeout", "herdr %s timed out" % " ".join(args))
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


def _text(args, timeout_s=60):
    p = _exec(args, timeout_s)
    if p.returncode != 0:
        raise _error_from(p)
    return p.stdout


def run(args, timeout_s=60):
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


def pane_visible(pane_id):
    return _text(["pane", "read", pane_id, "--source", "visible"])


def agent_visible(name):
    return _text(["agent", "read", name, "--source", "visible"])


def send_keys(name, keys):
    run(["agent", "send-keys", name] + list(keys))


_OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.*)$")


def choice_dialog(visible):
    if "Enter to select" not in visible or "↑/↓ to navigate" not in visible:
        return None
    labels = []
    for line in visible.splitlines():
        m = _OPTION.match(line)
        if m:
            labels.append(m.group(2).strip())
    return labels or None


def bg_shells(visible):
    found = [0]
    for pat in (r"· (\d+) shells?", r"(\d+) shells?(?:, \d+ monitors?)? still running"):
        found += [int(x) for x in re.findall(pat, visible)]
    return max(found)


def send(name, text, timeout_ms=15000):
    try:
        run(["agent", "prompt", name, text, "--wait", "--timeout", str(timeout_ms)],
            timeout_s=timeout_ms // 1000 + 30)
        return "accepted"
    except HerdrError as e:
        if e.code == "agent_prompt_stalled":
            return "stalled"
        if e.code == "agent_blocked":
            return "blocked"
        if e.code == "timeout":
            a = agent(name)
            if a and a.get("agent_status") == "working":
                return "accepted"
        raise
