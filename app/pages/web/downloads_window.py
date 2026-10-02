#!/usr/bin/env python3
"""
The Web tab's Downloads window (web page in downloads/): everything the
browser has downloaded, newest first and grouped by day - with a search,
the kind of file, how big, when, and a sort - and per download: drag it into
Resolve (it lands in the Media Pool's Downloads bin), Send to Resolve,
open it, show it in its folder, remove it from the list or move the file
to the Recycle Bin. A window of its own beside Resolve, like a link bar
folder's Peek.

The browser (page.py WebBrowserPage) keeps the list; this is a view of it.

Protocol (downloads/):
    to the view    downloads {items, folder}, drag_done
    from the view  drag, send, open, reveal, remove, delete, cancel, clear, folder, close
"""

import os

from PySide6.QtCore import Qt

from core.link_peek_web import _place
from core.web_page import WebDialog, _theme_host


def _ids(payload):
    ids = (payload or {}).get("ids")
    if isinstance(ids, str):
        ids = [ids]
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


class DownloadsWindow(WebDialog):
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
    SIZE = (580, 680)

    def __init__(self, owner):
        self.owner = owner
        super().__init__(_theme_host(owner), owner.window(), "Downloads", self.SIZE)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setModal(False)
        _place(self, self.SIZE)

    def web_ready(self):
        self.refresh()

    def refresh(self):
        self.emit("downloads", self.owner.downloads_view())

    # What the page asks; the browser does it.
    def on_drag(self, payload):
        self.owner.drag_downloads(_ids(payload), self.view)
        self.emit("drag_done")

    def on_send(self, payload):
        self.owner.send_downloads(_ids(payload))

    def on_open(self, payload):
        self.owner.open_downloads(_ids(payload))

    def on_reveal(self, payload):
        self.owner.reveal_download((_ids(payload) or [None])[0])

    def on_remove(self, payload):
        self.owner.remove_downloads(_ids(payload))

    def on_delete(self, payload):
        self.owner.delete_downloads(_ids(payload))

    def on_cancel(self, payload):
        self.owner.cancel_downloads(_ids(payload))

    def on_clear(self, _payload=None):
        self.owner.clear_downloads()

    def on_folder(self, _payload=None):
        self.owner.open_downloads_folder()

    def on_close(self, _payload=None):
        self.close()
