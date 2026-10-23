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


class TestDefaults(unittest.TestCase):
    def test_values_match_spec(self):
        d = _defaults.load()
        self.assertEqual(d["context"]["soft_pct"], 40)
        self.assertEqual(d["context"]["soft_max_tokens"], 500000)
        self.assertEqual(d["context"]["hard_pct"], 80)
        self.assertEqual(d["context"]["windows"], {"claude-opus-5-5": 1000000})
        self.assertEqual(d["context"]["unknown_window"], 200000)

        self.assertEqual(d["limits"]["five_hour"], {"no_new_mass": 60, "stop": 80})
        self.assertNotIn("seven_day", d["limits"])
        self.assertEqual(d["week"], {
            "alert_pct": 95, "clearance_pct": 100, "margin_pp": 20,
            "fallback_notify_pct": 50, "fallback_clearance_pct": 70,
            "burn_horizon_working_h": 10,
            "calendar": {"days": ["mon", "tue", "wed", "thu", "fri"],
                         "start": "09:00", "end": "19:00"}})
        with open(_defaults.DEFAULT_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertNotIn("sensor_path", raw["limits"])
        self.assertEqual(raw["limits"]["sensor_dir"], "~/.cache/prorab/usage")
        self.assertEqual(
            d["limits"]["sensor_dir"],
            os.path.expanduser("~/.cache/prorab/usage"),
        )
        self.assertEqual(d["limits"]["sensor_retention_s"], 86400)
        self.assertEqual(d["limits"]["week_unknown_blind_s"], 1800)
        self.assertEqual(d["limits"]["sensor_max_age_s"], 300)
        self.assertEqual(
            d["limits"]["message_patterns"],
            [
                "usage limit reached",
                "limit will reset",
                "approaching usage limit",
                "you've used",
                "you've hit your limit",
                "/rate-limit-options",
                "stop and wait for limit to reset",
                "api error: rate limit reached",
            ],
        )
        self.assertEqual(
            d["limits"]["limit_screen"],
            ["usage limit reached", "you've hit your limit",
             "you've hit your session limit", "/rate-limit-options"],
        )
        self.assertEqual(
            d["limits"]["limit_choice"], {"wait": "stop and wait", "upgrade": "upgrade"})
        self.assertEqual(
            d["limits"]["fatal_patterns"],
            ["model_not_found", "insufficient_quota", "quota_exceeded"],
        )
        self.assertEqual(
            d["send"],
            {"timeout_ms": 60000,
             "files_dir": os.path.expanduser("~/.cache/prorab/work-orders"),
             "composer_match_chars": 20},
        )
        self.assertEqual(
            d["screen"],
            {"tail_lines": 15, "resume_failed": "no conversation found with session id",
             "worktree_exit": {"marker": "exiting worktree session",
                               "keep": "1. keep worktree"},
             "exit_bg_work": "background work is running",
             "login": [["not logged in", "please run /login"], ["select login method"]]},
        )
        self.assertEqual(d["cycles"], {"approval": 6, "review": 10, "report_fix": 3})
        self.assertEqual(d["handover"], {"min_interval_s": 600})
        self.assertEqual(d["crew"], {
            "ask_timeout_s": 600,
            "trust_path": os.path.expanduser("~/.claude.json"),
            "trust_retry_s": 1,
            "exit_window_s": 300,
        })

        self.assertEqual(
            d["budget"],
            {"usd": 100, "week_pp": 10},
        )
        self.assertEqual(
            d["machine"],
            {"parallel_ceiling": 6, "heavy_slots": 2, "heavy_runs_per_stage": 60},
        )
        self.assertEqual(
            d["cost"],
            {"prices": {
                "opus": {"input": 4.0, "output": 20.0, "cache_read": 0.2},
                "sonnet": {"input": 2.0, "output": 10.0, "cache_read": 0.2},
                "haiku": {"input": 0.1, "output": 0.5, "cache_read": 0.01},
                "fable": {"input": 10.0, "output": 50.0, "cache_read": 0.25}},
             "cache_write_5m_factor": 1.25, "cache_write_1h_factor": 2.0},
        )
        self.assertEqual(
            d["rate"],
            {"default_window_usd_per_pp": 3.5, "default_week_usd_per_pp": 10.0,
             "min_rise_pp": 1, "window_horizon_s": 18000, "every_s": 600},
        )
        self.assertEqual(
            d["limits"]["five_hour_night"],
            {"stop_before_morning": 95,
             "morning_window": {"no_new_mass": 70, "stop": 80}},
        )
        self.assertEqual(d["night"], {"morning": "08:00"})
        self.assertEqual(
            d["schedule"], {"default_duration_min": 30, "burn_min_span_s": 600})
        self.assertEqual(
            d["monitor"],
            {"interval_s": 90, "heartbeat_s": 1200, "hung_s": 1800, "max_restarts": 2,
             "error_polls": 3, "interval_max_s": 300, "backoff_factor": 2,
             "hung_scan_max_files": 20000, "limit_panes": 2,
             "spend_every_s": 300, "reset_grace_s": 120,
             "awake_hold_s": 1200, "awake_min_left_s": 600, "sleep_gap_s": 120,
             "sleep_merge_s": 600},
        )
        self.assertEqual(d["timeouts"], {"bd_s": 30, "git_s": 60, "herdr_s": 60})
        self.assertEqual(d["hooks"], {
            "deny": ["git push", "gh pr create", "gh pr merge", "gh release", "bd init",
                     "sudo ", "brew install", "git reset --hard", "git clean -f",
                     "git checkout main", "git checkout master", "git switch main",
                     "git switch master", "claude auth", "claude login", "claude logout",
                     "claude setup-token", "claude /login"],
            "events_dir": os.path.expanduser("~/.cache/prorab/events"),
            "events_retention_s": 86400,
            "settings_path": os.path.expanduser("~/.cache/prorab/crew-settings.json"),
            "suggestions_setting": {"promptSuggestionEnabled": False}})
        self.assertNotIn("guard_max_ms", d["hooks"])
        self.assertEqual(d["emergency"],
                         {"stop_path": os.path.expanduser("~/.cache/prorab/STOP")})
        self.assertNotIn("polish_cmd", d)

    def test_awake_and_login_defaults(self):
        d = _defaults.load()
        mon = d["monitor"]
        self.assertEqual((mon["awake_hold_s"], mon["awake_min_left_s"], mon["sleep_gap_s"],
                          mon["sleep_merge_s"]), (1200, 600, 120, 600))
        self.assertEqual(d["screen"]["login"],
                         [["not logged in", "please run /login"], ["select login method"]])
        for entry in ("claude auth", "claude login", "claude logout", "claude setup-token",
                      "claude /login"):
            self.assertIn(entry, d["hooks"]["deny"])

    def test_no_script_logs_in(self):
        # a guard: no script ever comes to type a login; the markers live in defaults.json
        scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "scripts")
        for name in sorted(os.listdir(scripts)):
            if not os.path.isfile(os.path.join(scripts, name)):
                continue
            with open(os.path.join(scripts, name), encoding="utf-8") as fh:
                text = fh.read()
            for word in ("/login", "auth login", "setup-token"):
                self.assertNotIn(word, text, "%s holds %r" % (name, word))

    def test_price_table_families(self):
        prices = _defaults.load()["cost"]["prices"]
        self.assertEqual(list(prices), ["opus", "sonnet", "haiku", "fable"])
        for family, row in prices.items():
            self.assertEqual(sorted(row), ["cache_read", "input", "output"], family)
            for col, v in row.items():
                self.assertGreater(v, 0, "%s.%s" % (family, col))

    def test_send_files_dir_expanded(self):
        path = _defaults.load()["send"]["files_dir"]
        self.assertTrue(path.startswith("/"), path)
        self.assertTrue(path.endswith("work-orders"), path)

    def test_tilde_expanded(self):
        path = _defaults.load()["limits"]["sensor_dir"]
        self.assertTrue(path.startswith("/"), path)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "d.json")
            with open(cfg, "w") as fh:
                json.dump({"a_path": "~/x", "b_dir": "~/y", "c": "~/z"}, fh)
            d = _defaults.load(cfg)
        self.assertTrue(d["a_path"].startswith("/") and d["b_dir"].startswith("/"))
        self.assertEqual(d["c"], "~/z")

    def test_cli_argv_spells_options_with_hyphens(self):
        f = _defaults.cli_argv
        self.assertEqual(f(["restart", "--caller_pane", "w:p1"]), ["restart", "--caller-pane", "w:p1"])
        self.assertEqual(f(["--act_file=x"]), ["--act-file=x"])
        self.assertEqual(f(["--text=--a_b"]), ["--text=--a_b"])
        self.assertEqual(f(["a_b", "-x_y", "--a-b", "--Bad_x", "--9_x"]),
                         ["a_b", "-x_y", "--a-b", "--Bad_x", "--9_x"])
        self.assertEqual(f(["run", "--kind_x", "h", "--", "cmd", "--a_b", "--"]),
                         ["run", "--kind-x", "h", "--", "cmd", "--a_b", "--"])
        self.assertEqual(f([]), [])

    def test_every_cli_with_hyphenated_options_rewrites_its_argv(self):
        for name in ("crew", "journal", "shift", "scheduler", "limits"):
            with open(os.path.join(SCRIPTS_DIR, name + ".py"), encoding="utf-8") as fh:
                self.assertIn("_defaults.cli_argv(", fh.read(), name)


if __name__ == "__main__":
    unittest.main()
