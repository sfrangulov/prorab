#!/usr/bin/env python3
"""Machine-wide semaphore for heavy commands: n flock-ed lock files per kind.

Crews run ``slots.py run --kind heavy -- <cmd...>``; the lock is released when the
command ends (or when this process dies). Never touches the journal.
"""
import argparse
import fcntl
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402


def default_slots_dir():
    base = os.environ.get("PRORAB_CACHE") or os.path.expanduser("~/.cache/prorab")
    return os.path.join(base, "slots")


def slot_count(kind, directory=None):
    """The integer in ``<kind>.count`` when present, else machine.heavy_slots."""
    path = os.path.join(directory or default_slots_dir(), "%s.count" % kind)
    try:
        with open(path, encoding="utf-8") as fh:
            n = int(fh.read().strip())
        if n > 0:
            return n
    except (OSError, ValueError):
        pass
    return _defaults.load()["machine"]["heavy_slots"]


def _try_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def run(kind, cmd, slots_dir=None, n=None, poll_s=1.0):
    directory = slots_dir or default_slots_dir()
    os.makedirs(directory, exist_ok=True)
    count = n if n is not None else slot_count(kind, directory)
    while True:
        fd = None
        for i in range(count):
            fd = _try_lock(os.path.join(directory, "%s.%d.lock" % (kind, i)))
            if fd is not None:
                break
        if fd is not None:
            break
        time.sleep(poll_s)
    owner = os.path.join(directory, "%s.%d.owner" % (kind, i))
    try:
        _write_owner(owner, cmd)
        # the fd is O_CLOEXEC and close_fds is on: a grandchild cannot keep the slot
        return subprocess.run(cmd, close_fds=True).returncode
    finally:
        try:
            os.unlink(owner)
        except OSError:
            pass
        os.close(fd)


def _write_owner(path, cmd):
    """Who holds the slot: temp file + rename, so a reader never sees half a record."""
    rec = {"cwd": os.getcwd(), "pid": os.getpid(), "started_at": time.time(), "cmd": cmd[0]}
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, path)


def _owners(directory):
    """Owner records of slots whose lock is held now: ``{(kind, idx): record}``.

    An owner file whose lock is free is stale (its process died) and is ignored."""
    out = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.*.owner"))):
        kind, idx, _ = os.path.basename(path).rsplit(".", 2)
        lock = _try_lock(os.path.join(directory, "%s.%s.lock" % (kind, idx)))
        if lock is not None:
            os.close(lock)
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            out[(kind, idx)] = rec
    return out


def holders(slots_dir=None):
    """The owner records of the slots held now."""
    return list(_owners(slots_dir or default_slots_dir()).values())


def status(directory=None):
    """Busy slots per kind: ``{kind: {"slots": n, "busy": [i, ...], "owners": [record, ...]}}``."""
    directory = directory or default_slots_dir()
    out = {}
    owners = _owners(directory)
    for path in sorted(glob.glob(os.path.join(directory, "*.*.lock"))):
        kind, idx, _ = os.path.basename(path).rsplit(".", 2)
        entry = out.setdefault(kind, {"slots": slot_count(kind, directory), "busy": [],
                                      "owners": []})
        fd = _try_lock(path)
        if fd is None:
            entry["busy"].append(int(idx))
            if (kind, idx) in owners:
                entry["owners"].append(owners[(kind, idx)])
        else:
            os.close(fd)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--kind", default="heavy")
    p.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status()))
        return 0
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        ap.error("no command given")
    try:
        return run(a.kind, cmd)
    except OSError as e:
        print("slots: cannot run %s: %s" % (cmd[0], e), file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
