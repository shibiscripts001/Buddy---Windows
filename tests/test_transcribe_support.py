"""The pieces around transcription that need no model or Resolve:
language tables, model-folder identification, and the worker's text
clean-up (app/pages/transcribe/languages.py, env_setup.py, worker.py)."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _paths  # noqa: F401
import worker
from pages.transcribe import env_setup as es
from pages.transcribe import languages as L


class LanguageTableTests(unittest.TestCase):
    def test_every_nllb_language_is_listed_once(self):
        # 202 = every language token in NLLB-200's vocabulary (checked against
        # shared_vocabulary.json when the table was written).
        self.assertEqual(len(L.NLLB_LANGUAGES), 202)
        for code in L.NLLB_LANGUAGES:
            self.assertRegex(code, r"^[a-z]{3}_[A-Z][a-z]{3}$")
        names = [n for n, _ in L.sorted_targets()]
        self.assertEqual(len(names), len(set(names)))

    def test_whisper_languages_map_to_real_nllb_codes(self):
        for code in L.WHISPER_TO_NLLB.values():
            self.assertIn(code, L.NLLB_LANGUAGES)
        self.assertLessEqual(L.NO_SPACE_CODES, set(L.NLLB_LANGUAGES))

    def test_parakeet_languages_are_whisper_codes(self):
        self.assertEqual(len(es.PARAKEET_LANGS), 25)
        self.assertLessEqual(es.PARAKEET_LANGS, set(L.WHISPER_TO_NLLB))


class ModelFolderTests(unittest.TestCase):
    """identify_model_folder / identify_translation_folder on fake folders
    shaped like the real published exports."""

    def folder(self, files: dict):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in d.iterdir()] and d.rmdir())
        for name, content in files.items():
            (d / name).write_text(content if isinstance(content, str) else json.dumps(content),
                                  encoding="utf-8")
        return d

    def whisper(self, heads, mels=128):
        files = {"model.bin": "x", "config.json": {"alignment_heads": heads}}
        if mels != 80:
            files["preprocessor_config.json"] = {"feature_size": mels}
        return self.folder(files)

    def test_whisper_sizes_by_alignment_layers(self):
        # Layer indices as in the published configs.
        self.assertEqual(es.identify_model_folder(self.whisper([[1, 0], [1, 3]])), "distil-large-v3.5")
        self.assertEqual(es.identify_model_folder(self.whisper([[2, 4], [3, 0]])), "large-v3-turbo")
        self.assertEqual(es.identify_model_folder(self.whisper([[7, 0], [25, 6]])), "large-v3")
        self.assertEqual(es.identify_model_folder(self.whisper([[3, 1]], mels=80)), "small")

    def test_parakeet_folder(self):
        files = {f: "x" for f in es.MODEL_BY_ID["parakeet-v3"]["files"]}
        files["config.json"] = {"model_type": "nemo-conformer-tdt"}
        self.assertEqual(es.identify_model_folder(self.folder(files)), "parakeet-v3")

    def test_not_a_model(self):
        self.assertIsNone(es.identify_model_folder(self.folder({"readme.txt": "hi"})))

    def test_translation_folder(self):
        vocab = json.dumps(["<s>", "eng_Latn", "zho_Hans"])
        d = self.folder({"model.bin": "x", "tokenizer.json": "{}", "shared_vocabulary.json": vocab})
        self.assertEqual(es.identify_translation_folder(d), "nllb-600m")
        d2 = self.folder({"model.bin": "x", "tokenizer.json": "{}", "shared_vocabulary.json": '["a"]'})
        self.assertIsNone(es.identify_translation_folder(d2))


class WorkerTextTests(unittest.TestCase):
    def test_collapse_repeated_sentence(self):
        s = "ルーツとブートロードやロックを解除することもできました"
        self.assertEqual(worker._collapse_repeat(f"{s} {s}"), s)
        self.assertEqual(worker._collapse_repeat("I know I know it."), "I know I know it.")

    def test_speech_no_word_covers_is_found(self):
        # 今日の撮影は (0-0.9) [really went well - left out] と思います (1.86-2.28)
        words = [(0.0, 0.14), (0.14, 0.9), (1.86, 2.28)]
        self.assertEqual(worker.uncovered(words, 0.0, 2.3), [(0.9, 1.86)])
        self.assertEqual(worker.uncovered([], 1.0, 3.0), [(1.0, 3.0)])            # nothing came out
        self.assertEqual(worker.uncovered([(1.0, 1.5), (1.2, 2.9)], 1.0, 3.0), [])  # overlapping words
        self.assertEqual(worker.uncovered([(0.0, 1.0)], 0.0, 1.3), [])             # under GAP_MIN

    def test_fillers_dropped_in_english_only(self):
        words = lambda *t: [{"word": x, "start": 0, "end": 0} for x in t]
        text = lambda ws: [w["word"] for w in ws]
        self.assertEqual(text(worker._drop_fillers(words(" like", " the", " uh", " the"), "en")),
                         [" like", " the", " the"])
        self.assertEqual(text(worker._drop_fillers(words(" fine", " um."), "en")), [" fine."])
        self.assertEqual(text(worker._drop_fillers(words(" so", " uh"), "de")), [" so", " uh"])
        self.assertEqual(text(worker._drop_fillers(words(" er", " ist"), "en")), [" er", " ist"])

    def test_parakeet_tokens_become_timed_words(self):
        result = SimpleNamespace(
            tokens=[" So", " it", "'", "s", " ", "1", "2", " a", ".m", "."],
            timestamps=[0.0, 0.16, 0.24, 0.32, 0.8, 0.88, 0.96, 3.0, 3.08, 3.16])
        ws = worker._parakeet_words(result, offset=10.0, limit=14.0)
        self.assertEqual([w["word"] for w in ws], [" So", " it's", " 12", " a.m."])
        for a, b in zip(ws, ws[1:]):
            self.assertLessEqual(a["end"], b["start"])     # never runs into the next word
        self.assertLessEqual(ws[2]["end"], 11.2)          # a pause stays a pause
        self.assertGreaterEqual(ws[0]["start"], 10.0)


if __name__ == "__main__":
    unittest.main()
