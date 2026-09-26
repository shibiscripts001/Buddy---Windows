#!/usr/bin/env python3
"""
Color Palette's section of the shell's Settings window, as fields (see
core/settings_form.py), plus the backup export/import it offers. The page
supplies data_mgr, i18n, refresh_all(), update_focus_overlay() and
apply_mini_palette_transparency().

Theme, window transparency and stay-on-top were the standalone's own
Settings rows; they're shell settings in Buddy.
"""

import json
import os

from PySide6.QtWidgets import QFileDialog

from core import settings_form as sf

from .i18n import LANGUAGES, tr

TRANSPARENCY_STEPS = ["100%", "95%", "90%", "85%", "80%", "75%"]
TOGGLES = ("hide_on_dropper", "remember_export_folder", "focus_mode_enabled")


class ColorPaletteSettingsMixin:
    def settings_fields(self):
        s = self.data_mgr.settings
        opacity = round(float(s.get("focus_mode_opacity", 0.85)) * 100)
        return [
            sf.heading("Color Palette Manager"),
            sf.select("mini_palette_transparency", tr("Mini palette window transparency:"),
                      s.get("mini_palette_transparency", "95%"), TRANSPARENCY_STEPS),
            sf.check("hide_on_dropper", tr("Hide app window during screen dropper sampling"),
                     s.get("hide_on_dropper", True)),
            sf.check("remember_export_folder", tr("Remember previous export folder"),
                     s.get("remember_export_folder", False)),
            sf.check("focus_mode_enabled", tr("Enable focus mode dimming overlay"), s.get("focus_mode_enabled", False),
                     tooltip="Dims the screen behind Buddy while this tool is open."),
            sf.slider("focus_mode_opacity", tr("Overlay opacity:"), max(10, min(100, opacity)), 10, 100, 100,
                      {v: f"{v}%" for v in range(10, 101)}),
            sf.slider("focus_mode_color_val", tr("Overlay shade (Black → white):"),
                      int(s.get("focus_mode_color_val", 128)), 0, 255, 128, {}),
            sf.buttons((tr("Export settings / backup…"), "export_backup"),
                       (tr("Import settings / backup…"), "import_backup")),
            sf.select("language", tr("Language:"), s.get("language", "English"), LANGUAGES),
            sf.hint("Language applies to this tool only."),
        ]

    def on_setting(self, key, value, ui):
        s = self.data_mgr.settings
        if key == "mini_palette_transparency" and value in TRANSPARENCY_STEPS:
            s[key] = value
            self.data_mgr.save_settings()
            self.apply_mini_palette_transparency()
        elif key in TOGGLES:
            s[key] = bool(value)
            self.data_mgr.save_settings()
            if key == "focus_mode_enabled":
                self.update_focus_overlay()
        elif key == "focus_mode_opacity" and isinstance(value, int) and 10 <= value <= 100:
            s[key] = value / 100.0
            self.data_mgr.save_settings()
            self.update_focus_overlay()
        elif key == "focus_mode_color_val" and isinstance(value, int) and 0 <= value <= 255:
            s[key] = value
            self.data_mgr.save_settings()
            self.update_focus_overlay()
        elif key == "language" and value in LANGUAGES:
            s[key] = value
            self.data_mgr.save_settings()
            self.i18n.language = value

    def on_settings_action(self, action, ui):
        if action == "export_backup":
            self._export_data_backup(ui)
        elif action == "import_backup":
            self._import_data_backup(ui)

    # Backup/restore: the standalone SettingsDialog's own methods.

    def _export_data_backup(self, ui):
        default_path = self.data_mgr.default_export_path("ColorPaletteManager_Backup.json")
        out_path, _ = QFileDialog.getSaveFileName(ui.parent, tr("Export settings & data"), default_path,
                                                  "JSON Files (*.json)")
        if not out_path:
            return
        self.data_mgr.remember_export_folder(out_path)
        payload = {
            "app": "Color Palette Manager",
            "export_version": 1,
            "settings": self.data_mgr.settings,
            "palettes": self.data_mgr.palettes,
            "palette_folders": self.data_mgr.palette_folders,
            "palette_custom_tags": self.data_mgr.palette_custom_tags,
            "palette_versions": self.data_mgr.palette_versions,
            "visualizer_overrides": self.data_mgr.visualizer_overrides,
        }
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:  # noqa: BLE001 - reported to the user
            return ui.alert(tr("Export error"), tr("Could not export backup:\n{error}").format(error=e))
        ui.status(tr("Successfully exported settings/backup to {filename}").format(
            filename=os.path.basename(out_path)), "success")

    def _import_data_backup(self, ui):
        in_path, _ = QFileDialog.getOpenFileName(ui.parent, tr("Import settings & data"), "", "JSON Files (*.json)")
        if not in_path:
            return
        try:
            with open(in_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict) or ("palettes" not in payload and "settings" not in payload):
                return ui.alert(tr("Import error"), tr("Invalid backup file format."))
            if not ui.confirm(tr("Import settings & data"),
                              "This replaces your palettes, folders, tags, versions and Color Palette settings "
                              "with the ones in the backup. Continue?", "Import", danger=True):
                return
            for key, save in (("settings", self.data_mgr.save_settings),
                              ("palettes", self.data_mgr.save_palette_data),
                              ("palette_folders", self.data_mgr.save_palette_folders),
                              ("palette_custom_tags", self.data_mgr.save_palette_custom_tags),
                              ("palette_versions", self.data_mgr.save_palette_versions),
                              ("visualizer_overrides", self.data_mgr.save_visualizer_overrides)):
                if key in payload:
                    setattr(self.data_mgr, key, payload[key])
                    save()
            self.refresh_all()
        except Exception as e:  # noqa: BLE001 - reported to the user
            return ui.alert(tr("Import error"), tr("Could not read backup file:\n{error}").format(error=e))
        ui.status(tr("Successfully imported settings & data!"), "success")
