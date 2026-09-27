#!/usr/bin/env python3
"""
Asset Manager - a library of the media you reuse (music, SFX, logos,
stock), kept apart from any one Resolve project, with named Projects that
link into it, and a preview that plays audio and video. A web page
(core/web_page.py): the view is web/index.html + assets.js.

Data is unchanged in place and format: ~/.asset_manager/assets.json and
projects.json (data_manager.py). The lists' rules - filtering, sorting,
folder grouping - are library_view.py; the preview's playback is
player.py, Qt Multimedia without a widget (Chromium here can't play H.264
or AAC), streaming frames and a waveform to the page.

Features: drag files or folders straight in, thumbnails of
images in the preview, a waveform you can click and drag to seek, video
with a scrub bar, projects created and renamed in place, and Add from
library with search.

Protocol:
    to the view    state, list, projects, preview, frame, waveform, media,
                   playhead, playing, available, log, alert, toast
    from the view  view, sort, filter, project, select, add_files,
                   add_folder, import_selected, locate, open_folder, refresh,
                   remove, new_project, rename_project, delete_project,
                   list_available, link_existing, play, seek, volume
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog

from core.i18n import tr, tr_filter
from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage

from . import library_view, resolve_ext
from .data_manager import CATEGORIES, SUPPORTED_EXTS, AssetLibrary, ProjectLibrary
from .player import MediaPreview, image_data_url
from .settings_panel import AssetSettingsMixin

VIEWS = ("all", "folders", "projects")
LOG_LIMIT = 60
# Chromium draws these straight from the file; anything else (TIFF) is
# sent as a scaled copy.
WEB_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}


class AssetManagerPage(AssetSettingsMixin, WebToolPage):
    tool_id = "asset_manager"
    display_name = "Asset Manager"
    category = "Media & Assets"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    file_drops = True

    def build_state(self):
        # ~/.asset_manager/settings.json (the standalone's), for the two
        # behaviour toggles in Settings.
        self.settings = self.host.tool_settings(self.tool_id)
        self.library = AssetLibrary()
        self.projects = ProjectLibrary()
        self.list_view = "all"
        self.sorts = {v: {"column": library_view.SORT_NAME, "reverse": False} for v in VIEWS}
        self.filter = {"category": "All", "search": ""}
        self.project_id = None
        self._nodes = []
        self._preview_id = None
        self._log = []
        self._load_warnings = self.library.load_warnings + self.projects.load_warnings
        for line in self._load_warnings:
            self._add_log(line, "error")

        self.media = MediaPreview(self)
        self.media.frame.connect(lambda aid, src: self.emit("frame", {"id": aid, "src": src}))
        self.media.waveform.connect(lambda aid, w: self.emit("waveform", {"id": aid, **w}))
        self.media.ready.connect(lambda aid, kind: self.emit("media", {"id": aid, "kind": kind}))
        self.media.position.connect(lambda pos, dur: self.emit("playhead", {"position": pos, "duration": dur}))
        self.media.playing.connect(lambda on: self.emit("playing", on))
        self.media.failed.connect(self._media_failed)
        self._pick_default_project()

    def web_ready(self):
        self.emit("log", self._log)
        self._push_state()
        self._push_projects()
        self._push_list()
        self._show_preview(self._preview_id)

    def on_shown(self):
        # Files come and go while the page is elsewhere.
        self._push_list()
        if self._load_warnings:
            warnings, self._load_warnings = self._load_warnings, []
            self.emit("alert", {"title": self.display_name, "text": "\n\n".join(warnings)})

    def hideEvent(self, event):
        # Buddy lives on in the tray: nothing may keep playing out of sight.
        self.media.pause()
        super().hideEvent(event)

    def on_app_quitting(self):
        self.media.shutdown()

    # --------------------------------------------------------------- view --

    def _push_state(self):
        self.emit("state", {"view": self.list_view, "filter": self.filter, "categories": CATEGORIES,
                            "sorts": self.sorts})

    def _add_log(self, text, kind="info"):
        """text: a sentence, or a list of whole sentences (each translated
        on its own)."""
        parts = text if isinstance(text, list) else None
        self._log.append({"time": time.strftime("%H:%M"), "text": " ".join(parts) if parts else text,
                          "parts": parts, "kind": kind})
        del self._log[:-LOG_LIMIT]
        self.emit("log", self._log)

    def _pick_default_project(self):
        if self.project_id not in self.projects.projects:
            names = sorted(self.projects.projects.values(), key=lambda p: p["name"].lower())
            self.project_id = names[0]["id"] if names else None

    def _push_projects(self):
        self._pick_default_project()
        items = sorted(self.projects.projects.values(), key=lambda p: p["name"].lower())
        self.emit("projects", {
            "current": self.project_id,
            "items": [{"id": p["id"], "name": p["name"],
                       "count": sum(1 for a in p["asset_ids"] if a in self.library.assets)} for p in items],
        })

    def _assets_for_view(self):
        assets = self.library.assets
        if self.list_view == "projects":
            project = self.projects.projects.get(self.project_id)
            return [assets[a] for a in project["asset_ids"] if a in assets] if project else []
        if self.list_view == "all":
            return library_view.matching(assets.values(), self.filter["category"], self.filter["search"])
        return list(assets.values())

    def _push_list(self, reveal=()):
        """reveal: assets just added - the page opens their folders and
        scrolls to them, so they don't land unseen in a collapsed group."""
        sort = self.sorts[self.list_view]
        include = bool(self.settings.get("include_folders_in_sort", True))
        self._nodes = library_view.build(
            self._assets_for_view(), sort=sort["column"], reverse=sort["reverse"],
            include_folders_in_sort=include,
            always_bucket=self.list_view == "folders",
            flat=self.list_view == "all" and not self.settings.get("group_all_media_by_folder", True))
        rows = [n for node in self._nodes for n in (node["children"] if node["type"] == "folder" else [node])]
        self.emit("list", {
            "view": self.list_view,
            "nodes": self._nodes,
            "total": len(rows),
            "missing": sum(1 for r in rows if r["missing"]),
            "library_total": len(self.library.assets),
            "project": self.project_id if self.list_view == "projects" else None,
            "reveal": [r["id"] for r in rows if r["id"] in set(reveal)],
        })

    # --------------------------------------------------------- navigation --

    def on_view(self, payload):
        view = (payload or {}).get("view")
        if view in VIEWS:
            self.list_view = view
            self._push_state()
            if view == "projects":
                self._push_projects()
            self._push_list()

    def on_sort(self, payload):
        column = (payload or {}).get("column")
        if column not in (library_view.SORT_NAME, library_view.SORT_ADDED):
            return
        sort = self.sorts[self.list_view]
        sort["reverse"] = not sort["reverse"] if sort["column"] == column else False
        sort["column"] = column
        self._push_state()
        self._push_list()

    def on_filter(self, payload):
        payload = payload or {}
        category = payload.get("category", self.filter["category"])
        self.filter = {"category": category if category in CATEGORIES else "All",
                       "search": str(payload.get("search", self.filter["search"]))[:200]}
        self._push_state()   # the page highlights the type chosen from this
        self._push_list()

    def on_project(self, payload):
        project_id = (payload or {}).get("id")
        if project_id in self.projects.projects:
            self.project_id = project_id
            self._push_projects()
            self._push_list()

    def on_refresh(self, _payload):
        self._push_list()
        self._show_preview(self._preview_id)
        self._add_log("Checked every file again.")

    # ------------------------------------------------------------ preview --

    def on_select(self, payload):
        payload = payload or {}
        loose, groups = library_view.resolve_selection(self._nodes, payload.get("ids"), payload.get("folders"))
        ids = library_view.all_ids(loose, groups)
        if len(ids) == 1 and not groups:
            if ids[0] != self._preview_id:
                self._show_preview(ids[0])
        elif ids:
            self._preview_id = None
            self.media.stop()
            self.emit("preview", {"multi": len(ids)})
        else:
            self._show_preview(None)

    def _show_preview(self, asset_id):
        self._preview_id = asset_id
        self.media.stop()
        asset = self.library.assets.get(asset_id)
        if asset is None:
            self._preview_id = None
            self.emit("preview", None)
            return
        row = library_view.asset_row(asset)
        view = {**row, "image": None}
        if not row["missing"] and row["category"] == "Image":
            if row["ext"] in WEB_IMAGE_EXTS:
                view["image"] = QUrl.fromLocalFile(asset["path"]).toString()
            else:
                image = QImage(asset["path"])
                view["image"] = image_data_url(image) if not image.isNull() else None
        self.emit("preview", view)
        if not row["missing"] and row["category"] in ("Audio", "Video"):
            self.media.show(asset_id, asset["path"], row["category"])

    def _media_failed(self, asset_id, message):
        if asset_id == self._preview_id:
            self.emit("media", {"id": asset_id, "kind": "failed", "message": message})
            self._add_log(message, "warn")

    def on_play(self, payload):
        self.media.toggle((payload or {}).get("id"))

    def on_seek(self, payload):
        payload = payload or {}
        self.media.seek(payload.get("fraction", 0), payload.get("id"))

    def on_volume(self, payload):
        self.media.set_volume((payload or {}).get("value", 1))

    # ------------------------------------------------------------- adding --

    def _add_paths(self, paths, folder=""):
        """Adds media files to the library (and, in Projects, links them to
        the open project). Returns (added, already there)."""
        project = self.projects.projects.get(self.project_id) if self.list_view == "projects" else None
        added = existing = linked = 0
        touched = []   # every asset these paths are, new or not: what to show
        for path in paths:
            asset_id = self.library.add(path)
            if asset_id is None:
                existing += 1
                asset_id = self.library.find_id_by_path(path)
            else:
                added += 1
            if asset_id:
                touched.append(asset_id)
            if project and asset_id and self.projects.add_asset(project["id"], asset_id):
                linked += 1
        self.library.save()
        if project:
            self.projects.save()
        if folder:
            parts = [f"Added 1 asset from {folder}." if added == 1 else f"Added {added} assets from {folder}."]
        else:
            parts = ["Added 1 asset." if added == 1 else f"Added {added} assets."]
        if existing:
            parts.append("1 was in the library already." if existing == 1
                         else f"{existing} were in the library already.")
        if project:
            parts.append(f"{linked} linked into '{project['name']}'.")
        hidden = self._hidden_by_filter(touched)
        if hidden:
            parts.append("1 isn't shown – the type or search above hides it." if hidden == 1
                         else f"{hidden} aren't shown – the type or search above hides them.")
        self._add_log(parts, "success" if added or linked else "info")
        self._push_projects()
        self._push_list(reveal=touched)

    def _hidden_by_filter(self, asset_ids):
        """How many of these the All media view's type and search leave out."""
        if self.list_view != "all":
            return 0
        assets = [self.library.assets[a] for a in dict.fromkeys(asset_ids) if a in self.library.assets]
        shown = library_view.matching(assets, self.filter["category"], self.filter["search"])
        return len(assets) - len(shown)

    @staticmethod
    def _media_under(folder):
        found = []
        for root, _dirs, files in os.walk(folder):
            found.extend(os.path.join(root, f) for f in files if os.path.splitext(f)[1].lower() in SUPPORTED_EXTS)
        return found

    def _needs_project(self):
        if self.list_view == "projects" and self.project_id not in self.projects.projects:
            self.emit("alert", {"title": "Make a project first", "text": "Create a project to add assets to it."})
            return True
        return False

    def on_add_files(self, _payload):
        if self._needs_project():
            return
        filter_str = "Supported media (" + " ".join(f"*{e}" for e in sorted(SUPPORTED_EXTS)) + ");;All files (*.*)"
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Add assets"), "", tr_filter(filter_str))
        if paths:
            self._add_paths([os.path.normpath(p) for p in paths])

    def on_add_folder(self, _payload):
        if self._needs_project():
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Add every supported file in a folder"))
        if folder:
            self._add_paths(self._media_under(folder), os.path.basename(os.path.normpath(folder)))

    def on_files_dropped(self, paths):
        if self._needs_project():
            return
        files = []
        for path in paths:
            if os.path.isdir(path):
                files.extend(self._media_under(path))
            elif os.path.splitext(path)[1].lower() in SUPPORTED_EXTS:
                files.append(os.path.normpath(path))
        if files:
            self._add_paths(files)
        else:
            self.emit("toast", {"text": "None of those are media Asset Manager takes"})

    # ------------------------------------------------------------ actions --

    def on_import_selected(self, payload):
        payload = payload or {}
        loose, groups = library_view.resolve_selection(self._nodes, payload.get("ids"), payload.get("folders"))
        if not loose and not groups:
            self.emit("alert", {"title": "Select something first", "text": "Select assets or a folder to import."})
            return
        try:
            controller = self.host.ensure_connected()
        except ResolveConnectionError:
            return
        assets = self.library.assets
        present = lambda ids: [assets[i]["path"] for i in ids if i in assets and os.path.exists(assets[i]["path"])]  # noqa: E731
        total, error = 0, None
        self.host.set_busy(True, "Importing into the Media Pool…")
        try:
            paths = present(loose)
            if paths:
                total += len(resolve_ext.import_to_media_pool(controller, paths))
            for name, ids in groups:
                paths = present(ids)
                if paths:
                    total += len(resolve_ext.import_folder_to_media_pool(controller, name, paths))
        except Exception as exc:  # noqa: BLE001 - Resolve refused, or no project is open
            error = exc
        finally:
            self.host.set_busy(False)
        if error is not None:
            self._add_log(f"Import failed: {error}", "error")
            self.emit("alert", {"title": "Import failed", "text": str(error)})
        elif not total:
            self.emit("alert", {"title": "Nothing imported", "text": "None of the selected files are on disk any more."})
        else:
            text = "Imported 1 item into the Media Pool" if total == 1 else f"Imported {total} items into the Media Pool"
            bins = ", ".join(n for n, _ in groups)
            self._add_log(f"{text} (folders as new bins: {bins})." if groups else f"{text}.", "success")
            self.emit("toast", {"text": text})

    def on_locate(self, payload):
        asset = self.library.assets.get((payload or {}).get("id"))
        if asset is None:
            return
        start = os.path.dirname(asset["path"])
        path, _ = QFileDialog.getOpenFileName(self, tr("Find {name}").format(name=asset["name"]), start if os.path.isdir(start) else "")
        if path:
            self.library.update_path(asset["id"], os.path.normpath(path))
            self.library.save()
            self._add_log(f"Pointed '{asset['name']}' at its new location.", "success")
            self._push_list()
            self._show_preview(asset["id"])

    def on_open_folder(self, payload):
        asset = self.library.assets.get((payload or {}).get("id"))
        if asset is None:
            return
        folder = os.path.dirname(asset["path"])
        if not os.path.isdir(folder):
            self.emit("alert", {"title": "Folder not found", "text": f"{folder}\n\nisn't there any more."})
            return
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder])
        else:
            subprocess.run(["xdg-open", folder])

    def on_remove(self, payload):
        """Confirmed in the view already. In Projects it only unlinks from
        the project; elsewhere it takes assets out of the library (and every
        project). Files on disk are never touched."""
        payload = payload or {}
        loose, groups = library_view.resolve_selection(self._nodes, payload.get("ids"), payload.get("folders"))
        ids = [i for i in library_view.all_ids(loose, groups) if i in self.library.assets]
        if not ids:
            return
        if self.list_view == "projects":
            project = self.projects.projects.get(self.project_id)
            if not project:
                return
            for asset_id in ids:
                self.projects.remove_asset(project["id"], asset_id)
            self.projects.save()
            self._add_log(f"Took 1 asset out of '{project['name']}' (still in your library)." if len(ids) == 1
                          else f"Took {len(ids)} assets out of '{project['name']}' (still in your library).")
        else:
            for asset_id in ids:
                self.library.remove(asset_id)
                self.projects.unlink_asset_everywhere(asset_id)
            self.library.save()
            self.projects.save()
            self._add_log("Removed 1 asset from the library." if len(ids) == 1
                          else f"Removed {len(ids)} assets from the library.")
        if self._preview_id in ids:
            self._show_preview(None)
        self._push_projects()
        self._push_list()

    # ----------------------------------------------------------- projects --

    def _project_name_problem(self, name, current=None):
        if not name:
            return "Give the project a name."
        existing = self.projects.find_by_name(name)
        if existing and existing["id"] != current:
            return "There's already a project with that name."
        return None

    def on_new_project(self, payload):
        name = str((payload or {}).get("name", "")).strip()
        problem = self._project_name_problem(name)
        if problem:
            self.emit("alert", {"title": "Can't make that project", "text": problem})
            return
        self.project_id = self.projects.create(name)
        self.projects.save()
        self._add_log(f"Created the project '{name}'.", "success")
        self._push_projects()
        self._push_list()

    def on_rename_project(self, payload):
        project = self.projects.projects.get(self.project_id)
        name = str((payload or {}).get("name", "")).strip()
        if not project or name == project["name"]:
            return
        problem = self._project_name_problem(name, project["id"])
        if problem:
            self.emit("alert", {"title": "Can't rename it", "text": problem})
            return
        self.projects.rename(project["id"], name)
        self.projects.save()
        self._add_log(f"Renamed the project to '{name}'.")
        self._push_projects()

    def on_delete_project(self, _payload):
        project = self.projects.projects.get(self.project_id)
        if not project:
            return
        self.projects.delete(project["id"])
        self.projects.save()
        self.project_id = None
        self._add_log(f"Deleted the project '{project['name']}' (its assets are still in your library).")
        self._push_projects()
        self._push_list()

    def on_list_available(self, _payload):
        project = self.projects.projects.get(self.project_id)
        if not project:
            return
        rows = [library_view.asset_row(a) for a in self.library.assets.values() if a["id"] not in project["asset_ids"]]
        rows.sort(key=lambda r: r["name"].lower())
        self.emit("available", {"project": project["name"], "rows": rows})

    def on_link_existing(self, payload):
        project = self.projects.projects.get(self.project_id)
        ids = [i for i in (payload or {}).get("ids") or [] if i in self.library.assets]
        if not project or not ids:
            return
        linked = sum(1 for i in ids if self.projects.add_asset(project["id"], i))
        self.projects.save()
        self._add_log(f"Linked 1 asset from your library into '{project['name']}'." if linked == 1
                      else f"Linked {linked} assets from your library into '{project['name']}'.",
                      "success")
        self._push_projects()
        self._push_list()
