#!/usr/bin/env python3
"""
Ask Buddy's own folder, ~/.buddy/ask_buddy - what the user adds to what
Ask Buddy knows, kept apart from the manual (~/.buddy/manual, which Buddy
builds and can rebuild at any time):

  instructions.md   Custom instructions, sent with every question - their
                    delivery specs, naming conventions, how they like
                    answers. Edited in Settings > Ask Buddy or in any text
                    editor; the file is the only copy, so both see the same
                    text.

No Qt in here: settings_panel.py edits the instructions, page.py reads them
each time it builds the agent (once per question), so a change - from
either place - applies to the next question.
"""

from __future__ import annotations

import os
import tempfile

from core.settings_store import BUDDY_DIR

ASK_BUDDY_DIR = os.path.join(BUDDY_DIR, "ask_buddy")
INSTRUCTIONS_NAME = "instructions.md"

# What's sent with every question. Past this the rest of the file is kept
# but not sent - every question pays for these tokens, and a long enough
# file would crowd out the manual excerpts the answer is built from.
MAX_INSTRUCTION_CHARS = 8000


def instructions_path(folder: str = ASK_BUDDY_DIR) -> str:
    return os.path.join(folder, INSTRUCTIONS_NAME)


def read_instructions(folder: str = ASK_BUDDY_DIR) -> str:
    """The whole file, or "" when there isn't one (or it can't be read -
    Ask Buddy answers without them rather than not at all)."""
    try:
        with open(instructions_path(folder), encoding="utf-8-sig") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return ""


def instructions_for_prompt(folder: str = ASK_BUDDY_DIR) -> str:
    """What goes to the model: trimmed, and cut at MAX_INSTRUCTION_CHARS."""
    return read_instructions(folder).strip()[:MAX_INSTRUCTION_CHARS].rstrip()


def write_instructions(text: str, folder: str = ASK_BUDDY_DIR) -> None:
    """Saves `text` whole (a temp file swapped in, so a crash mid-save keeps
    the old file). Blank text removes the file."""
    path = instructions_path(folder)
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=INSTRUCTIONS_NAME + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def ensure_folder(folder: str = ASK_BUDDY_DIR) -> str:
    """The folder, created if need be - for "Open folder"."""
    os.makedirs(folder, exist_ok=True)
    return folder
