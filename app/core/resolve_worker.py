"""
Resolve calls off the UI thread, one at a time.

While the timeline plays, Resolve holds scripting calls until playback
stops: measured 5-14 s at a time from Stills Exporter's 2-second poll
(Buddy's UI thread stalled on each), and over two minutes for a call made
at the start of a long play. A page that polls Resolve on the UI thread
therefore freezes - Windows greys it out as "Not Responding" - for as long
as the timeline plays. A ResolveWorker runs the call on a thread instead,
so the page stays responsive and can say that Resolve is busy.

One job at a time, never two: whether Resolve's scripting connection takes
calls from two threads at once isn't known, so nothing here finds out.
A page's own Resolve calls on the UI thread (a long action behind the busy
overlay) should call wait_idle() first and not start while a job is stuck.
"""

import threading
import time

from PySide6.QtCore import QObject, Signal


class ResolveWorker(QObject):
    """Runs one job at a time on a daemon thread and calls its done(result,
    error) back on the UI thread: the job's return value and None, or None
    and the exception it raised."""

    _finished = Signal(object, object, object)   # done, result, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self._started = 0.0             # time.monotonic() the latest job began
        self._idle = threading.Event()
        self._idle.set()
        # Emitted from the job's thread: Qt queues it to this object's (the UI) thread.
        self._finished.connect(self._deliver)

    def busy(self) -> bool:
        """A job's thread is still running (a finished job's done() may still be
        queued - that doesn't count: its Resolve calls are over)."""
        return not self._idle.is_set()

    def running_for(self) -> float:
        """Seconds the running job has taken so far; 0 when idle."""
        return time.monotonic() - self._started if self.busy() else 0.0

    def start(self, job, done) -> bool:
        """Runs job() on a thread, then done(result, error) on the UI thread.
        False (and nothing started) while the previous job is still running."""
        if self.busy():
            return False
        self._started = time.monotonic()
        self._idle.clear()
        threading.Thread(target=self._run, args=(job, done), daemon=True, name="resolve-worker").start()
        return True

    def wait_idle(self, timeout: float) -> bool:
        """Blocks up to `timeout` seconds for the running job to end. True if
        nothing is running now (its done() may still be queued)."""
        return self._idle.wait(timeout)

    def _run(self, job, done):
        try:
            result, error = job(), None
        except Exception as exc:  # noqa: BLE001 - handed to done() to show
            result, error = None, exc
        self._idle.set()
        try:
            self._finished.emit(done, result, error)
        except RuntimeError:
            pass            # the page closed while Resolve was busy: nobody left to tell

    def _deliver(self, done, result, error):
        done(result, error)
