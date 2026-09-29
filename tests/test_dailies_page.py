"""Dailies' page: what it sends the view on each action, the stills it keeps,
and the preview never taking the previous clip's frame for the next one's."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from core import ffmpeg_log

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtMultimedia import QVideoFrame
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    from pages.asset_manager.player import MediaPreview
    from pages.dailies.page import DailiesPage
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


class Settings(dict):
    def save(self):
        pass


class Host:
    connected = False
    controller = None
    shared_settings = {"theme": "Resolve"}

    def tool_settings(self, _tool, defaults):
        return Settings(defaults)

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")


def catalog(count=3):
    clips = {}
    for n in range(count):
        cid = f"c{n}"
        clips[cid] = {"id": cid, "name": f"Clip {n}", "bin": "Master", "bin_id": "root",
                      "path": f"C:/nowhere/{cid}.mov", "type": "Video", "duration": "00:00:10:00",
                      "metadata": {"Comments": "", "Tag": "0"}}
    return {"project_id": "p1", "project_name": "Film", "current_bin": "Master",
            "current_bin_id": "root", "current_ids": list(clips), "clips": clips, "objects": {}}


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageMessageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self.page = DailiesPage(Host())
        self.addCleanup(delete, self.page)
        self.addCleanup(self.page.on_app_quitting)   # runs first: ends its threads
        self.events = []
        self.page.emit = lambda name, value=None: self.events.append((name, value))
        self.page.catalog = catalog()
        self.page.current_id = "c0"
        self.page._push()
        self.events.clear()

    def sent(self, name):
        return [value for event, value in self.events if event == name]

    def test_picking_a_clip_sends_no_tape_and_no_catalog(self):
        self.page.on_pick_clip({"id": "c1"})
        names = [name for name, _value in self.events]
        self.assertNotIn("tape", names)
        self.assertNotIn("catalog", names)
        self.assertEqual(names.count("current"), 1)
        self.assertEqual(self.sent("state")[-1]["current"], "c1")
        self.assertNotIn("clips", self.sent("state")[-1])

    def test_nothing_is_resent_when_nothing_changed(self):
        self.page._push()
        self.assertEqual(self.events, [])

    def test_a_clip_seen_before_comes_back_with_its_still(self):
        self.page._on_frame("c0", "data:image/jpeg;base64,AAA")
        self.page.on_pick_clip({"id": "c1"})
        self.assertEqual(self.sent("current")[-1]["frame"], "")
        self.page.on_pick_clip({"id": "c0"})
        self.assertEqual(self.sent("current")[-1]["frame"], "data:image/jpeg;base64,AAA")

    def test_a_still_decoded_again_is_not_sent_again(self):
        self.page._on_frame("c0", "data:image/jpeg;base64,AAA")
        self.page._on_frame("c0", "data:image/jpeg;base64,AAA")
        self.assertEqual(len(self.sent("frame")), 1)

    def test_catalog_only_on_request(self):
        self.page.on_catalog(None)
        self.assertEqual(sorted(c["id"] for c in self.sent("catalog")[-1]), ["c0", "c1", "c2"])

    def test_sources_carry_counts_not_every_clip_id(self):
        self.page.on_create_source({"name": "Day 1", "scope": "all"})
        source = self.sent("state")[-1]["sources"][0]
        self.assertEqual((source["name"], source["count"]), ("Day 1", 3))
        self.assertNotIn("ids", source)

    def test_the_tape_carries_each_clips_length_and_colour(self):
        self.page.catalog["clips"]["c1"]["seconds"] = 12.5
        self.page.catalog["clips"]["c1"]["metadata"]["Clip Color"] = "Teal"
        self.page._push_tape()
        rows = {row["id"]: row for row in self.sent("tape")[-1]["clips"]}
        self.assertEqual((rows["c1"]["seconds"], rows["c1"]["color"]), (12.5, "Teal"))
        # A colour picked in the notes shows on the tape before it's applied.
        self.page.on_edit({"id": "c1", "color": "Pink"})
        rows = {row["id"]: row for row in self.sent("tape")[-1]["clips"]}
        self.assertEqual(rows["c1"]["color"], "Pink")
        self.page.on_edit({"id": "c1", "color": "Not a colour"})
        self.assertEqual(self.page._drafts()["c1"]["color"], "Pink")

    def test_seeking_into_another_clip_opens_it_and_moves_there_once_ready(self):
        with mock.patch.object(self.page.preview, "seek_ms") as seek_ms:
            self.page.on_seek_to({"id": "c2", "ms": 4000})
            self.assertEqual(self.page.current_id, "c2")
            self.assertEqual(self.page._pending_seek, ("c2", 4000.0))
            seek_ms.assert_not_called()
            self.page._on_ready("c2", "video")
            seek_ms.assert_called_once_with(4000.0, "c2")
            # Within the clip that's open, it's just a seek.
            self.page.on_seek_to({"id": "c2", "ms": 1500})
            seek_ms.assert_called_with(1500.0, "c2")
        self.assertEqual(self.page.current_id, "c2")

    def test_seek_to_a_clip_not_on_the_tape_is_ignored(self):
        self.page.on_seek_to({"id": "elsewhere", "ms": 10})
        self.assertEqual(self.page.current_id, "c0")

    def test_viewer_choice_is_kept(self):
        self.page.on_viewer({"viewer": "source"})
        self.assertEqual(self.sent("state")[-1]["viewer"], "source")
        self.assertEqual(self.page.settings["viewer"], "source")
        self.page.on_viewer({"viewer": "sideways"})
        self.assertEqual(self.page.settings["viewer"], "source")

    def test_a_clip_with_no_file_says_so_instead_of_transcribing(self):
        self.page.on_pick_clip({"id": "c1"})
        transcript = self.sent("transcript")[-1]
        self.assertEqual((transcript["id"], transcript["status"]), ("c1", "error"))
        self.assertIsNone(self.page._transcript_job)

    def test_playing_on_into_the_next_clip_moves_the_page_not_the_player(self):
        self.page.auto_play = True
        with mock.patch.object(self.page.preview, "stop") as stop,                 mock.patch.object(self.page.preview, "open") as open_:
            self.page._on_ended("c0", "c1")
        self.assertEqual((self.page.current_id, self.page.ready_id), ("c1", "c1"))
        stop.assert_not_called()
        open_.assert_not_called()
        self.assertEqual(self.page._surface_clip, "c1")     # the surface stays up
        self.assertEqual(self.sent("state")[-1]["current"], "c1")

    def test_a_player_that_went_somewhere_unexpected_is_stopped(self):
        self.page.auto_play = True
        with mock.patch.object(self.page.preview, "stop") as stop,                 mock.patch.object(self.page, "_advance") as advance:
            self.page._on_ended("c0", "c2")     # not the clip after c0
        stop.assert_called()
        advance.assert_called_once()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TapePlayerTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtMultimedia import QVideoSink
        from pages.dailies.tape_player import TapePlayer
        self.app = QApplication.instance() or QApplication([])

        class Surface(QObject):
            drawn = Signal(QVideoFrame)
        self.surface = Surface()
        self.sink = QVideoSink()
        self.player = TapePlayer(None, self.sink, self.surface.drawn)
        self.addCleanup(self.player.shutdown)
        self.ready = []
        self.player.ready.connect(lambda clip_id, kind: self.ready.append(clip_id))

    def test_the_waiting_clip_is_taken_over_not_reopened(self):
        self.player.preload("next", "C:/nowhere/next.mov", "Video")
        waiting = self.player.spare
        waiting.state = "ready"                 # as if paused on its first frame
        self.player.open("next", "C:/nowhere/next.mov", "Video", play=False)
        self.assertIs(self.player.active, waiting)
        self.assertIsNone(self.player.spare)
        self.assertEqual(self.ready, ["next"])
        self.assertTrue(waiting.attached)       # showing its frame on the surface

    def test_a_different_clip_gets_a_fresh_deck(self):
        self.player.preload("next", "C:/nowhere/next.mov", "Video")
        waiting = self.player.spare
        self.player.open("other", "C:/nowhere/other.mov", "Video")
        self.assertIsNot(self.player.active, waiting)
        self.assertEqual(self.player.active.clip_id, "other")
        self.assertIs(self.player.spare, waiting)   # still waiting for its turn

    def test_a_retired_deck_reports_nothing(self):
        self.player.open("one", "C:/nowhere/one.mov", "Video")
        deck = self.player.active
        self.player.stop()
        self.assertEqual(deck.state, "retired")
        self.player._on_status(deck, deck.player.mediaStatus())
        self.player._on_error(deck, "late error")
        self.assertEqual(self.ready, [])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TranscriptCacheTests(unittest.TestCase):
    def test_a_transcript_is_kept_for_that_exact_file(self):
        from pages.dailies import transcripts
        with tempfile.TemporaryDirectory() as folder:
            media = os.path.join(folder, "clip.mov")
            with open(media, "wb") as handle:
                handle.write(b"one")
            cache = os.path.join(folder, "cache")
            self.assertIsNone(transcripts.cached(media, cache))
            transcripts.save(media, {"segments": [{"start": 0, "end": 1, "text": "Hi"}]}, cache)
            self.assertEqual(transcripts.cached(media, cache)["segments"][0]["text"], "Hi")
            with open(media, "ab") as handle:
                handle.write(b"changed")   # re-recorded / replaced: a different file
            self.assertIsNone(transcripts.cached(media, cache))

    def test_no_engine_installed_is_explained(self):
        from pages.dailies import transcripts
        from pathlib import Path
        with mock.patch("pages.transcribe.env_setup.venv_python", return_value=Path("C:/no/such/python.exe")):
            plan, why = transcripts.engine_plan()
        self.assertIsNone(plan)
        self.assertIn("Transcribe", why)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PreviewStillTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.preview = MediaPreview()
        self.addCleanup(delete, self.preview)
        self.frames = []
        self.preview.frame.connect(lambda clip_id, _src: self.frames.append(clip_id))
        image = QImage(64, 36, QImage.Format_RGB32)
        image.fill(QColor("red"))
        self.frame = QVideoFrame(image)

    def test_a_frame_before_the_clip_starts_priming_is_the_previous_clips(self):
        # show() has named the next clip, but its priming play hasn't begun:
        # a frame now is the old clip's, still in the pipeline.
        self.preview._pending_id, self.preview._kind = "next", "video"
        self.preview._on_video_frame(self.frame)
        self.assertEqual(self.frames, [])
        self.preview._priming_id = "next"
        self.preview._on_video_frame(self.frame)
        self.assertEqual(self.frames, ["next"])


class FfmpegLogTests(unittest.TestCase):
    def test_finds_windows_and_mac_layouts(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "Qt", "lib"))
            for name in ("avutil-59.dll", os.path.join("Qt", "lib", "libavutil.59.dylib"), "avcodec-61.dll"):
                open(os.path.join(folder, name), "w").close()
            found = [os.path.basename(p) for p in ffmpeg_log.avutil_candidates(folder)]
        self.assertEqual(sorted(found), ["avutil-59.dll", "libavutil.59.dylib"])

    def test_left_alone_when_debugging_playback(self):
        with mock.patch("ctypes.CDLL") as cdll:
            self.assertIsNone(ffmpeg_log.silence({"QT_FFMPEG_DEBUG": "1"}))
        cdll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
