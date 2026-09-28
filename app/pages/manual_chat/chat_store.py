"""Crash-safe, local storage for Ask Buddy conversations."""

from __future__ import annotations

from pathlib import Path

from core.atomic_io import read_json, write_json

from .conversation import ChatSessions


FILE_NAME = "conversations.json"


def load(folder):
    path = Path(folder) / FILE_NAME
    warnings = []
    data = read_json(str(path), default=None, warnings=warnings)
    return (ChatSessions.from_data(data) if data is not None else ChatSessions(), warnings)


def save(folder, chats: ChatSessions):
    write_json(str(Path(folder) / FILE_NAME), chats.to_data())
