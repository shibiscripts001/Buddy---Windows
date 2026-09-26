#!/usr/bin/env python3
"""
The desktop layout's bookkeeping (a theme with "layout": "desktop"): which
tool windows are open, where, how big, minimised or maximised, and which is
in front. core/desktop_window.py draws it; this decides it.

No Qt - tests/test_desktop_layout.py runs on plain Python. Geometry is
(x, y, width, height) in the desk's own pixels, the window's shadow
included.

Saved in the shared settings under "desktop" as
    {"windows": {tool_id: {"geom": [x, y, w, h], "open": bool,
                           "min": bool, "max": bool, "color": int}},
     "order": [tool_id, ...]}          back to front
"""

import math

SETTINGS_KEY = "desktop"

# Room kept clear round the edge of the desk, and between tiled windows.
MARGIN = 14
# How far each new window steps down and right from the last.
CASCADE_STEP = 30
# Smallest a window can be made: a title bar and a little of the page.
MIN_WIDTH, MIN_HEIGHT = 280, 160
# The share of the desk a window opens at when the page has no opinion.
DEFAULT_SHARE = 0.72
# Title-bar colours a palette has (theme.desktop_colors "windows").
WINDOW_COLORS = 4
# A desk smaller than this hasn't been laid out yet: don't place by it.
MIN_DESK = (400, 300)


def clamp(geom, area, minimum=(MIN_WIDTH, MIN_HEIGHT)):
    """`geom` kept wholly on a desk of `area` (w, h), no smaller than
    `minimum` - unless the desk itself is smaller, when it fills it."""
    x, y, w, h = (int(round(v)) for v in geom)
    area_w, area_h = max(1, int(area[0])), max(1, int(area[1]))
    w = max(min(w, area_w), min(minimum[0], area_w))
    h = max(min(h, area_h), min(minimum[1], area_h))
    x = min(max(x, 0), area_w - w)
    y = min(max(y, 0), area_h - h)
    return (x, y, w, h)


def edges_at(x, y, frame, grip, corner):
    """Which edges a press at (x, y) resizes, for a window frame (left, top,
    right, bottom): within `grip` of an edge, or within `corner` of a
    corner along either edge - corners are bigger targets than the edges,
    since a corner is what people aim for."""
    left, top, right, bottom = frame
    near = {"left": x <= left + grip, "right": x >= right - grip,
            "top": y <= top + grip, "bottom": y >= bottom - grip}
    horizontal = "left" if x <= left + corner else "right" if x >= right - corner else None
    vertical = "top" if y <= top + corner else "bottom" if y >= bottom - corner else None
    on_edge = [e for e, hit in near.items() if hit]
    if horizontal and vertical and on_edge:
        return frozenset({horizontal, vertical})
    return frozenset(on_edge)


def resize(geom, edges, dx, dy, area, minimum=(MIN_WIDTH, MIN_HEIGHT)):
    """`geom` with `edges` dragged by (dx, dy). The opposite edges stay
    where they are, and a dragged edge stops at the desk's edge and at the
    minimum size - it never pushes the window along instead."""
    x, y, w, h = geom
    area_w, area_h = area
    if "right" in edges:
        w = min(max(minimum[0], w + dx), area_w - x)
    if "bottom" in edges:
        h = min(max(minimum[1], h + dy), area_h - y)
    if "left" in edges:
        right = x + w
        x = max(0, min(x + dx, right - minimum[0]))
        w = right - x
    if "top" in edges:
        bottom = y + h
        y = max(0, min(y + dy, bottom - minimum[1]))
        h = bottom - y
    return clamp((x, y, w, h), area, minimum)


def opening_size(area, preferred=None, minimum=(MIN_WIDTH, MIN_HEIGHT)):
    """The size a window opens at: the page's own preference when it has
    one, else DEFAULT_SHARE of the desk - never past the desk's margins."""
    room_w, room_h = max(1, area[0] - 2 * MARGIN), max(1, area[1] - 2 * MARGIN)
    if preferred and preferred[0] > 0 and preferred[1] > 0:
        w, h = preferred
    else:
        w, h = area[0] * DEFAULT_SHARE, area[1] * DEFAULT_SHARE
    w = max(min(w, room_w), min(minimum[0], room_w))
    h = max(min(h, room_h), min(minimum[1], room_h))
    return (int(w), int(h))


def cascade_position(index, size, area):
    """Where the index-th cascaded window of `size` goes: stepping down and
    right from the top-left, starting over when it would leave the desk."""
    span_x = max(1, area[0] - size[0] - 2 * MARGIN)
    span_y = max(1, area[1] - size[1] - 2 * MARGIN)
    steps = max(1, min(span_x, span_y) // CASCADE_STEP + 1)
    step = index % steps
    return (MARGIN + step * CASCADE_STEP, MARGIN + step * CASCADE_STEP)


def tile(count, area):
    """`count` geometries filling the desk in a grid - as square as it gets,
    the last row's windows stretched to use the whole width."""
    if count <= 0:
        return []
    cols = math.ceil(math.sqrt(count * area[0] / max(1, area[1]) / 1.6)) or 1
    cols = max(1, min(cols, count))
    rows = math.ceil(count / cols)
    cell_h = (area[1] - MARGIN * (rows + 1)) / rows
    out = []
    for row in range(rows):
        in_row = min(cols, count - row * cols)
        cell_w = (area[0] - MARGIN * (in_row + 1)) / in_row
        for col in range(in_row):
            out.append((int(MARGIN + col * (cell_w + MARGIN)), int(MARGIN + row * (cell_h + MARGIN)),
                        int(cell_w), int(cell_h)))
    return out


class DesktopState:
    """Every tool window's state, and the stacking order."""

    def __init__(self, windows=None, order=None):
        self.windows = windows or {}
        self.order = order or []
        # When each window last opened (a counter), for the taskbar's order.
        self.opened = {t: i for i, t in enumerate(self.order)}
        self._clock = len(self.order)

    # ------------------------------------------------------------ settings --
    @classmethod
    def from_dict(cls, data, known_ids):
        """Whatever the settings file holds, trusted only as far as it
        checks out: unknown tools and malformed entries are dropped."""
        windows, order = {}, []
        data = data if isinstance(data, dict) else {}
        raw = data.get("windows") if isinstance(data.get("windows"), dict) else {}
        for tool_id, entry in raw.items():
            if tool_id not in known_ids or not isinstance(entry, dict):
                continue
            geom = entry.get("geom")
            if not (isinstance(geom, (list, tuple)) and len(geom) == 4
                    and all(isinstance(v, (int, float)) for v in geom)):
                geom = None
            color = entry.get("color")
            windows[tool_id] = {
                "geom": tuple(int(v) for v in geom) if geom else None,
                "open": bool(entry.get("open")),
                "min": bool(entry.get("min")),
                "max": bool(entry.get("max")),
                "color": color if isinstance(color, int) and 0 <= color < WINDOW_COLORS else 0,
            }
        for tool_id in data.get("order") or []:
            if tool_id in windows and tool_id not in order:
                order.append(tool_id)
        order += [t for t in windows if t not in order]
        return cls(windows, order)

    def to_dict(self):
        return {
            "windows": {t: {"geom": list(w["geom"]) if w["geom"] else None, "open": w["open"],
                            "min": w["min"], "max": w["max"], "color": w["color"]}
                        for t, w in self.windows.items()},
            "order": list(self.order),
        }

    # ------------------------------------------------------------- queries --
    def entry(self, tool_id):
        if tool_id not in self.windows:
            self.windows[tool_id] = {"geom": None, "open": False, "min": False, "max": False, "color": 0}
            self.order.append(tool_id)
        return self.windows[tool_id]

    def is_open(self, tool_id):
        return self.windows.get(tool_id, {}).get("open", False)

    def open_ids(self):
        """Open windows, back to front (minimised ones included)."""
        return [t for t in self.order if self.is_open(t)]

    def front(self):
        """The window in front: the topmost open one that isn't minimised."""
        for tool_id in reversed(self.order):
            w = self.windows[tool_id]
            if w["open"] and not w["min"]:
                return tool_id
        return None

    def window_state(self, tool_id):
        """"min", "open" or None - what the rail marks a tool with."""
        w = self.windows.get(tool_id)
        if not w or not w["open"]:
            return None
        return "min" if w["min"] else "open"

    # ------------------------------------------------------------- changes --
    def raise_(self, tool_id):
        self.entry(tool_id)
        self.order.remove(tool_id)
        self.order.append(tool_id)

    def open(self, tool_id):
        """Open (or bring back) a window and put it in front. True when it
        wasn't on show before."""
        w = self.entry(tool_id)
        newly = not w["open"] or w["min"]
        if not w["open"]:
            w["color"] = self._free_color(tool_id)
            self._clock += 1
            self.opened[tool_id] = self._clock
        w["open"], w["min"] = True, False
        self.raise_(tool_id)
        return newly

    def _free_color(self, tool_id):
        """A title colour no other open window has - its old one when that's
        free, so a tool tends to keep its colour - else the least used."""
        used = [self.windows[t]["color"] for t in self.open_ids() if t != tool_id]
        own = self.windows[tool_id]["color"]
        if own not in used:
            return own
        return min(range(WINDOW_COLORS), key=lambda c: (used.count(c), c))

    def close(self, tool_id):
        w = self.entry(tool_id)
        w["open"], w["min"], w["max"] = False, False, False

    def minimize(self, tool_id):
        self.entry(tool_id)["min"] = True

    def toggle_max(self, tool_id):
        w = self.entry(tool_id)
        w["max"] = not w["max"]
        return w["max"]

    def set_geom(self, tool_id, geom):
        w = self.entry(tool_id)
        w["geom"] = tuple(int(v) for v in geom)
        w["max"] = False

    def place_new(self, tool_id, area, preferred=None, minimum=(MIN_WIDTH, MIN_HEIGHT)):
        """Give a window that has never been placed a spot: its opening size,
        cascaded from the windows already open."""
        size = opening_size(area, preferred, minimum)
        index = len([t for t in self.open_ids() if t != tool_id])
        x, y = cascade_position(index, size, area)
        self.entry(tool_id)["geom"] = clamp((x, y, *size), area, minimum)

    def cascade(self, area):
        """Every open window restored and stepped down from the top-left, in
        their current order."""
        ids = self.open_ids()
        size = opening_size(area, (area[0] * 0.6, area[1] * 0.7))
        for i, tool_id in enumerate(ids):
            w = self.windows[tool_id]
            w["min"] = w["max"] = False
            x, y = cascade_position(i, size, area)
            w["geom"] = clamp((x, y, *size), area)

    def tile(self, area):
        """Every open window restored and laid out in a grid."""
        ids = self.open_ids()
        for tool_id, geom in zip(ids, tile(len(ids), area)):
            w = self.windows[tool_id]
            w["min"] = w["max"] = False
            w["geom"] = geom

    def keep_only(self, visible_ids):
        """Close the windows of tools no longer in the rail."""
        for tool_id in self.open_ids():
            if tool_id not in visible_ids:
                self.close(tool_id)


# ------------------------------------------------------------- taskbar --
#
# The desktop's taskbar: pinned tools first, in the order they were pinned,
# then every other open window in the order it opened. Pins are saved in
# the shared settings under PINS_KEY.

PINS_KEY = "desktop_pins"
DEFAULT_PINS = ("manual_chat", "project_setup", "time_tracker")


def load_pins(raw, known_ids):
    """The saved pins, trusted only as far as they check out; the defaults
    for someone who has never pinned anything (an empty list is a choice)."""
    if not isinstance(raw, list):
        raw = list(DEFAULT_PINS)
    pins = []
    for tool_id in raw:
        if tool_id in known_ids and tool_id not in pins:
            pins.append(tool_id)
    return pins


def taskbar_ids(pins, state, visible_ids):
    """[(tool_id, pinned)] in taskbar order - tools out of the rail's list
    (Organize sidebar) left off."""
    pinned = [t for t in pins if t in visible_ids]
    running = [t for t in state.open_ids() if t in visible_ids and t not in pinned]
    running.sort(key=lambda t: state.opened.get(t, 0))
    return [(t, True) for t in pinned] + [(t, False) for t in running]


def task_click(state, tool_id):
    """What clicking a tool's taskbar button does: "minimize" the window in
    front (as Windows does), else "open" - open, restore or bring forward."""
    if state.window_state(tool_id) == "open" and state.front() == tool_id:
        return "minimize"
    return "open"
