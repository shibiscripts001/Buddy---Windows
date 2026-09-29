"""Admin tools, mixed into NetworkCore (core.py): reports, bans, roles and
the admin log. Room renames and announcements are in core.py with the
other room requests.

Roles, lowest first: "user", "mod", "admin", "owner" (set on the server:
python -m server make-owner <id>). Everyone above "user" is staff: they can
delete any message, rename/delete any room, pin room announcements, ban
and unban, and work through reports. Bans only reach down: a mod bans
users, an admin bans mods too, only the owner bans admins, and nobody bans
the owner. Admins make and remove mods; only the owner makes admins, reads
the admin log, posts app announcements, makes a room public (in everyone's
list, with Global and Help - core.py _set_public) and deletes a message
forever - leaving no "message deleted" behind (core.py).

What admins can see: reported messages (the text as it was when reported,
the room, who reported it) and nothing else - they never browse DMs. DMs
are end-to-end encrypted, so for a reported DM the reporter's Buddy sends
the text it decrypted; the report says so (claimed), since nobody can check
that text is what was really sent.
Network bans store a keyed hash of a banned user's address, taken from
their live connection (nothing else keeps addresses, so a banned user who
is offline can only be banned by ID).
"""

from __future__ import annotations

import unicodedata

from .common import RequestError, network_of, public_user, tag_of

MAX_ID = 2 ** 63 - 1


def is_id(raw) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= MAX_ID

ROLES = ("user", "mod", "admin", "owner")   # lowest first
RANK = {role: i for i, role in enumerate(ROLES)}
STAFF_ROLES = ("mod", "admin", "owner")     # every moderation power
ROLE_GRANTERS = ("admin", "owner")          # can change roles (below their own)
BAN_DAYS = (1, 7, 30, None)          # None: permanent
REASON_MAX = 200
REPORT_LIMIT = (10, 3600.0)
REPORT_DAYS = 30
LOG_DAYS = 90
EXCERPT_DAYS = 30                    # the text of a message staff deleted, in the log
LOG_ENTRIES = 200


def one_line(raw, limit: int, what: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise RequestError("bad_request", f"The {what} is text.")
    text = " ".join("".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in raw).split())
    if len(text) > limit:
        raise RequestError("too_long", f"The {what} is at most {limit} characters.")
    return text


def _person(user_id: str, name) -> dict:
    return {"id": user_id or "", "tag": tag_of(user_id or ""), "name": name}


class AdminMixin:
    # Uses from NetworkCore: store, clock, limits, _by_user, _broadcast,
    # _sessions_of, disconnect, _dm_other, _room_or_error, _mark_deleted.

    def _role(self, user_id: str) -> str:
        user = self.store.user(user_id) if user_id else None
        return user["role"] if user else "user"

    def _is_staff(self, user_id: str) -> bool:
        return self._role(user_id) in STAFF_ROLES

    def _require_staff(self, session):
        if not self._is_staff(session.user_id):
            raise RequestError("not_allowed", "Only mods and admins can do that.")

    def _require_owner(self, session):
        if self._role(session.user_id) != "owner":
            raise RequestError("not_allowed", "Only the owner can do that.")

    def _log(self, session, action: str, target: str = "", detail: str = ""):
        self.store.log(session.user_id, action, target, detail[:300], self.clock())

    def _admin_sessions(self) -> list:
        return [s for uid in list(self._by_user) if self._is_staff(uid) for s in self._sessions_of(uid)]

    def _tell_admins_about_reports(self, sessions=None):
        payload = {"type": "reports_waiting", "count": len(self.store.open_reports())}
        self._broadcast(payload, self._admin_sessions() if sessions is None else sessions)

    def _kick(self, user_id: str, payload: dict):
        for s in self._sessions_of(user_id):
            s.send(payload)
            self.disconnect(s)
            s.user_id = None
            s.close()

    # ------------------------------------------------------------ reports

    def _report(self, session, msg: dict):
        message_id = msg.get("id")
        row, _room = self._visible_message(session, message_id)   # you can only report what you can see
        if row["deleted"]:
            raise RequestError("no_message", "That message isn't there any more.")
        if row["author_id"] == session.user_id:
            raise RequestError("bad_request", "That's your own message - delete it instead.")
        reason = one_line(msg.get("reason"), REASON_MAX, "reason")
        claimed = None
        if row.get("enc"):
            from .core import clean_text
            if not msg.get("text"):
                raise RequestError("bad_request", "Only a message Buddy could read can be reported.")
            claimed = clean_text(msg.get("text"))
        wait = self.limits.check(("report", session.user_id), *REPORT_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of reports - try again later.",
                               retry_after=round(wait))
        if self.store.add_report(row, session.user_id, reason, self.clock(), claimed):
            self._tell_admins_about_reports()
        session.send({"type": "reported", "id": message_id})

    def _reports_payload(self) -> dict:
        reports = []
        for r in self.store.open_reports():
            where = "a direct message" if r["room_kind"] == "dm" else f"#{r['room_name'] or 'deleted room'}"
            reports.append({
                "id": r["id"], "message_id": r["message_id"], "room": r["room"], "where": where,
                "text": r["text"], "reason": r["reason"], "created": r["created"], "times": r["times"],
                "claimed": bool(r["claimed"]),
                # The reported message's image, while it's still there (get_image fetches it).
                **({"image": r["image"]} if r["image"] else {}),
                "reporter": _person(r["reporter"], r["reporter_name"]),
                "reported": _person(r["reported"], r["reported_name"]),
            })
        return {"type": "reports", "reports": reports}

    def _list_reports(self, session, msg: dict):
        self._require_staff(session)
        session.send(self._reports_payload())

    def _resolve_report(self, session, msg: dict):
        """Closes every report of that message; delete=True deletes it too."""
        self._require_staff(session)
        report_id = msg.get("id")
        report = self.store.report(report_id) if is_id(report_id) else None
        if report is None:
            raise RequestError("no_report", "That report isn't there any more.")
        if report["reported"] == session.user_id:
            raise RequestError("not_allowed", "That report is about you - leave it for another mod or admin.")
        if msg.get("delete"):
            row = self.store.message(report["message_id"])
            if row is not None and not row["deleted"]:
                self._mark_deleted(row)
                self._log(session, "delete_message", report["reported"], row["text"][:120] or report["text"][:120])
        self.store.resolve_reports_for(report["message_id"], session.user_id)
        self._log(session, "resolve_report", report["reported"], "deleted" if msg.get("delete") else "kept")
        session.send(self._reports_payload())
        self._tell_admins_about_reports()

    # --------------------------------------------------------------- bans

    def _ban(self, session, msg: dict):
        self._require_staff(session)
        target = self._other_user(session, msg.get("user"))
        if RANK.get(target["role"], 0) >= RANK[self._role(session.user_id)]:
            raise RequestError("not_allowed", "You can only ban people below you: mods ban users, admins ban "
                                              "mods too, and only the owner bans admins.")
        days = msg.get("days")
        if days not in BAN_DAYS:
            raise RequestError("bad_request", "Ban for 1, 7 or 30 days, or permanently.")
        reason = one_line(msg.get("reason"), REASON_MAX, "reason")
        now = self.clock()
        until = None if days is None else now + days * 86400
        self.store.ban(target["id"], until, reason, session.user_id, now)
        networks = 0
        if msg.get("network"):
            for s in self._sessions_of(target["id"]):
                if s.ip:
                    self.store.ban_network(self.store.ip_hash(network_of(s.ip)), target["id"], until, now)
                    networks += 1
        self._log(session, "ban", target["id"], f"{'permanent' if days is None else f'{days} days'}; "
                  f"network: {networks}; {reason}")
        self._kick(target["id"], self._banned_error(until, reason))
        session.send({"type": "ban_done", "user": target["id"], "networks": networks})
        session.send(self._bans_payload())

    def _banned_error(self, until, reason: str) -> dict:
        return {"type": "error", "code": "banned", "re": "hello", "until": until, "reason": reason,
                "message": "You've been banned from Buddy Network."}

    def _bans_payload(self) -> dict:
        bans = [{"user": _person(b["user_id"], b["name"]), "until": b["until"], "reason": b["reason"],
                 "created": b["created"], "network": bool(b["network"])}
                for b in self.store.bans(self.clock())]
        return {"type": "bans", "bans": bans}

    def _unban(self, session, msg: dict):
        """Like bans, unbans only reach down - and never undo a ban by
        someone ranked above you."""
        self._require_staff(session)
        user_id = msg.get("user")
        ban = self.store.ban_of(user_id, self.clock()) if isinstance(user_id, str) else None
        if not ban:
            raise RequestError("no_ban", "They aren't banned.")
        mine = RANK[self._role(session.user_id)]
        if RANK.get(self._role(user_id), 0) >= mine or RANK.get(self._role(ban["by"]), 0) > mine:
            raise RequestError("not_allowed", "Someone ranked above you made that ban (or they're ranked "
                                              "at least as high as you) - ask them.")
        self.store.unban(user_id)
        self._log(session, "unban", user_id)
        session.send(self._bans_payload())

    def _list_bans(self, session, msg: dict):
        self._require_staff(session)
        session.send(self._bans_payload())

    # -------------------------------------------------------- owner only

    def _set_role(self, session, msg: dict):
        """The owner makes mods and admins; an admin makes mods. Either only
        changes someone below them, and only to a role below their own."""
        mine = self._role(session.user_id)
        if mine not in ROLE_GRANTERS:
            raise RequestError("not_allowed", "Only admins and the owner can change roles.")
        target = self._other_user(session, msg.get("user"))
        role = msg.get("role")
        if role not in ROLES[:-1]:
            raise RequestError("bad_request", "Make someone a mod or an admin, or an ordinary user again.")
        if RANK[role] >= RANK[mine] or RANK.get(target["role"], 0) >= RANK[mine]:
            raise RequestError("not_allowed", "Admins can make and remove mods; only the owner can make "
                                              "or remove admins.")
        if role != "user" and not target["name"]:
            raise RequestError("no_name", "They need a name before they can be staff.")
        self.store.set_role(target["id"], role)
        self._log(session, "set_role", target["id"], role)
        user = public_user(self.store.user(target["id"]))
        self._broadcast({"type": "role_changed", "user": user}, self._sessions_of(target["id"]))
        if role in STAFF_ROLES:
            self._tell_admins_about_reports(self._sessions_of(target["id"]))
        session.send(self._admins_payload())

    def _admins_payload(self) -> dict:
        staff = sorted(self.store.users_with_roles(),
                       key=lambda u: (-RANK.get(u["role"], 0), (u["name"] or "").casefold()))
        return {"type": "admins", "admins": [public_user(u) for u in staff]}

    def _list_admins(self, session, msg: dict):
        """Everyone with a role - for the owner and admins, who manage them."""
        if self._role(session.user_id) not in ROLE_GRANTERS:
            raise RequestError("not_allowed", "Only admins and the owner can see the staff list.")
        session.send(self._admins_payload())

    def _admin_log(self, session, msg: dict):
        self._require_owner(session)
        entries = [{"ts": e["ts"], "actor": _person(e["actor"], e["actor_name"]), "action": e["action"],
                    "target": e["target"], "detail": e["detail"]} for e in self.store.admin_log(LOG_ENTRIES)]
        session.send({"type": "admin_log", "entries": entries})

    def purge_admin(self):
        now = self.clock()
        self.store.purge_admin(now, now - REPORT_DAYS * 86400, now - LOG_DAYS * 86400,
                               now - EXCERPT_DAYS * 86400)

    _ADMIN_HANDLERS = {
        "report": _report,
        "list_reports": _list_reports,
        "resolve_report": _resolve_report,
        "ban": _ban,
        "unban": _unban,
        "list_bans": _list_bans,
        "set_role": _set_role,
        "list_admins": _list_admins,
        "admin_log": _admin_log,
    }
