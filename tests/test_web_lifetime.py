"""Web views are destroyed on the GUI thread, never by Python's cycle
collector in some other thread.

A closed dialog whose web view sat in a reference cycle (the page held its
bridge, the bridge held the page) waited for the cycle collector, which
runs in whatever thread allocates at the time - Ask Buddy's agent, say.
Destroying a QtWebEngine view there crashed Buddy (an access violation in
Qt6WebEngineCore.dll). Builds a real ShellWindow offscreen with stand-in
pages and in-memory settings, like test_dual_view."""

import gc
import os
import unittest
from unittest import mock

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import shiboken6
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    import core.shell_window as shell_window
    from core import gui_gc, message_dialog
    from core.settings_dialog import SettingsDialog
    from pages.base import ToolPage
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class _Settings(dict):
    def save(self):
        pass


def _page(tool_id):
    class Page(ToolPage):
        def build_ui(self):
            QVBoxLayout(self).addWidget(QWidget())

    Page.tool_id, Page.display_name, Page.category = tool_id, tool_id.title(), "Tools"
    return Page


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class WebLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.settings = _Settings()
        for p in [mock.patch.object(shell_window, "SharedSettings", lambda: self.settings),
                  mock.patch.object(shell_window, "resolve_connect", side_effect=RuntimeError("no Resolve")),
                  mock.patch.object(shell_window.AnnouncementChecker, "start", lambda self: None),
                  mock.patch.object(shell_window.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False)]:
            p.start()
            self.addCleanup(p.stop)
        self.win = shell_window.ShellWindow(self.app, [("Tools", _page("alpha"))])
        self.addCleanup(self.win.deleteLater)
        self.win.show()
        self.app.processEvents()
        self.addCleanup(self._restore_gc)
        gc.collect()

    def _restore_gc(self):
        gc.set_debug(0)
        gc.garbage.clear()
        gc.enable()

    @staticmethod
    def _close_dialogs_soon(kind):
        QTimer.singleShot(1500, lambda: [w.close() for w in QApplication.topLevelWidgets() if isinstance(w, kind)])

    def _live_qt_garbage(self):
        """Web-view objects the cycle collector would destroy that still exist
        in Qt. (Other tests' background jobs can finish, and turn into garbage,
        while this one runs - they aren't what this checks.)"""
        gc.set_debug(gc.DEBUG_SAVEALL)
        gc.collect()
        web = ("PySide6.QtWebEngine", "PySide6.QtWebChannel", "core.web_page", "core.message_dialog")
        return [type(o).__name__ for o in gc.garbage
                if isinstance(o, shiboken6.Shiboken.Object) and shiboken6.isValid(o)
                and (type(o).__module__ or "").startswith(web)]

    def test_a_closed_message_leaves_nothing_for_the_cycle_collector(self):
        gc.disable()
        # Whatever earlier tests left for the collector isn't this test's.
        gc.set_debug(gc.DEBUG_SAVEALL)
        gc.collect()
        gc.garbage.clear()
        gc.set_debug(0)
        self._close_dialogs_soon(message_dialog.MessageDialog)
        message_dialog.alert(self.win, "Test", "Closed a moment from now")
        _wait(300)
        self.assertEqual(self._live_qt_garbage(), [])

    def test_settings_is_freed_once_closed(self):
        QTimer.singleShot(1500, lambda: [d.reject() for d in self.win.findChildren(SettingsDialog)])
        self.win.open_settings()
        _wait(300)   # deleteLater runs from the event loop
        self.assertEqual(self.win.findChildren(SettingsDialog), [])

    def test_collection_happens_on_the_gui_thread_only(self):
        collector = gui_gc.install(self.app)
        self.addCleanup(collector.stop)
        self.assertFalse(gc.isenabled())   # no thread collects by itself
        self.assertIsInstance(collector.collect(), int)


if __name__ == "__main__":
    unittest.main()
