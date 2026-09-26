"""End-to-end encryption for Buddy Network's direct messages - no Qt here.

Each PC has its own key pair (X25519), made the first time Buddy Network
connects and kept in keys.json next to the identity, with the private half
locked to this Windows account (DPAPI - a copy of the file is no use
anywhere else). Only the public half goes to the server, which hands it to
the user's buddies in their buddy list.

Sending a DM (encrypt):
  1. A fresh random 32-byte message key encrypts the text
     (ChaCha20-Poly1305).
  2. That message key is then locked for every PC of both people - the
     buddy's, and your own so your other PCs and this one can read it back
     later: for each, X25519 between this PC's private key and that PC's
     public key, through HKDF-SHA256 with a random per-message salt, gives a
     one-time key that encrypts the message key.
  3. The room, the sender's user id and the sending PC are bound in as
     associated data, so the server can't move a message to another
     conversation or pass it off as someone else's.
The server stores and forwards the result but can't read it. A DM's image
(encrypt_image) is encrypted the same way under a key of its own, with the
image's id bound in too. Reading
(decrypt) only trusts sender keys this PC has seen for that person (the
KeyStore keeps every one it has been shown), never one inside the message -
and a message from a key that hasn't been accepted yet is marked as such
(the page shows a warning on it and doesn't save it).

What this doesn't do: forward secrecy (a stolen private key opens the
messages it was used for), or hide who talks to whom and when. The keys
come from the server, so a dishonest server could hand out its own - the
safety code lets two people check, and Buddy warns when a buddy's keys
change. Keep ENC_VERSION, the device id rule and the size limits in step
with server/core.py (tests check).
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import json
import os
import sys

from core import atomic_io

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True
except ImportError:   # the page says so (client.missing_support) instead of Buddy not starting
    AVAILABLE = False

ENC_VERSION = 1
KEYS_FILENAME = "keys.json"
_NONCE = bytes(12)   # every key here encrypts exactly one thing, so a fixed nonce is safe
_SAFETY_GROUPS = 6


class CryptoError(Exception):
    """This PC's key couldn't be made or unlocked."""


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text, size: int | None = None) -> bytes | None:
    if not isinstance(text, str):
        return None
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if size is None or len(data) == size else None


def device_id(public_key: bytes) -> str:
    """The same as server/common.py device_id."""
    return hashlib.sha256(public_key).hexdigest()[:16]


class DeviceKey:
    """This PC's key pair."""

    def __init__(self, private_bytes: bytes):
        self._private = X25519PrivateKey.from_private_bytes(private_bytes)
        self.public = self._private.public_key().public_bytes_raw()
        self.public_b64 = b64(self.public)
        self.id = device_id(self.public)

    @classmethod
    def generate(cls) -> "DeviceKey":
        return cls(X25519PrivateKey.generate().private_bytes_raw())

    def private_bytes(self) -> bytes:
        return self._private.private_bytes_raw()

    def shared(self, public: bytes) -> bytes:
        return self._private.exchange(X25519PublicKey.from_public_bytes(public))


def _aad(room: str, sender: str, device: str) -> bytes:
    return f"buddy-network-dm-v1|{room}|{sender}|{device}".encode("utf-8")


def _image_aad(room: str, sender: str, device: str, image_id: str) -> bytes:
    return f"buddy-network-dm-image-v1|{room}|{sender}|{device}|{image_id}".encode("utf-8")


def _wrap_key(shared: bytes, salt: bytes, sender_device: str, recipient_device: str, kind: str = "dm") -> bytes:
    info = f"buddy-network-{kind}-wrap-v1|{sender_device}|{recipient_device}".encode("ascii")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(shared)


def keys_by_device(public_keys) -> dict[str, bytes]:
    """device id -> public key, for the base64 keys a buddy list carries;
    anything that isn't a key is left out. The ids are worked out here,
    not taken from the server."""
    out = {}
    for text in public_keys or ():
        key = unb64(text, 32)
        if key is not None:
            out[device_id(key)] = key
    return out


def encrypt(text: str, *, room: str, sender: str, me: DeviceKey, recipients: dict[str, bytes]) -> dict:
    """`enc` for a send. recipients: device id -> public key, every PC of
    both people (this one included)."""
    message_key, salt = os.urandom(32), os.urandom(16)
    aad = _aad(room, sender, me.id)
    body = ChaCha20Poly1305(message_key).encrypt(_NONCE, text.encode("utf-8"), aad)
    wrapped = {}
    for device, public in recipients.items():
        key = _wrap_key(me.shared(public), salt, me.id, device)
        wrapped[device] = b64(ChaCha20Poly1305(key).encrypt(_NONCE, message_key, aad))
    return {"v": ENC_VERSION, "from": me.id, "salt": b64(salt), "body": b64(body), "keys": wrapped}


def decrypt(enc, *, room: str, sender: str, me: DeviceKey, sender_keys: dict[str, bytes]) -> str | None:
    """The text, or None if this PC can't read it: it wasn't encrypted for
    this PC, it came from a PC of theirs this one doesn't know, or it was
    changed on the way."""
    if not isinstance(enc, dict) or enc.get("v") != ENC_VERSION:
        return None
    public = sender_keys.get(enc.get("from"))
    wrapped = unb64((enc.get("keys") or {}).get(me.id) if isinstance(enc.get("keys"), dict) else None, 48)
    salt, body = unb64(enc.get("salt"), 16), unb64(enc.get("body"))
    if public is None or wrapped is None or salt is None or body is None:
        return None
    aad = _aad(room, sender, enc["from"])
    try:
        key = _wrap_key(me.shared(public), salt, enc["from"], me.id)
        message_key = ChaCha20Poly1305(key).decrypt(_NONCE, wrapped, aad)
        return ChaCha20Poly1305(message_key).decrypt(_NONCE, body, aad).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError):
        return None


def encrypt_image(data: bytes, *, room: str, sender: str, image_id: str, me: DeviceKey,
                  recipients: dict[str, bytes]) -> tuple[bytes, dict]:
    """(the encrypted bytes to upload, the image's `enc`: its key wrapped for
    every PC of both people). The same scheme as encrypt(), with its own key
    and salt - so editing the message's text leaves the image readable - and
    the image's id bound in, so the server can't swap one image for another."""
    image_key, salt = os.urandom(32), os.urandom(16)
    aad = _image_aad(room, sender, me.id, image_id)
    sealed = ChaCha20Poly1305(image_key).encrypt(_NONCE, data, aad)
    wrapped = {}
    for device, public in recipients.items():
        key = _wrap_key(me.shared(public), salt, me.id, device, "dm-image")
        wrapped[device] = b64(ChaCha20Poly1305(key).encrypt(_NONCE, image_key, aad))
    return sealed, {"v": ENC_VERSION, "from": me.id, "salt": b64(salt), "keys": wrapped}


def decrypt_image(sealed: bytes, enc, *, room: str, sender: str, image_id: str, readers,
                  sender_keys: dict[str, bytes]) -> bytes | None:
    """A DM image's bytes, or None if no key of this PC's (readers) can open
    it, it came from a PC of theirs this one doesn't know, or it was changed."""
    if not isinstance(enc, dict) or enc.get("v") != ENC_VERSION or not isinstance(enc.get("keys"), dict):
        return None
    public, salt = sender_keys.get(enc.get("from")), unb64(enc.get("salt"), 16)
    reader = next((k for k in readers if k.id in enc["keys"]), None)
    wrapped = unb64(enc["keys"].get(reader.id), 48) if reader is not None else None
    if public is None or salt is None or wrapped is None:
        return None
    aad = _image_aad(room, sender, enc["from"], image_id)
    try:
        key = _wrap_key(reader.shared(public), salt, enc["from"], reader.id, "dm-image")
        image_key = ChaCha20Poly1305(key).decrypt(_NONCE, wrapped, aad)
        return ChaCha20Poly1305(image_key).decrypt(_NONCE, sealed, aad)
    except (InvalidTag, ValueError):
        return None


def open_message(m: dict, *, readers, keystore: "KeyStore", url: str, salts: dict) -> dict:
    """A message as the server sent it, made ready to show: a DM's text
    decrypted, or marked "unreadable" if this PC can't open it. A DM is only
    ever what this PC decrypted - text the server put there itself is never
    shown. Also marks one from a PC of the sender's not accepted yet
    ("unverified"), and one whose random salt was seen before on another
    message ("replayed": an old message sent again). readers: this PC's
    keys (KeyStore.readers); salts: salt -> message id, kept by the caller."""
    if not isinstance(m.get("author"), dict):
        m["author"] = {"id": "", "tag": "", "name": None}
    m.setdefault("deleted", False)
    enc = m.pop("enc", None)
    dm = str(m.get("room", "")).startswith("dm-")
    if enc is None:
        if dm and not m["deleted"]:
            m["e2e"], m["text"], m["unreadable"] = True, "", True
        return m
    sender = m["author"].get("id") or ""
    text = None
    wrapped_for = enc.get("keys") if isinstance(enc, dict) and isinstance(enc.get("keys"), dict) else {}
    salt = enc.get("salt") if isinstance(enc, dict) else None
    if isinstance(salt, str) and salts.setdefault(salt, m.get("id")) != m.get("id"):
        m["e2e"], m["text"], m["unreadable"], m["replayed"] = True, "", True, True
        return m
    # This PC's key, or an older one it kept (a transfer brought a new one in).
    reader = next((k for k in readers if k.id in wrapped_for), None)
    if reader is not None and sender and dm:
        text = decrypt(enc, room=m["room"], sender=sender, me=reader, sender_keys=keystore.known_keys(url, sender))
        if text is not None and enc.get("from") not in keystore.accepted(url, sender):
            m["unverified"] = True
    m["e2e"], m["text"], m["unreadable"] = True, text or "", text is None
    return m


def safety_code(user_a: str, keys_a, user_b: str, keys_b) -> str:
    """Thirty digits both people see the same: from each person's id and
    all of their PCs' public keys. If the server swapped any key, the two
    screens show different codes."""
    digest = hashlib.sha256(b"buddy-network-safety-v1")
    for user, keys in sorted(((user_a, keys_a), (user_b, keys_b)), key=lambda p: p[0]):
        digest.update(len(user).to_bytes(2, "big") + user.encode("utf-8"))
        ordered = sorted(keys)
        digest.update(len(ordered).to_bytes(2, "big") + b"".join(ordered))
    raw = digest.digest()
    return " ".join(f"{int.from_bytes(raw[i * 5:i * 5 + 5], 'big') % 100000:05d}" for i in range(_SAFETY_GROUPS))


# ------------------------------------------------------------------ DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


_ENTROPY = b"Buddy Network"


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    entropy = _Blob(len(_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    ui_forbidden = 0x1
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = call(ctypes.byref(blob_in), "Buddy Network", ctypes.byref(entropy), None, None, ui_forbidden,
                  ctypes.byref(blob_out))
    else:
        ok = call(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, ui_forbidden,
                  ctypes.byref(blob_out))
    if not ok:
        raise CryptoError(f"Windows couldn't {'lock' if protect else 'unlock'} the key "
                          f"(error {ctypes.GetLastError()}).")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(data: bytes) -> tuple[str, str]:
    """(how, base64): locked to this Windows account where there's DPAPI."""
    if sys.platform == "win32":
        return "dpapi", b64(_dpapi(data, True))
    return "none", b64(data)   # not Windows: no DPAPI, so no lock available


def unprotect(how: str, text: str) -> bytes:
    data = unb64(text)
    if data is None or how not in ("dpapi", "none"):
        raise CryptoError("The saved key isn't readable.")
    if how == "none":
        return data
    if sys.platform != "win32":
        raise CryptoError("The saved key was locked by Windows.")
    return _dpapi(data, False)


# --------------------------------------------------------------- KeyStore

class KeyStore:
    """keys.json: for each server address, this PC's key (private half
    locked with DPAPI), older keys it had ("retired" - after a transfer
    brought another in, transfer.py - kept for reading old DMs) and what's
    known about each person's keys:

      "people": {user id: {"keys": {device: public}, every key seen
                                   for them - so old messages still open,
                           "ack": [device, ...]   every PC accepted so far,
                           "verified": "<safety code>" or ""}}

    A key seen but never accepted keeps the warning up - even once the
    server stops listing it, so a key shown for a moment and then taken
    away can't slip by. Accepting forgets any such key that's gone.

    Written with core/atomic_io, like the identity."""

    def __init__(self, folder: str):
        self.path = os.path.join(folder, KEYS_FILENAME)
        self.warnings: list[str] = []
        self._devices: dict[str, DeviceKey] = {}
        try:
            data = atomic_io.read_json(self.path, default={}, warnings=self.warnings)
        except atomic_io.CorruptFileError:
            try:
                atomic_io.set_aside(self.path)
            except OSError:
                pass
            self.warnings.append("This PC's Buddy Network encryption keys couldn't be read, so new ones "
                                 "will be made – direct messages sent before now can't be read here.")
            data = {}
        servers = data.get("servers") if isinstance(data, dict) else None
        self._servers: dict = servers if isinstance(servers, dict) else {}

    def _server(self, url: str) -> dict:
        entry = self._servers.get(url)
        if not isinstance(entry, dict):
            entry = self._servers[url] = {}
        if not isinstance(entry.get("people"), dict):
            entry["people"] = {}
        return entry

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_io.write_json(self.path, {"servers": self._servers})

    def device(self, url: str) -> DeviceKey:
        """This PC's key for that server, made (and saved) the first time.
        A key that can't be unlocked - Windows reinstalled, the file copied
        from another PC - is replaced, with a warning."""
        if url in self._devices:
            return self._devices[url]
        entry = self._server(url)
        saved = entry.get("device")
        key = None
        if isinstance(saved, dict):
            try:
                key = DeviceKey(unprotect(saved.get("protection", ""), saved.get("private", "")))
            except (CryptoError, ValueError):
                self.warnings.append("This PC's Buddy Network encryption key couldn't be unlocked, so a new "
                                     "one was made – direct messages sent before now can't be read here.")
        if key is None:
            key = DeviceKey.generate()
            entry["device"] = self._locked(key)
            self._write()
        self._devices[url] = key
        return key

    @staticmethod
    def _locked(key: DeviceKey) -> dict:
        how, locked = protect(key.private_bytes())
        return {"protection": how, "private": locked, "public": key.public_b64}

    def readers(self, url: str) -> list[DeviceKey]:
        """Every key this PC can read DMs with there: its own first, then
        retired ones. Retired keys that can't be unlocked are skipped."""
        keys = [self.device(url)]
        for saved in self._server(url).get("retired") or ():
            try:
                old = DeviceKey(unprotect(saved.get("protection", ""), saved.get("private", "")))
            except (CryptoError, ValueError, AttributeError):
                continue
            if all(k.id != old.id for k in keys):
                keys.append(old)
        return keys

    def export_server(self, url: str) -> dict:
        """This server's part, unlocked, for a transfer file."""
        return {"devices": [b64(k.private_bytes()) for k in self.readers(url)],
                "people": json.loads(json.dumps(self._server(url)["people"]))}

    def import_server(self, url: str, part: dict):
        """Takes over a transfer's keys: its first key becomes this PC's; the
        rest, and the key this PC had, are kept for reading. What it knew
        about people (keys seen, codes checked) is added to what's here."""
        keys = []
        for text in part.get("devices") or ():
            raw = unb64(text, 32)
            if raw is not None:
                keys.append(DeviceKey(raw))
        if not keys:
            raise CryptoError("The transfer file holds no encryption key.")
        entry = self._server(url)
        mine = self.readers(url) if isinstance(entry.get("device"), dict) else []
        entry["device"] = self._locked(keys[0])
        seen, retired = {keys[0].id}, []
        for k in keys[1:] + mine:
            if k.id not in seen:
                seen.add(k.id)
                retired.append(self._locked(k))
        entry["retired"] = retired
        for user_id, theirs in (part.get("people") or {}).items():
            if not isinstance(theirs, dict):
                continue
            person = self._person(url, user_id)
            person["keys"].update({d: k for d, k in (theirs.get("keys") or {}).items()
                                   if isinstance(k, str) and unb64(k, 32) is not None})
            person["ack"] = sorted(set(person.get("ack") or ()) | set(theirs.get("ack") or ()))
            if theirs.get("verified") and not person.get("verified"):
                person["verified"] = str(theirs["verified"])
        self._devices.pop(url, None)
        self._write()

    def _person(self, url: str, user_id: str) -> dict:
        people = self._server(url)["people"]
        person = people.get(user_id)
        if not isinstance(person, dict):
            person = people[user_id] = {}
        if not isinstance(person.get("keys"), dict):
            person["keys"] = {}
        return person

    def remember(self, url: str, user_id: str, current: dict[str, bytes]) -> bool:
        """Notes someone's current keys (device id -> public key). True if
        a key seen for them was never accepted - a new PC, Buddy
        reinstalled, or someone in the middle. The first keys seen for a
        person are accepted as they are; a PC that's gone is nothing to
        warn about."""
        person = self._person(url, user_id)
        before = json.dumps(person, sort_keys=True)
        for device, public in current.items():
            person["keys"][device] = b64(public)
        if not person.get("ack"):
            person["ack"] = sorted(current)
        if json.dumps(person, sort_keys=True) != before:
            self._write()
        return self.keys_changed(url, user_id)

    def known_keys(self, url: str, user_id: str) -> dict[str, bytes]:
        """Every key seen for them, for opening their messages (accepted()
        says which of them they can be trusted from)."""
        out = {}
        for device, text in self._person(url, user_id)["keys"].items():
            key = unb64(text, 32)
            if key is not None and device_id(key) == device:
                out[device] = key
        return out

    def accepted(self, url: str, user_id: str) -> set[str]:
        return set(self._person(url, user_id).get("ack") or ())

    def keys_changed(self, url: str, user_id: str) -> bool:
        """A key has been seen for them that was never accepted."""
        person = self._person(url, user_id)
        ack = set(person.get("ack") or ())
        return bool(ack) and bool(set(person["keys"]) - ack)

    def accept(self, url: str, user_id: str, current):
        """They've accepted the keys listed now (from the safety code).
        Keys seen but never accepted that aren't listed any more are
        forgotten: nothing from them opens after this."""
        person = self._person(url, user_id)
        ack = set(person.get("ack") or ()) | set(current)
        person["ack"] = sorted(ack)
        person["keys"] = {d: k for d, k in person["keys"].items() if d in ack}
        self._write()

    def verified(self, url: str, user_id: str) -> str:
        return str(self._person(url, user_id).get("verified") or "")

    def set_verified(self, url: str, user_id: str, code: str):
        self._person(url, user_id)["verified"] = code
        self._write()
