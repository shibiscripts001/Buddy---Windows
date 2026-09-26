#!/usr/bin/env python3
r"""
Launcher stub for DaVinci Resolve's Workspace > Scripts menu.

Runs Buddy straight out of "buddy.zip", which must sit next to this file -
so the whole toolkit is just these two files, and moving/copying them
together (to another machine, another Scripts folder, etc.) is enough, no
dev folder required. Modeled on the per-tool launcher stubs the standalone
tools used; the rationale for every piece here - Resolve's
exec()-without-__file__ behavior, the no-console error handling, the
size+mtime cache key - is unchanged from those.

Buddy replaces those one-tool stubs. Each of them extracted its own copy of
its own package; this extracts one app containing every ported tool, so
Resolve's Scripts menu gets a single "Buddy" entry instead of one per tool.
The old stubs keep working side by side - nothing here touches them.

Like the tools it replaces, Buddy connects to Resolve's scripting API
in-process (see core/resolve_bridge.py, which still probes reachability in
a disposable subprocess first).
"""

import hashlib
import inspect
import os
import re
import shutil
import sys
import tempfile
import traceback
import zipfile

APP_NAME = "Buddy"
ZIP_NAME = "buddy.zip"
PACKAGE_DIR_NAME = "buddy"

LOG_PATH = os.path.join(tempfile.gettempdir(), "buddy_launch.log")

# Last-resort fallback if even the frame/co_filename trick below comes up
# empty - the well-known location this launcher gets deployed to.
_FALLBACK_SCRIPT_PATH = os.path.expandvars(
    r"%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts"
    r"\Utility\Buddy.py"
)


def _log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except Exception:
        pass


def _show_fatal_error(error_text):
    _log("FATAL:\n" + error_text)
    message = error_text + f"\n\n(Also written to: {LOG_PATH})"
    try:
        import ctypes

        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(
            0, message, f"{APP_NAME} - Error", MB_ICONERROR
        )
    except Exception:
        print(message)


def _this_script_path():
    try:
        return os.path.abspath(__file__)
    except NameError:
        pass

    try:
        frame = inspect.currentframe()
        path = frame.f_code.co_filename
        if path and os.path.exists(path):
            return os.path.abspath(path)
    except Exception:
        pass

    if os.path.exists(_FALLBACK_SCRIPT_PATH):
        return _FALLBACK_SCRIPT_PATH

    raise RuntimeError(
        "Could not determine this script's own file path (no __file__, no "
        "usable frame filename, and the well-known fallback location doesn't "
        "exist either)."
    )


def _extracted_app_dir(zip_path):
    if not os.path.exists(zip_path):
        raise RuntimeError(
            f"Could not find {ZIP_NAME} next to this script.\n"
            f"Expected it at: {zip_path}\n"
            f"Make sure the zip and '{APP_NAME}.py' are copied into the same "
            f"folder."
        )

    stat = os.stat(zip_path)
    cache_key = hashlib.md5(
        f"{stat.st_size}-{stat.st_mtime_ns}".encode()
    ).hexdigest()[:16]
    cache_dir = os.path.join(tempfile.gettempdir(), APP_NAME, cache_key)
    done_marker = os.path.join(cache_dir, ".extracted")

    # Keyed on the zip's size+mtime, so shipping a new zip extracts fresh
    # while relaunching the same one costs nothing.
    if not os.path.exists(done_marker):
        os.makedirs(cache_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(cache_dir)
        with open(done_marker, "w", encoding="utf-8") as handle:
            handle.write("ok")

    _hold(cache_dir)
    _remove_old_versions(cache_dir)
    return os.path.join(cache_dir, PACKAGE_DIR_NAME)


# Old extractions pile up otherwise - one folder per zip ever launched, ~3 MB
# each. A running Buddy keeps this file open in its own folder for as long as
# it runs, and Windows won't rename a folder with a file open inside it: so
# an old folder is renamed out of the way first, and only deleted if that
# worked. A copy of Buddy still running from an older version (in the tray
# while its zip was replaced) keeps its folder - it imports pages and runs
# Transcribe's worker from there long after starting.
_IN_USE_NAME = ".in_use"
_in_use_handle = None
_VERSION_DIR = re.compile(r"[0-9a-f]{16}(\.deleting)?")


def _hold(cache_dir):
    global _in_use_handle
    try:
        _in_use_handle = open(os.path.join(cache_dir, _IN_USE_NAME), "a")
    except OSError:
        pass


def _remove_old_versions(keep_dir):
    """Deletes the other extracted versions nobody is running. Only folders
    named like this stub's own cache keys are touched."""
    parent = os.path.dirname(keep_dir)
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        path = os.path.join(parent, name)
        if path == keep_dir or not _VERSION_DIR.fullmatch(name) or not os.path.isdir(path):
            continue
        doomed = path if name.endswith(".deleting") else path + ".deleting"
        if doomed != path:
            try:
                os.rename(path, doomed)
            except OSError:
                continue   # in use by a running Buddy (or otherwise locked): left alone
        shutil.rmtree(doomed, ignore_errors=True)


def _run():
    script_path = _this_script_path()
    zip_path = os.path.join(os.path.dirname(script_path), ZIP_NAME)

    _log(
        f"--- launch attempt ---\n"
        f"python: {sys.executable}\n"
        f"version: {sys.version}\n"
        f"script: {script_path}\n"
    )

    app_dir = _extracted_app_dir(zip_path)
    _log(f"app_dir: {app_dir}")
    # Buddy's modules import each other absolutely (core.*, pages.*,
    # registry), so the extracted app folder itself goes on sys.path -
    # not its parent.
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    try:
        import PySide6  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "PySide6 is not installed for the Python interpreter DaVinci "
            f"Resolve is using to run this script ({sys.executable}).\n\n"
            "Install it for that exact interpreter, e.g.:\n"
            f'"{sys.executable}" -m pip install PySide6\n\n'
            f"Original error: {exc}"
        ) from exc

    import main

    main.main(start_hidden="--start-hidden" in sys.argv[1:])


try:
    _run()
except Exception:
    _show_fatal_error(traceback.format_exc())
