#!/usr/bin/env python3
"""
The Web tab's browser, the parts that need no Qt (pages/web/page.py has
the rest): what an address bar entry goes to, which tabs go to sleep,
which requests are ads or trackers, what's saved between runs, and the
new-tab page.

Sleeping is how the tab stays light: a background tab nobody has looked
at for a while is discarded - its memory given back - and reloads when
it's next shown, as Chrome's own tab discarding does. A tab playing
sound never sleeps (music keeps going with the Web tab out of sight), and
nor does one the user keeps awake or one on a site they listed.
"""

import datetime
import html
import os
import re
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from core import link_bar
from pages.web.blocklist import BLOCKED

# Stored key -> (name shown, search address with {q}).
SEARCH_ENGINES = {
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q={q}"),
    "google": ("Google", "https://www.google.com/search?q={q}"),
    "bing": ("Bing", "https://www.bing.com/search?q={q}"),
    "brave": ("Brave Search", "https://search.brave.com/search?q={q}"),
}
DEFAULT_ENGINE = "duckduckgo"

# Where the user is, set by hand (Settings > Tools > Web > Region): which
# country's results a search gives, and the language-and-region sites are
# told the user prefers. Country code -> (name, DuckDuckGo's region, the
# region's own language). "" is Automatic: the engines and sites decide.
AUTO_REGION = ""
REGIONS = {
    "US": ("United States", "us-en", "en"), "GB": ("United Kingdom", "uk-en", "en"),
    "CA": ("Canada", "ca-en", "en"), "AU": ("Australia", "au-en", "en"), "NZ": ("New Zealand", "nz-en", "en"),
    "IE": ("Ireland", "ie-en", "en"), "IN": ("India", "in-en", "en"), "SG": ("Singapore", "sg-en", "en"),
    "PH": ("Philippines", "ph-en", "en"), "ZA": ("South Africa", "za-en", "en"),
    "DE": ("Germany", "de-de", "de"), "AT": ("Austria", "at-de", "de"), "CH": ("Switzerland", "ch-de", "de"),
    "FR": ("France", "fr-fr", "fr"), "BE": ("Belgium", "be-fr", "fr"), "ES": ("Spain", "es-es", "es"),
    "MX": ("Mexico", "mx-es", "es"), "AR": ("Argentina", "ar-es", "es"), "IT": ("Italy", "it-it", "it"),
    "NL": ("Netherlands", "nl-nl", "nl"), "PT": ("Portugal", "pt-pt", "pt"), "BR": ("Brazil", "br-pt", "pt"),
    "SE": ("Sweden", "se-sv", "sv"), "NO": ("Norway", "no-no", "no"), "DK": ("Denmark", "dk-da", "da"),
    "FI": ("Finland", "fi-fi", "fi"), "PL": ("Poland", "pl-pl", "pl"), "TR": ("Turkey", "tr-tr", "tr"),
    "JP": ("Japan", "jp-jp", "ja"), "KR": ("South Korea", "kr-kr", "ko"), "CN": ("China", "cn-zh", "zh"),
    "TW": ("Taiwan", "tw-tzh", "zh"), "HK": ("Hong Kong", "hk-tzh", "zh"), "VN": ("Vietnam", "vn-vi", "vi"),
    "SA": ("Saudi Arabia", "xa-ar", "ar"), "AE": ("United Arab Emirates", "xa-ar", "ar"),
}
# Each engine's own name for the region in a search address.
_REGION_PARAM = {"duckduckgo": "kl", "google": "gl", "bing": "cc", "brave": "country"}

# Minutes in the background before a tab sleeps; 0 never.
SLEEP_CHOICES = {0: "Never", 5: "After 5 minutes", 15: "After 15 minutes", 30: "After 30 minutes",
                 60: "After an hour"}
DEFAULT_SLEEP = 15

MAX_TABS = 40          # saved between runs; more open is fine
MAX_SITES = 200        # in the never-sleep list
MAX_SHORTCUTS = 24     # the user's own on the new-tab page

# The new-tab page's + and x go to Buddy, not the web: links to this host
# (engine.TabPage takes them, from that page only). .invalid is never a
# real site, and Chromium only asks about http(s) links, not a scheme of
# Buddy's own.
START_HOST = "new-tab.buddy.invalid"
ADD_SHORTCUT = f"https://{START_HOST}/add"
REMOVE_SHORTCUT = f"https://{START_HOST}/remove/"

DEFAULTS = {
    "sleep_after": DEFAULT_SLEEP,
    "never_sleep": [],
    "search": DEFAULT_ENGINE,
    "region": AUTO_REGION,
    "block_trackers": True,     # before "blocking": True is "strong", False "off"
    "blocking": None,           # BLOCKING
    "allow_ads": [],            # sites nothing's blocked or hidden on
    "youtube_ads": False,       # youtube_ads_script, off unless asked for
    "duck": "resolve",          # pages/web/ducking.py MODES
    "duck_level": 30,
    "duck_restore": None,
    "downloads": "",          # "" = the user's Downloads folder
    "tabs": [],
    "active": 0,
    "suggest": True,
    "sites": {},            # record_site: {site: [visits, typed, last]}
    "shortcuts": [],        # the new-tab page's own: [{"name", "url"}]
    "video_quality": "auto",  # VIDEO_QUALITIES
}

# "readme.txt" is a file's name, not a site: a lone word ending in one of
# these is searched for (a real site under them has more to it).
_FILE_ENDINGS = {"txt", "pdf", "doc", "docx", "mov", "mp4", "mxf", "wav", "mp3", "png", "jpg", "jpeg", "exe",
                 "zip", "drp", "srt", "xml", "csv", "json", "md", "py", "js"}

_HOST = re.compile(r"^(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]{1,62})(?::\d{1,5})?$", re.I)


# ------------------------------------------------------------ addresses --

def region_value(engine, region):
    """(name, value) a search address carries for the region, or None."""
    if region not in REGIONS:
        return None
    engine = engine if engine in SEARCH_ENGINES else DEFAULT_ENGINE
    value = {"duckduckgo": REGIONS[region][1], "brave": region.lower()}.get(engine, region)
    return _REGION_PARAM[engine], value


def search_url(words, engine=DEFAULT_ENGINE, region=AUTO_REGION):
    template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES[DEFAULT_ENGINE])[1]
    url = template.replace("{q}", quote_plus(words))
    extra = region_value(engine, region)
    return f"{url}&{extra[0]}={extra[1]}" if extra else url


def accept_language(language, region):
    """What sites are told the user reads, for a Buddy language code ("en",
    "zh-Hans") and a region: "en-JP,en;q=0.9,ja;q=0.8" - the user's language
    as spoken there first, then the region's own. None for Automatic."""
    if region not in REGIONS:
        return None
    lang = (language or "en").split("-")[0].lower()
    parts = [f"{lang}-{region}", f"{lang};q=0.9"]
    local = REGIONS[region][2]
    if local != lang:
        parts.append(f"{local};q=0.8")
    if "en" not in (lang, local):
        parts.append("en;q=0.7")
    return ",".join(parts)


def address_to_url(text, engine=DEFAULT_ENGINE, region=AUTO_REGION):
    """What the address bar's text goes to: a web address (https:// put on
    a bare "youtube.com"), a file or folder on this PC, or else a search
    for the words (in the region, if one's set). None for nothing at all."""
    text = (text or "").strip()
    if not text:
        return None
    if link_bar.is_path(text.strip('"')):
        return Path(text.strip('"')).as_uri()
    lower = text.lower()
    if lower.startswith(("http://", "https://")) and " " not in text:
        return text
    if lower.startswith(("about:", "file:")):
        return text
    if " " not in text:
        host = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        labels = host.split(":", 1)[0].lower().split(".")
        if _HOST.match(host) and not (len(labels) == 2 and labels[1] in _FILE_ENDINGS and "/" not in text):
            address = link_bar.normalize_address(text)
            if address:
                return "http://" + text if host.lower().startswith("localhost") else address
    return search_url(text, engine, region)


def site_of(url):
    """A page's site: its host without "www." ("" for none)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def clean_sites(raw):
    """The never-sleep list as typed (lines, or a list): each a site
    ("music.youtube.com" from "https://music.youtube.com/watch"), once
    each, in order."""
    lines = raw.splitlines() if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
    out = []
    for line in lines:
        line = str(line).strip().lower()
        if not line:
            continue
        site = site_of(line if "://" in line else "https://" + line)
        if site and site not in out and _HOST.match(site):
            out.append(site)
        if len(out) >= MAX_SITES:
            break
    return out


def on_site(url, sites):
    """True if the page is on one of `sites` or under one ("youtube.com"
    covers music.youtube.com)."""
    site = site_of(url)
    return bool(site) and any(site == s or site.endswith("." + s) for s in sites)


# ------------------------------------------------------------- autofill --

# Sites the address bar can finish before the user has been anywhere - well
# below anything they've visited, so their own habits soon take over.
COMMON_SITES = (
    "google.com", "youtube.com", "drive.google.com", "mail.google.com", "docs.google.com", "music.youtube.com",
    "github.com", "frame.io", "vimeo.com", "dropbox.com", "wetransfer.com", "reddit.com", "wikipedia.org",
    "amazon.com", "facebook.com", "instagram.com", "x.com", "linkedin.com", "twitch.tv", "spotify.com",
    "soundcloud.com", "blackmagicdesign.com", "forum.blackmagicdesign.com", "artlist.io", "epidemicsound.com",
    "musicbed.com", "pexels.com", "unsplash.com", "motionarray.com", "envato.com", "chatgpt.com", "claude.ai",
    "notion.so", "figma.com", "canva.com", "adobe.com", "bing.com", "duckduckgo.com", "outlook.com",
)
MAX_SITES_KEPT = 300
DAY = 86400


def record_site(sites, url, now, typed=False):
    """A visit to `url`'s site, counted in `sites` ({site: [visits, typed,
    last]}, as saved) - only the site, never the page. Keeps the
    MAX_SITES_KEPT most used. Returns `sites`."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return sites
    site = site_of(url)
    if parts.scheme not in ("http", "https") or not site:
        return sites
    visits, typed_count, _last = (sites.get(site) or [0, 0, 0])[:3]
    sites[site] = [visits + 1, typed_count + (1 if typed else 0), now]
    if len(sites) > MAX_SITES_KEPT:
        for drop in sorted(sites, key=lambda k: _score(sites[k], now))[:len(sites) - MAX_SITES_KEPT]:
            del sites[drop]
    return sites


def _score(entry, now):
    try:
        visits, typed, last = (list(entry) + [0, 0, 0])[:3]
        return typed * 3 + visits + (2 if now - float(last) < 7 * DAY else 0)
    except (TypeError, ValueError):
        return 0


def ranked_sites(sites, links=(), tabs=(), now=0.0, common=COMMON_SITES):
    """Every site the address bar may finish, best first: the ones the user
    goes to (typed ones above clicked-through), their link bar links, the
    open tabs, then the common sites."""
    score = {}
    for site, entry in (sites or {}).items():
        if isinstance(site, str) and site:
            score[site] = _score(entry, now)
    for url, bonus in [(u, 4) for u in links] + [(u, 2) for u in tabs]:
        site = site_of(url)
        if site:
            score[site] = score.get(site, 0) + bonus
    for site in common:
        score.setdefault(site, 0.5)
    return sorted(score, key=lambda k: (-score[k], len(k), k))


def autofill(typed, ranked):
    """The address the bar finishes `typed` with - the best site starting
    with it ("goo" -> "google.com"), keeping what was typed as typed - or
    None. Only a site, never a page: nothing once there's a "/" or a space."""
    if not typed or " " in typed or "/" in typed.split("://", 1)[-1]:
        return None
    lower = typed.lower()
    scheme = ""
    for prefix in ("https://", "http://"):
        if lower.startswith(prefix):
            scheme, lower = prefix, lower[len(prefix):]
    if not lower:
        return None
    www = lower.startswith("www.")
    for site in ranked:
        candidate = ("www." + site) if www else site
        if candidate.startswith(lower) and candidate != lower:
            return typed + candidate[len(lower):]
    return None


# ---------------------------------------------------------------- sleep --

def should_sleep(tab, now, minutes, never_sites):
    """True if a tab should go to sleep now. `tab`: {"url", "visible",
    "audible", "awake" (kept awake by the user), "asleep", "seen" (when
    it was last on screen, in seconds)}."""
    if not minutes or tab.get("asleep") or tab.get("visible") or tab.get("audible") or tab.get("awake"):
        return False
    if not tab.get("url") or on_site(tab["url"], never_sites):
        return False
    return now - tab.get("seen", now) >= minutes * 60


def why_awake(tab, minutes, never_sites):
    """Why a background tab won't sleep, for its tooltip - or "" if it will."""
    if tab.get("audible"):
        return "Playing sound - it won't sleep."
    if tab.get("awake"):
        return "Kept awake."
    if tab.get("url") and on_site(tab["url"], never_sites):
        return "This site never sleeps."
    if not minutes:
        return "Tabs never sleep (Settings)."
    return ""


# -------------------------------------------------------------- blocking --

# Sites whose own domain is two labels under a country's: "bbc.co.uk".
_SECOND_LEVEL = {"co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "ne.jp", "or.jp", "com.au", "net.au", "org.au",
                 "co.nz", "co.kr", "com.br", "com.cn", "com.mx", "co.in", "com.tw", "com.hk", "com.sg", "co.za"}


def registrable(host):
    """"news.bbc.co.uk" -> "bbc.co.uk", "a.b.example.com" -> "example.com"."""
    parts = (host or "").lower().rstrip(".").split(".")
    if len(parts) > 2 and ".".join(parts[-2:]) in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_tracker(request_host, page_host, blocked=BLOCKED):
    """True for a request to an ad or tracking domain from another site's
    page. A site's own requests are never blocked (logging in to Google
    needs google.com's own trackers)."""
    host = (request_host or "").lower().rstrip(".")
    if not host or registrable(host) == registrable(page_host or ""):
        return False
    parts = host.split(".")
    return any(".".join(parts[i:]) in blocked for i in range(len(parts) - 1))


# ----------------------------------------------------------- video quality --

# YouTube's (and YouTube Music's) quality, set through its own player: the
# chosen one, or the nearest below it a video has. Other sites choose their
# own - there's no common way to ask. Stored key -> what Settings shows.
VIDEO_QUALITIES = {"auto": "Automatic (YouTube decides)", "best": "Best available", "2160": "2160p (4K)",
                   "1440": "1440p", "1080": "1080p", "720": "720p", "480": "480p", "360": "360p",
                   "144": "144p (least data)"}
DEFAULT_VIDEO_QUALITY = "auto"
# YouTube's names, lowest first.
_YOUTUBE_QUALITY = {"144": "tiny", "240": "small", "360": "medium", "480": "large", "720": "hd720",
                    "1080": "hd1080", "1440": "hd1440", "2160": "hd2160"}
YOUTUBE_HOSTS = ("youtube.com", "youtube-nocookie.com")


def video_quality(value):
    return value if value in VIDEO_QUALITIES else DEFAULT_VIDEO_QUALITY


def youtube_quality_script(choice):
    """For YouTube's pages (and its players embedded in other sites): each
    video, as it starts, put at `choice` - once, so picking another quality
    in YouTube's own menu still works. Run again with a new choice, it
    takes over from the one before (one timer per page)."""
    want = {"auto": "auto", "best": "best"}.get(choice) or _YOUTUBE_QUALITY.get(choice, "auto")
    order = ["tiny", "small", "medium", "large", "hd720", "hd1080", "hd1440", "hd2160", "highres"]
    return (
        "(() => {\n"
        f"  if (!{list(YOUTUBE_HOSTS)!r}.some(h => location.hostname === h || location.hostname.endsWith('.' + h))) return;\n"
        f"  window.__buddyQuality = {want!r};\n"
        "  if (window.__buddyQuality !== 'auto') window.__buddyQualitySet = '';\n"
        "  if (window.__buddyQualityTimer) return;\n"
        f"  const ORDER = {order!r};\n"
        "  window.__buddyQualityTimer = setInterval(() => {\n"
        "    const want = window.__buddyQuality, p = document.getElementById('movie_player');\n"
        "    if (!p || !p.getAvailableQualityLevels || !p.setPlaybackQualityRange) return;\n"
        "    if (want === 'auto') {                     // back to YouTube's own choice, once\n"
        "      if (window.__buddyQualitySet) { p.setPlaybackQualityRange('auto', 'auto'); window.__buddyQualitySet = ''; }\n"
        "      return;\n"
        "    }\n"
        "    const levels = p.getAvailableQualityLevels().filter(q => q !== 'auto');   // best first\n"
        "    const video = p.getVideoData ? (p.getVideoData() || {}).video_id : '';\n"
        "    if (!levels.length || !video) return;\n"
        "    const q = want === 'best' ? levels[0]\n"
        "      : levels.find(l => ORDER.indexOf(l) <= ORDER.indexOf(want)) || levels[levels.length - 1];\n"
        "    const key = video + ' ' + want;\n"
        "    if (window.__buddyQualitySet === key) return;\n"
        "    p.setPlaybackQualityRange(q, q);\n"
        "    window.__buddyQualitySet = key;\n"
        "  }, 1000);\n"
        "})();"
    )


# ------------------------------------------------------- YouTube's video ads --

# YouTube's video ads come from YouTube itself, so no filter list stops
# them: they're entries in the data YouTube's player is handed for each
# video. This takes those entries out before the player reads them - as
# uBlock Origin's json-prune does - wherever the data arrives: in the page
# (ytInitialPlayerResponse), and in what YouTube's own script parses or
# fetches. Buddy's own code, in YouTube's pages only; it only deletes.
# YouTube changes how it sends ads now and then, and this follows when
# it's updated.
AD_KEYS = ("adPlacements", "playerAds", "adSlots", "adBreakHeartbeatParams")


def youtube_ads_script():
    keys = list(AD_KEYS)
    return (
        "(() => {\n"
        f"  if (!{list(YOUTUBE_HOSTS)!r}.some(h => location.hostname === h || location.hostname.endsWith('.' + h))) return;\n"
        "  if (window.__buddyNoAds) return;\n"
        "  window.__buddyNoAds = true;\n"
        f"  const KEYS = {keys!r};\n"
        "  const prune = data => {\n"
        "    if (!data || typeof data !== 'object') return data;\n"
        "    if (Array.isArray(data)) { data.forEach(prune); return data; }\n"
        "    for (const key of KEYS) if (key in data) delete data[key];\n"
        "    if (data.playerResponse) prune(data.playerResponse);\n"
        "    return data;\n"
        "  };\n"
        "  const parse = JSON.parse;\n"
        "  JSON.parse = function (...args) { const out = parse.apply(this, args); try { prune(out); } catch (e) {} return out; };\n"
        "  const json = Response.prototype.json;\n"
        "  Response.prototype.json = function (...args) {\n"
        "    return json.apply(this, args).then(out => { try { prune(out); } catch (e) {} return out; });\n"
        "  };\n"
        "  // The first video's data comes in the page itself, as a variable.\n"
        "  let initial;\n"
        "  try {\n"
        "    Object.defineProperty(window, 'ytInitialPlayerResponse', {\n"
        "      configurable: true, get: () => initial, set: value => { initial = prune(value); }});\n"
        "  } catch (e) {}\n"
        "})();"
    )


# --------------------------------------------------------------- sign-in --

# Google's sign-in page turns away browsers built into other apps ("This
# browser or app may not be secure") - which it takes this one for: it says
# Chromium, not Google Chrome, and a version a year behind. Firefox it lets
# in, so on that page alone the browser says it's Firefox (as qutebrowser,
# on the same engine, does). Every other site still hears Chrome.
SIGN_IN_HOSTS = frozenset({"accounts.google.com"})

# Firefox 140 came out on 24 June 2025 and a new one about every four
# weeks since; counted at 30 days a release, so it's never one that isn't
# out yet.
_FIREFOX_BASE = (140, datetime.date(2025, 6, 24))


def signs_in_as_firefox(host):
    return (host or "").lower().rstrip(".") in SIGN_IN_HOSTS


def firefox_version(today=None):
    version, released = _FIREFOX_BASE
    days = ((today or datetime.date.today()) - released).days
    return version + max(0, days // 30)


def firefox_user_agent(today=None):
    v = firefox_version(today)
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}.0) Gecko/20100101 Firefox/{v}.0"


def firefox_script(agent):
    """For the page's own script, on those hosts only: what Firefox says
    about itself (and no navigator.userAgentData, which it hasn't got)."""
    hosts = sorted(SIGN_IN_HOSTS)
    return (
        "(() => {\n"
        f"  if (!{hosts!r}.includes(location.hostname)) return;\n"
        "  const nav = Object.getPrototypeOf(navigator);\n"
        "  const say = (key, value) => Object.defineProperty(nav, key, {get: () => value, configurable: true});\n"
        f"  say('userAgent', {agent!r});\n"
        "  say('appVersion', '5.0 (Windows)');\n"
        "  say('vendor', '');\n"
        "  say('userAgentData', undefined);\n"
        "})();"
    )


# --------------------------------------------------------------- blocking --

# How hard the Web tab blocks: the built-in tracker list (is_tracker), or
# that and the filter lists (pages/web/filters.py, filter_lists.py).
BLOCKING = {"off": "Off", "trackers": "Known trackers only",
            "strong": "Ads and trackers (EasyList and uBlock filter lists)"}
DEFAULT_BLOCKING = "strong"


def blocking(settings):
    mode = settings.get("blocking")
    if mode in BLOCKING:
        return mode
    return DEFAULT_BLOCKING if settings.get("block_trackers", True) else "off"


def allowed_site(site, allowed):
    """True if `site` (a host) is one of the sites ads are allowed on, or under one."""
    site = (site or "").lower()
    return bool(site) and any(site == a or site.endswith("." + a) for a in allowed)


# --------------------------------------------------------------- session --

def clean_tabs(raw):
    """Saved tabs -> [{"url", "title", "awake"}]: web addresses (and the
    new-tab page, url ""), at most MAX_TABS."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if not isinstance(url, str) or (url and not url.lower().startswith(("http://", "https://", "file:"))):
            continue
        title = str(item.get("title") or "")[:200]
        out.append({"url": url, "title": title, "awake": bool(item.get("awake"))})
        if len(out) >= MAX_TABS:
            break
    return out


def clean_shortcuts(raw):
    """The new-tab page's own shortcuts -> [{"name", "url"}]: web
    addresses only, each once, at most MAX_SHORTCUTS."""
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        url, name = item.get("url"), item.get("name")
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append({"name": (str(name or "").strip() or site_of(url) or url)[:80], "url": url})
        if len(out) >= MAX_SHORTCUTS:
            break
    return out


def start_page_action(action):
    """A link on the new-tab page -> ("add", None), ("remove", index), or
    None for anything else."""
    if action == ADD_SHORTCUT:
        return "add", None
    if isinstance(action, str) and action.startswith(REMOVE_SHORTCUT):
        tail = action[len(REMOVE_SHORTCUT):]
        if tail.isdigit():
            return "remove", int(tail)
    return None


def default_downloads():
    return os.path.join(os.path.expanduser("~"), "Downloads")


def unique_name(folder, name):
    """`name` in `folder`, or "name (2).ext" and on if it's taken."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "download").strip(" .") or "download"
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{stem} ({n}){ext}"
        n += 1
    return candidate


# --------------------------------------------------------------- new tab --

def start_page(colors, links, engine=DEFAULT_ENGINE, translate=lambda s: s, region=AUTO_REGION, shortcuts=(),
               private=False):
    """The new-tab page, as HTML: a search box, the link bar's web links,
    the user's own shortcuts (each with an x) and a + to add one. No script
    - the + and x are links Buddy takes (START_HOST). `colors`: {"bg",
    "card", "text", "dim", "accent", "border", "font"}. A private tab's
    says what it forgets."""
    name, template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES[DEFAULT_ENGINE])
    action, _, query = template.partition("?")
    field = query.split("=", 1)[0]
    extra = region_value(engine, region)
    hidden = f'<input type="hidden" name="{html.escape(extra[0])}" value="{html.escape(extra[1])}">' if extra else ""
    esc = html.escape

    def tile(link, remove=None):
        x = (f'<a class="x" href="{REMOVE_SHORTCUT}{remove}" title="{esc(translate("Remove"))}" '
             f'aria-label="{esc(translate("Remove"))}">&times;</a>') if remove is not None else ""
        return (f'<div class="cell"><a class="tile" href="{esc(link["url"])}"><b>{esc(link["name"][:1].upper())}</b>'
                f'<span>{esc(link["name"])}</span></a>{x}</div>')

    tiles = "".join(tile(link) for link in links if not link_bar.is_path(link["url"]))
    own = clean_shortcuts(list(shortcuts))
    tiles += "".join(tile(link, i) for i, link in enumerate(own))
    if len(own) < MAX_SHORTCUTS:
        tiles += (f'<div class="cell"><a class="tile add" href="{ADD_SHORTCUT}" title="{esc(translate("Add a shortcut"))}">'
                  f'<b>+</b><span>{esc(translate("Add a shortcut"))}</span></a></div>')
    note = (f'<p class="private"><b>{esc(translate("Private tab"))}</b> - '
            f'{esc(translate("Buddy forgets the sites, cookies and sign-ins of private tabs once you close the last one. Downloads stay."))}</p>'
            if private else "")
    # CSS isn't HTML: entities aren't read in a <style>, so the values are
    # kept to what a colour or a font list can hold instead.
    c = {k: re.sub(r"[^#\w\s,'().%-]", "", str(v)) for k, v in colors.items()}
    title = translate("Private tab") if private else translate("New tab")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{esc(title)}</title>
<style>
html, body {{ margin: 0; height: 100%; background: {c['bg']}; color: {c['text']}; font-family: {c['font']}; }}
main {{ max-width: 640px; margin: 0 auto; padding: 18vh 24px 40px; }}
form {{ display: flex; gap: 8px; }}
input {{ flex: 1; font: inherit; font-size: 16px; padding: 10px 14px; border-radius: 10px; outline: none;
        background: {c['card']}; color: {c['text']}; border: 1px solid {c['border']}; }}
input:focus {{ border-color: {c['accent']}; }}
button {{ font: inherit; padding: 0 16px; border-radius: 10px; border: 1px solid {c['border']};
         background: {c['card']}; color: {c['text']}; cursor: pointer; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; margin-top: 28px; }}
.cell {{ position: relative; display: flex; }}
.tile {{ flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 14px 8px;
        border-radius: 10px; background: {c['card']}; color: {c['text']}; text-decoration: none; font-size: 13px; }}
.tile:hover {{ outline: 1px solid {c['accent']}; }}
.tile b {{ width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center;
          background: {c['accent']}; color: {c['bg']}; font-size: 16px; }}
.tile span {{ max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.tile.add {{ background: transparent; border: 1px dashed {c['border']}; color: {c['dim']}; }}
.tile.add b {{ background: transparent; color: {c['text']}; border: 1px solid {c['border']}; font-size: 20px; font-weight: 400; }}
.x {{ position: absolute; top: 4px; right: 4px; width: 20px; height: 20px; border-radius: 50%; display: none;
      place-items: center; color: {c['dim']}; text-decoration: none; font-size: 15px; line-height: 1; }}
.cell:hover .x, .x:focus {{ display: grid; }}
.x:hover {{ color: {c['text']}; background: {c['bg']}; }}
p {{ color: {c['dim']}; font-size: 12px; margin-top: 28px; text-align: center; }}
.private {{ margin: 0 0 18px; font-size: 13px; line-height: 1.5; }}
.private b {{ color: {c['accent']}; }}
</style></head><body><main>
{note}<form action="{esc(action)}" method="get">
<input name="{esc(field)}" placeholder="{esc(translate("Search with {name}").replace("{name}", name))}" autofocus>
<button type="submit">{esc(translate("Search"))}</button>{hidden}</form>
<div class="tiles">{tiles}</div>
</main></body></html>"""
