"""A copy of your direct messages kept on this PC - only while "Keep a copy
of my direct messages on this PC" is ticked (Settings > Buddy Network). No
Qt here.

The server forgets messages after 30 days, and an encrypted DM can only be
read on a PC it was encrypted for; this copy keeps what this PC has read,
so it's still here after both. One file per conversation in
~/.buddy_network/saved_chats/, named by a hash (so the folder doesn't list
who you talk to) and locked to this Windows account with DPAPI, like the
encryption key (e2e.protect). Written with core/atomic_io.

A message deleted by its sender is deleted here too - "delete for
everyone" means this copy as well.
"""

from __future__ import annotations

import hashlib
import json
import os

from core import atomic_io

from . import e2e

FOLDER = "saved_chats"
SUFFIX = ".chat"
MAX_KEPT = 20000   # messages per conversation; the oldest go first


def _stored(m: dict) -> dict:
    author = m.get("author") or {}
    stored = {"id": m["id"], "room": m["room"], "ts": m["ts"], "deleted": bool(m.get("deleted")),
              "text": "" if m.get("deleted") else m.get("text", ""),
              "author": {k: author.get(k) for k in ("id", "tag", "name", "role")}}
    if m.get("edited"):
        stored["edited"] = m["edited"]
    if isinstance(m.get("reply"), dict) and isinstance(m["reply"].get("id"), int):
        stored["reply"] = {"id": m["reply"]["id"], "author": m["reply"].get("author") or {}}
    return stored


class ChatArchive:
    def __init__(self, folder: str):
        self.folder = folder
        self._cache: dict[str, dict] = {}   # file path -> its contents
        self.warnings: list[str] = []

    def _path(self, url: str, me: str, room: str) -> str:
        name = hashlib.sha256(f"{url}|{me}|{room}".encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.folder, name + SUFFIX)

    def _read(self, path: str) -> dict | None:
        if path in self._cache:
            return self._cache[path]
        try:
            outer = atomic_io.read_json(path, default=None)
            if outer is None:
                return None
            data = json.loads(e2e.unprotect(outer.get("protection", ""), outer.get("data", "")).decode("utf-8"))
        except (atomic_io.CorruptFileError, e2e.CryptoError, ValueError, AttributeError, OSError):
            return None   # another Windows account's, or damaged: left alone
        if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
            return None
        self._cache[path] = data
        return data

    def _write(self, path: str, data: dict):
        how, locked = e2e.protect(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        atomic_io.write_json(path, {"protection": how, "data": locked}, indent=None)
        self._cache[path] = data

    # ------------------------------------------------------------ writing

    def save(self, url: str, me: str, room: str, other: dict, messages) -> int:
        """Adds (or updates) messages of one conversation; returns how many
        were new. Ones this PC couldn't read aren't kept."""
        path = self._path(url, me, room)
        data = self._read(path) or {"server": url, "me": me, "room": room, "messages": {}}
        data["other"] = {k: (other or {}).get(k) for k in ("id", "tag", "name", "role")}
        kept = data["messages"]
        new = changed = 0
        for m in messages:
            if m.get("unreadable") or m.get("room") != room:
                continue
            key = str(m["id"])
            if key in kept and kept[key]["deleted"] and not m.get("deleted"):
                continue   # deleted stays deleted
            stored = _stored(m)
            if kept.get(key) != stored:
                new += key not in kept
                kept[key] = stored
                changed += 1
        if not changed and os.path.exists(path):
            return 0
        if len(kept) > MAX_KEPT:
            for key in sorted(kept, key=int)[:len(kept) - MAX_KEPT]:
                del kept[key]
        self._write(path, data)
        return new

    def mark_deleted(self, url: str, me: str, room: str, message_id: int):
        path = self._path(url, me, room)
        data = self._read(path)
        m = data and data["messages"].get(str(message_id))
        if m and not m["deleted"]:
            m["deleted"], m["text"] = True, ""
            self._write(path, data)
            self._write(path, data)   # again, so the .bak copy doesn't keep what it said either

    def forget(self, url: str, me: str, room: str, message_id: int):
        """A message deleted forever: gone from the copy too, with no
        placeholder - and from its .bak, hence the second write."""
        path = self._path(url, me, room)
        data = self._read(path)
        if data and data["messages"].pop(str(message_id), None) is not None:
            self._write(path, data)
            self._write(path, data)

    def delete(self, url: str, me: str, room: str):
        path = self._path(url, me, room)
        self._cache.pop(path, None)
        for p in (path, atomic_io.backup_path(path)):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def delete_all(self, url: str) -> int:
        """Every saved conversation for that server; returns how many."""
        gone = 0
        for c in self.conversations(url):
            self.delete(url, c["me"], c["room"])
            gone += 1
        return gone

    # ---------------------------------------------------------- transfers

    def export_all(self, url: str, me: str) -> list[dict]:
        """Every conversation of this identity there, for a transfer file."""
        out = []
        for c in self.conversations(url):
            data = c["me"] == me and self._read(self._path(url, me, c["room"]))
            if data:
                out.append(json.loads(json.dumps(data)))
        return out

    def import_all(self, url: str, me: str, chats) -> int:
        """Adds a transfer's conversations to what's saved here; returns how
        many messages were new."""
        new = 0
        for data in chats:
            if not isinstance(data, dict) or not isinstance(data.get("room"), str) \
                    or not isinstance(data.get("messages"), dict):
                continue
            messages = [m for m in data["messages"].values()
                        if isinstance(m, dict) and isinstance(m.get("id"), int) and isinstance(m.get("ts"), (int, float))
                        and isinstance(m.get("author"), dict) and m.get("room") == data["room"]]
            new += self.save(url, me, data["room"], data.get("other") or {}, messages)
        return new

    # ------------------------------------------------------------ reading

    def messages(self, url: str, me: str, room: str) -> list[dict]:
        """Oldest first."""
        data = self._read(self._path(url, me, room))
        if not data:
            return []
        return [dict(m, author=dict(m["author"])) for m in sorted(data["messages"].values(), key=lambda m: m["id"])]

    def conversations(self, url: str) -> list[dict]:
        """Every conversation saved for that server that this Windows
        account can open: {"me", "room", "other", "count", "last"}, the most
        recent first."""
        try:
            names = [n for n in os.listdir(self.folder) if n.endswith(SUFFIX)]
        except OSError:
            return []
        out = []
        for name in names:
            data = self._read(os.path.join(self.folder, name))
            if not data or data.get("server") != url:
                continue
            kept = data["messages"].values()
            out.append({"me": data.get("me", ""), "room": data.get("room", ""), "other": data.get("other") or {},
                        "count": len(kept), "last": max((m["ts"] for m in kept), default=0.0)})
        return sorted(out, key=lambda c: -c["last"])
