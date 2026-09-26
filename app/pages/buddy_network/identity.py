"""The Buddy Network identity saved on this PC: for each server address,
the user id and secret token that server issued. Losing the token loses
the account (there's no email to recover it by), so it's saved with
core/atomic_io - never half-written, with a .bak copy - in its own file
rather than the page's general settings.json.

The token is the whole account - anyone with a copy could sign in as you -
so on disk it's locked to this Windows account like the encryption keys
(e2e.protect, DPAPI): a copy of the file (a backup, a synced folder) is no
use anywhere else. Moving to another PC is what transfer files and the
recovery code are for. Files from before the lock are locked on first read.
"""

from __future__ import annotations

import os

from core import atomic_io

from . import e2e

FILENAME = "identity.json"


class IdentityStore:
    def __init__(self, folder: str):
        self.path = os.path.join(folder, FILENAME)
        self.warnings: list[str] = []
        try:
            data = atomic_io.read_json(self.path, default={}, warnings=self.warnings)
        except atomic_io.CorruptFileError:
            # Unreadable, and so is the .bak: keep it for the user to look
            # at, and start again rather than overwriting it later.
            self._set_aside("Your saved Buddy Network identity couldn't be read, so a new "
                            "one will be made. The damaged file was kept next to it.")
            data = {}
        servers = data.get("servers") if isinstance(data, dict) else None
        self._servers: dict = {}
        plain = False
        for url, entry in (servers if isinstance(servers, dict) else {}).items():
            if not isinstance(entry, dict):
                continue
            if "token_locked" in entry:
                try:
                    token = e2e.unprotect(str(entry.get("protection", "")), str(entry["token_locked"])).decode("utf-8")
                except (e2e.CryptoError, OSError, UnicodeDecodeError):
                    # Locked by another Windows account or PC: kept aside, never overwritten.
                    self._set_aside("Your saved Buddy Network identity was locked on another PC or Windows "
                                    "account, so it can't be used here – a new one will be made. Use a "
                                    "transfer file or your recovery code to bring an identity to this PC.")
                    self._servers = {}
                    return
            else:
                token, plain = str(entry.get("token") or ""), True
            self._servers[url] = {"id": entry.get("id"), "token": token}
        if plain and self._servers:
            try:
                self._write()
            except (OSError, e2e.CryptoError):
                pass   # stays as it was; locked on the next save

    def _set_aside(self, warning: str):
        try:
            atomic_io.set_aside(self.path)
        except OSError:
            pass
        self.warnings.append(warning)

    def get(self, server_url: str) -> dict:
        entry = self._servers.get(server_url)
        return entry if isinstance(entry, dict) else {}

    def token(self, server_url: str) -> str:
        return str(self.get(server_url).get("token") or "")

    def save(self, server_url: str, user_id: str, token: str):
        self._servers[server_url] = {"id": user_id, "token": token}
        self._write()

    def forget(self, server_url: str):
        if self._servers.pop(server_url, None) is not None:
            self._write()

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        servers = {}
        for url, entry in self._servers.items():
            how, locked = e2e.protect(str(entry.get("token") or "").encode("utf-8"))
            servers[url] = {"id": entry.get("id"), "protection": how, "token_locked": locked}
        data = {"servers": servers}
        atomic_io.write_json(self.path, data)
        # Twice: the .bak copy is the file as it was, which may still have
        # held a token in the open (or one since forgotten).
        atomic_io.write_json(self.path, data)
        return True
