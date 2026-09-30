"""Updating Buddy from inside Buddy - no installer for an ordinary release.

What an install is: Buddy.py (the launcher) and buddy.zip (the whole app) in
Resolve's Scripts/Utility folder, and the packages in requirements.txt in the
Python Resolve runs scripts with. A release almost always changes only
buddy.zip, and the launcher already unpacks a zip it hasn't seen (Buddy.py:
it's keyed on the zip's size and time), so updating is: fetch the new zip,
check it, put it in place, restart.

Each GitHub release carries, next to the installer (.github/workflows/
release.yml, build_update_manifest.py):
    buddy-update.json   the version, the zip's and launcher's size and SHA-256,
                        the packages it needs, what changed
    buddy.zip, Buddy.py the files themselves
Once a day at most (like core/announcements.py) Buddy reads the LATEST
release's buddy-update.json - a plain HTTPS request to GitHub, carrying
nothing about the user - and a newer version shows an Update button in the
header. Settings' "Check for Buddy updates" turns it off.

Updating, when the user says so: both files are downloaded over HTTPS from
github.com (and GitHub's own download hosts, nowhere else), each has to be
exactly the size and SHA-256 the manifest gives, and the zip has to be a
Buddy - buddy/main.py, and buddy/VERSION saying the version offered. Only
then is the running one kept as buddy.zip.previous (Buddy.py.previous) and
the new one moved into its place. Settings' "Roll back" swaps them back.

A release that needs a package this Python doesn't have (or a newer one)
can't be installed this way - Windows won't let pip replace what a running
Buddy has loaded - so for those the button opens the release page instead.

Not signed: the files are trusted as far as GitHub's HTTPS and the account
publishing the releases are. A signature would go in the manifest
("signature", over the zip's SHA-256) and be checked in check_download()
with a public key kept in this file; nothing else would change.

Run from its source folder (not from buddy.zip) Buddy has nothing to update,
and none of this runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from core.app_version import buddy_version

REPO = "shibiscripts001/Buddy---Windows"
PLATFORM = "windows"
RELEASES = f"https://github.com/{REPO}/releases"
MANIFEST_NAME = "buddy-update.json"
MANIFEST_URL = f"{RELEASES}/latest/download/{MANIFEST_NAME}"
# Where a release's files may come from - github.com redirects its download
# links to one of GitHub's own file hosts.
ALLOWED_HOSTS = ("github.com",)
ALLOWED_HOST_SUFFIXES = (".githubusercontent.com",)

ZIP_NAME, LAUNCHER_NAME = "buddy.zip", "Buddy.py"
PREVIOUS = ".previous"
PARTIAL = ".download"

CHECK_EVERY = 24 * 3600
RETRY_AFTER = 3600            # after a failed check
FIRST_CHECK_DELAY_MS = 20_000  # let Buddy finish starting first
MANIFEST_MAX = 64 * 1024
ZIP_MAX = 200 * 1024 * 1024
NOTES_MAX = 3000

ENABLED_KEY = "updates_enabled"
CACHE_KEY = "update_manifest"         # the last manifest read, checked again when used
CHECKED_KEY = "update_checked_at"
SKIP_KEY = "update_skip"              # a version rolled back from: not offered again

_HEX64 = re.compile(r"[0-9a-f]{64}")
_CACHE_DIR = re.compile(r"[0-9a-f]{16}")
_APP_DIR = Path(__file__).resolve().parent.parent       # buddy/ when run from the zip


# ------------------------------------------------------------- versions --

def version_key(version: str) -> tuple:
    """"1.1.33" -> (1, 1, 33); anything after the numbers is ignored."""
    parts = []
    for piece in str(version or "").split("."):
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        parts.append(int(digits.group()))
    return tuple(parts)


def is_newer(offered: str, current: str) -> bool:
    return bool(version_key(offered)) and version_key(offered) > version_key(current)


# ------------------------------------------------------------- manifest --

def _file_entry(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    size, sha = value.get("size"), value.get("sha256")
    if (isinstance(size, int) and not isinstance(size, bool) and 0 < size <= ZIP_MAX
            and isinstance(sha, str) and _HEX64.fullmatch(sha)):
        return {"size": size, "sha256": sha}
    return None


def parse_manifest(data: bytes, platform: str = PLATFORM) -> dict | None:
    """A release's buddy-update.json, checked - None if it isn't one for this
    platform. Anything unexpected is dropped rather than trusted."""
    if not data or len(data) > MANIFEST_MAX:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != 1 or doc.get("platform") != platform:
        return None
    version = doc.get("version")
    zip_entry = _file_entry(doc.get("zip"))
    if not isinstance(version, str) or not version_key(version) or zip_entry is None:
        return None
    launcher = _file_entry(doc.get("launcher")) if doc.get("launcher") is not None else None
    requires = []
    for req in doc.get("requires") or []:
        if isinstance(req, dict) and isinstance(req.get("name"), str) and req["name"].strip():
            requires.append({"name": req["name"].strip(), "spec": str(req.get("spec") or ""),
                             "marker": str(req.get("marker") or "")})
    notes = doc.get("notes") if isinstance(doc.get("notes"), str) else ""
    return {"version": version, "zip": zip_entry, "launcher": launcher, "requires": requires,
            "notes": notes.strip()[:NOTES_MAX], "page": f"{RELEASES}/tag/v{version}"}


def asset_url(version: str, name: str) -> str:
    return f"{RELEASES}/download/v{version}/{name}"


def allowed_url(url: QUrl | str) -> bool:
    """HTTPS, to GitHub - where a release's files are (after its redirects)."""
    url = QUrl(url) if isinstance(url, str) else url
    host = url.host().lower()
    return url.scheme() == "https" and (host in ALLOWED_HOSTS or host.endswith(ALLOWED_HOST_SUFFIXES))


# --------------------------------------------------------- requirements --

def _marker_applies(marker: str) -> bool:
    """Only sys_platform markers are written in requirements.txt."""
    found = re.search(r"""sys_platform\s*(==|!=)\s*["']([^"']+)["']""", marker or "")
    if not found:
        return True
    return (sys.platform == found.group(2)) == (found.group(1) == "==")


def _satisfies(installed: str, spec: str) -> bool:
    have = version_key(installed)
    for clause in filter(None, (c.strip() for c in spec.split(","))):
        op = re.match(r"(~=|==|!=|>=|<=|>|<)\s*(.+)", clause)
        if not op:
            continue
        want = version_key(op.group(2))
        if op.group(1) == "~=":
            ok = have >= want
        else:
            ok = {"==": have[:len(want)] == want, "!=": have[:len(want)] != want, ">=": have >= want,
                  "<=": have <= want, ">": have > want, "<": have < want}[op.group(1)]
        if not ok:
            return False
    return True


def missing_requirements(requires: list[dict], version_of=None) -> list[str]:
    """The packages a release needs that this Python lacks (or has too old
    or new a one of): "numpy>=2" - what the installer would have to add."""
    if version_of is None:
        from importlib import metadata

        def version_of(name):
            try:
                return metadata.version(name)
            except metadata.PackageNotFoundError:
                return None
    missing = []
    for req in requires:
        if not _marker_applies(req.get("marker", "")):
            continue
        have = version_of(req["name"])
        if have is None or not _satisfies(have, req.get("spec", "")):
            missing.append(req["name"] + req.get("spec", ""))
    return missing


# ---------------------------------------------------------- the install --

def scripts_dir() -> Path:
    """Resolve's per-user Scripts/Utility folder, where the installer puts Buddy."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
    return Path(os.path.expandvars(r"%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"))


def installed_zip(app_dir: Path = _APP_DIR) -> Path | None:
    """The buddy.zip this Buddy runs from - None when it runs from its source
    folder. The launcher names it (BUDDY_ZIP, from Buddy.py); an older one
    doesn't, and then it's the one in the Scripts folder, if this Buddy is an
    unpacked zip (%TEMP%/Buddy/<key>/buddy - see Buddy.py)."""
    named = os.environ.get("BUDDY_ZIP")
    if named and Path(named).is_file():
        return Path(named)
    unpacked = (app_dir.name == "buddy" and _CACHE_DIR.fullmatch(app_dir.parent.name)
                and app_dir.parent.parent.name == "Buddy")
    candidate = scripts_dir() / ZIP_NAME
    return candidate if unpacked and candidate.is_file() else None


_zip_versions: dict = {}      # (path, file id, size, mtime) -> version: the header asks often


def zip_version(path: Path) -> str:
    """The VERSION inside a Buddy zip, or ""."""
    try:
        stat = path.stat()
        key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if key not in _zip_versions:
            with zipfile.ZipFile(path) as archive:
                _zip_versions[key] = archive.read("buddy/VERSION").decode("utf-8").strip()[:20]
        return _zip_versions[key]
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
        return ""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_download(path: Path, entry: dict, version: str | None = None) -> str:
    """"" when the file at `path` is what the manifest's entry says (and, for
    the zip, a Buddy of that version); else what's wrong with it."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"it couldn't be read ({exc})"
    if size != entry["size"]:
        return f"it's {size:,} bytes, not the {entry['size']:,} the release says"
    if sha256_of(path) != entry["sha256"]:
        return "its contents aren't what the release says (SHA-256 differs)"
    if version is None:
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "buddy/main.py" not in names:
                return "it isn't a Buddy (no buddy/main.py)"
            if archive.testzip() is not None:
                return "it's damaged"
    except zipfile.BadZipFile:
        return "it isn't a zip"
    inside = zip_version(path)
    if inside != version:
        return f"it says it's version {inside or 'unknown'}, not {version}"
    return ""


def put_in_place(new_file: Path, target: Path):
    """The running file kept as <name>.previous, the new one moved into its place."""
    if target.exists():
        shutil.copy2(target, target.with_name(target.name + PREVIOUS))
    os.replace(new_file, target)


def previous_version(zip_path: Path | None) -> str:
    """The version Roll back would go back to - "" if there's none."""
    if zip_path is None:
        return ""
    previous = zip_path.with_name(zip_path.name + PREVIOUS)
    return zip_version(previous) if previous.is_file() else ""


def roll_back(zip_path: Path) -> str:
    """Swaps buddy.zip (and Buddy.py, if it was updated too) with the copies
    the last update kept. Returns the version now in place."""
    folder = zip_path.parent
    for name in (ZIP_NAME, LAUNCHER_NAME):
        current, previous = folder / name, folder / (name + PREVIOUS)
        if not previous.is_file():
            continue
        spare = folder / (name + ".swap")
        if current.exists():
            os.replace(current, spare)
        os.replace(previous, current)
        if spare.exists():
            os.replace(spare, previous)
    # A fresh time, so the launcher unpacks it rather than finding an old folder.
    os.utime(zip_path, None)
    return zip_version(zip_path)


# --------------------------------------------------------------- restart --

# Waits for this Buddy to have quit (its single-instance lock goes with it),
# then starts the launcher again. A process of its own: this one is gone by then.
_RELAUNCH = r"""
import os, subprocess, sys, time
pid, python, launcher = int(sys.argv[1]), sys.argv[2], sys.argv[3]
def alive():
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
deadline = time.time() + 60
while alive() and time.time() < deadline:
    time.sleep(0.25)
time.sleep(0.5)
kw = {"creationflags": 0x00000008} if os.name == "nt" else {"start_new_session": True}
subprocess.Popen([python, launcher], close_fds=True, **kw)
"""


# Where Buddy can start itself again. Not on macOS: there only a launch from
# Resolve's own Scripts menu can connect to the free Resolve
# (core/resolve_bridge.py), so the user reopens it from there.
CAN_RELAUNCH = sys.platform != "darwin"


def relaunch_after_quit(launcher: Path) -> bool:
    """Arranges for Buddy to start again once this process has quit. False
    if there's no Python to start it with (then the user reopens it)."""
    if not CAN_RELAUNCH:
        return False
    from core import startup_manager
    python = startup_manager.python_for_relaunch()
    if not python or not launcher.is_file():
        return False
    kwargs = {"close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([python, "-c", _RELAUNCH, str(os.getpid()), python, str(launcher)], **kwargs)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- network --

class _QtFetch:
    """GET a URL (HTTPS to GitHub only, redirects followed within it) into a
    file or memory. Replaced in tests."""

    def __init__(self, parent):
        self.net = QNetworkAccessManager(parent)

    def get(self, url, done, progress=None, to_file=None, limit=MANIFEST_MAX):
        """done(data_or_path_or_None, error_text); progress(received, total)."""
        if not allowed_url(url):
            done(None, "not a GitHub address")
            return None
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(60_000)
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
        request.setAttribute(QNetworkRequest.CookieSaveControlAttribute, QNetworkRequest.Manual)
        request.setAttribute(QNetworkRequest.CookieLoadControlAttribute, QNetworkRequest.Manual)
        try:
            sink = open(to_file, "wb") if to_file else None
        except OSError as exc:
            done(None, str(exc))
            return None
        reply = self.net.get(request)
        state = {"size": 0, "bad": ""}

        def read():
            if not allowed_url(reply.url()):
                state["bad"] = "it was sent somewhere other than GitHub"
                reply.abort()
                return
            chunk = bytes(reply.readAll())
            state["size"] += len(chunk)
            if state["size"] > limit:
                state["bad"] = "it's bigger than the release says"
                reply.abort()
                return
            if sink:
                sink.write(chunk)
            else:
                state.setdefault("data", bytearray()).extend(chunk)

        def finished():
            read()
            if sink:
                sink.close()
            error = state["bad"] or ("" if reply.error() == QNetworkReply.NoError else reply.errorString())
            reply.deleteLater()
            if error:
                done(None, error)
            else:
                done(Path(to_file) if to_file else bytes(state.get("data", b"")), "")

        reply.readyRead.connect(read)
        reply.finished.connect(finished)
        if progress is not None:
            reply.downloadProgress.connect(lambda got, total: progress(int(got), int(total)))
        return reply


# ---------------------------------------------------------------- checker --

class UpdateChecker(QObject):
    """The state behind the header's Update button and Settings' Updates."""

    changed = Signal()
    progress = Signal(str)               # "Downloading Buddy 1.1.33… 40%"
    installed = Signal(str)              # the version now in place (restart to run it)
    failed = Signal(str)                 # why an update didn't happen

    def __init__(self, settings, parent=None, clock=time.time, fetch=None, zip_path=...):
        super().__init__(parent)
        self.settings = settings
        self.clock = clock
        self.fetch = fetch or _QtFetch(self)
        self.zip_path = installed_zip() if zip_path is ... else zip_path
        self.current = buddy_version()
        self._checking = False
        self._busy = False
        self._next_try = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(60 * 60 * 1000)   # looks at the clock; fetches once a day
        self._timer.timeout.connect(self._maybe_check)

    # ---------------------------------------------------------------- state

    @property
    def supported(self) -> bool:
        """False when run from the source folder: nothing to update."""
        return self.zip_path is not None and bool(self.current)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get(ENABLED_KEY, True))

    def manifest(self) -> dict | None:
        cached = self.settings.get(CACHE_KEY)
        return parse_manifest(json.dumps(cached).encode("utf-8")) if isinstance(cached, dict) else None

    def offer(self) -> dict | None:
        """The update to offer: {"version", "notes", "size", "missing" (packages
        the installer would add, [] if none), "page"} - or None."""
        m = self.manifest()
        if (not self.supported or not self.enabled or m is None or not is_newer(m["version"], self.current)
                or m["version"] == self.settings.get(SKIP_KEY) or self.pending()):
            return None
        return {"version": m["version"], "notes": m["notes"], "size": m["zip"]["size"], "page": m["page"],
                "missing": missing_requirements(m["requires"])}

    def pending(self) -> str:
        """A version put in place - by an update or a roll back - but not
        running yet (restart to use it), or ""."""
        if not self.supported:
            return ""
        in_place = zip_version(self.zip_path)
        return in_place if in_place and version_key(in_place) != version_key(self.current) else ""

    def previous(self) -> str:
        return previous_version(self.zip_path) if self.supported else ""

    def header_state(self) -> dict | None:
        """What the header's button says: an update ready, one waiting for a
        restart, or nothing."""
        pending = self.pending()
        if pending:
            return {"kind": "restart", "version": pending}
        offer = self.offer()
        return {"kind": "update", "version": offer["version"]} if offer else None

    # -------------------------------------------------------------- control

    def start(self):
        if self.supported and self.enabled:
            self._timer.start()
            QTimer.singleShot(FIRST_CHECK_DELAY_MS, self._maybe_check)
        self.changed.emit()

    def set_enabled(self, on: bool):
        self.settings[ENABLED_KEY] = bool(on)
        self.settings.save()
        if on and self.supported:
            self._timer.start()
            self._maybe_check()
        else:
            self._timer.stop()
        self.changed.emit()

    def check_now(self, done=None):
        """Settings' "Check for updates now": ignores the once-a-day rule and
        a version skipped by rolling back. done(error_text) when answered."""
        self.settings[SKIP_KEY] = ""
        self._check(done)

    def _maybe_check(self):
        now = self.clock()
        due = now - float(self.settings.get(CHECKED_KEY, 0) or 0) >= CHECK_EVERY
        if self.supported and self.enabled and due and now >= self._next_try:
            self._check()

    def _check(self, done=None):
        if not self.supported or self._checking:
            if done:
                done("" if self.supported else "Buddy is running from its source folder.")
            return
        self._checking = True

        def answered(data, error):
            self._checking = False
            manifest = parse_manifest(data) if data is not None else None
            if re.search(r"not found|404", error or "", re.I):
                error = ""        # a release from before Buddy updated itself: nothing to read
            if manifest is None:
                self._next_try = self.clock() + RETRY_AFTER
                if done:
                    done(error or "The latest release has no update information.")
                return
            self.settings[CACHE_KEY] = {"format": 1, "platform": PLATFORM, "version": manifest["version"],
                                        "zip": manifest["zip"], "launcher": manifest["launcher"],
                                        "requires": manifest["requires"], "notes": manifest["notes"]}
            self.settings[CHECKED_KEY] = self.clock()
            self.settings.save()
            self.changed.emit()
            if done:
                done("")
        self.fetch.get(MANIFEST_URL, answered)

    # ------------------------------------------------------------- updating

    def download_and_install(self):
        """Fetches the offered version's zip (and launcher, if it changed),
        checks them and puts them in place. installed(version) or failed(why)."""
        offer, m = self.offer(), self.manifest()
        if self._busy or offer is None or m is None or offer["missing"]:
            return
        self._busy = True
        version, folder = m["version"], self.zip_path.parent
        partial = folder / (ZIP_NAME + PARTIAL)
        launcher = folder / LAUNCHER_NAME
        need_launcher = m["launcher"] is not None and (not launcher.is_file()
                                                        or sha256_of(launcher) != m["launcher"]["sha256"])

        def fail(why):
            self._busy = False
            for leftover in (partial, folder / (LAUNCHER_NAME + PARTIAL)):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            self.failed.emit(why)

        def install_launcher():
            if not need_launcher:
                return finish()
            target = folder / (LAUNCHER_NAME + PARTIAL)

            def got(path, error):
                if path is None:
                    return fail(f"Buddy.py couldn't be downloaded: {error}")
                problem = check_download(path, m["launcher"])
                if problem:
                    return fail(f"The downloaded Buddy.py was refused: {problem}.")
                finish(path)
            self.fetch.get(asset_url(version, LAUNCHER_NAME), got, to_file=target, limit=m["launcher"]["size"])

        def finish(new_launcher=None):
            try:
                put_in_place(partial, self.zip_path)
                if new_launcher is not None:
                    put_in_place(new_launcher, launcher)
            except OSError as exc:
                return fail(f"It couldn't be put in place: {exc}")
            self._busy = False
            self.settings[SKIP_KEY] = ""
            self.settings.save()
            self.installed.emit(version)
            self.changed.emit()

        def got_zip(path, error):
            if path is None:
                return fail(f"It couldn't be downloaded: {error}")
            problem = check_download(path, m["zip"], version)
            if problem:
                return fail(f"The download was refused: {problem}.")
            install_launcher()

        def on_progress(got, total):
            total = total if total > 0 else m["zip"]["size"]
            self.progress.emit(f"Downloading Buddy {version}… {min(100, round(100 * got / total))}%")

        self.progress.emit(f"Downloading Buddy {version}…")
        self.fetch.get(asset_url(version, ZIP_NAME), got_zip, progress=on_progress, to_file=partial,
                       limit=m["zip"]["size"])

    def roll_back(self) -> str:
        """Back to the version the last update replaced - not offered again
        until Check for updates now. Returns the version now in place."""
        if not self.previous():
            return ""
        undone = zip_version(self.zip_path)
        version = roll_back(self.zip_path)
        if undone:
            self.settings[SKIP_KEY] = undone
            self.settings.save()
        self.changed.emit()
        return version

    def launcher_path(self) -> Path | None:
        return self.zip_path.parent / LAUNCHER_NAME if self.supported else None
