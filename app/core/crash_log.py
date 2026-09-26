#!/usr/bin/env python3
"""
~/.buddy/crash.log - what Buddy was doing when it died.

Buddy runs inside Resolve's own script host (fuscript.exe), so a native
crash - an access violation in QtWebEngine, say - takes the process down
with no traceback anywhere: Windows' event log only names the DLL. This
turns on Python's faulthandler into a log file, which on a native crash
writes every thread's Python stack (the page action, the Resolve poll, the
emit that was running), and appends uncaught Python errors too.

No Qt imports - tests/test_crash_log.py runs on plain Python.
"""

import datetime
import faulthandler
import os
import sys
import traceback

# Past this the log is started afresh, keeping the newest half.
MAX_BYTES = 512 * 1024

_handle = None   # faulthandler writes to it at crash time, so it stays open


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
