import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _defaults  # noqa: E402
import crew  # noqa: E402
import journal as real_journal  # noqa: E402
import herdr_cli  # noqa: E402
from prorab.tests.test_herdr_cli import DIALOG, FakeBase, err, fixture, ok  # noqa: E402

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
        self.restarts = {}
        self.iters = {}
        self.gates = []
        self.stages = {}
        self.estimates = {}
        self.set_spent_log = []
        self.accepted = []
        self.closed_work_orders = []
        self.open_gates = set()
        self.close_fails = False
        self.already_closed = False
        self.closed_stages = {}
        self.shift = {"usd": 0.0, "work_orders": {}, "research": {}, "foreman_usd": 0.0}

    def bump_restarts(self, work_order):
        self.restarts[work_order] = self.restarts.get(work_order, 0) + 1
        return self.restarts[work_order]

    def wait_customer(self, bead, reason):
        self.gates.append((bead, reason))
        self.act_log.append((bead, "answer", reason))
        return "gate-%d" % len(self.gates)

    def bump_iterations(self, stage, kind):
        self.iters[(stage, kind)] = self.iters.get((stage, kind), 0) + 1
        return self.iters[(stage, kind)]

    def cycle_exceeded(self, stage, kind):
        return self.iters.get((stage, kind), 0) > _defaults.load()["cycles"][kind]

    def issue(self, work_order):
        v = self.kv.get("issue.%s" % work_order)
        return json.loads(v) if v else None

    def issue_set(self, work_order, state, **fields):
        rec = self.issue(work_order) or {}
        rec.update(fields)
        rec["state"] = state
        self.kv["issue.%s" % work_order] = json.dumps(rec)

    def open_stages(self, work_order):
        return list(self.stages.get(work_order, []))

    def working_work_orders(self):
        return [self.kv["crew.%s.work_order" % n] for n in self.active
                if self.kv.get("crew.%s.work_order" % n)]

    def all_stages(self, work_order):
        return list(self.stages.get(work_order, [])) + list(self.closed_stages.get(work_order, []))

    def close_work_order(self, work_order, reason):
        if self.close_fails:
            err = real_journal.JournalError("bd close failed: open gate")
            err.closed = ["N1.2"]
            raise err
        self.closed_work_orders.append((work_order, reason))
        return {"work_order": work_order, "closed": [], "already": self.already_closed}

    def has_open_gate(self, bead):
        return bead in self.open_gates

    def bd(self, args, json_out=True):
        return [{"id": args[1], "status": "in_progress"}]

    def machine(self):
        return {"parallel_ceiling": self.ceiling}

    def crews(self, kv=None):
        return list(self.active)

    def issues(self, state=None, kv=None):
        return {k[len("issue."):]: json.loads(v) for k, v in self.kv.items()
                if k.startswith("issue.")}

    def register_crew(self, name):
        if name not in self.active:
            self.active.append(name)

    def unregister_crew(self, name):
        self.active = [n for n in self.active if n != name]

    def work_order_files(self, work_order):
        return list(self.files.get(work_order, []))

    def kv_get(self, key):
        if key.startswith("restarts.") and key[len("restarts."):] in self.restarts:
            return str(self.restarts[key[len("restarts."):]])
        return self.kv.get(key)

    def kv_all(self):
        return dict(self.kv)

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

    def estimate(self, work_order):
        return dict({"usd": None, "heavy_runs_per_stage": 20}, **self.estimates.get(work_order, {}))

    def set_spent(self, work_order, usd):
        self.set_spent_log.append((work_order, usd))

    def shift_spend(self, d, now=None):
        return dict(self.shift)

    def spend_block(self, spend, rates, work_order=None):
        return real_journal.spend_block(spend, rates, work_order, kv={})

    def accept_stage(self, stage, act):
        self.accepted.append((stage, act))
        return 12.5

    def stage_info(self, stage):
        return {"started_at": self.kv.get("stage.%s.started_at" % stage)}


def agent_entry(name="a1", pane="w:p9", cwd="/work", status="idle", session="sess-1"):
    return {"name": name, "pane_id": pane, "cwd": cwd, "agent_status": status,
            "agent_session": {"value": session}}


def split_pane(pane, terminal=None, tab="w:1"):
    return {"pane_id": pane, "terminal_id": terminal or "term-" + pane, "tab_id": tab}


def pane_info(pane, terminal=None, tab="w:1", cwd=SITE_DIR, agent=None):
    d = {"pane_id": pane, "terminal_id": terminal or "term-" + pane, "tab_id": tab,
         "foreground_cwd": cwd}
    if agent:
        d["agent"] = agent
    return d


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
        p4 = mock.patch.dict(os.environ, {"PRORAB_CACHE": os.path.join(self.dir, "cache"),
                                          "PRORAB_LOG_DIR": os.path.join(self.dir, "logs")})
        p4.start()
        self.addCleanup(p4.stop)
        # a delivery reads the screen first (final review, I4): a clean screen unless a test says otherwise
        self.route("agent read", "plain screen\n")
        # the synthetic ~/.claude.json trusts the test sites (F1); tests of the trust replace it
        self.write_trust([SITE_DIR, os.path.join(self.dir, "repo")])

    def real_wt(self):
        """A worktree directory that exists (a restart looks for it after the exit)."""
        wt = os.path.join(self.dir, "site", ".claude", "worktrees", "prorab-N1")
        os.makedirs(wt)
        return wt

    def trust_file(self):
        return os.path.join(self.home, ".claude.json")

    def write_trust(self, paths, flag=True):
        projects = {p: {"hasTrustDialogAccepted": flag, "allowedTools": []} for p in paths}
        with open(self.trust_file(), "w", encoding="utf-8") as fh:
            json.dump({"projects": projects}, fh)

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

    def write_usage(self, cwd, session, messages, subagent=None):
        """A synthetic transcript with priced messages: [(id, model, input, output)]."""
        d = os.path.join(self.home, ".claude", "projects", crew.transcript.project_slug(cwd))
        if subagent:
            d = os.path.join(d, session, "subagents")
        os.makedirs(d, exist_ok=True)
        name = subagent or session
        with open(os.path.join(d, name + ".jsonl"), "w", encoding="utf-8") as fh:
            for i, (mid, model, tin, tout) in enumerate(messages):
                fh.write(json.dumps({
                    "type": "assistant", "cwd": cwd, "sessionId": session,
                    "timestamp": "2026-10-21T10:%02d:00Z" % i,
                    "message": {"id": mid, "model": model, "content": [],
                                "usage": {"input_tokens": tin, "output_tokens": tout}}}) + "\n")


class TestStart(CrewBase):
    def routes_for_start(self, not_ready=False):
        self.route("pane layout --pane w:p1", ok({"layout": {"panes": [
            {"pane_id": "w:p1", "rect": {"width": 200, "height": 40}}]}}))
        self.route("pane split w:p1", ok({"pane": split_pane("w:p9")}))
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
        self.assertIn("a1", self.j.active)
        self.assertIn("pane split w:p1 --direction right --no-focus --cwd %s" % repo,
                      self.calls())
        self.assertEqual(self.j.kv["crew.a1.work_order"], "N1")
        self.assertEqual(self.j.act_log[0][:2], ("N1", "answer"))
        self.assertTrue(self.j.act_log[0][2].startswith("crew a1 started: pane w:p9, branch "))
        self.assertIn(", session ", self.j.act_log[0][2])

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
        self.assertEqual(r, {"status": "needs_consent", "kind": "mcp", "pane_id": "w:p9"})
        for c in self.calls():
            self.assertNotIn("send", c)
            self.assertNotIn("prompt", c)

    def test_start_on_the_login_screen_needs_login(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_start(not_ready=True)
        self.route("pane read w:p9 --source visible", fixture("login", "select_method.txt"))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_login", "pane_id": "w:p9"})
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

    def stat_trust(self):
        with open(self.trust_file(), "rb") as fh:
            data = fh.read()
        return data, os.stat(self.trust_file()).st_mtime_ns

    def assert_needs_trust_untouched(self, r):
        self.assertEqual(r, {"status": "needs_trust", "pane_id": "w:p9", "site": SITE_DIR,
                             "trust": "missing"})
        for c in self.calls():
            self.assertFalse(c.startswith("agent start"), c)
            for word in ("send", "prompt", "keys"):
                self.assertNotIn(word, c)
        rec = self.j.issue("N1")
        self.assertEqual((rec["state"], rec["pane"], rec["reason"]),
                         ("not_issued", "w:p9", "needs_trust"))
        self.assertEqual(self.j.active, [])

    def test_untrusted_site_needs_trust_without_agent_start(self):
        self.write_trust(["/home/user/projects/other"])
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.assert_needs_trust_untouched(crew.start(SITE_DIR, "N1", "a1", "w:p1"))
        self.assertIn("pane split w:p1 --direction right --no-focus --cwd %s" % SITE_DIR,
                      self.calls())

    def test_parent_trust_does_not_count(self):
        self.write_trust(["/home/user/projects", "/home/user", "/"])
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.assert_needs_trust_untouched(crew.start(SITE_DIR, "N1", "a1", "w:p1"))

    def test_trust_flag_false_needs_trust(self):
        self.write_trust([SITE_DIR], flag=False)
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.assert_needs_trust_untouched(crew.start(SITE_DIR, "N1", "a1", "w:p1"))

    def test_trust_flag_not_true_needs_trust(self):
        with open(self.trust_file(), "w", encoding="utf-8") as fh:
            json.dump({"projects": {SITE_DIR: {"hasTrustDialogAccepted": "true"}}}, fh)
        self.assertIs(crew.site_trust(SITE_DIR), False)

    def test_missing_claude_json_needs_trust(self):
        os.remove(self.trust_file())
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.assert_needs_trust_untouched(crew.start(SITE_DIR, "N1", "a1", "w:p1"))

    def test_trailing_slash_is_stripped(self):
        self.assertIs(crew.site_trust(SITE_DIR + "/"), True)

    def test_trust_by_real_path(self):
        repo = self.make_repo()
        link = os.path.join(self.dir, "link")
        os.symlink(repo, link)
        self.write_trust([os.path.realpath(repo)])
        self.assertIs(crew.site_trust(link), True)
        self.add_worktree(repo, "N1")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.assertEqual(crew.start(link, "N1", "a1", "w:p1")["status"], "started")

    def test_unreadable_claude_json_read_twice(self):
        with open(self.trust_file(), "w", encoding="utf-8") as fh:
            fh.write('{"projects": {')
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        with mock.patch.object(crew.time, "sleep") as sleep:
            r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        sleep.assert_called_once_with(_defaults.load()["crew"]["trust_retry_s"])
        self.assertEqual(r["trust"], "unknown")
        self.assertIn(r["status"], ("started", "started_no_worktree"))

    def test_unreadable_then_valid_starts_without_trust_field(self):
        good = json.dumps({"projects": {SITE_DIR: {"hasTrustDialogAccepted": True}}})
        path = self.trust_file()

        def rewrite(_s):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(good)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        with mock.patch.object(crew.time, "sleep", side_effect=rewrite):
            r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertNotIn("trust", r)
        self.assertIn(r["status"], ("started", "started_no_worktree"))

    def test_trust_file_never_written(self):
        cases = [["/home/user/projects"], [SITE_DIR], []]
        for paths in cases:
            self.write_trust(paths)
            before = self.stat_trust()
            self.j.kv.clear()
            self.j.files = {"N1": ["x"]}
            self.routes_for_start()
            crew.start(SITE_DIR, "N1", "a1", "w:p1")
            self.assertEqual(self.stat_trust(), before)
        with open(self.trust_file(), "w", encoding="utf-8") as fh:
            fh.write("{")
        before = self.stat_trust()
        with mock.patch.object(crew.time, "sleep"):
            crew.site_trust(SITE_DIR)
        self.assertEqual(self.stat_trust(), before)
        self.assertEqual([f for f in os.listdir(self.home) if "claude" in f], [".claude.json"])

    def test_trust_gate_records_the_site(self):
        self.write_trust(["/home/user/projects/other"])
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(self.j.issue("N1")["site"], SITE_DIR)

    def test_issuing_record_has_the_site(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        crew.start(SITE_DIR, "N1", "a1", "w:p1")   # reaches `agent start`
        self.assertTrue(any(c.startswith("agent start a1") for c in self.calls()))
        self.assertEqual(self.j.issue("N1")["site"], SITE_DIR)

    def test_site_trust_without_retry_does_not_sleep(self):
        with open(self.trust_file(), "w", encoding="utf-8") as fh:
            fh.write('{"projects": {')
        with mock.patch.object(crew.time, "sleep") as sleep:
            self.assertIsNone(crew.site_trust(SITE_DIR, retry=False))
        sleep.assert_not_called()
        with mock.patch.object(crew.time, "sleep") as sleep:
            self.assertIsNone(crew.site_trust(SITE_DIR))
        sleep.assert_called_once_with(_defaults.load()["crew"]["trust_retry_s"])

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

    def files_dir(self):
        return os.path.join(self.home, ".cache", "prorab", "work-orders")

    def start_line(self):
        return [c for c in self.calls() if c.startswith("agent start a1")][0]

    def settings_path(self):
        return os.path.join(self.home, ".cache", "prorab", "crew-settings.json")

    def test_start_passes_settings_only_when_enabled(self):
        repo = self.make_repo()
        self.add_worktree(repo, "N1")
        self.add_worktree(repo, "N2")
        self.j.files = {"N1": ["x"], "N2": ["y"]}
        self.routes_for_start()
        crew.start(repo, "N1", "a1", "w:p1")
        self.assertNotIn("--settings", self.start_line())
        self.assertFalse(os.path.exists(self.settings_path()))
        os.remove(os.path.join(self.dir, "calls.log"))
        self.j.kv["hooks.enabled"] = "1"
        self.j.active = []
        crew.start(repo, "N2", "a1", "w:p1")
        self.assertTrue(self.start_line().endswith(" -w prorab-N2 --settings %s"
                                                   % self.settings_path()), self.start_line())
        self.assertIn(" -- ", self.start_line())
        self.assertLess(self.start_line().index(" -- "), self.start_line().index("--settings"))
        with open(self.settings_path(), encoding="utf-8") as fh:
            self.assertIn("hooks", json.load(fh))

    def test_start_with_hooks_off_value_passes_no_settings(self):
        self.j.kv["hooks.enabled"] = "0"
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertNotIn("--settings", self.start_line())
        self.assertFalse(os.path.exists(self.settings_path()))

    def test_start_records_issuing_before_agent_start(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.route("agent start a1", err("cli_error"), exit_code=1)
        with self.assertRaises(herdr_cli.HerdrError):
            crew.start(SITE_DIR, "N1", "a1", "w:p1")
        rec = self.j.issue("N1")
        self.assertEqual(rec["state"], "issuing")
        self.assertEqual((rec["name"], rec["pane"]), ("a1", "w:p9"))
        self.assertRegex(rec["session_id"], r"^[0-9a-f-]{36}$")

    def test_start_records_the_start_commit(self):
        repo = self.make_repo()
        git(repo, "checkout", "-q", "-b", "develop")
        self.commit(repo, "dev.txt", "d\n", "develop work")
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        sha = git(wt, "rev-parse", "HEAD").strip()
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        crew.start(repo, "N1", "a1", "w:p1")
        self.assertEqual(self.j.issue("N1")["base_commit"], sha)
        self.assertEqual(self.j.kv["crew.a1.base_commit"], sha)

    def test_start_records_worktree_in_issue(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        r = crew.start(repo, "N1", "a1", "w:p1")
        rec = self.j.issue("N1")
        self.assertEqual(rec["worktree"], r["worktree"])
        self.assertEqual(os.path.realpath(rec["worktree"]), os.path.realpath(wt))
        self.assertEqual((rec["state"], rec["name"], rec["pane"]), ("issuing", "a1", "w:p9"))

    def test_start_passes_session_id_add_dir_and_worktree(self):
        repo = self.make_repo()
        self.add_worktree(repo, "N1")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        r = crew.start(repo, "N1", "a1", "w:p1")
        m = re.match(r"^agent start a1 --kind claude --pane w:p9 -- --session-id (\S+) "
                     r"--add-dir (\S+) -w prorab-N1$", self.start_line())
        self.assertIsNotNone(m, self.start_line())
        uuid_str = m.group(1)
        self.assertEqual(str(__import__("uuid").UUID(uuid_str, version=4)), uuid_str)
        self.assertEqual(m.group(2), self.files_dir())
        self.assertTrue(m.group(2).startswith(self.home))
        self.assertEqual(r["session_id"], uuid_str)
        self.assertEqual(self.j.kv["crew.a1.session"], uuid_str)
        self.assertEqual(self.j.kv["crew.a1.transcript"],
                         crew.transcript.transcript_path(r["worktree"], uuid_str))
        self.assertEqual(self.j.issue("N1")["session_id"], uuid_str)

    def test_sessions_recorded_in_the_issue_record(self):
        repo = self.make_repo()
        self.add_worktree(repo, "N1")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        r = crew.start(repo, "N1", "a1", "w:p1")
        self.assertEqual(self.j.issue("N1")["sessions"], [r["session_id"]])

    def test_start_refuses_already_issued(self):
        self.j.issue_set("N1", "issued", name="a1", pane="w:p9")
        self.route("agent list", ok({"agents": [agent_entry()]}))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "already_issued", "name": "a1", "pane_id": "w:p9"})
        self.assertFalse([c for c in self.calls() if c.startswith("pane split")])
        self.j.issue_set("N1", "issuing", name="b1", pane="w:p9")
        self.assertEqual(crew.start(SITE_DIR, "N1", "a1", "w:p1")["status"], "already_issued")

    def routes_for_timeout(self, code="timeout"):
        self.routes_for_start()
        self.route("agent start a1", err(code), exit_code=1)

    def assert_nothing_pressed(self):
        for c in self.calls():
            for word in ("send", "prompt", "keys"):
                self.assertNotIn(word, c)

    def test_start_timeout_with_trust_dialog_needs_trust(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_timeout()
        self.route("pane read w:p9 --source visible", fixture("consent", "trust.txt"))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_trust", "pane_id": "w:p9"})
        self.assert_nothing_pressed()
        self.assertEqual(self.j.active, [])

    def test_start_timeout_with_worktree_refusal_needs_trust(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_timeout()
        self.route("pane read w:p9 --source visible",
                   fixture("consent", "worktree_trust_refused.txt"))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_trust", "pane_id": "w:p9"})
        self.assert_nothing_pressed()

    def test_start_timeout_with_bare_shell_is_not_ready(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_timeout()
        self.route("pane read w:p9 --source visible", "~/projects/demo-app  main\n❯\n")
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "not_ready", "pane_id": "w:p9"})

    def test_start_other_error_code_still_raises(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_timeout("cli_error")
        with self.assertRaises(herdr_cli.HerdrError) as cm:
            crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(cm.exception.code, "cli_error")

    def issuing_in_p9(self, name="a1", session="sess-first"):
        self.j.issue_set("N1", "issuing", name=name, pane="w:p9", session_id="sess-first")
        self.route("agent list", ok({"agents": [agent_entry(session=session)]}))

    def test_retry_while_consent_prompt_is_up_returns_it_again(self):
        self.issuing_in_p9()
        self.route("pane read w:p9 --source visible", fixture("consent", "trust.txt"))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_trust", "pane_id": "w:p9"})
        self.route("pane read w:p9 --source visible", fixture("consent", "mcp.txt"))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_consent", "kind": "mcp", "pane_id": "w:p9"})
        for c in self.calls():
            self.assertFalse(c.startswith(("pane split", "agent start")), c)
        self.assert_nothing_pressed()
        self.assertEqual(self.j.active, [])

    def test_retry_after_consent_answered_registers_the_crew(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1")
        self.j.files = {"N1": ["x"]}
        self.issuing_in_p9()
        self.route("pane read w:p9 --source visible", "Claude Code\n❯ \n")
        r = crew.start(repo, "N1", "a1", "w:p1")
        self.assertEqual(r["status"], "started")
        self.assertEqual((r["pane_id"], r["session_id"]), ("w:p9", "sess-first"))
        self.assertEqual(r["branch"], "prorab/N1")
        self.assertEqual(os.path.realpath(r["worktree"]), os.path.realpath(wt))
        self.assertEqual(self.j.active, ["a1"])
        self.assertEqual(self.j.kv["crew.a1.work_order"], "N1")
        self.assertEqual(self.j.kv["crew.a1.pane"], "w:p9")
        self.assertEqual(self.j.kv["crew.a1.site"], repo)
        self.assertEqual(self.j.kv["crew.a1.branch"], "prorab/N1")
        self.assertEqual(self.j.kv["crew.a1.session"], "sess-first")
        self.assertEqual(os.path.realpath(json.loads(self.j.kv["issue.N1"])["worktree"]),
                         os.path.realpath(wt))
        self.assertEqual(self.j.act_log[0][:2], ("N1", "answer"))
        self.assertIn("crew a1 started: pane w:p9", self.j.act_log[0][2])
        for c in self.calls():
            self.assertFalse(c.startswith(("pane split", "agent start")), c)
        self.assert_nothing_pressed()

    def test_foreign_session_in_the_pane_starts_anew(self):
        repo = self.make_repo()
        self.add_worktree(repo, "N1")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.route("pane split w:p1", ok({"pane": {"pane_id": "w:p10"}}))
        self.route("agent list", ok({"agents": [agent_entry(session="customers-own"),
                                                agent_entry(pane="w:p10", session="s2")]}))
        self.j.issue_set("N1", "issuing", name="a1", pane="w:p9", session_id="sess-first")
        r = crew.start(repo, "N1", "a1", "w:p1")
        self.assertEqual(r["status"], "started")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertNotEqual(r["session_id"], "sess-first")
        self.assertEqual(self.j.issue("N1")["pane"], "w:p10")
        self.assertEqual(self.j.kv["crew.a1.pane"], "w:p10")
        calls = self.calls()
        self.assertTrue([c for c in calls if c.startswith("pane split")])
        self.assertTrue([c for c in calls if c.startswith("agent start")])
        for c in calls:
            if not c.startswith("pane get"):  # reading the old pane is fine, touching it is not
                self.assertNotIn("w:p9", c.replace("pane layout", ""))
            self.assertNotIn("close", c)
        self.assert_nothing_pressed()

    def test_pane_agent_without_session_id_is_already_issued(self):
        self.issuing_in_p9(session=None)
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "already_issued", "name": "a1", "pane_id": "w:p9"})
        self.assertEqual(self.j.active, [])
        self.assertFalse([c for c in self.calls() if c.startswith(("pane split", "agent start"))])

    def test_issuing_under_another_name_is_already_issued(self):
        self.issuing_in_p9(name="b1")
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "already_issued", "name": "b1", "pane_id": "w:p9"})
        self.assertEqual(self.j.active, [])

    def test_start_continues_when_issued_pane_is_empty(self):
        self.j.issue_set("N1", "issued", name="a1", pane="w:p9")
        self.j.files = {"N1": ["x"]}
        self.routes_for_start()
        self.route("agent list", ok({"agents": []}))
        self.assertIn(crew.start(SITE_DIR, "N1", "a1", "w:p1")["status"],
                      ("started", "started_no_worktree"))

    def test_start_bad_name(self):
        self.assertEqual(crew.start(SITE_DIR, "N1", "Bad Name", "w:p1"),
                         {"status": "bad_name"})
        self.assertEqual(self.calls_or_empty(), [])

    def test_start_names_consent_kind(self):
        self.j.files = {"N1": ["x"]}
        self.routes_for_start(not_ready=True)
        self.route("pane read w:p9 --source visible", fixture("consent", "mcp.txt"))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "needs_consent", "kind": "mcp", "pane_id": "w:p9"})
        for c in self.calls():
            self.assertNotIn("send-keys", c)

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
    def test_name_for_follows_herdr_rule(self):
        self.assertEqual(crew.name_for("/home/user/projects/Demo.App", 1), "demo-app-1")
        long_name = crew.name_for("/x/" + "a" * 40, 1)
        self.assertEqual(len(long_name), 32)
        self.assertEqual(crew.name_for("/x/1demo", 1), "c-1demo-1")
        self.assertEqual(len(crew.name_for("/x/" + "b" * 40, 123)), 32)
        for site in ("/x/Demo..App//", "/x/_x", "/x/---", "/x/\u0434\u0435\u043c\u043e", "/x/"):
            self.assertTrue(herdr_cli.valid_name(crew.name_for(site, 7)), site)
        self.assertEqual(crew.name_for("/x/a__b--c  d", 2), "a__b-c-d-2")

    def stalled_routes(self, composer):
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", err("agent_prompt_stalled"), exit_code=1)
        self.route("agent get a1", ok({"agent": {"agent_status": "done"}}))
        self.route("agent read a1 --source visible", composer)
        self.route("agent send-keys a1", "")
        self.route("agent wait a1", ok({"agent": {"agent_status": "working"}}))

    def sent_line(self):
        return "Read the file %s and do what it says." % os.path.join(
            self.home, ".cache", "prorab", "work-orders", "N1.md")

    def composer_with(self, text):
        rule = "─" * 40
        return "● Ready.\n\n%s\n❯ %s\n%s\n  ⏵⏵ bypass permissions on\n" % (rule, text, rule)

    def test_send_work_order_never_resends_text(self):
        self.stalled_routes(self.composer_with(self.sent_line()))
        self.assertEqual(crew.send_work_order("a1", "do it\nnow"), "accepted")
        calls = self.calls()
        self.assertEqual(len([c for c in calls if c.startswith("agent prompt")]), 1)
        self.assertEqual([c for c in calls if c.startswith("agent send-keys")],
                         ["agent send-keys a1 enter"])
        self.assertEqual(self.j.issue("N1")["state"], "issued")

    def test_send_work_order_empty_composer_presses_nothing(self):
        for composer in (fixture("composer", "empty.txt"), fixture("composer", "suggestion.txt")):
            self.stalled_routes(composer)
            self.assertEqual(crew.send_work_order("a1", "do it\nnow"), "stalled")
            calls = self.calls()
            self.assertEqual(len([c for c in calls if c.startswith("agent prompt")]), 1)
            self.assertEqual([c for c in calls if "send-keys" in c], [])
            os.remove(os.path.join(self.dir, "calls.log"))
        self.assertIsNone(self.j.issue("N1"))

    def test_work_order_goes_by_file(self):
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        text = "Work order\nline two\nline three"
        self.assertEqual(crew.send_work_order("a1", text), "accepted")
        path = os.path.join(self.home, ".cache", "prorab", "work-orders", "N1.md")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), text)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        prompts = [c for c in self.calls() if c.startswith("agent prompt")]
        self.assertEqual(len(prompts), 1)
        self.assertTrue(prompts[0].startswith("agent prompt a1 " + self.sent_line() + " --wait"))
        # sent again: the file is replaced in place
        crew.send_work_order("a1", "second\nversion")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "second\nversion")

    def test_amended_work_order_reaches_a_running_crew_as_one_changed_line(self):
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        crew.send_work_order("a1", "Work order\nC1: first")
        crew.send_work_order("a1", "Work order\nC1: first\n\n## Amendment 1\nC2: new")
        prompts = [c for c in self.calls() if c.startswith("agent prompt")]
        path = os.path.join(self.home, ".cache", "prorab", "work-orders", "N1.md")
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[0].startswith("agent prompt a1 " + self.sent_line() + " --wait"))
        changed = crew._template("Changed work order").format(path=path)
        self.assertTrue(prompts[1].startswith("agent prompt a1 " + changed + " --wait"))
        with open(path, encoding="utf-8") as fh:
            self.assertIn("C2: new", fh.read())

    def test_multiline_goes_by_file(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.deliver("a1", "one\ntwo", "N1"), "accepted")
        self.assertEqual(crew.deliver("a1", "three\nfour", "N1"), "accepted")
        d = os.path.join(self.home, ".cache", "prorab", "work-orders")
        self.assertEqual(sorted(os.listdir(d)), ["N1-1.md", "N1-2.md"])
        with open(os.path.join(d, "N1-2.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "three\nfour")
        self.assertEqual(crew.deliver("a1", "x\ny"), "accepted")
        self.assertTrue(any(re.match(r"msg-\d+\.md$", n) for n in os.listdir(d)))
        self.assertEqual(crew.deliver("a1", "single line", "N1"), "accepted")
        self.assertIn("agent prompt a1 single line --wait --until working --until done "
                      "--until blocked --timeout 60000", self.calls())
        self.assertEqual(len(os.listdir(d)), 3)

    def test_message_file_name_is_not_a_path(self):
        for bad in ("../x", "a/b", ".hidden"):
            with self.assertRaises(ValueError):
                crew.message_file(bad, "t")

    def test_send_work_order_marks_issued(self):
        self.j.issue_set("N1", "issuing", name="a1", pane="w:p9")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        crew.send_work_order("a1", "t", "N1")
        rec = self.j.issue("N1")
        self.assertEqual((rec["state"], rec["pane"]), ("issued", "w:p9"))

    def test_send_work_order_not_accepted_stays_issuing(self):
        self.j.issue_set("N1", "issuing", name="a1", pane="w:p9")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "idle"}}))
        self.route("agent read a1 --source visible", "plain screen\n")
        self.assertEqual(crew.send_work_order("a1", "t", "N1"), "unconfirmed")
        self.assertEqual(self.j.issue("N1")["state"], "issuing")

    def test_blocked_after_send_names_consent(self):
        self.route("agent prompt a1", err("agent_blocked"), exit_code=1)
        self.route("agent read a1 --source visible", fixture("consent", "api_key.txt"))
        self.assertEqual(crew.send_work_order("a1", "t", "N1"), "consent:api_key")
        self.route("agent read a1 --source visible", "plain screen\n")
        self.assertEqual(crew.deliver("a1", "t", "N1"), "blocked")

    def test_stalled_then_consent_returns_kind(self):
        self.route("agent prompt a1", err("agent_prompt_stalled"), exit_code=1)
        self.route("agent get a1", ok({"agent": {"agent_status": "working"}}))  # recover: stays stalled
        self.route_seq("agent read a1 --source visible",  # clean before the send, a dialog after
                       ["plain screen\n", fixture("consent", "trust.txt")])
        self.assertEqual(crew.deliver("a1", "t", "N1"), "consent:trust")
        calls = self.calls()
        self.assertEqual([c for c in calls if "send-keys" in c], [])
        self.assertEqual(len([c for c in calls if c.startswith("agent prompt")]), 1)

    def test_unconfirmed_then_consent_returns_kind(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "idle"}}))
        self.route_seq("agent read a1 --source visible",
                       ["plain screen\n", fixture("consent", "mcp.txt")])
        self.assertEqual(crew.deliver("a1", "t", "N1"), "consent:mcp")
        self.assertEqual([c for c in self.calls() if "send-keys" in c], [])

    def test_deliver_names_the_login(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "idle"}}))
        self.route_seq("agent read a1 --source visible",
                       ["plain screen\n", fixture("login", "not_logged_in.txt")])
        self.assertEqual(crew.deliver("a1", "t", "N1"), "login")
        self.assertEqual([c for c in self.calls() if "send-keys" in c], [])
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent prompt")]), 1)

    def write_sensor(self, session, window=1000000, age=0):
        folder = os.path.join(self.home, ".cache", "prorab", "usage")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, session + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"model": {"id": "claude-opus-5-5"}, "context_window": {
                "context_window_size": window,
                "current_usage": {"input_tokens": 100, "cache_creation_input_tokens": 200,
                                  "cache_read_input_tokens": 300, "output_tokens": 400}}}, fh)
        if age:
            os.utime(path, (time.time() - age, time.time() - age))

    def test_state_prefers_the_sensor_context(self):
        self.write_transcript("/work", "sess-1", "x")  # model "x": unknown to the defaults
        self.write_sensor("sess-1")
        self.route("agent list", ok({"agents": [agent_entry(status="working")]}))
        self.route("agent read a1 --source visible", "text\n")
        ctx = crew.state("a1")["ctx"]
        self.assertEqual((ctx["source"], ctx["window"], ctx["tokens"]), ("sensor", 1000000, 1000))

    def test_state_falls_back_to_the_transcript_context(self):
        self.write_transcript("/work", "sess-1", "x")
        self.write_sensor("sess-1", age=self.d_sensor_max_age() + 60)
        self.route("agent list", ok({"agents": [agent_entry(status="working")]}))
        self.route("agent read a1 --source visible", "text\n")
        ctx = crew.state("a1")["ctx"]
        self.assertEqual((ctx["source"], ctx["window"]), ("transcript", 200000))

    @staticmethod
    def d_sensor_max_age():
        return _defaults.load()["limits"]["sensor_max_age_s"]

    def test_state_reads_the_login_marker(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        a = crew.herdr_cli.agents()[0]
        st = crew.state("a1", a, read_screen=False, screen=fixture("login", "not_logged_in.txt"))
        self.assertTrue(st["login"])
        st = crew.state("a1", a, read_screen=False, screen="plain\n")
        self.assertFalse(st["login"])
        self.assertIsNone(crew.state("a1", a, read_screen=False)["login"])
        self.route("agent list", ok({"agents": []}))
        self.assertIsNone(crew.state("a1")["login"])

    def test_unconfirmed_without_consent_stays_unconfirmed(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "idle"}}))
        self.route("agent read a1 --source visible", "plain screen\n")
        self.assertEqual(crew.deliver("a1", "t", "N1"), "unconfirmed")
        self.route("agent prompt a1", err("agent_prompt_stalled"), exit_code=1)
        self.route("agent get a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.deliver("a1", "t", "N1"), "stalled")
        self.assertEqual([c for c in self.calls() if "send-keys" in c], [])

    def test_state_and_summary(self):
        self.write_transcript("/work", "sess-1", "done")
        self.route("agent list", ok({"agents": [agent_entry(status="working")]}))
        self.route("agent read a1 --source visible", "text · 2 shells\n")
        st = crew.state("a1")
        self.assertEqual(st["status"], "working")
        self.assertEqual(st["bg_shells"], 2)
        self.assertTrue(st["name_ok"])
        self.assertEqual((st["consent"], st["limit_screen"], st["provider_error"]),
                         (None, None, None))
        self.assertEqual(st["session_id"], "sess-1")
        self.assertEqual(st["cwd"], "/work")
        self.assertEqual(st["ctx"]["tokens"], 10 + 20 + 999 + 30)
        self.assertEqual(crew.summary("a1"), "done")

    def test_state_names_its_transcript(self):
        self.write_transcript("/work", "sess-1", "x")
        path = crew.transcript.transcript_path("/work", "sess-1", self.home)
        self.route("agent list", ok({"agents": [agent_entry(status="idle")]}))
        self.route("agent read a1 --source visible", "idle\n")
        self.assertEqual(crew.state("a1")["transcript"], path)
        self.assertIsNone(crew.gone_state()["transcript"])

    def test_state_finds_agent_by_pane_and_reads_transcript_from_kv(self):
        self.write_transcript("/work", "sess-1", "x")
        path = crew.transcript.transcript_path("/work", "sess-1", self.home)
        self.j.kv.update({"crew.a1.pane": "w:p9", "crew.a1.transcript": path})
        # the agent was renamed: the pane still finds it, name_ok says so
        self.route("agent list", ok({"agents": [dict(agent_entry(name="other"),
                                                     agent_session={"value": "unrelated"})]}))
        self.route("agent read other --source visible", "idle\n")
        st = crew.state("a1")
        self.assertEqual(st["pane_id"], "w:p9")
        self.assertFalse(st["name_ok"])
        self.assertIsNotNone(st["ctx"])

    def test_state_reads_screen_once_and_reports_markers(self):
        self.route("agent list", ok({"agents": [agent_entry(status="blocked")]}))
        self.route("agent read a1 --source visible", fixture("limit_choice.txt"))
        st = crew.state("a1")
        self.assertEqual(st["limit_screen"]["kind"], "limit_choice")
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent read")]), 1)
        self.route("agent read a1 --source visible", fixture("consent", "mcp.txt"))
        self.assertEqual(crew.state("a1")["consent"], "mcp")
        self.route("agent read a1 --source visible", fixture("provider_error.txt"))
        self.assertIsNotNone(crew.state("a1")["provider_error"])

    def test_state_without_screen_has_unknown_markers(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        st = crew.state("a1", read_screen=False)
        self.assertEqual([st[k] for k in ("bg_shells", "consent", "limit_screen",
                                          "provider_error")], [None] * 4)
        self.assertFalse([c for c in self.calls() if c.startswith("agent read")])

    def test_state_takes_a_screen_already_read(self):
        self.route("agent list", ok({"agents": [agent_entry(status="blocked")]}))
        a = crew.herdr_cli.agents()[0]
        st = crew.state("a1", a, read_screen=False, screen=fixture("consent", "mcp.txt"))
        self.assertEqual(st["consent"], "mcp")
        self.assertFalse([c for c in self.calls() if c.startswith("agent read")])

    def test_state_empty_screen_is_a_read_screen(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        a = crew.herdr_cli.agents()[0]
        st = crew.state("a1", a, read_screen=False, screen="")
        self.assertEqual([st[k] for k in ("bg_shells", "consent", "limit_screen",
                                          "provider_error")], [0, None, None, None])
        self.assertFalse([c for c in self.calls() if c.startswith("agent read")])

    def test_state_uses_kv_snapshot(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        snap = {"crew.a1.pane": "w:p9"}
        self.assertEqual(crew.state("a1", read_screen=False, kv=snap)["pane_id"], "w:p9")

    def test_state_gone(self):
        self.route("agent list", ok({"agents": []}))
        self.assertEqual(crew.state("a1")["status"], "gone")

    def test_pause_and_handover_request_use_acts_md(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        crew.pause("a1")
        crew.request_handover("a1")
        calls = [c for c in self.calls() if c.startswith("agent prompt")]
        self.assertIn("wait for the foreman's message", calls[0])
        self.assertIn("HANDOVER READY", calls[1])
        self.assertIn("next steps", calls[1])

    def test_deliver_refuses_a_dialog_herdr_does_not_report_as_blocked(self):
        """The screen is read before typing: nothing is sent into a dialog (final review, I4)."""
        self.route("agent prompt a1", ok({"agent": {"agent_status": "idle"}}))
        screens = {"consent:trust": fixture("consent", "trust.txt"),
                   "consent:mcp": fixture("consent", "mcp.txt"),
                   "login": fixture("login", "not_logged_in.txt"),
                   "exit_dialog": fixture("exit", "worktree_exit.txt"),
                   "limit_screen": fixture("limit_choice.txt")}
        for expected, screen in screens.items():
            self.route("agent read a1 --source visible", screen)
            self.assertEqual(crew.deliver("a1", "t", "N1"), expected)
        self.assertEqual([c for c in self.calls() if c.startswith(("agent prompt", "agent send"))], [])

    def test_deliver_to_a_clean_screen_still_delivers(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.deliver("a1", "t", "N1"), "accepted")
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent prompt")]), 1)

    def test_the_emergency_pause_is_refused_the_same_way(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "idle"}}))
        self.route("agent read a1 --source visible", fixture("consent", "trust.txt"))
        os.makedirs(os.path.dirname(crew._defaults.load()["emergency"]["stop_path"]), exist_ok=True)
        with mock.patch.object(crew, "emergency_stop", return_value=True):
            self.assertEqual(crew.pause("a1"), "consent:trust")
        self.assertEqual([c for c in self.calls() if c.startswith("agent prompt")], [])


class TestHandover(CrewBase):
    def setUp(self):
        super().setUp()
        self.wt = self.real_wt()
        self.j.kv["crew.a1.worktree"] = self.wt
        self.write_transcript("/work", "sess-1", "Act: done A\nHANDOVER READY")
        self.route("agent read a1 --source visible", "idle\n")

    def test_complete_handover_with_shells_asks_nothing(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent read a1 --source visible", "bar \u00b7 2 shells\n")
        r = crew.complete_handover("a1", "N1", "work order", timeout_s=1, poll_s=0.01)
        self.assertEqual(r["status"], "bg_shells")
        self.assertEqual([c for c in self.calls() if c.startswith("agent prompt")], [])

    def test_complete_handover_refuses_with_bg_shells(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent read a1 --source visible", "bar · 1 shell\n")
        r = crew.complete_handover("a1", "N1", "work order", timeout_s=1, poll_s=0.01)
        self.assertEqual(r["status"], "bg_shells")
        self.assertEqual(self.j.act_log, [])
        for c in self.calls():
            self.assertFalse(c.startswith("agent prompt"))

    def test_complete_handover_survives_the_silent_pane_run(self):
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        inwt = {"pane": {"pane_id": "w:p9", "foreground_cwd": self.wt}}
        self.route("pane get w:p9", ok(inwt))
        self.route("pane run w:p9", "")
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.complete_handover("a1", "N1", "Work order text", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "restarted")
        self.assertTrue(any(c.startswith("agent start a1 --kind claude --pane w:p9 -- ")
                            for c in self.calls()))

    def test_complete_handover_sequence(self):
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        pane_info = {"pane": {"pane_id": "w:p9", "foreground_cwd": "/home/user/projects/demo-app"}}
        inwt = {"pane": {"pane_id": "w:p9", "foreground_cwd": self.wt}}
        self.route_seq("pane get w:p9", [ok(pane_info), ok(pane_info), ok(inwt)])
        self.route("pane run w:p9", "")
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.complete_handover("a1", "N1", "Work order text", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "restarted")
        calls = self.calls()
        i_exit = calls.index("agent prompt a1 /exit --wait --until working --until done --until blocked --timeout 60000")
        i_cd = calls.index("pane run w:p9 cd %s" % self.wt)
        i_start = next(i for i, c in enumerate(calls)
                       if c.startswith("agent start a1 --kind claude --pane w:p9 -- "))
        i_prompt = next(i for i, c in enumerate(calls)
                        if c.startswith("agent prompt a1 Read the file"))
        self.assertTrue(i_exit < i_cd < i_start < i_prompt)
        path = os.path.join(self.home, ".cache", "prorab", "work-orders", "N1-1.md")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertTrue(body.startswith("Work order text"))
        self.assertIn("Act: done A", body)
        self.assertNotIn("HANDOVER READY", body)
        self.assertEqual(len(self.j.act_log), 1)
        self.assertEqual(self.j.act_log[0][:2], ("N1", "handover"))
        self.assertTrue(self.j.act_log[0][2].startswith("Act: done A"))
        self.assertEqual(self.j.spent, [("N1", 1, 0, 0)])
        self.assertEqual(self.j.set_spent_log, [("N1", 0.0)])
        self.assertIn("Spend:", self.j.act_log[0][2])

    def test_complete_handover_records_the_new_session(self):
        self.j.issue_set("N1", "issued", sessions=["sess-1"])
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        inwt = {"pane": {"pane_id": "w:p9", "foreground_cwd": self.wt}}
        self.route("pane get w:p9", ok(inwt))
        self.route("pane run w:p9", "")
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.complete_handover("a1", "N1", "Work order text", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "restarted")
        self.assertEqual(self.j.issue("N1")["sessions"], ["sess-1", r["session_id"]])

    def test_restart_never_falls_back_to_site_dir(self):
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        pane_info = {"pane": {"pane_id": "w:p9", "foreground_cwd": "/home/user/projects/demo-app"}}
        self.route("pane get w:p9", ok(pane_info))
        self.route("pane run w:p9", "")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.complete_handover("a1", "N1", "t", timeout_s=0.1, poll_s=0.01)
        self.assertEqual(r["status"], "restart_failed")
        for c in self.calls():
            self.assertFalse(c.startswith("agent start"))
        self.assertEqual(self.j.spent, [("N1", 1, 0, 0)])

    def restart_routes(self, wt=None):
        wt = wt or self.wt
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        pane_info = {"pane": {"pane_id": "w:p9", "foreground_cwd": "/home/user/projects/demo-app"}}
        inwt = {"pane": {"pane_id": "w:p9", "foreground_cwd": wt}}
        self.route_seq("pane get w:p9", [ok(pane_info), ok(pane_info), ok(inwt)])
        self.route("pane run w:p9", "")

    def go(self):
        return crew.complete_handover("a1", "N1", "t", timeout_s=2, poll_s=0.01)

    def test_act_missing_sends_nothing(self):
        self.write_transcript("/work", "sess-1", "still working")
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.assertEqual(self.go(), {"status": "act_missing"})
        self.assertEqual(self.j.act_log, [])
        self.assertEqual(self.j.spent, [])
        for c in self.calls():
            self.assertFalse(c.startswith(("agent prompt", "pane run")))

    def named_act(self, message, path=None, text="Act: done B\nNext: C", size=None):
        """The last message names an act file; the file is written at `path` when given."""
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text if size is None else "x" * size)
        self.write_transcript("/work", "sess-1", message)
        self.restart_routes()
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))

    def assert_act_missing(self):
        self.assertEqual(self.go(), {"status": "act_missing"})
        self.assertEqual(self.j.act_log, [])
        self.assertEqual(self.j.spent, [])
        for c in self.calls():
            self.assertFalse(c.startswith(("agent prompt", "pane run", "agent start")))

    def test_handover_act_named_file_in_the_worktree_is_read(self):
        path = os.path.join(self.wt, "notes", "act.md")
        self.named_act("The act is in `%s`.\nHANDOVER READY" % path, path)
        self.assertEqual(self.go()["status"], "restarted")
        act = self.j.act_log[0][2]
        self.assertTrue(act.startswith("Act: done B\nNext: C"))
        self.assertIn("(read from %s)" % os.path.realpath(path), act)

    def test_handover_act_named_file_in_the_work_orders_dir_is_read(self):
        path = os.path.join(crew._files_dir(), "N1-act.md")
        self.named_act("Written to %s\nHANDOVER READY" % path, path)
        self.assertEqual(self.go()["status"], "restarted")
        self.assertIn("Act: done B", self.j.act_log[0][2])

    def test_handover_act_named_file_outside_is_act_missing(self):
        path = os.path.join(self.dir, "elsewhere", "act.md")
        self.named_act("See `%s`\nHANDOVER READY" % path, path)
        self.assert_act_missing()

    def test_handover_act_link_pointing_outside_is_act_missing(self):
        target = os.path.join(self.dir, "elsewhere", "act.md")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("secret")
        link = os.path.join(self.wt, "act.md")
        os.symlink(target, link)
        self.named_act("See `%s`\nHANDOVER READY" % link)
        self.assert_act_missing()

    def test_handover_marker_alone_names_no_file_is_act_missing(self):
        self.named_act("HANDOVER READY")
        self.assert_act_missing()

    def test_handover_act_oversized_file_is_act_missing(self):
        path = os.path.join(self.wt, "act.md")
        self.named_act("`%s`\nHANDOVER READY" % path, path, size=64 * 1024 + 1)
        self.assert_act_missing()

    def test_handover_act_file_of_64k_is_read(self):
        path = os.path.join(self.wt, "act.md")
        self.named_act("`%s`\nHANDOVER READY" % path, path, size=64 * 1024)
        self.assertEqual(self.go()["status"], "restarted")

    def test_unknown_worktree_never_restarts_in_cwd(self):
        del self.j.kv["crew.a1.worktree"]
        self.route("agent list", ok({"agents": [agent_entry()]}))
        r = self.go()
        self.assertEqual(r["status"], "restart_failed")
        for c in self.calls():
            self.assertFalse(c.startswith(("agent prompt", "pane run", "agent start")))

    def test_spend_recorded_before_exit_even_if_restart_fails(self):
        self.restart_routes()
        self.route("agent start a1", err("agent_start_failed"), exit_code=1)
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(self.go()["status"], "restart_failed")
        self.assertEqual(self.j.spent, [("N1", 1, 0, 0)])
        self.assertEqual(self.j.set_spent_log, [("N1", 0.0)])

    def test_handover_act_has_spend(self):
        self.write_usage(self.wt, "s-old", [("m1", "claude-opus-5", 1000000, 100000)])
        self.write_usage(self.wt, "s-old", [("m2", "claude-haiku-5", 1000000, 0)],
                         subagent="agent-1")
        self.j.estimates["N1"] = {"usd": 10.0}
        self.restart_routes()
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = self.go()
        self.assertEqual(r["status"], "restarted")
        self.assertAlmostEqual(r["usd"], 6.1)  # opus 4 + 2, haiku 0.1
        act = self.j.act_log[0][2]
        self.assertIn("Spend:", act)
        self.assertIn("work order N1: $6.10 of $10.00 estimated", act)
        self.assertIn("1.74 pp of the window", act)  # default rate 3.5 $/pp
        self.assertEqual(self.j.set_spent_log[0][0], "N1")
        self.assertAlmostEqual(self.j.set_spent_log[0][1], 6.1)
        self.assertEqual(self.j.spent, [("N1", 1, 0, 0)])

    def test_restart_consent_prompt_sends_nothing(self):
        self.restart_routes()
        self.route("agent start a1", err("agent_not_ready"), exit_code=1)
        self.route_seq("pane read w:p9 --source visible",  # clean before /exit, the dialog of the new session after
                       ["plain screen\n",
                        "Do you trust the files here?\nYes, I trust this folder\n"])
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(self.go()["status"], "needs_trust")
        calls = self.calls()
        self.assertEqual([c for c in calls if c.startswith(("agent prompt", "agent send-keys",
                                                            "pane send"))],
                         ["agent prompt a1 /exit --wait --until working --until done --until blocked --timeout 60000"])

    def test_prompt_not_accepted_is_not_restarted(self):
        self.restart_routes()
        self.route("agent start a1", ok({}))
        with mock.patch.object(crew.herdr_cli, "send",
                               side_effect=[None, "blocked"]):
            self.assertEqual(self.go()["status"], "prompt_not_accepted")

    def real_repo_handover(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "add g file")
        with open(os.path.join(wt, "dirty.txt"), "w") as fh:
            fh.write("x")
        self.j.kv["crew.a1.worktree"] = wt
        self.j.stages["N1"] = ["N1.1"]
        self.restart_routes(wt)
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        return wt

    def test_handover_act_has_git_facts(self):
        self.real_repo_handover()
        self.assertEqual(self.go()["status"], "restarted")
        act = self.j.act_log[0][2]
        self.assertIn("## Facts at handover", act)
        self.assertIn("git status --short", act)
        self.assertIn("?? dirty.txt", act)
        self.assertIn("add g file", act)
        self.assertIn("in_progress", act)
        self.assertTrue(act.startswith("Act: done A"))

    def test_no_exit_when_act_not_saved(self):
        self.restart_routes()
        with mock.patch.object(self.j, "add_act", side_effect=RuntimeError("bd down")):
            self.assertEqual(self.go()["status"], "act_not_saved")
        for c in self.calls():
            self.assertFalse(c.startswith(("agent prompt", "pane run", "agent start")))
        self.assertEqual(self.j.spent, [])

    def test_handover_never_bumps_restarts(self):
        self.real_repo_handover()
        self.go()
        self.assertEqual(self.j.restarts, {})
        self.assertIn("handover.N1.last", self.j.kv)

    def test_second_handover_within_10_min_is_a_loop(self):
        self.restart_routes()
        self.j.kv["handover.N1.last"] = str(time.time() - 60)
        self.assertEqual(self.go(), {"status": "handover_loop"})
        self.assertEqual(self.j.gates, [("N1", "handover loop")])
        self.assertEqual(self.j.spent, [])
        for c in self.calls():
            self.assertFalse(c.startswith(("agent prompt", "pane run", "agent start")))

    def test_handover_after_the_interval_proceeds(self):
        self.restart_routes()
        self.j.kv["handover.N1.last"] = str(time.time() - 3600)
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(self.go()["status"], "restarted")
        self.assertEqual(self.j.gates, [])

    def test_handover_new_session_id(self):
        self.restart_routes()
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.go()
        line = [c for c in self.calls() if c.startswith("agent start a1")][0]
        m = re.match(r"^agent start a1 --kind claude --pane w:p9 -- --session-id (\S+) "
                     r"--add-dir (\S+)$", line)
        self.assertIsNotNone(m, line)
        self.assertEqual(m.group(2), os.path.join(self.home, ".cache", "prorab", "work-orders"))
        self.assertEqual(self.j.kv["crew.a1.session"], m.group(1))
        self.assertEqual(self.j.kv["crew.a1.transcript"],
                         crew.transcript.transcript_path(self.wt, m.group(1)))

    def test_cd_command_is_shell_quoted(self):
        wt = os.path.join(self.dir, "it's a", "prorab-N1")
        os.makedirs(wt)
        self.j.kv["crew.a1.worktree"] = wt
        self.restart_routes(wt)
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.go()
        import shlex
        cd = [c for c in self.calls() if c.startswith("pane run w:p9 ")][0]
        self.assertEqual(shlex.split(cd[len("pane run w:p9 "):]), ["cd", wt])


class TestRecordedBase(CrewBase):
    """The worktree's start commit is the base of the acceptance checks (final review, I3)."""

    def develop_repo(self):
        """A site on `develop`, one commit ahead of `main`; returns (repo, worktree, start sha)."""
        repo = self.make_repo()
        git(repo, "checkout", "-q", "-b", "develop")
        self.commit(repo, "dev.txt", "d\n", "develop work")
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        return repo, wt, git(wt, "rev-parse", "HEAD").strip()

    def test_evidence_defaults_to_the_recorded_base(self):
        repo, wt, sha = self.develop_repo()
        self.commit(wt, "g.txt", "1\n", "one")
        self.assertEqual(len(crew.evidence(wt)["commits"]), 2)  # no record: main..HEAD (legacy)
        self.j.issue_set("N1", "issued", worktree=wt, base_commit=sha)
        ev = crew.evidence(wt)
        self.assertEqual(len(ev["commits"]), 1)
        self.assertNotIn("dev.txt", ev["diffstat"])
        self.assertEqual(len(crew.evidence(wt, base="main")["commits"]), 2)  # explicit wins

    def test_kv_of_the_crew_gives_the_base(self):
        repo, wt, sha = self.develop_repo()
        self.j.active = ["a1"]
        self.j.kv.update({"crew.a1.worktree": wt, "crew.a1.base_commit": sha})
        self.assertEqual(crew._resolve_base(wt, None), sha)

    def test_handover_facts_use_the_recorded_base(self):
        repo, wt, sha = self.develop_repo()
        self.commit(wt, "g.txt", "1\n", "one")
        self.j.issue_set("N1", "issued", worktree=wt, base_commit=sha)
        facts = crew._facts(wt, "N1", None)
        self.assertIn("one", facts)
        self.assertNotIn("develop work", facts)

    def test_check_report_delivery_uses_the_recorded_base(self):
        repo, wt, sha = self.develop_repo()
        self.commit(wt, "g.txt", "1\n", "one")
        self.j.issue_set("N1", "issued", worktree=wt, base_commit=sha)
        head = git(wt, "rev-parse", "HEAD").strip()
        problems = crew._check_delivery(head, ["`g.txt`"], ["g.txt"], wt, None)
        self.assertEqual([p for p in problems if "outside" in p or "not listed" in p], [])


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

    def test_cleanup_clears_the_consent_watch(self):
        repo = self.make_repo()
        self.j.kv.update({"consent.N1.trust": "{}", "consent.N1.mcp": "{}",
                          "consent.N2.trust": "{}", "consent_seen.N1": "[]"})
        crew.cleanup(repo, "N1")
        self.assertEqual(sorted(k for k in self.j.kv if k.startswith("consent")),
                         ["consent.N2.trust", "consent_seen.N1"])

    def test_cleanup_keeps_unmerged(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        self.assertEqual(crew.cleanup(repo, "N1"), {"status": "kept", "reason": "unmerged", "work_order_closed": False})
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
        self.j.add_act("N1", "answer", crew.DROP_MARKER + " (customer's words)")
        self.assertEqual(crew.cleanup(repo, "N1")["status"], "removed")
        self.assertFalse(os.path.exists(wt))

    def test_cleanup_closes_a_merged_work_order(self):
        repo = self.make_repo()
        self.add_worktree(repo, "N1", "prorab/N1")
        self.j.issue_set("N1", "issued", name="a1")
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "removed")
        self.assertTrue(r["work_order_closed"])
        self.assertEqual(self.j.closed_work_orders, [("N1", "the branch merged")])
        self.assertEqual(self.j.issue("N1")["decided"], "merged")

    def test_cleanup_closes_a_dropped_work_order(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        self.j.issue_set("N1", "issued", name="a1")
        self.j.add_act("N1", "answer", crew.DROP_MARKER + " (customer's words)")
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "removed")
        self.assertEqual(self.j.closed_work_orders, [("N1", "dropped by the customer")])
        self.assertEqual(self.j.issue("N1")["decided"], "dropped")

    def test_cleanup_closes_a_dropped_work_order_without_a_worktree(self):
        repo = self.make_repo()
        self.j.add_act("N1", "answer", crew.DROP_MARKER)
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "absent")
        self.assertTrue(r["work_order_closed"])
        self.assertEqual(self.j.closed_work_orders, [("N1", "dropped by the customer")])
        self.assertIsNone(self.j.issue("N1"))  # no record, no field

    def test_cleanup_absent_without_a_drop_closes_nothing(self):
        repo = self.make_repo()
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "absent")
        self.assertEqual(self.j.closed_work_orders, [])

    def test_cleanup_reports_a_refused_close(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.j.close_fails = True
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "removed")
        self.assertFalse(r["work_order_closed"])
        self.assertIn("open gate", r["close_error"])
        self.assertEqual(r["closed"], ["N1.2"])
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(r["branch"], "prorab/N1")

    def test_cleanup_absent_reports_a_refused_close(self):
        repo = self.make_repo()
        self.j.add_act("N1", "answer", crew.DROP_MARKER)
        self.j.close_fails = True
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "absent")
        self.assertFalse(r["work_order_closed"])
        self.assertIn("open gate", r["close_error"])
        self.assertEqual(r["closed"], ["N1.2"])

    def test_cleanup_kept_closes_nothing(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r, {"status": "kept", "reason": "unmerged", "work_order_closed": False})
        self.assertEqual(self.j.closed_work_orders, [])

    def test_cleanup_drop_needs_the_exact_marker(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        for text in ("drop the unused column", "dropdown menu", "decision: drop"):
            self.j.add_act("N1", "answer", text)
        self.assertEqual(crew.cleanup(repo, "N1"), {"status": "kept", "reason": "unmerged", "work_order_closed": False})
        self.assertTrue(os.path.isdir(wt))

    def drop_worktree(self, repo, wt):
        """What Claude Code does on exit for a no-change worktree: removes it, leaves the branch."""
        git(repo, "worktree", "remove", wt)

    def test_cleanup_gone_worktree_deletes_the_merged_branch(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.drop_worktree(repo, wt)
        self.j.issue_set("N1", "issued", branch="prorab/N1")
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "removed")
        self.assertIsNone(r["worktree"])
        self.assertEqual(r["branch"], "prorab/N1")
        self.assertTrue(r["work_order_closed"])
        self.assertEqual(self.j.closed_work_orders, [("N1", "the branch merged")])
        self.assertEqual(git(repo, "branch", "--list", "prorab/N1").strip(), "")
        self.assertEqual(self.j.issue("N1")["decided"], "merged")

    def test_cleanup_gone_worktree_finds_the_default_named_branches(self):
        for branch in ("prorab/N1", "worktree-prorab-N1"):
            repo = self.make_repo()
            git(repo, "branch", branch)   # no record: both names are tried
            r = crew.cleanup(repo, "N1")
            self.assertEqual((r["status"], r["branch"]), ("removed", branch))
            self.assertEqual(git(repo, "branch", "--list", branch).strip(), "")
            shutil.rmtree(repo)

    def test_cleanup_gone_worktree_prunes_a_stale_entry(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        shutil.rmtree(wt)   # the directory is gone, git still lists it
        r = crew.cleanup(repo, "N1")
        self.assertEqual((r["status"], r["worktree"]), ("removed", None))
        self.assertEqual(git(repo, "branch", "--list", "prorab/N1").strip(), "")
        self.assertNotIn("prorab-N1", git(repo, "worktree", "list"))

    def test_cleanup_gone_worktree_on_a_closed_work_order(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.drop_worktree(repo, wt)
        self.j.already_closed = True
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "removed")
        self.assertEqual(r["work_order_closed"], "already")
        self.assertEqual(git(repo, "branch", "--list", "prorab/N1").strip(), "")

    def test_cleanup_gone_worktree_keeps_an_unmerged_branch(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        self.drop_worktree(repo, wt)
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r, {"status": "kept", "reason": "unmerged", "work_order_closed": False})
        self.assertIn("prorab/N1", git(repo, "branch", "--list", "prorab/N1"))
        self.assertEqual(self.j.closed_work_orders, [])

    def test_cleanup_gone_worktree_drops_an_unmerged_branch_on_a_drop_decision(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        self.drop_worktree(repo, wt)
        self.j.add_act("N1", "answer", crew.DROP_MARKER + " (customer's words)")
        r = crew.cleanup(repo, "N1")
        self.assertEqual((r["status"], r["worktree"], r["branch"]), ("removed", None, "prorab/N1"))
        self.assertEqual(self.j.closed_work_orders, [("N1", "dropped by the customer")])
        self.assertEqual(git(repo, "branch", "--list", "prorab/N1").strip(), "")

    def test_cleanup_gone_worktree_neither_is_absent(self):
        repo = self.make_repo()
        self.assertEqual(crew.cleanup(repo, "N1"), {"status": "absent", "work_order_closed": False})

    def test_cleanup_gone_worktree_never_deletes_the_checked_out_branch(self):
        repo = self.make_repo()
        git(repo, "checkout", "-q", "-b", "prorab/N1")   # merged into main, but checked out
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "absent")
        self.assertIn("prorab/N1", git(repo, "branch", "--list", "prorab/N1"))

    def test_cleanup_is_refused_while_a_crew_of_the_work_order_runs(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.j.active = ["a1"]
        self.j.kv["crew.a1.work_order"] = "N1"
        refused = {"status": "kept", "reason": "crew_running", "work_order_closed": False}
        self.assertEqual(crew.cleanup(repo, "N1"), refused)   # the worktree path
        self.assertTrue(os.path.isdir(wt))
        self.drop_worktree(repo, wt)
        self.assertEqual(crew.cleanup(repo, "N1"), refused)   # the gone-worktree path
        self.assertIn("prorab/N1", git(repo, "branch", "--list", "prorab/N1"))
        self.assertEqual(self.j.closed_work_orders, [])
        self.j.active = ["a2"]   # another work order's crew does not hold it
        self.j.kv["crew.a2.work_order"] = "N2"
        self.assertEqual(crew.cleanup(repo, "N1")["status"], "removed")

    def test_cleanup_is_refused_under_the_emergency_stop(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.drop_worktree(repo, wt)
        stop_path = _defaults.load()["emergency"]["stop_path"]
        os.makedirs(os.path.dirname(stop_path), exist_ok=True)
        with open(stop_path, "w") as fh:
            fh.write("")
        self.addCleanup(lambda: os.path.exists(stop_path) and os.remove(stop_path))
        self.assertEqual(crew.cleanup(repo, "N1"), {"status": crew._STOPPED})
        self.assertIn("prorab/N1", git(repo, "branch", "--list", "prorab/N1"))
        self.assertEqual(self.j.closed_work_orders, [])

    def test_cleanup_gone_worktree_ignores_a_foreign_recorded_branch(self):
        repo = self.make_repo()
        git(repo, "branch", "prorab/N2")
        git(repo, "branch", "develop")
        for foreign in ("prorab/N2", "develop"):
            self.j.issue_set("N1", "issued", branch=foreign)
            self.assertEqual(crew.cleanup(repo, "N1"),
                             {"status": "absent", "work_order_closed": False})
        self.assertIn("prorab/N2", git(repo, "branch", "--list", "prorab/N2"))
        self.assertIn("develop", git(repo, "branch", "--list", "develop"))
        git(repo, "branch", "prorab/N1")   # its own default-named branch is still taken
        self.assertEqual(crew.cleanup(repo, "N1")["branch"], "prorab/N1")
        self.assertIn("prorab/N2", git(repo, "branch", "--list", "prorab/N2"))

    def test_cleanup_gone_worktree_keeps_the_branch_without_main_or_master(self):
        repo = self.make_repo()
        git(repo, "branch", "prorab/N1")
        git(repo, "branch", "-m", "main", "trunk")   # merged into whatever is checked out
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r, {"status": "kept", "reason": "unmerged", "work_order_closed": False})
        self.assertIn("prorab/N1", git(repo, "branch", "--list", "prorab/N1"))
        self.assertEqual(self.j.closed_work_orders, [])

    def test_cleanup_gone_worktree_keeps_the_work_order_open_when_branch_d_is_refused(self):
        repo = self.make_repo()
        git(repo, "branch", "prorab/N1")
        real = crew._git_soft

        def refusing(args, cwd=None):
            if args[:1] == ["branch"] and args[1:2] == ["-d"]:
                return subprocess.CompletedProcess(["git"] + list(args), 1, "", "cannot delete")
            return real(args, cwd)
        crew._git_soft = refusing
        self.addCleanup(setattr, crew, "_git_soft", real)
        r = crew.cleanup(repo, "N1")
        self.assertEqual((r["status"], r["reason"], r["work_order_closed"]),
                         ("kept", "branch", False))
        self.assertEqual(self.j.closed_work_orders, [])
        self.assertIn("prorab/N1", git(repo, "branch", "--list", "prorab/N1"))

    def test_cleanup_main_checkout_has_no_branch_step(self):
        repo = self.make_repo()
        git(repo, "branch", "prorab/N1")
        self.j.issue_set("N1", "issued", main_checkout=True, branch="prorab/N1")
        self.assertEqual(crew.cleanup(repo, "N1"), {"status": "absent", "work_order_closed": False})
        self.assertIn("prorab/N1", git(repo, "branch", "--list", "prorab/N1"))

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
        self.route("agent send-keys a1", "")

    def test_answer_choice_dialog_keys(self):
        self.route("agent read a1 --source visible", DIALOG)
        self.assertEqual(crew.answer("a1", choice=2), "working")
        self.assertIn("agent send-keys a1 down enter", self.calls())
        self.assertEqual(self.j.act_log[0][:2], ("N1", "answer"))
        self.assertTrue(self.j.act_log[0][2].startswith("choice for crew a1: 2. "))

    def test_answer_recorded_on_the_current_open_stage(self):
        # R56: the spec's stage bead; the first open stage in order is the current one
        self.j.stages["N1"] = ["N1.2", "N1.3"]
        self.route("agent read a1 --source visible", DIALOG)
        crew.answer("a1", choice=2)
        self.route("agent read a1 --source visible", "just a prompt\n")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        crew.answer("a1", text="go on")
        self.assertEqual([(b, k) for b, k, _ in self.j.act_log],
                         [("N1.2", "answer"), ("N1.2", "answer")])
        self.assertEqual(self.j.act_log[1][2], "answer to crew a1: go on")

    def test_answer_without_open_stage_recorded_on_the_work_order(self):
        self.j.stages["N1"] = []
        self.route("agent read a1 --source visible", "just a prompt\n")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        crew.answer("a1", text="go on")
        self.assertEqual(self.j.act_log, [("N1", "answer", "answer to crew a1: go on")])

    def test_answer_moves_up_from_highlighted_option(self):
        shifted = DIALOG.replace("❯ 1.", "  1.").replace("  3. Option C", "❯ 3. Option C")
        self.route("agent read a1 --source visible", shifted)
        crew.answer("a1", choice=1)
        self.assertIn("agent send-keys a1 up up enter", self.calls())

    def test_answer_ignores_numbered_lines_above_dialog(self):
        above = "Summary:\n  1. first\n  2. second\n\n"
        self.route("agent read a1 --source visible", above + DIALOG)
        crew.answer("a1", choice=3)
        self.assertIn("agent send-keys a1 down down enter", self.calls())

    def test_answer_free_text_takes_silent_send_text(self):
        self.route("agent read a1 --source visible", DIALOG)
        self.route("pane send-text w:p9", "")
        crew.answer("a1", text="own answer")
        self.assertIn("pane send-text w:p9 own answer", self.calls())
        self.assertIn("agent send-keys a1 enter", self.calls())

    def test_answer_text_selects_type_something(self):
        self.route("agent read a1 --source visible", DIALOG)
        self.route("pane send-text w:p9", "")
        crew.answer("a1", text="own answer")
        calls = self.calls()
        i_nav = calls.index("agent send-keys a1 down down down")
        i_txt = calls.index("pane send-text w:p9 own answer")
        i_enter = calls.index("agent send-keys a1 enter")
        self.assertTrue(i_nav < i_txt < i_enter)
        self.assertTrue(self.j.act_log[0][2].startswith("answer to crew a1 (free text): "))

    def test_answer_without_dialog_sends_text(self):
        self.route("agent read a1 --source visible", "just a prompt\n")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        crew.answer("a1", text="go on")
        self.assertEqual(self.j.act_log[0][2], "answer to crew a1: go on")
        self.assertIn("agent prompt a1 go on --wait --until working --until done --until blocked --timeout 60000", self.calls())

    def test_answer_refuses_consent_prompt(self):
        for kind, screen in (
                ("trust", "Do you trust the files in this folder?\n1. Yes, I trust this folder\n"),
                ("mcp", "New MCP server found in .mcp.json\n1. Use this MCP server\n"),
                ("tool_permission", "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
                 "Enter to select · ↑/↓ to navigate · Esc to cancel\n")):
            self.route("agent read a1 --source visible", screen)
            self.assertEqual(crew.answer("a1", choice=1), "consent:" + kind)
            self.assertEqual(crew.answer("a1", text="yes"), "consent:" + kind)
        for c in self.calls():
            self.assertFalse(c.startswith(("agent send-keys", "agent prompt", "pane send-text")))
        self.assertEqual(self.j.act_log, [])


    def test_answer_consent_returns_kind(self):
        self.route("agent read a1 --source visible", fixture("consent", "api_key.txt"))
        self.assertEqual(crew.answer("a1", choice=1), "consent:api_key")

    def keys_lines(self):
        return TestKeysLogged.keys_lines(self)

    def test_answer_choice_keys_are_logged(self):
        self.route("agent read a1 --source visible", DIALOG)
        crew.answer("a1", choice=2)
        (line,) = self.keys_lines()
        self.assertEqual((line["target"], line["crew"], line["keys"], line["why"], line["ok"]),
                         ("a1", "a1", ["down", "enter"], "choice", True))

    def test_answer_free_text_keys_are_logged(self):
        self.route("agent read a1 --source visible", DIALOG)
        self.route("pane send-text", "")
        self.route("agent list", ok({"agents": [agent_entry(status="working")]}))
        crew.answer("a1", text="my own words")
        self.assertEqual([(ln["keys"], ln["why"]) for ln in self.keys_lines()],
                         [(["down", "down", "down"], "free_text"), (["enter"], "free_text")])

    def test_answer_presses_nothing_on_a_notice_card(self):
        self.route("agent read a1 --source visible", fixture("notice", "heads_up.txt"))
        with self.assertRaises(ValueError) as cm:
            crew.answer("a1", choice=1)
        self.assertIn("no dialog is open", str(cm.exception))
        self.assertFalse([c for c in self.calls() if "send-keys" in c])

    def test_answer_presses_nothing_on_the_login_screen(self):
        for screen in (fixture("login", "select_method.txt"),
                       fixture("login", "not_logged_in.txt")):
            self.route("agent read a1 --source visible", screen)
            self.assertEqual(crew.answer("a1", choice=1), "login")
            self.assertEqual(crew.answer("a1", text="go on"), "login")
        for c in self.calls():
            self.assertFalse(c.startswith(("agent send-keys", "agent prompt", "pane send-text")))
        self.assertEqual(self.j.act_log, [])

    def test_answer_refuses_limit_screen(self):
        for screen in (fixture("limit_choice.txt"), fixture("limit_screen.txt")):
            self.route("agent read a1 --source visible", screen)
            self.assertEqual(crew.answer("a1", choice=1), "limit_screen")
            self.assertEqual(crew.answer("a1", text="go on"), "limit_screen")
        for c in self.calls():
            self.assertFalse(c.startswith(("agent send-keys", "agent prompt", "pane send-text")))
        self.assertEqual(self.j.act_log, [])

    def test_answer_multiline_text_goes_by_file(self):
        self.route("agent read a1 --source visible", "just a prompt\n")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.answer("a1", text="first\nsecond"), "accepted")
        prompts = [c for c in self.calls() if c.startswith("agent prompt")]
        self.assertEqual(len(prompts), 1)
        self.assertIn("Read the file " + self.home, prompts[0])
        self.assertEqual(os.listdir(os.path.join(self.home, ".cache", "prorab", "work-orders")),
                         ["N1-1.md"])


class TestKeysLogged(CrewBase):
    def keys_lines(self):
        try:
            with open(herdr_cli.keys_log_path(), encoding="utf-8") as fh:
                return [json.loads(ln) for ln in fh.read().splitlines()]
        except FileNotFoundError:
            return []


class TestWaitLimit(CrewBase):
    def setUp(self):
        super().setUp()
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent send-keys a1", "")

    def keys(self):
        return [c for c in self.calls() if c.startswith("agent send-keys")]

    def screens(self):
        """(Upgrade highlighted, Stop and wait highlighted, wait listed second and
        highlighted on Upgrade, wait listed second and highlighted on it)."""
        up = fixture("limit_choice.txt")
        stop = up.replace("  1. Stop", "❯ 1. Stop").replace("❯ 2. Upgrade", "  2. Upgrade")
        swapped_up = up.replace("  1. Stop and wait for limit to reset", "❯ 1. Upgrade your plan") \
            .replace("❯ 2. Upgrade your plan", "  2. Stop and wait for limit to reset")
        swapped_stop = swapped_up.replace("❯ 1. Upgrade", "  1. Upgrade") \
            .replace("  2. Stop and wait", "❯ 2. Stop and wait")
        return up, stop, swapped_up, swapped_stop

    def reads(self):
        return [c for c in self.calls() if c.startswith("agent read")]

    def test_wait_limit_presses_stop_and_wait_only(self):
        up, stop, _, _ = self.screens()
        self.route_seq("agent read a1 --source visible", [up, stop, stop])
        r = crew.wait_limit("a1")
        self.assertEqual(r["status"], "waiting")
        self.assertIsInstance(r["resets_at"], int)
        # "Upgrade" is highlighted: move up, re-read the screen, then enter alone
        self.assertEqual(self.keys(), ["agent send-keys a1 up", "agent send-keys a1 enter"])
        self.assertEqual(self.j.restarts, {})
        self.assertEqual(self.j.kv["crew.a1.limit_until"], str(r["resets_at"]))
        self.assertEqual(self.j.act_log[0][:2], ("N1", "answer"))
        self.assertTrue(self.j.act_log[0][2].startswith(
            "crew waits for the limit reset until "))

    def test_wait_limit_keys_are_logged(self):
        up, stop, _, _ = self.screens()
        self.route_seq("agent read a1 --source visible", [up, stop, stop])
        crew.wait_limit("a1")
        lines = TestKeysLogged.keys_lines(self)
        self.assertEqual([(ln["keys"], ln["why"], ln["crew"]) for ln in lines],
                         [(["up"], "limit_choice", "a1"), (["enter"], "limit_choice", "a1")])

    def test_wait_limit_moves_then_rereads_then_enter(self):
        up, stop, _, _ = self.screens()
        self.route_seq("agent read a1 --source visible", [up, stop])
        crew.wait_limit("a1")
        order = [c.split(" ")[1] for c in self.calls() if c.startswith(("agent read", "agent send-keys"))]
        self.assertEqual(order, ["read", "send-keys", "read", "send-keys"])
        self.assertEqual(self.keys(), ["agent send-keys a1 up", "agent send-keys a1 enter"])

    def test_wait_limit_no_enter_when_highlight_did_not_move(self):
        up, _, _, _ = self.screens()
        self.route_seq("agent read a1 --source visible", [up, up])
        self.assertEqual(crew.wait_limit("a1"), {"status": "unknown_limit_dialog"})
        self.assertEqual(self.keys(), ["agent send-keys a1 up"])  # the move only, never enter
        self.assertEqual(self.j.act_log, [])
        self.assertNotIn("crew.a1.limit_until", self.j.kv)

    def test_wait_limit_moves_down_and_enters_in_place(self):
        _, stop, _, _ = self.screens()
        self.route_seq("agent read a1 --source visible", [stop, stop])
        crew.wait_limit("a1")
        self.assertEqual(self.keys(), ["agent send-keys a1 enter"])  # re-read once, then enter
        self.assertEqual(len(self.reads()), 2)

    def test_wait_limit_moves_down_when_wait_is_listed_after_upgrade(self):
        _, _, swapped_up, swapped_stop = self.screens()
        self.route_seq("agent read a1 --source visible", [swapped_up, swapped_stop])
        crew.wait_limit("a1")
        self.assertEqual(self.keys(), ["agent send-keys a1 down", "agent send-keys a1 enter"])

    def test_wait_limit_plain_message_presses_nothing(self):
        self.route("agent read a1 --source visible", fixture("limit_screen.txt"))
        self.assertEqual(crew.wait_limit("a1")["status"], "waiting")
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.j.restarts, {})

    def test_wait_limit_unknown_dialog_presses_nothing(self):
        screen = ("You've hit your session limit · resets 7pm (UTC)\n\n"
                  "❯ 1. Upgrade your plan\n  2. Buy extra usage\n\n"
                  "Enter to select · ↑/↓ to navigate · Esc to cancel\n")
        self.route("agent read a1 --source visible", screen)
        self.assertEqual(crew.wait_limit("a1"), {"status": "unknown_limit_dialog"})
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.j.act_log, [])

    def test_wait_limit_without_limit_screen(self):
        self.route("agent read a1 --source visible", "just a prompt\n")
        self.assertEqual(crew.wait_limit("a1"), {"status": "no_limit_screen"})
        self.assertEqual(self.keys(), [])


def fingerprint(text):
    import hashlib
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class TestRestart(CrewBase):
    def setUp(self):
        super().setUp()
        self.wt = self.real_wt()
        self.j.kv.update({"crew.a1.worktree": self.wt, "crew.a1.pane": "w:p9",
                          "crew.a1.session": "old-session", "crew.a1.work_order": "N1"})
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": {"pane_id": "w:p9", "foreground_cwd": self.wt}}))
        self.route("pane run w:p9", "")
        self.route("agent start a1", ok({}))
        self.route("pane read w:p9 --source visible", "$ \n")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))

    def go(self, reason="gone"):
        return crew.restart("a1", "N1", "Work order text", reason, caller_pane="w:p1")

    def starts(self):
        return [c for c in self.calls() if c.startswith("agent start")]

    def files_dir(self):
        return os.path.join(self.home, ".cache", "prorab", "work-orders")

    def test_restart_resumes_below_soft(self):
        self.j.kv["crew.a1.delivered"] = fingerprint("Work order text")
        self.assertEqual(self.go()["status"], "resumed")
        self.assertEqual(self.starts(), ["agent start a1 --kind claude --pane w:p9 -- "
                                         "--resume old-session --add-dir %s" % self.files_dir()])
        self.assertEqual(self.j.restarts, {"N1": 1})
        self.assertIn("agent prompt a1 Continue from where you stopped. --wait --until working "
                      "--until done --until blocked --timeout 60000", self.calls())
        self.assertEqual(self.j.kv["crew.a1.session"], "old-session")

    def settings_path(self):
        return os.path.join(self.home, ".cache", "prorab", "crew-settings.json")

    def test_restart_and_handover_pass_settings(self):
        self.j.kv["hooks.enabled"] = "1"
        # a resume
        self.j.kv["crew.a1.delivered"] = fingerprint("Work order text")
        self.assertEqual(self.go()["status"], "resumed")
        self.assertEqual(self.starts(), [
            "agent start a1 --kind claude --pane w:p9 -- --resume old-session --add-dir %s "
            "--settings %s" % (self.files_dir(), self.settings_path())])
        self.assertTrue(os.path.isfile(self.settings_path()))
        os.remove(self.settings_path())
        # a new session
        self.write_transcript("/work", "old-session", "x", tokens=(130000, 0, 0))
        self.j.kv["crew.a1.transcript"] = crew.transcript.transcript_path(
            "/work", "old-session", self.home)
        self.assertEqual(self.go()["status"], "restarted")
        line = self.starts()[-1]
        self.assertTrue(line.endswith(" --add-dir %s --settings %s"
                                      % (self.files_dir(), self.settings_path())), line)
        self.assertTrue(os.path.isfile(self.settings_path()))
        # a handover restart
        os.remove(self.settings_path())
        with mock.patch.object(crew, "_cd_failure", return_value=None), \
                mock.patch.object(crew, "deliver", return_value="accepted"):
            r = crew._restart_after_exit("a1", "N1", "Work order text", self.wt, "w:p9",
                                         {"usd": 0}, 1, 0.01)
        self.assertEqual(r["status"], "restarted")
        self.assertTrue(self.starts()[-1].endswith("--settings %s" % self.settings_path()))
        self.assertTrue(os.path.isfile(self.settings_path()))

    def test_restart_keeps_the_merge_settings(self):
        self.j.kv["hooks.enabled"] = "1"
        self.j.issue_set("N1", "issued", main_checkout=True, merge_branch="prorab/x")
        merge = os.path.join(self.home, ".cache", "prorab", "crew-settings-merge-N1.json")
        # a resume
        self.j.kv["crew.a1.delivered"] = fingerprint("Work order text")
        self.assertEqual(self.go()["status"], "resumed")
        self.assertEqual(self.starts(), [
            "agent start a1 --kind claude --pane w:p9 -- --resume old-session --add-dir %s "
            "--settings %s" % (self.files_dir(), merge)])
        with open(merge, encoding="utf-8") as fh:
            self.assertIn("guard --merge-branch prorab/x", fh.read())
        self.assertFalse(os.path.exists(self.settings_path()))  # the shared file is not written
        # a new session
        self.write_transcript("/work", "old-session", "x", tokens=(130000, 0, 0))
        self.j.kv["crew.a1.transcript"] = crew.transcript.transcript_path(
            "/work", "old-session", self.home)
        self.assertEqual(self.go()["status"], "restarted")
        line = self.starts()[-1]
        self.assertTrue(line.endswith(" --add-dir %s --settings %s" % (self.files_dir(), merge)),
                        line)
        self.assertNotIn(" -w ", line)
        self.assertFalse(os.path.exists(self.settings_path()))

    def test_restart_and_handover_pass_no_settings_while_disabled(self):
        self.j.kv["crew.a1.delivered"] = fingerprint("Work order text")
        self.assertEqual(self.go()["status"], "resumed")
        with mock.patch.object(crew, "_cd_failure", return_value=None), \
                mock.patch.object(crew, "deliver", return_value="accepted"):
            crew._restart_after_exit("a1", "N1", "Work order text", self.wt, "w:p9",
                                     {"usd": 0}, 1, 0.01)
        for line in self.starts():
            self.assertNotIn("--settings", line)
        self.assertFalse(os.path.exists(self.settings_path()))

    def test_restart_new_session_when_soft_or_resume_failed(self):
        # soft context: a new session and the work order with every handover act
        self.write_transcript("/work", "old-session", "x", tokens=(130000, 0, 0))
        self.j.kv["crew.a1.transcript"] = crew.transcript.transcript_path(
            "/work", "old-session", self.home)
        self.j.add_act("N1", "handover", "Act one")
        self.assertEqual(self.go()["status"], "restarted")
        (line,) = self.starts()
        m = re.match(r"^agent start a1 --kind claude --pane w:p9 -- --session-id (\S+) "
                     r"--add-dir (\S+)$", line)
        self.assertIsNotNone(m, line)
        self.assertNotEqual(m.group(1), "old-session")
        self.assertEqual(self.j.kv["crew.a1.session"], m.group(1))
        with open(os.path.join(self.files_dir(), "N1-1.md"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("Work order text", body)
        self.assertIn("Act one", body)
        # resume failed on the screen: fall back to a new session
        os.remove(os.path.join(self.dir, "calls.log"))
        self.j.kv["crew.a1.transcript"] = ""
        self.j.kv["crew.a1.session"] = "old-session"
        self.route("pane read w:p9 --source visible",
                   "No conversation found with session id old-session\n$ \n")
        self.assertEqual(self.go()["status"], "restarted")
        first, second = self.starts()
        self.assertIn("--resume old-session", first)
        self.assertIn("--session-id", second)
        self.assertNotIn("--resume", second)

    def vanished_worktree(self):
        """The worktree is gone (Claude Code removed it on exit); the site is a real repo that
        shows the new one after `-w`."""
        repo = self.make_repo()
        os.rmdir(self.wt)
        self.j.kv["crew.a1.site"] = repo
        self.j.issue_set("N1", "issued", worktree=self.wt, site=repo, base_commit="stale")
        fresh = self.add_worktree(repo, "N1")  # what the new `-w` session creates
        return repo, fresh

    def test_a_vanished_worktree_restarts_with_w_in_the_site(self):
        repo, fresh = self.vanished_worktree()
        self.write_transcript("/work", "old-session", "x", tokens=(130000, 0, 0))
        self.j.kv["crew.a1.transcript"] = crew.transcript.transcript_path(
            "/work", "old-session", self.home)
        self.route("pane get w:p9", ok({"pane": {"pane_id": "w:p9", "foreground_cwd": repo}}))
        self.assertEqual(self.go()["status"], "restarted")
        self.assertIn("pane run w:p9 cd %s" % repo, self.calls())
        self.assertNotIn("pane run w:p9 cd %s" % self.wt, self.calls())
        (start,) = self.starts()
        self.assertIn("-w prorab-N1", start)
        self.assertNotIn("--resume", start)
        self.assertEqual(os.path.realpath(self.j.kv["crew.a1.worktree"]), os.path.realpath(fresh))
        self.assertEqual(self.j.kv["crew.a1.branch"], "prorab/N1")
        self.assertEqual(os.path.realpath(self.j.issue("N1")["worktree"]), os.path.realpath(fresh))
        self.assertEqual(self.j.issue("N1")["base_commit"], git(fresh, "rev-parse", "HEAD").strip())
        self.assertEqual(self.j.kv["crew.a1.base_commit"], self.j.issue("N1")["base_commit"])
        self.assertEqual(self.j.kv["crew.a1.transcript"], crew.transcript.transcript_path(
            self.j.kv["crew.a1.worktree"], self.j.kv["crew.a1.session"]))

    def test_a_vanished_worktree_is_not_resumed(self):
        repo, fresh = self.vanished_worktree()  # below soft: it would resume otherwise
        self.route("pane get w:p9", ok({"pane": {"pane_id": "w:p9", "foreground_cwd": repo}}))
        self.assertEqual(self.go()["status"], "restarted")
        self.assertNotIn("--resume", self.starts()[0])
        self.assertIn("-w prorab-N1", self.starts()[0])

    def test_a_vanished_worktree_and_an_unknown_site_fails_at_cd(self):
        os.rmdir(self.wt)
        r = self.go()
        self.assertEqual((r["status"], r["step"]), ("restart_failed", "cd"))
        self.assertIn("site is unknown", r["reason"])

    def test_sessions_grow_on_a_new_session_never_on_a_resume(self):
        self.j.issue_set("N1", "issued", worktree=self.wt, sessions=["old-session"])
        self.j.kv["crew.a1.delivered"] = fingerprint("Work order text")
        self.assertEqual(self.go()["status"], "resumed")
        self.assertEqual(self.j.issue("N1")["sessions"], ["old-session"])
        self.j.kv["crew.a1.session"] = "old-session"
        self.write_transcript("/work", "old-session", "x", tokens=(130000, 0, 0))
        self.j.kv["crew.a1.transcript"] = crew.transcript.transcript_path(
            "/work", "old-session", self.home)
        self.assertEqual(self.go()["status"], "restarted")
        new = self.j.kv["crew.a1.session"]
        self.assertEqual(self.j.issue("N1")["sessions"], ["old-session", new])
        crew._set_session("a1", self.wt, new, "N1")  # never repeated
        self.assertEqual(self.j.issue("N1")["sessions"], ["old-session", new])

    def test_limit_death_restart_not_counted(self):
        self.assertEqual(self.go("limit_died")["status"], "resumed")
        self.assertEqual(self.j.restarts, {})

    def test_login_restart_not_counted(self):
        self.assertEqual(self.go("login")["status"], "resumed")
        self.assertEqual(self.j.restarts, {})

    def test_restart_stopping_at_a_login_screen(self):
        self.route("agent start a1", err("agent_not_ready"), exit_code=1)
        self.route("pane read w:p9 --source visible", fixture("login", "not_logged_in.txt"))
        self.assertEqual(self.go("login"), {"status": "needs_login", "pane_id": "w:p9"})
        self.assertEqual(self.j.restarts, {})
        self.assertFalse([c for c in self.calls() if c.startswith("agent prompt")])

    def test_restart_over_limit_waits_customer(self):
        self.j.restarts["N1"] = _defaults.load()["monitor"]["max_restarts"]  # via kv_get
        self.assertEqual(self.go("hung"), {"status": "waits_customer"})
        self.assertEqual(len(self.j.gates), 1)
        self.assertEqual(self.calls_or_empty(), [])

    def test_restart_survives_the_silent_pane_run(self):
        self.route("pane run w:p9", "")  # the real herdr prints nothing on success
        self.assertEqual(self.go()["status"], "resumed")
        calls = self.calls()
        i_cd = calls.index("pane run w:p9 cd %s" % self.wt)
        i_start = next(i for i, c in enumerate(calls)
                       if c.startswith("agent start a1 --kind claude --pane w:p9 -- --resume "))
        self.assertTrue(i_cd < i_start)
        self.assertEqual(self.j.restarts, {"N1": 1})

    def test_restart_failure_names_step_and_pane(self):
        self.route("pane run w:p9", "", exit_code=1, stderr="nope")
        r = self.go()
        self.assertEqual(r["status"], "restart_failed")
        self.assertEqual((r["step"], r["pane_id"]), ("cd", "w:p9"))
        self.assertIn("nope", r["reason"])
        self.assertIn("pane run w:p9", r["reason"])

    def test_failed_herdr_call_leaves_counter_and_session(self):
        self.route("pane run w:p9", err("cli_error"), exit_code=1)
        r = self.go("hung")
        self.assertEqual(r["status"], "restart_failed")
        self.assertEqual(self.j.restarts, {})
        self.assertEqual(self.j.kv["crew.a1.session"], "old-session")
        self.route("pane run w:p9", "")
        self.route("agent start a1", err("agent_start_failed"), exit_code=1)
        self.assertEqual(self.go("hung")["status"], "restart_failed")
        self.assertEqual(self.j.restarts, {})
        self.assertEqual(self.j.kv["crew.a1.session"], "old-session")

    def test_delivery_failure_after_agent_start_is_counted_and_recorded(self):
        self.route("agent prompt a1", err("agent_not_found"), exit_code=1)
        # resume path: the old session stays recorded, the restart counts
        r = self.go("hung")
        self.assertEqual(r["status"], "prompt_not_accepted")
        self.assertEqual(self.j.restarts, {"N1": 1})
        self.assertEqual(self.j.kv["crew.a1.session"], "old-session")
        # new-session path: the new session is recorded and counted
        self.write_transcript("/work", "old-session", "x", tokens=(130000, 0, 0))
        self.j.kv["crew.a1.transcript"] = crew.transcript.transcript_path(
            "/work", "old-session", self.home)
        r = self.go("gone")
        self.assertEqual(r["status"], "prompt_not_accepted")
        self.assertEqual(self.j.restarts, {"N1": 2})
        self.assertNotEqual(self.j.kv["crew.a1.session"], "old-session")
        self.assertEqual(self.j.kv["crew.a1.session"], r["session_id"])
        # limit_died is never counted, even after a delivery failure
        self.assertEqual(self.go("limit_died")["status"], "prompt_not_accepted")
        self.assertEqual(self.j.restarts, {"N1": 2})

    def test_restart_unknown_reason(self):
        with self.assertRaises(ValueError):
            self.go("tired")

    def test_restart_in_a_new_split_when_the_pane_is_gone(self):
        self.route("pane get w:p9", err("pane_not_found"), exit_code=1)
        self.route("pane layout --pane w:p1", ok({"layout": {"panes": [
            {"pane_id": "w:p1", "rect": {"width": 200, "height": 40}}]}}))
        self.route("pane split w:p1", ok({"pane": {"pane_id": "w:p12"}}))
        self.route("pane get w:p12", ok({"pane": {"pane_id": "w:p12",
                                                  "foreground_cwd": self.wt}}))
        self.route("pane run w:p12", "")
        self.route("agent start a1", ok({}))
        self.route("pane read w:p12 --source visible", "$ \n")
        self.assertEqual(self.go()["status"], "resumed")
        self.assertEqual(self.j.kv["crew.a1.pane"], "w:p12")
        self.assertIn("--pane w:p12", self.starts()[0])

    def test_restart_consent_screen_maps_like_start(self):
        self.route("agent start a1", err("agent_not_ready"), exit_code=1)
        self.route("pane read w:p9 --source visible", fixture("consent", "mcp.txt"))
        self.assertEqual(self.go(), {"status": "needs_consent", "kind": "mcp",
                                     "pane_id": "w:p9"})
        self.assertFalse([c for c in self.calls() if c.startswith("agent prompt")])
    # ---- a changed work order on resume (F7) ----

    CHANGED = "The work order changed since you last read it: read the file %s, then continue " \
              "from where you stopped."

    def prompts(self):
        return [c for c in self.calls() if c.startswith("agent prompt a1")]

    def test_resume_sends_a_changed_work_order(self):
        self.j.kv["crew.a1.delivered"] = fingerprint("old text")
        r = crew.restart("a1", "N1", "new text", "gone", caller_pane="w:p1")
        self.assertEqual(r["status"], "resumed")
        self.assertTrue(r["work_order_sent"])
        path = os.path.join(self.files_dir(), "N1.md")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "new text")
        (line,) = self.prompts()
        self.assertIn(self.CHANGED % path, line)
        self.assertNotIn("Continue", line)
        self.assertEqual(self.j.kv["crew.a1.delivered"], fingerprint("new text"))

    def test_resume_unchanged_work_order_sends_continue(self):
        self.j.kv["crew.a1.delivered"] = fingerprint("Work order text\n")
        r = self.go()
        self.assertEqual(r["status"], "resumed")
        self.assertFalse(r["work_order_sent"])
        (line,) = self.prompts()
        self.assertIn("Continue from where you stopped.", line)
        self.assertFalse(os.path.exists(os.path.join(self.files_dir(), "N1.md")))

    def test_resume_without_fingerprint_sends_the_work_order(self):
        r = self.go()
        self.assertTrue(r["work_order_sent"])
        (line,) = self.prompts()
        self.assertIn("The work order changed since you last read it", line)
        self.assertEqual(self.j.kv["crew.a1.delivered"], fingerprint("Work order text"))

    def test_resume_not_accepted_keeps_the_old_fingerprint(self):
        self.j.kv["crew.a1.delivered"] = fingerprint("old text")
        with mock.patch.object(crew, "deliver", return_value="stalled"):
            r = crew.restart("a1", "N1", "new text", "gone", caller_pane="w:p1")
        self.assertEqual(r["status"], "prompt_not_accepted")
        self.assertEqual(self.j.kv["crew.a1.delivered"], fingerprint("old text"))

    def test_new_session_restart_records_the_fingerprint_of_the_work_order(self):
        self.write_transcript("/work", "old-session", "x", tokens=(130000, 0, 0))
        self.j.kv["crew.a1.transcript"] = crew.transcript.transcript_path(
            "/work", "old-session", self.home)
        self.j.add_act("N1", "handover", "Act one")
        self.assertEqual(self.go()["status"], "restarted")
        self.assertEqual(self.j.kv["crew.a1.delivered"], fingerprint("Work order text"))

    def test_fingerprint_recorded_on_accepted_delivery(self):
        with mock.patch.object(crew, "deliver", return_value="stalled"):
            crew.send_work_order("a1", "do it\nnow", "N1")
        self.assertNotIn("crew.a1.delivered", self.j.kv)
        with mock.patch.object(crew, "deliver", return_value="accepted"):
            crew.send_work_order("a1", "do it\nnow\n", "N1")
        self.assertEqual(self.j.kv["crew.a1.delivered"], fingerprint("do it\nnow"))

    def test_changed_work_order_block_in_acts_md(self):
        self.assertEqual(crew._template("Changed work order"), self.CHANGED % "{path}")

    def test_complete_handover_restart_records_the_fingerprint_on_accept_only(self):
        with mock.patch.object(crew, "_cd_failure", return_value=None), \
                mock.patch.object(crew, "_start_session", return_value=(None, "")), \
                mock.patch.object(crew, "deliver", return_value="stalled"):
            r = crew._restart_after_exit("a1", "N1", "Work order text", self.wt, "w:p9",
                                         {"usd": 0}, 1, 0.01)
        self.assertEqual(r["status"], "prompt_not_accepted")
        self.assertNotIn("crew.a1.delivered", self.j.kv)
        with mock.patch.object(crew, "_cd_failure", return_value=None), \
                mock.patch.object(crew, "_start_session", return_value=(None, "")), \
                mock.patch.object(crew, "deliver", return_value="accepted"):
            r = crew._restart_after_exit("a1", "N1", "Work order text\n", self.wt, "w:p9",
                                         {"usd": 0}, 1, 0.01)
        self.assertEqual(r["status"], "restarted")
        self.assertEqual(self.j.kv["crew.a1.delivered"], fingerprint("Work order text"))


class TestEmergencyStop(CrewBase):
    def stop_file(self):
        return os.path.join(self.home, ".cache", "prorab", "STOP")

    def touch_stop(self):
        os.makedirs(os.path.dirname(self.stop_file()), exist_ok=True)
        with open(self.stop_file(), "w"):
            pass

    def test_emergency_stop_follows_the_file(self):
        self.assertFalse(crew.emergency_stop())
        self.touch_stop()
        self.assertTrue(crew.emergency_stop())
        os.remove(self.stop_file())
        self.assertFalse(crew.emergency_stop())

    def test_emergency_stop_refuses_starts_and_sends(self):
        self.touch_stop()
        self.j.kv.update({"crew.a1.work_order": "N1", "crew.a1.worktree": "/wt",
                          "crew.a1.pane": "w:p9"})
        refused = {"status": "emergency_stop"}
        self.assertEqual(crew.start(SITE_DIR, "N1", "a1", "w:p1"), refused)
        self.assertEqual(crew.restart("a1", "N1", "text", "gone", caller_pane="w:p1"), refused)
        self.assertEqual(crew.complete_handover("a1", "N1", "text"), refused)
        self.assertEqual(crew.request_handover("a1"), "emergency_stop")
        self.assertEqual(crew.send_work_order("a1", "text", "N1"), "emergency_stop")
        self.assertEqual(crew.answer("a1", text="go on"), "emergency_stop")
        self.assertEqual(crew.resume("a1"), "emergency_stop")
        self.assertEqual(crew.report_fix("a1", "N1", "S1", ["a problem"]), refused)
        self.assertEqual(crew.wait_limit("a1"), refused)
        self.assertEqual(crew.stop("a1", "N1"), refused)
        self.assertEqual(self.calls_or_empty(), [])
        self.assertEqual(self.j.act_log, [])
        self.assertEqual(self.j.iters, {})
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cache", "prorab",
                                                     "work-orders")))
        self.assertEqual(self.j.active, [])

    def test_pause_during_the_emergency_stop_sends_the_emergency_line(self):
        self.touch_stop()
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.pause("a1"), "accepted")
        prompts = [c for c in self.calls() if c.startswith("agent prompt a1")]
        self.assertEqual(len(prompts), 1)
        self.assertIn(crew._template("Emergency pause"), prompts[0])
        self.assertIn("do not retry", prompts[0])
        self.assertNotIn("Finish the current step", prompts[0])

    def test_pause_without_the_stop_keeps_the_pause_line(self):
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.pause("a1"), "accepted")
        prompts = [c for c in self.calls() if c.startswith("agent prompt a1")]
        self.assertEqual(len(prompts), 1)
        self.assertIn("Finish the current step", prompts[0])
        self.assertNotIn("do not retry", prompts[0])

    def test_emergency_stop_still_pauses(self):
        self.touch_stop()
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.pause("a1"), "accepted")
        self.assertTrue(any(c.startswith("agent prompt a1") for c in self.calls()))

    def test_emergency_stop_leaves_the_reads_available(self):
        self.touch_stop()
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent read a1 --source visible", "idle\n")
        self.assertEqual(crew.state("a1")["status"], "idle")
        self.assertEqual(crew.summary("a1"), "")
        self.assertFalse(crew.check_report("", [], [], "/nonexistent")["ok"])
        self.assertEqual(crew.merge_check("/nonexistent", []),
                         {"missing": [], "conflicts": [], "errors": []})

    def test_without_the_file_nothing_is_refused(self):
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.assertEqual(crew.resume("a1"), "accepted")


def make_report(**kw):
    parts = {
        "Summary": "## Summary\nAdded the module.\n",
        "Behaviour": ("## Behaviour\n| Criterion | Status | Reason |\n|---|---|---|\n"
                      "| C1 | done | |\n| C2 | not applicable | no UI in this stage |\n"),
        "Changed files": "## Changed files\n- `src/a.py` - new module\n",
        "Verification": "## Verification\n- `python3 -m unittest` SKIP: 0\n",
        "Stubs": "## Stubs\n- none\n",
        "For the passport": "## For the passport\n- none\n",
        "Residual risks": "## Residual risks\nNone.\n",
    }
    parts.update(kw.get("sections", {}))
    text = "# Stage report\n\n" + "\n".join(v for v in parts.values() if v)
    heavy = kw.get("heavy", "Heavy runs: 3\n")
    guard = kw.get("guard", "Guard refusals: 0\n")
    return text + ("\n" + heavy if heavy else "") + guard + kw.get("tail", "")

class TestCheckReport(CrewBase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.wt = self.add_worktree(self.repo, "N1", "prorab/N1")
        os.makedirs(os.path.join(self.wt, "src"))
        self.commit(self.wt, "src/a.py", "x = 1\n", "add module")
        self.sha = git(self.wt, "rev-parse", "--short=10", "HEAD").strip()

    def check(self, text=None, files=("src/",), criteria=("C1", "C2"), worktree=None):
        text = text if text is not None else make_report(
            tail="\nResult: delivered\nCommit: %s\n" % self.sha)
        return crew.check_report(text, list(criteria), list(files), worktree or self.wt)

    def problems(self, **kw):
        r = self.check(**kw)
        self.assertFalse(r["ok"])
        return r["problems"]

    def has(self, problems, needle):
        self.assertTrue(any(needle in p for p in problems), (needle, problems))

    def test_criteria_ids(self):
        text = "Intro\nC1. first\n- C2: second\n| C3 | third |\n  C4 fourth\nsee C5 here\nC1 again\n"
        self.assertEqual(crew.criteria_ids(text), ["C1", "C2", "C3", "C4"])

    def test_live_criteria_ids(self):
        text = ("C1. plain\n- C2 (live): rates from the bank\n| C3 (live) | device |\n"
                "- **C4** (live): bold\n- C5: not live (live) later\nsee C6 (live)\n"
                "- C7 (LIVE): upper\n")
        self.assertEqual(crew.live_criteria(text), ["C2", "C3", "C4", "C7"])
        self.assertEqual(crew.criteria_ids(text), ["C1", "C2", "C3", "C4", "C5", "C7"])

    def stubs_report(self, stubs, rows=None, verification=None):
        sections = {"Stubs": stubs}
        if rows is not None:
            sections["Behaviour"] = "## Behaviour\n" + rows
        if verification is not None:
            sections["Verification"] = "## Verification\n" + verification
        return make_report(sections=sections,
                           tail="\nResult: delivered\nCommit: %s\n" % self.sha)

    def test_check_report_stubs_section_required(self):
        text = make_report(sections={"Stubs": ""},
                           tail="\nResult: delivered\nCommit: %s\n" % self.sha)
        self.has(self.problems(text=text), "missing section: Stubs")
        self.has(self.problems(text=self.stubs_report("## Stubs\n")),
                 "Stubs: write none or list each stub")

    def passport_report(self, body):
        return make_report(sections={"For the passport": body},
                           tail="\nResult: delivered\nCommit: %s\n" % self.sha)

    def test_check_report_passport_section_required(self):
        self.has(self.problems(text=self.passport_report("")), "missing section: For the passport")
        self.has(self.problems(text=self.passport_report("## For the passport\n")),
                 "For the passport: write none or list each note")

    def test_check_report_returns_the_passport_notes(self):
        for body in ("## For the passport\n- none\n", "## For the passport\nNone.\n"):
            r = self.check(text=self.passport_report(body))
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["passport_notes"], [])
        r = self.check(text=self.passport_report(
            "## For the passport\n- `npx expo install` fails under npm 11 with EALLOWSCRIPTS;"
            " run `npm install` after it\n- the first test run is slow\n"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["passport_notes"], [
            "`npx expo install` fails under npm 11 with EALLOWSCRIPTS; run `npm install` after it",
            "the first test run is slow"])

    def test_check_report_stubs_none_passes(self):
        for body in ("## Stubs\n- none\n", "## Stubs\n- None.\n", "## Stubs\nnone\n"):
            self.assertTrue(self.check(text=self.stubs_report(body))["ok"], body)

    def test_check_report_stub_backing_done_is_refused(self):
        p = self.problems(text=self.stubs_report(
            "## Stubs\n- `src/rates.py` - fixed rate table - C1\n"))
        self.has(p, "criterion C1: done but backed by a stub: `src/rates.py`")

    def test_check_report_stub_without_criterion(self):
        p = self.problems(text=self.stubs_report("## Stubs\n- `src/rates.py` - fixed table\n"))
        self.has(p, "stub item names no criterion: `src/rates.py` - fixed table")

    def test_check_report_stub_status_needs_an_item(self):
        rows = "| C1 | stub | fixed rates are fine here |\n| C2 | done | |\n"
        self.has(self.problems(text=self.stubs_report("## Stubs\n- none\n", rows=rows)),
                 "criterion C1: status stub but no Stubs item names it")
        r = self.check(text=self.stubs_report(
            "## Stubs\n- `src/rates.py` - fixed table - C1\n", rows=rows))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["warnings"], ["criterion C1: stub — accept only if the work order "
                                         "allows it"])
        # a stub status keeps the rule that a reason is needed
        self.has(self.problems(text=self.stubs_report(
            "## Stubs\n- `x` - y - C1\n", rows="| C1 | stub | |\n| C2 | done | |\n")),
            "criterion C1: status 'stub' needs a reason")

    def test_check_report_live_done_needs_live_item(self):
        tail = "\nResult: delivered\nCommit: %s\n" % self.sha

        def run(verification):
            text = make_report(sections={"Verification": "## Verification\n" + verification},
                               tail=tail)
            return crew.check_report(text, ["C1", "C2"], ["src/"], self.wt, live=["C1"])
        r = run("- `python3 -m unittest` SKIP: 0\n")
        self.assertFalse(r["ok"])
        self.has(r["problems"], "criterion C1 (live): done without a LIVE C1 check under "
                                "Verification")
        self.assertTrue(run("- `python3 -m unittest` SKIP: 0\n"
                            "- `rates.py --real` - 1 EUR = 1.08 USD - SKIP: 0 - LIVE C1\n")["ok"])
        # a LIVE item for another criterion does not count
        self.assertFalse(run("- `x` SKIP: 0\n- `y` - out - SKIP: 0 - LIVE C2\n")["ok"])
        # LIVE C<n> counts only as a field of its own, the item's last one
        for item in ("- `y` - mocked, not LIVE C1 - SKIP: 0\n",
                     "- `y` - mocked, not LIVE C1\n- `x` SKIP: 0\n",
                     "- LIVE C1 `y` - out - SKIP: 0\n",
                     "- `y` - out - LIVE C1 - SKIP: 0\n",
                     "- `y` - out - SKIP: 0 LIVE C1\n"):
            self.has(run(item)["problems"], "criterion C1 (live): done without a LIVE C1 check")
        self.assertTrue(run("- `y` - out - SKIP: 0 - **LIVE C1**\n")["ok"])

    def test_check_report_stub_item_names_an_unknown_criterion(self):
        p = self.problems(text=self.stubs_report(
            "## Stubs\n- `src/rates.py` - fixed table - C9\n"))
        self.has(p, "stub item names an unknown criterion: `src/rates.py` - fixed table - C9")
        p = self.problems(text=self.stubs_report(
            "## Stubs\n- `src/rates.py` - fixed table - C1 and C9\n"))
        self.has(p, "stub item names an unknown criterion")

    def test_check_report_stub_criterion_id_is_a_whole_token(self):
        # a path or name holding C<n> names no criterion
        p = self.problems(text=self.stubs_report("## Stubs\n- `src/C3.py` - fixed table\n"))
        self.has(p, "stub item names no criterion: `src/C3.py` - fixed table")
        self.assertFalse(any("unknown criterion" in x for x in p), p)
        r = self.check(text=self.stubs_report(
            "## Stubs\n- `src/C3.py` - fixed table - C1\n",
            rows="| C1 | stub | fixed table is fine |\n| C2 | done | |\n"))
        self.assertTrue(r["ok"], r)
        for item in ("`src/C1.py` - table", "`C1_rates` - table", "`x-C1` - table"):
            self.has(self.problems(text=self.stubs_report("## Stubs\n- %s\n" % item)),
                     "names no criterion")

    def guard_report(self, guard):
        return make_report(guard=guard, tail="\nResult: delivered\nCommit: %s\n" % self.sha)

    def test_check_report_guard_line_required(self):
        self.has(self.problems(text=self.guard_report("")), "no Guard refusals line")
        r = crew.check_report(self.guard_report(""), ["C1", "C2"], ["src/"], self.wt,
                              guard_counted=0)
        self.has(r["problems"], "no Guard refusals line")

    def test_check_report_guard_count_mismatch(self):
        def run(guard, counted):
            return crew.check_report(self.guard_report(guard), ["C1", "C2"], ["src/"], self.wt,
                                     guard_counted=counted)
        self.has(run("Guard refusals: 0\n", 2)["problems"],
                 "Guard refusals: reported 0, the transcript counts 2")
        self.has(run("Guard refusals: 3\n", 1)["problems"],
                 "Guard refusals: reported 3, the transcript counts 1")

    def test_check_report_guard_count_matches(self):
        def run(guard, counted):
            return crew.check_report(self.guard_report(guard), ["C1", "C2"], ["src/"], self.wt,
                                     guard_counted=counted)
        self.assertEqual(run("Guard refusals: 0\n", 0)["warnings"], [])
        self.assertTrue(run("**Guard refusals:** 0\n", 0)["ok"])
        r = run("Guard refusals: 2\n", 2)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("2 refusals of the worktree guard", r["warnings"][0])
        self.assertIn("Residual risks", r["warnings"][0])

    def test_check_report_guard_unverifiable(self):
        r = crew.check_report(self.guard_report("Guard refusals: 0\n"), ["C1", "C2"], ["src/"],
                              self.wt, guard_unverifiable="no transcript")
        self.has(r["problems"], "guard refusals not verifiable: no transcript")

    def test_check_report_ok(self):
        r = self.check()
        self.assertEqual(r, {"ok": True, "problems": [], "warnings": [],
                             "outcome": "delivered", "sha": self.sha, "passport_notes": []})

    def test_check_report_rejects(self):
        self.has(self.problems(text=make_report(
            sections={"Residual risks": ""}, tail="\nResult: delivered\nCommit: %s\n" % self.sha)),
            "missing section: Residual risks")
        self.has(self.problems(text=make_report(
            sections={"Behaviour": "## Behaviour\n| C1 | done | |\n"},
            tail="\nResult: delivered\nCommit: %s\n" % self.sha)), "missing criterion C2")
        self.has(self.problems(text=make_report(
            sections={"Behaviour": "## Behaviour\n| C1 | done | |\n| C2 | deferred | |\n"},
            tail="\nResult: delivered\nCommit: %s\n" % self.sha)), "criterion C2")
        self.has(self.problems(text=make_report(
            sections={"Behaviour": "## Behaviour\n| C1 | done | |\n| C2 | finished | ok |\n"},
            tail="\nResult: delivered\nCommit: %s\n" % self.sha)), "unknown status")
        self.has(self.problems(text=make_report(tail="\nResult: delivered\n")),
                 "no Commit SHA")
        self.has(self.problems(text=make_report()), "no Result")
        self.has(self.problems(text=make_report(tail="\nResult: shipped\n")), "unknown result")

    def test_check_report_heavy_runs(self):
        tail = "\nResult: delivered\nCommit: %s\n" % self.sha

        def run(heavy, **kw):
            text = make_report(heavy=heavy, tail=tail)
            return crew.check_report(text, ["C1", "C2"], ["src/"], self.wt, **kw)
        self.has(run("", heavy_limit=20)["problems"], "no Heavy runs line")
        self.has(run("Heavy runs: 21\n", heavy_limit=20)["problems"], "above the limit of 20")
        self.assertTrue(run("Heavy runs: 21\n", heavy_limit=20, cleared=True)["ok"])
        self.assertTrue(run("Heavy runs: 20\n", heavy_limit=20)["ok"])
        self.assertTrue(run("**Heavy runs:** 0\n", heavy_limit=20)["ok"])
        self.has(run("Heavy runs: 3\n", heavy_limit=20, heavy_counted=5)["problems"],
                 "reported 3, the slots log counts 5")
        # the larger of the reported and the counted value is tested against the limit
        got = run("Heavy runs: 21\n", heavy_limit=20, heavy_counted=25)["problems"]
        self.has(got, "25 above the limit")
        self.assertTrue(run("Heavy runs: 5\n", heavy_limit=20, heavy_counted=5)["ok"])

    def test_check_report_sha_not_on_branch(self):
        git(self.repo, "checkout", "-q", "-b", "other")
        self.commit(self.repo, "o.txt", "o\n", "other branch")
        other = git(self.repo, "rev-parse", "--short=10", "HEAD").strip()
        git(self.repo, "checkout", "-q", "main")
        self.has(self.problems(text=make_report(
            tail="\nResult: delivered\nCommit: %s\n" % other)), "not an ancestor of HEAD")
        self.has(self.problems(text=make_report(
            tail="\nResult: delivered\nCommit: %s\n" % ("0" * 12))), "git failed")

    def test_check_report_empty_list_item_is_a_rejection(self):
        tail = "\nResult: delivered\nCommit: %s\n" % self.sha
        for body in ("- \n- `src/a.py`\n", "- \n", "- ``\n", "-\n- `src/a.py`\n", "-\n"):
            p = self.problems(text=make_report(
                sections={"Changed files": "## Changed files\n" + body}, tail=tail))
            self.has(p, "empty list item under Changed files")
        r = self.check(text=make_report(
            sections={"Changed files": "## Changed files\n- \n"}, tail="\nResult: blocked\n"))
        self.assertFalse(r["ok"])

    def test_check_report_sha_must_be_in_the_branch_range(self):
        # on the default branch only: an ancestor of HEAD, but not one of the crew's commits
        base_sha = git(self.repo, "rev-parse", "--short=10", "main").strip()
        p = self.problems(text=make_report(
            tail="\nResult: delivered\nCommit: %s\n" % base_sha))
        self.has(p, "not in main..HEAD")
        self.has(self.problems(text=make_report(
            tail="\nResult: delivered\nCommit: %s\n" % ("0" * 12))), "git failed")
        # a commit the crew made but the report names by its short form still passes
        self.assertTrue(self.check()["ok"])

    def test_check_report_empty_branch(self):
        wt2 = self.add_worktree(self.repo, "N2", "prorab/N2")
        head = git(wt2, "rev-parse", "--short=10", "HEAD").strip()
        p = self.problems(worktree=wt2, text=make_report(
            tail="\nResult: delivered\nCommit: %s\n" % head))
        self.has(p, "no commit in main..HEAD")

    def test_check_report_non_repo_is_a_refusal(self):
        plain = os.path.join(self.dir, "plain")
        os.makedirs(plain)
        self.has(self.problems(worktree=plain), "git failed")

    def test_check_report_uses_the_default_branch_of_the_site(self):
        git(self.repo, "branch", "-m", "main", "trunk")
        r = self.check()
        self.assertTrue(r["ok"], r)

    def test_check_report_files(self):
        self.has(self.problems(files=("docs/",)), "outside")
        self.has(self.problems(text=make_report(
            sections={"Changed files": "## Changed files\n- `src/b.py`\n"},
            tail="\nResult: delivered\nCommit: %s\n" % self.sha)), "differ")
        self.assertTrue(self.check(files=())["ok"])
        self.assertTrue(self.check(files=("src/*.py",))["ok"])
        self.assertFalse(self.check(files=("src/a.txt",))["ok"])

    def test_check_report_command_without_skip(self):
        text = make_report(sections={"Verification": "## Verification\n- `make test`\n"},
                           tail="\nResult: delivered\nCommit: %s\n" % self.sha)
        self.has(self.problems(text=text), "without SKIP")
        none = make_report(sections={"Verification": "## Verification\nNothing run.\n"},
                           tail="\nResult: delivered\nCommit: %s\n" % self.sha)
        self.has(self.problems(text=none), "no check command")

    def test_check_report_skip_is_warning(self):
        text = make_report(
            sections={"Verification": "## Verification\n- `make test` SKIP: 3\n"},
            tail="\nResult: delivered\nCommit: %s\n" % self.sha)
        r = self.check(text=text)
        self.assertTrue(r["ok"])
        self.assertEqual(r["problems"], [])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("SKIP: 3", r["warnings"][0])

    def test_check_report_other_outcomes_skip_git(self):
        plain = os.path.join(self.dir, "plain")
        os.makedirs(plain)
        r = crew.check_report(make_report(tail="\nResult: blocked\n"), ["C1", "C2"], [], plain)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["outcome"], "blocked")

    def test_report_fix_gate_after_three(self):
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        d = os.path.join(self.home, ".cache", "prorab", "work-orders")
        for n in (1, 2, 3):
            self.assertEqual(crew.report_fix("a1", "N1", "N1.1", ["missing section: Summary"]),
                             {"status": "sent", "attempt": n})
        with open(os.path.join(d, "N1-3.md"), encoding="utf-8") as fh:
            self.assertIn("missing section: Summary", fh.read())
        prompts = [c for c in self.calls() if c.startswith("agent prompt")]
        self.assertEqual(len(prompts), 3)
        self.assertIn("The stage report failed the check; the mismatches are listed in the file",
                      prompts[0])
        self.assertEqual(crew.report_fix("a1", "N1", "N1.1", ["x"]), {"status": "waits_customer"})
        self.assertEqual(len(self.j.gates), 1)
        self.assertEqual(self.j.gates[0][0], "N1.1")
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent prompt")]), 3)


class TestWorkOrderSpend(CrewBase):
    def setUp(self):
        super().setUp()
        self.wt = "/home/user/projects/demo-app/.claude/worktrees/prorab-N1"
        self.j.issue_set("N1", "issued", worktree=self.wt)
        self.write_usage(self.wt, "crew-s", [("m1", "claude-opus-5", 1000000, 100000)])
        self.write_usage(self.wt, "crew-s", [("m2", "claude-haiku-5", 1000000, 0)],
                         subagent="agent-1")

    def test_work_order_spend_reads_worktree_transcripts(self):
        self.j.estimates["N1"] = {"usd": 0.01, "heavy_runs_per_stage": 7}
        r = crew.work_order_spend("N1")
        self.assertAlmostEqual(r["usd"], 6.1)
        self.assertEqual((r["estimate_usd"], r["heavy_runs_per_stage"]), (0.01, 7))
        self.assertTrue(r["over"])
        self.assertAlmostEqual(r["pp_window"], 6.1 / 3.5)
        self.assertAlmostEqual(r["pp_week"], 6.1 / 10.0)
        self.assertEqual(r["rates"]["five_hour"]["source"], "default")
        self.assertEqual(r["rates"]["seven_day"]["usd_per_pp"], 10.0)

    def test_spend_after_a_worktree_exit(self):
        site = "/home/user/projects/demo-app"
        wt = site + "/.claude/worktrees/prorab-N2"
        self.j.issue_set("N2", "issued", worktree=wt, sessions=["moved-s"])
        self.write_usage(site, "moved-s", [("mv1", "claude-opus-5", 1000000, 0)])
        self.write_usage(site, "customer-s", [("cu1", "claude-opus-5", 1000000, 0)])
        r = crew.work_order_spend("N2")
        self.assertGreater(r["usd"], 0)
        self.assertAlmostEqual(r["usd"], 4.0)

    def test_spend_of_a_main_checkout_work_order_counts_its_sessions_only(self):
        site = "/home/user/projects/demo-app"
        self.j.issue_set("N3", "issued", worktree=site, sessions=["merge-s"], main_checkout=True)
        self.write_usage(site, "merge-s", [("mg1", "claude-opus-5", 1000000, 0)])
        self.write_usage(site, "customer-s", [("cu1", "claude-opus-5", 1000000, 0)])
        self.write_usage(site + "/.claude/worktrees/prorab-N9", "other-s",
                         [("ot1", "claude-opus-5", 1000000, 0)])
        self.assertAlmostEqual(crew.work_order_spend("N3")["usd"], 4.0)
        self.j.issue_set("N4", "issued", worktree=site, sessions=["merge-s"])
        self.assertGreater(crew.work_order_spend("N4")["usd"], 4.0)  # a worktree: the whole folder

    def test_work_order_spend_unknown_week_rate(self):
        d = _defaults.load()
        d["rate"]["default_week_usd_per_pp"] = None  # the unknown-week path
        r = crew.work_order_spend("N1", d)
        self.assertIsNone(r["pp_week"])
        self.assertEqual(r["rates"]["seven_day"]["source"], "unknown")

    def test_not_over_within_the_estimate(self):
        self.j.estimates["N1"] = {"usd": 50.0}
        self.assertFalse(crew.work_order_spend("N1")["over"])

    def test_unknown_worktree_is_refused(self):
        with self.assertRaises(ValueError):
            crew.work_order_spend("N2")

    def test_accept_appends_spend_block_and_sets_spent(self):
        self.j.estimates["N1"] = {"usd": 5.0}
        self.j.shift = {"usd": 361.2, "work_orders": {}, "research": {}, "foreman_usd": 0.0}
        r = crew.accept("N1", "N1.1", "Stage accepted.\n")
        self.assertEqual(set(r), {"minutes", "usd", "estimate_usd", "over"})
        self.assertEqual((r["minutes"], r["estimate_usd"], r["over"]), (12.5, 5.0, True))
        self.assertAlmostEqual(r["usd"], 6.1)
        self.assertEqual(self.j.set_spent_log, [("N1", r["usd"])])
        (stage, act), = self.j.accepted
        self.assertEqual(stage, "N1.1")
        self.assertTrue(act.startswith("Stage accepted.\n\nSpend:\n"))
        self.assertIn("- work order N1: $6.10 of $5.00 estimated", act)
        self.assertIn("- shift: $361.20 of $100.00 budget", act)
        self.assertIn("- window rate: 3.50 $/pp (default)", act)

    def test_cli_accept_reads_the_act_file(self):
        self.j.estimates["N1"] = {"usd": 50.0}
        path = os.path.join(self.dir, "act.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Accepted.")
        with mock.patch("sys.stdout") as out:
            self.assertEqual(crew.main(["accept", "N1", "N1.1", "--act-file", path]), 0)
        self.assertEqual(self.j.accepted[0][0], "N1.1")
        self.assertIn('"over": false', "".join(c.args[0] for c in out.write.call_args_list))

    def test_cli_check_report_passes_estimate_clearance_and_counted(self):
        wt = os.path.join(self.dir, "wt-cli")
        os.makedirs(wt)
        self.j.kv["crew.a1.worktree"] = wt
        self.j.kv["stage.N1.1.started_at"] = 100
        self.j.estimates["N1"] = {"heavy_runs_per_stage": 4}
        self.j.bd = lambda args, json_out=True: [{"description": "C1. a\n- C2 (live): b",
                                                  "labels": ["clearance:granted"]}]
        self.route("agent list", ok({"agents": [agent_entry(cwd=wt)]}))
        with mock.patch.object(crew.slots, "runs", return_value=9) as runs, \
                mock.patch.object(crew, "summary", return_value="r"), \
                mock.patch.object(crew.transcript, "session_paths",
                                  return_value=["/home/user/t.jsonl"]) as paths, \
                mock.patch.object(crew.transcript, "guard_refusals", return_value=2) as refs, \
                mock.patch.object(crew, "check_report", return_value={"ok": True}) as chk:
            self.j.issue_set("N1", "issued", sessions=["s1"])
            crew._check_report_cli("a1", "N1", stage="N1.1")
        runs.assert_called_once_with("heavy", wt, since=100.0)
        paths.assert_called_once_with(wt, ["s1"])
        refs.assert_called_once_with("/home/user/t.jsonl", since=100.0)
        kw = chk.call_args.kwargs
        self.assertEqual((kw["heavy_limit"], kw["cleared"], kw["heavy_counted"]), (4, True, 9))
        self.assertEqual((kw["guard_counted"], kw["guard_unverifiable"]), (2, None))
        self.assertEqual(kw["live"], ["C2"])

    def _cli_check(self, stage, started):
        wt = os.path.join(self.dir, "wt-cli")
        os.makedirs(wt, exist_ok=True)
        self.j.kv["crew.a1.worktree"] = wt
        if started is not None:
            self.j.kv["stage.N1.1.started_at"] = started
        self.j.bd = lambda args, json_out=True: [{"description": "C1. a", "labels": []}]
        report = make_report(tail="\nResult: blocked\n")
        with mock.patch.object(crew, "summary", return_value=report):
            return crew._check_report_cli("a1", "N1", stage=stage)

    def test_cli_check_report_without_stage_is_not_verifiable(self):
        r = self._cli_check(None, None)
        self.assertFalse(r["ok"])
        self.assertTrue(any("heavy runs not verifiable" in p and "stage" in p
                            for p in r["problems"]), r["problems"])

    def test_cli_check_report_stage_without_start_is_not_verifiable(self):
        r = self._cli_check("N1.1", None)
        self.assertFalse(r["ok"])
        self.assertTrue(any("heavy runs not verifiable" in p and "start time" in p
                            for p in r["problems"]), r["problems"])

    def test_cli_check_report_verified_count_passes(self):
        with mock.patch.object(crew.slots, "runs", return_value=3), \
                mock.patch.object(crew.transcript, "session_paths",
                                  return_value=["/home/user/t.jsonl"]), \
                mock.patch.object(crew.transcript, "guard_refusals", return_value=0):
            r = self._cli_check("N1.1", 100)
        self.assertTrue(r["ok"], r)

    def test_cli_check_report_counts_the_transcripts_guard_refusals(self):
        with mock.patch.object(crew.slots, "runs", return_value=3), \
                mock.patch.object(crew.transcript, "session_paths",
                                  return_value=["/home/user/t.jsonl"]), \
                mock.patch.object(crew.transcript, "guard_refusals", return_value=2):
            r = self._cli_check("N1.1", 100)
        self.assertTrue(any("reported 0, the transcript counts 2" in p for p in r["problems"]),
                        r["problems"])

    def test_cli_check_report_counts_every_session_of_the_stage(self):
        # a restarted crew: the refusal of the earlier session counts too
        wt = os.path.join(self.dir, "wt-cli")
        os.makedirs(wt, exist_ok=True)
        refusal = {"type": "user", "timestamp": "2026-10-21T10:00:06Z", "message": {"content": [{
            "type": "tool_result", "is_error": True,
            "content": crew.transcript.GUARD_REFUSAL + " /home/user/w, but this command cd's."}]}}
        paths = []
        for sid, n in (("s1", 1), ("s2", 2)):
            paths.append(os.path.join(self.dir, sid + ".jsonl"))
            with open(paths[-1], "w", encoding="utf-8") as fh:
                fh.write(json.dumps(refusal) + "\n")
                if n == 2:
                    fh.write(json.dumps(refusal) + "\n")
        self.j.issue_set("N1", "issued", sessions=["s1", "s2"])
        started = crew.transcript._parse_ts("2026-10-21T09:00:00Z")
        with mock.patch.object(crew.slots, "runs", return_value=3), \
                mock.patch.object(crew.transcript, "session_paths", return_value=paths) as sp:
            r = self._cli_check("N1.1", started)
        sp.assert_called_once_with(wt, ["s1", "s2"])
        self.assertTrue(any("reported 0, the transcript counts 3" in p for p in r["problems"]),
                        r["problems"])

    def test_cli_check_report_unreadable_transcript_is_not_verifiable(self):
        with mock.patch.object(crew.slots, "runs", return_value=3):
            with mock.patch.object(crew.transcript, "session_paths", return_value=[]), \
                    mock.patch.object(crew, "_transcript_for", return_value=None):
                r = self._cli_check("N1.1", 100)
            self.assertTrue(any("guard refusals not verifiable" in p for p in r["problems"]),
                            r["problems"])
            with mock.patch.object(crew.transcript, "session_paths",
                                   return_value=["/home/user/none.jsonl"]):
                r = self._cli_check("N1.1", 100)
        self.assertTrue(any("guard refusals not verifiable" in p for p in r["problems"]),
                        r["problems"])

    def test_cli_check_report_without_stage_guard_is_not_verifiable(self):
        r = self._cli_check(None, None)
        self.assertTrue(any("guard refusals not verifiable" in p and "stage" in p
                            for p in r["problems"]), r["problems"])


class TestAskPredecessor(CrewBase):
    def test_ask_predecessor_refuses_cwd_outside_worktree(self):
        self.j.issue_set("N1", "issued", worktree=os.path.join(self.dir, "wt"))
        with self.assertRaises(ValueError):
            crew.ask_predecessor("N1", "sess-9", os.path.join(self.dir, "elsewhere"), "q")
        with self.assertRaises(ValueError):
            crew.ask_predecessor("N1", "sess-9", os.path.join(self.dir, "wt-sibling"), "q")
        with self.assertRaises(ValueError):
            crew.ask_predecessor("N2", "sess-9", os.path.join(self.dir, "wt"), "q")
        self.assertEqual(self.j.spent, [])

    def test_ask_predecessor_spends_one_run(self):
        bin_dir = os.path.join(self.dir, "bin")
        os.makedirs(bin_dir)
        script = os.path.join(bin_dir, "claude")
        argv_log = os.path.join(self.dir, "argv.txt")
        with open(script, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%%s\\n" "$@" > "%s"\npwd >> "%s"\n'
                     'echo "the answer"\n' % (argv_log, argv_log))
        os.chmod(script, 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        cwd = os.path.join(self.dir, "wt", "sub")
        os.makedirs(cwd)
        self.j.issue_set("N1", "issued", worktree=os.path.join(self.dir, "wt"))
        out = crew.ask_predecessor("N1", "sess-9", cwd, "Why this file?")
        self.assertEqual(out.strip(), "the answer")
        with open(argv_log) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[:5], ["--resume", "sess-9", "--fork-session", "-p",
                                     "Why this file?"])
        self.assertEqual(os.path.realpath(lines[5]), os.path.realpath(cwd))
        self.assertEqual(self.j.spent, [("N1", 0, 1, 0)])


class TestStop(CrewBase):
    def test_stop_exits_unregisters_and_spends(self):
        self.write_transcript("/work", "sess-1", "x", tokens=(1, 2, 3))
        self.j.active = ["a1"]
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        self.route("agent prompt a1", err("agent_not_found"), exit_code=1)
        self.route("pane read w:p9 --source visible", "$ \n")
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9")}))
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "stopped")
        self.assertEqual(self.j.active, [])
        self.assertEqual(self.j.spent, [("N1", 0, 0, 6)])
        self.assertIn("agent prompt a1 /exit --wait --until working --until done --until blocked --timeout 60000", self.calls())
        self.assertNotIn("crew.a1.work_order", self.j.kv)

    def test_stop_records_the_final_spend(self):
        wt = "/home/user/projects/demo-app/.claude/worktrees/prorab-N1"
        self.write_usage(wt, "s1", [("m1", "claude-opus-5", 1000000, 0)])
        self.j.kv["crew.a1.worktree"] = wt
        self.route("agent list", ok({"agents": []}))
        crew.stop("a1", "N1")
        self.assertEqual(len(self.j.set_spent_log), 1)
        self.assertEqual(self.j.set_spent_log[0][0], "N1")
        self.assertAlmostEqual(self.j.set_spent_log[0][1], 4.0)

    def test_stop_clears_session_kv(self):
        self.j.kv.update({"crew.a1.session": "s", "crew.a1.transcript": "t",
                          "crew.a1.limit_until": "1"})
        self.route("agent list", ok({"agents": []}))
        crew.stop("a1", "N1")
        for field in ("session", "transcript", "limit_until"):
            self.assertNotIn("crew.a1." + field, self.j.kv)


class TestStopCloses(CrewBase):
    def stop_it(self):
        self.route("agent list", ok({"agents": []}))
        return crew.stop("a1", "N1")

    def test_stop_closes_after_a_closed_stage(self):
        self.j.closed_stages["N1"] = ["N1.1"]   # accepted or closed by stage-close: none open
        r = self.stop_it()
        self.assertTrue(r["work_order_closed"])
        self.assertEqual(self.j.closed_work_orders, [("N1", "every stage accepted or closed")])

    def test_stop_with_no_stages_says_so(self):
        r = self.stop_it()
        self.assertTrue(r["work_order_closed"])
        self.assertEqual(self.j.closed_work_orders, [("N1", "no stages")])

    def test_stop_leaves_an_open_stage_open(self):
        self.j.stages["N1"] = ["N1.1"]
        self.assertFalse(self.stop_it()["work_order_closed"])
        self.assertEqual(self.j.closed_work_orders, [])


class TestStartPanes(CrewBase):
    """F2: a failed start's pane is reused or closed, never left empty."""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.j.files = {"N1": ["x"]}
        self.route("pane layout --pane w:p1", ok({"layout": {"panes": [
            {"pane_id": "w:p1", "rect": {"width": 200, "height": 40}}]}}))
        self.route("agent start a1", ok({"agent": agent_entry()}))
        self.route("agent list", ok({"agents": []}))

    def splits(self, *panes):
        self.route_seq("pane split w:p1", [ok({"pane": p}) for p in panes])

    def pane_get(self, pane, **kw):
        self.route("pane get " + pane, ok({"pane": pane_info(pane, **kw)}))

    def first_needs_trust(self, pane=None):
        self.write_trust([])
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["status"], "needs_trust")
        self.write_trust([self.repo])
        self.mark = len(self.calls())

    def later(self):
        return self.calls()[self.mark:]

    def test_retry_after_trust_reuses_the_pane(self):
        self.splits(split_pane("w:p9"))
        self.first_needs_trust()
        self.pane_get("w:p9", cwd=self.repo)
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p9")
        self.assertFalse([c for c in self.later() if c.startswith("pane split")])
        self.assertTrue([c for c in self.later()
                         if c.startswith("agent start a1") and "--pane w:p9" in c])

    def test_retry_with_the_customers_claude_running_leaves_the_pane(self):
        self.splits(split_pane("w:p9"), split_pane("w:p10"))
        self.first_needs_trust()
        self.pane_get("w:p9", cwd=self.repo, agent="claude")
        self.route("agent list", ok({"agents": [agent_entry(pane="w:p9", session="theirs")]}))
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        rec = self.j.issue("N1")
        self.assertEqual(rec["pane"], "w:p10")
        self.assertEqual(rec["spare"], [{"pane": "w:p9", "terminal": "term-w:p9", "tab": "w:1"}])
        for c in self.later():
            self.assertFalse(c.startswith("pane close"), c)
            self.assertNotIn("keys", c)

    def test_leftover_session_id_in_a_not_issued_record_is_not_our_session(self):
        self.splits(split_pane("w:p9"), split_pane("w:p10"))
        self.first_needs_trust()
        self.j.issue_set("N1", "not_issued", session_id="old-session")
        self.pane_get("w:p9", cwd=self.repo, agent="claude")
        self.route("agent list", ok({"agents": [
            agent_entry(pane="w:p9", session="old-session")]}))
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertEqual([s["pane"] for s in self.j.issue("N1")["spare"]], ["w:p9"])

    def test_recorded_pane_elsewhere_is_closed_then_split(self):
        self.splits(split_pane("w:p9"), split_pane("w:p10"))
        self.first_needs_trust()
        self.pane_get("w:p9", cwd="/home/user/elsewhere")
        self.route("pane close w:p9", "")
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertIn("pane close w:p9", self.later())

    def test_gone_recorded_pane_splits_anew(self):
        self.splits(split_pane("w:p9"), split_pane("w:p10"))
        self.first_needs_trust()
        self.route("pane get w:p9", err("pane_not_found"), exit_code=1)
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertFalse([c for c in self.later() if c.startswith("pane close")])
        self.assertFalse(self.j.issue("N1").get("spare"))

    def check_recycled(self, **other):
        self.splits(split_pane("w:p9"), split_pane("w:p10"))
        self.first_needs_trust()
        self.pane_get("w:p9", cwd="/home/user/elsewhere", **other)
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertFalse([c for c in self.later() if c.startswith("pane close")])
        self.assertEqual(crew.close_start_panes("N1"), [])

    def test_recycled_terminal_is_not_closed(self):
        self.check_recycled(terminal="term-new")

    def test_recycled_tab_is_not_closed(self):
        self.check_recycled(tab="w:2")

    def test_recycled_pane_id_is_not_closed_by_cleanup(self):
        self.splits(split_pane("w:p9"))
        self.first_needs_trust()
        self.pane_get("w:p9", terminal="term-new")
        self.assertEqual(crew.cleanup(os.path.join(self.dir, "nowhere"), "N1"),
                         {"status": "absent", "work_order_closed": False})
        self.assertFalse([c for c in self.later() if c.startswith("pane close")])
        self.assertIsNone(self.j.issue("N1").get("pane"))

    def test_pane_record_without_terminal_id_is_never_closed(self):
        self.splits({"pane_id": "w:p9", "tab_id": "w:1"}, split_pane("w:p10"))
        self.route("pane get w:p9", ok({"pane": {"pane_id": "w:p9", "tab_id": "w:1",
                                                 "foreground_cwd": "/home/user/elsewhere"}}))
        self.first_needs_trust()
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertFalse([c for c in self.later() if c.startswith("pane close")])
        self.assertEqual(crew.close_start_panes("N1"), [])

    def test_missing_identity_in_the_split_answer_is_read_by_pane_get(self):
        self.splits({"pane_id": "w:p9"})
        self.pane_get("w:p9")
        self.write_trust([])
        crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(self.j.issue("N1")["pane_identity"],
                         {"terminal": "term-w:p9", "tab": "w:1"})

    def test_stop_closes_start_panes_without_agent(self):
        self.j.issue_set("N1", "issued", pane="w:p9",
                         pane_identity={"terminal": "term-w:p9", "tab": "w:1"})
        self.pane_get("w:p9")
        self.route("pane close w:p9", "")
        crew.stop("a1", "N1")
        self.assertIn("pane close w:p9", self.calls())

    def test_stop_and_cleanup_keep_a_pane_with_an_unknown_cwd(self):
        ident = {"terminal": "term-w:p9", "tab": "w:1"}
        self.j.issue_set("N1", "issued", pane="w:p9", pane_identity=ident)
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", cwd=None)}))
        self.route("pane close w:p9", "")
        crew.stop("a1", "N1")
        crew.cleanup(os.path.join(self.dir, "nowhere"), "N1")
        self.assertNotIn("pane close w:p9", self.calls())
        self.assertEqual(self.j.issue("N1")["pane"], "w:p9")

    def test_cleanup_closes_spare_panes_never_one_with_an_agent(self):
        ident = lambda p: {"pane": p, "terminal": "term-" + p, "tab": "w:1"}  # noqa: E731
        self.j.issue_set("N1", "not_issued", spare=[ident("w:p5"), ident("w:p6")])
        self.pane_get("w:p5")
        self.pane_get("w:p6")
        self.route("agent list", ok({"agents": [agent_entry(pane="w:p5", session="theirs")]}))
        self.route("pane close w:p6", "")
        self.route("pane close w:p5", "")
        crew.cleanup(os.path.join(self.dir, "nowhere"), "N1")
        calls = self.calls()
        self.assertNotIn("pane close w:p5", calls)
        self.assertLess(calls.index("pane get w:p6"), calls.index("pane close w:p6"))
        self.assertLess(calls.index("agent list"), calls.index("pane close w:p6"))
        self.assertEqual(self.j.issue("N1")["spare"], [ident("w:p5")])

    def test_unknown_cwd_is_never_closed(self):
        self.splits(split_pane("w:p9"), split_pane("w:p10"))
        self.first_needs_trust()
        self.route("pane get w:p9", ok({"pane": {"pane_id": "w:p9", "terminal_id": "term-w:p9",
                                                 "tab_id": "w:1"}}))
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertFalse([c for c in self.later() if c.startswith("pane close")])
        self.assertEqual([x["pane"] for x in self.j.issue("N1")["spare"]], ["w:p9"])

    def test_failed_close_in_a_start_goes_on_with_a_new_split(self):
        self.splits(split_pane("w:p9"), split_pane("w:p10"))
        self.first_needs_trust()
        self.pane_get("w:p9", cwd="/home/user/elsewhere")
        self.route("pane close w:p9", err("pane_busy"), exit_code=1)
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["pane_id"], "w:p10")
        self.assertEqual([x["pane"] for x in self.j.issue("N1")["spare"]], ["w:p9"])

    def stale_record(self):
        self.j.kv.update({"crew.a1.work_order": "N1", "crew.a1.pane": "w:p9"})
        self.j.active = ["a1"]
        self.j.issue_set("N1", "issued", pane="w:p9",
                         pane_identity={"terminal": "term-w:p9", "tab": "w:1"})
        self.pane_get("w:p9")
        self.route("pane close w:p9", err("pane_busy"), exit_code=1)

    def test_failed_close_does_not_abort_stop(self):
        self.stale_record()
        r = crew.stop("a1", "N1")
        self.assertEqual(r["status"], "stopped")
        self.assertNotIn("crew.a1.work_order", self.j.kv)
        self.assertEqual(self.j.active, [])

    def test_failed_close_does_not_abort_cleanup(self):
        repo = self.repo
        wt = self.add_worktree(repo, "N1")
        self.stale_record()
        self.j.active = []   # stopped: cleanup is refused while a crew of the work order is registered
        r = crew.cleanup(repo, "N1")
        self.assertEqual(r["status"], "removed")  # the worktree step ran
        self.assertEqual(self.j.issue("N1")["pane"], "w:p9")

    def test_agent_check_failure_leaves_the_pane(self):
        self.stale_record()
        self.route("agent list", err("boom"), exit_code=1)
        self.assertEqual(crew.close_start_panes("N1"), [])
        self.assertNotIn("pane close w:p9", self.calls())


class TestExitDialog(CrewBase):
    """Every crew exit answers the worktree exit dialog with Keep (F3)."""
    EXIT_CALL = ("agent prompt a1 /exit --wait --until working --until done "
                 "--until blocked --timeout 60000")

    def setUp(self):
        super().setUp()
        self.wt = self.real_wt()
        self.j.kv.update({"crew.a1.worktree": self.wt, "crew.a1.pane": "w:p9",
                          "crew.a1.session": "old-session", "crew.a1.work_order": "N1"})
        self.j.active = ["a1"]
        self.write_transcript("/work", "sess-1", "Act: done A\nHANDOVER READY")
        self.composer = fixture("composer", "empty.txt")
        self.dialog = fixture("exit", "worktree_exit.txt")
        self.shell = ok({"pane": pane_info("w:p9")})
        self.inwt = ok({"pane": pane_info("w:p9", cwd=self.wt)})
        self.route("agent read a1 --source visible", self.composer)
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        self.route("pane send-keys w:p9", "")
        self.route("pane run w:p9", "")
        self.route("agent start a1", ok({}))

    def listed(self, *present):
        """`agent list` answers in turn: True = a1 listed, False = no agents."""
        bodies = [ok({"agents": [agent_entry()] if p else []}) for p in present]
        if len(bodies) == 1:
            self.route("agent list", bodies[0])
        else:
            self.route_seq("agent list", bodies)

    def screens(self, *texts):
        if len(texts) == 1:
            self.route("pane read w:p9 --source visible", texts[0])
        else:
            self.route_seq("pane read w:p9 --source visible", list(texts))

    def keys(self):
        return [c for c in self.calls() if c.startswith("pane send-keys")]

    def handover(self, timeout_s=2):
        return crew.complete_handover("a1", "N1", "t", timeout_s=timeout_s, poll_s=0.01)

    def test_exit_refuses_a_screen_with_a_prompt_and_types_nothing(self):
        for reason, screen in (("consent:trust", fixture("consent", "trust.txt")),
                               ("login", fixture("login", "not_logged_in.txt")),
                               ("limit_screen", fixture("limit_choice.txt"))):
            self.listed(True)
            self.screens(screen)
            res = self.handover()
            self.assertEqual((res["status"], res["step"], res["reason"]),
                             ("restart_failed", "exit", reason))
        self.assertNotIn(self.EXIT_CALL, self.calls())
        self.assertEqual(self.keys(), [])
        self.assertNotIn("crew.a1.exiting", self.j.kv)

    def test_exit_on_a_clean_screen_still_types_exit(self):
        self.listed(True, False)
        self.screens(self.composer, self.composer)
        self.route_seq("pane get w:p9", [self.shell, self.shell, self.inwt])
        self.assertEqual(self.handover()["status"], "restarted")
        self.assertIn(self.EXIT_CALL, self.calls())

    def test_exit_dialog_markers(self):
        self.assertEqual(crew.exit_dialog(fixture("exit", "worktree_exit.txt")), "worktree")
        self.assertIsNone(crew.exit_dialog(fixture("exit", "worktree_exit_scrolled.txt")))
        self.assertEqual(crew.exit_dialog(fixture("exit", "bg_work.txt")), "bg_work")
        self.assertIsNone(crew.exit_dialog(fixture("composer", "empty.txt")))
        # the marker alone, without the Keep option, is no dialog
        self.assertIsNone(crew.exit_dialog("Exiting worktree session\nsomething else\n"))

    def test_complete_handover_answers_the_exit_dialog_with_1(self):
        self.listed(True, True, False)
        self.screens(self.composer, self.dialog)
        self.route_seq("pane get w:p9", [self.shell, self.shell, self.inwt])
        r = self.handover()
        self.assertEqual(r["status"], "restarted")
        calls = self.calls()
        self.assertEqual(self.keys(), ["pane send-keys w:p9 1"])
        i_exit = calls.index(self.EXIT_CALL)
        i_key = calls.index("pane send-keys w:p9 1")
        i_cd = calls.index("pane run w:p9 cd %s" % self.wt)
        i_start = next(i for i, c in enumerate(calls) if c.startswith("agent start a1"))
        self.assertTrue(i_exit < i_key < i_cd < i_start)

    def keys_lines(self):
        return TestKeysLogged.keys_lines(self)

    def test_exit_dialog_press_is_logged(self):
        self.listed(True, True, False)
        self.screens(self.composer, self.dialog)
        self.route("pane get w:p9", self.shell)
        crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        (line,) = self.keys_lines()
        self.assertEqual((line["keys"], line["why"], line["target"], line["crew"], line["ok"]),
                         (["1"], "worktree_exit", "w:p9", "a1", True))

    def test_bg_work_esc_is_logged(self):
        self.listed(True)
        self.screens(self.composer, fixture("exit", "bg_work.txt"))
        self.route("pane get w:p9", self.shell)
        crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        (line,) = self.keys_lines()
        self.assertEqual((line["keys"], line["why"], line["target"]), (["esc"], "bg_work", "w:p9"))

    def test_exit_without_a_dialog_logs_nothing(self):
        self.listed(True, False)
        self.screens(self.composer, self.composer)
        self.route_seq("pane get w:p9", [self.shell, self.shell, self.inwt])
        self.assertEqual(self.handover()["status"], "restarted")
        self.assertEqual(self.keys_lines(), [])

    def test_handover_after_a_worktree_that_vanished_on_exit(self):
        repo = self.make_repo()
        self.j.kv["crew.a1.site"] = repo
        self.j.issue_set("N1", "issued", worktree=self.wt, site=repo)
        self.listed(True, False)
        self.screens(self.composer, self.composer)
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", cwd=repo)}))
        fresh = []

        def vanish(*a, **k):  # `/exit` removes the worktree; the new `-w` session makes one
            if os.path.isdir(self.wt):
                os.rmdir(self.wt)
            return real_exit(*a, **k)
        real_exit = crew._exit
        real_start = crew._start_session

        def start(name, pane, args, want_screen=False, **kw):
            fresh.append(self.add_worktree(repo, "N1"))
            return real_start(name, pane, args, want_screen, **kw)
        with mock.patch.object(crew, "_exit", vanish), \
                mock.patch.object(crew, "_start_session", start):
            res = self.handover()
        self.assertEqual(res["status"], "restarted")
        self.assertIn("pane run w:p9 cd %s" % repo, self.calls())
        self.assertTrue([c for c in self.calls()
                         if c.startswith("agent start") and "-w prorab-N1" in c])
        self.assertEqual(os.path.realpath(self.j.kv["crew.a1.worktree"]), os.path.realpath(fresh[0]))
        self.assertEqual(os.path.realpath(self.j.issue("N1")["worktree"]), os.path.realpath(fresh[0]))

    def test_failed_press_is_logged_as_failed(self):
        self.route("pane send-keys w:p9", "", exit_code=1, stderr="no such pane")
        self.listed(True)
        self.screens(self.composer, self.dialog)
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", agent="claude")}))
        r = crew.stop("a1", "N1", timeout_s=0.3, poll_s=0.01)
        self.assertEqual(r["status"], "not_exited")
        (line,) = self.keys_lines()
        self.assertEqual((line["keys"], line["why"], line["ok"]), (["1"], "worktree_exit", False))
        self.assertIn("no such pane", line["error"])

    def test_exit_dialog_key_pressed_once(self):
        self.listed(True)
        self.screens(self.composer, self.dialog)
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", agent="claude")}))
        r = self.handover(timeout_s=0.3)
        self.assertEqual(r["status"], "restart_failed")
        self.assertEqual(r["reason"], "agent did not exit")
        self.assertEqual(self.keys(), ["pane send-keys w:p9 1"])
        self.assertNotIn("agent start", " ".join(self.calls()))

    def test_stop_answers_the_exit_dialog(self):
        self.listed(True, True, False)
        self.screens(self.composer, self.dialog)
        self.route("pane get w:p9", self.shell)
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "stopped")
        self.assertEqual(self.keys(), ["pane send-keys w:p9 1"])
        self.assertEqual(self.j.active, [])
        self.assertNotIn("crew.a1.exiting", self.j.kv)

    def test_restart_answers_the_exit_dialog(self):
        self.listed(True, True, False)
        self.route_seq("pane get w:p9", [self.shell, self.inwt])
        self.screens(self.composer, self.dialog, self.composer)
        r = crew.restart("a1", "N1", "t", "gone", caller_pane="w:p1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "resumed")
        self.assertEqual(self.keys(), ["pane send-keys w:p9 1"])

    def test_refused_handover_exit_records_nothing(self):
        self.listed(True)
        self.screens(self.composer, fixture("exit", "bg_work.txt"))
        self.route("pane get w:p9", self.shell)
        self.assertEqual(self.handover()["status"], "bg_shells")
        self.assertNotIn("handover.N1.last", self.j.kv)
        self.assertEqual(self.j.spent, [])
        # the second call exits: one act in all, one session, no handover_loop
        self.listed(True, True, False)
        self.screens(self.composer)
        self.route_seq("pane get w:p9", [self.shell, self.shell, self.inwt])
        self.assertEqual(self.handover()["status"], "restarted")
        self.assertEqual(len(self.j.acts("N1", "handover")), 1)
        self.assertEqual(self.j.spent, [("N1", 1, 0, 0)])
        self.assertEqual(self.j.gates, [])

    def test_stop_after_a_refused_handover_clears_the_act_mark(self):
        self.listed(True)
        self.screens(self.composer, fixture("exit", "bg_work.txt"))
        self.route("pane get w:p9", self.shell)
        self.assertEqual(self.handover()["status"], "bg_shells")
        self.assertIn("crew.a1.handover_act", self.j.kv)
        self.listed(True, True, False)
        self.screens(self.composer)
        self.assertEqual(crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)["status"], "stopped")
        self.assertNotIn("crew.a1.handover_act", self.j.kv)

    def test_restart_after_a_refused_handover_clears_the_act_mark(self):
        self.listed(True)
        self.screens(self.composer, fixture("exit", "bg_work.txt"))
        self.route("pane get w:p9", self.shell)
        self.assertEqual(self.handover()["status"], "bg_shells")
        self.assertIn("crew.a1.handover_act", self.j.kv)
        self.listed(True, True, False)
        self.screens(self.composer)
        self.route_seq("pane get w:p9", [self.shell, self.shell, self.inwt])
        r = crew.restart("a1", "N1", "t", "gone", caller_pane="w:p1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "resumed")
        self.assertNotIn("crew.a1.handover_act", self.j.kv)

    def test_bg_work_marker_needs_the_dialog_shape(self):
        prose = "Background work is running in the other pane, I will wait.\n"
        self.assertIsNone(crew.exit_dialog(prose))

    def test_stop_without_a_pane_exits_nothing(self):
        self.j.kv.pop("crew.a1.pane")
        self.route("agent list", ok({"agents": [dict(agent_entry(), pane_id=None)]}))
        self.assertEqual(crew.stop("a1", "N1", timeout_s=0.2, poll_s=0.01)["status"],
                         "not_exited")
        self.assertEqual(self.j.active, ["a1"])

    def test_exit_with_bg_shells_sends_nothing(self):
        self.listed(True)
        self.screens("bar · 1 shell\n")
        self.route("pane get w:p9", self.shell)
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.stop("a1", "N1", timeout_s=0.3, poll_s=0.01)
        self.assertEqual(r["status"], "bg_shells")
        self.assertEqual(self.j.active, ["a1"])
        self.assertEqual(self.j.kv["crew.a1.work_order"], "N1")
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent prompt")]), 1)
        self.assertNotIn("agent prompt a1 /exit", " ".join(self.calls()))
        r = crew.restart("a1", "N1", "t", "hung", caller_pane="w:p1", timeout_s=0.3,
                         poll_s=0.01)
        self.assertEqual(r["status"], "restart_failed")
        self.assertEqual(r["step"], "exit")
        for c in self.calls():
            self.assertFalse(c.startswith(("pane send-keys", "agent start")), c)
        self.assertEqual(len([c for c in self.calls() if c.startswith("agent prompt")]), 1)
        self.assertNotIn("crew.a1.exiting", self.j.kv)

    END_LINE = ("The foreman is closing this work order: stop every background shell you "
                "started, start nothing new, and end your turn; if one cannot be stopped, "
                "say which in one line.")

    def prompts(self):
        return [c for c in self.calls() if c.startswith("agent prompt")]

    def test_stop_with_shells_asks_the_crew_to_end_them(self):
        self.listed(True)
        self.screens("bar \u00b7 2 shells\n")
        self.route("pane get w:p9", self.shell)
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.stop("a1", "N1", timeout_s=0.5, poll_s=0.01)
        self.assertEqual(r, {"status": "bg_shells", "shells": 2, "asked": "accepted"})
        self.assertEqual(len(self.prompts()), 1)
        self.assertIn(self.END_LINE, self.prompts()[0])
        self.assertNotIn("/exit", self.prompts()[0])
        self.assertEqual(self.j.active, ["a1"])
        self.assertEqual(self.j.closed_work_orders, [])
        self.assertIn("crew.a1.work_order", self.j.kv)

    def test_stop_bg_work_asks_too(self):
        self.listed(True)
        self.screens(self.composer, fixture("exit", "bg_work.txt"))
        self.route("pane get w:p9", self.shell)
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "bg_work")
        self.assertEqual(r["asked"], "accepted")
        self.assertEqual(self.keys(), ["pane send-keys w:p9 esc"])
        self.assertTrue(any(self.END_LINE in c for c in self.prompts()))
        self.assertEqual(self.j.active, ["a1"])

    def _session_with(self, entries):
        d = os.path.join(self.home, ".claude", "projects", crew.transcript.project_slug("/work"))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "sess-1.jsonl"), "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_stop_does_not_resend_the_line_the_crew_has_not_worked_on(self):
        self._session_with([{"type": "user", "message": {"role": "user", "content": self.END_LINE}}])
        self.listed(True)
        self.screens("bar \u00b7 2 shells\n")
        self.route("pane get w:p9", self.shell)
        r = crew.stop("a1", "N1", timeout_s=0.5, poll_s=0.01)
        self.assertEqual(r["status"], "bg_shells")
        self.assertEqual(r["asked"], "already_asked")
        self.assertEqual(self.prompts(), [])

    def test_stop_resends_the_line_after_the_crew_worked(self):
        self._session_with([
            {"type": "user", "message": {"role": "user", "content": self.END_LINE}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}])
        self.listed(True)
        self.screens("bar \u00b7 2 shells\n")
        self.route("pane get w:p9", self.shell)
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        r = crew.stop("a1", "N1", timeout_s=0.5, poll_s=0.01)
        self.assertEqual(r["asked"], "accepted")
        self.assertEqual(len(self.prompts()), 1)

    def test_bg_work_dialog_cancelled_with_esc(self):
        self.listed(True)
        self.screens(self.composer, fixture("exit", "bg_work.txt"))
        self.route("pane get w:p9", self.shell)
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "bg_work")
        self.assertEqual(self.keys(), ["pane send-keys w:p9 esc"])
        self.assertEqual(self.j.active, ["a1"])
        self.assertNotIn("crew.a1.exiting", self.j.kv)
        self.listed(True)
        r = self.handover()
        self.assertEqual(r["status"], "bg_shells")

    def test_exit_without_changes_presses_nothing(self):
        self.listed(True, False)
        self.screens(self.composer, self.composer)
        self.route_seq("pane get w:p9", [self.shell, self.shell, self.inwt])
        self.assertEqual(self.handover()["status"], "restarted")
        self.assertEqual(self.keys(), [])

    def test_answer_refuses_the_exit_dialog(self):
        self.route("agent read a1 --source visible", self.dialog)
        self.assertEqual(crew.answer("a1", choice=1), "exit_dialog")
        self.route("agent read a1 --source visible", fixture("exit", "bg_work.txt"))
        self.assertEqual(crew.answer("a1", text="hello"), "exit_dialog")
        for c in self.calls_or_empty():
            self.assertFalse(c.startswith(("agent send-keys", "pane send", "agent prompt")), c)

    def test_state_shows_the_exit_dialog(self):
        a = agent_entry(status="blocked")
        self.assertEqual(crew.state("a1", a, screen=self.dialog)["exit_dialog"], "worktree")
        self.assertIsNone(crew.state("a1", a, screen=self.composer)["exit_dialog"])
        self.assertIsNone(crew.state("a1", a, read_screen=False)["exit_dialog"])
        self.assertIsNone(crew.gone_state()["exit_dialog"])

    def test_exiting_mark_set_and_cleared(self):
        log = []
        real_set, real_clear = self.j.kv_set, self.j.kv_clear
        self.j.kv_set = lambda k, v: (log.append(("set", k)), real_set(k, v))[1]
        self.j.kv_clear = lambda k: (log.append(("clear", k)), real_clear(k))[1]
        self.listed(True, True, False)
        self.screens(self.composer, self.dialog)
        self.route_seq("pane get w:p9", [self.shell, self.shell, self.inwt])
        self.assertEqual(self.handover()["status"], "restarted")
        self.assertIn(("set", "crew.a1.exiting"), log)
        self.assertLess(log.index(("set", "crew.a1.exiting")),
                        log.index(("clear", "crew.a1.exiting")))
        self.assertNotIn("crew.a1.exiting", self.j.kv)
        # a failed exit clears the mark at once
        log.clear()
        self.listed(True)
        self.screens(self.composer)
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", agent="claude")}))
        self.j.kv.pop("handover.N1.last", None)
        self.assertEqual(self.handover(timeout_s=0.2)["status"], "restart_failed")
        self.assertNotIn("crew.a1.exiting", self.j.kv)


class TestStopClosesPane(CrewBase):
    IDENT = {"terminal": "term-w:p9", "tab": "w:1"}

    def setUp(self):
        super().setUp()
        self.j.active = ["a1"]
        self.j.kv.update({"crew.a1.work_order": "N1", "crew.a1.pane": "w:p9",
                          "crew.a1.pane_identity": json.dumps(self.IDENT)})
        self.route("agent prompt a1", err("agent_not_found"), exit_code=1)
        self.route("pane read w:p9 --source visible", "$ \n")
        self.route("pane close w:p9", "")

    def exiting(self):
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])

    def test_stop_closes_the_crew_pane_after_exit(self):
        self.exiting()
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9")}))
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "stopped")
        self.assertTrue(r["pane_closed"])
        calls = self.calls()
        i = next(k for k, c in enumerate(calls) if c.startswith("agent prompt a1 /exit"))
        self.assertLess(i, calls.index("pane get w:p9"))
        self.assertLess(calls.index("pane get w:p9"), calls.index("pane close w:p9"))

    def test_stop_never_closes_a_pane_with_an_agent(self):
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", agent="claude")}))
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertFalse(r["pane_closed"])
        self.assertNotIn("pane close w:p9", self.calls())

    def test_stop_never_closes_a_pane_listing_another_agent(self):
        self.route("agent list", ok({"agents": [agent_entry(name="other")]}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9")}))
        self.assertFalse(crew._close_crew_pane("a1"))
        self.assertNotIn("pane close w:p9", self.calls())

    def test_stop_leaves_a_pane_whose_identity_changed(self):
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", terminal="term-other")}))
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "stopped")
        self.assertFalse(r["pane_closed"])
        self.assertNotIn("pane close w:p9", self.calls())

    def test_stop_closes_the_pane_when_the_agent_is_already_gone(self):
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9")}))
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertTrue(r["pane_closed"])
        self.assertIn("pane close w:p9", self.calls())
        self.assertNotIn("crew.a1.pane", self.j.kv)

    def test_stop_does_not_close_on_an_unknown_cwd(self):
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", cwd=None)}))
        self.assertFalse(crew.stop("a1", "N1")["pane_closed"])
        self.assertNotIn("pane close w:p9", self.calls())

    def test_a_failed_close_is_not_an_error(self):
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9")}))
        self.route("pane close w:p9", err("pane_busy"), exit_code=1)
        r = crew.stop("a1", "N1")
        self.assertEqual(r["status"], "stopped")
        self.assertFalse(r["pane_closed"])

    def test_stop_leaves_the_pane_when_pane_get_fails(self):
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", err("pane_not_found"), exit_code=1)
        r = crew.stop("a1", "N1", timeout_s=2, poll_s=0.01)
        self.assertEqual(r["status"], "stopped")
        self.assertFalse(r["pane_closed"])
        self.assertNotIn("pane close w:p9", self.calls())

    def test_stop_closes_nothing_while_the_agent_stays(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("pane read w:p9 --source visible", "bar \u00b7 1 shell\n")
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        with mock.patch.object(crew, "_exit", return_value="bg_shells"):
            r = crew.stop("a1", "N1", timeout_s=1, poll_s=0.01)
        self.assertEqual(r, {"status": "bg_shells", "shells": 1, "asked": "accepted"})
        self.assertNotIn("pane close w:p9", self.calls_or_empty())
        self.assertIn("crew.a1.pane", self.j.kv)

    def stop_n1(self):
        self.route("agent list", ok({"agents": []}))
        return crew.stop("a1", "N1")

    def test_stop_closes_the_work_order_when_every_stage_is_accepted(self):
        self.j.closed_stages["N1"] = ["N1.1"]
        r = self.stop_n1()
        self.assertEqual(self.j.closed_work_orders, [("N1", "every stage accepted or closed")])
        self.assertTrue(r["work_order_closed"])

    def test_stop_keeps_a_work_order_with_an_open_stage_open(self):
        self.j.stages["N1"] = ["N1.2"]
        r = self.stop_n1()
        self.assertEqual(self.j.closed_work_orders, [])
        self.assertFalse(r["work_order_closed"])

    def test_stop_keeps_a_work_order_with_an_open_gate_open(self):
        self.j.open_gates.add("N1")
        r = self.stop_n1()
        self.assertEqual(self.j.closed_work_orders, [])
        self.assertFalse(r["work_order_closed"])

    def test_stop_reports_a_refused_close(self):
        self.j.close_fails = True
        self.j.kv["crew.a1.work_order"] = "N1"
        r = self.stop_n1()
        self.assertEqual(r["status"], "stopped")
        self.assertFalse(r["work_order_closed"])
        self.assertIn("open gate", r["close_error"])
        self.assertEqual(r["closed"], ["N1.2"])
        self.assertNotIn("crew.a1.work_order", self.j.kv)

    def test_refused_stop_closes_nothing(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
        with mock.patch.object(crew, "_exit", return_value="bg_shells"):
            r = crew.stop("a1", "N1", timeout_s=1, poll_s=0.01)
        self.assertEqual(r["status"], "bg_shells")
        self.assertEqual(self.j.closed_work_orders, [])

    def test_stop_clears_the_delivered_fingerprint(self):
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9")}))
        self.j.kv["crew.a1.delivered"] = "abc"
        crew.stop("a1", "N1")
        self.assertNotIn("crew.a1.delivered", self.j.kv)


class TestNewSiteTrust(CrewBase):
    """The one folder trust the foreman gives: the site `new_site` made, by its own dialog (15.2)."""
    TRUST_NAME = "site-x-trust"

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.home, "projects")
        os.makedirs(self.projects)
        self.site = self.make_site("site-x")
        self.commit = git(self.site, "rev-parse", "HEAD").strip()
        self.record(self.site)
        self.j.issue_set("N1", "not_issued", pane="w:p9")
        self.write_trust([self.site])
        self.route("agent start %s" % self.TRUST_NAME, err("agent_not_ready"), exit_code=1)
        self.route("agent wait %s" % self.TRUST_NAME, ok({"agent": {"agent_status": "idle"}}))
        self.route("agent prompt %s" % self.TRUST_NAME, ok({"agent": {"agent_status": "working"}}))
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": pane_info("w:p9", cwd=self.site)}))
        self.route("pane send-keys w:p9", "")
        self.composer = fixture("composer", "empty.txt")

    def make_site(self, name, parent=None):
        site = os.path.join(parent or self.projects, name)
        os.makedirs(site)
        git(site, "init", "-q", "-b", "main")
        git(site, "commit", "-q", "--allow-empty", "-m", "Start the site")
        return site

    def site_key(self, site):
        return "site.created.%s" % hashlib.sha1(os.path.realpath(site).encode()).hexdigest()[:16]

    def record(self, site, work_order="N1", commit=None, path=None):
        self.j.kv[self.site_key(site)] = json.dumps({
            "path": path or site, "commit": commit or self.commit, "work_order": work_order,
            "at": "2026-10-23T10:00:00+0000"})

    def dialog(self, site=None, selected=1):
        text = fixture("consent", "trust.txt").replace("/home/user/projects/demo-app",
                                                       site or self.site)
        if selected == 2:
            text = text.replace("> 1. Yes", "  1. Yes").replace("  2. No", "> 2. No")
        return text

    def screens(self, *texts):
        self.route_seq("pane read w:p9 --source visible", list(texts))

    def trust(self):
        return crew._new_site_trust(self.site, "N1", "w:p9")

    def keys(self):
        return [c for c in self.calls() if c.startswith("pane send-keys")]

    def stat_trust(self):
        with open(self.trust_file(), "rb") as fh:
            return fh.read(), os.stat(self.trust_file()).st_mtime_ns

    def assert_no_key(self):
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.keys_lines(), [])

    def after_first_read(self, action):
        """Run `action` once, right after the first screen read of the trust session."""
        real, seen = herdr_cli.pane_visible, []

        def read(pane):
            text = real(pane)
            if not seen:
                seen.append(1)
                action()
            return text
        return mock.patch.object(herdr_cli, "pane_visible", read)

    # the key

    def test_new_site_trust_presses_one_enter_on_its_own_dialog(self):
        self.screens(self.dialog(), self.dialog(), self.composer)
        r = self.trust()
        self.assertEqual(r, {"status": "trusted", "pane_id": "w:p9"})
        self.assertEqual(self.keys(), ["pane send-keys w:p9 enter"])
        (line,) = self.keys_lines()
        self.assertEqual((line["keys"], line["why"], line["target"], line["ok"]),
                         (["enter"], "new-site trust", "w:p9", True))
        start = [c for c in self.calls() if c.startswith("agent start")]
        self.assertEqual(len(start), 1)
        self.assertTrue(start[0].startswith("agent start %s --kind claude --pane w:p9 -- "
                                            "--session-id " % self.TRUST_NAME))
        self.assertNotIn(" -w ", start[0])
        calls = self.calls()
        self.assertLess(calls.index(self.keys()[0]),
                        next(i for i, c in enumerate(calls) if c.startswith("agent prompt")))
        self.assertIn(start[0].split("--session-id ")[1], self.j.issue("N1")["sessions"])

    def test_new_site_trust_other_path_presses_nothing(self):
        self.screens(self.dialog(site="/home/user/projects/other"))
        r = self.trust()
        self.assertEqual((r["status"], r["pane_id"]), ("needs_trust", "w:p9"))
        self.assertTrue(r["reason"])
        self.assert_no_key()

    def test_new_site_trust_prefix_path_presses_nothing(self):
        # the site's path is a prefix of the dialog's: not the same line
        self.screens(self.dialog(site=self.site + "-2"))
        self.assertEqual(self.trust()["status"], "needs_trust")
        self.assert_no_key()

    def test_new_site_trust_path_inside_a_longer_line_presses_nothing(self):
        text = fixture("consent", "trust.txt").replace("/home/user/projects/demo-app",
                                                       "see " + self.site + " here")
        self.screens(text)
        self.assertEqual(self.trust()["status"], "needs_trust")
        self.assert_no_key()

    def test_new_site_trust_other_kind_presses_nothing(self):
        self.screens(fixture("consent", "mcp.txt"))
        self.assertEqual(self.trust(), {"status": "needs_consent", "kind": "mcp",
                                        "pane_id": "w:p9"})
        self.assert_no_key()

    def test_new_site_trust_no_selected_yes_presses_nothing(self):
        self.screens(self.dialog(selected=2))
        r = self.trust()
        self.assertEqual(r["status"], "needs_trust")
        self.assert_no_key()

    def test_new_site_trust_trust_text_with_another_dialog_presses_nothing(self):
        self.screens(self.dialog() + "\n" + fixture("consent", "bypass_warning.txt"))
        self.assertEqual(self.trust()["status"], "needs_trust")
        self.assert_no_key()

    def test_new_site_trust_second_dialog_after_enter_presses_nothing_more(self):
        self.screens(self.dialog(), self.dialog(), fixture("consent", "bypass_warning.txt"))
        self.route("agent wait %s" % self.TRUST_NAME, ok({"agent": {"agent_status": "blocked"}}))
        r = self.trust()
        self.assertEqual(r, {"status": "needs_consent", "kind": "bypass_warning",
                             "pane_id": "w:p9"})
        self.assertEqual(self.keys(), ["pane send-keys w:p9 enter"])
        self.assertEqual(len(self.keys_lines()), 1)
        self.assertEqual(len(self.j.acts("N1", "answer")), 1)  # the Enter is still recorded
        self.assertFalse([c for c in self.calls() if c.startswith("agent prompt")])

    def test_new_site_trust_repo_changes_before_enter_presses_nothing(self):
        self.screens(self.dialog(), self.dialog(), self.composer)

        def dirty():
            with open(os.path.join(self.site, "stray.txt"), "w") as fh:
                fh.write("x\n")
        with self.after_first_read(dirty):
            r = self.trust()
        self.assertEqual(r["status"], "needs_trust")
        self.assertTrue(r["reason"])
        self.assert_no_key()

    def test_new_site_trust_emergency_stop_before_enter_presses_nothing(self):
        self.screens(self.dialog(), self.dialog(), self.composer)
        stop = crew._defaults.load()["emergency"]["stop_path"]

        def halt():
            os.makedirs(os.path.dirname(stop), exist_ok=True)
            with open(stop, "w") as fh:
                fh.write("stop\n")
        with self.after_first_read(halt):
            r = self.trust()
        self.assertEqual(r["status"], "needs_trust")
        self.assert_no_key()
        self.assertEqual(self.j.acts("N1"), [])

    def test_new_site_trust_under_the_emergency_stop_starts_nothing(self):
        with mock.patch.object(crew, "emergency_stop", return_value=True):
            self.assertEqual(self.trust(), {"status": crew._STOPPED})
        self.assertEqual(self.calls_or_empty(), [])

    def test_new_site_trust_unconfirmed_key_is_recorded_and_stops(self):
        # a failed or timed-out send-keys may still have delivered the Enter
        self.screens(self.dialog(), self.dialog(), self.composer)
        self.route("pane send-keys w:p9", err("cli_timeout"), exit_code=1)
        r = self.trust()
        self.assertEqual((r["status"], r["pane_id"]), ("needs_trust", "w:p9"))
        self.assertIn("delivery is not confirmed", r["reason"])
        (text,) = self.j.acts("N1", "answer")
        self.assertTrue(text.startswith(
            "[site] folder trust: Enter sent to w:p9 for %s but its delivery is not "
            "confirmed (" % self.site), text)
        self.assertIn(r["reason"], text)
        self.assertIn("(commit %s)" % self.commit, text)
        self.assertFalse([c for c in self.calls() if c.startswith("agent prompt")])
        (line,) = self.keys_lines()  # one attempt, no retry
        self.assertFalse(line["ok"])
        self.assertEqual(len(self.keys()), 1)

    def test_new_site_trust_act_failure_after_the_key_still_returns_a_result(self):
        self.screens(self.dialog(), self.dialog(), self.composer)
        with mock.patch.object(self.j, "add_act", side_effect=real_journal.JournalError("bd")):
            self.assertEqual(self.trust()["status"], "trusted")
        self.assertEqual(len(self.keys()), 1)

    def test_new_site_trust_path_only_in_scrollback_presses_nothing(self):
        # the site's path stands above the bottom block; the dialog at the bottom is another's
        old = "\n".join(["history line %d" % i for i in range(3)] + [self.site]
                        + ["more history %d" % i for i in range(20)])
        self.screens(old + "\n" + self.dialog(site="/home/user/projects/other"))
        self.assertEqual(self.trust()["status"], "needs_trust")
        self.assert_no_key()

    def test_new_site_trust_real_paths_decide(self):
        link = os.path.join(self.dir, "projects-link")
        os.symlink(self.projects, link)
        # a linked parent that resolves to the projects folder is accepted
        self.assertIsNone(crew._new_site_eligible(os.path.join(link, "site-x"), "N1"))
        # the site path itself a link is refused, even when its target qualifies
        own = os.path.join(self.projects, "own-link")
        os.symlink(self.site, own)
        self.record(own)
        self.assertTrue(crew._new_site_eligible(own, "N1"))

    def test_new_site_trust_with_an_ignored_file_not_eligible(self):
        with open(os.path.join(self.site, ".git", "info", "exclude"), "a") as fh:
            fh.write(".claude/\n")
        os.makedirs(os.path.join(self.site, ".claude"))
        with open(os.path.join(self.site, ".claude", "settings.local.json"), "w") as fh:
            fh.write("{}\n")
        self.assertEqual(git(self.site, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))

    # the outcome

    def test_new_site_trust_flag_missing_is_needs_trust(self):
        self.write_trust(["/home/user/projects/other"])
        self.screens(self.dialog(), self.dialog(), self.composer)
        r = self.trust()
        self.assertEqual((r["status"], r["reason"]), ("needs_trust", "trust not recorded"))
        self.assertEqual(len(self.keys()), 1)

    def test_new_site_trust_ready_without_dialog_presses_nothing(self):
        self.route("agent start %s" % self.TRUST_NAME, ok({"agent": agent_entry(
            name=self.TRUST_NAME)}))
        self.screens(self.composer)
        self.assertEqual(self.trust(), {"status": "trusted", "pane_id": "w:p9"})
        self.assert_no_key()
        self.assertEqual(self.j.acts("N1"), [])

    def test_new_site_trust_ready_without_dialog_flag_missing(self):
        self.write_trust([])
        self.route("agent start %s" % self.TRUST_NAME, ok({"agent": agent_entry(
            name=self.TRUST_NAME)}))
        self.screens(self.composer)
        r = self.trust()
        self.assertEqual((r["status"], r["reason"]), ("needs_trust", "trust not recorded"))
        self.assert_no_key()

    def test_new_site_trust_records_the_act(self):
        self.screens(self.dialog(), self.dialog(), self.composer)
        self.trust()
        (text,) = self.j.acts("N1", "answer")
        self.assertTrue(text.startswith(
            "[site] folder trust accepted by the foreman: %s, created empty by new_site "
            "(commit %s), pane w:p9, " % (self.site, self.commit)), text)
        self.assertEqual(self.j.issue("N1")["pane"], "w:p9")

    def test_new_site_trust_never_writes_trust_path(self):
        self.screens(self.dialog(), self.dialog(), self.composer)
        before = self.stat_trust()
        self.assertEqual(self.trust()["status"], "trusted")
        self.assertEqual(self.stat_trust(), before)

    def test_new_site_trust_trust_session_is_not_registered(self):
        self.screens(self.dialog(), self.dialog(), self.composer)
        self.trust()
        self.assertEqual(self.j.active, [])
        self.assertEqual([k for k in self.j.kv if k.startswith("crew.")], [])

    def test_new_site_trust_name_is_valid_and_short(self):
        for site in ("/p/Site X", "/p/9lives", "/p/" + "a" * 60, "/p/_"):
            name = crew._trust_name(site)
            self.assertTrue(herdr_cli.valid_name(name), name)
            self.assertTrue(name.endswith("-trust"), name)

    # eligibility

    def test_new_site_trust_eligible_site(self):
        self.assertIsNone(crew._new_site_eligible(self.site, "N1"))
        self.assertIsNone(crew._new_site_eligible(self.site + "/", "N1"))

    def test_new_site_trust_without_record_not_eligible(self):
        del self.j.kv[self.site_key(self.site)]
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))
        r = self.trust()
        self.assertEqual((r["status"], r["pane_id"]), ("needs_trust", "w:p9"))
        self.assertEqual(self.calls_or_empty(), [])

    def test_new_site_trust_record_of_another_work_order_not_eligible(self):
        self.record(self.site, work_order="N2")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))

    def test_new_site_trust_record_of_another_path_or_commit_not_eligible(self):
        self.record(self.site, path=self.site + "-2")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))
        self.record(self.site, commit="0" * 40)
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))
        self.j.kv[self.site_key(self.site)] = "not json"
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))

    def test_new_site_trust_with_remote_not_eligible(self):
        git(self.site, "remote", "add", "origin", "https://example.invalid/x.git")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))

    def test_new_site_trust_with_a_second_commit_not_eligible(self):
        git(self.site, "commit", "-q", "--allow-empty", "-m", "more")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))

    def test_new_site_trust_with_a_second_branch_not_eligible(self):
        git(self.site, "branch", "other")
        git(self.site, "checkout", "-q", "other")
        git(self.site, "commit", "-q", "--allow-empty", "-m", "more")
        git(self.site, "checkout", "-q", "main")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))  # rev-list --all sees both

    def test_new_site_trust_with_untracked_file_not_eligible(self):
        os.makedirs(os.path.join(self.site, "d"))
        with open(os.path.join(self.site, "d", "f.txt"), "w") as fh:
            fh.write("x\n")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))

    def test_new_site_trust_with_a_staged_change_not_eligible(self):
        with open(os.path.join(self.site, "f.txt"), "w") as fh:
            fh.write("x\n")
        git(self.site, "add", "f.txt")
        self.assertTrue(crew._new_site_eligible(self.site, "N1"))

    def test_new_site_trust_outside_projects_dir_not_eligible(self):
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.makedirs(elsewhere)
        nested = os.path.join(self.projects, "deeper")
        os.makedirs(nested)
        for parent in (elsewhere, nested):
            site = self.make_site("site-y", parent=parent)
            self.commit = git(site, "rev-parse", "HEAD").strip()
            self.record(site)
            self.assertTrue(crew._new_site_eligible(site, "N1"), parent)

    def test_new_site_trust_symlinked_site_not_eligible(self):
        link = os.path.join(self.projects, "link-x")
        os.symlink(self.site, link)
        self.record(link)
        self.assertTrue(crew._new_site_eligible(link, "N1"))

    def test_new_site_trust_folder_without_a_repo_of_its_own_not_eligible(self):
        site = os.path.join(self.projects, "plain")
        os.makedirs(site)
        self.record(site)
        self.assertTrue(crew._new_site_eligible(site, "N1"))


class TestNewSite(CrewBase):
    """`crew.py new_site`: a site from nothing (15.3), real git in a temporary projects dir."""

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.home, "projects")
        os.makedirs(self.projects)
        self.site = os.path.join(self.projects, "fresh-site")
        p = mock.patch.object(crew, "_take_pane", return_value="w:p7")
        self.take = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(crew, "_new_site_trust",
                              return_value={"status": "trusted", "pane_id": "w:p7"})
        self.trust = p.start()
        self.addCleanup(p.stop)

    def make(self, path=None):
        return crew.new_site(path or self.site, "N1", "w:p1")

    def test_new_site_makes_a_repository_with_one_empty_commit(self):
        r = self.make()
        self.assertEqual(r["status"], "created")
        self.assertEqual(r["site"], self.site)
        self.assertEqual(r["pane_id"], "w:p7")
        self.assertEqual(r["trust"], {"status": "trusted", "pane_id": "w:p7"})
        self.assertEqual(git(self.site, "rev-list", "--all").split(), [r["commit"]])
        self.assertEqual(git(self.site, "rev-parse", "HEAD").strip(), r["commit"])
        self.assertEqual(git(self.site, "branch", "--show-current").strip(), "main")
        self.assertEqual(git(self.site, "log", "-1", "--format=%s").strip(), "Start the site")
        self.assertEqual(git(self.site, "show", "--stat", "--format=", "HEAD").strip(), "")
        self.assertEqual(os.listdir(self.site), [".git"])

    def test_new_site_refuses_an_existing_path(self):
        os.makedirs(self.site)
        self.assertEqual(self.make()["status"], "exists")
        self.assertEqual(os.listdir(self.site), [])
        self.trust.assert_not_called()

    def test_new_site_second_call_for_the_same_path_is_exists_and_presses_nothing(self):
        self.make()
        self.trust.reset_mock()
        self.take.reset_mock()
        self.assertEqual(self.make()["status"], "exists")
        self.trust.assert_not_called()
        self.take.assert_not_called()

    def test_new_site_refuses_outside_projects_dir(self):
        other = os.path.join(self.dir, "elsewhere")
        os.makedirs(other)
        r = self.make(os.path.join(other, "site"))
        self.assertEqual(r["status"], "outside_projects")
        self.assertEqual(os.listdir(other), [])
        r = self.make(os.path.join(self.projects, "sub", "site"))
        self.assertEqual(r["status"], "outside_projects")
        self.assertFalse(os.path.exists(os.path.join(self.projects, "sub")))

    def test_new_site_refuses_a_bad_name(self):
        for bad in ("-site", ".site", "a b", "a;b", "é"):
            r = self.make(os.path.join(self.projects, bad))
            self.assertEqual(r["status"], "bad_name", bad)
            self.assertEqual(os.listdir(self.projects), [], bad)

    def test_new_site_refuses_under_the_emergency_stop(self):
        with mock.patch.object(crew, "emergency_stop", return_value=True):
            self.assertEqual(self.make(), {"status": "emergency_stop"})
        self.assertEqual(os.listdir(self.projects), [])
        self.trust.assert_not_called()

    def test_new_site_records_kv_and_act(self):
        r = self.make()
        key = "site.created.%s" % hashlib.sha1(os.path.realpath(self.site).encode()).hexdigest()[:16]
        rec = json.loads(self.j.kv[key])
        self.assertEqual(set(rec), {"path", "commit", "work_order", "at"})
        self.assertEqual((rec["path"], rec["commit"], rec["work_order"]),
                         (self.site, r["commit"], "N1"))
        acts = self.j.acts("N1")
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0], "[site] created %s empty: git init -b main, commit %s"
                         % (self.site, r["commit"]))

    def test_new_site_record_is_what_eligibility_reads(self):
        self.make()
        self.assertIsNone(crew._new_site_eligible(self.site, "N1"))

    def test_new_site_calls_the_trust_step(self):
        self.trust.return_value = {"status": "needs_trust", "pane_id": "w:p7", "reason": "x"}
        r = self.make()
        self.take.assert_called_once_with("N1", self.site, "w:p1")
        self.trust.assert_called_once_with(self.site, "N1", "w:p7")
        self.assertEqual(r["status"], "created")
        self.assertEqual(r["trust"]["status"], "needs_trust")

    def test_new_site_git_failure_removes_only_its_folder(self):
        real = crew._git_soft
        calls = []

        def broken(args, cwd=None):
            calls.append(args[0])
            if args[0] == "commit":
                return subprocess.CompletedProcess(["git"], 1, "", "no identity")
            return real(args, cwd)
        keep = os.path.join(self.projects, "keep")
        os.makedirs(keep)
        with mock.patch.object(crew, "_git_soft", broken):
            r = self.make()
        self.assertEqual(r["status"], "git_failed")
        self.assertIn("no identity", r["detail"])
        self.assertFalse(os.path.exists(self.site))
        self.assertTrue(os.path.isdir(keep))
        self.assertEqual(self.j.kv_all().keys() & {k for k in self.j.kv if k.startswith("site.")},
                         set())
        self.trust.assert_not_called()

    # round 1: nothing is left behind on a failure before the trust step

    def assert_nothing_left(self):
        self.assertFalse(os.path.exists(self.site))
        self.assertEqual([k for k in self.j.kv if k.startswith("site.")], [])
        self.trust.assert_not_called()

    def assert_rerun_succeeds(self):
        self.assertEqual(self.make()["status"], "created")

    def git_fails_at(self, verb):
        real = crew._git_soft

        def broken(args, cwd=None):
            if args[0] == verb:
                return subprocess.CompletedProcess(["git"], 1, "", "boom " + verb)
            return real(args, cwd)
        return mock.patch.object(crew, "_git_soft", broken)

    def test_new_site_missing_projects_dir_is_mkdir_failed(self):
        shutil.rmtree(self.projects)
        r = self.make()
        self.assertEqual(r["status"], "mkdir_failed")
        self.assertIn("detail", r)
        self.assertFalse(os.path.exists(self.projects))
        self.trust.assert_not_called()

    def test_new_site_unknown_work_order_makes_nothing(self):
        with mock.patch.object(self.j, "bd", side_effect=real_journal.JournalError("no such")):
            self.assertEqual(self.make()["status"], "no_work_order")
        self.assertEqual(os.listdir(self.projects), [])
        with mock.patch.object(self.j, "bd", return_value=[]):
            self.assertEqual(self.make()["status"], "no_work_order")
        with mock.patch.object(self.j, "bd", return_value=[{"id": "N1", "status": "closed"}]):
            self.assertEqual(self.make()["status"], "no_work_order")
        self.assertEqual(os.listdir(self.projects), [])

    def test_new_site_init_failure_leaves_nothing(self):
        with self.git_fails_at("init"):
            r = self.make()
        self.assertEqual(r["status"], "git_failed")
        self.assertIn("boom init", r["detail"])
        self.assert_nothing_left()
        self.assert_rerun_succeeds()

    def test_new_site_rev_parse_failure_leaves_nothing(self):
        with self.git_fails_at("rev-parse"):
            r = self.make()
        self.assertEqual(r["status"], "git_failed")
        self.assert_nothing_left()
        self.assert_rerun_succeeds()

    def test_new_site_missing_git_binary_leaves_nothing(self):
        with mock.patch.object(crew.subprocess, "run", side_effect=FileNotFoundError("git")):
            r = self.make()
        self.assertEqual(r["status"], "git_failed")
        self.assert_nothing_left()
        self.assert_rerun_succeeds()

    def test_new_site_failing_act_leaves_nothing(self):
        with mock.patch.object(self.j, "add_act", side_effect=real_journal.JournalError("closed")):
            r = self.make()
        self.assertEqual(r["status"], "act_failed")
        self.assertIn("closed", r["detail"])
        self.assert_nothing_left()
        self.take.assert_not_called()
        self.assert_rerun_succeeds()

    def test_new_site_kv_failure_leaves_nothing(self):
        with mock.patch.object(self.j, "kv_set", side_effect=real_journal.JournalError("bd")):
            r = self.make()
        self.assertEqual(r["status"], "record_failed")
        self.assert_nothing_left()
        self.assert_rerun_succeeds()

    def test_new_site_take_pane_failure_leaves_nothing(self):
        self.take.side_effect = herdr_cli.HerdrError("no pane")
        r = self.make()
        self.assertEqual(r["status"], "pane_failed")
        self.assert_nothing_left()
        self.take.side_effect = None
        self.assert_rerun_succeeds()

    # confinement

    def test_new_site_relative_path_resolves_against_cwd(self):
        old = os.getcwd()
        os.chdir(self.projects)
        self.addCleanup(os.chdir, old)
        self.assertEqual(crew.new_site("rel-site", "N1", "w:p1")["status"], "created")
        self.assertTrue(os.path.isdir(os.path.join(self.projects, "rel-site", ".git")))

    def test_new_site_dotdot_cannot_leave_projects_dir(self):
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        r = self.make(os.path.join(self.projects, "..", "other", "site"))
        self.assertEqual(r["status"], "outside_projects")
        self.assertEqual(os.listdir(other), [])
        r = self.make(os.path.join(self.projects, "..", "projects", "site2"))
        self.assertEqual(r["status"], "created")
        self.assertTrue(os.path.isdir(os.path.join(self.projects, "site2", ".git")))

    def test_new_site_trailing_slash(self):
        r = self.make(self.site + "/")
        self.assertEqual(r["status"], "created")
        self.assertEqual(r["site"], self.site)

    def test_new_site_symlinked_parent(self):
        link = os.path.join(self.dir, "plink")
        os.symlink(self.projects, link)
        r = self.make(os.path.join(link, "via-link"))
        self.assertEqual(r["status"], "created")
        self.assertTrue(os.path.isdir(os.path.join(self.projects, "via-link", ".git")))
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.makedirs(elsewhere)
        bad = os.path.join(self.dir, "blink")
        os.symlink(elsewhere, bad)
        self.assertEqual(self.make(os.path.join(bad, "x"))["status"], "outside_projects")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_new_site_dangling_symlink_name_is_exists(self):
        os.symlink(os.path.join(self.dir, "nowhere"), self.site)
        self.assertEqual(self.make()["status"], "exists")
        self.assertTrue(os.path.islink(self.site))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "nowhere")))

    def test_cli_new_site(self):
        with mock.patch.object(crew, "new_site", return_value={"status": "created"}) as n, \
                mock.patch("builtins.print"):
            code = crew.main(["new_site", "/home/user/projects/x", "N1", "w:p1"])
        self.assertEqual(code, 0)
        n.assert_called_once_with(site_path="/home/user/projects/x", work_order="N1",
                                  caller_pane="w:p1")


class TestMainCheckout(CrewBase):
    """`start --main-checkout`: the crew that merges a branch in the site's main checkout
    (15.4). Real git for the site, the fake herdr for the pane."""

    DECISION = '[customer decision] merge prorab/x ("merge it into main")'

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        git(self.repo, "checkout", "-q", "-b", "prorab/x")
        self.commit(self.repo, "x.txt", "x\n", "work on x")
        git(self.repo, "checkout", "-q", "main")
        self.head = git(self.repo, "rev-parse", "HEAD").strip()
        self.j.kv["hooks.enabled"] = "1"
        self.j.files = {"N1": ["x.txt"]}
        self.j.add_act("N1", "answer", self.DECISION)
        self.route("pane layout --pane w:p1", ok({"layout": {"panes": [
            {"pane_id": "w:p1", "rect": {"width": 200, "height": 40}}]}}))
        self.route("pane split w:p1", ok({"pane": split_pane("w:p9")}))
        self.route("agent start a1", ok({"agent": agent_entry()}))
        self.route("agent list", ok({"agents": [agent_entry()]}))

    def go(self, work_order="N1", name="a1"):
        return crew.start(self.repo, work_order, name, "w:p1", main_checkout=True)

    def refused(self, status):
        r = self.go()
        self.assertEqual(r["status"], status)
        self.assertEqual(self.calls_or_empty(), [])  # nothing was started, not even a pane
        self.assertIsNone(self.j.issue("N1"))
        self.assertNotIn("a1", self.j.active)
        return r

    def start_line(self):
        return [c for c in self.calls() if c.startswith("agent start a1")][0]

    def shared_settings(self):
        return os.path.join(self.home, ".cache", "prorab", "crew-settings.json")

    def merge_settings(self, work_order="N1"):
        return os.path.join(self.home, ".cache", "prorab",
                            "crew-settings-merge-%s.json" % work_order)

    def test_start_main_checkout_needs_hooks(self):
        del self.j.kv["hooks.enabled"]
        self.refused("hooks_off")
        self.j.kv["hooks.enabled"] = "0"
        self.refused("hooks_off")
        self.assertFalse(os.path.exists(self.merge_settings()))

    def test_start_main_checkout_needs_the_merge_decision(self):
        self.j.act_log.clear()
        self.refused("no_merge_decision")
        self.j.add_act("N1", "answer", "[customer decision] drop (customer's words)")
        self.j.add_act("N1", "handover", self.DECISION)  # only an `answer` act counts
        self.j.add_act("N2", "answer", self.DECISION)  # only this work order's
        self.j.add_act("N1", "answer", "[customer decision] mergeprorab/x")  # not the marker
        self.refused("no_merge_decision")

    def test_start_main_checkout_takes_the_latest_decision(self):
        git(self.repo, "branch", "prorab/y")
        self.j.add_act("N1", "answer", "[customer decision] merge prorab/y (changed my mind)")
        r = self.go()
        self.assertEqual(r["merge_branch"], "prorab/y")
        self.assertEqual(self.j.issue("N1")["merge_branch"], "prorab/y")

    def test_start_main_checkout_refuses_a_missing_branch(self):
        self.j.act_log.clear()
        self.j.add_act("N1", "answer", "[customer decision] merge prorab/none (words)")
        self.refused("no_branch")
        git(self.repo, "branch", "master", "HEAD")
        for branch in ("main", "master"):
            self.j.add_act("N1", "answer", "[customer decision] merge %s (words)" % branch)
            self.refused("no_branch")
        self.j.add_act("N1", "answer", "[customer decision] merge")
        self.refused("no_branch")

    def test_start_main_checkout_refuses_an_option_like_branch(self):
        for bad in ("-x", "--all", "a b", "a;b", "$(x)", "a..b"):
            self.j.add_act("N1", "answer", "[customer decision] merge %s (words)" % bad)
            self.refused("no_branch")

    def test_start_main_checkout_refuses_the_checked_out_branch(self):
        git(self.repo, "checkout", "-q", "prorab/x")
        self.refused("no_branch")  # no_branch comes before not_default_branch

    def test_start_main_checkout_refuses_a_non_default_checkout(self):
        git(self.repo, "checkout", "-q", "-b", "develop")
        self.refused("not_default_branch")
        git(self.repo, "checkout", "-q", "--detach")
        self.refused("not_default_branch")

    def test_start_main_checkout_accepts_master(self):
        git(self.repo, "branch", "-m", "main", "master")
        self.assertEqual(self.go()["status"], "started")

    def test_start_main_checkout_refuses_a_dirty_main_checkout(self):
        with open(os.path.join(self.repo, "new.txt"), "w") as fh:
            fh.write("u\n")
        self.refused("main_dirty")
        os.remove(os.path.join(self.repo, "new.txt"))
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("changed\n")
        self.refused("main_dirty")
        git(self.repo, "checkout", "--", "f.txt")
        os.makedirs(os.path.join(self.repo, "sub"))
        with open(os.path.join(self.repo, "sub", "n.txt"), "w") as fh:
            fh.write("u\n")
        self.refused("main_dirty")  # an untracked file inside a new folder is listed by name
        shutil.rmtree(os.path.join(self.repo, "sub"))
        # entries under .claude/worktrees/ are the crews' own and do not count
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees", "x"))
        with open(os.path.join(self.repo, ".claude", "worktrees", "x", "y.txt"), "w") as fh:
            fh.write("u\n")
        self.assertEqual(self.go()["status"], "started")

    def test_start_main_checkout_refuses_a_busy_main_checkout(self):
        self.j.active = ["b0"]
        self.j.kv.update({"crew.b0.work_order": "N0", "crew.b0.worktree": self.repo,
                          "crew.b0.site": self.repo})
        r = self.refused("main_busy")
        self.assertEqual(r.get("with"), ["b0"])
        # a crew in a worktree of the same site does not make it busy
        wt = self.add_worktree(self.repo, "N0")
        self.j.kv["crew.b0.worktree"] = wt
        self.j.files["N0"] = ["other.txt"]
        self.assertEqual(self.go()["status"], "started")

    def test_start_main_checkout_records_before_agent_start(self):
        self.route("agent start a1", err("cli_error"), exit_code=1)
        with self.assertRaises(herdr_cli.HerdrError):
            self.go()
        rec = self.j.issue("N1")
        self.assertEqual(rec["state"], "issuing")
        self.assertIs(rec["main_checkout"], True)
        self.assertEqual(rec["merge_branch"], "prorab/x")
        self.assertEqual(rec["site"], self.repo)

    def test_start_main_checkout_starts_without_w(self):
        r = self.go()
        self.assertEqual(r["status"], "started")
        self.assertEqual((r["worktree"], r["branch"], r["main_checkout"], r["merge_branch"]),
                         (self.repo, "main", True, "prorab/x"))
        m = re.match(r"^agent start a1 --kind claude --pane w:p9 -- --session-id (\S+) "
                     r"--add-dir (\S+) --settings (\S+)$", self.start_line())
        self.assertIsNotNone(m, self.start_line())
        self.assertEqual(m.group(2), os.path.join(self.home, ".cache", "prorab", "work-orders"))
        self.assertEqual(m.group(3), self.merge_settings())
        self.assertNotIn(" -w ", self.start_line())
        self.assertIn("pane split w:p1 --direction right --no-focus --cwd %s" % self.repo,
                      self.calls())
        self.assertEqual(self.j.kv["crew.a1.worktree"], self.repo)
        self.assertEqual(self.j.kv["crew.a1.branch"], "main")
        self.assertEqual(self.j.kv["crew.a1.transcript"],
                         crew.transcript.transcript_path(self.repo, m.group(1)))
        self.assertIn("a1", self.j.active)
        self.assertEqual(self.j.issue("N1")["sessions"], [m.group(1)])

    def test_start_main_checkout_records_head_as_base(self):
        r = self.go()
        self.assertEqual(self.j.kv["crew.a1.base_commit"], self.head)
        rec = self.j.issue("N1")
        self.assertEqual((rec["base_commit"], rec["worktree"]), (self.head, self.repo))
        self.assertEqual(r["status"], "started")

    def test_start_main_checkout_merge_settings_do_not_touch_the_shared_file(self):
        shared = crew.hooks.write_settings()
        with open(shared, "rb") as fh:
            before = fh.read()
        self.go()
        with open(shared, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertNotIn(b"--merge-branch", before)
        self.assertEqual(stat.S_IMODE(os.stat(self.merge_settings()).st_mode), 0o600)
        with open(self.merge_settings(), encoding="utf-8") as fh:
            self.assertIn("guard --merge-branch prorab/x", fh.read())
        # another crew's start passes the shared file, without the option
        self.add_worktree(self.repo, "N2")
        self.j.files["N2"] = ["y.txt"]
        self.route("agent start a2", ok({"agent": agent_entry(name="a2")}))
        self.route("agent list", ok({"agents": [agent_entry(), agent_entry(name="a2")]}))
        self.route("pane split w:p1", ok({"pane": split_pane("w:p10")}))
        r = crew.start(self.repo, "N2", "a2", "w:p1")
        self.assertEqual(r["status"], "started")
        line = [c for c in self.calls() if c.startswith("agent start a2")][0]
        self.assertTrue(line.endswith(" --settings %s" % self.shared_settings()), line)
        with open(self.shared_settings(), "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_start_without_the_flag_forgets_an_earlier_merge_record(self):
        self.j.issue_set("N1", "not_issued", main_checkout=True, merge_branch="prorab/x")
        self.add_worktree(self.repo, "N1")
        r = crew.start(self.repo, "N1", "a1", "w:p1")
        self.assertEqual(r["status"], "started")
        self.assertIn(" -w prorab-N1", self.start_line())
        self.assertFalse(self.j.issue("N1").get("main_checkout"))
        self.assertTrue(self.start_line().endswith("--settings %s" % self.shared_settings()))

    def test_start_main_checkout_consent_answered_path_registers_the_site(self):
        self.j.issue_set("N1", "issuing", name="a1", pane="w:p9", session_id="sess-1",
                         sessions=["sess-1"], site=self.repo, main_checkout=True,
                         merge_branch="prorab/x")
        self.route("pane read w:p9 --source visible", "plain screen\n")
        r = crew.start(self.repo, "N1", "a1", "w:p1")  # the record says what this start is
        self.assertEqual((r["status"], r["worktree"], r["branch"]), ("started", self.repo, "main"))
        self.assertEqual(self.j.kv["crew.a1.base_commit"], self.head)
        self.assertEqual([c for c in self.calls() if c.startswith("agent start")], [])
        self.assertEqual(self.j.issue("N1")["base_commit"], self.head)

    def test_recorded_base_of_a_second_merge_work_order(self):
        first = self.head
        self.j.issue_set("N1", "issued", worktree=self.repo, base_commit=first, main_checkout=True)
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "prorab/x")
        second = git(self.repo, "rev-parse", "HEAD").strip()
        self.j.issue_set("N2", "issued", worktree=self.repo, base_commit=second,
                         main_checkout=True)
        real, kv = os.path.realpath(self.repo), self.j.kv_all()
        self.assertEqual(crew._recorded_base_of(real, kv, "N1"), first)
        self.assertEqual(crew._recorded_base_of(real, kv, "N2"), second)
        self.assertIsNone(crew._recorded_base_of(real, kv))  # no work order: not guessed
        self.assertIsNone(crew._recorded_base_of(real, kv, "N3"))
        self.assertEqual(crew._resolve_base(self.repo, None, work_order="N2"), second)
        self.assertEqual(crew._resolve_base(self.repo, None, work_order="N1"), first)

    def test_cleanup_main_checkout_work_order_touches_no_branch(self):
        wt = self.add_worktree(self.repo, "N1", "prorab/N1")  # merged, would be removed
        self.j.issue_set("N1", "issued", name="a1", main_checkout=True, merge_branch="prorab/x")
        r = crew.cleanup(self.repo, "N1")
        self.assertEqual(r, {"status": "absent", "work_order_closed": False})
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("prorab/N1", git(self.repo, "branch", "--list", "prorab/N1"))
        self.assertIn("prorab/x", git(self.repo, "branch", "--list", "prorab/x"))
        self.assertEqual(self.j.closed_work_orders, [])
        # a drop decision still closes it, and still removes nothing
        self.j.add_act("N1", "answer", crew.DROP_MARKER + " (customer's words)")
        r = crew.cleanup(self.repo, "N1")
        self.assertEqual(r["status"], "absent")
        self.assertTrue(r["work_order_closed"])
        self.assertTrue(os.path.isdir(wt))

    def merge_record(self):
        self.j.issue_set("N1", "issued", worktree=self.repo, base_commit=self.head,
                         main_checkout=True, merge_branch="prorab/x")

    def merge_check(self, files=("x.txt",), changed="- `x.txt` - merged\n"):
        sha = git(self.repo, "rev-parse", "--short=10", "HEAD").strip()
        text = make_report(sections={"Changed files": "## Changed files\n" + changed},
                           tail="\nResult: delivered\nCommit: %s\n" % sha)
        return crew.check_report(text, ["C1", "C2"], list(files), self.repo, work_order="N1"), text

    def test_check_report_merge_commit_on_main(self):
        self.merge_record()
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "prorab/x")
        r, text = self.merge_check()
        self.assertTrue(r["ok"], r["problems"])
        # without the work order the base falls back to main itself: nothing to deliver
        r = crew.check_report(text, ["C1", "C2"], ["x.txt"], self.repo)
        self.assertFalse(r["ok"])

    def test_check_report_fast_forward_to_the_branch_tip_passes(self):
        self.merge_record()
        git(self.repo, "merge", "-q", "--ff-only", "prorab/x")
        r, _ = self.merge_check()
        self.assertTrue(r["ok"], r["problems"])

    def test_check_report_main_moved_by_more_than_the_merge_is_refused(self):
        self.merge_record()
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "prorab/x")
        self.commit(self.repo, "extra.txt", "e\n", "an extra commit on main")
        r, _ = self.merge_check(files=("x.txt", "extra.txt"),
                                changed="- `x.txt` - merged\n- `extra.txt` - extra\n")
        self.assertFalse(r["ok"])
        self.assertIn("main moved by more than the merge of prorab/x", r["problems"])

    def test_check_report_a_commit_before_the_merge_is_refused(self):
        self.merge_record()
        self.commit(self.repo, "pre.txt", "p\n", "a commit before the merge")
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "prorab/x")
        r, _ = self.merge_check(files=("x.txt", "pre.txt"),
                                changed="- `x.txt` - merged\n- `pre.txt` - extra\n")
        self.assertIn("main moved by more than the merge of prorab/x", r["problems"])

    def test_check_report_the_merge_of_another_branch_is_refused(self):
        self.merge_record()
        git(self.repo, "checkout", "-q", "-b", "prorab/y")
        self.commit(self.repo, "y.txt", "y\n", "work on y")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "prorab/y")
        r, _ = self.merge_check(files=("y.txt",), changed="- `y.txt` - merged\n")
        self.assertIn("main moved by more than the merge of prorab/x", r["problems"])

    def test_check_report_an_octopus_merge_is_refused(self):
        self.merge_record()
        git(self.repo, "checkout", "-q", "-b", "prorab/y")
        self.commit(self.repo, "y.txt", "y\n", "work on y")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "prorab/x", "prorab/y")
        self.assertEqual(len(git(self.repo, "rev-list", "--parents", "-n", "1",
                                 "HEAD").split()), 4)
        r, _ = self.merge_check(files=("x.txt", "y.txt"),
                                changed="- `x.txt` - merged\n- `y.txt` - merged\n")
        self.assertIn("main moved by more than the merge of prorab/x", r["problems"])

    def test_check_report_a_rewritten_base_is_refused(self):
        self.merge_record()
        git(self.repo, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten base")
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "--allow-unrelated-histories",
            "prorab/x")
        r, _ = self.merge_check()
        self.assertIn("main moved by more than the merge of prorab/x", r["problems"])

    def test_check_report_a_fast_forward_to_a_tip_not_descending_from_the_base(self):
        self.commit(self.repo, "later.txt", "l\n", "a later commit on main")
        later = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "reset", "-q", "--hard", self.head)
        self.j.issue_set("N1", "issued", worktree=self.repo, base_commit=later,
                         main_checkout=True, merge_branch="prorab/x")
        git(self.repo, "merge", "-q", "--ff-only", "prorab/x")
        r, _ = self.merge_check()
        self.assertIn("main moved by more than the merge of prorab/x", r["problems"])

    def test_check_report_without_a_recorded_merge_branch_is_refused(self):
        self.j.issue_set("N1", "issued", worktree=self.repo, base_commit=self.head,
                         main_checkout=True)
        git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "prorab/x")
        r, _ = self.merge_check()
        self.assertFalse(r["ok"])

    def test_decision_latest_of_merge_and_drop_wins(self):
        self.j.add_act("N1", "answer", crew.DROP_MARKER + " (changed my mind)")
        self.refused("no_merge_decision")
        self.j.add_act("N1", "answer", self.DECISION)
        self.assertEqual(self.go()["status"], "started")

    def test_start_main_checkout_refuses_while_another_start_is_issuing(self):
        self.j.issue_set("N0", "issuing", main_checkout=True, site=self.repo, name="b0")
        r = self.refused("main_busy")
        self.assertEqual(r.get("with"), ["N0"])
        self.j.issue_set("N0", "issued", main_checkout=True, site=self.repo, name="b0")
        self.assertEqual(self.go()["status"], "started")  # registered crews are what count then

    def test_restart_settings_follow_the_crews_recorded_work_order(self):
        self.go()
        os.remove(self.merge_settings())
        with mock.patch.object(crew, "_cd_failure", return_value=None), \
                mock.patch.object(crew, "deliver", return_value="accepted"):
            r = crew._restart_after_exit("a1", "N9", "Work order text", self.repo, "w:p9",
                                         {"usd": 0}, 1, 0.01)
        self.assertEqual(r["status"], "restarted")
        line = [c for c in self.calls() if c.startswith("agent start a1")][-1]
        self.assertTrue(line.endswith(" --settings %s" % self.merge_settings()), line)
        self.assertFalse(os.path.exists(self.shared_settings()))

    def test_complete_handover_without_hooks_exits_nothing(self):
        self.go()
        del self.j.kv["hooks.enabled"]
        self.write_transcript(self.repo, self.j.kv["crew.a1.session"],
                              "Act: done\nHANDOVER READY")
        self.route("agent read a1 --source visible", "idle\n")
        before = len(self.calls())
        r = crew.complete_handover("a1", "N1", "work order", timeout_s=1, poll_s=0.01)
        self.assertEqual((r["status"], r["step"]), ("restart_failed", "settings"))
        self.assertEqual([c for c in self.calls()[before:] if "/exit" in c], [])
        self.assertEqual(self.j.acts("N1", "handover"), [])

    def test_restart_without_hooks_exits_nothing(self):
        self.go()
        del self.j.kv["hooks.enabled"]
        before = len(self.calls())
        r = crew.restart("a1", "N1", "work order", "gone", caller_pane="w:p1",
                         timeout_s=1, poll_s=0.01)
        self.assertEqual((r["status"], r["step"]), ("restart_failed", "settings"))
        self.assertEqual([c for c in self.calls()[before:] if "/exit" in c or "agent start" in c],
                         [])

    def test_cli_start_takes_main_checkout(self):
        with mock.patch.object(crew, "start", return_value={"status": "started"}) as s, \
                mock.patch("builtins.print"):
            self.assertEqual(crew.main(["start", "/home/user/projects/x", "N1", "a1", "w:p1",
                                        "--main-checkout"]), 0)
            self.assertEqual(crew.main(["start", "/home/user/projects/x", "N1", "a1", "w:p1"]), 0)
        self.assertIs(s.call_args_list[0].kwargs["main_checkout"], True)
        self.assertIs(s.call_args_list[1].kwargs["main_checkout"], False)

    def test_complete_handover_keeps_the_merge_settings(self):
        self.go()
        os.remove(self.merge_settings())
        self.j.kv["crew.a1.delivered"] = "x"
        with mock.patch.object(crew, "_cd_failure", return_value=None), \
                mock.patch.object(crew, "deliver", return_value="accepted"):
            r = crew._restart_after_exit("a1", "N1", "Work order text", self.repo, "w:p9",
                                         {"usd": 0}, 1, 0.01)
        self.assertEqual(r["status"], "restarted")
        line = [c for c in self.calls() if c.startswith("agent start a1")][-1]
        self.assertNotIn(" -w ", line)
        self.assertTrue(line.endswith(" --settings %s" % self.merge_settings()), line)
        with open(self.merge_settings(), encoding="utf-8") as fh:
            self.assertIn("--merge-branch prorab/x", fh.read())

    def test_restart_without_hooks_starts_no_merge_crew(self):
        self.go()
        del self.j.kv["hooks.enabled"]
        before = [c for c in self.calls() if c.startswith("agent start")]
        with mock.patch.object(crew, "_cd_failure", return_value=None), \
                mock.patch.object(crew, "deliver", return_value="accepted"):
            r = crew._restart_after_exit("a1", "N1", "Work order text", self.repo, "w:p9",
                                         {"usd": 0}, 1, 0.01)
        self.assertEqual((r["status"], r["step"]), ("restart_failed", "settings"))
        self.assertEqual([c for c in self.calls() if c.startswith("agent start")], before)


class TestCli(unittest.TestCase):
    def test_cli_restart_takes_both_spellings(self):
        for flag in ("--caller_pane", "--caller-pane"):
            with mock.patch.object(crew, "restart", return_value={"status": "restarted"}) as r, \
                    mock.patch("builtins.print"):
                code = crew.main(["restart", "a1", "N1", "T", "gone", flag, "w:p1"])
            self.assertEqual(code, 0, flag)
            self.assertEqual(r.call_args.kwargs["caller_pane"], "w:p1", flag)


if __name__ == "__main__":
    unittest.main()
