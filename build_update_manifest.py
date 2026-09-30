#!/usr/bin/env python3
"""
Write dist/buddy-update.json - what a running Buddy reads to update itself
(app/core/updater.py) - for the release being built:

    python build_update_manifest.py --platform windows

The release workflow runs it after building the installer (so buddy.zip is
fresh) and attaches it, buddy.zip and Buddy.py to the release. It holds the
version (VERSION), each file's size and SHA-256, the packages the release
needs (installer/requirements.txt - a Buddy without one of them is sent to
the installer instead), and what changed: the commit subjects since the last
release's tag, version bumps left out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTES_MAX = 3000


def file_entry(path: Path) -> dict:
    data = path.read_bytes()
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def requirements(path: Path) -> list[dict]:
    """requirements.txt's packages: name, version spec, platform marker."""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec, _, marker = line.partition(";")
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec.strip())
        if name:
            out.append({"name": name.group(), "spec": spec.strip()[name.end():].replace(" ", ""),
                        "marker": marker.strip()})
    return out


def notes_since_last_release() -> str:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    last = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")
    subjects = git("log", "--no-merges", "--format=%s", f"{last}..HEAD" if last else "-20").splitlines()
    lines = [f"• {s}" for s in subjects if s and not re.match(r"(?i)bump version", s)]
    text = "\n".join(lines)
    return text if len(text) <= NOTES_MAX else text[:NOTES_MAX - 1].rsplit("\n", 1)[0] + "\n…"


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--platform", required=True, choices=["windows", "mac"])
    parser.add_argument("--out", default=str(ROOT / "dist" / "buddy-update.json"))
    args = parser.parse_args()
    manifest = {
        "format": 1,
        "platform": args.platform,
        "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "zip": file_entry(ROOT / "buddy.zip"),
        "launcher": file_entry(ROOT / "Buddy.py"),
        "requires": requirements(ROOT / "installer" / "requirements.txt"),
        "notes": notes_since_last_release(),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out}: Buddy {manifest['version']} ({manifest['zip']['size']:,} bytes)")


if __name__ == "__main__":
    main()
