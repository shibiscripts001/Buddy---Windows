#!/usr/bin/env python3
"""
The apps playing sound on this PC, and Buddy's own volume in the Windows
mixer - Windows' Core Audio session API, through ctypes (no extra
packages). The Web tab's audio ducking (pages/web/ducking.py) reads every
app's level here and turns Buddy's own sessions down and back up, as
Windows itself does for a call.

Windows only: on anything else `available` is False and nothing here
does anything.

A session is one app's sound on one output device. Every device that's
on is read, so DaVinci Resolve playing to a second output still counts.
Use one AudioSessions per thread: it starts COM on the thread it's made on.
"""

import ctypes
import os
import sys

available = sys.platform == "win32"

if available:
    from ctypes import wintypes

    _ole32 = ctypes.OleDLL("ole32")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, text):
            super().__init__()
            _ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(self))

    CLSID_MMDeviceEnumerator = GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
    IID_IMMDeviceEnumerator = GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
    IID_IAudioSessionManager2 = GUID("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
    IID_IAudioSessionControl2 = GUID("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
    IID_IAudioMeterInformation = GUID("C02216F6-8C67-4B5B-9D00-D008E73E0064")
    IID_ISimpleAudioVolume = GUID("87CE5498-68D6-44E5-9215-6DA47EF883D8")

    CLSCTX_ALL = 23
    E_RENDER = 0
    DEVICE_STATE_ACTIVE = 1
    COINIT_MULTITHREADED = 0
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.OpenProcess.restype = wintypes.HANDLE


def _call(obj, index, *args, argtypes=()):
    """Method `index` of a COM object's table, called; raises on a failing HRESULT."""
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return prototype(table[index])(obj, *args)


def _release(obj):
    if obj:
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(obj)


def _query(obj, iid):
    out = ctypes.c_void_p()
    _call(obj, 0, ctypes.byref(iid), ctypes.byref(out), argtypes=(ctypes.c_void_p, ctypes.c_void_p))
    return out


def processes():
    """{pid: (parent pid, exe name)} for everything running."""
    if not available:
        return {}
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out = {}
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def family(root, table):
    """`root` and every process it started, and theirs - Buddy and its
    Chromium helpers, one of which plays the Web tab's sound."""
    out, grew = {root}, True
    while grew:
        grew = False
        for pid, (parent, _exe) in table.items():
            if parent in out and pid not in out and pid != parent:
                out.add(pid)
                grew = True
    return out


class Session:
    """One app's sound on one device: its process, how loud it is right
    now (0..1), and - for Buddy's own - a handle to its volume."""

    def __init__(self, pid, peak, system, volume):
        self.pid, self.peak, self.system = pid, peak, system
        self._volume = volume

    def get_volume(self):
        level = ctypes.c_float()
        _call(self._volume, 4, ctypes.byref(level), argtypes=(ctypes.c_void_p,))
        return level.value

    def set_volume(self, level):
        _call(self._volume, 3, ctypes.c_float(max(0.0, min(1.0, level))), None,
              argtypes=(ctypes.c_float, ctypes.c_void_p))

    def close(self):
        _release(self._volume)
        self._volume = None


class AudioSessions:
    """Reads the sessions on every output device. scan() each time - apps
    come and go - and close() what it gives back."""

    def __init__(self):
        self._enumerator = None
        if not available:
            return
        try:
            _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        except OSError:
            pass                            # COM already started on this thread, another way
        enumerator = ctypes.c_void_p()
        _ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enumerator))
        self._enumerator = enumerator

    def _devices(self):
        collection = ctypes.c_void_p()
        _call(self._enumerator, 3, E_RENDER, DEVICE_STATE_ACTIVE, ctypes.byref(collection),
              argtypes=(ctypes.c_int, wintypes.DWORD, ctypes.c_void_p))
        try:
            count = wintypes.UINT()
            _call(collection, 3, ctypes.byref(count), argtypes=(ctypes.c_void_p,))
            for i in range(count.value):
                device = ctypes.c_void_p()
                _call(collection, 4, i, ctypes.byref(device), argtypes=(wintypes.UINT, ctypes.c_void_p))
                yield device
        finally:
            _release(collection)

    def scan(self, volumes_for=()):
        """[Session] on every device. Only sessions of processes in
        `volumes_for` keep a volume handle (the rest can't be changed)."""
        if not self._enumerator:
            return []
        out = []
        for device in self._devices():
            try:
                out += self._device_sessions(device, set(volumes_for))
            except OSError:
                pass                        # a device that went away mid-read
            finally:
                _release(device)
        return out

    def _device_sessions(self, device, wanted):
        manager = ctypes.c_void_p()
        _call(device, 3, ctypes.byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, ctypes.byref(manager),
              argtypes=(ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p))
        sessions = ctypes.c_void_p()
        out = []
        try:
            _call(manager, 5, ctypes.byref(sessions), argtypes=(ctypes.c_void_p,))
            count = ctypes.c_int()
            _call(sessions, 3, ctypes.byref(count), argtypes=(ctypes.c_void_p,))
            for i in range(count.value):
                control = ctypes.c_void_p()
                _call(sessions, 4, i, ctypes.byref(control), argtypes=(ctypes.c_int, ctypes.c_void_p))
                try:
                    out.append(self._session(control, wanted))
                except OSError:
                    pass
                finally:
                    _release(control)
        finally:
            _release(sessions)
            _release(manager)
        return out

    @staticmethod
    def _session(control, wanted):
        control2 = _query(control, IID_IAudioSessionControl2)
        try:
            pid = wintypes.DWORD()
            _call(control2, 14, ctypes.byref(pid), argtypes=(ctypes.c_void_p,))
            system = _call(control2, 15) == 0                         # S_OK: the system sounds
        finally:
            _release(control2)
        meter = _query(control, IID_IAudioMeterInformation)
        try:
            peak = ctypes.c_float()
            _call(meter, 3, ctypes.byref(peak), argtypes=(ctypes.c_void_p,))
        finally:
            _release(meter)
        volume = _query(control, IID_ISimpleAudioVolume) if pid.value in wanted else None
        return Session(pid.value, peak.value, system, volume)

    def close(self):
        _release(self._enumerator)
        self._enumerator = None


def own_family():
    """Buddy's process and its helpers."""
    return family(os.getpid(), processes())
