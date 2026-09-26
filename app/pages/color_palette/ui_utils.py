#!/usr/bin/env python3
"""
The one Qt helper Color Palette still needs: its own taskbar button for
the mini palette window. Everything else it shows is its web page (web/).
"""

import ctypes
import sys

_GWL_EXSTYLE = -20
_WS_EX_APPWINDOW = 0x00040000
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

if sys.platform == "win32":
    ctypes.windll.user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    ctypes.windll.user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    ctypes.windll.user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    ctypes.windll.user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    ctypes.windll.user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
    ]


def give_own_taskbar_entry(widget):
    """Forces this top-level window to get its OWN taskbar button on
    Windows, instead of being grouped under whichever window it was
    constructed with as its Qt parent. A Qt dialog created with a parent
    becomes an "owned" window at the Win32 level even when it behaves as
    a real standalone top-level frame (Qt.Window) - Windows groups owned
    windows under their owner's taskbar entry by default, which is why
    e.g. the Contrast Checker didn't get its own icon even though it's
    meant to stay open side-by-side with the main window. Adding the
    WS_EX_APPWINDOW extended style is the standard fix, and doesn't
    change the Qt-level parent/ownership relationship (still raises above
    its parent, still cleaned up the same way) - only the taskbar
    behavior. Call after the widget has a native window (e.g. right after
    show()).

    Deliberately does NOT also clear GWLP_HWNDPARENT (the native owner
    link itself) - that was tried, and while it stopped the minimize/
    restore cascade, it also put Qt's own internal window-state tracking
    out of sync with the real OS state badly enough that minimizing the
    main window could make an owned dialog close outright instead of just
    minimizing with it. See give_no_owner() for the actual fix for that,
    which avoids the owner link ever being created in the first place
    (construct the dialog with no Qt parent) rather than surgically
    un-creating it after Qt has already built its own bookkeeping around
    it being there."""
    if sys.platform != "win32":
        return
    hwnd = int(widget.winId())
    style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    ctypes.windll.user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style | _WS_EX_APPWINDOW)
    # Forces Windows to re-evaluate the taskbar button for this HWND now
    # that its extended style changed - without this, a window that's
    # already visible can keep its old (grouped) taskbar behavior until
    # something else happens to trigger a refresh.
    ctypes.windll.user32.SetWindowPos(
        hwnd, None, 0, 0, 0, 0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
    )
