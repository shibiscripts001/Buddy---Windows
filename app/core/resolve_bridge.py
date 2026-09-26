#!/usr/bin/env python3
"""
Shared DaVinci Resolve connection for the Buddy shell.

connect() below is the ONE place any tool page goes through to get a live
ResolveController - it always probes reachability in a disposable
subprocess first (see resolve_probe_worker.py for why that has to happen
out-of-process), and only imports DaVinciResolveScript in-process,
here, once that probe has confirmed Resolve is actually up. From that
point on, per-action calls (rename a clip, import media, read the current
timeline) go straight through this in-process controller - the fast path
every standalone tool already used - since a crash there was never the
observed failure mode; it was only ever the FIRST connection attempt made
before Resolve was confirmed running.

Every tool-specific need (get_current_clips, import_to_media_pool, etc.)
belongs on a subclass or a helper module next to that tool's page, not
here - this module only owns the connect/bootstrap plumbing every tool
was otherwise copy-pasting.
"""

import os
import subprocess
import sys

_PROBE_SCRIPT = os.path.join(os.path.dirname(__file__), "resolve_probe_worker.py")
_PROBE_TIMEOUT_SECONDS = 8


class ResolveConnectionError(RuntimeError):
    pass


def probe_resolve_reachable():
    """Runs the reachability check in a child process. Returns False (never
    raises) on any failure, including the probe itself timing out or the
    child process crashing outright - all of those just mean "not
    reachable right now", not an error the caller needs to handle
    specially."""
    try:
        result = subprocess.run(
            [sys.executable, _PROBE_SCRIPT],
            timeout=_PROBE_TIMEOUT_SECONDS,
            capture_output=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def _bootstrap_resolve_env():
    if sys.platform == "darwin":
        if "RESOLVE_SCRIPT_API" not in os.environ:
            os.environ["RESOLVE_SCRIPT_API"] = (
                "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
            )
        if "RESOLVE_SCRIPT_LIB" not in os.environ:
            os.environ["RESOLVE_SCRIPT_LIB"] = (
                "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
            )
    else:
        if "RESOLVE_SCRIPT_API" not in os.environ:
            os.environ["RESOLVE_SCRIPT_API"] = os.path.expandvars(
                r"%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
            )
        if "RESOLVE_SCRIPT_LIB" not in os.environ:
            os.environ["RESOLVE_SCRIPT_LIB"] = (
                r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
            )

    modules_path = os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules")
    if modules_path not in sys.path:
        sys.path.append(modules_path)

    import DaVinciResolveScript as dvr_script  # noqa: E402
    return dvr_script


class ResolveController:
    """In-process connection - only ever constructed after
    probe_resolve_reachable() has returned True (see connect() below)."""

    def __init__(self):
        self.dvr_script = _bootstrap_resolve_env()
        self.resolve = self.dvr_script.scriptapp("Resolve")
        if self.resolve is None:
            raise ResolveConnectionError(
                "Could not connect to DaVinci Resolve.\n"
                "Make sure Resolve is running with a project open."
            )
        self.project_manager = self.resolve.GetProjectManager()

    def current_project(self):
        return self.project_manager.GetCurrentProject()


def connect():
    """Returns a live ResolveController, or raises ResolveConnectionError.

    Always safe to call even when Resolve isn't running or hasn't started
    yet - the probe subprocess absorbs that crash risk so this function
    itself never takes the caller's process down with it."""
    if not probe_resolve_reachable():
        raise ResolveConnectionError(
            "Could not reach DaVinci Resolve.\n"
            "Make sure Resolve is running with a project open."
        )
    return ResolveController()
