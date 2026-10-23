import io
import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import journal  # noqa: E402
import monitor  # noqa: E402
import transcript  # noqa: E402
import herdr_cli  # noqa: E402
import limits  # noqa: E402
from prorab.tests.test_herdr_cli import FakeBase, fixture, ok  # noqa: E402

D = _defaults.load()
D["week"]["calendar"]["days"] = []  # no working days: W = 0, projected = used + the off-hours work
# never the real cache: tests that need the stop file or the events use a copy (`PollBase.d`)
_NO_CACHE = os.path.join(tempfile.gettempdir(), "prorab-tests-no-such-cache")
D["emergency"]["stop_path"] = os.path.join(_NO_CACHE, "STOP")
D["hooks"]["events_dir"] = os.path.join(_NO_CACHE, "events")


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
       limit_screen=None, provider_error=None, transcript=None):
    return {"status": status, "transcript": transcript, "bg_shells": bg, "ctx": ctx, "session_id": "s", "cwd": cwd,
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
                      "spend": [], "rates": [], "week_burn": [], "pause": [],
                      "site_trust": [], "awake": [], "final_report": []}
        self.reports = {}   # transcript path -> "stage_report" | "handover" | None
        self.awake_error = None    # an exception the hold raises
        self.awake_pid = os.getpid()   # the process the hold "started": alive
        self.trust = False   # what `site_trust` answers: a value, or a dict by site path
        self.pause_error = {}   # crew name -> exception raised once by the pause
        self.pause_results = {}  # crew name -> deliveries returned by its next pauses

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

    def _pause(self, name):
        err = self.pause_error.pop(name, None)
        if err:
            raise err
        self.calls["pause"].append(name)
        results = self.pause_results.get(name)
        return results.pop(0) if results else "accepted"

    def _hold_awake(self, seconds):
        self.calls["awake"].append(seconds)
        if self.awake_error:
            raise self.awake_error
        return self.awake_pid

    def _state(self, name, a, read_screen=True, screen=None, kv=None):
        if a is None:
            return st("gone")
        crew_state = self.crews[name]
        if screen is not None and "login" not in crew_state:
            crew_state = dict(crew_state, login=herdr_cli.not_logged_in(screen))
        return crew_state

    def _final_report(self, path):
        self.calls["final_report"].append(path)
        return self.reports.get(path)

    def _site_trust(self, site):
        self.calls["site_trust"].append(site)
        return self.trust.get(site, False) if isinstance(self.trust, dict) else self.trust

    def _over_budget(self, week, spend=None, kv=None):
        if self.over:  # a canned answer
            return list(self.over)
        return journal.over_budget(week, spend, kv=self.kv if kv is None else kv)

    def deps(self):
        return {
            "state": self._state,
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
            "pause": self._pause,
            "site_trust": self._site_trust,
            "hold_awake": self._hold_awake,
            "final_report": self._final_report,
        }


def ctx(pct, soft=False, hard=False):
    return {"pct": pct, "soft": soft, "hard": hard, "tokens": 0, "window": 1, "model": ""}


class PollBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = json.loads(json.dumps(D))
        self.d["emergency"]["stop_path"] = os.path.join(self.tmp.name, "STOP")
        self.d["hooks"]["events_dir"] = os.path.join(self.tmp.name, "events")
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
        return monitor.poll(list(names), self.d, self.mem, self.now if now is None else now,
                            deps=self.s.deps())



def shutil_copy(src, dst):
    with open(src, encoding="utf-8") as a, open(dst, "w", encoding="utf-8") as b:
        b.write(a.read())


class TestMonitorPoll(PollBase):
    def test_settled_suppressed_by_bg_shell(self):
        self.s.crews["b1"] = st("idle", bg=1)
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("idle", bg=0)
        self.assertEqual(self.poll(), ["settled b1 idle"])

    def test_settled_with_shells_and_a_stage_report(self):
        self.s.reports["t1"] = "stage_report"
        self.s.crews["b1"] = st("idle", bg=2, transcript="t1")
        self.assertEqual(self.poll(), ["settled b1 idle shells:2"])
        self.assertEqual(self.poll(), [])
        self.s.crews["b1"] = st("idle", bg=0, transcript="t1")
        self.assertEqual(self.poll(), ["settled b1 idle"])
        self.assertEqual(self.poll(), [])

    def test_shells_without_a_final_report_are_not_settled(self):
        self.s.reports["t1"] = None   # "waiting for the run"
        self.s.crews["b1"] = st("idle", bg=2, transcript="t1")
        for _ in range(3):
            self.assertEqual(self.poll(), [])

    def test_shells_and_an_older_report_before_a_newer_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for e in ({"type": "assistant", "message": {"content": [
                              {"type": "text", "text": "# Stage 1 report: demo\nbody"}]}},
                          {"type": "user", "message": {"content": "Go on with stage 2."}}):
                    fh.write(json.dumps(e) + "\n")
            self.s.crews["b1"] = st("idle", bg=2, transcript=path)
            deps = self.s.deps()
            deps["final_report"] = transcript.final_report
            for _ in range(3):
                self.assertEqual(monitor.poll(["b1"], self.d, self.mem, self.now, deps=deps), [])

    def test_settled_with_shells_and_handover_ready(self):
        self.s.reports["t1"] = "handover"
        self.s.crews["b1"] = st("idle", bg=1, transcript="t1")
        self.assertEqual(self.poll(), ["settled b1 idle shells:1"])

    def test_final_report_read_only_for_idle_crews_with_shells(self):
        self.s.crews = {"w": st("working", bg=1, transcript="tw", pane="p1"),
                        "i": st("idle", bg=0, transcript="ti", pane="p2"),
                        "k": st("blocked", bg=1, transcript="tk", pane="p3")}
        self.poll(names=("w", "i", "k"))
        self.assertEqual(self.s.calls["final_report"], [])
        self.s.crews["s"] = st("idle", bg=2, transcript="ts", pane="p4")
        self.poll(names=("w", "i", "k", "s"))
        self.poll(names=("w", "i", "k", "s"))
        self.assertEqual(self.s.calls["final_report"], ["ts", "ts"])

    def test_final_report_failure_is_a_failing_source(self):
        self.s.crews["b1"] = st("idle", bg=2, transcript="t1")
        deps = self.s.deps()

        def boom(path):
            raise OSError("disk")
        deps["final_report"] = boom
        out = monitor.poll(["b1"], self.d, self.mem, self.now, deps=deps)
        self.assertFalse([e for e in out if e.startswith("settled")])

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
        self.assertEqual(self.poll(), ["limit 20 50 sensor day week:notify fallback"])  # no reset: fallback
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
        self.assertEqual(self.poll(), ["limit 90 60 sensor day week:notify fallback", "budget sessions"])

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

    def test_monitor_and_plan_agree_on_the_week(self):
        """Fix wave 1, item 6: the same readings log, limits and budget at a shift start give
        the monitor and `scheduler.build_plan` one reading of the week."""
        import types
        import scheduler
        self.d["week"]["calendar"] = _defaults.load()["week"]["calendar"]  # Mon-Fri 09-19, UTC
        log = os.path.join(self.tmp.name, "monitor.log")
        utc = datetime.timezone.utc
        first = datetime.datetime(2026, 10, 21, 12, tzinfo=utc).timestamp()  # a Wednesday
        reset = datetime.datetime(2026, 10, 26, 13, tzinfo=utc).timestamp()
        now = first + 5 * 60  # the shift starts five minutes after the monitor's first poll

        def reading(ts, week):
            return {"ts": ts, "source": "sensor", "five_hour": 10.0, "seven_day": week,
                    "resets_at": None, "seven_day_resets_at": reset}

        def append(*rs):
            with open(log, "a", encoding="utf-8") as fh:
                for r in rs:
                    fh.write(json.dumps(r) + "\n")
        deps = self.s.deps()
        deps["week_burn"] = lambda d, t: limits.week_burn(scheduler.readings(log), d, t)
        self.s.limits = lim(10.0, None)  # the week is not on the sensor yet
        append(reading(first - 4 * 3600, 62.0))  # one reading of the week: no interval yet
        self.assertEqual(monitor.poll([], self.d, self.mem, first, deps=deps), [])
        self.s.limits = lim(10.0, 66.0, week_resets=reset)
        append(*[reading(first - h * 3600, 62.0 + (4 - h)) for h in (3, 2, 1, 0)])
        events = monitor.poll([], self.d, self.mem, now, deps=deps)
        j = types.SimpleNamespace(
            kv_get=lambda k: self.s.kv.get(k), override=lambda: None, graph=lambda: {},
            ready_work_orders=lambda: [], crews=lambda: [], machine=lambda: self.d["machine"],
            budget=lambda: journal.budget(kv=self.s.kv),
            shift_spend=lambda d, t=None: dict(self.s.spend))
        plan = scheduler.build_plan(j, self.d, now, self.s.limits, log, (0.5, 8))
        self.assertEqual(plan["week"]["basis"], "projection")
        self.assertEqual(plan["week"]["allows"], "no_new_mass")
        self.assertEqual(events, ["limit 10 66 sensor day week:no_new_mass"])

    def test_a_new_shift_reads_spend_and_rate_fresh(self):
        """Night-shift variant of the week test: the figures cached before the shift began are
        dropped when `shift.night` or the week start changes, so the next projection reads the
        shift's own spend and rate, as `scheduler.py plan` does."""
        self.week_world()
        self.s.limits = self.wk(20.0, 88.0)
        self.assertEqual(self.poll(), [])  # a day poll: spend 0 and no seven-day rate cached
        self.s.kv = {"shift.night": "1", "shift.week_start": "88"}
        self.s.spend = dict(self.s.spend, usd=60.0)
        self.s.rate = {"five_hour": {"usd_per_pp": 5.0, "source": "default"},
                       "seven_day": {"usd_per_pp": 10.0, "source": "estimated"}}
        # $40 left at 10 $/pp: 4 pp, projected 92: no notice; the stale cache said 98
        self.assertEqual(self.poll(self.now + 90), [])
        self.assertEqual(self.mem["rate"]["week_usd_per_pp"], 10.0)
        self.assertEqual(self.mem["spend"]["value"]["usd"], 60.0)

    def test_a_malformed_week_start_does_not_raise_out_of_the_poll(self):
        self.week_world()
        self.s.kv = {"shift.night": "1", "shift.week_start": "abc"}
        self.s.limits = self.wk(20.0, 88.0)
        self.poll()
        self.assertTrue(any(e.startswith("week_inputs") for e in self.mem["poll_errors"]),
                        self.mem["poll_errors"])

    def test_a_failing_cap_does_not_raise_out_of_the_poll(self):
        with mock.patch.object(monitor.scheduler, "cap_for", side_effect=ValueError("bad")):
            self.poll()
        self.assertTrue(any(e.startswith("cap") for e in self.mem["poll_errors"]),
                        self.mem["poll_errors"])

    def test_emergency_paused_is_kept_when_the_rest_of_the_poll_raises(self):
        self.d["emergency"]["stop_path"] = os.path.join(self.tmp.name, "STOP")
        open(self.d["emergency"]["stop_path"], "w").close()
        with mock.patch.object(monitor, "_limit_state", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.poll()
        self.assertEqual(self.mem["emergency"]["paused"], ["b1"])
        self.poll(self.now + 90)
        self.assertEqual(self.s.calls["pause"], ["b1"])  # not paused a second time

    def test_a_failed_kv_snapshot_skips_the_crew_events_of_the_poll(self):
        self.s.crews = {"b1": st("gone")}
        deps = self.s.deps()

        def broken():
            raise OSError("kv down")
        deps["kv_all"] = broken
        out = monitor.poll(["b1"], self.d, self.mem, self.now, deps=deps)
        self.assertNotIn("gone b1", out)
        self.assertTrue(any(e.startswith("kv") for e in self.mem["poll_errors"]))
        self.assertEqual(monitor.poll(["b1"], self.d, self.mem, self.now + 90,
                                      deps=self.s.deps()), ["gone b1"])

    def test_the_real_rates_and_burn_read_the_log_since_the_week(self):
        deps = monitor.real_deps()
        seen = []
        with mock.patch.object(monitor.scheduler, "readings",
                               side_effect=lambda since=None: seen.append(since) or []), \
                mock.patch.object(monitor.limits, "rates", return_value={}), \
                mock.patch.object(monitor.limits, "week_burn", return_value={}):
            deps["rates"](D, self.now)
            deps["week_burn"](D, self.now)
        week_s = 7 * 86400
        horizon_s = D["week"]["burn_horizon_working_h"] * 86400
        self.assertEqual(seen, [self.now - week_s - horizon_s] * 2)

    def test_monitor_measures_an_unmeasured_burn_again(self):
        self.week_world()
        self.s.limits = self.wk(20.0, 50.0)
        self.s.week_burn = burn(None, self.reset)
        for t in (0, 90):
            self.poll(self.now + t)
        self.assertNotIn("week_burn", self.mem)  # an unmeasured burn is never kept
        self.s.week_burn = burn(1.0, self.reset)
        for t in (180, 270, 360):
            self.poll(self.now + t)
        self.assertEqual([c - self.now for c in self.s.calls["week_burn"]], [0, 90, 180])
        self.assertEqual(self.mem["week_burn"]["value"]["pp_per_h"], 1.0)

    def test_week_fallback_events(self):
        self.s.limits = lim(20.0, 49.9)
        self.assertEqual(self.poll(), [])
        self.s.limits = lim(20.0, 50.0)
        self.assertEqual(self.poll(), ["limit 20 50 sensor day week:notify fallback"])
        self.s.limits = lim(20.0, 70.0)
        self.assertEqual(self.poll(), ["limit 20 70 sensor day week:clearance_only fallback"])

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
        self.s.week_burn = burn(0.5, self.now + 3 * 86400)  # a measured one is kept
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

    def test_statusline_context_reads_only_the_model_line(self):
        deps = self.s.deps()
        deps["statusline_ctx"] = limits.statusline_context
        self.s.crews["b1"] = st(ctx=None)
        self.s.screens["p"] = fixture("statusline_wrapped.txt")
        out = monitor.poll(["b1"], self.d, self.mem, self.now, deps=deps)
        self.assertEqual(out, ["context_soft b1 45.0 (statusline)"])
        self.s.screens["p"] = fixture("statusline_wrapped_no_context.txt")   # its wk row: 81 %
        self.mem.clear()
        out = monitor.poll(["b1"], self.d, self.mem, self.now, deps=deps)
        self.assertEqual(out, [])

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


class TestEmergencyStop(PollBase):
    def setUp(self):
        super().setUp()
        self.s.crews = {"a1": st(pane="p1", ctx=ctx(10.0)), "a2": st(pane="p2", ctx=ctx(10.0))}
        self.names = ("a1", "a2")

    def stop_file(self):
        return self.d["emergency"]["stop_path"]

    def touch_stop(self):
        with open(self.stop_file(), "w"):
            pass

    def release(self):
        os.remove(self.stop_file())

    def test_emergency_stop_pauses_every_crew_once(self):
        self.touch_stop()
        self.assertEqual(self.poll(names=self.names), ["emergency_stop a1,a2"])
        self.assertEqual(self.s.calls["pause"], ["a1", "a2"])
        self.assertEqual(self.mem["emergency"], {"since": self.now, "paused": ["a1", "a2"]})
        self.assertEqual(self.poll(self.now + 90, names=self.names), [])
        self.assertEqual(self.s.calls["pause"], ["a1", "a2"])

    def test_emergency_stop_comes_first(self):
        del self.s.crews["a2"]
        self.s.crews["a1"] = st("blocked", pane="p1", ctx=ctx(10.0))
        self.s.limits = lim(90.0, 10.0)
        self.touch_stop()
        self.assertEqual(self.poll(names=("a1", "a2")), ["emergency_stop a1"])
        self.assertEqual(monitor.EVENT_ORDER[:2], ("emergency_stop", "emergency_released"))

    def test_emergency_stop_silences_other_events(self):
        self.touch_stop()
        self.poll(names=self.names)
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))   # settles
        self.s.crews["a2"] = st("blocked", pane="p2", ctx=ctx(70.0, soft=True), consent="mcp")
        self.s.limits = lim(95.0, 99.0)
        self.s.over = ["usd"]
        self.s.own = ctx(70.0, hard=True)
        self.assertEqual(self.poll(self.now + 90, names=self.names), [])
        del self.s.crews["a2"]
        self.assertEqual(self.poll(self.now + 180, names=self.names), [])
        # readings are still logged and the spend is still computed
        self.assertEqual(self.mem["last_reading"]["five_hour"], 95.0)
        self.assertIn("spend", self.mem)
        self.assertTrue(self.mem["emergency"])

    def test_emergency_release_lets_the_held_events_through(self):
        self.touch_stop()
        self.poll(names=self.names)
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))
        self.s.limits = lim(95.0, 10.0)
        self.assertEqual(self.poll(self.now + 90, names=self.names), [])
        self.release()
        out = self.poll(self.now + 180, names=self.names)
        self.assertEqual(out[0], "emergency_released a1,a2")
        self.assertEqual([e.split()[0] for e in out], ["emergency_released", "limit", "settled"])

    def test_emergency_released_once(self):
        self.touch_stop()
        self.poll(names=self.names)
        self.release()
        self.assertEqual(self.poll(self.now + 90, names=self.names), ["emergency_released a1,a2"])
        self.assertNotIn("emergency", self.mem)
        self.assertEqual(self.poll(self.now + 180, names=self.names), [])
        self.assertEqual(self.s.calls["pause"], ["a1", "a2"])  # nothing is sent on release

    def test_emergency_stop_and_release_survive_the_json_state(self):
        self.touch_stop()
        self.poll(names=self.names)
        self.mem = json.loads(json.dumps(self.mem))
        self.assertEqual(self.poll(self.now + 90, names=self.names), [])
        self.assertEqual(self.s.calls["pause"], ["a1", "a2"])
        self.release()
        self.mem = json.loads(json.dumps(self.mem))
        self.assertEqual(self.poll(self.now + 180, names=self.names), ["emergency_released a1,a2"])

    def test_emergency_stop_pauses_a_crew_registered_meanwhile(self):
        del self.s.crews["a2"]
        self.touch_stop()
        self.assertEqual(self.poll(names=("a1",)), ["emergency_stop a1"])
        self.s.crews["a2"] = st(pane="p2", ctx=ctx(10.0))
        self.assertEqual(self.poll(self.now + 90, names=self.names), [])
        self.assertEqual(self.s.calls["pause"], ["a1", "a2"])
        self.assertEqual(self.mem["emergency"]["paused"], ["a1", "a2"])

    def test_emergency_stop_pauses_only_listed_agents(self):
        del self.s.crews["a2"]
        self.touch_stop()
        self.assertEqual(self.poll(names=self.names), ["emergency_stop a1"])
        self.assertEqual(self.s.calls["pause"], ["a1"])

    def test_emergency_stop_with_no_crews_still_announces(self):
        self.s.crews = {}
        self.touch_stop()
        self.assertEqual(self.poll(names=()), ["emergency_stop -"])
        self.assertEqual(self.poll(self.now + 90, names=()), [])

    def test_emergency_failed_pause_is_retried(self):
        self.s.pause_error["a1"] = RuntimeError("herdr down")
        self.touch_stop()
        self.assertEqual(self.poll(names=self.names), ["emergency_stop a2"])
        self.assertEqual(self.mem["emergency"]["paused"], ["a2"])
        self.assertTrue(any("pause.a1" in e for e in self.mem["poll_errors"]), self.mem)
        self.assertEqual(self.poll(self.now + 90, names=self.names), [])
        self.assertEqual(self.s.calls["pause"], ["a2", "a1"])
        self.assertEqual(self.mem["emergency"]["paused"], ["a2", "a1"])

    def test_emergency_refused_pause_is_not_counted_and_is_sent_again_each_poll(self):
        self.s.pause_results["a1"] = ["blocked", "stalled"]
        self.touch_stop()
        self.assertEqual(self.poll(names=self.names), ["emergency_stop a2"])
        self.assertEqual(self.mem["emergency"]["paused"], ["a2"])
        self.assertEqual(self.poll(self.now + 90, names=self.names), [])  # refused again
        self.assertEqual(self.mem["emergency"]["paused"], ["a2"])
        self.assertEqual(self.poll(self.now + 180, names=self.names), [])  # accepted now
        self.assertEqual(self.mem["emergency"]["paused"], ["a2", "a1"])
        self.assertEqual(self.s.calls["pause"], ["a1", "a2", "a1", "a1"])  # once per poll
        self.assertEqual(self.poll(self.now + 270, names=self.names), [])
        self.assertEqual(self.s.calls["pause"], ["a1", "a2", "a1", "a1"])
        self.release()
        self.assertEqual(self.poll(self.now + 360, names=self.names),
                         ["emergency_released a2,a1"])

    def test_emergency_check_uses_the_stop_path_of_the_defaults(self):
        self.d["emergency"]["stop_path"] = os.path.join(self.tmp.name, "other", "STOP")
        os.makedirs(os.path.dirname(self.d["emergency"]["stop_path"]))
        self.assertEqual(self.poll(names=self.names), [])
        with open(self.d["emergency"]["stop_path"], "w"):
            pass
        self.assertEqual(self.poll(self.now + 90, names=self.names), ["emergency_stop a1,a2"])


class TestHookEvents(PollBase):
    SID = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        super().setUp()
        self.s.crews = {"a1": st("working", pane="p1", ctx=ctx(10.0))}
        self.s.kv.update({"hooks.enabled": "1", "crew.a1.session": self.SID})
        self.events_dir = self.d["hooks"]["events_dir"]

    def events_file(self, sid=None):
        return os.path.join(self.events_dir, (sid or self.SID) + ".jsonl")

    def write_event(self, event, sid=None, raw=None, **fields):
        os.makedirs(self.events_dir, exist_ok=True)
        rec = dict({"ts": self.now, "event": event, "session_id": sid or self.SID}, **fields)
        with open(self.events_file(sid), "a", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(rec) + "\n")

    def prime(self):
        """The first sight of the crew (its events file is read from its end on)."""
        self.assertEqual(self.poll(names=("a1",)), [])

    def test_monitor_prefers_the_stop_event(self):
        self.prime()
        self.write_event("Stop", background_tasks=[], last_assistant_message="done")
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))   # herdr shows the change
        self.s.calls["visible"].clear()
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), ["settled a1 done"])
        self.assertEqual(self.s.calls["visible"], [])    # no screen was read for it
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), [])
        self.s.crews["a1"] = st("working", pane="p1", ctx=ctx(10.0))
        self.poll(self.now + 270, names=("a1",))
        self.write_event("Stop", background_tasks=[])
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))
        self.assertEqual(self.poll(self.now + 360, names=("a1",)), ["settled a1 done"])

    def test_stop_event_still_reads_the_screen_while_a_consent_episode_is_open(self):
        dialog = fixture("consent", "mcp.txt")
        self.prime()
        self.s.screens["p1"] = dialog
        self.s.crews["a1"] = st("blocked", pane="p1", ctx=ctx(10.0), consent="mcp")
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), ["consent a1 mcp"])
        self.write_event("Stop", background_tasks=[])
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0), consent="mcp")
        self.s.calls["visible"].clear()
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), [])   # the prompt is still up
        self.assertEqual(self.s.calls["visible"], ["p1"])
        self.s.screens["p1"] = "a clean screen\n"
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))
        self.assertEqual(self.poll(self.now + 270, names=("a1",)), ["settled a1 idle"])

    def test_stop_event_with_background_tasks_settles_nothing(self):
        self.prime()
        self.write_event("Stop", background_tasks=["npm run dev"])
        self.write_event("Stop", raw=json.dumps({"event": "Stop", "ts": 1}) + "\n")  # field absent
        self.s.crews["a1"] = st("idle", pane="p1", bg=1, ctx=ctx(10.0))
        self.s.screens["p1"] = "screen"
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])

    def test_stop_event_with_background_tasks_and_a_report_settles(self):
        self.s.reports["t1"] = "stage_report"
        self.prime()
        self.write_event("Stop", background_tasks=["npm run dev"])
        self.s.crews["a1"] = st("idle", pane="p1", bg=0, ctx=ctx(10.0), transcript="t1")
        self.s.screens["p1"] = "screen"
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), ["settled a1 idle shells:1"])
        # the event's count holds: no plain settled while the crew has not worked again
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), [])
        self.assertEqual(self.poll(self.now + 270, names=("a1",)), [])

    def test_a_screen_read_after_the_stop_event_clears_its_shell_count(self):
        self.s.reports["t1"] = "stage_report"
        self.prime()
        self.write_event("Stop", background_tasks=["npm run dev"])
        self.s.crews["a1"] = st("idle", pane="p1", bg=0, ctx=ctx(10.0), transcript="t1")
        self.s.screens["p1"] = "screen"
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), ["settled a1 idle shells:1"])
        # a later read (the status changed) shows no shell: the plain settled may follow
        self.s.crews["a1"] = st("done", pane="p1", bg=0, ctx=ctx(10.0), transcript="t1")
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), ["settled a1 done"])

    def test_stop_event_of_a_working_crew_is_ignored(self):
        self.prime()
        self.write_event("Stop", background_tasks=[])
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), ["settled a1 idle"])

    def test_screens_stay_the_fallback_without_a_stop_event(self):
        self.prime()
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), ["settled a1 idle"])
        self.assertEqual(self.s.calls["visible"], ["p1"] * 2)

    def test_notification_permission_prompt_is_consent(self):
        self.prime()
        self.write_event("Notification", notification_type="idle_prompt")
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])
        self.write_event("Notification", notification_type="permission_prompt")
        self.assertEqual(self.poll(self.now + 180, names=("a1",)),
                         ["consent a1 tool_permission"])
        # the prompt stays on screen: the episode stays open (a read that shows none ends it)
        self.s.screens["p1"] = fixture("consent", "tool_permission.txt")
        self.s.crews["a1"] = st("working", pane="p1", ctx=ctx(10.0), consent="tool_permission")
        self.write_event("Notification", notification_type="permission_prompt")
        self.assertEqual(self.poll(self.now + 270, names=("a1",)), [])   # once per episode

    def test_precompact_auto_is_compacted(self):
        self.prime()
        self.write_event("PreCompact", trigger="manual")
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])
        self.write_event("PreCompact", trigger="auto")
        self.write_event("PreCompact", trigger="auto")
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), ["compacted a1"])
        self.assertEqual(self.poll(self.now + 270, names=("a1",)), [])

    def test_compacted_follows_prorab_context_in_the_order(self):
        self.prime()
        self.s.own = ctx(70.0, soft=True)
        self.s.crews["a1"] = st("blocked", pane="p1", ctx=ctx(10.0))
        self.write_event("PreCompact", trigger="auto")
        out = self.poll(self.now + 90, names=("a1",))
        self.assertEqual([e.split()[0] for e in out],
                         ["prorab_context", "compacted", "settled"])
        order = monitor.EVENT_ORDER
        self.assertEqual(order.index("compacted"), order.index("prorab_context") + 1)

    def test_events_ignored_while_hooks_are_not_enabled(self):
        self.prime()
        del self.s.kv["hooks.enabled"]
        self.write_event("PreCompact", trigger="auto")
        self.write_event("Notification", notification_type="permission_prompt")
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])

    def test_events_read_once_and_a_partial_line_waits(self):
        self.prime()
        self.write_event("PreCompact", raw=json.dumps({"event": "PreCompact", "trigger": "auto"}))
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])   # no newline yet
        with open(self.events_file(), "a", encoding="utf-8") as fh:
            fh.write("\n")
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), ["compacted a1"])
        self.write_event("PreCompact", raw="not json\n")
        self.assertEqual(self.poll(self.now + 270, names=("a1",)), [])
        self.mem = json.loads(json.dumps(self.mem))
        self.assertEqual(self.poll(self.now + 360, names=("a1",)), [])

    def test_events_before_the_first_sight_are_not_replayed(self):
        self.write_event("PreCompact", trigger="auto")
        self.assertEqual(self.poll(names=("a1",)), [])
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])

    def test_events_file_that_appears_after_the_first_sight_is_read_from_its_start(self):
        self.prime()
        self.write_event("PreCompact", trigger="auto")
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), ["compacted a1"])

    def test_a_new_session_starts_a_new_offset(self):
        self.prime()
        self.write_event("PreCompact", trigger="auto")
        other = "99999999-2222-3333-4444-555555555555"
        self.s.kv["crew.a1.session"] = other
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])   # first sight: its end
        self.write_event("PreCompact", sid=other, trigger="auto")
        self.assertEqual(self.poll(self.now + 180, names=("a1",)), ["compacted a1"])

    def test_unsafe_session_id_is_never_read(self):
        outside = os.path.join(self.tmp.name, "x.jsonl")
        with open(outside, "w") as fh:
            fh.write(json.dumps({"event": "PreCompact", "trigger": "auto"}) + "\n")
        self.s.kv["crew.a1.session"] = "../x"
        os.makedirs(self.events_dir, exist_ok=True)
        self.assertEqual(self.poll(names=("a1",)), [])
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])

    def test_old_event_files_pruned(self):
        os.makedirs(self.events_dir)
        old, fresh = self.events_file("old"), self.events_file("fresh")
        for path, age in ((old, 90000), (fresh, 10)):
            with open(path, "w") as fh:
                fh.write("{}\n")
            os.utime(path, (self.now - age, self.now - age))
        stray = os.path.join(self.events_dir, "notes.txt")
        with open(stray, "w") as fh:
            fh.write("x")
        os.utime(stray, (self.now - 90000, self.now - 90000))
        self.poll(names=("a1",))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(stray))   # only `*.jsonl` event files are the reader's

    def test_pruning_runs_now_and_then_and_needs_the_hooks(self):
        os.makedirs(self.events_dir)
        old = self.events_file("old")
        with open(old, "w") as fh:
            fh.write("{}\n")
        os.utime(old, (self.now - 90000, self.now - 90000))
        del self.s.kv["hooks.enabled"]
        self.poll(names=("a1",))
        self.assertTrue(os.path.exists(old))
        self.s.kv["hooks.enabled"] = "1"
        self.poll(self.now + 90, names=("a1",))
        self.assertFalse(os.path.exists(old))


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


class TestAwake(PollBase):
    def awake(self):
        return self.s.calls["awake"]

    def test_awake_hold_starts_once_while_a_crew_is_listed(self):
        for i in range(3):
            self.poll(self.now + 90 * i)
        self.assertEqual(self.awake(), [1200])
        self.assertEqual(self.mem["awake"]["until"], self.now + 1200)
        self.assertEqual(self.mem["awake"]["since"], self.now)

    def test_awake_hold_renewed_when_little_is_left(self):
        self.poll()
        self.poll(self.now + 599)      # 601 s are left
        self.assertEqual(self.awake(), [1200])
        self.poll(self.now + 600)      # 600 s are left
        self.assertEqual(self.awake(), [1200, 1200])
        self.assertEqual(self.mem["awake"]["until"], self.now + 600 + 1200)
        self.poll(self.now + 690)
        self.assertEqual(self.awake(), [1200, 1200])

    def test_awake_hold_void_after_a_reboot(self):
        self.mem["awake"] = {"since": self.now - 300, "until": self.now + 900, "mono": 5000.0}
        with mock.patch.object(monitor.time, "monotonic", return_value=100.0):
            self.poll()
        self.assertEqual(self.awake(), [1200])
        self.assertEqual(self.mem["awake"]["mono"], 100.0)

    def test_awake_hold_gone_with_its_process(self):
        self.poll()
        self.assertEqual(self.awake(), [1200])
        self.poll(self.now + 90)        # its holder is alive (this process)
        self.assertEqual(self.awake(), [1200])
        self.assertEqual(self.mem["awake"]["pid"], os.getpid())
        with mock.patch.object(monitor.os, "kill", side_effect=ProcessLookupError):
            self.poll(self.now + 180)   # the holder was killed: renewed at once
        self.assertEqual(self.awake(), [1200, 1200])

    def test_awake_hold_for_an_idle_crew(self):
        self.s.crews["b1"] = st("idle")
        for i in range(3):
            self.poll(self.now + 90 * i)
        self.assertEqual(self.awake(), [1200])

    def test_no_awake_hold_without_a_listed_crew(self):
        self.poll(names=())
        self.s.crews["b1"] = st("gone")
        self.poll(self.now + 90)
        self.s.crews["b1"] = st("working")
        self.s.kv["crew.b1.exiting"] = str(self.now + 180)
        self.poll(self.now + 180)
        self.assertEqual(self.awake(), [])
        self.assertNotIn("awake", self.mem)

    def test_awake_hold_kept_through_the_emergency_stop(self):
        with open(self.d["emergency"]["stop_path"], "w"):
            pass
        for i in range(3):
            self.poll(self.now + 90 * i)
        self.assertEqual(self.awake(), [1200])
        self.assertEqual(self.mem["awake"]["until"], self.now + 1200)

    def test_failed_awake_hold_is_a_failing_source(self):
        self.s.awake_error = OSError("spawn failed")
        self.assertEqual(self.poll(), [])
        self.assertTrue(any(e.startswith("awake: OSError") for e in self.mem["poll_errors"]))
        self.assertNotIn("awake", self.mem)
        self.assertEqual(self.poll(self.now + 90), [])
        self.assertEqual(self.poll(self.now + 180), ["error awake"])
        self.assertEqual(self.poll(self.now + 270), [])
        self.assertEqual(len(self.awake()), 4)    # tried again on every poll
        self.s.awake_error = None
        self.poll(self.now + 360)
        self.assertEqual(self.mem["awake"]["until"], self.now + 360 + 1200)

    def test_no_caffeinate_raises_error_awake_once(self):
        self.s.awake_error = FileNotFoundError("caffeinate")
        self.assertEqual(self.poll(), ["error awake"])
        self.assertTrue(self.mem["awake_missing"])
        for i in range(1, 6):
            self.assertEqual(self.poll(self.now + 90 * i), [])
        restarted = json.loads(json.dumps(self.mem))    # the state file, read by a new monitor
        self.assertEqual(monitor.poll(["b1"], self.d, restarted, self.now + 900,
                                      deps=self.s.deps()), [])
        self.assertEqual(self.awake(), [1200])

    def test_no_caffeinate_under_the_emergency_stop_is_raised_after_the_release(self):
        self.s.awake_error = FileNotFoundError("caffeinate")
        stop = self.d["emergency"]["stop_path"]
        with open(stop, "w"):
            pass
        out = self.poll()
        self.assertNotIn("error awake", out)
        self.assertNotIn("awake_missing", self.mem)
        os.remove(stop)
        self.assertEqual(self.poll(self.now + 90), ["emergency_released b1", "error awake"])
        self.assertEqual(self.poll(self.now + 180), [])
        self.assertEqual(len(self.awake()), 2)

    def test_log_rows_carry_the_uptime_clock_and_the_hold(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = {"t": 1_000_000.0, "m": 50_000.0}

        def fake_sleep(sec):
            clock["t"] += sec
            clock["m"] += sec
        with mock.patch.dict(os.environ, {"PRORAB_LOG_DIR": tmp.name, "HOME": tmp.name}), \
                mock.patch.object(monitor.time, "sleep", fake_sleep), \
                mock.patch.object(monitor.time, "time", lambda: clock["t"]), \
                mock.patch.object(monitor.time, "monotonic", lambda: clock["m"]), \
                redirect_stdout(io.StringIO()):
            monitor.main(["--crew", "b1", "--interval", "2", "--heartbeat", "10"],
                         deps=self.s.deps())
            rows = monitor.readings()
        self.assertGreaterEqual(len(rows), 4)
        for i, r in enumerate(rows):
            self.assertEqual(r["mono"], 50_000.0 + 2 * i)
            self.assertEqual(r["awake_until"], 1_000_000.0 + 1200)
        self.assertEqual(self.awake(), [1200])


class TestLogin(PollBase):
    def setUp(self):
        super().setUp()
        self.s.crews = {"a1": st("working", pane="p1", ctx=ctx(10.0))}
        self.poll(names=("a1",))
        self.t = 0

    def go(self, status, screen=None, names=("a1",), **kw):
        self.t += 90
        self.s.crews["a1"] = st(status, pane="p1", ctx=ctx(10.0), **kw)
        if screen is not None:
            self.s.screens["p1"] = fixture(*screen.split("/"))
        return self.poll(self.now + self.t, names=names)

    def test_login_raises_one_event_per_episode(self):
        self.assertEqual(self.go("idle", "login/not_logged_in.txt"), ["login a1"])
        self.assertEqual(self.go("idle"), [])
        self.assertEqual(self.go("idle"), [])

    def test_login_presses_nothing(self):
        self.s.calls["visible"].clear()
        self.go("idle", "login/not_logged_in.txt")
        self.assertEqual(self.s.calls["visible"], ["p1"])   # the read of the status change
        self.assertEqual(self.s.calls["pause"], [])

    def test_login_episode_ends_when_the_crew_works_again(self):
        self.go("idle", "login/not_logged_in.txt")
        self.assertEqual(self.go("working"), [])
        self.assertEqual(self.go("idle", "login/after_resume.txt"), ["settled a1 idle"])

    def test_login_again_after_a_failed_resume(self):
        self.go("idle", "login/not_logged_in.txt")
        self.go("working")
        self.assertEqual(self.go("idle", "login/not_logged_in.txt"), ["login a1"])

    def test_login_dialog_on_a_blocked_crew(self):
        self.assertEqual(self.go("blocked", "login/select_method.txt"), ["login a1"])
        self.assertEqual(self.go("blocked"), [])

    def test_login_on_two_crews(self):
        self.s.crews = {"a1": st("working", pane="p1", ctx=ctx(10.0)),
                        "b1": st("working", pane="p2", ctx=ctx(10.0)),
                        "c1": st("working", pane="p3", ctx=ctx(10.0)),
                        "d1": st("gone", pane="p4")}
        names = ("a1", "b1", "c1", "d1")
        self.assertEqual(self.poll(self.now + 90, names=names), ["gone d1"])
        for pane in ("p1", "p2"):
            self.s.screens[pane] = fixture("login", "not_logged_in.txt")
        self.s.screens["p3"] = fixture("consent", "mcp.txt")
        self.s.crews["a1"] = st("idle", pane="p1", ctx=ctx(10.0))
        self.s.crews["b1"] = st("idle", pane="p2", ctx=ctx(10.0))
        self.s.crews["c1"] = st("blocked", pane="p3", ctx=ctx(10.0), consent="mcp")
        self.assertEqual(self.poll(self.now + 180, names=names),
                         ["login a1", "login b1", "consent c1 mcp"])

    def test_login_with_hooks_still_reads_the_screen(self):
        # a failed turn fires StopFailure, never Stop: no Stop record, the status change is read
        self.s.kv.update({"hooks.enabled": "1",
                          "crew.a1.session": "11111111-2222-3333-4444-555555555555"})
        self.assertEqual(self.go("idle", "login/not_logged_in.txt"), ["login a1"])


class TestOwnContext(unittest.TestCase):
    SID = "sess-own"
    CWD = "/home/user/projects/demo-app"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = json.loads(json.dumps(D))
        self.sensor = os.path.join(self.tmp.name, "usage")
        os.makedirs(self.sensor)
        self.d["limits"]["sensor_dir"] = self.sensor
        env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "HERDR_PANE_ID": "w:p1"})
        env.start()
        self.addCleanup(env.stop)
        path = transcript.transcript_path(self.CWD, self.SID, self.tmp.name)
        os.makedirs(os.path.dirname(path))
        shutil_copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                 "session_main.jsonl"), path)
        self.listed = [{"name": "foreman", "pane_id": "w:p1", "cwd": self.CWD,
                        "agent_session": {"value": self.SID}}]

    def sensor_file(self, age=0):
        path = os.path.join(self.sensor, self.SID + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"context_window": {"context_window_size": 1000000, "current_usage": {
                "input_tokens": 100000, "output_tokens": 0}}}, fh)
        if age:
            os.utime(path, (time.time() - age, time.time() - age))

    def test_own_context_prefers_the_sensor(self):
        self.sensor_file()
        ctx_own = monitor._own_context(self.d, listed=self.listed)
        self.assertEqual((ctx_own["source"], ctx_own["window"], ctx_own["pct"]),
                         ("sensor", 1000000, 10.0))
        self.sensor_file(age=self.d["limits"]["sensor_max_age_s"] + 60)
        ctx_own = monitor._own_context(self.d, listed=self.listed)
        self.assertEqual(ctx_own["source"], "transcript")


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
                                                 "PRORAB_CACHE", "HERDR_PANE_ID", "BD_LOG", "KV_JSON")}

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
            fh.write("#!/bin/sh\necho \"$*\" >> \"$BD_LOG\"\n"
                     "case \"$1 $2\" in 'kv list') cat \"$KV_JSON\";; *) echo '{}';; esac\n")
        os.chmod(bd, 0o755)
        self.kv_json = os.path.join(base, "kv.json")
        with open(self.kv_json, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.environ.update(KV_JSON=self.kv_json, HOME=self.home, PRORAB_BEADS_DIR=os.path.join(base, "beads", ".beads"),
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
        # a stub caffeinate that logs its arguments and stays up (never the real one)
        self.caff_log = os.path.join(base, "caffeinate.log")
        caff = os.path.join(bindir, "caffeinate")
        with open(caff, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho \"$*\" >> \"%s\"\nexec sleep 30\n" % self.caff_log)
        os.chmod(caff, 0o755)
        self.addCleanup(self._reap_holders)

    def _reap_holders(self):
        import signal
        pid = (self.mem.get("awake") or {}).get("pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except OSError:
                pass

    def _caffeinate_calls(self):
        deadline = time.time() + 5
        while not os.path.exists(self.caff_log) and time.time() < deadline:
            time.sleep(0.05)
        try:
            with open(self.caff_log, encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

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
        self.assertNotIn("week_burn", self.mem)  # no readings log here: unmeasured, not kept
        # all three change status and there is no sensor: the limit pass reuses the screens
        os.remove(os.path.join(self.sensor, "s1.json"))
        self._agents("idle")
        before = self._procs()
        monitor.poll(list(self.names), self.d, self.mem, time.time())
        self.assertEqual(self._procs() - before, 5)  # agents, kv, three screens
        self.assertIsNone(self.mem["last_reading"]["seven_day"])


    def test_process_budget_settled_with_shells(self):
        self._fresh_sensor()
        counts = []
        for tail, expect in (("# Stage 1 report: demo\nall done", True),
                             ("Waiting for the run.", False)):
            for i, n in enumerate(self.names, 1):
                path = transcript.transcript_path("/home/user/projects/demo-app-%d" % i,
                                                  "sess-%d" % i, self.home)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")
                    fh.write(json.dumps({"type": "assistant", "message": {"content": [
                        {"type": "text", "text": tail}]}}) + "\n")
                self.route("pane read w:p%d --source visible" % i, "run \u00b7 2 shells\n")
            self.mem = {}
            self._agents("working")
            monitor.poll(list(self.names), self.d, self.mem, time.time())
            self._agents("idle")
            before = self._procs()
            out = monitor.poll(list(self.names), self.d, self.mem, time.time())
            counts.append(self._procs() - before)
            self.assertEqual(
                [e for e in out if e.startswith("settled")],
                ["settled %s idle shells:2" % n for n in self.names] if expect else [])
        self.assertEqual(counts[0], counts[1])

    def test_process_budget_awake_hold(self):
        self._fresh_sensor()
        self._agents("working")
        monitor.poll(list(self.names), self.d, self.mem, time.time())
        self.assertEqual(self._caffeinate_calls(), ["-is -t 1200"])
        with open(os.path.join(self.dir, "calls.log"), encoding="utf-8") as fh:
            self.assertIn("agent list", fh.read())
        with open(self.bd_log, encoding="utf-8") as fh:
            self.assertIn("kv list", fh.read())
        for _ in range(2):
            before = self._procs()
            monitor.poll(list(self.names), self.d, self.mem, time.time())
            self.assertEqual(self._procs() - before, 2)   # agent list + kv list only
        self.assertEqual(self._caffeinate_calls(), ["-is -t 1200"])

    def test_process_budget_with_timers_and_a_trust_watch(self):
        site = "/home/user/projects/demo-app"
        with open(os.path.join(self.home, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"projects": {site: {"hasTrustDialogAccepted": True}}}, fh)
        kv = {"prorab:active": "w:p0",
              "due.check-tests": json.dumps({"at": time.time() + 3600, "text": "t", "set_at": 0}),
              "consent.wo-1.trust": json.dumps(
                  {"kind": "trust", "gate": "g-1", "bead": "wo-1", "pane": "w:p77", "name": "a9",
                   "site": site, "since": 1.0, "watch": True, "no_watch": None})}
        with open(self.kv_json, "w", encoding="utf-8") as fh:
            json.dump(kv, fh)
        self._fresh_sensor()
        self._agents("working")
        monitor.poll(list(self.names), self.d, self.mem, time.time())
        before = self._procs()
        events = monitor.poll(list(self.names), self.d, self.mem, time.time())
        self.assertEqual(self._procs() - before, 2)   # agent list + kv list; the trust is a file read
        self.assertEqual(events, [])   # the watch fired on the first poll (the pane is empty)

    def test_emergency_check_starts_no_process(self):
        self._fresh_sensor()
        self._agents("working")
        stop = os.path.join(self.dir, "STOP")
        self.d["emergency"]["stop_path"] = stop
        self.d["hooks"]["events_dir"] = os.path.join(self.dir, "events")
        pauses = []
        deps = monitor.real_deps()
        deps["pause"] = lambda name: pauses.append(name) or "accepted"
        poll = lambda: monitor.poll(list(self.names), self.d, self.mem, time.time(),  # noqa: E731
                                    deps=deps)
        poll()   # first sight: reads
        before = self._procs()
        poll()
        self.assertEqual(self._procs() - before, 2)   # agent list + kv list
        with open(stop, "w"):
            pass
        tests = []
        real_exists = os.path.exists
        before = self._procs()
        with mock.patch.object(monitor.os.path, "exists",
                               side_effect=lambda p: tests.append(p) or real_exists(p)):
            events = poll()
        self.assertEqual(events, ["emergency_stop b1,b2,b3"])
        self.assertEqual(pauses, ["b1", "b2", "b3"])
        self.assertEqual(self._procs() - before, 2)   # apart from the pauses: the same two
        self.assertEqual(tests.count(stop), 1)        # one file test per poll
        before = self._procs()
        self.assertEqual(poll(), [])
        self.assertEqual(self._procs() - before, 2)
        self.assertEqual(pauses, ["b1", "b2", "b3"])
        os.remove(stop)
        before = self._procs()
        self.assertEqual(poll(), ["emergency_released b1,b2,b3"])
        self.assertEqual(self._procs() - before, 2)


LOCK = "w:p1"
SITE = "/home/user/projects/demo-app"


def add_watch(stub, kind="trust", wo="wo-1", pane="w:p9", name="a1", site=SITE,
              since=1000.0, watch=True, no_watch=None):
    stub.kv["consent.%s.%s" % (wo, kind)] = json.dumps(
        {"kind": kind, "gate": "g-1", "bead": wo, "pane": pane, "name": name, "site": site,
         "since": since, "watch": watch, "no_watch": no_watch})


class TimerBase(PollBase):
    def setUp(self):
        super().setUp()
        self.s.kv["prorab:active"] = LOCK
        self.s.crews = {}
        self.T = self.now + 600

    def set_due(self, label="check-tests", at=None, text="the unit suite of wo-12"):
        self.s.kv["due." + label] = json.dumps(
            {"at": self.T if at is None else at, "text": text, "set_at": self.now})

    def poll(self, now=None, names=()):
        return super().poll(now, names=names)


class TestDue(TimerBase):
    def test_due_fires_once_at_its_time(self):
        self.set_due()
        self.assertEqual(self.poll(self.T - 1), [])
        self.assertEqual(self.poll(self.T), ["due check-tests the unit suite of wo-12"])
        self.assertEqual(self.poll(self.T + 90), [])

    def test_due_line_carries_the_text(self):
        self.set_due(text="x" * 120 + "\nsecond line")
        self.assertEqual(self.poll(self.T), ["due check-tests " + "x" * 80])
        self.set_due("other", text="")
        self.assertEqual(self.poll(self.T + 1), ["due other"])

    def test_due_fires_again_for_a_new_foreman(self):
        self.set_due()
        self.assertEqual(len(self.poll(self.T)), 1)
        self.s.kv["prorab:active"] = "w:p7"
        self.assertEqual(self.poll(self.T + 90), ["due check-tests the unit suite of wo-12"])
        self.assertEqual(self.poll(self.T + 180), [])

    def test_due_cleared_or_moved(self):
        self.set_due()
        self.poll(self.T)
        del self.s.kv["due.check-tests"]
        self.assertEqual(self.poll(self.T + 10), [])
        self.assertNotIn("check-tests", self.mem["due"])
        self.set_due(at=self.T + 600)
        self.assertEqual(self.poll(self.T + 500), [])
        self.assertEqual(len(self.poll(self.T + 600)), 1)

    def test_due_not_silenced_by_override_or_stop(self):
        self.set_due()
        self.s.override = {"goal": "g"}
        self.assertEqual(len(self.poll(self.T)), 1)
        self.set_due("second", at=self.T + 100)
        with open(self.d["emergency"]["stop_path"], "w"):
            pass
        self.assertEqual(self.poll(self.T + 100),
                         ["emergency_stop -", "due second the unit suite of wo-12"])
        self.assertEqual(self.poll(self.T + 200), [])   # fired once: the mark was kept

    def test_due_unreadable_record_is_a_failing_source(self):
        self.s.kv["due.x"] = "garbage"
        self.set_due()
        self.assertEqual(self.poll(self.T), ["due check-tests the unit suite of wo-12"])
        self.assertTrue(any(e.startswith("due.x") for e in self.mem["poll_errors"]))
        out = []
        for i in range(self.d["monitor"]["error_polls"] - 1):
            out = self.poll(self.T + 10 + i)
        self.assertEqual(out, ["error due.x"])

    def test_next_due_is_the_earliest_unfired(self):
        self.set_due("a", at=self.T)
        self.set_due("b", at=self.T + 50)
        self.poll(self.now)
        self.assertEqual(self.mem["next_due"], self.T)
        self.poll(self.T)
        self.assertEqual(self.mem["next_due"], self.T + 50)
        self.poll(self.T + 50)
        self.assertIsNone(self.mem["next_due"])

    def test_event_order_due_and_consent_done(self):
        self.s.crews = {"a1": st("idle", pane="w:p9")}
        self.set_due()
        self.s.over = ["usd"]
        self.s.own = ctx(95.0, hard=True)
        add_watch(self.s, "mcp")
        out = self.poll(self.T, names=("a1",))
        self.assertEqual([e.split()[0] for e in out],
                         ["budget", "due", "prorab_context", "consent_done", "settled"])
        order = monitor.EVENT_ORDER
        self.assertEqual(order.index("due"), order.index("budget") + 1)
        self.assertEqual(order.index("consent_done"), order.index("settled") - 1)


class TestConsentDone(TimerBase):
    def test_consent_done_trust(self):
        add_watch(self.s, "trust")
        self.s.trust = False
        self.assertEqual(self.poll(), [])
        self.s.trust = True
        self.s.crews["x"] = st("idle", pane="w:p9", bg=1)   # the customer's claude still runs
        self.assertEqual(self.poll(), [])
        del self.s.crews["x"]
        self.assertEqual(self.poll(), ["consent_done wo-1 trust"])
        self.assertEqual(self.poll(), [])

    def test_consent_done_trust_unknown_is_not_yet(self):
        add_watch(self.s, "trust")
        self.s.trust = None
        self.assertEqual(self.poll(), [])

    def test_trust_read_only_while_a_trust_watch_is_open(self):
        self.s.trust = True
        self.poll()
        self.s.crews["a1"] = st("idle", pane="w:p9")
        add_watch(self.s, "mcp")
        self.poll(names=("a1",))
        self.assertEqual(self.s.calls["site_trust"], [])
        del self.s.crews["a1"]
        add_watch(self.s, "trust", wo="wo-1")
        add_watch(self.s, "trust", wo="wo-2")
        self.poll()
        self.assertEqual(self.s.calls["site_trust"], [SITE])

    def test_consent_done_dialog(self):
        add_watch(self.s, "mcp")
        self.s.crews["a1"] = st("blocked", pane="w:p9")
        self.assertEqual(self.poll(names=("a1",)), ["settled a1 blocked"])
        self.s.crews["a1"] = st("idle", pane="w:p9")
        self.assertEqual(self.poll(names=("a1",)), ["consent_done wo-1 mcp", "settled a1 idle"])

    def test_consent_done_dialog_needs_the_same_agent_and_a_known_status(self):
        add_watch(self.s, "mcp")
        self.assertEqual(self.poll(), [])                       # a1 left the list
        self.s.crews["other"] = st("idle", pane="w:p9")
        self.assertEqual([e for e in self.poll(names=("other",)) if e.startswith("consent")], [])
        del self.s.crews["other"]
        self.s.crews["a1"] = st("unknown", pane="w:p9")
        self.assertEqual([e for e in self.poll(names=("a1",)) if e.startswith("consent")], [])
        self.s.crews["a1"] = st("working", pane="w:p9")
        self.assertEqual(self.poll(names=("a1",)), ["consent_done wo-1 mcp"])

    def test_consent_done_dialog_screen_still_shows_the_prompt(self):
        add_watch(self.s, "mcp")
        self.s.crews["a1"] = st("working", pane="w:p9")
        self.s.screens["w:p9"] = "New MCP server found in .mcp.json\n1. Use this\n2. Not now\n"
        self.assertEqual(self.poll(names=("a1",)), [])

    def test_consent_done_trust_not_before_the_exit_at_a_short_interval(self):
        add_watch(self.s, "trust")
        self.s.crews["x"] = st("idle", pane="w:p9", bg=1)   # the customer's claude
        for i in range(6):
            self.s.trust = i >= 3
            self.assertEqual(self.poll(self.T + 2 * i), [], i)
        del self.s.crews["x"]
        self.assertEqual(self.poll(self.T + 12), ["consent_done wo-1 trust"])
        self.assertEqual(self.poll(self.T + 14), [])

    def _dialog_poll(self, status, screen, t):
        self.s.screens["w:p9"] = screen
        self.s.crews["a1"] = st(status, pane="w:p9", ctx=ctx(10.0),
                                      consent=herdr_cli.consent_dialog(screen))
        before = len(self.s.calls["visible"])
        out = self.poll(self.T + t, names=("a1",))
        return out, len(self.s.calls["visible"]) - before

    def test_consent_done_dialog_not_before_the_prompt_is_left_at_a_short_interval(self):
        add_watch(self.s, "mcp")
        dialog = fixture("consent", "mcp.txt")
        clean = "just a screen\n"
        out = []
        for t in (0, 2, 4):
            out.append(self._dialog_poll("blocked", dialog, t)[0])
        self.assertEqual(out, [["consent a1 mcp"], [], []])
        self.assertEqual(self._dialog_poll("idle", dialog, 6)[0], [])
        for t in (8, 10):
            got, reads = self._dialog_poll("idle", dialog, t)
            self.assertEqual((got, reads), ([], 1), t)
        self.assertEqual(self._dialog_poll("idle", clean, 12)[0],
                         ["consent_done wo-1 mcp", "settled a1 idle"])
        self.assertEqual(self._dialog_poll("idle", clean, 14), ([], 0))

    def test_main_consent_done_after_the_exit_at_a_short_interval(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        add_watch(self.s, "trust")
        self.s.trust = True
        self.s.crews = {"x": st("idle", pane="w:p9", bg=1)}   # the customer's claude
        deps = self.s.deps()
        deps["crews"] = lambda kv=None: []
        start = 1_000_000.0
        clock = {"t": start, "m": 50_000.0}

        def fake_sleep(sec):
            clock["t"] += sec
            clock["m"] += sec
            if clock["t"] >= start + 7:
                self.s.crews.pop("x", None)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"PRORAB_LOG_DIR": tmp.name, "HOME": tmp.name}), \
                mock.patch.object(monitor.time, "sleep", fake_sleep), \
                mock.patch.object(monitor.time, "time", lambda: clock["t"]), \
                mock.patch.object(monitor.time, "monotonic", lambda: clock["m"]), \
                redirect_stdout(buf):
            monitor.main(["--interval", "2", "--heartbeat", "1000000"], deps=deps)
            rows = monitor.readings()
        self.assertEqual(buf.getvalue().strip(), "EVENT consent_done wo-1 trust")
        self.assertEqual(clock["t"], start + 8)
        for r in rows[:-1]:
            self.assertNotIn("consent_done wo-1 trust", r.get("events") or [])

    def test_api_key_watch_never_fires(self):
        add_watch(self.s, "api_key")
        self.s.crews["a1"] = st("idle", pane="w:p9", bg=1)
        self.assertEqual(self.poll(names=("a1",)), [])
        add_watch(self.s, "weird_kind")
        self.assertEqual(self.poll(names=("a1",)), [])

    def test_watchless_record_never_fires(self):
        self.s.crews["a1"] = st("idle", pane="w:p9", bg=1)
        add_watch(self.s, "mcp", watch=False, no_watch="repeat")
        add_watch(self.s, "bypass_warning", watch=False, no_watch="no_pane", pane=None)
        self.assertEqual(self.poll(names=("a1",)), [])

    def test_consent_done_presses_and_writes_nothing(self):
        import herdr_cli

        def boom(*a, **kw):
            raise AssertionError("the monitor must not write or press: %r" % (a,))
        add_watch(self.s, "mcp")
        self.s.crews["a1"] = st("idle", pane="w:p9", bg=1)
        self.set_due()
        with mock.patch.object(journal, "kv_set", boom), mock.patch.object(journal, "kv_clear", boom), \
                mock.patch.object(herdr_cli, "run", boom), mock.patch.object(herdr_cli, "send_keys", boom):
            out = self.poll(self.T, names=("a1",))
        self.assertEqual([e.split()[0] for e in out], ["due", "consent_done"])

    def test_trust_watch_parent_folder_never_counts(self):
        import crew
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home)
        trust_file = os.path.join(home, ".claude.json")
        d = json.loads(json.dumps(self.d))
        d["crew"]["trust_path"] = trust_file
        add_watch(self.s, "trust")
        deps = self.s.deps()
        deps["site_trust"] = lambda path: crew.site_trust(path, retry=False)
        with mock.patch.object(crew, "_defaults") as dd:
            dd.load.return_value = d
            with open(trust_file, "w", encoding="utf-8") as fh:
                json.dump({"projects": {"/home/user/projects": {"hasTrustDialogAccepted": True}}}, fh)
            self.assertEqual(monitor.poll([], self.d, self.mem, self.now, deps=deps), [])
            with open(trust_file, "w", encoding="utf-8") as fh:
                json.dump({"projects": {SITE: {"hasTrustDialogAccepted": True}}}, fh)
            self.assertEqual(monitor.poll([], self.d, self.mem, self.now + 1, deps=deps),
                             ["consent_done wo-1 trust"])

    def test_consent_done_once_per_watch_and_foreman(self):
        add_watch(self.s, "mcp", since=1000.0)
        self.s.crews["a1"] = st("idle", pane="w:p9", bg=1)
        self.assertEqual(self.poll(names=("a1",)), ["consent_done wo-1 mcp"])
        self.assertEqual(self.poll(names=("a1",)), [])
        add_watch(self.s, "mcp", since=2000.0)
        self.assertEqual(self.poll(names=("a1",)), ["consent_done wo-1 mcp"])
        self.s.kv["prorab:active"] = "w:p7"
        self.assertEqual(self.poll(names=("a1",)), ["consent_done wo-1 mcp"])
        self.assertEqual(self.poll(names=("a1",)), [])
        del self.s.kv["consent.wo-1.mcp"]
        self.poll(names=("a1",))
        self.assertEqual(self.mem["consent_done"], {})

    def test_consent_done_waits_for_stop_and_exiting(self):
        add_watch(self.s, "mcp")
        self.s.crews["a1"] = st("idle", pane="w:p9", bg=1)
        stop = self.d["emergency"]["stop_path"]
        with open(stop, "w"):
            pass
        self.assertEqual(self.poll(names=("a1",)), ["emergency_stop a1"])
        self.assertEqual(self.poll(self.now + 90, names=("a1",)), [])
        os.remove(stop)
        out = self.poll(self.now + 180, names=("a1",))
        self.assertEqual(out, ["emergency_released a1", "consent_done wo-1 mcp"])
        add_watch(self.s, "bypass_warning", wo="wo-2")
        t = self.now + 400
        self.s.kv["crew.a1.exiting"] = str(t - 30)
        self.assertEqual(self.poll(t, names=("a1",)), [])
        later = t + self.d["crew"]["exit_window_s"]
        self.assertEqual(self.poll(later, names=("a1",)), ["consent_done wo-2 bypass_warning"])

    def test_unreadable_watch_record_is_a_failing_source(self):
        self.s.kv["consent.wo-1.mcp"] = "garbage"
        self.poll()
        self.assertTrue(any(e.startswith("consent.wo-1.mcp") for e in self.mem["poll_errors"]))


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
        home = mock.patch.dict(os.environ, {"HOME": self.tmp.name})  # the stop file's path
        home.start()
        self.addCleanup(home.stop)
        self.s = Stub()

    def run_main(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = monitor.main(["--crew", "b1", *extra], deps=self.s.deps())
        return rc, buf.getvalue()

    def test_monitor_writes_and_removes_its_mark(self):
        os.environ["HERDR_PANE_ID"] = "w:p/9"
        self.addCleanup(os.environ.pop, "HERDR_PANE_ID", None)
        path = os.path.join(self.tmp.name, "monitor-w_p_9.json")
        seen = []
        deps = self.s.deps()
        orig = deps["agents"]

        def agents():
            with open(path, encoding="utf-8") as fh:  # present while the monitor runs
                seen.append(json.load(fh))
            return orig()
        deps["agents"] = agents
        before = time.time()
        with mock.patch("subprocess.run", side_effect=AssertionError("process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process")):
            with redirect_stdout(io.StringIO()):
                rc = monitor.main(["--crew", "b1", "--interval", "0.01", "--heartbeat", "0.05"],
                                  deps=deps)
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertEqual(seen[0]["pane"], "w:p/9")
        self.assertGreaterEqual(seen[0]["started_at"], before)
        self.assertFalse(os.path.exists(path))

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

    def test_main_sleep_ends_at_the_next_timer(self):
        deps = self.s.deps()
        deps["crews"] = lambda kv=None: []
        self.s.crews = {}
        clock = {"t": 1_000_000.0}
        sleeps = []

        def fake_sleep(sec):
            sleeps.append(sec)
            clock["t"] += sec
        self.s.kv["due.check-tests"] = json.dumps(
            {"at": clock["t"] + 5, "text": "the unit suite", "set_at": clock["t"]})
        buf = io.StringIO()
        with mock.patch.object(monitor.time, "sleep", fake_sleep), \
                mock.patch.object(monitor.time, "time", lambda: clock["t"]), \
                redirect_stdout(buf):
            monitor.main(["--heartbeat", "1000000"], deps=deps)
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 5)
        self.assertEqual(buf.getvalue().strip(), "EVENT due check-tests the unit suite")

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
