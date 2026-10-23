import builtins
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import transcript  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MAIN = os.path.join(FIX, "session_main.jsonl")
SIDE = os.path.join(FIX, "session_sidechain.jsonl")
COST = os.path.join(FIX, "cost")


def _ts(hh, mm):
    return datetime.datetime(2026, 10, 21, hh, mm, tzinfo=datetime.timezone.utc).timestamp()


def _entry(tokens, model):
    return json.dumps({
        "type": "assistant", "isSidechain": False,
        "message": {"model": model, "content": [],
                    "usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 0}},
    })


class TestTranscript(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(
            transcript.project_slug("/home/user/projects/demo-app"),
            "-home-user-projects-demo-app",
        )

    def test_transcript_path_before_file_exists(self):
        with tempfile.TemporaryDirectory() as home:
            p = transcript.transcript_path("/home/user/projects/demo-app", "sid-1", home)
            self.assertEqual(p, os.path.join(
                home, ".claude", "projects", "-home-user-projects-demo-app", "sid-1.jsonl"))
            self.assertFalse(os.path.exists(p))
            self.assertIsNone(transcript.find_transcript("/home/user/projects/demo-app", "sid-1", home))

    def test_models_first_seen_main_chain_only(self):
        self.assertEqual(transcript.models(os.path.join(FIX, "session_mixed_models.jsonl")),
                         ["claude-opus-5-5", "claude-haiku-4-5"])
        self.assertEqual(transcript.models(SIDE), ["claude-opus-5-5"])

    def test_window_is_largest_model_seen(self):
        st = transcript.context_status(
            os.path.join(FIX, "session_mixed_models.jsonl"), _defaults.load())
        self.assertEqual(st["tokens"], 150000)
        self.assertEqual(st["window"], 1000000)
        self.assertEqual(st["pct"], 15.0)
        self.assertFalse(st["soft"])
        self.assertEqual(st["window_model"], "claude-opus-5-5")

    def test_sidechain_model_does_not_widen_window(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "a.jsonl")
            side = json.loads(_entry(1, "claude-opus-5-5"))
            side["isSidechain"] = True
            with open(p, "w") as fh:
                fh.write(json.dumps(side) + "\n")
                fh.write(_entry(130000, "claude-unknown-9") + "\n")
            st = transcript.context_status(p, _defaults.load())
        self.assertEqual(st["window"], 200000)
        self.assertTrue(st["soft"])
        self.assertEqual(st["window_model"], "claude-unknown-9")

    def test_last_usage_ignores_sidechain(self):
        self.assertEqual(transcript.last_usage(SIDE), (362873, "claude-opus-5-5"))
        self.assertEqual(transcript.last_usage(MAIN), (362873, "claude-opus-5-5"))

    def test_context_status_opus(self):
        d = _defaults.load()
        st = transcript.context_status(SIDE, d)
        self.assertEqual(st["tokens"], 362873)
        self.assertEqual(st["window"], 1000000)
        self.assertAlmostEqual(st["pct"], 36.3, places=1)
        self.assertFalse(st["soft"])
        self.assertFalse(st["hard"])
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "a.jsonl")
            with open(p, "w") as fh:
                fh.write(_entry(600000, "claude-opus-5-5") + "\n")
            st = transcript.context_status(p, d)
            # 600 000 of a 1M window: soft by the 40 % share (400 000), not by the cap
            self.assertTrue(st["soft"])
            self.assertFalse(st["hard"])
            with open(p, "w") as fh:
                fh.write(_entry(130000, "claude-unknown-9") + "\n")
            st = transcript.context_status(p, d)
            self.assertEqual(st["window"], 200000)
            self.assertTrue(st["soft"])
            self.assertFalse(st["hard"])

    def test_soft_cap_binds_on_large_window(self):
        d = _defaults.load()
        d["context"]["windows"] = dict(d["context"]["windows"], **{"claude-big-1": 2000000})
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "a.jsonl")
            for tokens, soft in ((450000, False), (500000, True)):  # 40 % would be 800 000
                with open(p, "w") as fh:
                    fh.write(_entry(tokens, "claude-big-1") + "\n")
                st = transcript.context_status(p, d)
                self.assertEqual(st["window"], 2000000)
                self.assertEqual(st["soft"], soft, tokens)

    def test_last_text_skips_tool_only_entries(self):
        self.assertEqual(transcript.last_text(MAIN), "first part\nsecond part")

    def test_usage_totals(self):
        self.assertEqual(
            transcript.usage_totals(MAIN),
            {"input": 12, "cache_creation": 5694, "cache_read": 356912,
             "output": 1415, "messages": 2},
        )

    def test_usage_totals_dedups_by_message_id(self):
        # three lines share one id (last usage wins), one more id
        self.assertEqual(
            transcript.usage_totals(os.path.join(FIX, "session_multiline.jsonl")),
            {"input": 6, "cache_creation": 20, "cache_read": 200,
             "output": 47, "messages": 2},
        )

    def test_find_transcript_glob_fallback(self):
        with tempfile.TemporaryDirectory() as home:
            d = os.path.join(home, ".claude", "projects", "-other-slug")
            os.makedirs(d)
            p = os.path.join(d, "sess1.jsonl")
            open(p, "w").close()
            self.assertEqual(transcript.find_transcript("/a/b", "sess1", home), p)
            self.assertIsNone(transcript.find_transcript("/a/b", "nope", home))
            d2 = os.path.join(home, ".claude", "projects", "-a-b")
            os.makedirs(d2)
            p2 = os.path.join(d2, "sess1.jsonl")
            open(p2, "w").close()
            self.assertEqual(transcript.find_transcript("/a/b", "sess1", home), p2)


def _write_lines(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _said(text, **kw):
    return dict({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}, **kw)


def _told(content, **kw):
    return dict({"type": "user", "message": {"content": content}}, **kw)


class TestFinalReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "t.jsonl")

    def check(self, entries):
        _write_lines(self.path, entries)
        return transcript.final_report(self.path)

    def test_final_report_stage_report(self):
        self.assertEqual(self.check([
            _told("Do stage 2."), _said("# Stage 2 report: CSV export\n\nDone.")]),
            "stage_report")

    def test_final_report_handover_act(self):
        self.assertEqual(self.check([
            _told("Hand over."), _said("Act: done A\n\nHANDOVER READY\n")]), "handover")

    def test_final_report_other_text(self):
        self.assertIsNone(self.check([
            _told("Run it."), _said("Waiting for the test run to finish.")]))

    def test_final_report_older_report_then_a_newer_message(self):
        self.assertIsNone(self.check([
            _told("Do stage 1."), _said("# Stage 1 report: demo\nDone."),
            _told("Now stage 2.")]))

    def test_final_report_tool_results_and_meta_are_no_message(self):
        meta = "is" + "Meta"
        self.assertEqual(self.check([
            _told("Do stage 1."), _said("# Stage 1 report: demo\nDone."),
            _told([{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
            _told("caveat text", **{meta: True})]), "stage_report")

    def test_final_report_background_notice_is_a_message(self):
        self.assertIsNone(self.check([
            _told("Do stage 1."), _said("# Stage 1 report: demo\nDone."),
            _told("<task-notification>shell finished</task-notification>")]))

    def test_final_report_ignores_sidechain(self):
        self.assertEqual(self.check([
            _told("Go."), _said("# Stage 1 report: demo\nDone."),
            _said("Waiting for the run.", isSidechain=True)]), "stage_report")
        self.assertIsNone(self.check([
            _told("Go."), _said("Waiting for the run."),
            _said("# Stage 1 report: demo\nDone.", isSidechain=True)]))

    def test_final_report_missing_file_is_none(self):
        self.assertIsNone(transcript.final_report(os.path.join(self.tmp.name, "none.jsonl")))
        self.assertIsNone(transcript.final_report(None))

    def test_final_report_rereads_only_a_changed_file(self):
        _write_lines(self.path, [_told("Go."), _said("# Stage 1 report: demo\nDone.")])
        self.assertEqual(transcript.final_report(self.path), "stage_report")
        with unittest.mock.patch.object(transcript, "_conversation",
                                        side_effect=AssertionError("read again")):
            self.assertEqual(transcript.final_report(self.path), "stage_report")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_told("Next.")) + "\n")
        self.assertIsNone(transcript.final_report(self.path))

    def test_last_delivered_tells_whether_the_crew_worked_since(self):
        _write_lines(self.path, [_told("one"), _said("ok"), _told("two")])
        self.assertEqual(transcript.last_delivered(self.path), ("two", False))
        _write_lines(self.path, [_told("one"), _said("ok")])
        self.assertEqual(transcript.last_delivered(self.path), ("one", True))


class TestCost(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "home")
        self.root = os.path.realpath(tmp.name)
        self.wt = os.path.join(self.root, "wt")
        os.makedirs(self.wt)
        self.d = _defaults.load()

    def put(self, fixture, cwd, rel, slug_of=None):
        """Copy a cost fixture under the temp HOME, its cwd pointing at ``cwd``."""
        slug = transcript.project_slug(slug_of or cwd)
        dest = os.path.join(self.home, ".claude", "projects", slug, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(os.path.join(COST, fixture), encoding="utf-8") as fh:
            text = fh.read().replace("__CWD__", cwd)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
        return dest

    def test_message_cost_formula(self):
        usage = {"input_tokens": 1000, "output_tokens": 1000, "cache_read_input_tokens": 10000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                    "ephemeral_1h_input_tokens": 1000}}
        # (4000 + 5000 + 8000 + 2000 + 20 000) / 10^6
        self.assertAlmostEqual(transcript.message_cost(usage, "claude-opus-5-5", self.d), 0.039)

    def test_no_split_priced_as_1h(self):
        p = self.put("no_split.jsonl", self.wt, "s.jsonl")
        c = transcript.cost([p], self.d)
        self.assertAlmostEqual(c["usd"], 0.008)
        self.assertEqual(c["tokens"]["cache_write"], 1000)

    def test_family_by_substring(self):
        self.assertEqual(transcript.price_for("claude-opus-5-5", self.d),
                         {"input": 4.0, "output": 20.0, "cache_read": 0.2})
        self.assertEqual(transcript.price_for("claude-haiku-4-5", self.d),
                         {"input": 0.1, "output": 0.5, "cache_read": 0.01})

    def test_unknown_model_priced_at_column_max(self):
        self.assertEqual(transcript.price_for("claude-mystery-1", self.d),
                         {"input": 10.0, "output": 50.0, "cache_read": 0.25})
        self.assertEqual(transcript.price_for(None, self.d),
                         {"input": 10.0, "output": 50.0, "cache_read": 0.25})
        synth = {"cost": {"prices": {
            "a": {"input": 1.0, "output": 9.0, "cache_read": 0.5},
            "b": {"input": 2.0, "output": 3.0, "cache_read": 0.1}}}}
        self.assertEqual(transcript.price_for("zzz", synth),
                         {"input": 2.0, "output": 9.0, "cache_read": 0.5})

    def test_unknown_model_cost_file(self):
        p = self.put("unknown_model.jsonl", self.wt, "s.jsonl")
        self.assertAlmostEqual(transcript.cost([p], self.d)["usd"], 0.06)

    def test_dedupe_by_message_id_across_files(self):
        a = self.put("main.jsonl", self.wt, "s1.jsonl")
        b = self.put("fork.jsonl", self.wt, "s2.jsonl")
        c = transcript.cost([a, b], self.d)
        self.assertEqual(c["messages"], 2)
        self.assertEqual(c["tokens"]["output"], 1050)  # A: 1000, B once with 50
        self.assertAlmostEqual(c["usd"], 0.0404)
        self.assertEqual(list(c["models"]), ["claude-opus-5-5"])
        series = transcript.spend_series([a, b], self.d)
        self.assertEqual([round(t) for t, _ in series], [round(_ts(10, 1)), round(_ts(10, 2))])

    def test_subagents_and_print_runs_counted(self):
        sub = os.path.join(self.wt, "sub")
        os.makedirs(sub)
        self.put("main.jsonl", self.wt, "sess1.jsonl")
        self.put("subagent.jsonl", self.wt, os.path.join("sess1", "subagents", "agent-a.jsonl"))
        self.put("no_split.jsonl", sub, "print1.jsonl")
        files = transcript.worktree_files(self.wt, self.home)
        self.assertEqual(len(files), 3)
        self.assertTrue(any(f.endswith("agent-a.jsonl") for f in files))
        c = transcript.cost(files, self.d)
        self.assertAlmostEqual(c["usd"], 0.0404 + 0.014 + 0.008)
        self.assertEqual(c["messages"], 4)

    def test_session_cost_files(self):
        main = self.put("main.jsonl", self.wt, "sess1.jsonl")
        agent = self.put("subagent.jsonl", self.wt, os.path.join("sess1", "subagents", "a.jsonl"))
        self.put("subagent.jsonl", self.wt, os.path.join("other", "subagents", "b.jsonl"))
        self.assertEqual(transcript.session_cost_files(main), [main, agent])

    def test_worktree_files_skip_sibling_paths(self):
        other = os.path.join(self.root, "wt-other")
        os.makedirs(other)
        mine = self.put("main.jsonl", self.wt, "s.jsonl")
        self.put("main.jsonl", other, "s.jsonl")
        self.assertEqual(transcript.worktree_files(self.wt, self.home), [mine])

    def moved_layout(self):
        """A site with a worktree under it, as Claude Code lays them out."""
        site = os.path.join(self.root, "site")
        wt = os.path.join(site, ".claude", "worktrees", "prorab-N1")
        os.makedirs(wt)
        return site, wt

    def test_work_order_files_finds_a_moved_session(self):
        site, wt = self.moved_layout()
        moved = self.put("main.jsonl", wt, "crew-s.jsonl", slug_of=site)
        sub = self.put("subagent.jsonl", wt, os.path.join("crew-s", "subagents", "a.jsonl"),
                       slug_of=site)
        run = self.put("no_split.jsonl", wt, "print1.jsonl")
        self.put("main.jsonl", site, "customer-s.jsonl")
        files = transcript.work_order_files(wt, ["crew-s"], self.home)
        self.assertEqual(files, sorted([moved, sub, run]))
        self.assertAlmostEqual(transcript.cost(files, self.d)["usd"], 0.0404 + 0.014 + 0.008)

    def test_work_order_files_prefers_the_worktree_directory(self):
        site, wt = self.moved_layout()
        here = self.put("main.jsonl", wt, "crew-s.jsonl")
        self.put("main.jsonl", wt, "crew-s.jsonl", slug_of=site)
        self.assertEqual(transcript.work_order_files(wt, ["crew-s"], self.home), [here])

    def test_work_order_files_falls_back_to_any_project_directory(self):
        site, wt = self.moved_layout()
        elsewhere = self.put("main.jsonl", wt, "crew-s.jsonl",
                             slug_of=os.path.join(self.root, "elsewhere"))
        self.assertEqual(transcript.work_order_files(wt, ["crew-s"], self.home), [elsewhere])
        self.assertEqual(transcript.work_order_files(wt, ["no-such"], self.home), [])

    def test_moved_and_forked_message_counts_once(self):
        site, wt = self.moved_layout()
        moved = self.put("main.jsonl", wt, "crew-s.jsonl", slug_of=site)
        fork = self.put("fork.jsonl", wt, "fork-s.jsonl")
        files = transcript.work_order_files(wt, ["crew-s"], self.home)
        self.assertEqual(files, sorted([moved, fork]))
        c = transcript.cost(files, self.d)
        self.assertEqual(c["messages"], 2)
        self.assertAlmostEqual(c["usd"], 0.0404)

    def test_work_order_files_sibling_slug_not_attributed(self):
        other = os.path.join(self.root, "wt-other")
        os.makedirs(other)
        mine = self.put("main.jsonl", self.wt, "s.jsonl")
        self.put("main.jsonl", other, "s.jsonl")
        self.assertEqual(transcript.work_order_files(self.wt, [], self.home), [mine])

    def test_cost_cli_takes_sessions(self):
        site, wt = self.moved_layout()
        self.put("main.jsonl", wt, "crew-s.jsonl", slug_of=site)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch.dict(
                os.environ, {"HOME": self.home}):
            self.assertEqual(transcript.main(
                ["cost", "--worktree", wt, "--session", "crew-s"]), 0)
        self.assertAlmostEqual(json.loads(out.getvalue())["usd"], 0.0404)

    def test_cost_time_window_and_old_files(self):
        p = self.put("main.jsonl", self.wt, "s.jsonl")
        # A at 10:01, B at 10:02: [10:01, 10:02) holds A only
        c = transcript.cost([p], self.d, since=_ts(10, 1), until=_ts(10, 2))
        self.assertEqual(c["messages"], 1)
        self.assertAlmostEqual(c["usd"], 0.039)
        self.assertEqual(transcript.cost([p], self.d, since=_ts(10, 3))["messages"], 0)
        # a file older than since is not opened, though a line of it is inside the window
        old = _ts(10, 0) - 3600
        os.utime(p, (old, old))
        real_open = builtins.open
        opened = []

        def spy(path, *a, **k):
            opened.append(path)
            return real_open(path, *a, **k)
        builtins.open = spy
        try:
            c = transcript.cost([p], self.d, since=_ts(10, 0))
            files = transcript.machine_files(since=_ts(10, 0), home=self.home)
        finally:
            builtins.open = real_open
        self.assertEqual(c["messages"], 0)
        self.assertEqual(files, [])
        self.assertNotIn(p, opened)
        self.assertEqual(transcript.machine_files(home=self.home), [p])

    def test_sidechain_counts_in_cost_not_in_context(self):
        p = self.put("sidechain.jsonl", self.wt, "s.jsonl")
        self.assertAlmostEqual(transcript.cost([p], self.d)["usd"], 0.00024 + 0.03)
        self.assertEqual(transcript.last_usage(p), (20, "claude-opus-5-5"))

    def test_shared_seen_counts_once(self):
        a = self.put("main.jsonl", self.wt, "s1.jsonl")
        b = self.put("fork.jsonl", self.wt, "s2.jsonl")
        seen = {}
        first = transcript.cost([a], self.d, seen=seen)
        second = transcript.cost([b], self.d, seen=seen)
        self.assertEqual(first["messages"], 2)
        self.assertEqual(second["messages"], 0)
        self.assertEqual(second["usd"], 0.0)

    def test_bad_usage_values_do_not_raise(self):
        p = os.path.join(self.root, "bad.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2026-10-21T10:00:00Z",
                                 "message": {"model": "x", "usage": {"input_tokens": "n/a"}}}) + "\n")
            fh.write("not json \"usage\"\n")
            fh.write(json.dumps({"type": "assistant", "timestamp": "garbage",
                                 "message": {"usage": {"input_tokens": 5}}}) + "\n")
        c = transcript.cost([p], self.d)
        self.assertEqual((c["messages"], c["usd"]), (1, 0.0))

    def test_cli_cost_worktree(self):
        self.put("main.jsonl", self.wt, "s.jsonl")
        saved = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(lambda: os.environ.__setitem__("HOME", saved) if saved is not None
                        else os.environ.pop("HOME", None))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = transcript.main(["cost", "--worktree", self.wt])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertAlmostEqual(out["usd"], 0.0404)
        self.assertEqual(out["messages"], 2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            transcript.main(["cost", "--machine", "--since", "2026-10-21T10:02:00Z"])
        # main.jsonl was written just now, so its mtime passes; only B is in the window
        self.assertEqual(json.loads(buf.getvalue())["messages"], 1)

    def test_guard_refusals_count_and_since(self):
        path = os.path.join(FIX, "transcript_guard_refusal.jsonl")
        # per entry, not per occurrence: each refusal line holds the text twice
        self.assertEqual(transcript.guard_refusals(path), 2)
        stage_start = _ts(9, 30)
        self.assertEqual(transcript.guard_refusals(path, since=stage_start), 1)
        self.assertEqual(transcript.guard_refusals(path, since=_ts(10, 0) + 6), 1)
        self.assertEqual(transcript.guard_refusals(path, since=_ts(10, 0) + 7), 0)
        # an ordinary tool result, a typed message and a missing file count nothing
        self.assertEqual(transcript.guard_refusals(MAIN), 0)
        with self.assertRaises(OSError):
            transcript.guard_refusals(os.path.join(FIX, "no-such.jsonl"))

    def test_guard_refusals_only_real_errors_that_start_with_the_text(self):
        path = os.path.join(FIX, "transcript_guard_refusal.jsonl")
        # the fixture also holds a non-error Read of the work order quoting the marker (string
        # and list content) and an error result with the marker in the middle: none counts
        self.assertEqual(transcript.guard_refusals(path), 2)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.jsonl")
            text = transcript.GUARD_REFUSAL + " /home/user/w, but this command cd's."
            line = {"type": "user", "timestamp": "2026-10-21T10:00:00Z",
                    "message": {"content": [{"type": "tool_result", "is_error": True,
                                             "content": "\n  " + text}]}}
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
            self.assertEqual(transcript.guard_refusals(p), 1)  # leading whitespace is stripped
            line["message"]["content"][0]["is_error"] = False
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
            self.assertEqual(transcript.guard_refusals(p), 0)

    def test_guard_refusals_without_a_timestamp_count(self):
        # fail closed: an undercount would let a hidden refusal pass the equality check
        base = {"type": "user", "message": {"content": [{
            "type": "tool_result", "is_error": True,
            "content": transcript.GUARD_REFUSAL + " /home/user/w, but ..."}]}}
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.jsonl")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(base) + "\n")
                fh.write(json.dumps(dict(base, timestamp="not a time")) + "\n")
                fh.write(json.dumps(dict(base, timestamp="2026-10-21T08:00:00Z")) + "\n")
            self.assertEqual(transcript.guard_refusals(p, since=_ts(9, 0)), 2)
            self.assertEqual(transcript.guard_refusals(p), 3)

    def test_session_paths_are_the_crews_own_sessions(self):
        with tempfile.TemporaryDirectory() as home:
            wt = "/home/user/projects/demo-app/.claude/worktrees/prorab-x1"
            d = os.path.join(home, ".claude", "projects", transcript.project_slug(wt))
            os.makedirs(os.path.join(d, "s1", "subagents"))
            for name in ("s1.jsonl", "s2.jsonl", os.path.join("s1", "subagents", "agent-a.jsonl")):
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write("{}\n")
            got = transcript.session_paths(wt, ["s1", "s2", "s3"], home)
            self.assertEqual(got, [os.path.join(d, "s1.jsonl"), os.path.join(d, "s2.jsonl")])


if __name__ == "__main__":
    unittest.main()
