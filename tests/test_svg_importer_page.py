"""SVG Importer's web page: file lists, options, and a real conversion of a
tiny SVG through the unchanged engine, with the Resolve/Fusion connection
faked - never a real Resolve, never the real
~/.figma_fusion_importer_settings.json."""

import os
import tempfile
import time
import unittest
from unittest import mock

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
       '<rect x="10" y="10" width="50" height="40" fill="#ff0000"/></svg>')


class Host:
    def __init__(self):
        self.controller, self.connected = object(), True
        self.shared_settings = {"theme": "Resolve"}

    def ensure_connected(self):
        return self.controller

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for patch in (mock.patch("pages.svg_importer.data_manager.SETTINGS_PATH",
                                 os.path.join(self._tmp.name, "settings.json")),
                      mock.patch("pages.svg_importer.engine.connect_to_resolve",
                                 return_value=(None, None, None, 1920, 1080, 24.0, None))):
            patch.start()
            self.addCleanup(patch.stop)
        from pages.svg_importer.page import SVGImporterPage
        self.page = SVGImporterPage(Host())
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))
        self.svg = os.path.join(self._tmp.name, "Logo.svg")
        with open(self.svg, "w", encoding="utf-8") as f:
            f.write(SVG)

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def wait_idle(self):
        deadline = time.monotonic() + 20
        self.app.processEvents()
        while self.page._busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()

    def test_drop_sorts_files_by_kind(self):
        anim = os.path.join(self._tmp.name, "anim.json")
        open(anim, "w").close()
        self.page.on_files_dropped([anim, os.path.join(self._tmp.name, "notes.txt")])
        self.assertEqual((self.page.kind, [f["name"] for f in self.last("files")["lottie"]]), ("lottie", ["anim.json"]))
        self.page.on_files_dropped([self.svg, self.svg])                 # no duplicates
        self.assertEqual((self.page.kind, len(self.last("files")["svg"])), ("svg", 1))

    def test_options_are_saved_in_the_standalones_terms(self):
        self.page.on_option({"kind": "svg", "name": "scale", "value": "fit"})
        self.page.on_option({"kind": "lottie", "name": "flow", "value": "vertical"})
        s = self.page.data_mgr.settings
        self.assertEqual((s["svg_scale_mode"], s["lottie_paste_orientation"]), ("Timeline (fit to comp)", "Vertical"))
        self.assertEqual(self.page.scale_mode_var.get(), "Timeline (fit to comp)")
        self.assertEqual(self.last("options")["lottie"]["flow"], "vertical")

    def test_convert_copies_fusion_paste_text(self):
        self.page.on_files_dropped([self.svg])
        self.page.on_convert(None)
        self.wait_idle()
        result = self.last("result")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["names"], ["Logo.svg"])
        text = QApplication.clipboard().text()
        self.assertIn("Tools = ordered()", text)
        self.assertEqual(self.last("comp")["width"], 1920)
        self.assertFalse(self.last("state")["busy"])

    def test_a_failed_conversion_clears_our_old_clipboard(self):
        self.page.on_files_dropped([self.svg])
        self.page.on_convert(None)
        self.wait_idle()
        with open(self.svg, "w", encoding="utf-8") as f:
            f.write("<svg xmlns='http://www.w3.org/2000/svg'></svg>")    # nothing to draw
        self.page.on_convert(None)
        self.wait_idle()
        self.assertFalse(self.last("result")["ok"])
        self.assertNotIn("Tools = ordered()", QApplication.clipboard().text())
        self.assertTrue(any(d["kind"] == "error" for d in self.last("details")))

    def test_convert_needs_files(self):
        self.page.on_convert(None)
        self.assertEqual(self.last("alert")["title"], "Add a file first")


if __name__ == "__main__":
    unittest.main()
