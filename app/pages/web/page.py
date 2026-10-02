#!/usr/bin/env python3
"""
Web: a light browser in Buddy (pages/web/browser.py has the rules,
pages/web/engine.py the engine).

The page is a bar drawn by Buddy (web/: tabs, back and forward, the
address, downloads, a menu) over the open tab's page. Pages are real
Chromium pages in the browser's own profile, so sites remember you; a tab
only gets a page once it's first shown, and a background tab sleeps after
a while (Settings > Tools > Web) unless it's playing sound, kept awake, or
on a site that never sleeps - so music plays on with the Web tab out of
sight, and a dozen idle tabs cost next to nothing. Downloads go to a
folder of the user's choosing and can be dragged from the bar straight
into Resolve's Media Pool.

Protocol (bar, web/):
    to the view    browser, focus_address, toast
    from the view  new_tab, close, select, move, go, back, forward, reload,
                   stop, mute, media, menu, more, downloads, drag_download, size

A private tab (Ctrl+Shift+N) is in a profile kept in memory only
(engine.private_profile): nothing of it is saved, remembered for the
address bar or kept once the last private tab closes.
"""

import base64
import os
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QTimer, QUrl
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEnginePage, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QMenu, QStackedWidget, QVBoxLayout, QWidget

from core import settings_form as sf
from core import link_bar
from core.i18n import LANGUAGE_CODES, get_i18n, tr
from core.link_bar_web import LinkDialog
from core.link_peek_web import FolderPeek, start_file_drag
from core.message_dialog import alert
from pages.base import ToolPage
from core import audio_sessions
from pages.web import browser, ducking, engine, filter_lists
from pages.web.bar import BrowserBar

SETTINGS_ID = "buddy_web"
CHECK_SLEEP_MS = 20_000
MEMORY_MS = 4_000
SAVE_DELAY_MS = 1_500
ZOOMS = (0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)
RECENT_DOWNLOADS = 5
# Chromium says a tab is audible a moment late and stays so a moment
# after it stops: sound reported this soon after Buddy paused it is the
# old sound, not the page playing again.
PAUSE_SETTLE_S = 2.0
DROP_PRIVATE_MS = 1_000       # after the last private tab's page is gone

# The tab's pause and play: every playing video and audio in the page (and
# its frames), and back on with the ones it paused - or, after a reload,
# the one that was part-way through. Run in Buddy's own script world, so
# the page can't see or change it.
PAUSE_JS = """(() => {
    const playing = [...document.querySelectorAll('video, audio')].filter(m => !m.paused && !m.ended);
    playing.forEach(m => m.pause());
    window.__buddyPaused = playing;
    return playing.length;
})()"""
PLAY_JS = """(() => {
    let media = (window.__buddyPaused || []).filter(m => m.isConnected);
    if (!media.length) {
        media = [...document.querySelectorAll('video, audio')]
            .filter(m => m.paused && m.currentTime > 0 && !m.ended).slice(0, 1);
    }
    media.forEach(m => m.play().catch(() => {}));
    window.__buddyPaused = [];
    return media.length;
})()"""


def _refresh(ui):
    try:
        ui.refresh()
    except RuntimeError:                       # its window has gone
        pass


class Tab:
    """One tab: what it shows, and its page once it has one."""

    def __init__(self, browser_page, tab_id, url="", title="", awake=False, private=False):
        self.browser = browser_page
        self.private = private
        self.paused = False                   # its sound paused from its tab (Buddy's button)
        self.paused_at = 0.0
        self.id = tab_id
        self.url = url
        self.title = title
        self.icon = ""
        self.awake = awake
        self.seen = time.time()
        self.loading = False
        self.progress = 0
        self.web = None                      # made the first time it's shown
        self.page = None

    @property
    def asleep(self):
        return self.web is None or self.page.lifecycleState() == QWebEnginePage.LifecycleState.Discarded

    def make_view(self, load=True):
        self.web = QWebEngineView()
        self.page = engine.TabPage(self, self.web)
        self.web.setPage(self.page)
        b = self.browser
        self.page.titleChanged.connect(lambda t: self._set(title=t))
        self.page.urlChanged.connect(lambda u: self._set(url=self._shown_url(u)))
        self.page.iconChanged.connect(self._icon)
        self.page.loadStarted.connect(lambda: self._set(loading=True, progress=0, paused=False))
        self.page.loadProgress.connect(lambda p: self._set(progress=p))
        self.page.loadFinished.connect(lambda ok: (self._set(loading=False, progress=100), ok and b.visited(self)))
        self.page.recentlyAudibleChanged.connect(self._audible)
        self.page.audioMutedChanged.connect(lambda _m: b.changed())
        self.page.lifecycleStateChanged.connect(lambda _s: b.changed())
        self.page.fullScreenRequested.connect(lambda request: b.full_screen(self, request))
        self.page.renderProcessPidChanged.connect(lambda _p: b.changed())
        b.stack.addWidget(self.web)
        if load:
            self.load()

    def load(self):
        if self.url:
            self.page.load(QUrl(self.url))
        else:
            self.page.setHtml(self.browser.start_html(self.private), QUrl("about:blank"))

    @staticmethod
    def _shown_url(url):
        text = url.toString()
        return "" if text in ("about:blank", "") else text

    def _audible(self, audible):
        if audible and time.time() - self.paused_at > PAUSE_SETTLE_S:
            self.paused = False               # played again, from the page itself
        self.browser.changed()

    def _set(self, **values):
        for key, value in values.items():
            setattr(self, key, value)
        self.browser.changed(save="url" in values or "title" in values)

    def _icon(self, icon):
        pixmap = icon.pixmap(32, 32)
        if pixmap.isNull():
            self.icon = ""
        else:
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.WriteOnly)
            pixmap.save(buffer, "PNG")
            self.icon = "data:image/png;base64," + base64.b64encode(bytes(data)).decode("ascii")
        self.browser.changed()

    def state(self, minutes, never):
        page = self.page
        audible = bool(page and page.recentlyAudible())
        info = {"url": self.url, "visible": bool(self.web and self.web.isVisible()), "audible": audible,
                "awake": self.awake, "asleep": self.asleep, "seen": self.seen}
        return {
            "id": self.id, "url": self.url, "private": self.private,
            "title": self.title or browser.site_of(self.url) or tr("Private tab" if self.private else "New tab"),
            "paused": self.paused and not self.asleep,
            "icon": self.icon, "loading": self.loading, "progress": self.progress, "audible": audible,
            "muted": bool(page and page.isAudioMuted()), "asleep": self.asleep and bool(self.url),
            "awake": self.awake, "never": browser.on_site(self.url, never),
            "why": browser.why_awake(info, minutes, never),
        }


class WebBrowserPage(ToolPage):
    tool_id = "web"
    display_name = "Web"
    category = "Web"

    # ------------------------------------------------------------ build --
    def build_ui(self):
        self.settings = self.host.tool_settings(SETTINGS_ID, dict(browser.DEFAULTS))
        self.tabs = []
        self.active = None
        self._next_id = 1
        self.downloads = []                   # QWebEngineDownloadRequest, newest last
        self._full = None                     # (tab, window) while a page is full screen
        self.memory = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.bar = BrowserBar(self)
        self.bar.setFixedHeight(76)
        self.stack = QStackedWidget(self)
        self.blank = QWidget()                # behind a tab that has no page yet
        self.stack.addWidget(self.blank)
        layout.addWidget(self.bar)
        layout.addWidget(self.stack, 1)

        profile = engine.profile()
        profile.downloadRequested.connect(self.download_requested)
        self.lists = filter_lists.FilterLists(self)
        self.lists.ready.connect(self._lists_ready)
        self._apply_blocking()
        engine.set_video_quality(self.settings.get("video_quality"))
        engine.set_youtube_ads(self.settings.get("youtube_ads", False))
        self._apply_region()
        get_i18n().language_changed.connect(self._apply_region)
        self._badge = None
        self._ducked = False
        self.ducker = ducking.Ducker(self, restore=self.settings.get("duck_restore"))
        self.ducker.ducked.connect(self._on_ducked)
        self.ducker.saved.connect(self._duck_saved)
        self._apply_ducking()
        self.ducker.start()

        self._save_timer = QTimer(self, singleShot=True, interval=SAVE_DELAY_MS, timeout=self._save)
        self._push_timer = QTimer(self, singleShot=True, interval=60, timeout=self.push)
        self._sleep_timer = QTimer(self, interval=CHECK_SLEEP_MS, timeout=self.check_sleep)
        self._sleep_timer.start()
        self._memory_timer = QTimer(self, interval=MEMORY_MS, timeout=self._measure)
        self._memory_timer.start()
        self._shortcuts()

        for saved in browser.clean_tabs(self.settings.get("tabs")):
            self._add(saved["url"], saved["title"], saved["awake"])
        if not self.tabs:
            self._add("")
        active = self.settings.get("active", 0)
        self.select(self.tabs[active if isinstance(active, int) and 0 <= active < len(self.tabs) else 0])

    def _shortcuts(self):
        keys = {
            "Ctrl+T": lambda: self.on_new_tab(), "Ctrl+N": lambda: self.on_new_tab(),
            "Ctrl+Shift+N": lambda: self.new_private_tab(), "Ctrl+W": lambda: self.on_close({"id": self.active.id}),
            "Ctrl+L": self.focus_address, "F6": self.focus_address, "Alt+D": self.focus_address,
            "Ctrl+Tab": lambda: self._cycle(1), "Ctrl+Shift+Tab": lambda: self._cycle(-1),
            "F5": self.on_reload, "Ctrl+R": self.on_reload, "Alt+Left": self.on_back, "Alt+Right": self.on_forward,
            "Ctrl+=": lambda: self.zoom(1), "Ctrl++": lambda: self.zoom(1), "Ctrl+-": lambda: self.zoom(-1),
            "Ctrl+0": lambda: self.zoom(0), "Ctrl+Shift+T": self.reopen_closed,
        }
        for key, run in keys.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(run)
        self._closed = []                     # (url, title) of closed tabs, newest last

    # ------------------------------------------------------------- tabs --
    def _add(self, url="", title="", awake=False, after=None, private=False):
        tab = Tab(self, self._next_id, url, title, awake, private)
        self._next_id += 1
        index = len(self.tabs) if after is None else self.tabs.index(after) + 1
        self.tabs.insert(index, tab)
        return tab

    def open_tab(self, url="", show=True, after=None, private=False):
        tab = self._add(url, after=after, private=private)
        if show:
            self.select(tab)
        else:
            tab.make_view()
        self.changed(save=True)
        return tab

    def open_tab_for_page(self, opener):
        """A page opened a new window (a link to a new tab, a sign-in
        pop-up): it becomes a tab beside its opener, shown, with a page the
        site fills in itself."""
        tab = self._add("", after=opener, private=opener.private)     # a private page's stay private
        tab.make_view(load=False)
        self.select(tab)
        self.changed(save=True)
        return tab

    def select(self, tab):
        if tab not in self.tabs:
            return
        if self.active is not None:
            self.active.seen = time.time()
        self.active = tab
        if tab.web is None:
            tab.make_view()
        elif tab.page.lifecycleState() != QWebEnginePage.LifecycleState.Active:
            tab.page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
        self.stack.setCurrentWidget(tab.web)
        tab.seen = time.time()
        if not tab.url:
            self.focus_address()
        self.changed(save=True)

    def close_tab(self, tab):
        if tab not in self.tabs:
            return
        if tab.url and not tab.private:
            self._closed = (self._closed + [(tab.url, tab.title)])[-20:]
        index = self.tabs.index(tab)
        self.tabs.remove(tab)
        if tab.web is not None:
            self.stack.removeWidget(tab.web)
            tab.web.deleteLater()
        if not self.tabs:
            self._add("")
        if tab is self.active:
            self.active = None
            self.select(self.tabs[min(index, len(self.tabs) - 1)])
        if tab.private and not any(t.private for t in self.tabs):
            QTimer.singleShot(DROP_PRIVATE_MS, self._drop_private)
        self.changed(save=True)

    def new_private_tab(self, url=""):
        self.open_tab(url, after=self.active, private=True)

    def _drop_private(self):
        """The last private tab closed: all it knew forgotten."""
        if not any(t.private for t in self.tabs):
            engine.drop_private()

    def reopen_closed(self):
        if self._closed:
            url, _title = self._closed.pop()
            self.open_tab(url)

    def _cycle(self, step):
        if self.tabs:
            self.select(self.tabs[(self.tabs.index(self.active) + step) % len(self.tabs)])

    def tab(self, payload):
        wanted = (payload or {}).get("id")
        return next((t for t in self.tabs if t.id == wanted), None)

    # ------------------------------------------------------------ sleep --
    def never_sleep(self):
        return browser.clean_sites(self.settings.get("never_sleep"))

    def sleep_minutes(self):
        value = self.settings.get("sleep_after", browser.DEFAULT_SLEEP)
        return value if value in browser.SLEEP_CHOICES else browser.DEFAULT_SLEEP

    def check_sleep(self):
        """Background tabs past their time go to sleep (their page discarded,
        reloaded when they're next shown)."""
        now, minutes, never = time.time(), self.sleep_minutes(), self.never_sleep()
        for tab in self.tabs:
            if tab.web is not None and tab.web.isVisible():
                tab.seen = now
            elif tab.web is not None:
                info = {"url": tab.url, "visible": False, "audible": tab.page.recentlyAudible(),
                        "awake": tab.awake, "asleep": tab.asleep, "seen": tab.seen}
                if browser.should_sleep(info, now, minutes, never):
                    self.sleep(tab)

    def sleep(self, tab):
        if tab.web is not None and not tab.web.isVisible() and not tab.asleep and tab.url:
            tab.paused = False                # it starts over when it wakes
            tab.page.setLifecycleState(QWebEnginePage.LifecycleState.Discarded)
            self.changed()

    def keep_awake(self, tab, on):
        tab.awake = bool(on)
        self.changed(save=True)

    def set_never_sleep(self, site, on):
        sites = self.never_sleep()
        if on and site not in sites:
            sites.append(site)
        elif not on:
            sites = [s for s in sites if s != site and not site.endswith("." + s)]
        self.settings["never_sleep"] = sites
        self.settings.save()
        self.changed()

    # ------------------------------------------------------------- state --
    def emit(self, name, payload=None):
        """To the bar's view (web/browser.js)."""
        self.bar.emit(name, payload)

    def on_size(self, payload):
        height = (payload or {}).get("height")
        if isinstance(height, (int, float)) and 30 <= height <= 200:
            self.bar.setFixedHeight(int(round(height)))

    def changed(self, save=False):
        if save:
            self._save_timer.start()
        self._push_timer.start()

    def push(self):
        if not self.tabs:
            return
        audible = any(self._playing(t) for t in self.tabs)
        self.ducker.active = audible
        # Just paused, a tab still counts as audible for a moment: its mark is "paused" at once.
        sounding = any(self._playing(t) and not t.paused for t in self.tabs)
        badge = "sound" if sounding else "paused" if any(self._paused(t) for t in self.tabs) else None
        if badge != self._badge:
            self._badge = badge
            if hasattr(self.host, "refresh_badges"):
                self.host.refresh_badges()
        minutes, never = self.sleep_minutes(), self.never_sleep()
        page = self.active.page if self.active else None
        history = page.history() if page else None
        busy = [d for d in self.downloads if not d.isFinished()]
        total = sum(max(d.totalBytes(), 0) for d in busy)
        got = sum(d.receivedBytes() for d in busy)
        self.emit("browser", {
            "tabs": [t.state(minutes, never) for t in self.tabs],
            "active": self.active.id if self.active else None,
            "url": self.active.url if self.active else "",
            "back": bool(history and history.canGoBack()), "forward": bool(history and history.canGoForward()),
            "zoom": round((self.active.web.zoomFactor() if self.active and self.active.web else 1) * 100),
            "memory": self.memory,
            "ducked": self._ducked,
            "downloads": {"busy": len(busy), "progress": (got / total) if total else None,
                          "recent": [self._download_state(d) for d in self.downloads[-RECENT_DOWNLOADS:]][::-1]},
        })

    # --------------------------------------------------------- autofill --
    def visited(self, tab):
        if tab.url and not tab.private:
            self._remember(tab.url)

    def _remember(self, url, typed=False):
        if self.settings.get("suggest", True):
            sites = self.settings.get("sites")
            self.settings["sites"] = browser.record_site(sites if isinstance(sites, dict) else {}, url, time.time(), typed)
            self._save_timer.start()

    def on_sites(self, _payload=None):
        """The address bar has the focus: the sites it may finish typing with."""
        if not self.settings.get("suggest", True):
            self.emit("sites", {"sites": []})
            return
        links = [link["url"] for link in getattr(self.host, "links", []) or []]
        ranked = browser.ranked_sites(self.settings.get("sites"), links,
                                      [t.url for t in self.tabs if not t.private], time.time())
        self.emit("sites", {"sites": ranked[:500]})

    def forget_sites(self):
        self.settings["sites"] = {}
        self.settings.save()

    # ---------------------------------------------------------- ducking --
    def rail_badge(self):
        return self._badge

    @staticmethod
    def _playing(tab):
        return tab.page is not None and tab.page.recentlyAudible() and not tab.page.isAudioMuted()

    @staticmethod
    def _paused(tab):
        return tab.paused and not tab.asleep

    def rail_media(self):
        """The button beside "Web" in the sidebar: pauses every tab playing
        sound, or plays again the ones it paused."""
        playing = [t for t in self.tabs if self._playing(t) and not t.paused]
        for tab in playing or [t for t in self.tabs if self._paused(t)]:
            self.on_media({"id": tab.id})

    def duck_mode(self):
        mode = self.settings.get("duck", ducking.DEFAULT_MODE)
        return mode if mode in ducking.MODES else ducking.DEFAULT_MODE

    def duck_level(self):
        level = ducking.clean_level(self.settings.get("duck_level", ducking.DEFAULT_LEVEL))
        return ducking.DEFAULT_LEVEL if level is None else level

    def _apply_ducking(self):
        self.ducker.mode = self.duck_mode() if audio_sessions.available else "off"
        self.ducker.level = self.duck_level() / 100

    def _on_ducked(self, on):
        self._ducked = bool(on)
        self.changed()

    def _duck_saved(self, volume):
        """While the Web tab is down, the volume it came from, kept so it's
        put back next time should Buddy close before it comes back up."""
        if self.settings.get("duck_restore") != volume:
            self.settings["duck_restore"] = volume
            self.settings.save()

    def _measure(self):
        if not self.isVisible():
            return
        used = engine.memory_bytes(t.page.renderProcessPid() for t in self.tabs if t.page is not None)
        memory = None if used is None else round(used / (1024 * 1024))
        if memory != self.memory:
            self.memory = memory
            self.changed()

    def _save(self):
        kept = [t for t in self.tabs if not t.private]           # private tabs are never saved
        saved = [{"url": t.url, "title": t.title, "awake": t.awake} for t in kept]
        self.settings["tabs"] = browser.clean_tabs(saved)
        self.settings["active"] = kept.index(self.active) if self.active in kept else 0
        self.settings.save()

    def start_html(self, private=False):
        t = self.host.theme_tokens()
        links = getattr(self.host, "links", []) or []
        colors = {"bg": t["surface"], "card": t["surface_container"], "text": t["on_surface"], "dim": t["outline"],
                  "accent": t["primary"], "border": t["outline_variant"], "font": "'Segoe UI', 'Open Sans', sans-serif"}
        return browser.start_page(colors, links, self.settings.get("search", browser.DEFAULT_ENGINE), tr, self.region(),
                                  self.shortcuts(), private)

    # ------------------------------------------------- new-tab shortcuts --
    def shortcuts(self):
        return browser.clean_shortcuts(self.settings.get("shortcuts"))

    def start_page_action(self, tab, action):
        """The new-tab page's + (add a shortcut) or a shortcut's x."""
        what = browser.start_page_action(action)
        if what is None:
            return
        kind, index = what
        if kind == "add":
            self.add_shortcut()
            return
        shortcuts = self.shortcuts()
        if 0 <= index < len(shortcuts):
            del shortcuts[index]
            self._set_shortcuts(shortcuts)

    def add_shortcut(self):
        shortcuts = self.shortcuts()
        if len(shortcuts) >= browser.MAX_SHORTCUTS:
            alert(self.window(), "New tab", f"The new-tab page holds up to {browser.MAX_SHORTCUTS} shortcuts.")
            return
        dialog = LinkDialog(self.window())
        if dialog.exec() and dialog.link is not None:
            if link_bar.is_path(dialog.link["url"]):
                alert(self.window(), "New tab", "Shortcuts on the new-tab page are web addresses. "
                                                "Folders go on the link bar.")
            else:
                self._set_shortcuts(shortcuts + [{"name": dialog.link["name"], "url": dialog.link["url"]}])
        dialog.deleteLater()

    def _set_shortcuts(self, shortcuts):
        self.settings["shortcuts"] = browser.clean_shortcuts(shortcuts)
        self.settings.save()
        self._redraw_start_pages()

    def region(self):
        region = self.settings.get("region", browser.AUTO_REGION)
        return region if region in browser.REGIONS else browser.AUTO_REGION

    def _apply_region(self, _language=None):
        """Sites hear the user's language as spoken in their region."""
        language = LANGUAGE_CODES.get(get_i18n().language, "en")
        engine.set_languages(browser.accept_language(language, self.region()))

    def focus_address(self):
        self.bar.view.setFocus()
        self.emit("focus_address")

    def zoom(self, step):
        view = self.active.web if self.active else None
        if view is None:
            return
        now = view.zoomFactor()
        if step == 0:
            view.setZoomFactor(1.0)
        else:
            bigger = [z for z in ZOOMS if z > now + 0.001]
            smaller = [z for z in ZOOMS if z < now - 0.001]
            view.setZoomFactor((bigger[0] if bigger else ZOOMS[-1]) if step > 0 else (smaller[-1] if smaller else ZOOMS[0]))
        self.changed()

    # ------------------------------------------------------ full screen --
    def full_screen(self, tab, request):
        """A video's full-screen button: the page fills the screen in a
        window of its own, and comes back when it's done (Esc)."""
        request.accept()
        if request.toggleOn() and self._full is None:
            window = QWidget(None, Qt.Window | Qt.FramelessWindowHint)
            box = QVBoxLayout(window)
            box.setContentsMargins(0, 0, 0, 0)
            self.stack.removeWidget(tab.web)
            box.addWidget(tab.web)
            esc = QShortcut(QKeySequence("Esc"), window)
            esc.activated.connect(lambda: tab.page.triggerAction(QWebEnginePage.ExitFullScreen))
            window.showFullScreen()
            self._full = (tab, window)
        elif not request.toggleOn() and self._full is not None:
            full_tab, window = self._full
            self._full = None
            self.stack.addWidget(full_tab.web)
            if full_tab is self.active:
                self.stack.setCurrentWidget(full_tab.web)
            window.hide()
            window.deleteLater()

    # -------------------------------------------------------- downloads --
    def downloads_folder(self):
        folder = self.settings.get("downloads") or browser.default_downloads()
        return folder if os.path.isdir(folder) else browser.default_downloads()

    def download_requested(self, item):
        folder = self.downloads_folder()
        os.makedirs(folder, exist_ok=True)
        item.setDownloadDirectory(folder)
        item.setDownloadFileName(browser.unique_name(folder, item.downloadFileName()))
        item.receivedBytesChanged.connect(self.changed)
        item.isFinishedChanged.connect(lambda: self._download_done(item))
        self.downloads = (self.downloads + [item])[-20:]
        item.accept()
        self.changed()

    def _download_done(self, item):
        if item.state() == QWebEngineDownloadRequest.DownloadCompleted:
            self.emit("toast", {"text": tr("Saved {name}").replace("{name}", item.downloadFileName()),
                                    "download": len(self.downloads) - 1 - self.downloads.index(item)
                                    if item in self.downloads else None})
        self.changed()

    @staticmethod
    def _download_state(item):
        done = item.state() == QWebEngineDownloadRequest.DownloadCompleted
        total = item.totalBytes()
        return {"name": item.downloadFileName(), "done": done,
                "failed": item.state() in (QWebEngineDownloadRequest.DownloadCancelled,
                                           QWebEngineDownloadRequest.DownloadInterrupted),
                "progress": (item.receivedBytes() / total) if total > 0 else None}

    def _download_path(self, recent_index):
        recent = self.downloads[-RECENT_DOWNLOADS:][::-1]
        if isinstance(recent_index, int) and 0 <= recent_index < len(recent):
            item = recent[recent_index]
            path = os.path.join(item.downloadDirectory(), item.downloadFileName())
            if item.state() == QWebEngineDownloadRequest.DownloadCompleted and os.path.exists(path):
                return path
        return None

    def peek_downloads(self):
        FolderPeek(self.window(), self.downloads_folder(), tr("Downloads")).show()

    # ---------------------------------------------------------- the bar --
    def on_new_tab(self, payload=None):
        self.open_tab("", after=self.active, private=bool((payload or {}).get("private")))

    def on_close(self, payload):
        tab = self.tab(payload)
        if tab is not None:
            self.close_tab(tab)

    def on_move(self, payload):
        """A tab dragged to another place in the row."""
        tab, index = self.tab(payload), (payload or {}).get("index")
        if tab is None or not isinstance(index, int):
            return
        self.tabs.remove(tab)
        self.tabs.insert(max(0, min(index, len(self.tabs))), tab)
        self.changed(save=True)

    def _apply_video_quality(self):
        engine.set_video_quality(self.settings.get("video_quality"))
        script = browser.youtube_quality_script(browser.video_quality(self.settings.get("video_quality")))
        for tab in self.tabs:                  # open YouTube pages switch now, not on their next video
            if tab.page is not None and not tab.asleep and browser.on_site(tab.url, list(browser.YOUTUBE_HOSTS)):
                for frame in self._frames(tab.page.mainFrame()):
                    frame.runJavaScript(script, QWebEngineScript.ScriptWorldId.MainWorld, lambda _r: None)

    # --------------------------------------------------------- blocking --
    def allowed_sites(self):
        return browser.clean_sites(self.settings.get("allow_ads"))

    def _apply_blocking(self):
        f = engine.tracker_filter()
        f.mode = browser.blocking(self.settings)
        f.allowed = frozenset(self.allowed_sites())
        if f.mode == "strong" and self.lists.filters is None:
            self.lists.start()

    def _lists_ready(self, built):
        engine.tracker_filter().filters = built

    def allow_ads(self, site, on):
        sites = self.allowed_sites()
        if on and site not in sites:
            sites.append(site)
        elif not on:
            sites = [s for s in sites if s != site and not site.endswith("." + s)]
        self.settings["allow_ads"] = sites
        self.settings.save()
        self._apply_blocking()
        for tab in self.tabs:                   # see the site as it now is
            if tab.page is not None and not tab.asleep and browser.on_site(tab.url, [site]):
                tab.page.triggerAction(QWebEnginePage.Reload)

    def on_select(self, payload):
        tab = self.tab(payload)
        if tab is not None:
            self.select(tab)

    def on_go(self, payload):
        text = (payload or {}).get("text")
        engine = self.settings.get("search", browser.DEFAULT_ENGINE)
        url = browser.address_to_url(text, engine, self.region())
        private = self.active is not None and self.active.private
        if url and not private and url != browser.search_url((text or "").strip(), engine, self.region()):
            self._remember(url, typed=True)               # typed sites come first next time
        if url and self.active is not None:
            self.active.url = url
            if self.active.web is None:
                self.active.make_view()
            else:
                self.active.page.load(QUrl(url))
            self.active.web.setFocus()
            self.changed(save=True)

    def on_back(self, _payload=None):
        if self.active and self.active.page:
            self.active.page.triggerAction(QWebEnginePage.Back)

    def on_forward(self, _payload=None):
        if self.active and self.active.page:
            self.active.page.triggerAction(QWebEnginePage.Forward)

    def on_reload(self, _payload=None):
        if self.active and self.active.page:
            if self.active.url:
                self.active.page.triggerAction(QWebEnginePage.Reload)
            else:
                self.active.load()

    def on_stop(self, _payload=None):
        if self.active and self.active.page:
            self.active.page.triggerAction(QWebEnginePage.Stop)

    def on_mute(self, payload):
        tab = self.tab(payload)
        if tab is not None and tab.page is not None:
            tab.page.setAudioMuted(not tab.page.isAudioMuted())

    def on_media(self, payload):
        """The tab's pause / play button."""
        tab = self.tab(payload)
        if tab is None or tab.page is None or tab.asleep:
            return
        pausing = not tab.paused
        script = PAUSE_JS if pausing else PLAY_JS
        tab.paused = pausing
        tab.paused_at = time.time() if pausing else 0.0
        frames = self._frames(tab.page.mainFrame())
        found = []

        def done(count):
            found.append(count if isinstance(count, (int, float)) else 0)
            if len(found) == len(frames) and pausing and not any(found):
                tab.paused = False            # nothing in the page was playing after all
                self.changed()

        for frame in frames:
            frame.runJavaScript(script, QWebEngineScript.ScriptWorldId.ApplicationWorld, done)
        self.changed()

    def _frames(self, frame):
        out = [frame]
        for child in frame.children():
            out += self._frames(child)
        return out

    def on_zoom_reset(self, _payload=None):
        self.zoom(0)

    def on_downloads(self, _payload=None):
        self.peek_downloads()

    def on_drag_download(self, payload):
        path = self._download_path((payload or {}).get("index"))
        start_file_drag(self, self.bar.view, [path] if path else [])
        self.emit("drag_done")

    def on_menu(self, payload):
        tab = self.tab(payload)
        if tab is None:
            return
        menu = QMenu(self)
        site = browser.site_of(tab.url)
        add = lambda text, run, enabled=True: menu.addAction(tr(text), run).setEnabled(enabled)
        add("Reload", lambda: (self.select(tab), self.on_reload()), bool(tab.url))
        add("Duplicate", lambda: self.open_tab(tab.url, after=tab, private=tab.private), bool(tab.url))
        if not tab.private:
            add("Open in a private tab", lambda: self.open_tab(tab.url, after=tab, private=True), bool(tab.url))
        muted = bool(tab.page and tab.page.isAudioMuted())
        add("Unmute tab" if muted else "Mute tab", lambda: self.on_mute({"id": tab.id}), tab.page is not None)
        menu.addSeparator()
        awake = menu.addAction(tr("Keep this tab awake"), lambda: self.keep_awake(tab, not tab.awake))
        awake.setCheckable(True)
        awake.setChecked(tab.awake)
        if site:
            never = menu.addAction(tr("Never sleep on {site}").replace("{site}", site),
                                   lambda: self.set_never_sleep(site, not browser.on_site(tab.url, self.never_sleep())))
            never.setCheckable(True)
            never.setChecked(browser.on_site(tab.url, self.never_sleep()))
        if site:
            allowed = browser.allowed_site(browser.site_of(tab.url), self.allowed_sites())
            ads = menu.addAction(tr("Allow ads on {site}").replace("{site}", site),
                                 lambda: self.allow_ads(site, not allowed))
            ads.setCheckable(True)
            ads.setChecked(allowed)
            ads.setEnabled(browser.blocking(self.settings) != "off")
        add("Sleep now", lambda: self.sleep(tab),
            tab.web is not None and not tab.web.isVisible() and not tab.asleep and bool(tab.url))
        menu.addSeparator()
        add("Add to link bar…", lambda: self.host.add_link(tab.url), bool(tab.url) and hasattr(self.host, "add_link"))
        add("Open in your browser", lambda: engine.open_outside(tab.url), bool(tab.url))
        menu.addSeparator()
        add("Close tab", lambda: self.close_tab(tab))
        add("Close other tabs", lambda: [self.close_tab(t) for t in list(self.tabs) if t is not tab], len(self.tabs) > 1)
        menu.exec(QCursor.pos())
        menu.deleteLater()

    def on_more(self, _payload=None):
        menu = QMenu(self)
        tab = self.active
        menu.addAction(tr("New tab"), self.on_new_tab)
        menu.addAction(tr("New private tab"), lambda: self.new_private_tab())
        menu.addAction(tr("Reopen closed tab"), self.reopen_closed).setEnabled(bool(self._closed))
        menu.addSeparator()
        menu.addAction(tr("Zoom in"), lambda: self.zoom(1))
        menu.addAction(tr("Zoom out"), lambda: self.zoom(-1))
        menu.addAction(tr("Actual size"), lambda: self.zoom(0))
        menu.addSeparator()
        menu.addAction(tr("Add to link bar…"), lambda: self.host.add_link(tab.url)).setEnabled(
            bool(tab and tab.url) and hasattr(self.host, "add_link"))
        menu.addAction(tr("Open in your browser"), lambda: engine.open_outside(tab.url)).setEnabled(bool(tab and tab.url))
        menu.addAction(tr("Sleep other tabs now"),
                       lambda: [self.sleep(t) for t in self.tabs if t is not self.active])
        menu.addSeparator()
        menu.addAction(tr("Add a shortcut to the new-tab page…"), self.add_shortcut)
        menu.addAction(tr("Downloads"), self.peek_downloads)
        if hasattr(self.host, "open_settings"):
            menu.addAction(tr("Web settings…"), lambda: self.host.open_settings(self.tool_id))
        menu.exec(QCursor.pos())
        menu.deleteLater()

    # --------------------------------------------------------- the shell --
    def on_shown(self):
        if self.active is not None:
            self.active.seen = time.time()
        self.changed()

    def on_theme_changed(self):
        self.bar.on_theme_changed()
        self._redraw_start_pages()

    def _redraw_start_pages(self):
        """New-tab pages showing: drawn again in the theme, search and region now set."""
        for tab in self.tabs:
            if tab.web is not None and not tab.url and not tab.asleep:
                tab.load()

    def on_app_quitting(self):
        self.ducker.stop()                    # the Web tab's volume put back first
        engine.save_sign_ins()
        self._save_timer.stop()
        self._save()

    # --------------------------------------------------------- settings --
    def settings_fields(self):
        s = self.settings
        engines = [(key, name) for key, (name, _url) in browser.SEARCH_ENGINES.items()]
        return [
            sf.heading("Tabs"),
            sf.select("sleep_after", "Put background tabs to sleep", self.sleep_minutes(),
                      list(browser.SLEEP_CHOICES.items()),
                      tooltip="A sleeping tab gives its memory back and reloads when you next open it."),
            sf.hint("A tab playing sound never sleeps, so music keeps going while you work in Resolve. Nor does a "
                    "tab you keep awake - right-click it."),
            sf.textarea("never_sleep", "Sites that never sleep", "\n".join(self.never_sleep()),
                        placeholder="music.youtube.com\nopen.spotify.com", rows=4,
                        hint_text="One site per line. A site covers everything under it."),
            *self._sound_fields(),
            sf.heading("Video"),
            sf.select("video_quality", "YouTube video quality", browser.video_quality(s.get("video_quality")),
                      list(browser.VIDEO_QUALITIES.items())),
            sf.hint("YouTube and YouTube Music play at this quality, or the nearest below it a video has - lower "
                    "uses less data and memory. You can still change one video's quality in YouTube's own menu. "
                    "Other sites choose their own."),
            sf.heading("Search"),
            sf.select("search", "Search with", s.get("search", browser.DEFAULT_ENGINE), engines, raw=True),
            sf.select("region", "Region", self.region(),
                      [(browser.AUTO_REGION, "Automatic")]
                      + sorted(((code, name) for code, (name, _kl, _lang) in browser.REGIONS.items()),
                               key=lambda item: tr(item[1])),
                      tooltip="Which country's results a search gives, and the region sites are told you're in. "
                              "Automatic leaves it to the search engine and the sites."),
            sf.check("suggest", "Finish addresses as you type", s.get("suggest", True),
                     hint_text="Type \"goo\" and the address bar offers google.com. Buddy counts the sites you "
                               "visit to suggest them - only the site, never the page."),
            sf.buttons(("Forget visited sites", "forget_sites")),
            sf.heading("Privacy"),
            sf.select("blocking", "Block", browser.blocking(s), list(browser.BLOCKING.items()),
                      tooltip="Pages load faster and use less memory with ads and trackers blocked."),
            *self._list_fields(),
            sf.check("youtube_ads", "Remove YouTube's video ads", bool(s.get("youtube_ads", False)),
                     hint_text="YouTube's ads come from YouTube itself, so the filter lists can't stop them. This "
                               "takes them out of the video data YouTube sends its player. YouTube works against it: "
                               "it may show its \"ad blockers aren't allowed\" message, or ads may come back until "
                               "Buddy is updated. Reload YouTube after turning it on or off."),
            sf.textarea("allow_ads", "Sites where nothing's blocked", "\n".join(self.allowed_sites()),
                        placeholder="example.com", rows=3,
                        hint_text="One site per line, for a site that doesn't work with its ads blocked - or "
                                  "right-click its tab."),
            sf.buttons(("Clear cookies and site data…", "clear_data",
                        {"tooltip": "Signs you out of every site in the Web tab."})),
            sf.heading("Downloads"),
            sf.text("downloads", "Save downloads to", self.downloads_folder(), browse="browse_downloads"),
        ]

    def _list_fields(self):
        if browser.blocking(self.settings) != "strong":
            return []
        text, tone = self.lists.status()
        return [
            sf.status("Filter lists", text, tone,
                      hint_text="EasyList, EasyPrivacy and uBlock Origin's lists, from their makers, updated "
                                f"every {filter_lists.UPDATE_DAYS} days. They block ads and trackers, and hide "
                                "the boxes ads leave behind."),
            sf.buttons(("Update the lists now", "update_lists")),
        ]

    def _sound_fields(self):
        if not audio_sessions.available:
            return [sf.heading("Sound"), sf.hint("Lowering the Web tab's sound works on Windows only for now.")]
        return [
            sf.heading("Sound"),
            sf.select("duck", "Lower the Web tab's sound", self.duck_mode(), list(ducking.MODES.items()),
                      tooltip="Music in the Web tab drops while Resolve plays your timeline, and comes back "
                              "when it stops."),
            sf.slider("duck_level", "Lower it to", self.duck_level(), 0, 100, ducking.DEFAULT_LEVEL,
                      {v: f"{v}%" for v in range(0, 101, ducking.LEVEL_STEP)}, step=ducking.LEVEL_STEP,
                      hint_text="How loud the Web tab stays while it's lowered: 0% is silent, 100% leaves it "
                                "as it is."),
        ]

    def on_setting(self, key, value, ui):
        if key == "sleep_after":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return
            if value in browser.SLEEP_CHOICES:
                self.settings["sleep_after"] = value
        elif key == "never_sleep":
            self.settings["never_sleep"] = browser.clean_sites(value)
        elif key == "search" and value in browser.SEARCH_ENGINES:
            self.settings["search"] = value
        elif key == "region" and (value in browser.REGIONS or value == browser.AUTO_REGION):
            self.settings["region"] = value
            self._apply_region()
        elif key == "duck" and value in ducking.MODES:
            self.settings["duck"] = value
            self._apply_ducking()
        elif key == "duck_level":
            value = ducking.clean_level(value)
            if value is None:
                return
            self.settings["duck_level"] = value
            self._apply_ducking()
        elif key == "video_quality" and value in browser.VIDEO_QUALITIES:
            self.settings["video_quality"] = value
            self._apply_video_quality()
        elif key == "suggest":
            self.settings["suggest"] = bool(value)
            if not value:
                self.settings["sites"] = {}
        elif key == "blocking" and value in browser.BLOCKING:
            self.settings["blocking"] = value
            self._apply_blocking()
            ui.refresh()
        elif key == "youtube_ads":
            self.settings["youtube_ads"] = bool(value)
            engine.set_youtube_ads(bool(value))
        elif key == "allow_ads":
            self.settings["allow_ads"] = browser.clean_sites(value)
            self._apply_blocking()
        elif key == "downloads" and isinstance(value, str):
            folder = value.strip().strip('"')
            if folder and not os.path.isdir(folder):
                ui.alert("Downloads", "That folder isn't there. Choose one with Browse.")
                return
            self.settings["downloads"] = folder
        else:
            return
        self.settings.save()
        if key in ("search", "region"):
            self._redraw_start_pages()
        self.changed()

    def on_settings_action(self, action, ui):
        if action == "browse_downloads":
            folder = QFileDialog.getExistingDirectory(ui.parent(), tr("Save downloads to"), self.downloads_folder())
            if folder:
                self.settings["downloads"] = os.path.normpath(folder)
                self.settings.save()
                ui.refresh()
        elif action == "update_lists":
            self.lists.start(force=True)
            # Once, when they're done: Settings may have been closed by then.
            self.lists.changed.connect(lambda: _refresh(ui), Qt.SingleShotConnection)
            ui.status("Updating the filter lists…", "")
        elif action == "forget_sites":
            self.forget_sites()
            ui.status("Forgotten.", "success")
        elif action == "clear_data":
            if ui.confirm("Clear cookies and site data",
                          "Sign out of every site in the Web tab and clear what they've stored?", ok="Clear",
                          danger=True):
                engine.clear_site_data()
                self.forget_sites()
                ui.status("Signed out. Restart Buddy to finish clearing what sites stored.", "success")
