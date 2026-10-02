#!/usr/bin/env python3
"""
A page that goes full screen covers the whole screen, Buddy's address bar
included - so nothing shows whose page it is, and a site could draw a copy
of another's sign-in. This lays Buddy's own notice over the top of it
naming the site, for a few seconds when it goes full screen and again when
the pointer reaches the top edge (the way Chrome's does).
"""

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QLabel

SHOW_MS = 6000
EDGE_PX = 60


class FullScreenNotice(QObject):
    def __init__(self, window, view, text):
        super().__init__(window)
        self.window = window
        self.label = QLabel(text, window)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.label.setStyleSheet("color: white; background: rgba(15, 15, 18, 225); padding: 9px 20px;"
                                 "border: 1px solid rgba(255, 255, 255, 90); border-radius: 8px;"
                                 "font: 600 14px 'Segoe UI';")
        self.label.adjustSize()
        self._timer = QTimer(self, singleShot=True, interval=SHOW_MS, timeout=self.label.hide)
        window.installEventFilter(self)
        for target in (view, view.focusProxy()):
            if target is not None:
                target.installEventFilter(self)
        self.reveal()

    def reveal(self):
        self._place()
        self.label.show()
        self.label.raise_()
        self._timer.start()

    def _place(self):
        self.label.adjustSize()
        self.label.move(max(0, (self.window.width() - self.label.width()) // 2), 14)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Resize and obj is self.window:
            self._place()
        elif kind == QEvent.MouseMove and event.position().y() < EDGE_PX and not self.label.isVisible():
            self.reveal()
        return False
