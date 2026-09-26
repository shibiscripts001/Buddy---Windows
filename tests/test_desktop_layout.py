"""The desktop layout's window bookkeeping (app/core/desktop_layout.py):
where windows open, what survives the settings file, stacking, and the
Cascade / Tile arrangements. No Qt."""

import unittest

import _paths  # noqa: F401
from core import desktop_layout as dl

AREA = (1200, 800)


def inside(geom, area=AREA):
    x, y, w, h = geom
    return x >= 0 and y >= 0 and x + w <= area[0] and y + h <= area[1]


class ClampTests(unittest.TestCase):
    def test_a_window_is_pulled_back_onto_the_desk(self):
        self.assertEqual(dl.clamp((1100, 700, 400, 300), AREA), (800, 500, 400, 300))
        self.assertEqual(dl.clamp((-50, -20, 400, 300), AREA), (0, 0, 400, 300))

    def test_never_smaller_than_the_minimum_or_bigger_than_the_desk(self):
        self.assertEqual(dl.clamp((10, 10, 50, 40), AREA)[2:], (dl.MIN_WIDTH, dl.MIN_HEIGHT))
        self.assertEqual(dl.clamp((0, 0, 5000, 5000), AREA), (0, 0, *AREA))

    def test_a_desk_smaller_than_the_minimum_is_filled(self):
        self.assertEqual(dl.clamp((0, 0, 50, 50), (200, 100)), (0, 0, 200, 100))


class ResizeTests(unittest.TestCase):
    FRAME = (0, 0, 600, 400)     # left, top, right, bottom

    def edges(self, x, y):
        return set(dl.edges_at(x, y, self.FRAME, grip=6, corner=18))

    def test_every_edge_and_corner_can_be_grabbed(self):
        self.assertEqual(self.edges(2, 200), {"left"})
        self.assertEqual(self.edges(598, 200), {"right"})
        self.assertEqual(self.edges(300, 2), {"top"})
        self.assertEqual(self.edges(300, 398), {"bottom"})
        self.assertEqual(self.edges(2, 2), {"top", "left"})
        self.assertEqual(self.edges(598, 2), {"top", "right"})
        self.assertEqual(self.edges(2, 398), {"bottom", "left"})
        self.assertEqual(self.edges(598, 398), {"bottom", "right"})
        self.assertEqual(self.edges(300, 200), set())

    def test_corners_reach_further_along_the_edges_than_the_grip(self):
        # 15px along the top edge from the top-right corner is still the corner.
        self.assertEqual(self.edges(585, 3), {"top", "right"})
        self.assertEqual(self.edges(3, 15), {"top", "left"})
        # ...but only on the edge itself: 15px in from both edges is the title bar.
        self.assertEqual(self.edges(585, 15), set())

    def test_the_opposite_edges_stay_put(self):
        g = (100, 100, 500, 300)
        self.assertEqual(dl.resize(g, {"top", "left"}, -40, -30, AREA), (60, 70, 540, 330))
        self.assertEqual(dl.resize(g, {"top", "right"}, 40, 30, AREA), (100, 130, 540, 270))
        self.assertEqual(dl.resize(g, {"bottom", "right"}, 40, 30, AREA), (100, 100, 540, 330))

    def test_a_dragged_edge_stops_at_the_desk_and_the_minimum(self):
        g = (100, 100, 500, 300)
        # Past the top-left of the desk: pinned there, the window doesn't slide.
        self.assertEqual(dl.resize(g, {"top", "left"}, -500, -500, AREA), (0, 0, 600, 400))
        # Past the right of the desk: stops at it rather than pushing left.
        self.assertEqual(dl.resize(g, {"right"}, 5000, 0, AREA), (100, 100, 1100, 300))
        # Shrunk past the minimum from the top: the bottom edge still stays.
        x, y, w, h = dl.resize(g, {"top"}, 0, 1000, AREA)
        self.assertEqual((h, y + h), (dl.MIN_HEIGHT, 400))


class PlacementTests(unittest.TestCase):
    def test_new_windows_cascade_and_stay_on_the_desk(self):
        state = dl.DesktopState()
        spots = []
        for tool in "abcdefghij":
            state.open(tool)
            state.place_new(tool, AREA, preferred=(700, 500))
            spots.append(state.windows[tool]["geom"])
        self.assertTrue(all(inside(g) for g in spots))
        self.assertNotEqual(spots[0][:2], spots[1][:2])
        self.assertEqual(spots[1][0] - spots[0][0], dl.CASCADE_STEP)

    def test_opening_size_keeps_the_margin(self):
        w, h = dl.opening_size(AREA, (5000, 5000))
        self.assertEqual((w, h), (AREA[0] - 2 * dl.MARGIN, AREA[1] - 2 * dl.MARGIN))
        self.assertEqual(dl.opening_size(AREA), (int(AREA[0] * dl.DEFAULT_SHARE), int(AREA[1] * dl.DEFAULT_SHARE)))

    def test_tile_fills_the_desk_without_overlap(self):
        for count in range(1, 10):
            cells = dl.tile(count, AREA)
            self.assertEqual(len(cells), count)
            for g in cells:
                self.assertTrue(inside(g), (count, g))
            for i, a in enumerate(cells):
                for b in cells[i + 1:]:
                    overlap = (a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
                    self.assertFalse(overlap, (count, a, b))


class StateTests(unittest.TestCase):
    def test_open_close_minimise_and_what_is_in_front(self):
        state = dl.DesktopState()
        self.assertTrue(state.open("a"))
        self.assertTrue(state.open("b"))
        self.assertEqual(state.front(), "b")
        self.assertFalse(state.open("a"))          # already on show: just to the front
        self.assertEqual(state.front(), "a")
        state.minimize("a")
        self.assertEqual(state.front(), "b")
        self.assertEqual(state.window_state("a"), "min")
        self.assertTrue(state.open("a"))           # back from minimised counts as newly shown
        state.close("b")
        self.assertIsNone(state.window_state("b"))
        self.assertEqual(state.open_ids(), ["a"])

    def test_moving_a_window_un_maximises_it(self):
        state = dl.DesktopState()
        state.open("a")
        self.assertTrue(state.toggle_max("a"))
        state.set_geom("a", (10, 20, 400, 300))
        self.assertFalse(state.windows["a"]["max"])

    def test_cascade_and_tile_bring_back_minimised_windows(self):
        state = dl.DesktopState()
        for tool in "abc":
            state.open(tool)
        state.minimize("b")
        state.tile(AREA)
        self.assertEqual([state.window_state(t) for t in "abc"], ["open"] * 3)
        state.toggle_max("a")
        state.cascade(AREA)
        self.assertFalse(state.windows["a"]["max"])
        self.assertTrue(all(inside(state.windows[t]["geom"]) for t in "abc"))

    def test_open_windows_get_different_title_colours(self):
        state = dl.DesktopState()
        for tool in "abcd":
            state.open(tool)
        self.assertEqual(sorted(state.windows[t]["color"] for t in "abcd"), list(range(dl.WINDOW_COLORS)))
        # A fifth shares, and a tool reopened keeps its colour when it's free.
        state.open("e")
        colour = state.windows["b"]["color"]
        state.close("b")
        state.close("e")
        state.open("b")
        self.assertEqual(state.windows["b"]["color"], colour)

    def test_hidden_tools_lose_their_windows(self):
        state = dl.DesktopState()
        for tool in "abc":
            state.open(tool)
        state.keep_only({"a", "c"})
        self.assertEqual(state.open_ids(), ["a", "c"])


class TaskbarTests(unittest.TestCase):
    def test_pins_first_then_open_windows_in_the_order_they_opened(self):
        state = dl.DesktopState()
        for tool in ("c", "pinned_open", "a"):
            state.open(tool)
        state.raise_("c")            # stacking doesn't reorder the taskbar
        ids = dl.taskbar_ids(["p1", "pinned_open"], state, {"p1", "pinned_open", "a", "c"})
        self.assertEqual(ids, [("p1", True), ("pinned_open", True), ("c", False), ("a", False)])

    def test_tools_out_of_the_rail_are_left_off(self):
        state = dl.DesktopState()
        state.open("hidden")
        self.assertEqual(dl.taskbar_ids(["gone"], state, {"x"}), [])

    def test_clicking_the_window_in_front_minimises_it(self):
        state = dl.DesktopState()
        state.open("a")
        state.open("b")
        self.assertEqual(dl.task_click(state, "b"), "minimize")
        self.assertEqual(dl.task_click(state, "a"), "open")       # behind: bring forward
        state.minimize("b")
        self.assertEqual(dl.task_click(state, "b"), "open")       # minimised: restore
        self.assertEqual(dl.task_click(state, "never"), "open")

    def test_saved_pins_are_checked_and_an_empty_list_is_kept(self):
        known = {"manual_chat", "time_tracker", "x"}
        self.assertEqual(dl.load_pins(None, known), ["manual_chat", "time_tracker"])
        self.assertEqual(dl.load_pins([], known), [])
        self.assertEqual(dl.load_pins(["x", "x", "gone", 3], known), ["x"])


class SettingsTests(unittest.TestCase):
    def test_round_trip(self):
        state = dl.DesktopState()
        state.open("a")
        state.set_geom("a", (1, 2, 300, 200))
        state.open("b")
        state.minimize("b")
        again = dl.DesktopState.from_dict(state.to_dict(), {"a", "b"})
        self.assertEqual(again.to_dict(), state.to_dict())

    def test_a_damaged_file_is_trusted_only_as_far_as_it_checks_out(self):
        data = {"windows": {"a": {"geom": [1, 2, "x", 4], "open": 1}, "gone": {"open": True}, "b": "nope"},
                "order": ["gone", "a", "a"]}
        state = dl.DesktopState.from_dict(data, {"a", "b"})
        self.assertEqual(list(state.windows), ["a"])
        self.assertIsNone(state.windows["a"]["geom"])
        self.assertTrue(state.windows["a"]["open"])
        self.assertEqual(state.order, ["a"])
        self.assertEqual(dl.DesktopState.from_dict("junk", {"a"}).windows, {})


if __name__ == "__main__":
    unittest.main()
