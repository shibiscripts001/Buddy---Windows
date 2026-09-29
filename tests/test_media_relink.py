"""Media Relink: the matching rules and the two modes' row logic (no Qt),
and the web page driven against fake Media Pool clips pointing at files in
a temp folder - never a real Resolve."""

import os
import json
import tempfile
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.media_relink import relink_rows, resolve_ext
from pages.media_relink.relink_engine import MatchStatus, SearchCancelled, build_file_index
from pages.media_relink.relink_rows import MODE_FIX, MODE_RELOCATE

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return path


class Clip:
    def __init__(self, name, path, kind="Video", accept=True):
        self.name, self.path, self.kind, self.accept = name, path, kind, accept

    def GetName(self):
        return self.name

    def GetClipProperty(self, key):
        return {"Clip Name": self.name, "File Path": self.path, "Type": self.kind}[key]

    def ReplaceClip(self, path):
        if self.accept:
            self.path = path
        return self.accept


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.old = os.path.join(self.tmp, "Server")
        self.new = os.path.join(self.tmp, "SSD")
        touch(os.path.join(self.old, "A001.mov"))                      # still online
        for rel in ("Day1/A001.mov", "Day1/B002.mov", "Day1/C003.MOV", "Day2/C003.mov"):
            touch(os.path.join(self.new, rel))
        self.clips = [
            Clip("A001", os.path.join(self.old, "A001.mov")),
            Clip("B002", os.path.join(self.old, "B002.mov")),        # offline, one match
            Clip("C003", os.path.join(self.old, "c003.mov")),        # offline, two matches (case differs)
            Clip("D004", os.path.join(self.old, "D004.mov")),        # offline, nowhere
            Clip("Title", ""),                                       # generated - no file
        ]

    def rows(self):
        return relink_rows.make_rows([(c, "Master/Footage") for c in self.clips],
                                     lambda c: c.path, lambda c: c.name)


class RuleTests(Base):
    def test_scan(self):
        rows, skipped = self.rows()
        self.assertEqual(skipped, 1)
        self.assertEqual([r["status"] for r in rows],
                         [MatchStatus.ONLINE, MatchStatus.NOT_FOUND, MatchStatus.NOT_FOUND, MatchStatus.NOT_FOUND])

    def test_fix_mode_leaves_working_clips_alone(self):
        rows, _ = self.rows()
        matched, ambiguous = relink_rows.apply_search(rows, build_file_index(self.new), MODE_FIX)
        self.assertEqual((matched, ambiguous), (1, 1))
        self.assertEqual(rows[0]["status"], MatchStatus.ONLINE)
        self.assertEqual(rows[1]["resolved"], os.path.join(self.new, "Day1", "B002.mov"))
        self.assertEqual(len(rows[2]["candidates"]), 2)
        self.assertEqual(relink_rows.counts(rows), {"total": 4, "online": 1, "offline": 1, "matched": 1, "ambiguous": 1})

    def test_relocate_mode_considers_everything(self):
        rows, _ = self.rows()
        relink_rows.apply_search(rows, build_file_index(self.new), MODE_RELOCATE)
        self.assertEqual(rows[0]["status"], MatchStatus.MATCH_FOUND)
        # Searching where a clip already is proposes nothing for it.
        rows, _ = self.rows()
        relink_rows.apply_search(rows, build_file_index(self.old), MODE_RELOCATE)
        self.assertEqual(rows[0]["status"], MatchStatus.ONLINE)

    def test_a_pick_survives_a_later_ambiguous_search(self):
        rows, _ = self.rows()
        index = build_file_index(self.new)
        relink_rows.apply_search(rows, index, MODE_FIX)
        relink_rows.pick(rows[2], rows[2]["candidates"][1])
        relink_rows.apply_search(rows, index, MODE_FIX)
        self.assertEqual(rows[2]["resolved"], os.path.join(self.new, "Day2", "C003.mov"))

    def test_relink(self):
        rows, _ = self.rows()
        relink_rows.apply_search(rows, build_file_index(self.new), MODE_FIX)
        self.clips[1].accept = True
        result = relink_rows.relink(rows, lambda clip, path: clip.ReplaceClip(path))
        self.assertEqual(result, (1, 2, 0))    # B002 done; C003 unpicked and D004 unmatched skipped
        self.assertEqual(self.clips[1].path, os.path.join(self.new, "Day1", "B002.mov"))
        self.assertEqual(rows[1]["status"], MatchStatus.ONLINE)

    def test_index_can_stop(self):
        with self.assertRaises(SearchCancelled):
            build_file_index(self.new, should_stop=lambda: True)
        seen = []
        build_file_index(self.new, progress=seen.append)
        self.assertEqual(seen[-1], 4)

    def test_index_reports_missing_or_unreadable_folders(self):
        with self.assertRaises(NotADirectoryError):
            build_file_index(os.path.join(self.tmp, "missing"))

        unreadable = PermissionError("Access denied")

        def failing_walk(_root, onerror):
            yield self.new, [], ["B002.mov"]
            onerror(unreadable)

        with mock.patch("pages.media_relink.relink_engine.os.walk", side_effect=failing_walk):
            with self.assertRaises(PermissionError):
                build_file_index(self.new)


# ------------------------------------------------------------------ page --

class Folder:
    def __init__(self, name, clips, subs=()):
        self.name, self.clips, self.subs = name, clips, list(subs)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subs)


class Controller:
    def __init__(self, root):
        pool = type("Pool", (), {"GetRootFolder": lambda s: root,
                                  "GetCurrentFolder": lambda s: s.current})()
        pool.current = root
        self.project = type("Project", (), {"GetMediaPool": lambda s: pool,
                                            "GetUniqueId": lambda s: s._id})()
        self.project._id = "project-a"

    def current_project(self):
        return self.project


class ScopeTests(unittest.TestCase):
    def test_current_bin_excludes_siblings_sub_bins_and_timelines(self):
        clip = Clip("Current", "current.mov")
        child_clip = Clip("Child", "child.mov")
        sibling_clip = Clip("Sibling", "sibling.mov")
        current = Folder("Footage", [clip, Clip("Edit", "", kind="Timeline")],
                         [Folder("Child", [child_clip])])
        root = Folder("Master", [], [current, Folder("Sibling", [sibling_clip])])
        controller = Controller(root)
        controller.project.GetMediaPool().current = current
        self.assertEqual(resolve_ext.scan_current_bin(controller), [(clip, "Footage")])
        self.assertEqual([c for c, _ in resolve_ext.scan_all_clips(controller)],
                         [clip, child_clip, sibling_clip])

    def test_current_bin_never_falls_back_to_entire_project(self):
        controller = Controller(Folder("Master", [Clip("Clip", "clip.mov")]))
        controller.project.GetMediaPool().current = None
        with self.assertRaises(resolve_ext.ResolveConnectionError):
            resolve_ext.scan_current_bin(controller)


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


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(Base):
    def setUp(self):
        super().setUp()
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from pages.media_relink import page as page_mod
        self.page_mod = page_mod
        timeline = Clip("Edit", "", kind="Timeline")
        root = Folder("Master", [timeline], [Folder("Footage", self.clips)])
        self.host = Host(Controller(root))
        self.page = page_mod.MediaRelinkPage(self.host)
        self.addCleanup(delete, self.page)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def js(self, code):
        loop, result = QEventLoop(), {}
        self.page.view.page().runJavaScript(code, 0, lambda value: (result.update(value=value), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        return result.get("value")

    def test_shift_click_ranges_use_visible_rows_and_reset_after_rescan(self):
        self.page.show()
        deadline = time.monotonic() + 10
        while not self.js("typeof selectRow === 'function'"):
            self.app.processEvents()
            if time.monotonic() > deadline:
                self.fail("Media Relink web view did not load")
        self.page.on_scan(None)
        rows = self.last("rows")
        # Online A001 (id 0) is hidden; the three visible ids are 1, 2, 3.
        self.js(f"Buddy.receive('state', {json.dumps(self.last('state'))});"
                f"Buddy.receive('rows', {json.dumps(rows)});")
        self.assertEqual(self.js("document.querySelectorAll('tbody tr').length"), 3)

        def click(index, shift=False):
            self.js("document.querySelectorAll('tbody input')[" + str(index) + "]"
                    ".dispatchEvent(new MouseEvent('click', {bubbles: true, shiftKey: "
                    + str(shift).lower() + "}));")

        def picked():
            return self.js("JSON.stringify([...selected.fix].sort())")

        click(0)
        click(2, True)
        self.assertEqual(picked(), "[1,2,3]")
        click(2, True)  # Shift-uncheck clears the same range.
        self.assertEqual(picked(), "[]")
        click(2)
        click(0, True)  # Reverse range.
        self.assertEqual(picked(), "[1,2,3]")
        self.js("document.getElementById('only-offline').click()")
        click(0, True)  # Filter change discarded the old anchor.
        self.assertEqual(picked(), "[0,1,2,3]")
        self.page.on_scan(None)
        self.js(f"Buddy.receive('rows', {json.dumps(self.last('rows'))});")
        self.assertEqual(picked(), "[]")
        click(3, True)
        self.assertEqual(picked(), "[3]")
        self.page.on_mode({"mode": MODE_RELOCATE})
        self.page.on_scan(None)
        self.js(f"Buddy.receive('state', {json.dumps(self.last('state'))});"
                f"Buddy.receive('rows', {json.dumps(self.last('rows'))});")
        click(0)
        click(2, True)
        self.assertEqual(self.js("JSON.stringify([...selected.relocate].sort())"), "[0,1,2]")
        self.assertEqual(picked(), "[3]")  # Each mode keeps its own selection.

    def search(self, folder):
        with mock.patch.object(self.page_mod.QFileDialog, "getExistingDirectory", return_value=folder):
            self.page.on_search(None)
        deadline = time.monotonic() + 5
        while self.page._search and time.monotonic() < deadline:
            self.app.processEvents()

    def test_scan_search_pick_relink(self):
        self.page.on_scan(None)
        rows = self.last("rows")
        self.assertEqual([r["bin"] for r in rows["rows"]], ["Master/Footage"] * 4)   # timeline left out
        self.assertIn("1 had no single file", rows["summary"])
        self.search(self.new)
        rows = self.last("rows")
        self.assertEqual(rows["counts"]["ambiguous"], 1)
        self.assertIsNone(self.last("search"))
        c003 = rows["rows"][2]
        self.page.on_pick({"id": c003["id"], "path": "C:/not/a/candidate.mov"})    # refused
        self.assertIsNone(self.page._lists[MODE_FIX]["rows"][2]["resolved"])
        self.page.on_pick({"id": c003["id"], "path": c003["candidates"][0]})
        self.page.on_relink_all(None)
        self.assertEqual(self.clips[1].path, os.path.join(self.new, "Day1", "B002.mov"))
        self.assertEqual(self.clips[2].path, c003["candidates"][0])
        self.assertEqual(self.clips[0].path, os.path.join(self.old, "A001.mov"))   # the online clip untouched
        self.assertEqual(self.host.busy[-2:], [True, False])

    def test_relocate_only_relinks_what_was_selected(self):
        self.page.on_mode({"mode": MODE_RELOCATE})
        self.page.on_scan(None)
        self.search(self.new)
        self.page.on_relink_all(None)                      # not available in this mode
        self.assertEqual(self.clips[0].path, os.path.join(self.old, "A001.mov"))
        self.page.on_relink_selected({"ids": []})
        self.assertEqual(self.last("alert")["title"], "Select clips first")
        self.page.on_relink_selected({"ids": [0]})
        self.assertEqual(self.clips[0].path, os.path.join(self.new, "Day1", "A001.mov"))
        self.assertEqual(self.clips[1].path, os.path.join(self.old, "B002.mov"))
        self.assertEqual(self.page._lists[MODE_FIX]["rows"], [])   # the other mode's list is its own

    def test_search_needs_a_scan_first(self):
        self.page.on_search(None)
        self.assertEqual(self.last("alert")["title"], "Scan first")

    def test_scope_switch_rescans_and_clears_matches_in_both_modes(self):
        pool = self.host.controller.project.GetMediaPool()
        pool.current = Folder("Only B", [self.clips[1]])
        for mode in (MODE_FIX, MODE_RELOCATE):
            with self.subTest(mode=mode):
                self.page.on_mode({"mode": mode})
                self.page.on_scan(None)
                self.search(self.new)
                old_generation = self.last("rows")["generation"]
                self.page.on_scope({"scope": "bin"})
                rows = self.last("rows")
                self.assertEqual([r["name"] for r in rows["rows"]], ["B002"])
                self.assertFalse(rows["rows"][0]["new_path"])
                self.assertGreater(rows["generation"], old_generation)
                self.assertEqual(self.last("state")["scope"], "bin")
                self.page.on_scope({"scope": "project"})
                self.assertEqual(len(self.last("rows")["rows"]), 4)

    def test_scope_is_remembered_per_mode_and_locked_during_search(self):
        self.page.on_scope({"scope": "bin"})
        self.page.on_mode({"mode": MODE_RELOCATE})
        self.assertEqual(self.last("state")["scope"], "project")
        self.page.on_mode({"mode": MODE_FIX})
        self.assertEqual(self.last("state")["scope"], "bin")
        self.page._search = {"mode": MODE_FIX}
        self.page.on_scope({"scope": "project"})
        self.assertEqual(self.page._lists[MODE_FIX]["scope"], "bin")
        self.page._search = None

    def test_project_change_requires_new_scan(self):
        self.page.on_scan(None)
        self.search(self.new)
        self.host.controller.project._id = "project-b"

        with mock.patch.object(self.page_mod.QFileDialog, "getExistingDirectory") as dialog:
            self.page.on_search(None)
            dialog.assert_not_called()
        self.assertEqual(self.last("alert")["title"], "Project changed")

        self.page.on_relink_all(None)
        self.assertEqual(self.last("alert")["title"], "Project changed")
        self.assertEqual(self.clips[1].path, os.path.join(self.old, "B002.mov"))


if __name__ == "__main__":
    unittest.main()
