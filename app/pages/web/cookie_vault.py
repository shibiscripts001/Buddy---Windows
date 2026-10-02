#!/usr/bin/env python3
"""
The Web tab's sign-ins, kept encrypted (pages/web/engine.py uses it).

Qt WebEngine keeps a profile's cookies in a plain database - anyone or
anything able to copy that file could be signed in as the user. Chrome
encrypts the same file with the Windows account. So the browser's profile
keeps its cookies in memory only, and this keeps them on disk, encrypted
with Windows' DPAPI (CryptProtectData - only this Windows account on this
PC can read it back): every lasting cookie the profile has, saved a moment
after it changes and again as Buddy closes, and put back as it starts.
Session cookies go when Buddy closes, as in Chrome.

The first time, what the old plain database held is brought over and the
database deleted.

Where DPAPI isn't (not Windows), `available` is False and the profile
keeps its own cookie database as before.
"""

import base64
import ctypes
import datetime
import json
import os
import sqlite3
import sys

from PySide6.QtCore import QDateTime, QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkCookie

available = sys.platform == "win32"

FILE_NAME = "sign-ins.dat"
PLAIN_DB = ("Cookies", "Cookies-journal")
SAVE_DELAY_MS = 2_000
MAX_COOKIES = 20_000                  # far past what anyone has; a runaway site can't fill the disk
_ENTROPY = b"Buddy web sign-ins v1"
_FORMAT = 1
_SAME_SITE = {"default": QNetworkCookie.SameSite.Default, "none": QNetworkCookie.SameSite.None_,
              "lax": QNetworkCookie.SameSite.Lax, "strict": QNetworkCookie.SameSite.Strict}


# ------------------------------------------------------------------ DPAPI --

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


def protect(data):
    return _dpapi(data, True)


def unprotect(data):
    return _dpapi(data, False)


# ---------------------------------------------------------------- cookies --

def key(cookie):
    return (cookie.domain(), cookie.path(), bytes(cookie.name()))


def to_record(cookie):
    """A lasting cookie as JSON-able values, or None for a session cookie."""
    if cookie.isSessionCookie():
        return None
    same = cookie.sameSitePolicy()
    return {"name": base64.b64encode(bytes(cookie.name())).decode("ascii"),
            "value": base64.b64encode(bytes(cookie.value())).decode("ascii"),
            "domain": cookie.domain(), "path": cookie.path() or "/",
            "expires": cookie.expirationDate().toMSecsSinceEpoch(),
            "secure": cookie.isSecure(), "http_only": cookie.isHttpOnly(),
            "same_site": next((k for k, v in _SAME_SITE.items() if v == same), "default")}


def from_record(record, now_ms):
    """The cookie back, or None if it's expired or not one."""
    try:
        expires = int(record["expires"])
        if expires <= now_ms:
            return None
        cookie = QNetworkCookie(base64.b64decode(record["name"]), base64.b64decode(record["value"]))
        cookie.setDomain(str(record["domain"]))
        cookie.setPath(str(record.get("path") or "/"))
        cookie.setExpirationDate(QDateTime.fromMSecsSinceEpoch(expires))
        cookie.setSecure(bool(record.get("secure")))
        cookie.setHttpOnly(bool(record.get("http_only")))
        cookie.setSameSitePolicy(_SAME_SITE.get(record.get("same_site"), QNetworkCookie.SameSite.Default))
    except (KeyError, TypeError, ValueError):
        return None
    return cookie if cookie.name() else None


def origin(cookie):
    """The address a cookie belongs to, for QWebEngineCookieStore.setCookie."""
    scheme = "https" if cookie.isSecure() else "http"
    return QUrl(f"{scheme}://{cookie.domain().lstrip('.')}{cookie.path() or '/'}")


# Chromium's own database (the old plain one): times are microseconds
# since 1601; samesite -1 unset, 0 none, 1 lax, 2 strict.
_EPOCH_1601_MS = int((datetime.datetime(1970, 1, 1) - datetime.datetime(1601, 1, 1)).total_seconds() * 1000)
_DB_SAME_SITE = {-1: "default", 0: "none", 1: "lax", 2: "strict"}


def read_plain_db(path):
    """Records from the plain database Qt WebEngine kept - lasting cookies
    with a plain value (it never encrypted them) - or [] if it can't be read."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT host_key, name, value, path, expires_utc, is_secure, is_httponly, "
                               "samesite, is_persistent FROM cookies").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for host, name, value, path, expires, secure, http_only, same_site, persistent in rows:
        if not persistent or not name or not expires:
            continue
        out.append({"name": base64.b64encode(str(name).encode()).decode("ascii"),
                    "value": base64.b64encode(str(value).encode()).decode("ascii"),
                    "domain": host, "path": path or "/", "expires": expires // 1000 - _EPOCH_1601_MS,
                    "secure": bool(secure), "http_only": bool(http_only),
                    "same_site": _DB_SAME_SITE.get(same_site, "default")})
    return out


class CookieVault(QObject):
    """A profile's lasting cookies, kept in `folder`/sign-ins.dat. Set the
    profile to keep no cookies of its own (NoPersistentCookies) before
    making this, then call load() before its first page."""

    def __init__(self, profile, folder, parent=None):
        super().__init__(parent or profile)
        self.profile = profile
        self.folder = folder
        self.path = os.path.join(folder, FILE_NAME)
        self.cookies = {}                     # key() -> record
        self._loading = False
        self._timer = QTimer(self, singleShot=True, interval=SAVE_DELAY_MS, timeout=self.save)
        store = profile.cookieStore()
        store.cookieAdded.connect(self._added)
        store.cookieRemoved.connect(self._removed)

    # ---------------------------------------------------------------- in --
    def load(self):
        """Puts back what was saved, plus - once - what the old plain
        database held (which is then deleted)."""
        now = QDateTime.currentMSecsSinceEpoch()
        records = self._read()
        plain = os.path.join(self.folder, PLAIN_DB[0])
        moved = os.path.exists(plain)
        if moved:
            records = read_plain_db(plain) + records          # what's saved wins: it's newer
        store = self.profile.cookieStore()
        self._loading = True
        try:
            for record in records[-MAX_COOKIES:]:
                cookie = from_record(record, now)
                if cookie is not None:
                    self.cookies[key(cookie)] = to_record(cookie)
                    store.setCookie(cookie, origin(cookie))
        finally:
            self._loading = False
        if moved:
            self.save()                        # the encrypted copy first, then the plain one goes
            for name in PLAIN_DB:
                try:
                    os.remove(os.path.join(self.folder, name))
                except OSError:
                    pass
        return len(self.cookies)

    def _read(self):
        try:
            with open(self.path, "rb") as fh:
                data = json.loads(unprotect(fh.read()).decode("utf-8"))
        except (OSError, ValueError):
            return []                          # none yet, or another account's / PC's: start signed out
        if not isinstance(data, dict) or data.get("format") != _FORMAT or not isinstance(data.get("cookies"), list):
            return []
        return [r for r in data["cookies"] if isinstance(r, dict)]

    # ------------------------------------------------------------ changes --
    def _added(self, cookie):
        record = to_record(cookie)
        if record is None:
            self.cookies.pop(key(cookie), None)
        elif len(self.cookies) < MAX_COOKIES or key(cookie) in self.cookies:
            self.cookies[key(cookie)] = record
        if not self._loading:
            self._timer.start()

    def _removed(self, cookie):
        if self.cookies.pop(key(cookie), None) is not None and not self._loading:
            self._timer.start()

    # --------------------------------------------------------------- out --
    def save(self):
        """Written whole to a new file, then put in place, so a crash
        mid-way leaves the last one."""
        self._timer.stop()
        now = QDateTime.currentMSecsSinceEpoch()
        records = [r for r in self.cookies.values() if r["expires"] > now]
        data = json.dumps({"format": _FORMAT, "cookies": records}, separators=(",", ":")).encode("utf-8")
        try:
            os.makedirs(self.folder, exist_ok=True)
            temp = self.path + ".new"
            with open(temp, "wb") as fh:
                fh.write(protect(data))
            os.replace(temp, self.path)
        except OSError:
            return False
        return True

    def forget(self):
        """Signed out everywhere: nothing kept."""
        self.cookies.clear()
        self.save()
