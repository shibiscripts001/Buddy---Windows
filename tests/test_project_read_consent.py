"""Ask Buddy reads the open project only where the user has allowed what it
reads to go to the AI provider: a server on their PC or network needs no
leave, a cloud one needs it for that address."""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.manual_chat import llm
from pages.manual_chat.agent import ManualAgent

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
    from pages.manual_chat import page as chat_page
    from pages.manual_chat.page import ManualChatPage
    HAVE_QT = True
except ImportError:
    HAVE_QT = False


class Reply:
    def __init__(self, content="", calls=()):
        self.content, self.tool_calls = content, list(calls)

    @property
    def wants_tools(self):
        return bool(self.tool_calls)


class ScriptedLLM:
    """Asks for the project, then answers with whatever it was told."""

    def __init__(self):
        self.seen = []

    def chat(self, system, messages, specs):
        self.seen.append(messages[-1])
        if len(self.seen) == 1:
            return Reply(calls=[llm.ToolCall("1", "project_state", {"include_timeline": True})])
        return Reply("answer")


class AgentTests(unittest.TestCase):
    def test_a_refused_read_never_reaches_resolve_or_the_model(self):
        model, reached = ScriptedLLM(), []
        agent = ManualAgent(None, model, connect_resolve=lambda: reached.append(1) or object(), allow_reads=False)
        result = agent.ask("What is my frame rate?")
        self.assertEqual(reached, [])
        self.assertIn("has not allowed", model.seen[1]["content"])
        self.assertEqual(result.answer, "answer")
        self.assertEqual(result.events[0].summary, "not allowed")

    def test_it_does_not_prefetch_the_clip_either(self):
        model, reached = ScriptedLLM(), []
        agent = ManualAgent(None, model, connect_resolve=lambda: reached.append(1) or object(), allow_reads=False,
                            prefetch_focus=True)
        agent.ask("Explain this clip")
        self.assertEqual(reached, [])
        self.assertNotIn("Live Resolve context", model.seen[0]["content"])

    def test_a_proposal_is_not_built_without_it_either(self):
        agent = ManualAgent(None, None, connect_resolve=lambda: object(), allow_writes=True, allow_reads=False)
        from pages.manual_chat.agent import AgentResult
        text = agent._do_propose_action({"action_id": "add_markers", "arguments": {}}, AgentResult())
        self.assertIn("has not allowed", text)

    def test_reading_is_the_default_for_an_agent_built_without_the_flag(self):
        self.assertTrue(ManualAgent(None, None).allow_reads)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PageTests(unittest.TestCase):
    class Host:
        connected = True
        controller = None                                              # nothing to ask of Resolve here
        shared_settings = {"theme": "Resolve"}

        def __init__(self, store):
            self.store = store

        def tool_settings(self, _tool, defaults):
            return self.store

        def theme_tokens(self):
            from core.theme import get_theme_tokens
            return get_theme_tokens("Resolve")

    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patch = mock.patch.object(ManualChatPage, "ask_folder", folder.name)
        patch.start()
        self.addCleanup(patch.stop)

        class Store(dict):
            def save(self):
                pass

        self.store = Store(chat_page.DEFAULTS)
        self.page = ManualChatPage(self.Host(self.store))
        self.addCleanup(delete, self.page)

    def use(self, provider, **settings):
        self.store.update({"provider": provider, "model_" + provider: "m", **settings})

    def test_a_local_server_needs_no_leave_a_cloud_one_does(self):
        self.use("ollama")
        self.assertTrue(self.page.project_reads_allowed())
        self.use("anthropic", api_key_anthropic="k")
        self.assertFalse(self.page.project_reads_allowed())
        self.store["project_read_consent"] = "anthropic"
        self.assertTrue(self.page.project_reads_allowed())
        self.store["project_read_consent"] = "openai"                  # given for something else
        self.assertFalse(self.page.project_reads_allowed())

    def test_the_leave_is_for_the_address(self):
        self.use("openai", api_key_openai="k", base_url_openai="https://relay.example/v1")
        self.store["project_read_consent"] = "relay.example"
        self.assertTrue(self.page.project_reads_allowed())
        self.store["base_url_openai"] = "https://other.example/v1"
        self.assertFalse(self.page.project_reads_allowed())

    def test_it_asks_once_and_a_no_is_not_asked_again(self):
        self.use("anthropic", api_key_anthropic="k")
        with mock.patch.object(chat_page, "confirm", return_value=False) as ask:
            self.page._ask_to_read_project()
            self.page._ask_to_read_project()
            self.assertEqual(ask.call_count, 1)
        self.assertFalse(self.page.project_reads_allowed())
        with mock.patch.object(chat_page, "confirm", return_value=True) as ask:
            self.page._read_declined = None                            # Settings' switch resets it
            self.page._ask_to_read_project()
            self.page._ask_to_read_project()
            self.assertEqual(ask.call_count, 1)
        self.assertTrue(self.page.project_reads_allowed())
        self.assertEqual(self.store["project_read_consent"], "anthropic")

    def test_no_question_when_nothing_would_be_read(self):
        self.use("anthropic", api_key_anthropic="k")
        self.page.host.connected = False                               # Resolve isn't connected
        with mock.patch.object(chat_page, "confirm") as ask:
            self.page._ask_to_read_project()
            ask.assert_not_called()
        self.page.host.connected = True
        self.use("ollama")                                             # a local server
        with mock.patch.object(chat_page, "confirm") as ask:
            self.page._ask_to_read_project()
            ask.assert_not_called()

    def test_the_agent_it_builds_follows_the_consent(self):
        self.use("anthropic", api_key_anthropic="k")
        self.assertFalse(self.page._build_agent().allow_reads)
        self.store["project_read_consent"] = "anthropic"
        self.assertTrue(self.page._build_agent().allow_reads)

    def test_settings_switch_sets_and_clears_it(self):
        self.use("anthropic", api_key_anthropic="k")
        self.page.on_setting("allow_project_reads", True, mock.Mock())
        self.assertTrue(self.page.project_reads_allowed())
        self.page.on_setting("allow_project_reads", False, mock.Mock())
        self.assertFalse(self.page.project_reads_allowed())
        keys = [f["key"] for f in self.page.settings_fields() if f and f.get("key")]
        self.assertIn("allow_project_reads", keys)
        self.use("ollama")
        keys = [f["key"] for f in self.page.settings_fields() if f and f.get("key")]
        self.assertNotIn("allow_project_reads", keys)                  # a local server has nothing to allow


if __name__ == "__main__":
    unittest.main()
