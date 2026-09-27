#!/usr/bin/env python3
"""
Ask Buddy's section of the shell's Settings window, as fields (see
core/settings_form.py). Kept as a mixin, apart from the web page, so
page.py is only the chat. The page provides self.settings, self.host,
_save(), and the hooks _on_writes_revoked(), _on_history_limit_changed()
and _reload_manual().

Only the fields the selected provider actually uses are shown, each
holding THAT provider's own stored value - switching provider never
overwrites another's model or key.

Test connection sends one tiny request that asks for a tool call (Ask Buddy
works through tools), and Load models reads a cloud provider's model list;
both run on a thread, since a local model can take a while to answer. A
local server's models are listed on their own when the section is drawn.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QThread, Signal

from core import settings_form as sf
from core.write_consent import WriteConsentDialog

from .agent import DEFAULT_MAX_STEPS, MAX_MAX_STEPS
from .bundle_builder import read_bundle_meta
from .bundle_dialog import ManualBuildDialog
from .config import (
    DEFAULT_HISTORY_TURNS,
    MAX_HISTORY_TURNS,
    PROVIDER_SPEC,
    default_data_paths,
    history_limit,
    llm_client_from_settings,
    max_steps_limit,
    provider_spec,
    rebuild_target,
)
from .llm import AZURE_API_VERSION, PROVIDER_GEMINI, LLMError, list_ollama_models, list_openai_models

# How long a server's model list is trusted: the fields are redrawn on
# every change, and asking a server that isn't there can take a moment.
MODEL_LIST_SECONDS = 20
LOCAL_LIST_TIMEOUT = 2


class _LLMJob(QThread):
    """Test connection / Load models, off the UI thread."""

    done = Signal(str, object)   # kind, result

    def __init__(self, kind, work):
        super().__init__()
        self.kind, self.work = kind, work

    def run(self):
        try:
            result = self.work()
        except LLMError as exc:
            result = exc
        except Exception as exc:  # noqa: BLE001 - shown to the user
            result = LLMError(f"{type(exc).__name__}: {exc}")
        self.done.emit(self.kind, result)


class ChatSettingsMixin:
    def manual_summary_text(self) -> str:
        bundle = self.settings.get("bundle_dir") or ""
        if not bundle:
            bundle, _text = default_data_paths()
        if not bundle:
            return "No manual bundle yet – answers are not grounded in the manual."
        meta = read_bundle_meta(bundle)
        source = meta.get("source") or {}
        title = source.get("title") or "DaVinci Resolve Reference Manual"
        built = (meta.get("built_utc") or "")[:10] or "unknown date"
        kind = "semantic + keyword" if meta.get("vector") else "keyword only"
        chunks = meta.get("chunks")
        count = f"{chunks:,} passages, " if isinstance(chunks, int) else ""
        return f"{title} – {count}{kind}, built {built}.\n{bundle}"

    # ------------------------------------------------------ model lists --

    def _list_key(self, provider):
        client = llm_client_from_settings(self.settings)
        return provider, client.base_url, bool(client.api_key)

    def _cached_models(self, provider):
        """(models, error) already fetched for this provider/address, if
        still fresh - fetching first when it's a local server's (auto_list)."""
        spec = PROVIDER_SPEC[provider]
        cache = self.__dict__.setdefault("_model_lists", {})
        key = self._list_key(provider)
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < MODEL_LIST_SECONDS:
            return hit[1], hit[2]
        if not spec.get("auto_list"):
            return (hit[1], hit[2]) if hit else ([], "")
        client = llm_client_from_settings(self.settings)
        if spec["list_models"] == "ollama":
            models = list_ollama_models(client.base_url, timeout=LOCAL_LIST_TIMEOUT)
            error = "" if models else "Ollama isn't answering at that address – type a model name."
        else:
            try:
                models, error = list_openai_models(client.base_url, client.api_key, timeout=LOCAL_LIST_TIMEOUT), ""
            except LLMError:
                models, error = [], f"{client.label} isn't answering at {client.base_url} – is the server running?"
        cache[key] = (time.monotonic(), models, error)
        return models, error

    def _start_job(self, kind, work, ui):
        if getattr(self, "_llm_job", None) is not None:
            return ui.status("Still waiting for the last answer…", "")
        self._job_ui = ui
        self._llm_job = _LLMJob(kind, work)
        self._llm_job.done.connect(self._llm_job_done)
        self._llm_job.finished.connect(self._llm_job_finished)
        self._llm_job.start()

    def _llm_job_finished(self):
        self._llm_job = self._ending_job = None

    def _llm_job_done(self, kind, result):
        """Back on the UI thread (a queued signal to a bound method). The
        job counts as done now, so the redraw below shows it; a reference
        is kept until its thread has really ended."""
        self._ending_job, self._llm_job = getattr(self, "_llm_job", None), None
        ui = getattr(self, "_job_ui", None)
        provider = self.settings.get("provider", PROVIDER_GEMINI)
        if kind == "models":
            models, error = (([], str(result)) if isinstance(result, LLMError)
                             else (result, "" if result else "The server listed no chat models."))
            self.__dict__.setdefault("_model_lists", {})[self._list_key(provider)] = (time.monotonic(), models, error)
            message, tone = (error, "danger") if error else (f"{len(models):,} models listed.", "success")
        else:
            ok, message = (False, str(result)) if isinstance(result, LLMError) else result
            tone = "success" if ok else "danger"
            self._test_result = (provider, message, tone)
        if ui is not None:
            try:
                ui.status(message, tone)
                ui.refresh()
            except RuntimeError:
                pass   # the Settings window was closed meanwhile

    # ---------------------------------------------------------- fields --

    def settings_fields(self):
        provider = self.settings.get("provider", PROVIDER_GEMINI)
        if provider not in PROVIDER_SPEC:
            provider = PROVIDER_GEMINI
        spec = provider_spec(self.settings, provider)
        listed, list_error = self._cached_models(provider) if spec.get("list_models") else ([], "")
        models = listed or spec.get("models") or []
        test = getattr(self, "_test_result", None)
        test = test if test and test[0] == provider else None
        busy = getattr(self, "_llm_job", None) is not None
        buttons = [("Testing…" if busy else "Test connection", "test_connection")]
        if spec.get("list_models") and not spec.get("auto_list"):
            buttons.append(("Load models", "load_models"))
        history, steps = history_limit(self.settings), max_steps_limit(self.settings)
        return [
            sf.heading("Ask Buddy – AI provider"),
            sf.select("provider", "Provider", provider, [(pid, s["label"]) for pid, s in PROVIDER_SPEC.items()]),
            # Saved when it's left, not per keystroke: each change of address
            # asks the server there for its models.
            sf.text("base_url", spec.get("base_label", "Base URL"), self.settings.get(spec["base_url_key"], ""),
                    placeholder=spec.get("base_placeholder", "")) if spec["base_url_key"] else None,
            sf.text("model", spec.get("model_label", "Model"), self.settings.get(spec["model_key"], ""),
                    placeholder=spec["model_placeholder"], live=True, suggest=models),
            sf.hint(list_error, tone="danger") if list_error else None,
            sf.text("api_key", spec.get("key_label", "API key"), self.settings.get(spec["api_key_key"], ""),
                    placeholder="Paste your API key", password=True, live=True) if spec["api_key_key"] else None,
            sf.text("api_version", "API version", self.settings.get(spec["api_version_key"], ""),
                    placeholder=AZURE_API_VERSION) if spec.get("api_version_key") else None,
            sf.hint("", html=spec["note"]),   # Buddy's own text, with links
            sf.buttons(*buttons),
            sf.hint(test[1], tone=test[2]) if test else None,
            sf.slider("history_turns", "Conversation memory", history, 0, MAX_HISTORY_TURNS, DEFAULT_HISTORY_TURNS,
                      {n: "Off" if not n else "1 turn" if n == 1 else f"{n} turns" for n in range(MAX_HISTORY_TURNS + 1)},
                      hint_text="Past question/answer pairs replayed to the model each request. 0 means every "
                                "question is standalone."),
            sf.slider("max_steps", "Tool call budget", steps, DEFAULT_MAX_STEPS, MAX_MAX_STEPS, DEFAULT_MAX_STEPS,
                      {n: "1 call" if n == 1 else f"{n} calls" for n in range(DEFAULT_MAX_STEPS, MAX_MAX_STEPS + 1)},
                      hint_text="How many tool calls Buddy may make per answer before it gives up – manual "
                                "searches, reading your project, offering tools. More calls let it dig deeper on "
                                "hard questions, but each one is an extra model round-trip that uses more tokens."),
            sf.heading("Project changes"),
            sf.check("allow_project_writes", "Allow Buddy to make changes to my project",
                     self.settings.get("allow_project_writes", False),
                     hint_text="Off by default. Turning it on asks you to type a sentence confirming you understand "
                               "the risk, every time – unticking this box revokes consent, and re-ticking it asks "
                               "again. Buddy always shows you exactly what it wants to change and waits for you to "
                               "press Apply."),
            sf.heading("Manual data"),
            sf.hint(self.manual_summary_text()),
            sf.buttons(("Rebuild from PDF…", "rebuild_manual")),
        ]

    def on_setting(self, key, value, ui):
        spec = provider_spec(self.settings)
        if key in ("provider", "model", "api_key", "base_url", "api_version"):
            self._test_result = None   # a test of something else
        if key == "provider":
            if value in PROVIDER_SPEC:
                self._save("provider", value)
        elif key == "model":
            self._save(spec["model_key"], str(value or "").strip())
        elif key == "api_key" and spec["api_key_key"]:
            self._save(spec["api_key_key"], str(value or "").strip())
        elif key == "base_url" and spec["base_url_key"]:
            self._save(spec["base_url_key"], str(value or "").strip())
        elif key == "api_version" and spec.get("api_version_key"):
            self._save(spec["api_version_key"], str(value or "").strip())
        elif key == "history_turns" and isinstance(value, int) and 0 <= value <= MAX_HISTORY_TURNS:
            self._save("history_turns", value)
            # Now rather than at the next answer, so lowering the limit
            # mid-conversation does what the user just asked for.
            self._on_history_limit_changed()
        elif key == "max_steps" and isinstance(value, int) and DEFAULT_MAX_STEPS <= value <= MAX_MAX_STEPS:
            self._save("max_steps", value)
        elif key == "allow_project_writes":
            self._on_writes_toggled(bool(value), ui)

    def on_settings_action(self, action, ui):
        if action == "test_connection":
            client = llm_client_from_settings(self.settings)
            ui.status(f"Asking {client.label}…", "")
            self._start_job("test", client.test, ui)
        elif action == "load_models":
            client = llm_client_from_settings(self.settings)
            if client.key_required and not client.api_key:
                return ui.status("Add the API key first – the model list needs it.", "danger")
            self._start_job("models", lambda: list_openai_models(client.base_url, client.api_key), ui)
        elif action == "rebuild_manual":
            # Over the Settings window: it's modal, so the build window stacks on it.
            dialog = ManualBuildDialog(ui.parent, rebuild_target(self.settings))
            dialog.built.connect(self._reload_manual)
            dialog.exec()

    def _on_writes_toggled(self, checked, ui):
        """Ticking asks for consent; unticking revokes it immediately.

        Consent is never cached: there is no "already agreed" state, so
        every transition from off to on goes through the window again.
        Revoking needs no confirmation - making it harder to turn OFF a
        dangerous capability would be exactly backwards."""
        if not checked:
            self._save("allow_project_writes", False)
            self._on_writes_revoked()
            return
        self._save("allow_project_writes", bool(WriteConsentDialog.obtain(ui.parent)))
