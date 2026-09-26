"""App announcements: the checker's parsing and address rules
(app/core/announcements.py, app/core/buddy_server.py) and the server side
(server/core.py) - no network needed."""

import json
import unittest

import _paths  # noqa: F401
from core import announcements as ann
from core.buddy_server import http_url
from server import core
from server.store import Store


def answer(items) -> bytes:
    return json.dumps({"announcements": items}).encode("utf-8")


class ParseTests(unittest.TestCase):
    def test_newest_first_capped_and_trimmed(self):
        items = [{"id": i, "ts": 1.0, "title": f" T{i} ", "text": "x" * 2000} for i in range(15)]
        out = ann.parse(answer(items))
        self.assertEqual([a["id"] for a in out], list(range(14, 4, -1)))
        self.assertEqual((out[0]["title"], len(out[0]["text"])), ("T14", ann.TEXT_MAX))

    def test_anything_unexpected_is_dropped(self):
        good = {"id": 1, "ts": 1.0, "title": "Hi", "text": "there"}
        for bad in ({"id": "1", "ts": 1, "title": "a", "text": "b"}, {"id": True, "ts": 1, "title": "a", "text": "b"},
                    {"id": 2, "ts": "x", "title": "a", "text": "b"}, {"id": 3, "ts": 1, "title": " ", "text": "b"},
                    {"id": 4, "ts": 1, "title": "a", "text": None}, "not a dict"):
            self.assertEqual([a["id"] for a in ann.parse(answer([good, bad]))], [1], bad)
        for broken in (b"not json", b"[]", b'{"announcements": "no"}', b"\xff\xfe", b"x" * (ann.MAX_BYTES + 1)):
            self.assertIsNone(ann.parse(broken), broken[:20])

    def test_unseen_means_newer_than_the_last_opened(self):
        items = ann.parse(answer([{"id": 7, "ts": 1, "title": "a", "text": "b"}]))
        self.assertTrue(ann.has_unseen(items, 0))
        self.assertTrue(ann.has_unseen(items, 6))
        self.assertFalse(ann.has_unseen(items, 7))
        self.assertFalse(ann.has_unseen([], 0))

    def test_address_follows_the_chat_server_and_stays_encrypted(self):
        self.assertEqual(http_url("wss://chat.example.com", ann.PATH), "https://chat.example.com/announcements.json")
        self.assertEqual(http_url("ws://localhost:8765", ann.PATH), "http://localhost:8765/announcements.json")
        for unsafe in ("ws://chat.example.com", "http://chat.example.com", "", "chat.example.com"):
            self.assertEqual(http_url(unsafe, ann.PATH), "", unsafe)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.core = core.NetworkCore(self.store, limit_new_accounts=False)

    def tearDown(self):
        self.store.close()

    def session(self, name, role="user"):
        from test_network_server import FakeSession
        s = FakeSession()
        self.core.handle(s, json.dumps({"type": "hello", "v": core.PROTOCOL_VERSION}))
        self.core.handle(s, json.dumps({"type": "set_name", "name": name}))
        self.store.set_role(s.user_id, role)
        return s

    def test_only_the_owner_posts_and_the_file_says_nothing_about_them(self):
        owner, admin = self.session("Olive", "owner"), self.session("Adam", "admin")
        self.core.handle(admin, json.dumps({"type": "post_app_announcement", "title": "Hi", "text": "x"}))
        self.assertEqual(admin.last()["code"], "not_allowed")
        self.core.handle(owner, json.dumps({"type": "post_app_announcement", "title": " Buddy 2.1 ",
                                            "text": "New:\n- faster"}))
        posted = owner.last("app_announcements")["announcements"]
        self.assertEqual([(a["title"], a["text"]) for a in posted], [("Buddy 2.1", "New:\n- faster")])
        public = json.loads(self.core.announcements_json())["announcements"]
        self.assertEqual(set(public[0]), {"id", "ts", "title", "text"})   # no "by"
        self.assertEqual(ann.parse(self.core.announcements_json().encode("utf-8"))[0]["title"], "Buddy 2.1")
        self.core.handle(owner, json.dumps({"type": "delete_app_announcement", "id": public[0]["id"]}))
        self.assertEqual(json.loads(self.core.announcements_json())["announcements"], [])
        self.core.handle(owner, json.dumps({"type": "post_app_announcement", "title": "", "text": "x"}))
        self.assertEqual(owner.last()["code"], "bad_request")


if __name__ == "__main__":
    unittest.main()
