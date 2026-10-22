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
        saved = {k: os.environ.get(k) for k in ("TZ", "PATH", "PRORAB_LOG_DIR", "HOME", "HERDR_ENV")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "UTC"
        os.environ.pop("HERDR_ENV", None)  # tests never reach the real herdr
        time.tzset()

    # ---- critical path ----------------------------------------------------------

    def test_critical_order_two_chains(self):
        graph = {"A": [], "B": ["A"], "C": ["B"], "D": [], "E": ["D"]}
        est = {k: 30.0 for k in graph}
        order = scheduler.critical_order(graph, est)
        self.assertEqual(order, ["A", "B", "D", "C", "E"])  # 90, 60, 60, 30, 30; ties by id

    def test_critical_order_uses_durations_and_includes_unknown_blockers(self):
        graph = {"X": ["Z"], "Y": []}
        order = scheduler.critical_order(graph, {"X": 10, "Y": 50, "Z": 5})
        self.assertEqual(order, ["Y", "Z", "X"])  # Y 50, Z 15, X 10

    def test_duration_falls_back_to_history_then_default(self):
        hist = {"code": [10.0, 50.0, 20.0]}
        self.assertEqual(scheduler.duration({"duration_min": 7, "kind": "code"}, hist, D), 7)
        self.assertEqual(scheduler.duration({"duration_min": None, "kind": "code"}, hist, D), 20.0)
        self.assertEqual(scheduler.duration({"kind": "docs"}, hist, D), 30)
        self.assertEqual(scheduler.duration({"kind": None}, hist, D), 30)

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

    def test_cap_for_night_unknown_reset_unknown_week_blocks_mass(self):
        c = scheduler.cap_for(at(23), None, "08:00", D, night=True, week_known=False)
        self.assertEqual(c, {"mode": "day", "no_new_mass": 0, "stop": 80})
        c = scheduler.cap_for(at(23), None, "08:00", D, night=False, week_known=False)
        self.assertEqual(c["no_new_mass"], 60)  # by day the week does not matter

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
        # limit: 60 pp left, each of 2 crews burns 10 pp/h, 3 h to reset -> 60/30 = 2
        self.assertEqual(self._ap(burn_pp_per_h=20.0, resets_in_s=3 * 3600), 2)
        # load above ncpu: no new crews
        self.assertEqual(self._ap(load1=9.0), 2)
        self.assertEqual(self._ap(used_pct=80.0), 0)  # cap reached

    def test_allowed_parallel_limit_details(self):
        # unknown reset -> 5 h: 60 / (10 * 5) = 1
        self.assertEqual(self._ap(burn_pp_per_h=20.0, resets_in_s=None), 1)
        self.assertEqual(self._ap(burn_pp_per_h=None, active=2), 6)  # no burn data
        self.assertEqual(self._ap(burn_pp_per_h=20.0, active=0), 6)  # no active crew
        self.assertEqual(self._ap(burn_pp_per_h=0.0), 6)  # idle: no bound

    def test_allowed_parallel_override_keeps_machine_bounds(self):
        # the customer's command 'ignore limits and finish': cap 100, the ceiling and the load bound still hold
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
            stage_info=lambda s: {"s1": {"duration_min": 20, "kind": "code"},
                                  "s2": {"duration_min": 10, "kind": "code"},
                                  "s3": {"duration_min": None, "kind": "docs"},
                                  "s4": {"duration_min": 5, "kind": "code"}}[s],
            durations=lambda kind: [],
            ready_work_orders=lambda: ["n1", "n3"],
            crews=lambda: ["b1"],
            kv_get=lambda k: kw.get("kv", {}).get(k),
            override=lambda: kw.get("override"),
            machine=lambda: dict(D["machine"], **kw.get("machine", {})),
            estimate=lambda n: dict({"usd": 10.0, "heavy_runs_per_stage": 20},
                                    **kw.get("estimates", {}).get(n, {})),
            budget=lambda: dict({"usd": 350.0, "week_pp": 10.0}, **kw.get("budget", {})),
            shift_spend=lambda d, now=None: dict(kw.get("shift", {"usd": 0.0})),
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
        # burn 10 pp/h, 1 crew, 1 h left: 50 / 10 = 5 -> min(1 + 2, 5, 6) = 3
        self.assertEqual(plan["allowed"], 3)
        self.assertEqual(plan["new"], 2)
        self.assertEqual(plan["cap"]["mode"], "day")
        self.assertIsNone(plan["override"])

    def test_build_plan_uses_kv_ceiling(self):
        log = os.path.join(self.tmp.name, "monitor.log")
        now = at(14)
        lim = {"five_hour": 30.0, "seven_day": 20.0, "five_hour_resets_at": now + 3600,
               "five_hour_resets_in_s": 3600}
        plan = scheduler.build_plan(self._journal(machine={"parallel_ceiling": 2}), D, now,
                                    lim, log, (1.0, 8))
        self.assertEqual(plan["allowed"], 2)  # active 1 + 2 ready, held by the kv ceiling
        self.assertEqual(plan["new"], 1)

    def test_build_plan_night_cap_blocks_without_override(self):
        now = at(23)
        lim = {"five_hour": 97.0, "seven_day": None, "five_hour_resets_at": at(3, day=22),
               "five_hour_resets_in_s": 4 * 3600}
        plan = scheduler.build_plan(self._journal(kv={"shift.night": "1"}), D, now, lim,
                                    os.path.join(self.tmp.name, "none"), (0.5, 8))
        self.assertEqual(plan["cap"]["mode"], "night_before_morning")
        self.assertEqual(plan["cap"]["no_new_mass"], 0)  # week unknown at night
        self.assertEqual(plan["allowed"], 0)  # 97 >= 95

    def test_build_plan_override_lifts_the_cap(self):
        now = at(23)
        lim = {"five_hour": 97.0, "seven_day": None, "five_hour_resets_at": at(3, day=22),
               "five_hour_resets_in_s": 4 * 3600}
        ov = {"goal": "g", "since": 1, "source": "customer's words"}
        j = self._journal(kv={"shift.night": "1"}, override=ov)
        none = os.path.join(self.tmp.name, "none")
        plan = scheduler.build_plan(j, D, now, lim, none, (0.5, 8))
        self.assertEqual(plan["override"], ov)
        self.assertEqual(plan["cap"]["stop"], 95)  # the reported cap is unchanged
        self.assertEqual(plan["allowed"], 3)  # active 1 + 2 ready; the cap is 100
        # the machine bound still holds under the override
        plan = scheduler.build_plan(j, D, now, lim, none, (9.0, 8))
        self.assertEqual(plan["allowed"], 1)  # load > ncpu: only the running crew
        # by day: 85 > stop 80
        lim = dict(lim, five_hour=85.0)
        day = scheduler.build_plan(self._journal(), D, at(14), lim, none, (0.5, 8))
        self.assertEqual(day["allowed"], 0)
        day = scheduler.build_plan(self._journal(override=ov), D, at(14), lim, none, (0.5, 8))
        self.assertEqual(day["allowed"], 3)

    def _hold_plan(self, five_hour, *, night=True, week=20.0, override=None, now=None):
        none = os.path.join(self.tmp.name, "none")
        if night:
            now = now or at(23)
            lim = {"five_hour": five_hour, "seven_day": week,
                   "five_hour_resets_at": at(10, day=22), "five_hour_resets_in_s": 11 * 3600}
            kv = {"shift.night": "1"}
        else:
            now = now or at(14)
            lim = {"five_hour": five_hour, "seven_day": week,
                   "five_hour_resets_at": now + 3600, "five_hour_resets_in_s": 3600}
            kv = {}
        return scheduler.build_plan(self._journal(kv=kv, override=override), D, now, lim,
                                    none, (0.5, 8))

    def test_plan_holds_at_no_new_mass(self):
        plan = self._hold_plan(78.0)  # the window covers the morning: 70 / 80
        self.assertEqual(plan["cap"]["mode"], "night_morning_window")
        self.assertEqual(plan["hold"], "no_new_mass")
        self.assertEqual(plan["new"], 0)
        plan = self._hold_plan(69.0)
        self.assertIsNone(plan["hold"])
        self.assertGreater(plan["new"], 0)

    def test_plan_holds_by_day_at_the_threshold(self):
        self.assertIsNone(self._hold_plan(59.9, night=False)["hold"])
        plan = self._hold_plan(60.0, night=False)
        self.assertEqual(plan["hold"], "no_new_mass")
        self.assertEqual(plan["new"], 0)

    def test_plan_holds_at_night_with_unknown_week(self):
        none = os.path.join(self.tmp.name, "none")
        lim = {"five_hour": None, "seven_day": None, "five_hour_resets_at": None,
               "five_hour_resets_in_s": None}
        plan = scheduler.build_plan(self._journal(kv={"shift.night": "1"}), D, at(23), lim,
                                    none, (0.5, 8))
        self.assertEqual(plan["cap"]["no_new_mass"], 0)
        self.assertEqual(plan["hold"], "no_new_mass")
        self.assertEqual(plan["new"], 0)

    def test_plan_override_lifts_hold(self):
        ov = {"goal": "g", "since": 1, "source": "customer's words"}
        plan = self._hold_plan(78.0, override=ov)
        self.assertIsNone(plan["hold"])
        self.assertGreater(plan["new"], 0)

    def test_plan_no_hold_before_morning_window(self):
        # a window that resets before the morning has no separate no-new-mass threshold
        now = at(23)
        lim = {"five_hour": 90.0, "seven_day": 20.0, "five_hour_resets_at": at(3, day=22),
               "five_hour_resets_in_s": 4 * 3600}
        plan = scheduler.build_plan(self._journal(kv={"shift.night": "1"}), D, now, lim,
                                    os.path.join(self.tmp.name, "none"), (0.5, 8))
        self.assertIsNone(plan["cap"]["no_new_mass"])
        self.assertIsNone(plan["hold"])

    def test_build_plan_unknown_usage_is_conservative(self):
        none = os.path.join(self.tmp.name, "none")
        now = at(14)
        lim = {"five_hour": None, "seven_day": None, "five_hour_resets_at": None,
               "five_hour_resets_in_s": None}
        plan = scheduler.build_plan(self._journal(), D, now, lim, none, (0.5, 8))
        self.assertTrue(plan["limits_unknown"])
        self.assertEqual(plan["allowed"], 1)  # max(1, active=1), not active + ready
        j = self._journal()
        j.crews = lambda: ["b1", "b2", "b3"]
        plan = scheduler.build_plan(j, D, now, lim, none, (0.5, 8))
        self.assertEqual(plan["allowed"], 3)  # no growth beyond the running crews
        j.crews = lambda: []
        plan = scheduler.build_plan(j, D, now, lim, none, (0.5, 8))
        self.assertEqual(plan["allowed"], 1)  # one crew may start
        ok = dict(lim, five_hour=10.0)
        self.assertFalse(scheduler.build_plan(j, D, now, ok, none, (0.5, 8))["limits_unknown"])

    def test_build_plan_stale_log_is_ignored_fresh_log_is_used(self):
        log = os.path.join(self.tmp.name, "monitor.log")
        now = at(14)
        lim = {"five_hour": None, "seven_day": None, "five_hour_resets_at": None,
               "five_hour_resets_in_s": None}
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now - 3600, "five_hour": 10.0}) + "\n")
        plan = scheduler.build_plan(self._journal(), D, now, lim, log, (0.5, 8))
        self.assertTrue(plan["limits_unknown"])  # an hour old: older than sensor_max_age_s
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now - 60, "five_hour": 10.0}) + "\n")
        plan = scheduler.build_plan(self._journal(), D, now, lim, log, (0.5, 8))
        self.assertFalse(plan["limits_unknown"])
        self.assertEqual(plan["allowed"], 3)

    # ---- fit: the estimate in pp by the rate ------------------------------------

    RATES = {"five_hour": {"usd_per_pp": 5.0, "source": "default"},
             "seven_day": {"usd_per_pp": None, "source": "unknown"}}

    def _fits(self, estimate=10.0, left=100.0, rates=None, used=70.0, stop=80.0, rise=None,
              week_budget=10.0, override=False):
        return scheduler.fits(estimate, left, rates or self.RATES, used, stop, rise,
                              week_budget, override)

    def test_fits_converts_by_rate(self):
        r = self._fits(used=70.0)
        self.assertEqual((r["pp_window"], r["window"], r["ok"]), (2.0, True, True))
        self.assertIsNone(r["pp_week"])
        self.assertTrue(self._fits(used=78.0)["window"])  # 78 + 2 == stop
        r = self._fits(used=79.0)
        self.assertFalse(r["window"])
        self.assertFalse(r["ok"])
        self.assertTrue(self._fits(used=None)["window"])  # unknown usage is not guessed

    def test_fits_budget_left(self):
        r = self._fits(estimate=50.0, left=40.0)
        self.assertFalse(r["budget"])
        self.assertFalse(r["ok"])
        self.assertTrue(self._fits(estimate=40.0, left=40.0)["budget"])

    def test_fits_week(self):
        rates = dict(self.RATES, seven_day={"usd_per_pp": 20.0, "source": "estimated"})
        r = self._fits(rates=rates, rise=8.5)  # 10 / 20 = 0.5 pp of the week
        self.assertEqual(r["pp_week"], 0.5)
        self.assertTrue(r["week"])
        self.assertFalse(self._fits(rates=rates, rise=9.6)["week"])
        self.assertTrue(self._fits(rates=rates, rise=None)["week"])

    def test_fits_unknown_week_rate_skips_week_check(self):
        r = self._fits(rise=99.0)  # the rate of the week is unknown: never guessed
        self.assertIsNone(r["pp_week"])
        self.assertTrue(r["week"])

    def test_fits_override_lifts_all(self):
        r = self._fits(estimate=500.0, left=1.0, used=79.0, override=True)
        self.assertEqual((r["budget"], r["window"], r["week"], r["ok"]), (True,) * 4)
        self.assertEqual(r["pp_window"], 100.0)  # the numbers are still reported

    def test_build_plan_reports_budget_rates_and_fits(self):
        now = at(14)
        lim = {"five_hour": 70.0, "seven_day": 25.0, "five_hour_resets_at": now + 3600,
               "five_hour_resets_in_s": 3600}
        j = self._journal(kv={"shift.week_start": "20"},
                          estimates={"n1": {"usd": 10.0}, "n3": {"usd": 120.0}},
                          budget={"usd": 200.0, "week_pp": 10.0},
                          shift={"usd": 100.0})
        plan = scheduler.build_plan(j, D, now, lim, os.path.join(self.tmp.name, "none"),
                                    (1.0, 8), rates=self.RATES)
        self.assertEqual(plan["budget"], {"usd": 200.0, "spent_usd": 100.0, "left_usd": 100.0,
                                          "week_pp": 10.0, "week_rise_pp": 5.0})
        self.assertEqual(plan["rates"], self.RATES)
        self.assertEqual(set(plan["fits"]), set(plan["order"]))
        self.assertTrue(plan["fits"]["n1"]["ok"])
        self.assertEqual(plan["fits"]["n1"]["pp_window"], 2.0)
        self.assertFalse(plan["fits"]["n3"]["budget"])  # $120 > $100 left
        self.assertFalse(plan["fits"]["n3"]["ok"])  # 70 + 24 > 80 as well
        self.assertFalse(plan["fits"]["n3"]["window"])

    def test_build_plan_spend_is_injectable_and_rates_come_from_the_log(self):
        now = at(14)
        lim = {"five_hour": 10.0, "seven_day": None, "five_hour_resets_at": now + 3600,
               "five_hour_resets_in_s": 3600}
        plan = scheduler.build_plan(self._journal(), D, now, lim,
                                    os.path.join(self.tmp.name, "none"), (1.0, 8),
                                    spend={"usd": 349.0})
        self.assertEqual(plan["budget"]["spent_usd"], 349.0)
        self.assertEqual(plan["budget"]["left_usd"], 1.0)
        self.assertIsNone(plan["budget"]["week_rise_pp"])  # week unknown
        self.assertEqual(plan["rates"]["five_hour"]["source"], "default")  # empty log
        self.assertFalse(plan["fits"]["n1"]["budget"])

    def test_build_plan_override_makes_everything_fit(self):
        now = at(14)
        lim = {"five_hour": 79.0, "seven_day": None, "five_hour_resets_at": now + 3600,
               "five_hour_resets_in_s": 3600}
        ov = {"goal": "g", "since": 1, "source": "customer's words"}
        plan = scheduler.build_plan(self._journal(override=ov, shift={"usd": 400.0}), D, now,
                                    lim, os.path.join(self.tmp.name, "none"), (1.0, 8))
        self.assertTrue(all(f["ok"] for f in plan["fits"].values()))

    def test_build_plan_no_estimate_not_ok(self):
        now = at(14)
        lim = {"five_hour": 10.0, "seven_day": None, "five_hour_resets_at": now + 3600,
               "five_hour_resets_in_s": 3600}
        j = self._journal(estimates={"n3": {"usd": None}})
        plan = scheduler.build_plan(j, D, now, lim, os.path.join(self.tmp.name, "none"),
                                    (1.0, 8))
        self.assertEqual(plan["fits"]["n3"], {"ok": False, "reason": "no_estimate"})
        self.assertTrue(plan["fits"]["n1"]["ok"])

    def test_gather_limits_reads_pane_texts(self):
        os.environ["HOME"] = self.tmp.name  # no real sensor files
        d = _defaults.load()
        fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                               "statusline_wide.txt")
        with open(fixture, encoding="utf-8") as fh:
            text = fh.read()
        lim = scheduler.gather_limits(d, at(14), pane_texts=lambda: [text])
        self.assertEqual((lim["source"], lim["five_hour"]), ("statusline", 17.0))
        self.assertAlmostEqual(lim["five_hour_resets_at"], at(14) + 4 * 3600 + 7 * 60)
        os.environ.pop("HERDR_ENV", None)
        self.assertEqual(scheduler.gather_limits(d, at(14))["source"], "unknown")  # no herdr

    def test_gather_limits_lazy_panes(self):
        os.environ["HOME"] = self.tmp.name
        d = _defaults.load()
        d["limits"]["sensor_dir"] = os.path.join(self.tmp.name, "usage")
        os.makedirs(d["limits"]["sensor_dir"])
        path = os.path.join(d["limits"]["sensor_dir"], "s1.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"rate_limits": {"five_hour": {"used_percentage": 12.0,
                                                      "resets_at": at(17)},
                                       "seven_day": {"used_percentage": 30.0,
                                                     "resets_at": at(17, day=25)}}}, fh)
        m = os.path.getmtime(path)
        calls = []
        lim = scheduler.gather_limits(d, m + 5, pane_texts=lambda: calls.append(1) or [])
        self.assertEqual((lim["source"], calls), ("sensor", []))

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

    def test_cli_plan_without_now(self):
        # the live run: `scheduler.py plan` read `a.now`, which only `cap` defined
        seen = []
        patches = {"journal": types.SimpleNamespace(ensure_db=lambda: None),
                   "gather_limits": lambda d, now: {},
                   "machine_load": lambda: None,
                   "build_plan": lambda j, d, now, lim, log, load: seen.append(now) or {"new": 0}}
        for k, v in patches.items():
            self.addCleanup(setattr, scheduler, k, getattr(scheduler, k))
            setattr(scheduler, k, v)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = scheduler.main(["plan"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), {"new": 0})
        self.assertAlmostEqual(seen[0], time.time(), delta=60)


if __name__ == "__main__":
    unittest.main()
