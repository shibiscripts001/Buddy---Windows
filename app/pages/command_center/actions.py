#!/usr/bin/env python3
"""
Command Center's actions: what each one is called, the options it takes,
and the Resolve calls it makes. The page (page.py) runs them - from a
button, or a hot key pressed in any app (core/hotkeys.py).

Each run_* takes the shared Resolve controller and the binding's options,
runs on the page's ResolveWorker, and returns a Result: what to say, and
anything for the UI thread to do with it (put text or a picture on the
clipboard). Anything wrong is an ActionError with a sentence for the
person. They WRITE to Resolve - markers, clip colours, a timeline copy -
only when they're run; nothing here runs on its own.

Clips: the ones selected on the timeline (Resolve 21.0.4's
GetSelectedClips), or with nothing selected - or on an older Resolve -
the clip under the playhead on the top track (GetCurrentVideoItem).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from datetime import datetime

from core.marker_colors import NAMES as MARKER_COLORS, numeric_markers

from pages.stills_exporter.resolve_ext import frames_to_timecode, timecode_to_frames

# Resolve's 16 clip colours (TimelineItem.SetClipColor), in its own menu's order.
CLIP_COLORS = ("Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy", "Blue", "Purple",
               "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate")
CLIP_COLOR_HEX = {"Orange": "#E8742C", "Apricot": "#F2A65A", "Yellow": "#E8C52C", "Lime": "#9ED13B",
                  "Olive": "#5D8A2E", "Green": "#3DA35C", "Teal": "#2AA6A0", "Navy": "#2F4A8C",
                  "Blue": "#4C82D9", "Purple": "#8352C6", "Violet": "#B26BD9", "Pink": "#E05FA6",
                  "Tan": "#C9A16E", "Beige": "#DCCBA6", "Brown": "#8A5A36", "Chocolate": "#5C3B24"}
ANY = "Any"
NO_COLOR = "None"


class ActionError(Exception):
    """Something the person needs to know: the action did nothing."""


class Result(dict):
    """{"text": what to say, "clipboard": text to copy, "image": a PNG's path to copy}."""


def _choices(values, extra=()):
    return [{"id": v, "label": v} for v in (*extra, *values)]


# What the page shows, in order. options: key, label, kind ("select",
# "check", "folder", "preset"), default, choices for a select.
ACTIONS = [
    {"id": "marker", "group": "Timeline", "label": "Add a marker at the playhead",
     "about": "Drops a marker of your colour where the playhead is – one hot key per colour, if you like.",
     "options": [{"key": "color", "label": "Colour", "kind": "select", "default": "Blue",
                  "choices": _choices(MARKER_COLORS), "swatch": "marker"},
                 {"key": "ask", "label": "Ask for its name", "kind": "check", "default": False}]},
    {"id": "next_marker", "group": "Timeline", "label": "Jump to the next marker",
     "about": "Moves the playhead to the next marker after it.",
     "options": [{"key": "color", "label": "Only", "kind": "select", "default": ANY,
                  "choices": _choices(MARKER_COLORS, (ANY,)), "swatch": "marker"}]},
    {"id": "prev_marker", "group": "Timeline", "label": "Jump to the previous marker",
     "about": "Moves the playhead back to the marker before it.",
     "options": [{"key": "color", "label": "Only", "kind": "select", "default": ANY,
                  "choices": _choices(MARKER_COLORS, (ANY,)), "swatch": "marker"}]},
    {"id": "copy_timecode", "group": "Timeline", "label": "Copy the playhead's timecode",
     "about": "Puts the timecode on the clipboard – for notes, a chat or an email.",
     "options": [{"key": "with_clip", "label": "Add the clip's name", "kind": "check", "default": False}]},
    {"id": "clip_color", "group": "Clips", "label": "Set a clip colour",
     "about": "Colours the selected clips – or, with nothing selected, the clip under the playhead.",
     "options": [{"key": "color", "label": "Colour", "kind": "select", "default": "Orange",
                  "choices": _choices(CLIP_COLORS, ()) + [{"id": NO_COLOR, "label": "No colour"}],
                  "swatch": "clip"}]},
    {"id": "animation", "group": "Clips", "label": "Apply an Animation preset",
     "about": "Puts one of Animation's presets on the selected clips, as its Apply button does.",
     "options": [{"key": "preset", "label": "Preset", "kind": "preset", "default": ""},
                 {"key": "way", "label": "Put on", "kind": "select", "default": "both",
                  "choices": [{"id": "both", "label": "In & Out"}, {"id": "in", "label": "In"},
                              {"id": "out", "label": "Out"}]}]},
    {"id": "copy_frame", "group": "Export", "label": "Copy the frame as an image",
     "about": "Puts the frame in the viewer on the clipboard as a picture, ready to paste into a chat or email.",
     "options": [{"key": "folder", "label": "Also save it in", "kind": "folder", "default": ""}]},
    {"id": "save_version", "group": "Export", "label": "Save a version of the timeline",
     "about": "Copies the timeline as “Name – 1 Oct 14.32” in the same bin, and keeps you on the one you're editing.",
     "options": []},
    {"id": "remove_gaps", "group": "Timeline", "label": "Remove gaps (experimental)",
     "about": "Closes the empty stretches between clips – only where every track is empty, so nothing slips out of sync.",
     "options": [{"key": "keep_copy", "label": "Save a version first", "kind": "check", "default": True}]},
]
BY_ID = {a["id"]: a for a in ACTIONS}
GROUPS = ("Timeline", "Clips", "Export")


def default_options(action_id: str) -> dict:
    return {o["key"]: o["default"] for o in BY_ID[action_id]["options"]}


def clean_options(action_id: str, raw) -> dict:
    """A binding's options as saved, each one checked against what it takes."""
    raw = raw if isinstance(raw, dict) else {}
    out = default_options(action_id)
    for o in BY_ID[action_id]["options"]:
        value = raw.get(o["key"], o["default"])
        if o["kind"] == "check":
            out[o["key"]] = bool(value)
        elif o["kind"] == "select":
            out[o["key"]] = value if value in {c["id"] for c in o["choices"]} else o["default"]
        elif o["kind"] in ("folder", "preset"):
            out[o["key"]] = value if isinstance(value, str) else ""
    return out


def summary(action_id: str, options: dict, preset_names=None) -> str:
    """A binding's name in a list: "Blue marker at the playhead"."""
    o = options
    if action_id == "marker":
        return f"{o['color']} marker at the playhead" + (", asking its name" if o.get("ask") else "")
    if action_id in ("next_marker", "prev_marker"):
        which = "Next" if action_id == "next_marker" else "Previous"
        return f"{which} marker" if o["color"] == ANY else f"{which} {o['color']} marker"
    if action_id == "copy_timecode":
        return "Copy timecode and clip name" if o.get("with_clip") else "Copy timecode"
    if action_id == "clip_color":
        return "Clear the clip colour" if o["color"] == NO_COLOR else f"{o['color']} clip colour"
    if action_id == "remove_gaps":
        return "Remove gaps" if o.get("keep_copy") else "Remove gaps, no copy first"
    if action_id == "animation":
        name = (preset_names or {}).get(o.get("preset"), "a preset")
        return f"Animation: {name}"
    return BY_ID[action_id]["label"]


# ---------------------------------------------------------------- Resolve --

def _project(controller):
    project = controller.current_project() if controller else None
    if project is None:
        raise ActionError("No project is open in Resolve.")
    return project


def _timeline(controller):
    project = _project(controller)
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise ActionError("There's no timeline open in Resolve.")
    return project, timeline


def _rate(timeline):
    try:
        fps = float(timeline.GetSetting("timelineFrameRate") or 24)
    except (TypeError, ValueError):
        fps = 24.0
    drop = str(timeline.GetSetting("timelineDropFrameTimecode")) == "1"
    return fps, drop


def playhead(controller) -> dict:
    """Where the playhead is: {"timecode", "frame" (from the timeline's start)}."""
    _project_, timeline = _timeline(controller)
    fps, _drop = _rate(timeline)
    timecode = timeline.GetCurrentTimecode()
    if not timecode:
        raise ActionError("Resolve didn't say where the playhead is.")
    return {"timecode": timecode, "frame": timecode_to_frames(timecode, fps) - int(timeline.GetStartFrame() or 0)}


def add_marker(controller, frame: int, color: str, name: str = "", timecode: str = "") -> Result:
    _project_, timeline = _timeline(controller)
    if color not in MARKER_COLORS:
        color = "Blue"
    # Resolve refuses a marker with no name, and a second marker on one frame.
    if not timeline.AddMarker(int(frame), color, name or "Marker", "", 1, ""):
        if int(frame) in dict(numeric_markers(timeline.GetMarkers())):
            raise ActionError("There's already a marker at the playhead.")
        raise ActionError("Resolve didn't add the marker.")
    where = f" at {timecode}" if timecode else ""
    return Result(text=f"{color} marker{where}" + (f": {name}" if name else ""))


def run_marker(controller, options: dict) -> Result:
    at = playhead(controller)
    return add_marker(controller, at["frame"], options["color"], "", at["timecode"])


def jump(controller, forward: bool, color: str = ANY) -> Result:
    _project_, timeline = _timeline(controller)
    fps, drop = _rate(timeline)
    start = int(timeline.GetStartFrame() or 0)
    here = timecode_to_frames(timeline.GetCurrentTimecode(), fps) - start
    markers = [(f, info) for f, info in numeric_markers(timeline.GetMarkers())
               if color == ANY or info.get("color") == color]
    target = (next(((f, i) for f, i in markers if f > here), None) if forward
              else next(((f, i) for f, i in reversed(markers) if f < here), None))
    if target is None:
        which = "" if color == ANY else f"{color} "
        raise ActionError(f"No {which}marker {'after' if forward else 'before'} the playhead.")
    frame, info = target
    timecode = frames_to_timecode(int(frame) + start, fps, drop)
    if not timeline.SetCurrentTimecode(timecode):
        raise ActionError("Resolve didn't move the playhead.")
    name = info.get("name") or ""
    label = name if name not in ("", "Marker") else f"{info.get('color', '')} marker"
    return Result(text=f"{label} – {timecode}")


def run_copy_timecode(controller, options: dict) -> Result:
    _project_, timeline = _timeline(controller)
    timecode = timeline.GetCurrentTimecode()
    if not timecode:
        raise ActionError("Resolve didn't say where the playhead is.")
    text = timecode
    if options.get("with_clip"):
        item = timeline.GetCurrentVideoItem()
        name = item.GetName() if item else ""
        if name:
            text = f"{timecode} – {name}"
    return Result(text=f"Copied {text}", clipboard=text)


def target_clips(timeline) -> list:
    """The selected clips (subtitles left out), else the clip under the playhead."""
    get = getattr(timeline, "GetSelectedClips", None)
    items = []
    if callable(get):
        try:
            for item in get() or []:
                try:
                    kind, _index = item.GetTrackTypeAndIndex()
                except Exception:   # noqa: BLE001 - an item that won't say is still a clip
                    kind = "video"
                if kind != "subtitle":
                    items.append(item)
        except Exception:   # noqa: BLE001 - an older Resolve: the clip under the playhead instead
            items = []
    if not items:
        item = timeline.GetCurrentVideoItem()
        items = [item] if item else []
    return items


def run_clip_color(controller, options: dict) -> Result:
    _project_, timeline = _timeline(controller)
    items = target_clips(timeline)
    if not items:
        raise ActionError("Select some clips, or put the playhead over one.")
    color = options["color"]
    done = 0
    for item in items:
        ok = item.ClearClipColor() if color == NO_COLOR else item.SetClipColor(color)
        done += bool(ok)
    if not done:
        raise ActionError("Resolve didn't change the clip colour.")
    what = "No colour" if color == NO_COLOR else color
    return Result(text=f"{what} on 1 clip" if done == 1 else f"{what} on {done} clips")


def _safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", text).strip(" .-") or "Frame"


def run_copy_frame(controller, options: dict) -> Result:
    project, timeline = _timeline(controller)
    path = os.path.join(tempfile.gettempdir(), f"buddy-frame-{os.getpid()}.png")
    try:
        os.remove(path)
    except OSError:
        pass
    if not project.ExportCurrentFrameAsStill(path) or not os.path.isfile(path):
        raise ActionError("Resolve didn't export the frame – is a clip in the viewer?")
    text = "Frame copied"
    folder = options.get("folder") or ""
    if folder:
        timecode = (timeline.GetCurrentTimecode() or "").replace(":", ".").replace(";", ".")
        name = _safe_name(f"{timeline.GetName() or 'Frame'} {timecode}") + ".png"
        try:
            os.makedirs(folder, exist_ok=True)
            shutil.copyfile(path, os.path.join(folder, name))
            text = f"Frame copied, and saved as {name}"
        except OSError as exc:
            text = f"Frame copied – but it couldn't be saved in that folder: {exc.strerror or exc}"
    return Result(text=text, image=path)


def version_name(name: str, taken, now: float) -> str:
    """"Main edit – 1 Oct 14.32", with (2), (3)... if that's taken. Resolve
    refuses a timeline name with any of : / \\ ? * " < > |, so no colon."""
    stamp = datetime.fromtimestamp(now).strftime("%d %b %H.%M").lstrip("0")
    base = f"{name} – {stamp}"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base} ({n})", n + 1
    return candidate


def run_save_version(controller, options: dict) -> Result:
    project, timeline = _timeline(controller)
    name = timeline.GetName() or "Timeline"
    count = int(project.GetTimelineCount() or 0)
    taken = set()
    for i in range(1, count + 1):
        t = project.GetTimelineByIndex(i)
        if t is not None:
            taken.add(t.GetName())
    new_name = version_name(name, taken, time.time())
    copy = timeline.DuplicateTimeline(new_name)
    # Resolve makes the copy the current timeline: back to the one being edited.
    project.SetCurrentTimeline(timeline)
    if copy is None:
        raise ActionError("Resolve didn't copy the timeline.")
    return Result(text=f"Saved “{new_name}”", name=new_name)


# ------------------------------------------------------------ Remove gaps --
# Resolve's API has no "close gap". What works (tested on 21.1): put a filler
# clip in each gap on V1 and ripple-delete the fillers - DeleteClips(.., True)
# moves every track along, so the cuts above and the sound below stay in sync.

def _track_items(timeline, kinds=("video", "audio", "subtitle")):
    for kind in kinds:
        for track in range(1, int(timeline.GetTrackCount(kind) or 0) + 1):
            for item in timeline.GetItemListInTrack(kind, track) or []:
                yield kind, track, item


def empty_stretches(spans) -> list:
    """The (start, end) stretches between clips where nothing is on any track.
    Space before the first clip is left alone (it may be a countdown)."""
    gaps, reach = [], None
    for start, end in sorted(spans):
        if reach is not None and start > reach:
            gaps.append((reach, start))
        reach = end if reach is None else max(reach, end)
    return gaps


def shifted(frame: int, gaps) -> int:
    """Where a frame lands once the gaps (relative frames) are closed."""
    moved = 0
    for start, end in gaps:
        if frame >= end:
            moved += end - start
        elif frame > start:
            moved += frame - start
    return frame - moved


def _filler_sources(timeline, fps):
    """Media Pool clips with video already on this timeline, longest first,
    the timeline's own frame rate before others."""
    seen, out = set(), []
    for kind, _track, item in _track_items(timeline, ("video",)):
        try:
            clip = item.GetMediaPoolItem()
        except Exception:   # noqa: BLE001 - a generator or title has none
            clip = None
        if clip is None:
            continue
        key = clip.GetUniqueId() if hasattr(clip, "GetUniqueId") else id(clip)
        if key in seen:
            continue
        seen.add(key)
        try:
            frames = int(float(clip.GetClipProperty("Frames") or 0))
            rate = float(clip.GetClipProperty("FPS") or 0)
        except (TypeError, ValueError):
            continue
        if frames > 1:
            out.append((abs(rate - fps) > 0.01, -frames, frames, clip))
    return [(frames, clip) for *_sort, frames, clip in sorted(out, key=lambda r: r[:2])]


def run_remove_gaps(controller, options: dict) -> Result:
    project, timeline = _timeline(controller)
    for kind in ("video", "audio"):
        for track in range(1, int(timeline.GetTrackCount(kind) or 0) + 1):
            if timeline.GetIsTrackLocked(kind, track):
                raise ActionError(f"Unlock {kind[0].upper()}{track} first – a locked track wouldn't move along.")
    spans = [(int(i.GetStart()), int(i.GetEnd())) for _k, _t, i in _track_items(timeline)]
    gaps = empty_stretches(spans)
    if not gaps:
        return Result(text="No gaps between clips.")
    fps, _drop = _rate(timeline)
    sources = _filler_sources(timeline, fps)
    if not sources:
        raise ActionError("Remove gaps needs a video clip on this timeline to work with.")

    copy = ""
    if options.get("keep_copy"):
        copy = run_save_version(controller, {})["name"]

    pool = project.GetMediaPool()
    fillers = []

    def take_back():
        if fillers:
            timeline.DeleteClips(fillers, False)

    for start, end in gaps:
        at = start
        while at < end:
            for frames, clip in sources:
                length = min(end - at, frames)
                got = pool.AppendToTimeline([{"mediaPoolItem": clip, "startFrame": 0, "endFrame": length,
                                              "recordFrame": at, "trackIndex": 1, "mediaType": 1}])
                item = got[0] if got else None
                if item is None:
                    continue
                fillers.append(item)
                if (int(item.GetStart()), int(item.GetEnd())) == (at, at + length):
                    break
                # A clip at another frame rate lands longer or shorter: try the next.
                timeline.DeleteClips([fillers.pop()], False)
            else:
                take_back()
                raise ActionError("Resolve wouldn't fill a gap exactly, so nothing was moved.")
            at += length

    origin = int(timeline.GetStartFrame() or 0)
    markers_before = timeline.GetMarkers() or {}
    if not timeline.DeleteClips(fillers, True):
        take_back()
        raise ActionError("Resolve didn't close the gaps.")
    # Timeline markers: Resolve's ripple moves those after a gap (21.1), but
    # one inside a gap stays put and lands inside the next clip - put it where
    # the gap closed. (Moving them all ourselves too, should Resolve not.)
    rel_gaps = [(a - origin, b - origin) for a, b in gaps]
    rippled = (timeline.GetMarkers() or {}) != markers_before
    for frame, info in numeric_markers(markers_before):
        frame = int(frame)
        now = frame - sum(b - a for a, b in rel_gaps if b <= frame) if rippled else frame
        new = shifted(frame, rel_gaps)
        if new == now:
            continue
        current = dict(numeric_markers(timeline.GetMarkers() or {}))
        if now not in current:
            continue
        rest = (info.get("color", "Blue"), info.get("name") or "Marker", info.get("note", ""),
                int(info.get("duration", 1) or 1), info.get("customData", ""))
        timeline.DeleteMarkerAtFrame(now)
        # Two markers in one closed gap land on one frame: keep the second where it was.
        if not timeline.AddMarker(new, *rest):
            timeline.AddMarker(now, *rest)
    seconds = f"{sum(b - a for a, b in gaps) / fps:.1f}"
    closed = "Closed 1 gap" if len(gaps) == 1 else f"Closed {len(gaps)} gaps"
    if copy:
        return Result(text=f"{closed} ({seconds} s) – the old one is saved as “{copy}”")
    return Result(text=f"{closed} ({seconds} s)")


RUNNERS = {
    "marker": run_marker,
    "next_marker": lambda c, o: jump(c, True, o["color"]),
    "prev_marker": lambda c, o: jump(c, False, o["color"]),
    "copy_timecode": run_copy_timecode,
    "clip_color": run_clip_color,
    "copy_frame": run_copy_frame,
    "save_version": run_save_version,
    "remove_gaps": run_remove_gaps,
}
