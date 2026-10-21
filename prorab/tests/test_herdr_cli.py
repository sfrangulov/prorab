import json
import os
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import herdr_cli  # noqa: E402

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")

DIALOG = """\
────────────────────────────────────────────────────────────────────────────────
 ☐ Выбор

Какой вариант использовать?

❯ 1. Вариант A (Recommended)
     Описание варианта A
  2. Вариант B
     Описание варианта B
  3. Вариант C
     Описание варианта C
  4. Type something.
────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def err(code):
    return json.dumps({"error": {"code": code, "message": code}, "id": "cli:x"})


def ok(result):
    return json.dumps({"id": "cli:x", "result": result})


class FakeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_HERDR_DIR", "HERDR_ENV")}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ["PATH"] = FAKES + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_HERDR_DIR"] = self.dir
        os.environ["HERDR_ENV"] = "1"
        self.routes = {}

    def route(self, prefix, body, exit_code=0, name=None):
        name = name or "r%d.txt" % len(self.routes)
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            fh.write(body)
        self.routes[prefix] = {"file": name, "exit": exit_code}
        self._save()

    def route_seq(self, prefix, bodies, exit_code=0):
        names = []
        for i, b in enumerate(bodies):
            n = "s%d_%d.txt" % (len(self.routes), i)
            with open(os.path.join(self.dir, n), "w", encoding="utf-8") as fh:
                fh.write(b)
            names.append(n)
        self.routes[prefix] = {"seq": names, "exit": exit_code}
        self._save()

    def _save(self):
        with open(os.path.join(self.dir, "routes.json"), "w") as fh:
            json.dump(self.routes, fh)

    def calls(self):
        with open(os.path.join(self.dir, "calls.log"), encoding="utf-8") as fh:
            return fh.read().splitlines()


class TestEnv(FakeBase):
    def test_check_env_requires_herdr_env(self):
        self.route("--version", "herdr 0.9.5\n")
        os.environ.pop("HERDR_ENV")
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            herdr_cli.check_env()
        self.assertEqual(cm.exception.code, "not_in_herdr")
        os.environ["HERDR_ENV"] = "1"
        herdr_cli.check_env()

    def test_version_floor(self):
        self.route("--version", "herdr 0.9.4\n")
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            herdr_cli.check_env()
        self.assertEqual(cm.exception.code, "herdr_too_old")
        self.route("--version", "herdr 0.9.5\n")
        herdr_cli.check_env()
        self.route("--version", "herdr 0.10.0\n")
        herdr_cli.check_env()


class TestParsing(unittest.TestCase):
    def test_bg_shells(self):
        self.assertEqual(herdr_cli.bg_shells("bypass permissions on · 1 shell"), 1)
        self.assertEqual(herdr_cli.bg_shells("1 shell, 1 monitor still running"), 1)
        self.assertEqual(herdr_cli.bg_shells("3 shells still running"), 3)
        self.assertEqual(herdr_cli.bg_shells("nothing here"), 0)

    def test_choice_dialog_parses_options(self):
        self.assertEqual(herdr_cli.choice_dialog(DIALOG), [
            "Вариант A (Recommended)", "Вариант B", "Вариант C",
            "Type something.", "Chat about this"])

    def test_choice_dialog_none_on_plain_screen(self):
        self.assertIsNone(herdr_cli.choice_dialog("1. a numbered list\n2. in prose\n"))


class TestRun(FakeBase):
    def test_agents_and_agent(self):
        self.route("agent list", ok({"agents": [
            {"name": "a1", "pane_id": "w:p1", "agent_status": "idle"},
            {"pane_id": "w:p2", "agent_status": "idle"}]}))
        self.assertEqual(len(herdr_cli.agents()), 2)
        self.assertEqual(herdr_cli.agent("a1")["pane_id"], "w:p1")
        self.assertIsNone(herdr_cli.agent("zz"))

    def test_visible_is_plain_text(self):
        self.route("pane read w:p1 --source visible", "plain text\nfooter\n")
        self.route("agent read a1 --source visible", "agent text\n")
        self.assertIn("plain text", herdr_cli.pane_visible("w:p1"))
        self.assertIn("agent text", herdr_cli.agent_visible("a1"))

    def test_send_keys(self):
        self.route("agent send-keys a1 down enter", ok({}))
        herdr_cli.send_keys("a1", ["down", "enter"])
        self.assertIn("agent send-keys a1 down enter", self.calls())

    def test_non_json_error_is_cli_error(self):
        self.route("agent list", "syntax error: bad flag", exit_code=2)
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            herdr_cli.run(["agent", "list"])
        self.assertEqual(cm.exception.code, "cli_error")
        self.assertIn("syntax error", str(cm.exception))

    def test_seq_route(self):
        self.route_seq("agent list", [ok({"agents": [{"name": "a1"}]}), ok({"agents": []})])
        self.assertEqual(len(herdr_cli.agents()), 1)
        self.assertEqual(len(herdr_cli.agents()), 0)
        self.assertEqual(len(herdr_cli.agents()), 0)


class TestSend(FakeBase):
    def test_send_ok(self):
        self.route("agent prompt a1", ok({}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "accepted")
        self.assertIn("agent prompt a1 hi --wait --timeout 15000", self.calls())

    def test_send_timeout_but_working_is_accepted(self):
        self.route("agent prompt a1", err("timeout"), exit_code=1)
        self.route("agent list", ok({"agents": [{"name": "a1", "agent_status": "working"}]}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "accepted")

    def test_send_timeout_not_working_raises(self):
        self.route("agent prompt a1", err("timeout"), exit_code=1)
        self.route("agent list", ok({"agents": [{"name": "a1", "agent_status": "idle"}]}))
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            herdr_cli.send("a1", "hi")
        self.assertEqual(cm.exception.code, "timeout")

    def test_send_stalled(self):
        self.route("agent prompt a1", err("agent_prompt_stalled"), exit_code=1)
        self.assertEqual(herdr_cli.send("a1", "hi"), "stalled")

    def test_send_blocked(self):
        self.route("agent prompt a1", err("agent_blocked"), exit_code=1)
        self.assertEqual(herdr_cli.send("a1", "hi"), "blocked")

    def test_send_other_error_raises(self):
        self.route("agent prompt a1", err("agent_not_found"), exit_code=1)
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            herdr_cli.send("a1", "hi")
        self.assertEqual(cm.exception.code, "agent_not_found")


if __name__ == "__main__":
    unittest.main()
