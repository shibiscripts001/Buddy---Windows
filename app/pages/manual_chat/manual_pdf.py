"""Opening the Reference Manual PDF at a cited page.

Citations carry the PDF's own page index (bundle_builder numbers pages
i + 1), so the PDF to open is the one the bundle was built from: its path
is in the bundle's meta.json. A bundle built before that was recorded has
no path, so the page asks once and remembers the answer in settings.

Windows has no standard "open at page" verb, so the default PDF app is
looked up and given the page the way that app takes it. An app Buddy
doesn't know opens the manual in Edge at the page instead, and if there's
no Edge either, at the start with a note saying which page to go to. On a
Mac, Preview can't be given a page, so it's Chrome, Edge or Brave at the
page if one is installed, else Preview and the note.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .bundle_builder import read_bundle_meta

IS_WINDOWS = sys.platform == "win32"

# Chromium-based browsers and Firefox read #page=N off a file URL.
_BROWSERS = {"msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe", "firefox.exe"}
# Adobe's /A "page=N"; Foxit and PDF-XChange take the same switch.
_OPEN_PARAMETERS = {"acrobat.exe", "acrord32.exe", "foxitpdfreader.exe", "foxitreader.exe",
                    "foxitpdfeditor.exe", "foxitphantompdf.exe", "pdfxedit.exe", "pdfxcview.exe"}
_EDGE = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
_MAC_BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")


def built_from(bundle_dir) -> Path | None:
    """The PDF this bundle was built from, if meta.json records it and it
    is still there."""
    meta = read_bundle_meta(bundle_dir) if bundle_dir else {}
    source = meta.get("source") if isinstance(meta, dict) else None
    path = (source.get("path") or "") if isinstance(source, dict) else ""
    return Path(path) if path and Path(path).is_file() else None


def manual_pdf(bundle_dir, remembered="") -> Path | None:
    """The PDF to open: the bundle's own source, else the one the user
    pointed Buddy at before."""
    found = built_from(bundle_dir)
    if found:
        return found
    if remembered and Path(remembered).is_file():
        return Path(remembered)
    return None


def _default_pdf_app() -> str:
    """The executable Windows opens .pdf files with, or ""."""
    import ctypes
    from ctypes import wintypes
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(len(buf))
    # ASSOCF_NONE, ASSOCSTR_EXECUTABLE
    if ctypes.windll.shlwapi.AssocQueryStringW(0, 2, ".pdf", "open", buf, ctypes.byref(size)) != 0:
        return ""
    return buf.value if os.path.isfile(buf.value) else ""


def command_for(app: str, pdf: Path, page: int) -> list[str] | None:
    """How app opens pdf at page, or None if Buddy doesn't know how."""
    name = os.path.basename(app).lower()
    if name in _BROWSERS:
        return [app, f"{pdf.resolve().as_uri()}#page={page}"]
    if name in _OPEN_PARAMETERS:
        return [app, "/A", f"page={page}", str(pdf)]
    if name == "sumatrapdf.exe":
        return [app, "-page", str(page), str(pdf)]
    return None


def open_at_page(pdf: Path, page: int) -> bool:
    """Opens pdf at page. True if it went to the page, False if it could
    only open the file (the caller says which page to go to)."""
    page = max(1, int(page))
    if IS_WINDOWS:
        app = _default_pdf_app()
        command = command_for(app, pdf, page) if app else None
        if command is None:
            edge = next((p for p in _EDGE if os.path.isfile(p)), "")
            command = command_for(edge, pdf, page) if edge else None
        if command is not None:
            subprocess.Popen(command, creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
            return True
        os.startfile(str(pdf))  # noqa: S606 - the user's own manual, their PDF app
        return False
    if sys.platform == "darwin":
        # Preview, the usual default, can't be told a page; a browser can.
        # Its own binary rather than `open -a`, which may drop the #page.
        browser = next((p for p in _MAC_BROWSERS if os.path.isfile(p)), "")
        if browser:
            subprocess.Popen([browser, f"{pdf.resolve().as_uri()}#page={page}"], start_new_session=True)
            return True
        subprocess.Popen(["open", str(pdf)])
        return False
    subprocess.Popen(["xdg-open", str(pdf)])
    return False
