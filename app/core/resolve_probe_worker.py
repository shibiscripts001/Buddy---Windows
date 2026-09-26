#!/usr/bin/env python3
"""
Standalone worker invoked as a subprocess by resolve_bridge.py to check
whether DaVinci Resolve's scripting API is reachable right now.

This has to run out-of-process: connecting to Resolve's scripting DLL
(fusionscript.dll) when Resolve isn't running has been observed (see
Davinci Time Tracker's own resolve_poll_worker.py, which this is modeled
on) to hard crash the interpreter - a native segfault, not a catchable
Python exception - which would otherwise take the whole shell down with it.

Every standalone tool that connects in-process gets away with skipping this
probe because each is a one-shot launch from Resolve's own Scripts menu,
which guarantees Resolve is already running. The Buddy shell has no such
guarantee - it's a persistent app a person can open before Resolve, or
after Resolve has been closed - so it probes in a disposable child process
first and only imports the scripting module in-process (in
ResolveController, for real work) once this probe has confirmed it's safe.

Exits 0 if Resolve is reachable, 1 otherwise. Prints nothing either way -
callers only care about the exit code.
"""

import os
import sys


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


def main():
    try:
        dvr_script = _bootstrap_resolve_env()
        resolve = dvr_script.scriptapp("Resolve")
        sys.exit(0 if resolve is not None else 1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
