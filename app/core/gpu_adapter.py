#!/usr/bin/env python3
"""
Which graphics card Buddy's web pages draw on, on a PC with more than one.

Qt draws on DXGI's first adapter unless QT_D3D_ADAPTER_INDEX says
otherwise, and hands the same card to Chromium (--use-adapter-luid). The
first adapter is the one driving the main monitor - on a desktop with a
card and integrated graphics, the card Resolve is busy with. There, ANGLE's
Direct3D 11 on 12 (core/web_flags.py) went down with the card's driver:
dragging Buddy onto a monitor plugged into the integrated graphics, or
quitting Resolve, reset the NVIDIA driver (nvlddmkm event 153) and Buddy
crashed with it. Drawing on the low-power adapter - the integrated
graphics - survived all of that, at the same 60 frames a second: web pages
ask very little of a GPU, and the card is left to Resolve.

So with two or more hardware adapters, QT_D3D_ADAPTER_INDEX is set to the
one Windows calls minimum-power (IDXGIFactory6::EnumAdapterByGpuPreference).
With one adapter nothing is set. BUDDY_WEB_ADAPTER=<index> picks one by
hand, =default leaves Qt's choice; a QT_D3D_ADAPTER_INDEX already set wins.

No Qt here; nothing raises - without DXGI (not Windows) it's a no-op.
"""

import ctypes
import sys
import uuid

DXGI_ADAPTER_FLAG_SOFTWARE = 2
DXGI_GPU_PREFERENCE_MINIMUM_POWER = 1
_IID_FACTORY1 = uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387")
_IID_FACTORY6 = uuid.UUID("c1b6694f-ff09-44a9-b03c-77900a0a1d17")
_IID_ADAPTER1 = uuid.UUID("29038f61-3839-4626-91fd-086879011a05")


def choose(adapters, low_power_luid):
    """The index to draw on, or None for Qt's own choice. adapters: DXGI's
    order, [{"luid", "software"}]; low_power_luid: Windows' minimum-power
    adapter. Only when there's more than one real card to choose from."""
    hardware = [i for i, a in enumerate(adapters) if not a["software"]]
    if len(hardware) < 2 or low_power_luid is None:
        return None
    for i in hardware:
        if adapters[i]["luid"] == low_power_luid:
            return i
    return None


def apply(environ):
    """Sets QT_D3D_ADAPTER_INDEX if it should be; returns what the crash
    trail should say about it. Before the QApplication exists."""
    if environ.get("QT_D3D_ADAPTER_INDEX"):
        return f"adapter {environ['QT_D3D_ADAPTER_INDEX']} (QT_D3D_ADAPTER_INDEX)"
    manual = environ.get("BUDDY_WEB_ADAPTER", "").strip().lower()
    if manual == "default":
        return "adapter: Qt's choice (BUDDY_WEB_ADAPTER)"
    if manual.isdigit():
        environ["QT_D3D_ADAPTER_INDEX"] = manual
        return f"adapter {manual} (BUDDY_WEB_ADAPTER)"
    try:
        adapters, low_power = _enumerate()
    except Exception as exc:  # noqa: BLE001 - no DXGI, an old Windows: Qt's choice
        return f"adapter: Qt's choice ({type(exc).__name__})"
    index = choose(adapters, low_power)
    if index is None:
        return f"adapter: Qt's choice ({len(adapters)} adapters)"
    environ["QT_D3D_ADAPTER_INDEX"] = str(index)
    return f"adapter {index} ({adapters[index]['name']}, low-power of {len(adapters)})"


# ------------------------------------------------------------------ DXGI --

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint32),
                ("DeviceId", ctypes.c_uint32), ("SubSysId", ctypes.c_uint32), ("Revision", ctypes.c_uint32),
                ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", _LUID), ("Flags", ctypes.c_uint32)]


def _guid(u):
    return (ctypes.c_byte * 16).from_buffer_copy(u.bytes_le)


def _method(obj, index, restype, *argtypes):
    """A COM method: the index-th entry of the object's vtable."""
    vtable = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(obj):
    if obj:
        _method(obj, 2, ctypes.c_ulong)(obj)


def _describe(adapter):
    desc = _DESC1()
    if _method(adapter, 10, ctypes.c_long, ctypes.POINTER(_DESC1))(adapter, ctypes.byref(desc)) < 0:
        raise OSError("GetDesc1 failed")
    luid = (desc.AdapterLuid.HighPart, desc.AdapterLuid.LowPart)
    return {"name": desc.Description, "luid": luid, "software": bool(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)}


def _enumerate():
    """DXGI's adapters in its order, and the minimum-power one's LUID (None
    if this Windows can't say)."""
    if sys.platform != "win32":
        raise OSError("not Windows")
    dxgi = ctypes.WinDLL("dxgi")
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(_guid(_IID_FACTORY1)), ctypes.byref(factory)) < 0:
        raise OSError("CreateDXGIFactory1 failed")
    try:
        adapters = []
        enum1 = _method(factory, 12, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        for i in range(16):
            adapter = ctypes.c_void_p()
            if enum1(factory, i, ctypes.byref(adapter)) < 0:   # DXGI_ERROR_NOT_FOUND: no more
                break
            try:
                adapters.append(_describe(adapter))
            finally:
                _release(adapter)
        low_power = None
        factory6 = ctypes.c_void_p()
        query = _method(factory, 0, ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        if query(factory, ctypes.byref(_guid(_IID_FACTORY6)), ctypes.byref(factory6)) >= 0:
            try:
                adapter = ctypes.c_void_p()
                by_pref = _method(factory6, 29, ctypes.c_long, ctypes.c_uint, ctypes.c_int, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_void_p))
                if by_pref(factory6, 0, DXGI_GPU_PREFERENCE_MINIMUM_POWER, ctypes.byref(_guid(_IID_ADAPTER1)),
                           ctypes.byref(adapter)) >= 0:
                    try:
                        low_power = _describe(adapter)["luid"]
                    finally:
                        _release(adapter)
            finally:
                _release(factory6)
        return adapters, low_power
    finally:
        _release(factory)
