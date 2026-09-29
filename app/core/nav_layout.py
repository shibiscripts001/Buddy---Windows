#!/usr/bin/env python3
"""
The user's arrangement of the nav rail: which tools show, in what order,
and where the dividers go.

Stored in SharedSettings under "nav_layout" as a flat list, top to bottom:

    {"type": "divider", "label": "Media & Assets"}   # label "" = a plain line
    {"type": "tool", "id": "asset_manager", "visible": true}

The default is derived from registry.py - its categories become dividers -
so a user who never opens the organizer sees exactly the rail the registry
describes. Hiding a tool only removes its nav button: the page is still
built and still running (Time Tracker keeps tracking with its tab hidden).

A saved layout is always reconciled against the registry before use, so it
can never strand a tool: ids that no longer exist are dropped, and a tool
added to Buddy after the layout was saved is appended at the bottom rather
than silently missing from the rail.
"""

from __future__ import annotations

SETTINGS_KEY = "nav_layout"

DIVIDER = "divider"
TOOL = "tool"


def default_layout(registry) -> list[dict]:
    """Registry order, one divider per category - today's rail."""
    out: list[dict] = []
    current = None
    for category, page_cls in registry:
        if category != current:
            out.append({"type": DIVIDER, "label": category})
            current = category
        out.append({"type": TOOL, "id": page_cls.tool_id, "visible": True})
    return out


def reconcile(saved, registry) -> list[dict]:
    """A saved layout made safe to render against the current registry.

    Malformed entries and unknown or duplicate tool ids are dropped; tools
    the layout doesn't mention are appended, visible. If that leaves no
    visible tool at all, the first tool is shown - a rail with nothing on
    it would leave every page unreachable.
    """
    known = [page_cls.tool_id for _category, page_cls in registry]
    if not isinstance(saved, list) or not saved:
        return default_layout(registry)

    # Existing sidebar layouts placed these tools separately. Keep the new
    # combined entry at their first saved position and visible when either
    # old entry was visible.
    old_marker_ids = {"stills_exporter", "youtube_chapters"}
    migrate_markers = "marker_manager" in known
    marker_visible = any(
        isinstance(entry, dict) and entry.get("type") == TOOL
        and entry.get("id") in old_marker_ids and bool(entry.get("visible", True))
        for entry in saved
    )

    out: list[dict] = []
    seen: set[str] = set()
    for entry in saved:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == DIVIDER:
            out.append({"type": DIVIDER, "label": str(entry.get("label") or "")})
        elif entry.get("type") == TOOL:
            tool_id = entry.get("id")
            if migrate_markers and tool_id in old_marker_ids:
                tool_id = "marker_manager"
            if tool_id in known and tool_id not in seen:
                seen.add(tool_id)
                out.append({"type": TOOL, "id": tool_id,
                            "visible": marker_visible if migrate_markers and entry.get("id") in old_marker_ids
                            else bool(entry.get("visible", True))})

    # A tool added after the layout was saved goes at the bottom. It brings
    # its registry divider along when the layout has no heading of that name
    # yet (or it's a plain line, category ""): otherwise it would sit under
    # whatever heading happens to be last, e.g. Buddy Network under "Business".
    labels = {e["label"] for e in out if e["type"] == DIVIDER}
    added = set()
    for category, page_cls in registry:
        if page_cls.tool_id not in seen:
            seen.add(page_cls.tool_id)
            if category not in added and (not category or category not in labels):
                out.append({"type": DIVIDER, "label": category})
                added.add(category)
            out.append({"type": TOOL, "id": page_cls.tool_id, "visible": True})

    if not any(e["type"] == TOOL and e["visible"] for e in out):
        for e in out:
            if e["type"] == TOOL:
                e["visible"] = True
                break
    return out


def load(shared_settings, registry) -> list[dict]:
    return reconcile(shared_settings.get(SETTINGS_KEY), registry)


def save(shared_settings, layout) -> None:
    shared_settings[SETTINGS_KEY] = [dict(e) for e in layout]
    shared_settings.save()


def visible_tool_ids(layout) -> list[str]:
    return [e["id"] for e in layout if e["type"] == TOOL and e["visible"]]


# ------------------------------------------------ Organize sidebar edits --
# Settings > Organize sidebar (nav_organizer.py) edits a copy of the layout
# with these. Each returns an error message, or "" when it's done.

def _row(layout, index):
    return index if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(layout) else None


def move(layout, src, dst) -> str:
    """Moves entry src to position dst (as it lands, after removal)."""
    src, dst = _row(layout, src), _row(layout, dst)
    if src is None or dst is None or src == dst:
        return "nothing"
    layout.insert(dst, layout.pop(src))
    return ""


def set_visible(layout, index, visible) -> str:
    index = _row(layout, index)
    if index is None or layout[index]["type"] != TOOL:
        return "nothing"
    if not visible and visible_tool_ids(layout) == [layout[index]["id"]]:
        return "At least one tool has to stay visible."
    layout[index]["visible"] = bool(visible)
    return ""


def add_divider(layout, index, label) -> str:
    index = index if _row(layout, index) is not None else 0
    layout.insert(index, {"type": DIVIDER, "label": " ".join(str(label or "").split())[:40]})
    return ""


def rename_divider(layout, index, label) -> str:
    index = _row(layout, index)
    if index is None or layout[index]["type"] != DIVIDER:
        return "nothing"
    layout[index]["label"] = " ".join(str(label or "").split())[:40]
    return ""


def remove_divider(layout, index) -> str:
    index = _row(layout, index)
    if index is None or layout[index]["type"] != DIVIDER:
        return "nothing"
    layout.pop(index)
    return ""


def show_all(layout) -> str:
    for entry in layout:
        if entry["type"] == TOOL:
            entry["visible"] = True
    return ""
