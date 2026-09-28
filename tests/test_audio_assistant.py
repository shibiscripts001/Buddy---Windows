"""Audio Assistant's Timeline tab: what resolve_ext.py reads from and writes to a
fake Resolve timeline (never a real project), what levels.py asks of it for each
control, the child process, how peaks.py folds decoded audio into the waveform
bytes and loudness the page draws, and keys.py's volume keys - the curve, and the
timeline files they go to and come from Resolve in."""

import unittest

import _paths  # noqa: F401

try:
    import numpy as np
    from pages.audio_assistant import keys as K
    from pages.audio_assistant import peaks, resolve_ext
    HAVE_DEPS = True
except ImportError:  # pragma: no cover - numpy or PySide6 missing
    HAVE_DEPS = False


# ------------------------------------------------------------ fake Resolve --

class MediaPoolItem:
    def __init__(self, uid, path, fps="24.0", rate="48000", channels="2", codec="Linear PCM", name="", comments=""):
        self.uid, self.calls, self.name, self.comments = uid, 0, name, comments
        self.props = {"File Path": path, "FPS": fps, "Sample Rate": rate, "Audio Ch": channels, "Audio Codec": codec}

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.name

    def GetMetadata(self, key):
        return self.comments if key == "Comments" else ""

    def GetClipProperty(self):
        self.calls += 1
        return dict(self.props)


class Item:
    def __init__(self, uid, name, start, end, mpi=None, source_start=0, volume=0.0, enabled=True, color="",
                 fades=(0.0, 0.0), isolation=None):
        self.uid, self.name, self.start, self.end, self.mpi = uid, name, start, end, mpi
        self.source_start, self.enabled, self.color, self.fades = source_start, enabled, color, fades
        self.props = {"AudioVolumeEnabled": True, "AudioVolume": volume, "AudioPan": 0.0}
        if isolation is not None:
            self.props.update({"AudioVoiceIsolationEnabled": isolation[0], "AudioVoiceIsolationAmount": isolation[1]})

    def GetUniqueId(self): return self.uid
    def GetName(self): return self.name
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetClipEnabled(self): return self.enabled
    def GetClipColor(self): return self.color
    def GetSourceStartFrame(self): return self.source_start
    def GetMediaPoolItem(self): return self.mpi
    def GetProperties(self): return dict(self.props)
    def GetFades(self): return {"FadeIn": self.fades[0], "FadeOut": self.fades[1]}

    def SetProperties(self, props):
        # Resolve's rule: one key it won't take (or a clip without the feature) and nothing changes.
        if any(key not in self.props for key in props):
            return False
        self.props.update(props)
        return True

    def SetFades(self, fades):
        if fades["FadeIn"] + fades["FadeOut"] > self.end - self.start:
            return False
        self.fades = (float(fades["FadeIn"]), float(fades["FadeOut"]))
        return True

    def AddTransition(self, options):
        self.transition = options
        return object() if self.name != "no handles" else None


class Timeline:
    def __init__(self, tracks, selected=(), timecode="01:00:10:00", markers=None, name="Edit 1", fcp7=None):
        self.tracks, self.selected, self.timecode = tracks, list(selected), timecode
        self.markers = markers or {}
        self.moved_to = None
        self.name, self.fcp7 = name, fcp7          # fcp7: what Export(EXPORT_FCP_7_XML) writes

    def GetUniqueId(self): return "tl-" + self.name
    def GetName(self): return self.name

    def Export(self, path, kind, subtype):
        if self.fcp7 is None or kind != Resolve.EXPORT_FCP_7_XML:
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.fcp7)
        return True
    def GetSetting(self, key): return {"timelineFrameRate": "24", "timelineDropFrameTimecode": "0"}.get(key)
    def GetStartFrame(self): return 86400
    def GetEndFrame(self): return 86400 + 24 * 60
    def GetTrackCount(self, kind): return len(self.tracks) if kind == "audio" else 0
    def GetItemListInTrack(self, kind, i): return self.tracks[i - 1]["items"]
    def GetTrackName(self, kind, i): return self.tracks[i - 1]["name"]
    def GetTrackSubType(self, kind, i): return self.tracks[i - 1].get("kind", "stereo")
    def GetIsTrackEnabled(self, kind, i): return self.tracks[i - 1].get("enabled", True)
    def GetIsTrackLocked(self, kind, i): return False
    def GetMarkers(self): return self.markers
    def GetCurrentTimecode(self): return self.timecode
    def GetSelectedClips(self): return self.selected

    def SetCurrentTimecode(self, tc):
        self.moved_to = tc
        return True

    def NormalizeAudioLevel(self, items, options):
        self.normalized = options
        for item in items:
            item.props["AudioVolume"] = options.get("targetLoudness", -1.0) + 10
        return True


class Resolve:
    NORMALIZE_AUDIO_SET_LEVEL_RELATIVE = 0.0
    NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT = 1.0
    EXPORT_FCP_7_XML = 3
    EXPORT_NONE = 0

    def GetProductName(self):
        return "DaVinci Resolve Studio"


class Controller:
    def __init__(self, timeline, others=()):
        self.timeline, self.others = timeline, list(others)
        self.resolve = Resolve()

    def current_project(self):
        tl, all_ = self.timeline, [self.timeline] + self.others

        class Project:
            def GetCurrentTimeline(self):
                return tl

            def GetTimelineCount(self):
                return len(all_)

            def GetTimelineByIndex(self, i):
                return all_[i - 1]
        return Project()


def fcp7(*clips, rate=24):
    """A Resolve-style FCP 7 XML export: clips are (name, start, end, in, [(when, gain, eased)])."""
    items = []
    for name, start, end, in_, frames in clips:
        keys = "".join(f"<keyframe><when>{w}</when><value>{g}</value>"
                       + ("<inbez><horiz>1</horiz><vert>1</vert></inbez>" if e else "") + "</keyframe>"
                       for w, g, e in frames)
        items.append(f"""<clipitem id="{name} 0"><name>{name}</name><rate><timebase>{rate}</timebase><ntsc>FALSE</ntsc></rate>
            <start>{start}</start><end>{end}</end><in>{in_}</in><out>{in_ + end - start}</out>
            <filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid><parameter><name>Level</name>
            <value>1</value>{keys}</parameter></effect></filter></clipitem>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE xmeml><xmeml version="5"><sequence>
        <rate><timebase>{rate}</timebase><ntsc>FALSE</ntsc></rate><media><video><track/></video>
        <audio><track>{''.join(items)}</track></audio></media></sequence></xmeml>"""


@unittest.skipUnless(HAVE_DEPS, "numpy / PySide6 not installed")
class ReadTimelineTests(unittest.TestCase):
    def setUp(self):
        self.camera = MediaPoolItem("m1", r"D:\shoot\C001.MP4", fps="29.97")
        self.mic = MediaPoolItem("m2", r"D:\shoot\mic.wav")
        self.a = Item("c1", "C001.MP4", 86400, 86640, self.camera, source_start=299.7, enabled=False)
        self.b = Item("c2", "mic.wav", 86424, 86800, self.mic, volume=22.0, color="Teal", fades=(12.0, 6.0),
                      isolation=(True, 60.0))
        self.c = Item("c3", "mic.wav", 86900, 87000, self.mic, source_start=48)
        self.timeline = Timeline([
            {"name": "Camera", "items": [self.a], "kind": "adaptive16", "enabled": False},
            {"name": "Mic", "items": [self.b, self.c]},
        ], selected=[self.b], markers={"48": {"color": "Blue", "name": "Intro"}})
        self.cache = {}
        self.data, self.items = resolve_ext.read_timeline(Controller(self.timeline), self.cache)

    def test_tracks_and_clips_are_read_in_order(self):
        tracks = self.data["tracks"]
        self.assertEqual([t["name"] for t in tracks], ["Camera", "Mic"])
        self.assertEqual(tracks[0]["kind"], "adaptive16")
        self.assertFalse(tracks[0]["enabled"])
        self.assertEqual([c["id"] for c in tracks[1]["clips"]], ["c2", "c3"])
        self.assertEqual(set(self.items), {"c1", "c2", "c3"})
        self.assertEqual((self.data["start"], self.data["end"], self.data["fps"]), (86400, 87840, 24.0))

    def test_a_clip_carries_its_levels_and_colour(self):
        clip = self.data["tracks"][1]["clips"][0]
        self.assertEqual(clip["volume"], 22.0)
        self.assertEqual((clip["fade_in"], clip["fade_out"]), (12.0, 6.0))
        self.assertEqual(clip["color"], resolve_ext.CLIP_COLORS["Teal"])
        self.assertEqual(clip["isolation"], {"on": True, "amount": 60.0})
        self.assertFalse(self.data["tracks"][0]["clips"][0]["enabled"])

    def test_isolation_is_none_where_resolve_has_none(self):
        # A camera MXF's clip reads None for it: the page says "Not available".
        self.assertIsNone(self.data["tracks"][0]["clips"][0]["isolation"])

    def test_offset_is_the_source_frame_in_the_medias_own_rate(self):
        camera = self.data["tracks"][0]["clips"][0]
        self.assertAlmostEqual(camera["offset"], 10.0)       # 299.7 frames at 29.97
        self.assertAlmostEqual(self.data["tracks"][1]["clips"][1]["offset"], 2.0)   # 48 at 24

    def test_a_file_is_described_once_and_shares_its_key(self):
        b, c = self.data["tracks"][1]["clips"]
        self.assertEqual(self.mic.calls, 1)
        self.assertEqual(b["media"]["key"], c["media"]["key"])
        self.assertEqual((b["media"]["rate"], b["media"]["channels"]), (48000.0, 2))
        resolve_ext.read_timeline(Controller(self.timeline), self.cache)
        self.assertEqual(self.mic.calls, 1)

    def test_markers_are_absolute_frames(self):
        self.assertEqual(self.data["markers"][0]["frame"], 86448)
        self.assertEqual(self.data["markers"][0]["name"], "Intro")

    def test_live_read(self):
        self.b.props["AudioVolume"] = -3.0
        live = resolve_ext.read_live(Controller(self.timeline), 24.0, {"c2": self.items["c2"]})
        self.assertEqual(live["playhead"], 86400 + 240)
        self.assertEqual(live["resolve_selection"], ["c2"])
        self.assertEqual(live["levels"]["c2"]["volume"], -3.0)

    def test_no_timeline_is_said(self):
        with self.assertRaises(resolve_ext.ResolveConnectionError):
            resolve_ext.read_timeline(Controller(None), {})

    def test_a_crossfade_is_read_as_one_not_a_clip(self):
        self.timeline.tracks[1]["items"].append(Item("x1", "Cross Fade +3 dB", 86790, 86810))
        data, _items = resolve_ext.read_timeline(Controller(self.timeline), {})
        fade = data["tracks"][1]["clips"][-1]
        self.assertTrue(fade["transition"])
        self.assertNotIn("media", fade)
        self.assertTrue(data["studio"])


@unittest.skipUnless(HAVE_DEPS, "numpy / PySide6 not installed")
class ReadKeysTests(unittest.TestCase):
    """Keys on the timeline: a clip keyed in Resolve, and a Buddy curve clip."""

    def test_a_clip_keyed_in_resolve_reads_its_keys_in_file_time(self):
        mic = MediaPoolItem("m2", r"D:\shoot\mic.wav")
        clip = Item("c2", "mic.wav", 86424, 86800, mic, source_start=48)       # 2 s into the file
        # Keys at source frames 72 and 96 (3 s and 4 s), the second eased; 0.1 is -20 dB.
        tl = Timeline([{"name": "Mic", "items": [clip]}],
                      fcp7=fcp7(("mic.wav", 24, 400, 48, [(72, 1, False), (96, 0.1, True)])))
        data, _ = resolve_ext.read_timeline(Controller(tl), {})
        read = data["tracks"][0]["clips"][0]
        self.assertEqual(read["keys"], [{"t": 3.0, "db": 0.0, "ease": False}, {"t": 4.0, "db": -20.0, "ease": True}])
        self.assertIsNone(read["curve"])

    def test_no_export_means_no_keys(self):
        mic = MediaPoolItem("m2", r"D:\shoot\mic.wav")
        data, _ = resolve_ext.read_timeline(Controller(Timeline([{"name": "Mic", "items": [
            Item("c2", "mic.wav", 86424, 86800, mic)]}])), {})
        self.assertIsNone(data["tracks"][0]["clips"][0]["keys"])

    def test_a_buddy_curve_clip_takes_its_files_waveform_and_buddys_keys(self):
        mic = MediaPoolItem("m2", r"D:\shoot\mic.wav")
        ours = [{"t": 2.0, "db": 0.0, "ease": False}, {"t": 3.0, "db": -12.0, "ease": True}]
        inner = Item("i1", "mic.wav", 86400, 86776, mic, source_start=24)     # 1 s into the file
        # Resolve holds the expanded keys (as Buddy wrote them), in source frames.
        written = K.expand(ours, 24.0, 1.0)
        nested = Timeline([{"name": "A1", "items": [inner]}], name="mic.wav (Buddy curve)",
                          fcp7=fcp7(("mic.wav", 0, 376, 24, [(round(k["t"] * 24), 10 ** (k["db"] / 20), False)
                                                           for k in written])))
        tl_mpi = MediaPoolItem("t1", "", name="mic.wav (Buddy curve)", comments=K.note(ours, "c2"))
        outer = Item("n1", "mic.wav (Buddy curve)", 86424, 86800, tl_mpi, volume=6.0)
        tl = Timeline([{"name": "A2", "items": [outer]}])
        data, _ = resolve_ext.read_timeline(Controller(tl, [nested]), {})
        clip = data["tracks"][0]["clips"][0]
        self.assertEqual(clip["curve"], {"original": "c2", "edited": False})
        self.assertEqual(clip["keys"], ours)                        # Buddy's own, eases and all
        self.assertAlmostEqual(clip["offset"], 1.0)
        self.assertEqual(clip["media"]["path"], r"D:\shoot\mic.wav")
        # Changed in Resolve since: its keys are what's shown.
        nested.fcp7 = fcp7(("mic.wav", 0, 376, 24, [(48, 1, False), (60, 0.5, False)]))
        clip = resolve_ext.read_timeline(Controller(tl, [nested]), {})[0]["tracks"][0]["clips"][0]
        self.assertTrue(clip["curve"]["edited"])
        self.assertEqual([k["t"] for k in clip["keys"]], [2.0, 2.5])

    def test_a_nested_timeline_that_isnt_buddys_is_left_alone(self):
        other = MediaPoolItem("t2", "", name="Intro music", comments="")
        data, _ = resolve_ext.read_timeline(Controller(Timeline([{"name": "A1", "items": [
            Item("n2", "Intro music", 86400, 86500, other)]}])), {})
        clip = data["tracks"][0]["clips"][0]
        self.assertEqual((clip["media"], clip["keys"], clip["curve"]), (None, None, None))


@unittest.skipUnless(HAVE_DEPS, "numpy / PySide6 not installed")
class KeysTests(unittest.TestCase):
    def test_straight_in_db_between_keys_and_flat_outside(self):
        keys = [{"t": 1.0, "db": 0.0, "ease": False}, {"t": 3.0, "db": -20.0, "ease": False}]
        self.assertEqual(list(K.curve_db(keys, [0.0, 1.0, 2.0, 2.5, 3.0, 9.0])), [0.0, 0.0, -10.0, -15.0, -20.0, -20.0])
        self.assertEqual(float(K.curve_db(keys[:1], 5.0)), 0.0)
        self.assertEqual(float(K.curve_db([], 5.0)), 0.0)

    def test_an_eased_key_is_flat_into_it(self):
        keys = [{"t": 0.0, "db": 0.0, "ease": False}, {"t": 1.0, "db": -20.0, "ease": True}]
        near = K.curve_db(keys, [0.98, 0.99, 1.0])
        self.assertLess(abs(near[0] - near[2]), 0.05)                # hardly moving as it arrives
        self.assertLess(K.curve_db(keys, 0.5), -10.0)                 # so ahead of the straight line mid-way

    def test_expand_writes_eases_as_short_straight_pieces_on_frames(self):
        keys = [{"t": 1.0, "db": 0.0, "ease": True}, {"t": 3.0, "db": -20.0, "ease": True}, {"t": 4.0, "db": -20.0}]
        out = K.expand(keys, 24.0, origin=0.5)
        self.assertFalse(any(k["ease"] for k in out))
        for k in out:
            frames = (k["t"] - 0.5) * 24
            self.assertAlmostEqual(frames, round(frames), places=4)
        t = np.linspace(1.0, 4.0, 400)
        self.assertLess(np.abs(K.curve_db(out, t) - K.curve_db(K.clean(keys), t)).max(), 0.3)
        plain = [{"t": 1.0, "db": 0.0, "ease": False}, {"t": 2.0, "db": -6.0, "ease": False}]
        self.assertEqual(K.expand(plain, 24.0), plain)

    def test_clean_sorts_clamps_and_drops_junk(self):
        self.assertEqual(K.clean([{"t": 2, "db": 50}, {"t": 1, "db": -300, "ease": 1}, {"t": "x", "db": 0}, {}]),
                         [{"t": 1.0, "db": -100.0, "ease": True}, {"t": 2.0, "db": 30.0, "ease": False}])

    def test_the_fcpxml_puts_the_clip_and_keys_where_resolve_lands_them(self):
        import xml.etree.ElementTree as ET
        from fractions import Fraction
        media_frame = K.frame_duration(29.97)
        media_start = 146120 * media_frame                          # a camera file's 01:21:15;16
        source_in = 100 * media_frame
        keys = [{"t": float(source_in) + 1.0, "db": -6.0, "ease": False}]
        xml = K.nested_fcpxml("C1.MP4 (Buddy curve)", r"E:\My Media\C1.MP4", 24.0, 86400, 240, media_start,
                              source_in, 2, keys, media_frame=media_frame)
        root = ET.fromstring(xml)
        clip = root.find(".//asset-clip")

        def seconds(text):
            return Fraction(text[:-1]) if text != "0s" else Fraction(0)
        # A quarter of a file frame early (Resolve rounds a clip's start up)...
        self.assertEqual(seconds(clip.get("start")), media_start + source_in - media_frame / 4)
        self.assertEqual(clip.get("srcEnable"), "audio")
        self.assertEqual(seconds(root.find(".//sequence").get("tcStart")), 3600)
        # ...and a key on its timeline frame exactly, a sliver after (Resolve rounds a key down).
        key = root.find(".//keyframe")
        self.assertEqual(seconds(key.get("time")), media_start + source_in + 1 + Fraction(1, 24 * 64))
        self.assertEqual(key.get("value"), "-6dB")
        self.assertEqual(root.find(".//media-rep").get("src"), "file://localhost/E:/My%20Media/C1.MP4")
        self.assertEqual(root.find(".//asset").get("name"), "C1.MP4")
        self.assertEqual(K.file_url("/Volumes/SSD/a b.wav"), "file://localhost/Volumes/SSD/a%20b.wav")

    def test_ntsc_rates_are_exact(self):
        from fractions import Fraction
        self.assertEqual(K.frame_duration(23.976), Fraction(1001, 24000))
        self.assertEqual(K.frame_duration(29.97), Fraction(1001, 30000))
        self.assertEqual(K.frame_duration(25), Fraction(1, 25))

    def test_names_and_the_note(self):
        self.assertEqual(K.curve_name("a.wav", {"a.wav (Buddy curve)"}), "a.wav (Buddy curve 2)")
        self.assertTrue(K.is_curve_name("a.wav (Buddy curve 2)"))
        self.assertFalse(K.is_curve_name("a.wav"))
        keys = [{"t": 1.0, "db": -3.0, "ease": True}]
        self.assertEqual(K.read_note(K.note(keys, "c9")), {"original": "c9", "keys": keys})
        self.assertIsNone(K.read_note("my own comment"))


@unittest.skipUnless(HAVE_DEPS, "numpy / PySide6 not installed")
class WriteTests(unittest.TestCase):
    def setUp(self):
        mic = MediaPoolItem("m2", r"D:\shoot\mic.wav")
        self.b = Item("c2", "mic.wav", 86424, 86800, mic, volume=22.0, fades=(12.0, 6.0), isolation=(False, 0.0))
        self.c = Item("c3", "mic.wav", 86800, 87000, mic)
        self.timeline = Timeline([{"name": "Mic", "items": [self.b, self.c]}])
        self.controller = Controller(self.timeline)

    def test_apply_writes_reads_back_and_undo_puts_it_back(self):
        result = resolve_ext.apply(self.controller, {
            "c2": {"props": {"AudioVolume": -3.0, "AudioPan": 20.0}, "fades": {"FadeIn": 24}},
            "c3": {"props": {"AudioVolume": 1.5}}})
        self.assertEqual(result["failed"], [])
        self.assertEqual((result["after"]["c2"]["volume"], result["after"]["c2"]["pan"]), (-3.0, 20.0))
        self.assertEqual((result["after"]["c2"]["fade_in"], result["after"]["c2"]["fade_out"]), (24.0, 6.0))
        self.assertEqual(result["before"]["c2"], {"props": {"AudioVolume": 22.0, "AudioPan": 0.0},
                                                  "fades": {"FadeIn": 12, "FadeOut": 6}})
        undone = resolve_ext.apply(self.controller, result["before"])
        self.assertEqual(undone["failed"], [])
        self.assertEqual((self.b.props["AudioVolume"], self.b.props["AudioPan"], self.b.fades), (22.0, 0.0, (12.0, 6.0)))
        self.assertEqual(self.c.props["AudioVolume"], 0.0)

    def test_a_refused_clip_is_left_whole(self):
        # c3 has no Voice Isolation: Resolve refuses its whole change.
        result = resolve_ext.apply(self.controller, {
            "c3": {"props": {"AudioVolume": -6.0, "AudioVoiceIsolationEnabled": True}},
            "gone": {"props": {"AudioVolume": 0.0}}})
        self.assertEqual(sorted(result["failed"]), ["c3", "gone"])
        self.assertEqual(self.c.props["AudioVolume"], 0.0)
        self.assertNotIn("c3", result["before"])

    def test_a_refused_fade_puts_the_properties_back(self):
        result = resolve_ext.apply(self.controller, {"c3": {"props": {"AudioVolume": -6.0}, "fades": {"FadeIn": 500}}})
        self.assertEqual(result["failed"], ["c3"])
        self.assertEqual(self.c.props["AudioVolume"], 0.0)

    def test_crossfade_counts_the_refusals(self):
        self.c.name = "no handles"
        added, failed = resolve_ext.crossfade(self.controller, ["c2", "c3"], "Cross Fade 0 dB", 12)
        self.assertEqual((added, failed), (1, ["c3"]))
        self.assertEqual(self.b.transition, {"type": "Cross Fade 0 dB", "category": "audio", "position": "end",
                                             "alignment": "center", "duration": 12})

    def test_the_child_process_seeks_and_reports(self):
        import json
        import os
        import tempfile
        from pages.audio_assistant import resolve_child
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            code = resolve_child.main(path, {"do": "seek", "timecode": "01:00:05:03"}, connect=lambda: self.controller)
            self.assertEqual(code, 0)
            self.assertEqual(self.timeline.moved_to, "01:00:05:03")
            self.timeline.SetCurrentTimecode = lambda _tc: False       # playing: Resolve refuses
            self.assertEqual(resolve_child.main(path, {"do": "seek", "timecode": "01:00:06:00"},
                                                connect=lambda: self.controller), 2)
            with open(path, encoding="utf-8") as f:
                self.assertFalse(json.load(f)["ok"])
            self.assertEqual(resolve_child.main(path, {"do": "nope"}, connect=lambda: self.controller), 2)
        finally:
            os.remove(path)


@unittest.skipUnless(HAVE_DEPS, "numpy / PySide6 not installed")
class MixerTests(unittest.TestCase):
    def setUp(self):
        from pages.audio_assistant import levels
        self.levels = levels
        self.clips = {
            "a": {"id": "a", "start": 0, "end": 100, "volume": 22.0, "fade_in": 10, "fade_out": 0,
                  "isolation": {"on": False, "amount": 0}, "leveler": None},
            "b": {"id": "b", "start": 100, "end": 150, "volume": -3.0, "fade_in": 0, "fade_out": 0,
                  "isolation": None, "leveler": {"on": False, "mode": 0}},
            "x": {"id": "x", "start": 95, "end": 105, "transition": True},
        }

    def test_set_to_and_change_by(self):
        self.assertEqual(self.levels.changes(self.clips, {"ids": ["a", "b"], "volume": -6})["b"],
                         {"props": {"AudioVolume": -6.0}})
        by = self.levels.changes(self.clips, {"ids": ["a", "b"], "volume_by": 10})
        self.assertEqual((by["a"]["props"]["AudioVolume"], by["b"]["props"]["AudioVolume"]), (30.0, 7.0))  # +32 caps at +30

    def test_features_go_only_to_clips_that_have_them(self):
        out = self.levels.changes(self.clips, {"ids": ["a", "b"], "isolation": {"on": True, "amount": 250}})
        self.assertEqual(out, {"a": {"props": {"AudioVoiceIsolationEnabled": True, "AudioVoiceIsolationAmount": 100}}})
        out = self.levels.changes(self.clips, {"ids": ["a", "b"], "leveler": {"on": True, "mode": 2, "gain": 9}})
        self.assertEqual(out["b"]["props"], {"AudioDialogueLevelerEnabled": True, "AudioDialogueLevelerMode": 2.0,
                                             "AudioDialogueLevelerOutputGain": 6.0})
        self.assertNotIn("a", out)

    def test_fades_never_outrun_the_clip(self):
        out = self.levels.changes(self.clips, {"ids": ["b"], "fade_in": 30, "fade_out": 40})
        self.assertEqual(out["b"]["fades"], {"FadeIn": 30, "FadeOut": 20})
        out = self.levels.changes(self.clips, {"ids": ["a"], "fade_out": 200.4})
        self.assertEqual(out["a"]["fades"], {"FadeOut": 90})           # 100 frames, 10 of them fading in
        self.assertEqual(self.levels.changes(self.clips, {"ids": ["x"], "volume": 0}), {})   # not a clip

    def test_cuts_between_selected_clips(self):
        tracks = [{"clips": [self.clips["a"], self.clips["b"]]}]
        self.assertEqual(self.levels.cuts(tracks, ["a", "b"]), ["a"])
        self.assertEqual(self.levels.cuts(tracks, ["a"]), [])
        tracks[0]["clips"].append(self.clips["x"])                     # a crossfade is there already
        self.assertEqual(self.levels.cuts(tracks, ["a", "b"]), [])

    def test_every_preset_has_one_target(self):
        for preset in self.levels.PRESETS:
            self.assertEqual(len({"loudness", "peak"} & set(preset)), 1, preset["id"])


@unittest.skipUnless(HAVE_DEPS, "numpy / PySide6 not installed")
class MatchTests(unittest.TestCase):
    """Match: the clip volume that lands a clip on a target, by Buddy's own BS.1770 sums."""

    def setUp(self):
        from pages.audio_assistant import levels
        self.levels = levels
        rate, fps = 48000, 24.0
        t = np.arange(rate * 20) / rate
        # One file: 10 s of tone at -20 LUFS (both channels at -20 dBFS... see peaks' test), then 10 s at -30.
        tone = np.sin(2 * np.pi * 997 * t) * np.where(t < 10, 0.1, 0.1 / np.sqrt(10))
        loud = peaks.LoudnessAccumulator()
        acc = peaks.PeakAccumulator()
        stereo = np.stack([tone, tone], 1)
        for s in range(0, len(stereo), 4800):
            loud.add(stereo[s:s + 4800], rate)
            acc.add(stereo[s:s + 4800], rate)
        self.audio = {"codes": np.frombuffer(acc.codes(), np.uint8), "loud": np.frombuffer(loud.energies(), np.float32),
                      "rate": peaks.PEAK_RATE, "floor": peaks.FLOOR_DB, "block": peaks.LOUD_BLOCK_S}
        self.fps = fps
        # Clip "a" plays the loud half at +0 dB, "b" the quiet half at +5 dB.
        self.clips = {
            "a": {"id": "a", "start": 0, "end": 240, "offset": 0.0, "volume": 0.0, "media": {"key": "f"}},
            "b": {"id": "b", "start": 240, "end": 480, "offset": 10.0, "volume": 5.0, "media": {"key": "f"}},
            "c": {"id": "c", "start": 480, "end": 500, "offset": 0.0, "volume": 0.0, "media": None},
        }
        self.audio_of = lambda clip: self.audio if clip.get("media") else None

    def lufs(self, uid, volume):
        clip = dict(self.clips[uid], volume=volume)
        return self.levels.clip_lufs(clip, self.audio, self.fps)

    def test_each_clip_lands_on_the_target(self):
        self.assertAlmostEqual(self.lufs("a", 0.0), -20.0, delta=0.1)
        self.assertAlmostEqual(self.lufs("b", 5.0), -25.0, delta=0.1)
        youtube = self.levels.PRESET_IDS["youtube"]
        change, report = self.levels.match(self.clips, ["a", "b", "c"], self.audio_of, self.fps, youtube)
        self.assertEqual(report["missing"], ["c"])
        for uid in ("a", "b"):
            volume = change[uid]["props"]["AudioVolume"]
            self.assertAlmostEqual(self.lufs(uid, volume), -14.0, delta=0.1)

    def test_together_moves_every_clip_by_the_same(self):
        change, _report = self.levels.match(self.clips, ["a", "b"], self.audio_of, self.fps,
                                            self.levels.PRESET_IDS["ebu"], independent=False)
        moved = [change[u]["props"]["AudioVolume"] - self.clips[u]["volume"] for u in ("a", "b")]
        self.assertAlmostEqual(moved[0], moved[1], delta=0.11)

    def test_peak_target_and_the_volume_limit(self):
        change, _ = self.levels.match(self.clips, ["a"], self.audio_of, self.fps, self.levels.PRESET_IDS["peak"])
        self.assertAlmostEqual(change["a"]["props"]["AudioVolume"], 19.0, delta=0.3)    # -20 dBFS peak to -1
        # Turned right down, a clip still measures (the -70 gate reads it before its volume)...
        self.clips["b"]["volume"] = -60.0
        change, report = self.levels.match(self.clips, ["b"], self.audio_of, self.fps, self.levels.PRESET_IDS["youtube"])
        self.assertAlmostEqual(change["b"]["props"]["AudioVolume"], 16.0, delta=0.2)   # -30 LUFS raw, to -14
        # ...and past Resolve's +30 dB it's said.
        change, report = self.levels.match(self.clips, ["b"], self.audio_of, self.fps, {"loudness": 10.0})
        self.assertEqual((change["b"]["props"]["AudioVolume"], report["clamped"]), (30.0, ["b"]))

    def test_keys_are_what_a_clip_is_heard_at(self):
        flat = self.lufs("a", -10.0)
        keyed = dict(self.clips["a"], keys=[{"t": 0.0, "db": -10.0, "ease": False}])      # keyed in Resolve
        self.assertAlmostEqual(self.levels.clip_lufs(keyed, self.audio, self.fps), flat, delta=0.05)
        curve = dict(keyed, volume=5.0, curve={"original": "x"})                          # a Buddy curve: +5 on top
        self.assertAlmostEqual(self.levels.clip_lufs(curve, self.audio, self.fps), flat + 5.0, delta=0.05)
        self.assertAlmostEqual(self.levels.clip_peak(curve, self.audio, self.fps), -20.0 - 10.0 + 5.0, delta=0.3)

    def test_a_clip_keyed_in_resolve_keeps_its_volume(self):
        self.clips["a"]["keys"] = [{"t": 0.0, "db": -10.0, "ease": False}]
        change, report = self.levels.match(self.clips, ["a", "b"], self.audio_of, self.fps,
                                           self.levels.PRESET_IDS["youtube"])
        self.assertEqual((report["keyed"], sorted(change)), (["a"], ["b"]))
        self.assertEqual(self.levels.changes(self.clips, {"ids": ["a", "b"], "volume_by": 3}),
                         {"b": {"props": {"AudioVolume": 8.0}}})

    def test_fades_count_as_the_panel_shows_them(self):
        faded = dict(self.clips["a"], fade_in=120)                                    # half the clip fading in
        self.assertLess(self.levels.clip_lufs(faded, self.audio, self.fps), self.lufs("a", 0.0) - 0.5)


@unittest.skipUnless(HAVE_DEPS, "numpy / PySide6 not installed")
class PeakTests(unittest.TestCase):
    def test_codes_run_from_the_floor_to_full_scale(self):
        codes = peaks.to_codes([0.0, 1e-4, 0.5, 1.0, 2.0])
        self.assertEqual(codes[0], 0)
        self.assertEqual(codes[1], 0)                        # -80 dB: under the floor
        self.assertEqual(codes[2], round((-6.0206 - peaks.FLOOR_DB) / -peaks.FLOOR_DB * 255))
        self.assertEqual(codes[3], 255)
        self.assertEqual(codes[4], 255)

    def test_buffers_that_straddle_peaks_fold_into_one_per_hundredth(self):
        rate = 44100
        t = np.arange(rate * 2) / rate
        wave = np.where(t < 1, 0.5, 0.25) * np.sin(2 * np.pi * 440 * t)
        stereo = np.stack([wave, wave * 0.1], axis=1)
        acc = peaks.PeakAccumulator()
        for start in range(0, len(stereo), 1000):            # 1000 isn't a multiple of 441
            acc.add(stereo[start:start + 1000], rate)
        codes = np.frombuffer(acc.codes(), dtype=np.uint8)
        self.assertEqual(len(codes), 2 * peaks.PEAK_RATE)
        half, quarter = peaks.to_codes([0.5])[0], peaks.to_codes([0.25])[0]
        self.assertTrue(np.all(np.abs(codes[5:95].astype(int) - half) <= 1))
        self.assertTrue(np.all(np.abs(codes[105:195].astype(int) - quarter) <= 1))

    def test_mono_samples_are_taken_too(self):
        acc = peaks.PeakAccumulator()
        acc.add(np.full(480, -1.0), 48000)
        self.assertEqual(acc.codes(), bytes([255]))

    def loudness(self, samples, rate, chunk=1000):
        acc = peaks.LoudnessAccumulator()
        for start in range(0, len(samples), chunk):
            acc.add(samples[start:start + chunk], rate)
        return peaks.integrated_lufs(np.frombuffer(acc.energies(), dtype=np.float32))

    def test_loudness_matches_bs1770(self):
        # A 997 Hz sine at 0 dBFS in one channel is -3.01 LKFS, by the standard's own definition.
        for rate in (48000, 44100):
            t = np.arange(rate * 5) / rate
            sine = np.sin(2 * np.pi * 997 * t)
            self.assertAlmostEqual(self.loudness(np.stack([sine, np.zeros_like(sine)], 1), rate), -3.01, delta=0.05)
            self.assertAlmostEqual(self.loudness(np.stack([sine, sine], 1) * 0.1, rate), -20.0, delta=0.05)

    def test_loudness_gates_out_silence(self):
        rate = 48000
        t = np.arange(rate * 4) / rate
        tone = 0.1 * np.sin(2 * np.pi * 997 * t)
        with_gap = np.concatenate([tone, np.zeros(rate * 4), tone])[:, None]
        # Averaged plainly, 4 s of silence in 12 would cost 1.76 dB. Gated, only the few
        # blocks that straddle an edge (part tone, within 10 LU) count - about 0.2 dB.
        self.assertAlmostEqual(self.loudness(with_gap, rate), self.loudness(tone[:, None], rate), delta=0.3)
        self.assertIsNone(self.loudness(np.zeros((rate, 1)), rate))

    def test_cache_round_trip(self):
        import os
        import tempfile
        real = peaks.CACHE_DIR
        with tempfile.TemporaryDirectory() as folder:
            peaks.CACHE_DIR = os.path.join(folder, "peaks")
            media = os.path.join(folder, "a.wav")
            with open(media, "wb") as f:
                f.write(b"RIFF")
            try:
                self.assertIsNone(peaks.load_cached(media))
                peaks.save_cached(media, (b"\x01\x02", b"\x00" * 8))
                self.assertEqual(peaks.load_cached(media), (b"\x01\x02", b"\x00" * 8))
            finally:
                peaks.CACHE_DIR = real


if __name__ == "__main__":
    unittest.main()
