import io
import json
import os
import stat
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

import _defaults  # noqa: E402
import hooks  # noqa: E402

HOOKS_PY = os.path.join(SCRIPTS_DIR, "hooks.py")
BANNED_KEYS = ("permissions", "defaultMode", "enableAllProjectMcpServers",
               "skipDangerousModePermissionPrompt")


class HooksBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.home)
        patch = mock.patch.dict(os.environ, {"HOME": self.home, "PRORAB_CACHE": self.cache})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("PRORAB_BEADS_DIR", None)
        self.d = _defaults.load()

    def git_dir(self, name="repo", branch="main"):
        repo = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/%s\n" % branch)
        return repo

    def decide(self, command, cwd="/nowhere", tool="Bash"):
        raw = json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": cwd})
        with mock.patch("subprocess.run", side_effect=AssertionError("a process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("a process")):
            return hooks.guard_decision(raw, self.d)


class TestSettings(HooksBase):
    def test_paths_lie_under_the_temp_home(self):
        for key in ("settings_path", "events_dir"):
            self.assertTrue(self.d["hooks"][key].startswith(self.home), key)
        self.assertTrue(self.d["emergency"]["stop_path"].startswith(self.home))

    def test_settings_file_keys_only(self):
        path = hooks.write_settings(self.d)
        self.assertEqual(path, self.d["hooks"]["settings_path"])
        self.assertTrue(path.startswith(self.home))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(sorted(data), ["feedbackSurveyRate", "hooks", "promptSuggestionEnabled"])
        self.assertEqual(data["feedbackSurveyRate"], 0)
        self.assertIs(data["promptSuggestionEnabled"], False)
        for key in BANNED_KEYS:
            self.assertNotIn(key, data)
            self.assertNotIn(key, json.dumps(data))
        self.assertEqual(sorted(data["hooks"]),
                         ["Notification", "PreCompact", "PreToolUse", "SessionStart", "Stop"])
        self.assertEqual([f for f in os.listdir(os.path.dirname(path))],
                         [os.path.basename(path)])

    def test_settings_rewrite_is_atomic_and_idempotent(self):
        first = hooks.write_settings(self.d)
        with open(first, encoding="utf-8") as fh:
            before = fh.read()
        self.assertEqual(hooks.write_settings(self.d), first)
        with open(first, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_settings_guard_and_taps_use_the_absolute_script(self):
        with open(hooks.write_settings(self.d), encoding="utf-8") as fh:
            data = json.load(fh)
        (_, bash) = data["hooks"]["PreToolUse"]
        self.assertEqual(bash["matcher"], "Bash")
        self.assertEqual(bash["hooks"][0]["command"], "python3 -I %s guard" % HOOKS_PY)
        for event in ("Stop", "Notification", "PreCompact", "SessionStart"):
            (entry,) = data["hooks"][event]
            self.assertEqual(entry["hooks"][0]["command"], "python3 -I %s tap" % HOOKS_PY)

    def stop_command(self):
        with open(hooks.write_settings(self.d), encoding="utf-8") as fh:
            data = json.load(fh)
        every = [e for e in data["hooks"]["PreToolUse"] if e.get("matcher") == "*"]
        self.assertEqual(len(every), 1)
        (hook,) = every[0]["hooks"]
        return hook["command"]

    def test_settings_stop_check_covers_every_tool(self):
        command = self.stop_command()
        self.assertNotIn("python", command)
        self.assertTrue(command.startswith("[ ! -e "))
        self.assertIn(self.d["emergency"]["stop_path"], command)
        self.assertNotIn(">>", command)  # no file write

    def test_stop_check_denies_while_the_file_exists(self):
        command = self.stop_command()
        stop = self.d["emergency"]["stop_path"]
        run = lambda: subprocess.run(["sh", "-c", command], input='{"tool_name":"Read"}',
                                     capture_output=True, text=True)
        p = run()
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        os.makedirs(os.path.dirname(stop), exist_ok=True)
        with open(stop, "w"):
            pass
        p = run()
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr.strip(), hooks.STOP_REASON)
        self.assertIn("Do not retry; end your turn and wait for the foreman.", p.stderr)
        os.remove(stop)
        self.assertEqual(run().returncode, 0)

    def test_stop_check_quotes_a_path_with_spaces(self):
        self.d["emergency"]["stop_path"] = os.path.join(self.tmp.name, "a b", "STOP; echo x")
        command = self.stop_command()
        os.makedirs(os.path.dirname(self.d["emergency"]["stop_path"]))
        with open(self.d["emergency"]["stop_path"], "w"):
            pass
        p = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        self.assertEqual((p.returncode, "x\n" in p.stdout), (2, False))


class TestGuard(HooksBase):
    def test_guard_blocks_bans(self):
        for entry in self.d["hooks"]["deny"]:
            code, message = self.decide("echo start && %s origin work/x" % entry)
            self.assertEqual(code, 2, entry)
            self.assertEqual(message, "prorab guard: %s is forbidden for crews; ask the foreman"
                             % entry.strip())
        self.assertEqual(self.decide("git   push\t--dry-run")[0], 2)  # whitespace folded
        self.assertEqual(self.decide("sudo  ls")[0], 2)

    def test_guard_blocks_the_office_journal(self):
        office = os.path.expanduser("~/projects/prorab/.beads")
        for command in ("BEADS_DIR=%s bd list" % office, "bd --db ~/projects/prorab/.beads x",
                        "ls $HOME/projects/prorab/.beads"):
            self.assertEqual(self.decide(command)[0], 2, command)
        with mock.patch.dict(os.environ, {"PRORAB_BEADS_DIR": "/srv/office/.beads"}):
            self.assertEqual(self.decide("BEADS_DIR=/srv/office/.beads bd list")[0], 2)

    def test_guard_denies_unreadable_input(self):
        for raw in ("not json", "", "[]", '{"tool_name": "Bash"}',
                    '{"tool_name": "Bash", "tool_input": {}}',
                    '{"tool_name": "Bash", "tool_input": {"command": ""}}',
                    '{"tool_name": "Bash", "tool_input": {"command": 3}}', "null"):
            self.assertEqual(hooks.guard_decision(raw, self.d),
                             (2, "prorab guard: unreadable hook input"), raw)

    def test_guard_ignores_other_tools(self):
        self.assertEqual(self.decide("git push", tool="Read"), (0, ""))
        self.assertEqual(hooks.guard_decision('{"tool_name": "Read"}', self.d), (0, ""))

    def test_guard_allows_ordinary_git(self):
        repo = self.git_dir()
        for command in ("git status", "git commit -m x", "git log --oneline -3",
                        "git checkout -b prorab/wo-1", "python3 -m unittest"):
            self.assertEqual(self.decide(command, cwd=repo), (0, ""), command)

    def test_guard_merge_on_main_blocked_on_branch_allowed(self):
        repo = self.git_dir(branch="main")
        for command in ("git merge feature", "git rebase origin/x", "ls && git merge x"):
            code, message = self.decide(command, cwd=repo)
            self.assertEqual(code, 2, command)
            self.assertEqual(message, "prorab guard: merge or rebase on main is forbidden "
                             "for crews; ask the foreman")
        master = self.git_dir("m", branch="master")
        self.assertEqual(self.decide("git merge x", cwd=master)[0], 2)
        work = self.git_dir("w", branch="prorab/wo-1")
        self.assertEqual(self.decide("git merge main", cwd=work), (0, ""))
        # a worktree: .git is a file naming its git directory
        gitdir = os.path.join(self.tmp.name, "main-repo", ".git", "worktrees", "wt")
        os.makedirs(gitdir)
        wt = os.path.join(self.tmp.name, "wt")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % gitdir)
        with open(os.path.join(gitdir, "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/prorab/wo-2\n")
        self.assertEqual(self.decide("git merge main", cwd=wt), (0, ""))
        with open(os.path.join(gitdir, "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/main\n")
        self.assertEqual(self.decide("git merge x", cwd=wt)[0], 2)
        # a relative gitdir is relative to the folder holding .git
        rel = os.path.join(self.tmp.name, "rel")
        os.makedirs(os.path.join(rel, "meta"))
        with open(os.path.join(rel, ".git"), "w") as fh:
            fh.write("gitdir: meta\n")
        with open(os.path.join(rel, "meta", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/main\n")
        self.assertEqual(self.decide("git merge x", cwd=rel)[0], 2)

    def test_guard_merge_from_a_subdirectory_on_main_blocked(self):
        repo = self.git_dir()
        sub = os.path.join(repo, "src", "deep")
        os.makedirs(sub)
        self.assertEqual(self.decide("git merge x", cwd=sub)[0], 2)

    def test_guard_merge_with_dash_C_on_main_blocked(self):
        main = self.git_dir("main-site", branch="main")
        work = self.git_dir("work-site", branch="prorab/wo-1")
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        self.assertEqual(self.decide("git -C %s merge x" % main, cwd=work)[0], 2)
        self.assertEqual(self.decide("git -C '%s' rebase x" % main, cwd=work)[0], 2)
        self.assertEqual(self.decide("git -C %s merge x" % work, cwd=main), (0, ""))
        self.assertEqual(self.decide("git -C ../main-site merge x", cwd=work)[0], 2)
        self.assertEqual(self.decide("git -C %s merge x" % main, cwd=elsewhere)[0], 2)

    def test_guard_merge_branch_unreadable_denied(self):
        unknown = "prorab guard: cannot tell the checked-out branch; ask the foreman"
        nogit = os.path.join(self.tmp.name, "plain")
        os.makedirs(nogit)
        self.assertEqual(self.decide("git merge x", cwd=nogit), (2, unknown))
        dangling = os.path.join(self.tmp.name, "dangling")
        os.makedirs(dangling)
        with open(os.path.join(dangling, ".git"), "w") as fh:
            fh.write("gitdir: /nonexistent/place\n")
        self.assertEqual(self.decide("git merge x", cwd=dangling), (2, unknown))
        nameless = os.path.join(self.tmp.name, "nameless")
        os.makedirs(nameless)
        with open(os.path.join(nameless, ".git"), "w") as fh:
            fh.write("not a gitdir line\n")
        self.assertEqual(self.decide("git merge x", cwd=nameless), (2, unknown))
        empty = self.git_dir("empty")
        with open(os.path.join(empty, ".git", "HEAD"), "w"):
            pass
        self.assertEqual(self.decide("git merge x", cwd=empty), (2, unknown))
        odd = self.git_dir("odd")
        with open(os.path.join(odd, ".git", "HEAD"), "w") as fh:
            fh.write("something else\n")
        self.assertEqual(self.decide("git rebase x", cwd=odd), (2, unknown))
        # a command that is no merge never needs the branch
        self.assertEqual(self.decide("git status", cwd=nogit), (0, ""))

    def test_guard_merge_detached_head_allowed(self):
        repo = self.git_dir()
        with open(os.path.join(repo, ".git", "HEAD"), "w") as fh:
            fh.write("0123456789abcdef0123456789abcdef01234567\n")
        self.assertEqual(self.decide("git merge x", cwd=repo), (0, ""))

    def test_guard_cli_exit_codes_and_message(self):
        env = dict(os.environ)
        run = lambda raw: subprocess.run([sys.executable, "-I", HOOKS_PY, "guard"], input=raw,
                                         capture_output=True, text=True, env=env)
        p = run(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}}))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr.strip(),
                         "prorab guard: git push is forbidden for crews; ask the foreman")
        self.assertEqual(run(json.dumps({"tool_name": "Bash",
                                         "tool_input": {"command": "ls"}})).returncode, 0)
        self.assertEqual(run("garbage").returncode, 2)


class TestTap(HooksBase):
    def feed(self, data, raw=None):
        stdin = io.StringIO(raw if raw is not None else json.dumps(data))
        return hooks.tap(stdin)

    def lines(self, sid):
        path = os.path.join(self.d["hooks"]["events_dir"], sid + ".jsonl")
        with open(path, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh.read().splitlines()]

    def test_tap_writes_event_lines(self):
        sid = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(self.feed({"hook_event_name": "Stop", "session_id": sid,
                                    "background_tasks": [], "last_assistant_message": "x" * 300}), 0)
        self.assertEqual(self.feed({"hook_event_name": "Notification", "session_id": sid,
                                    "notification_type": "permission_prompt"}), 0)
        self.assertEqual(self.feed({"hook_event_name": "PreCompact", "session_id": sid,
                                    "trigger": "auto"}), 0)
        self.assertEqual(self.feed({"hook_event_name": "SessionStart", "session_id": sid,
                                    "source": "resume"}), 0)
        stop, note, compact, start = self.lines(sid)
        self.assertEqual((stop["event"], stop["session_id"], stop["background_tasks"]),
                         ("Stop", sid, []))
        self.assertEqual(len(stop["last_assistant_message"]), 200)
        self.assertEqual(note["notification_type"], "permission_prompt")
        self.assertEqual(compact["trigger"], "auto")
        self.assertEqual(start["source"], "resume")
        for rec in (stop, note, compact, start):
            self.assertIsInstance(rec["ts"], float)
        events_dir = self.d["hooks"]["events_dir"]
        self.assertEqual(stat.S_IMODE(os.stat(events_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(events_dir, sid + ".jsonl")).st_mode),
                         0o600)

    def test_tap_unsafe_session_id_writes_nothing(self):
        for sid in ("../x", "a/b", "", ".hidden", None, 5, "x" * 200, "a\x00b"):
            self.assertEqual(self.feed({"hook_event_name": "Stop", "session_id": sid}), 0)
        self.assertFalse(os.path.exists(self.d["hooks"]["events_dir"]))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x.jsonl")))

    def test_tap_garbage_and_other_events_exit_zero_and_write_nothing(self):
        self.assertEqual(self.feed(None, raw="not json"), 0)
        self.assertEqual(self.feed(None, raw=""), 0)
        self.assertEqual(self.feed(["x"]), 0)
        self.assertEqual(self.feed({"hook_event_name": "PreToolUse", "session_id": "s1"}), 0)
        self.assertFalse(os.path.exists(self.d["hooks"]["events_dir"]))

    def test_tap_cli_exits_zero_even_when_the_directory_is_unwritable(self):
        env = dict(os.environ, HOME="/dev/null")
        p = subprocess.run([sys.executable, "-I", HOOKS_PY, "tap"],
                           input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}),
                           capture_output=True, text=True, env=env)
        self.assertEqual((p.returncode, p.stdout), (0, ""))


class TestSettingsCli(HooksBase):
    def test_settings_command_prints_the_path(self):
        p = subprocess.run([sys.executable, "-I", HOOKS_PY, "settings"], capture_output=True,
                           text=True, env=dict(os.environ))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), self.d["hooks"]["settings_path"])
        self.assertTrue(os.path.isfile(self.d["hooks"]["settings_path"]))


if __name__ == "__main__":
    unittest.main()
