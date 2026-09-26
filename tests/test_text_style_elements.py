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


if __name__ == "__main__":
    unittest.main()
