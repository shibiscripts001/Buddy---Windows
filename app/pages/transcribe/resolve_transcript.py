#!/usr/bin/env python3
"""
Resolve Studio's own transcription as Transcribe's segments - no Qt, no
Resolve, so it's tested directly (tests/test_resolve_transcript.py).

From 21.1 a timeline can hand over the transcription Resolve makes of it
(measured on Studio 21.1.0.17, 2026-09-29): the timeline's media pool item
(Timeline.GetMediaPoolItem) is transcribed as one nested clip -
TranscribeAudio(useSpeakerDetection, transcribeAsNestedClip=True), which
blocks until it's done (~10 s for 2 minutes) - and read back with
GetTranscription(useNestedClipTranscription=True):

    {"language": "en", "segments": [{"start": "01:00:02:03", "end": ...,
      "text": " Welcome back...", "speaker": "Speaker 1",
      "words": [{"start", "end", "text": " Welcome"}, ...]}, ...]}

Times are the TIMELINE's own timecodes (the first word of a clip placed 2 s
in read 01:00:02:03), so seconds from its start are (frame - start frame) /
fps. Silences come as their own segments and words, "(...)", and are left
out. A word keeps its leading space, as Whisper's do. "speaker" is "Speaker
1", "Speaker 2"... unless someone renamed them in Resolve. Transcribing again
without ClearTranscription returns the old transcription at once, however
the timeline has changed since.

The free edition has no transcription at all, and 21.0 no GetTranscription,
so the choice is only offered on Studio 21.1 and later (available()).
"""

from __future__ import annotations

import re

SILENCE = "(...)"
MIN_VERSION = (21, 1)
_TIMECODE = re.compile(r"^(\d+):(\d\d):(\d\d)([:;.,])(\d+)$")
# The project setting transcriptionLanguage takes every code of Transcribe's
# Language menu as it is (and refuses made-up ones) - except Chinese: "zh"
# is refused, "zh-Hans" taken (Studio 21.1).
_TO_RESOLVE = {"zh": "zh-Hans"}


def resolve_language(code: str) -> str:
    """Transcribe's (Whisper) language code as the transcriptionLanguage setting takes it."""
    return _TO_RESOLVE.get(code or "", code or "")


def whisper_language(code: str) -> str:
    """A language Resolve names ("en", "zh-hans") as Transcribe's codes do ("zh")."""
    return (code or "").split("-")[0].lower()


def available(product: str, version) -> bool:
    """Whether this Resolve can transcribe a timeline for Buddy: Studio, 21.1 or later."""
    try:
        numbers = tuple(int(v) for v in list(version or [])[:2])
    except (TypeError, ValueError):
        return False
    return "studio" in (product or "").lower() and len(numbers) == 2 and numbers >= MIN_VERSION


def timecode_frames(timecode: str, fps: float) -> int | None:
    """'HH:MM:SS:FF' (or drop-frame 'HH:MM:SS;FF') -> frames counted from
    00:00:00:00, the way Timeline.GetStartFrame counts them. None if it
    isn't a timecode."""
    m = _TIMECODE.match(str(timecode or "").strip())
    if not m or not fps:
        return None
    hours, minutes, seconds, sep, frames = m.groups()
    nominal = int(round(fps))
    total = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * nominal + int(frames)
    if sep in ";,":   # drop frame: two (four at 59.94) frame numbers skipped each minute but every tenth
        dropped = int(round(fps * 0.066666))
        all_minutes = int(hours) * 60 + int(minutes)
        total -= dropped * (all_minutes - all_minutes // 10)
    return total


def _seconds(timecode, fps, start_frame) -> float | None:
    frames = timecode_frames(timecode, fps)
    return None if frames is None else max(0.0, (frames - start_frame) / fps)


def to_segments(transcription: dict, fps: float, start_frame: int) -> dict:
    """{"language", "segments": [{start, end, text, speaker, words: [{start,
    end, word}]}], "speakers": [names, in order of first word], "duration"}
    - seconds from the timeline's start, silences left out."""
    segments, speakers, duration = [], [], 0.0
    for seg in (transcription or {}).get("segments") or []:
        words = []
        for w in seg.get("words") or []:
            raw = str(w.get("text") or "")
            if not raw.strip() or raw.strip() == SILENCE:
                continue
            start, end = _seconds(w.get("start"), fps, start_frame), _seconds(w.get("end"), fps, start_frame)
            if start is None or end is None:
                continue
            words.append({"start": start, "end": max(end, start), "word": raw.replace(SILENCE, "").rstrip()})
        if not words:
            continue
        speaker = seg.get("speaker") or None
        if speaker and speaker not in speakers:
            speakers.append(speaker)
        text = "".join(w["word"] for w in words).strip()
        segments.append({"start": words[0]["start"], "end": words[-1]["end"], "text": text,
                         "speaker": speaker, "words": words})
        duration = max(duration, words[-1]["end"])
    return {"language": whisper_language((transcription or {}).get("language")), "segments": segments,
            "speakers": speakers, "duration": duration}
