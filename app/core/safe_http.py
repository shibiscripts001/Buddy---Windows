#!/usr/bin/env python3
"""
urlopen for requests that carry an API key. Python's own follows a redirect
to wherever it points and sends the request's headers along - the key
included - so a provider, a relay or an open redirect on one could hand the
key to another host, or to plain http. This follows a redirect only to the
same host (and port) with the connection no less secure than it was
(https stays https); anything else stops with the redirect's HTTPError, and
the key goes nowhere.

No Qt, so it's unit-tested.
"""

import urllib.error
import urllib.parse
import urllib.request


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


def may_follow(old_url, new_url):
    """True if a redirect from old_url to new_url keeps to the same host
    and port without dropping from https to http."""
    old, new = _origin(old_url), _origin(new_url)
    if new[0] not in ("http", "https") or old[1] != new[1] or not new[1]:
        return False
    if old[0] == "https" and new[0] != "https":
        return False
    # Same port; or the default port of the scheme it moved to (http -> https).
    return old[2] == new[2] or (old[0] != new[0] and new[2] == {"http": 80, "https": 443}[new[0]])


class _SameHostOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if not may_follow(req.full_url, target):
            return None                       # raised as the redirect's own HTTPError
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHostOnly)


def urlopen(request, timeout=None):
    """Like urllib.request.urlopen, minus redirects to anywhere else."""
    return _opener.open(request, timeout=timeout)
