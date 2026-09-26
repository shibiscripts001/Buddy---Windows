"""Buddy Network's safety checks - no Qt, so they're unit-tested directly.

- find_links(): where the links are in a message, so the page can make them
  clickable (through a warning, never directly) and flag the message.
- describe_link(): what the link warning dialog shows about one link.
- outgoing_warnings(): things in a message about to be sent that the user
  probably doesn't want strangers to have.
- check_server_url(): only encrypted wss:// connections, except to this PC.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from dataclasses import dataclass, field

# Links that get made clickable: with a scheme, or starting "www.".
_LINK_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'`]+")
# Anything that just looks like an address ("example.com/login") - not made
# clickable, but still gets the "this message contains a link" warning.
_BARE_DOMAIN_RE = re.compile(
    r"(?i)(?<![\w@.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|io|gg|co|me|app|dev|xyz|info|biz|ru|cn|tk|ml|ga|cf|gq|top|site|online|"
    r"link|live|shop|store|zip|mov|ly|to|be|tv|us|uk|de|fr|nl|ca|au)\b(?:/\S*)?")
_TRAILING = ".,;:!?)]}'\""

_SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "ow.ly", "cutt.ly", "rebrand.ly",
               "tiny.cc", "buff.ly", "shorturl.at", "rb.gy", "t.ly", "s.id", "lnkd.in"}
_RISKY_FILES = (".exe", ".msi", ".bat", ".cmd", ".scr", ".ps1", ".vbs", ".js", ".jar",
                ".zip", ".rar", ".7z", ".iso", ".dmg", ".apk", ".lnk", ".hta", ".reg", ".drfx")


def find_links(text: str) -> list[tuple[int, int, str]]:
    """(start, end, url) for each clickable link, trailing punctuation left
    out (a link at the end of a sentence doesn't swallow its full stop)."""
    out = []
    for m in _LINK_RE.finditer(text):
        url = m.group(0)
        while url and url[-1] in _TRAILING:
            # Keep a closing bracket that closes one inside the link:
            # https://en.wikipedia.org/wiki/Foo_(bar)
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            url = url[:-1]
        if len(url) > len("www."):
            out.append((m.start(), m.start() + len(url), url))
    return out


def contains_link(text: str) -> bool:
    return bool(find_links(text) or _BARE_DOMAIN_RE.search(text))


@dataclass
class LinkInfo:
    url: str                  # exactly as written in the message
    open_url: str             # what Open actually opens (a scheme added to "www.")
    host: str                 # the real destination, shown highlighted
    warnings: list[str] = field(default_factory=list)


def describe_link(url: str) -> LinkInfo:
    open_url = url if re.match(r"(?i)https?://", url) else "https://" + url
    parts = urllib.parse.urlsplit(open_url)
    host = (parts.hostname or "").lower()
    warnings = []
    if "@" in parts.netloc:
        warnings.append("This link has an '@' in its address. Everything before the '@' is "
                        f"ignored – it really goes to {host or 'an unknown site'}.")
    if host.startswith("xn--") or ".xn--" in host or any(ord(c) > 127 for c in host):
        warnings.append("The address uses international characters that can imitate a "
                        "well-known site's name letter for letter.")
    try:
        ipaddress.ip_address(host.strip("[]"))
        warnings.append("The address is a bare IP number instead of a site name.")
    except ValueError:
        pass
    if host in _SHORTENERS:
        warnings.append("This is a link shortener – you can't see where it really leads.")
    if parts.scheme == "http":
        warnings.append("The site isn't encrypted (http, not https).")
    if parts.path.lower().endswith(_RISKY_FILES):
        warnings.append("It looks like a direct download of a program or archive. Only run "
                        "files from people and sites you trust.")
    return LinkInfo(url, open_url, host, warnings)


# ---------------------------------------------------------- before sending

_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<![\w:])\+?\d[\d ().-]{7,}\d(?![\w:])")
_KEY_RES = [
    re.compile(p) for p in (
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}",   # OpenAI / Anthropic
        r"\bAIza[0-9A-Za-z_-]{30,}",                  # Google / Gemini
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",              # GitHub
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",            # Slack
        r"\bAKIA[0-9A-Z]{16}\b",                      # AWS
    )
]
# A long run of mixed letters and digits: what most keys and tokens look like.
_TOKEN_RE = re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}(?![\w/.-])")
_PASSWORD_RE = re.compile(r"(?i)\b(?:password|passwd|pwd|passcode|pin)\b\s*(?:is\b|[:=])")


@dataclass
class SendCheck:
    blocked: str = ""                              # a reason it must not be sent at all
    warnings: list[str] = field(default_factory=list)


def outgoing_warnings(text: str, own_secrets=()) -> SendCheck:
    """own_secrets: the user's own API keys (Ask Buddy's settings), compared
    on this PC only - a message containing one is never sent."""
    check = SendCheck()
    for secret in own_secrets:
        if secret and len(secret) >= 8 and secret in text:
            check.blocked = ("This message contains one of your own API keys (from Ask Buddy's "
                             "settings). Anyone who has it can use your account and run up "
                             "your bill – Buddy won't send it.")
            return check
    if _EMAIL_RE.search(text):
        check.warnings.append("an email address")
    phones = [m.group(0) for m in _PHONE_RE.finditer(text)]
    if any(sum(c.isdigit() for c in p) >= 9 for p in phones):
        check.warnings.append("what looks like a phone number")
    if any(r.search(text) for r in _KEY_RES) or _TOKEN_RE.search(text):
        check.warnings.append("what looks like an API key, token or licence key")
    if _PASSWORD_RE.search(text):
        check.warnings.append("what looks like a password")
    return check


# ------------------------------------------------------------- server URL

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_server_url(url: str) -> str:
    """"" if Buddy may connect to it, else why not."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        return "The server address must start with wss:// (for example wss://chat.example.com)."
    if parts.scheme == "ws" and parts.hostname not in LOCAL_HOSTS:
        return ("Only encrypted wss:// addresses are allowed, so nobody between you and the "
                "server can read your messages (ws:// is for a test server on this PC).")
    return ""
