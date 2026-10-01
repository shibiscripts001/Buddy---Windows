#!/usr/bin/env python3
"""
System-wide hot keys: a key combo that works whichever app is in front -
Resolve, mostly - and runs one of Command Center's actions
(pages/command_center/). Windows only for now: Windows' own RegisterHotKey,
so Buddy is told about its combos and nothing else - no keyboard hook, no
seeing anything else that's typed. While Buddy holds a combo it's Buddy's:
the app in front doesn't get it (conflicts() warns about Resolve's own).

A combo is kept as text, "Ctrl+Alt+M": modifiers in a fixed order, then
the key by its name in KEYS (what a web page's KeyboardEvent.code calls it,
so the page that records one and this module agree). from_event() turns a
page's keydown into one; parse() reads one back.

The parsing and checking need no Qt, so they're tested directly;
GlobalHotkeys is the Windows part (a native event filter for WM_HOTKEY).
"""

from __future__ import annotations

import sys

MODIFIERS = ("Ctrl", "Alt", "Shift", "Win")
_MOD_BITS = {"Alt": 0x0001, "Ctrl": 0x0002, "Shift": 0x0004, "Win": 0x0008}
MOD_NOREPEAT = 0x4000     # one press, one action - held down it doesn't repeat
WM_HOTKEY = 0x0312


def _keys() -> dict:
    """KeyboardEvent.code -> (what it's called, Windows virtual-key code)."""
    keys = {}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        keys[f"Key{c}"] = (c, ord(c))
    for d in "0123456789":
        keys[f"Digit{d}"] = (d, ord(d))
        keys[f"Numpad{d}"] = (f"Num {d}", 0x60 + int(d))
    for n in range(1, 25):
        keys[f"F{n}"] = (f"F{n}", 0x6F + n)
    keys.update({
        "Space": ("Space", 0x20), "Enter": ("Enter", 0x0D), "Tab": ("Tab", 0x09), "Backspace": ("Backspace", 0x08),
        "Insert": ("Insert", 0x2D), "Delete": ("Delete", 0x2E), "Home": ("Home", 0x24), "End": ("End", 0x23),
        "PageUp": ("Page Up", 0x21), "PageDown": ("Page Down", 0x22),
        "ArrowLeft": ("Left", 0x25), "ArrowUp": ("Up", 0x26), "ArrowRight": ("Right", 0x27), "ArrowDown": ("Down", 0x28),
        "Minus": ("-", 0xBD), "Equal": ("=", 0xBB), "BracketLeft": ("[", 0xDB), "BracketRight": ("]", 0xDD),
        "Backslash": ("\\", 0xDC), "Semicolon": (";", 0xBA), "Quote": ("'", 0xDE), "Comma": (",", 0xBC),
        "Period": (".", 0xBE), "Slash": ("/", 0xBF), "Backquote": ("`", 0xC0),
        "NumpadAdd": ("Num Plus", 0x6B), "NumpadSubtract": ("Num -", 0x6D), "NumpadMultiply": ("Num *", 0x6A),
        "NumpadDivide": ("Num /", 0x6F), "NumpadDecimal": ("Num .", 0x6E),
    })
    return keys


KEYS = _keys()
_BY_NAME = {name: code for code, (name, _vk) in KEYS.items()}
# Keys that are fine on their own: nobody types with them.
LONE_KEYS = {f"F{n}" for n in range(13, 25)}

# Resolve's own default shortcuts (Windows) a combo would take from it while
# Buddy holds it - the ones people use every day.
RESOLVE_SHORTCUTS = {
    "Ctrl+Z": "Undo", "Ctrl+Shift+Z": "Redo", "Ctrl+S": "Save project", "Ctrl+C": "Copy", "Ctrl+V": "Paste",
    "Ctrl+X": "Cut", "Ctrl+A": "Select all", "Ctrl+D": "Change clip duration", "Ctrl+B": "Razor",
    "Ctrl+\\": "Split clip", "Ctrl+N": "New timeline", "Ctrl+Shift+N": "New bin", "Ctrl+I": "Import media",
    "Ctrl+Q": "Quit Resolve", "Alt+V": "Paste attributes",
}
# Windows keeps these for itself: RegisterHotKey refuses them, or they'd lock the PC.
RESERVED = {"Ctrl+Alt+Delete", "Win+L", "Ctrl+Shift+Escape", "Alt+Tab", "Win+Tab", "Ctrl+Escape", "Win+D",
            "Win+R", "Win+E", "Alt+F4"}


def from_event(code: str, ctrl=False, alt=False, shift=False, meta=False) -> str:
    """A web page's keydown as a combo ("Ctrl+Alt+M"), or "" for a key this
    doesn't know (or a modifier on its own)."""
    if code not in KEYS:
        return ""
    held = [m for m, on in zip(MODIFIERS, (ctrl, alt, shift, meta)) if on]
    return "+".join([*held, KEYS[code][0]])


def parse(combo) -> tuple[list[str], str] | None:
    """"Ctrl+Alt+M" -> (["Ctrl", "Alt"], "KeyM"), or None if it isn't one."""
    if not isinstance(combo, str) or not combo:
        return None
    *mods, key = combo.split("+")   # no key is called "+" ("Num Plus"), so this splits cleanly
    if key not in _BY_NAME or len(set(mods)) != len(mods) or any(m not in MODIFIERS for m in mods):
        return None
    if mods != [m for m in MODIFIERS if m in mods]:
        return None
    return mods, _BY_NAME[key]


def problem(combo: str) -> str:
    """Why a combo can't be a hot key, or ""."""
    parsed = parse(combo)
    if parsed is None:
        return "That isn't a key combination Buddy knows."
    mods, code = parsed
    if combo in RESERVED:
        return "Windows keeps that combination for itself – pick another."
    if not (set(mods) & {"Ctrl", "Alt", "Win"}) and code not in LONE_KEYS:
        return "Hold Ctrl, Alt or the Windows key too – on its own (or with Shift) it would get in the way of typing."
    return ""


def conflicts(combo: str) -> str:
    """What of Resolve's this combo takes while it's set ("Razor"), or ""."""
    return RESOLVE_SHORTCUTS.get(combo, "")


def windows_codes(combo: str) -> tuple[int, int] | None:
    """(RegisterHotKey's modifiers, virtual-key code) for a combo."""
    parsed = parse(combo)
    if parsed is None:
        return None
    mods, code = parsed
    bits = MOD_NOREPEAT
    for m in mods:
        bits |= _MOD_BITS[m]
    return bits, KEYS[code][1]


SUPPORTED = sys.platform == "win32"


if SUPPORTED:
    import ctypes
    import ctypes.wintypes as wt

    from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
    from PySide6.QtWidgets import QApplication

    class _Filter(QAbstractNativeEventFilter):
        def __init__(self, owner):
            super().__init__()
            self._owner = owner

        def nativeEventFilter(self, event_type, message):
            if bytes(event_type) == b"windows_generic_MSG":
                msg = wt.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and int(msg.wParam) in self._owner._ids:
                    self._owner.pressed.emit(self._owner._ids[int(msg.wParam)])
                    return True, 0
            return False, 0

    class GlobalHotkeys(QObject):
        """Buddy's registered combos. pressed(name) when one is pressed -
        name being whatever set() was given for it. Call from the UI thread
        only: Windows sends WM_HOTKEY to the thread that registered it."""

        pressed = Signal(str)
        _FIRST_ID = 0xB000    # out of the way of any other RegisterHotKey in the process

        def __init__(self, parent=None):
            super().__init__(parent)
            self._ids: dict[int, str] = {}
            self._filter = _Filter(self)
            QApplication.instance().installNativeEventFilter(self._filter)

        def set(self, combos: dict[str, str]) -> dict[str, str]:
            """Registers {name: combo} in place of everything before. Returns
            {name: why} for those Windows refused (another app has the combo)."""
            self.clear()
            failed = {}
            user32 = ctypes.windll.user32
            for n, (name, combo) in enumerate(combos.items()):
                codes = windows_codes(combo)
                if codes is None:
                    failed[name] = "That isn't a key combination Buddy knows."
                    continue
                hotkey_id = self._FIRST_ID + n
                if user32.RegisterHotKey(None, hotkey_id, *codes):
                    self._ids[hotkey_id] = name
                else:
                    failed[name] = "Another app already uses that combination."
            return failed

        def clear(self):
            user32 = ctypes.windll.user32
            for hotkey_id in self._ids:
                user32.UnregisterHotKey(None, hotkey_id)
            self._ids = {}
