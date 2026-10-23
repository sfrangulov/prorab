"""The reference templates match what the scripts read and check.

`acts.md` holds the blocks `crew.py` sends to crews; `work-order.md` holds the work-order
template with the stage-report format `crew.check_report` parses, and an example report that
passes it; `superpowers.md` and `passport.md` hold the process and the site passport template.
"""
import os
import re
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
import crew  # noqa: E402
import shift  # noqa: E402

REFERENCES = os.path.join(os.path.dirname(SCRIPTS_DIR), "references")
_ADDR = "t" + "@" + "x.invalid"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": _ADDR,
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": _ADDR}
_FENCE = re.compile(r"^(`{3,}|~{3,})")


def read(name):
    with open(os.path.join(REFERENCES, name), encoding="utf-8") as fh:
        return fh.read()


def split_sections(text):
    """{heading: body} for every `## ` heading outside a fenced block."""
    out, heading, fence = {}, None, None
    for line in text.splitlines():
        if fence is None and line.startswith("## "):
            heading = line[3:].strip()
            out[heading] = []
            continue
        if fence is None:
            m = _FENCE.match(line)
            if m:
                fence = m.group(1)
        elif line.strip() == fence:
            fence = None
        if heading is not None:
            out[heading].append(line)
    return {h: "\n".join(lines) for h, lines in out.items()}


def fenced_blocks(body):
    """The fenced blocks of a markdown text in order; a fence closes only on its own marker."""
    blocks, fence, buf = [], None, []
    for line in body.splitlines():
        if fence is None:
            m = _FENCE.match(line)
            if m:
                fence, buf = m.group(1), []
        elif line.strip() == fence:
            blocks.append("\n".join(buf))
            fence = None
        else:
            buf.append(line)
    return blocks


def block(name, heading):
    blocks = fenced_blocks(split_sections(read(name)).get(heading, ""))
    if not blocks:
        raise AssertionError("no fenced block under ## %s in %s" % (heading, name))
    return blocks[0]


def limits_restated(text, cycles):
    """Lines that state a cycle limit as a number: a line naming `cycles.<kind>` that also
    holds the limit's value, or the value a few words before approval, review, fix, round,
    iteration or attempt. A leading list marker ("3.") is not a number of the text."""
    hits = []
    for line in text.splitlines():
        body = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", line)
        for kind, n in cycles.items():
            near = (r"\b%d\b\W+(?:\w+\W+){0,3}?"
                    r"(?:approval|review|report.fix|fix|round|iteration|attempt)" % n)
            if ("cycles.%s" % kind in body and re.search(r"\b%d\b" % n, body)) \
                    or re.search(near, body, re.I):
                hits.append(line)
                break
    return hits


def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


class TestActs(unittest.TestCase):
    def setUp(self):
        self.acts = read("acts.md")
        self.sections = split_sections(self.acts)

    def test_handover_request_present(self):
        text = block("acts.md", "Handover request")
        self.assertIn(crew.HANDOVER_MARKER, text)
        self.assertEqual(crew.HANDOVER_MARKER, "HANDOVER READY")
        self.assertNotIn("\n", text)  # one line: it goes by send, not by file
        with mock.patch.object(crew, "deliver", return_value="accepted") as deliver:
            crew.request_handover("a1")
        self.assertEqual(deliver.call_args[0], ("a1", text))

    def test_emergency_pause_block(self):
        self.assertIn("Emergency pause", self.sections)
        first = self.acts.split("\n\n", 2)[1]
        self.assertIn("Emergency pause", first)
        own = block("acts.md", "Emergency pause")
        self.assertNotIn("\n", own)
        self.assertIn("make no tool call", own)
        self.assertIn("do not retry", own)
        self.assertNotIn("finish", own.lower())
        self.assertLess(self.acts.index("## Pause"), self.acts.index("## Emergency pause"))
        self.assertLess(self.acts.index("## Emergency pause"), self.acts.index("## Continue"))

    def test_end_background_shells_block(self):
        self.assertIn("End background shells", self.sections)
        own = block("acts.md", "End background shells")
        self.assertNotIn("\n", own.strip())
        self.assertIn("stop every background shell", own)
        self.assertIn("end your turn", own)
        self.assertNotIn("is closed", own)
        first = self.acts.split("\n\n", 2)[1]
        self.assertIn("End background shells", first)
        self.assertLess(self.acts.index("## Changed work order"),
                        self.acts.index("## End background shells"))
        self.assertLess(self.acts.index("## End background shells"),
                        self.acts.index("## Message by file"))
        section = self.acts[self.acts.index("## End background shells"):
                            self.acts.index("## Message by file")]
        self.assertIn("one monitor heartbeat", section)
        self.assertIn("incident", section)

    def test_acts_sections_for_crew(self):
        with open(os.path.join(SCRIPTS_DIR, "crew.py"), encoding="utf-8") as fh:
            source = fh.read()
        read_by_crew = set(re.findall(r'_template\("([^"]+)"\)', source))
        self.assertEqual(read_by_crew, {"Handover request", "Pause", "Emergency pause",
                                        "Continue", "Changed work order", "End background shells",
                                        "Message by file", "Fix the report"})
        for heading in read_by_crew:
            own = block("acts.md", heading)
            self.assertTrue(own.strip(), heading)
            # the script finds this section's own block, never a later section's
            self.assertEqual(crew._template(heading), own, heading)
        for heading in set(re.findall(r'_template\("([^"]+)"\)\.format\(path=', source)):
            self.assertIn("{path}", block("acts.md", heading), heading)
        self.assertEqual(block("acts.md", "Customer decision to drop the branch"),
                         crew.DROP_MARKER)

    def test_acceptance_act_template(self):
        prose = self.sections["Acceptance act"]
        act = block("acts.md", "Acceptance act")
        self.assertTrue(act.startswith("Verdict: "), act)  # the morning report shows line 1
        self.assertIn("accepted | done, review needs rework | failed", act)
        for word in ("Report check", "Heavy runs", "Review", "Did not match"):
            self.assertIn(word, act)
        self.assertIn("check_report", prose)
        self.assertIn("crew.py accept", prose)
        self.assertIn("Spend:", prose)
        self.assertNotIn("Spend:", act)  # the script appends it

    def test_handover_act_templates(self):
        act = block("acts.md", "Handover act")
        for field in ("Goal:", "Done:", "In progress:", "Next steps:", "Agreements:",
                      "Important paths:", "Open questions:"):
            self.assertIn(field, act)
        self.assertEqual(act.splitlines()[-1], crew.HANDOVER_MARKER)
        prose = self.sections["Handover act"]
        self.assertIn("complete_handover", prose)
        self.assertIn("Facts at handover", prose)
        self.assertIn("Spend:", prose)
        foreman = block("acts.md", "Foreman's handover act")
        for field in ("Work orders:", "Crews and panes:", "Spend against estimates:",
                      "Limits:", "Expected events:", "Night shift:"):
            self.assertIn(field, foreman)
        self.assertNotIn("Spend:", foreman)
        prose = self.sections["Foreman's handover act"]
        self.assertIn("shift.py handoff", prose)
        self.assertIn("Spend:", prose)

    def test_incident_tag(self):
        self.assertTrue(block("acts.md", "Incident").startswith(shift.INCIDENT_TAG))


class TestCustomerReferences(unittest.TestCase):
    def test_customer_md_sections(self):
        text = read("customer.md")
        sections = split_sections(text)
        for heading in ("Promised times", "Consent outcomes", "Panes", "Grey suggestions",
                        "Reports", "Delegation words", "Stuck or working", "Budget units"):
            self.assertIn(heading, sections)
        self.assertIn("journal.py due set", text)
        self.assertIn("journal.py consent done", text)

    def test_customer_md_login_section(self):
        sections = split_sections(read("customer.md"))
        self.assertIn("Logging in", sections)
        body = sections["Logging in"]
        self.assertIn("customer: login", body)
        self.assertIn("resume", body)
        self.assertIn("customer's word", body)

    def test_customer_approval_block(self):
        sections = split_sections(read("acts.md"))
        self.assertIn("Customer approval", sections)
        blocks = fenced_blocks(sections["Customer approval"])
        self.assertTrue(blocks)
        self.assertTrue(blocks[0].startswith("[customer decision] approve:"))
        self.assertFalse(blocks[0].startswith(crew.DROP_MARKER))

    def test_foreman_handover_act_names_timers(self):
        body = split_sections(read("acts.md"))["Foreman's handover act"]
        line = next(ln for ln in body.splitlines() if ln.startswith("Expected events:"))
        self.assertIn("journal.py due list", line)


class TestWorkOrder(unittest.TestCase):
    def setUp(self):
        self.text = read("work-order.md")
        self.template = block("work-order.md", "Template")
        self.example = block("work-order.md", "Example stage report")

    def test_work_order_template_fields(self):
        t = self.template
        for field in ("## Site", "## Goal and why it matters", "## Stages", "Stop point:",
                      "## Constraints", "prorab/<id>", "## Estimate", "Estimate: $<",
                      "at API prices", "Heavy-run limit per stage:", "## Clearances",
                      "## Site passport", "## Acceptance criteria", "## Report format",
                      "Files:", "Expected duration, min:",
                      "<absolute path>/prorab/scripts/slots.py run --kind heavy -- <command>",
                      "subagents", "plain text", "choice dialog"):
            self.assertIn(field, t)
        constraints = t.split("## Constraints")[1].split("\n## ")[0]
        # safety rules: no consent prompt is answered, "Upgrade" is never pressed (P7, G);
        # one crew and one session per work order (P16)
        self.assertRegex(constraints, r"Never answer a trust prompt or any other consent prompt")
        self.assertIn('never press "Upgrade"', constraints)
        self.assertIn("one crew and one session", constraints)
        self.assertIn("take no second work order", constraints)
        self.assertRegex(t, r"(?m)^- C1: ")
        # the foreman's work order body is where check_report finds the criteria: the
        # template itself contributes no criterion id outside the criteria list
        self.assertEqual(crew.criteria_ids(t), ["C1", "C2"])

    def test_work_order_forbids_logging_in(self):
        constraints = self.template.split("## Constraints")[1].split("\n## ")[0]
        line = next(ln for ln in constraints.splitlines() if "Never run `/login`" in ln)
        self.assertIn("`claude auth", line)
        self.assertIn("logging in is the customer's", line)

    def test_report_fix_loop(self):
        # the foreman's acceptance steps say what to do when the report check fails
        self.assertIn("crew.py report_fix NAME WORK_ORDER STAGE --problems-file FILE", self.text)
        line = next(ln for ln in self.text.splitlines() if "crew.py report_fix" in ln)
        self.assertIn("`cycles.report_fix` in `defaults.json`", line)
        self.assertIn("gate", line)
        self.assertEqual(limits_restated(self.text, _defaults.load()["cycles"]), [])
        self.assertIn("cycles.report_fix", read("superpowers.md"))

    def test_report_format_matches_check_report(self):
        t = self.template
        for sec in crew.REPORT_SECTIONS:
            self.assertRegex(t, r"(?m)^## %s$" % re.escape(sec))
        for word in crew.CRITERION_STATUSES + crew.OUTCOMES:
            self.assertIn(word, t)
        for line in ("C-id | status | reason", "Result: <", "Commit: <", "Heavy runs: <",
                     "SKIP: <n>"):
            self.assertIn(line, t)
        self.assertIn("crew.py check_report NAME WORK_ORDER --stage STAGE", self.text)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, dict(GIT_ENV, HOME=tmp)):
            repo = os.path.join(tmp, "demo-app")
            os.makedirs(repo)
            git(repo, "init", "-q", "-b", "main")
            with open(os.path.join(repo, "README.md"), "w") as fh:
                fh.write("demo\n")
            git(repo, "add", "README.md")
            git(repo, "commit", "-q", "-m", "base")
            git(repo, "checkout", "-q", "-b", "prorab/N1")
            listed = [crew._listed_path(it) for it in
                      crew._items(crew._sections(self.example)["Changed files"])]
            self.assertTrue(listed)
            for path in listed:
                os.makedirs(os.path.dirname(os.path.join(repo, path)), exist_ok=True)
                with open(os.path.join(repo, path), "w") as fh:
                    fh.write("x = 1\n")
                git(repo, "add", path)
            git(repo, "commit", "-q", "-m", "stage 1")
            sha = git(repo, "rev-parse", "--short=10", "HEAD").strip()
            report, n = re.subn(r"(?m)^(Commit: )\S+$", lambda m: m.group(1) + sha,
                                self.example)
            self.assertEqual(n, 1)
            heavy = int(re.search(r"(?m)^Heavy runs: (\d+)$", report).group(1))
            criteria = crew.criteria_ids(report)
            self.assertGreaterEqual(len(criteria), 2)
            result = crew.check_report(
                report, criteria, listed, repo, base="main",
                heavy_limit=_defaults.load()["machine"]["heavy_runs_per_stage"],
                heavy_counted=heavy)
        self.assertEqual(result, {"ok": True, "problems": [], "warnings": [],
                                  "outcome": "delivered", "sha": sha})


class TestSuperpowersAndPassport(unittest.TestCase):
    def test_limits_restated_matcher(self):
        cycles = {"approval": 6, "review": 10, "report_fix": 3}
        for line in ("at most 6 approval iterations", "10 review rounds per stage",
                     "the limit `cycles.report_fix` (3)", "after 3 failed attempts"):
            self.assertEqual(limits_restated(line, cycles), [line], line)
        for line in ("6. Step six of a list", "3. **Implementation.** Commit each task",
                     "the limit is `cycles.approval` in `defaults.json`", "stage 2: 40 heavy runs",
                     "C3 | deferred"):
            self.assertEqual(limits_restated(line, cycles), [], line)

    def test_superpowers_process(self):
        text = read("superpowers.md")
        for skill in ("brainstorming", "writing-plans", "executing-plans",
                      "subagent-driven-development", "test-driven-development",
                      "systematic-debugging", "verification-before-completion",
                      "using-git-worktrees", "requesting-code-review", "code-review",
                      "finishing-a-development-branch"):
            self.assertIn("`%s`" % skill, text)
        for phrase in ("no open questions", "no placeholders", "without inventing requirements",
                       "cycles.approval", "cycles.review",
                       "journal.py iterations STAGE approval --bump",
                       "journal.py iterations STAGE review --bump", "gate",
                       "estimate", "## Fallback procedure"):
            self.assertIn(phrase, text)
        rework = block("superpowers.md", "Approval")
        self.assertIn("Rework:", rework)
        # limits by reference only, never restated
        self.assertEqual(limits_restated(text, _defaults.load()["cycles"]), [])
        # an answer is recorded on the stage bead (spec, R56)
        self.assertIn("records it as an `answer` act on the stage bead", text)
        fallback = split_sections(text)["Fallback procedure"]
        steps = ("Plan", "Test", "Implementation", "Verification", "Review")
        positions = [fallback.find("**%s.**" % s) for s in steps]
        self.assertNotIn(-1, positions, positions)
        self.assertEqual(positions, sorted(positions))

    def test_passport_template_fields(self):
        text = read("passport.md")
        self.assertIn("journal.py passport SITE --set-file", text)
        self.assertIn("prorab:passport", text)
        self.assertIn("after acceptance", text)
        t = block("passport.md", "Template")
        for field in ("Worktree setup", "Quick checks", "Heavy commands",
                      "<absolute path>/prorab/scripts/slots.py run --kind heavy -- <command>",
                      "Known quirks", "Actions that need a clearance"):
            self.assertIn(field, t)
        self.assertEqual(crew.criteria_ids(t), [])  # appended to the work-order file


if __name__ == "__main__":
    unittest.main()
