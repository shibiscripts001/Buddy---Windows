#!/usr/bin/env python3
"""
Mini Palette Window for Color Palette Manager
A small, standalone always-on-hand window showing one palette's swatches in
a fixed 2-row x 6-column grid (paged with "<"/">" past 12 colors). Read-only:
clicking a swatch just copies its hex, no editing/reordering. A pin button
toggles "stay on top of every other window" so it can be left floating over
other apps while working on a project.

A web page in a window of its own (core/web_page.py WebWindow), drawn by
web/mini/: it floats over Resolve, so it can't live inside Buddy's window.
The page (page.py) owns it: it passes itself as `owner` for the palettes,
the Vision mode, the clipboard and the theme.

Protocol:
    to the view    mini, strings
    from the view  copy, page, pin
"""

import os

from PySide6.QtCore import Qt, Signal

from core.web_page import WebWindow

from . import view
from .i18n import get_i18n, tr

BASE_WINDOW_FLAGS = Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint

COLS = 6
ROWS = 2
PAGE_SIZE = COLS * ROWS
WINDOW_SIZE = (368, 162)


class MiniPaletteWindow(WebWindow):
    """Small floating palette holder for one palette, opened from its row's
    pop-out button in the Palettes tab. Not tied to the page's lifetime of
    that row: it pulls the live colours from `owner` on every refresh."""

    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "mini")

    closed = Signal(str)

    def __init__(self, owner, palette_name):
        self.owner = owner
        self.palette_name = palette_name
        self.pinned = False
        self.page = 0
        self._pinned_pos = None
        super().__init__(owner.host)
        # palette_name is user-set data, not UI chrome - never run through tr().
        self.setWindowTitle(palette_name)
        self.setWindowFlags(BASE_WINDOW_FLAGS)
        self.setFixedSize(*WINDOW_SIZE)
        self.apply_transparency()
        get_i18n().language_changed.connect(self._on_language_changed)

    def web_ready(self):
        self._push_strings()
        self.refresh()

    def _push_strings(self):
        self.emit("strings", {"pin": tr("Pin on top"), "unpin": tr("Unpin"), "copied": tr("Copied {hex}")})

    def _on_language_changed(self, *_args):
        self._push_strings()
        self.refresh()

    def _colors(self):
        return self.owner.data_mgr.palettes.get(self.palette_name, [])

    def apply_transparency(self):
        text = self.owner.data_mgr.settings.get("mini_palette_transparency", "95%")
        try:
            alpha = int(str(text).replace("%", "")) / 100.0
        except ValueError:
            alpha = 1.0
        self.setWindowOpacity(max(0.2, min(1.0, alpha)))

    def refresh(self):
        """Redraws from the live palette data - on open, and whenever the
        owner's palettes or Vision mode change while this is open."""
        colors = self._colors()
        pages = max(1, (len(colors) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * PAGE_SIZE
        vision = self.owner.data_mgr.settings.get("colorblind_mode", "None")
        self.emit("mini", {
            "name": self.palette_name,
            "swatches": [dict(view.swatch(h, vision), index=start + i)
                         for i, h in enumerate(colors[start:start + PAGE_SIZE])],
            "page": self.page, "pages": pages, "pinned": self.pinned,
        })

    def on_copy(self, payload):
        index = (payload or {}).get("index")
        colors = self._colors()
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(colors):
            self.owner.copy_to_clipboard(colors[index])

    def on_page(self, payload):
        step = (payload or {}).get("step")
        if step in (-1, 1):
            self.page += step
            self.refresh()

    def set_palette_name(self, new_name):
        self.palette_name = new_name
        self.setWindowTitle(new_name)
        self.page = 0
        self.refresh()

    def on_pin(self, _payload=None):
        self.pinned = not self.pinned
        # Recompute from the fixed BASE_WINDOW_FLAGS rather than OR/AND-ing
        # into whatever windowFlags() currently returns - on Windows,
        # WindowStaysOnTopHint with a flag set missing WindowTitleHint/
        # WindowSystemMenuHint can leave the native close box unclickable.
        flags = BASE_WINDOW_FLAGS | Qt.WindowStaysOnTopHint if self.pinned else BASE_WINDOW_FLAGS
        # setWindowFlags() re-creates the native window, which hides it -
        # show() again right after so the toggle doesn't visibly close it.
        was_visible = self.isVisible()
        self._pinned_pos = self.pos() if self.pinned else None
        self.setWindowFlags(flags)
        if was_visible:
            self.show()
            if self.pinned:
                self.move(self._pinned_pos)
                # Re-showing drops the native topmost z-order until
                # something re-asserts it.
                self.raise_()
                self.activateWindow()
        self.apply_transparency()
        self.refresh()

    def moveEvent(self, event):
        super().moveEvent(event)
        # While pinned it stays where it was pinned: every position a
        # title-bar drag reports is put straight back.
        if self.pinned and self._pinned_pos is not None and self.pos() != self._pinned_pos:
            self.move(self._pinned_pos)

    def closeEvent(self, event):
        self.closed.emit(self.palette_name)
        super().closeEvent(event)
