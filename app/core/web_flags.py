#!/usr/bin/env python3
"""
The Chromium switches Buddy's web views start with.

Chromium draws through ANGLE's Direct3D 11 backend on a GPU thread inside
Buddy's own process, and on some machines - two GPUs (an integrated one and
the card Resolve is busy with) is the usual story - ANGLE faults inside its
input-layout cache and takes Buddy down with it: an access violation in
Qt6WebEngineCore.dll at a quiet moment, with nothing in the crash trail
before it. Buddy's pages are forms, lists and small previews, so they're
drawn in software instead, which never touches ANGLE.

BUDDY_WEB_GPU=1 puts the GPU back (for comparing, or a machine where it's
fine). Whatever QTWEBENGINE_CHROMIUM_FLAGS already holds is kept.
"""

SOFTWARE = ("--disable-gpu", "--disable-gpu-compositing")


def chromium_flags(env):
    """QTWEBENGINE_CHROMIUM_FLAGS for this run, given the environment."""
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if env.get("BUDDY_WEB_GPU") != "1":
        flags += [f for f in SOFTWARE if f not in flags]
    return " ".join(flags)


def apply(environ):
    """Sets the switches; must run before the QApplication exists."""
    environ["QTWEBENGINE_CHROMIUM_FLAGS"] = chromium_flags(environ)
    return environ["QTWEBENGINE_CHROMIUM_FLAGS"]
