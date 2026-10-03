#!/usr/bin/env python3
"""
Essentials - small everyday tools on one tab, each a sub-tab of its own
page (web/index.html): a calculator with a timecode mode, a data rate /
storage calculator, an aspect ratio calculator, a stopwatch and countdown
timer, a world clock, and notes - general ones, and a set per project.

The maths all happens in the view (essentials.js) - there is nothing here
for Python to work out. Python keeps what should outlast a restart: the
open sub-tab, each calculator's last inputs, the calculator's history and
the notes. It also owns the countdown's deadline, on a QTimer,
so the timer goes off on time while another tool is on screen (a hidden
web view's own timers are throttled) and can say so in a tray note.

Notes: "General" (settings["notes"], the scratchpad from before there
were projects) and one text per project name (settings["project_notes"]).
Which Resolve project is open comes from Time Tracker, which already asks
Resolve every few seconds in a throwaway process (its ResolveBridge) - so
this page never calls Resolve itself. With "Follow the open project" on,
opening another project in Resolve switches the notes to that project's.

World clock: the clocks are a list of {zone (an IANA name such as
"America/Los_Angeles"), label} in settings["clocks"]; the view does all
the time zone work with the browser's own Intl data.

Protocol:
    to the view    state, notes_state, countdown, countdown_done
    from the view  tab, pref, notes, notes_scope, notes_follow, history,
                   clocks, countdown, copy
"""

import os
import re
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication

from core.web_page import WebToolPage

TABS = ("calc", "data", "aspect", "timer", "clock", "notes")
DEFAULTS = {"tab": "calc", "prefs": {}, "notes": "", "history": [],
            "project_notes": {}, "notes_scope": "", "notes_follow": True, "clocks": None}
# Until the clock list is first changed, a few cities to start from.
DEFAULT_CLOCKS = [{"zone": "America/Los_Angeles", "label": ""}, {"zone": "America/New_York", "label": ""},
                  {"zone": "Europe/London", "label": ""}]
MAX_CLOCKS = 24
MAX_CLOCK_LABEL = 60
ZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")
MAX_PREFS = 80
MAX_PREF_KEY = 40
MAX_PREF_TEXT = 200
MAX_NOTES = 1_000_000
MAX_PROJECTS = 500
MAX_PROJECT_NAME = 200
MAX_HISTORY = 20
MAX_HISTORY_TEXT = 300
MAX_COUNTDOWN_MS = 100 * 3600 * 1000


def clean_pref(value):
    """A pref is one plain value - a field's text, a number or a tick."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if value == value and abs(value) < 1e15 else None
    if isinstance(value, str):
        return value[:MAX_PREF_TEXT]
    return None


def clean_project(name) -> str:
    """A project's name as a notes key: "" (General) when it isn't one."""
    return " ".join(name.split())[:MAX_PROJECT_NAME] if isinstance(name, str) else ""


def clean_project_notes(raw) -> dict:
    out = {}
    for name, text in raw.items() if isinstance(raw, dict) else ():
        name = clean_project(name)
        if name and isinstance(text, str) and text and len(out) < MAX_PROJECTS:
            out[name] = text[:MAX_NOTES]
    return out


def clean_clocks(raw) -> list[dict]:
    """The world clock's list: real-looking zone names, short labels, no repeats."""
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        zone, label = item.get("zone"), item.get("label")
        if not isinstance(zone, str) or len(zone) > 64 or not ZONE_NAME.match(zone) or zone in seen:
            continue
        seen.add(zone)
        out.append({"zone": zone, "label": " ".join(label.split())[:MAX_CLOCK_LABEL] if isinstance(label, str) else ""})
    return out[:MAX_CLOCKS]


def clean_history(raw) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("expr"), str) and isinstance(item.get("result"), str):
            out.append({"expr": item["expr"][:MAX_HISTORY_TEXT], "result": item["result"][:MAX_HISTORY_TEXT],
                        "mode": "tc" if item.get("mode") == "tc" else "std"})
    return out[:MAX_HISTORY]


class EssentialsPage(WebToolPage):
    tool_id = "essentials"
    display_name = "Essentials"
    category = "Essentials"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, dict(DEFAULTS))
        prefs = self.settings.get("prefs")
        self.prefs = {str(k)[:MAX_PREF_KEY]: clean_pref(v) for k, v in prefs.items()} if isinstance(prefs, dict) else {}
        self.history = clean_history(self.settings.get("history"))
        saved_clocks = self.settings.get("clocks")
        self.clocks = clean_clocks(saved_clocks) if saved_clocks is not None else [dict(c) for c in DEFAULT_CLOCKS]
        self.project_notes = clean_project_notes(self.settings.get("project_notes"))
        self.notes_scope = clean_project(self.settings.get("notes_scope"))
        self.open_project = ""        # the project open in Resolve, per Time Tracker
        self._project_hooked = False
        # The countdown: running until ends_at (epoch seconds), or paused
        # with left_ms to go, or idle. total_ms is what it was set to.
        self.ends_at = None
        self.left_ms = 0
        self.total_ms = 0
        self._alarm = QTimer(self)
        self._alarm.setSingleShot(True)
        self._alarm.timeout.connect(self._countdown_check)
        QTimer.singleShot(0, self._hook_project)   # once every page is built

    def web_ready(self):
        tab = self.settings.get("tab")
        self.emit("state", {
            "tab": tab if tab in TABS else "calc",
            "prefs": self.prefs,
            "history": self.history,
            "clocks": self.clocks,
        })
        self._push_notes()
        self._push_countdown()

    # ------------------------------------------------------------- saving --

    def on_tab(self, payload):
        tab = (payload or {}).get("tab")
        if tab in TABS and tab != self.settings.get("tab"):
            self.settings["tab"] = tab
            self.settings.save()

    def on_pref(self, payload):
        """One field's value, as typed - so each calculator opens as it was left."""
        payload = payload or {}
        key = payload.get("key")
        if not isinstance(key, str) or not key or len(key) > MAX_PREF_KEY:
            return
        if key not in self.prefs and len(self.prefs) >= MAX_PREFS:
            return
        value = clean_pref(payload.get("value"))
        if self.prefs.get(key) == value:
            return
        self.prefs[key] = value
        self.settings["prefs"] = dict(self.prefs)
        self.settings.save()

    # -------------------------------------------------------------- notes --

    def _notes_text(self, scope) -> str:
        return self.project_notes.get(scope, "") if scope else str(self.settings.get("notes") or "")

    def _push_notes(self):
        projects = set(self.project_notes)
        projects.update(p for p in (self.open_project, self.notes_scope) if p)
        self.emit("notes_state", {
            "scope": self.notes_scope,
            "text": self._notes_text(self.notes_scope),
            "projects": sorted(projects, key=str.casefold),
            "open": self.open_project,
            "follow": bool(self.settings.get("notes_follow", True)),
        })

    def on_notes(self, payload):
        """Text typed into one set of notes - the scope it was typed in, which
        may no longer be the one showing."""
        payload = payload or {}
        text, scope = payload.get("text"), clean_project(payload.get("scope"))
        if not isinstance(text, str):
            return
        text = text[:MAX_NOTES]
        listed = scope in self.project_notes
        if not scope:
            self.settings["notes"] = text
        elif text:
            if scope not in self.project_notes and len(self.project_notes) >= MAX_PROJECTS:
                return
            self.project_notes[scope] = text
        else:
            self.project_notes.pop(scope, None)   # emptied: it leaves the list unless it's open
        self.settings["project_notes"] = dict(self.project_notes)
        self.settings.save()
        # Saved after a switch away from it: the dropdown gains or loses it.
        if scope and scope != self.notes_scope and listed != (scope in self.project_notes):
            self._push_notes()

    def on_notes_scope(self, payload):
        self._show_scope(clean_project((payload or {}).get("scope")))

    def on_notes_follow(self, payload):
        on = bool((payload or {}).get("on"))
        self.settings["notes_follow"] = on
        self.settings.save()
        if on and self.open_project:
            self._show_scope(self.open_project)
        else:
            self._push_notes()

    def _show_scope(self, scope):
        self.notes_scope = scope
        self.settings["notes_scope"] = scope
        self.settings.save()
        self._push_notes()

    def on_shown(self):
        self._hook_project()

    def _hook_project(self):
        """Listen to Time Tracker's "which project is open" polls."""
        if self._project_hooked:
            return
        tracker = getattr(self.host, "pages", {}).get("time_tracker")
        bridge = getattr(tracker, "bridge", None)
        if bridge is None:
            return
        self._project_hooked = True
        bridge.project_detected.connect(self._project_detected)
        engine = getattr(tracker, "engine", None)
        self._project_detected(getattr(engine, "detected_project_name", None), True)

    def _project_detected(self, name, ok):
        if not ok:
            return
        name = clean_project(name)
        if name == self.open_project:
            return
        self.open_project = name
        if name and self.settings.get("notes_follow", True) and name != self.notes_scope:
            self._show_scope(name)
        else:
            self._push_notes()

    def on_clocks(self, payload):
        self.clocks = clean_clocks((payload or {}).get("items"))
        self.settings["clocks"] = self.clocks
        self.settings.save()

    def on_history(self, payload):
        self.history = clean_history((payload or {}).get("items"))
        self.settings["history"] = self.history
        self.settings.save()

    def on_copy(self, payload):
        """The page can't reach the clipboard itself (core/web_page.py turns
        that off), so a Copy button comes here."""
        text = (payload or {}).get("text")
        if isinstance(text, str) and text:
            QGuiApplication.clipboard().setText(text)

    # ---------------------------------------------------------- countdown --

    def on_countdown(self, payload):
        payload = payload or {}
        action = payload.get("action")
        now = time.time()
        if action == "start":
            if self.ends_at is not None:
                return
            if self.left_ms <= 0:
                ms = payload.get("ms")
                if not isinstance(ms, (int, float)) or isinstance(ms, bool) or not 0 < ms <= MAX_COUNTDOWN_MS:
                    return
                self.left_ms = self.total_ms = int(ms)
            self.ends_at = now + self.left_ms / 1000
            self._arm()
        elif action == "pause" and self.ends_at is not None:
            self.left_ms = max(0, int((self.ends_at - now) * 1000))
            self.ends_at = None
            self._alarm.stop()
        elif action == "reset":
            self.ends_at = None
            self.left_ms = self.total_ms = 0
            self._alarm.stop()
        else:
            return
        self._push_countdown()

    def _arm(self):
        # QTimer takes an int of ms; long countdowns re-arm on the way.
        left = max(0, int((self.ends_at - time.time()) * 1000))
        self._alarm.start(min(left, 3_600_000))

    def _countdown_check(self):
        if self.ends_at is None:
            return
        if time.time() < self.ends_at - 0.005:
            return self._arm()
        self.ends_at = None
        self.left_ms = 0
        total = self.total_ms
        self.total_ms = 0
        self._push_countdown()
        self.emit("countdown_done", {"total_ms": total})
        window = self.window()
        if not (window.isVisible() and window.isActiveWindow() and self.isVisible()):
            self.host.notify("Timer finished", "Your Essentials countdown is done.")

    def _push_countdown(self):
        self.emit("countdown", {
            "ends_at": None if self.ends_at is None else int(self.ends_at * 1000),
            "left_ms": self.left_ms,
            "total_ms": self.total_ms,
        })

    def on_app_quitting(self):
        self._alarm.stop()
