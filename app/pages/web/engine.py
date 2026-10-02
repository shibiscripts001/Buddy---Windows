#!/usr/bin/env python3
"""
The Web tab's engine: its own browser profile, the ad and tracker filter,
a tab's page, and how much memory the pages use. Qt WebEngine - the
Chromium Buddy's own windows are drawn with, so the browser adds nothing
to install.

The profile is the browser's alone, kept apart from Buddy's own pages
(core/web_page.py): it remembers logins and cookies between runs, under
~/.buddy/web, and Buddy's pages and the web's never share a process. Its
cookies - the user's sign-ins - are kept encrypted (cookie_vault.py), not
in the plain database Qt WebEngine keeps by itself.
"""

import ctypes
import json
import os
import re
import secrets
import shutil
import sys

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings,
                                     QWebEngineUrlRequestInfo, QWebEngineUrlRequestInterceptor)
from PySide6.QtWidgets import QApplication

from core.message_dialog import alert, confirm
from core.settings_store import BUDDY_DIR
from pages.web import browser, cookie_vault

PROFILE_DIR = os.path.join(BUDDY_DIR, "web")
# Left by clear_site_data: the whole profile folder goes as Buddy next
# starts, before the browser opens it (while it runs, it holds its files).
WIPE_MARK = "clear-on-next-start"
CACHE_BYTES = 256 * 1024 * 1024

# Schemes a page may open itself; the rest (mailto:, zoommtg:, ...) go to
# whatever on this PC handles them.
WEB_SCHEMES = ("http", "https", "about", "data", "blob", "file", "qrc")

# What Qt WebEngine asks when a page wants to be sure before it's left
# (onbeforeunload - YouTube Music while it plays, a half-filled form).
LEAVE_PAGE = "Are you sure you want to leave this page? Changes that you made may not be saved."


# Chrome's "which browser are you" headers, which Firefox never sends:
# emptied on Google's sign-in page (they can't be taken off altogether).
CLIENT_HINTS = (b"Sec-CH-UA", b"Sec-CH-UA-Mobile", b"Sec-CH-UA-Platform", b"Sec-CH-UA-Platform-Version",
                b"Sec-CH-UA-Full-Version", b"Sec-CH-UA-Full-Version-List", b"Sec-CH-UA-Arch",
                b"Sec-CH-UA-Bitness", b"Sec-CH-UA-Model", b"Sec-CH-UA-WoW64", b"Sec-CH-UA-Form-Factors")


# What kind of request it is, in the filter lists' words.
_RT = QWebEngineUrlRequestInfo.ResourceType
_KINDS = {getattr(_RT, f"ResourceType{name}"): kind for name, kind in (
    ("MainFrame", "document"), ("NavigationPreloadMainFrame", "document"), ("SubFrame", "subdocument"),
    ("NavigationPreloadSubFrame", "subdocument"), ("Stylesheet", "stylesheet"), ("Script", "script"),
    ("Worker", "script"), ("SharedWorker", "script"), ("ServiceWorker", "script"), ("Image", "image"),
    ("Favicon", "image"), ("FontResource", "font"), ("Object", "object"), ("PluginResource", "object"),
    ("Media", "media"), ("Xhr", "xmlhttprequest"), ("Json", "xmlhttprequest"), ("Ping", "ping"),
    ("WebSocket", "websocket")) if hasattr(_RT, f"ResourceType{name}")}


class TrackerFilter(QWebEngineUrlRequestInterceptor):
    """Refuses requests to ad and tracking domains from other sites' pages
    (browser.is_tracker) and, blocking "strong", what the filter lists
    block (filters.Filters) - never on a site the user allows ads on - and
    on Google's sign-in page says the browser is Firefox
    (browser.signs_in_as_firefox). Runs on Chromium's network thread, so
    it only reads its attributes (swapped whole from the GUI thread), and
    counts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = browser.DEFAULT_BLOCKING   # browser.BLOCKING
        self.filters = None                    # filters.Filters, once the lists are read
        self.allowed = frozenset()             # sites ads are allowed on
        self.blocked = 0
        self.agent = browser.firefox_user_agent().encode()

    @property
    def enabled(self):
        return self.mode != "off"

    @enabled.setter
    def enabled(self, on):
        self.mode = browser.DEFAULT_BLOCKING if on else "off"

    def blocks(self, url, host, site, kind):
        """True if this request is refused."""
        if self.mode == "off" or browser.allowed_site(site, self.allowed):
            return False
        if kind != "document" and browser.is_tracker(host, site):
            return True
        filters = self.filters
        return self.mode == "strong" and filters is not None and filters.matches(url, host, site, kind) is not None

    def interceptRequest(self, info):
        url = info.requestUrl()
        host = url.host()
        if self.blocks(url.toString(), host, info.firstPartyUrl().host(), _KINDS.get(info.resourceType(), "other")):
            info.block(True)
            self.blocked += 1
            return
        if browser.signs_in_as_firefox(host):
            info.setHttpHeader(b"User-Agent", self.agent)
            for name in CLIENT_HINTS:
                info.setHttpHeader(name, b"")


_profile = None
_vault = None
_quality = browser.DEFAULT_VIDEO_QUALITY
QUALITY_SCRIPT = "buddy-video-quality"
_youtube_ads = False
YOUTUBE_ADS_SCRIPT = "buddy-youtube-ads"
_private = None
_filter = None
_default_language = ""
_accept = None


def _set_up(made):
    """What both profiles share: plain Chrome to the sites (some refuse to
    sign in to anything that says it's an embedded browser) - Firefox to
    Google's sign-in page - the same page settings and the same filter."""
    made.setHttpUserAgent(re.sub(r"\s*QtWebEngine/\S+", "", made.httpUserAgent()))
    s = made.settings()
    for attribute, on in ((QWebEngineSettings.FullScreenSupportEnabled, True),
                          (QWebEngineSettings.JavascriptCanOpenWindows, False),
                          (QWebEngineSettings.JavascriptCanAccessClipboard, False),
                          (QWebEngineSettings.PluginsEnabled, False),
                          (QWebEngineSettings.PdfViewerEnabled, True),
                          (QWebEngineSettings.PlaybackRequiresUserGesture, False),
                          (QWebEngineSettings.DnsPrefetchEnabled, False),
                          (QWebEngineSettings.ScrollAnimatorEnabled, True),
                          (QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)):
        s.setAttribute(attribute, on)
    made.setUrlRequestInterceptor(_filter)
    # The page's own script hears Firefox too, on the sign-in page.
    script = QWebEngineScript()
    script.setName("buddy-sign-in")
    script.setInjectionPoint(QWebEngineScript.DocumentCreation)
    script.setWorldId(QWebEngineScript.MainWorld)
    script.setRunsOnSubFrames(True)
    script.setSourceCode(browser.firefox_script(_filter.agent.decode()))
    made.scripts().insert(script)
    _put_quality_script(made)
    _put_youtube_ads_script(made)
    _put_video_script(made)


# Chromium here can't decode H.264 or AAC; video_fallback.js notices a video
# that failed for it and says so on the console, behind this secret (a page
# can't guess it, so it can't make Buddy open a video). TabPage hears it.
VIDEO_TOKEN = secrets.token_hex(12)
VIDEO_SCRIPT = "buddy-video-fallback"
_VIDEO_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_fallback.js")


def _put_video_script(made):
    for old in made.scripts().find(VIDEO_SCRIPT):
        made.scripts().remove(old)
    try:
        with open(_VIDEO_JS, encoding="utf-8") as fh:
            source = fh.read().replace("__TOKEN__", VIDEO_TOKEN)
    except OSError:
        return
    script = QWebEngineScript()
    script.setName(VIDEO_SCRIPT)
    script.setInjectionPoint(QWebEngineScript.DocumentReady)
    script.setWorldId(QWebEngineScript.ApplicationWorld)       # out of the page's reach
    script.setRunsOnSubFrames(True)                            # players embedded in other sites too
    script.setSourceCode(source)
    made.scripts().insert(script)


def _put_quality_script(made):
    for old in made.scripts().find(QUALITY_SCRIPT):
        made.scripts().remove(old)
    script = QWebEngineScript()
    script.setName(QUALITY_SCRIPT)
    script.setInjectionPoint(QWebEngineScript.DocumentReady)
    script.setWorldId(QWebEngineScript.MainWorld)         # YouTube's player is the page's own
    script.setRunsOnSubFrames(True)                       # its players embedded in other sites too
    script.setSourceCode(browser.youtube_quality_script(_quality))
    made.scripts().insert(script)


def _put_youtube_ads_script(made):
    for old in made.scripts().find(YOUTUBE_ADS_SCRIPT):
        made.scripts().remove(old)
    if not _youtube_ads:
        return
    script = QWebEngineScript()
    script.setName(YOUTUBE_ADS_SCRIPT)
    # Before any of YouTube's own script runs, and in its world: it's
    # YouTube's own JSON.parse and fetch that hand the player its data.
    script.setInjectionPoint(QWebEngineScript.DocumentCreation)
    script.setWorldId(QWebEngineScript.MainWorld)
    script.setRunsOnSubFrames(True)                       # YouTube's players embedded in other sites
    script.setSourceCode(browser.youtube_ads_script())
    made.scripts().insert(script)


def set_youtube_ads(on):
    """YouTube's video ads taken out (browser.youtube_ads_script) from the
    next page YouTube loads, in both profiles."""
    global _youtube_ads
    _youtube_ads = bool(on)
    for made in (_profile, _private):
        if made is not None:
            _put_youtube_ads_script(made)


def set_video_quality(choice):
    """YouTube's quality from now on, in both profiles (pages already open:
    page.py runs the script in them)."""
    global _quality
    _quality = browser.video_quality(choice)
    for made in (_profile, _private):
        if made is not None:
            _put_quality_script(made)


def profile():
    """The browser's profile, made once and parented to the application,
    which outlives every page using it."""
    global _profile, _filter, _default_language, _vault
    if _profile is None:
        wipe_if_asked(PROFILE_DIR)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        _profile = QWebEngineProfile("buddy-web", QApplication.instance())
        _profile.setPersistentStoragePath(PROFILE_DIR)
        _profile.setCachePath(os.path.join(PROFILE_DIR, "cache"))
        _profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        _profile.setHttpCacheMaximumSize(CACHE_BYTES)
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies if cookie_vault.available
                                            else QWebEngineProfile.AllowPersistentCookies)
        _default_language = _profile.httpAcceptLanguage()
        _filter = TrackerFilter(_profile)
        _set_up(_profile)
        if cookie_vault.available:
            _vault = cookie_vault.CookieVault(_profile, PROFILE_DIR)
            _vault.load()
    return _profile


def save_sign_ins():
    """Now, not in a moment: Buddy is closing."""
    if _vault is not None:
        _vault.save()


def private_profile(on_download=None):
    """Private tabs' profile: kept in memory only, shared by every private
    tab (signed in in one, signed in in all, as in Chrome), and thrown away
    with all it holds once the last of them closes (drop_private)."""
    global _private
    if _private is None:
        profile()
        _private = QWebEngineProfile(QApplication.instance())       # no name: off the record
        _set_up(_private)
        _private.setHttpAcceptLanguage(_accept or _default_language)
        if on_download is not None:
            _private.downloadRequested.connect(on_download)
    return _private


def has_private():
    return _private is not None


def drop_private():
    """Forgets everything the private tabs did. Their pages must be gone
    first - a page can't outlive its profile."""
    global _private
    if _private is not None:
        _private.deleteLater()
        _private = None


def tracker_filter():
    profile()
    return _filter


def set_languages(accept):
    """What sites are told the user reads (browser.accept_language), or
    None to go back to the system's own."""
    global _accept
    _accept = accept
    for made in (profile(), _private):
        if made is not None:
            made.setHttpAcceptLanguage(accept or _default_language)


def clear_site_data():
    """Signs out of every site: cookies and the cache now, and everything
    else sites stored (their storage, databases, background workers, what
    they asked the browser to remember) as Buddy next starts - the profile
    as new as a private tab's."""
    p = profile()
    p.cookieStore().deleteAllCookies()
    if _vault is not None:
        _vault.forget()
    p.clearHttpCache()
    p.clearAllVisitedLinks()
    try:
        with open(os.path.join(PROFILE_DIR, WIPE_MARK), "w", encoding="utf-8") as fh:
            fh.write("Buddy clears this folder when it next starts.\n")
    except OSError:
        pass


def wipe_if_asked(folder):
    """Empties `folder` if clear_site_data asked for it. What can't be
    removed (a file still open somewhere) is left; True if it was asked."""
    if not os.path.exists(os.path.join(folder, WIPE_MARK)):
        return False
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass
    return True


HIDE_SCRIPT = "buddy-hide-ads"
# The page's hiding stylesheet (filters.Filters.hiding_css), added as the
# document is made - before any of it is drawn - and again once it's
# loaded, should the page have replaced the document's sheets meanwhile.
_HIDE_JS = """(() => {
  const css = %s;
  let sheet = null;
  const add = () => {
    try {
      if (sheet && document.adoptedStyleSheets.includes(sheet)) return;
      sheet = sheet || new CSSStyleSheet();
      if (!sheet.cssRules.length) sheet.replaceSync(css);
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    } catch (e) {
      const style = document.createElement('style');
      style.textContent = css;
      (document.head || document.documentElement).appendChild(style);
    }
  };
  add();
  document.addEventListener('DOMContentLoaded', add, {once: true});
})();"""


def hiding_css(url):
    """The stylesheet hiding a page's ad boxes, "" for none."""
    f = _filter
    if f is None or f.mode != "strong" or f.filters is None:
        return ""
    from urllib.parse import urlsplit
    if browser.allowed_site(urlsplit(url).hostname, f.allowed):
        return ""
    return f.filters.hiding_css(url)


class TabPage(QWebEnginePage):
    """One tab's page, in the private profile for a private tab. New
    windows a page opens become tabs; schemes that aren't the web's open in
    the app that handles them; requests for the camera, microphone,
    location or notifications are refused."""

    def __init__(self, tab, parent):
        super().__init__(private_profile(tab.browser.download_requested) if tab.private else profile(), parent)
        self.tab = tab
        if hasattr(self, "permissionRequested"):
            self.permissionRequested.connect(lambda permission: permission.deny())

    def createWindow(self, _kind):
        made = self.tab.browser.open_tab_for_page(self.tab)
        return made.page if made is not None else None

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if url.host().lower() == browser.START_HOST:
            # The new-tab page's own + and x (browser.start_page) - only
            # from that page, never from a site's.
            if is_main_frame and not self.tab.url and self.url().toString() in ("", "about:blank"):
                action = url.toString()
                QTimer.singleShot(0, lambda: self.tab.browser.start_page_action(self.tab, action))
            return False
        if url.scheme().lower() not in WEB_SCHEMES:
            if is_main_frame and nav_type == QWebEnginePage.NavigationTypeLinkClicked:
                QDesktopServices.openUrl(url)
            return False
        if is_main_frame and url.scheme().lower() in ("http", "https"):
            self.prepare_hiding(url.toString())
        return True

    def prepare_hiding(self, url):
        """The document this navigation makes gets its page's hiding
        stylesheet (none for frames: theirs are the ads)."""
        scripts = self.scripts()
        for old in scripts.find(HIDE_SCRIPT):
            scripts.remove(old)
        css = hiding_css(url)
        if css:
            script = QWebEngineScript()
            script.setName(HIDE_SCRIPT)
            script.setInjectionPoint(QWebEngineScript.DocumentCreation)
            script.setWorldId(QWebEngineScript.ApplicationWorld)    # the page can't see or undo it
            script.setRunsOnSubFrames(False)
            script.setSourceCode(_HIDE_JS % json.dumps(css))
            scripts.insert(script)

    # A page's own pop-ups, drawn as Buddy's dialogs rather than bare Qt
    # boxes, and over Buddy's window. What the site says is its own text.
    def _site(self, origin):
        return origin.host() or browser.site_of(self.url().toString()) or "This page"

    def _parent(self):
        return self.tab.web.window() if self.tab.web is not None else None

    def javaScriptConfirm(self, origin, message):
        site = self._site(origin)
        if message == LEAVE_PAGE:
            return confirm(self._parent(), "Leave {site}?".replace("{site}", site),
                           "The site asks before you leave. Anything you haven't saved on it, like a "
                           "half-filled form, may be lost - and what it's playing stops.",
                           ok="Leave", cancel="Stay")
        return confirm(self._parent(), site, message)

    def javaScriptAlert(self, origin, message):
        alert(self._parent(), self._site(origin), message)

    def javaScriptConsoleMessage(self, level, message, line, source):
        # The web's own logging isn't Buddy's - except a video that couldn't
        # play here (video_fallback.js), which Buddy's player takes.
        if message.startswith(VIDEO_TOKEN):
            raw = message[len(VIDEO_TOKEN):]
            QTimer.singleShot(0, lambda: self.tab.browser.play_video(self.tab, raw))


# ---------------------------------------------------------------- memory --

def memory_bytes(pids):
    """Memory the processes with these ids use (their private bytes,
    what Task Manager counts), or None where it can't be read."""
    pids = {p for p in pids if p}
    if not pids:
        return 0
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel32.OpenProcess.restype = wintypes.HANDLE
    total = 0
    for pid in pids:
        handle = kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                total += counters.PrivateUsage
        finally:
            kernel32.CloseHandle(handle)
    return total


def open_outside(url):
    """In the user's own browser."""
    QDesktopServices.openUrl(QUrl(url))
