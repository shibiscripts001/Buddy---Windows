"""The WebSocket side of the Buddy Network server: accepts connections and
feeds each text frame to NetworkCore (core.py), which does all the work.

In production it listens on 127.0.0.1 behind Caddy, which terminates TLS
(wss://) - see server/README.md. Nothing here logs an IP
address: the log only ever holds counts.

Limits that keep one person from knocking it over for everyone: connections
per network and in all, a few seconds to say hello, a steady rate of
requests per connection, and a cap on what's queued for a client that
isn't reading (it's dropped).

A connection may also send a bug report without saying hello (bugs.py),
and gets BUG_REPORT_TIMEOUT to finish it.

It also answers one plain HTTP request, GET /announcements.json - what
every Buddy checks once a day for the orb next to "Buddy" (chat users or
not). That request carries nothing about who's asking.

And it's the one place the server itself goes out to the web: GIF search's
downloads from GIPHY (make_fetch, for gifs.py), each on a thread so the
chat never waits for them.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
import urllib.error
import urllib.request

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .common import network_of
from .core import NetworkCore, Session

log = logging.getLogger("buddy_network")

MAX_FRAME_BYTES = 32 * 1024      # an encrypted 2,000-character DM is ~14 KB at worst; images come in ~24 KB parts
MAX_QUEUED_FRAMES = 500          # a client this far behind is dropped...
MAX_QUEUED_BYTES = 4 * 1024 * 1024   # ...or this many bytes behind
PURGE_EVERY_SECONDS = 3600
MAX_CONNECTIONS = 2000
MAX_CONNECTIONS_PER_NETWORK = 20     # a household or office shares one address
HELLO_TIMEOUT = 15.0                 # seconds a new connection has to say hello...
BUG_REPORT_TIMEOUT = 120.0           # ...or, sending a bug report instead (bugs.py), to finish it
# Requests per connection: a burst (signing in asks for a lot at once), then
# a steady rate. Past it requests are refused; far past it, disconnected.
FRAME_BURST, FRAME_RATE = 60, 10.0
FRAMES_REFUSED_MAX = 200
_LOOPBACK = {"127.0.0.1", "::1"}
FETCHES_AT_ONCE = 8                  # downloads from GIPHY at the same time
FETCH_TIMEOUT = 10.0


class WsSession(Session):
    def __init__(self, ip: str, ws=None):
        super().__init__(ip)
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(MAX_QUEUED_FRAMES)
        self.queued_bytes = 0
        self.overflowed = False
        self.tokens, self.refilled, self.refused = float(FRAME_BURST), time.monotonic(), 0

    def send(self, payload: dict):
        if self.overflowed:
            return
        frame = json.dumps(payload, ensure_ascii=False)
        if self.queued_bytes + len(frame) > MAX_QUEUED_BYTES:
            self._overflow()
            return
        try:
            self.queue.put_nowait(frame)
            self.queued_bytes += len(frame)
        except asyncio.QueueFull:
            self._overflow()

    def _overflow(self):
        """Too far behind (not reading): dropped now, rather than holding
        memory until it next sends something."""
        self.overflowed = True
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close(1008, "too far behind"))

    def allow_frame(self) -> bool:
        now = time.monotonic()
        self.tokens = min(FRAME_BURST, self.tokens + (now - self.refilled) * FRAME_RATE)
        self.refilled = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        self.refused += 1
        return False

    def close(self):
        super().close()
        try:
            self.queue.put_nowait(None)   # the writer closes after what's queued
        except asyncio.QueueFull:
            self._overflow()


def client_ip(ws, behind_proxy: bool) -> str:
    """The client's address, for the new-account limit only (never stored).
    Behind Caddy every connection comes from loopback, and the real address
    is the last X-Forwarded-For entry - the one Caddy itself added."""
    ip = ws.remote_address[0] if ws.remote_address else ""
    if behind_proxy and ip in _LOOPBACK:
        forwarded = ws.request.headers.get("X-Forwarded-For", "")
        if forwarded:
            ip = forwarded.split(",")[-1].strip()
    return ip


async def _writer(ws, session: WsSession):
    while True:
        frame = await session.queue.get()
        try:
            if frame is None:
                await ws.close()
                return
            session.queued_bytes -= len(frame)
            await ws.send(frame)
        finally:
            session.queue.task_done()


ANNOUNCEMENTS_PATH = "/announcements.json"


def make_process_request(core: NetworkCore):
    def process_request(connection, request):
        if request.path.split("?", 1)[0] != ANNOUNCEMENTS_PATH:
            return None   # carry on with the WebSocket handshake
        response = connection.respond(200, core.announcements_json())
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Cache-Control"] = "max-age=300"
        return response
    return process_request


def make_handler(core: NetworkCore, behind_proxy: bool, connections: set):
    per_network: collections.Counter = collections.Counter()

    async def handler(ws):
        ip = client_ip(ws, behind_proxy)
        network = network_of(ip)
        if len(connections) >= MAX_CONNECTIONS or per_network[network] >= MAX_CONNECTIONS_PER_NETWORK:
            await ws.close(1013, "too many connections")   # 1013: try again later
            return
        per_network[network] += 1
        session = WsSession(ip, ws)
        writer = asyncio.create_task(_writer(ws, session))
        connections.add(session)
        # Nothing but a hello (or a bug report) is any use before signing in:
        # a connection that sends neither is only holding a place.
        loop = asyncio.get_running_loop()
        timers = []

        def no_hello():
            if session.user_id is not None:
                return
            if session.bug_started and len(timers) == 1:
                timers.append(loop.call_later(BUG_REPORT_TIMEOUT - HELLO_TIMEOUT, no_hello))
                return
            asyncio.ensure_future(ws.close(1008, "no hello"))
        timers.append(loop.call_later(HELLO_TIMEOUT, no_hello))
        try:
            async for frame in ws:
                if not session.allow_frame():
                    if session.refused > FRAMES_REFUSED_MAX:
                        await ws.close(1008, "too many requests")
                        break
                    if session.refused % 20 == 1:   # say so now and then, not for every one
                        session.send({"type": "error", "code": "rate_limited", "re": None,
                                      "message": "Buddy is asking the server for too much at once - slow down."})
                    continue
                if not isinstance(frame, str):
                    session.send({"type": "error", "code": "bad_request", "message": "Text frames only.",
                                  "re": None})
                    continue
                core.handle(session, frame)
                if session.overflowed:
                    break
                if session.close_requested:
                    # Let the error that explains why reach the client first.
                    await asyncio.wait_for(session.queue.join(), 5)
                    break
        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        finally:
            for timer in timers:
                timer.cancel()
            core.disconnect(session)
            connections.discard(session)
            per_network[network] -= 1
            if per_network[network] <= 0:
                del per_network[network]
            writer.cancel()
            await ws.close()
    return handler


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """gifs.py checked the address is GIPHY's: a redirect isn't followed anywhere else."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def download(url: str, max_bytes: int) -> bytes | None:
    """The file, or None if it isn't there or is bigger than max_bytes.
    Nothing about it is logged: a search's address holds the API key."""
    request = urllib.request.Request(url, headers={"User-Agent": "BuddyNetwork", "Accept": "*/*"})
    try:
        with _opener.open(request, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                return None
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        log.warning("GIPHY answered %d", exc.code)
        return None
    except (OSError, ValueError) as exc:
        log.warning("GIPHY download failed: %s", type(exc).__name__)
        return None
    return data if len(data) <= max_bytes else None


def make_fetch():
    """fetch(url, max_bytes, done) for NetworkCore: done(bytes or None) is
    called on the event loop once the download is over."""
    limit = asyncio.Semaphore(FETCHES_AT_ONCE)
    running = set()

    async def go(url, max_bytes, done):
        async with limit:
            try:
                data = await asyncio.to_thread(download, url, max_bytes)
            except Exception as exc:   # whatever went wrong, done() is still called - or the search waits for ever
                log.warning("GIPHY download failed: %s", type(exc).__name__)
                data = None
        try:
            done(data)
        except Exception as exc:   # one answer handled badly mustn't stop the rest
            log.warning("handling a GIPHY download failed: %s", type(exc).__name__)

    def fetch(url, max_bytes, done):
        task = asyncio.ensure_future(go(url, max_bytes, done))
        running.add(task)
        task.add_done_callback(running.discard)
    return fetch


async def _purge_loop(core: NetworkCore, connections: set):
    while True:
        removed = core.purge()
        log.info("%d connected; purged %d old messages", len(connections), removed)
        await asyncio.sleep(PURGE_EVERY_SECONDS)


async def run(core: NetworkCore, host: str, port: int, behind_proxy: bool = False):
    connections: set = set()
    core.fetch = make_fetch()
    log.info("GIF search: %s", "on" if core.giphy else "off (no GIPHY_API_KEY)")
    async with serve(make_handler(core, behind_proxy, connections), host, port,
                     max_size=MAX_FRAME_BYTES, server_header=None,
                     process_request=make_process_request(core)) as server:
        log.info("Buddy Network server listening on %s:%d", host, port)
        purge = asyncio.create_task(_purge_loop(core, connections))
        try:
            await server.serve_forever()
        finally:
            purge.cancel()
