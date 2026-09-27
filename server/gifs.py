"""GIF search, from GIPHY, mixed into NetworkCore (core.py).

The server asks GIPHY - never a Buddy - so GIPHY's API key stays here
(GIPHY_API_KEY on the server, see server/README.md; it's never in Buddy),
and GIPHY only ever sees this server asking: not who is searching, or from
where. Without a key, the welcome says there's no GIF search and Buddy
doesn't offer it (sending a GIF file still works: it's just an image).

    gif_search {q, offset, lang, nonce} -> gif_results {nonce, results, more, next}
        then a gif_thumb {id, data} for each result's small preview as it
        arrives. q "" is GIPHY's Trending.
    gif_get {id}  -> gif_data {id, data}: the GIF itself, which the Buddy
        shrinks and sends like any other image (images.py) - so the people
        who see it never load anything from GIPHY either.

GIPHY's free key allows about 100 calls an hour, for everyone together.
So: results are kept (a search SEARCH_KEEP seconds, Trending TRENDING_KEEP),
two people asking the same thing at once ask GIPHY once, each person has
their own limit (GIF_SEARCH_LIMIT), and once the hour's calls are used
(GIPHY_CALLS_PER_HOUR) searches not already kept wait. Previews and the
GIFs themselves come from GIPHY's media servers, which aren't API calls.

Downloads go through `fetch` (net.py's: each on a thread, so nothing
waits for GIPHY), and only ever to GIPHY's own hosts over https. A message
with a GIF says it came from GIPHY, and whose it is where GIPHY says (its
terms ask for that) - but only for a GIF this connection was handed with
gif_get, so nobody can credit GIPHY or anyone else with their own picture.
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import re
import unicodedata
from urllib.parse import urlencode, urlsplit

from .common import RequestError

log = logging.getLogger("buddy_network")

API = "https://api.giphy.com/v1/gifs/"
PAGE = 24                     # GIFs per page of results
MAX_OFFSET = 10 * PAGE        # "More" goes this deep into one search
QUERY_MAX = 50
RATINGS = ("g", "pg", "pg-13", "r")
DEFAULT_RATING = "pg-13"
DEFAULT_CALLS_PER_HOUR = 90   # the free key's 100, with room to spare
# GIPHY's lang codes for Buddy's languages (core/i18n.py); anything else is English.
LANGS = {"en", "ja", "es", "de", "fr", "ko", "zh-CN", "ar", "vi"}
SEARCH_KEEP = 3600.0
TRENDING_KEEP = 1800.0
RESULTS_KEPT = 500            # searches kept
ITEMS_KEPT = 5000             # GIFs from those results, for gif_get
API_MAX_BYTES = 2 * 1024 ** 2
THUMB_MAX_BYTES = 64 * 1024   # a preview; past this, the next smaller kind
THUMBS_KEPT_BYTES = 32 * 1024 ** 2
# The GIF to send: the Buddy shrinks it to images.py's 400 KB. Kept well
# under net.py's MAX_QUEUED_BYTES, with a page of previews queued too.
SEND_MAX_BYTES = 1200 * 1024
FETCHED_KEPT = 20             # GIFs one connection has been handed (and may send)
TITLE_MAX, USER_MAX = 80, 40

# (limit, window seconds), per person
GIF_SEARCH_LIMIT = (30, 300.0)
GIF_GET_LIMIT = (30, 3600.0)

# GIPHY's renditions, best first: the preview in the picker, and what's sent.
THUMB_RENDITIONS = ("fixed_width_small", "fixed_height_small", "fixed_width_downsampled",
                    "fixed_height_downsampled")
SEND_RENDITIONS = ("original", "fixed_height", "fixed_width")
_GIF_ID = re.compile(r"[A-Za-z0-9]{1,64}")
_MEDIA_HOST = re.compile(r"(media[0-9]*|i)\.giphy\.com")


def is_gif_file(data) -> bool:
    """Starts like a GIF or a WebP - what GIPHY's media servers send. (Buddy
    decodes and checks it before showing or sending it.)"""
    return isinstance(data, bytes) and (data.startswith((b"GIF87a", b"GIF89a"))
                                        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def media_url(raw) -> str | None:
    """A GIPHY media address, or None - nothing else is ever downloaded."""
    if not isinstance(raw, str) or len(raw) > 1000:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    return raw if parts.scheme == "https" and _MEDIA_HOST.fullmatch(parts.hostname or "") and not parts.port else None


def clean_query(raw) -> str:
    """One line of at most QUERY_MAX characters; "" for Trending."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise RequestError("bad_request", "'q' is text.")
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in unicodedata.normalize("NFKC", raw))
    query = " ".join(text.split())
    if len(query) > QUERY_MAX:
        raise RequestError("bad_request", f"Searches are at most {QUERY_MAX} characters.")
    return query


def _count(raw) -> int:
    """GIPHY's sizes are strings of digits; 0 for anything else."""
    if isinstance(raw, str) and raw.isdigit() and len(raw) <= 12:
        return int(raw)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def _one_line(raw, most: int) -> str:
    if not isinstance(raw, str):
        return ""
    text = " ".join("".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in raw).split())
    return text[:most]


def _rendition(images: dict, names, max_bytes: int) -> dict | None:
    """The first of `names` that GIPHY has in a size that will do, as
    {"url", "w", "h"} - its WebP if it has one, else its GIF."""
    for name in names:
        r = images.get(name)
        if not isinstance(r, dict):
            continue
        w, h = _count(r.get("width")), _count(r.get("height"))
        if not (1 <= w <= 4096 and 1 <= h <= 4096):
            continue
        for url_key, size_key in (("webp", "webp_size"), ("url", "size")):
            url, size = media_url(r.get(url_key)), _count(r.get(size_key))
            if url and 0 < size <= max_bytes:
                return {"url": url, "w": w, "h": h}
    return None


def parse_results(raw: bytes, offset: int) -> tuple[list[dict], bool] | None:
    """GIPHY's answer as the GIFs this server can offer, and whether there
    are more; None if it isn't an answer."""
    try:
        answer = json.loads(raw.decode("utf-8"))
        data = answer["data"]
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(data, list):
        return None
    items = []
    for g in data[:PAGE]:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not _GIF_ID.fullmatch(g["id"]):
            continue
        images = g.get("images") if isinstance(g.get("images"), dict) else {}
        send = _rendition(images, SEND_RENDITIONS, SEND_MAX_BYTES)
        if send is None:
            continue
        user = g.get("user") if isinstance(g.get("user"), dict) else {}
        items.append({"id": g["id"], "send": send, "thumb": _rendition(images, THUMB_RENDITIONS, THUMB_MAX_BYTES),
                      "title": _one_line(g.get("title"), TITLE_MAX),
                      "user": _one_line(user.get("display_name") or g.get("username"), USER_MAX)})
    pagination = answer.get("pagination") if isinstance(answer.get("pagination"), dict) else {}
    total = _count(pagination.get("total_count"))
    more = bool(data) and offset + PAGE <= MAX_OFFSET and offset + len(data) < total
    return items, more


def _error(code: str, message: str, re_type: str, **extra) -> dict:
    return {"type": "error", "code": code, "message": message, "re": re_type, **extra}


class GifMixin:
    # Uses from NetworkCore: clock, limits, _sessions. Set by NetworkCore:
    # giphy (a GiphySettings or None) and fetch (net.py's, or a test's).

    def _init_gifs(self):
        self._gif_results = collections.OrderedDict()   # (q folded, offset, lang) -> {"until", "items", "more"}
        self._gif_items = collections.OrderedDict()     # GIPHY id -> an item from parse_results
        self._gif_thumbs = collections.OrderedDict()    # GIPHY id -> its preview's bytes
        self._gif_thumb_bytes = 0
        self._gif_asking = {}      # search key -> the sessions waiting for GIPHY's answer
        self._thumb_asking = {}    # GIPHY id -> the sessions waiting for its preview

    def gifs_offered(self) -> bool:
        return self.giphy is not None and self.fetch is not None

    # ------------------------------------------------------------ searching

    def _gif_search(self, session, msg: dict):
        if not self.gifs_offered():
            raise RequestError("no_gifs", "This server doesn't offer GIF search.")
        query = clean_query(msg.get("q"))
        offset = msg.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= MAX_OFFSET:
            raise RequestError("bad_request", f"'offset' is 0-{MAX_OFFSET}.")
        lang = msg.get("lang") if msg.get("lang") in LANGS else "en"
        nonce = msg.get("nonce") if isinstance(msg.get("nonce"), (int, str)) else None
        wait = self.limits.check(("gif_search", session.user_id), *GIF_SEARCH_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of GIF searching - wait a moment.",
                               retry_after=round(wait))
        key = (query.casefold(), offset, lang if query else "en")   # Trending is the same in every language
        session.gif_search = (key, nonce)
        session.gif_wanted = set()
        kept = self._gif_results.get(key)
        if kept is not None and kept["until"] > self.clock():
            self._gif_results.move_to_end(key)
            self._gif_deliver(session, key, kept)
            return
        if key in self._gif_asking:
            self._gif_asking[key].add(session)
            return
        wait = self.limits.check(("giphy",), self.giphy.calls_per_hour, 3600.0)
        if wait:
            minutes = max(1, round(wait / 60))
            raise RequestError("gif_busy", f"GIF search is busy - try again in {minutes} "
                                           f"minute{'s' if minutes != 1 else ''}.", retry_after=round(wait))
        self._gif_asking[key] = {session}
        params = {"api_key": self.giphy.key, "limit": PAGE, "offset": offset, "rating": self.giphy.rating,
                  "bundle": "messaging_non_clips"}
        if query:
            params.update(q=query, lang=key[2])
        url = API + ("search?" if query else "trending?") + urlencode(params)
        self.fetch(url, API_MAX_BYTES, lambda data: self._gif_answered(key, data))

    def _gif_answered(self, key, data):
        waiting = self._gif_asking.pop(key, set())
        parsed = parse_results(data, key[1]) if data else None
        if parsed is None:
            for session in waiting:
                if session in self._sessions and session.gif_search and session.gif_search[0] == key:
                    session.send(_error("gif_failed", "GIF search isn't answering right now - try again in a "
                                                      "moment.", "gif_search", nonce=session.gif_search[1]))
            return
        items, more = parsed
        keep = SEARCH_KEEP if key[0] else TRENDING_KEEP
        kept = {"until": self.clock() + keep, "items": [i["id"] for i in items], "more": more}
        self._gif_results[key] = kept
        while len(self._gif_results) > RESULTS_KEPT:
            self._gif_results.popitem(last=False)
        for item in items:
            self._gif_items[item["id"]] = item
            self._gif_items.move_to_end(item["id"])
        while len(self._gif_items) > ITEMS_KEPT:
            self._gif_items.popitem(last=False)
        for session in waiting:
            self._gif_deliver(session, key, kept)

    def _gif_deliver(self, session, key, kept: dict):
        """Results to a session still waiting for exactly them, then their previews."""
        if session not in self._sessions or not session.gif_search or session.gif_search[0] != key:
            return
        items = [self._gif_items[i] for i in kept["items"] if i in self._gif_items]
        session.send({"type": "gif_results", "nonce": session.gif_search[1], "more": kept["more"],
                      "next": key[1] + PAGE,
                      "results": [{"id": i["id"], "w": (i["thumb"] or i["send"])["w"],
                                   "h": (i["thumb"] or i["send"])["h"], "title": i["title"], "user": i["user"]}
                                  for i in items]})
        session.gif_wanted = {i["id"] for i in items}
        for item in items:
            self._gif_thumb(session, item)

    def _gif_thumb(self, session, item: dict):
        gif_id = item["id"]
        data = self._gif_thumbs.get(gif_id)
        if data is not None:
            self._gif_thumbs.move_to_end(gif_id)
            session.send({"type": "gif_thumb", "id": gif_id, "data": base64.b64encode(data).decode("ascii")})
            return
        if item["thumb"] is None:
            return
        if gif_id in self._thumb_asking:
            self._thumb_asking[gif_id].add(session)
            return
        self._thumb_asking[gif_id] = {session}
        self.fetch(item["thumb"]["url"], THUMB_MAX_BYTES, lambda data: self._gif_thumb_arrived(gif_id, data))

    def _gif_thumb_arrived(self, gif_id: str, data):
        waiting = self._thumb_asking.pop(gif_id, set())
        if not is_gif_file(data):
            return   # the picker shows the GIF's title instead
        if gif_id not in self._gif_thumbs:
            self._gif_thumbs[gif_id] = data
            self._gif_thumb_bytes += len(data)
            while self._gif_thumb_bytes > THUMBS_KEPT_BYTES:
                _old, old_data = self._gif_thumbs.popitem(last=False)
                self._gif_thumb_bytes -= len(old_data)
        text = base64.b64encode(data).decode("ascii")
        for session in waiting:
            if session in self._sessions and gif_id in session.gif_wanted:
                session.send({"type": "gif_thumb", "id": gif_id, "data": text})

    # ------------------------------------------------------------- choosing

    def _gif_get(self, session, msg: dict):
        """The GIF picked in the picker, to send."""
        gif_id = msg.get("id")
        if not isinstance(gif_id, str) or not _GIF_ID.fullmatch(gif_id):
            raise RequestError("bad_request", "'id' is a GIF's id.")
        item = self._gif_items.get(gif_id) if self.gifs_offered() else None
        if item is None:
            raise RequestError("no_gif", "That GIF isn't available any more - search for it again.", id=gif_id)
        wait = self.limits.check(("gif_get", session.user_id), *GIF_GET_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of GIFs - try again later.", retry_after=round(wait),
                               id=gif_id)
        session.gif_wanted = set()   # the picker has closed: no more previews
        self.fetch(item["send"]["url"], SEND_MAX_BYTES, lambda data: self._gif_fetched(session, item, data))

    def _gif_fetched(self, session, item: dict, data):
        if session not in self._sessions:
            return
        if not is_gif_file(data):
            session.send(_error("gif_failed", "That GIF couldn't be downloaded - try another one.", "gif_get",
                                id=item["id"]))
            return
        session.gif_fetched[item["id"]] = item["user"]
        session.gif_fetched.move_to_end(item["id"])
        while len(session.gif_fetched) > FETCHED_KEPT:
            session.gif_fetched.popitem(last=False)
        session.send({"type": "gif_data", "id": item["id"], "user": item["user"],
                      "data": base64.b64encode(data).decode("ascii")})

    def gif_credit(self, session, raw) -> str | None:
        """What a sent image says about where it's from (stored as JSON):
        None for a picture of the sender's own; for a GIF from the picker,
        GIPHY and the GIF's creator - only if this connection was handed
        it."""
        if raw is None:
            return None
        if not isinstance(raw, str) or raw not in session.gif_fetched:
            raise RequestError("bad_image", "That GIF didn't come from this server's GIF search.")
        return json.dumps({"source": "giphy", "id": raw, "user": session.gif_fetched[raw]}, ensure_ascii=False,
                          separators=(",", ":"))

    _GIF_HANDLERS = {
        "gif_search": _gif_search,
        "gif_get": _gif_get,
    }


class GiphySettings:
    """The server's GIPHY key and how to use it - from its environment
    (python -m server reads them; see server/README.md)."""

    def __init__(self, key: str, rating: str = DEFAULT_RATING, calls_per_hour: int = DEFAULT_CALLS_PER_HOUR):
        self.key = key
        self.rating = rating if rating in RATINGS else DEFAULT_RATING
        self.calls_per_hour = max(1, int(calls_per_hour))

    @classmethod
    def from_environment(cls, env) -> GiphySettings | None:
        key = (env.get("GIPHY_API_KEY") or "").strip()
        if not key:
            return None
        try:
            calls = int(env.get("GIPHY_CALLS_PER_HOUR") or DEFAULT_CALLS_PER_HOUR)
        except ValueError:
            calls = DEFAULT_CALLS_PER_HOUR
        return cls(key, (env.get("GIPHY_RATING") or DEFAULT_RATING).strip().lower(), calls)
