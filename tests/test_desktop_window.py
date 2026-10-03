"""The desktop layout's desk (core/desktop_window.py): its tool windows are
native, the desk isn't, so Windows places them against the main window. When
the link bar goes in above the desk, the desk only moves - and the windows
have to move with it, not stay over the bar."""

import os
import unittest

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    import shiboken6
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class DeskUnderTheLinkBar(unittest.TestCase):
    def setUp(self):
        from core.desktop_window import DesktopArea

        class Page(QWidget):
            display_name = "Tool"

            def on_shown(self):
                pass

        self.app = QApplication.instance() or QApplication([])
        self.top = QWidget()
        self.top.resize(1000, 700)
        self.root = QVBoxLayout(self.top)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)
        self.desk = DesktopArea(self.top, {"tool": Page()}, {"tool": "Tool"}, lambda page: None, lambda front: None)
        self.root.addWidget(self.desk, 1)
        self.bar = QLabel("links")
        self.bar.setFixedHeight(40)
        self.root.addWidget(self.bar)                       # along the bottom, as under the other themes
        self.top.show()
        self.desk.show()
        self.app.processEvents()
        self.desk.open("tool")
        self.app.processEvents()

    def tearDown(self):
        self.top.close()
        self.top.deleteLater()
        self.app.processEvents()

    def native_y(self):
        win = self.desk.windows["tool"]
        return win.windowHandle().position().y(), win.mapTo(self.top, QPoint(0, 0)).y()

    def test_the_windows_move_down_with_the_desk(self):
        actual, expected = self.native_y()
        self.assertEqual(actual, expected)
        # The desktop layout puts the bar along the top: the desk moves down,
        # its size the same.
        self.root.removeWidget(self.bar)
        self.root.insertWidget(0, self.bar)
        self.app.processEvents()
        self.assertEqual(self.desk.y(), 40)
        actual, expected = self.native_y()
        self.assertEqual(actual, expected)
        self.assertGreaterEqual(actual, 40)                 # below the bar, never over it


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class DraggingAWindow(unittest.TestCase):
    """A drag by the title moves a picture of the window (_DragGhost); the
    window steps off the desk and lands once, where the picture is."""

    def setUp(self):
        import time
        from unittest import mock
        from core import desktop_window
        from core.desktop_window import DesktopArea

        class Page(QWidget):
            display_name = "Tool"

            def on_shown(self):
                pass

        self.dw, self.time = desktop_window, time
        self.button_down = True
        patch = mock.patch.object(desktop_window, "_primary_button_down", lambda: self.button_down)
        patch.start()
        self.addCleanup(patch.stop)
        self.app = QApplication.instance() or QApplication([])
        self.top = QWidget()
        self.top.resize(1200, 800)
        self.moves = []
        self.desk = DesktopArea(self.top, {"tool": Page()}, {"tool": "Tool"}, lambda page: None, lambda front: None)
        self.desk.setGeometry(0, 0, 1200, 800)
        self.top.show()
        self.desk.show()
        self.wait()
        self.desk.open("tool")
        self.wait()
        self.win = self.desk.windows["tool"]
        self.start = self.win.geometry()

    def tearDown(self):
        self.top.close()
        self.top.deleteLater()
        self.wait()

    def wait(self, ms=0):
        end = self.time.monotonic() + ms / 1000
        while True:
            self.app.processEvents()
            if self.time.monotonic() >= end:
                return
            self.time.sleep(0.003)

    def mouse(self, kind, local):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        types = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease}
        buttons = Qt.NoButton if kind == "release" else Qt.LeftButton
        point = QPointF(local)
        event = QMouseEvent(types[kind], point, QPointF(self.win.mapToGlobal(local)), Qt.LeftButton, buttons, Qt.NoModifier)
        {"press": self.win.mousePressEvent, "move": self.win.mouseMoveEvent,
         "release": self.win.mouseReleaseEvent}[kind](event)

    def drag_to(self, dx, dy):
        """Press on the title, move by (dx, dy) - in desk terms, so the
        window stepping off the desk doesn't change where the pointer is."""
        title = QPoint(40, 10)
        self.mouse("press", title)
        origin = self.win.mapToGlobal(title)
        target = origin + QPoint(dx, dy)

        def move():
            self.mouse("move", self.win.mapFromGlobal(target))
        move()
        return move

    def test_a_wobble_is_a_click_not_a_drag(self):
        self.drag_to(1, 1)
        self.assertIsNone(self.win._ghost)
        self.mouse("release", QPoint(41, 11))
        self.assertEqual(self.win.geometry(), self.start)

    def test_the_picture_moves_and_the_window_lands_once(self):
        move = self.drag_to(120, 70)
        ghost = self.win._ghost
        self.assertIsNotNone(ghost)
        self.assertTrue(ghost.isVisible())
        self.wait(60)                                            # the window steps off the desk
        self.assertEqual(self.win.x(), self.dw.OFF_DESK)
        move()
        target = self.start.translated(120, 70)
        self.assertEqual(ghost.pos(), self.desk.mapToGlobal(target.topLeft()))
        self.mouse("release", QPoint(0, 0))
        self.assertEqual(self.win.geometry(), target)
        self.assertEqual(tuple(self.desk.state.windows["tool"]["geom"]), target.getRect())
        self.assertIsNone(self.win._ghost)
        self.wait(self.dw.LAND_MS + 60)
        self.assertFalse(shiboken6.isValid(ghost))              # closed and gone

    def test_a_release_that_never_comes_still_lands_it(self):
        self.drag_to(80, 40)
        self.wait(60)
        self.button_down = False                                 # let go somewhere Buddy didn't hear
        self.wait(self.dw.DRAG_CHECK_MS + 60)
        self.assertIsNone(self.win._ghost)
        self.assertEqual(self.win.geometry(), self.start.translated(80, 40))

    def test_a_resize_draws_the_frame_at_the_new_size_and_lands_once(self):
        f = self.win.frame_rect()
        corner = QPoint(f.right() - 2, f.bottom() - 2)
        self.mouse("press", corner)
        origin = self.win.mapToGlobal(corner)
        self.mouse("move", self.win.mapFromGlobal(origin + QPoint(150, 90)))
        ghost = self.win._ghost
        self.assertIsNotNone(ghost)
        self.wait(60)
        self.assertEqual(self.win.x(), self.dw.OFF_DESK)
        self.assertEqual(self.win.size(), self.start.size())           # the window itself waits
        self.mouse("move", self.win.mapFromGlobal(origin + QPoint(150, 90)))
        target = self.start.adjusted(0, 0, 150, 90)
        self.assertEqual(ghost.geometry(), QRect(self.desk.mapToGlobal(target.topLeft()), target.size()))
        self.mouse("release", QPoint(0, 0))
        self.assertEqual(self.win.geometry(), target)
        self.assertEqual(tuple(self.desk.state.windows["tool"]["geom"]), target.getRect())
        self.wait(self.dw.LAND_RESIZED_MS + 60)
        self.assertFalse(shiboken6.isValid(ghost))

    def test_closing_mid_drag_leaves_no_picture(self):
        self.drag_to(80, 40)
        ghost = self.win._ghost
        self.wait(60)
        self.desk.close("tool")
        self.wait(self.dw.LAND_MS + 60)
        self.assertIsNone(self.win._ghost)
        self.assertFalse(shiboken6.isValid(ghost))              # closed and gone


if __name__ == "__main__":
    unittest.main()
