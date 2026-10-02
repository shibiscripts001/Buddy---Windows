#!/usr/bin/env python3
"""
Start menu and desktop shortcuts to Buddy, made from Buddy's own settings
(Settings > General > Shortcuts) - the same ones the installer offers
(installer/Buddy.iss [Icons]): the windowless pythonw.exe of the Python
Buddy runs on, opening Buddy.py in Resolve's Scripts folder, with Buddy's
icon and taskbar identity (main.py's AppUserModelID), so a pinned
shortcut and the running window are one taskbar button.

A shortcut opens Buddy without Resolve's Scripts menu; only DaVinci
Resolve Studio lets a Buddy opened that way connect to it.

Windows only (IShellLink through ctypes); `available` is False elsewhere.
"""

import ctypes
import os
import shutil
import sys
import uuid

available = sys.platform == "win32"

NAME = "Buddy.lnk"
APP_ID = "Buddy.ResolveTools"                 # main.py SetCurrentProcessExplicitAppUserModelID
DESCRIPTION = "Buddy for DaVinci Resolve"
PLACES = ("start_menu", "desktop")

_FOLDERS = {"start_menu": "a77f5d77-2e2b-44c3-a6a2-aba601054a51",   # FOLDERID_Programs
            "desktop": "b4bfcc3a-db2c-424c-b029-7fe99a87c641"}      # FOLDERID_Desktop
_CLSID_SHELL_LINK = "00021401-0000-0000-c000-000000000046"
_IID_SHELL_LINK = "000214f9-0000-0000-c000-000000000046"
_IID_PERSIST_FILE = "0000010b-0000-0000-c000-000000000046"
_IID_PROPERTY_STORE = "886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"
_PKEY_APP_ID = ("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3", 5)         # PKEY_AppUserModel_ID
_VT_LPWSTR = 31
_ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "buddy.ico")


class _Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]


def _guid(text):
    g = _Guid()
    ctypes.memmove(g.data, uuid.UUID(text).bytes_le, 16)
    return g


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


def _method(obj, index, *argtypes):
    """The COM method at `index` of `obj`'s vtable, called with obj first;
    raises OSError on a failing HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])

    def call(*args):
        hr = fn(obj, *args)
        if hr < 0:
            raise OSError(hr & 0xFFFFFFFF, f"COM call failed (0x{hr & 0xFFFFFFFF:08X})")
        return hr
    return call


def _query(obj, iid):
    out = ctypes.c_void_p()
    _method(obj, 0, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(_guid(iid)),
                                                                           ctypes.byref(out))
    return out


def _release(obj):
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def _com():
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)           # S_FALSE / changed mode: COM's already up on this thread - fine
    return ole32


def folder(place):
    """Where a Start menu or desktop shortcut goes (wherever Windows keeps
    that folder - a desktop moved to OneDrive included), or None."""
    if not available or place not in _FOLDERS:
        return None
    path = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    if shell32.SHGetKnownFolderPath(ctypes.byref(_guid(_FOLDERS[place])), 0, None, ctypes.byref(path)) != 0:
        return None
    try:
        return path.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def path(place):
    where = folder(place)
    return os.path.join(where, NAME) if where else None


def exists(place):
    where = path(place)
    return bool(where and os.path.isfile(where))


def launcher():
    """Buddy.py in Resolve's Scripts folder, what Resolve's menu runs."""
    return os.path.expandvars(r"%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\Buddy.py")


def python():
    """The windowless pythonw.exe Buddy runs on (python.exe if there's none),
    or None if no real interpreter is found."""
    from core import startup_manager
    exe = startup_manager.python_for_relaunch()
    if not exe:
        return None
    windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return windowless if os.path.isfile(windowless) else exe


def icon():
    """Buddy's icon somewhere a shortcut can point: the installer's copy, or
    one copied next to it (Buddy itself may run from a folder an update
    replaces)."""
    home = os.path.expandvars(r"%LOCALAPPDATA%\Buddy")
    kept = os.path.join(home, "buddy.ico")
    if not os.path.isfile(kept) and os.path.isfile(_ICON):
        try:
            os.makedirs(home, exist_ok=True)
            shutil.copyfile(_ICON, kept)
        except OSError:
            return _ICON
    return kept if os.path.isfile(kept) else ""


def write(target_path, program, arguments, working_dir, icon_path="", app_id=APP_ID, description=DESCRIPTION):
    """Writes a .lnk at target_path."""
    ole32 = _com()
    link = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_SHELL_LINK)), None, 1,       # CLSCTX_INPROC_SERVER
                                ctypes.byref(_guid(_IID_SHELL_LINK)), ctypes.byref(link))
    if hr < 0:
        raise OSError(hr & 0xFFFFFFFF, "Windows couldn't make a shortcut")
    store = persist = None
    try:
        w = ctypes.c_wchar_p
        _method(link, 20, w)(program)                                   # SetPath
        _method(link, 11, w)(arguments)                                 # SetArguments
        _method(link, 9, w)(working_dir)                                # SetWorkingDirectory
        _method(link, 7, w)(description)                                # SetDescription
        if icon_path:
            _method(link, 17, w, ctypes.c_int)(icon_path, 0)            # SetIconLocation
        if app_id:
            store = _query(link, _IID_PROPERTY_STORE)
            key = _PropertyKey(_guid(_PKEY_APP_ID[0]), _PKEY_APP_ID[1])
            text = ctypes.create_unicode_buffer(app_id)
            value = _PropVariant(_VT_LPWSTR, 0, 0, 0, ctypes.cast(text, ctypes.c_void_p), None)
            _method(store, 6, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))(
                ctypes.byref(key), ctypes.byref(value))                 # SetValue
            _method(store, 7)()                                          # Commit
        persist = _query(link, _IID_PERSIST_FILE)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        _method(persist, 6, w, ctypes.c_int)(target_path, 1)            # Save
    finally:
        _release(persist)
        _release(store)
        _release(link)


def read(target_path):
    """What a .lnk opens: {"program", "arguments", "working_dir", "app_id"}."""
    ole32 = _com()
    link = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_SHELL_LINK)), None, 1,
                                ctypes.byref(_guid(_IID_SHELL_LINK)), ctypes.byref(link))
    if hr < 0:
        raise OSError(hr & 0xFFFFFFFF, "Windows couldn't read a shortcut")
    persist = store = None
    try:
        persist = _query(link, _IID_PERSIST_FILE)
        _method(persist, 5, ctypes.c_wchar_p, ctypes.c_uint32)(target_path, 0)      # Load
        out = {}
        buf = ctypes.create_unicode_buffer(1024)
        _method(link, 3, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)(buf, 1024, None, 0)
        out["program"] = buf.value
        _method(link, 10, ctypes.c_wchar_p, ctypes.c_int)(buf, 1024)
        out["arguments"] = buf.value
        _method(link, 8, ctypes.c_wchar_p, ctypes.c_int)(buf, 1024)
        out["working_dir"] = buf.value
        store = _query(link, _IID_PROPERTY_STORE)
        key = _PropertyKey(_guid(_PKEY_APP_ID[0]), _PKEY_APP_ID[1])
        value = _PropVariant()
        _method(store, 5, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))(ctypes.byref(key),
                                                                                     ctypes.byref(value))
        out["app_id"] = ctypes.wstring_at(value.value) if value.vt == _VT_LPWSTR and value.value else ""
        if value.vt == _VT_LPWSTR and value.value:
            ole32.CoTaskMemFree(ctypes.c_void_p(value.value))
        return out
    finally:
        _release(store)
        _release(persist)
        _release(link)


def make(place):
    """Buddy's shortcut in the Start menu or on the desktop. Raises OSError
    with a reason the user can read if it can't be made."""
    where = path(place)
    if where is None:
        raise OSError("Windows didn't say where that folder is.")
    program, script = python(), launcher()
    if not program:
        raise OSError("Couldn't find the Python Buddy runs on.")
    if not os.path.isfile(script):
        raise OSError(f"Buddy.py isn't in Resolve's Scripts folder ({os.path.dirname(script)}).")
    write(where, program, f'"{script}"', os.path.dirname(script), icon())


def remove(place):
    where = path(place)
    if where and os.path.isfile(where):
        os.remove(where)
