"""Every bd, herdr and git call has a timeout. Fast: no real bd, git or herdr; fake
executables that sleep stand in for them and the timeouts are lowered to 1 s."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(HERE), "scripts")
FAKES = os.path.join(HERE, "fakes")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import crew  # noqa: E402
import herdr_cli  # noqa: E402
import journal  # noqa: E402

REAL_LOAD = _defaults.load


def short_load(path=None):
    d = REAL_LOAD(path)
    d["timeouts"] = {"bd_s": 1, "git_s": 1, "herdr_s": 1}
    return d


class SleepyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        for name in ("bd", "git"):
            path = os.path.join(bindir, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\nsleep 10\n")
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        env = {"PATH": bindir + os.pathsep + FAKES + os.pathsep + os.environ.get("PATH", ""),
               "FAKE_HERDR_DIR": self.dir, "HERDR_ENV": "1",
               "PRORAB_BEADS_DIR": os.path.join(self.dir, ".beads")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(_defaults, "load", short_load)
        p.start()
        self.addCleanup(p.stop)


class TestTimeouts(SleepyBase):
    def test_bd_timeout_is_an_error(self):
        t0 = time.monotonic()
        with self.assertRaises(journal.JournalError) as cm:
            journal.bd(["list"])
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("bd list", str(cm.exception))
        self.assertIn("timed out after 1 s", str(cm.exception))

    def test_git_timeout_is_an_error(self):
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            crew._git(["status"], self.dir)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("git status", str(cm.exception))
        self.assertIn("timed out after 1 s", str(cm.exception))

    def test_herdr_timeout_is_an_error(self):
        with open(os.path.join(self.dir, "out.txt"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(self.dir, "routes.json"), "w") as fh:
            json.dump({"agent list": {"file": "out.txt", "sleep": 10}}, fh)
        t0 = time.monotonic()
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            herdr_cli.run(["agent", "list"])
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(cm.exception.code, "cli_timeout")
        self.assertIn("timed out after 1 s", str(cm.exception))

    def test_herdr_text_timeout_is_an_error(self):
        with open(os.path.join(self.dir, "out.txt"), "w") as fh:
            fh.write("x")
        with open(os.path.join(self.dir, "routes.json"), "w") as fh:
            json.dump({"pane read": {"file": "out.txt", "sleep": 10}}, fh)
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            herdr_cli.pane_visible("p1")
        self.assertEqual(cm.exception.code, "cli_timeout")

    def test_check_report_git_timeout_is_a_refusal(self):
        text = "Result: delivered\nCommit: abc1234\n"
        r = crew.check_report(text, [], [], self.dir)
        self.assertFalse(r["ok"])
        self.assertTrue(any("timed out" in p for p in r["problems"]), r["problems"])

    def test_git_soft_keeps_the_failing_process_contract(self):
        p = crew._git_soft(["status"], self.dir)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("timed out after 1 s", p.stderr)


if __name__ == "__main__":
    unittest.main()
