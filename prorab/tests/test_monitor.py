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
import monitor  # noqa: E402

D = _defaults.load()


def at(hh, mm=0, day=21):
    return time.mktime((2026, 10, day, hh, mm, 0, 0, 0, 0))


def lim(five=None, week=None, source="sensor", reached=False, resets_at=None):
    return {"five_hour": five, "seven_day": week, "source": source, "reached": reached,
            "five_hour_resets_at": resets_at, "five_hour_resets_in_s": None,
            "seven_day_resets_at": None}


def st(status="working", bg=0, ctx=None, size=100):
    return {"status": status, "bg_shells": bg, "ctx": ctx, "session_id": "s", "cwd": "/w",
            "pane_id": "p", "_size": size}


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

    def deps(self):
        return {
            "state": lambda n: self.crews.get(n) or st("gone"),
            "transcript_size": lambda s: s.get("_size"),
            "statusline_ctx": lambda n: self.statusline.get(n),
            "limits": lambda d, now: self.limits,
            "kv_get": lambda k: self.kv.get(k),
            "over_budget": lambda wk: list(self.over),
            "override": lambda: self.override,
            "own_context": lambda d: self.own,
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

    def test_limit_event_thresholds(self):
        self.s.limits = lim(59.9, 10.0)
        self.assertEqual(self.poll(), [])
        self.s.limits = lim(60.0, 10.0)
        self.assertEqual(self.poll(), ["limit 60 10 sensor day"])
        self.assertEqual(self.poll(), [])  # same band: no repeat
        self.s.limits = lim(80.0, 10.0)
        self.assertEqual(self.poll(), ["limit 80 10 sensor day"])  # band changed
        self.s.limits = lim(20.0, 50.0)
        self.assertEqual(self.poll(), ["limit 20 50 sensor day"])  # week notify
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
        self.assertEqual(self.poll(), ["limit 90 60 sensor day", "budget sessions"])

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
        self.s.statusline["b1"] = 65.0
        self.assertEqual(self.poll(), ["context_soft b1 65.0 (statusline)"])
        self.s.statusline["b1"] = 85.0
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


class Boom(RuntimeError):
    pass


class TestMonitorFaults(PollBase):
    def test_mid_poll_exception_keeps_events(self):
        del self.s.crews["b1"]
        self.s.limits = lim(90.0, 10.0)
        self.s.over = ["runs"]
        calls = {"n": 0}
        deps = self.s.deps()

        def flaky(d):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Boom("transient")
            return None
        deps["own_context"] = flaky
        # an unguarded failure (the crew list is not a guarded source) aborts the poll
        bad = dict(deps, state=lambda n: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            monitor.poll(["b1"], D, self.mem, self.now, deps=bad)
        self.assertEqual(self.mem, {})  # nothing committed
        out = monitor.poll(["b1"], D, self.mem, self.now, deps=deps)
        self.assertEqual([e.split()[0] for e in out], ["gone", "limit", "budget"])

    def test_failing_source_does_not_block_others(self):
        del self.s.crews["b1"]
        self.s.limits = lim(90.0, 10.0)
        deps = self.s.deps()

        def broken(d):
            raise Boom("pane read")
        deps["own_context"] = broken
        out = monitor.poll(["b1"], D, self.mem, self.now, deps=deps)
        self.assertEqual(out, ["gone b1", "limit 90 10 sensor day"])
        self.assertIn("own_context", self.mem["poll_errors"][0])

    def test_error_event_after_n_polls_once_per_streak(self):
        deps = self.s.deps()

        def broken(d):
            raise Boom("x")
        deps["own_context"] = broken
        run = lambda: monitor.poll(["b1"], D, self.mem, self.now, deps=deps)  # noqa: E731
        self.assertEqual([run(), run()], [[], []])
        self.assertEqual(run(), ["error own_context"])
        self.assertEqual(run(), [])
        deps["own_context"] = lambda d: None
        self.assertEqual(run(), [])
        deps["own_context"] = broken
        run(), run()
        self.assertEqual(run(), ["error own_context"])

    def test_unknown_streak_not_inflated_by_failed_poll(self):
        self.s.limits = lim(source="unknown")
        deps = self.s.deps()
        with self.assertRaises(KeyboardInterrupt):
            monitor.poll(["b1"], D, self.mem, self.now,
                         deps=dict(deps, state=lambda n: (_ for _ in ()).throw(KeyboardInterrupt())))
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.poll(), ["blind"])

    def test_no_hung_without_transcript(self):
        self.s.crews["b1"] = st(size=None)
        self.assertEqual(self.poll(self.now), [])
        self.assertEqual(self.poll(self.now + 5000), [])


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
        deps["crews"] = lambda: next(lists)
        self.s.crews["b1"] = st("done")
        orig = deps["state"]
        deps["state"] = lambda n: (seen.append(n), orig(n))[1]
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.main(["--interval", "0.01", "--heartbeat", "5"], deps=deps)
        self.assertEqual(buf.getvalue().strip(), "EVENT settled b1 done")

    def test_errors_logged_and_heartbeat_carries_last_error(self):
        deps = self.s.deps()

        def broken(d):
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
