#!/usr/bin/env python3
"""
Subtitles' pages in the shell's Settings window: AI > Transcription and
AI > Translation - the engine and the same model lists as the Setup tab,
from the same rows (plan.setup_rows), doing the same things through the
same handlers - and its rows of the Model library. Kept as a mixin, apart
from the web page; the page provides the probe's results (self.env,
self.models, self.found, ...), the running job and _setup().

The window redraws itself while a download runs: _push_setup / _push_job
call core.settings_dialog.refresh_open.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog

from core import settings_form as sf
from core.i18n import tr
from core.recycle import to_recycle_bin

from . import env_setup as es
from . import plan

USED_BY = "Subtitles, Dailies"


def _size_text(size):
    return f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{max(1, round(size / 1e6))} MB"


class TranscribeSettingsMixin:
    # ------------------------------------------------------------- rows --

    def _model_rows(self, catalog, installed, found, recommended, kind):
        """The Setup tab's rows (transcribe.js modelRow), as Settings' models rows."""
        busy, env_ready = self.job is not None, bool(self.env and self.env.ready)
        sizes = getattr(self, "sizes", {}) or {}
        rows = []
        for m in plan.setup_rows(catalog, installed, found, recommended, self.verified):
            where = installed.get(m["id"]) or m["found"]
            chip = ({"text": "Verified", "tone": "ok", "tip": "Every file matches the checksum Buddy expects for this model."}
                    if m["verified"] is True else
                    {"text": "Not verified", "tone": "warn",
                     "tip": "These files aren't exactly the ones Buddy expects for this model – an older download, or a "
                            "copy from somewhere else. Model files hold no code, so it can still be used; re-download "
                            "it for a verified copy."} if m["verified"] is False else
                    {"text": "Installed", "tone": ""} if m["installed"] else
                    {"text": "Not downloaded", "tone": ""})
            download = {"label": f"Download ({m['size']})", "action": f"download:{m['id']}",
                        "disabled": busy or not env_ready, "tip": "" if env_ready else "Install the engine first."}
            actions = []
            if m["installed"]:
                if m["verified"] is False:
                    actions.append(dict(download, label=f"Re-download ({m['size']})",
                                        tip="Downloads a verified copy into Buddy's folder."))
                own = m["where"] == "Buddy's folder"
                actions.append({"label": "Remove", "action": f"remove:{m['id']}", "disabled": busy,
                                "tip": "Moves it to the Recycle Bin." if own
                                       else "Buddy stops using this copy. Its files are left where they are."})
            else:
                if m["found"]:
                    actions.append({"label": "Use this copy", "action": f"use_copy:{m['id']}", "disabled": busy,
                                    "tip": m["found"]})
                actions.append(download)
            size = sizes.get(installed.get(m["id"], ""), 0)
            rows.append({
                "id": m["id"], "label": m["label"], "raw": True, "kind": kind,
                "sub": [kind, USED_BY, *(["Recommended"] if m["recommended"] else [])],
                "fit": m["fit"], "size": _size_text(size) if size else m["size"], "bytes": size,
                "where": "local" if m["installed"] else "missing",
                "note": ("Installed – your copy: " + where if m["installed"] and m["where"] != "Buddy's folder"
                         else "Found: " + where if m["found"] else ""),
                "chip": chip, "actions": actions,
            })
        return rows

    def _speech_rows(self):
        return self._model_rows(es.MODELS, self.models, self.found, self.hw.recommended_model if self.hw else "",
                                "Transcription")

    def _translation_rows(self):
        return self._model_rows(es.TRANSLATION_MODELS, self.tmodels, self.tfound, es.RECOMMENDED_TRANSLATION,
                                "Translation")

    @staticmethod
    def _detailed(rows):
        """On its own page a row says what the model is good for, as the Setup tab does."""
        return [dict(r, sub=[r["fit"], *(["Recommended"] if "Recommended" in r["sub"] else [])]) for r in rows]

    def _engine_row(self):
        env, hw = self.env, self.hw
        busy = self.job is not None
        if env is None:
            chip, label = {"text": "Checking…", "tone": ""}, ""
        elif env.ready and env.verified:
            chip, label = {"text": "Verified", "tone": "ok", "tip": "Installed and verified."}, "Repair engine"
        elif env.ready:
            chip, label = {"text": "Not verified", "tone": "warn", "tip": env.detail}, "Reinstall engine"
        else:
            chip, label = {"text": "Not installed", "tone": ""}, "Install engine"
        size = (getattr(self, "sizes", {}) or {}).get(str(es.VENV_DIR), 0)
        return {
            "id": "engine", "label": "Transcription engine", "kind": "Engine",
            "sub": ["Engine", "faster-whisper, Parakeet", USED_BY],
            "size": _size_text(size) if size else "", "bytes": size,
            "where": "local" if env and env.ready else "missing", "note": str(es.VENV_DIR) if env and env.ready else "",
            "chip": chip,
            "actions": [{"label": label, "action": "install_env", "disabled": busy or env is None}] if label else [],
        }

    # ------------------------------------------------------------ pages --

    def _job_fields(self):
        if self.job is not None and self.job_kind == "setup":
            return [sf.progress(self.stage or "Working…", self.progress, stop="stop")]
        if self.job is not None:
            return [sf.hint("Subtitles is busy – downloads and installs wait until it's done.")]
        r = self.result
        if r and r.get("kind") == "setup":
            return [sf.hint(r.get("message", ""), tone="success" if r.get("ok") else "danger")]
        return []

    def settings_pages(self):
        env, hw = self.env, self.hw
        busy = self.job is not None
        engine = self._engine_row()
        if env is None:
            env_text = "Checking the engine…"
        elif env.ready and env.verified:
            versions = ", ".join(f"{k} {v}" for k, v in env.versions.items()) if env.versions else ""
            env_text = f"Installed and verified ({versions})." if versions else "Installed and verified."
        else:
            env_text = env.detail
        size = plan.ENV_DOWNLOAD_NVIDIA if hw and hw.nvidia else plan.ENV_DOWNLOAD_CPU
        actions = [(a["label"], a["action"]) for a in engine["actions"]] + [
            ("Checking…" if self._probe is not None else "Check again", "rescan")]
        recommended = plan.model_label(hw.recommended_model) if hw else ""
        speech_hint = "Bigger models are more accurate and slower."
        if recommended:
            speech_hint = (f"Bigger models are more accurate and slower. Recommended for this computer: {recommended}, "
                           "plus Parakeet v3 if you work in European languages – with both, Auto uses each for what "
                           "it's best at.")
        return [
            sf.page("ai_transcription", "ai", "Transcription", [
                *self._job_fields(),
                sf.heading("Engine"),
                sf.status("Transcription engine", engine["chip"]["text"], engine["chip"]["tone"], env_text),
                sf.info("This computer", hw.describe() if hw else "Checking…"),
                sf.hint(f"Installs faster-whisper into Buddy's own folder ({es.ROOT}), built from the Python Buddy runs "
                        f"on. Download: {size}. Every package is checked against its published hash, and Windows "
                        "Defender scans the result."),
                sf.buttons(*[(label, action, {"disabled": busy}) for label, action in actions]),
                sf.heading("Models"),
                sf.hint(speech_hint),
                sf.models(self._detailed(self._speech_rows())),
                sf.buttons(("Use a model folder…", "pick:speech",
                            {"tooltip": "A faster-whisper model you already have (it contains model.bin)."})),
            ], "Speech to text for Subtitles and Dailies – it all runs on this computer."),
            sf.page("ai_translation", "ai", "Translation", [
                *self._job_fields(),
                sf.heading("Translation models"),
                sf.hint("Translates subtitles on this computer. Meta's NLLB-200 covers 200 languages, free for "
                        "non-commercial use only (CC-BY-NC 4.0). Google's MADLAD-400 covers about 180 and is free for "
                        "commercial use too (Apache 2.0) – pick it for client work. "
                        f"Recommended: {plan.model_label(es.RECOMMENDED_TRANSLATION)}. AI translation, with Ask "
                        "Buddy's model, needs nothing from here."),
                sf.models(self._detailed(self._translation_rows())),
                sf.buttons(("Use a model folder…", "pick:translation",
                            {"tooltip": "An NLLB-200 or MADLAD-400 model you already have (it contains model.bin "
                                        "and tokenizer.json)."})),
                sf.heading("AI translation"),
                sf.link("Chat assistant", "ai_chat", "Subtitles' AI translation uses the Chat assistant's model."),
            ], "Subtitles in other languages, on this computer or with the Chat assistant's model."),
        ]

    def ai_models(self):
        return [self._engine_row(), *self._speech_rows(), *self._translation_rows()]

    def ai_jobs(self):
        return [
            {"label": "Transcription", "where": "local", "page": "ai_transcription",
             "detail": "Your clips' audio is transcribed on this computer – or by Resolve Studio's own "
                       "transcription, when you pick it."},
            {"label": "Translation", "where": "local", "page": "ai_translation",
             "detail": "Translation models run on this computer. AI translation sends the subtitles to the Chat "
                       "assistant's model instead."},
        ]

    # ---------------------------------------------------------- actions --

    def on_settings_action(self, action, ui):
        name, _, arg = action.partition(":")
        if name == "stop":
            if ui.confirm("Stop?", "Stop this download or install?", ok="Stop", danger=True):
                self.on_stop()
        elif self.job is not None:
            ui.status("Subtitles is busy – try again when it's done.", "")
        elif name == "install_env":
            self.on_install_env()
        elif name == "download":
            self.on_download({"id": arg})
        elif name == "use_copy":
            self.on_use_copy({"id": arg, "path": {**self.found, **self.tfound}.get(arg)})
        elif name == "rescan":
            self._rescan()
        elif name == "pick":
            self._pick_model_folder(arg == "translation", ui.parent, ui.alert)
        elif name == "remove":
            label = plan.model_label(arg)
            if ui.confirm("Remove the model?", self._remove_question(arg), ok="Remove", danger=True):
                problem = self.remove_model(arg)
                if problem:
                    ui.status(problem, "danger")
                else:
                    ui.status(f"{label} removed.", "success")

    def _remove_question(self, model_id):
        label = plan.model_label(model_id)
        where = {**self.models, **self.tmodels}.get(model_id, "")
        if where and Path(where) == es.buddy_model_dir(model_id):
            return tr(f"{label} goes to the Recycle Bin. You can download it again from here.")
        return tr(f"Buddy stops using your copy of {label}. Its files are left where they are.")

    def remove_model(self, model_id) -> str:
        """Takes a model away: Buddy's own download to the Recycle Bin, a
        copy of the user's forgotten (its files left alone). "" or what
        went wrong."""
        where = {**self.models, **self.tmodels}.get(model_id)
        if not where or self.job is not None:
            return ""
        if Path(where) == es.buddy_model_dir(model_id):
            try:
                to_recycle_bin([where])
            except OSError as exc:
                return f"Couldn't remove {plan.model_label(model_id)}: {exc}"
        extra = dict(self.settings.get("extra_models", {}) or {})
        if extra.pop(model_id, None) is not None:
            self.settings["extra_models"] = extra
            self.settings.save()
        if self.settings.get("model") == model_id:
            self.settings["model"] = ""
            self.settings.save()
        self._add_log(f"Removed {plan.model_label(model_id)}.", "ok")
        self._rescan()
        return ""

    def _pick_model_folder(self, translation, parent, alert):
        if translation:
            folder = QFileDialog.getExistingDirectory(
                parent, tr("Choose an NLLB-200 or MADLAD-400 model folder (it contains model.bin and tokenizer.json)"))
        else:
            folder = QFileDialog.getExistingDirectory(parent, tr("Choose a faster-whisper model folder (it contains model.bin)"))
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
            return alert("Not a model folder", problem)
        self._use_path(model_id, folder)
