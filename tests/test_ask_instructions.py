"""Ask Buddy's custom instructions: the file in ~/.buddy/ask_buddy
(pages/manual_chat/ask_folder.py), its Settings field, and how the agent
puts them in the prompt. Always against a temp folder, never the real one."""

import os
import shutil
import tempfile
import unittest

import _paths  # noqa: F401
from pages.manual_chat import ask_folder
from pages.manual_chat.agent import ManualAgent, SYSTEM_PROMPT, build_system_prompt
from pages.manual_chat.llm import Reply


class UI:
    def __init__(self):
        self.statuses = []
        self.parent = None

    def status(self, text, tone=""):
        self.statuses.append((text, tone))


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.folder = os.path.join(self.root, "ask_buddy")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class FolderTests(Temp):
    def test_no_file_is_no_instructions(self):
        self.assertEqual(ask_folder.read_instructions(self.folder), "")
        self.assertEqual(ask_folder.instructions_for_prompt(self.folder), "")

    def test_saved_whole_and_read_back(self):
        ask_folder.write_instructions("We deliver ProRes 422 HQ.\r\nClips: SHOW_EP_SCENE.", self.folder)
        path = ask_folder.instructions_path(self.folder)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"We deliver ProRes 422 HQ.\nClips: SHOW_EP_SCENE.\n")
        self.assertEqual(ask_folder.instructions_for_prompt(self.folder),
                         "We deliver ProRes 422 HQ.\nClips: SHOW_EP_SCENE.")
        self.assertEqual(os.listdir(self.folder), ["instructions.md"])   # no temp file left behind

    def test_blank_removes_the_file(self):
        ask_folder.write_instructions("Something", self.folder)
        ask_folder.write_instructions("  \n ", self.folder)
        self.assertFalse(os.path.exists(ask_folder.instructions_path(self.folder)))
        ask_folder.write_instructions("", self.folder)   # and again, with nothing there

    def test_an_edit_in_a_text_editor_is_read(self):
        os.makedirs(self.folder)
        with open(ask_folder.instructions_path(self.folder), "w", encoding="utf-8-sig") as fh:
            fh.write("Écrit dans le Bloc-notes\n")   # Notepad's BOM is not part of the text
        self.assertEqual(ask_folder.instructions_for_prompt(self.folder), "Écrit dans le Bloc-notes")

    def test_the_prompt_gets_at_most_the_limit(self):
        ask_folder.write_instructions("x" * (ask_folder.MAX_INSTRUCTION_CHARS + 50), self.folder)
        self.assertEqual(len(ask_folder.instructions_for_prompt(self.folder)), ask_folder.MAX_INSTRUCTION_CHARS)
        self.assertEqual(len(ask_folder.read_instructions(self.folder).strip()), ask_folder.MAX_INSTRUCTION_CHARS + 50)


class PromptTests(unittest.TestCase):
    def test_without_instructions_the_prompt_is_unchanged(self):
        self.assertNotIn("custom_instructions", build_system_prompt())
        self.assertNotIn("custom_instructions", build_system_prompt(instructions="  \n"))

    def test_instructions_come_after_the_rules(self):
        prompt = build_system_prompt(instructions="Deliver {4K} DCI.")
        self.assertTrue(prompt.startswith(SYSTEM_PROMPT))
        self.assertIn("<custom_instructions>\nDeliver {4K} DCI.\n</custom_instructions>", prompt)
        self.assertGreater(prompt.index("Deliver {4K}"), prompt.index("Buddy's tools"))

    def test_the_agent_sends_them(self):
        seen = []

        class FakeLLM:
            def chat(self, system, messages, tools):
                seen.append(system)
                return Reply(content="ProRes 422 HQ, as you deliver.")

        ManualAgent(None, FakeLLM(), instructions="We deliver ProRes 422 HQ.").ask("What codec?", [])
        self.assertIn("We deliver ProRes 422 HQ.", seen[0])


class SettingsTests(Temp):
    def page(self):
        from pages.manual_chat.settings_panel import ChatSettingsMixin

        folder = self.folder

        class Page(ChatSettingsMixin):
            ask_folder = folder
            settings = {"provider": "gemini"}

        return Page()

    def fields(self, page):
        from unittest import mock
        with mock.patch("pages.manual_chat.settings_panel.default_data_paths", return_value=("", "")):
            return {f["key"]: f for f in page.settings_fields() if f and f.get("key")}

    def test_the_box_edits_the_file(self):
        page, ui = self.page(), UI()
        self.assertEqual(self.fields(page)["instructions"]["kind"], "textarea")
        page.on_setting("instructions", "Keep answers short.", ui)
        self.assertEqual(ui.statuses[-1][1], "success")
        self.assertEqual(ask_folder.read_instructions(self.folder), "Keep answers short.\n")
        self.assertEqual(self.fields(page)["instructions"]["value"], "Keep answers short.\n")
        self.assertIsNone(self.fields(page)["instructions"]["error"])

    def test_too_long_says_how_much_is_sent(self):
        page = self.page()
        page.on_setting("instructions", "y" * (ask_folder.MAX_INSTRUCTION_CHARS + 1), UI())
        self.assertIn("8,000", self.fields(page)["instructions"]["error"])


if __name__ == "__main__":
    unittest.main()
