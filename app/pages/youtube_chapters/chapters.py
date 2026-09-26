#!/usr/bin/env python3
"""
YouTube chapter lists from timeline markers - no Qt, no Resolve, so it's
unit-tested (tests/test_youtube_chapters.py).

YouTube's rules, all applied here:
  - the first chapter is at 00:00;
  - chapters are at least 10 seconds apart;
  - it takes at least 3 chapters for the video to get them.

How they're applied:
  - a first marker under 10 s in is MOVED to 00:00 and keeps its name;
  - with a colour filter on, that 10 s check looks at the first chapter
    actually kept, not the first marker of any colour;
  - markers dropped for being too close are reported, not silently lost.
"""

import re

from core.marker_colors import numeric_markers

ALL = "All"
MIN_GAP_SECONDS = 10
MIN_CHAPTERS = 3
DEFAULT_FILE_NAME = "YouTube_Chapters.txt"
ILLEGAL_FILENAME_CHARS = r'[\\/*?:"<>|]'


def format_timecode(seconds):
    """'MM:SS', or 'H:MM:SS' style ('01:02:03') past an hour - YouTube's."""
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def file_name_for(timeline_name):
    clean = re.sub(ILLEGAL_FILENAME_CHARS, "", timeline_name or "").strip().replace(" ", "_")
    return f"{clean}_Chapters.txt" if clean else DEFAULT_FILE_NAME


def build(markers, framerate, color=ALL):
    """Chapters from a GetMarkers() dict (keys: frames from the timeline's
    start). Returns {"chapters": [...], "skipped": [...], "warnings": [...],
    "text": "..."}; each chapter/skip is {time, seconds, name, color, note}."""
    fps = float(framerate or 24.0) or 24.0
    chapters, skipped = [], []
    last = None
    for frame, info in numeric_markers(markers):
        marker_color = info.get("color", "")
        if color != ALL and marker_color != color:
            continue
        seconds = int(frame / fps)
        name = (info.get("name") or "").strip() or "Chapter"
        entry = {"time": format_timecode(seconds), "seconds": seconds, "name": name,
                 "color": marker_color, "note": ""}
        if last is not None and seconds - last < MIN_GAP_SECONDS:
            entry["note"] = f"Only {seconds - last} s after the chapter before – YouTube needs {MIN_GAP_SECONDS} s."
            skipped.append(entry)
            continue
        # A first chapter under 10 s in goes to 00:00 (below), so the next
        # one is measured from there.
        last = 0 if not chapters and seconds < MIN_GAP_SECONDS else seconds
        chapters.append(entry)

    if chapters and chapters[0]["seconds"] != 0:
        first = chapters[0]
        if first["seconds"] < MIN_GAP_SECONDS:
            first["note"] = f"Moved from {first['time']} – YouTube's first chapter is at 00:00."
            first["seconds"], first["time"] = 0, format_timecode(0)
        else:
            chapters.insert(0, {"time": "00:00", "seconds": 0, "name": "Intro", "color": "",
                                "note": "Added – YouTube's first chapter is at 00:00."})

    warnings = []
    if chapters and len(chapters) < MIN_CHAPTERS:
        warnings.append(f"YouTube needs at least {MIN_CHAPTERS} chapters before it shows them – this has {len(chapters)}.")
    return {
        "chapters": chapters,
        "skipped": skipped,
        "warnings": warnings,
        "text": "\n".join(f"{c['time']} - {c['name']}" for c in chapters),
    }
