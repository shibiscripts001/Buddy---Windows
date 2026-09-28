"""Live preview scanning: finds every Text+ clip currently under DaVinci's playhead, across
all video tracks, and reads back its real text/font/size/color/position so the interactive
placement panel can render something that actually resembles DaVinci's Edit page - the
way apps like Easy Motion work, rather than a single manually-targeted bounding box."""
from typing import Any, Callable, List, NamedTuple, Optional, Tuple

from .font_utils import DEFAULT_FONT_NAME
from .subtitle_engine import timecode_to_frames


class ActiveTextItem(NamedTuple):
    """One Text+ clip currently under the playhead, with everything needed to render and
    reposition it. `center` is None when it couldn't be parsed from GetInput("Center") -
    callers should leave the item's existing displayed position alone in that case rather
    than substituting a fake default (a hardcoded (0.5, 0.5) fallback would make the live
    preview visibly snap back to center shortly after every drag, even though DaVinci itself
    kept the real position)."""

    key: Any  # stable identity for matching across refresh polls - see _make_key() below
    track_index: int
    clip: Any
    text: str
    font_name: str
    font_size: float
    color: Tuple[float, float, float]
    center: Optional[Tuple[float, float]]


# Tracks which unparseable Center value types have already been logged, so a real project
# with an unrecognized shape gets exactly one diagnostic line instead of one every ~0.5s.
_warned_center_types: set = set()



def parse_point(value: Any) -> Optional[Tuple[float, float]]:
    """Best-effort parse of a Fusion Point-type value (e.g. Center) into (x, y) floats.

    The shape GetInput("Center") returns isn't pinned down by a live dump, and assuming a
    plain 2-element list/tuple fails silently on real projects. Tries several plausible
    Fusion representations (list/tuple, a
    dict-like table keyed by 1/2 or X/Y, or an object exposing .X/.Y attributes) rather than
    committing to one guess; returns None if nothing matches, so the caller can leave the
    previous good position alone instead of guessing a default.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        for key_x, key_y in ((1, 2), ("1", "2"), ("X", "Y"), ("x", "y")):
            if key_x in value and key_y in value:
                try:
                    return float(value[key_x]), float(value[key_y])
                except (TypeError, ValueError):
                    return None
        return None
    for attr_x, attr_y in (("X", "Y"), ("x", "y")):
        x_val = getattr(value, attr_x, None)
        y_val = getattr(value, attr_y, None)
        if x_val is not None and y_val is not None:
            try:
                return float(x_val), float(y_val)
            except (TypeError, ValueError):
                return None
    return None


def _get_current_frame(timeline: Any) -> Optional[int]:
    if timeline is None or not (hasattr(timeline, "GetCurrentTimecode") and hasattr(timeline, "GetSetting")):
        return None
    try:
        fps_val = timeline.GetSetting("timelineFrameRate")
        fps = float(fps_val) if fps_val else 24.0
    except Exception:
        fps = 24.0
    try:
        df_val = timeline.GetSetting("timelineDropFrameTimecode")
        drop_frame = str(df_val) in ("1", "True", "true")
    except Exception:
        drop_frame = False
    try:
        current_tc = timeline.GetCurrentTimecode()
    except Exception:
        current_tc = None
    return timecode_to_frames(current_tc, fps, drop_frame)


def _read_text_tool_state(
    text_tool: Any,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """Defensively reads the fields needed to render/reposition one Text+ tool, defaulting
    anything unreadable rather than failing the whole scan over one bad clip. `log`, if
    given, receives a one-time diagnostic if Center can't be parsed (see parse_point). (A
    line per clip with its raw Size went too - the size rule is measured now, see
    canvas_math, and with a clip per word it flooded the activity log.)"""
    if text_tool is None or not hasattr(text_tool, "GetInput"):
        return None

    try:
        text = text_tool.GetInput("StyledText")
    except Exception:
        text = None
    try:
        font_name = text_tool.GetInput("Font")
    except Exception:
        font_name = None
    try:
        font_size = text_tool.GetInput("Size")
    except Exception:
        font_size = None
    try:
        red = text_tool.GetInput("Red1")
        green = text_tool.GetInput("Green1")
        blue = text_tool.GetInput("Blue1")
    except Exception:
        red = green = blue = None
    try:
        center = text_tool.GetInput("Center")
    except Exception:
        center = None

    parsed_center = parse_point(center)
    if parsed_center is None and center is not None:
        type_name = type(center).__name__
        if type_name not in _warned_center_types:
            _warned_center_types.add(type_name)
            if log is not None:
                log(
                    f"[Live Preview] Could not parse Center value of type {type_name!r} "
                    f"({center!r}) – leaving this item at its last known preview position "
                    "instead of guessing. Please report this so the parser can be fixed."
                )

    return {
        "text": text if isinstance(text, str) else "",
        "font_name": font_name if isinstance(font_name, str) and font_name else DEFAULT_FONT_NAME,
        "font_size": float(font_size) if isinstance(font_size, (int, float)) else 0.08,
        "color": (
            float(red) if isinstance(red, (int, float)) else 1.0,
            float(green) if isinstance(green, (int, float)) else 1.0,
            float(blue) if isinstance(blue, (int, float)) else 1.0,
        ),
        "center": parsed_center,
    }


def get_active_text_plus_items(
    timeline: Any,
    get_fusion_comp: Callable[[Any], Optional[Any]],
    find_text_tool: Callable[[Optional[Any]], Optional[Any]],
    log: Optional[Callable[[str], None]] = None,
) -> List[ActiveTextItem]:
    """Scans every video track for the one clip (if any) spanning the current playhead
    frame, and returns an ActiveTextItem for each that resolves to a reachable TextPlus
    tool. get_fusion_comp/find_text_tool are injected rather than imported so this reuses
    the exact same defensive comp/tool lookup already proven working elsewhere in ui.py,
    instead of a second, possibly-drifting implementation."""
    items: List[ActiveTextItem] = []
    if timeline is None or not hasattr(timeline, "GetTrackCount"):
        return items

    current_frame = _get_current_frame(timeline)
    if current_frame is None:
        return items

    try:
        track_count = timeline.GetTrackCount("video") or 0
    except Exception:
        track_count = 0

    for track_index in range(1, track_count + 1):
        try:
            clips = timeline.GetItemListInTrack("video", track_index) or []
        except Exception:
            continue

        for clip in clips:
            if clip is None or not (hasattr(clip, "GetStart") and hasattr(clip, "GetEnd")):
                continue
            try:
                start_frame = clip.GetStart()
                end_frame = clip.GetEnd()
            except Exception:
                continue
            if not (start_frame <= current_frame < end_frame):
                continue

            comp = get_fusion_comp(clip)
            text_tool = find_text_tool(comp) if comp is not None else None
            if text_tool is None:
                continue

            try:
                clip_name = clip.GetName() if hasattr(clip, "GetName") else None
            except Exception:
                clip_name = None

            # NOTE: `clip` itself (a BlackmagicFusion.PyRemoteObject) is NOT hashable
            # ("cannot use 'BlackmagicFusion.PyRemoteObject' as a set element") - it can't be used as a
            # dict/set key the way Python objects normally can. Build a plain hashable tuple
            # identity instead, from values stable for as long as this clip sits at this
            # position on this track.
            key = (track_index, clip_name, start_frame, end_frame)

            state = _read_text_tool_state(text_tool, log=log)
            if state is None:
                continue

            items.append(
                ActiveTextItem(
                    key=key,
                    track_index=track_index,
                    clip=clip,
                    text=state["text"],
                    font_name=state["font_name"],
                    font_size=state["font_size"],
                    color=state["color"],
                    center=state["center"],
                )
            )

    return items
