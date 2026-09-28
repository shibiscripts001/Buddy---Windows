#!/usr/bin/env python3
"""
What the Mixer controls ask of Resolve, worked out from the page's copy of
the timeline - no Qt, no Resolve, so it's tested directly.

    changes(clips, request)   a control's request -> resolve_ext.apply()'s changes
    match(clips, ids, ...)    Match: the clip volumes that hit a loudness or peak target
    cuts(tracks, ids)         the cuts between selected clips, for crossfades
    PRESETS                   the targets Match offers

Match is Buddy's own sums, not Resolve's Timeline.NormalizeAudioLevel: on 21.1
that ignores targetLoudness - EBU R128 at -14, at -23, with none, and YouTube
mode all set the same volume (measured on a duplicate timeline) - so it can't
aim for YouTube's -14. (targetLevel does work, for its peak modes.) Buddy's
loudness (peaks.py, BS.1770) reads the same as ffmpeg's ebur128, so the new
volume is target - measured + current: instant, and undone like any change.
It's the file's audio through the clip's volume and fades - not through
Voice Isolation or the Dialogue Leveler, which Resolve applies after.
timeline.js's lufs() is the same sum, so the panel's figure is what's aimed at.
"""

import numpy as np

from .resolve_ext import LEVELER_GAIN_RANGE, PAN_RANGE, VOLUME_RANGE

# Match's targets: integrated loudness (LUFS; LKFS is the same measure), or
# the loudest sample (dBFS).
PRESETS = [
    {"id": "youtube", "label": "YouTube, Spotify – −14 LUFS", "loudness": -14.0},
    {"id": "podcast", "label": "Podcast – −16 LUFS", "loudness": -16.0},
    {"id": "ebu", "label": "Broadcast, EBU R128 – −23 LUFS", "loudness": -23.0},
    {"id": "atsc", "label": "Broadcast, ATSC A/85 – −24 LKFS", "loudness": -24.0},
    {"id": "peak", "label": "Peaks at −1 dBFS", "peak": -1.0},
]
PRESET_IDS = {p["id"]: p for p in PRESETS}

# Resolve's Dialogue Leveler modes (resolve.DIALOGUE_LEVELER_MODE_*, 0-3).
LEVELER_MODES = ["Allow wider dynamics", "Optimize moderate levels", "More lift for low levels",
                 "Lift soft, whispery sources"]
_LEVELER_KEYS = {"reduce_loud": "AudioDialogueLevelerReduceLoudDialogue",
                 "lift_soft": "AudioDialogueLevelerLiftSoftDialogue",
                 "background": "AudioDialogueLevelerBackgroundReduction"}


def _clamp(value, bounds):
    return max(bounds[0], min(bounds[1], float(value)))


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def changes(clips, request):
    """{clip id: {"props": {...}, "fades": {...}}} for resolve_ext.apply().

    clips: {id: clip} as the page has them (resolve_ext.read_timeline's).
    request (from the view): {"ids", and any of "volume" (dB, set to),
    "volume_by" (dB, change by), "pan", "fade_in"/"fade_out" (frames),
    "isolation": {"on", "amount"}, "leveler": {"on", "mode", "reduce_loud",
    "lift_soft", "background", "gain"}}. Voice Isolation and the Dialogue
    Leveler are only asked of clips that have them (read as None otherwise:
    Resolve would refuse the clip's whole change)."""
    out = {}
    for uid in request.get("ids") or []:
        clip = clips.get(uid)
        if not clip or clip.get("transition"):
            continue
        props, fades = {}, {}
        volume = _number(request.get("volume"))
        by = _number(request.get("volume_by"))
        if volume is not None:
            props["AudioVolume"] = round(_clamp(volume, VOLUME_RANGE), 2)
        elif by is not None:
            props["AudioVolume"] = round(_clamp((clip.get("volume") or 0.0) + by, VOLUME_RANGE), 2)
        pan = _number(request.get("pan"))
        if pan is not None:
            props["AudioPan"] = round(_clamp(pan, PAN_RANGE), 1)
        iso = request.get("isolation")
        if isinstance(iso, dict) and clip.get("isolation") is not None:
            if "on" in iso:
                props["AudioVoiceIsolationEnabled"] = bool(iso["on"])
            amount = _number(iso.get("amount"))
            if amount is not None:
                props["AudioVoiceIsolationAmount"] = int(round(_clamp(amount, (0, 100))))
        lev = request.get("leveler")
        if isinstance(lev, dict) and clip.get("leveler") is not None:
            if "on" in lev:
                props["AudioDialogueLevelerEnabled"] = bool(lev["on"])
            mode = _number(lev.get("mode"))
            if mode is not None and 0 <= int(mode) < len(LEVELER_MODES):
                props["AudioDialogueLevelerMode"] = float(int(mode))
            for key, name in _LEVELER_KEYS.items():
                if key in lev:
                    props[name] = bool(lev[key])
            gain = _number(lev.get("gain"))
            if gain is not None:
                props["AudioDialogueLevelerOutputGain"] = round(_clamp(gain, LEVELER_GAIN_RANGE), 2)
        # Fades: whole frames, and the two together never longer than the clip.
        length = max(0, int(clip["end"]) - int(clip["start"]))
        fade_in, fade_out = _number(request.get("fade_in")), _number(request.get("fade_out"))
        # Asked together, the fade in gets its length first.
        now_in, now_out = int(round(clip.get("fade_in") or 0)), int(round(clip.get("fade_out") or 0))
        if fade_in is not None:
            fades["FadeIn"] = min(int(round(max(0.0, fade_in))), max(0, length - (now_out if fade_out is None else 0)))
        if fade_out is not None:
            fades["FadeOut"] = min(int(round(max(0.0, fade_out))), max(0, length - fades.get("FadeIn", now_in)))
        change = {}
        if props:
            change["props"] = props
        if fades:
            change["fades"] = fades
        if change:
            out[uid] = change
    return out


# ------------------------------------------------------------------ Match --

def gate(blocks, raw=None):
    """BS.1770 integrated loudness of 400 ms blocks' mean squares: gated at
    -70 LUFS, then 10 LU under the mean of what's left. None for silence.
    raw: the same blocks before the clip volume, for the -70 gate - so a clip
    turned right down still measures (and Match can bring it back up) instead
    of all of it falling under the gate."""
    blocks = np.asarray(blocks, dtype=np.float64)
    if not len(blocks):
        return None
    raw = blocks if raw is None else np.asarray(raw, dtype=np.float64)
    with np.errstate(divide="ignore"):
        loud = -0.691 + 10 * np.log10(np.maximum(raw, 1e-20))
    kept = blocks[loud > -70]
    if not len(kept):
        return None
    relative = -0.691 + 10 * np.log10(kept.mean()) - 10
    kept = kept[-0.691 + 10 * np.log10(np.maximum(kept, 1e-20)) > relative]
    return float(-0.691 + 10 * np.log10(kept.mean())) if len(kept) else None


def _fades(clip, frames):
    """Each frame's gain from the clip's fades (1 = none), linear like timeline.js's."""
    g = np.ones_like(frames)
    start, end = clip["start"], clip["end"]
    fi, fo = clip.get("fade_in") or 0, clip.get("fade_out") or 0
    if fi > 0:
        g = np.where(frames < start + fi, g * np.clip((frames - start) / fi, 0, 1), g)
    if fo > 0:
        g = np.where(frames > end - fo, g * np.clip((end - frames) / fo, 0, 1), g)
    return g


def clip_blocks(clip, audio, fps):
    """A clip's 400 ms gating blocks: (as it plays - its file's K-weighted
    energy, audio["loud"] one per audio["block"] s, through its volume and
    fades; the same without the volume, for gate()'s raw)."""
    loud, block = audio["loud"], audio["block"]
    first = max(0, int(np.floor(clip.get("offset", 0.0) / block)))
    last = min(len(loud), int(np.ceil((clip.get("offset", 0.0) + (clip["end"] - clip["start"]) / fps) / block)))
    if last - first < 4:
        return np.zeros(0), np.zeros(0)
    j = np.arange(first, last)
    frames = clip["start"] + ((j + 0.5) * block - clip.get("offset", 0.0)) * fps
    g = _fades(clip, frames)
    raw = np.convolve(loud[first:last] * g * g, np.ones(4) / 4, mode="valid")
    return raw * 10 ** ((clip.get("volume") or 0.0) / 10), raw


def clip_lufs(clip, audio, fps):
    """A clip's integrated loudness as it plays, or None for silence."""
    return gate(*clip_blocks(clip, audio, fps))


def clip_peak(clip, audio, fps):
    """The loudest sample a clip plays (dBFS), through its volume; None if silent."""
    codes, rate = audio["codes"], audio["rate"]
    a = max(0, int(np.floor(clip.get("offset", 0.0) * rate)))
    b = min(len(codes), int(np.ceil((clip.get("offset", 0.0) + (clip["end"] - clip["start"]) / fps) * rate)))
    top = int(codes[a:b].max()) if b > a else 0
    if not top:
        return None
    return audio["floor"] + top / 255 * -audio["floor"] + (clip.get("volume") or 0.0)


def match(clips, ids, audio_of, fps, preset, independent=True):
    """(changes for resolve_ext.apply(), report). Each clip's volume moves by
    what takes it to the preset's target - every clip by its own amount
    (independent), or all by one amount that takes them together there.
    audio_of(clip) gives the clip's file's decoded audio or None. The report
    says which clips were skipped: "missing" (no waveform yet, or no file),
    "silent", and "clamped" - Resolve's clip volume stops at +30 dB."""
    chosen = [clips[i] for i in ids if i in clips and not clips[i].get("transition")]
    report = {"missing": [], "silent": [], "clamped": []}
    measured = {}
    for clip in chosen:
        audio = audio_of(clip)
        if audio is None:
            report["missing"].append(clip["id"])
        elif "loudness" in preset:
            measured[clip["id"]] = clip_blocks(clip, audio, fps)
        else:
            measured[clip["id"]] = clip_peak(clip, audio, fps)
    target = preset.get("loudness", preset.get("peak"))
    if "loudness" in preset:
        level = {uid: gate(*blocks) for uid, blocks in measured.items()}
        together = gate(np.concatenate([b[0] for b in measured.values()]),
                        np.concatenate([b[1] for b in measured.values()])) if measured else None
    else:
        level = measured
        values = [v for v in measured.values() if v is not None]
        together = max(values) if values else None
    out = {}
    for clip in chosen:
        uid = clip["id"]
        if uid not in level:
            continue
        now = level[uid] if independent else together
        if now is None:
            report["silent"].append(uid)
            continue
        wanted = (clip.get("volume") or 0.0) + target - now
        volume = round(_clamp(wanted, VOLUME_RANGE), 1)
        if abs(volume - wanted) > 0.05:
            report["clamped"].append(uid)
        out[uid] = {"props": {"AudioVolume": volume}}
    return out, report


def cuts(tracks, ids):
    """The ids of the selected clips whose end is a cut straight into another
    selected clip on the same track, without a crossfade there already."""
    chosen = set(ids)
    left = []
    for track in tracks:
        clips = sorted((c for c in track["clips"] if not c.get("transition")), key=lambda c: c["start"])
        faded = [(t["start"], t["end"]) for t in track["clips"] if t.get("transition")]
        for a, b in zip(clips, clips[1:]):
            if a["id"] in chosen and b["id"] in chosen and a["end"] == b["start"] \
                    and not any(s <= a["end"] <= e for s, e in faded):
                left.append(a["id"])
    return left
