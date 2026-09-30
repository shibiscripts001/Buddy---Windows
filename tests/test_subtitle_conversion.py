"""Subtitle conversion onto a video track that already has clips where the subtitles
go: Resolve hands back no Text+ Buddy can reach there, so the words never landed - the
clips came out blank while Buddy counted them as made (measured on Resolve 21.1). Now
it's refused up front, and a clip whose text couldn't be set isn't counted."""

import types
import unittest

import _paths  # noqa: F401
from pages.text_animator.subtitle_engine import SubtitleData, TextPlusGenerator
from pages.transcribe.resolve_ext import TranscribeController, TranscribeResolveError


class Item:
    def __init__(self, start, end, name=""):
        self.start, self.end, self.name = start, end, name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetName(self):
        return self.name


class Timeline:
    def __init__(self, video, subtitles):
        self.video, self.subtitles = video, subtitles

    def GetTrackCount(self, kind):
        return len(self.video) if kind == "video" else 1

    def GetItemListInTrack(self, kind, index):
        if kind == "subtitle":
            return list(self.subtitles)
        return list(self.video[index - 1]) if index <= len(self.video) else []


def controller(timeline):
    project = types.SimpleNamespace(GetCurrentTimeline=lambda: timeline)
    return TranscribeController(types.SimpleNamespace(resolve=None, current_project=lambda: project))


class OccupiedTrackTests(unittest.TestCase):
    SUBS = [Item(86400, 86405, "A"), Item(86405, 86410, "big"), Item(86616, 86623, "A")]

    def test_a_track_with_clips_where_the_subtitles_go_is_refused(self):
        tl = Timeline([[Item(86400, 87116, "C1867.MP4")], [Item(86600, 86700, "Text+")], []], self.SUBS)
        with self.assertRaises(TranscribeResolveError) as caught:
            controller(tl).subtitles_to_text_plus(1, 2)
        self.assertIn("Video track 2 already has 1 clip(s) where the subtitles go", str(caught.exception))
        self.assertIn("track 3 is", str(caught.exception))

    def test_clips_elsewhere_on_the_track_are_no_obstacle(self):
        tl = Timeline([[Item(86400, 87116)], [Item(86410, 86616), Item(86700, 86800)]], self.SUBS)
        self.assertEqual(TranscribeController._clips_in_the_way(tl, 2, [(s.start, s.end) for s in self.SUBS]), 0)
        self.assertEqual(TranscribeController._clips_in_the_way(tl, 5, [(0, 99999)]), 0)     # a track to come


class CountTests(unittest.TestCase):
    def test_a_clip_without_its_words_isnt_counted_and_is_said(self):
        gen = TextPlusGenerator(None)
        placed = []

        class Pool:
            def AppendToTimeline(self, infos):
                placed.append(Item(infos[0]["recordFrame"], infos[0]["recordFrame"] + 5))
                return [placed[-1]]

        project = types.SimpleNamespace(GetMediaPool=lambda: Pool())
        gen.resolve = types.SimpleNamespace(GetProjectManager=lambda: types.SimpleNamespace(GetCurrentProject=lambda: project))
        gen._ensure_and_target_track = lambda *_a: None
        gen._diagnose_timeline_capabilities = lambda _tl: []
        gen._get_media_pool_text_plus_item = lambda _mp: types.SimpleNamespace(GetClipProperty=lambda _k: "24")
        gen._default_font_for = lambda *_a: "Arial"
        gen._apply_clip_timings = lambda *_a: True
        gen._find_clip_track_index = lambda *_a: 3
        gen._apply_text_and_styling = lambda item, text, **_k: text != "big"       # "big" landed on a taken spot
        subs = [SubtitleData(text=t, start_frame=86400 + 5 * i, end_frame=86405 + 5 * i) for i, t in enumerate(["A", "big", "thank"])]
        tl = Timeline([[], [], []], [])
        made, logs = gen.create_text_plus_clips(tl, subs, target_video_track=3)
        self.assertEqual(len(made), 2)
        fails = [line for line in logs if "[FAIL]" in line]
        self.assertEqual(len(fails), 1)
        self.assertIn("'big'", fails[0])
        self.assertFalse(any("unstyled" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
