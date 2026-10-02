#!/usr/bin/env python3
"""
BYOK chat transport for the Manual Chat page. Many providers, one interface.

Deliberately stdlib-only (urllib, not requests / google-generativeai).
Buddy's only dependency today is PySide6, and a chat page is a poor reason
to make an editor install an SDK stack. Both providers are plain JSON over
HTTPS; the whole surface we need is ~100 lines per provider.

This module is TRANSPORT ONLY - it knows nothing about the manual, the
system prompt, or what the tools do. agent.py owns all of that.

Three request shapes: Gemini's, Anthropic's, and OpenAI's /chat/completions -
which nearly everything else speaks: OpenAI itself, OpenRouter, Groq, the
local servers (Ollama, llama.cpp, LM Studio), Azure OpenAI (with its own
URL and key header) and any other compatible endpoint ("custom"). ENDPOINTS
says, for each of those, where it lives by default, whether it needs a key
and whether it runs on this machine.

The neutral message format, converted per provider on the way out:

    {"role": "user"|"assistant"|"tool", "content": str,
     "tool_calls": [{"id", "name", "arguments"}],   # assistant turns
     "tool_call_id": str, "name": str,              # tool result turns
     "images": [{"mime", "data"}]}                  # user turns: base64 pictures

Pictures go the way each provider takes them: Gemini's inline_data parts,
Anthropic's image blocks, OpenAI's image_url content parts (data: URLs) -
which OpenRouter, Groq, Ollama, llama.cpp, LM Studio and Azure all accept
for a model that can see. A model that can't gives an HTTP error, which
the page explains (pictures.py).

Not streaming. A tool-calling loop already makes several round trips per
answer, and streaming deltas that may turn out to be a tool call rather
than prose is a large complication for a first version - the page shows a
"thinking" state instead. The Reply shape below is what a streaming
implementation would yield incrementally, so adding it later is additive.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from core import safe_http

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_TIMEOUT = 90

PROVIDER_GEMINI = "gemini"
PROVIDER_OPENAI = "openai"          # any OpenAI-compatible endpoint ("Custom")
PROVIDER_OLLAMA = "ollama"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_API = "openai_api"  # OpenAI itself
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_GROQ = "groq"
PROVIDER_LLAMACPP = "llamacpp"
PROVIDER_LMSTUDIO = "lmstudio"
PROVIDER_AZURE = "azure"

# Azure OpenAI versions its API by date, like Anthropic; this one is GA.
AZURE_API_VERSION = "2024-10-21"

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
# Pinned, not "latest": Anthropic versions its API by date and a silent
# bump could change response shapes under a deployed copy of Buddy.
ANTHROPIC_VERSION = "2023-06-01"
# Required by the Messages API - unlike the other two, it has no default.
ANTHROPIC_MAX_TOKENS = 4096

# Ollama serves an OpenAI-compatible API at /v1, so it reuses the OpenAI
# transport wholesale - it needs no separate request builder, only different
# defaults. 127.0.0.1 rather than localhost for the same reason as
# retrieval.py: the IPv6 first attempt costs a flat ~2s per request.
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
# Local generation is far slower than a hosted API - measured on one
# desktop, one turn is 9s for gemma4:12b and 40s for qwen3:14b, and the
# agent makes several turns per answer. 90s would time out mid-answer.
OLLAMA_TIMEOUT = 600
LOCAL_TIMEOUT = OLLAMA_TIMEOUT

# The OpenAI-shaped providers: default base URL, whether a key is required,
# whether it's a server on this machine (or the user's own network), and
# the name errors are labelled with. A local server's base URL can be
# changed in Settings (another PC on the network, another port); a cloud
# one's is fixed. "Custom" has no default - its base URL is what picks it.
ENDPOINTS = {
    PROVIDER_OPENAI_API: {"base": "https://api.openai.com/v1", "key": True, "local": False, "label": "OpenAI"},
    PROVIDER_OPENROUTER: {"base": "https://openrouter.ai/api/v1", "key": True, "local": False, "label": "OpenRouter"},
    PROVIDER_GROQ: {"base": "https://api.groq.com/openai/v1", "key": True, "local": False, "label": "Groq"},
    PROVIDER_OLLAMA: {"base": OLLAMA_BASE_URL, "key": False, "local": True, "label": "Ollama"},
    PROVIDER_LLAMACPP: {"base": "http://127.0.0.1:8080/v1", "key": False, "local": True, "label": "llama.cpp"},
    PROVIDER_LMSTUDIO: {"base": "http://127.0.0.1:1234/v1", "key": False, "local": True, "label": "LM Studio"},
    PROVIDER_OPENAI: {"base": "", "key": False, "local": False, "label": "LLM"},
    PROVIDER_AZURE: {"base": "", "key": True, "local": False, "label": "Azure OpenAI"},
}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# Names a model list offers that can't chat: picking one would only give a
# confusing API error the first time it was used.
_NOT_CHAT = ("embed", "whisper", "tts", "dall-e", "moderation", "transcribe", "image")


def normalize_base_url(provider: str, url: str) -> str:
    """What a base URL typed into Settings means. A local server's may be
    typed as just host:port ("192.168.1.20:8080") - the scheme and the /v1
    its OpenAI-compatible API lives under are added. Anything else is
    taken as typed, minus a trailing slash."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if provider in (PROVIDER_OLLAMA, PROVIDER_LLAMACPP, PROVIDER_LMSTUDIO):
        if "://" not in url:
            url = "http://" + url
        parts = urllib.parse.urlsplit(url)
        if parts.path in ("", "/"):
            url = f"{parts.scheme}://{parts.netloc}/v1"
    return url


def is_local_url(url: str) -> bool:
    """A server on this PC, or on a private network (a GPU box down the hall)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in LOCAL_HOSTS or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)
    return False


def _chat_names(names) -> list[str]:
    return sorted({n for n in names if n and not any(bad in n.lower() for bad in _NOT_CHAT)})


def list_ollama_models(base_url: str = "", timeout: int = 3) -> list[str]:
    """Model names installed in Ollama (this PC's, or the one at base_url),
    for the Settings list. [] when it isn't reachable - the field stays
    free text, so a typed model name still works."""
    root = normalize_base_url(PROVIDER_OLLAMA, base_url or OLLAMA_BASE_URL)
    root = root[:-3] if root.endswith("/v1") else root
    try:
        with safe_http.urlopen(f"{root}/api/tags", timeout=timeout) as resp:
            models = json.load(resp).get("models") or []
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    return _chat_names(m.get("name", "") for m in models)


def list_openai_models(base_url: str, api_key: str = "", timeout: int = 5) -> list[str]:
    """The models an OpenAI-compatible server offers (GET /models) - what
    llama.cpp has loaded, LM Studio's downloads, OpenRouter's catalogue.
    Raises LLMError when it can't be read, so Settings can say why."""
    if api_key and not _safe_to_send_key(base_url):
        raise LLMError("That address starts with http:// - your API key would be sent unencrypted. "
                       "Use https://, or leave the key empty for a server on your own network that doesn't need one.")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    req = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers=headers)
    try:
        with safe_http.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise LLMError(f"Couldn't list the models: HTTP {e.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LLMError(f"Couldn't reach {base_url}: {getattr(e, 'reason', e)}")
    except ValueError:
        raise LLMError("The server's model list wasn't JSON.")
    items = data.get("data") if isinstance(data, dict) else data
    return _chat_names(m.get("id", "") for m in (items or []) if isinstance(m, dict))


class LLMError(RuntimeError):
    """Anything that stopped us getting an answer. The message is shown to
    the user verbatim, so it names the provider and stays readable."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Gemini 3 thinking models return an opaque thoughtSignature alongside
    # each functionCall part, and REJECT the next request if it isn't echoed
    # back verbatim on that same part ("Function call is missing a
    # thought_signature"). Opaque to us - store and replay, never inspect.
    signature: str = ""


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _safe_to_send_key(base_url: str) -> bool:
    """https, or plain http to this PC itself - where nothing on the way can read it."""
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and (parts.hostname or "") in LOCAL_HOSTS


def _post(url: str, payload: dict, headers: dict, label: str, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with safe_http.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            data = json.load(e)
            detail = data.get("error", {}).get("message") or ""
        except Exception:
            detail = e.reason or ""
        raise LLMError(f"{label}: HTTP {e.code}{' – ' + detail if detail else ''}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"{label}: could not reach the API ({e})")
    except ValueError:
        raise LLMError(f"{label}: the API returned a response that wasn't JSON")


class LLMClient:
    """One provider, configured once from settings.

    provider: one of the PROVIDER_* ids. base_url is only needed where
    there's no default (custom, Azure) or to point a local server at
    another machine or port. api_version is Azure's.
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        max_tokens: int = ANTHROPIC_MAX_TOKENS,
        api_version: str = "",
    ):
        self.provider = (provider or PROVIDER_GEMINI).strip().lower()
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.endpoint = ENDPOINTS.get(self.provider)
        self.base_url = normalize_base_url(self.provider, base_url)
        if self.endpoint and not self.base_url:
            self.base_url = self.endpoint["base"]
        self.api_version = (api_version or "").strip() or AZURE_API_VERSION
        self.timeout = timeout
        # Only Anthropic needs it (see ANTHROPIC_MAX_TOKENS); a translation
        # batch can run past the chat's 4096.
        self.max_tokens = max_tokens
        if self.local and timeout == DEFAULT_TIMEOUT:
            self.timeout = LOCAL_TIMEOUT

    @property
    def local(self) -> bool:
        """Runs on this PC or the user's own network: nothing goes to a
        cloud service (Transcribe says so), and it's given local-speed
        timeouts."""
        if self.provider == PROVIDER_OLLAMA and self.model.lower().endswith(("-cloud", ":cloud")):
            return False   # Ollama's cloud models run on Ollama's servers
        # Where the address points decides, never the provider's name: an
        # "Ollama" whose address is a hosted service is a cloud service.
        if self.endpoint and self.endpoint["local"]:
            return is_local_url(self.base_url)
        return self.provider == PROVIDER_OPENAI and bool(self.base_url) and is_local_url(self.base_url)

    @property
    def destination(self) -> str:
        """Who receives what's sent: the address's host, else the provider
        (Gemini and Anthropic have no address to type). What a consent to
        send text away is tied to - the same provider at another address
        needs asking again."""
        host = (urllib.parse.urlsplit(self.base_url).hostname or "").lower() if self.base_url else ""
        return host or self.provider

    @property
    def default_address(self) -> bool:
        """True while the address is the provider's own (or there is none)."""
        return not self.base_url or bool(self.endpoint and self.base_url == self.endpoint["base"])

    @property
    def key_required(self) -> bool:
        if self.provider in (PROVIDER_GEMINI, PROVIDER_ANTHROPIC):
            return True
        return bool(self.endpoint and self.endpoint["key"])

    @property
    def label(self) -> str:
        return self.endpoint["label"] if self.endpoint else self.provider.title()

    def validate(self) -> str:
        """Returns "" when usable, else a sentence telling the user what to
        fix. Called before every send so the page can show a clear message
        instead of an HTTP 400."""
        # Local servers (Ollama, llama.cpp, LM Studio) and most custom ones
        # have no key - demanding one would only make people type a fake.
        if not self.api_key and self.key_required:
            return "No API key set. Add one in Settings to use the chat."
        # llama.cpp answers with whatever model it was started with.
        if not self.model and self.provider != PROVIDER_LLAMACPP:
            return ("No deployment set. Enter your Azure deployment name in Settings."
                    if self.provider == PROVIDER_AZURE else "No model set. Choose a model in Settings.")
        if self.provider in (PROVIDER_OPENAI, PROVIDER_AZURE) and not self.base_url:
            return ("Azure OpenAI needs your resource's endpoint in Settings."
                    if self.provider == PROVIDER_AZURE else "OpenAI-compatible providers need a base URL in Settings.")
        if self.endpoint and self.base_url:
            scheme = urllib.parse.urlsplit(self.base_url).scheme
            if scheme not in ("http", "https"):
                return "The server address has to start with http:// or https://."
            # Only a key needs protecting: a keyless request to a server on
            # the user's own network over plain http is fine.
            if self.api_key and not _safe_to_send_key(self.base_url):
                return ("That address starts with http:// – your API key would be sent unencrypted. Use "
                        "https://, or leave the key empty for a server on your own network that doesn't need one.")
        return ""

    def test(self) -> tuple[bool, str]:
        """Settings > Test connection: one tiny request that asks for a tool
        call, since Ask Buddy works through tools. (ok, what to tell them)."""
        problem = self.validate()
        if problem:
            return False, problem
        ping = {"name": "ping", "description": "Confirms the connection works. Always call it when asked.",
                "parameters": {"type": "object", "properties": {}, "required": []}}
        try:
            reply = self.chat("You are testing a connection. Call the ping tool.",
                              [{"role": "user", "content": "Call the ping tool now."}], tools=[ping])
        except LLMError as exc:
            return False, str(exc)
        name = self.model or "The model"
        if reply.tool_calls:
            return True, f"Connected – {name} answered and can use tools."
        hint = (" Start llama-server with --jinja so it can." if self.provider == PROVIDER_LLAMACPP
                else " Ask Buddy needs a model with tool calling – try a larger or instruction-tuned one.")
        return False, f"Connected, but {name} didn't call the tool it was asked to.{hint}"

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None = None) -> Reply:
        problem = self.validate()
        if problem:
            raise LLMError(problem)
        if self.provider == PROVIDER_GEMINI:
            return self._chat_gemini(system, messages, tools)
        if self.provider == PROVIDER_ANTHROPIC:
            return self._chat_anthropic(system, messages, tools)
        return self._chat_openai(system, messages, tools)  # every OpenAI-shaped provider

    # ------------------------------------------------------------- gemini

    def _chat_gemini(self, system, messages, tools) -> Reply:
        contents = []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                # Gemini carries tool results as a functionResponse part on a
                # user turn. Multiple tool results from the same step MUST be
                # in the same turn.
                part = {
                    "functionResponse": {
                        "name": m.get("name", ""),
                        "response": {"result": m.get("content", "")},
                    }
                }
                if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            parts = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for image in m.get("images") or []:
                parts.append({"inline_data": {"mime_type": image["mime"], "data": image["data"]}})
            for call in m.get("tool_calls") or []:
                part = {
                    "functionCall": {
                        "name": call["name"],
                        "args": call["arguments"],
                    }
                }
                if call.get("signature"):
                    part["thoughtSignature"] = call["signature"]
                parts.append(part)
            if parts:
                contents.append(
                    {"role": "model" if role == "assistant" else "user", "parts": parts}
                )

        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
        }
        if tools:
            payload["tools"] = [{"function_declarations": tools}]

        url = GEMINI_ENDPOINT.format(model=self.model)
        data = _post(
            url,
            payload,
            # A header, not ?key= in the address: addresses end up in proxy logs.
            {"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            "Gemini",
            self.timeout,
        )

        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(
                f"Gemini returned no answer{f' ({blocked})' if blocked else ''}."
            )
        parts = (candidates[0].get("content") or {}).get("parts") or []
        reply = Reply()
        for i, part in enumerate(parts):
            if "text" in part:
                reply.content += part["text"]
            fc = part.get("functionCall")
            if fc:
                reply.tool_calls.append(
                    ToolCall(
                        id=f"{fc.get('name','tool')}-{i}",
                        name=fc.get("name", ""),
                        arguments=fc.get("args") or {},
                        signature=part.get("thoughtSignature") or "",
                    )
                )
        return reply

    # ---------------------------------------------------------- anthropic

    def _chat_anthropic(self, system, messages, tools) -> Reply:
        """Anthropic Messages API.

        Three shape differences from the OpenAI transport, each of which is
        a hard error rather than a silent degradation if missed:

          system        a top-level parameter, NOT a message in the list.
          max_tokens    required; the request is rejected without it.
          tool results  carried as tool_result CONTENT BLOCKS on a user
                        turn, not on a "tool" role - and every result for
                        one assistant turn must arrive in a SINGLE user
                        message, which is why consecutive tool messages are
                        merged below rather than emitted one apiece. The
                        agent loop can dispatch several calls per turn.

        No extended thinking is requested, so there are no thinking blocks
        to round-trip. If that is ever enabled, their signatures have to be
        replayed verbatim - the same trap Gemini's thoughtSignature set.
        """
        out = []
        for m in messages:
            role = m.get("role")

            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }
                # Merge into the previous user turn when that turn is itself
                # tool results, so parallel calls stay in one message.
                if (
                    out
                    and out[-1]["role"] == "user"
                    and isinstance(out[-1]["content"], list)
                    and out[-1]["content"]
                    and out[-1]["content"][0].get("type") == "tool_result"
                ):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            blocks = []
            for image in m.get("images") or []:   # pictures first, then the question about them
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": image["mime"],
                                                           "data": image["data"]}})
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for call in m.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                )
            # An empty content list is rejected; a turn with neither text nor
            # tool calls has nothing to say and is simply dropped.
            if blocks:
                out.append(
                    {
                        "role": "assistant" if role == "assistant" else "user",
                        "content": blocks,
                    }
                )

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": out,
        }
        if tools:
            # Same JSON Schema as the others, under a different key name.
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for t in tools
            ]

        data = _post(
            ANTHROPIC_ENDPOINT,
            payload,
            {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            "Claude",
            self.timeout,
        )

        blocks = data.get("content") or []
        if not blocks:
            stop = data.get("stop_reason") or ""
            raise LLMError(
                f"Claude returned no answer{f' ({stop})' if stop else ''}."
            )

        reply = Reply()
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                reply.content += block.get("text") or ""
            elif kind == "tool_use":
                reply.tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
                )
        return reply

    # ------------------------------------------------------------- openai

    def _chat_openai(self, system, messages, tools) -> Reply:
        out = [{"role": "system", "content": system}]
        for m in messages:
            role = m.get("role")
            if role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.get("tool_call_id", ""),
                        "content": m.get("content", ""),
                    }
                )
                continue
            msg = {"role": role, "content": m.get("content", "")}
            if m.get("images"):
                msg["content"] = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [
                    {"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}}
                    for i in m["images"]]
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"]),
                        },
                    }
                    for c in m["tool_calls"]
                ]
            out.append(msg)

        payload = {"model": self.model or "local", "messages": out}
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]

        headers = {"Content-Type": "application/json"}
        if self.provider == PROVIDER_AZURE:
            # The deployment is in the URL, and the key has its own header.
            url = (f"{self.base_url}/openai/deployments/{urllib.parse.quote(self.model)}/chat/completions"
                   f"?api-version={urllib.parse.quote(self.api_version)}")
            headers["api-key"] = self.api_key
        else:
            url = f"{self.base_url}/chat/completions"
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        data = _post(url, payload, headers, self.label, self.timeout)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError("The API returned no answer.")
        message = choices[0].get("message") or {}
        reply = Reply(content=message.get("content") or "")
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                # Some OpenAI-compatible servers emit malformed argument
                # JSON; treat it as no arguments rather than failing the turn.
                args = {}
            reply.tool_calls.append(
                ToolCall(id=call.get("id", ""), name=fn.get("name", ""), arguments=args)
            )
        return reply
