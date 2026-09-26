#!/usr/bin/env python3
"""
Stills Exporter - drop markers at the frames worth keeping, grab a still
at every marker of one colour, and export them as image files. A web page
(core/web_page.py): the view is web/index.html + stills.js; every Resolve
call is resolve_ext.py.

This tool WRITES to Resolve: it adds markers, switches to the Color page
and moves the playhead to grab stills, and - only when asked, and after a
confirmation - deletes the grabbed stills from the gallery after export.
Each is behind its own button. Grabbing and exporting run behind the
shell's busy overlay: they're Resolve calls that must stay on this thread.

The grabbed-stills list lives on this page, not the shared controller (a
reconnect would drop it), and only for this session.

Features: the open timeline's markers are read while the
page is on screen, so each colour shows how many markers it has and what
a grab will visit; markers can be named as they're added; the grabbed
stills are listed (and can be taken off the list one by one); format,
prefix and folder are remembered; the gallery-delete confirmation is in
the page.

Protocol:
    to the view    state, grabbed, options, log, toast, alert
    from the view  refresh, add_marker, grab, remove_grabbed, clear,
                   options, choose_folder, export, open_folder
"""

import os
import time
import uuid

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog

from core import marker_colors
from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage

from . import resolve_ext

EXPORT_FORMATS = {"JPEG": "jpg", "PNG": "png", "TIFF": "tif"}
DEFAULT_PREFIX = "Still_"
POLL_MS = 2000
LOG_LIMIT = 60
DEFAULTS = {"format": "JPEG", "prefix": DEFAULT_PREFIX, "folder": "", "delete_after": False, "color": "Blue"}


class StillsExporterPage(WebToolPage):
    tool_id = "stills_exporter"
    display_name = "Stills Exporter"
    category = "Export & Delivery"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, DEFAULTS)
        self.grabbed = []          # [{"id", "timecode", "color", "name", "still"}] - this session only
        self.timeline = None       # name, when one is open
        self.markers = []
        self.problem = ""
        self._signature = None
        self._log = []
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_resolve)
        self._poll.start()

    def web_ready(self):
        self._push_state()
        self._push_grabbed()
        self._push_options()
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
                self._add_log(f"Couldn't connect: {exc}", "error")
                return None
        return self.host.controller if getattr(self.host, "connected", False) else None

    def _poll_resolve(self):
        if self.isVisible():
            self._read(connect=False)

    def _read(self, connect):
        controller = self._controller(connect)
        name, markers, problem = None, [], ""
        if controller is None:
            problem = self.problem if connect else "Not connected to Resolve."
        else:
            try:
                name, markers = resolve_ext.timeline_markers(controller)
            except ResolveConnectionError as exc:
                problem = str(exc)
            except Exception as exc:  # noqa: BLE001 - shown to the user
                problem = f"Couldn't read the timeline: {exc}"
        signature = repr((name, markers, problem))
        if signature != self._signature:
            self._signature = signature
            self.timeline, self.markers, self.problem = name, markers, problem
            self._push_state()
        return controller

    # --------------------------------------------------------------- view --

    @property
    def color(self):
        color = self.settings.get("color")
        return color if color in marker_colors.NAMES else "Blue"

    def _push_state(self):
        self.emit("state", {
            "connected": self._controller(connect=False) is not None,
            "timeline": self.timeline or "",
            "problem": self.problem,
            "colors": marker_colors.color_counts({m["frame"]: m for m in self.markers}),
            "color": self.color,
            "markers": [m for m in self.markers if m["color"] == self.color],
        })

    def _push_grabbed(self):
        self.emit("grabbed", [{k: g[k] for k in ("id", "timecode", "color", "name")} for g in self.grabbed])

    def _push_options(self):
        s = self.settings
        self.emit("options", {
            "formats": list(EXPORT_FORMATS),
            "format": s.get("format") if s.get("format") in EXPORT_FORMATS else "JPEG",
            "prefix": s.get("prefix", DEFAULT_PREFIX),
            "folder": s.get("folder") or "",
            "delete_after": bool(s.get("delete_after")),
        })

    def _add_log(self, text, kind="info"):
        self._log.append({"time": time.strftime("%H:%M:%S"), "text": text, "kind": kind})
        del self._log[:-LOG_LIMIT]
        self.emit("log", self._log)

    def _fail(self, title, exc):
        self._add_log(f"{title}: {exc}", "error")
        self.emit("alert", {"title": title, "text": str(exc)})

    # ------------------------------------------------------------ actions --

    def on_refresh(self, _payload=None):
        self._signature = None
        self._read(connect=True)

    def on_options(self, payload):
        payload = payload or {}
        s = self.settings
        if payload.get("format") in EXPORT_FORMATS:
            s["format"] = payload["format"]
        if "prefix" in payload:
            s["prefix"] = str(payload["prefix"])
        if "delete_after" in payload:
            s["delete_after"] = bool(payload["delete_after"])
        if payload.get("color") in marker_colors.NAMES:
            s["color"] = payload["color"]
            self._push_state()
        s.save()
        self._push_options()

    def on_choose_folder(self, _payload=None):
        folder = QFileDialog.getExistingDirectory(self, "Choose export folder", self.settings.get("folder") or "")
        if folder:
            self.settings["folder"] = os.path.normpath(folder)
            self.settings.save()
            self._push_options()

    def on_add_marker(self, payload):
        controller = self._controller(connect=True)
        if controller is None:
            return
        color = self.color
        name = str((payload or {}).get("name") or "").strip()
        try:
            timecode = resolve_ext.add_marker_at_playhead(controller, color, name=name)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return self._fail("Couldn't add the marker", exc)
        self._add_log(f"Added a {color} marker at {timecode}.", "ok")
        self.emit("toast", {"text": f"{color} marker added at {timecode}"})
        self._signature = None
        self._read(connect=False)

    def on_grab(self, _payload=None):
        controller = self._controller(connect=True)
        if controller is None:
            return
        color = self.color
        self._add_log(f"Grabbing stills at every {color} marker…")
        self.host.set_busy(True, f"Grabbing stills at the {color} markers…")
        try:
            grabbed = resolve_ext.grab_stills_for_color(controller, color, log=self._add_log)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return self._fail("Couldn't grab stills", exc)
        finally:
            self.host.set_busy(False)
        names = {m["timecode"]: m["name"] for m in self.markers}
        for timecode, still in grabbed:
            self.grabbed.append({"id": uuid.uuid4().hex, "timecode": timecode, "color": color,
                                 "name": names.get(timecode, ""), "still": still})
        self._push_grabbed()
        self.emit("toast", {"text": f"Grabbed {len(grabbed)} still{'s' if len(grabbed) != 1 else ''} on the Color page"})

    def on_remove_grabbed(self, payload):
        """Off this session's list only - the gallery still has it."""
        gid = (payload or {}).get("id")
        self.grabbed = [g for g in self.grabbed if g["id"] != gid]
        self._push_grabbed()

    def on_clear(self, _payload=None):
        count = len(self.grabbed)
        self.grabbed = []
        self._push_grabbed()
        if count:
            self._add_log(f"Cleared {count} still(s) from the list – the gallery is untouched.")

    def on_export(self, payload):
        """The view confirms first when "delete after" is on (confirmed: true)."""
        s = self.settings
        folder = s.get("folder") or ""
        if not self.grabbed:
            return self.emit("alert", {"title": "Nothing to export", "text": "Grab some stills first (step 2)."})
        if not folder or not os.path.isdir(folder):
            return self.emit("alert", {"title": "Choose a folder", "text": "Choose the folder to export the stills to first."})
        delete_after = bool(s.get("delete_after"))
        if delete_after and not (payload or {}).get("confirmed"):
            return
        controller = self._controller(connect=True)
        if controller is None:
            return
        fmt_name = s.get("format") if s.get("format") in EXPORT_FORMATS else "JPEG"
        fmt = EXPORT_FORMATS[fmt_name]
        prefix = s.get("prefix") or DEFAULT_PREFIX
        count = len(self.grabbed)
        self._add_log(f"Exporting {count} still(s) as .{fmt}…")
        self.host.set_busy(True, "Exporting stills…")
        try:
            resolve_ext.export_stills(controller, [g["still"] for g in self.grabbed], folder, prefix, fmt,
                                      delete_after=delete_after, log=self._add_log)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            return self._fail("Export failed", exc)
        finally:
            self.host.set_busy(False)
        if delete_after:
            self.grabbed = []
            self._push_grabbed()
        self.emit("toast", {"text": f"Exported {count} still{'s' if count != 1 else ''}", "open_folder": True})

    def on_open_folder(self, _payload=None):
        folder = self.settings.get("folder") or ""
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
