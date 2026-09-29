"""Reactions: old-school emoticons, not emoji pictures - one fixed list,
the same as server/core.py's REACTIONS (tests check), so a reaction is only
ever one of these and never anything a person typed.

The server sends a message's as [{"r": key, "count", "mine", "people":
[{"id", "tag", "name"}, ...the first few]}]; keys this Buddy doesn't know
(from a newer server) are left out.
"""

from __future__ import annotations

# (key, what's shown, what it's called - the picker's tooltip), in the picker's order.
REACTIONS = (
    ("smile", ":)", "smile"),
    ("grin", ":D", "grin"),
    ("heart", "<3", "heart"),
    ("wink", ";)", "wink"),
    ("tongue", ":P", "tongue out"),
    ("sad", ":(", "sad"),
    ("wow", ":O", "wow"),
    ("laugh", "XD", "laughing"),
    ("cool", "8)", "cool"),
    ("happy", "^_^", "happy"),
    ("cheer", "\\o/", "cheer"),
    ("shrug", "\u00af\\_(\u30c4)_/\u00af", "shrug"),
)
TEXT = {key: text for key, text, _name in REACTIONS}
NAMES = {key: name for key, _text, name in REACTIONS}


def known(reactions) -> list[dict]:
    """A message's reactions as the server sent them, keeping only the
    well-formed ones this Buddy can show."""
    if not isinstance(reactions, list):
        return []
    out = []
    for r in reactions:
        if (isinstance(r, dict) and r.get("r") in TEXT and isinstance(r.get("count"), int)
                and not isinstance(r.get("count"), bool) and r["count"] > 0):
            people = [p for p in r.get("people") or [] if isinstance(p, dict) and isinstance(p.get("id"), str)]
            out.append({"r": r["r"], "count": r["count"], "mine": bool(r.get("mine")), "people": people})
    return out


def is_mine(reactions, key: str) -> bool:
    return any(r["r"] == key and r["mine"] for r in known(reactions))
