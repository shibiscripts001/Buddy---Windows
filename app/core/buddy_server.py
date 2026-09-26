"""Where the Buddy Network server is - shared by the Buddy Network page and
the shell's announcement checker (core/announcements.py)."""

from __future__ import annotations

import urllib.parse

# The default server (server/README.md covers running your own). For a
# local test server (python -m server --dev), set ws://localhost:8765 in Buddy Network's
# settings instead.
DEFAULT_SERVER_URL = "wss://chat.trevorsiebe.com"
# What the default was before the server went live - a copy of it saved in
# settings is treated as "not chosen" (see the Buddy Network page).
OLD_TEST_DEFAULT = "ws://localhost:8765"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def http_url(server_url: str, path: str) -> str:
    """The plain web address of `path` on the same server: wss:// becomes
    https://. "" unless the address is safe to use (see check_server_url)."""
    parts = urllib.parse.urlsplit((server_url or "").strip())
    if parts.scheme == "wss" and parts.hostname:
        scheme = "https"
    elif parts.scheme == "ws" and parts.hostname in LOCAL_HOSTS:
        scheme = "http"
    else:
        return ""
    return urllib.parse.urlunsplit((scheme, parts.netloc, path, "", ""))
