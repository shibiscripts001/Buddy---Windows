#!/usr/bin/env python3
"""
Data Manager for Color Palette Manager
Handles persistent storage of palettes, folders, tags, versions, and app settings
using standard Python json and os modules under ~/.color_palette_manager/
"""

import os
from datetime import datetime

from core import atomic_io

_MISSING = object()

class DataManager:
    def __init__(self, base_dir=None):
        if base_dir is None:
            base_dir = os.path.join(os.path.expanduser("~"), ".color_palette_manager")
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

        self.palette_data_path = os.path.join(self.base_dir, "palettes.json")
        self.palette_folders_path = os.path.join(self.base_dir, "folders.json")
        self.palette_custom_tags_path = os.path.join(self.base_dir, "custom_tags.json")
        self.palette_versions_path = os.path.join(self.base_dir, "versions.json")
        self.settings_path = os.path.join(self.base_dir, "settings.json")
        self.visualizer_overrides_path = os.path.join(self.base_dir, "visualizer_overrides.json")

        # One line per file that was there but couldn't be read - it's been
        # renamed aside (see _load_json) so nothing overwrites it, and the
        # page tells the user once.
        self.load_warnings = []

        self.palettes = self.load_palette_data()
        self.palette_folders = self.load_palette_folders()
        self.palette_custom_tags = self.load_palette_custom_tags()
        self.palette_versions = self.load_palette_versions()
        self.settings = self.load_settings()
        self.visualizer_overrides = self.load_visualizer_overrides()

    # --- Crash-safe file access (core/atomic_io.py) ---
    def _load_json(self, path, expected_type=dict):
        """The file's contents, or None if it doesn't exist yet. A file that
        is there but unreadable (or not the expected dict) is renamed to
        "<name>.corrupt-N" rather than left in place, so the first save with
        defaults can't overwrite the user's data, and a line goes into
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
                f"{name} was damaged and could not be read ({err}). Color Palette Manager started without it."
            )
            return None
        self.load_warnings.append(
            f"{name} was damaged and has been kept as {os.path.basename(moved or path)}. "
            f"Color Palette Manager started without it."
        )
        return None

    def load_palette_data(self):
        data = self._load_json(self.palette_data_path)
        if data is not None:
            return data
        return {
            "Cinematic Look": ["#1A2B3C", "#F39C12", "#E74C3C"], 
            "Base Grades": ["#FFFFFF", "#000000"]
        }

    def save_palette_data(self):
        atomic_io.write_json(self.palette_data_path, self.palettes, indent=2)

    def load_palette_folders(self):
        data = self._load_json(self.palette_folders_path)
        if data is None:
            return {}
        clean = {}
        for folder, pal_list in data.items():
            if isinstance(pal_list, list):
                clean[folder] = [p for p in pal_list if isinstance(p, str)]
        return clean

    def save_palette_folders(self):
        atomic_io.write_json(self.palette_folders_path, self.palette_folders, indent=2)

    def load_palette_custom_tags(self):
        data = self._load_json(self.palette_custom_tags_path)
        if data is None:
            return {}
        clean = {}
        for p_name, tags in data.items():
            if isinstance(tags, list):
                clean[p_name] = [str(t).strip().lower() for t in tags if str(t).strip()]
            elif isinstance(tags, str):
                clean[p_name] = [t.strip().lower() for t in tags.split(",") if t.strip()]
        return clean

    def save_palette_custom_tags(self):
        atomic_io.write_json(self.palette_custom_tags_path, self.palette_custom_tags, indent=2)

    def load_visualizer_overrides(self):
        data = self._load_json(self.visualizer_overrides_path)
        return data if data is not None else {}

    def save_visualizer_overrides(self):
        atomic_io.write_json(self.visualizer_overrides_path, self.visualizer_overrides, indent=2)

    def get_visualizer_slot_override(self, palette_name, slot_index):
        return self.visualizer_overrides.get(palette_name, {}).get(str(slot_index))

    def set_visualizer_slot_override(self, palette_name, slot_index, hex_code):
        slots = self.visualizer_overrides.setdefault(palette_name, {})
        slots[str(slot_index)] = hex_code
        self.save_visualizer_overrides()

    def _coerce_version_history(self, name, raw):
        if isinstance(raw, str):
            if not raw.strip():
                return []
            return [{"label": raw.strip(), "colors": list(self.palettes.get(name, [])), "timestamp": None}]
        if isinstance(raw, list):
            history = []
            for entry in raw:
                if isinstance(entry, dict) and isinstance(entry.get("label"), str) and isinstance(entry.get("colors"), list):
                    history.append({
                        "label": entry["label"],
                        "colors": list(entry["colors"]),
                        "timestamp": entry.get("timestamp"),
                    })
            return history
        return []

    def load_palette_versions(self):
        data = self._load_json(self.palette_versions_path)
        if data is None:
            return {}
        return {name: self._coerce_version_history(name, raw) for name, raw in data.items()}

    def save_palette_versions(self):
        atomic_io.write_json(self.palette_versions_path, self.palette_versions, indent=2)

    def load_settings(self):
        settings = self._load_json(self.settings_path)
        if settings is not None:
            # Migration: the preset's old saved name (below) became
            # just "Default" - remap an old saved value so an
            # existing settings file keeps using the exact same preset
            # instead of silently falling through to the derived-custom-
            # color code path.
            if settings.get("theme_preset") == "Material Default":
                settings["theme_preset"] = "Default"
            return settings
        return {
            "theme_preset": "Default",
            "accent": "#6750A4",
            "background": "#141218",
            "transparency": "100%",
            "mini_palette_transparency": "100%",
            "scale": "100%",
            "wheel": "RYB",
            "harmony_count": "5",
            "harmony_intensity": "15",
            "hide_on_dropper": True,
            "keep_external_windows_open": False,
            "remember_export_folder": False,
            "last_export_folder": None,
            "colorblind_mode": "None",
            "visualize_show_hex": True,
            "visualize_show_pct": True,
            "visualize_lock_layout": False,
            "focus_mode_enabled": False,
            "focus_mode_color_val": 128,
            "focus_mode_opacity": 1.0,
            "language": "English",
            "window_width": 820,
            "window_height": 940,
            "stay_on_top": True,
        }

    def save_settings(self):
        atomic_io.write_json(self.settings_path, self.settings, indent=2)

    # --- Export folder helpers (used by every "Export ..." dialog app-wide) ---
    def get_export_start_dir(self):
        """Where a new export dialog should start browsing: the folder
        from the last successful export, if "Remember Previous Export
        Folder" is on and one has actually been recorded yet - otherwise
        the user's Downloads folder (falling back to their home folder on
        the rare system where Downloads doesn't exist)."""
        if self.settings.get("remember_export_folder", False):
            last = self.settings.get("last_export_folder")
            if last and os.path.isdir(last):
                return last
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        return downloads if os.path.isdir(downloads) else os.path.expanduser("~")

    def default_export_path(self, filename):
        """get_export_start_dir() + filename, ready to hand straight to
        QFileDialog.getSaveFileName as its default path."""
        return os.path.join(self.get_export_start_dir(), filename)

    def remember_export_folder(self, file_path):
        """Call after any successful export, regardless of whether
        "Remember previous export folder" is currently on - so the folder
        is already there and ready the moment the user turns that setting
        on, without needing to export a second time first."""
        if not file_path:
            return
        self.settings["last_export_folder"] = os.path.dirname(file_path)
        self.save_settings()

    # --- Palette & Folder Helper Methods ---
    def move_palette_to_folder(self, palette_name, target_folder):
        for folder, pal_list in list(self.palette_folders.items()):
            if palette_name in pal_list:
                pal_list.remove(palette_name)
        if target_folder:
            if target_folder not in self.palette_folders:
                self.palette_folders[target_folder] = []
            if palette_name not in self.palette_folders[target_folder]:
                self.palette_folders[target_folder].append(palette_name)
        self.save_palette_folders()

    def rename_palette(self, old_name, new_name):
        if old_name in self.palettes:
            self.palettes[new_name] = self.palettes.pop(old_name)
            self.save_palette_data()

        for folder, pal_list in self.palette_folders.items():
            if old_name in pal_list:
                pal_list[pal_list.index(old_name)] = new_name
        self.save_palette_folders()

        if old_name in self.palette_custom_tags:
            self.palette_custom_tags[new_name] = self.palette_custom_tags.pop(old_name)
            self.save_palette_custom_tags()

        if old_name in self.palette_versions:
            self.palette_versions[new_name] = self.palette_versions.pop(old_name)
            self.save_palette_versions()

        if old_name in self.visualizer_overrides:
            self.visualizer_overrides[new_name] = self.visualizer_overrides.pop(old_name)
            self.save_visualizer_overrides()

    def delete_palette(self, name):
        if name in self.palettes:
            del self.palettes[name]
            self.save_palette_data()
        
        for folder, pal_list in list(self.palette_folders.items()):
            if name in pal_list:
                pal_list.remove(name)
        self.save_palette_folders()

        if name in self.palette_custom_tags:
            del self.palette_custom_tags[name]
            self.save_palette_custom_tags()

        if name in self.palette_versions:
            del self.palette_versions[name]
            self.save_palette_versions()

        if name in self.visualizer_overrides:
            del self.visualizer_overrides[name]
            self.save_visualizer_overrides()

    def get_palette_folder(self, palette_name):
        for folder, pal_list in self.palette_folders.items():
            if palette_name in pal_list:
                return folder
        return None

    def get_palette_custom_tags(self, palette_name):
        return self.palette_custom_tags.get(palette_name, [])

    def set_palette_custom_tags(self, palette_name, tags_list):
        clean_tags = []
        raw_list = tags_list if isinstance(tags_list, list) else str(tags_list).split(",")
        for t in raw_list:
            tag_clean = t.strip().lower()
            if tag_clean and tag_clean not in clean_tags:
                clean_tags.append(tag_clean)

        if clean_tags:
            self.palette_custom_tags[palette_name] = clean_tags
        else:
            self.palette_custom_tags.pop(palette_name, None)
        self.save_palette_custom_tags()

    def add_palette_version_snapshot(self, palette_name, label):
        history = self.palette_versions.setdefault(palette_name, [])
        history.append({
            "label": label.strip(),
            "colors": list(self.palettes.get(palette_name, [])),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })
        self.save_palette_versions()
