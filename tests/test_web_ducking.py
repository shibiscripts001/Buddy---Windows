"""The Web tab's audio ducking (app/pages/web/ducking.py): what counts as
another app's sound, the fade down and back up, and the thread turning a
(stand-in) mixer's sessions down and putting them back - never the real
Windows mixer. Plus the sidebar's sound mark (ToolPage.rail_badge)."""

import os
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.web import ducking as d

RESOLVE, CHROME, OURS = 100, 200, 300
NAMES = {RESOLVE: "Resolve.exe", CHROME: "chrome.exe", OURS: "QtWebEngineProcess.exe"}


class TriggerTests(unittest.TestCase):
    def test_what_ducks_the_web_tab(self):
        resolve = [(RESOLVE, 0.4, False)]
        chrome = [(CHROME, 0.4, False)]
        own = {OURS}
        self.assertTrue(d.triggers(resolve, "resolve", own, NAMES))
        self.assertFalse(d.triggers(chrome, "resolve", own, NAMES))         # only Resolve, by default
        self.assertTrue(d.triggers(chrome, "any", own, NAMES))
        self.assertFalse(d.triggers([(OURS, 0.9, False)], "any", own, NAMES))   # its own sound never
        self.assertFalse(d.triggers([(0, 0.9, True)], "any", own, NAMES))       # nor Windows' dings
        self.assertFalse(d.triggers([(RESOLVE, 0.001, False)], "resolve", own, NAMES))   # silence
        self.assertFalse(d.triggers(resolve, "off", own, NAMES))


class LevelTests(unittest.TestCase):
    def test_zero_to_a_hundred_in_fives(self):
        self.assertEqual([d.clean_level(v) for v in (0, 5, 30, 100, "45", 47, 48.9)], [0, 5, 30, 100, 45, 45, 50])
        for bad in (-5, 105, None, "loud"):
            self.assertIsNone(d.clean_level(bad))

    def test_at_zero_it_goes_silent_and_at_a_hundred_it_stays(self):
        duck = d.Duck()
        duck.step(0.0, True, 0.0)
        self.assertEqual(duck.step(d.FADE_DOWN, True, 0.0), 0.0)
        self.assertEqual(d.Duck().step(5.0, True, 1.0), 1.0)


class DuckTests(unittest.TestCase):
    def test_down_quickly_and_back_slowly_after_a_pause(self):
        duck = d.Duck()
        self.assertEqual(duck.step(0.0, True, 0.3), 1.0)
        self.assertAlmostEqual(duck.step(d.FADE_DOWN / 2, True, 0.3), 0.65)
        self.assertAlmostEqual(duck.step(d.FADE_DOWN, True, 0.3), 0.3)
        last = d.FADE_DOWN                                                   # the last sound
        self.assertAlmostEqual(duck.step(last + d.HOLD - 0.1, False, 0.3), 0.3)   # held through a pause
        rising = duck.step(last + d.HOLD + 0.1, False, 0.3)
        self.assertTrue(0.3 < rising < 0.5, rising)                          # coming back up, slowly
        self.assertEqual(duck.step(last + d.HOLD + 0.1 + d.FADE_UP, False, 0.3), 1.0)
        start = last
        self.assertEqual(duck.step(start + 10, False, 0.3), 1.0)


class _Session:
    def __init__(self, mixer, pid, peak, system=False):
        self.mixer, self.pid, self.peak, self.system = mixer, pid, peak, system

    def get_volume(self):
        return self.mixer.volume[self.pid]

    def set_volume(self, level):
        self.mixer.volume[self.pid] = level

    def close(self):
        pass


class _Mixer:
    """Stands in for core/audio_sessions.AudioSessions."""
    peaks = {}
    volume = {}

    def scan(self, volumes_for=()):
        return [_Session(self, pid, peak) for pid, peak in self.peaks.items()]

    def close(self):
        pass


class DuckerTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        _Mixer.peaks = {RESOLVE: 0.0, OURS: 0.2}
        _Mixer.volume = {RESOLVE: 1.0, OURS: 0.8}
        table = {RESOLVE: (1, "Resolve.exe"), OURS: (os.getpid(), "QtWebEngineProcess.exe")}
        for p in (mock.patch.object(d.audio_sessions, "available", True),
                  mock.patch.object(d.audio_sessions, "AudioSessions", _Mixer),
                  mock.patch.object(d.audio_sessions, "processes", return_value=table),
                  mock.patch.object(d, "FADE_DOWN", 0.05), mock.patch.object(d, "FADE_UP", 0.05),
                  mock.patch.object(d, "HOLD", 0.1)):
            p.start()
            self.addCleanup(p.stop)

    def until(self, check):
        for _ in range(100):
            self.app.processEvents()                 # the thread's signals arrive through the event loop
            if check():
                return
            time.sleep(0.05)
        self.fail(f"never: volumes {_Mixer.volume}")

    def test_it_lowers_the_web_tab_while_resolve_plays_and_puts_it_back(self):
        saved = []
        ducker = d.Ducker()
        ducker.saved.connect(saved.append)
        ducker.mode, ducker.level, ducker.active = "resolve", 0.25, True
        ducker.start()
        self.addCleanup(ducker.stop)
        _Mixer.peaks[RESOLVE] = 0.5                                          # Resolve plays
        self.until(lambda: abs(_Mixer.volume[OURS] - 0.2) < 1e-6)             # 0.8 x 0.25
        self.assertEqual(_Mixer.volume[RESOLVE], 1.0)                        # Resolve's own left alone
        self.until(lambda: 0.8 in saved)       # posted before the volume moved, delivered by the event loop
        _Mixer.peaks[RESOLVE] = 0.0                                          # and stops
        self.until(lambda: abs(_Mixer.volume[OURS] - 0.8) < 1e-6)
        self.until(lambda: saved[-1] is None)

    def test_stopping_while_down_puts_the_volume_back(self):
        ducker = d.Ducker()
        ducker.mode, ducker.level, ducker.active = "resolve", 0.25, True
        _Mixer.peaks[RESOLVE] = 0.5
        ducker.start()
        self.until(lambda: _Mixer.volume[OURS] < 0.3)
        ducker.stop()
        self.assertAlmostEqual(_Mixer.volume[OURS], 0.8)

    def test_a_volume_left_down_last_time_is_put_back(self):
        _Mixer.volume[OURS] = 0.2
        ducker = d.Ducker(restore=0.8)
        ducker.mode, ducker.active = "resolve", True
        ducker.start()
        self.addCleanup(ducker.stop)
        self.until(lambda: _Mixer.volume[OURS] == 0.8)

    def test_nothing_happens_while_nothing_plays_in_the_web_tab(self):
        ducker = d.Ducker()
        ducker.mode, ducker.active = "resolve", False
        _Mixer.peaks[RESOLVE] = 0.5
        ducker.start()
        time.sleep(0.4)
        ducker.stop()
        self.assertEqual(_Mixer.volume[OURS], 0.8)


class BadgeTests(unittest.TestCase):
    def test_tools_have_no_mark_unless_they_say(self):
        from pages.base import ToolPage
        self.assertIsNone(ToolPage.rail_badge(object()))
        self.assertIsNone(ToolPage.rail_media(object()))                  # nor a button to press


if __name__ == "__main__":
    unittest.main()
