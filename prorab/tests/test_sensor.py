import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import sensor  # noqa: E402

TAP = os.path.join(SCRIPTS_DIR, "statusline_tap.sh")
SRC = ". " + shlex.quote(TAP)
SENSOR_PY = os.path.join(SCRIPTS_DIR, "sensor.py")

STATUS_LINE = {"type": "command", "command": "/home/user/.claude/statusline.sh",
               "padding": 0, "refreshInterval": 60}


class SensorCase(unittest.TestCase):
    SHELL = "sh"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings = os.path.join(self.home, ".claude", "settings.json")

    def write_settings(self, obj):
        with open(self.settings, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    def read_settings(self):
        with open(self.settings, encoding="utf-8") as fh:
            return json.load(fh)

    def backups(self):
        d = os.path.dirname(self.settings)
        return [f for f in os.listdir(d) if f.startswith("settings.prorab-backup-")]

    def run_tap(self, stdin, orig=None, session=True):
        env = dict(os.environ, HOME=self.home, PRORAB_CACHE=self.cache)
        os.makedirs(self.cache, exist_ok=True)
        if orig is not None:
            with open(os.path.join(self.cache, "statusline.orig"), "w") as fh:
                fh.write(orig + "\n")
        return subprocess.run([self.SHELL, "-c", SRC], input=stdin, env=env,
                              capture_output=True)


class TestSensor(SensorCase):
    def base(self):
        return {"theme": "dark", "statusLine": dict(STATUS_LINE), "z": [1, 2]}

    def test_install_preserves_fields_and_backs_up(self):
        self.write_settings(self.base())
        with open(self.settings, "rb") as fh:
            before = fh.read()
        sensor.install(self.settings, self.cache, TAP)
        got = self.read_settings()
        self.assertEqual(got["statusLine"]["command"], SRC)
        for k in ("type", "padding", "refreshInterval"):
            self.assertEqual(got["statusLine"][k], STATUS_LINE[k])
        self.assertEqual(got["theme"], "dark")
        self.assertEqual(list(got), ["theme", "statusLine", "z"])
        b = self.backups()
        self.assertEqual(len(b), 1)
        with open(os.path.join(os.path.dirname(self.settings), b[0]), "rb") as fh:
            self.assertEqual(fh.read(), before)
        with open(os.path.join(self.cache, "statusline.orig")) as fh:
            self.assertEqual(fh.read().strip(), STATUS_LINE["command"])

    def test_install_idempotent(self):
        self.write_settings(self.base())
        sensor.install(self.settings, self.cache, TAP)
        with open(self.settings, "rb") as fh:
            once = fh.read()
        res = sensor.install(self.settings, self.cache, TAP)
        with open(self.settings, "rb") as fh:
            self.assertEqual(fh.read(), once)
        self.assertEqual(len(self.backups()), 1)
        self.assertFalse(res["changed"])
        with open(os.path.join(self.cache, "statusline.orig")) as fh:
            self.assertEqual(fh.read().strip(), STATUS_LINE["command"])

    def test_uninstall_restores_exactly(self):
        self.write_settings(self.base())
        sensor.install(self.settings, self.cache, TAP)
        sensor.uninstall(self.settings, self.cache)
        self.assertEqual(self.read_settings()["statusLine"], STATUS_LINE)
        self.assertEqual(list(self.read_settings()["statusLine"]), list(STATUS_LINE))
        sensor.uninstall(self.settings, self.cache)  # idempotent
        self.assertEqual(self.read_settings()["statusLine"], STATUS_LINE)

    def test_uninstall_removes_statusline_if_none_before(self):
        self.write_settings({"theme": "dark"})
        sensor.install(self.settings, self.cache, TAP)
        self.assertEqual(self.read_settings()["statusLine"]["command"], SRC)
        sensor.uninstall(self.settings, self.cache)
        self.assertEqual(self.read_settings(), {"theme": "dark"})
        self.assertEqual(os.listdir(self.cache) if os.path.isdir(self.cache) else [], [])

    def test_uninstall_removes_usage_dir(self):
        self.write_settings(self.base())
        sensor.install(self.settings, self.cache, TAP)
        self.run_tap(json.dumps({"session_id": "s1"}).encode())
        self.assertTrue(os.path.isdir(os.path.join(self.cache, "usage")))
        sensor.uninstall(self.settings, self.cache)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "usage")))

    def test_multiline_original_command_runs_whole(self):
        multi = "\nprintf A\nprintf B\n"
        sl = dict(STATUS_LINE, command=multi)
        self.write_settings({"statusLine": sl})
        sensor.install(self.settings, self.cache, TAP)
        with open(os.path.join(self.cache, "statusline.orig"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), multi)
        r = self.run_tap(json.dumps({"session_id": "s1"}).encode())
        self.assertEqual(r.stdout, b"AB")
        sensor.uninstall(self.settings, self.cache)
        self.assertEqual(self.read_settings()["statusLine"], sl)

    def test_tap_path_with_space_is_quoted(self):
        d = os.path.join(self.tmp, "my dir")
        os.makedirs(d)
        tap = os.path.join(d, "statusline_tap.sh")
        shutil.copy(TAP, tap)
        os.chmod(tap, 0o755)
        self.write_settings(self.base())
        sensor.install(self.settings, self.cache, tap)
        cmd = self.read_settings()["statusLine"]["command"]
        self.assertEqual(cmd, ". " + shlex.quote(tap))
        env = dict(os.environ, HOME=self.home, PRORAB_CACHE=self.cache)
        data = json.dumps({"session_id": "q1"}).encode()
        with open(os.path.join(self.cache, "statusline.orig"), "w") as fh:
            fh.write("printf Z")
        r = subprocess.run(["sh", "-c", cmd], input=data, env=env, capture_output=True)
        self.assertEqual(r.stdout, b"Z")
        with open(self.settings, "rb") as fh:
            once = fh.read()
        self.assertFalse(sensor.install(self.settings, self.cache, tap)["changed"])
        with open(self.settings, "rb") as fh:
            self.assertEqual(fh.read(), once)
        self.assertEqual(len(self.backups()), 1)
        sensor.uninstall(self.settings, self.cache)
        self.assertEqual(self.read_settings()["statusLine"], STATUS_LINE)

    def test_cli_install_with_spaced_path_executes(self):
        d = os.path.join(self.tmp, "my dir")
        os.makedirs(d)
        for name in ("sensor.py", "statusline_tap.sh"):
            shutil.copy(os.path.join(SCRIPTS_DIR, name), os.path.join(d, name))
            os.chmod(os.path.join(d, name), 0o755)
        self.write_settings(self.base())
        env = dict(os.environ, HOME=self.home, PRORAB_CACHE=self.cache)
        r = subprocess.run([sys.executable, os.path.join(d, "sensor.py"), "install",
                            "--yes"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmd = self.read_settings()["statusLine"]["command"]
        tap = os.path.join(os.path.realpath(d), "statusline_tap.sh")
        self.assertEqual(cmd, ". " + shlex.quote(tap))
        with open(os.path.join(self.cache, "statusline.orig")) as fh:
            self.assertEqual(fh.read(), STATUS_LINE["command"])
        with open(os.path.join(self.cache, "statusline.orig"), "w") as fh:
            fh.write("printf OUT")
        data = json.dumps({"session_id": "c1"}).encode()
        r = subprocess.run(["sh", "-c", cmd], input=data, env=env, capture_output=True)
        self.assertEqual(r.stdout, b"OUT")
        r = subprocess.run([sys.executable, os.path.join(d, "sensor.py"), "install",
                            "--yes"], env=env, capture_output=True, text=True)
        self.assertEqual(len(self.backups()), 1)

    def test_backup_keeps_private_mode(self):
        self.write_settings(self.base())
        os.chmod(self.settings, 0o600)
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        sensor.install(self.settings, self.cache, TAP)
        b = os.path.join(os.path.dirname(self.settings), self.backups()[0])
        self.assertEqual(os.stat(b).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)

    def test_install_converts_exec_form(self):
        self.write_settings(self.base())
        sensor.install(self.settings, self.cache, TAP)
        cfg = self.read_settings()
        cfg["statusLine"]["command"] = shlex.quote(TAP)  # an older, exec'd install
        self.write_settings(cfg)
        res = sensor.install(self.settings, self.cache, TAP)
        self.assertTrue(res["changed"])
        self.assertEqual(self.read_settings()["statusLine"]["command"], SRC)
        with open(os.path.join(self.cache, "statusline.orig")) as fh:
            self.assertEqual(fh.read(), STATUS_LINE["command"])
        sensor.uninstall(self.settings, self.cache)
        self.assertEqual(self.read_settings()["statusLine"], STATUS_LINE)

    def test_sourced_tap_passes_original_exit_code(self):
        data = json.dumps({"session_id": "s1"}).encode()
        r = self.run_tap(data, orig="printf A; exit 7")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (7, b"A", b""))
        r = self.run_tap(data, orig="printf A; printf B")
        self.assertEqual(r.stdout, b"AB")

    def test_exec_path_for_plain_commands(self):
        data = json.dumps({"session_id": "s1"}).encode()
        script = os.path.join(self.tmp, "orig.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\ncat >/dev/null\nprintf 'ok %s' \"$1\"\nexit 4\n")
        os.chmod(script, 0o755)
        r = self.run_tap(data, orig="%s arg" % script)
        self.assertEqual((r.returncode, r.stdout), (4, b"ok arg"))
        # leading assignment and trailing newline in the stored file use the safe path
        r = self.run_tap(data, orig="X=1 %s arg" % script)
        self.assertEqual((r.returncode, r.stdout), (4, b"ok arg"))
        # the original sees the saved JSON on stdin
        r = self.run_tap(data, orig="cat")
        self.assertEqual(r.stdout, data)
        r = self.run_tap(data, orig="wc -c < /dev/null")
        self.assertEqual(r.stdout.strip(), b"0")

    def test_status(self):
        self.write_settings(self.base())
        self.assertFalse(sensor.status(self.cache, 300)["installed"])
        sensor.install(self.settings, self.cache, TAP)
        st = sensor.status(self.cache, 300)
        self.assertTrue(st["installed"])
        self.assertFalse(st["fresh"])
        self.assertEqual(st["sessions"], 0)
        self.run_tap(json.dumps({"session_id": "s1"}).encode())
        st = sensor.status(self.cache, 300)
        self.assertTrue(st["fresh"])
        self.assertEqual(st["sessions"], 1)
        self.assertLess(st["age_s"], 60)

    def test_tap_passes_output_through(self):
        data = json.dumps({"session_id": "s1", "x": 1}).encode()
        r = self.run_tap(data, orig="printf X")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"X")
        with open(os.path.join(self.cache, "usage", "s1.json"), "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_tap_passes_stdin_to_orig(self):
        data = json.dumps({"session_id": "s1"}).encode()
        r = self.run_tap(data, orig="cat; exit 3")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout, data)

    def test_tap_per_session_files(self):
        self.run_tap(json.dumps({"session_id": "a"}).encode())
        self.run_tap(json.dumps({"session_id": "b"}).encode())
        usage = os.path.join(self.cache, "usage")
        self.assertEqual(sorted(os.listdir(usage)), ["a.json", "b.json"])
        # the tap never prunes: an old file stays for the reader to remove
        old = os.path.join(usage, "old.json")
        with open(old, "w") as fh:
            fh.write("{}")
        t = time.time() - 90000
        os.utime(old, (t, t))
        self.run_tap(json.dumps({"session_id": "a"}).encode())
        self.assertEqual(sorted(os.listdir(usage)), ["a.json", "b.json", "old.json"])

    def test_tap_session_id_with_space_after_colon(self):
        self.run_tap(b'{"a": 1, "session_id" :  "s-9_x", "b": 2}')
        self.assertEqual(os.listdir(os.path.join(self.cache, "usage")), ["s-9_x.json"])

    def test_tap_invalid_session_ids_write_nothing(self):
        for raw in (b'{"session_id": "../evil"}', b'{"session_id": ""}',
                    b'{"session_id": "a b"}', b'{"session_id": "a.b"}',
                    b'{"session_id": 5}', b'{"x": 1}', b"not json", b""):
            r = self.run_tap(raw, orig="printf Y")
            self.assertEqual((r.returncode, r.stdout), (0, b"Y"), raw)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "usage")))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "evil.json")))

    def test_tap_orig_gets_same_stdin_when_invalid(self):
        for orig in ("cat", "cat; true"):  # exec path and subshell path
            r = self.run_tap(b"plain text\nline2", orig=orig)
            self.assertEqual(r.stdout.rstrip(b"\n"), b"plain text\nline2", orig)

    def test_tap_silent_on_stderr_and_failures(self):
        data = json.dumps({"session_id": "s1"}).encode()
        r = self.run_tap(data, orig="echo err >&2; exit 5")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (5, b"", b""))
        env = dict(os.environ, HOME=self.home, PRORAB_CACHE="/dev/null/x")
        r = subprocess.run([self.SHELL, "-c", SRC], input=data, env=env, capture_output=True)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_tap_without_orig_prints_nothing(self):
        r = self.run_tap(json.dumps({"session_id": "s1"}).encode())
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_cli_install_requires_yes(self):
        self.write_settings(self.base())
        with open(self.settings, "rb") as fh:
            before = fh.read()
        env = dict(os.environ, HOME=self.home, PRORAB_CACHE=self.cache)
        r = subprocess.run([sys.executable, SENSOR_PY, "install"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn(STATUS_LINE["command"], r.stdout)
        with open(self.settings, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.backups(), [])
        r = subprocess.run([sys.executable, SENSOR_PY, "install", "--yes"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read_settings()["statusLine"]["command"], SRC)
        r = subprocess.run([sys.executable, SENSOR_PY, "status"], env=env,
                           capture_output=True, text=True)
        self.assertTrue(json.loads(r.stdout)["installed"])
        r = subprocess.run([sys.executable, SENSOR_PY, "uninstall"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.read_settings()["statusLine"], STATUS_LINE)


@unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
class TestSensorZsh(TestSensor):
    """The same checks with zsh as the shell Claude Code starts."""
    SHELL = "zsh"


if __name__ == "__main__":
    unittest.main()
