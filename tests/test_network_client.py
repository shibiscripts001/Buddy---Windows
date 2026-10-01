"""Buddy Network in Buddy (app/pages/buddy_network/): the safety checks,
message rendering, the saved identity, and the rail placement. No Qt: the
page and its QWebSocket client aren't imported (CI installs only
PySide6-Essentials, which has no QtWebSockets)."""

import ast
import json
import os
import tempfile
import unittest

import _paths
from core import nav_layout
from pages.buddy_network import reactions, render, safety
from pages.buddy_network.identity import IdentityStore
from server import core as server_core


class LinkTests(unittest.TestCase):
    def test_find_links(self):
        text = "See https://example.com/a. Or (www.example.org), https://en.wikipedia.org/wiki/Foo_(bar)!"
        self.assertEqual([u for _s, _e, u in safety.find_links(text)],
                         ["https://example.com/a", "www.example.org",
                          "https://en.wikipedia.org/wiki/Foo_(bar)"])
        start, end, url = safety.find_links(text)[0]
        self.assertEqual(text[start:end], url)
        self.assertEqual(safety.find_links("no links here"), [])

    def test_bare_addresses_count_as_links(self):
        for text in ("go to example.com/login", "free-stuff.xyz", "BIT.LY/abc"):
            self.assertTrue(safety.contains_link(text), text)
        for text in ("notes.txt", "Resolve 19.1.4", "e.g. this", "me@example.com", "v1.2"):
            self.assertFalse(safety.contains_link(text), text)

    def test_describe_link_warnings(self):
        info = safety.describe_link("https://google.com@evil.example/login")
        self.assertEqual(info.host, "evil.example")
        self.assertIn("'@'", info.warnings[0])
        info = safety.describe_link("http://203.0.113.9/setup.exe")
        joined = " ".join(info.warnings)
        for word in ("IP number", "http, not https", "download"):
            self.assertIn(word, joined)
        self.assertTrue(safety.describe_link("https://xn--pple-43d.com").warnings)
        self.assertIn("shortener", safety.describe_link("https://bit.ly/x").warnings[0])
        plain = safety.describe_link("www.blackmagicdesign.com/products")
        self.assertEqual((plain.open_url, plain.host, plain.warnings),
                         ("https://www.blackmagicdesign.com/products", "www.blackmagicdesign.com", []))


class SendCheckTests(unittest.TestCase):
    def test_private_looking_things_warn(self):
        cases = {
            "mail me at jo.smith@example.com": "email",
            "call +1 (555) 123-4567": "phone",
            "key sk-proj-abcdefghijklmnopqrstuvwxyz123456": "API key",
            "try AIzaSyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvWx": "API key",
            "token 9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc": "API key",
            "my password is hunter2": "password",
        }
        for text, word in cases.items():
            check = safety.outgoing_warnings(text)
            self.assertFalse(check.blocked)
            self.assertTrue(any(word in w for w in check.warnings), (text, check.warnings))

    def test_everyday_editing_talk_does_not_warn(self):
        for text in ("Cut at 01:00:10:12 please", "Export 3840x2160 at 23.976", "Resolve 19.1.4 on Windows",
                     "frame 1234 to 5678", "https://www.blackmagicdesign.com/support",
                     "Use Fusion > Text+ then Color > Nodes"):
            self.assertEqual(safety.outgoing_warnings(text).warnings, [], text)

    def test_own_api_key_is_blocked(self):
        key = "AIzaSyExampleExampleExampleExample123"
        check = safety.outgoing_warnings(f"here: {key}", own_secrets=["", key])
        self.assertTrue(check.blocked)
        self.assertFalse(safety.outgoing_warnings("hello", own_secrets=[key]).blocked)

    def test_server_url(self):
        for ok in ("ws://localhost:8765", "ws://127.0.0.1:8765", "wss://chat.example.com"):
            self.assertEqual(safety.check_server_url(ok), "", ok)
        for bad in ("ws://chat.example.com", "http://localhost:8765", "chat.example.com", ""):
            self.assertTrue(safety.check_server_url(bad), bad)


COLORS = {"text": "#fff", "muted": "#888", "me": "#0f0", "other": "#fff", "link": "#0af", "warning": "#fa0"}


def msg(i, text, author="u1", name="Jo", deleted=False, ts=1_750_000_000.0):
    return {"id": i, "room": "global", "text": text, "ts": ts, "deleted": deleted,
            "author": {"id": author, "tag": author[:6], "name": name}}


class RenderTests(unittest.TestCase):
    def html(self, messages, my_id="u1", more=False):
        links = []
        out = render.room_html(messages, my_id=my_id, room_name="#Global", more=more, links=links,
                               colors=COLORS, now=1_750_000_000.0)
        return out, links

    def test_user_text_can_never_become_markup(self):
        out, _ = self.html([msg(1, '<img src="http://tracker.example/p.png"> <b>hi</b>',
                                name="<a href='x'>Admin</a>")])
        self.assertNotIn('<img src="http', out)          # only the generated avatar is an image
        self.assertEqual(out.count("<img"), 1)
        self.assertNotIn("<b>hi", out)
        self.assertNotIn("<a href='x'>", out)
        self.assertIn("&lt;img", out)

    def test_links_go_through_the_link_table(self):
        out, links = self.html([msg(1, "look https://example.com/x?a=1&b=2 now")])
        self.assertEqual(links, ["https://example.com/x?a=1&b=2"])
        self.assertIn('href="bn-link:0"', out)
        self.assertNotIn('href="https', out)
        self.assertIn(render.LINK_NOTE, out)

    def test_delete_only_on_own_and_deleted_placeholder(self):
        out, _ = self.html([msg(1, "mine"), msg(2, "theirs", author="u2", name="Sam")])
        self.assertIn('href="bn-delete:1"', out)
        self.assertNotIn("bn-delete:2", out)
        out, _ = self.html([msg(3, "", deleted=True)])
        self.assertIn("message deleted", out)
        self.assertNotIn("bn-delete:3", out)

    def test_names_open_profiles_blocked_hidden_deleted_shown(self):
        out, _ = self.html([msg(1, "mine"), msg(2, "theirs", author="u2", name="Sam")])
        self.assertIn('class="who" translate="no" href="bn-user:u2"', out)
        self.assertIn('class="face-link" href="bn-user:u2"', out)   # the avatar too
        self.assertIn('href="bn-user:u1"', out)        # your own: your profile
        links = []
        out = render.room_html([msg(1, "hello"), msg(2, "spam", author="u2", name="Sam")], my_id="u1",
                               room_name="#Global", more=False, links=links, colors=COLORS,
                               now=1_750_000_000.0, hidden={"u2"})
        self.assertIn("hello", out)
        self.assertNotIn("spam", out)
        out, _ = self.html([msg(3, "left behind", author="", name=None)])
        self.assertIn(render.DELETED_USER, out)
        self.assertNotIn("bn-user:", out)
        self.assertEqual(render.display_name({"id": "", "name": None}), render.DELETED_USER)

    def test_badges_and_admin_links(self):
        staff = msg(1, "hi", author="u2", name="Adam")
        staff["author"]["role"] = "admin"
        out, _ = self.html([staff, msg(2, "hey", author="u3", name="Admin fan")])
        self.assertEqual(out.count("ADMIN</span>"), 1)          # the role, not the name
        mod = msg(4, "hello", author="u4", name="Mona")
        mod["author"]["role"] = "mod"
        self.assertIn("MOD</span>", self.html([mod])[0])
        self.assertIn('href="bn-report:1"', out)
        self.assertNotIn('bn-delete:1', out)
        links = []
        out = render.room_html([staff], my_id="u1", room_name="#Global", more=False, links=links,
                               colors=COLORS, now=1_750_000_000.0, admin=True)
        self.assertIn('href="bn-delete:1"', out)
        out, _ = self.html([msg(3, "mine")])
        self.assertNotIn("bn-report:3", out)                    # nobody reports themselves

    def test_reactions_under_a_message(self):
        m = msg(1, "rendered!")
        sam = {"id": "u2", "tag": "u2", "name": "<b>Sam</b>"}
        m["reactions"] = [{"r": "heart", "count": 2, "mine": True, "people": [{"id": "u1", "tag": "u1", "name": "Jo"}, sam]},
                          {"r": "grin", "count": 12, "mine": False, "people": [sam]},
                          {"r": "made-up", "count": 1, "mine": False, "people": []},     # a newer server's: left out
                          {"r": "sad", "count": 1, "mine": False, "people": [sam]}]
        out = render.room_html([m], my_id="u1", room_name="#Global", more=False, links=[], colors=COLORS,
                               now=1_750_000_000.0, can_reply=True, hidden={"u2"})
        self.assertIn('class="react mine" href="bn-reaction:1:heart"', out)
        self.assertIn('&lt;3<span class="n">1</span>', out)         # Sam is blocked: not counted or named
        self.assertIn(':D<span class="n">11</span>', out)
        self.assertIn('title=" +11"', out)                          # nobody named, 11 more
        self.assertNotIn("Sam", out)
        self.assertNotIn("made-up", out)
        self.assertNotIn("bn-reaction:1:sad", out)                  # only Sam: nothing left to show
        self.assertIn('href="bn-react:1"', out)
        out, _ = self.html([m])                                     # offline: shown, but not clickable
        self.assertNotIn("bn-reaction:", out)
        self.assertNotIn("bn-react:", out)
        self.assertIn("&lt;b&gt;Sam&lt;/b&gt;", out)                # a name in a tooltip is escaped too

    def test_top_of_room(self):
        self.assertIn('href="bn-more"', self.html([msg(1, "x")], more=True)[0])
        self.assertIn("Start of #Global", self.html([msg(1, "x")])[0])


class IdentityTests(unittest.TestCase):
    def test_round_trip_per_server_and_forget(self):
        with tempfile.TemporaryDirectory() as folder:
            store = IdentityStore(folder)
            self.assertEqual(store.token("ws://localhost:8765"), "")
            store.save("ws://localhost:8765", "abc", "tok1")
            store.save("wss://chat.example.com", "def", "tok2")
            again = IdentityStore(folder)
            self.assertEqual(again.token("ws://localhost:8765"), "tok1")
            self.assertEqual(again.get("wss://chat.example.com")["id"], "def")
            again.forget("ws://localhost:8765")
            self.assertEqual(IdentityStore(folder).token("ws://localhost:8765"), "")

    def test_damaged_file_is_kept_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "identity.json"), "w") as f:
                f.write("{not json")
            store = IdentityStore(folder)
            self.assertTrue(store.warnings)
            self.assertTrue(any(n.startswith("identity.json.corrupt") for n in os.listdir(folder)))
            store.save("ws://localhost:8765", "abc", "tok")
            self.assertEqual(IdentityStore(folder).token("ws://localhost:8765"), "tok")

    def test_the_token_is_locked_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "identity.json")
            secret = "not-to-be-seen-" + "x" * 30
            with open(path, "w") as f:   # a file from before the lock: locked as soon as it's read
                json.dump({"servers": {"wss://a": {"id": "abc", "token": secret}}}, f)
            self.assertEqual(IdentityStore(folder).token("wss://a"), secret)
            for name in os.listdir(folder):   # the .bak copy too
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    self.assertNotIn(secret, f.read(), name)
            with open(path) as f:
                entry = json.load(f)["servers"]["wss://a"]
            self.assertEqual((entry["id"], "token" in entry), ("abc", False))
            if os.name == "nt":
                self.assertEqual(entry["protection"], "dpapi")
                entry["token_locked"] = entry["token_locked"][:-8] + "AAAAAAA="   # locked somewhere else
                with open(path, "w") as f:
                    json.dump({"servers": {"wss://a": entry}}, f)
                store = IdentityStore(folder)
                self.assertEqual(store.token("wss://a"), "")
                self.assertTrue(store.warnings)
                self.assertTrue(any(n.startswith("identity.json.corrupt") for n in os.listdir(folder)))


class ProtocolTests(unittest.TestCase):
    def test_client_and_server_agree(self):
        source = (_paths.APP / "pages" / "buddy_network" / "client.py").read_text(encoding="utf-8")
        consts = {t.id: node.value.value for node in ast.parse(source).body
                  if isinstance(node, ast.Assign) for t in node.targets
                  if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant)}
        self.assertEqual(consts["PROTOCOL_VERSION"], server_core.PROTOCOL_VERSION)
        self.assertEqual(consts["MAX_MESSAGE_CHARS"], server_core.MAX_MESSAGE_CHARS)

    def test_the_same_emoticons(self):
        self.assertEqual(tuple(key for key, _text, _name in reactions.REACTIONS), server_core.REACTIONS)


class Page:
    def __init__(self, tool_id):
        self.tool_id = tool_id


class RailTests(unittest.TestCase):
    REGISTRY = [("Setup", Page("setup")), ("Business", Page("time_tracker")), ("", Page("buddy_network"))]

    def test_new_tool_brings_its_plain_divider_to_a_saved_layout(self):
        saved = [{"type": "divider", "label": "Business"}, {"type": "tool", "id": "time_tracker"},
                 {"type": "divider", "label": "Setup"}, {"type": "tool", "id": "setup"}]
        out = nav_layout.reconcile(saved, self.REGISTRY)
        self.assertEqual(out[-2:], [{"type": "divider", "label": ""},
                                    {"type": "tool", "id": "buddy_network", "visible": True}])

    def test_no_duplicate_heading_for_a_known_category(self):
        registry = self.REGISTRY + [("Setup", Page("new_setup_tool"))]
        saved = [{"type": "divider", "label": "Setup"}, {"type": "tool", "id": "setup"},
                 {"type": "tool", "id": "time_tracker"}, {"type": "tool", "id": "buddy_network"}]
        out = nav_layout.reconcile(saved, registry)
        self.assertEqual(out[-1], {"type": "tool", "id": "new_setup_tool", "visible": True})
        self.assertEqual(sum(e.get("label") == "Setup" for e in out), 1)

    def test_registry_ends_with_buddy_network_under_a_plain_line(self):
        # Read, not imported: importing registry.py pulls in every page.
        tree = ast.parse((_paths.APP / "registry.py").read_text(encoding="utf-8"))
        entries = next(node.value.elts for node in tree.body if isinstance(node, ast.Assign)
                       and any(getattr(t, "id", "") == "REGISTRY" for t in node.targets))
        category, page = entries[-1].elts
        self.assertEqual((category.value, page.id), ("", "BuddyNetworkPage"))


if __name__ == "__main__":
    unittest.main()
