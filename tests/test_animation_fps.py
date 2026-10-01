"""Preset durations stay in seconds when the timeline frame rate changes."""

import unittest

import _paths  # noqa: F401
from pages.text_animator.animation_engine import FusionAnimationEngine as E
from pages.text_animator import motion


class AnimationTiming(unittest.TestCase):
    def test_template_title_uses_its_comp_rate(self):
        class Comp:
            def GetAttrs(self):
                return {"COMPN_RenderStart": 12.0, "COMPN_RenderEnd": 91.0}

        # 80 source frames of Buddy's 24 fps title occupy 100 frames at 30 fps.
        self.assertAlmostEqual(E.comp_fps(Comp(), 100, 30), 24.0)
        self.assertEqual(E.scaled_duration(15, "Slow", E.comp_fps(Comp(), 100, 30)), 15)

    def test_text_plus_move_duration_tracks_timeline_fps(self):
        for base in (10, 12, 15, 20):
            for speed in E.SPEEDS:
                intended_seconds = base * E._SPEED_DURATION_SCALE[speed] / 24
                for fps in (23.976, 24, 29.97, 30, 59.94, 60):
                    actual = E.scaled_duration(base, speed, fps) / fps
                    self.assertAlmostEqual(actual, intended_seconds, delta=0.5 / fps + 1e-6,
                                           msg=(base, speed, fps))

    def test_letter_reveal_delay_tracks_timeline_fps(self):
        for text in ("HI", "x" * 41):
            for speed in E.SPEEDS:
                reference = E.letter_delay(text, speed, 24) / 24
                for fps in (23.976, 29.97, 30, 59.94, 60):
                    actual = E.letter_delay(text, speed, fps) / fps
                    self.assertAlmostEqual(actual, reference, delta=0.001,
                                           msg=(len(text), speed, fps))

    def test_motion_presets_keep_move_seconds_across_frame_rates(self):
        for preset in motion.load():
            for speed in motion.SPEEDS:
                reference = motion.plan(preset, 300, 24, speed=speed)
                for fps in (23.976, 29.97, 30, 59.94, 60):
                    actual = motion.plan(preset, round(300 * fps / 24), fps, speed=speed)
                    for earlier, later in zip(reference["moves"], actual["moves"]):
                        self.assertEqual(earlier[0], later[0])
                        earlier_seconds = (earlier[4] - earlier[3]) / 24
                        later_seconds = (later[4] - later[3]) / fps
                        self.assertAlmostEqual(later_seconds, earlier_seconds, delta=1 / fps,
                                               msg=(preset["id"], speed, fps, earlier[0]))


if __name__ == "__main__":
    unittest.main()
