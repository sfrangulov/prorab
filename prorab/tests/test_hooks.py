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

    def decide(self, command, cwd="/nowhere", tool="Bash", merge_branch=None):
        raw = json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": cwd})
        with mock.patch("subprocess.run", side_effect=AssertionError("a process")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("a process")):
            return hooks.guard_decision(raw, self.d, merge_branch=merge_branch)


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

    def test_failed_settings_write_leaves_no_temp_file(self):
        folder = os.path.dirname(self.d["hooks"]["settings_path"])
        with mock.patch.object(hooks.json, "dump", side_effect=ValueError("no")):
            with self.assertRaises(ValueError):
                hooks.write_settings(self.d)
        self.assertEqual(os.listdir(folder), [])

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

    def test_guard_denies_logging_in(self):
        for command in ("claude auth login", "claude /login", "claude setup-token",
                        "claude logout", "claude login"):
            code, message = self.decide(command)
            self.assertEqual(code, 2, command)
            self.assertTrue(message.startswith("prorab guard: claude "), command)
            self.assertIn("is forbidden for crews", message)

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

    def test_guard_internal_error_denies(self):
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        err = "prorab guard: internal error; ask the foreman"
        with mock.patch.object(_defaults, "load", side_effect=OSError("garbled")):
            self.assertEqual(hooks.guard_decision(raw), (2, err))
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                self.assertEqual(hooks.guard(io.StringIO(raw)), 2)
            self.assertEqual(stderr.getvalue().strip(), err)

    def test_guard_removed_cwd_denies(self):
        gone = os.path.join(self.tmp.name, "gone")
        os.makedirs(gone)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(gone)
        os.rmdir(gone)
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git merge x"}})
        self.assertEqual(hooks.guard_decision(raw, self.d)[0], 2)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(hooks.guard(io.StringIO(raw)), 2)
        self.assertTrue(stderr.getvalue().startswith("prorab guard:"))

    def test_guard_unreadable_stdin_denies(self):
        class Broken:
            def read(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(hooks.guard(Broken()), 2)
        self.assertIn("prorab guard:", stderr.getvalue())

    def test_guard_blocks_the_braced_home_form(self):
        self.assertEqual(self.decide("ls ${HOME}/projects/prorab/.beads")[0], 2)

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


class TestMergeCrew(HooksBase):
    """The guard of a main-checkout merge crew (`guard --merge-branch BRANCH`, fix wave 1)."""

    BRANCH = "prorab/x"

    def merge_decide(self, command, cwd):
        return self.decide(command, cwd=cwd, merge_branch=self.BRANCH)

    def test_guard_merge_branch_allows_only_that_merge(self):
        repo = self.git_dir(branch="main")
        for command in ("git merge --no-ff --no-edit prorab/x", "git merge prorab/x",
                        "git merge --no-ff prorab/x", "git merge --ff-only prorab/x",
                        "git merge --ff-only --no-edit prorab/x", "git merge --no-edit prorab/x",
                        "git merge --abort", "git status", "git log --oneline -3",
                        "git diff prorab/x", "git show HEAD", "git rev-parse HEAD",
                        "git merge-base main prorab/x", "git ls-files", "git branch",
                        "git branch --list", "git branch --show-current",
                        "git status && git log --oneline", "git diff | head -5",
                        "git -C %s status" % repo, "git --no-pager log -1",
                        "ls src && python3 -m unittest"):
            self.assertEqual(self.merge_decide(command, repo), (0, ""), command)
        for command in ("git merge other", "git merge -X theirs prorab/x",
                        "git merge prorab/x && git push", "git merge prorab/x; git push",
                        "git merge --no-ff --no-edit prorab/x && ls",
                        "git status; git merge prorab/x", "git merge --abort && git status",
                        "git -C /elsewhere merge prorab/x", "git pull", "git cherry-pick X",
                        "git commit --allow-empty -m x", "git reset HEAD~1",
                        "git rebase main", "git push", "git am p.patch", "git revert HEAD",
                        "git update-ref refs/heads/main HEAD", "git branch -D prorab/x",
                        "git branch -f main HEAD", "git branch -m x y", "git branch newone",
                        "git checkout prorab/x", "git switch prorab/x", "git stash",
                        "git merge prorab/x prorab/y", "git merge --squash prorab/x",
                        "git merge --no-ff --no-ff prorab/x", "git merge prorab/xx",
                        "git merge --no-ff --no-edit", "git -c core.pager=x log",
                        "git log --output=/tmp/x", "git diff --output /tmp/x",
                        "git config user.name x", "git clean -n", "git tag v1",
                        "git status > /tmp/x", "echo $(git commit -m x)",
                        "echo `git commit -m x`", "xargs git commit", "env git commit",
                        "GIT_DIR=x git commit", "bash -c 'git commit -m x'",
                        "/usr/bin/git commit -m x", "git"):
            code, message = self.merge_decide(command, repo)
            self.assertEqual(code, 2, command)
            self.assertTrue(message.startswith("prorab guard: "), command)

    def test_guard_merge_branch_master_is_the_same(self):
        master = self.git_dir("m", branch="master")
        self.assertEqual(self.merge_decide("git merge --no-ff --no-edit prorab/x", master),
                         (0, ""))
        self.assertEqual(self.merge_decide("git merge other", master)[0], 2)
        self.assertEqual(self.merge_decide("git pull", master)[0], 2)

    def test_guard_merge_branch_from_a_subdirectory(self):
        repo = self.git_dir()
        sub = os.path.join(repo, "src")
        os.makedirs(sub)
        self.assertEqual(self.merge_decide("git merge --no-ff --no-edit prorab/x", sub), (0, ""))
        self.assertEqual(self.merge_decide("git -C %s status" % repo, sub), (0, ""))
        self.assertEqual(self.merge_decide("git -C .. merge prorab/x", sub), (0, ""))
        self.assertEqual(self.merge_decide("git -C ../.. merge prorab/x", sub)[0], 2)
        self.assertEqual(self.merge_decide("git commit -m x", sub)[0], 2)

    def test_guard_merge_branch_dash_C_elsewhere_refused(self):
        main = self.git_dir("main-site")
        other = self.git_dir("other-site", branch="prorab/wo-1")
        for target in (other, "/elsewhere", os.path.join(self.tmp.name, "nowhere")):
            self.assertEqual(self.merge_decide("git -C %s merge prorab/x" % target, main)[0], 2)
            self.assertEqual(self.merge_decide("git -C %s reset --hard" % target, main)[0], 2)
        self.assertEqual(self.merge_decide("git -C %s -C %s status" % (main, other), main)[0], 2)

    def test_guard_merge_branch_keeps_the_rules_for_any_crew(self):
        repo = self.git_dir()
        for entry in self.d["hooks"]["deny"]:
            self.assertEqual(self.merge_decide("echo x && %s origin x" % entry, repo)[0], 2, entry)
        office = os.path.expanduser("~/projects/prorab/.beads")
        self.assertEqual(self.merge_decide("ls %s" % office, repo)[0], 2)
        self.assertEqual(self.merge_decide("", repo), (2, "prorab guard: unreadable hook input"))

    def test_guard_merge_branch_holds_in_every_checkout(self):
        site = self.git_dir("site", branch="main")
        work = self.git_dir("w", branch="prorab/wo-1")  # a crew that cd'ed into a worktree
        for command in ("git merge main", "git merge prorab/x", "git commit -m x",
                        "git -C %s reset --hard" % site, "git -C %s commit -m x" % site,
                        "git -C .. reset --hard", "git branch -D prorab/x", "git checkout x",
                        "git push"):
            self.assertEqual(self.merge_decide(command, work)[0], 2, command)
        # the merge itself needs its target on main/master, read-only git and --abort anywhere
        self.assertEqual(self.merge_decide("git -C %s merge --no-ff --no-edit prorab/x" % site,
                                           work), (0, ""))
        for command in ("git status", "git merge --abort", "git log -1", "git -C %s status" % site):
            self.assertEqual(self.merge_decide(command, work), (0, ""), command)
        nogit = os.path.join(self.tmp.name, "plain")
        os.makedirs(nogit)
        self.assertEqual(self.merge_decide("git merge prorab/x", nogit)[0], 2)  # cannot tell
        self.assertEqual(self.merge_decide("git status", nogit), (0, ""))

    def test_guard_merge_branch_sees_git_spelled_around_the_gate(self):
        repo = self.git_dir()
        for command in ("g''it commit -m a", "g\\it commit -m a", "\"g\"it reset --hard",
                        "$g't' commit", "${g}it commit", "eval 'g'i't commit'",
                        "eval git status", "echo ok && g''it push2", "X=1 g''it commit",
                        "$(printf 'g%s' it) commit", "`echo git` commit", "g=gi; $g't' commit"):
            code, message = self.merge_decide(command, repo)
            self.assertEqual(code, 2, command)
        # plain commands of a crew stay allowed
        for command in ("python3 -m unittest", "ls -la src", "echo done", "cat .git/HEAD",
                        "git status"):
            self.assertEqual(self.merge_decide(command, repo), (0, ""), command)

    def test_guard_without_the_option_nothing_changes(self):
        repo = self.git_dir()
        self.assertEqual(self.decide("git merge --no-ff --no-edit prorab/x", cwd=repo)[0], 2)
        self.assertEqual(self.decide("git pull", cwd=repo), (0, ""))
        self.assertEqual(self.decide("git commit -m x", cwd=repo), (0, ""))

    def test_guard_merge_branch_bad_value_is_refused(self):
        repo = self.git_dir()
        for bad in ("", "-x", "a b", "a;b", "x\n"):
            self.assertEqual(self.decide("git status", cwd=repo, merge_branch=bad)[0], 2, bad)

    def test_guard_cli_takes_the_option(self):
        repo = self.git_dir()
        run = lambda command, *extra: subprocess.run(
            [sys.executable, "-I", HOOKS_PY, "guard"] + list(extra),
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                              "cwd": repo}),
            capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(run("git merge --no-ff --no-edit prorab/x",
                             "--merge-branch", "prorab/x").returncode, 0)
        self.assertEqual(run("git merge --no-ff --no-edit prorab/x").returncode, 2)
        self.assertEqual(run("git pull", "--merge-branch", "prorab/x").returncode, 2)

    def test_merge_settings_carry_the_option_and_nothing_else_changes(self):
        shared = hooks.settings_dict(self.d)
        merge = hooks.settings_dict(self.d, merge_branch=self.BRANCH)
        self.assertNotIn("--merge-branch", json.dumps(shared))
        (_, bash) = merge["hooks"]["PreToolUse"]
        self.assertEqual(bash["hooks"][0]["command"],
                         "python3 -I %s guard --merge-branch %s" % (HOOKS_PY, self.BRANCH))
        merge["hooks"]["PreToolUse"][1]["hooks"][0]["command"] = "python3 -I %s guard" % HOOKS_PY
        self.assertEqual(merge, shared)
        for bad in ("", "-x", "a b", "a;b", "$(x)"):
            with self.assertRaises(ValueError):
                hooks.settings_dict(self.d, merge_branch=bad)

    def test_write_merge_settings_is_a_file_per_work_order_beside_the_shared_one(self):
        shared = hooks.write_settings(self.d)
        with open(shared, "rb") as fh:
            before = fh.read()
        path = hooks.write_settings(self.d, merge_branch=self.BRANCH, work_order="N1")
        self.assertEqual(path, os.path.join(os.path.dirname(shared),
                                            "crew-settings-merge-N1.json"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(sorted(data), ["feedbackSurveyRate", "hooks", "promptSuggestionEnabled"])
        self.assertIn("--merge-branch %s" % self.BRANCH, json.dumps(data))
        with open(shared, "rb") as fh:
            self.assertEqual(fh.read(), before)
        other = hooks.write_settings(self.d, merge_branch="prorab/y", work_order="N2")
        self.assertNotEqual(other, path)
        with open(path, encoding="utf-8") as fh:
            self.assertIn("prorab/x", fh.read())
        self.assertEqual(sorted(os.listdir(os.path.dirname(shared))),
                         ["crew-settings-merge-N1.json", "crew-settings-merge-N2.json",
                          "crew-settings.json"])

    def test_write_merge_settings_refuses_a_bad_work_order_or_a_half_pair(self):
        for wo in ("", "../x", "a/b", ".x", "a b"):
            with self.assertRaises(ValueError, msg=wo):
                hooks.write_settings(self.d, merge_branch=self.BRANCH, work_order=wo)
        with self.assertRaises(ValueError):
            hooks.write_settings(self.d, merge_branch=self.BRANCH)
        with self.assertRaises(ValueError):
            hooks.write_settings(self.d, work_order="N1")
        self.assertFalse(os.path.exists(os.path.dirname(self.d["hooks"]["settings_path"])))


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
