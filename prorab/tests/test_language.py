"""Guard: identifiers, comments and docs use English terms, not transliterated Russian.

The banned words are assembled from pieces so this file does not trip its own check.
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


if __name__ == "__main__":
    unittest.main()
