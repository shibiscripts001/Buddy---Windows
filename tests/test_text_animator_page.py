"""Text Animator: its settings (options.py, no Qt), the canvas measuring (canvas_math) and
the web page driven against a fake Resolve timeline of Text+ clips - never a real
project, and settings in memory."""

import os
import unittest

import _paths  # noqa: F401
from pages.text_animator import options as opts
from pages.text_animator import overlays

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class Mem(dict):
    saves = 0

    @property
    def values(self):
        return self

    def save(self):
        self.saves += 1


# ------------------------------------------------------------ fake Resolve --

class Tool:
    def __init__(self, text, center=(0.5, 0.5), size=0.08, font="Arial"):
        self.inputs = {"StyledText": text, "Font": font, "Size": size, "Center": {1: center[0], 2: center[1]},
                       "Red1": 1.0, "Green1": 0.8, "Blue1": 0.2}
        self.ID = "TextPlus"

    def GetInput(self, name):
        return self.inputs.get(name)

    def SetInput(self, name, value):
        self.inputs[name] = {1: value[0], 2: value[1]} if name == "Center" else value
        return True

    def center(self):
        c = self.inputs["Center"]
        return (c[1], c[2])


class Comp:
    def __init__(self, tool):
        self.tool = tool

    def GetToolList(self, _selected=False, kind=None):
        return {1: self.tool}


class Clip:
    def __init__(self, name, start, end, tool):
        self.name, self.start, self.end, self.tool = name, start, end, tool

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetFusionCompByIndex(self, _i):
        return Comp(self.tool)


class Timeline:
    def __init__(self, tracks):
        self.tracks = tracks        # [[Clip]] per video track
        self.playhead = "01:00:00:10"

    def GetName(self):
        return "Promo"

    def GetTrackCount(self, kind):
        return len(self.tracks) if kind == "video" else 1

    def GetItemListInTrack(self, kind, index):
        return list(self.tracks[index - 1]) if kind == "video" and index <= len(self.tracks) else []

    def GetSetting(self, key):
        return {"timelineFrameRate": "24", "timelineDropFrameTimecode": "0",
                "timelineResolutionWidth": "1920", "timelineResolutionHeight": "1080"}.get(key)

    def GetCurrentTimecode(self):
        return self.playhead

    def GetStartFrame(self):
        return 86400


def fake_timeline():
    start = 86400
    return Timeline([
        [Clip("Title", start, start + 100, Tool("Big Title", (0.5, 0.7), 0.1))],
        [Clip("Hello", start, start + 50, Tool("Hello", (0.3, 0.3), 0.06)),
         Clip("Later", start + 200, start + 300, Tool("Later", (0.5, 0.5), 0.06))],
        [Clip("World", start, start + 50, Tool("World", (0.7, 0.3), 0.06))],
    ])


class Resolve:
    def __init__(self, timeline):
        self.timeline = timeline
        project = type("P", (), {"GetCurrentTimeline": lambda s: timeline})()
        self.pm = type("PM", (), {"GetCurrentProject": lambda s: project})()

    def GetProjectManager(self):
        return self.pm


class Host:
    shared_settings = {"theme": "Resolve"}

    def __init__(self, timeline=None):
        self.timeline = timeline or fake_timeline()
        self.controller = type("C", (), {})()
        self.controller.resolve = Resolve(self.timeline)
        self.connected = True
        self.busy = []
        self.tools = {}

    def ensure_connected(self):
        return self.controller

    def tool_settings(self, tool_id, defaults=None):
        return self.tools.setdefault(tool_id, Mem(defaults or {}))

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def set_busy(self, on, message=None):
        self.busy.append(on)


# ------------------------------------------------------------------ tests --

class OptionTests(unittest.TestCase):
    def test_old_settings_carry_over(self):
        s = Mem(font_size=0.12, current_font_color_r=1.0, current_font_color_g=0.0, current_font_color_b=0.0,
                outline_group_on=True, font_style_scope_specific_track=True, font_style_scope_track=3,
                anim_speed=0, anim_preset="Fade (Opacity)")
        o = opts.Options(s)
        v = o.view()
        self.assertEqual(v["sliders"]["font_size"]["value"], 0.12)
        self.assertEqual(v["colors"]["font_color"], "#FF0000")
        self.assertTrue(v["toggles"]["outline_on"])
        self.assertEqual((v["style_scope"], v["tracks"]["style_track"]), ("track", 3))
        self.assertEqual((v["anim_speed"], v["anim_preset"]), ("Fast", "Fade (Opacity)"))

    def test_setting_validates_and_saves_under_the_old_keys(self):
        s = Mem()
        o = opts.Options(s)
        self.assertTrue(o.set("font_size", 5))                       # clamped to the slider's range
        self.assertEqual(s["font_size"], 1.0)
        self.assertTrue(o.set("outline_color", "#00ff80"))
        self.assertEqual((s["outline_color_r"], s["outline_color_g"]), (0.0, 1.0))
        self.assertTrue(o.set("style_scope", "playhead"))
        self.assertEqual((s["font_style_scope_playhead"], s["font_style_scope_specific_track"]), (True, False))
        self.assertTrue(o.set("anim_speed", "Medium"))
        self.assertEqual(s["anim_speed"], 1)
        for bad in (("font_size", "big"), ("outline_color", "red"), ("style_scope", "all"), ("nope", 1),
                    ("grid_type", "Hexagons")):
            self.assertFalse(o.set(*bad), bad)
        self.assertTrue(o.set("grid_type", "Rule of Thirds"))
        self.assertEqual(o.overlay_view()["grid_type"], "Rule of Thirds")

    def test_hex(self):
        self.assertEqual(opts.to_hex((1, 0.5, 0)), "#FF8000")
        self.assertIsNone(opts.from_hex("#12345"))

    def test_safe_zones_fit_the_frame(self):
        for name, rects in overlays.SAFE_ZONE_RECTS.items():
            for left, top, right, bottom in rects:
                self.assertTrue(0 <= left < right <= 1 and 0 <= top < bottom <= 1, name)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class CanvasMathTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])

    def test_text_box_scales_with_size(self):
        from pages.text_animator.canvas_math import text_box
        a, b = text_box("Arial", "Hello", 0.05), text_box("Arial", "Hello", 0.1)
        self.assertAlmostEqual(b["w"], 2 * a["w"], places=6)
        two = text_box("Arial", "Hello\nHello", 0.1)
        self.assertGreater(two["h"], b["h"])
        self.assertAlmostEqual(two["w"], b["w"], places=6)                # widest line, not the sum

    def test_standard_grid(self):
        from pages.text_animator.canvas_math import standard_grid_spacing
        self.assertAlmostEqual(standard_grid_spacing(1920, 1080), 120 / 1920)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from pages.text_animator.page import TextAnimatorPage
        self.host = Host()
        self.page = TextAnimatorPage(self.host)
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))
        self.page.tab = "layout"
        self.page._refresh_live_preview(force=True)

    def last(self, name):
        return [p for n, p in self.events if n == name][-1]

    def tool(self, track, index=0):
        return self.host.timeline.tracks[track][index].tool

    def test_the_canvas_shows_the_clips_under_the_playhead(self):
        c = self.last("canvas")
        self.assertEqual(c["tab"], "layout")
        self.assertEqual(sorted(i["text"] for i in c["items"]), ["Big Title", "Hello", "World"])   # not "Later"
        title = next(i for i in c["items"] if i["text"] == "Big Title")
        self.assertAlmostEqual(title["cy"], 0.3)                          # Fusion y is bottom-up
        self.assertEqual(title["color"], "#FFCC33")
        self.assertGreater(title["box"]["w"], 0)
        self.events.clear()
        self.page._refresh_live_preview(force=True)
        self.assertFalse([e for e in self.events if e[0] == "canvas"])    # nothing changed, nothing sent

    def item_id(self, text):
        return next(i["id"] for i in self.last("canvas")["items"] if i["text"] == text)

    def test_move_resize_undo_redo(self):
        hello = self.item_id("Hello")
        self.page.on_move({"tab": "layout", "id": hello, "cx": 0.25, "cy": 0.2})
        self.assertEqual(self.tool(1).center(), (0.25, 0.8))
        self.page.on_resize({"tab": "layout", "id": hello, "size": 0.09})
        self.assertEqual(self.tool(1).GetInput("Size"), 0.09)
        self.assertEqual(self.last("history"), {"undo": 2, "redo": 0})
        self.page.on_undo()
        self.page.on_undo()
        self.assertEqual((self.tool(1).center(), self.tool(1).GetInput("Size")), ((0.3, 0.3), 0.06))
        self.page.on_redo()
        self.assertEqual(self.tool(1).center(), (0.25, 0.8))
        self.assertEqual(self.last("history"), {"undo": 1, "redo": 1})

    def test_group_move_is_one_undo_step(self):
        self.page.on_group_move({"tab": "layout", "moves": [
            {"id": self.item_id("Hello"), "cx": 0.2, "cy": 0.5}, {"id": self.item_id("World"), "cx": 0.8, "cy": 0.5}]})
        self.assertEqual((self.tool(1).center(), self.tool(2).center()), ((0.2, 0.5), (0.8, 0.5)))
        self.assertEqual(self.last("history")["undo"], 1)
        self.page.on_undo()
        self.assertEqual((self.tool(1).center(), self.tool(2).center()), ((0.3, 0.3), (0.7, 0.3)))

    def test_apply_position_copies_the_playhead_clip_down_its_track(self):
        self.page.on_apply_position()
        self.assertEqual(self.host.timeline.tracks[1][1].tool.center(), (0.3, 0.3))   # "Later" follows "Hello"
        self.assertEqual(self.host.busy, [True, False])

    def test_style_is_applied_by_scope(self):
        self.page.on_set({"name": "font_size", "value": 0.2})
        self.page.on_set({"name": "font_color", "value": "#0000FF"})
        self.page.on_set({"name": "style_scope", "value": "track"})
        self.page.on_set({"name": "style_track", "value": 2})
        self.page.on_apply_style()
        self.assertEqual((self.tool(1).GetInput("Size"), self.tool(1, 1).GetInput("Size")), (0.2, 0.2))
        self.assertEqual(self.tool(0).GetInput("Size"), 0.1)                           # other tracks untouched
        self.assertEqual(self.tool(1).GetInput("Blue1"), 1.0)
        self.assertEqual(self.host.tools["text_animator"]["font_size"], 0.2)          # remembered

    def test_word_layout_uses_the_selected_hero(self):
        self.page.tab = "words"
        self.page._refresh_live_preview(force=True)
        world = next(i["id"] for i in self.last("canvas")["items"] if i["text"] == "World")
        self.page.on_apply_layout({"preset": "Hero + Stack", "selected": [world]})
        self.assertEqual(self.tool(2).center(), (0.5, 0.5))                  # the hero, centred
        self.assertAlmostEqual(self.tool(2).GetInput("Size"), 0.28)
        self.assertEqual(self.last("history")["undo"], 1)

    def test_bounding_fills_the_space_between_the_lines(self):
        self.page.on_bounding({"on": True, "left": 0.2, "right": 0.8})
        self.page.on_apply_bounding()
        self.assertEqual(self.tool(0).center()[0], 0.5)
        self.assertNotEqual(self.tool(0).GetInput("Size"), 0.1)

    def test_track_suggestion_and_page_switch(self):
        self.page.on_shown()
        s = self.last("state")
        self.assertEqual((s["timeline"], s["tracks"]["video"], s["resolution"]), ("Promo", 3, [1920, 1080]))
        self.page.switch_page("Subtitle Conversion")
        self.assertEqual(self.last("state")["tab"], "subtitles")


if __name__ == "__main__":
    unittest.main()
