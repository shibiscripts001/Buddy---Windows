"""Bug reports, mixed into NetworkCore (core.py): what someone writes in
Buddy's "Report a bug" window, with a few screenshots, for the owner's
Admin panel (its Bugs tab).

Anyone can send one - signed in to Buddy Network or not. A Buddy that's
signed in sends it over its connection, and the report says who it's from.
One that isn't opens a connection just for the report and sends it
without a hello (the only requests allowed before one: PRE_HELLO); the
report then has no name, and the connection closes once it's in. Either
way the screenshots come up first in bug_part frames, like a message's
image_part ones, and the bug_report that follows names them.

What a report holds: the text, the details the sender's Buddy adds (its
version, Windows', Resolve's and the tool that was open - nothing else
about the PC or the project) and the screenshots, already shrunk on the
sender's PC. No address: the per-network limit below is in memory, like
every other limit. Kept until the owner deletes it, BUG_DAYS at most.
"""

from __future__ import annotations

import base64
import binascii
import re

from .admin import _person, is_id, one_line
from .common import RequestError, network_of

# Keep in step with app/core/bug_report.py (tests check).
BUG_TEXT_MAX = 4000
BUG_IMAGES_MAX = 6
# What a Buddy may add, one line each. "windows" is the system, a Mac's too.
BUG_DETAILS = ("buddy", "windows", "resolve", "tool")
BUG_DETAIL_MAX = 120
BUG_LIMIT = (5, 3600.0)            # reports per network (and per person) an hour
BUG_DAYS = 90
BUG_OPEN_MAX = 500                 # past this many waiting, new ones are turned away...
BUG_STORE_MAX = 512 * 1024 ** 2    # ...and past this many bytes of screenshots
PRE_HELLO = ("hello", "bug_part", "bug_report")
BUG_REPORT_TIMEOUT = 120.0         # seconds a connection sending one without a hello has (net.py)
_IMAGE_ID = re.compile(r"[0-9a-f]{32}")


class BugMixin:
    # Uses from NetworkCore: store, clock, limits, _sessions_of, _by_user,
    # _broadcast, _role, _require_owner, _log; MAX_IMAGE_BYTES and
    # MAX_IMAGE_SIDE, is_image_file and clean_text from core.py.

    def _bug_problem(self, session):
        session.bug_upload, session.bug_images = None, []
        return RequestError("bad_image", "A screenshot didn't arrive in one piece - try again.")

    def _bug_part(self, session, msg: dict):
        """One piece of a screenshot on its way up. seq 0 starts one; the
        one marked last completes it. One at a time, BUG_IMAGES_MAX in all."""
        from .core import IMAGE_PART_CHARS, MAX_IMAGE_BYTES
        session.bug_started = True
        image_id, seq, data = msg.get("id"), msg.get("seq"), msg.get("data")
        if (not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id) or not is_id(seq)
                or not isinstance(data, str) or len(data) > IMAGE_PART_CHARS):
            raise self._bug_problem(session)
        if seq == 0:
            if len(session.bug_images) >= BUG_IMAGES_MAX:
                session.bug_upload, session.bug_images = None, []
                raise RequestError("too_many", f"A report can have {BUG_IMAGES_MAX} screenshots at most.")
            session.bug_upload = {"id": image_id, "parts": [], "size": 0}
        upload = session.bug_upload
        if upload is None or upload["id"] != image_id or seq != len(upload["parts"]):
            raise self._bug_problem(session)
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise self._bug_problem(session) from None
        upload["size"] += len(chunk)
        if upload["size"] > MAX_IMAGE_BYTES:
            session.bug_upload, session.bug_images = None, []
            raise RequestError("too_big", f"Screenshots are at most {MAX_IMAGE_BYTES // 1024} KB - update Buddy "
                                          "so it shrinks them to fit.")
        upload["parts"].append(chunk)
        if msg.get("last"):
            session.bug_images.append((image_id, b"".join(upload["parts"])))
            session.bug_upload = None

    def _bug_images(self, session, raw) -> list[dict]:
        """The screenshots a report names: exactly the ones this connection
        just sent, in order, each a picture of a sane size."""
        from .core import MAX_IMAGE_SIDE, is_image_file
        uploaded, session.bug_images, session.bug_upload = session.bug_images, [], None
        raw = [] if raw is None else raw
        if not isinstance(raw, list) or len(raw) != len(uploaded):
            raise RequestError("bad_image", "A screenshot didn't arrive in one piece - try again.")
        shots = []
        for entry, (image_id, data) in zip(raw, uploaded):
            w, h = (entry.get("w"), entry.get("h")) if isinstance(entry, dict) else (None, None)
            if (not isinstance(entry, dict) or entry.get("id") != image_id
                    or not all(is_id(v) and 1 <= v <= MAX_IMAGE_SIDE for v in (w, h))):
                raise RequestError("bad_image", "A screenshot didn't arrive in one piece - try again.")
            if not is_image_file(data):
                raise RequestError("bad_image", "Only pictures can be sent (WebP, JPEG or PNG).")
            if self.store.bug_image(image_id) is not None or self.store.image(image_id) is not None:
                raise RequestError("bad_image", "A screenshot didn't arrive in one piece - try again.")
            shots.append({"id": image_id, "w": w, "h": h, "data": data})
        return shots

    def _bug_report(self, session, msg: dict):
        from .core import clean_text
        session.bug_started = True
        shots = self._bug_images(session, msg.get("images"))
        raw_text = msg.get("text")
        if shots and (raw_text is None or (isinstance(raw_text, str) and not raw_text.strip())):
            text = ""
        else:
            text = clean_text(raw_text, BUG_TEXT_MAX, "report")
        raw_details = msg.get("details")
        details = {}
        if isinstance(raw_details, dict):
            for key in BUG_DETAILS:
                value = one_line(raw_details.get(key), BUG_DETAIL_MAX, "detail") if isinstance(
                    raw_details.get(key), str) else ""
                if value:
                    details[key] = value
        now = self.clock()
        if session.user_id is None and session.ip:
            if self.store.network_ban(self.store.ip_hash(network_of(session.ip)), now):
                session.close_requested = True
                raise RequestError("banned", "Bug reports can't be sent from this network right now.")
        # Per network (a household shares one), and per person wherever they are.
        keys = [("bug_network", network_of(session.ip or ""))]
        if session.user_id:
            keys.append(("bug_user", session.user_id))
        for key in keys:
            wait = self.limits.check(key, *BUG_LIMIT)
            if wait:
                raise RequestError("rate_limited", "That's a lot of bug reports - try again later.",
                                   retry_after=round(wait))
        if (self.store.bug_report_count() >= BUG_OPEN_MAX
                or self.store.bug_images_size() + sum(len(s["data"]) for s in shots) > BUG_STORE_MAX):
            raise RequestError("server_full", "The server can't take more bug reports right now - try again "
                                              "in a few days.")
        self.store.add_bug_report(session.user_id, text, details, now, shots)
        session.send({"type": "bug_reported"})
        self._tell_owner_about_bugs()
        if session.user_id is None:
            session.close()   # it only came for the report

    # --------------------------------------------------------- the owner's

    def _owner_sessions(self) -> list:
        return [s for uid in list(self._by_user) if self._role(uid) == "owner" for s in self._sessions_of(uid)]

    def _tell_owner_about_bugs(self, sessions=None):
        payload = {"type": "bug_reports_waiting", "count": self.store.bug_report_count()}
        self._broadcast(payload, self._owner_sessions() if sessions is None else sessions)

    def _bug_reports_payload(self) -> dict:
        reports = [{"id": r["id"], "created": r["created"], "text": r["text"], "details": r["details"],
                    "images": r["images"],
                    "reporter": _person(r["reporter"], r["reporter_name"]) if r["reporter"] else None}
                   for r in self.store.bug_reports()]
        return {"type": "bug_reports", "reports": reports}

    def _list_bug_reports(self, session, msg: dict):
        self._require_owner(session)
        session.send(self._bug_reports_payload())

    def _delete_bug_report(self, session, msg: dict):
        self._require_owner(session)
        report_id = msg.get("id")
        if not is_id(report_id) or not self.store.delete_bug_report(report_id):
            raise RequestError("no_report", "That bug report isn't there any more.")
        self._log(session, "delete_bug_report", "", str(report_id))
        session.send(self._bug_reports_payload())
        self._tell_owner_about_bugs()

    def bug_image_for(self, session, image_id: str) -> dict | None:
        """A screenshot's bytes, for get_image - the owner's alone."""
        return self.store.bug_image(image_id) if self._role(session.user_id) == "owner" else None

    def purge_bugs(self):
        self.store.purge_bug_reports(self.clock() - BUG_DAYS * 86400)

    _BUG_HANDLERS = {
        "bug_part": _bug_part,
        "bug_report": _bug_report,
        "list_bug_reports": _list_bug_reports,
        "delete_bug_report": _delete_bug_report,
    }
