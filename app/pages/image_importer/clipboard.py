#!/usr/bin/env python3
"""
Clipboard reading, filename hygiene and URL downloading for Image
Importer - everything the page does that is neither Qt layout nor Resolve.

Kept apart from the page so the parts worth testing can be tested: unique_filename and sanitize_filename are pure, and the download
worker is a plain QObject with signals rather than something tangled into
a window.
"""

import os
import re
import threading
import urllib.parse
import urllib.request

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication

from .staging import IMAGE_EXTS, sanitize_filename, unique_filename  # noqa: F401 - re-exported


# What a download is saved as, by what it turns out to be (the picture's own
# bytes, as Qt reads them) - never by the server's Content-Type or the URL's
# ending, which a server can set to anything, ".exe" included.
VERIFIED_EXT = {
    b"jpeg": ".jpg",
    b"png": ".png",
    b"gif": ".gif",
    b"bmp": ".bmp",
    b"tiff": ".tiff",
    b"webp": ".webp",
}

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25 MB safety cap
MAX_PIXELS = 100_000_000               # a small file can still unpack to gigabytes


def verified_extension(data):
    """The file extension for picture bytes of a kind Image Importer takes
    (JPEG, PNG, GIF, BMP, TIFF, WebP), read from the bytes themselves.
    Raises ValueError for anything else - not a picture, a kind it doesn't
    take, or one that would unpack to more than MAX_PIXELS."""
    raw = QByteArray(data)
    buffer = QBuffer(raw)
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    ext = VERIFIED_EXT.get(bytes(reader.format()).lower())
    size = reader.size()
    if ext is None or not reader.canRead():
        raise ValueError("That URL did not return a picture Image Importer can use "
                         "(JPEG, PNG, GIF, BMP, TIFF or WebP).")
    if size.isValid() and size.width() * size.height() > MAX_PIXELS:
        raise ValueError("That picture is too large to import.")
    return ext


class DownloadCancelled(Exception):
    pass


def download_image_from_url(url, dest_folder, timeout=15, cancelled=None):
    """Fetch an image URL to disk. Returns (dest_path, final_name).

    cancelled: optional callable, checked between chunks, so a quit doesn't
    have to wait out a slow 25 MB download (raises DownloadCancelled).

    The browser-ish headers are not decoration: plenty of image hosts
    refuse a bare urllib request, and the Referer is what gets past the
    hotlink checks on the rest.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http/https URLs are supported.")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = (
            response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        )
        if not content_type.startswith("image/"):
            raise ValueError(
                f"That URL did not return an image "
                f"(got '{content_type or 'unknown'}')."
            )
        chunks, size = [], 0
        while size <= MAX_DOWNLOAD_BYTES:
            if cancelled is not None and cancelled():
                raise DownloadCancelled()
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError("Image exceeds the 25 MB safety limit.")

    ext = verified_extension(data)
    base_name = (
        sanitize_filename(os.path.splitext(os.path.basename(parsed.path))[0])
        or "image"
    )

    os.makedirs(dest_folder, exist_ok=True)
    final_name = unique_filename(f"{base_name}{ext}", dest_folder)
    dest_path = os.path.join(dest_folder, final_name)
    with open(dest_path, "wb") as handle:
        handle.write(data)
    return dest_path, final_name


def read_clipboard():
    """(kind, payload) for whatever is on the clipboard.

    kind is one of image / files / url / text_other / empty. Priority:
    a real image beats local files, which beat a
    URL, which beats plain text.
    """
    clipboard = QApplication.clipboard()
    mime = clipboard.mimeData()

    if mime.hasImage():
        image = clipboard.image()
        if not image.isNull():
            return "image", image

    if mime.hasUrls():
        urls = mime.urls()
        local_files = [
            u.toLocalFile() for u in urls if u.isLocalFile() and u.toLocalFile()
        ]
        if local_files:
            return "files", local_files
        first = urls[0].toString()
        if first.lower().startswith(("http://", "https://")):
            return "url", first

    if mime.hasText():
        text = mime.text().strip()
        if text.lower().startswith(("http://", "https://")):
            return "url", text
        return "text_other", text

    return "empty", None


class DownloadWorker(QObject):
    """Runs download_image_from_url on a QThread.

    Never touches a widget - it only emits signals, which Qt marshals back
    onto the GUI thread. Same reason page.py's chat worker does.
    """

    success = Signal(str, str, str)  # dest_path, final_name, url
    failure = Signal(str, str)       # url, error message

    def __init__(self, url, dest_folder):
        super().__init__()
        self.url = url
        self.dest_folder = dest_folder
        self._cancel = threading.Event()

    def cancel(self):
        """Thread-safe: the download stops at its next chunk."""
        self._cancel.set()

    def run(self):
        try:
            dest_path, final_name = download_image_from_url(
                self.url, self.dest_folder, cancelled=self._cancel.is_set)
        except DownloadCancelled:
            self.failure.emit(self.url, "Cancelled.")
            return
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self.failure.emit(self.url, str(exc))
            return
        self.success.emit(dest_path, final_name, self.url)
