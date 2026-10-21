import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import crew  # noqa: E402
import herdr_cli  # noqa: E402
from prorab.tests.test_herdr_cli import DIALOG, FakeBase, err, ok  # noqa: E402

_ADDR = "t" + "@" + "x.invalid"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": _ADDR,
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": _ADDR}
SITE_DIR = "/home/user/projects/demo-app"


def git(cwd, *args):
    env = dict(os.environ, **GIT_ENV)
    p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    return p.stdout


class FakeJournal:
    def __init__(self, crews=(), ceiling=2):
        self.kv = {}
        self.active = list(crews)
        self.act_log = []
        self.spent = []
        self.files = {}
        self.ceiling = ceiling

    def budget(self):
        return {"parallel_ceiling": self.ceiling}

    def crews(self):
        return list(self.active)

    def register_crew(self, name):
        if name not in self.active:
            self.active.append(name)

    def unregister_crew(self, name):
        self.active = [n for n in self.active if n != name]

    def work_order_files(self, work_order):
        return list(self.files.get(work_order, []))

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.kv[key] = str(value)

    def kv_clear(self, key):
        self.kv.pop(key, None)

    def add_act(self, bead, kind, text):
        self.act_log.append((bead, kind, text))

    def acts(self, bead, kind=None):
        return [t for b, k, t in self.act_log if b == bead and (kind is None or k == kind)]

    def spend(self, work_order, sessions=0, runs=0, tokens=0):
        self.spent.append((work_order, sessions, runs, tokens))


def agent_entry(name="a1", pane="w:p9", cwd="/work", status="idle", session="sess-1"):
    return {"name": name, "pane_id": pane, "cwd": cwd, "agent_status": status,
            "agent_session": {"value": session}}


class CrewBase(FakeBase):
    def setUp(self):
        super().setUp()
        self.j = FakeJournal()
        p = mock.patch.object(crew, "journal", self.j)
        p.start()
        self.addCleanup(p.stop)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        p2 = mock.patch.dict(os.environ, {"HOME": self.home})
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.dict(os.environ, GIT_ENV)
        p3.start()
        self.addCleanup(p3.stop)

    def make_repo(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "f.txt"), "w") as fh:
            fh.write("a\n")
        git(repo, "add", "f.txt")
        git(repo, "commit", "-q", "-m", "base")
        return repo

    def add_worktree(self, repo, work_order, branch=None):
        wt = os.path.join(self.dir, "wt", "prorab-%s" % work_order)
        git(repo, "worktree", "add", "-q", "-b", branch or "worktree-prorab-%s" % work_order, wt)
        return wt

    def commit(self, cwd, fname, content, msg):
        with open(os.path.join(cwd, fname), "w") as fh:
            fh.write(content)
        git(cwd, "add", fname)
        git(cwd, "commit", "-q", "-m", msg)

    def calls_or_empty(self):
        try:
            return self.calls()
        except FileNotFoundError:
            return []

    def write_transcript(self, cwd, session, text, tokens=(10, 20, 30)):
        d = os.path.join(self.home, ".claude", "projects",
                         crew.transcript.project_slug(cwd))
        os.makedirs(d, exist_ok=True)
        entry = {"type": "assistant", "isSidechain": False, "message": {
            "id": "m1", "model": "x", "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": tokens[0], "cache_creation_input_tokens": tokens[1],
                      "cache_read_input_tokens": 999, "output_tokens": tokens[2]}}}
        with open(os.path.join(d, session + ".jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class TestStart(CrewBase):
    def routes_for_start(self, not_ready=False):
        self.route("pane layout --pane w:p1", ok({"layout": {"panes": [
            {"pane_id": "w:p1", "rect": {"width": 200, "height": 40}}]}}))
        self.route("pane split w:p1", ok({"pane": {"pane_id": "w:p9"}}))
        if not_ready:
            self.route("agent start a1", err("agent_not_ready"), exit_code=1)
        else:
            self.route("agent start a1", ok({"agent": agent_entry()}))
        self.route("agent list", ok({"agents": [agent_entry()]}))

    def test_start_refuses_overlapping_files(self):
        self.j.active = ["b0"]
        self.j.kv.update({"crew.b0.work_order": "N0", "crew.b0.site": SITE_DIR})
        self.j.files = {"N0": ["src/a.py"], "N1": ["src/a.py", "src/b.py"]}
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "files_overlap", "with": ["b0"]})
        self.assertEqual(self.calls_or_empty(), [])
        self.assertEqual(self.j.active, ["b0"])

    def test_empty_file_list_overlaps_everything(self):
        self.j.active = ["b0"]
        self.j.kv.update({"crew.b0.work_order": "N0", "crew.b0.site": SITE_DIR})
        self.j.files = {"N0": ["src/a.py"], "N1": []}
        self.assertEqual(crew.start(SITE_DIR, "N1", "a1", "w:p1")["status"], "files_overlap")

    def test_start_allows_disjoint_files_on_same_site(self):
        repo = self.make_repo()
        self.add_worktree(repo, "N1")
        self.j.active = ["b0"]
        self.j.kv.update({"crew.b0.work_order": "N0", "crew.b0.site": repo})
        self.j.files = {"N0": ["src/a.py"], "N1": ["src/b.py"]}
        self.routes_for_start()
        r = crew.start(repo, "N1", "a1", "w:p1")
        self.assertEqual(r["status"], "started")
        self.assertEqual(r["pane_id"], "w:p9")
        self.assertEqual(r["session_id"], "sess-1")
        self.assertIn("a1", self.j.active)
        self.assertIn("pane split w:p1 --direction right --no-focus --cwd %s" % repo,
                      self.calls())
        self.assertIn("agent start a1 --kind claude --pane w:p9 -- -w prorab-N1", self.calls())
        self.assertEqual(self.j.kv["crew.a1.work_order"], "N1")
        self.assertEqual(self.j.act_log[0][:2], ("N1", "answer"))

    def test_start_without_worktree_is_distinct(self):
        repo = self.make_repo()
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        r = crew.start(repo, "N1", "a1", "w:p1")
        self.assertEqual(r["status"], "started_no_worktree")
        self.assertEqual(r["pane_id"], "w:p9")
        self.assertNotIn("crew.a1.worktree", self.j.kv)

    def test_start_needs_consent_for_other_prompts(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_start(not_ready=True)
        self.route("pane read w:p9 --source visible", "New MCP server found in .mcp.json\n")
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_consent", "pane_id": "w:p9"})
        for c in self.calls():
            self.assertNotIn("send", c)
            self.assertNotIn("prompt", c)

    def test_split_down_when_pane_is_narrow(self):
        repo = self.make_repo()
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.route("pane layout --pane w:p1", ok({"layout": {"panes": [
            {"pane_id": "w:p1", "rect": {"width": 80, "height": 45}}]}}))
        crew.start(repo, "N1", "a1", "w:p1")
        self.assertIn("pane split w:p1 --direction down --no-focus --cwd %s" % repo,
                      self.calls())

    def test_start_parallel_limit(self):
        self.j.active = ["b0", "b1"]
        self.assertEqual(crew.start(SITE_DIR, "N1", "a1", "w:p1"), {"status": "parallel_limit"})
        self.assertEqual(self.calls_or_empty(), [])

    def test_start_needs_trust_sends_no_keys(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_start(not_ready=True)
        self.route("pane read w:p9 --source visible",
                   "Do you trust the files in this folder?\n1. Yes, I trust this folder\n")
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_trust", "pane_id": "w:p9"})
        for c in self.calls():
            self.assertNotIn("send-keys", c)
            self.assertNotIn("prompt", c)
            self.assertNotIn("send-text", c)
        self.assertEqual(self.j.active, [])

    def test_start_renames_branch(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        r = crew.start(repo, "N1", "a1", "w:p1")
        self.assertEqual(r["branch"], "prorab/N1")
        self.assertEqual(os.path.realpath(r["worktree"]), os.path.realpath(wt))
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip(), "prorab/N1")


class TestSendState(CrewBase):
    def test_send_work_order_retries_once_on_stall(self):
        with mock.patch.object(crew.herdr_cli, "send", side_effect=["stalled", "accepted"]) as m:
            self.assertEqual(crew.send_work_order("a1", "t", retry_delay=0), "accepted")
            self.assertEqual(m.call_count, 2)
        with mock.patch.object(crew.herdr_cli, "send", return_value="stalled") as m:
            self.assertEqual(crew.send_work_order("a1", "t", retry_delay=0), "stalled")
            self.assertEqual(m.call_count, 2)

    def test_state_and_summary(self):
        self.write_transcript("/work", "sess-1", "готово")
        self.route("agent list", ok({"agents": [agent_entry(status="working")]}))
        self.route("agent read a1 --source visible", "text · 2 shells\n")
        st = crew.state("a1")
        self.assertEqual(st["status"], "working")
        self.assertEqual(st["bg_shells"], 2)
        self.assertEqual(st["session_id"], "sess-1")
        self.assertEqual(st["cwd"], "/work")
        self.assertEqual(st["ctx"]["tokens"], 10 + 20 + 999 + 30)
        self.assertEqual(crew.summary("a1"), "готово")

    def test_state_gone(self):
        self.route("agent list", ok({"agents": []}))
        self.assertEqual(crew.state("a1")["status"], "gone")

    def test_pause_and_handover_request_use_acts_md(self):
        self.route("agent prompt a1", ok({}))
        crew.pause("a1")
        crew.request_handover("a1")
        calls = self.calls()
        self.assertIn("жди сообщения прораба", calls[0])
        self.assertIn("HANDOVER READY", calls[1])
        self.assertIn("следующие шаги", calls[1])


class TestHandover(CrewBase):
    def setUp(self):
        super().setUp()
        self.wt = "/home/user/projects/demo-app/.claude/worktrees/prorab-N1"
        self.j.kv["crew.a1.worktree"] = self.wt
        self.write_transcript("/work", "sess-1", "Акт: сделано A\nHANDOVER READY")
        self.route("agent read a1 --source visible", "idle\n")

    def test_complete_handover_refuses_with_bg_shells(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent read a1 --source visible", "bar · 1 shell\n")
        r = crew.complete_handover("a1", "N1", "наряд", timeout_s=1, poll_s=0.01)
        self.assertEqual(r["status"], "bg_shells")
        self.assertEqual(self.j.act_log, [])
        for c in self.calls():
            self.assertFalse(c.startswith("agent prompt"))

    def test_complete_handover_sequence(self):
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        pane_info = {"pane": {"pane_id": "w:p9", "foreground_cwd": "/home/user/projects/demo-app"}}
        inwt = {"pane": {"pane_id": "w:p9", "foreground_cwd": self.wt}}
        self.route_seq("pane get w:p9", [ok(pane_info), ok(pane_info), ok(inwt)])
        self.route("pane run w:p9", ok({}))
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({}))
        r = crew.complete_handover("a1", "N1", "Текст наряда", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "restarted")
        calls = self.calls()
        i_exit = calls.index("agent prompt a1 /exit --wait --timeout 15000")
        i_cd = calls.index("pane run w:p9 cd %s" % self.wt)
        i_start = calls.index("agent start a1 --kind claude --pane w:p9")
        i_prompt = next(i for i, c in enumerate(calls)
                        if c.startswith("agent prompt a1 Текст наряда"))
        self.assertTrue(i_exit < i_cd < i_start < i_prompt)
        with open(os.path.join(self.dir, "calls.log"), encoding="utf-8") as fh:
            tail = fh.read().split("agent prompt a1 Текст наряда", 1)[1]
        self.assertIn("Акт: сделано A", tail)
        self.assertNotIn("HANDOVER READY", tail)
        self.assertEqual(self.j.act_log, [("N1", "handover", "Акт: сделано A")])
        self.assertEqual(self.j.spent, [("N1", 1, 0, 60)])

    def test_restart_never_falls_back_to_site_dir(self):
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        pane_info = {"pane": {"pane_id": "w:p9", "foreground_cwd": "/home/user/projects/demo-app"}}
        self.route("pane get w:p9", ok(pane_info))
        self.route("pane run w:p9", ok({}))
        self.route("agent prompt a1", ok({}))
        r = crew.complete_handover("a1", "N1", "t", timeout_s=0.1, poll_s=0.01)
        self.assertEqual(r["status"], "restart_failed")
        for c in self.calls():
            self.assertFalse(c.startswith("agent start"))
        self.assertEqual(self.j.spent, [("N1", 1, 0, 60)])

    def restart_routes(self, wt=None):
        wt = wt or self.wt
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        pane_info = {"pane": {"pane_id": "w:p9", "foreground_cwd": "/home/user/projects/demo-app"}}
        inwt = {"pane": {"pane_id": "w:p9", "foreground_cwd": wt}}
        self.route_seq("pane get w:p9", [ok(pane_info), ok(pane_info), ok(inwt)])
        self.route("pane run w:p9", ok({}))

    def go(self):
        return crew.complete_handover("a1", "N1", "t", timeout_s=2, poll_s=0.01)

    def test_act_missing_sends_nothing(self):
        self.write_transcript("/work", "sess-1", "ещё работаю")
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.assertEqual(self.go(), {"status": "act_missing"})
        self.assertEqual(self.j.act_log, [])
        self.assertEqual(self.j.spent, [])
        for c in self.calls():
            self.assertFalse(c.startswith(("agent prompt", "pane run")))

    def test_unknown_worktree_never_restarts_in_cwd(self):
        del self.j.kv["crew.a1.worktree"]
        self.route("agent list", ok({"agents": [agent_entry()]}))
        r = self.go()
        self.assertEqual(r["status"], "restart_failed")
        for c in self.calls():
            self.assertFalse(c.startswith(("agent prompt", "pane run", "agent start")))

    def test_tokens_spent_before_exit_even_if_restart_fails(self):
        self.restart_routes()
        self.route("agent start a1", err("agent_start_failed"), exit_code=1)
        self.route("agent prompt a1", ok({}))
        self.assertEqual(self.go()["status"], "restart_failed")
        self.assertEqual(self.j.spent, [("N1", 1, 0, 60)])

    def test_restart_consent_prompt_sends_nothing(self):
        self.restart_routes()
        self.route("agent start a1", err("agent_not_ready"), exit_code=1)
        self.route("pane read w:p9 --source visible", "Do you trust the files here?\nYes, I trust this folder\n")
        self.route("agent prompt a1", ok({}))
        self.assertEqual(self.go()["status"], "needs_trust")
        calls = self.calls()
        self.assertEqual([c for c in calls if c.startswith(("agent prompt", "agent send-keys",
                                                            "pane send"))],
                         ["agent prompt a1 /exit --wait --timeout 15000"])

    def test_prompt_not_accepted_is_not_restarted(self):
        self.restart_routes()
        self.route("agent start a1", ok({}))
        with mock.patch.object(crew.herdr_cli, "send",
                               side_effect=[None, "blocked"]):
            self.assertEqual(self.go()["status"], "prompt_not_accepted")

    def test_cd_command_is_shell_quoted(self):
        wt = "/home/user/it's a/prorab-N1"
        self.j.kv["crew.a1.worktree"] = wt
        self.restart_routes(wt)
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({}))
        self.go()
        import shlex
        cd = [c for c in self.calls() if c.startswith("pane run w:p9 ")][0]
        self.assertEqual(shlex.split(cd[len("pane run w:p9 "):]), ["cd", wt])


class TestGit(CrewBase):
    def test_evidence_counts_commits(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        self.commit(wt, "h.txt", "2\n", "two")
        with open(os.path.join(wt, "dirty.txt"), "w") as fh:
            fh.write("x")
        ev = crew.evidence(wt)
        self.assertEqual(ev["branch"], "prorab/N1")
        self.assertEqual(len(ev["commits"]), 2)
        self.assertIn("g.txt", ev["diffstat"])
        self.assertTrue(ev["dirty"])

    def test_cleanup_keeps_unmerged(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        self.assertEqual(crew.cleanup(repo, "N1"), {"status": "kept", "reason": "unmerged"})
        self.assertTrue(os.path.isdir(wt))

    def test_cleanup_removes_merged(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.assertEqual(crew.cleanup(repo, "N1")["status"], "removed")
        self.assertFalse(os.path.exists(wt))

    def test_cleanup_drop_decision_removes_unmerged(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        self.j.add_act("N1", "answer", crew.DROP_MARKER + " (слова заказчика)")
        self.assertEqual(crew.cleanup(repo, "N1")["status"], "removed")
        self.assertFalse(os.path.exists(wt))

    def test_cleanup_drop_needs_the_exact_marker(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        for text in ("drop the unused column", "dropdown меню", "решение: drop"):
            self.j.add_act("N1", "answer", text)
        self.assertEqual(crew.cleanup(repo, "N1"), {"status": "kept", "reason": "unmerged"})
        self.assertTrue(os.path.isdir(wt))

    def test_drop_marker_matches_acts_md(self):
        self.assertEqual(crew._template("Customer decision to drop the branch"),
                         crew.DROP_MARKER)

    def test_merge_check_reports_missing_and_conflict(self):
        repo = self.make_repo()
        git(repo, "branch", "acc-a")
        git(repo, "branch", "acc-b")
        git(repo, "branch", "acc-c")
        wt_a = self.add_worktree(repo, "NA", "acc-a-wt")
        # acc-a changes f.txt; acc-b changes it differently; acc-c adds a new file
        git(repo, "checkout", "-q", "acc-a")
        self.commit(repo, "f.txt", "A\n", "a")
        git(repo, "checkout", "-q", "acc-b")
        self.commit(repo, "f.txt", "B\n", "b")
        git(repo, "checkout", "-q", "acc-c")
        self.commit(repo, "new.txt", "c\n", "c")
        git(repo, "checkout", "-q", "main")
        wt = self.add_worktree(repo, "N2", "prorab/N2")
        self.commit(wt, "f.txt", "W\n", "w")
        r = crew.merge_check(wt, ["main", "acc-a", "acc-c"])
        self.assertEqual(r["missing"], ["acc-a", "acc-c"])
        self.assertEqual(r["conflicts"], ["acc-a"])
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip(), "prorab/N2")
        self.assertEqual(git(wt, "status", "--porcelain").strip(), "")
        self.assertTrue(wt_a)


class TestAnswer(CrewBase):
    def setUp(self):
        super().setUp()
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent list", ok({"agents": [agent_entry(status="working")]}))
        self.route("agent send-keys a1", ok({}))

    def test_answer_choice_dialog_keys(self):
        self.route("agent read a1 --source visible", DIALOG)
        self.assertEqual(crew.answer("a1", choice=2), "working")
        self.assertIn("agent send-keys a1 down enter", self.calls())
        self.assertEqual(self.j.act_log[0][:2], ("N1", "answer"))

    def test_answer_moves_up_from_highlighted_option(self):
        shifted = DIALOG.replace("❯ 1.", "  1.").replace("  3. Вариант C", "❯ 3. Вариант C")
        self.route("agent read a1 --source visible", shifted)
        crew.answer("a1", choice=1)
        self.assertIn("agent send-keys a1 up up enter", self.calls())

    def test_answer_ignores_numbered_lines_above_dialog(self):
        above = "Итоги:\n  1. первый\n  2. второй\n\n"
        self.route("agent read a1 --source visible", above + DIALOG)
        crew.answer("a1", choice=3)
        self.assertIn("agent send-keys a1 down down enter", self.calls())

    def test_answer_text_selects_type_something(self):
        self.route("agent read a1 --source visible", DIALOG)
        self.route("pane send-text w:p9", ok({}))
        crew.answer("a1", text="свой ответ")
        calls = self.calls()
        i_nav = calls.index("agent send-keys a1 down down down")
        i_txt = calls.index("pane send-text w:p9 свой ответ")
        i_enter = calls.index("agent send-keys a1 enter")
        self.assertTrue(i_nav < i_txt < i_enter)

    def test_answer_without_dialog_sends_text(self):
        self.route("agent read a1 --source visible", "just a prompt\n")
        self.route("agent prompt a1", ok({}))
        crew.answer("a1", text="продолжай")
        self.assertIn("agent prompt a1 продолжай --wait --timeout 15000", self.calls())

    def test_answer_refuses_consent_prompt(self):
        for screen in ("Do you trust the files in this folder?\n1. Yes, I trust this folder\n",
                       "New MCP server found in .mcp.json\n1. Use this MCP server\n",
                       "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
                       "Enter to select · ↑/↓ to navigate · Esc to cancel\n"):
            self.route("agent read a1 --source visible", screen)
            self.assertEqual(crew.answer("a1", choice=1), "consent")
            self.assertEqual(crew.answer("a1", text="yes"), "consent")
        for c in self.calls():
            self.assertFalse(c.startswith(("agent send-keys", "agent prompt", "pane send-text")))
        self.assertEqual(self.j.act_log, [])


class TestStop(CrewBase):
    def test_stop_exits_unregisters_and_spends(self):
        self.write_transcript("/work", "sess-1", "x", tokens=(1, 2, 3))
        self.j.active = ["a1"]
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent prompt a1", err("agent_not_found"), exit_code=1)
        crew.stop("a1", "N1")
        self.assertEqual(self.j.active, [])
        self.assertEqual(self.j.spent, [("N1", 0, 0, 6)])
        self.assertIn("agent prompt a1 /exit --wait --timeout 15000", self.calls())
        self.assertNotIn("crew.a1.work_order", self.j.kv)


if __name__ == "__main__":
    unittest.main()
