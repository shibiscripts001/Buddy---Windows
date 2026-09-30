"""Render Nova in Buddy's Chromium engine, without Resolve or user settings.

Checks actual geometry and popup interactions: text-only CSS tests cannot
catch a transcript clipped by its player or panels overlapping at a breakpoint.
"""
import json
import os
import time
import unittest

import _paths

from core import gpu_adapter, web_flags

# Match the application's graphics setup, including its Windows ANGLE backend.
# Offscreen capture cannot present GPU textures; use software in headless runs.
# Set QT_QPA_PLATFORM=windows to exercise the real Windows compositor instead.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ["QT_QPA_PLATFORM"] == "offscreen":
    os.environ.setdefault("BUDDY_WEB_SOFTWARE", "1")
gpu_adapter.apply(os.environ)
web_flags.apply(os.environ)

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl, Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from shiboken6 import delete
    from core.theme import get_theme_tokens
    from core.web_theme import web_theme
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class NovaLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = QWebEngineView()
        self.addCleanup(delete, self.view)
        self.view.resize(1000, 700)
        loop = QEventLoop()
        loaded = []
        self.view.loadFinished.connect(lambda ok: (loaded.append(ok), loop.quit()))
        self.view.setUrl(QUrl.fromLocalFile(str(_paths.APP / "pages/dailies/web/index.html")))
        self.view.show()
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
        self.assertEqual(loaded, [True])
        self.js("""window.testMessages = [];
            console.log = (...args) => {
                if (args[0] === '[Buddy.send]') testMessages.push([args[1], args[2]]);
            };
            const clips = Array.from({length: 30}, (_, i) => ({id: 'c' + i,
                name: 'Scene 01 Shot 04 Take ' + i, bin: 'Master / Day 1',
                type: 'Video', seconds: 10, metadata: {}}));
            Buddy.receive('state', {project: 'Test film', project_id: 'p1', sources: [],
                current: 'c0', source: '@all', pending: 1, clip_count: 30, viewer: 'clip'});
            Buddy.receive('tape', {clips});
            Buddy.receive('current', {clip: clips[0]});
            return true;""")

    def js(self, body):
        loop = QEventLoop()
        result = []
        self.view.page().runJavaScript(
            "JSON.stringify((() => {" + body + "})())", 0,
            lambda value: (result.append(value), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        self.assertTrue(result, "JavaScript timed out")
        return json.loads(result[0])

    def settle(self):
        loop = QEventLoop()
        QTimer.singleShot(80, loop.quit)
        loop.exec()

    def until(self, body):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.js(body):
                return
            self.settle()
        self.fail("Timed out waiting for " + body)

    def theme(self, sub="Neon", **custom):
        theme = web_theme("Nova", sub, get_theme_tokens("Nova", sub, **custom))
        self.js(f"Buddy.receive('theme', {json.dumps(theme)}); return true;")
        self.settle()

    def test_player_and_notes_stay_reachable_at_every_breakpoint(self):
        for sub in ("Neon", "Nebula", "Ember", "Custom"):
            self.theme(sub)
            for width, height in ((1440, 900), (1200, 650), (1000, 700), (800, 600), (660, 600), (600, 700), (420, 600)):
                with self.subTest(sub=sub, width=width, height=height):
                    self.view.resize(width, height)
                    self.settle()
                    result = self.js("""
                        const rect = s => document.querySelector(s).getBoundingClientRect();
                        const panel = document.querySelector('.player-panel');
                        const p = rect('.player-panel'), n = rect('.notes-panel');
                        const w = document.querySelector('.workspace');
                        return {
                            clipped: panel.scrollHeight > panel.clientHeight + 1,
                            transcriptInside: rect('.transcript').bottom <= p.bottom,
                            overlap: n.left < p.right && n.right > p.left && n.top < p.bottom && n.bottom > p.top,
                            horizontalOverflow: w.scrollWidth > w.clientWidth + 1,
                            footerInside: rect('.review-bar').bottom <= innerHeight,
                            excessiveHeight: innerWidth > 1100 && innerHeight > 800 && w.scrollHeight > w.clientHeight + 1
                        };""")
                    self.assertEqual(result, dict(clipped=False, transcriptInside=True, overlap=False,
                                                 horizontalOverflow=False, footerInside=True, excessiveHeight=False))

    def test_still_dimensions_do_not_resize_the_player(self):
        self.theme()
        before = self.js("return document.querySelector('.player-panel').getBoundingClientRect().height;")
        self.js("""const canvas = document.createElement('canvas');
            canvas.width = 3840; canvas.height = 2160;
            Buddy.receive('frame', {id: 'c0', src: canvas.toDataURL()}); return true;""")
        self.settle()
        after = self.js("return document.querySelector('.player-panel').getBoundingClientRect().height;")
        self.assertEqual(before, after)

    def test_orbs_are_dim_stable_and_do_not_paint_the_rounded_corner(self):
        from PySide6.QtGui import QColor
        # Match WebToolPage's transparent surface: a browser with an opaque
        # canvas would conceal alpha/compositing faults at the Qt boundary.
        self.view.setStyleSheet("background: #070709;")
        self.view.page().setBackgroundColor(QColor(Qt.transparent))
        self.theme("Nebula")
        # Leave the background visible while retaining a real scrollable page.
        self.js("""document.querySelectorAll('main > *').forEach(e => e.style.visibility = 'hidden');
            const main = document.querySelector('main');
            main.style.overflowY = 'auto';
            const spacer = document.createElement('div');
            spacer.style.cssText = 'height:2000px;flex:none'; main.append(spacer);
            return true;""")
        self.settle()

        def samples():
            image = self.view.grab().toImage()
            scale = image.devicePixelRatio()
            return [image.pixelColor(round(x * scale), round(y * scale)).getRgb()[:3]
                    for x, y in ((0, 0), (80, 8), (420, 385), (880, 680))]

        # Some machines (GitHub's release runners among them) can't read a
        # web view's pixels back at all: grab() gives a flat fill. Check with
        # a solid red block first, and skip rather than fail on one of those.
        self.js("""const probe = document.createElement('div'); probe.id = 'pixel-probe';
            probe.style.cssText = 'position:fixed;inset:0;background:#ff0000;z-index:99999';
            document.body.append(probe); return true;""")
        self.settle()
        readable = all(r > 200 and g < 60 and b < 60 for r, g, b in samples())
        self.js("document.getElementById('pixel-probe').remove(); return true;")
        self.settle()
        if not readable:
            self.skipTest("this machine can't read a web view's pixels back")

        before = samples()

        def settled(expected):
            """The samples once they match - a slow machine (a release runner)
            repaints well after the 80ms settle() gives it."""
            deadline = time.monotonic() + 5
            got = samples()
            while got != expected and time.monotonic() < deadline:
                self.settle()
                got = samples()
            return got
        self.assertTrue(max(before[0]) < 25, before)
        # Nebula's purple should be a faint glow, not the full-strength accent.
        self.assertTrue(all(max(rgb) < 80 for rgb in before), before)
        self.assertGreater(before[1][2], before[1][0] + 5, before)
        self.js("document.querySelector('main').scrollTop = 600; return true;")
        self.settle()
        self.assertEqual(settled(before), before)
        other = web_theme("Resolve", None, get_theme_tokens("Resolve"))
        self.js(f"Buddy.receive('theme', {json.dumps(other)}); return true;")
        self.theme("Nebula")
        self.assertEqual(settled(before), before)
        self.view.resize(1200, 800)
        self.settle()
        self.assertTrue(all(max(rgb) < 80 for rgb in samples()))
        self.view.resize(1000, 700)
        self.settle()
        self.assertEqual(settled(before), before)

    def test_native_stage_stays_inside_workspace_and_hides_under_dialog(self):
        self.theme()
        self.js("document.querySelector('.workspace').scrollTop = 100; return true;")
        self.settle()
        self.assertTrue(self.js("""const box = [...testMessages].reverse().find(m => m[0] === 'stage_geometry')[1];
            const w = document.querySelector('.workspace').getBoundingClientRect();
            return box.y >= w.top - 1 && box.y + box.height <= w.bottom + 1;"""))
        self.js("document.getElementById('new-source').click(); return true;")
        self.until("""const box = [...testMessages].reverse().find(m => m[0] === 'stage_geometry')[1];
            return !!document.querySelector('.modal-backdrop') && box.width === 0 && box.height === 0;""")
        self.js("""const select = document.querySelector('.modal select');
            select.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0})); return true;""")
        self.until("""const pop = document.querySelector('.dropdown-pop');
            if (!pop) return false;
            const r = pop.getBoundingClientRect();
            return r.top >= 0 && r.bottom <= innerHeight && pop.contains(document.elementFromPoint(r.x + 10, r.y + 10));""")

    def test_the_strip_is_big_names_keep_their_ends_and_zoom_has_buttons(self):
        self.theme()
        self.js("""const clips = Array.from({length: 30}, (_, i) => ({id: 'c' + i,
                name: 'ZEN_FX3_A_20260128_82' + String(i).padStart(2, '0') + '.MP4', bin: 'Master', type: 'Video',
                seconds: 12, metadata: {}}));
            Buddy.receive('tape', {clips});
            Buddy.receive('state', {project: 'Test film', project_id: 'p1', sources: [], current: 'c3',
                source: '@all', pending: 0, clip_count: 30, viewer: 'source'});
            Buddy.receive('current', {clip: clips[3]});
            return true;""")
        self.settle()
        result = self.js("""const strip = document.getElementById('strip');
            const names = [...document.querySelectorAll('.strip .block-name')].map(n => n.textContent).filter(Boolean);
            return {height: strip.getBoundingClientRect().height, names,
                    info: document.getElementById('strip-info').textContent};""")
        self.assertGreaterEqual(result["height"], 96)
        # Camera names differ at the end: shortened in the middle, never "ZEN_FX3_A_2026...".
        self.assertTrue(result["names"])
        self.assertTrue(all(n.endswith(tuple(f"82{i:02d}" for i in range(30))) or "." in n for n in result["names"]),
                        result["names"])
        # Where a press would land shows before pressing.
        self.assertTrue(self.js("""const strip = document.getElementById('strip'), r = strip.getBoundingClientRect();
            strip.dispatchEvent(new PointerEvent('pointermove', {clientX: r.left + r.width / 2, clientY: r.top + 50, bubbles: true}));
            const line = document.getElementById('hover-line');
            return !line.hidden && /[0-9][0-9]:[0-9][0-9] · ZEN_/.test(document.getElementById('hover-time').textContent);"""))
        span = "const t = [...document.querySelectorAll('.strip .tick:not(.minor)')].map(t => t.textContent); return t;"
        before = self.js(span)
        self.js("document.getElementById('zoom-in').click(); return true;")
        self.assertNotEqual(self.js(span), before)
        self.js("document.getElementById('zoom-fit').click(); return true;")
        self.assertEqual(self.js("return document.querySelectorAll('.strip .block').length;"), 30)


if __name__ == "__main__":
    unittest.main()
