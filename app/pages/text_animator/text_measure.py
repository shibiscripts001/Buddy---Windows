"""Where Text+ puts a face's baseline, asked of Resolve itself.

Text+ centres its lines on the Center and puts one line's baseline a fixed distance below
it for each face - the same whatever the letters - but no font table gives that distance
(canvas_math.BASELINE_FALLBACK has the measurements). So it's measured: a Text+ tool of
our own, added to a comp the playhead's clips already have, never connected to anything
(it doesn't change what the clip renders), draws a capital H at Size PROBE_SIZE on the
Center in each face asked about; its rendered bounds (Output:GetDoD) give the H's foot and
top, and the H's own shape (canvas_math.glyph_extent) where the baseline is between them.
Then the tool is deleted, inside an undo group that's thrown away. About 1 ms a face in
Resolve 21.1, measured on 10 faces.

The DoD is the ink's pixels grown by MARGIN on every side (checked against a rendered
frame: the edges land 1.6 and 2.7 px inside it, so half a pixel is added back to each),
and the two estimates of the baseline, from the foot and from the top, are averaged.

No Qt here beyond canvas_math's measuring; no Resolve handle is kept.
"""

from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from . import canvas_math

PROBE_NAME = "BuddyMeasure"
PROBE_TEXT = "H"            # a flat top at cap height and a flat foot on the baseline
PROBE_SIZE = 0.4            # big (a 1080p H ~390 px tall), so a pixel is ~0.002 em
MARGIN = 2


def _dod(value) -> Optional[Tuple[float, float, float, float]]:
    """(left, bottom, right, top) in pixels, y up, from a DoD table."""
    if isinstance(value, dict):
        try:
            return tuple(float(value[k]) for k in (1, 2, 3, 4))
        except (KeyError, TypeError, ValueError):
            return None
    return None


def baseline_from_bounds(dod, frame_height: float, em_px: float, above: float, below: float) -> Optional[float]:
    """The baseline's distance below the Center in ems, from an H's rendered bounds (a DoD,
    pixels y up, the H centred at frame_height / 2): `above` / `below` are how far the H's
    ink reaches past its baseline, in ems."""
    box = _dod(dod)
    if box is None or em_px <= 0:
        return None
    _left, bottom, _right, top = box
    foot = bottom + MARGIN + 0.5
    head = top - MARGIN + 0.5
    if head - foot < em_px * 0.2:
        return None                                  # not an H - nothing rendered, or a font without one
    from_foot = foot + below * em_px
    from_top = head - above * em_px
    value = (frame_height / 2 - (from_foot + from_top) / 2) / em_px
    return value if 0.0 < value < 1.5 else None


def measure_baselines(comp: Any, faces: Iterable[Tuple[str, str]], frame: Tuple[int, int],
                      log: Optional[Callable[[str], None]] = None) -> Dict[Tuple[str, str], float]:
    """{(family, style): ems below the Center} for each face Resolve could draw, measured
    in `comp` (any Fusion comp; left exactly as it was). frame: the comp's (width,
    height) in pixels, the timeline's - its own image says, when it can."""
    faces = [canvas_math.face(*f) for f in faces]
    found: Dict[Tuple[str, str], float] = {}
    if comp is None or not faces:
        return found
    comp.Lock()
    comp.StartUndo("Buddy measure")
    tool = None
    try:
        for old in list((comp.GetToolList(False) or {}).values()):
            if str(getattr(old, "Name", "")).startswith(PROBE_NAME):
                old.Delete()                         # left by a Buddy that stopped mid-measure
        tool = comp.AddTool("TextPlus", -32768, -32768)
        if tool is None:
            return found
        tool.SetAttrs({"TOOLS_Name": PROBE_NAME})
        tool.SetInput("StyledText", PROBE_TEXT)
        tool.SetInput("Size", PROBE_SIZE)
        tool.SetInput("Center", {1: 0.5, 2: 0.5})
        time = comp.CurrentTime
        for family, style in faces:
            tool.SetInput("Font", family)
            tool.SetInput("Style", style)
            width, height = frame
            try:
                image = tool.Output.GetValue(time)
                width, height = int(image.Width) or width, int(image.Height) or height
            except Exception:
                pass
            em_px = PROBE_SIZE * canvas_math._px_per_size(family, style) * width
            above, below = canvas_math.glyph_extent(family, PROBE_TEXT, style)
            value = baseline_from_bounds(tool.Output.GetDoD(time), height, em_px, above, below)
            if value is not None:
                found[(family, style)] = value
            elif log is not None:
                log(f"[Placement Canvas] Couldn't measure where {family} {style} sits - using an estimate.")
    finally:
        if tool is not None:
            tool.Delete()
        comp.EndUndo(False)
        comp.Unlock()
    return found
