#!/usr/bin/env python3
"""
Settings persistence for the Buddy shell.

Two tiers, deliberately kept separate:

- SharedSettings: the ONE set of appearance/window prefs (theme preset,
  accent, background, stay-on-top) that used to be duplicated in every
  standalone tool's own settings.json. Stored once at ~/.buddy/settings.json
  so picking a theme in Settings applies to every tool page at once.

- ToolSettings: a per-tool bucket for whatever that specific tool still
  needs to remember beyond appearance (e.g. Time Tracker's hourly rate).
  Deliberately reuses each tool's ORIGINAL settings directory/filename
  (e.g. ~/.batch_clip_renamer/settings.json) rather than inventing a new
  layout - a person migrating from the standalone tool keeps their
  existing tool-specific settings for free, with no migration step, since
  the only keys being dropped (theme_preset/accent/background/stay_on_top)
  now live in SharedSettings instead.
"""

import os

from core import atomic_io

BUDDY_DIR = os.path.join(os.path.expanduser("~"), ".buddy")
SHARED_SETTINGS_PATH = os.path.join(BUDDY_DIR, "settings.json")

# theme = overall look (shape + palette family); subtheme = palette within
# it. theme_preset is the pre-hierarchy key, kept only for migration.
DEFAULT_SHARED_SETTINGS = {
    "theme": "Resolve",      # shown as "Default" (core/theme.py DEFAULT_THEME)
    "subtheme": "DaVinci",
    "theme_preset": "Default",
    # Custom accent/background are per theme: accent_<theme>. A single
    # shared pair meant picking Custom under Retro inherited the Default
    # theme's near-black background.
    "accent": "#D0BCFF",
    "background": "#141218",
    "stay_on_top": False,
    # Whether the shell's own [X] minimizes to the system tray (tracking
    # tools keep running) instead of quitting outright - see
    # core/shell_window.py's closeEvent.
    "keep_running_in_tray": True,
    # The orb next to "Buddy": a once-a-day check for announcements (see
    # core/announcements.py). One checkbox in Settings turns it off.
    "announcements_enabled": True,
}


class SharedSettings:
    def __init__(self):
        os.makedirs(BUDDY_DIR, exist_ok=True)
        self.values = dict(DEFAULT_SHARED_SETTINGS)
        # A damaged settings.json (or one whose .bak is also damaged) is
        # renamed aside (see _load) so nothing overwrites it, and the shell
        # tells the user once.
        self.load_warnings = []
        self._load()
        self._migrate_theme_preset()

    def _migrate_theme_preset(self):
        """Split the old flat theme_preset into theme + subtheme.

        Every old value was a palette name. Retro becomes the Retro theme
        with its own default palette; everything else was a Default-theme
        palette and keeps working under that theme.
        """
        if self.values.get("_theme_migrated"):
            return
        # Only a settings file from before themes existed has anything to
        # migrate. A fresh install (no file) or one that already names a
        # theme keeps it - otherwise the "Default" theme_preset default
        # would drag every new user onto the old theme instead of
        # DEFAULT_SHARED_SETTINGS' choice.
        if not getattr(self, "_loaded_keys", None) or "theme" in self._loaded_keys:
            self.values["_theme_migrated"] = True
            return
        old = self.values.get("theme_preset", "Default")
        if old == "Retro":
            self.values["theme"], self.values["subtheme"] = "Retro", "Peach"
        else:
            self.values["theme"] = "Default"
            self.values["subtheme"] = old
        # The old single accent/background pair was authored against
        # whichever theme was selected, so it belongs to that theme.
        old_theme = self.values["theme"]
        if "accent" in self.values:
            self.values.setdefault(f"accent_{old_theme}", self.values["accent"])
        if "background" in self.values:
            self.values.setdefault(f"background_{old_theme}", self.values["background"])
        self.values["_theme_migrated"] = True
        self.save()

    def get(self, key, default=None):
        return self.values.get(key, default)

    def __setitem__(self, key, value):
        self.values[key] = value

    def _load(self):
        try:
            saved = atomic_io.read_json(SHARED_SETTINGS_PATH, default=None, warnings=self.load_warnings)
        except atomic_io.CorruptFileError:
            name = os.path.basename(SHARED_SETTINGS_PATH)
            try:
                moved = atomic_io.set_aside(SHARED_SETTINGS_PATH)
                self.load_warnings.append(
                    f"{name} was damaged and has been kept as {os.path.basename(moved or SHARED_SETTINGS_PATH)}. "
                    f"Buddy started with default settings."
                )
            except OSError as err:
                self.load_warnings.append(f"{name} was damaged and could not be read ({err}).")
            return
        if saved is None:
            return
        self.values.update(saved)
        self._loaded_keys = set(saved)

    def save(self):
        atomic_io.write_json(SHARED_SETTINGS_PATH, self.values, indent=2)


class ToolSettings:
    """One JSON bucket for a single tool's own (non-appearance) settings.

    `legacy_dirname` should match that tool's original standalone folder
    name (e.g. "batch_clip_renamer" for ~/.batch_clip_renamer/) so an
    existing installation's tool-specific preferences carry over.
    """

    def __init__(self, legacy_dirname, defaults=None):
        self._dir = os.path.join(os.path.expanduser("~"), f".{legacy_dirname}")
        self._path = os.path.join(self._dir, "settings.json")
        self.values = dict(defaults or {})
        # See SharedSettings._load: a damaged file (and a damaged .bak) is
        # renamed aside rather than silently reset, and a line goes here.
        self.load_warnings = []
        os.makedirs(self._dir, exist_ok=True)
        self._load()

    def get(self, key, default=None):
        return self.values.get(key, default)

    def __setitem__(self, key, value):
        self.values[key] = value

    def _load(self):
        try:
            saved = atomic_io.read_json(self._path, default=None, warnings=self.load_warnings)
        except atomic_io.CorruptFileError:
            name = os.path.basename(self._path)
            try:
                moved = atomic_io.set_aside(self._path)
                self.load_warnings.append(
                    f"{name} was damaged and has been kept as {os.path.basename(moved or self._path)}. "
                    f"Started with default settings."
                )
            except OSError as err:
                self.load_warnings.append(f"{name} was damaged and could not be read ({err}).")
            return
        if saved is None:
            return
        self.values.update(saved)

    def save(self):
        atomic_io.write_json(self._path, self.values, indent=2)
