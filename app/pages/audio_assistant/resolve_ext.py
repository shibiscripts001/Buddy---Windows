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
import tempfile
import uuid
import xml.etree.ElementTree as ET
from fractions import Fraction

from core import marker_colors
from core.resolve_bridge import ResolveConnectionError
from pages.stills_exporter.resolve_ext import timecode_to_frames

from . import keys as K

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
    _read_keys(controller, timeline, fps, start, tracks, items, media_cache)
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


# ----------------------------------------------------------- volume keys --
#
# See keys.py for what Resolve does with them. A curve applied in Buddy is a
# timeline of its own (named "<clip> (Buddy curve)", in the "Buddy Audio" bin,
# with Buddy's note in its Comments) holding the clip with the keys, placed as a
# nested clip on a free track at the clip's place; the clip itself stays, turned
# off. The nested clip's own volume is heard on top of the keys, so the Levels
# controls (and Match) work on it as on any clip.

def _timelines(project):
    """{name: Timeline} for the project's timelines."""
    out = {}
    for index in range(1, int(_call(project, "GetTimelineCount", default=0) or 0) + 1):
        tl = _call(project, "GetTimelineByIndex", index)
        if tl is not None:
            out[str(_call(tl, "GetName", default="") or "")] = tl
    return out


def _curve_of(item, timelines):
    """A Buddy curve clip's (its timeline, note, the clip in it, its media pool
    item) - or None for any other clip. timelines() gives _timelines()."""
    mpi = _call(item, "GetMediaPoolItem")
    name = str(_call(mpi, "GetName", default="") or "") if mpi is not None else ""
    if not K.is_curve_name(name):
        return None
    note = K.read_note(_call(mpi, "GetMetadata", "Comments", default=""))
    tl = timelines().get(name) if note else None
    inner = next(iter(_call(tl, "GetItemListInTrack", "audio", 1, default=[]) or []), None) if tl else None
    return None if inner is None else (tl, note, inner, mpi)


def _exported(controller, timeline):
    """parse_fcp7() of an FCP 7 XML export of the timeline ([] if Resolve won't).
    A few ms, and nothing in the project changes."""
    resolve = getattr(controller, "resolve", None)
    path = os.path.join(tempfile.gettempdir(), f"buddy_keys_{uuid.uuid4().hex[:10]}.xml")
    try:
        kind, none = getattr(resolve, "EXPORT_FCP_7_XML"), getattr(resolve, "EXPORT_NONE")
        if not _call(timeline, "Export", path, kind, none, default=False):
            return []
        with open(path, encoding="utf-8") as f:
            return K.parse_fcp7(f.read())
    except (AttributeError, OSError, ET.ParseError, ValueError):
        return []
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _read_keys(controller, timeline, fps, start, tracks, items, cache):
    """Each clip's "keys" (file-time keys, or None) and "curve" (Buddy's curve
    clips: {"original": the clip it stands in for, "edited": its keys changed in
    Resolve since}, else None). A curve clip also takes the media and offset of
    the clip in it, so its waveform draws."""
    project = controller.current_project()
    found = {}

    def timelines():
        if "all" not in found:
            found["all"] = _timelines(project) if project is not None else {}
        return found["all"]

    plain = []
    for track in tracks:
        for clip in track["clips"]:
            if clip.get("transition"):
                continue
            clip["keys"], clip["curve"] = None, None
            if clip["media"] is None:
                _read_curve(controller, clip, items[clip["id"]], timelines, fps, cache)
            else:
                plain.append(clip)
    if plain:
        keyed = K.match_keys(_exported(controller, timeline), plain, start)
        for clip in plain:
            clip["keys"] = keyed.get(clip["id"]) or None


def _read_curve(controller, clip, item, timelines, fps, cache):
    curve = _curve_of(item, timelines)
    if curve is None:
        return
    tl, note, inner, _mpi = curve
    media = _media(inner, cache)
    if media is None:
        return
    src_fps = media.get("fps") or fps
    inner_offset = (_float(_call(inner, "GetSourceStartFrame"), 0.0) or 0.0) / src_fps
    clip["media"] = media
    # A nested clip's source start counts frames into its timeline: a trim.
    clip["offset"] = inner_offset + (_float(_call(item, "GetSourceStartFrame"), 0.0) or 0.0) / fps
    got = K.match_keys(_exported(controller, tl), [{
        "id": "inner", "name": str(_call(inner, "GetName", default="") or ""), "offset": inner_offset,
        "start": int(_call(inner, "GetStart", default=0)), "end": int(_call(inner, "GetEnd", default=0))}],
        int(_call(tl, "GetStartFrame", default=0))).get("inner", [])
    edited = not K.same_keys(K.expand(note["keys"], fps, inner_offset), got)
    clip["keys"] = (got if edited else note["keys"]) or None
    clip["curve"] = {"original": note["original"], "edited": edited}


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


# What a curve clip takes over from the clip it stands in for (and a new curve
# clip from the one it replaces): everything the Levels and Clean-up controls set.
_CARRIED = ("AudioPan", "AudioVoiceIsolationEnabled", "AudioVoiceIsolationAmount", "AudioDialogueLevelerEnabled",
            "AudioDialogueLevelerMode", "AudioDialogueLevelerReduceLoudDialogue",
            "AudioDialogueLevelerLiftSoftDialogue", "AudioDialogueLevelerBackgroundReduction",
            "AudioDialogueLevelerOutputGain")


def _outer(item):
    """A clip's settings that a curve clip carries on: {"props", "fades", "color"}."""
    props = _call(item, "GetProperties", default={}) or {}
    fades = _call(item, "GetFades", default={}) or {}
    return {
        "props": dict({key: props[key] for key in _CARRIED if props.get(key) is not None},
                      AudioVolume=_float(props.get("AudioVolume"), 0.0)),
        "fades": {key: int(round(_float(fades.get(key), 0.0))) for key in ("FadeIn", "FadeOut")},
        "color": str(_call(item, "GetClipColor", default="") or ""),
    }


def _set_outer(item, outer):
    """_outer()'s settings onto a clip: the volume and pan first (all a clip
    has for certain), then Voice Isolation and the Leveler as one - Resolve
    refuses a whole SetProperties with one key it doesn't take."""
    props = dict(outer["props"])
    base = {key: props.pop(key) for key in ("AudioVolume", "AudioPan") if key in props}
    _call(item, "SetProperties", base)
    if props:
        _call(item, "SetProperties", props)
    if any(outer.get("fades", {}).values()):
        _call(item, "SetFades", outer["fades"])
    if outer.get("color"):
        _call(item, "SetClipColor", outer["color"])


def _walk(folder):
    for clip in _call(folder, "GetClipList", default=[]) or []:
        yield folder, clip
    for sub in _call(folder, "GetSubFolderList", default=[]) or []:
        yield from _walk(sub)


def _folder_of(root, mpi):
    uid = _call(mpi, "GetUniqueId", default="")
    return next((folder for folder, clip in _walk(root) if _call(clip, "GetUniqueId", default="") == uid), root)


def _bin(media_pool):
    """Buddy's bin for curve timelines, made at the top of the media pool if it isn't there."""
    root = media_pool.GetRootFolder()
    for sub in _call(root, "GetSubFolderList", default=[]) or []:
        if _call(sub, "GetName", default="") == K.BIN_NAME:
            return sub
    made = _call(media_pool, "AddSubFolder", root, K.BIN_NAME)
    if made is None:
        raise ResolveConnectionError("Resolve didn't make Buddy's bin for curves.")
    return made


def _free_track(timeline, below, start, end):
    """The first audio track under track `below` with nothing in [start, end),
    on and unlocked - or None."""
    for index in range(below + 1, int(timeline.GetTrackCount("audio") or 0) + 1):
        if not _call(timeline, "GetIsTrackEnabled", "audio", index, default=True) \
                or _call(timeline, "GetIsTrackLocked", "audio", index, default=False):
            continue
        if not any(_call(i, "GetStart", default=0) < end and _call(i, "GetEnd", default=0) > start
                   for i in _call(timeline, "GetItemListInTrack", "audio", index, default=[]) or []):
            return index
    return None


def _place(media_pool, mpi, track, start, frames):
    """A timeline's media pool item placed as a nested clip, audio only, at
    record frame `start` of `track` of the current timeline - the placed item,
    or None. (On a spot that isn't empty Resolve places nothing.)"""
    placed = _call(media_pool, "AppendToTimeline", [{
        "mediaPoolItem": mpi, "startFrame": 0, "endFrame": frames, "mediaType": 2,
        "trackIndex": track, "recordFrame": start}], default=[]) or []
    item = placed[0] if placed else None
    if item is None or _call(item, "GetStart") != start or _call(item, "GetEnd") != start + frames:
        return None
    return item


def apply_curve(controller, clip_id, keys, fps, volume, outer=None):
    """Puts a volume curve on a clip - Resolve keyframes, in a one-clip timeline
    placed as a nested clip where the clip is (see keys.py).

    keys: the curve's own keys (file time), heard with the nested clip's
    `volume` on top. On a clip that's already a Buddy curve, the new one
    replaces it, on its track, taking over its settings; on any other clip it
    goes on the first free track under it (or a new one), the clip's settings
    come with it, and the clip is turned off. outer: _outer()'s settings to
    give it instead (an undone Remove).

    Everything is checked - the clip in the new timeline is the same file,
    from the same frame, as long - and taken back if anything fails. Returns
    {"id": the curve clip, "original": the clip it stands in for, "replaced":
    the curve clip it replaced or None, "track": the audio track it's on}."""
    project = controller.current_project()
    timeline, items = audio_items(controller)
    media_pool = project.GetMediaPool()
    item = items.get(clip_id)
    if item is None:
        raise ResolveConnectionError("That clip isn't on the open timeline any more.")
    kind, track = (_call(item, "GetTrackTypeAndIndex", default=["audio", 1]) or ["audio", 1])[:2]
    start, end = int(item.GetStart()), int(item.GetEnd())
    frames = end - start
    found = {}

    def timelines():
        if "all" not in found:
            found["all"] = _timelines(project)
        return found["all"]

    curve = _curve_of(item, timelines)
    source = curve[2] if curve else item
    original = curve[1]["original"] if curve else clip_id
    mpi = _call(source, "GetMediaPoolItem")
    props = (_call(mpi, "GetClipProperty", default={}) or {}) if mpi is not None else {}
    path = str(props.get("File Path") or "")
    if not path:
        raise ResolveConnectionError("That clip has no audio file Buddy can put a curve on.")
    media_fps = _float(props.get("FPS"), fps) or fps
    media_frame = K.frame_duration(media_fps)
    first = int(round(_float(_call(source, "GetSourceStartFrame"), 0.0) or 0.0))
    source_in = first * media_frame
    if curve:
        # A trimmed curve clip starts that many of its timeline's frames in.
        source_in += int(round(_float(_call(item, "GetSourceStartFrame"), 0.0) or 0.0)) * K.frame_duration(fps)
        first = int(round(source_in / media_frame))
    media_start = timecode_to_frames(str(props.get("Start TC") or "00:00:00:00"), media_fps) * media_frame
    origin = float(source_in)
    before = _outer(item)
    carried = outer or before
    carried = dict(carried, props=dict(carried["props"], AudioVolume=float(volume)))
    name = K.curve_name(str(_call(source, "GetName", default="") or "Clip"), timelines())
    fcpxml = K.nested_fcpxml(name, path, fps, int(timeline.GetStartFrame()), frames, media_start, source_in,
                             int(_float(props.get("Audio Ch"), 1) or 1), K.expand(keys, fps, origin),
                             media_frame=media_frame)
    xml_path = os.path.join(tempfile.gettempdir(), f"buddy_curve_{uuid.uuid4().hex[:10]}.fcpxml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(fcpxml)
    bin_ = _bin(media_pool)
    folder_before = _call(media_pool, "GetCurrentFolder")
    try:
        media_pool.SetCurrentFolder(bin_)
        new_tl = _call(media_pool, "ImportTimelineFromFile", xml_path, {
            "timelineName": name, "importSourceClips": False,
            "sourceClipsFolders": [_folder_of(media_pool.GetRootFolder(), mpi)]})
    finally:
        # The import makes the new timeline the current one, and it goes in the current bin.
        project.SetCurrentTimeline(timeline)
        if folder_before is not None:
            media_pool.SetCurrentFolder(folder_before)
        try:
            os.remove(xml_path)
        except OSError:
            pass
    if new_tl is None:
        raise ResolveConnectionError("Resolve didn't take the curve.")
    placed, disabled, removed = None, False, False
    try:
        inner = next(iter(_call(new_tl, "GetItemListInTrack", "audio", 1, default=[]) or []), None)
        if inner is None or _call(_call(inner, "GetMediaPoolItem"), "GetUniqueId") != _call(mpi, "GetUniqueId") \
                or int(_call(inner, "GetSourceStartFrame", default=-1)) != first \
                or int(inner.GetEnd()) - int(inner.GetStart()) != frames:
            raise ResolveConnectionError("Resolve didn't line the curve up with the clip, so Buddy took it back.")
        got = K.match_keys(_exported(controller, new_tl), [{
            "id": "inner", "name": str(_call(inner, "GetName", default="") or ""), "offset": origin,
            "start": int(inner.GetStart()), "end": int(inner.GetEnd())}], int(_call(new_tl, "GetStartFrame", default=0)))
        if not K.same_keys(K.expand(keys, fps, origin), got.get("inner", [])):
            raise ResolveConnectionError("Resolve didn't keep the curve's keys where Buddy put them, so Buddy took it back.")
        new_mpi = next((c for c in _call(bin_, "GetClipList", default=[]) or []
                        if _call(c, "GetName", default="") == name), None)
        if new_mpi is None or not _call(new_mpi, "SetMetadata", {"Comments": K.note(keys, original)}, default=False):
            raise ResolveConnectionError("Resolve didn't keep Buddy's note on the curve.")
        if curve:
            if not _call(timeline, "DeleteClips", [item], default=False):
                raise ResolveConnectionError("Resolve didn't make room for the new curve.")
            removed = True
            spot = int(track)
        else:
            spot = _free_track(timeline, int(track), start, end)
            if spot is None:
                if not _call(timeline, "AddTrack", "audio", _call(timeline, "GetTrackSubType", "audio", int(track),
                                                                  default="mono") or "mono", default=False):
                    raise ResolveConnectionError("Resolve didn't add a track for the curve.")
                spot = int(timeline.GetTrackCount("audio"))
        placed = _place(media_pool, new_mpi, spot, start, frames)
        if placed is None:
            raise ResolveConnectionError("Resolve didn't place the curve on the timeline.")
        _set_outer(placed, carried)
        if not curve:
            if not _call(item, "SetClipEnabled", False, default=False):
                raise ResolveConnectionError("Resolve didn't turn the clip off under its curve.")
            disabled = True
    except Exception:
        if placed is not None:
            _call(timeline, "DeleteClips", [placed])
        if removed:                       # the curve clip it was to replace goes back
            back = _place(media_pool, curve[3], int(track), start, frames)
            if back is not None:
                _set_outer(back, before)
        if disabled:
            _call(item, "SetClipEnabled", True)
        _call(media_pool, "DeleteTimelines", [new_tl])
        raise
    if curve:
        _call(media_pool, "DeleteTimelines", [curve[0]])
    return {"id": str(_call(placed, "GetUniqueId", default="") or ""), "original": original,
            "replaced": clip_id if curve else None, "track": spot}


def remove_curve(controller, clip_id):
    """Takes a Buddy curve clip off: the clip it stood in for is turned back on,
    the curve clip and its timeline go. Returns what apply_curve() needs to put
    it back: {"original", "keys", "volume", "outer"}."""
    project = controller.current_project()
    timeline, items = audio_items(controller)
    media_pool = project.GetMediaPool()
    item = items.get(clip_id)
    curve = _curve_of(item, lambda: _timelines(project)) if item is not None else None
    if curve is None:
        raise ResolveConnectionError("That clip isn't a Buddy curve any more.")
    tl, note, _inner, _mpi = curve
    outer = _outer(item)
    original = items.get(note["original"])
    if original is not None:
        _call(original, "SetClipEnabled", True)
    if not _call(timeline, "DeleteClips", [item], default=False):
        if original is not None:
            _call(original, "SetClipEnabled", False)
        raise ResolveConnectionError("Resolve didn't take the curve clip off.")
    _call(media_pool, "DeleteTimelines", [tl])
    return {"original": note["original"] if original is not None else "", "keys": note["keys"],
            "volume": outer["props"]["AudioVolume"], "outer": outer}


def seek(controller, timecode):
    """Moves Resolve's playhead. ~500 ms, holding the GIL: run it from
    resolve_child.py, never in Buddy itself."""
    if not get_timeline(controller).SetCurrentTimecode(timecode):
        raise ResolveConnectionError("Resolve didn't move the playhead - is the timeline playing?")
