"""SQLite storage for the Buddy Network server.

Holds users (a random id, a hash of their secret token, a chosen name and a
role), rooms and messages, and the Buddy System: buddy pairs, buddy
requests and blocks. A direct-message conversation is a room of kind "dm"
whose id names its two people (dm_room_id), so DMs get the same history,
paging, delete and 30-day purge as every other room.

DMs are end-to-end encrypted: a DM's row has no text, only `enc`
- what the sender's Buddy encrypted, which this server can't read - and
`devices` holds each PC's public key, handed to buddies so they can
encrypt for it (app/pages/buddy_network/e2e.py has the scheme).

Admin tools: bans (by user; optionally a keyed hash of a banned
user's network address - never the address itself), reports, the admin
log, and a pinned announcement per room. No IP addresses, ever. Messages
older than HISTORY_DAYS are deleted by purge_before(), which the server runs at start-up and every hour.

Images: a message can carry one, kept in `images` - the bytes
Buddy sent, already shrunk on the sender's PC - for IMAGE_DAYS (core.py),
then deleted by purge_images() while the message stays, saying its image
has expired. A DM's image is encrypted like its text (`enc` holds its
wrapped keys); a room's is a plain WebP, JPEG or PNG. Deleting a message
deletes its image straight away. `credit` says where a GIF from the GIF
picker came from (GIPHY, and whose it is - server/gifs.py).

Plain sqlite3 calls from the event loop: every query here is an indexed
lookup on a small database, far quicker than a network round-trip.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3

SCHEMA_VERSION = 11

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    created REAL NOT NULL,
    appear_offline INTEGER NOT NULL DEFAULT 0,
    avatar TEXT NOT NULL DEFAULT '',          -- the seed their avatar is drawn from ('' = from the ID)
    avatars_saved TEXT NOT NULL DEFAULT '[]'  -- up to 6 seeds they liked (JSON), only ever sent to them
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    owner TEXT,
    created REAL NOT NULL,
    last_active REAL,
    announcement TEXT NOT NULL DEFAULT '',
    slow INTEGER NOT NULL DEFAULT 0,    -- slow mode: seconds between one person's messages
    permanent INTEGER NOT NULL DEFAULT 0   -- never deleted for being idle (see ROOM_IDLE_DAYS)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    enc TEXT,               -- a DM: the encrypted message (JSON); text is ''
    reply_to INTEGER,       -- the message this one answers
    edited REAL,            -- when its author last changed it
    image TEXT              -- its image's id (the image itself may have expired)
);
CREATE INDEX IF NOT EXISTS messages_by_room ON messages (room, id);
CREATE INDEX IF NOT EXISTS messages_by_time ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_by_author ON messages (author);
-- One row per pair of buddies, the smaller id first.
CREATE TABLE IF NOT EXISTS buddies (a TEXT NOT NULL, b TEXT NOT NULL, since REAL NOT NULL,
                                    PRIMARY KEY (a, b));
CREATE INDEX IF NOT EXISTS buddies_by_b ON buddies (b);
-- declined: the target said no. The sender still sees it as waiting - a
-- decline looks exactly like a block or being ignored.
CREATE TABLE IF NOT EXISTS buddy_requests (sender TEXT NOT NULL, target TEXT NOT NULL,
                                           created REAL NOT NULL, declined INTEGER NOT NULL DEFAULT 0,
                                           PRIMARY KEY (sender, target));
CREATE INDEX IF NOT EXISTS requests_by_target ON buddy_requests (target);
CREATE TABLE IF NOT EXISTS blocks (blocker TEXT NOT NULL, blocked TEXT NOT NULL, created REAL NOT NULL,
                                   PRIMARY KEY (blocker, blocked));
CREATE INDEX IF NOT EXISTS blocks_by_blocked ON blocks (blocked);
-- until: when the ban ends (NULL = permanent).
CREATE TABLE IF NOT EXISTS bans (user_id TEXT PRIMARY KEY, until REAL, reason TEXT NOT NULL DEFAULT '',
                                 by TEXT NOT NULL, created REAL NOT NULL);
-- A keyed hash of a banned user's address (see ip_hash): stops new
-- identities from that network while the ban lasts.
CREATE TABLE IF NOT EXISTS network_bans (ip_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, until REAL,
                                         created REAL NOT NULL);
-- text: the message as it was when reported, so a deleted one can still
-- be judged. claimed: an encrypted DM, whose text the reporter's Buddy
-- supplied - the server can't check it.
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
                                    room TEXT NOT NULL, reporter TEXT NOT NULL, reported TEXT NOT NULL,
                                    text TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                                    resolved_by TEXT, claimed INTEGER NOT NULL DEFAULT 0,
                                    image TEXT,     -- the reported message's image (a room's only)
                                    UNIQUE (message_id, reporter));
-- Each PC a user is signed in on: its public key for encrypted DMs
-- (device: the first 16 hex characters of the key's SHA-256).
CREATE TABLE IF NOT EXISTS devices (user_id TEXT NOT NULL, device TEXT NOT NULL, key TEXT NOT NULL,
                                    created REAL NOT NULL, last_seen REAL NOT NULL,
                                    PRIMARY KEY (user_id, device));
-- Announcements for every Buddy, chat user or not: served as
-- /announcements.json (net.py) and shown behind the orb next to "Buddy".
CREATE TABLE IF NOT EXISTS app_announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                                              title TEXT NOT NULL, text TEXT NOT NULL, by TEXT NOT NULL);
-- A message's image, deleted after IMAGE_DAYS (core.py) or with its message.
-- enc: a DM's, whose data is encrypted - its wrapped keys (JSON).
-- credit: a GIF from GIF search - where it's from (JSON, gifs.gif_credit).
CREATE TABLE IF NOT EXISTS images (id TEXT PRIMARY KEY, room TEXT NOT NULL, author TEXT NOT NULL,
                                   ts REAL NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL, enc TEXT,
                                   size INTEGER NOT NULL, data BLOB NOT NULL, credit TEXT);
CREATE INDEX IF NOT EXISTS images_by_time ON images (ts);
CREATE INDEX IF NOT EXISTS images_by_room ON images (room);
CREATE INDEX IF NOT EXISTS images_by_size ON images (size);
CREATE TABLE IF NOT EXISTS admin_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                                      actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
                                      detail TEXT NOT NULL DEFAULT '');
"""


def dm_room_id(x: str, y: str) -> str:
    a, b = sorted((x, y))
    return f"dm-{a}-{b}"


def dm_people(room_id: str) -> tuple[str, str] | None:
    parts = room_id.split("-")
    return (parts[1], parts[2]) if len(parts) == 3 and parts[0] == "dm" else None


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # Deleted rows are overwritten with zeros, not just marked free - a
        # deleted or purged message doesn't linger in the file.
        self.db.execute("PRAGMA secure_delete=ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self._migrate()
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        self.db.execute("UPDATE meta SET value = ? WHERE key = 'schema'", (str(SCHEMA_VERSION),))
        self.db.commit()

    def _migrate(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(rooms)")}
        if "last_active" not in columns:   # the oldest databases
            self.db.execute("ALTER TABLE rooms ADD COLUMN last_active REAL")
        self.db.execute("UPDATE rooms SET last_active = created WHERE last_active IS NULL")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(users)")}
        for column in ("avatar", "avatars_saved"):   # older databases
            if column not in columns:
                default = "'[]'" if column == "avatars_saved" else "''"
                self.db.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}")
        if "appear_offline" not in columns:   # older databases
            self.db.execute("ALTER TABLE users ADD COLUMN appear_offline INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(rooms)")}
        if "announcement" not in columns:     # older databases
            self.db.execute("ALTER TABLE rooms ADD COLUMN announcement TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "enc" not in columns:              # older databases
            self.db.execute("ALTER TABLE messages ADD COLUMN enc TEXT")
            # DMs from before encryption were readable here: gone, rather
            # than kept next to encrypted ones until the purge.
            self.db.execute("DELETE FROM messages WHERE room LIKE 'dm-%'")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
        for column, kind in (("reply_to", "INTEGER"), ("edited", "REAL"), ("image", "TEXT")):   # older databases
            if column not in columns:
                self.db.execute(f"ALTER TABLE messages ADD COLUMN {column} {kind}")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(rooms)")}
        if "slow" not in columns:             # older databases
            self.db.execute("ALTER TABLE rooms ADD COLUMN slow INTEGER NOT NULL DEFAULT 0")
        if "permanent" not in columns:        # older databases
            self.db.execute("ALTER TABLE rooms ADD COLUMN permanent INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(reports)")}
        if "claimed" not in columns:          # older databases
            self.db.execute("ALTER TABLE reports ADD COLUMN claimed INTEGER NOT NULL DEFAULT 0")
        if "image" not in columns:            # older databases
            self.db.execute("ALTER TABLE reports ADD COLUMN image TEXT")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(images)")}
        if "credit" not in columns:           # older databases
            self.db.execute("ALTER TABLE images ADD COLUMN credit TEXT")
        # The key for ip_hash: random per server, so the hashes mean nothing
        # anywhere else (and a list of every IPv4 address hashed without
        # it would undo a plain hash in minutes).
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('ip_key', ?)", (secrets.token_hex(32),))

    def close(self):
        self.db.close()

    def compact(self):
        """Rewrites the file without its free pages, and empties the
        write-ahead log - so nothing deleted survives in either."""
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("VACUUM")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ------------------------------------------------------------- users

    def create_user(self, user_id: str, token_hash: str, now: float) -> dict:
        self.db.execute("INSERT INTO users (id, token_hash, created) VALUES (?, ?, ?)",
                        (user_id, token_hash, now))
        self.db.commit()
        return self.user(user_id)

    _USER_SELECT = "SELECT id, name, role, appear_offline, avatar FROM users"

    def user(self, user_id: str) -> dict | None:
        row = self.db.execute(self._USER_SELECT + " WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def users(self, user_ids) -> list[dict]:
        ids = list(user_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(self._USER_SELECT + f" WHERE id IN ({marks})", ids)
        return sorted((dict(r) for r in rows), key=lambda u: ((u["name"] or "").casefold(), u["id"]))

    def user_by_token_hash(self, token_hash: str) -> dict | None:
        row = self.db.execute(self._USER_SELECT + " WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def set_appear_offline(self, user_id: str, offline: bool):
        self.db.execute("UPDATE users SET appear_offline = ? WHERE id = ?", (int(offline), user_id))
        self.db.commit()

    def set_role(self, user_id: str, role: str):
        self.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        self.db.commit()

    def users_with_roles(self) -> list[dict]:
        rows = self.db.execute(self._USER_SELECT + " WHERE role != 'user' ORDER BY role DESC, name")
        return [dict(r) for r in rows]

    def delete_user(self, user_id: str):
        """Everything of theirs except messages in other people's rooms,
        which stay (authorless - shown as "Deleted user"). Their own rooms
        and DM conversations must already be gone (delete_room)."""
        db = self.db
        db.execute("UPDATE messages SET author = '' WHERE author = ?", (user_id,))
        db.execute("DELETE FROM buddies WHERE a = ? OR b = ?", (user_id, user_id))
        db.execute("DELETE FROM buddy_requests WHERE sender = ? OR target = ?", (user_id, user_id))
        db.execute("DELETE FROM blocks WHERE blocker = ? OR blocked = ?", (user_id, user_id))
        db.execute("DELETE FROM devices WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()

    def purge_nameless(self, cutoff: float, keep) -> int:
        """Forgets identities made before `cutoff` that never chose a name
        and haven't been back since (no PC of theirs seen) - so made-up
        identities can't pile up. `keep`: ids signed in right now."""
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM users WHERE name IS NULL AND role = 'user' AND created < ? AND id NOT IN "
            "(SELECT user_id FROM devices WHERE last_seen >= ?)", (cutoff, cutoff)) if r[0] not in keep]
        for user_id in ids:
            self.delete_user(user_id)
        return len(ids)

    # ------------------------------------------------------ buddy system

    def buddy_ids(self, user_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT b FROM buddies WHERE a = ? UNION SELECT a FROM buddies WHERE b = ?", (user_id, user_id))]

    def are_buddies(self, x: str, y: str) -> bool:
        a, b = sorted((x, y))
        return self.db.execute("SELECT 1 FROM buddies WHERE a = ? AND b = ?", (a, b)).fetchone() is not None

    def add_buddies(self, x: str, y: str, now: float):
        a, b = sorted((x, y))
        self.db.execute("INSERT OR IGNORE INTO buddies VALUES (?, ?, ?)", (a, b, now))
        self.db.execute("DELETE FROM buddy_requests WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?)",
                        (x, y, y, x))
        self.db.commit()

    def remove_buddies(self, x: str, y: str):
        a, b = sorted((x, y))
        self.db.execute("DELETE FROM buddies WHERE a = ? AND b = ?", (a, b))
        self.db.commit()

    def request(self, sender: str, target: str) -> dict | None:
        row = self.db.execute("SELECT sender, target, declined FROM buddy_requests WHERE sender = ? AND target = ?",
                              (sender, target)).fetchone()
        return dict(row) if row else None

    def add_request(self, sender: str, target: str, now: float):
        self.db.execute("INSERT OR IGNORE INTO buddy_requests (sender, target, created) VALUES (?, ?, ?)",
                        (sender, target, now))
        self.db.commit()

    def decline_request(self, sender: str, target: str):
        self.db.execute("UPDATE buddy_requests SET declined = 1 WHERE sender = ? AND target = ?", (sender, target))
        self.db.commit()

    def delete_request(self, sender: str, target: str):
        self.db.execute("DELETE FROM buddy_requests WHERE sender = ? AND target = ?", (sender, target))
        self.db.commit()

    def incoming_ids(self, user_id: str) -> list[str]:
        """Requests to user_id they haven't declined, from people they
        haven't blocked."""
        return [r[0] for r in self.db.execute(
            "SELECT sender FROM buddy_requests WHERE target = ? AND declined = 0 AND sender NOT IN "
            "(SELECT blocked FROM blocks WHERE blocker = ?)", (user_id, user_id))]

    def outgoing_ids(self, user_id: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT target FROM buddy_requests WHERE sender = ?", (user_id,))]

    def request_counterparts(self, user_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT target FROM buddy_requests WHERE sender = ? UNION SELECT sender FROM buddy_requests "
            "WHERE target = ?", (user_id, user_id))]

    def block(self, blocker: str, blocked: str, now: float):
        self.db.execute("INSERT OR IGNORE INTO blocks VALUES (?, ?, ?)", (blocker, blocked, now))
        # Their request to the blocker is left as it is: to them it stays
        # "waiting", so they can't tell they were blocked.
        self.db.execute("DELETE FROM buddy_requests WHERE sender = ? AND target = ?", (blocker, blocked))
        self.db.commit()
        self.remove_buddies(blocker, blocked)

    def unblock(self, blocker: str, blocked: str):
        self.db.execute("DELETE FROM blocks WHERE blocker = ? AND blocked = ?", (blocker, blocked))
        self.db.commit()

    def blocked_ids(self, blocker: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT blocked FROM blocks WHERE blocker = ?", (blocker,))]

    def blocked_by_ids(self, blocked: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT blocker FROM blocks WHERE blocked = ?", (blocked,))]

    def is_blocked(self, blocker: str, blocked: str) -> bool:
        return self.db.execute("SELECT 1 FROM blocks WHERE blocker = ? AND blocked = ?",
                               (blocker, blocked)).fetchone() is not None

    def set_avatar(self, user_id: str, seed: str):
        self.db.execute("UPDATE users SET avatar = ? WHERE id = ?", (seed, user_id))
        self.db.commit()

    def saved_avatars(self, user_id: str) -> list[str]:
        row = self.db.execute("SELECT avatars_saved FROM users WHERE id = ?", (user_id,)).fetchone()
        try:
            saved = json.loads(row[0]) if row else []
        except ValueError:
            return []
        return [s for s in saved if isinstance(s, str)] if isinstance(saved, list) else []

    def set_saved_avatars(self, user_id: str, seeds: list[str]):
        self.db.execute("UPDATE users SET avatars_saved = ? WHERE id = ?", (json.dumps(seeds), user_id))
        self.db.commit()

    def set_name(self, user_id: str, name: str):
        self.db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        self.db.commit()

    # ------------------------------------------------------------ devices

    def add_device(self, user_id: str, device: str, key: str, now: float, keep: int) -> bool:
        """Registers a PC's key, or notes it was seen again. True if it's
        new; then only the `keep` most recently seen of the user's PCs stay."""
        db = self.db
        seen = db.execute("UPDATE devices SET last_seen = ? WHERE user_id = ? AND device = ? AND key = ?",
                          (now, user_id, device, key)).rowcount
        if seen:
            db.commit()
            return False
        with db:
            db.execute("INSERT OR REPLACE INTO devices VALUES (?, ?, ?, ?, ?)", (user_id, device, key, now, now))
            db.execute("DELETE FROM devices WHERE user_id = ? AND device NOT IN (SELECT device FROM devices "
                       "WHERE user_id = ? ORDER BY last_seen DESC LIMIT ?)", (user_id, user_id, keep))
        return True

    def device_keys(self, user_ids) -> dict[str, dict[str, str]]:
        """user id -> {device: key}, for each of them (empty if none)."""
        ids = list(user_ids)
        out: dict[str, dict[str, str]] = {i: {} for i in ids}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in self.db.execute(f"SELECT user_id, device, key FROM devices WHERE user_id IN ({marks}) "
                                     "ORDER BY created", ids):
                out[r[0]][r[1]] = r[2]
        return out

    def purge_devices(self, cutoff: float) -> list[str]:
        """Forgets PCs not seen since `cutoff`; returns whose they were."""
        users = [r[0] for r in self.db.execute("SELECT DISTINCT user_id FROM devices WHERE last_seen < ?",
                                               (cutoff,))]
        self.db.execute("DELETE FROM devices WHERE last_seen < ?", (cutoff,))
        self.db.commit()
        return users

    # ------------------------------------------------------------- rooms

    _ROOM_SELECT = """
        SELECT r.id, r.name, r.topic, r.kind, r.announcement, r.slow, r.permanent,
               r.owner AS owner_id, u.name AS owner_name
        FROM rooms r LEFT JOIN users u ON u.id = r.owner
    """

    def ensure_room(self, room_id: str, name: str, topic: str, kind: str, now: float):
        self.db.execute("INSERT OR IGNORE INTO rooms (id, name, topic, kind, created, last_active) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (room_id, name, topic, kind, now, now))
        self.db.commit()

    def create_room(self, room_id: str, name: str, topic: str, owner: str, now: float) -> dict:
        self.db.execute("INSERT INTO rooms (id, name, topic, kind, owner, created, last_active) "
                        "VALUES (?, ?, ?, 'user', ?, ?, ?)", (room_id, name, topic, owner, now, now))
        self.db.commit()
        return self.room(room_id)

    def room(self, room_id: str) -> dict | None:
        row = self.db.execute(self._ROOM_SELECT + " WHERE r.id = ?", (room_id,)).fetchone()
        return dict(row) if row else None

    def system_rooms(self) -> list[dict]:
        return self._rooms(" WHERE r.kind = 'system' ORDER BY r.created, r.name")

    def rooms_owned_by(self, user_id: str) -> list[dict]:
        return self._rooms(" WHERE r.owner = ? ORDER BY r.created", (user_id,))

    def rooms_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        # Never DMs: anyone can see a user's id, and asking about a DM id
        # mustn't reveal that two people talk.
        return self._rooms(f" WHERE r.id IN ({marks}) AND r.kind != 'dm' ORDER BY r.name", tuple(ids))

    def room_names(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT name FROM rooms WHERE kind != 'dm'")]

    def dm_room_ids_of(self, user_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT id FROM rooms WHERE kind = 'dm' AND (id LIKE ? OR id LIKE ?)",
            (f"dm-{user_id}-%", f"dm-%-{user_id}"))]

    def find_rooms(self, query: str, limit: int, permanent_only: bool = False) -> list[dict]:
        """User rooms whose name or topic contains `query` (all if empty),
        most recently active first; permanent_only narrows that to rooms
        marked to never expire (see set_permanent)."""
        # "!" escapes LIKE's own wildcards, so a search for "50%" means 50%.
        like = "%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        where = " WHERE r.kind = 'user' AND (r.name LIKE ? ESCAPE '!' OR r.topic LIKE ? ESCAPE '!')"
        args = [like, like]
        if permanent_only:
            where += " AND r.permanent"
        where += " ORDER BY r.last_active DESC LIMIT ?"
        args.append(limit)
        return self._rooms(where, tuple(args))

    def _rooms(self, where: str, args=()) -> list[dict]:
        return [dict(r) for r in self.db.execute(self._ROOM_SELECT + where, args)]

    def rename_room(self, room_id: str, name: str):
        self.db.execute("UPDATE rooms SET name = ? WHERE id = ?", (name, room_id))
        self.db.commit()

    def set_announcement(self, room_id: str, text: str):
        self.db.execute("UPDATE rooms SET announcement = ? WHERE id = ?", (text, room_id))
        self.db.commit()

    def set_slow(self, room_id: str, seconds: int):
        self.db.execute("UPDATE rooms SET slow = ? WHERE id = ?", (seconds, room_id))
        self.db.commit()

    def set_topic(self, room_id: str, topic: str):
        self.db.execute("UPDATE rooms SET topic = ? WHERE id = ?", (topic, room_id))
        self.db.commit()

    def set_permanent(self, room_id: str, value: bool):
        self.db.execute("UPDATE rooms SET permanent = ? WHERE id = ?", (int(value), room_id))
        self.db.commit()

    def delete_room(self, room_id: str):
        self.db.execute("DELETE FROM images WHERE room = ?", (room_id,))
        self.db.execute("DELETE FROM messages WHERE room = ?", (room_id,))
        self.db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        self.db.commit()

    def idle_user_rooms(self, cutoff: float) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT id FROM rooms WHERE kind = 'user' AND NOT permanent AND last_active < ?", (cutoff,))]

    # ---------------------------------------------------------- messages

    # r: the message it replies to (gone once purged), ru: that one's author,
    # i: its image (gone once expired) - never the image's bytes.
    _MESSAGE_SELECT = """
        SELECT m.id, m.room, m.text, m.enc, m.ts, m.deleted, m.edited, m.reply_to, m.image,
               u.id AS author_id, u.name AS author_name, u.role AS author_role, u.avatar AS author_avatar,
               r.id AS reply_found, r.text AS reply_text, r.deleted AS reply_deleted, r.image AS reply_image,
               ru.id AS reply_author_id, ru.name AS reply_author_name, ru.role AS reply_author_role,
               ru.avatar AS reply_author_avatar,
               i.id AS image_found, i.w AS image_w, i.h AS image_h, i.enc AS image_enc,
               i.credit AS image_credit
        FROM messages m LEFT JOIN users u ON u.id = m.author
             LEFT JOIN messages r ON r.id = m.reply_to LEFT JOIN users ru ON ru.id = r.author
             LEFT JOIN images i ON i.id = m.image
    """

    def add_message(self, room: str, author: str, text: str, now: float, enc: str | None = None,
                    reply_to: int | None = None, image: dict | None = None) -> dict:
        """image: {"id", "w", "h", "enc", "data", "credit"}, stored along with the message."""
        with self.db:
            if image is not None:
                self.db.execute("INSERT INTO images (id, room, author, ts, w, h, enc, size, data, credit) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (image["id"], room, author, now, image["w"], image["h"], image.get("enc"),
                                 len(image["data"]), image["data"], image.get("credit")))
            cur = self.db.execute("INSERT INTO messages (room, author, text, ts, enc, reply_to, image) "
                                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (room, author, text, now, enc, reply_to, image["id"] if image else None))
            self.db.execute("UPDATE rooms SET last_active = ? WHERE id = ?", (now, room))
        return self.message(cur.lastrowid)

    # ------------------------------------------------------------ images

    def image(self, image_id: str) -> dict | None:
        """An image, its bytes and the room it's in."""
        row = self.db.execute("SELECT id, room, author, ts, w, h, enc, data FROM images WHERE id = ?",
                              (image_id,)).fetchone()
        return dict(row) if row else None

    def image_taken(self, image_id: str) -> bool:
        """Whether any message has had this image id, even one whose image has gone."""
        return self.db.execute("SELECT 1 FROM images WHERE id = ? UNION ALL SELECT 1 FROM messages WHERE image = ? "
                               "LIMIT 1", (image_id, image_id)).fetchone() is not None

    def images_size(self) -> int:
        return self.db.execute("SELECT COALESCE(SUM(size), 0) FROM images").fetchone()[0]

    def purge_images(self, cutoff: float) -> int:
        cur = self.db.execute("DELETE FROM images WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def message(self, message_id: int) -> dict | None:
        row = self.db.execute(self._MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def history(self, room: str, before: int | None, limit: int) -> tuple[list[dict], bool]:
        """Up to `limit` messages older than message id `before` (newest if
        None), oldest first, and whether there are more before them."""
        query = self._MESSAGE_SELECT + " WHERE m.room = ?"
        args: list = [room]
        if before is not None:
            query += " AND m.id < ?"
            args.append(before)
        query += " ORDER BY m.id DESC LIMIT ?"
        args.append(limit + 1)
        rows = [dict(r) for r in self.db.execute(query, args)]
        more = len(rows) > limit
        return list(reversed(rows[:limit])), more

    def edit_message(self, message_id: int, text: str, enc: str | None, now: float):
        self.db.execute("UPDATE messages SET text = ?, enc = ?, edited = ? WHERE id = ?", (text, enc, now, message_id))
        self.db.commit()

    def unread_counts(self, after: dict[str, int], cap: int) -> dict[str, int]:
        """room -> how many messages it has after the given message id (at
        most `cap`), for each room asked about. One grouped query instead of
        one per room - a single watch can ask about up to WATCH_MAX rooms,
        which used to mean that many separate round trips to the DB."""
        out = {room: 0 for room in after}
        if not after:
            return out
        values_sql = ", ".join(["(?, ?)"] * len(after))
        params = []
        for room, last in after.items():
            params.extend((room, last))
        params.append(cap)
        rows = self.db.execute(
            f"""
            WITH wanted(room, last) AS (VALUES {values_sql}),
            numbered AS (
                SELECT m.room AS room,
                       ROW_NUMBER() OVER (PARTITION BY m.room ORDER BY m.id) AS rn
                FROM messages m
                JOIN wanted w ON w.room = m.room AND m.id > w.last
                WHERE m.deleted = 0
            )
            SELECT room, COUNT(*) FROM numbered WHERE rn <= ? GROUP BY room
            """,
            params,
        ).fetchall()
        for room, count in rows:
            out[room] = count
        return out

    def users_by_id_prefix(self, prefix: str) -> list[dict]:
        like = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        return [dict(r) for r in self.db.execute(self._USER_SELECT + " WHERE id LIKE ? ESCAPE '!' LIMIT 20",
                                                 (like,))]

    def mark_deleted(self, message_id: int):
        """Blanks the text and deletes the image but keeps the row, so the
        chat can still show "message deleted" where it was."""
        with self.db:
            self.db.execute("DELETE FROM images WHERE id = (SELECT image FROM messages WHERE id = ?)", (message_id,))
            self.db.execute("UPDATE messages SET text = '', enc = NULL, deleted = 1 WHERE id = ?", (message_id,))

    def purge_message(self, message_id: int):
        """Gone for good (the owner's "delete forever"): the message, its
        reports, and the quote in any reply to it."""
        with self.db:
            self.db.execute("DELETE FROM images WHERE id = (SELECT image FROM messages WHERE id = ?)", (message_id,))
            self.db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self.db.execute("UPDATE messages SET reply_to = NULL WHERE reply_to = ?", (message_id,))
            self.db.execute("DELETE FROM reports WHERE message_id = ?", (message_id,))

    def purge_before(self, cutoff: float) -> int:
        self.db.execute("DELETE FROM images WHERE ts < ?", (cutoff,))
        cur = self.db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    # --------------------------------------------------------- admin tools

    def ip_hash(self, ip: str) -> str:
        key = self.db.execute("SELECT value FROM meta WHERE key = 'ip_key'").fetchone()[0]
        return hashlib.sha256(f"{key}|{ip}".encode("utf-8")).hexdigest()

    def ban(self, user_id: str, until: float | None, reason: str, by: str, now: float):
        self.db.execute("INSERT OR REPLACE INTO bans VALUES (?, ?, ?, ?, ?)", (user_id, until, reason, by, now))
        self.db.commit()

    def ban_network(self, ip_hash: str, user_id: str, until: float | None, now: float):
        self.db.execute("INSERT OR REPLACE INTO network_bans VALUES (?, ?, ?, ?)", (ip_hash, user_id, until, now))
        self.db.commit()

    def unban(self, user_id: str):
        self.db.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
        self.db.execute("DELETE FROM network_bans WHERE user_id = ?", (user_id,))
        self.db.commit()

    def ban_of(self, user_id: str, now: float) -> dict | None:
        row = self.db.execute("SELECT * FROM bans WHERE user_id = ? AND (until IS NULL OR until > ?)",
                              (user_id, now)).fetchone()
        return dict(row) if row else None

    def network_ban(self, ip_hash: str, now: float) -> dict | None:
        row = self.db.execute("SELECT until FROM network_bans WHERE ip_hash = ? AND (until IS NULL OR until > ?)",
                              (ip_hash, now)).fetchone()
        return dict(row) if row else None

    def bans(self, now: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT b.user_id, b.until, b.reason, b.by, b.created, u.name, "
            "EXISTS (SELECT 1 FROM network_bans n WHERE n.user_id = b.user_id) AS network "
            "FROM bans b LEFT JOIN users u ON u.id = b.user_id "
            "WHERE b.until IS NULL OR b.until > ? ORDER BY b.created DESC", (now,))
        return [dict(r) for r in rows]

    def add_report(self, message: dict, reporter: str, reason: str, now: float,
                   claimed_text: str | None = None) -> bool:
        """False if this person had already reported this message.
        claimed_text: an encrypted DM's text, as the reporter's Buddy read it."""
        text = message["text"] if claimed_text is None else claimed_text
        # A room's image, so staff can see it; a DM's is encrypted - no use to them.
        image = message.get("image_found") if not message.get("image_enc") else None
        cur = self.db.execute(
            "INSERT OR IGNORE INTO reports (message_id, room, reporter, reported, text, reason, created, claimed, "
            "image) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message["id"], message["room"], reporter, message["author_id"] or "", text, reason, now,
             int(claimed_text is not None), image))
        self.db.commit()
        return cur.rowcount == 1

    def open_reports(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.id, r.message_id, r.room, r.text, r.reason, r.created, r.reporter, r.reported, r.claimed, "
            "(SELECT i.id FROM images i WHERE i.id = r.image) AS image, "
            "a.name AS reporter_name, b.name AS reported_name, rm.name AS room_name, rm.kind AS room_kind, "
            "(SELECT COUNT(*) FROM reports o WHERE o.message_id = r.message_id) AS times "
            "FROM reports r LEFT JOIN users a ON a.id = r.reporter LEFT JOIN users b ON b.id = r.reported "
            "LEFT JOIN rooms rm ON rm.id = r.room WHERE r.resolved_by IS NULL ORDER BY r.created")
        return [dict(r) for r in rows]

    def report(self, report_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return dict(row) if row else None

    def resolve_reports_for(self, message_id: int, by: str):
        self.db.execute("UPDATE reports SET resolved_by = ? WHERE message_id = ? AND resolved_by IS NULL",
                        (by, message_id))
        self.db.commit()

    def log(self, actor: str, action: str, target: str, detail: str, now: float):
        self.db.execute("INSERT INTO admin_log (ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                        (now, actor, action, target, detail))
        self.db.commit()

    def admin_log(self, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT l.id, l.ts, l.actor, u.name AS actor_name, l.action, l.target, l.detail "
            "FROM admin_log l LEFT JOIN users u ON u.id = l.actor ORDER BY l.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def purge_admin(self, now: float, reports_before: float, log_before: float, excerpts_before: float):
        self.db.execute("DELETE FROM reports WHERE created < ?", (reports_before,))
        self.db.execute("DELETE FROM admin_log WHERE ts < ?", (log_before,))
        # What a deleted message said is kept for a while (to check a
        # deletion was fair), not as long as the rest of the log.
        self.db.execute("UPDATE admin_log SET detail = '' WHERE action = 'delete_message' AND ts < ?",
                        (excerpts_before,))
        self.db.execute("DELETE FROM bans WHERE until IS NOT NULL AND until <= ?", (now,))
        self.db.execute("DELETE FROM network_bans WHERE until IS NOT NULL AND until <= ?", (now,))
        self.db.commit()

    # ---------------------------------------------------- app announcements

    def add_app_announcement(self, title: str, text: str, by: str, now: float) -> int:
        cur = self.db.execute("INSERT INTO app_announcements (ts, title, text, by) VALUES (?, ?, ?, ?)",
                              (now, title, text, by))
        self.db.commit()
        return cur.lastrowid

    def delete_app_announcement(self, announcement_id: int) -> bool:
        cur = self.db.execute("DELETE FROM app_announcements WHERE id = ?", (announcement_id,))
        self.db.commit()
        return cur.rowcount == 1

    def app_announcements(self, limit: int) -> list[dict]:
        rows = self.db.execute("SELECT id, ts, title, text FROM app_announcements ORDER BY id DESC LIMIT ?",
                               (limit,))
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- staff tags

    def change_user_id(self, old: str, new: str):
        """Moves everything of one user to a new ID (give-tag): their
        token, name, role, messages, rooms, buddies and the rest stay
        theirs. DM conversations are renamed, since their ID names the two
        people. One transaction: all of it happens, or none."""
        db = self.db
        if self.user(new) is not None:
            raise ValueError("That ID is taken.")
        with db:
            db.execute("UPDATE users SET id = ? WHERE id = ?", (new, old))
            db.execute("UPDATE rooms SET owner = ? WHERE owner = ?", (new, old))
            db.execute("UPDATE messages SET author = ? WHERE author = ?", (new, old))
            db.execute("UPDATE images SET author = ? WHERE author = ?", (new, old))
            for room_id in self.dm_room_ids_of(old):
                a, b = dm_people(room_id)
                renamed = dm_room_id(new, b if a == old else a)
                db.execute("UPDATE rooms SET id = ? WHERE id = ?", (renamed, room_id))
                db.execute("UPDATE messages SET room = ? WHERE room = ?", (renamed, room_id))
                db.execute("UPDATE images SET room = ? WHERE room = ?", (renamed, room_id))
                db.execute("UPDATE reports SET room = ? WHERE room = ?", (renamed, room_id))
            pairs = [(r["a"], r["b"], r["since"]) for r in db.execute(
                "SELECT a, b, since FROM buddies WHERE a = ? OR b = ?", (old, old))]
            db.execute("DELETE FROM buddies WHERE a = ? OR b = ?", (old, old))
            for a, b, since in pairs:
                other = b if a == old else a
                db.execute("INSERT OR IGNORE INTO buddies VALUES (?, ?, ?)", (*sorted((new, other)), since))
            for table, columns in (("buddy_requests", ("sender", "target")), ("blocks", ("blocker", "blocked")),
                                   ("bans", ("user_id", "by")), ("network_bans", ("user_id",)),
                                   ("devices", ("user_id",)),
                                   ("reports", ("reporter", "reported", "resolved_by")),
                                   ("admin_log", ("actor", "target")), ("app_announcements", ("by",))):
                for column in columns:
                    db.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old))
