#!/usr/bin/env python3
"""
What the Color Palette page shows, as plain data for its web view - no Qt,
so it's unit-tested (tests/test_color_palette.py). page.py sends these
with emit(); the JS only draws them.

Every colour goes out as a swatch: {"hex": the stored colour, "shown": how
it looks under the current Vision simulation, "ink": black or white text
that reads on `shown`}. Edits always act on "hex"; "shown" is display only.
"""

import re

from .color_engine import (
    EXPORT_FORMATS, FIXED_SHAPE_WEIGHTS, apca_contrast, compute_fixed_shape_rects,
    contrasting_text_color, simulate_colorblindness, wcag_contrast_ratio,
)

UNORGANIZED = "__UNORGANIZED__"
SLOT_COUNT = len(FIXED_SHAPE_WEIGHTS)
DEFAULT_SLOT_HEX = "#808080"
VISION_MODES = ["None", "Protanopia", "Deuteranopia", "Tritanopia", "Achromatopsia"]
WCAG_CHECKS = [
    ("Normal text – AA", 4.5),
    ("Normal text – AAA", 7.0),
    ("Large text – AA", 3.0),
    ("Large text – AAA", 4.5),
]

_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


def normalize_hex(text):
    """'#1a2b3c', '1A2B3C' or 'abc' -> '#1A2B3C' / '#AABBCC'; None if it
    isn't a colour."""
    match = _HEX_RE.match(str(text or "").strip())
    if not match:
        return None
    digits = match.group(1).upper()
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return "#" + digits


def swatch(hex_code, vision="None"):
    shown = simulate_colorblindness(hex_code, vision)
    return {"hex": hex_code.upper(), "shown": shown.upper(), "ink": contrasting_text_color(shown)}


def swatches(colors, vision="None"):
    return [swatch(c, vision) for c in colors]


# ------------------------------------------------------------- palettes --

def matches(data, name, query):
    """A palette matches a search on its name, a tag or a colour's hex."""
    if not query:
        return True
    q = query.lower()
    if q in name.lower():
        return True
    if any(q in t.lower() for t in data.get_palette_custom_tags(name)):
        return True
    return any(q in c.lower() for c in data.palettes.get(name, []))


def palette_row(data, name, vision, open_name):
    history = data.palette_versions.get(name, [])
    return {
        "name": name,
        "colors": swatches(data.palettes.get(name, []), vision),
        "tags": list(data.get_palette_custom_tags(name)),
        "folder": data.get_palette_folder(name),
        "open": name == open_name,
        "versions": len(history),
    }


def library(data, query="", open_folders=(), open_name=None, vision="None"):
    """The Palettes list: folders (each with the palettes in it), then the
    palettes that aren't in one - the standalone's accordion rules: a
    search opens every folder, and a folder is left out only when neither
    its name nor any palette in it matches."""
    query = (query or "").strip()
    in_folders = {p for names in data.palette_folders.values() for p in names if p in data.palettes}
    folders = []
    for folder, names in data.palette_folders.items():
        rows = [n for n in names if n in data.palettes and matches(data, n, query)]
        if query and query.lower() not in folder.lower() and not rows:
            continue
        folders.append({
            "name": folder,
            "count": len(rows),
            "open": folder in open_folders or bool(query),
            "palettes": [palette_row(data, n, vision, open_name) for n in rows],
        })
    loose = [palette_row(data, n, vision, open_name)
             for n in data.palettes if n not in in_folders and matches(data, n, query)]
    return {"folders": folders, "loose": loose, "query": query, "total": len(data.palettes)}


def versions(data, name, vision="None"):
    """A palette's saved versions, newest first, for the History window."""
    history = data.palette_versions.get(name, [])
    return [{
        "index": i,
        "number": i + 1,
        "label": entry.get("label", ""),
        "timestamp": entry.get("timestamp"),
        "colors": swatches(entry.get("colors", []), vision),
    } for i, entry in reversed(list(enumerate(history)))]


def check_new_name(existing, name, what="palette"):
    """An error message for a new palette/folder name, or None if it's fine."""
    name = (name or "").strip()
    if not name:
        return f"Please enter a name for the new {what}."
    if name in existing:
        return f"A {what} with this name already exists."
    return None


def add_color(colors, hex_code):
    """Append a colour unless the palette already has it. True if added."""
    if hex_code in colors:
        return False
    colors.append(hex_code)
    return True


def move_color(colors, src, dst):
    """Move colors[src] so it lands before what is now at dst (dst ==
    len(colors) puts it last) - a drag and drop in the palette. True if
    anything moved."""
    if not (0 <= src < len(colors)) or not (0 <= dst <= len(colors)) or dst in (src, src + 1):
        return False
    color = colors.pop(src)
    if dst > src:
        dst -= 1
    colors.insert(dst, color)
    return True


# ------------------------------------------------------------ visualize --

def slot_colors(data, name):
    """Always exactly 8, in slot order: a Visualize-only override if one was
    set, else the palette's own colour, else grey. Editing slots never
    changes the saved palette - only the Palettes tab does that."""
    colors = data.palettes.get(name, [])
    out = []
    for i in range(SLOT_COUNT):
        override = data.get_visualizer_slot_override(name, i)
        if override:
            out.append(override)
        elif i < len(colors):
            out.append(colors[i])
        else:
            out.append(DEFAULT_SLOT_HEX)
    return out


def treemap(slots, vision="None"):
    """The fixed 8-block reference shape, each block's box in percent of
    the whole so the page can lay it out at any size."""
    rects = compute_fixed_shape_rects(100.0, 100.0)
    out = []
    for i, (hex_code, weight, rect) in enumerate(zip(slots, FIXED_SHAPE_WEIGHTS, rects)):
        item = swatch(hex_code, vision)
        item.update({
            "slot": i,
            "pct": f"{weight * 100:.0f}%" if weight >= 0.05 else f"{weight * 100:.1f}%",
            "box": [round(v, 4) for v in rect],
        })
        out.append(item)
    return out


def mockup(slots, vision="None"):
    """The three UI mockup cards: slot 0 leads the header card, slot 1 the
    stat tile, slot 2 is the accent on both; the tag row shows slots 0-5."""
    return {
        "dominant": swatch(slots[0], vision),
        "secondary": swatch(slots[1], vision),
        "accent": swatch(slots[2], vision),
        "tags": [dict(swatch(h, vision), slot=i) for i, h in enumerate(slots[:6])],
    }


# ---------------------------------------------------------------- tools --

def contrast(text_hex, bg_hex):
    ratio = wcag_contrast_ratio(text_hex, bg_hex)
    return {
        "text": swatch(text_hex),
        "bg": swatch(bg_hex),
        "ratio": f"{ratio:.2f}",
        "checks": [{"name": name, "threshold": f"{t:g}", "pass": ratio >= t} for name, t in WCAG_CHECKS],
        "apca": f"{abs(apca_contrast(text_hex, bg_hex)):.1f}",
    }


def export_preview(name, colors, fmt):
    """The file a text export would write, for the page to show and copy;
    None for the binary formats (ASE, Procreate) or an empty palette."""
    entry = EXPORT_FORMATS.get(fmt)
    if not entry or not colors:
        return None
    _ext, generator, is_binary = entry
    if is_binary:
        return None
    return generator(name, colors)
