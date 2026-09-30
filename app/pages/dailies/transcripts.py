"""
Dailies' transcripts: each clip's speech, word by word, for the transcript
box under the player.

The Transcribe tool's engine does the work - worker.py in its own venv, run
through its WorkerRun, with the model and language chosen on that tool's page
- on the clip's media file directly (the worker decodes it itself; no render
through Resolve). "Mixed languages" there carries over too: the worker
gives each utterance its own language among the ones named, so people
switching language sentence to sentence come out in both. Segments stream back as they are recognised, so a clip's
text fills in while it plays. A finished transcript is kept on disk, keyed by
the file (path, size, modified time): reviewing the same footage again, or
the same file under two clips, costs nothing.

Measured on an RTX 5070 Ti with Large v3: a 92 s clip in 4 s, 3.4 s of which
is reading the audio and loading the model - which is why a clip is
transcribed whole, not in chunks as it plays.
"""

import hashlib
import json
import os
import tempfile

from PySide6.QtCore import QThread, Signal

from core.settings_store import BUDDY_DIR

CACHE_DIR = os.path.join(BUDDY_DIR, "dailies", "transcripts")


def _key(path, languages=()):
    """The file (path, size, modified time) - and, for mixed languages,
    which: a transcript heard as one language isn't one heard as two."""
    stat = os.stat(path)
    ident = f"{os.path.normcase(os.path.abspath(path))}|{stat.st_size}|{stat.st_mtime_ns}"
    if languages:
        ident += "|mixed:" + ",".join(sorted(languages))
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:24]


def mixed_languages():
    """The languages the Transcribe tool is set to hear in one recording
    ("Mixed languages..." on its Language menu), or [] for one language or
    detecting it."""
    from pages.transcribe import jobs, plan
    settings = plan.Settings(jobs.SETTINGS_PATH)
    if settings.get("language", "") != plan.MIXED:
        return []
    languages = [c for c in settings.get("mixed_languages") or [] if c]
    return languages if len(languages) >= 2 else []


def cached(path, cache_dir=CACHE_DIR, languages=()):
    """The transcript saved for this exact file (heard in these mixed
    languages, if any), or None."""
    try:
        with open(os.path.join(cache_dir, _key(path, languages) + ".json"), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data.get("segments"), list) else None
    except (OSError, ValueError, AttributeError):
        return None


def save(path, data, cache_dir=CACHE_DIR, languages=()):
    try:
        os.makedirs(cache_dir, exist_ok=True)
        target = os.path.join(cache_dir, _key(path, languages) + ".json")
        with open(target + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(target + ".tmp", target)
    except OSError:
        pass        # an unsaved transcript is only redone next time


def engine_plan(languages=()):
    """(plan, None) - what the worker should run, from the Transcribe
    tool's own choices - or (None, why transcription can't run here).
    languages: mixed_languages(); plan["languages"] is what the worker is
    told (empty when the model can't do mixed - then it hears one)."""
    from pages.transcribe import env_setup as es
    from pages.transcribe import jobs, plan
    if not es.venv_python().exists():
        return None, ("Transcription isn't set up on this computer yet – install it on the "
                      "Subtitles tool's Setup tab.")
    settings = plan.Settings(jobs.SETTINGS_PATH)
    models = es.installed_models(settings.get("extra_models", {}))
    if not models:
        return None, "Get a transcription model on the Subtitles tool's Setup tab."
    languages = list(languages or [])
    language = plan.MIXED if languages else settings.get("language", "")
    if language == plan.MIXED and not languages:
        language = ""       # "Mixed" with fewer than two named: detect the one
    model = settings.get("model") or "auto"
    chosen, why = plan.plan_for(model, models, language, languages)
    if chosen is None:
        chosen, why = plan.plan_for("auto", models, language, languages)
    if chosen is None and languages:
        # No model here can be told each part's language: one per clip,
        # detected, is still a transcript.
        languages = []
        chosen, why = plan.plan_for(model, models, "")
        if chosen is None:
            chosen, why = plan.plan_for("auto", models, "")
    if chosen is not None:
        chosen = {**chosen, "languages": languages}
    return chosen, why


class TranscriptJob(QThread):
    """Transcribes one clip's file. segment(clip_id, segment) for each piece
    as it's recognised, then done(clip_id, transcript) or failed(clip_id,
    message). cancel() stops it; a cancelled job reports neither."""

    segment = Signal(str, dict)
    done = Signal(str, dict)
    failed = Signal(str, str)

    def __init__(self, clip_id, path, plan, parent=None, languages=()):
        """languages: the mixed languages asked for - what the transcript is
        saved under, even if this plan could only hear one."""
        super().__init__(parent)
        self.clip_id = clip_id
        self.path = path
        self._plan = plan
        self._languages = list(languages or [])
        self._run = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if self._run is not None:
            self._run.cancel()

    def run(self):
        from pages.transcribe.jobs import WorkerRun
        from pages.transcribe.resolve_ext import RenderCancelled
        out = os.path.join(tempfile.gettempdir(), f"buddy_dailies_{os.getpid()}_{id(self)}.json")
        plan = self._plan
        args = ["transcribe", "--audio", self.path, "--engine", plan["engine"],
                "--model", plan["whisper"], "--parakeet", plan["parakeet"], "--device", "auto",
                "--language", plan["language"], "--languages", ",".join(plan.get("languages") or []),
                "--out", out]

        def on_message(message):
            if message.get("type") == "segment" and not self._cancelled:
                self.segment.emit(self.clip_id, {key: message.get(key) for key in ("start", "end", "text", "words")})

        try:
            self._run = WorkerRun()
            if self._cancelled:
                return
            error = self._run.run(args, on_message)
            if self._cancelled:
                return
            if error:
                self.failed.emit(self.clip_id, error)
                return
            with open(out, encoding="utf-8") as handle:
                result = json.load(handle)
            transcript = {"language": result.get("language", ""), "duration": result.get("duration", 0),
                          "segments": [{key: s.get(key) for key in ("start", "end", "text", "words")}
                                       for s in result.get("segments", [])]}
            save(self.path, transcript, languages=self._languages)
            self.done.emit(self.clip_id, transcript)
        except RenderCancelled:
            pass
        except Exception as exc:  # noqa: BLE001 - shown in the transcript box
            if not self._cancelled:
                self.failed.emit(self.clip_id, str(exc))
        finally:
            try:
                os.remove(out)
            except OSError:
                pass
