"""Generated avatars - no Qt here (the page paints them).

Each person's picture is drawn from a short code: a colour and a
symmetric 5x5 pattern, the same on every PC. The code is their "avatar"
seed if they've picked one (random hex, "New avatar" in Buddy - the server
keeps it with the account, and up to 6 favourites), else their ID. Nothing
is uploaded or downloaded, so an avatar can't carry anything a person
didn't mean to share (a photo, its location data) and fetching one tells no
other server who's looking. A deleted account has none.
"""

from __future__ import annotations

import colorsys
import hashlib
import re
import secrets

GRID = 5
MAX_SAVED = 6                        # server/core.py MAX_SAVED_AVATARS
SEED = re.compile(r"[0-9a-f]{8,16}")  # server/core.py _AVATAR_SEED


def new_seed() -> str:
    return secrets.token_hex(4)


def key_of(person: dict) -> str:
    """What someone's avatar is drawn from: their seed, or their ID."""
    seed = person.get("avatar") or ""
    return seed if SEED.fullmatch(seed) else person.get("id") or ""


def avatar(user_id: str) -> tuple[str, list[tuple[int, int]]]:
    """("#rrggbb", [(column, row), ...]) - the filled cells, mirrored left
    to right like a face."""
    digest = hashlib.sha256(f"buddy-avatar|{user_id}".encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535
    r, g, b = colorsys.hls_to_rgb(hue, 0.52, 0.55)
    color = f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"
    bits = int.from_bytes(digest[2:6], "big")
    cells = []
    half = (GRID + 1) // 2
    for row in range(GRID):
        for column in range(half):
            if bits >> (row * half + column) & 1:
                cells.append((column, row))
                if column != GRID - 1 - column:
                    cells.append((GRID - 1 - column, row))
    if not cells:
        cells = [(GRID // 2, GRID // 2)]
    return color, sorted(cells)
