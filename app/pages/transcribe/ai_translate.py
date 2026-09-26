#!/usr/bin/env python3
"""
Translation by a language model: whichever provider Ask Buddy is set up
with (Claude, Gemini, an OpenAI-compatible service, or a local Ollama), via
manual_chat/llm.py - stdlib-only, so this runs in Buddy's own interpreter,
not the transcription venv. No Qt here: page.py runs it on a QThread.

Why a model at all, next to NLLB and MADLAD: it reads a batch of sentences
at once, with the lines before them, so names, terms and tone stay
consistent and "it" three lines on still means the right thing; and it can
be asked for subtitle-length phrasing rather than a literal rendering.

The contract with subtitles.translated_cues is one translation per source
sentence, in order - that is what times them. So each batch goes out as
numbered lines and must come back as the same numbers; a missing number is
asked for again, and a batch that still comes back short is an error rather
than a silently shifted subtitle file.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable

# Sentences per request: big enough for context and few round trips, small
# enough that one answer stays well inside every provider's output limit
# (and a local model's patience). A few earlier lines ride along, unasked.
BATCH = 40
CONTEXT = 4
# Output room for one batch. Only Anthropic needs it said (see llm.py);
# 40 subtitle sentences are ~2-5k tokens even in CJK scripts.
MAX_TOKENS = 16000


# "Busy, try again" answers - Gemini's 503 "high demand" turned up while
# this was written - are waited out rather than failing an hour-long
# transcript at batch 30. Any other error (a bad key, say) fails at once.
TRANSIENT = re.compile(r"HTTP (429|500|502|503|504)\b")
RETRY_WAITS = (5, 20)


class TranslateError(RuntimeError):
    """Shown to the user as is."""


class TranslateCancelled(Exception):
    pass


def _chat(client, system, content, cancelled, sleep):
    for wait in RETRY_WAITS + (None,):
        try:
            return client.chat(system, [{"role": "user", "content": content}])
        except Exception as exc:  # noqa: BLE001 - only the transient kind is retried
            if wait is None or not TRANSIENT.search(str(exc)):
                raise
        for _ in range(wait):
            if cancelled():
                raise TranslateCancelled()
            sleep(1)


def system_prompt(source: str, target: str, glossary: str = "") -> str:
    terms = (f"\nThese names and terms occur - spell them exactly like this: {glossary.strip()}."
             if glossary.strip() else "")
    return (
        f"You translate video subtitles from {source} into {target}.\n"
        "You receive JSON. \"lines\" maps a number to one spoken sentence; \"context\" holds the "
        "sentences just before them, for reference only - don't translate it.\n"
        f"Translate every line into natural spoken {target}, the way a professional subtitler "
        "would: faithful, concise and quick to read. Each line stays its own line - never merge, "
        "split, drop, add or reorder lines, even when a line is a fragment of a longer sentence. "
        "Leave names, brands and technical terms as they are wherever they're normally left "
        f"untranslated. A line that's already in {target} stays as it is.{terms}\n"
        "Reply with only a JSON object mapping each number to its translation, like "
        "{\"1\": \"...\", \"2\": \"...\"}. No notes, no code fences."
    )


def _request(lines: dict[int, str], context: list[str]) -> str:
    return json.dumps({"context": context, "lines": {str(k): v for k, v in lines.items()}},
                      ensure_ascii=False)


def parse_reply(text: str, wanted: set[int]) -> dict[int, str]:
    """The numbered translations in a reply, tolerating code fences and a
    sentence of chatter around the JSON (local models add both). Numbers
    that weren't asked for are ignored."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if isinstance(data.get("lines"), dict):     # echoed the request's shape
        data = data["lines"]
    out = {}
    for key, value in data.items():
        m = re.fullmatch(r"\s*(\d+)\s*", str(key))
        if m and int(m.group(1)) in wanted and isinstance(value, str) and value.strip():
            out[int(m.group(1))] = value.strip()
    return out


def translate(client, sentences: list[str], source: str, target: str, glossary: str = "",
              progress: Callable[[int, int], None] = lambda done, total: None,
              cancelled: Callable[[], bool] = lambda: False, sleep=time.sleep) -> list[str]:
    """sentences -> one translation each, in order. source / target are
    language NAMES ("English", "Japanese") - what a model understands.
    Raises TranslateError, TranslateCancelled, or llm.LLMError."""
    system = system_prompt(source, target, glossary)
    out: list[str] = [""] * len(sentences)
    for b in range(0, len(sentences), BATCH):
        if cancelled():
            raise TranslateCancelled()
        chunk = {i + 1: s for i, s in enumerate(sentences[b:b + BATCH])}
        context = sentences[max(0, b - CONTEXT):b]
        got: dict[int, str] = {}
        # One retry, for whatever came back missing (or unparseable).
        for _attempt in range(2):
            todo = {k: v for k, v in chunk.items() if k not in got}
            if not todo:
                break
            reply = _chat(client, system, _request(todo, context), cancelled, sleep)
            got.update(parse_reply(reply.content, set(todo)))
            if cancelled():
                raise TranslateCancelled()
        missing = [k for k in chunk if k not in got]
        if missing:
            raise TranslateError(
                f"The AI returned {len(chunk) - len(missing)} of {len(chunk)} lines for sentences "
                f"{b + 1}-{b + len(chunk)}, even when asked again. Try again, or choose another "
                "model (a larger one follows the format more reliably).")
        for k, text in got.items():
            out[b + k - 1] = text
        progress(min(b + BATCH, len(sentences)), len(sentences))
    return out
