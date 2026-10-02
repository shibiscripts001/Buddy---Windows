#!/usr/bin/env python3
"""
What a link bar folder's Peek window shows (core/link_peek_web.py): a
folder's contents - folders first, then files, each with the link bar's
icon for its kind - and where Peek may go from there. No Qt, so it's
unit-tested.

Peek stays inside the folder the link points at (its "root"): it can go
down into subfolders and back up, never above the root, and only paths
under it can be opened or dragged out.
"""

import os
import stat
from pathlib import Path

from core import link_bar

# More than this in one folder and the rest are left out (and counted).
MAX_ENTRIES = 1000

# Images the page can draw itself as a thumbnail, if they aren't too big to
# load for a small picture.
THUMB_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
MAX_THUMB_BYTES = 25 * 1024 * 1024

_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 4)


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def within(path, root):
    """True if `path` is `root` or anything under it."""
    try:
        return os.path.commonpath([_key(path), _key(root)]) == _key(root)
    except (TypeError, ValueError):       # another drive, or not a path at all
        return False


def _hidden(entry):
    if entry.name.startswith("."):
        return True
    try:
        return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & _HIDDEN)
    except OSError:
        return True


def human_size(size):
    """1536 -> "1.5 KB"."""
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{size} bytes" if size != 1 else "1 byte"
            return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.0f} TB"


def _crumbs(path, root):
    """[(name, path)] from the root down to `path`."""
    out = []
    here = os.path.abspath(path)
    while True:
        out.append((link_bar.default_name(here), here))
        if _key(here) == _key(root):
            break
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return out[::-1]


def list_folder(path, root):
    """{"path", "name", "up", "crumbs", "entries", "more", "error"} for a
    folder under `root`. "up" is the folder above, or None at the root;
    each entry is {"name", "path", "kind", "folder", "size", "thumb"} -
    "thumb" the file:// address of an image small enough to draw, or None.
    A path outside the root lists the root instead."""
    root = os.path.abspath(root)
    path = os.path.abspath(path) if path and within(path, root) else root
    state = {"path": path, "name": link_bar.default_name(path),
             "up": None if _key(path) == _key(root) else os.path.dirname(path),
             "crumbs": [{"name": n, "path": p} for n, p in _crumbs(path, root)],
             "entries": [], "more": 0, "error": None}
    try:
        with os.scandir(path) as it:
            found = [e for e in it if not _hidden(e)]
    except FileNotFoundError:
        state["error"] = "This folder isn't there any more."
        return state
    except PermissionError:
        state["error"] = "Windows won't let Buddy look in this folder."
        return state
    except OSError as exc:
        state["error"] = f"Couldn't read this folder: {exc.strerror or exc}"
        return state

    entries = []
    for e in found:
        try:
            folder = e.is_dir()
            size = None if folder else e.stat().st_size
        except OSError:
            continue
        lower = e.name.lower()
        thumb = (not folder and lower.endswith(THUMB_TYPES) and size is not None and size <= MAX_THUMB_BYTES)
        entries.append({"name": e.name, "path": e.path, "folder": folder,
                        "kind": "folder" if folder else link_bar.auto_icon(e.path),
                        "size": None if folder else human_size(size),
                        "thumb": Path(e.path).as_uri() if thumb else None})
    entries.sort(key=lambda x: (not x["folder"], x["name"].lower()))
    state["entries"] = entries[:MAX_ENTRIES]
    state["more"] = max(0, len(entries) - MAX_ENTRIES)
    return state


def allowed(paths, root):
    """The paths Peek may hand on (opened, dragged out): those that are
    there and under the root, in order, each once."""
    out, seen = [], set()
    for p in paths if isinstance(paths, list) else []:
        if not isinstance(p, str) or not p or not within(p, root) or not os.path.exists(p):
            continue
        if _key(p) not in seen:
            seen.add(_key(p))
            out.append(os.path.abspath(p))
    return out
