#!/usr/bin/env python3
"""
alert() and confirm(): a title, some text and one or two buttons, drawn
like the rest of Buddy (app/web/shell/message/) instead of a Qt message
box. Modal over the window you pass.

Protocol:
    to the view    message
    from the view  answer
"""

import os

from PySide6.QtWidgets import QDialog

from core.web_page import WEB_COMMON_DIR, WebDialog, _theme_host


class MessageDialog(WebDialog):
    web_dir = os.path.join(WEB_COMMON_DIR, "shell", "message")

    def __init__(self, host, parent, title, text, ok="OK", cancel=None, danger=False):
        self._message = {"title": title, "text": text, "ok": ok, "cancel": cancel, "danger": bool(danger)}
        self.answer = False
        super().__init__(host, parent, title, (440, 220))

    def web_ready(self):
        self.emit("message", self._message)

    def on_answer(self, payload):
        self.answer = bool((payload or {}).get("ok"))
        self.done(QDialog.Accepted if self.answer else QDialog.Rejected)


def alert(parent, title, text):
    """A message over `parent`."""
    MessageDialog(_theme_host(parent), parent, title, text).exec()


def confirm(parent, title, text, ok="OK", cancel="Cancel", danger=False):
    """True if they pressed `ok`."""
    dialog = MessageDialog(_theme_host(parent), parent, title, text, ok, cancel, danger)
    dialog.exec()
    return dialog.answer
