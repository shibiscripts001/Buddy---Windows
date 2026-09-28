#!/usr/bin/env python3
"""
"Start Buddy automatically when Resolve starts" integration.

DaVinci Resolve has no supported hook to run a script the moment the app
itself launches (its Comp/Edit/Deliver/Utility script folders only
auto-run for their own per-context events, e.g. a new composition loading -
not application startup), so this can't be implemented by asking Resolve to
call us. Instead, a tiny, dependency-free background watcher
(resolve_watcher.py) is registered as a normal per-user OS login item -
IT polls for Resolve's own process appearing and launches Buddy once it
does, rather than the (much heavier, PySide6-dependent) Buddy itself being
loaded at every login regardless of whether Resolve is ever opened that
session.

Ported from Davinci Time Tracker's startup_manager.py - unchanged apart
from deploying/launching Buddy instead of that standalone tool, and this
feature now living at the shell level (Settings > Window) rather than
inside one tool's own settings, since it starts the whole app, not a
single tool.

Cross-platform note: written with a future macOS port in mind - the OS-
specific "register a login item" mechanism is isolated behind
is_enabled()/set_enabled() (Windows: an HKCU "Run" registry value; macOS:
TODO, would be a ~/Library/LaunchAgents/*.plist loaded via `launchctl`) so
the Settings UI doesn't need to know which OS it's running on at all - only
this module and resolve_watcher.py do.
"""

import glob
import os
import platform
import re
import subprocess
import sys

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

if IS_WINDOWS:
    import winreg

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "BuddyResolveWatcher"

# Deployed OUTSIDE Resolve's own Scripts folder on purpose - anything placed
# under Scripts/Utility shows up as a runnable entry in Workspace > Scripts,
# and resolve_watcher.py isn't meant to be launched that way.
if IS_WINDOWS:
    _WATCHER_DIR = os.path.expandvars(r"%LOCALAPPDATA%\Buddy")
elif IS_MAC:
    # TODO (Mac port): a `~/Library/Application Support/...` style directory
    # is the conventional per-user app-data location there.
    _WATCHER_DIR = os.path.expanduser("~/Library/Application Support/Buddy")
else:
    _WATCHER_DIR = None
_WATCHER_PATH = os.path.join(_WATCHER_DIR, "resolve_watcher.py") if _WATCHER_DIR else None

# Where a stop request for a running watcher goes (resolve_watcher.py's
# STOP_PATH - same directory, same name).
_STOP_PATH = os.path.join(_WATCHER_DIR, "watcher.stop") if _WATCHER_DIR else None

# The Python install manager's default folder for Python 3.14. Only one
# fallback candidate among several below - most machines have Python
# wherever python.org's installer (or the Buddy installer) put it.
_KNOWN_RESOLVE_PYTHON_DIR = os.path.expandvars(r"%LOCALAPPDATA%\Python\pythoncore-3.14-64")

# Script hosts that embed Python: sys.executable inside Resolve is one of
# these, and none of them can run a .py file as a standalone interpreter.
_HOST_EXE_NAMES = ("fuscript.exe", "resolve.exe")
_PYTHON_EXE_NAMES = ("pythonw.exe", "python.exe")


def _running_python_dirs():
    """Directories of the Python install THIS process runs on. Inside
    Resolve sys.executable is fuscript.exe, but the embedded interpreter
    still knows its real home: sys.prefix/exec_prefix (and the base_ ones)
    are the install directory getpath found from python3xx.dll, os.py lives
    in <install>\\Lib, and sys.dllhandle is that DLL itself. That's the
    install Buddy is running on right now, so the one known to have PySide6."""
    dirs = []
    if os.path.basename(sys.executable).lower() not in _HOST_EXE_NAMES:
        dirs.append(os.path.dirname(sys.executable))
    base_exe = getattr(sys, "_base_executable", None)
    if base_exe and os.path.basename(base_exe).lower() not in _HOST_EXE_NAMES:
        dirs.append(os.path.dirname(base_exe))
    for prefix in (sys.exec_prefix, sys.prefix, sys.base_exec_prefix, sys.base_prefix):
        if prefix:
            dirs.append(prefix)
    try:
        dirs.append(os.path.dirname(os.path.dirname(os.path.abspath(os.__file__))))
    except Exception:
        pass
    dll_dir = _python_dll_dir()
    if dll_dir:
        dirs.append(dll_dir)
    return dirs


def running_python_exe():
    """The console python.exe of the install Buddy is running on - what a
    "pip install" hint should name, since inside Resolve sys.executable is
    fuscript.exe, which can't run pip. None if it can't be found."""
    for directory in _running_python_dirs():
        candidate = os.path.join(directory, "python.exe" if IS_WINDOWS else "python3")
        if os.path.isfile(candidate):
            return candidate
    return None


def _python_dll_dir():
    if not IS_WINDOWS or not hasattr(sys, "dllhandle"):
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(sys.dllhandle), buf, len(buf)):
            return os.path.dirname(buf.value)
    except Exception:
        pass
    return None


def _py_launcher_python_dir():
    """What "py -3" resolves to - the same lookup Resolve's fuscript.exe
    uses to pick its Python (see installer/Buddy.iss)."""
    if not IS_WINDOWS:
        return None
    launchers = [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"),
                 os.path.expandvars(r"%WINDIR%\py.exe"), "py"]
    for launcher in launchers:
        if launcher != "py" and not os.path.isfile(launcher):
            continue
        try:
            result = subprocess.run(
                [launcher, "-3", "-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            continue
        exe = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if result.returncode == 0 and exe and os.path.isfile(exe):
            return os.path.dirname(exe)
    return None


def _standard_python_dirs():
    """python.org's per-user and all-users install folders (newest first),
    then the Python install manager's."""
    def minor_version(path):  # Python313 / pythoncore-3.14-64 -> 13 / 14
        match = re.search(r"3\.?(\d+)", os.path.basename(path))
        return int(match.group(1)) if match else -1

    dirs = []
    for pattern in (r"%LOCALAPPDATA%\Programs\Python\Python3*", r"%ProgramFiles%\Python3*",
                    r"%LOCALAPPDATA%\Python\pythoncore-3*"):
        found = glob.glob(os.path.expandvars(pattern))
        dirs.extend(sorted(found, key=minor_version, reverse=True))
    return dirs


def _candidate_python_dirs():
    """In order of how sure each is to have PySide6: the running install
    first, then whatever Resolve itself would use, then the usual places.
    A generator, so the "py -3" subprocess only runs when the running
    install didn't already give an answer (sync_if_enabled calls this on
    every Buddy launch)."""
    seen = set()

    def fresh(dirs):
        for d in dirs:
            key = os.path.normcase(os.path.abspath(d))
            if key not in seen:
                seen.add(key)
                yield d

    yield from fresh(_running_python_dirs())
    py_dir = _py_launcher_python_dir()
    if py_dir:
        yield from fresh([py_dir])
    yield from fresh(_standard_python_dirs())
    yield from fresh([_KNOWN_RESOLVE_PYTHON_DIR])


def _find_python_for_startup():
    """Prefers a windowless pythonw.exe (no console flash at login) over
    python.exe, checked in each candidate directory in turn. The watcher
    reuses this exact interpreter (via its own sys.executable) to relaunch
    Buddy later, so this is the one place that needs to get it right.

    None if no real interpreter was found - never fuscript.exe/Resolve.exe,
    which can't run the watcher (a Run entry pointing at one silently does
    nothing at every login)."""
    for directory in _candidate_python_dirs():
        for name in _PYTHON_EXE_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _watcher_command(python_exe):
    return f'"{python_exe}" "{_WATCHER_PATH}"'


def _read_run_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, _VALUE_NAME)[0]
    except OSError:
        return None


def _clear_stop_request():
    try:
        os.remove(_STOP_PATH)
    except OSError:
        pass


def _request_watcher_stop():
    """Asks a running watcher to exit (it checks for this file every second
    or so). It would also notice its Run value is gone within one poll -
    this just makes it immediate, and covers a watcher that can't read the
    registry. A watcher from before this existed is found and stopped by
    resolve_watcher's own legacy handling instead (see
    _stop_legacy_watcher)."""
    try:
        os.makedirs(_WATCHER_DIR, exist_ok=True)
        with open(_STOP_PATH, "w", encoding="utf-8") as f:
            f.write("stop\n")
    except OSError:
        pass
    _stop_legacy_watcher()


def _stop_legacy_watcher():
    """Older watchers (only a watcher.pid file, no lock, no stop check) can
    only be stopped by killing them - done only once the PID's command line
    confirms it is that watcher. Best-effort."""
    try:
        from core import resolve_watcher
        resolve_watcher.stop_legacy_watcher(_WATCHER_DIR, _WATCHER_PATH)
    except Exception:
        pass


def _deploy_watcher():
    """(Re)writes resolve_watcher.py's source to its deployed location -
    imports the actual module (packaged inside buddy.zip right next to this
    file) rather than embedding a duplicate copy of its source as a string
    here, so there's exactly one place that file's logic lives."""
    from core import resolve_watcher
    os.makedirs(os.path.dirname(_WATCHER_PATH), exist_ok=True)
    with open(resolve_watcher.__file__, "r", encoding="utf-8") as src:
        content = src.read()
    with open(_WATCHER_PATH, "w", encoding="utf-8") as dst:
        dst.write(content)


def _spawn_watcher_now(python_exe=None):
    """A Windows "Run" registry entry (and, later, a macOS LaunchAgent) only
    actually starts a process on the NEXT login - it does nothing for
    whatever session is already running when the checkbox is toggled on, or
    for a session that started before this app was ever updated to include
    this feature. Called right after enabling the setting, and defensively
    on every Buddy startup (`sync_if_enabled()`) - safe to call even if a
    watcher is already running, since `resolve_watcher.py`'s own lock file
    makes a redundant extra one exit immediately instead of double-polling."""
    try:
        python_exe = python_exe or _find_python_for_startup()
        if not python_exe:
            return
        popen_kwargs = {}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS
            # Suppresses Windows' busy-cursor animation for this launch -
            # see resolve_watcher.py's identical fix for why this matters.
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= 0x00000080  # STARTF_FORCEOFFFEEDBACK
            popen_kwargs["startupinfo"] = startupinfo
        subprocess.Popen([python_exe, _WATCHER_PATH], **popen_kwargs)
    except Exception:
        pass  # best-effort - the next login's registered entry is still the durable fallback


def is_enabled():
    if IS_WINDOWS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, _VALUE_NAME)
            return True
        except OSError:
            return False
    return False  # TODO (Mac port): read the LaunchAgent plist's presence/state


def set_enabled(enabled):
    """Raises on failure (e.g. registry access denied) - the caller is
    expected to show that to the user rather than silently no-op, since a
    checkbox that doesn't actually do what it displays would be worse than
    an explicit error."""
    if not IS_WINDOWS:
        raise NotImplementedError(
            "\"Start Buddy automatically when Resolve starts\" isn't implemented for "
            f"{platform.system()} yet."
        )

    if enabled:
        python_exe = _find_python_for_startup()
        if not python_exe:
            raise RuntimeError(
                "Couldn't find a Python interpreter (pythonw.exe or python.exe) to run the "
                "background helper with. Run the Buddy installer again, or install Python 3.10 "
                "or newer from python.org, then try again."
            )
        _deploy_watcher()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _watcher_command(python_exe))
        # A stop request left from disabling earlier would make the new
        # watcher exit straight away.
        _clear_stop_request()
        # The registry entry alone only takes effect on the NEXT login -
        # without this, enabling the checkbox would silently do nothing
        # until the user next logs in.
        _spawn_watcher_now(python_exe)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _VALUE_NAME)
        except FileNotFoundError:
            pass
        # Removing the Run value only stops the NEXT login's watcher - the
        # one running now would otherwise keep launching Buddy until logout.
        _request_watcher_stop()


def sync_if_enabled():
    """Called once at app startup (see main.py) - keeps an already-deployed
    watcher's source in sync with whatever version of Buddy is currently
    installed, without requiring the user to re-toggle the Settings
    checkbox after every update, AND makes sure a watcher is actually
    running for the current session (self-healing: covers a watcher that
    crashed, was never started because the setting was enabled before this
    self-heal existed, or simply hasn't run yet this login - the lock file
    in resolve_watcher.py makes this safe to call unconditionally even when
    a watcher is already alive).

    Also repairs the Run value itself when it no longer points at a real
    interpreter - older versions could write fuscript.exe there when the
    setting was turned on from inside Resolve, which never starts anything."""
    try:
        if is_enabled():
            _deploy_watcher()
            python_exe = _find_python_for_startup()
            if python_exe and IS_WINDOWS:
                command = _read_run_value() or ""
                registered = command.split('"')[1] if command.startswith('"') and command.count('"') >= 2 else ""
                if os.path.basename(registered).lower() not in _PYTHON_EXE_NAMES or not os.path.isfile(registered):
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _watcher_command(python_exe))
            _spawn_watcher_now(python_exe)
    except Exception:
        pass  # best-effort background sync - must never block a normal launch
