#!/usr/bin/env python3
"""
The ONE Settings window for the whole shell - appearance/window prefs
apply to every tool page at once, and the tool on screen adds its own
section below (ToolPage.settings_fields(), pages/base.py). A web window
(app/web/shell/settings/): the fields are data from core/settings_form.py,
and every change comes back here to be checked, saved and applied at once -
there's no Apply button.

Protocol:
    to the view    settings, status
    from the view  set, action
"""

import os

from core import startup_manager
from core.settings_form import apply_shell, reset_theme, shell_fields
from core.message_dialog import alert, confirm
from core.web_page import WEB_COMMON_DIR, WebDialog


class SettingsUI:
    """What a tool's settings code can do from inside the window (the `ui`
    argument of on_setting / on_settings_action)."""

    def __init__(self, dialog):
        self._dialog = dialog

    @property
    def parent(self):
        """The window, for a file picker to sit over."""
        return self._dialog

    def refresh(self):
        self._dialog.push()

    def alert(self, title, text):
        alert(self._dialog, title, text)

    def confirm(self, title, text, ok="OK", danger=False):
        return confirm(self._dialog, title, text, ok, danger=danger)

    def status(self, text, tone=""):
        """A line at the foot of the window - "Saved.", "Exported..."."""
        self._dialog.emit("status", {"text": text, "tone": tone})

    def close(self):
        self._dialog.reject()


class SettingsDialog(WebDialog):
    web_dir = os.path.join(WEB_COMMON_DIR, "shell", "settings")

    def __init__(self, parent, shared_settings, on_apply, active_page=None):
        self.shared_settings = shared_settings
        self.on_apply = on_apply
        self.main_window = parent
        self.active_page = active_page
        self.ui = SettingsUI(self)
        super().__init__(parent, parent, "Settings", (560, 720))

    def web_ready(self):
        self.push()

    # ---------------------------------------------------------- sections --

    def _autostart(self):
        try:
            return startup_manager.is_enabled()
        except OSError:
            return None

    def sections(self):
        out = [{"id": "shell", "title": "", "fields": [f for f in shell_fields(self.shared_settings, self._autostart())
                                                         if f]}]
        page = self.active_page
        fields = page.settings_fields() if page is not None else None
        if fields:
            # Its own fields start with its heading.
            out.append({"id": "tool", "title": "", "fields": [f for f in fields if f]})
        return out

    def push(self):
        self.emit("settings", {"sections": self.sections()})

    # ----------------------------------------------------------- actions --

    def on_set(self, payload):
        payload = payload or {}
        key, value = str(payload.get("key") or ""), payload.get("value")
        if payload.get("section") == "tool":
            if self.active_page is not None:
                self.active_page.on_setting(key, value, self.ui)
        else:
            self._set_shell(key, value)
        self.push()

    def _set_shell(self, key, value):
        effect = apply_shell(self.shared_settings, key, value)
        if effect is None:
            return
        if effect == "announcements":
            shell = self.main_window
            if hasattr(shell, "set_announcements_enabled"):
                shell.set_announcements_enabled(bool(value))   # also stops/starts checking
            else:
                self.shared_settings["announcements_enabled"] = bool(value)
                self.shared_settings.save()
            return
        if effect == "autostart":
            # A real side effect straight away (a registry write).
            try:
                startup_manager.set_enabled(bool(value))
            except Exception as exc:  # noqa: BLE001 - reported to the user
                alert(self, "Startup setting failed",
                      f"Could not {'enable' if value else 'disable'} launching at startup:\n{exc}")
            return
        self.shared_settings.save()
        if effect in ("theme", "window"):
            self.on_apply()
            if effect == "theme":
                self.on_theme_changed()

    def on_action(self, payload):
        payload = payload or {}
        action = str(payload.get("action") or "")
        if payload.get("section") == "tool":
            if self.active_page is not None:
                self.active_page.on_settings_action(action, self.ui)
        elif action == "close":
            self.accept()
            return
        elif action == "organize" and hasattr(self.main_window, "open_nav_organizer"):
            self.main_window.open_nav_organizer(self)
        elif action == "reset_theme":
            reset_theme(self.shared_settings)
            self.shared_settings.save()
            self.on_apply()
            self.on_theme_changed()
        self.push()
