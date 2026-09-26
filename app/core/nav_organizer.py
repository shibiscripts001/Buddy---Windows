#!/usr/bin/env python3
"""
Settings > Window > Organize sidebar...: reorder the tools, show or hide
them, and add, rename or remove the dividers (headings or plain lines)
between them. A web window (app/web/shell/organizer/) editing a copy of the
layout with core/nav_layout.py's edits - Save hands it back, Cancel drops
it.

Hidden tools keep running (Time Tracker still tracks); at least one has to
stay visible.

Protocol:
    to the view    organizer
    from the view  edit, save, cancel
"""

import copy
import os

from core import nav_layout as nl
from core.web_page import WEB_COMMON_DIR, WebDialog, _theme_host

EDITS = {
    "move": lambda layout, p: nl.move(layout, p.get("from"), p.get("to")),
    "visible": lambda layout, p: nl.set_visible(layout, p.get("index"), p.get("visible")),
    "add": lambda layout, p: nl.add_divider(layout, p.get("index"), p.get("label")),
    "rename": lambda layout, p: nl.rename_divider(layout, p.get("index"), p.get("label")),
    "remove": lambda layout, p: nl.remove_divider(layout, p.get("index")),
    "show_all": lambda layout, p: nl.show_all(layout),
}


class NavOrganizerDialog(WebDialog):
    web_dir = os.path.join(WEB_COMMON_DIR, "shell", "organizer")

    def __init__(self, parent, layout, names, tokens=None, placeholder_ids=()):
        self.layout_copy = copy.deepcopy(layout)
        self.names = names
        self.placeholders = set(placeholder_ids)
        self.default = None
        self.result_layout = None
        self._note = ""
        super().__init__(_theme_host(parent), parent, "Organize sidebar", (620, 600))

    def set_default(self, layout):
        """What Reset to default goes back to."""
        self.default = copy.deepcopy(layout)

    def web_ready(self):
        self._push()

    def _push(self):
        rows = []
        for entry in self.layout_copy:
            if entry["type"] == nl.DIVIDER:
                rows.append({"type": "divider", "label": entry["label"]})
            else:
                rows.append({"type": "tool", "label": self.names.get(entry["id"], entry["id"]),
                             "visible": entry["visible"], "placeholder": entry["id"] in self.placeholders})
        self.emit("organizer", {"rows": rows, "note": self._note, "can_reset": self.default is not None})

    def on_edit(self, payload):
        payload = payload or {}
        action = str(payload.get("action") or "")
        if action == "reset" and self.default is not None:
            self.layout_copy = copy.deepcopy(self.default)
            self._note = "Back to the default layout – press Save to keep it."
        elif action in EDITS:
            problem = EDITS[action](self.layout_copy, payload)
            if problem != "nothing":
                self._note = problem
        self._push()

    def on_save(self, _payload=None):
        self.result_layout = copy.deepcopy(self.layout_copy)
        self.accept()

    def on_cancel(self, _payload=None):
        self.reject()
