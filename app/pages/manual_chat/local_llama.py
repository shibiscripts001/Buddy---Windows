#!/usr/bin/env python3
"""
Buddy's own llama.cpp: models run on this machine without Ollama or any
other app. Ask Buddy's semantic search uses it for an embedding model
dropped in the models folder (embedder.py); the same server can run chat
models later.

No Qt in here - the Settings window runs install() on a thread, and search
calls LlamaServer.ensure() from the agent's worker thread.

  Runtime   One pinned llama.cpp release, the CPU build for this machine
            (Apple's builds use Metal too), from ggml-org's GitHub releases.
            Checked against its SHA-256 before it's unpacked, unpacked into
            a staging folder and renamed into place, so a failed or
            cancelled download never leaves half a runtime.
  Models    GGUF files in MODELS_DIR - put there by hand, or the one
            download offered (EmbeddingGemma, which the manual bundle's
            vectors are built with).
  Server    llama-server, started on demand on 127.0.0.1 at a free port,
            with a random API key so nothing else on the machine (a web
            page included - it allows any origin) can use it. One per
            model; stopped after IDLE_SECONDS unused, when Buddy quits,
            and - on Windows - by the system if Buddy dies, through a job
            object that kills it with Buddy.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core import defender

from .ask_folder import ASK_BUDDY_DIR

RELEASE = "b11312"
RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download/" + RELEASE + "/"

ROOT = Path(ASK_BUDDY_DIR)
RUNTIME_DIR = ROOT / "llama.cpp" / RELEASE
MODELS_DIR = ROOT / "models"
LOG_PATH = ROOT / "llama.cpp" / "server.log"

IDLE_SECONDS = 15 * 60
START_TIMEOUT = 90          # a big model from a slow disk takes a while to load
CHUNK = 1 << 20

# Progress: (done bytes, total bytes, message).
Progress = Callable[[int, int, str], None]


@dataclass(frozen=True)
class Download:
    url: str
    name: str               # the file's name where it's saved
    size: int               # bytes, as published
    sha256: str
    label: str = ""

    @property
    def mb(self) -> int:
        return round(self.size / 1e6)


# (platform.system(), platform.machine()) -> the release's archive for it.
RUNTIMES = {
    ("Windows", "AMD64"): Download(RELEASE_URL + f"llama-{RELEASE}-bin-win-cpu-x64.zip", f"llama-{RELEASE}-bin-win-cpu-x64.zip",
                                   19265352, "d2d966c23e4d097d70633f9d92e4931245b575be7a5409201d8f352493f0e702", "llama.cpp"),
    ("Windows", "ARM64"): Download(RELEASE_URL + f"llama-{RELEASE}-bin-win-cpu-arm64.zip", f"llama-{RELEASE}-bin-win-cpu-arm64.zip",
                                   12108942, "1822a499587503423f65cc9348253d6f883c799116becdb36baa3f9e14390c2d", "llama.cpp"),
    ("Darwin", "arm64"): Download(RELEASE_URL + f"llama-{RELEASE}-bin-macos-arm64.tar.gz", f"llama-{RELEASE}-bin-macos-arm64.tar.gz",
                                  11820971, "3ceea603e48367ab694822f7304e6880817337f942f9672321411932c76c46f2", "llama.cpp"),
    ("Darwin", "x86_64"): Download(RELEASE_URL + f"llama-{RELEASE}-bin-macos-x64.tar.gz", f"llama-{RELEASE}-bin-macos-x64.tar.gz",
                                   11375343, "c78ebb522375db1675d5df252b0a36dbec8822425852673a5f22c8c9eaa23b86", "llama.cpp"),
}

# The embedding model the manual bundle is built with, as llama.cpp's own
# conversion (Ollama's copy is a GGUF llama.cpp can't load). Q8_0 matches the
# bundle's vectors at cosine 0.9996 - as close as int8 storage allows.
EMBEDDING_GEMMA = Download(
    "https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/main/embeddinggemma-300M-Q8_0.gguf",
    "embeddinggemma-300M-Q8_0.gguf", 333590944,
    "b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63", "EmbeddingGemma")


class LlamaError(Exception):
    """Something the person can act on, worded for them."""


class Cancelled(Exception):
    pass


def _machine() -> tuple[str, str]:
    """("Windows", "AMD64" | "ARM64") or ("Darwin", "arm64" | "x86_64").
    An x64 Python on an ARM PC says AMD64 - and the x64 build suits it, as
    that's what it runs under."""
    system, machine = platform.system(), platform.machine()
    return system, machine.upper() if system == "Windows" else machine


def runtime_download() -> Download | None:
    """The llama.cpp archive for this machine, or None where there's none."""
    return RUNTIMES.get(_machine())


def server_exe() -> Path | None:
    """llama-server, if the runtime is installed."""
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    if not RUNTIME_DIR.is_dir():
        return None
    for path in (RUNTIME_DIR / name, *RUNTIME_DIR.glob(f"*/{name}")):
        if path.is_file():
            return path
    return None


def model_files() -> list[Path]:
    """The GGUF files in the models folder, by name. Split models' later
    parts (-00002-of-00003.gguf) aren't listed - llama.cpp reads them
    through the first."""
    if not MODELS_DIR.is_dir():
        return []
    files = []
    for path in MODELS_DIR.glob("*.gguf"):
        part = re.search(r"-(\d{5})-of-\d{5}$", path.stem)
        # A vision model's projector (mmproj-...) isn't a model to run on its own.
        if not (part and part.group(1) != "00001") and not path.name.lower().startswith("mmproj") and is_gguf(path):
            files.append(path)
    return sorted(files, key=lambda p: p.name.lower())


def is_gguf(path: Path) -> bool:
    """A GGUF file by its content, not just its name: one starts with the
    bytes GGUF. GGUF holds weights as data - unlike PyTorch's .bin/.pt
    pickles, loading one runs no code from it - so only GGUF is ever run."""
    try:
        with open(path, "rb") as f:
            return path.is_file() and f.read(4) == b"GGUF"
    except OSError:
        return False


def ensure_models_dir() -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


_VERIFIED = {}      # (path, size, mtime) -> it's the pinned file


def is_pinned_copy(path: Path, item: Download = None) -> bool:
    """Whether a file is exactly the pinned download (EmbeddingGemma by
    default): its size, then its SHA-256 - hashed once per change of the
    file, then remembered."""
    item = item or EMBEDDING_GEMMA
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size != item.size:
        return False
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _VERIFIED:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(CHUNK * 8), b""):
                    digest.update(block)
        except OSError:
            return False
        _VERIFIED[key] = digest.hexdigest() == item.sha256
    return _VERIFIED[key]


def folder_size(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total


# ------------------------------------------------------------- downloads


def download(item: Download, dest: Path, progress: Progress | None = None,
             cancelled: Callable[[], bool] | None = None) -> Path:
    """item into dest (a file path), checked against its SHA-256. Written
    beside it as .part and renamed only once it's whole and right."""
    progress = progress or (lambda *_a: None)
    cancelled = cancelled or (lambda: False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    done = 0
    label = item.label or item.name
    try:
        req = urllib.request.Request(item.url, headers={"User-Agent": "Buddy"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or item.size)
            while True:
                if cancelled():
                    raise Cancelled()
                block = resp.read(CHUNK)
                if not block:
                    break
                out.write(block)
                digest.update(block)
                done += len(block)
                progress(done, total, f"Downloading {label}… {done / 1e6:.0f} of {total / 1e6:.0f} MB")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        part.unlink(missing_ok=True)
        raise LlamaError(f"Couldn't download {label}: {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != item.sha256:
        part.unlink(missing_ok=True)
        raise LlamaError(f"The {label} download didn't match its published checksum, so it wasn't used. "
                         "Try again - if it keeps happening, the file on the server has changed.")
    os.replace(part, dest)
    return dest


SCAN_CLEAN, SCAN_THREAT, SCAN_SKIPPED = defender.CLEAN, defender.THREAT, defender.SKIPPED


def defender_scan(folder: Path) -> tuple[str, str]:
    """Windows Defender's verdict on the unpacked runtime (core/defender.py)."""
    return defender.scan(folder, "llama.cpp")


def install_runtime(progress: Progress | None = None, cancelled: Callable[[], bool] | None = None) -> tuple[Path, str]:
    """Downloads, checks and unpacks llama.cpp for this machine: (its
    llama-server, what Windows Defender said of it)."""
    progress = progress or (lambda *_a: None)
    exe = server_exe()
    if exe:
        return exe, ""
    item = runtime_download()
    if item is None:
        raise LlamaError(f"There's no llama.cpp build for this computer ({' '.join(_machine())}).")
    archive = download(item, RUNTIME_DIR.parent / item.name, progress, cancelled)
    staging = RUNTIME_DIR.with_name(RUNTIME_DIR.name + ".unpacking")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(staging)
        else:
            with tarfile.open(archive) as t:
                # Its symlinks (libllama.dylib -> libllama.0.5.0.dylib) stay links,
                # and nothing may land outside the folder.
                if hasattr(tarfile, "data_filter"):
                    t.extractall(staging, filter="data")
                else:
                    safe = [m for m in t.getmembers()
                            if not (m.name.startswith(("/", "..")) or "/../" in m.name)]
                    t.extractall(staging, members=safe)
        progress(0, 0, "Checking llama.cpp with Windows Defender…" if os.name == "nt" else "Unpacking llama.cpp…")
        verdict, note = defender_scan(staging)
        if verdict == SCAN_THREAT:
            raise LlamaError("Windows Defender flagged the llama.cpp download, so it was deleted and won't be used. "
                             "Nothing was run.")
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
        os.replace(staging, RUNTIME_DIR)
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise LlamaError(f"Couldn't unpack llama.cpp: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
    exe = server_exe()
    if exe is None:
        raise LlamaError("llama.cpp was unpacked, but its llama-server wasn't in it.")
    if os.name != "nt":
        for path in exe.parent.iterdir():
            if path.is_file() and not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o111)
    return exe, note


# ---------------------------------------------------------------- server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _KillWithBuddy:
    """A Windows job object that ends every process in it when Buddy's
    handle to it closes - when Buddy quits or crashes alike."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.OpenProcess.restype = wintypes.HANDLE
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return

            class _Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class _Io(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                            "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class _Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if k32.SetInformationJobObject(wintypes.HANDLE(handle), 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle, self._k32 = handle, k32
        except (OSError, AttributeError):
            self.handle = None

    def add(self, pid: int):
        if not self.handle:
            return
        from ctypes import wintypes
        process = self._k32.OpenProcess(0x0101, False, pid)    # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if process:
            self._k32.AssignProcessToJobObject(wintypes.HANDLE(self.handle), wintypes.HANDLE(process))
            self._k32.CloseHandle(wintypes.HANDLE(process))


_JOB = None
_LOCK = threading.Lock()
_SERVERS: dict[tuple, "LlamaServer"] = {}


class LlamaServer:
    """llama-server for one model, started when first needed."""

    def __init__(self, model: Path, embedding: bool = True, extra: tuple[str, ...] = ()):
        self.model = Path(model)
        self.embedding = embedding
        self.extra = tuple(extra)
        self.key = secrets.token_urlsafe(24)
        self.port = 0
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._used = 0.0
        self._timer: threading.Timer | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ensure(self, timeout: float = START_TIMEOUT) -> str:
        """Its base URL, starting it (and waiting until the model is loaded)
        if it isn't running."""
        with self._lock:
            self._touch()
            if self.running():
                return self.base_url
            exe = server_exe()
            if exe is None:
                raise LlamaError("llama.cpp isn't set up yet – Settings > AI > Manual search > Set up.")
            if not self.model.is_file():
                raise LlamaError(f"The model file isn't there any more: {self.model}")
            if not is_gguf(self.model):
                raise LlamaError(f"{self.model.name} isn't a GGUF model, so it wasn't run.")
            self.port = _free_port()
            args = [str(exe), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                    "--api-key", self.key, "--no-webui", "--offline"]
            if self.embedding:
                # Four inputs at a time, each up to the 2,048 tokens EmbeddingGemma
                # reads; a whole input in one physical batch, as embeddings need.
                args += ["--embedding", "-c", "8192", "-np", "4", "-b", "2048", "-ub", "2048"]
            args += list(self.extra)
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            log = open(LOG_PATH, "ab")
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {self.model.name} ---\n".encode())
            log.flush()
            flags = 0x08000000 if os.name == "nt" else 0               # CREATE_NO_WINDOW
            try:
                self.process = subprocess.Popen(args, cwd=str(exe.parent), stdin=subprocess.DEVNULL, stdout=log,
                                                stderr=subprocess.STDOUT, creationflags=flags)
            except OSError as exc:
                raise LlamaError(f"llama.cpp wouldn't start: {exc}") from exc
            finally:
                log.close()
            global _JOB
            if _JOB is None:
                _JOB = _KillWithBuddy()
            _JOB.add(self.process.pid)
            self._wait_ready(timeout)
            return self.base_url

    def _wait_ready(self, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.process = None
                raise LlamaError(f"llama.cpp stopped while loading {self.model.name} – is it a GGUF model it can "
                                 f"read? Its log: {LOG_PATH}")
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/health",
                                             headers={"Authorization": f"Bearer {self.key}"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except urllib.error.HTTPError as exc:
                if exc.code != 503:                 # 503: still loading the model
                    break
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.25)
        self.stop()
        raise LlamaError(f"llama.cpp didn't get {self.model.name} ready in {timeout:.0f} s. Its log: {LOG_PATH}")

    def post(self, path: str, body: dict, timeout: float) -> dict:
        """A request to it, starting it first if need be."""
        base = self.ensure()
        req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LlamaError(f"llama.cpp answered {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            raise LlamaError(f"llama.cpp stopped answering: {exc}") from exc
        finally:
            self._touch()

    def _touch(self):
        """Used just now: it stops IDLE_SECONDS from now unless used again."""
        self._used = time.monotonic()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(IDLE_SECONDS, self._idle)
        self._timer.daemon = True
        self._timer.start()

    def _idle(self):
        if time.monotonic() - self._used >= IDLE_SECONDS - 1:
            self.stop()

    def stop(self):
        process, self.process = self.process, None
        if self._timer is not None:
            self._timer.cancel()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def server_for(model: Path, embedding: bool = True) -> LlamaServer:
    """The one server for this model, made the first time it's asked for."""
    key = (str(Path(model).resolve()), embedding)
    with _LOCK:
        server = _SERVERS.get(key)
        if server is None:
            server = _SERVERS[key] = LlamaServer(Path(model), embedding)
        return server


def stop_all():
    with _LOCK:
        servers = list(_SERVERS.values())
    for server in servers:
        server.stop()


atexit.register(stop_all)


if __name__ == "__main__":      # py -m pages.manual_chat.local_llama: what's set up
    print("runtime:", server_exe() or "not installed", "| for this machine:", runtime_download())
    print("models:", [p.name for p in model_files()], "in", MODELS_DIR)
    sys.exit(0)
