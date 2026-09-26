"""The shared colour picker opens where it can be seen.

Settings' Custom swatches (and a few Color Palette buttons) pass the
clicked button itself as `at`; the picker has to anchor to where that
button is drawn, or it lands below the window, out of sight. Runs the real Settings page offscreen
with a Custom theme in in-memory settings, like test_web_lifetime."""

import json
import os
import unittest
from unittest import mock

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    import core.shell_window as shell_window
    from core.settings_dialog import SettingsDialog
    from pages.base import ToolPage
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class _Settings(dict):
    def save(self):
        pass


class _Page(ToolPage):
    tool_id, display_name, category = "alpha", "Alpha", "Tools"

    def build_ui(self):
        QVBoxLayout(self).addWidget(QWidget())


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ColorPickerTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        settings = _Settings(theme="Desktop", subtheme="Custom")
        for p in [mock.patch.object(shell_window, "SharedSettings", lambda: settings),
                  mock.patch.object(shell_window, "resolve_connect", side_effect=RuntimeError("no Resolve")),
                  mock.patch.object(shell_window.AnnouncementChecker, "start", lambda self: None),
                  mock.patch.object(shell_window.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False)]:
            p.start()
            self.addCleanup(p.stop)
        self.win = shell_window.ShellWindow(self.app, [("Tools", _Page)])
        self.addCleanup(self.win.deleteLater)
        self.win.show()
        self.dialog = SettingsDialog(self.win, settings, lambda: None)
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.close)
        self.dialog.show()
        _wait(300)

    def _js(self, code):
        loop, out = QEventLoop(), {}
        self.dialog.view.page().runJavaScript(code, 0, lambda r: (out.update(r=r), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        return out.get("r")

    def test_a_swatch_opens_the_picker_inside_the_window(self):
        for _ in range(100):   # until the form has drawn
            if self._js("document.querySelectorAll('.set-color:not(:disabled)').length"):
                break
            _wait(100)
        else:
            self.fail("Settings never drew its colour swatches")
        self._js("document.querySelector('.set-color').click()")
        box = json.loads(self._js("""(() => {
            const p = document.querySelector('#picker'), r = p.getBoundingClientRect();
            const b = document.querySelector('.set-color').getBoundingClientRect();
            return JSON.stringify({hidden: p.hidden, top: r.top, bottom: r.bottom, left: r.left,
                                   right: r.right, h: innerHeight, w: innerWidth, under: b.bottom});
        })()"""))
        self.assertFalse(box["hidden"])
        self.assertGreaterEqual(box["top"], 0)
        self.assertLessEqual(box["bottom"], box["h"])
        self.assertGreaterEqual(box["left"], 0)
        self.assertLessEqual(box["right"], box["w"])
        self.assertGreater(box["top"], box["under"])   # anchored below the swatch it came from


if __name__ == "__main__":
    unittest.main()
