#!/usr/bin/env python3
"""
Pure-Python file-matching logic for Media Relink - deliberately free of any
PySide6 or DaVinci Resolve import so it can be exercised with plain unit
tests.

Resolve does have a built-in MediaPool.RelinkClips(items, folder) call that
searches a folder for matching media on Resolve's own terms, but its
matching/recursion behavior isn't documented precisely enough to build a
predictable "here's exactly what will happen" preview around - callers
would have to trust it and check afterward. Building the filename index
ourselves means every proposed match can be shown to the user (and
overridden) before anything is actually relinked.
"""

import os
import re

# Case-insensitive filename comparison everywhere below - Resolve projects
# routinely move between Windows and macOS/network-share workflows where a
# clip's recorded path and the file's real path may disagree only in case.


# Resolve records an image sequence as one path with its frame range in
# brackets: "R[5207310-5207311].jpg" is R5207310.jpg and R5207311.jpg on
# disk. No file has the bracketed name, so a sequence is checked - and
# found in a search - by its first frame.
_SEQUENCE = re.compile(r"^(.*)\[(\d+)-(\d+)\]([^\[\]]*)$")


def sequence_first_frame(path):
    """The first frame's own path for an image sequence's recorded path,
    or None when the path isn't a sequence's."""
    folder, name = os.path.split(path or "")
    match = _SEQUENCE.match(name)
    if not match:
        return None
    return os.path.join(folder, match.group(1) + match.group(2) + match.group(4))


def media_exists(path):
    """Whether a clip's recorded path is on disk - a sequence by its first
    frame (a file really named with brackets counts as itself too)."""
    if os.path.exists(path):
        return True
    first = sequence_first_frame(path)
    return bool(first) and os.path.exists(first)


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class SearchCancelled(Exception):
    """build_file_index was asked to stop."""


def build_file_index(search_root, progress=None, should_stop=None):
    """Recursively walks search_root and returns a dict mapping a
    lowercased filename to a list of every full path found with that name
    (a list, not a single path, because the same filename can legitimately
    exist more than once under a search folder - e.g. per-camera-card
    exports that reuse the same numbering).

    progress(files_seen) is called after each folder; should_stop() is
    checked there too, raising SearchCancelled when it returns True - a
    walk over a big network share can take minutes."""
    if not os.path.isdir(search_root):
        raise NotADirectoryError(search_root)

    def raise_walk_error(error):
        # os.walk silently skips unreadable or vanished folders by default.
        # A partial index must not be reported as a completed search.
        raise error

    index = {}
    seen = 0
    for dirpath, _dirnames, filenames in os.walk(search_root, onerror=raise_walk_error):
        for filename in filenames:
            key = filename.lower()
            full_path = os.path.join(dirpath, filename)
            index.setdefault(key, []).append(full_path)
        seen += len(filenames)
        if should_stop is not None and should_stop():
            raise SearchCancelled()
        if progress is not None:
            progress(seen)
    return index


def find_candidates(old_path, file_index):
    """Given a clip's old (offline) recorded path, returns the list of
    candidate replacement paths found in file_index by exact (case
    -insensitive) filename match. Empty list means no match found. A
    candidate list of length 1 is an unambiguous match; length > 1 means
    the caller should surface a choice rather than silently picking one.

    An image sequence is found by its first frame, and proposed as the
    same bracketed name in the folder that frame is in."""
    if not old_path:
        return []
    name = os.path.basename(old_path)
    candidates = list(file_index.get(name.lower(), []))
    first = sequence_first_frame(old_path)
    if first:
        for frame in file_index.get(os.path.basename(first).lower(), []):
            path = os.path.join(os.path.dirname(frame), name)
            if not any(same_path(path, c) for c in candidates):
                candidates.append(path)
    return candidates


class MatchStatus:
    ONLINE = "online"
    MATCH_FOUND = "match_found"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


def classify_match(is_online, candidates):
    """Turns an online/offline flag plus a candidate list into one of the
    MatchStatus values used to drive both the tree's status column and
    which rows a "Relink All Matches" pass should touch."""
    if is_online:
        return MatchStatus.ONLINE
    if len(candidates) == 1:
        return MatchStatus.MATCH_FOUND
    if len(candidates) > 1:
        return MatchStatus.AMBIGUOUS
    return MatchStatus.NOT_FOUND
