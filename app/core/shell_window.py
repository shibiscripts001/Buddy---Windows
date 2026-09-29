#!/usr/bin/env python3
"""
Buddy's main window: one header, one shared Resolve connection row, one
grouped nav rail, one QStackedWidget of tool pages, one Settings dialog -
all the chrome every standalone tool used to duplicate per-app, now owned
exactly once. Modeled directly on Project Setup's existing MainWindow (nav rail + QStackedWidget across its 4 tabs), just
scaled from 4 sibling tabs to N independent tool pages grouped by
category.

The header and the rail are web views (core/shell_web.py) and Settings,
Organize sidebar and the announcements are web windows; this window lays
them out around the panes and keeps everything they show. What's left in
Qt is the frame itself: the panes, the dual-view divider and its tint,
the busy overlay and the tray.
"""

import traceback

from PySide6.QtCore import QEvent, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPainterPath, QPixmap, QRegion
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QStackedWidget, QScrollArea,
    QApplication, QMenu, QSystemTrayIcon, QSplitter, QSplitterHandle,
)

from core.theme import (
    DEFAULT_SIDE_PANE_TINT,
    custom_defaults,
    get_app_theme,
    get_shape_tokens,
    get_theme_tokens,
    pane_radius,
    resolve as resolve_theme,
    side_pane_tint,
    theme_layout,
)
from core import desktop_layout
from core import nav_layout
from core.announcements_window import AnnouncementsDialog
from core.announcements import SEEN_KEY, AnnouncementChecker
from core.buddy_server import DEFAULT_SERVER_URL
from core.bug_report import BugReportDialog
from core.nav_organizer import NavOrganizerDialog
from core.settings_store import SharedSettings, ToolSettings
from core.resolve_bridge import (
    connect as resolve_connect,
    ResolveConnectionError,
    _PROBE_TIMEOUT_SECONDS,
)
from core.busy_overlay import BusyOverlay
from core.desk_web import DeskMenu, TaskbarView
from core.desktop_window import DesktopArea
from core import crash_log
from core.i18n import get_i18n, tr
from core.settings_dialog import SettingsDialog
from core.shell_web import HeaderView, RailView
from core.message_dialog import alert

# Widest each pane grows in dual view, so two tools side by side on a big
# monitor stay near each other; past that the space is left empty on the
# right. One tool on its own takes the whole width, however wide the
# window is made.
PANE_MAX_WIDTH = 1280
QWIDGETSIZE_MAX = (1 << 24) - 1   # Qt's "no maximum" (PySide6 doesn't export it)
# Gap between the two panes in dual view - the same as between rail and page.
PANE_GAP = 16


def _generate_window_icon(accent_hex):
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(accent_hex))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(8, 8, 48, 48, 14, 14)
    painter.end()
    return QIcon(pixmap)


class _PaneStack(QStackedWidget):
    """A stack sized by the page on show. A plain QStackedWidget asks for
    the largest minimum of EVERY page it holds, and the left pane holds all
    the pages not on show - so a narrow tool in dual view scrolled sideways
    for Asset Manager's sake."""

    def __init__(self):
        super().__init__()
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()


class _PaneHandle(QSplitterHandle):
    """The gap between the two panes: nothing but the window behind them,
    so they read as two separate windows - until the pointer is on it, when
    a short grip in the accent shows that's where it's dragged from."""

    def __init__(self, orientation, splitter):
        super().__init__(orientation, splitter)
        self.setAttribute(Qt.WA_Hover, True)

    def paintEvent(self, _event):
        if not (self.underMouse() or self.isSliderDown()):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.splitter().hover_color))
        grip = min(56, self.height() // 3)
        painter.drawRoundedRect(self.width() // 2 - 2, (self.height() - grip) // 2, 4, grip, 2, 2)
        painter.end()

    def isSliderDown(self):
        return QApplication.mouseButtons() & Qt.LeftButton and self.underMouse()

    def event(self, event):
        if event.type() in (QEvent.HoverEnter, QEvent.HoverLeave):
            self.update()
        return super().event(event)


class _PaneSplitter(QSplitter):
    line_color = "#000000"
    hover_color = "#000000"

    def createHandle(self):
        return _PaneHandle(self.orientation(), self)

    def set_line_colors(self, line, hover):
        self.line_color, self.hover_color = line, hover
        for i in range(self.count()):
            self.handle(i).update()


class _TintOverlay(QWidget):
    """A see-through wash over the right-hand pane (Settings > Second pane
    in dual view). Clicks, hovers and scrolls pass straight through to the
    tool underneath; it only changes what the pane looks like."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._color = None
        self._radius = 0
        parent.installEventFilter(self)

    def set_tint(self, tint, radius=0):
        """(colour, alpha) from theme.side_pane_tint(), or None for none;
        `radius` is the pane card's, so the wash keeps to its corners."""
        self._radius = radius
        if tint is None:
            self._color = None
            self.hide()
            return
        self._color = QColor(tint[0])
        self._color.setAlpha(tint[1])
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self.update()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
            self.raise_()
        return False

    def paintEvent(self, _event):
        if self._color is not None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._color)
            # Inside the pane card's border, so the wash never hides it.
            r = max(0, self._radius - 1)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), r, r)
            painter.end()


class _PaneCard(QScrollArea):
    """A pane's card (theme._pane_rules gives it its border/background/
    radius as a stylesheet). QSS border-radius only clips what Qt itself
    paints - the page inside is a QWebEngineView, which composites its own
    corners square regardless, so under an opaque theme it takes an exact
    padding inset (theme._curve_inset) to keep them just out of sight. That
    inset only works because the page happens to paint the same colour the
    card does; a "glass" theme's page is see-through instead (so the card's
    background/gradient shows through it), and there the WebEngineView's
    real square corners can show past the curve as a hard edge. A real mask
    is exact regardless of what the page underneath paints or matches."""

    def __init__(self):
        super().__init__()
        self._radius = 0

    def set_radius(self, radius):
        self._radius = radius
        self._clip_to_shape()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clip_to_shape()

    def _clip_to_shape(self):
        if self._radius <= 0:
            self.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self._radius, self._radius)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))


class _ResolveConnectWorker(QThread):
    """Runs resolve_connect() (an 8s-timeout subprocess probe, then the
    in-process bootstrap) off the UI thread, so a slow or absent Resolve
    doesn't freeze the whole shell at startup or on Reconnect."""
    done = Signal(object, object)  # controller or None, error or None

    def run(self):
        try:
            self.done.emit(resolve_connect(), None)
        except Exception as exc:
            self.done.emit(None, exc)


class ShellWindow(QMainWindow):
    def __init__(self, app, registry):
        """`registry` is the list of (category, page_cls) tool entries
        from registry.py, in the order they should appear grouped in the
        nav rail."""
        super().__init__()
        self.app = app
        # The desktop layout's tool windows are native (core/desktop_window.py);
        # without this, making them native would make every widget beside
        # them native too.
        QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings, True)
        self.registry = registry
        self.shared_settings = SharedSettings()
        # Before any page is built, so each draws in it from the start.
        get_i18n().language = self.shared_settings.get("language", "English")
        get_i18n().language_changed.connect(self._on_language_changed)
        self.controller = None
        self.connected = False
        self._tool_settings_cache = {}
        # Dual view: a second tool beside the one picked in the rail.
        self._split_on = False
        self._side_tool_id = None
        self._split_ratio = 0.5   # the left pane's share of the width
        # "panes" (the rail's tool, and dual view) or "desktop" (floating
        # windows) - the theme's choice (core/theme.py theme_layout).
        self._layout = "panes"
        self.tray_icon = None
        self._quitting = False
        self._pages_told_quitting = False

        self.setWindowTitle("Buddy")
        self.resize(1180, 760)
        # 1060, not the old 900: Asset Manager (nav rail + three views +
        # preview panel) needs a real floor. Below this its toolbars wrap
        # onto extra rows, which is fine, but 900 forced them to clip
        # outright. _center_on_screen() still lowers this minimum to fit a
        # smaller display when it has to.
        self.setMinimumSize(1060, 600)
        self.setWindowFlag(
            Qt.WindowStaysOnTopHint, self.shared_settings.get("stay_on_top", False)
        )

        self.pages = {}
        self._connect_worker = None
        self._init_ui()
        # The orb next to "Buddy" (core/announcements.py): on by default,
        # off with one checkbox in Settings.
        self.announcements = AnnouncementChecker(self.shared_settings, self._network_server_url, self)
        self.announcements.changed.connect(self.push_header)
        # closeEvent isn't the only way out: app.quit() from elsewhere skips
        # it, and at Windows logoff/shutdown Qt 6 closes no windows at all -
        # it only emits commitDataRequest (session ending) and then
        # aboutToQuit. Either of those still has to flush pages (Time
        # Tracker's open entry especially); _notify_pages_quitting() makes
        # sure that happens once, whichever path gets there first.
        app.aboutToQuit.connect(self._notify_pages_quitting)
        # A QThread destroyed while running crashes the exit.
        app.aboutToQuit.connect(self._shutdown_connect_worker)
        # Where the desktop's windows were is saved a moment after each
        # change; these write it straight away instead.
        app.aboutToQuit.connect(self._save_desk)
        if hasattr(app, "commitDataRequest"):
            app.commitDataRequest.connect(self._on_commit_data_request)
            app.commitDataRequest.connect(lambda _manager: self._save_desk())
        self._setup_tray_icon()
        self.apply_theme()
        self._center_on_screen()
        self._connect(silent=True)
        self.announcements.start()
        load_warnings = getattr(self.shared_settings, "load_warnings", None)
        if load_warnings:
            alert(self, "Settings", "\n\n".join(load_warnings))

    # ------------------------------------------------------------ ShellHost API --
    def ensure_connected(self):
        """Pages call this right before doing actual Resolve work, so unlike
        _connect() (startup/Reconnect, which run off-thread to avoid
        freezing the whole shell) this has to block and return a controller
        or raise - there's no useful "keep going" for the caller otherwise."""
        if not self.controller:
            try:
                self.controller = resolve_connect()
                self._set_connected(True)
            except Exception:
                self.controller = None
                self._set_connected(False)
        if not self.controller:
            raise ResolveConnectionError(
                "Could not reach DaVinci Resolve.\nMake sure Resolve is running with a project open."
            )
        return self.controller

    def set_busy(self, is_busy, message=None):
        if is_busy:
            self.busy_overlay.start(message or "Working…")
            QApplication.processEvents()
        else:
            self.busy_overlay.stop()

    def pump_busy(self, message=None):
        if message is not None:
            self.busy_overlay.set_message(message)
        self.busy_overlay.pump()

    def set_busy_message(self, message):
        """Updates the busy overlay caption WITHOUT pumping the event loop.

        For queued worker-signal handlers (e.g. Project Setup's Align tab
        progress callbacks): the loop is already running there and pumping
        from inside a queued handler would deliver the worker's LATER
        signals re-entrantly, in the middle of handling this one - the
        same reason the standalone's update_busy takes pump=False."""
        self.busy_overlay.set_message(message)

    def tool_settings(self, tool_id, defaults=None):
        if tool_id not in self._tool_settings_cache:
            self._tool_settings_cache[tool_id] = ToolSettings(tool_id, defaults)
        return self._tool_settings_cache[tool_id]

    def custom_pair(self):
        """(accent, background, panel) for Custom under the ACTIVE theme.

        Falls back to that theme's own palette rather than a global default.
        The panel colour defaults to None, not to a colour: that is what
        tells theme.py to derive it, so someone who never touches the third
        swatch keeps exactly the palette they had.
        """
        theme = self.shared_settings.get("theme", "Default")
        accent_default, bg_default = custom_defaults(theme)
        return (
            self.shared_settings.get(f"accent_{theme}", accent_default),
            self.shared_settings.get(f"background_{theme}", bg_default),
            self.shared_settings.get(f"panel_{theme}", "") or None,
        )

    def theme_tokens(self):
        accent, background, panel = self.custom_pair()
        return get_theme_tokens(
            self.shared_settings.get("theme", "Default"),
            self.shared_settings.get("subtheme"),
            accent,
            background,
            panel,
        )

    # ------------------------------------------------------------ theme --
    def apply_theme(self):
        tokens = self.theme_tokens()
        accent, background, panel = self.custom_pair()
        stylesheet = get_app_theme(
            self.shared_settings.get("theme", "Default"),
            self.shared_settings.get("subtheme"),
            custom_accent=accent,
            custom_bg=background,
            custom_panel=panel,
        )
        self.app.setStyleSheet(stylesheet)
        theme, _subtheme = resolve_theme(self.shared_settings.get("theme", "Default"),
                                         self.shared_settings.get("subtheme"))
        self.desktop.set_theme(tokens, get_shape_tokens(theme))
        self._apply_layout(theme_layout(theme))
        self._relayout_after_theme_change()
        icon = _generate_window_icon(tokens["primary"])
        self.setWindowIcon(icon)
        if self.tray_icon is not None:
            self.tray_icon.setIcon(icon)
        self.busy_overlay.set_accent(tokens["primary"])
        self.busy_overlay.set_text_color(tokens["overlay_text"])
        self.header.on_theme_changed()
        self.rail.on_theme_changed()
        self.taskbar.on_theme_changed()
        if self.desk_menu is not None:
            self.desk_menu.on_theme_changed()
        # "outline", not the fainter divider colour: that one is near-black
        # in Resolve and all but vanishes on Modern's gradient backdrop.
        self.panes.set_line_colors(tokens["outline"], tokens["primary"])
        radius_px = pane_radius(theme)
        self.main_pane.set_radius(radius_px)
        self.side_pane.set_radius(radius_px)
        self._side_tint.set_tint(side_pane_tint(
            tokens, self.shared_settings.get("split_tint", DEFAULT_SIDE_PANE_TINT)), radius_px)
        for page in self.pages.values():
            page.on_theme_changed()

    def _relayout_after_theme_change(self):
        """Recompute every widget's size after a stylesheet swap.

        A theme can change the font, and Qt does not invalidate cached size
        hints when a stylesheet does that - widgets keep the width they were
        given under the old metrics and clip the new text. Verdana is about
        25% wider than Segoe UI at the same pixel size, which is enough to
        turn "Change..." into "hange." on a dialog that was already open.
        """
        for widget in self.app.allWidgets():
            widget.updateGeometry()
        for window in self.app.topLevelWidgets():
            layout = window.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            # Grow open dialogs to fit; never resize the main window, whose
            # size is the user's own choice.
            if window is not self and window.isVisible():
                window.adjustSize()

    def apply_stay_on_top(self, enabled):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.show()

    # ------------------------------------------------------------ tray --
    # Buddy itself owns tray/background-survival behavior at the shell
    # level, not per-tool - see pages/time_tracker/page.py's own module
    # docstring for why: a background tool like Time Tracker needs to keep
    # polling/recording even with the window closed, and closing one page's
    # view was never a reason for the other eleven tools to stop being
    # reachable either. Ported from Davinci Time Tracker's own
    # MainWindow._setup_tray_icon/closeEvent, generalized to the whole shell.
    def _setup_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return  # rare, but don't crash the app over a missing tray

        self.tray_icon = QSystemTrayIcon(self)
        # An icon before show() below - apply_theme() (right after this)
        # repaints it, but showing an icon-less tray icon first made Qt warn
        # "QSystemTrayIcon::setVisible: No Icon set" on every launch.
        self.tray_icon.setIcon(_generate_window_icon(self.theme_tokens()["primary"]))
        self.tray_icon.setToolTip("Buddy")
        self.tray_icon.activated.connect(self._handle_tray_activated)

        menu = QMenu()
        self.tray_show_action = QAction(tr("Hide window"), menu)
        self.tray_show_action.triggered.connect(self._toggle_window_visible)
        menu.addAction(self.tray_show_action)

        menu.addSeparator()
        self.tray_quit_action = QAction(tr("Quit"), menu)
        self.tray_quit_action.triggered.connect(self._quit_app)
        menu.addAction(self.tray_quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.show()

    def _handle_tray_activated(self, reason):
        # Trigger = a single left-click on Windows; double-click also fires
        # Trigger there (Windows doesn't distinguish tray double-clicks the
        # way DoubleClick suggests), so this alone covers the normal click.
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._toggle_window_visible()

    def _toggle_window_visible(self):
        # A minimised window still counts as visible to Qt - hiding it on
        # that click left the user no way back but a second click.
        if self.isVisible() and not self.isMinimized():
            self.hide()
            self._update_tray_show_action()
        else:
            self.bring_to_front()

    def bring_to_front(self):
        """Shows, un-minimises and focuses the window. Clearing only the
        minimised flag (not showNormal()) keeps a maximised window maximised.
        Also used by core/single_instance.py when Buddy is launched again."""
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        self._update_tray_show_action()

    # ------------------------------------------------------ announcements --
    def _network_server_url(self) -> str:
        # Buddy Network's own address setting, so a test server is used for both.
        page = self.pages.get("buddy_network")
        url = page.settings.get("server_url") if page is not None else None
        return url or DEFAULT_SERVER_URL

    def open_announcements(self):
        seen = self.shared_settings.get(SEEN_KEY, 0)
        AnnouncementsDialog(self, self.announcements.items, seen).exec()
        self.announcements.mark_seen()

    def set_announcements_enabled(self, on: bool):
        """Settings' "Show announcements from Buddy" checkbox."""
        self.announcements.set_enabled(on)

    def check_announcements_now(self):
        """Buddy Network's Admin panel, right after an admin posts one."""
        self.announcements.check_now()

    # --------------------------------------------------------- bug report --
    def open_bug_report(self):
        """The bug button in the header or the taskbar (core/bug_report.py)."""
        dialog = BugReportDialog(self)
        dialog.exec()
        dialog.deleteLater()   # a child of this window: freed, like Settings

    def network_server_url(self) -> str:
        return self._network_server_url()

    def network_connection(self):
        """(client, me): Buddy Network's connection while it's signed in with
        a name - a bug report goes over it, saying who sent it - else (None, None)."""
        page = self.pages.get("buddy_network")
        client, me = getattr(page, "client", None), getattr(page, "me", None)
        if client is not None and client.state == "online" and me and me.get("name"):
            return client, me
        return None, None

    def pages_on_screen(self) -> list:
        """The tools in view: the one in front - and in dual view, the one beside it."""
        if self._layout == "desktop":
            front = self.desktop.front()
            return [self.pages[front]] if front in self.pages else []
        shown = [self.stack.currentWidget()]
        if self._split_on and self._side_tool_id in self.pages:
            shown.append(self.pages[self._side_tool_id])
        return [page for page in shown if getattr(page, "display_name", None)]

    def notify(self, title: str, message: str):
        """A tray balloon (Windows notification) - see ShellHost in pages/base.py."""
        if self.tray_icon is not None:
            self.tray_icon.showMessage(tr(title), tr(message), QSystemTrayIcon.Information, 5000)

    def _update_tray_show_action(self):
        if self.tray_icon is not None:
            visible = self.isVisible() and not self.isMinimized()
            self.tray_show_action.setText(tr("Hide window" if visible else "Show window"))

    def _on_language_changed(self, _language=None):
        """Settings' Language: the web views redraw themselves
        (core/web_page.py); the tray menu is Qt's."""
        if self.tray_icon is not None:
            self._update_tray_show_action()
            self.tray_quit_action.setText(tr("Quit"))

    def changeEvent(self, event):
        # Minimising/restoring from the taskbar changes what the tray's
        # Show/Hide entry should say.
        if event.type() == QEvent.WindowStateChange:
            self._update_tray_show_action()
        super().changeEvent(event)

    def _quit_app(self):
        """The one real, full shutdown path - used by the tray menu's own
        Quit action. self.close() re-enters closeEvent below, which (since
        _quitting is now True) skips the minimize-to-tray branch, gives
        every page a chance to flush background state via on_app_quitting(),
        and accepts the close - this then calls app.quit() itself."""
        self._quitting = True
        self.close()
        self.app.quit()

    def closeEvent(self, event):
        # With a tray icon AND "keep running in tray" on, the window's own
        # [X] should minimize to tray (background tools like Time Tracker
        # keep running - their own timers are independent of window
        # visibility) rather than quit the whole app. Bypassed by
        # _quitting (the tray menu's own Quit action) and by the setting
        # itself being off, in which case the window's own [X] behaves like
        # a normal application's: it really quits.
        keep_in_tray = self.tray_icon is not None and self.shared_settings.get("keep_running_in_tray", True)
        if keep_in_tray and not self._quitting:
            event.ignore()
            self.hide()
            self._update_tray_show_action()
            if not self.shared_settings.get("_tray_notice_shown", False):
                self.tray_icon.showMessage(
                    tr("Still running"),
                    tr("Buddy is still running in the background. Right-click the tray icon to "
                       "reopen or quit."),
                    QSystemTrayIcon.Information, 5000,
                )
                self.shared_settings["_tray_notice_shown"] = True
                self.shared_settings.save()
            return

        self._notify_pages_quitting()
        event.accept()

        if not self._quitting:
            # Reached via the window's own [X] with "keep running in tray"
            # OFF (or no tray icon at all) - not via _quit_app(), which
            # already calls app.quit() itself right after self.close()
            # returns. quitOnLastWindowClosed(False) means Qt won't do
            # this automatically once a tray icon exists, so it has to
            # happen explicitly here instead.
            self.app.quit()

    def _notify_pages_quitting(self):
        """Calls every page's on_app_quitting() - at most once per run, since
        a normal quit goes through closeEvent AND aboutToQuit, and a session
        end through commitDataRequest AND aboutToQuit. One page raising must
        not stop the rest from flushing."""
        if self._pages_told_quitting:
            return
        self._pages_told_quitting = True
        for page in self.pages.values():
            try:
                page.on_app_quitting()
            except Exception:
                traceback.print_exc()

    def _on_commit_data_request(self, _session_manager):
        # Windows is logging off/shutting down. Flush now rather than
        # waiting for aboutToQuit: the process may be ended before the
        # event loop ever gets that far.
        self._notify_pages_quitting()

    # ------------------------------------------------------------ UI --
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        self._root = root
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        self.header_slot = QWidget()
        self.header_slot.setFixedHeight(64)
        root.addWidget(self.header_slot)
        self.header = HeaderView(self, self.header_slot)

        body = QHBoxLayout()
        body.setSpacing(16)

        self.rail = RailView(self)

        # The rail's tool on the left; in dual view, a second one on the
        # right (side_stack). A page lives in whichever stack shows it and
        # moves between them as the choice changes (_place_page); every
        # page not on show waits in the left-hand stack.
        self.stack = _PaneStack()
        self.side_stack = _PaneStack()

        # Every page is built, visible in the rail or not - hiding a tab
        # must not stop a background tool (Time Tracker) from running.
        for _category, page_cls in self.registry:
            page = page_cls(self)
            self.pages[page_cls.tool_id] = page
            self.stack.addWidget(page)

        self.panes = _PaneSplitter(Qt.Horizontal)
        self.panes.setObjectName("paneSplitter")   # never a card itself (theme._pane_rules)
        self.panes.setChildrenCollapsible(False)
        self.panes.setHandleWidth(PANE_GAP)
        self.panes.splitterMoved.connect(self._on_divider_dragged)
        self.main_pane = self._pane_scroller(self.stack)
        self.panes.addWidget(self.main_pane)
        self.side_pane = self._pane_scroller(self.side_stack)
        self.panes.addWidget(self.side_pane)
        self.side_pane.setVisible(False)
        self._side_tint = _TintOverlay(self.side_pane)

        # The desktop layout's desk (a theme with "layout": "desktop"): the
        # same pages, in floating windows. Hidden under every other theme.
        self.desktop = DesktopArea(
            central, self.pages, {tid: page.display_name for tid, page in self.pages.items()},
            self._stash_page, self._on_desk_changed)
        self.desktop.load(self.shared_settings.get(desktop_layout.SETTINGS_KEY))
        self._desk_save_timer = QTimer(self, singleShot=True, interval=500)
        self._desk_save_timer.timeout.connect(self._save_desk)

        self._current_tool_id = None
        self.rebuild_nav()
        body.addWidget(self.rail)
        # The width cap goes on a plain holder, not the splitter itself:
        # Qt's stylesheet polish resets a QSplitter's maximum width on
        # show and on every theme change.
        self._panes_holder = QWidget()
        holder_layout = QHBoxLayout(self._panes_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(self.panes)
        body.addWidget(self._panes_holder, stretch=1)
        # The desk has no width cap: windows are the user's to place.
        body.addWidget(self.desktop, stretch=1)
        # In dual view, whatever is left past the panes' cap stays empty, on the right.
        body.addStretch(0)
        root.addLayout(body, stretch=1)
        # The desktop layout's taskbar, in place of the header and the rail;
        # its popup menu is made the first time the desktop is.
        self.pins = desktop_layout.load_pins(self.shared_settings.get(desktop_layout.PINS_KEY), set(self.pages))
        self.taskbar = TaskbarView(self)
        root.addWidget(self.taskbar)
        self.taskbar.hide()
        self.desk_menu = None
        self._fit_panes()

        # Before the first switch_tool() below, which calls the starting
        # page's on_shown() - a page is entitled to use host.set_busy()
        # there (pages/base.py documents it), and the overlay has to exist
        # by then or the very first page in the registry crashes the app.
        self.busy_overlay = BusyOverlay(central)
        # Native, so it can cover the desktop layout's native tool windows.
        self.busy_overlay.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.busy_overlay.setAttribute(Qt.WA_NativeWindow, True)

        visible = nav_layout.visible_tool_ids(self.nav_layout)
        if visible:
            self.switch_tool(visible[0])
        if self.shared_settings.get("split_view", False):
            self.set_split_view(True)

    @staticmethod
    def _pane_scroller(stack):
        """Wraps a pane's stack so a page squeezed below its own minimum
        size (two wide tools side by side on a small screen) scrolls
        instead of pushing the window past the edge of the monitor."""
        scroller = _PaneCard()
        # Each pane is a card of its own (theme._pane_rules).
        scroller.setObjectName("paneCard")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.NoFrame)
        scroller.setWidget(stack)
        return scroller

    # ------------------------------------------------------------ dual view --
    def set_split_view(self, on):
        """The header's dual-view toggle. Remembered across launches, and
        so is the tool last picked for the right-hand side."""
        on = bool(on)
        if on != self.shared_settings.get("split_view", False):
            self.shared_settings["split_view"] = on
            self.shared_settings.save()
        self._split_on = on
        self._show_side(self.shared_settings.get("split_tool") if on else None)
        self.side_pane.setVisible(on)
        self._fit_panes()
        if on:
            # Even halves each time it opens, not wherever the divider was
            # last dragged to. Balanced once the new page has been laid
            # out, since its size isn't final until then.
            self._split_ratio = 0.5
            self._balance_panes()
            QTimer.singleShot(0, self._balance_panes)

    def _on_divider_dragged(self, _pos, _index):
        left, right = self.panes.sizes()
        if left + right > 0:
            self._split_ratio = left / (left + right)

    def _balance_panes(self):
        """Puts the divider where the user last dragged it (halfway by
        default), moved just enough that neither tool is narrower than it
        needs - Time Tracker won't fit in half a 1920 screen - when the
        other side has the room to give. When both can't fit, the squeezed
        side scrolls."""
        if not self._split_on:
            return
        left, right = self.panes.sizes()
        total = left + right
        if total <= 0:
            return
        want = round(total * self._split_ratio)
        need_left = self.stack.minimumSizeHint().width()
        need_right = self.side_stack.minimumSizeHint().width()
        if need_left + need_right <= total:
            want = max(need_left, min(want, total - need_right))
        if want != left:
            self.panes.setSizes([want, total - want])

    def side_choices(self):
        """Tools that can go on the right: every tool in the rail except
        the one already on the left."""
        if not self._split_on:
            return []
        return [tid for tid in nav_layout.visible_tool_ids(self.nav_layout)
                if tid != self._current_tool_id]

    def _place_page(self, page, stack):
        """Moves a page into `stack` if it isn't there already. Pages keep
        running while they move - only their parent changes."""
        if stack.indexOf(page) >= 0:
            return
        other = self.side_stack if stack is self.stack else self.stack
        other.removeWidget(page)
        stack.addWidget(page)

    def _show_side(self, tool_id):
        """Puts `tool_id` on the right (falling back to the first choice),
        or empties the right side when dual view is off."""
        choices = self.side_choices()
        if tool_id not in choices:
            tool_id = choices[0] if choices else None
        for i in reversed(range(self.side_stack.count())):
            page = self.side_stack.widget(i)
            if tool_id is None or page is not self.pages[tool_id]:
                self._place_page(page, self.stack)
        self._side_tool_id = tool_id
        if tool_id is not None:
            page = self.pages[tool_id]
            # on_shown() only when it's newly on show: switching the left
            # side mustn't make the right side refresh every time too.
            newly_shown = self.side_stack.indexOf(page) < 0
            self._place_page(page, self.side_stack)
            self.side_stack.setCurrentWidget(page)
            if tool_id != self.shared_settings.get("split_tool"):
                self.shared_settings["split_tool"] = tool_id
                self.shared_settings.save()
            if newly_shown:
                page.on_shown()
        self.push_header()
        self._balance_panes()

    def _fit_panes(self):
        """One tool: the whole width. Dual view: two capped panes."""
        self._panes_holder.setMaximumWidth(2 * PANE_MAX_WIDTH + PANE_GAP if self._split_on else QWIDGETSIZE_MAX)

    # ------------------------------------------------------------ desktop --
    def _apply_layout(self, mode):
        """Swap between the panes and the desktop's floating windows when
        the theme changes which it wants. Pages only change parent - every
        tool keeps running - and each layout comes back as it was left."""
        if mode == self._layout:
            return
        self._layout = mode
        if mode == "desktop":
            if self._split_on:
                # Off for now, not forgotten: the setting stays for the panes.
                self._split_on = False
                self._show_side(None)
                self.side_pane.setVisible(False)
                self._fit_panes()
            self._panes_holder.hide()
            # The whole window is the desk, with the taskbar along the bottom.
            self.header_slot.hide()
            self.header.hide()
            self.rail.hide()
            self._root.setContentsMargins(0, 0, 0, 0)
            self._root.setSpacing(0)
            self.taskbar.show()
            if self.desk_menu is None:
                self.desk_menu = DeskMenu(self)
            self.desktop.show_desk(self._current_tool_id)
        else:
            self.taskbar.hide()
            if self.desk_menu is not None:
                self.desk_menu.hide()
            self._root.setContentsMargins(16, 16, 16, 16)
            self._root.setSpacing(16)
            self.header_slot.show()
            self.header.show()
            self.header.place()
            self.rail.show()
            front = self.desktop.front()
            self.desktop.release_all()
            self._panes_holder.show()
            visible = nav_layout.visible_tool_ids(self.nav_layout)
            tool_id = next((t for t in (front, self._current_tool_id) if t in visible),
                           visible[0] if visible else None)
            if tool_id is not None:
                self.switch_tool(tool_id)
            if self.shared_settings.get("split_view", False):
                self.set_split_view(True)
        self.push_header()
        self.push_rail()
        self.push_taskbar()

    def _stash_page(self, page):
        """A page whose window closed waits, still running, in the (hidden)
        left-hand stack - where every page not on show lives."""
        if self.stack.indexOf(page) < 0:
            self.stack.addWidget(page)

    def _on_desk_changed(self, front_id):
        if front_id is not None:
            self._current_tool_id = front_id
        self.push_taskbar()
        self._desk_save_timer.start()

    def _save_desk(self):
        if not hasattr(self, "desktop"):
            return
        self._desk_save_timer.stop()
        data = self.desktop.state.to_dict()
        if data != self.shared_settings.get(desktop_layout.SETTINGS_KEY):
            self.shared_settings[desktop_layout.SETTINGS_KEY] = data
            self.shared_settings.save()

    def arrange_windows(self, how):
        """The taskbar's Cascade / Tile buttons."""
        if self._layout == "desktop":
            (self.desktop.tile if how == "tile" else self.desktop.cascade)()

    # ------------------------------------------------------------ taskbar --
    def push_taskbar(self):
        if not hasattr(self, "taskbar") or self._layout != "desktop":
            return
        visible = set(nav_layout.visible_tool_ids(self.nav_layout))
        front = self.desktop.front()
        items = []
        for tool_id, pinned in desktop_layout.taskbar_ids(self.pins, self.desktop.state, visible):
            state = self.desktop.window_state(tool_id) or "closed"
            items.append({
                "id": tool_id, "label": self.pages[tool_id].display_name, "state": state,
                "active": tool_id == front, "pinned": pinned,
                "color": self.desktop.window_color(tool_id) if state != "closed" else "transparent",
            })
        self.taskbar.show_state({
            "items": items, "connected": self.connected,
            "orb": self.announcements.unseen() if hasattr(self, "announcements") else False,
        })

    def task_clicked(self, tool_id):
        """A taskbar button: open, restore or bring forward - or minimise
        the window already in front, as Windows does."""
        if desktop_layout.task_click(self.desktop.state, tool_id) == "minimize":
            self.desktop.minimize(tool_id)
        else:
            self.switch_tool(tool_id)

    def _programs_state(self):
        """Every tool in the rail's order, grouped under its headings."""
        groups = [{"heading": "", "items": []}]
        for entry in self.nav_layout:
            if entry["type"] == nav_layout.DIVIDER:
                groups.append({"heading": entry["label"] or "", "items": []})
            elif entry["visible"]:
                tool_id = entry["id"]
                groups[-1]["items"].append({
                    "id": tool_id, "label": self.pages[tool_id].display_name,
                    "pinned": tool_id in self.pins,
                    "state": self.desktop.window_state(tool_id) or "closed",
                })
        return {"mode": "programs", "groups": [g for g in groups if g["items"]]}

    def open_programs(self, anchor):
        self.desk_menu.popup("programs", self._programs_state(), anchor)

    def open_task_menu(self, tool_id, anchor):
        state = self.desktop.window_state(tool_id)
        if state is None:
            items = [{"action": "open", "label": "Open"}]
        elif state == "min":
            items = [{"action": "open", "label": "Restore"}]
        else:
            maxed = self.desktop.state.windows[tool_id]["max"]
            items = [{"action": "minimize", "label": "Minimise"},
                     {"action": "max", "label": "Restore size" if maxed else "Maximise"}]
        pinned = tool_id in self.pins
        items.append({"action": "unpin" if pinned else "pin",
                      "label": "Unpin from taskbar" if pinned else "Pin to taskbar"})
        if state is not None:
            items.append({"action": "close", "label": "Close window", "danger": True})
        self.desk_menu.popup(f"task:{tool_id}", {"mode": "actions", "id": tool_id,
                                                 "title": self.pages[tool_id].display_name, "items": items}, anchor)

    def task_action(self, action, tool_id):
        """A pick from a taskbar button's menu."""
        if tool_id not in self.pages:
            return
        if action == "open":
            self.switch_tool(tool_id)
        elif action == "minimize":
            self.desktop.minimize(tool_id)
        elif action == "max":
            self.desktop.toggle_max(tool_id)
        elif action == "close":
            self.desktop.close(tool_id)
        elif action in ("pin", "unpin"):
            self.set_pinned(tool_id, action == "pin")

    def set_pinned(self, tool_id, on):
        """Pin a tool to the taskbar (at the end) or unpin it."""
        if on and tool_id not in self.pins:
            self.pins.append(tool_id)
        elif not on and tool_id in self.pins:
            self.pins.remove(tool_id)
        else:
            return
        self.shared_settings[desktop_layout.PINS_KEY] = list(self.pins)
        self.shared_settings.save()
        self.push_taskbar()
        if self.desk_menu is not None and self.desk_menu.isVisible() and self.desk_menu._kind == "programs":
            self.desk_menu.refresh(self._programs_state())

    # -------------------------------------------------------- web chrome --
    def push_header(self):
        self.header.show_state({
            "connected": self.connected, "orb": self.announcements.unseen() if hasattr(self, "announcements") else False,
            "split": self._split_on, "side": self._side_tool_id,
            "choices": [{"id": tid, "label": self.pages[tid].display_name} for tid in self.side_choices()],
        })
        # The desktop layout's taskbar shows the connection and the orb too.
        self.push_taskbar()

    def push_rail(self):
        items = []
        for entry in self.nav_layout:
            if entry["type"] == nav_layout.DIVIDER:
                items.append({"type": "heading", "label": entry["label"]} if entry["label"] else {"type": "line"})
            elif entry["visible"]:
                tool_id = entry["id"]
                items.append({"type": "tool", "id": tool_id, "label": self.pages[tool_id].display_name,
                              "active": tool_id == self._current_tool_id})
        self.rail.show_items(items)

    # ------------------------------------------------------------ nav rail --
    def rebuild_nav(self):
        """(Re)draw the rail from the saved layout (core/nav_layout.py).

        Pages are untouched - only buttons, headings and lines are replaced.
        If the page on screen just lost its button, the first visible tool
        takes over, so the rail and the page never disagree.
        """
        self.nav_layout = nav_layout.load(self.shared_settings, self.registry)
        self.push_rail()

        if self._layout == "desktop":
            # A tool taken out of the rail loses its window and its taskbar
            # button too.
            self.desktop.keep_only(set(nav_layout.visible_tool_ids(self.nav_layout)))
            self.push_taskbar()
            return
        if self._current_tool_id is not None:
            visible = nav_layout.visible_tool_ids(self.nav_layout)
            if self._current_tool_id not in visible and visible:
                self.switch_tool(visible[0])
        if self._split_on:
            # The right-hand tool may have just been hidden from the rail.
            self._show_side(self._side_tool_id)

    def open_nav_organizer(self, parent=None):
        """Settings > Window > Organize sidebar... - edits a copy, applies on
        Save only."""
        dialog = NavOrganizerDialog(
            parent or self,
            self.nav_layout,
            {tid: page.display_name for tid, page in self.pages.items()},
            self.theme_tokens(),
            placeholder_ids=[tid for tid, page in self.pages.items() if page.is_placeholder],
        )
        dialog.set_default(nav_layout.default_layout(self.registry))
        if dialog.exec() and dialog.result_layout is not None:
            nav_layout.save(self.shared_settings, dialog.result_layout)
            self.rebuild_nav()

    def _center_on_screen(self):
        """Centre the window on the screen the pointer is currently on.

        Nothing persists Buddy's geometry, so without this the window
        manager decides where it opens - and on a machine that has had a
        second monitor attached that can be completely outside the current
        desktop. The title bar goes off-screen with it, so the window then
        can't be dragged back either.

        Also shrinks to fit a display smaller than the default 1180x760,
        lowering the minimum size when it has to: a minimum larger than the
        screen would put part of the window out of reach all over again.
        """
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()

        width = min(self.width(), available.width())
        height = min(self.height(), available.height())
        if (width, height) != (self.width(), self.height()):
            self.setMinimumSize(min(self.minimumWidth(), width),
                                min(self.minimumHeight(), height))
            self.resize(width, height)

        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        # Clamp instead of trusting moveCenter: a frame taller than the work
        # area would start above it, hiding the title bar off the top edge.
        x = max(available.left(), min(frame.left(), available.right() - width + 1))
        y = max(available.top(), min(frame.top(), available.bottom() - height + 1))
        self.move(x, y)

    def switch_tool(self, tool_id):
        crash_log.trail("shown", tool_id)
        if self._layout == "desktop":
            # Open its window, bring it back from minimised, or to the front.
            self._current_tool_id = tool_id
            self.desktop.open(tool_id)
            return
        previous = self._current_tool_id
        page = self.pages[tool_id]
        self._place_page(page, self.stack)
        self.stack.setCurrentWidget(page)
        self._current_tool_id = tool_id
        self.push_rail()
        page.on_shown()
        if self._split_on:
            # Picking the tool that's on the right swaps the two sides,
            # rather than leaving the right side empty.
            swap = tool_id == self._side_tool_id and previous is not None
            self._show_side(previous if swap else self._side_tool_id)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._balance_panes()
        if self.busy_overlay.isVisible():
            self.busy_overlay.setGeometry(self.centralWidget().rect())

    # ------------------------------------------------------------ connection --
    def _set_connected(self, connected):
        changed = connected != self.connected
        self.connected = connected
        self.push_header()
        if changed:
            for page in self.pages.values():
                page.on_connection_changed(connected)

    def reconnect(self):
        """The header's Connect (Reconnect once connected)."""
        self._connect(silent=False)

    def _connect(self, silent=False):
        if self._connect_worker is not None:
            return  # already connecting; let it finish rather than overlap
        worker = _ResolveConnectWorker(self)
        worker.done.connect(lambda controller, error: self._on_connect_done(controller, error, silent))
        worker.finished.connect(lambda: self._forget_connect_worker(worker))
        self._connect_worker = worker
        worker.start()

    def _on_connect_done(self, controller, error, silent):
        self.controller = controller
        self._set_connected(controller is not None)
        if controller is None and not silent:
            alert(self, "Connection failed", str(error))

    def _forget_connect_worker(self, worker):
        if self._connect_worker is worker:
            self._connect_worker = None

    def _shutdown_connect_worker(self):
        """For quitting: a QThread destroyed while running crashes the exit."""
        worker = self._connect_worker
        if worker is None:
            return
        try:
            worker.done.disconnect()
            worker.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        worker.wait(_PROBE_TIMEOUT_SECONDS * 1000 + 2000)
        self._connect_worker = None

    # ------------------------------------------------------------ settings --
    def open_settings(self):
        current_page = (self.pages.get(self._current_tool_id) if self._layout == "desktop"
                        else self.stack.currentWidget())
        dialog = SettingsDialog(self, self.shared_settings, self._on_settings_applied, current_page)
        dialog.exec()
        # A child of this window, so nothing else would ever free it: each
        # opening left a hidden Settings web view (and renderer) behind.
        # deleteLater: destroyed from the event loop, on this thread.
        dialog.deleteLater()

    def _on_settings_applied(self):
        self.apply_theme()
        self.apply_stay_on_top(self.shared_settings.get("stay_on_top", False))
