#!/usr/bin/env python3
"""
Buddy Network: a text chat between Buddy users. A web page
(core/web_page.py): the view is web/index.html + network.js.

Off until the user turns it on and accepts the rules - until then Buddy
never connects anywhere for it. Once on, it stays connected while Buddy
runs (in the tray too), whichever page is showing.

Rooms: the Global and Help rooms, choosing a name, sending, deleting your
own messages, 30 days of history. Rooms users make (up to 3 each, closed
after 30 days without messages), found through Browse and kept in the
sidebar - "Your rooms" from the server, "Saved" in this PC's settings (per
server address), so the server never holds a list of what you follow.
The Buddy System: buddies (with online dots and unread counts), direct
messages with them only, blocking (their messages are hidden here too),
appear offline, tray notifications, the recovery code and deleting the
account. Admin tools for mods, admins and the owner (roles come from the
server; the Admin panel in panels.py). Everyone gets "report" on other
people's messages. Every link opens through a warning; messages that look
like they hold private details (or your own API key, which is never sent)
are checked before sending - see safety.py. Direct messages are
end-to-end encrypted (e2e.py), with a safety code per DM and a warning when
keys change; any chat can be exported (export.py), and DMs kept on this PC
(archive.py). Replies, editing, @mentions, unread counts and muting,
search, slow mode (staff) and generated avatars (avatars.py). Images -
picked, pasted or dropped, shrunk on this PC, kept on the server for a
week, encrypted in DMs (attachments.py, images.py). GIFs stay animated,
and GIF search finds them on GIPHY through the server (gif_search.py).

How the web page stays safe: the messages are drawn by render.room_html,
which escapes everything anyone typed; its links are "bn-link:<n>" into a
table kept here, never URLs, and always open through the link warning. The
page's Content-Security-Policy allows only Buddy's own scripts and local or
data: images, so nothing in a message could run or fetch anything. Avatars
are inline SVG made here (web_view.py). Everything else the page shows -
names, topics, buddy lists - is drawn with textContent.

Questions are asked in the page (_ask: the view answers with "answer").
The account, safety code, avatar,
saved chats, transfer, ban and admin windows are modals in the page too:
panels.py says what each shows, and the page keeps a stack of the open
ones ("panels") so Ban opens over Admin and Avatars over Account.

The server is server/ in this repo. For testing, run `python -m server`
and leave the address at DEFAULT_SERVER_URL.

Protocol:
    to the view    state, sidebar, room, messages, compose, notice, people,
                   search, buddies, found_rooms, ask, menu, alert, toast,
                   panels (and attachments.py's and gif_search.py's)
    from the view  turn_on, turn_off, import_transfer, open, anchor, send,
                   cancel_compose, answer, menu_pick, chat_menu, sidebar_menu,
                   new_room, browse, find_rooms, open_found, buddies, social,
                   add_buddy, appear_offline, account, admin, rules,
                   open_search, search, close_search, safety_code,
                   panel_action, panel_close
"""

import os
import time

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog

from core.buddy_server import DEFAULT_SERVER_URL, OLD_TEST_DEFAULT
from core.i18n import tr, tr_filter
from core.web_page import WebToolPage

from . import (archive, avatars, dialogs, e2e, export, mentions, panels, render, safety, transfer,
               web_view)
from .attachments import ImageMixin
from .gif_search import GifSearchMixin
from .client import (CONNECTING, MAX_MESSAGE_CHARS, OFF, ONLINE, PROTOCOL_VERSION, WAITING, NetworkClient,
                     missing_support)
from .identity import IdentityStore
from .settings_panel import NetworkSettingsMixin

# server_url "" = the default server (DEFAULT_SERVER_URL), whatever it is at
# the time - only an address the user typed in Settings is kept.
DEFAULTS = {"enabled": False, "rules_version": 0, "server_url": "", "room": "global",
            "saved_rooms": {},   # server address -> [room id], in the order they were saved
            "notify_dms": True,  # tray notifications for DMs and buddy requests
            "keep_dms": False,   # keep a copy of DMs on this PC (archive.py)
            "last_seen": {},     # server address -> {room id: the newest message id seen there}
            "muted": {}}         # server address -> [room id]: no unread counts or tray notices
SLOW_LABELS = [("Off", 0), ("10 seconds", 10), ("30 seconds", 30), ("1 minute", 60), ("5 minutes", 300)]
NOT_SEEN = 2 ** 53   # a room never opened: watch it for new messages, but count none from before
EMPTY_BUDDIES = {"buddies": [], "incoming": [], "outgoing": [], "blocked": [], "appear_offline": False,
                 "my_keys": []}
EXPORT_MAX = 20000   # messages in one export
# The server's roles, lowest first (server/admin.py ROLES).
ROLE_RANK = {"user": 0, "mod": 1, "admin": 2, "owner": 3}
ROLE_NOTICES = {"user": "You're now an ordinary user on Buddy Network.",
                "mod": "You're now a mod on Buddy Network.",
                "admin": "You're now an admin on Buddy Network.",
                "owner": "You're now the owner on Buddy Network."}
ROLE_ACTIONS = {"user": "Make an ordinary user", "mod": "Make mod", "admin": "Make admin"}
EXPORT_DM_WARNING = (
    "The file will hold these direct messages as plain, readable text – they're only end-to-end "
    "encrypted inside Buddy Network. Anyone who gets the file can read them, so keep it somewhere "
    "private.\n\nExport anyway?")
COUNTER_FROM = 1500   # the character counter appears past this


def dm_room_id(me: str, other: str) -> str:
    """Same as the server's (server/store.py): the pair's ids, sorted."""
    a, b = sorted((me, other))
    return f"dm-{a}-{b}"


class BuddyNetworkPage(NetworkSettingsMixin, ImageMixin, GifSearchMixin, WebToolPage):
    tool_id = "buddy_network"
    display_name = "Buddy Network"
    category = ""   # its own group at the bottom of the rail, under a plain line
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    file_drops = True   # a picture dragged in goes in the composer (attachments.py)

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, dict(DEFAULTS))
        if not self.settings.get("server_url_follows_default"):
            # Once only: an address saved back when the default was the
            # local test server (and every save wrote it) wasn't chosen -
            # follow the live default from now on. One typed in later stays.
            if self.settings.get("server_url") in (OLD_TEST_DEFAULT, DEFAULT_SERVER_URL):
                self.settings["server_url"] = ""
            self.settings["server_url_follows_default"] = True
            self.settings.save()
        # Same folder ToolSettings uses for this tool's settings.json.
        folder = self._data_folder()
        self.identity = IdentityStore(folder)
        self.keystore = e2e.KeyStore(folder)
        self.archive = archive.ChatArchive(os.path.join(folder, archive.FOLDER))
        self._load_warnings = list(self.identity.warnings) + list(self.keystore.warnings)
        self.keystore.warnings.clear()

        self._unsupported = missing_support()
        self.client = None if self._unsupported else NetworkClient(self)
        if self.client is not None:
            self.client.state_changed.connect(self._on_state_changed)
            self.client.received.connect(self._on_received)

        self.me = None                 # {"id", "tag", "name"} once welcomed
        self.rooms = {}                # id -> every room this session knows about
        self.system_ids = []           # Global, Help, in the server's order
        self._room_draft = ("", "")    # a new room's name/topic, kept if the server says no
        self._room_error = ""
        self._creating = False
        self._browsing = False         # the Browse window is open
        self._found = {}               # id -> a room the last search found
        self.room_id = self.settings.get("room") or "global"
        self.messages = {}             # id -> message, current room only, oldest first
        self.more = False              # older messages exist on the server
        self.history_days = 30
        self._links = []               # render.room_html's link table
        self._pending = {}             # nonce -> text, until the server echoes it
        self._nonce = 0
        self._name_asking = False
        self._name_error = ""
        self.buddy_state = dict(EMPTY_BUDDIES)
        self.unread = {}               # room id -> messages not read yet
        self._seen_incoming = None     # buddy requests already known (None: not yet this session)
        self._restore_dm = None        # the DM that was open last time, reopened once buddies arrive
        self._opening_dm = None        # a buddy's id whose DM was asked for
        self._trying_code = None       # a recovery code being tried (the saved identity kept until it works)
        self.reports_waiting = 0
        self.reply_to = None           # the message the composer is replying to
        self.editing = None            # the id of your message the composer is changing
        self.mentioned = set()         # rooms where someone mentioned you since you looked
        self.saved_avatars = []        # avatar seeds you've kept (up to 6, with the account)
        self._panels = []              # the open windows, bottom first: {"kind", ...their state}
        self.device = None             # this PC's e2e.DeviceKey, once signed in
        self._readers = []             # it, then older keys kept for reading (KeyStore.readers)
        self._salts = {}               # each DM's random salt -> its id: a repeat is a replayed message
        self._export = None            # an export collecting pages from the server
        self._watched = None
        self._seen_timer = False
        self._status = ("", "")
        self._notice = ("", "")
        self.search_open = False
        self.search_query = ""
        self._asks = {}                # id -> what to do with the answer
        self._ask_id = 0
        self._menu = {}                # item id -> what it does, for the menu on screen
        self._init_images()
        self._init_gifs()

        if self._unsupported:
            self._status = (self._unsupported, "danger")
        elif self.settings.get("enabled") and self.settings.get("rules_version", 0) >= dialogs.RULES_VERSION:
            self._connect()

    def _data_folder(self):
        return os.path.join(os.path.expanduser("~"), f".{self.tool_id}")

    def web_ready(self):
        self._push_state()
        self._push_sidebar(watch=False)
        self._push_room()
        self._render(to_bottom=True)
        self._push_compose()
        self._push_people()
        self.emit("notice", {"text": self._notice[0], "tone": self._notice[1]})
        self.emit("search", {"open": self.search_open, "query": self.search_query})
        self._push_buddies()
        self._push_panels()
        self._push_images()
        self._push_attachment()

    # --------------------------------------------------------- lifecycle

    def on_shown(self):
        if self._load_warnings:
            warnings, self._load_warnings = self._load_warnings, []
            self._alert(self.display_name, "\n\n".join(warnings))

    def on_theme_changed(self):
        super().on_theme_changed()
        if self.client is not None:
            self._render()   # the action shades are mixed from the theme

    def on_app_quitting(self):
        if self.client is not None:
            self.client.stop()
        self._stop_shrinking()
        self.settings.save()   # the last messages seen, if not saved yet

    # ----------------------------------------------------- asking the view

    def _ask(self, kind: str, payload: dict, then, cancelled=None):
        """Puts a question in the page; then(value) runs if it's answered,
        cancelled() if it's closed. kind: choice, prompt, name, room, link,
        rules, select."""
        self._ask_id += 1
        self._asks[self._ask_id] = (then, cancelled)
        self.emit("ask", {"id": self._ask_id, "kind": kind, **payload})

    def on_answer(self, payload):
        payload = payload or {}
        then, cancelled = self._asks.pop(payload.get("id"), (None, None))
        if payload.get("ok") and then is not None:
            then(payload.get("value"))
        elif not payload.get("ok") and cancelled is not None:
            cancelled()

    def _choice(self, title, text, buttons, then, cancel="Cancel"):
        """buttons: [(id, label, kind)] - then(id) with the one pressed."""
        self._ask("choice", {"title": title, "text": text, "cancel": cancel,
                             "buttons": [{"id": b, "label": label, "kind": kind} for b, label, kind in buttons]},
                  then)

    def _confirm(self, title, text, ok, then, danger=False):
        self._choice(title, text, [("ok", ok, "danger" if danger else "accent")], lambda _v: then())

    def _prompt(self, title, text, then, value="", maxlength=300, placeholder="", ok="Save", multiline=False,
                password=False, danger=False):
        self._ask("prompt", {"title": title, "text": text, "value": value, "maxlength": maxlength,
                             "placeholder": placeholder, "ok": ok, "multiline": multiline,
                             "password": password, "danger": danger},
                  lambda v: then(str(v or "")))

    def _alert(self, title, text):
        self.emit("alert", {"title": title, "text": text})

    def _show_menu(self, items, x=0, y=0):
        """items: [(label, action, {"enabled", "danger"})] or None for a line."""
        self._menu = {}
        out = []
        for index, item in enumerate(items):
            if item is None:
                out.append({"sep": True})
                continue
            label, action, *rest = item
            opts = rest[0] if rest else {}
            self._menu[str(index)] = action
            out.append({"id": str(index), "label": label, "enabled": opts.get("enabled", action is not None),
                        "danger": bool(opts.get("danger"))})
        self.emit("menu", {"items": out, "x": x, "y": y})

    def on_menu_pick(self, payload):
        action = self._menu.pop(str((payload or {}).get("id")), None)
        self._menu = {}
        if action is not None:
            action()

    # ----------------------------------------------------------- on / off

    def on_turn_on(self, _payload=None):
        if self.client is None:
            return
        if self.settings.get("rules_version", 0) < dialogs.RULES_VERSION:
            self._ask("rules", {"html": dialogs.RULES_HTML, "accept": True}, lambda _v: self._accepted_rules())
            return
        self._accepted_rules()

    def _accepted_rules(self):
        self.settings["rules_version"] = dialogs.RULES_VERSION
        self.settings["enabled"] = True
        self.settings.save()
        self._connect()
        self._push_state()

    def on_turn_off(self, _payload=None):
        if self.client is None:
            return
        self.settings["enabled"] = False
        self.settings.save()
        self.client.stop()
        self._set_status("")
        self.me, self.rooms, self.system_ids = None, {}, []
        self.buddy_state, self.unread, self._seen_incoming = dict(EMPTY_BUDDIES), {}, None
        self._trying_code = None
        self.device, self._readers, self._export = None, [], None
        self._drop_attachment()
        self.image_limits = None
        self._reset_images()
        self._push_attachment()
        self.reply_to = self.editing = self._watched = None
        self.mentioned = set()
        self._push_compose()
        self._close_search()
        self.settings.save()   # the last messages seen, if not saved yet
        self._clear_room()
        self._push_sidebar(watch=False)
        self._push_room()
        self._push_buddies()
        self._panels = []
        self._push_panels()
        self._push_state()

    def on_rules(self, _payload=None):
        self._ask("rules", {"html": dialogs.RULES_HTML, "accept": False}, lambda _v: None)

    def _server_url(self) -> str:
        return (self.settings.get("server_url") or DEFAULT_SERVER_URL).strip()

    server_url = _server_url   # for the dialogs

    def _connect(self):
        url = self._server_url()
        problem = safety.check_server_url(url)
        if problem:
            self.client.stop()
            self._set_status(f"Can't connect: {problem}", "danger")
            return
        self.client.start(url, self._hello)

    def _hello(self) -> dict:
        hello = {"type": "hello", "v": PROTOCOL_VERSION}
        token = self._trying_code or self.identity.token(self._server_url())
        if token:
            hello["token"] = token
        return hello

    # ------------------------------------------------------------ status

    def _color(self, token: str) -> str:
        """For the Settings dialog's section (still Qt)."""
        tokens = self.host.theme_tokens()
        fallback = {"warning": "#E0A030", "danger": "#F2B8B5", "success": "#A3D5AD"}
        return tokens.get(token) or fallback.get(token, "")

    def _set_status(self, text: str, tone: str = ""):
        self._status = (text, tone)
        self._push_state()

    def _on_state_changed(self, state: str, detail: str):
        if state != ONLINE:
            self._reset_images()
            self._reset_gifs()
        if state == ONLINE:
            self._set_status("Online", "success")
        elif state == CONNECTING:
            self._set_status("Connecting…")
        elif state == WAITING:
            self._set_status(f"Offline: {detail}", "danger")
        elif state == OFF and not self.settings.get("enabled"):
            self._set_status("")
        self._push_state()

    def _online(self) -> bool:
        return self.client is not None and self.client.state == ONLINE

    def _push_state(self):
        online = self._online()
        named = bool(self.me and self.me.get("name"))
        if self._unsupported:
            mode = "unsupported"
        elif self.settings.get("enabled") and self.settings.get("rules_version", 0) >= dialogs.RULES_VERSION:
            mode = "chat"
        else:
            mode = "off"
        if not online:
            placeholder = "Not connected"
        elif not named:
            placeholder = "Choose a name to start chatting"
        else:
            encrypted = " (end-to-end encrypted)" if self._dm_other_id() else ""
            placeholder = f"Message {self._room_name()}{encrypted}"
        me = None
        if self.me:
            me = {"id": self.me["id"], "name": self.me.get("name") or "", "tag": self.me.get("tag", ""),
                  "label": render.display_name(self.me) if named else f"No name yet #{self.me['tag']}",
                  "avatar": web_view.avatar_url(avatars.key_of(self.me)), "role": self.my_role()}
        self.emit("state", {
            "mode": mode, "online": online, "named": named, "can_send": online and named,
            "status": {"text": self._status[0], "tone": self._status[1]},
            "placeholder": placeholder, "me": me,
            "appear_offline": bool(self.buddy_state.get("appear_offline")),
            "buddies_waiting": len(self.buddy_state.get("incoming", [])),
            "staff": self._am_staff(), "reports": self.reports_waiting,
            "max_chars": MAX_MESSAGE_CHARS, "counter_from": COUNTER_FROM,
            "images": self.images_allowed(), "gifs": self.gifs_allowed(),
        })

    def _notify(self, text: str, tone: str = "danger"):
        self._notice = (text, tone if text else "")
        self.emit("notice", {"text": text, "tone": self._notice[1]})

    # ------------------------------------------------------------- rooms

    def _saved_ids(self) -> list[str]:
        saved = self.settings.get("saved_rooms")
        ids = saved.get(self._server_url(), []) if isinstance(saved, dict) else []
        return [i for i in ids if isinstance(i, str)]

    def _set_saved(self, ids):
        saved = dict(self.settings.get("saved_rooms") or {})
        saved[self._server_url()] = list(dict.fromkeys(ids))
        self.settings["saved_rooms"] = saved
        self.settings.save()

    def _is_mine(self, room) -> bool:
        return bool(room and self.me and (room.get("owner") or {}).get("id") == self.me["id"])

    def _room_name(self) -> str:
        room = self.rooms.get(self.room_id)
        if not room:
            return ""
        return f"@{room['name']}" if room["kind"] == "dm" else f"#{room['name']}"

    def _push_sidebar(self, watch=True):
        if not self.me:
            self.emit("sidebar", [])
            return
        mine = [r for r in self.rooms.values() if self._is_mine(r) and r["kind"] != "dm"]
        # A saved room the owner has since made public is listed with Global and Help instead.
        saved = [self.rooms[i] for i in self._saved_ids()
                 if i in self.rooms and not self._is_mine(self.rooms[i]) and i not in self.system_ids]
        sections = web_view.sidebar(
            system=[self.rooms[i] for i in self.system_ids if i in self.rooms], mine=mine, saved=saved,
            buddies=self.buddy_state.get("buddies", []), me_id=self.me["id"], current=self.room_id,
            unread=self.unread, mentioned=self.mentioned, muted=self._is_muted, dm_id=dm_room_id)
        self.emit("sidebar", sections)
        if watch:
            self._maybe_watch(item["room"] for s in sections for item in s["items"] if item["room"])

    def _clear_room(self):
        self.messages, self.more, self._links = {}, False, []
        self._render()

    def _push_room(self):
        room = self.rooms.get(self.room_id)
        if not room or not self.me:
            self.emit("room", None)
            return
        dm = room["kind"] == "dm"
        banner, tone = "", ""
        facts = []
        if dm:
            other = room["other"]
            title = render.display_name(other)
            _code, verified, changed = self.safety_info(other["id"])
            topic = f"End-to-end encrypted – only you and {other.get('name') or 'they'} can read these."
            facts.append("Safety code checked" if verified else "Check the safety code to be sure nobody's in between")
            if changed:
                banner, tone = (f"The encryption keys in this chat changed – {other.get('name') or 'they'} "
                                "(or you) set up another PC or reinstalled Buddy, or someone is intercepting. "
                                "Check the safety code before sharing anything private.", "warn")
            avatar = web_view.avatar_url(avatars.key_of(other))
        else:
            title = room["name"]
            topic = room.get("topic", "")
            if room["kind"] == "user" and room.get("owner"):
                facts.append(f"Made by {render.display_name(room['owner'])}")
            if room.get("slow"):
                facts.append(f"Slow mode: one message every {self._slow_label(room['slow'])}")
            if room.get("made_public"):
                facts.append("Public – in everyone's room list")
            elif room.get("permanent"):
                facts.append("Kept permanently")
            if room.get("announcement"):
                banner, tone = room["announcement"], "pin"
            avatar = None
        if self._is_muted(room["id"]):
            facts.append("Muted")
        self.emit("room", {"id": room["id"], "kind": room["kind"], "title": title, "topic": topic,
                           "facts": facts, "banner": banner, "tone": tone, "avatar": avatar,
                           "verified": dm and self.safety_info(room["other"]["id"])[1]})

    def _dm_other_id(self) -> str | None:
        room = self.rooms.get(self.room_id)
        return room["other"]["id"] if room and room["kind"] == "dm" else None

    def _join_current(self):
        # A saved room may not be described yet (get_rooms is on its way):
        # join it anyway - the server says no_room if it's gone.
        if self.room_id not in self.rooms and self.room_id not in self._saved_ids():
            self.room_id = self.system_ids[0] if self.system_ids else "global"
        self._clear_room()
        self._push_room()
        self.client.send({"type": "join", "room": self.room_id})
        self._push_state()

    def _switch_room(self, room_id: str):
        self.unread.pop(room_id, None)
        self.mentioned.discard(room_id)
        if room_id != self.room_id:
            self.client.send({"type": "leave", "room": self.room_id})
            self.room_id = room_id
            self.settings["room"] = room_id
            self.settings.save()
            self._notify("")
            self.on_cancel_compose()
            if self.attachment is not None or self._preparing:
                # Never carried into another chat: a picture meant for one
                # person mustn't end up in a public room.
                self._drop_attachment()
                self._push_attachment()
            self._close_search()
            self._join_current()
            self._push_people()
        self._push_sidebar()

    def on_open(self, payload):
        """A sidebar item: a room id, or "user:<id>" for a buddy's DM."""
        key = str((payload or {}).get("key") or "")
        if not self._online():
            return
        if key.startswith("user:"):
            self.open_dm(key[len("user:"):])
        elif key in self.rooms or key in self._saved_ids():
            self._switch_room(key)

    def _room_gone(self, room_id: str, reason: str):
        room = self.rooms.pop(room_id, None)
        if room_id in self._saved_ids():
            self._set_saved([i for i in self._saved_ids() if i != room_id])
        self.unread.pop(room_id, None)
        if room_id == self.room_id:
            # Whole sentences, so each is translated as one.
            if room and room["kind"] == "dm":
                gone = f"{render.display_name(room['other'])} was no longer on Buddy Network."
            elif room:
                gone = (f"#{room['name']} was deleted by the person who made it." if reason == "deleted"
                        else f"#{room['name']} was closed after 30 days without messages.")
            else:
                gone = ("That room was deleted by the person who made it." if reason == "deleted"
                        else "That room was closed after 30 days without messages.")
            self.room_id = self.system_ids[0] if self.system_ids else "global"
            self.settings["room"] = self.room_id
            self.settings.save()
            self._join_current()
            self._notify(gone, "warning")
        self._push_sidebar()

    def _public_changed(self, room: dict):
        """The owner made a room everyone's (it joins Global and Help in the
        list) or a regular room again. One you're in stays in your list
        either way, saved."""
        self.rooms[room["id"]] = room
        if room["kind"] == "system":
            self.system_ids.append(room["id"])
        else:
            self.system_ids.remove(room["id"])
            if room["id"] == self.room_id and not self._is_mine(room) and room["id"] not in self._saved_ids():
                self._set_saved(self._saved_ids() + [room["id"]])
        self._push_sidebar()
        if room["id"] == self.room_id:
            self._push_room()

    def on_new_room(self, _payload=None):
        if not self._online():
            return
        self._ask("room", {"name": self._room_draft[0], "topic": self._room_draft[1], "error": self._room_error},
                  self._create_room)

    def _create_room(self, value):
        value = value or {}
        name = " ".join(str(value.get("name", "")).split())[:32]
        topic = " ".join(str(value.get("topic", "")).split())[:120]
        self._room_draft = (name, topic)
        if len(name) < 2:
            self._room_error = "Room names are at least 2 characters."
            self.on_new_room()
            return
        self._room_error = ""
        self._creating = True
        self.client.send({"type": "create_room", "name": name, "topic": topic})

    def on_browse(self, _payload=None):
        if self._online():
            self._browsing = True
            self.emit("found_rooms", {"open": True, "rooms": [], "query": "", "permanent_only": False,
                                      "searching": True})
            self.on_find_rooms({"query": "", "permanent_only": False})

    def on_find_rooms(self, payload):
        payload = payload or {}
        if self._browsing:
            self.client.send({"type": "find_rooms", "query": str(payload.get("query", ""))[:100],
                              "permanent_only": bool(payload.get("permanent_only"))})

    def on_open_found(self, payload):
        """A room picked in Browse - taken from what the server sent, never
        from the page."""
        payload = payload or {}
        room = self._found.get(payload.get("id"))
        if payload.get("close"):
            self._browsing = False
        if room is None:
            return
        self._browsing = False
        self.rooms[room["id"]] = room
        if not self._is_mine(room) and room["id"] not in self._saved_ids():
            self._set_saved(self._saved_ids() + [room["id"]])
        self._switch_room(room["id"])

    def _edit_topic(self):
        room = self.rooms.get(self.room_id)
        if not room or not (self._is_mine(room) or self._am_staff()):
            return
        self._prompt("Room topic", "Shown under the room's name (up to 120 characters).",
                     lambda topic: self.client.send({"type": "set_topic", "room": room["id"], "topic": topic}),
                     value=room.get("topic", ""), maxlength=120)

    def _delete_room(self):
        room = self.rooms.get(self.room_id)
        if not room or room["kind"] != "user" or not (self._is_mine(room) or self._am_staff()):
            return
        self._confirm("Delete room", f"Delete #{room['name']} for everyone? All of its messages go too – "
                      "this can't be undone.", "Delete room",
                      lambda: self.client.send({"type": "delete_room", "room": room["id"]}), danger=True)

    def _toggle_permanent(self):
        room = self.rooms.get(self.room_id)
        if not room or room["kind"] != "user" or not (self._is_mine(room) or self._am_staff()):
            return
        self.client.send({"type": "set_permanent", "room": room["id"], "value": not room.get("permanent")})

    def _toggle_public(self):
        """The owner's: a user room into everyone's list, with Global and
        Help - or a room made public back to a regular one (server/core.py
        _set_public)."""
        room = self.rooms.get(self.room_id)
        if not room or not self._am_owner() or not (room["kind"] == "user" or room.get("made_public")):
            return
        send = lambda value: self.admin({"type": "set_public", "room": room["id"], "value": value})  # noqa: E731
        if room["kind"] == "user":
            self._confirm("Make public", f"Put #{room['name']} in everyone's room list, with Global and Help? "
                          "It won't expire, and its maker can't change or delete it any more – you and your "
                          "staff look after it. You can make it a regular room again from this menu.",
                          "Make public", lambda: send(True))
        else:
            self._confirm("Make a regular room", f"Take #{room['name']} out of everyone's room list? It goes "
                          "back to the person who made it, and people who haven't saved it will have to find "
                          "it with Browse again.", "Make regular", lambda: send(False))

    def _rename_room(self):
        room = self.rooms.get(self.room_id)
        if not room or not self._am_staff():
            return
        self._prompt("Rename room", "New name (2-32 characters).",
                     lambda name: name.strip() and self.client.send(
                         {"type": "rename_room", "room": room["id"], "name": name}),
                     value=room["name"], maxlength=32, ok="Rename")

    def _edit_announcement(self):
        room = self.rooms.get(self.room_id)
        if not room or not self._am_staff():
            return
        self._prompt("Announcement", "Pinned at the top of the room for everyone (up to 300 characters). "
                     "Leave it empty to remove it.",
                     lambda text: self.client.send({"type": "set_announcement", "room": room["id"], "text": text}),
                     value=room.get("announcement", ""), maxlength=300, multiline=True)

    def _unsave_room(self):
        room_id = self.room_id
        self._set_saved([i for i in self._saved_ids() if i != room_id])
        self._switch_room(self.system_ids[0] if self.system_ids else "global")

    # ------------------------------------------------------ buddy system

    def _person(self, user_id: str) -> dict:
        state = self.buddy_state
        for group in ("buddies", "incoming", "outgoing", "blocked"):
            for person in state.get(group, []):
                if person["id"] == user_id:
                    return person
        for m in self.messages.values():
            if m["author"].get("id") == user_id:
                return m["author"]
        # The server's rule (server/common.py tag_of): staff IDs start 0000.
        tag = (user_id[:6].lstrip("0") or "0") if user_id.startswith("0000") else user_id[:6]
        return {"id": user_id, "tag": tag, "name": None}

    def _in(self, group: str, user_id: str) -> bool:
        return any(p["id"] == user_id for p in self.buddy_state.get(group, []))

    def social(self, kind: str, user_id: str):
        """One of the server's buddy_* / block / unblock requests."""
        if user_id:
            self.client.send({"type": kind, "user": user_id})

    SOCIAL_KINDS = {"buddy_request", "buddy_accept", "buddy_decline", "buddy_cancel", "buddy_remove",
                    "block", "unblock"}

    def on_social(self, payload):
        payload = payload or {}
        kind, user_id = payload.get("kind"), str(payload.get("user") or "")
        if kind not in self.SOCIAL_KINDS or not user_id:
            return
        self._buddies_error("")
        if kind == "block":
            self._block(user_id)
        elif kind == "buddy_remove":
            self._remove_buddy(user_id)
        else:
            self.social(kind, user_id)

    def on_add_buddy(self, payload):
        user_id = web_view.parse_buddy_id((payload or {}).get("text", ""))
        if not user_id:
            self._buddies_error("That doesn't look like a Buddy Network ID (32 letters and numbers).")
            return
        self._buddies_error("")
        self.social("buddy_request", user_id)
        self.emit("buddies", {**self._buddies_view(), "clear_add": True})

    def _buddies_error(self, text):
        self.emit("buddies_error", text)

    def _buddies_view(self):
        state = self.buddy_state
        return {group: web_view.people_view(state.get(group, []))
                for group in ("buddies", "incoming", "outgoing", "blocked")}

    def _push_buddies(self):
        self.emit("buddies", self._buddies_view())

    def on_buddies(self, _payload=None):
        if self._online():
            self._push_buddies()
            self.emit("buddies_open", True)

    def open_dm(self, user_id: str):
        if not self.me or not user_id:
            return
        room_id = dm_room_id(self.me["id"], user_id)
        if room_id in self.rooms:
            self._switch_room(room_id)
        else:
            self._opening_dm = user_id
            self.client.send({"type": "open_dm", "user": user_id})

    def _block(self, user_id):
        if not user_id:
            return
        name = render.display_name(self._person(user_id))
        self._confirm("Block", f"Block {name}?\n\nThey won't be able to message you or ask to be your buddy, "
                      "and you won't see their messages anywhere. They aren't told. You can unblock them "
                      "under Buddies.", "Block", lambda: self.social("block", user_id), danger=True)

    def _remove_buddy(self, user_id):
        if not user_id:
            return
        name = render.display_name(self._person(user_id))
        self._confirm("Remove buddy", f"Remove {name} from your buddies? You won't be able to message each "
                      "other until you're buddies again.", "Remove",
                      lambda: self.social("buddy_remove", user_id), danger=True)

    def on_appear_offline(self, payload):
        if self._online():
            self.client.send({"type": "set_presence", "offline": bool((payload or {}).get("on"))})

    def _user_menu(self, user_id: str, x=0, y=0):
        items = []
        if self._in("buddies", user_id):
            items += [("Message", lambda: self.open_dm(user_id)),
                      ("Remove buddy", lambda: self._remove_buddy(user_id))]
        elif self._in("incoming", user_id):
            items.append(("Accept buddy request", lambda: self.social("buddy_accept", user_id)))
        elif self._in("outgoing", user_id):
            items.append(("Buddy request sent", None))
        else:
            items.append(("Add as buddy", lambda: self.social("buddy_request", user_id)))
        items.append(None)
        if self._online() and self.me and self.me.get("name"):
            items.append(("Mention", lambda: self._insert_mention(self._person(user_id))))
        items.append(("Copy ID", lambda: self._copy(user_id, "Copied their ID")))
        items.append(("Block", lambda: self._block(user_id), {"danger": True}))
        if self._am_staff():
            person = self._person(user_id)
            role = person.get("role", "user")
            mine = ROLE_RANK[self.my_role()]
            if ROLE_RANK.get(role, 0) < mine:   # the server's rule: bans and roles only reach down
                items += [None, ("Ban…", lambda: self.ban(person), {"danger": True})]
                if self.my_role() in ("admin", "owner"):
                    for new in ("user", "mod", "admin"):
                        if new != role and ROLE_RANK[new] < mine:
                            items.append((ROLE_ACTIONS[new], self._role_setter(user_id, new)))
        self._show_menu(items, x, y)

    def _copy(self, text, toast):
        dialogs.copy_to_clipboard(text)
        self.emit("toast", {"text": toast})

    # ------------------------------------------------------- chat features

    @staticmethod
    def _slow_label(seconds: int) -> str:
        return next((label for label, s in SLOW_LABELS if s == seconds), f"{seconds} seconds")

    def _for_server(self, key: str):
        """This server's part of a per-server setting (last_seen, muted)."""
        whole = self.settings.get(key)
        return whole.get(self._server_url()) if isinstance(whole, dict) else None

    def _is_muted(self, room_id: str) -> bool:
        muted = self._for_server("muted")
        return isinstance(muted, list) and room_id in muted

    def _set_muted(self, room_id: str, on: bool):
        whole = dict(self.settings.get("muted") or {})
        ids = [i for i in (whole.get(self._server_url()) or []) if i != room_id]
        whole[self._server_url()] = ids + ([room_id] if on else [])
        self.settings["muted"] = whole
        self.settings.save()
        self._push_sidebar()
        self._push_room()

    def _mark_seen(self, room_id: str, message_ids):
        """Remembers the newest message seen in a room - where its unread
        count starts next time. Saved a few seconds later, not per message."""
        newest = max(message_ids, default=None)
        if newest is None:
            return
        whole = dict(self.settings.get("last_seen") or {})
        seen = dict(whole.get(self._server_url()) or {})
        if seen.get(room_id, 0) >= newest:
            return
        seen[room_id] = newest
        whole[self._server_url()] = seen
        self.settings["last_seen"] = whole
        if not self._seen_timer:
            self._seen_timer = True
            QTimer.singleShot(5000, self._save_seen)

    def _save_seen(self):
        self._seen_timer = False
        self.settings.save()

    def _maybe_watch(self, room_ids):
        """Asks the server for unread counts of everything in the sidebar -
        again only when that list changes."""
        ids = frozenset(room_ids)
        if not self.me or not self._online() or ids == self._watched:
            return
        self._watched = ids
        seen = self._for_server("last_seen") or {}
        self.client.send({"type": "watch", "rooms": {r: int(seen.get(r, NOT_SEEN)) for r in ids}})

    def _mention_people(self) -> list[dict]:
        """Who the message box offers after "@": buddies, and whoever has
        spoken in this chat."""
        me = (self.me or {}).get("id")
        people = list(self.buddy_state.get("buddies", []))
        for m in sorted(self.messages.values(), key=lambda m: -m["id"]):
            people.append(m["author"])
            if m.get("reply", {}).get("author"):
                people.append(m["reply"]["author"])
        blocked = {p["id"] for p in self.buddy_state.get("blocked", [])}
        seen, out = set(), []
        for p in people:
            if p.get("id") and p.get("name") and p["id"] != me and p["id"] not in blocked and p["id"] not in seen:
                seen.add(p["id"])
                out.append(p)
        return out

    def _push_people(self):
        people = self._mention_people()
        self.emit("people", [{**v, "token": mentions.token(p)} for p, v in zip(people, web_view.people_view(people))])

    def on_chat_menu(self, payload):
        payload = payload or {}
        room = self.rooms.get(self.room_id)
        if not room:
            return
        muted = self._is_muted(room["id"])
        items = [("Search   Ctrl+F", self.on_open_search), ("Export…", self._export_chat),
                 ("Unmute" if muted else "Mute", lambda: self._set_muted(room["id"], not muted))]
        if room["kind"] == "dm":
            items += [None, ("Safety code…", self.on_safety_code),
                      ("Remove buddy", lambda: self._remove_buddy(self._dm_other_id()), {"danger": True}),
                      ("Block", lambda: self._block(self._dm_other_id()), {"danger": True})]
        else:
            mine, staff, user_room = self._is_mine(room), self._am_staff(), room["kind"] == "user"
            if mine or staff:
                items += [None, ("Edit topic…", self._edit_topic)]
            if staff:
                items += [("Slow mode…", self._set_slow_mode), ("Announcement…", self._edit_announcement)]
                if user_room:
                    items.append(("Rename…", self._rename_room))
            if user_room and (mine or staff):
                permanent = bool(room.get("permanent"))
                items += [("Let this room expire normally" if permanent else "Keep this room permanently",
                           self._toggle_permanent),
                          ("Delete room…", self._delete_room, {"danger": True})]
            if self._am_owner() and (user_room or room.get("made_public")):
                items += [None, ("Make public for everyone…" if user_room else "Make a regular room again…",
                                 self._toggle_public)]
            if user_room and not mine and room["id"] in self._saved_ids():
                items += [None, ("Remove from list", self._unsave_room)]
        self._show_menu(items, payload.get("x", 0), payload.get("y", 0))

    def on_sidebar_menu(self, payload):
        payload = payload or {}
        key = str(payload.get("key") or "")
        if not key or not self.me:
            return
        room_id = dm_room_id(self.me["id"], key[5:]) if key.startswith("user:") else key
        muted = self._is_muted(room_id)
        items = [("Unmute" if muted else "Mute", lambda: self._set_muted(room_id, not muted))]
        if key.startswith("user:"):
            items += [None, ("Remove buddy", lambda: self._remove_buddy(key[5:]), {"danger": True})]
        self._show_menu(items, payload.get("x", 0), payload.get("y", 0))

    def _insert_mention(self, person: dict):
        if person.get("name"):
            self.emit("compose_insert", mentions.token(person) + " ")

    def _set_slow_mode(self):
        room = self.rooms.get(self.room_id)
        if not room or not self._am_staff():
            return
        self._ask("select", {"title": "Slow mode", "text": "How often each person can post here "
                             "(mods and admins aren't slowed).",
                             "options": [{"id": str(s), "label": label} for label, s in SLOW_LABELS],
                             "value": str(room.get("slow", 0))},
                  lambda v: str(v).isdigit() and self.admin(
                      {"type": "set_slow", "room": room["id"], "seconds": int(v)}))

    def on_open_search(self, _payload=None):
        if not self.rooms.get(self.room_id):
            return
        self.search_open = True
        self.emit("search", {"open": True, "query": self.search_query, "focus": True})

    def on_search(self, payload):
        self.search_query = str((payload or {}).get("query", ""))[:200]
        self._render(to_bottom=True)

    def on_close_search(self, _payload=None):
        self._close_search()

    def _close_search(self):
        if self.search_open or self.search_query:
            self.search_open, self.search_query = False, ""
            self.emit("search", {"open": False, "query": ""})
            self._render(to_bottom=True)

    def _start_reply(self, message_id: int):
        m = self.messages.get(message_id)
        if not m or m["deleted"] or m.get("unreadable"):
            return
        self.editing, self.reply_to = None, m
        self._push_compose(focus=True)

    def _start_edit(self, message_id: int):
        m = self.messages.get(message_id)
        if not m or m["deleted"] or m.get("unreadable") or m["author"].get("id") != (self.me or {}).get("id"):
            return
        self.reply_to, self.editing = None, message_id
        self._push_compose(text=m["text"], focus=True)

    def on_cancel_compose(self, _payload=None):
        clear = self.editing is not None
        self.reply_to = self.editing = None
        self._push_compose(text="" if clear else None)

    def _push_compose(self, text=None, focus=False):
        if self.editing is not None:
            mode, label = "edit", "Editing your message – Enter saves it."
        elif self.reply_to is not None:
            quoted = render.quote_text({"id": self.reply_to["id"], "text": self.reply_to["text"],
                                        "image": bool(self.reply_to.get("image"))}, self.messages)
            mode, label = "reply", f"Replying to {render.display_name(self.reply_to['author'])}: {quoted}"
        else:
            mode, label = None, ""
        # picture: the message being edited has an image, so its text may be emptied.
        picture = self.editing is not None and bool((self.messages.get(self.editing) or {}).get("image"))
        self.emit("compose", {"mode": mode, "label": label, "text": text, "focus": focus, "picture": picture})

    # ---------------------------------------------------------- encryption

    def _current_keys(self, user_id: str) -> dict[str, bytes]:
        """Someone's PCs as the server lists them now (device id -> key).
        Yours always include this PC."""
        if self.me and user_id == self.me["id"]:
            keys = e2e.keys_by_device(self.buddy_state.get("my_keys"))
            if self.device is not None:
                keys[self.device.id] = self.device.public
            return keys
        buddy = next((b for b in self.buddy_state.get("buddies", []) if b["id"] == user_id), None)
        return e2e.keys_by_device((buddy or {}).get("keys"))

    def _remember_keys(self):
        if not self.me:
            return
        url = self._server_url()
        for user_id in [self.me["id"]] + [b["id"] for b in self.buddy_state.get("buddies", [])]:
            self.keystore.remember(url, user_id, self._current_keys(user_id))

    def safety_info(self, other_id: str) -> tuple[str, bool, bool]:
        """(safety code, checked with them, keys changed since last seen)."""
        if not self.me:
            return "", False, False
        url, me = self._server_url(), self.me["id"]
        mine, theirs = self._current_keys(me), self._current_keys(other_id)
        code = e2e.safety_code(me, mine.values(), other_id, theirs.values()) if mine and theirs else ""
        verified = bool(code) and self.keystore.verified(url, other_id) == code
        changed = self.keystore.keys_changed(url, me) or self.keystore.keys_changed(url, other_id)
        return code, verified, changed

    def accept_keys(self, other_id: str):
        url = self._server_url()
        for user_id in (self.me["id"], other_id):
            self.keystore.accept(url, user_id, self._current_keys(user_id))
        self._push_room()

    def mark_verified(self, other_id: str, code: str):
        self.keystore.set_verified(self._server_url(), other_id, code)
        self._push_room()

    def my_device_count(self) -> int:
        return len(self._current_keys(self.me["id"])) if self.me else 0

    def on_safety_code(self, _payload=None):
        room = self.rooms.get(self.room_id)
        if room and room["kind"] == "dm":
            self._open_panel("safety", other=room["other"])

    def _open_message(self, m: dict) -> dict:
        """A message from the server, with a DM's text decrypted - or marked
        unreadable if this PC can't open it (e2e.open_message)."""
        return e2e.open_message(m, readers=self._readers, keystore=self.keystore, url=self._server_url(),
                                salts=self._salts)

    def _recipients(self, room: dict) -> dict[str, bytes] | None:
        """Every PC a DM is encrypted for - theirs and yours - or None (said
        why) if it can't be yet."""
        other = room["other"]
        theirs = self._current_keys(other["id"])
        if self.device is None:
            self._notify("Still setting up encryption on this PC – try again in a moment.")
            return None
        if not theirs:
            self._notify(f"{render.display_name(other)} needs to open Buddy Network on an up-to-date "
                         "Buddy before you can message them – direct messages are end-to-end encrypted.")
            return None
        return {**theirs, **self._current_keys(self.me["id"])}

    def _encrypt_for(self, room: dict, text: str) -> dict | None:
        recipients = self._recipients(room)
        if recipients is None:
            return None
        return e2e.encrypt(text, room=room["id"], sender=self.me["id"], me=self.device, recipients=recipients)

    # -------------------------------------------------------- saved copies

    def _keeping(self, room_id: str) -> bool:
        return bool(self.settings.get("keep_dms")) and room_id.startswith("dm-") and bool(self.me)

    def _keep(self, room_id: str, messages):
        if not self._keeping(room_id):
            return
        # Not one from an unaccepted PC: saved, it would lose its warning.
        messages = [m for m in messages if not m.get("unverified") and not m.get("replayed")]
        room = self.rooms.get(room_id) or {}
        other = room.get("other") or self._person(next(
            (i for i in room_id.split("-")[1:] if i != self.me["id"]), ""))
        try:
            self.archive.save(self._server_url(), self.me["id"], room_id, other, messages)
        except (OSError, e2e.CryptoError) as exc:
            self._notify(f"Couldn't save a copy of this message on this PC: {exc}")

    def _saved_before(self) -> list[dict]:
        """This DM's saved messages older than any loaded - the ones the
        server no longer has. Only once the server has nothing older."""
        if self.more or not self._keeping(self.room_id):
            return []
        oldest = min(self.messages) if self.messages else None
        # saved_only: the server has forgotten it, so it can't be edited, deleted or reported.
        return [dict(m, saved_only=True) for m in self.archive.messages(self._server_url(), self.me["id"],
                                                                        self.room_id)
                if oldest is None or m["id"] < oldest]

    # ------------------------------------------------------------- export

    def _export_chat(self):
        room = self.rooms.get(self.room_id)
        if not room or not self._online():
            return
        if self._export is not None:
            self._notify("Already exporting a chat – wait for it to finish.", "warning")
            return
        if room["kind"] == "dm":
            self._confirm("Export chat", EXPORT_DM_WARNING, "Export", lambda: self._start_export(room))
        else:
            self._start_export(room)

    def _start_export(self, room):
        title = self._room_name()
        path = self._ask_export_path(title)
        if not path:
            return
        self._nonce += 1
        self._export = {"room": room["id"], "title": title, "path": path, "nonce": f"export-{self._nonce}",
                        "messages": {}}
        self._notify("Exporting…", "warning")
        self.client.send({"type": "history", "room": room["id"], "nonce": self._export["nonce"]})

    def _ask_export_path(self, title: str) -> str:
        start = os.path.join(os.path.expanduser("~"), "Documents", export.default_filename(title, time.time()))
        path, chosen = QFileDialog.getSaveFileName(self, tr("Export chat"), start,
                                                   tr_filter("Text file (*.txt);;Web page (*.html)"))
        if path and not os.path.splitext(path)[1]:
            path += ".html" if "html" in chosen else ".txt"
        return path

    def _export_page(self, msg: dict):
        job = self._export
        for m in msg.get("messages", []):
            job["messages"][m["id"]] = self._open_message(m)
        if msg.get("more") and job["messages"] and len(job["messages"]) < EXPORT_MAX:
            self.client.send({"type": "history", "room": job["room"], "before": min(job["messages"]),
                              "nonce": job["nonce"]})
            return
        self._export = None
        got = job["messages"]
        if self._keeping(job["room"]):
            for m in self.archive.messages(self._server_url(), self.me["id"], job["room"]):
                got.setdefault(m["id"], m)
        hidden = {p["id"] for p in self.buddy_state.get("blocked", [])}
        ordered = [got[k] for k in sorted(got) if got[k]["author"].get("id") not in hidden]
        self._notify(*self._write_export(job["path"], job["title"], ordered))

    def _write_export(self, path: str, title: str, messages: list[dict], note: str = "") -> tuple[str, str]:
        """(message, tone) for the result."""
        note = note or f"The server keeps messages for {self.history_days} days."
        text = (export.as_html if path.lower().endswith((".html", ".htm")) else export.as_text)(
            title, messages, time.time(), note)
        try:
            export.write(path, text)
        except OSError as exc:
            return f"Couldn't save the export: {exc}", "danger"
        return f"Exported {len(messages):,} messages to {os.path.basename(path)}.", "success"

    def _export_saved(self, chat: dict, then):
        """Exports a conversation saved on this PC (the Saved chats panel);
        then(text, tone) with how it went."""
        def go():
            title = f"@{chat['other'].get('name') or 'Someone'}"
            path = self._ask_export_path(title)
            if not path:
                return
            messages = self.archive.messages(self._server_url(), chat["me"], chat["room"])
            then(*self._write_export(path, title, messages, "From the copy saved on this PC."))
        self._confirm("Export chat", EXPORT_DM_WARNING, "Export", go)

    # ---------------------------------------------------------- transfers

    def export_transfer(self):
        """Account > Move to another PC: this account, its keys and (if
        wanted) the saved DMs, in one file - password-locked if they chose
        one (transfer.py). The Transfer panel asks for the password."""
        if not self.me:
            return
        chats = self.archive.export_all(self._server_url(), self.me["id"])
        self._open_panel("transfer", over=True, chats=len(chats))

    def _save_transfer(self, password: str, with_chats: bool):
        if not self.me:
            return
        self._close_panel("transfer")
        url = self._server_url()
        name = self.me.get("name") or "Buddy"
        start = os.path.join(os.path.expanduser("~"), "Documents",
                             export.default_filename(name, time.time(), transfer.EXTENSION))
        path, _ = QFileDialog.getSaveFileName(self, tr("Save transfer file"), start,
                                              tr_filter(f"Buddy Network transfer (*{transfer.EXTENSION})"))
        if not path:
            return
        if not path.lower().endswith(transfer.EXTENSION):
            path += transfer.EXTENSION
        chats = self.archive.export_all(url, self.me["id"]) if with_chats else []
        payload = {"server": url, "made": time.time(),
                   "identity": {"id": self.me["id"], "name": self.me.get("name"),
                                "token": self.identity.token(url)},
                   "keys": self.keystore.export_server(url), "chats": chats}
        try:
            export.write(path, transfer.seal(payload, password))
        except (OSError, e2e.CryptoError) as exc:
            self._alert(self.display_name, f"The transfer file couldn't be saved: {exc}")
            return
        how = ("On the other PC, open Buddy Network and choose \"I have a transfer file\" (or Account > "
               "Import a transfer file), then type the password." if password else
               "On the other PC, open Buddy Network and choose \"I have a transfer file\" (or Account > "
               "Import a transfer file).")
        self._alert(self.display_name, f"Saved {os.path.basename(path)}.\n\n{how}\n\nDelete the file once "
                    "you've moved: it's enough to be you and read your messages.")

    def on_import_transfer(self, _payload=None):
        self.import_transfer()

    def import_transfer(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Import transfer file"), os.path.join(os.path.expanduser("~"), "Documents"),
            tr_filter(f"Buddy Network transfer (*{transfer.EXTENSION});;All files (*)"))
        if path:
            self._read_transfer(path)   # first without a password: a file saved without one opens straight away

    def _read_transfer(self, path: str, password: str | None = None):
        try:
            payload = transfer.read(path, password)
        except transfer.NeedsPassword:
            self._ask_transfer_password(path, "")
            return
        except transfer.TransferError as exc:
            if "password" in str(exc):
                self._ask_transfer_password(path, str(exc))
            else:
                self._alert("Import transfer file", str(exc))
            return
        self._confirm_import(payload)

    def _ask_transfer_password(self, path: str, problem: str):
        self._prompt("Import transfer file", (f"{problem}\n\n" if problem else "") + "The password it was saved with:",
                     lambda password: self._read_transfer(path, password), ok="Open", password=True)

    def _confirm_import(self, payload: dict):
        url, who = payload["server"], payload["identity"]
        shown = render.display_name({"id": who["id"], "name": who.get("name"),
                                     "tag": self._person(who["id"])["tag"]})
        question = f"Sign this PC in to Buddy Network as {shown}, with that account's encryption keys?"
        current = self.identity.get(url).get("id")
        if current and current != who["id"]:
            question += ("\n\nThis PC is signed in as a different identity now, and that one will be replaced. "
                         "If you want to keep it, cancel and copy its recovery code first (Account).")
        self._confirm("Import transfer file", question, "Sign in", lambda: self._finish_import(payload, shown))

    def _finish_import(self, payload: dict, shown: str):
        url, who = payload["server"], payload["identity"]
        try:
            self.keystore.import_server(url, payload["keys"])
            self.identity.save(url, who["id"], who["token"])
            chats = payload.get("chats") or []
            new = self.archive.import_all(url, who["id"], chats) if chats else 0
        except (OSError, e2e.CryptoError, ValueError) as exc:
            self._alert(self.display_name, f"The transfer couldn't be finished: {exc}")
            return
        if chats:
            self.settings["keep_dms"] = True   # they kept a copy there: keep one here too
            self.settings.save()
        done = f"Imported {shown}, with {new:,} saved direct messages." if chats else f"Imported {shown}."
        if url != self._server_url():
            done += (f"\n\nIt's for the server {url}, but Buddy Network is set to {self._server_url()} – "
                     "change the server address in Settings to use it.")
        elif not self.settings.get("enabled"):
            done += "\n\nTurn on Buddy Network to sign in."
        self._alert(self.display_name, done + "\n\nYou can delete the transfer file now.")
        if url == self._server_url() and self.settings.get("enabled") and self.client is not None:
            self._connect()   # signs in again, as the imported identity

    # ------------------------------------------------------------- panels
    # Account, Safety code, Avatars, Transfer, Saved chats, Ban and Admin:
    # modals in the page, drawn from panels.py. A stack, so one can open
    # over another (Ban over Admin, Avatars over Account) and closing it goes
    # back. Each entry is {"kind", ...its own state}; everything else they
    # show is read fresh from the page each time they're pushed.

    def _panel(self, kind: str) -> dict | None:
        return next((p for p in self._panels if p["kind"] == kind), None)

    def _open_panel(self, kind: str, over: bool = False, **state):
        """over: on top of what's open, else instead of it."""
        rest = [p for p in self._panels if p["kind"] != kind] if over else []
        self._panels = rest + [{"kind": kind, **state}]
        self._push_panels()

    def _close_panel(self, kind: str):
        self._panels = [p for p in self._panels if p["kind"] != kind]
        self._push_panels()

    def on_panel_close(self, payload):
        self._close_panel(str((payload or {}).get("kind")))

    def _push_panels(self):
        shown = []
        for p in list(self._panels):
            view = self._panel_view(p)
            if view is None:
                self._panels.remove(p)   # it can't be shown any more (signed out, say)
            else:
                shown.append({"kind": p["kind"], **view})
        self.emit("panels", shown)

    def _panel_view(self, p: dict) -> dict | None:
        kind = p["kind"]
        if kind in ("account", "avatars", "transfer") and not self.me:
            return None
        if kind == "account":
            return panels.account(self.me, devices=self.my_device_count(),
                                  code=self.recovery_code() if p.get("show_code") else None,
                                  saved_chats=len(self.archive.conversations(self._server_url())))
        if kind == "safety":
            return panels.safety(p["other"], *self.safety_info(p["other"]["id"]))
        if kind == "avatars":
            return panels.avatar_panel(self.me, self.saved_avatars, p["preview"], p.get("error", ""))
        if kind == "transfer":
            return panels.transfer_panel(p["chats"], p.get("error", ""))
        if kind == "saved":
            p["chats"] = self.archive.conversations(self._server_url())
            return panels.saved_chats(p["chats"], p.get("note", ""), p.get("tone", ""))
        if kind == "ban":
            return panels.ban_panel(p["person"])
        if kind == "admin":
            if not self._am_staff():
                return None
            return {**panels.admin(self.my_role(), p["answers"], p["tab"], p.get("error", "")), "sent": p["sent"]}
        return None

    def on_panel_action(self, payload):
        payload = payload or {}
        p = self._panel(str(payload.get("kind")))
        handler = getattr(self, f"_panel_{p['kind']}", None) if p else None
        if handler is not None:
            handler(p, str(payload.get("action")), payload)

    def _panel_index(self, items: list, payload: dict):
        index = payload.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(items):
            return items[index]
        return None

    def _panel_account(self, p: dict, action: str, payload: dict):
        if action == "copy_id":
            dialogs.copy_to_clipboard(self.me["id"])
            self.emit("toast", {"text": "ID copied"})
        elif action == "show_code":
            p["show_code"] = True
            self._push_panels()
        elif action == "copy_code":
            dialogs.copy_to_clipboard(self.recovery_code())
            self.emit("toast", {"text": "Recovery code copied – keep it somewhere private"})
        elif action == "use_code":
            self._confirm("Use a recovery code", "This swaps the identity on this PC for the one the code belongs "
                          "to.\n\nIf you want to keep this one too, copy its recovery code first – otherwise it's "
                          "lost.", "Continue", self._ask_recovery_code)
        elif action == "name":
            self._close_panel("account")
            self._ask_name()
        elif action == "avatars":
            self.open_avatars()
        elif action == "saved":
            self.open_saved_chats(over=True)
        elif action == "save_transfer":
            self.export_transfer()
        elif action == "import_transfer":
            self._close_panel("account")
            self.import_transfer()
        elif action == "delete":
            self._prompt("Delete my account", "This can't be undone. Type DELETE to delete your account:",
                         self._delete_typed, ok="Delete account", danger=True, maxlength=10)

    def _ask_recovery_code(self):
        def use(code):
            if code.strip():
                self._close_panel("account")
                self.use_recovery_code(code.strip())
        self._prompt("Use a recovery code", "Paste the recovery code:", use, ok="Use it", maxlength=200)

    def _delete_typed(self, text: str):
        if text.strip() == "DELETE":
            self._close_panel("account")
            self.delete_account()

    def _panel_safety(self, p: dict, action: str, _payload: dict):
        other_id = p["other"]["id"]
        code, verified, changed = self.safety_info(other_id)
        if action == "accept" and changed:
            self.accept_keys(other_id)
        elif action == "verified" and code and not verified:
            self.accept_keys(other_id)
            self.mark_verified(other_id, code)
        else:
            return
        self._close_panel("safety")

    def _panel_avatars(self, p: dict, action: str, payload: dict):
        p["error"] = ""
        saved = self.saved_avatars
        if action == "roll":
            p["preview"] = avatars.new_seed()
        elif action == "original":
            p["preview"] = ""
        elif action == "pick":
            p["preview"] = self._panel_index(saved, payload) or p["preview"]
        elif action == "use":
            self.set_avatar(p["preview"])
        elif action == "keep":
            if len(saved) >= avatars.MAX_SAVED:
                p["error"] = f"You can keep {avatars.MAX_SAVED} – remove one first."
            elif p["preview"] and p["preview"] not in saved:
                self.save_avatars(saved + [p["preview"]])
        elif action == "remove":
            seed = self._panel_index(saved, payload)
            if seed is not None:
                self.save_avatars([s for s in saved if s != seed])
        self._push_panels()

    def _panel_transfer(self, p: dict, action: str, payload: dict):
        if action != "save":
            return
        password, again = str(payload.get("password") or ""), str(payload.get("confirm") or "")
        with_chats = bool(payload.get("chats")) and p["chats"] > 0
        p["error"] = transfer.password_problem(password, again) or ""
        if p["error"]:
            self._push_panels()
        elif password:
            self._save_transfer(password, with_chats)
        else:
            self._confirm("No password", "Save the file without a password?\n\nThen anyone who gets it – from your "
                          "Downloads folder, a USB stick, an email – can sign in as you and read your direct "
                          "messages.", "Save without a password", lambda: self._save_transfer("", with_chats),
                          danger=True)

    def open_saved_chats(self, over: bool = False):
        self._open_panel("saved", over=over)

    def _panel_saved(self, p: dict, action: str, payload: dict):
        chat = self._panel_index(p.get("chats") or [], payload)
        if chat is None:
            return

        def done(text, tone):
            p["note"], p["tone"] = text, tone
            self._push_panels()

        if action == "export":
            self._export_saved(chat, done)
        elif action == "delete":
            def delete():
                self.archive.delete(self._server_url(), chat["me"], chat["room"])
                done("Deleted.", "success")
            self._confirm("Delete saved messages", f"Delete the copy of your messages with "
                          f"{render.display_name(chat['other'])} saved on this PC? This can't be undone.",
                          "Delete", delete, danger=True)

    def _panel_ban(self, p: dict, action: str, payload: dict):
        values = panels.ban_values(payload) if action == "ban" else None
        if values is None:
            return
        days, reason, network = values
        self._close_panel("ban")
        self.admin({"type": "ban", "user": p["person"]["id"], "days": days, "reason": reason, "network": network})

    def _panel_admin(self, p: dict, action: str, payload: dict):
        answers = p["answers"]
        p["error"] = ""
        if action == "tab":
            p["tab"] = str(payload.get("tab") or "reports")
        elif action in ("keep", "delete_message", "ban_author", "view_image"):
            report = panels.find(answers.get("reports"), "reports", payload.get("id"))
            if report and action == "view_image" and report.get("image"):
                self.show_image(report["image"], report["room"])
            elif report and action == "ban_author":
                if (report.get("reported") or {}).get("id"):
                    self.ban(report["reported"])
            elif report:
                self.admin({"type": "resolve_report", "id": report["id"], "delete": action == "delete_message"})
        elif action == "unban":
            ban = next((b for b in (answers.get("bans") or {}).get("bans", [])
                        if b["user"]["id"] == payload.get("id")), None)
            if ban:
                self.admin({"type": "unban", "user": ban["user"]["id"]})
        elif action == "give_role":
            user_id = str(payload.get("user") or "").strip().lower()
            role = payload.get("role")
            allowed = ("mod", "admin") if self._am_owner() else ("mod",)
            p["error"] = panels.role_problem(user_id)
            if not p["error"] and role in allowed and self.my_role() in ("admin", "owner"):
                self.admin({"type": "set_role", "user": user_id, "role": role})
                p["sent"] += 1
        elif action == "remove_role":
            person = panels.find(answers.get("admins"), "admins", payload.get("id"))
            allowed = ("mod", "admin") if self._am_owner() else ("mod",)
            if person and person["role"] not in allowed:
                p["error"] = "Admins can only change mods; only the owner changes admins."
            elif person:
                self._confirm("Remove role", f"Make {render.display_name(person)} an ordinary user again?",
                              "Remove role", self._role_setter(person["id"], "user"))
        elif action == "post" and self._am_owner():
            title, text = str(payload.get("title") or "").strip()[:80], str(payload.get("text") or "").strip()
            p["error"] = panels.announcement_problem(title, text)
            if not p["error"]:
                def post():
                    self.admin({"type": "post_app_announcement", "title": title, "text": text})
                    p["sent"] += 1
                    self._push_panels()
                self._confirm("Post announcement", f"Post \"{title}\" to every Buddy?", "Post", post)
        elif action == "delete_announcement" and self._am_owner():
            item = panels.find(answers.get("app"), "announcements", payload.get("id"))
            if item:
                self.admin({"type": "delete_app_announcement", "id": item["id"]})
        self._push_panels()

    # --------------------------------------------------------- admin tools

    def my_role(self) -> str:
        return (self.me or {}).get("role") or "user"

    def _role_setter(self, user_id: str, role: str):
        return lambda: self.admin({"type": "set_role", "user": user_id, "role": role})

    def _am_staff(self) -> bool:
        """A mod, an admin or the owner: every moderation tool."""
        return self.my_role() in ("mod", "admin", "owner")

    def _am_owner(self) -> bool:
        """Only the owner deletes messages forever (server/core.py _purge_message)."""
        return self.my_role() == "owner"

    def admin(self, payload: dict):
        if not self.client.send(payload):
            self._notify("Not connected.")

    def ban(self, person: dict):
        self._open_panel("ban", over=True, person=person)

    def on_admin(self, _payload=None):
        if not self._online() or not self._am_staff():
            return
        self._open_panel("admin", answers={}, tab="reports", sent=0)
        for ask in panels.admin_requests(self.my_role()):
            self.admin(ask)

    def _report(self, message_id: int):
        m = self.messages.get(message_id)
        if not m or m.get("unreadable"):
            return
        sees = ("Direct messages are end-to-end encrypted, so Buddy sends them this message's text "
                "with your report." if m.get("e2e") else "They'll see the message and that you reported it.")

        def send(reason):
            report = {"type": "report", "id": message_id, "reason": reason}
            if m.get("e2e"):
                # An encrypted image can't go with it: the admins are told there was one.
                report["text"] = " ".join(p for p in (m["text"], "[image]" if m.get("image") else "") if p)
            self.client.send(report)

        self._prompt("Report message", f"Report this message from {render.display_name(m['author'])} to the "
                     f"admins?\n\n{sees}\n\nWhy? (optional)", send, maxlength=300, ok="Report")

    def _banned(self, msg: dict):
        self.client.stop()
        until = msg.get("until")
        lasts = "It's permanent." if until is None else f"It ends {panels.when(until)}."
        reason = f"\n\nReason: {msg['reason']}" if msg.get("reason") else ""
        self._set_status("Banned permanently" if until is None else f"Banned until {panels.when(until)}",
                         "danger")
        self._alert(self.display_name, f"{msg.get('message', 'You have been banned.')}\n\n{lasts}{reason}")

    def _on_buddy_list(self, state: dict):
        incoming = {p["id"] for p in state.get("incoming", [])}
        if self._seen_incoming is not None:
            for person in state.get("incoming", []):
                if person["id"] not in self._seen_incoming:
                    self._tray(f"{render.display_name(person)} wants to be your buddy.")
        self._seen_incoming = incoming
        self.buddy_state = {**EMPTY_BUDDIES, **state}
        self._remember_keys()
        buddies = {b["id"]: b for b in state.get("buddies", [])}
        for room_id, room in list(self.rooms.items()):
            if room["kind"] != "dm":
                continue
            other = buddies.get(room["other"]["id"])
            if other is None:
                # No longer buddies (removed, blocked, or they left): DMs end.
                self.rooms.pop(room_id)
                self.unread.pop(room_id, None)
                if room_id == self.room_id:
                    self.room_id = self.system_ids[0] if self.system_ids else "global"
                    self._join_current()
                    self._notify(f"Direct messages with {render.display_name(room['other'])} ended – "
                                 "you're not buddies any more.", "warning")
            else:
                room["other"], room["name"] = other, other.get("name") or "Someone"
        if self._restore_dm:
            people = self._restore_dm.split("-")[1:]
            self._restore_dm = None
            other = next((i for i in people if i != (self.me or {}).get("id")), None)
            if other in buddies:
                self.open_dm(other)
        self._push_buddies()
        self._push_sidebar()
        self._push_room()
        self._push_people()
        self._render()

    def _is_looking(self) -> bool:
        window = self.window()
        return self.isVisible() and window.isActiveWindow() and not window.isMinimized()

    def _tray(self, text: str):
        notify = getattr(self.host, "notify", None)
        if notify is not None and self.settings.get("notify_dms", True):
            notify(self.display_name, text)

    # ----------------------------------------------------------- account

    def on_account(self, _payload=None):
        if self._online():
            self._open_panel("account")

    def open_avatars(self):
        if self.me:
            self._open_panel("avatars", over=True, preview=self.me.get("avatar") or "")

    def set_avatar(self, seed: str):
        """"" goes back to the avatar from your ID."""
        if not self.client.send({"type": "set_avatar", "avatar": seed}):
            self._notify("Not connected.")

    def save_avatars(self, seeds: list[str]):
        if not self.client.send({"type": "save_avatars", "saved": list(seeds)}):
            self._notify("Not connected.")

    def recovery_code(self) -> str:
        return self.identity.token(self._server_url())

    def use_recovery_code(self, code: str):
        """Tried first: the saved identity is only replaced once the server
        accepts the code (see _on_received "welcome" / _on_error)."""
        self._trying_code = code
        self._connect()

    def delete_account(self):
        self.client.send({"type": "delete_account"})

    def _account_deleted(self):
        url = self._server_url()
        self.identity.forget(url)
        saved = dict(self.settings.get("saved_rooms") or {})
        saved.pop(url, None)
        self.settings["saved_rooms"] = saved
        self.settings["room"] = "global"
        self.room_id = "global"
        self.on_turn_off()
        self._alert(self.display_name, "Your Buddy Network account has been deleted. "
                    "Turning Buddy Network on again starts a new one.")

    def _offer_new_identity(self):
        self._confirm(self.display_name, "This server doesn't recognise the Buddy Network identity saved on "
                      "this PC – it may have been reset.\n\nMake a new identity? You'll choose a name again; "
                      "the old one can't be recovered.", "Make a new identity",
                      lambda: (self.identity.forget(self._server_url()), self._connect()))

    def _ask_name(self):
        if self._name_asking or not self.me or not self._online():
            return
        self._name_asking = True

        def done(value):
            self._name_asking = False
            name = " ".join(str(value or "").split())[:24]
            if len(name) < 2:
                self._name_error = "Names are at least 2 characters."
                self._ask_name()
                return
            self.client.send({"type": "set_name", "name": name})

        def cancelled():
            self._name_asking = False

        self._ask("name", {"current": self.me.get("name") or "", "tag": self.me.get("tag", ""),
                           "error": self._name_error}, done, cancelled)

    # ---------------------------------------------------------- sending

    def _own_secrets(self) -> list[str]:
        """The user's own API keys, from Ask Buddy's settings - compared
        against outgoing messages on this PC, never sent anywhere."""
        chat = self.host.tool_settings("manual_chat")
        values = getattr(chat, "values", chat)
        return [str(v) for k, v in values.items() if k.startswith("api_key") and v]

    def on_send(self, payload):
        text = str((payload or {}).get("text", "")).strip()
        if self.editing is not None:
            # A message with an image can lose its text: the image stays.
            has_picture = bool((self.messages.get(self.editing) or {}).get("image"))
        else:
            has_picture = self.attachment is not None and self.images_allowed()
        if not (text or has_picture) or not self._online():
            return
        if self._preparing and self.editing is None:
            self._notify("Wait a moment – the picture isn't ready yet.", "warning")
            return
        if len(text) > MAX_MESSAGE_CHARS:
            self._notify(f"Messages are at most {MAX_MESSAGE_CHARS:,} characters.")
            return
        room = self.rooms.get(self.room_id)
        dm = bool(room) and room["kind"] == "dm"
        check = safety.outgoing_warnings(text, self._own_secrets())
        target = (self.room_id, self.editing, self.reply_to["id"] if self.reply_to else None)
        if check.blocked:
            self._alert("Not sent", check.blocked)
            return
        if check.warnings:
            found = "\n".join(f"• {w}" for w in check.warnings)   # a line each, so each is translated
            where = ("Direct messages are end-to-end encrypted, but the person you're talking to can still copy "
                     "or share it – and anyone can claim to be anyone." if dm else
                     "Buddy Network rooms are public: anyone in the room can read it, and it stays on the "
                     "server for 30 days unless you delete it.")
            self._choice("Send this message?", f"This message contains:\n{found}\n\n{where}",
                         [("send", "Send anyway", "danger")], lambda _v: self._send_checked(text, target),
                         cancel="Edit message")
            return
        self._send_checked(text, target)

    def _send_checked(self, text, target):
        if target[0] != self.room_id:
            return   # the room changed while they were deciding
        room = self.rooms.get(self.room_id)
        if room and room["kind"] == "dm":
            other = room["other"]
            _code, _verified, changed = self.safety_info(other["id"])
            if changed:
                def decided(choice):
                    if choice == "send":
                        self.accept_keys(other["id"])
                        self._send_now(text, target)
                    elif choice == "check":
                        self.on_safety_code()
                self._choice("Encryption keys changed",
                             f"The encryption keys in this chat changed since you last accepted them – "
                             f"{other.get('name') or 'they'} (or you) set up another PC or reinstalled Buddy, or "
                             "someone is intercepting.\n\nIf you send now, the new PCs can read this message. "
                             "Check the safety code first if it's anything private.",
                             [("check", "Check the safety code", ""), ("send", "Send anyway", "danger")], decided)
                return
        self._send_now(text, target)

    def _send_now(self, text, target):
        room_id, editing, reply_id = target
        if room_id != self.room_id or not self._online():
            return
        room = self.rooms.get(room_id)
        if editing is not None:
            payload = {"type": "edit", "id": editing}
        else:
            payload = {"type": "send", "room": room_id}
            if reply_id is not None:
                payload["reply_to"] = reply_id
        if room and room["kind"] == "dm":
            enc = self._encrypt_for(room, text)
            if enc is None:
                return
            payload["enc"] = enc
        else:
            payload["text"] = text
        attachment = self.attachment if editing is None and self.images_allowed() else None
        if attachment is not None:
            image = self._upload_image(room, attachment)
            if image is None:
                if not self._notice[0]:
                    self._notify("Not connected – your message wasn't sent.")
                return
            payload["image"] = image
        self._nonce += 1
        payload["nonce"] = self._nonce
        if self.client.send(payload):
            self._pending[self._nonce] = text
            if attachment is not None:
                self._pending_images[self._nonce] = attachment
                self.attachment = None
                self._push_attachment()
            self.reply_to = self.editing = None
            self._push_compose(text="")
            self._notify("")
        else:
            self._notify("Not connected – your message wasn't sent.")

    # ----------------------------------------------------------- clicks

    def on_anchor(self, payload):
        payload = payload or {}
        target = str(payload.get("href") or "")
        x, y = payload.get("x", 0), payload.get("y", 0)
        kind, _, arg = target.partition(":")
        if kind == "bn-link" and arg.isdigit() and int(arg) < len(self._links):
            self._open_link(self._links[int(arg)])
        elif kind == "bn-delete" and arg.isdigit():
            m = self.messages.get(int(arg))
            theirs = m is not None and m["author"].get("id") != (self.me or {}).get("id")
            self._confirm("Delete message", "Delete this message for everyone?" + (
                "\n\nIt's someone else's – as an admin you can, and it goes in the admin log." if theirs else ""),
                "Delete", lambda: self.client.send({"type": "delete", "id": int(arg)}), danger=True)
        elif kind == "bn-purge" and arg.isdigit() and self._am_owner():
            self._confirm("Delete forever", "Delete this message forever?\n\nIt disappears for everyone, with no "
                          "\"message deleted\" left in its place, and replies to it lose their quote. This can't "
                          "be undone.", "Delete forever",
                          lambda: self.client.send({"type": "purge_message", "id": int(arg)}), danger=True)
        elif kind == "bn-reply" and arg.isdigit():
            self._start_reply(int(arg))
        elif kind == "bn-edit" and arg.isdigit():
            self._start_edit(int(arg))
        elif kind == "bn-user" and arg:
            self._user_menu(arg, x, y)
        elif kind == "bn-report" and arg.isdigit():
            self._report(int(arg))
        elif kind == "bn-more" and self.more and self.messages:
            self.client.send({"type": "history", "room": self.room_id, "before": min(self.messages)})
        elif kind == "bn-more":
            for m in self._saved_before()[-50:]:   # the server has nothing older: this PC's copy
                self.messages[m["id"]] = m
            self._render(keep_position=True)

    def _open_link(self, raw_url: str):
        info = safety.describe_link(raw_url)

        def chosen(choice):
            if choice == "open":
                QDesktopServices.openUrl(QUrl(info.open_url))
            elif choice == "copy":
                self._copy(info.url, "Link copied")

        self._ask("link", {"host": info.host or "an unknown site", "url": info.url, "warnings": info.warnings},
                  chosen)

    # ------------------------------------------------------------ render

    def _render(self, keep_position=False, to_bottom=False):
        if not self.me:
            self.emit("messages", {"html": "", "to_bottom": True, "keep_position": False})
            return
        colors = web_view.message_colors(self.host.theme_tokens())
        self._links = []
        ordered = [self.messages[k] for k in sorted(self.messages)]
        hidden = {p["id"] for p in self.buddy_state.get("blocked", [])}
        query = self.search_query.strip().casefold() if self.search_open else ""
        top_note = ""
        if query:
            pool = {m["id"]: m for m in ordered}
            if self._keeping(self.room_id):   # a DM's saved copy reaches further back than the server
                for m in self.archive.messages(self._server_url(), self.me["id"], self.room_id):
                    pool.setdefault(m["id"], dict(m, saved_only=True))
            ordered = [pool[k] for k in sorted(pool) if not pool[k]["deleted"] and (
                query in pool[k].get("text", "").casefold()
                or query in (pool[k]["author"].get("name") or "").casefold())]
            found = sum(m["author"].get("id") not in hidden for m in ordered)
            shown = self.search_query.strip()
            top_note = (f"{found:,} message{'s match' if found != 1 else ' matches'} \"{shown}\"" if found
                        else f"No messages match \"{shown}\" – load earlier messages to search further back")
        body = render.room_html(
            ordered, my_id=self.me["id"], room_name=self._room_name(), more=self.more,
            links=self._links, colors=colors, now=time.time(), history_days=self.history_days,
            hidden=hidden, admin=self._am_staff(), owner=self._am_owner(),
            more_saved=bool(self._saved_before()), saved_copy=self._keeping(self.room_id),
            me=self.me, can_reply=self._online() and bool(self.me.get("name")) and not query,
            top_note=top_note, lookup=self.messages, avatar_url=web_view.avatar_url,
            image_days=(self.image_limits or {}).get("image_days", 7))
        self.emit("messages", {"html": body, "to_bottom": to_bottom, "keep_position": keep_position})
        self._want_images()

    # ---------------------------------------------------------- messages

    def _on_received(self, msg: dict):
        kind = msg.get("type")
        if kind == "welcome":
            self.me = msg["user"]
            self._watched = None   # a new connection: tell it what to watch again
            self.saved_avatars = [s for s in msg.get("saved_avatars", []) if isinstance(s, str)]
            system = msg.get("rooms", [])
            self.rooms = {r["id"]: r for r in system + msg.get("my_rooms", [])}
            self.system_ids = [r["id"] for r in system]
            limits = msg.get("limits") or {}
            days = limits.get("history_days", 30)
            self.history_days = days if isinstance(days, int) and not isinstance(days, bool) else 30
            # A server from before images says nothing about them: none offered.
            self.image_limits = limits if isinstance(limits.get("max_image_bytes"), int) else None
            self._reset_images()
            self._reset_gifs()
            url = self._server_url()
            if msg.get("token"):
                self.identity.save(url, self.me["id"], msg["token"])
            elif self.identity.get(url).get("id") not in (None, self.me["id"]) and not self._trying_code:
                # The server moved this identity to a new ID (a staff tag).
                self.identity.save(url, self.me["id"], self.identity.token(url))
            elif self._trying_code:
                self.identity.save(url, self.me["id"], self._trying_code)
                self._trying_code = None
                self._notify("Recovery code accepted – you're signed in as that identity.", "success")
            try:
                self.device = self.keystore.device(url)
                self._readers = self.keystore.readers(url)
            except (e2e.CryptoError, OSError) as exc:
                self.device, self._readers = None, []
                self._notify(f"Encryption couldn't be set up on this PC, so direct messages won't work: {exc}")
            if self.device is not None:
                self.client.send({"type": "set_device_key", "key": self.device.public_b64})
            if self.keystore.warnings:
                warnings = "\n\n".join(self.keystore.warnings)
                self.keystore.warnings.clear()
                self._alert(self.display_name, warnings)
            if self.room_id.startswith("dm-"):
                self._restore_dm = self.room_id   # reopened once the buddy list arrives
            if self._saved_ids():
                self.client.send({"type": "get_rooms", "ids": self._saved_ids()})
            self._push_sidebar()
            self._join_current()
            self._push_attachment()
            if not self.me.get("name"):
                QTimer.singleShot(0, self._ask_name)
        elif kind == "name_set":
            self.me = msg["user"]
            self._name_error = ""
            for m in self.messages.values():
                if m["author"]["id"] == self.me["id"]:
                    m["author"]["name"] = self.me["name"]
            self._render()
        elif kind == "avatar_set":
            self.me = msg["user"]
            self.saved_avatars = [s for s in msg.get("saved", []) if isinstance(s, str)]
            for m in self.messages.values():   # your messages already on screen change too
                if m["author"]["id"] == self.me["id"]:
                    m["author"]["avatar"] = self.me.get("avatar", "")
            self._render()
            self._push_panels()
        elif kind == "history" and self._export is not None and msg.get("nonce") == self._export["nonce"]:
            self._export_page(msg)
        elif kind == "history" and msg.get("room") == self.room_id and "nonce" not in msg:
            older = msg.get("before") is not None
            incoming = {m["id"]: self._open_message(m) for m in msg.get("messages", [])}
            self._keep(self.room_id, incoming.values())
            self.messages = {**incoming, **self.messages} if older else incoming
            self.more = bool(msg.get("more"))
            if not older:
                self._mark_seen(self.room_id, incoming)
            self._render(keep_position=older, to_bottom=not older)
            self._push_people()
        elif kind == "message":
            m = self._open_message(msg["message"])
            self._keep(m["room"], [m])
            mine = self._pending.pop(msg.get("nonce"), None) is not None
            self._sent(msg.get("nonce"))
            from_me = bool(self.me) and m["author"].get("id") == self.me["id"]
            blocked = self._in("blocked", m["author"].get("id", ""))
            if m["room"] == self.room_id:
                self.messages[m["id"]] = m
                self._mark_seen(self.room_id, [m["id"]])
                self._render(to_bottom=mine)
            elif m["room"].startswith("dm-") and not from_me and not blocked:
                self.unread[m["room"]] = self.unread.get(m["room"], 0) + 1
                self._push_sidebar()
            if m["room"].startswith("dm-") and not from_me and not blocked and not self._is_muted(m["room"]) \
                    and not (m["room"] == self.room_id and self._is_looking()):
                # The name only - never the text, which could be read over a shoulder.
                self._tray(f"New message from {render.display_name(m['author'])}")
        elif kind == "edited":
            enc = (msg.get("message") or {}).get("enc")
            if isinstance(enc, dict) and enc.get("salt") in self._salts:
                return   # an earlier version sent again, not a new edit
            m = self._open_message(msg["message"])
            self._keep(m["room"], [m])
            if m["room"] == self.room_id and m["id"] in self.messages:
                self.messages[m["id"]] = m
                self._render()
        elif kind == "unread":
            for room_id, count in (msg.get("rooms") or {}).items():
                if room_id != self.room_id and isinstance(count, int) and count > 0:
                    self.unread[room_id] = count
            self._push_sidebar()
        elif kind == "activity":
            room_id = msg.get("room")
            if isinstance(room_id, str) and room_id != self.room_id:
                self.unread[room_id] = min(99, self.unread.get(room_id, 0) + 1)
                self._push_sidebar()
        elif kind == "mentioned":
            m, room_id = msg.get("message") or {}, msg.get("room")
            who = m.get("author") or {}
            if isinstance(room_id, str) and not self._in("blocked", who.get("id", "")) and not (
                    room_id == self.room_id and self._is_looking()):
                if room_id != self.room_id:
                    self.mentioned.add(room_id)
                    self._push_sidebar()
                self._tray(f"{render.display_name(who)} mentioned you in #{msg.get('room_name') or 'a room'}")
        elif kind == "deleted":
            if self._keeping(msg.get("room") or "") and isinstance(msg.get("id"), int):
                self.archive.mark_deleted(self._server_url(), self.me["id"], msg["room"], msg["id"])
            m = self.messages.get(msg.get("id")) if msg.get("room") == self.room_id else None
            if m:
                m["deleted"], m["text"] = True, ""
                self._render()
        elif kind == "purged":
            # Deleted forever: gone from the view (and any saved copy), no placeholder.
            if self._keeping(msg.get("room") or "") and isinstance(msg.get("id"), int):
                self.archive.forget(self._server_url(), self.me["id"], msg["room"], msg["id"])
            if msg.get("room") == self.room_id and self.messages.pop(msg.get("id"), None) is not None:
                self._render()
        elif kind == "rooms_info":
            for room in msg.get("rooms", []):
                self.rooms[room["id"]] = room
            missing = [i for i in msg.get("missing", []) if isinstance(i, str)]
            for room_id in missing:
                self._room_gone(room_id, "deleted")
            self._push_sidebar()
            self._push_room()
        elif kind == "room_created":
            room = msg["room"]
            self.rooms[room["id"]] = room
            if self._creating:
                self._creating, self._room_draft, self._room_error = False, ("", ""), ""
                self._switch_room(room["id"])
            else:
                self._push_sidebar()
        elif kind == "room_updated":
            room = msg["room"]
            public = room.get("kind") == "system"
            if public != (room["id"] in self.system_ids):
                self._public_changed(room)
            elif room["id"] in self.rooms:
                self.rooms[room["id"]] = room
                self._push_sidebar()
                if room["id"] == self.room_id:
                    self._push_room()
        elif kind == "room_removed":
            self._room_gone(msg.get("room"), msg.get("reason", "deleted"))
        elif kind == "found_rooms":
            if self._browsing:
                rooms = [r for r in msg.get("rooms", []) if isinstance(r, dict) and isinstance(r.get("id"), str)]
                self._found = {r["id"]: r for r in rooms}
                self.emit("found_rooms", {
                    "open": True, "query": msg.get("query", ""), "permanent_only": bool(msg.get("permanent_only")),
                    "rooms": [{"id": r["id"], "name": r.get("name", ""), "topic": r.get("topic", ""),
                               "here": r.get("here", 0) if isinstance(r.get("here"), int) else 0,
                               "permanent": bool(r.get("permanent")),
                               "owner": render.display_name(r.get("owner") or {"id": "?", "name": None})}
                              for r in rooms]})
        elif kind == "buddy_list":
            self._on_buddy_list(msg)
        elif kind == "dm_opened":
            room = msg["room"]
            self.rooms[room["id"]] = room
            if self._opening_dm == room["other"]["id"]:
                self._opening_dm = None
                self.emit("buddies_open", False)
                self._switch_room(room["id"])
        elif kind == "account_deleted":
            QTimer.singleShot(0, self._account_deleted)
            return
        elif kind == "reports_waiting":
            if msg.get("count", 0) > self.reports_waiting:
                self._tray("A message was reported – see Admin.")
            self.reports_waiting = msg.get("count", 0)
        elif kind in panels.ADMIN_ANSWERS:
            if self._panel("admin") is not None:
                self._panel("admin")["answers"][panels.ADMIN_ANSWERS[kind]] = msg
                self._push_panels()
            if kind == "app_announcements" and hasattr(self.host, "check_announcements_now"):
                # So the owner's own orb shows a new one straight away.
                self.host.check_announcements_now()
        elif kind == "role_changed":
            self.me = msg["user"]
            self._push_room()
            self._render()
            self._notify(ROLE_NOTICES.get(self.my_role(), ROLE_NOTICES["user"]), "success")
        elif kind == "ban_done":
            self._notify("Banned, along with their network." if msg.get("networks") else "Banned.", "success")
        elif kind == "reported":
            self._notify("Reported – thanks. The admins will take a look.", "success")
        elif kind == "image":
            self._image_arrived(msg)
        elif kind == "gif_results":
            self._gif_results(msg)
        elif kind == "gif_thumb":
            self._gif_thumb(msg)
        elif kind == "gif_data":
            self._gif_data(msg)
        elif kind == "error":
            self._on_error(msg)
        self._push_state()

    def _on_error(self, msg: dict):
        code, text = msg.get("code"), msg.get("message", "Something went wrong.")
        if self._image_error(msg) or self._gif_error(msg):
            pass
        elif self._export is not None and msg.get("nonce") == self._export["nonce"]:
            self._export = None
            self._notify(f"The export stopped: {text}")
        elif code == "banned":
            QTimer.singleShot(0, lambda: self._banned(msg))
        elif self._panel("admin") is not None and msg.get("re") in panels.ADMIN_REQUESTS:
            self._panel("admin")["error"] = text
            self._push_panels()
        elif code == "bad_token" and self._trying_code:
            self._trying_code = None
            self.client.stop()
            self._connect()   # back to the identity saved on this PC
            self._notify("That recovery code wasn't recognised – nothing was changed.")
        elif code == "bad_token":
            self.client.stop()
            self._set_status("Offline: this server doesn't know your saved identity", "danger")
            QTimer.singleShot(0, self._offer_new_identity)
        elif code in ("update_required",) or (code == "rate_limited" and msg.get("re") == "hello"):
            self.client.stop()
            self._set_status(f"Offline: {text}", "danger")
        elif code == "bad_name":
            self._name_error = text
            QTimer.singleShot(0, self._ask_name)
        elif msg.get("re") == "create_room":
            self._creating = False
            self._room_error = text
            QTimer.singleShot(0, self.on_new_room)
        elif msg.get("re") == "join" and code == "no_room":
            self._room_gone(self.room_id, "deleted")
        elif msg.get("re") == "find_rooms" and self._browsing:
            self.emit("found_rooms", {"open": True, "error": text})
        elif msg.get("re") in ("buddy_request", "buddy_accept", "block", "open_dm"):
            self._buddies_error(text)
            self._notify(text)
        else:
            restored = self._pending.pop(msg.get("nonce"), None)
            text = self._send_failed(msg, text)
            if restored:
                self.emit("compose_restore", restored)
            self._notify(text)
