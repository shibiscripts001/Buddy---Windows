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

Semantic search picks what embeds the manual's passages and the questions
(embedder.py). Set up downloads Buddy's own - llama.cpp and EmbeddingGemma,
each checked against its pinned checksum, llama.cpp scanned by Windows
Defender before it's used (local_llama.py) - after saying what it fetches,
from where, and EmbeddingGemma's licence. Its Test checks the vectors
against the manual's index, not just that something answers.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from core import secrets_store
from core import settings_form as sf
from core.recycle import to_recycle_bin
from core.i18n import tr
from core.message_dialog import confirm
from core.write_consent import WriteConsentDialog

from . import embedder as emb
from . import local_llama
from .agent import DEFAULT_MAX_STEPS, MAX_MAX_STEPS
from .ask_folder import (
    ASK_BUDDY_DIR,
    MAX_INSTRUCTION_CHARS,
    ensure_folder,
    instructions_path,
    read_instructions,
    write_instructions,
)
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
from .retrieval import check_embedder

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


class _EmbedJob(QThread):
    """Set up or Test for semantic search, off the UI thread: progress
    as it goes, then (ok, message)."""

    progressed = Signal(str)
    done = Signal(str, bool, str)       # kind, ok, message

    def __init__(self, kind, work):
        super().__init__()
        self.kind, self.work = kind, work
        self.cancel = False

    def run(self):
        try:
            ok, message = self.work(lambda text: self.progressed.emit(text), lambda: self.cancel)
        except local_llama.Cancelled:
            ok, message = False, "Set up was cancelled – nothing was kept."
        except local_llama.LlamaError as exc:
            ok, message = False, str(exc)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self.done.emit(self.kind, ok, message)


def _set_up(want_runtime, want_model):
    """Set up's work: llama.cpp, then EmbeddingGemma, whichever is missing."""
    def work(say, cancelled):
        note = ""
        progress = lambda done, total, text: say(text)    # noqa: E731
        if want_runtime:
            _exe, note = local_llama.install_runtime(progress, cancelled)
        if want_model:
            model = local_llama.EMBEDDING_GEMMA
            local_llama.download(model, local_llama.ensure_models_dir() / model.name, progress, cancelled)
        done = "Semantic search is set up – Buddy's own EmbeddingGemma runs on this computer."
        return True, f"{done} {note}" if note else done
    return work


def _size_text(size):
    return f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{max(1, round(size / 1e6))} MB"


class ChatSettingsMixin:
    # Where custom instructions live (ask_folder.py); tests point it elsewhere.
    ask_folder = ASK_BUDDY_DIR

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

    def _chat_fields(self):
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
            sf.heading("AI provider"),
            sf.select("provider", "Provider", provider, [(pid, s["label"]) for pid, s in PROVIDER_SPEC.items()]),
            # Saved when it's left, not per keystroke: each change of address
            # asks the server there for its models.
            sf.text("base_url", spec.get("base_label", "Base URL"), self.settings.get(spec["base_url_key"], ""),
                    placeholder=spec.get("base_placeholder", "")) if spec["base_url_key"] else None,
            sf.text("model", spec.get("model_label", "Model"), self.settings.get(spec["model_key"], ""),
                    placeholder=spec["model_placeholder"], live=True, suggest=models),
            sf.hint(list_error, tone="danger") if list_error else None,
            sf.text("api_key", spec.get("key_label", "API key"), secrets_store.unlock(self.settings.get(spec["api_key_key"], "")),
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
            sf.check("allow_project_reads", "Let Ask Buddy send my project's details to my AI provider",
                     self.project_reads_allowed(),
                     hint_text="Ask Buddy reads your project's settings, timeline, clip and marker names to answer "
                               "questions about it, and a cloud provider receives what it reads. Off, it answers "
                               "from the manual only. A server on your own PC or network needs no permission.")
            if not llm_client_from_settings(self.settings).local else None,
            sf.check("allow_project_writes", "Allow Buddy to make changes to my project",
                     self.settings.get("allow_project_writes", False),
                     hint_text="Off by default. Turning it on asks you to type a sentence confirming you understand "
                               "the risk, every time – unticking this box revokes consent, and re-ticking it asks "
                               "again. Buddy always shows you exactly what it wants to change and waits for you to "
                               "press Apply."),
        ]

    def settings_fields(self):
        return [*self._chat_fields(), *self._instruction_fields(), *self._semantic_fields(), *self._manual_fields()]

    def _manual_fields(self):
        return [
            sf.heading("Manual data"),
            sf.hint(self.manual_summary_text()),
            sf.buttons(("Rebuild from PDF…", "rebuild_manual")),
        ]

    def settings_pages(self):
        """Settings' AI group has the chat model and the manual search;
        Tools > Ask Buddy points there."""
        return [
            sf.page("ai_chat", "ai", "Chat assistant", [*self._chat_fields(), *self._instruction_fields()],
                    "The model Ask Buddy answers with – and Subtitles' AI translation."),
            sf.page("ai_search", "ai", "Manual search", [*self._semantic_fields(), *self._manual_fields()],
                    "How Ask Buddy finds the right pages of the manual for a question."),
            sf.page(self.tool_id, "tools", self.display_name, [
                sf.hint("Ask Buddy's settings are with the rest of Buddy's AI."),
                sf.link("Chat assistant", "ai_chat", "The provider and model, memory, custom instructions and "
                                                     "project changes."),
                sf.link("Manual search", "ai_search", "Semantic search and the manual's data."),
            ]),
        ]

    # ------------------------------------------------ the Model library --

    def project_reads_allowed(self, llm=None) -> bool:
        """What Ask Buddy reads of the open project (names, clips, markers,
        settings) goes to the AI provider with the question. A server on this
        PC or the user's network needs no leave; a cloud one needs theirs, for
        that address."""
        llm = llm or llm_client_from_settings(self.settings)
        return llm.local or self.settings.get("project_read_consent") == llm.destination

    def ai_models(self):
        """The chat model, the embedding models on disk (or the one to get)
        and llama.cpp - rows for Settings' Model library."""
        client = llm_client_from_settings(self.settings)
        problem = client.validate()
        if problem:
            chat_chip, where = {"text": "Not set up", "tone": "warn", "tip": problem}, "missing"
        elif client.local:
            chat_chip, where = {"text": "Your server", "tone": "ok", "tip": client.base_url}, "local"
        else:
            chat_chip, where = {"text": "Cloud", "tone": "cloud", "tip": f"Sends your questions to {client.label}."}, "cloud"
        rows = [{
            "id": "chat", "label": client.model or client.label, "raw": True, "kind": "Chat",
            "sub": ["Chat", "Ask Buddy", "Subtitles' AI translation", client.label],
            "chip": chat_chip, "where": where, "actions": [{"label": "Change", "page": "ai_chat"}],
        }]
        backend = self.settings.get("embed_backend") or emb.BACKEND_AUTO
        chosen = emb.chosen_model_file(self.settings)
        busy = getattr(self, "_embed_job", None) is not None
        files = local_llama.model_files()
        for path in files:
            pinned = path.name == local_llama.EMBEDDING_GEMMA.name
            verified = pinned and local_llama.is_pinned_copy(path)
            in_use = backend in (emb.BACKEND_AUTO, emb.BACKEND_BUDDY) and chosen == path
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            rows.append({
                "id": f"gguf:{path.name}", "label": "EmbeddingGemma" if pinned else path.name, "raw": True,
                "kind": "Search", "sub": ["Search", "Ask Buddy", *(["in use"] if in_use else [])],
                "size": _size_text(size), "bytes": size, "where": "local", "note": str(path),
                "chip": ({"text": "Verified", "tone": "ok", "tip": "Exactly the file Buddy downloads, checksum and all."}
                         if verified else
                         {"text": "Not verified", "tone": "warn",
                          "tip": "Not the file Buddy downloads – a GGUF holds no code, but use ones from sources you trust."}),
                "actions": [{"label": "Remove", "action": f"embed_remove:{path.name}", "disabled": busy,
                             "tip": "Moves the file to the Recycle Bin."}],
            })
        if not any(p.name == local_llama.EMBEDDING_GEMMA.name for p in files):
            rows.append({
                "id": "gguf:embeddinggemma", "label": "EmbeddingGemma", "raw": True, "kind": "Search",
                "sub": ["Search", "Ask Buddy"],
                "size": f"{local_llama.EMBEDDING_GEMMA.mb} MB", "where": "missing",
                "chip": {"text": "Not downloaded", "tone": ""},
                "actions": [{"label": "Setting up…" if busy else "Set up", "action": "embed_setup", "disabled": busy}],
            })
        runtime = local_llama.server_exe()
        build = local_llama.runtime_download()
        if runtime is not None or build is not None:
            size = local_llama.folder_size(local_llama.RUNTIME_DIR) if runtime else 0
            rows.append({
                "id": "llama.cpp", "label": "llama.cpp", "raw": True, "kind": "Engine",
                "sub": ["Engine", "runs Ask Buddy's search model", f"release {local_llama.RELEASE}"],
                "size": _size_text(size) if runtime else f"{build.mb} MB", "bytes": size,
                "where": "local" if runtime else "missing", "note": str(local_llama.RUNTIME_DIR) if runtime else "",
                "chip": ({"text": "Installed", "tone": "ok",
                          "tip": "Checked against its published checksum and scanned by Windows Defender when it was "
                                 "downloaded."} if runtime else {"text": "Not downloaded", "tone": ""}),
                "actions": [] if runtime else [{"label": "Set up", "action": "embed_setup", "disabled": busy}],
            })
        return rows

    def ai_jobs(self):
        """Where Ask Buddy's questions go, for Settings' Privacy page."""
        client = llm_client_from_settings(self.settings)
        if client.validate():
            chat = {"where": "off", "text": "Not set up"}
        elif client.local:
            chat = {"where": "local", "text": "On this PC or your network"}
        else:
            chat = {"where": "cloud", "text": f"Sends to {client.label}"}
        embedder = emb.from_settings(self.settings)
        if embedder is None:
            search = {"where": "off", "text": "Keyword search only"}
        elif embedder.leaves_machine:
            search = {"where": "cloud", "text": "Sends to another server"}
        else:
            search = {"where": "local", "text": "On this PC"}
        writes = bool(self.settings.get("allow_project_writes", False))
        return [
            dict(chat, label="Chat assistant", page="ai_chat",
                 detail="Your questions, the manual passages found for them, your custom instructions and – when it "
                        "looks – details of your open project."),
            dict(search, label="Manual search", page="ai_search",
                 detail="Each question, to find the manual's passages for it."),
            {"label": "Changes to your project", "page": "ai_chat",
             "text": "Allowed" if writes else "Off", "tone": "warn" if writes else "ok",
             "detail": "Buddy shows exactly what it wants to change and waits for you to press Apply."
                       if writes else "Ask Buddy reads your project but never changes it."},
        ]

    # ------------------------------------------------- semantic search --

    def _semantic_fields(self):
        backend = self.settings.get("embed_backend") or emb.BACKEND_AUTO
        job = getattr(self, "_embed_job", None)
        result = getattr(self, "_embed_result", None)
        fields = [
            sf.heading("Semantic search"),
            sf.select("embed_backend", "Embedding model", backend, emb.BACKENDS),
        ]
        if backend in (emb.BACKEND_AUTO, emb.BACKEND_BUDDY):
            fields += self._buddy_embed_fields(backend, job)
        elif backend == emb.BACKEND_OLLAMA:
            fields.append(sf.hint("Ollama on this PC, with embeddinggemma pulled (ollama pull embeddinggemma)."))
        elif backend == emb.BACKEND_SERVER:
            server = emb.ServerEmbedder(self.settings.get("embed_base_url") or "",
                                        self.settings.get("embed_model") or "")
            fields += [
                sf.text("embed_base_url", "Server address", self.settings.get("embed_base_url") or "",
                        placeholder="LM Studio on this PC (127.0.0.1:1234)"),
                sf.text("embed_model", "Model", self.settings.get("embed_model") or "",
                        placeholder="its name there, e.g. text-embedding-embeddinggemma-300m"),
                sf.text("embed_api_key", "API key (optional)", secrets_store.unlock(self.settings.get("embed_api_key") or ""),
                        placeholder="only if the server asks for one", password=True),
                sf.hint("LM Studio, a llama-server started with --embedding, or any other OpenAI-compatible "
                        "/embeddings endpoint. It needs to run EmbeddingGemma to match the manual's index."),
                sf.hint("That address isn't on this computer or your local network – your questions are sent "
                        "there to be searched.", tone="danger") if server.leaves_machine else None,
            ]
        else:
            fields.append(sf.hint("Ask Buddy searches the manual by keywords only."))
        if backend != emb.BACKEND_OFF:
            fields.append(sf.buttons(("Testing…" if job and job.kind == "test" else "Test semantic search", "embed_test")))
        if result:
            fields.append(sf.hint(result[1], tone="success" if result[0] else "danger"))
        return [f for f in fields if f]

    def _buddy_embed_fields(self, backend, job):
        runtime, model = local_llama.server_exe(), emb.chosen_model_file(self.settings)
        files = local_llama.model_files()
        fields = []
        if backend == emb.BACKEND_AUTO:
            fields.append(sf.hint("Buddy's own model once it's set up, Ollama until then."))
        if runtime is None or model is None:
            build = local_llama.runtime_download()
            size = (0 if runtime or build is None else build.mb) + (0 if model else local_llama.EMBEDDING_GEMMA.mb)
            label = "Setting up…" if job and job.kind == "setup" else f"Set up – downloads {size} MB"
            fields += [
                sf.hint("Set up downloads llama.cpp, which runs models on this computer, and EmbeddingGemma, the "
                        "model the manual's index is built with – no other app needed."),
                sf.buttons((label, "embed_setup")),
            ]
        if files:
            names = [(f.name, f.name) for f in files]
            fields.append(sf.select("embed_model_file", "Model file", model.name if model else files[0].name,
                                    names, raw=True))
        fields += [
            sf.info("Models folder", str(local_llama.MODELS_DIR), raw=True),
            sf.buttons(("Open folder", "embed_open_folder")),
            sf.hint("To use another embedding model, drop its GGUF file in this folder. Only GGUF files are run – "
                    "they hold no code. Use ones from sources you trust. A model other than EmbeddingGemma needs "
                    "Rebuild from PDF… so the manual's index matches it."),
        ]
        return fields

    def _start_embed_job(self, kind, work, ui):
        if getattr(self, "_embed_job", None) is not None:
            return ui.status("Still busy with the last one…", "")
        self._embed_ui = ui
        self._embed_result = None
        self._embed_job = _EmbedJob(kind, work)
        self._embed_job.progressed.connect(self._embed_progress)
        self._embed_job.done.connect(self._embed_done)
        self._embed_job.start()
        try:
            ui.refresh()
        except RuntimeError:
            pass

    def _embed_progress(self, text):
        ui = getattr(self, "_embed_ui", None)
        try:
            if ui is not None:
                ui.status(text, "")
        except RuntimeError:
            pass

    def _embed_done(self, kind, ok, message):
        job, self._embed_job = self._embed_job, None
        self._ending_embed_job = job                    # kept until its thread has ended
        self._embed_result = (ok, message)
        if kind == "setup" and ok:
            model = emb.chosen_model_file(self.settings)
            if model is not None:
                self._save("embed_model_file", model.name)
            self._reload_manual()
        ui = getattr(self, "_embed_ui", None)
        try:
            if ui is not None:
                ui.status(message, "success" if ok else "danger")
                ui.refresh()
        except RuntimeError:
            pass                        # the Settings window was closed meanwhile

    def _confirm_setup(self, ui, want_runtime, want_model) -> bool:
        """What Set up downloads, from where, under what licence - before
        anything is fetched."""
        # Each part translated on its own: the dialog can't match them joined.
        parts = [tr("Buddy will download:")]
        if want_runtime:
            item = local_llama.runtime_download()
            scanned = ", and scanned by Windows Defender before it's used." if os.name == "nt" else "."
            parts.append(tr(f"• llama.cpp, {item.mb} MB – runs the model on this computer. MIT licence, from the "
                            "llama.cpp project's GitHub releases (github.com/ggml-org/llama.cpp). Checked against its "
                            f"published checksum{scanned}"))
        if want_model:
            parts.append(tr(f"• EmbeddingGemma, {local_llama.EMBEDDING_GEMMA.mb} MB – Google's embedding model, from "
                            "huggingface.co/ggml-org, checked against its published checksum. It's under the Gemma "
                            "Terms of Use (ai.google.dev/gemma/terms) – downloading it means you accept them."))
        parts.append(tr(f"It all goes in {local_llama.ROOT}. Nothing else is installed, and the model only ever runs "
                        "on this computer."))
        return confirm(ui.parent, "Set up semantic search", "\n\n".join(parts), ok="Download and set up")

    def _instruction_fields(self):
        """Custom instructions: the text box edits ask_folder's file, so
        what's shown is always what's on disk - an edit made in a text
        editor shows up the next time Settings draws."""
        text = read_instructions(self.ask_folder)
        size = len(text.strip())
        too_long = None
        if size > MAX_INSTRUCTION_CHARS:
            too_long = (f"Only the first {MAX_INSTRUCTION_CHARS:,} characters are sent with each question; "
                        f"these instructions have {size:,}.")
        return [
            sf.heading("Custom instructions"),
            sf.textarea("instructions", "What Ask Buddy should know about you and your work", text,
                        placeholder="For example:\n- We deliver 4K DCI at 24 fps, ProRes 422 HQ, Rec.709 Gamma 2.4.\n"
                                    "- Clips are named SHOW_EP_SCENE_TAKE.\n- Keep answers short, steps as a list.",
                        hint_text="Sent with every question – your delivery specs, naming conventions, how you like "
                                  "answers. Buddy still cites the manual for how Resolve works, and still asks "
                                  "before changing your project.",
                        error=too_long),
            sf.info("Saved in", instructions_path(self.ask_folder), raw=True),
            sf.buttons(("Open folder", "open_ask_folder")),
        ]

    def on_setting(self, key, value, ui):
        if key == "instructions":
            try:
                write_instructions(str(value or ""), self.ask_folder)
            except OSError as exc:
                ui.status(f"Couldn't save the instructions: {exc}", "danger")
            else:
                ui.status("Custom instructions saved.", "success")
            return
        if key in ("embed_backend", "embed_model_file", "embed_base_url", "embed_model", "embed_api_key"):
            value = str(value or "").strip()
            if key == "embed_backend" and value not in dict(emb.BACKENDS):
                return
            self._save(key, secrets_store.lock(value) if key == "embed_api_key" else value)
            self._embed_result = None
            self._reload_manual()           # the next question searches with it
            return
        spec = provider_spec(self.settings)
        if key in ("provider", "model", "api_key", "base_url", "api_version"):
            self._test_result = None   # a test of something else
        if key == "provider":
            if value in PROVIDER_SPEC:
                self._save("provider", value)
        elif key == "model":
            self._save(spec["model_key"], str(value or "").strip())
        elif key == "api_key" and spec["api_key_key"]:
            self._save(spec["api_key_key"], secrets_store.lock(str(value or "").strip()))
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
        elif key == "allow_project_reads":
            self._save("project_read_consent", llm_client_from_settings(self.settings).destination if value else "")
            self._read_declined = None
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
        elif action == "open_ask_folder":
            try:
                folder = ensure_folder(self.ask_folder)
            except OSError as exc:
                return ui.status(f"Couldn't create the folder: {exc}", "danger")
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        elif action == "embed_setup":
            want_runtime = local_llama.server_exe() is None
            want_model = emb.chosen_model_file(self.settings) is None
            if want_runtime and local_llama.runtime_download() is None:
                return ui.status("There's no llama.cpp build for this computer – use Ollama or another server.",
                                 "danger")
            if (want_runtime or want_model) and self._confirm_setup(ui, want_runtime, want_model):
                self._start_embed_job("setup", _set_up(want_runtime, want_model), ui)
        elif action.startswith("embed_remove:"):
            self._remove_model_file(action.split(":", 1)[1], ui)
        elif action == "embed_open_folder":
            try:
                folder = local_llama.ensure_models_dir()
            except OSError as exc:
                return ui.status(f"Couldn't create the folder: {exc}", "danger")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        elif action == "embed_test":
            embedder = emb.from_settings(self.settings)
            bundle = self.settings.get("bundle_dir") or default_data_paths()[0]
            if embedder is None:
                return ui.status("Semantic search is off.", "")
            ui.status(f"Asking {embedder.label}…", "")
            self._start_embed_job("test", lambda _say, _cancelled: check_embedder(bundle, embedder), ui)
        elif action == "rebuild_manual":
            # Over the Settings window: it's modal, so the build window stacks on it.
            dialog = ManualBuildDialog(ui.parent, rebuild_target(self.settings), emb.from_settings(self.settings))
            dialog.built.connect(self._reload_manual)
            dialog.exec()

    def _remove_model_file(self, name, ui):
        path = next((p for p in local_llama.model_files() if p.name == name), None)
        if path is None:
            return
        if not ui.confirm("Remove the model?", tr(f"{name} goes to the Recycle Bin. Semantic search needs an "
                                                  "embedding model – Set up downloads EmbeddingGemma again."),
                          ok="Remove", danger=True):
            return
        local_llama.stop_all()                  # a running server holds the file open
        try:
            to_recycle_bin([str(path)])
        except OSError as exc:
            return ui.status(f"Couldn't remove {name}: {exc}", "danger")
        if self.settings.get("embed_model_file") == name:
            self._save("embed_model_file", "")
        self._reload_manual()
        ui.status(f"{name} moved to the Recycle Bin.", "success")

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
