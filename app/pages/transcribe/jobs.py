#!/usr/bin/env python3
"""
Transcribe's long jobs, each a QThread the page only listens to: the
transcription (render -> Whisper/Parakeet -> SRT), the two translation
engines, the Setup tab's installs and downloads, and the engine check
(ProbeJob - on the GUI thread it would freeze the page for as long as the
venv's Python takes to import faster-whisper).
"""

from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from . import ai_translate as ai
from . import env_setup as es
from . import languages as L
from . import resolve_transcript
from . import subtitles as st
from .resolve_ext import RenderCancelled, TranscribeController, TranscribeResolveError

from .plan import MARK

RENDER_DIR = es.ROOT / "renders"
OUTPUT_DIR = es.ROOT / "output"
TRANSCRIPTS_DIR = es.ROOT / "transcripts"
SETTINGS_PATH = es.ROOT / "settings.json"
KEEP_TRANSCRIPTS = 20
RESOLVE_CHILD = Path(__file__).with_name("resolve_child.py")
RESOLVE_TIMEOUT = 3 * 3600   # seconds: Resolve took ~10 s for 2 minutes, so this is a very long timeline

CJK_WHISPER = {"ja", "zh", "yue"}                          # see languages.CJK_CODES
NO_SPACE_WHISPER = CJK_WHISPER | {"th", "lo", "my", "km"}  # see languages.NO_SPACE_CODES


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w\- ]+", "_", text).strip() or "timeline"


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _prune_transcripts():
    """Keep the newest KEEP_TRANSCRIPTS - they're small, but not free."""
    try:
        files = sorted(TRANSCRIPTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[KEEP_TRANSCRIPTS:]:
        try:
            old.unlink()
        except OSError:
            pass


class WorkerRun:
    """Runs worker.py in the venv and hands each protocol message to
    on_message. Shared by both jobs; cancel() kills the process."""

    def __init__(self):
        self.proc = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    def run(self, args: list[str], on_message) -> str | None:
        """Returns the worker's error message, if it reported one. A worker
        that dies outright on the GPU (a CUDA library crashing the process,
        which worker.py can't catch) is run once more on the CPU."""
        error, crashed = self._run_once(args, on_message)
        if crashed and "--device" in args and args[args.index("--device") + 1] != "cpu":
            on_message({"type": "status", "message": "The engine crashed on the GPU; trying again on the CPU…"})
            cpu_args = list(args)
            cpu_args[args.index("--device") + 1] = "cpu"
            error, _crashed = self._run_once(cpu_args, on_message)
        return error

    def _run_once(self, args: list[str], on_message) -> tuple[str | None, bool]:
        """(error message, whether the process died without reporting one)."""
        # Stop pressed before (or while) the process starts: cancel() had no
        # process to kill, so don't start one - or kill the one just
        # started - rather than reading it to the end.
        if self.cancelled:
            raise RenderCancelled()
        self.proc = subprocess.Popen(
            [str(es.venv_python()), str(es.WORKER), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, **es._no_window())
        if self.cancelled:
            self.proc.kill()
            self.proc.wait()
            raise RenderCancelled()
        error = None
        crash_log = collections.deque(maxlen=10)  # the end of any traceback it printed
        for line in self.proc.stdout:
            if self.cancelled:
                break
            if not line.startswith(MARK):
                crash_log.append(line.strip())
                continue
            try:
                msg = json.loads(line[len(MARK):])
            except ValueError:
                crash_log.append(line.strip())
                continue
            if msg.get("type") == "error":
                error = msg.get("message", "The engine reported an error.")
            else:
                on_message(msg)
        self.proc.wait()
        if self.cancelled:
            raise RenderCancelled()
        if error is None and self.proc.returncode != 0:
            error = "The engine stopped unexpectedly."
            tail = "\n".join(line for line in crash_log if line)
            if tail:
                error += f"\n\nOutput:\n{tail}"
            return error, True
        return error, False


class TranscribeJob(QThread):
    stage = Signal(str)
    progress = Signal(int)
    note = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, resolve: TranscribeController, timeline_name: str, plan: dict,
                 language: str, hotwords: str, style: st.Style, languages=()):
        """plan: {"engine": whisper|parakeet|auto, "whisper": dir or "",
        "parakeet": dir or ""} - see TranscribePage._plan. languages: the
        ones spoken, for mixed audio (language is then "")."""
        super().__init__()
        self.resolve = resolve
        self.timeline_name = timeline_name
        self.plan, self.language, self.hotwords, self.style = plan, language, hotwords, style
        self.languages = list(languages)
        self._cancel = False
        self._worker = WorkerRun()

    def cancel(self):
        self._cancel = True
        self._worker.cancel()

    def _on_message(self, msg):
        kind = msg.get("type")
        if kind == "progress" and msg.get("total"):
            frac = min(1.0, msg["done"] / msg["total"])
            self.progress.emit(25 + int(frac * 73))
            self.stage.emit(f"Transcribing… {_clock(msg['done'])} of {_clock(msg['total'])}")
        elif kind == "status":
            self.note.emit(msg.get("message", ""))
            if "GPU" in msg.get("message", "") or "CPU" in msg.get("message", ""):
                self.stage.emit(msg["message"])

    def run(self):
        t0 = time.time()
        audio = None
        out_json = None
        try:
            if self.plan["engine"] == "resolve":
                # 1-2. Resolve Studio transcribes the timeline itself: no render.
                result = self._transcribe_in_resolve()
                RENDER_DIR.mkdir(parents=True, exist_ok=True)
                out_json = RENDER_DIR / f"resolve {datetime.now().strftime('%H%M%S%f')}.json"
                out_json.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            else:
                # 1. Render. Progress 0-25% of the bar: rendering audio is quick.
                self.stage.emit("Rendering the timeline's audio…")
                audio = self.resolve.render_timeline_audio(
                    str(RENDER_DIR), progress=lambda pct: self.progress.emit(int(pct * 0.25)),
                    cancelled=lambda: self._cancel)
                if self._cancel:
                    raise RenderCancelled()

                # 2. Whisper. 25-98%.
                self.stage.emit("Loading the model…")
                out_json = RENDER_DIR / (Path(audio).stem + ".json")
                error = self._worker.run(
                    ["transcribe", "--audio", audio, "--engine", self.plan["engine"],
                     "--model", self.plan["whisper"], "--parakeet", self.plan["parakeet"], "--device", "auto",
                     "--language", self.language, "--languages", ",".join(self.languages),
                     "--hotwords", self.hotwords, "--out", str(out_json)],
                    self._on_message)
                if error or not out_json.is_file():
                    raise RuntimeError(error or "The transcription engine stopped unexpectedly.")
                result = json.loads(out_json.read_text(encoding="utf-8"))

            # 3. Cues + SRT.
            self.stage.emit("Building subtitles…")
            if len(result.get("languages") or {}) > 1:
                cues = st.build_cues_mixed(result["segments"], lambda lang: style_for(self.style, lang))
            else:
                cues = st.build_cues(result["segments"], style_for(self.style, result.get("language")))
            if not cues:
                raise RuntimeError("No speech was found in the timeline's audio.")
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
            srt_path = OUTPUT_DIR / f"{_safe_name(self.timeline_name)} {stamp}.srt"
            srt_path.write_text(st.to_srt(cues), encoding="utf-8")
            # The word timings are kept for translating later.
            TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            transcript = TRANSCRIPTS_DIR / (srt_path.stem + ".json")
            os.replace(out_json, transcript)
            _prune_transcripts()
            self.progress.emit(100)
            self.succeeded.emit({
                "srt": str(srt_path), "transcript": str(transcript), "cues": len(cues),
                "language": result.get("language"), "languages": result.get("languages") or {},
                "device": result.get("device"),
                "engine": result.get("engine", "whisper"),
                "duration": result.get("duration", 0.0), "seconds": time.time() - t0,
                "name": srt_path.stem, "speakers": result.get("speakers") or [],
            })
        except RenderCancelled:
            self.failed.emit("Stopped.")
        except (TranscribeResolveError, RuntimeError, OSError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            # The rendered mix is temporary (a long timeline is gigabytes).
            for leftover in (audio, out_json):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass


    def _transcribe_in_resolve(self) -> dict:
        """Resolve's own transcription of the timeline (resolve_child.py, in a
        process of its own so Buddy doesn't freeze while Resolve works), as the
        worker's result shape. Stop kills the child; Resolve may still finish
        transcribing in the background, which changes nothing but its own copy."""
        python = _child_python()
        if not python:
            raise RuntimeError("Buddy couldn't find the Python it runs on, so it can't ask Resolve to transcribe.")
        fd, result_path = tempfile.mkstemp(prefix="buddy_transcribe_", suffix=".json")
        os.close(fd)
        try:
            proc = subprocess.Popen([python, str(RESOLVE_CHILD), result_path], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **es._no_window())
            proc.stdin.write(json.dumps({"timeline": self.plan.get("timeline", ""), "language": self.language,
                                         "fresh": bool(self.plan.get("fresh", True))}).encode("utf-8"))
            proc.stdin.close()
            started = time.monotonic()
            self.progress.emit(10)
            while proc.poll() is None:
                waited = time.monotonic() - started
                if self._cancel:
                    proc.kill()
                    proc.wait()
                    raise RenderCancelled()
                if waited > RESOLVE_TIMEOUT:
                    proc.kill()
                    raise RuntimeError("Resolve didn't finish transcribing the timeline in time.")
                self.stage.emit(f"Resolve is transcribing the timeline… {_clock(waited)}")
                time.sleep(0.5)
            try:
                with open(result_path, encoding="utf-8") as f:
                    out = json.load(f)
            except (OSError, ValueError):
                out = {"ok": False, "error": "Resolve didn't answer."}
        finally:
            try:
                os.remove(result_path)
            except OSError:
                pass
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or "Resolve didn't answer.")
        got = out["result"]
        result = resolve_transcript.to_segments(got["transcription"], got["fps"], got["start_frame"])
        if not result["segments"]:
            raise RuntimeError("No speech was found in the timeline's audio.")
        self.progress.emit(95)
        return {**result, "engine": "resolve", "device": "resolve"}


def _child_python():
    """The interpreter Audio Assistant runs its Resolve child with - Buddy's
    own, not Resolve's script host (see audio_assistant.page.child_python)."""
    from pages.audio_assistant.page import child_python
    return child_python()


def style_for(style: st.Style, language) -> st.Style:
    """The cue style for text in this (Whisper) language: written without
    spaces, and in full-width characters, or not."""
    return st.Style(**{**style.__dict__, "cjk": language in NO_SPACE_WHISPER, "wide": language in CJK_WHISPER})


def write_srts(sentences: list[st.Sentence], translations: dict, targets: list[str],
                max_chars: int, max_lines: int, base_name: str, out_dir: Path) -> list[dict]:
    """One SRT per target language, cut and timed from its translations."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for code in targets:
        style = st.Style(max_chars=max_chars, max_lines=max_lines, cjk=code in L.NO_SPACE_CODES,
                         wide=code in L.CJK_CODES)
        cues = st.translated_cues(sentences, translations[code], style, code)
        path = out_dir / f"{base_name} - {L.name_of(code)}.srt"
        path.write_text(st.to_srt(cues), encoding="utf-8")
        written.append({"path": str(path), "language": L.name_of(code), "cues": len(cues),
                        "timed": [(c.start, c.end, c.text) for c in cues]})
    return written


class TranslateJob(QThread):
    """Sentences -> NLLB or MADLAD -> one SRT per target language in out_dir."""

    stage = Signal(str)
    progress = Signal(int)
    note = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, sentences: list[st.Sentence], source: str, targets: list[str],
                 model_dir: str, max_chars: int, max_lines: int, base_name: str, out_dir: str,
                 family: str = "nllb", sources=None):
        """sources: an NLLB code per sentence, for a mixed-language transcript."""
        super().__init__()
        self.sentences, self.source, self.targets = sentences, source, targets
        self.sources = sources
        self.model_dir, self.max_chars, self.max_lines = model_dir, max_chars, max_lines
        self.base_name, self.out_dir = base_name, Path(out_dir)
        self.family = family
        self._worker = WorkerRun()
        self._current = ""

    def cancel(self):
        self._worker.cancel()

    def _on_message(self, msg):
        kind = msg.get("type")
        if kind == "target":
            self._current = L.name_of(msg.get("code", ""))
        elif kind == "progress" and msg.get("total"):
            pct = int(100 * min(1.0, msg["done"] / msg["total"]))
            self.progress.emit(pct)
            self.stage.emit(f"Translating to {self._current}… {pct}%")
        elif kind == "status":
            self.note.emit(msg.get("message", ""))
            self.stage.emit(msg.get("message", ""))

    def run(self):
        t0 = time.time()
        stamp = datetime.now().strftime("%H%M%S%f")
        job_in = RENDER_DIR / f"translate {stamp} in.json"
        job_out = RENDER_DIR / f"translate {stamp} out.json"
        try:
            RENDER_DIR.mkdir(parents=True, exist_ok=True)
            job_in.write_text(json.dumps({
                "source": self.source, "targets": self.targets, "family": self.family,
                "tags": {c: L.MADLAD_CODES[c] for c in self.targets if c in L.MADLAD_CODES},
                "sentences": [s.text for s in self.sentences],
                **({"sources": self.sources} if self.sources else {})}, ensure_ascii=False), encoding="utf-8")
            self.stage.emit("Loading the translation model…")
            error = self._worker.run(["translate", "--model", self.model_dir, "--device", "auto",
                                      "--input", str(job_in), "--out", str(job_out)], self._on_message)
            if error or not job_out.is_file():
                raise RuntimeError(error or "The translation engine stopped unexpectedly.")
            result = json.loads(job_out.read_text(encoding="utf-8"))
            written = write_srts(self.sentences, result["translations"], self.targets,
                                  self.max_chars, self.max_lines, self.base_name, self.out_dir)
            self.progress.emit(100)
            where = "GPU" if result.get("device") == "cuda" else "CPU"
            self.succeeded.emit({"files": written, "where": f"on the {where}",
                                 "seconds": time.time() - t0})
        except RenderCancelled:
            self.failed.emit("Stopped.")
        except (RuntimeError, OSError, KeyError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            for leftover in (job_in, job_out):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass


class AITranslateJob(QThread):
    """Sentences -> Ask Buddy's language model -> one SRT per target. Runs
    in Buddy's own interpreter (plain HTTPS), not the transcription venv."""

    stage = Signal(str)
    progress = Signal(int)
    note = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, client, provider_name: str, sentences: list[st.Sentence], source: str,
                 targets: list[str], glossary: str, max_chars: int, max_lines: int,
                 base_name: str, out_dir: str):
        super().__init__()
        self.client, self.provider_name = client, provider_name
        self.sentences, self.source, self.targets, self.glossary = sentences, source, targets, glossary
        self.max_chars, self.max_lines = max_chars, max_lines
        self.base_name, self.out_dir = base_name, Path(out_dir)
        self._cancel = False

    def cancel(self):
        # Takes effect between requests: one already sent runs to its end.
        self._cancel = True

    def run(self):
        from pages.manual_chat.llm import LLMError

        t0 = time.time()
        try:
            texts = [s.text for s in self.sentences]
            translations = {}
            for n, code in enumerate(self.targets):
                name = L.name_of(code)
                self.stage.emit(f"Translating to {name} with {self.provider_name}…")

                def progress(done, total, n=n, name=name):
                    frac = (n + done / max(1, total)) / len(self.targets)
                    self.progress.emit(int(100 * frac))
                    self.stage.emit(f"Translating to {name} with {self.provider_name}… "
                                    f"{done} of {total} sentences")

                translations[code] = ai.translate(
                    self.client, texts, L.name_of(self.source), name, self.glossary,
                    progress=progress, cancelled=lambda: self._cancel)
            written = write_srts(self.sentences, translations, self.targets,
                                  self.max_chars, self.max_lines, self.base_name, self.out_dir)
            self.progress.emit(100)
            self.succeeded.emit({"files": written, "where": f"with {self.provider_name}",
                                 "seconds": time.time() - t0})
        except ai.TranslateCancelled:
            self.failed.emit("Stopped.")
        except (ai.TranslateError, LLMError, OSError, KeyError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SetupJob(QThread):
    """Runs one env_setup call (install, download); forwards its output lines."""

    line = Signal(str)
    failed = Signal(str)
    succeeded = Signal()

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self.fn(*self.args, progress=self.line.emit, cancelled=lambda: self._cancel)
        except es.SetupCancelled:
            self.failed.emit("Stopped.")
        except es.SetupError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.succeeded.emit()


class ProbeJob(QThread):
    """What's installed: the hardware (nvidia-smi, once), the engine (asks
    the venv's own Python) and the models on disk. Seconds, off the GUI
    thread."""

    done = Signal(object)

    def __init__(self, extra_models: dict, hw=None):
        super().__init__()
        self.extra, self.hw = dict(extra_models or {}), hw

    def run(self):
        hw = self.hw or es.detect_hardware()
        self.done.emit({
            "hw": hw,
            "env": es.env_status(),
            "models": es.installed_models(self.extra),
            "found": es.scan_existing_models(),
            "tmodels": es.installed_translation_models(self.extra),
            "tfound": es.scan_existing_translation_models(),
        })
