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

    def test_naryad_and_stage_ready_order(self):
        n = journal.create_naryad("obj1", "Наряд", "Тело", {"допуск": "да", "n": 3})
        self.assertEqual(journal.kv_get("smeta.%s.допуск" % n), "да")
        self.assertEqual(journal.kv_get("smeta.%s.n" % n), "3")
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
        n = journal.create_naryad("o", "T", "b", {})
        other = journal.bd(["create", "alien", "-d", "x", "--silent"], json_out=False).strip()
        ids = [i["id"] for i in journal.ready()]
        self.assertIn(n, ids)
        self.assertNotIn(other, ids)

    def test_acts_roundtrip(self):
        n = journal.create_naryad("o", "T", "b", {})
        journal.add_act(n, "приёмка", "принято")
        journal.add_act(n, "смена", "смена 1")
        journal.add_act(n, "приёмка", "принято снова")
        self.assertEqual(journal.acts(n, "приёмка"), ["принято", "принято снова"])
        self.assertEqual(journal.acts(n, "смена"), ["смена 1"])
        self.assertEqual(len(journal.acts(n)), 3)
        self.assertTrue(journal.acts(n)[0].startswith("[акт:приёмка]"))
        with self.assertRaises(ValueError):
            journal.add_act(n, "bogus", "x")

    def test_budget_override(self):
        self.assertEqual(journal.budget()["sessions"], 30)
        journal.set_budget(sessions=5, parallel_ceiling=3)
        b = journal.budget()
        self.assertEqual((b["sessions"], b["parallel_ceiling"], b["runs"]), (5, 3, 200))

    def test_spend_and_over_budget(self):
        n = journal.create_naryad("o", "T", "b", {})
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
        n = journal.create_naryad("o", "T", "b", {})
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
        n = journal.create_naryad("o", "T", "b", {})
        gate = journal.wait_customer(n, "нужен выбор")
        self.assertTrue(gate)
        self.assertNotIn(n, [i["id"] for i in journal.ready()])
        self.assertEqual(journal.acts(n, "ответ"), ["нужен выбор"])
        journal.bd(["gate", "resolve", gate], json_out=False)
        self.assertIn(n, [i["id"] for i in journal.ready()])

    def test_restarts_counter(self):
        n = journal.create_naryad("o", "T", "b", {})
        self.assertEqual(journal.bump_restarts(n), 1)
        self.assertEqual(journal.bump_restarts(n), 2)
        self.assertEqual(journal.kv_get("restarts.%s" % n), "2")

    def test_brigades_registry(self):
        self.assertEqual(journal.brigades(), [])
        journal.register_brigade("b1")
        journal.register_brigade("b2")
        journal.register_brigade("b1")
        self.assertEqual(journal.brigades(), ["b1", "b2"])
        journal.unregister_brigade("b1")
        self.assertEqual(journal.brigades(), ["b2"])
        journal.unregister_brigade("b2")
        self.assertEqual(journal.brigades(), [])

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

    def test_cli_naryad_ready_and_lock(self):
        import io
        from contextlib import redirect_stdout

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = journal.main(list(argv))
            return rc, json.loads(buf.getvalue())
        rc, out = run("naryad", "o", "Заголовок", "--body", "т", "--smeta", "k=v")
        self.assertEqual(rc, 0)
        nid = out["id"]
        self.assertEqual(journal.kv_get("smeta.%s.k" % nid), "v")
        rc, out = run("ready")
        self.assertIn(nid, [i["id"] for i in out])
        self.set_panes("p-a")
        rc, out = run("lock", "p-a")
        self.assertEqual((rc, out), (0, {"acquired": True}))
        rc, out = run("kv", "get", "prorab:active")
        self.assertEqual(out, {"key": "prorab:active", "value": "p-a"})


if __name__ == "__main__":
    unittest.main()
