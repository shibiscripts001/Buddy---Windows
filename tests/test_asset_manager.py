"""Asset Manager: the list rules (library_view, no Qt), the web page against
a library in a temp folder - never the real ~/.asset_manager - with a fake
Media Pool, and the preview's waveform decoded from a generated WAV."""

import math
import os
import struct
import tempfile
import time
import unittest
import wave
from unittest import mock

import _paths  # noqa: F401
from pages.asset_manager import library_view as lv

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def asset(i, path, added="2026-01-01 10:00", category="Image"):
    return {"id": str(i), "path": path, "name": os.path.basename(path), "ext": os.path.splitext(path)[1],
            "category": category, "date_added": added}


ALWAYS = lambda _p: True  # noqa: E731


class ListRuleTests(unittest.TestCase):
    def setUp(self):
        self.assets = [
            asset(1, "C:/Music/b.wav", "2026-01-03 10:00", "Audio"),
            asset(2, "C:/Music/a.wav", "2026-01-01 10:00", "Audio"),
            asset(3, "C:/Logos/zeta.png", "2026-01-05 10:00"),
            asset(4, "D:/Stock/clip.mov", "2026-01-02 10:00", "Video"),
        ]

    def names(self, nodes):
        return [n["name"] for n in nodes]

    def test_two_or_more_from_a_folder_become_a_bucket(self):
        nodes = lv.build(self.assets, exists=ALWAYS)
        self.assertEqual(self.names(nodes), ["clip.mov", "Music", "zeta.png"])
        self.assertEqual(self.names(nodes[1]["children"]), ["a.wav", "b.wav"])
        self.assertEqual(nodes[1]["count"], 2)

    def test_folders_first_when_not_sorted_with_items(self):
        nodes = lv.build(self.assets, sort=lv.SORT_ADDED, reverse=True, include_folders_in_sort=False, exists=ALWAYS)
        self.assertEqual(self.names(nodes), ["Music", "zeta.png", "clip.mov"])

    def test_by_date_a_bucket_sorts_by_its_newest(self):
        nodes = lv.build(self.assets, sort=lv.SORT_ADDED, exists=ALWAYS)
        self.assertEqual(self.names(nodes), ["clip.mov", "Music", "zeta.png"])

    def test_always_bucket_and_flat(self):
        self.assertTrue(all(n["type"] == "folder" for n in lv.build(self.assets, always_bucket=True, exists=ALWAYS)))
        self.assertEqual(self.names(lv.build(self.assets, flat=True, exists=ALWAYS)),
                         ["a.wav", "b.wav", "clip.mov", "zeta.png"])

    def test_filter(self):
        self.assertEqual([a["id"] for a in lv.matching(self.assets, "Audio", "")], ["1", "2"])
        self.assertEqual([a["id"] for a in lv.matching(self.assets, "All", "ZET")], ["3"])

    def test_selection_counts_a_folder_once(self):
        nodes = lv.build(self.assets, exists=ALWAYS)
        loose, groups = lv.resolve_selection(nodes, ["1", "3", "3"], ["C:/Music"])
        self.assertEqual((loose, groups), (["3"], [("Music", ["2", "1"])]))
        self.assertEqual(lv.all_ids(loose, groups), ["3", "2", "1"])

    def test_waveform_bars(self):
        levels = [(0.1, 0.2)] * 50 + [(0.5, 1.0)] * 50 + [(0.0, 0.0)] * 50
        bars = lv.waveform_bars(levels, 3)
        self.assertEqual(bars, [round(0.14 / 0.7, 4), 1.0, 0.03])
        self.assertEqual(lv.waveform_bars([]), [])
        self.assertEqual(len(lv.waveform_bars([(0.2, 0.3)] * 10)), 10)


# ------------------------------------------------------------------ page --

class Mem(dict):
    def save(self):
        pass


class Folder:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class Pool:
    def __init__(self):
        self.current = Folder("Day 1")
        self.imports = []

    def GetCurrentFolder(self):
        return self.current

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def AddSubFolder(self, parent, name):
        return Folder(f"{parent.name}/{name}")

    def ImportMedia(self, paths):
        self.imports.append((self.current.name, sorted(os.path.basename(p) for p in paths)))
        return list(paths)


class Host:
    def __init__(self):
        pool = self.pool = Pool()
        project = type("Project", (), {"GetMediaPool": lambda s: pool})()
        self.controller = type("C", (), {"current_project": lambda s: project})()
        self.connected = True
        self.shared_settings = {"theme": "Resolve"}
        self.busy = []
        self.tool = Mem()

    def ensure_connected(self):
        return self.controller

    def tool_settings(self, tool_id, defaults=None):
        return self.tool

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def set_busy(self, on, message=None):
        self.busy.append(on)


def make_wav(path, seconds=1.0, rate=8000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(12000 * math.sin(i / 8) * (i / (rate * seconds))))
                          for i in range(int(rate * seconds)))
        w.writeframes(frames)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        # The player lets go of a file a moment after it's stopped.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: [self.app.processEvents() or time.sleep(0.02) for _ in range(10)])
        data = os.path.join(self._tmp.name, "data")
        from pages.asset_manager import data_manager as dm
        for name, value in (("DATA_DIR", data), ("ASSET_STORE_PATH", os.path.join(data, "assets.json")),
                            ("PROJECT_STORE_PATH", os.path.join(data, "projects.json"))):
            patch = mock.patch.object(dm, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        from pages.asset_manager.page import AssetManagerPage
        self.host = Host()
        self.page = AssetManagerPage(self.host)
        self.addCleanup(self.page.on_app_quitting)
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))
        self.media = os.path.join(self._tmp.name, "media")
        self.files = []
        for rel in ("Music/one.wav", "Music/two.wav", "Logos/logo.png", "notes.txt"):
            path = os.path.join(self.media, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if path.endswith(".wav"):
                make_wav(path)
            else:
                open(path, "wb").close()
            self.files.append(path)

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def test_drop_a_folder_then_import_it_as_a_bin(self):
        self.page.on_files_dropped([self.media])
        listing = self.last("list")
        self.assertEqual(listing["total"], 3)                       # notes.txt isn't media
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, "data", "assets.json")))
        music = next(n for n in listing["nodes"] if n["type"] == "folder")
        logo = next(n for n in listing["nodes"] if n["type"] == "asset")
        self.page.on_import_selected({"ids": [logo["id"]], "folders": [music["path"]]})
        self.assertEqual(self.host.pool.imports, [("Day 1", ["logo.png"]), ("Day 1/Music", ["one.wav", "two.wav"])])
        self.assertEqual(self.host.pool.current.name, "Day 1")    # the open bin is put back
        self.assertEqual(self.host.busy, [True, False])

    def test_projects_link_into_the_library(self):
        self.page.on_files_dropped([self.files[2]])
        self.page.on_view({"view": "projects"})
        self.page.on_files_dropped([self.files[0]])
        self.assertEqual(self.last("alert")["title"], "Make a project first")
        self.page.on_new_project({"name": "Client A"})
        self.page.on_new_project({"name": "client a "})
        self.page.on_new_project({"name": "Client A"})
        self.assertEqual(self.last("alert")["text"], "There's already a project with that name.")
        self.page.on_files_dropped([self.files[0]])                  # new to the library, linked here
        self.page.on_list_available(None)
        (available,) = self.last("available")["rows"]
        self.page.on_link_existing({"ids": [available["id"]]})
        self.assertEqual(self.last("list")["total"], 2)
        self.page.on_remove({"ids": [available["id"]]})             # only out of the project
        self.assertEqual(len(self.page.library.assets), 2)
        self.page.on_rename_project({"name": "Client B"})
        self.assertIn("Client B", [p["name"] for p in self.last("projects")["items"]])

    def test_remove_from_the_library_leaves_every_project(self):
        self.page.on_files_dropped([self.files[2]])
        asset_id = next(iter(self.page.library.assets))
        self.page.on_new_project({"name": "P"})
        self.page.projects.add_asset(self.page.project_id, asset_id)
        self.page.on_remove({"ids": [asset_id]})
        self.assertEqual(self.page.library.assets, {})
        self.assertEqual(self.page.projects.projects[self.page.project_id]["asset_ids"], [])
        self.assertTrue(os.path.exists(self.files[2]))               # the file stays

    def test_preview_image_and_missing(self):
        self.page.on_files_dropped([self.files[2]])
        asset_id = next(iter(self.page.library.assets))
        self.page.on_select({"ids": [asset_id]})
        self.assertTrue(self.last("preview")["image"].startswith("file:///"))
        os.remove(self.files[2])
        self.page.on_refresh(None)
        self.assertTrue(self.last("preview")["missing"])

    def test_audio_preview_decodes_a_waveform(self):
        self.page.on_files_dropped([self.files[0]])
        asset_id = next(iter(self.page.library.assets))
        self.page.on_select({"ids": [asset_id]})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(n in ("media",) for n, _ in self.events):
            self.app.processEvents()
            time.sleep(0.02)
        media = self.last("media")
        if media.get("kind") == "failed":
            self.skipTest(f"Qt Multimedia can't decode here: {media.get('message')}")
        self.assertEqual(media["kind"], "audio")
        wave_event = self.last("waveform")
        self.assertTrue(wave_event["done"] and 1 <= len(wave_event["bars"]) <= 120)
        self.assertEqual(max(wave_event["bars"]), 1.0)


if __name__ == "__main__":
    unittest.main()
