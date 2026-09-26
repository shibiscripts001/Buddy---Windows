"""pages/project_setup/otio_engine.py: transitions and compound clips in an
exported timeline, silence checks on loose audio, and media with no
available_range - all on small hand-written OTIO documents."""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import _paths  # noqa: F401
import pages.project_setup
from pages.project_setup import otio_engine as oe

RATE = 24.0


def _rt(value, rate=RATE):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": value}


def _range(start, duration, rate=RATE):
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, rate),
            "duration": _rt(duration, rate)}


def clip(name, duration, path="", link=None, available=True, source_start=0.0):
    media = {"OTIO_SCHEMA": "ExternalReference.1", "target_url": path}
    if available:
        media["available_range"] = _range(0.0, 10000.0)
    metadata = {}
    if link is not None:
        metadata = {"Resolve_OTIO": {"Link Group ID": link}}
    return {"OTIO_SCHEMA": "Clip.2", "name": name, "metadata": metadata,
            "source_range": _range(source_start, duration),
            "media_references": {"DEFAULT_MEDIA": media},
            "active_media_reference_key": "DEFAULT_MEDIA"}


def gap(duration):
    return oe._make_gap(duration, RATE)


def transition(name="Cross Dissolve"):
    # As OTIO writes one: offsets into the neighbours, no source_range.
    return {"OTIO_SCHEMA": "Transition.1", "name": name, "metadata": {},
            "transition_type": "SMPTE_Dissolve",
            "in_offset": _rt(6), "out_offset": _rt(6)}


def compound(name, duration):
    inner = oe._make_track("Video", "inner", [clip("inside", duration)])
    return {"OTIO_SCHEMA": "Stack.1", "name": name, "metadata": {},
            "source_range": None, "effects": [], "markers": [],
            "enabled": True, "children": [inner]}


def document(*tracks):
    return {"OTIO_SCHEMA": "Timeline.1", "name": "T", "metadata": {},
            "tracks": {"OTIO_SCHEMA": "Stack.1", "children": list(tracks)}}


def track(kind, *children):
    return oe._make_track(kind, kind, list(children))


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.doc = document(track("Video", clip("A", 48), transition(),
                                  clip("B", 24), gap(12), clip("C", 10)))

    def test_transition_takes_no_track_time(self):
        entries = oe.read_entries(self.doc)
        self.assertEqual([(e["name"], e["start"]) for e in entries],
                         [("A", 0.0), ("B", 48.0), ("C", 84.0)])

    def test_rebuild_keeps_positions_and_warns(self):
        warnings = oe.rebuild_warnings(self.doc)
        self.assertEqual(len(warnings), 1)
        self.assertIn("1 transition", warnings[0])

        result, stats = oe.shift_document(self.doc)
        self.assertEqual(stats["clips"], 3)
        self.assertEqual([(e["name"], e["start"]) for e in oe.read_entries(result)],
                         [("A", 0.0), ("B", 48.0), ("C", 84.0)])
        self.assertEqual(oe.rebuild_warnings(result), [])

    def test_collapse_reports_it(self):
        _result, kept, dropped, warnings = oe.collapse_document_from(self.doc)
        self.assertEqual((kept, dropped), (3, 0))
        self.assertTrue(any("transition" in w for w in warnings))

    def test_no_transitions_no_warning(self):
        self.assertEqual(oe.rebuild_warnings(document(track("Video", clip("A", 5)))), [])


class CompoundClipTests(unittest.TestCase):
    def setUp(self):
        self.doc = document(track("Video", clip("A", 10), compound("Comp 1", 20),
                                  clip("B", 5)))

    def test_later_clips_keep_their_position(self):
        # The Stack has no source_range - its length comes from inside it.
        entries = oe.read_entries(self.doc)
        self.assertEqual([(e["name"], e["start"]) for e in entries],
                         [("A", 0.0), ("B", 30.0)])

    def test_every_rebuild_refuses_rather_than_dropping_it(self):
        self.assertEqual(oe.unsupported_items(self.doc), [("Stack", "Comp 1")])
        for run in (lambda: oe.shift_document(self.doc),
                    lambda: oe.expand_document(self.doc),
                    lambda: oe.collapse_document_from(self.doc),
                    lambda: oe.assemble_document(self.doc, "timecode", sync_audio=False)):
            with self.assertRaises(oe.OtioError) as caught:
                run()
            self.assertIn("Comp 1", str(caught.exception))


class NoAvailableRangeTests(unittest.TestCase):
    def test_logical_clip_has_no_timecode_instead_of_crashing(self):
        doc = document(track("Video", clip("A", 10, path="a.mov", available=False)))
        clips = oe.logical_clips(doc)
        self.assertEqual(len(clips), 1)
        self.assertIsNone(clips[0].start_tc_seconds)
        self.assertEqual(clips[0].source_fps, RATE)
        self.assertFalse(oe.has_usable_timecode(clips[0]))

    def test_timecode_offsets_skip_it(self):
        doc = document(track("Video", clip("A", 10, available=False),
                             clip("B", 10), clip("C", 10)))
        doc["tracks"]["children"][0]["children"][1]["media_references"]["DEFAULT_MEDIA"][
            "available_range"] = _range(24.0 * 3600, 10000.0)
        offsets, warnings = oe.compute_offsets_timecode(oe.logical_clips(doc))
        self.assertEqual(list(offsets), ["1:B"])
        self.assertTrue(any('"A"' in w for w in warnings))


class SilentAudioTests(unittest.TestCase):
    """is_silent is mocked to report silence whenever it is asked about only
    part of a file's channels - which is what used to happen to every loose
    recording on the timeline."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.paths = {}
        for name in ("cam", "rec1", "rec2"):
            path = os.path.join(self.dir.name, f"{name}.wav")
            with open(path, "wb"):
                pass
            self.paths[name] = path
        self.asked = []

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, doc, layouts):
        def is_silent(_ffmpeg, path, start_seconds=None, duration_seconds=None,
                      channels=None):
            self.asked.append((os.path.basename(path), channels))
            return channels != list(range(sum(layouts[path])))

        warnings = []
        # A stand-in audio_sync rather than patching the real one: loading
        # that needs numpy, which the release build's test machine doesn't
        # install. find_silent_entries only uses is_silent from it. Put in
        # both places `from . import audio_sync` looks, in case the real
        # module is already loaded.
        fake_audio_sync = types.SimpleNamespace(is_silent=is_silent)
        with mock.patch("pages.project_setup.ffmpeg_utils.audio_channel_layout",
                        side_effect=lambda _f, path: layouts[path]), \
                mock.patch.dict(sys.modules, {"pages.project_setup.audio_sync": fake_audio_sync}), \
                mock.patch.object(pages.project_setup, "audio_sync", fake_audio_sync, create=True):
            silent = oe.find_silent_entries(oe.read_entries(doc), "ffmpeg", warnings)
        return silent, warnings

    def test_unlinked_clips_are_each_judged_on_all_channels(self):
        doc = document(track("Audio", clip("R1", 10, self.paths["rec1"])),
                       track("Audio", clip("R2", 10, self.paths["rec2"])))
        silent, warnings = self._run(doc, {self.paths["rec1"]: [2],
                                           self.paths["rec2"]: [2]})
        self.assertEqual(silent, set())
        self.assertEqual(sorted(self.asked), [("rec1.wav", [0, 1]), ("rec2.wav", [0, 1])])
        self.assertEqual(warnings, [])

    def test_linked_tracks_of_one_file_get_one_channel_each(self):
        cam = self.paths["cam"]
        doc = document(track("Video", clip("V", 10, cam, link=1)),
                       track("Audio", clip("A1", 10, cam, link=1)),
                       track("Audio", clip("A2", 10, cam, link=1)))
        self._run(doc, {cam: [1, 1]})
        self.assertEqual(self.asked, [("cam.wav", [0]), ("cam.wav", [1])])

    def test_linked_audio_from_another_file_is_not_a_track_of_the_camera(self):
        cam, rec = self.paths["cam"], self.paths["rec1"]
        doc = document(track("Video", clip("V", 10, cam, link=7)),
                       track("Audio", clip("A1", 10, cam, link=7)),
                       track("Audio", clip("Lav", 10, rec, link=7)))
        silent, _warnings = self._run(doc, {cam: [2], rec: [2]})
        self.assertEqual(silent, set())
        self.assertEqual(self.asked, [("cam.wav", [0, 1]), ("rec1.wav", [0, 1])])

    def test_media_without_range_is_kept_unchecked(self):
        doc = document(track("Audio", clip("R1", 10, self.paths["rec1"],
                                           available=False, source_start=1e6)))
        silent, warnings = self._run(doc, {self.paths["rec1"]: [2]})
        self.assertEqual(silent, set())
        self.assertEqual(self.asked, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn('"R1"', warnings[0])


if __name__ == "__main__":
    unittest.main()
