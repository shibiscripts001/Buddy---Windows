#!/usr/bin/env python3
"""
Persistence for Asset Manager inside Buddy.

AssetLibrary/ProjectLibrary are plain Python + json with no UI
dependency, and they read and write ~/.asset_manager/assets.json /
projects.json directly. That is the location the standalone Asset Manager
used, so a person moving from it keeps their whole asset library and
project lists with no migration step (core/settings_store.py's
ToolSettings reuses each tool's original data directory for the same
reason).

Settings: the theme/window chrome belongs to the shell (SharedSettings),
so this page uses the shell's ToolSettings bucket only for the two
BEHAVIOR toggles that are genuinely this tool's own:

  include_folders_in_sort      folders interleaved with items when
                              sorting, vs always listed first
  group_all_media_by_folder    All Media collapses 2+ assets from the
                              same folder into one expandable row

ToolSettings("asset_manager") maps to ~/.asset_manager/settings.json -
the standalone tool's own settings file - so existing installs keep their
behavior prefs too; the theme/window keys that also lived in that file
are simply ignored, since the shell owns those now.
"""

import os
import uuid
from datetime import datetime

from core import atomic_io

DATA_DIR = os.path.join(os.path.expanduser("~"), ".asset_manager")
ASSET_STORE_PATH = os.path.join(DATA_DIR, "assets.json")
PROJECT_STORE_PATH = os.path.join(DATA_DIR, "projects.json")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".mxf", ".m4v"}
SUPPORTED_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
CATEGORIES = ["All", "Image", "Audio", "Video", "Other"]


def categorize_ext(ext):
    if ext in IMAGE_EXTS:
        return "Image"
    if ext in AUDIO_EXTS:
        return "Audio"
    if ext in VIDEO_EXTS:
        return "Video"
    return "Other"


def load_records(path, what, warnings, required=("id",)):
    """{id: record} from a JSON list of records (assets.json/projects.json).
    Missing file -> {}. A file that's there but unreadable (or not a list)
    is renamed to "<name>.corrupt-N" so the next save can't overwrite the
    user's library with an empty one, and a line goes into `warnings`. A
    single record missing one of the `required` keys is skipped on its own
    rather than emptying the whole library."""
    name = os.path.basename(path)
    try:
        data = atomic_io.read_json(path, default=[], warnings=warnings)
    except atomic_io.CorruptFileError:
        data = None
    if not isinstance(data, list):
        try:
            moved = atomic_io.set_aside(path)
        except OSError as err:
            warnings.append(f"{name} was damaged and could not be read ({err}). Asset Manager started without its {what}.")
            return {}
        warnings.append(
            f"{name} was damaged and has been kept as {os.path.basename(moved or path)}. "
            f"Asset Manager started without its {what}."
        )
        return {}
    records = {}
    skipped = 0
    for record in data:
        if (
            isinstance(record, dict)
            and isinstance(record.get("id"), (str, int))
            and all(record.get(key) is not None for key in required)
        ):
            records[record["id"]] = record
        else:
            skipped += 1
    if skipped:
        warnings.append(f"{name}: skipped {skipped} unreadable entr{'y' if skipped == 1 else 'ies'}.")
    return records


def _norm_path(path):
    return os.path.normcase(os.path.abspath(path))


class AssetLibrary:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.assets = {}
        # normalized path -> asset_id, kept in sync by add/remove/update_path
        # so duplicate checks and lookups are O(1) instead of a full rescan
        # of the library per file - the difference between an instant bulk
        # import and one that freezes the UI on a few thousand clips.
        self._path_index = {}
        # Lines for the page to show once - see load_records().
        self.load_warnings = []
        self._load()

    def _load(self):
        self.assets = load_records(ASSET_STORE_PATH, "assets", self.load_warnings, required=("id", "path"))
        for asset in self.assets.values():
            asset.setdefault("name", os.path.basename(str(asset["path"])))
        self._path_index = {_norm_path(asset["path"]): asset_id for asset_id, asset in self.assets.items()}

    def save(self):
        atomic_io.write_json(ASSET_STORE_PATH, list(self.assets.values()), indent=2)

    def add(self, path):
        norm = _norm_path(path)
        if norm in self._path_index:
            return None
        asset_id = str(uuid.uuid4())
        ext = os.path.splitext(path)[1].lower()
        self.assets[asset_id] = {
            "id": asset_id,
            "path": path,
            "name": os.path.basename(path),
            "ext": ext,
            "category": categorize_ext(ext),
            "date_added": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._path_index[norm] = asset_id
        return asset_id

    def remove(self, asset_id):
        asset = self.assets.pop(asset_id, None)
        if asset is not None:
            self._path_index.pop(_norm_path(asset["path"]), None)

    def find_id_by_path(self, path):
        return self._path_index.get(_norm_path(path))

    def update_path(self, asset_id, new_path):
        if asset_id in self.assets:
            self._path_index.pop(_norm_path(self.assets[asset_id]["path"]), None)
            self.assets[asset_id]["path"] = new_path
            self.assets[asset_id]["name"] = os.path.basename(new_path)
            self._path_index[_norm_path(new_path)] = asset_id


class ProjectLibrary:
    """Projects are just named lists of links into the shared asset library - removing an asset
    from a project never deletes it, and the same asset can sit in any number of projects at
    once."""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.projects = {}
        # Lines for the page to show once - see load_records().
        self.load_warnings = []
        self._load()

    def _load(self):
        self.projects = load_records(PROJECT_STORE_PATH, "projects", self.load_warnings)
        # The page reads these keys directly; a hand-edited project missing
        # one shouldn't break the Projects view.
        for project in self.projects.values():
            project.setdefault("name", "Untitled")
            if not isinstance(project.get("asset_ids"), list):
                project["asset_ids"] = []

    def save(self):
        atomic_io.write_json(PROJECT_STORE_PATH, list(self.projects.values()), indent=2)

    def create(self, name):
        project_id = str(uuid.uuid4())
        self.projects[project_id] = {
            "id": project_id,
            "name": name,
            "asset_ids": [],
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        return project_id

    def rename(self, project_id, new_name):
        if project_id in self.projects:
            self.projects[project_id]["name"] = new_name

    def delete(self, project_id):
        self.projects.pop(project_id, None)

    def find_by_name(self, name):
        for project in self.projects.values():
            if project["name"] == name:
                return project
        return None

    def add_asset(self, project_id, asset_id):
        project = self.projects.get(project_id)
        if project and asset_id not in project["asset_ids"]:
            project["asset_ids"].append(asset_id)
            return True
        return False

    def remove_asset(self, project_id, asset_id):
        project = self.projects.get(project_id)
        if project and asset_id in project["asset_ids"]:
            project["asset_ids"].remove(asset_id)

    def unlink_asset_everywhere(self, asset_id):
        """Called when an asset is deleted from the library entirely, so no project keeps a
        dangling reference to an id that no longer exists."""
        for project in self.projects.values():
            if asset_id in project["asset_ids"]:
                project["asset_ids"].remove(asset_id)