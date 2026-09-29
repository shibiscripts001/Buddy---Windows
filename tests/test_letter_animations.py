"""The letter-by-letter Text+ animations (Typewriter, Letter Fade, Letter Pop) on
Fusion's text Follower, against a fake comp that behaves the way Studio 21.1 did
when driven live: only the registry ID "StyledTextFollower" attaches a Follower,
the words move onto it, and the Text+ reads them back through the connection."""

import unittest

import _paths  # noqa: F401
from pages.text_animator import options
from pages.text_animator.animation_engine import FusionAnimationEngine as E


class Output:
    def __init__(self, tool, name):
        self.tool, self.Name = tool, name

    def GetTool(self):
        return self.tool


class Input:
    def __init__(self, tool, name, value=None):
        self.tool, self.name, self.value, self.source = tool, name, value, None

    def ConnectTo(self, output):
        self.source = output
        return True

    def GetConnectedOutput(self):
        return self.source

    def __setitem__(self, time, value):   # a keyframe, on the spline it's connected to
        self.source.tool.keys[time] = value


class Tool:
    def __init__(self, comp, tool_id, name, **values):
        self.comp, self.ID, self.Name = comp, tool_id, name
        self.inputs = {k: Input(self, k, v) for k, v in values.items()}
        self.keys = {}
        self.Value = Output(self, "Value")
        self.StyledTextOut = Output(self, "Styled Text")

    def __getattr__(self, name):
        if name[0].isupper():
            return self.inputs.setdefault(name, Input(self, name))
        raise AttributeError(name)

    def GetInput(self, name):
        inp = self.inputs.get(name)
        if inp is None:
            return None
        if inp.source is not None and inp.source.tool.ID == "StyledTextFollower":
            return inp.source.tool.GetInput("Text")
        return inp.value

    def SetInput(self, name, value, time=None):
        self.inputs.setdefault(name, Input(self, name)).value = value

    def AddModifier(self, input_name, modifier):
        if modifier != "StyledTextFollower":
            return False   # "Follower" attaches nothing (measured)
        follower = self.comp.add("StyledTextFollower", "Follower1", Text=self.GetInput(input_name), Delay=0.0)
        self.inputs[input_name].ConnectTo(follower.StyledTextOut)
        return True

    def SaveSettings(self):
        return {}

    def Delete(self):
        self.comp.tools.remove(self)


class Comp:
    def __init__(self, text="HELLO WORLD"):
        self.tools = []
        self.text_plus = self.add("TextPlus", "Template", StyledText=text)

    def add(self, tool_id, name, **values):
        tool = Tool(self, tool_id, name, **values)
        self.tools.append(tool)
        return tool

    def AddTool(self, tool_id):
        return self.add(tool_id, f"{tool_id}{len(self.tools)}")

    def ids(self):
        return sorted(t.ID for t in self.tools)


class LetterAnimationTests(unittest.TestCase):
    def test_typewriter_attaches_the_follower_by_its_registry_id(self):
        comp = Comp()
        ok, logs = E.apply_letter_preset_to_clip(comp.text_plus, comp, "Typewriter (Letters)", speed="Medium")
        self.assertTrue(ok, logs)
        follower = E.follower_of(comp.text_plus)
        self.assertIsNotNone(follower)
        self.assertEqual(follower.GetInput("Text"), "HELLO WORLD")      # the words live on it now
        self.assertEqual(comp.text_plus.GetInput("StyledText"), "HELLO WORLD")
        self.assertEqual(follower.GetInput("Delay"), 2.0)
        spline = follower.Opacity1.GetConnectedOutput().GetTool()
        self.assertEqual(spline.keys, {0: 0.0, 1: 1.0, 2: 1.0})          # each letter just appears

    def test_letter_fade_and_pop(self):
        comp = Comp()
        self.assertTrue(E.apply_letter_preset_to_clip(comp.text_plus, comp, "Letter Fade (Letters)", "Slow")[0])
        keys = E.follower_of(comp.text_plus).Opacity1.GetConnectedOutput().GetTool().keys
        self.assertEqual(keys[0], 0.0)
        self.assertEqual(max(keys.values()), 1.0)
        E.remove_animations_from_clip(comp.text_plus, comp)
        self.assertTrue(E.apply_letter_preset_to_clip(comp.text_plus, comp, "Letter Pop (Letters)", "Fast")[0])
        follower = E.follower_of(comp.text_plus)
        x = follower.CharacterSizeX.GetConnectedOutput().GetTool().keys
        y = follower.CharacterSizeY.GetConnectedOutput().GetTool().keys
        self.assertEqual(x, y)                                          # grows in proportion, not thin
        self.assertEqual(x[min(x)], E._START_SCALE)
        self.assertEqual(x[max(x)], 1.0)
        self.assertGreater(max(x.values()), 1.0)                        # the little overshoot

    def test_removing_puts_the_text_back_and_leaves_nothing_behind(self):
        comp = Comp()
        E.apply_letter_preset_to_clip(comp.text_plus, comp, "Letter Pop (Letters)")
        self.assertEqual(comp.ids(), ["BezierSpline", "BezierSpline", "StyledTextFollower", "TextPlus"])
        ok, _logs = E.remove_animations_from_clip(comp.text_plus, comp)
        self.assertTrue(ok)
        self.assertEqual(comp.ids(), ["TextPlus"])
        self.assertIsNone(comp.text_plus.StyledText.GetConnectedOutput())
        self.assertEqual(comp.text_plus.GetInput("StyledText"), "HELLO WORLD")
        self.assertEqual(E.remove_follower(comp.text_plus), (False, []))   # nothing there now

    def test_switching_presets_keeps_one_follower(self):
        comp = Comp()
        for preset in E.LETTER_PRESETS + E.LETTER_PRESETS:
            E.remove_animations_from_clip(comp.text_plus, comp)         # what the page does before Apply
            E.apply_letter_preset_to_clip(comp.text_plus, comp, preset)
        self.assertEqual(comp.ids().count("StyledTextFollower"), 1)
        self.assertEqual(comp.text_plus.GetInput("StyledText"), "HELLO WORLD")

    def test_a_follower_that_wont_attach_changes_nothing(self):
        comp = Comp()
        comp.text_plus.AddModifier = lambda *_a: False
        ok, logs = E.apply_letter_preset_to_clip(comp.text_plus, comp, "Typewriter (Letters)")
        self.assertFalse(ok)
        self.assertTrue(any("could not be attached" in line for line in logs))
        self.assertEqual(comp.ids(), ["TextPlus"])
        self.assertEqual(comp.text_plus.GetInput("StyledText"), "HELLO WORLD")

    def test_a_long_line_is_squeezed_to_arrive_in_time(self):
        self.assertEqual(E.letter_delay("HI", "Medium"), 2.0)            # the speed's own pace
        self.assertEqual(E.letter_delay("x" * 41, "Medium"), 0.5)        # 20 frames for the lot
        self.assertLess(E.letter_delay("x" * 41, "Fast"), E.letter_delay("x" * 41, "Slow"))
        self.assertEqual(E.letter_delay("", "Fast"), 1.0)

    def test_the_page_offers_them(self):
        for preset in E.LETTER_PRESETS:
            self.assertIn(preset, options.ANIM_PRESETS)


if __name__ == "__main__":
    unittest.main()
