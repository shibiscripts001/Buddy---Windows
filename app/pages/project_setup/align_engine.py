#!/usr/bin/env python3
"""
Engine behind the "Align" tab: Expand (spread whatever's on the current
timeline onto one dedicated video/audio track per clip) and Align (compute
a sync offset per clip - by waveform, embedded timecode, or embedded
recording date/time - and move each clip to it).

No Qt/UI imports here, same separation as relink_engine.py - this is pure
logic against a ResolveController, unit-testable without a real window.
"""


from . import ffmpeg_utils

METHOD_WAVEFORM = "waveform"
METHOD_WAVEFORM = "waveform"
METHOD_TIMECODE = "timecode"


class AlignError(RuntimeError):
    """Raised for a whole-batch problem (nothing to work with at all) -
    per-clip problems are collected as warnings instead, so one bad clip
    doesn't block handling the rest."""


# ---------- Offset computation (pure math, no Resolve calls) ----------

def offsets_to_frames(offsets_seconds, fps, snap_to_start=True, lead_in_frames=0,
                      anchor_id=None, anchor_frame=0):
    """Converts each clip's seconds-offset into a project-frame
    recordFrame. Returns (frames_by_id, shifted_frames).

    The offsets themselves are only ever RELATIVE - they say how clips sit
    against each other, never where the group belongs on the timeline. That
    choice is this function's, and it is made one of two ways:

    snap_to_start: the earliest clip lands at lead_in_frames (default 0,
    the very start of the timeline) and everything else follows from it.
    Timecode sync in particular tends to anchor at the source's real
    timecode (e.g. "01:00:00:00"), which un-snapped would leave the whole
    synced group sitting an hour into an otherwise empty timeline.

    Otherwise the group is anchored on anchor_id - the reference clip for
    waveform sync, the earliest clip for the metadata methods - which is
    left EXACTLY where it already sits, with every other clip placed
    around it, before or after. That is what picking a reference clip
    implies, and it was not what used to happen: offsets were previously
    used as raw frame positions, so every clip belonging before the
    reference got a negative recordFrame, which
    build_clip_info_attempts then clamped with max(0, ...) - silently
    stacking all of them on frame 0, sync destroyed, no warning.

    Either way the result is guaranteed non-negative: if anchoring would
    put anything before the start of the timeline, the whole group is
    shifted right together (preserving every relative offset, which is the
    part that must not change) and shifted_frames reports by how much, so
    the caller can say so rather than let it pass unmentioned."""
    frames = {cid: seconds * fps for cid, seconds in offsets_seconds.items()}
    if not frames:
        return {}, 0

    if snap_to_start:
        earliest = min(frames.values())
        frames = {cid: f - earliest + lead_in_frames for cid, f in frames.items()}
        shifted = 0.0
    else:
        base = anchor_frame - frames.get(anchor_id, 0.0)
        frames = {cid: f + base for cid, f in frames.items()}
        earliest = min(frames.values())
        shifted = -earliest if earliest < 0 else 0.0
        if shifted:
            frames = {cid: f + shifted for cid, f in frames.items()}

    return {cid: int(round(f)) for cid, f in frames.items()}, int(round(shifted))


