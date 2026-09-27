#!/usr/bin/env python3
"""
Image Importer - collects images from the clipboard (a copied image, copied
image files, or an image URL), from files dragged in or picked, and drops
them into a Media Pool bin in one go. A web page (core/web_page.py): the
view is web/index.html + importer.js.

Every staged image is a real file in the save folder (staging.py); the
bin import is resolve_ext.import_to_bin. URL downloads run on a
worker thread (clipboard.DownloadWorker) and show in the page until they
land.

Ctrl+V is caught by the page's script (outside a text box) and read here:
the page itself can't read the clipboard. Files dragged in from Explorer
arrive through WebToolPage's file_drops.

Features: thumbnails of everything staged, drag and drop,
an Add files button, downloads showing while they run, and the save folder
and bin name remembered between launches.

Protocol:
    to the view    settings, items, downloads, log, alert, toast
    from the view  paste, add_files, choose_folder, bin_name, remove, clear,
                   import_images
"""

from __future__ import annotations

import os
import time
import uuid

from PySide6.QtCore import QThread, QUrl
from PySide6.QtWidgets import QFileDialog

from core.i18n import tr, tr_filter
from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage

from . import resolve_ext, staging
from .clipboard import DownloadWorker, read_clipboard, unique_filename

LOG_LIMIT = 60
DEFAULTS = {
    "save_folder": os.path.join(os.path.expanduser("~"), "Downloads"),
    "bin_name": "Downloads",
}


class ImageImporterPage(WebToolPage):
    tool_id = "image_importer"
    display_name = "Image Importer"
    category = "Media & Assets"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    file_drops = True

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, DEFAULTS)
        self.staging = staging.Staging()
        self._downloads = {}   # id -> {"url", "thread", "worker"}, until its result is in
        self._running = []     # (thread, worker) until the thread has finished
        self._log = []

    def web_ready(self):
        self._push_settings()
        self._push_items()
        self._push_downloads()
        self.emit("log", self._log)

    def on_app_quitting(self):
        self._stop_downloads()

    # --------------------------------------------------------------- view --

    @property
    def save_folder(self):
        return self.settings.get("save_folder") or DEFAULTS["save_folder"]

    @property
    def bin_name(self):
        return self.settings.get("bin_name") or ""

    def _push_settings(self):
        self.emit("settings", {"save_folder": self.save_folder, "bin_name": self.bin_name})

    def _push_items(self):
        items = []
        for item in self.staging.items:
            exists = os.path.exists(item["path"])
            items.append({
                "id": item["id"], "name": item["name"], "source": item["source"],
                "added_at": item["added_at"], "exists": exists,
                # Cache-busted: a file re-saved under the same name shows fresh.
                "url": QUrl.fromLocalFile(item["path"]).toString() + f"?v={item['id'][:8]}" if exists else "",
                "path": item["path"],
            })
        self.emit("items", items)

    def _push_downloads(self):
        self.emit("downloads", [{"id": k, "url": v["url"]} for k, v in self._downloads.items()])

    def _add_log(self, text, kind="info"):
        self._log.append({"time": time.strftime("%H:%M"), "text": text, "kind": kind})
        del self._log[:-LOG_LIMIT]
        self.emit("log", self._log)

    def _ensure_folder(self):
        folder = self.save_folder
        os.makedirs(folder, exist_ok=True)
        return folder

    def _stage(self, path, source):
        self.staging.add(path, source)
        self._add_log(f"Added {os.path.basename(path)} ({source.lower()}).")

    # ------------------------------------------------------------ adding --

    def on_paste(self, _payload):
        try:
            kind, payload = read_clipboard()
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self._add_log(f"Couldn't read the clipboard: {exc}", "error")
            return
        if kind == "image":
            self._paste_image(payload)
        elif kind == "files":
            self._add_paths(payload, "Copied file")
        elif kind == "url":
            self._start_download(payload)
        elif kind == "text_other":
            self.emit("toast", {"text": "The clipboard has text, but not an image link"})
        else:
            self.emit("toast", {"text": "Nothing to paste – copy an image, image files or an image link first"})

    def _paste_image(self, qimage):
        try:
            folder = self._ensure_folder()
            dest = os.path.join(folder, unique_filename(staging.pasted_name(), folder))
            if not qimage.save(dest, "PNG"):
                raise RuntimeError("the image couldn't be written")
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self._add_log(f"Couldn't save the pasted image: {exc}", "error")
            return
        self._stage(dest, "Pasted image")
        self._push_items()

    def _add_paths(self, paths, source):
        try:
            copied, skipped, errors = staging.copy_into(paths, self._ensure_folder())
        except OSError as exc:
            self._add_log(f"Couldn't use the save folder: {exc}", "error")
            return
        for path in copied:
            self._stage(path, source)
        if skipped:
            names = ", ".join(skipped[:5]) + (" …" if len(skipped) > 5 else "")
            self._add_log(f"Skipped 1 file that isn't an image: {names}" if len(skipped) == 1
                          else f"Skipped {len(skipped)} files that aren't images: {names}", "warn")
        for error in errors:
            self._add_log(f"Couldn't copy {error}", "error")
        if not copied and not errors:
            self.emit("toast", {"text": "No image files there"})
        self._push_items()

    def on_files_dropped(self, paths):
        self._add_paths(paths, "Dropped file")

    def on_add_files(self, _payload):
        exts = " ".join(f"*{e}" for e in sorted(staging.IMAGE_EXTS))
        paths, _filter = QFileDialog.getOpenFileNames(self, tr("Add images"), "", tr_filter(f"Images ({exts})"))
        if paths:
            self._add_paths(paths, "Added file")

    # --------------------------------------------------------- downloads --

    def _start_download(self, url):
        job_id = uuid.uuid4().hex
        thread = QThread(self)
        worker = DownloadWorker(url, self._ensure_folder())
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Bound methods of this page (not lambdas), so Qt queues them onto
        # the UI thread; the job is found again from sender().
        worker.success.connect(self._download_done)
        worker.failure.connect(self._download_failed)
        worker.success.connect(thread.quit)
        worker.failure.connect(thread.quit)
        # Kept referenced until the thread has actually finished - the
        # download list forgets a job as soon as its result is in.
        self._running.append((thread, worker))
        thread.finished.connect(lambda t=thread: self._forget_thread(t))
        self._downloads[job_id] = {"url": url, "thread": thread, "worker": worker}
        thread.start()
        self._push_downloads()

    def _forget_thread(self, thread):
        for pair in [p for p in self._running if p[0] is thread]:
            self._running.remove(pair)
            pair[1].deleteLater()
            thread.deleteLater()

    def _pop_download(self):
        worker = self.sender()
        for job_id, job in list(self._downloads.items()):
            if job["worker"] is worker:
                return self._downloads.pop(job_id)
        return None

    def _download_done(self, dest, _name, _url):
        if self._pop_download() is None:
            return
        self._stage(dest, "Downloaded")
        self._push_downloads()
        self._push_items()

    def _download_failed(self, url, message):
        if self._pop_download() is None:
            return
        self._push_downloads()
        self._add_log(f"Couldn't download {url}: {message}", "error")
        self.emit("alert", {"title": "Download failed", "text": f"Couldn't download the image from\n{url}\n\n{message}"})

    def _stop_downloads(self):
        """Cancel downloads and wait for their threads - a QThread destroyed
        while running takes the process down. A stalled connection only
        gives up at its 15 s socket timeout, so the wait allows for that."""
        self._downloads = {}
        for thread, worker in list(self._running):
            worker.cancel()
            thread.quit()
            thread.wait(20000)

    # ----------------------------------------------------------- the list --

    def on_choose_folder(self, _payload):
        folder = QFileDialog.getExistingDirectory(self, tr("Save images to"), self.save_folder)
        if folder:
            self.settings["save_folder"] = os.path.normpath(folder)
            self.settings.save()
            self._push_settings()
            self._add_log(f"Images are saved to {os.path.normpath(folder)} from now on.")

    def on_bin_name(self, payload):
        self.settings["bin_name"] = str((payload or {}).get("value", "")).strip()
        self.settings.save()

    def on_remove(self, payload):
        removed = self.staging.remove((payload or {}).get("ids"))
        if removed:
            self._push_items()

    def on_clear(self, _payload):
        if self.staging.clear():
            self._add_log("Cleared the list (the files are still in the save folder).")
            self._push_items()

    def on_import_images(self, _payload):
        bin_name = self.bin_name
        if not bin_name:
            self.emit("alert", {"title": "Name the bin", "text": "Type the name of the bin to import into."})
            return
        present, missing = self.staging.split_existing()
        if not present:
            self.emit("alert", {"title": "Nothing to import",
                                "text": "None of the images in the list are on disk any more."
                                if missing else "Paste or drop some images first."})
            return
        try:
            controller = self.host.ensure_connected()
        except ResolveConnectionError:
            return
        n = len(present)
        self.host.set_busy(True, "Importing 1 image…" if n == 1 else f"Importing {n} images…")
        error = None
        try:
            imported = resolve_ext.import_to_bin(controller, [i["path"] for i in present], bin_name,
                                                 log=lambda text: self._add_log(text))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = exc
        finally:
            self.host.set_busy(False)
        if error is not None:
            self._add_log(f"Import failed: {error}", "error")
            self.emit("alert", {"title": "Import failed", "text": str(error)})
            return
        count = len(imported)
        self._add_log(f"Imported 1 image into the '{bin_name}' bin." if count == 1
                      else f"Imported {count} images into the '{bin_name}' bin.", "success")
        if missing:
            self._add_log(f"Left out {len(missing)} that were no longer on disk: {', '.join(missing)}", "warn")
        self.emit("toast", {"text": f"Imported 1 image into '{bin_name}'" if count == 1
                            else f"Imported {count} images into '{bin_name}'"})
        self.staging.clear()
        self._push_items()
