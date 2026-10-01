#!/usr/bin/env python3
"""
The link bar (app/web/shell/linkbar/) and the window that adds or edits a
link (app/web/shell/linkedit/). The shell (core/shell_window.py) keeps the
links (core/link_bar.py) and puts the bar along the bottom of the window -
or the top, under the desktop layout; the bar draws them and says what was
clicked, right-clicked or dragged.

Protocol (bar):
    to the view    linkbar
    from the view  open, add, menu, move, size
Protocol (edit window):
    to the view    link, picked, error
    from the view  save, browse, cancel
"""

import os

from PySide6.QtWidgets import QDialog, QFileDialog

from core import link_bar
from core.i18n import tr
from core.shell_web import SHELL_WEB_DIR, _ChromeView
from core.web_page import WebDialog, _FileDropFilter, _theme_host


def _index(payload, key="index"):
    value = (payload or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _LinkDropFilter(_FileDropFilter):
    """Files and folders from Explorer, and links dragged out of a browser."""

    @staticmethod
    def _paths(event):
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        out = []
        for url in mime.urls():
            if url.isLocalFile() and url.toLocalFile():
                out.append(os.path.normpath(url.toLocalFile()))
            elif url.scheme() in ("http", "https"):
                out.append(url.toString())
        return out


class LinkBarView(_ChromeView):
    web_dir = os.path.join(SHELL_WEB_DIR, "linkbar")
    file_drops = True

    def _make_drop_filter(self):
        return _LinkDropFilter(self)

    def on_files_dropped(self, paths):
        self.shell.drop_links(paths)

    def web_ready(self):
        self.shell.push_link_bar()

    def show_state(self, state):
        self.emit("linkbar", state)

    def fit(self, size):
        height = size.get("height")
        if isinstance(height, (int, float)) and 24 <= height <= 120:
            self.setFixedHeight(int(round(height)))

    def on_open(self, payload):
        index = _index(payload)
        if index is not None:
            self.shell.open_link(index)

    def on_add(self, _payload=None):
        self.shell.add_link()

    def on_menu(self, payload):
        index = _index(payload)
        self.shell.open_link_menu(-1 if index is None else index)

    def on_move(self, payload):
        index, to = _index(payload, "from"), _index(payload, "to")
        if index is not None and to is not None:
            self.shell.move_link(index, to)


class LinkDialog(WebDialog):
    """Name and address for a new link (start: what's filled in already -
    a dropped link's address), or one being edited. exec(); the link
    ({"name", "url"}) is in .link once it's saved."""

    web_dir = os.path.join(SHELL_WEB_DIR, "linkedit")

    def __init__(self, parent, start=None, editing=False):
        self._editing = editing
        self._start = dict(start or {"name": "", "url": ""})
        self.link = None
        title = "Edit link" if self._editing else "Add a link"
        super().__init__(_theme_host(parent), parent, title, (480, 330))

    def web_ready(self):
        self.emit("link", {"title": "Edit link" if self._editing else "Add a link",
                           "ok": "Save" if self._editing else "Add",
                           "name": self._start.get("name", ""), "url": self._start.get("url", "")})

    def on_save(self, payload):
        payload = payload or {}
        link, why = link_bar.make_link(payload.get("name"), payload.get("url"))
        if link is None:
            self.emit("error", {"text": why})
            return
        self.link = link
        self.done(QDialog.Accepted)

    def on_browse(self, payload):
        start = str((payload or {}).get("url") or "")
        folder = QFileDialog.getExistingDirectory(self, tr("Choose a folder"),
                                                  start if link_bar.is_path(start) else "")
        if folder:
            self.emit("picked", {"url": os.path.normpath(folder)})

    def on_cancel(self, _payload=None):
        self.done(QDialog.Rejected)
