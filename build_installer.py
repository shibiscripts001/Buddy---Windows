#!/usr/bin/env python3
"""
Build the Windows installer: dist/BuddySetup-<version>.exe.

    python build_installer.py

Steps: build buddy.zip fresh (build_buddy_zip.py, without deploying), stage
it with the Buddy.py launcher in build/, then compile installer/Buddy.iss
with Inno Setup's ISCC (version from the VERSION file). Nothing large is
bundled - Python and the packages are downloaded by the installer itself,
only when a PC is missing them - so the result is a few MB.

Needs Inno Setup 6 or 7 (jrsoftware.org, or `winget install
JRSoftware.InnoSetup`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
ISS = ROOT / "installer" / "Buddy.iss"


def find_iscc() -> Path:
    candidates = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")):
        for version in ("7", "6"):
            if base:
                candidates.append(Path(base) / f"Inno Setup {version}" / "ISCC.exe")
    found = shutil.which("iscc")
    if found:
        candidates.insert(0, Path(found))
    for c in candidates:
        if c.is_file():
            return c
    sys.exit("Inno Setup's ISCC.exe wasn't found. Install Inno Setup 6 or 7 (jrsoftware.org).")


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    iscc = find_iscc()
    print(f"Buddy {version} - using {iscc}")

    subprocess.run([sys.executable, str(ROOT / "build_buddy_zip.py")], check=True, cwd=ROOT)
    BUILD.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "buddy.zip", BUILD / "buddy.zip")
    shutil.copy2(ROOT / "Buddy.py", BUILD / "Buddy.py")

    subprocess.run([str(iscc), "/Q", f"/DAppVersion={version}", str(ISS)], check=True, cwd=ISS.parent)
    out = ROOT / "dist" / f"BuddySetup-{version}.exe"
    print(f"Built {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
