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

    def test_the_trail_is_a_no_op_until_enabled(self):
        crash_log.trail("emit", "nothing is open yet")   # must not raise

    def test_the_trail_writes_each_event_straight_through(self):
        trail = os.path.join(self.dir.name, "crash_trail.log")
        self.addCleanup(self._close_trail)
        self.assertTrue(crash_log.enable_trail(trail))
        crash_log.trail("emit", "buddy_network state 1200B")
        crash_log.trail("action", "buddy_network send")
        # No flush: a native crash gives no chance to, so the lines must
        # already be on their way to disk.
        with open(trail, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn("--- started", lines[1])
        self.assertTrue(lines[-2].endswith("emit     buddy_network state 1200B"), lines[-2])
        self.assertTrue(lines[-1].endswith("action   buddy_network send"), lines[-1])

    def test_the_trail_rolls_over_when_big(self):
        trail = os.path.join(self.dir.name, "crash_trail.log")
        self.addCleanup(self._close_trail)
        crash_log.enable_trail(trail)
        big = "x" * 1000
        for _ in range(crash_log.TRAIL_BYTES // 1000 + 5):
            crash_log.trail("emit", big)
        self.assertTrue(os.path.exists(trail + ".1"))
        self.assertLess(os.path.getsize(trail), crash_log.TRAIL_BYTES)
        crash_log.trail("emit", "after the roll")
        with open(trail, encoding="utf-8") as f:
            self.assertIn("after the roll", f.read())

    def _close_trail(self):
        if crash_log._trail:
            crash_log._trail.close()
        crash_log._trail = None

    def test_an_unwritable_place_is_not_fatal(self):
        blocker = os.path.join(self.dir.name, "file")
        with open(blocker, "w"):
            pass
        self.assertFalse(crash_log.enable(os.path.join(blocker, "crash.log")))


if __name__ == "__main__":
    unittest.main()
