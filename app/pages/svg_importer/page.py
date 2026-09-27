#!/usr/bin/env python3
"""
SVG Importer - turns SVG files (or Lottie/Bodymovin JSON animations) into
Fusion nodes: engine.py converts them in pure Python and the result goes on
the clipboard, for Ctrl+V into the Fusion page's Flow view. A web page
(core/web_page.py): the view is web/index.html + svg.js.

engine's run_* functions read a fixed set of attributes off an "app"
object, and this page is that object: GRID_SPACING_X/Y, _comp_w/_comp_h/_comp_fps, _log,
_set_os_clipboard and the five *_var wrappers.

Conversions run on a background thread. Anything they report
comes back to the UI thread through _MainThreadInvoker before it reaches
the view. The Resolve connection is the shell's: every action checks
host.ensure_connected() on the UI thread first, and only then lets the
worker call engine.connect_to_resolve() for Fusion and the comp - and the
comp is re-read at the start of every conversion, so the output always
matches the comp you're in.

The page has one list of files for either kind (drag them in, add files
or a folder, remove any), the comp shown up top, a clear result ("copied -
now press Ctrl+V in Fusion") with Copy again, and the engine's detailed
report kept apart from the summary.

Protocol:
    to the view    state, files, options, comp, result, details
    from the view  kind, add_files, add_folder, remove_file, clear_files,
                   option, convert, copy_again, refresh_comp, dump
"""

import json
import os
import tempfile
import threading
import time
import traceback
import uuid

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QFileDialog

from core.i18n import tr, tr_filter
from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage

from . import engine
from .data_manager import DataManager

KINDS = {
    "svg": {"ext": ".svg", "label": "SVG", "filter": "SVG files (*.svg)"},
    "lottie": {"ext": ".json", "label": "Lottie", "filter": "Lottie/Bodymovin JSON (*.json)"},
}
SCALE_NATIVE = "Native (1:1 pixels)"
SCALE_FIT = "Timeline (fit to comp)"
# The view's option names -> data_manager's keys, per kind. The stored
# values stay the strings earlier versions stored, so saved choices carry over.
OPTION_KEYS = {
    "svg": {"scale": "svg_scale_mode", "flow": "paste_orientation", "consolidate": "consolidate_colors"},
    "lottie": {"scale": "lottie_scale_mode", "flow": "lottie_paste_orientation"},
}
DETAILS_LIMIT = 600


class _Var:
    """.get() on a live value - the tkinter-style interface engine.py's
    run_* functions expect."""

    def __init__(self, getter):
        self._getter = getter

    def get(self):
        return self._getter()


class _MainThreadInvoker(QObject):
    """Runs a callable on the GUI thread via a queued signal - how the
    engine's worker threads reach the view and the clipboard."""

    _invoke = Signal(object)

    def __init__(self):
        super().__init__()
        self._invoke.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        fn()

    def call(self, fn):
        self._invoke.emit(fn)


class SVGImporterPage(WebToolPage):
    tool_id = "svg_importer"
    display_name = "SVG Importer"
    category = "Media & Assets"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    file_drops = True

    # Batch import lays every queued file out on a grid inside one paste -
    # sized for a collapsed group box per file (see engine.build_svgs_to_lua).
    GRID_SPACING_X = 250.0
    GRID_SPACING_Y = 150.0

    def build_state(self):
        self._invoker = _MainThreadInvoker()
        # ~/.figma_fusion_importer_settings.json, as earlier versions kept it.
        self.data_mgr = DataManager()
        self.kind = "svg"
        self.files = {"svg": [], "lottie": []}
        self._resolve = self._fusion = self._comp = None
        self._comp_w = self._comp_h = self._comp_fps = None
        self._comp_state = {"status": "unknown"}
        self._busy = None               # "convert" | "dump" | "comp" while a worker runs
        self._copied_this_run = False
        self._last_clipboard_payload = None
        self._result = None
        self._details = []

        s = self.data_mgr.settings
        self.scale_mode_var = _Var(lambda: s.get("svg_scale_mode", SCALE_NATIVE))
        self.paste_orientation_var = _Var(lambda: s.get("paste_orientation", "Horizontal"))
        self.consolidate_colors_var = _Var(lambda: bool(s.get("consolidate_colors", False)))
        self.lottie_scale_mode_var = _Var(lambda: s.get("lottie_scale_mode", SCALE_NATIVE))
        self.lottie_paste_orientation_var = _Var(lambda: s.get("lottie_paste_orientation", "Horizontal"))

    def web_ready(self):
        self._push_state()
        self._push_files()
        self._push_options()
        self.emit("comp", self._comp_state)
        self.emit("result", self._result)
        self.emit("details", self._details)

    def on_shown(self):
        # Only on a live connection: opening the page must never reconnect
        # (and pop an error) by itself.
        if getattr(self.host, "connected", False) and self._comp is None and not self._busy:
            self._run_worker("comp", self._read_comp)

    # --------------------------------------------------------------- view --

    def _push_state(self):
        self.emit("state", {"kind": self.kind, "busy": self._busy})

    def _push_files(self):
        self.emit("files", {k: [{"path": p, "name": os.path.basename(p)} for p in v]
                            for k, v in self.files.items()})

    def _options(self, kind):
        s = self.data_mgr.settings
        keys = OPTION_KEYS[kind]
        out = {"scale": "fit" if str(s.get(keys["scale"], SCALE_NATIVE)).startswith("Timeline") else "native",
               "flow": "vertical" if str(s.get(keys["flow"], "Horizontal")).lower() == "vertical" else "horizontal"}
        if "consolidate" in keys:
            out["consolidate"] = bool(s.get(keys["consolidate"], False))
        return out

    def _push_options(self):
        self.emit("options", {k: self._options(k) for k in KINDS})

    def _log(self, message):
        """engine.py's log - called from worker threads."""
        text = str(message).rstrip("\n")
        self._invoker.call(lambda: self._add_details(text))

    def _add_details(self, text):
        for line in text.splitlines() or [""]:
            kind = "error" if line.startswith("[ERROR]") else "note" if line.startswith(("[NOTE]", "Import notes", "  ")) \
                else "debug" if line.startswith("[DEBUG]") else "info"
            self._details.append({"text": line, "kind": kind})
        del self._details[:-DETAILS_LIMIT]
        self.emit("details", self._details)

    def _set_result(self, result):
        self._result = result
        self.emit("result", result)

    # ------------------------------------------------------------ workers --

    def _run_worker(self, busy, work):
        """Runs work() on a thread, with the page marked busy until it ends."""
        self._busy = busy
        self._push_state()

        def run():
            try:
                work()
            finally:
                self._invoker.call(self._worker_done)

        threading.Thread(target=run, daemon=True).start()

    def _worker_done(self):
        self._busy = None
        self._push_state()

    def _shell_connected(self):
        """UI thread only: the shell's probed connection (reconnecting if
        needed). Must succeed before any worker touches the scripting
        module - the unprobed import is what crashed apps with Resolve
        closed."""
        try:
            self.host.ensure_connected()
            return True
        except ResolveConnectionError:
            return False

    def _read_comp(self):
        """Worker thread: engine.connect_to_resolve(), which also reads the
        comp's size and fps. True when a comp was found."""
        resolve, fusion, comp, comp_w, comp_h, comp_fps, error = engine.connect_to_resolve(log=self._log)
        self._resolve, self._fusion, self._comp = resolve, fusion, comp
        self._comp_w, self._comp_h, self._comp_fps = comp_w, comp_h, comp_fps
        if error:
            self._log("[ERROR] " + error)
        state ={"status": "none", "error": error} if error else \
            {"status": "ok", "width": comp_w, "height": comp_h, "fps": comp_fps}
        self._invoker.call(lambda: self._set_comp(state))
        return not error

    def _set_comp(self, state):
        self._comp_state = state
        self.emit("comp", state)

    def on_refresh_comp(self, _payload):
        if self._busy:
            return
        if not self._shell_connected():
            self._set_comp({"status": "offline"})
            return
        self._run_worker("comp", self._read_comp)

    # ------------------------------------------------------------- files --

    def on_kind(self, payload):
        kind = (payload or {}).get("kind")
        if kind in KINDS and not self._busy:
            self.kind = kind
            self._push_state()

    def _add(self, kind, paths):
        ext = KINDS[kind]["ext"]
        wanted = [os.path.normpath(p) for p in paths if p.lower().endswith(ext) and os.path.isfile(p)]
        queue = self.files[kind]
        for path in wanted:
            if path not in queue:
                queue.append(path)
        self._push_files()
        return wanted

    def on_files_dropped(self, paths):
        if self._busy:
            return
        svgs = self._add("svg", paths)
        jsons = self._add("lottie", paths)
        if svgs and not jsons:
            self.kind = "svg"
        elif jsons and not svgs:
            self.kind = "lottie"
        self._push_state()
        if not svgs and not jsons:
            self.emit("toast", {"text": "Drop .svg files, or Lottie .json files"})

    def on_add_files(self, _payload):
        info = KINDS[self.kind]
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Choose {kind} files").format(kind=info["label"]), "",
                                                tr_filter(f"{info['filter']};;All files (*.*)"))
        if paths:
            self._add(self.kind, paths)

    def on_add_folder(self, _payload):
        info = KINDS[self.kind]
        folder = QFileDialog.getExistingDirectory(self, tr("Choose a folder of {kind} files").format(kind=info["label"]))
        if not folder:
            return
        paths = sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(info["ext"]))
        if not paths:
            self.emit("alert", {"title": f"No {info['label']} files there",
                                "text": f"No {info['ext']} files were found directly in:\n{folder}"})
            return
        self._add(self.kind, paths)

    def on_remove_file(self, payload):
        path = (payload or {}).get("path")
        queue = self.files[self.kind]
        if path in queue and not self._busy:
            queue.remove(path)
            self._push_files()

    def on_clear_files(self, _payload):
        if not self._busy:
            self.files[self.kind] = []
            self._push_files()

    def on_option(self, payload):
        payload = payload or {}
        kind, name, value = payload.get("kind"), payload.get("name"), payload.get("value")
        key = OPTION_KEYS.get(kind, {}).get(name)
        if key is None:
            return
        if name == "scale":
            value = SCALE_FIT if value == "fit" else SCALE_NATIVE
        elif name == "flow":
            value = "Vertical" if value == "vertical" else "Horizontal"
        else:
            value = bool(value)
        self.data_mgr.settings[key] = value
        self.data_mgr.save_settings()
        self._push_options()

    # --------------------------------------------------------- clipboard --

    def _set_os_clipboard(self, text):
        """engine.py's hand-off. Must run on the GUI thread; blocks the
        calling worker until it has."""
        done = threading.Event()

        def do_it():
            QApplication.clipboard().setText(text)
            self._last_clipboard_payload = text
            done.set()

        self._copied_this_run = True
        self._invoker.call(do_it)
        done.wait(timeout=5)

    def _invalidate_stale_clipboard(self):
        """After a failed conversion, take OUR previous paste-text off the
        clipboard, so Ctrl+V can't paste the last file as if it were this
        one - only when the clipboard still holds exactly that."""
        payload = self._last_clipboard_payload
        if not payload:
            return
        done = threading.Event()

        def do_it():
            try:
                if QApplication.clipboard().text() == payload:
                    QApplication.clipboard().clear()
                    self._last_clipboard_payload = None
                    self._add_details("[NOTE] Cleared the clipboard – it still held the previously converted "
                                      "file, and pasting that now would look like this file imported wrongly.")
            except Exception:
                pass
            done.set()

        self._invoker.call(do_it)
        done.wait(timeout=5)

    def on_copy_again(self, _payload):
        if self._last_clipboard_payload:
            QApplication.clipboard().setText(self._last_clipboard_payload)
            self.emit("toast", {"text": "Copied again – paste it in Fusion's Flow view"})

    # ----------------------------------------------------------- convert --

    def on_convert(self, _payload):
        if self._busy:
            return
        kind = self.kind
        paths = [p for p in self.files[kind] if os.path.isfile(p)]
        missing = len(self.files[kind]) - len(paths)
        if not paths:
            self.emit("alert", {"title": "Add a file first",
                                "text": f"Add the {KINDS[kind]['label']} file(s) to convert – "
                                        "drag them here, or use Add files."})
            return
        if not self._shell_connected():
            return
        if missing:
            self._add_details(f"[NOTE] {missing} file(s) in the list no longer exist and were left out.")
        names = [os.path.basename(p) for p in paths]
        self._set_result(None)
        self._add_details(f"--- {time.strftime('%H:%M:%S')} Converting {', '.join(names[:3])}"
                          f"{' …' if len(names) > 3 else ''}")
        self._copied_this_run = False

        def work():
            error = None
            try:
                if not self._read_comp():
                    error = "No Fusion comp is open. Open a clip on the Fusion page, then convert again."
                elif kind == "svg":
                    (engine.run_import(self, paths[0]) if len(paths) == 1 else engine.run_import_all(self, paths))
                else:
                    (engine.run_lottie_import(self, paths[0]) if len(paths) == 1
                     else engine.run_lottie_import_all(self, paths))
            except Exception as exc:  # noqa: BLE001 - the engine's own refusals and bugs alike
                error = str(exc) or type(exc).__name__
                self._log("[ERROR] Conversion failed:\n" + traceback.format_exc())
                self._invalidate_stale_clipboard()
            copied = self._copied_this_run and error is None
            result = {"ok": copied, "kind": kind, "count": len(paths), "names": names,
                      "error": None if copied else (error or "Nothing was copied – see the details.")}
            self._invoker.call(lambda: self._set_result(result))

        self._run_worker("convert", work)

    # ------------------------------------------------------- diagnostics --

    def on_dump(self, _payload):
        if self._busy or not self._shell_connected():
            return

        def work():
            script_path = None
            result_path = os.path.join(tempfile.gettempdir(), f"fusion_dump_result_{uuid.uuid4().hex}.json")
            try:
                if not self._read_comp():
                    return
                script_path = engine.build_dump_worker_script(result_path)
                self._log("Dumping selected node settings from inside Fusion…")
                self._fusion.RunScript(script_path)
                data = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    if os.path.exists(result_path):
                        time.sleep(0.15)
                        try:
                            with open(result_path, "r") as f:
                                data = json.load(f)
                            break
                        except Exception:
                            pass
                    time.sleep(0.2)
                if data is None:
                    self._log("[ERROR] Fusion didn't report back within 10s.")
                elif data.get("ok"):
                    self._log(data.get("message", "Done."))
                    out_path = os.path.join(os.path.expanduser("~"), "Desktop",
                                            f"fusion_node_dump_{uuid.uuid4().hex[:8]}.json")
                    try:
                        with open(out_path, "w", encoding="utf-8") as f:
                            json.dump(data.get("tools"), f, indent=2, default=str)
                        self._log(f"Saved to: {out_path}")
                        self._invoker.call(lambda: self.emit("toast", {"text": "Saved the node dump to your Desktop"}))
                    except Exception:
                        self._log("Couldn't save to Desktop – printing here instead:")
                        self._log(json.dumps(data.get("tools"), indent=2, default=str))
                else:
                    self._log("[ERROR] " + data.get("message", "Unknown failure."))
            except Exception:
                self._log("[ERROR] Dump failed:\n" + traceback.format_exc())
            finally:
                for p in (script_path, result_path):
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass

        self._run_worker("dump", work)
