#!/usr/bin/env python3
"""
Transcribe - turn the current timeline's dialogue into subtitles, and
translate them. A web page (core/web_page.py): the view is web/index.html
+ transcribe.js, with eight tabs - Subtitles, Translate, Subtitle
Conversion, the four Text+ tabs (Font Styling, Timeline Layout, Timeline
Animation, Custom Animation) and, at the far end, Setup.

One button, three stages, all off the GUI thread (jobs.py):

  1. Render   the timeline's audio mix to an audio-only file through
              Resolve's render queue (resolve_ext.py - the user's Deliver
              settings are restored afterwards, and the job removed).
  2. Whisper  worker.py, inside Buddy's private environment
              (~/.buddy/transcribe/venv), on the GPU with an NVIDIA card and
              on the CPU everywhere else. Streams progress back as it goes.
  3. Cues     subtitles.py rebuilds readable cues from the word timings and
              writes an SRT to ~/.buddy/transcribe/output/.

On Resolve Studio 21.1+ the Model menu also has "DaVinci Resolve (Studio)":
Resolve transcribes the timeline itself in place of stages 1-2, speakers
told apart (resolve_child.py, in a process of its own; resolve_transcript.py).
Nothing needs setting up for it. If Resolve already has a transcription of
the timeline, the user chooses: use it, or transcribe again (replacing it).

Several languages spoken: Language > "Mixed languages" asks which ones,
and the worker (--languages) gives each utterance its own - the subtitles
then have every line in the language it was said in, each language cut by
its own rules (subtitles.build_cues_mixed). Translating such a transcript
translates each sentence from its own language, and keeps the ones already
in the target language as they were.

Then, back on the GUI thread, the SRT goes onto subtitle track 1 - the only
track Resolve's API will place subtitles on - unless "Add to the timeline
when done" is unticked (then it's the SRT file only, and the result's "Add
to timeline" places it later, on the timeline it was made from). If track 1
already has subtitles the user chooses: replace them (the count is shown),
or keep the SRT file only. Nothing is overwritten without that choice.

Translate: the last transcript - or any SRT - becomes one SRT per chosen
language, saved to a folder the user picks - and, unless the user turns it
off, a NEW timeline: a copy of the current one with a subtitle track per
language, the original language first (resolve_ext.timeline_with_subtitles,
drt.py). The timeline they started from is never changed. Three engines:
NLLB-200 or MADLAD-400 on this computer (worker.py translate, in the venv),
or the language model Ask Buddy is set up with (ai_translate.py, in
Buddy's own interpreter). Whole sentences are translated, then re-cut
into cues (see subtitles.py).

Subtitle Conversion: each subtitle on a subtitle track becomes a Text+
clip on a video track, ready to style on the Animation page
(resolve_ext.subtitles_to_text_plus). The video track suggested is the
topmost empty one - until the user picks one, which is kept for that
timeline until a conversion uses it.

The Text+ tabs: styling, placing and animating the Text+ clips, conversion's
next step. They are text_animator/text_plus.py's (TextPlusTools, the old
Text Animator page), hosted here: its view is web/textplus.js + canvas.js,
its messages are "tp_<name>" both ways (routed in _dispatch, and never held
back by a running job - on their own page they never were), and its log
lines join this page's activity log.

Setup: the engine and every model say how much they download before
anything starts, and only download when asked. What's installed is checked
on a thread (jobs.ProbeJob) - asking the venv's Python takes seconds, and
would freeze the page.

Questions (replace track 1's subtitles? send the transcript to a cloud
model? which language is this SRT in?) are asked in the page: _ask() emits "ask", and the view's answer continues.

Settings live in ~/.buddy/transcribe/settings.json beside the rest of this
tool's files.

Protocol:
    to the view    catalog, setup, options, timeline, translate, convert, job,
                   result, log, ask, alert, toast, tab
    from the view  tab, rescan, install_env, download, use_copy,
                   pick_model_folder, stop, option, mixed, run, place_last, save_srt,
                   refresh_timeline, tr_option, targets, choose_folder,
                   translate_last, translate_srt, show_files, conv_option,
                   convert, answer
    and tp_* both ways for the Text+ tabs (see text_animator/text_plus.py)
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog

from core import crash_log
from core.i18n import tr, tr_filter
from core.resolve_bridge import ResolveConnectionError
from core.web_page import WebToolPage
from pages.text_animator.text_plus import TABS as TEXT_PLUS_TABS
from pages.text_animator.text_plus import TextPlusTools

from . import ai_translate as ai
from . import drt
from . import env_setup as es
from . import jobs
from . import languages as L
from . import plan
from . import subtitles as st
from .resolve_ext import TranscribeController, TranscribeResolveError

LOG_LIMIT = 200
RECHECK_SECONDS = 10
TABS = ("subtitles", "translate", "convert", *TEXT_PLUS_TABS, "setup")
_WHILE_BUSY = {"stop", "tab", "answer", "option", "mixed", "tr_option", "targets", "refresh_timeline",
               "show_files", "save_srt", "choose_folder", "rescan", "conv_option"}


class TranscribePage(WebToolPage):
    tool_id = "transcribe"
    display_name = "Subtitles"      # formerly "Transcribe" (tool_id kept, so settings carry over)
    category = "Editing Tools"
    web_dir = str(Path(__file__).with_name("web"))

    def build_state(self):
        self.settings = plan.Settings(self._settings_path())
        self.tab = "subtitles"
        self._chose_tab = False
        self.job = None
        self.job_kind = None
        self.stage, self.progress = "", None
        self.last_srt = None
        self.result = None
        self._run_timeline = ""     # the timeline the running transcription is of
        self._pending_translate = None   # a transcript to translate once the placing is done
        self._translating = None         # {"source", "srt", "label"} of the translation running
        self._placing = False
        self._last_output_dir = None
        self._log = []
        self._asks = {}
        self.timeline_text, self.timeline_ok = "Not read yet.", False
        # Resolve Studio 21.1+'s own transcription (TranscribeController.own_transcription).
        self.own = {"available": False, "uid": "", "existing": False}
        # Subtitle Conversion: from which subtitle track onto which video track.
        self.sub_track, self.target_track = 1, 1
        self._target_chosen = False      # the user picked target_track: keep it (see _read_tracks)
        self.tracks = {"timeline": "", "video": 0, "subtitle": 0}
        self.textplus = TextPlusTools(self)     # the Text+ tabs
        # What's installed, from jobs.ProbeJob.
        self.hw = None
        self.env = None
        self.models, self.found, self.tmodels, self.tfound = {}, {}, {}, {}
        self._probe = None
        self._probe_again = False
        self._probed_at = 0.0
        self._rescan()

    def _settings_path(self):
        return jobs.SETTINGS_PATH

    def web_ready(self):
        self.emit("catalog", {
            "languages": [{"code": c, "label": lbl} for lbl, c in plan.LANGUAGES],
            "spoken": [{"code": c, "name": n} for n, c in plan.spoken_languages()],
            "targets": [{"code": c, "name": n} for n, c in L.sorted_targets()],
            "mixed": plan.MIXED,
        })
        self._push_all()
        self.textplus.web_ready()
        self.emit("log", self._log)
        for ask_id, (kind, payload, _then, _cancel) in self._asks.items():
            self.emit("ask", dict(payload, id=ask_id, kind=kind))

    def on_shown(self):
        # Coming back to the page re-checks what's installed (a model may
        # have been added elsewhere) - but not straight after a check.
        if time.monotonic() - self._probed_at > RECHECK_SECONDS:
            self._rescan()
        self._read_timeline()
        self.textplus.on_shown()

    def on_app_quitting(self):
        self.textplus.on_app_quitting()
        if self.job is not None:
            self.job.cancel()
            self.job.wait(5000)
        if self._probe is not None:
            self._probe.wait(3000)

    def _dispatch(self, name, data):
        if name.startswith("tp_"):
            crash_log.trail("action", f"{self._trail_label()} {name}")
            handler = getattr(self.textplus, f"on_{name[3:]}", None)
            if handler is not None and name.isidentifier():
                handler(data)
            return
        if self.job is not None and name not in _WHILE_BUSY:
            return
        super()._dispatch(name, data)

    # ---------------------------------------------------------- probing --

    def _rescan(self):
        if self._probe is not None:
            self._probe_again = True
            return
        self._probe = jobs.ProbeJob(self.settings.get("extra_models", {}), self.hw)
        self._probe.done.connect(self._probed)
        self._probe.finished.connect(self._probe_finished)
        self._probe.start()
        self._push_setup()

    def _probed(self, found):
        self._probed_at = time.monotonic()
        self.hw, self.env = found["hw"], found["env"]
        self.models, self.found = found["models"], found["found"]
        self.tmodels, self.tfound = found["tmodels"], found["tfound"]
        if not self._chose_tab and not self.ready:
            self.tab = "setup"
            self.emit("tab", self.tab)
        self._push_all()

    def _probe_finished(self):
        self._probe = None
        if self._probe_again:
            self._probe_again = False
            self._rescan()

    @property
    def ready(self):
        """Something can transcribe: the engine here with a model, or Resolve Studio itself."""
        return self.ready_here or self.own["available"]

    @property
    def ready_here(self):
        return bool(self.env and self.env.ready and self.models)

    # ------------------------------------------------------------ pushes --

    def _push_all(self):
        self._push_setup()
        self._push_options()
        self._push_translate()
        self._push_job()
        self._push_timeline()
        self._push_convert()
        self.emit("result", self.result)

    def _push_setup(self):
        hw, env = self.hw, self.env
        busy = self.job is not None
        self.emit("setup", {
            "probing": self._probe is not None and env is None,
            "checking": self._probe is not None,
            "hardware": hw.describe() if hw else "",
            "gpu": bool(hw and hw.nvidia),
            "env_ready": bool(env and env.ready),
            "env_detail": env.detail if env else "",
            "env_versions": ", ".join(f"{k} {v}" for k, v in env.versions.items()) if env and env.versions else "",
            "env_size": plan.ENV_DOWNLOAD_NVIDIA if hw and hw.nvidia else plan.ENV_DOWNLOAD_CPU,
            "root": str(es.ROOT),
            "recommended": plan.model_label(hw.recommended_model) if hw else "",
            "models": plan.setup_rows(es.MODELS, self.models, self.found, hw.recommended_model if hw else ""),
            "tmodels": plan.setup_rows(es.TRANSLATION_MODELS, self.tmodels, self.tfound, es.RECOMMENDED_TRANSLATION),
            "tr_recommended": plan.model_label(es.RECOMMENDED_TRANSLATION),
            "busy": busy,
            "ready": self.ready,
        })

    def _model_options(self):
        return plan.model_options(self.models, self.hw.recommended_model if self.hw else "",
                                  self.settings.get("model"), resolve=self.own["available"])

    def _push_options(self):
        s = self.settings
        options, chosen = self._model_options()
        language = s.get("language", "")
        mixed = list(s.get("mixed_languages") or [])
        if language == plan.MIXED and len(mixed) < 2:
            language = ""
        why = ""
        if self.ready:
            pass
        elif self.env is None:
            why = "Checking the transcription engine…"
        elif not self.env.ready:
            why = "Transcription isn't set up on this computer yet – it takes a one-time install of the engine and a model."
        elif not self.models:
            why = "The engine is installed. Get a model on the Setup tab to start."
        own = chosen == plan.RESOLVE_ID
        self.emit("options", {
            "models": options, "model": chosen,
            "language": language, "mixed": mixed, "mixed_label": plan.mixed_label(mixed),
            "hotwords": s.get("hotwords", ""),
            "max_chars": int(s.get("max_chars", 42)), "max_lines": int(s.get("max_lines", 2)),
            "ready": self.ready, "why": why,
            "where": ("GPU" if self.hw and self.hw.nvidia else "CPU") if self.hw else "",
            "resolve": own, "speaker_names": bool(s.get("speaker_names", True)),
            "place": self._placing_on(),
            "run_note": self._run_note(own),
        })

    def _placing_on(self):
        return bool(self.settings.get("place_on_timeline", True))

    def _run_note(self, own):
        if self._placing_on():
            return ("Resolve transcribes the timeline itself, telling the speakers apart, and the subtitles "
                    "go on subtitle track 1." if own else
                    "Renders the timeline's audio, transcribes it on this computer, and puts the subtitles "
                    "on subtitle track 1.")
        return ("Resolve transcribes the timeline itself, telling the speakers apart, and the subtitles are "
                "saved as an SRT file." if own else
                "Renders the timeline's audio, transcribes it on this computer, and saves the subtitles as "
                "an SRT file.")

    def _push_translate(self):
        s = self.settings
        options, chosen = plan.translation_options(self.tmodels, self._ai_label(), s.get("translate_model"))
        env_ready = bool(self.env and self.env.ready)
        if chosen == plan.AI_ID:
            problem = self._ai_client().validate()
            ready = not problem
            status = (f"AI translation uses Ask Buddy's model, and it isn't set up yet: {problem}" if problem else
                      f"{self._ai_name()} translates whole passages at once, so names and tone stay consistent. "
                      "The transcript's text is sent to it"
                      + (" – locally, so it stays on your computer (or your own network)." if self._ai_is_local() else "."))
        else:
            ready = env_ready and bool(self.tmodels)
            if not env_ready:
                status = "Set up the transcription engine first to translate on this computer – or choose AI translation."
            elif not self.tmodels:
                status = ("Translating on this computer needs a model (NLLB-200 or MADLAD-400) – get one on the "
                          "Setup tab. Or choose AI translation.")
            elif es.translation_family(chosen) == "madlad":
                status = "MADLAD-400 translates into about 180 languages, on this computer. Free for commercial use."
            else:
                status = "NLLB-200 translates into any of 200 languages, on this computer. Free for non-commercial use."
        last = s.get("last_transcript") or {}
        has_last = bool(last.get("transcript")) and Path(last.get("transcript", "")).is_file()
        self.emit("translate", {
            "engines": options, "engine": chosen, "status": status, "ready": ready,
            "targets": [{"code": c, "name": L.name_of(c)} for c in s.get("translate_targets", []) or []],
            "folder": str(Path(self._output_dir())),
            "auto": bool(s.get("translate_auto", False)),
            "timeline": bool(s.get("translate_timeline", True)),
            "last": last.get("name") if has_last else "",
            "can_show": bool(self._last_output_dir),
        })

    def _push_job(self):
        self.emit("job", {"kind": self.job_kind if self.job is not None else None,
                          "stage": self.stage, "progress": self.progress})

    def _push_timeline(self):
        self.emit("timeline", {"text": self.timeline_text, "ok": self.timeline_ok,
                               "connected": bool(getattr(self.host, "connected", False))})

    def _push_convert(self):
        self.emit("convert", {"video": self.tracks["video"], "subtitle": self.tracks["subtitle"],
                              "sub_track": self.sub_track, "target_track": self.target_track})

    def _add_log(self, text, kind="info"):
        if not text:
            return
        self._log.append({"time": time.strftime("%H:%M"), "text": text, "kind": kind})
        del self._log[:-LOG_LIMIT]
        self.emit("log", self._log)

    def _alert(self, text, title="Subtitles"):
        self.emit("alert", {"title": title, "text": text})

    # ------------------------------------------------------------- asking --

    def _ask(self, kind, payload, then, cancelled=None):
        ask_id = uuid.uuid4().hex
        self._asks[ask_id] = (kind, payload, then, cancelled)
        self.emit("ask", dict(payload, id=ask_id, kind=kind))

    def on_answer(self, payload):
        payload = payload or {}
        entry = self._asks.pop(payload.get("id"), None)
        if entry is None:
            return
        _kind, _payload, then, cancelled = entry
        if payload.get("ok"):
            then(payload.get("value"))
        elif cancelled:
            cancelled()

    # ------------------------------------------------------------ timeline --

    def _read_timeline(self):
        controller = self.host.controller if getattr(self.host, "connected", False) else None
        available = self.own["available"]
        if controller is None:
            self.timeline_text, self.timeline_ok = "Not connected to Resolve.", False
            self.own = {"available": False, "uid": "", "existing": False}
        else:
            try:
                self.own = TranscribeController(controller).own_transcription()
            except Exception:  # noqa: BLE001 - then it just isn't offered
                self.own = {"available": False, "uid": "", "existing": False}
            try:
                info = TranscribeController(controller).timeline_info()
            except Exception as exc:  # noqa: BLE001 - shown inline
                self.timeline_text, self.timeline_ok = str(exc), False
            else:
                extra = (f" · subtitle track 1 already has {info.subtitle_items_on_track1} subtitle(s)"
                         if info.subtitle_items_on_track1 else "")
                self.timeline_text = (f"{info.name} · {info.duration_seconds / 60:.1f} min · "
                                      f"starts {info.start_timecode}{extra}")
                self.timeline_ok = True
            self._read_tracks(controller)
        self._push_timeline()
        self._push_convert()
        if self.own["available"] != available:   # the Model menu gains or loses Resolve's own
            self._push_options()
            self._push_setup()

    def _read_tracks(self, controller):
        """The timeline's tracks for Subtitle Conversion, and the video track to
        suggest: the topmost empty one - unless the user picked one for this
        timeline (picking track 2 and coming back to the page must not turn it
        into a new top track without a word)."""
        try:
            tracks = TranscribeController(controller).conversion_tracks()
        except Exception:  # noqa: BLE001 - the timeline line above says what's wrong
            return
        if not self._target_chosen or tracks["timeline"] != self.tracks["timeline"]:
            self.target_track, self._target_chosen = tracks["empty"], False
        self.tracks = tracks

    def on_refresh_timeline(self, _payload=None):
        try:
            self.host.ensure_connected()
        except ResolveConnectionError:
            pass
        self._read_timeline()
        self.textplus.on_refresh()

    # ------------------------------------------------------------ options --

    def on_tab(self, payload):
        tab = (payload or {}).get("tab")
        if tab in TABS:
            self.tab, self._chose_tab = tab, True
            if tab in TEXT_PLUS_TABS:
                self.textplus.tab_shown()

    def on_option(self, payload):
        payload = payload or {}
        key, value = payload.get("key"), payload.get("value")
        s = self.settings
        if key == "model":
            s["model"] = str(value or "")
        elif key == "language":
            if value == plan.MIXED and len(s.get("mixed_languages") or []) < 2:
                return self._push_options()
            s["language"] = str(value or "")
        elif key == "hotwords":
            s["hotwords"] = str(value or "").strip()
        elif key == "max_chars":
            try:
                s["max_chars"] = max(1, min(60, int(value)))    # 1: a word per subtitle
            except (TypeError, ValueError):
                pass
        elif key == "max_lines":
            s["max_lines"] = 1 if value in (1, "1") else 2
        elif key == "speaker_names":
            s["speaker_names"] = bool(value)
        elif key == "place_on_timeline":
            s["place_on_timeline"] = bool(value)
        else:
            return
        s.save()
        self._push_options()

    def on_mixed(self, payload):
        codes = [c for c in (payload or {}).get("codes") or [] if c in L.WHISPER_TO_NLLB]
        if len(codes) < 2:
            self._alert("Tick at least two languages for mixed audio – for one, choose it from the menu.")
            return self._push_options()
        self.settings["mixed_languages"] = codes
        self.settings["language"] = plan.MIXED
        self.settings.save()
        self._push_options()

    def _style(self):
        return st.Style(max_chars=int(self.settings.get("max_chars", 42)),
                        max_lines=int(self.settings.get("max_lines", 2)),
                        speaker_names=bool(self.settings.get("speaker_names", True)))

    # --------------------------------------------------------------- jobs --

    def _begin(self, kind, job, on_done):
        self.job, self.job_kind = job, kind
        self.stage, self.progress = "Starting…", 0
        job.stage.connect(self._on_stage)
        job.progress.connect(self._on_progress)
        job.note.connect(self._add_log)
        job.succeeded.connect(on_done)
        job.failed.connect(self._on_failed)
        job.finished.connect(self._on_finished)
        self._push_job()
        self._push_setup()
        job.start()

    def _on_stage(self, text):
        self.stage = text
        self._push_job()

    def _on_progress(self, value):
        self.progress = value
        self._push_job()

    def on_stop(self, _payload=None):
        if self.job is not None:
            self.stage = "Stopping…"
            self._pending_translate = None
            self.job.cancel()
            self._push_job()

    def _on_failed(self, message):
        self._add_log("Didn't finish: " + message, "error")
        self.result = {"ok": False, "kind": self.job_kind, "message": message}
        self._pending_translate = None

    def _on_finished(self):
        self.job, self.job_kind = None, None
        self.stage, self.progress = "", None
        self.emit("result", self.result)
        self._rescan()
        self._read_timeline()
        self._push_all()
        self._maybe_translate_pending()

    # --------------------------------------------------------- transcribe --

    def on_run(self, _payload=None):
        if self.job is not None or self._placing or not self.ready:
            return
        try:
            controller = self.host.ensure_connected()
        except ResolveConnectionError as exc:
            return self._alert(str(exc), "Not connected")
        resolve = TranscribeController(controller)
        try:
            info = resolve.timeline_info()
        except TranscribeResolveError as exc:
            return self._alert(str(exc), "No timeline")
        s = self.settings
        options, model_id = self._model_options()
        language = s.get("language", "")
        mixed = list(s.get("mixed_languages") or []) if language == plan.MIXED else []
        chosen_plan, problem = plan.plan_for(model_id, self.models, language, mixed)
        if problem:
            return self._alert(problem)
        label = next((o["label"] for o in options if o["id"] == model_id), model_id)

        def start(fresh=True):
            if self.job is not None:
                return
            self._add_log(f"Transcribing '{info.name}' ({info.duration_seconds / 60:.1f} min) with {label}…")
            self.result = None
            self._run_timeline = info.name
            self.emit("result", None)
            job_plan = dict(chosen_plan, timeline=self.own.get("uid", ""), fresh=fresh)
            self._begin("transcribe", jobs.TranscribeJob(resolve, info.name, job_plan, chosen_plan["language"],
                                                         s.get("hotwords", ""), self._style(), mixed), self._on_done)

        if chosen_plan["engine"] != "resolve":
            return start()
        try:
            self.own = resolve.own_transcription()
        except (ResolveConnectionError, TranscribeResolveError) as exc:
            return self._alert(str(exc))
        if not self.own["available"]:
            self._push_options()
            return self._alert("Resolve's own transcription needs DaVinci Resolve Studio 21.1 or later.")
        if not self.own["uid"]:
            return self._alert("Open a timeline in Resolve first.", "No timeline")
        if self.own["existing"]:
            # Transcribing again replaces what Resolve has - speaker names given there included.
            return self._ask("existing", {"name": info.name}, lambda choice: start(choice == "again"))
        start()

    def _on_done(self, result):
        self.last_srt = result["srt"]
        engine = "Parakeet" if result.get("engine") == "parakeet" else "Whisper"
        heard = result.get("languages") or {}
        spoken = (" + ".join(plan.spoken_name(c) for c in heard) if len(heard) > 1
                  else plan.language_name(result.get("language") or "") or "language not detected")
        where = "GPU" if result.get("device") == "cuda" else "CPU"
        if result.get("engine") == "resolve":
            speakers = len(result.get("speakers") or [])
            who = f", {speakers} speakers" if speakers > 1 else ""
            summary = (f"{result['cues']} subtitles ({spoken}{who}) – {result['duration'] / 60:.1f} min of audio "
                       f"in {result['seconds'] / 60:.1f} min, transcribed by Resolve.")
        else:
            summary = (f"{result['cues']} subtitles ({spoken}) – {result['duration'] / 60:.1f} min of audio in "
                       f"{result['seconds'] / 60:.1f} min with {engine} on the {where}.")
        self._add_log(f"{summary} Saved: {result['srt']}", "ok")
        self.settings["last_transcript"] = {
            "transcript": result["transcript"], "name": result["name"], "srt": result["srt"],
            "language": result.get("language") or "",
            "languages": sorted(heard, key=heard.get, reverse=True) if len(heard) > 1 else []}
        self.settings.save()
        if self.settings.get("translate_auto") and self.settings.get("translate_targets"):
            self._pending_translate = dict(self.settings.get("last_transcript"))
        self.result = {"ok": True, "kind": "transcribe", "srt": result["srt"], "summary": summary, "placed": 0,
                       "timeline": self._run_timeline, "placing": True,
                       "message": "Adding the subtitles to the timeline…"}
        if not self._placing_on():
            return self._placed(0, "Done – saved as an SRT file.")
        self._placing = True
        self._place(result["srt"])

    def on_place_last(self, _payload=None):
        """The result's "Add to timeline": the transcript that wasn't placed, onto the
        timeline it was made from - not whichever one is open now."""
        r = self.result
        if (self.job is not None or self._placing or not r or r.get("kind") != "transcribe"
                or r.get("placed") or not Path(r.get("srt") or "").is_file()):
            return
        try:
            info = TranscribeController(self.host.ensure_connected()).timeline_info()
        except (ResolveConnectionError, TranscribeResolveError) as exc:
            return self._alert(str(exc), "Not connected")
        if r.get("timeline") and info.name != r["timeline"]:
            return self._alert(f"These subtitles are from '{r['timeline']}'. Open it in Resolve to add them "
                               f"there – '{info.name}' is open now.", "Another timeline is open")
        self._placing = True
        r.update(placing=True, message="Adding the subtitles to the timeline…")
        self.emit("result", r)
        self._place(r["srt"])

    def _place(self, srt_path):
        """Subtitle track 1, asking first if that would replace subtitles
        already there."""
        try:
            resolve = TranscribeController(self.host.ensure_connected())
            existing = resolve.timeline_info().subtitle_items_on_track1
        except (ResolveConnectionError, TranscribeResolveError) as exc:
            self._add_log(f"Couldn't add to the timeline: {exc}", "error")
            return self._placed(0)
        if not existing:
            return self._placed(self._place_now(resolve, srt_path, False))
        self._ask("replace", {"count": existing},
                  lambda _v: self._placed(self._place_now(resolve, srt_path, True)),
                  lambda: self._placed(0))

    def _place_now(self, resolve, srt_path, replace):
        self.host.set_busy(True, "Adding subtitles to the timeline…")
        try:
            return resolve.place_subtitles(srt_path, replace_existing=replace)
        except TranscribeResolveError as exc:
            self._add_log(f"Couldn't add to the timeline: {exc}", "error")
            return 0
        finally:
            self.host.set_busy(False)

    def _placed(self, count, unplaced="Done – saved as an SRT file (not added to the timeline)."):
        self._placing = False
        if self.result:
            self.result.update(placed=count, placing=False)
            self.result["message"] = f"Done – {count} subtitles on subtitle track 1." if count else unplaced
        self.emit("result", self.result)
        self._read_timeline()
        self._maybe_translate_pending()

    def on_save_srt(self, _payload=None):
        if not self.last_srt or not Path(self.last_srt).is_file():
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save subtitles"), Path(self.last_srt).name,
                                              tr_filter("SubRip subtitles (*.srt)"))
        if path:
            Path(path).write_text(Path(self.last_srt).read_text(encoding="utf-8"), encoding="utf-8")
            self._add_log(f"Saved a copy to {path}", "ok")
            self.emit("toast", {"text": f"Saved {Path(path).name}"})

    # ------------------------------------------------ subtitle conversion --

    def on_conv_option(self, payload):
        payload = payload or {}
        try:
            value = max(1, min(99, int(payload.get("value") or 1)))
        except (TypeError, ValueError):
            return
        if payload.get("key") == "sub_track":
            self.sub_track = value
        elif payload.get("key") == "target_track":
            self.target_track, self._target_chosen = value, True
        self._push_convert()

    def on_convert(self, _payload=None):
        try:
            resolve = TranscribeController(self.host.ensure_connected())
        except ResolveConnectionError as exc:
            return self._alert(str(exc), "Subtitle conversion")
        sub_track, video_track = self.sub_track, self.target_track

        def log(message):
            message = message.strip().lstrip("- ").strip()
            if message and not message.startswith(("[Diagnostic]", "[Duration]")):
                bad = any(w in message for w in ("Warning", "FAIL", "Exception"))
                self._add_log(message, "error" if bad else "info")

        self.host.set_busy(True, "Converting subtitles to Text+…")
        try:
            made = resolve.subtitles_to_text_plus(sub_track, video_track, log)
        except TranscribeResolveError as exc:
            return self._alert(str(exc), "Subtitle conversion")
        except Exception as exc:  # noqa: BLE001 - Resolve's own errors, said rather than lost
            self._add_log(f"Couldn't convert the subtitles: {exc}", "error")
            return self._alert(f"Couldn't convert the subtitles: {exc}", "Subtitle conversion")
        finally:
            self.host.set_busy(False)
        text = (f"1 Text+ clip on video track {video_track}" if made == 1
                else f"{made} Text+ clips on video track {video_track}")
        self._add_log(text, "ok" if made else "error")
        self.emit("toast", {"text": text, "style": bool(made)})     # offers the Font Styling tab
        self._target_chosen = False      # used: the next conversion gets a fresh empty track
        self._read_timeline()
        self.textplus.on_refresh()       # the Text+ tabs' track lists: maybe a new track

    # ---------------------------------------------------------- translate --

    def _output_dir(self) -> str:
        return self.settings.get("translate_dir") or str(jobs.OUTPUT_DIR)

    def on_tr_option(self, payload):
        payload = payload or {}
        key, value = payload.get("key"), payload.get("value")
        if key == "engine":
            self.settings["translate_model"] = str(value or "")
        elif key in ("auto", "timeline"):
            self.settings[f"translate_{key}"] = bool(value)
        else:
            return
        self.settings.save()
        self._push_translate()

    def on_targets(self, payload):
        codes = [c for c in (payload or {}).get("codes") or [] if c in L.NLLB_LANGUAGES]
        self.settings["translate_targets"] = codes
        self.settings.save()
        self._push_translate()

    def on_choose_folder(self, _payload=None):
        folder = QFileDialog.getExistingDirectory(self, tr("Save translated subtitles to"), self._output_dir())
        if folder:
            self.settings["translate_dir"] = folder
            self.settings.save()
            self._push_translate()

    def on_show_files(self, _payload=None):
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output_dir or self._output_dir()))

    def _maybe_translate_pending(self):
        """The automatic translation, once the transcription has finished
        AND its subtitles are placed (either can come last)."""
        if self._pending_translate is None or self.job is not None or self._placing:
            return
        last, self._pending_translate = self._pending_translate, None
        self._translate_transcript(last, quiet=True)

    # AI (Ask Buddy's model)

    def _chat_settings(self):
        # The same cached settings object Ask Buddy's page edits, so a key
        # entered there counts here straight away.
        from pages.manual_chat.config import DEFAULTS as CHAT_DEFAULTS
        return self.host.tool_settings("manual_chat", dict(CHAT_DEFAULTS))

    def _ai_client(self):
        from pages.manual_chat.config import llm_client_from_settings

        client = llm_client_from_settings(self._chat_settings(), max_tokens=ai.MAX_TOKENS)
        # A batch of 40 sentences takes longer than a chat turn; a local
        # server keeps its own, longer default.
        if not client.local:
            client.timeout = plan.AI_TIMEOUT
        return client

    def _ai_provider(self) -> str:
        return self._chat_settings().get("provider", "") or ""

    def _ai_is_local(self) -> bool:
        """On this PC or the user's own network: the transcript goes to no
        cloud service (Ollama, llama.cpp, LM Studio, a local custom server)."""
        return self._ai_client().local

    def _ai_name(self) -> str:
        client = self._ai_client()
        name = plan.AI_PROVIDER_NAMES.get(client.provider, client.provider)
        return f"{name} ({client.model})" if client.model else name

    def _ai_label(self) -> str:
        if self._ai_client().validate():
            return "AI translation (set up in Ask Buddy)"
        return f"AI translation · {self._ai_name()}"

    def _engine(self, quiet, then):
        """Calls then(engine) with what to translate with - {"id", "dir",
        "family"} for a local model, {"id": AI_ID, "family": "ai"} for Ask
        Buddy's - or says what's missing. A cloud model gets the
        transcript's text: that's OK'd once per provider (remembered), and
        never sent unasked during an automatic run."""
        _options, model_id = plan.translation_options(self.tmodels, self._ai_label(),
                                                      self.settings.get("translate_model"))
        problem = None
        if not self.settings.get("translate_targets"):
            problem = "Choose the languages to translate into first."
        elif model_id == plan.AI_ID:
            problem = self._ai_client().validate()
            if problem:
                problem = f"AI translation uses Ask Buddy's model, which isn't set up: {problem}"
        elif model_id not in self.tmodels:
            problem = "Get a translation model first (on the Setup tab), or choose AI translation."
        if problem:
            self._add_log("Didn't translate: " + problem, "error")
            if not quiet:
                self._alert(problem, "Translate")
            return
        if model_id != plan.AI_ID:
            return then({"id": model_id, "dir": self.tmodels[model_id], "family": es.translation_family(model_id)})
        engine = {"id": plan.AI_ID, "family": "ai"}
        key = f"ai_consent_{self._ai_provider()}"
        if self._ai_is_local() or self.settings.get(key):
            return then(engine)
        if quiet:
            return self._add_log(f"Didn't translate automatically: AI translation sends the transcript to "
                                 f"{self._ai_name()}, and that hasn't been OK'd yet. Translate once by hand "
                                 "to allow it.", "error")

        def allowed(_value):
            self.settings[key] = True
            self.settings.save()
            then(engine)
        self._ask("consent", {"name": self._ai_name()}, allowed)

    def _ask_language(self, prompt, then):
        """An NLLB code picked in the view, remembered for next time."""
        def chosen(code):
            if code in L.NLLB_LANGUAGES:
                self.settings["srt_language"] = code
                self.settings.save()
                then(code)
        self._ask("language", {"prompt": prompt, "value": self.settings.get("srt_language") or "eng_Latn"}, chosen)

    def on_translate_last(self, _payload=None):
        last = self.settings.get("last_transcript") or {}
        if last.get("transcript"):
            self._translate_transcript(last)

    def _translate_transcript(self, last, quiet=False):
        if self.job is not None:
            return
        self._engine(quiet, lambda engine: self._translate_with(last, engine, quiet))

    def _read_transcript(self, last):
        try:
            return json.loads(Path(last["transcript"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as exc:
            self._add_log(f"Couldn't read the transcript: {exc}", "error")
            return None

    def _translate_with(self, last, engine, quiet):
        original = last.get("srt") or str(jobs.OUTPUT_DIR / f"{last.get('name')}.srt")
        name = last.get("name") or "transcript"
        spoken = last.get("languages") or []
        if len(spoken) > 1:
            # Each sentence from the language it was spoken in; one already
            # in a target language stays as it was said.
            missing = [c for c in spoken if c not in L.WHISPER_TO_NLLB]
            if missing:
                msg = f"Buddy can't translate from {', '.join(missing)} – not one of NLLB-200's languages."
                self._add_log("Didn't translate: " + msg, "error")
                return None if quiet else self._alert(msg, "Translate")
            data = self._read_transcript(last)
            if data is None:
                return
            main = L.WHISPER_TO_NLLB[spoken[0]]
            sentences = st.sentences_mixed(data.get("segments", []),
                                           lambda lang: L.WHISPER_TO_NLLB.get(lang, "") in L.NO_SPACE_CODES)
            sources = [L.WHISPER_TO_NLLB.get(s.language, main) for s in sentences]
            label = " and ".join(L.name_of(L.WHISPER_TO_NLLB[c]) for c in spoken)
            return self._run_translation(sentences, main, engine, name, original, sources=sources, label=label)

        def go(source):
            data = self._read_transcript(last)
            if data is None:
                return
            sentences = st.sentences_from_segments(data.get("segments", []), cjk=source in L.NO_SPACE_CODES)
            self._run_translation(sentences, source, engine, name, original)

        source = L.WHISPER_TO_NLLB.get(last.get("language") or "")
        if source is None and not last.get("language"):
            # Parakeet on its own doesn't say which language it heard.
            if quiet:
                return self._add_log("Didn't translate automatically: the spoken language wasn't detected "
                                     "(install a Whisper model to detect it). Use Translate last transcript.",
                                     "error")
            return self._ask_language("Which language is the transcript in?", go)
        if source is None:
            msg = (f"Buddy can't translate from '{last.get('language') or 'unknown'}' – "
                   "it isn't one of NLLB-200's languages.")
            self._add_log("Didn't translate: " + msg, "error")
            return None if quiet else self._alert(msg, "Translate")
        go(source)

    def on_translate_srt(self, _payload=None):
        if self.job is not None:
            return

        def with_engine(engine):
            path, _ = QFileDialog.getOpenFileName(self, tr("Translate subtitles"), self._output_dir(),
                                                  tr_filter("SubRip subtitles (*.srt)"))
            if not path:
                return
            try:
                segments = st.parse_srt(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
            except OSError as exc:
                return self._alert(f"Couldn't read {path}:\n{exc}", "Translate")
            if not segments:
                return self._alert("That file has no subtitles in it.", "Translate")

            # An SRT doesn't say what language it's in - ask, remembering the answer.
            def go(source):
                sentences = st.sentences_from_segments(segments, cjk=source in L.NO_SPACE_CODES)
                self._run_translation(sentences, source, engine, Path(path).stem, path)
            self._ask_language(f"The subtitles in {Path(path).name} are in:", go)
        self._engine(False, with_engine)

    def _run_translation(self, sentences, source, engine, base_name, original_srt="", sources=None, label=""):
        """original_srt: the subtitles being translated, for the new
        timeline's first track. sources: an NLLB code per sentence (mixed
        languages - no target is skipped then, since the others still need
        it); label: how to name the source languages."""
        if self.job is not None:
            return
        label = label or L.name_of(source)
        targets, notes = plan.targets_for(self.settings.get("translate_targets", []),
                                          "" if sources else source, engine["family"])
        for note in notes:
            self._add_log(note)
        if not targets:
            return self._add_log("Nothing to translate into.")
        if not sentences:
            return self._add_log("Nothing to translate – the transcript is empty.")
        names = ", ".join(L.name_of(c) for c in targets)
        if engine["family"] == "ai":
            how = self._ai_name()
        else:
            how = plan.model_label(engine["id"])
        self._add_log(f"Translating '{base_name}' ({len(sentences)} sentences) from {label} into {names} with {how}…")
        self._last_output_dir = self._output_dir()
        self._translating = {"source": source, "srt": original_srt, "label": label}
        s = self.settings
        max_chars, max_lines = int(s.get("max_chars", 42)), int(s.get("max_lines", 2))
        if engine["family"] == "ai":
            job = jobs.AITranslateJob(self._ai_client(), self._ai_name(), sentences, label if sources else source,
                                      targets, s.get("hotwords", ""), max_chars, max_lines, base_name,
                                      self._last_output_dir)
        else:
            job = jobs.TranslateJob(sentences, source, targets, engine["dir"], max_chars, max_lines, base_name,
                                    self._last_output_dir, family=engine["family"], sources=sources)
        self.result = None
        self._begin("translate", job, self._on_translated)

    def _on_translated(self, result):
        files = result["files"]
        for f in files:
            self._add_log(f"{f['language']}: {f['cues']} subtitles -> {f['path']}", "ok")
        message = f"Translated into {len(files)} language(s) in {result['seconds']:.0f} s {result.get('where', '')}."
        self.result = {"ok": True, "kind": "translate", "translated": [{"language": f["language"], "cues": f["cues"], "path": f["path"]}
                                                  for f in files], "message": message}
        if self.settings.get("translate_timeline", True):
            self._make_subtitle_timeline(files)

    def _make_subtitle_timeline(self, files):
        """The new timeline: a copy of the current one with the original
        language on subtitle track 1 and a track per translation."""
        tracks = []
        source = self._translating or {}
        try:
            text = Path(source.get("srt") or "").read_text(encoding="utf-8-sig", errors="replace")
            original = [(c["start"], c["end"], c["text"]) for c in st.parse_srt(text, keep_lines=True)]
        except OSError:
            original = []
        if original:
            tracks.append(drt.Track(source.get("label") or L.name_of(source.get("source", "")) or "Original", original))
        tracks += [drt.Track(f["language"], f["timed"]) for f in files if f.get("timed")]
        if not tracks:
            return
        self.host.set_busy(True, "Making a timeline with a subtitle track per language…")
        try:
            resolve = TranscribeController(self.host.ensure_connected())
            name, counts = resolve.timeline_with_subtitles(tracks, str(jobs.RENDER_DIR))
        except (ResolveConnectionError, TranscribeResolveError) as exc:
            self._add_log(f"Couldn't make the timeline with subtitle tracks: {exc} The SRT files are saved.", "error")
            return
        finally:
            self.host.set_busy(False)
        each = ", ".join(f"{t.name} {n}" for t, n in zip(tracks, counts))
        self._add_log(f"New timeline '{name}' – subtitle tracks: {each}.", "ok")
        if self.result:
            self.result["timeline"] = name
            self.result["message"] += f" '{name}' has a subtitle track for each language."

    # --------------------------------------------------------------- setup --

    def _setup(self, fn, *args, done_message):
        job = jobs.SetupJob(fn, *args)
        job.line.connect(self._on_setup_line)
        # A bound method, not a lambda: queued onto this thread.
        self._setup_message = done_message
        job.succeeded.connect(self._setup_done)
        self.job, self.job_kind = job, "setup"
        self.stage, self.progress = "Working…", None
        job.failed.connect(self._on_failed)
        job.finished.connect(self._on_finished)
        self._push_job()
        self._push_setup()
        job.start()

    def _setup_done(self):
        message = self._setup_message
        self.result = {"ok": True, "kind": "setup", "message": message}
        self._add_log(message, "ok")
        self.emit("toast", {"text": message})

    def _on_setup_line(self, text):
        parsed = plan.setup_line(text)
        if parsed is None:
            return
        if parsed[0] == "progress":
            self.progress, self.stage = int(100 * parsed[1]), parsed[2]
        else:
            self._add_log(parsed[1])
            self.stage = parsed[1][:110]
        self._push_job()

    def on_install_env(self, _payload=None):
        self._setup(es.install_environment, done_message="Engine installed.")

    def on_download(self, payload):
        model_id = (payload or {}).get("id")
        if model_id in es.MODEL_IDS + es.TRANSLATION_IDS:
            self._setup(es.download_model, model_id, done_message=f"{plan.model_label(model_id)} downloaded.")

    def on_use_copy(self, payload):
        model_id, path = (payload or {}).get("id"), (payload or {}).get("path")
        known = {**self.found, **self.tfound}
        if model_id in known and known[model_id] == path:
            self._use_path(model_id, path)

    def _use_path(self, model_id, path):
        extra = dict(self.settings.get("extra_models", {}) or {})
        extra[model_id] = path
        self.settings["extra_models"] = extra
        self.settings.save()
        self._add_log(f"Using the existing {plan.model_label(model_id)} at {path}.", "ok")
        self._rescan()

    def on_pick_model_folder(self, payload):
        translation = (payload or {}).get("kind") == "translation"
        if translation:
            folder = QFileDialog.getExistingDirectory(
                self, tr("Choose an NLLB-200 or MADLAD-400 model folder (it contains model.bin and tokenizer.json)"))
        else:
            folder = QFileDialog.getExistingDirectory(self, tr("Choose a faster-whisper model folder (it contains model.bin)"))
        if not folder:
            return
        if translation:
            model_id = es.identify_translation_folder(folder)
            problem = ("That folder doesn't look like a CTranslate2 NLLB-200 or MADLAD-400 3B model – it should "
                       "contain model.bin, tokenizer.json and shared_vocabulary.json.")
        else:
            model_id = es.identify_model_folder(folder)
            problem = ("That folder doesn't look like a faster-whisper (CTranslate2) model – it should contain "
                       "model.bin and config.json.")
        if model_id is None:
            return self._alert(problem, "Not a model folder")
        self._use_path(model_id, folder)

    def on_rescan(self, _payload=None):
        self._rescan()
