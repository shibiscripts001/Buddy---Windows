"""Media Relink: the matching rules and the two modes' row logic (no Qt),
and the web page driven against fake Media Pool clips pointing at files in
a temp folder - never a real Resolve."""

import os
import tempfile
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.media_relink import relink_rows
from pages.media_relink.relink_engine import MatchStatus, SearchCancelled, build_file_index
from pages.media_relink.relink_rows import MODE_FIX, MODE_RELOCATE

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
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
        pool = type("Pool", (), {"GetRootFolder": lambda s: root})()
        self.project = type("Project", (), {"GetMediaPool": lambda s: pool})()

    def current_project(self):
        return self.project


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
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

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


if __name__ == "__main__":
    unittest.main()
