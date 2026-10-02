"""The Web tab's download history (app/pages/web/downloads_log.py), the
Downloads bin calls (resolve_ext.py) and the browser keeping both."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.web import downloads_log as log
from pages.web import resolve_ext


class LogTests(unittest.TestCase):
    def test_kinds_by_extension(self):
        self.assertEqual([log.kind_of(n) for n in ("a.MP4", "b.wav", "c.PNG", "d.pdf", "e.zip", "f.xyz", "g")],
                         ["video", "audio", "image", "document", "archive", "other", "other"])

    def test_records_are_ordered_and_cleaned(self):
        good = log.new_record(1, "a.mp4", "C:/d/a.mp4", "example.com", now=100.0, state="done")
        later = log.new_record(2, "b.mp4", "C:/d/b.mp4", "", now=200.0, state="active")
        junk = [None, 3, {"name": "x"}, {"id": "1", "name": "", "path": "p", "time": 1},
                {"id": "z", "name": "z", "path": "p", "time": "soon"}]
        cleaned = log.clean_history(junk + [later, good])
        self.assertEqual([r["name"] for r in cleaned], ["a.mp4", "b.mp4"])         # oldest first
        self.assertEqual(cleaned[1]["state"], "failed")                          # Buddy closed mid-download
        self.assertEqual(log.clean_history("nope"), [])

    def test_only_finished_non_private_downloads_are_written_down(self):
        done = dict(log.new_record(1, "a.mp4", "p", "s", now=1.0), state="done")
        private = dict(log.new_record(2, "b.mp4", "p2", "s", now=2.0), state="done", private=True)
        active = log.new_record(3, "c.mp4", "p3", "s", now=3.0)
        self.assertEqual([r["name"] for r in log.save([done, private, active])], ["a.mp4"])
        self.assertNotIn("private", log.save([done])[0])

    def test_the_history_is_capped_but_never_drops_a_download_in_progress(self):
        records = [dict(log.new_record(i, f"{i}.bin", "p", "s", now=float(i)), state="done")
                   for i in range(1, log.MAX_HISTORY + 6)]
        records.insert(0, log.new_record(999, "running.bin", "p", "s", now=0.5))
        kept = log.trim(records)
        self.assertEqual(len([r for r in kept if r["state"] == "done"]), log.MAX_HISTORY)
        self.assertIn("running.bin", [r["name"] for r in kept])
        self.assertNotIn("1.bin", [r["name"] for r in kept])

    def test_what_the_window_is_shown(self):
        record = dict(log.new_record(1, "clip.mov", os.path.join("D:\\", "dl", "clip.mov"), "vimeo.com", now=5.0),
                      state="done", size=1536, resolve=True)
        shown = log.shown(record, True)
        self.assertEqual((shown["kind"], shown["sizeText"], shown["exists"], shown["resolve"]),
                         ("video", "1.5 KB", True, True))
        self.assertFalse(log.shown(record, False)["exists"])
        self.assertFalse(log.shown(dict(record, state="active"), True)["exists"])      # not yet a file to use
        self.assertEqual(log.shown(dict(record, state="active"), False, 0.5)["progress"], 0.5)


class Clip:
    def __init__(self, name, path):
        self.name, self.path = name, path

    def GetName(self):
        return self.name

    def GetClipProperty(self, key):
        return self.path if key == "File Path" else ""


class Folder:
    def __init__(self, name, clips=(), subs=()):
        self.name, self.clips, self.subs = name, list(clips), list(subs)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subs

    def GetUniqueId(self):
        return id(self)


class Pool:
    def __init__(self):
        self.root = Folder("Master")
        self.current = self.root

    def GetRootFolder(self):
        return self.root

    def GetCurrentFolder(self):
        return self.current

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def AddSubFolder(self, parent, name):
        made = Folder(name)
        parent.subs.append(made)
        return made

    def ImportMedia(self, paths):
        made = [Clip(os.path.basename(p), p) for p in paths]
        self.current.clips.extend(made)
        return made

    def MoveClips(self, clips, target):
        for folder in (self.root, *self.root.subs):
            folder.clips = [c for c in folder.clips if c not in clips]
        target.clips.extend(clips)
        return True


class Controller:
    def __init__(self, pool):
        project = type("Project", (), {"GetMediaPool": lambda s: pool})()
        self.current_project = lambda: project


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.pool = Pool()
        self.controller = Controller(self.pool)

    def test_send_makes_the_bin_once_and_leaves_the_open_bin_alone(self):
        shots = Folder("Shots")
        self.pool.root.subs.append(shots)
        self.pool.current = shots
        self.assertEqual(resolve_ext.send_to_bin(self.controller, ["C:/d/a.mp4"]), 1)
        self.assertEqual(resolve_ext.send_to_bin(self.controller, ["C:/d/b.mp4"]), 1)
        bins = [f for f in self.pool.root.subs if f.name == "Downloads"]
        self.assertEqual(len(bins), 1)
        self.assertEqual([c.name for c in bins[0].clips], ["a.mp4", "b.mp4"])
        self.assertIs(self.pool.current, shots)
        self.assertEqual(shots.clips, [])

    def test_a_dragged_file_is_moved_in_once_resolve_has_it(self):
        path = "C:/d/clip.mp4"
        self.assertFalse(resolve_ext.file_into_bin(self.controller, path))        # not imported yet
        self.assertEqual([f.name for f in self.pool.root.subs], [])               # and no bin made for nothing
        self.pool.ImportMedia([path])                                             # Resolve takes the drop
        self.assertTrue(resolve_ext.file_into_bin(self.controller, path))
        downloads = self.pool.root.subs[0]
        self.assertEqual((downloads.name, [c.name for c in downloads.clips], self.pool.root.clips),
                         ("Downloads", ["clip.mp4"], []))
        self.assertTrue(resolve_ext.file_into_bin(self.controller, path))         # already there: nothing more
        self.assertEqual(len(downloads.clips), 1)

    def test_a_file_dropped_on_another_bin_is_found_there(self):
        other = Folder("B-roll", [Clip("x.mov", "C:/d/x.mov")])
        self.pool.root.subs.append(other)
        self.assertTrue(resolve_ext.file_into_bin(self.controller, "C:/d/x.mov"))
        self.assertEqual(other.clips, [])

    def test_a_clip_of_the_same_name_from_elsewhere_is_left_alone(self):
        self.pool.root.clips.append(Clip("x.mov", "E:/other/x.mov"))
        self.assertFalse(resolve_ext.file_into_bin(self.controller, "C:/d/x.mov"))
        self.assertEqual(len(self.pool.root.clips), 1)

    def test_no_project_open(self):
        none = type("C", (), {"current_project": lambda s: None})()
        with self.assertRaises(Exception):
            resolve_ext.send_to_bin(none, ["a"])


try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class BrowserDownloadsTests(unittest.TestCase):
    """The browser keeping the list - on a throwaway profile and settings."""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="buddy_dl_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from pages.web import engine, filter_lists
        import pages.web.page as wp
        from pages.web import browser as b
        from test_settings_pages import Mem
        self.wp = wp
        if engine._profile is None:
            engine.PROFILE_DIR = os.path.join(self.tmp, "profile")
        self.saved = Mem(dict(b.DEFAULTS))
        self.saved["downloads"] = self.tmp
        lists = mock.patch.object(filter_lists.FilterLists, "start")
        lists.start()
        self.addCleanup(lists.stop)
        host = mock.Mock(spec=["tool_settings", "theme_tokens", "shared_settings", "links", "ensure_connected"])
        host.tool_settings.return_value = self.saved
        host.theme_tokens.side_effect = lambda: __import__("core.theme", fromlist=["x"]).get_theme_tokens("Resolve")
        host.shared_settings = {}
        host.links = []
        self.host = host

    def page(self):
        page = self.wp.WebBrowserPage(self.host)
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.ducker.stop)
        return page

    def file(self, name, size=10):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path

    def test_history_comes_back_and_is_saved(self):
        done = dict(log.new_record(1, "a.mp4", os.path.join(self.tmp, "a.mp4"), "example.com", now=50.0),
                    state="done", size=10)
        self.saved["download_history"] = [done]
        page = self.page()
        self.assertEqual([r["name"] for r in page.history], ["a.mp4"])
        view = page.downloads_view()
        self.assertEqual([i["name"] for i in view["items"]], ["a.mp4"])
        self.assertFalse(view["items"][0]["exists"])                          # the file isn't there
        page._save()
        self.assertEqual([r["name"] for r in self.saved["download_history"]], ["a.mp4"])

    def test_remove_clear_and_delete(self):
        names = ("a.bin", "b.bin", "c.bin")
        self.saved["download_history"] = [dict(log.new_record(i, n, self.file(n), "s", now=10.0 + i),
                                               state="done", size=10) for i, n in enumerate(names)]
        page = self.page()
        ids = {r["name"]: r["id"] for r in page.history}
        page.remove_downloads([ids["a.bin"]])
        self.assertEqual([r["name"] for r in page.history], ["b.bin", "c.bin"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a.bin")))      # the file stays
        with mock.patch.object(self.wp, "confirm", return_value=False), \
                mock.patch.object(self.wp.recycle, "to_recycle_bin") as bin_:
            page.delete_downloads([ids["b.bin"]])
            bin_.assert_not_called()
            self.assertEqual(len(page.history), 2)
        with mock.patch.object(self.wp, "confirm", return_value=True), \
                mock.patch.object(self.wp.recycle, "to_recycle_bin") as bin_:
            page.delete_downloads([ids["b.bin"]])
            bin_.assert_called_once_with([os.path.join(self.tmp, "b.bin")])
        self.assertEqual([r["name"] for r in page.history], ["c.bin"])
        page.clear_downloads()
        self.assertEqual(page.history, [])

    def test_send_to_resolve_marks_the_record(self):
        path = self.file("a.mp4", 1)
        self.saved["download_history"] = [dict(log.new_record(1, "a.mp4", path, "s", now=1.0), state="done", size=1)]
        page = self.page()
        self.host.ensure_connected.return_value = object()
        with mock.patch.object(self.wp.resolve_ext, "send_to_bin", return_value=1) as send:
            page.send_downloads([page.history[0]["id"]])
            send.assert_called_once()
        self.assertTrue(page.history[0]["resolve"])
        self.assertTrue(page.downloads_view()["items"][0]["resolve"])

    def test_a_file_dragged_onto_resolve_is_filed_when_it_turns_up(self):
        path = self.file("a.mp4", 1)
        self.saved["download_history"] = [dict(log.new_record(1, "a.mp4", path, "s", now=1.0), state="done", size=1)]
        page = self.page()
        self.host.ensure_connected.return_value = object()
        answers = iter([False, False, True])
        with mock.patch.object(self.wp.resolve_ext, "file_into_bin", side_effect=lambda c, p: next(answers)), \
                mock.patch.object(self.wp.QTimer, "singleShot", lambda ms, fn: None):
            page._file_in_resolve(page.history)
            for _ in range(3):
                page._file_step()
        self.assertEqual(page._filing, {})
        self.assertTrue(page.history[0]["resolve"])

    def test_opening_a_program_asks_first(self):
        records = []
        for i, name in enumerate(("setup.exe", "clip.mp4")):
            records.append(dict(log.new_record(i, name, self.file(name, 1), "s", now=1.0 + i), state="done", size=1))
        self.saved["download_history"] = records
        page = self.page()
        ids = [r["id"] for r in page.history]
        with mock.patch.object(self.wp.QDesktopServices, "openUrl") as opened, \
                mock.patch.object(self.wp, "confirm", return_value=False) as ask:
            page.open_downloads(ids)
            ask.assert_called_once()
            self.assertIn("setup.exe", ask.call_args[0][2])
            self.assertEqual([c[0][0].fileName() for c in opened.call_args_list], ["clip.mp4"])   # the video still opens
        with mock.patch.object(self.wp.QDesktopServices, "openUrl") as opened, \
                mock.patch.object(self.wp, "confirm", return_value=True):
            page.open_downloads(ids)
            self.assertEqual(sorted(c[0][0].fileName() for c in opened.call_args_list), ["clip.mp4", "setup.exe"])
        with mock.patch.object(self.wp.QDesktopServices, "openUrl") as opened, \
                mock.patch.object(self.wp, "confirm") as ask:
            page.open_downloads(ids[1:])                                              # no program: no question
            ask.assert_not_called()
            self.assertEqual(opened.call_count, 1)

    def test_it_gives_up_when_resolve_never_has_it(self):
        path = self.file("a.mp4", 1)
        self.saved["download_history"] = [dict(log.new_record(1, "a.mp4", path, "s", now=1.0), state="done", size=1)]
        page = self.page()
        self.host.ensure_connected.return_value = object()
        with mock.patch.object(self.wp.resolve_ext, "file_into_bin", return_value=False), \
                mock.patch.object(self.wp.QTimer, "singleShot", lambda ms, fn: None):
            page._file_in_resolve(page.history)
            for _ in range(self.wp.FILE_TRIES):
                page._file_step()
        self.assertEqual(page._filing, {})
        self.assertFalse(page.history[0]["resolve"])


if __name__ == "__main__":
    unittest.main()
