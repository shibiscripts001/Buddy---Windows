#!/usr/bin/env python3
"""
Time Tracker's section of the shell's Settings window, as fields (see
core/settings_form.py) - tracking mode, polling, idle detection, the global
shortcut, goals, the current project's rate, and backup/export/import of
all data. The page provides self.data_mgr, self.engine and
_apply_settings(), which re-reads everything saved here.

The project rate is two fields, for whichever project Resolve has open.
"""

import sys

from PySide6.QtWidgets import QFileDialog

from core import settings_form as sf

from .currencies import CURRENCIES, CURRENCY_CODES, DEFAULT_CURRENCY

POLL_INTERVAL_OPTIONS = [2, 5, 10, 30]
IDLE_THRESHOLD_OPTIONS = [2, 5, 10, 15, 30]
TOGGLES = ("idle_detection_enabled", "manual_tracking", "global_hotkey_enabled", "auto_backup_enabled",
           "remember_export_folder")
GOALS = {"weekly_goal_hours": 999.0, "monthly_goal_hours": 9999.0}
RATE_MAX = 1_000_000.0


def _closest(value, options):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return options[1]
    return min(options, key=lambda o: abs(o - value))


class TrackerSettingsMixin:
    def settings_fields(self):
        s = self.data_mgr.settings
        fields = [
            sf.heading("Time Tracker"),
            sf.select("poll_interval_seconds", "Check Resolve every",
                      _closest(s.get("poll_interval_seconds", 5), POLL_INTERVAL_OPTIONS),
                      [(n, f"{n} sec") for n in POLL_INTERVAL_OPTIONS]),
            sf.check("idle_detection_enabled", "Pause tracking when idle", s.get("idle_detection_enabled", True),
                     tooltip="Automatically stop the running entry (trimming off the idle time) after no "
                             "keyboard/mouse activity anywhere on this machine for the chosen duration. In manual "
                             "tracking mode you'll need to press Start again afterward; in auto-follow mode it "
                             "resumes on its own as soon as you're back."),
            sf.select("idle_threshold_minutes", "Idle threshold",
                      _closest(s.get("idle_threshold_minutes", 5), IDLE_THRESHOLD_OPTIONS),
                      [(n, f"{n} min") for n in IDLE_THRESHOLD_OPTIONS], indent=True),
            sf.check("manual_tracking", "Manual tracking", s.get("manual_tracking", False),
                     tooltip="Track time yourself with a Start/Pause/Stop button instead of auto-following "
                             "whatever project is open in DaVinci Resolve."),
        ]
        if sys.platform == "win32":
            fields.append(sf.check(
                "global_hotkey_enabled", "Global pause/resume shortcut (Ctrl+Alt+P)",
                s.get("global_hotkey_enabled", False),
                tooltip="Works even while DaVinci Resolve (or any other app) has focus – cycles Start (if manual "
                        "tracking is idle) -> Pause -> Resume. Off by default since a system-wide shortcut could "
                        "conflict with another app already using the same combination."))
        weekly, monthly = s.get("weekly_goal_hours", 0.0), s.get("monthly_goal_hours", 0.0)
        fields += [
            sf.line(),
            sf.heading("Goals & billing"),
            sf.number("weekly_goal_hours", "Weekly goal", f"{weekly:g}" if weekly else None, 0, 999, 1,
                      placeholder="10", suffix="hours (0 to disable)"),
            sf.number("monthly_goal_hours", "Monthly goal", f"{monthly:g}" if monthly else None, 0, 9999, 1,
                      placeholder="40", suffix="hours (0 to disable)"),
            *self._rate_fields(),
            sf.line(),
            sf.heading("Data management"),
            sf.check("auto_backup_enabled", "Automatic weekly backups", s.get("auto_backup_enabled", True),
                     tooltip="Silently saves a full backup (same format as Export all data) once a week to "
                             "~/.davinci_time_tracker/auto_backups – keeps the most recent 8, purely a safety net "
                             "in case entries.json is ever lost or corrupted."),
            sf.check("remember_export_folder", "Remember last export folder", s.get("remember_export_folder", False)),
            sf.hint("Moving to a new machine? Export or import your Time Tracker data."),
            sf.buttons(("Export all data…", "export_all"), ("Import data…", "import_all")),
        ]
        return fields

    def _rate_fields(self):
        project = self.engine.detected_project_name
        if not project:
            return [sf.info("Project rate", "No project is currently detected in DaVinci Resolve. Open a project "
                                            "there, then come back here to set its rate.")]
        info = self.data_mgr.get_project_rate_info(project)
        rate = info.get("rate") or 0.0
        currency = info.get("currency") if info.get("currency") in CURRENCY_CODES else DEFAULT_CURRENCY
        return [
            sf.info("Project rate", project),
            sf.number("project_rate", "Rate per hour", f"{rate:.2f}" if rate > 0 else None, 0, RATE_MAX, 2,
                      placeholder="0.00"),
            sf.select("project_currency", "Currency", currency, [(c, f"{c} ({sym})") for c, sym, _n in CURRENCIES]),
            sf.hint("Leave the rate at 0 to hide earnings for this project on Reports."),
        ]

    def on_setting(self, key, value, ui):
        s = self.data_mgr.settings
        if key == "poll_interval_seconds" and value in POLL_INTERVAL_OPTIONS:
            s[key] = value
        elif key == "idle_threshold_minutes" and value in IDLE_THRESHOLD_OPTIONS:
            s[key] = value
        elif key in TOGGLES:
            if key == "global_hotkey_enabled" and sys.platform != "win32":
                return
            s[key] = bool(value)
        elif key in GOALS:
            # Blank or not a number means off.
            s[key] = sf.parse_number(value, 0.0, GOALS[key]) or 0.0
        elif key in ("project_rate", "project_currency"):
            project = self.engine.detected_project_name
            if not project:
                return
            info = self.data_mgr.get_project_rate_info(project)
            rate, currency = info.get("rate") or 0.0, info.get("currency") or DEFAULT_CURRENCY
            if key == "project_rate":
                rate = sf.parse_number(value, 0.0, RATE_MAX) or 0.0
            elif value in CURRENCY_CODES:
                currency = value
            self.data_mgr.set_project_rate(project, rate, currency)
            self._apply_settings()   # earnings figures follow the rate
            return
        else:
            return
        self.data_mgr.save_settings()
        self._apply_settings()

    def on_settings_action(self, action, ui):
        if action == "export_all":
            self._export_all_data(ui)
        elif action == "import_all":
            self._import_all_data(ui)

    def _export_all_data(self, ui):
        default_path = self.data_mgr.default_export_path("time_tracker_backup.json")
        path, _ = QFileDialog.getSaveFileName(ui.parent, "Export all data", default_path, "JSON files (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            self.data_mgr.export_backup(path)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return ui.alert("Export failed", f"Could not export data:\n{exc}")
        self.data_mgr.remember_export_folder(path)
        ui.alert("Export complete",
                 f"Exported {len(self.data_mgr.entries)} entries and your settings to:\n{path}\n\n"
                 "Copy this file to the new machine and use Import data there.")

    def _import_all_data(self, ui):
        start_dir = self.data_mgr.get_export_start_dir()
        path, _ = QFileDialog.getOpenFileName(ui.parent, "Import data", start_dir, "JSON files (*.json)")
        if not path:
            return
        if not ui.confirm("Import data",
                          f"Import time entries and settings from:\n{path}\n\n"
                          "Entries already present (matched by id) are left alone – nothing already here gets "
                          "duplicated or overwritten. Your tracking mode, poll interval, and other saved Time "
                          "Tracker settings will be replaced with the ones from this file. Continue?", "Import"):
            return
        try:
            added, skipped = self.data_mgr.import_backup(path)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            return ui.alert("Import failed", f"Could not import data:\n{exc}")
        self._apply_settings()
        message = f"Imported {added} new {'entry' if added == 1 else 'entries'}."
        if skipped:
            message += f" ({skipped} already present, skipped.)"
        ui.alert("Import complete", message)
