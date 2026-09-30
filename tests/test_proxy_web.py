"""The Proxy tab's web view, driven for real: the shell builds the page,
Chromium loads its HTML/JS, and the test drives the page the way Buddy's
own tests drive every web tool - emit state in, actions out. Never a
real Resolve (the shell's connect is refused), never the real
~/.resolve_bin_generator settings."""

import os
import tempfile
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.project_setup import proxy

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QApplication
    import core.shell_window as shell_window
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class _Settings(dict):
    def save(self):
        pass


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ProxyWebTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        # Project Setup's settings must land in the scratch folder.
        from pages.project_setup import data_manager
        patch = mock.patch.object(data_manager, "SETTINGS_DIR", self._tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.settings = _Settings(theme="Resolve")
        for p in [mock.patch.object(shell_window, "SharedSettings", lambda: self.settings),
                  mock.patch.object(shell_window, "resolve_connect", side_effect=RuntimeError("no Resolve")),
                  mock.patch.object(shell_window.AnnouncementChecker, "start", lambda self: None),
                  mock.patch.object(shell_window.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False)]:
            p.start()
            self.addCleanup(p.stop)
        # Only the page under test, on its own.
        from pages.project_setup.page import ProjectSetupPage
        ProjectSetupPage.category = "Tools"

        class Page(ProjectSetupPage):
            pass
        Page.tool_id, Page.display_name, Page.category = "project_setup", "Project Setup", "Tools"
        self.win = shell_window.ShellWindow(self.app, [("Tools", Page)])
        self.addCleanup(self.win.deleteLater)
        self.win.show()
        self.page = self.win.pages["project_setup"]
        self.page._poll.stop()
        _wait(1200)

    def _js(self, code):
        out = {}
        loop = QEventLoop()
        self.page.view.page().runJavaScript(code, 0, lambda r: (out.update(r=r), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        return out.get("r")

    def _until(self, condition):
        for _ in range(100):
            if self._js(condition):
                return True
            _wait(50)
        self.fail(f"never true: {condition}")

    def test_the_proxy_tab_renders_and_switches(self):
        self._until("document.querySelector('[data-tab=proxy]') !== null")
        # Six tabs now, Proxy between Sync and Metadata.
        self.assertTrue(self._js("document.querySelectorAll('.tabs [data-tab]').length === 6"))
        self.assertTrue(self._js(
            "document.querySelector('[data-tab=proxy]').previousElementSibling.dataset.tab === 'sync'"))
        self.assertTrue(self._js(
            "document.querySelector('[data-tab=proxy]').nextElementSibling.dataset.tab === 'metadata'"))
        # Click it: the panel shows and Python follows.
        self._js("document.querySelector('[data-tab=proxy]').click()")
        self._until("!document.getElementById('panel-proxy').hidden")
        self.assertEqual(self.page.tab, "proxy")
        # The scope choices are all there.
        self.assertTrue(self._js("document.querySelectorAll('#proxy-scope [data-scope]').length === 4"))
        # Make proxies button disabled: no ffmpeg path set here and not connected.
        self.assertTrue(self._js("document.getElementById('proxy-go').disabled === true"))

    def test_options_reach_python_and_back(self):
        self._until("document.querySelector('[data-tab=proxy]') !== null")
        self._js("document.querySelector('[data-tab=proxy]').click()")
        self._until("!document.getElementById('panel-proxy').hidden")
        self._js("document.querySelector('#proxy-scope [data-scope=bin]').click()")
        self._until("document.querySelector('#proxy-scope [data-scope=bin]').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(self.page.proxy_options["scope"], "bin")
        # The recursive checkbox appears for the bin scope.
        self._until("!document.getElementById('proxy-recursive-box').hidden")
        self._js("document.getElementById('proxy-recursive').click()")
        self._until("document.getElementById('proxy-recursive').checked === true")
        self.assertTrue(self.page.data_mgr.settings["proxy_recursive"])
        # Resolution and format.
        self._js("document.querySelector('#proxy-resolution [data-resolution=quarter]').click()")
        self._until("document.querySelector('#proxy-resolution [data-resolution=quarter]').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(self.page.proxy_options["resolution"], "quarter")
        # Format: a dropdown of every format this platform offers, with its hint.
        self._until("document.getElementById('proxy-format').options.length === %d" % len(proxy.codec_choices()))
        self._js("const f = document.getElementById('proxy-format'); f.value = 'dnxhr_lb'; f.dispatchEvent(new Event('change'))")
        self._until("document.getElementById('proxy-format-hint').textContent.includes('Avid')")
        self.assertEqual(self.page.proxy_options["codec"], "dnxhr_lb")
        self.assertEqual(self.page.data_mgr.settings["proxy_codec"], "dnxhr_lb")

    def test_the_current_timeline_scope_is_always_offered(self):
        self._until("document.querySelector('[data-tab=proxy]') !== null")
        self._js("document.querySelector('[data-tab=proxy]').click()")
        # Any Resolve can do it: with nothing selected it takes the whole timeline.
        self._until("document.querySelector('#proxy-scope [data-scope=timeline]').textContent === 'Current timeline'")
        self.assertFalse(self._js("document.querySelector('#proxy-scope [data-scope=timeline]').hidden"))


if __name__ == "__main__":
    unittest.main()