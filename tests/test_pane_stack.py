"""The shell's pane stack (core/shell_window.py _PaneStack): a web page is
brought to the front only once Chromium has a picture of it at the pane's
size - shown underneath the page in front until then - so a tool never
shows small and jumps to fill the pane, or comes up blank. A page never
drawn comes once Chromium has laid it out at the pane's size. A page
without a web view switches at once."""

import os
import time
import unittest
from unittest import mock

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

# The waits, shortened for the tests.
WAITS = {"FIRST_DRAW_MS": 300, "SETTLE_MS": 40, "LAYOUT_SETTLE_MS": 40, "MAX_WAIT_MS": 600, "POLL_MS": 10}


def web_page():
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(QWebEngineView())
    return page


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PaneStackTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from core import shell_window
        self.sw = shell_window
        for name, value in WAITS.items():
            patch = mock.patch.object(shell_window, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # Whether Chromium "has a picture" of a view - the tests decide.
        self.pictures = set()
        patch = mock.patch.object(shell_window, "_has_picture", lambda view: view in self.pictures)
        patch.start()
        self.addCleanup(patch.stop)
        # When Chromium laid a view out at its size - the tests decide.
        self.layouts = {}
        patch = mock.patch.object(shell_window, "_laid_out_since",
                                  lambda view, started: self.layouts[view] if self.layouts.get(view, -1) >= started else None)
        patch.start()
        self.addCleanup(patch.stop)
        self.stack = shell_window._PaneStack()
        self.addCleanup(self.stack.deleteLater)
        self.a, self.b, self.c, self.plain = web_page(), web_page(), web_page(), QWidget()
        for page in (self.a, self.b, self.c, self.plain):
            self.stack.addWidget(page)
        self.stack.resize(600, 400)
        self.stack.show()
        self.wait()

    def view(self, page):
        return page.findChild(QWebEngineView)

    def wait(self, ms=0):
        end = time.monotonic() + ms / 1000
        while True:
            self.app.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.003)

    def test_a_page_with_its_picture_comes_at_once(self):
        self.stack.show_page(self.b)
        self.pictures.add(self.view(self.b))
        self.wait(WAITS["FIRST_DRAW_MS"] + 40)
        self.assertIs(self.stack.currentWidget(), self.b)
        self.stack.show_page(self.a)                               # drawn at this size, picture kept
        self.pictures.add(self.view(self.a))
        self.wait(30)
        self.assertIs(self.stack.currentWidget(), self.a)

    def test_the_page_in_front_stays_until_the_new_one_has_its_picture(self):
        self.stack.show_page(self.b)
        self.wait(WAITS["FIRST_DRAW_MS"] + 40)                     # long enough - but no picture yet
        self.assertIs(self.stack.currentWidget(), self.a)
        self.assertIs(self.stack.front(), self.b)
        self.assertTrue(self.b.isVisible())                        # being drawn, underneath
        self.assertEqual(self.b.size(), self.stack.contentsRect().size())
        self.pictures.add(self.view(self.b))
        self.wait(30)
        self.assertIs(self.stack.currentWidget(), self.b)
        self.assertFalse(self.a.isVisible())

    def test_a_never_drawn_page_waits_its_first_drawing_even_with_a_picture(self):
        # Chromium's first picture is at 640 x 480: not trusted yet.
        self.pictures.add(self.view(self.b))
        self.stack.show_page(self.b)
        self.wait(WAITS["SETTLE_MS"])
        self.assertIs(self.stack.currentWidget(), self.a)
        self.wait(WAITS["FIRST_DRAW_MS"])
        self.assertIs(self.stack.currentWidget(), self.b)

    def test_no_picture_at_all_still_comes_in_the_end(self):
        self.stack.show_page(self.b)
        self.wait(WAITS["MAX_WAIT_MS"] + 60)
        self.assertIs(self.stack.currentWidget(), self.b)

    def test_after_the_window_changes_size_the_old_picture_isnt_trusted_at_first(self):
        self.pictures.update({self.view(self.a), self.view(self.b)})
        self.stack.show_page(self.b)
        self.wait(WAITS["FIRST_DRAW_MS"] + 40)
        self.stack.show_page(self.a)
        self.wait(30)
        self.stack.resize(1200, 800)                               # Buddy maximized while A is in front
        self.wait()
        self.stack.show_page(self.b)
        self.wait(10)
        self.assertIs(self.stack.currentWidget(), self.a)
        self.wait(WAITS["SETTLE_MS"] + 40)
        self.assertIs(self.stack.currentWidget(), self.b)

    def test_a_quick_second_pick_wins(self):
        self.stack.show_page(self.b)
        self.stack.show_page(self.plain)
        self.assertIs(self.stack.currentWidget(), self.plain)
        self.assertFalse(self.b.isVisible())
        self.wait(WAITS["MAX_WAIT_MS"] + 60)
        self.assertIs(self.stack.currentWidget(), self.plain)

    def test_a_never_drawn_page_comes_once_laid_out_at_its_size(self):
        self.pictures.add(self.view(self.b))           # Chromium's 640 x 480 picture: not trusted
        self.stack.show_page(self.b)
        self.wait(30)
        self.assertIs(self.stack.currentWidget(), self.a)
        self.layouts[self.view(self.b)] = time.monotonic()
        self.wait(WAITS["LAYOUT_SETTLE_MS"] / 2)
        self.assertIs(self.stack.currentWidget(), self.a)   # its full-size picture is on the way
        self.wait(WAITS["LAYOUT_SETTLE_MS"] + 20)
        self.assertIs(self.stack.currentWidget(), self.b)    # well before FIRST_DRAW_MS

    def test_a_layout_from_before_the_pick_doesnt_count(self):
        self.pictures.add(self.view(self.b))
        self.layouts[self.view(self.b)] = time.monotonic() - 5
        self.stack.show_page(self.b)
        self.wait(WAITS["LAYOUT_SETTLE_MS"] + 40)
        self.assertIs(self.stack.currentWidget(), self.a)
        self.wait(WAITS["FIRST_DRAW_MS"])
        self.assertIs(self.stack.currentWidget(), self.b)

    def test_laid_out_but_no_picture_yet_still_waits(self):
        self.stack.show_page(self.b)
        self.layouts[self.view(self.b)] = time.monotonic()
        self.wait(WAITS["LAYOUT_SETTLE_MS"] + 40)
        self.assertIs(self.stack.currentWidget(), self.a)
        self.pictures.add(self.view(self.b))
        self.wait(30)
        self.assertIs(self.stack.currentWidget(), self.b)

    def test_a_page_without_a_web_view_never_waits(self):
        self.stack.show_page(self.plain)
        self.assertIs(self.stack.currentWidget(), self.plain)


if __name__ == "__main__":
    unittest.main()
