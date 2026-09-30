"""Subtitle building: wrapping, cues from word timings, translation
re-timing and SRT parsing (app/pages/transcribe/subtitles.py).

The randomized tests are seeded, so a failure reproduces exactly. They are
smaller versions of larger randomized runs (~100,000
transcript cues and ~210,000 translated cues, all clean)."""

import random
import re
import unittest

import _paths  # noqa: F401
from pages.transcribe import subtitles as st

LATIN = ["the", "a", "phone", "Xiaomi", "14", "Ultra", "really", "good", "battery,", "life.",
         "because", "and", "desbloquear", "Geschwindigkeitsbegrenzung", "I", "it's", "okay?",
         "well-known", "1,000"]
JAPANESE = ["もう", "少し", "時間が", "経ちました", "ロック", "を", "解除", "する", "こと", "が",
            "でき", "ました", " ", "、", "Xiaomi 14 Ultra", "っ", "ー", "「", "」"]


def random_words(rnd, n):
    t, words = rnd.uniform(0, 5), []
    for _ in range(n):
        t += rnd.choice([0.0, 0.05, 0.1, 0.3, 0.7, 2.0])
        d = rnd.uniform(0.05, 0.8)
        w = rnd.choice(LATIN)
        if rnd.random() < 0.15:
            w += rnd.choice([".", "?", ","])
        words.append({"start": t, "end": t + d, "word": " " + w})
        t += d
    return words


def fits(cue, style):
    """At most max_lines lines of line_chars - unless a single unbreakable
    unit is itself wider than a line."""
    lines = cue.text.split("\n")
    units, _ = st._units(cue.text.replace("\n", ""), style.cjk)
    widest = max((len(u) for u in units), default=0)
    if len(lines) <= style.max_lines and all(len(l) <= style.line_chars for l in lines):
        return True
    return widest > style.line_chars


class WrapTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(st.wrap("Hello there.", 42, 2), "Hello there.")

    def test_two_balanced_lines(self):
        out = st.wrap("so it's been just a little while since my last upload but in the", 42, 2)
        a, b = out.split("\n")
        self.assertLessEqual(max(len(a), len(b)), 42)
        self.assertLess(abs(len(a) - len(b)), 12)

    def test_prefers_breaking_after_punctuation(self):
        # Within reach of balance, a comma wins over the exact middle.
        out = st.wrap("We sold the phone, and we bought a new one", 30, 2)
        self.assertEqual(out, "We sold the phone,\nand we bought a new one")

    def test_cjk_keeps_latin_names_whole_when_it_can(self):
        out = st.wrap("ルーツとブートロードをしたり Xiaomi 14 Ultraの", 17, 2, cjk=True)
        self.assertIn("Xiaomi 14 Ultra", out.replace("\n", "|").split("|")[-1] + out)
        self.assertNotIn("Ultr\n", out)

    def test_cjk_never_starts_a_line_with_small_kana_or_closing_mark(self):
        for text in ("持っているので販売しました、はい", "まだ良いデバイスだと思っています。そう"):
            out = st.wrap(text, 8, 2, cjk=True)
            for line in out.split("\n")[1:]:
                self.assertNotIn(line[:1], st._NO_LINE_START, out)


class CueTests(unittest.TestCase):
    def test_random_transcripts_keep_every_invariant(self):
        rnd = random.Random(11)
        for _ in range(400):
            style = st.Style(max_chars=rnd.choice([1, 2, 5, 12, 24, 32, 42, 60]), max_lines=rnd.choice([1, 2]))
            words = random_words(rnd, rnd.randint(1, 80))
            cues = st.build_cues([{"start": 0, "end": 0, "text": "", "words": words}], style)
            said = "".join(w["word"].split()[0] for w in words)
            self.assertEqual(said, "".join(c.text.replace("\n", "").replace(" ", "") for c in cues))
            for a, b in zip(cues, cues[1:]):
                self.assertLessEqual(a.end, b.start + 1e-9)
            for c in cues:
                self.assertGreater(c.end, c.start)
                self.assertTrue(fits(c, style), c.text)

    def test_japanese_words_join_without_spaces(self):
        ws = ["こんにちは", "、", "今日", "は", "Xiaomi", " 14", " Ultra", "の", "話", "を", "します", "。"]
        segs = [{"start": 0, "end": 9, "text": "",
                 "words": [{"start": i * .5, "end": i * .5 + .4, "word": w} for i, w in enumerate(ws)]}]
        cues = st.build_cues(segs, st.Style(cjk=True))
        # A line break may stand in for the space in "Xiaomi 14": compare
        # the text without either, then check no line glued the name up.
        self.assertEqual(re.sub(r"\s", "", "".join(c.text for c in cues)),
                         "こんにちは、今日はXiaomi14Ultraの話をします。")
        for c in cues:
            for line in c.text.split("\n"):
                self.assertNotIn("Xiaomi14", line)
                self.assertNotIn(" の", line)

    def test_thai_joins_without_spaces_at_the_normal_line_length(self):
        # Whisper's Thai word pieces, as it spaces them. Thai has no spaces
        # between words, but isn't full-width: one 27-character line here,
        # not two of CJK's 17.
        ws = ["สวัสดี", "ครับ", " วันนี้", "อากาศ", "ดี", "มาก"]
        segs = [{"start": 0, "end": 4, "text": "",
                 "words": [{"start": i * .5, "end": i * .5 + .4, "word": w} for i, w in enumerate(ws)]}]
        cues = st.build_cues(segs, st.Style(cjk=True, wide=False))
        self.assertEqual([c.text for c in cues], ["สวัสดีครับ วันนี้อากาศดีมาก"])

    def test_thai_khmer_burmese_never_break_inside_a_character_cluster(self):
        import unicodedata
        leading = set("เแโใไ")
        for text in ("เพื่อนของฉันไม่ได้ไปโรงเรียนเมื่อวานนี้เพราะเขาป่วย",
                     "ភាសាខ្មែរគឺជាភាសាផ្លូវការរបស់ប្រទេសកម្ពុជា",
                     "မြန်မာဘာသာစကားသည်မြန်မာနိုင်ငံ၏ရုံးသုံးဘာသာစကားဖြစ်သည်"):
            out = st.wrap(text, 12, 4, cjk=True)
            self.assertEqual(out.replace("\n", ""), text)
            for line in out.split("\n"):
                self.assertFalse(unicodedata.category(line[0]).startswith("M"), out)
                self.assertNotIn(line[-1], leading, out)
                self.assertNotIn(line[-1], "\u1039\u17d2", out)  # virama/coeng

    def test_one_character_a_line_gives_a_word_a_subtitle(self):
        """For animating word by word: no word is split, none are put together,
        and each shows until the next starts."""
        said = ["So", "it's", "been", "a", "while.", "Welcome", "back,", "everyone"]
        words = [{"start": i * .3, "end": i * .3 + .25, "word": " " + w} for i, w in enumerate(said)]
        cues = st.build_cues([{"start": 0, "end": 0, "text": "", "words": words}], st.Style(max_chars=1))
        self.assertEqual([c.text for c in cues], said)
        for a, b in zip(cues, cues[1:]):
            self.assertAlmostEqual(a.end, b.start)                          # back to back: no blank frames
        # Whisper's words without a time of their own (one start, zero long), and
        # one-letter words that two lines of one would have paired up.
        timed = [(0.0, 0.3, "I"), (0.4, 0.4, "a"), (0.4, 0.4, "am"), (0.4, 0.4, "here"), (1.0, 1.2, "a"), (1.3, 1.5, "I")]
        words = [{"start": s0, "end": e0, "word": " " + w} for s0, e0, w in timed]
        cues = st.build_cues([{"start": 0, "end": 0, "text": "", "words": words}], st.Style(max_chars=1, max_lines=2))
        self.assertEqual([c.text for c in cues], ["I", "a", "am", "here", "a", "I"])
        for a, b in zip(cues, cues[1:]):
            self.assertGreater(a.end, a.start)
            self.assertLessEqual(a.end, b.start + 1e-9)
        self.assertAlmostEqual(cues[3].end, 1.0)                              # the untimed three share 0.4-1.0
        fast = [{"start": 0.01 * i, "end": 0.01 * i + 0.01, "word": f" w{i}"} for i in range(5)] +                [{"start": 2.0, "end": 2.3, "word": " later"}]
        cues = st.build_cues([{"start": 0, "end": 0, "text": "", "words": fast}], st.Style(max_chars=1))
        self.assertTrue(all(c.end - c.start >= st.WORD_MIN - 1e-9 for c in cues))   # a frame each, at least
        self.assertEqual(cues[-1].start, 2.0)                                           # caught up by the pause
        ja = ["今日", "は", "撮影", "です", "。"]
        segs = [{"start": 0, "end": 3, "text": "",
                 "words": [{"start": i * .5, "end": i * .5 + .4, "word": w} for i, w in enumerate(ja)]}]
        cues = st.build_cues(segs, st.Style(max_chars=1, cjk=True))
        self.assertEqual([c.text for c in cues], ["今日", "は", "撮影", "です", "。"])

    def test_words_are_put_in_time_order(self):
        """A segment the gap fill added lands after words it came before: its
        cue ran backwards over the next (seen on a real 104-minute transcript)."""
        segs = [{"start": 20, "end": 21, "text": "", "words": [{"start": 20, "end": 20.5, "word": " channel."}]},
                {"start": 4, "end": 5, "text": "", "words": [{"start": 4.6, "end": 5.0, "word": " Hmm"}]}]
        for style in (st.Style(), st.Style(max_chars=1)):
            cues = st.build_cues(segs, style)
            self.assertEqual([c.text for c in cues], ["Hmm", "channel."])
            self.assertLessEqual(cues[0].end, cues[1].start)

    def test_segments_without_word_timings_still_work(self):
        segs = [{"start": 0.0, "end": 10.0, "text": " ".join(["word"] * 40)}]
        cues = st.build_cues(segs, st.Style())
        self.assertGreater(len(cues), 1)
        self.assertEqual(sum(c.text.split().count("word") for c in cues), 40)


class SentenceTests(unittest.TestCase):
    def test_sentences_end_at_full_stops_and_long_pauses(self):
        words = [{"start": 0, "end": .3, "text": "Hello", "sp": True},
                 {"start": .4, "end": .7, "text": "there.", "sp": True},
                 {"start": .8, "end": 1.0, "text": "And", "sp": True},
                 {"start": 4.0, "end": 4.3, "text": "later", "sp": True}]
        self.assertEqual([s.text for s in st.sentences_from_words(words)],
                         ["Hello there.", "And", "later"])

    def test_run_on_speech_is_capped(self):
        words = [{"start": i * .3, "end": i * .3 + .25, "text": "word", "sp": True} for i in range(200)]
        self.assertTrue(all(len(s.text) <= st.SENTENCE_MAX_CHARS
                            for s in st.sentences_from_words(words)))


class TranslationTests(unittest.TestCase):
    def test_random_translations_keep_every_invariant(self):
        rnd = random.Random(7)
        for _ in range(600):
            cjk = rnd.random() < 0.4
            style = st.Style(max_chars=rnd.choice([1, 2, 5, 12, 24, 32, 42, 60]), max_lines=rnd.choice([1, 2]), cjk=cjk)
            words = [{"start": w["start"], "end": w["end"], "text": w["word"].strip(), "sp": True}
                     for w in random_words(rnd, rnd.randint(1, 60))]
            sents = st.sentences_from_words(words)
            pool = JAPANESE if cjk else LATIN
            joiner = "" if cjk else " "
            trans = ["" if rnd.random() < 0.05 else
                     joiner.join(rnd.choice(pool) for _ in range(rnd.randint(1, 45))).strip()
                     for _ in sents]
            lang = "jpn_Jpan" if cjk else "spa_Latn"
            cues = st.translated_cues(sents, trans, style, lang)
            # Nothing lost, nothing repeated, order kept.
            want = "".join(re.sub(r"\s+", "", st._tidy(x, lang)) for x in trans)
            self.assertEqual(want, "".join(re.sub(r"\s+", "", c.text) for c in cues))
            for a, b in zip(cues, cues[1:]):
                self.assertLessEqual(a.end, b.start + 1e-9)
            if sents:
                for c in cues:
                    self.assertGreaterEqual(c.start, sents[0].start - 1e-9)
                    self.assertTrue(c.text.strip())
                    self.assertTrue(fits(c, style), c.text)

    def test_cjk_punctuation(self):
        self.assertEqual(st._tidy("我有1,000个, 好吧,很好.", "zho_Hans"), "我有1,000个，好吧，很好")
        self.assertEqual(st._tidy("はい, 1,000円です.", "jpn_Jpan"), "はい、1,000円です")
        self.assertEqual(st._tidy("Hola,  mundo.", "spa_Latn"), "Hola, mundo.")


class SrtTests(unittest.TestCase):
    def test_round_trip(self):
        cues = [st.Cue(i * 2.5, i * 2.5 + 2, f"line {i}\nsecond, {i}") for i in range(50)]
        back = st.parse_srt("﻿" + st.to_srt(cues).replace("\n", "\r\n"))
        self.assertEqual(len(back), 50)
        for b, c in zip(back, cues):
            self.assertAlmostEqual(b["start"], c.start, places=6)
            self.assertAlmostEqual(b["end"], c.end, places=6)
            self.assertEqual(b["text"], c.text.replace("\n", " "))

    def test_tolerates_tags_short_millis_and_missing_numbers(self):
        got = st.parse_srt("1\n00:00:01,5 --> 00:00:02,000\n<i>Hi</i> {\\an8}there\n\n\n"
                           "00:00:03,000 --> 00:00:04,000\nno number\n")
        self.assertEqual(got, [{"start": 1.5, "end": 2.0, "text": "Hi there"},
                               {"start": 3.0, "end": 4.0, "text": "no number"}])


if __name__ == "__main__":
    unittest.main()
