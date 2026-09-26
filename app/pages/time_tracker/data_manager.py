#!/usr/bin/env python3
"""
Data Manager for Time Tracker - entries.json/settings.json/
project_rates.json persistence under ~/.davinci_time_tracker/, unchanged
from the standalone tool so an existing installation's tracked time and
rates carry over with no migration step.

settings.json here only holds tracking-specific
keys (poll interval, idle detection, goals, backups, ...). Appearance
(theme/accent/background), window stay-on-top, and "keep running in tray"
are now the shell's own concerns - see core/settings_store.py's
SharedSettings - so they're dropped from DEFAULT_SETTINGS below rather than
duplicated in two places.
"""

import glob
import os
import json
import uuid
from datetime import datetime, timedelta

from core import atomic_io

from .currencies import DEFAULT_CURRENCY

_MISSING = object()

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

class DataManager:
    def __init__(self, base_dir=None):
        if base_dir is None:
            base_dir = os.path.join(os.path.expanduser("~"), ".davinci_time_tracker")
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

        self.entries_path = os.path.join(self.base_dir, "entries.json")
        self.settings_path = os.path.join(self.base_dir, "settings.json")
        self.project_rates_path = os.path.join(self.base_dir, "project_rates.json")

        # One line per file that was there but couldn't be read - it's been
        # renamed aside (see _load_json) so nothing overwrites it, and the
        # page should tell the user once.
        self.load_warnings = []

        self.entries = self.load_entries()
        self.settings = self.load_settings()
        self.project_rates = self.load_project_rates()

    # --- Crash-safe file access (core/atomic_io.py) ---
    def _load_json(self, path, expected_type):
        """The file's contents, or None if it doesn't exist yet. A file that
        is there but unreadable (or not the expected list/dict) is renamed
        to "<name>.corrupt-N" rather than left in place, so the first save
        with defaults can't overwrite the user's data, and a line goes into
        load_warnings."""
        try:
            data = atomic_io.read_json(path, default=_MISSING, warnings=self.load_warnings)
        except atomic_io.CorruptFileError:
            data = None
        if data is _MISSING:
            return None
        if isinstance(data, expected_type):
            return data
        name = os.path.basename(path)
        try:
            moved = atomic_io.set_aside(path)
        except OSError as err:
            self.load_warnings.append(
                f"{name} was damaged and could not be read ({err}). Time Tracker started without it."
            )
            return None
        self.load_warnings.append(
            f"{name} was damaged and has been kept as {os.path.basename(moved or path)}. "
            f"Time Tracker started without it."
        )
        return None

    # --- Entries ---
    def load_entries(self):
        data = self._load_json(self.entries_path, list)
        if data is None:
            return []
        return [e for e in data if isinstance(e, dict) and "id" in e]

    def save_entries(self):
        atomic_io.write_json(self.entries_path, self.entries, indent=2)

    def start_entry(self, project_name):
        """Starts a new open-ended entry (end=None) for project_name and
        returns it. Caller is responsible for ending any previously-open
        entry first - kept separate so the caller can decide whether to
        discard a too-short entry instead of saving it."""
        entry = {
            "id": str(uuid.uuid4()),
            "project": project_name,
            "start": now_iso(),
            "end": None,
            "notes": "",
            "hidden": False,
            # See pause_entry()/resume_entry() - paused_seconds accumulates
            # every completed pause segment; pause_started_at marks a
            # currently-in-progress one (None the rest of the time).
            "paused_seconds": 0.0,
            "pause_started_at": None,
        }
        self.entries.append(entry)
        self.save_entries()
        return entry

    def pause_entry(self, entry):
        """Freezes an open entry's elapsed time in place WITHOUT ending it -
        used by auto-tracking's Pause/Resume escape valve on the Tracker tab
        so pausing/resuming continues the same session (duration_seconds()
        stops advancing, then picks back up right where it left off) rather
        than splitting it into separate saved entries each time. A no-op if
        the entry is already closed or already paused."""
        if entry.get("end") is not None or entry.get("pause_started_at"):
            return
        entry["pause_started_at"] = now_iso()
        self.save_entries()

    def resume_entry(self, entry):
        """Reverses pause_entry() - folds the just-finished pause segment's
        length into paused_seconds. A no-op if the entry isn't currently
        paused."""
        pause_started_at = entry.get("pause_started_at")
        if not pause_started_at:
            return
        paused_dt = datetime.fromisoformat(pause_started_at)
        entry["paused_seconds"] = entry.get("paused_seconds", 0.0) + max(
            0.0, (datetime.now() - paused_dt).total_seconds()
        )
        entry["pause_started_at"] = None
        self.save_entries()

    def end_entry(self, entry, when=None, min_seconds=1):
        """Closes an open entry. Entries shorter than min_seconds (e.g. a
        blip while switching projects) are dropped entirely rather than
        cluttering the log with near-zero-duration rows. If the entry is
        still mid-pause (e.g. the app was closed/quit without an explicit
        Resume first, rather than through the normal pause_entry/
        resume_entry pairing), that trailing pause segment is folded into
        paused_seconds here too, so it's excluded from the saved duration
        exactly like a normal pause would be."""
        if entry.get("end") is not None:
            return
        end_iso = when or now_iso()
        end_dt = datetime.fromisoformat(end_iso)

        if entry.get("pause_started_at"):
            paused_dt = datetime.fromisoformat(entry["pause_started_at"])
            entry["paused_seconds"] = entry.get("paused_seconds", 0.0) + max(
                0.0, (end_dt - paused_dt).total_seconds()
            )
            entry["pause_started_at"] = None
        # The heartbeat (page.py _touch_heartbeat) only matters while open.
        entry.pop("last_seen", None)

        start_dt = datetime.fromisoformat(entry["start"])
        duration = max(0.0, (end_dt - start_dt).total_seconds() - entry.get("paused_seconds", 0.0))
        if duration < min_seconds:
            self.entries.remove(entry)
        else:
            entry["end"] = end_iso
        self.save_entries()

    def get_open_entry(self):
        for entry in reversed(self.entries):
            if entry.get("end") is None:
                return entry
        return None

    def duration_seconds(self, entry):
        """Wall-clock time minus every completed pause segment
        (paused_seconds) minus, if the entry is CURRENTLY mid-pause, the
        still-ongoing segment too (entries stay open while paused - see
        pause_entry() - so this keeps the displayed timer frozen at exactly
        the moment pausing happened instead of continuing to climb)."""
        start_dt = datetime.fromisoformat(entry["start"])
        end_iso = entry.get("end")
        end_dt = datetime.now() if end_iso is None else datetime.fromisoformat(end_iso)
        paused = entry.get("paused_seconds", 0.0)
        pause_started_at = entry.get("pause_started_at")
        if pause_started_at:
            paused_dt = datetime.fromisoformat(pause_started_at)
            paused += max(0.0, (end_dt - paused_dt).total_seconds())
        return max(0.0, (end_dt - start_dt).total_seconds() - paused)

    def add_manual_entry(self, project_name, start_iso, end_iso, notes=""):
        entry = {
            "id": str(uuid.uuid4()),
            "project": project_name,
            "start": start_iso,
            "end": end_iso,
            "notes": notes,
            "hidden": False,
        }
        self.entries.append(entry)
        self.save_entries()
        return entry

    def update_entry(self, entry_id, project_name, start_iso, end_iso, notes=""):
        for entry in self.entries:
            if entry["id"] == entry_id:
                # If the user explicitly changed the start or end time manually,
                # they are defining an exact block, so discard the accumulated
                # paused seconds so the duration becomes exactly end - start.
                if entry.get("start") != start_iso or entry.get("end") != end_iso:
                    entry.pop("paused_seconds", None)
                    entry.pop("pause_started_at", None)

                entry["project"] = project_name
                entry["start"] = start_iso
                entry["end"] = end_iso
                entry["notes"] = notes
                self.save_entries()
                return entry
        return None

    def set_entry_hidden(self, entry_id, hidden):
        """Hidden entries stay in History (darkened) but are left out of
        every export - a soft "don't count this" rather than deletion."""
        for entry in self.entries:
            if entry["id"] == entry_id:
                entry["hidden"] = hidden
                self.save_entries()
                return entry
        return None

    def delete_entry(self, entry_id):
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        self.save_entries()

    def delete_entries(self, entry_ids):
        entry_ids = set(entry_ids)
        self.entries = [e for e in self.entries if e["id"] not in entry_ids]
        self.save_entries()

    def closed_entries_sorted(self):
        closed = [e for e in self.entries if e.get("end") is not None]
        closed.sort(key=lambda e: e["start"], reverse=True)
        return closed

    def project_names(self):
        names = sorted({e["project"] for e in self.entries if e.get("project")})
        return names

    # --- Settings (tracking-specific only - see module docstring) ---
    def load_settings(self):
        data = self._load_json(self.settings_path, dict)
        if data is not None:
            return data
        return {
            "poll_interval_seconds": 5,
            "manually_paused": False,
            "manual_tracking": False,
            "idle_detection_enabled": True,
            "idle_threshold_minutes": 5,
            "weekly_goal_hours": 0.0,
            "monthly_goal_hours": 0.0,
            "auto_backup_enabled": True,
            "last_auto_backup_at": None,
            "global_hotkey_enabled": False,
            "remember_export_folder": False,
            "last_export_folder": None,
        }

    def save_settings(self):
        atomic_io.write_json(self.settings_path, self.settings, indent=2)

    def get_export_start_dir(self):
        if self.settings.get("remember_export_folder", False):
            last = self.settings.get("last_export_folder")
            if last and os.path.isdir(last):
                return last
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        return downloads if os.path.isdir(downloads) else os.path.expanduser("~")

    def default_export_path(self, filename):
        return os.path.join(self.get_export_start_dir(), filename)

    def remember_export_folder(self, file_path):
        if not file_path:
            return
        self.settings["last_export_folder"] = os.path.dirname(file_path)
        self.save_settings()

    # --- Project rates (Settings > Set Rate for Current Project) ---
    # Each entry is {"rate": float, "currency": "USD"} - a per-hour rate in
    # that project's own currency. Rates for different projects can be in
    # different currencies (see currencies.py); Reports keeps earnings
    # totals split by currency rather than summing across them.
    def load_project_rates(self):
        data = self._load_json(self.project_rates_path, dict)
        if data is None:
            return {}
        result = {}
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            # One unreadable rate skips just that project, not every rate.
            try:
                if isinstance(v, dict):
                    result[k] = {
                        "rate": float(v.get("rate", 0.0)),
                        "currency": v.get("currency", DEFAULT_CURRENCY),
                    }
                else:
                    # Pre-currency format: a bare number, always USD.
                    result[k] = {"rate": float(v), "currency": DEFAULT_CURRENCY}
            except (TypeError, ValueError):
                continue
        return result

    def save_project_rates(self):
        atomic_io.write_json(self.project_rates_path, self.project_rates, indent=2)

    def get_project_rate_info(self, project_name):
        info = self.project_rates.get(project_name)
        if isinstance(info, dict):
            return {"rate": float(info.get("rate", 0.0)), "currency": info.get("currency", DEFAULT_CURRENCY)}
        return {"rate": 0.0, "currency": DEFAULT_CURRENCY}

    def get_project_rate(self, project_name):
        return self.get_project_rate_info(project_name)["rate"]

    def set_project_rate(self, project_name, rate, currency=DEFAULT_CURRENCY):
        if rate and rate > 0:
            self.project_rates[project_name] = {"rate": rate, "currency": currency}
        else:
            self.project_rates.pop(project_name, None)
        self.save_project_rates()

    def project_earnings(self, project_name, seconds):
        return (seconds / 3600.0) * self.get_project_rate(project_name)

    # --- Full data backup/restore (Settings > Data) ---
    # Meant for moving to a new machine, not as a per-entry export - see
    # export_utils.py's CSV/JSON/PDF/XLSX exporters for that instead.
    _SETTINGS_KEYS_EXCLUDED_FROM_BACKUP = {"last_auto_backup_at"}

    def export_backup(self, path):
        payload = {
            "app": "davinci_time_tracker_backup",
            "exported_at": now_iso(),
            "entries": [e for e in self.entries if "start" in e and "end" in e],
            "settings": {
                k: v for k, v in self.settings.items()
                if k not in self._SETTINGS_KEYS_EXCLUDED_FROM_BACKUP
            },
            "project_rates": self.project_rates,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def import_backup(self, path):
        """Merges entries by id (an entry already present, by id, is left
        alone rather than duplicated) and layers the backup's settings on
        top of the current ones. Returns (added_count, skipped_count).
        Raises on a malformed file - the caller is expected to show that to
        the user."""
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError("Not a valid Time Tracker backup file.")

        imported_entries = payload.get("entries", [])
        if not isinstance(imported_entries, list):
            raise ValueError("Not a valid Time Tracker backup file.")

        existing_ids = {e["id"] for e in self.entries}
        added = 0
        for entry in imported_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            
            if "start" not in entry or "end" not in entry:
                continue

            if entry["id"] not in existing_ids:
                self.entries.append(entry)
                existing_ids.add(entry["id"])
                added += 1
        skipped = len(imported_entries) - added
        self.save_entries()

        imported_settings = payload.get("settings", {})
        if isinstance(imported_settings, dict):
            for key, value in imported_settings.items():
                if key not in self._SETTINGS_KEYS_EXCLUDED_FROM_BACKUP and key in self.settings:
                    self.settings[key] = value
            self.save_settings()

        imported_rates = payload.get("project_rates", {})
        if isinstance(imported_rates, dict):
            for project, info in imported_rates.items():
                if not isinstance(project, str):
                    continue
                try:
                    if isinstance(info, dict):
                        self.project_rates[project] = {
                            "rate": float(info.get("rate", 0.0)),
                            "currency": info.get("currency", DEFAULT_CURRENCY),
                        }
                    else:
                        self.project_rates[project] = {"rate": float(info), "currency": DEFAULT_CURRENCY}
                except (TypeError, ValueError):
                    pass
            self.save_project_rates()

        return added, skipped

    # --- Automatic weekly backups (Settings > Automatic weekly backups) ---
    # A quiet safety net on top of the manual Export All Data/Import Data
    # flow above - writes the same backup format to a rotating set of files
    # under its own subfolder, so a lost/corrupted entries.json still has a
    # recent recovery point without the user needing to have remembered to
    # export one by hand.
    _AUTO_BACKUP_INTERVAL = timedelta(days=7)
    _AUTO_BACKUP_KEEP = 8

    def _auto_backup_dir(self):
        path = os.path.join(self.base_dir, "auto_backups")
        os.makedirs(path, exist_ok=True)
        return path

    def perform_auto_backup_if_due(self):
        """Best-effort - a failed auto-backup should never surface an error
        to the user or block anything else; the manual Export All Data flow
        already handles/shows real errors for someone who explicitly asked
        to export. Safe to call often (e.g. once an hour) - it's a no-op
        unless the interval has actually elapsed."""
        if not self.settings.get("auto_backup_enabled", True):
            return
        try:
            last_at = self.settings.get("last_auto_backup_at")
            if last_at:
                elapsed = datetime.now() - datetime.fromisoformat(last_at)
                if elapsed < self._AUTO_BACKUP_INTERVAL:
                    return

            backup_dir = self._auto_backup_dir()
            filename = f"autobackup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            self.export_backup(os.path.join(backup_dir, filename))

            self.settings["last_auto_backup_at"] = now_iso()
            self.save_settings()

            # Prune down to the most recent _AUTO_BACKUP_KEEP files - this
            # folder would otherwise grow forever for a long-lived install.
            existing = sorted(glob.glob(os.path.join(backup_dir, "autobackup_*.json")))
            for stale_path in existing[:-self._AUTO_BACKUP_KEEP]:
                try:
                    os.remove(stale_path)
                except OSError:
                    pass
        except Exception:
            pass
