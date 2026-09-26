"""The placement canvas's overlays - composition-guide grids and platform safe zones - as
plain fractions of the frame. No Qt: the web canvas draws them (web/canvas.js) and snaps to
them; canvas_math re-exports them for the Python side."""

from typing import Tuple

# Composition-guide line positions (as fractions of frame width/height) per grid type.
# Phi uses the golden ratio's reciprocal (~0.618) and its complement
# (~0.382), the standard "golden ratio grid" convention. "Standard" isn't a fixed
# set of fractions - it's an evenly-spaced grid covering the whole frame, spacing configurable
# in pixels via grid_spacing_x/y - so it's handled separately in drawForeground(), not here.
_PHI_RECIPROCAL = (5 ** 0.5 - 1) / 2  # ~0.618
GRID_FRACTIONS = {
    "None": [],
    "Rule of Thirds": [1 / 3, 2 / 3],
    "Center": [0.5],
    "Phi": [1 - _PHI_RECIPROCAL, _PHI_RECIPROCAL],
}
GRID_TYPES = ["None", "Standard"] + [t for t in GRID_FRACTIONS.keys() if t != "None"]  # "None" first, "Standard" second
DEFAULT_GRID_SPACING_PX = 40.0
MIN_GRID_SPACING_PX = 10
MAX_GRID_SPACING_PX = 150

# Platform safe-zone overlays - each value is a list of (left, top, right, bottom) fractions
# (left/right relative to canvas WIDTH, top/bottom relative to canvas HEIGHT) describing the
# rectangle(s) that stay clear of that platform's own UI chrome (profile info, caption area,
# action-button rail, etc). Everything outside the union of a platform's rects is the "unsafe"
# region the overlay shades. Measured from platform reference diagrams, each against a
# 1080x1920 canvas:
#   - "YouTube Shorts": top 240px, bottom 380px, left 60px, right 120px margins - a single
#     centered rectangle.
#   - "Instagram": an L-shape, not a simple rect - top margin 250px,
#     bottom margin 420px, left margin 70px throughout, but the right margin changes partway
#     down to make room for the action-button rail (like/comment/share icons): 55px right
#     margin from y=250 down to a corner at y=860, then a wider 193px right margin from
#     y=860 down to the bottom edge (y=1500).
#   - "TikTok": also an L-shape - a wide top strip (126px top margin, only 60px right margin,
#     down to y=180) sitting above a narrower main column (120px right margin, down to y=608)
#     before the 352px bottom caption/UI margin - all figures at the reference diagram's
#     native 540x960 scale, doubled here for the 1080x1920 canvas fractions below.
#     Both L-shapes are represented as two stacked rects so a simple per-rect "subtract from
#     full canvas" overlay (see PlacementCanvasView._draw_safe_zone_overlay) reproduces the
#     exact shape.
#   - "General": a single plain rectangle, safe across every platform above at once - each
#     margin is the MAX of that side's margin across YouTube Shorts/Instagram/TikTok (using
#     each platform's widest point where its own margin varies, e.g. Instagram's 193px and
#     TikTok's 240px right margins rather than their narrower top-of-frame values), so it's
#     guaranteed to sit inside all three platforms' safe areas simultaneously.
SAFE_ZONE_RECTS = {
    "YouTube Shorts": [(60 / 1080, 240 / 1920, 1 - 120 / 1080, 1 - 380 / 1920)],
    "Instagram": [
        (70 / 1080, 250 / 1920, 1 - 55 / 1080, 860 / 1920),
        (70 / 1080, 860 / 1920, 1 - 193 / 1080, 1 - 420 / 1920),
    ],
    "TikTok": [
        (120 / 1080, 252 / 1920, 1 - 120 / 1080, 360 / 1920),
        (120 / 1080, 360 / 1920, 1 - 240 / 1080, 1 - 704 / 1920),
    ],
    "General": [(120 / 1080, 252 / 1920, 1 - 240 / 1080, 1 - 704 / 1920)],
}
SAFE_ZONE_TYPES = ["None"] + sorted(SAFE_ZONE_RECTS.keys())
DEFAULT_SAFE_ZONE_COLOR: Tuple[float, float, float] = (0.0, 0.0, 0.0)
DEFAULT_SAFE_ZONE_OPACITY = 0.6
DEFAULT_GRID_OPACITY = 0.5
