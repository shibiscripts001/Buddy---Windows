"""The shell's web windows, without Qt where they can be: the Settings form
(core/settings_form.py) and each tool's section of it, the sidebar layout
edits (core/nav_layout.py), the write-consent check and the announcements
list. Tools' sections run against stand-in pages with in-memory settings -
never the real settings files, never the real clipboard or registry."""

import unittest
from unittest import mock

import _paths  # noqa: F401
from core import nav_layout, settings_form as sf
from core.announcements_window import entries
from core.write_consent import CONSENT_SENTENCE, matches, status


class Mem(dict):
    saves = 0

    @property
    def values(self):
        return self

    def save(self):
        self.saves += 1


class UI:
    """What a tool's settings code gets as `ui`: records what it asked."""

    def __init__(self, answer=True):
        self.answer, self.alerts, self.statuses, self.closed = answer, [], [], False
        self.parent = None

    def alert(self, title, text):
        self.alerts.append((title, text))

    def confirm(self, title, text, ok="OK", danger=False):
        return self.answer

    def status(self, text, tone=""):
        self.statuses.append((text, tone))

    def refresh(self):
        pass

    def close(self):
        self.closed = True


def kinds(fields):
    return {f["key"]: f for f in fields if f and f.get("key")}


class ShellFormTests(unittest.TestCase):
    def test_theme_and_subtheme(self):
        shared = Mem(theme="Resolve")
        fields = kinds(sf.shell_fields(shared, True))
        labels = {o["value"]: o["label"] for o in fields["theme"]["options"]}
        self.assertEqual(labels["Resolve"], "Default")            # labels, never keys, are shown
        self.assertFalse(fields["accent_color"]["enabled"])       # only for Custom
        self.assertEqual(sf.apply_shell(shared, "theme", "Retro"), "theme")
        self.assertEqual(shared["subtheme"], sf.default_subtheme("Retro"))
        self.assertIsNone(sf.apply_shell(shared, "theme", "Nope"))
        self.assertIsNone(sf.apply_shell(shared, "subtheme", "Nope"))
        self.assertEqual(sf.apply_shell(shared, "subtheme", "Custom"), "theme")
        self.assertTrue(kinds(sf.shell_fields(shared, True))["accent_color"]["enabled"])
        self.assertEqual(sf.apply_shell(shared, "accent_color", "#12ab34"), "theme")
        self.assertEqual(shared["accent_Retro"], "#12AB34")
        self.assertIsNone(sf.apply_shell(shared, "accent_color", "red"))
        sf.reset_theme(shared)
        self.assertEqual(shared["theme"], sf.DEFAULT_THEME)
        self.assertNotIn("accent_Retro", shared)

    def test_window_settings(self):
        shared = Mem()
        self.assertEqual(sf.apply_shell(shared, "split_tint", "off"), "window")
        self.assertIsNone(sf.apply_shell(shared, "split_tint", "neon"))
        self.assertEqual(sf.apply_shell(shared, "stay_on_top", 1), "window")
        self.assertIs(shared["stay_on_top"], True)
        self.assertEqual(sf.apply_shell(shared, "keep_running_in_tray", False), "tray")
        self.assertEqual(sf.apply_shell(shared, "announcements_enabled", False), "announcements")
        self.assertEqual(sf.apply_shell(shared, "autostart", True), "autostart")
        self.assertIsNone(sf.apply_shell(shared, "anything", True))
        unknown = kinds(sf.shell_fields(shared, None))["autostart"]
        self.assertTrue(unknown["disabled"])                      # Windows' setting couldn't be read

    def test_numbers(self):
        self.assertEqual(sf.parse_number("12,5", 0, 100), 12.5)
        for bad in ("", "x", "101", "-1", None, "nan"):
            self.assertIsNone(sf.parse_number(bad, 0, 100), bad)


class NavLayoutEditTests(unittest.TestCase):
    def layout(self):
        return [{"type": "divider", "label": "Edit"}, {"type": "tool", "id": "a", "visible": True},
                {"type": "tool", "id": "b", "visible": True}]

    def test_edits(self):
        layout = self.layout()
        self.assertEqual(nav_layout.move(layout, 2, 0), "")
        self.assertEqual(layout[0]["id"], "b")
        self.assertEqual(nav_layout.move(layout, 0, 9), "nothing")
        self.assertEqual(nav_layout.set_visible(layout, 0, False), "")
        self.assertEqual(nav_layout.set_visible(layout, 2, False), "At least one tool has to stay visible.")
        self.assertTrue(layout[2]["visible"])
        self.assertEqual(nav_layout.set_visible(layout, 1, False), "nothing")   # a divider
        nav_layout.add_divider(layout, 1, "  Colour   tools ")
        self.assertEqual(layout[1], {"type": "divider", "label": "Colour tools"})
        self.assertEqual(nav_layout.rename_divider(layout, 1, ""), "")
        self.assertEqual(layout[1]["label"], "")                  # a plain line
        self.assertEqual(nav_layout.rename_divider(layout, 0, "x"), "nothing")  # a tool
        self.assertEqual(nav_layout.remove_divider(layout, 1), "")
        self.assertEqual(nav_layout.remove_divider(layout, 0), "nothing")
        nav_layout.show_all(layout)
        self.assertTrue(all(e["visible"] for e in layout if e["type"] == "tool"))


class ConsentAndNewsTests(unittest.TestCase):
    def test_consent_sentence(self):
        self.assertTrue(matches(f"  {CONSENT_SENTENCE} "))
        self.assertFalse(matches(CONSENT_SENTENCE.upper()))
        self.assertEqual(status(CONSENT_SENTENCE), "Sentence matches.")
        self.assertEqual(status(""), "")
        self.assertIn("capitalisation", status(CONSENT_SENTENCE.upper()))
        self.assertEqual(status("I allow"), f"Keep going – {len(CONSENT_SENTENCE) - 7} characters to go.")
        self.assertIn("does not match", status("nope"))

    def test_announcements(self):
        items = [{"id": 3, "title": "<b>Hi</b>", "ts": 1_750_000_000, "text": "x"}, {"id": 1, "title": "Old", "ts": 0,
                                                                                    "text": ""}]
        shown = entries(items, 2)
        self.assertEqual([e["new"] for e in shown], [True, False])
        self.assertEqual(shown[0]["title"], "<b>Hi</b>")          # text; the view never renders HTML
        self.assertEqual(shown[1]["when"], "")


class ToolSectionTests(unittest.TestCase):
    def test_asset_manager(self):
        from pages.asset_manager.settings_panel import AssetSettingsMixin

        class Page(AssetSettingsMixin):
            settings, pushed = Mem(), []

            def _push_list(self):
                self.pushed.append(True)

        page = Page()
        self.assertEqual(set(kinds(page.settings_fields())), {"include_folders_in_sort", "group_all_media_by_folder"})
        page.on_setting("include_folders_in_sort", False, UI())
        self.assertIs(page.settings["include_folders_in_sort"], False)
        self.assertEqual(len(page.pushed), 1)
        page.on_setting("unknown", True, UI())
        self.assertNotIn("unknown", page.settings)

    def test_project_setup(self):
        from pages.project_setup.settings_panel import ProjectSetupSettingsMixin

        class Page(ProjectSetupSettingsMixin):
            def __init__(self):
                self.data_mgr = type("D", (), {"settings": {}})()
                self.set = []

            def _set_setting(self, key, value):
                self.set.append((key, value))

        page = Page()
        self.assertEqual(kinds(page.settings_fields())["ffmpeg_path"]["browse"], "browse_ffmpeg")
        page.on_setting("ffmpeg_path", "  C:/ffmpeg.exe ", UI())
        page.on_setting("shift_close_gaps", 1, UI())
        self.assertEqual(page.set, [("ffmpeg_path", "C:/ffmpeg.exe"), ("shift_close_gaps", True)])

    def test_color_palette(self):
        from pages.color_palette.settings_panel import ColorPaletteSettingsMixin

        class Page(ColorPaletteSettingsMixin):
            def __init__(self):
                self.data_mgr = mock.Mock(settings={"focus_mode_opacity": 0.5})
                self.i18n = mock.Mock(language="English")
                self.calls = []

            def apply_mini_palette_transparency(self):
                self.calls.append("mini")

            def update_focus_overlay(self):
                self.calls.append("focus")

        page = Page()
        fields = kinds(page.settings_fields())
        self.assertEqual((fields["focus_mode_opacity"]["value"], fields["focus_mode_opacity"]["readouts"]["50"]),
                         (50, "50%"))
        page.on_setting("focus_mode_opacity", 80, UI())
        self.assertEqual(page.data_mgr.settings["focus_mode_opacity"], 0.8)   # stored 0.1-1.0
        page.on_setting("focus_mode_opacity", 5, UI())                        # out of range
        page.on_setting("mini_palette_transparency", "90%", UI())
        page.on_setting("mini_palette_transparency", "50%", UI())
        self.assertEqual(page.data_mgr.settings["mini_palette_transparency"], "90%")
        page.on_setting("language", "Deutsch", UI())
        self.assertEqual(page.i18n.language, "Deutsch")
        self.assertEqual(page.calls, ["focus", "mini"])

    def test_buddy_network(self):
        from pages.buddy_network.settings_panel import NetworkSettingsMixin

        class Page(NetworkSettingsMixin):
            display_name, tool_id, room_id, client = "Buddy Network", "buddy_network", "global", None

            def __init__(self):
                self.settings = Mem()
                self.archive = mock.Mock()
                self.archive.conversations.return_value = [{"room": "dm-x"}]
                self.host = mock.Mock()
                self.opened = False

            def _server_url(self):
                return self.settings.get("server_url") or "wss://chat.trevorsiebe.com"

            def open_saved_chats(self):
                self.opened = True

        page, ui = Page(), UI()
        page.on_setting("server_url", "http://example.com", ui)
        self.assertIn("wss://", kinds(page.settings_fields())["server_url"]["error"])
        self.assertNotIn("server_url", page.settings)
        page.on_setting("server_url", "wss://test.example.com", ui)
        self.assertEqual(page.settings["server_url"], "wss://test.example.com")
        self.assertIsNone(kinds(page.settings_fields())["server_url"]["error"])
        self.assertEqual(ui.statuses[-1], ("Saved.", "success"))
        page.on_setting("keep_dms", False, UI(answer=True))
        page.archive.delete_all.assert_called_once()
        page.on_settings_action("saved_chats", ui)
        self.assertTrue(ui.closed and page.opened)
        page.host.switch_tool.assert_called_once_with("buddy_network")

    def test_time_tracker(self):
        from pages.time_tracker.settings_panel import TrackerSettingsMixin

        class Page(TrackerSettingsMixin):
            def __init__(self):
                self.data_mgr = mock.Mock(settings={})
                self.data_mgr.get_project_rate_info.return_value = {"rate": 0.0, "currency": "USD"}
                self.engine = mock.Mock(detected_project_name="Short Film")
                self.applied = 0

            def _apply_settings(self):
                self.applied += 1

        page = Page()
        fields = kinds(page.settings_fields())
        self.assertIn("project_rate", fields)
        page.on_setting("weekly_goal_hours", "12,5", UI())
        page.on_setting("monthly_goal_hours", "lots", UI())
        self.assertEqual((page.data_mgr.settings["weekly_goal_hours"], page.data_mgr.settings["monthly_goal_hours"]),
                         (12.5, 0.0))
        page.on_setting("poll_interval_seconds", 7, UI())          # not an option
        self.assertNotIn("poll_interval_seconds", page.data_mgr.settings)
        page.on_setting("project_rate", "45", UI())
        page.data_mgr.set_project_rate.assert_called_with("Short Film", 45.0, "USD")
        page.engine.detected_project_name = None
        self.assertNotIn("project_rate", kinds(page.settings_fields()))

    def test_ask_buddy(self):
        from pages.manual_chat.settings_panel import ChatSettingsMixin

        class Page(ChatSettingsMixin):
            def __init__(self):
                self.settings = Mem(provider="gemini")
                self.revoked = self.trimmed = 0

            def _save(self, key, value):
                self.settings[key] = value

            def _on_writes_revoked(self):
                self.revoked += 1

            def _on_history_limit_changed(self):
                self.trimmed += 1

        page = Page()
        with mock.patch("pages.manual_chat.settings_panel.default_data_paths", return_value=("", "")):
            fields = kinds(page.settings_fields())
            self.assertIn("api_key", fields)
            self.assertNotIn("base_url", fields)                  # only OpenAI-compatible has one
            page.on_setting("api_key", "abc", UI())
            self.assertEqual(page.settings["api_key_gemini"], "abc")
            page.on_setting("provider", "openai", UI())
            fields = kinds(page.settings_fields())
            self.assertIn("base_url", fields)
            self.assertEqual(fields["api_key"]["value"], "")      # its own key, not Gemini's
            page.on_setting("provider", "llamacpp", UI())
            with mock.patch("pages.manual_chat.settings_panel.list_openai_models", return_value=["qwen3-14b"]) as listed:
                fields = kinds(page.settings_fields())
                page.settings_fields()                           # listed once, then cached
            self.assertEqual(listed.call_count, 1)
            self.assertEqual(fields["model"]["suggest"], ["qwen3-14b"])
            self.assertEqual(fields["api_key"]["label"], "API key (optional)")
            self.assertEqual(fields["base_url"]["label"], "Server address")
            page.on_setting("provider", "azure", UI())
            fields = kinds(page.settings_fields())
            self.assertEqual((fields["model"]["label"], "api_version" in fields), ("Deployment", True))
            page.on_setting("api_version", " 2025-01-01 ", UI())
            self.assertEqual(page.settings["api_version_azure"], "2025-01-01")
        page.on_setting("history_turns", 99, UI())
        page.on_setting("history_turns", 3, UI())
        self.assertEqual((page.settings["history_turns"], page.trimmed), (3, 1))
        page.on_setting("allow_project_writes", False, UI())
        self.assertEqual(page.revoked, 1)
        with mock.patch("pages.manual_chat.settings_panel.WriteConsentDialog.obtain", return_value=False):
            page.on_setting("allow_project_writes", True, UI())
        self.assertIs(page.settings["allow_project_writes"], False)   # no consent, stays off


if __name__ == "__main__":
    unittest.main()
