#!/usr/bin/env python3
"""
Project Setup - bins from a saved list, a folder on disk imported as bins,
a bin dropped onto the timeline, and multicam sync. A web page
(core/web_page.py): the view is web/index.html + setup.js.

Four tabs, each backed by a Qt-free module:
    Bins      bins.py        the typed list -> a tree (previewed live) -> bins
    Import    importer.py    a folder's tree -> bins, its media imported
    Populate  resolve_ext    the bin open in Resolve -> the timeline
    Sync      otio_engine    export -> transform offline -> import a NEW timeline

The Sync tab's discipline: every operation
writes a new timeline and never edits the current one, so a failure at any
point leaves what you started from untouched. Jobs that read audio run on a
worker thread (worker.py) with their progress and a Cancel button in the
page; everything that touches Resolve stays on the main thread.

Features: a live tree preview of the bin list; a look
inside the chosen folder before importing it (clips by kind, image
sequences counted once) and where it will land; Populate saying which
timeline it will append to (or create); the Sync tab showing the open
timeline and following it; and Cancel for the long audio jobs.

Protocol:
    to the view    state, help, bins_text, bins, import, populate, sync, job,
                   log, alert, toast
    from the view  tab, connect, refresh, bins_text, bins_reset, create_bins,
                   choose_folder, rescan, to_master, import_folder,
                   populate_recursive, add_to_timeline, sync_option, assemble,
                   expand, align, sync_external, collapse, shift, cancel_job
"""

from __future__ import annotations

import os
import time
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from core.i18n import tr
from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage

from . import align_engine, bins, data_manager, importer, otio_engine, resolve_ext, sync_report
from .ffmpeg_utils import find_ffmpeg
from .settings_panel import ProjectSetupSettingsMixin
from .worker import EngineWorker

TABS = ("bins", "import", "populate", "sync")
POLL_MS = 1000
SLOW_POLL_MS = 5000
SLOW_POLL_SECONDS = 0.25
LOG_LIMIT = 80

# Sync options the view can change, with where each is kept. None: this
# session only.
SYNC_OPTIONS = {
    "method": None,
    "sync_audio": None,
    "steps_open": None,
    "delete_silent": "collapse_delete_silent_audio",
    "close_gaps": "shift_close_gaps",
}
METHODS = (align_engine.METHOD_TIMECODE, align_engine.METHOD_WAVEFORM)

# Actions that stay available while a job runs: they change nothing in
# Resolve.
_WHILE_BUSY = {"tab", "bins_text", "sync_option", "populate_recursive", "to_master", "cancel_job"}


class ProjectSetupPage(ProjectSetupSettingsMixin, WebToolPage):
    tool_id = "project_setup"
    display_name = "Project Setup"
    category = "Setup"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        # The same ~/.resolve_bin_generator/settings.json the standalone
        # used, so a saved bin list carries over.
        self.data_mgr = data_manager.DataManager()
        saved_tab = self.data_mgr.settings.get("web_tab")
        self.tab = saved_tab if saved_tab in TABS else "bins"
        self._wrapped_ref = None
        self._wrapped = None
        self._logs = {tab: [] for tab in TABS}
        self._sync_busy = False     # a synchronous step has the overlay up
        self._worker = None
        self._job = None            # {"action", "label", "stage", "done", "total"}
        self._job_finish = None
        self._job_title = ""

        folder =self.data_mgr.settings.get("last_import_folder") or ""
        self.import_folder = folder if folder and os.path.isdir(folder) else ""
        self._import_summary = None
        self._destination = None

        self.recursive = True
        self._populate = {}

        self.sync_options = {"method": align_engine.METHOD_TIMECODE, "sync_audio": True, "steps_open": False}
        self._sync = {}

        self._signature = None
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_resolve)
        self._poll.start()

    def web_ready(self):
        self.emit("help", {"bins": bins.INFO_TEXT, "import": importer.INFO_TEXT,
                           "sync": sync_report.INFO_TEXT})
        self.emit("bins_text", {"text": self.data_mgr.settings.get("bin_list", "")})
        for tab in TABS:
            self.emit("log", {"tab": tab, "entries": self._logs[tab]})
        self._push_state()
        self._push_bins()
        self._push_import()
        self._push_populate()
        self._push_sync()
        self.emit("job", self._job)

    def on_shown(self):
        self._refresh_tab(connect=False)

    def on_app_quitting(self):
        """The shell's quit can't be vetoed, so a running job is cancelled and
        waited out. Nothing is lost: every job writes a NEW timeline."""
        self._poll.stop()
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(30000)
        self.data_mgr.save_settings()

    def _dispatch(self, name, data):
        if (self._sync_busy or self._job) and name not in _WHILE_BUSY:
            return
        super()._dispatch(name, data)

    # ------------------------------------------------------------ Resolve --

    @property
    def controller(self):
        """Project Setup's controller around the shell's CURRENT connection,
        or None - never reconnects (polls and page switches use this).
        Rebuilt when the shell reconnects."""
        shell_controller = self.host.controller if getattr(self.host, "connected", True) else None
        if shell_controller is None:
            return None
        if self._wrapped_ref is not shell_controller:
            self._wrapped_ref = shell_controller
            self._wrapped = resolve_ext.ProjectSetupController(shell_controller)
        return self._wrapped

    def ensure_connected(self):
        """For user actions: reconnects if needed (the shell reports a
        failure itself). The controller, or None."""
        try:
            self.host.ensure_connected()
        except ResolveConnectionError:
            self._push_state()
            return None
        self._push_state()
        return self.controller

    def _current_bin_and_timeline(self, controller):
        project = controller.get_project()
        pool = project.GetMediaPool()
        folder = pool.GetCurrentFolder() or pool.GetRootFolder()
        root = pool.GetRootFolder()
        timeline = project.GetCurrentTimeline()
        return folder, root, timeline

    @staticmethod
    def _id_of(obj):
        if obj is None:
            return None
        try:
            return obj.GetUniqueId() or obj.GetName()
        except Exception:
            try:
                return obj.GetName()
            except Exception:
                return None

    def _poll_resolve(self):
        """While the page is on screen, follow what's open in Resolve: the
        selected bin (Import's destination, Populate's source) and the open
        timeline (Populate's target, Sync's source)."""
        if not self.isVisible() or self._sync_busy or self._job or self.tab == "bins":
            return
        controller = self.controller
        if controller is None:
            if self._signature is not None:
                self._signature = None
                self._refresh_tab(connect=False)
            return
        started = time.monotonic()
        try:
            folder, _root, timeline = self._current_bin_and_timeline(controller)
            signature = (self.tab, self._id_of(folder), self._id_of(timeline))
        except Exception:
            signature = (self.tab, None, None)
        slow = time.monotonic() - started > SLOW_POLL_SECONDS
        self._poll.setInterval(SLOW_POLL_MS if slow else POLL_MS)
        if signature != self._signature:
            self._refresh_tab(connect=False)

    def _refresh_tab(self, connect):
        """Re-read what the current tab shows from Resolve."""
        controller = self.ensure_connected() if connect else self.controller
        try:
            folder, _root, timeline = self._current_bin_and_timeline(controller) if controller else (None, None, None)
            self._signature = (self.tab, self._id_of(folder), self._id_of(timeline))
        except Exception:
            self._signature = (self.tab, None, None)
        if self.tab == "import":
            self._read_destination(controller)
        elif self.tab == "populate":
            self._read_populate(controller)
        elif self.tab == "sync":
            self._read_sync(controller)
        self._push_state()

    # --------------------------------------------------------------- view --

    def _push_state(self):
        self.emit("state", {
            "tab": self.tab,
            "connected": self.controller is not None,
            "busy": bool(self._sync_busy or self._job),
        })

    def _log(self, tab, text, kind="info"):
        entries = self._logs[tab]
        entries.append({"time": time.strftime("%H:%M"), "text": text, "kind": kind})
        del entries[:-LOG_LIMIT]
        self.emit("log", {"tab": tab, "entries": entries})

    def _alert(self, title, text):
        self.emit("alert", {"title": title, "text": text})

    def on_tab(self, payload):
        tab = (payload or {}).get("tab")
        if tab in TABS and tab != self.tab:
            self.tab = tab
            self.data_mgr.settings["web_tab"] = tab
            self.data_mgr.save_settings()
            self._refresh_tab(connect=False)

    def on_connect(self, _payload):
        self._refresh_tab(connect=True)

    def on_refresh(self, _payload):
        if self.tab == "import":
            self._scan()
        self._refresh_tab(connect=True)

    def _set_setting(self, key, value):
        """Saves one of the settings shared with the Settings dialog, and
        redraws what shows it."""
        self.data_mgr.settings[key] = value
        self.data_mgr.save_settings()
        self._push_import()
        self._push_sync()

    # --------------------------------------------------------------- Bins --

    def _push_bins(self):
        rows = bins.plan(self.data_mgr.settings.get("bin_list", ""))
        self.emit("bins", {"rows": rows, "adjusted": sum(1 for r in rows if r["adjusted"])})

    def on_bins_text(self, payload):
        self.data_mgr.settings["bin_list"] = str((payload or {}).get("text", ""))
        self.data_mgr.save_settings()
        self._push_bins()

    def on_bins_reset(self, _payload):
        self.data_mgr.settings["bin_list"] = data_manager.DEFAULT_BIN_LIST
        self.data_mgr.save_settings()
        self.emit("bins_text", {"text": data_manager.DEFAULT_BIN_LIST})
        self._push_bins()

    def on_create_bins(self, _payload):
        rows = bins.plan(self.data_mgr.settings.get("bin_list", ""))
        if not rows:
            self._log("bins", "The list is empty – type at least one bin name.", "error")
            return
        controller = self.ensure_connected()
        if controller is None:
            return
        log = lambda text, kind="info": self._log("bins", text, kind)  # noqa: E731
        self.host.set_busy(True, "Creating bins…")
        try:
            pool = controller.get_project().GetMediaPool()
            created, failed = bins.create(pool, rows, log)
        except Exception as exc:  # noqa: BLE001 - no project open, or the connection dropped
            created = failed = None
            error = exc
        finally:
            self.host.set_busy(False)
        if created is None:
            log(f"Couldn't create the bins: {error}", "error")
            return
        if created:
            log("Created 1 bin in the Media Pool." if created == 1 else f"Created {created} bins in the Media Pool.", "success")
            self.emit("toast", {"text": "Created 1 bin" if created == 1 else f"Created {created} bins"})
        if failed:
            self._alert("Some bins weren't created",
                        f"Created {created}, but {failed} failed. The log below has the details.")

    # ------------------------------------------------------------- Import --

    def _read_destination(self, controller):
        self._destination = None
        if controller is not None:
            try:
                folder, root, _timeline = self._current_bin_and_timeline(controller)
                is_root = self._id_of(folder) == self._id_of(root)
                self._destination = "Master" if is_root else (folder.GetName() or "Master")
            except Exception:
                self._destination = None
        self._push_import()

    def _scan(self):
        self._import_summary = importer.scan(self.import_folder) if self.import_folder else None

    def _push_import(self):
        summary = self._import_summary
        self.emit("import", {
            "folder": self.import_folder,
            "name": os.path.basename(os.path.normpath(self.import_folder)) if self.import_folder else "",
            "summary": {**summary, "items": importer.item_total(summary)} if summary else None,
            "to_master": bool(self.data_mgr.settings.get("import_to_master", False)),
            "destination": self._destination,
            "connected": self.controller is not None,
        })

    def on_choose_folder(self, _payload):
        start = self.import_folder or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, tr("Choose a folder to import"), start)
        if not folder:
            return
        self.import_folder = os.path.normpath(folder)
        self.data_mgr.settings["last_import_folder"] = self.import_folder
        self.data_mgr.save_settings()
        self._scan()
        self._push_import()

    def on_rescan(self, _payload):
        if self.import_folder and not os.path.isdir(self.import_folder):
            self.import_folder = ""
        self._scan()
        self._push_import()

    def on_to_master(self, payload):
        self._set_setting("import_to_master", bool((payload or {}).get("on")))

    def on_import_folder(self, _payload):
        folder = self.import_folder
        if not folder or not os.path.isdir(folder):
            self._alert("Choose a folder first", "Pick the folder on disk you want to bring into the Media Pool.")
            return
        controller = self.ensure_connected()
        if controller is None:
            return
        log = lambda text, kind="info": self._log("import", text, kind)  # noqa: E731
        self._sync_busy = True
        self.host.set_busy(True, "Importing folder…")
        try:
            pool = controller.get_project().GetMediaPool()
            if self.data_mgr.settings.get("import_to_master", False):
                destination = pool.GetRootFolder()
            else:
                destination = pool.GetCurrentFolder() or pool.GetRootFolder()
            result = importer.import_folder(pool, folder, destination, log,
                                            progress=lambda text: self.host.pump_busy(text + "…"))
            if result is not None:
                # Leave the new bin open, so what came in is what's on screen.
                try:
                    pool.SetCurrentFolder(result["top_bin"])
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 - no project open, or the connection dropped
            result = None
            log(f"Couldn't import the folder: {exc}", "error")
        finally:
            self._sync_busy = False
            self.host.set_busy(False)
        if result is None:
            self._refresh_tab(connect=False)
            return
        n = result["imported"]
        log(f"Imported 1 clip into '{result['bin']}', matching the folder's structure." if n == 1
            else f"Imported {n} clips into '{result['bin']}', matching the folder's structure.",
            "success" if n else "warn")
        if result["skipped_dirs"]:
            log(f"{result['skipped_dirs']} subfolder(s) were skipped because their bin couldn't be created.", "warn")
        if n:
            self.emit("toast", {"text": f"Imported 1 clip into '{result['bin']}'" if n == 1
                                else f"Imported {n} clips into '{result['bin']}'"})
        else:
            self._alert("No media found", f"Nothing under '{folder}' was a media file Resolve could import.")
        self._refresh_tab(connect=False)

    # ----------------------------------------------------------- Populate --

    def _read_populate(self, controller):
        info = {"bin": None, "count": None, "timeline": None, "error": ""}
        if controller is not None:
            try:
                folder, root, timeline = self._current_bin_and_timeline(controller)
                is_root = self._id_of(folder) == self._id_of(root)
                info["bin"] = "Master" if is_root else (folder.GetName() or "Master")
                info["timeline"] = timeline.GetName() if timeline else None
                info["count"] = len(controller.get_bin_clips(folder, recursive=self.recursive))
            except Exception as exc:  # noqa: BLE001
                info["error"] = str(exc) or "Couldn't read the Media Pool."
        self._populate = info
        self._push_populate()

    def _push_populate(self):
        self.emit("populate", {**self._populate, "recursive": self.recursive,
                               "connected": self.controller is not None})

    def on_populate_recursive(self, payload):
        self.recursive = bool((payload or {}).get("on"))
        self._read_populate(self.controller)

    def on_add_to_timeline(self, _payload):
        controller = self.ensure_connected()
        if controller is None:
            return
        log = lambda text, kind="info": self._log("populate", text, kind)  # noqa: E731
        # Read the bin fresh: it may have changed in Resolve since the last poll.
        try:
            folder = controller.get_current_bin()
        except Exception as exc:  # noqa: BLE001
            log(f"Couldn't find the open bin: {exc}", "error")
            return
        if folder is None:
            self._alert("No bin found", "Couldn't tell which bin is open in the Media Pool.")
            return
        name = folder.GetName() or "Master"
        try:
            clips = controller.get_bin_clips(folder, recursive=self.recursive)
        except Exception as exc:  # noqa: BLE001
            log(f"Couldn't read the clips in '{name}': {exc}", "error")
            return
        if not clips:
            log(f"'{name}' has no media clips to add.", "warn")
            self._alert("No clips found", f"'{name}' has no media clips to add.")
            return
        self.host.set_busy(True, f"Adding '{name}' to the timeline…")
        try:
            created_new, placed = controller.append_clips_to_timeline(clips, new_timeline_name=name)
            error = None
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            self.host.set_busy(False)
        if error is not None:
            log(f"Couldn't add the bin to the timeline: {error}", "error")
            return
        if created_new:
            log(f"No timeline was open, so created '{name}' with {placed} clip(s).", "success")
        elif placed == len(clips):
            log(f"Added {placed} clip(s) from '{name}' to the end of the timeline.", "success")
        elif placed:
            log(f"Added {placed} of {len(clips)} clip(s) from '{name}' – Resolve couldn't place the rest.", "warn")
        else:
            log(f"Resolve couldn't add any of the {len(clips)} clip(s) from '{name}'.", "error")
            self._alert("Nothing was added", "Resolve didn't place any of the clips. The log below has the details.")
        if placed:
            self.emit("toast", {"text": "Added 1 clip to the timeline" if placed == 1
                                else f"Added {placed} clips to the timeline"})
        self._refresh_tab(connect=False)

    # --------------------------------------------------------------- Sync --

    def _read_sync(self, controller):
        """Reads the open timeline by exporting it (the API walk was
        thousands of round trips on an expanded timeline). Changes nothing."""
        info = {"timeline": None, "summary": None, "error": ""}
        if controller is not None:
            try:
                timeline = controller.get_current_timeline()
                info["timeline"] = timeline.GetName()
                _document, clips, _workdir = otio_engine.read_current_timeline(controller)
                info["summary"] = sync_report.timeline_summary(clips)
            except (otio_engine.OtioError, ResolveConnectionError) as exc:
                info["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                info["error"] = f"Couldn't read the timeline ({exc})."
        self._sync = info
        self._push_sync()

    def _ffmpeg(self):
        return find_ffmpeg(self.data_mgr.settings.get("ffmpeg_path"))

    def _push_sync(self):
        settings = self.data_mgr.settings
        self.emit("sync", {
            **self._sync,
            **self.sync_options,
            "delete_silent": bool(settings.get("collapse_delete_silent_audio", False)),
            "close_gaps": bool(settings.get("shift_close_gaps", False)),
            "ffmpeg": bool(self._ffmpeg()),
            "connected": self.controller is not None,
        })

    def on_sync_option(self, payload):
        payload = payload or {}
        key, value = payload.get("key"), payload.get("value")
        if key not in SYNC_OPTIONS:
            return
        if key == "method":
            if value not in METHODS:
                return
        else:
            value = bool(value)
        setting = SYNC_OPTIONS[key]
        if setting:
            self.data_mgr.settings[setting] = value
            self.data_mgr.save_settings()
        else:
            self.sync_options[key] = value
        self._push_sync()

    def _sync_log(self, lines):
        for text, kind in lines:
            self._log("sync", text, kind)

    def _sync_failed(self, title, exc, trace=""):
        """A step stopped before or after its work: say so plainly for the
        engines' own refusals, with the stack for anything else (a bug -
        kept so a report can be copied straight out of the log)."""
        if isinstance(exc, (otio_engine.OtioError, align_engine.AlignError, ResolveConnectionError)):
            self._log("sync", str(exc), "error")
            self._alert(title, str(exc))
            return
        self._log("sync", f"Error: {type(exc).__name__}: {exc}", "error")
        for line in (trace or "").rstrip().splitlines():
            self._log("sync", f"    {line}", "error")

    def _run_step(self, busy_text, title, work):
        """A step that runs on the main thread with the overlay up (it
        calls Resolve throughout). `work(progress)` returns log lines."""
        controller = self.ensure_connected()
        if controller is None:
            return
        self._sync_busy = True
        self._push_state()
        self.host.set_busy(True, busy_text)

        def progress(stage, done=0, total=1):
            self.host.pump_busy(f"{stage} ({done + 1} of {total})" if total and total > 1 else f"{stage}…")

        lines, error = [], None
        try:
            lines = work(controller, progress)
        except Exception as exc:  # noqa: BLE001
            error = exc
            trace = traceback.format_exc()
        finally:
            self._sync_busy = False
            self.host.set_busy(False)
        if error is None:
            self._sync_log(lines)
        else:
            self._sync_failed(title, error, trace)
        self._refresh_tab(connect=False)

    def _start_job(self, action, label, prepare, job, finish, title):
        """Runs `job(report)` on a worker, then `finish(result)` back here.
        prepare(controller) does the main-thread reading first and returns
        the context job/finish need."""
        if self._worker is not None and self._worker.isRunning():
            self._log("sync", "Something is already running – wait for it to finish.", "warn")
            return
        controller = self.ensure_connected()
        if controller is None:
            return
        self.host.set_busy(True, "Reading the timeline…")
        try:
            context = prepare(controller)
        except Exception as exc:  # noqa: BLE001 - no timeline open, say
            self.host.set_busy(False)
            self._sync_failed(title, exc, traceback.format_exc())
            return
        self.host.set_busy(False)

        self._job = {"action": action, "label": label, "stage": "Starting", "done": 0, "total": 0}
        self._job_finish = lambda result: finish(controller, context, result)
        self._job_title = title
        try:
            self._worker = EngineWorker(lambda report: job(context, report), self)
            self._worker.progressed.connect(self._on_job_progress)
            self._worker.succeeded.connect(self._on_job_done)
            self._worker.failed.connect(self._on_job_failed)
            self._worker.cancelled.connect(self._on_job_cancelled)
            self._worker.start()
        except Exception as exc:  # noqa: BLE001
            self._end_job()
            self._log("sync", f"Couldn't start: {type(exc).__name__}: {exc}", "error")
            return
        self.emit("job", self._job)
        self._push_state()

    def _end_job(self):
        self._job = None
        self._job_finish = None
        # Release the worker and its closure (it holds the whole exported
        # timeline) - Qt would otherwise keep every one alive on the page.
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.release()
            worker.deleteLater()
        self.emit("job", None)
        self._push_state()

    def _on_job_progress(self, stage, done, total):
        if self._job is not None:
            self._job.update(stage=stage, done=done, total=total)
            self.emit("job", self._job)

    def _on_job_cancelled(self):
        self._end_job()
        self._log("sync", "Cancelled – nothing was changed.", "warn")

    def _on_job_failed(self, message, expected):
        title = self._job_title
        self._end_job()
        if expected:
            self._log("sync", message, "error")
            self._alert(title, message)
            return
        first, _, rest = message.partition("\n")
        self._log("sync", f"Error: {first}", "error")
        for line in rest.rstrip().splitlines():
            self._log("sync", f"    {line}", "error")

    def _on_job_done(self, result):
        finish, title = self._job_finish, self._job_title
        self._sync_busy = True
        self.host.set_busy(True, "Importing the new timeline…")
        lines, error, trace = [], None, ""
        try:
            if finish is not None:
                lines = finish(result)
        except Exception as exc:  # noqa: BLE001 - Resolve's side may have changed while the job ran
            error, trace = exc, traceback.format_exc()
        finally:
            self._sync_busy = False
            self.host.set_busy(False)
            self._end_job()
        if error is None:
            self._sync_log(lines)
        else:
            self._sync_failed(title, error, trace)
        self._refresh_tab(connect=False)

    def on_cancel_job(self, _payload):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            if self._job is not None:
                self._job["stage"] = "Stopping"
                self.emit("job", self._job)

    # The steps, in order.

    def on_assemble(self, _payload):
        method = self.sync_options["method"]
        sync_audio = self.sync_options["sync_audio"]
        ffmpeg_path = self._ffmpeg()
        if sync_audio and not ffmpeg_path:
            self._log("sync", "ffmpeg not found – carrying on without the steps that read audio.", "warn")

        def prepare(controller):
            document, _clips, workdir = otio_engine.read_current_timeline(controller)
            return document, workdir, controller.get_current_timeline().GetName()

        def job(context, report):
            document, _workdir, _name = context
            # Deleting silent clips and closing gaps are edit decisions,
            # left to the Collapse and Shift steps.
            return otio_engine.assemble_document(
                document, method, ffmpeg_path=ffmpeg_path, sync_audio=sync_audio,
                remove_silent=False, collapse=True, close_gaps=False, progress_cb=report)

        def finish(controller, context, payload):
            _document, workdir, base_name = context
            result, stats, warnings = payload
            timeline = otio_engine.import_rebuilt(controller, result, workdir, base_name,
                                                  "(Synced)", "assembled.otio")
            return sync_report.assemble(stats, method, timeline.GetTrackCount("video"),
                                        timeline.GetTrackCount("audio"), timeline.GetName(), warnings)

        self._start_job("assemble", "Syncing everything", prepare, job, finish, "Couldn't sync")

    def on_expand(self, _payload):
        def work(controller, progress):
            _timeline, stats, warnings = otio_engine.expand_timeline(controller, progress_cb=progress)
            return sync_report.expand(stats, warnings)
        self._run_step("Expanding timeline…", "Couldn't expand", work)

    def on_align(self, _payload):
        def work(controller, progress):
            document, clips, workdir = otio_engine.read_current_timeline(controller)
            offsets, warnings = otio_engine.compute_offsets_timecode(clips)
            fps = controller.get_timeline_fps()
            by_id = {c.clip_id: c for c in clips}
            anchor_id = min(offsets, key=offsets.get)
            anchor = by_id.get(anchor_id)
            # Where the group ends up is Shift's job, so Align only
            # positions clips relative to the anchor.
            frames, shifted = align_engine.offsets_to_frames(
                offsets, fps, snap_to_start=False, anchor_id=anchor_id,
                anchor_frame=anchor.record_frame if anchor else 0)
            _timeline, stats = otio_engine.apply_alignment(controller, document, clips, frames, workdir)
            return sync_report.align(stats, anchor.name if anchor else None,
                                     anchor.record_frame if anchor else 0, shifted, warnings)
        self._run_step("Aligning clips…", "Couldn't align", work)

    def on_sync_external(self, _payload):
        ffmpeg_path = self._ffmpeg()
        if not ffmpeg_path:
            self._log("sync", "ffmpeg not found – install it, or set its path in Settings, to sync by audio.", "error")
            self._alert("ffmpeg needed", "Syncing external audio needs ffmpeg installed, or its path set in Settings.")
            return

        def prepare(controller):
            return otio_engine.begin_external_sync(controller)

        def job(context, report):
            document, _workdir, _name = context
            return otio_engine.sync_external_audio_document(document, ffmpeg_path, progress_cb=report)

        def finish(controller, context, payload):
            _document, workdir, base_name = context
            result, stats, warnings = payload
            otio_engine.finish_external_sync(controller, result, stats, workdir, base_name)
            return sync_report.external(stats, warnings)

        self._start_job("sync_external", "Syncing external audio", prepare, job, finish, "Couldn't sync")

    def on_collapse(self, _payload):
        delete_silent = bool(self.data_mgr.settings.get("collapse_delete_silent_audio", False))
        ffmpeg_path = self._ffmpeg() if delete_silent else None
        if delete_silent and not ffmpeg_path:
            self._log("sync", "ffmpeg not found – collapsing without checking for silent audio.", "warn")

        def prepare(controller):
            document, _clips, workdir = otio_engine.read_current_timeline(controller)
            timeline = controller.get_current_timeline()
            return (document, workdir, timeline.GetName(),
                    (timeline.GetTrackCount("video"), timeline.GetTrackCount("audio")))

        def job(context, report):
            return otio_engine.collapse_document_from(
                context[0], delete_silent_audio=delete_silent, ffmpeg_path=ffmpeg_path, progress_cb=report)

        def finish(controller, context, payload):
            _document, workdir, base_name, was = context
            result, kept, dropped, warnings = payload
            timeline = otio_engine.import_rebuilt(controller, result, workdir, base_name,
                                                  "(Collapsed)", "collapsed.otio")
            now = (timeline.GetTrackCount("video"), timeline.GetTrackCount("audio"))
            return sync_report.collapse(kept, dropped, was, now, timeline.GetName(), warnings)

        self._start_job("collapse", "Collapsing the timeline", prepare, job, finish, "Couldn't collapse")

    def on_shift(self, _payload):
        close_gaps = bool(self.data_mgr.settings.get("shift_close_gaps", False))

        def work(controller, progress):
            _timeline, stats = otio_engine.shift_timeline(controller, close_gaps=close_gaps, progress_cb=progress)
            return sync_report.shift(stats, close_gaps)
        self._run_step("Shifting timeline…", "Couldn't shift", work)
