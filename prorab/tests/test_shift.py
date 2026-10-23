import datetime
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
import uuid
from contextlib import redirect_stdout
from unittest import mock

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

if os.environ.get("PRORAB_FAST") == "1":
    raise unittest.SkipTest("shift tests run the real `bd`: skipped in the fast tier (PRORAB_FAST=1)")
if shutil.which("bd") is None:
    raise unittest.SkipTest("shift tests need the `bd` (beads >= 1.3) binary on PATH")

import _defaults  # noqa: E402
import journal  # noqa: E402
import monitor  # noqa: E402
import shift  # noqa: E402
from prorab.tests.test_herdr_cli import err, ok  # noqa: E402
from prorab.tests.test_journal import JournalBase, new_work_order  # noqa: E402

T0 = 1790000000.0
OPUS = "claude-opus-5-5"
SITE = "/home/user/projects/demo-app"
ENV_KEYS = ("PRORAB_CACHE", "PRORAB_LOG_DIR", "PMSET_OUT", "CAFFEINATE_LOG", "HERDR_PANE_ID")


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def agent_entry(name, pane, session="sess-1"):
    return {"name": name, "pane_id": pane, "cwd": "/work", "agent_status": "idle",
            "agent_session": {"value": session}}


class ShiftBase(JournalBase):
    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in ENV_KEYS}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.cache = os.path.join(self.tmp.name, "cache")
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.cache)
        os.makedirs(self.logs)
        os.environ["PRORAB_CACHE"] = self.cache
        os.environ["PRORAB_LOG_DIR"] = self.logs
        os.environ.pop("HERDR_PANE_ID", None)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        os.environ["PATH"] = self.bin + os.pathsep + os.environ["PATH"]
        self.fake_script("pmset", 'echo "$PMSET_OUT"')
        os.environ["PMSET_OUT"] = "Now drawing from 'AC Power'"
        self.routes = {}
        self._files = 0
        self.route("agent list", ok({"agents": []}))
        self.d = _defaults.load()

    def fake_script(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n%s\n" % body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def route(self, prefix, body, exit_code=0):
        self._files += 1
        name = "r%d.txt" % self._files
        with open(os.path.join(self.herdr_dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)
        self.routes[prefix] = {"file": name, "exit": exit_code}
        with open(os.path.join(self.herdr_dir, "routes.json"), "w", encoding="utf-8") as fh:
            json.dump(self.routes, fh)

    def calls(self):
        try:
            with open(os.path.join(self.herdr_dir, "calls.log"), encoding="utf-8") as fh:
                return fh.read().splitlines()
        except FileNotFoundError:
            return []

    def write_transcript(self, cwd, session, msgs):
        """Synthetic transcript; msgs = [(message id, ts, usd)], priced as opus input tokens."""
        import transcript
        path = os.path.join(self.home, ".claude", "projects", transcript.project_slug(cwd),
                            session + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for mid, ts, usd in msgs:
                fh.write(json.dumps({"type": "assistant", "cwd": cwd, "timestamp": _iso(ts),
                                     "message": {"id": mid, "model": OPUS, "usage": {
                                         "input_tokens": int(usd / 4.0 * 1000000),
                                         "output_tokens": 0}}}) + "\n")
        return path

    def start_shift(self, at=T0, foreman=None):
        journal.begin_shift(None, foreman)
        journal.kv_set("shift.started_at", at)

    def write_log(self, rows):
        with open(os.path.join(self.logs, "monitor.log"), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def stage(self, work_order, title="S1", **kw):
        return journal.create_stage(work_order, title, "b", **kw)

    def accepted_work_order(self, title="Done", goal=None):
        n = new_work_order(title, goal=goal)
        s = self.stage(n)
        journal.start_stage(s)
        journal.accept_stage(s, "Acceptance of %s" % title)
        journal.bd(["close", n])
        return n, s

    def report_text(self, date="2026-10-22"):
        out = os.path.join(self.tmp.name, "reports")
        path = shift.report(date, out)
        self.assertEqual(path, os.path.join(out, date + ".md"))
        with open(path, encoding="utf-8") as fh:
            return fh.read()


class TestPreflight(ShiftBase):
    def test_preflight_battery(self):
        os.environ["PMSET_OUT"] = "Now drawing from 'Battery Power'"
        problems = shift.preflight(self.d)
        self.assertTrue(any("mains power" in p and not p.startswith("note") for p in problems),
                        problems)
        os.environ["PMSET_OUT"] = "Now drawing from 'AC Power'"
        self.assertFalse(any("mains power" in p for p in shift.preflight(self.d)))

    def test_preflight_missing_estimate(self):
        n = journal.bd(["create", "Hand made", "-d", "b", "-l",
                        "%s,site:o" % journal.WORK_ORDER_LABEL, "--silent"],
                       json_out=False).strip()
        self.assertIsNone(journal.estimate(n)["usd"])
        self.assertIn(n, journal.ready_work_orders())
        problems = shift.preflight(self.d)
        self.assertTrue(any(n in p and "estimate" in p for p in problems), problems)
        ok_one = new_work_order("With estimate")
        self.assertFalse(any(ok_one in p and "estimate" in p for p in problems))

    def test_preflight_clearance(self):
        n = new_work_order()
        s = self.stage(n)
        journal.add_label(s, "needs:clearance")
        problems = shift.preflight(self.d)
        self.assertTrue(any(s in p and "clearance" in p for p in problems), problems)
        journal.add_label(s, "clearance:granted")
        self.assertFalse(any(s in p and "clearance" in p for p in shift.preflight(self.d)))

    def test_preflight_names_budget_and_rate(self):
        notes = "\n".join(shift.preflight(self.d))
        self.assertIn("3.50 $/pp (default)", notes)
        self.assertIn("week rate: 10.00 $/pp (default)", notes)
        self.assertIn("$100, +10 pp of the week", notes)

    def test_preflight_budget_follows_the_journal(self):
        journal.set_budget(usd=120, week_pp=4)
        self.assertIn("$120, +4 pp of the week", "\n".join(shift.preflight(self.d)))

    def test_preflight_sensor_and_week_notes(self):
        notes = [p for p in shift.preflight(self.d) if p.startswith("note")]
        text = "\n".join(notes)
        self.assertIn("sensor", text)
        self.assertIn("week", text)
        self.assertIn("no new mass runs", text)

    def test_preflight_night_reports_morning_and_caps(self):
        day = "\n".join(shift.preflight(self.d, night=False))
        self.assertNotIn("morning", day)
        night = "\n".join(shift.preflight(self.d, night=True))
        self.assertIn("08:00", night)
        self.assertIn("stop at", night)
        cap = shift.scheduler.cap_for(time.time(), None, "08:00", self.d, night=True,
                                      week_known=False)
        self.assertIn("stop at %g %%" % cap["stop"], night)


class TestBegin(ShiftBase):
    def test_begin_records_foreman_session_and_slots(self):
        res = shift.begin(self.d, night=True, foreman_session="fs-1")
        self.assertEqual(journal.kv_get("shift.foreman_sessions"), "fs-1")
        self.assertEqual(journal.kv_get("shift.night"), "1")
        with open(os.path.join(self.cache, "slots", "heavy.count"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "2")
        self.assertEqual(res["machine"]["heavy_slots"], 2)
        self.assertEqual(res["budget"], {"usd": 100.0, "week_pp": 10.0})
        self.assertIn("five_hour", res["rates"])
        self.assertIn("stop", res["cap"])
        self.assertTrue(res["problems"])
        shift.begin(self.d, night=False, foreman_session="fs-1")
        self.assertEqual(journal.kv_get("shift.night"), "0")

    def test_begin_defaults_to_the_session_of_the_foremans_pane(self):
        os.environ["HERDR_PANE_ID"] = "w:p1"
        self.route("agent list", ok({"agents": [agent_entry("prorab", "w:p1", "fs-own")]}))
        shift.begin(self.d)
        self.assertEqual(journal.kv_get("shift.foreman_sessions"), "fs-own")

    def test_begin_without_a_pane_records_no_session(self):
        shift.begin(self.d)
        self.assertIsNone(journal.kv_get("shift.foreman_sessions"))

    def test_start_caffeinate_ties_to_the_pid(self):
        log = os.path.join(self.tmp.name, "caffeinate.log")
        os.environ["CAFFEINATE_LOG"] = log
        self.fake_script("caffeinate", 'echo "$@" > "$CAFFEINATE_LOG"')
        pid = shift.start_caffeinate(4242)
        self.assertIsInstance(pid, int)
        deadline = time.time() + 5
        while not os.path.exists(log) and time.time() < deadline:
            time.sleep(0.05)
        with open(log, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "-is -w 4242")


    def test_start_caffeinate_argv(self):
        with mock.patch.object(shift.os, "posix_spawnp", return_value=7) as spawn:
            self.assertEqual(shift.start_caffeinate(timeout_s=1200), 7)
            self.assertEqual(spawn.call_args[0][1], ["caffeinate", "-is", "-t", "1200"])
            shift.start_caffeinate(pid=42)
            self.assertEqual(spawn.call_args[0][1], ["caffeinate", "-is", "-w", "42"])
            shift.start_caffeinate(42, 60.5)
            self.assertEqual(spawn.call_args[0][1],
                             ["caffeinate", "-is", "-w", "42", "-t", "60"])
            with self.assertRaises(ValueError):   # never an unbounded hold
                shift.start_caffeinate()
            self.assertEqual(spawn.call_count, 3)


class TestReport(ShiftBase):
    def test_report_never_copies_approval_words(self):
        self.start_shift()
        n, s = self.accepted_work_order("Importer")
        journal.add_act(n, "answer", '[customer decision] approve: raise the estimate to $25; '
                                     'words: "WORDS-MARKER"')
        text = self.report_text()
        self.assertNotIn("WORDS-MARKER", text)

    def test_report_sections(self):
        self.start_shift()
        self.accepted_work_order("Importer")
        waiting = new_work_order("Exporter")
        s = self.stage(waiting)
        journal.wait_customer(s, "Which storage should the exporter use?")
        text = self.report_text()
        for heading in ("Accepted", "Waiting for the customer", "Budget", "Limits", "Incidents"):
            self.assertIn("## %s" % heading, text)
        self.assertIn("> Which storage should the exporter use?", text)
        self.assertIn("Importer", text)
        self.assertIn("Exporter", text)
        self.assertNotIn("## Without limits", text)

    def stop_file(self):
        path = self.d["emergency"]["stop_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8"):
            pass
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_report_counts_every_crew_session(self):
        n = journal.create_work_order("o", "Importer", "b", {"usd": 10})
        wt = "/home/user/projects/demo-app-a"
        journal.issue_set(n, "issued", worktree=wt, sessions=["s1", "s2", "s3"])
        self.start_shift()
        for i, sid in enumerate(("s1", "s2", "s3"), 1):
            self.write_transcript(wt, sid, [("m%d" % i, T0 + 60 * i, float(i))])
        journal.spend(n, sessions=1, runs=2, tokens=43011)
        old = new_work_order("Old")
        journal.issue_set(old, "issued", worktree="/home/user/projects/demo-app-old")
        journal.kv_set("shift.closed_before", old)
        journal.kv_set("spent.%s.sessions" % old, 5)
        text = self.report_text()
        self.assertIn("- %s: tokens 1500000, sessions 3, agent runs 2" % n, text)
        self.assertNotIn("- %s: tokens" % old, text)

    def test_report_counts_fall_back_to_kv_without_a_session_list(self):
        n = journal.create_work_order("o", "Importer", "b", {"usd": 10})
        journal.issue_set(n, "issued", worktree="/home/user/projects/demo-app-a")
        self.start_shift()
        journal.spend(n, sessions=2, runs=3)
        self.assertIn("- %s: tokens 0, sessions 2, agent runs 3" % n, self.report_text())

    def test_report_names_the_emergency_stop(self):
        self.start_shift()
        self.write_log([{"ts": T0 + 600, "events": ["emergency_stop a1"]},
                        {"ts": T0 + 660, "events": ["emergency_released a1"]}])
        text = self.report_text()
        section = text.split("## Emergency stop")[1].split("\n## ")[0]
        self.assertGreater(text.index("## Emergency stop"), text.index("## Incidents"))
        self.assertIn("- on at %s (seen by the monitor; crews paused: a1); released at %s"
                      % (shift._clock(T0 + 600), shift._clock(T0 + 660)), section)

    def test_report_emergency_stop_still_on_or_unseen(self):
        self.start_shift()
        self.write_log([{"ts": T0 + 600, "events": ["emergency_stop -"]}])
        first = self.stop_file()
        want = "- on at %s (seen by the monitor; crews paused: none); " % shift._clock(T0 + 600)
        self.assertIn(want + "still on when this report was written", self.report_text())
        os.remove(first)
        self.assertIn(want + "its release was not seen by the monitor",
                      self.report_text("2026-10-23"))

    def test_report_stop_file_without_an_event(self):
        self.start_shift()
        self.stop_file()
        self.assertIn("- on now (the stop file exists; the monitor did not see it in this "
                      "shift)", self.report_text())

    def test_report_without_an_emergency_stop_has_no_section(self):
        self.start_shift()
        self.write_log([{"ts": T0 + 60, "events": []}])
        self.assertNotIn("## Emergency stop", self.report_text())

    def sleep_section(self, date="2026-10-22"):
        text = self.report_text(date)
        if "## Machine sleep" not in text:
            return None
        return text.split("## Machine sleep")[1].split("\n## ")[0]

    def test_report_names_a_machine_sleep(self):
        self.start_shift()
        self.write_log([{"ts": T0, "mono": 1000, "events": []},
                        {"ts": T0 + 90, "mono": 1090, "events": []},
                        {"ts": T0 + 39780, "mono": 1180, "events": []},
                        {"ts": T0 + 39870, "mono": 1270, "events": []}])
        text = self.report_text()
        section = text.split("## Machine sleep")[1].split("\n## ")[0]
        self.assertGreater(text.index("## Machine sleep"), text.index("## Incidents"))
        self.assertEqual([ln for ln in section.splitlines() if ln.startswith("- ")], [
            "- slept about 11 h 00 min between the monitor's polls at %s and %s; "
            "the monitor held no awake hold" % (shift._clock(T0 + 90), shift._clock(T0 + 39780))])

    def test_report_sleep_after_the_emergency_stop_section(self):
        self.start_shift()
        self.write_log([{"ts": T0 + 600, "mono": 10, "events": ["emergency_stop a1"]},
                        {"ts": T0 + 660, "mono": 70, "events": ["emergency_released a1"]},
                        {"ts": T0 + 7260, "mono": 130, "events": []}])
        text = self.report_text()
        self.assertGreater(text.index("## Machine sleep"), text.index("## Emergency stop"))
        self.assertIn("slept about 1 h 49 min", text)

    def test_report_sleep_with_the_awake_hold_on(self):
        self.start_shift()
        self.write_log([{"ts": T0, "mono": 1000, "events": []},
                        {"ts": T0 + 90, "mono": 1090, "awake_until": T0 + 1200, "events": []},
                        {"ts": T0 + 39780, "mono": 1180, "events": []}])
        self.assertIn("; the monitor's awake hold was on, so something put the machine to "
                      "sleep (the lid, the power button or a sleep command)",
                      self.sleep_section())

    def test_report_short_sleep_is_named_in_minutes(self):
        self.start_shift()
        self.write_log([{"ts": T0, "mono": 1000, "events": []},
                        {"ts": T0 + 1830, "mono": 1090, "events": []}])
        self.assertIn("- slept about 29 min between", self.sleep_section())

    def test_report_long_gap_without_sleep_is_no_sleep(self):
        self.start_shift()
        self.write_log([{"ts": T0, "mono": 1000, "events": []},
                        {"ts": T0 + 10800, "mono": 1000 + 10800, "events": []}])
        self.assertIsNone(self.sleep_section())

    def test_report_sleep_needs_the_uptime_clock(self):
        self.start_shift()
        self.write_log([{"ts": T0, "events": []}, {"ts": T0 + 39780, "events": []}])
        self.assertIsNone(self.sleep_section())
        self.write_log([{"ts": T0, "mono": 5000, "events": []},
                        {"ts": T0 + 39780, "mono": 100, "events": []}])   # a reboot
        self.assertIsNone(self.sleep_section("2026-10-23"))

    def test_report_merges_a_sleep_broken_by_short_wakes(self):
        self.start_shift()
        self.write_log([{"ts": T0, "mono": 0, "events": []},
                        {"ts": T0 + 3690, "mono": 90, "events": []},
                        {"ts": T0 + 3990, "mono": 390, "events": []},
                        {"ts": T0 + 7680, "mono": 480, "events": []}])
        lines = [ln for ln in self.sleep_section().splitlines() if ln.startswith("- ")]
        self.assertEqual(lines, ["- slept about 2 h 00 min between the monitor's polls at %s "
                                 "and %s; the monitor held no awake hold"
                                 % (shift._clock(T0), shift._clock(T0 + 7680))])

    def test_report_without_sleep_has_no_section(self):
        self.start_shift()
        self.write_log([{"ts": T0 + 60, "mono": 60, "events": []},
                        {"ts": T0 + 150, "mono": 150, "events": []}])
        self.assertNotIn("## Machine sleep", self.report_text())

    def test_second_report_of_a_date_never_overwrites_the_first(self):
        self.start_shift()
        out = os.path.join(self.tmp.name, "reports")
        first = shift.report("2026-10-22", out)
        with open(first, "rb") as fh:
            body = fh.read()
        second = shift.report("2026-10-22", out)
        third = shift.report("2026-10-22", out)
        self.assertEqual([os.path.basename(p) for p in (first, second, third)],
                         ["2026-10-22.md", "2026-10-22-2.md", "2026-10-22-3.md"])
        with open(first, "rb") as fh:
            self.assertEqual(fh.read(), body)

    def test_report_skips_decided_branches(self):
        self.start_shift()
        a, _ = self.accepted_work_order("Kept")
        b, _ = self.accepted_work_order("Decided")
        journal.issue_set(a, "issued", worktree="/home/user/projects/demo-app-a")
        journal.issue_set(b, "issued", worktree="/home/user/projects/demo-app-b",
                          decided={"choice": "keep"})
        text = self.report_text()
        branches = text.split("## Finished branches")[1].split("\n## ")[0]
        self.assertIn("prorab/%s" % a, branches)
        self.assertNotIn("prorab/%s" % b, text)

    def test_report_branch_options(self):
        self.start_shift()
        n, _ = self.accepted_work_order("Importer")
        journal.issue_set(n, "issued", worktree="/home/user/projects/demo-app-wt", name="c1")
        text = self.report_text()
        self.assertIn("prorab/%s" % n, text)
        for option in ("merge", "pull request", "keep"):
            self.assertIn(option, text.lower())

    def test_report_budget_in_dollars_and_pp(self):
        n = journal.create_work_order("o", "Importer", "b", {"usd": 10})
        wt = "/home/user/projects/demo-app-a"
        journal.issue_set(n, "issued", worktree=wt)
        self.start_shift()
        self.write_transcript(wt, "s1", [("m1", T0 + 60, 5.0), ("m2", T0 + 120, 7.0)])
        journal.spend(n, sessions=2, runs=3, tokens=1500)
        text = self.report_text()
        self.assertIn("$12.00", text)
        self.assertIn("$10.00", text)
        self.assertIn("over", text.lower())
        self.assertIn("3.43 pp of the window", text)
        self.assertIn("$100.00", text)  # the shift budget
        self.assertIn("Machine spend over the shift: $12.00", text)
        self.assertIn("tokens 3000000, sessions 2, agent runs 3", text)
        self.assertIn("3.50 $/pp (default)", text)
        self.assertIn("week rate: 10.00 $/pp (default)", text)
        self.assertIn("1.20 pp of the week", text)

    def test_report_budget_lists_research_and_foreman(self):
        self.start_shift(foreman="fs1")
        own = "/home/user/projects/demo-app"
        self.write_transcript(own, "fs1", [("f1", T0 + 10, 1.5)])
        journal.research_begin("api-survey", 4.0, "fs1")
        text = self.report_text()
        self.assertIn("api-survey", text)
        self.assertIn("$4.00", text)
        self.assertIn("Foreman: $1.50", text)

    def test_report_week_rise_against_budget(self):
        self.start_shift()
        journal.kv_set("shift.week_start", 20)
        sensor_dir = self.d["limits"]["sensor_dir"]
        os.makedirs(sensor_dir)
        with open(os.path.join(sensor_dir, "s.json"), "w", encoding="utf-8") as fh:
            json.dump({"rate_limits": {"five_hour": {"used_percentage": 30,
                                                     "resets_at": int(time.time()) + 3600},
                                       "seven_day": {"used_percentage": 26,
                                                     "resets_at": int(time.time()) + 86400}}}, fh)
        text = self.report_text()
        self.assertIn("+6 pp of the week against +10 pp", text)
        self.assertIn("5h window: 30", text)
        self.assertIn("week: 26", text)


    def _no_working_days(self):
        real = _defaults.load

        def load(*a, **k):
            d = real(*a, **k)
            d["week"]["calendar"]["days"] = []
            return d
        _defaults.load = load
        self.addCleanup(setattr, _defaults, "load", real)

    def _week_sensor(self, reset, used=50):
        sensor_dir = self.d["limits"]["sensor_dir"]
        os.makedirs(sensor_dir, exist_ok=True)
        with open(os.path.join(sensor_dir, "s.json"), "w", encoding="utf-8") as fh:
            json.dump({"rate_limits": {"five_hour": {"used_percentage": 30,
                                                     "resets_at": int(time.time()) + 3600},
                                       "seven_day": {"used_percentage": used,
                                                     "resets_at": reset}}}, fh)

    def test_report_week_projection_lines(self):
        self._no_working_days()  # W = 0 whenever the test runs
        self.start_shift()
        reset = int(time.time()) + 48 * 3600
        self._week_sensor(reset)
        measured = {"pp_per_h": 0.30, "source": "measured", "reason": None, "intervals": 3,
                    "pp": 3.0, "hours": 10.0, "resets_at": reset, "at": time.time(),
                    "per": "working_hour"}
        with mock.patch.object(shift.limits, "week_burn", return_value=measured):
            text = self.report_text()
        self.assertIn("- week: 50 % used, resets ", text)
        self.assertIn("(in 48.0 h)", text)
        self.assertIn("0.30 pp per working hour (measured over 10.0 working hours, 3 intervals)",
                      text)
        self.assertIn("working hours left: 0.0 (calendar no working days)", text)
        self.assertIn("off-hours crew work: 0 pp (no night shift)", text)
        self.assertIn("- week projection at the reset: 50 + 0.30 \u00d7 0.0 + 0 = 50.0 %", text)
        self.assertIn("the week allows: room for more", text)
        self.assertNotIn("reserve", text)

    def test_report_week_fallback_named(self):
        self._no_working_days()
        self.start_shift()
        self._week_sensor(int(time.time()) + 48 * 3600, used=40)
        text = self.report_text()
        self.assertIn("week burn: none \u2014 fallback thresholds (no measured burn)", text)
        self.assertIn("week projection: none \u2014 fallback thresholds 50 % notify, "
                      "70 % clearance only", text)
        self.assertIn("the week allows: normal", text)
        self._week_sensor(int(time.time()) + 48 * 3600, used=50)
        self.assertIn("the week allows: notify the customer (fallback threshold)",
                      self.report_text("2026-10-23"))

    def test_report_week_unknown(self):
        self.start_shift()
        text = self.report_text()
        self.assertIn("week: unknown (no projection", text)
        self.assertNotIn("week burn", text)

    def test_report_override_section(self):
        self.start_shift()
        since = T0 + 100
        journal.kv_set(journal.OVERRIDE_HISTORY_KEY, json.dumps(
            [{"goal": "G1", "since": since, "until": None, "source": "customer's words"}]))
        self.write_log([
            {"ts": T0 + 50, "five_hour": 10, "seven_day": 20, "source": "sensor"},
            {"ts": T0 + 150, "five_hour": 20, "seven_day": 22, "source": "sensor"},
            {"ts": T0 + 250, "five_hour": 35, "seven_day": 24, "source": "sensor"},
            {"ts": T0 + 350, "five_hour": 5, "seven_day": 24, "source": "sensor"},
            {"ts": T0 + 450, "five_hour": 12, "seven_day": 26, "source": "sensor"}])
        self.write_transcript(SITE, "s9", [("m1", T0 + 200, 8.0), ("m0", T0 + 10, 3.0)])
        text = self.report_text()
        self.assertIn("## Without limits", text)
        self.assertIn("G1", text)
        self.assertIn("5h window: +22 pp", text)  # 10 + 15 + 7 (the drop is skipped)
        self.assertIn("week: +4 pp", text)  # 26 - 22
        self.assertIn("$8.00", text)

    def test_report_override_survives_expiry(self):
        self.start_shift()
        n = new_work_order("Importer", goal="G1")
        s = self.stage(n)
        journal.start_stage(s)
        journal.accept_stage(s, "Acceptance")
        journal.bd(["close", n])
        journal.set_override("G1")
        self.assertIsNone(journal.override())  # the goal is done: the record is gone
        self.assertIsNone(journal.kv_get(journal.OVERRIDE_KEY))
        hist = journal.override_history()
        self.assertEqual(len(hist), 1)
        self.assertIsNotNone(hist[0]["until"])
        hist[0].update(since=T0 + 100, until=T0 + 400)
        journal.kv_set(journal.OVERRIDE_HISTORY_KEY, json.dumps(hist))
        self.write_log([
            {"ts": T0 + 150, "five_hour": 20, "seven_day": 22, "source": "sensor"},
            {"ts": T0 + 250, "five_hour": 35, "seven_day": 24, "source": "sensor"},
            {"ts": T0 + 900, "five_hour": 90, "seven_day": 50, "source": "sensor"}])
        text = self.report_text()
        free = text.split("## Without limits")[1].split("\n## ")[0]
        self.assertIn("G1", free)
        self.assertIn(" since ", free)
        self.assertIn(" until ", free)
        self.assertIn("5h window: +15 pp", free)  # the reading after the expiry does not count
        self.assertIn("week: +2 pp", free)

    def test_report_override_before_the_shift_is_not_shown(self):
        self.start_shift()
        journal.kv_set(journal.OVERRIDE_HISTORY_KEY, json.dumps(
            [{"goal": "G0", "since": T0 - 5000, "until": T0 - 1000, "source": "customer's words"}]))
        self.assertNotIn("## Without limits", self.report_text())

    def test_report_incidents_unknown_without_the_monitor_log(self):
        self.start_shift()
        text = self.report_text()
        self.assertIn("Incidents unknown: monitor log missing", text)
        self.assertNotIn("No incidents.", text)

    def test_report_incidents_unknown_with_an_unreadable_log(self):
        self.start_shift()
        os.makedirs(os.path.join(self.logs, "monitor.log"))  # a directory cannot be read
        text = self.report_text()
        self.assertIn("Incidents unknown: monitor log unreadable", text)

    def test_report_no_incidents_with_a_readable_log(self):
        self.start_shift()
        self.write_log([{"ts": T0 + 60, "events": []}])
        text = self.report_text()
        self.assertIn("No incidents.", text)

    def test_report_incidents(self):
        self.start_shift()
        n = new_work_order("Importer")
        s = self.stage(n)
        journal.add_act(n, "answer", "%s the reviewer crew died twice" % shift.INCIDENT_TAG)
        journal.add_act(n, "answer", "crew waits for the limit reset until 03:00")
        journal.add_act(n, "answer", "plain answer to a crew")
        journal.add_act(s, "answer", "plain answer on the stage")  # where crew.answer writes
        journal.add_act(s, "answer", "%s the crew asked twice on the stage" % shift.INCIDENT_TAG)
        journal.wait_customer(s, "handover loop")
        self.write_log([{"ts": T0 + 60, "events": ["hung_unclear c1 scan_capped",
                                                    "provider_error c2 model_not_found"]}])
        text = self.report_text()
        incidents = text.split("## Incidents")[1].split("\n## ")[0]
        self.assertIn("the reviewer crew died twice", incidents)
        self.assertIn("limit reset until 03:00", incidents)
        self.assertIn("handover loop", incidents)
        self.assertIn("hung_unclear c1 scan_capped", incidents)
        self.assertIn("provider_error c2 model_not_found", incidents)
        self.assertIn("- %s: the crew asked twice on the stage" % s, incidents)
        self.assertNotIn("plain answer", incidents)

    def test_report_names_consent_kind_and_push(self):
        self.start_shift()
        n = new_work_order("Importer")
        gate = journal.wait_customer(n, "consent prompt: api_key")
        other = new_work_order("Exporter")
        quiet = journal.wait_customer(other, "consent prompt: trust")
        self.assertTrue(journal.mark_notified(gate))
        text = self.report_text()
        waiting = text.split("## Waiting for the customer")[1].split("\n## ")[0]
        self.assertIn("custom API key", waiting)
        self.assertIn("folder trust", waiting)
        sent = [ln for ln in waiting.splitlines() if "gate %s" % gate in ln][0]
        silent = [ln for ln in waiting.splitlines() if "gate %s" % quiet in ln][0]
        self.assertIn("push sent", sent)
        self.assertNotIn("push sent", silent)
        self.assertIn("no push", silent)

    def test_report_writes_spend_and_clears_the_night_flag(self):
        n = journal.create_work_order("o", "Importer", "b", {"usd": 10})
        wt = "/home/user/projects/demo-app-a"
        journal.issue_set(n, "issued", worktree=wt)
        self.start_shift()
        journal.kv_set("shift.night", 1)
        self.write_transcript(wt, "s1", [("m1", T0 + 60, 3.0)])
        self.report_text()
        self.assertIsNone(journal.kv_get("shift.night"))
        self.assertAlmostEqual(journal.shift_spent()["usd"], 3.0)

    def test_report_runs_no_external_command(self):
        self.start_shift()
        journal.kv_set("polish.cmd", "echo polished")
        real = subprocess.run
        seen = []

        def guard(args, *a, **kw):
            seen.append(args[0])
            if args[0] not in ("bd", "git", "herdr"):
                raise AssertionError("external command: %r" % (args,))
            return real(args, *a, **kw)
        with mock.patch.object(subprocess, "run", guard):
            text = self.report_text()
        self.assertNotIn("polished", text)
        self.assertTrue(seen)


class TestHandoff(ShiftBase):
    def routes_for_handoff(self):
        self.route("pane layout --pane w:p1", ok({"layout": {"panes": [
            {"pane_id": "w:p1", "rect": {"width": 200, "height": 40}}]}}))
        self.route("pane split w:p1", ok({"pane": {"pane_id": "w:p9"}}))
        self.route("agent start prorab-2", ok({"agent": agent_entry("prorab-2", "w:p9")}))
        self.route("agent prompt prorab-2", ok({"agent": {"agent_status": "working"}}))

    def confirm(self, pane="w:p9", reconcile=True, monitor_file=True, at=None, pid=None):
        """The new foreman's records: kv shift.reconciled and its monitor file."""
        at = time.time() + 100 if at is None else at
        if reconcile:
            journal.kv_set("shift.reconciled", json.dumps({"pane": pane, "ts": at}))
        if monitor_file:
            with open(monitor.mark_path(pane), "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid() if pid is None else pid, "pane": pane,
                           "started_at": at}, fh)

    def test_handoff_prorab_waits_for_lock(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p1")
        sleeps = []

        def sleeper(s):
            sleeps.append(s)
            if len(sleeps) == 2:
                journal.kv_set("prorab:active", "w:p9")
                self.confirm()
        with mock.patch.object(shift.time, "sleep", sleeper):
            res = shift.handoff_prorab("w:p1", "state of the shift", timeout_s=600, poll_s=1)
        self.assertEqual(res["status"], "handed_over")
        self.assertEqual(res["confirmed"], ["lock", "reconcile", "monitor"])
        self.assertEqual(res["new_pane"], "w:p9")
        self.assertEqual(len(sleeps), 2)
        self.assertIn("pane split w:p1 --direction right --no-focus --cwd %s"
                      % os.path.expanduser("~/projects/prorab"), self.calls())
        self.assertEqual(journal.handoff()["to"], "w:p9")  # the new foreman clears it
        self.assertEqual(journal.bd(["show", res["bead"]])[0]["status"], "closed")

    def test_handoff_waits_for_lock_reconcile_and_monitor(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p1")
        sleeps = []

        def sleeper(s):
            sleeps.append(s)
            if len(sleeps) == 1:
                journal.kv_set("prorab:active", "w:p9")  # the lock alone: still waiting
            elif len(sleeps) == 2:
                self.confirm(monitor_file=False)         # plus reconcile: still waiting
            elif len(sleeps) == 3:
                self.confirm()                           # all three
        with mock.patch.object(shift.time, "sleep", sleeper):
            res = shift.handoff_prorab("w:p1", "state", timeout_s=600, poll_s=1)
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(res["status"], "handed_over")
        self.assertEqual(res["confirmed"], ["lock", "reconcile", "monitor"])
        self.assertNotIn("unconfirmed", res)

    def test_handoff_lock_without_monitor_is_unconfirmed(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p9")
        self.confirm(monitor_file=False)
        res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["status"], "handed_over")
        self.assertEqual(res["unconfirmed"], ["monitor"])
        self.assertNotIn("confirmed", res)

    def test_handoff_lock_alone_names_both_missing_facts(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p9")
        res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["status"], "handed_over")
        self.assertEqual(res["unconfirmed"], ["reconcile", "monitor"])

    def test_handoff_old_records_do_not_count(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p9")
        self.confirm(at=time.time() - 3600)  # left by an earlier foreman in this pane
        res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["unconfirmed"], ["reconcile", "monitor"])

    def test_handoff_records_of_another_pane_or_a_dead_pid_do_not_count(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p9")
        self.confirm(pane="w:p5", monitor_file=False)  # reconcile run by another pane
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.confirm(reconcile=False, pid=dead.pid)    # a mark whose process never ran for us
        res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["unconfirmed"], ["reconcile", "monitor"])

    def test_handoff_prorab_times_out_and_withdraws_the_handoff(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p1")
        res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["status"], "timeout")
        self.assertIsNone(journal.kv_get("prorab:handoff"))
        self.assertEqual(journal.kv_get("prorab:active"), "w:p1")

    def test_handoff_timeout_race_new_foreman_takes_the_lock_at_the_deadline(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p1")
        real_clear = journal.kv_clear

        def clear(key):
            if key == "prorab:handoff":  # the new foreman acquires just before the withdrawal
                journal.kv_set("prorab:active", "w:p9")
            real_clear(key)
        with mock.patch.object(journal, "kv_clear", clear):
            res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["status"], "handed_over")
        self.assertEqual(journal.kv_get("prorab:active"), "w:p9")

    def test_handoff_timeout_race_handoff_consumed_by_the_new_foreman(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p1")
        real_get = journal.kv_get
        calls = []

        def get(key):
            if key == "prorab:active":
                calls.append(1)
                if len(calls) == 1:  # the loop's read sees the old holder ...
                    return "w:p1"
                return "w:p1"        # ... and so does everything after it
            if key == "prorab:handoff" and len(calls) >= 1:
                return None  # ... but the handoff was consumed meanwhile
            return real_get(key)
        with mock.patch.object(journal, "kv_get", get):
            res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["status"], "handed_over")

    def test_handoff_prorab_start_failure(self):
        self.routes_for_handoff()
        self.route("agent start prorab-2", err("agent_not_ready"), exit_code=1)
        res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["status"], "start_failed")
        self.assertIsNone(journal.kv_get("prorab:handoff"))
        self.assertIn("pane close w:p9", self.calls())  # the split pane does not stay behind

    def test_a_killed_then_retried_handoff_makes_no_second_split(self):
        self.routes_for_handoff()
        self.route("pane close", ok({}))
        journal.kv_set("prorab:active", "w:p1")
        with mock.patch.object(shift, "_await_takeover", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):  # the tool call was killed mid-wait
                shift.handoff_prorab("w:p1", "state", timeout_s=600, poll_s=1)
        self.assertEqual(journal.handoff()["to"], "w:p9")
        self.route("agent list", ok({"agents": [agent_entry("prorab-2", "w:p9")]}))
        self.confirm()
        journal.kv_set("prorab:active", "w:p9")
        res = shift.handoff_prorab("w:p1", "state", timeout_s=5, poll_s=0.01)
        self.assertEqual((res["status"], res["new_pane"], res["delivery"]),
                         ("handed_over", "w:p9", "resumed"))
        self.assertEqual(len([c for c in self.calls() if c.startswith("pane split")]), 1)
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent start")]), 1)
        self.assertEqual(journal.bd(["show", res["bead"]])[0]["status"], "closed")

    def test_a_stale_handoff_without_a_live_agent_is_replaced(self):
        self.routes_for_handoff()
        self.route("pane close", ok({}))
        journal.kv_set("prorab:active", "w:p1")
        journal.handoff_set("w:p1", "w:p7", bead="old", name="prorab-2", ts=1.0)
        self.route("agent list", ok({"agents": []}))
        res = shift.handoff_prorab("w:p1", "state", timeout_s=0.05, poll_s=0.01)
        self.assertEqual(res["new_pane"], "w:p9")
        self.assertIn("pane close w:p7", self.calls())
        self.assertEqual(len([c for c in self.calls() if c.startswith("pane split")]), 1)

    def test_handoff_prorab_assigns_session_id(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p9")  # already taken over
        res = shift.handoff_prorab("w:p1", "state", timeout_s=5, poll_s=0.01)
        self.assertEqual(res["status"], "handed_over")
        start = [c for c in self.calls() if c.startswith("agent start prorab-2")][0]
        self.assertIn("--kind claude --pane w:p9 -- --session-id ", start)
        sid = start.split("--session-id ")[1].split()[0]
        self.assertEqual(str(uuid.UUID(sid, version=4)), sid)
        self.assertEqual(journal.kv_get("shift.foreman_sessions"), sid)
        bead = journal.bd(["show", res["bead"]])[0]
        self.assertEqual(bead["metadata"]["session_id"], sid)
        prompt = [c for c in self.calls() if c.startswith("agent prompt prorab-2")][0]
        self.assertIn("Load the prorab skill and take over the shift from bead %s." % res["bead"],
                      prompt)

    def test_handoff_prorab_sets_the_handoff_before_the_start(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p9")
        seen = []
        real = shift.herdr_cli.run

        def spy(args, *a, **kw):
            if args[:2] == ["agent", "start"]:
                seen.append((journal.handoff() or {}).get("to"))
            return real(args, *a, **kw)
        with mock.patch.object(shift.herdr_cli, "run", spy):
            shift.handoff_prorab("w:p1", "state", timeout_s=5, poll_s=0.01)
        self.assertEqual(seen, ["w:p9"])

    def test_handoff_act_has_spend_and_rate(self):
        self.routes_for_handoff()
        journal.kv_set("prorab:active", "w:p9")
        n = journal.create_work_order("o", "Importer", "b", {"usd": 10})
        wt = "/home/user/projects/demo-app-a"
        journal.issue_set(n, "issued", worktree=wt)
        self.start_shift()
        self.write_transcript(wt, "s1", [("m1", T0 + 60, 3.0)])
        res = shift.handoff_prorab("w:p1", "Crews: c1 in w:p3.", timeout_s=5, poll_s=0.01)
        acts = journal.acts(res["bead"], "shift")
        self.assertEqual(len(acts), 1)
        self.assertIn("Crews: c1 in w:p3.", acts[0])
        self.assertIn("Spend:", acts[0])
        self.assertIn("$3.00 of $100.00 budget", acts[0])
        self.assertIn("window rate: 3.50 $/pp (default)", acts[0])
        self.assertIn("week rate: 10.00 $/pp (default)", acts[0])


class TestReconcile(ShiftBase):
    def test_reconcile_records_its_pane_and_time(self):
        os.environ["HERDR_PANE_ID"] = "w:p9"
        before = time.time()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(shift.main(["reconcile"]), 0)
        rec = json.loads(journal.kv_get("shift.reconciled"))
        self.assertEqual(rec["pane"], "w:p9")
        self.assertTrue(before <= rec["ts"] <= time.time())

    def test_reconcile_issues_alone_records_nothing(self):
        shift.reconcile_issues([])
        self.assertIsNone(journal.kv_get("shift.reconciled"))

    def test_reconcile_issues(self):
        a, b = new_work_order("A"), new_work_order("B")
        journal.issue_set(a, "issuing", name="c1", pane="w:p1", session_id="s1")
        journal.issue_set(b, "issuing", name="c2", pane="w:p2", session_id="s2")
        done = new_work_order("C")
        journal.issue_set(done, "issued", name="c3", pane="w:p3")
        self.route("agent list", ok({"agents": [agent_entry("c1", "w:p1")]}))
        res = shift.reconcile_issues()
        self.assertEqual(sorted(res, key=lambda r: r["work_order"]), sorted([
            {"work_order": a, "name": "c1", "pane": "w:p1", "found": True},
            {"work_order": b, "name": "c2", "pane": "w:p2", "found": False}],
            key=lambda r: r["work_order"]))
        self.assertEqual(journal.crews(), ["c1"])
        self.assertEqual(journal.issue(a)["state"], "issuing")
        self.assertEqual(journal.issue(b)["state"], "not_issued")
        self.assertEqual(journal.issue(done)["state"], "issued")

    def test_reconcile_a_reused_pane_with_another_agent_is_not_found(self):
        a = new_work_order("A")
        journal.issue_set(a, "issuing", name="c1", pane="w:p1")
        res = shift.reconcile_issues([agent_entry("other", "w:p1")])
        self.assertFalse(res[0]["found"])
        self.assertEqual(journal.issue(a)["state"], "not_issued")


if __name__ == "__main__":
    unittest.main()
