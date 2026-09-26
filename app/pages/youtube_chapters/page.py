#!/usr/bin/env python3
"""
YouTube Chapters - turns the open timeline's markers into a YouTube
chapter list to paste into a video description, or save as a .txt. A web
page (core/web_page.py): the view is web/index.html + chapters.js; the
chapter rules are chapters.py.

READ-ONLY in Resolve: it reads markers and never changes the project.

Features: the list follows the timeline live (read while
the page is on screen - no Generate button), a marker-colour filter that
shows how many markers each colour has, markers YouTube would reject
shown with the reason, Copy for pasting straight into YouTube, and the
save folder remembered. The text stays editable; once edited it's kept
until Reset, even if the markers change.

Protocol:
    to the view    state, chapters, chapters_meta, log, toast, alert
    from the view  refresh, filter, edit, reset_text, copy, browse, folder,
                   file_name, save, open_folder
"""

import os
import time

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog

from core import marker_colors
from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage

from . import chapters, resolve_ext

POLL_MS = 2000
LOG_LIMIT = 40
DEFAULTS = {"folder": ""}


class YouTubeChaptersPage(WebToolPage):
    tool_id = "youtube_chapters"
    display_name = "YouTube Chapters"
    category = "Export & Delivery"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, DEFAULTS)
        self.color = chapters.ALL
        self.timeline = None       # {"name", "fps", "markers"} or None
        self.problem = ""          # why there's no timeline, when there isn't
        self.result = chapters.build({}, 24)
        self.text = ""
        self.edited = False
        self.stale = False         # markers changed under an edited text
        self._built_text = ""      # what the markers made, before any edit
        self.file_name = chapters.DEFAULT_FILE_NAME
        self._named_for = None     # the timeline the file name was made for
        self._signature = None
        self._log = []
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_resolve)
        self._poll.start()

    def web_ready(self):
        self._push_state()
        self._push_chapters()
        self.emit("log", self._log)

    def on_shown(self):
        self._read(connect=False)

    def on_app_quitting(self):
        self._poll.stop()

    # ------------------------------------------------------------ Resolve --

    def _controller(self, connect):
        if connect:
            try:
                return self.host.ensure_connected()
            except ResolveConnectionError as exc:
                self.problem = str(exc)
                return None
        return self.host.controller if getattr(self.host, "connected", False) else None

    def _poll_resolve(self):
        if self.isVisible():
            self._read(connect=False)

    def _read(self, connect):
        """Re-read the open timeline's markers; redraw only on a change."""
        controller = self._controller(connect)
        timeline, problem = None, ""
        if controller is None:
            problem = self.problem if connect else "Not connected to Resolve."
        else:
            try:
                markers, fps, _start, name = resolve_ext.read_timeline_markers(
                    resolve_ext.get_current_timeline(controller))
                timeline = {"name": name, "fps": fps, "markers": markers}
            except ResolveConnectionError as exc:
                problem = str(exc)
            except Exception as exc:  # noqa: BLE001 - shown to the user
                problem = f"Couldn't read the timeline: {exc}"
        signature = repr((timeline, problem))
        if signature == self._signature:
            return
        self._signature = signature
        self.timeline, self.problem = timeline, problem
        if timeline and timeline["name"] != self._named_for:
            self._named_for = timeline["name"]
            self.file_name = chapters.file_name_for(timeline["name"])
        self._rebuild()
        self._push_state()

    def _rebuild(self):
        t = self.timeline
        self.result = chapters.build(t["markers"] if t else {}, t["fps"] if t else 24, self.color)
        if self.edited:
            self.stale = self.result["text"] != self._built_text
        else:
            self.text = self.result["text"]
            self._built_text = self.text
        self._push_chapters()

    # --------------------------------------------------------------- view --

    def _push_state(self):
        t = self.timeline
        self.emit("state", {
            "connected": self._controller(connect=False) is not None,
            "timeline": t["name"] if t else "",
            "fps": t["fps"] if t else None,
            "problem": self.problem,
            "total": len(marker_colors.numeric_markers(t["markers"])) if t else 0,
            "colors": marker_colors.color_counts(t["markers"] if t else {}),
            "filter": self.color,
        })

    def _push_chapters(self):
        self.emit("chapters", {
            "chapters": self.result["chapters"],
            "skipped": self.result["skipped"],
            "warnings": self.result["warnings"],
            "text": self.text,
            "edited": self.edited,
            "stale": self.stale,
            "file_name": self.file_name,
            "folder": self.settings.get("folder") or "",
        })

    def _add_log(self, text, kind="info"):
        self._log.append({"time": time.strftime("%H:%M"), "text": text, "kind": kind})
        del self._log[:-LOG_LIMIT]
        self.emit("log", self._log)

    # ------------------------------------------------------------ actions --

    def on_refresh(self, _payload=None):
        self._signature = None
        self._read(connect=True)

    def on_filter(self, payload):
        color = (payload or {}).get("color")
        if color in [chapters.ALL] + marker_colors.NAMES and color != self.color:
            self.color = color
            self._rebuild()
            self._push_state()

    def on_edit(self, payload):
        text = str((payload or {}).get("text") or "")
        if text != self.text:
            self.text = text
            self.edited = text != self._built_text
            self.stale = False
            self.emit("chapters_meta", {"edited": self.edited, "stale": False})

    def on_reset_text(self, _payload=None):
        self.edited = self.stale = False
        self._rebuild()

    def on_copy(self, _payload=None):
        if not self.text.strip():
            return self.emit("toast", {"text": "Nothing to copy yet – add markers to the timeline."})
        QApplication.clipboard().setText(self.text.strip() + "\n")
        count = len([line for line in self.text.splitlines() if line.strip()])
        self.emit("toast", {"text": f"Copied {count} chapter{'s' if count != 1 else ''} – paste them into the video's description."})
        self._add_log(f"Copied {count} chapter lines.")

    def on_browse(self, _payload=None):
        folder = QFileDialog.getExistingDirectory(self, "Save chapters to", self.settings.get("folder") or "")
        if folder:
            self._set_folder(folder)

    def on_folder(self, payload):
        self._set_folder(str((payload or {}).get("value") or "").strip().strip('"').strip("'"))

    def _set_folder(self, folder):
        self.settings["folder"] = os.path.normpath(folder) if folder else ""
        self.settings.save()
        self._push_chapters()

    def on_file_name(self, payload):
        self.file_name = str((payload or {}).get("value") or "").strip()

    def on_save(self, _payload=None):
        content = self.text.strip()
        if not content:
            return self.emit("alert", {"title": "Nothing to save", "text": "There are no chapters yet – add markers to the timeline first."})
        folder = self.settings.get("folder") or ""
        if not folder or not os.path.isdir(folder):
            return self.emit("alert", {"title": "Choose a folder", "text": "Choose the folder to save the .txt in first."})
        name = self.file_name or chapters.DEFAULT_FILE_NAME
        if not name.lower().endswith(".txt"):
            name += ".txt"
        path = os.path.join(folder, name)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")
        except OSError as exc:
            self._add_log(f"Couldn't save {name}: {exc}", "error")
            return self.emit("alert", {"title": "Couldn't save", "text": str(exc)})
        self._add_log(f"Saved {path}", "ok")
        self.emit("toast", {"text": f"Saved {name}", "open_folder": True})

    def on_open_folder(self, _payload=None):
        folder = self.settings.get("folder") or ""
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
