"""Buddy Network server logic (server/core.py), driven with fake sessions -
no network and no `websockets` package needed. Encrypted DMs here are
random bytes in the right shape: the server can't read them anyway (the
real encryption is tested in test_e2e.py)."""

import base64
import json
import os
import unittest

import _paths  # noqa: F401
from server import core, gifs
from server.store import SCHEMA_VERSION, Store


class FakeSession(core.Session):
    def __init__(self, ip="10.0.0.1"):
        super().__init__(ip)
        self.inbox = []

    def send(self, payload):
        self.inbox.append(payload)

    def last(self, kind=None):
        for p in reversed(self.inbox):
            if kind is None or p["type"] == kind:
                return p
        raise AssertionError(f"no {kind!r} in {self.inbox}")


def b64(n: int) -> str:
    return base64.b64encode(os.urandom(n)).decode("ascii")


def enc(device: str, to=("0123456789abcdef",)) -> dict:
    """What a Buddy sends for a DM, as far as the server can tell."""
    return {"v": core.ENC_VERSION, "from": device, "salt": b64(16), "body": b64(40),
            "keys": {d: b64(core.ENC_WRAP_BYTES) for d in to}}


class Clock:
    def __init__(self):
        self.now = 1_750_000_000.0

    def __call__(self):
        return self.now


class Harness(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:")
        self.core = core.NetworkCore(self.store, clock=self.clock)

    def tearDown(self):
        self.store.close()

    def request(self, session, **msg):
        """The first thing sent back - the answer (a buddy list, say, may follow)."""
        before = len(session.inbox)
        self.core.handle(session, json.dumps(msg))
        return session.inbox[before] if len(session.inbox) > before else None

    def user(self, name="Tester", ip="10.0.0.1", join="global"):
        s = FakeSession(ip)
        welcome = self.request(s, type="hello", v=core.PROTOCOL_VERSION)
        self.assertEqual(welcome["type"], "welcome", welcome)
        s.token = welcome["token"]
        if name:
            self.assertEqual(self.request(s, type="set_name", name=name)["type"], "name_set")
        if join:
            self.request(s, type="join", room=join)
        return s

    def register(self, session, key=None) -> str:
        """Registers a PC's key for the session; returns its device id."""
        reply = self.request(session, type="set_device_key", key=key or b64(32))
        self.assertEqual(reply["type"], "device_registered", reply)
        session.device = reply["device"]
        return reply["device"]


class IdentityTests(Harness):
    def test_new_identity_then_reconnect_with_token(self):
        a = self.user("Tess")
        uid = a.last("welcome")["user"]["id"]
        self.assertEqual(len(uid), 32)
        again = FakeSession()
        welcome = self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=a.token)
        self.assertEqual(welcome["user"], {"id": uid, "tag": uid[:6], "name": "Tess", "role": "user",
                                            "avatar": ""})
        self.assertNotIn("token", welcome)  # only handed out once
        self.assertEqual([r["id"] for r in welcome["rooms"]], ["global", "help"])

    def test_token_is_not_stored_in_plain_text(self):
        a = self.user()
        stored = self.store.db.execute("SELECT token_hash FROM users").fetchone()[0]
        self.assertNotEqual(stored, a.token)
        self.assertEqual(stored, core.hash_token(a.token))

    def test_unknown_token_old_version_and_no_hello(self):
        s = FakeSession()
        err = self.request(s, type="hello", v=core.PROTOCOL_VERSION, token="made-up")
        self.assertEqual((err["code"], s.close_requested), ("bad_token", True))
        for old in (0, 1):   # 1: a Buddy from before encrypted DMs
            s = FakeSession()
            self.assertEqual(self.request(s, type="hello", v=old)["code"], "update_required")
        s = FakeSession()
        self.assertEqual(self.request(s, type="join", room="global")["code"], "not_authenticated")

    def test_new_identities_are_limited_per_network(self):
        for _ in range(3):
            self.user(name=None, join=None, ip="10.9.9.9")
        s = FakeSession("10.9.9.9")
        self.assertEqual(self.request(s, type="hello", v=core.PROTOCOL_VERSION)["code"], "rate_limited")
        self.user(name=None, join=None, ip="10.8.8.8")   # another network is fine
        self.clock.now += 86401
        self.user(name=None, join=None, ip="10.9.9.9")   # and so is tomorrow

    def test_no_ip_address_is_ever_stored(self):
        a = self.user(ip="203.0.113.77")
        self.request(a, type="send", room="global", text="hi")
        for table in ("users", "rooms", "messages", "meta"):
            for row in self.store.db.execute(f"SELECT * FROM {table}"):
                self.assertNotIn("203.0.113.77", " ".join(str(v) for v in row))


class NameTests(Harness):
    def test_valid_names(self):
        self.assertEqual(core.clean_name("  Tess   B "), "Tess B")
        self.assertEqual(core.clean_name("Zoë_2"), "Zoë_2")
        self.assertEqual(core.clean_name("김민준"), "김민준")
        self.assertEqual(core.clean_name("BuddyFan"), "BuddyFan")

    def test_rejected_names(self):
        for bad in ("A", "x" * 25, "hi<b>", "-dash", "Admin", "ADM1N", "a.d.m.i.n", "Buddy",
                    "System", "Official Help", "Moderator Jo", "zero\u200bwidth", None, 5):
            with self.assertRaises(core.RequestError, msg=repr(bad)):
                core.clean_name(bad)

    def test_name_changes_are_limited(self):
        s = self.user("Name0")
        for i in range(1, 5):
            self.assertEqual(self.request(s, type="set_name", name=f"Name{i}")["type"], "name_set")
        self.assertEqual(self.request(s, type="set_name", name="Name9")["code"], "rate_limited")
        self.assertEqual(self.request(s, type="set_name", name="Name4")["type"], "name_set")  # unchanged: free


class MessageTests(Harness):
    def test_send_reaches_room_members_only(self):
        a, b = self.user("Alice"), self.user("Bob")
        c = self.user("Cara", join="help")
        self.request(a, type="send", room="global", text="Hello!", nonce=7)
        mine = a.last("message")
        self.assertEqual(mine["nonce"], 7)
        self.assertEqual(mine["message"]["text"], "Hello!")
        self.assertEqual(mine["message"]["author"]["name"], "Alice")
        self.assertNotIn("nonce", b.last("message"))
        self.assertEqual(b.last("message")["message"]["id"], mine["message"]["id"])
        self.assertFalse(any(p["type"] == "message" for p in c.inbox))

    def test_must_join_and_have_a_name(self):
        a = self.user("Alice", join=None)
        self.assertEqual(self.request(a, type="send", room="global", text="x")["code"], "not_joined")
        b = self.user(name=None)
        err = self.request(b, type="send", room="global", text="x", nonce=1)
        self.assertEqual((err["code"], err["nonce"]), ("no_name", 1))
        self.assertEqual(self.request(a, type="join", room="nowhere")["code"], "no_room")

    def test_text_is_cleaned_and_capped(self):
        a = self.user()
        self.request(a, type="send", room="global", text="  evil\u202egpj.exe\x07\r\n\n\n\n\n\nok  ")
        self.assertEqual(a.last("message")["message"]["text"], "evilgpj.exe\n\n\nok")
        self.assertEqual(self.request(a, type="send", room="global", text=" \n ")["code"], "bad_message")
        long = "x" * (core.MAX_MESSAGE_CHARS + 1)
        self.assertEqual(self.request(a, type="send", room="global", text=long)["code"], "too_long")

    def test_sending_is_rate_limited(self):
        a = self.user()
        for i in range(5):
            self.assertEqual(self.request(a, type="send", room="global", text=str(i))["type"], "message")
        err = self.request(a, type="send", room="global", text="too fast")
        self.assertEqual(err["code"], "rate_limited")
        self.assertGreater(err["retry_after"], 0)
        self.clock.now += 10.5
        self.assertEqual(self.request(a, type="send", room="global", text="ok")["type"], "message")

    def test_only_the_sender_can_delete(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="send", room="global", text="oops")
        mid = a.last("message")["message"]["id"]
        self.assertEqual(self.request(b, type="delete", id=mid)["code"], "not_allowed")
        self.request(a, type="delete", id=mid)
        self.assertEqual(b.last("deleted"), {"type": "deleted", "room": "global", "id": mid})
        rows, _ = self.store.history("global", None, 10)
        self.assertEqual((rows[0]["text"], rows[0]["deleted"]), ("", 1))
        self.request(b, type="join", room="global")
        shown = b.last("history")["messages"][0]
        self.assertEqual((shown["deleted"], shown["text"]), (True, ""))

    def test_history_pages_and_30_day_purge(self):
        a = self.user()
        for i in range(core.HISTORY_PAGE + 5):
            self.clock.now += 3   # stay under the send limit
            self.request(a, type="send", room="global", text=f"m{i}")
        self.request(a, type="join", room="global")
        page = a.last("history")
        self.assertTrue(page["more"])
        self.assertEqual(len(page["messages"]), core.HISTORY_PAGE)
        self.assertEqual(page["messages"][-1]["text"], f"m{core.HISTORY_PAGE + 4}")
        self.request(a, type="history", room="global", before=page["messages"][0]["id"])
        older = a.last("history")
        self.assertFalse(older["more"])
        self.assertEqual([m["text"] for m in older["messages"]], [f"m{i}" for i in range(5)])

        self.clock.now += core.HISTORY_DAYS * 86400 + 1
        self.request(a, type="send", room="global", text="new")
        self.assertEqual(self.core.purge(), core.HISTORY_PAGE + 5)
        rows, _ = self.store.history("global", None, 100)
        self.assertEqual([r["text"] for r in rows], ["new"])

    def test_disconnected_sessions_get_nothing(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.core.disconnect(b)
        before = len(b.inbox)
        self.request(a, type="send", room="global", text="anyone?")
        self.assertEqual(len(b.inbox), before)

    def test_bad_frames(self):
        s = FakeSession()
        self.core.handle(s, "not json")
        self.core.handle(s, "[1, 2]")
        self.core.handle(s, json.dumps({"type": "fly"}))
        self.assertEqual([p["code"] for p in s.inbox], ["bad_request"] * 3)


class RoomTests(Harness):
    def make(self, session, name, topic=""):
        return self.request(session, type="create_room", name=name, topic=topic)

    def test_create_find_and_chat_in_a_room(self):
        a, b = self.user("Alice"), self.user("Bob")
        created = self.make(a, "Colour Grading", "  LUTs,\nnodes   and looks ")
        self.assertEqual(created["type"], "room_created")
        room = created["room"]
        self.assertEqual((room["name"], room["topic"], room["kind"]),
                         ("Colour Grading", "LUTs, nodes and looks", "user"))
        self.assertEqual(room["owner"]["name"], "Alice")
        self.assertFalse(any(p["type"] == "room_created" for p in b.inbox))  # only the maker is told

        found = self.request(b, type="find_rooms", query="colour")
        self.assertEqual([r["id"] for r in found["rooms"]], [room["id"]])
        self.assertEqual(found["rooms"][0]["here"], 0)
        self.request(b, type="join", room=room["id"])
        self.request(b, type="send", room=room["id"], text="hi")
        self.assertEqual(self.request(a, type="find_rooms", query="")["rooms"][0]["here"], 1)
        self.assertEqual(self.request(b, type="find_rooms", query="nodes")["rooms"][0]["id"], room["id"])
        self.assertEqual(self.request(b, type="find_rooms", query="50%")["rooms"], [])

        again = FakeSession()
        welcome = self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=a.token)
        self.assertEqual([r["id"] for r in welcome["rooms"]], ["global", "help"])
        self.assertEqual([r["id"] for r in welcome["my_rooms"]], [room["id"]])

    def test_room_names(self):
        self.assertEqual(core.clean_room_name("  Fusion   Tips & Tricks "), "Fusion Tips & Tricks")
        for bad, taken in (("x", ()), ("y" * 33, ()), ("#hash", ()), ("-dash", ()), ("Admins", ()),
                           ("G1obal", ("Global",)), ("help", ("Help",)), ("colour grading", ("Colour Grading",))):
            with self.assertRaises(core.RequestError, msg=bad):
                core.clean_room_name(bad, taken)
        with self.assertRaises(core.RequestError):
            core.clean_topic("t" * (core.TOPIC_MAX + 1))
        self.assertEqual(core.clean_topic("a\u202eb\tc"), "ab c")

    def test_limits_and_needs_a_name(self):
        nameless = self.user(name=None)
        self.assertEqual(self.make(nameless, "Room One")["code"], "no_name")
        a = self.user("Alice")
        for i in range(core.MAX_ROOMS_PER_USER):
            self.assertEqual(self.make(a, f"Room {i}")["type"], "room_created")
        self.assertEqual(self.make(a, "One more")["code"], "too_many_rooms")
        self.assertEqual(self.make(a, "Room 0")["code"], "bad_room")   # taken, before the count

    def test_only_the_maker_can_change_or_delete(self):
        a, b = self.user("Alice"), self.user("Bob")
        room = self.make(a, "Editing")["room"]["id"]
        self.assertEqual(self.request(b, type="set_topic", room=room, topic="mine now")["code"], "not_allowed")
        self.assertEqual(self.request(b, type="delete_room", room=room)["code"], "not_allowed")
        self.assertEqual(self.request(a, type="delete_room", room="global")["code"], "not_allowed")
        self.request(a, type="set_topic", room=room, topic="Cutting and trimming")
        self.assertEqual(b.last("room_updated")["room"]["topic"], "Cutting and trimming")

        self.request(b, type="join", room=room)
        self.request(a, type="send", room=room, text="bye")
        self.request(a, type="delete_room", room=room)
        self.assertEqual(b.last("room_removed"), {"type": "room_removed", "room": room, "reason": "deleted"})
        self.assertEqual(self.request(b, type="send", room=room, text="hello?")["code"], "no_room")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages WHERE room = ?",
                                               (room,)).fetchone()[0], 0)
        self.assertEqual(self.make(a, "Editing")["type"], "room_created")   # the name is free again

    def test_idle_rooms_expire_active_ones_stay(self):
        a, b = self.user("Alice"), self.user("Bob")
        quiet = self.make(a, "Quiet")["room"]["id"]
        busy = self.make(a, "Busy")["room"]["id"]
        self.request(a, type="join", room=busy)
        self.clock.now += (core.ROOM_IDLE_DAYS - 1) * 86400
        self.request(a, type="send", room=busy, text="still here")
        self.clock.now += 2 * 86400
        self.core.purge()
        self.assertIsNone(self.store.room(quiet))
        self.assertIsNotNone(self.store.room(busy))
        self.assertEqual(b.last("room_removed"), {"type": "room_removed", "room": quiet, "reason": "expired"})
        self.assertIsNotNone(self.store.room("global"))   # system rooms never expire

    def test_a_permanent_room_survives_being_idle(self):
        a, b = self.user("Alice"), self.user("Bob")
        kept = self.make(a, "Kept")["room"]["id"]
        quiet = self.make(a, "Also quiet")["room"]["id"]
        self.assertEqual(self.request(b, type="set_permanent", room=kept, value=True)["code"], "not_allowed")
        updated = self.request(a, type="set_permanent", room=kept, value=True)
        self.assertTrue(updated["room"]["permanent"])
        self.assertFalse(self.request(a, type="set_permanent", room=quiet, value=False)["room"]["permanent"])
        self.clock.now += (core.ROOM_IDLE_DAYS + 1) * 86400
        self.core.purge()
        self.assertIsNotNone(self.store.room(kept))
        self.assertIsNone(self.store.room(quiet))
        # Turning it back off leaves it exposed to the very next purge.
        self.request(a, type="set_permanent", room=kept, value=False)
        self.core.purge()
        self.assertIsNone(self.store.room(kept))

    def test_find_rooms_can_be_narrowed_to_permanent_ones(self):
        a = self.user("Alice")
        kept = self.make(a, "Evergreen")["room"]["id"]
        self.make(a, "Seasonal")
        self.request(a, type="set_permanent", room=kept, value=True)
        narrowed = self.request(a, type="find_rooms", query="", permanent_only=True)
        self.assertEqual([r["id"] for r in narrowed["rooms"]], [kept])
        self.assertTrue(narrowed["permanent_only"])
        everything = self.request(a, type="find_rooms", query="")
        self.assertEqual(len(everything["rooms"]), 2)

    def test_get_rooms_reports_missing(self):
        a = self.user("Alice")
        room = self.make(a, "Sound")["room"]["id"]
        info = self.request(a, type="get_rooms", ids=[room, "rgone", room])
        self.assertEqual(([r["id"] for r in info["rooms"]], info["missing"]), ([room], ["rgone"]))
        self.assertEqual(self.request(a, type="get_rooms", ids="nope")["code"], "bad_request")


def uid(session):
    return session.user_id


def ids(people):
    return [p["id"] for p in people]


class BuddyTests(Harness):
    def test_request_accept_and_online_status(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="buddy_request", user=uid(b))
        self.assertEqual(ids(a.last("buddy_list")["outgoing"]), [uid(b)])
        self.assertEqual(ids(b.last("buddy_list")["incoming"]), [uid(a)])
        self.request(b, type="buddy_accept", user=uid(a))
        for me, them in ((a, b), (b, a)):
            buddies = me.last("buddy_list")["buddies"]
            self.assertEqual([(x["id"], x["online"]) for x in buddies], [(uid(them), True)])
            self.assertEqual(me.last("buddy_list")["incoming"] + me.last("buddy_list")["outgoing"], [])

        self.request(b, type="set_presence", offline=True)
        self.assertFalse(a.last("buddy_list")["buddies"][0]["online"])
        self.assertTrue(b.last("buddy_list")["appear_offline"])
        self.request(b, type="set_presence", offline=False)
        self.assertTrue(a.last("buddy_list")["buddies"][0]["online"])
        self.core.disconnect(b)
        self.assertFalse(a.last("buddy_list")["buddies"][0]["online"])

    def test_names_follow_renames(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="set_name", name="Bobby")
        self.assertEqual(a.last("buddy_list")["outgoing"][0]["name"], "Bobby")

    def test_asking_someone_who_asked_you_makes_you_buddies(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_request", user=uid(a))
        self.assertEqual(ids(a.last("buddy_list")["buddies"]), [uid(b)])

    def test_decline_and_block_look_the_same_as_waiting(self):
        a, b, c = self.user("Alice"), self.user("Bob"), self.user("Cara")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_decline", user=uid(a))
        self.assertEqual(b.last("buddy_list")["incoming"], [])
        self.assertEqual(ids(a.last("buddy_list")["outgoing"]), [uid(b)])   # still "waiting"
        self.request(a, type="buddy_request", user=uid(b))
        self.assertEqual(b.last("buddy_list")["incoming"], [])              # no second nag

        self.request(c, type="block", user=uid(a))
        self.request(a, type="buddy_request", user=uid(c))
        self.assertEqual(ids(a.last("buddy_list")["outgoing"]), sorted([uid(b), uid(c)],
                         key=lambda i: {uid(b): "bob", uid(c): "cara"}[i]))
        self.assertEqual(c.last("buddy_list")["incoming"], [])
        self.assertEqual(ids(c.last("buddy_list")["blocked"]), [uid(a)])
        self.assertEqual(self.request(c, type="buddy_request", user=uid(a))["code"], "blocked")

        self.request(c, type="unblock", user=uid(a))
        self.assertEqual(ids(c.last("buddy_list")["incoming"]), [uid(a)])   # the old request shows again

    def test_block_ends_buddies_and_bad_ids(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=uid(a))
        self.request(a, type="block", user=uid(b))
        self.assertEqual(b.last("buddy_list")["buddies"], [])
        self.assertNotIn("blocked", str(b.last("buddy_list")["blocked"]))   # b isn't told
        self.assertEqual(self.request(a, type="buddy_request", user="0" * 32)["code"], "no_user")
        self.assertEqual(self.request(a, type="buddy_request", user=uid(a))["code"], "no_user")
        nameless = self.user(name=None)
        self.assertEqual(self.request(nameless, type="buddy_request", user=uid(a))["code"], "no_name")


class DirectMessageTests(Harness):
    def buddies(self, a, b):
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=uid(a))

    def open_dm(self, a, b):
        opened = self.request(a, type="open_dm", user=uid(b))
        self.assertEqual(opened["type"], "dm_opened", opened)
        room = opened["room"]
        self.request(a, type="join", room=room["id"])
        return room

    def dm(self, session, room, sealed=None):
        self.clock.now += 3
        sealed = sealed or enc(getattr(session, "device", None) or self.register(session))
        reply = self.request(session, type="send", room=room, enc=sealed)
        self.assertEqual(reply["type"], "message", reply)
        return reply["message"], sealed

    def test_dm_reaches_both_even_when_not_open(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.buddies(a, b)
        room = self.open_dm(a, b)
        self.assertEqual((room["kind"], room["name"], room["other"]["id"]), ("dm", "Bob", uid(b)))
        _m, first = self.dm(a, room["id"])
        got = b.last("message")["message"]
        self.assertEqual((got["room"], got["text"], got["enc"]), (room["id"], "", first))
        b2 = FakeSession()   # Bob's second PC
        self.request(b2, type="hello", v=core.PROTOCOL_VERSION, token=b.token)
        _m, second = self.dm(a, room["id"])
        self.assertEqual(b2.last("message")["message"]["enc"], second)
        self.request(b, type="join", room=room["id"])
        self.assertEqual([m["enc"] for m in b.last("history")["messages"]], [first, second])
        stored = " ".join(str(v) for row in self.store.db.execute("SELECT * FROM messages") for v in row)
        self.assertNotIn('"text"', stored)

    def test_dms_must_be_encrypted_and_only_dms_are(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.buddies(a, b)
        room = self.open_dm(a, b)["id"]
        device = self.register(a)
        self.assertEqual(self.request(a, type="send", room=room, text="plain")["code"], "update_required")
        self.assertEqual(self.request(a, type="send", room="global", enc=enc(device))["code"], "bad_message")
        self.assertEqual(self.request(a, type="send", room=room, enc=enc("f" * 16))["code"], "no_device")
        good = enc(device)
        for broken in ({**good, "v": 2}, {**good, "salt": b64(8)}, {**good, "keys": {}},
                       {**good, "keys": {"not-a-device": b64(48)}}, {**good, "keys": {"a" * 16: b64(20)}},
                       {**good, "body": "not base64!"}, {**good, "extra": 1},
                       {**good, "body": b64(core.ENC_BODY_MAX + 1)},
                       {**good, "keys": {f"{i:016x}": b64(48) for i in range(11)}}):
            self.assertEqual(self.request(a, type="send", room=room, enc=broken)["code"], "bad_message", broken)
        self.dm(a, room, good)

    def test_keys_go_to_buddies_only_and_idle_pcs_drop_off(self):
        a, b, c = self.user("Alice"), self.user("Bob"), self.user("Cara")
        self.buddies(a, b)
        key = b64(32)
        device = self.register(a, key)
        self.assertEqual(device, core.device_id(base64.b64decode(key)))
        self.assertEqual(a.last("buddy_list")["my_keys"], [key])
        self.assertEqual(b.last("buddy_list")["buddies"][0]["keys"], [key])
        self.assertNotIn(key, json.dumps(c.inbox))
        pushed = len(b.inbox)
        self.register(a, key)                     # seen again: nothing new to tell anyone
        self.assertEqual(len(b.inbox), pushed)
        self.assertEqual(self.request(a, type="set_device_key", key="short")["code"], "bad_request")

        for _ in range(core.MAX_DEVICES):         # the oldest PCs make way
            self.clock.now += 1
            self.register(a)
        self.assertEqual(len(a.last("buddy_list")["my_keys"]), core.MAX_DEVICES)
        self.assertNotIn(key, a.last("buddy_list")["my_keys"])

        self.clock.now += core.DEVICE_IDLE_DAYS * 86400 + 1
        self.core.purge()
        self.assertEqual(b.last("buddy_list")["buddies"][0]["keys"], [])

    def test_history_pages_for_an_export_carry_its_nonce(self):
        a = self.user("Alice")
        self.request(a, type="send", room="global", text="hi")
        page = self.request(a, type="history", room="global", nonce="export-3")
        self.assertEqual((page["nonce"], [m["text"] for m in page["messages"]]), ("export-3", ["hi"]))
        self.assertNotIn("nonce", self.request(a, type="history", room="global"))

    def test_only_buddies_and_only_the_two_of_them(self):
        a, b, c = self.user("Alice"), self.user("Bob"), self.user("Cara")
        self.assertEqual(self.request(a, type="open_dm", user=uid(b))["code"], "not_buddies")
        self.buddies(a, b)
        room = self.open_dm(a, b)["id"]
        for request in ({"type": "join", "room": room}, {"type": "history", "room": room},
                        {"type": "send", "room": room, "text": "hi"}):
            self.assertEqual(self.request(c, **request)["code"], "no_room")
        self.assertEqual(self.request(c, type="get_rooms", ids=[room])["missing"], [room])
        self.assertNotIn(room, [r["id"] for r in self.request(c, type="find_rooms", query="")["rooms"]])

        self.request(b, type="buddy_remove", user=uid(a))
        self.assertEqual(self.request(a, type="send", room=room, text="still there?")["code"], "not_buddies")
        self.buddies(a, b)
        self.request(b, type="block", user=uid(a))
        self.assertEqual(self.request(a, type="send", room=room, text="hello?")["code"], "not_buddies")

    def test_deleting_a_dm_message_tells_both_and_drops_it(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.buddies(a, b)
        room = self.open_dm(a, b)["id"]
        mid = self.dm(a, room)[0]["id"]
        self.request(a, type="delete", id=mid)
        self.assertEqual(b.last("deleted")["id"], mid)
        row = self.store.message(mid)
        self.assertEqual((row["enc"], row["deleted"]), (None, 1))


class DeleteAccountTests(Harness):
    def test_everything_goes_but_room_messages_stay_authorless(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=uid(a))
        dm = self.request(a, type="open_dm", user=uid(b))["room"]["id"]
        self.request(a, type="join", room=dm)
        self.request(a, type="send", room=dm, text="secret")
        own_room = self.request(a, type="create_room", name="Alice's Room")["room"]["id"]
        self.clock.now += 11
        self.request(a, type="send", room="global", text="hello all")
        token = a.token

        self.request(a, type="delete_account")
        self.assertEqual(a.last()["type"], "account_deleted")
        self.assertTrue(a.close_requested)
        self.assertEqual(b.last("buddy_list")["buddies"], [])
        removed = {p["room"] for p in b.inbox if p["type"] == "room_removed"}
        self.assertEqual(removed, {dm, own_room})
        self.assertIsNone(self.store.room(dm))
        rows, _ = self.store.history("global", None, 10)
        self.assertEqual([(r["text"], r["author_id"]) for r in rows], [("hello all", None)])
        shown = core.public_message(rows[0])["author"]
        self.assertEqual((shown["id"], shown["name"]), ("", None))
        again = FakeSession()
        self.assertEqual(self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=token)["code"],
                         "bad_token")


class ReactionTests(Harness):
    def say(self, session, text, room="global"):
        self.clock.now += 3
        self.request(session, type="send", room=room, text=text)
        return session.last("message")["message"]["id"]

    def react(self, session, message_id, reaction, on=True):
        return self.request(session, type="react", id=message_id, reaction=reaction, on=on)

    def test_everyone_looking_sees_them_each_with_their_own(self):
        a, b = self.user("Alice"), self.user("Bob")
        m = self.say(a, "the render finished")
        shown = self.react(b, m, "grin")
        self.assertEqual(shown["type"], "reactions")
        self.assertEqual((shown["room"], shown["id"]), ("global", m))
        self.assertEqual([(r["r"], r["count"], r["mine"]) for r in shown["reactions"]], [("grin", 1, True)])
        self.assertEqual(shown["reactions"][0]["people"][0]["name"], "Bob")
        self.assertFalse(a.last("reactions")["reactions"][0]["mine"])   # Alice sees it, not as hers
        self.react(b, m, "heart")
        self.react(a, m, "grin")
        self.assertEqual([(r["r"], r["count"]) for r in a.last("reactions")["reactions"]],
                         [("grin", 2), ("heart", 1)])   # in the order they were first used

        cara = self.user("Cara")   # joining: history brings them
        got = next(x for x in cara.last("history")["messages"] if x["id"] == m)
        self.assertEqual([(r["r"], r["count"], r["mine"]) for r in got["reactions"]],
                         [("grin", 2, False), ("heart", 1, False)])
        self.assertEqual([p["name"] for p in got["reactions"][0]["people"]], ["Bob", "Alice"])

        self.react(b, m, "grin", on=False)   # Alice's :D came after Bob's <3
        self.assertEqual([(r["r"], r["count"]) for r in a.last("reactions")["reactions"]],
                         [("heart", 1), ("grin", 1)])
        before = len(a.inbox)
        self.react(b, m, "grin", on=False)   # nothing changed: only Bob is answered
        self.assertEqual(len(a.inbox), before)
        self.assertEqual(b.last()["type"], "reactions")

    def test_only_the_fixed_emoticons_where_you_can_see(self):
        a, b = self.user("Alice"), self.user("Bob", join=None)
        m = self.say(a, "hello")
        self.assertEqual(self.react(a, m, ":D")["code"], "bad_request")      # a key, never the text
        self.assertEqual(self.react(a, m, "<script>")["code"], "bad_request")
        self.assertEqual(self.react(b, m, "grin")["code"], "not_joined")
        self.assertEqual(self.react(a, 999_999, "grin")["code"], "no_message")
        nameless = self.user(name=None, ip="10.0.0.2")
        self.assertEqual(self.react(nameless, m, "grin")["code"], "no_name")
        self.request(a, type="delete", id=m)
        self.assertEqual(self.react(a, m, "grin")["code"], "no_message")
        self.assertEqual(set(core.REACTIONS), {"smile", "grin", "heart", "wink", "tongue", "sad", "wow", "laugh",
                                               "cool", "happy", "cheer", "shrug"})

    def test_too_many_too_quickly(self):
        a = self.user("Alice")
        m = self.say(a, "spam me")
        for i in range(core.REACT_LIMIT[0]):
            self.react(a, m, "grin", on=i % 2 == 0)
        self.assertEqual(self.react(a, m, "grin")["code"], "rate_limited")

    def test_in_a_dm_only_its_two_people(self):
        a, b = self.user("Alice"), self.user("Bob")
        cara = self.user("Cara", ip="10.0.0.2")
        DirectMessageTests.buddies(self, a, b)
        room = DirectMessageTests.open_dm(self, a, b)["id"]
        m, _sealed = DirectMessageTests.dm(self, a, room)
        self.assertEqual(self.react(cara, m["id"], "heart")["code"], "no_message")   # same as no message at all
        self.assertEqual(self.react(b, m["id"], "heart")["reactions"][0]["count"], 1)
        self.assertEqual(a.last("reactions")["room"], room)                          # Alice hears, room open or not
        self.request(a, type="block", user=uid(b))
        self.assertNotEqual(self.react(b, m["id"], "grin")["type"], "reactions")

    def test_they_go_with_the_message_and_the_person(self):
        a, b = self.user("Alice"), self.user("Bob")
        gone, kept = self.say(a, "one"), self.say(a, "two")
        self.react(b, gone, "sad")
        self.react(b, kept, "cool")
        self.react(a, kept, "cool")
        self.request(a, type="delete", id=gone)
        self.assertNotIn(gone, self.store.reactions([gone, kept]))
        self.request(b, type="delete_account")
        self.assertEqual([p["name"] for p in self.store.reactions([kept])[kept][0]["people"]], ["Alice"])
        self.clock.now += (core.HISTORY_DAYS + 1) * 86400
        self.core.purge()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reactions").fetchone()[0], 0)


class AdminTests(Harness):
    def setUp(self):
        super().setUp()
        self.core.limit_new_accounts = False   # more than 3 test identities from one address
        self.owner, self.admin, self.bob = self.user("Olive"), self.user("Adam"), self.user("Bob")
        self.store.set_role(uid(self.owner), "owner")
        self.store.set_role(uid(self.admin), "admin")

    def say(self, session, text, room="global"):
        self.clock.now += 3
        self.request(session, type="send", room=room, text=text)
        return session.last("message")["message"]

    def test_badges_come_from_the_server(self):
        m = self.say(self.admin, "hello")
        self.assertEqual(m["author"]["role"], "admin")
        self.assertEqual(self.say(self.bob, "hi")["author"]["role"], "user")

    def test_admins_delete_any_message_users_cannot(self):
        m = self.say(self.bob, "rude thing")
        self.assertEqual(self.request(self.owner, type="delete", id=m["id"])["type"], "deleted")
        m2 = self.say(self.admin, "admin says")
        self.assertEqual(self.request(self.bob, type="delete", id=m2["id"])["code"], "not_allowed")
        log = self.request(self.owner, type="admin_log")["entries"]
        self.assertEqual((log[0]["action"], log[0]["detail"]), ("delete_message", "rude thing"))
        self.assertEqual(self.request(self.admin, type="admin_log")["code"], "not_allowed")  # owner only

    def test_rooms_rename_topic_announcement(self):
        room = self.request(self.bob, type="create_room", name="Bobs Room")["room"]["id"]
        self.assertEqual(self.request(self.bob, type="rename_room", room=room, name="X Room")["code"],
                         "not_allowed")   # makers can't rename, admins can
        self.request(self.admin, type="rename_room", room=room, name="Better Name")
        self.assertEqual(self.bob.last("room_updated")["room"]["name"], "Better Name")
        self.assertEqual(self.request(self.admin, type="rename_room", room=room, name="Global")["code"], "bad_room")
        self.request(self.admin, type="set_announcement", room="global", text="Server update tonight")
        self.assertEqual(self.bob.last("room_updated")["room"]["announcement"], "Server update tonight")
        self.assertEqual(self.request(self.bob, type="set_announcement", room="global", text="lol")["code"],
                         "not_allowed")
        self.request(self.admin, type="set_topic", room="help", text="", topic="Ask here")
        self.assertEqual(self.store.room("help")["topic"], "Ask here")
        self.assertEqual(self.request(self.bob, type="set_topic", room="help", topic="mine")["code"], "not_allowed")
        self.request(self.admin, type="delete_room", room=room)
        self.assertIsNone(self.store.room(room))
        self.assertEqual(self.request(self.admin, type="delete_room", room="global")["code"], "not_allowed")

    def test_the_owner_makes_a_room_public_and_back(self):
        room = self.request(self.bob, type="create_room", name="Fusion Tips")["room"]["id"]
        cara = self.user("Cara")
        for who in (self.bob, self.admin):
            self.assertEqual(self.request(who, type="set_public", room=room, value=True)["code"], "not_allowed")
        public = self.request(self.owner, type="set_public", room=room, value=True)["room"]
        self.assertEqual((public["kind"], public["owner"], public["made_public"]), ("system", None, True))
        self.assertEqual(cara.last("room_updated")["room"]["kind"], "system")   # everyone is told
        again = FakeSession()
        welcome = self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=cara.token)
        self.assertEqual([r["id"] for r in welcome["rooms"]], ["global", "help", room])
        self.assertFalse(welcome["rooms"][0]["made_public"])
        self.assertEqual(self.store.rooms_owned_by(uid(self.bob)), [])   # no longer counts as Bob's
        self.assertEqual(self.request(self.bob, type="delete_room", room=room)["code"], "not_allowed")
        self.assertEqual(self.request(self.bob, type="set_topic", room=room, topic="mine")["code"], "not_allowed")
        self.request(self.admin, type="set_topic", room=room, topic="Staff look after it")
        self.assertEqual(self.store.room(room)["topic"], "Staff look after it")
        self.assertEqual(self.request(cara, type="find_rooms", query="fusion")["rooms"], [])   # it's listed anyway
        self.clock.now += (core.ROOM_IDLE_DAYS + 1) * 86400
        self.core.purge()
        self.assertIsNotNone(self.store.room(room))   # everyone's rooms never expire
        self.assertEqual(self.request(self.owner, type="set_public", room="global", value=False)["code"],
                         "not_allowed")
        self.assertEqual(self.request(self.owner, type="set_public", room=room, value=True)["type"],
                         "room_updated")   # already public: just the room again

        regular = self.request(self.owner, type="set_public", room=room, value=False)["room"]
        self.assertEqual((regular["kind"], regular["owner"]["name"], regular["made_public"]), ("user", "Bob", False))
        self.core.purge()
        self.assertIsNotNone(self.store.room(room))   # idle from now, not from its last message
        self.assertEqual([r["id"] for r in self.request(cara, type="find_rooms", query="fusion")["rooms"]], [room])
        self.assertEqual(self.request(self.bob, type="delete_room", room=room)["type"], "room_removed")
        actions = [e["action"] for e in self.request(self.owner, type="admin_log")["entries"]]
        self.assertIn("make_public", actions)
        self.assertIn("make_regular", actions)

    def test_a_public_room_outlives_its_maker(self):
        room = self.request(self.bob, type="create_room", name="Colour Club")["room"]["id"]
        self.request(self.owner, type="set_public", room=room, value=True)
        self.request(self.bob, type="delete_account")
        self.assertIsNotNone(self.store.room(room))
        self.assertIsNone(self.store.former_owner(room))   # nothing of Bob's kept
        regular = self.request(self.owner, type="set_public", room=room, value=False)["room"]
        self.assertEqual(regular["owner"]["id"], uid(self.owner))   # the maker's gone: the owner's

    def test_reports_reach_admins_with_a_snapshot(self):
        m = self.say(self.bob, "buy followers at scam.example")
        cara = self.user("Cara")
        self.assertEqual(self.request(cara, type="report", id=m["id"], reason="spam")["type"], "reported")
        self.assertEqual(self.admin.last("reports_waiting")["count"], 1)
        self.assertFalse(any(p["type"] == "reports_waiting" for p in cara.inbox))
        self.request(self.bob, type="delete", id=m["id"])     # the snapshot survives this
        self.request(self.admin, type="report", id=m["id"])   # deleted: can't report now
        reports = self.request(self.admin, type="list_reports")["reports"]
        self.assertEqual([(r["text"], r["reason"], r["reporter"]["name"], r["reported"]["name"], r["where"],
                           r["claimed"]) for r in reports],
                         [("buy followers at scam.example", "spam", "Cara", "Bob", "#Global", False)])
        self.assertEqual(self.request(cara, type="list_reports")["code"], "not_allowed")
        self.assertEqual(self.request(self.bob, type="report", id=self.say(self.bob, "mine")["id"])["code"],
                         "bad_request")
        self.request(self.admin, type="resolve_report", id=reports[0]["id"])
        self.assertEqual(self.admin.last("reports_waiting")["count"], 0)

    def test_dm_reports_only_by_the_two_people(self):
        self.request(self.bob, type="buddy_request", user=uid(self.owner))
        self.request(self.owner, type="buddy_accept", user=uid(self.bob))
        dm = self.request(self.bob, type="open_dm", user=uid(self.owner))["room"]["id"]
        self.request(self.bob, type="join", room=dm)
        self.clock.now += 3
        device = self.register(self.bob)
        self.request(self.bob, type="send", room=dm, enc=enc(device))
        m = self.bob.last("message")["message"]
        self.assertEqual(self.request(self.admin, type="report", id=m["id"], text="x")["code"], "no_message")
        # Encrypted: only the text the reporter's Buddy read can go with it.
        self.assertEqual(self.request(self.owner, type="report", id=m["id"])["code"], "bad_request")
        self.request(self.owner, type="report", id=m["id"], text="what it said")
        report = self.request(self.admin, type="list_reports")["reports"][0]
        self.assertEqual((report["where"], report["text"], report["claimed"]),
                         ("a direct message", "what it said", True))
        self.request(self.admin, type="resolve_report", id=report["id"], delete=True)
        log = self.request(self.owner, type="admin_log")["entries"]
        self.assertEqual((log[1]["action"], log[1]["detail"]), ("delete_message", "what it said"))

    def test_ban_kicks_and_refuses_sign_in_until_it_ends(self):
        token = self.bob.token
        self.request(self.admin, type="ban", user=uid(self.bob), days=1, reason="spam", network=True)
        self.assertEqual(self.bob.last("error")["code"], "banned")
        self.assertTrue(self.bob.close_requested)
        again = FakeSession("10.0.0.1")
        err = self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=token)
        self.assertEqual((err["code"], err["reason"]), ("banned", "spam"))
        fresh = FakeSession("10.0.0.1")   # same network, new identity: refused
        self.assertEqual(self.request(fresh, type="hello", v=core.PROTOCOL_VERSION)["code"], "banned")
        other = FakeSession("10.7.7.7")
        self.assertEqual(self.request(other, type="hello", v=core.PROTOCOL_VERSION)["type"], "welcome")
        bans = self.request(self.admin, type="list_bans")["bans"]
        self.assertEqual((bans[0]["user"]["name"], bans[0]["network"]), ("Bob", True))
        for table in ("bans", "network_bans"):
            for row in self.store.db.execute(f"SELECT * FROM {table}"):
                self.assertNotIn("10.0.0.1", " ".join(str(v) for v in row))   # a keyed hash only
        self.clock.now += 86401
        self.core.purge()
        self.assertEqual(self.request(FakeSession("10.0.0.1"), type="hello", v=core.PROTOCOL_VERSION,
                                      token=token)["type"], "welcome")

    def test_who_can_ban_whom_and_unban(self):
        self.assertEqual(self.request(self.bob, type="ban", user=uid(self.admin), days=1)["code"], "not_allowed")
        self.assertEqual(self.request(self.admin, type="ban", user=uid(self.owner), days=None)["code"],
                         "not_allowed")
        self.assertEqual(self.request(self.admin, type="ban", user=uid(self.bob), days=3)["code"], "bad_request")
        admin_id = uid(self.admin)   # a kicked session forgets who it was
        self.request(self.owner, type="ban", user=admin_id, days=None)
        self.assertEqual(self.admin.last("error")["code"], "banned")
        self.request(self.owner, type="unban", user=admin_id)
        self.assertEqual(self.request(self.owner, type="list_bans")["bans"], [])

    def test_mods_moderate_but_only_reach_down(self):
        mod, cara = self.user("Mona"), self.user("Cara")
        self.assertEqual(self.request(self.admin, type="set_role", user=uid(mod), role="mod")["type"], "admins")
        self.assertEqual(mod.last("role_changed")["user"]["role"], "mod")
        self.assertEqual(self.say(mod, "hi")["author"]["role"], "mod")     # the MOD badge
        # Every moderation power...
        m = self.say(cara, "rude")
        self.assertEqual(self.request(mod, type="delete", id=m["id"])["type"], "deleted")
        self.assertEqual(self.request(mod, type="list_reports")["type"], "reports")
        room = self.request(cara, type="create_room", name="Cara Room")["room"]["id"]
        self.assertEqual(self.request(mod, type="set_announcement", room=room, text="Be nice")["type"],
                         "room_updated")
        # ...bans only reach down...
        self.assertEqual(self.request(mod, type="ban", user=uid(self.admin), days=1)["code"], "not_allowed")
        self.assertEqual(self.request(mod, type="ban", user=uid(cara), days=1)["type"], "ban_done")
        # ...and the owner's and admins' own things stay theirs.
        for request in ({"type": "set_role", "user": uid(self.bob), "role": "mod"}, {"type": "list_admins"},
                        {"type": "admin_log"}, {"type": "post_app_announcement", "title": "Hi", "text": "x"}):
            self.assertEqual(self.request(mod, **request)["code"], "not_allowed", request)
        self.assertEqual(self.request(self.admin, type="set_role", user=uid(self.bob), role="admin")["code"],
                         "not_allowed")
        self.assertEqual(self.request(self.admin, type="set_role", user=uid(self.owner), role="user")["code"],
                         "not_allowed")
        self.assertEqual(self.request(self.admin, type="post_app_announcement", title="Hi", text="x")["code"],
                         "not_allowed")
        staff = self.request(self.admin, type="list_admins")["admins"]
        self.assertEqual([(a["name"], a["role"]) for a in staff], [("Olive", "owner"), ("Adam", "admin"),
                                                                   ("Mona", "mod")])
        mod_id = uid(mod)
        self.assertEqual(self.request(self.admin, type="ban", user=mod_id, days=1)["type"], "ban_done")
        self.request(self.owner, type="set_role", user=mod_id, role="user")
        self.assertEqual(self.store.user(mod_id)["role"], "user")

    def test_owner_makes_and_unmakes_admins(self):
        cara = self.user("Cara")
        self.assertEqual(self.request(self.admin, type="set_role", user=uid(cara), role="admin")["code"],
                         "not_allowed")
        self.request(self.owner, type="set_role", user=uid(cara), role="admin")
        self.assertEqual(cara.last("role_changed")["user"]["role"], "admin")
        self.assertEqual(self.request(cara, type="list_reports")["type"], "reports")
        admins = self.request(self.owner, type="list_admins")["admins"]
        self.assertEqual(sorted(a["name"] for a in admins), ["Adam", "Cara", "Olive"])
        self.request(self.owner, type="set_role", user=uid(cara), role="user")
        self.assertEqual(self.request(cara, type="list_reports")["code"], "not_allowed")
        self.assertEqual(self.request(self.owner, type="set_role", user=uid(self.owner), role="user")["code"],
                         "no_user")   # "that's you"


class ChatFeatureTests(Harness):
    """Replies, edits, @mentions, unread counts and slow mode."""

    def setUp(self):
        super().setUp()
        self.core.limit_new_accounts = False

    def say(self, session, text, room="global", **extra):
        self.clock.now += 3
        reply = self.request(session, type="send", room=room, text=text, **extra)
        self.assertEqual(reply["type"], "message", reply)
        return reply["message"]

    def test_replies_quote_the_original_in_the_same_room_only(self):
        a, b = self.user("Alice"), self.user("Bob")
        first = self.say(a, "Anyone know how to   stabilise\nfootage?")
        answer = self.say(b, "Yes - use the Tracker", reply_to=first["id"])
        quote = answer["reply"]
        self.assertEqual((quote["id"], quote["author"]["name"], quote["text"]),
                         (first["id"], "Alice", "Anyone know how to stabilise footage?"))
        self.assertEqual(self.request(b, type="history", room="global")["messages"][-1]["reply"]["id"], first["id"])
        self.request(b, type="join", room="help")
        for bad in (first["id"], 999999, True, "1"):
            self.assertEqual(self.request(b, type="send", room="help", text="x", reply_to=bad)["code"], "no_message")
        self.request(a, type="delete", id=first["id"])
        self.assertTrue(self.request(a, type="history", room="global")["messages"][-1]["reply"]["deleted"])
        self.clock.now += core.HISTORY_DAYS * 86400 - 2   # the original ages out first
        self.core.purge()
        rows, _ = self.store.history("global", None, 10)
        self.assertEqual(core.public_message(rows[-1])["reply"], {"id": first["id"], "gone": True})

    def test_only_the_author_edits_and_everyone_sees_it(self):
        a, b = self.user("Alice"), self.user("Bob")
        m = self.say(a, "teh render")
        self.assertEqual(self.request(b, type="edit", id=m["id"], text="hacked")["code"], "not_allowed")
        self.request(a, type="edit", id=m["id"], text="the render")
        edited = b.last("edited")["message"]
        self.assertEqual((edited["id"], edited["text"], edited["edited"]), (m["id"], "the render", self.clock.now))
        self.assertEqual(self.request(a, type="edit", id=m["id"], text=" ")["code"], "bad_message")
        self.request(a, type="delete", id=m["id"])
        self.assertEqual(self.request(a, type="edit", id=m["id"], text="back")["code"], "no_message")

    def test_editing_a_dm_stays_encrypted(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=uid(a))
        dm = self.request(a, type="open_dm", user=uid(b))["room"]["id"]
        self.request(a, type="join", room=dm)
        device = self.register(a)
        self.request(a, type="send", room=dm, enc=enc(device))
        mid = a.last("message")["message"]["id"]
        self.assertEqual(self.request(a, type="edit", id=mid, text="plain")["code"], "update_required")
        new = enc(device)
        self.request(a, type="edit", id=mid, enc=new)
        self.assertEqual(b.last("edited")["message"]["enc"], new)

    def test_mentions_reach_the_named_person_wherever_they_are(self):
        a, b, c = self.user("Alice"), self.user("Bob Smith", join="help"), self.user("Cara", join=None)
        tag_b, tag_c = b.last("welcome")["user"]["tag"], c.last("welcome")["user"]["tag"]
        m = self.say(a, f"thanks @Bob Smith#{tag_b} and @cara#{tag_c}! not @Bob#{tag_b} or @Dan#abcdef")
        for who in (b, c):
            got = who.last("mentioned")
            self.assertEqual((got["room"], got["room_name"], got["message"]["id"]), ("global", "Global", m["id"]))
        self.assertEqual(sum(p["type"] == "mentioned" for p in b.inbox), 1)   # once, not for the wrong name
        self.request(c, type="block", user=uid(a))
        self.say(a, f"@Cara#{tag_c} again")
        self.assertEqual(sum(p["type"] == "mentioned" for p in c.inbox), 1)   # blocked: not told
        self.say(a, f"me: @Alice#{a.last('welcome')['user']['tag']}")
        self.assertFalse(any(p["type"] == "mentioned" for p in a.inbox))       # not yourself

    def test_staff_numbers_can_be_mentioned(self):
        a, owner = self.user("Alice"), self.user("Shuttle", join=None)
        self.store.change_user_id(uid(owner), "000001" + "d" * 26)
        again = FakeSession()
        self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=owner.token)
        self.say(a, "ping @Shuttle#1 please")
        self.assertEqual(again.last("mentioned")["room"], "global")

    def test_unread_counts_and_activity_for_watched_rooms(self):
        a, b = self.user("Alice"), self.user("Bob", join="help")
        seen = self.say(a, "one")["id"]
        self.say(a, "two")
        self.request(b, type="buddy_request", user=uid(a))
        self.request(a, type="buddy_accept", user=uid(b))
        dm = self.request(a, type="open_dm", user=uid(b))["room"]["id"]
        unread = self.request(b, type="watch", rooms={"global": seen, "help": 0, dm: 0, "dm-x-y": 0,
                                                     "nowhere": 0})["rooms"]
        self.assertEqual(unread, {"global": 1, "help": 0, dm: 0})   # not someone else's DM, not a missing room
        m = self.say(a, "three")
        self.assertEqual(b.last("activity"), {"type": "activity", "room": "global", "id": m["id"]})
        before = len(b.inbox)
        self.request(b, type="watch", rooms={"help": 0})
        self.say(a, "four")
        self.assertFalse(any(p["type"] == "activity" for p in b.inbox[before:]))
        self.assertEqual(self.request(b, type="watch", rooms=["global"])["code"], "bad_request")

    def test_slow_mode_is_set_by_staff_and_spares_them(self):
        a, mod = self.user("Alice"), self.user("Mona")
        self.store.set_role(uid(mod), "mod")
        self.assertEqual(self.request(a, type="set_slow", room="global", seconds=30)["code"], "not_allowed")
        self.assertEqual(self.request(mod, type="set_slow", room="global", seconds=7)["code"], "bad_request")
        self.assertEqual(self.request(mod, type="set_slow", room="global", seconds=30)["room"]["slow"], 30)
        self.say(a, "first")
        self.clock.now += 3
        err = self.request(a, type="send", room="global", text="second")
        self.assertEqual((err["code"], err["retry_after"]), ("rate_limited", 27))
        self.say(mod, "mods aren't slowed")
        self.say(mod, "at all")
        self.clock.now += 30
        self.say(a, "now it goes")
        self.request(mod, type="set_slow", room="global", seconds=0)
        self.say(a, "and off again")


class AvatarTests(Harness):
    def test_everyone_sees_a_new_avatar_and_the_original_comes_back(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=uid(a))
        a2 = FakeSession()   # Alice's other PC
        self.request(a2, type="hello", v=core.PROTOCOL_VERSION, token=a.token)
        self.assertEqual(self.request(a, type="set_avatar", avatar="0f1e2d3c")["user"]["avatar"], "0f1e2d3c")
        self.assertEqual(a2.last("avatar_set")["user"]["avatar"], "0f1e2d3c")
        self.assertEqual(b.last("buddy_list")["buddies"][0]["avatar"], "0f1e2d3c")
        self.request(a, type="send", room="global", text="hi")
        self.assertEqual(b.last("message")["message"]["author"]["avatar"], "0f1e2d3c")
        for bad in ("short", "ABCDEF12", "0f1e2d3c" * 3, "<script>", 5, None):
            self.assertEqual(self.request(a, type="set_avatar", avatar=bad)["code"], "bad_request", bad)
        self.request(a, type="set_avatar", avatar="")
        self.assertEqual(self.store.user(uid(a))["avatar"], "")

    def test_up_to_six_saved_only_for_their_own_pcs(self):
        a, b = self.user("Alice"), self.user("Bob")
        six = [f"{i:08x}" for i in range(6)]
        self.assertEqual(self.request(a, type="save_avatars", saved=six + six[:2])["saved"], six)   # repeats dropped
        self.assertEqual(self.request(a, type="save_avatars", saved=six + ["abcdef01"])["code"], "too_many")
        self.assertEqual(self.request(a, type="save_avatars", saved=["nope"])["code"], "bad_request")
        again = FakeSession()
        self.assertEqual(self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=a.token)["saved_avatars"],
                         six)
        self.assertNotIn("00000005", json.dumps(b.inbox))

    def test_changes_are_limited(self):
        a = self.user("Alice")
        for i in range(core.AVATAR_LIMIT[0]):
            self.request(a, type="set_avatar", avatar=f"{i:08x}")
        self.assertEqual(self.request(a, type="set_avatar", avatar="ffffffff")["code"], "rate_limited")


class StaffTagTests(Harness):
    def test_tags_and_random_ids_stay_out_of_the_staff_range(self):
        from server import common
        self.assertEqual(common.tag_of("000001" + "a" * 26), "1")
        self.assertEqual(common.tag_of("000042" + "a" * 26), "42")
        self.assertEqual(common.tag_of("0a1b2c" + "a" * 26), "0a1b2c")   # an ordinary ID
        rolls = iter(["0000" + "f" * 28, "0000" + "e" * 28, "12" * 16])
        self.assertEqual(common.new_user_id(lambda n: next(rolls)), "12" * 16)
        self.assertTrue(common.staff_id(7, lambda n: "b" * (2 * n)).startswith("000007"))
        with self.assertRaises(ValueError):
            common.staff_id(100, lambda n: "")

    def test_everything_follows_the_user_to_their_new_id(self):
        self.core.limit_new_accounts = False
        a, b = self.user("Alice"), self.user("Bob")
        old = uid(a)
        self.store.set_role(old, "owner")
        self.request(a, type="send", room="global", text="hello")
        room = self.request(a, type="create_room", name="Alice Room")["room"]["id"]
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=old)
        dm = self.request(a, type="open_dm", user=uid(b))["room"]["id"]
        self.request(a, type="join", room=dm)
        self.clock.now += 11
        sealed = enc(self.register(a))
        self.request(a, type="send", room=dm, enc=sealed)
        self.request(a, type="post_app_announcement", title="Hi", text="there")

        new = "000001" + "c" * 26
        self.store.change_user_id(old, new)

        me = self.store.user(new)
        self.assertEqual((me["name"], me["role"]), ("Alice", "owner"))
        self.assertIsNone(self.store.user(old))
        again = FakeSession()
        welcome = self.request(again, type="hello", v=core.PROTOCOL_VERSION, token=a.token)
        self.assertEqual((welcome["user"]["id"], welcome["user"]["tag"]), (new, "1"))   # same token
        self.assertEqual([r["id"] for r in welcome["my_rooms"]], [room])
        rows, _ = self.store.history("global", None, 10)
        self.assertEqual(rows[-1]["author_id"], new)
        self.assertTrue(self.store.are_buddies(new, uid(b)))
        new_dm = self.store.dm_room_ids_of(new)
        self.assertEqual(len(new_dm), 1)
        self.assertNotEqual(new_dm[0], dm)
        rows, _ = self.store.history(new_dm[0], None, 10)
        self.assertEqual([json.loads(r["enc"]) for r in rows], [sealed])
        self.assertEqual(list(self.store.device_keys([new])[new]), [a.device])
        self.assertEqual(self.store.admin_log(10)[0]["actor"], new)
        with self.assertRaises(ValueError):
            self.store.change_user_id(uid(b), new)   # taken


def _quiet():
    import contextlib
    return contextlib.nullcontext()


class SecurityTests(Harness):
    """Abuse and robustness checks: nobody can flood others, probe
    DMs, pass as someone else by lookalike letters, or break the server
    with odd input."""

    def setUp(self):
        super().setUp()
        self.core.limit_new_accounts = False

    def test_repeats_that_change_nothing_reach_nobody_else(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.clock.now += 3
        self.request(a, type="send", room="global", text="oops")
        m = a.last("message")["message"]
        self.request(a, type="delete", id=m["id"])
        seen = len(b.inbox)
        for _ in range(20):
            self.assertEqual(self.request(a, type="delete", id=m["id"])["type"], "deleted")
        self.assertEqual(len(b.inbox), seen)
        for kind in ("buddy_cancel", "buddy_remove"):   # nothing between them to cancel or remove
            for _ in range(20):
                self.request(a, type=kind, user=uid(b))
        self.assertEqual(len(b.inbox), seen)
        self.request(a, type="create_room", name="Quiet", topic="t")
        room = a.last("room_created")["room"]["id"]
        seen = len(b.inbox)
        self.request(a, type="set_topic", room=room, topic="t")    # unchanged
        for i in range(core.TOPIC_LIMIT[0]):
            self.request(a, type="set_topic", room=room, topic=f"t{i}")
        self.assertEqual(self.request(a, type="set_topic", room=room, topic="more")["code"], "rate_limited")
        self.assertEqual(len(b.inbox) - seen, core.TOPIC_LIMIT[0])

    def test_presence_and_new_pc_keys_are_limited(self):
        a = self.user("Alice")
        self.request(a, type="set_presence", offline=False)   # unchanged: fine, and unlimited
        for i in range(10):
            self.request(a, type="set_presence", offline=i % 2 == 0)
        self.assertEqual(self.request(a, type="set_presence", offline=True)["code"], "rate_limited")
        key = b64(32)
        for _ in range(10):
            self.register(a)
        self.assertEqual(self.request(a, type="set_device_key", key=b64(32))["code"], "rate_limited")
        self.assertEqual(self.request(a, type="set_device_key", key=a.last("buddy_list")["my_keys"][-1])["type"],
                         "device_registered")   # one it already has is fine
        del key

    def test_one_identity_on_so_many_buddys_at_once(self):
        a = self.user("Alice")
        for _ in range(core.MAX_SESSIONS_PER_USER - 1):
            self.assertEqual(self.request(FakeSession(), type="hello", v=core.PROTOCOL_VERSION, token=a.token)["type"],
                             "welcome")
        extra = FakeSession()
        err = self.request(extra, type="hello", v=core.PROTOCOL_VERSION, token=a.token)
        self.assertEqual((err["code"], extra.close_requested), ("rate_limited", True))

    def test_dm_messages_cant_be_probed_by_outsiders(self):
        a, b, c = self.user("Alice"), self.user("Bob"), self.user("Cara")
        self.request(a, type="buddy_request", user=uid(b))
        self.request(b, type="buddy_accept", user=uid(a))
        dm = self.request(a, type="open_dm", user=uid(b))["room"]["id"]
        self.request(a, type="join", room=dm)
        device = self.register(a)
        self.clock.now += 3
        self.request(a, type="send", room=dm, enc=enc(device))
        m = a.last("message")["message"]
        missing = m["id"] + 1000
        for kind, extra in (("report", {"text": "x"}), ("edit", {"text": "x"}), ("delete", {})):
            self.assertEqual(self.request(c, type=kind, id=m["id"], **extra)["code"],
                             self.request(c, type=kind, id=missing, **extra)["code"], kind)
        self.request(a, type="delete", id=m["id"])
        self.assertEqual(self.request(c, type="report", id=m["id"], text="x")["code"], "no_message")

    def test_lookalike_and_invisible_names_are_refused(self):
        a = self.user("Alice")
        for name in ("Аdmin", "Ѕystem", "Вuddy", "Mоderator",   # Cyrillic letters
                     "ㅤㅤ", "Shuttleㅤ", "Sam️", "Tess͏",      # blank or invisible
                     "Mixed аnd Latin", "Za" + "́" * 4 + "lgo"):
            self.assertEqual(self.request(a, type="set_name", name=name)["code"], "bad_name", name)
        for name in ("Chloé", "Андрей", "Νίκος", "美咲", "José María"):   # one alphabet each is fine
            self.clock.now += 86400   # past the name-change limit
            self.assertEqual(self.request(a, type="set_name", name=name)["type"], "name_set", name)
        self.clock.now += 86400
        err = self.request(a, type="create_room", name="Glоbal")   # Cyrillic о, like Global
        self.assertEqual(err["code"], "bad_room")

    def test_unbans_only_reach_down(self):
        owner, mod, bob = self.user("Olive"), self.user("Mo"), self.user("Bob")
        admin, other_mod = self.user("Adam"), self.user("Meg")
        mod_id, bob_id = uid(mod), uid(bob)   # a kicked session forgets who it was
        for session, role in ((owner, "owner"), (admin, "admin"), (mod, "mod"), (other_mod, "mod")):
            self.store.set_role(uid(session), role)
        self.request(owner, type="ban", user=bob_id, days=7)
        self.assertEqual(self.request(other_mod, type="unban", user=bob_id)["code"], "not_allowed")
        self.assertEqual(self.request(admin, type="unban", user=bob_id)["code"], "not_allowed")
        self.request(admin, type="ban", user=mod_id, days=1)
        self.assertEqual(self.request(other_mod, type="unban", user=mod_id)["code"], "not_allowed")
        self.assertEqual(self.request(admin, type="unban", user=mod_id)["type"], "bans")
        self.assertEqual(self.request(owner, type="unban", user=bob_id)["type"], "bans")

    def test_staff_cant_close_reports_about_themselves(self):
        mod, bob = self.user("Mo"), self.user("Bob")
        self.store.set_role(uid(mod), "mod")
        self.clock.now += 3
        self.request(mod, type="send", room="global", text="hmm")
        m = mod.last("message")["message"]
        self.request(bob, type="report", id=m["id"], reason="rude")
        report = self.request(mod, type="list_reports")["reports"][0]
        self.assertEqual(self.request(mod, type="resolve_report", id=report["id"])["code"], "not_allowed")

    def test_the_owner_can_close_reports_about_themselves(self):
        owner, bob = self.user("Olive"), self.user("Bob")
        self.store.set_role(uid(owner), "owner")
        for text in ("one", "two"):
            self.clock.now += 3
            self.request(owner, type="send", room="global", text=text)
            self.request(bob, type="report", id=owner.last("message")["message"]["id"], reason="nope")
        kept, deleted = self.request(owner, type="list_reports")["reports"]
        self.assertEqual(self.request(owner, type="resolve_report", id=kept["id"])["type"], "reports")
        self.request(owner, type="resolve_report", id=deleted["id"], delete=True)
        self.assertEqual(owner.last("reports")["reports"], [])
        self.assertTrue(self.store.message(deleted["message_id"])["deleted"])
        self.assertFalse(self.store.message(kept["message_id"])["deleted"])

    def test_odd_input_gets_an_error_not_a_crash(self):
        a = self.user("Alice")
        for frame in ('{"type":"history","room":"global","before":%d}' % 2 ** 70,
                      '{"type":"leave","room":["global"]}',
                      '{"type":"send","room":"global","text":"\\ud800"}',
                      '{"type":"watch","rooms":{"global":%d}}' % 2 ** 70,
                      '{"type":["hello"]}',
                      "[" * 100000 + "]" * 100000):
            before = len(a.inbox)
            with self.assertLogs("buddy_network", "WARNING") if "leave" in frame[:20] else _quiet():
                self.core.handle(a, frame)
            self.assertIn(a.inbox[-1]["type"], ("error", "history", "unread"), frame[:60]) if len(a.inbox) > before \
                else None
        self.assertEqual(self.request(a, type="ping")["type"], "pong")   # still fine

    def test_appear_offline_people_arent_counted_in_a_room(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.assertEqual(self.request(a, type="get_rooms", ids=["global"])["rooms"][0]["here"], 2)
        self.request(b, type="set_presence", offline=True)
        self.assertEqual(self.request(a, type="get_rooms", ids=["global"])["rooms"][0]["here"], 1)

    def test_ipv6_counts_by_network(self):
        self.assertEqual(core.network_of("2001:db8:1:2:aaaa::1"), core.network_of("2001:db8:1:2:bbbb::9"))
        self.assertNotEqual(core.network_of("2001:db8:1:2::1"), core.network_of("2001:db8:1:3::1"))
        self.assertEqual(core.network_of("10.1.2.3"), "10.1.2.3")
        self.assertEqual(core.network_of("::ffff:10.1.2.3"), "10.1.2.3")
        self.core.limit_new_accounts = True
        for i in range(3):
            self.user(name=None, join=None, ip=f"2001:db8:1:2::{i + 1}")
        s = FakeSession("2001:db8:1:2::99")
        self.assertEqual(self.request(s, type="hello", v=core.PROTOCOL_VERSION)["code"], "rate_limited")

    def test_forgotten_identities_and_wiped_text(self):
        gone = self.user(name=None, join=None)
        self.core.disconnect(gone)
        kept = self.user("Named", join=None)
        self.assertEqual(self.store.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.clock.now += (core.NAMELESS_DAYS + 1) * 86400
        self.core.purge()
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT name FROM users")], ["Named"])
        self.assertIsNotNone(kept.user_id)
        self.core.limits.check(("old",), 1, 60.0)
        self.clock.now += 2 * 86400
        self.core.purge()
        self.assertNotIn(("old",), self.core.limits._events)

    def test_deleted_message_text_leaves_the_admin_log_after_a_while(self):
        owner, bob = self.user("Olive"), self.user("Bob")
        self.store.set_role(uid(owner), "owner")
        self.clock.now += 3
        self.request(bob, type="send", room="global", text="something nasty")
        self.request(owner, type="delete", id=bob.last("message")["message"]["id"])
        self.assertIn("nasty", self.store.admin_log(5)[0]["detail"])
        self.clock.now += 31 * 86400
        self.core.purge()
        entry = self.store.admin_log(5)[0]
        self.assertEqual((entry["action"], entry["detail"]), ("delete_message", ""))


class PurgeTests(Harness):
    """The owner's "delete forever"."""

    def setUp(self):
        super().setUp()
        self.core.limit_new_accounts = False
        self.owner, self.admin, self.bob = self.user("Olive"), self.user("Adam"), self.user("Bob")
        self.store.set_role(uid(self.owner), "owner")
        self.store.set_role(uid(self.admin), "admin")

    def say(self, session, text, **extra):
        self.clock.now += 3
        self.request(session, type="send", room="global", text=text, **extra)
        return session.last("message")["message"]

    def test_gone_without_a_trace_and_only_the_owner_can(self):
        m = self.say(self.bob, "something awful")
        reply = self.say(self.admin, "please don't", reply_to=m["id"])
        self.request(self.admin, type="report", id=m["id"], reason="awful")
        self.assertEqual(self.request(self.admin, type="purge_message", id=m["id"])["code"], "not_allowed")
        self.assertEqual(self.request(self.bob, type="purge_message", id=m["id"])["code"], "not_allowed")
        seen = len(self.bob.inbox)
        self.request(self.owner, type="purge_message", id=m["id"])
        told = [p["type"] for p in self.bob.inbox[seen:] if p.get("id") == m["id"]]
        self.assertEqual(told, ["deleted", "purged"])   # an older Buddy at least hides it
        self.assertIsNone(self.store.message(m["id"]))
        history = self.request(self.bob, type="history", room="global")["messages"]
        self.assertEqual([x["id"] for x in history], [reply["id"]])
        self.assertNotIn("reply", history[0])            # the quote goes with it
        self.assertEqual(self.request(self.admin, type="list_reports")["reports"], [])
        entry = self.store.admin_log(5)[0]
        self.assertEqual((entry["action"], entry["target"], entry["detail"]),
                         ("purge_message", uid(self.bob), ""))   # who, never what it said

    def test_a_deleted_one_loses_its_placeholder_and_dms_stay_private(self):
        m = self.say(self.bob, "oops")
        self.request(self.bob, type="delete", id=m["id"])
        self.request(self.owner, type="purge_message", id=m["id"])
        self.assertEqual(self.request(self.bob, type="history", room="global")["messages"], [])
        # A DM the owner isn't in is no more theirs to see than anyone else's.
        cara = self.user("Cara")
        self.request(self.bob, type="buddy_request", user=uid(cara))
        self.request(cara, type="buddy_accept", user=uid(self.bob))
        dm = self.request(self.bob, type="open_dm", user=uid(cara))["room"]["id"]
        self.request(self.bob, type="join", room=dm)
        device = self.register(self.bob)
        self.clock.now += 3
        self.request(self.bob, type="send", room=dm, enc=enc(device))
        secret = self.bob.last("message")["message"]
        self.assertEqual(self.request(self.owner, type="purge_message", id=secret["id"])["code"], "no_message")
        self.assertIsNotNone(self.store.message(secret["id"]))


class MigrationTests(unittest.TestCase):
    def test_phase_1_database_is_upgraded(self):
        import os
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.db")
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE rooms (id TEXT PRIMARY KEY, name TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '',
                                    kind TEXT NOT NULL, owner TEXT, created REAL NOT NULL);
                INSERT INTO rooms VALUES ('global', 'Global', '', 'system', NULL, 5.0);
                CREATE TABLE users (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, name TEXT,
                                    role TEXT NOT NULL DEFAULT 'user', created REAL NOT NULL);
                INSERT INTO users VALUES ('u1', 'h1', 'Old', 'user', 1.0);
                CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
                                       author TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL,
                                       deleted INTEGER NOT NULL DEFAULT 0);
                INSERT INTO messages (room, author, text, ts) VALUES ('global', 'u1', 'hello all', 6.0);
                INSERT INTO messages (room, author, text, ts) VALUES ('dm-u1-u2', 'u1', 'readable DM', 7.0);
            """)
            db.commit()
            db.close()
            store = Store(path)
            try:
                self.assertEqual(store.db.execute("SELECT last_active FROM rooms").fetchone()[0], 5.0)
                self.assertEqual(store.db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0],
                                 str(SCHEMA_VERSION))
                self.assertEqual(store.user("u1")["appear_offline"], 0)
                # DMs from before encryption were readable on the server: gone.
                self.assertEqual([r[0] for r in store.db.execute("SELECT text FROM messages")], ["hello all"])
            finally:
                store.close()


WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + os.urandom(3000)


def image_enc(device: str, to=("0123456789abcdef",)) -> dict:
    """A DM image's wrapped keys, as far as the server can tell."""
    return {"v": core.ENC_VERSION, "from": device, "salt": b64(16),
            "keys": {d: b64(core.ENC_WRAP_BYTES) for d in to}}


class ImageTests(DirectMessageTests):
    def upload(self, session, data=WEBP, image_id=None, part=None):
        """Sends an image up in parts, as Buddy does; returns its id and
        whatever came back (nothing, when it all arrived)."""
        image_id = image_id or os.urandom(16).hex()
        text = base64.b64encode(data).decode("ascii")
        part = part or core.IMAGE_PART_CHARS
        pieces = [text[i:i + part] for i in range(0, len(text), part)] or [""]
        answers = []
        for seq, piece in enumerate(pieces):
            answer = self.request(session, type="image_part", id=image_id, seq=seq, data=piece,
                                  last=seq == len(pieces) - 1)
            if answer:
                answers.append(answer)
        return image_id, answers

    def test_a_room_image_goes_up_in_parts_and_everyone_fetches_it(self):
        a, b = self.user("Alice"), self.user("Bob")
        data = b"RIFF\x00\x00\x00\x00WEBPVP8 " + os.urandom(60_000)
        image_id, answers = self.upload(a, data)
        self.assertEqual(answers, [])
        sent = self.request(a, type="send", room="global", text="look", image={"id": image_id, "w": 640, "h": 480})
        self.assertEqual(sent["message"]["image"], {"id": image_id, "w": 640, "h": 480})
        self.assertEqual(b.last("message")["message"]["image"]["id"], image_id)   # not the bytes
        self.assertNotIn("data", json.dumps(b.last("message")))
        got = self.request(b, type="get_image", id=image_id)
        self.assertEqual((got["type"], base64.b64decode(got["data"])), ("image", data))
        # No text needed with an image; the same upload can't be sent twice.
        image_id, _ = self.upload(a)
        self.assertEqual(self.request(a, type="send", room="global", text="  ",
                                      image={"id": image_id, "w": 1, "h": 1})["message"]["text"], "")
        self.assertEqual(self.request(a, type="send", room="global", text="again",
                                      image={"id": image_id, "w": 1, "h": 1})["code"], "bad_image")

    def test_parts_must_arrive_in_order_and_fit(self):
        a = self.user("Alice")
        image_id = os.urandom(16).hex()
        self.request(a, type="image_part", id=image_id, seq=0, data=b64(30))
        self.assertEqual(self.request(a, type="image_part", id=image_id, seq=2, data=b64(30))["code"], "bad_image")
        too_big = b"RIFF\x00\x00\x00\x00WEBP" + bytes(core.MAX_IMAGE_BYTES)
        _id, answers = self.upload(a, too_big)
        self.assertEqual(answers[0]["code"], "too_big")
        self.assertEqual(self.request(a, type="image_part", id=image_id, seq=0,
                                      data="x" * (core.IMAGE_PART_CHARS + 4))["code"], "bad_image")
        # Only pictures in rooms, of a sensible size, and only the one this connection uploaded.
        image_id, _ = self.upload(a, b"MZ\x90\x00 not a picture")
        self.assertEqual(self.request(a, type="send", room="global", text="hi",
                                      image={"id": image_id, "w": 5, "h": 5})["code"], "bad_image")
        image_id, _ = self.upload(a)
        for size in ({"w": 0, "h": 5}, {"w": 5, "h": core.MAX_IMAGE_SIDE + 1}):
            self.assertEqual(self.request(a, type="send", room="global", text="hi",
                                          image={"id": image_id, **size})["code"], "bad_image")
        other = self.user("Bob")
        self.assertEqual(self.request(other, type="send", room="global", text="hi",
                                      image={"id": image_id, "w": 5, "h": 5})["code"], "bad_image")

    def test_images_are_limited_per_person(self):
        a = self.user("Alice")
        for _ in range(core.IMAGE_LIMIT[0]):
            self.assertEqual(self.upload(a)[1], [])
        self.assertEqual(self.upload(a)[1][0]["code"], "rate_limited")

    def test_images_expire_after_a_week_but_the_message_stays(self):
        a, b = self.user("Alice"), self.user("Bob")
        image_id, _ = self.upload(a)
        self.request(a, type="send", room="global", text="soon gone", image={"id": image_id, "w": 2, "h": 2})
        self.clock.now += core.IMAGE_DAYS * 86400 + 1
        self.core.purge()
        self.assertEqual(self.request(b, type="get_image", id=image_id)["code"], "no_image")
        self.request(b, type="join", room="global")
        message = b.last("history")["messages"][-1]
        self.assertEqual((message["text"], message["image"]), ("soon gone", {"id": image_id, "gone": True}))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_deleting_the_message_deletes_its_image(self):
        a, b = self.user("Alice"), self.user("Bob")
        image_id, _ = self.upload(a)
        sent = self.request(a, type="send", room="global", text="oops", image={"id": image_id, "w": 2, "h": 2})
        self.request(a, type="delete", id=sent["message"]["id"])
        self.assertEqual(self.request(b, type="get_image", id=image_id)["code"], "no_image")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_editing_keeps_the_image_and_may_empty_the_text(self):
        a = self.user("Alice")
        image_id, _ = self.upload(a)
        sent = self.request(a, type="send", room="global", text="caption", image={"id": image_id, "w": 2, "h": 2})
        edited = self.request(a, type="edit", id=sent["message"]["id"], text="")
        self.assertEqual((edited["message"]["text"], edited["message"]["image"]["id"]), ("", image_id))
        plain = self.request(a, type="send", room="global", text="no image")
        self.assertEqual(self.request(a, type="edit", id=plain["message"]["id"], text="")["code"], "bad_message")

    def test_a_dm_image_is_encrypted_and_only_the_two_can_fetch_it(self):
        a, b, c = self.user("Alice"), self.user("Bob"), self.user("Carol")
        self.buddies(a, b)
        room = self.open_dm(a, b)["id"]
        device = self.register(a)
        sealed = os.urandom(5000)   # ciphertext: no picture header to check
        image_id, _ = self.upload(a, sealed)
        keys = image_enc(device)
        text = dict(enc(device), body=b64(16))   # no caption: just the tag
        sent = self.request(a, type="send", room=room, enc=text, image={"id": image_id, "w": 3, "h": 4, "enc": keys})
        self.assertEqual(sent["message"]["image"], {"id": image_id, "w": 3, "h": 4, "enc": keys})
        self.assertEqual(base64.b64decode(self.request(b, type="get_image", id=image_id)["data"]), sealed)
        self.assertEqual(self.request(c, type="get_image", id=image_id)["code"], "no_image")
        # Without its wrapped keys (an older Buddy), or from a PC that isn't theirs: refused.
        image_id, _ = self.upload(a, sealed)
        self.assertEqual(self.request(a, type="send", room=room, enc=enc(device),
                                      image={"id": image_id, "w": 3, "h": 4})["code"], "update_required")
        image_id, _ = self.upload(a, sealed)
        self.assertEqual(self.request(a, type="send", room=room, enc=enc(device),
                                      image={"id": image_id, "w": 3, "h": 4,
                                             "enc": image_enc("fedcba9876543210")})["code"], "no_device")
        # An empty caption only goes with an image.
        self.assertEqual(self.request(a, type="send", room=room, enc=text)["code"], "bad_message")

    def test_replies_say_they_quote_an_image_and_reports_carry_it(self):
        a, b = self.user("Alice"), self.user("Bob")
        image_id, _ = self.upload(a)
        sent = self.request(a, type="send", room="global", text="", image={"id": image_id, "w": 2, "h": 2})
        reply = self.request(b, type="send", room="global", text="nice", reply_to=sent["message"]["id"])
        self.assertTrue(reply["message"]["reply"]["image"])
        self.request(b, type="report", id=sent["message"]["id"], reason="spam")
        report = self.store.open_reports()[0]
        self.assertEqual(report["image"], image_id)
        self.assertEqual(self.core._reports_payload()["reports"][0]["image"], image_id)

    def test_welcome_says_images_are_taken(self):
        limits = self.user("Alice").last("welcome")["limits"]
        self.assertEqual((limits["max_image_bytes"], limits["image_days"], limits["image_part_chars"]),
                         (core.MAX_IMAGE_BYTES, core.IMAGE_DAYS, core.IMAGE_PART_CHARS))

    def test_a_part_fits_in_one_frame(self):
        # Read from net.py's source: importing it needs `websockets`, which the tests don't.
        import re
        import pathlib
        source = (pathlib.Path(core.__file__).parent / "net.py").read_text(encoding="utf-8")
        limit = eval(re.search(r"^MAX_FRAME_BYTES = ([0-9 *]+)", source, re.M).group(1))
        frame = json.dumps({"type": "image_part", "id": "f" * 32, "seq": 99, "data": "A" * core.IMAGE_PART_CHARS,
                            "last": False})
        self.assertLess(len(frame), limit)



GIF = b"GIF89a" + os.urandom(2000)


def giphy_answer(ids, total=1000, **rendition) -> bytes:
    """What GIPHY's search and trending send, cut down to what gifs.py reads."""
    def media(name, size):
        return {"url": f"https://media1.giphy.com/media/{name}.gif", "width": "200", "height": "150",
                "size": str(size), "webp": f"https://media1.giphy.com/media/{name}.webp", "webp_size": str(size)}
    data = [{"id": i, "title": f"{i} GIF", "user": {"display_name": f"maker of {i}"},
             "images": {"original": rendition.get("original", media(f"{i}/giphy", 300_000)),
                        "fixed_width_small": media(f"{i}/100w", 20_000)}} for i in ids]
    return json.dumps({"data": data, "pagination": {"total_count": total, "count": len(data)}}).encode()


class FakeGiphy:
    """Stands in for net.py's fetch: the tests answer each download."""

    def __init__(self):
        self.asked = []   # [url, max_bytes, done]

    def __call__(self, url, max_bytes, done):
        self.asked.append([url, max_bytes, done])

    def api_calls(self):
        return [a for a in self.asked if a[0].startswith(gifs.API)]

    def answer(self, match, data):
        """Answers every download waiting whose address has `match` in it."""
        waiting = [a for a in self.asked if match in a[0]]
        self.asked = [a for a in self.asked if match not in a[0]]
        for _url, _most, done in waiting:
            done(data)
        return len(waiting)


class GifTests(ImageTests):
    def setUp(self):
        super().setUp()
        self.giphy = FakeGiphy()
        self.core = core.NetworkCore(self.store, clock=self.clock, giphy=gifs.GiphySettings("KEY123"),
                                     fetch=self.giphy)

    def search(self, session, q="", offset=0, answer=None):
        self.request(session, type="gif_search", q=q, offset=offset, nonce=7)
        if answer is not None:
            self.giphy.answer("api.giphy.com", answer)
        return session.inbox[-1]

    def test_without_a_key_theres_no_gif_search(self):
        plain = core.NetworkCore(self.store, clock=self.clock)
        s = FakeSession()
        plain.handle(s, json.dumps({"type": "hello", "v": core.PROTOCOL_VERSION}))
        self.assertFalse(s.last("welcome")["limits"]["gifs"])
        plain.handle(s, json.dumps({"type": "gif_search", "q": "cat"}))
        self.assertEqual(s.last("error")["code"], "no_gifs")
        self.assertTrue(self.user("Alice").last("welcome")["limits"]["gifs"])

    def test_a_search_goes_to_giphy_with_the_servers_key_and_comes_back_with_previews(self):
        a = self.user("Alice")
        self.search(a, "  happy \n cat ")
        (url, _most, _done), = self.giphy.api_calls()
        self.assertTrue(url.startswith(gifs.API + "search?"))
        self.assertIn("api_key=KEY123", url)
        self.assertIn("q=happy+cat", url)
        self.assertIn("rating=pg-13", url)
        self.assertNotIn("Alice", url)   # nothing about who's asking
        self.giphy.answer("api.giphy.com", giphy_answer(["abc", "def"]))
        results = a.last("gif_results")
        self.assertEqual((results["nonce"], results["more"], results["next"]), (7, True, gifs.PAGE))
        self.assertEqual(results["results"][0], {"id": "abc", "w": 200, "h": 150, "title": "abc GIF",
                                                  "user": "maker of abc"})
        self.assertNotIn("giphy.com", json.dumps(results))   # the Buddy never gets GIPHY's addresses
        # The previews: the small WebP, downloaded here and passed on.
        self.assertEqual(self.giphy.answer("abc/100w.webp", GIF), 1)
        self.assertEqual(base64.b64decode(a.last("gif_thumb")["data"]), GIF)
        # Trending, for an empty search.
        self.search(a, "")
        self.assertTrue(self.giphy.api_calls()[0][0].startswith(gifs.API + "trending?"))

    def test_searches_are_kept_and_shared_so_giphy_is_asked_once(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.search(a, "cat")
        self.search(b, "CAT")                      # asked while the first is on its way
        self.assertEqual(len(self.giphy.api_calls()), 1)
        self.giphy.answer("api.giphy.com", giphy_answer(["abc"]))
        self.assertEqual(a.last("gif_results")["results"][0]["id"], "abc")
        self.assertEqual(b.last("gif_results")["results"][0]["id"], "abc")
        self.assertEqual(len(self.giphy.asked), 1)   # one preview download for both
        self.giphy.answer("abc/100w", GIF)
        self.assertEqual(a.last("gif_thumb")["id"], b.last("gif_thumb")["id"])
        # Later, the same search comes from what's kept - previews too.
        c = self.user("Carol")
        self.search(c, "cat")
        self.assertEqual((self.giphy.asked, c.last("gif_thumb")["id"]), ([], "abc"))
        self.clock.now += gifs.SEARCH_KEEP + 1     # until it's old
        self.search(c, "cat")
        self.assertEqual(len(self.giphy.api_calls()), 1)

    def test_a_new_search_replaces_the_one_waiting(self):
        a = self.user("Alice")
        self.search(a, "cat")
        self.search(a, "dog")
        self.giphy.answer("q=cat", giphy_answer(["abc"]))
        self.assertEqual([p for p in a.inbox if p["type"] == "gif_results"], [])
        self.giphy.answer("q=dog", giphy_answer(["dog1"]))
        self.assertEqual(a.last("gif_results")["results"][0]["id"], "dog1")

    def test_the_hours_giphy_calls_are_shared_out_and_then_wait(self):
        self.core.giphy.calls_per_hour = 3
        people = [self.user(f"Person {n}", ip=f"10.0.{n}.1") for n in range(4)]
        for n, person in enumerate(people[:3]):
            self.search(person, f"search {n}", answer=giphy_answer(["abc"]))
        busy = self.search(people[3], "one more")
        self.assertEqual(busy["code"], "gif_busy")
        self.assertIn("minute", busy["message"])
        self.assertEqual(self.search(people[3], "search 0")["type"], "gif_results")   # kept ones still work
        self.clock.now += 3601
        self.search(people[3], "one more", answer=giphy_answer(["abc"]))
        self.assertEqual(people[3].last("gif_results")["results"][0]["id"], "abc")

    def test_each_person_has_a_limit(self):
        a = self.user("Alice")
        self.search(a, "cat", answer=giphy_answer(["abc"]))
        for _ in range(gifs.GIF_SEARCH_LIMIT[0] - 1):
            self.search(a, "cat")
        self.assertEqual(self.search(a, "cat")["code"], "rate_limited")

    def test_bad_searches_and_giphy_failing(self):
        a = self.user("Alice")
        for bad in ({"q": 5}, {"q": "x" * (gifs.QUERY_MAX + 1)}, {"q": "cat", "offset": -1},
                    {"q": "cat", "offset": gifs.MAX_OFFSET + 1}, {"q": "cat", "offset": True}):
            self.assertEqual(self.request(a, type="gif_search", **bad)["code"], "bad_request", bad)
        self.search(a, "cat")
        self.giphy.answer("api.giphy.com", None)     # GIPHY didn't answer
        self.assertEqual((a.last("error")["code"], a.last("error")["nonce"]), ("gif_failed", 7))
        self.search(a, "cat", answer=b"<html>not json</html>")
        self.assertEqual(a.last("error")["code"], "gif_failed")

    def test_only_giphys_own_media_is_ever_downloaded(self):
        a = self.user("Alice")
        elsewhere = {"url": "https://evil.example/x.gif", "width": "10", "height": "10", "size": "100"}
        plain_http = {"url": "http://media1.giphy.com/x.gif", "width": "10", "height": "10", "size": "100"}
        self.search(a, "cat", answer=giphy_answer(["abc"], original=elsewhere))
        self.assertEqual(a.last("gif_results")["results"], [])   # nothing it could send
        self.search(a, "dog", answer=giphy_answer(["abc"], original=plain_http))
        self.assertEqual(a.last("gif_results")["results"], [])
        self.assertTrue(all(gifs.media_url(url) for url, _m, _d in self.giphy.asked))
        for url in ("https://media1.giphy.com.evil.example/x", "https://media1.giphy.com:8443/x",
                    "https://evil.example/?https://media1.giphy.com/"):
            self.assertIsNone(gifs.media_url(url), url)
        self.assertTrue(gifs.media_url("https://i.giphy.com/media/abc/giphy.webp"))

    def test_the_gif_picked_comes_back_to_send_and_the_message_credits_giphy(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.search(a, "cat", answer=giphy_answer(["abc"]))
        self.request(a, type="gif_get", id="abc")
        url, most, _done = self.giphy.asked[-1]
        self.assertTrue(url.endswith("abc/giphy.webp"))
        self.assertEqual(most, gifs.SEND_MAX_BYTES)
        self.giphy.answer("abc/giphy", GIF)
        got = a.last("gif_data")
        self.assertEqual((got["id"], got["user"], base64.b64decode(got["data"])), ("abc", "maker of abc", GIF))
        # Buddy shrinks it to a WebP and sends it up like any picture, saying which GIF it was.
        image_id, _ = self.upload(a)
        sent = self.request(a, type="send", room="global", text="",
                            image={"id": image_id, "w": 200, "h": 150, "gif": "abc"})
        credit = {"source": "giphy", "user": "maker of abc"}
        self.assertEqual(sent["message"]["image"]["credit"], credit)
        self.request(b, type="join", room="global")
        self.assertEqual(b.last("history")["messages"][-1]["image"]["credit"], credit)
        # Only a GIF this connection was handed can be credited.
        image_id, _ = self.upload(b)
        self.assertEqual(self.request(b, type="send", room="global", text="mine",
                                      image={"id": image_id, "w": 5, "h": 5, "gif": "abc"})["code"], "bad_image")

    def test_a_gif_to_send_must_have_been_found_and_download(self):
        a = self.user("Alice")
        self.assertEqual(self.request(a, type="gif_get", id="nothere")["code"], "no_gif")
        self.assertEqual(self.request(a, type="gif_get", id="../x")["code"], "bad_request")
        self.search(a, "cat", answer=giphy_answer(["abc"]))
        self.request(a, type="gif_get", id="abc")
        self.giphy.answer("abc/giphy", b"<html>an error page</html>")
        self.assertEqual((a.last("error")["code"], a.last("error")["id"]), ("gif_failed", "abc"))

    def test_a_gif_in_a_dm_is_credited_too(self):
        a, b = self.user("Alice"), self.user("Bob")
        self.buddies(a, b)
        room = self.open_dm(a, b)["id"]
        device = self.register(a)
        self.search(a, "cat", answer=giphy_answer(["abc"]))
        self.request(a, type="gif_get", id="abc")
        self.giphy.answer("abc/giphy", GIF)
        image_id, _ = self.upload(a, os.urandom(3000))
        sent = self.request(a, type="send", room=room, enc=dict(enc(device), body=b64(16)),
                            image={"id": image_id, "w": 200, "h": 150, "enc": image_enc(device), "gif": "abc"})
        self.assertEqual(sent["message"]["image"]["credit"]["source"], "giphy")

    def test_the_gif_and_a_page_of_previews_fit_in_a_clients_queue(self):
        import re
        import pathlib
        source = (pathlib.Path(core.__file__).parent / "net.py").read_text(encoding="utf-8")
        queued = eval(re.search(r"^MAX_QUEUED_BYTES = ([0-9 *]+)", source, re.M).group(1))

        def as_base64(n):
            return n * 4 // 3 + 200
        self.assertLess(as_base64(gifs.SEND_MAX_BYTES) + gifs.PAGE * as_base64(gifs.THUMB_MAX_BYTES), queued)

    def test_an_old_database_gets_the_credit_column(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE images (id TEXT PRIMARY KEY, room TEXT NOT NULL, author TEXT NOT NULL, "
                       "ts REAL NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL, enc TEXT, "
                       "size INTEGER NOT NULL, data BLOB NOT NULL)")
            db.commit()
            db.close()
            store = Store(path)
            try:
                columns = {row[1] for row in store.db.execute("PRAGMA table_info(images)")}
                self.assertIn("credit", columns)
            finally:
                store.close()

    def test_settings_from_the_environment(self):
        self.assertIsNone(gifs.GiphySettings.from_environment({}))
        settings = gifs.GiphySettings.from_environment({"GIPHY_API_KEY": " k ", "GIPHY_RATING": "G",
                                                         "GIPHY_CALLS_PER_HOUR": "40"})
        self.assertEqual((settings.key, settings.rating, settings.calls_per_hour), ("k", "g", 40))
        odd = gifs.GiphySettings.from_environment({"GIPHY_API_KEY": "k", "GIPHY_RATING": "nc-17",
                                                    "GIPHY_CALLS_PER_HOUR": "lots"})
        self.assertEqual((odd.rating, odd.calls_per_hour), (gifs.DEFAULT_RATING, gifs.DEFAULT_CALLS_PER_HOUR))

if __name__ == "__main__":
    unittest.main()
