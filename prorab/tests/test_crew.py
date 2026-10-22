import json
import os
import re
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

    def bd(self, args, json_out=True):
        return [{"id": args[1], "status": "in_progress"}]

    def machine(self):
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
        if key.startswith("restarts.") and key[len("restarts."):] in self.restarts:
            return str(self.restarts[key[len("restarts."):]])
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

    def files_dir(self):
        return os.path.join(self.home, ".cache", "prorab", "work-orders")

    def start_line(self):
        return [c for c in self.calls() if c.startswith("agent start a1")][0]

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

    def test_start_refuses_already_issued(self):
        self.j.issue_set("N1", "issued", name="a1", pane="w:p9")
        self.route("agent list", ok({"agents": [agent_entry()]}))
        r = crew.start(SITE_DIR, "N1", "a1", "w:p1")
        self.assertEqual(r, {"status": "already_issued", "name": "a1", "pane_id": "w:p9"})
        self.assertFalse([c for c in self.calls() if c.startswith("pane split")])
        self.j.issue_set("N1", "issuing", name="a1", pane="w:p9")
        self.assertEqual(crew.start(SITE_DIR, "N1", "a1", "w:p1")["status"], "already_issued")

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
        self.route("agent send-keys a1", ok({}))
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
        self.route("agent read a1 --source visible", fixture("consent", "trust.txt"))
        self.assertEqual(crew.deliver("a1", "t", "N1"), "consent:trust")
        calls = self.calls()
        self.assertEqual([c for c in calls if "send-keys" in c], [])
        self.assertEqual(len([c for c in calls if c.startswith("agent prompt")]), 1)

    def test_unconfirmed_then_consent_returns_kind(self):
        self.route("agent prompt a1", ok({"agent": {"agent_status": "idle"}}))
        self.route("agent read a1 --source visible", fixture("consent", "mcp.txt"))
        self.assertEqual(crew.deliver("a1", "t", "N1"), "consent:mcp")
        self.assertEqual([c for c in self.calls() if "send-keys" in c], [])

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
        calls = self.calls()
        self.assertIn("wait for the foreman's message", calls[0])
        self.assertIn("HANDOVER READY", calls[1])
        self.assertIn("next steps", calls[1])


class TestHandover(CrewBase):
    def setUp(self):
        super().setUp()
        self.wt = "/home/user/projects/demo-app/.claude/worktrees/prorab-N1"
        self.j.kv["crew.a1.worktree"] = self.wt
        self.write_transcript("/work", "sess-1", "Act: done A\nHANDOVER READY")
        self.route("agent read a1 --source visible", "idle\n")

    def test_complete_handover_refuses_with_bg_shells(self):
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent read a1 --source visible", "bar · 1 shell\n")
        r = crew.complete_handover("a1", "N1", "work order", timeout_s=1, poll_s=0.01)
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

    def test_restart_never_falls_back_to_site_dir(self):
        self.route_seq("agent list", [ok({"agents": [agent_entry()]}), ok({"agents": []})])
        pane_info = {"pane": {"pane_id": "w:p9", "foreground_cwd": "/home/user/projects/demo-app"}}
        self.route("pane get w:p9", ok(pane_info))
        self.route("pane run w:p9", ok({}))
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
        self.route("pane run w:p9", ok({}))

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
        self.route("pane read w:p9 --source visible", "Do you trust the files here?\nYes, I trust this folder\n")
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
        wt = "/home/user/it's a/prorab-N1"
        self.j.kv["crew.a1.worktree"] = wt
        self.restart_routes(wt)
        self.route("agent start a1", ok({}))
        self.route("agent prompt a1", ok({"agent": {"agent_status": "working"}}))
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
        self.j.add_act("N1", "answer", crew.DROP_MARKER + " (customer's words)")
        self.assertEqual(crew.cleanup(repo, "N1")["status"], "removed")
        self.assertFalse(os.path.exists(wt))

    def test_cleanup_drop_needs_the_exact_marker(self):
        repo = self.make_repo()
        wt = self.add_worktree(repo, "N1", "prorab/N1")
        self.commit(wt, "g.txt", "1\n", "one")
        for text in ("drop the unused column", "dropdown menu", "decision: drop"):
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

    def test_answer_text_selects_type_something(self):
        self.route("agent read a1 --source visible", DIALOG)
        self.route("pane send-text w:p9", ok({}))
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


class TestWaitLimit(CrewBase):
    def setUp(self):
        super().setUp()
        self.j.kv["crew.a1.work_order"] = "N1"
        self.route("agent send-keys a1", ok({}))

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


class TestRestart(CrewBase):
    def setUp(self):
        super().setUp()
        self.wt = "/home/user/projects/demo-app/.claude/worktrees/prorab-N1"
        self.j.kv.update({"crew.a1.worktree": self.wt, "crew.a1.pane": "w:p9",
                          "crew.a1.session": "old-session", "crew.a1.work_order": "N1"})
        self.route("agent list", ok({"agents": []}))
        self.route("pane get w:p9", ok({"pane": {"pane_id": "w:p9", "foreground_cwd": self.wt}}))
        self.route("pane run w:p9", ok({}))
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
        self.assertEqual(self.go()["status"], "resumed")
        self.assertEqual(self.starts(), ["agent start a1 --kind claude --pane w:p9 -- "
                                         "--resume old-session --add-dir %s" % self.files_dir()])
        self.assertEqual(self.j.restarts, {"N1": 1})
        self.assertIn("agent prompt a1 Continue from where you stopped. --wait --until working "
                      "--until done --until blocked --timeout 60000", self.calls())
        self.assertEqual(self.j.kv["crew.a1.session"], "old-session")

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

    def test_limit_death_restart_not_counted(self):
        self.assertEqual(self.go("limit_died")["status"], "resumed")
        self.assertEqual(self.j.restarts, {})

    def test_restart_over_limit_waits_customer(self):
        self.j.restarts["N1"] = _defaults.load()["monitor"]["max_restarts"]  # via kv_get
        self.assertEqual(self.go("hung"), {"status": "waits_customer"})
        self.assertEqual(len(self.j.gates), 1)
        self.assertEqual(self.calls_or_empty(), [])

    def test_failed_herdr_call_leaves_counter_and_session(self):
        self.route("pane run w:p9", err("cli_error"), exit_code=1)
        r = self.go("hung")
        self.assertEqual(r["status"], "restart_failed")
        self.assertEqual(self.j.restarts, {})
        self.assertEqual(self.j.kv["crew.a1.session"], "old-session")
        self.route("pane run w:p9", ok({}))
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
        self.route("pane run w:p12", ok({}))
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


def make_report(**kw):
    parts = {
        "Summary": "## Summary\nAdded the module.\n",
        "Behaviour": ("## Behaviour\n| Criterion | Status | Reason |\n|---|---|---|\n"
                      "| C1 | done | |\n| C2 | not applicable | no UI in this stage |\n"),
        "Changed files": "## Changed files\n- `src/a.py` - new module\n",
        "Verification": "## Verification\n- `python3 -m unittest` SKIP: 0\n",
        "Residual risks": "## Residual risks\nNone.\n",
    }
    parts.update(kw.get("sections", {}))
    text = "# Stage report\n\n" + "\n".join(v for v in parts.values() if v)
    heavy = kw.get("heavy", "Heavy runs: 3\n")
    return text + ("\n" + heavy if heavy else "") + kw.get("tail", "")


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

    def test_check_report_ok(self):
        r = self.check()
        self.assertEqual(r, {"ok": True, "problems": [], "warnings": [],
                             "outcome": "delivered", "sha": self.sha})

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
        self.j.bd = lambda args, json_out=True: [{"description": "C1. a", "labels":
                                                  ["clearance:granted"]}]
        self.route("agent list", ok({"agents": [agent_entry(cwd=wt)]}))
        with mock.patch.object(crew.slots, "runs", return_value=9) as runs, \
                mock.patch.object(crew, "summary", return_value="r"), \
                mock.patch.object(crew, "check_report", return_value={"ok": True}) as chk:
            crew._check_report_cli("a1", "N1", stage="N1.1")
        runs.assert_called_once_with("heavy", wt, since=100.0)
        kw = chk.call_args.kwargs
        self.assertEqual((kw["heavy_limit"], kw["cleared"], kw["heavy_counted"]), (4, True, 9))

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
        with mock.patch.object(crew.slots, "runs", return_value=3):
            r = self._cli_check("N1.1", 100)
        self.assertTrue(r["ok"], r)


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
        self.route("agent list", ok({"agents": [agent_entry()]}))
        self.route("agent prompt a1", err("agent_not_found"), exit_code=1)
        crew.stop("a1", "N1")
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


if __name__ == "__main__":
    unittest.main()
