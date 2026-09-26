"""The Buddy Network connection: one QWebSocket to the server, on Qt's own
event loop (no threads), that reconnects by itself with a growing delay.

The page gives start() the server address and a function returning the
hello to send on each (re)connect - it carries the saved token, which can
change after the first welcome. Every frame from the server is emitted as
`received`; the page reacts, and calls stop() on the errors that mean
retrying is pointless (bad_token, update_required).
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

# Keep in step with server/core.py (tests check). 2: encrypted DMs (e2e.py).
PROTOCOL_VERSION = 2
MAX_MESSAGE_CHARS = 2000

OFF, CONNECTING, ONLINE, WAITING = "off", "connecting", "online", "waiting"
RETRY_SECONDS = (2, 5, 10, 20, 30, 60)
KEEPALIVE_SECONDS = 30   # a dead connection is noticed within ~2x this


def missing_support() -> str:
    """"" normally. QtWebSockets comes with the full PySide6 the installer
    sets up; imported lazily so a PySide6 without it (e.g. PySide6-Essentials)
    only disables this page instead of stopping Buddy from starting. The
    same goes for the cryptography package, which encrypted DMs need."""
    try:
        import PySide6.QtWebSockets  # noqa: F401
    except ImportError:
        return ("Buddy Network needs PySide6's QtWebSockets module, which this PC's PySide6 "
                "doesn't include. Reinstalling Buddy adds it.")
    from . import e2e
    if not e2e.AVAILABLE:
        return ("Buddy Network needs the \"cryptography\" Python package for its encrypted direct "
                "messages. Reinstalling Buddy adds it.")
    return ""


class NetworkClient(QObject):
    state_changed = Signal(str, str)   # state, detail (e.g. why it's offline)
    received = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = OFF
        self._detail = ""
        self._url = ""
        self._hello = None
        self._running = False
        self._attempt = 0
        self._last_error = ""
        self._awaiting_pong = False

        from PySide6.QtWebSockets import QWebSocket
        self._ws = QWebSocket()
        self._ws.setParent(self)
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.errorOccurred.connect(self._on_error)
        self._ws.pong.connect(self._on_pong)

        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)
        self._keepalive = QTimer(self)
        self._keepalive.setInterval(KEEPALIVE_SECONDS * 1000)
        self._keepalive.timeout.connect(self._on_keepalive)

    # ------------------------------------------------------------- control

    def start(self, url: str, hello):
        """hello: a callable returning the hello payload for this attempt."""
        self.stop()
        self._url, self._hello = url, hello
        self._running = True
        self._attempt = 0
        self._open()

    def stop(self):
        self._running = False
        self._retry.stop()
        self._keepalive.stop()
        self._ws.abort()
        self._set_state(OFF)

    def send(self, payload: dict) -> bool:
        if self.state != ONLINE:
            return False
        self._ws.sendTextMessage(json.dumps(payload, ensure_ascii=False))
        return True

    # ------------------------------------------------------------ internals

    def _set_state(self, state: str, detail: str = ""):
        if (state, detail) != (self.state, self._detail):
            self.state, self._detail = state, detail
            self.state_changed.emit(state, detail)

    def _open(self):
        if not self._running:
            return
        self._last_error = ""
        self._set_state(CONNECTING)
        self._ws.open(QUrl(self._url))

    def _on_connected(self):
        self._awaiting_pong = False
        self._keepalive.start()
        self._ws.sendTextMessage(json.dumps(self._hello(), ensure_ascii=False))

    def _on_text(self, text: str):
        try:
            msg = json.loads(text)
        except (ValueError, RecursionError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "welcome":
            self._attempt = 0
            self._set_state(ONLINE)
        self.received.emit(msg)

    def _on_error(self, _error):
        self._last_error = self._ws.errorString()

    def _on_disconnected(self):
        self._keepalive.stop()
        if not self._running:
            self._set_state(OFF)
            return
        delay = RETRY_SECONDS[min(self._attempt, len(RETRY_SECONDS) - 1)]
        self._attempt += 1
        reason = self._last_error or "Connection lost"
        self._set_state(WAITING, f"{reason} – trying again in {delay}s")
        self._retry.start(delay * 1000)

    def _on_keepalive(self):
        if self._awaiting_pong:
            self._last_error = "The server stopped answering"
            self._ws.abort()
            return
        self._awaiting_pong = True
        self._ws.ping()

    def _on_pong(self, *_args):
        self._awaiting_pong = False
