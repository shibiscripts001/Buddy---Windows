"""Buddy Network profile pages and who's-here lists in Buddy: what the
profile window and the list show (pages/buddy_network/profiles.py, no Qt),
the "+" after a message's reactions, and the page asking for profiles,
saving yours and drawing the list (profile_page.py) - with a fake
connection, as in test_network_page.py."""

import unittest

import _paths  # noqa: F401
from pages.buddy_network import profiles, render
from server import profiles as server_profiles
import test_network_page as base   # not its classes by name: they'd run again here

HAVE_QT, ME, SAM = base.HAVE_QT, base.ME, base.SAM

COLORS = {"text": "#fff", "muted": "#888", "me": "#0f0", "other": "#fff", "link": "#0af", "warning": "#fa0"}


def answer(user, **profile):
    return {"type": "profile", "user": dict(user, role=user.get("role", "user")), "since": 1_750_000_000.0,
            "views": 3, "buddy_count": 1, "profile": {**profiles.EMPTY, **profile}, "top": []}


class InStepTests(unittest.TestCase):
    def test_moods_themes_and_limits_match_the_server(self):
        self.assertEqual(tuple(k for k, _n, _f in profiles.MOODS), server_profiles.MOODS)
        self.assertEqual(tuple(k for k, _n in profiles.THEMES), server_profiles.PROFILE_THEMES)
        for name in ("HEADLINE_MAX", "ABOUT_MAX", "LINE_MAX", "TOP_MAX"):
            self.assertEqual(getattr(profiles, name), getattr(server_profiles, name), name)

    def test_every_theme_is_drawn(self):
        import os
        with open(os.path.join(os.path.dirname(profiles.__file__), "web", "network.css"), encoding="utf-8") as fh:
            css = fh.read()
        for key, _name in profiles.THEMES:
            if key != profiles.DEFAULT_THEME:   # classic is .profile's own look
                self.assertIn(f'[data-theme="{key}"]', css, key)


class ViewTests(unittest.TestCase):
    def test_loading_then_their_page(self):
        p = {"user": SAM["id"], "data": None}
        self.assertTrue(profiles.profile_view(p, me_id=ME["id"], actions=[], buddies=[])["loading"])
        p["data"] = answer(SAM, headline="hi", mood="silly", theme="glitter", about="About\nme")
        v = profiles.profile_view(p, me_id=ME["id"], actions=[{"id": "add"}], buddies=[])
        self.assertEqual((v["theme"], v["headline"], v["mood"], v["about"]),
                         ("glitter", "hi", {"name": "silly", "face": ":P"}, "About\nme"))
        self.assertEqual((v["views"], v["top_note"], v["network"]),
                         ("3 profile views", "1 buddy", "Sam is in your extended network."))
        self.assertIsNone(v["online"])           # not a buddy: not told
        self.assertFalse(v["editing"])

    def test_what_isnt_one_of_buddys_is_left_out(self):
        p = {"user": SAM["id"], "data": answer(SAM, mood="<b>", theme="url(evil)", headline=5)}
        v = profiles.profile_view(p, me_id=ME["id"], actions=[], buddies=[])
        self.assertEqual((v["mood"], v["theme"], v["headline"]), (None, "classic", ""))

    def test_editing_your_own(self):
        p = {"user": ME["id"], "data": answer(ME, theme="hacker"), "editing": True, "draft": None}
        v = profiles.profile_view(p, me_id=ME["id"], actions=[], buddies=[SAM])
        self.assertTrue(v["editing"])
        self.assertEqual(v["draft"]["theme"], "hacker")
        self.assertEqual([b["id"] for b in v["buddies"]], [SAM["id"]])
        self.assertEqual(len(v["themes"]), len(profiles.THEMES))
        p["user"], p["data"] = SAM["id"], answer(SAM)
        self.assertFalse(profiles.profile_view(p, me_id=ME["id"], actions=[], buddies=[])["editing"])   # not theirs

    def test_what_the_form_sends(self):
        draft, problem = profiles.draft_from({"headline": "  a   b ", "mood": "nope", "theme": "ocean",
                                              "top": [SAM["id"], "stranger", SAM["id"]], "about": " x "}, [SAM["id"]])
        self.assertEqual(problem, "")
        self.assertEqual((draft["headline"], draft["mood"], draft["theme"], draft["top"], draft["about"]),
                         ("a b", "", "ocean", [SAM["id"]], "x"))
        self.assertIn("at most", profiles.draft_from({"headline": "h" * 101}, [])[1])

    def test_who_view_leaves_out_who_you_blocked_and_puts_staff_first(self):
        msg = {"type": "who", "room": "global", "total": 3,
               "people": [dict(SAM), dict(ME), {"id": "c" * 32, "tag": "cccccc", "name": "Al", "role": "mod"}]}
        v = profiles.who_view(msg, hidden={SAM["id"]}, me_id=ME["id"])
        self.assertEqual([p["name"] for p in v["people"]], ["Al", "Tess"])
        self.assertEqual(v["count"], "2 here")
        self.assertTrue(v["people"][1]["me"])


class RenderTests(unittest.TestCase):
    def test_a_plus_after_the_reactions(self):
        m = {"id": 7, "room": "global", "text": "hi", "ts": 1_750_000_000.0, "deleted": False,
             "author": {"id": "u2", "tag": "u2", "name": "Sam"},
             "reactions": [{"r": "heart", "count": 1, "mine": False, "people": [{"id": "u3", "name": "Al"}]}]}
        out = render.room_html([m], my_id="u1", room_name="#Global", more=False, links=[], colors=COLORS,
                               now=1_750_000_000.0, can_reply=True)
        self.assertIn('class="react add" href="bn-react:7"', out)
        out = render.room_html([m], my_id="u1", room_name="#Global", more=False, links=[], colors=COLORS,
                               now=1_750_000_000.0, can_reply=False)   # can't react: no +
        self.assertNotIn("react add", out)
        m["reactions"] = []
        out = render.room_html([m], my_id="u1", room_name="#Global", more=False, links=[], colors=COLORS,
                               now=1_750_000_000.0, can_reply=True)   # no reactions: no row, no +
        self.assertNotIn("react add", out)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ProfilePageTests(unittest.TestCase):
    setUp = base.PageTests.setUp
    last = base.PageTests.last
    answer = base.PageTests.answer

    def welcome(self, profiles_on=True):
        base.PageTests.welcome(self)
        if profiles_on:   # a server with profiles says so in its welcome
            self.page._on_received({"type": "welcome", "user": dict(ME), "rooms": [
                {"id": "global", "name": "Global", "kind": "system"}],
                "limits": {"profiles": True, "who": True}})

    def panel(self, kind="profile"):
        return next((p for p in self.last("panels") or [] if p["kind"] == kind), None)

    def test_a_name_opens_their_profile(self):
        self.welcome()
        self.page.on_anchor({"href": f"bn-user:{SAM['id']}"})
        self.assertEqual(self.client.sent[-1], {"type": "get_profile", "user": SAM["id"]})
        self.assertTrue(self.panel()["loading"])
        self.page._on_received(answer(SAM, headline="Grading all day"))
        view = self.panel()
        self.assertEqual(view["headline"], "Grading all day")
        self.assertIn("add", [a["id"] for a in view["actions"]])
        self.page.on_panel_action({"kind": "profile", "action": "add"})
        self.assertEqual(self.client.sent[-1], {"type": "buddy_request", "user": SAM["id"]})

    def test_a_server_without_profiles_keeps_the_name_menu(self):
        self.welcome(profiles_on=False)
        self.page.on_anchor({"href": f"bn-user:{SAM['id']}", "x": 1, "y": 2})
        self.assertIn("Add as buddy", [i.get("label") for i in self.last("menu")["items"]])
        self.assertFalse(any(p.get("type") == "get_profile" for p in self.client.sent))

    def test_editing_and_saving_your_own(self):
        self.welcome()
        self.page.on_profile({"user": "me"})
        self.assertEqual(self.client.sent[-1], {"type": "get_profile", "user": ME["id"]})
        self.page._on_received(answer(ME))
        self.assertIn("edit", [a["id"] for a in self.panel()["actions"]])
        self.page.on_panel_action({"kind": "profile", "action": "edit"})
        self.assertTrue(self.panel()["editing"])
        self.page.on_panel_action({"kind": "profile", "action": "save", "headline": "x" * 200})
        self.assertIn("at most", self.panel()["error"])           # caught before it's sent
        self.page.on_panel_action({"kind": "profile", "action": "save", "headline": "Hello", "theme": "retro",
                                   "mood": "happy", "about": "", "working_on": "", "listening": "", "top": []})
        sent = self.client.sent[-1]
        self.assertEqual((sent["type"], sent["headline"], sent["theme"]), ("set_profile", "Hello", "retro"))
        self.page._on_received(answer(ME, headline="Hello", theme="retro"))
        self.page._on_received({"type": "profile_saved"})
        self.assertFalse(self.panel()["editing"])
        self.assertEqual(self.panel()["theme"], "retro")

    def test_an_error_shows_in_the_window(self):
        self.welcome()
        self.page.on_anchor({"href": f"bn-user:{SAM['id']}"})
        self.page._on_received({"type": "error", "code": "no_user", "message": "There's no one with that ID.",
                                "re": "get_profile"})
        self.assertEqual(self.panel()["error"], "There's no one with that ID.")

    def test_who_is_here_in_public_rooms(self):
        self.welcome()
        self.page._on_received({"type": "who", "room": "global", "total": 2, "people": [dict(ME), dict(SAM)]})
        who = self.last("who")
        self.assertTrue(who["available"] and who["open"])
        self.assertEqual([p["name"] for p in who["people"]], ["Sam", "Tess"])
        self.page.on_people_toggle()
        self.assertFalse(self.last("who")["open"])
        self.page._on_received({"type": "buddy_list", **{k: [] for k in ("buddies", "incoming", "outgoing")},
                                "blocked": [dict(SAM)]})
        self.page.on_people_toggle()
        self.assertEqual([p["name"] for p in self.last("who")["people"]], ["Tess"])   # blocked: left out

    def test_no_list_in_a_users_room(self):
        self.welcome()
        self.page.rooms["r1"] = {"id": "r1", "name": "Mine", "kind": "user", "owner": dict(ME)}
        self.page._on_received({"type": "who", "room": "r1", "total": 1, "people": [dict(ME)]})
        self.page.room_id = "r1"
        self.page._push_who()
        self.assertFalse(self.last("who")["available"])


if __name__ == "__main__":
    unittest.main()
