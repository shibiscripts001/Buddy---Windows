#!/usr/bin/env python3
"""
Motion presets - the moves Animation's Previews tab shows and puts on clips.

Each preset (motion_presets.json) is five channels sampled 41 times across
`dur` seconds: position, rotation, scale and opacity, the way the approved
Presets Store design plays them. A preset has up to three moves in there -
the In at the start, an Emphasis in the middle, the Out at the end - with
the thing at rest between them. plan() lifts the moves out and lays them on
a clip of any length: the In from the clip's first frame, the Out ending on
its last, an Emphasis where the playhead is. What comes out is Fusion
keyframes for motion_resolve.py to put on a Merge (Center, Size, Angle,
Blend), in the clip's own comp frames: a few eased keys per move fitted to
the samples (fit), not one per sample, so they edit like hand-set keys in
Fusion's spline editor.

No Resolve and no Qt in here: tests/test_motion_presets.py runs it on
plain Python.
"""

import functools
import json
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "motion_presets.json")

SAMPLES = 41                    # 0..40
LAST = SAMPLES - 1

PACKS = [
    {"id": "Essentials", "credit": "Originals"},
    {"id": "Classic", "credit": "From animate.css · MIT"},
    {"id": "Magic", "credit": "From Magic Animations · MIT"},
]
KIND_IN_OUT, KIND_EMPHASIS, KIND_OUT = "In · Out", "Emphasis", "Out"
KINDS = [KIND_IN_OUT, KIND_EMPHASIS, KIND_OUT]

# Which part of an In · Out preset goes on the clip.
WAYS = ("both", "in", "out")
# How fast the move plays: 1 is the preset's own length.
SPEEDS = (0.5, 1.0, 1.5, 2.0)

# A sample counts as moving when it's this far from rest (x/y in the
# preset's half-units, degrees, scale, opacity).
_REST = {"x": 0.0, "y": 0.0, "r": 0.0, "s": 1.0, "o": 1.0}
_EPS = {"x": 0.05, "y": 0.05, "r": 0.05, "s": 0.005, "o": 0.005}
# A move that passes through rest for this many samples (Pop's overshoot)
# is still one move.
_GAP = 3


def load(path=DATA):
    """The presets, in the file's order: dicts with id, label, pack, kind,
    dur (seconds) and the channels x, y, r, s, o (41 floats each)."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)["presets"]
    out = []
    for p in raw:
        t = p["t"]
        channel = lambda values: [float(values[min(k, len(values) - 1)]) for k in range(SAMPLES)]
        out.append({
            "id": p["id"], "label": p["label"], "pack": p["pack"], "kind": p["kind"],
            "dur": float(p["dur"]),
            "x": channel([v[0] for v in t]), "y": channel([v[1] for v in t]),
            "r": channel(p["r"]), "s": channel(p["s"]), "o": channel(p["o"]),
        })
    return out


def moving(preset, k):
    return any(abs(preset[c][k] - _REST[c]) > _EPS[c] for c in _REST)


def segments(preset):
    """[(first, last)] sample ranges where the preset moves, a short pass
    through rest merged into the move around it. A range includes the rest
    sample it lands on (or starts from), so a move ends where it settles."""
    runs, start = [], None
    for k in range(SAMPLES):
        if moving(preset, k):
            if start is None:
                start = max(0, k - 1) if k else 0
        elif start is not None:
            runs.append([start, k])
            start = None
    if start is not None:
        runs.append([start, LAST])
    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= _GAP:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return [tuple(r) for r in merged]


def moves(preset):
    """{"in": (a, b), "emphasis": (a, b), "out": (a, b)} - whichever the
    preset has. An In starts on the first sample, an Out ends on the last;
    anything else is an Emphasis (Hinge, an Out, only has "out")."""
    found = {}
    for a, b in segments(preset):
        if a == 0 and "in" not in found and preset["kind"] == KIND_IN_OUT:
            found["in"] = (a, b)
        elif b == LAST and preset["kind"] in (KIND_IN_OUT, KIND_OUT):
            found["out"] = (a, b)
        elif preset["kind"] == KIND_EMPHASIS:
            # An emphasis preset's moves are one beat, however many runs it's in.
            lo, hi = found.get("emphasis", (a, b))
            found["emphasis"] = (min(lo, a), max(hi, b))
    return found


def view(preset):
    """What the Previews tab draws for a preset: its samples, and where its
    moves are along its length (0..1) for the timing bar."""
    return {
        "id": preset["id"], "label": preset["label"], "pack": preset["pack"],
        "kind": preset["kind"], "dur": preset["dur"],
        "x": preset["x"], "y": preset["y"], "r": preset["r"], "s": preset["s"], "o": preset["o"],
        "segs": [[a / LAST, b / LAST] for a, b in segments(preset)],
    }


def _seconds(preset, a, b):
    return (b - a) / LAST * preset["dur"]


# How far a fitted curve may stray from the design's samples, per channel:
# about 1% of the frame, 2% of scale, 3% of opacity, a degree and a half.
# The aim is the same look with keys a person would set - a long ease, not
# every wobble of 41 samples - so it's loose on purpose.
TOLERANCE = {"Center.X": 0.01, "Center.Y": 0.01, "Size": 0.02, "Angle": 1.5, "Blend": 0.03}
RELATIVE = 0.08                 # ...and at most this share of the move's own range

_CHANNELS = {
    "Center.X": ("x", lambda v: 0.5 + v / 64.0),
    "Center.Y": ("y", lambda v: 0.5 - v / 36.0),
    "Size": ("s", lambda v: max(0.0, v)),
    "Angle": ("r", lambda v: -v),
    "Blend": ("o", lambda v: min(1.0, max(0.0, v))),
}


def _bezier(v0, a, b, v1, u):
    w = 1 - u
    return w * w * w * v0 + 3 * w * w * u * a + 3 * w * u * u * b + u * u * u * v1


# Where a segment's two handles may sit in time, as fractions of it: the
# fit tries each pair. A Fusion (and a CSS cubic-bezier) ease is a curve in
# time as well as value - expo-out bunches its handles at the start - and a
# handle fixed at a third would need extra keys to follow one.
_TIMES = [i / 20 for i in range(1, 20)]


@functools.lru_cache(maxsize=None)
def _params(x1, x2, n):
    """u for each of n+1 evenly spaced times 0..1 on the time curve with
    handles at x1, x2 (bisection: that curve only ever rises)."""
    out = []
    for i in range(n + 1):
        t, lo, hi = i / n, 0.0, 1.0
        for _ in range(30):
            u = (lo + hi) / 2
            w = 1 - u
            if 3 * w * w * u * x1 + 3 * w * u * u * x2 + u * u * u < t:
                lo = u
            else:
                hi = u
        out.append((lo + hi) / 2)
    return tuple(out)


# A turn this sharp - both sides moving at over this share of the move's
# fastest step - is a hit (a bounce on the floor) and keeps its corner; any
# gentler peak or dip gets flat handles, like a key set by hand.
_SHARP = 0.25
_BULGE_STEPS = 24


def _turns(values, tolerance):
    """The ends of a run and the peaks and dips worth a key: a turn whose
    swing to the turns either side of it is under twice `tolerance` (Pop
    settling from 1.01 back to 1) is smoothed over instead."""
    n = len(values) - 1
    turns = [0] + [i for i in range(1, n) if (values[i] - values[i - 1]) * (values[i + 1] - values[i]) < 0] + [n]
    while True:
        small = [(min(abs(values[turns[k]] - values[turns[k - 1]]), abs(values[turns[k]] - values[turns[k + 1]])), k)
                 for k in range(1, len(turns) - 1)]
        small = [(swing, k) for swing, k in small if swing < 2 * tolerance]
        if not small:
            return turns
        turns.pop(min(small)[1])


def _tangents(values, turns):
    """(slope in, slope out) at every sample, per sample step - what a key
    there would get: flat on a peak or dip that's one of `turns`, a corner
    on a sharp hit, the same slope both sides anywhere else (no kink), a
    one-sided estimate at the ends."""
    n = len(values) - 1
    d = [values[i + 1] - values[i] for i in range(n)]
    fastest = max((abs(x) for x in d), default=0.0) or 1.0
    out = []
    for i in range(n + 1):
        if i == 0:
            slope = (-3 * values[0] + 4 * values[1] - values[2]) / 2 if n >= 2 else d[0]
            out.append((slope if slope * d[0] > 0 else 0.0,) * 2)
        elif i == n:
            slope = (3 * values[n] - 4 * values[n - 1] + values[n - 2]) / 2 if n >= 2 else d[-1]
            out.append((slope if slope * d[-1] > 0 else 0.0,) * 2)
        elif i in turns:
            sharp = min(abs(d[i - 1]), abs(d[i])) > _SHARP * fastest
            out.append((d[i - 1], d[i]) if sharp else (0.0, 0.0))
        elif d[i - 1] * d[i] == 0:
            out.append((0.0, 0.0))  # the edge of a flat stretch: level, like a peak
        else:
            slope = (values[i + 1] - values[i - 1]) / 2
            out.append((slope, slope))
    return out


def _segment(values, tangents, i, j, tolerance):
    """The best curve from key i to key j with their slopes fixed - only
    how long each handle is (its time) is free. (worst miss, where, x1, y1,
    x2, y2); a curve that bulges past the samples it spans counts as a
    miss, so none shoots out between two samples."""
    run = values[i:j + 1]
    n = j - i
    v0, v1 = run[0], run[-1]
    s0, s1 = tangents[i][1], tangents[j][0]
    lo, hi = min(run) - tolerance, max(run) + tolerance
    best = None
    for x1 in _TIMES:
        for x2 in _TIMES:
            y1 = v0 + s0 * x1 * n
            y2 = v1 - s1 * (1 - x2) * n
            worst, where = 0.0, None
            if n >= 2:
                us = _params(x1, x2, n)
                for k in range(1, n):
                    off = abs(_bezier(v0, y1, y2, v1, us[k]) - run[k])
                    if off > worst:
                        worst, where = off, k
            for step in range(1, _BULGE_STEPS):
                v = _bezier(v0, y1, y2, v1, step / _BULGE_STEPS)
                over = max(lo - v, v - hi, 0.0) + tolerance if (v < lo or v > hi) else 0.0
                if over > worst:
                    worst, where = over, None
            if best is None or worst < best[0]:
                best = (worst, where, x1, y1, x2, y2)
    return best


@functools.lru_cache(maxsize=None)
def _fit_samples(values, tolerance):
    """fit() on evenly spaced samples, in sample units - cached: a move's
    curve only stretches with the clip, and a Bezier stretches exactly."""
    cuts = _turns(values, tolerance)
    tangents = _tangents(values, set(cuts))
    pieces, todo = [], list(zip(cuts, cuts[1:]))
    while todo:
        i, j = todo.pop()
        worst, where, x1, y1, x2, y2 = _segment(values, tangents, i, j, tolerance)
        if worst > tolerance and j - i >= 2:
            k = i + where if where else (i + j) // 2
            todo += [(i, k), (k, j)]
            continue
        pieces.append((i, j, x1, y1, x2, y2))
    pieces.sort()
    keys = []
    for n, (i, j, x1, y1, x2, y2) in enumerate(pieces):
        if n == 0:
            keys.append([i, values[i], None, None])
        keys[-1][3] = (i + x1 * (j - i), y1)
        keys.append([j, values[j], (i + x2 * (j - i), y2), None])
    return tuple(tuple(k) for k in keys)


def fit(points, tolerance):
    """A few eased keys for a run of evenly spaced (frame, value) samples,
    set the way an animator would: a key where the run starts and ends and
    on every peak and dip (flat handles there, a corner only on a sharp
    hit), more where one curve between two keys can't come within
    `tolerance` of every sample. A key's two handles line up, and a curve
    never bulges past the samples it spans. Returns [(frame, value, left handle or None, right handle or
    None)] - handles as (frame, value), as a Fusion BezierSpline keys them."""
    t0, t1 = points[0][0], points[-1][0]
    step = (t1 - t0) / max(1, len(points) - 1)
    at = lambda i: t0 + i * step
    hand = lambda h: None if h is None else (at(h[0]), h[1])
    values = tuple(round(v, 6) for _t, v in points)
    # Never looser than a fraction of the move itself, or a small move
    # (Shake's ~9 px) would be smoothed away altogether.
    tolerance = min(tolerance, RELATIVE * (max(values) - min(values)) or tolerance)
    return [(at(i), v, hand(lh), hand(rh)) for i, v, lh, rh in _fit_samples(values, tolerance)]


def evaluate(keys, frame):
    """A fitted channel's value at a frame (for tests and checks)."""
    frames = sorted(keys)
    if frame <= frames[0]:
        return keys[frames[0]]["value"]
    for f0, f1 in zip(frames, frames[1:]):
        if f0 <= frame <= f1:
            k0, k1 = keys[f0], keys[f1]
            (h0, a), (h1, b) = k0["rh"] or (f0, k0["value"]), k1["lh"] or (f1, k1["value"])
            x1, x2, t = (h0 - f0) / (f1 - f0), (h1 - f0) / (f1 - f0), (frame - f0) / (f1 - f0)
            lo, hi = 0.0, 1.0
            for _ in range(40):
                u = (lo + hi) / 2
                w = 1 - u
                if 3 * w * w * u * x1 + 3 * w * u * u * x2 + u * u * u < t:
                    lo = u
                else:
                    hi = u
            return _bezier(k0["value"], a, b, k1["value"], (lo + hi) / 2)
    return keys[frames[-1]]["value"]


def plan(preset, clip_frames, fps, way="both", speed=1.0, at=None):
    """Keyframes for one clip: {"Center.X": {frame: key}, "Center.Y", "Size",
    "Angle", "Blend"} in comp frames 0..clip_frames-1, only the channels
    that move; a key is {"value", "lh", "rh"}, its handles (frame, value) or
    None. A few eased keys per move (fit). Plus "moves": [(name, first
    frame, last frame, and the two exactly - where the keys are)].

    way     "both", "in" or "out" - which part of an In · Out preset.
    speed   how fast it plays (2 = half as long).
    at      an Emphasis's first frame in the clip (the playhead); None puts
            it in the middle. Ignored by the others.

    A clip too short for the whole move gets it squeezed to fit; In and Out
    together share the clip in proportion. A one-frame clip has no room for
    a move at all: ValueError."""
    if int(clip_frames) < 2:
        raise ValueError("it's only one frame long - too short for a move")
    end = int(clip_frames) - 1
    rate = float(fps) / max(0.01, float(speed))
    found = moves(preset)
    kind = preset["kind"]
    wanted = []
    if kind == KIND_IN_OUT:
        if way in ("both", "in") and "in" in found:
            wanted.append("in")
        if way in ("both", "out") and "out" in found:
            wanted.append("out")
    elif kind == KIND_EMPHASIS and "emphasis" in found:
        wanted.append("emphasis")
    elif kind == KIND_OUT and "out" in found:
        wanted.append("out")

    lengths = {m: _seconds(preset, *found[m]) * rate for m in wanted}
    squeeze = min(1.0, end / sum(lengths.values())) if lengths and sum(lengths.values()) > end else 1.0

    placed = []                  # (name, a, b, first frame, frames long)
    for m in wanted:
        a, b = found[m]
        length = lengths[m] * squeeze
        if m == "in":
            first = 0.0
        elif m == "out":
            first = end - length
        else:
            first = (end - length) / 2 if at is None else min(max(0.0, float(at)), end - length)
        placed.append((m, a, b, first, length))

    def rounded(h):
        return None if h is None else (round(h[0], 3), round(h[1], 5))

    keys = {}
    for name, (c, value) in _CHANNELS.items():
        used = [k for _m, a, b, _f, _l in placed for k in range(a, b + 1)]
        if all(abs(preset[c][k] - _REST[c]) <= _EPS[c] for k in used):
            continue            # doesn't move in the parts being put on
        channel = {}
        for _m, a, b, first, length in placed:
            points = [(first + (k - a) / max(1, b - a) * length, value(preset[c][k])) for k in range(a, b + 1)]
            for t, v, lh, rh in fit(points, TOLERANCE[name]):
                channel[round(t, 3)] = {"value": round(v, 5), "lh": rounded(lh), "rh": rounded(rh)}
        keys[name] = dict(sorted(channel.items()))
    keys["moves"] = [(m, round(f), round(f + l), round(f, 3), round(f + l, 3)) for m, _a, _b, f, l in placed]
    return keys
