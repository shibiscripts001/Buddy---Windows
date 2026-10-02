"""The Web tab's ad blocker (app/pages/web/filters.py, filter_lists.py,
engine.TrackerFilter): filter-list rules read and matched as the lists
mean them, element hiding, the lists kept and refreshed - all on small
made-up lists, never the network - and a real page with its ad script
blocked and its ad box hidden."""

import http.server
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.web import filters

LIST = """[Adblock Plus 2.0]
! Title: made up
||ads.example^
||tracker.example^$third-party
||cdn.example/ads/*$script
||cdn.example^$image,domain=news.example|~sport.news.example
/banner/ad_
-ad-unit.
@@||ads.example/allowed.js
||important.example^$important
@@||important.example^
||cancelled.example^
||cancelled.example^$badfilter
||firstparty.example/x.js$1p
||popup.example^$popup
||redirected.example/x.js$redirect=noopjs
||whole.example^$document
@@||nohide.example^$elemhide
@@||nogeneric.example^$generichide
##.ad-banner
###sponsor
news.example##.news-ad
~sport.news.example##.sport-free
news.example#@#.ad-banner
example.org##.thing:has-text(Sponsored)
example.org##+js(set, x, 1)
"""


def match(f, url, site, kind="script"):
    from urllib.parse import urlsplit
    rule = f.matches(url, urlsplit(url).hostname, site, kind)
    return rule.text if rule else None


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = filters.build([LIST])

    def test_domains_and_everything_under_them(self):
        self.assertEqual(match(self.f, "https://ads.example/a.js", "site.test"), "||ads.example^")
        self.assertEqual(match(self.f, "https://x.y.ads.example/a.js", "site.test"), "||ads.example^")
        self.assertIsNone(match(self.f, "https://notads.example/a.js", "site.test"))
        self.assertIsNone(match(self.f, "https://ads.example.org/a.js", "site.test"))

    def test_third_party_only(self):
        self.assertIsNotNone(match(self.f, "https://tracker.example/t.js", "site.test"))
        self.assertIsNone(match(self.f, "https://tracker.example/t.js", "www.tracker.example"))   # its own site

    def test_first_party_only(self):
        self.assertIsNotNone(match(self.f, "https://firstparty.example/x.js", "firstparty.example"))
        self.assertIsNone(match(self.f, "https://firstparty.example/x.js", "other.test"))

    def test_paths_and_types(self):
        self.assertIsNotNone(match(self.f, "https://cdn.example/ads/one.js", "site.test", "script"))
        self.assertIsNone(match(self.f, "https://cdn.example/ads/one.js", "site.test", "image"))
        self.assertIsNone(match(self.f, "https://cdn.example/lib/one.js", "site.test", "script"))

    def test_on_some_sites(self):
        self.assertIsNotNone(match(self.f, "https://cdn.example/p.png", "www.news.example", "image"))
        self.assertIsNone(match(self.f, "https://cdn.example/p.png", "sport.news.example", "image"))
        self.assertIsNone(match(self.f, "https://cdn.example/p.png", "other.test", "image"))

    def test_pieces_of_any_address(self):
        self.assertEqual(match(self.f, "https://site.test/img/banner/ad_1.png", "site.test", "image"), "/banner/ad_")
        self.assertEqual(match(self.f, "https://x.test/js/top-ad-unit.min.js", "x.test"), "-ad-unit.")
        self.assertIsNone(match(self.f, "https://site.test/img/banner/home.png", "site.test", "image"))

    def test_exceptions_and_important(self):
        self.assertIsNone(match(self.f, "https://ads.example/allowed.js", "site.test"))
        self.assertIsNotNone(match(self.f, "https://important.example/x.js", "site.test"))   # wins over @@

    def test_what_is_left_out(self):
        self.assertIsNone(match(self.f, "https://cancelled.example/x.js", "site.test"))      # badfilter
        self.assertIsNone(match(self.f, "https://popup.example/x.js", "site.test"))          # only popups
        self.assertIsNone(match(self.f, "https://redirected.example/x.js", "site.test"))     # a redirect
        self.assertGreater(self.f.counts["skipped"], 0)

    def test_a_page_itself_only_when_a_rule_says(self):
        self.assertIsNone(match(self.f, "https://ads.example/", "ads.example", "document"))
        self.assertIsNotNone(match(self.f, "https://whole.example/", "whole.example", "document"))


class HidingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = filters.build([LIST])

    def test_every_site_and_one_site(self):
        css = self.f.hiding_css("https://site.test/")
        self.assertIn(".ad-banner{display:none!important}", css)
        self.assertIn("#sponsor{", css)
        self.assertNotIn(".news-ad", css)
        news = self.f.hiding_css("https://www.news.example/story")
        self.assertIn(".news-ad{", news)
        self.assertNotIn(".ad-banner{", news)                                 # news.example#@#.ad-banner
        self.assertIn(".sport-free{", self.f.hiding_css("https://site.test/"))
        self.assertNotIn(".sport-free{", self.f.hiding_css("https://sport.news.example/"))

    def test_sites_that_turn_it_off(self):
        self.assertEqual(self.f.hiding_css("https://nohide.example/"), "")
        generic_off = self.f.hiding_css("https://nogeneric.example/")
        self.assertNotIn(".ad-banner", generic_off)

    def test_uBO_only_selectors_are_left_out(self):
        css = self.f.hiding_css("https://example.org/")
        self.assertNotIn("has-text", css)
        self.assertNotIn("+js", css)


class ListTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.folder = tempfile.mkdtemp(prefix="buddy_lists_")
        self.addCleanup(shutil.rmtree, self.folder, True)
        from pages.web import filter_lists
        self.fl = filter_lists

    def opener(self, body):
        def open_(request, timeout):
            self.assertIn("Buddy", request.get_header("User-agent"))
            return io.BytesIO(body)
        return open_

    def test_a_list_is_kept_and_an_error_page_isnt(self):
        good = ("[Adblock Plus 2.0]\n" + "||a.example^\n" * 200).encode()
        self.assertTrue(self.fl.fetch("easylist", self.folder, self.opener(good)))
        self.assertFalse(self.fl.fetch("easylist", self.folder, self.opener(b"<html>Service unavailable</html>")))
        with open(self.fl.path("easylist", self.folder), "rb") as fh:
            self.assertEqual(fh.read(), good)                                   # the last good copy stays

    def test_old_lists_are_fetched_again(self):
        for key in self.fl.LISTS:
            with open(self.fl.path(key, self.folder), "w", encoding="utf-8") as fh:
                fh.write("[Adblock Plus 2.0]\n||kept.example^\n")
        old = time.time() - (self.fl.UPDATE_DAYS + 1) * 86400
        os.utime(self.fl.path("easylist", self.folder), (old, old))
        lists = self.fl.FilterLists(folder=self.folder)
        got = []
        lists.ready.connect(got.append)
        with mock.patch.object(self.fl, "fetch", return_value=False) as fetched:
            lists.start()
            lists._thread.join(10)
        self.assertEqual([c.args[0] for c in fetched.call_args_list], ["easylist"])    # only the old one
        for _ in range(50):
            self.app.processEvents()
            if got:
                break
            time.sleep(0.02)
        self.assertIn("kept.example", got[0].plain_hosts)
        text, tone = lists.status()
        self.assertIn("Couldn't update EasyList", text)
        self.assertEqual(tone, "warning")

    def test_nothing_kept_and_offline(self):
        lists = self.fl.FilterLists(folder=self.folder)
        with mock.patch.object(self.fl, "fetch", side_effect=OSError("offline")):
            lists.start()
            lists._thread.join(10)
        self.app.processEvents()
        self.assertIsNone(lists.filters)
        self.assertIn("couldn't be downloaded", lists.status()[0])


class FilterTests(unittest.TestCase):
    """engine.TrackerFilter: which requests it refuses."""

    def test_modes_and_allowed_sites(self):
        from pages.web import engine
        f = engine.TrackerFilter()
        f.filters = filters.build([LIST])
        url = "https://cdn.example/ads/one.js"
        f.mode = "strong"
        self.assertTrue(f.blocks(url, "cdn.example", "site.test", "script"))
        self.assertTrue(f.blocks("https://www.google-analytics.com/a.js", "www.google-analytics.com", "site.test",
                                 "script"))                                    # the built-in list too
        f.mode = "trackers"
        self.assertFalse(f.blocks(url, "cdn.example", "site.test", "script"))
        self.assertTrue(f.blocks("https://www.google-analytics.com/a.js", "www.google-analytics.com", "site.test",
                                 "script"))
        f.mode = "strong"
        f.allowed = frozenset({"site.test"})
        self.assertFalse(f.blocks(url, "cdn.example", "www.site.test", "script"))   # ads allowed there
        f.mode = "off"
        f.allowed = frozenset()
        self.assertFalse(f.blocks(url, "cdn.example", "site.test", "script"))
        self.assertFalse(f.enabled)


class YouTubeAdsTests(unittest.TestCase):
    """YouTube's ad entries taken out of its player's data - on a stand-in
    page that says it's www.youtube.com - and nowhere else."""

    def load(self, base, on=True):
        from PySide6.QtCore import QEventLoop, QTimer, QUrl
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWidgets import QApplication
        from pages.web import engine
        app = QApplication.instance() or QApplication([])
        profile = QWebEngineProfile(app)
        with mock.patch.object(engine, "_youtube_ads", on):
            engine._put_youtube_ads_script(profile)
        page = QWebEnginePage(profile, app)
        self.addCleanup(page.deleteLater)
        done = []
        page.loadFinished.connect(done.append)
        page.setHtml("<script>var ytInitialPlayerResponse = {adPlacements: [1], playerAds: [2], videoDetails: {id: 'v'}};"
                     "window.parsed = JSON.parse('{\"playerResponse\": {\"adSlots\": [3], \"streamingData\": 1}}');"
                     "new Response('{\"adPlacements\": [4], \"keep\": 5}').json().then(r => window.fetched = r);"
                     "</script>", QUrl(base))

        def wait(ms):
            loop = QEventLoop()
            QTimer.singleShot(ms, loop.quit)
            loop.exec()

        for _ in range(60):
            if done:
                break
            wait(50)
        wait(200)
        got = []
        page.runJavaScript("JSON.stringify([ytInitialPlayerResponse, window.parsed, window.fetched])", 0, got.append)
        for _ in range(40):
            if got:
                break
            wait(50)
        return got[0]

    def test_the_ads_go_and_the_video_stays(self):
        self.assertEqual(self.load("https://www.youtube.com/watch?v=v"),
                         '[{"videoDetails":{"id":"v"}},{"playerResponse":{"streamingData":1}},{"keep":5}]')

    def test_other_sites_and_switched_off_are_left_alone(self):
        untouched = ('[{"adPlacements":[1],"playerAds":[2],"videoDetails":{"id":"v"}},'
                     '{"playerResponse":{"adSlots":[3],"streamingData":1}},{"adPlacements":[4],"keep":5}]')
        self.assertEqual(self.load("https://www.example.com/"), untouched)
        self.assertEqual(self.load("https://www.youtube.com/watch?v=v", on=False), untouched)


class _Site(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _Site.seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if self.path.endswith(".js") else "text/html")
        self.end_headers()
        if self.path.endswith(".js"):
            self.wfile.write(b"window.adRan = true;" if "advert" in self.path else b"window.appRan = true;")
        else:
            self.wfile.write(b"<title>Shop</title><div class='promo-box'>AD</div><p class='story'>story</p>"
                             b"<script src='/js/advert.js'></script><script src='/js/app.js'></script>")

    def log_message(self, *args):
        pass


class RealPageTests(unittest.TestCase):
    """A local site in the browser's own profile: its ad script never
    asked for, its ad box hidden, its own content left be."""

    def test_blocked_and_hidden(self):
        from PySide6.QtCore import QEventLoop, QTimer, QUrl
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from pages.web import engine
        if engine._profile is None:
            engine.PROFILE_DIR = tempfile.mkdtemp(prefix="buddy_filters_profile_")
        server = http.server.HTTPServer(("127.0.0.1", 0), _Site)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        f = engine.tracker_filter()
        saved = (f.mode, f.filters, f.allowed)
        self.addCleanup(lambda: (setattr(f, "mode", saved[0]), setattr(f, "filters", saved[1]),
                                 setattr(f, "allowed", saved[2])))
        f.mode, f.allowed = "strong", frozenset()
        f.filters = filters.build(["/js/advert.\n127.0.0.1##.promo-box\n"])
        tab = mock.Mock(private=False, url="x")
        view = QWebEngineView()
        self.addCleanup(view.deleteLater)
        page = engine.TabPage(tab, view)
        tab.web = view
        view.setPage(page)
        done = []
        page.loadFinished.connect(done.append)
        _Site.seen = []
        page.load(QUrl(f"http://127.0.0.1:{server.server_port}/"))

        def wait(ms):
            loop = QEventLoop()
            QTimer.singleShot(ms, loop.quit)
            loop.exec()

        for _ in range(100):
            if done:
                break
            wait(50)
        wait(300)
        got = []
        page.runJavaScript("JSON.stringify([!!window.adRan, !!window.appRan, getComputedStyle(document.querySelector('.promo-box')).display,"
                           " getComputedStyle(document.querySelector('.story')).display])", 0, got.append)
        for _ in range(40):
            if got:
                break
            wait(50)
        self.assertEqual(got[0], '[false,true,"none","block"]')
        self.assertIn("/js/app.js", _Site.seen)
        self.assertNotIn("/js/advert.js", _Site.seen)                         # never even asked for


if __name__ == "__main__":
    unittest.main()
