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
to each spline whole (SetKeyFrames). An In and an Out follow the clip's
trims: their keys sit on carriers (unconnected Transforms, BuddyMotionKeys*)
and the Merge plays them through an expression that counts the In from the
comp's first frame and the Out back from its last (time_map) - so a clip
trimmed in the Edit page keeps its whole move, with Buddy closed. A Merge rather than
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
CARRIER = "BuddyMotionKeys"         # + channel: an unconnected Transform holding its keys (_keyed)
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
    knows whether there's anything to take off) - and "clip": the one
    animatable clip's {"name", "frames"} when there's only one (the Editor
    lays its curves out at that clip's length), else None."""
    _project, timeline = current_timeline(controller)
    if timeline is None:
        return {"timeline": "", "counts": {}, "total": 0, "animated": 0}
    counts, animated, reframed, looked, clips = {}, 0, 0, 0, []
    for item, track_type in selected_items(timeline):
        kind = classify(item, track_type)
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ANIMATABLE:
            clips.append(item)
            # The page asks every couple of seconds, and each Resolve call
            # holds Buddy up while it runs: past LOOK_AT clips, count them
            # as maybe animated rather than open every comp.
            looked += 1
            if looked > LOOK_AT or _has_motion(item):
                animated += 1
                if looked <= LOOK_AT and not fr.is_neutral(read_inspector(item)):
                    reframed += 1
    try:
        fps = float(timeline.GetSetting("timelineFrameRate"))
    except Exception:
        fps = 24.0
    if fps <= 0:
        fps = 24.0
    clip = None
    if len(clips) == 1:
        try:
            clip = {"name": clips[0].GetName(), "frames": int(clips[0].GetDuration() or 0)}
        except Exception:
            clip = None
    return {"timeline": timeline.GetName(), "fps": fps, "counts": counts,
            "total": sum(counts.values()), "animated": animated, "reframed": reframed, "clip": clip}


def _has_motion(item):
    """A cheap look: does one of the clip's comps hold a Buddy Merge? No comp, no."""
    try:
        return _motion_comp(item) is not None
    except Exception:
        return False


def _motion_comp(item):
    """(index, comp, Merge) for the comp Buddy's preset is in, or None. The
    newest that holds one: every change comes back in as a new comp
    (_swap_in), and Buddy 1.1.36 left the one it came from behind - an older
    copy with a Merge of its own, which this passes over."""
    for index in range(int(item.GetFusionCompCount() or 0), 0, -1):
        comp = item.GetFusionCompByIndex(index)
        merge = _buddy_merge(comp) if comp is not None else None
        if merge is not None:
            return index, comp, merge
    return None


def _working_comp(item):
    """(index, comp, Buddy Merge or None) for apply: the comp the preset is in,
    else the clip's only comp, else a new one. Raises for a clip with several
    of its own - which one plays isn't something the API says."""
    found = _motion_comp(item)
    if found is not None:
        return found
    count = int(item.GetFusionCompCount() or 0)
    if count > 1:
        raise RuntimeError("it has several Fusion compositions")
    comp = item.AddFusionComp() if count == 0 else item.GetFusionCompByIndex(1)
    if comp is None:
        raise RuntimeError("Resolve gave no Fusion composition for it")
    return 1, comp, None


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


def _carriers(comp):
    """The comp's key carriers (_keyed). Their splines go with them."""
    found = []
    for tool in (comp.GetToolList(False, "Transform") or {}).values():
        try:
            if tool.GetAttrs("TOOLS_Name").startswith(CARRIER):
                found.append(tool)
        except Exception:
            continue
    return found


def _take_out(comp, merge):
    """Removes a Buddy Merge, its canvas, its keys (on it, or on carriers) and
    the framing tools in front of it, and reconnects what fed them to
    MediaOut. That, or None."""
    media_out = _media_out(comp)
    upstream = _source_tool(merge.Foreground)
    for name in ("Size", "Angle", "Blend", "Center"):
        _delete_feeding(getattr(merge, name))
    for carrier in _carriers(comp):
        try:
            _delete_feeding(carrier.Angle)
            carrier.Delete()
        except Exception:
            pass
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


def _num(value):
    """A number as an expression writes it: plain decimals, never 1e+06."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def time_map(base, frames, in_end, out_len):
    """The expression that keeps an In on the clip's first frame and an Out on
    its last, however the clip is trimmed later: the comp frame (`time`) to
    play the keys at, as laid out for a clip of `frames` comp frames from
    `base` - the In over its first `in_end` frames, the Out over its last
    `out_len`. None when there's neither (an Emphasis stays on the frames it
    went on at).

    A trim moves the comp's render range with it (measured 21.1: 5 s still,
    0-119; a second off the end, 0-95; a second off the start, 24-95), so
    the In is counted from comp.RenderStart and the Out back from
    comp.RenderEnd, with the rest held between. A clip trimmed shorter than
    the two moves gets both squeezed in proportion, as plan() does. Only
    what read back right through the API goes in: time, comp.RenderStart,
    comp.RenderEnd, iif() and arithmetic."""
    if in_end <= 0 and out_len <= 0:
        return None
    first, last, total = _num(base), _num(base + frames - 1), _num(in_end + out_len)
    span = "(comp.RenderEnd - comp.RenderStart)"
    squeeze = f"iif({span} < {total}, iif({span} < 1, 1, {span}) / {total}, 1)"
    since = "(time - comp.RenderStart)"
    out = f"({last} - (comp.RenderEnd - time) / {squeeze})"
    rest = _num(base + in_end)
    return (f"iif({since} <= {_num(in_end)} * {squeeze}, {first} + {since} / {squeeze}, "
            f"iif({out} < {rest}, {rest}, {out}))")


def _keyed(comp, inp, channel, keys, base, mapping, times=1.0):
    """Keys for one channel. With a time map they go on a carrier - an
    unconnected Transform named CARRIER + channel, the keys on its Angle -
    and the input plays them through an expression (measured 21.1: a
    Transform's keyed Angle read through Tool:GetValue("Angle", time - 30)
    came back 30 frames late, exactly). Without one, straight on the input."""
    if mapping is None:
        return _key(comp, inp, keys, base, times)
    carrier = comp.AddTool("Transform", FAR, FAR)
    name = CARRIER + channel.replace(".", "")
    carrier.SetAttrs({"TOOLS_Name": name})
    _key(comp, carrier.Angle, keys, base, times)
    inp.SetExpression(f'{name}:GetValue("Angle", {mapping})')


def _temp_comp():
    handle, path = tempfile.mkstemp(prefix="buddy_motion_", suffix=".comp")
    os.close(handle)
    return path


def _forget(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _export(item, index):
    """The clip's comp `index` written to a temp .comp file: its path, or None."""
    path = _temp_comp()
    if item.ExportFusionComp(path, index):
        return path
    _forget(path)
    return None


def _swap_in(item, index, path):
    """Imports a .comp file in place of the clip's comp `index`. Resolve adds
    an import as a new comp and makes it the one that plays, so the comp it
    replaces is deleted after it - along with any older copy still holding a
    Buddy Merge (Buddy 1.1.36 left those) - and the new one takes the old
    one's name. True when the import went through; a delete or rename Resolve
    turns down leaves an extra comp, which _motion_comp passes over."""
    names = list(item.GetFusionCompNameList() or [])
    if not item.ImportFusionComp(path):
        return False
    after = list(item.GetFusionCompNameList() or [])
    if not after:
        return True
    new = after[-1]
    item.LoadFusionCompByName(new)
    old = names[index - 1] if 0 < index <= len(names) else None
    stale = [old] if old else []
    for i, name in enumerate(names, 1):
        if name != old and name != new:
            comp = item.GetFusionCompByIndex(i)
            if comp is not None and _buddy_merge(comp) is not None:
                stale.append(name)
    gone = []
    for name in stale:
        try:
            if item.DeleteFusionCompByName(name):
                gone.append(name)
        except Exception:
            pass
    if old in gone and old != new:
        try:
            item.RenameFusionCompByName(new, old)
            item.LoadFusionCompByName(old)
        except Exception:
            pass
    return True


def _reload(item, index):
    """Export comp `index` and import it in its place - the Edit page only
    renders what comes in that way (see the module note). True when it
    went through."""
    path = _export(item, index)
    if path is None:
        return False
    try:
        return _swap_in(item, index, path)
    finally:
        _forget(path)


def _restore(item, index, backup):
    """Puts the copy saved before a change back in place of comp `index`, so a
    change that failed half way leaves the clip as it was."""
    try:
        _swap_in(item, index, backup)
    except Exception:
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
    if clip_frames < 2:
        raise RuntimeError("it's only one frame long - too short for a move")
    index, comp, old = _working_comp(item)
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
    # The old preset comes out before the new one is built, so a failure half
    # way would leave the clip with neither: the comp as it was goes back.
    backup = _export(item, index)
    if backup is None:
        raise RuntimeError("Resolve wouldn't save a copy of its Fusion composition first")
    try:
        _build(comp, media_out, old, preset_id, keys, base, put, width, height, frame, canvas,
               {"raw": original, "place": place, "shown": shown, "options": options or {}},
               trim_map(keys, base, frames))
        if not _reload(item, index):
            raise RuntimeError("Resolve didn't take the updated Fusion composition")
    except Exception:
        _restore(item, index, backup)
        raise
    finally:
        _forget(backup)
    # The framing is in the comp now; the Inspector goes back to neutral (a
    # crop that couldn't come in stays).
    neutral = {k: v for k, v in fr.NEUTRAL.items() if k not in fr.CROPS or fr.crop_comes_in(raw)}
    write_inspector(item, {"ZoomGang": True, **neutral})


def trim_map(keys, base, frames):
    """time_map for motion.plan's keys on a comp of `frames` frames from
    `base`: how far its In runs from the start and its Out from the end."""
    in_end = out_len = 0
    for name, *_rounded, first, last in keys.get("moves") or []:
        if name == "in":
            in_end = max(in_end, last)
        elif name == "out":
            out_len = max(out_len, frames - 1 - first)
    return time_map(base, frames, in_end, out_len)


def _build(comp, media_out, old, preset_id, keys, base, put, width, height, frame, canvas, record, mapping=None):
    """apply's change to the comp: the old Buddy tools out, the framing and the
    keyed Merge in - its keys played through `mapping` (trim_map) when
    there is one. Raises with a reason."""
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
        merge.SetData(FRAMING, fr.dumps(record))
        size, turn = put["size"], put["angle"]
        dx, dy = put["centre"]
        centre = {0: {"value": 0.5}}
        if "Size" in keys:
            _keyed(comp, merge.Size, "Size", keys["Size"], base, mapping, size)
        elif abs(size - 1.0) > 1e-9:
            merge.SetInput("Size", size)
        if "Angle" in keys:
            _keyed(comp, merge.Angle, "Angle", _shifted(keys["Angle"], turn), base, mapping)
        elif turn:
            merge.SetInput("Angle", turn)
        if "Blend" in keys:
            _keyed(comp, merge.Blend, "Blend", keys["Blend"], base, mapping)
        if "Center.X" in keys or "Center.Y" in keys:
            # A position is a point, and an expression on a point input crashed
            # Resolve 21.1 while it was being tried: the move goes through an
            # XYPath, whose X and Y are plain numbers.
            merge.AddModifier("Center", "XYPath")
            path = _source_tool(merge.Center)
            if path is None:
                raise RuntimeError("couldn't attach a motion path")
            _keyed(comp, path.X, "X", _shifted(keys.get("Center.X") or centre, dx), base, mapping)
            _keyed(comp, path.Y, "Y", _shifted(keys.get("Center.Y") or centre, dy), base, mapping)
        elif dx or dy:
            merge.SetInput("Center", {1: 0.5 + dx, 2: 0.5 + dy})
    finally:
        comp.EndUndo(True)
        comp.Unlock()


def remove(item):
    """Takes Buddy's preset off a clip: its comp is as it was before, less the
    Buddy tools, and the framing Buddy took in goes back to the Inspector
    (with any re-framing since on top). False when it had none. A comp Buddy
    made stays, empty (MediaIn straight to MediaOut, which looks like the
    clip did): Resolve won't delete a clip's last composition
    (DeleteFusionCompByName returns False, 21.1)."""
    found = _motion_comp(item)
    if found is None:
        return False
    index, comp, merge = found
    record = fr.loads(merge.GetData(FRAMING))
    canvas = _source_tool(merge.Background)
    frame = (canvas.GetInput("Width"), canvas.GetInput("Height")) if canvas is not None else None
    backup = _export(item, index)
    if backup is None:
        raise RuntimeError("Resolve wouldn't save a copy of its Fusion composition first")
    try:
        comp.Lock()
        try:
            _take_out(comp, merge)
        finally:
            comp.Unlock()
        if not _reload(item, index):
            raise RuntimeError("Resolve didn't take the updated Fusion composition")
    except Exception:
        _restore(item, index, backup)
        raise
    finally:
        _forget(backup)
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
        if classify(item, track_type) not in ANIMATABLE:
            continue
        found = _motion_comp(item)
        if found is None:
            continue
        merge = found[2]
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
