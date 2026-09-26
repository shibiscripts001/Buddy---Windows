"""Every theme builds a stylesheet Qt accepts, and the theme table is
consistent (app/core/theme.py). Needs PySide6 (skipped without it); runs
off-screen."""

import os
import unittest

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication, QLabel
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from core import theme
        cls.theme = theme

    def test_every_theme_and_subtheme_parses(self):
        t = self.theme
        problems = []
        qInstallMessageHandler(lambda mode, ctx, msg: problems.append(msg)
                               if "stylesheet" in msg.lower() or "parse" in msg.lower() else None)
        try:
            for name in t.list_themes():
                for sub in t.list_subthemes(name):
                    qss = t.get_app_theme(name, sub)
                    self.assertNotIn("{tokens", qss)
                    self.assertNotIn("{shape", qss)
                    self.assertNotIn("{c[", qss)
                    self.app.setStyleSheet(qss)
                    label = QLabel("x")
                    label.ensurePolished()      # forces Qt to parse the sheet
        finally:
            qInstallMessageHandler(None)
            self.app.setStyleSheet("")
        self.assertEqual(problems, [])

    def test_resolve_is_the_default_and_labels_are_unique(self):
        t = self.theme
        self.assertEqual(t.DEFAULT_THEME, "Resolve")
        self.assertEqual(t.theme_label("Resolve"), "Default")
        self.assertEqual(t.resolve("no such theme", None), ("Resolve", "DaVinci"))
        labels = [t.theme_label(k) for k in t.list_themes()]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(t.default_subtheme("Retro"), "Mulberry")

    def test_bundled_font_loads(self):
        from PySide6.QtGui import QFontDatabase
        self.theme.load_bundled_fonts()
        self.assertIn("Open Sans", QFontDatabase.families())

    def test_new_installs_start_on_the_resolve_theme(self):
        from core import settings_store
        self.assertEqual(settings_store.DEFAULT_SHARED_SETTINGS["theme"], "Resolve")
        self.assertEqual(settings_store.DEFAULT_SHARED_SETTINGS["subtheme"], "DaVinci")



class SettingsMigrationTests(unittest.TestCase):
    """Which theme a user lands on, by what their settings file holds."""

    def load(self, saved):
        import json
        import tempfile
        from core import settings_store
        d = tempfile.mkdtemp()
        path = os.path.join(d, "settings.json")
        if saved is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(saved, f)
        old = settings_store.SHARED_SETTINGS_PATH
        settings_store.SHARED_SETTINGS_PATH = path
        try:
            s = settings_store.SharedSettings()
            return s.get("theme"), s.get("subtheme")
        finally:
            settings_store.SHARED_SETTINGS_PATH = old

    def test_fresh_install_gets_the_resolve_theme(self):
        self.assertEqual(self.load(None), ("Resolve", "DaVinci"))

    def test_pre_theme_settings_still_migrate(self):
        self.assertEqual(self.load({"theme_preset": "Ocean"}), ("Default", "Ocean"))
        self.assertEqual(self.load({"theme_preset": "Retro"}), ("Retro", "Peach"))

    def test_an_existing_choice_is_kept(self):
        saved = {"theme": "Retro", "subtheme": "Custom", "_theme_migrated": True}
        self.assertEqual(self.load(saved), ("Retro", "Custom"))
        self.assertEqual(self.load({"theme": "Default", "subtheme": "Ocean"}), ("Default", "Ocean"))


if __name__ == "__main__":
    unittest.main()
