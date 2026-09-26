#!/usr/bin/env python3
"""
~/.buddy/crash.log - what Buddy was doing when it died.

Buddy runs inside Resolve's own script host (fuscript.exe), so a native
crash - an access violation in QtWebEngine, say - takes the process down
with no traceback anywhere: Windows' event log only names the DLL. This
turns on Python's faulthandler into a log file, which on a native crash
writes every thread's Python stack (the page action, the Resolve poll, the
emit that was running), and appends uncaught Python errors too.

A native crash inside Qt's event loop has no Python frame of Buddy's to
show, so ~/.buddy/crash_trail.log (trail()) keeps a running record of what
the web pages were doing - loads, events sent to them, actions from them,
console errors, renderer deaths, tool switches - by name and size only.
Its last lines are what happened just before a crash.

No Qt imports - tests/test_crash_log.py runs on plain Python.
"""

import datetime
import faulthandler
import os
import sys
import traceback

# Past this the log is started afresh, keeping the newest half.
MAX_BYTES = 512 * 1024
# The trail rolls over to <name>.1 past this.
TRAIL_BYTES = 1024 * 1024

_handle = None   # faulthandler writes to it at crash time, so it stays open
_trail = None    # crash_trail.log, open while Buddy runs
_trail_path = None


def _trim(path):
    try:
        if os.path.getsize(path) <= MAX_BYTES:
            return
        with open(path, "rb") as f:
            f.seek(-MAX_BYTES // 2, os.SEEK_END)
            tail = f.read()
        with open(path, "wb") as f:
            f.write(b"[older entries trimmed]\n" + tail[tail.find(b"\n") + 1:])
    except OSError:
        pass


def enable(path, version=""):
    """Starts logging to path. Never raises: no log is better than no Buddy."""
    global _handle
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _trim(path)
        _handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        _handle.write(f"\n--- Buddy {version} started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                      f"(pid {os.getpid()}, {os.path.basename(sys.executable)}) ---\n")
        faulthandler.enable(file=_handle, all_threads=True)
    except (OSError, ValueError, RuntimeError):
        _handle = None
        return False
    previous = sys.excepthook

    def hook(kind, value, tb):
        try:
            _handle.write(f"{datetime.datetime.now():%H:%M:%S} uncaught error:\n")
            _handle.write("".join(traceback.format_exception(kind, value, tb)))
        except (OSError, ValueError):
            pass
        previous(kind, value, tb)

    sys.excepthook = hook
    return True


def enable_trail(path):
    """Starts crash_trail.log: what the web pages were doing, one line per
    event (see trail()). Written straight through - no buffer a native
    crash could lose - so its last lines are what happened just before one.
    Never raises."""
    global _trail, _trail_path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _trail_path = path
        _trail = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        _trail.write(f"\n--- started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} (pid {os.getpid()}) ---\n")
    except OSError:
        _trail = None
        return False
    return True


def trail(kind, text=""):
    """One line in the crash trail: `kind` is what happened (emit, action,
    console, load, renderer, shown), `text` which page and how big - names
    and sizes only, never what a message said. A no-op until enable_trail()."""
    global _trail
    if _trail is None:
        return
    try:
        now = datetime.datetime.now()
        _trail.write(f"{now:%H:%M:%S}.{now.microsecond // 1000:03d} {kind:<8} {text}\n")
        if _trail.tell() > TRAIL_BYTES:
            _trail.close()
            os.replace(_trail_path, _trail_path + ".1")
            _trail = open(_trail_path, "a", encoding="utf-8", errors="replace", buffering=1)
    except (OSError, ValueError):
        _trail = None
