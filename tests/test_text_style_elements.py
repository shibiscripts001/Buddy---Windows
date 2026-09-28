"""Text Animator's optional Text+ styles (Outline, Shadow, Background -
app/pages/text_animator/animation_engine.py): ticking one switches its
shading element on, unticking switches it off, with a fake Text+ tool."""

import unittest

import _paths  # noqa: F401
from pages.text_animator.animation_engine import FusionAnimationEngine as E


class FakeTextTool:
    def __init__(self):
        self.inputs = {}

    def SetInput(self, name, value):
        self.inputs[name] = value


class ShadingElementTests(unittest.TestCase):
    def test_apply_turns_each_element_on(self):
        tool = FakeTextTool()
        E.apply_outline_style(tool, color=(1, 0, 0), thickness=0.04, opacity=1.0)
        E.apply_shadow_style(tool, color=(0, 0, 0), offset=(0, -0.02), blur=5.0, opacity=0.75)
        E.apply_background_style(tool, color=(0, 0, 0), opacity=0.5)
        for element in (E.OUTLINE_ELEMENT, E.SHADOW_ELEMENT, E.BACKGROUND_ELEMENT):
            self.assertEqual(tool.inputs[f"Enabled{element}"], 1)

    def test_disable_turns_an_element_back_off(self):
        tool = FakeTextTool()
        E.apply_shadow_style(tool, opacity=0.75)
        ok, logs = E.disable_shading_element(tool, E.SHADOW_ELEMENT, "Shadow")
        self.assertTrue(ok)
        self.assertEqual(logs, [])
        self.assertEqual(tool.inputs["Enabled3"], 0)

    def test_disable_without_a_tool_reports_it(self):
        ok, logs = E.disable_shading_element(None, E.BACKGROUND_ELEMENT, "Background")
        self.assertFalse(ok)
        self.assertIn("Background", logs[0])


class GrowPresetTests(unittest.TestCase):
    """Pop and Bounce grow the text from a tiny scale, never 0: a Text+ at LayoutSize 0
    rendered the whole frame black over the video."""

    def keyframes(self, apply):
        seen = {}

        def spline(_tool, _comp, input_name, keyframes):
            seen[input_name] = keyframes
            return True, []
        original = E._create_and_connect_spline
        E._create_and_connect_spline = staticmethod(spline)
        try:
            ok, _logs = apply(FakeTextTool(), object())
        finally:
            E._create_and_connect_spline = original
        self.assertTrue(ok)
        return seen["LayoutSize"]

    def test_pop_and_bounce_start_just_above_zero(self):
        for apply in (E.apply_pop_preset_to_clip, E.apply_bounce_preset_to_clip):
            keys = self.keyframes(apply)
            first, last = keys[min(keys)], keys[max(keys)]
            self.assertGreater(first, 0, apply.__name__)
            self.assertLess(first, 0.01, apply.__name__)                   # still nothing to see
            self.assertEqual(last, 1.0, apply.__name__)


if __name__ == "__main__":
    unittest.main()
