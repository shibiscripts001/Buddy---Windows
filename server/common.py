"""What core.py and social.py share: the error a request handler raises,
and the shapes users, rooms and messages take on the wire."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json

TAG_CHARS = 6
# IDs starting with this are staff numbers, handed out on the server with
# `python -m server give-tag` (never by chance: new random IDs that start
# this way are rolled again). Their tag drops the leading zeros: an ID
# starting 000001 shows as #1.
STAFF_PREFIX = "0000"


class RequestError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def tag_of(user_id: str) -> str:
    tag = user_id[:TAG_CHARS]
    if user_id.startswith(STAFF_PREFIX):
        return tag.lstrip("0") or "0"
    return tag


def new_user_id(token_hex) -> str:
    """A random 32-character ID outside the staff range."""
    while True:
        user_id = token_hex(16)
        if not user_id.startswith(STAFF_PREFIX):
            return user_id


def staff_id(number: int, token_hex) -> str:
    """The ID for staff number 1-99: 0000 + the number as two digits, then
    random - tag #1 for 1, #42 for 42."""
    if not 1 <= number <= 99:
        raise ValueError("Staff numbers are 1-99.")
    return f"{STAFF_PREFIX}{number:02d}" + token_hex(13)


def key_bytes(raw) -> bytes | None:
    """A device's public key (32 bytes, base64) as bytes; None if it isn't one."""
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return key if len(key) == 32 else None


def device_id(key: bytes) -> str:
    """What a PC's key is called (the same in app/pages/buddy_network/e2e.py)."""
    return hashlib.sha256(key).hexdigest()[:16]


def network_of(ip: str) -> str:
    """What per-network limits and network bans count by: an IPv4 address
    as it is, but an IPv6 address's /64 - one home or phone is given a whole
    /64, so counting single IPv6 addresses would count nothing."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return f"{ipaddress.ip_network(f'{address}/64', strict=False).network_address}/64"
    return ip


def public_user(user: dict) -> dict:
    # role ("user", "mod", "admin", "owner") is what the client's badge comes from
    # - never anything a user typed.
    # avatar: the seed their generated avatar is drawn from; "" means from the ID.
    return {"id": user["id"], "tag": tag_of(user["id"]), "name": user["name"],
            "role": user.get("role") or "user", "avatar": user.get("avatar") or ""}


def public_room(row: dict, here: int | None = None) -> dict:
    owner = None
    if row.get("owner_id"):
        owner = {"id": row["owner_id"], "tag": tag_of(row["owner_id"]), "name": row["owner_name"]}
    room = {"id": row["id"], "name": row["name"], "topic": row["topic"], "kind": row["kind"], "owner": owner,
            "announcement": row.get("announcement") or "", "slow": row.get("slow") or 0,
            "permanent": bool(row.get("permanent"))}
    if here is not None:
        room["here"] = here
    return room


REPLY_SNIPPET = 120


def _reply(row: dict) -> dict | None:
    """What a reply quotes: who and the start of what they said. The
    original may be gone (purged); a DM's text is encrypted, so a DM's
    quote has no text and the client finds it itself."""
    if not row.get("reply_to"):
        return None
    if not row.get("reply_found"):
        return {"id": row["reply_to"], "gone": True}
    author_id = row.get("reply_author_id") or ""
    text = "" if row.get("reply_deleted") else " ".join((row.get("reply_text") or "").split())
    return {"id": row["reply_to"], "deleted": bool(row.get("reply_deleted")),
            "text": text[:REPLY_SNIPPET] + ("..." if len(text) > REPLY_SNIPPET else ""),
            **({"image": True} if row.get("reply_image") and not row.get("reply_deleted") else {}),
            "author": {"id": author_id, "tag": tag_of(author_id), "name": row.get("reply_author_name"),
                       "role": row.get("reply_author_role") or "user",
                       "avatar": row.get("reply_author_avatar") or ""}}


def _image(row: dict) -> dict | None:
    """A message's image as the client needs it to fetch and lay it out -
    never the bytes (get_image sends those). "gone": expired or deleted.
    A DM's carries its wrapped keys ("enc") - the image is encrypted."""
    if not row.get("image") or row.get("deleted"):
        return None
    if not row.get("image_found"):
        return {"id": row["image"], "gone": True}
    image = {"id": row["image"], "w": row["image_w"], "h": row["image_h"]}
    if row.get("image_enc"):
        image["enc"] = json.loads(row["image_enc"])
    return image


def public_message(row: dict) -> dict:
    # author_id is empty once the author has deleted their account.
    author_id = row["author_id"] or ""
    reply = _reply(row)
    image = _image(row)
    return {
        "id": row["id"],
        "room": row["room"],
        "author": {"id": author_id, "tag": tag_of(author_id), "name": row["author_name"],
                   "role": row.get("author_role") or "user", "avatar": row.get("author_avatar") or ""},
        "text": row["text"],
        "ts": row["ts"],
        "deleted": bool(row["deleted"]),
        # A DM: what the sender's Buddy encrypted. Passed on untouched.
        **({"enc": json.loads(row["enc"])} if row.get("enc") else {}),
        **({"edited": row["edited"]} if row.get("edited") else {}),
        **({"reply": reply} if reply else {}),
        **({"image": image} if image else {}),
    }
