"""Dailies catalog, shorthand and metadata writes without Resolve or Qt."""

import unittest

import _paths  # noqa: F401
from pages.dailies import model, resolve_ext


class Clip:
    def __init__(self, uid, name, path, kind="Video"):
        self.uid = uid
        self.name = name
        self.props = {"Clip Name": name, "File Path": path, "Type": kind, "Duration": "00:00:10:00"}
        self.meta = {"Comments": "Previous note", "Tag": "0"}

    def GetUniqueId(self): return self.uid
    def GetName(self): return self.name
    def GetClipProperty(self, key=None): return self.props if key is None else self.props.get(key, "")
    def GetMetadata(self, key=None): return self.meta if key is None else self.meta.get(key, "")
    def SetMetadata(self, changes): self.meta.update(changes); return True
    def GetClipColor(self): return ""


class Folder:
    def __init__(self, uid, name, clips=(), children=()):
        self.uid, self.name, self.clips, self.children = uid, name, list(clips), list(children)

    def GetUniqueId(self): return self.uid
    def GetName(self): return self.name
    def GetClipList(self): return self.clips
    def GetSubFolderList(self): return self.children


class Project:
    def __init__(self, root, current): self.root, self.current = root, current
    def GetUniqueId(self): return "project-1"
    def GetName(self): return "Film"
    def GetMediaPool(self): return self
    def GetRootFolder(self): return self.root
    def GetCurrentFolder(self): return self.current


class Controller:
    def __init__(self, project): self.project = project
    def current_project(self): return self.project


class DailiesTests(unittest.TestCase):
    def test_shorthand_keeps_free_notes_and_updates_metadata(self):
        clip = Clip("one", "Take 1", "C:/footage/a.mov")
        changes = model.changes_for({"log": "S1 sh02 t3 Great look", "tag": "1"}, clip.meta)
        self.assertEqual(changes["Scene"], "1")
        self.assertEqual(changes["Shot"], "2")
        self.assertEqual(changes["Take"], "3")
        self.assertEqual(changes["Tag"], "1")
        self.assertIn("S1 sh02 t3 Great look", changes["Comments"])
        project = Project(Folder("root", "Master", [clip]), None)
        saved, failures, written = resolve_ext.apply_drafts(Controller(project), "project-1", {"one": clip},
                                                             {"one": {"log": "S1 sh02 t3 Great look", "tag": "1"}})
        self.assertEqual((saved, failures), (["one"], []))
        self.assertEqual(written["one"]["Scene"], "1")
        self.assertEqual(clip.meta["Scene"], "1")
        self.assertEqual(clip.meta["Tag"], "1")
        self.assertEqual(model.comments_with_log(clip.meta["Comments"], "S1 sh02 t3 Great look"), clip.meta["Comments"])

    def test_catalog_uses_current_bin_and_excludes_timelines(self):
        first = Clip("a", "A", "C:/a.mov")
        second = Clip("b", "B", "C:/b.wav", "Audio")
        timeline = Clip("tl", "Timeline 1", "", "Timeline")
        child = Folder("child", "Day 1", [second])
        root = Folder("root", "Master", [first, timeline], [child])
        result = resolve_ext.scan(Controller(Project(root, child)))
        self.assertEqual(list(result["clips"]), ["a", "b"])
        self.assertEqual(result["current_ids"], ["b"])
        self.assertEqual(result["current_bin_id"], "child")
        self.assertEqual(result["clips"]["b"]["type"], "Audio")

    def test_location_follows_current_bin_without_scanning_clips(self):
        first = Folder("first", "Day 1")
        second = Folder("second", "Day 2")
        project = Project(Folder("root", "Master", children=[first, second]), first)
        controller = Controller(project)
        self.assertEqual(resolve_ext.current_location(controller), ("project-1", "first"))
        project.current = second
        self.assertEqual(resolve_ext.current_location(controller), ("project-1", "second"))

    def test_project_change_blocks_metadata_write(self):
        clip = Clip("one", "Take 1", "C:/a.mov")
        project = Project(Folder("root", "Master", [clip]), None)
        with self.assertRaisesRegex(Exception, "project changed"):
            resolve_ext.apply_drafts(Controller(project), "different", {"one": clip}, {"one": {"tag": "-1"}})
        self.assertEqual(clip.meta["Tag"], "0")

    def test_clearing_review_log_removes_only_the_buddy_block(self):
        with_note = model.comments_with_log("Previous note", "s1 Good shot")
        changes = model.changes_for({"log": "", "log_edited": True}, {"Comments": with_note})
        self.assertEqual(changes, {"Comments": "Previous note"})

    def test_clip_colour_is_written_or_cleared(self):
        self.assertEqual(model.changes_for({"color": "Teal"}, {"Clip Color": ""}), {"Clip Color": "Teal"})
        self.assertEqual(model.changes_for({"color": ""}, {"Clip Color": "Teal"}), {"Clip Color": ""})
        self.assertEqual(model.changes_for({"color": None}, {"Clip Color": "Teal"}), {})
        self.assertEqual(model.changes_for({"color": "Mauve"}, {}), {})

    def test_clip_length_on_the_tape(self):
        self.assertAlmostEqual(model.clip_seconds("Video", "960", 59.94), 16.016, places=3)
        self.assertAlmostEqual(model.clip_seconds("Video", "240", "23.976 DF"), 10.01, places=2)
        self.assertEqual(model.clip_seconds("Image", "1", 24), model.STILL_SECONDS)
        self.assertEqual(model.clip_seconds("Audio", "", ""), model.STILL_SECONDS)

    def test_typed_scene_overrides_shorthand(self):
        changes = model.changes_for({"log": "s1 Good shot", "fields": {"Scene": "Opening"}}, {})
        self.assertEqual(changes["Scene"], "Opening")
