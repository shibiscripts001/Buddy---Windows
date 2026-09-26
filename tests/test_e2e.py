"""Buddy Network's end-to-end encrypted DMs, saved chats and moving to
another PC, on Buddy's side (app/pages/buddy_network/e2e.py, archive.py,
export.py, transfer.py) - no Qt, no network.
The encryption is the real thing (the `cryptography` package); the server's
checks come from server/core.py, so the two can't drift apart."""

import json
import os
import tempfile
import unittest

import _paths  # noqa: F401
from pages.buddy_network import archive, e2e, export, render, transfer
from server import common as server_common
from server import core as server_core

ROOM = "dm-aaaa-bbbb"


def sealed(text="hello", sender="aaaa", me=None, to=()):
    me = me or e2e.DeviceKey.generate()
    return me, e2e.encrypt(text, room=ROOM, sender=sender, me=me,
                           recipients={k.id: k.public for k in (me, *to)})


class EncryptionTests(unittest.TestCase):
    def test_every_pc_of_both_people_can_read_it(self):
        alice_home, alice_work, bob = (e2e.DeviceKey.generate() for _ in range(3))
        me, enc = sealed("héllo 👋\nsecond line", me=alice_home, to=(alice_work, bob))
        known = {alice_home.id: alice_home.public}
        for reader in (alice_home, alice_work, bob):
            self.assertEqual(e2e.decrypt(enc, room=ROOM, sender="aaaa", me=reader, sender_keys=known),
                             "héllo 👋\nsecond line")
        self.assertNotIn("hello", json.dumps(enc))

    def test_anything_changed_or_misplaced_is_unreadable(self):
        bob = e2e.DeviceKey.generate()
        me, enc = sealed(to=(bob,))
        known = {me.id: me.public}
        read = lambda e, **kw: e2e.decrypt(e, **{"room": ROOM, "sender": "aaaa", "me": bob,  # noqa: E731
                                                 "sender_keys": known, **kw})
        self.assertEqual(read(enc), "hello")
        self.assertIsNone(read(enc, room="dm-aaaa-cccc"))           # moved to another chat
        self.assertIsNone(read(enc, sender="cccc"))                 # passed off as someone else's
        self.assertIsNone(read(enc, sender_keys={}))                # from a PC this one doesn't know
        self.assertIsNone(read(enc, me=e2e.DeviceKey.generate()))   # not encrypted for this PC
        impostor = e2e.DeviceKey.generate()                         # a key the server slipped in
        self.assertIsNone(read(enc, sender_keys={me.id: impostor.public}))
        body = bytearray(e2e.unb64(enc["body"]))
        body[0] ^= 1
        self.assertIsNone(read({**enc, "body": e2e.b64(bytes(body))}))
        for junk in (None, "x", {}, {**enc, "v": 9}, {**enc, "keys": "x"}, {**enc, "salt": "!!"}):
            self.assertIsNone(read(junk), junk)

    def test_the_server_accepts_what_buddy_sends(self):
        me, enc = sealed("x" * server_core.MAX_MESSAGE_CHARS, to=[e2e.DeviceKey.generate()
                                                                  for _ in range(2 * server_core.MAX_DEVICES - 1)])
        self.assertLessEqual(len(json.dumps(enc)), 32 * 1024 - 200)   # net.py's frame limit, with room
        stored = json.loads(server_core.clean_enc(enc, {me.id}))
        self.assertEqual(stored, enc)
        widest = "\U0001F600" * server_core.MAX_MESSAGE_CHARS          # 4 bytes a character in UTF-8
        server_core.clean_enc(sealed(widest, me=me)[1], {me.id})
        self.assertEqual(e2e.ENC_VERSION, server_core.ENC_VERSION)
        self.assertEqual(e2e.device_id(me.public), server_common.device_id(me.public))

    def test_safety_code_is_the_same_on_both_screens_and_changes_with_any_key(self):
        a1, a2, b = (e2e.DeviceKey.generate().public for _ in range(3))
        code = e2e.safety_code("alice", [a1, a2], "bob", [b])
        self.assertEqual(code, e2e.safety_code("bob", [b], "alice", [a2, a1]))
        self.assertRegex(code, r"^\d{5}( \d{5}){5}$")
        swapped = e2e.DeviceKey.generate().public
        self.assertNotEqual(code, e2e.safety_code("alice", [a1, a2], "bob", [swapped]))
        self.assertNotEqual(code, e2e.safety_code("alice", [a1], "bob", [b]))

    def test_keys_from_the_server_are_named_here(self):
        key = e2e.DeviceKey.generate()
        self.assertEqual(e2e.keys_by_device([key.public_b64, "junk", e2e.b64(b"short")]),
                         {key.id: key.public})

    def test_windows_lock_round_trips(self):
        how, locked = e2e.protect(b"secret key bytes")
        self.assertEqual(e2e.unprotect(how, locked), b"secret key bytes")
        if how == "dpapi":
            self.assertNotIn(b"secret", e2e.unb64(locked))
        with self.assertRaises(e2e.CryptoError):
            e2e.unprotect("dpapi", e2e.b64(b"not a dpapi blob"))


URL = "wss://chat.example.com"


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_this_pcs_key_is_kept_and_locked(self):
        key = e2e.KeyStore(self.folder).device(URL)
        again = e2e.KeyStore(self.folder).device(URL)
        self.assertEqual((again.id, again.public), (key.id, key.public))
        self.assertNotEqual(e2e.KeyStore(self.folder).device("ws://localhost:8765").id, key.id)
        with open(os.path.join(self.folder, e2e.KEYS_FILENAME), encoding="utf-8") as f:
            saved = json.load(f)["servers"][URL]["device"]
        if saved["protection"] == "dpapi":
            self.assertNotIn(e2e.b64(key.private_bytes()), json.dumps(saved))

    def test_a_key_that_cant_be_unlocked_is_replaced_with_a_warning(self):
        store = e2e.KeyStore(self.folder)
        old = store.device(URL)
        path = os.path.join(self.folder, e2e.KEYS_FILENAME)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["servers"][URL]["device"] = {"protection": "dpapi", "private": e2e.b64(b"from another PC")}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        fresh = e2e.KeyStore(self.folder)
        self.assertNotEqual(fresh.device(URL).id, old.id)
        self.assertTrue(fresh.warnings)

    def test_first_keys_are_trusted_a_new_one_warns_until_accepted(self):
        store = e2e.KeyStore(self.folder)
        pc1, pc2, pc3 = (e2e.DeviceKey.generate() for _ in range(3))
        self.assertFalse(store.remember(URL, "bob", {}))                       # not updated yet
        self.assertFalse(store.remember(URL, "bob", {pc1.id: pc1.public}))     # first sight
        self.assertTrue(store.remember(URL, "bob", {pc1.id: pc1.public, pc2.id: pc2.public}))
        self.assertTrue(e2e.KeyStore(self.folder).keys_changed(URL, "bob"))
        self.assertEqual(store.accepted(URL, "bob"), {pc1.id})
        store.accept(URL, "bob", [pc1.id, pc2.id])
        self.assertFalse(store.remember(URL, "bob", {pc2.id: pc2.public}))     # a PC gone: fine
        self.assertFalse(store.remember(URL, "bob", {pc1.id: pc1.public}))     # and back again
        self.assertTrue(store.remember(URL, "bob", {pc3.id: pc3.public}))
        # Every key seen still opens old messages.
        self.assertEqual(set(store.known_keys(URL, "bob")), {pc1.id, pc2.id, pc3.id})

    def test_a_key_shown_for_a_moment_still_warns_and_accepting_forgets_it(self):
        store = e2e.KeyStore(self.folder)
        pc1, sneaky = e2e.DeviceKey.generate(), e2e.DeviceKey.generate()
        store.remember(URL, "bob", {pc1.id: pc1.public})
        store.remember(URL, "bob", {pc1.id: pc1.public, sneaky.id: sneaky.public})
        self.assertTrue(store.remember(URL, "bob", {pc1.id: pc1.public}))      # taken away: still warns
        store.accept(URL, "bob", [pc1.id])
        self.assertFalse(store.keys_changed(URL, "bob"))
        self.assertEqual(set(store.known_keys(URL, "bob")), {pc1.id})          # nothing from it opens now

    def test_verified_code(self):
        store = e2e.KeyStore(self.folder)
        self.assertEqual(store.verified(URL, "bob"), "")
        store.set_verified(URL, "bob", "12345 67890")
        self.assertEqual(e2e.KeyStore(self.folder).verified(URL, "bob"), "12345 67890")


def dm(i, text, author="aaaa", deleted=False, unreadable=False, room=ROOM):
    return {"id": i, "room": room, "text": text, "ts": 1_750_000_000.0 + i, "deleted": deleted,
            "unreadable": unreadable, "e2e": True, "author": {"id": author, "tag": author[:6], "name": "Al"}}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, archive.FOLDER)
        self.other = {"id": "bbbb", "tag": "bbbb", "name": "Bo", "role": "user"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_saved_locked_and_read_back_oldest_first(self):
        saved = archive.ChatArchive(self.folder)
        self.assertEqual(saved.save(URL, "aaaa", ROOM, self.other,
                                    [dm(2, "second"), dm(1, "first"), dm(3, "", unreadable=True)]), 2)
        self.assertEqual(saved.save(URL, "aaaa", ROOM, self.other, [dm(2, "second")]), 0)
        again = archive.ChatArchive(self.folder)
        self.assertEqual([m["text"] for m in again.messages(URL, "aaaa", ROOM)], ["first", "second"])
        files = os.listdir(self.folder)
        self.assertEqual(len([f for f in files if f.endswith(archive.SUFFIX)]), 1)
        self.assertNotIn(ROOM, " ".join(files))                  # the names don't say who
        with open(os.path.join(self.folder, files[0]), encoding="utf-8") as f:
            raw = f.read()
        if '"dpapi"' in raw:
            self.assertNotIn("second", raw)
        [chat] = again.conversations(URL)
        self.assertEqual((chat["room"], chat["count"], chat["other"]["name"]), (ROOM, 2, "Bo"))
        self.assertEqual(again.conversations("ws://elsewhere"), [])

    def test_deleted_for_everyone_goes_from_the_copy_too(self):
        saved = archive.ChatArchive(self.folder)
        saved.save(URL, "aaaa", ROOM, self.other, [dm(1, "oops")])
        saved.mark_deleted(URL, "aaaa", ROOM, 1)
        saved.save(URL, "aaaa", ROOM, self.other, [dm(1, "oops")])   # an old copy can't bring it back
        [m] = archive.ChatArchive(self.folder).messages(URL, "aaaa", ROOM)
        self.assertEqual((m["deleted"], m["text"]), (True, ""))

    def test_delete_one_and_all(self):
        saved = archive.ChatArchive(self.folder)
        saved.save(URL, "aaaa", ROOM, self.other, [dm(1, "a")])
        saved.save(URL, "aaaa", "dm-aaaa-cccc", self.other, [dm(2, "b", room="dm-aaaa-cccc")])
        saved.delete(URL, "aaaa", ROOM)
        self.assertEqual([c["room"] for c in saved.conversations(URL)], ["dm-aaaa-cccc"])
        self.assertEqual(saved.delete_all(URL), 1)
        self.assertEqual(archive.ChatArchive(self.folder).conversations(URL), [])


def payload(**extra):
    return {"server": URL, "identity": {"id": "aaaa", "name": "Al", "token": "tok"},
            "keys": {"devices": [], "people": {}}, "chats": [], **extra}


class TransferTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def test_round_trip_and_nothing_readable_without_the_password(self):
        text = transfer.seal(payload(note="héllo"), self.PASSWORD)
        self.assertNotIn("tok", text)
        self.assertNotIn("héllo", text)
        self.assertEqual(transfer.open_file(text, self.PASSWORD)["note"], "héllo")
        with self.assertRaisesRegex(transfer.TransferError, "password"):
            transfer.open_file(text, self.PASSWORD + "!")

    def test_changed_damaged_or_foreign_files_are_refused(self):
        outer = json.loads(transfer.seal(payload(), self.PASSWORD))
        cases = {
            "changed": {**outer, "data": e2e.b64(bytes(b ^ (i == 0) for i, b in enumerate(e2e.unb64(outer["data"]))))},
            "weaker settings": {**outer, "kdf": {**outer["kdf"], "n": 2 ** 10}},   # bound in: can't be lowered
            "absurd settings": {**outer, "kdf": {**outer["kdf"], "n": 2 ** 30}},  # refused before any work
            "gigabytes of memory": {**outer, "kdf": {**outer["kdf"], "n": 2 ** 18, "r": 32, "p": 16}},
            "newer": {**outer, "v": 99},
            "foreign": {**outer, "kind": "something else"},
        }
        for why, broken in cases.items():
            with self.assertRaises(transfer.TransferError, msg=why):
                transfer.open_file(json.dumps(broken), self.PASSWORD)
        for junk in ("", "not json", "[]", "{}"):
            with self.assertRaises(transfer.TransferError):
                transfer.open_file(junk, self.PASSWORD)
        with self.assertRaises(transfer.TransferError):   # opens, but isn't a whole transfer
            transfer.open_file(transfer.seal({"server": URL}, self.PASSWORD), self.PASSWORD)

    def test_any_password_or_none_only_typed_the_same_twice(self):
        for password in ("", "a", "1234", self.PASSWORD):
            self.assertEqual(transfer.password_problem(password, password), "", password)
        self.assertTrue(transfer.password_problem(self.PASSWORD, "something else"))
        self.assertTrue(transfer.password_problem("", "x"))

    def test_a_file_without_a_password_opens_without_asking(self):
        with tempfile.TemporaryDirectory() as folder:
            open_one, locked = (os.path.join(folder, n) for n in ("open.buddynet", "locked.buddynet"))
            export.write(open_one, transfer.seal(payload(), ""))
            export.write(locked, transfer.seal(payload(), self.PASSWORD))
            self.assertEqual(transfer.read(open_one)["identity"]["id"], "aaaa")
            with self.assertRaises(transfer.NeedsPassword):
                transfer.read(locked)
            self.assertEqual(transfer.read(locked, self.PASSWORD)["identity"]["id"], "aaaa")
            with self.assertRaisesRegex(transfer.TransferError, "password"):
                transfer.read(locked, "")
            # Claiming "no password" on a locked file doesn't open it: the flag is bound in.
            with open(locked, encoding="utf-8") as f:
                outer = json.loads(f.read())
            with self.assertRaises(transfer.TransferError):
                transfer.open_file(json.dumps({**outer, "password": False}), "")
            # A file from before the password was optional says nothing: it asks.
            del outer["password"]
            self.assertTrue(transfer.has_password(json.dumps(outer)))


class MoveTests(unittest.TestCase):
    """The whole of a move, minus the file: keys and saved chats out of one
    PC's folder and into another's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_pc, self.new_pc = (os.path.join(self.tmp.name, n) for n in ("old", "new"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_new_pc_reads_what_the_old_one_could_and_keeps_its_own_key(self):
        old = e2e.KeyStore(self.old_pc)
        old_key = old.device(URL)
        bob = e2e.DeviceKey.generate()
        old.remember(URL, "bob", {bob.id: bob.public})
        old.set_verified(URL, "bob", "12345")
        to_old = e2e.encrypt("before the move", room=ROOM, sender="bob", me=bob,
                             recipients={old_key.id: old_key.public})

        new = e2e.KeyStore(self.new_pc)
        own_key = new.device(URL)   # it had already been used once
        to_new_pc = e2e.encrypt("to the new PC's own key", room=ROOM, sender="bob", me=bob,
                                recipients={own_key.id: own_key.public})
        new.import_server(URL, json.loads(json.dumps(old.export_server(URL))))

        again = e2e.KeyStore(self.new_pc)
        self.assertEqual(again.device(URL).id, old_key.id)                 # buddies see no change
        readers = again.readers(URL)
        self.assertEqual({k.id for k in readers}, {old_key.id, own_key.id})
        self.assertEqual(again.verified(URL, "bob"), "12345")
        known = again.known_keys(URL, "bob")
        for enc, text in ((to_old, "before the move"), (to_new_pc, "to the new PC's own key")):
            reader = next(k for k in readers if k.id in enc["keys"])
            self.assertEqual(e2e.decrypt(enc, room=ROOM, sender="bob", me=reader, sender_keys=known), text)
        with self.assertRaises(e2e.CryptoError):
            again.import_server(URL, {"devices": [], "people": {}})

    def test_saved_chats_come_along(self):
        other = {"id": "bbbb", "tag": "bbbb", "name": "Bo", "role": "user"}
        old = archive.ChatArchive(os.path.join(self.old_pc, archive.FOLDER))
        old.save(URL, "aaaa", ROOM, other, [dm(1, "one"), dm(2, "", deleted=True)])
        chats = json.loads(json.dumps(old.export_all(URL, "aaaa")))
        self.assertEqual(old.export_all(URL, "someone else"), [])
        new = archive.ChatArchive(os.path.join(self.new_pc, archive.FOLDER))
        self.assertEqual(new.import_all(URL, "aaaa", chats + ["junk", {"room": 5}]), 2)
        self.assertEqual([(m["text"], m["deleted"]) for m in new.messages(URL, "aaaa", ROOM)],
                         [("one", False), ("", True)])
        self.assertEqual(new.import_all(URL, "aaaa", chats), 0)            # twice changes nothing


class ExportTests(unittest.TestCase):
    MESSAGES = [dm(1, "hi <b>there</b>\nsee https://example.com"), dm(2, "", deleted=True),
                dm(3, "", unreadable=True)]

    def test_text(self):
        out = export.as_text("@Bo", self.MESSAGES, 1_750_000_100.0, "A note.")
        self.assertIn("Buddy Network – @Bo", out)
        self.assertIn("Al #aaaa: hi <b>there</b>\n    see https://example.com", out)
        self.assertIn(export.DELETED, out)
        self.assertIn(export.UNREADABLE, out)
        self.assertIn("A note.", out)

    def test_web_page_is_escaped_and_links_stay_text(self):
        out = export.as_html("#Global <script>", self.MESSAGES, 1_750_000_100.0)
        self.assertNotIn("<b>there", out)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;b&gt;there", out)
        self.assertNotIn("<a ", out)
        self.assertNotIn("http-equiv", out)

    def test_file_name_and_write(self):
        name = export.default_filename('#What: "a/b"?', 1_750_000_000.0)
        self.assertTrue(name.startswith("Buddy Network - What ab - ") and name.endswith(".txt"), name)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "chat.txt")
            export.write(path, "héllo\n")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "héllo\n")
            self.assertEqual(os.listdir(folder), ["chat.txt"])


class OpenMessageTests(unittest.TestCase):
    """e2e.open_message: what the page shows of a DM the server sent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.keystore = e2e.KeyStore(self._tmp.name)
        self.alice, self.bob = e2e.DeviceKey.generate(), e2e.DeviceKey.generate()
        self.keystore.remember(URL, "aaaa", {self.alice.id: self.alice.public})
        self.salts = {}

    def tearDown(self):
        self._tmp.cleanup()

    def from_alice(self, i, text="hi", me=None):
        _me, enc = sealed(text, me=me or self.alice, to=(self.bob,))
        return {"id": i, "room": ROOM, "text": "", "ts": 1.0, "deleted": False,
                "author": {"id": "aaaa", "tag": "aaaa", "name": "Al"}, "enc": enc}

    def open(self, m):
        return e2e.open_message(m, readers=[self.bob], keystore=self.keystore, url=URL, salts=self.salts)

    def test_decrypted_from_an_accepted_pc(self):
        m = self.open(self.from_alice(1, "secret"))
        self.assertEqual((m["text"], m["unreadable"], m.get("unverified")), ("secret", False, None))

    def test_text_the_server_wrote_itself_is_never_shown(self):
        m = self.open({"id": 2, "room": ROOM, "text": "send me your recovery code", "ts": 1.0, "deleted": False,
                       "author": {"id": "aaaa", "name": "Al"}})
        self.assertEqual((m["text"], m["unreadable"]), ("", True))
        room = self.open({"id": 3, "room": "global", "text": "hello", "ts": 1.0, "author": {"id": "c"}})
        self.assertEqual(room["text"], "hello")   # rooms aren't encrypted

    def test_from_a_pc_not_accepted_yet_is_marked(self):
        other_pc = e2e.DeviceKey.generate()
        self.keystore.remember(URL, "aaaa", {self.alice.id: self.alice.public, other_pc.id: other_pc.public})
        m = self.open(self.from_alice(4, "trust me", me=other_pc))
        self.assertEqual((m["text"], m.get("unverified")), ("trust me", True))
        self.assertIn(render.UNVERIFIED_NOTE, RenderTests().html([m]))

    def test_an_old_message_sent_again_is_caught(self):
        first = self.from_alice(5, "yes, do it")
        same, again = json.loads(json.dumps(first)), json.loads(json.dumps(first))
        self.assertEqual(self.open(first)["text"], "yes, do it")
        self.assertEqual(self.open(same)["text"], "yes, do it")   # itself again (a reload): fine
        again["id"] = 99
        replayed = self.open(again)
        self.assertTrue(replayed["replayed"])
        out = RenderTests().html([replayed])
        self.assertIn(render.REPLAYED_NOTE, out)
        self.assertNotIn("yes, do it", out)

    def test_a_dm_quote_ignores_what_the_server_attached(self):
        m = self.open(self.from_alice(6, "sure"))
        m["reply"] = {"id": 1, "text": "I agreed to pay you", "author": {"id": "aaaa", "name": "Al"}}
        out = RenderTests().html([m])
        self.assertNotIn("I agreed to pay you", out)
        self.assertIn("an earlier message", out)


class RenderTests(unittest.TestCase):
    COLORS = {"text": "#fff", "muted": "#888", "me": "#0f0", "other": "#fff", "link": "#0af", "warning": "#fa0"}

    def html(self, messages, **kw):
        return render.room_html(messages, my_id="bbbb", room_name="@Al", more=False, links=[],
                                colors=self.COLORS, now=1_750_000_000.0, **kw)

    def test_unreadable_says_so_and_cant_be_reported(self):
        out = self.html([dm(1, "", unreadable=True)])
        self.assertIn(render.UNREADABLE_NOTE, out)
        self.assertNotIn("bn-report:1", out)
        self.assertIn("bn-report:2", self.html([dm(2, "fine")]))

    def test_older_messages_from_the_saved_copy(self):
        self.assertIn("(saved on this PC)", self.html([dm(1, "x")], more_saved=True))
        self.assertIn("a copy is saved on this PC", self.html([dm(1, "x")], saved_copy=True))


if __name__ == "__main__":
    unittest.main()
