import json
import os
import shutil
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

if shutil.which("bd") is None:
    raise unittest.SkipTest("journal tests need the `bd` (beads >= 1.3) binary on PATH")

import journal  # noqa: E402

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")
ENV_KEYS = ("HOME", "PATH", "BEADS_DIR", "PRORAB_BEADS_DIR", "FAKE_HERDR_DIR", "HERDR_ENV")


class JournalBase(unittest.TestCase):
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
        journal.ensure_db()

    def set_panes(self, *pane_ids):
        body = {"id": "cli:x", "result": {"agents": [{"pane_id": p} for p in pane_ids]}}
        with open(os.path.join(self.herdr_dir, "agents.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        with open(os.path.join(self.herdr_dir, "routes.json"), "w", encoding="utf-8") as fh:
            json.dump({"agent list": "agents.json"}, fh)


class TestJournal(JournalBase):
    def test_ensure_db_is_idempotent_and_local(self):
        journal.ensure_db()
        self.assertTrue(os.path.isdir(os.path.join(self.pr, ".beads")))
        # no agent files / hooks dropped next to the journal
        for name in ("AGENTS.md", "CLAUDE.md", ".claude", ".codex", ".cursor", ".agents"):
            self.assertFalse(os.path.exists(os.path.join(self.pr, name)), name)

    def test_work_order_and_stage_ready_order(self):
        n = journal.create_work_order("site1", "Наряд", "Тело", {"допуск": "да", "n": 3})
        self.assertEqual(journal.kv_get("estimate.%s.допуск" % n), "да")
        self.assertEqual(journal.kv_get("estimate.%s.n" % n), "3")
        s1 = journal.create_stage(n, "Этап 1", "b")
        s2 = journal.create_stage(n, "Этап 2", "b", blocked_by=[s1])
        ids = [i["id"] for i in journal.ready()]
        self.assertIn(n, ids)
        self.assertIn(s1, ids)
        self.assertNotIn(s2, ids)
        journal.bd(["close", s1])
        ids = [i["id"] for i in journal.ready()]
        self.assertIn(s2, ids)

    def test_ready_ignores_foreign_beads(self):
        n = journal.create_work_order("o", "T", "b", {})
        other = journal.bd(["create", "alien", "-d", "x", "--silent"], json_out=False).strip()
        ids = [i["id"] for i in journal.ready()]
        self.assertIn(n, ids)
        self.assertNotIn(other, ids)

    def test_acts_roundtrip(self):
        n = journal.create_work_order("o", "T", "b", {})
        journal.add_act(n, "acceptance", "принято")
        journal.add_act(n, "shift", "смена 1")
        journal.add_act(n, "acceptance", "принято снова")
        self.assertEqual(journal.acts(n, "acceptance"), ["принято", "принято снова"])
        self.assertEqual(journal.acts(n, "shift"), ["смена 1"])
        self.assertEqual(len(journal.acts(n)), 3)
        self.assertTrue(journal.acts(n)[0].startswith("[act:acceptance]"))
        with self.assertRaises(ValueError):
            journal.add_act(n, "bogus", "x")

    def test_budget_override(self):
        self.assertEqual(journal.budget()["sessions"], 30)
        journal.set_budget(sessions=5, parallel_ceiling=3)
        b = journal.budget()
        self.assertEqual((b["sessions"], b["parallel_ceiling"], b["runs"]), (5, 3, 200))

    def test_spend_and_over_budget(self):
        n = journal.create_work_order("o", "T", "b", {})
        journal.spend(n, sessions=30)
        self.assertEqual(journal.over_budget(None), [])
        journal.spend(n, sessions=1, runs=2, tokens=10)
        self.assertEqual(journal.over_budget(None), ["sessions"])
        self.assertEqual(journal.shift_spent(), {"sessions": 31, "runs": 2, "tokens": 10})
        self.assertEqual(journal.kv_get("spent.%s.sessions" % n), "31")

    def test_week_budget_uses_shift_start(self):
        journal.set_budget(sessions=7)
        journal.begin_shift(25.0)
        self.assertEqual(journal.over_budget(35.0), [])
        self.assertEqual(journal.over_budget(35.5), ["week_pp"])
        self.assertEqual(journal.over_budget(None), [])
        self.assertEqual(json.loads(journal.kv_get("shift.budget"))["sessions"], 7)

    def test_begin_shift_resets_spent(self):
        n = journal.create_work_order("o", "T", "b", {})
        journal.spend(n, sessions=3)
        journal.begin_shift(None)
        self.assertEqual(journal.shift_spent(), {"sessions": 0, "runs": 0, "tokens": 0})

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
        n = journal.create_work_order("o", "T", "b", {})
        gate = journal.wait_customer(n, "нужен выбор")
        self.assertTrue(gate)
        self.assertNotIn(n, [i["id"] for i in journal.ready()])
        self.assertEqual(journal.acts(n, "answer"), ["нужен выбор"])
        journal.bd(["gate", "resolve", gate], json_out=False)
        self.assertIn(n, [i["id"] for i in journal.ready()])

    def test_restarts_counter(self):
        n = journal.create_work_order("o", "T", "b", {})
        self.assertEqual(journal.bump_restarts(n), 1)
        self.assertEqual(journal.bump_restarts(n), 2)
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
        journal.kv_set("a.b", "значение с пробелами")
        self.assertEqual(journal.kv_get("a.b"), "значение с пробелами")

    def test_cli_budget_set(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = journal.main(["budget", "--set", "sessions=12", "--set", "runs=50"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual((out["sessions"], out["runs"], out["parallel_ceiling"]), (12, 50, 6))
        self.assertEqual(journal.budget()["sessions"], 12)

    def test_cli_work_order_ready_and_lock(self):
        import io
        from contextlib import redirect_stdout

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = journal.main(list(argv))
            return rc, json.loads(buf.getvalue())
        rc, out = run("work-order", "o", "Заголовок", "--body", "т", "--estimate", "k=v")
        self.assertEqual(rc, 0)
        nid = out["id"]
        self.assertEqual(journal.kv_get("estimate.%s.k" % nid), "v")
        rc, out = run("ready")
        self.assertIn(nid, [i["id"] for i in out])
        self.set_panes("p-a")
        rc, out = run("lock", "p-a")
        self.assertEqual((rc, out), (0, {"acquired": True}))
        rc, out = run("kv", "get", "prorab:active")
        self.assertEqual(out, {"key": "prorab:active", "value": "p-a"})

    # ---- Amendment S: files, estimates, durations, override ----------------

    def test_stage_files_duration_and_actual(self):
        n = journal.create_work_order("o", "T", "b", {})
        s = journal.create_stage(n, "Этап", "b", files=["src/a.py", "docs/b.md"],
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
        mins = journal.accept_stage(s, "принято")
        self.assertGreater(mins, 0)
        self.assertEqual(journal.bd(["show", s])[0]["status"], "closed")
        self.assertEqual(journal.acts(s, "acceptance"), ["принято"])
        self.assertEqual(journal.stage_info(s)["actual_min"], mins)
        self.assertEqual(journal.durations("tests"), [mins])
        self.assertEqual(journal.durations("docs"), [])
        plain = journal.create_stage(n, "Без полей", "b")
        info = journal.stage_info(plain)
        self.assertEqual((info["files"], info["duration_min"], info["kind"]), ([], None, None))

    def test_work_order_files_union(self):
        n = journal.create_work_order("o", "T", "b", {}, goal="g1")
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
        # a goal without наряды does not expire the override
        journal.set_override("g1")
        self.assertIsNotNone(journal.override())
        n1 = journal.create_work_order("o", "T1", "b", {}, goal="g1")
        n2 = journal.create_work_order("o", "T2", "b", {}, goal="g1")
        other = journal.create_work_order("o", "T3", "b", {}, goal="g2")
        s = journal.create_stage(n1, "S", "b")
        self.assertIsNotNone(journal.override())
        # stages inherit the goal label but are not наряды (--no-parent)
        listed = journal.bd(["list", "--label", "goal:g1", "--no-parent", "--all", "--limit", "0"])
        self.assertEqual(sorted(i["id"] for i in listed), sorted([n1, n2]))
        self.assertIsNotNone(journal.override())  # n1 has an open stage, n2 is open
        journal.bd(["close", s])
        journal.bd(["close", n1])
        self.assertIsNotNone(journal.override())  # n2 still open
        journal.wait_customer(n2, "нужен выбор")
        self.assertIsNone(journal.override())  # closed + waiting for the customer
        self.assertIsNone(journal.kv_get("override.ignore_limits"))
        journal.set_override("g2")
        self.assertIsNotNone(journal.override())
        journal.bd(["close", other])
        self.assertIsNone(journal.override())

    def test_override_expires_when_stage_waits(self):
        journal.set_override("g1")
        n = journal.create_work_order("o", "T", "b", {}, goal="g1")
        s1 = journal.create_stage(n, "S1", "b")
        s2 = journal.create_stage(n, "S2", "b", blocked_by=[s1])
        s3 = journal.create_stage(n, "S3", "b")
        journal.bd(["close", s3])
        self.assertIsNotNone(journal.override())
        gate = journal.wait_customer(s1, "нужен выбор")  # on a STAGE, not the наряд
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
        _, out = run("work-order", "o", "T", "--goal", "g1")
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

    def test_graph_open_stages_and_ready_work_orders(self):
        a = journal.create_work_order("o", "A", "b", {})
        b = journal.create_work_order("o", "B", "b", {})
        c = journal.create_work_order("o", "C", "b", {})
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
        # a ready stage makes its наряд issuable; a working crew hides it
        self.assertIn(a, journal.ready_work_orders())
        self.assertNotIn(b, journal.ready_work_orders())  # blocked by a
        journal.register_crew("br-a")
        journal.kv_set("crew.br-a.work_order", a)
        self.assertNotIn(a, journal.ready_work_orders())
        self.assertEqual(journal.working_work_orders(), [a])


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
        journal.set_budget(sessions=5)
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
        self.assertEqual(journal.budget(kv=kv)["sessions"], 5)
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
        self.assertEqual(self.run_cli("notify", "gate-9"), (0, {"push": True}))
        self.assertEqual(self.run_cli("notify", "gate-9"), (0, {"push": False}))
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
