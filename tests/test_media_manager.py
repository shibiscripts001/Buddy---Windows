"""Media Manager: Batch Clip Renamer and Media Relink as its two tabs - the
rail entry, saved sidebars, opening a tab by its tool's id, the tab row."""

import os
import re
import types
import unittest

import _paths
from core import nav_layout
from core.tools_kb import get_tool

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    from pages.base import ToolPage
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


class Page:
    def __init__(self, tool_id):
        self.tool_id = tool_id


class NavTests(unittest.TestCase):
    def test_old_entries_become_one_tool_where_media_relink_was(self):
        registry = [("Setup", Page("setup")), ("Media & Assets", Page("media_manager"))]
        saved = [{"type": "divider", "label": "Media & Assets"},
                 {"type": "tool", "id": "media_relink", "visible": False},
                 {"type": "tool", "id": "setup", "visible": True},
                 {"type": "divider", "label": "Editing Tools"},
                 {"type": "tool", "id": "batch_clip_renamer", "visible": True}]
        self.assertEqual(nav_layout.reconcile(saved, registry), [
            {"type": "divider", "label": "Media & Assets"},
            {"type": "tool", "id": "media_manager", "visible": True},
            {"type": "tool", "id": "setup", "visible": True},
            {"type": "divider", "label": "Editing Tools"},
        ])

    def test_the_registry_has_media_manager_and_not_its_tabs(self):
        # Read, not imported: importing registry.py imports every page, numpy and
        # all - which the release workflow's test run doesn't install.
        source = (_paths.APP / "registry.py").read_text(encoding="utf-8")
        self.assertIn("MediaManagerPage", source)
        self.assertNotIn("MediaRelinkPage", source)
        self.assertNotIn("BatchClipRenamerPage", source)
        # Ask Buddy can still offer either tool by its own id.
        from pages.media_manager.page import MediaManagerPage
        registry = [("Media & Assets", MediaManagerPage)]
        for tool_id in ("media_relink", "batch_clip_renamer"):
            self.assertTrue(get_tool(tool_id, registry).is_available, tool_id)


class ViewTests(unittest.TestCase):
    def test_both_views_draw_the_same_tab_row_with_their_own_tab_chosen(self):
        for folder, chosen in (("batch_clip_renamer", "renamer"), ("media_relink", "relink")):
            html = (_paths.APP / "pages" / folder / "web" / "index.html").read_text(encoding="utf-8")
            with self.subTest(folder):
                self.assertIn('<h1 class="page-title">Media Manager</h1>', html)
                self.assertIn('../../media_manager/web/tabs.js', html)
                tabs = re.findall(r'data-tab="(\w+)" aria-selected="(\w+)"', html)
                self.assertEqual(tabs, [("renamer", str(chosen == "renamer").lower()),
                                        ("relink", str(chosen == "relink").lower())])


if HAVE_QT:
    class FakeTool(ToolPage):
        shown = 0

        def build_ui(self):
            pass

        def on_shown(self):
            self.shown += 1

    class FakeRenamer(FakeTool):
        tool_id = "batch_clip_renamer"

    class FakeRelink(FakeTool):
        tool_id = "media_relink"


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from pages.media_manager.page import MediaManagerPage

        class Manager(MediaManagerPage):
            TABS = (("renamer", FakeRenamer), ("relink", FakeRelink))
        self.page = Manager(types.SimpleNamespace())
        self.addCleanup(delete, self.page)

    def test_tabs_switch_and_the_one_shown_hears_it(self):
        renamer, relink = self.page.renamer, self.page.relink
        self.assertIs(self.page.tabs.currentWidget(), renamer)
        self.assertIs(relink._media_manager, self.page)
        self.page.show()
        self.page.show_tab("relink")
        self.assertIs(self.page.tabs.currentWidget(), relink)
        self.assertEqual(relink.shown, 1)
        self.page.show_tool("batch_clip_renamer")
        self.assertIs(self.page.tabs.currentWidget(), renamer)
        self.page.show_tab("nonsense")
        self.assertIs(self.page.tabs.currentWidget(), renamer)

    def test_the_shell_opens_a_tab_by_its_tools_id(self):
        from core.shell_window import ShellWindow
        opened = []
        shell = types.SimpleNamespace(pages={"media_manager": self.page}, _layout="desktop",
                                      desktop=types.SimpleNamespace(open=opened.append))
        ShellWindow.switch_tool(shell, "media_relink")
        self.assertEqual((opened, shell._current_tool_id), (["media_manager"], "media_manager"))
        self.assertIs(self.page.tabs.currentWidget(), self.page.relink)
        ShellWindow.switch_tool(shell, "no_such_tool")
        self.assertEqual(opened, ["media_manager"])


if __name__ == "__main__":
    unittest.main()
