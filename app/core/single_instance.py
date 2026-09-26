#!/usr/bin/env python3
"""
Single-instance enforcement for Buddy.

Launching from Resolve's Workspace > Scripts menu spawns a brand new
process every time - with nothing preventing it, opening the menu item
twice (e.g. because the first window was minimized to the tray and easy to
forget about) would start a second Buddy independently polling Resolve for
Time Tracker, silently double-counting or conflicting with the first. A
QLocalServer/QLocalSocket handshake (Qt's own standard mechanism for
exactly this) makes the first instance listen on a well-known local name;
every later launch just pings that name - if something answers, this
process asks it to come to the front and exits immediately without ever
building a window.

Ported from Davinci Time Tracker's single_instance.py, unchanged apart from
the server name and operating at the whole-shell level instead of one
tool's.
"""

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "Buddy-SingleInstance"


def notify_existing_instance(start_hidden=False):
    """Returns True if a running instance answered (and has been asked to
    raise itself, unless start_hidden - the auto-start watcher's launch
    mustn't pop up a Buddy that's already running) - the caller should
    exit without creating a window.
    Returns False if nothing answered, meaning this process should become
    the primary instance."""
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    # A real existing instance answers near-instantly; this timeout only
    # matters for how long a normal launch waits before concluding it's
    # the first one, so it's kept short.
    if socket.waitForConnected(250):
        socket.write(b"hidden" if start_hidden else b"show")
        socket.waitForBytesWritten(100)
        socket.disconnectFromServer()
        return True
    return False


class SingleInstanceServer(QObject):
    """Owned by the primary instance's ShellWindow for its whole lifetime -
    any later launch's connection brings the window to the front, unless
    that launch says it was --start-hidden (see notify_existing_instance)."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        # Defensive: if a previous process crashed without a clean
        # shutdown and left a stale registration behind, this clears it so
        # listen() below doesn't fail thinking the name's still taken.
        QLocalServer.removeServer(SERVER_NAME)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._handle_connection)
        self.server.listen(SERVER_NAME)

    def _handle_connection(self):
        socket = self.server.nextPendingConnection()
        hidden = False
        if socket is not None:
            # The message says whether the second launch was --start-hidden
            # (the auto-start watcher): then Buddy is already running and
            # stays where it is. A launch that sent nothing is a plain one.
            if socket.bytesAvailable() or socket.waitForReadyRead(100):
                hidden = socket.readAll().data() == b"hidden"
            socket.disconnectFromServer()
        if not hidden:
            self.window.bring_to_front()
