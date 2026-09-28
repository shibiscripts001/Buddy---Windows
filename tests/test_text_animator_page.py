"""The Text+ tools (the old Text Animator, now tabs on Transcribe): their settings
(options.py, no Qt), the canvas measuring (canvas_math) and TextPlusTools driven against
a fake Resolve timeline of Text+ clips, inside a stand-in for the page that hosts them -
never a real project, and settings in memory."""

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

    def GetTrackTypeAndIndex(self):
        return [self.kind, self.track]


class Timeline:
    def __init__(self, tracks):
        self.tracks = tracks        # [[Clip]] per video track
        self.playhead = "01:00:00:10"
        self.selected = []          # what's selected on the timeline (Resolve 21.0.4+)
        for index, clips in enumerate(tracks, start=1):
            for clip in clips:
                clip.kind, clip.track = "video", index

    def GetSelectedClips(self):
        return list(self.selected)

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


def font_file(test, ascent, descent, gap, win_ascent, win_descent, units=1000):
    """A minimal font file - head, hhea and OS/2, all canvas_math reads - removed after
    `test`. Vertical metrics in font units."""
    import struct
    import tempfile
    head = bytes(18) + struct.pack(">H", units) + bytes(34)
    hhea = struct.pack(">I", 0x00010000) + struct.pack(">hhh", ascent, -descent, gap) + bytes(26)
    os2 = bytes(62) + bytes(2) + bytes(4) + struct.pack(">hhhHH", 880, -120, 0, win_ascent, win_descent) + bytes(20)
    tables = [(b"OS/2", os2), (b"head", head), (b"hhea", hhea)]
    start = 12 + 16 * len(tables)
    directory, body = b"", b""
    for tag, data in tables:
        directory += struct.pack(">4sIII", tag, 0, start + len(body), len(data))
        body += data
    with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as f:
        f.write(struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0) + directory + body)
    test.addCleanup(os.remove, f.name)
    return f.name


class HostPage:
    """What TextPlusTools needs from the page it lives in (Transcribe's, in Buddy)."""

    def __init__(self, host):
        self.host, self.tab = host, "layout"
        self.sent, self.logs = [], []

    def emit(self, name, payload=None):
        self.sent.append((name, payload))

    def isVisible(self):
        return True

    def _add_log(self, text, kind="info"):
        self.logs.append((text, kind))


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
        self.assertTrue(o.set("style_scope", "selected"))                  # kept under a key of its own
        self.assertEqual((s["font_style_scope"], o.style_scope), ("selected", "selected"))
        self.assertTrue(o.set("anim_speed", "Medium"))
        self.assertEqual(s["anim_speed"], 1)
        for bad in (("font_size", "big"), ("outline_color", "red"), ("style_scope", "all"), ("nope", 1),
                    ("anim_scope", "tracks"), ("layout_track", "x"),
                    ("grid_type", "Hexagons")):
            self.assertFalse(o.set(*bad), bad)
        self.assertTrue(o.set("grid_type", "Rule of Thirds"))
        self.assertEqual(o.overlay_view()["grid_type"], "Rule of Thirds")

    def test_each_tab_has_its_own_apply_to(self):
        o = opts.Options(Mem())
        self.assertEqual({o.view()[k] for k in ("style_scope", "anim_scope", "layout_scope")}, {"timeline"})
        self.assertTrue(o.set("anim_scope", "track"))
        self.assertTrue(o.set("layout_scope", "selected"))
        self.assertTrue(o.set("layout_track", 4))
        v = o.view()
        self.assertEqual((v["style_scope"], v["anim_scope"], v["layout_scope"], v["tracks"]["layout_track"]),
                         ("timeline", "track", "selected", 4))

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

    def test_sizes_follow_the_text_plus_rule(self):
        """Text+ makes a font's ascent + descent TEXT_PLUS_HEIGHT x Size x width (measured
        in Resolve on 7 fonts), and the canvas box, the bounding fit and word widths all
        use it - so they agree with each other, whatever fonts this machine has."""
        from PySide6.QtGui import QFont, QFontMetricsF
        from pages.text_animator import canvas_math as cm
        font = QFont("Arial")
        font.setPixelSize(1000)
        metrics = QFontMetricsF(font)
        height = (metrics.ascent() + metrics.descent()) / 1000
        self.assertAlmostEqual(cm.text_box("Arial", "Hi", 0.1)["px"] * height, cm.TEXT_PLUS_HEIGHT * 0.1, places=6)
        size = cm.compute_bounding_fit_size("Arial", 0.09, "Because we're not\nno", 0.15, 0.85)
        self.assertAlmostEqual(cm._measure_word_width_fraction("Arial", "Because we're not\nno", size), 0.70, places=6)

    def test_standard_grid(self):
        from pages.text_animator.canvas_math import standard_grid_spacing
        self.assertAlmostEqual(standard_grid_spacing(1920, 1080), 120 / 1920)

    def use_font_files(self, files):
        from pages.text_animator import canvas_math as cm
        saved = dict(cm._font_files)

        def restore():
            cm._font_files.clear()
            cm._font_files.update(saved)
            cm._px_per_size.cache_clear()
            cm._text_metrics.cache_clear()
        self.addCleanup(restore)
        return cm.set_font_files(files)

    def test_resolves_font_file_decides_the_size(self):
        """Windows' Noto Sans JP: hhea 1.0 + 0.2 em, Windows metrics 1.16 + 0.288. Text+ goes
        by hhea - Resolve drew it 21% bigger than the Windows figures (Qt's) said."""
        from pages.text_animator import canvas_math as cm
        path = font_file(self, ascent=1000, descent=200, gap=0, win_ascent=1160, win_descent=288)
        self.assertTrue(self.use_font_files({"Arial": {"Bold": "C:/nowhere.ttf", "Regular": path}}))
        self.assertEqual(cm.font_vertical_metrics("Arial"), (1.0, 0.2, 0.0))
        self.assertAlmostEqual(cm.text_box("Arial", "Hi", 0.1)["px"], cm.TEXT_PLUS_HEIGHT * 0.1 / 1.2, places=9)
        self.assertFalse(cm.set_font_files({"Arial": {"Bold": "C:/nowhere.ttf", "Regular": path}}))   # unchanged

    def test_an_unreadable_font_file_falls_back_to_qt(self):
        from pages.text_animator import canvas_math as cm
        self.use_font_files({"Arial": {"Regular": __file__}})             # not a font
        ascent, descent, _gap = cm.font_vertical_metrics("Arial")
        self.assertGreater(ascent + descent, 0.9)

    def test_laid_out_around_the_center_like_text_plus(self):
        """Measured against Resolve's rendered bounds (to 1-2 px on 11 clips): the lines'
        block - ascent + descent, and a line step per further line - is centred on Center,
        each line centred on its own width."""
        from pages.text_animator import canvas_math as cm
        self.use_font_files({"Arial": {"Regular": font_file(self, 1000, 200, 0, 1160, 288)}})
        one = cm.text_box("Arial", "Hello", 0.1)
        px = one["px"]
        self.assertAlmostEqual(one["lines"][0][1], 0.4 * px, places=9)        # baseline below the line box's middle
        self.assertAlmostEqual(one["lines"][0][0], -cm._measure_word_width_fraction("Arial", "Hello", 0.1) / 2, places=9)
        two = cm.text_box("Arial", "Hello\nHi", 0.1)
        self.assertAlmostEqual(two["lines"][0][1], -0.2 * px, places=9)
        self.assertAlmostEqual(two["lines"][1][1] - two["lines"][0][1], 1.2 * px, places=9)
        self.assertGreater(two["lines"][1][0], two["lines"][0][0])             # the shorter line, centred


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from pages.text_animator.text_plus import TextPlusTools
        self.host = Host()
        self.host_page = HostPage(self.host)
        self.page = TextPlusTools(self.host_page)
        self.addCleanup(self.page.deleteLater)
        self.events = self.host_page.sent
        self.page._refresh_live_preview(force=True)

    def last(self, name):
        # The Text+ script's messages are "tp_"-prefixed; toast and alert are the page's own.
        name = name if name in ("toast", "alert") else f"tp_{name}"
        return [p for n, p in self.events if n == name][-1]

    def show(self, tab):
        self.host_page.tab = tab
        self.page.tab_shown()

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
        self.assertFalse([e for e in self.events if e[0] == "tp_canvas"])  # nothing changed, nothing sent

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

    def select(self, *clips):
        self.host.timeline.selected = list(clips)

    def clip(self, track, index=0):
        return self.host.timeline.tracks[track][index]

    def test_apply_to_finds_the_clips(self):
        timeline = self.host.timeline
        audio = Clip("Music", 86400, 90000, None)
        audio.kind, audio.track = "audio", 1
        self.select(self.clip(1, 1), audio, self.clip(0))                   # audio holds no Text+: left out
        names = lambda scope, track=1: [c.GetName() for c, _t in self.page._scope_clips(timeline, scope, track)[0]]
        self.assertEqual(names("selected"), ["Title", "Later"])             # track order
        self.assertEqual(names("track", 2), ["Hello", "Later"])
        self.assertEqual(sorted(names("playhead")), ["Hello", "Title", "World"])
        self.assertEqual(len(names("timeline")), 4)

    def test_selected_clips_needs_a_resolve_that_says(self):
        del Timeline.GetSelectedClips                                        # Resolve before 21.0.4
        self.addCleanup(setattr, Timeline, "GetSelectedClips", lambda s: list(s.selected))
        self.page.on_set({"name": "style_scope", "value": "selected"})
        self.page.on_apply_style()
        self.assertEqual(self.last("toast")["text"], "Selected clips needs DaVinci Resolve 21.0.4 or later.")
        self.assertEqual(self.host.busy, [])                                 # nothing started

    def test_style_goes_to_the_selected_clips_only(self):
        self.page.on_set({"name": "font_size", "value": 0.2})
        self.page.on_set({"name": "style_scope", "value": "selected"})
        self.page.on_apply_style()
        self.assertEqual(self.last("toast")["text"], "No clips selected on the timeline")
        self.select(self.clip(1, 1))                                         # just "Later"
        self.page.on_apply_style()
        self.assertEqual((self.tool(1, 1).GetInput("Size"), self.tool(1).GetInput("Size")), (0.2, 0.06))

    def test_apply_position_and_bounding_follow_apply_to(self):
        self.page.on_set({"name": "layout_scope", "value": "playhead"})      # it copies FROM those
        self.page.on_apply_position()
        self.assertIn("choose another Apply to", self.last("toast")["text"])
        self.assertEqual(self.tool(1, 1).center(), (0.5, 0.5))
        self.page.on_set({"name": "layout_scope", "value": "selected"})
        self.select(self.clip(1, 1))
        self.page.on_apply_position()
        self.assertEqual(self.tool(1, 1).center(), (0.3, 0.3))              # "Later" follows "Hello"
        self.page.on_bounding({"on": True, "left": 0.2, "right": 0.8})
        self.page.on_apply_bounding()
        self.assertEqual(self.tool(1, 1).center()[0], 0.5)                  # the selected one, fitted
        self.assertEqual(self.tool(0).center()[0], 0.5)                     # (already centred)
        self.assertEqual(self.tool(0).GetInput("Size"), 0.1)                # not selected: untouched

    def test_word_layout_uses_the_selected_hero(self):
        self.show("words")
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

    def test_style_preview_mirrors_the_text_under_the_playhead(self):
        def sent(name):
            return [p for n, p in self.events if n == f"tp_{name}"]

        self.show("style")
        self.assertEqual(self.last("state")["sample"], "Big Title")       # the lowest track's clip
        self.tool(0).inputs["StyledText"] = "Two\nlines"
        self.events.clear()
        self.page._refresh_live_preview(force=True)
        self.assertEqual(self.last("state")["sample"], "Two\nlines")      # every line, breaks kept
        self.assertFalse(sent("canvas"))                                  # no canvas on this tab
        self.events.clear()
        self.page._refresh_live_preview(force=True)
        self.assertFalse(sent("state"))                                   # unchanged: nothing sent
        self.host.timeline.playhead = "01:00:08:10"                       # only "Later" is here
        self.page._refresh_live_preview(force=True)
        self.assertEqual(self.last("state")["sample"], "Later")
        self.events.clear()
        self.host.timeline.playhead = "01:00:30:00"                       # nothing is here
        self.page._refresh_live_preview(force=True)
        self.assertFalse(sent("state"))                                   # the last text stays

    def test_the_poll_keeps_the_timeline_shape(self):
        timeline = self.host.timeline
        vertical = {"timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920"}
        timeline.GetSetting = lambda key: vertical.get(key)            # switched to a vertical timeline
        self.show("style")
        self.assertEqual(self.last("state")["resolution"], [1080, 1920])
        self.events.clear()
        timeline.GetSetting = lambda key: None                         # a read that failed
        self.page._refresh_live_preview(force=True)
        self.assertEqual(self.page.resolution, (1080, 1920))           # not flipped back to 16:9
        self.assertFalse([p for n, p in self.events if n == "tp_state"])

    def test_timeline_sync(self):
        self.page.on_shown()
        s = self.last("state")
        self.assertEqual((s["timeline"], s["tracks"]["video"], s["resolution"]), ("Promo", 3, [1920, 1080]))

    def test_nothing_is_polled_off_its_tabs(self):
        self.host_page.tab = "subtitles"                                  # one of Transcribe's own
        self.host.timeline.playhead = "01:00:08:10"
        self.events.clear()
        self.page._refresh_live_preview(force=True)
        self.assertFalse(self.events)

    def test_log_lines_join_the_pages_log(self):
        self.page.log("[Error] No active timeline found.")
        self.page.log("Styled 3 clips")
        self.assertEqual(self.host_page.logs[-2:], [("[Error] No active timeline found.", "error"),
                                                    ("Styled 3 clips", "info")])
        self.page.on_undo()                                               # nothing to undo: a toast
        self.assertEqual(self.last("toast"), {"text": "Nothing to undo"})

    def test_fonts_are_measured_from_the_files_resolve_uses(self):
        from types import SimpleNamespace
        from pages.text_animator import canvas_math as cm
        saved = dict(cm._font_files)
        self.addCleanup(lambda: (cm._font_files.clear(), cm._font_files.update(saved),
                                 cm._px_per_size.cache_clear(), cm._text_metrics.cache_clear()))
        path = font_file(self, 1000, 200, 0, 1160, 288)
        fonts = SimpleNamespace(GetFontList=lambda: {"Arial": {"Regular": path}})
        self.host.controller.resolve.Fusion = lambda: SimpleNamespace(FontManager=fonts)
        def layout_px():
            canvas = [p for n, p in self.events if n == "tp_canvas" and p["tab"] == "layout"][-1]
            return next(i["box"]["px"] for i in canvas["items"] if i["text"] == "Hello")

        before = layout_px()
        self.page.on_refresh()                                            # Refresh reads them again
        self.assertNotAlmostEqual(layout_px(), before, places=4)          # the canvas redrawn to Resolve's size
        self.assertAlmostEqual(self.last("options")["font_css"]["height"], 1.2)   # and the style preview


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AnimationPageTests(unittest.TestCase):
    def test_the_animation_page_keeps_the_old_id(self):
        from pages.text_animator.page import AnimationPage
        from pages.text_animator.text_plus import TABS
        self.assertEqual(list(TABS), ["style", "layout", "animation", "words"])
        self.assertEqual(AnimationPage.display_name, "Animation")
        self.assertEqual(AnimationPage.tool_id, "text_animator")          # saved settings and sidebar keep working


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SourceFramesTests(unittest.TestCase):
    def test_lengths_in_the_templates_own_frames(self):
        """Measured in Resolve 21: n frames of the 24 fps Text+ template last floor(n x 30/24)
        frames on a 30 fps timeline - so each length maps to the n that lands exactly, or one
        frame short where no whole n does (never long: that pushes the next subtitle)."""
        from pages.text_animator.subtitle_engine import source_frames_for
        landed = lambda n: int(n * 30 / 24)
        for want, n, got in [(10, 8, 10), (11, 9, 11), (29, 23, 28), (31, 25, 31), (33, 27, 33),
                             (39, 31, 38), (40, 32, 40), (90, 72, 90), (149, 119, 148), (151, 121, 151)]:
            self.assertEqual(source_frames_for(want, 24.0, 30.0), n, want)
            self.assertEqual(landed(n), got, want)
        self.assertEqual(source_frames_for(90, 30.0, 30.0), 90)          # same rate: unchanged
        self.assertEqual(source_frames_for(90, 24.0, 23.976), 91)       # 24 -> 23.976: just longer
        self.assertEqual(source_frames_for(1, 24.0, 60.0), 1)


if __name__ == "__main__":
    unittest.main()
