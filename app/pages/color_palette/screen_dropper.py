#!/usr/bin/env python3
"""
Windows Screen Color Dropper Tool
Uses pynput's global mouse listener to detect a click anywhere on any monitor,
and Win32 GDI (BitBlt + GetPixel) to sample and preview pixel colors -
combined with a magnified loupe preview.
"""

import ctypes
import os
import time

try:
    from pynput import mouse
    _PYNPUT_AVAILABLE = True
except Exception:
    _PYNPUT_AVAILABLE = False

from PySide6.QtCore import QObject, Signal, Qt, QTimer, QRect, QPoint
from PySide6.QtGui import QGuiApplication, QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import QWidget

from .color_engine import rgb_to_hex, contrasting_text_color

IS_WINDOWS = hasattr(ctypes, "windll")

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32

    _user32.GetDC.restype = ctypes.c_void_p
    _user32.GetDC.argtypes = [ctypes.c_void_p]
    _user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.GetPixel.restype = ctypes.c_uint32
    _gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _gdi32.BitBlt.restype = ctypes.c_int
    _gdi32.BitBlt.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
    ]
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

_SRCCOPY = 0x00CC0020
_CLR_INVALID = 0xFFFFFFFF

# Diagnostic logging for a rare freeze when the dropper opens. Written
# with an immediate flush+close after every line so whatever was logged
# right up to the moment of an actual freeze survives on disk even though
# the process itself has to be killed to recover - it shows which specific
# call inside ScreenColorDropper.__init__ never returns.
_DEBUG_LOG_PATH = os.path.join(os.path.expanduser("~"), ".color_palette_manager", "dropper_debug.log")


def _debug_log(msg):
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG_PATH), exist_ok=True)
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}.{int(time.time() * 1000) % 1000:03d}  {msg}\n")
    except Exception:
        pass


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_uint32)]


if IS_WINDOWS:
    _user32.MonitorFromPoint.restype = ctypes.c_void_p
    _user32.MonitorFromPoint.argtypes = [_POINT, ctypes.c_uint32]
    _user32.GetMonitorInfoW.restype = ctypes.c_int
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
    _user32.GetWindowRect.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    _user32.SetWindowPos.restype = ctypes.c_int
    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
    ]
    _user32.SetWindowDisplayAffinity.restype = ctypes.c_int
    _user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

_MONITOR_DEFAULTTONEAREST = 2
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_NOSENDCHANGING = 0x0400


def _monitor_rect_for(x, y):
    if not IS_WINDOWS:
        return 0, 0, 1920, 1080
    hmon = _user32.MonitorFromPoint(_POINT(x, y), _MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    r = info.rcMonitor
    return r.left, r.top, r.right, r.bottom


def _set_window_pos_physical(hwnd, x, y):
    if IS_WINDOWS:
        _user32.SetWindowPos(hwnd, None, x, y, 0, 0,
                              _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOSENDCHANGING)


_WDA_EXCLUDEFROMCAPTURE = 0x00000011


def _exclude_from_screen_capture(hwnd):
    if not IS_WINDOWS:
        return
    try:
        _user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), ctypes.c_uint32(_WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        pass


def _color_ref_to_rgb(color_ref):
    return (color_ref & 0xFF, (color_ref >> 8) & 0xFF, (color_ref >> 16) & 0xFF)


def _sample_pixel(x, y):
    if not IS_WINDOWS:
        return "#808080"
    hdc = _user32.GetDC(0)
    try:
        color_ref = _gdi32.GetPixel(hdc, x, y)
    finally:
        _user32.ReleaseDC(0, hdc)
    if color_ref == _CLR_INVALID:
        return None
    return rgb_to_hex(_color_ref_to_rgb(color_ref)).upper()


class _GdiCapture:
    def __init__(self, global_x, global_y, span):
        self.span = span
        if IS_WINDOWS:
            hdc_screen = _user32.GetDC(0)
            try:
                self.hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
                self.hbmp = _gdi32.CreateCompatibleBitmap(hdc_screen, span, span)
                self._old_obj = _gdi32.SelectObject(self.hdc_mem, self.hbmp)
                _gdi32.BitBlt(self.hdc_mem, 0, 0, span, span, hdc_screen, global_x, global_y, _SRCCOPY)
            finally:
                _user32.ReleaseDC(0, hdc_screen)

    def pixel_rgb(self, local_x, local_y):
        if not IS_WINDOWS:
            return (128, 128, 128)
        color_ref = _gdi32.GetPixel(self.hdc_mem, local_x, local_y)
        return None if color_ref == _CLR_INVALID else _color_ref_to_rgb(color_ref)

    def close(self):
        if IS_WINDOWS:
            _gdi32.SelectObject(self.hdc_mem, self._old_obj)
            _gdi32.DeleteObject(self.hbmp)
            _gdi32.DeleteDC(self.hdc_mem)


class _LoupePreview(QWidget):
    SAMPLE_SPAN = 15
    ZOOM = 11
    BAR_HEIGHT = 24
    GRAB_MARGIN = 45
    # Hard ceiling on how often a fresh BitBlt screen capture can happen,
    # independent of how fast the cursor moves. Each capture costs a real
    # compositor round-trip (~20-30ms, sometimes much more depending on
    # GPU/DWM load) on the Qt main thread, and pynput's global low-level
    # mouse hook (a separate thread, but GIL-bound) fires on every single
    # mouse move. Dragging the mouse fast enough to keep invalidating the
    # cache pits sustained GIL contention between the two against Windows'
    # low-level-hook timeout, which presents as a full-desktop input freeze
    # rather than a Python exception. Throttling here bounds capture frequency regardless
    # of mouse speed; the loupe preview just lags slightly behind on a fast
    # flick instead of risking the freeze.
    MIN_CAPTURE_INTERVAL = 0.08

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self.sampled_hex = "#000000"
        self._cache = None
        self._cursor_pos = QPoint(0, 0)
        self._last_capture_time = 0.0

        zoom_px = self.SAMPLE_SPAN * self.ZOOM
        self.loupe_w = zoom_px + 2
        self.loupe_h = zoom_px + self.BAR_HEIGHT + 2
        self.setFixedSize(self.loupe_w, self.loupe_h)

        capture_half = (self.SAMPLE_SPAN + self.GRAB_MARGIN * 2) // 2
        self.cursor_offset = capture_half + 12

    def move_to(self, global_x, global_y):
        self._cursor_pos = QPoint(global_x, global_y)

        hwnd = int(self.winId())
        win_w = self.loupe_w
        win_h = self.loupe_h

        if IS_WINDOWS:
            wr = _RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(wr))
            win_w = (wr.right - wr.left) or self.loupe_w
            win_h = (wr.bottom - wr.top) or self.loupe_h

        mon_left, mon_top, mon_right, mon_bottom = _monitor_rect_for(global_x, global_y)

        lx = global_x + self.cursor_offset
        ly = global_y + self.cursor_offset
        if lx + win_w > mon_right:
            lx = global_x - self.cursor_offset - win_w
        if ly + win_h > mon_bottom:
            ly = global_y - self.cursor_offset - win_h
        lx = max(mon_left, min(lx, mon_right - win_w))
        ly = max(mon_top, min(ly, mon_bottom - win_h))

        _set_window_pos_physical(hwnd, lx, ly)
        self.update()

    def _ensure_capture(self):
        span = self.SAMPLE_SPAN + self.GRAB_MARGIN * 2
        margin = self.SAMPLE_SPAN // 2 + 1
        pos = self._cursor_pos

        if self._cache is not None:
            c_global_x, c_global_y, c_span, c_capture = self._cache
            if (c_global_x + margin <= pos.x() <= c_global_x + c_span - margin
                    and c_global_y + margin <= pos.y() <= c_global_y + c_span - margin):
                return c_global_x, c_global_y, c_span, c_capture
            # Cache missed, but re-blitting too often is what risks the
            # system-wide input freeze (see MIN_CAPTURE_INTERVAL above) -
            # reuse the stale capture instead of recapturing on every poll
            # tick during a fast mouse drag.
            if time.monotonic() - self._last_capture_time < self.MIN_CAPTURE_INTERVAL:
                return c_global_x, c_global_y, c_span, c_capture

        half = span // 2
        global_x = pos.x() - half
        global_y = pos.y() - half

        if IS_WINDOWS:
            wr = _RECT()
            _user32.GetWindowRect(int(self.winId()), ctypes.byref(wr))
            self_rect_padded = QRect(wr.left, wr.top, wr.right - wr.left, wr.bottom - wr.top).adjusted(-8, -8, 8, 8)
        else:
            pr = self.devicePixelRatio()
            geom = self.geometry()
            self_rect_padded = QRect(
                int(geom.x() * pr), int(geom.y() * pr),
                int(geom.width() * pr), int(geom.height() * pr)
            ).adjusted(-8, -8, 8, 8)
        capture_rect = QRect(global_x, global_y, span, span)
        if capture_rect.intersects(self_rect_padded):
            if self._cache is not None:
                return self._cache
            return global_x, global_y, 0, None

        if self._cache is not None:
            self._cache[3].close()

        capture = _GdiCapture(global_x, global_y, span)
        self._last_capture_time = time.monotonic()
        self._cache = (global_x, global_y, span, capture)
        return global_x, global_y, span, capture

    def paintEvent(self, event):
        painter = QPainter(self)

        global_x, global_y, span, capture = self._ensure_capture()

        half = self.SAMPLE_SPAN // 2
        zoom_px = self.SAMPLE_SPAN * self.ZOOM

        def sample(dx, dy):
            if capture is None:
                return None
            lx = self._cursor_pos.x() - global_x + dx
            ly = self._cursor_pos.y() - global_y + dy
            if 0 <= lx < span and 0 <= ly < span:
                return capture.pixel_rgb(lx, ly)
            return None

        center = sample(0, 0)
        if center is not None:
            self.sampled_hex = rgb_to_hex(center).upper()

        painter.setPen(QPen(QColor("#f0f0f0"), 1))
        painter.setBrush(QColor("#1e1e1e"))
        painter.drawRect(0, 0, self.loupe_w - 1, self.loupe_h - 1)

        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                rgb = sample(dx, dy)
                c = QColor(*rgb) if rgb is not None else QColor("#000000")
                grid_x = 1 + (dx + half) * self.ZOOM
                grid_y = 1 + (dy + half) * self.ZOOM
                painter.setPen(Qt.NoPen)
                painter.setBrush(c)
                painter.drawRect(grid_x, grid_y, self.ZOOM, self.ZOOM)

        centre_x = 1 + half * self.ZOOM
        centre_y = 1 + half * self.ZOOM
        painter.setPen(QPen(QColor("#ffffff"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(centre_x, centre_y, self.ZOOM, self.ZOOM)
        painter.setPen(QPen(QColor("#000000"), 1))
        painter.drawRect(centre_x - 1, centre_y - 1, self.ZOOM + 2, self.ZOOM + 2)

        bar_y = zoom_px + 1
        painter.setBrush(QColor(self.sampled_hex))
        painter.setPen(Qt.NoPen)
        painter.drawRect(1, bar_y, self.loupe_w - 2, self.BAR_HEIGHT)

        txt_col_hex = contrasting_text_color(self.sampled_hex)
        painter.setPen(QPen(QColor(txt_col_hex)))
        font = QFont("Segoe UI", 10, QFont.Bold)
        painter.setFont(font)
        painter.drawText(1, bar_y, self.loupe_w - 2, self.BAR_HEIGHT, Qt.AlignCenter, self.sampled_hex)

    def cleanup(self):
        if self._cache is not None:
            self._cache[3].close()
            self._cache = None


class _CrosshairMarker(QWidget):
    SIZE = 20

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(self.SIZE, self.SIZE)

        _debug_log("_CrosshairMarker: forcing native winId()")
        hwnd = int(self.winId())
        _debug_log("_CrosshairMarker: winId() returned, calling SetWindowDisplayAffinity")
        _exclude_from_screen_capture(hwnd)
        _debug_log("_CrosshairMarker: SetWindowDisplayAffinity returned")

    def move_to(self, global_x, global_y):
        hwnd = int(self.winId())
        half = self.SIZE // 2
        _set_window_pos_physical(hwnd, global_x - half, global_y - half)

    def paintEvent(self, event):
        painter = QPainter(self)
        mid = self.SIZE // 2
        for color, width in ((QColor("#000000"), 3), (QColor("#ffffff"), 1)):
            painter.setPen(QPen(color, width))
            painter.drawLine(mid, 2, mid, self.SIZE - 2)
            painter.drawLine(2, mid, self.SIZE - 2, mid)


class ScreenColorDropper(QObject):
    color_sampled = Signal(str)
    _click_detected = Signal(object, object)

    def __init__(self, hide_parent_callback=None, show_parent_callback=None, parent=None):
        super().__init__(parent)
        self.hide_parent_callback = hide_parent_callback
        self.show_parent_callback = show_parent_callback
        self._finished = False
        self._cursor_overridden = False

        _debug_log(f"__init__ start (IS_WINDOWS={IS_WINDOWS}, pynput={_PYNPUT_AVAILABLE})")

        if not _PYNPUT_AVAILABLE:
            self._finished = True
            _debug_log("aborting: pynput unavailable")
            # The page shows this (on_dropper catches it).
            raise RuntimeError(
                "It needs the 'pynput' package to detect clicks, and it isn't installed "
                "(or failed to load) on this system.\n\n"
                "Install it from a command prompt with:\n\n"
                "    pip install pynput\n\n"
                "then restart Buddy and try again.")

        if self.hide_parent_callback:
            _debug_log("calling hide_parent_callback")
            self.hide_parent_callback()
            _debug_log("hide_parent_callback returned")

        _debug_log("setOverrideCursor")
        QGuiApplication.setOverrideCursor(Qt.CrossCursor)
        self._cursor_overridden = True

        _debug_log("constructing _LoupePreview")
        self._preview = _LoupePreview()
        _debug_log("constructing _CrosshairMarker")
        self._crosshair = _CrosshairMarker()
        _debug_log("constructing mouse.Controller")
        self._mouse_controller = mouse.Controller()
        _debug_log("starting poll timer")
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_position)
        self._poll_timer.start(33)
        _debug_log("first _poll_position")
        self._poll_position()
        _debug_log("showing preview/crosshair")
        self._preview.show()
        self._crosshair.show()

        self._click_detected.connect(self._handle_click)
        _debug_log("__init__ done, about to touch pynput Listener" if _PYNPUT_AVAILABLE else "__init__ done, no pynput")

        if _PYNPUT_AVAILABLE:
            _debug_log("constructing mouse.Listener")
            self._listener = mouse.Listener(on_click=self._on_click)
            _debug_log("calling listener.start()")
            self._listener.start()
            _debug_log("listener.start() returned")
        else:
            self._listener = None

    def _poll_position(self):
        if self._mouse_controller:
            x, y = self._mouse_controller.position
            x, y = int(x), int(y)
        else:
            x, y = 0, 0
        self._preview.move_to(x, y)
        self._crosshair.move_to(x, y)

    def _on_click(self, x, y, button, pressed):
        if not pressed:
            return
        if button == mouse.Button.left:
            self._click_detected.emit(int(x), int(y))
        else:
            self._click_detected.emit(None, None)
        return False

    def _handle_click(self, x, y):
        hex_color = _sample_pixel(x, y) if x is not None else None
        self.finish(hex_color)

    def finish(self, hex_color):
        if self._finished or not _PYNPUT_AVAILABLE:
            return
        self._finished = True

        self._poll_timer.stop()
        self._preview.cleanup()
        self._preview.close()
        self._preview.deleteLater()
        self._crosshair.close()
        self._crosshair.deleteLater()

        if self._cursor_overridden:
            QGuiApplication.restoreOverrideCursor()
            self._cursor_overridden = False

        if self._listener and self._listener.is_alive():
            self._listener.stop()

        if self.show_parent_callback:
            self.show_parent_callback()

        if hex_color:
            self.color_sampled.emit(hex_color)
