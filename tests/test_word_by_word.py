"""Subtitles > Word-by-word: single-word Text+ clips set out as one Text+ clip would show
the sentence (text_animator/word_by_word.py), and Space words on the tab
(text_plus.on_space_words) against the fake Resolve of test_text_animator_page."""

import os
import unittest

import _paths  # noqa: F401

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

ASPECT = 16 / 9


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class LayoutTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from pages.text_animator import canvas_math, word_by_word
        self.cm, self.wbw = canvas_math, word_by_word

    def words(self, text, size=0.08, at=(0.5, 0.5), sizes=None):
        return [self.wbw.Word(i, t, "Arial", "Regular", (sizes or {}).get(i, size), 1.0, at[0], at[1])
                for i, t in enumerate(text.split())]

    def edges(self, placed, words):
        """Each line's left edge and baseline (frame fractions, the baseline's down the
        height), from where its words were put."""
        out = []
        for row in placed.lines:
            first = next(w for w in words if w.key == row[0])
            box = self.cm.text_box(first.font, first.text, first.size)
            cx, cy = placed.centers[first.key]
            out.append((cx + box["left"], cy + box["lines"][0][1] * ASPECT))
        return out

    def test_the_words_go_where_one_clip_would_draw_the_sentence(self):
        words = self.words("Happy young typing today")
        for lines in ("1", "2", "3"):
            with self.subTest(lines=lines):
                placed = self.wbw.layout(words, lines, ASPECT)
                self.assertEqual(len(placed.lines), int(lines))
                sentence = "\n".join(" ".join(words[k].text for k in row) for row in placed.lines)
                one = self.cm.text_box("Arial", sentence, 0.08)
                want = [(0.5 + x, 0.5 + y * ASPECT) for x, y in one["lines"]]
                for got, expected in zip(self.edges(placed, words), want):
                    self.assertAlmostEqual(got[0], expected[0], places=6)
                    self.assertAlmostEqual(got[1], expected[1], places=6)
                self.assertAlmostEqual(placed.widest, one["w"], places=6)

    def test_lines_are_as_even_as_they_can_be(self):
        placed = self.wbw.layout(self.words("a b c extraordinarily"), "2", ASPECT)
        self.assertEqual(placed.lines, [[0, 1, 2], [3]])

    def test_auto_uses_the_fewest_lines_that_fit(self):
        # Sized from the font's own measure (the test machine's fonts vary): two words a
        # line fit in FIT_WIDTH, all four don't.
        text = "wide wide wide wide"
        pair = self.cm.text_box("Arial", "wide wide", 1.0)["w"]
        small = self.wbw.layout(self.words(text, size=0.9 * self.wbw.FIT_WIDTH / (2.2 * pair)), "auto", ASPECT)
        self.assertEqual(len(small.lines), 1)
        big = self.wbw.layout(self.words(text, size=0.95 * self.wbw.FIT_WIDTH / pair), "auto", ASPECT)
        self.assertEqual(big.lines, [[0, 1], [2, 3]])
        self.assertLessEqual(big.widest, self.wbw.FIT_WIDTH)

    def test_never_more_lines_than_words(self):
        self.assertEqual(len(self.wbw.layout(self.words("Two words"), "3", ASPECT).lines), 2)

    def test_the_group_stays_where_it_was_and_twice_changes_nothing(self):
        words = self.words("Stay right here", at=(0.3, 0.8))
        placed = self.wbw.layout(words, "2", ASPECT)
        moved = [w._replace(cx=placed.centers[w.key][0], cy=placed.centers[w.key][1]) for w in words]
        again = self.wbw.layout(moved, "2", ASPECT)
        for key, (cx, cy) in placed.centers.items():
            self.assertAlmostEqual(again.centers[key][0], cx, places=9)
            self.assertAlmostEqual(again.centers[key][1], cy, places=9)
        lefts = [cx + self.cm.text_box("Arial", w.text, w.size)["left"] for w, (cx, _cy) in
                 ((w, placed.centers[w.key]) for w in words)]
        rights = [left + self.cm.text_box("Arial", w.text, w.size)["w"] for left, w in zip(lefts, words)]
        self.assertAlmostEqual((min(lefts) + max(rights)) / 2, 0.3, places=6)   # still centred on 0.3
        ys = [cy for _cx, cy in placed.centers.values()]
        self.assertAlmostEqual((min(ys) + max(ys)) / 2, 0.8, places=6)

    def test_a_bigger_word_shares_the_baseline(self):
        words = self.words("small BIG small", sizes={1: 0.16})
        placed = self.wbw.layout(words, "1", ASPECT)
        baselines = {round(placed.centers[w.key][1] + self.cm.text_box("Arial", w.text, w.size)["lines"][0][1] * ASPECT, 9)
                     for w in words}
        self.assertEqual(len(baselines), 1)

    def test_word_spacing_scales_the_gaps(self):
        words = self.words("Happy young typing")
        gaps = {}
        for spacing in (0.0, 1.0, 2.0):
            placed = self.wbw.layout(words, "1", ASPECT, spacing)
            box = [self.cm.text_box("Arial", w.text, w.size) for w in words]
            edges = [(placed.centers[w.key][0] + b["left"], placed.centers[w.key][0] + b["left"] + b["w"])
                     for w, b in zip(words, box)]
            gaps[spacing] = [edges[i + 1][0] - edges[i][1] for i in range(len(edges) - 1)]
        space = self.wbw._space(words[0])
        for gap in gaps[0.0]:
            self.assertAlmostEqual(gap, 0.0, places=9)                      # touching
        for one, two in zip(gaps[1.0], gaps[2.0]):
            self.assertAlmostEqual(one, space, places=9)                    # 1: a space, as one clip has it
            self.assertAlmostEqual(two, 2 * space, places=9)
        self.assertEqual(self.wbw.layout(words, "1", ASPECT).centers, self.wbw.layout(words, "1", ASPECT, 1.0).centers)

    def test_only_single_words(self):
        self.assertTrue(self.wbw.is_single_word(" Hello "))
        for text in ("Two words", "Line\nbreak", "", "   ", None):
            self.assertFalse(self.wbw.is_single_word(text), text)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SpaceWordsTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        from test_text_animator_page import Clip, Host, HostPage, Timeline, Tool
        from pages.text_animator import canvas_math as cm
        from pages.text_animator.text_plus import TextPlusTools
        saved = dict(cm._baselines)
        self.addCleanup(lambda: (cm._baselines.clear(), cm._baselines.update(saved)))
        s = 86400
        # "Hello" starts last but sits on the lowest track: it's read last. The title (two
        # words) and "Later" (alone at its time) are left alone; "one" and "more" are a
        # second group, later on.
        self.clips = {
            "Title": Clip("Title", s, s + 100, Tool("Big Title", (0.5, 0.8), 0.1)),
            "Hello": Clip("Hello", s + 8, s + 60, Tool("Hello", (0.5, 0.5), 0.08)),
            "big": Clip("big", s, s + 60, Tool("big", (0.5, 0.5), 0.08)),
            "world": Clip("world", s + 4, s + 60, Tool("world", (0.5, 0.5), 0.08)),
            "one": Clip("one", s + 200, s + 260, Tool("one", (0.5, 0.3), 0.08)),
            "more": Clip("more", s + 210, s + 260, Tool("more", (0.5, 0.3), 0.08)),
            "Later": Clip("Later", s + 400, s + 450, Tool("Later", (0.2, 0.2), 0.08)),
        }
        c = self.clips
        self.host = Host(Timeline([[c["Title"]], [c["Hello"], c["one"]], [c["big"], c["more"]],
                                   [c["world"], c["Later"]]]))
        self.host_page = HostPage(self.host)
        self.host_page.tab = "wordbyword"
        self.page = TextPlusTools(self.host_page)
        self.addCleanup(self.page.deleteLater)
        self.page._refresh_live_preview(force=True)

    def x(self, name):
        return self.clips[name].tool.center()[0]

    def y(self, name):
        return self.clips[name].tool.center()[1]

    def last(self, name):
        name = name if name in ("toast", "alert") else f"tp_{name}"
        return [p for n, p in self.host_page.sent if n == name][-1]

    def test_the_tab_sits_after_timeline_layout_with_its_own_preview(self):
        from pages.text_animator.text_plus import TABS
        self.assertEqual(list(TABS)[1:3], ["layout", "wordbyword"])
        self.assertEqual(self.last("canvas")["tab"], "wordbyword")
        self.assertEqual(sorted(i["text"] for i in self.last("canvas")["items"]), ["Big Title", "Hello", "big", "world"])

    def test_the_words_under_the_playhead_in_the_order_they_start(self):
        self.page.on_set({"name": "wbw_lines", "value": "1"})
        self.page.on_space_words({})
        self.assertLess(self.x("big"), self.x("world"))
        self.assertLess(self.x("world"), self.x("Hello"))
        self.assertAlmostEqual(self.y("big"), self.y("Hello"))                  # one line
        self.assertEqual(self.clips["Title"].tool.center(), (0.5, 0.8))         # not a single word
        self.assertEqual(self.last("history")["undo"], 1)                       # one undo step
        self.page.on_undo()
        self.assertEqual(self.clips["Hello"].tool.center(), (0.5, 0.5))

    def test_only_the_words_selected_in_the_preview(self):
        self.page.on_set({"name": "wbw_lines", "value": "1"})
        ids = {i["text"]: i["id"] for i in self.last("canvas")["items"]}
        self.page.on_space_words({"selected": [ids["big"], ids["Hello"]]})
        self.assertEqual(self.clips["world"].tool.center(), (0.5, 0.5))
        self.assertLess(self.x("big"), self.x("Hello"))

    def test_lines_are_the_tabs_choice(self):
        self.page.on_set({"name": "wbw_lines", "value": "3"})
        self.page.on_space_words({})
        self.assertGreater(self.y("big"), self.y("world"))                      # Fusion y is bottom-up
        self.assertGreater(self.y("world"), self.y("Hello"))
        self.assertIn("on 3 line(s)", self.last("toast")["text"])

    def test_every_group_on_the_timeline(self):
        self.page.on_set({"name": "wbw_scope", "value": "timeline"})
        self.page.on_set({"name": "wbw_lines", "value": "1"})
        self.page.on_space_words({})
        self.assertLess(self.x("big"), self.x("Hello"))
        self.assertLess(self.x("one"), self.x("more"))
        self.assertAlmostEqual(self.y("one"), 0.3)                              # each group where it was
        self.assertEqual(self.clips["Later"].tool.center(), (0.2, 0.2))         # alone at its time
        self.assertEqual(self.host.busy, [True, False])
        self.assertEqual(self.last("toast")["text"], "Spaced 2 groups of words")

    def test_fewer_than_two_words_is_said(self):
        self.host.timeline.playhead = "01:00:16:20"                            # only "Later" there
        self.page.on_space_words({})
        self.assertIn("at least 2", self.last("toast")["text"])
        self.assertEqual(self.clips["Later"].tool.center(), (0.2, 0.2))

    def test_the_word_spacing_slider_is_used(self):
        self.page.on_set({"name": "wbw_lines", "value": "1"})
        self.page.on_space_words({})
        normal = self.x("Hello") - self.x("big")
        self.page.on_set({"name": "wbw_spacing", "value": 2.0})
        self.page.on_space_words({})
        self.assertGreater(self.x("Hello") - self.x("big"), normal)
        spec = self.last("options")["sliders"]["wbw_spacing"]
        self.assertEqual((spec["min"], spec["max"], spec["default"], spec["value"]), (0.0, 2.0, 1.0, 2.0))

    def test_the_settings_are_checked(self):
        from pages.text_animator.options import Options
        from test_text_animator_page import Mem
        options = Options(Mem())
        self.assertEqual((options.wbw_lines, options.wbw_scope), ("auto", "playhead"))
        self.assertFalse(options.set("wbw_lines", "4"))
        self.assertFalse(options.set("wbw_scope", "track"))
        self.assertTrue(options.set("wbw_lines", "2"))
        self.assertEqual(options.view()["wbw_lines"], "2")


if __name__ == "__main__":
    unittest.main()
