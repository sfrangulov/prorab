"""Guard: identifiers, comments and docs use English terms, not transliterated Russian,
and hold no Cyrillic outside a short allowlist (R41).

The banned words are assembled from pieces, and Cyrillic is written as \\u escapes, so this
file does not trip its own checks.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCAN = ("prorab", "docs", "CLAUDE.md")

BANNED_STEMS = ("nar" + "yad", "dop" + "usk", "sme" + "ta", "bri" + "gad",
                "obe" + "kt", "pri" + "emk", "pere" + "smen", "zakaz" + "chik")
SHORT_ABBR = re.compile("(?<![A-Za-z])" + "o" + "bj" + "(?![A-Za-z])", re.IGNORECASE)


def transliterations(line):
    """Banned stems (anywhere in the line) and the whole-identifier abbreviation found in line."""
    low = line.lower()
    hits = [s for s in BANNED_STEMS if s in low]
    hits += [m.group(0) for m in SHORT_ABBR.finditer(line)]
    return hits


CYRILLIC = re.compile("[\u0400-\u04ff]")
# Files that deliberately hold Russian, each listed by name: the Russian choice-dialog test
# fixture and the trigger-eval file (its Russian should-/should-not-trigger queries, R43).
ALLOWED_FILES = ("prorab/tests/fixtures/choice_dialog_cyrillic.txt", "prorab/evals/triggers.json")
SKILL_FILE = "prorab/SKILL.md"
SPEC_FILE = "docs/superpowers/specs/2026-10-21-prorab-design.md"


def _skill_description_lines(lines):
    """Zero-based indexes of the frontmatter `description:` value lines of SKILL.md."""
    if not lines or lines[0].strip() != "---":
        return set()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines))
    out, inside = set(), False
    for i in range(1, end):
        line = lines[i]
        if line.startswith("description:"):
            inside = True
        elif inside and line[:1] not in (" ", "\t", "") and not line.startswith("#"):
            inside = False
        if inside:
            out.add(i)
    return out


def cyrillic_hits(rel_path, text):
    """`path:line: excerpt` for each line with Cyrillic, outside the allowlist (R41)."""
    rel_path = rel_path.replace(os.sep, "/")
    if rel_path in ALLOWED_FILES:
        return []
    lines = text.splitlines()
    skip = _skill_description_lines(lines) if rel_path == SKILL_FILE else set()
    in_glossary = False
    hits = []
    for i, line in enumerate(lines):
        if rel_path == SPEC_FILE and line.startswith("## "):
            in_glossary = line.strip() == "## Glossary"
        if not CYRILLIC.search(line) or i in skip:
            continue
        if in_glossary and line.lstrip().startswith("|"):
            cells = line.strip().strip("|").split("|")
            # the Russian column is the second cell: drop it, the rest must be clean
            if len(cells) > 1 and not CYRILLIC.search("|".join(cells[:1] + cells[2:])):
                continue
        hits.append("%s:%d: %s" % (rel_path, i + 1, line.strip()[:80]))
    return hits


def _files():
    for top in SCAN:
        path = os.path.join(ROOT, top)
        if os.path.isfile(path):
            yield path
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(filenames):
                if not name.endswith(".pyc"):
                    yield os.path.join(dirpath, name)


class TestLanguage(unittest.TestCase):
    def test_transliteration_matcher(self):
        stem = BANNED_STEMS[0]
        abbr = "o" + "bj"
        for line in ("send_" + stem, stem.upper(), abbr, abbr + "_path",
                     abbr.upper() + "_DIR", abbr + "1", "x = " + abbr + "(1)"):
            self.assertTrue(transliterations(line), line)
        for s in BANNED_STEMS:
            self.assertTrue(transliterations("a_" + s.capitalize() + "s"), s)
        for line in ("object", "objective", "mock.patch.object", "site_path", "work_order"):
            self.assertEqual(transliterations(line), [], line)

    def test_no_transliterations(self):
        hits = []
        for path in _files():
            try:
                with open(path, "rb") as f:
                    data = f.read()
                if b"\0" in data:
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = os.path.relpath(path, ROOT)
            for n, line in enumerate(text.splitlines(), 1):
                for word in transliterations(line):
                    hits.append("%s:%d: %s" % (rel, n, word))
        self.assertEqual(hits, [], "transliterated words found:\n" + "\n".join(hits))

    def test_cyrillic_allowlist_matcher(self):
        ru = "\u043f\u0440\u0438\u0432\u0435\u0442"
        glossary = "## Glossary\n\n| English | Russian | Code |\n|---|---|---|\n"
        spec = SPEC_FILE
        self.assertEqual(cyrillic_hits(spec, glossary + "| crew | %s | `crew` |\n" % ru), [])
        self.assertEqual(len(cyrillic_hits(spec, glossary + "| crew | x | %s |\n" % ru)), 1)
        self.assertEqual(len(cyrillic_hits(spec, glossary + "\n## Next\n\n%s\n" % ru)), 1)
        self.assertEqual(len(cyrillic_hits(spec, "| crew | %s | x |\n" % ru)), 1)
        self.assertEqual(len(cyrillic_hits("other.md", glossary + "| a | %s | b |\n" % ru)), 1)
        skill = ("---\nname: prorab\ndescription: Use when asked.\n  Triggers: %s\n"
                 "  more %s\nlicense: x\n---\nbody\n") % (ru, ru)
        self.assertEqual(cyrillic_hits(SKILL_FILE, skill), [])
        self.assertEqual(len(cyrillic_hits(SKILL_FILE, skill.replace("license: x", "license: " + ru))), 1)
        self.assertEqual(len(cyrillic_hits(SKILL_FILE, skill + ru + "\n")), 1)
        self.assertEqual(len(cyrillic_hits("prorab/other/SKILL.md", skill)), 2)
        for name in ALLOWED_FILES:
            self.assertEqual(cyrillic_hits(name, ru + "\n"), [], name)
        self.assertEqual(len(cyrillic_hits("prorab/scripts/x.py", "# %s\n" % ru)), 1)

    def test_no_cyrillic(self):
        hits = []
        for path in _files():
            try:
                with open(path, "rb") as f:
                    data = f.read()
                if b"\0" in data:
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits += cyrillic_hits(os.path.relpath(path, ROOT), text)
        self.assertEqual(hits, [], "Cyrillic outside the allowlist:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
