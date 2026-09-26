"""Multi-text layout math for the Live Placement Preview - pure math only, no Resolve/Qt
dependencies.

LAYOUT_PRESETS ("Hero + Stack"/"Side-by-Side"/"Grid") arrange several Text+ items
currently visible under DaVinci's playhead into a designed pattern (one hero/featured item
plus supporting items), mimicking apps like Easy Motion - callers pass an item count and get
back a list of (center_x, center_y, size) targets in Fusion's own normalized units (Center:
0-1, Size: fraction of composition width), in the SAME order as
the input items, with index 0 always the hero's target.

compute_vertical_stack_centers() is a separate, hero-less helper for the
"Bounding" feature - vertically snapping a group of simultaneously-visible items together.

group_overlapping_clips() is a third, unrelated helper for the same feature -
finding EVERY group of temporally-overlapping clips across a whole timeline (not just
whatever's under the playhead right now), so "Bounding"'s vertical snapping can genuinely
apply everywhere a stack exists, matching how its font-size fit already scans every clip.
"""
import math
from typing import Any, Callable, Dict, List, Tuple

LayoutTarget = Tuple[float, float, float]  # (center_x, center_y, size)

# Reasonable starting proportions for a text hierarchy - not measured/derived like
# placement_canvas._SIZE_TO_WIDTH_GLYPH_HEIGHT_RATIO, just ordinary design defaults, easy to
# retune later without touching the layout math itself.
HERO_SIZE = 0.16
SECONDARY_SIZE = 0.07
MARGIN = 0.08


def hero_stack(count: int) -> List[LayoutTarget]:
    """One large hero near the top third; remaining items stacked smaller below it, evenly
    spaced and centered horizontally."""
    if count <= 0:
        return []

    targets: List[LayoutTarget] = [(0.5, 0.72, HERO_SIZE)]
    secondary_count = count - 1
    if secondary_count <= 0:
        return targets

    top, bottom = 0.5, MARGIN
    if secondary_count == 1:
        ys = [(top + bottom) / 2]
    else:
        step = (top - bottom) / (secondary_count - 1)
        ys = [top - i * step for i in range(secondary_count)]

    targets.extend((0.5, y, SECONDARY_SIZE) for y in ys)
    return targets


def side_by_side(count: int) -> List[LayoutTarget]:
    """One evenly-spaced horizontal row, all the same size. The hero (index 0) occupies the
    middle slot in the row rather than an end, so it reads as the visual center of attention."""
    if count <= 0:
        return []

    left, right = MARGIN, 1.0 - MARGIN
    if count == 1:
        xs = [0.5]
    else:
        step = (right - left) / (count - 1)
        xs = [left + i * step for i in range(count)]

    # Build the row left-to-right with the hero (original index 0) placed in the middle slot,
    # everyone else filling the remaining slots in their original relative order.
    row_order = list(range(1, count))
    mid = len(row_order) // 2
    row_order.insert(mid, 0)

    targets: List[LayoutTarget] = [None] * count
    for slot, original_index in enumerate(row_order):
        targets[original_index] = (xs[slot], 0.5, SECONDARY_SIZE)
    return targets


def grid(count: int) -> List[LayoutTarget]:
    """A roughly square grid, evenly spaced and uniformly sized - no hero emphasis, since a
    grid implies a collection without hierarchy."""
    if count <= 0:
        return []

    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)

    left, right = MARGIN, 1.0 - MARGIN
    top, bottom = 1.0 - MARGIN, MARGIN
    col_step = (right - left) / (cols - 1) if cols > 1 else 0.0
    row_step = (top - bottom) / (rows - 1) if rows > 1 else 0.0

    targets: List[LayoutTarget] = []
    for i in range(count):
        r, c = divmod(i, cols)
        x = left + c * col_step if cols > 1 else 0.5
        y = top - r * row_step if rows > 1 else 0.5
        targets.append((x, y, SECONDARY_SIZE))
    return targets


def compute_vertical_stack_centers(items: List[Tuple[Any, float, float]]) -> Dict[Any, float]:
    """Given [(key, current_center_y, height), ...] for a group of Text+ items "stacked
    together" (multiple clips simultaneously visible under the playhead, on different video
    tracks), returns {key: new_center_y} that arranges them
    contiguously (touching, no overlap or gaps) in their existing top-to-bottom order, while
    keeping the GROUP's own combined vertical center exactly where it originally was, rather
    than re-centering on some arbitrary new point.

    Coordinate-system-agnostic - works in canvas pixels, Fusion-normalized units, or anything
    else, as long as center_y/height are both in the same consistent unit. Only Y is touched;
    callers are responsible for leaving each item's own X (horizontal position) alone, since
    horizontal position is the whole basis Bounding fits text against.
    """
    if len(items) < 2:
        return {key: center_y for key, center_y, _height in items}

    ordered = sorted(items, key=lambda item: item[1])  # top-to-bottom by original center Y
    total_height = sum(height for _key, _center_y, height in ordered)

    tops = [center_y - height / 2 for _key, center_y, height in ordered]
    bottoms = [center_y + height / 2 for _key, center_y, height in ordered]
    original_group_center = (min(tops) + max(bottoms)) / 2

    result: Dict[Any, float] = {}
    running_top = original_group_center - total_height / 2
    for key, _center_y, height in ordered:
        result[key] = running_top + height / 2
        running_top += height
    return result


def group_overlapping_clips(clips: List[Tuple[Any, float, float]]) -> List[List[Any]]:
    """Groups clips - given as (key, start, end) - into mutually overlapping clusters
    where every clip overlaps with every other clip in its group.
    
    This avoids transitive grouping where a full-length logo would link every subtitle
    into one giant stacked group. Pure math, no Resolve/Qt dependencies - callers provide
    whatever start/end units they like (frames, seconds, etc.), consistently; treats ranges
    as half-open [start, end), matching DaVinci's own GetStart()/GetEnd() convention.

    Returns groups in no particular order, each with at least 1 member - a clip that overlaps
    nothing else is its own group of size 1.
    """
    # Group into cliques where every clip mutually overlaps with all others in the group
    sorted_clips = sorted(clips, key=lambda c: c[1])  # Sort by start time
    groups: List[List[Any]] = []
    
    for clip in sorted_clips:
        _key, start, end = clip
        placed = False
        # Try to place in an existing group where it mutually overlaps all members
        for g in groups:
            mutually_overlaps = True
            for member in g:
                _m_key, m_start, m_end = member
                if not (start < m_end and m_start < end):
                    mutually_overlaps = False
                    break
            
            if mutually_overlaps:
                g.append(clip)
                placed = True
                break
                
        if not placed:
            groups.append([clip])
            
    return [[c[0] for c in g] for g in groups]


LAYOUT_PRESETS: Dict[str, Callable[[int], List[LayoutTarget]]] = {
    "Hero + Stack": hero_stack,
    "Side-by-Side": side_by_side,
    "Grid": grid,
}
