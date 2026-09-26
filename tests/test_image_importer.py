"""Image Importer: staging (no Qt) and the web page driven with a fake Media
Pool, a fake clipboard and files in a temp folder - never a real Resolve,
never the real ~/.image_importer settings or the real Downloads folder."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.image_importer import staging

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QMimeData, QPointF, Qt, QUrl, QEvent
    from PySide6.QtGui import QDropEvent, QImage
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class StagingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = os.path.join(self._tmp.name, "src")
        self.save = os.path.join(self._tmp.name, "save")

    def test_copy_into(self):
        a = touch(os.path.join(self.src, "a.png"))
        touch(os.path.join(self.save, "a.png"))                       # a clash in the save folder
        notes = touch(os.path.join(self.src, "notes.txt"))
        already = touch(os.path.join(self.save, "b.jpg"))             # already where it'd be copied to
        copied, skipped, errors = staging.copy_into([a, notes, already, "missing.png"], self.save)
        self.assertEqual([os.path.basename(p) for p in copied], ["a (2).png", "b.jpg"])
        self.assertEqual((skipped, errors), (["notes.txt"], []))
        self.assertTrue(os.path.exists(a))                            # copied, not moved

    def test_list(self):
        s = staging.Staging()
        one = s.add(touch(os.path.join(self.save, "one.png")), "Pasted image")
        s.add(os.path.join(self.save, "gone.png"), "Copied file")
        present, missing = s.split_existing()
        self.assertEqual(([i["id"] for i in present], missing), ([one["id"]], ["gone.png"]))
        self.assertEqual(s.remove([one["id"], "nope"]), 1)
        self.assertEqual(s.clear(), 1)


class Mem(dict):
    def save(self):
        self.saved = True


class Folder:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class Pool:
    def __init__(self):
        self.root = Folder("Master")
        self.subs = [Folder("Footage")]
        self.imported = None

    def GetRootFolder(self):
        root = self.root
        root.GetSubFolderList = lambda: list(self.subs)
        return root

    def AddSubFolder(self, parent, name):
        folder = Folder(name)
        self.subs.append(folder)
        return folder

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def ImportMedia(self, paths):
        self.imported = (self.current.name, [os.path.basename(p) for p in paths])
        return [object() for _ in paths]


class Controller:
    def __init__(self):
        self.pool = Pool()
        pool = self.pool
        self.project = type("Project", (), {"GetMediaPool": lambda s: pool, "GetName": lambda s: "Test"})()

    def current_project(self):
        return self.project


class Host:
    def __init__(self, save_folder):
        self.controller, self.connected = Controller(), True
        self.shared_settings = {"theme": "Resolve"}
        self.busy = []
        self.tool = Mem(save_folder=save_folder, bin_name="Downloads")

    def ensure_connected(self):
        return self.controller

    def tool_settings(self, tool_id, defaults=None):
        return self.tool

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def set_busy(self, on, message=None):
        self.busy.append(on)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.save = os.path.join(self._tmp.name, "save")
        from pages.image_importer import page as page_mod
        self.page_mod = page_mod
        self.host = Host(self.save)
        self.page = page_mod.ImageImporterPage(self.host)
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def test_paste_an_image_then_import(self):
        image = QImage(8, 8, QImage.Format_RGB32)
        image.fill(0xFF3366)
        with mock.patch.object(self.page_mod, "read_clipboard", return_value=("image", image)):
            self.page.on_paste(None)
        (item,) = self.last("items")
        self.assertTrue(item["name"].startswith("pasted_") and item["exists"])
        self.assertTrue(item["url"].startswith("file:///"))
        self.page.on_bin_name({"value": "  Stills "})
        self.assertEqual(self.host.tool["bin_name"], "Stills")
        self.page.on_import_images(None)
        self.assertEqual(self.host.controller.pool.imported, ("Stills", [item["name"]]))
        self.assertEqual(self.last("items"), [])
        self.assertEqual(self.host.busy, [True, False])

    def test_dropped_files_are_copied_and_non_images_skipped(self):
        src = os.path.join(self._tmp.name, "src")
        paths = [touch(os.path.join(src, "shot.jpg")), touch(os.path.join(src, "readme.md"))]
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        drop = QDropEvent(QPointF(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.assertTrue(self.page._drop_filter.eventFilter(self.page.view, drop))
        self.app.processEvents()
        (item,) = self.last("items")
        self.assertEqual(item["name"], "shot.jpg")
        self.assertTrue(os.path.exists(os.path.join(self.save, "shot.jpg")))
        self.assertIn("readme.md", self.last("log")[-1]["text"])

    def test_import_needs_a_bin_name_and_files(self):
        self.page.on_import_images(None)
        self.assertEqual(self.last("alert")["title"], "Nothing to import")
        self.page.on_bin_name({"value": ""})
        self.page.staging.add(touch(os.path.join(self.save, "x.png")), "Copied file")
        self.page.on_import_images(None)
        self.assertEqual(self.last("alert")["title"], "Name the bin")

    def test_nothing_on_the_clipboard(self):
        with mock.patch.object(self.page_mod, "read_clipboard", return_value=("empty", None)):
            self.page.on_paste(None)
        self.assertIn("Nothing to paste", self.last("toast")["text"])


if __name__ == "__main__":
    unittest.main()
