"""Command Center: hot key combos (core/hotkeys.py, no Qt), its actions
against a fake Resolve (pages/command_center/actions.py), and the page
recording keys, saving bindings and running one - with the Windows hot
keys faked, so a test never takes real keys from anything."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from core import hotkeys
from pages.command_center import actions


class ComboTests(unittest.TestCase):
    def test_a_keydown_becomes_a_combo_and_back(self):
        self.assertEqual(hotkeys.from_event("KeyM", ctrl=True, alt=True), "Ctrl+Alt+M")
        self.assertEqual(hotkeys.from_event("Digit1", ctrl=True, shift=True), "Ctrl+Shift+1")
        self.assertEqual(hotkeys.from_event("NumpadAdd", alt=True), "Alt+Num Plus")
        self.assertEqual(hotkeys.from_event("ShiftLeft", shift=True), "")     # a modifier alone
        self.assertEqual(hotkeys.parse("Ctrl+Alt+M"), (["Ctrl", "Alt"], "KeyM"))
        self.assertEqual(hotkeys.parse("Alt+Num Plus"), (["Alt"], "NumpadAdd"))
        for bad in ("", None, "Alt+Ctrl+M", "Ctrl+Ctrl+M", "Hyper+M", "Ctrl+Banana"):
            self.assertIsNone(hotkeys.parse(bad), bad)

    def test_what_can_be_a_hot_key(self):
        self.assertEqual(hotkeys.problem("Ctrl+Alt+M"), "")
        self.assertEqual(hotkeys.problem("F13"), "")                    # nobody types with F13
        self.assertIn("Hold Ctrl", hotkeys.problem("M"))
        self.assertIn("Hold Ctrl", hotkeys.problem("Shift+M"))
        self.assertIn("Windows keeps", hotkeys.problem("Win+L"))
        self.assertEqual(hotkeys.conflicts("Ctrl+B"), "Razor")
        self.assertEqual(hotkeys.conflicts("Ctrl+Alt+B"), "")

    def test_windows_codes(self):
        mods, vk = hotkeys.windows_codes("Ctrl+Shift+F5")
        self.assertEqual(vk, 0x74)
        self.assertEqual(mods, hotkeys.MOD_NOREPEAT | 0x0002 | 0x0004)


class FakeItem:
    def __init__(self, name, kind="video", color=""):
        self.name, self.kind, self.color = name, kind, color

    def GetName(self):
        return self.name

    def GetTrackTypeAndIndex(self):
        return self.kind, 1

    def SetClipColor(self, color):
        self.color = color
        return True

    def ClearClipColor(self):
        self.color = ""
        return True


class FakeTimeline:
    def __init__(self, name="Main edit", start=86400, fps="24"):
        self.name, self.start, self.fps = name, start, fps
        self.markers = {}
        self.playhead = "01:00:10:00"
        self.selected = []
        self.under = FakeItem("A001.mov")

    def GetName(self):
        return self.name

    def GetSetting(self, key):
        return {"timelineFrameRate": self.fps, "timelineDropFrameTimecode": "0"}.get(key, "")

    def GetStartFrame(self):
        return self.start

    def GetCurrentTimecode(self):
        return self.playhead

    def SetCurrentTimecode(self, tc):
        self.playhead = tc
        return True

    def GetMarkers(self):
        return dict(self.markers)

    def AddMarker(self, frame, color, name, note, duration, data):
        # Like Resolve 21.1: no nameless markers, one per frame.
        if not name or frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name}
        return True

    def GetSelectedClips(self):
        return self.selected

    def GetCurrentVideoItem(self):
        return self.under

    def DuplicateTimeline(self, name):
        if any(ch in name for ch in ':/\\?*"<>|'):
            return None   # Resolve refuses these in a timeline name
        self.project.current = FakeTimeline(name)
        self.project.timelines.append(self.project.current)
        return self.project.current


class FakeProject:
    def __init__(self):
        self.timeline = FakeTimeline()
        self.timeline.project = self
        self.timelines = [self.timeline]
        self.current = self.timeline
        self.frames = []

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, i):
        return self.timelines[i - 1]

    def ExportCurrentFrameAsStill(self, path):
        self.frames.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG")
        return True


class GapItem:
    """A clip on GapTimeline: where it sits, and its Media Pool clip."""
    def __init__(self, start, end, clip=None):
        self.start, self.end, self.clip = start, end, clip

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetMediaPoolItem(self):
        return self.clip


class GapClip:
    def __init__(self, frames=120, fps="24"):
        self.frames, self.fps = frames, fps

    def GetUniqueId(self):
        return id(self)

    def GetClipProperty(self, key):
        return {"Frames": str(self.frames), "FPS": self.fps}[key]


class GapTimeline(FakeTimeline):
    """Tracks of clips, ripple-deleting as Resolve 21.1 does (measured): every
    track moves up, and markers after a deleted stretch move with it."""
    def __init__(self, tracks):
        super().__init__()
        self.tracks = {k: [[GapItem(*span) for span in row] for row in rows] for k, rows in tracks.items()}
        self.locked = set()

    def GetTrackCount(self, kind):
        return len(self.tracks.get(kind, []))

    def GetItemListInTrack(self, kind, track):
        return sorted(self.tracks[kind][track - 1], key=lambda i: i.start)

    def GetIsTrackLocked(self, kind, track):
        return (kind, track) in self.locked

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(frame, None) is not None

    def DeleteClips(self, items, ripple=False):
        for row in (r for rows in self.tracks.values() for r in rows):
            row[:] = [i for i in row if i not in items]
        if ripple:
            for item in sorted(items, key=lambda i: -i.start):
                cut = item.end - item.start
                for row in (r for rows in self.tracks.values() for r in rows):
                    for i in row:
                        if i.start >= item.end:
                            i.start, i.end = i.start - cut, i.end - cut
                rel = item.end - self.start
                self.markers = {(f - cut if f >= rel else f): m for f, m in self.markers.items()}
        return True


class GapPool:
    def __init__(self, timeline):
        self.timeline = timeline

    def AppendToTimeline(self, infos):
        info = infos[0]
        length = info["endFrame"] - info["startFrame"]
        item = GapItem(info["recordFrame"], info["recordFrame"] + length, info["mediaPoolItem"])
        self.timeline.tracks["video"][info["trackIndex"] - 1].append(item)
        return [item]


class FakeController:
    def __init__(self):
        self.project = FakeProject()

    def current_project(self):
        return self.project


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.c = FakeController()
        self.tl = self.c.project.timeline

    def test_a_marker_at_the_playhead_and_a_second_is_refused(self):
        r = actions.run_marker(self.c, {"color": "Red", "ask": False})
        self.assertEqual(self.tl.markers, {240: {"color": "Red", "name": "Marker"}})   # 10 s in, at 24 fps
        self.assertIn("Red marker at 01:00:10:00", r["text"])
        with self.assertRaises(actions.ActionError):
            actions.run_marker(self.c, {"color": "Red", "ask": False})
        at = actions.playhead(self.c)
        self.tl.markers.clear()
        actions.add_marker(self.c, at["frame"], "Blue", "Client note", at["timecode"])
        self.assertEqual(self.tl.markers[240]["name"], "Client note")

    def test_jumping_between_markers_and_by_colour(self):
        self.tl.markers = {48: {"color": "Blue"}, 300: {"color": "Red", "name": "Fix audio"}, 480: {"color": "Blue"}}
        self.assertEqual(actions.jump(self.c, True)["text"], "Fix audio – 01:00:12:12")
        self.assertEqual(self.tl.playhead, "01:00:12:12")
        actions.jump(self.c, True, "Blue")
        self.assertEqual(self.tl.playhead, "01:00:20:00")
        actions.jump(self.c, False, "Blue")
        self.assertEqual(self.tl.playhead, "01:00:02:00")
        with self.assertRaises(actions.ActionError):
            actions.jump(self.c, False)                       # nothing before 2 s in

    def test_copy_timecode_with_the_clip(self):
        self.assertEqual(actions.run_copy_timecode(self.c, {"with_clip": False})["clipboard"], "01:00:10:00")
        self.assertEqual(actions.run_copy_timecode(self.c, {"with_clip": True})["clipboard"], "01:00:10:00 – A001.mov")

    def test_clip_colour_on_the_selection_else_under_the_playhead(self):
        picked = [FakeItem("a"), FakeItem("b"), FakeItem("subs", kind="subtitle")]
        self.tl.selected = picked
        self.assertEqual(actions.run_clip_color(self.c, {"color": "Teal"})["text"], "Teal on 2 clips")
        self.assertEqual([i.color for i in picked], ["Teal", "Teal", ""])
        self.tl.selected = []
        actions.run_clip_color(self.c, {"color": "Pink"})
        self.assertEqual(self.tl.under.color, "Pink")
        actions.run_clip_color(self.c, {"color": actions.NO_COLOR})
        self.assertEqual(self.tl.under.color, "")
        self.tl.under = None
        with self.assertRaises(actions.ActionError):
            actions.run_clip_color(self.c, {"color": "Pink"})

    def test_saving_a_version_keeps_you_on_your_timeline(self):
        r = actions.run_save_version(self.c, {})
        self.assertIs(self.c.project.current, self.tl)
        self.assertEqual(len(self.c.project.timelines), 2)
        self.assertTrue(self.c.project.timelines[1].name.startswith("Main edit – "))
        self.assertIn("Saved", r["text"])

    def test_version_names_dont_collide(self):
        name = actions.version_name("A", set(), 1_790_000_000)
        self.assertNotIn(":", name)
        self.assertEqual(actions.version_name("A", {name}, 1_790_000_000), f"{name} (2)")

    def test_copy_frame_and_save_it_too(self):
        folder = tempfile.mkdtemp()
        r = actions.run_copy_frame(self.c, {"folder": folder})
        self.assertTrue(os.path.isfile(r["image"]))
        self.assertEqual(os.listdir(folder), ["Main edit 01.00.10.00.png"])
        self.c.project.ExportCurrentFrameAsStill = lambda path: False
        with self.assertRaises(actions.ActionError):
            actions.run_copy_frame(self.c, {"folder": ""})

    def test_no_timeline(self):
        self.c.project.current = None
        for run in (actions.run_copy_timecode, actions.run_marker):
            with self.assertRaises(actions.ActionError):
                run(self.c, {"with_clip": False, "color": "Blue", "ask": False})

    def test_options_are_checked(self):
        self.assertEqual(actions.clean_options("marker", {"color": "Plaid", "ask": "yes"}), {"color": "Blue", "ask": True})
        self.assertEqual(actions.clean_options("copy_frame", {"folder": 5}), {"folder": ""})
        self.assertEqual(actions.summary("next_marker", {"color": "Red"}), "Next Red marker")
        self.assertEqual(actions.summary("clip_color", {"color": actions.NO_COLOR}), "Clear the clip colour")


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class RemoveGapsTests(unittest.TestCase):
    """The layout live-tested on Resolve 21.1: gaps on V1/A1, a cutaway on V2
    inside the first one, a sound on A2, a clip on V3, timeline markers."""
    S = 86400

    def setUp(self):
        S, clip = self.S, GapClip()
        main = [(S, S + 119), (S + 168, S + 287), (S + 312, S + 431)]
        self.tl = GapTimeline({"video": [main, [(S + 130, S + 150)], [(S + 400, S + 420)]],
                               "audio": [main, [(S + 200, S + 260)]]})
        for row in self.tl.tracks["video"]:
            for item in row:
                item.clip = clip
        self.c = FakeController()
        project = self.c.project
        project.timeline = project.current = self.tl
        project.timelines = [self.tl]
        self.tl.project = project
        project.GetMediaPool = lambda: GapPool(self.tl)
        for frame, name in ((50, "before"), (140, "cutaway"), (200, "after 2"), (300, "inside 3"), (330, "after 3")):
            self.tl.AddMarker(frame, "Blue", name, "", 1, "")

    def where(self, kind, track):
        return [(i.start - self.S, i.end - self.S) for i in self.tl.GetItemListInTrack(kind, track)]

    def test_only_stretches_empty_on_every_track(self):
        self.assertEqual(actions.empty_stretches([(0, 10), (5, 20), (30, 40), (35, 50), (60, 70)]),
                         [(20, 30), (50, 60)])
        self.assertEqual(actions.empty_stretches([(10, 20)]), [])   # space before the first clip stays
        self.assertEqual(actions.shifted(300, [(119, 130), (150, 168), (287, 312)]), 258)
        self.assertEqual(actions.shifted(330, [(119, 130), (150, 168), (287, 312)]), 276)

    def test_closes_them_with_everything_in_sync(self):
        r = actions.run_remove_gaps(self.c, {"keep_copy": True})
        self.assertTrue(r["text"].startswith("Closed 3 gaps (2.2 s) – the old one is saved as “Main edit – "))
        self.assertEqual(self.where("video", 1), [(0, 119), (139, 258), (258, 377)])
        self.assertEqual(self.where("video", 2), [(119, 139)])
        self.assertEqual(self.where("video", 3), [(346, 366)])
        self.assertEqual(self.where("audio", 2), [(171, 231)])
        # As measured: the one inside a gap goes where the gap closed.
        self.assertEqual({f: m["name"] for f, m in self.tl.markers.items()},
                         {50: "before", 129: "cutaway", 171: "after 2", 258: "inside 3", 276: "after 3"})
        self.assertIs(self.c.project.current, self.tl)
        self.assertEqual(len(self.c.project.timelines), 2)
        self.assertEqual(actions.run_remove_gaps(self.c, {"keep_copy": False})["text"], "No gaps between clips.")

    def test_a_locked_track_stops_it_before_anything_changes(self):
        self.tl.locked.add(("video", 3))
        with self.assertRaises(actions.ActionError) as e:
            actions.run_remove_gaps(self.c, {"keep_copy": True})
        self.assertIn("Unlock V3", str(e.exception))
        self.assertEqual(self.where("video", 1), [(0, 119), (168, 287), (312, 431)])
        self.assertEqual(len(self.c.project.timelines), 1)

    def test_a_short_clip_fills_a_long_gap_in_pieces(self):
        for row in self.tl.tracks["video"]:
            for item in row:
                item.clip = GapClip(frames=10)
        actions.run_remove_gaps(self.c, {"keep_copy": False})
        self.assertEqual(self.where("video", 1), [(0, 119), (139, 258), (258, 377)])


class Mem(dict):
    def save(self):
        pass


class FakeHotkeys:
    def __init__(self, parent=None):
        from PySide6.QtCore import QObject, Signal

        class _S(QObject):
            pressed = Signal(str)
        self._s = _S()
        self.pressed = self._s.pressed
        self.registered = {}

    def set(self, combos):
        self.registered = dict(combos)
        return {}

    def clear(self):
        self.registered = {}


class Host:
    def __init__(self):
        self.tools, self.connected, self.controller, self.pages = {}, True, FakeController(), {}

    def tool_settings(self, tool_id, defaults=None):
        return self.tools.setdefault(tool_id, Mem(defaults or {}))

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtCore import QCoreApplication, Qt
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from pages.command_center import page as page_mod
        self.page_mod = page_mod
        for patch in (mock.patch.object(page_mod.hotkeys, "SUPPORTED", True),
                      mock.patch.object(page_mod.hotkeys, "GlobalHotkeys", FakeHotkeys, create=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.host = Host()
        self.page = page_mod.CommandCenterPage(self.host)
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))

    def last(self, name):
        found = [p for n, p in self.events if n == name]
        return found[-1] if found else None

    def settle(self, until, seconds=5):
        """Runs Qt's events until until() is true: the worker's answer comes back as a queued signal."""
        import time
        end = time.monotonic() + seconds
        while not until() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def binding(self, action):
        return next(b for b in self.page.bindings if b["action"] == action)

    def test_every_action_starts_with_one_hot_key_to_set(self):
        self.assertEqual(sorted(b["action"] for b in self.page.bindings), sorted(a["id"] for a in actions.ACTIONS))
        self.assertTrue(all(not b["keys"] for b in self.page.bindings))

    def test_recording_keys_registers_them_and_warns_about_resolves(self):
        marker = self.binding("marker")
        self.page.on_record({"id": marker["id"], "code": "KeyB", "ctrl": True})
        self.assertEqual(marker["keys"], "Ctrl+B")
        self.assertEqual(self.page.hotkeys.registered, {marker["id"]: "Ctrl+B"})
        view = next(b for g in self.last("state")["groups"] for a in g["actions"] for b in a["bindings"]
                    if b["id"] == marker["id"])
        self.assertIn("Razor", view["note"])
        self.page.on_record({"id": marker["id"], "code": "KeyM"})      # no modifier: refused
        self.assertEqual(marker["keys"], "Ctrl+B")
        self.assertIn("Hold Ctrl", self.last("toast")["text"])
        self.page.on_enable({"on": False})
        self.assertEqual(self.page.hotkeys.registered, {})
        self.page.on_enable({"on": True})
        self.page.on_clear_keys({"id": marker["id"]})
        self.assertEqual(self.page.hotkeys.registered, {})

    def test_a_hot_key_per_colour(self):
        self.page.on_add({"action": "marker"})
        markers = [b for b in self.page.bindings if b["action"] == "marker"]
        self.assertEqual(len(markers), 2)
        self.page.on_option({"id": markers[1]["id"], "key": "color", "value": "Red"})
        self.assertEqual(markers[1]["options"]["color"], "Red")
        self.page.on_remove({"id": markers[1]["id"]})
        self.assertEqual(len([b for b in self.page.bindings if b["action"] == "marker"]), 1)

    def test_running_one_from_its_button(self):
        b = self.binding("marker")
        self.page.window = lambda: mock.Mock(isVisible=lambda: True, isActiveWindow=lambda: True)
        self.page.on_run({"id": b["id"]})
        self.settle(lambda: self.page.recent)
        self.assertEqual(self.host.controller.project.timeline.markers, {240: {"color": "Blue", "name": "Marker"}})
        self.assertIn("Blue marker", self.last("toast")["text"])
        self.assertEqual(self.page.recent[0]["what"], "Blue marker at the playhead")

    def test_a_pressed_hot_key_runs_its_binding(self):
        b = self.binding("copy_timecode")
        shown = []
        with mock.patch.object(self.page_mod, "Osd") as osd:
            osd.return_value.flash = lambda text, ok, tokens: shown.append((text, ok))
            self.page._pressed(b["id"])
            self.settle(lambda: shown)
        self.assertEqual(shown, [("Copied 01:00:10:00", True)])
        self.assertEqual(QApplication.clipboard().text(), "01:00:10:00")

    def test_saved_bindings_survive_and_bad_ones_are_dropped(self):
        kept = self.page_mod.clean_bindings([
            {"id": "x1", "action": "marker", "options": {"color": "Red"}, "keys": "Ctrl+Alt+1"},
            {"id": "x2", "action": "nope"}, "junk",
            {"id": "x1", "action": "clip_color", "options": {}, "keys": "Ctrl+Banana"}])
        self.assertEqual([(b["action"], b["keys"]) for b in kept], [("marker", "Ctrl+Alt+1"), ("clip_color", "")])
        self.assertNotEqual(kept[1]["id"], "x1")              # ids stay unique


if __name__ == "__main__":
    unittest.main()
