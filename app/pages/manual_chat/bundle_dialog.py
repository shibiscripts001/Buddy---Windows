#!/usr/bin/env python3
"""
"Rebuild from PDF..." - drop in a new Reference Manual, press Build.

Opened from Settings > Ask Buddy, so it is modal over a window that is
itself modal (the Settings window runs under exec()). The build takes
minutes, so it runs on a QThread and the window only ever renders what the
worker's signals carry - bundle_builder has no Qt in it.

A web window (web/build/): a PDF dragged in from Explorer arrives through
the view's file drops (on_files_dropped), or Browse... opens a picker.

Closing mid-build asks first, then cancels. The builder checks for that
between page batches and embedding batches, so a cancel lands within a
couple of seconds, and it writes into a staging folder until the very end,
so the bundle Buddy is using is never left half-replaced.

Protocol:
    to the view    build, drop_hover
    from the view  browse, start, close
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QFileDialog

from core.i18n import tr, tr_filter
from core.message_dialog import confirm
from core.web_page import WebDialog, _theme_host

from . import bundle_builder as bb

# Share of the progress bar each stage gets, from a measured full-manual
# build: reading ~6 min, embedding ~1.5 min, the rest seconds.
STAGE_SPAN = {
    "extract": (0, 78),
    "chunk": (78, 80),
    "embed": (80, 98),
    "write": (98, 100),
}


class _BuildWorker(QThread):
    progressed = Signal(str, int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)
    was_cancelled = Signal()

    def __init__(self, pdf_path: Path, out_dir: Path):
        super().__init__()
        self.pdf_path = pdf_path
        self.out_dir = out_dir
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            result = bb.build_bundle(
                self.pdf_path,
                self.out_dir,
                progress=lambda *a: self.progressed.emit(*a),
                cancelled=lambda: self._cancel,
            )
        except bb.BuildCancelled:
            self.was_cancelled.emit()
        except bb.BuildError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # a pymupdf4llm crash on some odd page etc.
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.succeeded.emit(result)


def checks():
    """What will happen, said before Build is pressed rather than after:
    ([{tone: ok|bad, text, code}], can_build). Missing PyMuPDF blocks the
    build; missing Ollama only downgrades it."""
    has_fitz, has_llm = bb.pymupdf_status()
    reachable, has_model = bb.ollama_status()
    rows = []
    if not has_fitz:
        rows.append({"tone": "bad", "text": "✗ PyMuPDF is not installed. Run:", "code": bb.install_hint()})
    elif not has_llm:
        rows.append({"tone": "bad", "text": "⚠ pymupdf4llm is not installed – passages will lose headings and "
                                            "tables. Run:", "code": bb.install_hint()})
    else:
        rows.append({"tone": "ok", "text": "✓ PDF reader ready."})
    if reachable and has_model:
        rows.append({"tone": "ok", "text": f"✓ Ollama with {bb.EMBED_MODEL} – semantic search will be built."})
    else:
        why = ("Ollama is not running" if not reachable
               else f"Ollama has no {bb.EMBED_MODEL} (run: ollama pull {bb.EMBED_MODEL})")
        rows.append({"tone": "bad", "text": f"⚠ {why} – the bundle will be keyword-only. Start it and reopen this "
                                            "window for semantic search."})
    return rows, has_fitz


class ManualBuildDialog(WebDialog):
    """Modal. Emits `built` with the BuildResult once the new bundle is in
    place, so the chat page can reload it without a restart."""

    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "build")
    file_drops = True

    built = Signal(object)

    def __init__(self, parent, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.pdf_path: Path | None = None
        self.worker: _BuildWorker | None = None
        self._closing = False
        self._progress = None
        self._status = ""
        self._checks, self._can_build = checks()
        super().__init__(_theme_host(parent), parent, "Rebuild the manual bundle", (600, 560))

    def web_ready(self):
        self._push()

    def _push(self):
        pdf = None
        if self.pdf_path:
            size = self.pdf_path.stat().st_size / 1e6 if self.pdf_path.exists() else 0
            pdf = {"name": self.pdf_path.name, "detail": f"{self.pdf_path}  ({size:.0f} MB)"}
        self.emit("build", {
            "pdf": pdf, "checks": self._checks,
            "saves_to": f"Saves to: {self.out_dir}\nThe current bundle is kept next to it as "
                        f"{self.out_dir.name}.previous until the next rebuild.",
            "running": self.worker is not None, "closing": self._closing,
            "can_build": bool(self.pdf_path) and self._can_build and self.worker is None,
            "progress": self._progress, "status": self._status,
        })

    # --------------------------------------------------------------- logic

    def _pick(self, path):
        if self.worker is not None:
            return
        self.pdf_path = Path(path)
        self._status = ""
        self._push()

    def on_browse(self, _payload=None):
        if self.worker is not None:
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose the DaVinci Resolve Reference Manual"), "",
                                              tr_filter("PDF files (*.pdf)"))
        if path:
            self._pick(path)

    def on_files_dropped(self, paths):
        pdf = next((p for p in paths if p.lower().endswith(".pdf")), None)
        if pdf:
            self._pick(pdf)

    def on_start(self, _payload=None):
        if not self.pdf_path or self.worker is not None or not self._can_build:
            return
        self.worker = _BuildWorker(self.pdf_path, self.out_dir)
        self.worker.progressed.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.was_cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self._on_worker_finished)
        self._progress, self._status = 0, "Opening the PDF…"
        self._push()
        self.worker.start()

    def _on_progress(self, stage, done, total, message):
        lo, hi = STAGE_SPAN.get(stage, (0, 100))
        frac = (done / total) if total else 0
        self._progress, self._status = int(lo + (hi - lo) * frac), message
        self._push()

    def _on_succeeded(self, result):
        lines = [f"Done in {result.seconds / 60:.1f} min: {result.chunks:,} passages "
                 f"from {result.chapters} chapters ({result.pages:,} pages)."]
        lines += [f"Note: {w}" for w in result.warnings]
        self._progress, self._status = 100, "\n".join(lines)
        self._push()
        self.built.emit(result)

    def _on_failed(self, message):
        self._progress, self._status = None, f"Build failed – the existing bundle was not changed.\n\n{message}"
        self._push()

    def _on_cancelled(self):
        self._progress, self._status = None, "Stopped. The existing bundle was not changed."
        self._push()

    def _on_worker_finished(self):
        self.worker = None
        self._push()
        if self._closing:
            super().reject()

    # ------------------------------------------------------------- closing

    def on_close(self, _payload=None):
        self.reject()

    def reject(self):
        """Close / Stop / Esc / the window's [X] all land here."""
        if self.worker is None:
            super().reject()
            return
        if self._closing:
            return
        if confirm(self, "Stop the build?", "Stop rebuilding the manual? The bundle Buddy is using now stays as it is.",
                   "Stop", danger=True) and self.worker is not None:
            self._status, self._closing = "Stopping…", True
            self._push()
            self.worker.cancel()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
