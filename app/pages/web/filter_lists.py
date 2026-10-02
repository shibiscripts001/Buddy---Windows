#!/usr/bin/env python3
"""
The ad blocker's filter lists (pages/web/filters.py reads them): fetched
from their makers, kept in ~/.buddy/web-filters, fetched again when
they're UPDATE_DAYS old, and read into Filters off the GUI thread - so
Buddy starts at once and the Web tab blocks with the built-in tracker list
(browser.is_tracker) until the lists are ready.

A list that doesn't download, or doesn't look like a filter list, keeps
its last good copy.
"""

import os
import threading
import time
import urllib.request

from PySide6.QtCore import QObject, Signal

from core.i18n import tr
from core.settings_store import BUDDY_DIR
from pages.web import filters

FOLDER = os.path.join(BUDDY_DIR, "web-filters")
UPDATE_DAYS = 4
TIMEOUT_S = 30
MAX_BYTES = 20 * 1024 * 1024
# key -> (name shown, address). The order is the order they're read in.
LISTS = {
    "easylist": ("EasyList", "https://easylist.to/easylist/easylist.txt"),
    "easyprivacy": ("EasyPrivacy", "https://easylist.to/easylist/easyprivacy.txt"),
    "ublock": ("uBlock filters", "https://ublockorigin.github.io/uAssets/filters/filters.txt"),
    "ublock-privacy": ("uBlock filters - Privacy", "https://ublockorigin.github.io/uAssets/filters/privacy.txt"),
    "ublock-quick-fixes": ("uBlock filters - Quick fixes",
                           "https://ublockorigin.github.io/uAssets/filters/quick-fixes.txt"),
}


def path(key, folder=FOLDER):
    return os.path.join(folder, f"{key}.txt")


def looks_like_a_list(data):
    """A filter list starts with its header ([Adblock Plus ...] or
    ! Title: ...), not an error page."""
    head = data[:400].lstrip(b"\xef\xbb\xbf").lstrip()
    return len(data) > 1000 and (head.startswith(b"[Adblock") or head.startswith(b"! Title") or
                                 head.startswith(b"!"))


def age_days(key, folder=FOLDER, now=None):
    try:
        return ((now or time.time()) - os.path.getmtime(path(key, folder))) / 86400
    except OSError:
        return None


def fetch(key, folder=FOLDER, opener=urllib.request.urlopen):
    """Downloads one list over its last copy. True if it's new."""
    _name, url = LISTS[key]
    request = urllib.request.Request(url, headers={"User-Agent": "Buddy (DaVinci Resolve tools)"})
    with opener(request, timeout=TIMEOUT_S) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or not looks_like_a_list(data):
        return False
    os.makedirs(folder, exist_ok=True)
    temp = path(key, folder) + ".new"
    with open(temp, "wb") as fh:
        fh.write(data)
    os.replace(temp, path(key, folder))
    return True


def read_all(folder=FOLDER):
    """The kept lists' texts, in LISTS order."""
    texts = []
    for key in LISTS:
        try:
            with open(path(key, folder), encoding="utf-8", errors="replace") as fh:
                texts.append(fh.read())
        except OSError:
            pass
    return texts


def last_updated(folder=FOLDER):
    times = [os.path.getmtime(path(k, folder)) for k in LISTS if os.path.exists(path(k, folder))]
    return max(times) if times else None


class FilterLists(QObject):
    """Keeps the lists and their Filters. ready(Filters) when they're read
    (on the GUI thread, by Qt's queued signal); changed() whenever the
    state Settings shows changes."""

    ready = Signal(object)
    changed = Signal()

    def __init__(self, parent=None, folder=FOLDER):
        super().__init__(parent)
        self.folder = folder
        self.filters = None
        self.state = "idle"                   # idle, updating, ready, failed
        self.error = ""
        self.failed = []
        self._thread = None

    def start(self, force=False):
        """Reads what's kept, fetching lists that are missing or old (all
        of them if force). Does nothing while it's already at it."""
        if self._thread is not None and self._thread.is_alive():
            return
        self.state = "updating"
        self.changed.emit()
        self._thread = threading.Thread(target=self._run, args=(force,), name="buddy-filter-lists", daemon=True)
        self._thread.start()

    def _run(self, force):
        failed = []
        for key in LISTS:
            age = age_days(key, self.folder)
            if force or age is None or age >= UPDATE_DAYS:
                try:
                    if not fetch(key, self.folder):
                        failed.append(LISTS[key][0])
                except Exception:  # noqa: BLE001 - offline, a site down: the last copy stays
                    failed.append(LISTS[key][0])
        texts = read_all(self.folder)
        built = filters.build(texts) if texts else None
        self.failed = failed
        self.error = ("Couldn't update " + ", ".join(failed)) if failed else ""
        self._finished(built)

    def _finished(self, built):
        if built is not None:
            self.filters = built
            self.state = "ready"
            self.ready.emit(built)
        else:
            self.state = "failed"
        self.changed.emit()

    def status(self):
        """What Settings says about the lists: (text, tone)."""
        if self.state == "updating":
            return tr("Updating the filter lists…"), ""
        if self.filters is None:
            if self.state == "failed":
                return tr("The filter lists couldn't be downloaded. Buddy blocks known trackers until they are."),                     "warning"
            return tr("The filter lists aren't downloaded yet."), "warning"
        when = last_updated(self.folder)
        rules = self.filters.counts["network"] + self.filters.counts["cosmetic"]
        text = (tr("{rules} rules from {count} lists, updated {date}.")
                .replace("{rules}", f"{rules:,}").replace("{count}", str(len(read_all(self.folder))))
                .replace("{date}", time.strftime("%d %b %Y", time.localtime(when)) if when else "-"))
        if self.error:
            text += " " + tr("Couldn't update {lists}.").replace("{lists}", ", ".join(self.failed))
        return text, "warning" if self.error else "success"
