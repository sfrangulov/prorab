#!/usr/bin/env python3
"""Read Claude Code transcripts (JSONL): context size, last text, usage totals, spend in dollars."""
import argparse
import datetime
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


def levels(tokens, window, d):
    """{"pct", "soft", "hard"} of `tokens` against `window` by the context thresholds."""
    c = d["context"]
    return {
        "pct": round(tokens * 100.0 / window, 1),
        "soft": tokens >= min(window * c["soft_pct"] / 100, c["soft_max_tokens"]),
        "hard": tokens >= window * c["hard_pct"] / 100,
    }


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
    return dict({"tokens": tokens, "model": model, "window": window,
                 "window_model": window_model, "source": "transcript"},
                **levels(tokens, window, d))


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


_META = "is" + "Meta"   # Claude Code's flag on entries it writes itself (no message to the crew)
_STAGE_REPORT = re.compile(r"^#\s*Stage\s+\S+\s+report:")
_NOTICE = "<task-notification"   # a finished background task tells the crew, by a user entry
_final_cache = {}


def _delivered(entry):
    """The text of a message delivered to the crew (a non-sidechain `user` entry that is no
    tool result and no Claude Code meta entry), else None. A background-task notice counts
    even when marked meta: it wakes the crew."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    if not text.strip():
        return None
    if entry.get(_META) and not text.lstrip().startswith(_NOTICE):
        return None
    return text


def _conversation(path):
    """(role, text) of the main chain in order: "user" for a delivered message, "assistant"
    for an entry with a text block."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            text = _delivered(entry)
            if text is not None:
                yield "user", text
            elif entry.get("type") == "assistant" and not entry.get("isSidechain"):
                msg = entry.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    parts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
                    if parts:
                        yield "assistant", "\n".join(parts)


def final_report(path):
    """"stage_report" | "handover" | None: whether the last assistant text after the latest
    message delivered to the crew is its stage report (first line `# Stage N report:`) or
    its handover act (last line `HANDOVER READY`). One pass, in this process; the answer is
    cached by the file's size and mtime, so a steady poll reads an unchanged file once."""
    try:
        st = os.stat(path)
        key = (st.st_size, st.st_mtime_ns)
        hit = _final_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        text = None
        for role, body in _conversation(path):
            text = body if role == "assistant" else None
    except (OSError, TypeError, ValueError):
        return None
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    result = None
    if lines and _STAGE_REPORT.match(lines[0]):
        result = "stage_report"
    elif lines and lines[-1] == "HANDOVER READY":
        result = "handover"
    _final_cache[path] = (key, result)
    return result


def last_delivered(path):
    """(text, worked): the latest message delivered to the crew and whether an assistant
    entry follows it; (None, False) when there is none or the file cannot be read."""
    text, worked = None, False
    try:
        for role, body in _conversation(path):
            if role == "user":
                text, worked = body, False
            else:
                worked = True
    except (OSError, TypeError):
        return None, False
    return text, worked


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


# ---- spend in dollars ---------------------------------------------------------------------

def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def price_for(model, d):
    """Dollars per million tokens ``{"input", "output", "cache_read"}`` for a model name.

    The first ``cost.prices`` family whose name is a substring of the model; an unknown
    model gets the column-wise maximum over all families (never cheaper than a known one).
    """
    prices = d["cost"]["prices"]
    low = (model or "").lower()
    for family, row in prices.items():
        if family in low:
            return dict(row)
    cols = ("input", "output", "cache_read")
    return {c: max(row[c] for row in prices.values()) for c in cols}


def _cache_write_tokens(usage):
    """``(tokens written for 5 minutes, tokens written for 1 hour)``.

    Without a split the whole ``cache_creation_input_tokens`` counts as 1-hour writes.
    """
    split = usage.get("cache_creation")
    if isinstance(split, dict) and (
            "ephemeral_5m_input_tokens" in split or "ephemeral_1h_input_tokens" in split):
        return _int(split.get("ephemeral_5m_input_tokens")), _int(split.get("ephemeral_1h_input_tokens"))
    return 0, _int(usage.get("cache_creation_input_tokens"))


def message_cost(usage, model, d):
    """Dollars for one API message's ``usage``."""
    price = price_for(model, d)
    c = d["cost"]
    w5, w1 = _cache_write_tokens(usage)
    total = (_int(usage.get("input_tokens")) * price["input"]
             + w5 * c["cache_write_5m_factor"] * price["input"]
             + w1 * c["cache_write_1h_factor"] * price["input"]
             + _int(usage.get("cache_read_input_tokens")) * price["cache_read"]
             + _int(usage.get("output_tokens")) * price["output"])
    return total / 1000000.0


def session_files(project_dir):
    """The transcripts of a project directory: its ``*.jsonl`` and ``*/subagents/*.jsonl``."""
    base = glob.escape(project_dir)
    return sorted(glob.glob(os.path.join(base, "*.jsonl"))
                  + glob.glob(os.path.join(base, "*", "subagents", "*.jsonl")))


def session_cost_files(transcript_path):
    """A session's transcript plus the transcripts of its subagents."""
    stem, _ = os.path.splitext(transcript_path)
    subs = sorted(glob.glob(os.path.join(glob.escape(stem), "subagents", "*.jsonl")))
    return [transcript_path] + subs


def _projects_dir(home):
    return os.path.join(home or os.path.expanduser("~"), ".claude", "projects")


def _first_cwd(files):
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"cwd"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and isinstance(entry.get("cwd"), str):
                        return entry["cwd"]
        except OSError:
            continue
    return None


def worktree_files(worktree, home=None):
    """Transcripts of every run made in a worktree or in a directory below it."""
    base = _projects_dir(home)
    slug = project_slug(worktree)
    root = os.path.realpath(worktree)
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if name != slug and not name.startswith(slug + "-"):
            continue
        files = session_files(os.path.join(base, name))
        cwd = _first_cwd(files)
        if cwd is None:
            continue
        real = os.path.realpath(cwd)
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            out.extend(files)
    return out


def _site_of(worktree):
    """The site a worktree ``<site>/.claude/worktrees/<name>`` belongs to, else None."""
    marker = os.sep + os.path.join(".claude", "worktrees") + os.sep
    head, found, _ = worktree.rstrip(os.sep).partition(marker)
    return head if found and head else None


def _session_transcript(worktree, session_id, home):
    """A session's transcript: in the worktree's project directory, else in the site's (where
    Claude Code moves it when a ``-w`` session exits), else in any project directory."""
    base = _projects_dir(home)
    for cwd in (worktree, _site_of(worktree)):
        if cwd:
            path = os.path.join(base, project_slug(cwd), session_id + ".jsonl")
            if os.path.isfile(path):
                return path
    return find_transcript("", session_id, home)


def work_order_files(worktree, sessions=(), home=None, own_only=False):
    """Transcripts of a work order: every run in its worktree, plus each crew session by id
    wherever Claude Code left it (and its subagents). The site's directory is never listed whole.
    With `own_only` (a crew in the site's main checkout, whose "worktree" is the site) only the
    sessions by id are read."""
    files = set() if own_only else set(worktree_files(worktree, home))
    for session_id in sessions:
        path = _session_transcript(worktree, session_id, home)
        if path:
            files.update(session_cost_files(path))
    return sorted(files)


def machine_files(since=None, home=None):
    """Every transcript on the machine; files untouched since ``since`` are left out unopened."""
    out = []
    try:
        names = sorted(os.listdir(_projects_dir(home)))
    except OSError:
        return out
    for name in names:
        for path in session_files(os.path.join(_projects_dir(home), name)):
            if since is not None:
                try:
                    if os.path.getmtime(path) < since:
                        continue
                except OSError:
                    continue
            out.append(path)
    return out


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _messages(paths, d, since, until, seen):
    """Deduplicated assistant messages with usage in the window, in time order."""
    best = {}
    anon = []
    for path in paths:
        if since is not None:
            try:
                if os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                msg = entry.get("message")
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(usage, dict):
                    continue
                ts = _parse_ts(entry.get("timestamp"))
                if ts is None or (since is not None and ts < since) \
                        or (until is not None and ts >= until):
                    continue
                rec = {"ts": ts, "usage": usage, "model": msg.get("model") or "",
                       "out": _int(usage.get("output_tokens"))}
                mid = msg.get("id")
                if not mid:
                    anon.append(rec)
                elif mid not in best or rec["out"] > best[mid]["out"]:
                    best[mid] = rec
    recs = list(anon)
    for mid, rec in best.items():
        if seen is not None:
            if mid in seen:
                continue
            seen[mid] = True
        recs.append(rec)
    recs.sort(key=lambda r: r["ts"])
    for rec in recs:
        rec["usd"] = message_cost(rec["usage"], rec["model"], d)
    return recs


def spend_series(paths, d, since=None, until=None, seen=None):
    """``(timestamp, usd)`` per assistant message, in time order; each message id counts once."""
    return [(r["ts"], r["usd"]) for r in _messages(paths, d, since, until, seen)]


def cost(paths, d, since=None, until=None, seen=None):
    """Total dollars, message count, token sums and dollars per model over the same set."""
    out = {"usd": 0.0, "messages": 0,
           "tokens": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0},
           "models": {}}
    for r in _messages(paths, d, since, until, seen):
        u = r["usage"]
        w5, w1 = _cache_write_tokens(u)
        out["usd"] += r["usd"]
        out["messages"] += 1
        t = out["tokens"]
        t["input"] += _int(u.get("input_tokens"))
        t["cache_write"] += w5 + w1
        t["cache_read"] += _int(u.get("cache_read_input_tokens"))
        t["output"] += r["out"]
        out["models"][r["model"]] = out["models"].get(r["model"], 0.0) + r["usd"]
    return out


def _ts_arg(value):
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        ts = _parse_ts(value)
        if ts is None:
            raise SystemExit("bad timestamp %r" % value)
        return ts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["status", "text", "usage", "cost"])
    ap.add_argument("--cwd")
    ap.add_argument("--session", action="append", default=[])
    ap.add_argument("--worktree")
    ap.add_argument("--machine", action="store_true")
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--since")
    ap.add_argument("--until")
    args = ap.parse_args(argv)
    if args.cmd == "cost":
        since, until = _ts_arg(args.since), _ts_arg(args.until)
        if args.worktree:
            paths = work_order_files(args.worktree, args.session)
        elif args.machine:
            paths = machine_files(since)
        elif args.file:
            paths = list(args.file)
        else:
            ap.error("cost needs --worktree, --machine or --file")
        print(json.dumps(cost(paths, _defaults.load(), since, until)))
        return 0
    if not args.cwd or not args.session:
        ap.error("--cwd and --session are required")
    path = find_transcript(args.cwd, args.session[-1])
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
