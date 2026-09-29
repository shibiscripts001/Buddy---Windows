"""Project Setup: the bin list rules, the folder import, the Sync tab's
report wording (all Qt-free), and the web page driven against a fake
Media Pool - never a real Resolve, and never the real
~/.resolve_bin_generator settings."""

import os
import json
import tempfile
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.project_setup import bins, importer, sync_report

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


# ---------------------------------------------------------- fake Resolve --

class Folder:
    def __init__(self, name, parent=None):
        self.name, self.parent, self.subs, self.clips = name, parent, [], []

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return f"id-{id(self)}"

    def GetSubFolderList(self):
        return list(self.subs)

    def GetClipList(self):
        return list(self.clips)

    def path(self):
        return (self.parent.path() + "/" if self.parent else "") + self.name


class Clip:
    def __init__(self, name, kind="Video", path=None, tc="19:45:12:08"):
        self.name, self.kind = name, kind
        self.path = path
        self.tc = tc
        self.linked = []

    def GetClipProperty(self, key=None):
        props = {"Type": self.kind, "Clip Name": self.name, "Start TC": self.tc}
        if self.path is not None:
            props["File Path"] = self.path
        return props if key is None else props.get(key)

    def GetUniqueId(self):
        return f"clip-{self.name}"

    def GetName(self):
        return self.name

    def LinkProxyMedia(self, path):
        if self.kind == "Timeline" or self.path is None:
            return False
        self.linked.append(path)
        return True


class Pool:
    def __init__(self):
        self.root = Folder("Master")
        self.current = self.root
        self.refuse = set()
        self.imported = []
        self.selected = []

    def GetSelectedClips(self):
        return self.selected

    def GetRootFolder(self):
        return self.root

    def GetCurrentFolder(self):
        return self.current

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def AddSubFolder(self, parent, name):
        if name in self.refuse:
            return None
        folder = Folder(name, parent)
        parent.subs.append(folder)
        return folder

    def ImportMedia(self, paths):
        self.imported.append((self.current.path(), sorted(os.path.basename(p) for p in paths)))
        clips = [Clip(os.path.basename(p)) for p in paths]
        self.current.clips.extend(clips)
        return clips

    def AppendToTimeline(self, clips):
        return list(clips)

    def CreateTimelineFromClips(self, name, clips):
        self.created_timeline = (name, len(clips))
        return object()

    def tree(self, folder=None, depth=0):
        folder = folder or self.root
        out = []
        for sub in folder.subs:
            out.append(("  " * depth) + sub.name)
            out.extend(self.tree(sub, depth + 1))
        return out


class Timeline:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return "tl-" + self.name

    def GetTrackCount(self, kind):
        return 1


class Project:
    def __init__(self, pool):
        self.pool, self.timeline = pool, None

    def GetUniqueId(self):
        return f"project-{id(self)}"

    def GetMediaPool(self):
        return self.pool

    def GetCurrentTimeline(self):
        return self.timeline


class ProjectManager:
    def __init__(self, project):
        self.project = project

    def GetCurrentProject(self):
        return self.project


class ResolveApp:
    def __init__(self, project):
        self.pm = ProjectManager(project)

    def GetProjectManager(self):
        return self.pm


class ShellController:
    def __init__(self):
        self.pool = Pool()
        self.project = Project(self.pool)
        self.resolve = ResolveApp(self.project)


def logger():
    lines = []
    return lines, lambda text, kind="info": lines.append((kind, text))


# ------------------------------------------------------------------ rules --

class BinRuleTests(unittest.TestCase):
    def test_nesting(self):
        rows = bins.plan("01_Timelines\n>1080p\n>>15sec\n>4K\n\n  02_Footage \n>Ronin")
        self.assertEqual([(r["name"], r["depth"]) for r in rows],
                         [("01_Timelines", 0), ("1080p", 1), ("15sec", 2), ("4K", 1), ("02_Footage", 0), ("Ronin", 1)])
        self.assertEqual([r["children"] for r in rows], [2, 1, 0, 0, 1, 0])
        self.assertFalse(any(r["adjusted"] for r in rows))

    def test_too_deep_is_nested_as_deep_as_it_can_go(self):
        rows = bins.plan(">>>Orphan\nTop\n>>>Deep")
        self.assertEqual([(r["depth"], r["adjusted"]) for r in rows], [(0, True), (0, False), (1, True)])
        self.assertEqual(bins.plan(">  \n\n"), [])

    def test_create_builds_the_tree_and_survives_a_refusal(self):
        pool = Pool()
        pool.refuse = {"1080p"}
        lines, log = logger()
        created, failed = bins.create(pool, bins.plan("A\n>1080p\n>>15sec\nB"), log)
        self.assertEqual((created, failed), (3, 1))
        # 15sec had no parent to go in, so it went in A instead.
        self.assertEqual(pool.tree(), ["A", "  15sec", "B"])
        self.assertIn("1080p", lines[0][1])


class ImportRuleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "Shoot Day")
        files = ["A001.mov", "notes.txt", "Sound/take1.wav", "Sound/Empty/.keep",
                 "Plates/p_0001.exr", "Plates/p_0002.exr", "Plates/p_0003.exr", "Plates/still.jpg",
                 "Plates/x_1.png", "Plates/x_2.png"]
        for rel in files:
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()

    def test_sequences_count_once(self):
        self.assertEqual(importer.count_items(["p_0001.exr", "p_0002.exr", "p_0003.exr", "a.mov", "b.wav", "x_1.png", "x_2.png"]),
                         {"video": 1, "audio": 1, "image": 2, "sequence": 1})

    def test_scan(self):
        s = importer.scan(self.root)
        self.assertEqual((s["folders"], s["video"], s["audio"], s["sequence"], s["image"], s["skipped"], s["partial"]),
                         (3, 1, 1, 1, 3, 2, False))
        self.assertEqual(importer.item_total(s), 6)

    def test_import_mirrors_the_tree(self):
        pool = Pool()
        _lines, log = logger()
        result = importer.import_folder(pool, self.root, pool.root, log)
        self.assertEqual(result["bin"], "Shoot Day")
        self.assertEqual(pool.tree(), ["Shoot Day", "  Plates", "  Sound", "    Empty"])
        self.assertEqual(pool.imported[0], ("Master/Shoot Day", ["A001.mov"]))   # notes.txt skipped
        self.assertEqual(result["imported"], 1 + 6 + 1)

    def test_a_refused_top_bin_imports_nothing(self):
        pool = Pool()
        pool.refuse = {"Shoot Day"}
        lines, log = logger()
        self.assertIsNone(importer.import_folder(pool, self.root, pool.root, log))
        self.assertEqual(pool.imported, [])
        self.assertEqual(lines[0][0], "error")


class ReportTests(unittest.TestCase):
    def test_assemble(self):
        lines = sync_report.assemble({"aligned": 4, "total": 5, "audio_total": 2, "audio_matched": 1},
                                     "timecode", 3, 6, "Day 1 (Synced)", ["one clip had no media"])
        texts = [t for t, _k in lines]
        self.assertEqual(texts[0], "Aligned 4 of 5 clip(s) by timecode.")
        self.assertIn("Placed 1 of 2", texts[1])
        self.assertIn("3 video and 6 audio", texts[2])
        self.assertEqual(lines[3], ('Created "Day 1 (Synced)" – the timeline you started from is unchanged.', "success"))
        self.assertEqual(lines[-1], ("Warning: one clip had no media", "warn"))

    def test_shift_and_external(self):
        texts = [t for t, _ in sync_report.shift({"lead_in_removed": 0, "clips": 3, "gap_frames_removed": 0,
                                                  "gaps": 0, "name": "T"}, close_gaps=True)]
        self.assertEqual(texts[:2], ["3 clip(s) already started at the beginning.", "There were no gaps between clips to close."])
        lines = sync_report.external({"matched": 2, "external": 3, "scratch_used": 4, "sessions": 2,
                                      "unmatched": 1, "name": "T"}, [])
        self.assertIn("across 2 separate stretches", lines[0][0])
        self.assertEqual(lines[1][1], "warn")


# ------------------------------------------------------------------ page --

class Host:
    def __init__(self):
        self.controller, self.connected = ShellController(), True
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
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patch = mock.patch("pages.project_setup.data_manager.SETTINGS_DIR", self._tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        from pages.project_setup.page import ProjectSetupPage
        self.host = Host()
        self.pool = self.host.controller.pool
        self.page = ProjectSetupPage(self.host)
        self.page._poll.stop()
        self.addCleanup(delete, self.page)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def logs(self, tab):
        return [e["text"] for e in [p for n, p in self.events if n == "log" and p["tab"] == tab][-1]["entries"]]

    def test_settings_stay_in_the_scratch_folder(self):
        self.page.on_bins_text({"text": "X\n>Y"})
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, "settings.json")))
        self.assertEqual(self.last("bins")["rows"][1]["depth"], 1)

    def test_create_bins(self):
        self.page.on_bins_text({"text": "01_Footage\n>A-Cam\n>B-Cam\n02_Audio"})
        self.page.on_create_bins(None)
        self.assertEqual(self.pool.tree(), ["01_Footage", "  A-Cam", "  B-Cam", "02_Audio"])
        self.assertEqual(self.logs("bins")[-1], "Created 4 bins in the Media Pool.")
        self.assertEqual(self.host.busy, [True, False])

    def test_populate_follows_the_open_bin_and_timeline(self):
        cams = self.pool.AddSubFolder(self.pool.root, "Cams")
        cams.clips = [Clip("a"), Clip("b"), Clip("TL", kind="Timeline")]
        sub = self.pool.AddSubFolder(cams, "Extra")
        sub.clips = [Clip("c")]
        self.pool.current = cams
        self.page.on_tab({"tab": "populate"})
        pop = self.last("populate")
        self.assertEqual((pop["bin"], pop["count"], pop["timeline"]), ("Cams", 3, None))
        self.page.on_populate_recursive({"on": False})
        self.assertEqual(self.last("populate")["count"], 2)
        self.page.on_add_to_timeline(None)
        self.assertEqual(self.pool.created_timeline, ("Cams", 2))
        self.host.controller.project.timeline = Timeline("Edit")
        self.page.on_add_to_timeline(None)
        self.assertIn("to the end of the timeline", self.logs("populate")[-1])

    def test_import_needs_a_folder_and_lands_in_the_open_bin(self):
        self.page.on_import_folder(None)
        self.assertEqual(self.last("alert")["title"], "Choose a folder first")
        folder = os.path.join(self._tmp.name, "Card A")
        os.makedirs(folder)
        open(os.path.join(folder, "C0001.MP4"), "w").close()
        self.page.import_folder = folder
        self.page.on_rescan(None)
        self.assertEqual(self.last("import")["summary"]["items"], 1)
        day = self.pool.AddSubFolder(self.pool.root, "Day 1")
        self.pool.current = day
        self.page.on_tab({"tab": "import"})
        self.assertEqual(self.last("import")["destination"], "Day 1")
        self.page.on_import_folder(None)
        self.assertEqual(self.pool.tree(), ["Day 1", "  Card A"])
        self.assertIs(self.pool.current, day.subs[0])     # the new bin is left open
        self.page.on_to_master({"on": True})
        self.page.on_import_folder(None)
        self.assertEqual(self.pool.tree()[-1], "Card A")

    def test_a_worker_job_reports_progress_and_can_be_cancelled(self):
        from pages.project_setup import otio_engine

        def slow_collapse(document, delete_silent_audio, ffmpeg_path, progress_cb):
            for i in range(200):
                progress_cb("Checking audio", i, 200)
                time.sleep(0.01)
            return document, 1, 0, []

        self.host.controller.project.timeline = Timeline("Day 1")
        with mock.patch.object(otio_engine, "read_current_timeline", return_value=({}, [], self._tmp.name)), \
                mock.patch.object(otio_engine, "collapse_document_from", side_effect=slow_collapse), \
                mock.patch("pages.project_setup.resolve_ext.ProjectSetupController.get_current_timeline",
                           return_value=mock.Mock(**{"GetName.return_value": "Day 1", "GetTrackCount.return_value": 4})):
            self.page.on_collapse(None)
            self.assertEqual(self.last("job")["action"], "collapse")
            self.assertTrue(self.last("state")["busy"])
            self.page._dispatch("create_bins", None)          # refused while the job runs
            self.assertEqual(self.pool.tree(), [])
            deadline = time.monotonic() + 5
            while not any(n == "job" and p and p["total"] == 200 for n, p in self.events) and time.monotonic() < deadline:
                self.app.processEvents()
            self.page.on_cancel_job(None)
            while self.page._worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
        self.assertIsNone(self.last("job"))
        self.assertEqual(self.logs("sync")[-1], "Cancelled – nothing was changed.")
        self.assertFalse(self.last("state")["busy"])

    def test_sync_without_a_timeline_says_so(self):
        self.page.on_tab({"tab": "sync"})
        self.assertIn("No timeline is open", self.last("sync")["error"])
        self.page.on_align(None)
        self.assertEqual(self.last("alert")["title"], "Couldn't align")
        self.assertEqual(self.host.busy[-1], False)

    def test_sync_options(self):
        self.page.on_sync_option({"key": "method", "value": "waveform"})
        self.page.on_sync_option({"key": "method", "value": "nonsense"})
        self.page.on_sync_option({"key": "close_gaps", "value": True})
        sync = self.last("sync")
        self.assertEqual((sync["method"], sync["close_gaps"]), ("waveform", True))
        self.assertTrue(self.page.data_mgr.settings["shift_close_gaps"])

    def test_bulk_metadata_and_selection_change_guard(self):
        from test_project_metadata import MetadataClip
        a, b, other = MetadataClip("A"), MetadataClip("B"), MetadataClip("Other")
        self.pool.selected = [a, b]
        self.page.on_tab({"tab": "metadata"})
        revision = self.last("metadata")["revision"]
        self.assertEqual(self.last("metadata")["count"], 2)
        self.pool.selected = [other]
        self.page.on_metadata_apply({"revision": revision, "changes": {"Scene": "4"}})
        self.assertIn("selected clips changed", self.last("alert")["text"])
        self.assertEqual(a.writes + b.writes + other.writes, [])
        self.pool.selected = [b, a]  # Same selection in a different order is fine.
        self.page.on_metadata_apply({"revision": revision, "changes": {"Scene": "4"}})
        self.assertEqual((a.meta["Scene"], b.meta["Scene"]), ("4", "4"))
        self.assertEqual(other.writes, [])
        self.page.on_metadata_apply({"revision": revision, "changes": {"Scene": "5"}})
        self.assertEqual(a.meta["Scene"], "4")  # Old/repeated apply messages are refused.

    def test_metadata_rejects_project_change_and_empty_selection(self):
        from test_project_metadata import MetadataClip
        clip = MetadataClip("A")
        self.pool.selected = [clip]
        self.page.on_metadata_load(None)
        revision = self.last("metadata")["revision"]
        with mock.patch.object(Project, "GetUniqueId", return_value="different-project"):
            self.page.on_metadata_apply({"revision": revision, "changes": {"Take": "2"}})
        self.assertEqual(clip.writes, [])
        self.pool.selected = []
        self.page.on_metadata_load(None)
        self.assertEqual(self.last("metadata")["count"], 0)
        self.assertIn("Select one or more", self.last("metadata")["error"])

    def test_remove_gaps_is_offered_after_both_sync_methods_and_uses_shift(self):
        from pages.project_setup import otio_engine
        project = self.host.controller.project
        for method in ("timecode", "waveform"):
            with self.subTest(method=method):
                source = Timeline("Original " + method)
                synced = Timeline("Synced " + method)
                project.timeline = source
                self.page.tab = "sync"
                self.page.sync_options["method"] = method

                def imported(*args):
                    project.timeline = synced
                    return synced

                with mock.patch.object(otio_engine, "read_current_timeline", return_value=({}, [], self._tmp.name)), \
                        mock.patch.object(otio_engine, "assemble_document", return_value=({}, {"aligned": 2, "total": 2}, [])), \
                        mock.patch.object(otio_engine, "import_rebuilt", side_effect=imported):
                    self.page._read_sync(self.page.controller)
                    self.assertFalse(self.last("sync")["can_remove_gaps"])
                    self.page.on_assemble(None)
                    deadline = time.monotonic() + 5
                    while self.page._worker is not None and time.monotonic() < deadline:
                        self.app.processEvents()
                    self.assertIsNone(self.page._worker)
                    self.assertTrue(self.last("sync")["can_remove_gaps"])
                    project.timeline = source
                    self.page._read_sync(self.page.controller)
                    self.assertFalse(self.last("sync")["can_remove_gaps"])
                    with mock.patch.object(otio_engine, "shift_timeline") as shift:
                        self.page.on_remove_gaps(None)
                        shift.assert_not_called()
                    project.timeline = synced
                    stats = {"clips": 2, "lead_in_removed": 0, "gaps": 1, "gap_frames_removed": 20, "name": "Shifted"}
                    with mock.patch.object(otio_engine, "shift_timeline", return_value=(Timeline("Shifted"), stats)) as shift:
                        self.page.on_remove_gaps(None)
                        self.assertTrue(shift.call_args.kwargs["close_gaps"])
                    self.assertFalse(self.last("sync")["can_remove_gaps"])
                    self.assertFalse(self.page.data_mgr.settings.get("shift_close_gaps", False))

    def test_failed_sync_does_not_offer_remove_gaps(self):
        from pages.project_setup import otio_engine
        self.host.controller.project.timeline = Timeline("Original")
        self.page.tab = "sync"
        with mock.patch.object(otio_engine, "read_current_timeline", return_value=({}, [], self._tmp.name)), \
                mock.patch.object(otio_engine, "assemble_document", side_effect=otio_engine.OtioError("No usable audio")):
            self.page.on_assemble(None)
            deadline = time.monotonic() + 5
            while self.page._worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
            self.page._read_sync(self.page.controller)
        self.assertFalse(self.last("sync")["can_remove_gaps"])

    # ------------------------------------------------------------ proxy --

    def test_proxy_tab_reads_bin_and_options_persist(self):
        self.pool.current = self.pool.AddSubFolder(self.pool.root, "Footage")
        self.page.on_tab({"tab": "proxy"})
        state = self.last("proxy")
        self.assertEqual(state["bin"], "Footage")
        self.assertFalse(state["can_select_timeline"])   # the fake Timeline predates 21.0.4
        self.page.on_proxy_option({"key": "scope", "value": "bin"})
        self.page.on_proxy_option({"key": "scope", "value": "nonsense"})   # refused
        self.page.on_proxy_option({"key": "resolution", "value": "quarter"})
        self.page.on_proxy_option({"key": "codec", "value": "prores"})
        self.page.on_proxy_option({"key": "recursive", "value": True})
        self.assertEqual(self.page.data_mgr.settings["proxy_scope"], "bin")
        self.assertEqual(self.page.data_mgr.settings["proxy_resolution"], "quarter")
        self.assertEqual(self.page.data_mgr.settings["proxy_codec"], "prores")
        self.assertTrue(self.page.data_mgr.settings["proxy_recursive"])
        state = self.last("proxy")
        self.assertEqual((state["scope"], state["resolution"], state["codec"], state["recursive"]),
                         ("bin", "quarter", "prores", True))

    def test_proxy_needs_ffmpeg(self):
        with mock.patch.object(self.page, "_ffmpeg", return_value=None):
            self.page.on_proxy_go(None)
        self.assertEqual(self.last("alert")["title"], "ffmpeg needed")
        self.assertEqual([e for e in self.logs("proxy")], ["ffmpeg not found – install it, or set its path in Settings."])

    def test_proxy_run_renders_and_links(self):
        media = os.path.join(self._tmp.name, "media")
        os.makedirs(media)
        open(os.path.join(media, "A001.mp4"), "w").close()
        cams = self.pool.AddSubFolder(self.pool.root, "Cams")
        cams.clips = [Clip("A001", path=os.path.join(media, "A001.mp4")),
                      Clip("sound", kind="Audio", path=os.path.join(media, "t.wav")),
                      Clip("TL", kind="Timeline")]
        self.pool.current = cams
        self.pool.selected = [cams.clips[0]]
        self.page.on_proxy_option({"key": "scope", "value": "selection"})

        rendered = []
        with mock.patch.object(self.page, "_ffmpeg", return_value="ffmpeg.exe"), \
                mock.patch("pages.project_setup.proxy.render_one",
                           side_effect=lambda ff, src, dst, res, codec, run=None, timecode="":
                           rendered.append((dst, timecode)) or dst):
            self.page.on_proxy_go(None)
            deadline = time.monotonic() + 5
            while self.page._worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
        # Stamped with the clip's own start timecode - Resolve won't link one without it.
        self.assertEqual(rendered, [(os.path.join(media, "Proxy", "A001_proxy.mov"), "19:45:12:08")])
        self.assertEqual(cams.clips[0].linked, [os.path.join(media, "Proxy", "A001_proxy.mov")])
        texts = self.logs("proxy")
        self.assertTrue(any("Linked 1 clip to its proxy." == t for t in texts), texts)
        # The audio clip and the timeline pseudo-clip never reached ffmpeg.
        self.assertEqual(len(rendered), 1)

    def _existing_run(self, file_tc, links=True):
        """A run over one clip whose proxy file is already there, with
        file_tc as that file's timecode. Returns (clip, what was rendered)."""
        media = os.path.join(self._tmp.name, "media")
        os.makedirs(os.path.join(media, "Proxy"))
        existing = os.path.join(media, "Proxy", "A001_proxy.mov")
        open(existing, "w").close()
        open(os.path.join(media, "A001.mp4"), "w").close()
        clip = Clip("A001", path=os.path.join(media, "A001.mp4"))
        if not links:
            clip.LinkProxyMedia = lambda path: False
        self.pool.selected = [clip]
        self.page.on_proxy_option({"key": "scope", "value": "selection"})
        rendered = []
        with mock.patch.object(self.page, "_ffmpeg", return_value="ffmpeg.exe"), \
                mock.patch("pages.project_setup.proxy.file_timecode", return_value=file_tc), \
                mock.patch("pages.project_setup.proxy.render_one",
                           side_effect=lambda *a, **k: rendered.append((a[2], k.get("timecode"))) or a[2]):
            self.page.on_proxy_go(None)
            deadline = time.monotonic() + 5
            while self.page._worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
        return clip, rendered, existing

    def test_an_existing_proxy_with_the_wrong_timecode_is_rendered_again(self):
        clip, rendered, existing = self._existing_run("")   # an older Buddy's: no timecode at all
        self.assertEqual(rendered, [(existing, "19:45:12:08")])
        self.assertEqual(clip.linked, [existing])
        texts = self.logs("proxy")
        self.assertIn("1 proxy file already there had the wrong timecode – rendered again.", texts)

    def test_an_existing_proxy_resolve_refuses_is_reported(self):
        _clip, rendered, _existing = self._existing_run("19:45:12:08", links=False)
        self.assertEqual(rendered, [])
        self.assertTrue(any("wouldn't link the proxy file already there" in t for t in self.logs("proxy")),
                        self.logs("proxy"))

    def test_proxy_run_adopts_an_existing_file(self):
        media = os.path.join(self._tmp.name, "media")
        os.makedirs(os.path.join(media, "Proxy"))
        existing = os.path.join(media, "Proxy", "A001_proxy.mov")
        open(existing, "w").close()
        open(os.path.join(media, "A001.mp4"), "w").close()
        clip = Clip("A001", path=os.path.join(media, "A001.mp4"))
        self.pool.selected = [clip]
        self.page.on_proxy_option({"key": "scope", "value": "selection"})
        rendered = []
        with mock.patch.object(self.page, "_ffmpeg", return_value="ffmpeg.exe"), \
                mock.patch("pages.project_setup.proxy.file_timecode", return_value="19:45:12:08"), \
                mock.patch("pages.project_setup.proxy.render_one",
                           side_effect=lambda *a, **k: rendered.append(a) or a[2]):
            self.page.on_proxy_go(None)
            deadline = time.monotonic() + 5
            while self.page._worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
        self.assertEqual(rendered, [])   # nothing rendered: the file was linked as-is
        self.assertEqual(clip.linked, [existing])
        self.assertTrue(any("already had a proxy file" in t for t in self.logs("proxy")), self.logs("proxy"))

    def test_proxy_refuses_empty_selection(self):
        self.pool.selected = []
        with mock.patch.object(self.page, "_ffmpeg", return_value="ffmpeg.exe"):
            self.page.on_proxy_go(None)
        self.assertEqual(self.last("alert")["title"], "No clips to proxy")
        self.assertIn("Select one or more clips", self.last("alert")["text"])


if __name__ == "__main__":
    unittest.main()
