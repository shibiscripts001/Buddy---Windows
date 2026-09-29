"""Audio Assistant's processing pipeline: dsp.py's effects engine and chain,
render.py's WAV, sidecar and folder rule, and peaks.decode_range reading a
file's stretch back at full quality. Files go in throwaway folders only."""

import os
import tempfile
import unittest
import wave
from unittest import mock

import _paths  # noqa: F401

try:
    import numpy as np
    from pages.audio_assistant import dsp, render
    HAVE_DEPS = True
except ImportError:  # pragma: no cover - numpy missing
    HAVE_DEPS = False

RATE = 48000


def tone(freq, seconds=1.0, rate=RATE, amp=0.5):
    t = np.arange(int(seconds * rate)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def level_db(x):
    x = np.asarray(x, dtype=np.float64)
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)


class _Unity:
    def __init__(self, rate, strength):
        pass

    def gain(self, _spectrum):
        return 1.0


def read_wav(path):
    with wave.open(path, "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.uint8).reshape(-1, 3)
    ints = (raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8) | (raw[:, 2].astype(np.int32) << 16))
    ints = np.where(ints >= 2 ** 23, ints - 2 ** 24, ints)
    return (ints / 2 ** 23).reshape(-1, channels), rate, width


@unittest.skipUnless(HAVE_DEPS, "needs numpy")
class EngineTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(dsp.EFFECTS, {"unity": {"name": "Unity", "default": 0.5, "make": _Unity}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_gain_at_one_gives_back_what_went_in(self):
        rng = np.random.default_rng(1)
        for frames in (1, 100, dsp.N_FFT, dsp.N_FFT * 3 + 17, RATE + 5):
            x = rng.uniform(-1, 1, (frames, 2))
            y = dsp.process(x, RATE, [{"id": "unity"}])
            self.assertEqual(y.shape, x.shape)
            self.assertLess(np.abs(y - x).max(), 1e-5, frames)

    def test_mono_stays_mono(self):
        x = tone(440, 0.2)
        y = dsp.process(x, RATE, [{"id": "unity"}])
        self.assertEqual(y.shape, x.shape)
        self.assertLess(np.abs(y - x).max(), 1e-5)

    def test_nothing_on_is_a_straight_copy(self):
        x = tone(440, 0.1)
        np.testing.assert_allclose(dsp.process(x, RATE, [{"id": "unity", "on": False}]), x, atol=1e-7)

    def test_stopping_gives_none(self):
        self.assertIsNone(dsp.process(tone(440), RATE, [{"id": "unity"}], cancelled=lambda: True))

    def test_the_chain_is_cleaned(self):
        chain = dsp.clean_chain([{"id": "rumble", "strength": 7}, {"id": "rumble"}, {"id": "nope"}, "junk",
                                 {"id": "unity", "strength": "x", "on": 0}])
        self.assertEqual(chain, [{"id": "rumble", "on": True, "strength": 1.0},
                                 {"id": "unity", "on": False, "strength": 0.5}])
        self.assertEqual(dsp.clean_chain(None), [])
        self.assertEqual(dsp.active(chain), chain[:1])


@unittest.skipUnless(HAVE_DEPS, "needs numpy")
class RumbleTests(unittest.TestCase):
    def run_it(self, x, strength=0.6):
        return dsp.process(x, RATE, [{"id": "rumble", "strength": strength}])

    def test_rumble_goes_and_a_voice_stays(self):
        middle = slice(RATE // 4, -RATE // 4)       # away from the ends
        low, voice = tone(25), tone(1000)
        self.assertLess(level_db(self.run_it(low)[middle]) - level_db(low[middle]), -30)
        self.assertAlmostEqual(level_db(self.run_it(voice)[middle]), level_db(voice[middle]), delta=0.05)

    def test_dc_goes(self):
        # Down 30 dB and more, not to nothing: a window's spectrum spreads a
        # steady offset into the lowest bins (23 Hz apart), which pass a little.
        y = self.run_it(np.full(RATE, 0.25))
        self.assertLess(np.abs(y[RATE // 4:-RATE // 4]).max(), 0.25 * 10 ** (-30 / 20))

    def test_strength_moves_the_cutoff(self):
        self.assertEqual(dsp.EFFECTS["rumble"]["make"](RATE, 0.0).cutoff, 50.0)
        self.assertEqual(dsp.EFFECTS["rumble"]["make"](RATE, 1.0).cutoff, 100.0)
        mid = tone(80)[RATE // 4:-RATE // 4]
        gentle = level_db(self.run_it(tone(80), 0.0)[RATE // 4:-RATE // 4])
        firm = level_db(self.run_it(tone(80), 1.0)[RATE // 4:-RATE // 4])
        self.assertLess(firm, gentle)
        self.assertLess(gentle, level_db(mid))


@unittest.skipUnless(HAVE_DEPS, "needs numpy")
class ResolveMappingTests(unittest.TestCase):
    def test_the_channels_a_clip_plays(self):
        # As Resolve 21.1 gave them for A1-A3 of one FX6 MXF.
        for idx in (1, 2, 3):
            mapping = ('{"embedded_audio_channels":8,"linked_audio":{},"track_mapping":{"1":{"channel_idx":[%d],'
                       '"mute":false,"type":"mono"}}}' % idx)
            self.assertEqual(render.channels_from_mapping(mapping), [idx - 1])
        self.assertEqual(render.channels_from_mapping({"track_mapping": {"1": {"channel_idx": [1, 2]}}}), [0, 1])
        for junk in (None, "", "junk", "{}", {"track_mapping": {"1": {"channel_idx": [0]}}}):
            self.assertIsNone(render.channels_from_mapping(junk), junk)

    def test_the_handle_is_whole_frames_of_the_wav(self):
        from fractions import Fraction
        frame = Fraction(1, 24)
        self.assertEqual(render.frame_handle(0, frame), 0)
        self.assertEqual(render.frame_handle(10, frame), 1)
        self.assertEqual(render.frame_handle(Fraction(1, 2), frame), Fraction(1, 2))
        camera = 5 * Fraction(1001, 24000)          # 5 frames into a 23.976 file
        handle = render.frame_handle(camera, frame)
        self.assertEqual(handle, Fraction(5, 24))
        self.assertLessEqual(handle, camera)


@unittest.skipUnless(HAVE_DEPS, "needs numpy")
class WavTests(unittest.TestCase):
    def test_24_bit_round_trip_and_clipping_counted(self):
        x = np.stack([tone(440, 0.1), -tone(440, 0.1)], axis=1)
        x[0] = [1.5, -1.5]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "a.wav")
            self.assertEqual(render.write_wav(path, x, RATE), 2)
            back, rate, width = read_wav(path)
            self.assertEqual(os.listdir(folder), ["a.wav"])      # no temp file left
        self.assertEqual((rate, width, back.shape), (RATE, 3, x.shape))
        self.assertLess(np.abs(back[1:] - x[1:]).max(), 1.0 / 2 ** 22)
        self.assertAlmostEqual(back[0, 0], 1.0, places=5)
        self.assertEqual(back[0, 1], -1.0)


@unittest.skipUnless(HAVE_DEPS, "needs numpy")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        self.media = os.path.join(self.folder, "Card", "A001.wav")
        os.makedirs(os.path.dirname(self.media))
        with open(self.media, "wb") as f:
            f.write(b"not really audio - decode is faked")
        self.fallback = os.path.join(self.folder, "home", "renders")
        self.calls = []

    def decode(self, path, start, end, progress=None, cancelled=None, channels=None):
        self.calls.append((path, start, end))
        self.channels = channels
        frames = int(round((end - start) * RATE))
        return (np.stack([tone(30, frames / RATE) + tone(1000, frames / RATE)] * 2, axis=1), RATE), None

    def run_it(self, chain=None, **kw):
        return render.render(self.media, 5.0, 7.0, chain or [{"id": "rumble"}], self.decode,
                             fallback=self.fallback, **kw)

    def test_goes_beside_the_media_with_handles_and_a_sidecar(self):
        result, error = self.run_it()
        self.assertIsNone(error)
        self.assertEqual(os.path.dirname(result["path"]), os.path.join(os.path.dirname(self.media), "Buddy Audio"))
        self.assertTrue(result["beside"])
        self.assertTrue(os.path.basename(result["path"]).startswith("A001 - Buddy "))
        self.assertEqual(self.calls, [(self.media, 4.0, 8.0)])     # a second either side
        self.assertEqual((result["file_start"], result["frames"], result["channels"]), (4.0, 4 * RATE, 2))
        side = render.read_sidecar(result["path"])
        self.assertEqual(side["chain"], [{"id": "rumble", "on": True, "strength": 0.6}])
        self.assertEqual(side["range"], [5.0, 7.0])
        self.assertEqual(side["source"]["path"], os.path.abspath(self.media))
        back, _rate, _width = read_wav(result["path"])
        self.assertLess(level_db(back[RATE:-RATE, 0]), level_db(tone(1000)) + 0.1)   # the 30 Hz went

    def test_the_same_settings_reuse_the_file(self):
        first, _ = self.run_it()
        again, _ = self.run_it()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(again["reused"])
        self.assertEqual(again["path"], first["path"])
        other, _ = self.run_it([{"id": "rumble", "strength": 0.9}])
        self.assertNotEqual(other["path"], first["path"])
        self.assertEqual(len(self.calls), 2)

    def test_a_changed_source_is_processed_again(self):
        first, _ = self.run_it()
        st = os.stat(self.media)
        os.utime(self.media, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        again, _ = self.run_it()
        self.assertFalse(again["reused"])
        self.assertNotEqual(again["path"], first["path"])

    def test_a_folder_that_cant_be_written_falls_back(self):
        result, error = self.run_it(writable=lambda folder: False)
        self.assertIsNone(error)
        self.assertFalse(result["beside"])
        self.assertEqual(os.path.dirname(result["path"]), self.fallback)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.media), "Buddy Audio")))

    def test_the_handle_stops_at_the_files_start(self):
        render.render(self.media, 0.4, 2.0, [{"id": "rumble"}], self.decode, fallback=self.fallback)
        self.assertEqual(self.calls[-1][1:], (0.0, 3.0))

    def test_the_clips_channels_are_asked_for_and_kept_apart(self):
        mono, _ = self.run_it(channels=[1])
        self.assertEqual(self.channels, [1])
        both, _ = self.run_it()
        self.assertIsNone(self.channels)
        self.assertNotEqual(mono["path"], both["path"])
        self.assertEqual(render.read_sidecar(mono["path"])["channels_used"], [1])

    def test_what_is_refused(self):
        self.assertEqual(self.run_it([{"id": "rumble", "on": False}]), (None, "No effect is on."))
        self.assertEqual(render.render(self.media, 3, 3, [{"id": "rumble"}], self.decode)[1], "That clip has no length.")
        self.assertEqual(render.render(self.media + "x", 1, 2, [{"id": "rumble"}], self.decode)[1],
                         "The clip's file is offline.")
        failing = lambda *a, **k: (None, "It couldn't be decoded.")
        self.assertEqual(render.render(self.media, 1, 2, [{"id": "rumble"}], failing, fallback=self.fallback),
                         (None, "It couldn't be decoded."))
        self.assertEqual(self.calls, [])



# ------------------------------------------------ Tidy up, on a fake project --

class PoolClip:
    def __init__(self, uid, name, kind="Audio", path="", usage=0, comments=""):
        self.uid, self.name, self.comments = uid, name, comments
        self.props = {"Clip Name": name, "Type": kind, "File Path": path, "Usage": str(usage)}

    def GetUniqueId(self): return self.uid
    def GetName(self): return self.name
    def GetMetadata(self, key=None): return self.comments if key == "Comments" else ""

    def GetClipProperty(self, key=None):
        return self.props.get(key, "") if key else dict(self.props)


class Folder:
    def __init__(self, name, clips=(), subs=()):
        self.name, self.clips, self.subs = name, list(clips), list(subs)

    def GetName(self): return self.name
    def GetClipList(self): return list(self.clips)
    def GetSubFolderList(self): return list(self.subs)


class TidyTimeline:
    def __init__(self, name, clips=()):
        self.name, self.clips = name, list(clips)

    def GetName(self): return self.name
    def GetTrackCount(self, kind): return 1 if kind == "audio" else 0

    def GetItemListInTrack(self, kind, index):
        return [mock.Mock(GetMediaPoolItem=lambda c=c: c) for c in self.clips]


class TidyProject:
    def __init__(self, uid, root, timelines):
        self.uid, self.root, self.timelines = uid, root, list(timelines)

    def GetUniqueId(self): return self.uid
    def GetMediaPool(self): return self
    def GetRootFolder(self): return self.root
    def GetTimelineCount(self): return len(self.timelines)
    def GetTimelineByIndex(self, i): return self.timelines[i - 1]

    def _drop(self, clip, folder=None):
        folder = folder or self.root
        if clip in folder.clips:
            folder.clips.remove(clip)
        for sub in folder.subs:
            self._drop(clip, sub)

    def DeleteTimelines(self, timelines):
        for tl in timelines:
            self.timelines.remove(tl)
            clip = next(c for f in [self.root] + self.root.subs for c in f.clips if c.name == tl.name)
            self._drop(clip)
            for inner in tl.clips:                      # its clips aren't used there any more
                inner.props["Usage"] = str(int(inner.props["Usage"]) - 1)
        return True

    def DeleteClips(self, clips):
        for clip in clips:
            self._drop(clip)
        return True


@unittest.skipUnless(HAVE_DEPS, "needs numpy")
class TidyTests(unittest.TestCase):
    PID = "project-1"

    def setUp(self):
        from pages.audio_assistant import resolve_ext
        from core.atomic_io import write_json
        self.R = resolve_ext
        self.folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        media = os.path.join(self.folder, "Card", "A001.MXF")
        buddy = os.path.join(self.folder, "Card", "Buddy Audio")
        os.makedirs(buddy)
        open(media, "wb").close()

        def wav(name, projects, sidecar=True):
            path = os.path.join(buddy, name)
            render.write_wav(path, np.zeros(10), RATE)
            if sidecar:
                write_json(render.sidecar_path(path), {"version": 1, "projects": projects})
            return path

        self.paths = {"w1": wav("A001 - Buddy 11111111.wav", [self.PID]),
                      "w2": wav("A001 - Buddy 22222222.wav", [self.PID, "project-2"]),
                      "w3": wav("A001 - Buddy 33333333.wav", [self.PID]),
                      "gone": wav("A001 - Buddy 44444444.wav", [self.PID]),          # left the media pool
                      "theirs": wav("A001 - Buddy 55555555.wav", ["project-2"]),
                      "stranger": wav("A001 - Buddy 66666666.wav", [], sidecar=False)}
        self.source = PoolClip("m-src", "A001.MXF", "Video + Audio", media, usage=3)
        self.w1 = PoolClip("m-w1", "A001 - Buddy 11111111.wav", path=self.paths["w1"], usage=0)
        self.w2 = PoolClip("m-w2", "A001 - Buddy 22222222.wav", path=self.paths["w2"], usage=1)   # only in curve B
        self.w3 = PoolClip("m-w3", "A001 - Buddy 33333333.wav", path=self.paths["w3"], usage=2)
        self.stranger = PoolClip("m-s", "A001 - Buddy 66666666.wav", path=self.paths["stranger"], usage=0)
        from pages.audio_assistant import keys as K
        buddys = K.note([{"t": 1.0, "db": -6.0}], "m-src")               # what apply_curve leaves in the Comments
        self.curve_a = PoolClip("m-ca", "A001.MXF (Buddy curve)", "Timeline", usage=1, comments=buddys)
        self.curve_b = PoolClip("m-cb", "A001.MXF (Buddy curve 2)", "Timeline", usage=0, comments=buddys)
        self.bin = Folder("Buddy Audio", [self.curve_a, self.curve_b, self.w1, self.w2, self.w3, self.stranger])
        self.tl_b = TidyTimeline("A001.MXF (Buddy curve 2)", [self.w2])
        self.project = TidyProject(self.PID, Folder("Master", [self.source], [self.bin]),
                                   [TidyTimeline("Edit"), TidyTimeline("A001.MXF (Buddy curve)"), self.tl_b])
        self.controller = mock.Mock(current_project=lambda: self.project)

    def scan(self):
        return {i["id"]: i for i in self.R.tidy_scan(self.controller)["items"]}

    def test_the_scan_lists_only_what_nothing_uses(self):
        found = self.scan()
        self.assertEqual(sorted(found), sorted(["curve:m-cb", "wav:m-w1", "wav:m-w2", "file:" + self.paths["gone"]]))
        self.assertEqual(found["curve:m-cb"]["kind"], "curve")
        self.assertGreater(found["wav:m-w1"]["size"], 0)
        self.assertEqual(found["file:" + self.paths["gone"]]["kind"], "file")

    def test_removing_deletes_the_timeline_and_recycles_only_files_no_one_else_has(self):
        result = self.R.tidy_remove(self.controller, list(self.scan()))
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["removed"]), 4)
        self.assertNotIn(self.tl_b, self.project.timelines)
        self.assertNotIn(self.w1, self.bin.clips)
        self.assertNotIn(self.w2, self.bin.clips)
        self.assertIn(self.w3, self.bin.clips)                 # in use: kept
        self.assertIn(self.stranger, self.bin.clips)           # no sidecar: not Buddy's to judge
        recycled = set(result["recycle"])
        self.assertIn(self.paths["w1"], recycled)
        self.assertIn(render.sidecar_path(self.paths["w1"]), recycled)
        self.assertIn(self.paths["gone"], recycled)
        self.assertNotIn(self.paths["w2"], recycled)           # project-2 still has it...
        self.assertEqual(render.read_sidecar(self.paths["w2"])["projects"], ["project-2"])   # ...and only it now
        self.assertNotIn(self.paths["theirs"], recycled)

    def test_a_timeline_named_like_buddys_without_its_note_is_never_tidied(self):
        # Someone's own "... (Buddy curve 3)" in the bin: named like Buddy's, not Buddy's.
        theirs = PoolClip("m-u", "Mine (Buddy curve 3)", "Timeline", usage=0, comments="my notes")
        self.bin.clips.append(theirs)
        tl = TidyTimeline("Mine (Buddy curve 3)")
        self.project.timelines.append(tl)
        self.assertNotIn("curve:m-u", self.scan())
        result = self.R.tidy_remove(self.controller, ["curve:m-u"])
        self.assertEqual((result["removed"], result["failed"]), ([], ["curve:m-u"]))
        self.assertIn(tl, self.project.timelines)
        self.assertIn(theirs, self.bin.clips)

    def test_what_got_used_meanwhile_is_left_alone(self):
        ids = list(self.scan())
        self.w1.props["Usage"] = "1"
        result = self.R.tidy_remove(self.controller, ids)
        self.assertIn("wav:m-w1", result["failed"])
        self.assertIn(self.w1, self.bin.clips)
        self.assertNotIn(self.paths["w1"], result["recycle"])

    def test_with_the_bin_gone_this_projects_files_are_all_left_on_disk(self):
        self.project.root.subs = []             # Resolve takes a deleted bin's clips off the timelines too
        self.assertEqual(sorted(self.scan()), sorted("file:" + self.paths[k] for k in ("w1", "w2", "w3", "gone")))

    def test_a_wav_moved_to_another_bin_and_used_is_left_alone(self):
        self.bin.clips.remove(self.w3)
        self.project.root.subs.append(Folder("Mix", [self.w3]))
        found = self.scan()
        self.assertNotIn("wav:m-w3", found)
        self.assertNotIn("file:" + self.paths["w3"], found)


@unittest.skipUnless(HAVE_DEPS, "needs numpy")
class ReleaseTests(unittest.TestCase):
    def test_a_render_is_claimed_by_each_project_that_uses_it(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        media = os.path.join(folder, "a.wav")
        open(media, "wb").close()

        def decode(path, a, b, progress=None, cancelled=None, channels=None):
            return (np.zeros((int((b - a) * RATE), 1)), RATE), None

        first, _ = render.render(media, 1.0, 2.0, [{"id": "rumble"}], decode, project="p1")
        render.render(media, 1.0, 2.0, [{"id": "rumble"}], decode, project="p2")       # reused
        self.assertEqual(render.read_sidecar(first["path"])["projects"], ["p1", "p2"])
        self.assertFalse(render.release(first["path"], "p1"))
        self.assertTrue(render.release(first["path"], "p2"))
        self.assertFalse(render.release(os.path.join(folder, "nope.wav"), "p1"))

    def test_the_recycle_bin_skips_missing_files_and_is_asked_once(self):
        from core import recycle
        with tempfile.TemporaryDirectory() as folder:
            there = os.path.join(folder, "a.wav")
            open(there, "wb").close()
            with mock.patch.object(recycle, "_windows") as win, mock.patch.object(recycle, "_mac") as mac, \
                    mock.patch.object(recycle.sys, "platform", "win32"):
                recycle.to_recycle_bin([there, os.path.join(folder, "gone.wav")])
                recycle.to_recycle_bin([os.path.join(folder, "gone.wav")])
            win.assert_called_once_with([there])
            mac.assert_not_called()

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtMultimedia import QAudioDecoder  # noqa: F401
    from pages.audio_assistant import peaks
    HAVE_QT = HAVE_DEPS
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "needs PySide6's Qt Multimedia")
class DecodeRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_a_stretch_comes_back_at_full_quality(self):
        x = np.stack([tone(440, 2.0), tone(660, 2.0, amp=0.25)], axis=1)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "two.wav")
            render.write_wav(path, x, RATE)
            got, error = peaks.decode_range(path, 0.5, 1.25)
            tail, tail_error = peaks.decode_range(path, 1.5, 9.0)       # past the end: what there is
        self.assertIsNone(error)
        samples, rate = got
        self.assertEqual((rate, samples.shape), (RATE, (int(0.75 * RATE), 2)))
        self.assertLess(np.abs(samples - x[RATE // 2:int(1.25 * RATE)]).max(), 1e-4)
        self.assertIsNone(tail_error)
        self.assertEqual(len(tail[0]), RATE // 2)

    def test_only_the_clips_channels_come_back(self):
        x = np.stack([tone(440, 1.0), tone(660, 1.0, amp=0.25)], axis=1)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "two.wav")
            render.write_wav(path, x, RATE)
            second, error = peaks.decode_range(path, 0.0, 0.5, channels=[1])
            missing, why = peaks.decode_range(path, 0.0, 0.5, channels=[2])
        self.assertIsNone(error)
        self.assertEqual(second[0].shape, (RATE // 2, 1))
        self.assertLess(np.abs(second[0][:, 0] - x[:RATE // 2, 1]).max(), 1e-4)
        self.assertIsNone(missing)
        self.assertEqual(why, "Buddy can read only the first 2 channels of this file, and the clip plays channel 3.")


if __name__ == "__main__":
    unittest.main()
