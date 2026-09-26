"""@mentions in chat text - no Qt here.

Buddy writes a mention as "@Name#tag" (the message box's name completion
does it): the name, then the tag from the person's ID - six hex characters,
or a staff number (#1). The tag is what makes it one particular person, as
names aren't unique. The server finds the same pattern (server/core.py
MENTION - keep the two in step; tests check) and tells that person, and
Buddy highlights a message that mentions you.
"""

from __future__ import annotations

import re

MENTION = re.compile(r"@([^@#\n]{2,24}?)#([0-9a-f]{6}|[1-9][0-9]?)(?![0-9a-z])")


def token(person: dict) -> str:
    return f"@{person.get('name') or 'Someone'}#{person.get('tag', '')}"


def find(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, name, tag) for each mention in the text."""
    return [(m.start(), m.end(), m.group(1), m.group(2)) for m in MENTION.finditer(text or "")]


def is_me(name: str, tag: str, me: dict | None) -> bool:
    return bool(me and me.get("name")) and tag == me.get("tag") and \
        name.strip().casefold() == me["name"].casefold()


def mentions_me(text: str, me: dict | None) -> bool:
    return any(is_me(name, tag, me) for _s, _e, name, tag in find(text))


def partial_before(text: str, cursor: int) -> tuple[int, str] | None:
    """While typing "@Sa|": (where the "@" is, "Sa") - what the name
    completion matches against. None when the cursor isn't in one."""
    start = text.rfind("@", 0, cursor)
    if start < 0:
        return None
    typed = text[start + 1:cursor]
    if "\n" in typed or "#" in typed or len(typed) > 24 or (start and not text[start - 1].isspace()):
        return None
    return start, typed
