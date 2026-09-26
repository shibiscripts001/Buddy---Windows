"""Pictures sent with an Ask Buddy question (app/pages/manual_chat/pictures.py,
conversation.py, agent.py) - no Qt, no network."""

import base64
import io
import unittest

import _paths  # noqa: F401
from pages.manual_chat import pictures
from pages.manual_chat.agent import ManualAgent
from pages.manual_chat.conversation import YOU, ChatSessions, block_view
from pages.manual_chat.llm import Reply

try:
    from PIL import Image
except ImportError:
    raise unittest.SkipTest("Pillow not installed")


def picture(w, h, fmt="PNG", mode="RGB", **save):
    out = io.BytesIO()
    Image.new(mode, (w, h), (10, 120, 200, 90)[:len(mode)]).save(out, fmt, **save)
    return out.getvalue()


class PrepareTests(unittest.TestCase):
    def test_scaled_to_fit_as_a_jpeg_without_metadata(self):
        exif = Image.Exif()
        exif[0x010F] = "SecretCam"
        pic = pictures.prepare(picture(4000, 2000, "JPEG", exif=exif.tobytes()))
        data = base64.b64decode(pic.data)
        self.assertEqual((pic.w, pic.h), (pictures.MAX_SIDE, pictures.MAX_SIDE // 2))
        self.assertEqual(Image.open(io.BytesIO(data)).format, "JPEG")
        self.assertNotIn(b"SecretCam", data)
        self.assertTrue(pic.preview.startswith("data:image/jpeg;base64,"))
        self.assertEqual(pic.for_model(), {"mime": "image/jpeg", "data": pic.data})

    def test_transparency_is_flattened_and_junk_refused(self):
        pic = pictures.prepare(picture(50, 50, "PNG", mode="RGBA"))
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(pic.data))).mode, "RGB")
        with self.assertRaises(pictures.PictureError):
            pictures.prepare(b"not a picture")


class HistoryTests(unittest.TestCase):
    def test_pictures_go_once_and_later_turns_are_told_about_them(self):
        chats = ChatSessions()
        block = chats.add(YOU, "What's this?", images=["data:image/jpeg;base64,AAAA"])
        self.assertEqual(block_view(1, block)["images"], ["data:image/jpeg;base64,AAAA"])
        chats.record_turn("What's this?", "The Color page.", 10, pictures=1)
        self.assertEqual(chats.history[0], {"role": "user",
                                            "content": "What's this?\n\n[I attached a picture with this question.]"})
        self.assertNotIn("images", chats.history[0])
        self.assertIn("[1 picture(s)]", chats.export_text())

    def test_the_agent_puts_them_on_this_question_only(self):
        seen = []

        class FakeLLM:
            def chat(self, system, messages, tools):
                seen.append([dict(m) for m in messages])
                return Reply(content="It's the Color page.")

        agent = ManualAgent(None, FakeLLM())
        image = {"mime": "image/jpeg", "data": "QUJD"}
        result = agent.ask("What's this?", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
                           images=[image])
        self.assertEqual(result.answer, "It's the Color page.")
        self.assertEqual(seen[0][-1]["images"], [image])
        self.assertNotIn("images", seen[0][0])


if __name__ == "__main__":
    unittest.main()
