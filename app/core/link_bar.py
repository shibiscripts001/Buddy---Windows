#!/usr/bin/env python3
"""
The link bar's links: what the user added, each a name of their own and an
address - a web page, or a file or folder on this PC. Drawn by
core/link_bar_web.py; along the bottom of the window, or along the top
under the desktop layout, whose bottom is its taskbar. No Qt, so it's
unit-tested.

Saved in the shared settings as a list of {"name", "url"} under LINKS_KEY;
SHOW_KEY turns the bar on (off until the user asks for it).
"""

import os
import re
from urllib.parse import unquote, urlsplit

SHOW_KEY = "link_bar"
LINKS_KEY = "links"

MAX_LINKS = 60
MAX_NAME = 60
MAX_ADDRESS = 2048

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


def clean_name(name, address):
    name = " ".join(str(name or "").split())[:MAX_NAME]
    return name or default_name(address)[:MAX_NAME]


def load_links(raw):
    """The saved list -> [{"name", "url"}], dropping anything that isn't one."""
    links = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        address = normalize_address(item.get("url"))
        if address is None:
            continue
        links.append({"name": clean_name(item.get("name"), address), "url": address})
        if len(links) >= MAX_LINKS:
            break
    return links


def make_link(name, address_text):
    """(link, None), or (None, why) if the address won't do."""
    address = normalize_address(address_text)
    if address is None:
        if not str(address_text or "").strip():
            return None, "Type the address of a web page, or a file or folder on this PC."
        return None, "That isn't a web address or a file or folder path."
    return {"name": clean_name(name, address), "url": address}, None


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
    """What the bar draws: each link's name, address and kind ("web" or "path")."""
    return [{"name": link["name"], "url": link["url"], "kind": "path" if is_path(link["url"]) else "web"}
            for link in links]
