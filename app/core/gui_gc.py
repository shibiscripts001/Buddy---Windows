#!/usr/bin/env python3
"""
Python's garbage collector, run on the GUI thread only.

The cycle collector normally runs inside whatever thread happens to
allocate past its threshold - Ask Buddy's agent, a Resolve poll, a model
download. If the garbage it frees holds a Qt object (a web view, its page,
a channel), that object is destroyed on that thread. QtWebEngine can't take
it: a Chromium check fails and Buddy dies with an access violation in
Qt6WebEngineCore.dll, at a random-looking moment.

So automatic collection is switched off and a timer on the GUI thread
collects instead, between events: young objects every couple of seconds,
everything once a minute. Reference counting still frees almost everything
the moment it's unused; this only decides where the leftover cycles go.
"""

import gc

from PySide6.QtCore import QObject, QTimer

TICK_MS = 2000
FULL_EVERY = 30            # ticks: a full collection about once a minute


class GuiThreadCollector(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self._ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.collect)

    def start(self):
        gc.disable()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        gc.enable()

    def collect(self):
        """One tick: the young generation, or everything every FULL_EVERY."""
        self._ticks += 1
        if self._ticks % FULL_EVERY == 0:
            return gc.collect()
        return gc.collect(0)


def install(app):
    """Starts collecting on the GUI thread for as long as `app` lives."""
    collector = GuiThreadCollector(app)
    collector.start()
    return collector
