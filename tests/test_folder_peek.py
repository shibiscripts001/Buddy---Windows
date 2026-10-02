"""Peek and Preview for link bar links: what a folder's Peek lists and
where it may go (app/core/folder_peek.py, no Qt), the Peek window's drag
and its fence (core/link_peek_web.py), a web page photographed out of
sight, and the link menu offering each. Real folders, but only in a
temporary directory."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from core import folder_peek as fp

try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class _Folder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="buddy_peek_")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "B-Roll", "Day 2"))
        os.makedirs(os.path.join(self.root, "audio"))
        for name, size in (("A001.mov", 3000), ("notes.txt", 10), ("still.PNG", 5), (".DS_Store", 1),
                           ("Zed.wav", 2048)):
            with open(os.path.join(self.root, name), "wb") as fh:
                fh.write(b"x" * size)

    def at(self, *parts):
        return os.path.join(self.root, *parts)


class ListTests(_Folder):
    def test_folders_first_then_files_with_their_kinds(self):
        state = fp.list_folder(self.root, self.root)
        self.assertEqual([e["name"] for e in state["entries"]],
                         ["audio", "B-Roll", "A001.mov", "notes.txt", "still.PNG", "Zed.wav"])   # no .DS_Store
        kinds = {e["name"]: e["kind"] for e in state["entries"]}
        self.assertEqual(kinds, {"audio": "folder", "B-Roll": "folder", "A001.mov": "film", "notes.txt": "file",
                                 "still.PNG": "image", "Zed.wav": "music"})
        sizes = {e["name"]: e["size"] for e in state["entries"]}
        self.assertEqual((sizes["B-Roll"], sizes["A001.mov"], sizes["notes.txt"], sizes["Zed.wav"]),
                         (None, "2.9 KB", "10 bytes", "2.0 KB"))
        thumbs = [e["name"] for e in state["entries"] if e["thumb"]]
        self.assertEqual(thumbs, ["still.PNG"])
        self.assertTrue(state["entries"][4]["thumb"].startswith("file:///"))
        self.assertIsNone(state["up"])
        self.assertIsNone(state["error"])

    def test_inside_and_back_up_but_never_above(self):
        state = fp.list_folder(self.at("B-Roll", "Day 2"), self.root)
        self.assertEqual(state["up"], self.at("B-Roll"))
        self.assertEqual([c["name"] for c in state["crumbs"]], [os.path.basename(self.root), "B-Roll", "Day 2"])
        self.assertEqual(state["entries"], [])
        # Asked for somewhere outside, it lists the root.
        for outside in (os.path.dirname(self.root), self.at("..", "elsewhere"), "", None, "C:\\Windows"):
            self.assertEqual(fp.list_folder(outside, self.root)["path"], os.path.abspath(self.root), outside)

    def test_a_folder_thats_gone_says_so(self):
        gone = self.at("audio")
        os.rmdir(gone)
        state = fp.list_folder(gone, self.root)
        self.assertEqual(state["path"], gone)
        self.assertIn("isn't there", state["error"])

    def test_a_huge_folder_is_cut_short_and_counted(self):
        with mock.patch.object(fp, "MAX_ENTRIES", 3):
            state = fp.list_folder(self.root, self.root)
        self.assertEqual((len(state["entries"]), state["more"]), (3, 3))

    def test_only_paths_under_the_root_are_handed_on(self):
        film = self.at("A001.mov")
        self.assertEqual(fp.allowed([film, film.upper(), self.at("missing.mov"), os.path.dirname(self.root),
                                     self.at("..", os.path.basename(self.root) + "x"), 5, None,
                                     self.at("B-Roll")], self.root),
                         [film, self.at("B-Roll")])
        self.assertEqual(fp.allowed("not a list", self.root), [])
        self.assertFalse(fp.within(self.root + "x", self.root))     # a sibling with the same start
        self.assertTrue(fp.within(self.at("B-Roll", "Day 2"), self.root))

    def test_sizes(self):
        self.assertEqual([fp.human_size(n) for n in (0, 1, 999, 1536, 15 * 1024 ** 2, 3 * 1024 ** 3)],
                         ["0 bytes", "1 byte", "999 bytes", "1.5 KB", "15 MB", "3.0 GB"])


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PeekWindowTests(_Folder):
    def setUp(self):
        super().setUp()
        self.app = QApplication.instance() or QApplication([])
        import core.link_peek_web as lpw
        self.lpw = lpw
        host = mock.Mock()
        host.theme_tokens.side_effect = lambda: __import__("core.theme", fromlist=["x"]).get_theme_tokens("Resolve")
        host.shared_settings = {}
        patch = mock.patch.object(lpw, "_theme_host", lambda parent: host)
        patch.start()
        self.addCleanup(patch.stop)
        self.peek = lpw.FolderPeek(None, self.root, "Footage")
        self.peek.emit = mock.Mock()
        self.addCleanup(self.peek.deleteLater)

    def test_dragging_out_copies_only_whats_under_the_root(self):
        with mock.patch.object(self.lpw, "QDrag") as Drag:
            self.peek.on_drag({"paths": [self.at("A001.mov"), os.path.join(os.path.dirname(self.root), "x"),
                                         self.at("B-Roll")]})
        mime = Drag.return_value.setMimeData.call_args[0][0]
        self.assertEqual([os.path.normpath(u.toLocalFile()) for u in mime.urls()],
                         [self.at("A001.mov"), self.at("B-Roll")])
        Drag.return_value.exec.assert_called_once_with(self.lpw.Qt.CopyAction)     # never a move
        self.peek.emit.assert_called_with("drag_done")

    def test_nothing_to_drag_starts_no_drag(self):
        with mock.patch.object(self.lpw, "QDrag") as Drag:
            self.peek.on_drag({"paths": ["C:\\Windows\\notepad.exe"]})
        Drag.assert_not_called()
        self.peek.emit.assert_called_with("drag_done")

    def test_it_goes_in_and_out_but_not_above_the_root(self):
        self.peek.on_go({"path": self.at("B-Roll")})
        self.assertEqual(self.peek.folder, self.at("B-Roll"))
        self.peek.on_go({"path": os.path.dirname(self.root)})
        self.assertEqual(self.peek.folder, self.at("B-Roll"))
        self.peek.on_open({"paths": [self.at("B-Roll", "Day 2")]})          # opening a folder goes into it
        self.assertEqual(self.peek.folder, self.at("B-Roll", "Day 2"))
        with mock.patch.object(self.lpw.QDesktopServices, "openUrl") as opened:
            self.peek.on_open({"paths": [self.at("notes.txt"), "C:\\Windows\\notepad.exe"]})
        self.assertEqual([os.path.normpath(c.args[0].toLocalFile()) for c in opened.call_args_list],
                         [self.at("notes.txt")])

    def test_a_page_is_photographed_and_closed(self):
        page = self.at("page.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write("<title>Hi</title><body style='background:#36c'><h1>Hello</h1>")
        got = []
        with mock.patch.object(self.lpw, "SETTLE_MS", 50):
            capture = self.lpw.PageCapture("file:///" + page.replace("\\", "/"))
            capture.done.connect(lambda jpeg, title, why: got.append((jpeg, title, why)))
            for _ in range(80):
                if got:
                    break
                _wait(100)
        self.assertTrue(got, "never photographed")
        jpeg, title, why = got[0]
        self.assertEqual((title, why), ("Hi", ""))
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))                       # a JPEG


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class LinkMenuTests(_Folder):
    def setUp(self):
        super().setUp()
        from test_settings_pages import _Shell
        self.shell = _Shell("setUp")
        self.shell.setUp()
        self.addCleanup(self.shell.doCleanups)
        self.win = self.shell.win
        self.win.links = [{"name": "Footage", "url": self.root}, {"name": "Site", "url": "https://example.com"},
                          {"name": "Clip", "url": self.at("A001.mov")}]

    def menu_for(self, index):
        import core.shell_window as sw
        made = []

        class Menu(sw.QMenu):
            def __init__(self, *a):
                super().__init__(*a)
                made.append(self)

            def exec(self, *_a):
                made.append([a.text() for a in self.actions() if a.text()])

        with mock.patch.object(sw, "QMenu", Menu):
            self.win.open_link_menu(index)
        return made[-1]

    def test_folders_peek_and_web_pages_preview(self):
        self.assertIn("Peek", self.menu_for(0))
        self.assertNotIn("Preview", self.menu_for(0))
        self.assertIn("Preview", self.menu_for(1))
        self.assertNotIn("Peek", self.menu_for(1))
        self.assertFalse({"Peek", "Preview"} & set(self.menu_for(2)))           # a file just opens

    def test_each_opens_its_window(self):
        import core.shell_window as sw
        with mock.patch.object(sw, "FolderPeek") as peek, mock.patch.object(sw, "PagePreview") as preview:
            self.win.peek_link(0)
            self.win.peek_link(1)                                            # not a folder
            self.win.preview_link(1)
            self.win.preview_link(0)                                         # not a web page
        peek.assert_called_once_with(self.win, self.root, "Footage")
        preview.assert_called_once_with(self.win, "https://example.com", "Site")


if __name__ == "__main__":
    unittest.main()
