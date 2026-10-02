"""The Settings window as pages on a rail (core/settings_dialog.py,
core/settings_form.py, app/web/shell/settings/): the shell's pages, every
tool's (not only the one on screen), the AI group's Model library and
Privacy page built from the tools' models and jobs, where each change is
sent, search - and Subtitles' and Ask Buddy's rows of the library. Stand-in
tools and in-memory settings: never the real settings, models or Recycle Bin."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401
from core import settings_form as sf
from core.web_theme import link_bar_vars

try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    import core.settings_dialog as settings_dialog
    import core.shell_window as shell_window
    from core.settings_dialog import SettingsDialog
    from pages.base import ToolPage
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class Mem(dict):
    @property
    def values(self):
        return self

    def save(self):
        pass


class ShellPageTests(unittest.TestCase):
    def test_the_shells_pages_and_groups(self):
        pages = sf.shell_pages(Mem(), True)
        self.assertEqual([(p["id"], p["group"]) for p in pages],
                         [("general", "general"), ("window", "general"), ("appearance", "look"), ("about", "about")])
        general = pages[0]["fields"]
        self.assertEqual(next(f for f in general if f.get("key"))["key"], "language")    # first now, not last
        appearance = [a["action"] for f in pages[2]["fields"] if f["kind"] == "buttons" for a in f["items"]]
        self.assertIn("reset_theme", appearance)                 # reset sits with what it resets
        self.assertEqual({p["group"] for p in pages} - set(sf.GROUP_IDS), set())
        self.assertTrue(all(f for p in pages for f in p["fields"]))                       # no Nones left in

    def test_new_field_kinds(self):
        self.assertEqual(sf.link("Chat", "ai_chat")["page"], "ai_chat")
        self.assertEqual(sf.storage("1 GB", [{"label": "a", "bytes": 0}, {"label": "b", "bytes": 5}])["parts"],
                         [{"label": "b", "bytes": 5}])           # nothing drawn for an empty part
        self.assertIsNone(sf.progress("Working")["value"])


if HAVE_QT:
    class _Plain(ToolPage):
        """A tool with settings_fields only: one Tools page."""
        tool_id, display_name, category = "plain", "Plain", "Tools"

        def build_ui(self):
            QVBoxLayout(self).addWidget(QWidget())
            self.got = []

        def settings_fields(self):
            return [sf.heading("Plain"), sf.check("tick", "Tick", False)]

        def on_setting(self, key, value, ui):
            self.got.append((key, value))

    class _Smart(_Plain):
        """A tool with AI pages, models and jobs."""
        tool_id, display_name = "smart", "Smart"

        def settings_pages(self):
            return [sf.page("ai_translation", "ai", "Translation", [sf.check("t", "T", True)]),
                    sf.page("ai_chat", "ai", "Chat assistant", [sf.check("c", "C", True)]),
                    sf.page("smart", "tools", "Smart", [sf.link("Chat", "ai_chat")])]

        def ai_models(self):
            return [{"id": "gone", "label": "Gone", "kind": "Search", "where": "missing", "actions": []},
                    {"id": "big", "label": "Big", "kind": "Transcription", "where": "local", "bytes": 3_000_000_000},
                    {"id": "small", "label": "Small", "kind": "Search", "where": "local", "bytes": 500_000_000}]

        def ai_jobs(self):
            return [{"label": "Chat assistant", "where": "cloud", "text": "Sends to Someone", "page": "ai_chat"}]

        def on_settings_action(self, action, ui):
            self.got.append(("action", action))

    class _Broken(_Plain):
        tool_id, display_name = "broken", "Broken"

        def settings_pages(self):
            raise RuntimeError("a bug in one tool")


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class _Shell(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        quiet = mock.patch("traceback.print_exc")        # the broken tool's traceback
        quiet.start()
        self.addCleanup(quiet.stop)
        self.settings = Mem(theme="Resolve")
        for p in [mock.patch.object(shell_window, "SharedSettings", lambda: self.settings),
                  mock.patch.object(shell_window, "resolve_connect", side_effect=RuntimeError("no Resolve")),
                  mock.patch.object(shell_window.AnnouncementChecker, "start", lambda self: None),
                  mock.patch.object(shell_window.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False)]:
            p.start()
            self.addCleanup(p.stop)
        self.win = shell_window.ShellWindow(self.app, [("Tools", _Plain), ("Tools", _Broken), ("Tools", _Smart)])
        self.addCleanup(self.win.deleteLater)

    def dialog(self, active=None):
        d = SettingsDialog(self.win, self.settings, lambda: None, active)
        self.addCleanup(d.deleteLater)
        self.addCleanup(d.close)
        d.emit = mock.Mock()
        return d


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class LinkBarThemeTests(_Shell):
    def test_the_link_bar_takes_each_new_theme(self):
        """It kept the last theme's look (Desktop's, after going back to
        Default): the shell repainted every bar but it."""
        self.win.set_link_bar_visible(True)
        self.win.link_bar.on_theme_changed = mock.Mock()
        self.settings.update(theme="Desktop", subtheme="Cocoa")
        self.win.apply_theme()
        self.settings.update(theme="Resolve", subtheme="DaVinci")
        self.win.apply_theme()
        self.assertEqual(self.win.link_bar.on_theme_changed.call_count, 2)

    def test_its_own_settings_repaint_it_at_once(self):
        self.win.set_link_bar_visible(True)
        self.win.link_bar.on_theme_changed = mock.Mock()
        d = self.dialog()
        d.on_set({"key": "link_bar_shade", "value": "darker", "section": "shell"})
        self.assertEqual(self.settings["link_bar_shade"], "darker")
        self.win.link_bar.on_theme_changed.assert_called_once()
        self.assertEqual(self.win.link_bar.theme_vars(self.win.theme_tokens())["linkbar-bg"],
                         link_bar_vars("Resolve", None, self.win.theme_tokens(),
                                       {"shade": "darker", "tint": "off", "icons": "theme"})["linkbar-bg"])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class DialogTests(_Shell):
    def test_every_tools_pages_in_the_rails_order(self):
        pages = self.dialog().pages()
        self.assertEqual([p["id"] for p in pages],
                         ["general", "window", "appearance", "ai_library", "ai_chat", "ai_translation", "ai_privacy",
                          "plain", "smart", "about"])
        plain = next(p for p in pages if p["id"] == "plain")
        self.assertEqual(plain["fields"][0]["key"], "tick")      # its name is the page's title already
        self.assertEqual({p["owner"] for p in pages if p["group"] == "ai"}, {"shell", "smart"})

    def test_the_model_library_and_privacy_page(self):
        pages = {p["id"]: p for p in self.dialog().pages()}
        storage, models = pages["ai_library"]["fields"][:2]
        self.assertEqual(storage["total"], "3.5 GB on this PC")
        self.assertEqual([r["id"] for r in models["rows"]], ["big", "small", "gone"])     # what's here first
        self.assertEqual({r["owner"] for r in models["rows"]}, {"smart"})
        status = next(f for f in pages["ai_privacy"]["fields"] if f["kind"] == "status")
        self.assertEqual((status["text"], status["tone"], status["page"]), ("Sends to Someone", "warn", "ai_chat"))

    def test_it_opens_on_general_whatever_tool_is_on_screen(self):
        smart = self.dialog(self.win.pages["smart"])
        self.assertEqual(smart._first_page(smart.pages()), "general")
        d = self.dialog()
        d.push()
        self.assertEqual(d.emit.call_args[0][1]["open"], "general")
        d.push()
        self.assertNotIn("open", d.emit.call_args[0][1])          # only the first time

    def test_a_tools_right_click_opens_its_own_page(self):
        smart = SettingsDialog(self.win, self.settings, lambda: None, None, open_tool="smart")
        self.addCleanup(smart.deleteLater)
        self.assertEqual(smart._first_page(smart.pages()), "smart")             # its Tools page, not an AI one
        plain = SettingsDialog(self.win, self.settings, lambda: None, None, open_tool="plain")
        self.addCleanup(plain.deleteLater)
        self.assertEqual(plain._first_page(plain.pages()), "plain")
        self.assertEqual(settings_dialog.tool_pages([{"id": "x", "owner": "a", "group": "ai"}], "a")[0]["id"], "x")
        self.assertTrue(self.win.has_settings("smart"))
        self.assertFalse(self.win.has_settings("broken"))                       # its pages raise
        self.assertFalse(self.win.has_settings("nope"))
        with mock.patch.object(shell_window, "SettingsDialog") as dialog:
            self.win.open_settings("smart")
            self.win.open_settings("broken")
        self.assertEqual([c.kwargs["open_tool"] for c in dialog.call_args_list], ["smart", None])

    def test_the_sidebar_and_taskbar_menus_offer_it(self):
        made = []

        class Menu(shell_window.QMenu):
            def exec(self, *_a):
                made.append({a.text(): a.isEnabled() for a in self.actions() if a.text()})

        with mock.patch.object(shell_window, "QMenu", Menu):
            self.win.rail.on_menu({"id": "smart"})
            self.win.rail.on_menu({"id": "broken"})
        self.assertEqual([m["Settings…"] for m in made], [True, False])
        self.win.desk_menu = mock.Mock()
        with mock.patch.object(self.win.desktop, "window_state", return_value=None):
            self.win.open_task_menu("smart", None)
        items = self.win.desk_menu.popup.call_args[0][1]["items"]
        self.assertIn("settings", [i["action"] for i in items])
        with mock.patch.object(self.win, "open_settings") as opened:
            self.win.task_action("settings", "smart")
        opened.assert_called_once_with("smart")

    def test_changes_go_to_the_page_they_came_from(self):
        d = self.dialog(self.win.pages["plain"])
        d.on_set({"section": "smart", "key": "t", "value": False})
        d.on_set({"section": "tool", "key": "tick", "value": True})          # "tool": the one on screen
        d.on_action({"section": "smart", "action": "download:big"})
        self.assertEqual(self.win.pages["smart"].got, [("t", False), ("action", "download:big")])
        self.assertEqual(self.win.pages["plain"].got, [("tick", True)])
        d.on_set({"section": "shell", "key": "stay_on_top", "value": True})
        self.assertTrue(self.settings["stay_on_top"])

    def test_a_tool_redraws_an_open_window_a_few_times_a_second(self):
        d = self.dialog()
        with mock.patch.object(d, "push") as push:
            for _ in range(20):
                settings_dialog.refresh_open("smart")
            _wait(settings_dialog.REFRESH_MS + 150)
        self.assertEqual([c for c in push.call_args_list if c.args], [mock.call({"smart"})])
        d.done(0)
        self.assertNotIn(d, settings_dialog._OPEN)
        settings_dialog.refresh_open("smart")                     # a closed window: nothing to do


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ViewTests(_Shell):
    """The real view: the rail, a page, and search."""

    def setUp(self):
        super().setUp()
        self.view_dialog = SettingsDialog(self.win, self.settings, lambda: None, self.win.pages["plain"])
        self.addCleanup(self.view_dialog.deleteLater)
        self.addCleanup(self.view_dialog.close)
        self.view_dialog.show()
        self._until("document.querySelector('[data-page=general]') !== null")

    def search(self, text):
        self._js(f"{{ const s = document.getElementById('search'); s.value = {text!r}; s.dispatchEvent(new Event('input')); }}")

    def _js(self, code):
        loop, out = QEventLoop(), {}
        self.view_dialog.view.page().runJavaScript(code, 0, lambda r: (out.update(r=r), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        return out.get("r")

    def _until(self, condition):
        for _ in range(100):
            if self._js(condition):
                return
            _wait(50)
        self.fail(f"never true: {condition}")

    def test_the_rail_and_its_pages(self):
        self.assertEqual(self._js("[...document.querySelectorAll('.set-rail-item')].map(b => b.title).join()"),
                         "General,Look,AI,Tools,About")
        # General > General first, whatever tool is on screen ("plain", a Tools page).
        self.assertEqual(self._js("document.querySelector('.set-rail-item[aria-current=page]').title"), "General")
        self.assertEqual(self._js("document.getElementById('title').textContent"), "General")
        # No heading over a group's pages repeating the rail's name.
        self.assertTrue(self._js("document.getElementById('group-name') === null"))
        self._js("document.querySelector('.set-rail-item[title=AI]').click()")
        self._until("document.getElementById('title').textContent === 'Model library'")
        self.assertEqual(self._js("[...document.querySelectorAll('#pages .set-page')].map(b => b.textContent).join()"),
                         "Model library,Chat assistant,Translation,Privacy and safety")

    def test_search_finds_a_setting_on_any_page_and_opens_it(self):
        self.search("stay on top")
        self._until("document.getElementById('title').textContent === 'Search results'")
        self.assertEqual(self._js("[...document.querySelectorAll('#pages .set-page-name')].map(b => b.textContent).join()"),
                         "")                                      # "stay" isn't in "Keep Buddy on top"
        self.search("keep top")
        self._until("document.querySelector('#form input[data-key=stay_on_top]') !== null")    # the real control
        self._js("document.querySelector('.set-crumb').click()")
        self._until("document.getElementById('title').textContent === 'Window'")
        self.assertEqual(self._js("document.getElementById('search').value"), "")
        self.assertTrue(self._js("document.querySelector('.set-hit[data-key=stay_on_top]') !== null"))
        self.search("big")
        self._until("document.querySelectorAll('#form .set-model').length === 1")             # one row, not all


class TranscribeRowTests(unittest.TestCase):
    """Subtitles' rows: the Setup tab's, in Settings' words."""

    def page(self, env_ready=True, busy=False):
        from pages.transcribe import env_setup as es
        from pages.transcribe.settings_panel import TranscribeSettingsMixin

        root = Path(tempfile.mkdtemp())

        class Page(TranscribeSettingsMixin):
            def __init__(self):
                self.job = object() if busy else None
                self.job_kind, self.result = None, None
                self.env = mock.Mock(ready=env_ready, verified=True, detail="", versions={})
                self.hw = mock.Mock(recommended_model="large-v3", nvidia=True, describe=lambda: "GPU")
                self.models = {"large-v3": str(es.MODELS_DIR / "large-v3"), "small": str(root / "mine")}
                self.found = {"parakeet-v3": str(root / "hf")}
                self.tmodels, self.tfound = {}, {}
                self.verified = {self.models["large-v3"]: True, self.models["small"]: False}
                self.sizes = {self.models["large-v3"]: 3_100_000_000}
                self.settings = Mem(extra_models={"small": str(root / "mine")}, model="small")
                self._probe = None
                self.logged, self.rescanned = [], 0

            def _add_log(self, text, kind="info"):
                self.logged.append(text)

            def _rescan(self):
                self.rescanned += 1

        return Page()

    def rows(self, page):
        return {r["id"]: r for r in page.ai_models()}

    def labels(self, row):
        return [a["label"] for a in row["actions"]]

    def test_rows_say_and_offer_what_the_setup_tab_does(self):
        rows = self.rows(self.page())
        self.assertEqual((rows["large-v3"]["chip"]["text"], self.labels(rows["large-v3"])), ("Verified", ["Remove"]))
        self.assertEqual(rows["large-v3"]["bytes"], 3_100_000_000)
        self.assertEqual((rows["small"]["chip"]["text"], self.labels(rows["small"])),
                         ("Not verified", ["Re-download (0.48 GB)", "Remove"]))
        self.assertEqual(self.labels(rows["parakeet-v3"]), ["Use this copy", "Download (0.67 GB)"])
        self.assertEqual(rows["large-v3-turbo"]["where"], "missing")
        self.assertIn("Recommended", rows["large-v3"]["sub"])
        self.assertEqual(rows["engine"]["actions"][0]["action"], "install_env")

    def test_downloads_wait_for_the_engine_and_for_a_running_job(self):
        turbo = self.rows(self.page(env_ready=False))["large-v3-turbo"]["actions"][-1]
        self.assertEqual((turbo["disabled"], turbo["tip"]), (True, "Install the engine first."))
        busy = self.page(busy=True)
        self.assertTrue(all(a["disabled"] for r in self.rows(busy).values() for a in r["actions"]))
        ui = mock.Mock()
        busy.on_settings_action("download:large-v3-turbo", ui)
        ui.status.assert_called_once()

    def test_removing_buddys_download_recycles_it_but_a_copy_of_yours_is_only_forgotten(self):
        page = self.page()
        with mock.patch("pages.transcribe.settings_panel.to_recycle_bin") as recycle:
            self.assertEqual(page.remove_model("small"), "")
            recycle.assert_not_called()                           # the user's own folder: left alone
            self.assertEqual(page.settings["extra_models"], {})
            self.assertEqual(page.settings["model"], "")          # no longer chosen either
            self.assertEqual(page.remove_model("large-v3"), "")
            recycle.assert_called_once_with([page.models["large-v3"]])
        self.assertEqual(page.rescanned, 2)


class AskBuddyRowTests(unittest.TestCase):
    def page(self, **settings):
        from pages.manual_chat.settings_panel import ChatSettingsMixin

        class Page(ChatSettingsMixin):
            ask_folder = os.path.join(tempfile.mkdtemp(), "ask_buddy")

            def __init__(self):
                self.settings = Mem(settings)

        return Page()

    def setUp(self):
        from pages.manual_chat import local_llama
        self.root = Path(tempfile.mkdtemp())
        for name, value in {"MODELS_DIR": self.root / "models", "RUNTIME_DIR": self.root / "llama.cpp"}.items():
            patcher = mock.patch.object(local_llama, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_cloud_chat_model_says_so_and_a_local_one_too(self):
        rows = {r["id"]: r for r in self.page(provider="anthropic", api_key_anthropic="k",
                                               model_anthropic="claude-x").ai_models()}
        self.assertEqual((rows["chat"]["label"], rows["chat"]["where"], rows["chat"]["chip"]["text"]),
                         ("claude-x", "cloud", "Cloud"))
        local = {r["id"]: r for r in self.page(provider="ollama", model_ollama="qwen3").ai_models()}
        self.assertEqual(local["chat"]["where"], "local")
        jobs = {j["label"]: j for j in self.page(provider="anthropic", api_key_anthropic="k",
                                                  model_anthropic="claude-x").ai_jobs()}
        self.assertEqual(jobs["Chat assistant"]["where"], "cloud")
        self.assertEqual(jobs["Changes to your project"]["text"], "Off")

    def test_embedding_models_on_disk_and_the_one_to_get(self):
        rows = {r["id"]: r for r in self.page().ai_models()}
        self.assertEqual((rows["gguf:embeddinggemma"]["where"], self.labels(rows["gguf:embeddinggemma"])),
                         ("missing", ["Set up"]))
        (self.root / "models").mkdir()
        (self.root / "models" / "my-embed.gguf").write_bytes(b"GGUF" + b"\0" * 100)
        rows = {r["id"]: r for r in self.page(embed_backend="buddy").ai_models()}
        mine = rows["gguf:my-embed.gguf"]
        self.assertEqual((mine["chip"]["text"], mine["where"], mine["bytes"]), ("Not verified", "local", 104))
        self.assertIn("in use", mine["sub"])

    def labels(self, row):
        return [a["label"] for a in row["actions"]]


if __name__ == "__main__":
    unittest.main()


class StartToolTests(unittest.TestCase):
    def test_which_tool_buddy_opens_on(self):
        visible = ["ask", "web", "text_animator"]
        self.assertEqual(sf.start_tool({}, visible), "ask")                                  # the first, as ever
        self.assertEqual(sf.start_tool({sf.START_TOOL_KEY: "web"}, visible), "web")
        self.assertEqual(sf.start_tool({sf.START_TOOL_KEY: "gone"}, visible), "ask")          # hidden or removed
        last = {sf.START_TOOL_KEY: sf.START_LAST, sf.LAST_TOOL_KEY: "text_animator"}
        self.assertEqual(sf.start_tool(last, visible), "text_animator")
        self.assertEqual(sf.start_tool({sf.START_TOOL_KEY: sf.START_LAST}, visible), "ask")    # none used yet
        self.assertIsNone(sf.start_tool({}, []))

    def test_the_setting(self):
        shared = Mem()
        general = next(p for p in sf.shell_pages(shared, True, tools=[("web", "Web")]) if p["id"] == "general")
        field = next(f for f in general["fields"] if f.get("key") == sf.START_TOOL_KEY)
        self.assertEqual([o["value"] for o in field["options"]], ["", "last", "web"])
        self.assertEqual(sf.apply_shell(shared, sf.START_TOOL_KEY, "web"), "startup")
        self.assertEqual(shared[sf.START_TOOL_KEY], "web")
        self.assertIsNone(sf.apply_shell(shared, sf.START_TOOL_KEY, "../evil"))
        self.assertEqual(shared[sf.START_TOOL_KEY], "web")

    def test_switching_tools_remembers_the_last(self):
        from types import SimpleNamespace
        from core.shell_window import ShellWindow
        shell = SimpleNamespace(shared_settings=Mem())
        ShellWindow._remember_tool(shell, "web")
        self.assertEqual(shell.shared_settings[sf.LAST_TOOL_KEY], "web")
        ShellWindow._remember_tool(SimpleNamespace(), "web")                # a shell without settings: nothing


class ShortcutSettingTests(unittest.TestCase):
    def test_the_switches_show_whats_there(self):
        general = next(p for p in sf.shell_pages(Mem(), True, shortcuts={"start_menu": True, "desktop": False})
                       if p["id"] == "general")
        values = {f["key"]: f["value"] for f in general["fields"] if f.get("key", "").startswith("shortcut_")}
        self.assertEqual(values, {"shortcut_start_menu": True, "shortcut_desktop": False})
        self.assertFalse(any(f.get("key", "").startswith("shortcut_")
                             for f in sf.shell_fields(Mem(), True, shortcuts=None)))   # where they can't be made
        self.assertEqual(sf.apply_shell(Mem(), "shortcut_desktop", True), "shortcut")
