#!/usr/bin/env python3
"""
Looks at a video address before Buddy's player is let loose on it. A page
decides the address, so a hostile one could aim Buddy's player (FFmpeg) at
things the page can't reach itself: a playlist naming local files or
machines on the user's network, or a redirect to one. So first, from Python,
with no cookies:

  - the host has to be a public one (every address it resolves to) - unless
    the page is itself on this PC or the user's network;
  - redirects are followed here, each hop checked the same way, and the
    player is handed the address they end at, so it meets none;
  - the first bytes have to be a media file (MP4, WebM, ...): playlists
    and anything else are refused.

The player's own request can still differ from this one (a server can answer
twice differently), which at worst makes a plain GET to a host that was
checked; nothing it returns is shown to the page. No Qt, so it's
unit-tested.
"""

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

SNIFF_BYTES = 4096
TIMEOUT_S = 8
MAX_REDIRECTS = 4

_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot")


def host_is_private(host, resolve=socket.getaddrinfo):
    """True if `host` is, or resolves to, an address that isn't a public
    one (loopback, a private network, link-local, ...) - or can't be told."""
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan", ".home")):
        return True
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in resolve(host, None)}
        except (OSError, ValueError):
            return True
    return not addresses or any(not a.is_global or a.is_multicast for a in addresses)


def is_media_start(head):
    """True if these first bytes begin a media file (not a playlist, not a
    page). MP4/MOV/3GP, WebM/MKV, Ogg, FLV, MP3, AAC, WAV/AVI, MPEG-TS/PS."""
    if len(head) < 12:
        return False
    if head[4:8] in _MP4_BOXES:
        return True
    if head[:4] == b"\x1a\x45\xdf\xa3" or head[:4] == b"OggS" or head[:3] == b"FLV":
        return True
    if head[:3] == b"ID3" or (head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return True
    if head[:4] == b"RIFF" and head[8:12] in (b"WAVE", b"AVI "):
        return True
    if head[:4] == b"\x00\x00\x01\xba" or (head[0] == 0x47 and len(head) > 188 and head[188] == 0x47):
        return True
    return False


class VideoRefused(Exception):
    """Why the address won't be played - worded for the user."""


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_private, resolve):
        self.allow_private, self.resolve = allow_private, resolve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_address(urllib.parse.urljoin(req.full_url, newurl), self.allow_private, self.resolve)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_address(url, allow_private, resolve):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise VideoRefused("That video isn't on the web.")
    if not allow_private and host_is_private(parts.hostname, resolve):
        raise VideoRefused("That video is on this computer or a private network, which a web page can't ask for.")


def _fetch(request, allow_private, resolve):
    """(the address it ended at, its first bytes)."""
    opener = urllib.request.build_opener(_CheckedRedirects(allow_private, resolve))
    with opener.open(request, timeout=TIMEOUT_S) as response:
        return response.geturl(), response.read(SNIFF_BYTES)


def probe(url, allow_private=False, resolve=socket.getaddrinfo, fetch=None):
    """The address to play for `url` (after redirects), or raises
    VideoRefused. `fetch(request, allow_private, resolve)` is for tests."""
    _check_address(url, allow_private, resolve)
    request = urllib.request.Request(url, headers={"Range": f"bytes=0-{SNIFF_BYTES - 1}",
                                                   "User-Agent": "Mozilla/5.0", "Accept": "video/*,audio/*,*/*;q=0.5"})
    try:
        final, head = (fetch or _fetch)(request, allow_private, resolve)
    except VideoRefused:
        raise
    except urllib.error.HTTPError as exc:
        raise VideoRefused(f"The site answered {exc.code}.") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VideoRefused("Buddy couldn't reach that video.") from exc
    _check_address(final, allow_private, resolve)
    if not is_media_start(head):
        raise VideoRefused("That address isn't a plain video file (a playlist or a stream, say), which "
                           "Buddy's player doesn't take.")
    return final
