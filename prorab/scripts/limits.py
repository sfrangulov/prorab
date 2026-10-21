#!/usr/bin/env python3
"""Claude Max limits: sensor file, status line text, or limit messages."""
import argparse
import bisect
import datetime
import json
import os
import re
import stat
import sys
import time
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402
import herdr_cli  # noqa: E402

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
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if now - mtime > max_age_s:
            continue
        rl = data.get("rate_limits") if isinstance(data, dict) else None
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


def detect_messages(text, patterns, tail_lines=None):
    """True when a limit message pattern is present; with `tail_lines`, only in the
    bottom block of the screen (old output above does not count)."""
    if tail_lines is not None:
        text = herdr_cli.bottom_block(text, tail_lines)
    low = text.lower()
    return any(p.lower() in low for p in patterns)


_RESETS = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?"
    r"(?:\s*\(([A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*)\))?", re.I)


def parse_resets(text, now):
    """Unix seconds of the first "resets 7pm" / "resets 7:30pm" / "resets 19:00" moment
    after `now`, optionally with a "(Area/City)" zone; None when absent or unreadable."""
    for m in _RESETS.finditer(text):
        hour, minute, meridiem, zone = int(m.group(1)), int(m.group(2) or 0), m.group(3), m.group(4)
        if meridiem is None and m.group(2) is None:
            continue  # a bare number is not a time
        if meridiem:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
        if hour > 23 or minute > 59:
            continue
        tz = None
        if zone:
            try:
                tz = zoneinfo.ZoneInfo(zone)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                return None
        today = datetime.datetime.fromtimestamp(now, tz).date()
        for add in range(3):
            moment = datetime.datetime.combine(today + datetime.timedelta(days=add),
                                               datetime.time(hour, minute), tzinfo=tz)
            if moment.timestamp() > now:
                return int(moment.timestamp())
        return None
    return None


def limit_screen(text, d, now=None):
    """A limit screen in the bottom block: a choice dialog with a "wait" option, or a plain
    limit message. Carries the reset time when the screen shows one; else None."""
    lim = d["limits"]
    now = time.time() if now is None else now
    block = herdr_cli.bottom_block(text, d["screen"]["tail_lines"])
    wait, upgrade = lim["limit_choice"]["wait"].lower(), lim["limit_choice"]["upgrade"].lower()
    st = herdr_cli.choice_state(block)
    if st:
        labels, highlighted = st
        for i, label in enumerate(labels):
            low = label.lower()
            if low.startswith(wait) and not low.startswith(upgrade):
                return {"kind": "limit_choice", "options": labels, "highlighted": highlighted,
                        "wait_option": i, "resets_at": parse_resets(block, now)}
    if detect_messages(block, lim["limit_screen"]):
        return {"kind": "limit", "resets_at": parse_resets(block, now)}
    return None


def provider_error(text, d):
    """The first final provider error pattern (an account problem, not a limit) in the
    bottom block, else None."""
    block = herdr_cli.bottom_block(text, d["screen"]["tail_lines"]).lower()
    for pat in d["limits"]["fatal_patterns"]:
        if pat.lower() in block:
            return pat
    return None


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
    """`pane_texts`: a list of screens or a zero-argument callable returning one; the
    callable runs only when the sensor gives nothing."""
    lim = d["limits"]
    prune_sensor(lim["sensor_dir"], lim["sensor_retention_s"])  # real clock
    r = read_sensor(lim["sensor_dir"], lim["sensor_max_age_s"], now)
    if r:
        return r
    if callable(pane_texts):
        pane_texts = pane_texts()
    r = _from_panes(pane_texts)
    if r:
        return r
    tail = d["screen"]["tail_lines"]
    for t in pane_texts:
        if detect_messages(t, lim["message_patterns"], tail):
            at = parse_resets(herdr_cli.bottom_block(t, tail),
                              time.time() if now is None else now)
            return _make(source="messages", reached=True, five_at=at)
    return _make()


_RESET_KEY = {"five_hour": "resets_at", "seven_day": "seven_day_resets_at"}


def _sensor_points(readings, window):
    """``(ts, percent, resets_at)`` of the sensor readings that carry both for `window`."""
    key = _RESET_KEY[window]
    out = []
    for r in readings:
        if r.get("source") != "sensor":
            continue
        pct, at = _num(r.get(window)), _num(r.get(key))
        if pct is not None and at is not None:
            out.append((r["ts"], pct, at))
    out.sort(key=lambda p: p[0])
    return out


def rate_intervals(readings, window, d):
    """``(t0, t1, rise_pp)`` intervals of one window from sensor readings, never across a reset."""
    min_span = d["schedule"]["burn_min_span_s"]
    min_rise = d["rate"]["min_rise_pp"]
    out = []
    anchor = None
    for ts, pct, at in _sensor_points(readings, window):
        if anchor is None or abs(at - anchor[2]) > _WINDOW_TOLERANCE_S or pct < anchor[1]:
            anchor = (ts, pct, at)
        elif ts - anchor[0] >= min_span and pct - anchor[1] >= min_rise:
            out.append((anchor[0], ts, pct - anchor[1]))
            anchor = (ts, pct, at)
    return out


def _counted(readings, window, d, now):
    if window == "seven_day":
        pts = _sensor_points(readings, window)
        if not pts:
            return []
        latest = max(p[2] for p in pts)
        current = {p[0] for p in pts if latest - p[2] <= _WINDOW_TOLERANCE_S}
        readings = [r for r in readings if r.get("ts") in current]
        return rate_intervals(readings, window, d)
    horizon = d["rate"]["window_horizon_s"]
    return [iv for iv in rate_intervals(readings, window, d) if now - horizon <= iv[1] <= now]


def estimate_rate(readings, spend_between, d, now, window="five_hour"):
    """Dollars per percentage point of `window`: the sum of dollars over the sum of pp of the
    counted intervals; the default (five_hour) or None (seven_day) without an interval."""
    ivs = _counted(readings, window, d, now)
    usd = sum(spend_between(t0, t1) for t0, t1, _ in ivs)
    pp = sum(rise for _, _, rise in ivs)
    if usd > 0 and pp > 0:
        return {"window": window, "usd_per_pp": usd / pp, "source": "estimated",
                "intervals": len(ivs), "usd": usd, "pp": pp, "at": now}
    default = d["rate"]["default_window_usd_per_pp" if window == "five_hour"
                        else "default_week_usd_per_pp"]
    return {"window": window, "usd_per_pp": default,
            "source": "unknown" if default is None else "default",
            "intervals": 0, "usd": 0.0, "pp": 0.0, "at": now}


def rates(readings, d, now, home=None):
    """The rate of both windows; one pass over the machine's transcripts."""
    import transcript  # lazy: only the rate needs it
    starts = [iv[0] for w in _RESET_KEY for iv in _counted(readings, w, d, now)]
    times, prefix = [], [0.0]
    if starts:
        since = min(starts)
        for ts, usd in transcript.spend_series(
                transcript.machine_files(since=since, home=home), d, since=since):
            times.append(ts)
            prefix.append(prefix[-1] + usd)

    def spend_between(t0, t1):
        return prefix[bisect.bisect_left(times, t1)] - prefix[bisect.bisect_left(times, t0)]
    return {w: estimate_rate(readings, spend_between, d, now, w) for w in _RESET_KEY}


def to_pp(usd, rate):
    """Dollars as pp of the window the rate belongs to; None when the rate is unknown."""
    per = rate.get("usd_per_pp")
    return None if not per else usd / per


def _live_pane_texts():
    import herdr_cli  # lazy: only the live CLI path needs herdr
    return [herdr_cli.pane_visible(a["pane_id"]) for a in herdr_cli.agents()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", nargs="?", choices=["rate"])
    ap.add_argument("--log")
    ap.add_argument("--pane-text-file", action="append", default=[])
    args = ap.parse_args(argv)
    if args.cmd == "rate":
        import scheduler  # lazy: scheduler imports journal
        print(json.dumps(rates(scheduler.readings(args.log), _defaults.load(), time.time())))
        return 0
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
