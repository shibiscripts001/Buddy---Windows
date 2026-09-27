"""GIF search, for the page (page.py mixes this in): the picker's searches,
its previews and the GIF picked - which goes in the composer like any
picture, so it can have a message with it and is shrunk and sent (and in a
DM, encrypted) the same way (attachments.py).

Every search goes to the Buddy Network server, which asks GIPHY
(server/gifs.py): GIPHY never sees this PC, and nothing here ever loads
anything from GIPHY. Each preview is decoded and checked here
(images.thumb_check) before the page gets it, as a data: URL.

Offered only when the server says it has GIF search (its welcome's
limits: "gifs") and images can be sent.

To the view: gif_results, gif_thumb. From it: gif_search, gif_pick.
"""

from __future__ import annotations

from core.i18n import LANGUAGE_CODES, current_language

from . import e2e, images

# GIPHY's names for Buddy's languages, where they differ (server/gifs.py LANGS).
GIPHY_LANGS = {"zh-Hans": "zh-CN"}
QUERY_MAX = 50


class GifSearchMixin:
    # Uses from the page: client, emit, _notify, editing; from attachments.py:
    # images_allowed, image_limits, _attach, _drop_attachment, _push_attachment,
    # _preparing, _shrink_generation.

    def _init_gifs(self):
        self._gif_nonce = 0
        self._gif_query = ""
        self._gif_next = None      # the offset of the next page of this search, if there is one
        self._gif_appending = False   # the search on its way is the next page, not a new one
        self._gif_shown = set()    # the GIFs in the picker: only their previews are wanted
        self._gif_picking = None   # (GIF id, attachments.py's generation) of the one asked for, to send

    def gifs_allowed(self) -> bool:
        return self.images_allowed() and bool((self.image_limits or {}).get("gifs"))

    def _reset_gifs(self):
        """A new connection: nothing asked for is coming any more."""
        self._gif_next, self._gif_shown, self._gif_picking = None, set(), None
        self._gif_nonce += 1
        self.emit("gif_results", {"reset": True})

    def on_gif_search(self, payload):
        """A search (or "" for GIPHY's Trending), or more of the last one."""
        payload = payload or {}
        if not self.gifs_allowed():
            return
        query = " ".join(str(payload.get("q") or "").split())[:QUERY_MAX]
        more = bool(payload.get("more")) and query == self._gif_query and self._gif_next is not None
        offset = self._gif_next if more else 0
        self._gif_nonce += 1
        self._gif_query, self._gif_appending = query, more
        if not more:
            self._gif_shown = set()
        code = LANGUAGE_CODES.get(current_language(), "en")
        if not self.client.send({"type": "gif_search", "q": query, "offset": offset, "nonce": self._gif_nonce,
                                 "lang": GIPHY_LANGS.get(code, code)}):
            self.emit("gif_results", {"error": "Not connected."})
            return
        self.emit("gif_results", {"loading": True, "append": more})

    def _gif_results(self, msg: dict):
        if msg.get("nonce") != self._gif_nonce:
            return   # an older search
        results = [r for r in msg.get("results") or [] if isinstance(r, dict) and isinstance(r.get("id"), str)]
        append = self._gif_appending
        self._gif_next = msg.get("next") if msg.get("more") and isinstance(msg.get("next"), int) else None
        self._gif_shown |= {r["id"] for r in results}
        self.emit("gif_results", {
            "append": append, "more": self._gif_next is not None,
            "empty": "No GIFs found – try other words." if not results and not append else "",
            "results": [{"id": r["id"], "w": _size(r.get("w")), "h": _size(r.get("h")),
                         "title": str(r.get("title") or "")[:80], "user": str(r.get("user") or "")[:40]}
                        for r in results],
        })

    def _gif_thumb(self, msg: dict):
        gif_id = msg.get("id")
        if gif_id not in self._gif_shown:
            return
        raw = e2e.unb64(msg.get("data"))
        mime = images.thumb_check(raw) if raw else None
        if mime:
            self.emit("gif_thumb", {"id": gif_id, "url": images.data_url(raw, mime)})

    def on_gif_pick(self, payload):
        gif_id = str((payload or {}).get("id") or "")
        if not self.gifs_allowed() or gif_id not in self._gif_shown:
            return
        if self.editing is not None:
            self._notify("Finish editing first – a picture goes with a new message.")
            return
        if not self.client.send({"type": "gif_get", "id": gif_id}):
            return
        # In the composer's place, like a picture being prepared: Remove
        # (or another picture) means it isn't wanted when it comes.
        self._drop_attachment()
        self._gif_picking = (gif_id, self._shrink_generation)
        self._preparing = "Getting the GIF from GIPHY…"
        self._notify("")
        self._push_attachment()

    def _gif_wanted(self, gif_id) -> bool:
        return self._gif_picking == (gif_id, self._shrink_generation)

    def _gif_data(self, msg: dict):
        if not self._gif_wanted(msg.get("id")):
            return   # removed, or another picture since
        self._gif_picking = None
        raw = e2e.unb64(msg.get("data"))
        if not raw:
            self._gif_failed("That GIF couldn't be downloaded – try another one.")
            return
        self._attach(raw, gif={"id": msg["id"], "user": str(msg.get("user") or "")[:40]})

    def _gif_failed(self, text: str):
        self._preparing = ""
        self._push_attachment()
        self._notify(text)

    def _gif_error(self, msg: dict) -> bool:
        """Handles an error about GIF search; False if it wasn't one."""
        if msg.get("re") == "gif_search":
            if msg.get("nonce") in (self._gif_nonce, None):
                self.emit("gif_results", {"error": msg.get("message") or "GIF search isn't working right now."})
            return True
        if msg.get("re") == "gif_get":
            if self._gif_wanted(msg.get("id")):
                self._gif_picking = None
                self._gif_failed(msg.get("message") or "That GIF couldn't be downloaded – try another one.")
            return True
        return False


def _size(raw) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 4096 else 100
