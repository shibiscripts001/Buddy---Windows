#!/usr/bin/env python3
"""
YouTube Chapters' own Resolve calls - kept separate from
core/resolve_bridge.py, which only owns the shared connect/bootstrap
plumbing every tool needs.

get_current_timeline()'s error messages are the point of it: "no project
open" and "no timeline open" are different problems with different fixes,
and the shell's connection check can tell you neither.

READ-ONLY. This tool reads markers and writes a .txt; it never modifies
the Resolve project.
"""

from core.resolve_bridge import ResolveConnectionError


def get_current_timeline(controller):
    """The open timeline, or ResolveConnectionError explaining which part
    is missing."""
    project = controller.current_project()
    if not project:
        raise ResolveConnectionError("No project is currently open in Resolve.")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise ResolveConnectionError(
            "No active timeline open in the current project."
        )
    return timeline


def read_timeline_markers(timeline):
    """(markers, framerate, duration_frames, timeline_name) for one timeline.

    Every value the chapter builder needs, read in one place so page.py
    holds no Resolve calls of its own. The framerate fallback:
    Resolve does not always report timelineFrameRate,
    and 24 is a better guess than crashing.
    """
    markers = timeline.GetMarkers() or {}
    try:
        framerate = float(timeline.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        framerate = 24.0
    try:
        start_frame = int(timeline.GetStartFrame())
        end_frame = int(timeline.GetEndFrame())
        duration_frames = max(0, end_frame - start_frame)
    except (AttributeError, TypeError, ValueError):
        duration_frames = None
    return markers, framerate, duration_frames, str(timeline.GetName() or "")
