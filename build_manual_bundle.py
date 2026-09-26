#!/usr/bin/env python3
"""Builds Ask Buddy's manual bundle from the DaVinci Resolve Reference Manual PDF.

The same builder the "Rebuild from PDF..." button in Settings > Ask Buddy
runs (app/pages/manual_chat/bundle_builder.py), for building from a
terminal instead. Needs PyMuPDF (+ pymupdf4llm for headings and tables),
and Ollama running with embeddinggemma pulled for semantic search - without
Ollama the bundle is built keyword-only.

The previous bundle is kept alongside as "<out>.previous".

Usage:
    python build_manual_bundle.py DavinciManual.pdf
    python build_manual_bundle.py DavinciManual.pdf --out path/to/bundle
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

from pages.manual_chat.bundle_builder import (  # noqa: E402
    BuildCancelled,
    BuildError,
    build_bundle,
)

# Where Ask Buddy looks first (DATA_CANDIDATES in pages/manual_chat/page.py).
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), ".buddy", "manual", "bundle")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", help="the Reference Manual PDF")
    parser.add_argument(
        "--out", default=DEFAULT_OUT, help=f"bundle folder (default: {DEFAULT_OUT})"
    )
    parser.add_argument(
        "--require-vectors",
        action="store_true",
        help="fail instead of building keyword-only when Ollama is unavailable",
    )
    args = parser.parse_args()

    last_stage = [None]

    def progress(stage, done, total, message):
        # One line per stage, rewritten in place as its count climbs.
        if last_stage[0] not in (None, stage):
            print()
        last_stage[0] = stage
        print(f"\r  {message}".ljust(60), end="", flush=True)

    try:
        result = build_bundle(
            args.pdf, args.out, progress=progress, require_vectors=args.require_vectors
        )
    except BuildCancelled:
        raise SystemExit("\nCancelled.")
    except BuildError as exc:
        raise SystemExit(f"\n{exc}")
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted - the existing bundle was left untouched.")

    print(
        f"\nBuilt {result.chunks} passages from {result.chapters} chapters "
        f"({result.pages} pages) in {result.seconds / 60:.1f} min"
        f"{'' if result.has_vectors else ', keyword-only'}.\n  -> {result.out_dir}"
    )
    for warning in result.warnings:
        print(f"Note: {warning}")


if __name__ == "__main__":
    main()
