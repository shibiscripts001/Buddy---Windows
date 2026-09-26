"""The translation engines beside NLLB: MADLAD-400 (its language table,
model folder and input format - app/pages/transcribe/languages.py,
env_setup.py, worker.py) and AI translation's request/reply handling
(ai_translate.py, with a fake client - no network)."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _paths  # noqa: F401
import worker
from pages.transcribe import ai_translate as ai
from pages.transcribe import env_setup as es
from pages.transcribe import languages as L
from pages.transcribe import subtitles as st


class MadladTableTests(unittest.TestCase):
    def test_codes_are_nllb_codes_with_distinct_tags(self):
        self.assertGreaterEqual(len(L.MADLAD_CODES), 170)
        self.assertLessEqual(set(L.MADLAD_CODES), set(L.NLLB_LANGUAGES))
        tags = list(L.MADLAD_CODES.values())
        self.assertEqual(len(tags), len(set(tags)), "two languages share one MADLAD tag")
        for tag in tags:
            self.assertRegex(tag, r"^[a-z]{2,3}(_[A-Z][a-z]{3}|_[A-Z]{2})?$")

    def test_tags_of_common_and_tricky_languages(self):
        # Checked against the model's own shared_vocabulary.json.
        want = {"eng_Latn": "en", "deu_Latn": "de", "jpn_Jpan": "ja", "zho_Hans": "zh",
                "zho_Hant": "zh_Hant", "arb_Arab": "ar", "pes_Arab": "fa", "nob_Latn": "no",
                "tgl_Latn": "fil", "jav_Latn": "jv", "ace_Arab": "ace_Arab", "ace_Latn": "ace"}
        for code, tag in want.items():
            self.assertEqual(L.MADLAD_CODES[code], tag, code)
        self.assertNotIn("yue_Hant", L.MADLAD_CODES)     # MADLAD has no Cantonese


class MadladModelTests(unittest.TestCase):
    def folder(self, vocab: list, size: int = 1):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in d.iterdir()] and d.rmdir())
        (d / "model.bin").write_bytes(b"x" * size)
        (d / "tokenizer.json").write_text("{}", encoding="utf-8")
        (d / "shared_vocabulary.json").write_text(json.dumps(vocab), encoding="utf-8")
        return d

    def test_offered_and_identified(self):
        entry = es.TRANSLATION_BY_ID["madlad-3b"]
        self.assertEqual(entry["family"], "madlad")
        self.assertEqual(es.translation_family("madlad-3b"), "madlad")
        self.assertEqual(es.translation_family("nllb-1.3b"), "nllb")
        self.assertIn("commercial", entry["fit"])
        d = self.folder(["<unk>", "<s>", "</s>", "<2de>", "<2en>", "▁Hello"])
        self.assertEqual(es.identify_translation_folder(d), "madlad-3b")

    def test_every_model_has_a_family(self):
        for m in es.TRANSLATION_MODELS:
            self.assertIn(m["family"], ("nllb", "madlad"), m["id"])


class FakeTokenizer:
    """Enough of tokenizers.Tokenizer for worker._encode: SentencePiece-style
    pieces, with the lone "▁" it puts before a leading <2xx> tag."""

    def encode(self, text, add_special_tokens=False):
        m = re.match(r"(<2[^>]+>) (.*)", text)
        pieces = ["▁", m.group(1)] + ["▁" + w for w in m.group(2).split()] if m else \
            ["▁" + w for w in text.split()]
        return SimpleNamespace(tokens=pieces)


class WorkerEncodeTests(unittest.TestCase):
    def test_madlad_input_is_tagged_as_sentencepiece_splits_it(self):
        toks = worker._encode(FakeTokenizer(), "madlad", "Hello there", "eng_Latn", "de")
        self.assertEqual(toks, ["▁", "<2de>", "▁Hello", "▁there", "</s>"])

    def test_nllb_input_starts_with_the_source_code(self):
        toks = worker._encode(FakeTokenizer(), "nllb", "Hello there", "eng_Latn", "")
        self.assertEqual(toks, ["eng_Latn", "▁Hello", "▁there", "</s>"])


class MadladTidyTests(unittest.TestCase):
    def test_real_madlad_quirks(self):
        # Actual MADLAD-400 3B output.
        self.assertEqual(st._tidy("タイムラインは23. 976フレーム/秒だが クリップは29. 97だ", "jpn_Jpan"),
                         "タイムラインは23.976フレーム/秒だが クリップは29.97だ")
        self.assertEqual(st._tidy("時間軸是每秒23.976帧 ， 但那段影片是29.97帧 。", "zho_Hant"),
                         "時間軸是每秒23.976帧，但那段影片是29.97帧")


class FakeClient:
    """Answers each request from `answer(lines) -> reply text`, recording them."""

    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def chat(self, system, messages, tools=None):
        req = json.loads(messages[-1]["content"])
        self.requests.append(req)
        return SimpleNamespace(content=self.answer({int(k): v for k, v in req["lines"].items()}))


def upper(lines):
    return json.dumps({str(k): v.upper() for k, v in lines.items()})


class AITranslateTests(unittest.TestCase):
    def test_parse_reply_tolerates_fences_and_chatter(self):
        text = 'Sure! Here you go:\n```json\n{"1": "Hallo", "2": "Welt", "9": "extra"}\n```'
        self.assertEqual(ai.parse_reply(text, {1, 2}), {1: "Hallo", 2: "Welt"})
        self.assertEqual(ai.parse_reply('{"lines": {"1": "Hallo"}}', {1}), {1: "Hallo"})
        self.assertEqual(ai.parse_reply("no json at all", {1}), {})
        self.assertEqual(ai.parse_reply('{"1": ""}', {1}), {})

    def test_every_sentence_comes_back_in_order_across_batches(self):
        sentences = [f"line {i}" for i in range(ai.BATCH + 5)]
        client = FakeClient(upper)
        seen = []
        out = ai.translate(client, sentences, "English", "German",
                           progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(out, [s.upper() for s in sentences])
        self.assertEqual(len(client.requests), 2)
        # The second batch carries the lines before it, as context only.
        self.assertEqual(client.requests[1]["context"], sentences[ai.BATCH - ai.CONTEXT:ai.BATCH])
        self.assertEqual(seen[-1], (len(sentences), len(sentences)))

    def test_missing_lines_are_asked_for_again(self):
        calls = []

        def forgetful(lines):
            calls.append(sorted(lines))
            if len(calls) == 1:
                lines = {k: v for k, v in lines.items() if k != 2}
            return upper(lines)

        out = ai.translate(FakeClient(forgetful), ["a", "b", "c"], "English", "German")
        self.assertEqual(out, ["A", "B", "C"])
        self.assertEqual(calls, [[1, 2, 3], [2]])

    def test_still_missing_is_an_error_not_a_shifted_file(self):
        client = FakeClient(lambda lines: upper({k: v for k, v in lines.items() if k != 1}))
        with self.assertRaises(ai.TranslateError):
            ai.translate(client, ["a", "b"], "English", "German")

    def test_cancel_stops_between_requests(self):
        with self.assertRaises(ai.TranslateCancelled):
            ai.translate(FakeClient(upper), ["a"], "English", "German", cancelled=lambda: True)

    def test_busy_provider_is_waited_out_but_a_bad_key_is_not(self):
        # A real message Gemini returns.
        busy = RuntimeError("Gemini: HTTP 503 - This model is currently experiencing high demand.")
        replies = [busy, busy]

        class Flaky(FakeClient):
            def chat(self, system, messages, tools=None):
                if replies:
                    raise replies.pop(0)
                return super().chat(system, messages, tools)

        slept = []
        out = ai.translate(Flaky(upper), ["a"], "English", "German", sleep=slept.append)
        self.assertEqual(out, ["A"])
        self.assertEqual(len(slept), sum(ai.RETRY_WAITS))

        class BadKey(FakeClient):
            def chat(self, system, messages, tools=None):
                raise RuntimeError("Gemini: HTTP 400 - API key not valid.")

        slept.clear()
        with self.assertRaises(RuntimeError):
            ai.translate(BadKey(upper), ["a"], "English", "German", sleep=slept.append)
        self.assertEqual(slept, [])

    def test_glossary_reaches_the_prompt(self):
        prompt = ai.system_prompt("English", "German", "DaVinci Resolve, Buddy")
        self.assertIn("DaVinci Resolve, Buddy", prompt)
        self.assertNotIn("spell them", ai.system_prompt("English", "German", ""))


if __name__ == "__main__":
    unittest.main()
