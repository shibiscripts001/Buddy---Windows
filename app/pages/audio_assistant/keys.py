#!/usr/bin/env python3
"""
Volume keyframes - Resolve's own, read and written through timeline files, as
Resolve's scripting has no call for them. No Qt, no Resolve, so it's tested
directly.

Measured on Resolve 21.1.0.17, on probe timelines that were rendered and the
renders measured:
  - Timeline.Export(path, EXPORT_FCP_7_XML) - a few ms, and nothing changes -
    lists every audio clip with its keys, under an "Audio Levels" filter as
    linear gain, <when> in source frames: parse_fcp7() reads them.
  - an FCPXML imported with MediaPool.ImportTimelineFromFile keeps the keys of
    an <adjust-volume>'s keyframeAnimation (interp="ease" comes in as a curve):
    nested_fcpxml() writes one. The FCPXML export can't be read back instead:
    it leaves out audio clips with no video over them.
  - on a keyed clip the keys are what plays. Its AudioVolume is still stored
    and read back, but not heard (-10 dB on a clip keyed at -20 still played
    at -20).
  - a timeline placed on another as a clip (a nested clip) plays its keys with
    the nested clip's own volume on top (-6 dB on it: 5.9 dB quieter).
  - between two keys Resolve moves the volume in a straight line in dB (a
    0 to -10 dB ramp measured -4.99 halfway). Its own ease is a curve of its
    making (and it kept only some of the eases asked for), so Buddy doesn't
    ask for it: expand() writes an eased stretch as short straight pieces,
    and what plays is what Buddy drew, to a fraction of a dB.
  - a clip's start in a file with a start timecode (a camera's MP4, 29.97)
    came in one frame late, written exactly; written a quarter frame early
    it comes in on the frame (nested_fcpxml does that; apply_curve checks).
So a curve drawn in Buddy goes to Resolve as a one-clip timeline carrying the
keys, placed where the clip was (resolve_ext.apply_curve), and a clip keyed in
Resolve shows its keys in Buddy.

A key is {"t": seconds into the file, "db": volume, "ease": bool}: file time,
so a key stays on its word however the clip is trimmed. Between two keys the
volume moves straight, in dB - or, next to an eased key, on a curve that
flattens out into it: curve_db() is that line, and timeline.js's curveDb() is
the same sum. Buddy's own keys (with their eases) are kept in the curve
timeline's Comments, as Resolve only has the expanded ones (note(), read_note()).
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction
from xml.sax.saxutils import quoteattr

import numpy as np

# The volume a key can have: Resolve's clip volume range (FCP 7 XML's
# Audio Levels run 1e-05 to 31.6228 - the same, as gain).
MIN_DB, MAX_DB = -100.0, 30.0
# The timeline that carries a curve is named after its clip's file with this on
# the end (" (Buddy curve 2)" when that's taken) - which is also how Buddy knows
# one again, with the note in its Comments.
CURVE_SUFFIX = " (Buddy curve)"
_CURVE_NAME = re.compile(r" \(Buddy curve(?: \d+)?\)$")
BIN_NAME = "Buddy Audio"


def is_curve_name(name):
    return bool(_CURVE_NAME.search(name or ""))


def curve_name(base, taken):
    """A curve timeline's name for a clip of file `base`, not one of `taken`."""
    name, n = f"{base}{CURVE_SUFFIX}", 2
    while name in taken:
        name, n = f"{base} (Buddy curve {n})", n + 1
    return name


def clamp_db(db):
    return max(MIN_DB, min(MAX_DB, float(db)))


def gain_to_db(gain):
    return clamp_db(20 * np.log10(max(float(gain), 1e-5)))


def db_to_gain(db):
    return 10 ** (clamp_db(db) / 20)


# ------------------------------------------------------------- the curve --

def clean(keys):
    """keys as the view sends them -> sorted, one per time, values in range."""
    out = {}
    for key in keys or []:
        try:
            t, db = float(key["t"]), float(key["db"])
        except (KeyError, TypeError, ValueError):
            continue
        if np.isfinite(t) and np.isfinite(db):
            out[round(t, 6)] = {"t": round(t, 6), "db": round(clamp_db(db), 2), "ease": bool(key.get("ease"))}
    return [out[t] for t in sorted(out)]


def curve_db(keys, t):
    """The volume (dB) the keys give at file time(s) t: the first key's before
    it, the last's after. Between two keys a cubic in dB whose slope at each end
    is the straight line's - or flat, at an eased key - so with no eased key
    it's the straight line itself."""
    t = np.asarray(t, dtype=np.float64)
    if not keys:
        return np.zeros_like(t)
    kt = np.array([k["t"] for k in keys], dtype=np.float64)
    kd = np.array([k["db"] for k in keys], dtype=np.float64)
    if len(keys) == 1:
        return np.full_like(t, kd[0])
    ease = np.array([bool(k.get("ease")) for k in keys])
    i = np.clip(np.searchsorted(kt, t, side="right") - 1, 0, len(kt) - 2)
    h = np.maximum(kt[i + 1] - kt[i], 1e-9)
    u = np.clip((t - kt[i]) / h, 0.0, 1.0)
    chord = kd[i + 1] - kd[i]
    ma = np.where(ease[i], 0.0, chord)            # slopes, per unit of u
    mb = np.where(ease[i + 1], 0.0, chord)
    u2, u3 = u * u, u * u * u
    out = ((2 * u3 - 3 * u2 + 1) * kd[i] + (u3 - 2 * u2 + u) * ma
           + (-2 * u3 + 3 * u2) * kd[i + 1] + (u3 - u2) * mb)
    return np.where(t <= kt[0], kd[0], np.where(t >= kt[-1], kd[-1], out))


def shifted(keys, db):
    """The same keys, every one moved by db."""
    return [dict(k, db=round(clamp_db(k["db"] + db), 2)) for k in keys]


# An eased stretch is written as straight pieces this many dB apart at most,
# and no shorter than a frame: a smooth 20 dB turn is then within ~0.2 dB.
EXPAND_DB = 1.5
EXPAND_MAX = 32


def expand(keys, fps, origin=0.0):
    """keys with no eases: every eased stretch as straight pieces on frame
    boundaries (a frame being 1/fps s from origin - the clip's start in the
    file), which is what Buddy asks of Resolve."""
    keys = clean(keys)
    if not any(k["ease"] for k in keys):
        return keys
    out = []
    for a, b in zip(keys, keys[1:]):
        out.append({"t": a["t"], "db": a["db"], "ease": False})
        if not (a["ease"] or b["ease"]):
            continue
        frames = int(np.floor((b["t"] - a["t"]) * fps + 1e-6))
        n = min(EXPAND_MAX, frames, max(2, int(np.ceil(abs(b["db"] - a["db"]) / EXPAND_DB))))
        seen = {round((a["t"] - origin) * fps), round((b["t"] - origin) * fps)}
        for j in range(1, n):
            frame = round((a["t"] + (b["t"] - a["t"]) * j / n - origin) * fps)
            if frame in seen:
                continue
            seen.add(frame)
            t = origin + frame / fps
            out.append({"t": round(t, 6), "db": round(float(curve_db([a, b], t)), 2), "ease": False})
    out.append(dict(keys[-1], ease=False))
    return clean(out)


def same_keys(a, b, db=0.05, t=0.002):
    """Whether two lists of keys are the same line (times within t s, levels within db)."""
    return len(a) == len(b) and all(abs(x["t"] - y["t"]) <= t and abs(x["db"] - y["db"]) <= db
                                    for x, y in zip(a, b))


# ---------------------------------------------------- Buddy's own note --

def note(keys, original):
    """What a curve timeline's Comments hold: Buddy's keys (with their eases,
    as the clip's own volume on top will be) and the clip it stands in for."""
    return json.dumps({"buddy": "curve", "v": 1, "original": original, "keys": clean(keys)}, separators=(",", ":"))


def read_note(text):
    """note()'s {"original", "keys"}, or None for anything else."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("buddy") != "curve":
        return None
    return {"original": str(data.get("original") or ""), "keys": clean(data.get("keys"))}


# ------------------------------------------------- reading: FCP 7 XML --

def _int(node, path, default=0):
    try:
        return int(float(node.findtext(path)))
    except (TypeError, ValueError):
        return default


def _rate(node):
    """A <rate>'s frames per second."""
    base = _int(node, "rate/timebase", 0)
    ntsc = (node.findtext("rate/ntsc") or "").upper() == "TRUE"
    return base * 1000 / 1001 if ntsc else float(base)


def _levels(clipitem):
    """(the static level in dB, [{"f": source frame, "db", "ease"}]) of a
    clipitem's Audio Levels, or (None, []) without one."""
    for effect in clipitem.iter("effect"):
        if (effect.findtext("effectid") or "") != "audiolevels":
            continue
        param = effect.find("parameter")
        if param is None:
            return None, []
        try:
            level = gain_to_db(float(param.findtext("value")))
        except (TypeError, ValueError):
            level = None
        keys = []
        for frame in param.findall("keyframe"):
            try:
                when, value = float(frame.findtext("when")), float(frame.findtext("value"))
            except (TypeError, ValueError):
                continue
            keys.append({"f": when, "db": round(gain_to_db(value), 2),
                         "ease": frame.find("inbez") is not None or frame.find("outbez") is not None})
        return level, keys
    return None, []


def parse_fcp7(text):
    """The audio clips of an FCP 7 XML export (Resolve's, EXPORT_FCP_7_XML):
    [{"track", "name", "start", "end" (sequence frames from 0), "in" (source
    frame), "rate", "level" (dB), "keys": [{"f": source frame, "db", "ease"}]}].
    A clip whose start or end is a transition's has -1 there."""
    root = ET.fromstring(text)
    out = []
    for n, track in enumerate(root.findall("./sequence/media/audio/track"), start=1):
        for item in track.findall("clipitem"):
            level, keys = _levels(item)
            out.append({
                "track": n, "name": item.findtext("name") or "",
                "start": _int(item, "start", -1), "end": _int(item, "end", -1), "in": _int(item, "in", 0),
                "rate": _rate(item) or _rate(root.find("sequence")), "level": level, "keys": keys,
            })
    return out


def match_keys(items, clips, timeline_start):
    """{clip id: keys in file time} for the clips (read_timeline's: "id",
    "name", "start", "end", "offset") that have keys in parse_fcp7()'s items.
    Matched by where they sit (a stereo clip can come as one clipitem per
    channel - the first with keys counts), then by name."""
    by_place = {}
    for item in items:
        if item["keys"]:
            by_place.setdefault((item["start"], item["end"]), []).append(item)
    out = {}
    for clip in clips:
        start, end = clip["start"] - timeline_start, clip["end"] - timeline_start
        found = by_place.get((start, end)) or by_place.get((-1, end)) or by_place.get((start, -1)) or []
        item = next((i for i in found if i["name"] == clip["name"]), found[0] if found else None)
        if item is None or not item["rate"]:
            continue
        out[clip["id"]] = clean([{"t": clip["offset"] + (k["f"] - item["in"]) / item["rate"], "db": k["db"],
                                  "ease": k["ease"]} for k in item["keys"]])
    return out


# ------------------------------------------------- writing: FCPXML --

def frame_duration(fps):
    """One frame, exactly: 1001/30000 s for 29.97, 1/25 s for 25."""
    fps = float(fps)
    whole = round(fps)
    if abs(fps - whole) < 0.001:
        return Fraction(1, whole)
    ntsc = round(fps * 1001 / 1000)
    if abs(fps - ntsc * 1000 / 1001) < 0.01:
        return Fraction(1001, ntsc * 1000)
    return Fraction(1, whole)


def fcp_time(value):
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}s" if value else "0s"


def file_url(path):
    """A media path as FCPXML's src: file://localhost/E:/My%20Media/a.wav."""
    path = os.path.abspath(path).replace("\\", "/")
    return "file://localhost" + ("" if path.startswith("/") else "/") + urllib.parse.quote(path, safe="/:")


def _db(value):
    text = f"{round(float(value), 2):g}"
    return f"{text}dB"


def nested_fcpxml(name, path, fps, timeline_start, frames, media_start, source_in, channels, keys,
                  media_frame=None, media_duration=None):
    """An FCPXML timeline `name` holding one audio clip - `frames` long, from
    the file at `path` - with `keys` as its volume keyframes.

    fps / timeline_start: the timeline it goes on (the new one starts at the
    same timecode). media_start: the file's own start in seconds (its start
    timecode - a camera file's isn't 0), as a Fraction. source_in: where in the
    file the clip starts, seconds from the file's start, as a Fraction, on one
    of the file's frames (media_frame long) - written a quarter of one early,
    as Resolve rounds it up. keys: expand()'s, at file times (seconds from its
    start), each put on the frame of the timeline it's nearest - exactly, and
    a sliver after, as Resolve rounds a key's time down (keys written as
    decimals just short of their frame came in a frame early)."""
    fd = frame_duration(fps)
    clip_in = media_start + source_in
    if media_frame and source_in + media_start >= Fraction(media_frame) / 4:
        clip_in -= Fraction(media_frame) / 4
    start = timeline_start * fd
    length = frames * fd
    duration = Fraction(media_duration) if media_duration else source_in + length
    stem = os.path.basename(path)

    def key_time(t):
        return media_start + source_in + round((t - float(source_in)) / float(fd)) * fd + fd / 64

    frames_xml = "".join(
        f'<keyframe time="{fcp_time(key_time(k["t"]))}"'
        f' value="{_db(k["db"])}"{" interp=" + quoteattr("ease") if k.get("ease") else ""}/>'
        for k in keys)
    volume = (f'<adjust-volume><param name="amount"><keyframeAnimation>{frames_xml}</keyframeAnimation></param></adjust-volume>'
              if keys else '<adjust-volume amount="0dB"/>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
    <resources>
        <format id="r0" frameDuration="{fcp_time(fd)}" width="1920" height="1080"/>
        <asset id="r1" name={quoteattr(stem)} start="{fcp_time(media_start)}" duration="{fcp_time(duration)}" hasAudio="1" audioSources="1" audioChannels="{max(1, int(channels or 1))}">
            <media-rep kind="original-media" src={quoteattr(file_url(path))}/>
        </asset>
    </resources>
    <library>
        <event name={quoteattr(BIN_NAME)}>
            <project name={quoteattr(name)}>
                <sequence format="r0" tcFormat="NDF" tcStart="{fcp_time(start)}" duration="{fcp_time(length)}">
                    <spine>
                        <gap offset="{fcp_time(start)}" duration="{fcp_time(length)}" start="{fcp_time(start)}">
                            <asset-clip ref="r1" lane="-1" offset="{fcp_time(start)}" duration="{fcp_time(length)}" start="{fcp_time(clip_in)}" name={quoteattr(stem)} srcEnable="audio">{volume}</asset-clip>
                        </gap>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
'''
