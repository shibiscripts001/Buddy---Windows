#!/usr/bin/env python3
"""
Audio Assistant's own Resolve calls - free functions taking a controller,
like every tool's resolve_ext.py. The reads the Timeline tab draws, and
the Mixer's writes (apply, crossfade) at the bottom. seek() is only ever run
from resolve_child.py, in a process of its own (see there).

Not used, on purpose: Timeline.NormalizeAudioLevel. On 21.1 it ignores
targetLoudness (EBU R128 at -14 and -23, and YouTube mode, all set the same
volume - measured on a duplicate timeline; targetLevel does work, for the
peak modes) and does nothing on a timeline that isn't the active one. Match
is Buddy's own measurement instead - see levels.py.

What the Timeline tab mirrors, measured on Resolve 21.1.0.17:
  - an audio track's name, sub type ("mono", "stereo", "adaptive16", ...),
    enabled and locked state
  - each clip's record range (GetStart/GetEnd, absolute frames), enabled
    state, clip colour, AudioVolume/AudioPan (GetProperties), fades in
    frames (GetFades), and its media's file path and format
  - where in the file a clip starts: GetSourceStartFrame() in the media's
    own frames (its "FPS" clip property). GetSourceStartTime() is NOT that -
    it's the file's source timecode in seconds (4875.5 for a camera MP4 whose
    clip starts at frame 0). Speed changes aren't read: a retimed clip's
    waveform is drawn at 100%.
  - the playhead (GetCurrentTimecode) and Resolve's own selection
    (GetSelectedClips, matched to the drawn clips by GetUniqueId)
Voice Isolation reads None on clips that don't support it, and only works
on the active timeline - which is the only one read here.

A full read costs ~3 ms per clip (94 clips: 0.3 s); the live read (playhead,
selection, the selected clips' levels) under 1 ms. The page does the live
one twice a second and the full one every few seconds.
"""

import hashlib
import os

from core import marker_colors
from core.resolve_bridge import ResolveConnectionError
from pages.stills_exporter.resolve_ext import timecode_to_frames

# Resolve's 16 clip colours, for display (close to its swatches). "" = none set.
CLIP_COLORS = {
    "Orange": "#E8742B", "Apricot": "#F0A052", "Yellow": "#E2C33D", "Lime": "#9FC43A",
    "Olive": "#76902F", "Green": "#3F9E4D", "Teal": "#2FA39A", "Navy": "#2B4F8F",
    "Blue": "#4A83D8", "Purple": "#8C5BC9", "Violet": "#B066C9", "Pink": "#E0709F",
    "Tan": "#C9A77C", "Beige": "#D8C9A8", "Brown": "#8A5A3A", "Chocolate": "#6B4331",
}


# Resolve's audio transitions (AddTransition category "audio"), as named on the timeline.
CROSSFADES = ["Cross Fade +3 dB", "Cross Fade 0 dB", "Cross Fade -3 dB"]
TRANSITION_PREFIX = "Cross Fade"
# Clip property ranges Resolve accepts (a value outside makes SetProperties refuse everything).
VOLUME_RANGE = (-100.0, 30.0)
PAN_RANGE = (-100.0, 100.0)
LEVELER_GAIN_RANGE = (0.0, 6.0)


def file_key(path):
    """A short id for a media file, shared by every clip that uses it."""
    return hashlib.sha1(os.path.normcase(os.path.normpath(path)).encode("utf-8")).hexdigest()[:16]


def get_timeline(controller):
    project = controller.current_project()
    if project is None:
        raise ResolveConnectionError("No project is open in Resolve.")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise ResolveConnectionError("No timeline is open in Resolve.")
    return timeline


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _call(obj, name, *args, default=None):
    """obj.name(*args), or `default` if Resolve refuses. Resolve's objects
    answer hasattr() True for any name, so a missing method only shows up
    as a failed call."""
    try:
        result = getattr(obj, name)(*args)
    except Exception:  # noqa: BLE001 - a clip or file Resolve can't describe
        return default
    return default if result is None else result


def _media(item, cache):
    """The file under a clip: {path, key, rate, channels, codec, fps} or
    None (a compound clip, a generator). `cache` is kept by the page, keyed
    by media pool item id: GetClipProperty() is the slowest call here."""
    mpi = _call(item, "GetMediaPoolItem")
    if mpi is None:
        return None
    mid = _call(mpi, "GetUniqueId", default="")
    if mid and mid in cache:
        return cache[mid]
    props = _call(mpi, "GetClipProperty", default={}) or {}
    path = str(props.get("File Path") or "")
    media = None
    if path:
        media = {
            "path": path,
            "key": file_key(path),
            "rate": _float(str(props.get("Sample Rate") or "").split()[0] if props.get("Sample Rate") else None),
            "channels": int(_float(props.get("Audio Ch"), 0) or 0),
            "codec": str(props.get("Audio Codec") or ""),
            "fps": _float(props.get("FPS")),
        }
    if mid:
        cache[mid] = media
    return media


def _levels(item):
    """A clip's own audio settings - what the live read refreshes."""
    props = _call(item, "GetProperties", default={}) or {}
    fades = _call(item, "GetFades", default={}) or {}
    volume = _float(props.get("AudioVolume"))
    isolation = props.get("AudioVoiceIsolationEnabled")
    leveler = props.get("AudioDialogueLevelerEnabled")
    return {
        "enabled": bool(_call(item, "GetClipEnabled", default=True)),
        "volume": volume if volume is not None and props.get("AudioVolumeEnabled", True) is not False else 0.0,
        "pan": _float(props.get("AudioPan"), 0.0),
        "fade_in": _float(fades.get("FadeIn"), 0.0),
        "fade_out": _float(fades.get("FadeOut"), 0.0),
        # None: this clip can't have it (Resolve reads None and refuses writes).
        "isolation": None if isolation is None else {
            "on": bool(isolation), "amount": _float(props.get("AudioVoiceIsolationAmount"), 0.0)},
        "leveler": None if leveler is None else {
            "on": bool(leveler),
            "mode": int(_float(props.get("AudioDialogueLevelerMode"), 0.0) or 0),
            "reduce_loud": bool(props.get("AudioDialogueLevelerReduceLoudDialogue")),
            "lift_soft": bool(props.get("AudioDialogueLevelerLiftSoftDialogue")),
            "background": bool(props.get("AudioDialogueLevelerBackgroundReduction")),
            "gain": _float(props.get("AudioDialogueLevelerOutputGain"), 0.0),
        },
    }


def _clip(item, fps, cache):
    uid = _call(item, "GetUniqueId", default="")
    start, end = _call(item, "GetStart"), _call(item, "GetEnd")
    if not uid or start is None or end is None:
        return None
    media = _media(item, cache)
    name = str(_call(item, "GetName", default="") or "")
    if media is None and name.startswith(TRANSITION_PREFIX):
        # An audio crossfade sits in the track's item list like a clip.
        return {"id": uid, "name": name, "start": int(start), "end": int(end), "transition": True}
    src_fps = (media or {}).get("fps") or fps
    clip = {
        "id": uid,
        "name": name,
        "start": int(start),
        "end": int(end),
        "color": CLIP_COLORS.get(_call(item, "GetClipColor", default=""), ""),
        # Seconds into the file where the clip starts.
        "offset": (_float(_call(item, "GetSourceStartFrame"), 0.0) or 0.0) / src_fps if src_fps else 0.0,
        "media": media,
    }
    clip.update(_levels(item))
    return clip


def read_timeline(controller, media_cache):
    """The open timeline's audio: (data for the view, {clip id: item}) -
    the items so the live read can refresh the selected ones."""
    timeline = get_timeline(controller)
    fps = _float(timeline.GetSetting("timelineFrameRate"), 24.0) or 24.0
    start = int(timeline.GetStartFrame())
    tracks, items = [], {}
    for index in range(1, int(timeline.GetTrackCount("audio") or 0) + 1):
        clips = []
        for item in timeline.GetItemListInTrack("audio", index) or []:
            clip = _clip(item, fps, media_cache)
            if clip is not None:
                clips.append(clip)
                items[clip["id"]] = item
        tracks.append({
            "index": index,
            "name": str(_call(timeline, "GetTrackName", "audio", index, default="") or f"Audio {index}"),
            "kind": str(_call(timeline, "GetTrackSubType", "audio", index, default="") or ""),
            "enabled": bool(_call(timeline, "GetIsTrackEnabled", "audio", index, default=True)),
            "locked": bool(_call(timeline, "GetIsTrackLocked", "audio", index, default=False)),
            "clips": clips,
        })
    markers = [{"frame": start + int(frame), "color": marker_colors.HEX.get(info.get("color", ""), "#888888"),
                "name": str(info.get("name") or "")}
               for frame, info in marker_colors.numeric_markers(_call(timeline, "GetMarkers", default={}))]
    resolve = getattr(controller, "resolve", None)
    data = {
        "id": str(_call(timeline, "GetUniqueId", default="") or ""),
        "name": str(timeline.GetName() or ""),
        # Voice Isolation and the Dialogue Leveler are probably Studio's only.
        "studio": "Studio" in str(_call(resolve, "GetProductName", default="Studio") if resolve else "Studio"),
        "fps": fps,
        "drop_frame": str(_call(timeline, "GetSetting", "timelineDropFrameTimecode", default="0")) == "1",
        "start": start,
        "end": int(timeline.GetEndFrame()),
        "tracks": tracks,
        "markers": markers,
    }
    return data, items


def read_live(controller, fps, selected_items):
    """What changes by the second: {timeline id, playhead frame, the clip ids
    Resolve has selected, {id: levels} for the clips Buddy has selected}."""
    timeline = get_timeline(controller)
    timecode = _call(timeline, "GetCurrentTimecode", default="")
    selected = []
    for item in _call(timeline, "GetSelectedClips", default=[]) or []:
        uid = _call(item, "GetUniqueId", default="")
        if uid:
            selected.append(uid)
    return {
        "id": str(_call(timeline, "GetUniqueId", default="") or ""),
        "playhead": timecode_to_frames(timecode, fps) if timecode else None,
        "resolve_selection": selected,
        "levels": {uid: _levels(item) for uid, item in selected_items.items()},
    }


# ------------------------------------------------------------- writes --
#
# Each finds the clips afresh by id and reads its result back through fresh
# handles again, so what the page shows is what Resolve has - not what was
# asked for. Measured on a duplicate timeline: SetProperties and SetFades
# take 0-2 ms and don't hold the GIL, so they run on the page's worker.

def audio_items(controller):
    """(timeline, {clip id: item}) for the open timeline's audio - fresh handles."""
    timeline = get_timeline(controller)
    items = {}
    for index in range(1, int(timeline.GetTrackCount("audio") or 0) + 1):
        for item in timeline.GetItemListInTrack("audio", index) or []:
            uid = _call(item, "GetUniqueId", default="")
            if uid:
                items[uid] = item
    return timeline, items


def _fresh_levels(controller, ids):
    _timeline, items = audio_items(controller)
    return {uid: _levels(items[uid]) for uid in ids if uid in items}


def apply(controller, changes):
    """Sets clips' properties and fades. changes: {clip id: {"props": {Resolve
    key: value}, "fades": {"FadeIn": frames, "FadeOut": frames}}} (either part
    may be missing). Returns {"before": the same shape, holding what each clip
    had - apply() it to undo; "after": {id: levels}, read back; "failed": [ids]}.
    SetProperties is all or nothing per clip, so a clip either takes every
    property asked of it or none."""
    _timeline, items = audio_items(controller)
    before, failed = {}, []
    for uid, change in changes.items():
        item = items.get(uid)
        if item is None:
            failed.append(uid)
            continue
        old = {}
        props = change.get("props") or {}
        if props:
            now = _call(item, "GetProperties", default={}) or {}
            old["props"] = {key: now[key] for key in props if now.get(key) is not None}
            if not _call(item, "SetProperties", props, default=False):
                failed.append(uid)
                continue
        fades = change.get("fades")
        if fades:
            now = _call(item, "GetFades", default={}) or {}
            old["fades"] = {key: int(round(_float(now.get(key), 0.0))) for key in ("FadeIn", "FadeOut")}
            wanted = dict(old["fades"], **{key: int(round(value)) for key, value in fades.items()})
            if not _call(item, "SetFades", wanted, default=False):
                if old.get("props"):
                    _call(item, "SetProperties", old["props"])      # all of it, or none of it
                failed.append(uid)
                continue
        before[uid] = old
    return {"before": before, "after": _fresh_levels(controller, changes), "failed": failed}


def crossfade(controller, left_ids, kind, frames):
    """An audio crossfade over the cut at the end of each clip in left_ids,
    centred on it. Buddy can't take one off again - DeleteClips refuses a
    transition (measured) - so the page asks first. Returns (added, failed)."""
    _timeline, items = audio_items(controller)
    added, failed = 0, []
    for uid in left_ids:
        item = items.get(uid)
        done = item is not None and _call(item, "AddTransition", {
            "type": kind, "category": "audio", "position": "end", "alignment": "center", "duration": int(frames)})
        if done:
            added += 1
        else:
            failed.append(uid)
    return added, failed


def seek(controller, timecode):
    """Moves Resolve's playhead. ~500 ms, holding the GIL: run it from
    resolve_child.py, never in Buddy itself."""
    if not get_timeline(controller).SetCurrentTimecode(timecode):
        raise ResolveConnectionError("Resolve didn't move the playhead - is the timeline playing?")
