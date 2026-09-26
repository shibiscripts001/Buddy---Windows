#!/usr/bin/env python3
"""
Lightweight, dependency-free background watcher for Buddy's "Start Buddy
automatically when Resolve starts" setting (see startup_manager.py).

Deployed (by startup_manager.py, not manually, and not shipped loose in
Resolve's own Scripts folder - see that module's docstring for why) as this
OS's own per-user login item. Its only job: poll for DaVinci Resolve's
process starting, and launch Buddy itself once it does - kept completely
free of any non-stdlib dependency (no PySide6, no importing anything else
from this package) since it needs to sit resident in the background from
login onward, not just while Buddy itself happens to be open.

Ported from Davinci Time Tracker's resolve_watcher.py - unchanged apart
from launching Buddy's own deployed launcher stub instead of Time
Tracker's, and this file's own deployed name/paths.

Reuses `sys.executable` to relaunch Buddy rather than re-deriving which
Python to use - `startup_manager.py` already picked an interpreter with
PySide6 installed when the checkbox was enabled, and that's the exact
interpreter this watcher itself is running under (it's what the OS login-
item entry points at), so it's already the right one.

Important: an OS login-item entry (Windows' HKCU "Run" key; the future
macOS LaunchAgent) only actually starts a process on the NEXT login/logon -
enabling the Settings checkbox does NOT retroactively start a watcher for
whatever session happens to already be running. `startup_manager.py`
compensates by also launching one immediately (`_spawn_watcher_now()`)
right when the checkbox is turned on, and again defensively every time
Buddy itself starts (`sync_if_enabled()`, called from `main.py`) - the lock
file below (`_acquire_single_instance_lock`) is what makes launching "one
more, just in case" safe rather than accumulating duplicate watchers
polling in parallel.

Stopping: turning the setting off removes the Run value and writes a stop
file (startup_manager.py); the watcher checks for both while it runs and
exits, rather than carrying on launching Buddy until the next logout.

Cross-platform note: written with a future macOS port in mind. The polling
loop (`main()`) itself is already portable; only `_is_resolve_running()`
and `_launcher_path()` are platform-specific, and the macOS branches below
are explicitly marked TODO rather than guessed at - they should be
confirmed against a real Mac install before relying on them.
"""

import hashlib
import os
import platform
import subprocess
import sys
import time

POLL_SECONDS = 5

# Resolve.exe existing as an OS process does not mean its own scripting
# server is actually ready yet (it's still well within its own splash-
# screen/initialization sequence at that point) - connecting too early has
# been observed to leave that session's scripting connection unusable for
# the rest of its lifetime. Waiting this long after the not-running ->
# running transition, before ever launching Buddy (and therefore before its
# first scripting connection attempt), gives Resolve's own startup a real
# head start.
STARTUP_GRACE_SECONDS = 30

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# Deployed alongside this file itself (see startup_manager.py's _WATCHER_DIR)
# - using its own directory rather than something Windows/Mac-specific
# means the log/lock paths need no separate per-OS branch of their own.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_BASE_DIR, "resolve_watcher.log")
# Held locked for the watcher's whole lifetime - the OS releases it when
# the process ends, however it ends, so unlike a PID file it can't be
# fooled by Windows reusing an old watcher's PID after a reboot.
LOCK_PATH = os.path.join(_BASE_DIR, "watcher.lock")
# The lock holder's pid + source version, for a newer watcher to read.
INFO_PATH = os.path.join(_BASE_DIR, "watcher.info")
# Its presence asks the running watcher to exit (startup_manager.py writes
# it when the setting is turned off; a newer watcher writes it to replace
# an older one).
STOP_PATH = os.path.join(_BASE_DIR, "watcher.stop")
# What watchers before the lock file wrote instead (pid + version). Only
# read now, to find and stop one of those still running.
LEGACY_PID_FILENAME = "watcher.pid"

# How long a newer watcher waits for an older one to honour its stop
# request (it checks about once a second, but a tasklist call can take a
# few seconds).
REPLACE_TIMEOUT_SECONDS = 20

# startup_manager.py's Run entry - the watcher exits once it's gone. Kept
# as literals here since this file can't import anything from the package.
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "BuddyResolveWatcher"

# Win32 STARTUPINFO.dwFlags bit (not exposed as a named constant by
# Python's own subprocess module, but the numeric value is documented and
# stable) - suppresses Windows' "an application is starting" busy-cursor
# animation for a short-lived helper process launch. This watcher spawns
# one of these every POLL_SECONDS, forever, for as long as the user is
# logged in.
_STARTF_FORCEOFFFEEDBACK = 0x00000080


def _no_feedback_startupinfo():
    """None on non-Windows (always safe to pass through as the `startupinfo`
    kwarg regardless of platform - Popen only consults it on Windows)."""
    if not IS_WINDOWS:
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= _STARTF_FORCEOFFFEEDBACK
    return startupinfo


def _log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def _launcher_path():
    if IS_WINDOWS:
        return os.path.expandvars(
            r"%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\Buddy.py"
        )
    if IS_MAC:
        # TODO (Mac port): confirm this is actually where Buddy.py gets
        # deployed on macOS - DaVinci Resolve's per-user Fusion scripts
        # folder there is normally under "~/Library/Application Support/
        # Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/", by
        # analogy with the Windows path above.
        return os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
            "Fusion/Scripts/Utility/Buddy.py"
        )
    raise NotImplementedError(f"Unsupported platform: {platform.system()}")


def _is_resolve_running():
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Resolve.exe"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=_no_feedback_startupinfo(),
            )
            return "Resolve.exe" in result.stdout
        if IS_MAC:
            # TODO (Mac port): confirm "Resolve" is the real process name
            # Resolve.app runs under before relying on this in production.
            result = subprocess.run(
                ["pgrep", "-x", "Resolve"], capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
    except Exception as exc:
        _log(f"_is_resolve_running() check failed: {exc!r}")  # not detected this tick, not a crash
    return False


def _launch_buddy():
    launcher = _launcher_path()
    if not os.path.exists(launcher):
        _log(f"Buddy launcher not found at {launcher} - not launching.")
        return
    try:
        popen_kwargs = {}
        if IS_WINDOWS:
            # Detach fully from this watcher process (its own console, if
            # any, and its process group) - Buddy is a real GUI app in its
            # own right, not a child that should die with the watcher.
            popen_kwargs["creationflags"] = subprocess.DETACHED_PROCESS
            popen_kwargs["startupinfo"] = _no_feedback_startupinfo()
        # --start-hidden: this is an auto-start, not something the user just
        # asked for by opening Workspace > Scripts - it should open straight
        # into the tray, not pop up its full window unprompted (see
        # Buddy.py and main.py's main(start_hidden=...) for the other half
        # of this).
        subprocess.Popen([sys.executable, launcher, "--start-hidden"], **popen_kwargs)
        _log(f"Launched Buddy via {sys.executable} {launcher}")
    except Exception as exc:
        _log(f"Failed to launch Buddy: {exc!r}")  # will just try again on the next transition


def _own_source_hash():
    """A short fingerprint of this exact file's contents - lets a freshly
    started watcher tell whether an already-running one (found via the lock
    file) is the SAME code or a stale, superseded version, since
    `sync_if_enabled()` (main.py) redeploys this file on every Buddy
    startup but has no way to force an already-running background process
    to pick up the change on its own otherwise."""
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return "unknown"


def _kill_pid(pid):
    """Only ever called on a PID `_is_watcher_process` has confirmed."""
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=_no_feedback_startupinfo(),
            )
        else:
            os.kill(pid, 15)  # TODO (Mac port): confirm SIGTERM is the right signal here
    except Exception:
        pass


def _process_command_line(pid):
    """The command line of process `pid`: "" if there's no such process,
    None if it couldn't be checked. PowerShell's CIM query rather than wmic,
    which newer Windows 11 builds no longer ship."""
    try:
        if IS_WINDOWS:
            script = (
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=_no_feedback_startupinfo(),
            )
        else:
            # TODO (Mac port): `ps -o command=` is the POSIX equivalent; untested there.
            result = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                                    capture_output=True, timeout=5)
            if result.returncode == 1 and not result.stdout.strip():
                return ""
        if IS_WINDOWS and result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def _is_watcher_process(pid, watcher_path):
    """True only when pid's command line runs `watcher_path` (this watcher
    script) - a PID from a file can belong to any unrelated program after a
    reboot. False if it doesn't (or no longer exists), None if unknown."""
    command_line = _process_command_line(pid)
    if command_line is None:
        return None
    return os.path.normcase(os.path.abspath(watcher_path)) in os.path.normcase(command_line)


def stop_legacy_watcher(base_dir=None, watcher_path=None):
    """Stops a watcher from before the lock file, which knows nothing of
    stop requests or its Run value. It left its PID in watcher.pid; that
    PID is killed only once its command line confirms it's the watcher,
    never on the PID alone. Also called by startup_manager.py (from the
    app, so the paths are passed in). Best-effort."""
    base_dir = base_dir or _BASE_DIR
    pid_path = os.path.join(base_dir, LEGACY_PID_FILENAME)
    watcher_path = watcher_path or os.path.join(base_dir, "resolve_watcher.py")
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            first = (f.read().splitlines() or [""])[0].strip()
    except OSError:
        return
    pid = int(first) if first.isdigit() else None
    if pid and pid != os.getpid():
        confirmed = _is_watcher_process(pid, watcher_path)
        if confirmed is None:
            return  # couldn't check - keep the file and try again next time
        if confirmed:
            _log(f"Stopping an older watcher (pid={pid}) that predates the lock file.")
            _kill_pid(pid)
    try:
        os.remove(pid_path)
    except OSError:
        pass


_lock_fd = None  # kept open (and locked) until this process exits


def _try_lock():
    """True if this process now holds LOCK_PATH's lock, False if another
    live process does. Raises OSError if the file can't be opened at all."""
    global _lock_fd
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if IS_WINDOWS:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # TODO (Mac port): flock is the POSIX equivalent; untested there
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _lock_fd = fd
    return True


def _read_info():
    try:
        with open(INFO_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None, None
    return (lines[0] if lines else None), (lines[1] if len(lines) > 1 else None)


def _clear_stop_request():
    try:
        os.remove(STOP_PATH)
    except OSError:
        pass


def _acquire_single_instance_lock():
    """Best-effort. A live watcher already running the SAME code is left
    alone (more than one watcher running at once wouldn't actually corrupt
    anything - each would just separately try to launch Buddy on a
    Resolve-start transition, and Buddy's own single-instance guard makes
    any resulting duplicate launch harmless - but this avoids the wasted
    polling and log noise from that). A live watcher running OLD code,
    though, is asked to exit (STOP_PATH) and replaced once it has: a
    background process like this has no other way to pick up a code update
    on its own. "Live" is the lock, never a PID - nothing unrelated can hold
    it. If the check itself fails for any reason, err on the side of running
    anyway rather than refusing to start the whole feature over a broken
    lock-file check."""
    try:
        my_version = _own_source_hash()
        if not _try_lock():
            old_pid, old_version = _read_info()
            if old_version == my_version:
                return False
            _log(
                f"Asking the running watcher (pid={old_pid}, version={old_version}) to exit, "
                f"to replace it with the current version ({my_version})."
            )
            with open(STOP_PATH, "w", encoding="utf-8") as f:
                f.write("replace\n")
            deadline = time.time() + REPLACE_TIMEOUT_SECONDS
            while not _try_lock():
                if time.time() > deadline:
                    _log("The running watcher didn't exit - leaving it running.")
                    _clear_stop_request()
                    return False
                time.sleep(0.5)
        # Holding the lock: any stop request still here was for a watcher
        # that has already gone (or for this one's replacement, just done).
        _clear_stop_request()
        with open(INFO_PATH, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n{my_version}\n")
        stop_legacy_watcher()
        return True
    except Exception as exc:
        _log(f"Single-instance check failed ({exc!r}) - running anyway.")
        return True


def _still_registered():
    """False once startup_manager's Run value is gone (the setting was
    turned off, or Buddy was uninstalled). True when it can't tell."""
    if not IS_WINDOWS:
        return True  # TODO (Mac port): check the LaunchAgent plist instead
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _RUN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _stop_reason():
    if os.path.exists(STOP_PATH):
        return "a stop was requested"
    if not _still_registered():
        return "its startup entry was removed (setting turned off)"
    return None


def _sleep_unless_stopped(seconds):
    """Sleeps in one-second steps so a stop request is honoured promptly.
    False if one arrived."""
    end = time.time() + seconds
    while True:
        if os.path.exists(STOP_PATH):
            return False
        remaining = end - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))


def main():
    if not _acquire_single_instance_lock():
        _log("Another watcher instance is already running - exiting.")
        return

    _log(f"Watcher started (pid={os.getpid()}, interpreter={sys.executable}).")
    # A Resolve that's already open when the watcher starts (the setting
    # was just turned on from inside Buddy, or this watcher replaced an
    # older one) isn't "newly started" - Buddy is normally already running
    # then. Only a not-running -> running transition launches it.
    was_running = _is_resolve_running()
    while True:
        reason = _stop_reason()
        if reason:
            _log(f"Watcher exiting: {reason}.")
            return
        running = _is_resolve_running()
        if running and not was_running:
            _log(
                f"Resolve detected as newly started - waiting {STARTUP_GRACE_SECONDS}s for its "
                "own scripting server to finish initializing before launching Buddy."
            )
            _sleep_unless_stopped(STARTUP_GRACE_SECONDS)
            reason = _stop_reason()
            if reason:
                _log(f"Watcher exiting without launching Buddy: {reason}.")
                return
            _log("Launching Buddy.")
            _launch_buddy()
        was_running = running
        _sleep_unless_stopped(POLL_SECONDS)


if __name__ == "__main__":
    main()
