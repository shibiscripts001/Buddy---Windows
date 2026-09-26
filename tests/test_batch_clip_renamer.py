"""Batch Clip Renamer: the rename rules (renamer.py, no Qt) and the page
driven against fake Media Pool clips - never a real Resolve."""

import os
import unittest

import _paths  # noqa: F401
from pages.batch_clip_renamer import renamer

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def seq(base, start="1"):
    return {"mode": "sequential", "base": base, "start": start}


def rep(find, replace, match_case=True):
    return {"mode": "replace", "find": find, "replace": replace, "match_case": match_case}


class RuleTests(unittest.TestCase):
    def test_sequential_numbering(self):
        rows, problem = renamer.plan(["a", "b", "c"], seq("  Drone ", "9"))
        self.assertIsNone(problem)
        self.assertEqual([r["new"] for r in rows], ["Drone_09", "Drone_10", "Drone_11"])
        self.assertTrue(all(r["changed"] for r in rows))
        rows, _ = renamer.plan(["x"] * 3, seq("S", "99"))
        self.assertEqual(rows[-1]["new"], "S_101")

    def test_start_number(self):
        self.assertEqual(renamer.parse_start(""), 1)
        self.assertEqual(renamer.parse_start(" 7 "), 7)
        self.assertEqual(renamer.parse_start("0"), 0)
        for bad in ("-1", "1.5", "one"):
            self.assertIsNone(renamer.parse_start(bad), bad)
        rows, problem = renamer.plan(["a"], seq("B", "x"))
        self.assertEqual(problem[0], "start")
        self.assertFalse(rows[0]["changed"])

    def test_an_empty_field_is_a_quiet_problem(self):
        self.assertEqual(renamer.plan(["a"], seq(""))[1], ("base", ""))
        self.assertEqual(renamer.plan(["a"], rep("", "x"))[1], ("find", ""))

    def test_renaming_to_the_same_name_is_not_a_change(self):
        rows, _ = renamer.plan(["S_01", "S_02"], seq("S"))
        self.assertFalse(any(r["changed"] for r in rows))

    def test_find_and_replace(self):
        rows, _ = renamer.plan(["A001_cam", "B002_CAM", "other"], rep("cam", "Camera"))
        self.assertEqual([r["new"] for r in rows], ["A001_Camera", "B002_CAM", "other"])
        rows, _ = renamer.plan(["A001_cam", "B002_CAM"], rep("cam", "Camera", match_case=False))
        self.assertEqual([r["new"] for r in rows], ["A001_Camera", "B002_Camera"])

    def test_find_text_is_literal_not_a_pattern(self):
        rows, _ = renamer.plan(["a.b", "axb"], rep(".", "_"))
        self.assertEqual([r["new"] for r in rows], ["a_b", "axb"])
        rows, _ = renamer.plan(["x"], rep("x", r"\1\g<0>"))
        self.assertEqual(rows[0]["new"], r"\1\g<0>")

    def test_a_name_is_never_emptied(self):
        rows, _ = renamer.plan(["take", "take_2"], rep("take", ""))
        self.assertEqual((rows[0]["new"], rows[0]["changed"], rows[0]["skipped"]), ("take", False, "would be empty"))
        self.assertEqual(rows[1]["new"], "_2")

    def test_duplicates(self):
        rows, _ = renamer.plan(["a_1", "b_1", "c_2"], rep("a_", "b_"))
        self.assertEqual(renamer.duplicate_names(rows), ["b_1"])


# ---------------------------------------------------------- fake Resolve --

class Clip:
    def __init__(self, uid, name, kind="Video", accept=True):
        self.uid, self.name, self.kind, self.accept = uid, name, kind, accept

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.name

    def GetClipProperty(self, key):
        return {"Clip Name": self.name, "Type": self.kind}[key]

    def SetClipProperty(self, key, value):
        if key == "Clip Name" and self.accept:
            self.name = value
            return True
        return False


class Folder:
    def __init__(self, clips):
        self.clips = clips

    def GetName(self):
        return "Day 1"

    def GetUniqueId(self):
        return "folder-1"

    def GetClipList(self):
        return list(self.clips)


class Pool:
    def __init__(self, folder):
        self.folder, self.selected = folder, []

    def GetCurrentFolder(self):
        return self.folder

    def GetSelectedClips(self):
        return list(self.selected)


class Project:
    def __init__(self, pool):
        self.pool = pool

    def GetMediaPool(self):
        return self.pool


class Controller:
    def __init__(self, clips):
        self.pool = Pool(Folder(clips))

    def current_project(self):
        return Project(self.pool)


class Host:
    def __init__(self, controller):
        self.controller, self.connected = controller, True
        self.shared_settings = {"theme": "Resolve"}
        self.busy = []

    def ensure_connected(self):
        return self.controller

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def set_busy(self, on, message=None):
        self.busy.append(on)

    def pump_busy(self, message=None):
        pass


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from pages.batch_clip_renamer.page import BatchClipRenamerPage
        self.clips = [Clip("1", "A001"), Clip("2", "A002"), Clip("t", "Timeline 1", kind="Timeline"),
                      Clip("3", "B001")]
        self.host = Host(Controller(self.clips))
        self.page = BatchClipRenamerPage(self.host)
        self.page._poll.stop()
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))
        self.page.on_shown()

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def names(self):
        return [c.name for c in self.clips]

    def test_preview_leaves_timelines_out_and_changes_nothing(self):
        self.page.on_inputs(seq("Shot"))
        state = self.last("state")
        self.assertEqual([r["new"] for r in state["rows"]], ["Shot_01", "Shot_02", "Shot_03"])
        self.assertEqual((state["where"], state["changes"]), ("Day 1", 3))
        self.assertEqual(self.names(), ["A001", "A002", "Timeline 1", "B001"])   # preview only

    def test_rename_then_undo(self):
        self.page.on_inputs(rep("A0", "Cam_"))
        self.page.on_rename(None)
        self.assertEqual(self.names(), ["Cam_01", "Cam_02", "Timeline 1", "B001"])
        self.assertEqual(self.last("state")["undo"]["count"], 2)
        self.assertEqual(self.last("state")["changes"], 0)   # nothing left to match
        self.clips[1].name = "Renamed in Resolve"
        self.page.on_undo(None)
        self.assertEqual(self.names(), ["A001", "Renamed in Resolve", "Timeline 1", "B001"])
        self.assertIsNone(self.last("state")["undo"])
        self.assertIn("left alone", self.last("log")[-1]["text"])

    def test_rename_refuses_when_resolve_changed_since_the_preview(self):
        self.page.on_inputs(seq("Shot"))
        self.clips.append(Clip("4", "C001"))        # a clip lands in the bin
        self.page.on_rename(None)
        self.assertEqual(self.names()[:2], ["A001", "A002"])
        self.assertEqual(self.last("state")["total"], 4)   # preview redrawn with it
        self.page.on_rename(None)                   # a second press, having looked
        self.assertEqual(self.names()[-1], "Shot_04")

    def test_selected_scope(self):
        self.page.on_scope({"scope": "selected"})
        self.assertEqual(self.last("state")["error"], "No clips are selected in the Media Pool.")
        self.host.controller.pool.selected = [self.clips[2], self.clips[3]]   # a timeline and a clip
        self.page.on_scope({"scope": "bin"})
        self.page.on_scope({"scope": "selected"})
        self.page.on_inputs(seq("Pick"))
        self.assertEqual([r["new"] for r in self.last("state")["rows"]], ["Pick_01"])

    def test_a_refused_rename_is_reported(self):
        self.clips[0].accept = False
        self.page.on_inputs(seq("Shot"))
        self.page.on_rename(None)
        self.assertEqual(self.names(), ["A001", "Shot_02", "Timeline 1", "Shot_03"])
        self.assertIn("refused to rename 1: A001", self.last("log")[-1]["text"])

    def test_not_connected(self):
        self.host.connected = False
        self.page.on_shown()
        state = self.last("state")
        self.assertFalse(state["connected"])
        self.assertEqual(state["rows"], [])


if __name__ == "__main__":
    unittest.main()
