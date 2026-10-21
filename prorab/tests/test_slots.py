import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import slots  # noqa: E402

HOLD = ("import os, sys, time\n"
        "open(sys.argv[1] + '.started', 'w').close()\n"
        "while not os.path.exists(sys.argv[1]):\n"
        "    time.sleep(0.02)\n")


class TestSlots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "slots")
        saved = os.environ.get("PRORAB_CACHE")

        def restore():
            if saved is None:
                os.environ.pop("PRORAB_CACHE", None)
            else:
                os.environ["PRORAB_CACHE"] = saved
        self.addCleanup(restore)
        os.environ["PRORAB_CACHE"] = self.tmp.name

    def _hold(self, name):
        marker = os.path.join(self.tmp.name, name)
        out = {}

        def target():
            out["rc"] = slots.run("heavy", [sys.executable, "-c", HOLD, marker],
                                  slots_dir=self.dir, n=2, poll_s=0.02)
        t = threading.Thread(target=target)
        t.start()
        return marker, t, out

    def _wait_for(self, path, timeout=10):
        end = time.time() + timeout
        while not os.path.exists(path):
            self.assertLess(time.time(), end, "timed out waiting for %s" % path)
            time.sleep(0.02)

    def test_slots_third_waits(self):
        m1, t1, o1 = self._hold("one")
        m2, t2, o2 = self._hold("two")
        self._wait_for(m1 + ".started")
        self._wait_for(m2 + ".started")
        m3, t3, o3 = self._hold("three")
        time.sleep(0.4)
        self.assertFalse(os.path.exists(m3 + ".started"))  # both slots are busy
        open(m1, "w").close()  # release one holder
        self._wait_for(m3 + ".started")
        open(m2, "w").close()
        open(m3, "w").close()
        for t in (t1, t2, t3):
            t.join(10)
        self.assertEqual((o1["rc"], o2["rc"], o3["rc"]), (0, 0, 0))

    def test_exit_code_and_default_dir(self):
        rc = slots.run("heavy", [sys.executable, "-c", "raise SystemExit(7)"], n=1)
        self.assertEqual(rc, 7)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "slots", "heavy.0.lock")))

    def test_count_file_overrides_defaults(self):
        self.assertEqual(slots.slot_count("heavy", self.dir), 2)  # defaults.json
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "heavy.count"), "w") as fh:
            fh.write("3\n")
        self.assertEqual(slots.slot_count("heavy", self.dir), 3)
        with open(os.path.join(self.dir, "heavy.count"), "w") as fh:
            fh.write("junk")
        self.assertEqual(slots.slot_count("heavy", self.dir), 2)

    def test_slot_count_falls_back_to_machine(self):
        self.assertEqual(slots.slot_count("heavy", self.dir),
                         _defaults.load()["machine"]["heavy_slots"])

    def test_lock_released_when_command_missing(self):
        with self.assertRaises(OSError):
            slots.run("heavy", ["/nonexistent/cmd-xyz"], slots_dir=self.dir, n=1)
        rc = slots.run("heavy", [sys.executable, "-c", "pass"], slots_dir=self.dir, n=1,
                       poll_s=0.02)
        self.assertEqual(rc, 0)

    def test_grandchild_does_not_keep_slot(self):
        cmd = ["sh", "-c", "sleep 2 >/dev/null 2>&1 &"]
        self.assertEqual(slots.run("heavy", cmd, slots_dir=self.dir, n=1), 0)
        t0 = time.time()
        rc = slots.run("heavy", [sys.executable, "-c", "pass"], slots_dir=self.dir, n=1,
                       poll_s=0.05)
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - t0, 1.5)

    def test_status_and_cli(self):
        m1, t1, o1 = self._hold("one")
        self._wait_for(m1 + ".started")
        st = slots.status(self.dir)
        self.assertEqual(st["heavy"]["busy"], [0])
        self.assertEqual(st["heavy"]["slots"], 2)
        open(m1, "w").close()
        t1.join(10)
        self.assertEqual(slots.status(self.dir)["heavy"]["busy"], [])
        script = os.path.join(SCRIPTS_DIR, "slots.py")
        p = subprocess.run([sys.executable, script, "run", "--kind", "heavy", "--",
                            sys.executable, "-c", "raise SystemExit(3)"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 3)
        p = subprocess.run([sys.executable, script, "status"], capture_output=True, text=True)
        self.assertEqual(json.loads(p.stdout)["heavy"]["busy"], [])

    def test_owner_written_and_removed(self):
        m1, t1, o1 = self._hold("one")
        self._wait_for(m1 + ".started")
        owner = os.path.join(self.dir, "heavy.0.owner")
        with open(owner, encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["cwd"], os.getcwd())
        self.assertEqual(rec["cmd"], sys.executable)
        self.assertIsInstance(rec["pid"], int)
        self.assertIn("started_at", rec)
        open(m1, "w").close()
        t1.join(10)
        self.assertFalse(os.path.exists(owner))
        self.assertFalse([f for f in os.listdir(self.dir) if f.endswith(".tmp")])

    def test_holders_skip_stale_owner(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "heavy.0.owner"), "w", encoding="utf-8") as fh:
            json.dump({"cwd": "/home/user/projects/demo-app", "pid": 1}, fh)
        self.assertEqual(slots.holders(self.dir), [])  # no lock held: stale
        m1, t1, o1 = self._hold("one")
        self._wait_for(m1 + ".started")
        got = slots.holders(self.dir)
        self.assertEqual([h["cwd"] for h in got], [os.getcwd()])
        open(m1, "w").close()
        t1.join(10)
        self.assertEqual(slots.holders(self.dir), [])

    def test_status_lists_owners(self):
        m1, t1, o1 = self._hold("one")
        self._wait_for(m1 + ".started")
        owners = slots.status(self.dir)["heavy"]["owners"]
        self.assertEqual([o["cwd"] for o in owners], [os.getcwd()])
        open(m1, "w").close()
        t1.join(10)
        self.assertEqual(slots.status(self.dir)["heavy"]["owners"], [])


if __name__ == "__main__":
    unittest.main()
