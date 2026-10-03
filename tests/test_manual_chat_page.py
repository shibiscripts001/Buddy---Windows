"""Ask Buddy's conversation navigation preserves each chat's tool shortcut."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.manual_chat import chat_store
from pages.manual_chat.conversation import YOU

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    from pages.manual_chat.page import ManualChatPage
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


class Host:
    connected = False
    shared_settings = {"theme": "Resolve"}

    def tool_settings(self, _tool, defaults):
        return defaults

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class NavigationTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        patch = mock.patch.object(ManualChatPage, "ask_folder", self.folder)
        patch.start()
        self.addCleanup(patch.stop)
        self.page = ManualChatPage(Host())
        self.addCleanup(delete, self.page)
        self.events = []
        self.page.emit = lambda name, value=None: self.events.append((name, value))

    def test_buddy_opens_on_a_fresh_chat_with_the_saved_ones_in_the_list(self):
        self.page._append(YOU, "Relink my clips")
        self.page.on_new_chat(None)
        self.page._append(YOU, "Make subtitles")
        self.page.on_select_chat({"index": 0})       # left on the first one
        chat_store.save(self.folder, self.page.chats)
        again = ManualChatPage(Host())                # Buddy launched again
        self.addCleanup(delete, again)
        events = []
        again.emit = lambda name, value=None: events.append((name, value))
        again.web_ready()
        self.assertTrue(again.chats.is_empty())
        self.assertEqual(again.chats.index, len(again.chats.chats) - 1)
        self.assertEqual([c.title for c in again.chats.chats], ["Relink my clips", "Make subtitles", "New chat"])

    def offer(self):
        return [value for name, value in self.events if name == "offer"][-1]

    def test_new_previous_next_picker_and_reload_restore_the_right_offer(self):
        first = {"tool_id": "media_relink", "label": "Open Media Relink", "reason": "Missing files"}
        second = {"tool_id": "transcribe", "label": "Open Transcribe", "reason": "Subtitles"}
        self.page._append(YOU, "Relink my clips")
        self.page._set_offer(first)
        self.page.on_new_chat(None)
        self.assertIsNone(self.offer())
        self.page._append(YOU, "Make subtitles")
        self.page._set_offer(second)
        self.page._proposal = object()
        self.page.on_prev_chat(None)
        self.assertEqual(self.offer(), first)
        self.assertIsNone(self.page._proposal)
        self.page.on_next_chat(None)
        self.assertEqual(self.offer(), second)
        self.page.on_select_chat({"index": 0})
        self.assertEqual(self.offer(), first)
        self.page.chats, warnings = chat_store.load(self.folder)
        self.assertEqual(warnings, [])
        self.page.web_ready()
        self.assertEqual(self.offer(), first)
        self.page.on_delete_chat({"index": 0})
        self.assertEqual(self.offer(), second)
        self.page.on_delete_all_chats(None)
        self.assertIsNone(self.offer())


if __name__ == "__main__":
    unittest.main()
