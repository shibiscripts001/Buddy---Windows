"""Windows Defender's verdict on files Buddy downloaded and is about to run -
llama.cpp for Ask Buddy (pages/manual_chat/local_llama.py), Transcribe's
Python environment (pages/transcribe/env_setup.py). No Qt.

MpCmdRun's custom scan, which needs no admin rights: exit code 0 is clean,
2 is threats found, anything else is a scan that couldn't run (Defender
turned off for another antivirus, say - which checks files as they're
written anyway). -DisableRemediation reports without quarantining: the
caller deletes a flagged download whole rather than keep what's left of it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLEAN, THREAT, SKIPPED = "clean", "threat", "skipped"
SCAN_TIMEOUT = 900          # a 2 GB environment scans in well under a minute


def scan(folder: Path, what: str) -> tuple[str, str]:
    """(CLEAN | THREAT | SKIPPED, what to tell the person) for a folder.
    `what` names it in the message ("llama.cpp", "the transcription engine")."""
    if os.name != "nt":
        return SKIPPED, ""
    exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
    if not exe.is_file():
        return SKIPPED, f"Windows Defender isn't on this PC, so it couldn't scan {what}."
    try:
        done = subprocess.run([str(exe), "-Scan", "-ScanType", "3", "-File", str(folder), "-DisableRemediation"],
                              capture_output=True, timeout=SCAN_TIMEOUT, creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SKIPPED, f"Windows Defender couldn't scan {what}: {exc}"
    if done.returncode == 0:
        return CLEAN, f"Windows Defender scanned {what} and found no threats."
    if done.returncode == 2:
        return THREAT, ""
    return SKIPPED, (f"Windows Defender couldn't scan {what} (it may be turned off for another antivirus, "
                     f"code {done.returncode}).")
