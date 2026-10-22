#!/usr/bin/env python3
"""Bounded, parallel test runner (standard library only).

    python3 prorab/tests/run.py [--fast] [--jobs N] [--bound S] [--stall S]
                                [--log-dir D] [--root DIR] [MODULE ...]

Without modules: every prorab/tests/test_*.py. Each bd-backed module (test_journal,
test_shift) is a group of its own; all other modules form one group. At most --jobs
groups run at once, each in a child python3 that runs its modules with unittest at
verbosity 2 and prints every test's time into <log dir>/<group>.log.

A child that runs longer than --bound seconds, or whose log has not grown for --stall
seconds, gets its stack dumped (faulthandler, all threads) and is killed with its process
group; the group is reported as `bound` or `stall`. The exit code is 0 only when every
group passed. --fast sets PRORAB_FAST=1 for the children (the fast tier skips the tests
that run the real bd).

--root DIR is the directory the children run in and import from (default: the repo root);
module names are dotted, relative to it.
"""
import argparse
import faulthandler
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
BD_MODULES = ("test_journal", "test_shift")
PACKAGE = "prorab.tests"
RESULT_PREFIX = "RUN-RESULT "
TIMING = re.compile(r"^  \[(\d+(?:\.\d+)?)s\] (\S+)$")
DUMP_WAIT_S = 1.0
POLL_S = 0.2


def discover(root=REPO_ROOT):
    """Dotted names of every prorab/tests/test_*.py under root."""
    paths = sorted(glob.glob(os.path.join(root, "prorab", "tests", "test_*.py")))
    return ["%s.%s" % (PACKAGE, os.path.basename(p)[:-3]) for p in paths]


def group_modules(modules):
    """[(group name, [modules])]: each bd-backed module alone, everything else together."""
    groups, rest = [], []
    for m in modules:
        if m.rsplit(".", 1)[-1] in BD_MODULES:
            groups.append((m.rsplit(".", 1)[-1], [m]))
        else:
            rest.append(m)
    if rest:
        groups.append(("rest", rest))
    return groups


# ---- child ------------------------------------------------------------------------

class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        self._t0 = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        self.stream.write("  [%.1fs] %s\n" % (time.monotonic() - self._t0, test.id()))
        self.stream.flush()


def child_main(root, modules):
    os.chdir(root)
    sys.path.insert(0, root)
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    suite = unittest.TestSuite()
    for name in modules:
        try:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
        except unittest.SkipTest as e:  # a module that skips itself at import
            reason = str(e)

            class Skipped(unittest.TestCase):
                @unittest.skip(reason)
                def runTest(self):
                    pass
            Skipped.__name__ = Skipped.__qualname__ = "ModuleSkipped"
            Skipped.__module__ = name
            suite.addTest(Skipped())
    t0 = time.monotonic()
    r = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult, stream=sys.stdout).run(suite)
    print(RESULT_PREFIX + json.dumps({
        "run": r.testsRun, "failures": len(r.failures), "errors": len(r.errors),
        "skipped": len(r.skipped), "time": round(time.monotonic() - t0, 1)}), flush=True)
    return 0 if r.wasSuccessful() else 1


# ---- parent -----------------------------------------------------------------------

class Group:
    def __init__(self, name, modules, log_dir):
        self.name, self.modules = name, modules
        self.log_path = os.path.join(log_dir, name + ".log")
        self.proc = self.log = None
        self.started = self.last_growth = self.elapsed = None
        self.size = 0
        self.status = None  # ok | failed | bound | stall
        self.result = None

    def start(self, root, env):
        self.log = open(self.log_path, "wb")
        cmd = [sys.executable, "-u", os.path.abspath(__file__), "--child", "--root", root]
        self.proc = subprocess.Popen(cmd + self.modules, stdout=self.log,
                                     stderr=subprocess.STDOUT, env=env, cwd=root,
                                     start_new_session=True)
        self.started = self.last_growth = time.monotonic()

    def poll(self, bound_s, stall_s):
        """True when the group has finished."""
        now = time.monotonic()
        rc = self.proc.poll()
        if rc is not None:
            self._finish("ok" if rc == 0 else "failed", now)
            return True
        size = os.path.getsize(self.log_path)
        if size != self.size:
            self.size, self.last_growth = size, now
        if now - self.started > bound_s:
            self._kill("bound", now)
            return True
        if now - self.last_growth > stall_s:
            self._kill("stall", now)
            return True
        return False

    def _kill(self, why, now):
        try:
            self.proc.send_signal(signal.SIGUSR1)  # faulthandler: all threads into the log
        except OSError:
            pass
        time.sleep(DUMP_WAIT_S)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)  # the child and anything it started
        except OSError:
            pass
        self.proc.wait()
        self._finish(why, now)

    def abort(self):
        """Kill the group's process group (the child and anything it started)."""
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except OSError:
            pass
        self.proc.wait()
        self.log.close()

    def _finish(self, status, now):
        self.status = status
        self.elapsed = now - self.started
        self.log.close()
        self.result = parse_log(self.log_path)


def parse_log(path):
    """Counts and per-test times from a group log (partial when the child was killed)."""
    out = {"run": 0, "failures": 0, "errors": 0, "skipped": 0, "time": None, "times": []}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TIMING.match(line.rstrip("\n"))
            if m:
                out["times"].append((float(m.group(1)), m.group(2)))
            elif line.startswith(RESULT_PREFIX):
                out.update(json.loads(line[len(RESULT_PREFIX):]))
    if not out["run"]:
        out["run"] = len(out["times"])
    return out


def run_groups(groups, root, env, jobs, bound_s, stall_s, out=sys.stdout):
    pending, running, done = list(groups), [], []
    try:
        while pending or running:
            while pending and len(running) < jobs:
                g = pending.pop(0)
                g.start(root, env)
                running.append(g)
                print("start %s: %s" % (g.name, " ".join(g.modules)), file=out, flush=True)
            for g in list(running):
                if g.poll(bound_s, stall_s):
                    running.remove(g)
                    done.append(g)
                    print("end %s: %s in %.0f s" % (g.name, g.status, g.elapsed),
                          file=out, flush=True)
            time.sleep(POLL_S)
    finally:
        for g in running:  # interrupted (Ctrl-C, SIGTERM, an exception): leave nothing behind
            g.abort()
    return done


def summary(groups, out=sys.stdout):
    print("\nSummary", file=out)
    print("%-14s %-7s %6s %9s %7s %8s %8s" % (
        "group", "status", "tests", "failures", "errors", "skipped", "time"), file=out)
    times = []
    for g in groups:
        r = g.result
        print("%-14s %-7s %6d %9d %7d %8d %7.0fs" % (
            g.name, g.status, r["run"], r["failures"], r["errors"], r["skipped"], g.elapsed),
            file=out)
        times += r["times"]
        if g.status in ("bound", "stall"):
            print("  %s: stack dump in %s" % (g.status, g.log_path), file=out)
        elif g.status == "failed":
            print("  details in %s" % g.log_path, file=out)
    print("\nSlowest tests", file=out)
    for t, name in sorted(times, reverse=True)[:10]:
        print("  %7.1fs  %s" % (t, name), file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bounded, parallel test runner")
    ap.add_argument("modules", nargs="*", help="dotted test modules (default: all)")
    ap.add_argument("--fast", action="store_true", help="PRORAB_FAST=1 for the children")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--bound", type=float, default=1200, help="seconds per group")
    ap.add_argument("--stall", type=float, default=600, help="seconds without log growth")
    ap.add_argument("--log-dir")
    ap.add_argument("--root", default=REPO_ROOT)
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child:
        return child_main(args.root, args.modules)
    modules = args.modules or discover(args.root)
    log_dir = args.log_dir or tempfile.mkdtemp(prefix="prorab-tests-")
    os.makedirs(log_dir, exist_ok=True)
    print("logs: %s" % log_dir, flush=True)
    env = dict(os.environ)
    if args.fast:
        env["PRORAB_FAST"] = "1"
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))  # same path as Ctrl-C
    # A runner started in the background of a non-interactive shell (`run.py ... &`, a CI
    # step) inherits SIGINT as ignored, and Python then keeps it ignored: the interrupt path
    # below would never run and the groups would be left behind. Always handle it.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    t0 = time.monotonic()
    groups = [Group(n, m, log_dir) for n, m in group_modules(modules)]
    done = run_groups(groups, args.root, env, max(1, args.jobs), args.bound, args.stall)
    summary(done)
    print("\nwall time: %.0f s" % (time.monotonic() - t0))
    return 0 if all(g.status == "ok" for g in done) else 1


if __name__ == "__main__":
    sys.exit(main())
