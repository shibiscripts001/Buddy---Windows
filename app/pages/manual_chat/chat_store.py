"""Crash-safe, local storage for Ask Buddy conversations."""

from __future__ import annotations

import os
from pathlib import Path

from core.atomic_io import backup_path, read_json, write_json

from .conversation import ChatSessions


FILE_NAME = "conversations.json"


def load(folder):
    path = Path(folder) / FILE_NAME
    warnings = []
    data = read_json(str(path), default=None, warnings=warnings)
    return (ChatSessions.from_data(data) if data is not None else ChatSessions(), warnings)


def save(folder, chats: ChatSessions, forget_backup=False):
    """forget_backup after a delete: write_json keeps the previous save as
    the .bak, and a deleted conversation must not live on in there."""
    path = str(Path(folder) / FILE_NAME)
    write_json(path, chats.to_data())
    if forget_backup:
        _remove(backup_path(path))


def erase(folder):
    """Delete all: the file, its .bak, and any damaged copies read_json set
    aside (.corrupt-N) - those hold conversations too."""
    folder = Path(folder)
    if not folder.is_dir():
        return
    for entry in folder.iterdir():
        name = entry.name
        if name == FILE_NAME or name.startswith(FILE_NAME + ".bak") or name.startswith(FILE_NAME + ".corrupt-"):
            _remove(str(entry))


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
