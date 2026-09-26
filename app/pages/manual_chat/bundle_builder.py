#!/usr/bin/env python3
"""
Build the manual bundle (chunks.jsonl + vectors-int8-512.bin + sidecars)
straight from the Resolve Reference Manual PDF, so a new manual release
needs nothing but the PDF and a button press.

No Qt in here: the Settings dialog runs build_bundle() on a QThread, and
build_manual_bundle.py at the repo root runs it from a terminal. Both pass
a progress callback and a cancel check; neither changes what gets written.

Pipeline, one pass per stage:

  1. Extract   Every page twice. Plain get_text() is fast and keeps the
               running footer intact ("Setup and Workflows | Chapter 8
               Improving Performance..."), which is where the chapter
               metadata comes from - the PDF's own bookmarks have 217
               mangled entries for 203 chapters, the footer has the real
               ones. pymupdf4llm gives the Markdown body (headings, tables)
               that becomes the chunk text. OCR is forced off: every page
               already has a text layer, and with OCR on the full manual
               takes ~37 min instead of ~6.
  2. Clean     Drop the footer, the page number, the side-tab label (FUSION,
               MEDIA...) and chapter-contents dot leaders. Leaders are pure
               keyword noise to BM25: "Clip Resolution....296" outranks the
               page that actually explains clip resolution.
  3. Chunk     Paragraphs packed up to MAX_CHARS within one chapter, cut at
               headings once a chunk is big enough, cited by start page.
  4. Embed     Local Ollama, embeddinggemma, document-side prefix. Optional:
               without Ollama the bundle is written keyword-only and Buddy
               runs at the bundle-bm25 tier.
  5. Write     Into a sibling "<name>.building" dir, then swapped in, so a
               cancelled or failed build never leaves Buddy a half bundle.

The vector format is checked against the bundle this replaces: re-embedding
its stored chunks with DOC_PREFIX reproduces the stored vectors at cosine
0.99997 (int8 rounding), while "title: none" drops to 0.94 and no prefix to
0.93. That is why the prefix carries the real chapter title.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from .retrieval import (
        BUNDLE_CHUNKS, BUNDLE_VECTORS, EMBED_MODEL, OLLAMA_HOST, VECTOR_MAGIC,
    )
except ImportError:  # run as a plain script, no package context
    from retrieval import (  # type: ignore[no-redef]
        BUNDLE_CHUNKS, BUNDLE_VECTORS, EMBED_MODEL, OLLAMA_HOST, VECTOR_MAGIC,
    )

DOC_PREFIX = "title: {title} | text: "
FULL_DIM = 768
MRL_DIM = 512

BUNDLE_CHAPTERS = "chapters.json"
BUNDLE_META = "meta.json"
# Not read by Buddy, but older bundles may hold it, and a rebuild
# shouldn't be what deletes it.
CARRY_OVER = ("notes_chunks.jsonl",)

MAX_CHARS = 950
MIN_CHARS = 350        # below this a heading doesn't start a new chunk
EXTRACT_BATCH = 40     # pages per pymupdf4llm call - also the cancel grain
EMBED_BATCH = 64       # measured: 64 is ~30% faster than 16, 1.3 min total
EMBED_TIMEOUT = 300

OLLAMA_EMBED_URL = f"{OLLAMA_HOST}/api/embed"
OLLAMA_TAGS_URL = f"{OLLAMA_HOST}/api/tags"

LICENSE_NOTE = (
    "Bundle contains text extracted from the DaVinci Resolve Reference "
    "Manual, (c) Blackmagic Design. Built locally by the user; do not "
    "redistribute unless you have determined your use is permitted."
)

# The footer after NFKC + whitespace collapse. The PDF separates the parts
# with U+2002/U+2003, which NFKC folds to plain spaces.
FOOTER_RE = re.compile(
    r"^(?P<section>[^|]+?)\s*\|\s*Chapter\s*(?P<num>\d+)\s+(?P<title>.+?)$"
)
# The coloured side tab on every page. Measured over the 21 manual: these
# 13 labels, nothing else. Matched only as a whole line, so a sentence
# that happens to say "Fusion" is untouched.
SIDE_TABS = {
    "FUSION", "MEDIA", "COLOR", "EDIT", "FAIRLIGHT", "RESOLVE FX", "DELIVER",
    "PHOTO", "INTRO", "IMMERSIVE", "CLOUD", "COLLAB", "OTHER",
}
# Dot leaders come through pymupdf4llm as U+FFFD or \x08 runs, plain text
# as periods. Five in a row never occurs in prose.
LEADER_RE = re.compile(r"[.�\x08…]{5,}")
PAGE_NO_RE = re.compile(r"^\**\s*\d{1,5}\s*\**$")


class BuildError(RuntimeError):
    """A build that cannot go ahead, with a message fit to show the user."""


class BuildCancelled(Exception):
    pass


@dataclass
class BuildResult:
    out_dir: Path
    chunks: int
    chapters: int
    pages: int
    has_vectors: bool
    seconds: float
    warnings: list[str] = field(default_factory=list)


# (stage, done, total, message). Stages: "extract", "chunk", "embed",
# "write". total is 0 for a stage with no meaningful count.
Progress = Callable[[str, int, int, str], None]


# ----------------------------------------------------------- dependencies


def pymupdf_status() -> tuple[bool, bool]:
    """(pymupdf importable, pymupdf4llm importable) for THIS interpreter."""
    import importlib.util

    has_fitz = bool(
        importlib.util.find_spec("pymupdf") or importlib.util.find_spec("fitz")
    )
    return has_fitz, bool(importlib.util.find_spec("pymupdf4llm"))


def install_hint() -> str:
    # sys.executable, not "pip": inside Resolve it is Resolve's interpreter,
    # which is rarely the one on PATH. Same reasoning as Buddy.py's launcher.
    return f'"{sys.executable}" -m pip install pymupdf pymupdf4llm'


def ollama_status() -> tuple[bool, bool]:
    """(Ollama reachable, embeddinggemma pulled). Asks /api/tags, which
    needs no inference, so a cold model doesn't read as a missing one."""
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=3) as resp:
            models = json.load(resp).get("models") or []
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False, False
    return True, any(
        (m.get("name") or "").startswith(EMBED_MODEL) for m in models
    )


def _open_pdf(path: Path):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore[no-redef]
        except ImportError:
            raise BuildError(
                "PyMuPDF is not installed for the Python running Buddy "
                f"({sys.executable}).\n\nInstall it with:\n{install_hint()}"
            ) from None
    try:
        return pymupdf.open(str(path))
    except Exception as exc:  # pymupdf raises its own FileDataError etc.
        raise BuildError(f"Could not open {path.name} as a PDF: {exc}") from exc


# ------------------------------------------------------------ extraction


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", line)).strip()


def _plain_md(line: str) -> str:
    """A Markdown line minus emphasis/heading marks, for matching
    boilerplate. Keeps "|": the footer's own separator is one, and a table
    row still starts with one, so neither can be mistaken for the other."""
    return _norm(re.sub(r"[*#_`]", " ", line))


def parse_footer(page_text: str) -> dict | None:
    lines = [_norm(l) for l in page_text.splitlines() if l.strip()]
    for line in reversed(lines[-6:]):
        m = FOOTER_RE.match(line)
        if m:
            return {
                "section": m.group("section").strip(),
                "chapter_no": int(m.group("num")),
                "chapter_title": m.group("title").strip(),
            }
    return None


TABLE_RULE_RE = re.compile(r"^\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*$")


def clean_page(markdown: str) -> str:
    kept: list[str] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if TABLE_RULE_RE.match(line.strip()):
            # A table's header rule. Kept only while the header row above
            # it survived - a chapter-contents table loses every row to the
            # leader filter and would otherwise leave a bare "|---|".
            if kept and kept[-1].lstrip().startswith("|"):
                kept.append(line)
            continue
        bare = _plain_md(line)
        if not bare:
            kept.append("")
            continue
        if FOOTER_RE.match(bare) or PAGE_NO_RE.match(bare) or bare in SIDE_TABS:
            continue
        if LEADER_RE.search(line):
            continue
        kept.append(unicodedata.normalize("NFKC", line).replace("�", ""))
    text = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract(doc, progress: Progress, cancelled) -> list[dict]:
    """One dict per page: page (1-based), text (cleaned), and footer meta."""
    try:
        import pymupdf4llm
    except ImportError:
        pymupdf4llm = None

    n = doc.page_count
    pages: list[dict] = []
    for start in range(0, n, EXTRACT_BATCH):
        if cancelled():
            raise BuildCancelled()
        idx = list(range(start, min(start + EXTRACT_BATCH, n)))
        if pymupdf4llm is not None:
            md = pymupdf4llm.to_markdown(
                doc, pages=idx, page_chunks=True, show_progress=False,
                use_ocr=False,
            )
            bodies = [p.get("text", "") for p in md]
            if len(bodies) != len(idx):
                raise BuildError(
                    f"pymupdf4llm returned {len(bodies)} pages for a batch "
                    f"of {len(idx)} (pages {idx[0] + 1}-{idx[-1] + 1})."
                )
        else:
            bodies = [doc[i].get_text() for i in idx]
        for i, body in zip(idx, bodies):
            pages.append({
                "page": i + 1,
                "text": clean_page(body),
                "meta": parse_footer(doc[i].get_text()),
            })
        done = idx[-1] + 1
        progress("extract", done, n, f"Reading page {done} of {n}")
    return pages


# -------------------------------------------------------------- chunking


def _split_long(paragraph: str) -> list[str]:
    """A paragraph over MAX_CHARS, cut at lines, then sentences, then words."""
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    for sep in ("\n", ". ", " "):
        parts = paragraph.split(sep)
        if len(parts) == 1:
            continue
        out, cur = [], ""
        for i, part in enumerate(parts):
            piece = part + (sep if i < len(parts) - 1 else "")
            if cur and len(cur) + len(piece) > MAX_CHARS:
                out.append(cur.rstrip())
                cur = ""
            cur += piece
        if cur.strip():
            out.append(cur.rstrip())
        if all(len(p) <= MAX_CHARS for p in out):
            return out
        return [q for p in out for q in _split_long(p)]
    return [paragraph[i:i + MAX_CHARS] for i in range(0, len(paragraph), MAX_CHARS)]


def chunk_pages(pages: list[dict]) -> tuple[list[dict], dict]:
    """(chunks, chapters). Chunks never span chapters, so every chunk's
    chapter citation is exact; they may span pages and cite the first."""
    chunks: list[dict] = []
    chapters: dict[int, dict] = {}
    seen_chapter = False

    cur: list[str] = []
    cur_len = 0
    cur_page = None
    cur_meta: dict = {}

    def flush():
        nonlocal cur, cur_len, cur_page
        text = "\n\n".join(cur).strip()
        if text:
            chunks.append({
                "id": len(chunks),
                "text": text,
                "section": cur_meta.get("section", ""),
                "chapter_no": cur_meta.get("chapter_no", 0),
                "chapter_title": cur_meta.get("chapter_title", ""),
                "page": cur_page,
            })
        cur, cur_len, cur_page = [], 0, None

    for p in pages:
        meta = p["meta"]
        if meta:
            seen_chapter = True
            ch = chapters.setdefault(meta["chapter_no"], {
                "title": meta["chapter_title"],
                "section": meta["section"],
                "start_page": p["page"],
                "end_page": p["page"],
            })
            ch["end_page"] = p["page"]
        else:
            # Cover, contents, part dividers, the quick reference at the
            # back - real pages with no chapter. Chapter 0, so citations
            # still carry a page number.
            meta = {
                "section": "",
                "chapter_no": 0,
                "chapter_title": "Back Matter" if seen_chapter else "Front Matter",
            }
        if meta.get("chapter_no") != cur_meta.get("chapter_no") or (
            meta.get("chapter_title") != cur_meta.get("chapter_title")
        ):
            flush()
            cur_meta = meta

        for para in re.split(r"\n\s*\n", p["text"]):
            para = para.strip()
            if not para:
                continue
            for piece in _split_long(para):
                is_heading = piece.startswith("#")
                if cur and (
                    cur_len + len(piece) + 2 > MAX_CHARS
                    or (is_heading and cur_len >= MIN_CHARS)
                ):
                    flush()
                if cur_page is None:
                    cur_page = p["page"]
                cur.append(piece)
                cur_len += len(piece) + 2
    flush()

    # Tail fragments - a heading orphaned at a chapter's end - carry no
    # answer on their own and only dilute BM25's length normalisation.
    chunks = [c for c in chunks if len(c["text"]) >= 40]
    for i, c in enumerate(chunks):
        c["id"] = i

    out = {
        str(n): {
            "title": c["title"],
            "section": c["section"],
            "start_page": c["start_page"],
            "pages": c["end_page"] - c["start_page"] + 1,
        }
        for n, c in sorted(chapters.items())
    }
    out["0"] = {"title": "Front Matter", "section": "Front", "start_page": None, "pages": 1}
    return chunks, out


# ------------------------------------------------------------- embedding


def _embed(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_EMBED_URL, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            vecs = json.load(resp).get("embeddings") or []
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise BuildError(f"Ollama stopped answering mid-build: {exc}") from exc
    if len(vecs) != len(texts):
        raise BuildError(
            f"Ollama returned {len(vecs)} embeddings for {len(texts)} chunks."
        )
    return vecs


def quantize(vec: list[float]) -> tuple[float, bytes]:
    """MRL-truncate, L2-normalise, int8 with one scale per row - the exact
    inverse of retrieval.ManualRetriever._vector_scores."""
    v = vec[:MRL_DIM]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    v = [x / norm for x in v]
    peak = max((abs(x) for x in v), default=0.0)
    scale = (peak / 127.0) or 1.0
    q = bytes((max(-127, min(127, round(x / scale))) & 0xFF) for x in v)
    return scale, q


def _embed_all(chunks, progress: Progress, cancelled) -> bytes:
    n = len(chunks)
    scales: list[float] = []
    rows: list[bytes] = []
    for start in range(0, n, EMBED_BATCH):
        if cancelled():
            raise BuildCancelled()
        batch = chunks[start:start + EMBED_BATCH]
        texts = [
            DOC_PREFIX.format(title=c["chapter_title"] or "none") + c["text"]
            for c in batch
        ]
        for vec in _embed(texts):
            if len(vec) < MRL_DIM:
                raise BuildError(
                    f"{EMBED_MODEL} returned {len(vec)}-dim vectors; the "
                    f"bundle format needs at least {MRL_DIM}."
                )
            s, q = quantize(vec)
            scales.append(s)
            rows.append(q)
        done = min(start + EMBED_BATCH, n)
        progress("embed", done, n, f"Embedding chunk {done} of {n}")
    return (
        VECTOR_MAGIC
        + struct.pack("<ii", n, MRL_DIM)
        + struct.pack(f"<{n}f", *scales)
        + b"".join(rows)
    )


# --------------------------------------------------------------- driver


def _no_progress(stage, done, total, message):
    pass


def build_bundle(
    pdf_path: str | Path,
    out_dir: str | Path,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] | None = None,
    require_vectors: bool = False,
) -> BuildResult:
    """Build a bundle from pdf_path into out_dir, replacing what is there.

    The previous bundle is kept as "<out_dir>.previous" (one generation) so
    a bad manual extraction can be rolled back by renaming a folder.
    """
    t0 = time.time()
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    progress = progress or _no_progress
    cancelled = cancelled or (lambda: False)
    warnings: list[str] = []

    if not pdf_path.is_file():
        raise BuildError(f"No PDF at {pdf_path}.")
    has_fitz, has_llm = pymupdf_status()
    if not has_fitz:
        raise BuildError(
            "PyMuPDF is not installed for the Python running Buddy "
            f"({sys.executable}).\n\nInstall it with:\n{install_hint()}"
        )
    if not has_llm:
        warnings.append(
            "pymupdf4llm is not installed, so chunks are plain text without "
            "headings or tables. Install it for better chunks: "
            + install_hint()
        )
    reachable, has_model = ollama_status()
    embed = reachable and has_model
    if not embed:
        why = (
            "Ollama is not running" if not reachable
            else f"Ollama has no {EMBED_MODEL} model (ollama pull {EMBED_MODEL})"
        )
        if require_vectors:
            raise BuildError(f"{why}, and vectors were required.")
        warnings.append(
            f"{why}: built a keyword-only bundle. Rebuild with it running "
            "for semantic search."
        )

    doc = _open_pdf(pdf_path)
    staging = out_dir.with_name(out_dir.name + ".building")
    try:
        pdf_meta = dict(doc.metadata or {})
        page_count = doc.page_count
        pages = _extract(doc, progress, cancelled)
        if not any(p["meta"] for p in pages):
            raise BuildError(
                f"{pdf_path.name} has no 'Section | Chapter N Title' page "
                "footers. Is it the DaVinci Resolve Reference Manual?"
            )

        progress("chunk", 0, 0, "Splitting into passages")
        chunks, chapters = chunk_pages(pages)
        if not chunks:
            raise BuildError(f"No text could be extracted from {pdf_path.name}.")

        vectors = _embed_all(chunks, progress, cancelled) if embed else None

        if cancelled():
            raise BuildCancelled()
        progress("write", 0, 0, "Writing the bundle")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with open(staging / BUNDLE_CHUNKS, "w", encoding="utf-8", newline="\n") as f:
            for c in chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        (staging / BUNDLE_CHAPTERS).write_text(
            json.dumps(chapters, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        if vectors is not None:
            (staging / BUNDLE_VECTORS).write_bytes(vectors)
        meta = {
            "model": EMBED_MODEL if vectors is not None else None,
            "vector": (
                {"full_dim": FULL_DIM, "mrl_dim": MRL_DIM, "dtype": "int8"}
                if vectors is not None else None
            ),
            "prefixes": {
                "query": "task: search result | query: ",
                "document": "title: <chapter title> | text: ",
            },
            "chunks": len(chunks),
            "chapter_count": len(chapters) - 1,
            "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": {
                "file": pdf_path.name,
                "title": pdf_meta.get("title", ""),
                "pdf_modified": pdf_meta.get("modDate", ""),
                "pages": page_count,
                "extractor": "pymupdf4llm" if has_llm else "pymupdf",
            },
            "license_note": LICENSE_NOTE,
        }
        (staging / BUNDLE_META).write_text(
            json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        for name in CARRY_OVER:
            if (out_dir / name).exists():
                shutil.copy2(out_dir / name, staging / name)

        previous = out_dir.with_name(out_dir.name + ".previous")
        if out_dir.exists():
            if previous.exists():
                shutil.rmtree(previous)
            out_dir.rename(previous)
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        doc.close()

    return BuildResult(
        out_dir=out_dir,
        chunks=len(chunks),
        chapters=len(chapters) - 1,
        pages=page_count,
        has_vectors=vectors is not None,
        seconds=time.time() - t0,
        warnings=warnings,
    )


def read_bundle_meta(bundle_dir: str | Path) -> dict:
    """meta.json of an existing bundle, or {} - for the Settings summary."""
    try:
        return json.loads((Path(bundle_dir) / BUNDLE_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
