"""The measuring behind Text Animator's placement canvas: how big a Text+ clip's text
renders for a Fusion "Size" (from real font metrics - QtGui, no widgets), bounding-line and
word-layout fits, and the grid / safe-zone overlays. Was placement_canvas.py; its
QGraphicsView canvas is now the web page's (web/canvas.js), which draws what text_box()
measures here."""
import math
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QFontMetrics, QFontMetricsF

from .overlays import GRID_FRACTIONS, GRID_TYPES, SAFE_ZONE_RECTS, SAFE_ZONE_TYPES  # noqa: F401 - re-exported


# How Text+ sizes text: it makes the font's ascent + descent this x Size x composition
# width. Measured in Resolve 21 from rendered bounds (a Text+ tool's Output:GetDoD()) on 7
# fonts from Segoe UI (ascent + descent 1.33 em) to Times New Roman (1.11 em): 0.8024-0.8038
# for all. (The earlier rule - cap height = 0.444 x Size x width, from one Open Sans render -
# was only right for fonts shaped like Open Sans: it drew Arial 14% small.) The web style
# preview uses the same constant (web/animator.js).
TEXT_PLUS_HEIGHT = 0.803

# Fonts are measured at this pixel size and scaled - big enough that whole-pixel rounding
# doesn't matter, and every length scales linearly with it.
_MEASURE_PX = 1000


def _measure_font(font_name: str) -> QFont:
    font = QFont(font_name or "Arial")
    font.setPixelSize(_MEASURE_PX)
    return font


@lru_cache(maxsize=256)
def _px_per_size(font_name: str) -> float:
    """Text+'s font pixel size for Size 1, as a fraction of composition width: the
    TEXT_PLUS_HEIGHT rule solved with this font's own ascent + descent."""
    metrics = QFontMetricsF(_measure_font(font_name))
    height = (metrics.ascent() + metrics.descent()) / _MEASURE_PX
    return TEXT_PLUS_HEIGHT / (height if height > 0 else 1.117)   # 1.117: Arial's, if Qt can't say


def cap_height_fraction(font_name: str, size: float) -> float:
    """How tall a Text+ clip's capital letters draw at `size`, as a fraction of composition
    width."""
    cap = QFontMetricsF(_measure_font(font_name)).capHeight() / _MEASURE_PX
    return max(size, 0.0) * _px_per_size(font_name or "Arial") * cap


def compute_bounding_fit_size(
    font_name: str,
    font_size: float,
    styled_text: str,
    left_frac: float,
    right_frac: float,
) -> Optional[float]:
    """Computes the Fusion "Size" value that makes styled_text's rendered width fill the
    ENTIRE gap between the two Bounding lines (left_frac/right_frac, fractions of composition
    width), so the text fills the bounded space horizontally as much as possible. Returns
    None if the lines are degenerate (right_frac <= left_frac) or the text/font size is
    unusable.

    Sizing text symmetrically around the clip's OWN current center, clamped by whichever of
    the two lines is closer, would let an off-center clip use at most twice its smaller gap,
    leaving the other side's space unused (visibly lopsided). Instead - paired with the
    caller (apply_bounding_to_timeline()) also recentering the clip's Center X to the lines'
    midpoint - this always uses the full width regardless of where the clip started.

    This is a LOCAL Qt font-metrics approximation, the same category as style_preview.py's
    Live Preview - "Size" itself is a real TextPlus input, only the TARGET value fed into it here
    is estimated rather than read from Fusion's own renderer, since there's no scripting
    access to Fusion's real rendered text width for a hypothetical (not-yet-applied) Size."""
    if font_size <= 0 or not styled_text:
        return None

    target_width_fraction = right_frac - left_frac
    if target_width_fraction <= 0:
        return None

    # Width scales linearly with Size, so one measurement at the current Size gives the
    # Size that fills the gap.
    current_width_fraction = _measure_word_width_fraction(font_name, styled_text, font_size)
    if current_width_fraction <= 0:
        return None

    return font_size * (target_width_fraction / current_width_fraction)


# One (key, text, font_name, current_center_y) tuple per word - the shared input shape for
# compute_auto_spaced_row()/compute_large_word_layout() below. `current_center_y` is each
# word's EXISTING Fusion Center.y (0-1) before either function runs - compute_large_word_layout()
# uses it to decide which row a word belongs in.
WordEntry = Tuple[Any, str, str, float]

# Same shape as layout_presets.LayoutTarget (center_x, center_y, size), redeclared locally so
# this module doesn't need to import layout_presets (which is deliberately kept Qt-free) just
# for a type alias.
LayoutTarget = Tuple[float, float, float]

# For the Custom Animation tab's "Auto-Space Words"/"Large Word" buttons - reasonable
# design defaults, same spirit as layout_presets.HERO_SIZE/
# SECONDARY_SIZE/MARGIN (not measured/derived, just sensible starting proportions, easy to
# retune later without touching the layout math itself).
_WORD_ROW_MARGIN = 0.08  # matches layout_presets.MARGIN
_WORD_ROW_GAP_FRACTION = 0.02  # horizontal gap between adjacent words in a row
_LARGE_WORD_HERO_SIZE = 0.28  # bigger than layout_presets.HERO_SIZE (0.16) - meant to read as
# a distinctly larger "emphasis" word, not just another hero+stack-style hero.
_LARGE_WORD_SECONDARY_SIZE = 0.07  # matches layout_presets.SECONDARY_SIZE
# Vertical offset (fraction of composition HEIGHT) from the hero's own row to each secondary
# row. Deliberately NOT derived from _LARGE_WORD_HERO_SIZE's rendered glyph height - Size is a
# fraction of composition WIDTH while this offset is a fraction of
# HEIGHT, and converting between the two needs the composition's actual aspect ratio, which
# isn't available to this pure-math function - so, like layout_presets' own constants, this is
# a flat, reasonable default rather than a precise fit.
_LARGE_WORD_ROW_OFFSET = 0.22


def _measure_word_width_fraction(font_name: str, text: str, size: float) -> float:
    """Given a Fusion "Size" value, what fraction of composition width does `text` render
    at? The forward direction of compute_bounding_fit_size().

    A multi-line Text+ (a literal "\\n" in the text) is as wide as its WIDEST line -
    horizontalAdvance() has no concept of line breaks, and on the whole string would sum
    every line's width into one meaningless, far too wide measurement."""
    if size <= 0 or not text:
        return 0.0
    metrics = QFontMetricsF(_measure_font(font_name))
    widest = max((metrics.horizontalAdvance(line) for line in text.split("\n")), default=0.0)
    return size * _px_per_size(font_name or "Arial") * widest / _MEASURE_PX


def compute_auto_spaced_row(
    words: List[WordEntry], target_size: float, gap_fraction: float = _WORD_ROW_GAP_FRACTION
) -> Dict[Any, LayoutTarget]:
    """"Auto-Space Words" button: lays every word from
    `words` out left-to-right in one row, at a single uniform `target_size` (or smaller, if
    that doesn't fit - see below), spaced by `gap_fraction` between each word's own rendered
    edges, and centered as a group around Center.x = 0.5. Vertically, every word keeps the
    SAME Center.y - the group's own current average - since this button only touches
    horizontal spacing/sizing, never vertical placement.

    If the words' combined width (all widths + all gaps) would overflow the safe row width
    (1.0 - 2*_WORD_ROW_MARGIN), `target_size` is scaled down proportionally (same ratio math
    compute_bounding_fit_size() uses for its own single-string fit) and every word is
    re-measured once at the new size - this keeps the whole row uniformly sized rather than
    letting the row simply run off both edges of frame.

    Returns {key: (center_x, center_y, size)}; empty dict for an empty `words` list."""
    if not words:
        return {}

    size = target_size
    widths = [_measure_word_width_fraction(font_name, text, size) for _key, text, font_name, _y in words]
    total_width = sum(widths) + gap_fraction * max(0, len(words) - 1)

    safe_width = 1.0 - 2 * _WORD_ROW_MARGIN
    if total_width > safe_width > 0:
        # Only the words' own widths shrink with size - the gaps between them are a fixed
        # fraction, not proportional to size - so the scale ratio must be solved from the
        # width-only portion (safe_width minus the fixed gap budget), not from the combined
        # total. Scaling by safe_width/total_width instead would under-correct whenever gaps
        # make up a non-trivial share of the row, leaving the shrunk row still overflowing.
        gap_total = gap_fraction * max(0, len(words) - 1)
        available_for_widths = max(safe_width - gap_total, 0.0)
        widths_total = sum(widths)
        if widths_total > 0:
            size = size * (available_for_widths / widths_total)
        widths = [_measure_word_width_fraction(font_name, text, size) for _key, text, font_name, _y in words]
        total_width = sum(widths) + gap_total

    avg_y = sum(y for _key, _text, _font_name, y in words) / len(words)

    targets: Dict[Any, LayoutTarget] = {}
    cursor_x = 0.5 - total_width / 2.0
    for (key, _text, _font_name, _y), width in zip(words, widths):
        targets[key] = (cursor_x + width / 2.0, avg_y, size)
        cursor_x += width + gap_fraction
    return targets


def compute_large_word_layout(
    hero_key: Any,
    words: List[WordEntry],
    hero_size: float = _LARGE_WORD_HERO_SIZE,
    secondary_size: float = _LARGE_WORD_SECONDARY_SIZE,
    row_offset: float = _LARGE_WORD_ROW_OFFSET,
) -> Dict[Any, LayoutTarget]:
    """"Hero + Stack" for word-by-word subtitles: `hero_key`
    gets enlarged to `hero_size`, centered at frame center. Every OTHER word is split into two
    groups by its position in `words` RELATIVE TO THE HERO'S OWN POSITION THERE - `words` must
    already be in reading order (e.g. sorted by clip start time), and words that come BEFORE
    the hero in that order stack above it, words that come AFTER stack below (each group
    packed into its own row via compute_auto_spaced_row(), offset `row_offset` from center).

    Deliberately based on word ORDER, not each word's current on-screen Y: with "This is a
    test" auto-spaced into one row (so every word
    starts at roughly the same Y), picking "is" as the hero must still put "This" above it and
    "a"/"test" below, matching how a reader would expect the sentence to split - not group by
    incidental current Y, which is ambiguous/wrong once every word starts on the same row.

    Returns {key: (center_x, center_y, size)} for the hero and every word in `words` sharing
    its key with a member of `words` (or just the hero alone if `hero_key` isn't found)."""
    hero_index = next((i for i, entry in enumerate(words) if entry[0] == hero_key), None)
    if hero_index is None:
        return {}

    above = words[:hero_index]
    below = words[hero_index + 1:]

    hero_y = 0.5
    targets: Dict[Any, LayoutTarget] = {hero_key: (0.5, hero_y, hero_size)}

    for group, offset in ((above, row_offset), (below, -row_offset)):
        if not group:
            continue
        # Re-center each word's own y at the hero's row before measuring, so
        # compute_auto_spaced_row()'s "average y" output lands exactly on hero_y + offset
        # regardless of how spread out the group's original y values were.
        row_words = [(key, text, font_name, hero_y) for key, text, font_name, _y in group]
        row_targets = compute_auto_spaced_row(row_words, secondary_size)
        for key, (center_x, center_y, size) in row_targets.items():
            targets[key] = (center_x, center_y + offset, size)

    return targets


def compute_multiline_ink_rect(font: QFont, text: str) -> QRectF:
    """Measures the actual glyph-ink bounding box of (possibly multi-line, "\\n"-separated)
    `text` in `font`, in the SAME local coordinate frame QGraphicsSimpleTextItem itself uses -
    origin (0, 0) at the top-left of the item's full font box, NOT QFontMetrics' own
    baseline-relative convention (getting that distinction wrong makes the box land nowhere
    near the rendered text).

    QFontMetrics.tightBoundingRect() only understands a single line - fed a multi-line string
    directly, it measures totally wrong text (empirically, close to the SUM of every line's
    own width, and only one line's worth of height) rather than raising or refusing. This
    measures each line
    separately and reproduces the same top-to-bottom stacking QGraphicsSimpleTextItem itself
    uses (each line's baseline `metrics.lineSpacing()` pixels below the previous one, first
    line's baseline at `metrics.ascent()`), then returns the union of every line's ink rect in
    that shared frame - collapses to the exact single-line-correct behavior when `text` has no
    newline."""
    metrics = QFontMetrics(font)
    ascent = metrics.ascent()
    line_spacing = metrics.lineSpacing()

    lefts, rights, tops, bottoms = [], [], [], []
    for i, line in enumerate(text.split("\n")):
        tight = metrics.tightBoundingRect(line or " ")
        baseline = ascent + i * line_spacing
        lefts.append(tight.left())
        rights.append(tight.left() + tight.width())
        tops.append(baseline + tight.top())
        bottoms.append(baseline + tight.top() + tight.height())

    left, top = min(lefts), min(tops)
    return QRectF(left, top, max(rights) - left, max(bottoms) - top)


# One measurement, reused: every length text_box() returns scales linearly with Size, so
# fonts/texts are measured once at this Size and scaled.
_MEASURE_SIZE = 1.0


@lru_cache(maxsize=512)
def _text_metrics(font_name: str, text: str) -> Tuple[float, ...]:
    """(pixel size, ink left, ink top, ink width, ink height, ascent, line spacing), all
    as fractions of composition width, for `text` at Size _MEASURE_SIZE."""
    font = _measure_font(font_name)
    metrics = QFontMetrics(font)
    ink = compute_multiline_ink_rect(font, text or " ")
    k = _MEASURE_SIZE * _px_per_size(font_name) / _MEASURE_PX   # measured px -> composition width
    return (_MEASURE_PX * k, ink.left() * k, ink.top() * k, ink.width() * k, ink.height() * k,
            metrics.ascent() * k, metrics.lineSpacing() * k)


def text_box(font_name: str, text: str, size: float) -> Dict[str, float]:
    """How a Text+ clip's text draws, for the web canvas: font pixel size, the ink box
    (left/top relative to the text origin, width/height) and ascent/line spacing - each a
    fraction of composition width, so the page multiplies by its canvas width. The same
    Qt-measured glyph-height and ink-rect maths the old QGraphicsView canvas drew with."""
    scale = max(size, 0.0) / _MEASURE_SIZE
    px, left, top, width, height, ascent, line = _text_metrics(font_name or "Arial", text or " ")
    return {"px": px * scale, "left": left * scale, "top": top * scale, "w": width * scale,
            "h": height * scale, "ascent": ascent * scale, "line": line * scale}


def standard_grid_spacing(width: float, height: float) -> float:
    """The "Standard" grid's default cell, as a fraction of composition width: the GCD of
    the real resolution - the largest square cell that tiles the frame evenly (1920x1080 ->
    120 px, a 16x9 grid) - kept between 1/64 and 1/4 of the width."""
    if width <= 0 or height <= 0:
        return 1 / 16
    gcd = math.gcd(int(width), int(height)) or 1
    return max(1 / 64, min(1 / 4, gcd / width))
