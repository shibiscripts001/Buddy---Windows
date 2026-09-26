#!/usr/bin/env python3
"""
The private environment Buddy's transcription runs in, and the models it
uses. No Qt here: the Setup window drives it on a QThread, and it can be
run from a terminal for testing.

Why a separate environment at all: Buddy runs inside whatever Python
DaVinci Resolve found, and a speech model's stack (CTranslate2, ONNX
Runtime, on NVIDIA machines ~1.3 GB of CUDA libraries) has no business in
that interpreter. So transcription lives in its own venv under
~/.buddy/transcribe/, built from the SAME Python that runs Buddy (its
built-in venv + pip - nothing to install first), and Buddy talks to it
only by launching worker.py there as a subprocess.

Engine: faster-whisper on every machine. It runs on the GPU where there is
an NVIDIA card (CUDA) and in an int8 CPU mode everywhere else - Intel and
Apple Silicon Macs, AMD/Intel graphics - so one code path covers every
Windows and Mac user. The CUDA libraries are only installed when an NVIDIA
GPU is present.

Pinned versions: ctranslate2 4.8.2 + cuBLAS 12.9.2.10 + cuDNN 9 is the
combination known to work on an RTX 50-series card. Every package has wheels for Python 3.10-3.14 on
Windows x64 and both Mac architectures, checked on PyPI; where the newest
release skips a platform, pip falls back to the previous one by itself.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path.home() / ".buddy" / "transcribe"
VENV_DIR = ROOT / "venv"
MODELS_DIR = ROOT / "models"
WORKER = Path(__file__).with_name("worker.py")

BASE_PACKAGES = ["faster-whisper==1.2.1", "ctranslate2==4.8.2", "onnx-asr==0.12.0"]
# onnx-asr runs Parakeet: pure Python (<1 MB) on the onnxruntime that
# faster-whisper already pulls in. Older installs without it get it on
# their first Parakeet download (ensure_parakeet_runtime).
PARAKEET_PACKAGE = "onnx-asr==0.12.0"
NVIDIA_PACKAGES = ["nvidia-cublas-cu12==12.9.2.10", "nvidia-cudnn-cu12>=9,<10"]

# What the Setup window offers. Sizes are the CTranslate2 weights as
# published (Systran / faster-whisper's own model table); "fit" is who the
# model suits, shown next to it.
#
# Two engines. "whisper" models run on faster-whisper (GPU with NVIDIA,
# else CPU). "parakeet" is NVIDIA's Parakeet TDT 0.6B v3 (CC-BY 4.0) as an
# int8 ONNX export, run by onnx-asr on the CPU - measured 35x real time on
# a desktop CPU, so it needs no GPU. It covers 25 European languages
# (PARAKEET_LANGS, from its model card), punctuates by itself, and gives
# per-token times. Distil-Whisper is English only.
MODELS = [
    {"id": "large-v3", "label": "Large v3", "size_gb": 3.1, "engine": "whisper",
     "fit": "Most accurate, every language. Best with an NVIDIA GPU."},
    {"id": "large-v3-turbo", "label": "Large v3 Turbo", "size_gb": 1.6, "engine": "whisper",
     "fit": "Nearly as accurate, several times faster. Good default."},
    {"id": "parakeet-v3", "label": "Parakeet v3 (NVIDIA)", "size_gb": 0.67, "engine": "parakeet",
     "repo": "istupakov/parakeet-tdt-0.6b-v3-onnx",
     "files": ["config.json", "vocab.txt", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"],
     "fit": "25 European languages only. Very fast even without a GPU; punctuates by itself."},
    {"id": "distil-large-v3.5", "label": "Distil-Whisper Large v3.5", "size_gb": 1.51, "engine": "whisper",
     "english_only": True,
     "fit": "English only. About twice as fast as Large v3, nearly as accurate."},
    {"id": "small", "label": "Small", "size_gb": 0.48, "engine": "whisper",
     "fit": "Fast on any computer. Less accurate."},
]
MODEL_IDS = [m["id"] for m in MODELS]
MODEL_BY_ID = {m["id"]: m for m in MODELS}
PARAKEET_LANGS = {"bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "hr", "hu", "it",
                  "lt", "lv", "mt", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "uk"}
# Multilingual Whisper models, best first: what "Auto" falls back to.
WHISPER_PREFERENCE = ["large-v3", "large-v3-turbo", "small"]

# Translation: Meta's NLLB-200 (200 languages, any direction), as
# CTranslate2 int8 conversions - the same engine as faster-whisper, so no
# new packages: its tokenizer is a Hugging Face tokenizer.json, which the
# `tokenizers` library faster-whisper already depends on reads. The 1.3B
# and 3.3B come from OpenNMT (CTranslate2's own maintainers); OpenNMT
# publishes no 600M, so that one is a community conversion with the same
# file layout. Sizes are model.bin as published. Licence: CC-BY-NC 4.0 -
# fine for a free tool, credited in the README.
#
# Google's MADLAD-400 3B is the commercial-safe alternative (Apache 2.0):
# ~180 of NLLB's languages (languages.MADLAD_CODES), a T5 steered by a
# "<2xx>" tag on its input instead of a language code on its output. The
# conversion is Nextcloud's (their translation app runs it) - the same file
# layout as the NLLB repos, tokenizer.json included, so again nothing new
# to install. Their 7B is 8.3 GB, too big to offer on most machines.
TRANSLATION_MODELS = [
    {"id": "nllb-1.3b", "label": "NLLB 1.3B (distilled)", "size_gb": 1.38, "family": "nllb",
     "repo": "OpenNMT/nllb-200-distilled-1.3B-ct2-int8",
     "fit": "Best balance of quality and speed. Good default. Non-commercial use only."},
    {"id": "nllb-3.3b", "label": "NLLB 3.3B", "size_gb": 3.36, "family": "nllb",
     "repo": "OpenNMT/nllb-200-3.3B-ct2-int8",
     "fit": "Most accurate NLLB. Best with an NVIDIA GPU. Non-commercial use only."},
    {"id": "nllb-600m", "label": "NLLB 600M (distilled)", "size_gb": 0.62, "family": "nllb",
     "repo": "mijuanlo/nllb-200-distilled-600M-ct2-int8",
     "fit": "Fast on any computer. Less accurate. Non-commercial use only."},
    {"id": "madlad-3b", "label": "MADLAD-400 3B (Google)", "size_gb": 2.95, "family": "madlad",
     "repo": "Nextcloud-AI/madlad400-3b-mt-ct2-int8",
     "fit": "About 180 languages. Free for commercial use (Apache 2.0). Best with an NVIDIA GPU."},
]
TRANSLATION_IDS = [m["id"] for m in TRANSLATION_MODELS]
TRANSLATION_BY_ID = {m["id"]: m for m in TRANSLATION_MODELS}
TRANSLATION_FILES = ["model.bin", "config.json", "shared_vocabulary.json", "tokenizer.json",
                     "special_tokens_map.json", "tokenizer_config.json", "generation_config.json"]
RECOMMENDED_TRANSLATION = "nllb-1.3b"


class SetupError(RuntimeError):
    """A setup step that failed, with a message fit to show the user."""


# ------------------------------------------------------------ hardware


@dataclass
class Hardware:
    system: str            # "Windows" / "Darwin" / "Linux"
    machine: str           # "AMD64" / "arm64" / "x86_64"
    nvidia: bool
    gpu_name: str = ""

    @property
    def device(self) -> str:
        return "cuda" if self.nvidia else "cpu"

    @property
    def recommended_model(self) -> str:
        return "large-v3" if self.nvidia else "large-v3-turbo"

    def describe(self) -> str:
        if self.nvidia:
            return f"NVIDIA GPU ({self.gpu_name}) – transcription runs on the GPU."
        if self.system == "Darwin":
            chip = "Apple Silicon" if self.machine == "arm64" else "Intel"
            return f"Mac ({chip}) – transcription runs on the CPU."
        return "No NVIDIA GPU found – transcription runs on the CPU."


def detect_hardware() -> Hardware:
    system, machine = platform.system(), platform.machine()
    nvidia, name = False, ""
    smi = shutil.which("nvidia-smi")
    if smi and system != "Darwin":
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10, **_no_window(),
            )
            if out.returncode == 0 and out.stdout.strip():
                nvidia, name = True, out.stdout.strip().splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return Hardware(system, machine, nvidia, name)


def _no_window() -> dict:
    """Keep console windows from flashing up when Buddy (a GUI app) runs a
    subprocess on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ---------------------------------------------------------- environment


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def base_python() -> str:
    """The interpreter to build the venv from: the one running Buddy.

    Inside Resolve sys.executable can be the host app rather than a
    python binary, so fall back to one found on PATH.
    """
    exe = Path(sys.executable)
    if exe.name.lower().startswith("python"):
        return str(exe)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    raise SetupError(
        "Couldn't find a Python interpreter to build the transcription "
        "environment from. Install Python 3.10 or newer from python.org."
    )


def packages_for(hw: Hardware) -> list[str]:
    return BASE_PACKAGES + (NVIDIA_PACKAGES if hw.nvidia else [])


@dataclass
class EnvStatus:
    ready: bool
    detail: str
    versions: dict = field(default_factory=dict)


def env_status(timeout: int = 60) -> EnvStatus:
    """Is the environment built and importable? Asks the venv's own
    Python, so a half-finished or broken install reads as not ready."""
    py = venv_python()
    if not py.exists():
        return EnvStatus(False, "Not set up yet.")
    probe = (
        "import json, faster_whisper, ctranslate2\n"
        "v = {'faster_whisper': faster_whisper.__version__, 'ctranslate2': ctranslate2.__version__}\n"
        "try:\n"
        "    import importlib.metadata as md; v['onnx_asr'] = md.version('onnx-asr')\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps(v))"
    )
    try:
        out = subprocess.run([str(py), "-c", probe], capture_output=True,
                             text=True, timeout=timeout, **_no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        return EnvStatus(False, f"Environment can't start: {exc}")
    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or ["unknown error"])[-1]
        return EnvStatus(False, f"Environment is incomplete: {last}")
    try:
        versions = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        versions = {}
    return EnvStatus(True, "Ready.", versions)


Progress = Callable[[str], None]


def _run_streaming(cmd, progress: Progress, cancelled, what: str):
    """Run cmd, forwarding each output line to progress; raise SetupError
    with the tail of the output on failure, SetupCancelled on cancel."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1, **_no_window(),
    )
    tail: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail = (tail + [line])[-12:]
            progress(line)
            if cancelled():
                proc.kill()
                raise SetupCancelled()
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise SetupError(f"{what} failed:\n" + "\n".join(tail))


class SetupCancelled(Exception):
    pass


def install_environment(progress: Progress = print, cancelled=lambda: False,
                        hw: Hardware | None = None) -> None:
    """Create ~/.buddy/transcribe/venv and install the engine into it.
    Safe to re-run: an existing venv is reused and pip only fetches what's
    missing."""
    hw = hw or detect_hardware()
    ROOT.mkdir(parents=True, exist_ok=True)
    if not venv_python().exists():
        progress(f"Creating the environment with {base_python()} …")
        _run_streaming([base_python(), "-m", "venv", str(VENV_DIR)],
                       progress, cancelled, "Creating the environment")
    pkgs = packages_for(hw)
    progress("Installing: " + ", ".join(pkgs))
    _run_streaming(
        [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
         "--progress-bar", "off", *pkgs],
        progress, cancelled, "Installing packages",
    )
    status = env_status()
    if not status.ready:
        raise SetupError(status.detail)
    progress("Environment ready: " + ", ".join(f"{k} {v}" for k, v in status.versions.items()))


def ensure_parakeet_runtime(progress: Progress = print, cancelled=lambda: False) -> None:
    """Add onnx-asr to an environment built before Parakeet was offered."""
    if "onnx_asr" in env_status().versions:
        return
    progress(f"Adding Parakeet support ({PARAKEET_PACKAGE}, under 1 MB) …")
    _run_streaming(
        [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
         "--progress-bar", "off", PARAKEET_PACKAGE],
        progress, cancelled, "Installing onnx-asr",
    )


def remove_environment() -> None:
    """Delete the venv (models are kept - they're the expensive part)."""
    if VENV_DIR.exists():
        shutil.rmtree(VENV_DIR)


# --------------------------------------------------------------- models


def _looks_like_model(folder: Path) -> bool:
    """A CTranslate2 Whisper model."""
    return (folder / "model.bin").is_file() and (folder / "config.json").is_file()


def _looks_like_parakeet(folder: Path) -> bool:
    return all((folder / f).is_file() for f in MODEL_BY_ID["parakeet-v3"]["files"])


def _looks_like(model_id: str, folder: Path) -> bool:
    if MODEL_BY_ID[model_id]["engine"] == "parakeet":
        return _looks_like_parakeet(folder)
    return _looks_like_model(folder)


def buddy_model_dir(model_id: str) -> Path:
    return MODELS_DIR / model_id


def installed_models(extra_paths: dict | None = None) -> dict[str, str]:
    """model id -> folder, for every model Buddy can use right now: ones
    it downloaded, plus any the user pointed it at (extra_paths, saved in
    settings) that still exist."""
    found = {}
    for mid in MODEL_IDS:
        d = buddy_model_dir(mid)
        if _looks_like(mid, d):
            found[mid] = str(d)
    for mid, path in (extra_paths or {}).items():
        if mid in MODEL_BY_ID and mid not in found and path and _looks_like(mid, Path(path)):
            found[mid] = path
    return found


def scan_existing_models() -> dict[str, str]:
    """Models already on disk from other apps, so nobody downloads 3 GB
    twice: the standard Hugging Face cache (faster-whisper's own default)
    is checked for the Systran/mobiuslabs conversions of each size."""
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    repos = {
        "large-v3": ["models--Systran--faster-whisper-large-v3"],
        "large-v3-turbo": ["models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
                           "models--deepdml--faster-whisper-large-v3-turbo-ct2"],
        "small": ["models--Systran--faster-whisper-small"],
        "distil-large-v3.5": ["models--distil-whisper--distil-large-v3.5-ct2"],
        "parakeet-v3": ["models--istupakov--parakeet-tdt-0.6b-v3-onnx"],
    }
    found = {}
    for mid, names in repos.items():
        for name in names:
            snaps = hub / name / "snapshots"
            if not snaps.is_dir():
                continue
            for snap in sorted(snaps.iterdir(), reverse=True):
                if _looks_like(mid, snap):
                    found[mid] = str(snap)
                    break
            if mid in found:
                break
    return found


def identify_model_folder(folder: str | Path) -> str | None:
    """Which of MODELS a user-picked folder holds (by its config), or None
    if it isn't a CTranslate2 Whisper model at all."""
    folder = Path(folder)
    if _looks_like_parakeet(folder):
        try:
            kind = json.loads((folder / "config.json").read_text(encoding="utf-8")).get("model_type", "")
        except (OSError, ValueError):
            return None
        return "parakeet-v3" if "tdt" in kind else None
    if not _looks_like_model(folder):
        return None
    # A CTranslate2 Whisper config.json holds only alignment/suppression
    # tables - checked on real Systran exports. The mel-bin count lives in
    # preprocessor_config.json (feature_size: 128 for large-v3 and turbo),
    # which small doesn't ship at all: absent means Whisper's default 80.
    # Turbo and large-v3 share 128 bins; turbo's 4 decoder layers vs 32
    # make it about half the size on disk, which tells them apart.
    mels = 80
    pre = folder / "preprocessor_config.json"
    if pre.is_file():
        try:
            mels = int(json.loads(pre.read_text(encoding="utf-8")).get("feature_size", 80))
        except (OSError, ValueError, TypeError):
            return None
    size_gb = (folder / "model.bin").stat().st_size / 1e9
    if mels == 128:
        # Distil-Whisper (2 decoder layers) and Turbo (4) are both ~1.5 GB;
        # the decoder layers their alignment heads sit in tell them apart
        # (checked on the published configs: distil <=1, turbo 2-3, large-v3
        # up to 25).
        try:
            heads = json.loads((folder / "config.json").read_text(encoding="utf-8")).get("alignment_heads") or []
            top_layer = max(a for a, _b in heads) if heads else None
        except (OSError, ValueError, TypeError):
            top_layer = None
        if top_layer is not None:
            return "distil-large-v3.5" if top_layer <= 1 else "large-v3-turbo" if top_layer <= 3 else "large-v3"
        return "large-v3" if size_gb > 2.2 else "large-v3-turbo"
    if size_gb < 1.0:
        return "small"
    return None


def download_model(model_id: str, progress: Progress = print, cancelled=lambda: False) -> str:
    """Download a model into ~/.buddy/transcribe/models/<id>, run inside
    the venv: Whisper sizes through faster-whisper's own downloader (it
    knows which repo holds each size), translation models from their named
    repo. Returns the folder."""
    if model_id not in MODEL_IDS + TRANSLATION_IDS:
        raise SetupError(f"Unknown model {model_id!r}.")
    if not venv_python().exists():
        raise SetupError("Set up the transcription environment first.")
    target = buddy_model_dir(model_id)
    target.mkdir(parents=True, exist_ok=True)
    entry = next(m for m in MODELS + TRANSLATION_MODELS if m["id"] == model_id)
    expected = int(entry["size_gb"] * 1e9)
    cmd = [str(venv_python()), str(WORKER), "download", "--output", str(target),
           "--expected-bytes", str(expected)]
    if model_id in TRANSLATION_IDS:
        cmd += ["--model", entry["repo"], "--files", ",".join(TRANSLATION_FILES)]
    elif entry.get("files"):
        ensure_parakeet_runtime(progress, cancelled)
        cmd += ["--model", entry["repo"], "--files", ",".join(entry["files"])]
    else:
        cmd += ["--model", model_id]    # faster-whisper knows each size's repo
    progress(f"Downloading {model_id} …")
    _run_streaming(cmd, progress, cancelled, f"Downloading {model_id}")
    ok = _looks_like_translator(target) if model_id in TRANSLATION_IDS else _looks_like(model_id, target)
    if not ok:
        raise SetupError(f"The {model_id} download finished but the model files are missing.")
    return str(target)


# ------------------------------------------------------ translation models


def _looks_like_translator(folder: Path) -> bool:
    return all((folder / f).is_file() for f in ("model.bin", "tokenizer.json", "shared_vocabulary.json"))


def translation_family(model_id: str) -> str:
    """"nllb" or "madlad" - how worker.py steers the model."""
    return TRANSLATION_BY_ID.get(model_id, {}).get("family", "nllb")


def installed_translation_models(extra_paths: dict | None = None) -> dict[str, str]:
    """Like installed_models, for the translation models."""
    found = {}
    for mid in TRANSLATION_IDS:
        d = buddy_model_dir(mid)
        if _looks_like_translator(d):
            found[mid] = str(d)
    for mid, path in (extra_paths or {}).items():
        if mid in TRANSLATION_IDS and mid not in found and path and _looks_like_translator(Path(path)):
            found[mid] = path
    return found


def scan_existing_translation_models() -> dict[str, str]:
    """Translation models already in the Hugging Face cache, from the same repos."""
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    found = {}
    for m in TRANSLATION_MODELS:
        snaps = hub / ("models--" + m["repo"].replace("/", "--")) / "snapshots"
        if snaps.is_dir():
            for snap in sorted(snaps.iterdir(), reverse=True):
                if _looks_like_translator(snap):
                    found[m["id"]] = str(snap)
                    break
    return found


def identify_translation_folder(folder: str | Path) -> str | None:
    """Which translation model a user-picked folder holds, or None if it
    isn't a CTranslate2 NLLB or MADLAD model - told apart by the language
    tokens in the vocabulary. The three NLLB sizes are far apart on disk
    (0.6 / 1.4 / 3.4 GB); MADLAD's 3B is the only MADLAD offered (its 7B,
    8.3 GB, isn't recognised)."""
    folder = Path(folder)
    if not _looks_like_translator(folder):
        return None
    try:
        vocab = (folder / "shared_vocabulary.json").read_text(encoding="utf-8")
    except OSError:
        return None
    size_gb = (folder / "model.bin").stat().st_size / 1e9
    if '"<2en>"' in vocab and '"<2de>"' in vocab:
        return "madlad-3b" if size_gb < 5 else None
    if '"eng_Latn"' not in vocab or '"zho_Hans"' not in vocab:
        return None
    if size_gb < 1.0:
        return "nllb-600m"
    return "nllb-1.3b" if size_gb < 2.5 else "nllb-3.3b"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Buddy transcription environment")
    ap.add_argument("action", choices=["status", "hardware", "install", "models", "scan", "download"])
    ap.add_argument("--model", choices=MODEL_IDS + TRANSLATION_IDS)
    args = ap.parse_args()
    if args.action == "hardware":
        hw = detect_hardware(); print(hw, "->", hw.describe(), "| packages:", packages_for(hw))
    elif args.action == "status":
        print(env_status())
    elif args.action == "install":
        install_environment()
    elif args.action == "models":
        print(installed_models())
    elif args.action == "scan":
        print(scan_existing_models())
    elif args.action == "download":
        print(download_model(args.model))
