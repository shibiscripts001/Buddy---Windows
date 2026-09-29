"""
Dailies' transcripts: each clip's speech, word by word, for the transcript
box under the player.

The Transcribe tool's engine does the work - worker.py in its own venv, run
through its WorkerRun, with the model and language chosen on that tool's page
- on the clip's media file directly (the worker decodes it itself; no render
through Resolve). Segments stream back as they are recognised, so a clip's
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


def _key(path):
    stat = os.stat(path)
    ident = f"{os.path.normcase(os.path.abspath(path))}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:24]


def cached(path, cache_dir=CACHE_DIR):
    """The transcript saved for this exact file, or None."""
    try:
        with open(os.path.join(cache_dir, _key(path) + ".json"), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data.get("segments"), list) else None
    except (OSError, ValueError, AttributeError):
        return None


def save(path, data, cache_dir=CACHE_DIR):
    try:
        os.makedirs(cache_dir, exist_ok=True)
        target = os.path.join(cache_dir, _key(path) + ".json")
        with open(target + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(target + ".tmp", target)
    except OSError:
        pass        # an unsaved transcript is only redone next time


def engine_plan():
    """(plan, None) - what the worker should run, from the Transcribe
    tool's own choices - or (None, why transcription can't run here)."""
    from pages.transcribe import env_setup as es
    from pages.transcribe import jobs, plan
    if not es.venv_python().exists():
        return None, ("Transcription isn't set up on this computer yet – install it on the "
                      "Transcribe tool's Setup tab.")
    settings = plan.Settings(jobs.SETTINGS_PATH)
    models = es.installed_models(settings.get("extra_models", {}))
    if not models:
        return None, "Get a transcription model on the Transcribe tool's Setup tab."
    language = settings.get("language", "")
    if language == plan.MIXED:
        language = ""       # detected per clip; mixed runs need the languages named
    chosen, why = plan.plan_for(settings.get("model") or "auto", models, language)
    if chosen is None:
        chosen, why = plan.plan_for("auto", models, language)
    return chosen, why


class TranscriptJob(QThread):
    """Transcribes one clip's file. segment(clip_id, segment) for each piece
    as it's recognised, then done(clip_id, transcript) or failed(clip_id,
    message). cancel() stops it; a cancelled job reports neither."""

    segment = Signal(str, dict)
    done = Signal(str, dict)
    failed = Signal(str, str)

    def __init__(self, clip_id, path, plan, parent=None):
        super().__init__(parent)
        self.clip_id = clip_id
        self.path = path
        self._plan = plan
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
                "--language", plan["language"], "--out", out]

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
            save(self.path, transcript)
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
