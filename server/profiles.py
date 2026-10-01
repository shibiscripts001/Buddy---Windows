"""Profile pages and who's here, mixed into NetworkCore (core.py).

Profiles: everyone has one, old-MySpace style - a headline, a mood, a bit
about themselves, what they're working on and listening to, a theme for
the page (one of PROFILE_THEMES, which Buddy draws - never anything a
person sent), their top buddies (up to TOP_MAX, picked from their buddies)
and a count of profile views. Anyone signed in can look at anyone's.
Online status still only goes to buddies (social.py's rule): a profile
only says whether its person is online to their buddies.

A view is counted once a day per viewer, and never your own - so the
count can't be run up by reopening a page. Staff can empty what someone
wrote on theirs (clear_profile, in the admin log), reaching down like bans.

Who's here: everyone in a public room (Global, Help, and the rooms the
owner made public) can see who else is in it - not anyone set to appear
offline, the same rule as the room's "here" count. A Buddy that joins gets
the list at once; everyone else gets it again at most every WHO_EVERY
seconds (net.py calls flush_who), so a busy room isn't sent a list for
every coming and going. User rooms and DMs have no list.
"""

from __future__ import annotations

from .admin import RANK, one_line
from .common import RequestError, public_user

MOODS = ("happy", "chill", "busy", "creative", "tired", "excited", "focused", "silly", "grumpy", "inspired",
         "rendering", "caffeinated")
PROFILE_THEMES = ("classic", "midnight", "bubblegum", "hacker", "sunset", "ocean", "forest", "glitter",
                  "notebook", "retro")
HEADLINE_MAX = 100
ABOUT_MAX = 1000
LINE_MAX = 100          # working on, listening to
TOP_MAX = 8
PROFILE_SET_LIMIT = (30, 3600.0)
PROFILE_GET_LIMIT = (120, 60.0)
VIEW_EVERY = 86400.0    # one counted view per viewer and profile in this long
WHO_EVERY = 2.0         # seconds between who's-here lists to one room
WHO_MAX = 200           # people listed (the count covers everyone)

TEXT_FIELDS = ("headline", "about", "working_on", "listening")


def _about(raw) -> str:
    """Several lines, kept like a message's text - but may be empty."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    from .core import clean_text   # core imports this module
    text = clean_text(raw, ABOUT_MAX * 2, "about me")
    if len(text) > ABOUT_MAX:
        raise RequestError("too_long", f"About me is at most {ABOUT_MAX:,} characters.")
    return text


class ProfileMixin:
    # Uses from NetworkCore: store, clock, limits, _subscribers, _sessions_of,
    # _other_user, _role, _require_staff, _log, _visible_online.

    def _init_profiles(self):
        self._viewed: dict[tuple[str, str], float] = {}
        self._who_dirty: set[str] = set()

    # ------------------------------------------------------------ profiles

    def _profile_payload(self, viewer_id: str, user: dict) -> dict:
        store = self.store
        row = store.profile(user["id"])
        buddies = set(store.buddy_ids(user["id"]))
        top_ids = [i for i in row["top"] if i in buddies][:TOP_MAX]
        by_id = {u["id"]: u for u in store.users(top_ids)}
        payload = {
            "type": "profile",
            "user": public_user(user),
            "since": row["created"],
            "views": row["views"],
            "buddy_count": len(buddies),
            "profile": {k: row[k] for k in ("headline", "mood", "about", "working_on", "listening", "theme")},
            "top": [public_user(by_id[i]) for i in top_ids if i in by_id],
        }
        if viewer_id == user["id"] or viewer_id in buddies:
            payload["online"] = self._visible_online(user)
        return payload

    def _get_profile(self, session, msg: dict):
        raw = msg.get("user")
        user = (self.store.user(session.user_id) if raw == session.user_id
                else self._other_user(session, raw))
        wait = self.limits.check(("profile_get", session.user_id), *PROFILE_GET_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of profiles - slow down a little.",
                               retry_after=round(wait, 1))
        now = self.clock()
        key = (session.user_id, user["id"])
        if user["id"] != session.user_id and now - self._viewed.get(key, -VIEW_EVERY) >= VIEW_EVERY:
            self._viewed[key] = now
            self.store.add_profile_view(user["id"])
        session.send(self._profile_payload(session.user_id, user))

    def _set_profile(self, session, msg: dict):
        fields = {
            "headline": one_line(msg.get("headline"), HEADLINE_MAX, "headline"),
            "about": _about(msg.get("about")),
            "working_on": one_line(msg.get("working_on"), LINE_MAX, "working on"),
            "listening": one_line(msg.get("listening"), LINE_MAX, "listening to"),
        }
        mood, theme = msg.get("mood") or "", msg.get("theme") or ""
        if mood not in MOODS + ("",):
            raise RequestError("bad_request", "That isn't one of the moods.")
        if theme not in PROFILE_THEMES + ("",):
            raise RequestError("bad_request", "That isn't one of the profile themes.")
        top = msg.get("top") if msg.get("top") is not None else []
        if not isinstance(top, list) or not all(isinstance(i, str) for i in top):
            raise RequestError("bad_request", "'top' is a list of IDs.")
        buddies = set(self.store.buddy_ids(session.user_id))
        top = [i for i in dict.fromkeys(top) if i in buddies]
        if len(top) > TOP_MAX:
            raise RequestError("too_many", f"Pick up to {TOP_MAX} top buddies.")
        wait = self.limits.check(("profile_set", session.user_id), *PROFILE_SET_LIMIT)
        if wait:
            raise RequestError("rate_limited", "That's a lot of profile changes - try again later.",
                               retry_after=round(wait))
        self.store.set_profile(session.user_id, {**fields, "mood": mood, "theme": theme, "top": top},
                               self.clock())
        me = self.store.user(session.user_id)
        for s in self._sessions_of(session.user_id):
            s.send(self._profile_payload(session.user_id, me))
        session.send({"type": "profile_saved"})

    def _clear_profile(self, session, msg: dict):
        """Staff: empties what someone wrote on their profile (not its theme
        or top buddies)."""
        self._require_staff(session)
        target = self._other_user(session, msg.get("user"))
        if RANK.get(target["role"], 0) >= RANK[self._role(session.user_id)]:
            raise RequestError("not_allowed", "You can only clear the profiles of people below you.")
        self.store.clear_profile(target["id"], self.clock())
        self._log(session, "clear_profile", target["id"])
        session.send(self._profile_payload(session.user_id, target))
        for s in self._sessions_of(target["id"]):
            s.send(self._profile_payload(target["id"], target))

    def _prune_profile_views(self):
        cutoff = self.clock() - VIEW_EVERY
        self._viewed = {k: t for k, t in self._viewed.items() if t >= cutoff}

    # ---------------------------------------------------------- who's here

    def _is_public(self, room_id: str) -> bool:
        room = self.store.room(room_id) if isinstance(room_id, str) else None
        return room is not None and room["kind"] == "system"

    def _who_payload(self, room_id: str) -> dict:
        ids = {s.user_id for s in self._subscribers.get(room_id, ()) if s.user_id}
        people = [u for u in self.store.users(ids) if not u["appear_offline"] and u["name"]]
        return {"type": "who", "room": room_id, "total": len(people),
                "people": [public_user(u) for u in people[:WHO_MAX]]}

    def _send_who(self, session, room_id: str):
        if self._is_public(room_id):
            session.send(self._who_payload(room_id))

    def _who_changed(self, room_ids):
        """Someone came, went, or changed how they're shown: those rooms'
        lists go out with the next flush_who."""
        for room_id in room_ids:
            if self._is_public(room_id):
                self._who_dirty.add(room_id)

    def _who_changed_for(self, user_id: str):
        """Every public room this person is in, on any of their Buddys."""
        self._who_changed({r for s in self._sessions_of(user_id) for r in s.rooms})

    def flush_who(self):
        """Sends each changed room's list to everyone in it (net.py, every
        WHO_EVERY seconds)."""
        dirty, self._who_dirty = self._who_dirty, set()
        for room_id in dirty:
            sessions = self._subscribers.get(room_id)
            if sessions:
                payload = self._who_payload(room_id)
                for s in list(sessions):
                    s.send(payload)

    _PROFILE_HANDLERS = {
        "get_profile": _get_profile,
        "set_profile": _set_profile,
        "clear_profile": _clear_profile,
    }
