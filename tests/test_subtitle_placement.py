"""Putting an SRT on the timeline (app/pages/transcribe/resolve_ext.py
place_subtitles) against a fake Resolve that does what Resolve 21.1 was
measured doing: the first AppendToTimeline on a timeline with anything on
it puts every cue after the end of the timeline's content; placed again,
the cues land where the SRT says. Every placement is checked cue by cue,
redone once if it's off, and never left misplaced. On a switched-off track
it puts nothing at all, so the track is switched on (and unlocked) first.
Resolve appends to its destination subtitle track, which scripts can't set:
when that isn't ST1, what landed elsewhere is taken off and that's said."""

import os
import tempfile
import unittest

import _paths  # noqa: F401
from pages.transcribe.resolve_ext import PLACE_TRIES, TranscribeController, TranscribeResolveError

FPS = 24
START = 86400          # 01:00:00:00
CONTENT_END = START + 720 * FPS   # 12 minutes of video on the timeline


class Item:
    def __init__(self, start):
        self.start = start

    def GetStart(self):
        return self.start


class Timeline:
    def __init__(self, fps="24", has_content=True, always_off=0, enabled=True, locked=False, destination=1,
                 tracks=1, subs=()):
        self.fps, self.subs, self.appends = fps, list(subs), 0
        self.has_content, self.always_off = has_content, always_off
        self.enabled, self.locked = enabled, locked
        self.destination, self.track2 = destination, [Item(START)]   # track 2 has one of its own
        self.tracks = max(tracks, destination)

    def GetIsTrackEnabled(self, kind, index):
        return self.enabled

    def SetTrackEnable(self, kind, index, enabled):
        self.enabled = enabled
        return True

    def GetIsTrackLocked(self, kind, index):
        return self.locked

    def SetTrackLock(self, kind, index, locked):
        self.locked = locked
        return True

    def GetSetting(self, key):
        return self.fps if key == "timelineFrameRate" else None

    def GetStartFrame(self):
        return START

    def GetTrackCount(self, kind):
        return self.tracks

    def AddTrack(self, kind):
        return True

    def GetItemListInTrack(self, kind, index):
        return list(self.subs if index == 1 else self.track2)

    def DeleteClips(self, items):
        self.subs = [s for s in self.subs if s not in items]
        self.track2 = [s for s in self.track2 if s not in items]
        return True

    def place(self, cue_starts):
        """What Resolve does with AppendToTimeline([the SRT's item])."""
        self.appends += 1
        if not self.enabled or self.locked:
            return
        first_time = self.appends == 1
        shift = (CONTENT_END - START) if first_time and self.has_content else 0
        fps = float(self.fps)
        placed = [Item(START + round(t * fps) + shift + self.always_off) for t in cue_starts]
        if self.destination == 2:
            self.track2 += placed
        else:
            self.subs += placed


class MediaPool:
    def __init__(self, timeline, cue_starts):
        self.timeline, self.cue_starts, self.folder = timeline, cue_starts, "Master"

    def GetCurrentFolder(self):
        return self.folder

    def SetCurrentFolder(self, folder):
        self.folder = folder
        return True

    def GetRootFolder(self):
        return type("Root", (), {"GetSubFolderList": lambda s: [], "GetName": lambda s: "Master"})()

    def AddSubFolder(self, parent, name):
        return name

    def ImportMedia(self, paths):
        return ["the SRT's item"]

    def AppendToTimeline(self, items):
        self.timeline.place(self.cue_starts)
        return [True]


class Project:
    def __init__(self, timeline, cue_starts):
        self.timeline, self.pool = timeline, MediaPool(timeline, cue_starts)

    def GetCurrentTimeline(self):
        return self.timeline

    def GetMediaPool(self):
        return self.pool

    def GetSetting(self, key):
        return "24"


def srt(starts):
    def t(s):
        ms = round(s * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{n}\n{t(s)} --> {t(s + 1)}\nline {n}\n" for n, s in enumerate(starts, 1))


class PlacementTests(unittest.TestCase):
    STARTS = [0.0, 5.0, 83.5, 600.0]

    def place(self, timeline, starts=None, log=lambda msg: None, replace=False):
        starts = self.STARTS if starts is None else starts
        project = Project(timeline, starts)
        controller = TranscribeController(type("C", (), {"current_project": lambda s: project})())
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "t.srt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(srt(starts))
            return controller.place_subtitles(path, replace_existing=replace, log=log), project

    def assert_where_they_belong(self, timeline, fps=24.0):
        self.assertEqual(sorted(s.GetStart() for s in timeline.subs),
                         [START + round(t * fps) for t in self.STARTS])

    def test_landing_after_the_timelines_end_is_caught_and_placed_again(self):
        tl = Timeline()
        placed, _ = self.place(tl)
        self.assertEqual((placed, tl.appends), (len(self.STARTS), 2))
        self.assert_where_they_belong(tl)

    def test_placed_right_the_first_time_is_left_alone(self):
        tl = Timeline(has_content=False)
        self.place(tl)
        self.assertEqual(tl.appends, 1)
        self.assert_where_they_belong(tl)

    def test_a_fractional_frame_rate_is_checked_in_its_own_frames(self):
        tl = Timeline(fps="23.976")
        self.place(tl)
        self.assert_where_they_belong(tl, fps=23.976)

    def test_still_wrong_after_trying_again_is_taken_off_and_said(self):
        tl = Timeline(has_content=False, always_off=2 * FPS)
        with self.assertRaises(TranscribeResolveError) as caught:
            self.place(tl)
        self.assertEqual(tl.appends, PLACE_TRIES)
        self.assertEqual(tl.subs, [])                           # nothing left misplaced
        self.assertIn("2.0 s late", str(caught.exception))
        self.assertIn("SRT file is saved", str(caught.exception))

    def test_a_switched_off_or_locked_track_is_opened_first_and_said(self):
        tl = Timeline(has_content=False, enabled=False, locked=True)
        said = []
        placed, _ = self.place(tl, log=said.append)
        self.assertEqual((placed, tl.enabled, tl.locked), (len(self.STARTS), True, False))
        self.assert_where_they_belong(tl)
        self.assertEqual(said, ["Subtitle track 1 was switched off in Resolve, so Buddy switched it on.",
                                "Subtitle track 1 was locked in Resolve, so Buddy unlocked it."])

    def test_an_open_track_is_left_as_it_is(self):
        said = []
        self.place(Timeline(has_content=False), log=said.append)
        self.assertEqual(said, [])

    def test_landing_on_another_destination_track_is_taken_off_and_said(self):
        tl = Timeline(destination=2)
        own = list(tl.track2)
        with self.assertRaises(TranscribeResolveError) as caught:
            self.place(tl)
        self.assertEqual((tl.subs, tl.track2, tl.appends), ([], own, 1))    # its own subtitle stays
        self.assertIn("subtitle track 2, its destination track", str(caught.exception))
        self.assertIn("Make ST1 the destination", str(caught.exception))

    def test_replacing_on_the_only_track_takes_the_old_ones_off_first(self):
        tl = Timeline(subs=[Item(START + 5), Item(START + 50)])
        placed, _ = self.place(tl, replace=True)
        self.assertEqual(placed, len(self.STARTS))
        self.assert_where_they_belong(tl)

    def test_replacing_with_other_tracks_takes_the_old_ones_off_once_the_new_are_there(self):
        tl = Timeline(tracks=2, subs=[Item(START + 5), Item(START + 50)])
        placed, _ = self.place(tl, replace=True)
        self.assertEqual((placed, tl.appends), (len(self.STARTS), 2))
        self.assert_where_they_belong(tl)

    def test_replacing_when_the_destination_is_another_track_keeps_the_old_ones(self):
        old = [Item(START + 5), Item(START + 50)]
        tl = Timeline(destination=2, subs=old)
        own = list(tl.track2)
        with self.assertRaises(TranscribeResolveError):
            self.place(tl, replace=True)
        self.assertEqual((tl.subs, tl.track2), (old, own))

    def test_a_frame_of_rounding_is_not_a_mistake(self):
        tl = Timeline(has_content=False, always_off=1)
        placed, _ = self.place(tl)
        self.assertEqual((placed, tl.appends), (len(self.STARTS), 1))


if __name__ == "__main__":
    unittest.main()
