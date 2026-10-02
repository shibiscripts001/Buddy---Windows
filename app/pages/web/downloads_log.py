#!/usr/bin/env python3
"""
The Web tab's download history (what the Downloads window lists): one
record per download - its name, where it was saved, its size, the site it
came from, when it started, and how it ended - kept in the Web tab's
settings so the list survives restarts. No Qt, so it's unit-tested.

A record is a plain dict:
    id        unique, ordered by start ("<milliseconds>-<n>")
    name      the saved file's name
    path      where it was saved
    size      bytes (0 until known)
    time      when it started (seconds since the epoch)
    site      the page's site ("example.com"), "" if unknown
    state     "active", "done" or "failed" (cancelled and interrupted too)
    resolve   True once it's been sent to Resolve's Downloads bin

A private tab's downloads are listed this session but never written down
(save() leaves them out).
"""

import os
import time

from core import folder_peek

MAX_HISTORY = 300
STATES = ("active", "done", "failed")

# The Downloads window's kinds, by extension.
KINDS = {
    "video": (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mxf", ".mpg", ".mpeg", ".wmv", ".flv", ".mts",
              ".m2ts", ".braw", ".r3d", ".3gp", ".ts"),
    "audio": (".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".aif", ".aiff", ".wma", ".mid", ".midi"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".psd", ".ai", ".eps",
              ".heic", ".exr", ".dpx", ".ico", ".raw", ".cr2", ".arw", ".nef", ".dng"),
    "document": (".pdf", ".doc", ".docx", ".txt", ".rtf", ".md", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".srt",
                 ".vtt", ".odt", ".ods", ".epub", ".json", ".xml", ".html", ".htm", ".drt", ".drp", ".drx", ".lut",
                 ".cube", ".fcpxml", ".edl", ".otio", ".ttf", ".otf"),
    "archive": (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso", ".dmg", ".exe", ".msi", ".drfx"),
}
_KIND_OF = {ext: kind for kind, exts in KINDS.items() for ext in exts}


def kind_of(name):
    """"video", "audio", "image", "document", "archive" or "other"."""
    return _KIND_OF.get(os.path.splitext(str(name))[1].lower(), "other")


def site_of(url):
    """The site a download came from, for showing: "example.com"."""
    from pages.web import browser
    return browser.site_of(url) or ""


def new_record(serial, name, path, site, now=None, state="active"):
    now = time.time() if now is None else now
    return {"id": f"{int(now * 1000)}-{serial}", "name": str(name), "path": str(path), "size": 0, "time": now,
            "site": str(site or ""), "state": state, "resolve": False}


def clean_history(raw):
    """The saved list, kept to what can be shown: whole records only, an
    unfinished one (Buddy closed mid-download) counted as failed."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name, path, ident = item.get("name"), item.get("path"), item.get("id")
        when, size = item.get("time"), item.get("size", 0)
        if not (isinstance(name, str) and isinstance(path, str) and isinstance(ident, str)
                and isinstance(when, (int, float)) and name and path and ident):
            continue
        state = item.get("state")
        out.append({"id": ident, "name": name, "path": path, "time": float(when),
                    "size": int(size) if isinstance(size, (int, float)) and size > 0 else 0,
                    "site": item.get("site") if isinstance(item.get("site"), str) else "",
                    "state": "failed" if state == "active" else state if state in STATES else "done",
                    "resolve": bool(item.get("resolve"))})
    out.sort(key=lambda r: r["time"])
    return out[-MAX_HISTORY:]


def save(records):
    """What's written down: finished downloads, none from private tabs
    (those carry "private": True)."""
    return clean_history([r for r in records if not r.get("private") and r.get("state") != "active"])[-MAX_HISTORY:]


def trim(records):
    """Newest MAX_HISTORY kept; downloads still going are never dropped."""
    done = [r for r in records if r["state"] != "active"]
    if len(done) <= MAX_HISTORY:
        return records
    drop = {r["id"] for r in done[:len(done) - MAX_HISTORY]}
    return [r for r in records if r["id"] not in drop]


def shown(record, exists, progress=None):
    """A record as the Downloads window draws it."""
    done = record["state"] == "done"
    return {"id": record["id"], "name": record["name"], "folder": os.path.dirname(record["path"]),
            "size": record["size"], "sizeText": folder_peek.human_size(record["size"]) if record["size"] else "",
            "kind": kind_of(record["name"]), "time": record["time"], "site": record["site"],
            "state": record["state"], "exists": bool(exists) if done else False,
            "resolve": bool(record.get("resolve")), "private": bool(record.get("private")),
            "progress": progress}
