#!/usr/bin/env python3
"""
Shared design system for the Buddy shell and every tool page inside it.

This is the same token system every standalone tool used to copy-paste
independently - consolidated here as the single copy so changing a
preset, or adding one, takes effect everywhere at once instead of needing
edits in eleven places.
"""

import colorsys
import math
import os
import tempfile
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

# --- theme hierarchy ------------------------------------------------------
#
# Two levels. A THEME is the overall look: its shape (corners, border
# weights, font) plus its own family of palettes. A SUBTHEME is a palette
# within that theme. Subthemes are scoped per theme rather than shared,
# because Ocean/Sunset/Forest were drawn as dark, softly rounded palettes and
# would read as broken under Retro's flat, outlined shape - and because it
# leaves room for Retro to grow its own variants.
#
# "Custom" is offered under every theme: it derives its palette from the
# user's accent/background, which composes with any shape.

SUBTHEME_CUSTOM = "Custom"

_PALETTES = {
    "Default": {
        "primary": "#D0BCFF",
        "on_primary": "#381E72",
        "primary_container": "#4F378B",
        "on_primary_container": "#EADDFF",
        "secondary": "#CCC2DC",
        "on_secondary": "#332D41",
        "secondary_container": "#4A4458",
        "surface": "#141218",
        "on_surface": "#E6E0E9",
        "surface_container": "#211F26",
        "surface_container_high": "#2B2930",
        "outline": "#938F99",
        "outline_variant": "#49454F",
    },
    "Ocean": {
        "primary": "#80D4F6",
        "on_primary": "#003547",
        "primary_container": "#004D65",
        "on_primary_container": "#C2E8FF",
        "secondary": "#B4CBD6",
        "on_secondary": "#1F333C",
        "secondary_container": "#354A53",
        "surface": "#0F1417",
        "on_surface": "#DEE3E6",
        "surface_container": "#1B2024",
        "surface_container_high": "#252B30",
        "outline": "#899296",
        "outline_variant": "#40484C",
    },
    "Sunset": {
        "primary": "#FFB59D",
        "on_primary": "#5F1500",
        "primary_container": "#812807",
        "on_primary_container": "#FFDBCF",
        "secondary": "#E7BDB2",
        "on_secondary": "#442A22",
        "secondary_container": "#5D4037",
        "surface": "#1A110F",
        "on_surface": "#F0DFDA",
        "surface_container": "#271D1A",
        "surface_container_high": "#322824",
        "outline": "#A08C87",
        "outline_variant": "#53433F",
    },
    "Forest": {
        "primary": "#A3D5AD",
        "on_primary": "#0B371B",
        "primary_container": "#244F2F",
        "on_primary_container": "#BEF2C8",
        "secondary": "#BACCB9",
        "on_secondary": "#253427",
        "secondary_container": "#3C4B3D",
        "surface": "#111412",
        "on_surface": "#E2E3DF",
        "surface_container": "#1D211E",
        "surface_container_high": "#272B28",
        "outline": "#8B938A",
        "outline_variant": "#414941",
    },
    "Peach": {
        # Peach/cream paper, near-black
        # navy ink, cyan chrome, pink for destructive/alert. The light
        # half of the Retro family - Licorice/Mulberry below are its
        # dark counterparts.
        "primary": "#2BA9CF",            # cyan title bars, active chrome
        "on_primary": "#12303B",
        "primary_container": "#8FD6EA",
        "on_primary_container": "#12303B",
        "secondary": "#E8607D",          # pink: errors, destructive
        "on_secondary": "#FFF2F4",
        "secondary_container": "#F6B8C4",
        "surface": "#F6E0C8",            # peach paper
        "on_surface": "#2E2A3B",         # navy ink
        "surface_container": "#FBEEDC",
        "surface_container_high": "#FFF8EE",
        "outline": "#2E2A3B",            # every edge is the ink color
        "outline_variant": "#2E2A3B",
    },
}

_PALETTES["Bubblegum"] = {
    # The pink-led variant: same paper and ink, pink
    # chrome instead of cyan, cyan demoted to the accent role.
    "primary": "#E8607D",
    "on_primary": "#FFF2F4",
    "primary_container": "#F6B8C4",
    "on_primary_container": "#4A1622",
    "secondary": "#2BA9CF",
    "on_secondary": "#12303B",
    "secondary_container": "#8FD6EA",
    "surface": "#F6E0C8",
    "on_surface": "#2E2A3B",
    "surface_container": "#FBEEDC",
    "surface_container_high": "#FFF8EE",
    "outline": "#2E2A3B",
    "outline_variant": "#2E2A3B",
}

# The dark half of the Retro family: the light palettes' own ink, as paper.
# The relationship is inverted wholesale rather than only the background -
# the plum-navy that was Peach/Bubblegum's ink becomes the paper (darkened
# one step so panels can still sit lighter than it), and the peach paper
# comes back as a cream ink for text and outlines. Accent hues stay the
# light palettes' own, lifted in lightness the same way every
# dark palette in this file lifts its primary, so cyan and pink still read
# as text and as fills on the dark surface.
#
# Names: Licorice (paper that is nearly the light palettes' ink colour) and
# Mulberry (the pink-led counterpart, as Bubblegum is Peach's).
_PALETTES["Licorice"] = {
    # Dark counterpart to Peach: cyan chrome, pink for destructive.
    "primary": "#6FCBE8",
    "on_primary": "#0D2530",
    "primary_container": "#17586F",
    "on_primary_container": "#C6ECF8",
    "secondary": "#F0788F",
    "on_secondary": "#F4B8C4",
    "secondary_container": "#6E2233",
    "surface": "#221E2C",
    "on_surface": "#F0E7D8",
    "surface_container": "#2F2B39",
    "surface_container_high": "#393543",
    "outline": "#D8CCBB",
    "outline_variant": "#D8CCBB",
}

_PALETTES["Mulberry"] = {
    # Dark counterpart to Bubblegum: pink chrome, cyan demoted to accent.
    "primary": "#F2798E",
    "on_primary": "#33101A",
    "primary_container": "#6E2233",
    "on_primary_container": "#F8D3DB",
    "secondary": "#6FCBE8",
    "on_secondary": "#C6ECF8",
    "secondary_container": "#17586F",
    "surface": "#221E2C",
    "on_surface": "#F0E7D8",
    "surface_container": "#2F2B39",
    "surface_container_high": "#393543",
    "outline": "#D8CCBB",
    "outline_variant": "#D8CCBB",
}

# The SaaS family: the dark "product landing page" look - a deep navy-teal
# backdrop with a soft glow, slate cards a shade lighter with a thin
# blue-tinted edge, and one vivid accent. Qt stylesheets cannot blur, so the
# frosted-glass cards of that look are approximated by a gentle
# top-to-bottom card gradient (surface_container_high -> surface_container)
# over a radial backdrop gradient (surface -> surface_glow). surface_glow is
# the one key only these palettes carry; see get_app_theme().
_PALETTES["Midnight"] = {
    # The base palette: navy-teal night, bright azure accent. Every
    # primary_container in this family is lifted until it clears
    # MIN_SELECTION_CONTRAST against its cards (with margin) - otherwise
    # selection falls back to the stark inverted bar, which fights the look.
    "primary": "#2F9BFF",
    "on_primary": "#FFFFFF",
    "primary_container": "#164C79",
    "on_primary_container": "#D2E8FF",
    "secondary": "#5FD4F4",
    "on_secondary": "#06283A",
    "secondary_container": "#173A4E",
    "surface": "#0A1720",
    "surface_glow": "#0F3A52",
    "on_surface": "#EAF2F7",
    "surface_container": "#15293A",
    "surface_container_high": "#1D3548",
    "outline": "#8FA8BA",
    "outline_variant": "#2A4A60",
}

_PALETTES["Aurora"] = {
    # Same night, teal-emerald accent - the backdrop's glow colour
    # pulled forward into the chrome.
    "primary": "#1FD1AE",
    "on_primary": "#032B24",
    "primary_container": "#10534A",
    "on_primary_container": "#C4F5EA",
    "secondary": "#7FB8FF",
    "on_secondary": "#0A2240",
    "secondary_container": "#16384A",
    "surface": "#071619",
    "surface_glow": "#0B3B3A",
    "on_surface": "#E6F4F1",
    "surface_container": "#12292C",
    "surface_container_high": "#1A3438",
    "outline": "#89ABA6",
    "outline_variant": "#264A4C",
}

_PALETTES["Graphite"] = {
    # Neutral slate for people who find the navy too blue; violet accent.
    "primary": "#8B7BFF",
    "on_primary": "#FFFFFF",
    "primary_container": "#3A357C",
    "on_primary_container": "#E1DCFF",
    "secondary": "#5FD4F4",
    "on_secondary": "#06283A",
    "secondary_container": "#262B3A",
    "surface": "#0D0F15",
    "surface_glow": "#1C2033",
    "on_surface": "#ECEDF3",
    "surface_container": "#181B25",
    "surface_container_high": "#20242F",
    "outline": "#9499AB",
    "outline_variant": "#2F3444",
}

_PALETTES["Daylight"] = {
    # The light counterpart: cool off-white page, white cards, the same
    # azure accent deepened enough to carry white text.
    "primary": "#1673E6",
    "on_primary": "#FFFFFF",
    "primary_container": "#B5D4FA",
    "on_primary_container": "#0B3A75",
    "secondary": "#0E9CC2",
    "on_secondary": "#FFFFFF",
    "secondary_container": "#DCEFF6",
    "surface": "#EEF3F8",
    "surface_glow": "#D8E9F9",
    "on_surface": "#142433",
    "surface_container": "#F7FAFD",
    "surface_container_high": "#FFFFFF",
    "outline": "#5E7385",
    "outline_variant": "#D0DCE7",
}

# --- Nova ---------------------------------------------------------------------
#
# The glass look. Colours stay plain hex like every other palette - the
# helpers above (contrast, blends, QColor) all parse hex - and the glass
# itself (translucent cards, blur, glow orbs) is drawn by the web pages
# from these same tokens (see core/web_theme.py). The Qt widgets get
# Modern's gradient treatment with rounder, roomier shapes.

_PALETTES["Neon"] = {
    "primary": "#FF3366",            # hot pink
    "on_primary": "#FFFFFF",
    "primary_container": "#5C1530",
    "on_primary_container": "#FFD6E0",
    "secondary": "#33CCFF",          # cyan: the second speaker, the second orb
    "on_secondary": "#00202B",
    "secondary_container": "#152A3A",
    "surface": "#09090B",
    "surface_glow": "#2A1030",
    "on_surface": "#F4F4F5",
    "surface_container": "#18181B",
    "surface_container_high": "#232329",
    "outline": "#A1A1AA",
    "outline_variant": "#3A2A33",
}

_PALETTES["Nebula"] = {
    "primary": "#A78BFA",            # violet
    "on_primary": "#1A0B3D",
    "primary_container": "#3F2A7A",
    "on_primary_container": "#E9E2FF",
    "secondary": "#2DD4BF",          # teal
    "on_secondary": "#002822",
    "secondary_container": "#16303A",
    "surface": "#0B0A14",
    "surface_glow": "#1E1640",
    "on_surface": "#EEEDF7",
    "surface_container": "#171526",
    "surface_container_high": "#211E33",
    "outline": "#A09CB8",
    "outline_variant": "#2F2B48",
}

_PALETTES["Ember"] = {
    "primary": "#FB923C",            # amber
    "on_primary": "#2B1100",
    "primary_container": "#6A2C0A",
    "on_primary_container": "#FFE3CC",
    "secondary": "#F472B6",          # rose
    "on_secondary": "#3A0A22",
    "secondary_container": "#35182A",
    "surface": "#0E0A08",
    "surface_glow": "#34170A",
    "on_surface": "#F6EFEA",
    "surface_container": "#1C1612",
    "surface_container_high": "#271F1A",
    "outline": "#B3A49A",
    "outline_variant": "#3D2E26",
}


# --- Off-world ----------------------------------------------------------------
#
# A retro-futurist phosphor terminal: one glowing colour on near-black glass,
# monospace type, square outlined controls, scanlines. Every palette is
# monochrome - the web mapping (core/web_theme.py _offworld) derives the
# text, borders and fills from "primary" alone, which is also what lets a
# Custom palette work. Status colours sit on each palette (they override
# the family's) because "success" has to read against its own phosphor.

_PALETTES["Amber"] = {
    "primary": "#FF7A1A",            # orange phosphor
    "on_primary": "#140800",
    "primary_container": "#3D1C06",
    "on_primary_container": "#FFC78F",
    "secondary": "#FFB066",
    "on_secondary": "#140800",
    "secondary_container": "#2A1507",
    # Only for colours that must differ from everything else (Buddy
    # Network's "report"): the orange muted grey drags the fallback pink
    # onto the red otherwise.
    "tertiary": "#9D7BFF",
    "surface": "#0A0705",
    "on_surface": "#F2A55E",
    "surface_container": "#110C08",
    "surface_container_high": "#18110B",
    "outline": "#A8662E",
    "outline_variant": "#3A2414",
    "success": "#9CE35B",
    "danger": "#FF4545",
}

_PALETTES["Green"] = {
    "primary": "#3DFF7A",            # P1 green
    "on_primary": "#021006",
    "primary_container": "#0B3A1B",
    "on_primary_container": "#AAFFC4",
    "secondary": "#9CFFB8",
    "on_secondary": "#021006",
    "secondary_container": "#0A2413",
    "surface": "#040906",
    "on_surface": "#66E892",
    "surface_container": "#08100A",
    "surface_container_high": "#0D180F",
    "outline": "#2F9A52",
    "outline_variant": "#143020",
    "success": "#D6FF5C",
    "danger": "#FF5A4A",
}

_PALETTES["Ice"] = {
    "primary": "#5CD8FF",            # cold light-blue phosphor
    "on_primary": "#021016",
    "primary_container": "#0A3447",
    "on_primary_container": "#C0EEFF",
    "secondary": "#A8E9FF",
    "on_secondary": "#021016",
    "secondary_container": "#0A2230",
    "surface": "#04080B",
    "on_surface": "#7ACDEB",
    "surface_container": "#080F14",
    "surface_container_high": "#0D161D",
    "outline": "#3786A3",
    "outline_variant": "#15303D",
    "success": "#6CFFB0",
    "danger": "#FF5C73",
}


# --- Desktop ----------------------------------------------------------------
#
# A cheerful retro desktop: tools open as floating windows (THEMES
# "layout": "desktop", core/desktop_window.py) on a dotted desk, drawn with
# ink outlines, rounded corners and hard offset shadows. "window_1".."4"
# colour the title bars in turn; "desk" is what's behind the windows.
# Custom derives both (desktop_colors below).
#
# "outline" is the ink every line and shadow is drawn in; text is
# "on_surface". On paper they're the same colour. The dark palettes keep
# near-black ink - so the shadows still read as shadows - on a desk a step
# lighter than the windows, with cream text and dark-tinted containers.

_PALETTES["Paper"] = {
    "primary": "#2FB5B0",            # teal
    "on_primary": "#22212B",
    "primary_container": "#8ED8D2",
    "on_primary_container": "#22212B",
    "secondary": "#FFA630",          # orange
    "on_secondary": "#22212B",
    "secondary_container": "#FFD9A6",
    "tertiary": "#F28B8B",           # pink
    "surface": "#F3E8D8",            # cream paper
    "on_surface": "#22212B",         # ink
    "surface_container": "#FBF3E7",
    "surface_container_high": "#FFFAF2",
    "outline": "#22212B",
    "outline_variant": "#22212B",
    "desk": "#EADFCD",
    "window_1": "#FFA630", "window_2": "#5FC6C1", "window_3": "#8DB8F2", "window_4": "#F28B8B",
}

_PALETTES["Mint"] = {
    "primary": "#FF8A5C",            # coral
    "on_primary": "#1D3530",
    "primary_container": "#FFC4AB",
    "on_primary_container": "#1D3530",
    "secondary": "#4FC3A6",          # sea green
    "on_secondary": "#1D3530",
    "secondary_container": "#BDEBDD",
    "tertiary": "#B79CFF",           # lilac
    "surface": "#E3F2EA",
    "on_surface": "#1D3530",
    "surface_container": "#F0F9F4",
    "surface_container_high": "#F9FDFB",
    "outline": "#1D3530",
    "outline_variant": "#1D3530",
    "desk": "#D5EADF",
    "window_1": "#FFD35C", "window_2": "#FF8A5C", "window_3": "#4FC3A6", "window_4": "#B79CFF",
}

_PALETTES["Sky"] = {
    "primary": "#FF7A93",            # bubblegum pink
    "on_primary": "#1E2A44",
    "primary_container": "#FFC2CE",
    "on_primary_container": "#1E2A44",
    "secondary": "#6FA8F5",          # blue
    "on_secondary": "#1E2A44",
    "secondary_container": "#C7DCFB",
    "tertiary": "#FFC857",           # sunshine
    "surface": "#E4ECFA",
    "on_surface": "#1E2A44",
    "surface_container": "#F0F4FD",
    "surface_container_high": "#F9FBFF",
    "outline": "#1E2A44",
    "outline_variant": "#1E2A44",
    "desk": "#D3DFF5",
    "window_1": "#6FA8F5", "window_2": "#FFC857", "window_3": "#FF7A93", "window_4": "#7FD6B5",
}

_PALETTES["Dusk"] = {                # Paper after dark
    "primary": "#3CCFC8",            # teal
    "on_primary": "#10222A",
    "primary_container": "#1E4E4B",
    "on_primary_container": "#D5F7F3",
    "secondary": "#FFB347",          # orange
    "on_secondary": "#2A1A05",
    "secondary_container": "#4A3A26",
    "tertiary": "#F59A9A",
    "surface": "#22212F",
    "on_surface": "#F1EADF",         # cream text
    "surface_container": "#2B2A3B",
    "surface_container_high": "#343346",
    "outline": "#0B0A12",            # near-black ink
    "outline_variant": "#0B0A12",
    "desk": "#3A3850",
    "window_1": "#FFB347", "window_2": "#5FD3CC", "window_3": "#93BDF7", "window_4": "#F59A9A",
}

_PALETTES["Arcade"] = {              # neon candy on a night-time cabinet
    "primary": "#FF6FB1",            # hot pink
    "on_primary": "#2A0717",
    # Bright enough to clear MIN_SELECTION_CONTRAST on the panels (#5A2141
    # landed at 1.2 and flipped selection to a white bar).
    "primary_container": "#7A2C58",
    "on_primary_container": "#FFD6EA",
    "secondary": "#7CE0FF",          # cyan
    "on_secondary": "#06222C",
    "secondary_container": "#1E3E4F",
    "tertiary": "#FFE066",
    "surface": "#1A1826",
    "on_surface": "#F4F0FF",
    "surface_container": "#232033",
    "surface_container_high": "#2C2940",
    "outline": "#07060C",
    "outline_variant": "#07060C",
    "desk": "#2E2A45",
    "window_1": "#FF6FB1", "window_2": "#7CE0FF", "window_3": "#FFE066", "window_4": "#9DFF8A",
}

_PALETTES["Cocoa"] = {               # warm browns, caramel and mint
    "primary": "#F2A541",            # caramel
    "on_primary": "#2A1804",
    "primary_container": "#5A3E1B",
    "on_primary_container": "#FCE3C0",
    "secondary": "#8ED1B5",          # mint
    "on_secondary": "#0D2A20",
    "secondary_container": "#2E4A40",
    "tertiary": "#E88D8D",
    "surface": "#2A211C",
    "on_surface": "#F5E9DC",
    "surface_container": "#342A24",
    "surface_container_high": "#3E322B",
    "outline": "#0F0B09",
    "outline_variant": "#0F0B09",
    "desk": "#4A3A30",
    "window_1": "#F2A541", "window_2": "#8ED1B5", "window_3": "#E88D8D", "window_4": "#A7B6F2",
}


# --- DaVinci Resolve --------------------------------------------------------
#
# The "Default" theme (key "Resolve"): Resolve 21's own UI colours:
#
#   #1C1C20  window / menu bar          #17181A  toolbars, selected segment
#   #28282E  panels                     #212126  panel header strips
#   #1F1F1F  text fields, dropdowns     #1A1A1A  viewer surround
#   #070707  field/checkbox borders     #090909  panel dividers
#   #43474D  pill-button outline        #929292  label / button text
#   #EAEAEB  values (#FFFFFF headers)   #48484A  disabled text
#   #E64B3D  accent red (page bar, loop button, selected-clip outline)
#
# Type is Open Sans, the closest match to Resolve's labels, bundled in
# app/assets/fonts so every machine has it: labels regular ~12px, values
# semibold ~13px, headers bold ~13px.
#
# States not listed above (hover, focus, an open dropdown, list
# selection) use neutral steps of the same greys, never the red.
_PALETTES["DaVinci"] = {
    "primary": "#E64B3D",
    "on_primary": "#FFFFFF",
    # Selection wash: the lightest grey step that still clears
    # MIN_SELECTION_CONTRAST against the panels (#3B3B42 lands at 1.30 and
    # would flip selection to the inverted bar).
    "primary_container": "#46464E",
    "on_primary_container": "#FFFFFF",
    "secondary": "#929292",
    "on_secondary": "#17181A",
    "secondary_container": "#323238",
    "surface": "#1C1C20",
    "on_surface": "#929292",
    "surface_container": "#28282E",
    "surface_container_high": "#1F1F1F",
    "outline": "#48484A",
    "outline_variant": "#090909",
}

RESOLVE = {
    "window": "#1C1C20", "toolbar": "#17181A", "panel": "#28282E", "header": "#212126",
    "field": "#1F1F1F", "field_border": "#070707", "divider": "#090909",
    "button_border": "#43474D", "button_border_hi": "#6A6E75", "hover": "#2F2F36",
    "label": "#929292", "value": "#EAEAEB", "bright": "#FFFFFF", "dim": "#48484A",
    "accent": "#E64B3D", "scroll": "#48484A", "scroll_hi": "#6A6A6E",
}

# Where each of Resolve's greys sits from the window grey, or from the
# panel's (the ones drawn on a panel, text included), as an RGB step. A dark
# Custom background, or a colour variant's tinted greys, takes the same
# steps, so the look survives a new base colour and DaVinci comes out
# exactly as above.
_ON_PANEL = ("header", "button_border", "button_border_hi", "hover", "label", "value", "dim")


def _rgb_step(color, base):
    return tuple(a - b for a, b in zip(_hex_to_rgb(color), _hex_to_rgb(base)))


def _stepped(base, step):
    return _rgb_to_hex(tuple(max(0, min(255, c + d)) for c, d in zip(_hex_to_rgb(base), step)))


# A light background can't take the dark steps (they'd make grey text paler
# still): its greys are blends instead - lines and text toward black,
# fields toward white.
_LIGHT_RESOLVE = {
    "toolbar": ("window", "#000000", 0.05), "header": ("panel", "#000000", 0.04),
    "field": ("window", "#FFFFFF", 0.7), "field_border": ("window", "#000000", 0.3),
    "divider": ("window", "#000000", 0.18), "button_border": ("panel", "#000000", 0.28),
    "button_border_hi": ("panel", "#000000", 0.45), "hover": ("panel", "#000000", 0.06),
    "label": ("window", "#000000", 0.62), "value": ("window", "#000000", 0.85),
    "bright": ("window", "#000000", 1.0), "dim": ("window", "#000000", 0.38),
    "scroll": ("window", "#000000", 0.25), "scroll_hi": ("window", "#000000", 0.4),
}


def resolve_colors(tokens):
    """Resolve's named greys (RESOLVE's keys) for a token set: the window
    is the palette's surface, the panels its surface_container and the
    accent its primary. DaVinci's tokens give RESOLVE itself."""
    window, panel = tokens["surface"], tokens["surface_container"]
    out = {"window": window, "panel": panel, "accent": tokens["primary"]}
    for key, value in RESOLVE.items():
        if key in out:
            continue
        if _is_light(window):
            base, toward, amount = _LIGHT_RESOLVE[key]
            out[key] = _blend(out[base], toward, amount)
        else:
            base = "panel" if key in _ON_PANEL else "window"
            out[key] = _stepped(out[base], _rgb_step(value, RESOLVE[base]))
    # A panel picked far from the window can still leave text faint.
    for key, target in (("label", 4.5), ("value", 7)):
        out[key] = ensure_contrast(out[key], panel, target)
    return out


# Default's colour variants: Resolve's greys washed a little toward a
# colour, and that colour as the accent where DaVinci has its red.
RESOLVE_VARIANTS = {
    "Blue": "#3D8BFF", "Teal": "#1FB5AC", "Green": "#4CB860", "Yellow": "#E8B931",
    "Orange": "#F07F2E", "Purple": "#9D6BFF", "Pink": "#EC5F9E",
}
RESOLVE_TINT = 0.06


def _resolve_variant(accent):
    base = _PALETTES["DaVinci"]
    palette = {key: _blend(value, accent, RESOLVE_TINT) for key, value in base.items()}
    palette["primary"] = accent
    palette["on_primary"] = "#FFFFFF" if contrast_ratio("#FFFFFF", accent) >= 3 else "#17181A"
    palette["on_primary_container"] = "#FFFFFF"
    return palette

# Keys are what settings.json stores (and what custom palettes are filed
# under), so they never change; "label" is the name shown in Settings.
THEMES = {
    "Resolve": {
        "label": "Default",
        "shape": "resolve",
        "status": "resolve",
        "selection": "container",
        "default_subtheme": "DaVinci",
        "subthemes": ["DaVinci", *RESOLVE_VARIANTS],
    },
    "Default": {
        "label": "Don't be evil",
        "shape": "default",
        "status": "default",
        "selection": "container",
        "default_subtheme": "Default",
        "subthemes": ["Default", "Ocean", "Sunset", "Forest"],
    },
    "Retro": {
        "label": "Retro",
        "shape": "retro",
        "status": "retro",
        "selection": "inverted",
        "default_subtheme": "Mulberry",
        "subthemes": ["Peach", "Bubblegum", "Licorice", "Mulberry"],
    },
    "SaaS": {
        "label": "Modern",
        "shape": "saas",
        "status": "saas",
        "selection": "container",
        "default_subtheme": "Midnight",
        "subthemes": ["Midnight", "Aurora", "Graphite", "Daylight"],
    },
    "Nova": {
        "label": "Nova",
        "shape": "nova",
        "status": "saas",
        "selection": "container",
        "default_subtheme": "Neon",
        "subthemes": ["Neon", "Nebula", "Ember"],
    },
    "Offworld": {
        "label": "Off-world",
        "shape": "offworld",
        "status": "offworld",
        # Phosphor on black: a selected row is the lit bar, text knocked out.
        "selection": "inverted",
        "default_subtheme": "Amber",
        "subthemes": ["Amber", "Green", "Ice"],
    },
    "Desktop": {
        "label": "Desktop",
        "shape": "desktop",
        # Ink-on-paper status colours, lifted for a dark Custom background.
        "status": "retro",
        "selection": "container",
        "default_subtheme": "Cocoa",
        "subthemes": ["Paper", "Mint", "Sky", "Dusk", "Arcade", "Cocoa"],
        # Tools open as floating windows instead of the rail's panes
        # (core/desktop_window.py). A theme without it uses the panes.
        "layout": "desktop",
    },
}

LAYOUTS = ("panes", "desktop")


def theme_layout(theme):
    """How the shell lays tools out under a theme: "panes" (the rail's tool,
    plus dual view) or "desktop" (floating windows)."""
    return (THEMES.get(theme) or {}).get("layout", "panes")


def desktop_colors(tokens):
    """{"desk", "windows": [4 title-bar colours], "ink", "text",
    "title_text"} for the desktop layout. "ink" draws lines and shadows,
    "text" is text on the palette's surfaces, "title_text" is text on a
    candy title bar (always dark). Presets name the desk and title bars;
    Custom derives them - the desk a step toward the text colour, the title
    bars the accent turned round the wheel."""
    windows = [tokens.get(f"window_{i}") for i in range(1, 5)]
    # Presets draw their ink themselves. A Custom palette's outline is a
    # mid-grey step from the background - on a dark one that would make
    # the hard shadows lighter than the desk - so it inks in near-black on
    # dark, and in its text colour on light (the presets' own choice).
    if all(windows):
        ink = tokens["outline"]
    elif _is_light(tokens["surface"]):
        ink = tokens["on_surface"]
    else:
        ink = _blend(tokens["surface"], "#000000", 0.7)
    if not all(windows):
        base = tokens["primary"]
        windows = [_readable_under(c, tokens["on_primary"]) for c in
                   [base] + [_rotate_hue(base, turn) for turn in (0.33, 0.58, 0.83)]]
    return {
        "desk": tokens.get("desk") or _blend(tokens["surface"], tokens["on_surface"], 0.06),
        "windows": windows,
        "ink": ink,
        "text": tokens["on_surface"],
        "title_text": tokens["on_primary"],
    }


def _readable_under(fill, text, target=4.5):
    """`fill` lightened just until dark `text` reads on it - a derived title
    bar can land on a deep colour the title text vanishes into."""
    for _ in range(12):
        if contrast_ratio(text, fill) >= target:
            break
        fill = _blend(fill, "#FFFFFF", 0.12)
    return fill


def _rotate_hue(hex_color, turn):
    r, g, b = (c / 255 for c in _hex_to_rgb(hex_color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return _rgb_to_hex(tuple(round(c * 255) for c in colorsys.hls_to_rgb((h + turn) % 1, l, s)))


# What a fresh install (and "Reset to Default") uses.
DEFAULT_THEME = "Resolve"


def list_themes():
    return list(THEMES)


def theme_label(theme):
    """The name Settings shows for a theme key."""
    return (THEMES.get(theme) or {}).get("label", theme)


def list_subthemes(theme="Default"):
    """Palettes available under a theme, Custom always last."""
    spec = THEMES.get(theme) or THEMES["Default"]
    return list(spec["subthemes"]) + [SUBTHEME_CUSTOM]


def default_subtheme(theme="Default"):
    spec = THEMES.get(theme) or THEMES["Default"]
    return spec["default_subtheme"]


# Dual view's right-hand pane can be washed slightly so the two tools are
# easy to tell apart. Keys are what settings.json stores ("split_tint");
# the labels are what Settings shows.
SIDE_PANE_TINTS = {"off": "Off", "lighter": "Lighter", "darker": "Darker", "accent": "Accent"}
DEFAULT_SIDE_PANE_TINT = "lighter"


def side_pane_tint(tokens, key):
    """(colour, alpha 0-255) to wash the right-hand pane with, or None.

    A wash, not a new palette: it goes over whatever the tool paints,
    including the inline colours some tools set themselves, so every
    tool shifts the same way. Kept light enough that text still reads."""
    if key == "lighter":
        return ("#FFFFFF", 22) if not _is_light(tokens["surface"]) else ("#FFFFFF", 40)
    if key == "darker":
        return ("#000000", 45) if not _is_light(tokens["surface"]) else ("#000000", 16)
    if key == "accent":
        return (tokens["primary"], 18)
    return None


def _relative_luminance(hex_str):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _hex_to_rgb(hex_str))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    """WCAG contrast between two colors, 1.0 (identical) to 21.0."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ensure_contrast(hex_color, surface, target=4.5):
    """Same hue, adjusted in lightness until it is readable on surface.

    A palette's accent is chosen to look right as a fill - Retro's cyan on
    peach is 2.1:1, fine for a button body and too weak for a text label.
    Hue and saturation are preserved so the theme still reads as itself.
    """
    if contrast_ratio(hex_color, surface) >= target:
        return hex_color
    r, g, b = _hex_to_rgb(hex_color)
    hue, lightness, saturation = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    step = -0.02 if _is_light(surface) else 0.02
    for _ in range(50):
        lightness = min(1.0, max(0.0, lightness + step))
        candidate = _rgb_to_hex(
            tuple(round(c * 255) for c in colorsys.hls_to_rgb(hue, lightness, saturation))
        )
        if contrast_ratio(candidate, surface) >= target:
            return candidate
        if lightness in (0.0, 1.0):
            break
    return candidate


def hue_distance(a, b):
    """0..0.5 - how far apart two colors are on the hue wheel."""
    ha = colorsys.rgb_to_hls(*[c / 255 for c in _hex_to_rgb(a)])[0]
    hb = colorsys.rgb_to_hls(*[c / 255 for c in _hex_to_rgb(b)])[0]
    gap = abs(ha - hb) % 1.0
    return min(gap, 1.0 - gap)


def complementary_color(hex_color, surface="#141218"):
    """The hue opposite hex_color, set to a lightness that reads on surface.

    Used to tell two speakers apart in the chat transcript. Lightness is
    taken from the background rather than the source color, because a
    complement that merely differs in hue can still be illegible - and a
    near-grey accent has no meaningful complement at all, hence the
    saturation floor.
    """
    r, g, b = _hex_to_rgb(hex_color)
    hue, _lightness, saturation = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    hue = (hue + 0.5) % 1.0
    saturation = max(saturation, 0.45)
    lightness = 0.34 if _is_light(surface) else 0.72
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    return _rgb_to_hex((round(r * 255), round(g * 255), round(b * 255)))


def _surface_steps(surface):
    """(panel, raised) blend factors toward white for a page colour.

    A near-white surface has almost no headroom left toward white, so the
    0.08 / 0.14 that read clearly on a near-black page vanish on a light
    one. Measured on Retro's #F6E0C8, the old 0.08 produced #F6E2CC - a
    two-point change, where the hand-drawn preset steps to #FBEEDC.

    The dark numbers are unchanged, so every existing dark Custom palette
    renders exactly as before.
    """
    if _is_light(surface):
        return 0.35, 0.55
    return 0.08, 0.14


def derive_panel(surface, theme=None):
    """The panel colour Custom uses when the user has not picked one.
    Under Default (Resolve's look) on a dark background it's Resolve's own
    step from window to panel, so Custom starts out as DaVinci."""
    if theme in THEMES and THEMES[theme]["shape"] == "resolve" and not _is_light(surface):
        return _stepped(surface, _rgb_step(RESOLVE["panel"], RESOLVE["window"]))
    return _blend(surface, "#FFFFFF", _surface_steps(surface)[0])


def _is_light(hex_str):
    """Perceived luminance test, for deciding which way to blend."""
    r, g, b = _hex_to_rgb(hex_str)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5


def custom_defaults(theme="Default"):
    """(accent, background) a Custom palette starts from under this theme.

    Seeded from the theme's own default subtheme, so Custom under Retro is
    peach paper rather than the Default theme's near-black - picking Custom
    should change what you can adjust, not jump to another theme's look.
    """
    base = _PALETTES[default_subtheme(theme)]
    return base["primary"], base["surface"]


def resolve(theme, subtheme):
    """Clamp a (theme, subtheme) pair to something that exists.

    Settings can hold a stale pair - a theme renamed, or a subtheme that
    belonged to the other theme before the user switched - and the app must
    not fall over on a bad combination.
    """
    if theme not in THEMES:
        theme = DEFAULT_THEME
    if subtheme not in list_subthemes(theme):
        subtheme = default_subtheme(theme)
    return theme, subtheme



def _hex_to_rgb(hex_str):
    hex_clean = hex_str.lstrip('#')
    if len(hex_clean) == 6:
        return tuple(int(hex_clean[i:i + 2], 16) for i in (0, 2, 4))
    return (128, 128, 128)


def _rgb_to_hex(rgb):
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _blend(c1, c2, factor):
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    r = int(r1 + (r2 - r1) * factor)
    g = int(g1 + (g2 - g1) * factor)
    b = int(b1 + (b2 - b1) * factor)
    return _rgb_to_hex((max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))))


for _name, _accent in RESOLVE_VARIANTS.items():
    _PALETTES[_name] = _resolve_variant(_accent)


# Status colours live outside the colour-role token set but still have to
# follow the theme - pastels tuned for a dark surface are unreadable on the
# Retro theme's peach paper. Defaulted here so Custom and every existing
# preset keep exactly the colours they use today.
DEFAULT_STATUS_COLORS = {
    "success": "#A3D5AD",
    "danger": "#F2B8B5",
    "overlay_text": "#FFFFFF",
    "hairline": "rgba(255,255,255,20)",
    "swatch_border": "rgba(255,255,255,60)",
}

STATUS_BY_THEME = {
    "retro": {
        "success": "#1E7A4C",
        "danger": "#C23A57",
        "overlay_text": "#2E2A3B",
        "hairline": "rgba(46,42,59,60)",
        "swatch_border": "rgba(46,42,59,140)",
    },
}

# The retro family's dark half (Licorice/Mulberry, and Custom with a dark
# background under Retro) cannot use the light-paper status set above: the
# ink-dark overlay text and dark green/red would vanish on dark surfaces.
# These are the same hues at dark-surface lightness - the same move the
# family's palettes make - so status reads on paper of either polarity.
DARK_STATUS_COLORS = {
    "success": "#5DBB7E",
    "danger": "#F0788F",
    "overlay_text": "#F0E7D8",
    "hairline": "rgba(240,231,216,40)",
    "swatch_border": "rgba(240,231,216,90)",
}


# SaaS: the saturated emerald/coral a product dashboard uses for status,
# at dark-surface lightness - with a deepened set for Daylight (or a light
# Custom background), picked by surface lightness like Retro's.
SAAS_STATUS_COLORS = {
    "success": "#34D399",
    "danger": "#F87171",
    "overlay_text": "#EAF2F7",
    "hairline": "rgba(143,168,186,40)",
    "swatch_border": "rgba(234,242,247,70)",
}

SAAS_LIGHT_STATUS_COLORS = {
    "success": "#0F8A5F",
    "danger": "#D14343",
    "overlay_text": "#142433",
    "hairline": "rgba(20,36,51,40)",
    "swatch_border": "rgba(20,36,51,110)",
}


# Resolve's own greens/reds: the render queue's "Completed" green lifted to
# read on the panel grey (as drawn it is #1C7524, 2.4:1), and the accent red.
RESOLVE_STATUS_COLORS = {
    "success": "#3FAE4B",
    "danger": "#E64B3D",
    "overlay_text": "#FFFFFF",
    "hairline": "rgba(255,255,255,14)",
    "swatch_border": "rgba(255,255,255,50)",
}


# Off-world's status set for a Custom palette (the presets carry their own,
# tuned against their phosphor): a lime and a hot red that read on black.
OFFWORLD_STATUS_COLORS = {
    "success": "#9CE35B",
    "danger": "#FF4545",
    "overlay_text": "#F2E6D8",
    "hairline": "rgba(255,255,255,16)",
    "swatch_border": "rgba(255,255,255,60)",
}


# Status set per THEMES[...]["status"] name, as (dark surface, light
# surface). Picked by the palette's surface lightness, not its name, so a
# Custom background of either polarity gets a set that reads on it. A name
# missing here gets DEFAULT_STATUS_COLORS; every set is laid over those, so
# it only has to name what it changes.
STATUS_SETS = {
    "retro": (DARK_STATUS_COLORS, STATUS_BY_THEME["retro"]),
    "saas": (SAAS_STATUS_COLORS, SAAS_LIGHT_STATUS_COLORS),
    "resolve": (RESOLVE_STATUS_COLORS, RESOLVE_STATUS_COLORS),
    "offworld": (OFFWORLD_STATUS_COLORS, OFFWORLD_STATUS_COLORS),
}


def _status_colors(family, surface):
    """Status set for a theme family on a specific palette surface.

    The retro set is authored for light paper; the dark palettes in that
    same family need the lifted set or status text/colours silently
    disappear. Selection is by surface lightness (the same test the
    Custom derivation uses for blend direction), not by subtheme name.
    """
    dark, light = STATUS_SETS.get(family, ({}, {}))
    return {**DEFAULT_STATUS_COLORS, **(light if _is_light(surface) else dark)}

# How a selected row is drawn. Two genuinely different idioms, not two
# shades of one - which is why this is a theme property rather than a
# contrast threshold:
#
#   container  a tinted wash behind the row. It reads
#              clearly on the dark themes because a saturated purple block
#              against near-black shifts hue and saturation as much as
#              luminance, which a contrast ratio alone does not capture.
#   inverted   the classic ink-on-paper selection bar. Retro's palette is
#              light-on-light, so a wash has nothing to work with: measured
#              against the row background its tint reaches only 1.42:1, and
#              even a solid primary fill manages 2.39:1. Inverting is both
#              the readable answer and the period-correct one.
# Named on each THEMES entry under "selection"; there is no family
# indirection here, because the idiom is a property of the theme itself
# rather than of a palette family it shares with others.
SELECTION_STYLES = ("container", "inverted")

# Only a hand-picked Custom accent/background can land below this; every
# shipped preset is well clear of it (the lowest is Retro/Custom at 1.64).
# Under it a selection is invisible whatever the theme asked for, so the
# inverted bar is forced.
MIN_SELECTION_CONTRAST = 1.35


def _with_selection(tokens, style):
    """Add selection_bg / selection_fg to a finished token set.

    Kept separate from the palette tables because it is derived: a preset
    only has to declare its primary and surface, and both selection idioms
    fall out of tokens that already exist.
    """
    if style == "inverted":
        bg, fg = tokens["on_surface"], tokens["surface"]
    else:
        bg, fg = tokens["primary_container"], tokens["on_primary_container"]

    if contrast_ratio(bg, tokens["surface_container"]) < MIN_SELECTION_CONTRAST:
        bg, fg = tokens["on_surface"], tokens["surface"]

    return {
        **tokens,
        "selection_bg": bg,
        # The fallback above can pair colours the palette never intended, so
        # the text is checked against whatever background it ended up on.
        "selection_fg": ensure_contrast(fg, bg),
    }


def get_theme_tokens(theme="Default", subtheme=None, custom_accent=None,
                     custom_bg=None, custom_panel=None):
    """Colour tokens for a (theme, subtheme) pair.

    Exposed so tool pages can match the shell's actual current colours
    (painted icons, chart bars) instead of hardcoding anything. Status
    colours come from the THEME, not the subtheme: they belong to the
    overall look, and a Custom palette under Retro still needs Retro's
    dark-on-light status text to be readable.
    """
    theme, subtheme = resolve(theme, subtheme)
    family = THEMES[theme]["status"]
    selection = THEMES[theme].get("selection", "container")
    if subtheme != SUBTHEME_CUSTOM:
        palette = _PALETTES[subtheme]
        status = _status_colors(family, palette["surface"])
        return _with_selection({**status, **palette}, selection)

    # Custom derives from the user's own accent/background, seeded from the
    # theme's own palette so a blank custom setting isn't a dark purple
    # surprise under Retro.
    base = _PALETTES[default_subtheme(theme)]
    primary = custom_accent or base["primary"]
    surface = custom_bg or base["surface"]
    status = _status_colors(family, surface)

    # Which way "more contrast" points depends on the background. The old
    # derivation always blended toward white, which is right on a near-black
    # surface and gives unreadable white-on-cream once a light background is
    # possible. Containers still go toward white either way - that is how
    # the hand-written light palettes raise a surface, too.
    ink = "#000000" if _is_light(surface) else "#FFFFFF"

    # Panels sit above the page. The presets draw these as two separate
    # hand-picked colours (Retro is #F6E0C8 behind #FBEEDC), so Custom gets
    # to pick the second one too rather than only ever deriving it.
    raised_step = _surface_steps(surface)[1]
    if custom_panel:
        panel = custom_panel
        # One more step up from whatever they chose, so a raised surface is
        # still distinguishable from a panel.
        raised = _blend(panel, "#FFFFFF", _surface_steps(panel)[0])
    else:
        panel = derive_panel(surface, theme)
        raised = _blend(surface, "#FFFFFF", raised_step)

    return _with_selection({
        **status,
        "primary": primary,
        "on_primary": _blend(primary, "#000000", 0.75),
        "primary_container": _blend(primary, surface, 0.4),
        "on_primary_container": _blend(primary, "#FFFFFF", 0.6),
        "secondary": _blend(primary, "#FFFFFF", 0.3),
        "on_secondary": _blend(surface, "#000000", 0.5),
        "secondary_container": _blend(primary, surface, 0.25),
        "surface": surface,
        "on_surface": _blend(surface, ink, 0.85),
        "surface_container": panel,
        "surface_container_high": raised,
        "outline": _blend(surface, ink, 0.45),
        "outline_variant": _blend(surface, ink, 0.25),
    }, selection)


def _checkbox_check_asset_path(color_hex):
    """Renders a small X-mark PNG in the given color and returns its path,
    for use as a QCheckBox::indicator:checked image (Qt's QSS engine
    doesn't render data: URIs, only real file paths). Cached per-color
    under the system temp dir so repeat theme applies don't re-render it."""
    fname = f"buddy_theme_checkbox_x_{color_hex.lstrip('#').upper()}.png"
    path = os.path.join(tempfile.gettempdir(), fname)
    if not os.path.exists(path):
        size = 18
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(color_hex))
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        margin = 4.5
        painter.drawLine(int(margin), int(margin), size - int(margin), size - int(margin))
        painter.drawLine(size - int(margin), int(margin), int(margin), size - int(margin))
        painter.end()
        pixmap.save(path)
    return path.replace("\\", "/")


# --- shape ----------------------------------------------------------------
#
# Colors alone can't express a theme like the retro one: its identity is
# square corners and thick outlines, not its palette. These are the knobs
# the QSS template reads for everything that isn't a color.
#
# DEFAULT_SHAPE holds exactly the literals the template used to hardcode, so
# every existing preset renders byte-for-byte identically and only a preset
# that opts in via SHAPE_PRESETS looks different.
DEFAULT_SHAPE = {
    "r_xs": "5px",
    "r_sm": "6px",
    "r_md": "8px",
    "r_lg": "12px",
    "r_xl": "14px",
    "r_2xl": "16px",
    "r_pill": "18px",
    "r_round": "20px",
    # Fully rounded things in the web pages - chips, badges, counters,
    # progress bars, numbered steps. Square families square them off.
    "r_full": "999px",
    "bw": "1px",
    "bw_thick": "2px",
    "bw_accent": "6px",
    "font": '"Segoe UI", "SF Pro Display", Roboto, Arial, sans-serif',
    # Points, not pixels. A px font-size leaves QFont.pointSize() == -1, and
    # the first time a QComboBox popup is built Qt copies that -1 into
    # setPointSize() and logs "Point size <= 0" once per run. 9.75pt is the
    # same 13px at 96 DPI (verified: identical height, ascent and advance);
    # unlike px it also scales on a high-DPI display.
    "font_size": "9.75pt",
    # "solid": every surface is a flat colour. "gradient": the window gets
    # a radial glow and cards a vertical sheen (SaaS) - see get_app_theme.
    "backdrop": "solid",
}

SHAPE_BY_THEME = {
    "default": DEFAULT_SHAPE,
    # Square everything, one heavy outline weight throughout. The look
    # draws every edge at the same thickness - varying it is what makes
    # a flat theme read as "unfinished modern" rather than deliberate.
    "retro": {
        "r_xs": "0px", "r_sm": "0px", "r_md": "0px", "r_lg": "0px",
        "r_xl": "0px", "r_2xl": "0px", "r_pill": "0px", "r_round": "0px", "r_full": "0px",
        "bw": "2px", "bw_thick": "3px", "bw_accent": "6px",
        "font": '"Verdana", "DejaVu Sans", "Segoe UI", sans-serif',
        "font_size": "9.75pt",
    },
    # Soft, even rounding (cards ~14px, controls ~10px), hairline borders,
    # and a geometric UI sans - Inter where installed, else Windows 11's
    # Segoe UI Variable, which has the same open, even-width feel.
    "saas": {
        "r_xs": "4px", "r_sm": "6px", "r_md": "8px", "r_lg": "10px",
        "r_xl": "10px", "r_2xl": "14px", "r_pill": "14px", "r_round": "16px",
        "bw": "1px", "bw_thick": "2px", "bw_accent": "5px",
        "font": '"Inter", "Segoe UI Variable Display", "Segoe UI", Roboto, sans-serif',
        "font_size": "9.75pt",
        "backdrop": "gradient",
    },
    # Resolve: square panels, 2px field corners, pill buttons (r_xl), 1px
    # everything. Open Sans is bundled (app/assets/fonts), 13px like
    # Resolve's own text at 100% scaling.
    "resolve": {
        "r_xs": "2px", "r_sm": "2px", "r_md": "2px", "r_lg": "2px",
        "r_xl": "11px", "r_2xl": "0px", "r_pill": "0px", "r_round": "0px", "r_full": "2px",
        "bw": "1px", "bw_thick": "1px", "bw_accent": "2px",
        "font": '"Open Sans", "Segoe UI", Arial, sans-serif',
        # 12px, Resolve's label size at 100% scaling.
        "font_size": "9pt",
    },
    # Nova: Modern's gradient backdrop with rounder, softer shapes. The
    # glass (blur, translucency, glow) only exists in the web pages - Qt
    # stylesheets can't blur what is behind a widget.
    "nova": {
        "r_xs": "6px", "r_sm": "8px", "r_md": "10px", "r_lg": "12px",
        "r_xl": "14px", "r_2xl": "20px", "r_pill": "20px", "r_round": "24px",
        "bw": "1px", "bw_thick": "2px", "bw_accent": "4px",
        "font": '"Inter", "Segoe UI Variable Display", "Segoe UI", Roboto, sans-serif',
        "font_size": "9.75pt",
        "backdrop": "gradient",
    },
    # Off-world: a terminal - square hairline boxes and monospace type.
    # Cascadia Mono ships with Windows 11 (and Terminal); Consolas with
    # every Windows since Vista.
    "offworld": {
        "r_xs": "0px", "r_sm": "0px", "r_md": "0px", "r_lg": "0px",
        "r_xl": "0px", "r_2xl": "0px", "r_pill": "0px", "r_round": "0px", "r_full": "0px",
        "bw": "1px", "bw_thick": "1px", "bw_accent": "2px",
        "font": '"Cascadia Mono", Consolas, "Lucida Console", monospace',
        "font_size": "9.75pt",
    },
    # Desktop: friendly rounded boxes with a heavy ink line; the windows
    # themselves are drawn by core/desktop_window.py.
    "desktop": {
        "r_xs": "4px", "r_sm": "6px", "r_md": "8px", "r_lg": "10px",
        "r_xl": "10px", "r_2xl": "12px", "r_pill": "14px", "r_round": "14px",
        "bw": "2px", "bw_thick": "2px", "bw_accent": "4px",
        "font": '"Segoe UI Variable Display", "Segoe UI", "Trebuchet MS", sans-serif',
        "font_size": "9.75pt",
    },
}


def get_shape_tokens(theme="Default"):
    """Non-color values for a THEME (not a subtheme) - shape is what makes
    a theme a theme; a subtheme only ever changes colours."""
    family = (THEMES.get(theme) or THEMES["Default"])["shape"]
    return {**DEFAULT_SHAPE, **SHAPE_BY_THEME.get(family, {})}


FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
_fonts_loaded = False


def load_bundled_fonts():
    """Register the fonts Buddy ships (Open Sans, for the Resolve theme) with
    Qt, once. A theme that names a font nobody installed would silently
    fall back to something else - bundling makes "Open Sans" mean Open Sans
    on every machine. SIL Open Font License; see assets/fonts/OFL.txt."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    from PySide6.QtGui import QFontDatabase
    if os.path.isdir(FONTS_DIR):
        for name in sorted(os.listdir(FONTS_DIR)):
            if name.lower().endswith((".ttf", ".otf")):
                QFontDatabase.addApplicationFont(os.path.join(FONTS_DIR, name))
    _fonts_loaded = True


def get_app_theme(theme="Default", subtheme=None, custom_accent=None,
                  custom_bg=None, custom_panel=None):
    load_bundled_fonts()
    theme, subtheme = resolve(theme, subtheme)
    tokens = get_theme_tokens(theme, subtheme, custom_accent, custom_bg,
                              custom_panel)
    shape = get_shape_tokens(theme)
    checkbox_x_path = _checkbox_check_asset_path(tokens['on_primary'])

    # Backdrop. For "solid" these are exactly the flat colours the template
    # always used, so Default/Retro render byte-for-byte as before.
    window_bg = tokens['surface']
    stack_bg = tokens['surface']
    card_bg = tokens['surface_container']
    extra = ""
    if shape.get("backdrop") == "gradient":
        # Custom palettes have no hand-picked glow: derive one by leaning the
        # page colour a little toward the accent, as the presets do.
        glow = tokens.get("surface_glow") or _blend(tokens['surface'], tokens['primary'], 0.18)
        window_bg = (
            "qradialgradient(cx:0.78, cy:0.95, radius:1.15, fx:0.78, fy:0.95, "
            f"stop:0 {glow}, stop:0.55 {_blend(glow, tokens['surface'], 0.6)}, "
            f"stop:1 {tokens['surface']})"
        )
        stack_bg = "transparent"
        card_bg = (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {tokens['surface_container_high']}, stop:1 {tokens['surface_container']})"
        )
        extra = _saas_extra_rules(tokens, shape)
    if THEMES[theme]["shape"] in QT_EXTRA_RULES:
        extra = QT_EXTRA_RULES[THEMES[theme]["shape"]](tokens, shape)

    return f"""
QWidget {{
    background-color: {tokens['surface']};
    color: {tokens['on_surface']};
    font-family: {shape['font']};
    font-size: {shape['font_size']};
    selection-background-color: {tokens['primary_container']};
    selection-color: {tokens['on_primary_container']};
}}

QToolTip {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['on_surface']};
    border: {shape['bw']} solid {tokens['outline']};
    /* Square: a tooltip is a window of its own and can't be see-through,
       so round corners left its square ones showing past the border. */
    border-radius: 0px;
    padding: 6px;
    font-size: 12px;
}}

QMainWindow, QDialog {{
    background-color: {window_bg};
}}

QFrame, .QGroupBox {{
    background-color: {card_bg};
    border: {shape['bw']} solid {tokens['outline_variant']};
    border-radius: {shape['r_2xl']};
}}

/* QColorDialog's current-colour preview is a QFrame, so it inherits the
   rounded card above - but Qt paints its fill natively, unclipped, so the
   colour squares off past the curve. From Color Palette Manager's theme. */
QColorDialog QFrame {{
    border-radius: 0px;
}}

QStackedWidget {{
    background-color: {stack_bg};
    border: none;
}}

QLabel {{
    border: none;
    background-color: transparent;
    color: {tokens['on_surface']};
}}

#headerFrame {{
    background-color: {tokens['surface_container']};
    border: {shape['bw']} solid {tokens['outline_variant']};
    border-radius: {shape['r_pill']};
}}

#navRail {{
    background-color: {tokens['surface_container']};
    border: {shape['bw']} solid {tokens['outline_variant']};
    border-radius: {shape['r_round']};
}}

QLabel#navGroupLabel {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    color: {tokens['outline']};
    padding: 10px 10px 2px 10px;
    text-transform: uppercase;
}}

QPushButton#navButton {{
    background-color: transparent;
    border: none;
    border-radius: {shape['r_2xl']};
    padding: 10px 16px;
    text-align: left;
    color: {tokens['on_surface']};
    font-weight: 600;
    font-size: 14px;
}}

QPushButton#navButton:hover {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['primary']};
}}

QPushButton#navButton[active="true"] {{
    background-color: {tokens['primary_container']};
    color: {tokens['on_primary_container']};
    border: {shape['bw']} solid {tokens['primary']};
}}

QPushButton#navButton:disabled {{
    color: {tokens['outline']};
}}

QPushButton {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['on_surface']};
    border: {shape['bw']} solid {tokens['outline_variant']};
    border-radius: {shape['r_xl']};
    padding: 7px 16px;
    font-weight: 600;
}}

QPushButton:hover {{
    background-color: {tokens['secondary_container']};
    border-color: {tokens['outline']};
    color: {tokens['primary']};
}}

QPushButton:pressed {{
    background-color: {tokens['primary_container']};
}}

QPushButton:disabled {{
    background-color: {tokens['surface_container']};
    color: {tokens['outline']};
    border-color: transparent;
}}

QPushButton#accentButton {{
    background-color: {tokens['primary']};
    color: {tokens['on_primary']};
    border: none;
    border-radius: {shape['r_xl']};
    font-weight: 700;
}}

QPushButton#accentButton:hover {{
    background-color: {tokens['on_primary_container']};
    color: {tokens['primary_container']};
}}

QPushButton#accentButton:pressed {{
    background-color: {tokens['primary_container']};
    color: {tokens['on_primary_container']};
}}

/* Without this the accent fill wins over QPushButton:disabled, so a
   disabled primary action (e.g. Transcribe before setup) looked live. */
QPushButton#accentButton:disabled {{
    background-color: {tokens['surface_container']};
    color: {tokens['outline']};
    border: {shape['bw']} solid {tokens['outline_variant']};
}}

QPushButton#iconButton {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['on_surface']};
    border: {shape['bw']} solid {tokens['outline_variant']};
    border-radius: {shape['r_xl']};
    padding: 0px;
    font-size: 16px;
}}

QPushButton#iconButton:hover {{
    background-color: {tokens['secondary_container']};
    border-color: {tokens['outline']};
    color: {tokens['primary']};
}}

/* A toggle among the icon buttons (the header's dual view): lit while on. */
QPushButton#iconButton:checked {{
    background-color: {tokens['secondary_container']};
    border-color: {tokens['primary']};
}}

QPushButton#dangerButton {{
    background-color: #F2B8B5;
    color: #601410;
    border: none;
    border-radius: {shape['r_xl']};
    font-weight: 700;
}}

QPushButton#dangerButton:hover {{
    background-color: #F9DEDC;
}}

QPushButton#successButton {{
    background-color: {tokens['primary_container']};
    color: {tokens['on_primary_container']};
    border: {shape['bw']} solid {tokens['primary']};
    border-radius: {shape['r_xl']};
    font-weight: 700;
}}

QPushButton#successButton:hover {{
    background-color: {tokens['primary']};
    color: {tokens['on_primary']};
}}

QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['on_surface']};
    border: {shape['bw']} solid {tokens['outline']};
    border-radius: {shape['r_lg']};
    padding: 6px 12px;
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: {shape['bw_thick']} solid {tokens['primary']};
    background-color: {tokens['surface_container']};
}}

QComboBox {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['on_surface']};
    border: {shape['bw']} solid {tokens['outline']};
    border-radius: {shape['r_lg']};
    padding: 6px 12px;
    padding-right: 28px;
}}

QComboBox:hover {{
    border-color: {tokens['primary']};
    background-color: {tokens['secondary_container']};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: none;
}}

QComboBox::down-arrow {{
    image: none;
    border-top: {shape['bw_accent']} solid {tokens['primary']};
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    margin-right: 10px;
}}

QComboBox QAbstractItemView {{
    background-color: {tokens['surface_container_high']};
    border: {shape['bw']} solid {tokens['outline']};
    border-radius: {shape['r_lg']};
    selection-background-color: {tokens['selection_bg']};
    selection-color: {tokens['selection_fg']};
    outline: none;
    padding: 6px;
}}

/* Dropdown rows (for any Qt dropdown still shown - native pickers). Qt's
   menu-style delegate ignores ::item rules, so these only apply where a
   QStyledItemDelegate is set. More specific than the QListView::item rules below, which
   matters: their hover colour IS the popup's background, so a hovered row
   used to look like every other row. The popup moves its current item to
   whatever is under the cursor, so :selected tracks the pointer and
   :hover covers the moment before it does - both get the same highlight,
   the theme's own selection idiom (a tint, or Retro's inverted bar). */
QComboBox QAbstractItemView::item {{
    min-height: 24px;
    padding: 4px 10px;
    border: none;
    border-radius: {shape['r_md']};
    color: {tokens['on_surface']};
}}

QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected,
QComboBox QAbstractItemView::item:selected:!active {{
    background-color: {tokens['selection_bg']};
    color: {tokens['selection_fg']};
}}

/* Item views - the staging lists, clip tables and marker trees in tool
   pages. QTreeWidget/QListWidget/QTableWidget are covered by their view
   base classes, so one rule reaches every ported tool.

   Before this there was no ::item rule at all and selection fell through
   to QWidget's selection-background-color. That reads on the dark themes
   and effectively vanishes on Retro, whose tint lands at 1.42:1 against
   the row behind it; selection_bg is picked per theme to fix that. */
QTreeView, QListView, QTableView {{
    outline: none;
}}

QTreeView::item, QListView::item, QTableView::item {{
    border: none;
    outline: none;
}}

QTreeView::item:hover, QListView::item:hover, QTableView::item:hover {{
    background-color: {tokens['surface_container_high']};
}}

/* :!active as well as the default :active state. Qt dims an inactive
   selection, so without this the highlighted row fades the instant focus
   moves to a button - which is exactly when the user is about to act on
   it. Listed after :hover so a selected row stays selected under the
   cursor; equal specificity in QSS is resolved by source order. */
QTreeView::item:selected, QTreeView::item:selected:!active,
QListView::item:selected, QListView::item:selected:!active,
QTableView::item:selected, QTableView::item:selected:!active {{
    background-color: {tokens['selection_bg']};
    color: {tokens['selection_fg']};
}}

QLabel#titleLabel {{
    font-size: 18px;
    font-weight: 700;
    color: {tokens['primary']};
}}

QLabel#sectionLabel {{
    font-size: 15px;
    font-weight: 700;
    color: {tokens['on_surface']};
}}

QLabel#subLabel {{
    font-size: 12px;
    color: {tokens['outline']};
}}

QCheckBox {{
    spacing: 10px;
    color: {tokens['on_surface']};
    background-color: transparent;
}}

QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: {shape['bw_thick']} solid {tokens['outline']};
    border-radius: {shape['r_sm']};
    background-color: {tokens['surface_container_high']};
}}

QCheckBox::indicator:hover {{
    border-color: {tokens['primary']};
}}

QCheckBox::indicator:checked {{
    background-color: {tokens['primary']};
    border-color: {tokens['primary']};
    image: url({checkbox_x_path});
}}

/* Checkable rows in list/tree views (e.g. Organize sidebar) - the same
   box as a QCheckBox. Unstyled, Qt draws only a bare tick with no box,
   which doesn't read as something you can click. */
QListView::indicator, QTreeView::indicator {{
    width: 16px;
    height: 16px;
    border: {shape['bw_thick']} solid {tokens['outline']};
    border-radius: {shape['r_sm']};
    background-color: {tokens['surface_container_high']};
}}

QListView::indicator:hover, QTreeView::indicator:hover {{
    border-color: {tokens['primary']};
}}

QListView::indicator:checked, QTreeView::indicator:checked {{
    background-color: {tokens['primary']};
    border-color: {tokens['primary']};
    image: url({checkbox_x_path});
}}

QScrollBar:vertical {{
    background-color: transparent;
    width: 10px;
    margin: 6px 2px 6px 0px;
}}

QScrollBar::handle:vertical {{
    background-color: {tokens['outline_variant']};
    min-height: 24px;
    border-radius: {shape['r_xs']};
}}

QScrollBar::handle:vertical:hover {{
    background-color: {tokens['primary']};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background-color: transparent;
    height: 10px;
    margin: 0px;
}}

QScrollBar::handle:horizontal {{
    background-color: {tokens['outline_variant']};
    min-width: 24px;
    border-radius: {shape['r_xs']};
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {tokens['primary']};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

QScrollArea {{
    background-color: transparent;
    border: none;
}}

/* ---------------------------------------------------------------- Text
   Animator: checkable QGroupBox sections, radio buttons, sliders, the
   page's inner nav-rail separator, and its collapsible "More Style
   Options" toggle. Ported from the standalone tool's own theme.py,
   restated in Buddy's token/shape dialect. No other page used these
   widget classes before Text Animator, so these rules cannot surprise
   one - the first tool to need a given widget adds its rules here. */
QGroupBox {{
    font-weight: 700;
    margin-top: 10px;
    padding-top: 14px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: {tokens['primary']};
}}

QGroupBox::indicator {{
    width: 16px;
    height: 16px;
    border: {shape['bw_thick']} solid {tokens['outline']};
    border-radius: 5px;
    background-color: {tokens['surface_container_high']};
}}

QGroupBox::indicator:checked {{
    background-color: {tokens['primary']};
    border-color: {tokens['primary']};
}}

QRadioButton {{
    spacing: 10px;
    color: {tokens['on_surface']};
    background-color: transparent;
}}

QRadioButton::indicator {{
    width: 18px;
    height: 18px;
    border: {shape['bw_thick']} solid {tokens['outline']};
    border-radius: 9px;
    background-color: {tokens['surface_container_high']};
}}

QRadioButton::indicator:hover {{
    border-color: {tokens['primary']};
}}

QRadioButton::indicator:checked {{
    background-color: {tokens['primary']};
    border: 5px solid {tokens['surface_container_high']};
}}

QSlider {{
    background-color: transparent;
}}

QSlider::groove:horizontal {{
    height: 8px;
    background-color: {tokens['surface_container_high']};
    border-radius: 4px;
}}

QSlider::sub-page:horizontal {{
    background-color: {tokens['primary']};
    border-radius: 4px;
}}

QSlider::add-page:horizontal {{
    background-color: {tokens['surface_container_high']};
    border-radius: 4px;
}}

QSlider::handle:horizontal {{
    background-color: {tokens['primary']};
    border: {shape['bw_thick']} solid {tokens['surface']};
    width: 20px;
    height: 20px;
    margin: -6px 0;
    border-radius: 10px;
}}

QSlider::handle:horizontal:hover {{
    background-color: {tokens['on_primary_container']};
}}

QFrame#navSeparator {{
    background-color: {tokens['outline_variant']};
    border: none;
}}

QPushButton#collapsibleSectionToggle {{
    background-color: transparent;
    color: {tokens['on_surface']};
    border: none;
    border-radius: {shape['r_xl']};
    padding: 8px 12px;
    text-align: left;
    font-weight: 700;
}}

QPushButton#collapsibleSectionToggle:hover {{
    color: {tokens['primary']};
    background-color: {tokens['surface_container_high']};
}}

/* A pending project change awaiting Apply. Bordered in the danger colour
   because it is the one panel in Buddy whose button modifies the user's
   actual project - it should not look like an ordinary info box. */
QFrame#proposalCard {{
    background-color: {tokens['surface_container']};
    border: {shape['bw_thick']} solid {tokens['danger']};
    border-radius: {shape['r_lg']};
}}

/* Time Tracker's Tracker tab - a toggle-style Pause/Resume button, the
   big elapsed-time readout, and a small state pill (tracking/paused/
   offline all get their own tint via the [state=...] property). */
QPushButton#toggleButton {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['on_surface']};
    border: {shape['bw']} solid {tokens['outline_variant']};
    border-radius: {shape['r_sm']};
    padding: 6px 10px;
    font-weight: 600;
}}

QPushButton#toggleButton:hover {{
    border-color: {tokens['outline']};
    color: {tokens['primary']};
}}

QPushButton#toggleButton[active="true"] {{
    background-color: {tokens['primary_container']};
    color: {tokens['on_primary_container']};
    border: {shape['bw']} solid {tokens['primary']};
}}

QLabel#bigTimerLabel {{
    font-size: 56px;
    font-weight: 700;
    color: {tokens['primary']};
}}

QLabel#statusPillLabel {{
    font-size: 13px;
    font-weight: 700;
    padding: 6px 14px;
    border-radius: {shape['r_sm']};
    background-color: {tokens['surface_container_high']};
    color: {tokens['on_surface']};
}}

QLabel#statusPillLabel[state="tracking"] {{
    background-color: {tokens['primary_container']};
    color: {tokens['on_primary_container']};
}}

QLabel#statusPillLabel[state="paused"] {{
    background-color: {tokens['secondary_container']};
    color: {tokens['on_secondary']};
}}

QLabel#statusPillLabel[state="offline"] {{
    background-color: #F2B8B5;
    color: #601410;
}}

/* Time Tracker's This Project/History tabs - a scrolled list of plain row
   widgets (see pages/time_tracker/history_tab.py's docstring for why a
   real QTableWidget was dropped in favor of this) rather than an item
   view, so its rows/header need their own named rules. */
QFrame#historyHeaderRow {{
    background-color: {tokens['surface_container_high']};
    border: none;
    border-bottom: {shape['bw']} solid {tokens['outline_variant']};
    /* Sits flush against #headerFrame's own rounded top corners (no
       margin between them) - matching its radius here, rather than
       adding padding, is what keeps the two curves aligned instead of
       this row's square corners cutting into them. */
    border-top-left-radius: {shape['r_pill']};
    border-top-right-radius: {shape['r_pill']};
}}

QLabel#historyHeaderCell {{
    background-color: transparent;
    color: {tokens['on_surface']};
    font-weight: 700;
    padding: 0px 4px;
}}

QFrame#historyRow {{
    background-color: transparent;
    border: none;
    border-bottom: {shape['bw']} solid {tokens['outline_variant']};
}}

QFrame#historyRow[alt="true"] {{
    background-color: {tokens['surface_container_high']};
}}

QFrame#historyRow:hover {{
    background-color: {tokens['secondary_container']};
}}

QFrame#historyRow[selected="true"] {{
    background-color: {tokens['primary_container']};
}}

QLabel#historyCell {{
    background-color: transparent;
    color: {tokens['on_surface']};
    padding: 0px 4px;
}}
{extra}
{_pane_rules(theme, tokens, shape, card_bg)}"""


def _pane_rules(theme, tokens, shape, card_bg):
    """The shell's panes (core/shell_window.py): each a card of its own -
    in dual view, two separate windows with a gap between - and the
    splitter holding them nothing at all. A QSplitter is a QFrame, so the
    card rule above would otherwise box both panes as one. Last, so they
    win over every family's rules. Resolve has no pane card: its panes are
    flat, boxed only by its divider line."""
    if THEMES[theme]["shape"] == "resolve":
        bg, edge, radius, pad = "transparent", resolve_colors(tokens)["divider"], "0px", 0
    else:
        gradient = shape.get("backdrop") == "gradient"
        radius = shape["r_2xl"]
        if gradient:
            # A gradient backdrop's pages are see-through and round their
            # own corners to match (buddy.css's body > main), so the inset
            # only needs to clear the border line, not the curve too - the
            # bigger tangent inset below would just widen the ring where the
            # card's own (differently coloured) background shows around it.
            pad = int(float(str(shape["bw"]).rstrip("px") or 0))
        else:
            # A page is square: inset it just far enough that its corners
            # sit inside the card's curve, not over the border. The card
            # takes the colour the page paints (its surface) so the inset
            # doesn't show.
            pad = _curve_inset(radius, shape["bw"])
        bg = card_bg if gradient else tokens['surface']
        if THEMES[theme]["shape"] == "nova":
            # Nova's page isn't see-through: it paints its own surface and
            # glow (themes/nova.css), so the card's gradient would only ever
            # show in the 1px inset - a grey ring inside the border.
            bg = tokens['surface']
        edge = _blend(tokens['outline_variant'], tokens['primary'], 0.25) if gradient else tokens['outline_variant']
    return f"""
QSplitter#paneSplitter {{
    background-color: transparent;
    border: none;
}}

QScrollArea#paneCard {{
    background: {bg};
    border: {shape['bw']} solid {edge};
    border-radius: {radius};
    padding: {pad}px;
}}
"""


def _curve_inset(radius, border):
    """How far in from a rounded box's edge a square corner clears the
    curve: the corner of the inset square lies on the inner arc."""
    r, bw = (int(float(str(v).rstrip("px") or 0)) for v in (radius, border))
    if r <= bw:
        return 0
    return math.ceil((r - bw) * (1 - 1 / math.sqrt(2))) + bw


def pane_radius(theme):
    """The pane card's corner radius in pixels (the dual-view tint follows it)."""
    if (THEMES.get(theme) or {}).get("shape") == "resolve":
        return 0
    return int(float(get_shape_tokens(theme)["r_2xl"].rstrip("px") or 0))


def _saas_extra_rules(tokens, shape):
    """Rules only the gradient (SaaS) backdrop adds, appended last so equal-
    specificity rules above lose to them by source order.

    .QWidget matches plain QWidget instances only - the containers pages
    build their rows and tabs out of - never a subclass, so line edits,
    lists, menus and every top-level window keep their solid backgrounds.
    Without it those containers paint flat surface-coloured blocks over the
    window's glow and cards' sheen, and the backdrop shows only in margins.
    """
    # The accent button's gradient, anchored on its LIGHTEST stop: that stop
    # is pulled just far enough from the label colour to keep bold label
    # text at >= 3.6:1 (a bright azure like Midnight's carries white at only
    # 2.9:1 as-is), and the sheen runs darker from there, never lighter.
    accent_hi = ensure_contrast(tokens['primary'], tokens['on_primary'], 3.6)
    accent_lo = _blend(accent_hi, "#000000", 0.22)
    edge = _blend(tokens['outline_variant'], tokens['primary'], 0.25)
    return f"""
.QWidget {{
    background-color: transparent;
}}

QFrame, .QGroupBox, #navRail, #headerFrame {{
    border-color: {edge};
}}

QPushButton#accentButton {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent_hi}, stop:1 {accent_lo});
    color: {tokens['on_primary']};
}}

QPushButton#accentButton:hover {{
    background-color: {accent_hi};
    color: {tokens['on_primary']};
}}

QPushButton#accentButton:pressed {{
    background-color: {accent_lo};
    color: {tokens['on_primary']};
}}

QPushButton#accentButton:disabled {{
    background-color: {tokens['surface_container_high']};
    color: {tokens['outline']};
}}

QPushButton#navButton[active="true"] {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {tokens['primary_container']}, stop:1 {_blend(tokens['primary_container'], tokens['surface_container'], 0.5)});
    color: {tokens['on_primary_container']};
    border: {shape['bw']} solid {tokens['primary']};
}}

QLabel#titleLabel {{
    color: {tokens['on_surface']};
    font-size: 19px;
    font-weight: 700;
}}
"""


def _painted_asset(name, color_hex, size, draw):
    """A small PNG painted once per colour into the temp dir, for QSS
    image: rules (Qt's stylesheet engine only reads real files)."""
    fname = f"buddy_theme_{name}_{color_hex.lstrip('#').upper()}_{size[0]}x{size[1]}.png"
    path = os.path.join(tempfile.gettempdir(), fname)
    if not os.path.exists(path):
        pixmap = QPixmap(*size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(color_hex))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        draw(painter, pen)
        painter.end()
        pixmap.save(path)
    return path.replace("\\", "/")


def _tick_asset_path(color_hex):
    """Resolve's checkbox mark: a white tick, not Buddy's X."""
    def draw(p, pen):
        pen.setWidthF(2.0)
        p.setPen(pen)
        p.drawPolyline([QPointF(3.5, 8.8), QPointF(6.6, 11.8), QPointF(12.6, 4.4)])
    return _painted_asset("tick", color_hex, (16, 16), draw)


def _dot_asset_path(color_hex):
    """The white dot inside Resolve's selected radio button."""
    def draw(p, pen):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color_hex))
        p.drawEllipse(QPointF(7.0, 7.0), 3.2, 3.2)
    return _painted_asset("dot", color_hex, (14, 14), draw)


def _chevron_asset_path(color_hex):
    """The thin down-chevron on Resolve's dropdowns."""
    def draw(p, pen):
        pen.setWidthF(1.5)
        p.setPen(pen)
        p.drawPolyline([QPointF(1.5, 1.8), QPointF(5.5, 5.6), QPointF(9.5, 1.8)])
    return _painted_asset("chevron", color_hex, (11, 8), draw)


def _resolve_extra_rules(tokens, shape):
    """Everything the shared template can't express for the Resolve look,
    appended last so these win by source order. Colours are Resolve's
    own (see the palette above); the red accent is kept for the
    few places Resolve itself uses it - the active page, focus of attention -
    and never as a button fill, because Resolve has none."""
    c = resolve_colors(tokens)
    tick = _tick_asset_path(c["bright"])
    dot = _dot_asset_path(c["bright"])
    chevron = _chevron_asset_path(c["label"])
    chevron_hi = _chevron_asset_path(c["value"])
    pill = shape["r_xl"]
    return f"""
/* ---- Resolve: surfaces. Panels are square, butted together, divided by
   near-black hairlines; plain containers stay transparent so a panel's grey
   shows through its contents. */
QMainWindow, QDialog {{
    background-color: {c['window']};
}}

.QWidget {{
    background-color: transparent;
}}

QFrame, .QGroupBox {{
    background-color: {c['panel']};
    border: 1px solid {c['divider']};
    border-radius: 0px;
}}

/* QLabel, QScrollArea and QStackedWidget are QFrames too: without this
   the panel rule above boxes every label and scroll area. */
QLabel, QScrollArea, QStackedWidget, QSplitter {{
    background-color: transparent;
    border: none;
}}

#headerFrame {{
    background-color: {c['toolbar']};
    border: 1px solid {c['divider']};
    border-radius: 0px;
}}

#navRail {{
    background-color: {c['panel']};
    border: 1px solid {c['divider']};
    border-radius: 0px;
}}

QLabel {{
    color: {c['label']};
}}

QLabel#titleLabel {{
    color: {c['bright']};
    font-size: 15px;
    font-weight: 600;
}}

QLabel#sectionLabel {{
    color: {c['bright']};
    font-size: 13px;
    font-weight: 700;
}}

QLabel#subLabel {{
    color: {c['label']};
    font-size: 12px;
}}

QLabel#navGroupLabel {{
    color: {c['dim']};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
}}

QLabel#bigTimerLabel {{
    color: {c['bright']};
    font-weight: 600;
}}

QToolTip {{
    background-color: {c['field']};
    color: {c['value']};
    border: 1px solid {c['button_border']};
    border-radius: 0px;
    padding: 4px 6px;
    font-size: 12px;
}}

/* ---- The sidebar reads like Resolve's page bar: grey labels, the active
   page white on the darker toolbar grey with a red marker. */
QPushButton#navButton {{
    background-color: transparent;
    color: {c['label']};
    border: none;
    border-left: 2px solid transparent;
    border-radius: 0px;
    padding: 8px 14px;
    font-size: 13px;
    font-weight: 400;
}}

QPushButton#navButton:hover {{
    background-color: {c['hover']};
    color: {c['bright']};
}}

QPushButton#navButton[active="true"] {{
    background-color: {c['toolbar']};
    color: {c['bright']};
    border: none;
    border-left: 2px solid {c['accent']};
    font-weight: 600;
}}

/* ---- Buttons are Resolve's outlined pills ("Browse", "Render All"):
   panel-grey fill, a thin grey ring, grey regular text. There is no filled
   primary button in Resolve, so the accent button is the same pill with
   white text and a brighter ring. */
QPushButton {{
    background-color: {c['panel']};
    color: {c['label']};
    border: 1px solid {c['button_border']};
    border-radius: {pill};
    padding: 2px 14px;
    font-weight: 400;
}}

QPushButton:hover {{
    background-color: {c['hover']};
    border-color: {c['button_border_hi']};
    color: {c['bright']};
}}

QPushButton:pressed {{
    background-color: {c['field']};
}}

QPushButton:disabled {{
    background-color: {c['panel']};
    color: {c['dim']};
    border-color: #333338;
}}

QPushButton#accentButton, QPushButton#successButton {{
    background-color: {c['panel']};
    color: {c['bright']};
    border: 1px solid {c['button_border_hi']};
    border-radius: {pill};
    font-weight: 600;
}}

QPushButton#accentButton:hover, QPushButton#successButton:hover {{
    background-color: {c['hover']};
    border-color: #9A9EA5;
    color: {c['bright']};
}}

QPushButton#accentButton:pressed, QPushButton#successButton:pressed {{
    background-color: {c['field']};
    color: {c['bright']};
}}

QPushButton#accentButton:disabled {{
    background-color: {c['panel']};
    color: {c['dim']};
    border: 1px solid #333338;
}}

QPushButton#dangerButton {{
    background-color: {c['panel']};
    color: {c['accent']};
    border: 1px solid {c['accent']};
    border-radius: {pill};
    font-weight: 600;
}}

QPushButton#dangerButton:hover {{
    background-color: {c['hover']};
}}

/* Icon buttons (the settings gear, "..." menus) are flat in Resolve. */
QPushButton#iconButton {{
    background-color: transparent;
    color: {c['label']};
    border: 1px solid transparent;
    border-radius: 2px;
}}

QPushButton#iconButton:hover {{
    background-color: {c['hover']};
    border-color: transparent;
    color: {c['bright']};
}}

QPushButton#iconButton:checked {{
    background-color: {c['hover']};
    border-color: transparent;
}}

QPushButton#collapsibleSectionToggle {{
    color: {c['label']};
    border-radius: 0px;
}}

QPushButton#collapsibleSectionToggle:hover {{
    color: {c['bright']};
    background-color: transparent;
}}

QPushButton#toggleButton {{
    background-color: {c['header']};
    color: {c['label']};
    border: 1px solid {c['divider']};
    border-radius: 0px;
}}

QPushButton#toggleButton[active="true"] {{
    background-color: {c['toolbar']};
    color: {c['bright']};
    border: 1px solid {c['divider']};
}}

/* ---- Fields and dropdowns: near-black, 1px black border, almost-square
   corners, white values. */
QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
    background-color: {c['field']};
    color: {c['value']};
    border: 1px solid {c['field_border']};
    border-radius: 2px;
    selection-background-color: {tokens['primary_container']};
    selection-color: {c['bright']};
}}

/* Resolve's fields and dropdowns are ~20px tall, buttons and segments
   22px; the padding here lands Open Sans 12px on those. */
QLineEdit, QSpinBox, QDoubleSpinBox {{
    padding: 0px 6px;
    min-height: 18px;
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {c['button_border_hi']};
    background-color: {c['field']};
}}

QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {c['dim']};
}}

QComboBox {{
    background-color: {c['field']};
    color: {c['value']};
    border: 1px solid {c['field_border']};
    border-radius: 2px;
    padding: 0px 8px;
    padding-right: 26px;
    min-height: 18px;
}}

QComboBox:hover {{
    background-color: {c['field']};
    border-color: {c['button_border']};
}}

QComboBox::drop-down {{
    width: 22px;
    border: none;
}}

QComboBox::down-arrow {{
    image: url({chevron});
    width: 11px;
    height: 8px;
    border: none;
    margin-right: 8px;
}}

QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{
    image: url({chevron_hi});
}}

QComboBox QAbstractItemView {{
    background-color: {c['panel']};
    border: 1px solid {c['field_border']};
    border-radius: 2px;
    padding: 2px;
}}

QComboBox QAbstractItemView::item {{
    min-height: 22px;
    padding: 2px 10px;
    border-radius: 0px;
    color: {c['value']};
}}

QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected,
QComboBox QAbstractItemView::item:selected:!active {{
    background-color: {tokens['primary_container']};
    color: {c['bright']};
}}

/* ---- Lists and tables sit in the field colour, like Resolve's bins. */
QTreeView, QListView, QTableView {{
    background-color: {c['field']};
    color: {c['value']};
    border: 1px solid {c['field_border']};
    border-radius: 0px;
    alternate-background-color: #232323;
}}

QTreeView::item:hover, QListView::item:hover, QTableView::item:hover {{
    background-color: {c['hover']};
}}

QHeaderView::section {{
    background-color: {c['header']};
    color: {c['label']};
    border: none;
    border-right: 1px solid {c['divider']};
    border-bottom: 1px solid {c['divider']};
    padding: 4px 8px;
}}

/* ---- Checkboxes and radio buttons: dark boxes, white marks - no accent
   fill ("Export Audio", "Single clip"). */
QCheckBox, QRadioButton {{
    color: {c['label']};
    spacing: 8px;
}}

QCheckBox::indicator, QListView::indicator, QTreeView::indicator, QGroupBox::indicator {{
    width: 14px;
    height: 14px;
    background-color: {c['field']};
    border: 1px solid {c['field_border']};
    border-radius: 2px;
}}

QCheckBox::indicator:hover, QListView::indicator:hover, QTreeView::indicator:hover {{
    border-color: {c['button_border_hi']};
}}

QCheckBox::indicator:checked, QListView::indicator:checked,
QTreeView::indicator:checked, QGroupBox::indicator:checked {{
    background-color: {c['field']};
    border: 1px solid {c['field_border']};
    image: url({tick});
}}

QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    background-color: {c['field']};
    border: 1px solid {c['field_border']};
    border-radius: 8px;
}}

QRadioButton::indicator:hover {{
    border-color: {c['button_border_hi']};
}}

QRadioButton::indicator:checked {{
    background-color: {c['field']};
    border: 1px solid {c['field_border']};
    image: url({dot});
}}

QGroupBox::title {{
    color: {c['bright']};
}}

/* ---- Tabs: Resolve's segmented control ("Video | Audio | File") - the
   selected segment drops to the toolbar grey with white text. */
QTabWidget::pane {{
    background-color: {c['panel']};
    border: 1px solid {c['divider']};
    border-radius: 0px;
}}

QTabBar::tab {{
    background-color: {c['header']};
    color: {c['label']};
    border: 1px solid {c['divider']};
    padding: 2px 16px;
    margin-right: -1px;
}}

QTabBar::tab:hover {{
    color: {c['bright']};
}}

QTabBar::tab:selected {{
    background-color: {c['toolbar']};
    color: {c['bright']};
}}

/* ---- Scroll bars, sliders, progress: thin and grey, as in Resolve's
   timeline and zoom slider. */
QScrollBar:vertical {{
    width: 8px;
    margin: 2px 1px 2px 0px;
}}

QScrollBar:horizontal {{
    height: 8px;
}}

QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background-color: {c['scroll']};
    border-radius: 3px;
}}

QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
    background-color: {c['scroll_hi']};
}}

QSlider::groove:horizontal {{
    height: 2px;
    background-color: {c['scroll']};
    border-radius: 1px;
}}

QSlider::sub-page:horizontal {{
    background-color: {c['label']};
    border-radius: 1px;
}}

QSlider::add-page:horizontal {{
    background-color: {c['scroll']};
    border-radius: 1px;
}}

QSlider::handle:horizontal {{
    background-color: #B8B8BA;
    border: none;
    width: 10px;
    height: 10px;
    margin: -4px 0;
    border-radius: 5px;
}}

QSlider::handle:horizontal:hover {{
    background-color: {c['bright']};
}}

QProgressBar {{
    background-color: {c['field']};
    color: {c['value']};
    border: 1px solid {c['field_border']};
    border-radius: 2px;
    text-align: center;
}}

QProgressBar::chunk {{
    background-color: {c['accent']};
    border-radius: 1px;
}}

QMenu {{
    background-color: {c['panel']};
    color: {c['value']};
    border: 1px solid {c['divider']};
    padding: 2px;
}}

QMenu::item {{
    padding: 4px 22px 4px 14px;
    background-color: transparent;
}}

QMenu::item:selected {{
    background-color: {tokens['primary_container']};
    color: {c['bright']};
}}

QMenu::separator {{
    height: 1px;
    background-color: {c['divider']};
    margin: 3px 0px;
}}

QFrame#historyHeaderRow {{
    background-color: {c['header']};
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
}}

QFrame#navSeparator {{
    background-color: {c['divider']};
}}
"""


# Qt rules a shape family appends to the shared template, after the
# gradient backdrop's (so they win by source order). A family missing here
# gets the template alone - which is all a web-first theme needs, since the
# only Qt left is the window frame, the busy overlay and the tray.
QT_EXTRA_RULES = {
    "resolve": _resolve_extra_rules,
}
