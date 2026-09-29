"""What the web page needs that isn't Qt: avatars as inline SVG, the
message colours as CSS, and the sidebar's list of rooms, saved rooms and
buddies. No Qt here, so it's tested directly.

Avatars are drawn from avatars.avatar(key) - a colour and a 5x5 pattern -
into an SVG data: URL. Nothing is fetched, and the page's
Content-Security-Policy allows data: images and local files only, so even
a mistake here couldn't make it load anything from the web.
"""

from __future__ import annotations

import base64
import functools

from . import avatars, render, shades


@functools.lru_cache(maxsize=512)
def avatar_url(key: str) -> str:
    """An SVG data: URL for the avatar drawn from `key` (a seed or an ID).
    `key` only ever picks the colour and pattern - it's never written into
    the SVG."""
    color, cells = avatars.avatar(key or "")
    size = 100
    cell = size * 0.56 / avatars.GRID
    origin = (size - cell * avatars.GRID) / 2
    rects = "".join(
        f'<rect x="{origin + c * cell:.2f}" y="{origin + r * cell:.2f}" width="{cell + 0.4:.2f}" '
        f'height="{cell + 0.4:.2f}"/>' for c, r in cells)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
           f'<circle cx="50" cy="50" r="50" fill="{color}"/>'
           f'<g fill="#fff" fill-opacity="0.92">{rects}</g></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("ascii")).decode("ascii")


def message_colors(tokens: dict) -> dict[str, str]:
    """render.room_html's colours for the web page: the theme's CSS
    variables (so they follow a theme change), and the action shades
    shades.py mixes for this theme."""
    muted = tokens.get("outline", "#938F99")
    surface = tokens.get("surface", "#1C1B1F")
    colors = {
        "text": "var(--text)",
        "muted": "var(--text-dim)",
        "me": "var(--accent-text)",
        "other": "var(--text-strong)",
        "link": "var(--link)",
        "warning": "var(--second-text)",
        "badge": "var(--accent-text)",
        "mention": "color-mix(in srgb, var(--accent-text) 16%, transparent)",
    }
    colors.update(shades.action_colors(tokens, muted, surface))
    return colors


def sidebar(*, system: list[dict], mine: list[dict], saved: list[dict], buddies: list[dict],
            me_id: str | None, current: str, unread: dict, mentioned: set, muted, dm_id) -> list[dict]:
    """Sections of the sidebar: {"heading", "items": [...]}. An item is
    {"key" (a room id, or "user:<id>" for a buddy), "room" (the room id it
    opens), "label", "sub", "avatar", "online", "pinned", "unread",
    "mention", "muted", "current"}. Everything shown is data - the page
    puts it in with textContent."""
    def room_item(room):
        return {"key": room["id"], "room": room["id"], "label": room["name"], "sub": room.get("topic", ""),
                "avatar": None, "online": None, "kind": "room",
                # Everyone's rooms never expire anyway: no pin for a room made public.
                "pinned": bool(room.get("permanent")) and room.get("kind") != "system"}

    def buddy_item(person):
        room_id = dm_id(me_id, person["id"]) if me_id else ""
        return {"key": f"user:{person['id']}", "room": room_id, "label": person.get("name") or "Someone",
                "tag": person.get("tag", ""), "sub": "Online" if person.get("online") else "Offline",
                "avatar": avatar_url(avatars.key_of(person)), "online": bool(person.get("online")),
                "pinned": False, "kind": "buddy"}

    by_name = lambda r: (r.get("name") or "").casefold()   # noqa: E731
    sections = [("", [room_item(r) for r in system]),
                ("Your rooms", [room_item(r) for r in sorted(mine, key=by_name)]),
                ("Saved", [room_item(r) for r in sorted(saved, key=by_name)]),
                ("Buddies", [buddy_item(b) for b in buddies])]
    out = []
    for heading, items in sections:
        if heading and not items:
            continue
        for item in items:
            room_id = item["room"]
            item.update(unread=unread.get(room_id, 0), mention=room_id in mentioned,
                        muted=muted(room_id), current=bool(room_id) and room_id == current)
        out.append({"heading": heading, "items": items})
    return out


def person_label(person: dict) -> str:
    return render.display_name(person)


def parse_buddy_id(text: str) -> str:
    """The 32-hex-character ID in what was pasted ("Name #tag (id)" works
    too), lowercased - or "" if there isn't one."""
    text = (text or "").strip()
    candidates = [w.strip("()[]<>#,.") for w in text.split()] or [text]
    return next((w.lower() for w in candidates if len(w) == 32 and all(
        c in "0123456789abcdefABCDEF" for c in w)), "")


def people_view(people: list[dict]) -> list[dict]:
    """People for a list in the page: {"id", "name", "tag", "label",
    "avatar", "online"} - plain data, drawn with textContent."""
    return [{"id": p["id"], "name": p.get("name") or "Someone", "tag": p.get("tag", ""),
             "label": render.display_name(p), "avatar": avatar_url(avatars.key_of(p)),
             "online": bool(p.get("online"))} for p in people if p.get("id")]
