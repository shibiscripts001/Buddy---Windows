"""A clip's Inspector framing as one placement in its Fusion comp
(pages/text_animator/framing.py), checked against what Studio 21.1 drew:
a 641x479 still fitted 1444x1080 on a 1920x1080 timeline, zoomed to 0.5."""

import math
import unittest

import _paths  # noqa: F401
from pages.text_animator import framing as fr

FRAME = (1920, 1080)
STILL = (641 * 1080 / 479, 1080)        # the still as fitted: ~1444 x 1080
FULL = FRAME                            # a 16:9 clip fills the frame


def near(test, got, want, places=3):
    for g, w in zip(got, want):
        test.assertAlmostEqual(g, w, places=places)


class Neutral(unittest.TestCase):
    def test_neutral(self):
        self.assertTrue(fr.is_neutral(dict(fr.NEUTRAL, ZoomGang=True)))
        self.assertTrue(fr.is_neutral({}))
        self.assertFalse(fr.is_neutral({"Pan": 12.0}))
        self.assertFalse(fr.is_neutral({"FlipX": True}))
        self.assertFalse(fr.is_neutral({"CropLeft": 4.0}))

    def test_a_crop_that_stays_in_the_inspector_isnt_framing(self):
        self.assertTrue(fr.is_neutral({"CropLeft": 50.0, "CropRetain": True}))
        self.assertTrue(fr.is_neutral({"CropLeft": 50.0, "CropSoftness": 3.0}))


class FromInspector(unittest.TestCase):
    def test_pan_is_measured_against_the_source_as_shown(self):
        # Pan 200 moved the still 151 px and a full-frame clip 200 px.
        self.assertAlmostEqual(fr.from_inspector({"Pan": 200.0}, STILL, FRAME)["t"][0], 150.5, places=0)
        self.assertAlmostEqual(fr.from_inspector({"Pan": 200.0}, FULL, FRAME)["t"][0], 200.0)
        self.assertAlmostEqual(fr.from_inspector({"Tilt": 100.0}, STILL, FRAME)["t"][1], 100.0)

    def test_zoom_and_rotation_turn_round_the_anchor(self):
        # Anchor X set to 200 (read back 265.7), zoom 0.5, rotation 90: the
        # still's centre was drawn at (1160, 640) - 200 right, 100 down.
        raw = {"ZoomX": 0.5, "ZoomY": 0.5, "RotationAngle": 90.0, "AnchorPointX": 265.6959611717802}
        near(self, fr.from_inspector(raw, STILL, FRAME)["t"], (200.0, -100.0), places=0)
        # ...and zoom 0.25 with the same anchor: 150 right.
        raw = {"ZoomX": 0.25, "ZoomY": 0.25, "AnchorPointX": 265.6959611717802}
        near(self, fr.from_inspector(raw, STILL, FRAME)["t"], (150.0, 0.0), places=0)

    def test_a_plain_crop_comes_in_and_the_rest_of_the_picture_stays_put(self):
        # Crop Left 100 at zoom 0.5: the still's box centre moved 25 px right.
        place = fr.from_inspector({"ZoomX": 0.5, "ZoomY": 0.5, "CropLeft": 100.0}, STILL, FRAME)
        self.assertEqual(place["crop"], (100.0, 0.0, 0.0, 0.0))
        put = fr.merge_inputs(place, 1080 / 479, FRAME)
        self.assertAlmostEqual(put["centre"][0] * FRAME[0], 25.0)
        self.assertAlmostEqual(put["crop"][0], 100 * 479 / 1080)       # in the source's pixels

    def test_a_retained_or_soft_crop_stays_in_the_inspector(self):
        self.assertEqual(fr.from_inspector({"CropLeft": 100.0, "CropRetain": True}, STILL, FRAME)["crop"], (0.0,) * 4)
        self.assertEqual(fr.from_inspector({"CropLeft": 100.0, "CropSoftness": 5.0}, STILL, FRAME)["crop"], (0.0,) * 4)


class Matrices(unittest.TestCase):
    def test_a_flip_is_a_vertical_flip_turned(self):
        zx, zy, angle, flip = fr.decompose(fr.matrix(1, 1, 0, flip_x=True))
        self.assertTrue(flip)
        self.assertAlmostEqual(abs(angle), 180)
        zx, zy, angle, flip = fr.decompose(fr.matrix(0.5, 0.25, 30))
        self.assertFalse(flip)
        near(self, (zx, zy, angle), (0.5, 0.25, 30))

    def test_uneven_zoom_goes_to_the_shape_step(self):
        put = fr.merge_inputs(fr.from_inspector({"ZoomX": 0.25, "ZoomY": 0.5}, FULL, FRAME), 1.0, FRAME)
        self.assertAlmostEqual(put["size"], 0.5)
        near(self, put["shape"], (0.5, 1.0))

    def test_what_changes_after_goes_on_top(self):
        before = fr.from_inspector({"ZoomX": 0.5, "ZoomY": 0.5, "Pan": 100.0}, FULL, FRAME)
        after = fr.from_inspector({"ZoomX": 2.0, "ZoomY": 2.0, "Pan": -50.0}, FULL, FRAME)
        both = fr.compose(after, before)
        near(self, fr.decompose(both["m"])[:3], (1.0, 1.0, 0.0))
        near(self, both["t"], (150.0, 0.0))                 # -50 + 2 x 100
        crop = fr.compose(fr.from_inspector({"CropLeft": 20.0}, FULL, FRAME), before)["crop"]
        self.assertAlmostEqual(crop[0], 40.0)               # 20 screen px of a picture at half size

    def test_back_to_the_inspector(self):
        raw = {"ZoomX": 0.7, "ZoomY": 0.7, "Pan": -120.0, "Tilt": 60.0, "RotationAngle": 15.0, "CropTop": 30.0}
        place = fr.from_inspector(raw, STILL, FRAME)
        back = fr.to_inspector(place, STILL, FRAME)
        for key in ("ZoomX", "ZoomY", "Pan", "Tilt", "RotationAngle", "CropTop"):
            self.assertAlmostEqual(back[key], raw[key], places=6, msg=key)

    def test_a_record_survives_the_comp(self):
        place = fr.from_inspector({"Pan": 10.0, "CropLeft": 3.0}, FULL, FRAME)
        record = fr.loads(fr.dumps({"raw": {"Pan": 10.0}, "place": place, "shown": FULL, "options": {"way": "in"}}))
        self.assertEqual(record["place"]["t"], place["t"])
        self.assertEqual(record["options"], {"way": "in"})
        self.assertIsNone(fr.loads("not json"))
        self.assertIsNone(fr.loads(None))


if __name__ == "__main__":
    unittest.main()
