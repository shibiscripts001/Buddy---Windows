"""The Web tab's sign-ins kept encrypted (app/pages/web/cookie_vault.py):
Windows' DPAPI, a cookie to and from what's saved, the old plain database
brought over, and the real thing - a local site's cookies saved, read back
by a fresh profile, never on disk in plain text. Throwaway profiles only."""

import base64
import http.server
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest

import _paths  # noqa: F401
from PySide6.QtCore import QDateTime, QEventLoop, QTimer, QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWidgets import QApplication

from pages.web import cookie_vault as cv

MARK = "PLAINTEXT_SIGN_IN_7781"
only_windows = unittest.skipUnless(cv.available, "DPAPI is Windows'")


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _cookie(name=b"SID", value=b"a;b c", days=30, **flags):
    cookie = QNetworkCookie(name, value)
    cookie.setDomain(flags.get("domain", ".example.com"))
    cookie.setPath("/")
    if days is not None:
        cookie.setExpirationDate(QDateTime.currentDateTime().addDays(days))
    cookie.setSecure(flags.get("secure", True))
    cookie.setHttpOnly(flags.get("http_only", True))
    cookie.setSameSitePolicy(flags.get("same_site", QNetworkCookie.SameSite.Lax))
    return cookie


@only_windows
class DpapiTests(unittest.TestCase):
    def test_round_trip_and_tampering(self):
        sealed = cv.protect(b"secret sign-in")
        self.assertNotIn(b"secret", sealed)
        self.assertEqual(cv.unprotect(sealed), b"secret sign-in")
        broken = bytearray(sealed)
        broken[len(broken) // 2] ^= 0xFF
        with self.assertRaises(OSError):
            cv.unprotect(bytes(broken))


class RecordTests(unittest.TestCase):
    def test_a_cookie_and_back(self):
        now = QDateTime.currentMSecsSinceEpoch()
        back = cv.from_record(cv.to_record(_cookie()), now)
        self.assertEqual((bytes(back.name()), bytes(back.value()), back.domain()), (b"SID", b"a;b c", ".example.com"))
        self.assertTrue(back.isSecure() and back.isHttpOnly())
        self.assertEqual(back.sameSitePolicy(), QNetworkCookie.SameSite.Lax)
        self.assertIsNone(cv.to_record(_cookie(days=None)))                  # session cookies aren't kept
        self.assertIsNone(cv.from_record(cv.to_record(_cookie(days=-1)), now))   # nor expired ones
        self.assertIsNone(cv.from_record({"name": "x"}, now))
        self.assertEqual(cv.origin(_cookie()).toString(), "https://example.com/")

    def test_the_old_plain_database(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "Cookies")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, path TEXT, expires_utc INTEGER, "
                    "is_secure INTEGER, is_httponly INTEGER, samesite INTEGER, is_persistent INTEGER)")
        later = (QDateTime.currentMSecsSinceEpoch() + 86_400_000 + cv._EPOCH_1601_MS) * 1000
        con.executemany("INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?)", [
            (".youtube.com", "PREF", "f6=40000000", "/", later, 1, 0, 0, 1),
            ("example.com", "session", "x", "/", 0, 0, 0, -1, 0)])
        con.commit()
        con.close()
        records = cv.read_plain_db(path)
        self.assertEqual(len(records), 1)                                      # the lasting one
        cookie = cv.from_record(records[0], QDateTime.currentMSecsSinceEpoch())
        self.assertEqual((cookie.domain(), bytes(cookie.value())), (".youtube.com", b"f6=40000000"))
        self.assertEqual(cookie.sameSitePolicy(), QNetworkCookie.SameSite.None_)
        self.assertEqual(cv.read_plain_db(os.path.join(folder, "missing")), [])


class WipeTests(unittest.TestCase):
    """Clear cookies and site data: the whole profile folder goes as Buddy
    next starts - only when asked."""

    def test_only_when_asked(self):
        from pages.web import engine
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        os.makedirs(os.path.join(folder, "Local Storage", "leveldb"))
        open(os.path.join(folder, "Local Storage", "leveldb", "000003.log"), "w").close()
        open(os.path.join(folder, cv.FILE_NAME), "w").close()
        self.assertFalse(engine.wipe_if_asked(folder))
        self.assertEqual(sorted(os.listdir(folder)), sorted([cv.FILE_NAME, "Local Storage"]))
        open(os.path.join(folder, engine.WIPE_MARK), "w").close()
        self.assertTrue(engine.wipe_if_asked(folder))
        self.assertEqual(os.listdir(folder), [])                              # marker and all
        self.assertFalse(engine.wipe_if_asked(folder))                       # once


class _Site(http.server.BaseHTTPRequestHandler):
    sent = []

    def do_GET(self):
        _Site.sent.append(self.headers.get("Cookie") or "")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if self.path == "/set":
            self.send_header("Set-Cookie", f"signin={MARK}; Max-Age=86400; Path=/; HttpOnly")
            self.send_header("Set-Cookie", "tab=session-only; Path=/")
        self.end_headers()
        self.wfile.write(b"<title>site</title>ok")

    def log_message(self, *args):
        pass


@only_windows
class VaultTests(unittest.TestCase):
    """Real profiles, a site on 127.0.0.1."""

    def setUp(self):
        from PySide6.QtWebEngineCore import QWebEngineProfile
        self.app = QApplication.instance() or QApplication([])
        self.Profile = QWebEngineProfile
        self.folder = tempfile.mkdtemp(prefix="buddy_vault_")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        _Site.sent = []

    def profile(self, name):
        profile = self.Profile(name, self.app)
        profile.setPersistentStoragePath(os.path.join(self.folder, name))
        profile.setPersistentCookiesPolicy(self.Profile.NoPersistentCookies)
        vault = cv.CookieVault(profile, self.folder)
        vault.load()
        return profile, vault

    def visit(self, profile, path):
        from PySide6.QtWebEngineCore import QWebEnginePage
        page = QWebEnginePage(profile)
        done = []
        page.loadFinished.connect(done.append)
        page.load(QUrl(f"http://127.0.0.1:{self.server.server_port}{path}"))
        for _ in range(100):
            if done:
                break
            _wait(50)
        _wait(300)
        page.deleteLater()
        _wait(100)

    def plain_anywhere(self):
        """A file holding the sign-in in plain text. Ones Chromium has
        locked open are skipped - none of them is a cookie database."""
        for root, _dirs, files in os.walk(self.folder):
            for name in files:
                self.assertNotIn(name, cv.PLAIN_DB)                          # no cookie database at all
                try:
                    with open(os.path.join(root, name), "rb") as fh:
                        if MARK.encode() in fh.read():
                            return os.path.join(root, name)
                except PermissionError:
                    continue
        return None

    def test_signed_in_next_time_and_never_in_plain_text(self):
        first, vault = self.profile("first")
        self.visit(first, "/set")
        for _ in range(40):
            if any(k[2] == b"signin" for k in vault.cookies):
                break
            _wait(50)
        self.assertTrue(vault.save())
        self.assertTrue(os.path.exists(vault.path))
        self.assertEqual([k[2] for k in vault.cookies], [b"signin"])          # the session one isn't kept
        self.assertIsNone(self.plain_anywhere())                             # not in the vault, nor a database
        # Buddy starts again: a new profile, the same vault - still signed in.
        second, _vault2 = self.profile("second")
        _wait(300)
        self.visit(second, "/see")
        self.assertIn(f"signin={MARK}", _Site.sent[-1])
        self.assertNotIn("session-only", _Site.sent[-1])
        # Clearing site data signs out for good.
        _vault2.forget()
        third, vault3 = self.profile("third")
        self.assertEqual(vault3.cookies, {})

    def test_the_plain_database_is_brought_over_then_deleted(self):
        path = os.path.join(self.folder, "Cookies")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, path TEXT, expires_utc INTEGER, "
                    "is_secure INTEGER, is_httponly INTEGER, samesite INTEGER, is_persistent INTEGER)")
        later = (QDateTime.currentMSecsSinceEpoch() + 86_400_000 + cv._EPOCH_1601_MS) * 1000
        con.execute("INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?)",
                    ("127.0.0.1", "signin", MARK, "/", later, 0, 1, -1, 1))
        con.commit()
        con.close()
        open(path + "-journal", "wb").close()
        profile, vault = self.profile("moved")
        self.assertFalse(os.path.exists(path) or os.path.exists(path + "-journal"))
        self.assertIsNone(self.plain_anywhere())
        _wait(300)
        self.visit(profile, "/see")
        self.assertIn(f"signin={MARK}", _Site.sent[-1])

    def test_a_vault_it_cant_read_starts_signed_out(self):
        with open(os.path.join(self.folder, cv.FILE_NAME), "wb") as fh:
            fh.write(b"not DPAPI at all")
        _profile, vault = self.profile("unreadable")
        self.assertEqual(vault.cookies, {})


if __name__ == "__main__":
    unittest.main()
