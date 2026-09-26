"""The message box: Enter sends, Shift+Enter starts a new line, Escape
cancels a reply or an edit, and typing "@" offers names to mention.

The name list comes from the page (`people`: a function returning the
people who make sense here - buddies and whoever's talking in this room)
and picking one writes "@Name#tag" (mentions.py), which the server turns
into a notification for exactly that person.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPlainTextEdit

from . import mentions, render

MAX_SUGGESTIONS = 8


class Composer(QPlainTextEdit):
    send_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, people=lambda: [], parent=None):
        super().__init__(parent)
        self.people = people
        self._popup = QListWidget(self)
        self._popup.setWindowFlags(Qt.ToolTip)   # shown beside the text, never taking the focus
        self._popup.setFocusPolicy(Qt.NoFocus)
        self._popup.itemClicked.connect(self._choose)
        self._partial = None
        self._typing = False
        # Names are offered as you type - not when text is put in for you
        # (editing a message) - and go when the cursor moves elsewhere.
        self.cursorPositionChanged.connect(lambda: None if self._typing else self._popup.hide())

    # ----------------------------------------------------------- keys

    def keyPressEvent(self, event):
        key = event.key()
        if self._popup.isVisible():
            if key in (Qt.Key_Down, Qt.Key_Up):
                row = self._popup.currentRow() + (1 if key == Qt.Key_Down else -1)
                self._popup.setCurrentRow(max(0, min(row, self._popup.count() - 1)))
                return
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                self._choose(self._popup.currentItem())
                return
            if key == Qt.Key_Escape:
                self._popup.hide()
                return
        if key in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.send_requested.emit()
            return
        if key == Qt.Key_Escape:
            self.cancel_requested.emit()
            return
        self._typing = True
        try:
            super().keyPressEvent(event)
            self._update_popup()
        finally:
            self._typing = False

    def focusOutEvent(self, event):
        self._popup.hide()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self._popup.hide()
        super().hideEvent(event)

    # ---------------------------------------------------------- names

    def _matches(self, typed: str) -> list[dict]:
        typed = typed.casefold()
        seen, out = set(), []
        for person in self.people():
            name = person.get("name") or ""
            if not name or person["id"] in seen or not name.casefold().startswith(typed):
                continue
            seen.add(person["id"])
            out.append(person)
        return sorted(out, key=lambda p: p["name"].casefold())[:MAX_SUGGESTIONS]

    def _update_popup(self):
        cursor = self.textCursor()
        self._partial = mentions.partial_before(self.toPlainText(), cursor.position())
        found = self._matches(self._partial[1]) if self._partial else []
        if not found:
            self._popup.hide()
            return
        self._popup.clear()
        for person in found:
            item = QListWidgetItem(render.display_name(person))
            item.setData(Qt.UserRole, person)
            self._popup.addItem(item)
        self._popup.setCurrentRow(0)
        rect = self.cursorRect()
        self._popup.setFixedWidth(max(220, self._popup.sizeHintForColumn(0) + 24))
        self._popup.setFixedHeight(min(len(found), MAX_SUGGESTIONS) * (self._popup.sizeHintForRow(0) + 2) + 6)
        self._popup.move(self.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 4)))
        self._popup.show()

    def _choose(self, item):
        if item is None or self._partial is None:
            return
        start, typed = self._partial
        cursor = self.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + 1 + len(typed), QTextCursor.KeepAnchor)
        cursor.insertText(mentions.token(item.data(Qt.UserRole)) + " ")
        self.setTextCursor(cursor)
        self._popup.hide()
