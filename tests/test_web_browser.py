"""The Web tab (app/pages/web/): what the address bar goes to, which tabs
sleep (and that sound, a kept-awake tab and a never-sleep site keep one
awake), the tracker filter, what's saved between runs and the new-tab
page - and, with Qt, the browser itself on local pages: tabs that only
load when first shown, sleeping and waking, the settings. Never the real
browser profile or settings."""

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.web import browser as b

try:
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class AddressTests(unittest.TestCase):
    def test_addresses_and_searches(self):
        self.assertEqual(b.address_to_url("youtube.com"), "https://youtube.com")
        self.assertEqual(b.address_to_url("  https://frame.io/x?y=1 "), "https://frame.io/x?y=1")
        self.assertEqual(b.address_to_url("localhost:3000/app"), "http://localhost:3000/app")
        self.assertEqual(b.address_to_url("192.168.1.20:8080"), "https://192.168.1.20:8080")
        self.assertEqual(b.address_to_url("davinci resolve fusion"),
                         "https://duckduckgo.com/?q=davinci+resolve+fusion")
        self.assertEqual(b.address_to_url("fusion", "google"), "https://www.google.com/search?q=fusion")
        self.assertEqual(b.address_to_url("what is 4:2:2?", "bing"), "https://www.bing.com/search?q=what+is+4%3A2%3A2%3F")
        self.assertEqual(b.address_to_url("readme.txt"), "https://duckduckgo.com/?q=readme.txt")   # not a site
        self.assertTrue(b.address_to_url(r"D:\Footage\a.png").startswith("file:///D:/Footage/a.png"))
        self.assertIsNone(b.address_to_url("   "))
        self.assertTrue(b.address_to_url("cats", "nonsense").startswith("https://duckduckgo.com/"))

    def test_sites(self):
        self.assertEqual(b.site_of("https://www.YouTube.com/watch"), "youtube.com")
        self.assertEqual(b.site_of("not a url"), "")
        self.assertEqual(b.clean_sites("https://music.youtube.com/watch?v=1\n\nopen.spotify.com\nOPEN.spotify.com\n x y "),
                         ["music.youtube.com", "open.spotify.com"])
        self.assertEqual(b.clean_sites(["youtube.com", 5]), ["youtube.com"])
        self.assertTrue(b.on_site("https://music.youtube.com/x", ["youtube.com"]))
        self.assertFalse(b.on_site("https://notyoutube.com/", ["youtube.com"]))


class RegionTests(unittest.TestCase):
    def test_searches_ask_for_the_regions_results(self):
        self.assertEqual(b.search_url("fusion", "google", "JP"), "https://www.google.com/search?q=fusion&gl=JP")
        self.assertEqual(b.search_url("fusion", "duckduckgo", "GB"), "https://duckduckgo.com/?q=fusion&kl=uk-en")
        self.assertEqual(b.search_url("fusion", "bing", "DE"), "https://www.bing.com/search?q=fusion&cc=DE")
        self.assertEqual(b.search_url("fusion", "brave", "FR"), "https://search.brave.com/search?q=fusion&country=fr")
        self.assertEqual(b.search_url("fusion", "google", b.AUTO_REGION), "https://www.google.com/search?q=fusion")
        self.assertEqual(b.search_url("fusion", "google", "XX"), "https://www.google.com/search?q=fusion")
        self.assertEqual(b.address_to_url("color grading", "google", "AU"),
                         "https://www.google.com/search?q=color+grading&gl=AU")
        self.assertEqual(b.address_to_url("youtube.com", "google", "AU"), "https://youtube.com")     # not a search

    def test_what_sites_are_told(self):
        self.assertEqual(b.accept_language("en", "JP"), "en-JP,en;q=0.9,ja;q=0.8")
        self.assertEqual(b.accept_language("ja", "JP"), "ja-JP,ja;q=0.9,en;q=0.7")
        self.assertEqual(b.accept_language("zh-Hans", "TW"), "zh-TW,zh;q=0.9,en;q=0.7")
        self.assertEqual(b.accept_language("en", "US"), "en-US,en;q=0.9")
        self.assertIsNone(b.accept_language("en", b.AUTO_REGION))

    def test_the_new_tab_search_carries_it(self):
        colors = dict.fromkeys(("bg", "card", "text", "dim", "accent", "border", "font"), "#000000")
        self.assertIn('<input type="hidden" name="kl" value="jp-jp">', b.start_page(colors, [], "duckduckgo", region="JP"))
        self.assertNotIn('type="hidden"', b.start_page(colors, [], "duckduckgo"))


class AutofillTests(unittest.TestCase):
    def test_finishing_a_site(self):
        ranked = b.ranked_sites({}, now=0)
        self.assertEqual(b.autofill("goo", ranked), "google.com")
        self.assertEqual(b.autofill("Goo", ranked), "Google.com")                 # what was typed, as typed
        self.assertEqual(b.autofill("www.you", ranked), "www.youtube.com")
        self.assertEqual(b.autofill("https://fra", ranked), "https://frame.io")
        for nothing in ("", "google.com", "google.com/", "goo gle", "zzzz"):
            self.assertIsNone(b.autofill(nothing, ranked), nothing)

    def test_the_users_own_sites_come_first(self):
        now = 1_000_000
        sites = {}
        for _ in range(3):
            b.record_site(sites, "https://gooey.dev/a/b?c", now)
        self.assertEqual(sites, {"gooey.dev": [3, 0, now]})                       # the site, not the page
        self.assertEqual(b.autofill("goo", b.ranked_sites(sites, now=now)), "gooey.dev")
        b.record_site(sites, "https://www.google.com/search?q=x", now, typed=True)
        b.record_site(sites, "https://www.google.com/", now, typed=True)
        self.assertEqual(b.autofill("goo", b.ranked_sites(sites, now=now)), "google.com")   # typed beats clicked
        ranked = b.ranked_sites({}, links=["https://www.frontier.tv/"], now=now)
        self.assertEqual(b.autofill("fr", ranked), "frontier.tv")                 # link bar links count
        b.record_site(sites, "file:///D:/x.html", now)
        b.record_site(sites, "about:blank", now)
        self.assertEqual(set(sites), {"gooey.dev", "google.com"})

    def test_only_so_many_are_kept(self):
        sites = {f"site{i}.com": [i, 0, 0] for i in range(b.MAX_SITES_KEPT)}
        b.record_site(sites, "https://new.com", 0)
        self.assertEqual(len(sites), b.MAX_SITES_KEPT)
        self.assertNotIn("site0.com", sites)                                      # the least used goes
        self.assertIn("new.com", sites)
        self.assertEqual(b.ranked_sites({"bad": "junk", 5: [1]}, common=())[:1], ["bad"])   # junk doesn't break it


class SleepTests(unittest.TestCase):
    def tab(self, **values):
        return {"url": "https://a.com", "visible": False, "audible": False, "awake": False, "asleep": False,
                "seen": 1000, **values}

    def test_a_background_tab_sleeps_after_its_time(self):
        self.assertFalse(b.should_sleep(self.tab(), 1000 + 14 * 60, 15, []))
        self.assertTrue(b.should_sleep(self.tab(), 1000 + 15 * 60, 15, []))

    def test_what_keeps_one_awake(self):
        late = 1000 + 3600
        for why, tab, sites, minutes in (("on screen", self.tab(visible=True), [], 15),
                                         ("playing sound", self.tab(audible=True), [], 15),
                                         ("kept awake", self.tab(awake=True), [], 15),
                                         ("a never-sleep site", self.tab(url="https://music.youtube.com"), ["youtube.com"], 15),
                                         ("sleeping is off", self.tab(), [], 0),
                                         ("already asleep", self.tab(asleep=True), [], 15),
                                         ("the new-tab page", self.tab(url=""), [], 15)):
            self.assertFalse(b.should_sleep(tab, late, minutes, sites), why)
        self.assertIn("sound", b.why_awake(self.tab(audible=True), 15, []))
        self.assertEqual(b.why_awake(self.tab(), 15, []), "")


class TrackerTests(unittest.TestCase):
    def test_third_party_trackers_only(self):
        self.assertTrue(b.is_tracker("securepubads.g.doubleclick.net", "news.example.com"))
        self.assertTrue(b.is_tracker("www.google-analytics.com", "example.co.uk"))
        self.assertFalse(b.is_tracker("doubleclick.net", "www.doubleclick.net"))         # its own site
        self.assertFalse(b.is_tracker("cdn.example.com", "example.com"))
        self.assertFalse(b.is_tracker("i.ytimg.com", "www.youtube.com"))
        self.assertFalse(b.is_tracker("", "example.com"))
        self.assertEqual(b.registrable("news.bbc.co.uk"), "bbc.co.uk")
        self.assertEqual(b.registrable("a.b.example.com"), "example.com")


class SignInTests(unittest.TestCase):
    """Google's sign-in page hears Firefox; nothing else does."""

    def test_which_pages(self):
        self.assertTrue(b.signs_in_as_firefox("accounts.google.com"))
        self.assertTrue(b.signs_in_as_firefox("Accounts.Google.com."))
        for host in ("google.com", "www.google.com", "music.youtube.com", "accounts.google.com.evil.example", ""):
            self.assertFalse(b.signs_in_as_firefox(host), host)

    def test_a_firefox_thats_out(self):
        import datetime
        self.assertEqual(b.firefox_version(datetime.date(2025, 6, 24)), 140)
        self.assertEqual(b.firefox_version(datetime.date(2025, 1, 1)), 140)          # never older than the base
        self.assertEqual(b.firefox_version(datetime.date(2026, 10, 2)), 155)         # behind its ~156, never ahead
        agent = b.firefox_user_agent(datetime.date(2026, 10, 2))
        self.assertEqual(agent, "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101 Firefox/155.0")
        script = b.firefox_script(agent)
        self.assertIn("['accounts.google.com'].includes(location.hostname)", script)
        self.assertIn(repr(agent), script)

    def test_the_filter_swaps_the_headers_there_only(self):
        from pages.web import engine
        f = engine.TrackerFilter()
        info = mock.Mock()
        info.requestUrl.return_value.host.return_value = "accounts.google.com"
        info.firstPartyUrl.return_value.host.return_value = "music.youtube.com"
        f.interceptRequest(info)
        headers = dict(c.args for c in info.setHttpHeader.call_args_list)
        self.assertEqual(headers[b"User-Agent"], f.agent)
        self.assertEqual(headers[b"Sec-CH-UA"], b"")
        info.block.assert_not_called()
        other = mock.Mock()
        other.requestUrl.return_value.host.return_value = "music.youtube.com"
        other.firstPartyUrl.return_value.host.return_value = "music.youtube.com"
        f.interceptRequest(other)
        other.setHttpHeader.assert_not_called()
        f.enabled = False                                                     # blocking off: still swapped
        info.reset_mock()
        f.interceptRequest(info)
        self.assertTrue(info.setHttpHeader.called)


class VideoQualityTests(unittest.TestCase):
    """YouTube's quality through its own player - tried on a stand-in
    player in a page that says it's www.youtube.com."""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def run_in_youtube(self, choice, levels, video="abc"):
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        page = QWebEnginePage(QWebEngineProfile(self.app), self.app)
        self.addCleanup(page.deleteLater)
        done = []
        page.loadFinished.connect(done.append)
        player = ("<div id=movie_player></div><script>const p = document.getElementById('movie_player');"
                  f"p.getAvailableQualityLevels = () => {levels!r}.concat(['auto']);"
                  f"p.getVideoData = () => ({{video_id: {video!r}}}); window.set = [];"
                  "p.setPlaybackQualityRange = (a, b) => window.set.push(a + '/' + b);</script>")
        page.setHtml(player, QUrl("https://www.youtube.com/watch?v=abc"))
        for _ in range(60):
            if done:
                break
            _wait(50)
        page.runJavaScript(b.youtube_quality_script(choice))
        for _ in range(30):                                                  # its timer ticks each second
            _wait(100)
            if _js(page, "window.set.length"):
                break
        if choice == "auto":
            _wait(1200)
        return page, _js(page, "JSON.stringify(window.set)")

    def test_the_chosen_one_or_the_nearest_below(self):
        levels = ["hd1080", "hd720", "large", "medium", "small", "tiny"]
        self.assertEqual(self.run_in_youtube("720", levels)[1], '["hd720/hd720"]')
        self.assertEqual(self.run_in_youtube("1440", levels)[1], '["hd1080/hd1080"]')     # none higher: the best
        self.assertEqual(self.run_in_youtube("best", levels)[1], '["hd1080/hd1080"]')
        self.assertEqual(self.run_in_youtube("144", levels)[1], '["tiny/tiny"]')
        self.assertEqual(self.run_in_youtube("auto", levels)[1], "[]")                   # YouTube decides

    def test_once_a_video_then_back_to_youtube(self):
        page, first = self.run_in_youtube("480", ["hd720", "large", "medium"])
        self.assertEqual(first, '["large/large"]')
        _wait(1200)
        self.assertEqual(_js(page, "window.set.length"), 1)                  # not again: YouTube's menu still works
        page.runJavaScript(b.youtube_quality_script("auto"))
        _wait(1200)
        self.assertEqual(_js(page, "window.set[window.set.length - 1]"), "auto/auto")

    def test_other_sites_are_left_alone(self):
        self.assertIn("'youtube.com'", b.youtube_quality_script("720"))
        self.assertEqual(b.video_quality("8k"), "auto")


class SessionTests(unittest.TestCase):
    def test_saved_tabs(self):
        raw = [{"url": "https://a.com", "title": "A", "awake": 1}, {"url": ""}, {"url": "javascript:alert(1)"},
               "junk", {"url": 5}, {"url": "file:///D:/x.pdf", "title": "x" * 500}]
        self.assertEqual(b.clean_tabs(raw), [{"url": "https://a.com", "title": "A", "awake": True},
                                             {"url": "", "title": "", "awake": False},
                                             {"url": "file:///D:/x.pdf", "title": "x" * 200, "awake": False}])
        self.assertEqual(len(b.clean_tabs([{"url": ""}] * (b.MAX_TABS + 5))), b.MAX_TABS)

    def test_download_names_never_overwrite(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        open(os.path.join(folder, "clip.mov"), "w").close()
        open(os.path.join(folder, "clip (2).mov"), "w").close()
        self.assertEqual(b.unique_name(folder, "clip.mov"), "clip (3).mov")
        self.assertEqual(b.unique_name(folder, 'a:b?.mov'), "a_b_.mov")
        self.assertEqual(b.unique_name(folder, ""), "download")

    def test_the_new_tab_page(self):
        colors = {"bg": "#111111", "card": "#222222", "text": "#EEEEEE", "dim": "#888888", "accent": "#E64B3D",
                  "border": "#333333", "font": "'Segoe UI', sans-serif</style><script>"}
        page = b.start_page(colors, [{"name": "Y<ou>Tube", "url": "https://youtube.com"},
                                     {"name": "Footage", "url": "D:\\Footage"}], "google")
        self.assertIn('action="https://www.google.com/search"', page)
        self.assertIn('name="q"', page)
        self.assertIn("Y&lt;ou&gt;Tube", page)
        self.assertNotIn("Footage", page)                                   # folders aren't web pages
        self.assertIn("font-family: 'Segoe UI', sans-serifstylescript", page)   # no way out of the <style>
        self.assertNotIn("<script", page)

    def test_the_new_tab_pages_own_shortcuts(self):
        raw = [{"name": "Frame", "url": "https://frame.io"}, {"name": "again", "url": "https://frame.io"},
               {"name": "", "url": "https://www.artlist.io/x"}, {"name": "x", "url": "javascript:alert(1)"},
               {"name": "Footage", "url": "D:\\Footage"}, "junk"]
        self.assertEqual(b.clean_shortcuts(raw), [{"name": "Frame", "url": "https://frame.io"},
                                                  {"name": "artlist.io", "url": "https://www.artlist.io/x"}])
        self.assertEqual(len(b.clean_shortcuts([{"name": str(i), "url": f"https://s{i}.com"} for i in range(40)])),
                         b.MAX_SHORTCUTS)
        self.assertEqual(b.start_page_action(b.ADD_SHORTCUT), ("add", None))
        self.assertEqual(b.start_page_action(b.REMOVE_SHORTCUT + "2"), ("remove", 2))
        self.assertIsNone(b.start_page_action(b.REMOVE_SHORTCUT + "x"))
        self.assertIsNone(b.start_page_action("https://example.com"))
        colors = dict.fromkeys(("bg", "card", "text", "dim", "accent", "border", "font"), "#123456")
        page = b.start_page(colors, [{"name": "YouTube", "url": "https://youtube.com"}], shortcuts=raw)
        self.assertIn(f'href="{b.ADD_SHORTCUT}"', page)                      # the +
        self.assertIn(f'href="{b.REMOVE_SHORTCUT}1"', page)                  # its own have an x
        self.assertEqual(page.count('class="x"'), 2)                         # the link bar's don't
        self.assertNotIn("Private tab", page)
        full = b.start_page(colors, [], shortcuts=[{"name": str(i), "url": f"https://s{i}.com"} for i in range(40)])
        self.assertNotIn(b.ADD_SHORTCUT, full)                               # no room for more
        self.assertIn("Private tab", b.start_page(colors, [], private=True))


_PROFILE_DIR = tempfile.mkdtemp(prefix="buddy_web_profile_")


def _tone(path, seconds=2):
    """A sine tone at about -55 dB - near silent, but loud enough that
    Chromium counts the tab as playing sound."""
    import math
    import struct
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"".join(struct.pack("<h", int(60 * math.sin(i * 0.06))) for i in range(22050 * seconds)))


def _js(page, script, ms=3000):
    got = []
    page.runJavaScript(script, 0, got.append)
    for _ in range(ms // 50):
        if got:
            return got[0]
        _wait(50)
    return None


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class BrowserTests(unittest.TestCase):
    """The real browser, on local files, in a throwaway profile."""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="buddy_web_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from pages.web import engine
        import pages.web.page as wp
        from test_settings_pages import Mem
        self.wp, self.engine = wp, engine
        if engine._profile is None:                     # made once per run, never in ~/.buddy
            engine.PROFILE_DIR = os.path.join(_PROFILE_DIR, "profile")
        self.saved = Mem(dict(b.DEFAULTS))
        # Never the real filter lists: no downloads, nothing in ~/.buddy.
        from pages.web import filter_lists
        lists = mock.patch.object(filter_lists.FilterLists, "start")
        self.list_start = lists.start()
        self.addCleanup(lists.stop)
        host = mock.Mock(spec=["tool_settings", "theme_tokens", "shared_settings", "links"])
        host.tool_settings.return_value = self.saved
        host.theme_tokens.side_effect = lambda: __import__("core.theme", fromlist=["x"]).get_theme_tokens("Resolve")
        host.shared_settings = {}
        host.links = []
        self.host = host
        self.pages = []
        for name in ("a", "b"):
            path = os.path.join(self.tmp, f"{name}.html")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"<title>Page {name.upper()}</title><h1>{name}</h1>")
            self.pages.append("file:///" + path.replace("\\", "/"))

    def browser(self):
        page = self.wp.WebBrowserPage(self.host)
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.ducker.stop)
        page.resize(900, 600)
        page.show()
        return page

    def until(self, check, ms=8000):
        for _ in range(ms // 100):
            if check():
                return
            _wait(100)
        self.fail("never happened")

    def test_tabs_load_when_shown_and_sleep_when_left(self):
        self.saved["tabs"] = [{"url": self.pages[0], "title": "Page A"}, {"url": self.pages[1], "title": "Page B"}]
        self.saved["active"] = 1
        page = self.browser()
        a, bee = page.tabs
        self.assertIsNone(a.web)                                           # never shown: no page at all
        self.assertIs(page.active, bee)
        self.until(lambda: bee.title == "Page B")
        page.select(a)
        self.until(lambda: a.title == "Page A" and not a.loading)
        bee.seen = time.time() - 3600
        page.check_sleep()
        self.assertEqual(bee.page.lifecycleState(), QWebEnginePage.LifecycleState.Discarded)
        self.assertTrue(page.tabs[1].state(15, [])["asleep"])
        page.select(bee)                                                    # wakes it
        self.assertEqual(bee.page.lifecycleState(), QWebEnginePage.LifecycleState.Active)
        # Kept awake, or on a never-sleep site: it stays up.
        a.seen = time.time() - 3600
        page.keep_awake(a, True)
        page.check_sleep()
        self.assertEqual(a.page.lifecycleState(), QWebEnginePage.LifecycleState.Active)
        page._save()
        self.assertEqual([t["awake"] for t in self.saved["tabs"]], [True, False])
        self.assertEqual(self.saved["active"], 1)

    def test_the_address_bar_and_closing(self):
        page = self.browser()
        self.assertEqual([t.url for t in page.tabs], [""])                  # a fresh start: the new-tab page
        page.on_go({"text": self.pages[0]})
        self.until(lambda: page.active.title == "Page A")
        page.open_tab(self.pages[1])
        self.assertEqual(len(page.tabs), 2)
        page.close_tab(page.active)
        self.assertEqual([t.url for t in page.tabs], [self.pages[0]])
        page.reopen_closed()
        self.assertEqual([t.url for t in page.tabs], [self.pages[0], self.pages[1]])
        page.close_tab(page.tabs[0])
        page.close_tab(page.tabs[0])
        self.assertEqual([t.url for t in page.tabs], [""])                  # never no tabs at all

    def test_a_new_tab_starts_in_the_themes_colour_not_white(self):
        page = self.browser()
        tab = page.open_tab("")
        surface = self.host.theme_tokens()["surface"]
        self.assertEqual(tab.page.backgroundColor().name(), surface.lower())
        tab.url = self.pages[0]                                   # a site: the white every browser gives it
        tab._ground()
        self.assertEqual(tab.page.backgroundColor().name(), "#ffffff")
        page.on_go({"text": self.pages[1]})
        self.until(lambda: page.active.title == "Page B")
        self.assertEqual(page.active.page.backgroundColor().name(), "#ffffff")

    def test_new_tabs_always_join_the_right_hand_end(self):
        page = self.browser()
        first = page.tabs[0]
        page.open_tab(self.pages[0], show=False)
        page.open_tab(self.pages[1], show=False)
        page.select(first)                                                   # the active tab is the leftmost
        page.on_new_tab()
        made = page.tabs[-1]
        page.new_private_tab()
        private = page.tabs[-1]
        opened = page.open_tab_for_page(first)                               # a link opening a new window
        page.open_tab(first.url, private=first.private)                      # Duplicate
        self.assertEqual(len(page.tabs), 7)
        self.assertIs(page.tabs[0], first)
        self.assertEqual([page.tabs[3], page.tabs[4], page.tabs[5]], [made, private, opened])
        self.assertTrue(private.private and not made.private)

    def test_private_tabs_leave_nothing_behind(self):
        page = self.browser()
        page.on_go({"text": self.pages[0]})
        self.until(lambda: page.active.title == "Page A")
        page.new_private_tab(self.pages[1])
        private = page.active
        self.until(lambda: private.title == "Page B")
        self.assertTrue(private.private)
        self.assertTrue(private.page.profile().isOffTheRecord())
        self.assertFalse(page.tabs[0].page.profile().isOffTheRecord())
        self.assertTrue(private.state(15, [])["private"])
        page.on_go({"text": "private-only.example"})                       # typed in a private tab
        page._save()
        self.assertEqual([t["url"] for t in self.saved["tabs"]], [self.pages[0]])   # never saved
        self.assertEqual(self.saved["active"], 0)
        self.assertNotIn("private-only.example", self.saved["sites"])        # nor remembered
        # What a private page opens is private too.
        opened = page.open_tab_for_page(private)
        self.assertTrue(opened.private and opened.page.profile() is private.page.profile())
        page.close_tab(opened)
        page.close_tab(private)
        self.assertEqual(page._closed, [])                                    # not offered to reopen
        self.assertTrue(self.engine.has_private())
        self.until(lambda: not self.engine.has_private(), ms=4000)            # forgotten once the last closes
        page.on_new_tab({"private": True})
        self.assertTrue(page.active.private and self.engine.has_private())   # a fresh one
        page.close_tab(page.active)

    def test_the_new_tab_pages_plus_and_x(self):
        self.saved["shortcuts"] = [{"name": "Frame", "url": "https://frame.io"},
                                   {"name": "Artlist", "url": "https://artlist.io"}]
        page = self.browser()
        tab = page.active
        self.until(lambda: not tab.loading and tab.page.url().toString() == "about:blank")
        # A real click on the first x: Buddy takes the link, the page doesn't go anywhere.
        tab.page.runJavaScript("document.querySelector('a.x').click()")
        self.until(lambda: len(self.saved["shortcuts"]) == 1)
        self.assertEqual(self.saved["shortcuts"], [{"name": "Artlist", "url": "https://artlist.io"}])
        dialog = mock.Mock()
        dialog.exec.return_value = True
        dialog.link = {"name": "Vimeo", "url": "https://vimeo.com"}
        with mock.patch.object(self.wp, "LinkDialog", return_value=dialog):
            self.until(lambda: not tab.loading)
            tab.page.runJavaScript("document.querySelector('a.add').click()")
            self.until(lambda: len(self.saved["shortcuts"]) == 2)
        self.assertEqual(self.saved["shortcuts"][-1], {"name": "Vimeo", "url": "https://vimeo.com"})
        dialog.link = {"name": "Footage", "url": "D:\\Footage"}
        with mock.patch.object(self.wp, "LinkDialog", return_value=dialog), \
                mock.patch.object(self.wp, "alert") as told:
            page.add_shortcut()
        told.assert_called_once()                                             # folders go on the link bar
        self.assertEqual(len(self.saved["shortcuts"]), 2)
        # A site's own link in that scheme does nothing.
        with mock.patch.object(page, "start_page_action") as acted:
            page.on_go({"text": self.pages[0]})
            self.until(lambda: tab.title == "Page A" and not tab.loading)
            tab.page.runJavaScript(f"location.href = '{b.ADD_SHORTCUT}'")
            _wait(500)
        acted.assert_not_called()

    def test_pause_and_play_from_the_tab(self):
        _tone(os.path.join(self.tmp, "tone.wav"))
        song = os.path.join(self.tmp, "song.html")
        with open(song, "w", encoding="utf-8") as fh:
            fh.write('<title>Song</title><audio src="tone.wav" autoplay loop></audio>')
        page = self.browser()
        page.on_go({"text": "file:///" + song.replace("\\", "/")})
        tab = page.active
        playing = lambda: _js(tab.page, "!document.querySelector('audio').paused")
        self.until(lambda: playing() is True)
        page.on_media({"id": tab.id})
        self.assertTrue(tab.paused and tab.state(15, [])["paused"])
        self.until(lambda: playing() is False)
        page.on_media({"id": tab.id})
        self.assertFalse(tab.paused)
        self.until(lambda: playing() is True)
        # The same from the sidebar's button beside "Web".
        page.host.refresh_badges = mock.Mock()
        self.until(lambda: (page.push(), page.rail_badge())[1] == "sound")
        page.rail_media()
        page.push()
        self.assertEqual(page.rail_badge(), "paused")                      # play, at once
        page.host.refresh_badges.assert_called()
        self.until(lambda: playing() is False)
        page.rail_media()
        self.until(lambda: playing() is True)
        self.until(lambda: (page.push(), page.rail_badge())[1] == "sound")
        # Nothing playing to pause: the button doesn't stick.
        page.on_go({"text": self.pages[0]})
        self.until(lambda: tab.title == "Page A" and not tab.loading)
        page.on_media({"id": tab.id})
        self.until(lambda: not tab.paused)

    def test_a_page_asking_before_its_left(self):
        held = os.path.join(self.tmp, "held.html")
        with open(held, "w", encoding="utf-8") as fh:
            fh.write("<title>Held</title><body style='height:400px'><script>"
                     "addEventListener('beforeunload', e => { e.preventDefault(); e.returnValue = ''; });"
                     "</script>")
        page = self.browser()
        page.on_go({"text": "file:///" + held.replace("\\", "/")})
        tab = page.active
        self.until(lambda: tab.title == "Held" and not tab.loading)
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        target = tab.web.focusProxy() or tab.web
        QTest.mouseClick(target, Qt.LeftButton, Qt.NoModifier, QPoint(50, 50))   # a site only asks once used
        _wait(300)
        with mock.patch.object(self.engine, "confirm", return_value=False) as asked:
            page.on_go({"text": self.pages[0]})
            self.until(lambda: asked.called)
            _wait(500)
        self.assertEqual(asked.call_args.kwargs["ok"], "Leave")              # Buddy's own dialog, not Qt's box
        self.assertTrue(asked.call_args.args[1].startswith("Leave "))
        self.assertEqual(tab.title, "Held")                                    # Stay: it stays
        with mock.patch.object(self.engine, "confirm", return_value=True):
            page.on_go({"text": self.pages[0]})
            self.until(lambda: tab.title == "Page A")

    def test_dragging_a_tab_moves_it(self):
        self.saved["tabs"] = [{"url": self.pages[0], "title": "A"}, {"url": self.pages[1], "title": "B"},
                              {"url": "", "title": ""}]
        page = self.browser()
        a, bee, new = page.tabs
        page.on_move({"id": a.id, "index": 2})
        self.assertEqual(page.tabs, [bee, new, a])
        page.on_move({"id": a.id, "index": 99})                             # past the end: last
        page.on_move({"id": new.id, "index": -3})                           # before the start: first
        self.assertEqual(page.tabs, [new, bee, a])
        page.on_move({"id": 999, "index": 0})
        page.on_move({"id": a.id, "index": "x"})
        self.assertEqual(page.tabs, [new, bee, a])
        page._save()
        self.assertEqual([t["title"] for t in self.saved["tabs"]], ["", "B", "A"])   # kept in that order

    def test_settings(self):
        page = self.browser()
        ui = mock.Mock()
        page.on_setting("sleep_after", "30", ui)
        page.on_setting("sleep_after", "7", ui)                             # not a choice
        page.on_setting("never_sleep", "https://music.youtube.com/x\nopen.spotify.com", ui)
        page.on_setting("search", "google", ui)
        page.on_setting("blocking", "off", ui)
        self.assertEqual((self.saved["sleep_after"], self.saved["never_sleep"], self.saved["search"]),
                         (30, ["music.youtube.com", "open.spotify.com"], "google"))
        self.assertFalse(self.engine.tracker_filter().enabled)
        page.on_setting("blocking", "strong", ui)
        page.on_setting("blocking", "max", ui)                               # not a choice
        self.assertEqual(self.engine.tracker_filter().mode, "strong")
        with mock.patch.object(self.engine, "set_youtube_ads") as told:
            page.on_setting("youtube_ads", True, ui)
        self.assertEqual((self.saved["youtube_ads"], told.call_args.args), (True, (True,)))
        page.on_setting("allow_ads", "https://www.frame.io/x\nbad site", ui)
        self.assertEqual(self.saved["allow_ads"], ["frame.io"])                    # and not "bad site"
        self.assertEqual(self.engine.tracker_filter().allowed, frozenset({"frame.io"}))
        page.on_setting("duck_level", 55, ui)
        self.assertEqual(self.saved["duck_level"], 55)
        page.on_setting("duck_level", 12, ui)                               # onto the step
        self.assertEqual((self.saved["duck_level"], page.ducker.level), (10, 0.10))
        page.on_setting("duck_level", 140, ui)                              # not a level
        page.on_setting("duck_level", "loud", ui)
        self.assertEqual(self.saved["duck_level"], 10)
        with mock.patch.object(self.engine, "set_video_quality") as told:
            page.on_setting("video_quality", "720", ui)
            page.on_setting("video_quality", "8k", ui)
        self.assertEqual((self.saved["video_quality"], told.call_count), ("720", 1))
        page.on_setting("downloads", os.path.join(self.tmp, "nowhere"), ui)
        ui.alert.assert_called_once()
        keys = [f.get("key") for f in page.settings_fields() if f.get("key")]
        self.assertEqual(keys, ["sleep_after", "never_sleep", "duck", "duck_level", "video_quality", "search",
                                "region", "suggest",
                                "blocking", "youtube_ads", "allow_ads", "downloads"])
        page.on_go({"text": "gooey.dev"})
        page.on_go({"text": "some search words"})
        self.assertEqual(list(self.saved["sites"]), ["gooey.dev"])                 # searches aren't sites
        page.emit = mock.Mock()
        page.on_sites()
        self.assertEqual(page.emit.call_args[0][1]["sites"][0], "gooey.dev")
        page.on_setting("suggest", False, ui)
        self.assertEqual(self.saved["sites"], {})
        page.on_sites()
        self.assertEqual(page.emit.call_args[0][1]["sites"], [])
        page.on_setting("suggest", True, ui)
        del page.emit
        with mock.patch.object(self.engine, "set_languages") as told:
            page.on_setting("region", "JP", ui)
            page.on_setting("region", "Atlantis", ui)
            page.on_setting("region", "", ui)
        self.assertEqual(self.saved["region"], "")
        self.assertEqual([c.args[0] for c in told.call_args_list], ["en-JP,en;q=0.9,ja;q=0.8", None])
        self.assertEqual(page.region(), "")
        page.set_never_sleep("youtube.com", True)
        page.set_never_sleep("music.youtube.com", False)                    # off for the site it's under too
        self.assertEqual(self.saved["never_sleep"], ["open.spotify.com"])


if __name__ == "__main__":
    unittest.main()
