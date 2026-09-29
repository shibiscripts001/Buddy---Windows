#!/usr/bin/env python3
"""
Files to the Recycle Bin (Windows) or the Trash (Mac) instead of deleting
them, so a clean-up can be taken back from there. No Qt.

Windows: the shell's own SHFileOperationW with FOF_ALLOWUNDO - what Explorer
does. On a drive with no Recycle Bin (a network share, some removable
drives) that would delete for good, so FOF_WANTNUKEWARNING is set: Windows
asks first instead of Buddy deleting quietly. That can show a dialog, so
call this on the GUI thread.

Mac: Finder moves them to the Trash (osascript), where Put Back works.
"""

import os
import subprocess
import sys

_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400
_FOF_WANTNUKEWARNING = 0x4000


def _windows(paths):
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    op = SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"    # a double-null-ended list
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI | _FOF_WANTNUKEWARNING
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted:
        raise OSError(f"Windows didn't move them to the Recycle Bin (code {code:#x}).")


def _mac(paths):
    names = ", ".join('POSIX file "%s"' % os.path.abspath(p).replace("\\", "\\\\").replace('"', '\\"')
                      for p in paths)
    done = subprocess.run(["osascript", "-e", f'tell application "Finder" to delete {{{names}}}'],
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise OSError(done.stderr.strip() or "Finder didn't move them to the Trash.")


def to_recycle_bin(paths):
    """Moves the files that are there to the Recycle Bin / Trash. Missing ones
    are skipped. Raises OSError if it didn't happen (nothing is deleted
    instead)."""
    there = [p for p in paths if os.path.exists(p)]
    if not there:
        return
    if sys.platform == "win32":
        _windows(there)
    elif sys.platform == "darwin":
        _mac(there)
    else:
        raise OSError("Buddy can only use the Recycle Bin on Windows and the Trash on a Mac.")
