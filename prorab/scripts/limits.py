#!/usr/bin/env python3
"""Claude Max limits: sensor file, status line text, or limit messages."""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402

_FIVE = re.compile(
    r"5h\s+\S+\s+([\d.]+)%"
    r"(?:\s*\((?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?\))?")
_WEEK = re.compile(r"wk\s+\S+\s+([\d.]+)%")


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


def read_sensor(path, max_age_s, now=None):
    try:
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    if now - mtime > max_age_s:
        return None
    rl = obj.get("rate_limits") if isinstance(obj, dict) else None
    if not isinstance(rl, dict):
        return None
    five = rl.get("five_hour") if isinstance(rl.get("five_hour"), dict) else {}
    seven = rl.get("seven_day") if isinstance(rl.get("seven_day"), dict) else {}
    f, s = _num(five.get("used_percentage")), _num(seven.get("used_percentage"))
    if f is None and s is None:
        return None
    f_at = _int(five.get("resets_at"))
    return _make(f, s, f_at, _int(seven.get("resets_at")), "sensor",
                 five_in=None if f_at is None else max(0, int(f_at - now)))


def parse_statusline(text):
    f, w = _FIVE.search(text), _WEEK.search(text)
    if not f and not w:
        return None
    five_in = None
    if f and any(f.group(i) for i in (2, 3, 4)):
        five_in = (int(f.group(2) or 0) * 86400 + int(f.group(3) or 0) * 3600
                   + int(f.group(4) or 0) * 60)
    return _make(float(f.group(1)) if f else None,
                 float(w.group(1)) if w else None, source="statusline",
                 five_in=five_in)


def detect_messages(text, patterns):
    low = text.lower()
    return any(p.lower() in low for p in patterns)


def get_limits(pane_texts, d, now=None):
    lim = d["limits"]
    r = read_sensor(lim["sensor_path"], lim["sensor_max_age_s"], now)
    if r:
        return r
    for text in pane_texts:
        r = parse_statusline(text)
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
