"""Buddy Network's web page with a fake connection: turning on, choosing a
name, what the message list and sidebar get, the questions the page asks
before sending or opening a link, the buddy tools and the panels (account,
avatars, transfer, saved chats, ban, admin). Never a real server, never the
real ~/.buddy_network (identity and keys go in a temp folder) and never the
real clipboard. The web helpers (avatars, sidebar, pasted IDs) and what
each panel shows (panels.py) are tested without Qt."""

import base64
import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.buddy_network import panels, web_view

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

ME = {"id": "a" * 32, "tag": "aaaaaa", "name": "Tess"}
SAM = {"id": "b" * 32, "tag": "bbbbbb", "name": "Sam"}


class HelperTests(unittest.TestCase):
    def test_avatar_is_a_self_contained_svg(self):
        url = web_view.avatar_url("0f1e2d3c")
        self.assertTrue(url.startswith("data:image/svg+xml;base64,"))
        svg = base64.b64decode(url.split(",", 1)[1]).decode("ascii")
        self.assertNotIn("http:", svg.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("0f1e2d3c", svg)            # the key picks the drawing, it isn't written in
        self.assertEqual(web_view.avatar_url("0f1e2d3c"), url)
        self.assertNotEqual(web_view.avatar_url("11111111"), url)

    def test_pasted_ids(self):
        self.assertEqual(web_view.parse_buddy_id(f"Sam #bbbbbb ({'B' * 32})"), "b" * 32)
        self.assertEqual(web_view.parse_buddy_id("sam"), "")

    def test_sidebar_sections(self):
        rooms = [{"id": "global", "name": "Global", "kind": "system"}]
        sections = web_view.sidebar(system=rooms, mine=[], saved=[], buddies=[dict(SAM, online=True)],
                                    me_id=ME["id"], current="global", unread={"dm-x": 2}, mentioned=set(),
                                    muted=lambda r: False, dm_id=lambda a, b: "dm-x")
        self.assertEqual([s["heading"] for s in sections], ["", "Buddies"])   # empty sections left out
        buddy = sections[1]["items"][0]
        self.assertEqual((buddy["key"], buddy["room"], buddy["unread"], buddy["online"]), (f"user:{SAM['id']}", "dm-x", 2, True))
        self.assertTrue(sections[0]["items"][0]["current"])


class PanelViewTests(unittest.TestCase):
    def test_safety_code(self):
        code = "12345 67890 12345 67890 12345 67890"
        view = panels.safety(SAM, code, False, True)
        self.assertEqual(view["lines"], [code[:17], code[18:]])
        self.assertEqual(view["tone"], "warning")
        self.assertTrue(view["can_accept"] and view["can_verify"])
        self.assertFalse(panels.safety(SAM, code, True, False)["can_verify"])
        self.assertEqual(panels.safety(SAM, "", False, False)["lines"], [])

    def test_avatars(self):
        me = dict(ME, avatar="s1")
        view = panels.avatar_panel(me, ["s1", "s2"], "s1")
        self.assertTrue(view["using"])
        self.assertFalse(view["can_keep"])                      # already a favourite
        self.assertEqual([s["in_use"] for s in view["saved"]], [True, False])
        self.assertTrue(panels.avatar_panel(me, ["s1"], "new")["can_keep"])
        self.assertFalse(panels.avatar_panel(me, ["x"] * 6, "new")["can_keep"])   # full
        self.assertFalse(panels.avatar_panel(me, [], "")["can_keep"])             # the original isn't kept
        self.assertTrue(panels.avatar_panel(me, [], "")["is_original"])

    def test_ban_values_only_take_what_was_offered(self):
        self.assertEqual(panels.ban_values({"length": 3, "reason": "  spam \n spam ", "network": 1}),
                         (None, "spam spam", True))
        self.assertEqual(panels.ban_values({"length": 0})[0], 1)
        for bad in (4, -1, True, "1", None):
            self.assertIsNone(panels.ban_values({"length": bad}), bad)
        self.assertEqual(len(panels.ban_values({"length": 1, "reason": "x" * 500})[1]), 200)

    def test_admin_by_role(self):
        self.assertEqual([t["id"] for t in panels.admin_tabs("mod")], ["reports", "bans"])
        self.assertEqual([t["id"] for t in panels.admin_tabs("owner")], ["reports", "bans", "admins", "log", "app"])
        self.assertEqual([a["type"] for a in panels.admin_requests("admin")], ["list_reports", "list_bans", "list_admins"])
        staff = {"admins": [dict(SAM, role="mod"), dict(ME, role="admin")]}
        view = panels.admin("admin", {"admins": staff}, "log")
        self.assertEqual(view["tab"], "reports")                # not theirs: back to Reports
        self.assertEqual([a["can_remove"] for a in view["staff"]], [True, False])
        self.assertEqual([r["id"] for r in view["roles"]], ["mod"])
        self.assertIsNone(view["reports"])                      # not arrived yet
        owner = panels.admin("owner", {"admins": staff}, "admins")
        self.assertEqual([a["can_remove"] for a in owner["staff"]], [True, True])
        report = {"id": 7, "reported": SAM, "reporter": ME, "where": "#Global", "created": 0, "text": "<b>hi</b>",
                  "times": 2, "claimed": True, "reason": "rude"}
        row = panels.admin("mod", {"reports": {"reports": [report]}}, "reports")["reports"][0]
        self.assertEqual(row["text"], "<b>hi</b>")              # text; the view never renders it as HTML
        self.assertIn("(2 reports)", row["head"])
        self.assertEqual(row["by"], "Reported by Tess #aaaaaa: rude")

    def test_checks(self):
        self.assertTrue(panels.role_problem("abc"))
        self.assertEqual(panels.role_problem("a" * 32), "")
        self.assertTrue(panels.role_problem("a" * 31 + "!"))
        self.assertTrue(panels.announcement_problem("Hi", ""))
        self.assertTrue(panels.announcement_problem("Hi", "x" * 1001))
        self.assertEqual(panels.announcement_problem("Hi", "x"), "")


class FakeClient(QObject if HAVE_QT else object):
    if HAVE_QT:
        state_changed = Signal(str, str)
        received = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "off"
        self.sent = []
        self.started = None

    def start(self, url, hello):
        self.started = (url, hello())

    def stop(self):
        self.state = "off"

    def send(self, payload):
        if self.state != "online":
            return False
        self.sent.append(payload)
        return True


class Mem(dict):
    @property
    def values(self):
        return self

    def save(self):
        pass


class Host:
    def __init__(self):
        self.controller, self.connected = None, False
        self.shared_settings = {"theme": "Resolve"}
        self.tools = {"buddy_network": None, "manual_chat": Mem(api_key_openai="sk-proj-" + "x" * 30)}
        self.notes = []

    def tool_settings(self, tool_id, defaults=None):
        if self.tools.get(tool_id) is None:
            self.tools[tool_id] = Mem(defaults or {})
        return self.tools[tool_id]

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def notify(self, title, text):
        self.notes.append(text)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from pages.buddy_network import page as page_mod
        self.page_mod = page_mod
        for patch in (mock.patch.object(page_mod, "NetworkClient", FakeClient),
                      mock.patch.object(page_mod.BuddyNetworkPage, "_data_folder", lambda s: self._tmp.name)):
            patch.start()
            self.addCleanup(patch.stop)
        self.host = Host()
        self.page = page_mod.BuddyNetworkPage(self.host)
        self.addCleanup(self.page.deleteLater)
        self.events = []
        self.page.emit = lambda name, payload=None: self.events.append((name, payload))
        self.client = self.page.client

    def last(self, name):
        found = [p for n, p in self.events if n == name]
        return found[-1] if found else None

    def answer(self, value=None, ok=True, kind=None):
        ask = self.last("ask")
        if kind:
            self.assertEqual(ask["kind"], kind)
        self.page.on_answer({"id": ask["id"], "ok": ok, "value": value})
        return ask

    def welcome(self, name="Tess"):
        self.page.on_turn_on()
        self.answer(True, kind="rules")
        self.assertTrue(self.host.tools["buddy_network"]["enabled"])
        self.client.state = "online"
        user = dict(ME, name=name)
        self.page._on_received({"type": "welcome", "user": user, "token": "t" * 40,
                                "rooms": [{"id": "global", "name": "Global", "kind": "system"},
                                          {"id": "help", "name": "Help", "kind": "system"}]})
        self.page._on_received({"type": "history", "room": "global", "messages": [
            {"id": 1, "room": "global", "ts": 1_750_000_000.0, "deleted": False, "author": SAM,
             "text": "<img src=x onerror=alert(1)> see https://example.com/x and hi @Tess#aaaaaa"}]})

    def test_turning_on_asks_for_the_rules_first(self):
        self.page.on_turn_on()
        ask = self.last("ask")
        self.assertEqual(ask["kind"], "rules")
        self.assertIsNone(self.client.started)
        self.answer(ok=False)
        self.assertFalse(self.host.tools["buddy_network"].get("enabled"))
        self.page.on_turn_on()
        self.answer(True)
        self.assertEqual(self.client.started[1]["type"], "hello")
        self.assertEqual(self.last("state")["mode"], "chat")

    def test_messages_are_escaped_and_links_go_through_the_warning(self):
        self.welcome()
        html = self.last("messages")["html"]
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)
        self.assertIn('href="bn-link:0"', html)
        self.assertNotIn('href="https', html)
        self.assertIn('src="data:image/svg+xml;base64,', html)   # avatars are inline, never fetched
        self.assertIn("mentioned", html)
        with mock.patch.object(self.page_mod.QDesktopServices, "openUrl") as opened:
            self.page.on_anchor({"href": "bn-link:0"})
            ask = self.answer("open", kind="link")
            self.assertEqual(ask["host"], "example.com")
            self.assertEqual(opened.call_args[0][0].toString(), "https://example.com/x")
        with mock.patch.object(self.page_mod.QDesktopServices, "openUrl") as opened:
            self.page.on_anchor({"href": "bn-link:0"})
            self.answer(ok=False)
            opened.assert_not_called()
            self.page.on_anchor({"href": "bn-link:99"})         # not in the table: nothing
            self.assertEqual(self.last("ask")["kind"], "link")

    def test_sidebar_and_room(self):
        self.welcome()
        sections = self.last("sidebar")
        self.assertEqual([i["label"] for i in sections[0]["items"]], ["Global", "Help"])
        self.assertEqual(self.last("room")["title"], "Global")
        self.page.on_open({"key": "help"})
        self.assertEqual(self.client.sent[-1], {"type": "join", "room": "help"})
        self.assertIn({"type": "leave", "room": "global"}, self.client.sent)

    def test_a_new_account_is_asked_for_a_name(self):
        self.welcome(name=None)
        self.app.processEvents()
        ask = self.last("ask")
        self.assertEqual((ask["kind"], ask["tag"]), ("name", "aaaaaa"))
        self.answer("  T  ")                                    # too short: asked again
        self.assertEqual(self.last("ask")["error"], "Names are at least 2 characters.")
        self.answer("Tess  Bee")
        self.assertEqual(self.client.sent[-1], {"type": "set_name", "name": "Tess Bee"})

    def test_sending_checks_first(self):
        self.welcome()
        self.page.on_send({"text": "hello all"})
        self.assertEqual(self.client.sent[-1]["text"], "hello all")
        self.assertEqual(self.last("compose")["text"], "")      # the box is cleared once it's sent
        self.page.on_send({"text": "mail me at tess@example.com"})
        ask = self.last("ask")
        self.assertEqual(ask["kind"], "choice")
        self.assertIn("an email address", ask["text"])
        count = len(self.client.sent)
        self.answer(ok=False)                                   # "Edit message"
        self.assertEqual(len(self.client.sent), count)
        self.page.on_send({"text": "mail me at tess@example.com"})
        self.answer("send")
        self.assertEqual(self.client.sent[-1]["text"], "mail me at tess@example.com")
        self.page.on_send({"text": "my key sk-proj-" + "x" * 30})   # their own API key: never
        self.assertEqual(self.last("alert")["title"], "Not sent")
        self.assertNotIn("sk-proj", str(self.client.sent[-1]))

    def test_reply_and_edit(self):
        self.welcome()
        self.page.on_anchor({"href": "bn-reply:1"})
        self.assertEqual(self.last("compose")["mode"], "reply")
        self.page.on_send({"text": "yes"})
        self.assertEqual(self.client.sent[-1]["reply_to"], 1)
        self.page._on_received({"type": "message", "nonce": self.client.sent[-1]["nonce"], "message": {
            "id": 2, "room": "global", "ts": 1_750_000_001.0, "deleted": False, "author": ME, "text": "yes"}})
        self.page.on_anchor({"href": "bn-edit:2"})
        self.assertEqual(self.last("compose")["text"], "yes")
        self.page.on_send({"text": "yes!"})
        self.assertEqual({k: self.client.sent[-1][k] for k in ("type", "id", "text")}, {"type": "edit", "id": 2, "text": "yes!"})
        self.page.on_anchor({"href": "bn-edit:1"})              # not yours
        self.assertIsNone(self.last("compose")["mode"])

    def test_delete_asks_and_the_user_menu(self):
        self.welcome()
        self.page.on_anchor({"href": "bn-delete:1"})
        self.answer("ok", kind="choice")
        self.assertEqual(self.client.sent[-1], {"type": "delete", "id": 1})
        self.page.on_anchor({"href": f"bn-user:{SAM['id']}", "x": 10, "y": 20})
        menu = self.last("menu")
        labels = [i.get("label") for i in menu["items"]]
        self.assertIn("Add as buddy", labels)
        self.assertNotIn("Ban...", labels)                      # not staff
        pick = next(i for i in menu["items"] if i.get("label") == "Add as buddy")
        self.page.on_menu_pick({"id": pick["id"]})
        self.assertEqual(self.client.sent[-1], {"type": "buddy_request", "user": SAM["id"]})
        self.page.on_menu_pick({"id": pick["id"]})              # a menu is used once
        self.assertEqual(len([p for p in self.client.sent if p["type"] == "buddy_request"]), 1)

    def test_buddies(self):
        self.welcome()
        self.page.on_add_buddy({"text": "nope"})
        self.assertIn("doesn't look like", self.last("buddies_error"))
        self.page.on_add_buddy({"text": f"Sam ({SAM['id']})"})
        self.assertEqual(self.client.sent[-1], {"type": "buddy_request", "user": SAM["id"]})
        self.page._on_received({"type": "buddy_list", "buddies": [dict(SAM, online=True)], "incoming": [],
                                "outgoing": [], "blocked": []})
        self.assertEqual(self.last("buddies")["buddies"][0]["label"], "Sam #bbbbbb")
        self.assertEqual(self.last("sidebar")[-1]["heading"], "Buddies")
        self.page.on_social({"kind": "block", "user": SAM["id"]})
        self.answer("ok", kind="choice")                        # blocking is confirmed first
        self.assertEqual(self.client.sent[-1], {"type": "block", "user": SAM["id"]})
        self.page.on_social({"kind": "nonsense", "user": SAM["id"]})
        self.assertEqual(self.client.sent[-1]["type"], "block")

    def test_browse_opens_only_rooms_the_server_found(self):
        self.welcome()
        self.page.on_browse()
        self.assertEqual(self.client.sent[-1]["type"], "find_rooms")
        room = {"id": "r1", "name": "Grading", "kind": "user", "topic": "", "owner": SAM, "here": 3}
        self.page._on_received({"type": "found_rooms", "rooms": [room], "query": "", "permanent_only": False})
        self.assertEqual(self.last("found_rooms")["rooms"][0]["owner"], "Sam #bbbbbb")
        self.page.on_open_found({"id": "made-up"})
        self.assertNotIn("made-up", self.page.rooms)
        self.page.on_open_found({"id": "r1"})
        self.assertEqual(self.page.room_id, "r1")
        self.assertIn("r1", self.page._saved_ids())

    def panels(self):
        return [p["kind"] for p in self.last("panels") or []]

    def panel(self, kind):
        return next(p for p in self.last("panels") if p["kind"] == kind)

    def act(self, kind, action, **extra):
        self.page.on_panel_action({"kind": kind, "action": action, **extra})

    def test_account_panel(self):
        self.welcome()
        self.page.on_account()
        self.assertEqual(self.panels(), ["account"])
        self.assertIsNone(self.panel("account")["code"])        # hidden until Show
        self.act("account", "show_code")
        self.assertEqual(self.panel("account")["code"], "t" * 40)
        with mock.patch.object(self.page_mod.dialogs, "copy_to_clipboard") as copied:
            self.act("account", "copy_id")
            copied.assert_called_with(ME["id"])
        self.act("account", "delete")
        self.assertTrue(self.last("ask")["danger"])
        self.answer("delete")                                   # must be DELETE exactly
        self.assertNotIn({"type": "delete_account"}, self.client.sent)
        self.act("account", "delete")
        self.answer("DELETE")
        self.assertEqual(self.client.sent[-1], {"type": "delete_account"})
        self.assertEqual(self.panels(), [])
        self.act("account", "show_code")                        # closed: nothing happens
        self.assertEqual(self.panels(), [])

    def test_avatars_open_over_the_account(self):
        self.welcome()
        self.page.on_account()
        self.act("account", "avatars")
        self.assertEqual(self.panels(), ["account", "avatars"])
        first = self.panel("avatars")["preview"]
        self.act("avatars", "roll")
        self.assertNotEqual(self.panel("avatars")["preview"], first)
        self.act("avatars", "keep")
        seed = self.client.sent[-1]["saved"][0]
        self.assertEqual(self.client.sent[-1]["type"], "save_avatars")
        self.page._on_received({"type": "avatar_set", "user": dict(ME, name="Tess"), "saved": [seed]})
        self.assertEqual(len(self.panel("avatars")["saved"]), 1)
        self.act("avatars", "pick", index=9)                    # not one of theirs
        self.act("avatars", "use")
        self.assertEqual(self.client.sent[-1], {"type": "set_avatar", "avatar": seed})
        self.page.on_panel_close({"kind": "avatars"})
        self.assertEqual(self.panels(), ["account"])

    def test_admin_panel(self):
        self.welcome()
        self.page.me["role"] = "mod"
        self.page.on_admin()
        self.assertEqual(self.panels(), ["admin"])
        self.assertEqual([p["type"] for p in self.client.sent[-2:]], ["list_reports", "list_bans"])
        report = {"id": 7, "reported": SAM, "reporter": ME, "where": "#Global", "created": 0, "text": "hi"}
        self.page._on_received({"type": "reports", "reports": [report]})
        self.assertEqual(self.panel("admin")["reports"][0]["id"], 7)
        count = len(self.client.sent)
        self.act("admin", "delete_message", id=99)              # not a report it was sent
        self.act("admin", "give_role", user="c" * 32, role="mod")   # mods don't give roles
        self.assertEqual(len(self.client.sent), count)
        self.act("admin", "delete_message", id=7)
        self.assertEqual(self.client.sent[-1], {"type": "resolve_report", "id": 7, "delete": True})
        self.act("admin", "ban_author", id=7)
        self.assertEqual(self.panels(), ["admin", "ban"])
        self.act("ban", "ban", length=9)
        self.assertEqual(self.panels(), ["admin", "ban"])
        self.act("ban", "ban", length=1, reason="spam", network=False)
        self.assertEqual(self.client.sent[-1], {"type": "ban", "user": SAM["id"], "days": 7, "reason": "spam",
                                                "network": False})
        self.assertEqual(self.panels(), ["admin"])
        self.page._on_received({"type": "error", "re": "ban", "message": "Not allowed."})
        self.assertEqual(self.panel("admin")["error"], "Not allowed.")
        self.page.on_turn_off()
        self.assertEqual(self.panels(), [])

    def test_transfer_panel_checks_the_password(self):
        self.welcome()
        self.page.export_transfer()
        self.act("transfer", "save", password="abc", confirm="abd")
        self.assertTrue(self.panel("transfer")["error"])
        with mock.patch.object(self.page_mod.QFileDialog, "getSaveFileName", return_value=("", "")) as picked:
            self.act("transfer", "save", password="", confirm="")
            picked.assert_not_called()
            self.answer("ok", kind="choice")                    # "Save without a password"
            picked.assert_called_once()
        self.assertEqual(self.panels(), [])

    def test_saved_chats(self):
        self.welcome()
        chat = {"other": SAM, "me": ME["id"], "room": "dm-x", "count": 3, "last": 0}
        with mock.patch.object(self.page.archive, "conversations", return_value=[chat]), \
             mock.patch.object(self.page.archive, "delete") as deleted:
            self.page.open_saved_chats()
            self.assertEqual(self.panel("saved")["chats"][0]["name"], "Sam #bbbbbb")
            self.act("saved", "delete", index=4)
            self.act("saved", "delete", index=True)
            deleted.assert_not_called()
            self.act("saved", "delete", index=0)
            self.answer("ok", kind="choice")
            deleted.assert_called_once_with(self.page._server_url(), ME["id"], "dm-x")

    def gifs_on(self):
        self.welcome()
        self.page._on_received({"type": "welcome", "user": dict(ME), "rooms": [
            {"id": "global", "name": "Global", "kind": "system"}],
            "limits": {"max_image_bytes": 400 * 1024, "image_part_chars": 24000, "image_days": 7, "gifs": True}})
        self.assertTrue(self.last("state")["gifs"])

    def wait_for(self, done, seconds=20):
        """Lets the page's threads (shrinking a picture) finish."""
        import time
        end = time.monotonic() + seconds
        while not done() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(done())

    def test_gif_search_goes_through_the_server_and_the_gif_picked_is_sent_like_a_picture(self):
        try:
            from test_network_images import animation
        except unittest.SkipTest:
            self.skipTest("Pillow not installed")
        self.gifs_on()
        self.page.on_gif_search({"q": "  happy   cat "})
        asked = self.client.sent[-1]
        self.assertEqual((asked["type"], asked["q"], asked["offset"], asked["lang"]), ("gif_search", "happy cat", 0, "en"))
        self.page._on_received({"type": "gif_results", "nonce": asked["nonce"] - 1, "results": [{"id": "old"}]})
        self.assertTrue(self.last("gif_results")["loading"])   # an older search's answer is dropped
        self.page._on_received({"type": "gif_results", "nonce": asked["nonce"], "more": True, "next": 24,
                                "results": [{"id": "abc", "w": 100, "h": 75, "title": "Cat", "user": "Maker"}]})
        shown = self.last("gif_results")
        self.assertEqual((shown["results"][0]["id"], shown["more"], shown["append"]), ("abc", True, False))
        # Previews are checked here; the page gets data: URLs only.
        gif = animation(100, 75)
        self.page._on_received({"type": "gif_thumb", "id": "abc", "data": base64.b64encode(gif).decode()})
        self.assertTrue(self.last("gif_thumb")["url"].startswith("data:image/gif;base64,"))
        self.page._on_received({"type": "gif_thumb", "id": "abc", "data": base64.b64encode(b"<svg/>").decode()})
        self.page._on_received({"type": "gif_thumb", "id": "nope", "data": base64.b64encode(gif).decode()})
        self.assertEqual(len([n for n, _p in self.events if n == "gif_thumb"]), 1)
        # More of the same search.
        self.page.on_gif_search({"q": "happy cat", "more": True})
        self.assertEqual(self.client.sent[-1]["offset"], 24)
        # Picking one: the server downloads it, Buddy shrinks it and it waits in the composer.
        self.page.on_gif_pick({"id": "not-shown"})
        self.assertEqual(self.client.sent[-1]["type"], "gif_search")
        self.page.on_gif_pick({"id": "abc"})
        self.assertEqual(self.client.sent[-1], {"type": "gif_get", "id": "abc"})
        self.assertEqual(self.last("attachment")["busy"], "Getting the GIF from GIPHY…")
        self.page.on_send({"text": "too soon"})
        self.assertEqual(self.client.sent[-1]["type"], "gif_get")   # not sent without its GIF
        self.page._on_received({"type": "gif_data", "id": "abc", "user": "Maker",
                                "data": base64.b64encode(animation(320, 240)).decode()})
        self.wait_for(lambda: self.page.attachment is not None)
        self.assertTrue(self.last("attachment")["label"].startswith("GIPHY GIF, 320 x 240"))
        self.page.on_send({"text": "ha"})
        send = self.client.sent[-1]
        self.assertEqual((send["type"], send["image"]["gif"]), ("send", "abc"))

    def test_a_gif_file_stays_animated_and_can_be_removed_while_its_being_prepared(self):
        try:
            from test_network_images import animation
        except unittest.SkipTest:
            self.skipTest("Pillow not installed")
        self.gifs_on()
        path = os.path.join(self._tmp.name, "wave.gif")
        with open(path, "wb") as f:
            f.write(animation())
        self.page._attach_file(path)
        self.assertEqual(self.last("attachment")["busy"], "Getting the GIF ready…")
        self.page.on_remove_attachment()
        self.page._attach_file(path)
        self.wait_for(lambda: self.page.attachment is not None)
        self.wait_for(lambda: not self.page._shrink_workers or not any(w.isRunning() for w in self.page._shrink_workers))
        self.app.processEvents()
        self.assertTrue(self.page.attachment["animated"])
        self.assertIsNone(self.page.attachment["gif"])
        self.page.on_send({"text": ""})
        self.assertNotIn("gif", self.client.sent[-1]["image"])

    def test_gif_errors_show_in_the_picker(self):
        self.gifs_on()
        self.page.on_gif_search({"q": "cat"})
        nonce = self.client.sent[-1]["nonce"]
        self.page._on_received({"type": "error", "code": "gif_busy", "re": "gif_search", "nonce": nonce,
                                "message": "GIF search is busy - try again in 5 minutes."})
        self.assertEqual(self.last("gif_results")["error"], "GIF search is busy - try again in 5 minutes.")

    def test_off_clears_everything(self):
        self.welcome()
        self.page.on_turn_off()
        self.assertEqual(self.last("state")["mode"], "off")
        self.assertEqual(self.last("sidebar"), [])
        self.assertIsNone(self.last("room"))


if __name__ == "__main__":
    unittest.main()
