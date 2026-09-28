"""Ask Buddy's edition awareness and read-only timeline diagnostics."""

import unittest
import tempfile
from pathlib import Path

import _paths  # noqa: F401
from pages.manual_chat.agent import ManualAgent
from pages.manual_chat.resolve_ext import focused_clip, project_checkup, resolve_info


class Item:
    def __init__(self, name, start, media):
        self.name, self.start, self.media = name, start, media

    def GetName(self): return self.name
    def GetStart(self): return self.start
    def GetEnd(self): return self.start + 20
    def GetDuration(self): return 20
    def GetUniqueId(self): return self.name
    def GetMediaPoolItem(self): return self.media


class Media:
    def __init__(self, fps="24", size="1920x1080"):
        self.props = {"FPS": fps, "Resolution": size, "File Name": "shot.mov"}

    def GetMediaId(self): return "media-" + self.props["FPS"]
    def GetClipProperty(self, key): return self.props.get(key, "")


class Timeline:
    def __init__(self):
        self.items = [Item("A", 0, Media()), Item("B", 30, Media("30", "1280x720"))]
        self.selected = [self.items[1]]

    def GetSelectedClips(self): return self.selected
    def GetCurrentTimecode(self): return "00:00:00:05"
    def GetName(self): return "Edit"
    def GetTrackCount(self, kind): return 1 if kind == "video" else 0
    def GetItemListInTrack(self, kind, track): return self.items
    def GetSetting(self, key): return {"timelineFrameRate": "24", "timelineResolutionWidth": "1920",
                                       "timelineResolutionHeight": "1080"}.get(key, "")
    def GetStartFrame(self): return 0
    def GetEndFrame(self): return 50


class Project:
    def __init__(self): self.timeline = Timeline()
    def GetCurrentTimeline(self): return self.timeline
    def GetName(self): return "My project"
    def GetSetting(self, key): return ""


class Resolve:
    def GetProductName(self): return "DaVinci Resolve"
    def GetVersionString(self): return "21.1"
    def GetCurrentPage(self): return "edit"


class Controller:
    def __init__(self): self.project, self.resolve = Project(), Resolve()
    def current_project(self): return self.project


class ContextTests(unittest.TestCase):
    def test_explain_clip_prefetches_live_context_before_model_reply(self):
        class LLM:
            def chat(self, system, messages, specs):
                self.question = messages[-1]["content"]
                return type("Reply", (), {"wants_tools": False, "content": "Here is the clip."})()
        llm = LLM()
        controller = Controller()
        agent = ManualAgent(None, llm, connect_resolve=lambda: controller,
                            edition=resolve_info(controller), prefetch_focus=True)
        result = agent.ask("Explain clip")
        self.assertIn('"name": "B"', llm.question)
        self.assertEqual(result.events[0].name, "project_state")

    def test_selected_clip_beats_playhead_and_reports_mismatch(self):
        controller = Controller()
        focus = focused_clip(controller)
        self.assertEqual(focus["mode"], "selected timeline clip")
        self.assertEqual(focus["clips"][0]["name"], "B")
        self.assertEqual(len(focus["clips"][0]["mismatch"]), 2)

    def test_old_resolve_selection_falls_back_to_playhead(self):
        controller = Controller()
        controller.project.timeline.GetSelectedClips = None
        focus = focused_clip(controller)
        self.assertEqual(focus["mode"], "clip at playhead")
        self.assertEqual(focus["clips"][0]["name"], "A")

    def test_checkup_reports_scan_limit_and_real_mismatch_only(self):
        report = project_checkup(Controller(), max_items=1)
        self.assertEqual(report["scanned_clips"], 1)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["issues"], [])
        full = project_checkup(Controller())
        self.assertEqual(full["issues"][0]["clip"], "B")
        self.assertEqual(len(full["issues"][0]["details"]), 2)

    def test_missing_local_media_path_suggests_relink(self):
        controller = Controller()
        with tempfile.TemporaryDirectory() as folder:
            controller.project.timeline.items[0].media.props["File Path"] = str(Path(folder) / "gone.mov")
            report = project_checkup(controller)
        self.assertTrue(any(i["kind"] == "missing_path" for i in report["issues"]))
        self.assertTrue(any("Media Relink" in item for item in report["recommendations"]))

    def test_edition_is_in_every_agent_prompt(self):
        edition = resolve_info(Controller())
        self.assertFalse(edition["is_studio"])
        agent = ManualAgent(None, None, edition=edition)
        self.assertIn('"is_studio": false', agent._system)
        unknown = ManualAgent(None, None)
        self.assertIn("edition: unknown", unknown._system)


if __name__ == "__main__":
    unittest.main()
