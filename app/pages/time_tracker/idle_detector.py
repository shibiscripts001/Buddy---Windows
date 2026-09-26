#!/usr/bin/env python3
"""
Idle detection for Time Tracker - reads Windows' system-wide "last input"
timestamp (keyboard/mouse, any application) via GetLastInputInfo, so
tracking can be auto-paused when nobody's actually at the machine even
though Resolve is still open (or a manual session is still running).
"""

import ctypes


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds():
    """Seconds since the last keyboard/mouse input anywhere on the system.
    Returns 0.0 (i.e. "not idle") if the platform call fails for any reason
    - a broken idle check should never itself be the thing that stops
    tracking."""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick_count = ctypes.windll.kernel32.GetTickCount()
        # Both are 32-bit millisecond counters that wrap around every ~49.7
        # days; using the difference modulo 2**32 keeps this correct across
        # that wraparound instead of producing a huge bogus idle time.
        millis_idle = (tick_count - info.dwTime) & 0xFFFFFFFF
        return max(0.0, millis_idle / 1000.0)
    except Exception:
        return 0.0
