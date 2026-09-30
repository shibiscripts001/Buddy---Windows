#!/usr/bin/env python3
"""
Media Relink's own Resolve calls - kept separate from
core/resolve_bridge.py, which only owns the shared connect/bootstrap
plumbing every tool needs.

Free functions taking a controller, matching the other tools (see
image_importer/resolve_ext.py). Read-only except replace_clip, which is
the one call that actually changes a clip's linked media.

Every scan returns (clip, bin_path) pairs of Media Pool items - a timeline
item is not itself a MediaPoolItem, so the timeline and selection scans
map theirs back, and look their bins up in the pool.
"""

from core.resolve_bridge import ResolveConnectionError

SCOPE_PROJECT = "project"
SCOPE_BIN = "bin"
SCOPE_TIMELINE = "timeline"
SCOPE_SELECTED = "selected"
SCOPES = (SCOPE_PROJECT, SCOPE_BIN, SCOPE_TIMELINE, SCOPE_SELECTED)

# Tracks whose items can link to a file (a subtitle track's items don't).
TIMELINE_TRACKS = ("video", "audio")


class NothingToScan(Exception):
    """The chosen scope has nothing in it to scan - no timeline open, or
    nothing selected. (title, text), worded for the user."""

    def __init__(self, title, text):
        super().__init__(text)
        self.title, self.text = title, text


def _project(controller):
    project = controller.current_project()
    if not project:
        raise ResolveConnectionError("No project is open in Resolve!")
    return project


def scan_current_bin(controller):
    """Only the clips directly in the bin currently open in Resolve."""
    project = _project(controller)
    folder = project.GetMediaPool().GetCurrentFolder()
    if not folder:
        raise ResolveConnectionError("No Media Pool bin is open in Resolve!")
    return [(clip, folder.GetName()) for clip in (folder.GetClipList() or [])
            if _is_real_media_clip(clip)]


def _is_real_media_clip(clip):
    """A Media Pool bin's own GetClipList() includes the project's own
    TIMELINES as pseudo-clips, indistinguishable from real footage by
    has_video/has_audio alone. A timeline has no "File Path" of its own to
    go offline, so leaving it unfiltered here would show up as a
    permanently-offline clip with nothing sensible to relink it to.
    GetClipProperty("Type") is the one property found to reliably tell
    them apart."""
    try:
        return clip.GetClipProperty("Type") != "Timeline"
    except Exception:
        return True  # can't tell - err on the side of including it


def scan_all_clips(controller):
    """Walks every bin in the Media Pool recursively and returns a flat
    list of (clip, bin_path) tuples for every real media clip found
    (timelines filtered out - see _is_real_media_clip). bin_path is a
    "/"-joined display path like "Footage/Drone/Day 1"."""
    project = _project(controller)

    media_pool = project.GetMediaPool()
    root_folder = media_pool.GetRootFolder()
    if not root_folder:
        return []

    results = []
    _walk_folder(root_folder, root_folder.GetName(), results)
    return results


def _walk_folder(folder, bin_path, results):
    clips = folder.GetClipList() or []
    for clip in clips:
        if _is_real_media_clip(clip):
            results.append((clip, bin_path))

    subfolders = folder.GetSubFolderList() or []
    for sub in subfolders:
        sub_path = f"{bin_path}/{sub.GetName()}"
        _walk_folder(sub, sub_path, results)


def scan_timeline(controller):
    """Every Media Pool clip used on the open timeline's video and audio
    tracks, once each however often it's cut in."""
    project = _project(controller)
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise NothingToScan("No timeline open", "Open a timeline in Resolve, then scan again.")
    items = []
    for kind in TIMELINE_TRACKS:
        for index in range(1, int(timeline.GetTrackCount(kind) or 0) + 1):
            items.extend(timeline.GetItemListInTrack(kind, index) or [])
    return _with_bins(project, _pool_items(items))


def scan_selected(controller):
    """The clips selected in the Media Pool - or, when none are, the ones
    selected on the open timeline (Resolve 21.0.4 or later can say)."""
    project = _project(controller)
    clips = [c for c in (project.GetMediaPool().GetSelectedClips() or []) if c is not None]
    if not clips:
        timeline = project.GetCurrentTimeline()
        # dir() is the truth on Resolve's objects - hasattr always says yes.
        if timeline is not None and "GetSelectedClips" in dir(timeline):
            clips = _pool_items(timeline.GetSelectedClips() or [])
    entries = _with_bins(project, _unique(c for c in clips if _is_real_media_clip(c)))
    if not entries:
        raise NothingToScan("Select clips first",
                            "Select the clips in Resolve's Media Pool (or on the timeline), then scan again.")
    return entries


def _pool_items(timeline_items):
    """The Media Pool clips behind timeline items. Titles, generators and
    the like have none; a nested timeline's is a timeline - both left out."""
    clips = []
    for item in timeline_items:
        try:
            clip = item.GetMediaPoolItem()
        except Exception:
            clip = None
        if clip is not None and _is_real_media_clip(clip):
            clips.append(clip)
    return _unique(clips)


def clip_id(clip):
    """The clip's Media Pool id, or None."""
    try:
        return clip.GetUniqueId() or None
    except Exception:
        return None


def _unique(clips):
    seen, result = set(), []
    for clip in clips:
        key = clip_id(clip) or id(clip)
        if key not in seen:
            seen.add(key)
            result.append(clip)
    return result


def _with_bins(project, clips):
    """(clip, bin_path) for clips found some other way than a bin walk -
    the bin looked up by id, "" if it isn't in any."""
    if not clips:
        return []
    bins = {}
    root = project.GetMediaPool().GetRootFolder()
    if root:
        _walk_ids(root, root.GetName(), bins)
    return [(clip, bins.get(clip_id(clip), "")) for clip in clips]


def _walk_ids(folder, bin_path, bins):
    for clip in folder.GetClipList() or []:
        uid = clip_id(clip)
        if uid:
            bins.setdefault(uid, bin_path)
    for sub in folder.GetSubFolderList() or []:
        _walk_ids(sub, f"{bin_path}/{sub.GetName()}", bins)


def current_paths(controller, ids):
    """{id: File Path} for the clips with these ids, read afresh from the
    Media Pool - how a relink is checked, not taken on ReplaceClip's word."""
    wanted, paths = set(ids), {}
    root = _project(controller).GetMediaPool().GetRootFolder()

    def walk(folder):
        for clip in folder.GetClipList() or []:
            uid = clip_id(clip)
            if uid in wanted and uid not in paths:
                paths[uid] = get_clip_file_path(clip)
        for sub in folder.GetSubFolderList() or []:
            walk(sub)

    if root and wanted:
        walk(root)
    return paths


SCANS = {SCOPE_PROJECT: scan_all_clips, SCOPE_BIN: scan_current_bin,
         SCOPE_TIMELINE: scan_timeline, SCOPE_SELECTED: scan_selected}


def get_clip_file_path(clip):
    """The path Resolve has recorded for this clip's media. Comes back as
    "" (not None) for a clip type that has no single backing file (e.g. a
    still-image sequence's numbering placeholder) - callers should treat a
    falsy result as "nothing to check" rather than "offline"."""
    try:
        return clip.GetClipProperty("File Path") or ""
    except Exception:
        return ""


def replace_clip(clip, new_path):
    """Relinks a single Media Pool clip to a new file on disk.
    ReplaceClip() returns a bool - False on failure, with no further
    detail (wrong media type, permissions, Resolve rejecting the format
    change, etc. all look the same from here). The page checks each True
    afterwards with current_paths: Resolve has answered True and kept the
    old file."""
    try:
        return bool(clip.ReplaceClip(new_path))
    except Exception:
        return False
