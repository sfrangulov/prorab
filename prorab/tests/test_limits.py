import calendar
import datetime
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zoneinfo
from contextlib import redirect_stdout

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import limits  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


SENSOR = {"rate_limits": {
    "five_hour": {"used_percentage": 17.0, "resets_at": 1761060000},
    "seven_day": {"used_percentage": 26.0, "resets_at": 1761400000}}}


class TestLimitScreens(unittest.TestCase):
    def setUp(self):
        saved = os.environ.get("TZ")

        def restore():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = _defaults.load()
        self.d["limits"]["sensor_dir"] = os.path.join(self.tmp.name, "usage")
        self.now = calendar.timegm((2026, 10, 21, 18, 0, 0))

    def at(self, day, hour, minute=0):
        return calendar.timegm((2026, 10, day, hour, minute, 0))

    def test_limit_screen_plain(self):
        r = limits.limit_screen(_read("limit_screen.txt"), self.d, self.now)
        self.assertEqual(r, {"kind": "limit", "resets_at": None})
        self.assertIsNone(limits.limit_screen("● all fine\n> \n", self.d, self.now))

    def test_limit_screen_resets_at(self):
        r = limits.limit_screen("usage limit reached\nresets 7pm\n", self.d, self.now)
        self.assertEqual(r["resets_at"], self.at(21, 19))

    def test_limit_choice_wait_option_never_upgrade(self):
        r = limits.limit_screen(_read("limit_choice.txt"), self.d, self.now)
        self.assertEqual(r["kind"], "limit_choice")
        self.assertEqual(r["highlighted"], 1)
        self.assertTrue(r["options"][1].lower().startswith("upgrade"))
        self.assertEqual(r["wait_option"], 0)
        self.assertEqual(r["resets_at"], self.at(21, 19))

    def test_limit_choice_without_wait_option(self):
        text = _read("limit_choice.txt").replace("Stop and wait for limit to reset",
                                                  "Switch model")
        r = limits.limit_screen(text, self.d, self.now)
        self.assertEqual(r["kind"], "limit")  # a plain limit screen; no wait option to pick
        self.assertNotIn("wait_option", r)

    def test_limit_in_scrollback_ignored(self):
        text = _read("limit_in_scrollback.txt")
        self.assertIsNone(limits.limit_screen(text, self.d, self.now))
        pats = self.d["limits"]["message_patterns"]
        self.assertTrue(limits.detect_messages(text, pats))
        self.assertFalse(limits.detect_messages(text, pats, tail_lines=15))
        r = limits.get_limits([text], self.d, now=self.now)
        self.assertFalse(r["reached"])

    def test_parse_resets_variants(self):
        base = calendar.timegm((2026, 10, 21, 18, 0, 0))
        self.assertEqual(limits.parse_resets("resets 7pm", base), self.at(21, 19))
        self.assertEqual(limits.parse_resets("resets 7pm", self.at(21, 20)), self.at(22, 19))
        self.assertEqual(limits.parse_resets("resets 7:30pm", base), self.at(21, 19, 30))
        self.assertEqual(limits.parse_resets("resets 19:00", base), self.at(21, 19))
        self.assertEqual(limits.parse_resets("resets 12am", base), self.at(22, 0))
        self.assertEqual(limits.parse_resets("resets 7pm (Europe/Berlin)", self.at(21, 15)),
                         self.at(21, 17))  # 19:00 CEST is 17:00 UTC
        self.assertIsNone(limits.parse_resets("resets 7pm (Nowhere/Land)", base))
        self.assertIsNone(limits.parse_resets("resets soon", base))
        self.assertIsNone(limits.parse_resets("resets 7", base))
        self.assertIsNone(limits.parse_resets("", base))

    def test_parse_resets_zone_names_under_foreign_tz(self):
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        base = self.at(21, 15)
        self.assertEqual(limits.parse_resets("resets 7pm (UTC)", base), self.at(21, 19))
        self.assertEqual(limits.parse_resets("resets 7pm (GMT)", base), self.at(21, 19))
        self.assertEqual(limits.parse_resets("resets 7pm (Europe/Berlin)", base), self.at(21, 17))
        self.assertIsNone(limits.parse_resets("resets 7pm (XYZ)", base))

    def test_provider_error_final(self):
        self.assertEqual(limits.provider_error(_read("provider_error.txt"), self.d),
                         "quota_exceeded")
        self.assertIsNone(limits.provider_error(_read("limit_screen.txt"), self.d))

    def test_provider_error_only_in_bottom_block(self):
        text = "quota_exceeded\n" + "x\n" * 30
        self.assertIsNone(limits.provider_error(text, self.d))

    def test_messages_source_carries_reset(self):
        r = limits.get_limits(["blah", _read("limit_choice.txt")], self.d, now=self.now)
        self.assertEqual(r["source"], "messages")
        self.assertTrue(r["reached"])
        self.assertEqual(r["five_hour_resets_at"], self.at(21, 19))

    def test_pane_texts_callable_not_called_with_fresh_sensor(self):
        usage = self.d["limits"]["sensor_dir"]
        os.makedirs(usage)
        with open(os.path.join(usage, "s1.json"), "w") as fh:
            json.dump(SENSOR, fh)
        m = os.path.getmtime(os.path.join(usage, "s1.json"))
        calls = []

        def texts():
            calls.append(1)
            return ["blah"]
        self.assertEqual(limits.get_limits(texts, self.d, now=m + 1)["source"], "sensor")
        self.assertEqual(calls, [])
        os.unlink(os.path.join(usage, "s1.json"))
        limits.get_limits(texts, self.d, now=m + 1)
        self.assertEqual(calls, [1])


class TestLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sensor = os.path.join(self.tmp.name, "usage")
        os.makedirs(self.sensor)
        self.d = _defaults.load()
        self.d["limits"]["sensor_dir"] = self.sensor

    def _write_sensor(self, data=SENSOR, name="s1.json", age=0):
        path = os.path.join(self.sensor, name)
        with open(path, "w") as fh:
            json.dump(data, fh)
        t = time.time() - age
        os.utime(path, (t, t))
        return os.path.getmtime(path)

    def _sess(self, name, five, five_at, age=0):
        return self._write_sensor({"rate_limits": {"five_hour": {
            "used_percentage": five, "resets_at": five_at}}}, name, age)

    def test_fixture_has_nbsp(self):
        self.assertIn(" ", _read("statusline_wide.txt"))

    def test_statusline_nbsp(self):
        r = limits.parse_statusline(_read("statusline_wide.txt"))
        self.assertEqual(r["five_hour"], 17.0)
        self.assertEqual(r["seven_day"], 26.0)
        self.assertEqual(r["source"], "statusline")
        self.assertFalse(r["reached"])

    def test_narrow_returns_none(self):
        self.assertIsNone(limits.parse_statusline(_read("statusline_narrow.txt")))

    def test_sensor_fresh(self):
        m = self._write_sensor()
        r = limits.read_sensor(self.sensor, 300, now=m + 10)
        self.assertEqual(r["five_hour"], 17.0)
        self.assertEqual(r["seven_day"], 26.0)
        self.assertEqual(r["five_hour_resets_at"], 1761060000)
        self.assertEqual(r["seven_day_resets_at"], 1761400000)
        self.assertEqual(r["source"], "sensor")

    def test_statusline_countdown(self):
        r = limits.parse_statusline(_read("statusline_wide.txt"))
        self.assertEqual(r["five_hour_resets_in_s"], 4 * 3600 + 7 * 60)
        r = limits.parse_statusline("5h \u00a0▓░ 9.0% (2d3h) │ wk ▓ 1.0% (4d21h7m)")
        self.assertEqual(r["five_hour_resets_in_s"], 2 * 86400 + 3 * 3600)
        r = limits.parse_statusline("5h ▓░ 9.0% (45m)")
        self.assertEqual(r["five_hour_resets_in_s"], 45 * 60)
        self._write_sensor({"rate_limits": {"five_hour": {
            "used_percentage": 5.0, "resets_at": 1761060000}}}, "s2.json")
        r = limits.read_sensor(self.sensor, 300, now=1761060000 - 600)
        self.assertEqual(r["five_hour_resets_in_s"], 600)
        self.assertIsNone(limits.parse_statusline("5h ▓ 9.0% │ wk ▓ 1.0%")
                          ["five_hour_resets_in_s"])
        self.assertIsNone(limits.parse_statusline(_read("statusline_narrow.txt")))

    def test_sensor_stale_falls_through(self):
        m = self._write_sensor()
        self.assertIsNone(limits.read_sensor(self.sensor, 300, now=m + 301))
        r = limits.get_limits([_read("statusline_wide.txt")], self.d, now=m + 301)
        self.assertEqual(r["source"], "statusline")
        self.assertEqual(r["five_hour"], 17.0)

    def test_sensor_missing_or_garbage(self):
        self.assertIsNone(limits.read_sensor(self.sensor, 300))
        with open(os.path.join(self.sensor, "bad.json"), "w") as fh:
            fh.write("{nope")
        self.assertIsNone(limits.read_sensor(self.sensor, 300))
        self._write_sensor({"rate_limits": {}})
        self.assertIsNone(limits.read_sensor(self.sensor, 300))
        self.assertIsNone(limits.read_sensor(os.path.join(self.tmp.name, "nodir"), 300))

    def test_get_limits_prefers_fresh_sensor(self):
        m = self._write_sensor()
        r = limits.get_limits([_read("statusline_wide.txt")], self.d, now=m + 1)
        self.assertEqual(r["source"], "sensor")

    def test_messages_only(self):
        r = limits.get_limits(["blah", "Claude usage limit reached |1761"], self.d)
        self.assertEqual(r["source"], "messages")
        self.assertTrue(r["reached"])
        self.assertIsNone(r["five_hour"])
        self.assertIsNone(r["seven_day"])

    def test_detect_messages_case_insensitive(self):
        self.assertTrue(limits.detect_messages("USAGE LIMIT REACHED", ["usage limit reached"]))
        self.assertFalse(limits.detect_messages("all fine", ["usage limit reached"]))

    def test_unknown(self):
        r = limits.get_limits(["nothing", _read("statusline_narrow.txt")], self.d)
        self.assertEqual(r["source"], "unknown")
        self.assertFalse(r["reached"])
        self.assertIsNone(r["five_hour"])

    # ---- Amendment L: stale idle sessions -----------------------------------

    CTX = ("Opus 5.5 · xhigh │ ▓▓░░░░░░░░\u00a023.0% │ ⎇ main (+0,-0) │ 11m "
           "5h\u00a0▓▓▓░░░░░░░\u00a027.0% (2h22m) │ wk\u00a0▓▓░░░░░░░░\u00a026.0% (4d21h7m)")

    @staticmethod
    def _line(five, five_cd, wk=None, wk_cd="4d21h7m"):
        t = "Opus 5.5 │ ▓░░░░░░░░░ 9.0%% │ 5h ▓▓░░░░░░░░ %.1f%% (%s)" % (five, five_cd)
        if wk is not None:
            t += " │ wk ▓▓░░░░░░░░ %.1f%% (%s)" % (wk, wk_cd)
        return t

    def test_max_over_panes(self):
        panes = [self._line(17.0, "4h7m", 26.0), self._line(28.0, "2h1m", 30.0)]
        r = limits.get_limits(panes, self.d)
        self.assertEqual((r["five_hour"], r["seven_day"]), (28.0, 30.0))
        self.assertEqual(r["source"], "statusline")
        self.assertEqual(r["five_hour_resets_in_s"], 2 * 3600 + 60)

    def test_zero_countdown_pane_ignored(self):
        panes = [self._line(70.0, "0m", 60.0, "0m"), self._line(5.0, "4h59m", 10.0)]
        r = limits.get_limits(panes, self.d)
        self.assertEqual((r["five_hour"], r["seven_day"]), (5.0, 10.0))
        self.assertEqual(r["five_hour_resets_in_s"], 4 * 3600 + 59 * 60)

    def test_truncated_countdown_still_counts(self):
        panes = [self._line(17.0, "4h7m"), "Opus │ ▓ 1.0% │ 5h ▓▓░░ 40.0%"]
        r = limits.get_limits(panes, self.d)
        self.assertEqual(r["five_hour"], 40.0)
        self.assertEqual(r["five_hour_resets_in_s"], 4 * 3600 + 7 * 60)

    def test_narrow_only_week_none(self):
        r = limits.get_limits(["Opus │ ▓ 1.0% │ 11m 5h ▓▓░░ 40.0% (3h2m)"], self.d)
        self.assertEqual(r["five_hour"], 40.0)
        self.assertIsNone(r["seven_day"])

    def test_context_is_first_bar(self):
        self.assertEqual(limits.statusline_context(self.CTX), 23.0)
        self.assertIsNone(limits.statusline_context("no bars here"))
        self.assertIsNone(limits.statusline_context(_read("statusline_wide.txt")))

    def test_sensor_same_reset_takes_max(self):
        self._sess("a.json", 17.0, 1761060000)
        self._sess("b.json", 28.0, 1761060000)
        r = limits.read_sensor(self.sensor, 300)
        self.assertEqual(r["five_hour"], 28.0)

    def test_sensor_previous_window_ignored(self):
        self._sess("old.json", 70.0, 1761040000)
        self._sess("new.json", 5.0, 1761060000)
        r = limits.read_sensor(self.sensor, 300)
        self.assertEqual((r["five_hour"], r["five_hour_resets_at"]), (5.0, 1761060000))

    def test_sensor_all_stale_falls_through(self):
        self._sess("a.json", 70.0, 1761060000, age=1000)
        self.assertIsNone(limits.read_sensor(self.sensor, 300))
        r = limits.get_limits([_read("statusline_wide.txt")], self.d)
        self.assertEqual((r["source"], r["five_hour"]), ("statusline", 17.0))

    def test_sensor_stale_file_ignored_among_fresh(self):
        self._sess("stale.json", 90.0, 1761060000, age=1000)
        self._sess("fresh.json", 11.0, 1761060000)
        self.assertEqual(limits.read_sensor(self.sensor, 300)["five_hour"], 11.0)

    def test_sensor_resets_at_tolerance(self):
        T = 1761060000
        self._sess("a.json", 17.0, T)
        self._sess("b.json", 28.0, T + 120)
        r = limits.read_sensor(self.sensor, 300)
        self.assertEqual((r["five_hour"], r["five_hour_resets_at"]), (28.0, T + 120))
        self._sess("b.json", 28.0, T + 600)
        self.assertEqual(limits.read_sensor(self.sensor, 300)["five_hour"], 28.0)
        self._sess("a.json", 28.0, T)
        self._sess("b.json", 17.0, T + 120)  # same window: the maximum, not the latest file
        r = limits.read_sensor(self.sensor, 300)
        self.assertEqual((r["five_hour"], r["five_hour_resets_at"]), (28.0, T + 120))
        self._sess("a.json", 17.0, T)
        self._sess("b.json", 5.0, T + 601)
        r = limits.read_sensor(self.sensor, 300)
        self.assertEqual((r["five_hour"], r["five_hour_resets_at"]), (5.0, T + 601))

    def test_sensor_previous_window_beyond_tolerance(self):
        T = 1761060000
        self._sess("old.json", 70.0, T - 3600)
        self._sess("new.json", 5.0, T)
        self.assertEqual(limits.read_sensor(self.sensor, 300)["five_hour"], 5.0)

    def test_prune_sensor_deletes_only_old_files_in_dir(self):
        old = time.time() - 90000
        for name in ("old.json", ".tmp.123"):
            self._write_sensor(name=name, age=90000)
        self._write_sensor(name="new.json")
        outside = os.path.join(self.tmp.name, "outside.json")
        with open(outside, "w") as fh:
            fh.write("{}")
        os.utime(outside, (old, old))
        os.makedirs(os.path.join(self.sensor, "sub"))
        os.symlink(outside, os.path.join(self.sensor, "link.json"))
        n = limits.prune_sensor(self.sensor, 86400, now=time.time())
        self.assertEqual(n, 2)
        self.assertEqual(sorted(os.listdir(self.sensor)), ["link.json", "new.json", "sub"])
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(limits.prune_sensor(os.path.join(self.tmp.name, "nodir"), 1), 0)

    def test_get_limits_prunes(self):
        self._write_sensor(name="old.json", age=90000)
        self._sess("new.json", 5.0, 1761060000)
        limits.get_limits([], self.d)
        self.assertEqual(os.listdir(self.sensor), ["new.json"])


def _sensor_reading(ts, five=None, resets=None, week=None, week_resets=None, source="sensor"):
    return {"ts": ts, "source": source, "five_hour": five, "seven_day": week,
            "resets_at": resets, "seven_day_resets_at": week_resets}


class TestRate(unittest.TestCase):
    R = 1761060000

    def setUp(self):
        self.d = _defaults.load()
        # a defaults dict whose week default is null: the unknown-week path
        self.d_null = _defaults.load()
        self.d_null["rate"]["default_week_usd_per_pp"] = None
        self.t = 1761000000

    def five(self, minutes, pct, resets=None):
        return _sensor_reading(self.t + minutes * 60, pct, self.R if resets is None else resets)

    def week(self, minutes, pct, resets):
        return _sensor_reading(self.t + minutes * 60, week=pct, week_resets=resets)

    def test_rate_same_window_only(self):
        rs = [self.five(0, 10), self.five(30, 12, self.R + 18000), self.five(60, 14, self.R + 18000)]
        iv = limits.rate_intervals(rs, "five_hour", self.d)
        self.assertEqual(iv, [(self.t + 1800, self.t + 3600, 2.0)])

    def test_rate_interval_needs_span_and_rise(self):
        d = self.d
        self.assertEqual(limits.rate_intervals([self.five(0, 10), self.five(9, 12)], "five_hour", d), [])
        self.assertEqual(limits.rate_intervals([self.five(0, 10), self.five(12, 10.5)], "five_hour", d), [])
        iv = limits.rate_intervals([self.five(0, 10), self.five(12, 12)], "five_hour", d)
        self.assertEqual(iv, [(self.t, self.t + 720, 2.0)])

    def test_rate_skipped_reading_keeps_anchor(self):
        rs = [self.five(0, 10), self.five(5, 10.2), self.five(12, 12)]
        self.assertEqual(limits.rate_intervals(rs, "five_hour", self.d), [(self.t, self.t + 720, 2.0)])

    def test_rate_lower_percentage_is_new_anchor(self):
        rs = [self.five(0, 50), self.five(20, 5), self.five(40, 8)]
        self.assertEqual(limits.rate_intervals(rs, "five_hour", self.d),
                         [(self.t + 1200, self.t + 2400, 3.0)])

    def test_rate_reset_tolerance(self):
        rs = [self.five(0, 10), self.five(12, 12, self.R + 300)]
        self.assertEqual(len(limits.rate_intervals(rs, "five_hour", self.d)), 1)

    def test_rate_ignores_statusline_readings(self):
        rs = [_sensor_reading(self.t, 10, self.R, source="statusline"),
              _sensor_reading(self.t + 1200, 20, self.R, source="statusline")]
        rates = limits.estimate_rate(rs, lambda a, b: 1.0, self.d, self.t + 1300)
        self.assertEqual((rates["usd_per_pp"], rates["source"]), (3.5, "default"))
        wk = limits.estimate_rate(rs, lambda a, b: 1.0, self.d, self.t + 1300, "seven_day")
        self.assertEqual((wk["usd_per_pp"], wk["source"]), (10.0, "default"))
        wk = limits.estimate_rate(rs, lambda a, b: 1.0, self.d_null, self.t + 1300, "seven_day")
        self.assertEqual((wk["usd_per_pp"], wk["source"]), (None, "unknown"))

    def test_estimate_rate_sum_over_sum(self):
        rs = [self.five(0, 10), self.five(20, 12), self.five(40, 16)]
        spend = {self.t: 10.0, self.t + 1200: 30.0}
        r = limits.estimate_rate(rs, lambda a, b: spend[a], self.d, self.t + 3000)
        self.assertAlmostEqual(r["usd_per_pp"], 40.0 / 6.0)
        self.assertEqual((r["intervals"], r["source"], r["usd"], r["pp"], r["window"], r["at"]),
                         (2, "estimated", 40.0, 6.0, "five_hour", self.t + 3000))

    def test_rate_zero_spend_falls_back_to_default(self):
        rs = [self.five(0, 10), self.five(20, 12)]
        r = limits.estimate_rate(rs, lambda a, b: 0.0, self.d, self.t + 1500)
        self.assertEqual((r["usd_per_pp"], r["source"], r["intervals"]), (3.5, "default", 0))
        self.assertEqual(limits.to_pp(7.0, r), 2.0)
        wk = [self.week(0, 10, 1761500000), self.week(20, 12, 1761500000)]
        r = limits.estimate_rate(wk, lambda a, b: 0.0, self.d, self.t + 1500, "seven_day")
        self.assertEqual((r["usd_per_pp"], r["source"]), (10.0, "default"))
        r = limits.estimate_rate(wk, lambda a, b: 0.0, self.d_null, self.t + 1500, "seven_day")
        self.assertEqual((r["usd_per_pp"], r["source"]), (None, "unknown"))

    def test_rate_window_horizon(self):
        rs = [self.five(0, 10), self.five(20, 12)]
        r = limits.estimate_rate(rs, lambda a, b: 4.0, self.d, self.t + 1200 + 6 * 3600)
        self.assertEqual((r["intervals"], r["source"]), (0, "default"))
        r = limits.estimate_rate(rs, lambda a, b: 4.0, self.d, self.t + 1200 + 3600)
        self.assertEqual(r["intervals"], 1)

    def test_rate_week_current_window_only(self):
        w1, w2 = 1761500000, 1761500000 + 7 * 86400
        rs = [self.week(0, 10, w1), self.week(20, 12, w1)]
        r = limits.estimate_rate(rs, lambda a, b: 4.0, self.d, self.t + 3000, "seven_day")
        self.assertEqual((r["intervals"], r["usd_per_pp"], r["source"]), (1, 2.0, "estimated"))
        rs += [self.week(60, 1, w2)]
        r = limits.estimate_rate(rs, lambda a, b: 4.0, self.d, self.t + 4000, "seven_day")
        self.assertEqual((r["intervals"], r["usd_per_pp"], r["source"]), (0, 10.0, "default"))
        r = limits.estimate_rate(rs, lambda a, b: 4.0, self.d_null, self.t + 4000, "seven_day")
        self.assertEqual((r["intervals"], r["usd_per_pp"], r["source"]), (0, None, "unknown"))

    def test_rate_week_needs_week_reset_time(self):
        rs = [_sensor_reading(self.t, week=10), _sensor_reading(self.t + 1200, week=12)]
        self.assertEqual(limits.rate_intervals(rs, "seven_day", self.d), [])

    def test_rate_defaults(self):
        r = limits.rates([], self.d, self.t)
        self.assertEqual((r["five_hour"]["usd_per_pp"], r["five_hour"]["source"]), (3.5, "default"))
        self.assertEqual((r["seven_day"]["usd_per_pp"], r["seven_day"]["source"]), (10.0, "default"))

    def test_rate_week_default_null_is_unknown(self):
        r = limits.rates([], self.d_null, self.t)
        self.assertEqual((r["five_hour"]["usd_per_pp"], r["five_hour"]["source"]), (3.5, "default"))
        self.assertEqual((r["seven_day"]["usd_per_pp"], r["seven_day"]["source"]), (None, "unknown"))

    def test_to_pp(self):
        self.assertEqual(limits.to_pp(10.0, {"usd_per_pp": 5.0}), 2.0)
        self.assertIsNone(limits.to_pp(10.0, {"usd_per_pp": None}))

    def test_rates_reads_machine_spend(self):
        import datetime
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proj = os.path.join(tmp.name, ".claude", "projects", "-home-user-demo-app")
        os.makedirs(proj)

        def msg(i, ts):
            iso = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
            return json.dumps({"type": "assistant", "timestamp": iso, "message": {
                "id": "m%d" % i, "model": "claude-opus-5-5",
                "usage": {"input_tokens": 1000000, "output_tokens": 0}}})
        with open(os.path.join(proj, "s.jsonl"), "w") as fh:
            fh.write(msg(1, self.t + 100) + "\n" + msg(2, self.t + 600) + "\n"
                     + msg(3, self.t - 100) + "\n" + msg(4, self.t + 1300) + "\n")
        rs = [self.five(0, 10), self.five(20, 12)]
        r = limits.rates(rs, self.d, self.t + 1500, home=tmp.name)["five_hour"]
        self.assertAlmostEqual(r["usd"], 8.0)  # two $4 messages inside [t, t+1200)
        self.assertAlmostEqual(r["usd_per_pp"], 4.0)


class TestWeekBurn(unittest.TestCase):
    W1 = 1761500000
    W0 = W1 - 7 * 86400
    H = 3600

    def setUp(self):
        self.d = _defaults.load()
        self.t = self.W1 - 4 * 86400  # the week is half over

    def wk(self, hours, pct, resets=None, source="sensor"):
        return _sensor_reading(self.t + hours * self.H, week=pct,
                               week_resets=self.W1 if resets is None else resets,
                               source=source)

    def test_week_burn_current_week_only(self):
        rs = [self.wk(-30, 10, self.W0), self.wk(-28, 40, self.W0),
              self.wk(0, 20), self.wk(1, 21), self.wk(2, 22)]
        b = limits.week_burn(rs, self.d, self.t + 2 * self.H)
        self.assertAlmostEqual(b["pp_per_h"], 1.0)
        self.assertEqual((b["source"], b["intervals"], b["resets_at"]), ("measured", 2, self.W1))

    def test_week_burn_never_across_a_reset(self):
        rs = [self.wk(-3, 55, self.W0), self.wk(-2, 60, self.W0), self.wk(0, 2), self.wk(0.1, 2)]
        b = limits.week_burn(rs, self.d, self.t + 0.1 * self.H)
        self.assertIsNone(b["pp_per_h"])  # a fresh week: no interval of its own yet
        self.assertEqual((b["source"], b["reason"]), ("none", "no interval within the horizon"))
        rs += [self.wk(1, 3)]
        self.assertAlmostEqual(limits.week_burn(rs, self.d, self.t + self.H)["pp_per_h"], 1.0)

    def test_week_burn_rise_over_covered_hours(self):
        rs = [self.wk(0, 10), self.wk(1, 12), self.wk(3, 13), self.wk(4, 13.4)]
        b = limits.week_burn(rs, self.d, self.t + 4 * self.H)
        self.assertAlmostEqual(b["pp_per_h"], 0.75)  # 3 pp over 4 h, the last 0.4 not counted
        self.assertEqual((b["intervals"], b["pp"], b["hours"]), (2, 3.0, 4.0))

    def test_week_burn_horizon(self):
        rs = [self.wk(0, 10), self.wk(1, 12)]
        old = limits.week_burn(rs, self.d, self.t + 1 * self.H + 25 * self.H)
        self.assertIsNone(old["pp_per_h"])
        self.assertEqual(old["reason"], "no interval within the horizon")
        b = limits.week_burn(rs, self.d, self.t + 1 * self.H + 23 * self.H)
        self.assertEqual(b["intervals"], 1)
        self.assertAlmostEqual(b["hours"], 1.0)  # from the interval's start to the last reading

    def test_week_burn_needs_sensor_span_and_rise(self):
        sl = [self.wk(0, 10, source="statusline"), self.wk(2, 20, source="statusline")]
        b = limits.week_burn(sl, self.d, self.t + 3 * self.H)
        self.assertEqual((b["pp_per_h"], b["reason"]), (None, "no sensor reading of the current week"))
        quick = [self.wk(0, 10), self.wk(0.15, 12)]  # 9 min
        b = limits.week_burn(quick, self.d, self.t + self.H)
        self.assertEqual((b["pp_per_h"], b["reason"]), (None, "no interval within the horizon"))
        small = [self.wk(0, 10), self.wk(0.2, 10.5)]  # 12 min, +0.5 pp
        b = limits.week_burn(small, self.d, self.t + self.H)
        self.assertEqual((b["pp_per_h"], b["reason"]), (None, "no interval within the horizon"))

    def test_week_burn_past_week_is_not_current(self):
        rs = [self.wk(0, 10), self.wk(1, 12)]
        b = limits.week_burn(rs, self.d, self.W1 + 10)
        self.assertEqual((b["pp_per_h"], b["reason"], b["resets_at"]),
                         (None, "no sensor reading of the current week", None))

    def test_week_burn_equal_timestamps_both_count(self):
        rs = [self.wk(0, 10), self.wk(1, 12), _sensor_reading(self.t + self.H, week=13,
                                                              week_resets=self.W1 + 5)]
        self.assertEqual(len(limits._current_week(rs)[1]), 3)


class TestWorkingHours(unittest.TestCase):
    CAL = {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "19:00"}
    UTC = zoneinfo.ZoneInfo("UTC")

    def ts(self, y, mo, d, h, mi=0, tz=None):
        return datetime.datetime(y, mo, d, h, mi, tzinfo=tz or self.UTC).timestamp()

    def test_working_hours_weekend_and_outside_hours(self):
        f = lambda a, b, cal=None: limits.working_hours(a, b, cal or self.CAL, self.UTC)
        t = self.ts
        self.assertEqual(f(t(2026, 10, 24, 10), t(2026, 10, 26, 12)), 3.0)
        self.assertEqual(f(t(2026, 10, 22, 20), t(2026, 10, 23, 10)), 1.0)
        self.assertEqual(f(t(2026, 10, 21, 12), t(2026, 10, 22, 12)), 10.0)
        self.assertEqual(f(t(2026, 10, 21, 19), t(2026, 10, 23, 19)), 20.0)
        self.assertEqual(f(t(2026, 10, 23, 12), t(2026, 10, 21, 12)), 0.0)
        self.assertEqual(f(t(2026, 10, 21, 12), t(2026, 10, 22, 12), dict(self.CAL, days=[])), 0.0)

    def test_working_hours_local_wall_time_across_dst(self):
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")
        a = self.ts(2026, 10, 24, 9, tz=berlin)
        b = self.ts(2026, 10, 26, 10, tz=berlin)  # the clocks went back on Sunday
        self.assertEqual(limits.working_hours(a, b, self.CAL, berlin), 1.0)

    def test_working_hours_bad_calendar(self):
        with self.assertRaises(ValueError):
            limits.working_hours(0, 86400, dict(self.CAL, start="19:00", end="09:00"), self.UTC)


class TestWeekProjection(unittest.TestCase):
    UTC = zoneinfo.ZoneInfo("UTC")

    def setUp(self):
        self.d = _defaults.load()
        self.now = datetime.datetime(2026, 10, 21, 19, tzinfo=self.UTC).timestamp()
        self.reset = self.now + 48 * 3600

    def proj(self, used, pace, resets="reset", d=None, lim_extra=None, burn=True):
        lim = {"seven_day": used}
        if resets:
            lim["seven_day_resets_at"] = self.reset
        lim.update(lim_extra or {})
        b = {"pp_per_h": pace, "resets_at": None} if burn else None
        return limits.week_projection(lim, b, d or self.d, self.now, self.UTC)

    def no_calendar(self):
        d = _defaults.load()
        d["week"]["calendar"]["days"] = []
        return d

    def test_week_projection_arithmetic(self):
        p = self.proj(50.0, 0.3)
        self.assertEqual((p["hours_to_reset"], p["working_hours"], p["reserve_pp"]), (48.0, 20.0, 20.0))
        self.assertAlmostEqual(p["projected"], 84.4)
        self.assertEqual((p["basis"], p["allows"], p["level"]), ("projection", "normal", 0))
        self.assertEqual(p["resets_at"], self.reset)

    def test_week_projection_levels(self):
        d = self.no_calendar()
        got = {u: self.proj(u, 0.0, d=d) for u in (95.0, 94.99, 100.0, 80.0, 80.01)}
        self.assertEqual(got[95.0]["allows"], "no_new_mass")
        self.assertEqual(got[94.99]["allows"], "normal")
        self.assertEqual(got[100.0]["allows"], "clearance_only")
        self.assertEqual(got[80.0]["allows"], "room")
        self.assertEqual(got[80.01]["allows"], "normal")
        self.assertEqual([got[u]["level"] for u in (95.0, 94.99, 100.0, 80.0)], [2, 0, 3, 0])
        self.assertEqual(got[80.0]["projected"], 80.0)

    def test_week_projection_fallback(self):
        for used, allows in ((49.9, "normal"), (50, "notify"), (70, "clearance_only"), (10, "normal")):
            p = self.proj(used, None)
            self.assertEqual((p["basis"], p["reason"], p["allows"], p["projected"]),
                             ("fallback", "no measured burn", allows, None))
        self.assertEqual([self.proj(u, None, burn=False)["level"] for u in (50, 70)], [1, 3])
        p = self.proj(60.0, 0.5, resets=None)  # a burn but no reset time anywhere
        self.assertEqual((p["basis"], p["reason"], p["allows"]),
                         ("fallback", "reset time unknown", "notify"))

    def test_week_projection_stale_sensor_keeps_reset(self):
        lim = {"seven_day": 50.0, "seven_day_resets_at": None}
        p = limits.week_projection(lim, {"pp_per_h": 0.3, "resets_at": self.reset}, self.d,
                                   self.now, self.UTC)
        self.assertEqual((p["basis"], p["resets_at"]), ("projection", self.reset))
        p = limits.week_projection(lim, {"pp_per_h": 0.3, "resets_at": self.now - 5}, self.d,
                                   self.now, self.UTC)
        self.assertEqual((p["basis"], p["reason"]), ("fallback", "reset time unknown"))

    def test_week_projection_unknown_week(self):
        p = limits.week_projection({"seven_day": None}, {"pp_per_h": 1.0}, self.d, self.now)
        self.assertEqual((p["basis"], p["allows"], p["level"], p["projected"]),
                         ("unknown", "unknown", 0, None))
        self.assertEqual(limits.week_projection({}, None, self.d, self.now)["allows"], "unknown")


class TestCliWeek(unittest.TestCase):
    def test_cli_week(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = {k: os.environ.get(k) for k in ("HOME", "PRORAB_LOG_DIR")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ["HOME"] = tmp.name
        os.environ["PRORAB_LOG_DIR"] = os.path.join(tmp.name, "logs")
        usage = os.path.join(tmp.name, ".cache", "prorab", "usage")
        os.makedirs(usage)
        now = time.time()
        reset = now + 48 * 3600
        with open(os.path.join(usage, "s1.json"), "w") as fh:
            json.dump({"rate_limits": {"five_hour": {"used_percentage": 5.0, "resets_at": now + 3600},
                                       "seven_day": {"used_percentage": 50.0, "resets_at": reset}}}, fh)
        log = os.path.join(tmp.name, "monitor.log")
        with open(log, "w") as fh:
            for h, pct in ((-3, 47.0), (-2, 48.0), (-1, 49.0), (0, 50.0)):
                fh.write(json.dumps(_sensor_reading(now + h * 3600, week=pct, week_resets=reset)) + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = limits.main(["week", "--log", log, "--now", str(now)])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["used"], 50.0)
        self.assertAlmostEqual(out["burn"]["pp_per_h"], 1.0)
        self.assertEqual(out["basis"], "projection")
        self.assertIsNotNone(out["projected"])
        self.assertIn(out["allows"], ("room", "normal", "no_new_mass", "clearance_only"))


if __name__ == "__main__":
    unittest.main()
