"""Images in Buddy Network messages - no Qt here.

Sending (shrink): whatever picture the user picks, pastes or drops is
decoded here, turned upright (its EXIF orientation), scaled to fit
MAX_SIDE and saved again as WebP, stepping the quality (then the size)
down until it fits TARGET_BYTES. Saving it again leaves every bit of
metadata behind - EXIF, GPS position, camera serial numbers - so nothing
about where or on what it was taken goes with it.

An animation (a GIF, or an animated WebP or PNG) stays animated: it's saved
as an animated WebP, which is far smaller than a GIF, scaled to fit
ANIM_SIDE and then smaller - and with fewer frames, if that's what it
takes - until it fits the same TARGET_BYTES. That can take a few seconds,
so the page does it off the main thread (attachments.py).

Showing (check): an image from the server is only ever shown after it
decodes here as a WebP, JPEG or PNG of a sane size (and, if it's animated,
a sane number of frames) - anything else is treated as broken, and nothing
is fetched from anywhere but the Buddy Network server. A GIF search's
previews (thumb_check) may be GIFs too - they come from GIPHY through the
server.

A DM's image is encrypted before it goes up and decrypted before it's
checked (e2e.encrypt_image / decrypt_image). Keep MAX_IMAGE_BYTES and
MAX_IMAGE_SIDE in step with server/core.py (tests check).
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass

try:
    from PIL import Image, ImageOps
    AVAILABLE = True
except ImportError:   # Buddy still runs; the page just doesn't offer images
    AVAILABLE = False

MAX_IMAGE_BYTES = 400 * 1024      # the server's limit
MAX_IMAGE_SIDE = 4096             # the server's limit on the width and height it's told
MAX_SIDE = 1600                   # what a picture is scaled to fit
TARGET_BYTES = MAX_IMAGE_BYTES - 1024   # room for a DM's 16-byte tag, and then some
QUALITIES = (82, 72, 62, 52, 42)
SMALLER = 0.8                     # each step down in size, once the quality is as low as it goes
MIN_SIDE = 320
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_INPUT_PIXELS = 80_000_000     # a 10,000 x 8,000 photo; past that it's refused, not decoded
PREVIEW_SIDE = 160
SHOWN_FORMATS = {"WEBP": "image/webp", "JPEG": "image/jpeg", "PNG": "image/png"}

# Animations.
ANIM_SIDE = 480                   # what an animation is scaled to fit first
ANIM_KEEP_SIDE = 240              # below this, frames are dropped first...
ANIM_MIN_FRAMES = 24              # ...down to this many
ANIM_MIN_SIDE = 96
ANIM_MAX_FRAMES = 150             # more than this and every other one (or more) goes
ANIM_QUALITY, ANIM_LOW_QUALITY = 75, 45
MAX_ANIM_PIXELS = 1_500_000_000   # frames x width x height; past that it's refused, not decoded
ANIM_MIN_MS = 20                  # a frame's shortest time (browsers slow shorter ones right down)
MAX_SHOWN_FRAMES = 300            # an animation with more isn't shown (Buddy sends at most ANIM_MAX_FRAMES)
# What the page may be asked to decode: a small file can unpack to far more
# (frames x width x height x 4 bytes in the browser), and the sender's the one choosing.
MAX_SHOWN_PIXELS = 300_000_000    # all frames together: ~1.2 GB if the browser held them at once, never more
MAX_SHOWN_AREA = 4096 * 4096      # any one frame
# A GIF search's previews: small, and GIFs allowed (they're GIPHY's own).
THUMB_MAX_BYTES = 128 * 1024
THUMB_MAX_SIDE = 400
THUMB_MAX_PIXELS = 40_000_000
THUMB_FORMATS = {**SHOWN_FORMATS, "GIF": "image/gif"}


class ImageError(Exception):
    """Why a picture can't be sent - shown to the user as it is."""


@dataclass
class Shrunk:
    data: bytes      # WebP
    w: int
    h: int
    animated: bool = False


def _open(data: bytes):
    if len(data) > MAX_INPUT_BYTES:
        raise ImageError(f"That file is over {MAX_INPUT_BYTES // (1024 * 1024)} MB – pick a smaller picture.")
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_INPUT_PIXELS:
            raise ImageError("That picture is too big to send – crop or shrink it first.")
        image.seek(0)
        image.load()
    except ImageError:
        raise
    except Exception:   # Pillow raises many kinds for a file it can't read
        raise ImageError("That file isn't a picture Buddy can read – try a PNG, JPEG or WebP.") from None
    return image


def _flatten(image):
    """Upright, in a mode WebP saves (keeping transparency)."""
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:   # a broken orientation tag: leave it as it is
        pass
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        return image.convert("RGBA")
    return image.convert("RGB")


def _fit(image, side: int):
    if max(image.size) <= side:
        return image
    scale = side / max(image.size)
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                        Image.LANCZOS)


def _webp(image, quality: int) -> bytes:
    out = io.BytesIO()
    image.save(out, "WEBP", quality=quality, method=4)   # no exif=, no icc_profile=: no metadata
    return out.getvalue()


def _animated(image) -> bool:
    """A GIF, WebP or PNG that moves. (A TIFF's pages aren't an animation.)"""
    return image.format in ("GIF", "WEBP", "PNG") and bool(getattr(image, "is_animated", False))


def _frames(image) -> tuple[list, list[int]]:
    """An animation's frames, each whole (not just what changed) and fitted
    to ANIM_SIDE, and how long each shows (ms). Past ANIM_MAX_FRAMES, only
    every step-th is kept, showing for the ones it stands in for too."""
    count = image.n_frames
    if count * image.width * image.height > MAX_ANIM_PIXELS:
        raise ImageError("That animation is too long or too big to send – trim it first.")
    step = math.ceil(count / ANIM_MAX_FRAMES)
    frames, durations = [], []
    alpha = False
    for index in range(count):
        image.seek(index)
        image.load()   # a WebP only says how long a frame shows once it's loaded
        duration = image.info.get("duration") or 100
        if index % step:
            durations[-1] += duration
            continue
        frame = image.convert("RGBA")
        alpha = alpha or frame.getextrema()[3][0] < 255
        frames.append(_fit(frame, ANIM_SIDE))
        durations.append(duration)
    if not alpha:   # no transparency anywhere: smaller as RGB
        frames = [f.convert("RGB") for f in frames]
    return frames, [max(ANIM_MIN_MS, int(d)) for d in durations]


def _animated_webp(frames: list, durations: list[int], quality: int) -> bytes:
    out = io.BytesIO()
    frames[0].save(out, "WEBP", save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   quality=quality, method=4)   # no exif=, no icc_profile=: no metadata
    return out.getvalue()


def _halve(frames: list, durations: list[int]) -> tuple[list, list[int]]:
    """Every other frame, each showing for the one dropped after it too."""
    kept = durations[::2]
    for i, d in enumerate(durations[1::2]):
        kept[i] += d
    return frames[::2], kept


def _shrink_animated(image) -> Shrunk:
    """Each try is a whole encode (seconds, for a big one), so each goes
    about as far as the last one was over: its size follows its area and
    its number of frames. Near enough, a lower quality; far off, smaller -
    or, rather than below ANIM_KEEP_SIDE, half the frames."""
    frames, durations = _frames(image)
    side = max(frames[0].size)
    for _attempt in range(10):
        fitted = [_fit(f, side) for f in frames]
        out = _animated_webp(fitted, durations, ANIM_QUALITY)
        if len(out) > TARGET_BYTES and len(out) < TARGET_BYTES * 1.8:
            out = _animated_webp(fitted, durations, ANIM_LOW_QUALITY)
        if len(out) <= TARGET_BYTES:
            return Shrunk(out, fitted[0].width, fitted[0].height, animated=True)
        smaller = int(side * math.sqrt(TARGET_BYTES / len(out)) * 0.95)
        if len(frames) > ANIM_MIN_FRAMES and smaller < ANIM_KEEP_SIDE:
            frames, durations = _halve(frames, durations)
            side = max(min(side, ANIM_KEEP_SIDE), int(smaller * math.sqrt(2)))
        elif smaller >= ANIM_MIN_SIDE:
            side = min(smaller, int(side * SMALLER))
        else:
            break
    raise ImageError("That animation couldn't be made small enough to send – try a shorter one.")


def is_animated(data: bytes) -> bool:
    """Whether the picture moves (and so shrinking it will take a while)."""
    try:
        return AVAILABLE and _animated(Image.open(io.BytesIO(data)))
    except Exception:
        return False


def shrink(data: bytes, max_side: int = MAX_SIDE) -> Shrunk:
    """The picture as it will be sent. Raises ImageError. max_side: what
    it's scaled to fit (a bug report's screenshots keep more of their
    detail - core/bug_report.py)."""
    if not AVAILABLE:
        raise ImageError("Sending images needs the Pillow package – reinstalling Buddy adds it.")
    opened = _open(data)
    if _animated(opened):
        try:
            return _shrink_animated(opened)
        except ImageError:
            raise
        except Exception:   # a frame Pillow can't read
            raise ImageError("That animation couldn't be read – try another file.") from None
    image = _flatten(opened)
    side = max_side
    while True:
        fitted = _fit(image, side)
        for quality in QUALITIES:
            out = _webp(fitted, quality)
            if len(out) <= TARGET_BYTES:
                return Shrunk(out, fitted.width, fitted.height)
        if side <= MIN_SIDE:
            raise ImageError("That picture couldn't be made small enough to send.")
        side = max(MIN_SIDE, int(side * SMALLER))


def _checked(data: bytes, max_bytes: int, max_side: int, formats: dict, max_pixels: int | None = None) -> str | None:
    if not AVAILABLE or not data or len(data) > max_bytes:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        frames = getattr(image, "n_frames", 1)
        # Everything is judged from the header, before any frame is decoded.
        max_pixels = MAX_SHOWN_PIXELS if max_pixels is None else max_pixels
        if (image.format not in formats or max(image.size) > max_side
                or image.width * image.height > MAX_SHOWN_AREA or frames > MAX_SHOWN_FRAMES
                or frames * image.width * image.height > max_pixels):
            return None
        image.load()
    except Exception:
        return None
    return formats[image.format]


def check(data: bytes) -> str | None:
    """The MIME type of an image that's safe to show - a WebP, JPEG or PNG
    that decodes completely and isn't absurdly big - else None."""
    return _checked(data, MAX_IMAGE_BYTES, MAX_IMAGE_SIDE, SHOWN_FORMATS)


def thumb_check(data: bytes) -> str | None:
    """The same for a GIF search's preview, which may be a GIF."""
    return _checked(data, THUMB_MAX_BYTES, THUMB_MAX_SIDE, THUMB_FORMATS, THUMB_MAX_PIXELS)


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def preview_url(data: bytes) -> str:
    """A small copy for the composer, before it's sent. An animation is
    shown as it is - it's already small."""
    image = _open(data)
    if _animated(image):
        return data_url(data, "image/webp")
    return data_url(_webp(_fit(_flatten(image), PREVIEW_SIDE), 80), "image/webp")


def shown_size(w: int, h: int, box_w: int = 320, box_h: int = 240) -> tuple[int, int]:
    """How big a message's image is drawn: it fits the box, never larger than it is."""
    w, h = max(1, int(w)), max(1, int(h))
    scale = min(1.0, box_w / w, box_h / h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def size_label(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"
