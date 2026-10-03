"""Buddy's web views draw on the GPU through ANGLE's Direct3D 11 on 12 -
plain Direct3D 11 crashes in ANGLE's state cache, and software drawing
(BUDDY_WEB_SOFTWARE=1) is slow - painting pages on the GPU without
multisampling (with it, 11 on 12 cuts rounded corners on AMD's integrated
graphics; painting on the CPU made switching tools slow) - and, with two
GPUs, on the low-power one (core/gpu_adapter.py). All of Buddy's own pages
share one renderer process (--process-per-site)."""

import unittest

import _paths  # noqa: F401
from core import gpu_adapter, web_flags

GPU = "--use-angle=d3d11on12 --gpu-rasterization-msaa-sample-count=0"
DEFAULT = "--process-per-site " + GPU


class WebFlagsTests(unittest.TestCase):
    def test_d3d11on12_by_default(self):
        self.assertEqual(web_flags.chromium_flags({}), DEFAULT)

    def test_existing_flags_are_kept(self):
        env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--remote-debugging-port=9223"}
        self.assertEqual(web_flags.chromium_flags(env), "--remote-debugging-port=9223 " + DEFAULT)

    def test_painting_on_the_cpu_can_be_put_back(self):
        self.assertEqual(web_flags.chromium_flags({"BUDDY_WEB_CPU_PAINT": "1"}),
                         DEFAULT + " --disable-gpu-rasterization")
        env = {"BUDDY_WEB_CPU_PAINT": "1", "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu-rasterization"}
        self.assertEqual(web_flags.chromium_flags(env), "--disable-gpu-rasterization " + DEFAULT)

    def test_a_multisampling_count_of_its_own_wins(self):
        env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--gpu-rasterization-msaa-sample-count=4"}
        self.assertEqual(web_flags.chromium_flags(env),
                         "--gpu-rasterization-msaa-sample-count=4 --process-per-site --use-angle=d3d11on12")

    def test_an_existing_angle_choice_wins(self):
        env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--use-angle=d3d9"}
        self.assertEqual(web_flags.chromium_flags(env), "--use-angle=d3d9 --process-per-site")

    def test_software_can_be_put_back_and_is_not_doubled(self):
        env = {"BUDDY_WEB_SOFTWARE": "1", "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"}
        self.assertEqual(web_flags.chromium_flags(env), "--disable-gpu --process-per-site --disable-gpu-compositing")

    def test_one_renderer_per_page_can_be_put_back(self):
        self.assertEqual(web_flags.chromium_flags({"BUDDY_WEB_PROCESS_PER_PAGE": "1"}), GPU)

    def test_the_shared_renderer_is_not_doubled(self):
        env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--process-per-site"}
        self.assertEqual(web_flags.chromium_flags(env), DEFAULT)

    def test_chromium_default_can_be_put_back(self):
        env = {"BUDDY_WEB_GPU": "1", "QTWEBENGINE_CHROMIUM_FLAGS": "--foo"}
        self.assertEqual(web_flags.chromium_flags(env), "--foo")

    def test_apply_sets_the_environment(self):
        env = {}
        web_flags.apply(env)
        self.assertIn("--use-angle=d3d11on12", env["QTWEBENGINE_CHROMIUM_FLAGS"])



NVIDIA, AMD, WARP = (0, 96515), (0, 102444), (0, 102353)


def adapters(*luids, software=()):
    return [{"name": str(l), "luid": l, "software": l in software} for l in luids]


class AdapterTests(unittest.TestCase):
    def test_two_cards_draw_on_the_low_power_one(self):
        self.assertEqual(gpu_adapter.choose(adapters(NVIDIA, AMD, WARP, software=[WARP]), AMD), 1)

    def test_one_card_is_left_to_qt(self):
        self.assertIsNone(gpu_adapter.choose(adapters(NVIDIA, WARP, software=[WARP]), NVIDIA))
        self.assertIsNone(gpu_adapter.choose(adapters(NVIDIA), NVIDIA))

    def test_when_windows_cant_say_it_is_left_to_qt(self):
        self.assertIsNone(gpu_adapter.choose(adapters(NVIDIA, AMD), None))
        self.assertIsNone(gpu_adapter.choose(adapters(NVIDIA, AMD), WARP))   # not one of the cards

    def test_the_software_renderer_is_never_chosen(self):
        self.assertIsNone(gpu_adapter.choose(adapters(NVIDIA, WARP, software=[WARP]), WARP))

    def test_a_setting_already_there_wins(self):
        env = {"QT_D3D_ADAPTER_INDEX": "0"}
        gpu_adapter.apply(env)
        self.assertEqual(env["QT_D3D_ADAPTER_INDEX"], "0")
        env = {"BUDDY_WEB_ADAPTER": "2"}
        gpu_adapter.apply(env)
        self.assertEqual(env["QT_D3D_ADAPTER_INDEX"], "2")
        env = {"BUDDY_WEB_ADAPTER": "default"}
        gpu_adapter.apply(env)
        self.assertNotIn("QT_D3D_ADAPTER_INDEX", env)

    def test_apply_never_raises(self):
        env = {}
        self.assertIsInstance(gpu_adapter.apply(env), str)
        self.assertIn(env.get("QT_D3D_ADAPTER_INDEX", "0"), [str(i) for i in range(16)])


if __name__ == "__main__":
    unittest.main()
