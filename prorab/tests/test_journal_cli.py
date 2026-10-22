"""Journal CLI checks that never reach the real `bd`: argument errors exit before any bd call."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import journal  # noqa: E402


class TestJournalCliArguments(unittest.TestCase):
    def setUp(self):
        def no_bd(*args, **kwargs):
            raise AssertionError("bd must not be reached: %r" % (args,))

        for name in ("_run", "ensure_db"):
            p = mock.patch.object(journal, name, no_bd)
            p.start()
            self.addCleanup(p.stop)

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                journal.main(list(argv))
        return cm.exception.code

    def test_cli_notify_requires_urgent_kind(self):
        self.assertEqual(self.main("notify", "gate-9"), 2)
        self.assertEqual(self.main("notify", "gate-9", "--kind", "other"), 2)
        self.assertEqual(self.main("notify", "gate-9", "--kind", "routine"), 2)

    def test_notify_kinds_are_the_four_urgent_ones(self):
        self.assertEqual(journal.URGENT_KINDS,
                         ("limit", "all_waiting", "foreman_failure", "api_key"))


if __name__ == "__main__":
    unittest.main()
