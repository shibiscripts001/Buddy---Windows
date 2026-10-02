"""Animation > Previews > Animate by: a preset played line by line, word by
word or letter by letter (pages/text_animator/units.py), and putting it on a
Text+ through a text Follower (motion_resolve.apply_units) - against the same
fake Fusion as test_motion_presets, wired the way Studio 21.1 did when driven
live (2026-10-02): the Follower's Order on Manual Curve (6) with one delay
key per character, the unit's Size/Angle/Offset inputs keyed, and each
shading element the Text+ shows faded with it."""

import unittest

import _paths  # noqa: F401
from pages.text_animator import motion, motion_resolve as mr, units as un
from test_motion_presets import PRESETS, Comp, Item, chain


# -------------------------------------------------------------- the timing --

class Units(unittest.TestCase):
    def test_words_lines_and_letters(self):
        text = "a extraordinarily b c"
        self.assertEqual(un.count(text, un.WORDS), 4)
        self.assertEqual(un.count(text, un.LETTERS), 18)
        self.assertEqual(un.count("Short\nA much longer line\nEnd", un.LINES), 3)
        self.assertEqual(un.count("one\n\n  \ntwo", un.LINES), 2)        # a blank line is no unit
        self.assertEqual(un.count("", un.WORDS), 0)

    def test_a_space_takes_the_next_units_delay(self):
        marks = un.unit_of("a bb", un.WORDS)
        self.assertEqual(marks, [0, 1, 1, 1])
        self.assertEqual(un.unit_of("  hi ", un.WORDS), [0, 0, 0, 0, 0])   # leading: the first, trailing: the last
        self.assertEqual(un.unit_of("ab\ncd", un.LINES), [0, 0, 1, 1, 1])

    def test_words_start_evenly_whatever_their_length(self):
        # What Resolve showed: Delay alone counts characters, so "b" waited
        # for all of "extraordinarily". The curve gives every letter of a
        # word the word's delay.
        text = "a extraordinarily b c"
        delays = un.delays(text, un.WORDS, un.FORWARD, 6.0)
        starts = sorted({d for ch, d in zip(text, delays) if ch != " "})
        self.assertEqual(starts, [0.0, 6.0, 12.0, 18.0])
        word = [d for ch, d in zip(text, delays)][2:17]
        self.assertEqual(set(word), {6.0})

    def test_lines_count_their_line_break(self):
        text = "Short\nA much much longer second line\nEnd"
        delays = un.delays(text, un.LINES, un.FORWARD, 6.0)
        self.assertEqual(len(delays), len(text))
        self.assertEqual(delays[text.index("\n")], 6.0)                  # the break: the next line's
        self.assertEqual(delays[-1], 12.0)

    def test_orders(self):
        self.assertEqual(un.ranks(4, un.FORWARD), [0, 1, 2, 3])
        self.assertEqual(un.ranks(4, un.REVERSE), [3, 2, 1, 0])
        self.assertEqual(un.ranks(5, un.MIDDLE), [2, 1, 0, 1, 2])
        self.assertEqual(un.ranks(4, un.MIDDLE), [1, 0, 0, 1])           # the two middle ones together
        self.assertEqual(un.ranks(5, un.EDGES), [0, 1, 2, 1, 0])
        shuffled = un.ranks(12, un.RANDOM, seed=3)
        self.assertEqual(sorted(shuffled), list(range(12)))
        self.assertEqual(shuffled, un.ranks(12, un.RANDOM, seed=3))     # the same every Apply
        self.assertNotEqual(shuffled, list(range(12)))

    def test_a_long_stagger_shrinks_to_fit_the_clip(self):
        delays = un.delays("one two three four five six", un.WORDS, un.FORWARD, 10.0)   # last word at 50
        fitted = un.fitted(delays, 41)
        self.assertAlmostEqual(max(fitted), 20.0)                       # half the clip at most
        self.assertAlmostEqual(fitted[4] / fitted[-1], delays[4] / delays[-1])   # all alike
        self.assertEqual(un.fitted([0.0, 3.0], 120), [0.0, 3.0])          # room enough: as it was

    def test_the_keys_run_short_by_the_longest_delay(self):
        self.assertEqual(un.plan_frames(120, [0.0, 5.5, 11.0]), 109)
        self.assertEqual(un.plan_frames(3, [0.0, 10.0]), 2)

    def test_what_comes_from_settings_is_made_safe(self):
        self.assertEqual(un.clean_options({}), {"unit": "clip", "order": "forward", "stagger": 0.08})
        self.assertEqual(un.clean_options({"unit": "words", "order": "edges", "stagger": "0.2"}),
                         {"unit": "words", "order": "edges", "stagger": 0.2})
        self.assertEqual(un.clean_options({"unit": "x", "order": 3, "stagger": 0.07}),
                         {"unit": "clip", "order": "forward", "stagger": 0.08})
        self.assertEqual(un.clean_options(None)["unit"], "clip")


# ---------------------------------------------------------------- on clips --

def text_clip(text="a extraordinarily b c", elements=(1,), **inspector):
    comp = Comp(upstream="TextPlus")
    tp = comp.FindTool("TextPlus1")
    tp.SetInput("StyledText", text)
    for n in range(1, 9):
        tp.SetInput(f"Enabled{n}", 1.0 if n in elements else 0.0)
    return Item(comp, name="Text+", **inspector)


def text_tool(item):
    return mr.text_plus(item.comps[-1])


def spline(inp):
    return inp.source.tool.keyframes if inp.source else None


WORDS = {"unit": "words", "order": "forward", "stagger": 0.2}       # 4.8 frames at 24 fps
LAST_START = 14.4                    # the 4th word's delay


class OnTextPlus(unittest.TestCase):
    def put(self, item, pid="pop", choice=WORDS, clip_frames=120):
        planned = []

        def plan_for(frames, ratio, a):
            planned.append(frames)
            return motion.plan(PRESETS[pid], frames, 24 * ratio, at=a)
        done = mr.apply_units(item, pid, plan_for, clip_frames, 24.0, un.clean_options(choice),
                              options={"way": "both", **choice})
        return done, planned

    def test_a_follower_goes_on_with_a_delay_per_character(self):
        item = text_clip()
        done, _planned = self.put(item)
        self.assertTrue(done)
        tp = text_tool(item)
        fol = mr.follower(tp)
        self.assertEqual(fol.reg_id, "StyledTextFollower")
        self.assertEqual(fol.Text.value, "a extraordinarily b c")
        self.assertEqual(fol.Order.value, 6)                              # Manual Curve
        self.assertEqual(fol.Delay.value, 1.0)
        delays = {f: k[1] for f, k in spline(fol.DelayByCharacterPosition).items()}
        expected = un.delays("a extraordinarily b c", "words", "forward", 4.8)
        self.assertEqual(sorted(delays), [float(i) for i in range(len(expected))])   # a key per character
        for i, d in enumerate(expected):
            self.assertAlmostEqual(delays[float(i)], d)
        self.assertEqual(item.reloads, 1)
        self.assertEqual(item.comps[-1].locked, 0)
        self.assertEqual(chain(item.comps[-1]), ["MediaOut1", "TextPlus1"])   # no Merge
        self.assertIsNone(item.comps[-1].FindTool("BuddyMotion"))

    def test_the_preset_moves_each_word_never_from_nothing(self):
        item = text_clip()
        self.put(item, "pop")
        fol = mr.follower(text_tool(item))
        for axis in ("WordSizeX", "WordSizeY"):
            keys = spline(getattr(fol, axis))
            self.assertTrue(keys)
            self.assertGreaterEqual(min(k[1] for k in keys.values()), mr.MIN_SIZE)    # Pop starts at 0
        self.assertIsNone(fol.inputs.get("CharacterSizeX"))              # the word's own inputs only
        record = mr._units_record(text_tool(item))
        self.assertEqual(record["preset"], "pop")
        self.assertIn("WordSizeX", record["keyed"])

    def test_lines_and_letters_use_their_own_inputs(self):
        item = text_clip("one\ntwo")
        self.put(item, "pop", {**WORDS, "unit": "lines"})
        self.assertTrue(spline(mr.follower(text_tool(item)).LineSizeX))
        item = text_clip("abc")
        self.put(item, "pop", {**WORDS, "unit": "letters"})
        self.assertTrue(spline(mr.follower(text_tool(item)).CharacterSizeY))

    def test_a_fade_takes_the_outline_and_shadow_with_it(self):
        item = text_clip(elements=(1, 2, 3))
        self.put(item, "css-animate-zoomIn-card")                         # fades as it grows
        fol = mr.follower(text_tool(item))
        for n in (1, 2, 3):
            self.assertEqual(fol.inputs[f"Enabled{n}"].value, 1)
            self.assertTrue(spline(fol.inputs[f"Opacity{n}"]))
        self.assertIsNone(fol.inputs.get("Opacity4"))                     # not shown: left alone

    def test_a_move_goes_through_a_path_on_the_offset(self):
        item = text_clip()
        self.put(item, "slide")
        fol = mr.follower(text_tool(item))
        path = fol.WordOffset.source.tool
        self.assertEqual(path.reg_id, "XYPath")
        # An offset rests at 0 where the Merge's Center rests at 0.5.
        planned = motion.plan(PRESETS["slide"], 120 - 15, 24)
        x = {f: k[1] for f, k in spline(path.X).items()}
        self.assertEqual(x, {f: k["value"] - 0.5 for f, k in planned["Center.X"].items()})
        self.assertEqual({k[1] for k in spline(path.Y).values()}, {0.0})  # Slide doesn't rise

    def test_the_last_word_still_ends_on_the_last_frame(self):
        item = text_clip()
        _done, planned = self.put(item, "pop", clip_frames=120)
        self.assertEqual(planned, [120 - 15])                             # the last word starts 14.4 late
        keys = spline(mr.follower(text_tool(item)).WordSizeX)
        self.assertAlmostEqual(max(keys) + LAST_START, 119, delta=1)

    def test_applying_again_replaces_instead_of_stacking(self):
        item = text_clip()
        self.put(item, "slide")
        self.put(item, "pop")
        comp = item.comps[-1]
        self.assertEqual(len(comp.GetToolList(False, "StyledTextFollower")), 1)
        self.assertEqual(len(comp.GetToolList(False, "XYPath")), 0)        # Pop doesn't move
        # The delay curve and the two sizes: nothing left over from Slide.
        self.assertEqual(len(comp.GetToolList(False, "BezierSpline")), 3)
        self.assertEqual(mr.follower(text_tool(item)).Text.value, "a extraordinarily b c")

    def test_remove_puts_the_words_back_on_the_text_plus(self):
        item = text_clip(elements=(1, 2))
        self.put(item, "slide")
        self.assertTrue(mr.remove(item))
        comp = item.comps[-1]
        tp = text_tool(item)
        self.assertIsNone(mr.follower(tp))
        self.assertEqual(tp.StyledText.value, "a extraordinarily b c")
        self.assertEqual(sorted(t.reg_id for t in comp.tools.values()), ["MediaOut", "TextPlus"])
        self.assertIsNone(mr._units_record(tp))
        self.assertFalse(mr.remove(item))

    def test_the_whole_clip_and_animate_by_never_stack(self):
        item = text_clip()
        self.put(item, "pop")
        mr.apply(item, "pop", lambda n, r, a: motion.plan(PRESETS["pop"], n, 24 * r, at=a), 120,
                 None, (1920, 1080), mr.FIT, mr.read_inspector(item), {"way": "both"})
        comp = item.comps[-1]
        self.assertIsNone(mr.follower(text_tool(item)))
        self.assertEqual(chain(comp), ["MediaOut1", "BuddyMotion", "TextPlus1"])
        self.put(item, "pop")                                             # and back
        comp = item.comps[-1]
        self.assertIsNone(comp.FindTool("BuddyMotion"))
        self.assertTrue(mr.follower(text_tool(item)))

    def test_taking_the_whole_clip_preset_off_puts_its_framing_back(self):
        item = text_clip(Pan=120.0)
        mr.apply(item, "pop", lambda n, r, a: motion.plan(PRESETS["pop"], n, 24 * r, at=a), 120,
                 None, (1920, 1080), mr.FIT, mr.read_inspector(item), {"way": "both"})
        self.assertEqual(item.props["Pan"], 0.0)                          # in the comp
        self.put(item, "pop")
        self.assertAlmostEqual(item.props["Pan"], 120.0)                  # back in the Inspector

    def test_a_follower_of_someone_elses_is_left_alone(self):
        item = text_clip()
        tp = text_tool(item)
        tp.AddModifier("StyledText", "StyledTextFollower")
        with self.assertRaises(RuntimeError):
            self.put(item)
        self.assertEqual(item.reloads, 0)
        self.assertFalse(mr.remove(item))

    def test_a_clip_that_isnt_a_text_plus_is_turned_back_untouched(self):
        still = Item()
        done, _planned = self.put(still)
        self.assertFalse(done)
        self.assertEqual(still.comps, [])
        two = Comp(upstream="TextPlus")
        two.AddTool("TextPlus")
        self.assertFalse(self.put(Item(two))[0])

    def test_no_words_no_animation(self):
        with self.assertRaises(RuntimeError):
            self.put(text_clip("   "))

    def test_a_failed_change_leaves_the_clip_as_it_was(self):
        item = text_clip()
        real = mr._build_units

        def broken(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("Resolve said no")
        mr._build_units = broken
        try:
            with self.assertRaises(RuntimeError):
                self.put(item)
        finally:
            mr._build_units = real
        tp = text_tool(item)
        self.assertIsNone(mr.follower(tp))
        self.assertEqual(tp.StyledText.value, "a extraordinarily b c")


# ------------------------------------------------------------- the jobs --

class Timeline:
    def __init__(self, *clips):
        self.clips = list(clips)

    def GetName(self): return "T"
    def GetSelectedClips(self): return list(self.clips)
    def GetCurrentTimecode(self): return "00:00:00:00"

    def GetSetting(self, key):
        return {"timelineFrameRate": "24", "timelineResolutionWidth": "1920",
                "timelineResolutionHeight": "1080"}.get(key, "")


def on_timeline(item, start=1000, frames=120):
    item.GetTrackTypeAndIndex = lambda: ("video", 1)
    item.GetStart = lambda: start
    item.GetEnd = lambda: start + frames
    item.GetDuration = lambda: frames
    return item


def controller(timeline):
    project = type("Project", (), {"GetCurrentTimeline": lambda self: timeline,
                                   "GetSetting": lambda self, key: ""})()
    return type("Controller", (), {"current_project": lambda self: project})()


class Jobs(unittest.TestCase):
    def test_text_plus_by_word_and_the_rest_as_a_whole(self):
        text = on_timeline(text_clip())
        still = on_timeline(Item(name="still"))
        pool = type("Pool", (), {"GetClipProperty": lambda self, key: "Still"})()
        still.GetMediaPoolItem = lambda: pool
        result = mr.run_apply(controller(Timeline(text, still)), PRESETS["pop"],
                              lambda n, fps, a: motion.plan(PRESETS["pop"], n, fps, at=a),
                              options={"way": "both", "speed": 1.0, **WORDS})
        self.assertEqual((result["applied"], result["whole"], result["failed"]), (2, 1, []))
        self.assertTrue(mr.follower(text_tool(text)))
        self.assertEqual(chain(still.comps[-1]), ["MediaOut1", "BuddyMotion", "MediaIn1"])

    def test_clip_is_as_before(self):
        text = on_timeline(text_clip())
        result = mr.run_apply(controller(Timeline(text)), PRESETS["pop"],
                              lambda n, fps, a: motion.plan(PRESETS["pop"], n, fps, at=a),
                              options={"way": "both", "speed": 1.0})
        self.assertEqual((result["applied"], result["whole"]), (1, 0))
        self.assertEqual(chain(text.comps[-1]), ["MediaOut1", "BuddyMotion", "TextPlus1"])

    def test_the_summary_counts_it_animated_and_never_reframed(self):
        text = on_timeline(text_clip(Pan=50.0))
        mr.run_apply(controller(Timeline(text)), PRESETS["pop"],
                     lambda n, fps, a: motion.plan(PRESETS["pop"], n, fps, at=a), options=WORDS)
        s = mr.summary(controller(Timeline(text)))
        self.assertEqual((s["animated"], s["reframed"]), (1, 0))          # its Pan stayed in the Inspector
        self.assertEqual(text.props["Pan"], 50.0)
        removed = mr.run_remove(controller(Timeline(text)))
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(mr.summary(controller(Timeline(text)))["animated"], 0)


if __name__ == "__main__":
    unittest.main()
