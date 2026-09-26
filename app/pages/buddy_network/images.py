"""Images in Buddy Network messages - no Qt here.

Sending (shrink): whatever picture the user picks, pastes or drops is
decoded here, turned upright (its EXIF orientation), scaled to fit
MAX_SIDE and saved again as WebP, stepping the quality (then the size)
down until it fits TARGET_BYTES. Saving it again leaves every bit of
metadata behind - EXIF, GPS position, camera serial numbers - so nothing
about where or on what it was taken goes with it. Only the first frame of
an animation is kept.

Showing (check): an image from the server is only ever shown after it
decodes here as a WebP, JPEG or PNG of a sane size - anything else is
treated as broken, and nothing is fetched from anywhere but the Buddy
Network server.

A DM's image is encrypted before it goes up and decrypted before it's
checked (e2e.encrypt_image / decrypt_image). Keep MAX_IMAGE_BYTES and
MAX_IMAGE_SIDE in step with server/core.py (tests check).
"""

from __future__ import annotations

import base64
import io
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


class ImageError(Exception):
    """Why a picture can't be sent - shown to the user as it is."""


@dataclass
class Shrunk:
    data: bytes      # WebP
    w: int
    h: int


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


def shrink(data: bytes) -> Shrunk:
    """The picture as it will be sent. Raises ImageError."""
    if not AVAILABLE:
        raise ImageError("Sending images needs the Pillow package – reinstalling Buddy adds it.")
    image = _flatten(_open(data))
    side = MAX_SIDE
    while True:
        fitted = _fit(image, side)
        for quality in QUALITIES:
            out = _webp(fitted, quality)
            if len(out) <= TARGET_BYTES:
                return Shrunk(out, fitted.width, fitted.height)
        if side <= MIN_SIDE:
            raise ImageError("That picture couldn't be made small enough to send.")
        side = max(MIN_SIDE, int(side * SMALLER))


def check(data: bytes) -> str | None:
    """The MIME type of an image that's safe to show - a WebP, JPEG or PNG
    that decodes completely and isn't absurdly big - else None."""
    if not AVAILABLE or not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in SHOWN_FORMATS or max(image.size) > MAX_IMAGE_SIDE:
            return None
        image.load()
    except Exception:
        return None
    return SHOWN_FORMATS[image.format]


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def preview_url(data: bytes) -> str:
    """A small copy for the composer, before it's sent."""
    image = _fit(_flatten(_open(data)), PREVIEW_SIDE)
    return data_url(_webp(image, 80), "image/webp")


def shown_size(w: int, h: int, box_w: int = 320, box_h: int = 240) -> tuple[int, int]:
    """How big a message's image is drawn: it fits the box, never larger than it is."""
    w, h = max(1, int(w)), max(1, int(h))
    scale = min(1.0, box_w / w, box_h / h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def size_label(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"
