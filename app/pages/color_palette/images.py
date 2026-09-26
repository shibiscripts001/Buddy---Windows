#!/usr/bin/env python3
"""
The Extract tab's image work, Qt-free: previews for the web view, fetching
a dragged or pasted web image, and the two exports (image + palette strip,
ARRI LogC false colour with its legend). The preview goes out as a data:
URL.
"""

import base64
import io
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from .color_engine import (
    FALSE_COLOR_INFO, FALSE_COLOR_ZONES, apply_colorblind_filter, apply_false_color,
    false_color_ire_strip, false_color_legend_strip, hex_to_rgb_tuple,
)

try:
    from PIL import Image, ImageDraw
    IMAGING = True
except ImportError:  # the installer ships Pillow; this is for a bare checkout
    IMAGING = False

PREVIEW_SIZE = (960, 720)
MAX_URL_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MB safety cap
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif")
IMAGE_FILTER = "Image files (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff *.tif);;All files (*.*)"


def is_image_path(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def preview_image(path):
    """The picture shown on the Extract tab: RGB, at most PREVIEW_SIZE."""
    with Image.open(path) as src:
        img = src.convert("RGB")
    img.thumbnail(PREVIEW_SIZE)
    return img


def shown_preview(img, false_color=False, vision="None"):
    return apply_false_color(img) if false_color else apply_colorblind_filter(img, vision)


def data_url(img, quality=88):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def false_color_legend():
    """The zones for the page's legend: name, IRE range, colour, meaning."""
    return [{
        "name": name,
        "range": label,
        "color": "#{:02X}{:02X}{:02X}".format(*rgb),
        "info": FALSE_COLOR_INFO.get(name, ""),
    } for name, _lo, _hi, rgb, label in FALSE_COLOR_ZONES]


# -------------------------------------------------------------- exports --

def save_with_palette(path, colors, out_path):
    """The image with a strip of `colors` under it - the standalone's
    "Export Image + Palette Strip"."""
    with Image.open(path) as src:
        base = src.convert("RGB")
    width, height = base.size
    gap = max(2, round(width / 250))
    strip_height = max(60, round(width * 0.15))

    canvas = Image.new("RGB", (width, height + gap + strip_height), (255, 255, 255))
    canvas.paste(base, (0, 0))
    draw = ImageDraw.Draw(canvas)
    n = len(colors)
    edges = [round(i * width / n) for i in range(n + 1)]
    top, bottom = height + gap, height + gap + strip_height
    for i, color_hex in enumerate(colors):
        x0 = edges[i] + (gap / 2 if i > 0 else 0)
        x1 = edges[i + 1] - (gap / 2 if i < n - 1 else 0)
        draw.rectangle([x0, top, x1, bottom], fill=hex_to_rgb_tuple(color_hex))
    canvas.save(out_path, "PNG")


def save_false_color(path, out_path):
    """The image in ARRI LogC false colour with the zone legend and IRE
    labels under it."""
    with Image.open(path) as src:
        base = src.convert("RGB")
    false_colored = apply_false_color(base)
    width, height = false_colored.size
    gap = max(2, round(width / 250))
    legend_height = max(40, round(width * 0.08))
    ire_height = max(24, round(width * 0.035))

    canvas = Image.new("RGB", (width, height + gap * 2 + legend_height + ire_height), (255, 255, 255))
    canvas.paste(false_colored, (0, 0))
    next_y = height + gap
    legend = false_color_legend_strip(width, legend_height)
    if legend:
        canvas.paste(legend, (0, next_y))
    next_y += legend_height + gap
    ire = false_color_ire_strip(width, ire_height)
    if ire:
        canvas.paste(ire, (0, next_y))
    canvas.save(out_path, "PNG")


# ----------------------------------------------------------- web images --

# Google/Bing/etc. image-search results hand you a link to their own results
# page (e.g. google.com/imgres?...&imgurl=<real image>&imgrefurl=<source
# page>&...), not the actual image - these are the query keys they tuck the
# real, direct image URL (and the page it came from) behind.
_SEARCH_ENGINE_IMAGE_URL_KEYS = ("imgurl", "mediaurl", "murl", "img_url")
_SEARCH_ENGINE_REFERER_KEYS = ("imgrefurl", "docid_url")


def unwrap_search_engine_url(url):
    """If `url` is a search engine's image-viewer wrapper page, pull the
    real image URL (and its original referring page, for the Referer
    header) out of the wrapper's query string. Returns (url, referer) -
    referer is None when there's nothing to unwrap or no referring page
    was given, in which case the caller falls back to same-origin."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    image_url = None
    for key in _SEARCH_ENGINE_IMAGE_URL_KEYS:
        candidate = query.get(key, [None])[0]
        if candidate and candidate.startswith(("http://", "https://")):
            image_url = candidate
            break
    if image_url is None:
        return url, None
    referer = None
    for key in _SEARCH_ENGINE_REFERER_KEYS:
        candidate = query.get(key, [None])[0]
        if candidate and candidate.startswith(("http://", "https://")):
            referer = candidate
            break
    return image_url, referer


def sanitize_url(url):
    """Percent-encode any raw spaces/control characters in a URL without
    double-encoding sequences that are already escaped. Browsers sometimes
    hand drag/drop payloads with a literal, un-encoded space in the query
    string, which urllib.request otherwise rejects outright."""
    parts = urllib.parse.urlsplit(url)
    safe = "%/:@&=+$,;?#"
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        urllib.parse.quote(parts.path, safe=safe),
        urllib.parse.quote(parts.query, safe=safe),
        urllib.parse.quote(parts.fragment, safe=safe),
    ))


def is_image_link(text):
    text = (text or "").strip()
    return text.startswith(("http://", "https://", "data:image"))


def download_bytes(url, referer_override=None, opener=urllib.request.urlopen):
    """Fetch image bytes for a dragged/pasted web URL.

    Many sites (hotlink-protected CDNs, image search results) return a 403
    or an HTML page unless the request looks like a real browser tab, so it
    sends a full Chrome UA plus image Accept headers. The Referer is the
    part that varies site to site: some CDNs reject a foreign Referer and
    want same-origin, others reject a missing one - so it tries the best
    guess first and falls back through the others on a 403."""
    url = sanitize_url(url)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http/https URLs are supported.")

    same_origin = f"{parsed.scheme}://{parsed.netloc}/"
    attempts = [referer_override or same_origin]
    if same_origin not in attempts:
        attempts.append(same_origin)
    attempts.append(None)  # no Referer at all

    last_error = None
    for referer in attempts:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=15) as response:
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError(f"That URL did not return an image (got '{content_type}').")
                raw = response.read(MAX_URL_DOWNLOAD_BYTES + 1)
            if len(raw) > MAX_URL_DOWNLOAD_BYTES:
                raise ValueError("Image exceeds the 25 MB safety limit.")
            return raw
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 403:
                continue  # try the next Referer strategy
            raise
    raise ValueError(
        "This site is blocking automated downloads (403 Forbidden). "
        "Try saving the image and using Browse… to open the file instead."
    ) from last_error


def fetch_to_temp(link):
    """A web image (or data: URL) saved to a temp file; returns its path.
    Runs on a worker thread - it can take up to 15 s per attempt."""
    if link.startswith("data:image"):
        _header, b64 = link.split(",", 1)
        raw = base64.b64decode(b64)
    else:
        real_url, referer = unwrap_search_engine_url(link)
        raw = download_bytes(real_url, referer)
    with Image.open(io.BytesIO(raw)) as probe:
        ext = "." + (probe.format or "PNG").lower()
    fd, path = tempfile.mkstemp(suffix=ext, prefix="cpm_url_")
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path
