#!/usr/bin/env python3
"""
Batch Clip Renamer - renames the clips in the open Media Pool bin (or just
the selected ones) by sequential numbering or find & replace. A web page
(core/web_page.py): the view is web/index.html + renamer.js.

Features: a live preview of every
targeted clip's current and new name, drawn from the same rules the
rename uses (renamer.plan), and an Undo for the last rename. Only the
Clip Name property is ever written - files on disk are never touched.

Safety: Rename re-reads the targets from Resolve immediately before
writing, and refuses (asking for another look) if they are not exactly
the clips and names the preview showed - the selection or a name may have
changed in Resolve since. So what gets applied is always what was on
screen.

Protocol:
    to the view    state, inputs, log, toast
    from the view  inputs, scope, refresh, connect, rename, undo
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QTimer

from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage

from . import renamer, resolve_ext
from .resolve_ext import SCOPE_BIN, SCOPE_SELECTED, TargetError

# How often the page checks whether the open bin or the selection changed,
# while it is on screen. Backed off if Resolve is slow to answer, so the
# polling can never make Buddy feel sluggish.
POLL_MS = 2000
SLOW_POLL_MS = 10000
SLOW_POLL_SECONDS = 0.25
LOG_LIMIT = 50
# Above this many clips the rename shows the shell's busy overlay.
BUSY_ABOVE = 20


class BatchClipRenamerPage(WebToolPage):
    tool_id = "batch_clip_renamer"
    display_name = "Batch Clip Renamer"
    category = "Editing Tools"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        # Scope deliberately starts on the whole bin every launch: which
        # clips are selected changes every session (see tools_kb.json).
        self.scope = SCOPE_BIN
        self.inputs = {"mode": renamer.MODE_SEQUENTIAL, "base": "", "start": "1",
                       "find": "", "replace": "", "match_case": True}
        self._clips = []
        self._names = []
        self._keys = []
        self._where = ""
        self._source_error = ""
        self._signature = None
        self._undo = None
        self._log = []
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_resolve)
        self._poll.start()

    def web_ready(self):
        self.emit("inputs", {**self.inputs, "scope": self.scope})
        self.emit("log", self._log)
        self._push_state()

    def on_shown(self):
        self._refresh(connect=False)

    # ------------------------------------------------------------- reading

    def _controller(self, connect):
        """The shell's controller. With connect, go through
        ensure_connected() (which reports a failure itself); without, only
        use a connection that already exists - polling and page switches
        must never pop up an error box."""
        if connect:
            try:
                return self.host.ensure_connected()
            except ResolveConnectionError:
                return None
        return self.host.controller if getattr(self.host, "connected", False) else None

    def _read(self, controller):
        """(clips, names, keys, where) for the current scope, or raises
        TargetError."""
        clips, where = resolve_ext.read_targets(controller, self.scope)
        return clips, [resolve_ext.clip_name(c) for c in clips], \
            [resolve_ext.clip_key(c) for c in clips], where

    def _refresh(self, connect):
        controller = self._controller(connect)
        self._clips, self._names, self._keys = [], [], []
        if controller is None:
            self._where, self._source_error, self._signature = "", "", None
        else:
            try:
                self._clips, self._names, self._keys, self._where = self._read(controller)
                self._source_error = ""
            except TargetError as exc:
                self._where, self._source_error = "", str(exc)
            except Exception as exc:  # noqa: BLE001 - a dropped connection, shown as such
                self._where, self._source_error = "", f"Could not read from Resolve ({exc})."
            try:
                self._signature = resolve_ext.target_signature(controller, self.scope)
            except Exception:
                self._signature = None
        self._push_state()

    def _poll_resolve(self):
        """Re-read when the open bin or the selection changes in Resolve, so
        the preview follows what the user is doing there."""
        if not self.isVisible():
            return
        controller = self._controller(connect=False)
        if controller is None:
            if self._names or self._source_error:
                self._refresh(connect=False)   # the connection went away
            return
        started = time.monotonic()
        try:
            signature = resolve_ext.target_signature(controller, self.scope)
        except Exception:
            return
        slow = time.monotonic() - started > SLOW_POLL_SECONDS
        self._poll.setInterval(SLOW_POLL_MS if slow else POLL_MS)
        if signature != self._signature:
            self._refresh(connect=False)

    # ------------------------------------------------------------ the view

    def _preview(self):
        rows, problem = renamer.plan(self._names, self.inputs)
        return rows, problem

    def _push_state(self):
        rows, problem = self._preview()
        changes = sum(1 for r in rows if r["changed"])
        self.emit("state", {
            "scope": self.scope,
            "connected": self._controller(connect=False) is not None,
            "where": self._where,
            "error": self._source_error,
            "rows": rows,
            "total": len(rows),
            "changes": changes,
            "skipped": sum(1 for r in rows if r.get("skipped")),
            "duplicates": renamer.duplicate_names(rows) if changes else [],
            "problem": {"field": problem[0], "message": problem[1]} if problem else None,
            "undo": {"count": len(self._undo["items"]), "where": self._undo["where"]} if self._undo else None,
        })

    def _add_log(self, text, kind="info"):
        self._log.append({"time": time.strftime("%H:%M"), "text": text, "kind": kind})
        del self._log[:-LOG_LIMIT]
        self.emit("log", self._log)

    # ------------------------------------------------------------- actions

    def on_inputs(self, payload):
        payload = payload or {}
        for key in self.inputs:
            if key in payload:
                self.inputs[key] = payload[key]
        self._push_state()

    def on_scope(self, payload):
        scope = (payload or {}).get("scope")
        if scope in (SCOPE_BIN, SCOPE_SELECTED) and scope != self.scope:
            self.scope = scope
            self._refresh(connect=False)

    def on_refresh(self, _payload):
        self._refresh(connect=True)

    def on_connect(self, _payload):
        self._refresh(connect=True)

    def on_rename(self, _payload):
        controller = self._controller(connect=True)
        if controller is None:
            self._refresh(connect=False)
            return
        try:
            clips, names, keys, where = self._read(controller)
        except TargetError as exc:
            self._add_log(str(exc), "error")
            self._refresh(connect=False)
            return
        if keys != self._keys or names != self._names:
            # Something changed in Resolve since the preview was drawn.
            # Show the new preview and let the user look before renaming.
            self._clips, self._names, self._keys, self._where = clips, names, keys, where
            self._push_state()
            self.emit("toast", {"text": "The clips changed in Resolve – check the preview, then Rename again."})
            return

        rows, problem = renamer.plan(names, self.inputs)
        todo = [(clip, row) for clip, row in zip(clips, rows) if row["changed"]]
        if problem or not todo:
            return

        busy = len(todo) > BUSY_ABOVE
        if busy:
            self.host.set_busy(True, f"Renaming {len(todo)} clips…")
        done, failed = [], []
        try:
            for n, (clip, row) in enumerate(todo, 1):
                ok = False
                try:
                    ok = bool(clip.SetClipProperty("Clip Name", row["new"]))
                except Exception:
                    ok = False
                (done if ok else failed).append((clip, row["old"], row["new"]))
                if busy and n % 25 == 0:
                    self.host.pump_busy(f"Renaming clips… {n} of {len(todo)}")
        finally:
            if busy:
                self.host.set_busy(False)

        if done:
            self._undo = {"items": done, "where": where}
            self._add_log(f"Renamed 1 clip in {where}." if len(done) == 1
                          else f"Renamed {len(done)} clips in {where}.", "success")
        if failed:
            names_failed = ", ".join(old for _c, old, _n in failed[:5])
            more = f" and {len(failed) - 5} more" if len(failed) > 5 else ""
            self._add_log(f"Resolve refused to rename {len(failed)}: {names_failed}{more}.", "error")
        self._refresh(connect=False)

    def on_undo(self, _payload):
        """Put back the names the last rename replaced - but only on clips
        still carrying the name it gave them. One renamed again since, in
        Resolve or here, is left alone rather than clobbered."""
        undo, self._undo = self._undo, None
        if not undo or self._controller(connect=True) is None:
            self._undo = undo
            self._push_state()
            return
        restored, skipped = 0, 0
        for clip, old, new in undo["items"]:
            try:
                if resolve_ext.clip_name(clip) == new and clip.SetClipProperty("Clip Name", old):
                    restored += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        text = ("Undid the rename: 1 clip back to its old name." if restored == 1
                else f"Undid the rename: {restored} clips back to their old name.")
        if skipped:
            text += f" {skipped} had been renamed again since and were left alone."
        self._add_log(text, "success" if restored else "info")
        self._refresh(connect=False)

    def on_app_quitting(self):
        self._poll.stop()
