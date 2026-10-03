"""Essentials: what the page keeps between runs (the open tab, each field,
the calculator's history, the notes) and the countdown it runs for the
view. The maths itself is the view's (essentials.js)."""

import os
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.essentials import page as page_mod

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class CleaningTests(unittest.TestCase):
    def test_a_pref_is_one_plain_value(self):
        self.assertEqual(page_mod.clean_pref("1920"), "1920")
        self.assertEqual(page_mod.clean_pref(True), True)
        self.assertEqual(page_mod.clean_pref(1.5), 1.5)
        self.assertEqual(len(page_mod.clean_pref("x" * 5000)), page_mod.MAX_PREF_TEXT)
        for bad in ({"a": 1}, [1], float("nan"), 1e20):
            self.assertIsNone(page_mod.clean_pref(bad), bad)

    def test_history_keeps_only_well_formed_rows(self):
        rows = [{"expr": "1+1", "result": "2"}, {"expr": 3}, "junk", {"expr": "10:00*2", "result": "00:00:20:00", "mode": "tc"}]
        self.assertEqual(page_mod.clean_history(rows), [
            {"expr": "1+1", "result": "2", "mode": "std"},
            {"expr": "10:00*2", "result": "00:00:20:00", "mode": "tc"},
        ])
        self.assertEqual(len(page_mod.clean_history([{"expr": "1", "result": "1"}] * 50)), page_mod.MAX_HISTORY)
        self.assertEqual(page_mod.clean_history(None), [])


class Mem(dict):
    def save(self):
        self.saves = getattr(self, "saves", 0) + 1


class Host:
    def __init__(self, saved=None):
        self.tools, self.notes = {}, []
        if saved is not None:
            self.tools["essentials"] = Mem(saved)

    def tool_settings(self, tool_id, defaults=None):
        return self.tools.setdefault(tool_id, Mem(defaults or {}))

    def notify(self, title, message):
        self.notes.append((title, message))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtCore import QCoreApplication, Qt
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])

    def make(self, saved=None):
        self.host = Host(saved)
        page = page_mod.EssentialsPage(self.host)
        self.addCleanup(page.deleteLater)
        self.events = []
        page.emit = lambda name, payload=None: self.events.append((name, payload))
        return page

    def last(self, name):
        found = [p for n, p in self.events if n == name]
        return found[-1] if found else None

    def test_it_opens_as_it_was_left(self):
        page = self.make({"tab": "notes", "prefs": {"dr_codec": "prores_hq", "sw_run": True}, "notes": "call the client",
                          "history": [{"expr": "2*3", "result": "6"}]})
        page.web_ready()
        state = self.last("state")
        self.assertEqual(state["tab"], "notes")
        self.assertEqual(state["prefs"], {"dr_codec": "prores_hq", "sw_run": True})
        self.assertNotIn("notes", state)   # notes come in notes_state
        self.assertEqual(self.last("notes_state"), {"scope": "", "text": "call the client", "projects": [], "open": "",
                                                    "follow": True})
        self.assertEqual(state["history"], [{"expr": "2*3", "result": "6", "mode": "std"}])
        self.assertEqual(self.last("countdown"), {"ends_at": None, "left_ms": 0, "total_ms": 0})

    def test_an_unknown_tab_opens_the_calculator(self):
        page = self.make({"tab": "nope"})
        page.web_ready()
        self.assertEqual(self.last("state")["tab"], "calc")

    def test_what_the_view_sends_is_saved(self):
        page = self.make()
        settings = self.host.tools["essentials"]
        page.on_tab({"tab": "timer"})
        page.on_tab({"tab": "elsewhere"})
        page.on_pref({"key": "ar_w", "value": "3840"})
        page.on_pref({"key": "", "value": "x"})
        page.on_pref({"key": "bad", "value": {"no": 1}})
        page.on_notes({"scope": "", "text": "line one\nline two"})
        page.on_history({"items": [{"expr": "1+2", "result": "3"}]})
        self.assertEqual(settings["tab"], "timer")
        self.assertEqual(settings["prefs"], {"ar_w": "3840"})   # an unusable value is nothing
        self.assertEqual(settings["notes"], "line one\nline two")
        self.assertEqual(settings["history"], [{"expr": "1+2", "result": "3", "mode": "std"}])

    def test_prefs_are_capped(self):
        page = self.make()
        for i in range(page_mod.MAX_PREFS + 10):
            page.on_pref({"key": f"k{i}", "value": i})
        self.assertEqual(len(page.prefs), page_mod.MAX_PREFS)

    def test_the_world_clock_starts_with_a_few_cities(self):
        page = self.make()
        page.web_ready()
        self.assertEqual([c["zone"] for c in self.last("state")["clocks"]],
                         ["America/Los_Angeles", "America/New_York", "Europe/London"])

    def test_the_clock_list_is_kept_even_when_emptied(self):
        page = self.make()
        page.on_clocks({"items": [{"zone": "Asia/Kolkata", "label": "  Studio   B "}, {"zone": "Asia/Kolkata"},
                                  {"zone": "../../etc/passwd"}, {"zone": "Etc/GMT+5", "label": 7}]})
        self.assertEqual(self.host.tools["essentials"]["clocks"],
                         [{"zone": "Asia/Kolkata", "label": "Studio B"}, {"zone": "Etc/GMT+5", "label": ""}])
        page.on_clocks({"items": []})
        page = self.make({"clocks": []})
        page.web_ready()
        self.assertEqual(self.last("state")["clocks"], [])

    def test_clock_lists_are_capped(self):
        zones = [{"zone": f"Etc/GMT+{i % 12}x{i}"} for i in range(40)]
        self.assertEqual(len(page_mod.clean_clocks(zones)), page_mod.MAX_CLOCKS)
        self.assertEqual(page_mod.clean_clocks("nope"), [])

    def test_copy_goes_to_the_clipboard(self):
        page = self.make()
        page.on_copy({"text": "01:00:10:00"})
        self.assertEqual(QApplication.clipboard().text(), "01:00:10:00")

    def test_countdown_start_pause_resume_reset(self):
        page = self.make()
        page.on_countdown({"action": "start", "ms": 60_000})
        c = self.last("countdown")
        self.assertEqual(c["total_ms"], 60_000)
        self.assertAlmostEqual(c["ends_at"] / 1000, time.time() + 60, delta=1)
        self.assertTrue(page._alarm.isActive())

        page.on_countdown({"action": "pause"})
        c = self.last("countdown")
        self.assertIsNone(c["ends_at"])
        self.assertGreater(c["left_ms"], 59_000)
        self.assertFalse(page._alarm.isActive())

        page.on_countdown({"action": "start", "ms": 5})    # resumes what was left, not a new 5 ms
        self.assertEqual(self.last("countdown")["total_ms"], 60_000)

        page.on_countdown({"action": "reset"})
        self.assertEqual(self.last("countdown"), {"ends_at": None, "left_ms": 0, "total_ms": 0})
        self.assertFalse(page._alarm.isActive())

    def test_countdown_refuses_nonsense(self):
        page = self.make()
        for ms in (0, -5, "60", True, page_mod.MAX_COUNTDOWN_MS + 1, None):
            page.on_countdown({"action": "start", "ms": ms})
        self.assertIsNone(self.last("countdown"))
        self.assertFalse(page._alarm.isActive())

    def test_countdown_goes_off_and_says_so_when_buddy_is_not_in_front(self):
        page = self.make()
        page.on_countdown({"action": "start", "ms": 30})
        end = time.monotonic() + 3
        while self.last("countdown_done") is None and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(self.last("countdown_done"), {"total_ms": 30})
        self.assertEqual(self.last("countdown"), {"ends_at": None, "left_ms": 0, "total_ms": 0})
        self.assertEqual(self.host.notes, [("Timer finished", "Your Essentials countdown is done.")])

    def test_an_early_wakeup_waits_for_the_deadline(self):
        page = self.make()
        page.on_countdown({"action": "start", "ms": 60_000})
        with mock.patch.object(page, "_arm") as arm:
            page._countdown_check()   # the timer fired early (or a long one re-arming)
        arm.assert_called_once()
        self.assertIsNone(self.last("countdown_done"))


class FakeBridge:
    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _S(QObject):
            project_detected = Signal(object, bool)
        self._s = _S()
        self.project_detected = self._s.project_detected


class FakeTracker:
    def __init__(self, open_project=None):
        self.bridge = FakeBridge()
        self.engine = type("Engine", (), {"detected_project_name": open_project})()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ProjectNotesTests(unittest.TestCase):
    setUp = PageTests.setUp
    last = PageTests.last

    def make(self, saved=None, open_project=None, tracker=True):
        page = PageTests.make(self, saved)
        self.host.pages = {"time_tracker": FakeTracker(open_project)} if tracker else {}
        page._hook_project()
        return page

    def bridge(self):
        return self.host.pages["time_tracker"].bridge

    def test_general_notes_are_the_old_scratchpad(self):
        page = self.make({"notes": "from before"})
        page.web_ready()
        self.assertEqual(self.last("notes_state")["scope"], "")
        self.assertEqual(self.last("notes_state")["text"], "from before")

    def test_the_open_project_is_listed_and_followed(self):
        self.make({"project_notes": {"Ad spot": "logo at end", "Doc": "interview B-roll"}}, open_project="Wedding")
        n = self.last("notes_state")
        self.assertEqual(n["scope"], "Wedding")
        self.assertEqual(n["projects"], ["Ad spot", "Doc", "Wedding"])
        self.assertEqual(n["open"], "Wedding")
        self.assertEqual(n["text"], "")

        self.bridge().project_detected.emit("Doc", True)
        self.assertEqual(self.last("notes_state")["scope"], "Doc")
        self.assertEqual(self.last("notes_state")["text"], "interview B-roll")
        self.assertEqual(self.host.tools["essentials"]["notes_scope"], "Doc")

    def test_not_following_keeps_what_you_picked(self):
        page = self.make({"notes_follow": False}, open_project="Wedding")
        self.assertEqual(self.last("notes_state")["scope"], "")
        self.bridge().project_detected.emit("Doc", True)
        n = self.last("notes_state")
        self.assertEqual((n["scope"], n["open"], n["projects"]), ("", "Doc", ["Doc"]))
        page.on_notes_follow({"on": True})
        self.assertEqual(self.last("notes_state")["scope"], "Doc")

    def test_a_failed_poll_or_the_same_project_changes_nothing(self):
        self.make(open_project="Wedding")
        before = len(self.events)
        self.bridge().project_detected.emit(None, False)
        self.bridge().project_detected.emit("Wedding", True)
        self.assertEqual(len(self.events), before)

    def test_text_goes_to_the_notes_it_was_typed_in(self):
        page = self.make({"notes": "general"})
        page.on_notes_scope({"scope": "Ad spot"})
        page.on_notes({"scope": "Ad spot", "text": "logo at end"})
        page.on_notes_scope({"scope": ""})
        page.on_notes({"scope": "Ad spot", "text": "logo at end, then card"})   # a save that lands after the switch
        settings = self.host.tools["essentials"]
        self.assertEqual(settings["notes"], "general")
        self.assertEqual(settings["project_notes"], {"Ad spot": "logo at end, then card"})
        page.on_notes_scope({"scope": ""})
        self.assertEqual(self.last("notes_state")["projects"], ["Ad spot"])

    def test_a_save_that_lands_after_resolve_switched_still_lists_the_project(self):
        page = self.make(open_project="Wedding")
        self.bridge().project_detected.emit("Ad spot", True)    # the view's pending save for Wedding comes after
        self.assertEqual(self.last("notes_state")["projects"], ["Ad spot"])
        page.on_notes({"scope": "Wedding", "text": "speech at 12:30"})
        n = self.last("notes_state")
        self.assertEqual((n["scope"], n["projects"]), ("Ad spot", ["Ad spot", "Wedding"]))

    def test_emptied_project_notes_leave_the_list(self):
        page = self.make({"project_notes": {"Old job": "x"}})
        page.on_notes({"scope": "Old job", "text": ""})
        page.on_notes_scope({"scope": ""})
        self.assertEqual(self.host.tools["essentials"]["project_notes"], {})
        self.assertEqual(self.last("notes_state")["projects"], [])

    def test_names_are_tidied_and_junk_is_dropped(self):
        self.assertEqual(page_mod.clean_project("  My   Film \n"), "My Film")
        self.assertEqual(page_mod.clean_project(None), "")
        self.assertEqual(page_mod.clean_project_notes({"A": "a", "": "x", "B": "", "C": 3, " D ": "d"}), {"A": "a", "D": "d"})

    def test_without_time_tracker_there_is_no_open_project(self):
        page = self.make({}, tracker=False)
        page.web_ready()
        self.assertEqual(self.last("notes_state")["open"], "")


class RegistryTests(unittest.TestCase):
    def test_essentials_is_a_main_tab_with_its_own_heading(self):
        # Read, not imported: importing registry.py pulls in every page,
        # some needing packages the release machines don't install (numpy).
        from test_games import registry_page
        self.assertEqual(registry_page("Essentials"), ("EssentialsPage", True))

    def test_ask_buddy_knows_it(self):
        from core.tools_kb import get_tool
        info = get_tool("essentials", [("Essentials", page_mod.EssentialsPage)])
        self.assertIsNotNone(info)
        self.assertTrue(info.is_available)


if __name__ == "__main__":
    unittest.main()
