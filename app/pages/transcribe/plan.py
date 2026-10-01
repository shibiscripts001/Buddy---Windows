#!/usr/bin/env python3
"""
Transcribe's choices, as plain data for the web page - no Qt, so it's
unit-tested (tests/test_transcribe_page.py): which models the menus offer,
which engine(s) a choice runs, which languages a translation goes into,
and what the Setup tab lists. Each "that can't work" is a message the
page shows.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import env_setup as es
from . import languages as L

MARK = "@@BUDDY "   # worker.py's protocol prefix (worker.py keeps its own copy: it runs in the venv)

# (label, Whisper language code). Whisper supports ~99; these are the ones
# worth a menu entry - "Auto-detect" covers the rest.
LANGUAGES = [
    ("Auto-detect", ""), ("English", "en"), ("Spanish", "es"), ("French", "fr"),
    ("German", "de"), ("Italian", "it"), ("Portuguese", "pt"), ("Dutch", "nl"),
    ("Swedish", "sv"), ("Polish", "pl"), ("Russian", "ru"), ("Ukrainian", "uk"),
    ("Turkish", "tr"), ("Arabic", "ar"), ("Hindi", "hi"), ("Japanese", "ja"),
    ("Korean", "ko"), ("Chinese", "zh"), ("Indonesian", "id"), ("Vietnamese", "vi"),
]
LANGUAGES_BY_CODE = [(code, label) for label, code in LANGUAGES]
# The menu shows these; each entry's tooltip carries the full description.
SHORT_LABELS = {"parakeet-v3": "Parakeet v3 · European", "distil-large-v3.5": "Distil-Whisper · English"}
AUTO_TIP = ("Parakeet for the 25 European languages it covers (fast, even without a GPU), "
            "the best installed Whisper model for everything else. The language is detected "
            "first, unless you choose one.")

# The translation menu's entry for Ask Buddy's language model.
AI_ID = "ai"
AI_TIMEOUT = 240
AI_PROVIDER_NAMES = {"anthropic": "Claude", "gemini": "Gemini", "ollama": "Ollama",
                     "openai": "OpenAI-compatible", "openai_api": "OpenAI", "openrouter": "OpenRouter",
                     "groq": "Groq", "azure": "Azure OpenAI", "llamacpp": "llama.cpp",
                     "lmstudio": "LM Studio"}
AI_TIP = ("Uses the model Ask Buddy is set up with (Claude, Gemini, Ollama…). Best quality – "
          "it translates whole passages with their context – but a cloud model gets the "
          "transcript's text, and may charge for it.")

MIXED = "mixed"   # the Language menu's "Mixed languages..." entry

# The Model menu's entry for Resolve Studio's own transcription (21.1+,
# resolve_transcript.py): nothing to install, and it tells speakers apart.
RESOLVE_ID = "resolve"
RESOLVE_LABEL = "DaVinci Resolve (Studio)"
RESOLVE_TIP = ("Resolve Studio transcribes the timeline itself – nothing to install or download here, and it "
               "tells the speakers apart. The transcription stays with the timeline in Resolve too.")

DEFAULTS = {"model": "", "language": "", "mixed_languages": [], "hotwords": "", "max_chars": 42,
            "max_lines": 2, "extra_models": {}, "speaker_names": True, "place_on_timeline": True,
            "translate_model": "", "translate_targets": [], "translate_auto": False,
            "translate_dir": "", "translate_timeline": True, "last_transcript": None,
            "srt_language": "eng_Latn"}


class Settings:
    """ToolSettings' interface (get / [] / save) over one JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.values = dict(DEFAULTS)
        try:
            self.values.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        return self.values.get(key, default)

    def __setitem__(self, key, value):
        self.values[key] = value

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
        except OSError:
            pass


# ------------------------------------------------------------- menus --

def model_options(models: dict, recommended: str, saved: str, resolve: bool = False):
    """([{id, label, tip}], chosen id) for the Model menu: Auto first when
    both Parakeet and a multilingual Whisper are installed; Resolve's own
    last, when the Resolve connected can (resolve: Studio 21.1+) - chosen
    by default only when nothing is installed here."""
    options = []
    if "parakeet-v3" in models and any(m in models for m in es.WHISPER_PREFERENCE):
        options.append({"id": "auto", "label": "Auto (Parakeet + Whisper)", "tip": AUTO_TIP})
    for m in es.MODELS:
        if m["id"] in models:
            options.append({"id": m["id"], "label": f"{SHORT_LABELS.get(m['id'], m['label'])} ({m['size_gb']:g} GB)",
                            "tip": m["fit"]})
    if resolve:
        options.append({"id": RESOLVE_ID, "label": RESOLVE_LABEL, "tip": RESOLVE_TIP})
    ids = [o["id"] for o in options]
    wanted = saved or ("auto" if "auto" in ids else recommended)
    return options, (wanted if wanted in ids else (ids[0] if ids else ""))


def language_name(code: str) -> str:
    return dict(LANGUAGES_BY_CODE).get(code, code)


def spoken_name(code: str) -> str:
    """A Whisper code, named as NLLB names it."""
    return L.name_of(L.WHISPER_TO_NLLB.get(code, code))


def spoken_languages() -> list[tuple[str, str]]:
    """(name, Whisper code) for every language Whisper transcribes, alphabetical."""
    return sorted(((L.name_of(nllb), code) for code, nllb in L.WHISPER_TO_NLLB.items()),
                  key=lambda x: x[0].lower())


def mixed_label(codes) -> str:
    codes = list(codes or [])
    return f"Mixed: {' + '.join(spoken_name(c) for c in codes)}" if len(codes) >= 2 else "Mixed languages\u2026"


def plan_for(model_id: str, models: dict, language: str, mixed=()):
    """What the worker runs for this choice: (plan, None), or (None, why
    not). plan = {"engine": whisper|parakeet|auto, "whisper": dir, "parakeet":
    dir, "language": code for the worker}. language is a Whisper code, "" to
    detect, or MIXED (then `mixed` lists the languages spoken)."""
    whisper = next((models[m] for m in es.WHISPER_PREFERENCE if m in models), "")
    parakeet = models.get("parakeet-v3", "")
    info = es.MODEL_BY_ID.get(model_id, {})
    if model_id == RESOLVE_ID:
        if language == MIXED:
            return None, ("Resolve's transcription hears one language per timeline. For mixed languages, "
                          "choose a Whisper model.")
        return {"engine": "resolve", "whisper": "", "parakeet": "", "language": language}, None
    if model_id != "auto" and model_id not in models:
        return None, "That model isn't installed – get it on the Setup tab."
    if language == MIXED:
        if len(list(mixed or [])) < 2:
            return None, "Choose at least two languages for mixed audio – for one, pick it from the menu."
        # Whisper is told each part's language; Parakeet and the
        # English-only models can't be.
        if model_id == "auto":
            if not whisper:
                return None, ("Mixed languages need a multilingual Whisper model (Large v3, Large v3 Turbo "
                              "or Small) – get one on the Setup tab.")
            return {"engine": "whisper", "whisper": whisper, "parakeet": "", "language": ""}, None
        if model_id == "parakeet-v3" or info.get("english_only"):
            return None, (f"Mixed languages need a Whisper model that knows them all – {info.get('label', model_id)} "
                          "can't be told which language each part is in. Choose Auto or a multilingual Whisper model.")
        return {"engine": "whisper", "whisper": models[model_id], "parakeet": "", "language": ""}, None
    name = language_name(language)
    if model_id == "auto":
        return {"engine": "auto", "whisper": whisper, "parakeet": parakeet, "language": language}, None
    if model_id == "parakeet-v3":
        if language and language not in es.PARAKEET_LANGS:
            return None, f"Parakeet doesn't cover {name}. Choose Auto or a Whisper model for it."
        # With auto-detect, a Whisper model (if any) names the language
        # first - translation needs to know it. Parakeet does the rest.
        return {"engine": "parakeet", "whisper": "" if language else whisper, "parakeet": parakeet,
                "language": language}, None
    if info.get("english_only"):
        if language not in ("", "en"):
            return None, f"{info['label']} only understands English. Choose another model for {name}."
        language = "en"
    return {"engine": "whisper", "whisper": models[model_id], "parakeet": "", "language": language}, None


def translation_options(tmodels: dict, ai_label: str, saved: str):
    """([{id, label, tip}], chosen) for the translation Model menu: the
    local models installed, then AI - always listed, so it can be found."""
    options = [{"id": m["id"], "label": f"{m['label']} ({m['size_gb']:g} GB)", "tip": m["fit"]}
               for m in es.TRANSLATION_MODELS if m["id"] in tmodels]
    options.append({"id": AI_ID, "label": ai_label, "tip": AI_TIP})
    ids = [o["id"] for o in options]
    wanted = saved or (es.RECOMMENDED_TRANSLATION if tmodels else AI_ID)
    return options, (wanted if wanted in ids else ids[0])


def targets_for(targets, source: str, family: str):
    """(the languages to translate into, notes about any left out)."""
    notes = []
    targets = [c for c in (targets or []) if c in L.NLLB_LANGUAGES]
    if source and source in targets:
        notes.append(f"Skipping {L.name_of(source)} – it's the language already spoken.")
    targets = [c for c in targets if c != source]
    if family == "madlad":
        lacking = [c for c in targets if c not in L.MADLAD_CODES]
        if lacking:
            notes.append("Skipping " + ", ".join(L.name_of(c) for c in lacking)
                         + " – MADLAD-400 doesn't have it. NLLB-200 or AI translation does.")
        targets = [c for c in targets if c in L.MADLAD_CODES]
    return targets, notes


# ------------------------------------------------------------- setup --

# Measured on PyPI for env_setup's pins: shown before installing.
ENV_DOWNLOAD_NVIDIA = "about 1.45 GB (mostly NVIDIA CUDA libraries)"
ENV_DOWNLOAD_CPU = "about 110 MB"


def model_label(model_id: str) -> str:
    return next((m["label"] for m in es.MODELS + es.TRANSLATION_MODELS if m["id"] == model_id), model_id)


def setup_rows(catalog, installed: dict, found: dict, recommended: str, verified: dict | None = None):
    """One row per model for the Setup tab: installed (and where), or a
    copy already on disk to use, beside the download - each with whether
    it's exactly the pinned files (verified: folder -> bool, missing while
    that's still being checked)."""
    verified = verified or {}
    rows = []
    for m in catalog:
        mid = m["id"]
        where = installed.get(mid)
        copy = "" if where else found.get(mid, "")
        rows.append({
            "id": mid, "label": m["label"], "size": f"{m['size_gb']:g} GB", "fit": m["fit"],
            "recommended": mid == recommended,
            "installed": bool(where),
            "where": ("Buddy's folder" if str(es.MODELS_DIR) in where else where) if where else "",
            "found": copy,
            "verified": verified.get(where or copy),
        })
    return rows


def setup_line(text: str):
    """One line of install/download output -> ("progress", fraction,
    caption), ("log", text) or None (pip's noise). Downloads report through
    the worker's protocol; pip through its own output."""
    if text.startswith(MARK):
        try:
            msg = json.loads(text[len(MARK):])
        except ValueError:
            return None
        if msg.get("type") == "progress" and msg.get("total"):
            frac = min(1.0, msg["done"] / msg["total"])
            return ("progress", frac, f"Downloading\u2026 {msg['done'] / 1e9:.2f} of {msg['total'] / 1e9:.2f} GB")
        if msg.get("type") in ("status", "error"):
            return ("log", msg.get("message", ""))
        return None
    if text.startswith(("Collecting", "Downloading", "Installing", "Successfully", "Creating", "Environment")):
        return ("log", text)
    return None
