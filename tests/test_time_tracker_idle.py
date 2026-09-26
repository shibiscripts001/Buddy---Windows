"""Time Tracker's idle-pause race, downtime recovery and heartbeat
(app/pages/time_tracker/engine.py - no Qt), and the shell's once-only quit
flush (app/core/shell_window.py, needs PySide6). The engine runs against a
DataManager in a temp folder, never the real ~/.davinci_time_tracker."""

import contextlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

import _paths  # noqa: F401

from pages.time_tracker.data_manager import DataManager
from pages.time_tracker.engine import TrackerEngine

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pages.time_tracker.resolve_bridge import _extract_name
    from core.shell_window import ShellWindow
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def _iso(dt):
    return dt.isoformat(timespec="seconds")


class TimeTrackerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dm = DataManager(base_dir=self._tmp.name)
        self.engine = TrackerEngine(self.dm)
        self.engine.sync_manual_mode()   # auto mode, as on a fresh install

    def tearDown(self):
        self._tmp.cleanup()

    def _open_entry(self, start, **extra):
        entry = self.dm.start_entry("Proj")
        entry["start"] = _iso(start)
        entry.update(extra)
        self.dm.save_entries()
        return entry

    # ---- idle race ----

    def test_poll_result_during_idle_pause_starts_nothing(self):
        self.engine._idle_paused = True
        self.engine.on_project_detected("Proj", True)
        self.assertIsNone(self.dm.get_open_entry())
        self.assertEqual(self.engine.current_state, "offline")
        self.assertEqual(self.engine.detected_project_name, "Proj")

    def test_poll_result_when_not_idle_starts_tracking(self):
        self.engine.on_project_detected("Proj", True)
        self.assertIsNotNone(self.dm.get_open_entry())
        self.assertEqual(self.engine.current_state, "tracking")

    # ---- downtime at startup ----

    def test_stale_entry_ends_at_last_seen(self):
        now = datetime.now()
        entry = self._open_entry(now - timedelta(hours=10), last_seen=_iso(now - timedelta(hours=9)))
        self.engine.close_stale_open_entry()
        self.assertIsNone(self.dm.get_open_entry())
        self.assertEqual(entry["end"], _iso(now - timedelta(hours=9)))
        self.assertAlmostEqual(self.dm.duration_seconds(entry), 3600, delta=2)
        # And it's what's on disk, too.
        reloaded = DataManager(base_dir=self._tmp.name).entries
        self.assertEqual(reloaded[0]["end"], entry["end"])

    def test_recent_entry_is_left_for_normal_startup(self):
        now = datetime.now()
        self._open_entry(now - timedelta(hours=1), last_seen=_iso(now - timedelta(seconds=30)))
        self.engine.close_stale_open_entry()
        self.assertIsNotNone(self.dm.get_open_entry())

    def test_stale_entry_without_last_seen_uses_pause_start(self):
        now = datetime.now()
        entry = self._open_entry(
            now - timedelta(hours=10), pause_started_at=_iso(now - timedelta(hours=8)),
        )
        entry.pop("last_seen", None)
        self.engine.close_stale_open_entry()
        self.assertEqual(entry["end"], _iso(now - timedelta(hours=8)))
        self.assertIsNone(entry["pause_started_at"])
        self.assertAlmostEqual(self.dm.duration_seconds(entry), 2 * 3600, delta=2)

    def test_stale_paused_entry_excludes_pause_before_last_seen(self):
        now = datetime.now()
        entry = self._open_entry(
            now - timedelta(hours=10),
            pause_started_at=_iso(now - timedelta(hours=9)),
            last_seen=_iso(now - timedelta(hours=8)),
        )
        self.engine.close_stale_open_entry()
        self.assertEqual(entry["end"], _iso(now - timedelta(hours=8)))
        self.assertAlmostEqual(self.dm.duration_seconds(entry), 3600, delta=2)

    # ---- heartbeat ----

    def test_heartbeat_is_written_then_rate_limited(self):
        now = datetime.now()
        entry = self._open_entry(now - timedelta(minutes=5))
        self.engine.touch_heartbeat()
        first = entry.get("last_seen")
        self.assertIsNotNone(first)
        self.assertEqual(DataManager(base_dir=self._tmp.name).entries[0]["last_seen"], first)

        entry["last_seen"] = _iso(now - timedelta(seconds=10))
        self.engine.touch_heartbeat()
        self.assertEqual(entry["last_seen"], _iso(now - timedelta(seconds=10)))

    def test_heartbeat_does_nothing_without_open_entry(self):
        self.engine.touch_heartbeat()
        self.assertEqual(self.dm.entries, [])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ShellQuitFlushTests(unittest.TestCase):
    def test_pages_are_told_once_even_if_one_raises(self):
        calls = []

        def boom():
            calls.append("boom")
            raise RuntimeError("page failed")

        shell = SimpleNamespace(
            _pages_told_quitting=False,
            pages={
                "a": SimpleNamespace(on_app_quitting=boom),
                "b": SimpleNamespace(on_app_quitting=lambda: calls.append("b")),
            },
        )
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stderr(devnull):
                ShellWindow._notify_pages_quitting(shell)   # e.g. commitDataRequest
                ShellWindow._notify_pages_quitting(shell)   # then aboutToQuit
        self.assertEqual(calls, ["boom", "b"])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ResolveBridgeBannerTests(unittest.TestCase):
    """resolve_poll_worker.py's subprocess is spawned fresh on every poll, so
    fusionscript.dll's own startup banner ("DaVinci Resolve Script
    Interpreter" / "Copyright (C) 2005 - 2026 Blackmagic Design ...") prints
    every time - ordinarily before the worker's own output, but it's been
    seen landing after it instead while Resolve is mid-shutdown, which used
    to be mistaken for the project name itself."""

    def test_the_banner_is_never_read_as_the_project_name(self):
        banner = "DaVinci Resolve Script Interpreter (Python 3.11)\n" \
                 "Copyright (C) 2005 - 2026 Blackmagic Design Pty. Ltd. All Rights Reserved.\n"
        self.assertEqual(_extract_name(banner + "My Project"), "My Project")
        self.assertEqual(_extract_name("My Project\n" + banner), "My Project")   # the banner printed late
        self.assertIsNone(_extract_name(banner))                                 # nothing but the banner
        self.assertIsNone(_extract_name(""))
        self.assertEqual(_extract_name("My Project"), "My Project")


if __name__ == "__main__":
    unittest.main()
