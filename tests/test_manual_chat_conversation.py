"""Ask Buddy's conversations and answer rendering (pages/manual_chat/
conversation.py) - what the web view draws and what the model is re-sent.
No Qt."""

import unittest

import _paths  # noqa: F401
from pages.manual_chat import config
from pages.manual_chat.conversation import (
    BUDDY,
    WELCOME,
    YOU,
    ChatSessions,
    block_view,
    help_text,
    is_help,
    md_to_html,
    md_to_plain,
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
        self.assertIn('<ol><li>one <span class="cite">(Chapter 3, p. 12)</span></li><li><b>two</b></li></ol>', out)
        self.assertIn("<ul><li>a</li><li><i>b</i></li></ul>", out)
        self.assertIn('<span class="source">(general knowledge)</span>', out)

    def test_a_list_not_starting_at_one_keeps_its_number(self):
        self.assertIn('<ol start="3">', md_to_html("3. third\n4. fourth"))

    def test_plain_export_drops_markup(self):
        self.assertEqual(md_to_plain("**Hi** there (Chapter 2, p. 4)"), "Hi there")


class SessionTests(unittest.TestCase):
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
        self.assertEqual((s["api_key_gemini"], s["model_gemini"], s.saved), ("k", "m", True))


if __name__ == "__main__":
    unittest.main()
