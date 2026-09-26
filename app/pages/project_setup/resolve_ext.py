#!/usr/bin/env python3
"""
Project Setup's Resolve surface, adapted to the Buddy shell.

The connection bootstrap lives in core/resolve_bridge.py - the shell owns
one probe-guarded connection. What lives here is the tool-specific half
(get_project, get_bin_clips, append_clips_to_timeline,
export_timeline_otio, import_timeline_file, ...): a controller API
constructed AROUND the shell's already-connected resolve object instead of
connecting itself.
"""

from core.resolve_bridge import ResolveConnectionError


def _is_real_media_clip(clip):
    """A Media Pool bin's own GetClipList() includes the project's own
    timelines as pseudo-clip entries, indistinguishable from real footage
    by has_video/has_audio alone - confirmed against a running Resolve
    via fuscript.exe (see docs/RESOLVE_API_GUIDE.md). A timeline has no real footage to
    place on another timeline, so it must be filtered out of anything
    handed to AppendToTimeline. GetClipProperty("Type") != "Timeline" is
    the one property that reliably tells them apart."""
    try:
        return clip.GetClipProperty("Type") != "Timeline"
    except Exception:
        return True  # can't tell - err on the side of including it


class ProjectSetupController:
    """Wraps the shell's ResolveController with Project Setup's own API.
    Stateless itself - all state lives in Resolve's own objects - so a
    fresh wrapper per call would also be safe; the page caches one and
    rebuilds it when the shell reconnects."""

    def __init__(self, shell_controller):
        self._shell = shell_controller
        self.resolve = shell_controller.resolve

    def get_project(self):
        project = self.resolve.GetProjectManager().GetCurrentProject()
        if not project:
            raise ResolveConnectionError("No project is open in Resolve!")
        return project

    def get_media_storage(self):
        """Used by the Import Folder tab to hand real filesystem paths to
        Resolve for ingest (MediaStorage.AddItemListToMediaPool), as opposed
        to MediaPool's own folder-creation calls."""
        return self.resolve.GetMediaStorage()

    def get_current_bin(self):
        """The bin currently selected/open in Resolve's own Media Pool
        panel - used by the Timeline tab so it always targets whatever the
        user is looking at there, instead of a separately-maintained
        picker. Falls back to the root folder on the rare chance
        GetCurrentFolder() comes back None (every API hop
        can - see docs/RESOLVE_API_GUIDE.md)."""
        project = self.get_project()
        media_pool = project.GetMediaPool()
        return media_pool.GetCurrentFolder() or media_pool.GetRootFolder()

    def get_bin_clips(self, folder, recursive=False):
        """Real media clips directly inside `folder` (timelines filtered
        out - see _is_real_media_clip), and every subfolder's too if
        recursive=True."""
        clips = [c for c in (folder.GetClipList() or []) if _is_real_media_clip(c)]
        if recursive:
            for sub in folder.GetSubFolderList() or []:
                clips.extend(self.get_bin_clips(sub, recursive=True))
        return clips

    def append_clips_to_timeline(self, clips, new_timeline_name=None):
        """Appends clips to whatever timeline is currently open, or creates
        a new one (named new_timeline_name) first if none is open. Returns
        (created_new_timeline, placed_count)."""
        project = self.get_project()
        media_pool = project.GetMediaPool()
        timeline = project.GetCurrentTimeline()

        if timeline is None:
            new_timeline = media_pool.CreateTimelineFromClips(new_timeline_name or "Timeline 1", clips)
            if new_timeline is None:
                raise ResolveConnectionError("Could not create a new timeline from these clips.")
            return True, len(clips)

        items = media_pool.AppendToTimeline(clips)
        # AppendToTimeline can return a list CONTAINING None (e.g. [None])
        # on total failure, not an empty list - confirmed against a
        # running Resolve via fuscript.exe (see docs/RESOLVE_API_GUIDE.md). `if items:`
        # alone would misreport that failure as success.
        placed = [i for i in (items or []) if i]
        return False, len(placed)

    # ---------- Align tab ----------
    def get_current_timeline(self):
        project = self.get_project()
        timeline = project.GetCurrentTimeline()
        if timeline is None:
            raise ResolveConnectionError(
                "No timeline is open – open (or create) one with your footage on it first."
            )
        return timeline

    def get_timeline_fps(self, timeline=None):
        """The timeline's actual frame rate."""
        if timeline is None:
            timeline = self.get_current_timeline()
        raw = timeline.GetSetting("timelineFrameRate")
        if isinstance(raw, (int, float)):
            return float(raw)
        raw = (raw or "24").strip().replace("DF", "").replace("df", "").strip()
        try:
            return float(raw)
        except ValueError:
            return 24.0

    def get_project_fps(self):
        """The project's default timeline frame rate."""
        project = self.get_project()
        raw = project.GetSetting("timelineFrameRate")
        if isinstance(raw, (int, float)):
            return float(raw)
        raw = (raw or "24").strip().replace("DF", "").replace("df", "").strip()
        try:
            return float(raw)
        except ValueError:
            return 24.0

    # ---------- OTIO interchange (see otio_engine.py) ----------
    def export_timeline_otio(self, timeline, file_path):
        """Writes `timeline` to file_path as OTIO. Returns True on success.

        OTIO rather than DRT or FCPXML: DRT is Resolve's own format and holds
        the most, but it is a ZIP archive of XML rather than something to
        edit directly; FCPXML export has been seen to report success and
        write a zero-byte file. OTIO is plain JSON, exports in hundredths of a
        second, and round-trips clip names, positions, durations and source
        in/out points exactly - verified item by item against a real 98-clip
        timeline."""
        return bool(timeline.Export(file_path, self.resolve.EXPORT_OTIO,
                                    self.resolve.EXPORT_NONE))

    def all_media_pool_folders(self, folder=None):
        """Every folder in the Media Pool, depth first."""
        folder = folder or self.get_project().GetMediaPool().GetRootFolder()
        found = [folder]
        for sub in folder.GetSubFolderList() or []:
            found.extend(self.all_media_pool_folders(sub))
        return found

    def import_timeline_file(self, file_path, timeline_name):
        """Imports an interchange file as a NEW timeline, reusing the Media
        Pool clips already in the project. Returns the Timeline, or None.

        importSourceClips=False stops Resolve adding a second copy of every
        source file to the Media Pool, and sourceClipsFolders tells it where
        to look for the ones already there. That list must be EVERY folder,
        not just the root: the root folder of a real project often holds no
        clips at all (they live in bins), and passing only the root imported
        98 items with no Media Pool clip attached to any of them - clips that
        play but that the scripting API cannot then reach through
        GetMediaPoolItem(). With the full folder list, all 98 came back
        bound, and no duplicates were added to the pool."""
        media_pool = self.get_project().GetMediaPool()
        return media_pool.ImportTimelineFromFile(file_path, {
            "timelineName": timeline_name,
            "importSourceClips": False,
            "sourceClipsFolders": self.all_media_pool_folders(),
        })
