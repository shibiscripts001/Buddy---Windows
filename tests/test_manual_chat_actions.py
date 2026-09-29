"""Ask Buddy's project changes (app/pages/manual_chat/actions.py) against a
fake Resolve: Apply must land on the timeline that was previewed, clips are
addressed by the index project_state reports, and deleting a bin counts
everything inside it."""

import unittest

import _paths  # noqa: F401
from pages.manual_chat import actions
from pages.manual_chat.resolve_ext import timeline_contents


class FakeItem:
    def __init__(self, name, start=0, duration=10):
        self.name, self.start, self.duration = name, start, duration
        self.color = None

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetDuration(self):
        return self.duration

    def GetMediaPoolItem(self):
        return None

    def SetClipColor(self, color):
        if not getattr(self, "title", False):   # a title says True and keeps nothing (measured)
            self.color = color
        return True

    def GetClipColor(self):
        return self.color or ""


class FakeTimeline:
    def __init__(self, name, uid, tracks=None):
        self.name, self.uid = name, uid
        self.tracks = tracks or []
        self.markers = {}

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetStartFrame(self):
        return 86400

    def GetEndFrame(self):
        return 90000

    def GetMarkers(self):
        return dict(self.markers)

    def AddMarker(self, frame, color, name, note, duration):
        self.markers[frame] = {"color": color, "name": name, "note": note}
        return True

    def GetTrackCount(self, kind):
        return len(self.tracks) if kind == "video" else 0

    def GetItemListInTrack(self, kind, track):
        return self.tracks[track - 1]

    def GetSetting(self, key):
        return ""


class FakeClip:
    def __init__(self, name, kind="Video"):
        self.name, self.kind = name, kind

    def GetClipProperty(self, key):
        return {"Type": self.kind, "Clip Name": self.name}.get(key, "")

    def GetName(self):
        return self.name


class FakeFolder:
    def __init__(self, name, clips=(), subs=()):
        self.name, self.clips, self.subs = name, list(clips), list(subs)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subs


class FakePool:
    def __init__(self, root):
        self.root = root

    def GetRootFolder(self):
        return self.root


class FakeProject:
    def __init__(self, name, uid, timeline, root=None):
        self.name, self.uid, self.timeline = name, uid, timeline
        self.pool = FakePool(root or FakeFolder("Master"))

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetCurrentTimeline(self):
        return self.timeline

    def GetMediaPool(self):
        return self.pool

    def GetTimelineCount(self):
        return 1

    def GetTimelineByIndex(self, n):
        return self.timeline


class FakeController:
    def __init__(self, project):
        self.project = project

    def current_project(self):
        return self.project


def _setup(tracks=None, root=None):
    timeline = FakeTimeline("Edit v1", "tl-1", tracks)
    project = FakeProject("Show", "pr-1", timeline, root)
    return FakeController(project), project, timeline


class TargetCheckTests(unittest.TestCase):
    def test_apply_refused_after_timeline_switch(self):
        controller, project, timeline = _setup()
        proposal = actions.preview(
            controller, "add_markers", {"markers": [{"frame": 10}]}
        )
        other = FakeTimeline("Edit v2", "tl-2")
        project.timeline = other
        with self.assertRaises(actions.ActionError) as ctx:
            actions.execute(controller, proposal)
        self.assertIn("'Edit v1' -> 'Edit v2'", str(ctx.exception))
        self.assertIn("nothing was changed", str(ctx.exception))
        self.assertEqual(other.markers, {})
        self.assertEqual(timeline.markers, {})

    def test_same_name_different_timeline_refused(self):
        controller, project, _ = _setup()
        proposal = actions.preview(
            controller, "add_markers", {"markers": [{"frame": 10}]}
        )
        project.timeline = FakeTimeline("Edit v1", "tl-other")
        with self.assertRaises(actions.ActionError):
            actions.execute(controller, proposal)

    def test_apply_refused_after_project_switch(self):
        controller, project, _ = _setup()
        proposal = actions.preview(controller, "create_bin", {"name": "New"})
        controller.project = FakeProject("Other", "pr-2", project.timeline)
        with self.assertRaises(actions.ActionError) as ctx:
            actions.execute(controller, proposal)
        self.assertIn("project changed", str(ctx.exception))

    def test_project_action_survives_timeline_switch(self):
        controller, project, _ = _setup()
        proposal = actions.preview(controller, "create_bin", {"name": "New"})
        project.timeline = FakeTimeline("Edit v2", "tl-2")
        # Passes the target check; the fake pool has no AddSubFolder, so the
        # execute itself is not exercised here.
        actions._check_target(controller, proposal.action_id, proposal.target)

    def test_apply_on_same_timeline_works(self):
        controller, _, timeline = _setup()
        proposal = actions.preview(
            controller, "add_markers", {"markers": [{"frame": 10}]}
        )
        actions.execute(controller, proposal)
        self.assertIn(10, timeline.markers)


class ClipIndexTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeItem("A", 86400), FakeItem("B", 86410)
        self.controller, _, _ = _setup(tracks=[[self.a, self.b]])

    def test_listing_reports_the_index_actions_take(self):
        items = timeline_contents(self.controller)["items"]
        self.assertEqual(
            [(i["track"], i["index"], i["name"]) for i in items],
            [("V1", 1, "A"), ("V1", 2, "B")],
        )

    def test_index_from_listing_selects_that_clip(self):
        entry = timeline_contents(self.controller)["items"][1]
        proposal = actions.preview(self.controller, "set_clip_colors", {
            "color": "Teal",
            "clips": [{"track": entry["track"], "index": entry["index"],
                       "name": entry["name"], "start": entry["start"]}],
        })
        actions.execute(self.controller, proposal)
        self.assertEqual(self.b.color, "Teal")
        self.assertIsNone(self.a.color)

    def test_mismatched_name_is_refused(self):
        with self.assertRaises(actions.ActionError) as ctx:
            actions.preview(self.controller, "delete_timeline_clips", {
                "clips": [{"track": "V1", "index": 1, "name": "B"}],
            })
        self.assertIn("is 'A', not 'B'", str(ctx.exception))

    def test_mismatched_start_is_refused(self):
        with self.assertRaises(actions.ActionError):
            actions.preview(self.controller, "set_clip_colors", {
                "color": "Teal",
                "clips": [{"track": "V1", "index": 1, "start": 99}],
            })


class FakeResolve:
    def __init__(self, page):
        self.page, self.visited = page, []

    def GetCurrentPage(self):
        return self.page

    def OpenPage(self, page):
        self.page = page
        self.visited.append(page)
        return True


class ResolveTrapTests(unittest.TestCase):
    """What Resolve says isn't always what it did (both measured by
    github.com/samuelgursky/davinci-resolve-mcp)."""

    def setUp(self):
        self.a, self.b = FakeItem("A", 86400), FakeItem("Lower third", 86410)
        self.b.title = True
        self.controller, _, self.timeline = _setup(tracks=[[self.a, self.b]])
        self.resolve = self.controller.resolve = FakeResolve("fairlight")
        track = self.timeline.tracks[0]

        def delete(items, ripple=False):
            if self.resolve.page != "edit":
                return False                       # only on the Edit page
            for item in items:
                track.remove(item)
            return True
        self.timeline.DeleteClips = delete

    def test_a_colour_that_didnt_stick_isnt_counted(self):
        proposal = actions.preview(self.controller, "set_clip_colors", {
            "color": "Teal", "clips": [{"track": "V1", "index": 1}, {"track": "V1", "index": 2}]})
        log = []
        result = actions.execute(self.controller, proposal, log=log.append)
        self.assertIn("Coloured 1 of 2", result)
        self.assertIn("titles and generators", result)
        self.assertTrue(any("didn't stay" in line for line in log))

    def test_delete_opens_the_edit_page_for_a_moment(self):
        proposal = actions.preview(self.controller, "delete_timeline_clips", {"clips": [{"track": "V1", "index": 1}]})
        self.assertTrue(any("linked" in w for w in proposal.warnings))
        actions.execute(self.controller, proposal)
        self.assertEqual(self.timeline.tracks[0], [self.b])
        self.assertEqual(self.resolve.visited, ["edit", "fairlight"])   # and back where they were

    def test_delete_refused_on_the_edit_page_is_an_error(self):
        self.resolve.page = "edit"
        self.timeline.DeleteClips = lambda items, ripple=False: False
        proposal = actions.preview(self.controller, "delete_timeline_clips", {"clips": [{"track": "V1", "index": 1}]})
        with self.assertRaises(actions.ActionError):
            actions.execute(self.controller, proposal)
        self.assertEqual(self.resolve.visited, [])


class DeleteBinTests(unittest.TestCase):
    def test_counts_recursively_and_warns_about_timelines(self):
        inner = FakeFolder("Inner", [FakeClip("c3"), FakeClip("Cut 2", "Timeline")])
        doomed = FakeFolder(
            "Doomed", [FakeClip("c1"), FakeClip("Cut 1", "Timeline")], [inner]
        )
        controller, _, _ = _setup(root=FakeFolder("Master", subs=[doomed]))
        proposal = actions.preview(
            controller, "delete_media_pool_items", {"bins": ["Doomed"]}
        )
        self.assertIn("2 clip(s), 2 timeline(s), 1 sub-bin(s)", proposal.details[0])
        self.assertTrue(any("'Cut 1'" in w and "'Cut 2'" in w for w in proposal.warnings))
        self.assertIn("destroys the edit", proposal.warnings[0])
        self.assertIn("plus 5 item(s)", proposal.summary)

    def test_recursive_total_is_capped(self):
        many = FakeFolder("Deep", [FakeClip(f"c{i}") for i in range(actions.MAX_ITEMS_PER_ACTION)])
        doomed = FakeFolder("Doomed", subs=[many])
        controller, _, _ = _setup(root=FakeFolder("Master", subs=[doomed]))
        with self.assertRaises(actions.ActionError):
            actions.preview(
                controller, "delete_media_pool_items", {"bins": ["Doomed"]}
            )


if __name__ == "__main__":
    unittest.main()
