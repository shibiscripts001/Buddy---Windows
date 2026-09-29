"""The Proxy tab's pure logic: naming and claiming proxy paths, video
detection, the ffmpeg command, render/part-file behaviour, and the
report lines. No Qt, no Resolve, no real ffmpeg - render_one's process
call is replaced."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.project_setup import proxy


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, rel, content=b"x"):
        path = os.path.join(self.dir, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path


class EntryTests(unittest.TestCase):
    def _clip(self, name="A001", path="C:/media/A001.mp4", type_="video", uid="u1",
              props=None, fail_props=False):
        class Clip:
            def GetClipProperty(self, *a):
                if fail_props:
                    raise RuntimeError("bridge went away")
                return props if a == () else (props or {}).get(a[0])
            def GetName(self):
                return name
            def GetUniqueId(self):
                return uid
        return Clip()

    def test_entry_reads_path_name_and_type(self):
        clip = self._clip(props={"File Path": "C:/media/A001.mp4", "Type": "video"})
        entry = proxy._entry(clip)
        self.assertEqual(entry, {"id": "u1", "name": "A001", "path": "C:/media/A001.mp4", "type": "video", "tc": ""})

    def test_entry_survives_a_dead_bridge(self):
        self.assertIsNone(proxy._entry(self._clip(fail_props=True)))

    def test_entries_dedupes_by_id(self):
        clip = self._clip(props={"File Path": "p", "Type": "video"})
        entries, objects = proxy.entries([clip, clip])
        self.assertEqual(len(entries), 1)
        self.assertEqual(list(objects), ["u1"])


class IsVideoTests(unittest.TestCase):
    def entry(self, path, type_=""):
        return {"id": "x", "name": "n", "path": path, "type": type_}

    def test_video_extensions_and_types(self):
        for path in ("a.mp4", "a.MOV", "a.braw", "a.mxf"):
            self.assertTrue(proxy.is_video(self.entry(path)), path)
        self.assertTrue(proxy.is_video(self.entry("weird container", "video")))
        self.assertTrue(proxy.is_video(self.entry("weird container", "Movie")))

    def test_audio_image_and_unknown_are_not_video(self):
        for path in ("a.wav", "a.mp3", "a.jpg", "a.tif", "a.dpx", "a.exr"):
            self.assertFalse(proxy.is_video(self.entry(path)), path)
        self.assertFalse(proxy.is_video(self.entry("no extension at all")))


class ClaimTests(_Tmp):
    def test_first_proxy_takes_the_plain_name(self):
        src = self.touch("footage/A001.mp4")
        kind, dst = proxy._claim(src, {})
        self.assertEqual(kind, "render")
        self.assertEqual(dst, os.path.join(self.dir, "footage", "Proxy", "A001_proxy.mov"))

    def test_existing_file_is_adopted_once_then_numbered(self):
        src = self.touch("footage/A001.mp4")
        self.touch("footage/Proxy/A001_proxy.mov", b"old")
        claimed = {}
        first = proxy._claim(src, claimed)
        second = proxy._claim(src, claimed)
        self.assertEqual(first[0], "existing")
        self.assertEqual(second[0], "render")
        self.assertEqual(second[1], os.path.join(self.dir, "footage", "Proxy", "A001_proxy_2.mov"))

    def test_same_stem_different_folders_do_not_collide(self):
        a = self.touch("camA/A001.mp4")
        b = self.touch("camB/A001.mp4")
        claimed = {}
        _, dst_a = proxy._claim(a, claimed)
        _, dst_b = proxy._claim(b, claimed)
        self.assertNotEqual(dst_a, dst_b)

    def test_plan_splits_renders_existing_and_skips(self):
        self.touch("footage/A001.mp4")
        self.touch("footage/A002.mp4")
        self.touch("footage/Proxy/A002_proxy.mov", b"old")
        videos = [
            {"id": "1", "name": "A001", "path": os.path.join(self.dir, "footage", "A001.mp4"), "type": "video"},
            {"id": "2", "name": "A002", "path": os.path.join(self.dir, "footage", "A002.mp4"), "type": "video"},
            {"id": "3", "name": "gone", "path": os.path.join(self.dir, "footage", "missing.mp4"), "type": "video"},
            {"id": "4", "name": "nopath", "path": "", "type": "video"},
        ]
        result = proxy.plan(videos)
        self.assertEqual([t["id"] for t in result["render"]], ["1"])
        self.assertEqual([t["id"] for t in result["existing"]], ["2"])
        self.assertEqual(len(result["skipped"]), 2)
        self.assertTrue(any("offline" in s for s in result["skipped"]))
        self.assertTrue(any("no file behind" in s for s in result["skipped"]))


class CommandTests(unittest.TestCase):
    def test_command_shape(self):
        cmd = proxy.build_command("ffmpeg", "in.mov", "out.mov", "half", "h264")
        self.assertEqual(cmd[:7], ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", "in.mov"][:7])
        self.assertIn("-vf", cmd)
        self.assertIn("0:v:0", cmd)
        self.assertIn("0:a?", cmd)
        self.assertIn("copy", cmd)
        self.assertEqual(cmd[-1], "out.mov")
        # No -r anywhere: a proxy must keep its source's frame rate.
        self.assertNotIn("-r", cmd)

    def test_original_resolution_has_no_scale_filter(self):
        cmd = proxy.build_command("ffmpeg", "in.mov", "out.mov", "original", "prores")
        self.assertNotIn("-vf", cmd)
        self.assertIn("prores_ks", cmd)

    def test_unknown_choice_is_refused(self):
        with self.assertRaises(proxy.ProxyError):
            proxy.build_command("ffmpeg", "in", "out", "enormous", "h264")
        with self.assertRaises(proxy.ProxyError):
            proxy.build_command("ffmpeg", "in", "out", "half", "av1")


class RenderTests(_Tmp):
    def test_success_writes_part_then_renames(self):
        src = self.touch("footage/A001.mp4")
        dst = os.path.join(self.dir, "footage", "Proxy", "A001_proxy.mov")

        def fake_run(cmd):
            part = cmd[-1]
            self.assertTrue(part.endswith(proxy.PART_EXT))
            with open(part, "wb") as fh:
                fh.write(b"proxy!")
            return 0, b""
        out = proxy.render_one("ffmpeg", src, dst, "half", "h264", run=fake_run)
        self.assertEqual(out, dst)
        self.assertTrue(os.path.isfile(dst))
        self.assertFalse(os.path.exists(dst + proxy.PART_EXT))

    def test_failure_removes_the_part_file(self):
        src = self.touch("footage/A001.mp4")
        dst = os.path.join(self.dir, "footage", "Proxy", "A001_proxy.mov")

        def fake_run(cmd):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"half a proxy")
            return 1, b"stderr: could not write header"
        with self.assertRaises(proxy.ProxyError) as raised:
            proxy.render_one("ffmpeg", src, dst, "half", "h264", run=fake_run)
        self.assertIn("A001.mp4", str(raised.exception))
        self.assertIn("could not write header", str(raised.exception))
        self.assertFalse(os.path.exists(dst + proxy.PART_EXT))
        self.assertFalse(os.path.exists(dst))

    def test_missing_proxy_folder_is_created(self):
        src = self.touch("footage/A001.mp4")
        dst = os.path.join(self.dir, "footage", "Proxy", "A001_proxy.mov")

        def fake_run(cmd):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"p")
            return 0, b""
        proxy.render_one("ffmpeg", src, dst, "half", "h264", run=fake_run)
        self.assertTrue(os.path.isfile(dst))


class TerminateTests(unittest.TestCase):
    def test_terminate_kills_registered_processes(self):
        class P:
            def __init__(self):
                self.killed = False
            def kill(self):
                self.killed = True
        p = P()
        with mock.patch.object(proxy, "_ACTIVE", {p}):
            self.assertEqual(proxy.terminate_active(), 1)
        self.assertTrue(p.killed)


class ScopeTests(unittest.TestCase):
    def test_scope_labels(self):
        self.assertEqual(proxy.scope_label("selection"), "the Media Pool selection")
        self.assertEqual(proxy.scope_label("bin", "Footage"), "bin 'Footage'")
        self.assertEqual(proxy.scope_label("bin", "Footage", recursive=True), "bin 'Footage' and its sub-bins")
        self.assertEqual(proxy.scope_label("bin", "Master"), "Master")
        self.assertEqual(proxy.scope_label("timeline", None, "Cut 1"), "the selection on 'Cut 1'")
        self.assertEqual(proxy.scope_label("all"), "every clip in the project")

    def test_timeline_selection_needs_21_0_4(self):
        class OldTimeline:
            pass  # no GetSelectedClips in dir()
        self.assertFalse(proxy.timeline_can_select(OldTimeline()))
        with self.assertRaises(proxy.ProxyError) as raised:
            proxy.selected_timeline(OldTimeline())
        self.assertIn("21.0.4", str(raised.exception))

    def test_timeline_selection_maps_items_to_pool_media(self):
        class Item:
            def __init__(self, media):
                self._media = media
            def GetMediaPoolItem(self):
                return self._media
        class Media:
            def GetClipProperty(self, *a):
                return {"File Path": "C:/m/v.mp4", "Type": "video"} if a == () else {"File Path": "C:/m/v.mp4", "Type": "video"}.get(a[0])
            def GetName(self):
                return "v"
            def GetUniqueId(self):
                return "id1"
        class Timeline:
            def GetSelectedClips(self):
                return [Item(Media()), Item(None)]
        entries, objects = proxy.selected_timeline(Timeline())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], "C:/m/v.mp4")

    def test_pool_selection_filters_timelines_and_none(self):
        class Clip:
            def GetClipProperty(self, *a):
                return {"Type": "video", "File Path": "p"} if a == () else {"Type": "video", "File Path": "p"}.get(a[0])
            def GetName(self):
                return "c"
            def GetUniqueId(self):
                return "id"
        class TimelinePseudoClip(Clip):
            def GetClipProperty(self, *a):
                return {"Type": "Timeline"} if a == () else "Timeline"
        class Pool:
            def GetSelectedClips(self):
                return [Clip(), None, TimelinePseudoClip()]
        class Project:
            def GetMediaPool(self):
                return Pool()
        entries, _objects = proxy.selected_pool(Project())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "id")

    def test_pool_selection_reports_old_resolve(self):
        class Pool:
            def GetSelectedClips(self):
                raise RuntimeError("no such method")
        class Project:
            def GetMediaPool(self):
                return Pool()
        with self.assertRaises(proxy.ProxyError) as raised:
            proxy.selected_pool(Project())
        self.assertIn("could not read the Media Pool selection", str(raised.exception))


class ReportTests(unittest.TestCase):
    def test_render_report(self):
        lines = proxy.render_report([{"id": "a", "ok": True}, {"id": "b", "ok": False, "error": "boom"}])
        self.assertEqual(lines, [("Rendered 1 of 2 proxies.", "success"), ("boom", "error")])
        self.assertEqual(proxy.render_report([{"id": "a", "ok": True}]), [("Rendered 1 proxy.", "success")])

    def test_link_report(self):
        lines = proxy.link_report(2, ["Bad Clip"], 3)
        self.assertEqual(lines[0], ("Linked 2 clips to their proxies.", "success"))
        self.assertEqual(lines[1], ('Resolve wouldn\'t link "Bad Clip".', "error"))
        self.assertEqual(lines[2], ("3 clips already had proxy files – linked instead of rendering.", "info"))
        self.assertEqual(proxy.link_report(1, [], 0), [("Linked 1 clip to its proxy.", "success")])
        self.assertEqual(proxy.link_report(0, [], 1),
                         [("1 clip already had a proxy file – linked to it instead of rendering.", "info")])


class TimecodeTests(unittest.TestCase):
    """Resolve only links a proxy whose timecode matches the clip's."""

    def test_clean_and_compare(self):
        self.assertEqual(proxy.clean_timecode(" 19:45:12:08 "), "19:45:12:08")
        self.assertEqual(proxy.clean_timecode("01:00:00;02"), "01:00:00;02")   # drop frame
        for bad in ("", None, "19:45:12", "-y -i evil", "19:45:12:08 -f"):
            self.assertEqual(proxy.clean_timecode(bad), "", bad)
        self.assertTrue(proxy.same_timecode("01:00:00;02", "01:00:00:02"))
        self.assertTrue(proxy.same_timecode("", "00:00:00:00"))   # no timecode reads as zero
        self.assertFalse(proxy.same_timecode("", "19:45:12:08"))

    def test_the_entry_carries_resolves_start_timecode(self):
        class Clip:
            def GetClipProperty(self, *a):
                return {"File Path": "C:/m/A.mp4", "Clip Name": "A.mp4", "Type": "Video", "Start TC": "19:45:12:08"}

            def GetUniqueId(self):
                return "u"
        self.assertEqual(proxy._entry(Clip())["tc"], "19:45:12:08")

    def test_the_command_stamps_it(self):
        cmd = proxy.build_command("ffmpeg", "in.mp4", "out.mov", "half", "h264", "19:45:12:08")
        self.assertEqual(cmd[cmd.index("-timecode") + 1], "19:45:12:08")
        self.assertEqual(cmd[-1], "out.mov")
        self.assertNotIn("-timecode", proxy.build_command("ffmpeg", "in.mp4", "out.mov", "half", "h264", "bogus"))

    def test_reading_a_files_timecode(self):
        with mock.patch.object(proxy, "_ffprobe_path", return_value="ffprobe"), \
             mock.patch.object(proxy.subprocess, "run", return_value=mock.Mock(
                 returncode=0, stdout=b'{"streams": [{"tags": {}}, {"tags": {"timecode": "19:45:12:08"}}]}')):
            self.assertEqual(proxy.file_timecode("ffmpeg", "x.mov"), "19:45:12:08")
        with mock.patch.object(proxy, "_ffprobe_path", return_value=None), \
             mock.patch.object(proxy.subprocess, "run", return_value=mock.Mock(
                 returncode=1, stderr=b"  Metadata:\n    timecode        : 01:00:00;02\n")):
            self.assertEqual(proxy.file_timecode("ffmpeg", "x.mov"), "01:00:00;02")
        with mock.patch.object(proxy, "_ffprobe_path", return_value=None), \
             mock.patch.object(proxy.subprocess, "run", side_effect=OSError("gone")):
            self.assertEqual(proxy.file_timecode("ffmpeg", "x.mov"), "")

    def test_link_report_says_what_was_redone_and_refused(self):
        lines = proxy.link_report(2, [], 0, redone=2, refused_existing=["B.mp4"])
        texts = [t for t, _tone in lines]
        self.assertIn("2 proxy files already there had the wrong timecode – rendered again.", texts)
        self.assertTrue(any("B.mp4" in t and "delete it" in t for t in texts), texts)


class FormatTests(unittest.TestCase):
    def test_every_format_builds_a_command(self):
        self.assertIn("h264", proxy.CODECS)
        self.assertIn("prores", proxy.CODECS)   # saved choices from before there were more still work
        for key, codec in proxy.CODECS.items():
            cmd = proxy.build_command("ffmpeg", "in.mp4", "out.mov", "half", key)
            self.assertIn("-c:v", cmd, key)
            self.assertTrue(codec["label"] and codec["hint"], key)
        self.assertEqual([c["id"] for c in proxy.codec_choices()], list(proxy.CODECS))

    def test_dnxhr_and_prores_profiles(self):
        cmd = proxy.build_command("ffmpeg", "in.mp4", "out.mov", "half", "dnxhr_sq")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "dnxhr_sq")
        cmd = proxy.build_command("ffmpeg", "in.mp4", "out.mov", "half", "prores_lt")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "1")


if __name__ == "__main__":
    unittest.main()
