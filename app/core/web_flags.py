#!/usr/bin/env python3
"""
The Chromium switches Buddy's web views start with.

Chromium draws through ANGLE on a GPU thread inside Buddy's own process.
ANGLE's default backend, Direct3D 11, crashes Buddy within a minute or two
of ordinary use, on any card: on Chromium's GPU thread, a write through a
freed pointer while relinking a list in ANGLE's cache of Direct3D state
objects (Qt6WebEngineCore.dll+0xd020d0 in Qt 6.11.2). The more varied the
pages' drawing, the sooner - Buddy's themed pages brought it in ~35 s of
switching tools. Direct3D 11 on 12 never does: 550+ switches without a
fault where Direct3D 11 managed ~75. ANGLE's OpenGL and Vulkan backends
lost their context over and over.

11 on 12 has one weakness of its own: on the card's driver. Dragging Buddy
onto a monitor plugged into another GPU, or quitting Resolve, reset the
NVIDIA driver (nvlddmkm event 153) and took Buddy with it - so on a PC with
two GPUs, Buddy draws on the low-power one (core/gpu_adapter.py), where it
survived all of that.

Drawing in software (--disable-gpu) never crashed, but managed 7 frames a
second on a busy page where the GPU does 60.

11 on 12 on AMD's integrated graphics (the low-power adapter above) draws
some rounded corners wrong: a box's bottom-left curve and border go
missing and a long thin sliver runs from the corner across the box - on
Buddy's own cards and fields (Nova shows it most) and on sites' pages
alike, and redrawn with every frame of whatever animates over it. It's
Chromium painting the page on the GPU (GPU rasterization): with the
painting on the CPU and only the compositing on the GPU
(--disable-gpu-rasterization), every corner came out right, still at 60
frames a second on the Animation tab. Plain Direct3D 11 and the NVIDIA
card drew them right too, but crash (above). So that goes with 11 on 12.

BUDDY_WEB_SOFTWARE=1 draws in software again (a machine where the GPU
still crashes); BUDDY_WEB_GPU=1 adds nothing, leaving Chromium's own
default (for comparing). Whatever QTWEBENGINE_CHROMIUM_FLAGS already holds
is kept, and a --use-angle there wins over Buddy's - as does
--enable-gpu-rasterization over its painting on the CPU.
"""

GPU = ("--use-angle=d3d11on12", "--disable-gpu-rasterization")
SOFTWARE = ("--disable-gpu", "--disable-gpu-compositing")


def chromium_flags(env):
    """QTWEBENGINE_CHROMIUM_FLAGS for this run, given the environment."""
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if env.get("BUDDY_WEB_SOFTWARE") == "1":
        flags += [f for f in SOFTWARE if f not in flags]
    elif env.get("BUDDY_WEB_GPU") != "1" and not any(f.startswith("--use-angle") for f in flags):
        flags += [f for f in GPU if not (f == "--disable-gpu-rasterization" and "--enable-gpu-rasterization" in flags)]
    return " ".join(flags)


def apply(environ):
    """Sets the switches; must run before the QApplication exists."""
    environ["QTWEBENGINE_CHROMIUM_FLAGS"] = chromium_flags(environ)
    return environ["QTWEBENGINE_CHROMIUM_FLAGS"]
