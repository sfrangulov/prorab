"""Journal CLI checks that never reach the real `bd`: argument errors exit before any bd call."""
import contextlib
import io
import json
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

    def test_research_without_pane_needs_session_before_any_bd_call(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERDR_PANE_ID", None)
            with contextlib.redirect_stdout(out):
                rc = journal.main(["research", "k", "--estimate-usd", "5"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue()),
                         {"error": "foreman session unknown: pass --session"})

    def test_notify_kinds_are_the_four_urgent_ones(self):
        self.assertEqual(journal.URGENT_KINDS,
                         ("limit", "all_waiting", "foreman_failure", "api_key"))


class TestLockAndMainsErrors(unittest.TestCase):
    """Fix wave 1, 15.10: the lock's OSError and the mains' JSON errors."""

    def test_an_unusable_lock_file_is_a_journal_error_naming_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "blocker")
            with open(blocker, "w"):
                pass
            with mock.patch.dict(os.environ, {"PRORAB_CACHE": os.path.join(blocker, "sub")}):
                with self.assertRaises(journal.JournalError) as cm:
                    with journal._locked():
                        pass
            self.assertIn("journal.lock", str(cm.exception))
            self.assertEqual(journal._HELD[0], 0)

    def test_the_journal_main_prints_a_json_error_for_an_oserror(self):
        out = io.StringIO()
        with mock.patch.object(journal, "ensure_db"), \
                mock.patch.object(journal, "kv_get", side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(out):
            rc = journal.main(["kv", "get", "k"])
        self.assertEqual(rc, 1)
        self.assertIn("disk full", json.loads(out.getvalue())["error"])

    def test_the_shift_main_prints_a_json_error_for_a_journal_error(self):
        import shift
        out = io.StringIO()
        with mock.patch.object(shift.journal, "ensure_db",
                               side_effect=journal.JournalError("lock gone")), \
                contextlib.redirect_stdout(out):
            rc = shift.main(["reconcile"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue()), {"error": "lock gone"})


if __name__ == "__main__":
    unittest.main()
