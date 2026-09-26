"""App announcements: the pulsing orb next to "Buddy" in the header
(core/shell_web.py's header, which opens core/announcements_window.py) when there's one the user hasn't opened yet.

Once a day at most, Buddy fetches /announcements.json from the Buddy
Network server - a plain request carrying nothing about the user (no id,
no token, no project details). The answer and the time are kept in
SharedSettings, so restarting Buddy doesn't fetch again. Turning off
"Show announcements from Buddy" in Settings stops it completely.

On by default for everyone, chat user or not - the server's admin posts them
from Buddy Network's Admin panel (App tab).
"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from core.buddy_server import http_url

PATH = "/announcements.json"
CHECK_EVERY = 24 * 3600
RETRY_AFTER = 3600            # after a failed check
FIRST_CHECK_DELAY_MS = 10_000  # let Buddy finish starting first
MAX_BYTES = 64 * 1024
MAX_ITEMS = 10
TITLE_MAX, TEXT_MAX = 80, 1000

ENABLED_KEY = "announcements_enabled"
SEEN_KEY = "announcements_seen"          # the newest id the user has opened
CACHE_KEY = "announcements_cache"
CHECKED_KEY = "announcements_checked_at"


def parse(data: bytes) -> list[dict] | None:
    """The announcements in a server answer, newest first, or None if it
    isn't one. Anything unexpected is dropped rather than shown."""
    if len(data) > MAX_BYTES:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    items = doc.get("announcements") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ident, ts, title, text = item.get("id"), item.get("ts"), item.get("title"), item.get("text")
        if (isinstance(ident, int) and not isinstance(ident, bool) and isinstance(ts, (int, float))
                and isinstance(title, str) and isinstance(text, str) and title.strip()):
            out.append({"id": ident, "ts": float(ts), "title": title.strip()[:TITLE_MAX],
                        "text": text.strip()[:TEXT_MAX]})
    out.sort(key=lambda a: a["id"], reverse=True)
    return out[:MAX_ITEMS]


def has_unseen(items: list[dict], seen_id: int) -> bool:
    return bool(items) and items[0]["id"] > (seen_id or 0)


class AnnouncementChecker(QObject):
    changed = Signal()

    def __init__(self, settings, server_url, parent=None, clock=time.time):
        """settings: SharedSettings. server_url: a callable returning the
        Buddy Network server's address (read when checking, so a changed
        address is used)."""
        super().__init__(parent)
        self.settings = settings
        self.server_url = server_url
        self.clock = clock
        self._net = QNetworkAccessManager(self)
        self._reply = None
        self._next_try = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(15 * 60 * 1000)   # just looks at the clock; fetches once a day
        self._timer.timeout.connect(self._maybe_check)

    # ---------------------------------------------------------------- state

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get(ENABLED_KEY, True))

    @property
    def items(self) -> list[dict]:
        # Re-checked like a fresh answer: settings.json is only a cache.
        cached = self.settings.get(CACHE_KEY)
        if not isinstance(cached, list):
            return []
        return parse(json.dumps({"announcements": cached}).encode("utf-8")) or []

    def unseen(self) -> bool:
        return self.enabled and has_unseen(self.items, self.settings.get(SEEN_KEY, 0))

    def mark_seen(self):
        if self.items:
            self.settings[SEEN_KEY] = self.items[0]["id"]
            self.settings.save()
        self.changed.emit()

    # -------------------------------------------------------------- control

    def start(self):
        if self.enabled:
            self._timer.start()
            QTimer.singleShot(FIRST_CHECK_DELAY_MS, self._maybe_check)
        self.changed.emit()

    def set_enabled(self, on: bool):
        self.settings[ENABLED_KEY] = bool(on)
        self.settings.save()
        if on:
            self._timer.start()
            self._maybe_check()
        else:
            self._timer.stop()
            if self._reply is not None:
                self._reply.abort()
        self.changed.emit()

    def check_now(self):
        """Ignores the once-a-day rule (used right after an admin posts one)."""
        if self.enabled:
            self._fetch()

    def _maybe_check(self):
        now = self.clock()
        due = now - float(self.settings.get(CHECKED_KEY, 0) or 0) >= CHECK_EVERY
        if self.enabled and due and now >= self._next_try:
            self._fetch()

    def _fetch(self):
        url = http_url(self.server_url(), PATH)
        if not url or self._reply is not None:
            return
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(15_000)
        # No cookies, no identity - just the request.
        request.setAttribute(QNetworkRequest.CookieSaveControlAttribute, QNetworkRequest.Manual)
        request.setAttribute(QNetworkRequest.CookieLoadControlAttribute, QNetworkRequest.Manual)
        self._reply = self._net.get(request)
        self._reply.finished.connect(self._on_finished)

    def _on_finished(self):
        reply, self._reply = self._reply, None
        items = None
        if reply.error() == QNetworkReply.NoError:
            items = parse(bytes(reply.read(MAX_BYTES + 1)))
        reply.deleteLater()
        if items is None:
            self._next_try = self.clock() + RETRY_AFTER
            return
        self.settings[CACHE_KEY] = items
        self.settings[CHECKED_KEY] = self.clock()
        self.settings.save()
        self.changed.emit()
