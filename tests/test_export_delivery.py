"""YouTube Chapters and Stills Exporter: the chapter rules (no Qt) and
both web pages driven against a fake Resolve - never a real project."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from core import marker_colors
from pages.youtube_chapters import chapters

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

FPS = 24.0



def markers(*items):
    """(seconds, name, color) -> a GetMarkers() dict (frame keys)."""
    return {float(s * FPS): {"color": c, "name": n, "note": "", "duration": 1} for s, n, c in items}


class ChapterRuleTests(unittest.TestCase):
    def test_timecodes(self):
        self.assertEqual(chapters.format_timecode(0), "00:00")
        self.assertEqual(chapters.format_timecode(75.9), "01:15")
        self.assertEqual(chapters.format_timecode(3723), "01:02:03")

    def test_intro_is_added_when_the_first_marker_is_late(self):
        r = chapters.build(markers((30, "Setup", "Blue"), (90, "Grade", "Blue"), (200, "Export", "Blue")), FPS)
        self.assertEqual(r["text"], "00:00 - Intro\n00:30 - Setup\n01:30 - Grade\n03:20 - Export")
        self.assertEqual(r["warnings"], [])

    def test_an_early_first_marker_moves_to_zero_and_keeps_its_name(self):
        r = chapters.build(markers((4, "Cold open", "Blue"), (12, "Titles", "Blue")), FPS)
        self.assertEqual(r["text"], "00:00 - Cold open\n00:12 - Titles")   # 12 s from 00:00, not 8 from 00:04
        self.assertIn("Moved from 00:04", r["chapters"][0]["note"])
        self.assertIn("at least 3", r["warnings"][0])

    def test_too_close_markers_are_left_out_and_reported(self):
        r = chapters.build(markers((0, "A", "Blue"), (5, "B", "Blue"), (20, "C", "Blue")), FPS)
        self.assertEqual([c["name"] for c in r["chapters"]], ["A", "C"])
        self.assertEqual([(s["name"], s["time"]) for s in r["skipped"]], [("B", "00:05")])
        self.assertIn("Only 5 s", r["skipped"][0]["note"])

    def test_colour_filter_and_unnamed_markers(self):
        mk = markers((3, "Red one", "Red"), (40, "", "Blue"), (80, "Last", "Blue"))
        r = chapters.build(mk, FPS, "Blue")
        # The Red marker at 3 s doesn't count as "first" when only Blue is shown.
        self.assertEqual(r["text"], "00:00 - Intro\n00:40 - Chapter\n01:20 - Last")
        self.assertEqual(chapters.build(mk, FPS, "Green")["chapters"], [])

    def test_file_names(self):
        self.assertEqual(chapters.file_name_for('My: "Film" v2'), "My_Film_v2_Chapters.txt")
        self.assertEqual(chapters.file_name_for(""), chapters.DEFAULT_FILE_NAME)

    def test_colour_counts(self):
        counts = {c["name"]: c["count"] for c in marker_colors.color_counts(markers((1, "", "Red"), (2, "", "Red"), (3, "", "Mint")))}
        self.assertEqual((counts["Red"], counts["Mint"], counts["Blue"]), (2, 1, 0))
        self.assertEqual(len(counts), 16)


# ------------------------------------------------------------ fake Resolve --

class Timeline:
    def __init__(self):
        self.markers = markers((0, "Open", "Blue"), (30, "Middle", "Blue"), (60, "Wide", "Red"), (95, "End", "Blue"))
        self.playhead = "01:00:10:00"
        self.visited = []

    def GetName(self):
        return "My Film"

    def GetMarkers(self):
        return dict(self.markers)

    def GetSetting(self, key):
        return {"timelineFrameRate": "24", "timelineDropFrameTimecode": "0"}.get(key)

    def GetStartFrame(self):
        return 86400

    def GetCurrentTimecode(self):
        return self.playhead

    def AddMarker(self, frame, color, name, note, duration, custom):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration}
        return True

    def SetCurrentTimecode(self, tc):
        self.visited.append(tc)
        return True

    def GrabStill(self):
        return f"still@{self.visited[-1]}"


class Album:
    def __init__(self):
        self.exported = self.deleted = None

    def ExportStills(self, stills, folder, prefix, fmt):
        self.exported = (list(stills), folder, prefix, fmt)
        return True

    def DeleteStills(self, stills):
        self.deleted = list(stills)
        return True


class Project:
    def __init__(self):
        self.timeline = Timeline()
        self.album = Album()

    def GetCurrentTimeline(self):
        return self.timeline

    def GetGallery(self):
        album = self.album
        return type("Gallery", (), {"GetCurrentStillAlbum": lambda s: album})()


class Controller:
    def __init__(self):
        self.project = Project()
        self.pages = []
        pages = self.pages
        self.resolve = type("Resolve", (), {"OpenPage": lambda s, p: pages.append(p)})()

    def current_project(self):
        return self.project


class Mem(dict):
    def save(self):
        pass


class Host:
    shared_settings = {"theme": "Resolve"}

    def __init__(self):
        self.controller, self.connected = Controller(), True
        self.busy = []
        self.tools = {}

    def ensure_connected(self):
        return self.controller

    def tool_settings(self, tool_id, defaults=None):
        return self.tools.setdefault(tool_id, Mem(defaults or {}))

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def set_busy(self, on, message=None):
        self.busy.append(on)


class _PageCase(unittest.TestCase):
    page_class = None

    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.host = Host()
        self.page = self.page_class(self.host)
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ChaptersPageTests(_PageCase):
    @property
    def page_class(self):
        from pages.youtube_chapters.page import YouTubeChaptersPage
        return YouTubeChaptersPage

    def test_reads_the_timeline_and_follows_the_filter(self):
        self.page.on_refresh()
        s = self.last("state")
        self.assertEqual((s["timeline"], s["total"]), ("My Film", 4))
        c = self.last("chapters")
        self.assertEqual(c["text"], "00:00 - Open\n00:30 - Middle\n01:00 - Wide\n01:35 - End")
        self.assertEqual(c["file_name"], "My_Film_Chapters.txt")
        self.page.on_filter({"color": "Red"})
        self.assertEqual(self.last("chapters")["text"], "00:00 - Intro\n01:00 - Wide")

    def test_an_edit_is_kept_until_reset(self):
        self.page.on_refresh()
        self.page.on_edit({"text": "00:00 - Mine"})
        self.host.controller.project.timeline.markers[float(120 * FPS)] = {"color": "Blue", "name": "New"}
        self.page._read(connect=False)
        c = self.last("chapters")
        self.assertEqual((c["text"], c["edited"], c["stale"]), ("00:00 - Mine", True, True))
        self.page.on_reset_text()
        self.assertTrue(self.last("chapters")["text"].endswith("02:00 - New"))

    def test_copy_and_save(self):
        self.page.on_refresh()
        self.page.on_copy()
        self.assertTrue(QApplication.clipboard().text().startswith("00:00 - Open"))
        self.page.on_save()
        self.assertEqual(self.last("alert")["title"], "Choose a folder")
        self.page.on_folder({"value": self._tmp.name})
        self.page.on_file_name({"value": "chapters"})
        self.page.on_save()
        with open(os.path.join(self._tmp.name, "chapters.txt"), encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("00:00 - Open\n00:30 - Middle"))

    def test_no_timeline(self):
        self.host.controller.project.timeline = None
        self.page.on_refresh()
        self.assertIn("No active timeline", self.last("state")["problem"])
        self.assertEqual(self.last("chapters")["chapters"], [])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class StillsPageTests(_PageCase):
    @property
    def page_class(self):
        from pages.stills_exporter.page import StillsExporterPage
        return StillsExporterPage

    def settle(self):
        """Lets the page's worker thread (it reads and adds markers off the UI thread)
        finish, and its answers - which can start the next read - arrive."""
        for _ in range(3):
            self.assertTrue(self.page._worker.wait_idle(5))
            QCoreApplication.processEvents()

    def test_marker_counts_and_the_markers_a_grab_will_visit(self):
        self.page.on_refresh()
        self.settle()
        s = self.last("state")
        self.assertEqual({c["name"]: c["count"] for c in s["colors"]}["Blue"], 3)
        self.assertEqual([x["timecode"] for x in s["markers"]], ["01:00:00:00", "01:00:30:00", "01:01:35:00"])
        self.page.on_options({"color": "Red"})
        self.assertEqual([x["name"] for x in self.last("state")["markers"]], ["Wide"])

    def test_add_a_named_marker(self):
        self.page.on_add_marker({"name": "Hero"})
        self.settle()
        added = self.host.controller.project.timeline.markers[10 * FPS]
        self.assertEqual((added["color"], added["name"]), ("Blue", "Hero"))
        self.assertIn("Hero", [m["name"] for m in self.last("state")["markers"]])   # read again after
        self.page.on_add_marker({"name": ""})                        # same frame again
        self.settle()
        self.assertEqual(self.last("alert")["title"], "Couldn't add the marker")
        self.assertIn("already be a marker", self.last("alert")["text"])

    def test_a_marker_refused_while_playing_says_so(self):
        timeline = self.host.controller.project.timeline
        frames = iter(range(1000))
        timeline.GetCurrentTimecode = lambda: f"01:00:10:{next(frames) % 24:02d}"   # moving
        timeline.AddMarker = lambda *args: False
        self.page.on_add_marker({"name": ""})
        self.settle()
        self.assertIn("while the timeline is playing", self.last("alert")["text"])

    def test_a_stuck_read_leaves_the_page_responsive_and_says_so(self):
        """Resolve holds scripting calls while the timeline plays - the page mustn't
        wait with it (on the UI thread each 2-second poll froze Buddy)."""
        import threading
        import time
        from pages.stills_exporter import page as stills
        timeline = self.host.controller.project.timeline
        release, real = threading.Event(), timeline.GetMarkers
        timeline.GetMarkers = lambda: (release.wait(10), real())[1]
        self.addCleanup(release.set)
        with mock.patch.object(stills, "BUSY_AFTER_S", 0.05), mock.patch.object(stills, "ACTION_WAIT_S", 0.05):
            t0 = time.monotonic()
            self.page.on_refresh()                                   # a read Resolve holds
            time.sleep(0.1)
            self.page._read(connect=False)                           # the next poll: no second read
            self.assertTrue(self.last("state")["busy"])
            self.page.on_add_marker({"name": "Hero"})
            self.assertEqual(self.last("alert")["title"], stills.BUSY_TITLE)
            self.page.on_grab()
            self.assertEqual(self.host.controller.pages, [])        # nothing started behind it
            self.assertLess(time.monotonic() - t0, 2)               # never waited on Resolve
        release.set()
        self.settle()
        self.assertFalse(self.last("state")["busy"])
        self.assertEqual(self.last("state")["timeline"], "My Film")
        self.assertNotIn(10 * FPS, timeline.markers)                # the refused click added nothing

    def test_grab_then_export_and_delete(self):
        self.page.on_grab()
        self.assertEqual(self.host.controller.pages, ["color"])
        grabbed = self.last("grabbed")
        self.assertEqual([g["timecode"] for g in grabbed], ["01:00:00:00", "01:00:30:00", "01:01:35:00"])
        self.page.on_remove_grabbed({"id": grabbed[1]["id"]})
        self.assertEqual(len(self.last("grabbed")), 2)

        self.page.on_export({})
        self.assertEqual(self.last("alert")["title"], "Choose a folder")
        self.page.settings["folder"] = self._tmp.name
        self.page.on_options({"format": "PNG", "prefix": "Shot_", "delete_after": True})
        album = self.host.controller.project.album
        self.page.on_export({})                                       # delete needs the view's confirmation
        self.assertIsNone(album.exported)
        self.page.on_export({"confirmed": True})
        self.assertEqual(album.exported, (["still@01:00:00:00", "still@01:01:35:00"], self._tmp.name, "Shot_", "png"))
        self.assertEqual(album.deleted, album.exported[0])
        self.assertEqual(self.last("grabbed"), [])
        self.assertEqual(self.host.busy, [True, False, True, False])

    def test_nothing_to_export(self):
        self.page.on_export({})
        self.assertEqual(self.last("alert")["title"], "Nothing to export")


if __name__ == "__main__":
    unittest.main()
