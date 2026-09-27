"""Languages (core/i18n.py): the matching, the strings themselves, the
Language dropdown at the foot of Settings, and buddy.js translating a real
page offscreen - including leaving what people type alone. In-memory
settings only, like test_color_picker."""

import json
import os
import re
import unittest
from unittest import mock

import _paths  # noqa: F401
from core import i18n, settings_form as sf
from core.i18n import LANGUAGES, TRANSLATIONS, translate, translate_filter

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    import core.shell_window as shell_window
    from core.settings_dialog import SettingsDialog
    from pages.base import ToolPage
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

OTHERS = LANGUAGES[1:]
PLACEHOLDER = re.compile(r"\{\w+\}")
HAN = re.compile(r"[一-鿿]")
HANGUL = re.compile(r"[가-힯]")
KANA = re.compile(r"[぀-ヿ]")
ARABIC = re.compile(r"[؀-ۿ]")


class MatchingTests(unittest.TestCase):
    def test_english_and_unknown_text_pass_through(self):
        self.assertEqual(translate("Settings", "English"), "Settings")
        self.assertEqual(translate("Nothing like this is in the table", "Deutsch"),
                         "Nothing like this is in the table")
        self.assertEqual(translate("Settings", "Klingon"), "Settings")

    def test_whitespace_colons_and_ellipses(self):
        de = TRANSLATIONS["Settings"]["Deutsch"]
        self.assertEqual(translate("  Settings\n", "Deutsch"), f"  {de}\n")          # kept around it
        self.assertEqual(translate("Settings:", "Deutsch"), de + ":")
        self.assertEqual(translate("Settings…", "Deutsch"), de + "…")
        preset = TRANSLATIONS["Theme preset:"]["Deutsch"]
        self.assertEqual(translate("Theme preset", "Deutsch"), preset.rstrip(":"))
        self.assertEqual(translate("Organize\n        sidebar...", "Deutsch"),
                         TRANSLATIONS["Organize sidebar…"]["Deutsch"])

    def test_placeholders_keep_the_value(self):
        self.assertEqual(translate("Copied #FF00AA", "Deutsch"),
                         TRANSLATIONS["Copied {hex}"]["Deutsch"].replace("{hex}", "#FF00AA"))

    def test_a_placeholder_alone_is_no_template(self):
        self.assertFalse(i18n.is_template("{count}%"))
        self.assertTrue(i18n.is_template("Copied {hex}"))

    def test_file_filters_keep_their_patterns(self):
        with mock.patch.dict(TRANSLATIONS, {"Images": {"Deutsch": "Bilder"}}), \
                mock.patch.dict(i18n._catalogs, clear=True):
            self.assertEqual(translate_filter("Images (*.png *.jpg);;Nope (*)", "Deutsch"),
                             "Bilder (*.png *.jpg);;Nope (*)")


class StringTests(unittest.TestCase):
    """Every string, whichever file it's in (core/translations/*.json too)."""

    def test_every_translation_file_reads(self):
        for name in os.listdir(i18n.TRANSLATIONS_DIR):
            if name.endswith(".json"):
                with self.subTest(name), open(os.path.join(i18n.TRANSLATIONS_DIR, name), encoding="utf-8") as fh:
                    entries = json.load(fh)
                    for english, by_language in entries.items():
                        if not english.startswith("_"):
                            self.assertIsInstance(by_language, dict, english)

    def test_every_string_has_every_language(self):
        missing = {k: sorted(set(OTHERS) - set(v)) for k, v in TRANSLATIONS.items() if set(OTHERS) - set(v)}
        self.assertEqual(missing, {})

    def test_translations_keep_the_placeholders(self):
        wrong = [(k, lang) for k, v in TRANSLATIONS.items() for lang, text in v.items()
                 if sorted(PLACEHOLDER.findall(k)) != sorted(PLACEHOLDER.findall(text))]
        self.assertEqual(wrong, [])

    def test_each_language_is_in_its_own_script(self):
        wrong = []
        for key, entry in TRANSLATIONS.items():
            for lang, text in entry.items():
                bad = ((lang == "한국어" and HAN.search(text))
                       or (lang == "中文" and (HANGUL.search(text) or KANA.search(text)))
                       or (lang == "日本語" and HANGUL.search(text))
                       or (lang in ("Español", "Deutsch", "Français", "Tiếng Việt")
                           and (HAN.search(text) or HANGUL.search(text) or KANA.search(text) or ARABIC.search(text))))
                if bad:
                    wrong.append((key, lang, text))
        self.assertEqual(wrong, [])


class SettingsTests(unittest.TestCase):
    def test_the_dropdown_shows_each_language_in_its_own_name(self):
        fields = {f["key"]: f for f in sf.language_fields({"language": "Deutsch"}) if f.get("key")}
        box = fields["language"]
        self.assertEqual(box["value"], "Deutsch")
        self.assertTrue(box["raw"])                                  # never translated
        labels = {o["value"]: o["label"] for o in box["options"]}
        self.assertEqual(labels["한국어"], "한국어")
        self.assertEqual([o["value"] for o in box["options"]], LANGUAGES)

    def test_korean_saved_under_its_old_key_is_still_korean(self):
        # Until 1.1.3 Korean was stored as "한국인" ("Korean person").
        from core.i18n import I18nManager, canonical_language
        self.assertEqual(canonical_language("한국인"), "한국어")
        self.assertEqual(canonical_language("Deutsch"), "Deutsch")
        self.assertEqual(I18nManager("한국인").language, "한국어")
        manager = I18nManager()
        manager.language = "한국인"
        self.assertEqual(manager.language, "한국어")
        box = {f["key"]: f for f in sf.language_fields({"language": "한국인"}) if f.get("key")}["language"]
        self.assertEqual(box["value"], "한국어")                     # not English
        shared = {}
        sf.apply_shell(shared, "language", "한국인")
        self.assertEqual(shared["language"], "한국어")

    def test_choosing_a_language(self):
        shared = {}
        self.assertEqual(sf.apply_shell(shared, "language", "日本語"), "language")
        self.assertEqual(shared["language"], "日本語")
        self.assertIsNone(sf.apply_shell(shared, "language", "Klingon"))
        self.assertEqual(shared["language"], "日本語")


class _Settings(dict):
    def save(self):
        pass


class _Page(ToolPage if HAVE_QT else object):
    tool_id, display_name, category = "alpha", "Alpha", "Tools"

    def build_ui(self):
        QVBoxLayout(self).addWidget(QWidget())

    def settings_fields(self):
        return [sf.heading("Alpha"), sf.text("name", "Name", "Settings"),
                sf.select("who", "Palette", "Settings", ["Settings"], raw=True)]


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    """The real Settings window, drawn in Japanese by buddy.js."""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.settings = _Settings(theme="Resolve", language="日本語")
        for p in [mock.patch.object(shell_window, "SharedSettings", lambda: self.settings),
                  mock.patch.object(shell_window, "resolve_connect", side_effect=RuntimeError("no Resolve")),
                  mock.patch.object(shell_window.AnnouncementChecker, "start", lambda self: None),
                  mock.patch.object(shell_window.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False)]:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, i18n.get_i18n(), "language", "English")
        self.win = shell_window.ShellWindow(self.app, [("Tools", _Page)])
        self.addCleanup(self.win.deleteLater)
        page = self.win.pages["alpha"]
        self.dialog = SettingsDialog(self.win, self.settings, lambda: None, page)
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.close)
        self.dialog.show()
        self._until("document.querySelector('[data-section=language] select') !== null")

    def _js(self, code):
        loop, out = QEventLoop(), {}
        self.dialog.view.page().runJavaScript(code, 0, lambda r: (out.update(r=r), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        return out.get("r")

    def _until(self, condition):
        for _ in range(100):
            if self._js(condition):
                return
            _wait(50)
        self.fail(f"never true: {condition}")

    def _text(self, selector):
        return self._js(f"document.querySelector({selector!r}).textContent.trim()")

    def test_the_page_is_drawn_in_the_language(self):
        ja = TRANSLATIONS["Appearance"]["日本語"]
        self._until(f"[...document.querySelectorAll('h2')].some(h => h.textContent === {ja!r})")
        self.assertEqual(self._js("document.documentElement.lang"), "ja")
        self.assertEqual(self.dialog.windowTitle(), TRANSLATIONS["Settings"]["日本語"])
        # The Language dropdown is the last section, whichever tool is open.
        self.assertEqual(self._js("[...document.querySelectorAll('section[data-section]')]"
                                  ".map(s => s.dataset.section).join()"), "shell,tool,language")

    def test_what_people_type_or_name_is_left_alone(self):
        self._until("document.querySelector('[data-key=name]') !== null")
        self.assertEqual(self._js("document.querySelector('input[data-key=name]').value"), "Settings")
        self.assertEqual(self._text("select[data-key=who] option"), "Settings")       # raw: a name
        self.assertEqual(self._text("select[data-key=language] option[value=English]"), "English")

    def test_switching_back_to_english_puts_the_english_back(self):
        ja = TRANSLATIONS["Appearance"]["日本語"]
        self._until(f"[...document.querySelectorAll('h2')].some(h => h.textContent === {ja!r})")
        self.dialog.on_set({"section": "language", "key": "language", "value": "English"})
        self._until("[...document.querySelectorAll('h2')].some(h => h.textContent === 'Appearance')")
        self.assertEqual(self.settings["language"], "English")
        self.assertEqual(self.dialog.windowTitle(), "Settings")
        self.assertEqual(self._js("document.documentElement.lang"), "en")

    def test_text_a_page_adds_later_is_translated_too(self):
        self._js("document.body.append(Buddy.el('p#late', {title: 'Settings'}, ['Copied #123456']))")
        self._until("document.getElementById('late') !== null")
        _wait(50)
        self.assertEqual(self._text("#late"), TRANSLATIONS["Copied {hex}"]["日本語"].replace("{hex}", "#123456"))
        self.assertEqual(self._js("document.getElementById('late').title"), TRANSLATIONS["Settings"]["日本語"])


if __name__ == "__main__":
    unittest.main()
