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
the multisampling Chromium paints those curves with on the GPU: with it
off (--gpu-rasterization-msaa-sample-count=0) every corner comes out right.

Painting on the CPU instead (--disable-gpu-rasterization) drew the corners
right too, but slowly: a maximized tool on a 4K screen took ~450-650 ms to
appear after switching to it, against ~70-190 ms painted on the GPU
(measured from screen captures, 2026-10-03) - Chromium repaints a page
that's been out of sight from scratch. BUDDY_WEB_CPU_PAINT=1 puts that
back, for a machine whose GPU paints something else wrong.

BUDDY_WEB_SOFTWARE=1 draws in software again (a machine where the GPU
still crashes); BUDDY_WEB_GPU=1 adds nothing, leaving Chromium's own
default (for comparing). Whatever QTWEBENGINE_CHROMIUM_FLAGS already holds
is kept, and a --use-angle there wins over Buddy's - as does a
multisampling count of its own.

Every one of Buddy's own pages (all local files: one "site" to Chromium)
shares one renderer process (--process-per-site). By default each got its
own - 25 of them, ~540 MB of private memory and ~1.9 GB of working set
between them, against ~130 MB and ~250 MB shared (measured 2026-10-03).
Switching tools, the Animation tab's 60 fps and startup were unchanged;
none of Buddy's pages ran a task over 50 ms, so sharing the one thread
holds nothing up; and when the shared renderer was killed every page
reloaded within 2 s (core/web_page.py). Sites in the Web tab still get
their own process, one per site. BUDDY_WEB_PROCESS_PER_PAGE=1 goes back to
one per page.
"""

GPU = ("--use-angle=d3d11on12", "--gpu-rasterization-msaa-sample-count=0")
CPU_PAINT = "--disable-gpu-rasterization"
SOFTWARE = ("--disable-gpu", "--disable-gpu-compositing")
SHARED_RENDERER = "--process-per-site"


def chromium_flags(env):
    """QTWEBENGINE_CHROMIUM_FLAGS for this run, given the environment."""
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if env.get("BUDDY_WEB_GPU") == "1":
        return " ".join(flags)
    if env.get("BUDDY_WEB_PROCESS_PER_PAGE") != "1" and SHARED_RENDERER not in flags:
        flags.append(SHARED_RENDERER)
    if env.get("BUDDY_WEB_SOFTWARE") == "1":
        flags += [f for f in SOFTWARE if f not in flags]
    elif not any(f.startswith("--use-angle") for f in flags):
        given = {f.split("=")[0] for f in flags}
        flags += [f for f in GPU if f.split("=")[0] not in given]
        if env.get("BUDDY_WEB_CPU_PAINT") == "1" and CPU_PAINT not in flags:
            flags.append(CPU_PAINT)
    return " ".join(flags)


def apply(environ):
    """Sets the switches; must run before the QApplication exists."""
    environ["QTWEBENGINE_CHROMIUM_FLAGS"] = chromium_flags(environ)
    return environ["QTWEBENGINE_CHROMIUM_FLAGS"]
