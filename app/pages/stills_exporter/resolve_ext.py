#!/usr/bin/env python3
"""
Stills Exporter's own Resolve calls - kept separate from
core/resolve_bridge.py, which only owns the shared connect/bootstrap
plumbing every tool needs.

Free functions taking a controller, not methods on a ResolveController
subclass: Buddy's controller is shared by every page, so one tool's
working state (the grabbed-stills list) must not live there. The page
owns that list and passes it in.

This tool WRITES to Resolve: it adds
markers, switches to the Color page, moves the playhead, grabs stills, and
can delete them from the gallery afterward. Every such call is behind an
explicit button.
"""

import time

from core.marker_colors import numeric_markers
from core.resolve_bridge import ResolveConnectionError


def valid_export_prefix(prefix):
    """Keep Resolve's generated filenames inside the selected folder."""
    return not any(char in '\\/:*?"<>|' or ord(char) < 32 for char in prefix)


def timecode_to_frames(timecode, fps):
    """'HH:MM:SS:FF' or 'HH:MM:SS;FF' to an absolute frame number."""
    drop_frame = ";" in timecode
    parts = timecode.replace(";", ":").split(":")
    hours, minutes, seconds, frames = (int(part) for part in parts)
    
    fps_int = int(round(fps))
    total_frames = ((hours * 3600) + (minutes * 60) + seconds) * fps_int + frames
    
    if drop_frame:
        drop_count = int(round(fps * 0.066666))
        total_minutes = hours * 60 + minutes
        drops = drop_count * (total_minutes - (total_minutes // 10))
        total_frames -= drops
        
    return total_frames


def frames_to_timecode(frame, fps, drop_frame=False):
    """An absolute frame number back to 'HH:MM:SS:FF' (or ;FF for drop frame)."""
    fps_int = int(round(fps))
    
    if drop_frame:
        drop_count = int(round(fps * 0.066666))
        frames_per_10_min = fps_int * 600 - drop_count * 9
        frames_per_min = fps_int * 60 - drop_count
        
        D = frame // frames_per_10_min
        M = frame % frames_per_10_min
        if M > drop_count:
            frame += drop_count * 9 * D + drop_count * ((M - drop_count) // frames_per_min)
        else:
            frame += drop_count * 9 * D

    frames = int(frame) % fps_int
    total_seconds = int(frame) // fps_int
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    
    sep = ";" if drop_frame else ":"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{frames:02d}"


def get_project(controller):
    project = controller.current_project()
    if project is None:
        raise ResolveConnectionError("No project is open in Resolve.")
    return project


def get_timeline(controller, project=None):
    project = project or get_project(controller)
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise ResolveConnectionError(
            "No timeline is open/active in the current project."
        )
    return timeline


def get_fps(timeline):
    return float(timeline.GetSetting("timelineFrameRate"))


def timeline_markers(controller):
    """The open timeline's name and its markers, in frame order, as
    [{frame, timecode, color, name}] - what the page shows. Read-only."""
    timeline = get_timeline(controller)
    fps = get_fps(timeline)
    drop_frame = str(timeline.GetSetting("timelineDropFrameTimecode")) == "1"
    start = timeline.GetStartFrame()
    markers = []
    for frame, info in numeric_markers(timeline.GetMarkers()):
        frame_id = int(frame)
        markers.append({
            "frame": frame_id,
            "timecode": frames_to_timecode(int(frame_id) + start, fps, drop_frame),
            "color": info.get("color", ""),
            "name": info.get("name", ""),
        })
    return str(timeline.GetName() or ""), markers


def _playhead_moving(timeline, gap=0.15):
    """True if the playhead moves within `gap` seconds - the timeline is playing."""
    try:
        before = timeline.GetCurrentTimecode()
        time.sleep(gap)
        return timeline.GetCurrentTimecode() != before
    except Exception:  # noqa: BLE001 - only used to explain a refusal
        return False


def add_marker_at_playhead(controller, color, name="", note=""):
    """Drop a marker on the current frame. Returns its timecode."""
    timeline = get_timeline(controller)
    fps = get_fps(timeline)
    current_tc = timeline.GetCurrentTimecode()
    frame_id = timecode_to_frames(current_tc, fps) - timeline.GetStartFrame()

    ok = timeline.AddMarker(frame_id, color, name or f"{color} Marker", note, 1, "")
    if not ok:
        if _playhead_moving(timeline):
            raise ResolveConnectionError(
                "Resolve can't add a marker while the timeline is playing. Stop playback and try again.")
        raise ResolveConnectionError(
            f"Resolve rejected the marker at {current_tc} "
            "(there may already be a marker on that frame)."
        )
    return current_tc


def grab_stills_for_color(controller, color, log=lambda msg: None):
    """Visit every marker of one colour in frame order, grabbing a still.

    Switches Resolve to the Color page - GrabStill() only works there.
    Returns [(timecode, still)] for this run; the caller accumulates them.
    """
    project = get_project(controller)
    timeline = get_timeline(controller, project)
    fps = get_fps(timeline)
    drop_frame = str(timeline.GetSetting("timelineDropFrameTimecode")) == "1"
    start_frame = timeline.GetStartFrame()

    matching = [int(frame) for frame, info in numeric_markers(timeline.GetMarkers())
                if info.get("color") == color]
    if not matching:
        raise ResolveConnectionError(
            f"No '{color}' markers found on the current timeline."
        )

    log(f"Found {len(matching)} '{color}' marker(s). Switching to Color page…")
    controller.resolve.OpenPage("color")

    # Re-read the timeline: switching pages can invalidate the handle.
    timeline = get_timeline(controller, project)
    grabbed = []
    for frame_id in matching:
        timecode = frames_to_timecode(frame_id + start_frame, fps, drop_frame)
        if not timeline.SetCurrentTimecode(timecode):
            log(f"  WARNING: failed to move the playhead to {timecode}; no still grabbed")
            continue
        still = timeline.GrabStill()
        if still:
            grabbed.append((timecode, still))
            log(f"  Grabbed still at {timecode}")
        else:
            log(f"  WARNING: failed to grab still at {timecode}")
    return grabbed


def export_stills(controller, stills, folder, prefix, fmt,
                  delete_after=False, log=lambda msg: None):
    """Write the grabbed stills out as image files.

    delete_after also removes them from Resolve's gallery - the one
    destructive thing this tool can do, and why the page confirms first.
    Returns False only when the files exported but gallery deletion failed;
    export failures raise instead.
    """
    if not stills:
        raise ResolveConnectionError("No stills have been grabbed yet.")
    project = get_project(controller)
    gallery = project.GetGallery()
    if gallery is None:
        raise ResolveConnectionError("Could not access the Gallery.")
    album = gallery.GetCurrentStillAlbum()
    if album is None:
        raise ResolveConnectionError("Could not access the current still album.")

    if not album.ExportStills(stills, folder, prefix, fmt):
        raise ResolveConnectionError("Resolve reported the export failed.")
    log(f"Exported {len(stills)} still(s) to {folder} as .{fmt}")

    if not delete_after:
        return True
    try:
        deleted = bool(album.DeleteStills(stills))
    except Exception:
        deleted = False
    log(
        f"Deleted {len(stills)} still(s) from the Resolve gallery."
        if deleted
        else "WARNING: Resolve reported the gallery deletion failed."
    )
    return deleted
