"""The Buddy System, direct messages and account deletion, mixed into
NetworkCore (core.py) - split out only to keep core.py readable.

Privacy rules this file keeps:
- Online status is only ever sent to buddies, and "appear offline" hides
  it from them too.
- A buddy request that's declined, or sent to someone who has blocked the
  sender, stays "waiting" on the sender's side forever: they can't tell a
  decline from a block from being ignored. Only accepting changes what
  they see.
- DMs are only possible between buddies, checked on every send.
- A PC's public key (for encrypted DMs) goes only to its owner and their
  buddies, in the buddy list.

Each user gets their whole buddy list again (a `buddy_list` frame) after
any change that affects it - lists are small, and it saves the client
from ever getting out of step.
"""

from __future__ import annotations

from .common import RequestError, device_id, key_bytes, public_user
from .store import dm_people, dm_room_id

MAX_BUDDIES = 200
MAX_WAITING = 100                   # outgoing requests not yet answered
BUDDY_REQUEST_LIMIT = (20, 86400.0)
BLOCK_LIMIT = (30, 60.0)
PRESENCE_LIMIT = (10, 60.0)
NEW_DEVICE_LIMIT = (10, 86400.0)    # new PC keys per account (each one tells every buddy)
MAX_DEVICES = 5              # PCs per account that DMs are encrypted for
DEVICE_IDLE_DAYS = 60        # a PC not seen this long is forgotten


def dm_room(room_id: str, other: dict) -> dict:
    """A DM conversation as the viewer sees it: named after the other person."""
    return {"id": room_id, "name": other["name"] or "Someone", "topic": "", "kind": "dm",
            "owner": None, "other": public_user(other)}


class SocialMixin:
    # Uses from NetworkCore: store, clock, limits, _by_user, _broadcast,
    # _remove_room, disconnect.

    # ------------------------------------------------------------ helpers

    def _sessions_of(self, user_id: str) -> list:
        return list(self._by_user.get(user_id, ()))

    def _visible_online(self, user: dict) -> bool:
        return bool(self._by_user.get(user["id"])) and not user["appear_offline"]

    def buddy_list_for(self, user_id: str) -> dict:
        store = self.store
        me = store.user(user_id)
        buddies = store.users(store.buddy_ids(user_id))
        keys = store.device_keys([u["id"] for u in buddies] + [user_id])
        return {
            "type": "buddy_list",
            # keys: their PCs' public keys, which your Buddy encrypts DMs for.
            "buddies": [{**public_user(u), "online": self._visible_online(u), "keys": list(keys[u["id"]].values())}
                        for u in buddies],
            "my_keys": list(keys[user_id].values()),
            "incoming": [public_user(u) for u in store.users(store.incoming_ids(user_id))],
            "outgoing": [public_user(u) for u in store.users(store.outgoing_ids(user_id))],
            "blocked": [public_user(u) for u in store.users(store.blocked_ids(user_id))],
            "appear_offline": bool(me and me["appear_offline"]),
        }

    def _push_buddy_lists(self, user_ids):
        for user_id in set(user_ids):
            sessions = self._sessions_of(user_id)
            if sessions:
                self._broadcast(self.buddy_list_for(user_id), sessions)

    def _people_who_see(self, user_id: str) -> set:
        """Everyone whose buddy list shows this user (as a buddy, a request
        either way, or a block) - told again when their name or status changes."""
        store = self.store
        return (set(store.buddy_ids(user_id)) | set(store.request_counterparts(user_id))
                | set(store.blocked_by_ids(user_id)))

    def _presence_changed(self, user_id: str):
        self._push_buddy_lists(self.store.buddy_ids(user_id))

    def _keys_changed(self, user_id: str):
        self._push_buddy_lists([user_id] + self.store.buddy_ids(user_id))

    def _other_user(self, session, raw) -> dict:
        if not isinstance(raw, str) or not raw.strip():
            raise RequestError("no_user", "There's no one with that ID.")
        user = self.store.user(raw.strip().lower())
        if user is None:
            raise RequestError("no_user", "There's no one with that ID.")
        if user["id"] == session.user_id:
            raise RequestError("no_user", "That's you.")
        return user

    # ----------------------------------------------------------- requests

    def _buddy_request(self, session, msg: dict):
        store, me = self.store, session.user_id
        other = self._other_user(session, msg.get("user"))
        them = other["id"]
        if not store.user(me)["name"]:
            raise RequestError("no_name", "Choose a name before adding buddies.")
        if store.are_buddies(me, them):
            self._push_buddy_lists([me])
            return
        if store.is_blocked(me, them):
            raise RequestError("blocked", "You've blocked them - unblock them first.")
        if store.request(them, me):
            # They asked first (even if you'd said no before): you're buddies.
            store.add_buddies(me, them, self.clock())
            self._push_buddy_lists([me, them])
            return
        if store.request(me, them):
            self._push_buddy_lists([me])
            return
        if len(store.buddy_ids(me)) >= MAX_BUDDIES:
            raise RequestError("too_many", f"You have {MAX_BUDDIES} buddies - remove some to add more.")
        if len(store.outgoing_ids(me)) >= MAX_WAITING:
            raise RequestError("too_many", f"You have {MAX_WAITING} requests waiting for an answer - "
                                           "cancel some first.")
        wait = self.limits.check(("buddy_request", me), *BUDDY_REQUEST_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of buddy requests today - try again "
                                               "tomorrow.", retry_after=round(wait))
        store.add_request(me, them, self.clock())
        # If they've blocked you, their list doesn't show it (incoming_ids)
        # - and nothing here tells you so.
        self._push_buddy_lists([me, them])

    def _buddy_accept(self, session, msg: dict):
        other = self._other_user(session, msg.get("user"))
        if not self.store.request(other["id"], session.user_id):
            raise RequestError("no_request", "That request isn't there any more.")
        self.store.add_buddies(session.user_id, other["id"], self.clock())
        self._push_buddy_lists([session.user_id, other["id"]])

    def _buddy_decline(self, session, msg: dict):
        other = self._other_user(session, msg.get("user"))
        if self.store.request(other["id"], session.user_id):
            self.store.decline_request(other["id"], session.user_id)
        self._push_buddy_lists([session.user_id])   # not theirs: to them it's still waiting

    # The other person is only sent their list again when something changed
    # for them - otherwise repeating these would flood anyone whose ID you know.

    def _buddy_cancel(self, session, msg: dict):
        other = self._other_user(session, msg.get("user"))
        changed = self.store.request(session.user_id, other["id"]) is not None
        self.store.delete_request(session.user_id, other["id"])
        self._push_buddy_lists([session.user_id, other["id"]] if changed else [session.user_id])

    def _buddy_remove(self, session, msg: dict):
        other = self._other_user(session, msg.get("user"))
        changed = self.store.are_buddies(session.user_id, other["id"])
        self.store.remove_buddies(session.user_id, other["id"])
        self._push_buddy_lists([session.user_id, other["id"]] if changed else [session.user_id])

    def _block(self, session, msg: dict):
        other = self._other_user(session, msg.get("user"))
        wait = self.limits.check(("block", session.user_id), *BLOCK_LIMIT)
        if wait:
            raise RequestError("rate_limited", "Slow down a little.", retry_after=round(wait, 1))
        self.store.block(session.user_id, other["id"], self.clock())
        self._push_buddy_lists([session.user_id, other["id"]])

    def _unblock(self, session, msg: dict):
        other = self._other_user(session, msg.get("user"))
        self.store.unblock(session.user_id, other["id"])
        self._push_buddy_lists([session.user_id])

    def _set_presence(self, session, msg: dict):
        offline = bool(msg.get("offline"))
        if offline == bool(self.store.user(session.user_id)["appear_offline"]):
            self._push_buddy_lists([session.user_id])
            return
        wait = self.limits.check(("presence", session.user_id), *PRESENCE_LIMIT)
        if wait:
            raise RequestError("rate_limited", "Slow down a little.", retry_after=round(wait, 1))
        self.store.set_appear_offline(session.user_id, offline)
        self._push_buddy_lists([session.user_id])
        self._presence_changed(session.user_id)

    # ---------------------------------------------------------------- DMs

    def _set_device_key(self, session, msg: dict):
        """A Buddy registers its PC's public key after every sign-in (which
        also keeps it from being forgotten as idle)."""
        key = key_bytes(msg.get("key"))
        if key is None:
            raise RequestError("bad_request", "That isn't a device key.")
        device = device_id(key)
        known = self.store.device_keys([session.user_id])[session.user_id].get(device) == msg["key"]
        if not known:
            wait = self.limits.check(("new_device", session.user_id), *NEW_DEVICE_LIMIT)
            if wait:
                raise RequestError("rate_limited", "That's a lot of new PCs for one identity today - try "
                                                   "again tomorrow.", retry_after=round(wait))
        new = self.store.add_device(session.user_id, device, msg["key"], self.clock(), MAX_DEVICES)
        session.send({"type": "device_registered", "device": device})
        if new:
            self._keys_changed(session.user_id)

    def _open_dm(self, session, msg: dict):
        other = self._other_user(session, msg.get("user"))
        if not self.store.are_buddies(session.user_id, other["id"]):
            raise RequestError("not_buddies", "You can only message your buddies.")
        room_id = dm_room_id(session.user_id, other["id"])
        self.store.ensure_room(room_id, "", "", "dm", self.clock())
        session.send({"type": "dm_opened", "room": dm_room(room_id, other)})

    def _dm_other(self, session, room_id: str) -> str | None:
        """The other person in a DM the session is part of; None if it's
        not a DM, NoRoom (as RequestError) if it's someone else's."""
        people = dm_people(room_id)
        if people is None:
            return None
        if session.user_id not in people:
            # Same answer as a room that doesn't exist: nothing to learn.
            raise RequestError("no_room", "That room doesn't exist.")
        return people[1] if people[0] == session.user_id else people[0]

    def _check_can_dm(self, me: str, them: str):
        store = self.store
        if not store.are_buddies(me, them) or store.is_blocked(me, them) or store.is_blocked(them, me):
            raise RequestError("not_buddies", "You can only message your buddies.")

    def _dm_audience(self, room_id: str) -> list:
        """Every session of both people - a DM reaches them even when that
        conversation isn't open, for unread counts and notifications."""
        a, b = dm_people(room_id)
        return self._sessions_of(a) + self._sessions_of(b)

    # ------------------------------------------------------------ account

    def _delete_account(self, session, msg: dict):
        store, me = self.store, session.user_id
        affected = self._people_who_see(me) | set(store.blocked_ids(me))
        for room in store.rooms_owned_by(me):
            self._remove_room(room["id"], "deleted")
        for room_id in store.dm_room_ids_of(me):
            audience = self._dm_audience(room_id)
            store.delete_room(room_id)
            self._broadcast({"type": "room_removed", "room": room_id, "reason": "deleted"}, audience)
        store.delete_user(me)
        for s in self._sessions_of(me):
            s.send({"type": "account_deleted"})
            self.disconnect(s)
            s.user_id = None
            s.close()
        self._push_buddy_lists(affected)

    _SOCIAL_HANDLERS = {
        "buddy_request": _buddy_request,
        "buddy_accept": _buddy_accept,
        "buddy_decline": _buddy_decline,
        "buddy_cancel": _buddy_cancel,
        "buddy_remove": _buddy_remove,
        "block": _block,
        "unblock": _unblock,
        "set_presence": _set_presence,
        "open_dm": _open_dm,
        "set_device_key": _set_device_key,
        "delete_account": _delete_account,
    }
