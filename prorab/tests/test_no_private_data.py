"""Guard: no customer data in prorab/ or docs/.

The private word list comes only from the environment variable
PRORAB_PRIVATE_WORDS (comma-separated); it is never stored in the repo.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCAN_DIRS = ("prorab", "docs")

# Patterns are assembled from pieces so this file does not trip its own checks.
_MAC_HOME = re.compile("/" + "Users" + r"/[^/\s]+")
_LINUX_HOME = re.compile("/" + "home" + r"/(?!user(?![\w.-]))[^/\s]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def iter_text_lines():
    """Yield (relative path, line number, line) for every text file."""
    for top in SCAN_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(filenames):
                if name.endswith(".pyc"):
                    continue
                path = os.path.join(dirpath, name)
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
                    yield rel, n, line


def find(matcher):
    hits = []
    for rel, n, line in iter_text_lines():
        for m in matcher(line):
            hits.append("%s:%d: %s" % (rel, n, m))
    return hits


class TestNoPrivateData(unittest.TestCase):
    def test_no_home_paths(self):
        hits = find(lambda l: [m.group(0) for r in (_MAC_HOME, _LINUX_HOME) for m in r.finditer(l)])
        self.assertEqual(hits, [], "home-directory paths found:\n" + "\n".join(hits))

    def test_no_emails(self):
        hits = find(lambda l: [m.group(0) for m in _EMAIL.finditer(l)])
        self.assertEqual(hits, [], "e-mail addresses found:\n" + "\n".join(hits))

    def test_no_private_words(self):
        words = [w.strip().lower() for w in os.environ.get("PRORAB_PRIVATE_WORDS", "").split(",")]
        words = [w for w in words if w]
        if not words:
            self.skipTest("PRORAB_PRIVATE_WORDS is empty or unset: private-word check skipped")

        def matcher(line):
            low = line.lower()
            return [w for w in words if w in low]

        hits = find(matcher)
        self.assertEqual(hits, [], "private words found:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
