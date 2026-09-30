#!/usr/bin/env python3
"""
Whisper segments -> subtitle cues -> SRT. Pure Python, no Qt, no Resolve.

Whisper's own segments are sized for decoding, not reading: one can run
for 15 seconds or stop mid-clause. So cues are rebuilt from the WORD
timings instead, against the usual subtitle readability limits (broadcast
and streaming style guides agree on roughly these):

  max_chars   characters per line (42; 1-60 - a word is never split). 1 is
              "a word a cue" (Style.one_word), for animating word by word:
              every word its own cue, back to back with no gap
  max_lines   lines per cue (2)
  min / max   seconds on screen (1.0 / 7.0)
  gap         a pause this long between words always starts a new cue

A cue also ends at a sentence end (. ? !) once it has some length, so a
subtitle rarely straddles two sentences. Text is wrapped into balanced
lines - the split nearest the middle that fits - rather than filled
greedily, which leaves one long and one short line.

When a segment has no word timings (they're optional in the worker), its
text is split into cues with time shared out by character count - coarser,
but never wrong about which words were said.

Translation (translated_cues) works on whole SENTENCES, never on cues: a
cue is often half a clause, and translating halves garbles grammar. The
words are regrouped into sentences, each is translated as one, and the
result is split back into cues across that sentence's own time span -
each break placed where the source had spoken the same share of its
text, snapped to a real gap between two source words.

Chinese, Japanese and Cantonese are written without spaces, in
full-width characters: with Style.cjk set, words are joined as Whisper
spaced them (not with a space each) and lines are wrapped per character,
at CJK_WIDTH of the Latin line length. Thai, Lao, Burmese and Khmer are
also written without spaces, so they set cjk too, but their characters
aren't full-width: they set wide=False and keep the Latin line length.

A mixed-language transcript (the worker's --languages) marks each segment
with its language. build_cues_mixed and sentences_mixed take each run of
one language on its own - with its own Style, so Japanese is cut per
character and English at spaces - and never join two languages into one
cue or sentence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

SENTENCE_END = (".", "?", "!", "…", "。", "？", "！")
# 42 Latin characters per line ~ 17 full-width ones (style guides give
# 13-16 for Japanese, 16 for Chinese).
CJK_WIDTH = 0.4


@dataclass
class Cue:
    start: float
    end: float
    text: str   # already wrapped, lines joined with "\n"


@dataclass
class Style:
    max_chars: int = 42
    max_lines: int = 2
    min_duration: float = 1.0
    max_duration: float = 7.0
    pause_gap: float = 0.6
    min_gap: float = 0.08     # between consecutive cues, ~2 frames
    cjk: bool = False         # no-space script: see the module docstring
    wide: bool = True         # with cjk: full-width characters (not Thai etc.)
    speaker_names: bool = False   # "Speaker 1: ..." where a named speaker starts (build_cues)

    @property
    def one_word(self) -> bool:
        """A word a cue, each shown until the next starts: max_chars 1."""
        return self.max_chars <= 1

    @property
    def narrow_lines(self) -> bool:
        return self.cjk and self.wide

    @property
    def line_chars(self) -> int:
        return max(1, round(self.max_chars * CJK_WIDTH)) if self.narrow_lines else self.max_chars


# -------------------------------------------------------------- wrapping


# CJK text breaks between any two characters, except inside a run of
# Latin letters/digits ("14 Ultra" in Japanese text) or of katakana (a
# loanword), which stay whole. Spaces are kept as units: NLLB separates
# Japanese phrases with them, and they're the best places to break.
# In Thai, Lao, Burmese and Khmer a character can carry vowel and tone
# marks, and a Burmese/Khmer consonant can stack on the next one (virama/
# coeng): each such cluster is one unit, with a Thai/Lao leading vowel
# kept on the consonant after it - a line starting with a bare mark would
# draw it on a dotted circle.
_MARKS = (r"\u0e30-\u0e3a\u0e46-\u0e4e"                            # Thai
          r"\u0eb0-\u0ebc\u0ec6-\u0ece"                            # Lao
          r"\u102b-\u103e\u1056-\u1059\u105e-\u1060\u1062-\u1064"  # Burmese
          r"\u1067-\u106d\u1071-\u1074\u1082-\u108d\u108f\u109a-\u109d"
          r"\u17b4-\u17d3\u17dd"                                   # Khmer
          r"\u0300-\u036f\u3099\u309a")                            # combining
_LEADING_VOWELS = r"\u0e40-\u0e44\u0ec0-\u0ec4"                    # Thai, Lao
_STACKERS = r"\u1039\u17d2"                                        # Burmese virama, Khmer coeng
_CJK_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’.\-]*|[ァ-ヺー・]+|\s+"
                       rf"|[{_LEADING_VOWELS}]?.(?:[{_STACKERS}].|[{_MARKS}])*", re.S)


def _units(text: str, cjk: bool) -> tuple[list[str], str]:
    """What a line may break between, and how the pieces rejoin."""
    return (_CJK_UNIT.findall(text), "") if cjk else (text.split(), " ")


def _is_latin(unit: str) -> bool:
    return unit[:1].isascii() and unit[:1].isalnum()


def _good_break(units: list[str], i: int) -> bool:
    """A break before units[i] that falls at punctuation, or at a space
    between phrases - not one inside a Latin name ("Xiaomi 14 Ultra")."""
    prev = units[i - 1]
    if prev.endswith(CLAUSE_END + SENTENCE_END):
        return True
    for k in (i - 1, i):
        if units[k].isspace():
            left, right = units[k - 1] if k else "", units[k + 1] if k + 1 < len(units) else ""
            return not (_is_latin(left) and _is_latin(right))
    return False


# Japanese/Chinese line-breaking rules (kinsoku): a line never starts with
# closing punctuation, a small kana or the long-vowel mark, and never ends
# with an opening bracket.
_NO_LINE_START = set("、。，．,.!?！？・ー」』）)]】〉》ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々")
_NO_LINE_END = set("「『（([【〈《")


def _forbidden_break(units: list[str], i: int) -> bool:
    return units[i][:1] in _NO_LINE_START or units[i - 1][-1:] in _NO_LINE_END


def _inside_latin(units: list[str], i: int) -> bool:
    """A break before units[i] that would split a Latin name in CJK text."""
    for k in (i - 1, i):
        if units[k].isspace():
            return 0 < k < len(units) - 1 and _is_latin(units[k - 1]) and _is_latin(units[k + 1])
    return _is_latin(units[i - 1]) and _is_latin(units[i])


def wrap(text: str, max_chars: int, max_lines: int, cjk: bool = False) -> str:
    """Balanced line breaks. Falls back to greedy filling past two lines."""
    words, joiner = _units(text, cjk)
    if len(text) <= max_chars or not words:
        return text
    if max_lines >= 2:
        best, best_score = None, None
        for i in range(1, len(words)):
            if cjk and _forbidden_break(words, i):
                continue
            a, b = joiner.join(words[:i]).strip(), joiner.join(words[i:]).strip()
            if not a or not b or len(a) > max_chars or len(b) > max_chars:
                continue
            # Prefer balanced lines, and breaking after punctuation. In CJK
            # text a phrase break beats any mid-phrase one that fits (there
            # is no word boundary to fall back on), and a Latin name stays
            # on one line where it can.
            if cjk:
                score = abs(len(a) - len(b)) - (max_chars if _good_break(words, i) else 0)                     + (max_chars if _inside_latin(words, i) else 0)
            else:
                score = abs(len(a) - len(b)) - (6 if a[-1] in ",;:" + "".join(SENTENCE_END) else 0)
            if best_score is None or score < best_score:
                best, best_score = a + "\n" + b, score
        if best:
            return best
    lines, cur = [], ""
    for i, w in enumerate(words):
        # CJK: a mark that may not start a line stays on this one, even a
        # character over (the usual kinsoku "hanging" rule).
        hang = cjk and i and _forbidden_break(words, i)
        if cur and not hang and len(cur) + len(joiner) + len(w) > max_chars:
            lines.append(cur.strip())
            cur = w
        else:
            cur = f"{cur}{joiner}{w}" if cur else w
    lines.append(cur.strip())
    return "\n".join(l for l in lines if l)


# ----------------------------------------------------------------- cues


def _flatten_words(segments: list[dict]) -> list[dict] | None:
    words = []
    for seg in segments:
        seg_words = seg.get("words")
        if not seg_words:
            return None
        for w in seg_words:
            raw = w.get("word") or ""
            text = raw.strip()
            if text:
                # sp: Whisper spelled a space before this word. Only CJK
                # text uses it - elsewhere every word gets one anyway.
                words.append({"start": float(w["start"]), "end": float(w["end"]), "text": text,
                              "sp": raw[:1].isspace(), "speaker": seg.get("speaker")})
    # In time order: a segment filled in later (the worker's gap fill) can come
    # after words it went before - one cue then ran backwards over the next.
    words.sort(key=lambda w: w["start"])
    return words


def _join(words: list[dict], cjk: bool = False) -> str:
    if not cjk:
        return " ".join(w["text"] for w in words)
    return "".join((" " if i and w.get("sp") else "") + w["text"] for i, w in enumerate(words))


def _fits(text: str, style: Style) -> bool:
    """True when text wraps into at most max_lines lines of max_chars. A
    character budget alone isn't enough: 84 characters can still need three
    lines when the words don't split evenly (found on a real transcript)."""
    width = style.line_chars
    lines = wrap(text, width, style.max_lines, style.cjk).split("\n")
    return len(lines) <= style.max_lines and all(len(l) <= width for l in lines)


CLAUSE_END = (",", ";", ":", "—", "–", "、", "，", "；", "：")


def _clause_break(cur: list[dict], style: Style):
    """Index of the last word ending a clause, if it sits in the second
    half of the cue's text (earlier would leave a stubby cue), else None."""
    best = None
    total = len(_join(cur, style.cjk))
    for i, x in enumerate(cur[:-1]):
        if x["text"].endswith(CLAUSE_END) and len(_join(cur[:i + 1], style.cjk)) >= total * 0.5:
            best = i
    return best


def _cues_from_words(words: list[dict], style: Style) -> list[tuple[float, float, str]]:
    raw, cur = [], []

    def flush():
        if cur:
            raw.append((cur[0]["start"], cur[-1]["end"], _join(cur, style.cjk), cur[0].get("speaker"), len(cur)))
            cur.clear()

    for i, w in enumerate(words):
        if cur:
            # Another person speaking always starts a new cue (words carry a
            # speaker only from Resolve's own transcription).
            turn = w.get("speaker") != cur[-1].get("speaker")
            too_long = not _fits(_join(cur + [w], style.cjk), style)
            too_slow = w["end"] - cur[0]["start"] > style.max_duration
            paused = w["start"] - cur[-1]["end"] >= style.pause_gap
            sentence_done = (cur[-1]["text"].endswith(SENTENCE_END)
                             and len(_join(cur, style.cjk)) >= style.line_chars // 2)
            if turn:
                flush()
                cur.append(w)
                continue
            if too_long and not (too_slow or paused or sentence_done):
                # Full mid-sentence: break at the last clause boundary in
                # the cue's second half rather than wherever space ran out
                # ("...since my last / upload, but" reads worse than
                # "...since my last upload, / but"). The words after the
                # comma start the next cue.
                k = _clause_break(cur, style)
                if k is not None:
                    carry = cur[k + 1:]
                    del cur[k + 1:]
                    flush()
                    cur.extend(carry)
                    too_long = not _fits(_join(cur + [w], style.cjk), style)
                    if not too_long:
                        cur.append(w)
                        continue
            if too_long or too_slow or paused or sentence_done:
                flush()
        cur.append(w)
    flush()
    return _merge_orphans(raw, style)


# Style.one_word: no word shows for less than a frame at 23.976 fps (41.7 ms) -
# Resolve can't hold a subtitle shorter, and 36 of 5,558 words were (fast speech).
WORD_MIN = 0.042


def _word_cues(words: list[dict]) -> list[tuple]:
    """Style.one_word: a cue per word. Words Whisper gave no time of their own
    - one start for a run of them, zero long (26 in a 104-minute transcript) -
    share the time up to the next word that has one, by length, so each still
    shows; and a word that follows the last by less than WORD_MIN waits for it
    (the run catches up at the next pause). (start, end, text, one word?) like
    _merge_orphans."""
    out, i = [], 0
    while i < len(words):
        j = i + 1
        while j < len(words) and words[j]["start"] <= words[i]["start"] + 1e-6:
            j += 1
        group = words[i:j]
        if len(group) == 1:
            w = group[0]
            out.append((w["start"], w["end"], w["text"], True))
        else:
            start = group[0]["start"]
            end = words[j]["start"] if j < len(words) else max(max(w["end"] for w in group), start + 0.2 * len(group))
            total = sum(len(w["text"]) for w in group) or 1
            t = start
            for w in group:
                span = (end - start) * len(w["text"]) / total
                out.append((t, t + span, w["text"], True))
                t += span
        i = j
    for k in range(1, len(out)):
        start, end, text, one = out[k]
        earliest = out[k - 1][0] + WORD_MIN
        if start < earliest:
            out[k] = (earliest, max(end, earliest + WORD_MIN), text, one)
    return out


# Shorter than this, a cue is an orphan - a word or two cut off by a length
# break, flashing up on its own for a second.
ORPHAN_CHARS = 14


def _merge_orphans(raw, style: Style):
    """Fold a tiny cue back into the one before it when they're contiguous
    (no real pause between), the same person said both, and the result still
    fits on screen. raw: (start, end, text, speaker, words); out: (start,
    end, text, one word?) - the speaker goes."""
    orphan = round(ORPHAN_CHARS * CJK_WIDTH) if style.narrow_lines else ORPHAN_CHARS
    out = []
    for start, end, text, speaker, count in raw:
        if out and len(text) < orphan and out[-1][3] == speaker:
            p_start, p_end, p_text, _, p_count = out[-1]
            joined = p_text + text if style.cjk else f"{p_text} {text}"
            if (start - p_end < style.pause_gap and end - p_start <= style.max_duration
                    and _fits(joined, style)):
                out[-1] = (p_start, end, joined, speaker, p_count + count)
                continue
        out.append((start, end, text, speaker, count))
    return [(start, end, text, count == 1) for start, end, text, _speaker, count in out]


def _cues_from_segments(segments: list[dict], style: Style) -> list[tuple[float, float, str]]:
    budget = style.line_chars * style.max_lines
    raw = []
    for seg in segments:
        text = (seg.get("text") or "").strip() if style.cjk else " ".join((seg.get("text") or "").split())
        if not text:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        units, joiner = _units(text, style.cjk)
        chunks, cur = [], ""
        for w in units:
            if cur and len(cur) + len(joiner) + len(w) > budget:
                chunks.append(cur.strip())
                cur = w
            else:
                cur = f"{cur}{joiner}{w}" if cur else w
        chunks.append(cur.strip())
        chunks = [c for c in chunks if c]
        total = sum(len(c) for c in chunks) or 1
        t = start
        for c in chunks:
            span = (end - start) * len(c) / total
            raw.append((t, t + span, c))
            t += span
    return raw


def _finish(raw, style: Style) -> list[Cue]:
    """Final timing and wrapping, shared by transcripts and translations.
    raw: (start, end, text[, one word?]) - a cue that's one spoken word stays
    on one line, however short the lines are (a word is never split: CJK
    text wraps between characters, and would put "今日" on two)."""
    cues: list[Cue] = []
    gap = 0.0 if style.one_word else style.min_gap      # a word a cue: no blank between words
    for i, (start, end, text, *one_word) in enumerate(raw):
        nxt = raw[i + 1][0] if i + 1 < len(raw) else None
        # Stretch short cues toward the minimum, but never into the next -
        # not even to keep a cue visible: a floor applied after the clamp
        # pushed ends past the next start (caught by a 500-transcript
        # property test). If the next cue is closer than the gap, this one
        # ends halfway to it.
        if end - start < style.min_duration:
            end = start + style.min_duration
        end = max(end, start + 0.2)   # a zero-length cue would vanish
        if nxt is not None:
            end = min(end, nxt - gap)
            if end <= start:
                end = start + (nxt - start) / 2 if nxt > start else start + 0.01
        cues.append(Cue(start, end, text if one_word and one_word[0] else
                        wrap(text, style.line_chars, style.max_lines, style.cjk)))
    return cues


def _name_speakers(words: list[dict]) -> list[dict]:
    """Each turn's first word led by who's speaking ("Speaker 2: Thanks") -
    only when more than one person speaks. Part of the word, so the cue's
    length checks count it."""
    if len({w.get("speaker") for w in words if w.get("speaker")}) < 2:
        return words
    out, last = [], None
    for w in words:
        speaker = w.get("speaker")
        if speaker and speaker != last:
            w = dict(w, text=f"{speaker}: {w['text']}", sp=True)
        last = speaker or last
        out.append(w)
    return out


def build_cues(segments: list[dict], style: Style | None = None) -> list[Cue]:
    """segments: [{start, end, text, speaker?, words: [{start, end, word}]?}],
    in seconds from the start of the audio. A segment's speaker (Resolve's
    own transcription names them) always starts a new cue, and with
    style.speaker_names leads its first one."""
    style = style or Style()
    words = _flatten_words(segments)
    if words and style.speaker_names:
        words = _name_speakers(words)
    if words and style.one_word:
        raw = _word_cues(words)
    else:
        raw = _cues_from_words(words, style) if words else _cues_from_segments(segments, style)
    return _finish(raw, style)


def language_runs(segments: list[dict]) -> list[tuple[str, list[dict]]]:
    """Consecutive segments grouped by their "language" ("" if unmarked)."""
    runs: list[tuple[str, list[dict]]] = []
    for seg in segments:
        lang = seg.get("language") or ""
        if runs and runs[-1][0] == lang:
            runs[-1][1].append(seg)
        else:
            runs.append((lang, [seg]))
    return runs


def build_cues_mixed(segments: list[dict], style_for) -> list[Cue]:
    """A mixed-language transcript -> cues, each run of one language with
    style_for(language)'s Style. A cue stretched toward the minimum never
    runs into the next language's first one."""
    cues: list[Cue] = []
    gap = 0.0
    for lang, run in language_runs(segments):
        style = style_for(lang)
        gap = style.min_gap
        part = build_cues(run, style)
        if cues and part and cues[-1].end > part[0].start - gap:
            last = cues[-1]
            end = part[0].start - gap
            cues[-1] = Cue(last.start, end if end > last.start else last.start + (part[0].start - last.start) / 2,
                           last.text)
        cues += part
    return cues


# ------------------------------------------------------------ sentences

# A sentence also ends at a pause this long (Whisper sometimes runs speech
# on without a full stop), or before it grows past this many characters -
# NLLB was trained on sentences, and quality drops on paragraph-long input.
SENTENCE_PAUSE = 1.5
SENTENCE_MAX_CHARS = 240


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    words: list[dict]   # the source words, for timing the translation
    language: str = ""  # a mixed-language transcript: the language it was spoken in


def sentences_from_words(words: list[dict], cjk: bool = False) -> list[Sentence]:
    out, cur = [], []

    def flush(upto=None):
        take = cur[:upto] if upto is not None else list(cur)
        if take:
            out.append(Sentence(take[0]["start"], take[-1]["end"], _join(take, cjk), take))
        del cur[:len(take)]

    for w in words:
        if cur and w["start"] - cur[-1]["end"] >= SENTENCE_PAUSE:
            flush()
        if cur and len(_join(cur + [w], cjk)) > SENTENCE_MAX_CHARS:
            # Too long without a full stop: end at its last clause break
            # if it has one past halfway, else here.
            k = None
            for i, x in enumerate(cur[:-1]):
                if x["text"].endswith(CLAUSE_END) and len(_join(cur[:i + 1], cjk)) >= SENTENCE_MAX_CHARS / 2:
                    k = i
            flush(k + 1 if k is not None else None)
        cur.append(w)
        if w["text"].endswith(SENTENCE_END):
            flush()
    flush()
    return out


def sentences_from_segments(segments: list[dict], cjk: bool = False) -> list[Sentence]:
    """Sentences from a transcript. Segments without word timings (or an
    SRT's cues) get words with time shared out by characters inside each."""
    words = _flatten_words(segments)
    if not words:
        words = []
        for seg in segments:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            start, end = float(seg["start"]), float(seg["end"])
            units = _CJK_UNIT.findall(text) if cjk else text.split()
            total = sum(len(u) for u in units) or 1
            t, spaced = start, False
            for u in units:
                span = (end - start) * len(u) / total
                if u.isspace():
                    spaced = True     # CJK: carried as the next character's "sp"
                else:
                    words.append({"start": t, "end": t + span, "text": u, "sp": spaced})
                    spaced = False
                t += span
    return sentences_from_words(words, cjk)


def sentences_mixed(segments: list[dict], cjk_for) -> list[Sentence]:
    """Sentences from a mixed-language transcript, each marked with its
    language; cjk_for(language) says whether it's written without spaces."""
    out = []
    for lang, run in language_runs(segments):
        out += [replace(x, language=lang) for x in sentences_from_segments(run, cjk=cjk_for(lang))]
    return out


# ---------------------------------------------------------- translation


def _balanced_chunks(units: list[str], joiner: str, n: int) -> list[str]:
    """Split units into n pieces of about equal length, moving each break
    to a nearby punctuation mark when there is one (within about a quarter
    of a piece's length)."""
    if n <= 1 or len(units) <= 1:
        return [joiner.join(units).strip()]
    n = min(n, len(units))
    pos, acc = [], 0
    for i, u in enumerate(units):
        acc += len(u) + (len(joiner) if i else 0)
        pos.append(acc)           # pos[i]: length through unit i
    piece = pos[-1] / n
    cuts, prev = [], -1
    for k in range(1, n):
        target, best, best_score = k * piece, None, None
        # A cut after unit i; leave a unit for every piece still to come.
        span = range(prev + 1, len(units) - (n - k))
        allowed = [i for i in span if not (joiner == "" and _forbidden_break(units, i + 1))]
        for i in allowed or span:     # every break forbidden: take the least bad
            score = abs(pos[i] - target) / piece
            if units[i].endswith(SENTENCE_END):
                score -= 0.35
            elif _good_break(units, i + 1):
                score -= 0.45 if joiner == "" else 0.25
            if joiner == "" and _inside_latin(units, i + 1):
                score += 0.5
            if best_score is None or score < best_score:
                best, best_score = i, score
        cuts.append(best)
        prev = best
    out, start = [], 0
    for c in cuts + [len(units) - 1]:
        out.append(joiner.join(units[start:c + 1]).strip())
        start = c + 1
    return [x for x in out if x]


def _split_translation(text: str, span: float, style: Style) -> list[str]:
    """The fewest balanced pieces that each fit on screen and, spread over
    the sentence's time, stay under max_duration each."""
    units, joiner = _units(text, style.cjk)
    if not units:
        return []
    n = max(1, math.ceil(span / style.max_duration))
    while n < len(units):
        chunks = _balanced_chunks(units, joiner, n)
        if all(_fits(c, style) for c in chunks):
            return chunks
        n += 1
    return _balanced_chunks(units, joiner, len(units))


def _time_pieces(sentence: Sentence, pieces: list[str]) -> list[tuple[float, float, str]]:
    """Give each translated piece its share of the sentence's time. A break
    x% of the way through the translated text goes where the source had
    spoken x% of its characters - at the gap between two source words, so
    a cue never changes mid-word or straddles a pause. With fewer source
    words than pieces, time is shared out evenly instead."""
    words = sentence.words
    if len(pieces) == 1:
        return [(sentence.start, sentence.end, pieces[0])]
    fracs, acc, total = [], 0, sum(len(p) for p in pieces) or 1
    for p in pieces[:-1]:
        acc += len(p)
        fracs.append(acc / total)
    if len(words) - 1 < len(fracs):
        span = sentence.end - sentence.start
        edges = [sentence.start] + [sentence.start + f * span for f in fracs] + [sentence.end]
        return [(edges[i], edges[i + 1], p) for i, p in enumerate(pieces)]
    src_pos, acc = [], 0
    for w in words:
        acc += len(w["text"]) + 1
        src_pos.append(acc)
    src_total = src_pos[-1]
    cuts, prev = [], -1
    for k, f in enumerate(fracs):
        # A boundary after source word j: strictly increasing, and leaving
        # a word for every piece still to come.
        hi = len(words) - 1 - (len(fracs) - k)
        j = min(range(prev + 1, hi + 1), key=lambda j: abs(src_pos[j] / src_total - f))
        cuts.append(j)
        prev = j
    out, first = [], 0
    for p, c in zip(pieces, cuts + [len(words) - 1]):
        out.append((words[first]["start"], words[c]["end"], p))
        first = c + 1
    return out


def _tidy(text: str, lang: str = "") -> str:
    if not lang.startswith(("jpn", "zho", "yue")):
        return " ".join(text.split())
    # NLLB writes ASCII punctuation in Japanese and Chinese: commas become
    # the native mark, and the final full stop goes - subtitles in these
    # languages normally end on no mark at all.
    comma = "、" if lang.startswith("jpn") else "，"
    # MADLAD's Japanese splits decimals: "23. 976" -> "23.976".
    text = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", text.strip())
    text = re.sub(r"\s*(?:(?<!\d),|,(?!\d))\s*", comma, text)   # not "1,000"
    # ...and its Chinese spaces out the native marks ("帧 ， 但").
    text = re.sub(r"\s*([，。、！？：；])\s*", r"\1", text)
    return re.sub(r"[.。]+$", "", text)


def translated_cues(sentences: list[Sentence], translations: list[str],
                    style: Style, lang: str = "") -> list[Cue]:
    """One translation per sentence, in order -> timed, wrapped cues.
    lang: the NLLB code, for per-language punctuation."""
    raw = []
    for sentence, text in zip(sentences, translations):
        text = _tidy(text or "", lang)
        if not text:
            continue
        pieces = _split_translation(text, sentence.end - sentence.start, style)
        raw.extend(_time_pieces(sentence, pieces))
    return _finish(raw, style)


# ------------------------------------------------------------ SRT input

_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_srt(text: str, keep_lines: bool = False) -> list[dict]:
    """SRT -> [{start, end, text}] - build_cues' segment shape, without
    word timings. Tolerates a BOM, CRLFs, missing cue numbers and
    formatting tags. keep_lines: a cue's lines stay separate ("\n"), as
    they're shown, instead of joined into one run of text."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").lstrip("﻿")):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        times = _SRT_TIME.findall(lines[idx])
        if len(times) < 2:
            continue
        start, end = (int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000
                      for h, m, s, ms in times[:2])
        body = ("\n" if keep_lines else " ").join(
            re.sub(r"<[^>]+>|\{[^}]*\}", "", l).strip() for l in lines[idx + 1:]).strip()
        if body:
            out.append({"start": start, "end": end, "text": body})
    return out


# ------------------------------------------------------------------ SRT


def _srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[Cue], offset: float = 0.0) -> str:
    """offset shifts every cue, e.g. by the timeline's start timecode if
    the importer places SRT times absolutely."""
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{_srt_time(c.start + offset)} --> {_srt_time(c.end + offset)}\n{c.text}\n")
    return "\n".join(out)


def to_plain_text(cues: list[Cue]) -> str:
    return "\n".join(c.text.replace("\n", " ") for c in cues)
