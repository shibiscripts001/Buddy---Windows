"""The web pages' theme (app/core/web_theme.py) and the contract between the
web views and their Python pages. No Qt: the CSS/JS files are read as text.

Every var(--name) a stylesheet uses must be defined by every theme - a
missing one silently falls back to buddy.css's placeholder grey, which
only shows up as "that one button looks wrong under Retro"."""

import re
import unittest

import _paths
from core import theme as t
from core.web_theme import web_theme

WEB = _paths.APP / "web"
# Every web tool page: pages/<tool>/web/index.html next to its page.py.
PAGE_WEB_DIRS = sorted(p.parent for p in (_paths.APP / "pages").glob("*/web/index.html"))
# Python a page hosts from elsewhere, whose view shares the page's: Transcribe's Text+
# tabs are text_animator/text_plus.py (their script sends and hears through the local
# send()/on() that add the "tp_" prefix).
HOSTED = {"transcribe": [_paths.APP / "pages" / "text_animator" / "text_plus.py"]}
# Web windows of their own (core/web_page.py WebWindow): view folder -> its Python.
_CP = _paths.APP / "pages" / "color_palette"
_CORE, _SHELL = _paths.APP / "core", WEB / "shell"
WINDOW_WEB = {
    _CP / "web" / "mini": _CP / "mini_palette_window.py",
    _paths.APP / "pages" / "manual_chat" / "web" / "build": _paths.APP / "pages" / "manual_chat" / "bundle_dialog.py",
    _SHELL / "header": _CORE / "shell_web.py",
    _SHELL / "rail": _CORE / "shell_web.py",
    _SHELL / "settings": _CORE / "settings_dialog.py",
    _SHELL / "message": _CORE / "message_dialog.py",
    _SHELL / "consent": _CORE / "write_consent.py",
    _SHELL / "organizer": _CORE / "nav_organizer.py",
    _SHELL / "announcements": _CORE / "announcements_window.py",
    _SHELL / "taskbar": _CORE / "desk_web.py",
    _SHELL / "deskmenu": _CORE / "desk_web.py",
}
# A bespoke theme's own rules (imported by buddy.css): themes/<shape>.css.
THEME_SHEETS = sorted((WEB / "themes").glob("*.css"))
STYLESHEETS = ([WEB / "buddy.css", _SHELL / "shell.css", *THEME_SHEETS]
               + [css for d in [*PAGE_WEB_DIRS, *WINDOW_WEB] for css in d.glob("*.css")])


def _selectors(css):
    """Every selector in a stylesheet, one per comma-separated part (commas
    inside :is(...) don't split). @keyframes steps aren't selectors."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    css = re.sub(r"@import[^;]*;", "", css)
    out = []
    for prelude in re.findall(r"([^{}]+)\{[^{}]*\}", css):
        part, depth = "", 0
        for ch in prelude:
            depth += (ch == "(") - (ch == ")")
            if ch == "," and depth == 0:
                out.append(part.strip()); part = ""
            else:
                part += ch
        out.append(part.strip())
    return out


def all_themes():
    for name in t.list_themes():
        for sub in t.list_subthemes(name):
            yield name, sub, web_theme(name, sub, t.get_theme_tokens(name, sub))


class WebThemeTests(unittest.TestCase):
    def test_every_variable_the_css_uses_is_defined_by_every_theme(self):
        used, local = set(), set()
        for sheet in STYLESHEETS:
            text = sheet.read_text(encoding="utf-8")
            used |= set(re.findall(r"var\(--([\w-]+)", text))
            # Declared by a component itself (".pill { --dot: ... }"), not
            # the theme. buddy.css's :root fallbacks don't count - those
            # only exist until the theme arrives.
            local |= set(re.findall(r"^\s+--([\w-]+):", re.sub(r":root\s*\{[^}]*\}", "", text), re.M))
        used -= local
        self.assertIn("btn-bg", used)
        for name, sub, theme in all_themes():
            missing = used - set(theme["vars"])
            self.assertFalse(missing, f"{name}/{sub} leaves {sorted(missing)} undefined")

    def test_values_are_plain_css(self):
        for name, sub, theme in all_themes():
            for key, value in theme["vars"].items():
                self.assertIsInstance(value, str, f"{name}/{sub} --{key}")
                self.assertTrue(value.strip(), f"{name}/{sub} --{key} is empty")
                self.assertNotIn("qlineargradient", value)   # Qt syntax, not CSS
                self.assertNotRegex(value, r"[;{}]", f"{name}/{sub} --{key} would break out of the declaration")

    def test_families_follow_the_theme_shape(self):
        self.assertEqual(web_theme("Resolve", None, t.get_theme_tokens("Resolve"))["family"], "resolve")
        nova = web_theme("Nova", "Neon", t.get_theme_tokens("Nova", "Neon"))
        self.assertEqual(nova["family"], "nova")
        self.assertEqual(nova["vars"]["glass"], "1")
        self.assertTrue(nova["vars"]["card-bg"].startswith("rgba("))
        daylight = web_theme("SaaS", "Daylight", t.get_theme_tokens("SaaS", "Daylight"))
        self.assertTrue(daylight["light"])

    def test_resolve_keeps_its_measured_greys(self):
        v = web_theme("Resolve", "DaVinci", t.get_theme_tokens("Resolve", "DaVinci"))["vars"]
        self.assertEqual(v["btn-bg"], t.RESOLVE["panel"])
        self.assertEqual(v["field-bg"], t.RESOLVE["field"])
        # No filled accent button in Resolve - the accent is an outlined pill.
        self.assertEqual(v["accent-bg"], t.RESOLVE["panel"])

    def test_resolve_keeps_its_red_for_focus(self):
        """Icons, badges, banners and bars take Resolve's greys - its red on
        a folder icon or a "Recommended" chip reads as an error."""
        v = web_theme("Resolve", "DaVinci", t.get_theme_tokens("Resolve", "DaVinci"))["vars"]
        for key in ("emphasis", "emphasis-line", "emphasis-fill", "emphasis-container", "on-emphasis-container"):
            self.assertIn(v[key], t.RESOLVE.values(), key)
            self.assertNotEqual(v[key], t.RESOLVE["accent"], key)
        self.assertGreaterEqual(t.contrast_ratio(v["emphasis"], t.RESOLVE["panel"]), 4.5)

    def test_offworld_is_one_phosphor_on_black(self):
        self.assertEqual(t.theme_label("Offworld"), "Off-world")
        for sub in ("Amber", "Green", "Ice", t.SUBTHEME_CUSTOM):
            tokens = t.get_theme_tokens("Offworld", sub)
            theme = web_theme("Offworld", sub, tokens)
            v = theme["vars"]
            self.assertEqual(theme["family"], "offworld")
            self.assertFalse(theme["light"])
            # Hollow outlined controls, square corners, monospace.
            self.assertEqual(v["btn-bg"], "transparent")
            self.assertEqual(v["accent-border"], tokens["primary"])
            self.assertEqual(v["card-radius"], "0px")
            self.assertIn("monospace", v["font"])
            for key in ("text", "text-strong", "text-dim"):
                self.assertGreaterEqual(t.contrast_ratio(v[key], v["card-bg"]), 4.5, f"{sub} --{key}")
            self.assertLess(t.hue_distance(v["text"], tokens["primary"]), 0.02, sub)

    def test_only_a_themes_own_stylesheet_names_it(self):
        # Shared and page CSS style through variables; anything family-
        # specific lives in web/themes/<shape>.css, so adding a theme never
        # means editing every page.
        for sheet in STYLESHEETS:
            if sheet in THEME_SHEETS:
                continue
            found = re.findall(r'data-family="(\w+)"', sheet.read_text(encoding="utf-8"))
            self.assertFalse(found, f"{sheet.relative_to(_paths.APP)} names {sorted(set(found))} - "
                                    "use a variable, or the theme's web/themes/ stylesheet")

    def test_theme_stylesheets_are_scoped_and_imported(self):
        shapes = {spec["shape"] for spec in t.THEMES.values()}
        buddy = (WEB / "buddy.css").read_text(encoding="utf-8")
        self.assertTrue(THEME_SHEETS)
        for sheet in THEME_SHEETS:
            family = sheet.stem
            self.assertIn(family, shapes, f"{sheet.name}: no theme has shape {family!r}")
            self.assertIn(f'@import url("themes/{sheet.name}");', buddy, f"buddy.css doesn't import {sheet.name}")
            selectors = _selectors(sheet.read_text(encoding="utf-8"))
            self.assertTrue(selectors, sheet.name)
            for sel in selectors:
                self.assertTrue(sel.startswith(f'html[data-family="{family}"]'),
                                f"{sheet.name}: {sel!r} reaches beyond the {family} theme")

    def test_every_theme_names_known_tables(self):
        for name, spec in t.THEMES.items():
            self.assertIn(spec["shape"], t.SHAPE_BY_THEME, name)
            self.assertTrue(spec["status"] == "default" or spec["status"] in t.STATUS_SETS, name)
            self.assertIn(spec.get("selection", "container"), t.SELECTION_STYLES, name)
            self.assertIn(spec["default_subtheme"], spec["subthemes"], name)
            self.assertIn(t.theme_layout(name), t.LAYOUTS, name)
            for sub in spec["subthemes"]:
                self.assertIn(sub, t._PALETTES, f"{name}/{sub}")

    def test_desktop_text_reads_on_every_fill_light_or_dark(self):
        pairs = [("text", "card-bg"), ("text", "page-bg"), ("btn-fg", "btn-bg"), ("btn-hover-fg", "btn-hover-bg"),
                 ("accent-fg", "accent-bg"), ("danger-fg", "danger-bg"), ("field-fg", "field-bg"),
                 ("tab-active-fg", "tab-active-bg"), ("nav-active-fg", "nav-active-bg"),
                 ("pop-hover-fg", "pop-hover-bg"), ("bubble-fg", "bubble-bg"), ("text-dim", "card-bg")]
        darks = 0
        for sub in [*t.list_subthemes("Desktop"), t.SUBTHEME_CUSTOM]:
            tokens = t.get_theme_tokens("Desktop", sub)
            v = web_theme("Desktop", sub, tokens)["vars"]
            darks += not t._is_light(tokens["surface"])
            for fg, bg in pairs:
                self.assertGreaterEqual(t.contrast_ratio(v[fg], v[bg]), 4.4, f"{sub}: --{fg} on --{bg}")
            colors = t.desktop_colors(tokens)
            for title in colors["windows"]:
                self.assertGreaterEqual(t.contrast_ratio(colors["title_text"], title), 4.4, f"{sub}: title on {title}")
            # Shadows are ink: darker than the desk they fall on.
            self.assertLess(t._relative_luminance(colors["ink"]), t._relative_luminance(colors["desk"]), sub)
            if sub != t.SUBTHEME_CUSTOM:
                # A tinted selection, never the inverted fallback bar.
                self.assertEqual(tokens["selection_bg"], tokens["primary_container"], sub)
        self.assertGreaterEqual(darks, 3)

    def test_text_colours_are_readable(self):
        for name, sub, theme in all_themes():
            v = theme["vars"]
            surface = t.get_theme_tokens(name, sub)["surface"]
            for key in ("accent-text", "second-text", "danger-text"):
                self.assertGreaterEqual(t.contrast_ratio(v[key], surface), 4.4, f"{name}/{sub} --{key}")

    def test_text_is_sized_from_the_type_scale(self):
        """No hand-picked sizes: font-size and font shorthands take a --fs-*
        step (relative em sizes inside running text are fine)."""
        for sheet in STYLESHEETS:
            text = re.sub(r":root\s*\{[^}]*\}", "", sheet.read_text(encoding="utf-8"))
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            for m in re.finditer(r"font(?:-size)?\s*:\s*([^;}]*)", text):
                where = f"{sheet.relative_to(_paths.APP)}: {m.group(0).strip()}"
                self.assertNotRegex(m.group(1), r"\b\d+(\.\d+)?(px|pt|rem)\b", where)
                # A shorthand's size step stands on its own: "var(--fs-sm)None"
                # or "var(--fs-sm)var(..." is not CSS, and drops the whole rule.
                self.assertNotRegex(m.group(1), r"var\(--fs-[\w-]+\)(?![\s/;]|$)", where)

    def test_a_disabled_button_is_readable_but_plainly_dimmer(self):
        for name, sub, theme in all_themes():
            v = theme["vars"]
            tokens = t.get_theme_tokens(name, sub)
            card = v["card-bg"] if re.fullmatch(r"#[0-9A-Fa-f]{6}", v["card-bg"]) else tokens["surface_container"]
            label = t.contrast_ratio(v["btn-disabled-fg"], card)
            self.assertGreaterEqual(label, 3.0, f"{name}/{sub}")
            if re.fullmatch(r"#[0-9A-Fa-f]{6}", v["btn-fg"]):
                self.assertLess(label, t.contrast_ratio(v["btn-fg"], card), f"{name}/{sub}")
            self.assertEqual(v["btn-disabled-bg"], "transparent", f"{name}/{sub}")
            self.assertNotEqual(v["btn-disabled-border"], "transparent", f"{name}/{sub}")

    def test_a_custom_palette_works_too(self):
        tokens = t.get_theme_tokens("Nova", t.SUBTHEME_CUSTOM, "#33AAFF", "#101010")
        self.assertEqual(web_theme("Nova", t.SUBTHEME_CUSTOM, tokens)["vars"]["primary"], "#33AAFF")


class WebPageContractTests(unittest.TestCase):
    """Each web page's page.py and its script name the same events and
    actions, and the page only loads Buddy's own files."""

    def test_there_are_web_pages(self):
        names = [d.parent.name for d in PAGE_WEB_DIRS]
        self.assertIn("manual_chat", names)
        self.assertIn("batch_clip_renamer", names)
        self.assertIn("project_setup", names)
        self.assertIn("color_palette", names)

    def each(self):
        """(name, js, py, html, folder) per view - views sharing one Python
        file (the shell's header and rail) are checked against it together."""
        for web in PAGE_WEB_DIRS:
            js = "\n".join(f.read_text(encoding="utf-8") for f in web.glob("*.js"))
            # page.py, plus any mixin of the page's own (a class ...Mixin in its folder).
            py = [(web.parent / "page.py").read_text(encoding="utf-8")]
            for f in sorted(web.parent.glob("*.py")):
                text = f.read_text(encoding="utf-8")
                if f.name != "page.py" and re.search(r"^class \w+Mixin\b", text, re.M):
                    py.append(text)
            py += [f.read_text(encoding="utf-8") for f in HOSTED.get(web.parent.name, [])]
            yield (web.parent.name, js, "\n".join(py), (web / "index.html").read_text(encoding="utf-8"), web)
        by_py = {}
        for web, py in WINDOW_WEB.items():
            by_py.setdefault(py, []).append(web)
        for py, webs in by_py.items():
            js = "\n".join(f.read_text(encoding="utf-8") for web in webs for f in web.glob("*.js"))
            for web in webs:
                yield (f"{py.stem} ({web.name})", js, py.read_text(encoding="utf-8"),
                       (web / "index.html").read_text(encoding="utf-8"), web)

    def test_every_action_a_view_sends_has_a_handler(self):
        for name, js, py, html, _web in self.each():
            # Sent from script, or from a button marked data-action="x".
            actions = set(re.findall(r"\bsend\(\"(\w+)\"", js)) | set(re.findall(r'data-action="(\w+)"', html))
            for action in actions:
                self.assertRegex(py, rf"def on_{action}\(self", f"{name}: the view sends {action!r}")

    def test_pages_leave_the_base_class_attributes_alone(self):
        # Asset Manager once kept its current list in self.view - which is
        # the web view itself, and broke the page on load.
        reserved = ("view", "host", "_ready", "_queue", "_bridge", "_channel", "_drop_filter")
        for name, _js, py, _html, _web in self.each():
            for attr in reserved:
                self.assertNotRegex(py, rf"self\.{attr}\s*=[^=]", f"{name} assigns self.{attr}")

    def test_every_event_a_page_emits_is_drawn(self):
        for name, js, py, _html, _web in self.each():
            if not js.strip():
                continue                # a view with no script (Animation's empty Text+ tab)
            emitted = set(re.findall(r"self\.emit\(\"(\w+)\"", py))
            self.assertTrue(emitted, name)
            handled = set(re.findall(r"\b(?:Buddy\.)?on\(\"(\w+)\"", js))
            self.assertFalse(emitted - handled, f"{name}: the view never handles {sorted(emitted - handled)}")

    def test_the_views_only_load_local_files(self):
        for name, _js, _py, html, web in self.each():
            for src in re.findall(r'(?:src|href)="([^"]+)"', html):
                self.assertFalse(re.match(r"https?:", src), f"{name}: {src}")
                if not src.startswith("qrc:"):
                    self.assertTrue((web / src).resolve().exists(), f"{name}: {src}")
            self.assertIn("Content-Security-Policy", html, name)

    def test_every_icon_a_page_asks_for_exists(self):
        runtime = (WEB / "buddy.js").read_text(encoding="utf-8")
        known = set(re.findall(r"^\s+(\w+): '<", runtime, re.M))
        for name, js, _py, _html, _web in self.each():
            for wanted in set(re.findall(r"\bicon\(\"(\w+)\"\)", js)):
                self.assertIn(wanted, known, f"{name} uses icon({wanted!r})")


if __name__ == "__main__":
    unittest.main()
