#!/usr/bin/env python3
"""
The link bar's links: what the user added, each a name of their own and an
address - a web page, or a file or folder on this PC. Drawn by
core/link_bar_web.py; along the bottom of the window, or along the top
under the desktop layout, whose bottom is its taskbar. No Qt, so it's
unit-tested.

Saved in the shared settings as a list of {"name", "url"} under LINKS_KEY,
with "icon" when the user chose one of ICONS (none: the bar guesses one
from the address); SHOW_KEY turns the bar on (off until the user asks for
it).
"""

import os
import re
from urllib.parse import unquote, urlsplit

SHOW_KEY = "link_bar"
LINKS_KEY = "links"

# How the bar looks (Settings > Window > Link bar), each {stored: label} with
# the default first; core/web_theme.py _link_bar draws them.
SHADES = {"auto": "Automatic", "lighter": "Lighter", "darker": "Darker", "match": "Same as the header"}
TINTS = {"off": "Off", "subtle": "Subtle", "strong": "Strong"}
ICON_COLOURS = {"theme": "Theme", "accent": "Accent", "text": "Same as the text"}
# Where a web link opens: the user's own browser, or Buddy's Web tab.
OPEN_KEY = "link_bar_open"
OPEN_CHOICES = {"browser": "Your default browser", "buddy": "Buddy's Web tab"}

STYLE_KEYS = {"link_bar_shade": ("shade", SHADES), "link_bar_tint": ("tint", TINTS),
              "link_bar_icons": ("icons", ICON_COLOURS)}

MAX_LINKS = 60
MAX_NAME = 60
MAX_ADDRESS = 2048

# The icons a link can have - app/web/buddy.js draws them - in the order the
# edit window offers them.
ICONS = ("globe", "folder", "file", "film", "play", "music", "mic", "image", "camera", "palette",
         "cloud", "mail", "chat", "calendar", "book", "code", "cart", "users", "home", "star",
         "heart", "bookmark", "map", "tool")

# The icon a link gets when it has none of its own: by site (the host, or a
# site it's under) ...
_SITE_ICONS = {
    "youtube.com": "play", "youtu.be": "play", "vimeo.com": "play", "twitch.tv": "play",
    "frame.io": "film", "blackmagicdesign.com": "film", "imdb.com": "film",
    "drive.google.com": "cloud", "dropbox.com": "cloud", "onedrive.live.com": "cloud", "box.com": "cloud",
    "icloud.com": "cloud", "wetransfer.com": "cloud",
    "mail.google.com": "mail", "outlook.live.com": "mail", "outlook.office.com": "mail",
    "calendar.google.com": "calendar", "docs.google.com": "file", "notion.so": "book",
    "github.com": "code", "gitlab.com": "code", "stackoverflow.com": "code",
    "discord.com": "chat", "slack.com": "chat", "reddit.com": "chat", "chatgpt.com": "chat",
    "claude.ai": "chat",
    "spotify.com": "music", "soundcloud.com": "music", "artlist.io": "music", "epidemicsound.com": "music",
    "unsplash.com": "image", "pexels.com": "image", "pinterest.com": "image", "behance.net": "palette",
    "dribbble.com": "palette", "coolors.co": "palette", "fonts.google.com": "palette",
    "amazon.com": "cart", "bhphotovideo.com": "cart", "ebay.com": "cart",
    "maps.google.com": "map", "wikipedia.org": "book",
}
# ... by the first part of the host ("mail.example.com") ...
_SUBDOMAIN_ICONS = {"mail": "mail", "calendar": "calendar", "drive": "cloud", "docs": "file",
                    "maps": "map", "music": "music", "photos": "image", "shop": "cart", "store": "cart"}
# ... and a file by its kind; a path that isn't one of these is a folder.
_FILE_ICONS = {
    "film": (".mp4", ".mov", ".mxf", ".avi", ".mkv", ".braw", ".r3d", ".m4v", ".webm"),
    "music": (".wav", ".mp3", ".aac", ".flac", ".m4a", ".aif", ".aiff", ".ogg"),
    "image": (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dpx", ".psd", ".gif", ".webp", ".bmp"),
    "file": (".pdf", ".doc", ".docx", ".txt", ".rtf", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".drp",
             ".drt", ".edl", ".xml", ".fcpxml", ".srt", ".zip", ".cube", ".md"),
}

# C:\..., C:/..., \\server\share - a path, not a web address.
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\])")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def is_path(address):
    return bool(_WINDOWS_PATH.match(address) or (os.sep == "/" and address.startswith("/")))


def normalize_address(text):
    """What the user typed -> the address to keep: "https://..." for a web
    page ("youtube.com" gets https:// put on), the path itself for a file
    or folder (a file:// URL becomes its path). None if it's neither - only
    web pages and paths open from the bar, never another app's own scheme."""
    if not isinstance(text, str):
        return None
    address = text.strip().strip('"').strip()
    if not address or len(address) > MAX_ADDRESS or any(ch in address for ch in "\r\n\t"):
        return None
    if is_path(address):
        return address
    if address.lower().startswith("file:"):
        path = unquote(urlsplit(address).path)
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        if os.sep == "\\":
            path = path.replace("/", "\\")
        return path if is_path(path) else None
    if not _SCHEME.match(address):
        if ":" in address.split("/", 1)[0] and not re.match(r"^[^:/]+:\d+(?:/|$)", address):
            return None        # mailto:, ms-settings: and the like
        address = "https://" + address
    try:
        parts = urlsplit(address)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or " " in address:
        return None
    return address


def default_name(address):
    """A name for a link the user didn't name: the site ("youtube.com"),
    or the file or folder's own name."""
    if is_path(address):
        tail = re.split(r"[\\/]", address.rstrip("\\/"))[-1]
        return tail or address
    host = (urlsplit(address).hostname or address).lower()
    return host[4:] if host.startswith("www.") else host


def auto_icon(address):
    """The icon a link shows when the user hasn't picked one: the site's or
    the file's kind, else a globe for a web page and a folder for a path."""
    if is_path(address):
        lower = address.lower().rstrip("\\/")
        return next((icon for icon, ends in _FILE_ICONS.items() if lower.endswith(ends)), "folder")
    host = (urlsplit(address).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    for at in range(len(parts) - 1):
        icon = _SITE_ICONS.get(".".join(parts[at:]))
        if icon:
            return icon
    return _SUBDOMAIN_ICONS.get(parts[0], "globe") if len(parts) > 2 else "globe"


def clean_icon(icon):
    """A chosen icon, or "" (none chosen: the bar guesses one)."""
    return icon if isinstance(icon, str) and icon in ICONS else ""


def clean_name(name, address):
    name = " ".join(str(name or "").split())[:MAX_NAME]
    return name or default_name(address)[:MAX_NAME]


def _link(name, address, icon):
    link = {"name": clean_name(name, address), "url": address}
    if clean_icon(icon):
        link["icon"] = icon
    return link


def load_links(raw):
    """The saved list -> [{"name", "url"[, "icon"]}], dropping anything
    that isn't one (and an icon Buddy doesn't have)."""
    links = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        address = normalize_address(item.get("url"))
        if address is None:
            continue
        links.append(_link(item.get("name"), address, item.get("icon")))
        if len(links) >= MAX_LINKS:
            break
    return links


def make_link(name, address_text, icon=""):
    """(link, None), or (None, why) if the address won't do. `icon`: one of
    ICONS, or "" to let the bar guess (then the link has no "icon")."""
    address = normalize_address(address_text)
    if address is None:
        if not str(address_text or "").strip():
            return None, "Type the address of a web page, or a file or folder on this PC."
        return None, "That isn't a web address or a file or folder path."
    return _link(name, address, icon), None


def opens_in(settings):
    """"browser" or "buddy" - where the bar's web links open."""
    value = settings.get(OPEN_KEY)
    return value if value in OPEN_CHOICES else "browser"


def style(settings):
    """{"shade", "tint", "icons"} from the shared settings - each the saved
    choice, or its default when there's none (or it isn't one)."""
    out = {}
    for key, (name, choices) in STYLE_KEYS.items():
        value = settings.get(key)
        out[name] = value if value in choices else next(iter(choices))
    return out


def move(links, index, to):
    """The link at `index` moved to `to` (both in range), in place.
    True if anything moved."""
    if not (0 <= index < len(links) and 0 <= to < len(links)) or index == to:
        return False
    links.insert(to, links.pop(index))
    return True


def placement(layout):
    """Where the bar goes: "top" under the desktop layout (its taskbar has
    the bottom), "bottom" under every other theme."""
    return "top" if layout == "desktop" else "bottom"


def view_items(links):
    """What the bar draws: each link's name, address, kind ("web" or
    "path") and icon - its own, or the guess."""
    return [{"name": link["name"], "url": link["url"], "kind": "path" if is_path(link["url"]) else "web",
             "icon": clean_icon(link.get("icon")) or auto_icon(link["url"])}
            for link in links]
