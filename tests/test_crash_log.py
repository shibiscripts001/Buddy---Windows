"""core/crash_log.py: the log Buddy writes so a native crash inside Resolve's
script host leaves a trace. Plain Python, a scratch folder."""

import os
import sys
import tempfile
import unittest

import _paths  # noqa: F401
from core import crash_log


class CrashLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "sub", "crash.log")
        hook = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", hook)
        self.addCleanup(self._close)

    def _close(self):
        import faulthandler
        faulthandler.disable()
        if crash_log._handle:
            crash_log._handle.close()
            crash_log._handle = None

    def read(self):
        crash_log._handle.flush()
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_marks_each_start_and_keeps_old_entries(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("an earlier crash\n")
        self.assertTrue(crash_log.enable(self.path, version="test"))
        text = self.read()
        self.assertIn("an earlier crash", text)
        self.assertIn("--- Buddy test started", text)

    def test_uncaught_errors_are_written_and_still_reported(self):
        seen = []
        sys.excepthook = lambda *a: seen.append(a[0])
        crash_log.enable(self.path)
        try:
            raise KeyError("boom")
        except KeyError:
            sys.excepthook(*sys.exc_info())
        self.assertEqual(seen, [KeyError])
        self.assertIn("KeyError: 'boom'", self.read())

    def test_a_big_log_is_trimmed_to_its_newest_half(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("old line\n" * (crash_log.MAX_BYTES // 9 + 1000))
            f.write("newest line\n")
        crash_log.enable(self.path)
        self.assertLess(os.path.getsize(self.path), crash_log.MAX_BYTES)
        text = self.read()
        self.assertTrue(text.startswith("[older entries trimmed]\n"))
        self.assertIn("newest line", text)

    def test_an_unwritable_place_is_not_fatal(self):
        blocker = os.path.join(self.dir.name, "file")
        with open(blocker, "w"):
            pass
        self.assertFalse(crash_log.enable(os.path.join(blocker, "crash.log")))


if __name__ == "__main__":
    unittest.main()
