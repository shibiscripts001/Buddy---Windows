#!/usr/bin/env python3
"""
Media Relink's own Resolve calls - kept separate from
core/resolve_bridge.py, which only owns the shared connect/bootstrap
plumbing every tool needs.

Free functions taking a controller, matching the other tools (see
image_importer/resolve_ext.py). Read-only except replace_clip, which is
the one call that actually changes a clip's linked media.
"""

from core.resolve_bridge import ResolveConnectionError

SCOPE_PROJECT = "project"
SCOPE_BIN = "bin"
SCOPES = (SCOPE_PROJECT, SCOPE_BIN)


def scan_current_bin(controller):
    """Only the clips directly in the bin currently open in Resolve."""
    project = controller.current_project()
    if not project:
        raise ResolveConnectionError("No project is open in Resolve!")
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
    project = controller.current_project()
    if not project:
        raise ResolveConnectionError("No project is open in Resolve!")

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
    change, etc. all look the same from here)."""
    try:
        return bool(clip.ReplaceClip(new_path))
    except Exception:
        return False
