#!/usr/bin/env python3
"""
The ONE Settings window for the whole shell: pages on a rail (General,
Look, AI, Tools, About - core/settings_form.py GROUPS). The shell's own
pages are settings_form.shell_pages(); every tool adds its own through
ToolPage.settings_pages() (pages/base.py) - under Tools, and under AI for
what it does with models - whichever tool is open. The AI group opens with
the Model library, every tool's ToolPage.ai_models() in one list, and ends
with Privacy and safety, their ToolPage.ai_jobs().

A web window (app/web/shell/settings/): the fields are data, and every
change comes back here to be checked, saved and applied at once - there's
no Apply button. Search is the view's own, over the fields it was sent.

Protocol:
    to the view    settings {groups, pages, [open]}, status
    from the view  set {section, key, value}, action {section, action}
                   - section is "shell", or the tool id whose page it is
"""

import os
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices

from core import crash_log, link_bar, startup_manager
from core import settings_form as sf
from core.i18n import get_i18n
from core.message_dialog import alert, confirm
from core.web_page import WEB_COMMON_DIR, WebDialog

# The AI pages in order, whichever tool gives them; any other comes before Privacy.
AI_ORDER = ["ai_library", "ai_chat", "ai_search", "ai_transcription", "ai_translation"]
BUDDY_DIR = Path.home() / ".buddy"
REFRESH_MS = 250

_OPEN = []      # the Settings windows on screen (one, in practice)


def refresh_open(owner=None):
    """Something a tool shows in Settings changed by itself - a download's
    progress, a check finishing: an open Settings window redraws, at most
    a few times a second. owner: that tool's id (None: everything)."""
    for dialog in list(_OPEN):
        try:
            dialog.refresh_soon(owner)
        except RuntimeError:            # already destroyed
            _OPEN.remove(dialog)


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


def gigabytes(size):
    return f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{max(1, round(size / 1e6))} MB"


class SettingsDialog(WebDialog):
    web_dir = os.path.join(WEB_COMMON_DIR, "shell", "settings")

    def __init__(self, parent, shared_settings, on_apply, active_page=None):
        self.shared_settings = shared_settings
        self.on_apply = on_apply
        self.main_window = parent
        self.active_page = active_page
        self.ui = SettingsUI(self)
        self._built = {}            # tool id -> {"pages", "models", "jobs"}
        self._opened = False
        self._dirty = set()
        super().__init__(parent, parent, "Settings", (940, 680))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh_now)
        _OPEN.append(self)

    def done(self, result):
        if self in _OPEN:
            _OPEN.remove(self)
        self._timer.stop()
        super().done(result)

    def web_ready(self):
        self.push()

    # ------------------------------------------------------------ tools --

    def tools(self):
        """Every tool page, in the rail's order - and the one on screen."""
        pages = getattr(self.main_window, "pages", None)
        tools = list(pages.values()) if isinstance(pages, dict) else []
        if self.active_page is not None and self.active_page not in tools:
            tools.append(self.active_page)
        return tools

    def tool(self, section):
        if section == "tool":            # the tool on screen (how it used to be addressed)
            return self.active_page
        return next((t for t in self.tools() if getattr(t, "tool_id", None) == section), None)

    def _build(self, tool):
        """One tool's pages, models and jobs - nothing, if they fail to
        build (logged): one tool's bug never takes Settings with it."""
        owner = getattr(tool, "tool_id", "tool")
        name = getattr(tool, "display_name", owner)
        built = {"pages": [], "models": [], "jobs": []}
        try:
            for p in getattr(tool, "settings_pages", lambda: [])() or []:
                group = p.get("group") if p.get("group") in sf.GROUP_IDS else "tools"
                fields = [f for f in p.get("fields") or [] if f]
                # The page is titled with the tool's name already.
                if fields and fields[0].get("kind") == "heading" and fields[0].get("text") == p.get("title", name):
                    fields = fields[1:]
                built["pages"].append({"id": p.get("id") or owner, "group": group, "title": p.get("title") or name,
                                       "subtitle": p.get("subtitle") or "", "owner": owner, "fields": fields})
            built["models"] = [dict(r, owner=r.get("owner") or owner)
                               for r in getattr(tool, "ai_models", lambda: [])() or []]
            built["jobs"] = [dict(j, owner=owner) for j in getattr(tool, "ai_jobs", lambda: [])() or []]
        except Exception:  # noqa: BLE001 - shown as nothing, logged
            traceback.print_exc()
            crash_log.trail("settings", f"{owner}: {traceback.format_exc(limit=1).strip()[-300:]}")
        return built

    # ------------------------------------------------------------ pages --

    def _autostart(self):
        try:
            return startup_manager.is_enabled()
        except OSError:
            return None

    def pages(self, owners=None):
        """Every page, in the rail's order. owners: rebuild only these
        tools' (the rest as last built)."""
        tools = self.tools()
        for t in tools:
            owner = getattr(t, "tool_id", "tool")
            if owners is None or owner in owners or owner not in self._built:
                self._built[owner] = self._build(t)
        built = [self._built[getattr(t, "tool_id", "tool")] for t in tools]
        shell = [dict(p, owner="shell") for p in
                 sf.shell_pages(self.shared_settings, self._autostart(), getattr(self.main_window, "updates", None))]
        tool_pages = [p for b in built for p in b["pages"]]
        models = [r for b in built for r in b["models"]]
        jobs = [j for b in built for j in b["jobs"]]
        ai = [p for p in tool_pages if p["group"] == "ai"]
        ai.sort(key=lambda p: AI_ORDER.index(p["id"]) if p["id"] in AI_ORDER else len(AI_ORDER))
        if models or ai:
            ai = [self._library(models), *ai, self._privacy(jobs)]
        out = []
        for group in sf.GROUP_IDS:
            out += [p for p in shell if p["group"] == group]
            out += ai if group == "ai" else [p for p in tool_pages if p["group"] == group]
        seen = set()
        for p in out:                    # ids are what the view goes by: never two alike
            if p["id"] in seen:
                p["id"] = f"{p['owner']}:{p['id']}"
            seen.add(p["id"])
        return out

    def _library(self, rows):
        """Every model Buddy uses, the space they take, and getting them."""
        kinds = {}
        for r in rows:
            if r.get("where") == "local" and r.get("bytes"):
                kinds[r.get("kind", "")] = kinds.get(r.get("kind", ""), 0) + r["bytes"]
        total = sum(kinds.values())
        fields = []
        if total:
            fields.append(sf.storage(f"{gigabytes(total)} on this PC",
                                     [{"label": kind, "size": gigabytes(size), "bytes": size, "tone": kind.lower()}
                                      for kind, size in kinds.items()], str(BUDDY_DIR)))
        # What's here (or in use in the cloud) first, what can be fetched after.
        rows = sorted(rows, key=lambda r: r.get("where") == "missing")
        fields += [
            sf.models(rows, filters=True) if rows else sf.hint("No tool here uses a model."),
            sf.buttons(("Open Buddy's folder", "open_buddy_folder")),
        ]
        return sf.page("ai_library", "ai", "Model library", fields,
                       "Every model Buddy can use, on this PC or in the cloud, and what uses it.") | {"owner": "shell"}

    def _privacy(self, jobs):
        """Where each AI job's data goes, and how what Buddy downloads is checked."""
        rows = []
        for j in jobs:
            where = j.get("where")
            text = j.get("text") or {"local": "On this PC", "cloud": "Leaves this PC", "off": "Off"}.get(where, "")
            tone = j.get("tone", "ok" if where == "local" else "warn" if where == "cloud" else "")
            row = sf.status(j.get("label", ""), text, tone, j.get("detail"))
            row["page"] = j.get("page")
            rows.append(row)
        return sf.page("ai_privacy", "ai", "Privacy and safety", [
            sf.heading("Where your data goes"),
            *(rows or [sf.hint("No tool here uses AI.")]),
            sf.heading("How downloads are checked"),
            sf.hint("Every model Buddy downloads is pinned to one exact version, and each of its files is checked "
                    "against the SHA-256 checksum Buddy expects before it's used. Model files hold no code."),
            sf.hint("The transcription engine installs only exact package versions, each checked against its "
                    "published hash, and Windows Defender scans it before it's used. llama.cpp, which runs "
                    "the search model, is checked against its published checksum and scanned the same way."),
            sf.hint("A copy from somewhere else – a folder you picked, another app's download – shows as Not "
                    "verified when its files aren't exactly the expected ones."),
        ], "What leaves this computer, and what Buddy checks.") | {"owner": "shell"}

    def _first_page(self, pages):
        """The tool on screen's first page, or General."""
        owner = getattr(self.active_page, "tool_id", None)
        mine = [p for p in pages if p["owner"] == owner]
        return mine[0]["id"] if mine else "general"

    def push(self, owners=None):
        pages = self.pages(owners)
        data = {"groups": sf.GROUPS, "pages": pages}
        if not self._opened:
            self._opened = True
            data["open"] = self._first_page(pages)
        self.emit("settings", data)

    def refresh_soon(self, owner=None):
        self._dirty.add(owner)
        if not self._timer.isActive():
            self._timer.start()

    def _refresh_now(self):
        dirty, self._dirty = self._dirty, set()
        self.push(None if None in dirty else dirty)

    # ----------------------------------------------------------- actions --

    def on_set(self, payload):
        payload = payload or {}
        key, value = str(payload.get("key") or ""), payload.get("value")
        section = payload.get("section") or "shell"
        if section in ("shell", "language"):
            self._set_shell(key, value)
        else:
            tool = self.tool(section)
            if tool is not None:
                tool.on_setting(key, value, self.ui)
        self.push()

    def _set_shell(self, key, value):
        effect = sf.apply_shell(self.shared_settings, key, value)
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
        if effect == "updates":
            shell = self.main_window
            if hasattr(shell, "set_updates_enabled"):
                shell.set_updates_enabled(bool(value))         # also stops/starts checking
            else:
                self.shared_settings["updates_enabled"] = bool(value)
                self.shared_settings.save()
            return
        if effect == "linkbar":
            shell = self.main_window
            if hasattr(shell, "set_link_bar_visible"):
                shell.set_link_bar_visible(bool(value))        # also makes the bar, the first time
            else:
                self.shared_settings[link_bar.SHOW_KEY] = bool(value)
                self.shared_settings.save()
            return
        if effect == "language":
            self.shared_settings.save()
            # Every open view (this window too) gets the new language's
            # strings and redraws in it - see core/web_page.py.
            get_i18n().language = value
            return
        if effect == "autostart":
            # A real side effect straight away (a registry write).
            try:
                startup_manager.set_enabled(bool(value))
            except Exception as exc:  # noqa: BLE001 - reported to the user
                alert(self, "Startup setting failed",
                      f"Could not enable launching at startup:\n{exc}" if value
                      else f"Could not disable launching at startup:\n{exc}")
            return
        self.shared_settings.save()
        if effect in ("theme", "window"):
            self.on_apply()
            if effect == "theme":
                self.on_theme_changed()

    def on_action(self, payload):
        payload = payload or {}
        action = str(payload.get("action") or "")
        section = payload.get("section") or "shell"
        if section not in ("shell", "language"):
            tool = self.tool(section)
            if tool is not None:
                tool.on_settings_action(action, self.ui)
        elif action == "close":
            self.accept()
            return
        elif action == "organize" and hasattr(self.main_window, "open_nav_organizer"):
            self.main_window.open_nav_organizer(self)
        elif action == "check_updates" and hasattr(self.main_window, "check_updates_now"):
            self.main_window.check_updates_now(self)
        elif action == "roll_back_update" and hasattr(self.main_window, "roll_back_update"):
            self.main_window.roll_back_update(self)
        elif action == "open_buddy_folder":
            BUDDY_DIR.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(BUDDY_DIR)))
        elif action == "reset_theme":
            sf.reset_theme(self.shared_settings)
            self.shared_settings.save()
            self.on_apply()
            self.on_theme_changed()
        self.push()
