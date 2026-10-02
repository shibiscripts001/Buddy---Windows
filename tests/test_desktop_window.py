"""The desktop layout's desk (core/desktop_window.py): its tool windows are
native, the desk isn't, so Windows places them against the main window. When
the link bar goes in above the desk, the desk only moves - and the windows
have to move with it, not stay over the bar."""

import os
import unittest

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
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


if __name__ == "__main__":
    unittest.main()
