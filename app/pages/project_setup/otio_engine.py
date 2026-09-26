#!/usr/bin/env python3
"""
Timeline restructuring done as an offline file transform instead of through
the timeline API.

Resolve can export a timeline to OTIO (a plain JSON interchange format) and
import one back as a new timeline. Both are single calls. That makes it
possible to do the whole of Expand - read the timeline, work out a new
layout, build it - as local JSON manipulation, handing Resolve two calls
instead of thousands.

Why this exists at all: the API route asks Resolve to perform every step
itself. Expanding a 49-clip timeline meant ~1,800 property reads, up to 336
AddTrack calls, a 336-entry append and a 336-item delete - and on real
footage that reproducibly CRASHED Resolve (two crashes at the same fault
address, within two minutes of launch each time), quite apart from taking
long enough to look hung. Measured against the same real timeline, the
transform below completes in 0.87s end to end, with every clip's name,
timeline position, duration and source in/out preserved exactly and every
item bound to its existing Media Pool clip.

Nothing here deletes anything. The source timeline is exported and left
alone; the result arrives as a NEW timeline. The elaborate
delete-only-after-the-replacement-is-confirmed machinery in align_engine.py
exists because moving a clip through the API is "append a copy, then delete
the original" and a failure between those two steps loses footage. That
whole class of risk simply does not arise here - if anything fails, the
original timeline is still sitting there untouched.

OTIO structure, as Resolve writes it:

    timeline["tracks"]["children"]      -> one Track per timeline track
    track["kind"]                       -> "Video" or "Audio"
    track["children"]                   -> Clips and Gaps, in playback order
    child["source_range"]["duration"]   -> {"value": frames, "rate": fps}

A clip's position on the timeline is not stored; it is the sum of the
durations of everything before it on that track. Placing a clip somewhere
means writing a Gap of that length in front of it, which is why building a
layout here is just arithmetic.
"""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
import tempfile

# Resolve records which video and audio items belong to the same clip in the
# metadata it writes, so grouping needs no guessing here - align_engine's
# _match_key had to infer it from (source clip, position) because the
# timeline API exposes no reliable equivalent (GetLinkedItems only reflects
# links Resolve itself made).
_RESOLVE_META = "Resolve_OTIO"
_LINK_KEY = "Link Group ID"


class OtioError(RuntimeError):
    """Raised when the export/transform/import round trip can't be completed
    at all - a per-clip problem is collected as a warning instead."""


# ---------- document helpers (pure, no Resolve) ----------

def _rational(value, rate):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": value}


def _make_gap(duration, rate):
    return {
        "OTIO_SCHEMA": "Gap.1", "metadata": {}, "name": "",
        "source_range": {
            "OTIO_SCHEMA": "TimeRange.1",
            "duration": _rational(duration, rate),
            "start_time": _rational(0.0, rate),
        },
        "effects": [], "markers": [], "enabled": True,
    }


def _make_track(kind, name, children):
    return {
        "OTIO_SCHEMA": "Track.1", "metadata": {}, "name": name,
        "source_range": None, "effects": [], "markers": [],
        "enabled": True, "children": children, "kind": kind,
    }


def _is_clip(child):
    return child.get("OTIO_SCHEMA", "").startswith("Clip")


def _is_gap(child):
    return child.get("OTIO_SCHEMA", "").startswith("Gap")


def _is_transition(child):
    return child.get("OTIO_SCHEMA", "").startswith("Transition")


def _duration(child):
    return child["source_range"]["duration"]["value"]


def _rate(child):
    return child["source_range"]["duration"]["rate"]


def _item_duration(child):
    """How much track time one track child takes up, in frames.

    A Clip or Gap carries its own source_range. A nested Stack or Track (a
    compound clip) may not - OTIO then derives its length from what it
    holds: a Track plays its children one after another, a Stack plays its
    tracks on top of each other. A Transition takes no time at all (see
    read_entries), so it counts for nothing inside a nested track either."""
    if _is_transition(child):
        return 0.0
    source = child.get("source_range")
    if source and source.get("duration"):
        return source["duration"]["value"]
    children = child.get("children")
    schema = child.get("OTIO_SCHEMA", "")
    if children is not None and schema.startswith("Track"):
        return sum(_item_duration(c) for c in children)
    if children is not None and schema.startswith("Stack"):
        return max((_item_duration(c) for c in children), default=0.0)
    raise OtioError(
        f'Could not work out how long "{child.get("name", "")}" '
        f"({schema or 'unknown item'}) is on the timeline, so the positions of "
        "everything after it cannot be read. The timeline itself has not been "
        "changed."
    )


def unsupported_items(document):
    """(kind of item, name) for every track child that is neither a Clip, a
    Gap nor a Transition - in practice a compound clip or nested timeline,
    which Resolve exports as a Stack. build_document only knows how to lay
    out clips, so a rebuild would leave these out of the new timeline."""
    found = []
    for track in document["tracks"]["children"]:
        for child in track["children"]:
            if not (_is_clip(child) or _is_gap(child) or _is_transition(child)):
                found.append((child.get("OTIO_SCHEMA", "unknown").split(".")[0],
                              child.get("name", "") or "(unnamed)"))
    return found


def refuse_unsupported(document):
    """Raises OtioError if rebuilding `document` would drop anything.

    Refused rather than warned about: the result is a new timeline and the
    original stays put, but a new timeline quietly missing a compound clip
    is exactly the kind of loss nobody notices until the edit is delivered."""
    found = unsupported_items(document)
    if not found:
        return
    shown = ", ".join(f'"{name}" ({kind})' for kind, name in found[:3])
    more = f" and {len(found) - 3} more" if len(found) > 3 else ""
    raise OtioError(
        f"This timeline holds {len(found)} item(s) that can't be rebuilt – "
        f"compound clips or nested timelines: {shown}{more}.\n\nRebuilding it "
        "would leave them out, so nothing was changed. Decompose them in place "
        "(right-click > Decompose in Place) and run this again."
    )


def rebuild_warnings(document):
    """What a rebuild of `document` loses without refusing over it.

    Transitions only. A clip is laid out by its own cut points, and a
    transition merely borrows handle frames either side of one, so every
    clip still plays exactly where and for as long as it did - what goes is
    the dissolve itself, which is quick to put back and no reason to refuse
    the whole run."""
    count = sum(1 for track in document["tracks"]["children"]
                for child in track["children"] if _is_transition(child))
    if not count:
        return []
    return [f"{count} transition(s) (cross dissolves and the like) are not carried "
            "into the new timeline – every clip keeps its cut points, but the "
            "transitions need adding again."]


def read_entries(document):
    """Every clip in the document, with the kind of track it sits on, the
    frame it starts at, and its link group. Position is accumulated from the
    durations before it, since OTIO stores no absolute position.

    Transitions are skipped outright: in OTIO a transition sits between two
    items and overlaps them by its in/out offsets rather than occupying
    track time of its own, so it has no source_range and must not move
    anything after it. Anything else that is not a clip - a nested compound
    clip - still takes up its length, so later clips keep their positions,
    but is not returned (see unsupported_items)."""
    entries = []
    numbering = {"Video": 0, "Audio": 0}
    for track in document["tracks"]["children"]:
        kind = track.get("kind")
        numbering[kind] = numbering.get(kind, 0) + 1
        number = numbering[kind]
        position = 0.0
        for child in track["children"]:
            if _is_transition(child):
                continue
            if _is_clip(child):
                entries.append({
                    "clip": child,
                    "kind": kind,
                    "track": number,
                    "start": position,
                    "rate": _rate(child),
                    "link": (child.get("metadata", {})
                             .get(_RESOLVE_META, {}).get(_LINK_KEY)),
                    "name": child.get("name", ""),
                })
            position += _item_duration(child)
    return entries


def group_entries(entries):
    """Clips that belong together as one logical clip - a video item and the
    audio item(s) Resolve linked to it - grouped by link group, with
    unlinked clips standing alone. Groups come back in timeline order."""
    linked, loose = {}, []
    for entry in entries:
        if entry["link"] is not None:
            linked.setdefault(entry["link"], []).append(entry)
        else:
            loose.append(entry)
    groups = [linked[key] for key in sorted(linked)] + [[e] for e in loose]
    groups.sort(key=lambda g: min(e["start"] for e in g))
    return groups


def build_document(document, placements):
    """A copy of `document` whose tracks are rebuilt from `placements` - a
    list of (entry, kind, track_number, start_frame). Track numbers are
    1-based within their kind; any left empty is still created, so the
    numbering the caller chose is what comes out.

    A track may carry ANY number of clips, laid out in start order with a
    Gap of the right length in front of each. Expand happens to put one clip
    on each track, but Collapse deliberately puts many, and an earlier
    version of this that stored a single clip per track number silently kept
    only the last one written to each - turning 98 clips into 2.

    Only clips are written back, so a document holding anything else a
    rebuild would drop is refused here, whichever operation got this far
    (see refuse_unsupported). Transitions are dropped - see
    rebuild_warnings for why that one is only a warning."""
    refuse_unsupported(document)
    by_track = {"Video": {}, "Audio": {}}
    for entry, kind, track_number, start in placements:
        by_track[kind].setdefault(track_number, []).append((start, entry))

    tracks = []
    for kind in ("Video", "Audio"):
        highest = max(by_track[kind]) if by_track[kind] else 0
        for number in range(1, highest + 1):
            children = []
            position = 0.0
            for start, entry in sorted(by_track[kind].get(number, []),
                                       key=lambda pair: pair[0]):
                if start < position:
                    raise OtioError(
                        f"Internal error: two clips were assigned overlapping "
                        f"space on {kind} {number} (at frame {start:.0f}, which "
                        f"is inside a clip running to {position:.0f})."
                    )
                if start > position:
                    children.append(_make_gap(start - position, entry["rate"]))
                children.append(copy.deepcopy(entry["clip"]))
                position = start + _duration(entry["clip"])
            tracks.append(_make_track(kind, f"{kind} {number}", children))

    out = copy.deepcopy(document)
    out["tracks"]["children"] = tracks
    return out


def expand_document(document):
    """One clip per track, every clip keeping its exact position and trim.

    Track numbers are handed out per kind, in timeline order, with no
    attempt to force a clip's picture and sound onto the SAME number. In the
    ordinary case - every clip a video/audio pair - they line up anyway,
    because each group contributes one of each. Forcing the two cursors to
    stay level was tried and dropped: an unbalanced group (a clip with no
    audio, a standalone recorder file) then had to skip a number on the
    other side, leaving an empty track behind for a correspondence that is
    meaningless for that clip in the first place."""
    groups = group_entries(read_entries(document))
    placements = []
    next_track = {"Video": 1, "Audio": 1}
    for group in groups:
        # Video first so a paired clip's two halves take the same number for
        # as long as the two cursors happen to agree.
        for entry in sorted(group, key=lambda e: e["kind"] != "Video"):
            kind = entry["kind"]
            placements.append((entry, kind, next_track[kind], entry["start"]))
            next_track[kind] += 1
    return build_document(document, placements), len(groups), len(placements)


# ---------- Resolve side ----------

def _timeline_names(project):
    return {project.GetTimelineByIndex(i).GetName()
            for i in range(1, project.GetTimelineCount() + 1)}


def unique_timeline_name(project, base):
    """`base`, or "base (2)", "base (3)"... if that name is taken. Resolve
    allows duplicate timeline names, which makes the result impossible to
    identify afterwards - both from the UI and from the API."""
    existing = _timeline_names(project)
    if base not in existing:
        return base
    n = 2
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def pack_entries(entries):
    """Assigns each clip the lowest-numbered track of its own kind that is
    free by the time it starts - the classic "fewest machines to run these
    intervals" assignment. Returns placements for build_document.

    Packing is per CLIP here, not per link group. Doing it through the
    timeline API had to keep a clip's audio items together on consecutive
    tracks, because an audio placement is addressed by Media Pool clip and
    Resolve lays that source's whole audio track set out from the track it
    is given - so the tracks a clip's audio landed on were Resolve's choice,
    not the caller's. Writing the document directly removes that
    constraint entirely: every clip is placed on exactly the track it is
    written onto, so two clips that do not overlap in time can share a
    track no matter which sources they came from."""
    placements = []
    ends = {"Video": [], "Audio": []}
    for entry in sorted(entries, key=lambda e: e["start"]):
        kind = entry["kind"]
        finish = entry["start"] + _duration(entry["clip"])
        track_ends = ends[kind]
        target = next((i for i, busy_until in enumerate(track_ends)
                       if busy_until <= entry["start"]), None)
        if target is None:
            track_ends.append(finish)
            target = len(track_ends) - 1
        else:
            track_ends[target] = finish
        placements.append((entry, kind, target + 1, entry["start"]))
    return placements


def collapse_document(document, drop=()):
    """Packs everything onto the fewest tracks that fit without overlap,
    leaving every clip at exactly the frame it already plays at - Collapse
    changes which track a clip is on, never when it plays.

    Clips in `drop` (identified by id()) are left out of the result
    altogether, which is how the silent-audio option removes them: there is
    no delete, the clip simply is not written into the new timeline.

    Empty tracks need no removing either. The timeline-API version had to
    delete them one at a time afterwards, measured at ~239ms each - about
    twenty seconds of pure track deletion on a 49-track expanded timeline,
    every one of them a mutation of the kind that was destabilising Resolve.
    Here the track count is simply however many the packing needed."""
    entries = [e for e in read_entries(document) if id(e["clip"]) not in drop]
    placements = pack_entries(entries)
    tracks = {"Video": 0, "Audio": 0}
    for _entry, kind, number, _start in placements:
        tracks[kind] = max(tracks[kind], number)
    return build_document(document, placements), len(entries), tracks


def find_silent_entries(entries, ffmpeg_path, warnings, progress_cb=None):
    """ids of the audio clips whose own track carries nothing audible over
    the stretch they actually play. Returns a set of id(clip).

    Judged per track: a clip is one of its source's audio tracks, so the
    file's real channel layout decides which channels to listen to (see
    ffmpeg_utils.channels_for_track). How many tracks the source is showing
    as is taken from how many audio clips share its link group. Anything
    that cannot be checked - no path, an unmappable layout, an ffmpeg
    failure - is KEPT: "couldn't tell" is not "confirmed silent"."""
    from . import audio_sync
    from . import ffmpeg_utils

    audio = [e for e in entries if e["kind"] == "Audio"]
    # Siblings are the audio items of ONE source inside one link group -
    # those are the tracks that source is showing as. An unlinked clip has
    # no siblings: grouping on a link of None once lumped every loose
    # recording on the timeline together, so each was judged on a slice of
    # its channels and recordings with sound in them were dropped as
    # silent. Keyed on the file as well, because a link group can join a
    # camera clip to audio from elsewhere, and that recorder's items are not
    # tracks of the camera file.
    per_group = {}
    for entry in audio:
        if entry["link"] is not None:
            key = (entry["link"], _media_path(entry["clip"]))
            per_group.setdefault(key, []).append(entry)

    layouts = {}
    silent = set()
    unplaced = []
    for index, entry in enumerate(audio):
        if progress_cb:
            progress_cb("Checking for silent audio", index, len(audio))

        path = _media_path(entry["clip"])
        if not path or not os.path.isfile(path):
            continue
        if not _media_of(entry["clip"]).get("available_range"):
            # Without it there is no telling where in the file the clip's
            # used portion begins (see source_offset_seconds), and a window
            # read from the wrong place - past the end of the file, say -
            # comes back as no audio, which is_silent reports as silent.
            unplaced.append(entry["name"])
            continue

        if path not in layouts:
            try:
                layouts[path] = ffmpeg_utils.audio_channel_layout(ffmpeg_path, path)
            except Exception:
                layouts[path] = []

        siblings = (per_group.get((entry["link"], path), [entry])
                    if entry["link"] is not None else [entry])
        track_count = len(siblings)
        track_index = siblings.index(entry)
        channels = ffmpeg_utils.channels_for_track(layouts[path], track_count, track_index)
        if channels is None:
            continue

        source = entry["clip"]["source_range"]
        rate = source["duration"]["rate"] or 1.0
        try:
            if audio_sync.is_silent(
                ffmpeg_path, path,
                # NOT source_range.start_time directly: that is a position in
                # the media's own timecode, so a clip whose media starts at
                # 14:00:00:00 reports ~50,400 seconds and ffmpeg was being
                # asked to seek fourteen hours into a forty-second file.
                start_seconds=source_offset_seconds(entry["clip"]),
                duration_seconds=source["duration"]["value"] / rate,
                channels=channels,
            ):
                silent.add(id(entry["clip"]))
        except Exception as exc:
            warnings.append(f'"{entry["name"]}" could not be checked for silence: {exc}')
    if unplaced:
        shown = ", ".join(f'"{name}"' for name in unplaced[:3])
        more = f" (and {len(unplaced) - 3} more)" if len(unplaced) > 3 else ""
        warnings.append(
            f"{len(unplaced)} audio clip(s) were kept without being checked for "
            f"silence – Resolve gave no range for their media, so there is no "
            f"telling which part of the file they play: {shown}{more}")
    return silent


def collapse_document_from(document, delete_silent_audio=False, ffmpeg_path=None,
                           progress_cb=None):
    """The offline half of Collapse: decide what to drop, pack the tracks.
    Returns (result_document, kept, dropped, warnings).

    Split out so it can run on a worker thread - checking for silent audio
    is one ffmpeg decode per audio clip, which on a real timeline is minutes
    of media reading and has no business on the thread Qt paints from. No
    Resolve call happens in here."""
    # Refused before the silence check rather than after it: that is minutes
    # of decoding, and build_document would only refuse at the end.
    refuse_unsupported(document)
    warnings = rebuild_warnings(document)
    entries = read_entries(document)
    if not entries:
        raise OtioError("The current timeline has no clips on it.")

    drop = set()
    if delete_silent_audio:
        if not ffmpeg_path:
            warnings.append("Could not check for silent audio clips – ffmpeg not found.")
        else:
            drop = find_silent_entries(entries, ffmpeg_path, warnings, progress_cb)

    if progress_cb:
        progress_cb("Packing tracks", 0, 1)
    result, kept, _tracks = collapse_document(document, drop)
    return result, kept, len(drop), warnings


def import_rebuilt(controller, result, workdir, base_name, suffix, filename):
    """Writes a rebuilt document out and imports it as a new timeline.
    MAIN THREAD ONLY - this is the half that talks to Resolve."""
    result_path = os.path.join(workdir, filename)
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)

    name = unique_timeline_name(controller.get_project(), f"{base_name} {suffix}")
    new_timeline = controller.import_timeline_file(result_path, name)
    if new_timeline is None:
        raise OtioError(
            "Resolve refused to import the rebuilt timeline. The original "
            f"timeline is untouched; the rebuilt file is at {result_path}"
        )
    return new_timeline


def expand_timeline(controller, progress_cb=None):
    """Exports the current timeline, rebuilds it one-clip-per-track offline,
    and imports the result as a new timeline. The current timeline is not
    modified. Returns (new_timeline, stats, warnings)."""
    def report(stage, done=0, total=1):
        if progress_cb:
            progress_cb(stage, done, total)

    project = controller.get_project()
    timeline = controller.get_current_timeline()
    warnings = []

    workdir = tempfile.mkdtemp(prefix="project_setup_otio_")
    source_path = os.path.join(workdir, "source.otio")
    result_path = os.path.join(workdir, "expanded.otio")

    report("Exporting timeline")
    if not controller.export_timeline_otio(timeline, source_path):
        raise OtioError(
            "Resolve could not export this timeline, so there is nothing to "
            "rebuild from. The timeline itself has not been changed."
        )

    report("Rebuilding layout")
    with open(source_path, encoding="utf-8") as handle:
        document = json.load(handle)

    entries = read_entries(document)
    if not entries:
        raise OtioError("The current timeline has no clips on it.")

    warnings.extend(rebuild_warnings(document))
    result, group_count, clip_count = expand_document(document)
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)

    report("Importing new timeline")
    name = unique_timeline_name(project, f"{timeline.GetName()} (Expanded)")
    new_timeline = controller.import_timeline_file(result_path, name)
    if new_timeline is None:
        raise OtioError(
            "Resolve refused to import the rebuilt timeline. The original "
            f"timeline is untouched; the rebuilt file is at {result_path}"
        )

    stats = {
        "clips": group_count,
        "items": clip_count,
        "video_tracks": new_timeline.GetTrackCount("video"),
        "audio_tracks": new_timeline.GetTrackCount("audio"),
        "name": new_timeline.GetName(),
    }
    return new_timeline, stats, warnings


# ---------- Align ----------

class LogicalClip:
    """One clip as the Align tab thinks of it - a video item plus the audio
    item(s) Resolve linked to it, or a standalone recording - carrying just
    enough to feed align_engine's offset calculations unchanged.

    Deliberately shaped like align_engine.TimelineClip (clip_id, name, path,
    has_audio, source_fps, record_frame) so locate_offsets_waveform and the
    rest can be reused as they are; those are pure calculations over clip
    metadata and have nothing to do with how the timeline gets rebuilt."""

    def __init__(self, clip_id, name, path, entries, source_fps, start_tc_seconds):
        self.clip_id = clip_id
        self.name = name
        self.path = path
        self.entries = entries
        self.source_fps = source_fps
        self.start_tc_seconds = start_tc_seconds
        self.record_frame = min(e["start"] for e in entries)
        self.has_video = any(e["kind"] == "Video" for e in entries)
        self.has_audio = any(e["kind"] == "Audio" for e in entries)


def _media_of(clip):
    key = clip.get("active_media_reference_key", "DEFAULT_MEDIA")
    return clip.get("media_references", {}).get(key, {}) or {}


def _media_path(clip):
    return (_media_of(clip).get("target_url") or "").replace("file://", "")


def logical_clips(document):
    """Every logical clip in the document, in timeline order."""
    clips = []
    for index, group in enumerate(group_entries(read_entries(document))):
        # Prefer the audio item's own file for waveform matching - for a
        # camera clip both halves name the same file anyway, but a clip whose
        # audio came from elsewhere would otherwise be matched on the wrong
        # media.
        audio = [e for e in group if e["kind"] == "Audio"]
        primary = (audio or group)[0]["clip"]
        # available_range can be missing altogether (offline media, a
        # generator), and with it the media's start: that is no timecode,
        # not a crash.
        available = _media_of(primary).get("available_range") or {}
        start = available.get("start_time") or {}
        rate = start.get("rate") or primary["source_range"]["duration"]["rate"]
        start_value = start.get("value")
        clips.append(LogicalClip(
            clip_id=f"{index}:{group[0]['clip'].get('name', '')}",
            name=group[0]["clip"].get("name", "") or "(unnamed)",
            path=_media_path(primary),
            entries=group,
            source_fps=rate,
            # Embedded timecode: OTIO gives the media's start as a frame
            # count at its own rate, which is exactly what "Start TC" is,
            # already parsed - no HH:MM:SS:FF string to pick apart.
            start_tc_seconds=(start_value / rate
                              if rate and start_value is not None else None),
        ))
    return clips


def _device_of(clip):
    """Which camera or recorder a clip came from.

    The containing folder is the sturdiest signal available without reading
    metadata - cameras write their own card structure - with the filename
    prefix as a fallback for media that has been flattened into one folder."""
    import re
    path = clip.path or ""
    folder = os.path.basename(os.path.dirname(path))
    if folder and folder.lower() not in ("", "media", "footage", "clips", "audio"):
        return folder
    name = os.path.basename(path)
    match = re.match(r"([A-Za-z]+\d*|[0-9]+)", name)
    return match.group(1) if match else (name or "unknown")


def find_timecode_collisions(clips):
    """Clips from one device whose timecode ranges overlap each other.

    Impossible in reality, so it means the timecode is not unique across the
    footage - almost always more than one day, since timecode carries no
    date and repeats every 24 hours."""
    by_device = {}
    for clip in clips:
        if not has_usable_timecode(clip):
            continue
        entry = clip.entries[0]
        rate = entry["rate"] or 1.0
        start = clip.start_tc_seconds
        by_device.setdefault(_device_of(clip), []).append(
            (start, start + _duration(entry["clip"]) / rate, clip))

    clashes = []
    for device, spans in by_device.items():
        spans.sort(key=lambda s: (s[0], s[1]))
        for (_s1, end1, first), (start2, _e2, second) in zip(spans, spans[1:]):
            if start2 < end1 - 0.5:
                clashes.append((device, first, second))
    return clashes


def compute_offsets_timecode(clips):
    """Seconds-offsets from embedded timecode, relative to the earliest.

    A start timecode of exactly 00:00:00:00 counts as NO timecode. On camera
    originals that is what a camera writes when nothing set it - phones,
    consumer cameras, and every audio recorder without a timecode input -
    and treating it as a real position stacks all of those clips on one
    frame while reporting a successful sync. Leaving them where they are and
    naming them is the same thing every other method here does with a clip
    it cannot place.

    A genuine 00:00:00:00 start does exist, but it is rare, and the cost of
    the two mistakes is not symmetric: a clip wrongly left alone is visible
    and easily dragged, while a clip wrongly stacked is a silent desync in a
    pile of others."""
    parsed = {}
    warnings = []
    unset = []
    for clip in clips:
        if clip.start_tc_seconds is None:
            warnings.append(f'"{clip.name}" has no usable embedded timecode – left unsynced.')
            continue
        if abs(clip.start_tc_seconds) < 0.001:
            unset.append(clip.name)
            continue
        parsed[clip.clip_id] = clip.start_tc_seconds

    if unset:
        shown = ", ".join(unset[:3])
        more = f" (and {len(unset) - 3} more)" if len(unset) > 3 else ""
        warnings.append(
            f"{len(unset)} clip(s) start at 00:00:00:00, which means no timecode was "
            f"ever set – left where they were: {shown}{more}. Audio recorders and "
            "phones do this; use Waveform, or Sync external audio for loose audio.")

    if not parsed:
        raise OtioError(
            "None of the clips have usable embedded timecode to sync from."
            "\n\nEvery clip reads 00:00:00:00, which is what a camera writes when "
            "nothing set the timecode – common on phones, consumer cameras and audio "
            "recorders.\n\nUse Waveform instead if the clips were rolling at the same "
            "time, or File Date to lay them out in the order they were shot."
        )

    # Timecode that is present but identical everywhere carries no more
    # information than none at all, and would silently stack the lot.
    distinct = len({round(value, 3) for value in parsed.values()})
    if len(parsed) >= 5 and distinct <= 2:
        raise OtioError(
            f"All {len(parsed)} clips report the same embedded timecode, so there is "
            "nothing to sync from – aligning on it would stack them on one frame."
            "\n\nUse Waveform if the clips were rolling at the same time, or File "
            "Date to lay them out in the order they were shot."
        )

    clashes = find_timecode_collisions(clips)
    if clashes:
        devices = sorted({d for d, _a, _b in clashes})
        shown = ", ".join(devices[:3])
        warnings.append(
            f"WARNING: {len(clashes)} pair(s) of clips from the same camera claim "
            f"overlapping timecode ({shown}). One camera cannot record two things "
            "at once, so this is almost certainly more than one day of footage – "
            "timecode has no date in it and repeats every 24 hours. Day two will "
            "land on top of day one. Sync one day at a time.")

    earliest = min(parsed.values())
    return {cid: t - earliest for cid, t in parsed.items()}, warnings


def align_document(document, frames_by_id, clips):
    """Moves each clip whose id appears in frames_by_id to that frame,
    keeping every clip on the track it is already on where that is free.

    A clip only changes track if its new position would overlap something
    else already on that one - then it takes the next free track of its kind
    rather than being refused. On an expanded timeline (one clip per track,
    which is what Align is for) nothing collides and every clip stays
    exactly where it was, track-wise."""
    placements = []
    occupied = {"Video": {}, "Audio": {}}
    moved = 0

    def fits(kind, track, start, end):
        return all(end <= s or start >= e for s, e in occupied[kind].get(track, ()))

    def claim(kind, track, start, end):
        occupied[kind].setdefault(track, []).append((start, end))

    # Clips that actually move are placed first, so they get their own track
    # back before a stationary clip can claim the space.
    ordered = sorted(clips, key=lambda c: c.clip_id not in frames_by_id)
    for clip in ordered:
        target = frames_by_id.get(clip.clip_id)
        shift = 0.0 if target is None else target - clip.record_frame
        if target is not None:
            moved += 1
        for entry in clip.entries:
            kind = entry["kind"]
            start = entry["start"] + shift
            end = start + _duration(entry["clip"])
            track = entry["track"]
            if not fits(kind, track, start, end):
                track = 1
                while not fits(kind, track, start, end):
                    track += 1
            claim(kind, track, start, end)
            placements.append((entry, kind, track, start))
    return build_document(document, placements), moved


def read_current_timeline(controller):
    """Exports the current timeline and returns (document, clips, workdir)
    without changing anything - what the Align tab needs both to fill its
    reference-clip list and to compute offsets."""
    timeline = controller.get_current_timeline()
    workdir = tempfile.mkdtemp(prefix="project_setup_otio_")
    path = os.path.join(workdir, "source.otio")
    if not controller.export_timeline_otio(timeline, path):
        raise OtioError("Resolve could not export the current timeline.")
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    return document, logical_clips(document), workdir


def apply_alignment(controller, document, clips, frames_by_id, workdir,
                    progress_cb=None):
    """Rebuilds the document with the computed positions and imports it as a
    new timeline. Returns (new_timeline, stats)."""
    if progress_cb:
        progress_cb("Rebuilding layout", 0, 1)
    result, moved = align_document(document, frames_by_id, clips)
    result_path = os.path.join(workdir, "aligned.otio")
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)

    if progress_cb:
        progress_cb("Importing new timeline", 0, 1)
    project = controller.get_project()
    timeline = controller.get_current_timeline()
    name = unique_timeline_name(project, f"{timeline.GetName()} (Aligned)")
    new_timeline = controller.import_timeline_file(result_path, name)
    if new_timeline is None:
        raise OtioError(
            "Resolve refused to import the aligned timeline. The original "
            f"timeline is untouched; the rebuilt file is at {result_path}"
        )
    return new_timeline, {"moved": moved, "total": len(clips),
                          "name": new_timeline.GetName(),
                          "warnings": rebuild_warnings(document)}


# ---------- Waveform refine against a reference track ----------

def _seconds(rational):
    """A RationalTime as seconds. Each value MUST be divided by its OWN rate:
    Resolve writes source_range at the timeline rate and available_range at
    the media rate, so the same instant appears as 96460.36 @24 in one and
    96364.00 @23.976 in the other. Dividing both by one rate makes an
    untrimmed clip look like it starts 4 seconds in."""
    if not rational:
        return 0.0
    rate = rational.get("rate") or 1.0
    return rational.get("value", 0.0) / rate


def source_offset_seconds(clip):
    """How far into its own media file a clip's used portion begins."""
    available = _media_of(clip).get("available_range") or {}
    offset = (_seconds(clip["source_range"].get("start_time"))
              - _seconds(available.get("start_time")))
    # Two large frame counts converted at slightly different rates and
    # subtracted leave a floating-point residue - an untrimmed clip comes out
    # as 1.1e-13 rather than 0. Nothing below a millisecond is a real trim.
    return offset if abs(offset) >= 0.001 else 0.0


# ---------- Shift ----------

def merged_occupancy(entries):
    """The stretches of the timeline where SOMETHING is playing, merged
    across every track, as a sorted list of (start, end) in frames.

    Across every track, not per track, and that is the whole point. Closing
    gaps one track at a time would slide each track's clips left by a
    different amount and pull the timeline out of sync - undoing exactly
    what Align and the refine pass just established. A stretch is only dead
    air if nothing anywhere is playing through it."""
    spans = sorted((e["start"], e["start"] + _duration(e["clip"])) for e in entries)
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def shift_document(document, close_gaps=False):
    """Moves everything left so the first clip starts at frame 0.

    With close_gaps, the dead stretches BETWEEN blocks of footage are
    removed too, and everything after each gap slides left to meet what
    came before - a tidy assembly with no empty space. Relative timing
    inside a block is untouched, so clips that were in sync stay in sync;
    what changes is only the empty time between one block and the next.

    Without it, every clip keeps its exact spacing and the whole timeline
    simply slides left to start at zero.

    Returns (new_document, stats)."""
    entries = read_entries(document)
    clips = logical_clips(document)
    if not entries:
        raise OtioError("The current timeline has no clips on it.")

    blocks = merged_occupancy(entries)
    lead_in = blocks[0][0]
    playing = sum(end - start for start, end in blocks)

    # Where each block of footage begins once the timeline is rebuilt.
    bases = []
    running = 0.0
    for start, end in blocks:
        bases.append(running if close_gaps else start - lead_in)
        running += end - start

    def remap(position):
        for index, (start, end) in enumerate(blocks):
            if start <= position <= end:
                return position - start + bases[index]
        return position - lead_in

    frames_by_id = {clip.clip_id: remap(clip.record_frame) for clip in clips}
    result, _moved = align_document(document, frames_by_id, clips)

    span = blocks[-1][1] - blocks[0][0]
    stats = {
        "clips": len(clips),
        "lead_in_removed": int(round(lead_in)),
        "gaps": len(blocks) - 1,
        "gap_frames_removed": int(round(span - playing)) if close_gaps else 0,
    }
    return result, stats


def shift_timeline(controller, close_gaps=False, progress_cb=None):
    """Exports the current timeline, shifts it to the start (optionally
    closing gaps), and imports the result as a new timeline."""
    def report(stage, done=0, total=1):
        if progress_cb:
            progress_cb(stage, done, total)

    project = controller.get_project()
    timeline = controller.get_current_timeline()

    report("Exporting timeline")
    document, _clips, workdir = read_current_timeline(controller)

    report("Shifting clips")
    result, stats = shift_document(document, close_gaps=close_gaps)

    result_path = os.path.join(workdir, "shifted.otio")
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)

    report("Importing new timeline")
    name = unique_timeline_name(project, f"{timeline.GetName()} (Shifted)")
    new_timeline = controller.import_timeline_file(result_path, name)
    if new_timeline is None:
        raise OtioError(
            "Resolve refused to import the shifted timeline. The original "
            f"timeline is untouched; the rebuilt file is at {result_path}"
        )
    stats["name"] = new_timeline.GetName()
    stats["warnings"] = rebuild_warnings(document)
    return new_timeline, stats


# ---------- Sync External Audio ----------

# How many camera clips to decode at once when building the scratch
# reference. Four, not the eight that benchmarked marginally faster: these
# are real ffmpeg processes reading the same network drive Resolve is also
# reading from, and making playback stutter to save twenty seconds is a bad
# trade. Past eight it got slower anyway - the drive, not the CPU, is the
# limit (measured: 0.87s per clip serial, 0.28s at four, 0.39s at sixteen).
SCRATCH_DECODE_WORKERS = 4


def has_usable_timecode(clip):
    """Whether this clip's start timecode says anything.

    Exactly 00:00:00:00 counts as nothing: that is what a camera writes when
    nothing set it, which is the normal state of phones, consumer cameras
    and audio recorders (see compute_offsets_timecode)."""
    return (clip.start_tc_seconds is not None
            and abs(clip.start_tc_seconds) >= 0.001)


def split_base_and_adrift(clips, force_improvise=False):
    """(base, adrift, improvised): which clips are already positioned, and
    which still need placing.

    base clips supply the scratch audio everything else is matched against,
    and are never moved. adrift clips are the ones to place - video or not,
    since carrying a picture has nothing to do with whether a clip is in the
    right place.

    improvised is True when nothing had timecode, so there was no base and
    the longest clip was declared the origin arbitrarily. Positions are then
    relative to that clip rather than to the timeline, which is the one case
    where a clip may legitimately belong before the origin."""
    audible = [c for c in clips if c.has_audio and c.path]
    if not audible:
        return [], [], False

    with_tc = [] if force_improvise else [c for c in audible
                                          if has_usable_timecode(c)]
    if with_tc:
        return with_tc, [c for c in audible if not has_usable_timecode(c)], False

    origin = max(audible, key=lambda c: _duration(c.entries[0]["clip"])
                 / (c.entries[0]["rate"] or 1.0))
    return [origin], [c for c in audible if c is not origin], True


def split_sessions(camera_clips, max_gap):
    """Camera clips grouped into stretches of timeline separated by silence
    wider than max_gap, earliest first.

    Splitting is what makes several days of footage workable at all. One
    reference over everything has to be allocated across the whole span, which
    on a four-day project came to 24.03 hours - past the refusal below, so the
    pass never ran at all - and forced the sample rate down to its floor to fit
    the memory budget. The same project splits into six sessions, the largest
    2.82 hours, which fits at full rate in 40 MB instead of 350 MB.

    It also sharpens the answers. A standout is measured against the rest of
    the search, so folding in three days the recording has nothing to do with
    raises the field it is divided by: one recording here scored 3.43 against
    the whole timeline and 7.58 against its own day, for the same position.

    max_gap is the longest loose recording, which makes the split lossless: a
    recording shorter than the gap cannot reach footage on both sides of it, so
    no possible match is being cut in half."""
    spans = []
    for clip in camera_clips:
        entry = next((e for e in clip.entries if e["kind"] == "Audio"), None)
        if entry is None:
            continue
        rate = entry["rate"] or 1.0
        start = entry["start"] / rate
        spans.append((start, start + _duration(entry["clip"]) / rate, clip))

    sessions = []
    for start, end, clip in sorted(spans, key=lambda s: s[0]):
        if sessions and start <= sessions[-1][1] + max_gap:
            sessions[-1][1] = max(sessions[-1][1], end)
            sessions[-1][2].append(clip)
        else:
            sessions.append([start, end, [clip]])
    return [(start, end, clips) for start, end, clips in sessions]


def build_scratch_reference(camera_clips, ffmpeg_path, rate, warnings,
                            progress_cb=None, stage="Reading camera audio"):
    """One signal covering this session, holding every camera clip's own audio
    at the position that clip plays at. Silence everywhere else.

    Returns (reference, placed, origin, envelopes), where envelopes is a list
    of (clip, start seconds, onset envelope) for every clip that contributed -
    kept because the reference sums them beyond recovery, and corroborating a
    match means asking each clip on its own afterwards (see corroboration()).
    An envelope costs a twentieth of the audio it came from, so holding them
    alongside the reference is not the part that matters.

    This is what external recordings are matched against, rather than being
    compared with camera clips one at a time. Three reasons it is better:
    a half-hour lav overlaps dozens of short takes, so the correlation has
    far more evidence than any single pairing could give; the answer comes
    back already expressed as a timeline position; and it costs one
    correlation per recording instead of one per recording per camera clip
    (127 clips and 11 recordings would otherwise be 1,397 of them)."""
    from . import audio_sync
    import numpy as np

    spans = []
    for clip in camera_clips:
        entry = next((e for e in clip.entries if e["kind"] == "Audio"), None)
        if entry is None:
            continue
        clip_rate = entry["rate"] or 1.0
        start = entry["start"] / clip_rate
        spans.append((start, start + _duration(entry["clip"]) / clip_rate, clip, entry))
    if not spans:
        raise OtioError("None of the clips with video have audio to match against.")

    # Anchored on the first clip rather than on frame zero: empty timeline
    # before the footage is not evidence, and allocating it is the entire
    # cost of parking a shoot away from the start.
    origin = min(start for start, _e, _c, _entry in spans)
    total_seconds = max(end for _s, end, _c, _e in spans) - origin
    # Refuse an implausible allocation rather than asking the OS for it. A
    # stray clip parked far down the timeline, or a rate read as something
    # odd, would otherwise turn into tens of gigabytes of commit with no
    # explanation - which is exactly how this went wrong once.
    #
    # Per session now rather than per timeline (see split_sessions), so it no
    # longer fires merely because a project holds several days: it takes one
    # unbroken stretch of footage longer than a day, which is not a thing.
    if total_seconds > 24 * 3600:
        raise OtioError(
            f"One unbroken stretch of footage spans {total_seconds/3600:.1f} hours, "
            "which is too far apart to match against itself.\n\nUsually this means "
            "one clip is parked a long way down the timeline – check for a stray "
            "clip at the far right before running this again."
        )
    reference = np.zeros(int(total_seconds * rate) + 1, dtype=np.float32)
    envelopes = []

    def decode(span):
        start, end, _clip, entry = span
        return audio_sync.load_window(
            ffmpeg_path, _media_path(entry["clip"]),
            source_offset_seconds(entry["clip"]), end - start, rate=rate,
        )

    def place(span, samples):
        start, _end, clip, _entry = span
        at = int((start - origin) * rate)
        room = min(len(samples), len(reference) - at)
        if room <= 0:
            return False
        reference[at:at + room] += samples[:room]
        envelope = audio_sync.onset_envelope(samples[:room], rate=rate)
        if len(envelope):
            envelopes.append((clip, start, envelope))
        return True

    placed = 0
    retry = []
    pool = ThreadPoolExecutor(max_workers=min(SCRATCH_DECODE_WORKERS, len(spans)))
    try:
        # Submitted all at once so they overlap; read back in order so the
        # sum is deterministic (see this module's Sync External Audio notes).
        pending = [pool.submit(decode, span) for span in spans]
        for index, (span, future) in enumerate(zip(spans, pending)):
            if progress_cb:
                progress_cb(stage, index, len(spans))
            try:
                samples = future.result()
            except audio_sync.NoAudioError:
                continue
            except Exception:
                # Not reported yet: camera originals run to tens of GB, and
                # four of them streaming at once off a network drive is the
                # likeliest reason a decode gave up. Worth one quiet attempt
                # on its own before saying the clip could not be read.
                retry.append(span)
                continue
            if place(span, samples):
                placed += 1
    finally:
        # cancel_futures so a cancelled run stops at the next clip rather
        # than quietly finishing all 127 decodes on the way out.
        pool.shutdown(wait=False, cancel_futures=True)

    for index, span in enumerate(retry):
        _start, _end, clip, _entry = span
        if progress_cb:
            progress_cb("Re-reading slow clips", index, len(retry))
        try:
            samples = decode(span)
        except audio_sync.NoAudioError:
            continue
        except Exception as exc:
            warnings.append(f'"{clip.name}" scratch audio could not be read: {exc}')
            continue
        if place(span, samples):
            placed += 1
    if retry:
        warnings.append(
            f"{len(retry)} clip(s) were too slow to read alongside the others and "
            "were re-read one at a time – usually large files on a busy drive.")

    if not placed:
        raise OtioError("Could not read usable audio from any clip with video.")
    peak = float(np.abs(reference).max())
    if peak > 0:
        reference /= peak
    return reference, placed, origin, envelopes


def corroboration(sample_env, duration, position, envelopes):
    """How many camera clips independently put this recording where the
    reference did. Returns (agreed, asked, names).

    Only clips that would be playing under the recording at this position are
    asked - a clip somewhere else has no opinion to give. Each is matched
    against the recording ON ITS OWN, so its answer owes nothing to the summed
    reference that produced the candidate, which is the whole point of asking.
    See audio_sync.MIN_CORROBORATION for what the two populations measured."""
    from . import audio_sync

    overlapping = [(clip, start, env) for clip, start, env in envelopes
                   if start + len(env) / audio_sync.ONSET_RATE > position
                   and start < position + duration]
    if not overlapping:
        return 0, 0, []

    answers = audio_sync.corroborating_positions(
        sample_env, [env for _clip, _start, env in overlapping])
    agreed = []
    for index, seconds, _standout in answers:
        if seconds is None:
            continue
        implied = overlapping[index][1] - seconds
        if abs(implied - position) <= audio_sync.CORROBORATION_TOLERANCE:
            agreed.append(overlapping[index][0].name)
    return len(agreed), len(overlapping), agreed


def sync_external_audio_document(document, ffmpeg_path, progress_cb=None,
                                 force_improvise=False):
    """Places every clip the metadata pass could not, by matching its audio
    against the scratch audio of the clips it could.

    Returns (new_document, stats, warnings). Anything without a convincing
    match is left exactly where it is and reported - which on sequentially
    shot footage is most of it, and rightly so: two takes recorded an hour
    apart share no audio and have no true offset to find.

    The footage is split into sessions first and each recording is tried
    against every one of them (see split_sessions), so a project holding
    several days works the same as a project holding one. A candidate is then
    only accepted if individual camera clips agree with it (see
    corroboration), because on a long timeline a peak can stand out at a
    position nothing supports."""
    from . import audio_sync

    # Before any decoding - build_document would refuse only at the end.
    refuse_unsupported(document)
    clips = logical_clips(document)
    base, adrift, improvised = split_base_and_adrift(clips, force_improvise)
    warnings = rebuild_warnings(document)

    if not adrift:
        raise OtioError(
            "Every clip with sound is already positioned by its timecode, so "
            "there is nothing left to place.\n\nThis syncs the clips timecode "
            "could not – lav packs, field recorders, and any camera that was not "
            "timecode-jammed."
        )
    if not base:
        raise OtioError(
            "There is no audio anywhere on this timeline to match against."
        )

    if improvised:
        warnings.append(
            f'No clip has usable timecode, so "{base[0].name}" – the longest '
            "recording here – was taken as the starting point and everything else "
            "positioned against it. The result is correct relative to that clip; "
            "where the group sits overall is arbitrary, which is what Shift is for.")

    longest_seconds = max(_duration(c.entries[0]["clip"])
                          / (c.entries[0]["rate"] or 1.0) for c in adrift)
    sessions = split_sessions(base, longest_seconds)

    # Pick the rate BEFORE decoding anything: the transform's cost is set by
    # the length of the longest SESSION, not of the timeline, and this runs
    # inside Resolve's process where a few hundred megabytes is enough to be
    # refused. Splitting first is most of why the rate can now stay where it
    # belongs on a multi-day project.
    span = max(end - start for start, end, _clips in sessions)
    rate = audio_sync.choose_locate_rate(span, longest_seconds)
    if rate < audio_sync.LOCATE_RATE:
        warnings.append(
            f"The longest unbroken stretch of footage here is {span/60:.0f} minutes, "
            f"so the audio was matched at {rate} Hz instead of "
            f"{audio_sync.LOCATE_RATE} Hz to keep the transform inside the memory "
            "Resolve leaves available. Accuracy is unaffected; the match is "
            "slightly less discriminating.")
    if len(sessions) > 1:
        warnings.append(
            f"The footage falls into {len(sessions)} separate stretches with more "
            f"than {longest_seconds/60:.0f} minutes of nothing between them – long "
            "enough that no recording here could span one. Each recording was "
            "matched against every stretch and kept its best answer.")

    # Decoded once and held, because every session asks for the same audio and
    # re-reading it per session would multiply the slowest part of the run by
    # the number of days in the project. At LOCATE_RATE this is 4 bytes a
    # millisecond - 96 MB for the 400 minutes of loose audio measured here,
    # against the 350 MB the single whole-timeline reference used to take.
    recordings = {}
    for index, clip in enumerate(adrift):
        if progress_cb:
            progress_cb("Reading loose audio", index, len(adrift))
        entry = clip.entries[0]
        clip_rate = entry["rate"] or 1.0
        duration = _duration(entry["clip"]) / clip_rate
        try:
            samples = audio_sync.load_window(
                ffmpeg_path, _media_path(entry["clip"]),
                source_offset_seconds(entry["clip"]), duration, rate=rate,
            )
        except audio_sync.NoAudioError as exc:
            recordings[clip.clip_id] = (None, None, duration, str(exc))
            continue
        except Exception as exc:
            recordings[clip.clip_id] = (None, None, duration, f"could not be read: {exc}")
            continue
        recordings[clip.clip_id] = (samples,
                                    audio_sync.onset_envelope(samples, rate=rate),
                                    duration, None)

    stats = {"external": len(adrift), "camera": len(base),
             "scratch_used": 0, "matched": 0, "unmatched": 0,
             "improvised": improvised, "sessions": len(sessions)}
    # A single origin clip is a far thinner reference than a whole session's
    # scratch track, so it needs a higher bar - see IMPROVISED_MIN_STANDOUT.
    needed = (audio_sync.IMPROVISED_MIN_STANDOUT if improvised
              else audio_sync.LOCATE_MIN_STANDOUT)
    best = {}

    for number, (_start, _end, session_clips) in enumerate(sessions):
        label = (f"Reading camera audio ({number + 1}/{len(sessions)})"
                 if len(sessions) > 1 else "Reading camera audio")
        try:
            reference, placed, origin, envelopes = build_scratch_reference(
                session_clips, ffmpeg_path, rate, warnings, progress_cb, label)
        except OtioError as exc:
            # One unreadable stretch is not a reason to abandon the others.
            warnings.append(str(exc).split("\n")[0])
            continue
        stats["scratch_used"] += placed
        if progress_cb:
            progress_cb("Preparing reference", 0, 1)

        # Retry by decimation rather than re-reading: the budget above is
        # calibrated on two timelines and on however much memory Resolve
        # happens to be holding, so it cannot be exact.
        longest_sample = max((len(s) for s, _e, _d, _err in recordings.values()
                              if s is not None), default=0)
        while True:
            try:
                prepared = audio_sync.prepare_reference(reference, longest_sample)
                break
            except MemoryError:
                if rate <= audio_sync.LOCATE_MIN_RATE:
                    raise OtioError(
                        "Not enough memory to match this footage. The longest "
                        f"unbroken stretch is {span/60:.0f} minutes, and the app runs "
                        "inside Resolve's own process.\n\nClosing other timelines, or "
                        "restarting Resolve and running this first, usually frees "
                        "enough. Aligning by timecode alone needs none of this."
                    )
                reference = audio_sync.decimate(reference)
                rate //= 2
                # The recordings were decoded at the old rate and are still
                # held, so they have to come down with it - a sample array at
                # one rate matched against a reference at another returns an
                # offset scaled by two, which is a wrong answer rather than a
                # failure.
                for clip_id, (held, _env, held_duration, held_failure) in \
                        list(recordings.items()):
                    if held is None:
                        continue
                    held = audio_sync.decimate(held)
                    recordings[clip_id] = (
                        held, audio_sync.onset_envelope(held, rate=rate),
                        held_duration, held_failure)
                longest_sample = max((len(held) for held, _e, _d, _f
                                      in recordings.values() if held is not None),
                                     default=0)
                warnings.append(
                    f"Ran out of memory preparing the match; retried at {rate} Hz.")
        # The transform is all that gets read from here on, and the
        # time-domain copy is another 88-138 MB inside Resolve's own process.
        del reference

        for index, clip in enumerate(adrift):
            if progress_cb:
                progress_cb("Matching audio", index, len(adrift))
            samples, envelope, duration, failure = recordings[clip.clip_id]
            if samples is None:
                continue
            try:
                # allow_before only when the reference is one improvised origin
                # clip: a clip can start before THAT, but nothing precedes the
                # start of a timeline the base clips are already laid out in.
                position, standout = audio_sync.locate_in_reference(
                    prepared, samples, rate, allow_before=improvised)
            except Exception as exc:
                warnings.append(f'"{clip.name}" could not be matched: {exc}')
                continue
            at = origin + position
            agreed, asked, _names = corroboration(envelope, duration, at, envelopes)
            accepted = (agreed >= audio_sync.MIN_CORROBORATION
                        or (agreed >= 1 and standout >= needed))
            rank = (accepted, agreed, standout)
            if clip.clip_id not in best or rank > best[clip.clip_id][0]:
                best[clip.clip_id] = (rank, at, standout, agreed, asked, accepted)

        del prepared, envelopes

    frames_by_id = {}
    unmatched = []
    for clip in adrift:
        samples, _envelope, _seconds, failure = recordings[clip.clip_id]
        if samples is None:
            unmatched.append(f"{clip.name} ({failure})")
            stats["unmatched"] += 1
            continue
        if clip.clip_id not in best:
            unmatched.append(f"{clip.name} (no footage overlaps it anywhere)")
            stats["unmatched"] += 1
            continue
        _rank, at, standout, agreed, asked, accepted = best[clip.clip_id]
        if not accepted:
            unmatched.append(
                f"{clip.name} (stood out {standout:.1f}x, needs {needed:.0f}x; "
                f"{agreed} of {asked} clip(s) playing there agree, "
                f"needs {audio_sync.MIN_CORROBORATION})")
            stats["unmatched"] += 1
            continue
        # position came back relative to its session's start, and `at` already
        # carries that back to timeline time.
        frames_by_id[clip.clip_id] = at * (clip.entries[0]["rate"] or 1.0)
        stats["matched"] += 1

    if unmatched:
        shown = ", ".join(unmatched[:3])
        more = f" (and {len(unmatched) - 3} more)" if len(unmatched) > 3 else ""
        warnings.append(f"{len(unmatched)} clip(s) could not be placed "
                        f"- left where they were: {shown}{more}")

    # Relative positions only when improvised, so nothing may be clamped on
    # its own: if the group would start before zero the WHOLE group moves
    # right, the one transformation that leaves every offset intact.
    if improvised and frames_by_id:
        seconds = {}
        for clip in clips:
            entry = clip.entries[0]
            clip_rate = entry["rate"] or 1.0
            seconds[clip.clip_id] = (frames_by_id[clip.clip_id] / clip_rate
                                     if clip.clip_id in frames_by_id
                                     else entry["start"] / clip_rate)
        earliest = min(seconds.values())
        if earliest < 0:
            warnings.append(
                f"The group would have started {-earliest:.1f}s before the timeline, "
                "so everything moved right together – the sync itself is unchanged.")
            frames_by_id = {
                c.clip_id: (seconds[c.clip_id] - earliest) * (c.entries[0]["rate"] or 1.0)
                for c in clips}

    result, _moved = align_document(document, frames_by_id, clips)
    return result, stats, warnings


def begin_external_sync(controller):
    """Exports the current timeline, ready to be synced. MAIN THREAD ONLY.

    Split from the matching itself so the slow half - reading hours of audio
    through ffmpeg - can run on a worker thread while Resolve is only ever
    touched from the thread Resolve called us on. The in-process Python
    bridge is not thread-safe, and calling it from a worker does not raise,
    it takes the whole application down."""
    document, _clips, workdir = read_current_timeline(controller)
    return document, workdir, controller.get_current_timeline().GetName()


def finish_external_sync(controller, result, stats, workdir, base_name):
    """Writes the synced document out and imports it as a new timeline.
    MAIN THREAD ONLY, for the same reason as begin_external_sync."""
    result_path = os.path.join(workdir, "external.otio")
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)

    name = unique_timeline_name(controller.get_project(),
                                f"{base_name} (Audio Synced)")
    new_timeline = controller.import_timeline_file(result_path, name)
    if new_timeline is None:
        raise OtioError(
            "Resolve refused to import the synced timeline. The original "
            f"timeline is untouched; the rebuilt file is at {result_path}"
        )
    stats["name"] = new_timeline.GetName()
    return new_timeline


# ---------- Waveform Align ----------


# ---------- the whole job, as one transform ----------

def offsets_with_fallback(clips, method, dates=None):
    """(offsets, warnings, method_used). offsets is None when timecode can
    place nothing - a state to carry on from, not an error, since the
    waveform pass can still do the work.

    There used to be a second metadata method to fall back to. There is not
    any more, so this is now only about turning a refusal into a state the
    caller can continue from."""
    try:
        offsets, warnings = compute_offsets_timecode(clips)
    except OtioError as exc:
        return None, [f"Timecode: {str(exc).splitlines()[0]}"], None
    return offsets, warnings, "timecode"


def assemble_document(document, method, dates=None, ffmpeg_path=None,
                      sync_audio=True, remove_silent=False, collapse=True,
                      close_gaps=False, progress_cb=None):
    """Aligns, optionally syncs loose audio, collapses and shifts - all on
    the document, so the caller imports once instead of four times.

    Returns (new_document, stats, warnings).

    Expand is deliberately absent. align_document already puts a clip on a
    new track only when its new position would collide, which is the same
    allocation Expand performs up front for every clip whether it needs one
    or not: on a real timeline that was 594 tracks against 21, for identical
    positions. The expanded shape is still available as its own action for
    working by hand; it just has no business being a prerequisite.

    `dates` is {normalised path: (recorded, modified)} and is only needed for
    the File Date method - it comes from Resolve, so the caller fetches it on
    the main thread before handing the work to a worker."""
    # Up front, not left to build_document: the waveform and loose-audio
    # passes below are minutes of decoding to throw away.
    refuse_unsupported(document)
    warnings = rebuild_warnings(document)
    stats = {}

    if progress_cb:
        progress_cb("Aligning", 0, 1)
    clips = logical_clips(document)
    if method == "waveform":
        # No metadata at all: clips are related to each other by their audio,
        # in groups. Nothing else in assemble_document applies - there are no
        # offsets to anchor and no loose-audio pass to run afterwards,
        # because this already placed everything it could.
        if not ffmpeg_path:
            raise OtioError(
                "Lining clips up by audio needs ffmpeg. Install it, or set a path "
                "in Settings."
            )
        # Choosing this on footage that HAS timecode gives up something
        # that would place every clip, not just the ones sharing audio.
        with_tc = sum(1 for c in clips if has_usable_timecode(c))
        if with_tc >= max(2, len(clips) // 4):
            warnings.append(
                f"NOTE: {with_tc} of {len(clips)} clips DO carry usable timecode. "
                "Timecode would position all of them; this only relates clips that "
                "were rolling at the same time. Use Timecode unless you know its "
                "timecode is wrong.")
        document, wave_stats, wave_warnings = sync_by_waveform_document(
            document, ffmpeg_path, progress_cb=progress_cb)
        warnings.extend(wave_warnings)
        stats.update(wave_stats)
        stats["method_used"] = "waveform"
        stats["aligned"] = wave_stats["placed"]
        stats["total"] = wave_stats["considered"]
        return _finish_assembly(document, logical_clips(document), ffmpeg_path,
                                False, remove_silent, collapse, close_gaps,
                                stats, warnings, progress_cb)

    offsets, align_warnings, used = offsets_with_fallback(clips, method)
    warnings.extend(align_warnings)
    stats["method_used"] = used

    if offsets is None:
        # Nothing to sort by. Every clip stays exactly where it is and the
        # waveform pass takes over - it improvises an origin and places what
        # it can against it, which is the whole point of not stopping here.
        warnings.append(
            "No clip carries usable timecode, so nothing was re-ordered – every "
            "clip stayed where it was. Anything that shares audio will still be "
            "lined up by waveform below. To sync by audio alone, choose the "
            "Waveform method instead.")
        stats["aligned"] = 0
        stats["total"] = len(clips)
        return _finish_assembly(document, clips, ffmpeg_path, sync_audio,
                                remove_silent, collapse, close_gaps,
                                stats, warnings, progress_cb)

    # Relative offsets only, so the group is anchored on its earliest clip
    # and nothing is clamped individually - the same rule offsets_to_frames
    # applies, kept here so the chain does not depend on the tab.
    anchor = min(offsets, key=offsets.get)
    by_id = {c.clip_id: c for c in clips}
    base = by_id[anchor].record_frame - offsets[anchor] * (
        by_id[anchor].entries[0]["rate"] or 1.0)
    frames = {cid: seconds * (by_id[cid].entries[0]["rate"] or 1.0) + base
              for cid, seconds in offsets.items()}
    earliest = min(list(frames.values()) + [c.record_frame for c in clips])
    if earliest < 0:
        frames = {cid: f - earliest for cid, f in frames.items()}
        warnings.append(
            f"Everything moved {abs(earliest):.0f} frame(s) right so nothing landed "
            "before the start of the timeline (relative sync is unchanged).")
    document, moved = align_document(document, frames, clips)
    stats["aligned"] = moved
    stats["total"] = len(clips)

    return _finish_assembly(document, clips, ffmpeg_path, sync_audio,
                            remove_silent, collapse, close_gaps,
                            stats, warnings, progress_cb)


def _finish_assembly(document, clips, ffmpeg_path, sync_audio, remove_silent,
                     collapse, close_gaps, stats, warnings, progress_cb,
                     force_improvise=False):
    """Everything after the metadata step: waveform placement, packing,
    shifting. Shared so that a run with no usable metadata takes exactly the
    same path as one that aligned successfully - the only difference being
    that the clips start where they already were."""
    if sync_audio and ffmpeg_path:
        try:
            document, sync_stats, sync_warnings = sync_external_audio_document(
                document, ffmpeg_path, progress_cb=progress_cb,
                force_improvise=force_improvise)
            warnings.extend(sync_warnings)
            stats["audio_matched"] = sync_stats["matched"]
            stats["audio_total"] = sync_stats["external"]
        except OtioError as exc:
            # "nothing left to place" is the happy case, not a failure - it
            # means timecode already positioned everything with sound.
            warnings.append(str(exc).split("\n")[0])
            stats["audio_matched"] = stats["audio_total"] = 0
    elif sync_audio:
        warnings.append("Loose audio was not synced – ffmpeg not found.")

    if collapse or remove_silent:
        if progress_cb:
            progress_cb("Packing tracks", 0, 1)
        document, kept, dropped, collapse_warnings = collapse_document_from(
            document, delete_silent_audio=remove_silent,
            ffmpeg_path=ffmpeg_path, progress_cb=progress_cb)
        warnings.extend(collapse_warnings)
        stats["kept"] = kept
        stats["silent_removed"] = dropped

    if progress_cb:
        progress_cb("Shifting to start", 0, 1)
    document, shift_stats = shift_document(document, close_gaps=close_gaps)
    stats.update(shift_stats)
    # Each pass reports what its own rebuild dropped, and a pass that ran on
    # the untouched document repeats the transitions note the first one gave.
    return document, stats, list(dict.fromkeys(warnings))


# ---------- Waveform: sync by audio alone, in groups ----------

# Blank timeline left between one synced group and the next, and between the
# clips that matched nothing. Groups have no known relation to each other, so
# they must not merely avoid overlapping - the boundary has to be obvious at
# the scale of the material. Sixty seconds was invisible beside a half-hour
# recording; five minutes reads as a deliberate gap at any zoom level.
#
# Shift with "close the gaps" compacts all of this afterwards for anyone who
# wants it tight, so erring wide costs nothing.
GROUP_GAP_SECONDS = 300.0

# Between the clips that matched nothing, a much smaller gap will do. The
# wide gap exists to mark where one SYNCED group ends and the next begins -
# a boundary that carries meaning. Between two clips that relate to nothing
# at all there is no boundary to advertise, only a need not to overlap, and
# spending five minutes on each of a hundred of them turned a 462-minute
# timeline into a 786-minute one.
SOLO_GAP_SECONDS = 30.0


def _decode_all(clips, ffmpeg_path, rate, warnings, progress_cb):
    """Every clip's audio, decoded once, in parallel."""
    from . import audio_sync

    def decode(clip):
        entry = clip.entries[0]
        clip_rate = entry["rate"] or 1.0
        return audio_sync.load_window(
            ffmpeg_path, _media_path(entry["clip"]),
            source_offset_seconds(entry["clip"]),
            _duration(entry["clip"]) / clip_rate, rate=rate)

    out = {}
    pool = ThreadPoolExecutor(max_workers=min(SCRATCH_DECODE_WORKERS, len(clips)))
    try:
        pending = [pool.submit(decode, c) for c in clips]
        for index, (clip, future) in enumerate(zip(clips, pending)):
            if progress_cb:
                progress_cb("Reading audio", index, len(clips))
            try:
                out[clip.clip_id] = future.result()
            except audio_sync.NoAudioError:
                continue
            except Exception as exc:
                warnings.append(f'"{clip.name}" could not be read: {exc}')
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def build_sync_groups(clips, ffmpeg_path, warnings, progress_cb=None):
    """Clips gathered into groups that genuinely share audio.

    Returns a list of {clip_id: seconds relative to that group's start}.
    Every pair inside a group was matched directly and survived the segment
    check, so a group is a set of recordings that were rolling together -
    not an ordering, which audio cannot recover."""
    from . import audio_sync

    samples = _decode_all(clips, ffmpeg_path, audio_sync.LOCATE_RATE,
                          warnings, progress_cb)
    envelopes = {cid: audio_sync.onset_envelope(s) for cid, s in samples.items()}
    envelopes = {cid: e for cid, e in envelopes.items() if len(e)}
    # The decoded audio is twenty times the size of the envelopes made from
    # it, and nothing below reads it - 195 MB held for the whole comparison
    # on the project this was measured against, inside Resolve's own process,
    # and growing with every day of footage added. The few pairs that want it
    # back re-read just those clips (see below).
    del samples
    ids = [c.clip_id for c in clips if c.clip_id in envelopes]
    by_id = {c.clip_id: c for c in clips}

    def shared_frames(a, b, lag):
        return (min(len(envelopes[a]), lag + len(envelopes[b]))
                - max(0, lag))

    adjacency, edges = {}, []
    short = []
    for index, a in enumerate(ids):
        if progress_cb:
            progress_cb("Comparing clips", index, len(ids))
        for b in ids[index + 1:]:
            lag, score = audio_sync.correlate_envelopes(envelopes[a], envelopes[b])
            if score < audio_sync.ONSET_MIN_SCORE:
                continue
            overlap = shared_frames(a, b, lag) / audio_sync.ONSET_RATE
            if overlap < audio_sync.SHORT_OVERLAP_MIN_SECONDS:
                # Includes the negative case, where the offset puts the two
                # clips nowhere near each other - a peak with no overlap
                # behind it is not a match however tall it is.
                continue
            if shared_frames(a, b, lag) < 3 * audio_sync.ONSET_RATE * 20:
                # Too little to split into stretches; held for the raw
                # waveform to confirm or reject once the pass is done, so the
                # audio is re-read for a handful of clips rather than kept for
                # all of them.
                short.append((score, a, b, lag))
                continue
            if audio_sync.segments_agree(envelopes[a], envelopes[b], lag) \
                    < audio_sync.ONSET_MIN_SEGMENTS:
                continue
            offset = lag / audio_sync.ONSET_RATE
            edges.append((score, a, b, offset))
            adjacency.setdefault(a, []).append((b, offset, score))
            adjacency.setdefault(b, []).append((a, -offset, score))

    if short:
        wanted = {cid for _score, a, b, _lag in short for cid in (a, b)}
        if progress_cb:
            progress_cb("Confirming short overlaps", 0, len(wanted))
        again = _decode_all([by_id[cid] for cid in sorted(wanted)], ffmpeg_path,
                            audio_sync.LOCATE_RATE, warnings, progress_cb)
        for score, a, b, lag in short:
            if a not in again or b not in again:
                continue
            offset = lag / audio_sync.ONSET_RATE
            if not audio_sync.raw_confirms(again[a], again[b], offset,
                                           audio_sync.LOCATE_RATE):
                continue
            edges.append((score, a, b, offset))
            adjacency.setdefault(a, []).append((b, offset, score))
            adjacency.setdefault(b, []).append((a, -offset, score))
        del again

    def grow(first, second, offset):
        """Extends a group by taking the best-corroborated clip each time.

        A clip is positioned by the agreement of its edges to clips already
        in the group, not by any single edge - which is what stops one bad
        match dragging everything after it out of place."""
        local = {first: 0.0, second: offset}
        while True:
            best = None
            for node in ids:
                if node in local or node not in adjacency:
                    continue
                votes = [(local[o] - off, s) for o, off, s in adjacency[node]
                         if o in local]
                if not votes:
                    continue
                group, power = None, 0.0
                for position, _score in votes:
                    near = [(p, s) for p, s in votes if abs(p - position) <= 1.5]
                    strength = sum(s for _p, s in near)
                    if group is None or len(near) > len(group) or (
                            len(near) == len(group) and strength > power):
                        group, power = near, strength
                candidate = (len(group), power, node,
                             sum(p for p, _s in group) / len(group))
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
            if best is None:
                return local
            local[best[2]] = best[3]

    groups, claimed = [], set()
    for _score, a, b, offset in sorted(edges, reverse=True):
        if a in claimed or b in claimed:
            continue
        local = grow(a, b, offset)
        groups.append(local)
        claimed |= set(local)
    return groups, len(ids)


def sync_by_waveform_document(document, ffmpeg_path, progress_cb=None):
    """Lines clips up by their audio alone, with no metadata at all.

    Each group is laid out one after another with a gap, because nothing
    relates one group to another and letting them overlap would imply a
    relationship that was never measured. Clips matching nothing keep their
    place; they share audio with no other clip, so there is nothing to sync
    them to."""
    refuse_unsupported(document)
    clips = logical_clips(document)
    audible = [c for c in clips if c.has_audio and c.path]
    warnings = rebuild_warnings(document)
    if len(audible) < 2:
        raise OtioError("There are fewer than two clips with audio, so there "
                        "is nothing to line up against anything.")

    groups, considered = build_sync_groups(clips=audible, ffmpeg_path=ffmpeg_path,
                                           warnings=warnings, progress_cb=progress_cb)
    real = [g for g in groups if len(g) > 1]
    if not real:
        raise OtioError(
            "No two clips were found to share any audio.\n\nThis lines up "
            "recordings that were rolling AT THE SAME TIME. Clips shot one after "
            "another share nothing to match, and no tool can order them from "
            "audio – use Timecode or File Date for that."
        )

    frames_by_id = {}
    by_id = {c.clip_id: c for c in audible}

    def seconds_of(clip_id):
        entry = by_id[clip_id].entries[0]
        return _duration(entry["clip"]) / (entry["rate"] or 1.0)

    def put(clip_id, seconds):
        frames_by_id[clip_id] = seconds * (by_id[clip_id].entries[0]["rate"] or 1.0)

    cursor = 0.0
    for group in sorted(real, key=len, reverse=True):
        base = min(group.values())
        for clip_id in group:
            put(clip_id, cursor + (group[clip_id] - base))
        cursor += max(group[c] - base + seconds_of(c) for c in group)
        cursor += GROUP_GAP_SECONDS

    # Everything that matched nothing is laid out after the groups rather
    # than left where it was. Leaving it put is what let a long recording
    # run straight through the groups: its old position was never meaningful
    # here, and keeping it only made it collide with positions that are.
    grouped = {c for group in real for c in group}
    for clip in audible:
        if clip.clip_id in grouped:
            continue
        put(clip.clip_id, cursor)
        cursor += seconds_of(clip.clip_id) + SOLO_GAP_SECONDS

    placed = sum(len(g) for g in real)
    warnings.append(
        f"Found {len(real)} group(s) of clips that share audio, holding {placed} "
        f"clip(s) in total. Each group is laid out separately with a gap – they "
        "were never related to each other, only to themselves.")
    left = considered - placed
    if left:
        warnings.append(
            f"{left} clip(s) share audio with nothing else. They are laid out "
            "after the groups, one per slot – they have no offset to find, so "
            "where they sit carries no meaning.")

    result, _moved = align_document(document, frames_by_id, clips)
    stats = {"groups": len(real), "placed": placed, "considered": considered}
    return result, stats, warnings
