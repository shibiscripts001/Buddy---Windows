#!/usr/bin/env python3
"""The Web tab's bar (web/): Buddy's own view over the pages. What it
sends goes to the browser (page.py WebBrowserPage on_<action>), and the
browser's emit() comes here."""

import os

from PySide6.QtWidgets import QWidget

from core.web_page import WebSurface


class BrowserBar(WebSurface, QWidget):
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def __init__(self, browser_page):
        QWidget.__init__(self, browser_page)
        self.host = browser_page.host
        self.browser = browser_page
        self._build_web()

    def web_ready(self):
        self.browser.push()

    def __getattr__(self, name):
        # Its actions are the browser's: "go" -> WebBrowserPage.on_go.
        if name.startswith("on_"):
            return getattr(self.browser, name)
        raise AttributeError(name)
