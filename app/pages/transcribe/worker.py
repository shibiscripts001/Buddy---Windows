#!/usr/bin/env python3
"""
Runs INSIDE the transcription venv (~/.buddy/transcribe/venv), never in
Buddy's own interpreter - it imports faster-whisper, which Buddy doesn't
have. Buddy launches it as a subprocess and reads its stdout.

Protocol: every message is one line, "@@BUDDY " + a JSON object, so stray
library output (ONNX Runtime warnings, Hugging Face notices) can never be
mistaken for data. Types:

  {"type": "status",   "message": str}
  {"type": "progress", "done": float, "total": float}   seconds / bytes
  {"type": "segment",  "start", "end", "text", "words": [...]}
  {"type": "target",   "code": str}          translate: starting this language
  {"type": "done",     "language", "duration", "device", "out": path}
  {"type": "error",    "message": str}

The full result is also written to --out as JSON, so a transcript never
depends on every stdout line having been read.

Modes:
  transcribe --audio F [--engine whisper|parakeet|auto] [--model DIR] [--parakeet DIR]
             [--device auto|cuda|cpu] [--language xx | --languages xx,yy] [--hotwords "..."] --out F
             (--languages: mixed audio - Whisper, each part in its own language;
              every segment then carries "language", and the result "languages")
  download   --model ID|REPO --output DIR [--expected-bytes N] [--files a,b,...]
  translate  --model DIR --input F --out F [--device auto|cuda|cpu]   (NLLB or MADLAD)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback

MARK = "@@BUDDY "

if sys.platform == "win32":
    import glob
    import site
    # env_setup's NVIDIA_PACKAGES (pip's cuBLAS/cuDNN) put their DLLs in
    # site-packages/nvidia/*/bin, which Windows never searches - without
    # this, ctranslate2 can't find them and the GPU run fails.
    for _pkg_dir in site.getsitepackages():
        for _dll_dir in glob.glob(os.path.join(_pkg_dir, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(_dll_dir)
            except OSError:
                pass


def emit(kind: str, **fields) -> None:
    sys.stdout.write(MARK + json.dumps({"type": kind, **fields}) + "\n")
    sys.stdout.flush()


# -------------------------------------------------------------- transcribe


def _load_model(model_dir: str, device: str):
    """GPU in float16 when asked (or auto + CUDA present), else CPU int8.
    A GPU that fails to load falls back to CPU rather than failing the run -
    slower, but the user still gets subtitles, and is told why."""
    from faster_whisper import WhisperModel
    import ctranslate2

    try:
        want_cuda = device == "cuda" or (device == "auto" and ctranslate2.get_cuda_device_count() > 0)
    except Exception as exc:
        emit("status", message=f"Failed to query GPU ({type(exc).__name__}: {exc}); falling back to CPU.")
        want_cuda = False

    if want_cuda:
        try:
            return WhisperModel(model_dir, device="cuda", compute_type="float16"), "cuda"
        except Exception as exc:  # missing/mismatched CUDA libraries, out of VRAM
            emit("status", message=f"GPU unavailable ({type(exc).__name__}: {exc}); using the CPU instead.")
    return WhisperModel(model_dir, device="cpu", compute_type="int8"), "cpu"


def _gpu_failed(device: str, exc: Exception) -> bool:
    """A missing cuBLAS/cuDNN often only shows when the model first runs
    ("Library cublas64_12.dll is not found"), after it loaded fine - the
    caller then reloads on the CPU and tries again. Says so if it did."""
    if device != "cuda" or not isinstance(exc, (RuntimeError, OSError)):
        return False
    emit("status", message=f"The GPU failed ({type(exc).__name__}: {exc}); using the CPU instead.")
    return True


# A punctuated primer, fed to every 30-second window through the hotwords
# slot. Measured on a real 20-minute vlog with large-v3: with no primer
# Whisper wrote lowercase run-ons (about 10 sentence ends per 3 minutes);
# with it, 89 sentence ends in 20 minutes and proper capitals, with no
# repetition loops. The textbook alternative - initial_prompt plus
# condition_on_previous_text=True - punctuated slightly more but drifted
# its timestamps by minutes (a 279 s hole where the words still existed),
# which for subtitles is the worst possible failure. initial_prompt alone
# is no good either: faster-whisper drops it after the first window when
# conditioning is off; hotwords are re-inserted into every window.
PRIMERS = {
    "en": "Hello, and welcome back. Here's what happened today, in full sentences.",
}


def _whisper_segments(model, audio, language, hotwords, total, offset=0.0, tag=False, vad=True, primer=True):
    """offset: where this audio starts in the timeline's (a piece of it, in
    mixed mode); tag: mark each segment with its language; primer: False
    for a short piece - a second of audio can come back as the primer
    itself ("Here's what happened." between Japanese sentences)."""
    # The primer is English, and an English prompt can pull non-English
    # speech toward English - so it's only used when the audio IS English.
    hotwords = " ".join(x for x in (PRIMERS.get(language, "") if primer else "", hotwords.strip()) if x) or None
    segments, _info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        # Silero VAD skips silence and music beds - the main source of
        # Whisper "hallucinating" text into quiet stretches.
        vad_filter=vad,
        hotwords=hotwords,
        condition_on_previous_text=False,
    )
    result = []
    for seg in segments:
        item = {
            "start": round(seg.start + offset, 3),
            "end": round(seg.end + offset, 3),
            "text": seg.text.strip(),
            "words": [
                {"start": round(w.start + offset, 3), "end": round(w.end + offset, 3), "word": w.word}
                for w in (seg.words or [])
            ],
        }
        if tag:
            item["language"] = language
        result.append(item)
        emit("segment", **item)
        emit("progress", done=min(seg.end + offset, total), total=total)
    return result


# Mixed languages: the audio is cut into utterances (Silero VAD, as
# everywhere here), each one's language is detected among only the ones the
# user said are spoken, and each run of utterances in one language is
# transcribed with that language set. Measured on an English/Japanese
# interview (alternating sentences, down to one-word "Okay." / "はい。"):
# every utterance right, 1.3 s of detection on the GPU for 38 s of audio.
# Left on auto-detect, the same audio came out Japanese with four of its
# five English sentences missing. Whisper's own multilingual=True only
# re-detects once per 30-second window - too coarse for people switching
# language sentence by sentence.
MIXED_VAD = dict(min_silence_duration_ms=300, speech_pad_ms=150, max_speech_duration_s=30)
MIXED_SURE = 0.75       # an utterance less sure than this takes its neighbours' language
MIXED_SHORT_RUN = 2.0   # seconds: a run this short skips Whisper's own VAD (it can drop it)
# Inside a sentence: an utterance is transcribed in one language, and
# Whisper told "ja" leaves an English phrase in it out - no text, no gap in
# the timing either side. So after each run, speech no word covers is
# listened to again, and transcribed in its own language if it's another
# one (or if the utterance came out with no words at all). Measured on
# Japanese sentences with English phrases inside and no pause around them
# ("今日の撮影は really went well と思います。"): every dropped phrase back, 1.5 s
# extra for 22 s. On 23 min of English it added nothing false and got back
# 16 s of speech the first pass had dropped (8 s extra). A word Whisper
# stretches over the other language's (one-word switches) still hides it.
GAP_MIN = 0.4           # seconds of speech with no word on it
GAP_PAD = 0.05
GAP_SPEECH = 0.3        # of it that VAD calls speech
# ...and it has to sound like one of the named languages: their detection
# scores together at least this. Real phrases scored 0.87-1.0; a burst of
# background noise that came back as "Papa!" scored 0.18 (Whisper's own
# no_speech_prob didn't tell them apart - a real one scored 0.62).
GAP_LANGUAGE = 0.5


def decide_languages(scores: list[dict], languages: list[str]) -> list[str]:
    """One language per utterance from each one's detection scores ({code:
    probability}), choosing only among `languages`. An unsure one (or one
    with no scores) takes the language of the nearest sure one before it,
    else after it - a mumbled "mm" shouldn't split a run."""
    picks, sure = [], []
    for probs in scores:
        allowed = {code: float(probs.get(code, 0.0)) for code in languages}
        best = max(allowed, key=allowed.get)
        total = sum(allowed.values())
        picks.append(best)
        sure.append(total > 0 and allowed[best] / total >= MIXED_SURE)
    out = []
    for i, pick in enumerate(picks):
        if sure[i]:
            out.append(pick)
            continue
        before = next((out[j] for j in range(i - 1, -1, -1) if sure[j]), None)
        after = next((picks[j] for j in range(i + 1, len(picks)) if sure[j]), None)
        out.append(before or after or pick)
    return out


def language_runs(spans: list[dict], picks: list[str]) -> list[tuple[str, int, int]]:
    """(language, first sample, last sample) for each run of consecutive
    utterances in the same language."""
    runs = []
    for span, lang in zip(spans, picks):
        if runs and runs[-1][0] == lang:
            runs[-1] = (lang, runs[-1][1], span["end"])
        else:
            runs.append((lang, span["start"], span["end"]))
    return runs


def _mixed_segments(model, device, audio, languages, hotwords, total):
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    rate = 16000
    emit("status", message="Working out which language each part is in…")
    spans = get_speech_timestamps(audio, VadOptions(**MIXED_VAD))
    scores = []
    for n, span in enumerate(spans):
        _lang, _prob, probs = model.detect_language(audio[span["start"]:span["end"]])
        scores.append(dict(probs))
        emit("progress", done=min(span["end"] / rate, total) * 0.1, total=total)
    picks = decide_languages(scores, languages)
    runs = language_runs(spans, picks)
    spoken = {}
    for lang, first, last in runs:
        spoken[lang] = spoken.get(lang, 0.0) + (last - first) / rate
    emit("status", message="Languages heard: " + ", ".join(
        f"{code} {secs:.0f}s" for code, secs in sorted(spoken.items(), key=lambda x: -x[1])) + ".")
    emit("status", message=f"Transcribing on the {'GPU' if device == 'cuda' else 'CPU'}…")
    result = []
    for lang, first, last in runs:
        piece = audio[first:last]
        long_enough = (last - first) / rate >= MIXED_SHORT_RUN
        result += _whisper_segments(model, piece, lang, hotwords, total, offset=first / rate, tag=True,
                                    vad=long_enough, primer=long_enough)
    result += _fill_gaps(model, audio, spans, picks, languages, result, hotwords, total)
    result.sort(key=lambda seg: seg["start"])
    return result, spoken


def uncovered(words: list[tuple[float, float]], first: float, last: float, least: float = GAP_MIN):
    """The stretches of [first, last] no word's (start, end) covers, at
    least `least` seconds long."""
    gaps, at = [], first
    for start, end in sorted(words):
        if start - at >= least:
            gaps.append((at, start))
        at = max(at, end)
    if last - at >= least:
        gaps.append((at, last))
    return gaps


def _fill_gaps(model, audio, spans, picks, languages, result, hotwords, total):
    """What the runs left out inside each utterance: another language's
    phrase (see GAP_MIN), or all of an utterance that came out empty."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    rate = 16000
    words = [(w["start"], w["end"]) for seg in result for w in seg["words"]]
    added = []
    for span, lang in zip(spans, picks):
        first, last = span["start"] / rate, span["end"] / rate
        inside = [w for w in words if w[1] > first and w[0] < last]
        for start, end in uncovered(inside, first, last):
            a, b = max(0, int((start - GAP_PAD) * rate)), min(len(audio), int((end + GAP_PAD) * rate))
            piece = audio[a:b]
            speech = get_speech_timestamps(piece, VadOptions(min_silence_duration_ms=100, speech_pad_ms=30))
            if sum(x["end"] - x["start"] for x in speech) / rate < GAP_SPEECH:
                continue
            _lang, _prob, probs = model.detect_language(piece)
            probs = dict(probs)
            if sum(float(probs.get(code, 0.0)) for code in languages) < GAP_LANGUAGE:
                continue
            heard = decide_languages([probs], languages)[0]
            if heard == lang and inside:
                continue        # its own language, left out on purpose (a cough, a filler)
            added += _whisper_segments(model, piece, heard, hotwords, total, offset=a / rate, tag=True,
                                       vad=False, primer=False)
    return [seg for seg in added if seg["text"]]


# Parakeet stamps each token with the frame (80 ms) it was emitted in, and
# no end. Measured against audio onsets on a real vlog, its starts land
# ~0.1 s late (Whisper's are as accurate on average), so a word starts one
# frame before its first token and ends a little after its last one -
# never past the next word.
PARAKEET_FRAME = 0.08
PARAKEET_TAIL = 0.16
PARAKEET_BATCH = 8
# Parakeet writes hesitations out; Whisper and subtitle style guides drop
# them (measured: 14 in 20 minutes of a vlog, against none from Whisper).
# English only - "um", "uh" are real words elsewhere.
FILLERS = {"en": {"uh", "um", "uhm", "umm", "erm", "hmm"}}


def _drop_fillers(words, language):
    fillers = FILLERS.get(language or "")
    if not fillers:
        return words
    out = []
    for w in words:
        bare = w["word"].strip().lower().rstrip(".,!?;:")
        if bare in fillers:
            end = w["word"].strip()[-1:]
            # A filler that ended the sentence hands its full stop back.
            if end in ".!?" and out and out[-1]["word"][-1:] not in ".!?":
                out[-1]["word"] = out[-1]["word"].rstrip(",;:") + end
            continue
        out.append(w)
    return out


def _parakeet_words(result, offset, limit):
    raw = []
    for tok, t in zip(result.tokens or [], result.timestamps or []):
        t = offset + float(t)
        if tok.startswith(" ") or not raw:
            raw.append({"text": tok.strip(), "first": t, "last": t, "sp": tok[:1] == " "})
        else:
            raw[-1]["text"] += tok
            raw[-1]["last"] = t
    raw = [w for w in raw if w["text"]]
    words, prev_end = [], offset
    for i, w in enumerate(raw):
        nxt = raw[i + 1]["first"] - PARAKEET_FRAME if i + 1 < len(raw) else limit
        start = max(prev_end, w["first"] - PARAKEET_FRAME)
        end = max(start + 0.02, min(w["last"] + PARAKEET_TAIL, nxt, limit))
        words.append({"start": round(start, 3), "end": round(end, 3),
                      "word": (" " if w["sp"] else "") + w["text"]})
        prev_end = end
    return words


def _parakeet_segments(model_dir, audio, total, language=None):
    import onnx_asr
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    emit("status", message="Loading Parakeet…")
    asr = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", model_dir, quantization="int8",
                              providers=["CPUExecutionProvider"]).with_timestamps()
    emit("status", message="Transcribing with Parakeet on the CPU…")
    # Parakeet is fed speech only, in pieces of at most 30 s (the same
    # Silero VAD faster-whisper uses): silence is skipped, and memory stays
    # flat however long the timeline is.
    rate = 16000
    spans = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=200,
                                                    max_speech_duration_s=30))
    emit("progress", done=0.0, total=total)
    result = []
    for b in range(0, len(spans), PARAKEET_BATCH):
        batch = spans[b:b + PARAKEET_BATCH]
        outs = asr.recognize([audio[s["start"]:s["end"]] for s in batch])
        for span, out in zip(batch, outs):
            words = _drop_fillers(_parakeet_words(out, span["start"] / rate, span["end"] / rate), language)
            if not words:
                continue
            item = {"start": words[0]["start"], "end": words[-1]["end"],
                    "text": "".join(w["word"] for w in words).strip(), "words": words}
            result.append(item)
            emit("segment", **item)
        emit("progress", done=min(batch[-1]["end"] / rate, total), total=total)
    return result


def decode_audio(path: str, rate: int = 16000):
    """faster_whisper.decode_audio, reading only the audio. Every other
    stream is marked discard=all first, so the demuxer skips a video's
    bytes instead of reading them: a camera file is mostly video, and
    Dailies hands this the camera file itself. On a 24 GB Sony MP4, two
    minutes of audio read 29 MB instead of 2.2 GB - the whole clip went
    from ~10 minutes of reading to seconds. Anything the shortcut can't do
    falls back to faster-whisper's own, which reads everything."""
    import gc

    import av
    import numpy as np
    from faster_whisper import decode_audio as decode_everything
    try:
        from av.stream import Discard
        from faster_whisper.audio import _group_frames, _ignore_invalid_frames, _resample_frames
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=rate)
        chunks = []
        with av.open(path, mode="r", metadata_errors="ignore") as container:
            wanted = container.streams.audio[0]
            for stream in container.streams:
                if stream.index != wanted.index:
                    stream.discard = Discard.all
            frames = _resample_frames(_group_frames(_ignore_invalid_frames(container.decode(wanted)), 500000),
                                      resampler)
            chunks = [frame.to_ndarray().reshape(-1) for frame in frames]
        del resampler
        gc.collect()        # faster-whisper does the same: the resampler leaks otherwise
    except Exception:  # noqa: BLE001 - an older PyAV, an odd file: the slow way still works
        return decode_everything(path, sampling_rate=rate)
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe(args) -> int:
    """--engine whisper: --model is the Whisper model. parakeet: --parakeet
    is the model, and --model (optional) a Whisper model used only to
    detect the language. auto: Parakeet when the language is one of its
    25, else Whisper."""
    from env_setup import PARAKEET_LANGS

    emit("status", message="Reading the audio…")
    audio = decode_audio(args.audio, rate=16000)
    total = len(audio) / 16000.0
    engine, language, probability = args.engine, args.language or None, None
    mixed = [c for c in (args.languages or "").split(",") if c.strip()]
    if len(mixed) >= 2:
        return _transcribe_mixed(args, audio, total, [c.strip() for c in mixed])

    whisper, device = None, "cpu"
    if args.model and (engine == "whisper" or language is None):
        emit("status", message="Loading the model…")
        whisper, device = _load_model(args.model, args.device)
    if language is None and whisper is not None:
        # faster-whisper detects the language inside transcribe(), before
        # any decoding (the generator is lazy): one window's cost.
        try:
            _probe, probe_info = whisper.transcribe(audio, vad_filter=True)
        except Exception as exc:
            if not _gpu_failed(device, exc):
                raise
            whisper, device = _load_model(args.model, "cpu")
            _probe, probe_info = whisper.transcribe(audio, vad_filter=True)
        language, probability = probe_info.language, probe_info.language_probability
        emit("status", message=f"Detected language: {language} ({probability:.0%}).")

    if engine == "auto":
        engine = "parakeet" if args.parakeet and (language is None or language in PARAKEET_LANGS) else "whisper"
    if engine == "parakeet" and language is not None and language not in PARAKEET_LANGS:
        raise RuntimeError(f"Parakeet doesn't cover this language ({language}). "
                           "Pick Auto or a Whisper model.")
    if engine == "whisper" and whisper is None:
        if not args.model:
            raise RuntimeError(f"No Whisper model is installed for this language ({language}).")
        whisper, device = _load_model(args.model, args.device)

    if engine == "parakeet":
        del whisper    # free the GPU before the long run
        device = "cpu"
        result = _parakeet_segments(args.parakeet, audio, total, language)
    else:
        emit("status", message=f"Transcribing on the {'GPU' if device == 'cuda' else 'CPU'}…")
        emit("progress", done=0.0, total=total)
        try:
            result = _whisper_segments(whisper, audio, language, args.hotwords, total)
        except Exception as exc:
            if not _gpu_failed(device, exc):
                raise
            whisper, device = _load_model(args.model, "cpu")
            emit("progress", done=0.0, total=total)
            result = _whisper_segments(whisper, audio, language, args.hotwords, total)

    payload = {
        "language": language,
        "language_probability": round(float(probability or 0), 3),
        "duration": total,
        "device": device,
        "engine": engine,
        "segments": result,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    emit("progress", done=total, total=total)
    emit("done", language=language, duration=total, device=device, engine=engine, out=args.out)
    return 0


def _transcribe_mixed(args, audio, total, languages) -> int:
    if not args.model:
        raise RuntimeError("Mixed languages need a Whisper model – get one on the Setup tab.")
    emit("status", message="Loading the model…")
    whisper, device = _load_model(args.model, args.device)
    emit("progress", done=0.0, total=total)
    try:
        result, spoken = _mixed_segments(whisper, device, audio, languages, args.hotwords, total)
    except Exception as exc:
        if not _gpu_failed(device, exc):
            raise
        whisper, device = _load_model(args.model, "cpu")
        result, spoken = _mixed_segments(whisper, device, audio, languages, args.hotwords, total)
    # The one heard most stands for the whole where a single language is
    # expected (the file name, older readers of the transcript).
    main = max(spoken, key=spoken.get) if spoken else languages[0]
    payload = {"language": main, "languages": {k: round(v, 1) for k, v in spoken.items()},
               "language_probability": 0.0, "duration": total, "device": device, "engine": "whisper",
               "segments": result}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    emit("progress", done=total, total=total)
    emit("done", language=main, languages=payload["languages"], duration=total, device=device,
         engine="whisper", out=args.out)
    return 0


# ---------------------------------------------------------------- download


def _folder_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def download(args) -> int:
    done = threading.Event()
    failure: list[BaseException] = []

    def run():
        try:
            if args.files:
                # A named repo (translation models): faster-whisper's
                # downloader only fetches Whisper's own file names.
                from huggingface_hub import snapshot_download
                snapshot_download(args.model, local_dir=args.output,
                                  allow_patterns=args.files.split(","))
            else:
                from faster_whisper.utils import download_model
                download_model(args.model, output_dir=args.output)
        except BaseException as exc:  # noqa: BLE001 - reported below
            failure.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    # Hugging Face's own progress bar redraws in place with \r, which a
    # line-reading parent never sees - so progress is the folder growing.
    expected = args.expected_bytes or 0
    while not done.wait(1.0):
        emit("progress", done=float(_folder_bytes(args.output)), total=float(expected))
    if failure:
        raise failure[0]
    size = _folder_bytes(args.output)
    emit("progress", done=float(size), total=float(expected or size))
    emit("done", out=args.output)
    return 0


# ---------------------------------------------------------------- translate


def _load_translator(model_dir: str, device: str):
    """NLLB through CTranslate2: GPU with int8 weights and float16 maths,
    else CPU int8 - with the same fall-back-and-say-so as Whisper."""
    import ctranslate2

    try:
        want_cuda = device == "cuda" or (device == "auto" and ctranslate2.get_cuda_device_count() > 0)
    except Exception as exc:
        emit("status", message=f"Failed to query GPU ({type(exc).__name__}: {exc}); falling back to CPU.")
        want_cuda = False

    if want_cuda:
        try:
            return ctranslate2.Translator(model_dir, device="cuda", compute_type="int8_float16"), "cuda"
        except Exception as exc:
            emit("status", message=f"GPU unavailable ({type(exc).__name__}: {exc}); using the CPU instead.")
    return ctranslate2.Translator(model_dir, device="cpu", compute_type="int8"), "cpu"


BATCH = 24
# NLLB can loop: measured on a real vlog with the 1.3B, Japanese repeated a
# whole sentence twice and Chinese ran "小小小的小小小..." to the length cap.
# A 1.2 repetition penalty cleared every case across six languages without
# hurting the rest; no_repeat_ngram_size (3 or 4) missed some.
REPETITION_PENALTY = 1.2
MADLAD_REPETITION_PENALTY = 1.0


def _collapse_repeat(text: str) -> str:
    """Last line of defence: "X X" (a sentence said twice) -> "X"."""
    t = text.strip()
    for sep in (" ", ""):
        half = (len(t) - len(sep)) // 2
        if half > 4 and t[:half] == t[half + len(sep):] and t[half:half + len(sep)] == sep:
            return t[:half]
    return t


def _encode(tok, family: str, sentence: str, src: str, tag: str) -> list[str]:
    """NLLB reads "<source code> tokens </s>" and is steered to the output
    language by starting its answer with the target code. MADLAD reads
    "<2xx> tokens </s>", tagged with the TARGET, exactly as SentencePiece
    splits that string - which puts a lone "▁" before the tag. Built any
    other way (tag first, or no "</s>"), MADLAD answered in broken,
    looping fragments on every sentence tried."""
    if family == "madlad":
        return tok.encode(f"<2{tag}> {sentence}", add_special_tokens=False).tokens + ["</s>"]
    return [src] + tok.encode(sentence, add_special_tokens=False).tokens + ["</s>"]


def translate(args) -> int:
    """--input JSON {"source": "eng_Latn", "targets": [codes], "sentences": [str],
                     "family": "nllb"|"madlad", "tags": {code: MADLAD tag},
                     "sources": [code per sentence] - a mixed-language transcript}
    -> --out JSON {"device", "translations": {code: [str, one per sentence]}}.

    Codes are NLLB's throughout; a MADLAD job carries each target's tag
    too. The model is loaded once for every target language."""
    from tokenizers import Tokenizer

    with open(args.input, encoding="utf-8") as f:
        job = json.load(f)
    sentences, targets, src = job["sentences"], job["targets"], job["source"]
    # Mixed: each sentence has its own source language, and one already in
    # the target language is kept as it was said.
    sources = job.get("sources") or [src] * len(sentences)
    family, tags = job.get("family", "nllb"), job.get("tags") or {}
    emit("status", message="Loading the translation model…")
    translator, device = _load_translator(args.model, args.device)
    tok = Tokenizer.from_file(os.path.join(args.model, "tokenizer.json"))
    emit("status", message=f"Translating on the {'GPU' if device == 'cuda' else 'CPU'}…")

    total = float(sum(1 for tgt in targets for s in sources if s != tgt))
    done = 0
    out = {}
    for tgt in targets:
        emit("target", code=tgt)
        # MADLAD's input carries the target, so it's encoded per language.
        result = [s if sources[i] == tgt else "" for i, s in enumerate(sentences)]
        todo = [i for i in range(len(sentences)) if sources[i] != tgt]
        encoded = {i: _encode(tok, family, sentences[i], sources[i], tags.get(tgt, "")) for i in todo}
        # Similar lengths batch together (much less padding), and the answer
        # length is capped per batch at twice its longest input - a runaway
        # repetition loop can't then run to the model's limit.
        order = sorted(todo, key=lambda i: len(encoded[i]))
        for b in range(0, len(order), BATCH):
            idx = order[b:b + BATCH]
            batch = [encoded[i] for i in idx]
            longest = max(len(x) for x in batch)
            options = dict(beam_size=4, max_decoding_length=min(256, 2 * longest + 16))
            if family == "madlad":
                # NLLB's 1.2 made MADLAD drop the end of numbers ("29.97"
                # became "29." in Japanese); at 1.0 it didn't loop in four
                # languages. disable_unk as Nextcloud's own MADLAD app runs it.
                options.update(repetition_penalty=MADLAD_REPETITION_PENALTY, disable_unk=True)
            else:
                options.update(repetition_penalty=REPETITION_PENALTY, target_prefix=[[tgt]] * len(batch))
            try:
                hyps = translator.translate_batch(batch, **options)
            except Exception as exc:
                if not _gpu_failed(device, exc):
                    raise
                translator, device = _load_translator(args.model, "cpu")
                hyps = translator.translate_batch(batch, **options)
            for i, h in zip(idx, hyps):
                # NLLB's answer starts with the language code; MADLAD's doesn't.
                tokens = h.hypotheses[0][1:] if family == "nllb" else h.hypotheses[0]
                ids = [tok.token_to_id(t) for t in tokens]
                text = _collapse_repeat(
                    tok.decode([x for x in ids if x is not None], skip_special_tokens=True))
                # MADLAD learned subtitle dialogue dashes: "Okay." came back
                # as "- Okay." Kept only where the source had one.
                if family == "madlad" and not sentences[i].lstrip().startswith("-"):
                    text = text.lstrip("-– ").strip() or text
                result[i] = text
            done += len(idx)
            emit("progress", done=float(done), total=total)
        out[tgt] = result
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"device": device, "translations": out}, f, ensure_ascii=False)
    emit("done", device=device, out=args.out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("--audio", required=True)
    t.add_argument("--model", default="", help="Whisper model folder (for parakeet: the language detector)")
    t.add_argument("--parakeet", default="", help="Parakeet model folder")
    t.add_argument("--engine", default="whisper", choices=["whisper", "parakeet", "auto"])
    t.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    t.add_argument("--language", default="")
    t.add_argument("--languages", default="", help="mixed audio: the languages spoken, e.g. ja,en")
    t.add_argument("--hotwords", default="")
    t.add_argument("--out", required=True)
    d = sub.add_parser("download")
    d.add_argument("--model", required=True)
    d.add_argument("--output", required=True)
    d.add_argument("--expected-bytes", type=int, default=0)
    d.add_argument("--files", default="", help="comma-separated: download these from repo --model")
    tr = sub.add_parser("translate")
    tr.add_argument("--model", required=True)
    tr.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    tr.add_argument("--input", required=True)
    tr.add_argument("--out", required=True)
    args = ap.parse_args()
    try:
        return {"transcribe": transcribe, "download": download, "translate": translate}[args.mode](args)
    except Exception as exc:  # noqa: BLE001 - becomes a message for the UI
        emit("error", message=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-2000:])
        return 1


if __name__ == "__main__":
    sys.exit(main())
