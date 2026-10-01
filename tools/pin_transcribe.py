"""Pins Transcribe's downloads: every model to one commit with each file's
SHA-256, and the engine's Python packages to exact versions with each
wheel's SHA-256. Run it to move to newer models or packages - a deliberate
change, reviewed in the diff - never at a user's install.

    py -3 tools/pin_transcribe.py models     app/pages/transcribe/model_pins.json
    py -3 tools/pin_transcribe.py packages   app/pages/transcribe/locks/*.txt (needs uv)
    py -3 tools/pin_transcribe.py all

Models: Hugging Face's API gives each repo's current commit and, for files
in Git LFS (the weights), their SHA-256; small files outside LFS (configs,
tokenizers) are downloaded at that commit and hashed here. Only the files
Buddy downloads are pinned - the patterns each model fetches.

Packages: uv resolves faster-whisper & co. for every platform Buddy runs on
at once (Windows x64, both Macs; Python 3.10-3.14), writing each package's
version under its platform markers, with the hashes of all its published
files. pip installs from these with --require-hashes --only-binary :all:
--no-deps (env_setup.py), so nothing else - and nothing unhashed - gets in.
"""
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
from pages.transcribe import env_setup as es  # noqa: E402

API = "https://huggingface.co/api/models/"
# onnxruntime's last release with Intel Mac wheels is 1.23.2 (Python 3.10-3.13):
# without this, the lock would pin a newer one an Intel Mac can't install. Both
# branches spelled out, or uv settles on 1.23.2 everywhere - which has no
# Python 3.14 wheels at all.
INTEL_MAC = ["onnxruntime<1.24 ; sys_platform == 'darwin' and platform_machine == 'x86_64'",
             "onnxruntime>=1.24 ; (sys_platform != 'darwin' or platform_machine != 'x86_64') and python_version >= '3.11'"]
PYTHON_RANGE = ">=3.10"


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Buddy pinning"}), timeout=60) as r:
        return json.load(r)


def sha256_url(url):
    h = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Buddy pinning"}), timeout=120) as r:
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def pin_models():
    pins = {}
    for m in es.MODELS + es.TRANSLATION_MODELS:
        repo = m["repo"]
        patterns = es.model_patterns(m)
        revision = get_json(f"{API}{repo}/revision/main")["sha"]
        tree = get_json(f"{API}{repo}/tree/{revision}?recursive=true")
        files = {}
        for entry in tree:
            path = entry.get("path", "")
            if entry.get("type") != "file" or not any(fnmatch.fnmatch(path, p) for p in patterns):
                continue
            lfs = entry.get("lfs")
            if lfs and lfs.get("oid"):
                sha, size = lfs["oid"], int(lfs["size"])
            else:
                sha, size = sha256_url(f"https://huggingface.co/{repo}/resolve/{revision}/{path}")
            files[path] = {"size": size, "sha256": sha}
        missing = [p for p in patterns if "*" not in p and p not in files and p not in es.OPTIONAL_FILES]
        if missing:
            raise SystemExit(f"{m['id']}: {repo}@{revision} lacks {missing}")
        pins[m["id"]] = {"repo": repo, "revision": revision, "files": dict(sorted(files.items()))}
        total = sum(f["size"] for f in files.values()) / 1e9
        print(f"{m['id']:<20} {repo}@{revision[:10]}  {len(files)} files, {total:.2f} GB")
    out = es.PINS_PATH
    out.write_text(json.dumps(pins, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("->", out)


def pin_packages():
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv isn't installed (py -3 -m pip install uv).")
    es.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, packages in (("base", es.BASE_PACKAGES), ("nvidia", es.NVIDIA_PACKAGES)):
            req = Path(tmp) / f"{name}.in"
            # NVIDIA's CUDA wheels are Windows' and Linux's - none for Macs.
            marker = " ; sys_platform == 'win32'" if name == "nvidia" else ""
            extra = INTEL_MAC if name == "base" else []
            req.write_text("".join(p + marker + "\n" for p in packages) + "".join(e + "\n" for e in extra),
                           encoding="utf-8")
            out = es.LOCKS_DIR / f"{name}.txt"
            env = dict(os.environ, UV_CUSTOM_COMPILE_COMMAND="py -3 tools/pin_transcribe.py packages")
            subprocess.run([uv, "pip", "compile", str(req), "--universal", "--generate-hashes",
                            "--python-version", "3.10", "--only-binary", ":all:", "--no-header",
                            "--no-annotate", "-o", str(out)],
                           check=True, env=env)
            lines = [l for l in out.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("--hash")]
            print(f"{name}: {len(lines)} packages -> {out}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("models", "all"):
        pin_models()
    if what in ("packages", "all"):
        pin_packages()
