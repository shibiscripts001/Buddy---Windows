"""Transcribe's "DaVinci Resolve (Studio)" model: Resolve's own transcription
(pages/transcribe/resolve_transcript.py, resolve_child.py) turned into cues
that keep each speaker's words apart. The sample is shaped like what Studio
21.1.0.17 returned for a two-voice clip placed 2 s into a 24 fps timeline."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.transcribe import resolve_child
from pages.transcribe import resolve_transcript as rt
from pages.transcribe import subtitles as st

SAMPLE = {"language": "en", "segments": [
    {"start": "01:00:00:00", "end": "01:00:02:03", "text": "(...)",
     "words": [{"start": "01:00:00:00", "end": "01:00:02:03", "text": "(...)"}]},
    {"start": "01:00:02:03", "end": "01:00:04:00", "text": " Welcome back.(...) Today", "speaker": "Speaker 1",
     "words": [{"start": "01:00:02:03", "end": "01:00:02:13", "text": " Welcome"},
               {"start": "01:00:02:13", "end": "01:00:03:00", "text": " back."},
               {"start": "01:00:03:00", "end": "01:00:03:06", "text": "(...)"},
               {"start": "01:00:03:06", "end": "01:00:04:00", "text": " Today"}]},
    {"start": "01:00:04:00", "end": "01:00:05:00", "text": " Thanks!", "speaker": "Speaker 2",
     "words": [{"start": "01:00:04:00", "end": "01:00:05:00", "text": " Thanks!"}]},
]}


class ConversionTests(unittest.TestCase):
    def test_only_studio_21_1_or_later(self):
        self.assertTrue(rt.available("DaVinci Resolve Studio", [21, 1, 0, 17, ""]))
        self.assertTrue(rt.available("DaVinci Resolve Studio", [22, 0, 0, 1, ""]))
        self.assertFalse(rt.available("DaVinci Resolve Studio", [21, 0, 4, 5, ""]))   # no GetTranscription
        self.assertFalse(rt.available("DaVinci Resolve", [21, 1, 0, 17, ""]))         # free: no transcription
        self.assertFalse(rt.available("DaVinci Resolve Studio", None))
        self.assertFalse(rt.available("DaVinci Resolve Studio", ["x"]))

    def test_timecodes_count_like_the_timeline(self):
        self.assertEqual(rt.timecode_frames("01:00:02:03", 24.0), 86400 + 2 * 24 + 3)
        self.assertEqual(rt.timecode_frames("00:01:00;02", 29.97), 1800)        # drop frame
        self.assertEqual(rt.timecode_frames("00:10:00;00", 29.97), 17982)
        self.assertEqual(rt.timecode_frames("00:00:01:00", 23.976), 24)
        self.assertIsNone(rt.timecode_frames("soon", 24.0))

    def test_segments_in_seconds_without_the_silences(self):
        got = rt.to_segments(SAMPLE, 24.0, 86400)
        self.assertEqual(got["language"], "en")
        self.assertEqual(got["speakers"], ["Speaker 1", "Speaker 2"])
        first = got["segments"][0]
        self.assertAlmostEqual(first["start"], 2 + 3 / 24)
        self.assertEqual([w["word"] for w in first["words"]], [" Welcome", " back.", " Today"])
        self.assertEqual(first["text"], "Welcome back. Today")
        self.assertEqual(first["speaker"], "Speaker 1")
        self.assertEqual(len(got["segments"]), 2)                                # the silence segment went
        self.assertAlmostEqual(got["duration"], 5.0)
        self.assertEqual(rt.to_segments({}, 24.0, 0), {"language": "", "segments": [], "speakers": [], "duration": 0.0})

    def test_languages_both_ways(self):
        self.assertEqual(rt.resolve_language("zh"), "zh-Hans")       # Resolve refuses a bare "zh"
        self.assertEqual(rt.resolve_language("de"), "de")
        self.assertEqual(rt.resolve_language(""), "")
        self.assertEqual(rt.whisper_language("zh-hans"), "zh")        # so Chinese still wraps per character
        self.assertEqual(rt.to_segments({"language": "zh-hans"}, 24, 0)["language"], "zh")


class SpeakerCueTests(unittest.TestCase):
    def segments(self):
        return rt.to_segments(SAMPLE, 24.0, 86400)["segments"]

    def test_a_new_speaker_starts_a_new_cue(self):
        cues = st.build_cues(self.segments())
        self.assertEqual([c.text for c in cues], ["Welcome back. Today", "Thanks!"])   # "Thanks!" isn't folded in

    def test_speaker_names_lead_each_turn(self):
        cues = st.build_cues(self.segments(), st.Style(speaker_names=True))
        self.assertEqual([c.text for c in cues], ["Speaker 1: Welcome back. Today", "Speaker 2: Thanks!"])

    def test_one_speaker_is_never_named(self):
        one = [dict(s, speaker="Speaker 1") for s in self.segments()]
        self.assertFalse(any("Speaker" in c.text for c in st.build_cues(one, st.Style(speaker_names=True))))

    def test_whisper_transcripts_are_cut_as_before(self):
        segs = [{"start": 0, "end": 2, "text": "Hello there. Hi", "words": [
            {"start": 0.0, "end": 0.5, "word": " Hello"}, {"start": 0.5, "end": 1.0, "word": " there."},
            {"start": 1.1, "end": 1.4, "word": " Hi"}]}]
        self.assertEqual([c.text for c in st.build_cues(segs, st.Style(speaker_names=True))], ["Hello there. Hi"])


class FakeItem:
    def __init__(self, transcription):
        self.transcription, self.calls = transcription, []

    def ClearTranscription(self, nested):
        self.calls.append(("clear", nested))
        return True

    def TranscribeAudio(self, speakers, nested):
        self.calls.append(("transcribe", speakers, nested))
        return True

    def GetTranscription(self, nested):
        return self.transcription


class FakeTimeline:
    def __init__(self, item):
        self.item = item

    def GetUniqueId(self):
        return "tl-1"

    def GetMediaPoolItem(self):
        return self.item

    def GetSetting(self, key):
        return "24"

    def GetStartFrame(self):
        return 86400


class FakeProject:
    def __init__(self, timeline, language="auto", takes=True):
        self.timeline, self.settings, self.takes, self.changes = timeline, {"transcriptionLanguage": language}, takes, []

    def GetCurrentTimeline(self):
        return self.timeline

    def GetSetting(self, key):
        return self.settings[key]

    def SetSetting(self, key, value):
        self.changes.append(value)
        if not self.takes:
            return False
        self.settings[key] = value
        return True


class Controller:
    def __init__(self, project):
        self.project = project

    def current_project(self):
        return self.project


class ChildTests(unittest.TestCase):
    def run_child(self, project, **command):
        return resolve_child.run(dict({"timeline": "tl-1", "language": "", "fresh": True}, **command),
                                 Controller(project))

    def test_transcribes_the_timeline_as_one_clip_with_speakers(self):
        item = FakeItem(SAMPLE)
        got = self.run_child(FakeProject(FakeTimeline(item)))
        self.assertEqual(item.calls, [("clear", True), ("transcribe", True, True)])
        self.assertEqual((got["fps"], got["start_frame"]), (24.0, 86400))
        self.assertIs(got["transcription"], SAMPLE)

    def test_resolves_own_copy_when_asked(self):
        item = FakeItem(SAMPLE)
        self.run_child(FakeProject(FakeTimeline(item)), fresh=False)
        self.assertEqual(item.calls, [("transcribe", True, True)])   # nothing cleared: the old one comes back

    def test_a_chosen_language_is_set_for_the_call_and_put_back(self):
        project = FakeProject(FakeTimeline(FakeItem(SAMPLE)))
        self.run_child(project, language="zh")
        self.assertEqual(project.changes, ["zh-Hans", "auto"])
        with self.assertRaises(resolve_child.Refused):
            self.run_child(FakeProject(FakeTimeline(FakeItem(SAMPLE)), takes=False), language="xx")

    def test_refusals(self):
        with self.assertRaisesRegex(resolve_child.Refused, "timeline changed"):
            self.run_child(FakeProject(FakeTimeline(FakeItem(SAMPLE))), timeline="another")
        with self.assertRaisesRegex(resolve_child.Refused, "no speech"):
            self.run_child(FakeProject(FakeTimeline(FakeItem({"segments": []}))))
        with self.assertRaisesRegex(resolve_child.Refused, "Open a timeline"):
            self.run_child(FakeProject(None))

    def test_main_writes_what_happened(self):
        def read(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)

        def unreachable():
            raise RuntimeError("not running")

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.json")
            code = resolve_child.main(out, {"timeline": "tl-1"}, connect=lambda: Controller(FakeProject(
                FakeTimeline(FakeItem(SAMPLE)))))
            self.assertEqual((code, read(out)["ok"]), (0, True))
            self.assertEqual((resolve_child.main(out, {}, connect=unreachable), read(out)["ok"]), (1, False))


try:
    from pages.transcribe import jobs
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class JobTests(unittest.TestCase):
    """TranscribeJob's Resolve step, with a stand-in child script."""

    def job(self, answer):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        child = os.path.join(tmp.name, "child.py")
        with open(child, "w", encoding="utf-8") as f:
            f.write("import json, sys\nsys.stdin.read()\n"
                    f"json.dump({answer!r}, open(sys.argv[1], 'w'))\n")
        for name, value in (("RESOLVE_CHILD", child), ("_child_python", lambda: sys.executable)):
            patcher = mock.patch.object(jobs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return jobs.TranscribeJob(None, "Interview", {"engine": "resolve", "timeline": "tl-1", "fresh": True},
                                  "", "", st.Style())

    def test_resolves_answer_becomes_the_workers_result(self):
        job = self.job({"ok": True, "result": {"transcription": SAMPLE, "fps": 24.0, "start_frame": 86400}})
        got = job._transcribe_in_resolve()
        self.assertEqual((got["engine"], got["speakers"], len(got["segments"])), ("resolve", ["Speaker 1", "Speaker 2"], 2))

    def test_resolves_refusal_is_the_jobs_error(self):
        job = self.job({"ok": False, "error": "Resolve couldn't transcribe the timeline."})
        with self.assertRaisesRegex(RuntimeError, "couldn't transcribe"):
            job._transcribe_in_resolve()


if __name__ == "__main__":
    unittest.main()
