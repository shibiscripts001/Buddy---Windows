"""Color Palette: the view model (view.py, no Qt), the generators
(generators.py, no Qt), the image helpers (images.py) and the web page
driven directly - data in a temp folder, never the real
~/.color_palette_manager, and never the real clipboard."""

import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import _paths  # noqa: F401
import random

from pages.color_palette import generators, images, view
from pages.color_palette.data_manager import DataManager

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False


def data_in(folder, palettes=None, folders=None, tags=None):
    dm = DataManager(folder)
    dm.palettes = palettes if palettes is not None else {
        "Warm": ["#FF8800", "#CC2200"],
        "Night": ["#0D1B2A", "#415A77", "#E0E1DD"],
        "Mono": ["#FFFFFF", "#000000"],
    }
    dm.palette_folders = folders if folders is not None else {"Client": ["Night"], "Empty": []}
    dm.palette_custom_tags = tags if tags is not None else {"Mono": ["greys"]}
    return dm


class ViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dm = data_in(self._tmp.name)

    def test_hex_codes(self):
        self.assertEqual(view.normalize_hex(" 1a2b3c "), "#1A2B3C")
        self.assertEqual(view.normalize_hex("#abc"), "#AABBCC")
        for bad in ("", None, "#12345", "zzzzzz", "#1234567"):
            self.assertIsNone(view.normalize_hex(bad), bad)

    def test_swatch_shows_the_simulation_but_keeps_the_colour(self):
        sw = view.swatch("#ff0000", "Achromatopsia")
        self.assertEqual(sw["hex"], "#FF0000")
        r, g, b = (int(sw["shown"][i:i + 2], 16) for i in (1, 3, 5))
        self.assertTrue(r == g == b)
        self.assertEqual(sw["ink"], view.contrasting_text_color(sw["shown"]))

    def test_library_groups_by_folder(self):
        lib = view.library(self.dm, open_name="Warm")
        self.assertEqual([f["name"] for f in lib["folders"]], ["Client", "Empty"])
        self.assertEqual([p["name"] for p in lib["loose"]], ["Warm", "Mono"])
        self.assertFalse(lib["folders"][0]["open"])
        self.assertTrue(lib["loose"][0]["open"])
        self.assertEqual(lib["loose"][1]["tags"], ["greys"])

    def test_search_matches_names_tags_and_hex_and_opens_folders(self):
        lib = view.library(self.dm, query="415a")
        self.assertEqual([f["name"] for f in lib["folders"]], ["Client"])     # "Empty" has no match
        self.assertTrue(lib["folders"][0]["open"])
        self.assertEqual(lib["loose"], [])
        self.assertEqual([p["name"] for p in view.library(self.dm, query="GREY")["loose"]], ["Mono"])
        self.assertEqual([f["name"] for f in view.library(self.dm, query="empt")["folders"]], ["Empty"])

    def test_moving_a_colour(self):
        colors = ["a", "b", "c", "d"]
        self.assertTrue(view.move_color(colors, 0, 3))
        self.assertEqual(colors, ["b", "c", "a", "d"])
        self.assertTrue(view.move_color(colors, 3, 0))
        self.assertEqual(colors, ["d", "b", "c", "a"])
        self.assertTrue(view.move_color(colors, 1, 4))                         # to the end
        self.assertEqual(colors, ["d", "c", "a", "b"])
        for src, dst in ((1, 1), (1, 2), (9, 0), (0, 9)):                      # no-ops
            self.assertFalse(view.move_color(colors, src, dst))
        self.assertEqual(colors, ["d", "c", "a", "b"])

    def test_names(self):
        self.assertIn("enter a name", view.check_new_name(self.dm.palettes, "  "))
        self.assertIn("already exists", view.check_new_name(self.dm.palettes, "Warm"))
        self.assertIn("folder", view.check_new_name(self.dm.palette_folders, "Client", "folder"))
        self.assertIsNone(view.check_new_name(self.dm.palettes, "New"))

    def test_slots_always_eight_and_overrides_win(self):
        self.dm.visualizer_overrides = {"Warm": {"0": "#123456", "5": "#ABCDEF"}}
        slots = view.slot_colors(self.dm, "Warm")
        self.assertEqual(slots[:3], ["#123456", "#CC2200", view.DEFAULT_SLOT_HEX])
        self.assertEqual(slots[5], "#ABCDEF")
        self.assertEqual(len(slots), view.SLOT_COUNT)
        self.assertEqual(self.dm.palettes["Warm"], ["#FF8800", "#CC2200"])     # the palette itself untouched

    def test_treemap_boxes_cover_the_whole(self):
        blocks = view.treemap(view.slot_colors(self.dm, "Night"))
        area = sum(w * h for _x, _y, w, h in (b["box"] for b in blocks))
        self.assertAlmostEqual(area, 100 * 100, delta=1)
        self.assertEqual([b["pct"] for b in blocks][:2], ["40%", "10%"])
        self.assertEqual(blocks[-1]["pct"], "2.5%")

    def test_mockup_roles(self):
        m = view.mockup(view.slot_colors(self.dm, "Night"))
        self.assertEqual((m["dominant"]["hex"], m["secondary"]["hex"], m["accent"]["hex"]),
                         ("#0D1B2A", "#415A77", "#E0E1DD"))
        self.assertEqual([t["slot"] for t in m["tags"]], list(range(6)))

    def test_contrast(self):
        c = view.contrast("#FFFFFF", "#000000")
        self.assertEqual(c["ratio"], "21.00")
        self.assertTrue(all(k["pass"] for k in c["checks"]))
        grey = view.contrast("#777777", "#FFFFFF")
        self.assertEqual([k["pass"] for k in grey["checks"]], [False, False, True, False])

    def test_export_preview(self):
        self.assertIn("#ff8800", view.export_preview("Warm", ["#FF8800"], "CSS Variables (.css)").lower())
        self.assertIsNone(view.export_preview("Warm", ["#FF8800"], "Adobe Swatch Exchange (.ase)"))
        self.assertIsNone(view.export_preview("Warm", [], "CSS Variables (.css)"))

    def test_versions_newest_first(self):
        self.dm.add_palette_version_snapshot("Warm", "one")
        self.dm.palettes["Warm"].append("#00FF00")
        self.dm.add_palette_version_snapshot("Warm", "two")
        v = view.versions(self.dm, "Warm")
        self.assertEqual([(x["number"], x["label"], len(x["colors"])) for x in v], [(2, "two", 3), (1, "one", 2)])


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.settings, self.saves = {}, []
        self.rng = random.Random(7)

    def make(self, kind):
        return generators.make(kind, self.settings, lambda: self.saves.append(dict(self.settings)), self.rng)

    def test_jitter_only_applies_when_it_fits(self):
        colors = ["#336699", "#abcdef"]
        self.assertEqual(generators.apply_jitter(colors, None), ["#336699", "#ABCDEF"])
        self.assertEqual(generators.apply_jitter(colors, [(0.1, 0, 0)]), ["#336699", "#ABCDEF"])   # wrong length
        moved = generators.apply_jitter(colors, [(0.1, 0, 0), (0.1, 0, 0)], keep="#336699")
        self.assertEqual(moved[0], "#336699")                      # Harmony's base stays put
        self.assertNotEqual(moved[1], "#ABCDEF")

    def test_harmony(self):
        h = self.make("harmony")
        self.assertEqual(len(h.preview), 5)
        self.assertTrue(h.act("count", {"value": 8}))
        self.assertEqual((len(h.preview), self.settings["harmony_count"]), (8, 8))
        for bad in (2, 21, True, "8"):
            self.assertFalse(h.act("count", {"value": bad}), bad)
        self.assertTrue(h.act("wheel", {"value": "OKLCH"}))
        self.assertEqual(self.settings["wheel"], "OKLCH")
        self.assertFalse(h.act("rule", {"value": "Nope"}))
        self.assertTrue(h.act("rule", {"value": "Triad"}))
        plain = list(h.preview)
        h.act("regenerate", {})
        self.assertNotEqual(h.preview, plain)
        h.act("undo", {})
        self.assertEqual(h.preview, plain)                         # undo puts the exact colours back
        h.act("redo", {})
        self.assertNotEqual(h.preview, plain)
        self.assertFalse(h.act("redo", {}))
        self.assertTrue(h.act("base", {"hex": "0f0"}))
        self.assertEqual((h.base, h.default_name()), ("#00FF00", "Triad (#00FF00)"))
        self.assertFalse(h.act("base", {"hex": "green"}))
        self.assertEqual(generators.make("harmony", self.settings, lambda: None).count, 8)   # remembered

    def test_preview_can_be_rearranged(self):
        g = self.make("grayscale")
        first, last = g.preview[0], g.preview[-1]
        self.assertTrue(g.act("swap", {"from": 0, "to": 5}))
        self.assertEqual((g.preview[0], g.preview[5]), (last, first))
        self.assertFalse(g.act("swap", {"from": 0, "to": 0}))
        self.assertFalse(g.act("swap", {"from": 0, "to": 99}))
        self.assertTrue(g.act("replace", {"index": 1, "hex": "#123456"}))
        self.assertEqual(g.result()[1], "#123456")
        self.assertFalse(g.act("replace", {"index": 1, "hex": "<b>"}))
        result = g.result()
        result.append("#FFFFFF")
        self.assertEqual(len(g.preview), 6)                        # a new palette gets a copy

    def test_theme_builds_from_its_colours(self):
        t = self.make("theme")
        self.assertFalse(t.act("mode", {"value": "Dark"}))          # nothing to work from yet
        for hex_code in ("#FF0000", "#00FF00", "#0000FF"):
            t.act("add_color", {"hex": hex_code})
        self.assertTrue(t.act("mode", {"value": "Dark"}))
        self.assertEqual((len(t.preview), t.default_name()), (3, "Theme (Dark)"))
        made = list(t.preview)
        self.assertTrue(t.act("reorder", {"from": 0, "to": 2}))
        self.assertEqual(t.colors, ["#00FF00", "#FF0000", "#0000FF"])   # dropped before the third
        self.assertEqual(t.preview, made)                          # the preview waits for the button
        self.assertFalse(t.act("undo", {}))                        # history was about the old colours
        for _ in range(7):
            t.act("add_color", {"hex": "#111111"})
        self.assertFalse(t.act("add_color", {"hex": "#222222"}))   # ten at most
        self.assertTrue(t.act("add_palette", {"colors": ["#333333"]}) is False)
        t.act("clear", {})
        self.assertFalse(t.act("remove", {"index": 0}))

    def test_mood_and_glass(self):
        m = self.make("mood")
        m.act("add_palette", {"colors": ["#336699", "nope", "#996633"]})
        self.assertEqual(m.colors, ["#336699", "#996633"])
        m.act("mode", {"value": "Pastel"})
        self.assertEqual(m.default_name(), "Pastel Mix")
        g = self.make("glass")
        self.assertEqual(g.preview, [])
        self.assertEqual(g.shape()["primary"], "#5D8EC4")          # the stand-in before Generate
        self.assertFalse(g.act("remove", {"index": 0}))            # two at least
        self.assertTrue(g.act("generate", {}))
        self.assertEqual(len(g.preview), 3)
        g.act("undo", {})
        self.assertEqual(len(g.preview), 3)                        # back to the un-nudged set, still shown
        self.assertEqual(g.shape()["primary"], g.preview[0])

    def test_gradient(self):
        g = self.make("gradient")
        stops = g.stops()
        self.assertEqual((len(stops), stops[0][1].upper(), stops[-1][1].upper()), (41, "#140F8C", "#FFE600"))
        self.assertTrue(g.act("weight", {"value": 50}))
        self.assertEqual(g.weight_label(), "50 ▶")
        self.assertFalse(g.act("weight", {"value": 101}))
        g.act("add_color", {"hex": "#FF0000"})
        self.assertEqual(len(g.stops()), 3)
        self.assertTrue(g.act("easing", {"curve": [2, 9, -1, -9]}))
        self.assertEqual((g.easing, g.preset), ((1.0, 1.6, 0.0, -0.6), None))
        self.assertFalse(g.act("easing", {"curve": [1, 2]}))
        self.assertTrue(g.act("preset", {"value": "Ease"}))
        self.assertTrue(g.act("angle", {"value": 370}))
        self.assertEqual(g.angle, 10.0)
        self.assertTrue(g.act("png", {"width": 99999, "height": "x", "bit_depth": 16}))
        self.assertEqual(self.settings["gradient_png_export"], {"width": 8192, "height": 2160, "bit_depth": 16})
        self.assertEqual(g.preview_size(), (360, 640))
        g.act("aspect", {"value": "16:9"})
        self.assertEqual(g.preview_size(), (640, 360))


class _Response(io.BytesIO):
    def __init__(self, data, content_type="image/png"):
        super().__init__(data)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ImageHelperTests(unittest.TestCase):
    def test_search_engine_links_unwrap(self):
        url, referer = images.unwrap_search_engine_url(
            "https://www.google.com/imgres?imgurl=https://cdn.example/a.jpg&imgrefurl=https://example.com/page")
        self.assertEqual((url, referer), ("https://cdn.example/a.jpg", "https://example.com/page"))
        self.assertEqual(images.unwrap_search_engine_url("https://x.example/a.png"), ("https://x.example/a.png", None))

    def test_raw_spaces_are_encoded_once(self):
        self.assertEqual(images.sanitize_url("https://x.example/a b.png?q=red bird&x=%20"),
                         "https://x.example/a%20b.png?q=red%20bird&x=%20")

    def test_a_403_tries_the_other_referers(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.get_header("Referer"))
            if len(seen) < 3:
                raise urllib.error.HTTPError(request.full_url, 403, "no", {}, io.BytesIO())
            return _Response(b"png-bytes")

        raw = images.download_bytes("https://cdn.example/a.png", "https://source.example/", opener=opener)
        self.assertEqual(raw, b"png-bytes")
        self.assertEqual(seen, ["https://source.example/", "https://cdn.example/", None])

    def test_a_web_page_is_not_an_image(self):
        with self.assertRaisesRegex(ValueError, "did not return an image"):
            images.download_bytes("https://x.example/", opener=lambda r, timeout: _Response(b"<html>", "text/html"))
        with self.assertRaises(ValueError):
            images.download_bytes("file:///C:/secret.png")

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_image_with_palette_strip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "a.png"), os.path.join(tmp, "b.png")
            Image.new("RGB", (500, 200), (10, 20, 30)).save(src)
            images.save_with_palette(src, ["#FF0000", "#00FF00"], out)
            with Image.open(out) as result:
                self.assertEqual(result.width, 500)
                self.assertGreater(result.height, 200 + 60)
                self.assertEqual(result.getpixel((10, result.height - 5)), (255, 0, 0))
                self.assertEqual(result.getpixel((490, result.height - 5)), (0, 255, 0))


class Host:
    shared_settings = {"theme": "Resolve"}
    controller, connected = None, False

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        folder = self._tmp.name
        dm = data_in(folder)
        for save in (dm.save_palette_data, dm.save_palette_folders, dm.save_palette_custom_tags):
            save()
        from pages.color_palette import page as page_mod
        self.page_mod = page_mod
        patcher = mock.patch.object(page_mod.ColorPalettePage, "_make_data_manager", lambda s: DataManager(folder))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.page = page_mod.ColorPalettePage(Host())
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))
        self.dm = self.page.data_mgr

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def test_it_never_uses_the_real_data_folder(self):
        self.assertEqual(self.dm.base_dir, self._tmp.name)
        self.page.web_ready()
        self.assertEqual(self.last("state")["vision"], "None")
        self.assertEqual(self.last("library")["total"], 3)

    def test_add_remove_undo(self):
        self.page.on_add_color({"name": "Warm", "hex": "0f0"})
        self.assertEqual(self.dm.palettes["Warm"][-1], "#00FF00")
        self.page.on_add_color({"name": "Warm", "hex": "#00ff00"})
        self.assertIn("already", self.last("toast")["text"])
        self.page.on_add_color({"name": "Warm", "hex": "nope"})
        self.assertIn("hex code", self.last("alert")["text"])
        self.page.on_remove_color({"name": "Warm", "index": 0})
        self.assertEqual(self.last("toast")["undo"], "undo_remove")
        self.page.on_undo_remove()
        self.assertEqual(self.dm.palettes["Warm"], ["#FF8800", "#CC2200", "#00FF00"])
        self.assertEqual(DataManager(self._tmp.name).palettes["Warm"], self.dm.palettes["Warm"])   # saved

    def test_change_and_move(self):
        self.page.on_change_color({"name": "Night", "index": 0, "hex": "#111111"})
        self.page.on_move_color({"name": "Night", "from": 0, "to": 3})
        self.assertEqual(self.dm.palettes["Night"], ["#415A77", "#E0E1DD", "#111111"])

    def test_rename_follows_everywhere(self):
        self.page.current = self.page.vis_palette = "Night"
        self.dm.set_visualizer_slot_override("Night", 0, "#ABCDEF")
        self.page.on_rename_palette({"name": "Night", "new": "Night Ext"})
        self.assertEqual((self.page.current, self.page.vis_palette), ("Night Ext", "Night Ext"))
        self.assertEqual(self.dm.palette_folders["Client"], ["Night Ext"])
        self.assertEqual(self.dm.get_visualizer_slot_override("Night Ext", 0), "#ABCDEF")
        self.page.on_rename_palette({"name": "Warm", "new": "Mono"})
        self.assertIn("already exists", self.last("alert")["text"])

    def test_the_last_palette_stays(self):
        for name in ("Warm", "Night"):
            self.page.on_delete_palette({"name": name})
        self.page.on_delete_palette({"name": "Mono"})
        self.assertEqual(list(self.dm.palettes), ["Mono"])
        self.assertIn("last remaining", self.last("alert")["text"])

    def test_folders(self):
        self.page.on_new_folder({"name": "Archive"})
        self.page.on_move_palette({"name": "Warm", "folder": "Archive"})
        self.assertEqual(self.dm.palette_folders["Archive"], ["Warm"])
        self.page.on_rename_folder({"name": "Client", "new": "Acme"})
        self.assertEqual(list(self.dm.palette_folders), ["Acme", "Empty", "Archive"])   # keeps its place
        self.page.on_move_palette({"name": "Warm", "folder": None})
        self.page.on_delete_folder({"name": "Acme"})
        self.assertIsNone(self.dm.get_palette_folder("Night"))
        self.assertIn("Night", self.dm.palettes)

    def test_versions_and_tags(self):
        self.page.on_save_version({"name": "Warm", "label": "v1"})
        self.page.on_add_color({"name": "Warm", "hex": "#123123"})
        self.page.on_history({"name": "Warm"})
        self.assertEqual([v["label"] for v in self.last("history")["versions"]], ["v1"])
        self.page.on_restore_version({"name": "Warm", "index": 0})
        self.assertEqual(self.dm.palettes["Warm"], ["#FF8800", "#CC2200"])
        self.page.on_set_tags({"name": "Warm", "tags": "Sun, sun, dusk"})
        self.assertEqual(self.dm.palette_custom_tags["Warm"], ["sun", "dusk"])

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_extract_then_make_a_palette(self):
        path = os.path.join(self._tmp.name, "shot.png")
        img = Image.new("RGB", (300, 200), (200, 30, 30))
        img.paste((20, 40, 200), (0, 0, 150, 200))
        img.save(path)
        self.page.on_files_dropped([os.path.join(self._tmp.name, "notes.txt")])
        self.assertIn("isn't an image", self.last("toast")["text"])
        self.page.on_files_dropped([path])
        x = self.last("extract")
        self.assertTrue(x["has_image"] and x["preview"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.page.tab, "extract")
        self.page.on_extract_options({"count": 2, "weighing": "Balanced", "false_color": True})
        colors = [c["hex"] for c in self.last("extract")["colors"]]
        self.assertEqual(len(colors), 2)
        self.assertTrue(all(c == c.upper() for c in colors))
        self.page.on_create_from_image({"name": "Shot"})
        self.assertEqual(self.dm.palettes["Shot"], colors)
        self.assertEqual(self.page.current, "Shot")
        self.page.on_add_extracted({"hex": colors[0], "name": "Warm"})
        self.assertEqual(self.dm.palettes["Warm"][-1], colors[0])

    def test_a_download_from_an_old_request_is_dropped(self):
        stale = os.path.join(self._tmp.name, "stale.png")
        open(stale, "wb").close()
        self.page._fetch_generation = 2
        self.page._on_fetched(1, stale, "")
        self.assertFalse(os.path.exists(stale))
        self.page._fetching = "https://x.example/a.png"
        self.page._on_fetched(2, "", "403 Forbidden")
        self.assertEqual(self.last("alert")["title"], "Download failed")
        self.assertIsNone(self.page._fetching)

    def test_visualizer_edits_never_touch_the_palette_and_reset(self):
        self.page.on_vis_palette({"name": "Warm"})
        self.page.on_set_slot({"slot": 7, "hex": "#FF00AA"})
        self.page.on_swap_slots({"a": 0, "b": 1})
        v = self.last("visualize")
        self.assertEqual([b["hex"] for b in v["treemap"]][:2], ["#CC2200", "#FF8800"])
        self.assertTrue(v["can_reset"])
        self.assertEqual(self.dm.palettes["Warm"], ["#FF8800", "#CC2200"])
        self.page.on_reset_slots()
        self.assertEqual(self.dm.visualizer_overrides["Warm"], {})
        self.assertFalse(self.last("visualize")["can_reset"])

    def test_import_asks_before_replacing(self):
        path = os.path.join(self._tmp.name, "in.gpl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("GIMP Palette\nName: In\n#\n255 0 0 Red\n0 0 255 Blue\n")
        with mock.patch.object(self.page_mod.QFileDialog, "getOpenFileName", return_value=(path, "")):
            self.page.on_import_file()
        ask = self.last("import_name")
        self.assertEqual((ask["suggested"], len(ask["colors"])), ("in", 2))
        self.page.on_import_commit({"name": "Warm"})                    # exists, no overwrite
        self.assertEqual(self.dm.palettes["Warm"], ["#FF8800", "#CC2200"])
        with mock.patch.object(self.page_mod.QFileDialog, "getOpenFileName", return_value=(path, "")):
            self.page.on_import_file()
        self.page.on_import_commit({"name": "Warm", "overwrite": True})
        self.assertEqual([c.upper() for c in self.dm.palettes["Warm"]], ["#FF0000", "#0000FF"])

    def test_contrast_and_export(self):
        self.page.on_contrast_set({"which": "text", "hex": "#000"})
        self.page.on_contrast_set({"which": "bg", "hex": "#FFFFFF"})
        self.page.on_contrast_swap()
        c = self.last("tools")["contrast"]
        self.assertEqual((c["text"]["hex"], c["bg"]["hex"], c["ratio"]), ("#FFFFFF", "#000000", "21.00"))
        self.page.on_export_options({"palette": "Night", "format": "JSON (.json)"})
        t = self.last("tools")
        self.assertIn("#0d1b2a", t["preview"].lower())
        self.page.on_contrast_palette({"name": "Mono"})
        self.assertEqual([s["hex"] for s in self.last("tools")["ct_colors"]], ["#FFFFFF", "#000000"])

    def test_language(self):
        self.page.i18n.language = "Deutsch"
        try:
            s = self.last("strings")
            self.assertEqual(s["language"], "Deutsch")
            self.assertTrue(s["strings"]["Palettes"])
        finally:
            self.page.i18n.language = "English"
        self.assertEqual(self.last("strings")["strings"], {})

    def test_generators_open_in_the_tab(self):
        for gid in generators.IDS:
            self.page.on_generator({"id": gid})
            g = self.last("generator")
            self.assertEqual((g["open"], g["view"]["id"]), (gid, gid))
            self.assertEqual(g["view"]["target"], self.page.current)
        self.assertTrue(self.last("generator")["view"]["shape"])
        self.page.on_generator({"id": "gradient"})
        self.assertTrue(self.last("generator")["view"]["image"].startswith("data:image/"))
        harmony = self.page.generators["harmony"]
        self.page.on_generator({"id": "harmony"})
        self.page.on_gen({"id": "harmony", "action": "count", "value": 4})
        self.assertIs(self.page.generators["harmony"], harmony)          # kept, not remade
        self.assertEqual(len(self.last("generator")["view"]["preview"]), 4)
        self.page.on_generator({"id": None})
        self.assertEqual(self.last("generator"), {"open": None})
        self.page.on_gen({"id": "nope", "action": "count", "value": 4})   # nothing happens

    def test_generators_save_into_palettes(self):
        self.page.on_generator({"id": "grayscale"})
        made = list(self.page.generators["grayscale"].preview)
        self.page.on_gen({"id": "grayscale", "action": "create", "name": "Warm"})
        self.assertIn("already exists", self.last("alert")["text"])
        self.page.on_gen({"id": "grayscale", "action": "create", "name": "Greys"})
        self.assertEqual(self.dm.palettes["Greys"], made)
        self.assertEqual(self.last("toast")["show"], "Greys")
        before = list(self.dm.palettes[self.page.current])
        self.page.on_gen({"id": "grayscale", "action": "add", "indexes": [0, 1, 99]})
        self.assertEqual(self.dm.palettes[self.page.current], before + made[:2])
        self.page.on_gen({"id": "grayscale", "action": "add", "indexes": [0]})
        self.assertIn("Already", self.last("toast")["text"])
        # Palette colours come from the palette, whatever the view says.
        self.page.on_generator({"id": "theme"})
        self.page.on_gen({"id": "theme", "action": "add_palette", "name": "Warm", "colors": ["#000000"]})
        self.assertEqual(self.page.generators["theme"].colors, self.dm.palettes["Warm"][:10])
        self.page.on_gen({"id": "theme", "action": "source", "name": "Mono"})
        self.assertEqual(self.last("generator")["view"]["source"], "Mono")

    def test_mini_window(self):
        self.page.on_mini({"name": "Warm"})
        win = self.page.mini_windows["Warm"]
        self.addCleanup(win.deleteLater)
        shown = []
        win.emit = lambda name, payload=None: shown.append((name, payload))
        win.refresh()
        mini = [p for n, p in shown if n == "mini"][-1]
        self.assertEqual([s["hex"] for s in mini["swatches"]], self.dm.palettes["Warm"][:12])
        with mock.patch.object(self.page, "copy_to_clipboard") as copied:
            win.on_copy({"index": 1})
            copied.assert_called_once_with(self.dm.palettes["Warm"][1])
            win.on_copy({"index": 99})
            self.assertEqual(copied.call_count, 1)
        self.page.on_rename_palette({"name": "Warm", "new": "Hot"})
        self.assertIs(self.page.mini_windows["Hot"], win)
        self.page.on_delete_palette({"name": "Hot"})
        self.assertEqual(self.page.mini_windows, {})

    def test_dropper_results(self):
        self.page._dropped({"for": "add", "name": "Mono"}, "#336699")
        self.assertEqual(self.dm.palettes["Mono"][-1], "#336699")
        self.page._dropped({"for": "picker"}, "#abcdef")
        self.assertEqual(self.last("picked"), {"hex": "#ABCDEF"})


if __name__ == "__main__":
    unittest.main()
