#!/usr/bin/env python3
"""
Putting a motion preset (motion.py) on the clips selected in Resolve's
timeline, and taking it off again. Runs on the Previews tab's ResolveWorker.

How a clip is animated: its Fusion comp (made with AddFusionComp when it has
none - a still or a video clip; a Text+ already has one) gets a Merge slotted
in just before MediaOut, over a clear Background:

    <whatever fed MediaOut> -> BuddyMotion (Merge, foreground) -> MediaOut1
    BuddyMotionCanvas (Background, alpha 0) -> BuddyMotion (background)

and the Merge's Center (through an XYPath), Size, Angle and Blend are keyed
with BezierSplines: motion.plan's few eased keys, handles and all, written
to each spline whole (SetKeyFrames). A Merge rather than
a Transform: Blend over a clear background is a real fade, where a
Transform's Blend mixes in the unmoved picture. Nothing already in the comp
is changed, so the user's own Fusion work stays, and the Buddy tools carry
tool data ("BuddyMotion") so a second Apply replaces them and Remove takes
exactly them out.

Measured on Studio 21.1 (2026-09-30): nodes and keys built through the API
in a clip's comp read back right, and even ExportFusionComp shows them, but
the Edit page never renders them - it renders the same comp once it's been
through ExportFusionComp + ImportFusionComp. So every change here ends with
that round trip (_reload).

The canvas is the timeline's size, so a move can start or end off-screen.
A clip's comp works at its SOURCE size (a 641x479 still, a 3840x2160 clip),
and a Merge draws its foreground at its own pixel size, so the Merge scales
it the way Resolve would have fitted it to the timeline (fit_scale: the
clip's Scaling, else the project's input-size setting) and the preset's
Size keys are that times the preset's scale.

The clip's Edit-page framing (Inspector Zoom, Position, Rotation, Anchor,
Flip, Crop) is built into the comp in front of the move (framing.py) and the
Inspector set back to neutral: the Inspector works on the comp's finished
frame, so framing left there would cut the move off at that frame's edge.
Re-frame in the Inspector afterwards and Update framing (or Apply) folds
it in; Remove puts it back.

Resolve 21.0.4 or later: Timeline.GetSelectedClips.
"""

import os
import tempfile
import time

from . import framing as fr

TAG = "BuddyMotion"                 # tool data key on the Merge: the preset id
MERGE_NAME, CANVAS_NAME = "BuddyMotion", "BuddyMotionCanvas"
CROP_NAME, SHAPE_NAME = "BuddyMotionCrop", "BuddyMotionShape"   # the framing, in front of the Merge
FRAMING = "BuddyMotionFraming"      # tool data on the Merge: framing.dumps() of what came in
FAR = -32768                        # AddTool's "don't place it in the flow" position
LOOK_AT = 40                        # summary(): comps opened per read, at most

# What a selected clip is, for the summary and for what can be animated.
IMAGE, VIDEO, TITLE, OTHER, NOT_VIDEO = "image", "video", "title", "other", "not_video"
ANIMATABLE = (IMAGE, VIDEO, TITLE, OTHER)

# What moves in the Previews tiles: the selected clips' kind (whichever
# there's most of), or the card with nothing to animate selected.
_SHAPES = {IMAGE: "photo", VIDEO: "clip", TITLE: "title", OTHER: "clip"}
DEFAULT_SHAPE = "card"


def preview_shape(counts):
    """The tile shape for a selection's {kind: n}."""
    best = max((k for k in _SHAPES if counts.get(k)), key=lambda k: counts[k], default=None)
    return _SHAPES[best] if best else DEFAULT_SHAPE


# How Resolve fits a clip of another size to the timeline. A clip's own
# Scaling property (0 = the project's) and the timeline/project setting
# timelineInputResMismatchBehavior, as read on Studio 21.1.
FIT, FILL, CROP, STRETCH = "fit", "fill", "crop", "stretch"
_CLIP_SCALING = {1: CROP, 2: FIT, 3: FILL, 4: STRETCH}
_PROJECT_SCALING = {"scaleToFit": FIT, "scaleToCrop": FILL, "centerCrop": CROP, "stretch": STRETCH}


def scaling_mode(clip_scaling, project_setting):
    """FIT / FILL / CROP / STRETCH for one clip."""
    try:
        mode = _CLIP_SCALING.get(int(clip_scaling or 0))
    except (TypeError, ValueError):
        mode = None
    return mode or _PROJECT_SCALING.get(str(project_setting or ""), FIT)


def fit_scale(width, height, canvas_w, canvas_h, mode=FIT):
    """The Merge Size that puts a width x height picture on the canvas the way
    Resolve fits it: whole (FIT), filling it (FILL), at its own pixels (CROP).
    A Merge scales both ways alike, so STRETCH is drawn as FIT."""
    if not (width and height and canvas_w and canvas_h):
        return 1.0
    sx, sy = canvas_w / width, canvas_h / height
    if mode == CROP:
        return 1.0
    return max(sx, sy) if mode == FILL else min(sx, sy)


class SelectionUnavailable(RuntimeError):
    """This Resolve can't say what's selected (older than 21.0.4)."""


# -------------------------------------------------------------- selection --

def current_timeline(controller):
    project = controller.current_project()
    timeline = project.GetCurrentTimeline() if project else None
    return project, timeline


def selected_items(timeline):
    """The timeline's selected clips, video tracks first, in track and time
    order - audio and subtitle clips too (classify() tells them apart)."""
    get = getattr(timeline, "GetSelectedClips", None)
    if not callable(get):
        raise SelectionUnavailable("Applying to selected clips needs DaVinci Resolve 21.0.4 or later.")
    items = []
    for item in get() or []:
        try:
            track_type, index = item.GetTrackTypeAndIndex()
        except Exception:
            track_type, index = "video", 0
        items.append((0 if track_type == "video" else 1, int(index or 0), item.GetStart(), item, track_type))
    items.sort(key=lambda row: row[:3])
    return [(item, track_type) for *_rest, item, track_type in items]


def classify(item, track_type):
    if track_type != "video":
        return NOT_VIDEO
    try:
        pool_item = item.GetMediaPoolItem()
    except Exception:
        pool_item = None
    if pool_item is None:
        return TITLE                # Text+, other titles, generators, adjustment clips
    try:
        kind = str(pool_item.GetClipProperty("Type") or "")
    except Exception:
        kind = ""
    if kind == "Still":
        return IMAGE
    if kind.startswith("Video"):
        return VIDEO
    return OTHER                    # compound, multicam, nested timeline...


def summary(controller):
    """{"timeline", "counts": {kind: n}, "total", "animated"} for the
    selection - "animated" is how many carry a Buddy preset (so Remove
    knows whether there's anything to take off)."""
    _project, timeline = current_timeline(controller)
    if timeline is None:
        return {"timeline": "", "counts": {}, "total": 0, "animated": 0}
    counts, animated, reframed, looked = {}, 0, 0, 0
    for item, track_type in selected_items(timeline):
        kind = classify(item, track_type)
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ANIMATABLE:
            # The page asks every couple of seconds, and each Resolve call
            # holds Buddy up while it runs: past LOOK_AT clips, count them
            # as maybe animated rather than open every comp.
            looked += 1
            if looked > LOOK_AT or _has_motion(item):
                animated += 1
                if looked <= LOOK_AT and not fr.is_neutral(read_inspector(item)):
                    reframed += 1
    return {"timeline": timeline.GetName(), "counts": counts, "total": sum(counts.values()),
            "animated": animated, "reframed": reframed}


def _has_motion(item):
    """A cheap look: does the clip's comp hold a Buddy Merge? No comp, no."""
    try:
        if not item.GetFusionCompCount():
            return False
        comp = item.GetFusionCompByIndex(1)
        return comp is not None and _buddy_merge(comp) is not None
    except Exception:
        return False


# ------------------------------------------------------------------ comps --

def _buddy_merge(comp):
    for tool in (comp.GetToolList(False, "Merge") or {}).values():
        try:
            if tool.GetData(TAG):
                return tool
        except Exception:
            continue
    return None


def _media_out(comp):
    tool = comp.FindTool("MediaOut1")
    if tool is None:
        found = list((comp.GetToolList(False, "MediaOut") or {}).values())
        tool = found[0] if found else None
    return tool


def _source_tool(inp):
    """The tool feeding an input, or None."""
    out = inp.GetConnectedOutput()
    return out.GetTool() if out is not None else None


def _delete_feeding(inp):
    """Deletes the spline (or an XYPath and its two splines) feeding an input."""
    tool = _source_tool(inp)
    if tool is None:
        return
    if tool.GetAttrs("TOOLS_RegID") == "XYPath":
        for axis in ("X", "Y"):
            spline = _source_tool(getattr(tool, axis))
            if spline is not None:
                spline.Delete()
    tool.Delete()


def _take_out(comp, merge):
    """Removes a Buddy Merge, its canvas, its keys and the framing tools in
    front of it, and reconnects what fed them to MediaOut. That, or None."""
    media_out = _media_out(comp)
    upstream = _source_tool(merge.Foreground)
    for name in ("Size", "Angle", "Blend", "Center"):
        _delete_feeding(getattr(merge, name))
    canvas = _source_tool(merge.Background)
    merge.Delete()
    if canvas is not None and canvas.GetAttrs("TOOLS_Name").startswith(CANVAS_NAME):
        canvas.Delete()
    while upstream is not None and upstream.GetAttrs("TOOLS_Name").startswith((CROP_NAME, SHAPE_NAME)):
        before = _source_tool(upstream.Input)
        upstream.Delete()
        upstream = before
    if media_out is not None and upstream is not None:
        media_out.ConnectInput("Input", upstream)
    return upstream


def read_inspector(item):
    """The clip's Inspector framing values (framing.KEYS) - one Resolve call:
    GetProperty() with no key gives them all."""
    try:
        every = item.GetProperty() or {}
    except Exception:
        every = {}
    if not isinstance(every, dict):
        every = {}
    return {k: every[k] for k in fr.KEYS if k in every}


def write_inspector(item, values):
    """Sets Inspector values, ZoomGang first (it links ZoomY to ZoomX). Resolve
    21.1 won't set an Anchor Point axis to 0 while the other one is 0
    (SetProperty returns False, the old value stays), so a refused 0 goes in
    as 1e-9 - nothing on screen, and neutral to framing.is_neutral."""
    for key in ("ZoomGang",) + tuple(k for k in values if k != "ZoomGang"):
        if key not in values:
            continue
        value = values[key]
        try:
            if not item.SetProperty(key, value) and isinstance(value, float) and value == 0.0:
                item.SetProperty(key, 1e-9)
        except Exception:
            pass


def _shifted(keys, add=0.0, times=1.0):
    """Keys (and their handle heights) times `times`, plus `add`."""
    move = lambda v: v * times + add
    return {f: {"value": move(k["value"]),
                "lh": k.get("lh") and (k["lh"][0], move(k["lh"][1])),
                "rh": k.get("rh") and (k["rh"][0], move(k["rh"][1]))} for f, k in keys.items()}


def spline_table(keys, base=0.0, times=1.0):
    """motion.plan's keys for one channel as BezierSpline.SetKeyFrames takes
    them: {frame: {1: value, "LH": {1: dt, 2: dv}, "RH": ...}}, moved to
    start at `base` and values (handle heights too) times `times`. A handle
    there is its OFFSET from its key, in frames and value - not a point on
    the curve (measured 21.1: a flat stretch's own handles read back as
    +/- a third of the gap, 0; the .comp file then writes key + offset)."""
    table = {}
    for frame, key in keys.items():
        value = key["value"] * times
        entry = {1: value}
        for side, handle in (("LH", key.get("lh")), ("RH", key.get("rh"))):
            if handle:
                entry[side] = {1: handle[0] - frame, 2: handle[1] * times - value}
        table[base + frame] = entry
    return table


def _key(comp, inp, keys, base=0.0, times=1.0):
    """A new BezierSpline on an input, holding exactly `keys` and their ease
    handles, written whole and BEFORE it's connected. A comp Resolve has
    already drawn sits at some frame (where the playhead was), and a spline
    made there starts with a key on that frame; once the spline feeds an
    input, SetKeyFrames(..., True) can't replace that key, and it bends the
    curve (Studio 21.1: a Text+ drawn at 2 s kept a key at frame 48). Keying
    the input a frame at a time hit the same key and sent Text+ off its
    path."""
    spline = comp.AddTool("BezierSpline")
    spline.SetKeyFrames(spline_table(keys, base, times), True)
    if not inp.ConnectTo(spline.Value):
        raise RuntimeError("couldn't connect a keyframe curve")


def _reload(item):
    """Export the comp and import it again - the Edit page only renders what
    comes in that way (see the module note). True when it went through."""
    handle, path = tempfile.mkstemp(prefix="buddy_motion_", suffix=".comp")
    os.close(handle)
    try:
        if not item.ExportFusionComp(path, 1):
            return False
        if not item.ImportFusionComp(path):
            return False
        names = item.GetFusionCompNameList() or []
        if names:
            item.LoadFusionCompByName(names[-1])
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def comp_frames(comp, clip_frames):
    """(first frame, frame count) of a clip's comp. A video clip's comp runs
    at its SOURCE rate - a 56-frame clip of 29.97 fps footage on a 24 fps
    timeline has a 69-frame comp (measured 21.1) - with frame 0 on the
    clip's first frame even when it's trimmed. A still's and a Text+'s
    match the clip."""
    attrs = comp.GetAttrs() or {}
    start, end = attrs.get("COMPN_RenderStart"), attrs.get("COMPN_RenderEnd")
    if start is None or end is None or end < start:
        return 0.0, clip_frames
    return float(start), int(round(end - start)) + 1


def apply(item, preset_id, plan_for, clip_frames, at=None, canvas=None, mode=FIT, inspector=None, options=None):
    """Puts a preset on one clip. `plan_for(frames, rate, at)` is motion.plan
    for it, called with the comp's own frame count and the timeline's rate
    scaled to the comp (`rate` and `at` in comp frames), so a move takes as
    long on screen whatever the footage's rate. `canvas` is the timeline's
    (width, height) - None keeps the comp's own - and `mode` how Resolve
    fits this clip to it (scaling_mode).

    `inspector` is the clip's Inspector framing (read_inspector): it's built
    into the comp in front of the move (framing.py) and the Inspector is set
    back to neutral. On a clip Buddy already animated, it's what changed
    since and goes on top of the framing already in. `options` ({"way",
    "speed", "at"}) are kept for Update framing. Raises with a reason."""
    count = item.GetFusionCompCount() or 0
    if count > 1:
        raise RuntimeError("it has several Fusion compositions")
    comp = item.AddFusionComp() if count == 0 else item.GetFusionCompByIndex(1)
    if comp is None:
        raise RuntimeError("Resolve gave no Fusion composition for it")
    media_out = _media_out(comp)
    if media_out is None:
        raise RuntimeError("its Fusion composition has no MediaOut")
    base, frames = comp_frames(comp, clip_frames)
    ratio = frames / max(1, clip_frames)
    keys = plan_for(frames, ratio, None if at is None else at * ratio)
    frame = tuple(canvas) if canvas else (comp.GetPrefs("Comp.FrameFormat.Width"), comp.GetPrefs("Comp.FrameFormat.Height"))
    width = comp.GetPrefs("Comp.FrameFormat.Width") or frame[0]
    height = comp.GetPrefs("Comp.FrameFormat.Height") or frame[1]
    fit = fit_scale(width, height, frame[0], frame[1], mode) if canvas else 1.0
    shown = (width * fit, height * fit)

    old = _buddy_merge(comp)
    before = fr.loads(old.GetData(FRAMING)) if old is not None else None
    raw = inspector or {}
    if before:                          # re-framed since: on top of what's in
        place = fr.compose(fr.from_inspector(raw, frame, frame), before["place"])
        original = before.get("raw") if fr.is_neutral(raw) else None
        shown = tuple(before.get("shown") or shown)
    else:
        place = fr.from_inspector(raw, shown, frame)
        original = raw
    put = fr.merge_inputs(place, fit, frame)
    comp.Lock()
    comp.StartUndo("Buddy motion preset")
    try:
        upstream = _take_out(comp, old) if old is not None else _source_tool(media_out.Input)
        if upstream is None:
            raise RuntimeError("nothing is connected to its MediaOut")
        l, r, t, b = put["crop"]
        if any(v > 1e-6 for v in (l, r, t, b)):
            crop = comp.AddTool("Crop", FAR, FAR)
            crop.SetAttrs({"TOOLS_Name": CROP_NAME})
            crop.ConnectInput("Input", upstream)
            crop.SetInput("XOffset", l)
            crop.SetInput("YOffset", b)
            crop.SetInput("XSize", max(1.0, width - l - r))
            crop.SetInput("YSize", max(1.0, height - t - b))
            upstream = crop
        if put["flip"] or abs(put["shape"][0] - 1) > 1e-6 or abs(put["shape"][1] - 1) > 1e-6:
            shape = comp.AddTool("Transform", FAR, FAR)
            shape.SetAttrs({"TOOLS_Name": SHAPE_NAME})
            shape.ConnectInput("Input", upstream)
            shape.SetInput("UseSizeAndAspect", 0)
            shape.SetInput("XSize", put["shape"][0])
            shape.SetInput("YSize", put["shape"][1])
            shape.SetInput("FlipVert", 1 if put["flip"] else 0)
            upstream = shape
        backdrop = comp.AddTool("Background", FAR, FAR)
        merge = comp.AddTool("Merge", FAR, FAR)
        backdrop.SetAttrs({"TOOLS_Name": CANVAS_NAME})
        merge.SetAttrs({"TOOLS_Name": MERGE_NAME})
        backdrop.SetInput("TopLeftAlpha", 0)
        if canvas:
            backdrop.SetInput("UseFrameFormatSettings", 0)
            backdrop.SetInput("Width", int(frame[0]))
            backdrop.SetInput("Height", int(frame[1]))
        merge.ConnectInput("Background", backdrop)
        merge.ConnectInput("Foreground", upstream)
        media_out.ConnectInput("Input", merge)
        merge.SetData(TAG, preset_id)
        merge.SetData(FRAMING, fr.dumps({"raw": original, "place": place, "shown": shown,
                                         "options": options or {}}))
        size, turn = put["size"], put["angle"]
        dx, dy = put["centre"]
        centre = {0: {"value": 0.5}}
        if "Size" in keys:
            _key(comp, merge.Size, keys["Size"], base, size)
        elif abs(size - 1.0) > 1e-9:
            merge.SetInput("Size", size)
        if "Angle" in keys:
            _key(comp, merge.Angle, _shifted(keys["Angle"], turn), base)
        elif turn:
            merge.SetInput("Angle", turn)
        if "Blend" in keys:
            _key(comp, merge.Blend, keys["Blend"], base)
        if "Center.X" in keys or "Center.Y" in keys:
            merge.AddModifier("Center", "XYPath")
            path = _source_tool(merge.Center)
            if path is None:
                raise RuntimeError("couldn't attach a motion path")
            _key(comp, path.X, _shifted(keys.get("Center.X") or centre, dx), base)
            _key(comp, path.Y, _shifted(keys.get("Center.Y") or centre, dy), base)
        elif dx or dy:
            merge.SetInput("Center", {1: 0.5 + dx, 2: 0.5 + dy})
    finally:
        comp.EndUndo(True)
        comp.Unlock()
    if not _reload(item):
        raise RuntimeError("Resolve didn't take the updated Fusion composition")
    # The framing is in the comp now; the Inspector goes back to neutral (a
    # crop that couldn't come in stays).
    neutral = {k: v for k, v in fr.NEUTRAL.items() if k not in fr.CROPS or fr.crop_comes_in(raw)}
    write_inspector(item, {"ZoomGang": True, **neutral})


def remove(item):
    """Takes Buddy's preset off a clip: its comp is as it was before, less the
    Buddy tools, and the framing Buddy took in goes back to the Inspector
    (with any re-framing since on top). False when it had none. A comp Buddy
    made stays, empty (MediaIn straight to MediaOut, which looks like the
    clip did): Resolve won't delete a clip's last composition
    (DeleteFusionCompByName returns False, 21.1)."""
    if not item.GetFusionCompCount():
        return False
    comp = item.GetFusionCompByIndex(1)
    merge = _buddy_merge(comp) if comp is not None else None
    if merge is None:
        return False
    record = fr.loads(merge.GetData(FRAMING))
    canvas = _source_tool(merge.Background)
    frame = (canvas.GetInput("Width"), canvas.GetInput("Height")) if canvas is not None else None
    comp.Lock()
    try:
        _take_out(comp, merge)
    finally:
        comp.Unlock()
    if not _reload(item):
        raise RuntimeError("Resolve didn't take the updated Fusion composition")
    if record and frame and frame[0] and frame[1]:
        now = read_inspector(item)
        if fr.is_neutral(now) and record.get("raw"):
            back = {k: record["raw"][k] for k in fr.KEYS if k in record["raw"] and k not in ("CropSoftness", "CropRetain")}
        else:
            place = fr.compose(fr.from_inspector(now, frame, frame), record["place"])
            back = fr.to_inspector(place, record.get("shown") or frame, frame)
        write_inspector(item, back)
    return True


# ---------------------------------------------------------------- the jobs --

def _timecode_frames(timecode, fps):
    """'HH:MM:SS:FF' (';' for drop frame) to a frame count."""
    drop = ";" in timecode
    h, m, s, f = (int(part) for part in timecode.replace(";", ":").split(":"))
    rate = int(round(fps))
    frames = ((h * 60 + m) * 60 + s) * rate + f
    if drop:
        dropped = int(round(fps * 0.066666))
        minutes = h * 60 + m
        frames -= dropped * (minutes - minutes // 10)
    return frames


def run_apply(controller, preset, plan_for, name_of=lambda item: item.GetName(), options=None):
    """Applies `preset` to every animatable selected clip. `plan_for(frames,
    rate, at)` gives motion.plan's keys for one clip's comp, `rate` being the
    comp's frames per timeline frame (see apply). Returns
    {"applied", "skipped": {kind: n}, "failed": [(clip, reason)], "seconds"}."""
    started = time.monotonic()
    _project, timeline = current_timeline(controller)
    if timeline is None:
        raise RuntimeError("There's no timeline open in Resolve.")
    fps = float(timeline.GetSetting("timelineFrameRate") or 24)
    project = controller.current_project()
    setting = lambda key: timeline.GetSetting(key) or (project.GetSetting(key) if project else "")
    try:
        canvas = (int(setting("timelineResolutionWidth")), int(setting("timelineResolutionHeight")))
    except (TypeError, ValueError):
        canvas = None
    fitting = setting("timelineInputResMismatchBehavior")
    try:
        playhead = _timecode_frames(timeline.GetCurrentTimecode(), fps)
    except Exception:
        playhead = None
    applied, skipped, failed = 0, {}, []
    for item, track_type in selected_items(timeline):
        kind = classify(item, track_type)
        if kind not in ANIMATABLE:
            skipped[kind] = skipped.get(kind, 0) + 1
            continue
        frames = int(item.GetDuration() or 0)
        at = None
        if playhead is not None and item.GetStart() <= playhead < item.GetEnd():
            at = playhead - item.GetStart()
        try:
            try:
                clip_scaling = item.GetProperty("Scaling")
            except Exception:
                clip_scaling = 0
            apply(item, preset["id"], lambda n, ratio, a: plan_for(n, fps * ratio, a), frames, at,
                  canvas, scaling_mode(clip_scaling, fitting), read_inspector(item),
                  {**(options or {}), "at": at})
            applied += 1
        except Exception as exc:  # noqa: BLE001 - one clip's trouble is reported, the rest go on
            failed.append((name_of(item), str(exc)))
    return {"applied": applied, "skipped": skipped, "failed": failed,
            "seconds": round(time.monotonic() - started, 2)}


def run_remove(controller):
    """Takes Buddy's presets off every selected clip that has one."""
    _project, timeline = current_timeline(controller)
    if timeline is None:
        raise RuntimeError("There's no timeline open in Resolve.")
    removed, failed = 0, []
    for item, track_type in selected_items(timeline):
        if classify(item, track_type) not in ANIMATABLE:
            continue
        try:
            removed += bool(remove(item))
        except Exception as exc:  # noqa: BLE001
            failed.append((item.GetName(), str(exc)))
    return {"removed": removed, "failed": failed}


def run_update(controller, plan_for_preset, name_of=lambda item: item.GetName()):
    """Update framing: every selected clip Buddy animated gets its own preset
    again, with the options it went on with, and whatever the Inspector
    framing has become since built in. `plan_for_preset(preset_id, options)`
    is a plan_for for run_apply's apply, or None for a preset Buddy no
    longer has. Returns {"updated", "failed": [(clip, reason)]}."""
    _project, timeline = current_timeline(controller)
    if timeline is None:
        raise RuntimeError("There's no timeline open in Resolve.")
    fps = float(timeline.GetSetting("timelineFrameRate") or 24)
    project = controller.current_project()
    setting = lambda key: timeline.GetSetting(key) or (project.GetSetting(key) if project else "")
    try:
        canvas = (int(setting("timelineResolutionWidth")), int(setting("timelineResolutionHeight")))
    except (TypeError, ValueError):
        canvas = None
    fitting = setting("timelineInputResMismatchBehavior")
    updated, failed = 0, []
    for item, track_type in selected_items(timeline):
        if classify(item, track_type) not in ANIMATABLE or not item.GetFusionCompCount():
            continue
        merge = _buddy_merge(item.GetFusionCompByIndex(1))
        if merge is None:
            continue
        preset_id = merge.GetData(TAG)
        record = fr.loads(merge.GetData(FRAMING)) or {}
        options = record.get("options") or {}
        plan_for = plan_for_preset(preset_id, options)
        if plan_for is None:
            failed.append((name_of(item), "its preset isn't in this Buddy"))
            continue
        try:
            clip_scaling = item.GetProperty("Scaling")
        except Exception:
            clip_scaling = 0
        try:
            apply(item, preset_id, lambda n, ratio, a: plan_for(n, fps * ratio, a), int(item.GetDuration() or 0),
                  options.get("at"), canvas, scaling_mode(clip_scaling, fitting), read_inspector(item), options)
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed.append((name_of(item), str(exc)))
    return {"updated": updated, "failed": failed}
