#!/usr/bin/env python3
"""Status-line tap: save the status-line JSON per session, then pass it on.

Installed (with the customer's consent) as Claude Code's statusLine.command.
Never prints anything of its own and always exits 0.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _cache_dir():
    return os.environ.get("PRORAB_CACHE") or os.path.expanduser("~/.cache/prorab")


def _retention_s():
    try:
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            "..", "references", "defaults.json")
        with open(path, encoding="utf-8") as fh:
            return float(json.load(fh)["limits"]["sensor_retention_s"])
    except Exception:
        return 86400.0


def _save(data, cache):
    session = json.loads(data.decode("utf-8")).get("session_id")
    if not isinstance(session, str) or not _SESSION_RE.match(session):
        return
    usage = os.path.join(cache, "usage")
    os.makedirs(usage, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=usage, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(usage, session + ".json"))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    limit = time.time() - _retention_s()
    for name in os.listdir(usage):
        p = os.path.join(usage, name)
        try:
            if os.path.getmtime(p) < limit:
                os.unlink(p)
        except OSError:
            pass


def _orig(cache):
    try:
        with open(os.path.join(cache, "statusline.orig"), encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def main():
    try:
        data = sys.stdin.buffer.read()
    except Exception:
        data = b""
    cache = _cache_dir()
    try:
        _save(data, cache)
    except Exception:
        pass
    try:
        cmd = _orig(cache)
        if cmd:
            res = subprocess.run(["/bin/sh", "-c", cmd], input=data,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            sys.stdout.buffer.write(res.stdout)
            sys.stdout.buffer.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
