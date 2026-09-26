#!/usr/bin/env python3
"""
Manual lookup for the chat page, degrading gracefully by what's on disk.

Buddy reads the PORTABLE BUNDLE FORMAT directly (PDF -> chunks -> vectors,
written by bundle_builder.py), so answering needs nothing but the bundle
files on disk.

Four tiers, best available wins. describe_tier() explains the current one
to the user, because "why are my answers worse today" should be answerable
from the UI:

  hybrid       bundle + Ollama  - vector similarity fused with BM25. Finds
                                  paraphrases ("get rid of the hum" ->
                                  noise reduction).
  bundle-bm25  bundle, no Ollama- BM25 over chunks.jsonl. Keyword-only, but
                                  chunks are cleaned and pre-cited, so it
                                  still beats raw page text.
  text-bm25    manual.txt only  - BM25 over whole pages, citations parsed
                                  out of the running footer at query time.
  none         nothing built    - the page tells the user how to fix it.

Vector decode mirrors bundle_builder.py's writer exactly: b"RMS1" | int32 N |
int32 dim | N float32 scales | N*dim int8 components.
"""

from __future__ import annotations

import json
import math
import re
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# --- bundle format (bundle_builder.py writes it with these) -----------------

VECTOR_MAGIC = b"RMS1"
QUERY_PREFIX = "task: search result | query: "
EMBED_MODEL = "embeddinggemma"
RRF_K = 60

BUNDLE_CHUNKS = "chunks.jsonl"
BUNDLE_VECTORS = "vectors-int8-512.bin"

# 127.0.0.1, never "localhost": on Windows the name resolves to ::1 first,
# and the failed IPv6 connect costs a flat ~2.0s before falling back to IPv4.
# Measured: 2.09s per embed via localhost vs 0.046s here.
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_URL = f"{OLLAMA_HOST}/api/embed"
OLLAMA_TAGS_URL = f"{OLLAMA_HOST}/api/tags"
OLLAMA_TIMEOUT = 15

TIER_HYBRID = "hybrid"
TIER_BUNDLE_BM25 = "bundle-bm25"
TIER_TEXT_BM25 = "text-bm25"
TIER_NONE = "none"

TIER_BLURB = {
    TIER_HYBRID: "Semantic + keyword search over the built bundle.",
    TIER_BUNDLE_BM25: (
        "Keyword search over the built bundle. Start Ollama "
        "(with embeddinggemma pulled) for semantic search."
    ),
    TIER_TEXT_BM25: (
        "Keyword search over the plain text extract. Build the bundle "
        "for chunk-level citations, and run Ollama for semantic search."
    ),
    TIER_NONE: "No manual data found – answers will not be grounded.",
}

FOOTER_MARK = "|Chapter"
FOOTER_RE = re.compile(
    r"^(?P<section>[A-Za-z][\w &/,.'’-]*?)\s*\|\s*"
    r"Chapter\s*(?P<num>\d+)\s*(?P<title>.+?)\s*$"
)
TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


@dataclass
class Passage:
    """One retrieved excerpt, always carrying enough to cite it."""

    text: str
    page: int | None = None
    chapter_no: int | None = None
    chapter_title: str = ""
    section: str = ""
    score: float = 0.0

    def citation(self) -> str:
        bits = []
        if self.chapter_no is not None:
            bits.append(f"Chapter {self.chapter_no}")
        if self.chapter_title:
            bits.append(self.chapter_title)
        if self.page is not None:
            bits.append(f"p.{self.page}")
        return " – ".join(bits) if bits else "DaVinci Resolve manual"


class BM25:
    """Okapi BM25 with the standard parameters (k1 1.5, b 0.75)."""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs_tokens)
        self.lengths = [len(d) for d in docs_tokens]
        self.avg_len = (sum(self.lengths) / self.n) if self.n else 0.0
        self.freqs: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for tokens in docs_tokens:
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            self.freqs.append(tf)
            for t in tf:
                df[t] = df.get(t, 0) + 1
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }

    def search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        terms = [t for t in tokenize(query) if t in self.idf]
        if not terms:
            return []
        scores: list[tuple[int, float]] = []
        for i, tf in enumerate(self.freqs):
            s = 0.0
            for t in terms:
                f = tf.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (
                    1 - self.b + self.b * self.lengths[i] / (self.avg_len or 1)
                )
                s += self.idf[t] * f * (self.k1 + 1) / denom
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:limit]


def embed_query(query: str, timeout: int = OLLAMA_TIMEOUT) -> list[float] | None:
    """Embed via local Ollama, or None if it isn't reachable.

    The prefix is not optional - EmbeddingGemma is prefix-trained, and the
    bundle's document vectors were built with the matching document-side
    prefix. Dropping it silently degrades every result.
    """
    body = json.dumps(
        {"model": EMBED_MODEL, "input": [QUERY_PREFIX + query]}
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    vecs = payload.get("embeddings") or []
    return vecs[0] if vecs else None


class ManualRetriever:
    """Picks the best tier available at construction time.

    Construct once and reuse - loading the bundle parses ~10 MB and builds a
    BM25 index, which is fine once at page load but not per keystroke.
    """

    def __init__(self, bundle_dir: str | Path | None, text_path: str | Path | None):
        self.bundle_dir = Path(bundle_dir) if bundle_dir else None
        self.text_path = Path(text_path) if text_path else None
        self.tier = TIER_NONE
        self.chunks: list[dict] = []
        self.dim = 0
        self._vectors: bytes = b""
        self._scales: list[float] = []
        self._bm25: BM25 | None = None

        if self._load_bundle():
            self.tier = TIER_HYBRID if self.ollama_available() else TIER_BUNDLE_BM25
        elif self._load_text():
            self.tier = TIER_TEXT_BM25

    # ---------------------------------------------------------------- load

    def _load_bundle(self) -> bool:
        if not self.bundle_dir:
            return False
        chunks_path = self.bundle_dir / BUNDLE_CHUNKS
        vec_path = self.bundle_dir / BUNDLE_VECTORS
        if not chunks_path.exists():
            return False
        self.chunks = [
            json.loads(line)
            for line in chunks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not self.chunks:
            return False
        self._bm25 = BM25([tokenize(c.get("text", "")) for c in self.chunks])

        if vec_path.exists():
            raw = vec_path.read_bytes()
            if raw[:4] == VECTOR_MAGIC:
                n, dim = struct.unpack_from("<ii", raw, 4)
                if n == len(self.chunks):
                    off = 12
                    self._scales = list(struct.unpack_from(f"<{n}f", raw, off))
                    off += 4 * n
                    self._vectors = raw[off : off + n * dim]
                    self.dim = dim
        return True

    def _load_text(self) -> bool:
        if not self.text_path or not self.text_path.exists():
            return False
        pages = self.text_path.read_text(
            encoding="utf-8", errors="replace"
        ).split("\f")
        for i, page in enumerate(pages):
            if not page.strip():
                continue
            meta = self._parse_footer(page)
            self.chunks.append({"text": page.strip(), "page": i + 1, **meta})
        if not self.chunks:
            return False
        self._bm25 = BM25([tokenize(c["text"]) for c in self.chunks])
        return True

    @staticmethod
    def _parse_footer(page_text: str) -> dict:
        """Recover chapter metadata from the running footer pdftotext leaves
        in the page body, e.g. 'Setup and Workflows|Chapter 8Improving...'."""
        for line in reversed(page_text.split("\n")):
            if FOOTER_MARK not in line:
                continue
            m = FOOTER_RE.match(line.strip())
            if m:
                return {
                    "section": m.group("section"),
                    "chapter_no": int(m.group("num")),
                    "chapter_title": m.group("title").strip(),
                }
        return {}

    # -------------------------------------------------------------- search

    def ollama_available(self) -> bool:
        """Ask Ollama what it has rather than embedding a probe string.

        /api/tags needs no inference, so this stays fast even when the model
        is cold - and a cold embeddinggemma load is exactly the case a short
        probe timeout would misread as "Ollama isn't there", silently
        dropping the page to keyword-only search.
        """
        if self.dim <= 0:
            return False
        try:
            with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=3) as resp:
                models = json.load(resp).get("models") or []
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False
        return any(
            (m.get("name") or "").startswith(EMBED_MODEL) for m in models
        )

    def _vector_scores(self, qvec: list[float]) -> list[tuple[int, float]]:
        """Cosine against dequantized int8 vectors, via MRL-truncated query."""
        q = qvec[: self.dim]
        norm = math.sqrt(sum(x * x for x in q)) or 1.0
        q = [x / norm for x in q]
        out = []
        for i, scale in enumerate(self._scales):
            base = i * self.dim
            row = self._vectors[base : base + self.dim]
            dot = 0.0
            for j, byte in enumerate(row):
                dot += (byte - 256 if byte > 127 else byte) * q[j]
            out.append((i, dot * scale))
        out.sort(key=lambda x: -x[1])
        return out[:50]

    @staticmethod
    def _rrf(rankings: list[list[int]], limit: int) -> list[tuple[int, float]]:
        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, idx in enumerate(ranking):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        return sorted(fused.items(), key=lambda x: -x[1])[:limit]

    def search(self, query: str, limit: int = 5) -> list[Passage]:
        if not self._bm25 or not self.chunks:
            return []
        keyword = self._bm25.search(query, limit=50)
        rankings = [[i for i, _ in keyword]]

        if self.tier == TIER_HYBRID:
            qvec = embed_query(query)
            if qvec:
                rankings.append([i for i, _ in self._vector_scores(qvec)])
            else:
                # Ollama went away mid-session; keep answering, note the drop.
                self.tier = TIER_BUNDLE_BM25

        fused = self._rrf(rankings, limit) if len(rankings) > 1 else [
            (i, s) for i, s in keyword[:limit]
        ]
        results = []
        for idx, score in fused:
            c = self.chunks[idx]
            results.append(
                Passage(
                    text=c.get("text", ""),
                    page=c.get("page"),
                    chapter_no=c.get("chapter_no"),
                    chapter_title=c.get("chapter_title", ""),
                    section=c.get("section", ""),
                    score=score,
                )
            )
        return results

    def describe_tier(self) -> str:
        return TIER_BLURB.get(self.tier, "")
