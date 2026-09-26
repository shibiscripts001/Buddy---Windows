"""A timeline where several languages are spoken: deciding each
utterance's language (worker.py) and cutting subtitles and sentences per
language (subtitles.py). No model - detection scores are given."""

import unittest

import _paths  # noqa: F401
import worker
from pages.transcribe import subtitles as st


def seg(start, end, text, lang, words=None):
    item = {"start": start, "end": end, "text": text, "language": lang}
    if words is not None:
        item["words"] = words
    return item


class DecideTests(unittest.TestCase):
    def test_only_the_chosen_languages_and_unsure_ones_follow_their_neighbours(self):
        scores = [
            {"en": 0.98, "ja": 0.01},
            {"ja": 0.97, "en": 0.02},
            {"de": 0.60, "en": 0.20, "ja": 0.19},   # unsure between en and ja: takes ja before it
            {"ko": 0.90, "ja": 0.09, "en": 0.01},   # Korean isn't spoken here: Japanese
            {},                                       # nothing at all: the one before
            {"en": 0.95},
        ]
        self.assertEqual(worker.decide_languages(scores, ["ja", "en"]), ["en", "ja", "ja", "ja", "ja", "en"])
        # An unsure first one takes the next sure one.
        self.assertEqual(worker.decide_languages([{"en": 0.5, "ja": 0.4}, {"ja": 0.9}], ["ja", "en"]),
                         ["ja", "ja"])

    def test_runs_of_one_language(self):
        spans = [{"start": 0, "end": 10}, {"start": 12, "end": 20}, {"start": 25, "end": 30},
                 {"start": 31, "end": 40}]
        self.assertEqual(worker.language_runs(spans, ["en", "en", "ja", "en"]),
                         [("en", 0, 20), ("ja", 25, 30), ("en", 31, 40)])
        self.assertEqual(worker.language_runs([], []), [])


class MixedCueTests(unittest.TestCase):
    def style_for(self, lang):
        return st.Style(max_chars=42, cjk=lang == "ja", wide=lang == "ja")

    def test_each_language_is_cut_its_own_way_and_never_joined(self):
        segments = [
            seg(0.5, 2.0, "Welcome back.", "en",
                [{"start": 0.5, "end": 1.2, "word": " Welcome"}, {"start": 1.2, "end": 2.0, "word": " back."}]),
            seg(2.1, 3.0, "はい。", "ja", [{"start": 2.1, "end": 3.0, "word": "はい。"}]),
            seg(3.2, 4.0, "Okay.", "en", [{"start": 3.2, "end": 4.0, "word": " Okay."}]),
        ]
        cues = st.build_cues_mixed(segments, self.style_for)
        self.assertEqual([c.text for c in cues], ["Welcome back.", "はい。", "Okay."])
        for a, b in zip(cues, cues[1:]):
            self.assertLessEqual(a.end, b.start)   # a minimum-length stretch never runs into the next language
        self.assertEqual([lang for lang, _run in st.language_runs(segments)], ["en", "ja", "en"])

    def test_sentences_carry_their_language(self):
        segments = [seg(0.0, 2.0, "How long have you farmed here?", "en"),
                    seg(2.5, 5.0, "二十年くらいです。父の代からです。", "ja"),
                    seg(5.5, 6.0, "Wow.", "en")]
        sentences = st.sentences_mixed(segments, lambda lang: lang == "ja")
        self.assertEqual([(s.language, s.text) for s in sentences],
                         [("en", "How long have you farmed here?"), ("ja", "二十年くらいです。"),
                          ("ja", "父の代からです。"), ("en", "Wow.")])


if __name__ == "__main__":
    unittest.main()
