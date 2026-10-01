"""Buddy Network profile pages and who's-here lists (server/profiles.py),
driven with the fake sessions of test_network_server.py - no network."""

import sqlite3
import tempfile
import os
import unittest

import _paths  # noqa: F401
from server import core, profiles
from server.store import SCHEMA_VERSION, Store
from test_network_server import FakeSession, Harness, ids, uid


class ProfileTests(Harness):
    def setUp(self):
        super().setUp()
        self.core.limit_new_accounts = False

    def buddies(self, a, b):
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=uid(a))

    def profile(self, viewer, of):
        reply = self.request(viewer, type="get_profile", user=uid(of))
        self.assertEqual(reply["type"], "profile", reply)
        return reply

    def test_the_server_says_it_has_profiles(self):
        limits = self.user("Tess").last("welcome")["limits"]
        self.assertTrue(limits["profiles"])
        self.assertTrue(limits["who"])

    def test_everyone_starts_with_an_empty_profile(self):
        a, b = self.user("Ann"), self.user("Bea")
        p = self.profile(b, a)
        self.assertEqual(p["user"]["name"], "Ann")
        self.assertEqual(p["profile"], {"headline": "", "mood": "", "about": "", "working_on": "", "listening": "",
                                        "theme": ""})
        self.assertEqual((p["top"], p["buddy_count"], p["since"]), ([], 0, self.clock.now))
        self.assertNotIn("online", p)   # not their buddy

    def test_setting_a_profile_and_others_seeing_it(self):
        a, b = self.user("Ann"), self.user("Bea")
        self.request(a, type="set_profile", headline="  Colourist   by night ", mood="creative",
                     about="I grade.\n\nAnd edit.", working_on="A short film", listening="Daft Punk",
                     theme="glitter")
        self.assertEqual(a.last("profile")["profile"]["headline"], "Colourist by night")
        self.assertEqual(a.last()["type"], "profile_saved")
        p = self.profile(b, a)["profile"]
        self.assertEqual((p["about"], p["mood"], p["theme"], p["working_on"]),
                         ("I grade.\n\nAnd edit.", "creative", "glitter", "A short film"))

    def test_what_a_profile_takes(self):
        a = self.user("Ann")
        for bad in ({"mood": "furious"}, {"theme": "geocities"}, {"headline": "x" * 101},
                    {"about": "y" * 1001}, {"top": "nope"}, {"working_on": 5}):
            self.assertEqual(self.request(a, type="set_profile", **bad)["type"], "error", bad)
        self.assertEqual(self.request(a, type="set_profile")["type"], "profile")   # all empty is fine

    def test_online_only_to_buddies(self):
        a, b, c = self.user("Ann"), self.user("Bea"), self.user("Cat")
        self.buddies(a, b)
        self.assertTrue(self.profile(b, a)["online"])
        self.assertNotIn("online", self.profile(c, a))
        self.request(a, type="set_presence", offline=True)
        self.assertFalse(self.profile(b, a)["online"])

    def test_top_buddies_are_only_buddies(self):
        a, b, c, d = self.user("Ann"), self.user("Bea"), self.user("Cat"), self.user("Dot")
        self.buddies(a, b)
        self.buddies(a, c)
        self.request(a, type="set_profile", top=[uid(c), uid(d), uid(b), uid(c)])
        self.assertEqual(ids(self.profile(d, a)["top"]), [uid(c), uid(b)])
        self.assertEqual(self.profile(d, a)["buddy_count"], 2)
        self.request(a, type="buddy_remove", user=uid(c))
        self.assertEqual(ids(self.profile(d, a)["top"]), [uid(b)])   # gone once they aren't buddies
        many = [self.user(f"P{i}") for i in range(9)]
        for p in many:
            self.buddies(a, p)
        self.assertEqual(self.request(a, type="set_profile", top=[uid(p) for p in many])["code"], "too_many")

    def test_a_view_counts_once_a_day_and_never_your_own(self):
        a, b = self.user("Ann"), self.user("Bea")
        for _ in range(3):
            self.profile(b, a)
        self.request(a, type="get_profile", user=uid(a))
        self.assertEqual(self.profile(a, a)["views"], 1)
        self.clock.now += profiles.VIEW_EVERY + 1
        self.core.purge()
        self.assertEqual(self.profile(b, a)["views"], 2)

    def test_unknown_people_have_no_profile(self):
        a = self.user("Ann")
        self.assertEqual(self.request(a, type="get_profile", user="0" * 32)["code"], "no_user")

    def test_staff_clear_profiles_below_them(self):
        owner, bob, mod = self.user("Olive"), self.user("Bob"), self.user("Mo")
        self.store.set_role(uid(owner), "owner")
        self.store.set_role(uid(mod), "mod")
        self.request(bob, type="set_profile", headline="rude", about="ruder", theme="hacker", mood="grumpy")
        self.request(mod, type="set_profile", headline="mod words")
        self.assertEqual(self.request(bob, type="clear_profile", user=uid(mod))["code"], "not_allowed")
        self.assertEqual(self.request(mod, type="clear_profile", user=uid(owner))["code"], "not_allowed")
        p = self.request(mod, type="clear_profile", user=uid(bob))["profile"]
        self.assertEqual((p["headline"], p["about"], p["theme"], p["mood"]), ("", "", "hacker", "grumpy"))
        self.assertEqual(bob.last("profile")["profile"]["headline"], "")   # they see it went
        log = self.request(owner, type="admin_log")["entries"]
        self.assertEqual((log[0]["action"], log[0]["target"]), ("clear_profile", uid(bob)))

    def test_deleting_an_account_deletes_its_profile(self):
        a = self.user("Ann")
        self.request(a, type="set_profile", headline="hi")
        self.request(a, type="delete_account")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)


class WhoTests(Harness):
    def setUp(self):
        super().setUp()
        self.core.limit_new_accounts = False

    def test_joining_a_public_room_gets_the_list_others_get_it_batched(self):
        a = self.user("Ann")
        self.assertEqual([p["name"] for p in a.last("who")["people"]], ["Ann"])
        b = self.user("Bea")
        self.assertEqual([p["name"] for p in b.last("who")["people"]], ["Ann", "Bea"])
        self.assertEqual(len(a.last("who")["people"]), 1)   # not until the flush
        self.core.flush_who()
        self.assertEqual([p["name"] for p in a.last("who")["people"]], ["Ann", "Bea"])
        before = len(a.inbox)
        self.core.flush_who()
        self.assertEqual(len(a.inbox), before)   # nothing changed: nothing sent

    def test_leaving_disconnecting_and_appearing_offline(self):
        a, b, c = self.user("Ann"), self.user("Bea"), self.user("Cat")
        self.request(b, type="leave", room="global")
        self.core.disconnect(c)
        self.core.flush_who()
        self.assertEqual([p["name"] for p in a.last("who")["people"]], ["Ann"])
        d = self.user("Dot")
        self.request(d, type="set_presence", offline=True)
        self.core.flush_who()
        self.assertEqual((a.last("who")["total"], [p["name"] for p in a.last("who")["people"]]), (1, ["Ann"]))

    def test_names_and_avatars_follow(self):
        a, b = self.user("Ann"), self.user("Bea")
        self.core.flush_who()
        self.clock.now += 5
        self.request(b, type="set_name", name="Beatrix")
        self.core.flush_who()
        self.assertIn("Beatrix", [p["name"] for p in a.last("who")["people"]])

    def test_nameless_people_and_user_rooms_have_no_list(self):
        a = self.user("Ann")
        nameless = self.user(None)
        self.core.flush_who()
        self.assertEqual([p["name"] for p in a.last("who")["people"]], ["Ann"])
        room = self.request(a, type="create_room", name="Ann's Room")["room"]["id"]
        before = len([p for p in a.inbox if p["type"] == "who"])
        self.request(a, type="join", room=room)
        self.core.flush_who()
        self.assertEqual(len([p for p in a.inbox if p["type"] == "who"]), before)
        del nameless

    def test_one_person_on_two_buddys_is_listed_once(self):
        a = self.user("Ann")
        again = FakeSession()
        self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=a.token)
        self.request(again, type="join", room="global")
        self.assertEqual([p["name"] for p in again.last("who")["people"]], ["Ann"])


class ProfileMigrationTests(unittest.TestCase):
    def test_an_older_database_gets_profiles(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "old.db")
        Store(path).close()
        db = sqlite3.connect(path)
        db.execute("DROP TABLE profiles")
        db.execute("UPDATE meta SET value = '14' WHERE key = 'schema'")
        db.commit()
        db.close()
        store = Store(path)
        store.create_user("a" * 32, "hash", 1.0)
        self.assertEqual(store.profile("a" * 32)["views"], 0)
        self.assertEqual(store.db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0],
                         str(SCHEMA_VERSION))
        store.close()


if __name__ == "__main__":
    unittest.main()
