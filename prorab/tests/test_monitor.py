import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import journal  # noqa: E402
import monitor  # noqa: E402
import transcript  # noqa: E402
from prorab.tests.test_herdr_cli import FakeBase, ok  # noqa: E402

D = _defaults.load()
D["week"]["calendar"]["days"] = []  # no working days: W = 0, projected = used + the off-hours work


def at(hh, mm=0, day=21):
    return time.mktime((2026, 10, day, hh, mm, 0, 0, 0, 0))


def lim(five=None, week=None, source="sensor", reached=False, resets_at=None, week_resets=None):
    return {"five_hour": five, "seven_day": week, "source": source, "reached": reached,
            "five_hour_resets_at": resets_at, "five_hour_resets_in_s": None,
            "seven_day_resets_at": week_resets}


def burn(pp_per_h=None, resets_at=None):
    return {"pp_per_h": pp_per_h, "source": "none" if pp_per_h is None else "measured",
            "reason": None, "intervals": 0, "pp": 0.0, "hours": 0.0,
            "resets_at": resets_at, "at": 0}


def st(status="working", bg=0, ctx=None, size=100, pane="p", cwd="/w", consent=None,
       limit_screen=None, provider_error=None):
    return {"status": status, "bg_shells": bg, "ctx": ctx, "session_id": "s", "cwd": cwd,
            "pane_id": pane, "_size": size, "consent": consent, "limit_screen": limit_screen,
            "provider_error": provider_error}


class Stub:
    """Mutable fake world injected into poll/main as `deps`."""

    def __init__(self):
        self.crews = {"b1": st()}
        self.limits = lim(10.0, 10.0)
        self.kv = {}
        self.over = []
        self.override = None
        self.own = None
        self.statusline = {}
        self.screens = {}   # pane id -> screen text
        self.holders = []
        self.age = {}       # worktree -> newest file age; missing = very old
        self.spend = {"usd": 0.0, "work_orders": {}, "research": {}, "foreman_usd": 0.0}
        self.rate = {"five_hour": {"usd_per_pp": 5.0, "source": "default"},
                     "seven_day": {"usd_per_pp": None, "source": "unknown"}}
        self.week_burn = burn()
        self.calls = {"agents": 0, "kv_all": 0, "visible": [], "limit_panes": None,
                      "spend": [], "rates": [], "week_burn": []}

    def _agents(self):
        self.calls["agents"] += 1
        return [{"name": n, "pane_id": c["pane_id"], "agent_status": c["status"],
                 "cwd": c["cwd"]} for n, c in self.crews.items() if c["status"] != "gone"]

    def _kv_all(self):
        self.calls["kv_all"] += 1
        return dict(self.kv)

    def _visible(self, pane):
        self.calls["visible"].append(pane)
        return self.screens.get(pane, "")

    def _limits(self, d, now, pane_texts=None):
        self.calls["limit_panes"] = pane_texts
        return self.limits

    def _shift_spend(self, d, now=None, kv=None):
        self.calls["spend"].append((now, kv))
        return json.loads(json.dumps(self.spend))

    def _rates(self, d, now):
        self.calls["rates"].append(now)
        return self.rate

    def _week_burn(self, d, now):
        self.calls["week_burn"].append(now)
        return json.loads(json.dumps(self.week_burn))

    def _over_budget(self, week, spend=None, kv=None):
        if self.over:  # a canned answer
            return list(self.over)
        return journal.over_budget(week, spend, kv=self.kv if kv is None else kv)

    def deps(self):
        return {
            "state": lambda n, a, read_screen=True, screen=None, kv=None: (
                self.crews[n] if a is not None else st("gone")),
            "agents": self._agents,
            "kv_all": self._kv_all,
            "visible": self._visible,
            "slot_holders": lambda: list(self.holders),
            "newest_age": lambda wt, now, max_files: self.age.get(wt, 10 ** 6),
            "transcript_size": lambda s: s.get("_size"),
            "statusline_ctx": lambda text: self.statusline.get(text),
            "limits": self._limits,
            "kv_get": lambda k: self.kv.get(k),
            "over_budget": self._over_budget,
            "shift_spend": self._shift_spend,
            "rates": self._rates,
            "week_burn": self._week_burn,
            "budget": lambda kv=None: journal.budget(kv=self.kv if kv is None else kv),
            "override": lambda kv=None: self.override,
            "own_context": lambda d, listed=None: self.own,
            "crews": lambda kv=None: [],
        }


def ctx(pct, soft=False, hard=False):
    return {"pct": pct, "soft": soft, "hard": hard, "tokens": 0, "window": 1, "model": ""}


class PollBase(unittest.TestCase):
    def setUp(self):
        self.s = Stub()
        self.mem = {}
        self.now = at(12)
        saved_tz = os.environ.get("TZ")

        def restore_tz():
            if saved_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved_tz
            time.tzset()
        self.addCleanup(restore_tz)
        os.environ["TZ"] = "UTC"
        time.tzset()

    def poll(self, now=None, names=("b1",)):
        if getattr(self, "no_crews", False):
            names = ()
        return monitor.poll(list(names), D, self.mem, self.now if now is None else now,
                            deps=self.s.deps())



class TestMonitorPoll(PollBase):
    def test_settled_suppressed_by_bg_shell(self):
        self.s.crews["b1"] = st("idle", bg=1)
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("idle", bg=0)
        self.assertEqual(self.poll(), ["settled b1 idle"])

    def test_blocked_always_settled(self):
        self.s.crews["b1"] = st("blocked", bg=2)
        self.assertEqual(self.poll(), ["settled b1 blocked"])

    def test_settled_once_per_transition(self):
        self.s.crews["b1"] = st("done")
        self.assertEqual(self.poll(), ["settled b1 done"])
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("working")
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("done")
        self.assertEqual(self.poll(), ["settled b1 done"])

    def test_exiting_crew_raises_no_event(self):
        self.s.kv["crew.b1.exiting"] = str(self.now - 30)
        self.s.crews["b1"] = st("blocked")
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("idle", consent="trust")
        self.assertEqual(self.poll(), [])
        del self.s.crews["b1"]
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.mem.get("gone", {}), {})
        self.assertEqual(self.mem.get("settled", {}), {})

    def test_stale_exiting_mark_is_ignored(self):
        self.s.kv["crew.b1.exiting"] = str(self.now - 400)
        self.s.crews["b1"] = st("blocked")
        self.assertEqual(self.poll(), ["settled b1 blocked"])
        del self.s.crews["b1"]
        self.assertEqual(self.poll(), ["gone b1"])

    def test_gone_once(self):
        del self.s.crews["b1"]
        self.assertEqual(self.poll(), ["gone b1"])
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st()
        self.assertEqual(self.poll(), [])
        del self.s.crews["b1"]
        self.assertEqual(self.poll(), ["gone b1"])

    def test_blind_once_then_ack_silences(self):
        self.s.limits = lim(source="unknown")
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), ["blind"])
        self.assertEqual(self.poll(), [])  # once per streak
        self.s.limits = lim(10.0, 10.0)
        self.poll()
        self.s.limits = lim(source="unknown")
        self.s.kv["limits.blind_ack"] = "1"
        self.poll()
        self.assertEqual(self.poll(), [])

    def test_no_blind_right_after_reset(self):
        T = self.now + 600
        self.s.limits = lim(10.0, 10.0, resets_at=T)
        self.poll(T - 100)  # the 5h reset time is known
        self.s.limits = lim(source="unknown")
        self.assertEqual(self.poll(T + 30), [])
        self.assertEqual(self.poll(T + 90), [])  # a second unknown poll, still in the grace
        self.assertEqual(self.poll(T + 200), [])  # the grace is over: the streak starts here
        self.assertEqual(self.poll(T + 210), ["blind"])
        self.assertEqual(self.mem["last_resets_at"], T)  # persisted with the rest of mem

    def test_limit_event_thresholds(self):
        self.s.limits = lim(59.9, 10.0)
        self.assertEqual(self.poll(), [])
        self.s.limits = lim(60.0, 10.0)
        self.assertEqual(self.poll(), ["limit 60 10 sensor day"])
        self.assertEqual(self.poll(), [])  # same band: no repeat
        self.s.limits = lim(80.0, 10.0)
        self.assertEqual(self.poll(), ["limit 80 10 sensor day"])  # band changed
        self.s.limits = lim(20.0, 50.0)
        self.assertEqual(self.poll(), ["limit 20 50 sensor day week:notify"])  # no reset: fallback
        self.s.limits = lim(None, None, source="messages", reached=True)
        self.assertEqual(self.poll(), ["limit - - messages day"])

    def test_limit_event_night_caps(self):
        self.s.kv["shift.night"] = "1"
        now = at(23)
        morning_window = at(10, day=22)
        before_morning = at(5, day=22)
        self.s.limits = lim(69.9, 10.0, resets_at=morning_window)
        self.assertEqual(self.poll(now), [])
        self.s.limits = lim(70.0, 10.0, resets_at=morning_window)
        self.assertEqual(self.poll(now), ["limit 70 10 sensor night_morning_window"])
        self.mem.clear()
        self.s.limits = lim(94.9, 10.0, resets_at=before_morning)
        self.assertEqual(self.poll(now), [])
        self.s.limits = lim(95.0, 10.0, resets_at=before_morning)
        self.assertEqual(self.poll(now), ["limit 95 10 sensor night_before_morning"])

    def test_override_silences_limit_and_budget_but_logs(self):
        self.s.override = "goal-1"
        self.s.limits = lim(90.0, 60.0)
        self.s.over = ["sessions"]
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.mem["last_reading"]["five_hour"], 90.0)
        self.s.override = None
        self.assertEqual(self.poll(), ["limit 90 60 sensor day week:notify", "budget sessions"])

    def test_budget_on_change_only(self):
        self.s.over = ["sessions"]
        self.assertEqual(self.poll(), ["budget sessions"])
        self.assertEqual(self.poll(), [])
        self.s.over = ["sessions", "runs"]
        self.assertEqual(self.poll(), ["budget sessions,runs"])
        self.s.over = []
        self.assertEqual(self.poll(), [])
        self.s.over = ["runs"]
        self.assertEqual(self.poll(), ["budget runs"])

    def test_budget_event_in_dollars(self):
        self.s.spend["usd"] = 361.2
        self.assertEqual(self.poll(), ["budget usd=361.20/100.00"])
        self.assertEqual(self.poll(self.now + 90), [])
        self.s.spend["work_orders"] = {"N7": {"usd_shift": 40.0, "usd_total": 40.0,
                                              "estimate_usd": 30.0, "over": True}}
        self.assertEqual(self.poll(self.now + 400),
                         ["budget usd=361.20/100.00,estimate:N7=40.00/30.00"])

    def test_week_pp_in_the_budget_event(self):
        self.s.kv["shift.week_start"] = "20"
        self.s.limits = lim(10.0, 30.5)
        self.assertEqual(self.poll(), ["budget week_pp=10.5/10"])

    def test_estimate_over_names_work_order(self):
        self.s.spend["work_orders"] = {"N1": {"usd_shift": 31.5, "usd_total": 31.5,
                                              "estimate_usd": 30.0, "over": True},
                                       "N2": {"usd_shift": 1.0, "usd_total": 1.0,
                                              "estimate_usd": 30.0, "over": False}}
        self.s.spend["research"] = {"scan": {"usd": 12.4, "estimate_usd": 10.0, "over": True}}
        self.assertEqual(self.poll(), ["budget estimate:N1=31.50/30.00,research:scan=12.40/10.00"])

    def test_override_silences_budget_but_logs_spend(self):
        self.s.override = "goal-1"
        self.s.spend["usd"] = 361.2
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.mem["last_reading"]["spend_usd"], 361.2)
        self.assertEqual(self.mem["spend"]["value"]["usd"], 361.2)
        self.s.override = None
        self.assertEqual(self.poll(self.now + 90), ["budget usd=361.20/100.00"])

    def test_spend_every_s(self):
        for t in (0, 90, 180, 270, 299, 300, 390):
            self.poll(self.now + t)
        self.assertEqual([c[0] - self.now for c in self.s.calls["spend"]], [0, 300])
        self.assertIsNotNone(self.s.calls["spend"][0][1])  # the poll's kv snapshot

    def test_spend_survives_the_json_roundtrip_of_the_state(self):
        self.s.spend["usd"] = 5.0
        self.poll()
        mem = json.loads(json.dumps(self.mem))
        monitor.poll(["b1"], D, mem, self.now + 90, deps=self.s.deps())
        self.assertEqual(len(self.s.calls["spend"]), 1)

    def test_rate_logged_every_estimate(self):
        self.s.limits = dict(lim(10.0, 10.0), seven_day_resets_at=12345)
        logged = {}
        for t in (0, 90, 540, 599, 600, 690):
            self.poll(self.now + t)
            logged[t] = dict(self.mem["last_reading"])
        self.assertEqual([c - self.now for c in self.s.calls["rates"]], [0, 600])
        for t in (0, 600):
            self.assertEqual(logged[t]["rate"], self.s.rate)
        for t in (90, 540, 599, 690):
            self.assertNotIn("rate", logged[t])
        for r in logged.values():
            self.assertEqual(r["seven_day_resets_at"], 12345)

    # ---- the week by pace to its reset --------------------------------------------

    def week_world(self, pace=0.0):
        """A week that resets 3 days after the poll, with a measured burn of `pace` pp/h."""
        self.reset = self.now + 3 * 86400
        self.s.week_burn = burn(pace, self.reset)

    def wk(self, five, week, resets=None, **kw):
        return lim(five, week, week_resets=self.reset if resets is None else resets, **kw)

    def test_week_notice_once_per_level_per_week(self):
        self.week_world()
        self.s.limits = self.wk(20.0, 94.9)
        self.assertEqual(self.poll(), [])
        self.s.limits = self.wk(20.0, 95.1)
        self.assertEqual(self.poll(), ["limit 20 95.1 sensor day week:no_new_mass"])
        self.s.limits = self.wk(20.0, 94.9)
        self.assertEqual(self.poll(), [])
        self.s.limits = self.wk(20.0, 95.2)
        self.assertEqual(self.poll(), [])  # the level was reported for this week
        self.s.limits = self.wk(20.0, 100.1)
        self.assertEqual(self.poll(), ["limit 20 100.1 sensor day week:clearance_only"])
        self.s.limits = self.wk(60.0, 100.1)  # a 5h band rise: no week field, no repeat
        self.assertEqual(self.poll(), ["limit 60 100.1 sensor day"])
        self.s.limits = self.wk(20.0, 95.1, resets=self.reset + 7 * 86400)  # the next week
        self.assertEqual(self.poll(), ["limit 20 95.1 sensor day week:no_new_mass"])

    def test_week_notice_counts_the_night_shift(self):
        self.week_world()  # a measured 0.0 pp per working hour; W = 0 by the empty calendar
        self.s.kv = {"shift.night": "1", "shift.week_start": "88"}  # the shift has used 0 pp
        self.s.limits = self.wk(20.0, 88.0)
        out = self.poll()
        self.assertEqual(len(out), 1)  # projected 88 + 10 pp of the night shift's budget left
        self.assertTrue(out[0].startswith("limit 20 88 sensor "), out)
        self.assertTrue(out[0].endswith(" week:no_new_mass"), out)

    def test_week_notice_night_shift_dollar_term_uses_the_cached_rate(self):
        self.week_world()
        self.s.kv = {"shift.night": "1", "shift.week_start": "88"}
        self.s.spend = dict(self.s.spend, usd=60.0)
        self.s.rate = {"five_hour": {"usd_per_pp": 5.0, "source": "default"},
                       "seven_day": {"usd_per_pp": 10.0, "source": "estimated"}}
        self.s.limits = self.wk(20.0, 88.0)
        self.assertEqual(self.poll(), [])  # $40 left at 10 $/pp: 4 pp, projected 92
        self.assertEqual(self.mem["rate"]["week_usd_per_pp"], 10.0)

    def test_week_fallback_events(self):
        self.s.limits = lim(20.0, 49.9)
        self.assertEqual(self.poll(), [])
        self.s.limits = lim(20.0, 50.0)
        self.assertEqual(self.poll(), ["limit 20 50 sensor day week:notify"])
        self.s.limits = lim(20.0, 70.0)
        self.assertEqual(self.poll(), ["limit 20 70 sensor day week:clearance_only"])

    def test_first_poll_computes_the_burn_before_deciding(self):
        self.week_world()
        self.s.limits = self.wk(20.0, 50.0)
        self.assertEqual(self.mem, {})
        self.assertEqual(self.poll(), [])  # projected 50, not the 50 % fallback threshold
        self.assertEqual(len(self.s.calls["week_burn"]), 1)
        self.assertEqual(self.mem["week_notice"]["level"], 0)
        self.assertEqual(self.mem["week_burn"]["value"]["pp_per_h"], 0.0)

    def test_week_room_raises_no_event(self):
        self.week_world()
        self.s.limits = self.wk(20.0, 70.0)
        self.assertEqual(self.poll(), [])

    def test_week_stale_sensor_keeps_the_week(self):
        self.week_world()
        self.s.limits = self.wk(20.0, 95.1)
        self.assertEqual(self.poll(), ["limit 20 95.1 sensor day week:no_new_mass"])
        stored = dict(self.mem["week_notice"])
        self.s.limits = lim(20.0, 95.1, source="statusline")  # no week reset from the status line
        self.assertEqual(self.poll(self.now + 90), [])
        self.assertEqual(self.mem["week_notice"], stored)

    def test_old_limit_state_reads_as_empty(self):
        self.mem["limit"] = [1, True, False]
        self.s.limits = lim(10.0, 10.0)
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.mem["limit"], [0, False])

    def test_unknown_week_raises_no_week_event(self):
        self.no_crews = True
        self.s.limits = lim(70.0, None)
        self.assertEqual(self.poll(self.now), ["limit 70 - sensor day"])
        self.assertEqual(self.poll(self.now + 1801), ["blind_week"])

    def test_override_silences_week_then_reports(self):
        self.week_world()
        self.s.limits = self.wk(20.0, 100.1)
        self.s.override = "goal-1"
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.mem["last_reading"]["seven_day"], 100.1)  # still logged
        self.s.override = None
        self.assertEqual(self.poll(self.now + 90), ["limit 20 100.1 sensor day week:clearance_only"])
        self.assertEqual(self.poll(self.now + 180), [])

    def test_week_burn_with_the_rate(self):
        self.s.limits = lim(10.0, 10.0)
        logged = {}
        for t in (0, 90, 540, 599, 600, 690):
            self.poll(self.now + t)
            logged[t] = dict(self.mem["last_reading"])
        self.assertEqual([c - self.now for c in self.s.calls["week_burn"]], [0, 600])
        for t in (0, 600):
            self.assertIn("week_burn", logged[t])
        for t in (90, 540, 599, 690):
            self.assertNotIn("week_burn", logged[t])

    def test_failing_week_burn_is_isolated(self):
        deps = self.s.deps()

        def boom(d, now):
            raise Boom("no log")
        deps["week_burn"] = boom
        self.s.limits = lim(90.0, 10.0)
        out = monitor.poll(["b1"], D, self.mem, self.now, deps=deps)
        self.assertEqual(out, ["limit 90 10 sensor day"])
        self.assertIn("week_burn: Boom: no log", self.mem["poll_errors"][0])

    def test_failing_spend_is_isolated(self):
        deps = self.s.deps()

        def boom(d, now=None, kv=None):
            raise Boom("no files")
        deps["shift_spend"] = boom
        self.s.limits = lim(90.0, 10.0)
        out = monitor.poll(["b1"], D, self.mem, self.now, deps=deps)
        self.assertEqual(out, ["limit 90 10 sensor day"])
        self.assertIn("shift_spend: Boom: no files", self.mem["poll_errors"][0])

    def test_blind_week_after_1800s_once(self):
        self.no_crews = True
        self.s.limits = lim(10.0, None)
        self.assertEqual(self.poll(self.now), [])
        self.assertEqual(self.poll(self.now + 1800), [])
        self.assertEqual(self.poll(self.now + 1801), ["blind_week"])
        self.assertEqual(self.poll(self.now + 3000), [])
        self.s.limits = lim(10.0, 10.0)
        self.poll(self.now + 3100)
        self.s.limits = lim(10.0, None)
        self.s.kv["limits.blind_week_ack"] = "1"
        self.poll(self.now + 4000)
        self.assertEqual(self.poll(self.now + 6000), [])

    def test_context_soft_fires_once(self):
        self.s.crews["b1"] = st(ctx=ctx(61.0, soft=True))
        self.assertEqual(self.poll(), ["context_soft b1 61.0"])
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st(ctx=ctx(81.0, soft=True, hard=True))
        self.assertEqual(self.poll(), ["context_hard b1 81.0"])
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st(ctx=ctx(10.0))
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st(ctx=ctx(62.0, soft=True))
        self.assertEqual(self.poll(), ["context_soft b1 62.0"])

    def test_context_statusline_fallback(self):
        self.s.crews["b1"] = st(ctx=None)
        self.s.screens["p"] = "SL"
        self.s.statusline["SL"] = 65.0
        self.assertEqual(self.poll(), ["context_soft b1 65.0 (statusline)"])
        self.s.statusline["SL"] = 85.0
        self.assertEqual(self.poll(), ["context_hard b1 85.0 (statusline)"])

    def test_prorab_context_once(self):
        self.s.own = ctx(61.0, soft=True)
        self.assertEqual(self.poll(), ["prorab_context 61.0"])
        self.assertEqual(self.poll(), [])
        self.s.own = ctx(5.0)
        self.poll()
        self.s.own = ctx(70.0, soft=True)
        self.assertEqual(self.poll(), ["prorab_context 70.0"])

    def test_hung_after_1800s(self):
        self.assertEqual(self.poll(self.now), [])
        self.assertEqual(self.poll(self.now + 1799), [])
        self.assertEqual(self.poll(self.now + 1800), ["hung b1"])
        self.assertEqual(self.poll(self.now + 2000), [])  # once per episode
        self.s.crews["b1"] = st(size=200)  # growth ends the episode
        self.assertEqual(self.poll(self.now + 2100), [])
        self.assertEqual(self.poll(self.now + 4000), ["hung b1"])

    def test_priority_order(self):
        del self.s.crews["b1"]
        self.s.crews["b2"] = st("blocked")
        self.s.limits = lim(90.0, 10.0)
        self.s.over = ["runs"]
        out = self.poll(names=("b1", "b2"))
        self.assertEqual([e.split()[0] for e in out], ["gone", "limit", "budget", "settled"])

    def test_state_survives_json_roundtrip(self):
        self.s.limits = lim(90.0, 10.0)
        self.s.crews["b1"] = st("done", ctx=ctx(61.0, soft=True))
        first = self.poll()
        self.mem = json.loads(json.dumps(self.mem))
        self.assertTrue(first)
        self.assertEqual(self.poll(), [])


class TestMonitorG(PollBase):
    def test_one_agents_and_one_kv_call_per_poll(self):
        for n, pane in (("b1", "p1"), ("b2", "p2"), ("b3", "p3")):
            self.s.crews[n] = st("working", pane=pane, ctx=ctx(10.0))
        self.poll(names=("b1", "b2", "b3"))
        self.poll(names=("b1", "b2", "b3"))
        self.assertEqual((self.s.calls["agents"], self.s.calls["kv_all"]), (2, 2))

    def test_screen_read_only_on_change(self):
        self.s.crews["b1"] = st("working", ctx=ctx(10.0))
        self.poll()
        self.assertEqual(self.s.calls["visible"], ["p"])  # first sight: the status is new
        self.poll()
        self.assertEqual(self.s.calls["visible"], ["p"])  # unchanged, context from transcript
        self.s.crews["b1"] = st("idle", bg=2, ctx=ctx(10.0))
        self.poll()
        self.assertEqual(len(self.s.calls["visible"]), 2)  # changed
        self.poll()
        self.assertEqual(len(self.s.calls["visible"]), 3)  # idle with background shells: again
        self.s.crews["b1"] = st("idle", bg=0, ctx=ctx(10.0))
        self.poll()
        self.assertEqual(len(self.s.calls["visible"]), 4)  # re-read; shells gone
        self.assertEqual(self.poll(), [])
        self.assertEqual(len(self.s.calls["visible"]), 4)  # settled and unchanged: no read

    def test_screen_read_when_context_only_from_statusline(self):
        self.s.crews["b1"] = st("working", ctx=None)
        self.s.screens["p"] = "SL"
        self.s.statusline["SL"] = 10.0
        self.poll()
        self.poll()
        self.assertEqual(self.s.calls["visible"], ["p", "p"])

    def test_settled_uses_remembered_shells_without_reading(self):
        self.s.crews["b1"] = st("idle", bg=0, ctx=ctx(10.0))
        self.assertEqual(self.poll(), ["settled b1 idle"])
        self.assertEqual(self.poll(), [])
        self.assertEqual(len(self.s.calls["visible"]), 1)

    def test_hung_needs_both_signs(self):
        self.s.age["/w"] = 10  # a file changed ten seconds ago
        self.poll(self.now)
        self.assertEqual(self.poll(self.now + 1800), [])
        self.assertEqual(self.poll(self.now + 2400), [])
        self.s.age["/w"] = 5000
        self.assertEqual(self.poll(self.now + 2500), ["hung b1"])

    def test_worktree_not_scanned_before_first_sign(self):
        calls = []
        deps = self.s.deps()
        deps["newest_age"] = lambda wt, now, mx: calls.append(wt) or 10 ** 6
        monitor.poll(["b1"], D, self.mem, self.now, deps=deps)
        monitor.poll(["b1"], D, self.mem, self.now + 100, deps=deps)
        self.assertEqual(calls, [])
        monitor.poll(["b1"], D, self.mem, self.now + 1800, deps=deps)
        self.assertEqual(calls, ["/w"])

    def test_slot_holder_never_hung(self):
        self.s.holders = [{"cwd": "/w/build", "pid": 1}]
        self.poll(self.now)
        self.assertEqual(self.poll(self.now + 5000), [])
        self.s.holders = [{"cwd": "/elsewhere", "pid": 1}]
        self.assertEqual(self.poll(self.now + 5100), ["hung b1"])

    def test_hung_unclear_once_never_hung(self):
        with tempfile.TemporaryDirectory() as wt:
            self.s.crews["b1"] = st(cwd=wt)
            self.s.age[wt] = None  # unreadable or too many files
            self.poll(self.now)
            self.assertEqual(self.poll(self.now + 1800), ["hung_unclear b1 scan_capped"])
            self.assertEqual(self.poll(self.now + 2400), [])
        self.s.crews["b1"] = st(size=None)
        self.mem.clear()
        self.poll(self.now)
        self.assertEqual(self.poll(self.now + 1800), ["hung_unclear b1 no_transcript"])
        self.s.crews["b1"] = st(cwd=None)
        self.mem.clear()
        self.poll(self.now)
        self.assertEqual(self.poll(self.now + 1800), ["hung_unclear b1 no_worktree"])

    def test_consent_event_names_kind_once(self):
        self.s.crews["b1"] = st("blocked", consent="api_key")
        self.assertEqual(self.poll(), ["consent b1 api_key"])
        self.assertEqual(self.poll(), [])
        self.mem = json.loads(json.dumps(self.mem))  # a monitor restart on the same screen
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("working")
        self.poll()
        self.s.crews["b1"] = st("blocked", consent="trust")
        self.assertEqual(self.poll(), ["consent b1 trust"])

    def test_limit_screen_event_not_hung_not_gone(self):
        self.s.crews["b1"] = st("idle", bg=0, limit_screen={"kind": "limit", "resets_at": 1761060000.0})
        self.assertEqual(self.poll(self.now), ["limit_screen b1 1761060000"])
        self.assertEqual(self.poll(self.now + 100), [])
        self.s.crews["b1"] = st("working", limit_screen=None)
        self.poll(self.now + 200)  # the screen is read again on the change: episode over
        self.s.crews["b1"] = st("idle", bg=0, limit_screen={"kind": "limit", "resets_at": None})
        self.assertEqual(self.poll(self.now + 300), ["limit_screen b1 -"])
        # a crew on a limit screen is never hung, even working with a still transcript
        self.s.crews["b1"] = st("working", limit_screen={"kind": "limit", "resets_at": None})
        self.poll(self.now + 400)
        self.assertNotIn("hung b1", self.poll(self.now + 9000))

    def test_provider_error_event_once(self):
        self.s.crews["b1"] = st("idle", bg=0, provider_error="invalid api key")
        out = self.poll()
        self.assertIn("provider_error b1 invalid api key", out)
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("working")
        self.poll()
        self.s.crews["b1"] = st("idle", bg=0, provider_error="invalid api key")
        self.assertIn("provider_error b1 invalid api key", self.poll())

    def test_provider_error_episode_replaces_settled(self):
        self.s.crews["b1"] = st("idle", bg=0, provider_error="invalid api key")
        self.assertEqual(self.poll(), ["provider_error b1 invalid api key"])
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("working")
        self.poll()
        self.s.crews["b1"] = st("done", bg=0)  # the error is gone: settled again
        self.assertEqual(self.poll(), ["settled b1 done"])

    def test_priority_order_new_events(self):
        self.s.crews["b1"] = st("blocked", consent="mcp")
        self.s.crews["b2"] = st("idle", pane="p2", limit_screen={"kind": "limit", "resets_at": None})
        self.s.crews["b3"] = st("idle", pane="p3", provider_error="x")
        self.s.crews["b4"] = st("idle", pane="p4", bg=0)
        self.s.limits = lim(90.0, 10.0)
        out = self.poll(names=("b1", "b2", "b3", "b4"))
        self.assertEqual([e.split()[0] for e in out],
                         ["consent", "provider_error", "limit_screen", "limit", "settled"])

    def test_limit_pass_lazy_and_reuses_screens(self):
        for n, pane, status in (("b1", "p1", "working"), ("b2", "p2", "idle"),
                                ("b3", "p3", "working"), ("b4", "p4", "working")):
            self.s.crews[n] = st(status, pane=pane, bg=0, ctx=ctx(10.0))
        self.s.screens.update({"p1": "one", "p2": "two", "p3": "three", "p4": "four"})
        self.poll(names=("b2", "b1"))  # reads p2, p1
        self.assertEqual(self.s.calls["visible"], ["p2", "p1"])
        self.assertTrue(callable(self.s.calls["limit_panes"]))
        reads = len(self.s.calls["visible"])
        texts = self.s.calls["limit_panes"]()
        # one working screen read so far (limit_panes is 2): one more working crew is read
        self.assertEqual(texts, ["two", "one"])  # b3/b4 not in this poll's crew list
        self.assertEqual(len(self.s.calls["visible"]), reads)
        self.s.calls["visible"].clear()
        self.poll(names=("b2", "b1", "b3", "b4"))  # b3, b4 are new: read
        texts = self.s.calls["limit_panes"]()
        self.assertEqual(texts, ["three", "four"])  # both new screens are working crews

    def test_limit_pass_reads_further_working_screens_up_to_limit_panes(self):
        for n, pane in (("b1", "p1"), ("b2", "p2"), ("b3", "p3"), ("b4", "p4")):
            self.s.crews[n] = st("working", pane=pane, ctx=ctx(10.0))
            self.s.screens[pane] = "text-" + pane
        names = ("b1", "b2", "b3", "b4")
        self.poll(names=names)  # first sight: all four read
        self.s.calls["visible"].clear()
        self.poll(names=names)  # unchanged: nothing read, then the limit pass wants some
        texts = self.s.calls["limit_panes"]()
        self.assertEqual(texts, ["text-p1", "text-p2"])  # limit_panes = 2 working crews
        self.assertEqual(self.s.calls["visible"], ["p1", "p2"])

    def test_unknown_window_stays_none(self):
        import scheduler
        d = json.loads(json.dumps(D))
        with tempfile.TemporaryDirectory() as tmp:
            d["limits"]["sensor_dir"] = os.path.join(tmp, "usage")
            self.s.crews["b1"] = st("working", ctx=ctx(10.0))
            self.s.screens["p"] = "just a screen, no status line"
            deps = self.s.deps()
            deps["limits"] = lambda dd, now, pane_texts=None: scheduler.gather_limits(
                dd, now, pane_texts=pane_texts)
            monitor.poll(["b1"], d, self.mem, self.now, deps=deps)
        self.assertEqual(self.mem["last_reading"]["seven_day"], None)
        self.assertEqual(self.mem["last_reading"]["five_hour"], None)
        self.assertEqual(json.loads(json.dumps(self.mem["last_reading"]))["seven_day"], None)


class TestNewestAge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = self.tmp.name

    def _file(self, rel, age, now):
        path = os.path.join(self.wt, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        os.utime(path, (now - age, now - age))

    def test_newest_file_wins_and_skip_dirs_are_ignored(self):
        now = 1000000.0
        self._file("src/a.py", 5000, now)
        self._file("src/b.py", 3000, now)
        for skipped in (".git", "node_modules", ".venv", "__pycache__", "src/__pycache__"):
            self._file(skipped + "/fresh", 1, now)  # fresh files in skipped dirs do not count
        self.assertEqual(monitor._newest_age(self.wt, now, 1000), 3000)

    def test_skipped_dirs_do_not_count_toward_the_cap(self):
        now = 1000000.0
        self._file("a.py", 10, now)
        for i in range(50):
            self._file("node_modules/m%d" % i, 1, now)
        self.assertEqual(monitor._newest_age(self.wt, now, 5), 10)

    def test_more_than_max_files_is_none(self):
        now = 1000000.0
        for i in range(6):
            self._file("f%d" % i, 10, now)
        self.assertIsNone(monitor._newest_age(self.wt, now, 5))
        self.assertEqual(monitor._newest_age(self.wt, now, 6), 10)

    def test_empty_tree_uses_its_own_mtime(self):
        now = 1000000.0
        os.utime(self.wt, (now - 700, now - 700))
        self.assertEqual(monitor._newest_age(self.wt, now, 10), 700)

    def test_missing_or_unreadable_tree_is_none(self):
        self.assertIsNone(monitor._newest_age(os.path.join(self.wt, "nope"), 1.0, 10))
        locked = os.path.join(self.wt, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.access(locked, os.R_OK):
            self.skipTest("directory permissions are not enforced here")
        self.assertIsNone(monitor._newest_age(self.wt, 1.0, 10))

    def test_unreadable_tree_gives_hung_unclear_never_hung(self):
        locked = os.path.join(self.wt, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.access(locked, os.R_OK):
            self.skipTest("directory permissions are not enforced here")
        s = Stub()
        s.crews["b1"] = st(cwd=self.wt)
        deps = s.deps()
        deps["newest_age"] = monitor._newest_age
        mem, now = {}, at(12)
        monitor.poll(["b1"], D, mem, now, deps=deps)
        out = monitor.poll(["b1"], D, mem, now + 1800, deps=deps)
        self.assertEqual(out, ["hung_unclear b1 scan_capped"])

    def test_real_scan_old_tree_is_hung(self):
        now = at(12)
        self._file("a.py", 5000, now)
        s = Stub()
        s.crews["b1"] = st(cwd=self.wt)
        deps = s.deps()
        deps["newest_age"] = monitor._newest_age
        mem = {}
        monitor.poll(["b1"], D, mem, now, deps=deps)
        self.assertEqual(monitor.poll(["b1"], D, mem, now + 1800, deps=deps), ["hung b1"])


class Boom(RuntimeError):
    pass


class TestMonitorFaults(PollBase):
    def test_mid_poll_exception_keeps_events(self):
        del self.s.crews["b1"]
        self.s.limits = lim(90.0, 10.0)
        self.s.over = ["runs"]
        calls = {"n": 0}
        deps = self.s.deps()

        def flaky(d, listed=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Boom("transient")
            return None
        deps["own_context"] = flaky
        # an unguarded failure (the crew list is not a guarded source) aborts the poll
        bad = dict(deps, state=lambda n, a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            monitor.poll(["b1"], D, self.mem, self.now, deps=bad)
        self.assertEqual(self.mem, {})  # nothing committed
        out = monitor.poll(["b1"], D, self.mem, self.now, deps=deps)
        self.assertEqual([e.split()[0] for e in out], ["gone", "limit", "budget"])

    def test_failing_source_does_not_block_others(self):
        del self.s.crews["b1"]
        self.s.limits = lim(90.0, 10.0)
        deps = self.s.deps()

        def broken(d, listed=None):
            raise Boom("pane read")
        deps["own_context"] = broken
        out = monitor.poll(["b1"], D, self.mem, self.now, deps=deps)
        self.assertEqual(out, ["gone b1", "limit 90 10 sensor day"])
        self.assertIn("own_context", self.mem["poll_errors"][0])

    def test_error_event_after_n_polls_once_per_streak(self):
        deps = self.s.deps()

        def broken(d, listed=None):
            raise Boom("x")
        deps["own_context"] = broken
        run = lambda: monitor.poll(["b1"], D, self.mem, self.now, deps=deps)  # noqa: E731
        self.assertEqual([run(), run()], [[], []])
        self.assertEqual(run(), ["error own_context"])
        self.assertEqual(run(), [])
        deps["own_context"] = lambda d, listed=None: None
        self.assertEqual(run(), [])
        deps["own_context"] = broken
        run(), run()
        self.assertEqual(run(), ["error own_context"])

    def test_unknown_streak_not_inflated_by_failed_poll(self):
        self.s.limits = lim(source="unknown")
        deps = self.s.deps()
        with self.assertRaises(KeyboardInterrupt):
            monitor.poll(["b1"], D, self.mem, self.now,
                         deps=dict(deps, state=lambda n, a, **kw: (_ for _ in ()).throw(KeyboardInterrupt())))
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), ["blind"])

    def test_no_hung_without_transcript(self):
        self.s.crews["b1"] = st(size=None)
        self.assertEqual(self.poll(self.now), [])
        out = self.poll(self.now + 5000)
        self.assertNotIn("hung b1", out)
        self.assertNotIn("hung b1", self.poll(self.now + 9000))


class TestProcessBudget(FakeBase):
    """Real dependencies against the fake herdr and a fake bd that log every call."""

    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in ("HOME", "PRORAB_BEADS_DIR", "PRORAB_LOG_DIR",
                                                 "PRORAB_CACHE", "HERDR_PANE_ID", "BD_LOG")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        base = self.dir
        self.home = os.path.join(base, "home")
        os.makedirs(self.home)
        os.makedirs(os.path.join(base, "beads"))
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        self.bd_log = os.path.join(base, "bd.log")
        bd = os.path.join(bindir, "bd")
        with open(bd, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho \"$*\" >> \"$BD_LOG\"\necho '{}'\n")
        os.chmod(bd, 0o755)
        os.environ.update(HOME=self.home, PRORAB_BEADS_DIR=os.path.join(base, "beads", ".beads"),
                          PRORAB_LOG_DIR=os.path.join(base, "logs"),
                          PRORAB_CACHE=os.path.join(base, "cache"), BD_LOG=self.bd_log,
                          PATH=bindir + os.pathsep + os.environ["PATH"])
        os.environ.pop("HERDR_PANE_ID", None)
        self.d = json.loads(json.dumps(D))
        self.sensor = os.path.join(base, "usage")
        self.d["limits"]["sensor_dir"] = self.sensor
        os.makedirs(self.sensor)
        self.names = ("b1", "b2", "b3")
        for i, n in enumerate(self.names, 1):
            cwd = "/home/user/projects/demo-app-%d" % i
            path = transcript.transcript_path(cwd, "sess-%d" % i, self.home)
            os.makedirs(os.path.dirname(path))
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "tests", "fixtures", "session_main.jsonl"),
                      encoding="utf-8") as src, open(path, "w", encoding="utf-8") as out:
                out.write(src.read())
            self.route("pane read w:p%d --source visible" % i, "screen %d\nno status line\n" % i)
        self.mem = {}

    def _agents(self, status):
        self.route("agent list", ok({"agents": [
            {"name": n, "pane_id": "w:p%d" % i, "agent_status": status,
             "cwd": "/home/user/projects/demo-app-%d" % i,
             "agent_session": {"value": "sess-%d" % i}}
            for i, n in enumerate(self.names, 1)]}))

    def _procs(self):
        n = 0
        for path in (os.path.join(self.dir, "calls.log"), self.bd_log):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    n += len(fh.read().splitlines())
        return n

    def _fresh_sensor(self):
        with open(os.path.join(self.sensor, "s1.json"), "w", encoding="utf-8") as fh:
            json.dump({"rate_limits": {"five_hour": {"used_percentage": 12.0,
                                                      "resets_at": time.time() + 3600},
                                       "seven_day": {"used_percentage": 30.0,
                                                     "resets_at": time.time() + 86400}}}, fh)

    def test_process_budget_real_deps(self):
        self._fresh_sensor()
        self._agents("working")
        monitor.poll(list(self.names), self.d, self.mem, time.time())  # first sight: reads
        before = self._procs()
        monitor.poll(list(self.names), self.d, self.mem, time.time())
        self.assertEqual(self._procs() - before, 2)  # agent list + kv list, nothing else
        self.assertIn("spend", self.mem)  # computed by reading files in the first poll
        self.assertIn("rate", self.mem)
        self.assertIn("week_burn", self.mem)
        # all three change status and there is no sensor: the limit pass reuses the screens
        os.remove(os.path.join(self.sensor, "s1.json"))
        self._agents("idle")
        before = self._procs()
        monitor.poll(list(self.names), self.d, self.mem, time.time())
        self.assertEqual(self._procs() - before, 5)  # agents, kv, three screens
        self.assertIsNone(self.mem["last_reading"]["seven_day"])


class TestMonitorMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("PRORAB_LOG_DIR")
        os.environ["PRORAB_LOG_DIR"] = self.tmp.name

        def restore():
            if saved is None:
                os.environ.pop("PRORAB_LOG_DIR", None)
            else:
                os.environ["PRORAB_LOG_DIR"] = saved
        self.addCleanup(restore)
        self.s = Stub()

    def run_main(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = monitor.main(["--crew", "b1", *extra], deps=self.s.deps())
        return rc, buf.getvalue()

    def test_heartbeat_exit(self):
        rc, out = self.run_main("--interval", "0.01", "--heartbeat", "0.05")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "EVENT heartbeat")

    def test_crew_list_reread_every_poll(self):
        deps = self.s.deps()
        seen = []
        lists = iter([[], ["b1"]])
        deps["crews"] = lambda kv=None: next(lists)
        self.s.crews["b1"] = st("done")
        orig = deps["state"]
        deps["state"] = lambda n, a, **kw: (seen.append(n), orig(n, a, **kw))[1]
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.main(["--interval", "0.01", "--heartbeat", "5"], deps=deps)
        self.assertEqual(buf.getvalue().strip(), "EVENT settled b1 done")

    def test_errors_logged_and_heartbeat_carries_last_error(self):
        deps = self.s.deps()

        def broken(d, listed=None):
            raise Boom("x")
        deps["own_context"] = broken
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.main(["--crew", "b1", "--interval", "0.01", "--heartbeat", "5"],
                         deps=deps)
        self.assertEqual(buf.getvalue().strip(), "EVENT error own_context")
        rows = monitor.readings()
        self.assertTrue(all("own_context" in r["errors"][0] for r in rows))
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.main(["--crew", "b1", "--interval", "0.01", "--heartbeat", "0.02"],
                         deps=deps)
        self.assertEqual(buf.getvalue().strip(), "EVENT heartbeat last_error=own_context")

    def test_event_exit_and_log(self):
        self.s.crews["b1"] = st("done")
        self.s.limits = lim(12.0, 3.0, resets_at=12345)
        rc, out = self.run_main("--interval", "0.01", "--heartbeat", "5")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "EVENT settled b1 done")
        rows = monitor.readings()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["five_hour"], r["seven_day"], r["source"], r["resets_at"]),
                         (12.0, 3.0, "sensor", 12345))
        self.assertEqual(r["events"], ["settled b1 done"])
        self.assertEqual(monitor.readings(since=r["ts"] + 1), [])

    def _sleeps(self, deps, extra, trigger_after):
        sleeps = []
        real = monitor.time.sleep

        def fake(sec):
            sleeps.append(sec)
            if len(sleeps) == trigger_after:
                self.s.limits = lim(95.0, 10.0)  # an event ends the run
        monitor.time.sleep = fake
        self.addCleanup(setattr, monitor.time, "sleep", real)
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.main(["--heartbeat", "1000000"] + extra, deps=deps)
        return sleeps

    def test_backoff_grows_and_resets(self):
        deps = self.s.deps()
        self.assertEqual(self._sleeps(deps, [], 4), [90, 180, 300, 300])
        # a working crew keeps the interval at the base
        os.remove(monitor.state_path())
        self.s.limits = lim(10.0, 10.0)
        self.s.crews["b1"] = st("working")
        deps["crews"] = lambda kv=None: ["b1"]
        self.assertEqual(self._sleeps(deps, [], 3), [90, 90, 90])

    def test_backoff_resets_on_status_change_and_interval_flag_fixes_it(self):
        deps = self.s.deps()
        sleeps = []
        real = monitor.time.sleep
        names = ["b1"]
        deps["crews"] = lambda kv=None: names
        self.s.crews["b1"] = st("idle", bg=1)  # quiet: settled is suppressed by a shell

        def fake(sec):
            sleeps.append(sec)
            if len(sleeps) == 3:
                self.s.crews["b1"] = st("done", bg=1)  # status change, still no event
            if len(sleeps) == 5:
                self.s.limits = lim(95.0, 10.0)
        monitor.time.sleep = fake
        self.addCleanup(setattr, monitor.time, "sleep", real)
        with redirect_stdout(io.StringIO()):
            monitor.main(["--heartbeat", "1000000"], deps=deps)
        self.assertEqual(sleeps, [90, 90, 180, 90, 90])
        sleeps.clear()
        self.s.limits = lim(10.0, 10.0)
        self.s.crews["b1"] = st("idle", bg=1)
        monitor.time.sleep = lambda sec: (sleeps.append(sec), len(sleeps) == 3 and setattr(
            self.s, "limits", lim(95.0, 10.0)))
        with redirect_stdout(io.StringIO()):
            monitor.main(["--heartbeat", "1000000", "--interval", "7"], deps=deps)
        self.assertEqual(sleeps, [7, 7, 7])

    def test_restart_does_not_repeat_persisting_condition(self):
        self.s.crews["b1"] = st("done")
        self.s.limits = lim(85.0, 3.0)
        _, out1 = self.run_main("--interval", "0.01", "--heartbeat", "5")
        self.assertIn("EVENT limit 85 3 sensor day", out1)
        self.assertIn("EVENT settled b1 done", out1)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "monitor-state.json")))
        _, out2 = self.run_main("--interval", "0.01", "--heartbeat", "0.05")
        self.assertEqual(out2.strip(), "EVENT heartbeat")


if __name__ == "__main__":
    unittest.main()
