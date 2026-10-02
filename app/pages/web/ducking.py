#!/usr/bin/env python3
"""
Audio ducking for the Web tab: while DaVinci Resolve (or, if the user
chose it, any other app) is making sound, the Web tab's sound fades down,
and fades back up once it's been quiet for a moment - music under a
timeline being played back, the way Windows lowers other apps for a call.

The browser's sound comes out of one of Buddy's own Chromium helpers, a
session of its own in the Windows mixer (core/audio_sessions.py), so it's
that session's volume that's turned down - every page and player at once,
whatever the site - and put back to where the user had it.

Duck is the rule (no Qt, unit-tested); Ducker runs it on a thread of its
own, reading the mixer ten times a second while a tab is playing.
"""

import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from core import audio_sessions

MODES = {"off": "Off", "resolve": "When DaVinci Resolve plays sound", "any": "When any other app plays sound"}
DEFAULT_MODE = "resolve"
# How loud the Web tab stays while lowered, in percent: 0 silent, 100 not
# lowered at all, in steps of LEVEL_STEP (Settings > Tools > Web > Sound).
LEVEL_STEP = 5
DEFAULT_LEVEL = 30


def clean_level(value):
    """A stored or slid duck level -> a whole percent on the step, or None."""
    try:
        level = int(round(float(value) / LEVEL_STEP)) * LEVEL_STEP
    except (TypeError, ValueError):
        return None
    return level if 0 <= level <= 100 else None

RESOLVE_EXES = {"resolve.exe", "resolve"}
LOUD = 0.01                 # a session's peak above this is sound, not silence
HOLD = 1.5                  # seconds of quiet before the Web tab comes back up
FADE_DOWN = 0.3             # seconds to fade down, and up
FADE_UP = 1.2


def triggers(sessions, mode, own, names):
    """True if another app's sound should duck the Web tab. `sessions`:
    [(pid, peak, is_system_sounds)], `own`: Buddy's pids, `names`: {pid: exe}."""
    if mode not in ("resolve", "any"):
        return False
    for pid, peak, system in sessions:
        if system or pid in own or peak < LOUD:
            continue
        if mode == "any" or names.get(pid, "").lower() in RESOLVE_EXES:
            return True
    return False


class Duck:
    """How far down the Web tab is (1.0 = as the user set it), moving
    toward the duck level while there's sound and back after HOLD."""

    def __init__(self):
        self.factor = 1.0
        self._last_loud = None
        self._at = None

    def step(self, now, loud, level):
        if loud:
            self._last_loud = now
        target = level if self._last_loud is not None and now - self._last_loud < HOLD else 1.0
        dt = 0.0 if self._at is None else max(0.0, now - self._at)
        self._at = now
        span = max(1e-6, 1.0 - level)
        if target < self.factor:
            self.factor = max(target, self.factor - span * dt / FADE_DOWN)
        elif target > self.factor:
            self.factor = min(target, self.factor + span * dt / FADE_UP)
        return self.factor


class Ducker(QObject):
    """Reads the mixer and turns Buddy's sessions down and up. Set `mode`,
    `level` (0..1) and `active` (a tab is playing) from the GUI thread;
    `ducked` says when the Web tab is lowered, `saved` the volume to put
    back if Buddy closes while it is (None once it's put back)."""

    ducked = Signal(bool)
    saved = Signal(object)

    def __init__(self, parent=None, restore=None):
        super().__init__(parent)
        self.mode = "off"
        self.level = DEFAULT_LEVEL / 100
        self.active = False
        self._restore = restore if isinstance(restore, (int, float)) and 0 <= restore <= 1 else None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if audio_sessions.available and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="web-ducking", daemon=True)
            self._thread.start()

    def stop(self):
        """Puts the volume back and ends the thread (waits for it)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self):
        try:
            mixer = audio_sessions.AudioSessions()
        except OSError:
            return
        duck, originals, was_ducked = Duck(), {}, False
        table, own, table_at = {}, set(), 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                busy = self.active or duck.factor < 1.0 or self._restore is not None
                if not busy or self.mode == "off" and duck.factor >= 1.0 and self._restore is None:
                    self._stop.wait(0.5)
                    continue
                if now - table_at > 5:
                    table = audio_sessions.processes()
                    own = audio_sessions.family(os.getpid(), table)
                    table_at = now
                sessions = mixer.scan(volumes_for=own)
                try:
                    mine = [s for s in sessions if s.pid in own and not s.system]
                    if self._restore is not None and mine:
                        # Buddy closed last time with the Web tab still down.
                        for s in mine:
                            s.set_volume(self._restore)
                        self._restore = None
                        self.saved.emit(None)
                    names = {pid: exe for pid, (_parent, exe) in table.items()}
                    loud = self.active and triggers([(s.pid, s.peak, s.system) for s in sessions],
                                                    self.mode, own, names)
                    factor = duck.step(now, loud, self.level)
                    for s in mine:
                        key = s.pid
                        if factor < 1.0 and key not in originals:
                            originals[key] = s.get_volume()
                            self.saved.emit(originals[key])
                        if key in originals:
                            s.set_volume(originals[key] * factor)
                    if factor >= 1.0 and originals:
                        originals.clear()
                        self.saved.emit(None)
                    if (factor < 1.0) != was_ducked:
                        was_ducked = factor < 1.0
                        self.ducked.emit(was_ducked)
                finally:
                    for s in sessions:
                        s.close()
                self._stop.wait(0.1)
        except OSError:
            pass
        finally:
            self._put_back(mixer, originals)
            mixer.close()

    def _put_back(self, mixer, originals):
        if not originals:
            return
        try:
            for s in mixer.scan(volumes_for=set(originals)):
                if s.pid in originals:
                    s.set_volume(originals[s.pid])
                s.close()
        except OSError:
            pass
        self.saved.emit(None)
