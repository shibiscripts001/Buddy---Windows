#!/usr/bin/env python3
"""
Ask Buddy's settings: which AI provider, its key and model, and where the
manual data lives. No Qt - Transcribe's AI translation reads the same
settings (one place to set a key), and the tests run on plain Python.

API keys: read from the environment FIRST, settings second. In settings a
key is kept locked to the Windows account (core/secrets_store.py, DPAPI), not
as typed, so a copied or synced settings file - or its .bak - holds nothing
usable; anyone who would rather not have a key on disk at all can export
BUDDY_LLM_API_KEY (or GEMINI_API_KEY) and leave the settings field empty.
"""

from __future__ import annotations

import os
from pathlib import Path

from core import atomic_io, secrets_store

from .agent import DEFAULT_MAX_STEPS, MAX_MAX_STEPS
from .llm import (
    PROVIDER_ANTHROPIC,
    PROVIDER_AZURE,
    PROVIDER_GEMINI,
    PROVIDER_GROQ,
    PROVIDER_LLAMACPP,
    PROVIDER_LMSTUDIO,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI,
    PROVIDER_OPENAI_API,
    PROVIDER_OPENROUTER,
    LLMClient,
)

# How many past question/answer pairs to replay to the model. Each turn is
# re-sent in full on every request, so this trades follow-up awareness
# against tokens. 0 makes every question standalone.
DEFAULT_HISTORY_TURNS = 6
MAX_HISTORY_TURNS = 50

# Everything that differs per provider, in one table so the Settings panel,
# the env-var lookup and the agent builder can't disagree about it. Keys and
# models are stored PER PROVIDER: switching from Gemini to Ollama and back
# must not have silently overwritten the Gemini key with a blank.
#
# Dropdown order: the cloud services, then servers on your own machine or
# network, then any other OpenAI-compatible endpoint. Per provider:
#   api_key_key / key_label    where its key is stored ("" = no key field)
#   base_url_key / base_label  an editable address ("" = fixed, llm.ENDPOINTS)
#   list_models                "ollama" or "openai": Settings can list the
#                              server's models; auto_list does so on its own
#                              (local servers - a cloud list waits for Load models)
#   models                     suggestions offered while nothing's listed
#   api_version_key            Azure's API version
PROVIDER_SPEC = {
    PROVIDER_GEMINI: {
        "label": "Google Gemini",
        "model_key": "model_gemini",
        "api_key_key": "api_key_gemini",
        "base_url_key": "",
        "env_vars": ("BUDDY_LLM_API_KEY", "GEMINI_API_KEY"),
        "model_placeholder": "gemini-3.8-flash",
        "note": (
            'Get a free key at <a href="https://aistudio.google.com/apikey">'
            "aistudio.google.com/apikey</a>. Leave the key empty to use "
            "$GEMINI_API_KEY from your environment instead."
        ),
    },
    PROVIDER_ANTHROPIC: {
        "label": "Anthropic Claude",
        "model_key": "model_anthropic",
        "api_key_key": "api_key_anthropic",
        "base_url_key": "",
        "env_vars": ("BUDDY_LLM_API_KEY", "ANTHROPIC_API_KEY"),
        "model_placeholder": "claude-sonnet-5",
        # Offered in the dropdown, but the field stays editable - a model
        # released after this build still has to be typeable.
        "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
        "note": (
            'Get a key at <a href="https://console.anthropic.com/settings/keys">'
            "console.anthropic.com/settings/keys</a>. Leave the key empty to "
            "use $ANTHROPIC_API_KEY from your environment instead."
        ),
    },
    PROVIDER_OPENAI_API: {
        "label": "OpenAI",
        "model_key": "model_openai_api",
        "api_key_key": "api_key_openai_api",
        "base_url_key": "",
        "env_vars": ("BUDDY_LLM_API_KEY", "OPENAI_API_KEY"),
        "model_placeholder": "gpt-4.1-mini",
        "models": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o4-mini"],
        "list_models": "openai",
        "note": (
            'Get a key at <a href="https://platform.openai.com/api-keys">platform.openai.com/api-keys</a>. '
            "Leave the key empty to use $OPENAI_API_KEY from your environment instead."
        ),
    },
    PROVIDER_OPENROUTER: {
        "label": "OpenRouter",
        "model_key": "model_openrouter",
        "api_key_key": "api_key_openrouter",
        "base_url_key": "",
        "env_vars": ("OPENROUTER_API_KEY",),
        "model_placeholder": "provider/model, e.g. meta-llama/llama-3.3-70b-instruct",
        "list_models": "openai",
        "note": (
            "One key for hundreds of models from every major lab, some of them free. Get one at "
            '<a href="https://openrouter.ai/keys">openrouter.ai/keys</a>, then Load models to pick. '
            "Leave the key empty to use $OPENROUTER_API_KEY from your environment instead."
        ),
    },
    PROVIDER_GROQ: {
        "label": "Groq",
        "model_key": "model_groq",
        "api_key_key": "api_key_groq",
        "base_url_key": "",
        "env_vars": ("GROQ_API_KEY",),
        "model_placeholder": "llama-3.3-70b-versatile",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "list_models": "openai",
        "note": (
            'Very fast hosted open models. Get a key at <a href="https://console.groq.com/keys">'
            "console.groq.com/keys</a>. Leave the key empty to use $GROQ_API_KEY from your environment instead."
        ),
    },
    PROVIDER_AZURE: {
        "label": "Azure OpenAI",
        "model_key": "model_azure",
        "model_label": "Deployment",
        "api_key_key": "api_key_azure",
        "base_url_key": "base_url_azure",
        "base_label": "Endpoint",
        "base_placeholder": "https://your-resource.openai.azure.com",
        "api_version_key": "api_version_azure",
        "env_vars": ("AZURE_OPENAI_API_KEY",),
        "model_placeholder": "your deployment's name",
        "note": (
            "Your organisation's Azure OpenAI resource. The endpoint and key are on the resource's "
            "Keys and Endpoint page in the Azure portal; Deployment is the name you gave the model "
            "there. Leave the key empty to use $AZURE_OPENAI_API_KEY from your environment instead."
        ),
    },
    PROVIDER_OLLAMA: {
        "label": "Ollama (local)",
        "model_key": "model_ollama",
        "api_key_key": "",
        "base_url_key": "base_url_ollama",
        "base_label": "Server address",
        "base_placeholder": "This PC (127.0.0.1:11434)",
        "env_vars": (),
        "model_placeholder": "qwen3:14b",
        "list_models": "ollama",
        "auto_list": True,
        "note": (
            "Runs on your own hardware – no API key, and nothing goes to a cloud service. Ollama must "
            "be running; the list above shows what it has installed. Leave the address empty for this "
            "PC, or enter another machine's (e.g. 192.168.1.20:11434)."
        ),
    },
    PROVIDER_LLAMACPP: {
        "label": "llama.cpp server (local)",
        "model_key": "model_llamacpp",
        "api_key_key": "api_key_llamacpp",
        "key_label": "API key (optional)",
        "base_url_key": "base_url_llamacpp",
        "base_label": "Server address",
        "base_placeholder": "This PC (127.0.0.1:8080)",
        "env_vars": (),
        "model_placeholder": "optional – it answers with the model it loaded",
        "list_models": "openai",
        "auto_list": True,
        "note": (
            "llama.cpp's own server, llama-server. Start it with <b>--jinja</b>, or the model can't use "
            "the tools Ask Buddy works through. Leave the address empty for this PC on port 8080; the "
            "key only if you started it with --api-key."
        ),
    },
    PROVIDER_LMSTUDIO: {
        "label": "LM Studio (local)",
        "model_key": "model_lmstudio",
        "api_key_key": "api_key_lmstudio",
        "key_label": "API key (optional)",
        "base_url_key": "base_url_lmstudio",
        "base_label": "Server address",
        "base_placeholder": "This PC (127.0.0.1:1234)",
        "env_vars": (),
        "model_placeholder": "a model you've downloaded in LM Studio",
        "list_models": "openai",
        "auto_list": True,
        "note": (
            "Start LM Studio's local server (the Developer tab). Leave the address empty for this PC on "
            "port 1234 – the list above shows the models it has. Nothing goes to a cloud service."
        ),
    },
    PROVIDER_OPENAI: {
        "label": "Custom (OpenAI-compatible)",
        "model_key": "model_openai",
        "api_key_key": "api_key_openai",
        "key_label": "API key (optional)",
        "base_url_key": "base_url_openai",
        "base_label": "Base URL",
        "base_placeholder": "https://api.example.com/v1",
        "env_vars": ("BUDDY_LLM_API_KEY", "OPENAI_API_KEY"),
        "model_placeholder": "the model name the service uses",
        "list_models": "openai",
        "note": (
            "Any other endpoint that speaks OpenAI's /chat/completions – vLLM, Jan, KoboldCpp, LocalAI, "
            "DeepSeek, Mistral, xAI, Together and more. The base URL is what picks the service; the key is "
            "only needed if it uses one."
        ),
    },
}

DEFAULTS = {
    "provider": PROVIDER_GEMINI,
    "model_gemini": "gemini-3.8-flash",
    "model_anthropic": "claude-sonnet-5",
    "model_ollama": "",
    "model_openai": "",
    "model_openai_api": "",
    "model_openrouter": "",
    "model_groq": "",
    "model_azure": "",
    "model_llamacpp": "",
    "model_lmstudio": "",
    "api_key_gemini": "",
    "api_key_anthropic": "",
    "api_key_openai": "",
    "api_key_openai_api": "",
    "api_key_openrouter": "",
    "api_key_groq": "",
    "api_key_azure": "",
    "api_key_llamacpp": "",
    "api_key_lmstudio": "",
    "base_url_openai": "",
    "base_url_azure": "",
    "base_url_ollama": "",
    "base_url_llamacpp": "",
    "base_url_lmstudio": "",
    "api_version_azure": "",
    "bundle_dir": "",
    "text_path": "",
    # The manual PDF citations open, for a bundle whose meta.json doesn't
    # say what it was built from (manual_pdf.py).
    "manual_pdf": "",
    # Semantic search's embedder (embedder.py): "auto" is Buddy's own model
    # once set up, else Ollama. The file is one in the models folder, ""
    # for the first whose name says it embeds; the rest are for "server".
    "embed_backend": "auto",
    "embed_model_file": "",
    "embed_base_url": "",
    "embed_model": "",
    "embed_api_key": "",
    "history_turns": DEFAULT_HISTORY_TURNS,
    # How many tool-call turns the agent may make per answer before it
    # gives up (Ask Buddy Settings -> "Tool call budget").
    "max_steps": DEFAULT_MAX_STEPS,
    # Agentic project changes. Off unless the user has typed the consent
    # sentence; see core/write_consent.py for why it is not just a flag.
    "allow_project_writes": False,
    "project_read_consent": "",           # the address (llm.destination) project details may be sent to
}

# Where the manual data lives, best first. ~/.buddy/manual is where
# "Rebuild from PDF..." (and build_manual_bundle.py) write it.
DATA_CANDIDATES = [
    Path.home() / ".buddy" / "manual",
]


def provider_spec(settings, provider=None) -> dict:
    provider = provider or settings.get("provider", PROVIDER_GEMINI)
    return PROVIDER_SPEC.get(provider, PROVIDER_SPEC[PROVIDER_GEMINI])


def api_key_from_settings(settings, provider=None) -> str:
    """This provider's key: its own env vars first, then its own field.

    Only the selected provider's variables are consulted - picking up
    OPENAI_API_KEY while Gemini is selected would produce a baffling
    authentication error.
    """
    spec = provider_spec(settings, provider)
    for var in spec["env_vars"]:
        value = os.environ.get(var)
        if value:
            return value
    return secrets_store.unlock(settings.get(spec["api_key_key"], "")) if spec["api_key_key"] else ""


def key_fields() -> list[str]:
    """Every settings field that holds a key."""
    return [s["api_key_key"] for s in PROVIDER_SPEC.values() if s["api_key_key"]] + ["embed_api_key"]


def llm_client_from_settings(settings, **kwargs) -> LLMClient:
    """The client for whichever provider Ask Buddy's Settings select - also
    what Transcribe's AI translation uses, so there is one place to set a
    key. kwargs go to LLMClient (timeout, max_tokens)."""
    provider = settings.get("provider", PROVIDER_GEMINI)
    spec = provider_spec(settings, provider)
    if provider not in PROVIDER_SPEC:
        provider = PROVIDER_GEMINI
    return LLMClient(
        provider=provider,
        api_key=api_key_from_settings(settings, provider),
        model=settings.get(spec["model_key"], ""),
        base_url=settings.get(spec["base_url_key"], "") if spec["base_url_key"] else "",
        api_version=settings.get(spec.get("api_version_key") or "", "") if spec.get("api_version_key") else "",
        **kwargs,
    )


def default_data_paths() -> tuple[str, str]:
    """(bundle_dir, text_path) - first candidate that actually has data."""
    for root in DATA_CANDIDATES:
        for bundle in (root / "bundle", root / "app" / "bundle"):
            if (bundle / "chunks.jsonl").exists():
                text = root / "manual.txt"
                return str(bundle), str(text if text.exists() else "")
        text = root / "manual.txt"
        if text.exists():
            return "", str(text)
    return "", ""


def rebuild_target(settings) -> Path:
    """Where "Rebuild from PDF..." writes: an explicit bundle_dir setting if
    there is one, else the first candidate - so the result is always the
    bundle this page loads next, never a copy it would skip past."""
    custom = settings.get("bundle_dir") or ""
    return Path(custom) if custom else DATA_CANDIDATES[0] / "bundle"


def history_limit(settings) -> int:
    try:
        limit = int(settings.get("history_turns", DEFAULT_HISTORY_TURNS))
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY_TURNS
    return max(0, min(limit, MAX_HISTORY_TURNS))


def max_steps_limit(settings) -> int:
    """The agent's per-answer tool-call budget, clamped to the same range
    the Settings slider offers. The agent clamps again, so the two can
    never disagree about the boundary."""
    try:
        steps = int(settings.get("max_steps", DEFAULT_MAX_STEPS))
    except (TypeError, ValueError):
        steps = DEFAULT_MAX_STEPS
    return max(DEFAULT_MAX_STEPS, min(steps, MAX_MAX_STEPS))


def migrate_legacy_settings(settings):
    """Carry a pre-existing setup onto the per-provider keys.

    Settings used to be one shared model / api_key / base_url. Those are
    moved onto whichever provider was selected when they were written -
    the only provider they could have belonged to - so someone who had
    Gemini working does not open this build to an empty key field. The
    legacy keys are dropped once moved (a key is no longer left in the file
    as typed); nothing reads them after this. Every key still stored as typed
    is locked here (lock_stored_keys).
    """
    spec = PROVIDER_SPEC.get(settings.get("provider", PROVIDER_GEMINI))
    if not spec:
        return
    moved = False
    for legacy, target in (
        ("model", spec["model_key"]),
        ("api_key", spec["api_key_key"]),
        ("base_url", spec["base_url_key"]),
    ):
        value = settings.get(legacy, "")
        if value and target and not settings.get(target, ""):
            settings[target] = value
            moved = True
    if moved:
        settings.save()
    lock_stored_keys(settings)


def lock_stored_keys(settings):
    """Seals every key still in the file as typed - and the old shared
    `api_key`, which is removed - then deletes the settings file's .bak,
    which holds the file as it was before (the keys as typed). Returns True
    if anything was locked."""
    values = getattr(settings, "values", None)
    if not isinstance(values, dict):
        values = settings                 # a plain dict stands in for ToolSettings
    changed = False
    for name in key_fields():
        text = values.get(name)
        if isinstance(text, str) and text and not secrets_store.is_locked(text):
            locked = secrets_store.lock(text)
            if locked != text:
                values[name] = locked
                changed = True
    if values.get("api_key"):
        values.pop("api_key", None)
        changed = True
    if changed:
        settings.save()
        path = getattr(settings, "_path", None)
        if path:
            try:
                os.remove(atomic_io.backup_path(path))
            except OSError:
                pass                         # none there
    return changed
