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

    def test_acts_sections_for_crew(self):
        with open(os.path.join(SCRIPTS_DIR, "crew.py"), encoding="utf-8") as fh:
            source = fh.read()
        read_by_crew = set(re.findall(r'_template\("([^"]+)"\)', source))
        self.assertEqual(read_by_crew, {"Handover request", "Pause", "Continue",
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
        self.assertRegex(t, r"(?m)^- C1: ")
        # the foreman's work order body is where check_report finds the criteria: the
        # template itself contributes no criterion id outside the criteria list
        self.assertEqual(crew.criteria_ids(t), ["C1", "C2"])

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
        cycles = _defaults.load()["cycles"]
        for kind in ("approval", "review"):  # limits by reference only, never restated
            self.assertNotRegex(text, r"\b%d\b" % cycles[kind])
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
