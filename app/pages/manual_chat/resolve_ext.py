#!/usr/bin/env python3
"""
Manual Chat's own Resolve calls - kept separate from core/resolve_bridge.py,
which only owns the shared connect/bootstrap plumbing.

STRICTLY READ-ONLY. Everything here answers "what is the user's project
actually set to right now", so the agent can say "your timeline is 23.976
but this clip is 29.97" instead of reciting the manual's generic advice.
Nothing in this module writes to Resolve; the chat page never mutates a
project, and any future write belongs behind an explicit confirmation step.

Every getter is defensive by design. The Resolve API returns None (not an
error) for a closed project, an empty timeline, or a setting that doesn't
exist in the installed version, and GetSetting() silently returns "" for
keys that were renamed between releases. A snapshot with holes in it is
still useful to the agent; an exception mid-collection is not.
"""

from __future__ import annotations

import os

# Project settings worth knowing about, mapped to the label the manual and
# the Project Settings UI actually use - so the agent's answer matches what
# the user sees on screen rather than the raw API key.
PROJECT_SETTING_LABELS = {
    "timelineFrameRate": "Timeline frame rate",
    "timelineResolutionWidth": "Timeline width",
    "timelineResolutionHeight": "Timeline height",
    "timelinePlaybackFrameRate": "Playback frame rate",
    "colorScienceMode": "Color science",
    "colorSpaceInput": "Input color space",
    "colorSpaceTimeline": "Timeline color space",
    "colorSpaceOutput": "Output color space",
    "timelineWorkingLuminance": "Working luminance",
    "superScale": "SuperScale",
    "useCATransform": "Color adaptation transform",
    "isAutoColorManage": "Automatic color management",
}

# Clip properties that explain "why does this look/behave wrong" questions.
CLIP_PROPERTIES = [
    "File Name",
    "Format",
    "Video Codec",
    "Resolution",
    "FPS",
    "Data Level",
    "Input Color Space",
    "Audio Ch",
]

# Source-clip properties that explain a format mismatch. Queried per key
# rather than via a no-argument GetClipProperty() snapshot: Blackmagic's
# own scripting README says specific keys are faster (Support/Developer/
# Scripting/README.txt, "Getting values").
SOURCE_PROPERTIES = [
    "File Name",
    "FPS",
    "Resolution",
    "Video Codec",
    "Input Color Space",
]

MAX_CLIPS = 10
MAX_TIMELINE_ITEMS = 40
MAX_MARKERS = 40

# Frame rates are strings like "23.976" on one side and "23.976023" on the
# other depending on where they are read from, so exact comparison would
# report mismatches that are not real.
FPS_TOLERANCE = 0.01


def _safe(fn, default=None):
    """Resolve's API raises on version mismatches as readily as it returns
    None. Callers want a partial snapshot either way."""
    try:
        value = fn()
    except Exception:
        return default
    return default if value is None else value


def _is_real_media_clip(clip) -> bool:
    """Media Pool bins list TIMELINES as pseudo-clips - see the same guard in
    batch_clip_renamer/resolve_ext.py, which documents it."""
    try:
        return clip.GetClipProperty("Type") != "Timeline"
    except Exception:
        return True


def _as_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_resolution(value):
    """'1920x1080' -> (1920, 1080). None for anything else."""
    try:
        width, height = str(value).lower().split("x", 1)
        return int(width.strip()), int(height.strip())
    except (TypeError, ValueError):
        return None


def resolve_info(controller) -> dict:
    """Which Resolve this is - crucially, Studio or free.

    The Reference Manual documents Studio-only features without always
    flagging them, so without this the agent will happily walk a free user
    through Magic Mask or the Neural Engine. GetProductName() returns
    "DaVinci Resolve" or "DaVinci Resolve Studio" (Scripting README, line
    91); the suffix is the only reliable signal, as there is no boolean for
    it in the API.
    """
    resolve = getattr(controller, "resolve", None)
    if resolve is None:
        return {}

    product = _safe(resolve.GetProductName, "") or ""
    info = {
        "product": product,
        "version": _safe(resolve.GetVersionString, "") or "",
    }
    if product:
        # Substring test, not equality: Blackmagic has shipped the name with
        # trailing whitespace and with locale decoration before now.
        info["is_studio"] = "studio" in product.lower()
    return info


def project_snapshot(controller) -> dict:
    """A JSON-serializable picture of the open project, for the agent.

    Returns {"open": False} rather than raising when nothing is open, so the
    agent can say so plainly instead of surfacing a traceback.
    """
    # Read before the early return: whether this is Studio matters just as
    # much when no project is open ("can I use Magic Mask?" needs no project).
    info = resolve_info(controller)

    project = _safe(controller.current_project)
    if not project:
        return {
            "open": False,
            "resolve": info,
            "reason": "No project is currently open in Resolve.",
        }

    snap: dict = {
        "open": True,
        "resolve": info,
        "project_name": _safe(project.GetName, ""),
        "current_page": _safe(lambda: controller.resolve.GetCurrentPage(), ""),
        "settings": {},
        "timeline": None,
    }

    for key, label in PROJECT_SETTING_LABELS.items():
        value = _safe(lambda k=key: project.GetSetting(k), "")
        if value not in ("", None):
            snap["settings"][label] = value

    timeline = _safe(project.GetCurrentTimeline)
    if timeline:
        snap["timeline"] = {
            "name": _safe(timeline.GetName, ""),
            "video_tracks": _safe(lambda: timeline.GetTrackCount("video"), 0),
            "audio_tracks": _safe(lambda: timeline.GetTrackCount("audio"), 0),
            "start_frame": _safe(timeline.GetStartFrame, 0),
            "end_frame": _safe(timeline.GetEndFrame, 0),
        }
        # A timeline's own settings override the project's when "Use Project
        # Settings" is off - which is exactly the mismatch users ask about.
        for key in ("timelineFrameRate", "timelineResolutionWidth",
                    "timelineResolutionHeight"):
            value = _safe(lambda k=key: timeline.GetSetting(k), "")
            if value not in ("", None):
                snap["timeline"][PROJECT_SETTING_LABELS[key]] = value

    return snap


def selected_clip_properties(controller) -> list[dict]:
    """Properties of the Media Pool's selected clips (timelines excluded).

    Falls back to the current bin's clips when nothing is selected, since
    "what format is this footage" is usually asked with a bin open rather
    than a specific clip highlighted. Capped at MAX_CLIPS so a 2,000-clip
    bin can't blow up the agent's context.
    """
    project = _safe(controller.current_project)
    if not project:
        return []
    pool = _safe(project.GetMediaPool)
    if not pool:
        return []

    clips = _safe(pool.GetSelectedClips, []) or []
    if not clips:
        folder = _safe(pool.GetCurrentFolder)
        clips = (_safe(folder.GetClipList, []) if folder else []) or []

    out = []
    for clip in [c for c in clips if _is_real_media_clip(c)][:MAX_CLIPS]:
        props = {}
        for name in CLIP_PROPERTIES:
            value = _safe(lambda n=name: clip.GetClipProperty(n), "")
            if value not in ("", None):
                props[name] = value
        if props:
            out.append(props)
    return out


def _timeline_format(project, timeline) -> dict:
    """The timeline's own frame rate and resolution, project as fallback.

    A timeline with "Use Project Settings" switched off overrides the
    project - and that override is the single most common cause of the
    mismatches this module exists to find, so the timeline is asked first.
    """
    out = {}
    for key, label in (
        ("timelineFrameRate", "fps"),
        ("timelineResolutionWidth", "width"),
        ("timelineResolutionHeight", "height"),
    ):
        value = _safe(lambda k=key: timeline.GetSetting(k), "")
        if value in ("", None):
            value = _safe(lambda k=key: project.GetSetting(k), "")
        if value not in ("", None):
            out[label] = value
    return out


def _source_properties(item, cache: dict) -> dict:
    """Properties of the media behind one timeline item, memoized.

    Every GetClipProperty() is an IPC round trip into Resolve, and a
    timeline normally holds far fewer distinct source clips than items - a
    40-item cut of 5 camera files is 5 lookups here instead of 200. Keyed
    on GetMediaId(), falling back to the object's own identity when the
    item has no media pool item at all (titles, generators, compounds).
    """
    pool_item = _safe(item.GetMediaPoolItem)
    if pool_item is None:
        return {}

    key = _safe(pool_item.GetMediaId, "") or f"id:{id(pool_item)}"
    if key in cache:
        return cache[key]

    props = {}
    for name in SOURCE_PROPERTIES:
        value = _safe(lambda n=name: pool_item.GetClipProperty(n), "")
        if value not in ("", None):
            props[name] = value
    cache[key] = props
    return props


def _find_mismatches(entry: dict, fmt: dict) -> list[str]:
    """Ways one timeline clip disagrees with its timeline. Empty is good.

    Computed here, not in the prompt: asking a model to compare forty pairs
    of frame rates is asking it to do arithmetic it is known to fumble,
    while the same check is four lines of Python that is either right or
    raises.

    Deliberately NOT flagged: source resolution HIGHER than the timeline.
    4K footage in a 1080 timeline is normal practice, and reporting every
    such clip would bury the real problems in noise. Only upscaling - where
    the source has fewer pixels than the timeline - is called out.
    """
    issues = []
    source = entry.get("source", {})

    timeline_fps = _as_float(fmt.get("fps"))
    clip_fps = _as_float(source.get("FPS"))
    if timeline_fps and clip_fps and abs(timeline_fps - clip_fps) > FPS_TOLERANCE:
        issues.append(
            f"frame rate {source['FPS']} vs timeline {fmt['fps']}"
        )

    size = _parse_resolution(source.get("Resolution"))
    width = _as_float(fmt.get("width"))
    height = _as_float(fmt.get("height"))
    if size and width and height and (size[0] < width or size[1] < height):
        issues.append(
            f"resolution {source['Resolution']} is smaller than the "
            f"timeline's {int(width)}x{int(height)} (it is being upscaled)"
        )

    return issues


def timeline_contents(controller, max_items: int = MAX_TIMELINE_ITEMS) -> dict:
    """What is actually ON the current timeline, and what does not fit it.

    project_snapshot() reports track COUNTS and the Media Pool's selection.
    Neither answers "why does THIS clip look wrong", because the clip the
    user is staring at is on the timeline and very often is not the one
    selected in the Media Pool. This walks the video tracks instead.

    Video tracks only. Audio is where format mismatches are rare and item
    counts are high, so including it would cost context without buying
    answers; ask for it explicitly if that ever changes.
    """
    project = _safe(controller.current_project)
    if not project:
        return {"open": False, "reason": "No project is currently open in Resolve."}

    timeline = _safe(project.GetCurrentTimeline)
    if not timeline:
        return {
            "open": True,
            "timeline": None,
            "reason": "No timeline is open in the current project.",
        }

    fmt = _timeline_format(project, timeline)
    out = {
        "open": True,
        "timeline_name": _safe(timeline.GetName, ""),
        "timeline_format": fmt,
        "items": [],
        "mismatches": [],
    }

    cache: dict = {}
    truncated = False
    track_count = _safe(lambda: timeline.GetTrackCount("video"), 0) or 0

    for track in range(1, int(track_count) + 1):
        items = _safe(lambda t=track: timeline.GetItemListInTrack("video", t), []) or []
        for index, item in enumerate(items, start=1):
            if len(out["items"]) >= max_items:
                truncated = True
                break
            entry = {
                "track": f"V{track}",
                # 1-based position within the track. set_clip_colors and
                # delete_timeline_clips (actions.py) address clips by
                # {track, index}, so the model needs the exact number rather
                # than counting down the list itself.
                "index": index,
                "name": _safe(item.GetName, ""),
                "start": _safe(item.GetStart, 0),
                "duration": _safe(item.GetDuration, 0),
            }
            source = _source_properties(item, cache)
            if source:
                entry["source"] = source
            issues = _find_mismatches(entry, fmt)
            if issues:
                entry["mismatch"] = issues
                out["mismatches"].append(
                    {"clip": entry["name"], "track": entry["track"],
                     "index": index, "issues": issues}
                )
            out["items"].append(entry)
        if truncated:
            break

    if truncated:
        out["truncated"] = (
            f"Only the first {max_items} timeline clips are listed; the "
            "timeline has more."
        )
    return out


def timeline_markers(controller, max_markers: int = MAX_MARKERS) -> dict:
    """Markers on the current timeline, sorted by frame.

    GetMarkers() hands back a dict keyed by frame, whose iteration order is
    not the timeline order anyone means when they ask "what are my markers"
    - hence the sort.
    """
    project = _safe(controller.current_project)
    if not project:
        return {"open": False, "reason": "No project is currently open in Resolve."}

    timeline = _safe(project.GetCurrentTimeline)
    if not timeline:
        return {"open": True, "markers": [], "reason": "No timeline is open."}

    raw = _safe(timeline.GetMarkers, {}) or {}
    start_frame = _safe(timeline.GetStartFrame, 0) or 0

    markers = []
    for frame in sorted(raw, key=lambda f: _as_float(f) or 0):
        info = raw[frame] or {}
        markers.append({
            # GetMarkers() returns frames RELATIVE to the timeline start,
            # while GetStartFrame() is usually 3600 (01:00:00:00). Both are
            # reported so the agent can talk about either without guessing.
            "frame_offset": frame,
            "timeline_frame": (_as_float(frame) or 0) + start_frame,
            "name": info.get("name", ""),
            "color": info.get("color", ""),
            "note": info.get("note", ""),
            "duration": info.get("duration", 1),
        })

    out = {"open": True, "total": len(markers), "markers": markers[:max_markers]}
    if len(markers) > max_markers:
        out["truncated"] = (
            f"Showing the first {max_markers} of {len(markers)} markers."
        )
    return out


def focused_clip(controller) -> dict:
    """Read the timeline selection, or the video clip under the playhead.

    Older Resolve versions may not expose GetSelectedClips. In that case the
    playhead lookup still works. Neither path changes selection or timecode.
    """
    project = _safe(controller.current_project)
    timeline = _safe(project.GetCurrentTimeline) if project else None
    if not timeline:
        return {"found": False, "reason": "Open a timeline in Resolve first."}

    fmt = _timeline_format(project, timeline)
    selected = _safe(lambda: timeline.GetSelectedClips(), []) or []
    chosen = list(selected)[:3]
    mode = "selected timeline clip"
    timecode = _safe(timeline.GetCurrentTimecode, "") or ""
    if not chosen:
        current = _safe(lambda: timeline.GetCurrentVideoItem())
        if current:
            chosen = [current]
            mode = "clip at playhead"
    if not chosen and timecode:
        try:
            from pages.stills_exporter.resolve_ext import timecode_to_frames
            frame = timecode_to_frames(timecode, float(fmt.get("fps") or 0))
        except (ValueError, TypeError, AttributeError, ZeroDivisionError):
            frame = None
        if frame is not None:
            # Highest video track wins when clips overlap at the playhead.
            for track in range(int(_safe(lambda: timeline.GetTrackCount("video"), 0) or 0), 0, -1):
                items = _safe(lambda t=track: timeline.GetItemListInTrack("video", t), []) or []
                hits = [item for item in items if
                        (_safe(item.GetStart, -1) or -1) <= frame < (_safe(item.GetEnd, -1) or -1)]
                if hits:
                    chosen = hits[:1]
                    mode = "clip at playhead"
                    break
    if not chosen:
        return {"found": False, "reason": "Select a timeline clip or place the playhead over a video clip.",
                "timecode": timecode}

    tracks = {}
    for track in range(1, int(_safe(lambda: timeline.GetTrackCount("video"), 0) or 0) + 1):
        for index, item in enumerate(_safe(lambda t=track: timeline.GetItemListInTrack("video", t), []) or [], 1):
            uid = _safe(item.GetUniqueId, "") or f"object:{id(item)}"
            tracks[uid] = (f"V{track}", index)
    cache = {}
    clips = []
    for item in chosen:
        uid = _safe(item.GetUniqueId, "") or f"object:{id(item)}"
        track, index = tracks.get(uid, ("unknown", None))
        entry = {"name": _safe(item.GetName, ""), "track": track, "index": index,
                 "start": _safe(item.GetStart, 0), "duration": _safe(item.GetDuration, 0)}
        source = _source_properties(item, cache)
        if source:
            entry["source"] = source
        entry["mismatch"] = _find_mismatches(entry, fmt)
        clips.append(entry)
    return {"found": True, "mode": mode, "timeline": _safe(timeline.GetName, ""),
            "timecode": timecode, "timeline_format": fmt, "clips": clips}


def project_checkup(controller, max_items=500) -> dict:
    """Deterministic read-only checkup; limits and unknowns stay visible."""
    snap = project_snapshot(controller)
    if not snap.get("open"):
        return {"open": False, "resolve": snap.get("resolve", {}),
                "reason": snap.get("reason", "No project is open.")}
    contents = timeline_contents(controller, max_items=max_items)
    issues = []
    recommendations = set()
    for mismatch in contents.get("mismatches", []):
        issues.append({"kind": "format", "clip": mismatch["clip"],
                       "location": f"{mismatch['track']} #{mismatch['index']}",
                       "details": mismatch["issues"]})
        if any("frame rate" in detail for detail in mismatch["issues"]):
            recommendations.add("Review source frame rate and retiming before changing the timeline frame rate.")
        if any("upscaled" in detail for detail in mismatch["issues"]):
            recommendations.add("For soft clips, use a higher-resolution source or review the timeline resolution.")

    # Resolve's source File Path is a local path. Check only paths the OS can
    # interpret as absolute files; blank paths, generators, and URIs are not
    # enough evidence to call a clip offline. Deduplicate repeated sources.
    project = _safe(controller.current_project)
    timeline = _safe(project.GetCurrentTimeline) if project else None
    checked_media = set()
    checked_items = 0
    if timeline:
        for track in range(1, int(_safe(lambda: timeline.GetTrackCount("video"), 0) or 0) + 1):
            for index, item in enumerate(_safe(lambda t=track: timeline.GetItemListInTrack("video", t), []) or [], 1):
                if checked_items >= max_items:
                    break
                checked_items += 1
                media = _safe(item.GetMediaPoolItem)
                if not media:
                    continue
                media_id = _safe(media.GetMediaId, "") or id(media)
                if media_id in checked_media:
                    continue
                checked_media.add(media_id)
                path = _safe(lambda: media.GetClipProperty("File Path"), "") or ""
                if isinstance(path, str) and os.path.isabs(path) and not os.path.exists(path):
                    issues.append({"kind": "missing_path", "clip": _safe(item.GetName, ""),
                                   "location": f"V{track} #{index}",
                                   "details": ["source file path is not found on this computer"]})
                    recommendations.add("Open Media Relink to locate source files whose paths are missing.")
            if checked_items >= max_items:
                break
    if not snap.get("timeline"):
        recommendations.add("Open a timeline to check its clips and format.")
    return {"open": True, "project": snap.get("project_name", ""),
            "resolve": snap.get("resolve", {}), "timeline": snap.get("timeline"),
            "settings": snap.get("settings", {}),
            "timeline_format": contents.get("timeline_format", {}),
            "scanned_clips": len(contents.get("items", [])),
            "truncated": contents.get("truncated", ""), "issues": issues,
            "recommendations": sorted(recommendations),
            "scope": "Current timeline video clips; frame rate, upscaling, and local source path checks."}
