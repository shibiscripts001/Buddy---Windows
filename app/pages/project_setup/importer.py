#!/usr/bin/env python3
"""
The Import Folder tab's rules, without Qt: which files count as media, a
quick look at a folder before importing it, and the import itself - the
folder's tree recreated as bins, each subfolder's media imported into its
matching bin.

Resolve calls stay on the main thread (the scripting bridge isn't
thread-safe), so the import runs synchronously and calls `progress` after
each folder to keep the window painting.
"""

import os
import re
import time

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v", ".wmv", ".mts", ".m2ts",
    ".braw", ".r3d", ".ari", ".arx", ".webm", ".flv", ".vob", ".gxf", ".lxf",
}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".aac", ".flac", ".aiff", ".aif", ".m4a", ".ogg", ".wma"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".dpx", ".tga",
                    ".psd", ".bmp", ".dng"}
# Anything else under the folder (project files, notes, thumbnails) is
# skipped rather than handed to Resolve to reject one file at a time.
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS

# A run of at least this many numbered stills (A_0001.dpx, A_0002.dpx...)
# is an image sequence, which Resolve imports as one clip.
SEQUENCE_MIN = 3
_NUMBERED = re.compile(r"^(.*?)(\d+)$")

# The pre-import look stops here, so choosing a huge share can't hang
# Buddy - the import itself still takes everything.
SCAN_FILE_LIMIT = 50000
SCAN_SECONDS = 2.0

INFO_TEXT = (
    "Choose a folder anywhere on disk. Every media file under it, subfolders "
    "included, is imported into the Media Pool, with the same folder structure "
    "recreated as bins: one bin named after the folder, and a bin inside it for "
    "every subfolder.\n\n"
    "Numbered stills (an image sequence) come in as one clip. Files that aren't "
    "media – project files, notes, thumbnails – are skipped. Empty folders still "
    "get a bin, so the structure matches exactly."
)


def kind_of(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def media_in(files):
    """The media files among `files` (names in one folder), sorted."""
    return sorted(f for f in files if kind_of(f))


def count_items(files):
    """How many clips `files` (media names in one folder) should become:
    {"video", "audio", "image", "sequence"}. An image sequence counts once."""
    counts = {"video": 0, "audio": 0, "image": 0, "sequence": 0}
    runs = {}
    for name in files:
        kind = kind_of(name)
        if kind != "image":
            counts[kind] += 1
            continue
        stem, ext = os.path.splitext(name)
        match = _NUMBERED.match(stem)
        if match:
            runs.setdefault((match.group(1), len(match.group(2)), ext.lower()), []).append(name)
        else:
            counts["image"] += 1
    for members in runs.values():
        if len(members) >= SEQUENCE_MIN:
            counts["sequence"] += 1
        else:
            counts["image"] += len(members)
    return counts


def scan(folder):
    """A quick summary of what importing `folder` would bring in:
    {"folders", "video", "audio", "image", "sequence", "skipped", "partial"}.
    partial is True when the look stopped early (see SCAN_FILE_LIMIT)."""
    summary = {"folders": 0, "video": 0, "audio": 0, "image": 0, "sequence": 0,
               "skipped": 0, "partial": False}
    started = time.monotonic()
    seen = 0
    for _current, subdirs, files in os.walk(folder):
        summary["folders"] += len(subdirs)
        media = media_in(files)
        summary["skipped"] += len(files) - len(media)
        for key, value in count_items(media).items():
            summary[key] += value
        seen += len(files)
        if seen >= SCAN_FILE_LIMIT or time.monotonic() - started > SCAN_SECONDS:
            summary["partial"] = True
            break
    return summary


def item_total(summary):
    return summary["video"] + summary["audio"] + summary["image"] + summary["sequence"]


def import_folder(media_pool, folder, destination, log, progress=None):
    """Recreates `folder` as a bin inside `destination` (a Media Pool
    folder) and imports every subfolder's media into its own bin.
    Returns {"imported", "bin" (its name), "top_bin" (the Media Pool
    folder), "failed_bins", "skipped_dirs"}, or None if even the top bin
    couldn't be created."""
    top_name = os.path.basename(os.path.normpath(folder)) or folder
    top_bin = media_pool.AddSubFolder(destination, top_name)
    if not top_bin:
        log(f"Resolve didn't create the bin '{top_name}'.", "error")
        return None

    bins = {folder: top_bin}
    result = {"imported": 0, "bin": top_name, "top_bin": top_bin, "failed_bins": 0, "skipped_dirs": 0}
    for current, subdirs, files in os.walk(folder):
        subdirs.sort()
        parent = bins.get(current)
        if parent is None:
            # Its bin failed to create, so its subfolders can't have one.
            result["skipped_dirs"] += 1
            subdirs[:] = []
            continue
        for sub in subdirs:
            new_bin = media_pool.AddSubFolder(parent, sub)
            if new_bin:
                bins[os.path.join(current, sub)] = new_bin
            else:
                result["failed_bins"] += 1
                log(f"Resolve didn't create a bin for '{sub}'.", "error")

        media = media_in(files)
        if media:
            media_pool.SetCurrentFolder(parent)
            added = media_pool.ImportMedia([os.path.join(current, f) for f in media])
            added_count = len([a for a in (added or []) if a])
            result["imported"] += added_count
            expected = sum(count_items(media).values())
            if added_count < expected:
                where = os.path.relpath(current, os.path.dirname(folder)) or top_name
                log(f"Only {added_count} of {expected} clip(s) came in from '{where}'.", "warn")
        if progress:
            progress(f"Importing {os.path.relpath(current, os.path.dirname(folder))}")
    return result
