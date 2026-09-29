"""Bulk edits touch only chosen fields and report Resolve's actual readback."""

import unittest

import _paths  # noqa: F401
from pages.project_setup import metadata


class MetadataClip:
    def __init__(self, name, **props):
        self.props = {"Type": "Video", "Clip Name": name, "Tag": "0", "Start TC": "00:00:00:00", **props}
        self.meta = {}
        self.color = ""
        self.writes = []
        self.refuse = set()

    def GetUniqueId(self): return str(id(self))
    def GetName(self): return self.props["Clip Name"]
    def GetClipProperty(self, key=None): return self.props.get(key, "") if key else dict(self.props)
    def GetMetadata(self): return dict(self.meta)
    def GetClipColor(self): return self.color

    def SetMetadata(self, changes):
        self.writes.append(changes)
        for key, value in changes.items():
            if key in self.refuse:
                return False
            self.meta[key] = value
        return True

    def SetClipProperty(self, key, value):
        self.writes.append({key: value})
        if key in self.refuse:
            return False
        self.props[key] = value
        return False  # Resolve sometimes returns False after a successful write.

    def SetName(self, value): return self.SetClipProperty("Clip Name", value)

    def SetClipColor(self, color):
        self.color = color
        self.writes.append({"Clip Color": color})
        return True

    def ClearClipColor(self): return self.SetClipColor("")


class MetadataTests(unittest.TestCase):
    def test_mixed_values_and_shared_values(self):
        a = MetadataClip("A", Scene="1", Comments="Keep")
        b = MetadataClip("B", Scene="2", Comments="Keep")
        snapshot = metadata.snapshot([a, b])
        fields = {f["key"]: f for f in snapshot["fields"]}
        self.assertEqual(snapshot["count"], 2)
        self.assertTrue(fields["Scene"]["mixed"])
        self.assertEqual(fields["Comments"]["value"], "Keep")
        self.assertFalse(fields["Comments"]["mixed"])
        self.assertEqual(fields["Date Created"]["kind"], "readonly")

    def test_only_chosen_fields_are_written_for_every_clip(self):
        clips = [MetadataClip("A", Comments="First"), MetadataClip("B", Comments="Second")]
        self.assertEqual(metadata.apply(clips, {"Scene": "12", "Camera #": "B"}), (2, []))
        for clip in clips:
            self.assertEqual(clip.meta, {"Scene": "12", "Camera #": "B"})
        self.assertEqual([metadata.values(c)["Comments"] for c in clips], ["First", "Second"])

    def test_clear_color_clear_text_tags_reel_and_rename(self):
        clip = MetadataClip("Original", Comments="Clear me")
        clip.color = "Orange"
        changes = {"Comments": "", "Clip Color": "", "Clip Name": "New name", "Tag": "-1", "Reel Number": "R2"}
        self.assertEqual(metadata.apply([clip], changes), (1, []))
        self.assertEqual({k: metadata.values(clip)[k] for k in changes}, changes)
        self.assertIn({"Tag": "-1"}, clip.writes)

    def test_refusals_are_reported_and_other_clips_still_update(self):
        a, b = MetadataClip("A"), MetadataClip("B")
        a.refuse.add("Scene")
        updated, failures = metadata.apply([a, b], {"Scene": "2", "Take": "3"})
        self.assertEqual(updated, 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("A — Scene", failures[0])
        self.assertEqual(metadata.values(a)["Take"], "3")
        self.assertEqual(metadata.values(b)["Scene"], "2")

    def test_invalid_edits_write_nothing(self):
        clip = MetadataClip("A")
        for changes in ({}, {"Date Created": "2026-01-01"}, {"Clip Name": " "},
                        {"Start TC": "bad"}, {"Start TC": "10:80:00:00"},
                        {"Tag": "unknown"}, {"Clip Color": "Red"}, {"Scene": None}):
            with self.subTest(changes=changes), self.assertRaises(metadata.MetadataError):
                metadata.apply([clip], changes)
        self.assertEqual(clip.writes, [])

    def test_selection_excludes_timelines_and_does_not_fall_back_to_a_bin(self):
        from types import SimpleNamespace
        a, timeline = MetadataClip("A"), MetadataClip("Timeline", Type="Timeline")
        pool = SimpleNamespace(GetSelectedClips=lambda: [a, timeline])
        project = SimpleNamespace(GetMediaPool=lambda: pool)
        self.assertEqual(metadata.selected_clips(project), [a])
        pool.GetSelectedClips = lambda: []
        self.assertEqual(metadata.selected_clips(project), [])
        pool.GetSelectedClips = None
        with self.assertRaises(metadata.MetadataError):
            metadata.selected_clips(project)


if __name__ == "__main__":
    unittest.main()
