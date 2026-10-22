"""The bounded parallel runner, against synthetic test packages in temp directories.
These tests never run the real suite (no recursion)."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "run.py")
sys.path.insert(0, HERE)

import run  # noqa: E402


def make_package(root, files):
    pkg = os.path.join(root, "synth")
    os.makedirs(pkg)
    with open(os.path.join(pkg, "__init__.py"), "w") as fh:
        fh.write("")
    for name, body in files.items():
        with open(os.path.join(pkg, name + ".py"), "w") as fh:
            fh.write("import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n" + body)


PASSING = "    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING = "    def test_bad(self):\n        self.assertTrue(False)\n"
SLEEPING = "    def test_hang(self):\n        time.sleep(60)\n"
SILENT = ("    def test_quiet(self):\n        time.sleep(60)\n")


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "root")
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.root)

    def go(self, *args, timeout=40):
        t0 = time.monotonic()
        env = {k: v for k, v in os.environ.items() if k != "PRORAB_FAST"}
        p = subprocess.run([sys.executable, RUN, "--root", self.root, "--log-dir", self.logs]
                           + list(args), capture_output=True, text=True, timeout=timeout, env=env)
        return p, time.monotonic() - t0

    def test_groups_split_bd_modules(self):
        mods = ["prorab.tests.test_crew", "prorab.tests.test_journal",
                "prorab.tests.test_herdr_cli", "prorab.tests.test_shift"]
        self.assertEqual(run.group_modules(mods), [
            ("test_journal", ["prorab.tests.test_journal"]),
            ("test_shift", ["prorab.tests.test_shift"]),
            ("rest", ["prorab.tests.test_crew", "prorab.tests.test_herdr_cli"])])
        self.assertEqual(run.group_modules(["a.test_x"]), [("rest", ["a.test_x"])])

    def test_bound_kills_a_hung_group(self):
        make_package(self.root, {"test_hang": SLEEPING})
        p, took = self.go("--bound", "2", "synth.test_hang")
        self.assertNotEqual(p.returncode, 0)
        self.assertLess(took, 15)
        self.assertIn("bound", p.stdout)
        with open(os.path.join(self.logs, "rest.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("most recent call first", log)  # the faulthandler stack dump
        self.assertIn("test_hang", log)

    def test_stall_kills_a_silent_group(self):
        make_package(self.root, {"test_quiet": SILENT})
        p, took = self.go("--stall", "2", "synth.test_quiet")
        self.assertNotEqual(p.returncode, 0)
        self.assertLess(took, 15)
        self.assertIn("stall", p.stdout)
        with open(os.path.join(self.logs, "rest.log"), encoding="utf-8") as fh:
            self.assertIn("most recent call first", fh.read())

    def test_summary_and_exit_code(self):
        make_package(self.root, {"test_a": PASSING, "test_b": FAILING})
        p, _ = self.go("synth.test_a", "synth.test_b")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("rest", p.stdout)
        self.assertIn("failed", p.stdout)
        self.assertIn("Slowest tests", p.stdout)
        p, _ = self.go("synth.test_a")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("ok", p.stdout)
        self.assertIn("wall time", p.stdout)

    @staticmethod
    def _kill_sleeper(pidfile):
        try:
            with open(pidfile) as fh:
                os.kill(int(fh.read()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def _interrupted_runner_leaves_nothing(self, sig):
        pidfile = os.path.join(self.tmp.name, "child.pid")
        make_package(self.root, {"test_hang": (
            "    def test_hang(self):\n"
            "        import os\n"
            "        open(%r, 'w').write(str(os.getpid()))\n"
            "        time.sleep(60)\n" % pidfile)})
        env = {k: v for k, v in os.environ.items() if k != "PRORAB_FAST"}
        # Start the runner with SIGINT ignored, as a background job of a non-interactive shell
        # does: the runner must restore the handler itself (it did not, and the test failed
        # whenever the suite itself ran in the background).
        p = subprocess.Popen([sys.executable, RUN, "--root", self.root, "--log-dir", self.logs,
                              "synth.test_hang"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
        self.addCleanup(p.kill)
        self.addCleanup(self._kill_sleeper, pidfile)  # a failing test leaves nothing behind
        deadline = time.monotonic() + 20
        while not os.path.exists(pidfile) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(pidfile), "the test never started")
        time.sleep(0.3)
        with open(pidfile) as fh:
            child = int(fh.read())
        p.send_signal(sig)
        p.wait(timeout=10)
        gone = False
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.1)
        if not gone:
            os.kill(child, signal.SIGKILL)
        self.assertTrue(gone, "the sleeping test process survived the runner")

    def test_sigterm_kills_the_groups(self):
        self._interrupted_runner_leaves_nothing(signal.SIGTERM)

    def test_sigint_kills_the_groups(self):
        self._interrupted_runner_leaves_nothing(signal.SIGINT)

    def test_bd_module_runs_as_its_own_group_in_parallel(self):
        make_package(self.root, {"test_journal": PASSING, "test_other": PASSING})
        p, _ = self.go("synth.test_journal", "synth.test_other")
        self.assertEqual(p.returncode, 0, p.stdout)
        for name in ("test_journal.log", "rest.log"):
            self.assertTrue(os.path.exists(os.path.join(self.logs, name)), name)

    def test_module_skipping_itself_at_import_is_a_skip_not_a_failure(self):
        pkg = os.path.join(self.root, "synth")
        os.makedirs(pkg)
        open(os.path.join(pkg, "__init__.py"), "w").close()
        with open(os.path.join(pkg, "test_skipper.py"), "w") as fh:
            fh.write("import unittest\nraise unittest.SkipTest('not here')\n")
        p, _ = self.go("synth.test_skipper")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertRegex(p.stdout, r"rest\s+ok\s+1\s+0\s+0\s+1")

    def test_fast_sets_the_environment_for_children(self):
        make_package(self.root, {"test_env": (
            "    def test_env(self):\n"
            "        import os\n"
            "        self.assertEqual(os.environ.get('PRORAB_FAST'), '1')\n")})
        p, _ = self.go("--fast", "synth.test_env")
        self.assertEqual(p.returncode, 0, p.stdout)
        p, _ = self.go("synth.test_env")
        self.assertEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()
