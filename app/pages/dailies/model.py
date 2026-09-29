"""Dailies classification and note-to-metadata rules (no Qt or Resolve)."""

import os
import re

# Resolve's clip colours, in the order its own Clip Color picker shows them.
from pages.project_setup.metadata import COLORS

VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".mts", ".m2ts", ".webm", ".braw", ".r3d", ".ari"}
AUDIO_EXTS = {".wav", ".mp3", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".dpx", ".exr"}
TAGS = {"1": "Good Take", "0": "Untagged", "-1": "Rejected"}
# How long a still plays on the tape (page.py's still timer).
STILL_SECONDS = 5.0
EDITABLE = {"Scene", "Shot", "Take", "Camera #", "Reel Number"}
BEGIN = "[Buddy Dailies]"
END = "[/Buddy Dailies]"


def media_type(kind, path):
    kind = str(kind or "").lower()
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in VIDEO_EXTS or kind in ("video", "movie"):
        return "Video"
    if ext in AUDIO_EXTS or kind == "audio":
        return "Audio"
    if ext in IMAGE_EXTS or kind in ("still", "image"):
        return "Image"
    return "Other"


def clip_seconds(kind, frames, fps):
    """How long a clip runs on the source tape: frames / fps from the Media
    Pool, a still's five seconds, or the same for anything Resolve gives no
    length for (it plays no longer - see page.py's still and failure timers)."""
    if kind == "Image":
        return STILL_SECONDS
    try:
        frames = float(str(frames).strip())
        fps = float(re.match(r"\s*([\d.]+)", str(fps)).group(1))
    except (AttributeError, TypeError, ValueError):
        return STILL_SECONDS
    return frames / fps if frames > 0 and fps > 0 else STILL_SECONDS


def quick_fields(log):
    """Recognize short standalone tokens in free notes; the notes stay intact."""
    rules = (("Scene", r"(?<!\w)s\s*0*(\d+[a-z]?)(?!\w)"),
             ("Shot", r"(?<!\w)sh\s*0*(\d+[a-z]?)(?!\w)"),
             ("Take", r"(?<!\w)t\s*0*(\d+[a-z]?)(?!\w)"))
    result = {}
    for key, pattern in rules:
        matches = list(re.finditer(pattern, log or "", re.IGNORECASE))
        if matches:
            result[key] = matches[-1].group(1).upper()
    return result


def comments_with_log(existing, log):
    """Replace a prior Buddy block instead of duplicating it on a second pass."""
    existing = str(existing or "")
    previous = re.compile(r"\n?\[Buddy Dailies\]\n.*?\n\[/Buddy Dailies\]", re.DOTALL)
    base = previous.sub("", existing).rstrip()
    note = str(log or "").strip()
    return base + ("\n\n" if base and note else "") + (f"{BEGIN}\n{note}\n{END}" if note else "")


def changes_for(draft, current):
    """Only write fields the reviewer changed, plus tokens from the log."""
    if not isinstance(draft, dict):
        return {}
    result = {}
    log = draft.get("log")
    if isinstance(log, str) and (log.strip() or draft.get("log_edited")):
        result["Comments"] = comments_with_log(current.get("Comments", ""), log)
        result.update(quick_fields(log))
    fields = draft.get("fields") or {}
    if isinstance(fields, dict):
        result.update({key: value.strip() for key, value in fields.items()
                       if key in EDITABLE and isinstance(value, str)})
    tag = draft.get("tag")
    if tag is not None and str(tag) in TAGS:
        result["Tag"] = str(tag)
    color = draft.get("color")
    if color is not None and (color == "" or color in COLORS):
        result["Clip Color"] = color
    return {key: value for key, value in result.items() if str(current.get(key, "")) != value}
