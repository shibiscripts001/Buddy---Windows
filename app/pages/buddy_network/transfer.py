"""Moving Buddy Network to another PC - Windows or Mac - with one file. No Qt
here.

The file holds, for one server: the identity (id and token, like the
recovery code), this PC's encryption keys (e2e.py - the current one and any
older ones, so old DMs still open), what's known about buddies' keys
(including safety codes already checked), and optionally the direct
messages saved on this PC (archive.py).

It's locked with a password the user picks (any password - or none, if
they choose), not with Windows: scrypt stretches the password (slow on
purpose, so guessing takes ages), and ChaCha20-Poly1305 encrypts the
contents - a wrong password or a changed byte just fails to open. The
header says whether a password was set ("password"), so a file saved
without one opens without asking. The file (plus its password, if any) is
enough to become that user and read their DMs, so the page says so and
asks for the file to be deleted once it's moved.
"""

from __future__ import annotations

import json
import os

from . import e2e

KIND = "buddy-network-transfer"
VERSION = 1
EXTENSION = ".buddynet"
MAX_FILE_BYTES = 64 * 1024 * 1024
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 17, 8, 1   # ~128 MB and a fifth of a second here
# A file asking for more is refused: memory is 128 * n * r * p bytes, and a
# made-up file could otherwise ask for gigabytes.
_SCRYPT_N_MAX, _SCRYPT_R_MAX, _SCRYPT_P_MAX = 2 ** 18, 8, 2


class TransferError(Exception):
    """The file can't be opened - shown to the user as it is."""


class NeedsPassword(TransferError):
    """read() was given no password, and this file has one."""


def password_problem(password: str, confirm: str | None = None) -> str:
    """"" if the password will do, else why not. Any password will - or
    none; it only has to be typed the same twice."""
    if confirm is not None and confirm != password:
        return "The two passwords don't match."
    return ""


def _key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def seal(payload: dict, password: str) -> str:
    """The file's text."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {"kind": KIND, "v": VERSION, "password": bool(password),
              "kdf": {"name": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P, "salt": e2e.b64(salt)}}
    aad = json.dumps(header, sort_keys=True).encode("utf-8")
    body = ChaCha20Poly1305(_key(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)).encrypt(
        nonce, json.dumps(payload, ensure_ascii=False).encode("utf-8"), aad)
    return json.dumps({**header, "nonce": e2e.b64(nonce), "data": e2e.b64(body)})


def open_file(text: str, password: str) -> dict:
    """The payload, or TransferError."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    try:
        outer = json.loads(text)
        kdf = outer["kdf"]
        ok = (outer.get("kind") == KIND and kdf.get("name") == "scrypt"
              and all(isinstance(kdf.get(k), int) and not isinstance(kdf.get(k), bool) for k in "nrp"))
    except (ValueError, KeyError, TypeError, AttributeError):
        ok = False
    if not ok:
        raise TransferError("That isn't a Buddy Network transfer file.")
    if outer.get("v") != VERSION:
        raise TransferError("That transfer file is from a newer Buddy – update Buddy first.")
    n, r, p = kdf["n"], kdf["r"], kdf["p"]
    salt, nonce, body = e2e.unb64(kdf.get("salt"), 16), e2e.unb64(outer.get("nonce"), 12), e2e.unb64(outer.get("data"))
    if (salt is None or nonce is None or body is None or not 2 <= n <= _SCRYPT_N_MAX or n & (n - 1)
            or not 1 <= r <= _SCRYPT_R_MAX or not 1 <= p <= _SCRYPT_P_MAX):
        raise TransferError("That transfer file is damaged.")
    header = {"kind": KIND, "v": VERSION, "kdf": {"name": "scrypt", "n": n, "r": r, "p": p, "salt": kdf["salt"]}}
    if isinstance(outer.get("password"), bool):   # files from before it was optional don't say
        header["password"] = outer["password"]
    try:
        data = ChaCha20Poly1305(_key(password, salt, n, r, p)).decrypt(
            nonce, body, json.dumps(header, sort_keys=True).encode("utf-8"))
        payload = json.loads(data.decode("utf-8"))
    except InvalidTag:
        raise TransferError("That password doesn't open this file (or the file was changed).") from None
    except (ValueError, UnicodeDecodeError):
        raise TransferError("That transfer file is damaged.") from None
    if not isinstance(payload, dict):
        raise TransferError("That transfer file is damaged.")
    identity = payload.get("identity")
    if (not isinstance(payload.get("server"), str) or not isinstance(identity, dict)
            or not all(isinstance(identity.get(k), str) and identity[k] for k in ("id", "token"))
            or not isinstance(payload.get("keys"), dict) or not isinstance(payload.get("chats", []), list)):
        raise TransferError("That transfer file is damaged.")
    return payload


def has_password(text: str) -> bool:
    """False only for a file saved without one (older files always had one)."""
    try:
        return json.loads(text).get("password") is not False
    except (ValueError, AttributeError):
        return True


def read(path: str, password: str | None = None) -> dict:
    """password None: open a file saved without one, else NeedsPassword."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise TransferError("That file is far too big to be a Buddy Network transfer file.")
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        raise TransferError("That isn't a Buddy Network transfer file.") from None
    except OSError as exc:
        raise TransferError(f"The file couldn't be read: {exc}") from None
    if password is None:
        if has_password(text):
            raise NeedsPassword("This transfer file has a password.")
        password = ""
    return open_file(text, password)
