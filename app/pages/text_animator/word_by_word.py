"""Word-by-word: single-word Text+ clips set out as if they were one Text+ clip showing
the sentence - the Word-by-word tab's Space words (text_plus.py). No Resolve, no widgets:
the measuring is canvas_math's, the same model the placement canvas draws with.

How one Text+ clip lays out a sentence (canvas_math.text_box, measured in Resolve 21.1):
each line is centred on its own advance width, words a space's advance apart; the first
line's baseline is the face's baseline() below the Center, and further lines step down by
ascent + descent x LineSpacing - 0.800 x Size x LineSpacing of the frame width, whatever
the face (TEXT_PLUS_HEIGHT). Here every word is its own clip, so each gets the Center
that puts its text where the sentence's clip would have drawn it. Words of different
fonts or sizes keep theirs: gaps are the space of the word before, lines step by the
biggest word's, and each word sits on its line's baseline.

Where: the words' own group stays where it is - its box's middle across, the middle of
its Centers down - so a second Space words changes nothing.

Word spacing (the tab's slider) scales every gap: 1 is the space itself, 0 has the words
touching, 2 twice as wide. Line breaks and Auto's fit count the gaps as they'll be.
"""

from itertools import combinations
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import canvas_math

MAX_LINES = 3
# "Auto": the fewest lines whose widest fits this share of the frame's width.
FIT_WIDTH = 0.9
LINE_CHOICES = ("auto", "1", "2", "3")


class Word(NamedTuple):
    key: Any
    text: str
    font: str
    style: str
    size: float
    line_spacing: float
    cx: float           # Center, as a fraction of frame width
    cy: float           # Center, as a fraction of frame height, top-down


class Layout(NamedTuple):
    centers: Dict[Any, Tuple[float, float]]    # key -> (cx, cy), the fractions Word uses
    lines: List[List[Any]]                     # the keys on each line
    widest: float                              # the widest line, as a fraction of frame width


def is_single_word(text: Optional[str]) -> bool:
    """A clip Space words takes: one word, nothing around it but spaces."""
    text = (text or "").strip()
    return bool(text) and not any(c.isspace() for c in text)


def _box(word: Word) -> dict:
    return canvas_math.text_box(word.font, word.text.strip(), word.size, word.style, word.line_spacing)


def _space(word: Word) -> float:
    """A space's advance in this word's font and size, as a fraction of frame width."""
    family, style = canvas_math.face(word.font, word.style)
    return canvas_math.text_box(family, "x x", word.size, style)["w"] - 2 * canvas_math.text_box(family, "x", word.size, style)["w"]


def _step(word: Word) -> float:
    spacing = word.line_spacing if word.line_spacing and word.line_spacing > 0 else 1.0
    return canvas_math.TEXT_PLUS_HEIGHT * max(word.size, 0.0) * spacing


def _split(widths: List[float], spaces: List[float], lines: int) -> Tuple[float, List[Tuple[int, int]]]:
    """The words into `lines` runs, in order, with the widest as narrow as it can be: (its
    width, [(first, end)] per line). Ties go to the split with more words on top."""
    n = len(widths)

    def width(i, j):
        return sum(widths[i:j]) + sum(spaces[i:j - 1])

    best = None
    for cuts in combinations(range(1, n), lines - 1):
        bounds = list(zip((0,) + cuts, cuts + (n,)))
        widest = max(width(i, j) for i, j in bounds)
        if best is None or widest < best[0] - 1e-12:
            best = (widest, bounds)
    return best


def layout(words: List[Word], lines: str = "auto", aspect: float = 16 / 9,
           spacing: float = 1.0) -> Optional[Layout]:
    """Where each of `words` (in reading order) goes. `lines`: "auto" or "1".."3" (never
    more than there are words); `aspect`: the frame's width / height; `spacing`: the gaps
    between words, in spaces. None for no words."""
    if not words:
        return None
    boxes = [_box(w) for w in words]
    widths = [b["w"] for b in boxes]
    spaces = [_space(w) * max(0.0, float(spacing)) for w in words]

    most = min(MAX_LINES, len(words))
    if lines in ("1", "2", "3"):
        count = min(int(lines), most)
        widest, bounds = _split(widths, spaces, count)
    else:
        for count in range(1, most + 1):
            widest, bounds = _split(widths, spaces, count)
            if widest <= FIT_WIDTH:
                break

    # Where the group is now: its boxes' middle across, its Centers' middle down.
    lefts = [w.cx + b["left"] for w, b in zip(words, boxes)]
    rights = [left + b["w"] for left, b in zip(lefts, boxes)]
    mid_x = (min(lefts) + max(rights)) / 2
    mid_y = (min(w.cy for w in words) + max(w.cy for w in words)) / 2

    # The biggest word sets the line step and the baseline, as one clip's font would.
    lead = max(range(len(words)), key=lambda i: words[i].size)
    step, first_baseline = _step(words[lead]), boxes[lead]["lines"][0][1]

    centers, keys = {}, []
    for row, (i, j) in enumerate(bounds):
        line_width = sum(widths[i:j]) + sum(spaces[i:j - 1])
        baseline = first_baseline + (row - (len(bounds) - 1) / 2) * step       # from mid_y, frame widths, down
        x = mid_x - line_width / 2
        for k in range(i, j):
            word, box = words[k], boxes[k]
            centers[word.key] = (x - box["left"], mid_y + (baseline - box["lines"][0][1]) * aspect)
            x += widths[k] + (spaces[k] if k < j - 1 else 0.0)
        keys.append([w.key for w in words[i:j]])
    return Layout(centers, keys, widest)


def stack_plan(clips, chosen, playhead: int) -> Dict[Tuple[int, int], Tuple[int, int]]:
    """Where Stack puts each chosen clip: {(video track, start): (new track, frames to
    the playhead)}. `clips`: (track, start, end) for every video clip on the timeline,
    `chosen` among them. In the order they start, each goes on the lowest track from its
    own up that nothing else uses between its start and the playhead - the other clips
    where they are, the chosen ones already placed - so no two share a track."""
    chosen = sorted(set(chosen), key=lambda c: (c[1], c[0]))
    picked = set(chosen)
    taken: Dict[int, list] = {}
    for track, start, end in clips:
        if (track, start, end) not in picked:
            taken.setdefault(track, []).append((start, end))
    plan = {}
    for track, start, _end in chosen:
        new = track
        while any(a < playhead and start < b for a, b in taken.get(new, [])):
            new += 1
        taken.setdefault(new, []).append((start, playhead))
        plan[(track, start)] = (new, playhead - start)
    return plan
