import json
import os
import shutil
import sys
import tempfile
import time
import unittest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

if os.environ.get("PRORAB_FAST") == "1":
    raise unittest.SkipTest("journal tests run the real `bd`: skipped in the fast tier (PRORAB_FAST=1)")
if shutil.which("bd") is None:
    raise unittest.SkipTest("journal tests need the `bd` (beads >= 1.3) binary on PATH")

import journal  # noqa: E402

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")
ENV_KEYS = ("HOME", "PATH", "BEADS_DIR", "PRORAB_BEADS_DIR", "FAKE_HERDR_DIR", "HERDR_ENV")


class JournalBase(unittest.TestCase):
    """One `bd init` per test class (setUpClass), a fresh copy of that database per test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._class_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._class_tmp.cleanup)
        saved = {k: os.environ.get(k) for k in ENV_KEYS}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        cls.addClassCleanup(restore)
        home = os.path.join(cls._class_tmp.name, "home")  # bd writes config under HOME
        cls._class_pr = os.path.join(cls._class_tmp.name, "prorab")
        os.makedirs(home)
        os.makedirs(cls._class_pr)
        os.environ["HOME"] = home
        os.environ["PRORAB_BEADS_DIR"] = os.path.join(cls._class_pr, ".beads")
        os.environ.pop("BEADS_DIR", None)
        journal.ensure_db()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = {k: os.environ.get(k) for k in ENV_KEYS}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.home = os.path.join(self.tmp.name, "home")
        self.herdr_dir = os.path.join(self.tmp.name, "herdr")
        os.makedirs(self.home)
        os.makedirs(self.herdr_dir)
        self.pr = os.path.join(self.tmp.name, "prorab")
        os.makedirs(self.pr)
        os.environ["HOME"] = self.home
        os.environ["PRORAB_BEADS_DIR"] = os.path.join(self.pr, ".beads")
        os.environ.pop("BEADS_DIR", None)
        os.environ["PATH"] = FAKES + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_HERDR_DIR"] = self.herdr_dir
        os.environ["HERDR_ENV"] = "1"
        shutil.copytree(os.path.join(self._class_pr, ".beads"), os.path.join(self.pr, ".beads"))

    def set_panes(self, *pane_ids):
        body = {"id": "cli:x", "result": {"agents": [{"pane_id": p} for p in pane_ids]}}
        with open(os.path.join(self.herdr_dir, "agents.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        with open(os.path.join(self.herdr_dir, "routes.json"), "w", encoding="utf-8") as fh:
            json.dump({"agent list": "agents.json"}, fh)


def new_work_order(title="T", goal=None, usd=10, site="o"):
    return journal.create_work_order(site, title, "b", {"usd": usd}, goal=goal)


class TestJournal(JournalBase):
    def test_copied_db_is_independent(self):
        journal.kv_set("copy.probe", "1")
        bead = new_work_order("Probe")
        self.assertEqual(journal.kv_get("copy.probe"), "1")
        fresh = os.path.join(self.tmp.name, "fresh")
        os.makedirs(fresh)
        shutil.copytree(os.path.join(self._class_pr, ".beads"), os.path.join(fresh, ".beads"))
        os.environ["PRORAB_BEADS_DIR"] = os.path.join(fresh, ".beads")
        self.assertIsNone(journal.kv_get("copy.probe"))
        self.assertNotIn(bead, [i["id"] for i in journal.ready()])

    def test_ensure_db_is_idempotent_and_local(self):
        journal.ensure_db()
        self.assertTrue(os.path.isdir(os.path.join(self.pr, ".beads")))
        # no agent files / hooks dropped next to the journal
        for name in ("AGENTS.md", "CLAUDE.md", ".claude", ".codex", ".cursor", ".agents"):
            self.assertFalse(os.path.exists(os.path.join(self.pr, name)), name)

    def test_work_order_and_stage_ready_order(self):
        n = journal.create_work_order("site1", "Work order", "Body", {"usd": 12.5})
        s1 = journal.create_stage(n, "Stage 1", "b")
        s2 = journal.create_stage(n, "Stage 2", "b", blocked_by=[s1])
        ids = [i["id"] for i in journal.ready()]
        self.assertIn(n, ids)
        self.assertIn(s1, ids)
        self.assertNotIn(s2, ids)
        journal.bd(["close", s1])
        ids = [i["id"] for i in journal.ready()]
        self.assertIn(s2, ids)

    def test_ready_ignores_foreign_beads(self):
        n = new_work_order()
        other = journal.bd(["create", "alien", "-d", "x", "--silent"], json_out=False).strip()
        ids = [i["id"] for i in journal.ready()]
        self.assertIn(n, ids)
        self.assertNotIn(other, ids)

    def test_acts_roundtrip(self):
        n = new_work_order()
        journal.add_act(n, "acceptance", "accepted")
        journal.add_act(n, "shift", "shift 1")
        journal.add_act(n, "acceptance", "accepted again")
        self.assertEqual(journal.acts(n, "acceptance"), ["accepted", "accepted again"])
        self.assertEqual(journal.acts(n, "shift"), ["shift 1"])
        self.assertEqual(len(journal.acts(n)), 3)
        self.assertTrue(journal.acts(n)[0].startswith("[act:acceptance]"))
        with self.assertRaises(ValueError):
            journal.add_act(n, "bogus", "x")

    def test_budget_override(self):
        self.assertEqual(journal.budget(), {"usd": 100, "week_pp": 10})
        journal.set_budget(usd=500, week_pp=20)
        self.assertEqual(journal.budget(), {"usd": 500, "week_pp": 20})

    def test_machine_override(self):
        self.assertEqual(journal.machine()["parallel_ceiling"], 6)
        journal.set_machine(parallel_ceiling=3)
        m = journal.machine()
        self.assertEqual((m["parallel_ceiling"], m["heavy_slots"]), (3, 2))

    def test_counts_never_over_budget(self):
        n = new_work_order()
        journal.spend(n, sessions=31, runs=2, tokens=10)
        self.assertEqual(journal.over_budget(None), [])
        self.assertEqual(journal.shift_spent(),
                         {"sessions": 31, "runs": 2, "tokens": 10, "usd": 0.0})
        self.assertEqual(journal.kv_get("spent.%s.sessions" % n), "31")
        journal.begin_shift(25.0)
        self.assertEqual(journal.over_budget(35.5), ["week_pp"])

    def test_week_budget_uses_shift_start(self):
        journal.set_budget(usd=700)
        journal.begin_shift(25.0)
        self.assertEqual(journal.over_budget(35.0), [])
        self.assertEqual(journal.over_budget(35.5), ["week_pp"])
        self.assertEqual(journal.over_budget(None), [])
        self.assertEqual(json.loads(journal.kv_get("shift.budget"))["usd"], 700)

    def test_begin_shift_resets_spent(self):
        n = new_work_order()
        journal.spend(n, sessions=3)
        journal.begin_shift(None)
        self.assertEqual(journal.shift_spent(),
                         {"sessions": 0, "runs": 0, "tokens": 0, "usd": 0.0})

    def test_lock_refuses_live_takes_dead(self):
        self.set_panes("p-a", "p-b")
        self.assertTrue(journal.acquire_active("p-a"))
        self.assertTrue(journal.acquire_active("p-a"))
        self.assertFalse(journal.acquire_active("p-b"))
        self.assertEqual(journal.kv_get("prorab:active"), "p-a")
        self.set_panes("p-b")
        self.assertTrue(journal.acquire_active("p-b"))
        self.assertEqual(journal.kv_get("prorab:active"), "p-b")

    def test_lock_handoff(self):
        self.set_panes("p-a", "p-b", "p-c")
        self.assertTrue(journal.acquire_active("p-a"))
        journal.kv_set("prorab:handoff", "p-c")
        self.assertFalse(journal.acquire_active("p-b"))
        self.assertEqual(journal.kv_get("prorab:active"), "p-a")
        self.assertTrue(journal.acquire_active("p-c"))
        self.assertEqual(journal.kv_get("prorab:active"), "p-c")
        self.assertIsNone(journal.kv_get("prorab:handoff"))

    def test_wait_customer_blocks(self):
        n = new_work_order()
        gate = journal.wait_customer(n, "a choice is needed")
        self.assertTrue(gate)
        self.assertNotIn(n, [i["id"] for i in journal.ready()])
        self.assertEqual(journal.acts(n, "answer"), ["a choice is needed"])
        journal.bd(["gate", "resolve", gate], json_out=False)
        self.assertIn(n, [i["id"] for i in journal.ready()])

    def test_restarts_counter(self):
        n = new_work_order()
        self.assertEqual(journal.bump_restarts(n), 1)
        self.assertEqual(journal.bump_restarts(n), 2)
        self.assertEqual(journal.kv_get("restarts.%s" % n), "2")

    def cli_restarts(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = journal.main(["restarts", *argv])
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_restarts_cli_reads_without_bumping(self):
        n = new_work_order()
        self.assertEqual(self.cli_restarts(n), {"restarts": 0})
        self.assertEqual(self.cli_restarts(n), {"restarts": 0})
        self.assertIsNone(journal.kv_get("restarts.%s" % n))
        journal.bump_restarts(n)
        self.assertEqual(self.cli_restarts(n), {"restarts": 1})
        self.assertEqual(self.cli_restarts(n), {"restarts": 1})

    def test_restarts_cli_bump(self):
        n = new_work_order()
        self.assertEqual(self.cli_restarts(n, "--bump"), {"restarts": 1})
        self.assertEqual(self.cli_restarts(n, "--bump"), {"restarts": 2})
        self.assertEqual(journal.kv_get("restarts.%s" % n), "2")

    def test_crews_registry(self):
        self.assertEqual(journal.crews(), [])
        journal.register_crew("b1")
        journal.register_crew("b2")
        journal.register_crew("b1")
        self.assertEqual(journal.crews(), ["b1", "b2"])
        journal.unregister_crew("b1")
        self.assertEqual(journal.crews(), ["b2"])
        journal.unregister_crew("b2")
        self.assertEqual(journal.crews(), [])

    def test_kv_missing_and_roundtrip(self):
        self.assertIsNone(journal.kv_get("nope"))
        journal.kv_set("a.b", "value with spaces")
        self.assertEqual(journal.kv_get("a.b"), "value with spaces")

    def test_cli_budget_set(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = journal.main(["budget", "--set", "usd=500", "--set", "week_pp=20"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out, {"usd": 500, "week_pp": 20})
        self.assertEqual(journal.budget()["usd"], 500)

    def test_cli_work_order_ready_and_lock(self):
        import io
        from contextlib import redirect_stdout

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = journal.main(list(argv))
            return rc, json.loads(buf.getvalue())
        rc, out = run("work-order", "o", "Title", "--body", "b", "--estimate-usd", "10")
        self.assertEqual(rc, 0)
        nid = out["id"]
        self.assertEqual(journal.kv_get("estimate.%s.usd" % nid), "10.0")
        rc, out = run("ready")
        self.assertIn(nid, [i["id"] for i in out])
        self.set_panes("p-a")
        rc, out = run("lock", "p-a")
        self.assertEqual((rc, out), (0, {"acquired": True}))
        rc, out = run("kv", "get", "prorab:active")
        self.assertEqual(out, {"key": "prorab:active", "value": "p-a"})

    # ---- Amendment S: files, estimates, durations, override ----------------

    def test_stage_files_duration_and_actual(self):
        n = new_work_order()
        s = journal.create_stage(n, "Stage", "b", files=["src/a.py", "docs/b.md"],
                                 duration_min=12.5, kind="tests")
        info = journal.stage_info(s)
        self.assertEqual(info["files"], ["src/a.py", "docs/b.md"])
        self.assertEqual(info["duration_min"], 12.5)
        self.assertEqual(info["kind"], "tests")
        self.assertIsNone(info["started_at"])
        self.assertIsNone(info["actual_min"])
        self.assertEqual(journal.durations("tests"), [])
        journal.start_stage(s)
        self.assertEqual(journal.bd(["show", s])[0]["status"], "in_progress")
        self.assertIsNotNone(journal.stage_info(s)["started_at"])
        mins = journal.accept_stage(s, "accepted")
        self.assertGreater(mins, 0)
        self.assertEqual(journal.bd(["show", s])[0]["status"], "closed")
        self.assertEqual(journal.acts(s, "acceptance"), ["accepted"])
        self.assertEqual(journal.stage_info(s)["actual_min"], mins)
        self.assertEqual(journal.durations("tests"), [mins])
        self.assertEqual(journal.durations("docs"), [])
        plain = journal.create_stage(n, "No fields", "b")
        info = journal.stage_info(plain)
        self.assertEqual((info["files"], info["duration_min"], info["kind"]), ([], None, None))

    def test_work_order_files_union(self):
        n = new_work_order("T", goal="g1")
        self.assertEqual(journal.work_order_files(n), [])
        journal.create_stage(n, "A", "b", files=["a.py", "common.py"])
        journal.create_stage(n, "B", "b", files=["common.py", "b.py"])
        self.assertEqual(sorted(journal.work_order_files(n)), ["a.py", "b.py", "common.py"])
        self.assertIn("goal:g1", journal.bd(["show", n])[0]["labels"])

    def test_override_set_get_clear(self):
        self.assertIsNone(journal.override())
        journal.set_override("g1")
        o = journal.override()
        self.assertEqual(o["goal"], "g1")
        self.assertEqual(o["source"], "customer's words")
        self.assertTrue(o["since"])
        journal.clear_override()
        self.assertIsNone(journal.override())
        self.assertIsNone(journal.kv_get("override.ignore_limits"))

    def test_override_expires_with_goal(self):
        # a goal without work orders does not expire the override
        journal.set_override("g1")
        self.assertIsNotNone(journal.override())
        n1 = new_work_order("T1", goal="g1")
        n2 = new_work_order("T2", goal="g1")
        other = new_work_order("T3", goal="g2")
        s = journal.create_stage(n1, "S", "b")
        self.assertIsNotNone(journal.override())
        # stages inherit the goal label but are not work orders (--no-parent)
        listed = journal.bd(["list", "--label", "goal:g1", "--no-parent", "--all", "--limit", "0"])
        self.assertEqual(sorted(i["id"] for i in listed), sorted([n1, n2]))
        self.assertIsNotNone(journal.override())  # n1 has an open stage, n2 is open
        journal.bd(["close", s])
        journal.bd(["close", n1])
        self.assertIsNotNone(journal.override())  # n2 still open
        journal.wait_customer(n2, "a choice is needed")
        self.assertIsNone(journal.override())  # closed + waiting for the customer
        self.assertIsNone(journal.kv_get("override.ignore_limits"))
        journal.set_override("g2")
        self.assertIsNotNone(journal.override())
        journal.bd(["close", other])
        self.assertIsNone(journal.override())

    def test_override_expires_when_stage_waits(self):
        journal.set_override("g1")
        n = new_work_order("T", goal="g1")
        s1 = journal.create_stage(n, "S1", "b")
        s2 = journal.create_stage(n, "S2", "b", blocked_by=[s1])
        s3 = journal.create_stage(n, "S3", "b")
        journal.bd(["close", s3])
        self.assertIsNotNone(journal.override())
        gate = journal.wait_customer(s1, "a choice is needed")  # on a STAGE, not the work order
        self.assertIsNone(journal.override())  # s2 is blocked by the waiting s1
        self.assertIsNone(journal.kv_get("override.ignore_limits"))
        # an open stage that does not wait keeps the override alive
        journal.set_override("g1")
        s4 = journal.create_stage(n, "S4", "b")
        self.assertIsNotNone(journal.override())
        journal.bd(["close", s4])
        self.assertIsNone(journal.override())
        self.assertIsNotNone(gate)
        self.assertTrue(s2)

    def test_override_bad_value_cleared(self):
        for bad in ("not json", "[1, 2]", "{\"since\": 1}", "\"str\""):
            journal.kv_set("override.ignore_limits", bad)
            self.assertIsNone(journal.override(), bad)
            self.assertIsNone(journal.kv_get("override.ignore_limits"), bad)

    def test_cli_stage_override(self):
        import io
        from contextlib import redirect_stdout

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = journal.main(list(argv))
            return rc, json.loads(buf.getvalue())
        _, out = run("work-order", "o", "T", "--goal", "g1", "--estimate-usd", "10")
        nid = out["id"]
        _, out = run("stage", nid, "S", "--file", "a.py", "--file", "b.py",
                     "--duration", "20", "--kind", "docs")
        sid = out["id"]
        _, out = run("stage-info", sid)
        self.assertEqual((out["files"], out["duration_min"], out["kind"]),
                         (["a.py", "b.py"], 20.0, "docs"))
        self.assertEqual(run("work-order-files", nid)[1], ["a.py", "b.py"])
        run("stage-start", sid)
        _, out = run("stage-accept", sid, "ok")
        self.assertGreater(out["actual_min"], 0)
        self.assertEqual(run("durations", "docs")[1], [out["actual_min"]])
        self.assertEqual(run("override", "get")[1], {"override": None})
        run("override", "set", "g1")
        self.assertEqual(run("override", "get")[1]["override"]["goal"], "g1")
        run("override", "clear")
        self.assertEqual(run("override", "get")[1], {"override": None})

    def test_open_stages_in_creation_order(self):
        # bd lists children newest first; the first open stage is the current one (R56)
        n = new_work_order("A")
        stages = [journal.create_stage(n, "S%d" % i, "b") for i in range(1, 4)]
        self.assertEqual(journal.open_stages(n), stages)
        journal.bd(["close", stages[0]])
        self.assertEqual(journal.open_stages(n), stages[1:])

    def test_graph_open_stages_and_ready_work_orders(self):
        a = new_work_order("A")
        b = new_work_order("B")
        c = new_work_order("C")
        journal._run(["dep", "add", b, a])  # b is blocked by a
        journal._run(["dep", "add", c, b])
        sa1 = journal.create_stage(a, "A1", "b")
        sa2 = journal.create_stage(a, "A2", "b", blocked_by=[sa1])
        journal.create_stage(b, "B1", "b")
        g = journal.graph()
        self.assertEqual(g, {a: [], b: [a], c: [b]})
        self.assertEqual(sorted(journal.open_stages(a)), sorted([sa1, sa2]))
        journal.bd(["close", sa1])
        self.assertEqual(journal.open_stages(a), [sa2])
        journal._run(["close", c, "--force"])
        self.assertNotIn(c, journal.graph())
        # a ready stage makes its work order issuable; a working crew hides it
        self.assertIn(a, journal.ready_work_orders())
        self.assertNotIn(b, journal.ready_work_orders())  # blocked by a
        journal.register_crew("br-a")
        journal.kv_set("crew.br-a.work_order", a)
        self.assertNotIn(a, journal.ready_work_orders())
        self.assertEqual(journal.working_work_orders(), [a])


# ---- 8c.4: estimates, budget and spend in dollars -----------------------------------

T0 = 1790000000.0
OPUS = "claude-opus-5-5"


def _iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


class TestJournalDollars(JournalBase):
    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = journal.main(list(argv))
        return rc, json.loads(buf.getvalue())

    def write(self, cwd, session, msgs, sub=None):
        """Synthetic transcript; msgs = [(message id, ts, usd)], priced as opus input tokens."""
        import transcript
        proj = os.path.join(self.home, ".claude", "projects", transcript.project_slug(cwd))
        path = os.path.join(proj, session + ".jsonl")
        if sub:
            path = os.path.join(proj, session, "subagents", sub + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for mid, ts, usd in msgs:
                fh.write(json.dumps({"type": "assistant", "cwd": cwd, "timestamp": _iso(ts),
                                     "message": {"id": mid, "model": OPUS, "usage": {
                                         "input_tokens": int(usd / 4.0 * 1000000),
                                         "output_tokens": 0}}}) + "\n")
        return path

    def start(self, at=T0, foreman=None):
        journal.begin_shift(None, foreman)
        journal.kv_set("shift.started_at", at)

    def spend(self, kv=None):
        import _defaults
        return journal.shift_spend(_defaults.load(), kv=kv or journal.kv_all(), home=self.home)

    def test_work_order_estimate_usd(self):
        n = journal.create_work_order("o", "T", "b", {"usd": 12.5, "heavy_runs_per_stage": 7})
        self.assertEqual(journal.estimate(n), {"usd": 12.5, "heavy_runs_per_stage": 7})
        m = new_work_order()
        self.assertEqual(journal.estimate(m), {"usd": 10.0, "heavy_runs_per_stage": 60})
        self.assertEqual(journal.estimate("wo-none"), {"usd": None, "heavy_runs_per_stage": 60})
        for bad in ({}, {"usd": 0}, {"usd": -1}, {"usd": 5, "tokens": 3},
                    {"usd": 5, "heavy_runs_per_stage": 0}, {"heavy_runs_per_stage": 3}):
            with self.assertRaises(ValueError, msg=bad):
                journal.create_work_order("o", "T", "b", bad)

    def test_budget_usd_override(self):
        journal.set_budget(usd=500.5, week_pp=7.5)
        self.assertEqual(journal.budget(), {"usd": 500.5, "week_pp": 7.5})
        with self.assertRaises(ValueError):
            journal.set_budget(tokens=5)
        rc, out = self.run_cli("budget", "--set", "usd=12.5", "--set", "week_pp=2.5")
        self.assertEqual((rc, out), (0, {"usd": 12.5, "week_pp": 2.5}))

    def test_shift_spend_attributes_by_worktree(self):
        a, b = new_work_order("A", usd=10), new_work_order("B", usd=10)
        journal.issue_set(a, "issued", worktree="/home/user/projects/demo-app-a")
        journal.issue_set(b, "issued", worktree="/home/user/projects/demo-app-b")
        self.start()
        self.write("/home/user/projects/demo-app-a", "s1", [("m1", T0 - 600, 3.0), ("m2", T0 + 60, 2.0)])
        self.write("/home/user/projects/demo-app-b", "s2", [("m3", T0 + 90, 4.0)])
        sp = self.spend()
        self.assertAlmostEqual(sp["work_orders"][a]["usd_shift"], 2.0)
        self.assertAlmostEqual(sp["work_orders"][a]["usd_total"], 5.0)
        self.assertAlmostEqual(sp["work_orders"][b]["usd_shift"], 4.0)
        self.assertEqual((sp["work_orders"][a]["estimate_usd"], sp["work_orders"][a]["over"]), (10.0, False))
        self.assertAlmostEqual(sp["usd"], 6.0)
        self.assertEqual(sp["since"], T0)

    def test_shift_spend_counts_a_moved_session(self):
        a = new_work_order("A", usd=10)
        site = "/home/user/projects/demo-app"
        wt = site + "/.claude/worktrees/prorab-A"
        journal.issue_set(a, "issued", worktree=wt, sessions=["crew-s"])
        self.start()
        self.write(wt, "crew-s", [("m1", T0 + 60, 2.0)])
        moved = os.path.join(self.home, ".claude", "projects", "-home-user-projects-demo-app")
        os.makedirs(moved)
        src = os.path.join(self.home, ".claude", "projects",
                           __import__("transcript").project_slug(wt), "crew-s.jsonl")
        os.rename(src, os.path.join(moved, "crew-s.jsonl"))
        self.write(site, "customer-s", [("c1", T0 + 70, 9.0)])
        sp = self.spend()
        self.assertAlmostEqual(sp["work_orders"][a]["usd_shift"], 2.0)
        self.assertAlmostEqual(sp["usd"], 2.0)

    def test_research_items_by_start_time(self):
        self.start(foreman="fs1")
        own = "/home/user/projects/demo-app"
        self.write(own, "fs1", [("f1", T0 + 10, 1.0)])
        self.write(own, "fs1", [("s1", T0 + 50, 0.5)], sub="agent-1")
        self.write(own, "fs1", [("s2", T0 + 150, 2.0)], sub="agent-2")
        self.write(own, "fs1", [("s3", T0 + 250, 3.0)], sub="agent-3")
        self.write(own, "fs1", [("s4", T0 + 260, 4.0)], sub="agent-4")
        self.write("/home/user/projects/demo-app-r", "run1", [("r1", T0 + 310, 5.0)])
        for key, since, est, cwd in (("a", 100, 1.5, None), ("b", 200, 20.0, None),
                                     ("c", 300, 4.0, "/home/user/projects/demo-app-r")):
            journal.research_begin(key, est, "fs1", cwd)
            rec = journal.research_items()[key]
            self.assertEqual((rec["estimate_usd"], rec["session"], rec["run_cwd"]), (est, "fs1", cwd))
            rec["since"] = T0 + since
            journal.kv_set("research.%s" % key, json.dumps(rec))
        sp = self.spend()
        self.assertAlmostEqual(sp["research"]["a"]["usd"], 2.0)
        self.assertTrue(sp["research"]["a"]["over"])
        self.assertAlmostEqual(sp["research"]["b"]["usd"], 7.0)
        self.assertFalse(sp["research"]["b"]["over"])
        self.assertAlmostEqual(sp["research"]["c"]["usd"], 5.0)
        self.assertAlmostEqual(sp["foreman_usd"], 1.5)
        self.assertAlmostEqual(sp["usd"], 15.5)

    def test_over_budget_items(self):
        journal.begin_shift(25.0)
        spend = {"usd": 360.0,
                 "work_orders": {"wo-1": {"over": True}, "wo-2": {"over": False}},
                 "research": {"k": {"over": True}, "j": {"over": False}}}
        self.assertEqual(journal.over_budget(35.5, spend),
                         ["usd", "week_pp", "estimate:wo-1", "research:k"])
        spend = {"usd": 100.0, "work_orders": {}, "research": {}}
        self.assertEqual(journal.over_budget(35.0, spend), [])
        self.assertEqual(journal.over_budget(35.5), ["week_pp"])

    def test_foreman_handover_keeps_shift_spend(self):
        own = "/home/user/projects/demo-app"
        self.start(foreman="fs1")
        self.write(own, "fs1", [("f1", T0 + 10, 1.0)])
        self.write(own, "fs2", [("f2", T0 + 20, 2.0)])
        journal.kv_set("shift.spent.usd", 3.0)
        self.assertAlmostEqual(self.spend()["foreman_usd"], 1.0)
        journal.add_foreman_session("fs2")
        self.assertEqual(journal.kv_get("shift.foreman_sessions"), "fs1,fs2")
        self.assertEqual(journal.kv_get("shift.started_at"), str(T0))
        self.assertEqual(journal.shift_spent()["usd"], 3.0)
        self.assertAlmostEqual(self.spend()["foreman_usd"], 3.0)

    def test_spend_block_names_rate_source(self):
        n = new_work_order(usd=30)
        self.start()
        spend = {"since": T0, "usd": 120.0, "foreman_usd": 0.0, "research": {},
                 "work_orders": {n: {"usd_shift": 31.0, "usd_total": 31.0,
                                     "estimate_usd": 30.0, "over": True}}}
        rates = {"five_hour": {"usd_per_pp": 5.0, "source": "default", "intervals": 0},
                 "seven_day": {"usd_per_pp": None, "source": "unknown", "intervals": 0}}
        text = journal.spend_block(spend, rates, n)
        self.assertTrue(text.startswith("Spend:"))
        self.assertIn("5.00 $/pp (default)", text)
        self.assertIn("week rate: unknown", text)
        self.assertIn("$31.00 of $30.00", text)
        self.assertIn("6.20 pp of the window", text)
        self.assertIn("$120.00 of $100.00", text)
        rates["seven_day"] = {"usd_per_pp": 12.0, "source": "estimated", "intervals": 3}
        text = journal.spend_block(spend, rates, n)
        self.assertIn("12.00 $/pp (estimated over 3 intervals)", text)
        self.assertIn("2.58 pp of the week", text)

    def test_cli_research_refuses_over_budget(self):
        own = "/home/user/projects/demo-app"
        self.start(foreman="fs1")
        self.write(own, "fs1", [("f1", T0 + 10, 2.0)])
        journal.set_budget(usd=10)
        rc, out = self.run_cli("research", "big", "--estimate-usd", "15", "--session", "fs1")
        self.assertEqual((rc, out["fits"], round(out["left_usd"], 2)), (1, False, 8.0))
        self.assertEqual(journal.research_items(), {})
        rc, out = self.run_cli("research", "ok", "--estimate-usd", "5", "--session", "fs1")
        self.assertEqual((rc, out["fits"]), (0, True))
        self.assertEqual(journal.research_items()["ok"]["estimate_usd"], 5.0)

    def test_cli_research_override_lifts_refusal(self):
        own = "/home/user/projects/demo-app"
        self.start(foreman="fs1")
        self.write(own, "fs1", [("f1", T0 + 10, 2.0)])
        journal.set_budget(usd=10)
        journal.set_override("g1")
        rc, out = self.run_cli("research", "big", "--estimate-usd", "15", "--session", "fs1",
                               "--run-cwd", "/home/user/projects/demo-app-r")
        self.assertEqual((rc, out["fits"], out["override"], round(out["left_usd"], 2)),
                         (0, True, True, 8.0))
        self.assertEqual(journal.research_items()["big"]["run_cwd"], "/home/user/projects/demo-app-r")

    def set_agent(self, pane, session):
        body = {"id": "cli:x", "result": {"agents": [
            {"name": "prorab", "pane_id": pane, "agent_session": {"value": session}}]}}
        with open(os.path.join(self.herdr_dir, "agents.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        with open(os.path.join(self.herdr_dir, "routes.json"), "w", encoding="utf-8") as fh:
            json.dump({"agent list": "agents.json"}, fh)

    def test_cli_research_default_session_from_pane(self):
        own = "/home/user/projects/demo-app"
        self.start(foreman="fs1")
        self.write(own, "fs1", [("f1", T0 + 10, 2.0)])
        self.set_agent("w:p1", "fs1")
        os.environ["HERDR_PANE_ID"] = "w:p1"
        self.addCleanup(os.environ.pop, "HERDR_PANE_ID", None)
        rc, out = self.run_cli("research", "k1", "--estimate-usd", "5")
        self.assertEqual((rc, out["fits"]), (0, True))
        self.assertEqual(journal.research_items()["k1"]["session"], "fs1")
        # an explicit --session still wins
        rc, out = self.run_cli("research", "k2", "--estimate-usd", "1", "--session", "fs2")
        self.assertEqual(journal.research_items()["k2"]["session"], "fs2")

    def test_cli_research_without_pane_needs_session(self):
        os.environ.pop("HERDR_PANE_ID", None)
        rc, out = self.run_cli("research", "k1", "--estimate-usd", "5")
        self.assertEqual((rc, out), (1, {"error": "foreman session unknown: pass --session"}))
        self.set_agent("w:p1", "fs1")  # a pane with no agent in it: still unknown
        os.environ["HERDR_PANE_ID"] = "w:p2"
        self.addCleanup(os.environ.pop, "HERDR_PANE_ID", None)
        rc, out = self.run_cli("research", "k1", "--estimate-usd", "5")
        self.assertEqual((rc, out), (1, {"error": "foreman session unknown: pass --session"}))
        self.assertEqual(journal.research_items(), {})

    def test_begin_shift_lists_work_orders_closed_before(self):
        wt = "/home/user/projects/demo-app-%s"
        closed, still_open, no_time, late, by_act = (new_work_order(t) for t in "CODLA")
        for n in (closed, still_open, no_time, late, by_act):
            journal.issue_set(n, "issued", worktree=wt % n)
        no_issue = new_work_order("N")  # no worktree record: never a shift-spend item
        for n in (closed, no_time, late, by_act, no_issue):
            journal.bd(["close", n])
        journal.add_act(by_act, "acceptance", "done")
        real = journal._show

        def patched(bead):
            item = dict(real(bead))
            if bead in (no_time, by_act):
                item.pop("closed_at", None)
            if bead == late:
                item["closed_at"] = _iso(time.time() + 3600)
            return item
        journal._show = patched
        self.addCleanup(setattr, journal, "_show", real)
        journal.begin_shift(None, "fs1")
        got = journal.kv_get("shift.closed_before").split(",")
        self.assertEqual(sorted(got), sorted([closed, by_act]))
        self.assertAlmostEqual(float(journal.kv_get("shift.started_at")), time.time(), delta=60)

    def test_shift_spend_skips_work_orders_closed_before_shift(self):
        import transcript
        old, during, over = (new_work_order(t, usd=10) for t in "ODV")
        wts = {n: "/home/user/projects/demo-app-%s" % t for n, t in ((old, "o"), (during, "d"), (over, "v"))}
        for n, wt in wts.items():
            journal.issue_set(n, "issued", worktree=wt)
            self.write(wt, "s-" + n, [("m-" + n, T0 + 10, 11.0 if n != during else 1.0)])
        self.start()
        journal.kv_set("shift.closed_before", old)
        asked = []
        real = transcript.worktree_files
        transcript.worktree_files = lambda wt, home=None: (asked.append(wt), real(wt, home))[1]
        self.addCleanup(setattr, transcript, "worktree_files", real)
        sp = self.spend()
        self.assertNotIn(old, sp["work_orders"])
        self.assertNotIn(wts[old], asked)
        self.assertEqual(sorted(sp["work_orders"]), sorted([during, over]))
        self.assertEqual(journal.over_budget(None, sp), ["estimate:%s" % over])

    def test_snapshot_reads_no_bd_dollars(self):
        n = new_work_order()
        journal.issue_set(n, "issued", worktree="/home/user/projects/demo-app-a")
        self.start(foreman="fs1")
        self.write("/home/user/projects/demo-app-a", "s1", [("m1", T0 + 60, 2.0)])
        journal.research_begin("k", 3.0, "fs1")
        kv = journal.kv_all()
        calls = []
        real = journal._run

        def counting(args, check=True):
            calls.append(list(args))
            return real(args, check)
        journal._run = counting
        self.addCleanup(setattr, journal, "_run", real)
        self.assertEqual(journal.estimate(n, kv=kv)["usd"], 10.0)
        self.assertEqual(journal.machine(kv=kv)["heavy_slots"], 2)
        self.assertEqual(list(journal.research_items(kv=kv)), ["k"])
        sp = self.spend(kv)
        self.assertAlmostEqual(sp["work_orders"][n]["usd_shift"], 2.0)
        self.assertEqual(journal.over_budget(None, sp, kv=kv), [])
        self.assertEqual(calls, [])

    def test_set_spent_is_latest_total(self):
        journal.set_spent("wo-1", 4.5)
        journal.set_spent("wo-1", 6.25)
        self.assertEqual(journal.kv_get("spent.wo-1.usd"), "6.25")

    def test_cli_shift_commands(self):
        rc, _ = self.run_cli("shift-begin", "--week-pct", "20", "--foreman-session", "fs1")
        self.assertEqual(rc, 0)
        self.run_cli("foreman-session", "fs2")
        self.assertEqual(journal.kv_get("shift.foreman_sessions"), "fs1,fs2")
        rc, out = self.run_cli("shift-spend")
        self.assertEqual((rc, out["usd"], out["over"]), (0, 0.0, []))


if __name__ == "__main__":
    unittest.main()


class TestJournalG(JournalBase):
    """Amendment G: cycle counters, one push per gate, passport, issue records, kv snapshot."""

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = journal.main(list(argv))
        return rc, json.loads(buf.getvalue())

    def test_iterations_and_cycle_limit(self):
        for i in range(1, 7):
            self.assertEqual(journal.bump_iterations("st1", "approval"), i)
            self.assertFalse(journal.cycle_exceeded("st1", "approval"))
        journal.bump_iterations("st1", "approval")
        self.assertTrue(journal.cycle_exceeded("st1", "approval"))
        self.assertEqual(journal.iterations("st1", "approval"), 7)
        self.assertEqual(journal.iterations("st1", "review"), 0)
        for _ in range(10):
            journal.bump_iterations("st1", "review")
        self.assertFalse(journal.cycle_exceeded("st1", "review"))
        journal.bump_iterations("st1", "review")
        self.assertTrue(journal.cycle_exceeded("st1", "review"))
        for bad in ("bogus", ""):
            with self.assertRaises(ValueError):
                journal.bump_iterations("st1", bad)
            with self.assertRaises(ValueError):
                journal.iterations("st1", bad)
            with self.assertRaises(ValueError):
                journal.cycle_exceeded("st1", bad)

    def test_cycle_exceeded_uses_given_defaults(self):
        journal.bump_iterations("st2", "report_fix")
        journal.bump_iterations("st2", "report_fix")
        self.assertFalse(journal.cycle_exceeded("st2", "report_fix"))
        self.assertTrue(journal.cycle_exceeded("st2", "report_fix", {"cycles": {"approval": 6, "review": 10, "report_fix": 1}}))

    def test_mark_notified_once(self):
        self.assertTrue(journal.mark_notified("gate-1"))
        self.assertFalse(journal.mark_notified("gate-1"))
        self.assertTrue(journal.mark_notified("api_key.crew-a"))
        n = journal.notified()
        self.assertEqual(sorted(n), ["api_key.crew-a", "gate-1"])
        self.assertGreater(int(n["gate-1"]), 1_000_000_000)

    def test_passport_one_per_site(self):
        self.assertIsNone(journal.passport("demo-app"))
        a = journal.set_passport("demo-app", "run tests: make test")
        self.assertEqual(journal.passport("demo-app"), {"id": a, "text": "run tests: make test"})
        self.assertEqual(journal.set_passport("demo-app", "run tests: make check"), a)
        self.assertEqual(journal.passport("demo-app")["text"], "run tests: make check")
        b = journal.set_passport("other-app", "x")
        self.assertNotEqual(a, b)
        self.assertEqual(journal.passport("demo-app")["id"], a)
        labels = journal.bd(["show", a])[0]["labels"]
        self.assertIn("prorab:passport", labels)
        self.assertIn("site:demo-app", labels)

    def test_issue_records(self):
        self.assertIsNone(journal.issue("wo-1"))
        journal.issue_set("wo-1", "issuing", name="crew-a", pane="p-1", session_id="s-1")
        rec = journal.issue("wo-1")
        self.assertEqual((rec["state"], rec["name"], rec["pane"], rec["session_id"]),
                         ("issuing", "crew-a", "p-1", "s-1"))
        self.assertIn("ts", rec)
        journal.issue_set("wo-1", "issued", worktree="/home/user/projects/demo-app-wt")
        rec = journal.issue("wo-1")
        self.assertEqual((rec["state"], rec["name"], rec["worktree"]),
                         ("issued", "crew-a", "/home/user/projects/demo-app-wt"))
        journal.issue_set("wo-2", "not_issued")
        self.assertEqual(sorted(journal.issues()), ["wo-1", "wo-2"])
        self.assertEqual(list(journal.issues("not_issued")), ["wo-2"])
        self.assertEqual(journal.issues("issuing"), {})
        with self.assertRaises(ValueError):
            journal.issue_set("wo-1", "bogus")
        self.assertEqual(journal.issue("wo-1")["state"], "issued")

    def test_snapshot_reads_no_bd(self):
        journal.set_budget(usd=5)
        journal.register_crew("crew-a")
        journal.spend("wo-1", sessions=2)
        journal.kv_set("shift.week_start", "10")
        kv = journal.kv_all()
        calls = []
        real = journal._run

        def counting(args, check=True):
            calls.append(list(args))
            return real(args, check)
        journal._run = counting
        self.addCleanup(setattr, journal, "_run", real)
        self.assertEqual(journal.budget(kv=kv)["usd"], 5)
        self.assertEqual(journal.shift_spent(kv=kv)["sessions"], 2)
        self.assertEqual(journal.over_budget(12.0, kv=kv), [])
        self.assertEqual(journal.over_budget(100.0, kv=kv), ["week_pp"])
        self.assertEqual(journal.crews(kv=kv), ["crew-a"])
        self.assertIsNone(journal.override(kv=kv))
        self.assertEqual(calls, [])

    def test_snapshot_override_set(self):
        journal.set_override("goal-x")
        kv = journal.kv_all()
        self.assertEqual(journal.override(kv=kv)["goal"], "goal-x")  # no work orders: not done
        journal.clear_override()
        self.assertIsNone(journal.override(kv=journal.kv_all()))

    def test_cli_notify_and_iterations(self):
        self.assertEqual(self.run_cli("notify", "gate-9", "--kind", "api_key"),
                         (0, {"push": True}))
        self.assertEqual(self.run_cli("notify", "gate-9", "--kind", "api_key"),
                         (0, {"push": False}))
        self.assertEqual(self.run_cli("iterations", "st9", "approval"),
                         (0, {"n": 0, "limit": 6, "exceeded": False}))
        self.assertEqual(self.run_cli("iterations", "st9", "report_fix", "--bump")[1],
                         {"n": 1, "limit": 3, "exceeded": False})
        path = os.path.join(self.tmp.name, "passport.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("entry: src/main.py")
        rc, out = self.run_cli("passport", "demo-app", "--set-file", path)
        self.assertEqual(out["text"], "entry: src/main.py")
        journal.issue_set("wo-3", "issuing")
        rc, out = self.run_cli("issues", "--state", "issuing")
        self.assertEqual(list(out), ["wo-3"])


class TestCloseWorkOrder(JournalBase):
    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = journal.main(list(argv))
        return rc, json.loads(buf.getvalue())

    def status(self, bead):
        return journal._show(bead).get("status")

    def two_stages(self):
        n = new_work_order()
        done = journal.create_stage(n, "One", "b")
        todo = journal.create_stage(n, "Two", "b")
        journal.start_stage(done)
        journal.accept_stage(done, "ok")
        return n, done, todo

    def test_close_work_order_closes_open_stages_then_itself(self):
        n, done, todo = self.two_stages()
        got = journal.close_work_order(n, "the branch merged")
        self.assertEqual(got, {"work_order": n, "closed": [todo, n], "already": False})
        for bead in (n, done, todo):
            self.assertEqual(self.status(bead), "closed")
        self.assertNotEqual(journal._show(done).get("close_reason"), "the branch merged")
        self.assertEqual(journal._show(todo).get("close_reason"), "the branch merged")

    def test_close_work_order_twice_closes_nothing(self):
        n, _done, _todo = self.two_stages()
        journal.close_work_order(n, "every stage accepted")
        self.assertEqual(journal.close_work_order(n, "every stage accepted"),
                         {"work_order": n, "closed": [], "already": True})

    def test_closed_work_order_is_not_ready(self):
        n = new_work_order()
        s = journal.create_stage(n, "One", "b")
        journal.start_stage(s)
        journal.accept_stage(s, "ok")
        self.assertIn(n, journal.ready_work_orders())  # the live defect
        journal.close_work_order(n, "every stage accepted")
        self.assertNotIn(n, journal.ready_work_orders())
        self.assertNotIn(n, journal.graph())

    def test_stage_added_to_a_closed_work_order_reopens_it(self):
        n, _done, _todo = self.two_stages()
        journal.close_work_order(n, "every stage accepted")
        s = journal.create_stage(n, "Three", "b")
        self.assertEqual(self.status(n), "open")
        self.assertEqual(journal.open_stages(n), [s])
        self.assertIn(n, journal.ready_work_orders())

    def test_closed_before_the_shift_leaves_the_shift_spend(self):
        import _defaults
        n = new_work_order()
        journal.issue_set(n, "issued", worktree="/home/user/projects/demo-app-a")
        journal.close_work_order(n, "every stage accepted")
        time.sleep(1.1)  # bd stamps whole seconds
        journal.begin_shift(None, "fs1")
        self.assertIn(n, journal.kv_get("shift.closed_before").split(","))
        sp = journal.shift_spend(_defaults.load(), kv=journal.kv_all(), home=self.home)
        self.assertNotIn(n, sp["work_orders"])

    def test_has_open_gate(self):
        n = new_work_order()
        self.assertFalse(journal.has_open_gate(n))
        journal.wait_customer(n, "a question")
        self.assertTrue(journal.has_open_gate(n))

    def test_close_work_order_cli(self):
        n, _done, todo = self.two_stages()
        rc, out = self.run_cli("close-work-order", n, "--reason", "dropped by the customer")
        self.assertEqual((rc, out), (0, {"work_order": n, "closed": [todo, n], "already": False}))
        self.assertEqual(self.status(n), "closed")


class TestOverrideHistory(JournalBase):
    def test_history_written_on_set_and_closed_on_expiry(self):
        journal.set_override("g1")
        hist = journal.override_history()
        self.assertEqual([(h["goal"], h["until"]) for h in hist], [("g1", None)])
        self.assertIsInstance(hist[0]["since"], int)
        n = new_work_order("T", goal="g1")
        st = journal.create_stage(n, "S", "b")
        journal.start_stage(st)
        journal.accept_stage(st, "ok")
        journal.bd(["close", n])
        self.assertIsNone(journal.override())
        hist = journal.override_history()
        self.assertEqual(len(hist), 1)
        self.assertGreaterEqual(hist[0]["until"], hist[0]["since"])

    def test_history_closed_when_the_customer_cancels(self):
        journal.set_override("g1")
        journal.clear_override()
        self.assertIsNotNone(journal.override_history()[0]["until"])
        journal.set_override("g2")
        self.assertEqual([h["until"] is None for h in journal.override_history()], [False, True])


SITE = "/home/user/projects/demo-app"


def cli(*argv):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = journal.main(list(argv))
    return rc, json.loads(buf.getvalue())


def cli_exit(*argv):
    """The exit code of a CLI call that argparse or the journal refuses."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        with unittest.TestCase().assertRaises(SystemExit) as cm:
            journal.main(list(argv))
    return cm.exception.code


class TestDue(JournalBase):
    def setUp(self):
        super().setUp()
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
        self.log_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.log_dir)
        saved = os.environ.get("PRORAB_LOG_DIR")
        os.environ["PRORAB_LOG_DIR"] = self.log_dir

        def restore_log():
            if saved is None:
                os.environ.pop("PRORAB_LOG_DIR", None)
            else:
                os.environ["PRORAB_LOG_DIR"] = saved
        self.addCleanup(restore_log)

    def test_due_at_next_occurrence(self):
        import datetime
        utc = datetime.timezone.utc

        def ts(*a, tz=utc):
            return datetime.datetime(*a, tzinfo=tz).timestamp()
        self.assertEqual(journal.due_at("15:30", ts(2026, 10, 22, 14, 0), utc),
                         ts(2026, 10, 22, 15, 30))
        self.assertEqual(journal.due_at("15:30", ts(2026, 10, 22, 16, 0), utc),
                         ts(2026, 10, 23, 15, 30))
        self.assertEqual(journal.due_at("15:30", ts(2026, 10, 22, 15, 30), utc),
                         ts(2026, 10, 22, 15, 30))
        import zoneinfo
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")
        now = ts(2026, 10, 24, 23, 0, tz=berlin)
        got = journal.due_at("09:00", now, berlin)
        self.assertEqual(got, ts(2026, 10, 25, 9, 0, tz=berlin))
        self.assertEqual(got - now, 11 * 3600)   # the clock change is inside the gap
        for bad in ("24:00", "9:5", "x", "12:60", ""):
            with self.assertRaises(ValueError, msg=bad):
                journal.due_at(bad, now, utc)

    def test_due_set_list_clear(self):
        now = time.time()
        rec = journal.set_due("check-tests", now + 1200, "the unit suite of wo-12")
        self.assertEqual(rec["label"], "check-tests")
        self.assertEqual(rec["at_local"], time.strftime("%Y-%m-%d %H:%M",
                                                         time.localtime(now + 1200)))
        stored = json.loads(journal.kv_get("due.check-tests"))
        self.assertEqual(stored["at"], now + 1200)
        self.assertEqual(stored["text"], "the unit suite of wo-12")
        self.assertAlmostEqual(stored["set_at"], now, delta=30)
        listed = journal.dues(now=now)
        self.assertEqual([(d["label"], d["overdue"]) for d in listed], [("check-tests", False)])
        self.assertIn("at_local", listed[0])
        self.assertTrue(journal.dues(now=now + 1300)[0]["overdue"])
        journal.set_due("check-tests", now + 600, "moved")
        listed = journal.dues(now=now)
        self.assertEqual((len(listed), listed[0]["at"], listed[0]["text"]),
                         (1, now + 600, "moved"))
        self.assertTrue(journal.clear_due("check-tests"))
        self.assertFalse(journal.clear_due("check-tests"))
        self.assertEqual(journal.dues(now=now), [])

    def test_dues_sorted_and_unreadable_listed(self):
        now = time.time()
        journal.set_due("late", now + 900)
        journal.set_due("early", now + 60)
        journal.kv_set("due.broken", "garbage")
        listed = journal.dues(now=now)
        self.assertEqual([d["label"] for d in listed if "error" not in d], ["early", "late"])
        self.assertIn({"label": "broken", "error": "unreadable"}, listed)

    def test_due_bad_label(self):
        for bad in ("Check Tests", "1abc", "a" * 33, "", "a.b"):
            with self.assertRaises(ValueError, msg=bad):
                journal.set_due(bad, time.time() + 60, "t")
        self.assertEqual([k for k in journal.kv_all() if k.startswith("due.")], [])
        journal.set_due("a" * 32, time.time() + 60, "ok")   # the longest label

    def test_due_cli(self):
        before = time.time()
        rc, out = cli("due", "set", "check-tests", "--in", "20", "--text", "T")
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(out["at"], before + 1200, delta=60)
        self.assertEqual((out["label"], out["text"], out["monitor"]), ("check-tests", "T", False))
        with open(os.path.join(self.log_dir, "monitor-w_p1.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "pane": "w:p1", "started_at": before}, fh)
        _, out = cli("due", "set", "second", "--in", "0.5")
        self.assertTrue(out["monitor"])
        self.assertAlmostEqual(out["at"], time.time() + 30, delta=30)
        _, out = cli("due", "set", "clock", "--at", "23:59", "--text", "late")
        self.assertRegex(out["at_local"], r" 23:59$")
        self.assertEqual(cli_exit("due", "set", "x1", "--at", "10:00", "--in", "5"), 2)
        self.assertEqual(cli_exit("due", "set", "x1"), 2)
        self.assertEqual(cli_exit("due", "set", "x1", "--in", "0"), 2)
        self.assertEqual(cli_exit("due", "set", "x1", "--at", "25:00"), 2)
        self.assertEqual(cli_exit("due", "set", "Bad Label", "--in", "5"), 2)
        _, out = cli("due", "list")
        self.assertEqual({d["label"] for d in out}, {"check-tests", "second", "clock"})
        self.assertEqual(cli("due", "clear", "second")[1], {"label": "second", "cleared": True})
        self.assertEqual(cli("due", "clear", "second")[1], {"label": "second", "cleared": False})

    def test_due_cli_ignores_a_dead_monitor_mark(self):
        with open(os.path.join(self.log_dir, "monitor-w_p1.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 2 ** 22 + 12345, "pane": "w:p1", "started_at": 0}, fh)
        self.assertFalse(cli("due", "set", "x1", "--in", "5")[1]["monitor"])


class TestConsent(JournalBase):
    def work_order(self, **issue):
        wo = new_work_order("Handbook")
        fields = dict(name="a1", pane="w:p9", site=SITE)
        fields.update(issue)
        journal.issue_set(wo, "not_issued", **fields)
        return wo

    def gate_open(self, gate):
        return journal.bd(["show", gate])[0]["status"] != "closed"

    def wait(self, bead, reason):
        return cli("wait", bead, reason)[1]

    def test_wait_on_consent_records_the_watch(self):
        wo = self.work_order()
        out = self.wait(wo, "consent prompt: trust")
        self.assertIsNone(out["no_watch"])
        self.assertFalse(out["reused"])
        rec = json.loads(journal.kv_get("consent.%s.trust" % wo))
        self.assertEqual(out["watch"], rec)
        self.assertEqual((rec["kind"], rec["gate"], rec["bead"], rec["pane"], rec["name"],
                          rec["site"], rec["watch"], rec["no_watch"]),
                         ("trust", out["gate"], wo, "w:p9", "a1", SITE, True, None))
        self.assertAlmostEqual(rec["since"], time.time(), delta=60)
        self.assertEqual(journal.consents()[(wo, "trust")], rec)
        self.assertTrue(self.gate_open(out["gate"]))

    def test_wait_on_a_stage_finds_its_work_order(self):
        wo = self.work_order()
        stage = journal.create_stage(wo, "S1", "b")
        out = self.wait(stage, "consent prompt: mcp")
        self.assertIsNone(out["no_watch"])
        rec = json.loads(journal.kv_get("consent.%s.mcp" % wo))
        self.assertEqual((rec["bead"], rec["gate"]), (stage, out["gate"]))
        self.assertIsNone(journal.kv_get("consent.%s.mcp" % stage))

    def test_wait_watch_takes_the_crews_current_pane(self):
        wo = self.work_order()
        journal.register_crew("a1")
        journal.kv_set("crew.a1.pane", "w:p12")
        self.wait(wo, "consent prompt: mcp")
        self.assertEqual(json.loads(journal.kv_get("consent.%s.mcp" % wo))["pane"], "w:p12")

    def test_wait_site_from_the_crew_when_the_record_lacks_it(self):
        wo = self.work_order(site=None)
        journal.register_crew("a1")
        journal.kv_set("crew.a1.site", SITE)
        self.wait(wo, "consent prompt: trust")
        self.assertEqual(json.loads(journal.kv_get("consent.%s.trust" % wo))["site"], SITE)

    def test_wait_reports_why_no_watch(self):
        orphan = new_work_order("No record")
        out = self.wait(orphan, "consent prompt: trust")
        self.assertEqual((out["watch"], out["no_watch"]), (None, "no_issue_record"))
        self.assertEqual([k for k in journal.kv_all() if k.startswith("consent.")], [])
        wo = self.work_order()
        out = self.wait(wo, "consent prompt: folder trust")
        self.assertEqual((out["watch"], out["no_watch"]), (None, "unknown_kind"))
        self.assertEqual([k for k in journal.kv_all() if k.startswith("consent.")], [])
        for kind, issue, why in (("api_key", {}, "api_key"),
                                 ("mcp", {"pane": None}, "no_pane"),
                                 ("trust", {"site": None}, "no_site")):
            wo = self.work_order(**issue)
            out = self.wait(wo, "consent prompt: %s" % kind)
            self.assertEqual((out["watch"], out["no_watch"]), (None, why), kind)
            rec = json.loads(journal.kv_get("consent.%s.%s" % (wo, kind)))
            self.assertEqual((rec["watch"], rec["no_watch"]), (False, why))

    def test_wait_other_reason_records_no_watch(self):
        wo = self.work_order()
        out = self.wait(wo, "spent $31 of estimate $30")
        self.assertEqual((out["watch"], out["no_watch"], out["reused"]), (None, None, False))
        self.assertEqual([k for k in journal.kv_all() if k.startswith("consent")], [])
        self.assertTrue(self.gate_open(out["gate"]))
        self.assertEqual(journal.acts(wo, "answer"), ["spent $31 of estimate $30"])

    def test_second_watch_same_kind_in_shift_is_not_armed(self):
        wo = self.work_order()
        first = self.wait(wo, "consent prompt: trust")
        again = self.wait(wo, "consent prompt: trust")
        self.assertEqual((again["gate"], again["reused"], again["watch"], again["no_watch"]),
                         (first["gate"], True, None, "repeat"))
        rec = json.loads(journal.kv_get("consent.%s.trust" % wo))
        self.assertEqual((rec["watch"], rec["no_watch"]), (False, "repeat"))
        self.assertTrue(self.gate_open(first["gate"]))   # nothing is resolved
        gates = [d for d in journal.bd(["list", "--type", "gate", "--all", "--limit", "0"])]
        self.assertEqual(len(gates), 1)
        done = journal.consent_done(wo, "trust", by="customer")
        self.assertTrue(done["resolved"])
        third = self.wait(wo, "consent prompt: trust")   # the prompt came back later
        self.assertNotEqual(third["gate"], first["gate"])
        self.assertFalse(third["reused"])
        self.assertEqual((third["watch"], third["no_watch"]), (None, "repeat"))
        other = self.wait(wo, "consent prompt: mcp")     # another kind is armed
        self.assertIsNotNone(other["watch"])
        seen = json.loads(journal.kv_get("consent_seen.%s" % wo))
        self.assertEqual([s["kind"] for s in seen], ["trust", "mcp"])
        self.assertNotIn((wo, "seen"), journal.consents())
        self.assertTrue(all(k[1] != "seen" for k in journal.consents()))

    def test_a_later_shift_arms_the_same_kind_again(self):
        wo = self.work_order()
        self.wait(wo, "consent prompt: trust")
        journal.consent_done(wo, "trust", by="customer")
        journal.kv_set("shift.started_at", time.time() + 5)
        out = self.wait(wo, "consent prompt: trust")
        self.assertIsNotNone(out["watch"])

    def test_records_of_two_kinds_stand_side_by_side(self):
        wo = self.work_order()
        trust = self.wait(wo, "consent prompt: trust")
        mcp = self.wait(wo, "consent prompt: mcp")
        self.assertNotEqual(trust["gate"], mcp["gate"])
        self.assertEqual({k for k in journal.consents()}, {(wo, "trust"), (wo, "mcp")})
        done = journal.consent_done(wo, "mcp")
        self.assertTrue(done["resolved"])
        self.assertTrue(self.gate_open(trust["gate"]))
        self.assertFalse(self.gate_open(mcp["gate"]))
        self.assertEqual(set(journal.consents()), {(wo, "trust")})

    def test_consent_done_resolves_a_watchless_record(self):
        wo = self.work_order()
        out = self.wait(wo, "consent prompt: api_key")
        rc, done = cli("consent", "done", wo, "api_key", "--by", "customer")
        self.assertEqual((rc, done["found"], done["resolved"], done["gate"]),
                         (0, True, True, out["gate"]))
        self.assertFalse(self.gate_open(out["gate"]))
        self.assertIsNone(journal.kv_get("consent.%s.api_key" % wo))

    def test_consent_done_gate_without_a_record(self):
        orphan = new_work_order("No record")
        out = self.wait(orphan, "consent prompt: trust")
        self.assertEqual(cli_exit("consent", "done", orphan, "trust", "--gate", out["gate"]), 2)
        self.assertEqual(cli_exit("consent", "done", orphan, "trust", "--gate", out["gate"],
                                  "--by", "monitor"), 2)
        self.assertTrue(self.gate_open(out["gate"]))
        rc, done = cli("consent", "done", orphan, "trust", "--gate", out["gate"],
                       "--by", "customer")
        self.assertEqual((done["found"], done["resolved"], done["gate"]), (True, True, out["gate"]))
        self.assertFalse(self.gate_open(out["gate"]))
        acts = journal.acts(orphan, "answer")
        self.assertTrue(any(a.startswith("consent done: trust answered by the customer")
                            for a in acts), acts)

    def test_consent_done_resolves_clears_and_records(self):
        wo = self.work_order()
        out = self.wait(wo, "consent prompt: trust")
        rc, done = cli("consent", "done", wo, "trust")
        self.assertEqual(done, {"work_order": wo, "kind": "trust", "found": True,
                                "gate": out["gate"], "resolved": True})
        self.assertFalse(self.gate_open(out["gate"]))
        self.assertIsNone(journal.kv_get("consent.%s.trust" % wo))
        self.assertIn("consent done: trust answered by the customer in pane w:p9 "
                      "(seen by monitor)", journal.acts(wo, "answer"))
        again = journal.consent_done(wo, "trust")
        self.assertEqual(again, {"work_order": wo, "kind": "trust", "found": False})
        wo2 = self.work_order()
        self.wait(wo2, "consent prompt: mcp")
        rc, done = cli("consent", "done", wo2, "mcp", "--by", "customer")
        self.assertIn("(seen by customer)", journal.acts(wo2, "answer")[-1])

    def test_consent_list_cli(self):
        wo = self.work_order()
        self.wait(wo, "consent prompt: trust")
        rc, listed = cli("consent", "list")
        self.assertEqual([(r["kind"], r["work_order"]) for r in listed], [("trust", wo)])

    def test_consent_done_gate_already_closed(self):
        wo = self.work_order()
        out = self.wait(wo, "consent prompt: trust")
        journal.bd(["gate", "resolve", out["gate"]], json_out=False)   # the customer closed it
        done = journal.consent_done(wo, "trust")
        self.assertEqual((done["found"], done["resolved"]), (True, False))
        self.assertIsNone(journal.kv_get("consent.%s.trust" % wo))
        self.assertTrue(any(a.startswith("consent done: trust") for a in journal.acts(wo, "answer")))


class TestJournalLabels(JournalBase):
    def test_add_label_and_labels(self):
        n = new_work_order()
        self.assertNotIn("needs:clearance", journal.labels(n))
        journal.add_label(n, "needs:clearance")
        journal.add_label(n, "needs:clearance")  # idempotent
        self.assertEqual(journal.labels(n).count("needs:clearance"), 1)
        self.assertIn(journal.WORK_ORDER_LABEL, journal.labels(n))

    def test_label_cli(self):
        import io
        from contextlib import redirect_stdout
        n = new_work_order()

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = journal.main(list(argv))
            return rc, json.loads(buf.getvalue())
        rc, out = run("label", "add", n, "clearance:granted")
        self.assertEqual(rc, 0)
        rc, out = run("label", "list", n)
        self.assertEqual(rc, 0)
        self.assertIn("clearance:granted", out)

    def test_act_records_carry_time(self):
        n = new_work_order()
        journal.add_act(n, "answer", "first")
        journal.add_act(n, "shift", "second")
        recs = journal.act_records(n)
        self.assertEqual([(r["kind"], r["text"]) for r in recs],
                         [("answer", "first"), ("shift", "second")])
        self.assertTrue(all(isinstance(r["ts"], float) for r in recs))
        self.assertEqual([r["text"] for r in journal.act_records(n, "shift")], ["second"])
