#!/usr/bin/env python3
"""
Project Setup's section of the shell's Settings window, as fields (see
core/settings_form.py). Writes go straight to data_mgr
(~/.resolve_bin_generator/settings.json), then the page redraws so its own
copies of the same toggles agree.
"""

from PySide6.QtWidgets import QFileDialog

from core import settings_form as sf
from core.i18n import tr

TOGGLES = ("import_to_master", "collapse_delete_silent_audio", "shift_close_gaps")


class ProjectSetupSettingsMixin:
    def settings_fields(self):
        s = self.data_mgr.settings
        return [
            sf.heading("Project Setup"),
            sf.check("import_to_master", "Send imported folders to Master level", s.get("import_to_master", False),
                     tooltip="When off, Import folder creates its bin inside whichever bin is currently selected "
                             "in the Media Pool instead."),
            sf.text("ffmpeg_path", "ffmpeg path", s.get("ffmpeg_path", ""), placeholder="Blank = find it automatically",
                    browse="browse_ffmpeg",
                    hint_text="Needed to read audio: Sync everything uses it to place clips that carry no timecode, "
                              "and Collapse uses it to find silent ones. Leave blank unless ffmpeg is somewhere "
                              "unusual."),
            sf.check("collapse_delete_silent_audio", "Delete silent audio clips when collapsing",
                     s.get("collapse_delete_silent_audio", False),
                     tooltip="Checks each audio clip's actual trimmed content and deletes it outright if it never "
                             "carries anything above the noise floor. Needs ffmpeg."),
            sf.check("shift_close_gaps", "Close the gaps between clips when shifting", s.get("shift_close_gaps", False),
                     tooltip="The Sync tab's Shift step also removes the dead air between blocks of footage."),
        ]

    def on_setting(self, key, value, ui):
        if key in TOGGLES:
            self._set_setting(key, bool(value))
        elif key == "ffmpeg_path":
            self._set_setting("ffmpeg_path", str(value or "").strip())

    def on_settings_action(self, action, ui):
        if action == "browse_ffmpeg":
            path, _ = QFileDialog.getOpenFileName(ui.parent, tr("Choose ffmpeg executable"))
            if path:
                self._set_setting("ffmpeg_path", path)
