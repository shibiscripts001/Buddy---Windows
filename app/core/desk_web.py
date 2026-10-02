#!/usr/bin/env python3
"""
The desktop layout's taskbar and its popup menu, as web views
(app/web/shell/taskbar/ and app/web/shell/deskmenu/). The shell
(core/shell_window.py) keeps the state; these draw it and report clicks.

The taskbar runs along the bottom of the shell window in place of the
header and the nav rail: a Programs button, the pinned and open tools, and
a tray - the Resolve connection, the bug report, Cascade / Tile, the news
orb, Settings.

The menu - Programs, or a taskbar button's right-click - is a popup window
of its own rather than part of the taskbar's page: the tool windows are
native (core/desktop_window.py), and nothing inside the shell window could
draw over them.

Protocol (taskbar):
    to the view    taskbar
    from the view  programs, task, task_menu, reconnect, arrange, orb, bug, settings, size
Protocol (menu):
    to the view    menu
    from the view  open, pin, act, settings, size
"""

import os
import time

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication

from core.shell_web import SHELL_WEB_DIR, _ChromeView
from core.web_page import WebWindow

# How long after the menu closes a click on the button that opened it is
# still the click that closed it (Qt closes a popup on the press, then
# hands the same press to whatever is under it).
REOPEN_GUARD = 0.25


class TaskbarView(_ChromeView):
    web_dir = os.path.join(SHELL_WEB_DIR, "taskbar")

    def web_ready(self):
        self.shell.push_taskbar()

    def show_state(self, state):
        self.emit("taskbar", state)

    def fit(self, size):
        height = size.get("height")
        if isinstance(height, (int, float)) and 24 <= height <= 160:
            self.setFixedHeight(int(round(height)))

    def _anchor(self, payload):
        """The top-left of the clicked button, on screen."""
        left = (payload or {}).get("left")
        left = int(left) if isinstance(left, (int, float)) and 0 <= left <= 10000 else 0
        return self.mapToGlobal(QPoint(left, 0))

    def on_programs(self, payload):
        self.shell.open_programs(self._anchor(payload))

    def on_task(self, payload):
        tool_id = (payload or {}).get("id")
        if tool_id in self.shell.pages:
            self.shell.task_clicked(tool_id)

    def on_task_menu(self, payload):
        tool_id = (payload or {}).get("id")
        if tool_id in self.shell.pages:
            self.shell.open_task_menu(tool_id, self._anchor(payload))

    def on_media(self, payload):
        self.shell.badge_media((payload or {}).get("id"))

    def on_reconnect(self, _payload=None):
        self.shell.reconnect()

    def on_arrange(self, payload):
        how = (payload or {}).get("how")
        if how in ("cascade", "tile"):
            self.shell.arrange_windows(how)

    def on_orb(self, _payload=None):
        self.shell.open_announcements()

    def on_bug(self, _payload=None):
        self.shell.open_bug_report()

    def on_settings(self, _payload=None):
        self.shell.open_settings()


class DeskMenu(WebWindow):
    """The popup: Programs (every tool, grouped as in the rail, with a pin
    each) or a taskbar button's own actions. Closes on a click elsewhere."""

    web_dir = os.path.join(SHELL_WEB_DIR, "deskmenu")
    transparent = True

    def __init__(self, shell):
        self.shell = shell
        self._anchor = QPoint()
        self._state = None
        self._closed_at = 0.0
        self._kind = None
        super().__init__(shell)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(320, 420)

    def web_ready(self):
        if self._state is not None:
            self.emit("menu", self._state)

    def popup(self, kind, state, anchor):
        """Show `state` ("programs" or "actions") above `anchor` (the
        button's top-left on screen). A second click on the same button
        while it's open just closes it."""
        if time.monotonic() - self._closed_at < REOPEN_GUARD and kind == self._kind:
            return
        self._kind, self._state, self._anchor = kind, state, anchor
        self.emit("menu", state)
        self._place()
        self.show()
        self.raise_()
        self.activateWindow()

    def refresh(self, state):
        """New contents while open (a pin toggled)."""
        self._state = state
        self.emit("menu", state)

    def _place(self):
        screen = QApplication.screenAt(self._anchor) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(self._anchor.x(), area.right() - self.width()))
        y = max(area.top(), self._anchor.y() - self.height() - 6)
        self.move(x, y)

    def on_size(self, payload):
        w, h = (payload or {}).get("width"), (payload or {}).get("height")
        if all(isinstance(v, (int, float)) and 40 <= v <= 2000 for v in (w, h)):
            self.resize(int(w), int(h))
            self._place()

    def hideEvent(self, event):
        self._closed_at = time.monotonic()
        super().hideEvent(event)

    def on_open(self, payload):
        tool_id = (payload or {}).get("id")
        self.hide()
        if tool_id in self.shell.pages:
            self.shell.switch_tool(tool_id)

    def on_pin(self, payload):
        tool_id = (payload or {}).get("id")
        if tool_id in self.shell.pages:
            self.shell.set_pinned(tool_id, bool((payload or {}).get("on")))

    def on_act(self, payload):
        action = (payload or {}).get("action")
        self.hide()
        self.shell.task_action(action, (payload or {}).get("id"))

    def on_settings(self, _payload=None):
        self.hide()
        self.shell.open_settings()
