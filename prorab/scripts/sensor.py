#!/usr/bin/env python3
"""Install, remove and inspect the optional limits sensor (status-line tap)."""
import json
import os
import shlex
import shutil
import sys
import tempfile
import time

STATE = "statusline.state.json"
ORIG = "statusline.orig"


def _load(settings):
    with open(settings, encoding="utf-8") as fh:
        return json.load(fh)


def _write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dump(settings, obj):
    _write_atomic(settings, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _backup_path(settings):
    stem = os.path.splitext(settings)[0]
    return "%s.prorab-backup-%s" % (stem, time.strftime("%Y%m%d%H%M%S"))


def _plan(settings, tap):
    cfg = _load(settings)
    sl = cfg.get("statusLine")
    old = sl.get("command") if isinstance(sl, dict) else None
    return cfg, sl, old


def install(settings, cache, tap):
    cfg, sl, old = _plan(settings, tap)
    tap = shlex.quote(tap)
    if old == tap:
        return {"changed": False, "old": None, "new": tap, "backup": None}
    backup = _backup_path(settings)
    shutil.copy2(settings, backup)
    os.chmod(backup, os.stat(settings).st_mode & 0o777)
    os.makedirs(cache, exist_ok=True)
    state = {"had_statusline": "statusLine" in cfg, "statusLine": sl}
    _write_atomic(os.path.join(cache, STATE), json.dumps(state, ensure_ascii=False))
    orig = os.path.join(cache, ORIG)
    if isinstance(old, str) and old.strip():
        _write_atomic(orig, old)
    elif os.path.exists(orig):
        os.unlink(orig)
    if isinstance(sl, dict):
        sl["command"] = tap
    else:
        cfg["statusLine"] = {"type": "command", "command": tap}
    _dump(settings, cfg)
    return {"changed": True, "old": old, "new": tap, "backup": backup}


def uninstall(settings, cache):
    state_path = os.path.join(cache, STATE)
    restored = False
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        cfg = _load(settings)
        if state.get("had_statusline"):
            cfg["statusLine"] = state["statusLine"]
        else:
            cfg.pop("statusLine", None)
        _dump(settings, cfg)
        restored = True
    shutil.rmtree(os.path.join(cache, "usage"), ignore_errors=True)
    for name in (STATE, ORIG):
        try:
            os.unlink(os.path.join(cache, name))
        except OSError:
            pass
    try:
        os.rmdir(cache)  # only if empty; we created it
    except OSError:
        pass
    return {"restored": restored}


def status(cache, max_age_s):
    usage = os.path.join(cache, "usage")
    now = time.time()
    ages = []
    try:
        for name in os.listdir(usage):
            if name.endswith(".json") and not name.startswith("."):
                age = now - os.path.getmtime(os.path.join(usage, name))
                if age <= max_age_s:
                    ages.append(age)
    except OSError:
        pass
    return {"installed": os.path.exists(os.path.join(cache, STATE)),
            "fresh": bool(ages),
            "age_s": round(min(ages), 1) if ages else None,
            "sessions": len(ages)}


def _max_age():
    try:
        p = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                         "..", "references", "defaults.json")
        with open(p, encoding="utf-8") as fh:
            return int(json.load(fh)["limits"]["sensor_max_age_s"])
    except Exception:
        return 300


def main(argv):
    if not argv or argv[0] not in ("install", "uninstall", "status"):
        print("usage: sensor.py install [--yes] | uninstall | status", file=sys.stderr)
        return 2
    settings = os.path.expanduser("~/.claude/settings.json")
    cache = os.environ.get("PRORAB_CACHE") or os.path.expanduser("~/.cache/prorab")
    tap = os.path.join(os.path.dirname(os.path.realpath(__file__)), "statusline_tap.py")
    cmd = argv[0]
    if cmd == "status":
        print(json.dumps(status(cache, _max_age())))
        return 0
    if cmd == "uninstall":
        print(json.dumps(uninstall(settings, cache)))
        return 0
    _cfg, _sl, old = _plan(settings, tap)
    if old == shlex.quote(tap):
        print("already installed; nothing to change")
        return 0
    if "--yes" not in argv[1:]:
        print("Planned change in %s:" % settings)
        print("  statusLine.command: %s -> %s" % (old if old else "(none)", shlex.quote(tap)))
        print("  backup: %s" % _backup_path(settings))
        print("Re-run with --yes after the customer agrees.")
        return 2
    print(json.dumps(install(settings, cache, tap)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
