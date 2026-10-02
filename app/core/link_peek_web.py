#!/usr/bin/env python3
"""
A link bar link, looked at without leaving Buddy - from its right-click
menu (core/shell_window.py):

  FolderPeek    a folder's contents (core/folder_peek.py) in a small window
                of its own: go into subfolders, open a file, or drag files
                and folders out - into Resolve's Media Pool, say. The page
                can't start a drag that leaves the window (a browser can't
                hand out real files), so it says which items, and the drag
                is started here as Explorer's would be: copy only, never a
                move.
  PagePreview   a still picture of a web page: the page is loaded out of
                sight in a private browser (no cookies or logins, muted, no
                pop-ups or downloads), photographed once it settles, and
                closed - what's shown can't be clicked or run anything.

Protocol (FolderPeek, app/web/shell/peek/):
    to the view    folder, drag_done
    from the view  go, open, reveal, drag, close
Protocol (PagePreview, app/web/shell/preview/):
    to the view    preview
    from the view  refresh, browser, close
"""

import base64
import os
from urllib.parse import urlsplit

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QMimeData, QObject, QPointF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices, QDrag, QMouseEvent
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

from core import folder_peek, link_bar
from core.shell_web import SHELL_WEB_DIR
from core.web_page import WebDialog, _theme_host


def _place(window, size):
    """Beside the pointer (where the link was right-clicked), kept on its screen."""
    at = QCursor.pos()
    screen = QApplication.screenAt(at) or QApplication.primaryScreen()
    area = screen.availableGeometry()
    w, h = size
    x = min(max(area.left(), at.x() - w // 2), area.right() - w)
    y = at.y() - h - 12 if at.y() - h - 12 >= area.top() else at.y() + 12
    window.move(x, min(max(area.top(), y), area.bottom() - h))


def start_file_drag(owner, view, paths):
    """Carries `paths` out of a web page as files, as Explorer's drag
    would - copy only, never a move. Called while the page's button is
    still down (it says when a drag starts); runs until it's let go, and
    returns what the drop did (Qt.IgnoreAction: nothing took it)."""
    if not paths:
        return Qt.IgnoreAction
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    drag = QDrag(owner)
    drag.setMimeData(mime)
    result = drag.exec(Qt.CopyAction)
    # Windows' drag took the button's release, so the page never saw it -
    # without one it would think the button's still down.
    target = view.focusProxy() or view
    local = QPointF(target.mapFromGlobal(QCursor.pos()))
    QApplication.sendEvent(target, QMouseEvent(QEvent.MouseButtonRelease, local, QPointF(QCursor.pos()),
                                               Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    return result


class FolderPeek(WebDialog):
    """A folder link's contents. show() it - it stays open beside Resolve
    so things can be dragged across; closing it frees it."""

    web_dir = os.path.join(SHELL_WEB_DIR, "peek")
    SIZE = (440, 520)

    def __init__(self, parent, root, name=""):
        self.root = os.path.abspath(root)
        self.folder = self.root
        super().__init__(_theme_host(parent), parent, "Peek", self.SIZE)
        self.setWindowTitle(name or link_bar.default_name(self.root))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setModal(False)
        _place(self, self.SIZE)

    def web_ready(self):
        self._push()

    def _push(self):
        self.emit("folder", folder_peek.list_folder(self.folder, self.root))

    def on_go(self, payload):
        path = (payload or {}).get("path")
        if isinstance(path, str) and folder_peek.within(path, self.root) and os.path.isdir(path):
            self.folder = os.path.abspath(path)
        self._push()                                   # a refresh, too

    def on_open(self, payload):
        for path in folder_peek.allowed((payload or {}).get("paths"), self.root)[:10]:
            if os.path.isdir(path):
                self.folder = path
                self._push()
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def on_reveal(self, _payload=None):
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder))

    def on_close(self, _payload=None):
        self.close()

    def on_drag(self, payload):
        """The page's items are being dragged: carry them out as files.
        The button is still down, so the drag runs until it's let go."""
        start_file_drag(self, self.view, folder_peek.allowed((payload or {}).get("paths"), self.root))
        self.emit("drag_done")


# ------------------------------------------------------------- previews --

# The page is drawn at this size, photographed, then shrunk to this width.
CAPTURE_SIZE = (1280, 800)
PICTURE_WIDTH = 1280
SETTLE_MS = 1500           # after it loads: late images, fonts, cookie banners
GIVE_UP_MS = 20000         # still loading by then: photograph what there is

_preview_profile = None


def _private_profile():
    """Off the record and kept apart from Buddy's own pages: no cookies,
    cache or logins kept, no pop-ups, plugins or clipboard, and downloads
    refused. Parented to the application, which outlives every page."""
    global _preview_profile
    if _preview_profile is None:
        _preview_profile = QWebEngineProfile(QApplication.instance())
        _preview_profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
        _preview_profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        s = _preview_profile.settings()
        for attribute, on in ((QWebEngineSettings.JavascriptCanOpenWindows, False),
                              (QWebEngineSettings.JavascriptCanAccessClipboard, False),
                              (QWebEngineSettings.PluginsEnabled, False),
                              (QWebEngineSettings.LocalContentCanAccessFileUrls, False),
                              (QWebEngineSettings.PlaybackRequiresUserGesture, True),
                              (QWebEngineSettings.ShowScrollBars, False)):
            s.setAttribute(attribute, on)
        _preview_profile.downloadRequested.connect(lambda item: item.cancel())
    return _preview_profile


class PageCapture(QObject):
    """Loads `url` out of sight and photographs it: done(jpeg bytes or
    None, page title, why it failed or "")."""

    done = Signal(object, str, str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self._finished = False
        self.web = QWebEngineView()
        page = QWebEnginePage(_private_profile(), self.web)
        page.setAudioMuted(True)
        self.web.setPage(page)
        self.web.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.web.resize(*CAPTURE_SIZE)
        self.web.show()
        page.loadFinished.connect(self._loaded)
        QTimer.singleShot(GIVE_UP_MS, lambda: self._snap(timed_out=True))
        self.web.load(QUrl(url))

    def _loaded(self, ok):
        if self._finished:
            return
        if not ok:
            self._finish(None, "", "Couldn't load the page. Check the address, and that you're online.")
            return
        QTimer.singleShot(SETTLE_MS, self._snap)

    def _snap(self, timed_out=False):
        if self._finished:
            return
        image = self.web.grab().toImage()
        if image.isNull() or image.width() < 10:
            self._finish(None, "", "The page didn't draw anything to show.")
            return
        if image.width() > PICTURE_WIDTH:
            image = image.scaledToWidth(PICTURE_WIDTH, Qt.SmoothTransformation)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QBuffer.WriteOnly)
        image.save(buffer, "JPEG", 85)
        self._finish(bytes(data), self.web.page().title(), "")

    def _finish(self, jpeg, title, why):
        self._finished = True
        # Closed straight away: nothing on the page runs once it's photographed.
        self.web.page().triggerAction(QWebEnginePage.Stop)
        self.web.deleteLater()
        self.done.emit(jpeg, title, why)


class PagePreview(WebDialog):
    """A still picture of a web link. show() it."""

    web_dir = os.path.join(SHELL_WEB_DIR, "preview")
    SIZE = (720, 540)

    def __init__(self, parent, url, name=""):
        self.url = url
        self._capture = None
        self._state = {"url": url, "host": urlsplit(url).hostname or url, "name": name, "state": "loading"}
        super().__init__(_theme_host(parent), parent, "Preview", self.SIZE)
        self.setWindowTitle(name or self._state["host"])
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setModal(False)
        _place(self, self.SIZE)
        self._take()

    def web_ready(self):
        self.emit("preview", self._state)

    def _take(self):
        self._state.update(state="loading", image=None, error="")
        self.emit("preview", self._state)
        self._capture = PageCapture(self.url, self)
        self._capture.done.connect(self._taken)

    def _taken(self, jpeg, title, why):
        self._capture = None
        if jpeg:
            self._state.update(state="ready", title=title,
                               image="data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"))
        else:
            self._state.update(state="failed", error=why)
        self.emit("preview", self._state)

    def on_refresh(self, _payload=None):
        if self._capture is None:
            self._take()

    def on_browser(self, _payload=None):
        QDesktopServices.openUrl(QUrl(self.url))

    def on_close(self, _payload=None):
        self.close()
