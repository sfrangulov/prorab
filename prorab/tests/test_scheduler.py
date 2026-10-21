import io
import json
import os
import stat
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import scheduler  # noqa: E402

D = _defaults.load()


def at(hh, mm=0, day=21):
    """Unix time of 2026-10-<day> hh:mm in the pinned zone (UTC)."""
    return time.mktime((2026, 10, day, hh, mm, 0, 0, 0, 0))


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = {k: os.environ.get(k) for k in ("TZ", "PATH", "PRORAB_LOG_DIR", "HOME")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "UTC"
        time.tzset()

    # ---- critical path ----------------------------------------------------------

    def test_critical_order_two_chains(self):
        graph = {"A": [], "B": ["A"], "C": ["B"], "D": [], "E": ["D"]}
        est = {k: 30.0 for k in graph}
        order = scheduler.critical_order(graph, est)
        self.assertEqual(order, ["A", "B", "D", "C", "E"])  # 90, 60, 60, 30, 30; ties by id

    def test_critical_order_uses_estimates_and_includes_unknown_blockers(self):
        graph = {"X": ["Z"], "Y": []}
        order = scheduler.critical_order(graph, {"X": 10, "Y": 50, "Z": 5})
        self.assertEqual(order, ["Y", "Z", "X"])  # Y 50, Z 15, X 10

    def test_estimate_falls_back_to_history_then_default(self):
        hist = {"code": [10.0, 50.0, 20.0]}
        self.assertEqual(scheduler.estimate({"estimate_min": 7, "kind": "code"}, hist, D), 7)
        self.assertEqual(scheduler.estimate({"estimate_min": None, "kind": "code"}, hist, D), 20.0)
        self.assertEqual(scheduler.estimate({"kind": "docs"}, hist, D), 30)
        self.assertEqual(scheduler.estimate({"kind": None}, hist, D), 30)

    # ---- burn rate --------------------------------------------------------------

    def test_burn_rate_since_last_reset(self):
        r = [(0, 10.0), (3600, 50.0), (7200, 5.0), (10800, 15.0), (14400, 25.0)]
        self.assertAlmostEqual(scheduler.burn_rate(r), 10.0)  # 20 pp over 2 h after the drop
        self.assertIsNone(scheduler.burn_rate([(0, 5.0)]))
        self.assertIsNone(scheduler.burn_rate([(0, 5.0), (300, 9.0)]))  # span < 600 s
        self.assertIsNone(scheduler.burn_rate([(0, 50.0), (3600, 5.0)]))  # one reading after drop

    def test_readings_skips_bad_lines_and_missing_file(self):
        self.assertEqual(scheduler.readings(os.path.join(self.tmp.name, "none.log")), [])
        path = os.path.join(self.tmp.name, "monitor.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 100.0, "five_hour": 10.0}) + "\n")
            fh.write("garbage\n\n")
            fh.write(json.dumps({"ts": 200.0, "five_hour": None}) + "\n")
            fh.write(json.dumps({"ts": 300.0, "five_hour": 12.0}) + "\n")
        self.assertEqual([r["ts"] for r in scheduler.readings(path)], [100.0, 200.0, 300.0])
        self.assertEqual([r["ts"] for r in scheduler.readings(path, since=250)], [300.0])
        os.environ["PRORAB_LOG_DIR"] = self.tmp.name
        self.assertEqual(len(scheduler.readings()), 3)
        self.assertEqual(scheduler.burn_points(scheduler.readings(path)),
                         [(100.0, 10.0), (300.0, 12.0)])

    # ---- caps -------------------------------------------------------------------

    def test_cap_for_day(self):
        c = scheduler.cap_for(at(14), at(17), "08:00", D)
        self.assertEqual(c, {"mode": "day", "no_new_mass": 60, "stop": 80})

    def test_cap_for_night_before_morning(self):
        c = scheduler.cap_for(at(23), at(3, day=22), "08:00", D, night=True)
        self.assertEqual(c, {"mode": "night_before_morning", "no_new_mass": None, "stop": 95})

    def test_cap_for_morning_window(self):
        # 05:30, reset at 10:00, morning 08:00 -> the reset falls after the morning
        c = scheduler.cap_for(at(5, 30), at(10), "08:00", D, night=True)
        self.assertEqual(c, {"mode": "night_morning_window", "no_new_mass": 70, "stop": 80})
        # reset exactly at morning still counts as before it
        c = scheduler.cap_for(at(5, 30), at(8), "08:00", D, night=True)
        self.assertEqual(c["mode"], "night_before_morning")

    def test_cap_for_morning_is_next_local_time(self):
        # 09:00 now: the next 08:00 is tomorrow, so a reset at 12:00 today is before it
        c = scheduler.cap_for(at(9), at(12), "08:00", D, night=True)
        self.assertEqual(c["mode"], "night_before_morning")

    def test_cap_for_unknown_reset_is_day(self):
        c = scheduler.cap_for(at(23), None, "08:00", D, night=True)
        self.assertEqual(c, {"mode": "day", "no_new_mass": 60, "stop": 80})

    def test_cap_for_night_unknown_week_blocks_mass(self):
        c = scheduler.cap_for(at(23), at(3, day=22), "08:00", D, night=True, week_known=False)
        self.assertEqual((c["mode"], c["no_new_mass"], c["stop"]), ("night_before_morning", 0, 95))
        c = scheduler.cap_for(at(5, 30), at(10), "08:00", D, night=True, week_known=False)
        self.assertEqual((c["mode"], c["no_new_mass"], c["stop"]), ("night_morning_window", 0, 80))
        c = scheduler.cap_for(at(14), at(17), "08:00", D, week_known=False)  # day: unchanged
        self.assertEqual(c["no_new_mass"], 60)

    # ---- parallelism ------------------------------------------------------------

    def _ap(self, **kw):
        a = dict(ready=10, burn_pp_per_h=None, used_pct=20.0, cap_pct=80.0, resets_in_s=3600,
                 active=2, load1=1.0, ncpu=8, d=D)
        a.update(kw)
        return scheduler.allowed_parallel(**a)

    def test_allowed_parallel_bounds(self):
        self.assertEqual(self._ap(), 6)  # ceiling
        self.assertEqual(self._ap(ready=1), 3)  # active + ready
        self.assertEqual(self._ap(ready=0), 2)
        # limit: 60 pp left, each of 2 brigades burns 10 pp/h, 3 h to reset -> 60/30 = 2
        self.assertEqual(self._ap(burn_pp_per_h=20.0, resets_in_s=3 * 3600), 2)
        # load above ncpu: no new brigades
        self.assertEqual(self._ap(load1=9.0), 2)
        self.assertEqual(self._ap(used_pct=80.0), 0)  # cap reached

    def test_allowed_parallel_limit_details(self):
        # unknown reset -> 5 h: 60 / (10 * 5) = 1
        self.assertEqual(self._ap(burn_pp_per_h=20.0, resets_in_s=None), 1)
        self.assertEqual(self._ap(burn_pp_per_h=None, active=2), 6)  # no burn data
        self.assertEqual(self._ap(burn_pp_per_h=20.0, active=0), 6)  # no active brigade
        self.assertEqual(self._ap(burn_pp_per_h=0.0), 6)  # idle: no bound

    def test_allowed_parallel_override_keeps_machine_bounds(self):
        # «игнорируй лимиты»: cap 100, the ceiling and the load bound still hold
        self.assertEqual(self._ap(cap_pct=100.0, used_pct=95.0), 6)
        self.assertEqual(self._ap(cap_pct=100.0, used_pct=95.0, load1=9.0), 2)
        self.assertEqual(self._ap(cap_pct=100.0, ready=50), 6)

    # ---- machine load -----------------------------------------------------------

    def test_machine_load_via_sysctl_stub(self):
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "sysctl")
        with open(stub, "w") as fh:
            fh.write('#!/bin/sh\ncase "$2" in\n vm.loadavg) echo "{ 2.50 1.00 0.50 }";;\n'
                     ' hw.ncpu) echo 12;;\nesac\n')
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        self.assertEqual(scheduler.machine_load(), (2.5, 12))

    # ---- plan (journal stubbed) -------------------------------------------------

    def _journal(self, **kw):
        j = types.SimpleNamespace(
            graph=lambda: {"n1": [], "n2": ["n1"], "n3": []},
            open_stages=lambda n: {"n1": ["s1"], "n2": ["s2", "s3"], "n3": ["s4"]}[n],
            stage_info=lambda s: {"s1": {"estimate_min": 20, "kind": "code"},
                                  "s2": {"estimate_min": 10, "kind": "code"},
                                  "s3": {"estimate_min": None, "kind": "docs"},
                                  "s4": {"estimate_min": 5, "kind": "code"}}[s],
            durations=lambda kind: [],
            ready_naryads=lambda: ["n1", "n3"],
            brigades=lambda: ["b1"],
            kv_get=lambda k: kw.get("kv", {}).get(k),
            override=lambda: kw.get("override"),
        )
        return j

    def test_build_plan(self):
        log = os.path.join(self.tmp.name, "monitor.log")
        now = at(14)
        with open(log, "w", encoding="utf-8") as fh:
            for i, pct in enumerate((10.0, 20.0, 30.0)):
                fh.write(json.dumps({"ts": now - 7200 + i * 3600, "five_hour": pct,
                                     "seven_day": 20.0, "resets_at": now + 3600}) + "\n")
        lim = {"five_hour": 30.0, "seven_day": 20.0, "five_hour_resets_at": now + 3600,
               "five_hour_resets_in_s": 3600}
        plan = scheduler.build_plan(self._journal(), D, now, lim, log, (1.0, 8))
        self.assertEqual(plan["order"], ["n1", "n3"])  # n1: 20+10+30 = 60 > n3: 5
        self.assertEqual(plan["active"], 1)
        # burn 10 pp/h, 1 brigade, 1 h left: 50 / 10 = 5 -> min(1 + 2, 5, 6) = 3
        self.assertEqual(plan["allowed"], 3)
        self.assertEqual(plan["new"], 2)
        self.assertEqual(plan["cap"]["mode"], "day")
        self.assertIsNone(plan["override"])

    def test_build_plan_override_and_night(self):
        now = at(23)
        lim = {"five_hour": 90.0, "seven_day": None, "five_hour_resets_at": at(3, day=22),
               "five_hour_resets_in_s": 4 * 3600}
        ov = {"goal": "g", "since": 1, "source": "слова заказчика"}
        j = self._journal(kv={"shift.night": "1"}, override=ov)
        plan = scheduler.build_plan(j, D, now, lim, os.path.join(self.tmp.name, "none"), (0.5, 8))
        self.assertEqual(plan["override"], ov)
        self.assertEqual(plan["cap"]["mode"], "night_before_morning")
        self.assertEqual(plan["cap"]["no_new_mass"], 0)  # week unknown at night
        self.assertGreater(plan["allowed"], 0)  # override: the cap is 100, not 95

    def test_cli_cap(self):
        real = scheduler.journal
        scheduler.journal = self._journal()
        self.addCleanup(setattr, scheduler, "journal", real)
        os.environ["HOME"] = self.tmp.name  # no real sensor files
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = scheduler.main(["cap", "--now", str(at(14)), "--resets-at", str(at(17))])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["mode"], "day")


if __name__ == "__main__":
    unittest.main()
