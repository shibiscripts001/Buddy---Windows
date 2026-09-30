#!/usr/bin/env python3
"""
A clip's Edit-page framing (the Inspector's Zoom, Position, Rotation, Anchor
Point, Flip and Crop) as one placement Buddy builds into the clip's Fusion
comp, in front of a motion preset.

Why: Resolve runs a clip's Fusion comp first and the Inspector's transform
on the comp's finished frame afterwards, so a preset moving the picture past
that frame's edge is cut off there as soon as the clip is zoomed out or
moved. With the framing inside the comp, the preset moves the picture from
and to where it was placed, on the whole screen, and the Inspector is left
at neutral. Apply again after re-framing in the Inspector folds the new
framing on top (compose); Remove puts it back in the Inspector.

A placement is {"m": (a, b, c, d), "t": (x, y), "crop": (l, r, t, b)}:
    m      2x2 matrix [[a, b], [c, d]] - flip, zoom, then rotate - taking a
           point on the picture (screen pixels from its centre, y up) to the
           screen
    t      where the picture's centre lands, screen pixels from the frame's
           centre, y up
    crop   pixels off each edge of the picture as fitted to the timeline

Resolve's units, measured on Studio 21.1 (2026-09-30), a 641x479 still
fitted 1444x1080 on a 1920x1080 timeline:
    Pan / Tilt       move the clip by value x (source frame's SHOWN size /
                     timeline size): Pan 200 moved that still 151 px, a
                     full-frame clip 200 px. Tilt is up.
    Anchor Point     GetProperty reads it in the same units as Pan (set 200,
                     read 265.7); zoom and rotation turn the picture round
                     it: its centre lands at A - M.A.
    Crop             pixels of the picture as shown, before zoom; the rest
                     of the picture stays put. "Retain Image Position" and a
                     soft crop aren't copied: that crop stays in the
                     Inspector, working on the finished frame as before.

No Resolve and no Qt in here: tests/test_framing.py runs it on plain Python.
"""

import json
import math

CROPS = ("CropLeft", "CropRight", "CropTop", "CropBottom")
NEUTRAL = {
    "ZoomX": 1.0, "ZoomY": 1.0, "Pan": 0.0, "Tilt": 0.0, "RotationAngle": 0.0,
    "AnchorPointX": 0.0, "AnchorPointY": 0.0, "FlipX": False, "FlipY": False,
    "CropLeft": 0.0, "CropRight": 0.0, "CropTop": 0.0, "CropBottom": 0.0,
}
# Read as well, not changed: whether the crop can come in, and the zoom link.
KEYS = tuple(NEUTRAL) + ("ZoomGang", "CropSoftness", "CropRetain")
IDENTITY = {"m": (1.0, 0.0, 0.0, 1.0), "t": (0.0, 0.0), "crop": (0.0, 0.0, 0.0, 0.0)}


def _num(raw, key):
    value = raw.get(key, NEUTRAL.get(key, 0.0))
    return float(value) if isinstance(value, (int, float)) else float(NEUTRAL.get(key, 0.0))


def crop_comes_in(raw):
    """A plain crop is copied into the comp; one that retains the image
    position or has a soft edge stays in the Inspector."""
    return not raw.get("CropRetain") and not _num(raw, "CropSoftness")


def is_neutral(raw, crop=True):
    """No framing to speak of (crop too, if it would come in)."""
    for key, value in NEUTRAL.items():
        if key in CROPS and not (crop and crop_comes_in(raw)):
            continue
        if isinstance(value, bool):
            if bool(raw.get(key)) != value:
                return False
        elif abs(_num(raw, key) - value) > 1e-6:
            return False
    return True


def matrix(zoom_x, zoom_y, angle, flip_x=False, flip_y=False):
    """Flip, zoom, then rotate by `angle` degrees anticlockwise."""
    fx, fy = (-1 if flip_x else 1), (-1 if flip_y else 1)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (c * zoom_x * fx, -s * zoom_y * fy, s * zoom_x * fx, c * zoom_y * fy)


def _times(m, v):
    return (m[0] * v[0] + m[1] * v[1], m[2] * v[0] + m[3] * v[1])


def _mul(p, q):
    return (p[0] * q[0] + p[1] * q[2], p[0] * q[1] + p[1] * q[3],
            p[2] * q[0] + p[3] * q[2], p[2] * q[1] + p[3] * q[3])


def scale_of(m):
    """The overall zoom of a matrix (its area's square root)."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0


def decompose(m):
    """(zoom_x, zoom_y, angle, flip) - flip being a vertical flip of the
    picture before it turns (a horizontal one is that, turned 180). A shear
    - only from re-framing an unevenly zoomed clip at an angle - is dropped."""
    a, b, c, d = m
    angle = math.degrees(math.atan2(c, a))
    zoom_x = math.hypot(a, c) or 1e-9
    zoom_y = (a * d - b * c) / zoom_x
    return zoom_x, abs(zoom_y), angle, zoom_y < 0


def from_inspector(raw, shown, frame):
    """The placement an Inspector's values make. `shown` is the clip's
    source frame as fitted to the timeline (w, h), `frame` the timeline."""
    sx, sy = shown[0] / frame[0], shown[1] / frame[1]
    zoom_x = _num(raw, "ZoomX")
    zoom_y = _num(raw, "ZoomY") if "ZoomY" in raw else zoom_x
    m = matrix(zoom_x, zoom_y, _num(raw, "RotationAngle"), bool(raw.get("FlipX")), bool(raw.get("FlipY")))
    anchor = (_num(raw, "AnchorPointX") * sx, _num(raw, "AnchorPointY") * sy)
    turned = _times(m, anchor)
    t = (_num(raw, "Pan") * sx + anchor[0] - turned[0], _num(raw, "Tilt") * sy + anchor[1] - turned[1])
    crop = tuple(_num(raw, k) for k in CROPS) if crop_comes_in(raw) else (0.0, 0.0, 0.0, 0.0)
    return {"m": m, "t": t, "crop": crop}


def compose(after, before):
    """`before`, then `after` on the finished frame: a clip re-framed in the
    Inspector after Buddy took its framing in. A crop made afterwards is
    taken as a crop of the picture (in its fitted pixels)."""
    m = _mul(after["m"], before["m"])
    moved = _times(after["m"], before["t"])
    t = (after["t"][0] + moved[0], after["t"][1] + moved[1])
    k = scale_of(before["m"])
    crop = tuple(b + a / k for a, b in zip(after["crop"], before["crop"]))
    return {"m": m, "t": t, "crop": crop}


def to_inspector(place, shown, frame):
    """Inspector values that make a placement (for Remove, when there's no
    untouched original to put back)."""
    sx, sy = shown[0] / frame[0], shown[1] / frame[1]
    zoom_x, zoom_y, angle, flip = decompose(place["m"])
    values = {"ZoomGang": abs(zoom_x - zoom_y) < 1e-6, "ZoomX": zoom_x, "ZoomY": zoom_y,
              "RotationAngle": angle, "FlipX": False, "FlipY": flip,
              "AnchorPointX": 0.0, "AnchorPointY": 0.0,
              "Pan": place["t"][0] / sx, "Tilt": place["t"][1] / sy}
    values.update(dict(zip(CROPS, place["crop"])))
    return values


def merge_inputs(place, fit, frame):
    """What the comp's tools get for a placement, the picture fitted by
    `fit`: {"crop" (l, r, t, b in the SOURCE's pixels), "size", "shape"
    (x, y as parts of size, both <= 1), "flip", "angle", "centre" (dx, dy
    as parts of the frame from its middle)}."""
    l, r, t, b = place["crop"]
    shift = _times(place["m"], ((l - r) / 2, (b - t) / 2))     # a crop moves the picture's centre
    zoom_x, zoom_y, angle, flip = decompose(place["m"])
    big = max(zoom_x, zoom_y) or 1.0
    f = fit or 1.0
    return {"crop": (l / f, r / f, t / f, b / f), "size": fit * big,
            "shape": (zoom_x / big, zoom_y / big), "flip": flip, "angle": angle,
            "centre": ((place["t"][0] + shift[0]) / frame[0], (place["t"][1] + shift[1]) / frame[1])}


def dumps(record):
    return json.dumps(record, separators=(",", ":"))


def loads(text):
    try:
        record = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or "place" not in record:
        return None
    place = record["place"]
    record["place"] = {"m": tuple(place["m"]), "t": tuple(place["t"]), "crop": tuple(place["crop"])}
    return record
