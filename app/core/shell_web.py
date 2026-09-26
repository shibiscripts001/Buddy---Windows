#!/usr/bin/env python3
"""
The shell's header and nav rail as web views (app/web/shell/header/ and
app/web/shell/rail/), laid out by core/shell_window.py around the panes
exactly where the Qt header and rail used to be. They draw what the shell
sends and hand every click straight back to it - the shell keeps the
connection, the layout, dual view and the settings.

Each reports the size its content needs ("size"), and the shell fixes the
view to it: the header's height, the rail's width (its longest tool name,
so a wider theme font never clips one).

The header sits over a slot in the shell's layout rather than in it, so
while its dropdown is open ("overlay") the view can grow down over the
panes to show the list, and shrink back when it closes - without moving
anything.

Protocol (header):
    to the view    header
    from the view  reconnect, split, side, settings, orb, size, overlay
Protocol (rail):
    to the view    rail
    from the view  switch, size
"""

import os

from PySide6.QtCore import QEvent

from core.web_page import WEB_COMMON_DIR, WebWindow

SHELL_WEB_DIR = os.path.join(WEB_COMMON_DIR, "shell")

# Floor for the nav rail, so narrow-font themes keep today's proportions.
NAV_MIN_WIDTH = 220


class _ChromeView(WebWindow):
    """A shell web view, laid out in the shell window. `shell` is the
    ShellWindow - also the host that gives it the theme."""

    transparent = True

    def __init__(self, shell):
        self.shell = shell
        super().__init__(shell)

    def on_size(self, payload):
        self.fit((payload or {}))

    def fit(self, size):
        raise NotImplementedError


class HeaderView(_ChromeView):
    web_dir = os.path.join(SHELL_WEB_DIR, "header")

    def __init__(self, shell, slot):
        """slot: the widget in the shell's layout this sits over."""
        self.slot = slot
        self._overlay = 0
        super().__init__(shell)
        self.setParent(slot.parentWidget())
        slot.installEventFilter(self)
        self.place()
        self.show()

    def eventFilter(self, obj, event):
        if obj is self.slot and event.type() in (QEvent.Move, QEvent.Resize, QEvent.Show):
            self.place()
        return False

    def place(self):
        g = self.slot.geometry()
        self.setGeometry(g.x(), g.y(), g.width(), max(g.height(), self._overlay))
        self.raise_()

    def on_overlay(self, payload):
        height = (payload or {}).get("height")
        self._overlay = int(height) if isinstance(height, (int, float)) and 0 <= height <= 1200 else 0
        self.place()

    def web_ready(self):
        self.shell.push_header()

    def show_state(self, state):
        self.emit("header", state)

    def fit(self, size):
        height = size.get("height")
        if isinstance(height, (int, float)) and 20 <= height <= 200:
            self.slot.setFixedHeight(int(round(height)))
            self.place()

    def on_reconnect(self, _payload=None):
        self.shell.reconnect()

    def on_split(self, payload):
        self.shell.set_split_view(bool((payload or {}).get("on")))

    def on_side(self, payload):
        tool_id = (payload or {}).get("id")
        if tool_id in self.shell.side_choices() and tool_id != self.shell._side_tool_id:
            self.shell._show_side(tool_id)

    def on_settings(self, _payload=None):
        self.shell.open_settings()

    def on_orb(self, _payload=None):
        self.shell.open_announcements()


class RailView(_ChromeView):
    web_dir = os.path.join(SHELL_WEB_DIR, "rail")

    def web_ready(self):
        self.shell.push_rail()

    def show_items(self, items):
        self.emit("rail", {"items": items})

    def fit(self, size):
        width = size.get("width")
        if isinstance(width, (int, float)) and 0 < width <= 600:
            self.setFixedWidth(max(NAV_MIN_WIDTH, int(round(width))))

    def on_switch(self, payload):
        tool_id = (payload or {}).get("id")
        if tool_id in self.shell.pages:
            self.shell.switch_tool(tool_id)
