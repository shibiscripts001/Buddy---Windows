#!/usr/bin/env python3
"""
The Chromium switches Buddy's web views start with.

Chromium draws through ANGLE on a GPU thread inside Buddy's own process.
ANGLE's default backend, Direct3D 11, can fault inside its input-layout
cache on some machines - two GPUs (an integrated one and the card Resolve
is busy with) is the usual story - and take Buddy down with it: an access
violation in Qt6WebEngineCore.dll at a quiet moment, with nothing in the
crash trail before it. So ANGLE is pointed at Direct3D 11 on 12 instead,
which keeps the GPU without going through that path.

Drawing in software (--disable-gpu) avoided the crash too, but with a
dozen-odd views open inside Resolve every page was slow to draw and load.

BUDDY_WEB_SOFTWARE=1 draws in software again (a machine where the GPU
still crashes); BUDDY_WEB_GPU=1 adds nothing, leaving Chromium's own
default (for comparing). Whatever QTWEBENGINE_CHROMIUM_FLAGS already holds
is kept, and a --use-angle there wins over Buddy's.
"""

GPU = ("--use-angle=d3d11on12",)
SOFTWARE = ("--disable-gpu", "--disable-gpu-compositing")


def chromium_flags(env):
    """QTWEBENGINE_CHROMIUM_FLAGS for this run, given the environment."""
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if env.get("BUDDY_WEB_SOFTWARE") == "1":
        flags += [f for f in SOFTWARE if f not in flags]
    elif env.get("BUDDY_WEB_GPU") != "1" and not any(f.startswith("--use-angle") for f in flags):
        flags += GPU
    return " ".join(flags)


def apply(environ):
    """Sets the switches; must run before the QApplication exists."""
    environ["QTWEBENGINE_CHROMIUM_FLAGS"] = chromium_flags(environ)
    return environ["QTWEBENGINE_CHROMIUM_FLAGS"]
