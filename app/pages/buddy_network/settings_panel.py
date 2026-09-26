"""Buddy Network's section of the shell's Settings window, as fields (see
core/settings_form.py)."""

from core import settings_form as sf
from core.buddy_server import DEFAULT_SERVER_URL

from . import safety

SERVER_HINT = ("Leave this as it is unless you're testing your own server "
               "(e.g. ws://localhost:8765 with python -m server --dev).")


class NetworkSettingsMixin:
    def settings_fields(self):
        return [
            sf.heading("Buddy Network"),
            sf.text("server_url", "Server address", self.settings.get("server_url") or DEFAULT_SERVER_URL,
                    hint_text=SERVER_HINT, error=getattr(self, "_server_url_problem", "") or None),
            sf.check("notify_dms", "Tray notifications for new direct messages and buddy requests",
                     self.settings.get("notify_dms", True)),
            sf.check("keep_dms", "Keep a copy of my direct messages on this PC", self.settings.get("keep_dms"),
                     hint_text="So they stay after the server deletes them (30 days), and after a PC change if you "
                               "keep the files. Locked to your Windows account."),
            sf.buttons(("Saved chats…", "saved_chats")),
        ]

    def on_setting(self, key, value, ui):
        if key == "server_url":
            url = str(value or "").strip()
            self._server_url_problem = safety.check_server_url(url)
            if self._server_url_problem:
                return
            if url != self._server_url():
                self.settings["server_url"] = "" if url == DEFAULT_SERVER_URL else url
                self.settings.save()
                if self.settings.get("enabled") and self.client is not None:
                    self._connect()
            ui.status("Saved.", "success")
        elif key == "notify_dms":
            self.settings["notify_dms"] = bool(value)
            self.settings.save()
        elif key == "keep_dms":
            self._set_keep_dms(bool(value), ui)

    def on_settings_action(self, action, ui):
        if action == "saved_chats":
            # Saved chats is a panel in the page: close Settings and show it.
            ui.close()
            switch = getattr(self.host, "switch_tool", None)
            if switch is not None:
                switch(self.tool_id)
            self.open_saved_chats()

    def _set_keep_dms(self, on: bool, ui):
        self.settings["keep_dms"] = on
        self.settings.save()
        if on:
            if self.room_id.startswith("dm-"):
                self._keep(self.room_id, list(self.messages.values()))
            return
        url = self._server_url()
        if self.archive.conversations(url) and ui.confirm(
                self.display_name, "Also delete the direct messages already saved on this PC?\n\n"
                "If you keep them, you can still export or delete them under Saved chats.", "Delete them",
                danger=True):
            self.archive.delete_all(url)
