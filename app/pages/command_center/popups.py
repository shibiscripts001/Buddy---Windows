#!/usr/bin/env python3
"""
What a hot key shows over whatever app is in front (Resolve, mostly): a
short note of what happened (Osd), and the box that asks a marker's name
(NamePrompt). Plain Qt widgets, not web pages, so they're up at once.

The note never takes the keyboard from Resolve (it's shown without
activating, and clicks go through it). The name box has to - it's typed
in - and hands it back to the window that had it when it closes.
"""

import sys

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from core.i18n import tr

SHOW_MS = 1800


def _screen_rect():
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def _foreground():
    if sys.platform != "win32":
        return None
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()


def _activate(hwnd):
    if sys.platform == "win32" and hwnd:
        import ctypes
        ctypes.windll.user32.SetForegroundWindow(hwnd)


class Osd(QWidget):
    """A note near the bottom of the screen the pointer is on, gone after a moment."""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(self)
        self._label.setTextFormat(Qt.PlainText)
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(560)
        layout.addWidget(self._label)
        self._timer = QTimer(self, singleShot=True, interval=SHOW_MS)
        self._timer.timeout.connect(self.hide)

    def flash(self, text: str, ok: bool, tokens: dict):
        accent = tokens.get("primary", "#4C82D9") if ok else tokens.get("danger", "#E5393B")
        self._label.setText(("✓  " if ok else "✕  ") + tr(text))
        self._label.setStyleSheet(
            f"QLabel {{ background: {tokens.get('surface_container_high', '#2b2b2b')}; "
            f"color: {tokens.get('on_surface', '#eee')}; border: 2px solid {accent}; border-radius: 10px; "
            "padding: 10px 16px; font-size: 14px; font-weight: 600; }")
        self.adjustSize()
        area = _screen_rect()
        self.move(area.center().x() - self.width() // 2, area.bottom() - self.height() - 80)
        self.show()
        self.raise_()
        self._timer.start()


class NamePrompt(QDialog):
    """A marker's name, typed where the pointer is. answered(text) on Enter;
    nothing on Esc or clicking away."""

    answered = Signal(str)

    def __init__(self, title: str, tokens: dict):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._back_to = _foreground()
        self._was_active = False
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        heading = QLabel(tr(title), self)
        hint = QLabel(tr("Enter to add it, Esc to cancel"), self)
        self._field = QLineEdit(self)
        self._field.setMinimumWidth(320)
        self._field.setMaxLength(120)
        for widget in (heading, self._field, hint):
            layout.addWidget(widget)
        surface = tokens.get("surface_container_high", "#2b2b2b")
        text = tokens.get("on_surface", "#eee")
        self.setStyleSheet(
            f"NamePrompt {{ background: {surface}; border: 2px solid {tokens.get('primary', '#4C82D9')}; "
            "border-radius: 10px; }"
            f"QLabel {{ color: {text}; background: transparent; }}"
            f"QLineEdit {{ background: {tokens.get('surface', '#1e1e1e')}; color: {text}; padding: 6px 8px; "
            f"border: 1px solid {tokens.get('outline', '#666')}; border-radius: 6px; font-size: 14px; }}")
        heading.setStyleSheet("font-weight: 700; font-size: 13px;")
        hint.setStyleSheet("font-size: 11px; opacity: .7;")
        self._field.returnPressed.connect(self._done)

    def _done(self):
        text = " ".join(self._field.text().split())
        self.close()
        self.answered.emit(text)

    def ask(self):
        self.adjustSize()
        area = _screen_rect()
        at = QCursor.pos() + QPoint(-self.width() // 2, 16)
        at.setX(max(area.left(), min(at.x(), area.right() - self.width())))
        at.setY(max(area.top(), min(at.y(), area.bottom() - self.height())))
        self.move(at)
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            _activate(int(self.winId()))
        self._field.setFocus()

    def changeEvent(self, event):
        super().changeEvent(event)
        # Clicking away is a cancel, like Esc - once it has had the keyboard.
        if event.type() == event.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active and self.isVisible():
                QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        super().closeEvent(event)
        _activate(self._back_to)   # the keyboard back to Resolve
