#!/usr/bin/env python3
"""
Resolve's 16 marker colours, in Resolve's own order, with a display colour
for each so a web page can show a chip that looks like the marker. The
names are what Resolve's scripting API takes and returns; the hex values
are close approximations of Resolve's marker swatches, for display only.

No Qt - used by the Stills Exporter and YouTube Chapters pages.
"""

MARKER_COLORS = [
    ("Blue", "#2F7BF5"), ("Cyan", "#16C6D8"), ("Green", "#3DB94A"), ("Yellow", "#F2CC2E"),
    ("Red", "#E5393B"), ("Pink", "#F46BAF"), ("Purple", "#9453D6"), ("Fuchsia", "#CF3A95"),
    ("Rose", "#F2A0B0"), ("Lavender", "#B3A4E4"), ("Sky", "#8CC8F5"), ("Mint", "#7EDCB2"),
    ("Lemon", "#EFEA7C"), ("Sand", "#D5B27C"), ("Cocoa", "#7B5534"), ("Cream", "#F1E6CC"),
]
NAMES = [name for name, _hex in MARKER_COLORS]
HEX = dict(MARKER_COLORS)


def numeric_markers(markers):
    """A GetMarkers() dict with its frame keys as numbers, in frame order.
    Keys Resolve returns as strings or floats are both handled; anything
    that isn't a frame is dropped."""
    out = []
    for key, info in (markers or {}).items():
        try:
            frame = float(key)
        except (TypeError, ValueError):
            continue
        out.append((frame, info or {}))
    return sorted(out, key=lambda item: item[0])


def color_counts(markers):
    """[{name, hex, count}] for all 16 colours, from a GetMarkers() dict."""
    counts = {}
    for _frame, info in numeric_markers(markers):
        color = info.get("color", "")
        counts[color] = counts.get(color, 0) + 1
    return [{"name": name, "hex": hex_code, "count": counts.get(name, 0)} for name, hex_code in MARKER_COLORS]
