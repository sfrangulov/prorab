#!/usr/bin/env python3
"""Read Claude Code transcripts (JSONL): context size, last text, usage totals."""
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402


def project_slug(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_path(cwd, session_id, home=None):
    """Expected transcript path; the file may not exist yet (the id is assigned up front)."""
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects", project_slug(cwd), session_id + ".jsonl")


def find_transcript(cwd, session_id, home=None):
    home = home or os.path.expanduser("~")
    base = os.path.join(home, ".claude", "projects")
    direct = transcript_path(cwd, session_id, home)
    if os.path.isfile(direct):
        return direct
    hits = sorted(glob.glob(os.path.join(glob.escape(base), "*", glob.escape(session_id) + ".jsonl")))
    return hits[0] if hits else None


def _assistant_entries(path):
    """Yield (message dict) for non-sidechain assistant entries, in order."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            if entry.get("isSidechain"):
                continue
            msg = entry.get("message")
            if isinstance(msg, dict):
                yield msg


def _tokens(usage):
    return sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "output_tokens"))


def last_usage(path):
    result = None
    for msg in _assistant_entries(path):
        usage = msg.get("usage")
        if isinstance(usage, dict):
            result = (_tokens(usage), msg.get("model") or "")
    return result


def models(path):
    """Distinct models of non-sidechain assistant entries, first-seen order."""
    seen = []
    for msg in _assistant_entries(path):
        m = msg.get("model")
        if m and m not in seen:
            seen.append(m)
    return seen


def context_status(path, d):
    last = last_usage(path)
    if last is None:
        return None
    tokens, model = last
    c = d["context"]
    window, window_model = c["unknown_window"], model
    best = None
    for m in models(path):
        w = c["windows"].get(m, c["unknown_window"])
        if best is None or w > best:
            best, window, window_model = w, w, m
    return {
        "tokens": tokens,
        "model": model,
        "window": window,
        "window_model": window_model,
        "pct": round(tokens * 100.0 / window, 1),
        "soft": tokens >= min(window * c["soft_pct"] / 100, c["soft_max_tokens"]),
        "hard": tokens >= window * c["hard_pct"] / 100,
    }


def last_text(path):
    result = ""
    for msg in _assistant_entries(path):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        if parts:
            result = "\n".join(parts)
    return result


def usage_totals(path):
    """Sum usage once per API message: lines sharing message.id count once (last wins)."""
    by_id = {}
    anon = []
    for msg in _assistant_entries(path):
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        if msg.get("id"):
            by_id[msg["id"]] = usage
        else:
            anon.append(usage)
    t = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0, "messages": 0}
    for usage in list(by_id.values()) + anon:
        t["input"] += int(usage.get("input_tokens") or 0)
        t["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
        t["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        t["output"] += int(usage.get("output_tokens") or 0)
        t["messages"] += 1
    return t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["status", "text", "usage"])
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--session", required=True)
    args = ap.parse_args(argv)
    path = find_transcript(args.cwd, args.session)
    if path is None:
        print(json.dumps({"error": "transcript not found"}), file=sys.stderr)
        return 1
    if args.cmd == "status":
        print(json.dumps(context_status(path, _defaults.load())))
    elif args.cmd == "text":
        print(last_text(path))
    else:
        print(json.dumps(usage_totals(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
