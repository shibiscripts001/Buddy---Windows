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

# Case-insensitive filename comparison everywhere below - Resolve projects
# routinely move between Windows and macOS/network-share workflows where a
# clip's recorded path and the file's real path may disagree only in case.


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
    index = {}
    seen = 0
    for dirpath, _dirnames, filenames in os.walk(search_root):
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
    the caller should surface a choice rather than silently picking one."""
    if not old_path:
        return []
    filename = os.path.basename(old_path).lower()
    return list(file_index.get(filename, []))


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
