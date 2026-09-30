"""The measuring behind the Text+ placement canvas: how big a Text+ clip's text renders for
a Fusion "Size" and where it sits around its Center (font files' own metrics, QtGui for
glyph widths - no widgets - and each face's baseline as Resolve measured it,
text_measure.py), bounding-line and word-layout fits, and the grid / safe-zone
overlays. Was placement_canvas.py; its QGraphicsView canvas is now the web page's
(transcribe/web/canvas.js), which draws what text_box() measures here."""
import math
import struct
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtGui import QFont, QFontMetricsF

from .overlays import GRID_FRACTIONS, GRID_TYPES, SAFE_ZONE_RECTS, SAFE_ZONE_TYPES  # noqa: F401 - re-exported


# How Text+ sizes text: it makes the font's ascent + descent this x Size x composition
# width. Measured in Resolve 21.1 (2026-09-30) on 10 faces from Calibri (ascent + descent
# 1.0 em) to Comic Sans MS (1.39 em), Arial Bold among them, from a Text+ tool's
# Output:GetDoD(): the width "HHHH" adds over "H" - three advances, so the 2 px DoD adds to
# each edge cancels out - gave 0.7987-0.8010. (0.803, the figure before, came from rendered
# heights, which carry that margin.) The line step is the same ascent + descent, with no
# line gap (to 0.4%, same 10 faces), times the tool's LineSpacing. The web style preview
# uses the same constant (transcribe/web/textplus.js).
TEXT_PLUS_HEIGHT = 0.800

# Where the baseline sits: the lines' block is centred on the Center and the baseline is a
# fixed distance below it for each face - the same for "H", "xg" or "Ty." (to 0.005 em) -
# but a distance no table in the font gives: 0.35-0.40 em across the 14 faces measured,
# where (ascent - descent) / 2 is off by up to 0.17 em (Gabriola). So Resolve is asked, once
# per face (text_measure.py) and set here (set_baselines); a face not yet measured uses
# BASELINE_FALLBACK, the middle of that range (within 0.025 em of all 14).
BASELINE_FALLBACK = 0.375
_baselines: Dict[Tuple[str, str], float] = {}     # (family, style) -> ems below the Center

# WHICH ascent and descent: the font file's hhea table - FreeType's, which Fusion renders
# with - not the Windows metrics (OS/2 usWin) Qt and the browser use on Windows. For most
# fonts they're the same, but not all: Windows' own Noto Sans JP (NotoSansJP-VF.ttf) has
# hhea 1.0 + 0.2 em and usWin 1.16 + 0.288, and Resolve drew it 1.448 / 1.2 = 21% bigger
# than Qt's figures said - measured on 16 words, all within the 2-3 px edge the rendered
# bounds add. The same bounds showed how Text+ lays text out around its Center (text_box).
# The file is the one Resolve's own FontManager names (set_font_files), since a family can
# be installed several times over with different metrics (that one three times here).
_font_files: Dict[str, Dict[str, str]] = {}      # family -> style -> font file, from Resolve

# Fonts are measured at this pixel size and scaled - big enough that whole-pixel rounding
# doesn't matter, and every length scales linearly with it.
_MEASURE_PX = 1000


def set_font_files(font_list) -> bool:
    """Takes Resolve's FontManager.GetFontList() ({family: {style: file}}), so fonts are
    measured from the files Resolve renders them with. True if that changed anything."""
    files = {str(family): {str(style): str(path) for style, path in styles.items()}
             for family, styles in (font_list or {}).items() if isinstance(styles, dict)}
    if not files or files == _font_files:
        return False
    _font_files.clear()
    _font_files.update(files)
    _px_per_size.cache_clear()
    _text_metrics.cache_clear()
    return True


def face(font_name: Optional[str], style: Optional[str] = None) -> Tuple[str, str]:
    """(family, style) - what a Text+ clip's Font and Style inputs name."""
    return (font_name or "Arial", style or "Regular")


def font_file(font_name: Optional[str], style: Optional[str] = None) -> Optional[str]:
    """The file Resolve renders this face from: its style's, else the family's Regular,
    else any of its styles."""
    family, style = face(font_name, style)
    styles = _font_files.get(family) or {}
    return styles.get(style) or styles.get("Regular") or next(iter(styles.values()), None)


def set_baselines(values: Dict[Tuple[str, str], float]) -> bool:
    """Takes baselines Resolve measured ({(family, style): ems below the Center}, see
    text_measure.py). True if that changed anything."""
    new = {face(*key): float(v) for key, v in (values or {}).items() if 0.0 < float(v) < 1.5}
    if all(_baselines.get(key) == v for key, v in new.items()):
        return False
    _baselines.update(new)
    return True


def baseline(font_name: Optional[str], style: Optional[str] = None) -> float:
    """How far below a Text+ Center a one-line clip's baseline sits, in ems."""
    return _baselines.get(face(font_name, style), BASELINE_FALLBACK)


def has_baseline(font_name: Optional[str], style: Optional[str] = None) -> bool:
    return face(font_name, style) in _baselines


def _sfnt_tables(data: bytes) -> Dict[str, bytes]:
    """The tables of a .ttf/.otf, or of the first font in a .ttc collection."""
    offset = struct.unpack(">I", data[12:16])[0] if data[:4] == b"ttcf" else 0
    count = struct.unpack(">H", data[offset + 4:offset + 6])[0]
    tables = {}
    for i in range(count):
        entry = offset + 12 + 16 * i
        tag, _checksum, start, length = struct.unpack(">4sIII", data[entry:entry + 16])
        tables[tag.decode("latin-1")] = data[start:start + length]
    return tables


@lru_cache(maxsize=128)
def _file_metrics(path: str) -> Optional[Tuple[float, float, float]]:
    """(ascent, descent, line gap) in ems from a font file's hhea table - OS/2's typo
    figures if hhea has none - or None if it can't be read."""
    try:
        with open(path, "rb") as f:
            tables = _sfnt_tables(f.read())
        units = struct.unpack(">H", tables["head"][18:20])[0]
        ascent, descent, gap = struct.unpack(">hhh", tables["hhea"][4:10])
        if ascent - descent <= 0 and len(tables.get("OS/2", b"")) >= 74:
            ascent, descent, gap = struct.unpack(">hhh", tables["OS/2"][68:74])
    except (OSError, KeyError, struct.error, UnicodeDecodeError):
        return None
    if units <= 0 or ascent - descent <= 0:
        return None
    return ascent / units, -descent / units, max(gap, 0) / units


def font_vertical_metrics(font_name: str, style: Optional[str] = None) -> Tuple[float, float, float]:
    """(ascent, descent, line gap) in ems, as Text+ sees them: from the file Resolve uses
    for this face (font_file), else Qt's figures for it."""
    path = font_file(font_name, style)
    found = _file_metrics(path) if path else None
    if found:
        return found
    metrics = QFontMetricsF(_measure_font(font_name, style))
    ascent, descent = metrics.ascent() / _MEASURE_PX, metrics.descent() / _MEASURE_PX
    if ascent + descent <= 0:
        return 0.905, 0.212, 0.033          # Arial's, if Qt can't say
    return ascent, descent, max(metrics.leading(), 0.0) / _MEASURE_PX


def _measure_font(font_name: str, style: Optional[str] = None) -> QFont:
    """The face as Qt shapes it - kerned, as Text+ is: Qt's widths, style and kerning
    included, came within 2 px of Resolve's on the same 10 faces (Arial's "AVAVAVAV" is
    60 px narrower than "AAAAVVVV" there, and here)."""
    font = QFont(font_name or "Arial")
    if style:
        font.setStyleName(style)
    font.setPixelSize(_MEASURE_PX)
    font.setKerning(True)
    return font


@lru_cache(maxsize=256)
def _px_per_size(font_name: str, style: Optional[str] = None) -> float:
    """Text+'s font pixel size for Size 1, as a fraction of composition width: the
    TEXT_PLUS_HEIGHT rule solved with this face's ascent + descent."""
    ascent, descent, _gap = font_vertical_metrics(font_name, style)
    return TEXT_PLUS_HEIGHT / (ascent + descent)


def cap_height_fraction(font_name: str, size: float, style: Optional[str] = None) -> float:
    """How tall a Text+ clip's capital letters draw at `size`, as a fraction of composition
    width."""
    cap = QFontMetricsF(_measure_font(font_name, style)).capHeight() / _MEASURE_PX
    return max(size, 0.0) * _px_per_size(font_name or "Arial", style) * cap


def glyph_extent(font_name: str, text: str, style: Optional[str] = None) -> Tuple[float, float]:
    """(how far the text's ink reaches above its baseline, how far below), in ems - what
    text_measure.py needs to find the baseline in Resolve's rendered bounds."""
    ink = QFontMetricsF(_measure_font(font_name, style)).tightBoundingRect(text)
    return -ink.top() / _MEASURE_PX, ink.bottom() / _MEASURE_PX


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


def _measure_word_width_fraction(font_name: str, text: str, size: float, style: Optional[str] = None) -> float:
    """Given a Fusion "Size" value, what fraction of composition width does `text` render
    at? The forward direction of compute_bounding_fit_size().

    A multi-line Text+ (a literal "\\n" in the text) is as wide as its WIDEST line -
    horizontalAdvance() has no concept of line breaks, and on the whole string would sum
    every line's width into one meaningless, far too wide measurement."""
    if size <= 0 or not text:
        return 0.0
    metrics = QFontMetricsF(_measure_font(font_name, style))
    widest = max((metrics.horizontalAdvance(line) for line in text.split("\n")), default=0.0)
    return size * _px_per_size(font_name or "Arial", style) * widest / _MEASURE_PX


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


# One measurement, reused: every length text_box() returns scales linearly with Size, so
# fonts/texts are measured once at this Size and scaled.
_MEASURE_SIZE = 1.0


@lru_cache(maxsize=512)
def _text_metrics(font_name: str, style: str, text: str, line_spacing: float, below: float) -> Tuple[Any, ...]:
    """(pixel size, box left, box top, box width, box height, ((x, baseline) per line)) at
    Size _MEASURE_SIZE, as fractions of composition width, relative to the Text+ Center
    (y down). `below`: the face's baseline() - an argument so a newly measured one is
    never answered from the cache. See text_box()."""
    metrics = QFontMetricsF(_measure_font(font_name, style))
    ascent, descent, _gap = (v * _MEASURE_PX for v in font_vertical_metrics(font_name, style))
    lines = (text or " ").split("\n")
    step = (ascent + descent) * line_spacing
    first = below * _MEASURE_PX - (len(lines) - 1) * step / 2          # the lines, centred on the one line's place
    origins, widths = [], []
    for i, line in enumerate(lines):
        width = metrics.horizontalAdvance(line)
        widths.append(width)
        origins.append((-width / 2, first + i * step))                 # each line centred on its own width
    k = _MEASURE_SIZE * _px_per_size(font_name, style) / _MEASURE_PX   # measured px -> composition width
    widest = max(widths)
    top, bottom = first - ascent, origins[-1][1] + descent
    return (_MEASURE_PX * k, -widest / 2 * k, top * k, widest * k, (bottom - top) * k,
            tuple((x * k, y * k) for x, y in origins))


def text_box(font_name: str, text: str, size: float, style: Optional[str] = None,
             line_spacing: float = 1.0) -> Dict[str, Any]:
    """How a Text+ clip's text draws, for the web canvas: the font's pixel size, the line
    box (left/top from the clip's Center, y down, and width/height) and each line's origin
    (x and baseline, from the Center) - fractions of composition width, so the page
    multiplies by its canvas width.

    The box is the lines' own, not their ink's: from the first baseline up by the font's
    ascent to the last baseline down by its descent, and as wide as the widest line's
    advance. So it's the same height for "use", "happy" and "I" - two words of one face
    and size on one baseline have boxes that line up exactly, top and bottom, and two
    words set side by side a space apart are a sentence. The ink stays inside it (a
    font's ascent and descent are drawn to hold its letters).

    Laid out as Text+ does it (measured in Resolve, see TEXT_PLUS_HEIGHT and
    BASELINE_FALLBACK): one line's baseline is baseline() below the Center; further
    lines step by ascent + descent x LineSpacing, the lines centred on where the one
    would be; each line centred on its own width."""
    scale = max(size, 0.0) / _MEASURE_SIZE
    family, style = face(font_name, style)
    spacing = float(line_spacing) if isinstance(line_spacing, (int, float)) and line_spacing > 0 else 1.0
    px, left, top, width, height, lines = _text_metrics(family, style, text or " ", spacing, baseline(family, style))
    return {"px": px * scale, "left": left * scale, "top": top * scale, "w": width * scale,
            "h": height * scale, "lines": [[x * scale, y * scale] for x, y in lines]}


def standard_grid_spacing(width: float, height: float) -> float:
    """The "Standard" grid's default cell, as a fraction of composition width: the GCD of
    the real resolution - the largest square cell that tiles the frame evenly (1920x1080 ->
    120 px, a 16x9 grid) - kept between 1/64 and 1/4 of the width."""
    if width <= 0 or height <= 0:
        return 1 / 16
    gcd = math.gcd(int(width), int(height)) or 1
    return max(1 / 64, min(1 / 4, gcd / width))
