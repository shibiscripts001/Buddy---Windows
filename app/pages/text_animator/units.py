#!/usr/bin/env python3
"""
Animate by - a Previews preset played line by line, word by word or letter by
letter on one Text+, each unit a stagger after the one before. The timing
here; motion_resolve.apply_units puts it on the clip.

How it plays in Fusion (measured on Studio 21.1, 2026-10-02): a text Follower
on the Text+ animates each character at its own delay. Its Word* and Line*
inputs move each word or line as one piece, but its Delay counts characters,
so a long word holds the next one back. With Order on Manual Curve (stored as
6 - the dropdown's numbers aren't in the order it shows them) the delays come
from a curve over the character positions instead: one key per character,
its value that character's delay in frames. Every character of a unit gets
the unit's delay, so units start evenly whatever their length, and a fade
fades each unit whole. A space or line break takes the next unit's delay.

The Follower plays every unit's keys shifted by its delay, the Out's too. So
the keys are planned for a clip shorter by the longest delay (plan_frames),
and the last unit's Out still ends on the clip's last frame.

No Resolve and no Qt in here: tests/test_motion_units.py runs it on plain
Python.
"""

import math
import random
import zlib

CLIP, LINES, WORDS, LETTERS = "clip", "lines", "words", "letters"
UNITS = (CLIP, LINES, WORDS, LETTERS)
# The Follower's inputs for each unit: Line*, Word* or Character*.
LEVEL = {LINES: "Line", WORDS: "Word", LETTERS: "Character"}

FORWARD, REVERSE, MIDDLE, EDGES, RANDOM = "forward", "reverse", "middle", "edges", "random"
ORDERS = (FORWARD, REVERSE, MIDDLE, EDGES, RANDOM)

# Seconds from one unit to the next.
STAGGERS = (0.03, 0.05, 0.08, 0.12, 0.2, 0.3)
DEFAULT_STAGGER = 0.08

# The stagger's share of the clip, at most: past it, every delay shrinks
# alike, so the units still come in order with room left for the move.
MAX_SHARE = 0.5

_SPACE = " \t\r\n "


def unit_of(text, unit):
    """Each character's unit number (0 up), for LINES, WORDS or LETTERS.
    Spaces and line breaks take the number of the unit after them - or the
    last unit's, at the end - so they never hold a unit back. Lines with
    nothing on them are no unit."""
    marks, n, inside = [], -1, False
    for ch in text:
        if unit == LINES:
            if ch == "\n":
                inside = False
                marks.append(None)
                continue
            if ch in _SPACE:
                marks.append(None)
                continue
            if not inside:
                n, inside = n + 1, True
        elif ch in _SPACE:
            inside = False
            marks.append(None)
            continue
        elif unit == LETTERS or not inside:
            n, inside = n + 1, True
        marks.append(n)
    # Whitespace: the next unit's number, or the last one's.
    following = n if n >= 0 else 0
    for i in range(len(marks) - 1, -1, -1):
        if marks[i] is None:
            marks[i] = following
        else:
            following = marks[i]
    return marks


def count(text, unit):
    """How many units the text has."""
    marks = unit_of(text, unit)
    return max(marks) + 1 if marks else 0


def ranks(n, order, seed=0):
    """When each of n units starts, as steps from the first: 0, 1, 2... FORWARD
    is first to last; MIDDLE starts at the middle and works out, units the
    same distance from it together, EDGES the other way; RANDOM is the same
    shuffle every time for the same seed (the text)."""
    if n <= 0:
        return []
    if order == REVERSE:
        return [n - 1 - i for i in range(n)]
    if order in (MIDDLE, EDGES):
        middle = (n - 1) / 2
        distance = [abs(i - middle) for i in range(n)]
        steps = sorted(set(distance), reverse=order == EDGES)
        return [steps.index(d) for d in distance]
    if order == RANDOM:
        shuffled = list(range(n))
        random.Random(seed).shuffle(shuffled)
        return [shuffled.index(i) for i in range(n)]
    return list(range(n))


def seed_of(text):
    """RANDOM's seed: the same text shuffles the same way on every Apply."""
    return zlib.crc32(text.encode("utf-8"))


def delays(text, unit, order, step):
    """Each character's delay in frames, `step` frames from one unit to the
    next in `order` - the values for the Follower's Delay by Character
    Position curve."""
    marks = unit_of(text, unit)
    if not marks:
        return []
    when = ranks(max(marks) + 1, order, seed_of(text))
    return [when[m] * step for m in marks]


def fitted(delay_list, frames):
    """The delays, shrunk alike if the longest is past MAX_SHARE of the clip
    (frames long) - a long sentence on a short clip still gets its move."""
    longest = max(delay_list, default=0.0)
    room = max(0.0, (frames - 1) * MAX_SHARE)
    if longest <= room or longest <= 0:
        return list(delay_list)
    return [d * room / longest for d in delay_list]


def plan_frames(frames, delay_list):
    """How long one unit's keys may run, so the last unit, delayed the most,
    still ends on the clip's last frame."""
    return max(2, int(frames - math.ceil(max(delay_list, default=0.0))))


def clean_options(raw):
    """Animate by's choices from settings or the page, made safe:
    {"unit", "order", "stagger"}."""
    raw = raw if isinstance(raw, dict) else {}
    unit = raw.get("unit") if raw.get("unit") in UNITS else CLIP
    order = raw.get("order") if raw.get("order") in ORDERS else FORWARD
    try:
        stagger = float(raw.get("stagger"))
    except (TypeError, ValueError):
        stagger = DEFAULT_STAGGER
    if stagger not in STAGGERS:
        stagger = DEFAULT_STAGGER
    return {"unit": unit, "order": order, "stagger": stagger}
