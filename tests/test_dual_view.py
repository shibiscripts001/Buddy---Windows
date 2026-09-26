"""The shell's dual view (app/core/shell_window.py): a second tool beside the
one picked in the rail. Builds a real ShellWindow offscreen, but with
stand-in pages and in-memory settings - never the real ~/.buddy, never
Resolve, no tray icon, no announcement fetch. The header is a web view
(core/shell_web.py), driven here through its handlers - what its toggle and
dropdown send."""

import os
import unittest
from unittest import mock

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    import core.shell_window as shell_window
    from pages.base import ToolPage
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class _Settings(dict):
    def save(self):
        pass


def _page(tool_id, min_width=100):
    class Page(ToolPage):
        shown = 0

        def build_ui(self):
            layout = QVBoxLayout(self)
            filler = QWidget()
            filler.setMinimumWidth(min_width)
            layout.addWidget(filler)

        def on_shown(self):
            self.shown += 1

    Page.tool_id = tool_id
    Page.display_name = tool_id.title()
    Page.category = "Tools"
    return Page


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class DualViewTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.settings = _Settings()
        patches = [
            mock.patch.object(shell_window, "SharedSettings", lambda: self.settings),
            mock.patch.object(shell_window, "resolve_connect", side_effect=RuntimeError("no Resolve")),
            mock.patch.object(shell_window.AnnouncementChecker, "start", lambda self: None),
            mock.patch.object(shell_window.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        registry = [("Tools", _page("alpha")), ("Tools", _page("beta")),
                    ("Tools", _page("gamma", min_width=900))]
        self.win = shell_window.ShellWindow(self.app, registry)
        self.addCleanup(self.win.deleteLater)
        self.win.resize(1920, 1000)
        self.win.show()
        self.app.processEvents()

    def sides(self):
        return self.win._current_tool_id, self.win._side_tool_id

    def picker_ids(self):
        return self.win.side_choices()

    def toggle(self):
        """The header's dual-view button."""
        self.win.header.on_split({"on": not self.win._split_on})

    def pick_side(self, tool_id):
        """The header's right-hand tool dropdown."""
        self.win.header.on_side({"id": tool_id})

    def test_single_page_stops_growing_past_the_cap(self):
        self.assertEqual(self.win._panes_holder.width(), shell_window.PANE_MAX_WIDTH)
        self.win.apply_theme()   # a stylesheet swap must not undo the cap
        self.app.processEvents()
        self.assertEqual(self.win._panes_holder.width(), shell_window.PANE_MAX_WIDTH)

    def test_toggle_shows_a_second_tool_and_remembers_it(self):
        self.toggle()
        self.assertEqual(self.sides(), ("alpha", "beta"))
        self.assertTrue(self.win.side_pane.isVisible())
        self.assertEqual(self.picker_ids(), ["beta", "gamma"])   # never the left tool
        self.assertEqual(self.win.pages["beta"].shown, 1)
        self.assertTrue(self.settings["split_view"])
        self.assertEqual(self.settings["split_tool"], "beta")

    def test_picking_the_right_hand_tool_in_the_rail_swaps_sides(self):
        self.toggle()
        self.win.switch_tool("beta")
        self.assertEqual(self.sides(), ("beta", "alpha"))
        self.assertIs(self.win.side_stack.currentWidget(), self.win.pages["alpha"])

    def test_switching_the_left_leaves_the_right_alone(self):
        self.toggle()
        self.win.switch_tool("gamma")
        self.assertEqual(self.sides(), ("gamma", "beta"))
        self.assertEqual(self.win.pages["beta"].shown, 1)   # not refreshed again

    def test_dropdown_changes_the_right_hand_tool(self):
        self.toggle()
        self.pick_side("gamma")
        self.assertEqual(self.sides(), ("alpha", "gamma"))
        self.assertEqual(self.win.side_stack.count(), 1)

    def test_wide_tool_gets_the_room_it_needs(self):
        self.toggle()
        self.pick_side("gamma")
        self.app.processEvents()
        _left, right = self.win.panes.sizes()
        self.assertGreaterEqual(right, self.win.pages["gamma"].minimumSizeHint().width())

    def test_right_pane_is_tinted_by_default_and_clicks_pass_through(self):
        from PySide6.QtCore import Qt
        self.toggle()
        tint = self.win._side_tint
        self.assertTrue(tint.isVisible())
        self.assertTrue(tint.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertEqual(tint.geometry(), self.win.side_pane.rect())

    def test_settings_dropdown_changes_the_tint(self):
        from core.settings_dialog import SettingsDialog
        self.toggle()
        dialog = SettingsDialog(self.win, self.settings, self.win._on_settings_applied)
        self.addCleanup(dialog.deleteLater)
        dialog.on_set({"section": "shell", "key": "split_tint", "value": "off"})
        self.assertEqual(self.settings["split_tint"], "off")
        self.assertFalse(self.win._side_tint.isVisible())
        dialog.on_set({"section": "shell", "key": "split_tint", "value": "accent"})
        self.assertTrue(self.win._side_tint.isVisible())
        dialog.on_set({"section": "shell", "key": "split_tint", "value": "nonsense"})
        self.assertEqual(self.settings["split_tint"], "accent")

    def test_the_header_is_told_what_to_show(self):
        shown = []
        self.win.header.show_state = shown.append
        self.toggle()
        state = shown[-1]
        self.assertTrue(state["split"])
        self.assertEqual((state["side"], [c["id"] for c in state["choices"]]), ("beta", ["beta", "gamma"]))
        self.pick_side("alpha")                             # the left tool: not offered, ignored
        self.assertEqual(self.sides(), ("alpha", "beta"))

    def test_the_rail_lists_the_layout(self):
        shown = []
        self.win.rail.show_items = shown.append
        self.win.switch_tool("gamma")
        items = shown[-1]
        self.assertEqual([i.get("id") for i in items if i["type"] == "tool"], ["alpha", "beta", "gamma"])
        self.assertEqual([i["id"] for i in items if i.get("active")], ["gamma"])
        self.win.rail.on_switch({"id": "beta"})
        self.assertEqual(self.win._current_tool_id, "beta")
        self.win.rail.on_switch({"id": "nope"})
        self.assertEqual(self.win._current_tool_id, "beta")

    def test_turning_it_off_puts_every_page_back(self):
        self.toggle()
        self.toggle()
        self.assertFalse(self.win.side_pane.isVisible())
        self.assertEqual(self.win.side_stack.count(), 0)
        self.assertEqual(self.win.stack.count(), 3)
        self.assertFalse(self.settings["split_view"])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SidePaneTintTests(unittest.TestCase):
    def test_every_choice_works_under_every_palette(self):
        from core.theme import (SIDE_PANE_TINTS, DEFAULT_SIDE_PANE_TINT, get_theme_tokens,
                                list_subthemes, list_themes, side_pane_tint)
        self.assertIn(DEFAULT_SIDE_PANE_TINT, SIDE_PANE_TINTS)
        for theme in list_themes():
            for sub in list_subthemes(theme):
                tokens = get_theme_tokens(theme, sub)
                self.assertIsNone(side_pane_tint(tokens, "off"))
                for key in SIDE_PANE_TINTS.keys() - {"off"}:
                    color, alpha = side_pane_tint(tokens, key)
                    self.assertRegex(color, r"^#[0-9A-Fa-f]{6}$")
                    # A wash, never enough to hide what's under it.
                    self.assertTrue(0 < alpha <= 64, (theme, sub, key, alpha))


if __name__ == "__main__":
    unittest.main()
