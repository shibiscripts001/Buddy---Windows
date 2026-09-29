"""Report a bug: the bug button in the header (and the desktop layout's
taskbar) opens this window - what went wrong, in words and screenshots -
and sends it to the Buddy Network server, where the owner reads it in the
Admin panel's Bugs tab (server/bugs.py).

Anyone can send one. Signed in to Buddy Network, it goes over that
connection and says who it's from; otherwise Buddy opens a connection just
for the report, sends it without signing in, and it arrives with no name.
Either way nothing is sent until Send is pressed.

What goes with it (details): Buddy's version, the system's (Windows or macOS), Resolve's (read
once when Buddy connected to it - core/resolve_bridge.py - so sending
never waits on Resolve) and the tool on screen. The window lists them
before anything is sent. Nothing about the project, its media or the PC.

Screenshots - picked, pasted or dropped - are shrunk on this PC by Buddy
Network's own images.shrink (a WebP, every bit of metadata left behind),
up to BUG_IMAGES_MAX of them. They go up in parts, one every
PART_EVERY_MS, so a report never runs past the server's request limit.

Protocol (the window, app/web/shell/bugreport/):
    to the view    bug
    from the view  add, paste, remove, send, close
"""

from __future__ import annotations

import base64
import json
import os
import platform
import sys

from PySide6.QtCore import QBuffer, QIODevice, QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog

from core.app_version import buddy_version
from core.i18n import tr, tr_filter
from core.web_page import WEB_COMMON_DIR, WebDialog

# Keep in step with server/bugs.py (tests check).
BUG_TEXT_MAX = 4000
BUG_IMAGES_MAX = 6
BUG_SIDE = 2400             # what a screenshot is scaled to fit - more than a chat picture, to keep text legible
PART_CHARS = 24000          # base64 per bug_part frame (the server's IMAGE_PART_CHARS)
PART_EVERY_MS = 120         # under the server's 10 requests a second, with room for the chat
TIMEOUT_MS = 120_000        # the whole report; the server gives a connection without a hello as long (BUG_REPORT_TIMEOUT)
PICK_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff);;All files (*)"
PICTURE_ENDINGS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
CANT_REACH = ("Couldn't reach the Buddy Network server – check the internet connection and try again.")
LOST = "The connection dropped before the report was in – try again."
TOO_SLOW = "The server didn't answer in time – try again."


# ------------------------------------------------------------- details

def system_version() -> str:
    """"Windows 11 (build 26200)" - Windows 11 still calls itself 10 inside;
    its builds start at 22000 - or "macOS 15.1"."""
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if sys.platform != "win32":
        return f"{platform.system()} {platform.release()}".strip()
    version = sys.getwindowsversion()
    name = "11" if version.major == 10 and version.build >= 22000 else platform.release()
    return f"Windows {name} (build {version.build})"


def details(shell) -> dict:
    """What goes with the report, from the shell as it is right now."""
    # "windows" is the server's name for it (server/bugs.py), a Mac's too.
    out = {"buddy": buddy_version(), "windows": system_version()}
    controller = shell.controller if getattr(shell, "connected", False) else None
    out["resolve"] = (getattr(controller, "about", "") or "Connected") if controller else "Not connected"
    tools = [page.display_name for page in shell.pages_on_screen()]
    out["tool"] = " + ".join(tools)
    return {key: value for key, value in out.items() if value}


DETAIL_LABELS = (("buddy", "Buddy"), ("windows", "System"), ("resolve", "Resolve"), ("tool", "Tool"))


def detail_rows(found: dict) -> list[dict]:
    """For the window's "Sent with it" list: each label (translated) and its value (as it is)."""
    return [{"label": label, "value": found[key]} for key, label in DETAIL_LABELS if found.get(key)]


# -------------------------------------------------------------- frames

def frames(text: str, found: dict, shots: list[dict], part_chars: int = PART_CHARS) -> list[dict]:
    """Every request the report takes, in order: each screenshot's parts
    (bug_part), then the report (bug_report) naming them. shots: [{"data",
    "w", "h"}]."""
    out, named = [], []
    for shot in shots:
        image_id = os.urandom(16).hex()
        encoded = base64.b64encode(shot["data"]).decode("ascii")
        pieces = [encoded[i:i + part_chars] for i in range(0, len(encoded), part_chars)] or [""]
        out += [{"type": "bug_part", "id": image_id, "seq": seq, "data": piece, "last": seq == len(pieces) - 1}
                for seq, piece in enumerate(pieces)]
        named.append({"id": image_id, "w": shot["w"], "h": shot["h"]})
    out.append({"type": "bug_report", "text": text, "details": found, "images": named})
    return out


# -------------------------------------------------------------- sending

class BugReportSender(QObject):
    """Sends a report's frames, paced, and says how it went. Over
    Buddy Network's own connection (a NetworkClient that's online), or -
    with none - over a connection of its own, closed once it's done."""

    progress = Signal(int)     # percent sent
    finished = Signal(str)     # "" once the server has it; else what went wrong

    def __init__(self, payloads: list[dict], client=None, server_url: str = "", parent=None):
        super().__init__(parent)
        self._payloads = payloads
        self._client = client
        self._url = server_url
        self._ws = None
        self._next = 0
        self._done = False
        self._tick = QTimer(self)
        self._tick.setInterval(PART_EVERY_MS)
        self._tick.timeout.connect(self._send_next)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._finish(TOO_SLOW))

    def start(self):
        self._timeout.start(TIMEOUT_MS)
        if self._client is not None:
            self._client.received.connect(self._on_message)
            self._client.state_changed.connect(self._on_client_state)
            self._begin()
            return
        from PySide6.QtWebSockets import QWebSocket
        self._ws = QWebSocket()
        self._ws.setParent(self)
        self._ws.connected.connect(self._begin)
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.disconnected.connect(lambda: self._finish(LOST if self._next else CANT_REACH))
        self._ws.open(QUrl(self._url))

    def cancel(self):
        """The window closed: stop, quietly."""
        self._done = True
        self._stop()

    def _begin(self):
        self._send_next()
        self._tick.start()

    def _send(self, payload: dict) -> bool:
        if self._client is not None:
            return self._client.send(payload)
        self._ws.sendTextMessage(json.dumps(payload, ensure_ascii=False))
        return True

    def _send_next(self):
        if self._done:
            return
        if self._next >= len(self._payloads):
            self._tick.stop()   # all sent: the answer comes next
            return
        if not self._send(self._payloads[self._next]):
            self._finish(LOST)
            return
        self._next += 1
        self.progress.emit(self._next * 100 // len(self._payloads))

    def _on_text(self, text: str):
        try:
            msg = json.loads(text)
        except (ValueError, RecursionError):
            return
        if isinstance(msg, dict):
            self._on_message(msg)

    def _on_message(self, msg: dict):
        if msg.get("type") == "bug_reported":
            self._finish("")
        elif msg.get("type") == "error" and msg.get("re") in ("bug_part", "bug_report"):
            self._finish(str(msg.get("message") or "The server couldn't take the report."))

    def _on_client_state(self, state: str, _detail: str = ""):
        if state != "online":
            self._finish(LOST)

    def _finish(self, problem: str):
        if self._done:
            return
        self._done = True
        self._stop()
        self.finished.emit(problem)

    def _stop(self):
        self._tick.stop()
        self._timeout.stop()
        if self._client is not None:
            for signal, slot in ((self._client.received, self._on_message),
                                 (self._client.state_changed, self._on_client_state)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        if self._ws is not None:
            try:
                self._ws.disconnected.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._ws.close()


# ------------------------------------------------------------ the window

class _ShrinkWorker(QThread):
    """images.shrink off the main thread: a big screenshot takes a moment."""

    shrunk = Signal(int, object, str)   # ticket, (Shrunk, preview URL) or None, why not

    def __init__(self, ticket: int, data: bytes):
        super().__init__()
        self.ticket, self.data = ticket, data

    def run(self):
        from pages.buddy_network import images
        try:
            shrunk = images.shrink(self.data, max_side=BUG_SIDE)
            self.shrunk.emit(self.ticket, (shrunk, images.preview_url(shrunk.data)), "")
        except images.ImageError as exc:
            self.shrunk.emit(self.ticket, None, str(exc))
        except Exception:   # noqa: BLE001 - anything Pillow raises for a file it can't read
            self.shrunk.emit(self.ticket, None,
                             "That file isn't a picture Buddy can read – try a PNG, JPEG, WebP or GIF.")


class BugReportDialog(WebDialog):
    web_dir = os.path.join(WEB_COMMON_DIR, "shell", "bugreport")
    file_drops = True

    def __init__(self, shell):
        """shell: the ShellWindow - its details, and Buddy Network's connection."""
        self.shell = shell
        super().__init__(shell, shell, "Report a bug", (560, 600))

    def build_state(self):
        self.details = details(self.shell)
        self.shots = []          # {"data", "w", "h", "preview", "label", "ticket"}, in the order added
        self._preparing = set()  # tickets of screenshots being shrunk
        self._ticket = 0
        self._workers = []
        self._sender = None
        self._progress = None    # percent while sending
        self._error = ""
        self._sent = False

    def web_ready(self):
        self._push()

    def _push(self, focus=False):
        client, me = self.shell.network_connection()
        from pages.buddy_network import images, render
        self.emit("bug", {
            "shots": [{"preview": s["preview"], "label": s["label"]} for s in self.shots],
            "max": BUG_IMAGES_MAX, "text_max": BUG_TEXT_MAX,
            "can_add": images.AVAILABLE and len(self.shots) + len(self._preparing) < BUG_IMAGES_MAX,
            "preparing": len(self._preparing),
            "sending": self._progress, "sent": self._sent, "error": self._error,
            "details": detail_rows(self.details),
            "who": (f"Sent as {render.display_name(me)} on Buddy Network, so you can get a reply there."
                    if client is not None and me else
                    "Sent without a name – you're not signed in to Buddy Network."),
            "focus": focus,
        })

    # ------------------------------------------------------ screenshots

    def on_add(self, _payload=None):
        if self._busy():
            return
        paths, _chosen = QFileDialog.getOpenFileNames(self, tr("Add screenshots"), "", tr_filter(PICK_FILTER))
        self._add_files(paths)

    def on_paste(self, _payload=None):
        """Ctrl+V with a picture (or picture files copied in Explorer) on the clipboard."""
        if self._busy():
            return
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        files = [u.toLocalFile() for u in (mime.urls() if mime is not None and mime.hasUrls() else [])
                 if u.isLocalFile() and u.toLocalFile().lower().endswith(PICTURE_ENDINGS)]
        if files:
            self._add_files(files)
            return
        image = clipboard.image()
        if image.isNull():
            return
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        self._add(bytes(buffer.data()))

    def on_files_dropped(self, paths):
        if self._busy():
            return
        pictures = [p for p in paths if p.lower().endswith(PICTURE_ENDINGS)]
        if not pictures:
            self._error = "Only pictures can go with a report – PNG, JPEG, WebP, GIF, BMP or TIFF."
            self._push()
            return
        self._add_files(pictures)

    def _add_files(self, paths):
        from pages.buddy_network import images
        for path in paths:
            try:
                if os.path.getsize(path) > images.MAX_INPUT_BYTES:
                    raise images.ImageError(f"That file is over {images.MAX_INPUT_BYTES // (1024 * 1024)} MB – "
                                            "pick a smaller picture.")
                with open(path, "rb") as f:
                    data = f.read()
            except images.ImageError as exc:
                self._error = str(exc)
                self._push()
                return
            except OSError as exc:
                self._error = f"Couldn't read that file: {exc.strerror or exc}"
                self._push()
                return
            if not self._add(data):
                return

    def _add(self, data: bytes) -> bool:
        """Shrinks a screenshot (on a thread); False if there's no room for it."""
        from pages.buddy_network import images
        if not images.AVAILABLE:
            return False
        if len(self.shots) + len(self._preparing) >= BUG_IMAGES_MAX:
            self._error = f"A report can have {BUG_IMAGES_MAX} screenshots at most."
            self._push()
            return False
        self._ticket += 1
        self._preparing.add(self._ticket)
        self._error = ""
        worker = _ShrinkWorker(self._ticket, data)
        worker.shrunk.connect(self._on_shrunk)
        worker.finished.connect(self._reap)
        self._workers.append(worker)
        worker.start()
        self._push()
        return True

    def _on_shrunk(self, ticket: int, result, problem: str):
        if ticket not in self._preparing:
            return
        self._preparing.discard(ticket)
        if result is None:
            self._error = problem
        else:
            from pages.buddy_network import images
            shrunk, preview = result
            self.shots.append({"data": shrunk.data, "w": shrunk.w, "h": shrunk.h, "preview": preview,
                               "label": f"{shrunk.w} x {shrunk.h}, {images.size_label(len(shrunk.data))}",
                               "ticket": ticket})
            # In the order they were added, whichever finished shrinking first.
            self.shots.sort(key=lambda s: s["ticket"])
        self._push()

    def _reap(self):
        self._workers = [w for w in self._workers if w.isRunning()]

    def on_remove(self, payload):
        index = (payload or {}).get("index")
        if self._busy() or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(self.shots):
            return
        del self.shots[index]
        self._error = ""
        self._push()

    # ---------------------------------------------------------- sending

    def _busy(self) -> bool:
        return self._sender is not None or self._sent

    def on_send(self, payload):
        if self._busy():
            return
        text = str((payload or {}).get("text") or "").strip()
        if self._preparing:
            self._error = "Wait a moment – a screenshot is still being prepared."
        elif not text and not self.shots:
            self._error = "Say what went wrong, or add a screenshot."
        elif len(text) > BUG_TEXT_MAX:
            self._error = f"A report is at most {BUG_TEXT_MAX:,} characters."
        else:
            self._error = ""
        if self._error:
            self._push()
            return
        client, _me = self.shell.network_connection()
        self._sender = BugReportSender(frames(text, self.details, self.shots), client=client,
                                       server_url=self.shell.network_server_url(), parent=self)
        self._sender.progress.connect(self._on_progress)
        self._sender.finished.connect(self._on_finished)
        self._progress = 0
        self._push()
        self._sender.start()

    def _on_progress(self, percent: int):
        self._progress = percent
        self._push()

    def _on_finished(self, problem: str):
        self._sender = None
        self._progress = None
        self._sent = not problem
        self._error = problem
        self._push()

    def on_close(self, _payload=None):
        self.reject()

    def done(self, result):
        """However it closes: nothing more is sent, and no thread outlives it."""
        if self._sender is not None:
            self._sender.cancel()
            self._sender = None
        self._preparing.clear()
        for worker in self._workers:
            worker.wait(15000)
        super().done(result)
