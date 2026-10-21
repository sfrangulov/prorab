import json
import os
import sys
import tempfile
import unittest

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


class TestLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sensor = os.path.join(self.tmp.name, "usage.json")
        self.d = _defaults.load()
        self.d["limits"]["sensor_path"] = self.sensor

    def _write_sensor(self, obj=SENSOR):
        with open(self.sensor, "w") as fh:
            json.dump(obj, fh)
        return os.path.getmtime(self.sensor)

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
            "used_percentage": 5.0, "resets_at": 1761060000}}})
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
        with open(self.sensor, "w") as fh:
            fh.write("{nope")
        self.assertIsNone(limits.read_sensor(self.sensor, 300))
        self._write_sensor({"rate_limits": {}})
        self.assertIsNone(limits.read_sensor(self.sensor, 300))

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


if __name__ == "__main__":
    unittest.main()
