#!/usr/bin/env python3
"""Claude Max limits: sensor file, status line text, or limit messages."""
import argparse
import json
import os
import re
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402

_COUNTDOWN = r"(?:\s*\((?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?\))?"
_FIVE = re.compile(r"5h\s+\S+\s+([\d.]+)%" + _COUNTDOWN)
_WEEK = re.compile(r"wk\s+\S+\s+([\d.]+)%" + _COUNTDOWN)
# a status-line bar: a label-less run of bar glyphs, then its percent
_BAR = re.compile(r"(?:(\S+)\s+)?[▓░█▒▌▏]+\s+([\d.]+)%")


def _make(five=None, seven=None, five_at=None, seven_at=None, source="unknown",
          reached=False, five_in=None):
    return {"five_hour_resets_in_s": five_in, "five_hour": five, "seven_day": seven,
            "five_hour_resets_at": five_at, "seven_day_resets_at": seven_at,
            "source": source, "reached": reached}


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _int(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _countdown(m):
    """Seconds from the d/h/m groups (2..4) of a match; None when absent."""
    if not any(m.group(i) for i in (2, 3, 4)):
        return None
    return (int(m.group(2) or 0) * 86400 + int(m.group(3) or 0) * 3600
            + int(m.group(4) or 0) * 60)


_WINDOW_TOLERANCE_S = 600


def prune_sensor(path, retention_s, now=None):
    """Delete regular files directly in `path` (never symlinks, never elsewhere) older
    than `retention_s`. Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(path)
    except OSError:
        return 0
    for name in names:
        full = os.path.join(path, name)
        try:
            st = os.lstat(full)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > retention_s:
                os.unlink(full)
                removed += 1
        except OSError:
            pass
    return removed


def read_sensor(path, max_age_s, now=None):
    """Limits from the per-session sensor files in directory `path`.

    Only files fresher than `max_age_s` count. Per window the files are grouped by
    `resets_at` (10-minute tolerance); the group with the latest `resets_at` wins, its
    maximum percent is used.
    """
    now = time.time() if now is None else now
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return None
    seen = {"five_hour": [], "seven_day": []}
    for name in names:
        if not name.endswith(".json"):
            continue
        full = os.path.join(path, name)
        try:
            mtime = os.path.getmtime(full)
            with open(full, encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError):
            continue
        if now - mtime > max_age_s:
            continue
        rl = obj.get("rate_limits") if isinstance(obj, dict) else None
        if not isinstance(rl, dict):
            continue
        for win in seen:
            w = rl.get(win)
            pct = _num(w.get("used_percentage")) if isinstance(w, dict) else None
            if pct is not None:
                seen[win].append((_int(w.get("resets_at")), pct))

    def pick(rows):
        if not rows:
            return None, None
        latest = max((r[0] for r in rows if r[0] is not None), default=None)
        if latest is None:
            return max(p for at, p in rows), None
        return max(p for at, p in rows
                   if at is not None and latest - at <= _WINDOW_TOLERANCE_S), latest

    f, f_at = pick(seen["five_hour"])
    s, s_at = pick(seen["seven_day"])
    if f is None and s is None:
        return None
    return _make(f, s, f_at, s_at, "sensor",
                 five_in=None if f_at is None else max(0, int(f_at - now)))


def _parse_panel(text):
    """Per-window (percent, countdown_s) from one status line; None where absent."""
    f, w = _FIVE.search(text), _WEEK.search(text)
    return ((float(f.group(1)), _countdown(f)) if f else None,
            (float(w.group(1)), _countdown(w)) if w else None)


def parse_statusline(text):
    five, week = _parse_panel(text)
    if not five and not week:
        return None
    return _make(five[0] if five else None, week[0] if week else None,
                 source="statusline", five_in=five[1] if five else None)


def statusline_context(text):
    """Percent of the FIRST bar on the status line (the session's context), else None."""
    m = _BAR.search(text)
    if not m or m.group(1) in ("5h", "wk"):
        return None  # no bars, or the line starts at a limit bar (no context shown)
    return float(m.group(2))


def detect_messages(text, patterns):
    low = text.lower()
    return any(p.lower() in low for p in patterns)


def _from_panes(pane_texts):
    """Per-window maximum over all panes; a window whose countdown is (0m) is skipped."""
    best = {"five": None, "week": None}  # (percent, countdown)
    positive = []
    for text in pane_texts:
        five, week = _parse_panel(text)
        if five and five[1] is not None and five[1] > 0:
            positive.append(five[1])
        for key, val in (("five", five), ("week", week)):
            if val is None or val[1] == 0:
                continue
            if best[key] is None or val[0] > best[key][0]:
                best[key] = val
    if best["five"] is None and best["week"] is None:
        return None
    five_in = None
    if best["five"] is not None:
        five_in = best["five"][1]
        if five_in is None and positive:
            five_in = min(positive)
    return _make(best["five"][0] if best["five"] else None,
                 best["week"][0] if best["week"] else None,
                 source="statusline", five_in=five_in)


def get_limits(pane_texts, d, now=None):
    lim = d["limits"]
    prune_sensor(lim["sensor_dir"], lim["sensor_retention_s"])  # real clock
    r = read_sensor(lim["sensor_dir"], lim["sensor_max_age_s"], now)
    if r:
        return r
    r = _from_panes(pane_texts)
    if r:
        return r
    if any(detect_messages(t, lim["message_patterns"]) for t in pane_texts):
        return _make(source="messages", reached=True)
    return _make()


def _live_pane_texts():
    import herdr_cli  # lazy: only the live CLI path needs herdr
    return [herdr_cli.pane_visible(a["pane_id"]) for a in herdr_cli.agents()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pane-text-file", action="append", default=[])
    args = ap.parse_args(argv)
    if args.pane_text_file:
        texts = []
        for p in args.pane_text_file:
            with open(p, encoding="utf-8") as fh:
                texts.append(fh.read())
    else:
        texts = _live_pane_texts()
    print(json.dumps(get_limits(texts, _defaults.load())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
