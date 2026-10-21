import json
import os
import sys
import unittest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402


class TestDefaults(unittest.TestCase):
    def test_values_match_spec(self):
        d = _defaults.load()
        self.assertEqual(d["context"]["soft_pct"], 60)
        self.assertEqual(d["context"]["soft_max_tokens"], 500000)
        self.assertEqual(d["context"]["hard_pct"], 80)
        self.assertEqual(d["context"]["windows"], {"claude-opus-5-5": 1000000})
        self.assertEqual(d["context"]["unknown_window"], 200000)

        self.assertEqual(d["limits"]["five_hour"], {"no_new_mass": 60, "stop": 80})
        self.assertEqual(d["limits"]["seven_day"], {"notify": 50, "dopusk_only": 70})
        with open(_defaults.DEFAULT_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["limits"]["sensor_path"], "~/.cache/prorab/usage.json")
        self.assertEqual(
            d["limits"]["sensor_path"],
            os.path.expanduser("~/.cache/prorab/usage.json"),
        )
        self.assertEqual(d["limits"]["sensor_max_age_s"], 300)
        self.assertEqual(
            d["limits"]["message_patterns"],
            [
                "usage limit reached",
                "limit will reset",
                "approaching usage limit",
                "you've used",
            ],
        )

        self.assertEqual(
            d["budget"],
            {"sessions": 30, "runs": 200, "week_pp": 10,
             "parallel_ceiling": 6, "heavy_slots": 2},
        )
        self.assertEqual(
            d["limits"]["five_hour_night"],
            {"stop_before_morning": 95,
             "morning_window": {"no_new_mass": 70, "stop": 80}},
        )
        self.assertEqual(d["night"], {"morning": "08:00"})
        self.assertEqual(
            d["schedule"], {"default_estimate_min": 30, "burn_min_span_s": 600})
        self.assertEqual(
            d["monitor"],
            {"interval_s": 90, "heartbeat_s": 1200, "hung_s": 1800, "max_restarts": 2},
        )
        self.assertIsNone(d["polish_cmd"])

    def test_tilde_expanded(self):
        path = _defaults.load()["limits"]["sensor_path"]
        self.assertTrue(path.startswith("/"), path)


if __name__ == "__main__":
    unittest.main()
