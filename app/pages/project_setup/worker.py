#!/usr/bin/env python3
"""
Runs one Sync-tab engine job off the UI thread.

Only pure work belongs in the job: ffmpeg decodes, numpy, plain dict
manipulation. Every Resolve call stays on the main thread - exporting
before the job starts, importing when it finishes - because Resolve's
in-process bridge is not thread-safe and misusing it segfaults rather
than raising.

Cancellation rides on the progress callback the engines already call
between clips, which keeps them free of any threading concept and means a
stop lands at a clean boundary - no half-decoded file, no partial document.
"""

import traceback

from PySide6.QtCore import QThread, Signal

from . import align_engine, otio_engine


class JobCancelled(Exception):
    """Raised inside a worker to unwind a job the user asked to stop.
    Never reaches the UI - the worker turns it into the cancelled signal."""


class EngineWorker(QThread):
    progressed = Signal(str, int, int)
    succeeded = Signal(object)
    failed = Signal(str, bool)
    cancelled = Signal()

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self._job = job
        self._stop = False

    def release(self):
        """Drops the job closure, which is what holds the exported document
        (one leaked document per run once measured at 49 GB in fuscript)."""
        self._job = None

    def cancel(self):
        """Stops the job, and stops it reading: the flag alone only takes
        effect at the next clip boundary, which on a slow share can be most
        of a minute away. Killing the decodes makes each fail at once (every
        caller already treats that as a clip it couldn't read), and the flag
        unwinds the run at the very next callback."""
        self._stop = True
        try:
            from . import ffmpeg_utils
            ffmpeg_utils.terminate_active()
        except Exception:
            pass

    def _report(self, stage, done=0, total=1):
        if self._stop:
            raise JobCancelled()
        self.progressed.emit(stage, done, total)

    def run(self):
        """Nothing may touch a widget from in here - the result goes back as
        a signal, which Qt queues onto the UI thread."""
        try:
            result = self._job(self._report)
        except JobCancelled:
            self.cancelled.emit()
        except (otio_engine.OtioError, align_engine.AlignError) as exc:
            # Written to be read - no type, no stack.
            self.failed.emit(str(exc), True)
        except Exception as exc:
            # Anything else is a bug, and the only place its traceback exists
            # is this thread. str(exc) alone is empty for several builtins.
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}", False)
        else:
            self.succeeded.emit(result)
