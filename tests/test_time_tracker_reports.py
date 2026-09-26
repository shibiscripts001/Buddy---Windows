"""Time Tracker without Qt: the tracking engine's controls (pause, stop and
undo, manual mode, idle trimming), Reports' figures and the History lists.
Every test uses a DataManager in a temp folder - never the real
~/.davinci_time_tracker."""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

import _paths  # noqa: F401
from pages.time_tracker import history, reports
from pages.time_tracker.data_manager import DataManager
from pages.time_tracker.engine import TrackerEngine


def iso(dt):
    return dt.isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dm = DataManager(base_dir=self._tmp.name)

    def add(self, project, start, hours, notes="", hidden=False):
        self.dm.add_manual_entry(project, iso(start), iso(start + timedelta(hours=hours)), notes)
        if hidden:
            self.dm.set_entry_hidden(self.dm.entries[-1]["id"], True)
        return self.dm.entries[-1]


class EngineTests(Base):
    def setUp(self):
        super().setUp()
        self.changes = 0
        self.polls = 0
        self.engine = TrackerEngine(self.dm, on_change=self._changed, request_poll=self._poll)
        self.engine.sync_manual_mode()

    def _changed(self):
        self.changes += 1

    def _poll(self):
        self.polls += 1

    def backdate_open(self, minutes):
        entry = self.dm.get_open_entry()
        entry["start"] = iso(datetime.now() - timedelta(minutes=minutes))
        return entry

    def test_follows_the_project_open_in_resolve(self):
        self.engine.on_project_detected("A", True)
        self.assertEqual((self.engine.current_state, self.dm.get_open_entry()["project"]), ("tracking", "A"))
        self.backdate_open(10)
        self.engine.on_project_detected("B", True)                   # switched project
        self.assertEqual([e["project"] for e in self.dm.entries], ["A", "B"])
        self.backdate_open(10)
        self.engine.on_project_detected(None, True)                  # Resolve closed
        self.assertIsNone(self.dm.get_open_entry())
        self.assertEqual(self.engine.current_state, "offline")
        self.engine.on_project_detected("C", False)                  # a failed poll changes nothing
        self.assertIsNone(self.dm.get_open_entry())

    def test_pause_freezes_in_place_and_resume_continues(self):
        self.engine.on_project_detected("A", True)
        entry = self.backdate_open(30)
        self.engine.toggle_pause(True)
        self.assertEqual(self.engine.current_state, "paused")
        self.assertIsNotNone(entry["pause_started_at"])
        self.engine.on_project_detected("A", True)                   # polling doesn't un-pause
        self.assertEqual(self.engine.current_state, "paused")
        self.engine.toggle_pause(False)
        self.assertEqual((self.engine.current_state, len(self.dm.entries)), ("tracking", 1))
        self.assertGreater(self.polls, 0)

    def test_stop_saves_and_undo_reopens(self):
        self.engine.on_project_detected("A", True)
        self.backdate_open(20)
        entry_id, project = self.engine.stop_tracking()
        self.assertEqual(project, "A")
        self.assertIsNone(self.dm.get_open_entry())
        self.engine.on_project_detected("A", True)                   # tracking picked up again...
        self.assertEqual(len(self.dm.entries), 2)
        self.assertTrue(self.engine.undo_stop(entry_id))             # ...undo drops that one, reopens the first
        self.assertEqual([e["id"] for e in self.dm.entries], [entry_id])
        self.assertIsNone(self.dm.entries[0]["end"])
        self.assertFalse(self.engine.undo_stop("missing"))

    def test_a_blip_leaves_nothing_to_undo(self):
        self.engine.on_project_detected("A", True)
        self.assertIsNone(self.engine.stop_tracking())               # under a second: dropped

    def test_manual_mode(self):
        self.dm.settings["manual_tracking"] = True
        self.engine.sync_manual_mode()
        self.engine.on_project_detected("A", True)
        self.assertEqual((self.engine.current_state, self.dm.get_open_entry()), ("stopped", None))
        self.engine.manual_toggle(True)
        self.assertEqual(self.dm.get_open_entry()["project"], "A")
        self.backdate_open(5)
        self.engine.on_project_detected("B", True)                   # attributed to what's open now
        self.assertEqual(self.dm.get_open_entry()["project"], "B")
        self.backdate_open(5)
        self.assertIsNotNone(self.engine.manual_toggle(False))
        self.assertEqual(self.engine.current_state, "stopped")

    def test_idle_trims_the_entry_back_to_when_activity_stopped(self):
        self.dm.settings["idle_threshold_minutes"] = 5
        self.engine.on_project_detected("A", True)
        entry = self.backdate_open(60)
        self.engine.check_idle(10 * 60)                              # away for 10 minutes
        self.assertTrue(self.engine.idle_paused)
        self.assertAlmostEqual(self.dm.duration_seconds(entry), 50 * 60, delta=5)
        self.assertFalse(self.engine.wants_poll())
        self.engine.check_idle(1)                                    # back
        self.assertFalse(self.engine.idle_paused)
        self.assertTrue(self.engine.wants_poll())

    def test_hotkey_cycles_pause(self):
        self.engine.on_project_detected("A", True)
        self.engine.handle_global_hotkey()
        self.assertEqual(self.engine.current_state, "paused")
        self.engine.handle_global_hotkey()
        self.assertEqual(self.engine.current_state, "tracking")


class ReportTests(Base):
    def test_totals_split_across_midnight_and_skip_hidden(self):
        today = date.today()
        midnight = datetime.combine(today, datetime.min.time())
        self.add("A", midnight - timedelta(hours=1), 3)              # 23:00 yesterday -> 02:00 today
        self.add("B", midnight + timedelta(hours=8), 1, hidden=True)
        report = reports.build(self.dm, today=today)
        self.assertAlmostEqual(report["today_seconds"], 2 * 3600, delta=1)
        self.assertEqual([p for p, *_ in report["by_project"]], ["A"])
        self.assertEqual(report["daily"][0], ("Today", report["daily"][0][1]))
        self.assertAlmostEqual(report["daily"][1][1], 3600, delta=1)

    def test_earnings_stay_per_currency(self):
        start = datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=9)
        self.add("A", start, 2)
        self.add("B", start + timedelta(hours=3), 1)
        self.dm.set_project_rate("A", 50, "USD")
        self.dm.set_project_rate("B", 40, "EUR")
        report = reports.build(self.dm)
        self.assertEqual(report["today_earnings_by_currency"], {"USD": 100.0, "EUR": 40.0})
        view = reports.view(self.dm, report)
        self.assertEqual(view["tiles"][0]["money"], ["€40.00 EUR earned", "$100.00 USD earned"])
        self.assertEqual(view["projects"][0]["money"], "$100.00")

    def test_custom_range_is_checked_and_joins_the_export(self):
        self.assertIsNotNone(reports.custom_range(self.dm, "2026-02-01", "2026-01-01")[1])
        self.assertIsNotNone(reports.custom_range(self.dm, "", "2026-01-01")[1])
        self.add("A", datetime(2026, 1, 5, 9), 4)
        custom, error = reports.custom_range(self.dm, "2026-01-01", "2026-01-31")
        self.assertIsNone(error)
        self.assertAlmostEqual(custom["custom_seconds"], 4 * 3600, delta=1)
        self.assertIn("custom_seconds", reports.build(self.dm, custom=custom))

    def test_goals_only_when_set(self):
        self.assertEqual(reports.goals(self.dm, reports.build(self.dm)), [])
        self.dm.settings["weekly_goal_hours"] = 10
        self.add("A", datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=1), 5)
        (goal,) = reports.goals(self.dm, reports.build(self.dm))
        self.assertEqual((goal["label"], goal["fraction"]), ("This week", 0.5))


class HistoryTests(Base):
    def test_scopes(self):
        start = datetime(2026, 3, 2, 9)
        self.add("A", start, 1)
        self.add("B", start + timedelta(days=1), 2)
        self.add("A", start + timedelta(days=2), 1, hidden=True)
        self.assertEqual(len(history.entries_for(self.dm, history.SCOPE_ALL, None)), 3)
        self.assertEqual(history.entries_for(self.dm, history.SCOPE_CURRENT, None), [])
        self.assertEqual(len(history.entries_for(self.dm, history.SCOPE_CURRENT, "A")), 2)
        self.assertEqual(len(history.entries_for(self.dm, "B", "A")), 1)
        view = history.view(self.dm, history.SCOPE_CURRENT, "A")
        self.assertEqual(view["summary"], '1h tracked on "A"')   # the hidden hour doesn't count
        self.assertEqual(history.view(self.dm, history.SCOPE_CURRENT, None)["summary"], "No project detected in Resolve")
        row = view["rows"][0]
        self.assertEqual((row["start"], row["end"], row["duration"]), ("09:00", "10:00", "1h"))

    def test_entry_checks(self):
        ok, problem = history.clean_entry({"project": " X ", "start": "2026-01-01T09:00",
                                           "end": "2026-01-01T10:30:15", "notes": " cut "})
        self.assertIsNone(problem)
        self.assertEqual(ok, {"project": "X", "start": "2026-01-01T09:00:00",
                              "end": "2026-01-01T10:30:15", "notes": "cut"})
        for data, field in (({"project": "", "start": "2026-01-01T09:00", "end": "2026-01-01T10:00"}, "project"),
                            ({"project": "X", "start": "", "end": "2026-01-01T10:00"}, "start"),
                            ({"project": "X", "start": "2026-01-01T10:00", "end": "2026-01-01T09:00"}, "end")):
            self.assertEqual(history.clean_entry(data)[1][0], field)


class PdfExportTests(Base):
    """The PDF exporters render through Qt's printer, so these need PySide6."""

    def setUp(self):
        super().setUp()
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtWidgets import QApplication
        except ImportError:  # pragma: no cover
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])

    def test_invoice_pdf(self):
        from pages.time_tracker.export_utils import export_invoice_pdf
        self.add("Client <A>", datetime(2026, 1, 5, 9), 2)
        self.dm.set_project_rate("Client <A>", 50, "USD")
        path = os.path.join(self._tmp.name, "invoice.pdf")
        export_invoice_pdf(reports.visible_entries(self.dm), self.dm, "Client <A>", "2026-01-01", "2026-01-31",
                           path, bill_to="ACME & Co", invoice_number="7")
        with open(path, "rb") as f:
            self.assertEqual(f.read(4), b"%PDF")


if __name__ == "__main__":
    unittest.main()
