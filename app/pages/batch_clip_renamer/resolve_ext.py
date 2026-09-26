#!/usr/bin/env python3
"""
Batch Clip Renamer's own Resolve calls - kept separate from
core/resolve_bridge.py, which only owns the shared connect/bootstrap
plumbing every tool needs, not any tool-specific logic.
"""


def _is_real_media_clip(clip):
    """A Media Pool bin's own GetClipList()/GetSelectedClips() includes the
    project's own TIMELINES as pseudo-clips, indistinguishable from real
    footage by has_video/has_audio alone (a timeline reports "Audio Ch" > 0
    despite having no real audio) - confirmed against a running Resolve
    via fuscript.exe. Left unfiltered, a timeline
    sitting in the same bin as real clips both breaks the sequential
    renamer's numbering (it counts a slot for something that isn't really
    "clip N" of the batch) and gets its own name silently overwritten by
    Find & Replace. GetClipProperty("Type") is the one property found
    to actually tell them apart."""
    try:
        return clip.GetClipProperty("Type") != "Timeline"
    except Exception:
        return True  # can't tell - err on the side of including it, as before


def get_current_clips(controller):
    """Clips in the Media Pool's currently open bin/folder (timelines
    filtered out), or None if no project is open."""
    project = controller.current_project()
    if not project:
        return None

    media_pool = project.GetMediaPool()
    current_folder = media_pool.GetCurrentFolder()
    clips = current_folder.GetClipList()
    if not clips:
        return clips
    return [c for c in clips if _is_real_media_clip(c)]


def get_selected_clips(controller):
    """Clips currently selected in the Media Pool (timelines filtered
    out), or None if no project is open. Some older Resolve versions
    don't support GetSelectedClips() at all - callers should be ready to
    catch an exception from this."""
    project = controller.current_project()
    if not project:
        return None
    clips = project.GetMediaPool().GetSelectedClips()
    if not clips:
        return clips
    return [c for c in clips if _is_real_media_clip(c)]


SCOPE_BIN = "bin"
SCOPE_SELECTED = "selected"


class TargetError(Exception):
    """Why there is nothing to rename, worded for the user."""


def clip_name(clip):
    """What the Media Pool shows: the Clip Name property, falling back to
    GetName() for a clip that has never had one set."""
    return clip.GetClipProperty("Clip Name") or clip.GetName() or ""


def clip_key(clip):
    """Stable identity for a clip across API calls - the wrapper objects
    Resolve hands back are new each call, so they can't be compared."""
    try:
        return clip.GetUniqueId()
    except Exception:
        return clip_name(clip)


def read_targets(controller, scope):
    """(clips, where) for the chosen scope - `where` names the bin, or says
    "Selected clips". Raises TargetError when there is nothing to work on."""
    project = controller.current_project()
    if not project:
        raise TargetError("No project is open in Resolve.")
    if scope == SCOPE_SELECTED:
        try:
            clips = get_selected_clips(controller)
        except Exception as exc:
            raise TargetError(
                f"Could not read the Media Pool selection ({exc}). Your Resolve version may "
                "not support this – use the current bin instead."
            ) from exc
        if not clips:
            raise TargetError("No clips are selected in the Media Pool.")
        return clips, "Selected clips"
    folder = project.GetMediaPool().GetCurrentFolder()
    where = (folder.GetName() if folder else "") or "Current bin"
    clips = get_current_clips(controller)
    if not clips:
        raise TargetError(f"The bin \"{where}\" has no clips in it.")
    return clips, where


def target_signature(controller, scope):
    """A cheap fingerprint of what the scope points at, for the page to
    poll while it's on screen: the open bin and how many items it holds,
    or which clips are selected. No per-clip property reads for a bin -
    polling must stay far cheaper than read_targets(). None when there's
    no project."""
    project = controller.current_project()
    if not project:
        return None
    pool = project.GetMediaPool()
    if scope == SCOPE_SELECTED:
        try:
            return tuple(clip_key(c) for c in (pool.GetSelectedClips() or []))
        except Exception:
            return None
    folder = pool.GetCurrentFolder()
    if not folder:
        return None
    return (folder.GetUniqueId(), len(folder.GetClipList() or []))
