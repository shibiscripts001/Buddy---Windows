"""Buddy Network's chat features on Buddy's side - @mentions, generated
avatars, and how replies, edits and mentions show (mentions.py, avatars.py,
render.py, export.py). No Qt, no network."""

import unittest

import _paths  # noqa: F401
from pages.buddy_network import avatars, export, mentions, render
from server import core as server_core

COLORS = {"text": "#fff", "muted": "#888", "me": "#0f0", "other": "#fff", "link": "#0af", "warning": "#fa0",
          "mention": "#223344"}
ME = {"id": "bbbb" + "0" * 28, "tag": "bbbb00", "name": "Tess B"}


def msg(i, text, author="a1b2c3", name="Al", **extra):
    return {"id": i, "room": "global", "text": text, "ts": 1_750_000_000.0 + i, "deleted": False,
            "author": {"id": author + "0" * 26, "tag": author, "name": name}, **extra}


def html(messages, **kw):
    options = {"my_id": ME["id"], "room_name": "#Global", "more": False, "links": [], "colors": COLORS,
               "now": 1_750_000_100.0, "me": ME, "can_reply": True, **kw}
    return render.room_html(messages, **options)


class MentionTests(unittest.TestCase):
    def test_tokens_and_finding_them(self):
        self.assertEqual(mentions.token(ME), "@Tess B#bbbb00")
        text = "hi @Tess B#bbbb00, @Shuttle#1 and @nobody#zzzzzz or a@b#abcdef1"
        self.assertEqual([(n, t) for _s, _e, n, t in mentions.find(text)], [("Tess B", "bbbb00"), ("Shuttle", "1")])
        start, end, _n, _t = mentions.find(text)[0]
        self.assertEqual(text[start:end], "@Tess B#bbbb00")
        self.assertTrue(mentions.mentions_me("ping @tess b#bbbb00", ME))     # names match whatever the case
        self.assertFalse(mentions.mentions_me("ping @Tess#bbbb00", ME))      # the whole name
        self.assertFalse(mentions.mentions_me("ping @Tess B#bbbb01", ME))    # the right person
        self.assertFalse(mentions.mentions_me("hi", None))

    def test_the_server_finds_exactly_the_same(self):
        for text in ("@Tess B#bbbb00", "x @A#1 @BB#12 @C#123", "@x y z#abcdef!", "@@Sam#abcdef", "@Sam#ABCDEF"):
            self.assertEqual(server_core.MENTION.findall(text), [(n, t) for _s, _e, n, t in mentions.find(text)], text)
        self.assertEqual(server_core.MENTION.pattern, mentions.MENTION.pattern)

    def test_what_the_completion_matches_against(self):
        self.assertEqual(mentions.partial_before("hello @Te", 9), (6, "Te"))
        self.assertEqual(mentions.partial_before("@", 1), (0, ""))
        self.assertEqual(mentions.partial_before("line\n@Sa", 8), (5, "Sa"))   # at the start of a line too
        for text in ("email me@home", "@Tess B#bb", "no at sign", "@" + "x" * 25):
            self.assertIsNone(mentions.partial_before(text, len(text)), text)


class AvatarTests(unittest.TestCase):
    def test_the_same_for_everyone_and_different_for_each_person(self):
        first = avatars.avatar("a" * 32)
        self.assertEqual(first, avatars.avatar("a" * 32))
        others = {str(avatars.avatar(f"{i:032x}")) for i in range(50)}
        self.assertGreater(len(others), 45)
        color, cells = first
        self.assertRegex(color, r"^#[0-9a-f]{6}$")
        self.assertTrue(cells)
        for column, row in cells:   # mirrored left to right
            self.assertIn((avatars.GRID - 1 - column, row), cells)
            self.assertTrue(0 <= column < avatars.GRID and 0 <= row < avatars.GRID)


class AvatarSeedTests(unittest.TestCase):
    def test_a_chosen_avatar_or_the_original(self):
        seed = avatars.new_seed()
        self.assertRegex(seed, r"^[0-9a-f]{8}$")
        self.assertTrue(avatars.SEED.fullmatch(seed))
        person = {"id": "a" * 32, "avatar": seed}
        self.assertEqual(avatars.key_of(person), seed)
        self.assertEqual(avatars.key_of({"id": "a" * 32, "avatar": ""}), "a" * 32)
        self.assertEqual(avatars.key_of({"id": "a" * 32, "avatar": "../evil"}), "a" * 32)   # never trusted blindly
        self.assertNotEqual(avatars.avatar(seed), avatars.avatar("a" * 32))
        self.assertEqual(avatars.MAX_SAVED, server_core.MAX_SAVED_AVATARS)
        self.assertEqual(avatars.SEED.pattern, server_core._AVATAR_SEED.pattern)

    def test_messages_draw_the_chosen_one(self):
        m = msg(1, "hi")
        m["author"]["avatar"] = "0f1e2d3c"
        self.assertIn('src="bn-avatar:0f1e2d3c"', html([m]))


class RenderTests(unittest.TestCase):
    def test_replies_quote_the_original(self):
        out = html([msg(2, "yes", reply={"id": 1, "text": "Anyone around?",
                                         "author": {"id": "c" * 32, "tag": "cccccc", "name": "Cara"}})])
        self.assertIn("Cara: Anyone around?", out)
        # A DM's quote has no text: it comes from the loaded messages.
        dm_reply = msg(3, "sure", reply={"id": 1, "text": "", "author": {"id": "c" * 32, "name": "Cara"}})
        self.assertIn("Cara: the secret plan", html([dm_reply], lookup={1: msg(1, "the secret plan")}))
        self.assertIn("an earlier message", html([dm_reply], lookup={}))
        self.assertIn("no longer on the server", html([msg(4, "ok", reply={"id": 1, "gone": True})]))
        self.assertIn("a deleted message", html([msg(5, "ok", reply={"id": 1, "deleted": True, "text": ""})]))
        blocked = msg(6, "hm", reply={"id": 1, "text": "spam", "author": {"id": "d" * 32, "name": "Dan"}})
        out = html([blocked], hidden={"d" * 32})
        self.assertNotIn("spam", out)
        self.assertIn("a message from someone you", out)

    def test_links_on_each_message(self):
        mine = msg(1, "mine", author="bbbb00", name="Tess B")
        mine["author"]["id"] = ME["id"]
        out = html([mine, msg(2, "theirs"), msg(3, "", unreadable=True)])
        self.assertIn("bn-reply:1", out)
        self.assertIn("bn-edit:1", out)
        self.assertIn("bn-reply:2", out)
        self.assertNotIn("bn-edit:2", out)          # only your own
        self.assertNotIn("bn-reply:3", out)         # nothing to quote
        self.assertNotIn("bn-reply:1", html([mine], can_reply=False))
        saved = dict(mine, saved_only=True)          # the server has forgotten it
        out = html([saved])
        self.assertNotIn("bn-edit:1", out)
        self.assertNotIn("bn-delete:1", out)

    def test_edited_mentions_and_avatars(self):
        out = html([msg(1, "fixed typo", edited=1_750_000_050.0)])
        self.assertIn("(edited)", out)
        self.assertIn('src="bn-avatar:a1b2c3', out)
        gone = msg(2, "left")
        gone["author"] = {"id": "", "tag": "", "name": None}   # a deleted account
        self.assertNotIn("bn-avatar", html([gone]))
        out = html([msg(3, "thanks @Tess B#bbbb00 <b>!</b>")])
        self.assertIn("background-color:#223344", out)   # it mentions you
        self.assertIn(">@Tess B</span>", out)
        self.assertNotIn("<b>!", out)
        self.assertNotIn("background-color", html([msg(4, "thanks @Sam#abcdef")]))

    def test_search_note_replaces_the_top_line(self):
        out = html([msg(1, "x")], top_note="1 message matches \"x\"")
        self.assertIn("1 message matches", out)
        self.assertNotIn("Start of", out)


class StampAndPurgeTests(unittest.TestCase):
    def test_every_message_shows_its_date_and_time(self):
        from datetime import datetime
        now = datetime(2026, 9, 25, 15, 0).timestamp()
        self.assertEqual(render.stamp(datetime(2026, 9, 25, 14, 32).timestamp(), now), "25 Sep, 14:32")
        self.assertEqual(render.stamp(datetime(2025, 3, 3, 9, 5).timestamp(), now), "3 Mar 2025, 09:05")
        m = msg(1, "hi")
        self.assertIn(render.stamp(m["ts"], 1_750_000_100.0), html([m]))

    def test_delete_forever_is_the_owners_even_on_a_deleted_message(self):
        gone = msg(2, "", deleted=True)
        self.assertIn("bn-purge:1", html([msg(1, "hi")], owner=True))
        self.assertIn("bn-purge:2", html([gone], owner=True))       # takes the placeholder away too
        self.assertNotIn("bn-purge", html([msg(1, "hi"), gone], admin=True))
        self.assertNotIn("bn-purge", html([dict(msg(3, "old"), saved_only=True)], owner=True))

    def test_the_saved_copy_forgets_it_backup_and_all(self):
        import os
        import tempfile
        from pages.buddy_network import archive
        with tempfile.TemporaryDirectory() as folder:
            saved = archive.ChatArchive(folder)
            room, other = "dm-aaaa-bbbb", {"id": "bbbb", "tag": "bbbb", "name": "Bo"}
            saved.save("wss://x", "aaaa", room, other, [msg(1, "keep me", room=room), msg(2, "secret", room=room)])
            saved.forget("wss://x", "aaaa", room, 2)
            self.assertEqual([m["text"] for m in archive.ChatArchive(folder).messages("wss://x", "aaaa", room)],
                             ["keep me"])
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    with open(os.path.join(root, name), "rb") as f:
                        self.assertNotIn(b"secret", f.read(), name)


class ActionColorTests(unittest.TestCase):
    def test_each_action_link_has_its_own_colour(self):
        colors = dict(COLORS, reply="#111111", edit="#222222", delete="#333333", report="#444444",
                      purge="#555555")
        mine = msg(1, "mine", author="bbbb00", name="Tess B")
        mine["author"]["id"] = ME["id"]
        out = html([mine, msg(2, "theirs")], colors=colors, owner=True)
        for kind, colour in (("reply", "#111111"), ("edit", "#222222"), ("delete", "#333333"),
                             ("report", "#444444"), ("purge", "#555555")):
            self.assertRegex(out, rf'href="bn-{kind}:\d+" style="color:{colour};', kind)
        self.assertIn(f'href="bn-reply:1" style="color:{COLORS["muted"]};', html([mine]))   # none given: muted

    def test_every_theme_keeps_them_apart_and_readable(self):
        try:
            from core import theme
            from pages.buddy_network.shades import action_colors
        except ImportError:
            self.skipTest("PySide6 not installed")
        for key in theme.THEMES:
            for sub in theme._PALETTES:
                try:
                    tokens = theme.get_theme_tokens(key, sub)
                except Exception:
                    continue
                shades = action_colors(tokens, tokens["outline"], tokens["surface"])
                self.assertEqual(set(shades), {"reply", "edit", "delete", "report", "purge"})
                for a in shades:
                    self.assertGreaterEqual(theme.contrast_ratio(shades[a], tokens["surface"]), 3.0, (key, sub, a))
                    for b in shades:
                        if a < b:
                            self.assertGreaterEqual(theme.hue_distance(shades[a], shades[b]), 0.07, (key, sub, a, b))


class ExportTests(unittest.TestCase):
    def test_edits_and_replies_are_noted(self):
        messages = [msg(1, "first", edited=1.0), msg(2, "second", reply={"id": 1, "author": {"name": "Al"}})]
        text = export.as_text("#Global", messages, 1_750_000_100.0)
        self.assertIn("Al #a1b2c3 (edited): first", text)
        self.assertIn("(replying to Al)", text)
        self.assertIn("replying to Al", export.as_html("#Global", messages, 1_750_000_100.0))


if __name__ == "__main__":
    unittest.main()
