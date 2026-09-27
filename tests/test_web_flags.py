"""Buddy's web views are drawn in software unless BUDDY_WEB_GPU=1: Chromium's
GPU path (ANGLE's Direct3D 11 backend) can crash Buddy on two-GPU machines."""

import unittest

import _paths  # noqa: F401
from core import web_flags


class WebFlagsTests(unittest.TestCase):
    def test_software_by_default(self):
        self.assertEqual(web_flags.chromium_flags({}), "--disable-gpu --disable-gpu-compositing")

    def test_existing_flags_are_kept_and_not_doubled(self):
        env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--remote-debugging-port=9223 --disable-gpu"}
        self.assertEqual(web_flags.chromium_flags(env),
                         "--remote-debugging-port=9223 --disable-gpu --disable-gpu-compositing")

    def test_gpu_can_be_put_back(self):
        env = {"BUDDY_WEB_GPU": "1", "QTWEBENGINE_CHROMIUM_FLAGS": "--foo"}
        self.assertEqual(web_flags.chromium_flags(env), "--foo")

    def test_apply_sets_the_environment(self):
        env = {}
        web_flags.apply(env)
        self.assertIn("--disable-gpu", env["QTWEBENGINE_CHROMIUM_FLAGS"])


if __name__ == "__main__":
    unittest.main()
