#!/usr/bin/env python3
"""
Full-window "frosted glass" busy overlay - promoted from Project Setup's
main.py (originally from Media Relink) into shared code, since the shell
needs exactly one of these covering the whole window rather than one per
tool page.

The defaults are that shell-wide overlay. A page can also scope one to a
single widget - Ask Buddy covers just its transcript while the agent
thinks - with a theme tint, a lighter blur and take_focus=False, so the
page's own controls keep their focus handling. It tracks its parent's
size either way.
"""

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

# The shell overlay's historical look: near-black at ~63%.
DEFAULT_TINT = QColor(20, 18, 24, 160)


class BusyOverlay(QWidget):
    def __init__(self, parent, blur=16, take_focus=True):
        super().__init__(parent)
        self._take_focus = take_focus
        self._blur = max(1, int(blur))
        self.setFocusPolicy(Qt.StrongFocus if take_focus else Qt.NoFocus)
        self.setCursor(Qt.WaitCursor)
        self._bg_pixmap = None
        self._message = "Loading…"
        self._spinner_color = "#D0BCFF"
        self._text_color = "#FFFFFF"
        self._tint = QColor(DEFAULT_TINT)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        parent.installEventFilter(self)
        self.hide()

    def set_tint(self, color):
        """Wash over the blurred snapshot - a QColor with alpha, or None
        for the default dark wash."""
        self._tint = QColor(color) if color is not None else QColor(DEFAULT_TINT)
        self.update()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize and self.isVisible():
            self.setGeometry(obj.rect())
        return False

    def set_text_color(self, hex_color):
        """White is unreadable on a light theme's overlay."""
        self._text_color = hex_color
        self.update()

    def set_accent(self, hex_color):
        self._spinner_color = hex_color
        if self.isVisible():
            self.update()

    def start(self, message="Loading…"):
        self._message = message
        parent = self.parentWidget()
        self.setGeometry(parent.rect())
        # Hidden first: grab() paints children too, and a restart while
        # visible (e.g. a theme change mid-job) would blur the overlay's own
        # previous frame into the new snapshot.
        self.hide()
        self._bg_pixmap = self._blurred_snapshot(parent, self._blur)
        self._angle = 0
        self.show()
        self.raise_()
        if self._take_focus:
            self.setFocus()
        self._timer.start(30)

    def restart(self):
        """Re-snapshot the parent under the current caption - after the
        parent repaints in new colours while the overlay is up."""
        self.start(self._message)

    def set_message(self, message):
        self._message = message
        self.update()

    def stop(self):
        self._timer.stop()
        self.hide()
        self._bg_pixmap = None

    def pump(self):
        """Forces one repaint - use after set_message() from synchronous
        (non-worker-thread) code so the caption actually shows before a
        blocking call runs."""
        QApplication.processEvents()

    def _tick(self):
        self._angle = (self._angle - 8) % 360
        self.update()

    @staticmethod
    def _blurred_snapshot(widget, factor=16):
        pixmap = widget.grab()
        if pixmap.isNull() or pixmap.width() < 12 or pixmap.height() < 12:
            return pixmap
        tiny = pixmap.scaled(
            max(1, pixmap.width() // factor), max(1, pixmap.height() // factor),
            Qt.IgnoreAspectRatio, Qt.SmoothTransformation,
        )
        return tiny.scaled(pixmap.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        event.accept()

    def keyPressEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        # Unaccepted, a wheel event falls through to the parent - which,
        # for a scroll area, would scroll the live content underneath the
        # frozen snapshot.
        event.accept()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._bg_pixmap is not None:
            painter.drawPixmap(self.rect(), self._bg_pixmap)
        painter.fillRect(self.rect(), self._tint)

        cx, cy = self.width() / 2, self.height() / 2
        radius = 22
        pen = QPen(QColor(self._spinner_color))
        pen.setWidth(5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(
            int(cx - radius), int(cy - radius - 28), radius * 2, radius * 2,
            self._angle * 16, 100 * 16,
        )

        painter.setPen(QColor(self._text_color))
        font = painter.font()
        font.setPointSize(13)
        font.setBold(True)
        painter.setFont(font)
        text_rect = self.rect().adjusted(0, int(cy + radius - 10), 0, 0)
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, self._message)
        painter.end()
