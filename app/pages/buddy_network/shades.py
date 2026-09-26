"""The colours of the actions under each chat message - reply, edit, delete,
report and delete forever - picked from the current theme so they're told
apart at a glance without shouting.

Each is a theme colour mixed mostly toward the muted grey the actions used
to share, kept a little colourful (pastel themes would otherwise go nearly
white), then lifted just enough to read on the background (3:1):
  reply    the theme's accent - unless it's close to a warning colour, as
           in Default (its accent IS its red) and Retro (pink): then its
           secondary, or a calm blue
  edit     the theme's green      delete  amber
  report   a colour none of the others use (secondary, else the first
           of violet, teal, pink, blue that's clear of the rest)
  purge    "delete forever": the theme's red, the strongest of them
"""

from __future__ import annotations

import colorsys

from core.theme import ensure_contrast, hue_distance

FALLBACK = {"danger": "#F2B8B5", "warning": "#E0A030", "success": "#A3D5AD"}
CALM_BLUE = "#6FA8DC"
# For report, in this order, when the theme's own colours are all taken.
SPARE = ("#A98BD8", "#5BC0B5", "#D98BB5", CALM_BLUE)   # violet, teal, pink, blue
MIN_HUE_GAP = 0.08          # ~30 degrees: close enough below this to mix up
MIN_CONTRAST = 3.0
# Pastel themes ("Don't be evil") mixed toward grey go nearly white: each
# shade keeps at least this much colour, and stays inside this lightness
# band, so the five still look different.
MIN_SATURATION = 0.5
LIGHTNESS = (0.3, 0.7)
# How much of the theme colour goes in (the rest is the muted grey).
AMOUNT = {"reply": 0.6, "edit": 0.55, "delete": 0.6, "report": 0.5, "purge": 0.8}


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _saturation(color: str) -> float:
    return colorsys.rgb_to_hls(*[c / 255 for c in _rgb(color)])[2]


def _vivid(color: str) -> str:
    """The same hue, with at least MIN_SATURATION and inside LIGHTNESS."""
    hue, light, sat = colorsys.rgb_to_hls(*[c / 255 for c in _rgb(color)])
    light = min(max(light, LIGHTNESS[0]), LIGHTNESS[1])
    r, g, b = colorsys.hls_to_rgb(hue, light, max(sat, MIN_SATURATION))
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


def blend(top: str, under: str, amount: float) -> str:
    """`amount` of top over under."""
    mixed = (round(a * amount + b * (1 - amount)) for a, b in zip(_rgb(top), _rgb(under)))
    return "#" + "".join(f"{c:02x}" for c in mixed)


def action_colors(tokens: dict, muted: str, surface: str) -> dict[str, str]:
    """{"reply", "edit", "delete", "report", "purge": colour} for this theme."""
    danger = tokens.get("danger") or FALLBACK["danger"]
    warning = tokens.get("warning") or FALLBACK["warning"]
    success = tokens.get("success") or FALLBACK["success"]
    taken = [danger, warning, success]

    def distinct(color) -> bool:
        return bool(color) and _saturation(color) > 0.25 and all(
            hue_distance(color, other) >= MIN_HUE_GAP for other in taken)

    reply = next((c for c in (tokens.get("primary"), tokens.get("secondary"), CALM_BLUE) if distinct(c)), CALM_BLUE)
    taken.append(reply)
    report = next((c for c in (tokens.get("secondary"), tokens.get("tertiary"), *SPARE) if distinct(c)), SPARE[0])
    base = {"reply": reply, "edit": success, "delete": warning, "report": report, "purge": danger}
    return {kind: ensure_contrast(_vivid(blend(color, muted, AMOUNT[kind])), surface, MIN_CONTRAST)
            for kind, color in base.items()}

