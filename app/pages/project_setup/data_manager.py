#!/usr/bin/env python3
"""
Settings persistence for Resolve Project Setup - theme choice, the
last saved bin list, and the last folder used for a folder import, mirroring
Davinci Time Tracker's data_manager.py pattern (plain os.path.expanduser,
works identically on macOS).

The settings directory keeps its original "bin_generator" name (this app's
previous name) rather than being renamed to match - renaming it would have
silently discarded anyone's existing saved bin list/theme on first launch
after the update, for no real benefit.
"""

import json
import os

SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".resolve_bin_generator")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

DEFAULT_BIN_LIST = "01_Timelines\n02_Footage\n03_Audio\n04_Assets\n05_Music"

DEFAULT_SETTINGS = {
    "theme_preset": "Default",
    "accent": "#D0BCFF",
    "background": "#141218",
    "bin_list": DEFAULT_BIN_LIST,
    "last_import_folder": "",
    # When False (the default), Import Folder creates its top-level bin
    # inside whichever bin is currently selected in the Media Pool. When
    # True, it always goes to the Master/root level instead, regardless of
    # selection - see Settings' "Send imported folders to Master level".
    "import_to_master": False,
    # Whether the app window stays above Resolve's own window - see
    # Settings' "Keep window on top of Resolve".
    "stay_on_top": True,
    # Align tab: whether a synced group of clips gets moved to the very
    # start of the timeline, or left sitting at its raw computed offset
    # (which for Timecode sync in particular is often the source's real
    # timecode, e.g. an hour in on an otherwise empty timeline).
    "align_snap_to_start": True,
    # Explicit override for where to find ffmpeg (used by Align's Waveform
    # method) - blank means "search PATH and common install locations"
    # (see ffmpeg_utils.find_ffmpeg).
    "ffmpeg_path": "",
    # How many seconds of each clip's audio to decode for waveform
    # matching - the sync point is normally near the start of a clip
    # anyway (a clap, a slate, or just where two cameras' rolls overlap),
    # so there's no need to decode a whole multi-hour file to find it.
    # How far the refine pass is allowed to move a clip. A coarse pass
    # should leave clips roughly right, so a larger correction than this
    # is treated as a false match rather than applied.
    # Shift: also remove the dead air between blocks of footage.
    "shift_close_gaps": False,
    # Collapse tab: whether to delete audio clips whose actual trimmed
    # content is silent throughout, before packing everything onto the
    # minimum number of tracks - off by default since it deletes clips
    # outright rather than just rearranging them.
    "collapse_delete_silent_audio": False,
    # Proxy tab: which clips a run targets ("selection", "timeline",
    # "bin" or "all"), the proxy resolution ("original", "half",
    # "quarter") and format (a proxy.CODECS id), and whether the bin
    # scope reaches into sub-bins. See pages/project_setup/proxy.py.
    "proxy_scope": "selection",
    "proxy_resolution": "half",
    "proxy_codec": "h264",
    "proxy_recursive": False,
}


class DataManager:
    def __init__(self, base_dir=None):
        """base_dir: a folder to keep settings.json in instead of the real
        one (tests use a temp folder)."""
        base_dir = base_dir or SETTINGS_DIR
        os.makedirs(base_dir, exist_ok=True)
        self.path = os.path.join(base_dir, "settings.json")
        self.settings = dict(DEFAULT_SETTINGS)
        self._load_settings()

    def _load_settings(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.settings.update(saved)
        except Exception:
            pass

    def save_settings(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass
