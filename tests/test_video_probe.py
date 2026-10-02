"""Buddy's player is only pointed at a page's video after it's been looked
at (app/pages/web/video_probe.py): a public host, no redirect into this PC
or the user's network, and a plain media file - never a playlist."""

import http.server
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import _paths  # noqa: F401
from pages.web import video_probe

PUBLIC = "93.184.216.34"
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20


def resolver(mapping):
    def resolve(host, port):
        if host not in mapping:
            raise OSError("no such host")
        return [(2, 1, 6, "", (address, 0)) for address in mapping[host]]
    return resolve


def fetch(final, head):
    return lambda request, allow_private, resolve: (final, head)


class HostTests(unittest.TestCase):
    def test_which_hosts_are_private(self):
        resolve = resolver({"cdn.example": [PUBLIC], "rebind.example": [PUBLIC, "10.0.0.7"],
                            "lan.example": ["192.168.1.9"]})
        for host in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.0.5", "172.16.0.1", "169.254.169.254", "::1",
                     "fe80::1", "0.0.0.0", "printer.local", "lan.example", "rebind.example", "nowhere.example", ""):
            self.assertTrue(video_probe.host_is_private(host, resolve), host)
        for host in (PUBLIC, "cdn.example", "8.8.8.8"):
            self.assertFalse(video_probe.host_is_private(host, resolve), host)


class SniffTests(unittest.TestCase):
    def test_what_counts_as_a_media_file(self):
        webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 16
        ts = b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 8
        for head in (MP4, webm, ts, b"OggS" + b"\x00" * 12, b"RIFF\x00\x00\x00\x00WAVEfmt "):
            self.assertTrue(video_probe.is_media_start(head), head[:8])
        for head in (b"#EXTM3U\n#EXT-X-VERSION:3\n", b"<?xml version='1.0'?><MPD/>", b"<html><body>hi</body></html>",
                     b"MZ\x90\x00" + b"\x00" * 20, b"", b"short"):
            self.assertFalse(video_probe.is_media_start(head), head[:8])


class ProbeTests(unittest.TestCase):
    def test_it_hands_back_the_address_it_checked(self):
        resolve = resolver({"cdn.example": [PUBLIC], "edge.example": [PUBLIC]})
        self.assertEqual(video_probe.probe("https://cdn.example/a.mp4", resolve=resolve,
                                           fetch=fetch("https://edge.example/real.mp4", MP4)),
                         "https://edge.example/real.mp4")

    def test_it_refuses_the_rest(self):
        resolve = resolver({"cdn.example": [PUBLIC], "inside.example": ["10.0.0.9"]})
        playlist = b"#EXTM3U\n#EXTINF:5,\nfile:///C:/secret\n"
        for url, final, head, why in (
                ("http://127.0.0.1/a.mp4", "http://127.0.0.1/a.mp4", MP4, "private network"),
                ("https://inside.example/a.mp4", "https://inside.example/a.mp4", MP4, "private network"),
                ("file:///C:/Windows/win.ini", "file:///C:/Windows/win.ini", MP4, "isn't on the web"),
                ("https://cdn.example/list.m3u8", "https://cdn.example/list.m3u8", playlist, "plain video file"),
                ("https://cdn.example/a.mp4", "http://10.0.0.9/internal", MP4, "private network")):   # redirected inside
            with self.assertRaises(video_probe.VideoRefused) as caught:
                video_probe.probe(url, resolve=resolve, fetch=fetch(final, head))
            self.assertIn(why, str(caught.exception), url)

        def missing(*_args):
            raise urllib.error.HTTPError("u", 404, "no", {}, None)
        with self.assertRaises(video_probe.VideoRefused):
            video_probe.probe("https://cdn.example/a.mp4", resolve=resolve, fetch=missing)

    def test_a_page_on_the_users_own_network_may_ask_for_what_is_on_it(self):
        resolve = resolver({})
        self.assertEqual(video_probe.probe("http://10.0.0.9/a.mp4", allow_private=True, resolve=resolve,
                                           fetch=fetch("http://10.0.0.9/a.mp4", MP4)), "http://10.0.0.9/a.mp4")

    def test_a_redirect_into_the_network_is_stopped_on_the_way(self):
        handler = video_probe._CheckedRedirects(False, resolver({"cdn.example": [PUBLIC]}))
        request = urllib.request.Request("https://cdn.example/a.mp4")
        for target in ("http://127.0.0.1:8080/admin", "http://192.168.1.1/", "file:///C:/x"):
            with self.assertRaises(video_probe.VideoRefused):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/b.mp4"))

    def test_the_real_request_follows_nothing_into_the_network(self):
        """Two real local servers: the second is "inside"; the first redirects to it."""
        seen = []

        class Inside(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        inside = http.server.HTTPServer(("127.0.0.1", 0), Inside)

        class Outside(Inside):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{inside.server_port}/admin")
                self.end_headers()

        outside = http.server.HTTPServer(("127.0.0.1", 0), Outside)
        for server in (inside, outside):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        # "video.test" looks public to the checker and is really the first server.
        resolve = lambda host, port: [(2, 1, 6, "", (PUBLIC if host == "video.test" else "127.0.0.1", 0))]  # noqa: E731
        real_open = urllib.request.OpenerDirector.open

        def reach(director, request, *args, **kwargs):
            if isinstance(request, urllib.request.Request) and "video.test" in request.full_url:
                request.full_url = request.full_url.replace("video.test", "127.0.0.1")
            return real_open(director, request, *args, **kwargs)

        with mock.patch.object(urllib.request.OpenerDirector, "open", reach):
            with self.assertRaises(video_probe.VideoRefused):
                video_probe.probe(f"http://video.test:{outside.server_port}/clip.mp4", resolve=resolve)
        self.assertEqual(seen, [])


try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from pages.web import video_window
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PlayerGateTests(unittest.TestCase):
    """What the player window does with a page's address."""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.videos = video_window.Videos(mock.Mock())
        self.opened, self.refusals = [], []
        made = mock.patch.object(video_window, "VideoWindow", side_effect=lambda *a: self.opened.append(a) or mock.Mock())
        made.start()
        self.addCleanup(made.stop)

    def wait(self, check, ms=3000):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        timer = QTimer()
        timer.timeout.connect(lambda: loop.quit() if check() else None)
        timer.start(20)
        loop.exec()
        timer.stop()

    def test_a_checked_address_is_what_the_player_gets(self):
        with mock.patch.object(video_window.video_probe, "probe", return_value="https://edge.example/final.mp4"):
            self.videos.play("https://cdn.example/a.mp4", "T", 1.5, page_host="news.example",
                             refused=self.refusals.append)
            self.wait(lambda: self.opened)
        self.assertEqual([a[1] for a in self.opened], ["https://edge.example/final.mp4"])
        self.assertEqual(self.refusals, [])

    def test_a_refused_address_opens_nothing_and_says_why(self):
        refuse = video_window.video_probe.VideoRefused("nope")
        with mock.patch.object(video_window.video_probe, "probe", side_effect=refuse):
            self.videos.play("http://127.0.0.1/a.mp4", refused=self.refusals.append)
            self.wait(lambda: self.refusals)
        self.assertEqual((self.opened, self.refusals), ([], ["nope"]))

    def test_a_page_on_a_private_address_may_ask_for_private_ones(self):
        seen = []
        with mock.patch.object(video_window.video_probe, "probe",
                               side_effect=lambda url, allow_private=False: seen.append(allow_private) or url):
            self.videos.play("http://192.168.1.5/a.mp4", page_host="192.168.1.5")
            self.wait(lambda: seen)
        self.assertEqual(seen, [True])

    def test_only_a_few_windows_at_once(self):
        self.videos.windows = {f"https://x.example/{n}.mp4": mock.Mock() for n in range(video_window.MAX_WINDOWS)}
        self.videos.play("https://x.example/new.mp4", refused=self.refusals.append)
        self.assertEqual(len(self.refusals), 1)
        self.assertEqual(self.opened, [])


if __name__ == "__main__":
    unittest.main()
