"""Ask Buddy's conversations and answer rendering (pages/manual_chat/
conversation.py) - what the web view draws and what the model is re-sent.
No Qt."""

import json
import os
import random
import tempfile
import unittest

import _paths
from pages.manual_chat import config
from pages.manual_chat import chat_store
from pages.manual_chat.conversation import (
    BUDDY,
    HELP_SUGGESTION,
    SUGGESTION_POOL,
    WELCOME,
    YOU,
    ChatSessions,
    block_view,
    help_text,
    is_help,
    md_to_html,
    md_to_plain,
    shuffled_pool,
    suggestions,
)


class RenderTests(unittest.TestCase):
    def test_nothing_the_model_writes_becomes_markup(self):
        out = md_to_html('<script>alert(1)</script> <img src=x onerror=alert(1)> "quoted"')
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)

    def test_only_web_links_are_links(self):
        out = md_to_html("[a](https://x.com/a) [b](javascript:alert(1)) javascript:alert(2) http://y.org/p.")
        self.assertIn('<a href="https://x.com/a">a</a>', out)
        self.assertIn('<a href="http://y.org/p">http://y.org/p</a>.', out)   # the full stop stays outside
        self.assertEqual(out.count("<a "), 2)

    def test_a_link_stops_at_an_escaped_quote(self):
        out = md_to_html('See "https://a.com/x?a=1&b=2".')
        self.assertIn('href="https://a.com/x?a=1&amp;b=2"', out)

    def test_code_is_left_alone(self):
        out = md_to_html("Use `**not bold** <b>` then\n```\nx = <y> **z**\n```")
        self.assertIn("<code>**not bold** &lt;b&gt;</code>", out)
        self.assertIn("<pre><code>x = &lt;y&gt; **z**</code></pre>", out)

    def test_lists_headings_and_citations(self):
        out = md_to_html("## Steps\n1. one (Chapter 3, p. 12)\n2. **two**\n\n- a\n- *b*\nend (general knowledge)")
        self.assertIn("<h4>Steps</h4>", out)
        self.assertIn('<ol><li>one <span class="cite" data-page="12">(Chapter 3, p. 12)</span></li><li><b>two</b></li></ol>', out)
        self.assertIn("<ul><li>a</li><li><i>b</i></li></ul>", out)
        self.assertIn('<span class="source">(general knowledge)</span>', out)

    def test_a_citation_carries_the_page_to_open(self):
        self.assertIn('data-page="1198"', md_to_html("Fades (Chapter 55, pp.1198–1202)"))
        self.assertIn('data-page="40"', md_to_html("See (Chapter 2 – Setup Step 3 – page 40)"))
        # A number in the chapter's title is not a page.
        self.assertNotIn("data-page", md_to_html("See (Chapter 2 – Step 3)"))

    def test_a_list_not_starting_at_one_keeps_its_number(self):
        self.assertIn('<ol start="3">', md_to_html("3. third\n4. fourth"))

    def test_plain_export_drops_markup(self):
        self.assertEqual(md_to_plain("**Hi** there (Chapter 2, p. 4)"), "Hi there")


class SessionTests(unittest.TestCase):
    def test_tool_recommendations_stay_with_saved_chats(self):
        chats = ChatSessions()
        first = {"tool_id": "media_relink", "label": "Open Media Relink", "reason": "Offline clips"}
        second = {"tool_id": "transcribe", "label": "Open Transcribe", "reason": "Make subtitles"}
        chats.add(YOU, "Fix my media")
        chats.current.offer = first
        chats.new_chat()
        self.assertIsNone(chats.current.offer)
        chats.add(YOU, "Make subtitles")
        chats.current.offer = second
        chats.go(0)
        self.assertEqual(chats.current.offer, first)
        with tempfile.TemporaryDirectory() as folder:
            chat_store.save(folder, chats)
            restored, warnings = chat_store.load(folder)
        self.assertEqual(warnings, [])
        self.assertEqual(restored.current.offer, first)
        restored.go(1)
        self.assertEqual(restored.current.offer, second)
        restored.delete(0)
        self.assertEqual(restored.current.offer, second)
        restored.delete_all()
        self.assertIsNone(restored.current.offer)

    def test_old_chats_and_malformed_offers_still_load(self):
        data = ChatSessions().to_data()
        del data["chats"][0]["offer"]
        self.assertIsNone(ChatSessions.from_data(data).current.offer)
        for offer in (None, [], "media_relink", {"tool_id": 123},
                      {"tool_id": "media_relink", "label": [], "reason": "test"}):
            data["chats"][0]["offer"] = offer
            self.assertIsNone(ChatSessions.from_data(data).current.offer)

    def test_saved_chats_restore_search_and_small_picture_previews(self):
        chats = ChatSessions()
        chats.add(YOU, "How do I fix a soft shot?", images=["data:image/jpeg;base64,YQ=="])
        chats.add(BUDDY, "Check resolution")
        chats.record_turn("How do I fix a soft shot?", "Check resolution", 6)
        chats.new_chat()
        chats.add(YOU, "A different question")
        chats.rename(0, "Soft footage")
        with tempfile.TemporaryDirectory() as folder:
            chat_store.save(folder, chats)
            restored, warnings = chat_store.load(folder)
        self.assertEqual(warnings, [])
        self.assertEqual((restored.index, len(restored.chats)), (1, 2))
        self.assertEqual(restored.summaries("resolution")[0]["title"], "Soft footage")
        self.assertEqual(restored.chats[0].blocks[1]["images"], ["data:image/jpeg;base64,YQ=="])
        self.assertEqual(restored.chats[0].history[0]["content"], "How do I fix a soft shot?")

    def _three_chats(self):
        chats = ChatSessions()
        for question in ("first", "second", "third"):
            chats.add(YOU, question)
            chats.new_chat()
        chats.go(1)                     # on "second"; an empty fourth is last
        return chats

    def test_a_launch_starts_on_a_fresh_chat_keeping_the_saved_ones(self):
        chats = self._three_chats()                 # on "second"; an empty fourth is last
        chats.start_fresh()
        self.assertEqual([c.title for c in chats.chats], ["first", "second", "third", "New chat"])
        self.assertEqual(chats.index, 3)
        self.assertTrue(chats.is_empty())
        self.assertEqual([b["body"] for b in chats.blocks], [WELCOME])
        self.assertEqual(chats.chats[1].blocks[1]["body"], "second")      # nothing lost
        chats.start_fresh()                          # again, with nothing asked: still just one empty chat
        self.assertEqual(len(chats.chats), 4)

    def test_deleting_another_chat_keeps_the_open_one(self):
        chats = self._three_chats()
        self.assertTrue(chats.delete(0))
        self.assertEqual(chats.current.title, "second")
        self.assertEqual([c.title for c in chats.chats], ["second", "third", "New chat"])
        self.assertTrue(chats.delete(2))
        self.assertEqual(chats.current.title, "second")

    def test_deleting_the_open_chat_opens_the_next_or_the_one_before(self):
        chats = self._three_chats()
        chats.delete(1)
        self.assertEqual(chats.current.title, "third")
        chats.go(len(chats.chats) - 1)
        chats.delete(chats.index)       # the last one: the one before takes over
        self.assertEqual(chats.current.title, "third")

    def test_deleting_the_only_chat_leaves_a_fresh_one(self):
        chats = ChatSessions()
        chats.add(YOU, "only")
        self.assertTrue(chats.delete(0))
        self.assertEqual((len(chats.chats), chats.index), (1, 0))
        self.assertTrue(chats.is_empty())
        self.assertEqual([b["body"] for b in chats.blocks], [WELCOME])

    def test_deleting_a_chat_that_isnt_there_does_nothing(self):
        chats = self._three_chats()
        for index in (-1, 4, "1", None):
            self.assertFalse(chats.delete(index))
        self.assertEqual(len(chats.chats), 4)

    def test_delete_all_leaves_one_empty_chat(self):
        chats = self._three_chats()
        chats.delete_all()
        self.assertEqual((len(chats.chats), chats.index), (1, 0))
        self.assertTrue(chats.is_empty())
        self.assertEqual(chats.summaries("first"), [])

    def test_a_deleted_chat_does_not_live_on_in_the_backup(self):
        chats = self._three_chats()
        with tempfile.TemporaryDirectory() as folder:
            chat_store.save(folder, chats)
            chat_store.save(folder, chats)          # a .bak now holds all three
            chats.delete(0)
            chat_store.save(folder, chats, forget_backup=True)
            names = os.listdir(folder)
            restored, _warnings = chat_store.load(folder)
            with open(os.path.join(folder, chat_store.FILE_NAME), encoding="utf-8") as fh:
                saved = fh.read()
        self.assertEqual(names, [chat_store.FILE_NAME])
        self.assertNotIn('"first"', saved)
        self.assertEqual([c.title for c in restored.chats], ["second", "third", "New chat"])

    def test_erase_removes_the_file_its_backup_and_damaged_copies(self):
        with tempfile.TemporaryDirectory() as folder:
            chat_store.save(folder, self._three_chats())
            chat_store.save(folder, self._three_chats())
            with open(os.path.join(folder, chat_store.FILE_NAME + ".corrupt-1"), "w") as fh:
                fh.write("{")
            with open(os.path.join(folder, "instructions.md"), "w") as fh:
                fh.write("keep me")
            chat_store.erase(folder)
            self.assertEqual(os.listdir(folder), ["instructions.md"])
            restored, _warnings = chat_store.load(folder)
        self.assertTrue(restored.is_empty())
        chat_store.erase(os.path.join(folder, "gone"))     # no folder: nothing to do

    def test_starts_with_the_welcome(self):
        chats = ChatSessions()
        self.assertEqual([b["body"] for b in chats.blocks], [WELCOME])
        self.assertTrue(chats.is_empty())

    def test_history_keeps_whole_recent_turns(self):
        chats = ChatSessions()
        for i in range(5):
            chats.record_turn(f"q{i}", f"a{i}", limit=3)
        self.assertEqual([m["content"] for m in chats.history], ["q2", "a2", "q3", "a3", "q4", "a4"])
        self.assertEqual(chats.turns, 3)
        chats.trim(0)
        self.assertEqual(chats.history, [])

    def test_new_chat_keeps_the_old_one_and_never_stacks_empties(self):
        chats = ChatSessions()
        self.assertFalse(chats.new_chat())           # nothing to keep yet
        chats.add(YOU, "hello")
        chats.record_turn("hello", "hi", 6)
        self.assertTrue(chats.new_chat())
        self.assertEqual((chats.index, len(chats.chats)), (1, 2))
        self.assertFalse(chats.new_chat())           # already on a fresh one
        self.assertTrue(chats.go(0))
        self.assertEqual(chats.history[0]["content"], "hello")
        self.assertFalse(chats.go(5))

    def test_export_is_readable_text(self):
        chats = ChatSessions()
        chats.add(YOU, "Q?")
        chats.add(BUDDY, "**A** (Chapter 1, p. 2)", trace=["search_manual: q"])
        text = chats.export_text()
        self.assertIn("You:\n  Q?", text)
        self.assertIn("Buddy:\n  A\n  [search_manual: q]", text)

    def test_block_view(self):
        chats = ChatSessions()
        chats.add("Error", "boom", error=True, copyable=True)
        view = block_view(1, chats.blocks[1])
        self.assertEqual((view["role"], view["copyable"], view["i"]), ("error", True, 1))
        self.assertEqual(block_view(0, chats.blocks[0])["role"], "buddy")


class SuggestionTests(unittest.TestCase):
    def test_three_of_different_kinds_then_help(self):
        pool = shuffled_pool(random.Random(1))
        row = suggestions(pool, connected=True)
        self.assertEqual(len(row), 4)
        self.assertEqual(row[-1], HELP_SUGGESTION)
        self.assertTrue(is_help(row[-1]))                                   # answered locally, not by the model
        kinds = [next(g for g, qs in SUGGESTION_POOL.items() if q in qs) for q in row[:-1]]
        self.assertEqual(kinds, ["resolve", "project", "tools"])

    def test_project_questions_wait_for_a_connection(self):
        row = suggestions(shuffled_pool(random.Random(2)), connected=False)
        self.assertEqual(len(set(row)), 4)
        self.assertFalse(set(row) & set(SUGGESTION_POOL["project"]))

    def test_the_pick_holds_for_the_run_and_changes_between_runs(self):
        pool = shuffled_pool(random.Random(3))
        self.assertEqual(suggestions(pool, True), suggestions(pool, True))  # a new chat keeps them
        rows = {tuple(suggestions(shuffled_pool(random.Random(seed)), True)) for seed in range(20)}
        self.assertGreater(len(rows), 10)

    def test_every_suggestion_is_translated(self):
        path = _paths.APP / "core" / "translations" / "manual_chat.json"
        known = json.loads(path.read_text(encoding="utf-8"))
        missing = [q for qs in SUGGESTION_POOL.values() for q in qs + [HELP_SUGGESTION] if q not in known]
        self.assertEqual(missing, [])


class HelpAndConfigTests(unittest.TestCase):
    def test_help_is_answered_locally(self):
        for word in ("help", "HELP!", "/help", "what can you do?"):
            self.assertTrue(is_help(word), word)
        self.assertFalse(is_help("help me with keyframes"))
        self.assertIn("I cannot change your project", help_text(False))
        self.assertIn("I can change your project", help_text(True))

    def test_limits_are_clamped(self):
        self.assertEqual(config.history_limit({"history_turns": 999}), config.MAX_HISTORY_TURNS)
        self.assertEqual(config.history_limit({"history_turns": "junk"}), config.DEFAULT_HISTORY_TURNS)
        self.assertEqual(config.max_steps_limit({"max_steps": 0}), config.DEFAULT_MAX_STEPS)

    def test_legacy_single_key_moves_to_its_provider(self):
        class S(dict):
            saved = False

            def save(self):
                self.saved = True

        s = S(provider="gemini", api_key="k", model="m")
        config.migrate_legacy_settings(s)
        self.assertEqual((config.api_key_from_settings(s), s["model_gemini"], s.saved), ("k", "m", True))
        self.assertNotIn("api_key", s)                      # the old shared field doesn't keep the key as typed


if __name__ == "__main__":
    unittest.main()
