#!/usr/bin/env python3
"""
Text Animator's settings - every control on the page, its range and default, stored in the
tool's ToolSettings bucket (~/.text_animator/settings.json) under the SAME keys earlier
versions used, so saved values carry over. No Qt: unit-tested
(tests/test_text_animator_page.py).

Colours are stored as float triples (<prefix>_r/_g/_b, 0-1 - what Fusion's Red1/
Green1/Blue1 inputs take) and go to the page as "#RRGGBB".
"""

from __future__ import annotations

import re

from .animation_engine import FusionAnimationEngine
from .overlays import GRID_TYPES, SAFE_ZONE_TYPES
from .layout_presets import LAYOUT_PRESETS

ANIM_PRESETS = ["Pop (Scale)", "Bounce (Extra Rebound)", "Fade (Opacity)", "Slide (Direction + Fade)"]
# Which Text+ clips an Apply works on - each tab's "Apply to" dropdown (Font Styling, Timeline
# Layout, Timeline Animation). "selected" is the clips selected on Resolve's timeline.
SCOPES = ("timeline", "selected", "playhead", "track")
# tab -> settings key of its "Apply to" choice. Font Styling's also reads the two older keys below.
SCOPE_KEYS = {"style_scope": "font_style_scope", "anim_scope": "anim_scope", "layout_scope": "layout_scope"}

# name -> (label, min, max, step, decimals, default, settings key). The page's sliders.
SLIDERS = {
    "font_size": ("Size", 0.01, 1.0, 0.01, 3, 0.08, "font_size"),
    "outline_thickness": ("Thickness", 0.0, 1.0, 0.005, 3, 0.04, "outline_thickness"),
    "outline_opacity": ("Opacity", 0.0, 1.0, 0.01, 2, 1.0, "outline_opacity"),
    "shadow_offset_x": ("Offset X", -1.0, 1.0, 0.01, 3, 0.0, "shadow_offset_x"),
    "shadow_offset_y": ("Offset Y", -1.0, 1.0, 0.01, 3, -0.02, "shadow_offset_y"),
    "shadow_blur": ("Blur", 0.0, 100.0, 0.5, 2, 5.0, "shadow_blur"),
    "shadow_opacity": ("Opacity", 0.0, 1.0, 0.01, 2, 0.75, "shadow_opacity"),
    "background_outline_width": ("Outline width", 0.0, 1.0, 0.01, 3, 0.0, "background_outline_width"),
    "background_corner_radius": ("Corner radius", 0.0, 1.0, 0.01, 3, 0.0, "background_corner_radius"),
    "background_extend_horizontal": ("Extend H", -1.0, 1.0, 0.01, 3, 0.0, "background_extend_horizontal"),
    "background_extend_vertical": ("Extend V", -1.0, 1.0, 0.01, 3, 0.0, "background_extend_vertical"),
    "background_opacity": ("Opacity", 0.0, 1.0, 0.01, 2, 0.5, "background_opacity"),
}
# name -> (default (r, g, b), settings key prefix)
COLORS = {
    "font_color": ((1.0, 1.0, 1.0), "current_font_color"),
    "outline_color": ((1.0, 0.0, 0.0), "outline_color"),
    "shadow_color": ((0.0, 0.0, 0.0), "shadow_color"),
    "background_color": ((0.0, 0.0, 0.0), "background_color"),
    "background_outline_color": ((1.0, 1.0, 1.0), "background_outline_color"),
}
# name -> (default, settings key). "*_group_on" replaced "*_group_checked" when outline/
# shadow/background started defaulting to off, so old default-on settings don't tick them.
TOGGLES = {
    "font_on": (True, "font_group_checked"),
    "outline_on": (False, "outline_group_on"),
    "shadow_on": (False, "shadow_group_on"),
    "background_on": (False, "background_group_on"),
    "background_override_sizing": (False, "background_override_sizing"),
}
TRACKS = {
    "style_track": (1, "font_style_scope_track"),
    "anim_track": (1, "anim_specific_track"),
    "layout_track": (1, "layout_specific_track"),
}
# The canvases' overlay and snapping settings (keys of their own).
OVERLAY = {
    "grid_type": ("None", GRID_TYPES),
    "grid_color": "#E6E6E6",
    "grid_opacity": 0.5,
    "grid_spacing": 0.0,        # fraction of canvas width; 0 = the resolution's own (canvas_math)
    "safe_type": ("None", SAFE_ZONE_TYPES),
    "safe_color": "#000000",
    "safe_opacity": 0.6,
    "snap": True,
    "snap_elements": True,
    "snap_safe": True,
}

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(float(c) * 255))):02X}" for c in rgb)


def from_hex(text):
    match = _HEX.match(str(text or "").strip())
    if not match:
        return None
    digits = match.group(1)
    return tuple(int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Options:
    """The page's settings, read from and written straight to the ToolSettings bucket."""

    def __init__(self, settings):
        self.s = settings

    # ------------------------------------------------------------ reads --

    def slider(self, name) -> float:
        _label, lo, hi, _step, decimals, default, key = SLIDERS[name]
        return round(max(lo, min(hi, _float(self.s.get(key, default), default))), decimals)

    def color(self, name):
        default, prefix = COLORS[name]
        return tuple(_float(self.s.get(f"{prefix}_{c}", d), d) for c, d in zip("rgb", default))

    def toggle(self, name) -> bool:
        default, key = TOGGLES[name]
        return bool(self.s.get(key, default))

    def track(self, name) -> int:
        default, key = TRACKS[name]
        try:
            return max(1, min(99, int(self.s.get(key, default))))
        except (TypeError, ValueError):
            return default

    @property
    def font_name(self) -> str:
        from .font_utils import DEFAULT_FONT_NAME
        return str(self.s.get("selected_font_name") or DEFAULT_FONT_NAME)

    def scope(self, name: str) -> str:
        """A tab's "Apply to" choice (see SCOPES); name is one of SCOPE_KEYS."""
        value = self.s.get(SCOPE_KEYS[name])
        if value in SCOPES:
            return value
        if name == "style_scope":          # saved before the dropdown: two ticks
            if self.s.get("font_style_scope_specific_track"):
                return "track"
            if self.s.get("font_style_scope_playhead"):
                return "playhead"
        return "timeline"

    @property
    def style_scope(self) -> str:
        return self.scope("style_scope")

    @property
    def anim_preset(self) -> str:
        preset = self.s.get("anim_preset")
        return preset if preset in ANIM_PRESETS else ANIM_PRESETS[0]

    @property
    def anim_direction(self) -> str:
        direction = self.s.get("anim_slide_direction")
        return direction if direction in FusionAnimationEngine.SLIDE_DIRECTIONS else FusionAnimationEngine.SLIDE_DIRECTIONS[0]

    @property
    def anim_speed(self) -> str:
        speeds = FusionAnimationEngine.SPEEDS
        try:
            return speeds[max(0, min(len(speeds) - 1, int(self.s.get("anim_speed", len(speeds) - 1))))]
        except (TypeError, ValueError):
            return speeds[-1]

    @property
    def layout_preset(self) -> str:
        preset = self.s.get("layout_preset")
        return preset if preset in LAYOUT_PRESETS else next(iter(LAYOUT_PRESETS))

    def overlay(self, name):
        spec = OVERLAY[name]
        if isinstance(spec, tuple):
            default, choices = spec
            value = self.s.get(f"canvas_{name}", default)
            return value if value in choices else default
        value = self.s.get(f"canvas_{name}", spec)
        if isinstance(spec, bool):
            return bool(value)
        if isinstance(spec, float):
            return max(0.0, min(1.0, _float(value, spec)))
        return value if from_hex(value) else spec

    # ------------------------------------------------------------ write --

    def set(self, name, value) -> bool:
        """Store one control's new value (validated). False if it isn't a known control
        or the value doesn't fit it."""
        s = self.s
        if name in SLIDERS:
            _label, lo, hi, _step, decimals, _default, key = SLIDERS[name]
            number = _float(value, None)
            if number is None:
                return False
            s[key] = round(max(lo, min(hi, number)), decimals)
        elif name in COLORS:
            rgb = from_hex(value)
            if rgb is None:
                return False
            prefix = COLORS[name][1]
            for c, v in zip("rgb", rgb):
                s[f"{prefix}_{c}"] = v
        elif name in TOGGLES:
            s[TOGGLES[name][1]] = bool(value)
        elif name in TRACKS:
            try:
                s[TRACKS[name][1]] = max(1, min(99, int(value)))
            except (TypeError, ValueError):
                return False
        elif name == "font_name":
            if not isinstance(value, str) or not value.strip():
                return False
            s["selected_font_name"] = value.strip()
        elif name in SCOPE_KEYS:
            if value not in SCOPES:
                return False
            s[SCOPE_KEYS[name]] = value
            if name == "style_scope":        # kept in step, for an older Buddy on this PC
                s["font_style_scope_playhead"] = value == "playhead"
                s["font_style_scope_specific_track"] = value == "track"
        elif name == "anim_preset":
            if value not in ANIM_PRESETS:
                return False
            s["anim_preset"] = value
        elif name == "anim_direction":
            if value not in FusionAnimationEngine.SLIDE_DIRECTIONS:
                return False
            s["anim_slide_direction"] = value
        elif name == "anim_speed":
            if value not in FusionAnimationEngine.SPEEDS:
                return False
            s["anim_speed"] = FusionAnimationEngine.SPEEDS.index(value)
        elif name == "layout_preset":
            if value not in LAYOUT_PRESETS:
                return False
            s["layout_preset"] = value
        elif name in OVERLAY:
            spec = OVERLAY[name]
            if isinstance(spec, tuple):
                if value not in spec[1]:
                    return False
            elif isinstance(spec, bool):
                value = bool(value)
            elif isinstance(spec, float):
                number = _float(value, None)
                if number is None:
                    return False
                value = max(0.0, min(1.0, number))
            elif not from_hex(value):
                return False
            s[f"canvas_{name}"] = value
        else:
            return False
        s.save()
        return True

    # ------------------------------------------------------------- view --

    def view(self) -> dict:
        """Everything the page's controls show."""
        return {
            "sliders": {name: {"label": spec[0], "min": spec[1], "max": spec[2], "step": spec[3],
                               "decimals": spec[4], "default": spec[5], "value": self.slider(name)}
                        for name, spec in SLIDERS.items()},
            "colors": {name: to_hex(self.color(name)) for name in COLORS},
            "toggles": {name: self.toggle(name) for name in TOGGLES},
            "tracks": {name: self.track(name) for name in TRACKS},
            "font_name": self.font_name,
            "style_scope": self.style_scope,
            "anim_scope": self.scope("anim_scope"),
            "layout_scope": self.scope("layout_scope"),
            "anim_presets": ANIM_PRESETS, "anim_preset": self.anim_preset,
            "anim_directions": list(FusionAnimationEngine.SLIDE_DIRECTIONS), "anim_direction": self.anim_direction,
            "anim_speeds": list(FusionAnimationEngine.SPEEDS), "anim_speed": self.anim_speed,
            "layout_presets": list(LAYOUT_PRESETS), "layout_preset": self.layout_preset,
        }

    def overlay_view(self) -> dict:
        return {name: self.overlay(name) for name in OVERLAY}
