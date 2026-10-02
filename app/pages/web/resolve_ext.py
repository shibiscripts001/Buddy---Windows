#!/usr/bin/env python3
"""
The Web tab's own Resolve calls: a download goes into a "Downloads" bin of
the Media Pool - sent there with Send to Resolve, or dragged onto Resolve,
which imports it into whatever bin is open and has it moved to Downloads
once it's in.

Free functions taking a controller, like the other tools'
(pages/asset_manager/resolve_ext.py). The shell owns the connection.
"""

import os
import sys

from core.resolve_bridge import ResolveConnectionError

BIN = "Downloads"
# Looking for a dragged file in a pool bigger than this stops at the open bin.
MAX_WALK = 4000


def _pool(controller):
    project = controller.current_project()
    if not project:
        raise ResolveConnectionError("No project is open in Resolve.")
    pool = project.GetMediaPool()
    if pool is None:
        raise ResolveConnectionError("Could not access the Media Pool.")
    return pool


def downloads_bin(pool):
    """The Downloads bin at the Media Pool's top, made if it isn't there."""
    root = pool.GetRootFolder()
    for folder in root.GetSubFolderList() or []:
        if folder.GetName() == BIN:
            return folder
    made = pool.AddSubFolder(root, BIN)
    if made is None:
        raise ResolveConnectionError(f"Could not create a '{BIN}' bin in the Media Pool.")
    return made


def send_to_bin(controller, paths):
    """Imports `paths` into the Downloads bin, leaving the open bin as it
    was. Returns how many items Resolve made."""
    pool = _pool(controller)
    target = downloads_bin(pool)
    before = pool.GetCurrentFolder()
    pool.SetCurrentFolder(target)
    try:
        imported = pool.ImportMedia(list(paths))
    finally:
        if before is not None:
            pool.SetCurrentFolder(before)
    if not imported:
        raise ResolveConnectionError("Resolve reported the import failed (check the files are valid media).")
    return len(imported)


def _id(folder):
    return folder.GetUniqueId() if hasattr(folder, "GetUniqueId") else id(folder)


def _same(a, b):
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _walk(folder):
    """`folder` and every folder under it, level by level."""
    stack = [folder]
    while stack:
        current = stack.pop(0)
        yield current
        stack.extend(current.GetSubFolderList() or [])


def find_clip(pool, path):
    """(clip, folder) for the Media Pool item whose file is `path` - the
    open bin looked in first - or (None, None)."""
    name = os.path.basename(path)
    current = pool.GetCurrentFolder()
    root = pool.GetRootFolder()
    order = ([current] if current is not None else []) + list(_walk(root))
    seen, looked = set(), 0
    for folder in order:
        key = _id(folder)
        if key in seen:
            continue
        seen.add(key)
        for clip in folder.GetClipList() or []:
            looked += 1
            if clip.GetName() == name and _same(clip.GetClipProperty("File Path") or "", path):
                return clip, folder
        if looked > MAX_WALK:
            break
    return None, None


def file_into_bin(controller, path):
    """Moves the Media Pool item for `path` into the Downloads bin. True
    if it's there now; False if Resolve hasn't made it (yet)."""
    pool = _pool(controller)
    clip, folder = find_clip(pool, path)
    if clip is None:
        return False
    target = downloads_bin(pool)
    if folder is not None and folder.GetName() == BIN and _id(folder) == _id(target):
        return True
    return bool(pool.MoveClips([clip], target))


def cursor_over_resolve():
    """True if the mouse is over a Resolve window - where a drag was let
    go. Windows only; False anywhere it can't tell."""
    return program_under_cursor() == "resolve.exe"


def program_under_cursor():
    """The program (lower-case file name) whose window the mouse is over,
    "" if it can't be told. Windows only."""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    kernel32.OpenProcess.restype = wintypes.HANDLE
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return ""
    window = user32.WindowFromPoint(point)
    if not window:
        return ""
    root = user32.GetAncestor(window, 2) or window            # GA_ROOT
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value).lower()
    finally:
        kernel32.CloseHandle(handle)
