#!/usr/bin/env python3
"""
The contract every tool page implements to live inside the Buddy shell.

Porting a standalone tool means taking its old MainWindow.init_ui() body
(minus the header/connection-row/settings-button chrome the shell now
owns once, for everyone) and putting it in build_ui() below. Everything a
page needs from the outside world - the shared Resolve connection, the
busy overlay, tool-specific settings - comes through `self.host`, so a
page never touches QApplication, its own connection state, or its own
theme application directly.
"""

from PySide6.QtWidgets import QWidget


class ToolPage(QWidget):
    # Unique slug - also used as the ToolSettings legacy directory name
    # when a page needs its own settings bucket (see core/settings_store.py).
    tool_id = None
    # Shown in the nav rail and as the page's own header title.
    display_name = None
    # One of the categories registered in registry.py, e.g. "Editing Tools".
    category = None
    # True only for the stand-in pages made by pages/placeholder.py. Lets
    # anything that reasons ABOUT the toolset (core/tools_kb.py, and through
    # it the chat agent) tell a working tool from one that just shows "Not
    # yet integrated", instead of guessing from the class name.
    is_placeholder = False

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.build_ui()

    def build_ui(self):
        """Build this page's own layout. Required override."""
        raise NotImplementedError

    def on_theme_changed(self):
        """Optional hook: called after the shell applies a new theme, for
        pages that cache anything theme-derived (e.g. a hand-painted
        icon or a chart color) and need to regenerate it."""

    def on_shown(self):
        """Optional hook: called each time the shell switches to this
        page, for pages that want to refresh data lazily rather than
        polling in the background."""

    def rail_badge(self):
        """Optional hook: a small mark beside this tool's name in the
        sidebar and on its taskbar button - "sound" (the Web tab playing
        something, with a pause button) or "paused" (a play button) - or
        None. Call host.refresh_badges() when it changes."""
        return None

    def rail_media(self):
        """Optional hook: the pause / play button beside a "sound" or
        "paused" mark was clicked."""

    def on_connection_changed(self, connected):
        """Optional hook: Buddy connected to Resolve, or lost it
        (host.connected is already the new value)."""

    def settings_fields(self):
        """Optional hook: this tool's own section of the shared Settings
        window, as a list of fields (see core/settings_form.py) - or None
        if it has no settings beyond the shell's. Built fresh each time
        the window shows it, so it always shows what's saved."""
        return None

    def settings_pages(self):
        """This tool's pages in the Settings window (core/settings_form.py
        page()): by default one page under Tools, of settings_fields(). A
        tool with AI settings gives pages in the "ai" group too."""
        fields = self.settings_fields()
        if not fields:
            return []
        return [{"id": self.tool_id, "group": "tools", "title": self.display_name, "fields": fields}]

    def ai_models(self):
        """The models this tool uses, as rows for Settings' Model library
        (core/settings_form.py models()) - [] if none."""
        return []

    def ai_jobs(self):
        """What this tool's AI does and where the data goes, for Settings'
        Privacy page: [{label, model, where ("local" / "cloud" / "off"),
        detail, [page]}]."""
        return []

    def on_setting(self, key, value, ui):
        """One of this tool's settings fields changed: check it, save it
        and apply it. `ui` (core/settings_dialog.py SettingsUI) can alert,
        confirm, show a status line or close the window. The fields are
        redrawn afterwards, so an error goes in the field's "error"."""

    def on_settings_action(self, action, ui):
        """A button in this tool's settings section was pressed."""

    def on_app_quitting(self):
        """Optional hook: called once, before the shell actually tears
        itself down - both on a normal close and when the tray's Quit
        action fires (see core/shell_window.py's closeEvent). For a page
        that keeps state running independent of which page is currently
        visible (e.g. Time Tracker's own timers), this is the one place to
        flush/close that state; on_shown()/on_theme_changed() only fire
        while a page is the one on screen."""


class ShellHost:
    """The interface a ToolPage's `self.host` actually satisfies, documented
    here since ShellWindow (core/shell_window.py) implements it directly
    rather than through a formal ABC.

    ensure_connected() -> ResolveController
        Raises ResolveConnectionError if Resolve isn't reachable. Reuses
        the shell's single existing connection when there is one, rather
        than reconnecting per page/per action.

    set_busy(is_busy, message=None)
        Shows/hides the shell-wide frosted-glass overlay (see
        core/busy_overlay.py) - blocks input across every page, not just
        the current one, while a long operation runs.

    pump_busy(message=None)
        Updates the overlay's caption and forces one repaint - for
        synchronous (non-worker-thread) long operations only.

    tool_settings(tool_id, defaults=None) -> ToolSettings
        Returns (creating if needed) this tool's own settings bucket.

    theme_tokens() -> dict
        Current resolved theme tokens, for pages that paint their own
        icons or chart colors to match.

    notify(title, message)
        A tray notification (does nothing without a tray icon). Used by
        Buddy Network for new direct messages and buddy requests.
    """
