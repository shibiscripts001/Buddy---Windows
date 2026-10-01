"""Profile pages and the who's-here list on Buddy Network's page, mixed into
BuddyNetworkPage (page.py). profiles.py says what they show; the server
keeps them (server/profiles.py).

Clicking anyone's name or avatar - in a chat, the who's-here list, a top
buddies box, your own in the sidebar - opens their profile, with what the
name menu used to offer (add as buddy, message, block, ban for staff) in
its Contacting box. A server from before profiles doesn't say it has them
("profiles" in its welcome): there a name opens that menu, as it did.

The who's-here list sits on the right in public rooms, while the server
sends one ("who") and it isn't hidden (its setting, "show_people").

Protocol:
    to the view    who (and the "profile" panel, through panels)
    from the view  profile, people_toggle (and panel_action for "profile")
"""

from __future__ import annotations

from . import profiles, render


class ProfileMixin:
    # Uses from the page: me, client, rooms, room_id, buddy_state, settings,
    # emit, _open_panel, _close_panel, _panel, _push_panels, _in, _online,
    # _am_staff, my_role, social, open_dm, _block, _remove_buddy, ban,
    # _role_setter, _copy, _insert_mention, _person, _notify.

    def _init_profiles(self):
        self.profiles_on = False      # the server has profile pages
        self.who_on = False           # and who's-here lists
        self.who = {}                 # room id -> the server's latest "who"

    def _profiles_welcome(self, limits: dict):
        self.profiles_on = limits.get("profiles") is True
        self.who_on = limits.get("who") is True
        self.who = {}
        self._push_who()

    # ------------------------------------------------------------ profiles

    def open_profile(self, user_id: str) -> bool:
        """Their profile window (over whatever's open). False if this server
        has no profiles."""
        if not (self.profiles_on and self._online() and isinstance(user_id, str) and user_id):
            return False
        self._open_panel("profile", over=True, user=user_id, data=None, editing=False, draft=None, error="")
        self.client.send({"type": "get_profile", "user": user_id})
        return True

    def on_profile(self, payload):
        """A name or avatar in the who's-here list, a buddy row, or your own."""
        user_id = str((payload or {}).get("user") or "")
        if user_id == "me" and self.me:
            user_id = self.me["id"]
        if not self.open_profile(user_id) and user_id and self.me and user_id != self.me["id"]:
            self._user_menu(user_id, (payload or {}).get("x", 0), (payload or {}).get("y", 0))

    def _profile_arrived(self, msg: dict):
        user = msg.get("user") if isinstance(msg.get("user"), dict) else {}
        p = self._panel("profile")
        if p is not None and user.get("id") == p["user"]:
            p["data"] = msg
            p["error"] = ""
            self._push_panels()

    def _refresh_my_profile(self):
        """Your name or avatar changed: your profile window, if it's open, shows it."""
        p = self._panel("profile")
        if p is not None and self.me and p["user"] == self.me["id"] and self._online():
            self.client.send({"type": "get_profile", "user": p["user"]})

    def _profile_saved(self):
        p = self._panel("profile")
        if p is not None and p.get("editing"):
            p["editing"], p["draft"] = False, None
            self._push_panels()
        self.emit("toast", {"text": "Profile saved"})

    def _profile_error(self, msg: dict) -> bool:
        """An error answering a profile request, shown in the window. True if it was one."""
        if msg.get("re") not in ("get_profile", "set_profile", "clear_profile"):
            return False
        p = self._panel("profile")
        if p is not None:
            p["error"] = msg.get("message") or "Something went wrong."
            self._push_panels()
        else:
            self._notify(msg.get("message") or "Something went wrong.")
        return True

    def _profile_actions(self, user_id: str, role: str) -> list[dict]:
        """The Contacting box: what the name menu used to offer."""
        if self.me and user_id == self.me["id"]:
            return [{"id": "edit", "label": "Edit profile", "kind": "accent"},
                    {"id": "avatar", "label": "Change avatar…", "kind": ""},
                    {"id": "copy_id", "label": "Copy my ID", "kind": ""}]
        named = self._online() and bool((self.me or {}).get("name"))
        out = []
        if self._in("buddies", user_id):
            out += [{"id": "message", "label": "Send message", "kind": "accent"},
                    {"id": "remove", "label": "Remove buddy", "kind": ""}]
        elif self._in("incoming", user_id):
            out.append({"id": "accept", "label": "Accept buddy request", "kind": "accent"})
        elif self._in("outgoing", user_id):
            out.append({"id": "cancel_request", "label": "Cancel buddy request", "kind": ""})
        elif not self._in("blocked", user_id):
            out.append({"id": "add", "label": "Add to buddies", "kind": "accent"})
        if named:
            out.append({"id": "mention", "label": "Mention", "kind": ""})
        out.append({"id": "copy_id", "label": "Copy ID", "kind": ""})
        out.append({"id": "unblock", "label": "Unblock", "kind": ""} if self._in("blocked", user_id)
                   else {"id": "block", "label": "Block", "kind": "danger"})
        if self._am_staff() and _rank(role) < _rank(self.my_role()):
            out += [{"id": "ban", "label": "Ban…", "kind": "danger"},
                    {"id": "clear", "label": "Clear profile", "kind": "danger"}]
            if self.my_role() in ("admin", "owner"):
                for new, label in (("user", "Make an ordinary user"), ("mod", "Make mod"), ("admin", "Make admin")):
                    if new != role and _rank(new) < _rank(self.my_role()):
                        out.append({"id": f"role:{new}", "label": label, "kind": ""})
        return out

    def _profile_panel_view(self, p: dict) -> dict | None:
        if not self.me:
            return None
        user = ((p.get("data") or {}).get("user") or {}) if isinstance(p.get("data"), dict) else {}
        actions = self._profile_actions(p["user"], user.get("role", "user")) if user else []
        return profiles.profile_view(p, me_id=self.me["id"], actions=actions,
                                     buddies=self.buddy_state.get("buddies", []))

    def _panel_profile(self, p: dict, action: str, payload: dict):
        user_id = p["user"]
        data = p.get("data") or {}
        person = dict(data.get("user") or self._person(user_id))
        mine = bool(self.me) and user_id == self.me["id"]
        if action == "open":
            other = str(payload.get("user") or "")
            if other and other != user_id:
                self.open_profile(other)
        elif action == "edit" and mine and data:
            p["editing"], p["error"] = True, ""
            p["draft"] = dict(profiles.fields_of(data), top=[t["id"] for t in data.get("top") or []
                                                             if isinstance(t, dict) and t.get("id")])
            self._push_panels()
        elif action == "cancel_edit":
            p["editing"], p["draft"], p["error"] = False, None, ""
            self._push_panels()
        elif action == "save" and mine and p.get("editing"):
            buddy_ids = [b["id"] for b in self.buddy_state.get("buddies", [])]
            draft, problem = profiles.draft_from(payload, buddy_ids)
            p["draft"], p["error"] = draft, problem
            if problem:
                self._push_panels()
            elif not self.client.send({"type": "set_profile", **draft}):
                p["error"] = "Not connected."
                self._push_panels()
        elif action == "avatar" and mine:
            self.open_avatars()
        elif action == "copy_id":
            self._copy(user_id, "Copied their ID" if not mine else "ID copied")
        elif mine:
            return
        elif action == "message" and self._in("buddies", user_id):
            self._close_panel("profile")
            self.open_dm(user_id)
        elif action == "add":
            self.social("buddy_request", user_id)
            self.emit("toast", {"text": "Buddy request sent"})
        elif action == "accept":
            self.social("buddy_accept", user_id)
        elif action == "cancel_request":
            self.social("buddy_cancel", user_id)
        elif action == "remove":
            self._remove_buddy(user_id)
        elif action == "block":
            self._block(user_id)
        elif action == "unblock":
            self.social("unblock", user_id)
        elif action == "mention" and person.get("name"):
            self._close_panel("profile")
            self._insert_mention(person)
        elif action == "ban" and self._am_staff():
            self.ban(person)
        elif action == "clear" and self._am_staff():
            name = render.display_name(person)
            self._confirm("Clear profile", f"Empty what {name} wrote on their profile – the headline, About me "
                          "and the rest? Their theme, mood and top buddies stay. It goes in the admin log.",
                          "Clear profile", lambda: self.admin({"type": "clear_profile", "user": user_id}),
                          danger=True)
        elif action.startswith("role:") and self.my_role() in ("admin", "owner"):
            self._role_setter(user_id, action.partition(":")[2])()

    # ---------------------------------------------------------- who's here

    def _who_arrived(self, msg: dict):
        room_id = msg.get("room")
        if isinstance(room_id, str):
            self.who[room_id] = msg
            if room_id == self.room_id:
                self._push_who()

    def _push_who(self):
        """The list on the right: only in a public room the server sends one for."""
        room = self.rooms.get(self.room_id) or {}
        public = room.get("kind") == "system"
        msg = self.who.get(self.room_id) if public and self.who_on and self.me else None
        hidden = {p["id"] for p in self.buddy_state.get("blocked", [])}
        view = profiles.who_view(msg, hidden=hidden, me_id=(self.me or {}).get("id", "")) if msg else None
        self.emit("who", {"available": msg is not None, "open": bool(self.settings.get("show_people", True)),
                          "profiles": self.profiles_on, **(view or {"people": [], "count": ""})})

    def on_people_toggle(self, _payload=None):
        self.settings["show_people"] = not self.settings.get("show_people", True)
        self.settings.save()
        self._push_who()


def _rank(role: str) -> int:
    return {"user": 0, "mod": 1, "admin": 2, "owner": 3}.get(role, 0)
