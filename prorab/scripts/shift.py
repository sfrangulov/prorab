#!/usr/bin/env python3
"""Shift: night-shift preflight, caffeinate, the foreman's handover, the reconciliation of
"issuing" records and the morning report.

The report is written once and no external command runs for it (no polish step). The short
summary in chat, in the customer's language, is the foreman's own.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _defaults  # noqa: E402
import crew  # noqa: E402
import herdr_cli  # noqa: E402
import journal  # noqa: E402
import limits  # noqa: E402
import scheduler  # noqa: E402
import sensor  # noqa: E402
import slots  # noqa: E402
import transcript  # noqa: E402

SHIFT_LABEL = "prorab:shift"
INCIDENT_TAG = "[incident]"  # the foreman starts an `answer` act with it to log an incident
# `answer` acts the scripts write themselves that also count as incidents
INCIDENT_PREFIXES = ("crew waits for the limit reset", "handover loop", "provider error")
INCIDENT_EVENTS = ("provider_error", "hung_unclear", "limit_screen")
BRANCH_OPTIONS = ("merge it into the default branch, open a pull request, or keep the branch "
                  "as it is; the foreman does none of these without the customer's word")


def _g(value):
    return "%g" % round(float(value), 2)


def _cache_dir():
    return os.environ.get("PRORAB_CACHE") or os.path.expanduser("~/.cache/prorab")


# ---- preflight ----------------------------------------------------------------------

def _on_mains():
    """True / False from `pmset -g batt`; None when it cannot be asked."""
    try:
        p = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return "AC Power" in p.stdout


def _open_work_orders():
    return [n for n in journal.bd(["list", "--label", journal.WORK_ORDER_LABEL, "--no-parent",
                                   "--limit", "0"]) if n.get("status") != "closed"]


def _missing_clearances():
    """Open work orders and stages that need a clearance and have no `clearance:granted`."""
    out = []
    for wo in _open_work_orders():
        beads = [wo] + [c for c in journal.bd(["list", "--parent", wo["id"], "--all", "--limit",
                                               "0"]) if c.get("status") != "closed"]
        for b in beads:
            labels = b.get("labels") or []
            if "needs:clearance" in labels and "clearance:granted" not in labels:
                out.append(b["id"])
    return out


def _rates_and_limits(d, now):
    lim = scheduler.gather_limits(d, now)
    return lim, limits.rates(scheduler.readings(), d, now)


def _cap(d, now, lim, night):
    return scheduler.cap_for(now, lim.get("five_hour_resets_at"), d["night"]["morning"], d,
                             night=night, week_known=lim.get("seven_day") is not None)


def _cap_text(cap):
    mass = "none" if cap["no_new_mass"] is None else "%g %%" % cap["no_new_mass"]
    return "mode %s, no new mass runs at %s, stop at %g %%" % (cap["mode"], mass, cap["stop"])


def preflight(d, night=False):
    """Problems (plain lines) and notes (lines starting with "note: ") before a shift."""
    now = time.time()
    out = []
    mains = _on_mains()
    if mains is False:
        out.append("not on mains power: plug in before leaving the machine alone")
    elif mains is None:
        out.append("note: the power source could not be checked (pmset is unavailable)")
    kv = journal.kv_all()
    for n in journal.ready_work_orders():
        if journal.estimate(n, kv)["usd"] is None:
            out.append("work order %s has no estimate in dollars" % n)
    for bead in _missing_clearances():
        out.append("%s needs a clearance and has none (label clearance:granted)" % bead)
    lim, rates = _rates_and_limits(d, now)
    st = sensor.status(_cache_dir(), d["limits"]["sensor_max_age_s"])
    if not st["installed"]:
        out.append("note: the limit sensor is not installed; limits come from status lines "
                   "and the rate may stay at its default")
    elif not st["fresh"]:
        out.append("note: the limit sensor is installed but has no fresh reading")
    if lim.get("seven_day") is None:
        out.append("note: the week percent is unknown (it is not treated as 0); at night "
                   "no new mass runs are issued")
    b = journal.budget(kv)
    out.append("note: shift budget $%s, +%s pp of the week; window rate: %s; week rate: %s"
               % (_g(b["usd"]), _g(b["week_pp"]), journal.rate_text(rates["five_hour"]),
                  journal.rate_text(rates["seven_day"])))
    if night:
        out.append("note: night shift; the customer is expected back at %s; 5h window "
                   "thresholds now: %s" % (d["night"]["morning"], _cap_text(_cap(d, now, lim, True))))
    return out


def start_caffeinate(pid):
    """`caffeinate -is -w <pid>`: the machine stays awake while the foreman's process lives.

    Spawned without a Popen object (nothing to reap) and with its standard streams on
    /dev/null, so a caller that captures this script's output is not kept waiting."""
    actions = [(os.POSIX_SPAWN_OPEN, fd, os.devnull, os.O_RDWR, 0) for fd in (0, 1, 2)]
    args = ["caffeinate", "-is", "-w", str(pid)]
    try:
        return os.posix_spawnp("caffeinate", args, os.environ, file_actions=actions, setsid=True)
    except (TypeError, NotImplementedError):  # no setsid before Python 3.13
        return os.posix_spawnp("caffeinate", args, os.environ, file_actions=actions)


def begin(d, night=False, foreman_session=None):
    now = time.time()
    problems = preflight(d, night)
    lim, rates = _rates_and_limits(d, now)
    journal.begin_shift(lim.get("seven_day"), foreman_session or herdr_cli.own_session())
    journal.kv_set("shift.night", 1 if night else 0)
    machine = journal.machine()
    slots_dir = slots.default_slots_dir()
    os.makedirs(slots_dir, exist_ok=True)
    with open(os.path.join(slots_dir, "heavy.count"), "w", encoding="utf-8") as fh:
        fh.write("%d\n" % machine["heavy_slots"])
    return {"problems": problems, "budget": journal.budget(), "machine": machine,
            "rates": rates, "cap": _cap(d, now, lim, night)}


# ---- foreman handover ------------------------------------------------------------------

def _free_foreman_name():
    taken = {a.get("name") for a in herdr_cli.agents()}
    n = 2
    while "prorab-%d" % n in taken:
        n += 1
    return "prorab-%d" % n


def handoff_prorab(caller_pane, act, timeout_s=600, poll_s=5):
    """Start a new foreman in a new pane and wait until it holds `prorab:active`.

    `prorab:handoff` names the new pane before the new foreman starts, so its first lock
    attempt succeeds. On a timeout the handoff is withdrawn and the old foreman keeps working."""
    d = _defaults.load()
    now = time.time()
    spend = journal.shift_spend(d, now)
    rates = limits.rates(scheduler.readings(), d, now)
    bead = journal.bd(["create", "Shift handover %s" % time.strftime("%Y-%m-%d %H:%M"),
                       "-d", "Foreman handover from pane %s" % caller_pane,
                       "-l", SHIFT_LABEL, "--silent"], json_out=False).strip()
    if not bead:
        raise journal.JournalError("bd create returned no id")
    journal.add_act(bead, "shift", "%s\n\n%s" % (act.rstrip(), journal.spend_block(spend, rates)))
    name = _free_foreman_name()
    session_id = str(uuid.uuid4())
    pane = herdr_cli.run(["pane", "split", caller_pane, "--direction",
                          crew._split_direction(caller_pane), "--no-focus", "--cwd",
                          os.path.expanduser("~/projects/prorab")])["result"]["pane"]["pane_id"]
    journal.kv_set("prorab:handoff", pane)
    try:
        herdr_cli.run(["agent", "start", name, "--kind", "claude", "--pane", pane]
                      + crew._claude_args(("--session-id", session_id)), timeout_s=90)
    except herdr_cli.HerdrError as e:
        journal.kv_clear("prorab:handoff")
        return {"status": "start_failed", "reason": str(e), "new_pane": pane, "bead": bead}
    journal.bd(["update", bead, "--metadata", json.dumps({"session_id": session_id,
                                                           "name": name, "pane": pane})])
    journal.add_foreman_session(session_id)
    try:
        delivery = herdr_cli.send(name, "Load the prorab skill and take over the shift from "
                                        "bead %s." % bead)
    except herdr_cli.HerdrError as e:
        delivery = "error: %s" % e
    deadline = time.monotonic() + timeout_s
    while True:
        holder = journal.kv_get("prorab:active")
        if holder is not None and holder != caller_pane:
            return {"status": "handed_over", "new_pane": pane, "bead": bead,
                    "delivery": delivery}
        if time.monotonic() >= deadline:
            # the new foreman may take the lock between the read above and the withdrawal:
            # a consumed handoff or a changed holder after the withdrawal is a handover
            consumed = journal.kv_get("prorab:handoff") != pane
            journal.kv_clear("prorab:handoff")
            holder = journal.kv_get("prorab:active")
            status = ("handed_over" if consumed or (holder is not None and holder != caller_pane)
                      else "timeout")
            return {"status": status, "new_pane": pane, "bead": bead, "delivery": delivery}
        time.sleep(poll_s)


def reconcile_issues(agents_list=None):
    """Match every "issuing" record with the agents herdr lists: a crew found in its pane
    under its name is registered; a missing one makes the work order issuable again."""
    agents_list = herdr_cli.agents() if agents_list is None else agents_list
    out = []
    for work_order, rec in sorted(journal.issues("issuing").items()):
        name, pane = rec.get("name"), rec.get("pane")
        a = herdr_cli.agent_by_pane(pane, agents_list) if pane else None
        found = a is not None and a.get("name") == name
        if found:
            journal.register_crew(name)
        else:
            journal.issue_set(work_order, "not_issued")
        out.append({"work_order": work_order, "name": name, "pane": pane, "found": found})
    return out


# ---- morning report ---------------------------------------------------------------------

def _clock(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _children(work_order):
    return journal.bd(["list", "--parent", work_order, "--all", "--limit", "0"])


def _all_work_orders():
    return journal.bd(["list", "--label", journal.WORK_ORDER_LABEL, "--no-parent", "--all",
                       "--limit", "0"])


def _recent(rec, start):
    return start is None or rec["ts"] is None or rec["ts"] >= start - 1


def _first_line(text):
    return (text.strip().splitlines() or [""])[0]


def _open_gates(bead):
    out = []
    for dep in journal.bd(["show", bead])[0].get("dependencies") or []:
        if dep.get("issue_type") == "gate" and dep.get("status") != "closed":
            desc = dep.get("description") or ""
            m = re.search(r"Reason: (.*)", desc, re.S)
            out.append({"id": dep["id"], "bead": bead, "reason": (m.group(1) if m else desc).strip()})
    return out


def _accepted(work_orders, start):
    lines = []
    for wo in work_orders:
        rows = []
        for bead, title in [(wo["id"], None)] + [(c["id"], c.get("title")) for c in _children(wo["id"])]:
            for rec in journal.act_records(bead, "acceptance"):
                if _recent(rec, start):
                    rows.append("  - %s%s: %s" % (bead, " " + title if title else "",
                                                   _first_line(rec["text"])))
        if rows:
            closed = " (work order closed)" if wo.get("status") == "closed" else ""
            lines.append("- %s %s%s" % (wo["id"], wo.get("title", ""), closed))
            lines += rows
    return lines or ["Nothing was accepted."]


def _push_text(sent):
    if sent in (None, ""):
        return "no push went out; this report is the only notice"
    try:
        return "push sent at %s" % _clock(float(sent))
    except ValueError:
        return "push sent"


def _waiting(work_orders, notified):
    lines = []
    for wo in work_orders:
        if wo.get("status") == "closed":
            continue
        beads = [wo["id"]] + [c["id"] for c in _children(wo["id"]) if c.get("status") != "closed"]
        gates = [g for b in beads for g in _open_gates(b)]
        if not gates:
            continue
        lines.append("- %s %s" % (wo["id"], wo.get("title", "")))
        for g in gates:
            push = _push_text(notified.get(g["id"]))
            lines.append("  - gate %s on %s: %s" % (g["id"], g["bead"], push))
            m = re.match(r"consent prompt: (\w+)", g["reason"])
            if m:
                lines.append("    Consent prompt, kind: %s. The foreman never answers it; "
                             "the customer does." % herdr_cli.CONSENT_NAMES.get(m.group(1), m.group(1)))
            lines += ["    > %s" % ln for ln in g["reason"].splitlines()]
    return lines or ["Nothing waits for the customer."]


def _finished_branches(work_orders):
    lines = []
    for wo in work_orders:
        rec = journal.issue(wo["id"]) or {}
        if wo.get("status") != "closed" or not rec.get("worktree"):
            continue
        wt = rec["worktree"]
        detail = ""
        if os.path.isdir(wt):
            try:
                ev = crew.evidence(wt)
                detail = ", %d commit(s) ahead%s" % (len(ev["commits"]),
                                                     ", uncommitted changes" if ev["dirty"] else "")
            except Exception:  # a git hiccup must not stop the report
                detail = ""
        lines.append("- branch prorab/%s (work order %s, worktree %s%s)" % (wo["id"], wo["id"], wt, detail))
    if not lines:
        return []
    return lines + ["", "For each branch: %s." % BRANCH_OPTIONS]


def _usd(v):
    return "$%.2f" % v


def _work_order_budget_lines(sp, d):
    lines = []
    for wo in sorted(sp["work_orders"]):
        try:
            s = crew.work_order_spend(wo, d)
        except ValueError:
            continue
        title = (journal.bd(["show", wo]) or [{}])[0].get("title", "")
        line = "- %s %s: %s" % (wo, title, _usd(s["usd"]))
        if s["estimate_usd"] is None:
            line += ", no estimate"
        else:
            line += " of %s estimated" % _usd(s["estimate_usd"])
            if s["over"]:
                line += " (over the estimate by %s)" % _usd(s["usd"] - s["estimate_usd"])
        pps = [("%.2f pp of the %s" % (pp, label)) for label, pp in
               (("window", s["pp_window"]), ("week", s["pp_week"])) if pp is not None]
        if pps:
            line += "; " + ", ".join(pps)
        lines.append(line)
    return lines


def _counts_lines(work_orders, kv):
    lines = []
    for wo in work_orders:
        c = {k: int(kv.get("spent.%s.%s" % (wo["id"], k)) or 0) for k in journal.SPENT_KEYS}
        if any(c.values()):
            lines.append("- %s: tokens %d, sessions %d, agent runs %d"
                         % (wo["id"], c["tokens"], c["sessions"], c["runs"]))
    return lines


def _budget(sp, d, kv, lim, rates, start, work_orders):
    b = journal.budget(kv)
    lines = _work_order_budget_lines(sp, d)
    for key, r in sorted(sp["research"].items()):
        line = "- research %s: %s" % (key, _usd(r["usd"]))
        if r["estimate_usd"] is not None:
            line += " of %s estimated" % _usd(r["estimate_usd"])
            if r["over"]:
                line += " (over the estimate)"
        lines.append(line)
    lines.append("- Foreman: %s" % _usd(sp["foreman_usd"]))
    lines.append("")
    total = "- Shift total: %s of %s budget" % (_usd(sp["usd"]), _usd(b["usd"]))
    lines.append(total + (" (over budget)" if sp["usd"] > b["usd"] else ""))
    wk_start, wk_now = kv.get("shift.week_start"), lim.get("seven_day")
    if wk_start in (None, "") or wk_now is None:
        lines.append("- Week rise: unknown (the week percent was not known at the start or is not "
                     "known now)")
    else:
        rise = float(wk_now) - float(wk_start)
        lines.append("- Week rise: +%s pp of the week against +%s pp budget%s"
                     % (_g(rise), _g(b["week_pp"]), " (over budget)" if rise > b["week_pp"] else ""))
    lines.append("- window rate: " + journal.rate_text(rates["five_hour"]))
    lines.append("- week rate: " + journal.rate_text(rates["seven_day"]))
    machine = transcript.cost(transcript.machine_files(since=start), d, since=start)["usd"]
    lines.append("- Machine spend over the shift: %s (every session on this machine, the "
                 "customer's own included)" % _usd(machine))
    counts = _counts_lines(work_orders, kv)
    if counts:
        lines += ["", "Counts per work order (for the record, not budget):"] + counts
    return lines


def _limits_lines(d, now, lim):
    five, week = lim.get("five_hour"), lim.get("seven_day")
    resets = lim.get("five_hour_resets_at")
    lines = ["- 5h window: %s%s" % ("unknown" if five is None else "%s %%" % _g(five),
                                   "" if resets is None else " (resets %s)" % _clock(resets)),
             "- week: %s" % ("unknown" if week is None else "%s %%" % _g(week)),
             "- source: %s" % lim.get("source", "unknown")]
    night = journal.kv_get("shift.night") == "1"
    lines.append("- 5h thresholds in force: %s" % _cap_text(_cap(d, now, lim, night)))
    return lines


def _incidents(work_orders, start):
    lines = []
    for wo in work_orders:
        for bead in [wo["id"]] + [c["id"] for c in _children(wo["id"])]:
            for rec in journal.act_records(bead, "answer"):
                if not _recent(rec, start):
                    continue
                text = rec["text"]
                if text.startswith(INCIDENT_TAG):
                    lines.append("- %s: %s" % (bead, text[len(INCIDENT_TAG):].strip()))
                elif text.startswith(INCIDENT_PREFIXES):
                    lines.append("- %s: %s" % (bead, text))
    seen = set()
    log = scheduler.log_path()
    unknown = None
    if not os.path.exists(log):
        unknown = "missing"
    else:
        try:
            with open(log, encoding="utf-8"):
                pass
        except OSError:
            unknown = "unreadable"
    for r in scheduler.readings(since=start):
        for ev in r.get("events") or []:
            if isinstance(ev, str) and ev.split(" ")[0] in INCIDENT_EVENTS and ev not in seen:
                seen.add(ev)
                lines.append("- monitor: %s" % ev)
    if unknown:  # never claim what was not verified
        lines.append("Incidents unknown: monitor log %s" % unknown)
    return lines or ["No incidents."]


def _override_section(d, rec, now):
    since, until = float(rec["since"]), rec.get("until")
    end = now if until is None else float(until)
    pts = [r for r in scheduler.readings(since=since) if r["ts"] <= end]
    fives = [r["five_hour"] for r in pts if isinstance(r.get("five_hour"), (int, float))]
    weeks = [r["seven_day"] for r in pts if isinstance(r.get("seven_day"), (int, float))]
    rise5 = sum(max(0.0, b - a) for a, b in zip(fives, fives[1:]))
    rise7 = weeks[-1] - weeks[0] if len(weeks) >= 2 else None
    usd = transcript.cost(transcript.machine_files(since=since), d, since=since,
                          until=end)["usd"]
    return ["The customer's command 'ignore limits and finish' held for the goal %s, since %s, "
            "%s." % (rec["goal"], _clock(since),
                     "until " + _clock(until) if until is not None else "and still holds"),
            "- 5h window: +%s pp while it held (sum of rises between monitor readings)" % _g(rise5),
            "- week: %s while it held (last reading minus first)"
            % ("unknown" if rise7 is None else "+%s pp" % _g(rise7)),
            "- machine spend over the same time: %s" % _usd(usd)]


def _without_limits(d, kv, start, now):
    """One block per override that held during the shift (the journal keeps their history
    after the record itself has expired)."""
    lines = []
    for rec in journal.override_history(kv):
        if not isinstance(rec.get("since"), (int, float)) or not rec.get("goal"):
            continue
        until = rec.get("until")
        if until is not None and start is not None and until < start:
            continue
        lines += _override_section(d, rec, now) + [""]
    return lines[:-1]


def report(date, out_dir):
    """Write `out_dir/<date>.md` and return its path; ends the night shift."""
    d = _defaults.load()
    now = time.time()
    kv = journal.kv_all()
    started = kv.get("shift.started_at")
    start = float(started) if started not in (None, "") else None
    sp = journal.shift_spend(d, now, kv=kv)
    lim, rates = _rates_and_limits(d, now)
    work_orders = _all_work_orders()
    notified = {k[len("notified."):]: v for k, v in kv.items() if k.startswith("notified.")}
    parts = ["# Shift report %s" % date, ""]

    def section(title, lines):
        parts.extend(["## %s" % title, ""] + lines + [""])
    section("Accepted", _accepted(work_orders, start))
    section("Waiting for the customer", _waiting(work_orders, notified))
    branches = _finished_branches(work_orders)
    if branches:
        section("Finished branches", branches)
    section("Budget", _budget(sp, d, kv, lim, rates, start, work_orders))
    section("Limits", _limits_lines(d, now, lim))
    section("Incidents", _incidents(work_orders, start))
    free = _without_limits(d, kv, start, now)
    if free:
        section("Without limits", free)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.md" % date)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts).rstrip("\n") + "\n")
    journal.kv_set("shift.spent.usd", sp["usd"])
    journal.kv_clear("shift.night")
    return path


# ---- CLI -------------------------------------------------------------------------------

def _default_reports_dir():
    return os.path.join(os.path.dirname(journal.beads_dir().rstrip(os.sep)), "reports")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight").add_argument("--night", action="store_true")
    p = sub.add_parser("begin")
    p.add_argument("--night", action="store_true"); p.add_argument("--foreman-session")
    sub.add_parser("caffeinate").add_argument("pid", type=int)
    p = sub.add_parser("handoff")
    p.add_argument("caller_pane"); p.add_argument("--act-file", required=True)
    p.add_argument("--timeout", type=int, default=600)
    sub.add_parser("reconcile")
    p = sub.add_parser("report")
    p.add_argument("--date", default=datetime.date.today().isoformat())
    p.add_argument("--out-dir")
    a = ap.parse_args(argv)
    d = _defaults.load()
    journal.ensure_db()
    if a.cmd == "preflight":
        result = {"problems": preflight(d, a.night)}
    elif a.cmd == "begin":
        result = begin(d, a.night, a.foreman_session)
    elif a.cmd == "caffeinate":
        result = {"pid": start_caffeinate(a.pid)}
    elif a.cmd == "handoff":
        with open(a.act_file, encoding="utf-8") as fh:
            result = handoff_prorab(a.caller_pane, fh.read(), a.timeout)
    elif a.cmd == "reconcile":
        result = reconcile_issues()
    else:
        result = {"path": report(a.date, a.out_dir or _default_reports_dir())}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
