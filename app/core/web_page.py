#!/usr/bin/env python3
"""
WebToolPage - a ToolPage whose UI is HTML/CSS/JS in a QWebEngineView -
and WebWindow, the same for a small window of its own (Color Palette's
mini palette, which floats over Resolve), and WebDialog for a modal one
(the shell's Settings, core/message_dialog.py's alert() and confirm()).
All are a WebSurface: the
view, the channel to its JS and the theme.

Buddy is moving its UI to web pages one tool at a time. A web tool is
still a ToolPage living in the same shell - nav rail, header, dual view,
busy overlay and the Settings dialog are unchanged - so a ported tool and
an unported one sit side by side and Buddy stays releasable throughout.

The split:
    Python (the page subclass) owns ALL state and every Resolve / file /
    network call, exactly as before. JS is a view: it draws what Python
    sends and reports what the user did.

    Python -> JS   self.emit("event_name", payload)   (payload: JSON-able)
    JS -> Python   Buddy.send("action_name", payload) calls
                   self.on_action_name(payload) on the page

Actions are dispatched on the next event-loop turn, never inside the
channel's own message handler, so a handler may open a modal dialog
(QFileDialog, a consent prompt) without re-entering QWebChannel. Events
emitted before the page's JS has connected are queued and flushed once it
does. After a reload (or a crashed renderer being restarted) the queue
starts empty and web_ready() is called again: a page should push its whole
state from there, not assume the view remembers anything.

The HTML lives next to the page, at pages/<tool>/web/index.html, and pulls
in the shared app/web/buddy.css and buddy.js (see those files). The theme
arrives as CSS variables from core/web_theme.py, on load and on every
theme change; the language's strings ("i18n", core/i18n.py) on load and
whenever Settings changes it, and buddy.js translates the page - so a page
writes English and needs nothing more.

Set BUDDY_WEB_DEBUG=1 to get Chromium DevTools at http://localhost:9223.
"""

import json
import os
import sys
import weakref

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QWidget

if os.environ.get("BUDDY_WEB_DEBUG"):
    os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", "9223")

from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from core import crash_log
from core.i18n import LANGUAGE_CODES, get_i18n, tr, web_strings
from core.web_theme import web_theme
from pages.base import ToolPage

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_COMMON_DIR = os.path.join(APP_DIR, "web")

# A window of its own waits this long at most for its page before showing
# anyway (see _ReadyToShow), and this long after the page connects - for
# its script to apply the theme and Chromium to draw.
READY_TIMEOUT_MS = 1500
READY_SETTLE_MS = 40

# Links a page may open in the user's browser. Anything else a page tries
# to navigate to is refused: the view only ever shows its own file.
EXTERNAL_SCHEMES = ("http", "https", "mailto")

_profile = None


def _shared_profile():
    """One off-the-record profile for every web page: nothing (cache,
    cookies, localStorage) is written to disk, and the pages share one set
    of Chromium processes. Parented to the application so it outlives
    every page using it - Qt warns and leaks if a profile dies first."""
    global _profile
    if _profile is None:
        _profile = QWebEngineProfile(QApplication.instance())
        _profile.setHttpCacheType(QWebEngineProfile.NoCache)
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        s = _profile.settings()
        s.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
        s.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, False)
        s.setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, False)
        s.setAttribute(QWebEngineSettings.PluginsEnabled, False)
        s.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)
    return _profile


def view_label(home_url):
    """A short name for a view in the crash trail, from its page's folder:
    pages/buddy_network/web/index.html -> "buddy_network",
    web/shell/header/index.html -> "shell/header"."""
    parts = home_url.toLocalFile().replace("\\", "/").split("/")
    if "web" not in parts:
        return parts[-1]
    i = len(parts) - 1 - parts[::-1].index("web")
    after = parts[i + 1:-1]
    return "/".join(after if parts[i - 1] == "app" else [parts[i - 1], *after]) or parts[-1]


class _Page(QWebEnginePage):
    """Keeps the view on its own file: links to the web open in the user's
    browser, everything else is refused."""

    def __init__(self, home_url, parent):
        super().__init__(_shared_profile(), parent)
        self._home = home_url
        self.label = view_label(home_url)

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if url.scheme() in EXTERNAL_SCHEMES:
            QDesktopServices.openUrl(url)
            return False
        if not is_main_frame:
            return False
        # The page's own file (a load or reload) and nothing else.
        return url.isLocalFile() and url.toLocalFile() == self._home.toLocalFile()

    def createWindow(self, _type):
        return None

    def javaScriptConsoleMessage(self, level, message, line, source):
        if level != QWebEnginePage.InfoMessageLevel or os.environ.get("BUDDY_WEB_DEBUG"):
            where = os.path.basename(QUrl(source).path()) or source
            crash_log.trail("console", f"{self.label} {where}:{line} {message[:160]}")
            print(f"[web {where}:{line}] {message}", file=sys.stderr)


class _Bridge(QObject):
    """The one object the page's JS sees (as `backend`). Deliberately
    generic - two members - so a tool's protocol lives in its own page
    class, not in a growing list of typed slots here."""

    event = Signal(str, str)

    def __init__(self, page):
        # A child of the view's widget, so Qt destroys it with the window
        # (on the GUI thread) rather than leaving Python to.
        super().__init__(page)
        # Weak: the page holds this bridge, and a strong reference back made
        # the pair a reference cycle - a closed dialog's whole web view then
        # waited for Python's cycle collector, which destroyed it on
        # whatever thread it ran in. Chromium can't take that (Buddy
        # crashed in QtWebEngine). Now it goes when the dialog does.
        self._page = weakref.ref(page)

    @Slot()
    def ready(self):
        page = self._page()
        if page is not None:
            page._js_ready()

    @Slot(str, str)
    def send(self, name, payload):
        try:
            data = json.loads(payload) if payload else None
        except ValueError:
            data = None
        page = self._page()
        if page is None:
            return
        # Next turn of the event loop, never inside QWebChannel's handler.
        page_ref = self._page
        QTimer.singleShot(0, lambda: (p := page_ref()) is not None and p._dispatch(name, data))


class _FileDropFilter(QObject):
    """Catches files dragged onto the view from Explorer. The page's JS
    can't get at a dropped file's path (browsers hide it), so drops are
    taken on the Qt side and handed to the page as paths."""

    def __init__(self, page):
        super().__init__(page)
        self._page = page

    @staticmethod
    def _paths(event):
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in (QEvent.DragEnter, QEvent.DragMove):
            if self._paths(event):
                event.acceptProposedAction()
                if kind == QEvent.DragEnter:
                    self._page.emit("drop_hover", True)
                return True
        elif kind == QEvent.DragLeave:
            self._page.emit("drop_hover", False)
        elif kind == QEvent.Drop:
            paths = self._paths(event)
            if paths:
                event.acceptProposedAction()
                self._page.emit("drop_hover", False)
                QTimer.singleShot(0, lambda: self._page.on_files_dropped(paths))
                return True
        return False


class WebSurface:
    """The web view and its channel, for a QWidget subclass. Set `web_dir`
    (a folder holding `web_file`) and implement web_ready() plus an
    on_<action>(payload) method per JS action. `self.host` gives the theme
    (theme_tokens(), shared_settings).

    Set `file_drops = True` to take files dragged in from Explorer:
    on_files_dropped(paths) is called, and the view gets "drop_hover"
    (true/false) while a drag is over it."""

    web_dir = None
    web_file = "index.html"
    file_drops = False
    # Transparent, so a tool sits on whatever the shell draws behind it
    # (Modern's gradient, Resolve's window grey) like a Qt page does. A
    # window of its own has nothing behind it: its page paints --page-bg.
    transparent = True

    def _build_web(self):
        """Builds the view into this widget, runs build_state() and loads
        the page."""
        self._ready = False
        self._queue = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = QWebEngineView(self)
        home = QUrl.fromLocalFile(os.path.join(self.web_dir, self.web_file))
        page = _Page(home, self.view)
        if self.transparent:
            page.setBackgroundColor(QColor(Qt.transparent))
        else:
            # The theme's colour until the page paints, not a white flash.
            try:
                page.setBackgroundColor(QColor(self.host.theme_tokens()["surface"]))
            except Exception:  # noqa: BLE001 - a host without a theme
                pass
        self.view.setPage(page)
        self._channel = QWebChannel(page)
        self._bridge = _Bridge(self)
        self._channel.registerObject("backend", self._bridge)
        page.setWebChannel(self._channel)
        page.loadStarted.connect(self._on_load_started)
        page.renderProcessTerminated.connect(self._on_renderer_died)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._edit_menu)
        layout.addWidget(self.view)
        if self.file_drops:
            # Chromium's own widget (the focus proxy) is what receives the
            # drag, and it's made when the page first loads.
            self._drop_filter = _FileDropFilter(self)
            self.view.installEventFilter(self._drop_filter)
            page.loadFinished.connect(self._watch_drops)

        # A new language: this view gets its strings and redraws in it.
        # A bound method of a QObject, so Qt drops the connection with it.
        get_i18n().language_changed.connect(self._on_language_changed)

        self.build_state()
        page.load(home)

    def _watch_drops(self, _ok=True):
        proxy = self.view.focusProxy()
        if proxy is not None:
            proxy.installEventFilter(self._drop_filter)

    def on_files_dropped(self, paths):
        """Files dragged in from Explorer (see file_drops)."""

    # ---------------------------------------------------------- overrides --

    def build_state(self):
        """Set up the page's Python-side state. Runs before the HTML loads;
        don't emit() from here - web_ready() is where the view gets it."""

    def web_ready(self):
        """The page's JS has connected. Push everything it should show."""

    def on_language_changed(self):
        """The language changed and the page has its new strings. For a
        page that caches text in Python it doesn't send as English."""

    def theme_vars(self, tokens):
        """Extra CSS variables (names without "--") for this page only."""
        return {}

    def on_theme_changed(self):
        self._push_theme()

    # --------------------------------------------------------------- API --

    def emit(self, name, payload=None):
        message = (name, json.dumps(payload))
        crash_log.trail("emit", f"{self._trail_label()} {name} {len(message[1])}B{'' if self._ready else ' (queued)'}")
        if self._ready:
            self._bridge.event.emit(*message)
        else:
            self._queue.append(message)

    # ---------------------------------------------------------- plumbing --

    def _push_theme(self):
        host = self.host
        settings = getattr(host, "shared_settings", None) or {}
        tokens = host.theme_tokens()
        theme = web_theme(settings.get("theme", "Default"), settings.get("subtheme"), tokens)
        theme["vars"].update(self.theme_vars(tokens))
        theme["common"] = QUrl.fromLocalFile(WEB_COMMON_DIR + os.sep).toString()
        self.emit("theme", theme)

    def _push_i18n(self):
        language = get_i18n().language
        self.emit("i18n", {"language": language, "lang": LANGUAGE_CODES.get(language, "en"),
                           "strings": web_strings(language)})

    def _on_language_changed(self, _language=None):
        title = getattr(self, "_english_title", None)
        if title:
            self.setWindowTitle(tr(title))
        self._push_i18n()
        self.on_language_changed()

    def _trail_label(self):
        view = getattr(self, "view", None)
        return getattr(view.page(), "label", type(self).__name__) if view is not None else type(self).__name__

    def _on_load_started(self):
        crash_log.trail("load", self._trail_label())
        self._ready = False
        self._queue = []

    def _js_ready(self):
        # Strings before anything is drawn, so nothing shows in English first.
        self._push_i18n()
        self._push_theme()
        self.web_ready()
        self._ready = True
        queued, self._queue = self._queue, []
        for message in queued:
            self._bridge.event.emit(*message)
        self._page_ready()

    def _page_ready(self):
        """The page has its theme and state (see _ReadyToShow)."""

    def _dispatch(self, name, data):
        crash_log.trail("action", f"{self._trail_label()} {name}")
        handler = getattr(self, f"on_{name}", None)
        if handler is None or not name.isidentifier():
            print(f"[web] {type(self).__name__} has no handler for {name!r}", file=sys.stderr)
            return
        handler(data)

    def _on_renderer_died(self, _status, _code):
        crash_log.trail("renderer", f"{self._trail_label()} died (status {_status}, exit {_code})")
        # The Chromium renderer is a separate process; if it dies the view
        # goes blank. Reloading brings it back, and web_ready() redraws
        # everything from the Python-side state.
        QTimer.singleShot(500, self.view.reload)

    def _edit_menu(self, pos):
        """Cut/copy/paste/select all - the browser's own menu without Back,
        Reload or View Source, which mean nothing inside Buddy."""
        menu = self.view.createStandardContextMenu()
        page = self.view.page()
        keep = {page.action(a) for a in (
            QWebEnginePage.Undo, QWebEnginePage.Redo, QWebEnginePage.Cut, QWebEnginePage.Copy,
            QWebEnginePage.Paste, QWebEnginePage.SelectAll, QWebEnginePage.CopyLinkToClipboard,
        )}
        for action in menu.actions():
            if action not in keep and not action.isSeparator():
                menu.removeAction(action)
        if any(not a.isSeparator() for a in menu.actions()):
            menu.exec(self.view.mapToGlobal(pos))
        menu.deleteLater()


class WebToolPage(WebSurface, ToolPage):
    """A tool page in the shell, drawn by pages/<tool>/web/index.html."""

    def build_ui(self):
        self._build_web()


class _ReadyToShow:
    """A web window appears only once its page has loaded and themed
    itself - "ready to show", as Electron calls it. Shown straight away, a
    new window is on screen before Chromium has drawn anything, and Windows
    fills it white for a frame or two: a jarring flash on a dark theme. The
    page loads while the window is hidden; show() and exec() wait for it,
    READY_TIMEOUT_MS at most, so a page that never connects still opens."""

    _show_pending = False
    _ready_loop = None

    def _page_ready(self):
        if self._ready_loop is not None:
            QTimer.singleShot(READY_SETTLE_MS, self._ready_loop.quit)
        if self._show_pending:
            QTimer.singleShot(READY_SETTLE_MS, self._show_now)

    def _wait_until_ready(self):
        """Blocks (running the event loop) until the page is ready."""
        if self._ready:
            return
        self._ready_loop = QEventLoop()
        QTimer.singleShot(READY_TIMEOUT_MS, self._ready_loop.quit)
        self._ready_loop.exec()
        self._ready_loop = None

    def show(self):
        # Only a window of its own waits; a view laid out inside the shell
        # (the header, the rail) shows with its parent as usual.
        if self.isWindow() and not self._ready:
            if not self._show_pending:
                self._show_pending = True
                QTimer.singleShot(READY_TIMEOUT_MS, self._show_now)
            return
        super().show()

    def _show_now(self):
        if self._show_pending:
            self._show_pending = False
            super().show()
            self.raise_()


class WebWindow(_ReadyToShow, WebSurface, QWidget):
    """A web page in a window of its own (no Qt parent, so it isn't
    minimized with Buddy). host: what gives it the theme - the shell."""

    transparent = False

    def __init__(self, host):
        super().__init__()
        self.host = host
        self._build_web()


class WebDialog(_ReadyToShow, WebSurface, QDialog):
    """A modal window drawn by a web page - exec() it. host gives the theme
    (the shell); parent is the window it sits over."""

    transparent = False

    def __init__(self, host, parent=None, title="", size=(480, 360)):
        super().__init__(parent)
        self.host = host
        self._english_title = title
        self.setWindowTitle(tr(title))
        self.resize(*size)
        self._build_web()

    def exec(self):
        self._wait_until_ready()
        return super().exec()


def _theme_host(widget):
    """The shell (what gives a window its theme) from any widget in it."""
    node = widget
    while node is not None:
        if hasattr(node, "theme_tokens") and hasattr(node, "shared_settings"):
            return node
        node = getattr(node, "host", None) or node.parentWidget()
    return None
