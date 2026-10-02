#!/usr/bin/env python3
"""
Keeping a secret (an AI provider's API key) out of a plain settings file.

lock() turns it into "dpapi:<base64>", sealed by Windows to this Windows
account (DPAPI): the settings file, its backup, a copied support folder or
a synced one then hold nothing another person or PC can use. unlock() gives
the key back, and passes an unlocked (older) value through unchanged, so
settings written before this still work until they're locked.

This keeps a key from files left lying around - it can't hide it from
something already running as this user. Off Windows there is no DPAPI, so
a value is stored as typed.

No Qt, so it's unit-tested.
"""

import base64
import ctypes
import sys

PREFIX = "dpapi:"
_ENTROPY = b"Buddy secrets v1"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(data, encrypt):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source, _keep = _blob(data)
    entropy, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    run = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN: never a prompt.
    if not run(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out)):
        raise OSError(ctypes.GetLastError(), "DPAPI refused")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


available = sys.platform == "win32"


def is_locked(text):
    return isinstance(text, str) and text.startswith(PREFIX)


def lock(text):
    """`text` sealed to this Windows account ("" stays ""; one already
    locked, or any text where there's no DPAPI, comes back unchanged)."""
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text or is_locked(text) or not available:
        return text
    try:
        return PREFIX + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")
    except OSError:
        return text                       # better a working key than none: it's stored as typed


def unlock(text):
    """The secret `text` holds. A locked value that this account can't open
    (another PC's or user's file) is "" - the key has to be entered again."""
    if not isinstance(text, str):
        return ""
    if not is_locked(text):
        return text
    if not available:
        return ""
    try:
        return _dpapi(base64.b64decode(text[len(PREFIX):], validate=True), False).decode("utf-8")
    except (OSError, ValueError):
        return ""
