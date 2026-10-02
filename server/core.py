"""Buddy Network's server logic, kept apart from the network.

NetworkCore takes already-received text frames from a Session and answers
through session.send(payload). The WebSocket side (net.py) is a thin
wrapper round it, and the tests drive it directly with fake sessions - no
network, no `websockets` package needed.

Protocol: one JSON object per frame, {"type": ...}. The first frame must be
a hello carrying PROTOCOL_VERSION and, after the first run, the client's
token. Errors come back as {"type": "error", "code": ..., "message": ...,
"re": <the request type>} (plus "nonce" for a failed send).

Direct messages are end-to-end encrypted (version 2 of the protocol): a DM
send carries `enc` instead of `text`, which this server checks the shape
of (clean_enc) and passes on, but can't read. Each Buddy registers its
PC's public key (set_device_key) and gets its buddies' keys in the buddy
list - see app/pages/buddy_network/e2e.py.

Images: a message can carry one image, which the sender's Buddy has already
shrunk (and, in a DM, encrypted - the server never sees a DM's picture).
It comes up first in image_part frames of IMAGE_PART_CHARS each, so no
frame is bigger than any other request, and the send that follows names
it. Everyone else fetches it with get_image when they show the message.
Images are kept IMAGE_DAYS; the message stays after, saying so.

GIFs: an image can be animated (an animated WebP - Buddy turns any GIF into
one). GIF search goes through this server to GIPHY (gifs.py), and the GIF
picked comes back to the sender's Buddy to be sent like any other image.

Reactions: old-school emoticons (REACTIONS - :D, <3 and the rest) on any
message, rooms and DMs alike. History carries each message's, and a react
request tells everyone looking what the message's reactions are now.

Bug reports: anyone's Buddy can send one, signed in or not - the only
requests besides hello taken before a hello (bugs.py). The owner reads
them in the Admin panel.

The client's IP address arrives on the session only for the per-IP limits
on new accounts and bug reports. It's held in memory and never stored,
logged or sent to anyone.
"""

from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import json
import logging
import re
import secrets
import time
import unicodedata

from .common import (STAFF_PREFIX, TAG_CHARS, RequestError, device_id, key_bytes, network_of,  # noqa: F401
                     new_user_id, public_message, public_room, public_user, tag_of)
from .admin import STAFF_ROLES, AdminMixin, one_line
from .bugs import PRE_HELLO, BugMixin
from .gifs import GifMixin
from .profiles import ProfileMixin
from .social import DEVICE_IDLE_DAYS, MAX_DEVICES, SocialMixin, dm_room
from .store import Store, dm_people

log = logging.getLogger("buddy_network")

# Keep in step with app/pages/buddy_network/client.py (tests check).
# 2: DMs are end-to-end encrypted - a Buddy that can't do that is told to update.
PROTOCOL_VERSION = 2
MAX_MESSAGE_CHARS = 2000

# Encrypted DMs (clean_enc). Keep in step with app/pages/buddy_network/e2e.py.
ENC_VERSION = 1
ENC_BODY_MAX = MAX_MESSAGE_CHARS * 4 + 16    # UTF-8 at its longest, plus the 16-byte tag
ENC_WRAP_BYTES = 48                          # a 32-byte message key plus its tag
_DEVICE_ID = re.compile(r"[0-9a-f]{16}")

# Images. Keep in step with app/pages/buddy_network/images.py (tests check).
IMAGE_DAYS = 7
MAX_IMAGE_BYTES = 400 * 1024       # what Buddy shrinks a picture to fit (a DM's plus its 16-byte tag)
MAX_IMAGE_SIDE = 4096
MAX_IMAGE_PARTS = 64               # Buddy sends a 400 KB picture in ~17 parts; more is a client holding memory
UPLOAD_SECONDS = 120.0             # from its first part to its last
IMAGE_PART_CHARS = 24000           # base64 per image_part frame: ~24 KB, under net.py's frame limit
IMAGE_STORE_MAX = 4 * 1024 ** 3    # every image on the server together; past it, new ones wait
_IMAGE_ID = re.compile(r"[0-9a-f]{32}")   # the sender's Buddy picks it (random)

HISTORY_DAYS = 30
HISTORY_PAGE = 50
NAME_MIN, NAME_MAX = 2, 24

ROOM_NAME_MIN, ROOM_NAME_MAX = 2, 32
TOPIC_MAX = 120
MAX_ROOMS_PER_USER = 3
ROOM_IDLE_DAYS = 30        # a user room with no messages this long is deleted
FIND_RESULTS = 50
GET_ROOMS_MAX = 50
WATCH_MAX = 250            # rooms and DMs one Buddy can ask unread counts for
UNREAD_CAP = 99
SLOW_CHOICES = (0, 10, 30, 60, 300)
MAX_SAVED_AVATARS = 6
# An avatar seed: random hex the client picks (avatars.py). Only ever drawn
# from, never shown - so nothing a person types.
_AVATAR_SEED = re.compile(r"[0-9a-f]{8,16}")
MAX_MENTIONS = 5           # people one message can notify
# Reactions: old-school emoticons from one fixed list, so nobody can type
# their own text into one. Keep in step with app/pages/buddy_network/reactions.py
# (tests check). In a DM they aren't encrypted: the server sees which one.
REACTIONS = ("smile", "grin", "heart", "wink", "tongue", "sad", "wow", "laugh", "cool", "happy", "cheer", "shrug")
REACTION_PEOPLE = 10       # names sent with each emoticon (its count covers everyone)
# "@Name#tag", as Buddy's name completion writes it: the name, then the tag
# from the ID (6 hex characters, or a staff number: #1).
MENTION = re.compile(r"@([^@#\n]{2,24}?)#([0-9a-f]{6}|[1-9][0-9]?)(?![0-9a-z])")

# (limit, window seconds)
SEND_LIMIT = (5, 10.0)
EDIT_LIMIT = (10, 60.0)
REACT_LIMIT = (30, 60.0)
NAME_CHANGE_LIMIT = (5, 86400.0)
AVATAR_LIMIT = (30, 3600.0)
ACCOUNTS_PER_IP_LIMIT = (3, 86400.0)
ROOM_CREATE_LIMIT = (5, 86400.0)   # so delete-and-recreate can't churn
FIND_LIMIT = (30, 60.0)
TOPIC_LIMIT = (10, 600.0)
IMAGE_LIMIT = (20, 3600.0)          # images one person can send
IMAGE_FETCH_LIMIT = (300, 60.0)

MAX_SESSIONS_PER_USER = 10   # Buddys signed in to one account at once
NAMELESS_DAYS = 30           # an identity that never chose a name (and hasn't been back) is forgotten
COMPACT_EVERY = 86400.0      # how often deleted text is wiped from the database file's free space
MAX_ID = 2 ** 63 - 1         # SQLite's largest integer

SYSTEM_ROOMS = [
    ("global", "Global", "Everyone's room. Be kind."),
    ("help", "Help", "Questions and answers about Buddy and DaVinci Resolve."),
]

# Names that would pass as Buddy itself or as staff, compared after folding
# case, lookalike digits/symbols and punctuation away ("Adm1n", "S.y.s.t.e.m").
# "1", "l", "|" and "!" all fold to "i", so "ADM1N" and "Officia1" match.
_LOOKALIKES = str.maketrans("0134579@$!|l", "oieastgasiii")
# Cyrillic and Greek letters that look like Latin ones (after casefold), so
# "Аdmin" with a Cyrillic А or "Glоbal" with a Cyrillic о fold the same way.
_CONFUSABLES = str.maketrans(
    "авеёкмнорстухіїјѕһԁӏԛԝɑɡıαβεηικνορτυχζμ",
    "abeekmhopctyxiijshdlqwagiabenikvoptuxzm")


def _skeleton(name: str) -> str:
    return "".join(ch for ch in name.casefold().translate(_CONFUSABLES).translate(_LOOKALIKES) if ch.isalnum())


_RESERVED_NAMES = {_skeleton(n) for n in ("admin", "administrator", "buddy", "buddynetwork", "system",
                                          "moderator", "mod", "owner", "staff", "support", "official",
                                          "server")}
_RESERVED_PARTS = tuple(_skeleton(n) for n in ("admin", "moderator", "official"))
_ROOM_PUNCTUATION = set(" ._-'&+")
_NAME_PUNCTUATION = set(" ._-'")
# Letters that draw nothing (Hangul fillers) and marks that change nothing
# you can see (variation selectors, the grapheme joiner): a name made of them
# looks blank, or exactly like someone else's.
_INVISIBLE = re.compile("[\u115f\u1160\u3164\uffa0\u034f\u17b4\u17b5\u180b-\u180f\ufe00-\ufe0f"
                        "\U000e0100-\U000e01ef]")
_MAX_MARKS = 2     # combining marks on one letter - more is "Zalgo" text
# Alphabets that share lookalike letters: one name uses only one of them.
_LOOKALIKE_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK")

# Bidirectional overrides can make "gpj.exe" read as "exe.jpg" - and a link
# look like a different one - so they never reach another user.
_BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")
_EXTRA_BLANK_LINES = re.compile(r"\n{4,}")


class Session:
    """One connected client. The transport subclasses this; tests fake it."""

    def __init__(self, ip: str = ""):
        self.ip = ip
        self.user_id: str | None = None
        self.rooms: set[str] = set()
        self.watching: set[str] = set()   # rooms in its sidebar: told of new messages (activity)
        self.close_requested = False
        self.upload: dict | None = None           # an image arriving in parts: {"id", "parts", "size"}
        self.uploaded: tuple | None = None        # (id, bytes) of the one that arrived, until it's sent
        self.gif_search: tuple | None = None      # (key, nonce) of the GIF search it's waiting for (gifs.py)
        self.gif_wanted: set[str] = set()         # the GIFs whose previews it still wants
        self.gif_fetched = collections.OrderedDict()   # GIPHY id -> its creator: GIFs it may send
        self.bug_started = False                  # it's sending a bug report (bugs.py) - maybe with no hello
        self.bug_upload: dict | None = None       # a screenshot arriving in parts, like upload
        self.bug_images: list = []                # (id, bytes) of the ones that arrived, until the report

    def send(self, payload: dict):
        raise NotImplementedError

    def close(self):
        """Ends the connection once what's been sent so far has gone."""
        self.close_requested = True


class RateLimiter:
    """Sliding window: at most `limit` events per `window` seconds per key.
    In memory only, so a restart resets it - fine for spam control."""

    def __init__(self, clock):
        self.clock = clock
        self._events: dict[object, collections.deque] = {}

    def check(self, key, limit: int, window: float) -> float:
        """0 and records the event if allowed; else seconds until it would be."""
        now = self.clock()
        events = self._events.setdefault(key, collections.deque())
        while events and events[0] <= now - window:
            events.popleft()
        if len(events) >= limit:
            return max(0.1, events[0] + window - now)
        events.append(now)
        return 0.0

    def prune(self, longest_window: float = 86400.0):
        """Forgets keys with nothing recent, so rotating keys (addresses,
        rooms) can't grow memory for ever."""
        cutoff = self.clock() - longest_window
        for key in [k for k, events in self._events.items() if not events or events[-1] <= cutoff]:
            del self._events[key]


def is_id(raw) -> bool:
    """A message/announcement id as it can be looked up: a whole number
    SQLite can hold (not True/False)."""
    return isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= MAX_ID


def hash_token(token: str) -> str:
    # The token is 256 random bits, so a plain hash is enough (no salt or
    # slow hash needed - there's nothing to guess).
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(raw) -> str:
    """The name as it will be stored, or RequestError("bad_name")."""
    if not isinstance(raw, str):
        raise RequestError("bad_name", "Choose a name.")
    name = " ".join(unicodedata.normalize("NFKC", raw).split())
    if not NAME_MIN <= len(name) <= NAME_MAX:
        raise RequestError("bad_name", f"Names are {NAME_MIN}-{NAME_MAX} characters.")
    _check_chars(name, _NAME_PUNCTUATION, "bad_name", "Names")
    skeleton = _skeleton(name)
    if skeleton in _RESERVED_NAMES or any(part in skeleton for part in _RESERVED_PARTS):
        raise RequestError("bad_name", "That name is reserved - choose another.")
    return name


def _check_chars(name: str, allowed_punctuation: set, code: str, label: str):
    marks, scripts = 0, set()
    for ch in name:
        category = unicodedata.category(ch)
        if (ch not in allowed_punctuation and category[0] not in "LNM") or _INVISIBLE.match(ch):
            shown = " ".join(sorted(allowed_punctuation - {" "}))
            raise RequestError(code, f"{label} can use letters, numbers, spaces and {shown} only.")
        marks = marks + 1 if category[0] == "M" else 0
        if marks > _MAX_MARKS:
            raise RequestError(code, f"{label} can't stack that many accents on one letter.")
        if category[0] == "L":
            script = unicodedata.name(ch, "").split(" ")[0]
            if script in _LOOKALIKE_SCRIPTS:
                scripts.add(script)
    if name[0] in allowed_punctuation or unicodedata.category(name[0])[0] == "M":
        raise RequestError(code, f"{label} must start with a letter or number.")
    if len(scripts) > 1:
        raise RequestError(code, f"{label} can't mix alphabets (Latin, Cyrillic, Greek) - use one.")


def clean_room_name(raw, taken=()) -> str:
    """A new room's name, or RequestError("bad_room"). `taken`: every
    existing room's name - a new one mustn't match any of them once case
    and lookalikes are folded away, so nobody can open a second "G1obal"."""
    if not isinstance(raw, str):
        raise RequestError("bad_room", "Give the room a name.")
    name = " ".join(unicodedata.normalize("NFKC", raw).split())
    if not ROOM_NAME_MIN <= len(name) <= ROOM_NAME_MAX:
        raise RequestError("bad_room", f"Room names are {ROOM_NAME_MIN}-{ROOM_NAME_MAX} characters.")
    _check_chars(name, _ROOM_PUNCTUATION, "bad_room", "Room names")
    skeleton = _skeleton(name)
    if skeleton in _RESERVED_NAMES or any(part in skeleton for part in _RESERVED_PARTS):
        raise RequestError("bad_room", "That name is reserved - choose another.")
    if skeleton in {_skeleton(t) for t in taken}:
        raise RequestError("bad_room", "There's already a room with that name (or one that looks "
                                       "just like it).")
    return name


def clean_topic(raw) -> str:
    """One line, at most TOPIC_MAX characters; may be empty."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise RequestError("bad_topic", "The topic is text.")
    text = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in raw)
    topic = " ".join(_BIDI_CONTROLS.sub("", text).split())
    if len(topic) > TOPIC_MAX:
        raise RequestError("bad_topic", f"Topics are at most {TOPIC_MAX} characters.")
    return topic


def clean_text(raw, limit: int = MAX_MESSAGE_CHARS, what: str = "message") -> str:
    """A message's text as it will be stored: control and bidi characters
    removed (newlines and tabs kept), long runs of blank lines shortened.
    limit and what: for other text kept the same way (a bug report)."""
    if not isinstance(raw, str):
        raise RequestError("bad_message", f"{what.capitalize()}s are text.")
    text = "".join(ch for ch in raw if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    text = _BIDI_CONTROLS.sub("", text).replace("\r", "")
    text = _EXTRA_BLANK_LINES.sub("\n\n\n", text).strip()
    if not text:
        raise RequestError("bad_message", f"The {what} is empty.")
    if len(text) > limit:
        raise RequestError("too_long", f"{what.capitalize()}s are at most {limit:,} characters.")
    return text


def _text_or_image(raw, has_image: bool) -> str:
    """A room message's text; with an image it may be empty."""
    if has_image and (raw is None or (isinstance(raw, str) and not raw.strip())):
        return ""
    return clean_text(raw)


def _b64_length(raw) -> int:
    """The decoded length of a base64 string; -1 if it isn't one."""
    if not isinstance(raw, str) or len(raw) > 2 * ENC_BODY_MAX:
        return -1
    try:
        return len(base64.b64decode(raw, validate=True))
    except (binascii.Error, ValueError):
        return -1


def clean_enc(raw, sender_devices, may_be_empty=False) -> str:
    """An encrypted DM as it will be stored (JSON): checked for shape and
    size only - the server can't read it. sender_devices: the ids of the
    sender's registered PCs; the message must come from one of them.
    may_be_empty: it has an image, so its text can be nothing (the 16-byte
    tag alone)."""
    bad = RequestError("bad_message", "That encrypted message isn't in a form this server knows - "
                                      "update Buddy.")
    if not isinstance(raw, dict) or set(raw) != {"v", "from", "salt", "body", "keys"} or raw["v"] != ENC_VERSION:
        raise bad
    shortest = 16 if may_be_empty else 17
    if not shortest <= _b64_length(raw["body"]) <= ENC_BODY_MAX:
        raise bad
    return json.dumps({**_clean_wrapping(raw, sender_devices, bad), "body": raw["body"]}, separators=(",", ":"))


def _clean_wrapping(raw: dict, sender_devices, bad: RequestError) -> dict:
    """The part a DM and a DM's image share: the sending PC, the salt and
    the key wrapped for each PC that can read it."""
    if raw["from"] not in sender_devices:
        raise RequestError("no_device", "This PC's encryption key isn't registered yet - try again in a moment.")
    keys = raw["keys"]
    if _b64_length(raw["salt"]) != 16 or not isinstance(keys, dict) or not 1 <= len(keys) <= 2 * MAX_DEVICES:
        raise bad
    for device, wrapped in keys.items():
        if not isinstance(device, str) or not _DEVICE_ID.fullmatch(device) or _b64_length(wrapped) != ENC_WRAP_BYTES:
            raise bad
    return {"v": ENC_VERSION, "from": raw["from"], "salt": raw["salt"], "keys": keys}


def clean_image_enc(raw, sender_devices) -> str:
    """A DM image's wrapped keys (its bytes are encrypted with them), as
    they will be stored (JSON). The same checks as clean_enc, minus a body."""
    bad = RequestError("bad_image", "That encrypted image isn't in a form this server knows - update Buddy.")
    if not isinstance(raw, dict) or set(raw) != {"v", "from", "salt", "keys"} or raw["v"] != ENC_VERSION:
        raise bad
    return json.dumps(_clean_wrapping(raw, sender_devices, bad), separators=(",", ":"))


def is_image_file(data: bytes) -> bool:
    """A room's image starts like a WebP, JPEG or PNG. (Buddy decodes and
    checks every image it's sent before showing it - this only keeps other
    kinds of file off the server.)"""
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


ANNOUNCEMENT_MAX = 300
# App announcements (the orb in every Buddy): a few, short, plain text.
APP_ANNOUNCEMENTS_SHOWN = 10
APP_TITLE_MAX, APP_TEXT_MAX = 80, 1000


class NetworkCore(SocialMixin, AdminMixin, GifMixin, BugMixin, ProfileMixin):
    def __init__(self, store: Store, clock=time.time, limit_new_accounts=True, giphy=None, fetch=None):
        """limit_new_accounts=False (python -m server --dev) is for testing on
        one PC, where every test identity comes from the same address.
        giphy: a gifs.GiphySettings for GIF search (None: none offered);
        fetch(url, max_bytes, done): downloads for it - net.py sets it."""
        self.store = store
        self.giphy, self.fetch = giphy, fetch
        self._init_gifs()
        self._init_profiles()
        self.clock = clock
        self.limit_new_accounts = limit_new_accounts
        self.limits = RateLimiter(clock)
        self._subscribers: dict[str, set[Session]] = collections.defaultdict(set)
        self._watchers: dict[str, set[Session]] = collections.defaultdict(set)
        self._sessions: set[Session] = set()     # every signed-in session
        self._by_user: dict[str, set[Session]] = collections.defaultdict(set)
        now = clock()
        self._compacted = now
        for room_id, name, topic in SYSTEM_ROOMS:
            store.ensure_room(room_id, name, topic, "system", now)

    # ---------------------------------------------------- connection hooks

    def disconnect(self, session: Session):
        for room in session.rooms:
            self._subscribers[room].discard(session)
        self._who_changed(session.rooms)
        session.rooms.clear()
        for room in session.watching:
            self._watchers[room].discard(session)
        session.watching.clear()
        self._sessions.discard(session)
        user_id = session.user_id
        if user_id and session in self._by_user.get(user_id, ()):
            self._by_user[user_id].discard(session)
            if not self._by_user[user_id]:
                del self._by_user[user_id]
                self._presence_changed(user_id)   # their last Buddy went offline

    def purge(self) -> int:
        """Deletes messages past HISTORY_DAYS and user rooms idle for
        ROOM_IDLE_DAYS; returns how many messages went."""
        now = self.clock()
        removed = self.store.purge_before(now - HISTORY_DAYS * 86400)
        self.store.purge_images(now - IMAGE_DAYS * 86400)
        for room_id in self.store.idle_user_rooms(now - ROOM_IDLE_DAYS * 86400):
            self._remove_room(room_id, "expired")
        for user_id in self.store.purge_devices(now - DEVICE_IDLE_DAYS * 86400):
            self._keys_changed(user_id)
        self.store.purge_nameless(now - NAMELESS_DAYS * 86400, set(self._by_user))
        self.purge_admin()
        self.purge_bugs()
        self._prune_profile_views()
        self.limits.prune()
        if now - self._compacted >= COMPACT_EVERY:
            # Deleted and edited text otherwise lingers in the file's free
            # pages (secure_delete wipes new deletions; this catches the rest).
            self._compacted = now
            self.store.compact()
        return removed

    def _broadcast(self, payload: dict, sessions=None):
        for session in list(self._sessions if sessions is None else sessions):
            session.send(payload)

    def _here(self, room_id: str) -> int:
        """How many people are in the room - not counting anyone set to
        appear offline, or a small room would give them away."""
        ids = {s.user_id for s in self._subscribers.get(room_id, ()) if s.user_id}
        return sum(1 for u in self.store.users(ids) if not u["appear_offline"])

    def _remove_room(self, room_id: str, reason: str):
        self.store.delete_room(room_id)
        for session in list(self._subscribers.pop(room_id, ())):
            session.rooms.discard(room_id)
        for session in list(self._watchers.pop(room_id, ())):
            session.watching.discard(room_id)
        self._broadcast({"type": "room_removed", "room": room_id, "reason": reason})

    # ------------------------------------------------------------ dispatch

    def handle(self, session: Session, raw: str):
        try:
            msg = json.loads(raw)
            # A lone surrogate (an escaped \ud800) parses, but can't be stored or sent on.
            json.dumps(msg, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            session.send(_error("bad_request", "That wasn't valid JSON.", None))
            return
        if not isinstance(msg, dict):
            session.send(_error("bad_request", "Expected a JSON object.", None))
            return
        kind = msg.get("type")
        handler = self._HANDLERS.get(kind) if isinstance(kind, str) else None
        try:
            if handler is None:
                raise RequestError("bad_request", "Unknown request type.")
            if kind not in PRE_HELLO and session.user_id is None:
                raise RequestError("not_authenticated", "Say hello first.")
            try:
                handler(self, session, msg)
            except RequestError:
                raise
            except Exception as exc:   # a request shaped in some way nothing checked for
                log.warning("%s request failed: %s", kind, type(exc).__name__)
                raise RequestError("bad_request", "The server couldn't handle that request.") from None
        except RequestError as exc:
            payload = _error(exc.code, exc.message, kind if isinstance(kind, str) else None, **exc.extra)
            if isinstance(msg.get("nonce"), (int, str)):
                payload["nonce"] = msg["nonce"]
            session.send(payload)

    # ------------------------------------------------------------ requests

    def _hello(self, session: Session, msg: dict):
        if session.user_id is not None:
            raise RequestError("bad_request", "Already signed in.")
        if msg.get("v") != PROTOCOL_VERSION:
            session.close_requested = True
            raise RequestError("update_required", "Update Buddy to use Buddy Network.")
        token = msg.get("token")
        new_token = None
        now = self.clock()
        if token:
            user = self.store.user_by_token_hash(hash_token(str(token)))
            if user is None:
                session.close_requested = True
                raise RequestError("bad_token", "This server doesn't know the identity saved on this PC.")
            ban = self.store.ban_of(user["id"], now)
            if ban:
                session.close_requested = True
                raise RequestError("banned", "You've been banned from Buddy Network.",
                                   until=ban["until"], reason=ban["reason"])
        else:
            network_ban = session.ip and self.store.network_ban(self.store.ip_hash(network_of(session.ip)), now)
            if network_ban:
                session.close_requested = True
                raise RequestError("banned", "New identities can't be made from this network right now.",
                                   until=network_ban["until"], reason="")
            wait = self.limit_new_accounts and self.limits.check(("accounts", network_of(session.ip)),
                                                                 *ACCOUNTS_PER_IP_LIMIT)
            if wait:
                session.close_requested = True
                raise RequestError("rate_limited", "Too many new identities from this network today - "
                                   "try again tomorrow.", retry_after=round(wait))
            new_token = secrets.token_urlsafe(32)
            user = self.store.create_user(new_user_id(secrets.token_hex), hash_token(new_token), self.clock())
        if len(self._by_user.get(user["id"], ())) >= MAX_SESSIONS_PER_USER:
            session.close_requested = True
            raise RequestError("rate_limited", f"This identity is already signed in on {MAX_SESSIONS_PER_USER} "
                                               "Buddys - close one first.")
        session.user_id = user["id"]
        self._sessions.add(session)
        first = not self._by_user.get(user["id"])
        self._by_user[user["id"]].add(session)
        welcome = {
            "type": "welcome",
            "user": public_user(user),
            "saved_avatars": self.store.saved_avatars(user["id"]),
            "rooms": [public_room(r) for r in self.store.system_rooms()],
            "my_rooms": [public_room(r) for r in self.store.rooms_owned_by(user["id"])],
            "limits": {"max_message_chars": MAX_MESSAGE_CHARS, "history_days": HISTORY_DAYS,
                       "max_rooms": MAX_ROOMS_PER_USER, "room_idle_days": ROOM_IDLE_DAYS,
                       "max_devices": MAX_DEVICES,
                       # A Buddy offers images only where the server says it takes them.
                       "max_image_bytes": MAX_IMAGE_BYTES, "max_image_side": MAX_IMAGE_SIDE,
                       "image_part_chars": IMAGE_PART_CHARS, "image_days": IMAGE_DAYS,
                       "gifs": self.gifs_offered(),
                       # Profile pages and who's-here lists (profiles.py): a Buddy offers
                       # them only where the server says it has them.
                       "profiles": True, "who": True},
        }
        if new_token:
            welcome["token"] = new_token
        session.send(welcome)
        session.send(self.buddy_list_for(user["id"]))
        if first:
            self._presence_changed(user["id"])
        if user["role"] in STAFF_ROLES:
            self._tell_admins_about_reports([session])
        if user["role"] == "owner":
            self._tell_owner_about_bugs([session])

    def _set_name(self, session: Session, msg: dict):
        name = clean_name(msg.get("name"))
        user = self.store.user(session.user_id)
        if user["name"] == name:
            session.send({"type": "name_set", "user": public_user(user)})
            return
        wait = self.limits.check(("name", session.user_id), *NAME_CHANGE_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of name changes - try again tomorrow.",
                               retry_after=round(wait))
        self.store.set_name(session.user_id, name)
        session.send({"type": "name_set", "user": public_user(self.store.user(session.user_id))})
        self._push_buddy_lists(self._people_who_see(session.user_id))
        self._who_changed_for(session.user_id)

    def _avatar_payload(self, user_id: str) -> dict:
        return {"type": "avatar_set", "user": public_user(self.store.user(user_id)),
                "saved": self.store.saved_avatars(user_id)}

    def _set_avatar(self, session: Session, msg: dict):
        """Changes the seed everyone draws this person's avatar from; "" goes
        back to the one from their ID."""
        seed = msg.get("avatar")
        if not isinstance(seed, str) or (seed and not _AVATAR_SEED.fullmatch(seed)):
            raise RequestError("bad_request", "That isn't an avatar.")
        wait = self.limits.check(("avatar", session.user_id), *AVATAR_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of avatar changes - try again later.",
                               retry_after=round(wait))
        self.store.set_avatar(session.user_id, seed)
        for s in self._sessions_of(session.user_id):   # all of their Buddys
            s.send(self._avatar_payload(session.user_id))
        self._push_buddy_lists(self._people_who_see(session.user_id))
        self._who_changed_for(session.user_id)

    def _save_avatars(self, session: Session, msg: dict):
        """The avatars they liked, kept with the account so every PC has them."""
        saved = msg.get("saved")
        if not isinstance(saved, list) or not all(isinstance(s, str) and _AVATAR_SEED.fullmatch(s) for s in saved):
            raise RequestError("bad_request", "'saved' is a list of avatars.")
        saved = list(dict.fromkeys(saved))
        if len(saved) > MAX_SAVED_AVATARS:
            raise RequestError("too_many", f"You can save {MAX_SAVED_AVATARS} avatars - remove one first.")
        self.store.set_saved_avatars(session.user_id, saved)
        for s in self._sessions_of(session.user_id):
            s.send(self._avatar_payload(session.user_id))

    def _room_or_error(self, room_id, session: Session | None = None) -> dict:
        """A DM only exists for its two people: anyone else gets no_room,
        the same as for a room that doesn't exist."""
        room = self.store.room(room_id) if isinstance(room_id, str) else None
        if room is not None and room["kind"] == "dm":
            other = self._dm_other(session, room_id) if session is not None else None
            if other is None:
                room = None
            else:
                room = dm_room(room_id, self.store.user(other) or {"id": other, "name": None})
                room["owner_id"] = None
        if room is None:
            raise RequestError("no_room", "That room doesn't exist.")
        return room

    def _audience(self, room: dict) -> set:
        if room["kind"] == "dm":
            return set(self._dm_audience(room["id"]))
        return set(self._subscribers[room["id"]])

    def _join(self, session: Session, msg: dict):
        room = self._room_or_error(msg.get("room"), session)
        new = room["id"] not in session.rooms
        session.rooms.add(room["id"])
        self._subscribers[room["id"]].add(session)
        self._send_history(session, room["id"], None)
        self._send_who(session, room["id"])
        if new:
            self._who_changed([room["id"]])

    def _leave(self, session: Session, msg: dict):
        room_id = msg.get("room")
        if room_id in session.rooms:
            self._who_changed([room_id])
        session.rooms.discard(room_id)
        self._subscribers.get(room_id, set()).discard(session)

    def _history(self, session: Session, msg: dict):
        room = self._room_or_error(msg.get("room"), session)
        before = msg.get("before")
        if before is not None and not is_id(before):
            raise RequestError("bad_request", "'before' is a message id.")
        self._send_history(session, room["id"], before, msg.get("nonce"))

    def _send_history(self, session: Session, room_id: str, before, nonce=None):
        rows, more = self.store.history(room_id, before, HISTORY_PAGE)
        messages = [public_message(r) for r in rows]
        reactions = self.store.reactions(m["id"] for m in messages)
        for m in messages:
            if m["id"] in reactions:
                m["reactions"] = shown_reactions(reactions[m["id"]], session.user_id)
        payload = {"type": "history", "room": room_id, "before": before, "messages": messages, "more": more}
        if isinstance(nonce, (int, str)):
            payload["nonce"] = nonce   # an export collecting pages, not the chat view
        session.send(payload)

    def _send(self, session: Session, msg: dict):
        room = self._room_or_error(msg.get("room"), session)
        if room["id"] not in session.rooms:
            raise RequestError("not_joined", "Join the room first.")
        if not self.store.user(session.user_id)["name"]:
            raise RequestError("no_name", "Choose a name before sending messages.")
        enc = None
        devices = self.store.device_keys([session.user_id])[session.user_id]
        image = self._sent_image(session, room, msg.get("image"), devices)
        if room["kind"] == "dm":
            self._check_can_dm(session.user_id, room["other"]["id"])
            if msg.get("text") or msg.get("enc") is None:
                raise RequestError("update_required", "Update Buddy to send direct messages - they're "
                                                      "end-to-end encrypted now.")
            text = ""
            enc = clean_enc(msg.get("enc"), devices, may_be_empty=image is not None)
        elif msg.get("enc") is not None:
            raise RequestError("bad_message", "Only direct messages are encrypted.")
        else:
            text = _text_or_image(msg.get("text"), image is not None)
        reply_to = self._reply_target(room, msg.get("reply_to"))
        wait = self.limits.check(("send", session.user_id), *SEND_LIMIT)
        if wait:
            raise RequestError("rate_limited", "You're sending messages too quickly - slow down a little.",
                               retry_after=round(wait, 1))
        slow = room.get("slow") or 0
        if slow and not self._is_staff(session.user_id):
            wait = self.limits.check(("slow", room["id"], session.user_id), 1, float(slow))
            if wait:
                raise RequestError("rate_limited", f"Slow mode is on here - you can post again in {wait:.0f}s.",
                                   retry_after=round(wait))
        message = public_message(self.store.add_message(room["id"], session.user_id, text, self.clock(), enc,
                                                        reply_to, image))
        if image is not None:
            session.uploaded = None
        audience = self._audience(room)
        for other in audience:
            payload = {"type": "message", "message": message}
            if other is session and "nonce" in msg:
                payload["nonce"] = msg["nonce"]
            other.send(payload)
        if room["kind"] != "dm":
            for other in self._watchers.get(room["id"], set()) - audience:
                other.send({"type": "activity", "room": room["id"], "id": message["id"]})
            self._notify_mentions(session, room, message)

    # ------------------------------------------------------------ images

    def _image_part(self, session: Session, msg: dict):
        """One piece of an image on its way up (see the note at the top).
        seq 0 starts it; the one marked last completes it, and the send
        naming its id follows. One at a time per connection."""
        image_id, seq, data = msg.get("id"), msg.get("seq"), msg.get("data")
        if (not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id) or not is_id(seq)
                or not isinstance(data, str) or len(data) > IMAGE_PART_CHARS):
            session.upload = None
            raise RequestError("bad_image", "That image didn't arrive in one piece - try again.")
        if seq == 0:
            wait = self.limits.check(("image", session.user_id), *IMAGE_LIMIT)
            if wait:
                session.upload = None
                raise RequestError("rate_limited", "That's a lot of images - try again later.",
                                   retry_after=round(wait))
            session.upload = {"id": image_id, "parts": [], "size": 0, "t": self.clock()}
        upload = session.upload
        if (upload is None or upload["id"] != image_id or seq != len(upload["parts"])
                or seq >= MAX_IMAGE_PARTS or self.clock() - upload["t"] > UPLOAD_SECONDS):
            session.upload = None
            raise RequestError("bad_image", "That image didn't arrive in one piece - try again.")
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            session.upload = None
            raise RequestError("bad_image", "That image didn't arrive in one piece - try again.") from None
        if not chunk:                      # nothing to add: only a way to keep an upload (and its memory) open
            session.upload = None
            raise RequestError("bad_image", "That image didn't arrive in one piece - try again.")
        upload["size"] += len(chunk)
        if upload["size"] > MAX_IMAGE_BYTES:
            session.upload = None
            raise RequestError("too_big", f"Images are at most {MAX_IMAGE_BYTES // 1024} KB - update Buddy "
                                          "so it shrinks them to fit.")
        upload["parts"].append(chunk)
        if msg.get("last"):
            session.uploaded = (image_id, b"".join(upload["parts"]))
            session.upload = None

    def _sent_image(self, session: Session, room: dict, raw, devices) -> dict | None:
        """The image a send names: the one this connection just uploaded,
        checked - its size, and for a room that it's an image file; a DM's
        is encrypted, so its wrapped keys instead."""
        if raw is None:
            return None
        bad = RequestError("bad_image", "That image didn't arrive in one piece - try again.")
        if not isinstance(raw, dict) or not session.uploaded or raw.get("id") != session.uploaded[0]:
            raise bad
        w, h = raw.get("w"), raw.get("h")
        if not all(is_id(v) and 1 <= v <= MAX_IMAGE_SIDE for v in (w, h)):
            raise bad
        image_id, data = session.uploaded
        # "gif": a GIF from the picker, so the message says it's from GIPHY.
        image = {"id": image_id, "w": w, "h": h, "enc": None, "data": data,
                 "credit": self.gif_credit(session, raw.get("gif"))}
        fields = set(raw) - {"gif"}
        if room["kind"] == "dm":
            if fields != {"id", "w", "h", "enc"}:
                raise RequestError("update_required", "Update Buddy to send images in direct messages.")
            image["enc"] = clean_image_enc(raw["enc"], devices)
            if len(data) <= 16:
                raise bad
        elif fields != {"id", "w", "h"} or not is_image_file(data):
            raise RequestError("bad_image", "Only pictures can be sent (WebP, JPEG or PNG).")
        if self.store.image_taken(image_id):
            raise bad
        if self.store.images_size() + len(data) > IMAGE_STORE_MAX:
            log.warning("image storage is full")
            raise RequestError("server_full", "The server has no room for more images right now - try "
                                              "again later.")
        return image

    def _get_image(self, session: Session, msg: dict):
        """An image's bytes, for a message this person can see: any room's,
        or one of their own DMs'. Asked for one at a time as they're shown."""
        image_id = msg.get("id")
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise RequestError("bad_request", "'id' is an image id.")
        wait = self.limits.check(("get_image", session.user_id), *IMAGE_FETCH_LIMIT)
        if wait:
            raise RequestError("rate_limited", "Loading images too quickly - wait a moment.",
                               retry_after=round(wait, 1), id=image_id)
        row = self.store.image(image_id)
        if row is not None:
            try:
                self._room_or_error(row["room"], session)
            except RequestError:
                row = None   # someone else's DM: the same answer as no image at all
        else:
            row = self.bug_image_for(session, image_id)   # a bug report's screenshot (bugs.py)
        if row is None:
            raise RequestError("no_image", f"That image has expired - images are kept for {IMAGE_DAYS} days.",
                               id=image_id)
        session.send({"type": "image", "id": row["id"], "data": base64.b64encode(row["data"]).decode("ascii")})

    def _reply_target(self, room: dict, raw) -> int | None:
        if raw is None:
            return None
        row = self.store.message(raw) if is_id(raw) else None
        if row is None or row["room"] != room["id"] or row["deleted"]:
            raise RequestError("no_message", "The message you're replying to isn't there any more.")
        return row["id"]

    def _notify_mentions(self, session: Session, room: dict, message: dict):
        """Tells the people "@Name#tag"-mentioned in a room message, wherever
        they are: the name has to be theirs, and nobody who has blocked the
        sender hears about it."""
        told = set()
        for name, tag in MENTION.findall(message["text"])[:MAX_MENTIONS * 2]:
            prefix = f"{STAFF_PREFIX}{int(tag):02d}" if tag.isdigit() and len(tag) <= 2 else tag
            for user in self.store.users_by_id_prefix(prefix):
                if (user["id"] in told or user["id"] == session.user_id or tag_of(user["id"]) != tag
                        or (user["name"] or "").casefold() != name.strip().casefold()
                        or self.store.is_blocked(user["id"], session.user_id)):
                    continue
                told.add(user["id"])
                for s in self._sessions_of(user["id"]):
                    s.send({"type": "mentioned", "room": room["id"], "room_name": room["name"],
                            "message": message})
            if len(told) >= MAX_MENTIONS:
                break

    def _edit(self, session: Session, msg: dict):
        """The author changes their own message. A DM's new text comes
        encrypted, like a send."""
        row, room = self._visible_message(session, msg.get("id"))
        if row["deleted"]:
            raise RequestError("no_message", "That message isn't there any more.")
        if row["author_id"] != session.user_id:
            raise RequestError("not_allowed", "You can only edit your own messages.")
        has_image = bool(row.get("image"))   # its text can be emptied: the image stays
        if room["kind"] == "dm":
            self._check_can_dm(session.user_id, room["other"]["id"])
            if msg.get("text") or msg.get("enc") is None:
                raise RequestError("update_required", "Update Buddy to edit direct messages.")
            text = ""
            enc = clean_enc(msg.get("enc"), self.store.device_keys([session.user_id])[session.user_id],
                            may_be_empty=has_image)
        elif msg.get("enc") is not None:
            raise RequestError("bad_message", "Only direct messages are encrypted.")
        else:
            text, enc = _text_or_image(msg.get("text"), has_image), None
        wait = self.limits.check(("edit", session.user_id), *EDIT_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of edits - wait a moment.", retry_after=round(wait, 1))
        self.store.edit_message(row["id"], text, enc, self.clock())
        edited = public_message(self.store.message(row["id"]))
        for other in self._audience(room):
            other.send({"type": "edited", "message": edited})

    def _react(self, session: Session, msg: dict):
        """Adds (on, the default) or takes away one of REACTIONS on a message
        this person can see - in a room they've joined, or a DM of theirs.
        Everyone looking at it is told its reactions as they are now, each
        with whether they're theirs."""
        row, room = self._visible_message(session, msg.get("id"))
        if row["deleted"]:
            raise RequestError("no_message", "That message isn't there any more.")
        reaction = msg.get("reaction")
        if reaction not in REACTIONS:
            raise RequestError("bad_request", "That isn't one of Buddy Network's reactions - update Buddy.")
        if not self.store.user(session.user_id)["name"]:
            raise RequestError("no_name", "Choose a name before reacting.")
        if room["kind"] == "dm":
            self._check_can_dm(session.user_id, room["other"]["id"])
        elif room["id"] not in session.rooms:
            raise RequestError("not_joined", "Join the room first.")
        wait = self.limits.check(("react", session.user_id), *REACT_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of reactions - wait a moment.",
                               retry_after=round(wait, 1))
        changed = self.store.react(row["id"], session.user_id, reaction, bool(msg.get("on", True)), self.clock())
        now = self.store.reactions([row["id"]]).get(row["id"], [])
        for other in (self._audience(room) | {session}) if changed else {session}:
            other.send({"type": "reactions", "room": row["room"], "id": row["id"],
                        "reactions": shown_reactions(now, other.user_id)})

    def _watch(self, session: Session, msg: dict):
        """The rooms and DMs in this Buddy's sidebar, each with the last
        message it has seen: answers how many it has missed, and from now
        on says when a watched room gets a new one (activity)."""
        rooms = msg.get("rooms")
        if not isinstance(rooms, dict) or len(rooms) > WATCH_MAX or not all(
                isinstance(k, str) and is_id(v) for k, v in rooms.items()):
            raise RequestError("bad_request", f"'rooms' maps up to {WATCH_MAX} room ids to message ids.")
        allowed = {}
        for room_id, last in rooms.items():
            people = dm_people(room_id)
            if people is not None:
                if session.user_id in people:
                    allowed[room_id] = last       # counts only: DMs reach both people anyway
            elif self.store.room(room_id) is not None:
                allowed[room_id] = last
        for room_id in session.watching:
            self._watchers[room_id].discard(session)
        session.watching = {r for r in allowed if dm_people(r) is None}
        for room_id in session.watching:
            self._watchers[room_id].add(session)
        session.send({"type": "unread", "rooms": self.store.unread_counts(allowed, UNREAD_CAP)})

    def _set_slow(self, session: Session, msg: dict):
        room = self._own_room_or_error(session, msg.get("room"), system_too=True)
        self._require_staff(session)
        seconds = msg.get("seconds")
        if seconds not in SLOW_CHOICES:
            raise RequestError("bad_request", "Slow mode is off, 10s, 30s, 1 minute or 5 minutes.")
        self.store.set_slow(room["id"], seconds)
        self._log(session, "slow_mode", room["id"], f"{room['name']}: {seconds}s")
        self._room_updated(room["id"])

    def _visible_message(self, session: Session, message_id) -> tuple[dict, dict]:
        """(message, room) for a message this session can see. One in a DM
        they aren't part of gets the same answer as one that doesn't exist,
        so nobody can probe which messages are there."""
        row = self.store.message(message_id) if is_id(message_id) else None
        try:
            room = self._room_or_error(row["room"], session) if row is not None else None
        except RequestError:
            room = None
        if room is None:
            raise RequestError("no_message", "That message isn't there any more.")
        return row, room

    def _delete(self, session: Session, msg: dict):
        row, _room = self._visible_message(session, msg.get("id"))
        mine = row["author_id"] == session.user_id
        if not mine and not self._is_staff(session.user_id):
            raise RequestError("not_allowed", "You can only delete your own messages.")
        if row["deleted"]:
            # Already gone: tell only whoever asked, so repeats can't flood the room.
            session.send({"type": "deleted", "room": row["room"], "id": row["id"]})
            return
        if not mine:
            self._log(session, "delete_message", row["author_id"] or "", row["text"][:120])
        self._mark_deleted(row)

    def _purge_message(self, session: Session, msg: dict):
        """The owner deletes a message forever: the row itself goes, not
        just its text, so nothing - not even "message deleted" - is left
        where it was. Replies to it lose their quote, and reports of it
        (which hold a copy of its text) go too. Every Buddy is told
        "deleted" first - an older one then at least hides it - and then
        "purged", which a current one removes it on."""
        self._require_owner(session)
        row, _room = self._visible_message(session, msg.get("id"))
        self.store.purge_message(row["id"])
        self._log(session, "purge_message", row["author_id"] or "")
        audience = (self._dm_audience(row["room"]) if dm_people(row["room"])
                    else self._subscribers[row["room"]])
        for other in set(audience):
            other.send({"type": "deleted", "room": row["room"], "id": row["id"]})
            other.send({"type": "purged", "room": row["room"], "id": row["id"]})
        self._tell_admins_about_reports()

    def _mark_deleted(self, row: dict):
        if not row["deleted"]:
            self.store.mark_deleted(row["id"])
        audience = (self._dm_audience(row["room"]) if dm_people(row["room"])
                    else self._subscribers[row["room"]])
        for other in set(audience):
            other.send({"type": "deleted", "room": row["room"], "id": row["id"]})

    # ------------------------------------------------------------- rooms

    def _own_room_or_error(self, session: Session, room_id, system_too=False) -> dict:
        """A room its maker - or an admin - may change. System rooms
        (Global, Help) only with system_too, and only by an admin."""
        room = self._room_or_error(room_id, session)
        admin = self._is_staff(session.user_id)
        allowed = ((room["kind"] == "user" and (room["owner_id"] == session.user_id or admin))
                   or (room["kind"] == "system" and system_too and admin))
        if not allowed:
            raise RequestError("not_allowed", "Only the person who made this room (or an admin) can "
                                              "change it.")
        return room

    def _log_if_not_theirs(self, session: Session, room: dict, action: str, detail: str = ""):
        if room.get("owner_id") != session.user_id:
            self._log(session, action, room["id"], f"{room['name']}: {detail}" if detail else room["name"])

    def _create_room(self, session: Session, msg: dict):
        if not self.store.user(session.user_id)["name"]:
            raise RequestError("no_name", "Choose a name before making a room.")
        name = clean_room_name(msg.get("name"), self.store.room_names())
        topic = clean_topic(msg.get("topic"))
        if len(self.store.rooms_owned_by(session.user_id)) >= MAX_ROOMS_PER_USER:
            raise RequestError("too_many_rooms", f"You can have {MAX_ROOMS_PER_USER} rooms at a time - "
                                                 "delete one to make another.")
        wait = self.limits.check(("create_room", session.user_id), *ROOM_CREATE_LIMIT)
        if wait:
            raise RequestError("rate_limited", "You've made a lot of rooms today - try again tomorrow.",
                               retry_after=round(wait))
        room = self.store.create_room("r" + secrets.token_hex(6), name, topic, session.user_id, self.clock())
        mine = [s for s in self._sessions if s.user_id == session.user_id]
        self._broadcast({"type": "room_created", "room": public_room(room)}, mine)

    def _delete_room(self, session: Session, msg: dict):
        room = self._own_room_or_error(session, msg.get("room"))
        self._log_if_not_theirs(session, room, "delete_room")
        self._remove_room(room["id"], "deleted")

    def _room_updated(self, room_id: str):
        self._broadcast({"type": "room_updated", "room": public_room(self.store.room(room_id))})

    def _set_topic(self, session: Session, msg: dict):
        room = self._own_room_or_error(session, msg.get("room"), system_too=True)
        topic = clean_topic(msg.get("topic"))
        if topic == room["topic"]:
            session.send({"type": "room_updated", "room": public_room(room)})
            return
        wait = self.limits.check(("topic", session.user_id), *TOPIC_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of topic changes - try again later.",
                               retry_after=round(wait))
        self.store.set_topic(room["id"], topic)
        self._log_if_not_theirs(session, room, "set_topic", topic)
        self._room_updated(room["id"])

    def _set_permanent(self, session: Session, msg: dict):
        """Exempts (or not) a user room from the ROOM_IDLE_DAYS purge - the
        maker's or an admin's call, same as deleting it or changing its
        topic (_own_room_or_error already limits this to 'user' rooms, since
        system_too isn't passed). No extra staff gate, since
        MAX_ROOMS_PER_USER already caps how many any one person can keep
        alive forever."""
        room = self._own_room_or_error(session, msg.get("room"))
        value = bool(msg.get("value"))
        if value == bool(room["permanent"]):
            session.send({"type": "room_updated", "room": public_room(room)})
            return
        self.store.set_permanent(room["id"], value)
        self._log_if_not_theirs(session, room, "set_permanent", "on" if value else "off")
        self._room_updated(room["id"])

    def _set_public(self, session: Session, msg: dict):
        """The owner makes a user room everyone's (value true): it's listed
        in every Buddy with Global and Help, never expires, and staff look
        after it rather than its maker. value false makes it a regular room
        again - its maker's, or the owner's if the maker has gone. Global and
        Help themselves are always everyone's."""
        self._require_owner(session)
        room = self._room_or_error(msg.get("room"), session)
        if room["kind"] == "dm":
            raise RequestError("not_allowed", "Only rooms can be made public.")
        if room["kind"] == "system" and not room["made_public"]:
            raise RequestError("not_allowed", f"#{room['name']} is always everyone's.")
        value = bool(msg.get("value"))
        if value == (room["kind"] == "system"):
            session.send({"type": "room_updated", "room": public_room(room)})
            return
        if value:
            self.store.make_public(room["id"], self.clock())
        else:
            maker = self.store.former_owner(room["id"])
            owner = maker if maker and self.store.user(maker) else session.user_id
            self.store.make_regular(room["id"], owner, self.clock())
        self._log(session, "make_public" if value else "make_regular", room["id"], room["name"])
        self._room_updated(room["id"])

    def _rename_room(self, session: Session, msg: dict):
        room = self._own_room_or_error(session, msg.get("room"))
        self._require_staff(session)
        taken = [n for n in self.store.room_names() if n != room["name"]]
        name = clean_room_name(msg.get("name"), taken)
        self.store.rename_room(room["id"], name)
        self._log(session, "rename_room", room["id"], f"{room['name']} -> {name}")
        self._room_updated(room["id"])

    def _set_announcement(self, session: Session, msg: dict):
        """A pinned line at the top of a room, for everyone in it; "" clears it."""
        room = self._own_room_or_error(session, msg.get("room"), system_too=True)
        self._require_staff(session)
        text = one_line(msg.get("text"), ANNOUNCEMENT_MAX, "announcement")
        self.store.set_announcement(room["id"], text)
        self._log(session, "announcement", room["id"], f"{room['name']}: {text or '(cleared)'}")
        self._room_updated(room["id"])

    def _find_rooms(self, session: Session, msg: dict):
        query = msg.get("query") or ""
        if not isinstance(query, str):
            raise RequestError("bad_request", "'query' is text.")
        wait = self.limits.check(("find", session.user_id), *FIND_LIMIT)
        if wait:
            raise RequestError("rate_limited", "Searching too quickly - wait a moment.",
                               retry_after=round(wait, 1))
        permanent_only = bool(msg.get("permanent_only"))
        rows = self.store.find_rooms(" ".join(query.split())[:ROOM_NAME_MAX], FIND_RESULTS, permanent_only)
        session.send({"type": "found_rooms", "query": query, "permanent_only": permanent_only,
                      "rooms": [public_room(r, self._here(r["id"])) for r in rows]})

    def _get_rooms(self, session: Session, msg: dict):
        ids = msg.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise RequestError("bad_request", "'ids' is a list of room ids.")
        ids = list(dict.fromkeys(ids))[:GET_ROOMS_MAX]
        rows = self.store.rooms_by_ids(ids)
        found = {r["id"] for r in rows}
        session.send({"type": "rooms_info", "rooms": [public_room(r, self._here(r["id"])) for r in rows],
                      "missing": [i for i in ids if i not in found]})

    # --------------------------------------------------- app announcements

    def announcements_json(self) -> str:
        """What every Buddy fetches (net.py serves it at /announcements.json):
        no names, no ids of who posted - just the announcements."""
        items = self.store.app_announcements(APP_ANNOUNCEMENTS_SHOWN)
        return json.dumps({"announcements": items}, ensure_ascii=False)

    def _app_announcements_payload(self) -> dict:
        return {"type": "app_announcements",
                "announcements": self.store.app_announcements(APP_ANNOUNCEMENTS_SHOWN)}

    def _post_app_announcement(self, session: Session, msg: dict):
        self._require_owner(session)
        title = one_line(msg.get("title"), APP_TITLE_MAX, "title")
        if not title:
            raise RequestError("bad_request", "Give the announcement a title.")
        text = clean_text(msg.get("text"))
        if len(text) > APP_TEXT_MAX:
            raise RequestError("too_long", f"Announcements are at most {APP_TEXT_MAX:,} characters.")
        self.store.add_app_announcement(title, text, session.user_id, self.clock())
        self._log(session, "app_announcement", "", title)
        session.send(self._app_announcements_payload())

    def _delete_app_announcement(self, session: Session, msg: dict):
        self._require_owner(session)
        announcement_id = msg.get("id")
        if not is_id(announcement_id) or not self.store.delete_app_announcement(announcement_id):
            raise RequestError("no_announcement", "That announcement isn't there any more.")
        self._log(session, "delete_app_announcement", str(announcement_id))
        session.send(self._app_announcements_payload())

    def _list_app_announcements(self, session: Session, msg: dict):
        self._require_owner(session)
        session.send(self._app_announcements_payload())

    def _ping(self, session: Session, msg: dict):
        session.send({"type": "pong"})

    _HANDLERS = {
        "hello": _hello,
        "set_name": _set_name,
        "set_avatar": _set_avatar,
        "save_avatars": _save_avatars,
        "join": _join,
        "leave": _leave,
        "history": _history,
        "send": _send,
        "edit": _edit,
        "react": _react,
        "watch": _watch,
        "set_slow": _set_slow,
        "delete": _delete,
        "purge_message": _purge_message,
        "image_part": _image_part,
        "get_image": _get_image,
        "create_room": _create_room,
        "delete_room": _delete_room,
        "set_topic": _set_topic,
        "set_permanent": _set_permanent,
        "set_public": _set_public,
        "find_rooms": _find_rooms,
        "get_rooms": _get_rooms,
        "rename_room": _rename_room,
        "set_announcement": _set_announcement,
        "post_app_announcement": _post_app_announcement,
        "delete_app_announcement": _delete_app_announcement,
        "list_app_announcements": _list_app_announcements,
        "ping": _ping,
        **SocialMixin._SOCIAL_HANDLERS,
        **AdminMixin._ADMIN_HANDLERS,
        **GifMixin._GIF_HANDLERS,
        **BugMixin._BUG_HANDLERS,
        **ProfileMixin._PROFILE_HANDLERS,
    }


def shown_reactions(entries: list[dict], viewer: str | None) -> list[dict]:
    """A message's reactions (Store.reactions) as one person is sent them:
    each emoticon, how many used it, whether they did, and the first
    REACTION_PEOPLE of who."""
    return [{"r": e["r"], "count": len(e["people"]), "mine": any(p["id"] == viewer for p in e["people"]),
             "people": [{"id": p["id"], "tag": tag_of(p["id"]), "name": p["name"]}
                        for p in e["people"][:REACTION_PEOPLE]]} for e in entries]


def _error(code: str, message: str, re_type, **extra) -> dict:
    return {"type": "error", "code": code, "message": message, "re": re_type, **extra}
