"""The window the header's news orb opens (core/shell_web.py draws the orb,
shown while there's an announcement the user hasn't opened - see
core/announcements.py). A web window (app/web/shell/announcements/):
titles and text go in as plain text, never HTML, so nothing in an
announcement can load or run anything.

Protocol:
    to the view    announcements
    from the view  close
"""

from __future__ import annotations

import os
from datetime import datetime

from core.web_page import WEB_COMMON_DIR, WebDialog


def entries(items: list[dict], seen_id: int) -> list[dict]:
    """The announcements as the window shows them, newest first as given."""
    return [{"title": str(item.get("title", "")), "text": str(item.get("text", "")),
             "when": datetime.fromtimestamp(item["ts"]).strftime("%d %B %Y").lstrip("0") if item.get("ts") else "",
             "new": item.get("id", 0) > (seen_id or 0)} for item in items]


class AnnouncementsDialog(WebDialog):
    web_dir = os.path.join(WEB_COMMON_DIR, "shell", "announcements")

    def __init__(self, parent, items: list[dict], seen_id: int):
        self._entries = entries(items, seen_id)
        super().__init__(parent, parent, "Announcements", (500, 440))

    def web_ready(self):
        self.emit("announcements", {"items": self._entries})

    def on_close(self, _payload=None):
        self.accept()
