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

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")

DIALOG = """\
────────────────────────────────────────────────────────────────────────────────
 ☐ Choice

Which option should we use?

❯ 1. Option A (Recommended)
     Description of option A
  2. Option B
     Description of option B
  3. Option C
     Description of option C
  4. Type something.
────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def fixture(*parts):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as fh:
        return fh.read()


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
        self._n = getattr(self, "_n", 0) + 1
        name = name or "r%d.txt" % self._n
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
            "Option A (Recommended)", "Option B", "Option C",
            "Type something.", "Chat about this"])

    def test_choice_dialog_cyrillic_labels(self):
        # crews work on sites whose screens may hold Russian: non-ASCII labels must parse
        self.assertEqual(herdr_cli.choice_dialog(fixture("choice_dialog_cyrillic.txt")), [
            "\u0412\u0430\u0440\u0438\u0430\u043d\u0442 A (Recommended)", "\u0412\u0430\u0440\u0438\u0430\u043d\u0442 B", "\u0412\u0430\u0440\u0438\u0430\u043d\u0442 C",
            "Type something.", "Chat about this"])

    def test_choice_dialog_ignores_numbered_lines_above(self):
        above = "Summary:\n  1. first item\n  2. second item\n\n"
        st = herdr_cli.choice_state(above + DIALOG.replace("❯ 1.", "  1.")
                                    .replace("  2. Option B", "❯ 2. Option B"))
        self.assertEqual(st[0][0], "Option A (Recommended)")
        self.assertEqual(len(st[0]), 5)
        self.assertEqual(st[1], 1)

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
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "accepted")
        self.assertIn("agent prompt a1 hi --wait --until working --until done "
                      "--until blocked --timeout 60000", self.calls())

    def test_send_timeout_but_working_is_accepted(self):
        self.route("agent prompt a1", err("timeout"), exit_code=1)
        self.route("agent list", ok({"agents": [{"name": "a1", "agent_status": "working"}]}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "accepted")

    def test_send_timeout_not_working_is_unconfirmed(self):
        self.route("agent prompt a1", err("timeout"), exit_code=1)
        self.route("agent list", ok({"agents": [{"name": "a1", "agent_status": "idle"}]}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "unconfirmed")
        self.assertEqual([c for c in self.calls() if "prompt" in c or "send-keys" in c],
                         [c for c in self.calls() if c.startswith("agent prompt")])
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent prompt")]), 1)

    def test_send_refuses_multiline(self):
        for text in ("one\ntwo", "line\n"):
            with self.assertRaises(ValueError):
                herdr_cli.send("a1", text)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "calls.log")))

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


class TestSendWait(FakeBase):
    def test_send_waits_until_working_done_blocked(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        herdr_cli.send("a1", "hi", timeout_ms=60000)
        line = self.calls()[0]
        for until in ("--until working", "--until done", "--until blocked", "--timeout 60000"):
            self.assertIn(until, line)
        self.assertNotIn("--until idle", line)

    def test_send_success_with_blocked_status(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "blocked"}}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "blocked")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "done"}}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "accepted")

    def test_send_without_status_falls_back_to_agent_get(self):
        self.route("agent prompt a1", ok({}))
        self.route("agent get a1", ok({"agent": {"agent_status": "blocked"}}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "blocked")
        self.route("agent get a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "accepted")
        self.route("agent get a1", ok({"agent": {"agent_status": "idle"}}))
        self.assertEqual(herdr_cli.send("a1", "hi"), "unconfirmed")

    def test_status_in_known_statuses_only(self):
        self.assertIsNone(herdr_cli._status_in({"result": {"status": "ok"}}))
        self.assertEqual(herdr_cli._status_in({"result": {"agent": {"agent_status": "done"}}}),
                         "done")

    def test_wait_ready_until_idle_or_done(self):
        self.route("agent wait a1", ok({"agent": {"agent_status": "done"}}))
        self.assertEqual(herdr_cli.wait_ready("a1"), "done")
        self.assertIn("agent wait a1 --until idle --until done --timeout 60000", self.calls())
        self.route("agent wait a1", err("timeout"), exit_code=1)
        self.assertEqual(herdr_cli.wait_ready("a1"), "timeout")


class TestComposer(unittest.TestCase):
    def test_composer_text_from_fixtures(self):
        self.assertTrue(fixture("composer", "ours.txt") and
                        herdr_cli.composer_text(fixture("composer", "ours.txt")).startswith(
                            "Read the work order in"))
        self.assertEqual(herdr_cli.composer_text(fixture("composer", "empty.txt")), "")
        self.assertIsNone(herdr_cli.composer_text("● just output\nmore output\n"))

    def test_composer_text_legacy_marker(self):
        self.assertTrue(herdr_cli.composer_text(fixture("composer", "legacy_marker.txt"))
                        .startswith("Read the work order"))

    def test_composer_text_needs_rules_around(self):
        rule = "─" * 40
        self.assertEqual(herdr_cli.composer_text("out\n%s\n❯\n%s\nstatus\n" % (rule, rule)), "")
        self.assertEqual(herdr_cli.composer_text("out\n%s\n❯ hi there  \n%s\n" % (rule, rule)),
                         "hi there")
        self.assertIsNone(herdr_cli.composer_text("out\n❯ hi\nstatus\n"))
        self.assertIsNone(herdr_cli.composer_text("out\n> hi\n%s\n" % rule))

    def test_composer_text_after_no_break_space(self):
        # Claude Code 2.1.307 puts U+00A0, not a space, after the marker
        rule = "─" * 40
        self.assertEqual(herdr_cli.composer_text(fixture("composer", "suggestion.txt")),
                         "run the tests again")
        self.assertEqual(herdr_cli.composer_text("out\n%s\n❯\xa0hi there\n%s\n" % (rule, rule)),
                         "hi there")
        self.assertEqual(herdr_cli.composer_text("out\n%s\n❯\xa0\n%s\n" % (rule, rule)), "")
        self.assertEqual(herdr_cli.composer_text("out\n%s\n>\xa0hi\n%s\n" % (rule, rule)), "hi")

    def test_choice_dialog_line_is_not_a_composer(self):
        self.assertIsNone(herdr_cli.composer_text(DIALOG))
        rule = "─" * 40
        self.assertIsNone(herdr_cli.composer_text(
            "%s\n❯ 1. Yes\n%s\n  2. No\n" % (rule, rule)))
        self.assertIsNone(herdr_cli.composer_text(
            "%s\n❯\xa01. Yes\n%s\n  2. No\n" % (rule, rule)))


class TestRecoverStalled(FakeBase):
    TEXT = "Read the work order in /home/user/.cache/prorab/work-orders/N1.md and start"

    def setup_agent(self, status, screen):
        self.route("agent get a1", ok({"agent": {"name": "a1", "agent_status": status}}))
        self.route("agent read a1", fixture("composer", screen))
        self.route("agent send-keys a1 enter", ok({}))
        self.route("agent wait a1", ok({"agent": {"agent_status": "working"}}))

    def keys(self):
        return [c for c in self.calls() if "send-keys" in c]

    def test_recover_stalled_idle_or_done_with_our_text_one_enter(self):
        for status in ("idle", "done"):
            self.setup_agent(status, "ours.txt")
            open(os.path.join(self.dir, "calls.log"), "w").close()
            self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "accepted")
            self.assertEqual(self.keys(), ["agent send-keys a1 enter"])
            self.assertFalse([c for c in self.calls() if c.startswith("agent prompt")])
            self.assertIn("agent wait a1 --until working --until blocked "
                          "--timeout 60000", self.calls())

    def test_recover_stalled_wait_outcomes(self):
        self.setup_agent("idle", "ours.txt")
        self.route("agent wait a1", err("timeout"), exit_code=1)
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "unconfirmed")
        self.route("agent wait a1", ok({"agent": {"agent_status": "blocked"}}))
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "blocked")

    def test_recover_stalled_swallowed_enter_is_never_accepted(self):
        # the wait matches the CURRENT state: the pane is still done, so done is no evidence
        self.setup_agent("done", "ours.txt")
        self.route("agent wait a1", ok({"agent": {"agent_status": "done"}}))
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "unconfirmed")
        self.assertEqual(self.keys(), ["agent send-keys a1 enter"])

    def test_recover_stalled_without_status_falls_back_to_agent_get(self):
        self.setup_agent("done", "ours.txt")
        self.route("agent wait a1", ok({}))
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "unconfirmed")

    def test_recover_stalled_empty_composer_presses_nothing(self):
        self.setup_agent("done", "empty.txt")
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "stalled")
        self.assertEqual(self.keys(), [])

    def test_recover_stalled_no_break_space_composer(self):
        # the fixtures carry the real U+00A0 rendering: ours is found, a suggestion is not
        self.setup_agent("done", "ours.txt")
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "accepted")
        self.assertEqual(self.keys(), ["agent send-keys a1 enter"])
        open(os.path.join(self.dir, "calls.log"), "w").close()
        self.setup_agent("done", "suggestion.txt")
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "stalled")
        self.assertEqual(self.keys(), [])

    def test_recover_stalled_suggestion_presses_nothing(self):
        self.setup_agent("done", "suggestion.txt")
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "stalled")
        self.assertEqual(self.keys(), [])

    def test_recover_stalled_no_composer_presses_nothing(self):
        self.setup_agent("idle", "ours.txt")
        self.route("agent read a1", "● plain output, no input line\n")
        self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "stalled")
        self.assertEqual(self.keys(), [])

    def test_recover_stalled_other_status_presses_nothing(self):
        for status in ("working", "blocked"):
            self.setup_agent(status, "ours.txt")
            self.assertEqual(herdr_cli.recover_stalled("a1", self.TEXT), "stalled")
            self.assertEqual(self.keys(), [])


class TestConsent(unittest.TestCase):
    def test_consent_kinds_from_fixtures(self):
        for kind in ("trust", "mcp", "claude_md_imports", "bypass_warning", "api_key",
                     "tool_permission"):
            self.assertEqual(herdr_cli.consent_dialog(fixture("consent", kind + ".txt")),
                             kind, kind)
            self.assertIn(kind, herdr_cli.CONSENT_NAMES)

    def test_worktree_trust_refusal_is_a_trust_sign(self):
        self.assertEqual(herdr_cli.consent_dialog(
            fixture("consent", "worktree_trust_refused.txt")), "trust")

    def test_consent_only_in_bottom_block(self):
        self.assertIsNone(herdr_cli.consent_dialog(fixture("consent", "marker_in_scrollback.txt")))
        self.assertEqual(herdr_cli.consent_dialog(
            fixture("consent", "marker_in_scrollback.txt"), tail_lines=40), "tool_permission")

    def test_bypass_status_line_is_not_consent(self):
        self.assertIsNone(herdr_cli.consent_dialog("output\nbypass permissions on · 1 shell\n"))

    def test_tool_permission_is_not_a_choice_dialog(self):
        screen = DIALOG + "\nDo you want to proceed?\n"
        self.assertIsNotNone(herdr_cli.choice_dialog(DIALOG))
        self.assertIsNone(herdr_cli.choice_dialog(screen))
        self.assertIsNone(herdr_cli.choice_state(screen))


class TestNames(FakeBase):
    def test_valid_name(self):
        self.assertTrue(herdr_cli.valid_name("demo-app-1"))
        for bad in ("Demo-1", "1demo", "a" * 33, "", "-x"):
            self.assertFalse(herdr_cli.valid_name(bad), bad)
        self.assertTrue(herdr_cli.valid_name("a" * 32))

    def test_bottom_block(self):
        self.assertEqual(herdr_cli.bottom_block("a\n\nb\n  \nc\n", 2), "b\nc")

    def test_agent_by_pane(self):
        lst = [{"name": "a1", "pane_id": "w:p1"}, {"pane_id": "w:p2"}]
        self.assertEqual(herdr_cli.agent_by_pane("w:p2", lst), {"pane_id": "w:p2"})
        self.assertIsNone(herdr_cli.agent_by_pane("w:p3", lst))
        self.route("agent list", ok({"agents": lst}))
        self.assertEqual(herdr_cli.agent_by_pane("w:p1")["name"], "a1")


if __name__ == "__main__":
    unittest.main()
