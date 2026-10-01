#!/usr/bin/env python3
"""
What turns text into vectors for Ask Buddy's semantic search - the
person's choice in Settings, and not only Ollama:

  buddy    Buddy's own llama.cpp running a GGUF model from the models
           folder (local_llama.py) - nothing else to install.
  ollama   Ollama, with embeddinggemma pulled.
  server   Any OpenAI-compatible /embeddings endpoint: LM Studio, a
           llama-server started by hand, vLLM and the like.
  auto     Buddy's own once it's set up, else Ollama - what an existing
           install had, so nothing changes for it until Set up is pressed.
  off      Keyword search only.

The manual bundle's vectors are EmbeddingGemma's, and a query only finds
them through the same model - retrieval.py checks that before it relies on
one (ManualRetriever.verify), so a model that doesn't match costs answers
nothing: search falls back to keywords and says why.

No Qt here: the Settings window, the chat's search and bundle_builder all
call these from their own threads.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import local_llama
from .llm import PROVIDER_LMSTUDIO, is_local_url, normalize_base_url
from .retrieval import EMBED_MODEL, OLLAMA_HOST

BACKEND_AUTO, BACKEND_BUDDY, BACKEND_OLLAMA, BACKEND_SERVER, BACKEND_OFF = "auto", "buddy", "ollama", "server", "off"
BACKENDS = [
    (BACKEND_AUTO, "Automatic"),
    (BACKEND_BUDDY, "Buddy's own model"),
    (BACKEND_OLLAMA, "Ollama"),
    (BACKEND_SERVER, "Another server (LM Studio, llama.cpp…)"),
    (BACKEND_OFF, "Off – keyword search only"),
]
DEFAULT_SERVER_URL = "http://127.0.0.1:1234/v1"       # LM Studio's
EMBED_TIMEOUT = 300
READY_TIMEOUT = 3


class EmbedError(Exception):
    """Worded for the person: what didn't answer, and what to do."""


class Embedder:
    backend = ""
    label = ""                  # "Buddy's own EmbeddingGemma", "Ollama", "LM Studio at ..."
    model_name = ""             # what bundle meta.json records

    def ready(self) -> tuple[bool, str]:
        """Whether it can embed now, asked cheaply - nothing is loaded or
        started - and why not."""
        raise NotImplementedError

    def embed(self, texts: list[str], timeout: float = EMBED_TIMEOUT) -> list[list[float]]:
        raise NotImplementedError

    @property
    def leaves_machine(self) -> bool:
        """True for a server off this computer and its local network."""
        return False


def _post(url: str, body: dict, timeout: float, key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _openai_vectors(payload: dict, count: int, who: str) -> list[list[float]]:
    rows = sorted(payload.get("data") or [], key=lambda d: d.get("index", 0))
    vecs = [d.get("embedding") for d in rows]
    if len(vecs) != count or not all(isinstance(v, list) and v for v in vecs):
        raise EmbedError(f"{who} returned {len(vecs)} embeddings for {count} texts.")
    return vecs


class OllamaEmbedder(Embedder):
    backend = BACKEND_OLLAMA
    label = "Ollama"
    model_name = EMBED_MODEL

    def ready(self):
        try:
            with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=READY_TIMEOUT) as resp:
                models = json.load(resp).get("models") or []
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False, "Ollama isn't running."
        if not any((m.get("name") or "").startswith(EMBED_MODEL) for m in models):
            return False, f"Ollama has no {EMBED_MODEL} model (ollama pull {EMBED_MODEL})."
        return True, ""

    def embed(self, texts, timeout=EMBED_TIMEOUT):
        try:
            vecs = _post(f"{OLLAMA_HOST}/api/embed", {"model": EMBED_MODEL, "input": texts}, timeout).get("embeddings") or []
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            raise EmbedError(f"Ollama stopped answering: {exc}") from exc
        if len(vecs) != len(texts):
            raise EmbedError(f"Ollama returned {len(vecs)} embeddings for {len(texts)} texts.")
        return vecs


class ServerEmbedder(Embedder):
    backend = BACKEND_SERVER

    def __init__(self, base_url: str, model: str = "", api_key: str = ""):
        # Typed as host:port like a chat server's - the scheme and /v1 are added.
        self.base_url = normalize_base_url(PROVIDER_LMSTUDIO, base_url) or DEFAULT_SERVER_URL
        self.model = model.strip()
        self.api_key = api_key.strip()
        self.model_name = self.model or "server default"
        self.label = f"the server at {self.base_url}"

    @property
    def leaves_machine(self):
        return not is_local_url(self.base_url)

    def ready(self):
        try:
            req = urllib.request.Request(f"{self.base_url}/models",
                                         headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
            with urllib.request.urlopen(req, timeout=READY_TIMEOUT):
                return True, ""
        except urllib.error.HTTPError as exc:
            return False, f"The server at {self.base_url} answered {exc.code}."
        except (urllib.error.URLError, OSError, TimeoutError):
            return False, f"Nothing is answering at {self.base_url} – is the server running?"

    def embed(self, texts, timeout=EMBED_TIMEOUT):
        body = {"input": texts}
        if self.model:
            body["model"] = self.model
        try:
            payload = _post(f"{self.base_url}/embeddings", body, timeout, self.api_key)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise EmbedError(f"The server at {self.base_url} answered {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            raise EmbedError(f"The server at {self.base_url} stopped answering: {exc}") from exc
        return _openai_vectors(payload, len(texts), f"The server at {self.base_url}")


class BuddyEmbedder(Embedder):
    backend = BACKEND_BUDDY

    def __init__(self, model_path: Path | None):
        self.model_path = Path(model_path) if model_path else None
        name = self.model_path.stem if self.model_path else ""
        self.model_name = EMBED_MODEL if "embeddinggemma" in name.lower() else name
        self.label = ("Buddy's own EmbeddingGemma" if self.model_name == EMBED_MODEL
                      else f"Buddy's own {self.model_path.name}" if self.model_path else "Buddy's own model")

    def ready(self):
        if local_llama.server_exe() is None:
            return False, "Buddy's own model isn't set up yet – Settings > AI > Manual search > Set up."
        if self.model_path is None:
            return False, f"There's no embedding model in {local_llama.MODELS_DIR} – Set up downloads one."
        if not local_llama.is_gguf(self.model_path):
            return False, f"{self.model_path.name} isn't there, or isn't a GGUF model."
        return True, ""

    def embed(self, texts, timeout=EMBED_TIMEOUT):
        ok, why = self.ready()
        if not ok:
            raise EmbedError(why)
        try:
            payload = local_llama.server_for(self.model_path).post("/embeddings", {"input": texts}, timeout)
        except local_llama.LlamaError as exc:
            raise EmbedError(str(exc)) from exc
        return _openai_vectors(payload, len(texts), "llama.cpp")


def chosen_model_file(settings) -> Path | None:
    """The models folder's file to embed with: the one picked in Settings
    if it's still there, else the first whose name says it embeds - a chat
    model dropped in beside it is never guessed to be one."""
    files = local_llama.model_files()
    picked = (settings.get("embed_model_file") or "").strip()
    for path in files:
        if path.name == picked:
            return path
    return next((p for p in files if "embed" in p.name.lower()), None)


def from_settings(settings) -> Embedder | None:
    """The embedder Settings chose, or None for keyword search only."""
    backend = settings.get("embed_backend") or BACKEND_AUTO
    if backend == BACKEND_OFF:
        return None
    if backend == BACKEND_OLLAMA:
        return OllamaEmbedder()
    if backend == BACKEND_SERVER:
        return ServerEmbedder(settings.get("embed_base_url") or "", settings.get("embed_model") or "",
                              settings.get("embed_api_key") or "")
    buddy = BuddyEmbedder(chosen_model_file(settings))
    if backend == BACKEND_BUDDY or buddy.ready()[0]:
        return buddy
    return OllamaEmbedder()            # automatic, before Buddy's own is set up
