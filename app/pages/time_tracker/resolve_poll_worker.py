#!/usr/bin/env python3
"""
Standalone worker invoked as a subprocess by resolve_bridge.py to fetch the
current Resolve project name.

This has to run out-of-process: connecting to Resolve's scripting DLL
(fusionscript.dll) when Resolve isn't reachable has been observed to hard
crash the interpreter (a native segfault) rather than raise a catchable
Python exception, which would otherwise take the whole Buddy shell down
with it - not just this one tab. Running it in a short-lived child process
means a crash here only ever kills this worker - stdout simply comes back
empty and the parent treats that poll as "no project detected". This is
deliberately kept separate from core/resolve_bridge.py's own probe-then-
connect model: that module hands pages a single persistent, already-
connected controller for one-shot actions, while this one is polled every
few seconds forever for as long as Buddy is open, and isolating each poll
in its own disposable process is what makes that safe.

Prints the project name to stdout (nothing if Resolve/no project) and exits
0 on success, 1 on any handled failure.
"""

import os
import sys


def _bootstrap_resolve_env():
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
        if resolve is None:
            sys.exit(1)
        project_manager = resolve.GetProjectManager()
        if project_manager is None:
            sys.exit(1)
        project = project_manager.GetCurrentProject()
        if project is None:
            sys.exit(1)
        name = project.GetName()
        if not name:
            sys.exit(1)

        # Resolve keeps SOME project "current" (a default "Untitled
        # Project") even while sitting on the Project Manager/home screen,
        # before the user has actually opened the project they mean to work
        # in - GetCurrentProject() alone can't tell that apart from
        # genuinely being inside a project, so time was getting tracked
        # against "Untitled Project" while just sitting at the picker. This
        # name-based check is a cheap, good-enough heuristic: it costs no
        # extra call into Resolve's scripting API, at the acceptable cost
        # of also skipping tracking for anyone who's deliberately kept
        # their own real project literally named "Untitled Project".
        if name == "Untitled Project":
            sys.exit(1)

        sys.stdout.write(name)
        sys.exit(0)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
