"""Buddy's web views draw on the GPU through ANGLE's Direct3D 11 on 12:
plain Direct3D 11 can crash Buddy on two-GPU machines, and software drawing
(BUDDY_WEB_SOFTWARE=1) is slow."""

import unittest

import _paths  # noqa: F401
from core import web_flags


class WebFlagsTests(unittest.TestCase):
    def test_d3d11on12_by_default(self):
        self.assertEqual(web_flags.chromium_flags({}), "--use-angle=d3d11on12")

    def test_existing_flags_are_kept(self):
        env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--remote-debugging-port=9223"}
        self.assertEqual(web_flags.chromium_flags(env), "--remote-debugging-port=9223 --use-angle=d3d11on12")

    def test_an_existing_angle_choice_wins(self):
        env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--use-angle=d3d9"}
        self.assertEqual(web_flags.chromium_flags(env), "--use-angle=d3d9")

    def test_software_can_be_put_back_and_is_not_doubled(self):
        env = {"BUDDY_WEB_SOFTWARE": "1", "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"}
        self.assertEqual(web_flags.chromium_flags(env), "--disable-gpu --disable-gpu-compositing")

    def test_chromium_default_can_be_put_back(self):
        env = {"BUDDY_WEB_GPU": "1", "QTWEBENGINE_CHROMIUM_FLAGS": "--foo"}
        self.assertEqual(web_flags.chromium_flags(env), "--foo")

    def test_apply_sets_the_environment(self):
        env = {}
        web_flags.apply(env)
        self.assertIn("--use-angle=d3d11on12", env["QTWEBENGINE_CHROMIUM_FLAGS"])


if __name__ == "__main__":
    unittest.main()
