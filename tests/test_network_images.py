"""Images in Buddy Network on Buddy's side (app/pages/buddy_network/images.py,
e2e.encrypt_image, render) - no Qt, no network. The server's limits come
from server/core.py, so the two can't drift apart."""

import io
import os
import unittest
import unittest.mock

import _paths  # noqa: F401
from pages.buddy_network import e2e, export, images, render
from server import core as server_core

try:
    from PIL import Image
except ImportError:   # Buddy's installer always has it; a bare checkout might not
    raise unittest.SkipTest("Pillow not installed")

ROOM = "dm-aaaa-bbbb"
IMAGE_ID = "0123456789abcdef0123456789abcdef"


def picture(w=800, h=600, fmt="PNG", mode="RGB", noise=False, **save) -> bytes:
    image = Image.frombytes(mode, (w, h), os.urandom(w * h * len(mode))) if noise else Image.new(mode, (w, h), (0, 128, 128, 100)[:len(mode)])
    out = io.BytesIO()
    image.save(out, fmt, **save)
    return out.getvalue()


def animation(w=320, h=240, frames=12, fmt="GIF", noise=0, duration=50, **save) -> bytes:
    """A moving square (and, with noise, grain that doesn't compress)."""
    shown = []
    for i in range(frames):
        frame = Image.new("RGB", (w, h), (20, 30, 60))
        frame.paste((200, 90, 40), (i * 7 % w, i * 5 % h, i * 7 % w + w // 4, i * 5 % h + h // 4))
        if noise:
            frame = Image.blend(frame, Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)), noise)
        shown.append(frame)
    out = io.BytesIO()
    shown[0].save(out, fmt, save_all=True, append_images=shown[1:], duration=duration, loop=0, **save)
    return out.getvalue()


class ShrinkTests(unittest.TestCase):
    def test_limits_match_the_server(self):
        self.assertEqual(images.MAX_IMAGE_BYTES, server_core.MAX_IMAGE_BYTES)
        self.assertEqual(images.MAX_IMAGE_SIDE, server_core.MAX_IMAGE_SIDE)
        self.assertLessEqual(images.TARGET_BYTES + 16, server_core.MAX_IMAGE_BYTES)   # a DM's tag

    def test_a_big_noisy_photo_is_scaled_and_squeezed_to_fit(self):
        shrunk = images.shrink(picture(3000, 2000, "PNG", noise=True))
        self.assertLessEqual(len(shrunk.data), images.TARGET_BYTES)
        self.assertLessEqual(max(shrunk.w, shrunk.h), images.MAX_SIDE)
        self.assertAlmostEqual(shrunk.w / shrunk.h, 1.5, places=1)
        self.assertEqual(Image.open(io.BytesIO(shrunk.data)).format, "WEBP")
        self.assertTrue(server_core.is_image_file(shrunk.data))

    def test_a_small_picture_keeps_its_size(self):
        shrunk = images.shrink(picture(300, 200))
        self.assertEqual((shrunk.w, shrunk.h), (300, 200))

    def test_metadata_is_left_behind_and_the_photo_turned_upright(self):
        exif = Image.Exif()
        exif[0x0112] = 6            # orientation: rotated 90 degrees
        exif[0x010F] = "SecretCam"  # camera make
        raw = picture(400, 200, "JPEG", exif=exif.tobytes())
        self.assertIn(b"SecretCam", raw)
        shrunk = images.shrink(raw)
        self.assertEqual((shrunk.w, shrunk.h), (200, 400))
        self.assertNotIn(b"SecretCam", shrunk.data)
        self.assertNotIn(b"Exif", shrunk.data)
        self.assertFalse(Image.open(io.BytesIO(shrunk.data)).getexif())

    def test_transparency_survives(self):
        shrunk = images.shrink(picture(64, 64, "PNG", mode="RGBA"))
        self.assertEqual(Image.open(io.BytesIO(shrunk.data)).mode, "RGBA")

    def test_what_isnt_a_picture_is_refused_with_a_reason(self):
        for junk in (b"", b"MZ\x90\x00 an exe", os.urandom(5000)):
            with self.assertRaises(images.ImageError):
                images.shrink(junk)

    def test_an_absurd_size_is_refused_before_it_is_decoded(self):
        header = picture(1, 1, "PNG")
        # A PNG that claims to be 20,000 x 20,000 (its pixels never arrive).
        huge = header[:16] + (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big") + header[24:]
        with self.assertRaises(images.ImageError):
            images.shrink(huge)


class AnimationTests(unittest.TestCase):
    def test_a_gif_stays_animated_as_a_webp(self):
        shrunk = images.shrink(animation(frames=12, duration=70))
        self.assertTrue(shrunk.animated)
        self.assertEqual((shrunk.w, shrunk.h), (320, 240))
        out = Image.open(io.BytesIO(shrunk.data))
        self.assertEqual((out.format, out.n_frames), ("WEBP", 12))
        out.seek(3)
        out.load()
        self.assertEqual(out.info["duration"], 70)
        self.assertTrue(server_core.is_image_file(shrunk.data))
        self.assertEqual(images.check(shrunk.data), "image/webp")
        self.assertTrue(images.is_animated(animation()))
        self.assertFalse(images.is_animated(picture(fmt="GIF")))

    def test_a_big_animation_is_scaled_and_thinned_to_fit(self):
        shrunk = images.shrink(animation(500, 500, frames=60, noise=0.3, duration=40))
        self.assertLessEqual(len(shrunk.data), images.TARGET_BYTES)
        self.assertLessEqual(max(shrunk.w, shrunk.h), images.ANIM_SIDE)
        out = Image.open(io.BytesIO(shrunk.data))
        self.assertTrue(out.is_animated)
        # However many frames went, it lasts as long as it did.
        total = 0
        for i in range(out.n_frames):
            out.seek(i)
            out.load()
            total += out.info["duration"]
        self.assertAlmostEqual(total, 60 * 40, delta=out.n_frames)

    def test_a_long_one_keeps_at_most_so_many_frames(self):
        shrunk = images.shrink(animation(60, 40, frames=images.ANIM_MAX_FRAMES * 2 + 10, duration=30))
        self.assertLessEqual(Image.open(io.BytesIO(shrunk.data)).n_frames, images.ANIM_MAX_FRAMES)

    def test_an_animated_webp_or_png_stays_animated_too(self):
        for fmt in ("WEBP", "PNG"):
            shrunk = images.shrink(animation(fmt=fmt, duration=70))
            self.assertTrue(shrunk.animated, fmt)
            out = Image.open(io.BytesIO(shrunk.data))
            out.seek(2)
            out.load()
            self.assertEqual(out.info["duration"], 70, fmt)   # GIPHY's are WebPs: their timing is kept

    def test_an_animation_leaves_its_metadata_behind_too(self):
        frames = [Image.new("RGB", (64, 64), (i * 40, 0, 0)) for i in range(4)]
        exif = Image.Exif()
        exif[0x010F] = "SecretCam"
        out = io.BytesIO()
        frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=100, exif=exif.tobytes(),
                       xmp=b"<x:xmpmeta>SecretXMP</x:xmpmeta>")
        shrunk = images.shrink(out.getvalue())
        self.assertTrue(shrunk.animated)
        for secret in (b"SecretCam", b"SecretXMP", b"EXIF", b"XMP "):
            self.assertNotIn(secret, shrunk.data)

    def test_transparency_survives_in_an_animation(self):
        frames = [Image.new("RGBA", (50, 50), (255, 0, 0, a)) for a in (0, 128, 255)]
        out = io.BytesIO()
        frames[0].save(out, "PNG", save_all=True, append_images=frames[1:], duration=100)
        self.assertEqual(Image.open(io.BytesIO(images.shrink(out.getvalue()).data)).mode, "RGBA")

    def test_the_composer_preview_of_an_animation_moves(self):
        shrunk = images.shrink(animation())
        url = images.preview_url(shrunk.data)
        self.assertEqual(url, images.data_url(shrunk.data, "image/webp"))

    def test_too_many_frames_is_refused_before_they_are_decoded(self):
        with self.assertRaises(images.ImageError):
            with unittest.mock.patch.object(images, "MAX_ANIM_PIXELS", 1000):
                images.shrink(animation(frames=3))

    def test_previews_from_gif_search_may_be_gifs_but_are_still_checked(self):
        self.assertEqual(images.thumb_check(animation(100, 80)), "image/gif")
        self.assertEqual(images.thumb_check(picture(100, 80, fmt="WEBP")), "image/webp")
        self.assertIsNone(images.thumb_check(picture(images.THUMB_MAX_SIDE + 1, 10, fmt="GIF")))
        self.assertIsNone(images.thumb_check(b"GIF89a<script>"))
        self.assertIsNone(images.thumb_check(os.urandom(images.THUMB_MAX_BYTES + 1)))


class CheckTests(unittest.TestCase):
    def test_only_whole_webp_jpeg_or_png_is_shown(self):
        self.assertEqual(images.check(images.shrink(picture()).data), "image/webp")
        self.assertEqual(images.check(picture(fmt="JPEG")), "image/jpeg")
        self.assertEqual(images.check(picture(fmt="PNG")), "image/png")
        self.assertIsNone(images.check(picture(fmt="GIF")))
        self.assertIsNone(images.check(picture(fmt="BMP")))
        self.assertIsNone(images.check(picture(fmt="JPEG")[:200]))   # cut short
        self.assertIsNone(images.check(b"<svg onload=alert(1)>"))
        self.assertIsNone(images.check(b""))

    def test_shown_size_fits_the_box_and_never_grows(self):
        self.assertEqual(images.shown_size(1600, 1200), (320, 240))
        self.assertEqual(images.shown_size(100, 50), (100, 50))
        self.assertEqual(images.shown_size(400, 4000), (24, 240))


class ImageEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.alice, self.alice_work, self.bob = (e2e.DeviceKey.generate() for _ in range(3))
        self.data = images.shrink(picture()).data
        self.sealed, self.enc = e2e.encrypt_image(
            self.data, room=ROOM, sender="aaaa", image_id=IMAGE_ID, me=self.alice,
            recipients={k.id: k.public for k in (self.alice, self.alice_work, self.bob)})
        self.known = {self.alice.id: self.alice.public}

    def open(self, sealed=None, enc=None, **kw):
        args = {"room": ROOM, "sender": "aaaa", "image_id": IMAGE_ID, "readers": [self.bob],
                "sender_keys": self.known, **kw}
        return e2e.decrypt_image(self.sealed if sealed is None else sealed, self.enc if enc is None else enc, **args)

    def test_every_pc_of_both_people_can_open_it_and_the_server_cant(self):
        for reader in (self.alice, self.alice_work, self.bob):
            self.assertEqual(self.open(readers=[reader]), self.data)
        self.assertNotIn(self.data[:64], self.sealed)
        self.assertEqual(len(self.sealed), len(self.data) + 16)
        self.assertEqual(server_core.clean_image_enc(self.enc, {self.alice.id}).count(self.alice.id), 2)

    def test_anything_changed_swapped_or_misplaced_is_refused(self):
        flipped = bytearray(self.sealed)
        flipped[40] ^= 1
        self.assertIsNone(self.open(sealed=bytes(flipped)))
        self.assertIsNone(self.open(image_id="f" * 32))          # another image's id
        self.assertIsNone(self.open(room="dm-aaaa-cccc"))         # another conversation
        self.assertIsNone(self.open(sender="cccc"))               # passed off as someone else's
        self.assertIsNone(self.open(sender_keys={}))              # from a PC nobody knows
        self.assertIsNone(self.open(readers=[e2e.DeviceKey.generate()]))   # not for this PC
        self.assertIsNone(self.open(enc={"v": 99}))

    def test_a_message_key_cant_open_the_image(self):
        """The text and the image are sealed apart: editing the text leaves the image readable."""
        text_enc = e2e.encrypt("caption", room=ROOM, sender="aaaa", me=self.alice,
                               recipients={self.bob.id: self.bob.public})
        self.assertIsNone(self.open(enc={k: text_enc[k] for k in ("v", "from", "salt", "keys")}))
        self.assertEqual(self.open(), self.data)


class RenderTests(unittest.TestCase):
    COLORS = {"text": "#eee", "muted": "#999", "me": "#0af", "other": "#fa0", "link": "#0af", "warning": "#fc0"}

    def html(self, **m):
        message = {"id": 1, "room": "global", "author": {"id": "u2", "tag": "u2", "name": "Bob"}, "text": "",
                   "ts": 1_750_000_000, "deleted": False, **m}
        return render.room_html([message], my_id="u1", room_name="#Global", more=False, links=[],
                                colors=self.COLORS, now=1_750_000_000, image_days=7)

    def test_an_image_is_a_placeholder_the_page_fills_in(self):
        out = self.html(text="look", image={"id": IMAGE_ID, "w": 1600, "h": 1200})
        self.assertIn(f'data-image="{IMAGE_ID}"', out)
        self.assertIn("width:320px; height:240px", out)
        self.assertIn("look", out)
        self.assertNotIn("src=", out.split('class="shot"')[1])   # no URL: the page gets a checked data: URL

    def test_an_expired_image_says_so(self):
        out = self.html(text="", image={"id": IMAGE_ID, "gone": True})
        self.assertIn("Image expired – images are kept for 7 days", out)
        self.assertNotIn("data-image", out)

    def test_ids_that_arent_ids_and_unreadable_messages_show_nothing(self):
        self.assertNotIn("data-image", self.html(image={"id": '"><script>', "w": 1, "h": 1}))
        self.assertNotIn("data-image", self.html(image={"id": IMAGE_ID, "w": 1, "h": 1}, unreadable=True))
        self.assertNotIn("data-image", self.html(image={"id": IMAGE_ID, "w": 1, "h": 1}, deleted=True))

    def test_a_gif_from_gif_search_credits_giphy_and_its_maker_escaped(self):
        out = self.html(image={"id": IMAGE_ID, "w": 200, "h": 150,
                               "credit": {"source": "giphy", "user": "<b>Maker</b>"}})
        self.assertIn("GIF via GIPHY", out)
        self.assertIn("&lt;b&gt;Maker&lt;/b&gt;", out)
        self.assertNotIn("<b>Maker", out)
        self.assertNotIn("GIPHY", self.html(image={"id": IMAGE_ID, "w": 200, "h": 150}))

    def test_quotes_and_exports_mention_the_image(self):
        self.assertEqual(render.quote_text({"id": 5, "text": "", "image": True}, {}), "an image")
        self.assertEqual(render.quote_text({"id": 5}, {5: {"text": "", "image": {"id": IMAGE_ID}}}), "an image")
        message = {"id": 1, "room": "global", "author": {"id": "u2", "tag": "u2", "name": "Bob"}, "text": "look",
                   "ts": 1_750_000_000, "image": {"id": IMAGE_ID, "w": 1, "h": 1}}
        self.assertIn("look [image]", export.as_text("#Global", [message], 1_750_000_000))


if __name__ == "__main__":
    unittest.main()
