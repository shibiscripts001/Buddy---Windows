#!/usr/bin/env python3
r"""
SVG to Fusion engine
================================
Connects to a *running* DaVinci Resolve instance, parses an SVG (or a
Lottie/Bodymovin JSON animation) in pure Python, and copies Fusion's own
node-graph paste-text to the clipboard for pasting straight into DaVinci
Resolve's Fusion page. Driven by the SVG Importer page (page.py), from a
normal desktop Python (NOT from inside Fusion). The import below is a
pure Python geometry/text generator, not UI automation.


HOW SVG IMPORT WORKS (bespoke, not the native Fusion importer)
------------------------------------------------------------------
Fusion's own vector import (Fusion > Import > SVG...) has no scripting
hook -- confirmed by probing fusion.ActionManager.GetActions() both from
outside Fusion and from inside it via RunScript(): almost all 1104
registered actions came back as empty placeholders over the bridge, and
pushing on that further twice destabilized Resolve's scripting connection
badly enough to crash it. A follow-up attempt to drive the real Fusion >
Import > SVG... menu via UI automation (pywinauto) also proved too
unreliable -- Resolve's themed Qt menu bar doesn't respond to a plain
simulated click at the coordinates UI Automation reports.

So this parses the SVG itself (paths, rects, circles, ellipses, polygons,
transforms, arcs/beziers, fill colors) in plain Python and generates
Fusion's own paste-text format -- the same Lua-table syntax you'd get from
copying nodes in Fusion and pasting as plain text -- then copies it to the
OS clipboard for you to paste yourself (Ctrl+V into Fusion's Flow view).

Calling comp.Paste() from inside a RunScript worker (the same "run inside
Fusion's own interpreter" trick used by the "Dump Selected Node Settings"
diagnostic below) only works for small graphs (a handful of tools) -- for
anything larger (e.g. a 44-tool graph) it silently creates nothing.
Manual Ctrl+V has no such limit, so that's the one path. Every
shape becomes a bezier PolylineMask + a paired Background fill, wired
with Merges, wrapped in Groups mirroring the SVG's own <g> nesting.

Calibrated against real native-import dumps ("Dump Selected Node Settings")
for several confirmed details: coordinates are converted with X normalized
by the comp's own width and Y by its own height, independently (Fusion's
mask coordinates don't do any aspect-ratio correction on their own, so a
non-square frame like 1920x1080 needs this to avoid distorting shapes).
Native import draws at 1:1 pixel scale and does NOT scale to fit -- measured,
not assumed: a native dump and one of ours both come out at an implied pixel
scale of exactly 1.0000 on both axes, and in a comp matching the SVG's own
size our output reproduces native's normalized point span to four decimals
(0.9824 x 0.9852). The Scale option's "native" mode is therefore what native
import does; "timeline" is our own addition for scaling to fit the frame
(see compute_svg_scale). MaskWidth/MaskHeight are always the tool's own
320x240 default regardless of comp size (confirmed unrelated to point
normalization). A compound path becomes one PolylineMask per subpath,
chained -- each one after the first with PaintMode=Invert and its
EffectMask fed from the previous mask -- which computes an even-odd fill
and so needs no outer-vs-hole analysis at all (see build_shape_lua). NOT
Fusion's Polyline2 input (present-but-disabled on every mask regardless of
shape, so its existence means nothing). Every mask also
carries JoinStyle=2/MiterLimit=4/CapStyle=0 and DrawMode="ModifyOnly" to
match native. And the whole graph sits on top of one fully transparent
(TopLeftAlpha=0), comp-sized Background via a final Merge, matching
native's own structure -- on a test logo this generator
emits exactly native's own tool counts (21 masks, 16 of them inverted, 6
Backgrounds, 5 Merges). Still unverified: exact color management/gamma
handling -- use "Dump Selected Node Settings" to check that against a fresh
native import if colors still look off.


HOW THE DIAGNOSTICS DUMP WORKS
--------------------------
Resolve's *external* scripting connection (the one this app normally talks
over) isn't reliable for live node-graph reads -- three independent
failures show it: Paste() silently no-op'ing, tool
objects coming back as unhashable PyRemoteObject proxies, and
flow.GetPos(tool) returning a bare float (e.g. 2.0) instead of an (x, y)
pair. All three point at the same place: the bridge mangles Lua's
multi-return values.

So instead of reading tool settings directly from here, clicking "Dump
Selected Node Settings" writes a small worker script to a temp file and
asks Fusion to run it via fusion.RunScript(...) -- which executes inside
Fusion's own embedded interpreter, the same context Workspace > Scripts
uses, where these calls are confirmed to behave correctly. The worker reads
the current selection's settings and writes its result to a small JSON
file; this app polls for that file and reports what happened in the Log
panel below.

BEFORE YOU RUN THIS
--------------------
1. DaVinci Resolve must be running, with the Fusion page open (or a Fusion
   clip loaded) so there's an active composition.
2. In Resolve: Preferences > System > General > "External scripting using"
   must be set to "Local" (not "None").
3. If using the diagnostics dump, select the tools to inspect in Fusion's
   Flow view (Ctrl+A works well inside a group) before clicking the button.
"""

import os
import sys
import json
import re
import collections
import time
import math
import colorsys
import uuid
import ctypes
import platform
import tempfile
import threading
import traceback
import xml.etree.ElementTree as ET

# ==========================================================================
# Theme & window styling helpers (same as the Color Palette Manager)
# NOTE: these are dead code now that the GUI lives in main.py (PySide6, its
# own theme.py) - kept here untouched rather than removed, since rgb_to_hex
# below IS a real dependency of the core engine (ColorConsolidationRegistry),
# and splitting that one function out from its neighbors here wasn't worth
# the extra edit surface on an otherwise byte-for-byte-preserved file.
# ==========================================================================
DEFAULT_ACCENT_COLOR = "#2c456b"
DEFAULT_BACKGROUND_COLOR = "#1e1e1e"
FONT_NORMAL = ("Segoe UI", 12)
FONT_BOLD = ("Segoe UI", 12, "bold")
FONT_MONO = ("Consolas", 11)
# Header title, matching ColorPaletteManager_modern_W.py's own FONT_TITLE so
# the two apps' headers read identically.
FONT_TITLE = ("Segoe UI", 16, "bold")


def hex_to_rgb_tuple(hex_color):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join([c * 2 for c in hex_color])
    if len(hex_color) != 6:
        return (255, 255, 255)
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def relative_luminance(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def contrasting_text_color(hex_color):
    return "#1c1c1c" if relative_luminance(hex_color) > 0.55 else "#f0f0f0"


def adjust_brightness(hex_color, amount):
    r, g, b = hex_to_rgb_tuple(hex_color)
    return rgb_to_hex((r + amount, g + amount, b + amount))


def _hex_to_colorref(hex_color):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return 0
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return r | (g << 8) | (b << 16)


def apply_windows_title_bar_color(root, hex_color):
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwmapi = ctypes.windll.dwmapi
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_CAPTION_COLOR = 35
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWA_SYSTEMBACKDROP_TYPE = 38
        dark_mode = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark_mode), ctypes.sizeof(dark_mode))
        colorref = ctypes.c_int(_hex_to_colorref(hex_color))
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(colorref), ctypes.sizeof(colorref))
        corner_pref = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner_pref), ctypes.sizeof(corner_pref))
        backdrop_type = ctypes.c_int(3)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(backdrop_type), ctypes.sizeof(backdrop_type))
    except Exception:
        pass


# ==========================================================================
# Diagnostics worker: dumps SaveSettings() for whatever's currently selected
# to a JSON file. Useful for comparing our generated node graph against a
# real native "Fusion > Import > SVG..." result on the same test file --
# the exact tool-type names, field names and coordinate scale Resolve's own
# importer uses, read straight from Fusion rather than guessed.
# ==========================================================================
_DUMP_WORKER_SOURCE = r'''
import json
import traceback

RESULT_PATH = __RESULT_PATH__


def _find_comp():
    g = globals()
    fu = g.get("fusion")
    if fu is not None:
        try:
            c = fu.GetCurrentComp()
            if c is not None:
                return c
        except Exception:
            pass
    c = g.get("comp")
    if c is not None:
        return c
    ap = g.get("app")
    if ap is not None:
        try:
            c = ap.GetCurrentComp()
            if c is not None:
                return c
        except Exception:
            pass
    return None


result = {"ok": False, "message": ""}
try:
    c = _find_comp()
    if c is None:
        raise RuntimeError("Couldn't find an active composition.")
    tools_dict = c.GetToolList(True)
    tools = list(tools_dict.values()) if tools_dict else []
    if not tools:
        result["message"] = "No tools are currently selected in Fusion."
    else:
        dumped = {}
        for t in tools:
            try:
                name = t.GetAttrs()["TOOLS_Name"]
            except Exception:
                name = str(t)
            try:
                dumped[name] = t.SaveSettings()
            except Exception:
                dumped[name] = {"_error": traceback.format_exc()}
        result["ok"] = True
        result["message"] = f"Dumped settings for {len(dumped)} tool(s)."
        result["tools"] = dumped
except Exception:
    result["message"] = "Dump failed inside Fusion:\n" + traceback.format_exc()

with open(RESULT_PATH, "w") as f:
    json.dump(result, f, indent=2, default=str)
'''


def build_dump_worker_script(result_path):
    script_path = os.path.join(tempfile.gettempdir(), f"fusion_dump_worker_{uuid.uuid4().hex}.py")
    source = _DUMP_WORKER_SOURCE.replace("__RESULT_PATH__", repr(result_path))
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(source)
    return script_path


# ==========================================================================
# DaVinci Resolve external-scripting connection
# ==========================================================================
def _candidate_module_dirs():
    system = platform.system()
    if system == "Windows":
        programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return [os.path.join(programdata, "Blackmagic Design", "DaVinci Resolve",
                              "Support", "Developer", "Scripting", "Modules")]
    if system == "Darwin":
        return ["/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
                 "Developer/Scripting/Modules"]
    return ["/opt/resolve/Developer/Scripting/Modules",
            "/home/resolve/Developer/Scripting/Modules"]


def connect_to_resolve(log=None):
    """Returns (resolve, fusion, comp, comp_w, comp_h, comp_fps, error_message).

    comp_fps: the ACTIVE comp's own project frame rate, read from the same
    GetPrefs() table comp_w/comp_h already come from (Comp.FrameFormat.Rate,
    sibling of the already-confirmed-working Width/Height keys) -- needed so
    a Lottie import can rescale the source file's own frame numbers (see
    build_lottie_tools' time_scale) onto however many frames-per-second THIS
    comp actually runs at, rather than assuming it matches the Lottie file's
    own "fr". Falls back to None (see build_lottie_tools: None means "assume
    same fps as the source file", i.e. no rescaling) on any failure, same
    spirit as comp_w/comp_h's own 1920x1080 fallback.

    log: UNCONFIRMED key -- "Rate" is a guess-by-analogy to the sibling
    Width/Height keys, not yet verified against a real GetPrefs() dump (see
    this file's own rule: verify unfamiliar Fusion constructs before
    trusting them). If the Rate lookup fails, logs the real FrameFormat
    table's own key names (when log is given) so the actual field name can
    be read from the log instead of guessed again."""
    try:
        import DaVinciResolveScript as dvr
    except ImportError:
        for d in _candidate_module_dirs():
            if os.path.isdir(d) and d not in sys.path:
                sys.path.append(d)
        try:
            import DaVinciResolveScript as dvr
        except ImportError as e:
            return None, None, None, None, None, None, (
                "Couldn't find DaVinci Resolve's scripting module.\n\n"
                "Check that DaVinci Resolve is installed, and that Preferences > "
                "System > General > \"External scripting using\" is set to "
                "\"Local\" (not \"None\").\n\nDetails: " + str(e)
            )
    try:
        resolve = dvr.scriptapp("Resolve")
    except Exception as e:
        return None, None, None, None, None, None, f"Found the scripting module but couldn't call it: {e}"

    if resolve is None:
        return None, None, None, None, None, None, (
            "Connected to the scripting module, but Resolve didn't respond.\n\n"
            "Make sure DaVinci Resolve is running, and that Preferences > System > "
            "General > \"External scripting using\" is set to \"Local\"."
        )

    try:
        fusion = resolve.Fusion()
        comp = fusion.GetCurrentComp() if fusion else None
    except Exception as e:
        return resolve, None, None, None, None, None, f"Connected to Resolve, but couldn't reach Fusion: {e}"

    if comp is None:
        return resolve, fusion, None, None, None, None, (
            "Connected to Fusion, but no composition is open.\n\n"
            "Open the Fusion page (or double-click a Fusion clip) with a "
            "composition loaded, then try again."
        )

    comp_w, comp_h = 1920, 1080
    comp_fps = None
    prefs = None
    try:
        prefs = comp.GetPrefs()
        comp_w = int(prefs["Comp"]["FrameFormat"]["Width"])
        comp_h = int(prefs["Comp"]["FrameFormat"]["Height"])
    except Exception:
        pass  # fall back to the 1920x1080 default above
    try:
        comp_fps = float(prefs["Comp"]["FrameFormat"]["Rate"])
    except Exception as e:
        if log is not None:
            try:
                frame_format_keys = list(prefs["Comp"]["FrameFormat"].keys())
            except Exception:
                frame_format_keys = None
            log(f"[DEBUG] Couldn't read comp frame rate via Comp.FrameFormat.Rate ({e}). "
                f"Comp.FrameFormat's own keys: {frame_format_keys}")

    return resolve, fusion, comp, comp_w, comp_h, comp_fps, None


# ==========================================================================
# SVG -> Fusion geometry engine
# --------------------------------------------------------------------------
# The external comp.Paste()
# (even from inside a RunScript worker) is unreliable for
# anything but tiny graphs, so run_import() below copies the generated
# paste-text to the OS clipboard for a manual Ctrl+V instead (see module
# docstring).
#
# Every shape becomes a real bezier PolylineMask + a paired Background
# (fill), wired together with Merges, wrapped in Groups that mirror the
# SVG's own <g> nesting and names.
#
# KNOWN LIMITATIONS (see "Dump Selected Node Settings" below for how to
# check anything here against a real native import):
# - Gradient fills/strokes (linearGradient/radialGradient, including
#   xlink:href stop inheritance and gradientTransform) are parsed into
#   Fusion's own native Gradient Background fields (Type/GradientType/
#   Start/End/Gradient.Colors) -- see resolve_gradient and
#   _build_gradient_background_lua. Two things still aren't representable
#   and fall back to that same flat mid-grey placeholder instead: a
#   gradient that isn't purely linear/radial (SVG's fallback color on an
#   otherwise-unsupported paint), and a radialGradient with no stops of its
#   own AND no resolvable href. A radialGradient's own fx/fy focal offset
#   and any elliptical (non-uniform-radius) shape ARE parsed but silently
#   dropped -- Fusion's own radial gradient has no equivalent (confirmed
#   against a real native dump: only Start/End, i.e. center + one point on
#   the rim, no separate focal or ellipse fields). objectBoundingBox units
#   use an approximate bbox (path anchors + bezier handles, not the curves'
#   true extrema). This is the only place a Gradient-typed Input is
#   written into paste-text -- unlike everything above it, which is
#   checked against a real "Dump Selected Node Settings" dump, the exact
#   Lua constructor syntax here is UNVERIFIED -- paste and dump-compare before trusting its output.
# - fill-rule is ignored: a compound path always fills even-odd, since
#   that's what the chained-Invert mask structure computes (see
#   build_shape_lua). Native import appears to ignore it the same way, and
#   the two rules agree unless a path deliberately overlaps subpaths that
#   wind the SAME direction.
# - Strokes are solid-colour only -- stroke-dasharray isn't read, and
#   stroke-linecap/stroke-linejoin always come out as native import's own
#   fixed butt/miter (see build_mask_tool_lua) rather than whatever the SVG
#   actually specifies, since Fusion's own numeric IDs for the round/bevel
#   variants aren't confirmed against a real native dump.
# - A compound path's stroke unions its subpaths' outlines by chaining their
#   border masks with Fusion's own DEFAULT PolylineMask paint mode (i.e.
#   deliberately NOT the PaintMode=Invert the fill chain uses) -- confirmed
#   correct for a single subpath, but not verified against a real native
#   dump for a multi-subpath stroked compound path specifically.
# - stroke-width scales with a path's own transform (translate/scale/rotate),
#   approximated via the transform matrix's determinant for skew/non-uniform
#   scale, where even a real renderer would vary the stroke's thickness
#   along the path.
# ==========================================================================
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def mat_point(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def mat_vector(m, dx, dy):
    """Transforms a pure OFFSET (no translation component) -- e.g. an SVG
    feOffset's (dx, dy) -- through m's linear part only, unlike mat_point's
    full affine map. Used for drop-shadow offsets (see
    parse_drop_shadow_filter_defs), which are a displacement, not an
    absolute position."""
    a, b, c, d, _, _ = m
    return (a * dx + c * dy, b * dx + d * dy)


_TRANSFORM_RE = re.compile(r'(\w+)\s*\(([^)]*)\)')
_NUM_RE = re.compile(r'-?\d*\.?\d+(?:[eE][+-]?\d+)?')


def parse_transform(s):
    if not s:
        return IDENTITY
    m = IDENTITY
    for name, args in _TRANSFORM_RE.findall(s):
        nums = [float(v) for v in _NUM_RE.findall(args)]
        if name == 'translate':
            tx = nums[0] if nums else 0.0
            ty = nums[1] if len(nums) > 1 else 0.0
            t = (1, 0, 0, 1, tx, ty)
        elif name == 'scale':
            sx = nums[0] if nums else 1.0
            sy = nums[1] if len(nums) > 1 else sx
            t = (sx, 0, 0, sy, 0, 0)
        elif name == 'rotate':
            ang = math.radians(nums[0]) if nums else 0.0
            ca, sa = math.cos(ang), math.sin(ang)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                t = mat_mul(mat_mul((1, 0, 0, 1, cx, cy), (ca, sa, -sa, ca, 0, 0)),
                            (1, 0, 0, 1, -cx, -cy))
            else:
                t = (ca, sa, -sa, ca, 0, 0)
        elif name == 'skewX':
            t = (1, 0, math.tan(math.radians(nums[0])), 1, 0, 0)
        elif name == 'skewY':
            t = (1, math.tan(math.radians(nums[0])), 0, 1, 0, 0)
        elif name == 'matrix' and len(nums) >= 6:
            t = tuple(nums[:6])
        else:
            continue
        m = mat_mul(m, t)
    return m


class Subpath:
    __slots__ = ("anchors", "closed")

    def __init__(self):
        self.anchors = []
        self.closed = False

    def add(self, x, y, in_cp=None, out_cp=None):
        self.anchors.append({"x": x, "y": y, "in_cp": in_cp, "out_cp": out_cp})

    def set_out_cp(self, index, cp):
        if 0 <= index < len(self.anchors):
            self.anchors[index]["out_cp"] = cp

    def set_in_cp(self, index, cp):
        if 0 <= index < len(self.anchors):
            self.anchors[index]["in_cp"] = cp


def _arc_to_beziers(x1, y1, rx, ry, phi_deg, large_arc, sweep, x2, y2):
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [((x1, y1), (x2, y2), (x2, y2))]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg % 360.0)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    co = math.sqrt(max(num, 0.0) / den) if den else 0.0
    if large_arc == sweep:
        co = -co
    cxp = co * (rx * y1p) / ry
    cyp = -co * (ry * x1p) / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2.0

    def ang(ux, uy, vx, vy):
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / d))) if d else 0.0
        return a if (ux * vy - uy * vx) >= 0 else -a

    theta1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    n_segs = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2))))
    seg_theta = dtheta / n_segs
    alpha = math.sin(seg_theta) * (math.sqrt(4 + 3 * math.tan(seg_theta / 2) ** 2) - 1) / 3.0
    segs = []
    t0 = theta1
    for _ in range(n_segs):
        t1 = t0 + seg_theta
        cos_t0, sin_t0 = math.cos(t0), math.sin(t0)
        cos_t1, sin_t1 = math.cos(t1), math.sin(t1)
        e0x = cx + rx * cos_p * cos_t0 - ry * sin_p * sin_t0
        e0y = cy + rx * sin_p * cos_t0 + ry * cos_p * sin_t0
        e1x = cx + rx * cos_p * cos_t1 - ry * sin_p * sin_t1
        e1y = cy + rx * sin_p * cos_t1 + ry * cos_p * sin_t1
        d0x = -rx * cos_p * sin_t0 - ry * sin_p * cos_t0
        d0y = -rx * sin_p * sin_t0 + ry * cos_p * cos_t0
        d1x = -rx * cos_p * sin_t1 - ry * sin_p * cos_t1
        d1y = -rx * sin_p * sin_t1 + ry * cos_p * cos_t1
        c1 = (e0x + alpha * d0x, e0y + alpha * d0y)
        c2 = (e1x - alpha * d1x, e1y - alpha * d1y)
        segs.append((c1, c2, (e1x, e1y)))
        t0 = t1
    return segs


# Path-data scanners, each matched AT a position (skipping the whitespace/
# comma separators in front of it) rather than tokenizing the whole string up
# front -- an arc's large-arc/sweep flags are single characters that minified
# output (SVGO) packs straight against the next number ("a5 5 0 1010 0" is
# flags 1, 0 then x=10), which no context-free number regex can split.
_PATH_CMD_RE = re.compile(r'[\s,]*([MLHVCSQTAZmlhvcsqtaz])')
_PATH_NUM_RE = re.compile(r'[\s,]*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)')
_PATH_FLAG_RE = re.compile(r'[\s,]*([01])')
_PATH_END_RE = re.compile(r'[\s,]*$')


class _PathDataError(Exception):
    """Malformed path data -- per the SVG spec's error handling, everything
    up to the bad spot still renders and the rest of that d= is dropped."""


def parse_path_d(d):
    pos = 0

    def nextnum():
        nonlocal pos
        mt = _PATH_NUM_RE.match(d, pos)
        if not mt:
            raise _PathDataError
        pos = mt.end()
        return float(mt.group(1))

    def nextflag():
        nonlocal pos
        mt = _PATH_FLAG_RE.match(d, pos)
        if not mt:
            raise _PathDataError
        pos = mt.end()
        return int(mt.group(1))

    subpaths = []
    cur = None
    cx = cy = 0.0
    start_x = start_y = 0.0
    last_cubic_cp = None
    last_quad_cp = None
    cmd = None

    while True:
        mt = _PATH_CMD_RE.match(d, pos)
        if mt:
            cmd = mt.group(1); pos = mt.end()
        elif _PATH_END_RE.match(d, pos):
            break
        elif cmd is None or cmd in 'Zz':
            # Numbers with no command in front of them, or straight after a
            # Z (which takes none) -- a spec error, not an implicit repeat.
            # Stopping here is also what guarantees every pass consumes
            # something: otherwise "Z2 2" would loop forever.
            break
        is_rel = cmd.islower()
        C = cmd.upper()
        if C != 'M' and cur is None:
            break  # path data has to open with a moveto (spec error otherwise)
        try:
            if C == 'M':
                x, y = nextnum(), nextnum()
                if is_rel and cur is not None:
                    x, y = cx + x, cy + y
                cur = Subpath(); subpaths.append(cur)
                cur.add(x, y)
                cx, cy = x, y
                start_x, start_y = x, y
                cmd = 'l' if is_rel else 'L'
                last_cubic_cp = last_quad_cp = None
            elif C == 'L':
                x, y = nextnum(), nextnum()
                if is_rel:
                    x, y = cx + x, cy + y
                cur.add(x, y); cx, cy = x, y
                last_cubic_cp = last_quad_cp = None
            elif C == 'H':
                x = nextnum(); x = cx + x if is_rel else x
                cur.add(x, cy); cx = x
                last_cubic_cp = last_quad_cp = None
            elif C == 'V':
                y = nextnum(); y = cy + y if is_rel else y
                cur.add(cx, y); cy = y
                last_cubic_cp = last_quad_cp = None
            elif C == 'C':
                x1, y1, x2, y2, x, y = (nextnum() for _ in range(6))
                if is_rel:
                    x1, y1 = cx + x1, cy + y1
                    x2, y2 = cx + x2, cy + y2
                    x, y = cx + x, cy + y
                cur.set_out_cp(len(cur.anchors) - 1, (x1, y1))
                cur.add(x, y, in_cp=(x2, y2))
                cx, cy = x, y
                last_cubic_cp = (x2, y2); last_quad_cp = None
            elif C == 'S':
                x2, y2, x, y = (nextnum() for _ in range(4))
                if is_rel:
                    x2, y2 = cx + x2, cy + y2
                    x, y = cx + x, cy + y
                if last_cubic_cp is not None:
                    x1, y1 = 2 * cx - last_cubic_cp[0], 2 * cy - last_cubic_cp[1]
                else:
                    x1, y1 = cx, cy
                cur.set_out_cp(len(cur.anchors) - 1, (x1, y1))
                cur.add(x, y, in_cp=(x2, y2))
                cx, cy = x, y
                last_cubic_cp = (x2, y2); last_quad_cp = None
            elif C == 'Q':
                qx, qy, x, y = (nextnum() for _ in range(4))
                if is_rel:
                    qx, qy = cx + qx, cy + qy
                    x, y = cx + x, cy + y
                c1 = (cx + 2.0 / 3.0 * (qx - cx), cy + 2.0 / 3.0 * (qy - cy))
                c2 = (x + 2.0 / 3.0 * (qx - x), y + 2.0 / 3.0 * (qy - y))
                cur.set_out_cp(len(cur.anchors) - 1, c1)
                cur.add(x, y, in_cp=c2)
                cx, cy = x, y
                last_quad_cp = (qx, qy); last_cubic_cp = None
            elif C == 'T':
                x, y = nextnum(), nextnum()
                if is_rel:
                    x, y = cx + x, cy + y
                if last_quad_cp is not None:
                    qx, qy = 2 * cx - last_quad_cp[0], 2 * cy - last_quad_cp[1]
                else:
                    qx, qy = cx, cy
                c1 = (cx + 2.0 / 3.0 * (qx - cx), cy + 2.0 / 3.0 * (qy - cy))
                c2 = (x + 2.0 / 3.0 * (qx - x), y + 2.0 / 3.0 * (qy - y))
                cur.set_out_cp(len(cur.anchors) - 1, c1)
                cur.add(x, y, in_cp=c2)
                cx, cy = x, y
                last_quad_cp = (qx, qy); last_cubic_cp = None
            elif C == 'A':
                rx, ry, phi = nextnum(), nextnum(), nextnum()
                large_arc, sweep = nextflag(), nextflag()
                x, y = nextnum(), nextnum()
                if is_rel:
                    x, y = cx + x, cy + y
                for c1, c2, end in _arc_to_beziers(cx, cy, rx, ry, phi, large_arc, sweep, x, y):
                    cur.set_out_cp(len(cur.anchors) - 1, c1)
                    cur.add(end[0], end[1], in_cp=c2)
                cx, cy = x, y
                last_cubic_cp = last_quad_cp = None
            elif C == 'Z':
                cur.closed = True
                cx, cy = start_x, start_y
                last_cubic_cp = last_quad_cp = None
        except _PathDataError:
            break  # ran out of numbers mid-command -- keep what parsed so far
    for sp in subpaths:
        _fold_closing_anchor(sp)
    return subpaths


def _fold_closing_anchor(sp, eps=1e-9):
    """Drops a subpath's final anchor when it merely repeats the first one,
    folding its incoming tangent onto the first anchor instead.

    Many exporters close a subpath with an explicit line/curve back to the
    start point before Z ("... L<startX> <startY> Z") rather than relying on
    Z alone -- and some (see build_shape_lua's force_closed) never write a Z
    at all, instead relying entirely on their commands tracing back to the
    exact start point. Kept literally, that leaves a duplicate anchor sitting
    exactly on top of the first -- and since a fill mask always ends up
    Closed=true regardless of whether the source had a Z (see
    subpath_to_polyline_lua's force_closed), Fusion wraps the last point back
    to the first, adding a zero-length closing segment ON TOP of that
    duplicate. Worse, the real closing curve's incoming handle ends up
    stranded on that phantom point instead of on the first anchor, so the
    seam renders as a stray loop/spike instead of a smooth join -- confirmed
    against a real paste: every affected subpath was one with no explicit Z,
    each with a bit-identical first/last anchor still present.

    This intentionally does NOT gate on sp.closed
    -- whether the source spelled out a Z is irrelevant to whether the first
    and last anchor happen to coincide, which is the only thing that actually
    determines whether there's a phantom point to fold."""
    if len(sp.anchors) < 2:
        return
    first, last = sp.anchors[0], sp.anchors[-1]
    if abs(first['x'] - last['x']) > eps or abs(first['y'] - last['y']) > eps:
        return
    if last['in_cp'] is not None:
        first['in_cp'] = last['in_cp']
    if last['out_cp'] is not None and first['out_cp'] is None:
        first['out_cp'] = last['out_cp']
    sp.anchors.pop()


def rect_to_subpath(x, y, w, h, rx, ry):
    sp = Subpath(); sp.closed = True
    if rx <= 0 and ry <= 0:
        sp.add(x, y); sp.add(x + w, y); sp.add(x + w, y + h); sp.add(x, y + h)
        return sp
    rx = min(rx or ry, w / 2.0)
    ry = min(ry or rx, h / 2.0)
    k = 0.5522847498
    coords = [(x + rx, y), (x + w - rx, y), (x + w, y + ry), (x + w, y + h - ry),
              (x + w - rx, y + h), (x + rx, y + h), (x, y + h - ry), (x, y + ry)]
    corners_in = {2: (x + w, y + ry - ry * k), 4: (x + w - rx + rx * k, y + h),
                  6: (x, y + h - ry + ry * k), 0: (x + rx - rx * k, y)}
    corners_out = {1: (x + w - rx + rx * k, y), 3: (x + w, y + h - ry + ry * k),
                   5: (x + rx - rx * k, y + h), 7: (x, y + ry - ry * k)}
    for idx, (px, py) in enumerate(coords):
        sp.add(px, py, in_cp=corners_in.get(idx), out_cp=corners_out.get(idx))
    return sp


def ellipse_to_subpath(cx, cy, rx, ry):
    k = 0.5522847498
    sp = Subpath(); sp.closed = True
    pts = [
        (cx + rx, cy, (cx + rx, cy - ry * k), (cx + rx, cy + ry * k)),
        (cx, cy + ry, (cx + rx * k, cy + ry), (cx - rx * k, cy + ry)),
        (cx - rx, cy, (cx - rx, cy + ry * k), (cx - rx, cy - ry * k)),
        (cx, cy - ry, (cx - rx * k, cy - ry), (cx + rx * k, cy - ry)),
    ]
    for x, y, in_cp, out_cp in pts:
        sp.add(x, y, in_cp=in_cp, out_cp=out_cp)
    return sp


def polyline_to_subpath(points_str, closed):
    nums = [float(v) for v in _NUM_RE.findall(points_str)]
    sp = Subpath(); sp.closed = closed
    for j in range(0, len(nums) - 1, 2):
        sp.add(nums[j], nums[j + 1])
    return sp


# The full CSS/SVG named-colour set (with only a partial set,
# fill="crimson" etc. would draw as the grey placeholder). Keys are lower-case;
# parse_color lower-cases the value first, since CSS keywords are
# case-insensitive (fill="Red" is red).
_NAMED_COLORS = {name: "#" + hx for name, hx in (pair.split(':') for pair in """
    aliceblue:f0f8ff antiquewhite:faebd7 aqua:00ffff aquamarine:7fffd4 azure:f0ffff
    beige:f5f5dc bisque:ffe4c4 black:000000 blanchedalmond:ffebcd blue:0000ff
    blueviolet:8a2be2 brown:a52a2a burlywood:deb887 cadetblue:5f9ea0 chartreuse:7fff00
    chocolate:d2691e coral:ff7f50 cornflowerblue:6495ed cornsilk:fff8dc crimson:dc143c
    cyan:00ffff darkblue:00008b darkcyan:008b8b darkgoldenrod:b8860b darkgray:a9a9a9
    darkgreen:006400 darkgrey:a9a9a9 darkkhaki:bdb76b darkmagenta:8b008b
    darkolivegreen:556b2f darkorange:ff8c00 darkorchid:9932cc darkred:8b0000
    darksalmon:e9967a darkseagreen:8fbc8f darkslateblue:483d8b darkslategray:2f4f4f
    darkslategrey:2f4f4f darkturquoise:00ced1 darkviolet:9400d3 deeppink:ff1493
    deepskyblue:00bfff dimgray:696969 dimgrey:696969 dodgerblue:1e90ff firebrick:b22222
    floralwhite:fffaf0 forestgreen:228b22 fuchsia:ff00ff gainsboro:dcdcdc
    ghostwhite:f8f8ff gold:ffd700 goldenrod:daa520 gray:808080 green:008000
    greenyellow:adff2f grey:808080 honeydew:f0fff0 hotpink:ff69b4 indianred:cd5c5c
    indigo:4b0082 ivory:fffff0 khaki:f0e68c lavender:e6e6fa lavenderblush:fff0f5
    lawngreen:7cfc00 lemonchiffon:fffacd lightblue:add8e6 lightcoral:f08080
    lightcyan:e0ffff lightgoldenrodyellow:fafad2 lightgray:d3d3d3 lightgreen:90ee90
    lightgrey:d3d3d3 lightpink:ffb6c1 lightsalmon:ffa07a lightseagreen:20b2aa
    lightskyblue:87cefa lightslategray:778899 lightslategrey:778899
    lightsteelblue:b0c4de lightyellow:ffffe0 lime:00ff00 limegreen:32cd32 linen:faf0e6
    magenta:ff00ff maroon:800000 mediumaquamarine:66cdaa mediumblue:0000cd
    mediumorchid:ba55d3 mediumpurple:9370db mediumseagreen:3cb371
    mediumslateblue:7b68ee mediumspringgreen:00fa9a mediumturquoise:48d1cc
    mediumvioletred:c71585 midnightblue:191970 mintcream:f5fffa mistyrose:ffe4e1
    moccasin:ffe4b5 navajowhite:ffdead navy:000080 oldlace:fdf5e6 olive:808000
    olivedrab:6b8e23 orange:ffa500 orangered:ff4500 orchid:da70d6 palegoldenrod:eee8aa
    palegreen:98fb98 paleturquoise:afeeee palevioletred:db7093 papayawhip:ffefd5
    peachpuff:ffdab9 peru:cd853f pink:ffc0cb plum:dda0dd powderblue:b0e0e6
    purple:800080 rebeccapurple:663399 red:ff0000 rosybrown:bc8f8f royalblue:4169e1
    saddlebrown:8b4513 salmon:fa8072 sandybrown:f4a460 seagreen:2e8b57 seashell:fff5ee
    sienna:a0522d silver:c0c0c0 skyblue:87ceeb slateblue:6a5acd slategray:708090
    slategrey:708090 snow:fffafa springgreen:00ff7f steelblue:4682b4 tan:d2b48c
    teal:008080 thistle:d8bfd8 tomato:ff6347 turquoise:40e0d0 violet:ee82ee
    wheat:f5deb3 white:ffffff whitesmoke:f5f5f5 yellow:ffff00 yellowgreen:9acd32
""".split())}

_HEX_COLOR_RE = re.compile(r'#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$')
_COLOR_FUNC_RE = re.compile(r'(rgba?|hsla?)\(\s*(.*?)\s*\)$', re.S)
_CSS_NUM_RE = re.compile(r'([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(%|deg|rad|grad|turn)?$')
_HUE_UNIT_DEG = {None: 1.0, 'deg': 1.0, 'rad': 180.0 / math.pi, 'grad': 0.9, 'turn': 360.0}


def _css_num(tok):
    """(value, unit) for one colour-function argument -- unit is None, '%'
    or an angle unit; CSS4's 'none' reads as 0. Raises ValueError otherwise."""
    if tok == 'none':
        return 0.0, None
    mt = _CSS_NUM_RE.match(tok)
    if not mt:
        raise ValueError(tok)
    return float(mt.group(1)), mt.group(2)


def _parse_color_func(name, body):
    """rgb()/rgba()/hsl()/hsla(), in both the legacy comma syntax
    ("rgb(255, 0, 0)", "rgba(100%,0%,0%,.5)") and CSS Color 4's
    space-separated one ("rgb(255 0 0 / 50%)")."""
    alpha_tok = None
    if '/' in body:
        body, alpha_tok = body.split('/', 1)
        alpha_tok = alpha_tok.strip()
    toks = [t for t in re.split(r'[\s,]+', body.strip()) if t]
    if alpha_tok is None and len(toks) == 4:
        alpha_tok = toks.pop()
    if len(toks) != 3:
        raise ValueError(body)
    a = 1.0
    if alpha_tok is not None:
        v, unit = _css_num(alpha_tok)
        if unit not in (None, '%'):
            raise ValueError(alpha_tok)
        a = v / 100.0 if unit == '%' else v
    if name.startswith('rgb'):
        rgb = []
        for tok in toks:
            v, unit = _css_num(tok)
            if unit not in (None, '%'):
                raise ValueError(tok)
            rgb.append(v / 100.0 if unit == '%' else v / 255.0)
    else:
        h, h_unit = _css_num(toks[0])
        if h_unit == '%':
            raise ValueError(toks[0])
        sl = []
        for tok in toks[1:]:
            v, unit = _css_num(tok)
            if unit not in (None, '%'):
                raise ValueError(tok)
            sl.append(v / 100.0)  # CSS4 allows a bare number here, meaning the same as %
        sat, light = (max(0.0, min(1.0, c)) for c in sl)
        rgb = colorsys.hls_to_rgb((h * _HUE_UNIT_DEG[h_unit] / 360.0) % 1.0, light, sat)
    r, g, b = (max(0.0, min(1.0, c)) for c in rgb)
    return (r, g, b, max(0.0, min(1.0, a)))


def parse_color(value):
    """(r, g, b, a) in 0..1 for a CSS/SVG colour value, or None for
    none/url(...)/anything unparseable -- never raises, so one malformed
    fill falls back to the grey placeholder (and the Import Report's
    unparsed-colour note) instead of aborting the whole import. currentColor
    isn't resolved (no 'color' property tracking) and lands there too."""
    if not value:
        return None
    value = value.strip().lower()
    if value.startswith("url(") or value == "none":
        return None
    if value == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    value = _NAMED_COLORS.get(value, value)
    mt = _HEX_COLOR_RE.match(value)
    if mt:
        h = mt.group(1)
        if len(h) <= 4:
            h = ''.join(c * 2 for c in h)
        a = int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, a)
    mt = _COLOR_FUNC_RE.match(value)
    if mt:
        try:
            return _parse_color_func(mt.group(1), mt.group(2))
        except (ValueError, KeyError):
            return None
    return None


def _grad_coord(s, default):
    """x1/y1/x2/y2/cx/cy/r accept a plain number or a percentage -- treated
    as a bare 0..1 fraction either way (correct for objectBoundingBox, and a
    reasonable stand-in for the rare userSpaceOnUse-with-percentage case,
    which would properly need the referencing element's own viewport size)."""
    if s is None:
        return default
    s = s.strip()
    return float(s[:-1]) / 100.0 if s.endswith('%') else float(s)


def _parse_gradient_stops(el, id_map, visited):
    """A gradient with no <stop> children of its own inherits its stops from
    whatever it xlink:href's -- per the SVG spec's gradient-href
    inheritance, common when a doc defines one gradient's stops once and
    reuses them (via href) across several differently-positioned/shaped
    gradients. Recurses through id_map (the raw elements) rather than an
    already-parsed defs dict, so it works regardless of which gradient in
    the document happens to get parsed first."""
    stops = []
    for stop_el in el:
        if strip_ns(stop_el.tag) != 'stop':
            continue
        style = stop_el.get('style', '')
        style_map = {}
        for part in style.split(';'):
            if ':' in part:
                k, v = part.split(':', 1)
                style_map[k.strip()] = v.strip()
        offset_raw = stop_el.get('offset', '0')
        offset = float(offset_raw[:-1]) / 100.0 if offset_raw.endswith('%') else float(offset_raw or 0.0)
        color_raw = style_map.get('stop-color', stop_el.get('stop-color', '#000000'))
        color = parse_color(color_raw) or (0.0, 0.0, 0.0, 1.0)
        stop_opacity = float(style_map.get('stop-opacity', stop_el.get('stop-opacity', 1.0)))
        r, g, b, a = color
        stops.append((max(0.0, min(1.0, offset)), r, g, b, a * stop_opacity))
    if stops:
        return sorted(stops, key=lambda s: s[0])
    href = el.get('{http://www.w3.org/1999/xlink}href') or el.get('href')
    if href and href.startswith('#'):
        ref_id = href[1:]
        if ref_id not in visited and ref_id in id_map:
            return _parse_gradient_stops(id_map[ref_id], id_map, visited | {ref_id})
    return []


def parse_gradient_defs(root):
    """Builds id -> gradient-def dict for every <linearGradient>/
    <radialGradient> in the document (not just ones inside <defs> -- per
    spec a gradient, like a <use> target, can be declared anywhere)."""
    id_map = {el.get('id'): el for el in root.iter() if el.get('id')}
    defs = {}
    for el in root.iter():
        tag = strip_ns(el.tag)
        if tag not in ('linearGradient', 'radialGradient'):
            continue
        gid = el.get('id')
        if not gid:
            continue
        common = {
            "units": el.get('gradientUnits', 'objectBoundingBox'),
            "transform": parse_transform(el.get('gradientTransform', '')),
            "stops": _parse_gradient_stops(el, id_map, {gid}),
        }
        if tag == 'linearGradient':
            defs[gid] = dict(common, kind="linear",
                              x1=_grad_coord(el.get('x1'), 0.0), y1=_grad_coord(el.get('y1'), 0.0),
                              x2=_grad_coord(el.get('x2'), 1.0), y2=_grad_coord(el.get('y2'), 0.0))
        else:
            defs[gid] = dict(common, kind="radial",
                              cx=_grad_coord(el.get('cx'), 0.5), cy=_grad_coord(el.get('cy'), 0.5),
                              r=_grad_coord(el.get('r'), 0.5))
    return defs


def _subpaths_bbox(subpaths):
    """Bounding box (minx, miny, w, h) over every anchor AND control point --
    an approximation (the true bbox of a bezier can bulge past its anchors),
    good enough for objectBoundingBox gradient placement, which is already
    just an approximation of Fusion's own gradient model (see
    resolve_gradient)."""
    xs, ys = [], []
    for sp in subpaths:
        for a in sp.anchors:
            xs.append(a['x']); ys.append(a['y'])
            if a['in_cp'] is not None:
                xs.append(a['in_cp'][0]); ys.append(a['in_cp'][1])
            if a['out_cp'] is not None:
                xs.append(a['out_cp'][0]); ys.append(a['out_cp'][1])
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    return (minx, miny, max(maxx - minx, 1e-9), max(maxy - miny, 1e-9))


def resolve_gradient(value, gradient_defs, m, local_bbox):
    """Resolves a fill/stroke="url(#id)" reference against parsed gradient
    defs into a dict of ABSOLUTE, already-transformed SVG user-space
    coordinates ready for to_point() -- {"kind": "linear", "p0": (x, y),
    "p1": (x, y), "stops": [...]} or {"kind": "radial", "center": (x, y),
    "edge": (x, y), "stops": [...]}. Returns None for a plain color, "none",
    or an unresolvable/stop-less reference -- the caller falls back to the
    flat mid-grey placeholder in that case.

    local_bbox (see _subpaths_bbox) is the shape's own bounding box in its
    OWN user space, before `m`. Per spec an objectBoundingBox gradient maps
    0-1 onto that box and THEN goes through the shape's transform, so a
    rotated or skewed shape carries its gradient round with it. (Using the
    absolute, already-transformed bbox instead left the gradient axis-aligned
    on a rotated shape.)"""
    if not value:
        return None
    ref = re.match(r'url\(#([^)]+)\)', value.strip())
    if not ref:
        return None
    gdef = gradient_defs.get(ref.group(1))
    if not gdef or not gdef["stops"]:
        return None
    if gdef["units"] == "objectBoundingBox":
        minx, miny, w, h = local_bbox
        base = mat_mul(m, (w, 0.0, 0.0, h, minx, miny))
    else:
        base = m
    full = mat_mul(base, gdef["transform"])
    if gdef["kind"] == "linear":
        p0 = mat_point(full, gdef["x1"], gdef["y1"])
        p1 = mat_point(full, gdef["x2"], gdef["y2"])
        return {"kind": "linear", "p0": p0, "p1": p1, "stops": gdef["stops"]}
    center = mat_point(full, gdef["cx"], gdef["cy"])
    # Fusion's own radial gradient has no ellipse/rotation support (its
    # Start/End fields are just "center" and "one point on the rim" --
    # confirmed against a real native-import dump), so a non-uniform SVG
    # radial gradient (elliptical, or one with a separate fx/fy focal point)
    # can't be represented exactly; this picks the 0-degree rim point and
    # drops fx/fy, which is what native import itself appears to do too.
    edge = mat_point(full, gdef["cx"] + gdef["r"], gdef["cy"])
    return {"kind": "radial", "center": center, "edge": edge, "stops": gdef["stops"]}


# A filter that's ONLY a Gaussian blur -- the feFlood+feBlend pair is just
# framing boilerplate every Figma/svgrepo export wraps around it (flood a
# transparent backdrop, blend the source over it) with no visible effect of
# its own -- maps onto a PolylineMask's own SoftEdge input (see
# resolve_blur_filter/build_mask_tool_lua). A filter combining feOffset/
# feColorMatrix/feComposite/feMorphology (drop-shadow, inner-shadow -- common
# in the same document, e.g. this SVG's own filter0_iii/filter3_d/filter9_i)
# is deliberately left unresolved rather than partially approximated; a
# shape referencing one of those is treated exactly like having no filter,
# same as today.
_BLUR_ONLY_TAGS = {'feFlood', 'feBlend', 'feGaussianBlur'}


def parse_filter_defs(root):
    """Builds id -> Gaussian-blur stdDeviation (float) for every <filter>
    that's a pure blur per _BLUR_ONLY_TAGS above. Filters that don't qualify
    simply have no entry, exactly like a gradient id that fails to resolve."""
    defs = {}
    for el in root.iter():
        if strip_ns(el.tag) != 'filter':
            continue
        fid = el.get('id')
        if not fid:
            continue
        stddev = None
        simple = True
        for prim in el:
            ptag = strip_ns(prim.tag)
            if ptag not in _BLUR_ONLY_TAGS:
                simple = False
                break
            if ptag == 'feGaussianBlur':
                if stddev is not None:
                    simple = False  # more than one blur primitive -- not the plain case
                    break
                nums = [float(v) for v in _NUM_RE.findall(prim.get('stdDeviation', '0'))]
                stddev = nums[0] if nums else 0.0
        if simple and stddev:
            defs[fid] = stddev
    return defs


def resolve_blur_filter(value, filter_defs):
    if not value or not filter_defs:
        return None
    ref = re.match(r'url\(#([^)]+)\)', value.strip())
    if not ref:
        return None
    return filter_defs.get(ref.group(1))


def parse_drop_shadow_filter_defs(root):
    """Builds id -> {dx, dy, stddev, color:(r,g,b), opacity} for every
    <filter> that's a plain drop-shadow expansion: feFlood, then a
    feColorMatrix isolating SourceAlpha (Figma/svgrepo's usual
    "hardAlpha"), feOffset, feGaussianBlur, an OPTIONAL feComposite (some
    exports add one cutting the original silhouette out of the shadow
    before recoloring -- moot once the shadow is drawn BEHIND the original,
    so it's skipped rather than modeled), a recoloring feColorMatrix, and
    exactly two feBlend primitives ending with the ORIGINAL SourceGraphic
    blended back on top (confirming "shadow behind", not one shadow layer
    stacking onto another -- see below).

    This structural check (second primitive reads SourceAlpha; last blend's
    `in` is SourceGraphic) is what tells a drop shadow apart from an INNER
    shadow filter (same document, e.g. this SVG's own filter0_iii/
    filter5_iii) without relying on the exporter's own filter-id naming
    (_d vs _i/_iii) -- an inner shadow's first couple of primitives are
    instead feFlood + feBlend(SourceGraphic, result="shape"), and each
    shadow LAYER blends onto that accumulating "shape" rather than a
    separate BackgroundImageFix, since it composites INSIDE the shape, not
    behind it. Filters matching neither shape are left unresolved, same as
    any other unsupported filter."""
    defs = {}
    for el in root.iter():
        if strip_ns(el.tag) != 'filter':
            continue
        fid = el.get('id')
        if not fid:
            continue
        prims = list(el)
        tags = [strip_ns(p.tag) for p in prims]
        if len(tags) < 7 or tags[0] != 'feFlood' or tags[1] != 'feColorMatrix':
            continue
        if prims[1].get('in') != 'SourceAlpha':
            continue
        i = 2
        if i >= len(tags) or tags[i] != 'feOffset':
            continue
        offset_el = prims[i]
        i += 1
        if i >= len(tags) or tags[i] != 'feGaussianBlur':
            continue
        blur_el = prims[i]
        i += 1
        if i < len(tags) and tags[i] == 'feComposite':
            i += 1
        if i >= len(tags) or tags[i] != 'feColorMatrix':
            continue
        recolor_el = prims[i]
        i += 1
        if i + 2 != len(tags) or tags[i] != 'feBlend' or tags[i + 1] != 'feBlend':
            continue
        if prims[i + 1].get('in') != 'SourceGraphic':
            continue
        nums = [float(v) for v in _NUM_RE.findall(recolor_el.get('values', ''))]
        if len(nums) != 20:
            continue
        bnums = [float(v) for v in _NUM_RE.findall(blur_el.get('stdDeviation', '0'))]
        defs[fid] = {
            "dx": float(offset_el.get('dx', 0) or 0),
            "dy": float(offset_el.get('dy', 0) or 0),
            "stddev": bnums[0] if bnums else 0.0,
            "color": (nums[4], nums[9], nums[14]),
            "opacity": nums[18],
        }
    return defs


def resolve_drop_shadow_filter(value, drop_shadow_defs):
    if not value or not drop_shadow_defs:
        return None
    ref = re.match(r'url\(#([^)]+)\)', value.strip())
    if not ref:
        return None
    return drop_shadow_defs.get(ref.group(1))


_CSS_COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)
_CSS_RULE_RE = re.compile(r'([^{}]*)\{([^{}]*)\}')
_CSS_CLASS_SELECTOR_RE = re.compile(r'\.([A-Za-z0-9_-]+)$')


def parse_css_classes(root, report=None):
    """Builds class name -> [(rule order, {property: value})] from every
    <style> element. Illustrator exports put all their colours here
    (.cls-1{fill:#e30613;}, often grouped: .cls-1,.cls-3{fill:none;}), so
    ignoring <style> imported those files solid black.

    Only plain class selectors are understood; any other rule (element or
    id selectors, descendants, @media...) is counted on the report rather
    than silently dropped. The order is kept so that, as in CSS, a later
    rule wins when an element has several classes."""
    classes = {}
    order = 0
    for style_el in root.iter():
        if strip_ns(style_el.tag) != 'style' or not style_el.text:
            continue
        text = _CSS_COMMENT_RE.sub('', style_el.text)
        if '@' in text and report is not None:
            report.note_unsupported_css_rule()
        for selectors, body in _CSS_RULE_RE.findall(text):
            decls = {}
            for part in body.split(';'):
                if ':' in part:
                    k, v = part.split(':', 1)
                    decls[k.strip()] = v.replace('!important', '').strip()
            for sel in selectors.split(','):
                sel = sel.strip()
                if not sel:
                    continue
                cls = _CSS_CLASS_SELECTOR_RE.match(sel)
                if cls is None:
                    if report is not None:
                        report.note_unsupported_css_rule()
                    continue
                classes.setdefault(cls.group(1), []).append((order, decls))
                order += 1
    return classes


def _style_map(el, css_classes):
    """The element's CSS class declarations (in rule order) overlaid by its
    own style="..." - both of which beat presentation attributes like
    fill="...", which callers fall back to."""
    style_map = {}
    if css_classes:
        rules = []
        for cls in el.get('class', '').split():
            rules.extend(css_classes.get(cls, ()))
        for _order, decls in sorted(rules, key=lambda r: r[0]):
            style_map.update(decls)
    for part in el.get('style', '').split(';'):
        if ':' in part:
            k, v = part.split(':', 1)
            style_map[k.strip()] = v.strip()
    return style_map


def parse_inner_shadow_filter_defs(root):
    """Builds id -> list of {dx, dy, stddev, color, opacity} layers (1-3 in
    practice) for every <filter> that's an inner-shadow expansion: feFlood
    + feBlend(SourceGraphic, BackgroundImageFix, result="shape") -- a
    running accumulator, NOT the drop-shadow's SourceAlpha isolation, which
    is the structural check that tells the two apart -- followed by one or
    more repeating cycles of: feColorMatrix isolating SourceAlpha, an
    OPTIONAL feMorphology (erode/dilate radius -- consumed but not
    modeled, see _build_inner_shadow_layer_lua), feOffset, feGaussianBlur,
    feComposite, a recoloring feColorMatrix, and a feBlend that accumulates
    onto "shape" (each layer's `in2` is the PREVIOUS layer's own result,
    or "shape" for the first -- not read here, since layer order in the
    document is already the right composite order for rebuilding it)."""
    defs = {}
    for el in root.iter():
        if strip_ns(el.tag) != 'filter':
            continue
        fid = el.get('id')
        if not fid:
            continue
        prims = list(el)
        tags = [strip_ns(p.tag) for p in prims]
        if len(tags) < 2 or tags[0] != 'feFlood' or tags[1] != 'feBlend':
            continue
        if prims[1].get('in') != 'SourceGraphic':
            continue
        layers = []
        i = 2
        ok = True
        while i < len(tags):
            if tags[i] != 'feColorMatrix' or prims[i].get('in') != 'SourceAlpha':
                ok = False
                break
            i += 1
            if i < len(tags) and tags[i] == 'feMorphology':
                i += 1
            if i >= len(tags) or tags[i] != 'feOffset':
                ok = False
                break
            offset_el = prims[i]
            i += 1
            if i >= len(tags) or tags[i] != 'feGaussianBlur':
                ok = False
                break
            blur_el = prims[i]
            i += 1
            if i >= len(tags) or tags[i] != 'feComposite':
                ok = False
                break
            i += 1
            if i >= len(tags) or tags[i] != 'feColorMatrix':
                ok = False
                break
            recolor_el = prims[i]
            i += 1
            if i >= len(tags) or tags[i] != 'feBlend':
                ok = False
                break
            i += 1
            nums = [float(v) for v in _NUM_RE.findall(recolor_el.get('values', ''))]
            if len(nums) != 20:
                ok = False
                break
            bnums = [float(v) for v in _NUM_RE.findall(blur_el.get('stdDeviation', '0'))]
            layers.append({
                "dx": float(offset_el.get('dx', 0) or 0),
                "dy": float(offset_el.get('dy', 0) or 0),
                "stddev": bnums[0] if bnums else 0.0,
                "color": (nums[4], nums[9], nums[14]),
                "opacity": nums[18],
            })
        if ok and layers:
            defs[fid] = layers
    return defs


def resolve_inner_shadow_filter(value, inner_shadow_defs):
    if not value or not inner_shadow_defs:
        return None
    ref = re.match(r'url\(#([^)]+)\)', value.strip())
    if not ref:
        return None
    return inner_shadow_defs.get(ref.group(1))


class ShapeNode:
    def __init__(self, name, subpaths, has_fill, color, opacity,
                 has_stroke, stroke_color, stroke_width, stroke_opacity,
                 fill_gradient=None, stroke_gradient=None, blur_stddev_px=None, drop_shadow=None,
                 inner_shadow=None):
        self.name, self.subpaths = name, subpaths
        self.has_fill, self.color, self.opacity = has_fill, color, opacity
        self.has_stroke = has_stroke
        self.stroke_color, self.stroke_width, self.stroke_opacity = stroke_color, stroke_width, stroke_opacity
        self.fill_gradient, self.stroke_gradient = fill_gradient, stroke_gradient
        self.blur_stddev_px = blur_stddev_px
        self.drop_shadow = drop_shadow
        self.inner_shadow = inner_shadow


class GroupNode:
    def __init__(self, name):
        self.name = name
        self.children = []
        # Populated by walk() when this <g> carries a mask="url(#...)" or
        # clip-path="url(#...)" that resolves to a real <mask>/<clipPath>
        # element -- see _resolve_ref_clip_subpaths.
        self.clip_subpaths = None
        # Populated by lottie_layer_to_group_node when this group is a
        # Lottie shape layer with its own Position/Anchor/Scale/Rotation/
        # Opacity keyframes -- a LottieLayerAnimation, or None for every
        # SVG-sourced group (SVG carries no animation at all) and for a
        # Lottie layer whose own ks never gets consulted (the static-pose
        # export ignores this field entirely; only the animated export
        # reads it).
        self.layer_animation = None
        # Populated by lottie_layer_to_group_node for a Lottie layer with
        # a real AE "parent" chain -- a list of LottieLayerAnimation (one
        # per ancestor, nearest parent first, root last), each an
        # ADDITIONAL Transform to compose on top of this layer's own (see
        # build_group_lua's own wrap hookup) -- [] for every SVG-sourced
        # group and every unparented Lottie layer.
        self.ancestor_animations = []
        # True when this node's own layer_animation MUST be emitted as a
        # real (live) Fusion Transform rather than baked into geometry --
        # set for a PROMOTED Lottie shape group (see
        # lottie_shapes_to_nodes/_lottie_group_needs_promotion) and for
        # any layer containing one, since baking an ancestor's transform
        # while a descendant's stays live would compose the two in the
        # WRONG order (AE's own child_world = parent o child). False for
        # every SVG-sourced group.
        self.needs_live_transform = False
        # Which coordinate spaces this node's own transform maps between
        # (one of the _LOTTIE_SPACE_* constants, which decides its
        # Center/Pivot offsets -- see _lottie_transform_offsets). None
        # means the default "own layer" space; a promoted shape group
        # sets _LOTTIE_SPACE_SHAPE_GROUP instead.
        self.transform_space = None
        # Populated for a Lottie layer clipped by a "td" track matte (see
        # _lottie_matte_clip_subpaths): the matte's resolved-static
        # geometry, in composition-coordinates-minus-half-the-comp space.
        # matte_clip_after is how many of this node's own
        # ancestor_animations belong to the SAME composition as the matte,
        # i.e. how far along the chain the clip has to be applied -- any
        # ancestors past that came from an enclosing precomp instance and
        # must be applied AFTER the clip, so the clipped result animates
        # with the instance instead of the mask being left behind.
        self.matte_clip_subpaths = None
        self.matte_clip_after = 0
        # Populated by lottie_layer_to_group_node for a Lottie layer: a
        # LIST of (ip, op) intervals this layer is actually visible for,
        # already converted to comp-absolute LOTTIE frame units and
        # intersected through every enclosing precomp instance's own
        # window (see _collect_lottie_shape_layers). Always exactly one
        # interval for a plain imported layer; a CONSOLIDATED object (see
        # _consolidate_lottie_layers) carries the union of its segments'
        # own windows, which is normally still one interval since
        # consecutive segments touch. None for every SVG-sourced group
        # (SVG has no time axis at all).
        self.visibility_window = None


def strip_ns(tag):
    return tag.split('}')[-1] if '}' in tag else tag


def get_name(el, fallback):
    return el.get('id') or el.attrib.get('data-name') or fallback


def get_style_fill(el, inherited_fill, inherited_opacity, css_classes=None,
                   inherited_fill_opacity=1.0, inherited_visible=True):
    """Returns (fill, fill_opacity, opacity, displayed, visible).

    fill-opacity INHERITS (an unset child keeps its parent's value) and only
    affects fills; opacity MULTIPLIES down the tree and affects fill and
    stroke alike - so they're kept apart, or a group's fill-opacity would
    dim its children's strokes too.

    displayed=False (display:none) drops the element and everything in it.
    visibility inherits and a child may set it back to visible, so it's
    returned for the caller to pass down and only applied to shapes."""
    style_map = _style_map(el, css_classes)
    fill = style_map.get('fill', el.get('fill', inherited_fill))
    fill_opacity = float(style_map.get('fill-opacity', el.get('fill-opacity', inherited_fill_opacity)))
    opacity = float(style_map.get('opacity', el.get('opacity', 1.0))) * inherited_opacity
    displayed = style_map.get('display', el.get('display', '')).strip() != 'none'
    visibility = style_map.get('visibility', el.get('visibility', '')).strip()
    visible = inherited_visible if visibility in ('', 'inherit') else visibility == 'visible'
    return fill, fill_opacity, opacity, displayed, visible


_LENGTH_RE = re.compile(r'\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(%|[A-Za-z]*)\s*$')
# CSS absolute units at 96 px/in. em/ex/rem have no font to resolve against
# here, so they get the browser-default 16px font (ex ~ half an em) -- close
# enough to draw something sensible instead of failing the import.
_LENGTH_UNIT_PX = {
    '': 1.0, 'px': 1.0, 'in': 96.0, 'cm': 96.0 / 2.54, 'mm': 96.0 / 25.4,
    'q': 96.0 / 101.6, 'pt': 96.0 / 72.0, 'pc': 16.0,
    'em': 16.0, 'rem': 16.0, 'ex': 8.0,
}


def svg_length_px(s, ref=None, default=None):
    """A length attribute/property value (x="10", width="210mm",
    r="10px", width="100%") in user units/px. A percentage is of ref --
    the relevant viewport extent, see _viewport_ref -- and falls back to
    default when there's no ref to take it of; so does anything empty,
    malformed or in an unknown unit, rather than raising and aborting the
    whole import (as a bare float() of width="100%" would)."""
    if s is None:
        return default
    mt = _LENGTH_RE.match(str(s))
    if not mt:
        return default
    v = float(mt.group(1))
    unit = mt.group(2).lower()
    if unit == '%':
        return v / 100.0 * ref if ref is not None else default
    factor = _LENGTH_UNIT_PX.get(unit)
    return v * factor if factor is not None else default


def _viewport_ref(viewport, axis):
    """What a percentage length is OF, per the SVG spec: the viewport's width
    for x-ish attributes ('x'), its height for y-ish ones ('y'), and its
    normalized diagonal sqrt((w^2 + h^2) / 2) for ones with no direction
    (circle r, 'd'). viewport is (w, h) in user units -- the root viewBox
    extent, see build_svg_tools -- or None when unknown."""
    if not viewport:
        return None
    w, h = viewport
    if axis == 'x':
        return w
    if axis == 'y':
        return h
    return math.sqrt((w * w + h * h) / 2.0)


def parse_svg_length(s):
    """A stroke-width-style length as a bare float in px (units converted,
    see svg_length_px), or None if s is empty/unparseable. Percentages have
    no reference here, so they also come back None."""
    if not s:
        return None
    return svg_length_px(s)


def get_style_stroke(el, inherited_stroke, inherited_stroke_width, inherited_stroke_opacity, css_classes=None):
    """stroke/stroke-width inherit like fill does (an unset child keeps the
    parent's actual value, per the SVG spec), NOT the multiplicative
    accumulation get_style_fill uses for the generic 'opacity' property --
    those are genuinely different inheritance rules, not an inconsistency."""
    style_map = _style_map(el, css_classes)
    stroke = style_map.get('stroke', el.get('stroke', inherited_stroke))
    stroke_width_raw = style_map.get('stroke-width', el.get('stroke-width'))
    stroke_width = parse_svg_length(stroke_width_raw) if stroke_width_raw else inherited_stroke_width
    stroke_opacity = float(style_map.get('stroke-opacity', el.get('stroke-opacity', inherited_stroke_opacity)))
    return stroke, stroke_width, stroke_opacity


# Tags walk() has no drawing behavior for but that are EXPECTED to produce
# no visible shape -- accessibility/authoring metadata, animation
# directives, etc. -- so they're excluded from ImportReport's "unsupported
# element" tally. Anything else walk() doesn't recognize (text, image,
# pattern, symbol, clipPath, tspan, foreignObject, ...) genuinely represents
# content that got silently dropped, and IS worth surfacing.
_REPORT_IGNORED_TAGS = {
    'title', 'desc', 'metadata', 'style', 'script',
    'animate', 'animateTransform', 'animateMotion', 'animateColor', 'set', 'switch',
}


class ImportReport:
    """Collects notable approximations/omissions made during one SVG parse
    -- gradients that didn't resolve, colors that didn't parse, filters that
    matched no supported pattern, clip-path/mask references that didn't
    resolve, and element types walk() has no drawing behavior for at all --
    for surfacing to the user afterward (see build_svg_tools's own summary
    line) instead of each one silently falling back to a placeholder or
    vanishing with no trace, which is what happened before this existed.

    Deliberately tallies by CATEGORY rather than per-element/per-shape
    detail -- an animator needs "3 gradients got approximated" to know
    where to go look, not a full trace; "Dump Selected Node Settings"
    already exists for genuine per-shape debugging.

    NOT covered (too expensive to detect cheaply, and rare in practice --
    see build_shape_lua's own docstring on this): a fill-rule="nonzero"
    path whose subpaths happen to overlap with the SAME winding direction,
    which this generator always fills even-odd regardless."""

    def __init__(self):
        self.unsupported_elements = collections.Counter()
        self.unparsed_colors = 0
        self.unresolved_gradients = 0
        self.unsupported_filters = 0
        self.unresolved_masks = 0
        self.unsupported_clip_path_attr = 0
        self.unsupported_css_rules = 0

    def note_unsupported_element(self, tag):
        self.unsupported_elements[tag] += 1

    def note_unparsed_color(self):
        self.unparsed_colors += 1

    def note_unresolved_gradient(self):
        self.unresolved_gradients += 1

    def note_unsupported_filter(self):
        self.unsupported_filters += 1

    def note_unresolved_mask(self):
        self.unresolved_masks += 1

    def note_unresolved_clip_path_attr(self):
        self.unsupported_clip_path_attr += 1

    def note_unsupported_css_rule(self):
        self.unsupported_css_rules += 1

    def has_findings(self):
        return bool(self.unsupported_elements) or any([
            self.unparsed_colors, self.unresolved_gradients, self.unsupported_filters,
            self.unresolved_masks, self.unsupported_clip_path_attr, self.unsupported_css_rules,
        ])

    def summary_lines(self):
        lines = []
        if self.unparsed_colors:
            lines.append(f"[NOTE] {self.unparsed_colors} fill/stroke colour value(s) couldn't be "
                          "parsed (unsupported or malformed syntax, e.g. currentColor/a misspelled "
                          "colour name) -- drawn as flat mid-grey placeholders instead.")
        if self.unresolved_gradients:
            lines.append(f"[NOTE] {self.unresolved_gradients} gradient fill/stroke reference(s) "
                          "couldn't be resolved (missing, no stops, or not linear/radial) -- drawn "
                          "as flat mid-grey placeholders instead.")
        if self.unsupported_filters:
            lines.append(f"[NOTE] {self.unsupported_filters} filter(s) didn't match a supported "
                          "pattern (plain blur / drop-shadow / inner-shadow) -- ignored entirely, "
                          "those shapes import with no filter effect at all.")
        if self.unresolved_masks:
            lines.append(f"[NOTE] {self.unresolved_masks} mask=\"url(...)\" reference(s) couldn't "
                          "be resolved -- ignored, those groups import unclipped.")
        if self.unsupported_clip_path_attr:
            lines.append(f"[NOTE] {self.unsupported_clip_path_attr} clip-path=\"url(...)\" reference(s) "
                          "couldn't be applied -- not on a <g>/<svg> element, didn't resolve to a real "
                          "<clipPath>, or that element already had its own mask=... (mask wins rather "
                          "than combining the two) -- ignored, those elements/groups import unclipped.")
        if self.unsupported_css_rules:
            lines.append(f"[NOTE] {self.unsupported_css_rules} <style> rule(s) weren't understood -- "
                          "only plain class rules (.name {...}) are read; shapes those rules "
                          "style may import with the wrong colours.")
        for tag in sorted(self.unsupported_elements):
            count = self.unsupported_elements[tag]
            lines.append(f"[NOTE] Skipped {count} <{tag}> element(s) -- not a supported type "
                         "(nothing drawn for it).")
        return lines


def walk(el, matrix, inherited_fill, inherited_opacity, counter, id_map=None, use_stack=frozenset(),
         inherited_stroke=None, inherited_stroke_width=1.0, inherited_stroke_opacity=1.0, gradient_defs=None,
         filter_defs=None, inherited_blur=None, drop_shadow_defs=None, inherited_drop_shadow=None,
         inner_shadow_defs=None, inherited_inner_shadow=None, report=None, viewport=None, css_classes=None,
         inherited_fill_opacity=1.0, inherited_visible=True):
    tag = strip_ns(el.tag)
    m = mat_mul(matrix, parse_transform(el.get('transform', '')))
    fill, fill_opacity, opacity, displayed, visible = get_style_fill(
        el, inherited_fill, inherited_opacity, css_classes, inherited_fill_opacity, inherited_visible)
    stroke, stroke_width, stroke_opacity = get_style_stroke(
        el, inherited_stroke, inherited_stroke_width, inherited_stroke_opacity, css_classes)
    if not displayed:
        return None  # display:none - hidden layers in Illustrator/Figma exports
    # A filter doesn't cascade the way fill/stroke properties do -- per spec
    # each element needs its own filter to be affected -- but propagating a
    # <g filter="..."> down to its children is the closest approximation
    # available, and matches how these exports actually use it: one filter
    # wrapping exactly the shape(s) it's meant for (see this SVG's own
    # <g filter="url(#filter1_f...)"><circle .../></g> cheek blush).
    own_blur = resolve_blur_filter(el.get('filter'), filter_defs)
    blur = own_blur if own_blur is not None else inherited_blur
    own_drop_shadow = resolve_drop_shadow_filter(el.get('filter'), drop_shadow_defs)
    drop_shadow = own_drop_shadow if own_drop_shadow is not None else inherited_drop_shadow
    own_inner_shadow = resolve_inner_shadow_filter(el.get('filter'), inner_shadow_defs)
    inner_shadow = own_inner_shadow if own_inner_shadow is not None else inherited_inner_shadow

    if report is not None:
        filter_attr = el.get('filter')
        if filter_attr and own_blur is None and own_drop_shadow is None and own_inner_shadow is None:
            report.note_unsupported_filter()
        # clip-path is a DIFFERENT attribute from mask, and per spec is valid
        # on any element -- but only resolved on <g>/<svg> here (same scope
        # as mask=..., see the 'g'/'svg' branch below), since that's the
        # overwhelmingly common real-world pattern (Figma/Illustrator wrap
        # the clipped content in a group) and clip_subpaths only exists on
        # GroupNode. On any other element it's flagged unsupported straight
        # away; on a <g>/<svg> it's flagged below instead, only if it
        # DIDN'T end up resolving.
        if el.get('clip-path') and tag not in ('g', 'svg'):
            report.note_unresolved_clip_path_attr()

    if tag == 'defs':
        # Definitions are never rendered directly -- content only appears
        # when a <use> elsewhere in the document references its id (below).
        # Without this, walking into <defs> like any other container would
        # render every definition a SECOND time in place, on top of
        # whatever <use> already draws it -- common in icon sets/complex
        # exports that define shapes once and reuse them.
        return None

    if tag == 'use':
        href = el.get('{http://www.w3.org/1999/xlink}href') or el.get('href')
        if not href or not href.startswith('#') or id_map is None:
            return None
        ref_id = href[1:]
        target = id_map.get(ref_id)
        if target is None or ref_id in use_stack:
            return None  # unresolvable or a circular <use> reference -- skip rather than loop forever
        # <use> positions its target via an implicit translate(x, y) --
        # applied here rather than folded into parse_transform, since x/y
        # are separate attributes, not part of the transform= list.
        ux = svg_length_px(el.get('x'), _viewport_ref(viewport, 'x'), 0.0)
        uy = svg_length_px(el.get('y'), _viewport_ref(viewport, 'y'), 0.0)
        m = mat_mul(m, (1, 0, 0, 1, ux, uy))
        return walk(target, m, fill, opacity, counter, id_map, use_stack | {ref_id},
                    stroke, stroke_width, stroke_opacity, gradient_defs, filter_defs, blur,
                    drop_shadow_defs, drop_shadow, inner_shadow_defs, inner_shadow, report=report,
                    viewport=viewport, css_classes=css_classes, inherited_fill_opacity=fill_opacity,
                    inherited_visible=visible)

    if tag in ('g', 'svg'):
        node = GroupNode(get_name(el, f"Group_{next(counter)}"))
        # A mask="url(#...)" referencing a real <mask> element, or a
        # clip-path="url(#...)" referencing a real <clipPath> element, gets
        # approximated as a hard-edged clip on this group's own children
        # (see _resolve_ref_clip_subpaths/build_group_lua) -- rather than
        # silently dropped, which is what happened before either of these
        # existed (and still happens for a CSS-shape or filter-based
        # clip-path, or one using objectBoundingBox units -- out of scope
        # for now).
        mask_ref = el.get('mask')
        if mask_ref:
            resolved = False
            if id_map is not None:
                ref = re.match(r'url\(#([^)]+)\)', mask_ref.strip())
                mask_el = id_map.get(ref.group(1)) if ref else None
                if mask_el is not None and strip_ns(mask_el.tag) == 'mask':
                    node.clip_subpaths = _resolve_ref_clip_subpaths(
                        mask_el, m, counter, id_map, gradient_defs, filter_defs, report=report,
                        viewport=viewport, css_classes=css_classes)
                    resolved = True
            if not resolved and report is not None:
                report.note_unresolved_mask()

        # clip-path resolves the same way mask does, approximated as a
        # hard-edged clip on the union of the referenced <clipPath>'s own
        # child shapes (see _resolve_ref_clip_subpaths). If mask ALSO
        # resolved above, mask wins here rather than trying to intersect two
        # different simplistic clips -- both landing on the SAME element is
        # rare in practice, and this is flagged in the Import Report either
        # way (see note_unresolved_clip_path_attr below).
        clip_path_ref = el.get('clip-path')
        if clip_path_ref:
            if node.clip_subpaths is not None:
                if report is not None:
                    report.note_unresolved_clip_path_attr()
            else:
                resolved = False
                if id_map is not None:
                    ref = re.match(r'url\(#([^)]+)\)', clip_path_ref.strip())
                    clip_el = id_map.get(ref.group(1)) if ref else None
                    if clip_el is not None and strip_ns(clip_el.tag) == 'clipPath':
                        node.clip_subpaths = _resolve_ref_clip_subpaths(
                            clip_el, m, counter, id_map, gradient_defs, filter_defs, report=report,
                            viewport=viewport, css_classes=css_classes)
                        resolved = True
                if not resolved and report is not None:
                    report.note_unresolved_clip_path_attr()
        for child in el:
            child_node = walk(child, m, fill, opacity, counter, id_map, use_stack,
                               stroke, stroke_width, stroke_opacity, gradient_defs, filter_defs, blur,
                               drop_shadow_defs, drop_shadow, inner_shadow_defs, inner_shadow, report=report,
                               viewport=viewport, css_classes=css_classes, inherited_fill_opacity=fill_opacity,
                               inherited_visible=visible)
            if child_node is not None:
                node.children.append(child_node)
        return node if node.children else None

    def length(attr, axis):
        return svg_length_px(el.get(attr), _viewport_ref(viewport, axis), 0.0)

    subpaths = None
    if tag == 'path':
        d = el.get('d')
        if d:
            subpaths = parse_path_d(d)
    elif tag == 'rect':
        # Geometry attributes are lengths, not bare numbers -- a full-canvas
        # <rect width="100%" height="100%"/> background is common, and a
        # plain float() of one would abort the whole import (see
        # svg_length_px). Unparseable values read as 0, i.e. not drawn.
        x, y = length('x', 'x'), length('y', 'y')
        w, h = length('width', 'x'), length('height', 'y')
        rx, ry = length('rx', 'x'), length('ry', 'y')
        if w > 0 and h > 0:
            subpaths = [rect_to_subpath(x, y, w, h, rx, ry)]
    elif tag == 'circle':
        cx, cy, r = length('cx', 'x'), length('cy', 'y'), length('r', 'd')
        if r > 0:
            subpaths = [ellipse_to_subpath(cx, cy, r, r)]
    elif tag == 'ellipse':
        cx, cy = length('cx', 'x'), length('cy', 'y')
        rx, ry = length('rx', 'x'), length('ry', 'y')
        if rx > 0 and ry > 0:
            subpaths = [ellipse_to_subpath(cx, cy, rx, ry)]
    elif tag == 'polygon':
        subpaths = [polyline_to_subpath(el.get('points', ''), closed=True)]
    elif tag == 'polyline':
        subpaths = [polyline_to_subpath(el.get('points', ''), closed=False)]
    elif tag == 'line':
        sp = Subpath()
        sp.add(length('x1', 'x'), length('y1', 'y'))
        sp.add(length('x2', 'x'), length('y2', 'y'))
        subpaths = [sp]
    elif report is not None and tag not in _REPORT_IGNORED_TAGS:
        report.note_unsupported_element(tag)

    if not subpaths or not visible:
        return None

    # Before the transform below: objectBoundingBox gradients are laid out
    # in the shape's own space - see resolve_gradient.
    local_bbox = _subpaths_bbox(subpaths) if gradient_defs else None

    for sp in subpaths:
        for a in sp.anchors:
            a['x'], a['y'] = mat_point(m, a['x'], a['y'])
            if a['in_cp'] is not None:
                a['in_cp'] = mat_point(m, *a['in_cp'])
            if a['out_cp'] is not None:
                a['out_cp'] = mat_point(m, *a['out_cp'])

    has_fill = fill != 'none'
    stroke_paint = stroke if stroke not in (None, 'none') else None
    has_stroke = stroke_paint is not None and stroke_width and stroke_width > 0
    if not has_fill and not has_stroke:
        # Genuinely unpainted -- e.g. fill="none" with no stroke at all (the
        # common stroke-only-line-art root default, see svg_to_lua). Skip it
        # rather than falling into the gradient-placeholder path below,
        # which would otherwise render it as a wrong solid grey block.
        return None

    color = parse_color(fill) if has_fill else None
    fill_gradient = resolve_gradient(fill, gradient_defs, m, local_bbox) if has_fill and gradient_defs else None
    if has_fill and report is not None:
        if fill.strip().startswith('url('):
            if fill_gradient is None:
                report.note_unresolved_gradient()
        elif color is None:
            report.note_unparsed_color()
    stroke_color = None
    stroke_gradient = None
    stroke_width_px = 0.0
    if has_stroke:
        stroke_color = parse_color(stroke_paint)
        stroke_gradient = resolve_gradient(stroke_paint, gradient_defs, m, local_bbox) if gradient_defs else None
        if report is not None:
            if stroke_paint.strip().startswith('url('):
                if stroke_gradient is None:
                    report.note_unresolved_gradient()
            elif stroke_color is None:
                report.note_unparsed_color()
        # Anchors/handles are already matrix-transformed above; stroke-width
        # is a scalar, not a point, so its on-canvas scale is approximated
        # as sqrt(|det(m)|) -- exact for translate/scale/rotate (the
        # overwhelming majority of real transforms), and a reasonable
        # single-number stand-in for skew/non-uniform scale, where even a
        # real renderer would vary the stroke's thickness along the path.
        ma, mb, mc, md, _, _ = m
        stroke_width_px = stroke_width * math.sqrt(abs(ma * md - mb * mc))

    blur_stddev_px = None
    if blur is not None:
        # stdDeviation is a length in the SVG's own user-space units, same
        # as stroke-width above -- so it gets the identical transform-scale
        # treatment (sqrt(|det(m)|)), not the anchor/handle point transform.
        ma, mb, mc, md, _, _ = m
        blur_stddev_px = blur * math.sqrt(abs(ma * md - mb * mc))

    drop_shadow_px = None
    if drop_shadow is not None:
        # dx/dy are a pure offset (no translation component) -- mat_vector,
        # not mat_point/the stroke-width scalar trick -- so the shadow
        # displaces in the right on-canvas DIRECTION under rotation/skew,
        # not just by the right magnitude. stddev is a length, same
        # treatment as blur above.
        ma, mb, mc, md, _, _ = m
        dx_t, dy_t = mat_vector(m, drop_shadow["dx"], drop_shadow["dy"])
        drop_shadow_px = {
            "dx": dx_t, "dy": dy_t,
            "stddev": drop_shadow["stddev"] * math.sqrt(abs(ma * md - mb * mc)),
            "color": drop_shadow["color"], "opacity": drop_shadow["opacity"],
        }

    inner_shadow_px = None
    if inner_shadow is not None:
        ma, mb, mc, md, _, _ = m
        det_scale = math.sqrt(abs(ma * md - mb * mc))
        inner_shadow_px = []
        for layer in inner_shadow:
            dx_t, dy_t = mat_vector(m, layer["dx"], layer["dy"])
            inner_shadow_px.append({
                "dx": dx_t, "dy": dy_t,
                "stddev": layer["stddev"] * det_scale,
                "color": layer["color"], "opacity": layer["opacity"],
            })

    return ShapeNode(get_name(el, f"Shape_{next(counter)}"), subpaths,
                      has_fill, color, fill_opacity * opacity,
                      has_stroke, stroke_color, stroke_width_px, stroke_opacity * opacity,
                      fill_gradient, stroke_gradient, blur_stddev_px, drop_shadow_px, inner_shadow_px)


def _resolve_ref_clip_subpaths(ref_el, m, counter, id_map, gradient_defs, filter_defs, report=None,
                               viewport=None, css_classes=None):
    """Flattens a <mask> or <clipPath> element's own vector content into one
    list of already-transformed Subpath objects, for use as a clip on
    whatever <g mask="url(#...)">/<g clip-path="url(#...)"> references it
    (see walk).

    Reuses walk() itself rather than a separate simplified path -- both a
    <mask>'s and a <clipPath>'s content are otherwise just ordinary drawable
    shapes, and by spec (maskContentUnits/clipPathUnits both default to
    userSpaceOnUse) in the SAME coordinate space as wherever the reference
    comes from, so passing the referencing element's own `m` through handles
    any transform/nesting inside the referenced content exactly like
    anywhere else.

    Approximates mask-type:alpha / clip-path the same way: which pixels the
    referenced element's own child shapes fill, not (for a <mask>) its
    children's own colors/opacity -- fine for the common case (one solid
    path carving out an opening, or a <clipPath> with one or a few shapes
    unioned together), not a full luminance-mask model, and not
    objectBoundingBox-relative clipPathUnits (out of scope -- see walk)."""
    subpaths = []

    def collect(n):
        if isinstance(n, ShapeNode):
            subpaths.extend(n.subpaths)
        else:
            for c in n.children:
                collect(c)

    for child in ref_el:
        child_node = walk(child, m, "#000000", 1.0, counter, id_map, frozenset(),
                           None, 1.0, 1.0, gradient_defs, filter_defs, None, report=report,
                           viewport=viewport, css_classes=css_classes)
        if child_node is not None:
            collect(child_node)
    return subpaths


# How much of the frame "Timeline" scaling fills: 0.9 leaves a ~5% border on
# the limiting axis, which is the "slightly less than full" this is meant to
# give rather than running the artwork right off the edges.
TIMELINE_FIT_MARGIN = 0.9

# Inner-shadow layers extract their opacity straight from the SVG filter's
# own recolor feColorMatrix -- correct per-layer, but 1-3 bright, translucent
# layers stacked via normal alpha-over compositing reads stronger overall
# than the source's own subtler rendering (browsers don't stack quite the
# same way once blur/erode softens each layer first). This is a manual
# intensity dial on top of that correct extraction, not a fix to a wrong
# value -- 0.1 means each layer's own alpha is scaled to 10% of what the SVG
# specifies (i.e. roughly a 90% reduction in visible strength).
INNER_SHADOW_INTENSITY = 0.1


def compute_svg_scale(mode, svg_w, svg_h, comp_w, comp_h):
    """SVG-units-to-comp-pixels factor for the chosen scaling mode.

    "native" is 1.0 -- the SVG's own pixel dimensions, which is exactly what
    Resolve's own importer does. That's measured, not assumed: dumping a
    native import and our own side by side gives an implied pixel scale of
    1.0000 on both axes in both, the native one only looking frame-filling
    because that comp happened to be the same size as the SVG. So a small
    SVG lands small in a 4K comp under native import too.

    "timeline" scales the viewBox to fit the comp, uniformly (one factor for
    both axes, from whichever axis runs out of room first) so the artwork
    keeps its proportions, then backs off by TIMELINE_FIT_MARGIN. Fitting
    the viewBox rather than the artwork's own bounding box keeps any padding
    the author built into the canvas, and keeps the result stable regardless
    of where in that canvas the art actually sits."""
    if mode != "timeline" or not svg_w or not svg_h:
        return 1.0
    return min(comp_w / float(svg_w), comp_h / float(svg_h)) * TIMELINE_FIT_MARGIN


def make_converter(min_x, min_y, svg_w, svg_h, comp_w, comp_h, scale=1.0):
    """Fusion's mask/polyline coordinates normalize X and Y INDEPENDENTLY --
    X as a plain fraction of the frame's own width, Y as a plain fraction of
    its own height, centered on 0 (confirmed by how RectangleMask/EllipseMask
    store their Center/Width/Height: e.g. Center={0.44,0.5} for a 1920x1080
    frame -- no aspect-ratio correction folded in anywhere). This is exactly
    the "0-1, but X and Y scale by different reference lengths since the
    resolution usually isn't square" behavior -- each axis needs its OWN
    comp dimension as the scale reference, not a shared one.
    min_x/min_y is the SVG viewBox's own origin (viewBox="10 20 100 100"
    doesn't start at 0,0) -- centering on plain svg_w/svg_h without this
    offset would shift everything by however far the viewBox is from the
    origin.

    scale (see compute_svg_scale) multiplies the SVG-space offset before
    that per-axis normalization, so one uniform factor still comes out
    undistorted on a non-square frame: the differing divisors are exactly
    what cancels the frame's own aspect."""
    cx = min_x + svg_w / 2.0
    cy = min_y + svg_h / 2.0

    def to_fusion_point(x, y):
        return ((x - cx) * scale / comp_w, (cy - y) * scale / comp_h)

    def to_fusion_vector(dx, dy):
        # Bezier handles are offsets in SVG space, so they take the same
        # scale as the anchors do -- scaling anchors alone would keep the
        # old curvature on new-sized geometry and warp every curve.
        return (dx * scale / comp_w, -dy * scale / comp_h)

    return to_fusion_point, to_fusion_vector


def make_lottie_converter(comp_w, comp_h, scale=1.0):
    """Lottie's own analog of make_converter, deliberately NOT a reuse of
    it -- make_converter's centering math is SVG-viewBox-specific (an
    explicit min_x/min_y origin offset), which Lottie has no equivalent
    of. A Lottie shape's local coordinate space (post static <tr> bake,
    pre its own layer ks -- see lottie_layer_to_group_node) should map
    (0, 0) straight onto Fusion's own frame-centered (0, 0), since that's
    the point the layer's wrapping pivot/Transform gets built around, not
    onto some recentered composition origin.

    Same per-axis Y-flip convention as to_fusion_point/to_fusion_vector
    above (Lottie, like SVG, is Y-down from the top; Fusion is Y-up) --
    just without the centering term, since there's no separate origin to
    subtract here."""
    def to_point(x, y):
        return (x * scale / comp_w, -y * scale / comp_h)

    def to_vector(dx, dy):
        return (dx * scale / comp_w, -dy * scale / comp_h)

    return to_point, to_vector


def subpath_to_polyline_lua(sp, to_point, to_vector, force_closed=None):
    """force_closed overrides sp.closed (whether the SVG source actually had
    a Z/z on this subpath) -- needed because "closed" means something
    different for a fill mask than for a stroke. A FILL always implicitly
    closes every subpath regardless of Z (that's the SVG fill rule itself:
    Z only affects whether a STROKE gets a joined corner or two open end
    caps at the start point) -- confirmed against native import, which
    writes Closed=true on every fill mask it emits even for subpaths with no
    Z at all. Leaving this at sp.closed for a fill mask would leave an open gap between the last and first point on any
    Z-less subpath, which Fusion does NOT auto-close for masking -- exactly
    the kind of arc-heavy multi-subpath path (drawn via a sequence of bare
    'M ... M ...' segments, no Z) that renders as a visibly corrupted icon
    (e.g. a globe wireframe). A stroke mask, by contrast, genuinely needs
    the real sp.closed (an open stroke gets end caps, a closed one doesn't),
    so build_shape_lua passes force_closed=True only for the fill chain.

    ALL FOUR handle fields (LX/LY/RX/RY) are always written, explicitly zero
    when that side of the anchor has no control point -- never omitted. This
    is not redundancy: Fusion INFERS a missing handle rather than defaulting
    it to zero, and what it infers depends on where the anchor sits. Measured
    on two masks that are the same SVG shape traced in opposite directions
    (so the same edge is an interior segment in one and the CLOSING
    wraparound segment in the other): on the interior segment the omitted
    handles came back as (0, 0), but on the closing segment they came back as
    the exact NEGATION of the opposite handle -- i.e. Fusion mirrored the
    other side to make the point smooth. A straight edge (an SVG H/V/L
    command, which legitimately has no control point) therefore rendered as
    a curve whenever it happened to be the closing segment, bulging out into
    a teardrop/petal. Writing the zeros explicitly leaves Fusion nothing to
    infer. Note this is invisible to any external rasterization check, which
    naturally treats an absent handle as zero -- i.e. renders what the file
    MEANS rather than what Fusion does with it -- so it can only be caught by
    dumping the masks back out of Fusion and comparing handles."""
    lines = []
    for a in sp.anchors:
        x, y = to_point(a['x'], a['y'])
        if a['in_cp'] is not None:
            dx, dy = a['in_cp'][0] - a['x'], a['in_cp'][1] - a['y']
            lx, ly = to_vector(dx, dy)
        else:
            lx, ly = 0.0, 0.0
        if a['out_cp'] is not None:
            dx, dy = a['out_cp'][0] - a['x'], a['out_cp'][1] - a['y']
            rx, ry = to_vector(dx, dy)
        else:
            rx, ry = 0.0, 0.0
        fields = [f"X = {x!r}", f"Y = {y!r}",
                  f"LX = {lx!r}", f"LY = {ly!r}",
                  f"RX = {rx!r}", f"RY = {ry!r}"]
        if a['in_cp'] is None and a['out_cp'] is None:
            fields.append("Linear = true")
        lines.append("{ " + ", ".join(fields) + " }")
    is_closed = sp.closed if force_closed is None else force_closed
    closed = "true" if is_closed else "false"
    return "Polyline { Closed = %s, Points = { %s } }" % (closed, ", ".join(lines))


class LuaNameAllocator:
    """Keeps generated Fusion tool names unique within one import run.

    suffix is appended to EVERY name, including ones nested inside this
    file's own wrapper GroupOperator. Leaving inner names plain
    (Shape_1_Mask, not Shape_1_Mask_a1b2c3) on the theory that the group
    itself is their namespace doesn't work: when several files' groups are
    pasted together in one clipboard payload (see build_svgs_to_lua),
    Fusion's paste parser doesn't scope SourceOp resolution to each nested
    group's own Tools table, so two different files' same-named inner
    tools get cross-wired to each other. The suffix also keeps
    a second paste of the same (or same-named) file from colliding with
    the first one's leftovers, and makes it obvious which import a given
    node came from."""
    _RESERVED = {
        "and", "break", "do", "else", "elseif", "end", "false", "for",
        "function", "goto", "if", "in", "local", "nil", "not", "or",
        "repeat", "return", "then", "true", "until", "while",
    }

    def __init__(self, suffix=""):
        self.used = set()
        self.suffix = suffix

    def safe_name(self, raw, fallback):
        name = re.sub(r'[^A-Za-z0-9_]', '_', raw or fallback)
        if self.suffix:
            name = f"{name}_{self.suffix}"
        if not name or name[0].isdigit():
            name = "_" + name
        if name.lower() in self._RESERVED:
            name = name + "_"
        base, n = name, 1
        while name in self.used:
            n += 1
            name = f"{base}_{n}"
        self.used.add(name)
        return name


# CAREFUL: these are in the unit the PASTE TEXT's ViewInfo Pos uses, which
# is NOT the same abstract-grid unit Fusion's own node-graph scripting uses
# for flow.SetPos() (single digits, ~1 unit per node) -- these run in the
# hundreds. Measured off real dumps: native import lays its nodes out on an
# exactly regular grid of 110 across by 33 down, and a node is ~24 units
# tall (the constant Fusion itself stores in GroupInfo Size), so native's 33
# leaves only about a 9-unit gap -- correct but tight.
#
# A single fixed value, not a user-facing spacing choice: node sprawl is
# prevented by the flow-row tracking (see BuiltGraph.output_flow's own
# comment), not by spacing. Exactly double native's own grid -- "two
# spaces" between adjacent nodes instead of native's one -- comfortably
# roomier.
SVG_X_SPACING = 110 * 2
SVG_Y_SPACING = 33 * 2

# tools/output_name are what gets emitted; slots is how much room this
# subtree took on the stack axis, and output_slot is the slot its OUTPUT node
# actually landed on -- which is what lets a consumer Merge line itself up
# with its input instead of guessing (see build_group_lua). output_source is
# the OUTPUT NODE'S OWN pin name a consumer must use to read it -- "Output"
# for an ordinary tool, but "Output1" for a GroupOperator (see its own
# Outputs = { Output1 = InstanceOutput { ... } } in build_group_lua's
# group_lua template -- that's the pin it actually exposes, confirmed
# against a real native dump). A Merge (or a group's own Outputs mapping)
# that reads a GroupOperator via a hardcoded "Output" instead of "Output1"
# doesn't error -- Fusion's paste parser just can't resolve the connection
# and silently drops that one Input, leaving a Merge with a missing
# Background/Foreground and nothing rendering downstream of it. Defaults to
# "Output" since only build_group_lua's own wrap_groups=True return ever
# produces a Group.
#
# output_flow is the FLOW ROW (see PastePositions) the output node actually
# landed on -- added alongside the per-shape/whole-graphic pivot Transforms
# (see _shape_pivot_point/_weighted_pivot_point) specifically so a caller
# combining two BuiltGraphs can claim ONE ROW PAST whatever this one's own
# tail really used, instead of assuming a fixed "+1 from depth" offset.
# That assumption is wrong for anything whose own internal chain runs
# deeper than expected (a shape with a drop shadow or inner shadow, and
# every shape's own pivot too) -- the assumed row collides with the
# child's own true last row, and PastePositions.claim's collision recovery
# masks it by drifting sideways on the STACK axis instead, so a handful of
# colliding rows turns into shapes sprawling arbitrarily far apart with
# long diagonal wires between them. Defaults to 0 since it's only
# ever read when output_name is not None.
BuiltGraph = collections.namedtuple("BuiltGraph", "tools output_name slots output_slot output_source output_flow")
BuiltGraph.__new__.__defaults__ = ("Output", 0)


class PastePositions:
    """Turns an abstract (flow_level, stack_slot) grid reference into a
    concrete Flow-view (x, y) for the paste text.

    flow_level counts steps downstream (sources -> combined result), including
    the negative offsets a shape's own subpath-mask chain uses to stack above
    its Background (see build_shape_lua); stack_slot counts places across,
    one per sibling (shape, group, or merge). Which physical axis each lands
    on flips with orientation, so callers only ever think in flow/stack and
    never in x/y. stack_slot is the dominant axis in practice (see claim()
    below), so orientation="horizontal" puts it on X and flow on Y,
    "vertical" the reverse -- flow on X, stack down Y.

    stack_slot is a SLOT INDEX, not a shape index -- every node consumes one
    slot. Counting shapes instead and giving each a fixed row would
    silently overlap them: one shape occupies (subpaths + 1) nodes, so a
    6-subpath shape needs 7 rows' worth of space, and with only one its
    tail lands on top of the next shape (uneven Y gaps such as 30/50/60/140
    where native's are a uniform 33)."""

    def __init__(self, x_spacing, y_spacing, orientation, origin=(0.0, 0.0)):
        self.x_spacing = x_spacing
        self.y_spacing = y_spacing
        self.orientation = orientation
        self.origin_x, self.origin_y = origin
        self._taken = set()
        self._min_x = self.origin_x

    def claim(self, flow_level, stack_slot, flow_steps=0):
        """Reserves one grid cell and returns ((x, y), actual_stack_slot).

        Callers pass the slot they'd LIKE. If it's already held, this
        searches outward along the stack axis (+1, -1, +2, ...) for the
        nearest free cell, and returns whichever slot it settled on. Nodes aren't handed
        strictly increasing slots -- Merges deliberately ask for the slot
        their Foreground input is already on, to keep that connection
        straight -- so two independently-placed nodes CAN prefer the same
        cell. Ungrouped mode reaches it concretely: a dissolved nested
        group's Merge and an outer Merge share a flow column and can both
        derive their slot from the same upstream node. Tracking occupancy
        here is what keeps "nothing overlaps" a structural guarantee rather
        than something that happens to hold for the files tested."""
        flow = flow_level + flow_steps
        slot = stack_slot
        k = 0
        while (flow, slot) in self._taken:
            k += 1
            for cand in (stack_slot + k, stack_slot - k):
                if (flow, cand) not in self._taken:
                    slot = cand
                    break
        self._taken.add((flow, slot))

        # stack_slot is what actually reads as the dominant spread direction
        # in practice -- most SVGs have little or no <g> nesting, so
        # flow_level stays at (or near) 0 for nearly everything, and a
        # shape's own subpath-mask chain advances stack_slot, not
        # flow_level. So "Horizontal" needs stack on X (reads left-to-right)
        # with flow as the minor Y axis, and "Vertical" the reverse -- flow
        # on X, stack down the dominant Y axis. Mapping flow -> the SAME
        # axis regardless of orientation would put the
        # dominant stack axis on Y for "Horizontal" and on X for
        # "Vertical" -- exactly backwards from what those labels promise,
        # pasting a shape's mask chain as a wide row under "Vertical"
        # instead of a clean top-down column.
        if self.orientation == "horizontal":
            x, y = slot * self.x_spacing, flow * self.y_spacing
        else:
            x, y = flow * self.x_spacing, slot * self.y_spacing
        x += self.origin_x
        y += self.origin_y
        if x < self._min_x:
            self._min_x = x
        return (x, y), slot

    def min_x_used(self):
        """Leftmost X any claimed cell has reached so far (origin_x if
        nothing's been claimed yet). Under "vertical" orientation, flow_level
        (and therefore X - see claim() above) goes negative for a shape's own
        subpath-mask chain, and how far left that reaches depends on that
        ONE shape's own subpath count, not on how many shapes the file has -
        so a fixed offset from origin isn't safe; the Master Controller needs
        to know how far this file's own layout actually went."""
        return self._min_x


def build_mask_tool_lua(mask_name, sp, to_point, to_vector, comp_w, comp_h, pos,
                         invert=False, effect_mask_source=None, border_width=None, force_closed=None,
                         level=None, soft_edge=None, paint_mode=None):
    """comp_w/comp_h aren't used for MaskWidth/MaskHeight below -- confirmed
    against a real native-import dump that Fusion's own importer always
    leaves those at a fixed 320x240 (a legacy default baked into
    PolylineMask) regardless of the comp's actual resolution, so it clearly
    isn't tied to point normalization. comp_w/comp_h are still what
    to_point/to_vector (already built from them) actually use for that.

    invert / effect_mask_source: how a compound path's subpaths combine --
    confirmed against real native-import dumps. NOT via Polyline2 (that
    field is present-but-disabled on every mask regardless of shape,
    including ones with no hole at all, so it says nothing about holes).
    Each subpath after the first gets its own
    PolylineMask with PaintMode=Invert and its EffectMask fed from the
    previous mask in the chain; see build_shape_lua for why that chain is
    all the hole handling this needs.

    border_width: set (with Solid=0) to turn this mask into a STROKE
    (border-only) mask instead of a solid fill mask -- confirmed against a
    real native-import dump of a stroked path, which emits exactly these two
    extra inputs and nothing else different. Native's own BorderWidth,
    0.0038642561983471078, times its SVG's own width (484px) reproduces its
    stroke-width (1.8703px) exactly, i.e. native's convention is
    stroke_width_px / reference_width_px -- see build_shape_lua for how that
    generalizes to comp size + the Timeline scale factor.

    JoinStyle/MiterLimit/CapStyle are set to match native import, which
    writes all three on every mask it creates (confirmed: 21/21 masks in a
    native dump, always these same values). They're the SVG spec's own
    stroke defaults -- stroke-linejoin: miter, stroke-miterlimit: 4,
    stroke-linecap: butt -- so native is evidently transcribing the SVG's
    computed stroke style onto Fusion's mask-border model whether or not
    the path is actually stroked, rather than reading the SVG's ACTUAL
    stroke-linejoin/stroke-linecap (round/bevel/square aren't confirmed
    Fusion enum values -- see the module docstring). With BorderWidth at its
    default 0 they have no effect on a solid fill, so these are here for
    fidelity even when this particular mask isn't a stroke. DrawMode
    likewise matches native: Fusion's own default, ClickAppend, leaves a
    finished imported shape primed to ADD points on the next viewer click,
    where ModifyOnly only edits existing ones.

    level: a compound path's Fusion mask has its own "Level" input --
    confirmed against a real native-import dump that a gradient-filled
    shape's own fill-opacity/stroke-opacity is applied HERE, on the
    mask feeding the Background, rather than baked into the Background's
    own Gradient color stops (see _build_gradient_background_lua for the
    stops' own, separate premultiplied-alpha convention). Only ever set on
    the LAST mask in a chain (the one actually wired as the Background's
    EffectMask) -- setting it earlier would scale a mask's alpha before
    it's XOR/union-combined with the rest of the chain, corrupting the
    even-odd/union computation instead of just dimming the final result.

    soft_edge: a PolylineMask's own SoftEdge input, confirmed by field name
    and raw-float storage against a real dump (typed 0.0646 into the
    Inspector, dumped Value=0.0646 back -- no hidden UI-to-storage scaling).
    Used to approximate an SVG feGaussianBlur (see parse_filter_defs) --
    exact for a solid-colour fill, since blurring a UNIFORM color's alpha
    edge is mathematically identical to blurring the whole shape
    (blurred_color = color * blurred_alpha when color doesn't vary). There's
    no native Fusion SVG-filter import to dump this against (native ignores
    filters), so the stdDeviation-to-SoftEdge scale
    factor (reusing to_vector's own X-axis convention, same as border_width
    below) is calibrated against the browser-rendered SVG directly rather
    than a native dump -- the correct ground truth here, since there's no
    Fusion convention to match in the first place.

    paint_mode: explicit PaintMode override for the SVG-<mask>-as-clip case
    (see build_group_lua/_build_mask_chain's initial_effect_mask) -- pass
    "Multiply" there, NOT left at Fusion's own default. Per Fusion's
    documented mask Apply Modes, the default combine mode for an EffectMask
    feeding a mask tool is "Merge" (union-like -- comparable to Maximum),
    not intersect, so leaving PaintMode unset here would make the
    receiving mask balloon out to the UNION of its own shape and the clip,
    not the intersection: a shape's own mask, wired to a clip's EffectMask
    with no PaintMode, shows the clip's entire shape rather than just the
    overlap. "Multiply" multiplies
    the two masks' alpha together, the correct operation for intersecting
    two ANTIALIASED (not just binary) alpha masks -- "Minimum" would be a
    cruder pixel-wise min. Mutually exclusive with invert in practice (this
    is only ever set on a chain's FIRST subpath, which invert_chain never
    marks invert=True regardless)."""
    poly_lua = subpath_to_polyline_lua(sp, to_point, to_vector, force_closed=force_closed)
    return _build_mask_tool_lua_from_poly(mask_name, poly_lua, pos, invert=invert,
                                           effect_mask_source=effect_mask_source, border_width=border_width,
                                           level=level, soft_edge=soft_edge, paint_mode=paint_mode)


def _build_mask_tool_lua_from_poly(mask_name, poly_lua, pos, invert=False, effect_mask_source=None,
                                    border_width=None, level=None, soft_edge=None, paint_mode=None):
    """The template half of build_mask_tool_lua, factored out so a caller
    that already HAS a finished 'Polyline { ... }' literal can reuse the
    exact same native-import-matched mask template (JoinStyle/MiterLimit/
    MaskWidth/etc, see build_mask_tool_lua's own docstring) without needing
    a Subpath object or a to_point/to_vector pair to build one from."""
    extra_lines = ""
    if level is not None:
        extra_lines += f'            Level = Input {{ Value = {level!r} }},\n'
    if soft_edge is not None:
        extra_lines += f'            SoftEdge = Input {{ Value = {soft_edge!r} }},\n'
    if invert:
        extra_lines += '            PaintMode = Input { Value = FuID { "Invert" } },\n'
    elif paint_mode:
        extra_lines += f'            PaintMode = Input {{ Value = FuID {{ "{paint_mode}" }} }},\n'
    if border_width is not None:
        extra_lines += f'            BorderWidth = Input {{ Value = {border_width!r} }},\n'
        extra_lines += '            Solid = Input { Value = 0 },\n'
    if effect_mask_source:
        extra_lines += f'            EffectMask = Input {{ SourceOp = "{effect_mask_source}", Source = "Mask" }},\n'
    return f"""{mask_name} = PolylineMask {{
        DrawMode = "ModifyOnly",
        Inputs = {{
            JoinStyle = Input {{ Value = 2 }},
            MiterLimit = Input {{ Value = 4 }},
            CapStyle = Input {{ Value = 0 }},
            MaskWidth = Input {{ Value = 320.0 }},
            MaskHeight = Input {{ Value = 240.0 }},
            PixelAspect = Input {{ Value = {{ 1, 1 }} }},
            Polyline = Input {{ Value = {poly_lua} }},
{extra_lines}        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def _build_background_lua(name, comp_w, comp_h, r, g, b, a, effect_mask_source, pos, color_exprs=None,
                            alpha_source_op=None):
    """color_exprs, when given, is an (r_expr, g_expr, b_expr) tuple of Lua
    expression strings -- TopLeftRed/Green/Blue are written as
    Input { Expression = ... } instead of Input { Value = ... } in that
    case, r/g/b themselves going unused (see
    ColorConsolidationRegistry/build_color_master_controller_lua: this is
    how a shape's own fill/stroke color gets wired to a shared Master
    Controller swatch instead of staying a literal baked-in value).
    TopLeftAlpha is never consolidated -- always either a literal Value or
    alpha_source_op's own SourceOp -- so the same color can still be used
    at different (possibly independently-animated) opacities.

    An Input with a live Expression carries no separate Value field at all
    in a real dump (e.g. a WriteLength field: just
    __ctor/__flags/Expression, no Value) -- so
    that's what gets written here, not both.

    alpha_source_op: for the Lottie per-shape animated-opacity path (see
    build_shape_lua's own layer_opacity_anim handling) -- the NAME of an
    already-built BezierSpline tool to wire TopLeftAlpha's Input to
    (Fusion always routes an animated scalar through a separate tool, per
    a real animated-Transform dump -- never inline keyframes), or None (every
    static caller) to keep TopLeftAlpha a plain literal Value using the
    real `a`."""
    if color_exprs is not None:
        r_expr, g_expr, b_expr = color_exprs
        red_line = f'Input {{ Expression = "{r_expr}" }}'
        green_line = f'Input {{ Expression = "{g_expr}" }}'
        blue_line = f'Input {{ Expression = "{b_expr}" }}'
    else:
        red_line = f'Input {{ Value = {r!r} }}'
        green_line = f'Input {{ Value = {g!r} }}'
        blue_line = f'Input {{ Value = {b!r} }}'
    if alpha_source_op is None:
        alpha_line = f'Input {{ Value = {a!r} }}'
    else:
        alpha_line = f'Input {{ SourceOp = "{alpha_source_op}", Source = "Value" }}'
    return f"""{name} = Background {{
        NameSet = true,
        Inputs = {{
            Width = Input {{ Value = {comp_w!r} }},
            Height = Input {{ Value = {comp_h!r} }},
            TopLeftRed = {red_line},
            TopLeftGreen = {green_line},
            TopLeftBlue = {blue_line},
            TopLeftAlpha = {alpha_line},
            EffectMask = Input {{ SourceOp = "{effect_mask_source}", Source = "Mask" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def _build_gradient_background_lua(name, comp_w, comp_h, gradient, to_point, effect_mask_source, pos):
    """gradient is a ShapeNode.fill_gradient/stroke_gradient dict (see
    resolve_gradient) -- its points are still in absolute SVG user-space,
    same as a shape's own path anchors.

    Start/End do NOT use to_point()'s own convention directly, even though
    to_point() is what's available here -- to_point() implements
    PolylineMask's point convention, centered on (0, 0) at frame middle
    (see make_converter). A Background's own position-style fields (this
    module's own prior-confirmed example: RectangleMask/EllipseMask's
    Center, stored as e.g. {0.44, 0.5} for a 1920x1080 frame -- frame
    middle at (0.5, 0.5), not (0, 0)) use a DIFFERENT convention that's
    otherwise identical -- X/Y still normalized independently by comp
    width/height, just shifted so the frame's middle sits at 0.5 instead
    of 0. Using PolylineMask's centered convention directly for Start/End
    renders gradients with their start/end points positioned way off from
    where the SVG actually places them, so that +0.5 shift on both
    axes is applied via to_gradient_point below rather than to_point
    directly. Distance(Start, End) -- what sets a radial gradient's radius
    -- is unaffected either way, since both points get the same shift.

    Field names/shapes (Type/GradientType/Start/End/Gradient.Colors) are
    taken from a real native-import SaveSettings() dump of a
    gradient-filled shape.

    Gradient.Colors stores each stop's RGB PREMULTIPLIED by that stop's OWN
    alpha (stop-opacity only -- NOT the shape's overall fill-opacity/
    stroke-opacity, which the caller applies separately via the mask's own
    Level input; see build_mask_tool_lua), with the alpha channel left as
    that same straight stop alpha. Confirmed against a real native dump of
    a translucent gradient (stop-opacity=0.67 on #84F8FF): native stored
    (0.347, 0.652, 0.67, 0.67), i.e. each of R/G/B independently multiplied
    by 0.67, not the straight (0.518, 0.973, 1.0, 0.67) an unpremultiplied
    reading of the same stop would give. Getting this wrong doesn't error
    -- it just quietly desaturates/darkens every translucent stop and
    leaves the shape's own overall opacity entirely unapplied, since without
    the caller's separate Level fix that opacity had nowhere else to go."""
    def to_gradient_point(x, y):
        px, py = to_point(x, y)
        return px + 0.5, py + 0.5

    start = to_gradient_point(*gradient["p0" if gradient["kind"] == "linear" else "center"])
    end = to_gradient_point(*gradient["p1" if gradient["kind"] == "linear" else "edge"])
    gradient_type = "Linear" if gradient["kind"] == "linear" else "Radial"
    stops_lua = ", ".join(
        f"[{offset!r}] = {{ {r * a!r}, {g * a!r}, {b * a!r}, {a!r} }}"
        for offset, r, g, b, a in gradient["stops"])
    return f"""{name} = Background {{
        NameSet = true,
        Inputs = {{
            Width = Input {{ Value = {comp_w!r} }},
            Height = Input {{ Value = {comp_h!r} }},
            Type = Input {{ Value = FuID {{ "Gradient" }} }},
            GradientType = Input {{ Value = FuID {{ "{gradient_type}" }} }},
            Start = Input {{ Value = {{ {start[0]!r}, {start[1]!r} }} }},
            End = Input {{ Value = {{ {end[0]!r}, {end[1]!r} }} }},
            Gradient = Input {{ Value = Gradient {{ Colors = {{ {stops_lua} }} }} }},
            EffectMask = Input {{ SourceOp = "{effect_mask_source}", Source = "Mask" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def _build_mask_chain(subpaths, to_point, to_vector, comp_w, comp_h,
                       end_flow_level, stack_slot, names, positions,
                       invert_chain, border_width, name_for, force_closed=None, level=None,
                       soft_edge=None, initial_effect_mask=None, initial_paint_mode="Multiply",
                       initial_invert=False):
    """Builds one mask per subpath, chained via EffectMask, ending at
    end_flow_level with the masks stacked at negative flow offsets above it
    (see build_shape_lua). Shared by the fill chain (invert_chain=True, the
    even-odd rule) and the stroke chain (invert_chain=False -- a stroke
    unions each subpath's outline instead).

    level (see build_mask_tool_lua) is only ever applied to the LAST mask
    in the chain -- the one actually wired as EffectMask on whatever
    Background this chain feeds. soft_edge, by contrast, is applied to
    EVERY mask in the chain, same as border_width -- a blur softens every
    subpath's own boundary, not just the chain's final combined edge.

    initial_effect_mask: an EXTERNAL mask (e.g. an SVG <mask>'s own clip
    chain -- see build_group_lua -- or an inner shadow's own undisplaced
    silhouette/clipped-shifted copy -- see _build_inner_shadow_layer_lua)
    to combine into just the FIRST subpath's own mask, seeded as the
    starting prev_mask below rather than passed to every iteration. Every
    later subpath in this chain keeps chaining off the previous mask IN
    THIS CHAIN exactly as before (untouched) -- only the base of the chain
    also combines with the external mask.

    initial_paint_mode defaults to "Multiply" (AND/intersect -- the SVG
    <mask>-as-clip case, and the inner-shadow case's own middle "clip"
    stage) -- see build_mask_tool_lua's paint_mode docstring for why the
    default isn't simply omitting PaintMode (Fusion's own default there is
    "Merge", union-like, which gives the wrong result).

    initial_invert: set instead of relying on initial_paint_mode="Subtract"
    when the caller needs "this chain's own shape MINUS initial_effect_mask"
    and initial_effect_mask is ALREADY a subset of that shape (true for the
    inner-shadow band -- see _build_inner_shadow_layer_lua) -- Fusion's
    own "Subtract" doesn't behave as a simple
    input-minus-new either (a mask fed via Subtract comes out always empty
    with the operands assigned the only way the tool's own shape/EffectMask
    roles allow). XOR against a SUBSET has exactly the same effect as minus
    (A xor B == A - B when B subset-of A) and reuses the "Invert" PaintMode
    already proven correct throughout this file's own hole-punching chains,
    rather than trusting a second unverified combine mode."""
    n = len(subpaths)
    tools = []
    prev_mask = initial_effect_mask
    for i, sp in enumerate(subpaths):
        mask_name = names.safe_name(name_for(i), "Mask")
        mask_pos, _ = positions.claim(end_flow_level - (n - i), stack_slot)
        tools.append(build_mask_tool_lua(mask_name, sp, to_point, to_vector, comp_w, comp_h,
                                          mask_pos,
                                          invert=(invert_chain and i > 0) or (i == 0 and initial_invert),
                                          effect_mask_source=prev_mask, border_width=border_width,
                                          force_closed=force_closed,
                                          level=level if i == n - 1 else None,
                                          soft_edge=soft_edge,
                                          paint_mode=(initial_paint_mode if i == 0 and initial_effect_mask is not None
                                                      else None)))
        prev_mask = mask_name
    return tools, prev_mask


# ==========================================================================
# "Consolidate Colors": every distinct solid fill/stroke color (RGB, not
# RGBA -- see ColorConsolidationRegistry's own docstring) used anywhere in
# the file -- whether it's on one shape or shared by many -- gets its own
# swatch on one Fusion Custom tool ("Master Controller"), and every shape
# using that color has its own TopLeftRed/Green/Blue rewired to an
# Expression referencing it instead of a baked-in literal -- so recoloring
# later (or a color shared by several shapes) means editing one swatch, not
# hunting down every Background by hand.
#
# Field names/shape for the Custom tool + its UserControls come from a real
# hand-built dump of a Custom tool with two
# single-channel "ColorControl"-styled Number inputs -- see
# build_color_master_controller_lua's own docstring for exactly what's
# confirmed vs inferred from that dump.
# ==========================================================================
def _collect_color_usage(node, usage):
    """Recursively walks an already-built ShapeNode/GroupNode tree (see
    walk()), recording every solid (non-gradient) fill/stroke color's exact
    (r, g, b) -- rounded to 6 decimals purely to dodge float-repr noise, not
    because two DIFFERENT hex codes should ever fuzzy-match -- into
    usage[color] = [(shape, "fill"|"stroke"), ...]. Gradients are excluded
    entirely; there's no single color to consolidate for those."""
    if isinstance(node, ShapeNode):
        if node.has_fill and node.fill_gradient is None and node.color is not None:
            key = tuple(round(c, 6) for c in node.color[:3])
            usage.setdefault(key, []).append((node, "fill"))
        if node.has_stroke and node.stroke_gradient is None and node.stroke_color is not None:
            key = tuple(round(c, 6) for c in node.stroke_color[:3])
            usage.setdefault(key, []).append((node, "stroke"))
    else:
        for child in node.children:
            _collect_color_usage(child, usage)


class ColorConsolidationRegistry:
    """Built once per SVG import when "Consolidate Colors" is on (see
    build_svg_tools), BEFORE build_group_lua walks the tree to generate
    tools -- maps each ShapeNode's own (fill|stroke) color to the Lua
    Expression that should replace its literal TopLeftRed/Green/Blue values
    (see build_shape_lua/_build_background_lua's own color_exprs param), so
    every shape using that exact color -- even if it's the only one -- can
    be repainted by editing ONE Master Controller swatch instead of the
    shape itself.

    Deliberately RGB-only, not RGBA -- "the exact same hex code" is an
    RGB concept; alpha is left as each shape's own value
    so the same brand color used at different opacities still consolidates
    without forcing identical transparency. Also deliberately doesn't touch
    drop-shadow/inner-shadow colors -- out of scope, those are a separate
    concept from a shape's own fill/stroke paint."""

    def __init__(self, master_tool_name):
        self.master_tool_name = master_tool_name
        self.swatches = []
        self._expr_by_shape_channel = {}

    def build(self, top):
        usage = {}
        _collect_color_usage(top, usage)
        for color, sites in usage.items():
            index = len(self.swatches) + 1
            hex_label = rgb_to_hex(tuple(round(c * 255) for c in color)).lstrip('#').upper()
            r_ctrl, g_ctrl, b_ctrl = f"Swatch{index}_R", f"Swatch{index}_G", f"Swatch{index}_B"
            self.swatches.append({"index": index, "color": color, "hex_label": hex_label,
                                   "r_ctrl": r_ctrl, "g_ctrl": g_ctrl, "b_ctrl": b_ctrl})
            r_expr = f"{self.master_tool_name}:GetValue('{r_ctrl}', time)"
            g_expr = f"{self.master_tool_name}:GetValue('{g_ctrl}', time)"
            b_expr = f"{self.master_tool_name}:GetValue('{b_ctrl}', time)"
            for shape, channel in sites:
                self._expr_by_shape_channel[(id(shape), channel)] = (r_expr, g_expr, b_expr)

    def lookup(self, shape, channel):
        """Returns an (r_expr, g_expr, b_expr) tuple ready for
        _build_background_lua's own color_exprs param, or None if this
        shape/channel has no solid color at all (no fill/stroke, or a
        gradient) -- every solid color gets a swatch now, shared or not."""
        return self._expr_by_shape_channel.get((id(shape), channel))

    def has_swatches(self):
        return bool(self.swatches)


# Every fresh Custom tool ships with 8 placeholder "Number" and 4 placeholder
# "Point" controls on its own Controls tab (separate from the LUTIn1-4
# boilerplate, which lives on a different tab entirely) -- clutter the
# generated swatches have nothing to do with. Per a real dump, setting each
# ShowNumberN/ShowPointN
# to 0.0 (unchecking "Name for Number N"/"Name for Point N" in the tool's own
# Config tab) hides them, so a pasted Master Controller starts tidy without
# needing that manual cleanup step every time.
_TIDY_CUSTOM_TOOL_INPUTS_LUA = """            NumberControls = Input { Value = 1.0 },
            ShowNumber1 = Input { Value = 0.0 },
            ShowNumber2 = Input { Value = 0.0 },
            ShowNumber3 = Input { Value = 0.0 },
            ShowNumber4 = Input { Value = 0.0 },
            ShowNumber5 = Input { Value = 0.0 },
            ShowNumber6 = Input { Value = 0.0 },
            ShowNumber7 = Input { Value = 0.0 },
            ShowNumber8 = Input { Value = 0.0 },
            PointControls = Input { Value = 1.0 },
            ShowPoint1 = Input { Value = 0.0 },
            ShowPoint2 = Input { Value = 0.0 },
            ShowPoint3 = Input { Value = 0.0 },
            ShowPoint4 = Input { Value = 0.0 },"""


def build_color_master_controller_lua(master_tool_name, swatches, pos=(0.0, 0.0)):
    """Builds a Fusion Custom tool exposing one grouped R/G/B swatch per
    entry in `swatches` (see ColorConsolidationRegistry.build).

    Matches a real dump of a Custom tool with
    TWO genuine 3-channel Color controls added via Fusion's own native
    "Add Custom Control" menu (not hand-edited), both showing correctly
    under the tool's "Controls" tab: each channel's IC_ControlGroup matches
    across its own trio (Group 1 for the first swatch, Group 2 for the
    second) with IC_ControlID 0/1/2 for R/G/B. Every control also needs
    ICS_ControlPage = "Controls": without it the first swatch still lands
    on the "Controls" tab but every subsequent one falls back to a
    generic "User" tab instead. INP_MinScale/INP_MaxScale (0..1) are also copied from that
    same dump -- clamps the slider's own usable range to a sensible span for
    a color channel instead of Fusion's much wider numeric default.

    Every Custom tool's own built-in LUTIn1-4/LUTBezier boilerplate (present
    on every fresh Custom tool regardless of what user controls get added)
    is deliberately NOT reproduced here -- inert scaffolding, not part of
    what the generated swatches actually store."""
    inputs_lines = []
    controls_lines = []
    for sw in swatches:
        r, g, b = sw["color"]
        for channel, ctrl, val, control_id in (
                ("R", sw["r_ctrl"], r, 0), ("G", sw["g_ctrl"], g, 1), ("B", sw["b_ctrl"], b, 2)):
            inputs_lines.append(f'            {ctrl} = Input {{ Value = {val!r} }},')
            controls_lines.append(f"""            {ctrl} = {{
                LINKS_Name = "Swatch {sw['index']} (#{sw['hex_label']}) {channel}",
                LINKID_DataType = "Number",
                INPID_InputControl = "ColorControl",
                INP_Integer = false,
                INP_MinScale = 0.0,
                INP_MaxScale = 1.0,
                INP_SplineType = "Default",
                IC_ControlGroup = {sw['index']},
                IC_ControlID = {control_id},
                CLRC_ColorSpace = "HSV",
                CLRC_ShowWheel = false,
                CLRC_NoSliders = false,
                ICS_ControlPage = "Controls",
            }},""")
    return f"""{master_tool_name} = Custom {{
        NameSet = true,
        Inputs = ordered() {{
{_TIDY_CUSTOM_TOOL_INPUTS_LUA}
{chr(10).join(inputs_lines)}
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
        UserControls = ordered() {{
{chr(10).join(controls_lines)}
        }},
    }}"""


# ==========================================================================
# Per-shape pivot: every shape's own fill/stroke/shadow output gets wrapped
# in one Transform tool whose Pivot sits at that shape's own bounding-box
# center, instead of Fusion's own default (0.5, 0.5) -- the FRAME's center.
# Since a Background/Merge has no position/rotation/scale controls of its
# own, without this wrapper there's nowhere on a fill-only shape to even SET
# a pivot; an animator keyframing Angle/Size on whatever tool they found
# upstream would rotate/scale the piece around the frame's own middle (an
# orbit) instead of spinning it in place -- the classic Fusion gotcha this
# exists to avoid, mirroring what After Effects' own per-layer Anchor Point
# does automatically.
#
# Center is deliberately LEFT ALONE (never written here, so Fusion's own
# default applies) -- writing it causes visible ghosting/misregistration
# between shapes that should coincide exactly. Center and Pivot
# are NOT "where the pivot lands on screen" / "which point is
# the pivot" (a pair that would cancel out when equal) -- Center is its own
# absolute translation from frame-center, applied independently of Pivot
# even at Angle=0/Size=1.
# Every shape's true on-screen position already comes entirely from its own
# Polyline/mask geometry, so writing a non-default Center here would double-
# apply a second, shape-specific offset on top of that -- and since each
# shape's own bbox-center sits a slightly different distance from the
# frame's center, that offset would differ shape to shape, visibly pulling
# apart pieces (e.g. a colored fill and its own outline) that are only
# supposed to look like one piece. Pivot alone causes no such shift: at
# Angle=0/Size=1 rotating/scaling by identity doesn't move anything
# regardless of which point it's anchored on, so moving ONLY Pivot avoids
# the orbit-around-frame-center problem with no positional side
# effect -- leaving Center free for the animator to use.
#
# Confirmed field names/shape (Center and Pivot as genuinely independent
# Transform inputs) -- from a dump of a hand-built
# Transform with both moved off their own defaults purely to capture the
# structure (its specific VALUES weren't meaningful).
# ==========================================================================
def _shape_pivot_point(shape, to_point):
    """This shape's own bounding-box center (see _subpaths_bbox), converted
    into the SAME frame-normalized, (0.5, 0.5)-is-center convention
    Transform's Pivot input uses -- NOT to_point()'s own convention
    directly, which centers on (0, 0) instead (see make_converter). Exactly
    _build_gradient_background_lua's own to_gradient_point shift (+0.5 on
    both axes), reused here for the same reason: a shape's own geometry and
    a tool's position-style fields are two different conventions that are
    otherwise identical."""
    minx, miny, w, h = _subpaths_bbox(shape.subpaths)
    px, py = to_point(minx + w / 2.0, miny + h / 2.0)
    return px + 0.5, py + 0.5


def _build_transform_lua(name, pivot, input_source, pos):
    """pivot is an (x, y) tuple in the convention _shape_pivot_point
    returns -- written to Pivot ONLY. Center is deliberately NOT written at
    all here (see this section's own module comment for why leaving it at
    Fusion's own default, rather than matching it to Pivot, is what avoids
    shifting the shape). input_source is always read from "Output" -- every
    tool build_shape_lua could wrap this in (Background, the fill/stroke
    Merge, a shadow Merge) is a plain tool, never a GroupOperator, so
    there's no "Output1"-vs-"Output" ambiguity here the way build_merge_lua
    has to account for."""
    px, py = pivot
    return f"""{name} = Transform {{
        NameSet = true,
        Inputs = {{
            Pivot = Input {{ Value = {{ {px!r}, {py!r} }} }},
            Input = Input {{ SourceOp = "{input_source}", Source = "Output" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def _build_recentering_transform_lua(name, pivot, input_source, pos):
    """Like _build_transform_lua, but ALSO writes Center -- used ONLY for
    Lottie's own whole-graphic OverallPivot (see build_group_lua's
    is_root branch, recenter_overall_pivot=True), to move the assembled
    artwork's own area-weighted centroid to the Fusion frame's center. A
    Lottie composition's own subject is routinely NOT centered within its
    own comp -- other design elements this app doesn't import (e.g. a
    background/text/image layer, common in a real AE project) usually
    fill the rest of the frame, so preserving the source's exact in-comp
    position (what _build_transform_lua's own pivot-only convention
    already does correctly for SVG, which has no such "other elements"
    concern) would leave the imported subject visibly off-center. Explicitly NOT
    the SVG default -- confirmed only for Lottie's own is_root call.

    Center = {0.5, 0.5} is Fusion's own DEFAULT value for that input --
    per this file's own confirmed Center/Pivot module comment right
    before _shape_pivot_point, Center is an absolute translation FROM
    frame-center, entirely independent of Pivot even at Angle=0/Size=1.
    Writing the literal default there is a no-op shift, not a "move to
    center" (the result is unchanged from the non-recentering case).
    The actual translation
    needed to land a centroid currently sitting at (px, py) exactly on
    frame-center is (1 - px, 1 - py): shift = Center - (0.5, 0.5), so
    solving Center - (0.5, 0.5) = (0.5, 0.5) - (px, py) gives Center =
    (1 - px, 1 - py).

    Deliberately a SEPARATE function from _build_transform_lua
    (used everywhere else -- per-shape pivots, SVG's own whole-graphic
    pivot) rather than a new parameter on it, so the pivot-only callers
    never go through the Center-writing code path this Lottie-specific
    use case needs."""
    px, py = pivot
    cx, cy = 1.0 - px, 1.0 - py
    return f"""{name} = Transform {{
        NameSet = true,
        Inputs = {{
            Pivot = Input {{ Value = {{ {px!r}, {py!r} }} }},
            Center = Input {{ Value = {{ {cx!r}, {cy!r} }} }},
            Input = Input {{ SourceOp = "{input_source}", Source = "Output" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


# ==========================================================================
# Lottie animation -- real animated Fusion keyframes, modelled on a real
# "Dump Selected Node Settings" capture of a hand-built animated Transform.
# Structure:
#
#   - An animated `Center` (Point-type Input) is NOT two per-axis splines
#     -- it's `Center = Input { SourceOp = "<PolyPath>", Source =
#     "Position" }`. That PolyPath holds a `PolyLine` (one point per
#     position keyframe, in this file's own frame-normalized (0.5,0.5)-
#     center convention, each point's own LX/LY/RX/RY shaping the ON-
#     SCREEN path curve) and a `Displacement` Input wired to ANOTHER
#     separate BezierSpline (0..1 progress-over-time). Confirmed
#     Displacement isn't naive index/(count-1) -- it tracks actual spacing
#     between the path's own points (see _lottie_position_displacement_
#     specs).
#   - `Pivot` stayed a plain STATIC Value in the real dump even with
#     Center animated -- the animated export keeps Pivot always-static (the layer's
#     own anchor, resolved to its first/static value), matching this
#     file's existing static per-shape/whole-graphic pivots.
#   - Scalar Inputs (Angle/Size/Aspect, identical in all three; also
#     applied to TopLeftAlpha as the same mechanism, not a new
#     unconfirmed construct) each wire to their own
#     separately-named BezierSpline tool.
#   - BezierSpline.KeyFrames: keyed by frame number AS A STRING, each
#     entry holding the value plus LH/RH tangent handles as
#     {frame, value} (omitted on the first/last keyframe respectively),
#     and Flags = { Linear = true } only when BOTH bounding keyframes are
#     linear (handles then sit exactly on the straight line -- verified
#     algebraically against the real dump's own numbers).
#
# What's still an ASSUMPTION rather than dump-confirmed (flagged inline
# below): how Lottie's own "o"/"i" temporal ease fractions map onto
# Fusion's absolute LH/RH handles, and how a position keyframe's own
# spatial "to"/"ti" tangents map onto the PolyPath's own LX/LY/RX/RY.
# ==========================================================================
def _lottie_resolved_static(value_or_keyframes, scalar=False):
    """Resolves a LottieLayerAnimation field (already either a plain
    static value or a lottie_keyframes() list) down to ONE static value:
    as-is if never animated, or its first keyframe's own value if it was
    -- used for Pivot, which stays static even when Center/Angle/Size
    animate (see this section's own module comment), and for any other
    field this file doesn't animate. Shared by the static bake path
    (_bake_lottie_layer_pose) and the animated wrap path."""
    if not _lottie_prop_is_animated(value_or_keyframes):
        return value_or_keyframes
    val = value_or_keyframes[0][1]
    return val[0] if scalar and isinstance(val, list) else val


def _lottie_field_is_really_animated(value):
    """Collapses a Lottie a:1 property with only ONE keyframe (no actual
    motion -- sometimes exported anyway) down to "static", same treatment
    a genuinely non-animated property gets everywhere else in this file."""
    return _lottie_prop_is_animated(value) and len(value) >= 2


def _layer_has_transform_animation(anim):
    """True iff this layer's own position/scale/rotation carries any REAL
    animation -- deliberately excludes anchor (Pivot stays static
    regardless, tallied separately via note_approximated_animated_anchor)
    and opacity (threaded independently as layer_opacity_anim -- opacity
    alone never needs a wrapping Transform)."""
    return any(_lottie_field_is_really_animated(getattr(anim, f))
               for f in ("position", "scale", "rotation"))


_LOTTIE_EASE_EPS = 1e-4


def _lottie_ease_fraction(ease_dict, component=0):
    """Reads one Lottie "o"/"i" bezier-ease dict down to this property's
    OWN (x, y) fraction pair. Lottie allows both a single shared {x,y}
    (the whole multi-dimensional value eases together) and a per-
    component {x:[...], y:[...]} (each axis eased independently) --
    component selects which axis this call cares about, ignored for the
    shared shape. Returns None if ease_dict itself is None (no ease
    authored -- Lottie's own plain/linear convention)."""
    if ease_dict is None:
        return None
    fx, fy = ease_dict.get("x"), ease_dict.get("y")
    if isinstance(fx, list):
        fx = fx[component] if component < len(fx) else fx[0]
    if isinstance(fy, list):
        fy = fy[component] if component < len(fy) else fy[0]
    return float(fx), float(fy)


def _lottie_ease_segment(t0, v0, t1, v1, out_ease, in_ease, component=0):
    """One segment's Fusion-space (is_linear, rh, lh) -- rh/lh each an
    (frame, value) tuple, or a synthesized on-the-line placement when the
    segment collapses to Linear.

    out_ease/in_ease are BOTH read off the SEGMENT-START keyframe (kf i's
    own "o" AND "i" together describe the one segment from kf i to kf
    i+1 -- confirmed against a real exported file, layer3/gr[0]/tr.r in
    Shapes.json: keyframe 0 carries both "o" and "i", keyframe 1 -- the
    last, starting no further segment -- carries neither).

    ASSUMPTION (not independently dump-confirmed -- the Fusion-side LH/RH
    shape IS confirmed, this is about what Lottie's own "o"/"i" VALUES
    mean once you have them): they're already a literal 2-control-point
    cubic Bezier in (time-fraction, value-fraction) space, measured from
    the segment's own start -- not a normalized y=f(x) timing function
    needing inversion -- so converting to Fusion's absolute-frame/
    absolute-value LH/RH is a straight linear rescale. Not yet verified
    against a pasted, genuinely curved animation, so treat it with care
    for anything precision-sensitive.

    Absent o/i (Lottie's plain/no-ease convention) or a numerically
    collinear fraction (fx == fy on both handles, or a flat v0==v1
    segment with nothing to ease toward) both collapse to Linear -- a
    Linear-flagged segment still needs real handle values written
    (Flags.Linear is what actually drives the interpolation; the handles
    just need to sit ON the line), so a conventional 1/3-2/3 placement is
    synthesized for the absent case."""
    out_f = _lottie_ease_fraction(out_ease, component)
    in_f = _lottie_ease_fraction(in_ease, component)
    if out_f is None and in_f is None:
        fx_out, fy_out = 1.0 / 3.0, 1.0 / 3.0
        fx_in, fy_in = 2.0 / 3.0, 2.0 / 3.0
        is_linear = True
    else:
        fx_out, fy_out = out_f if out_f is not None else (1.0 / 3.0, 1.0 / 3.0)
        fx_in, fy_in = in_f if in_f is not None else (2.0 / 3.0, 2.0 / 3.0)
        is_linear = (v1 == v0) or (
            abs(fx_out - fy_out) < _LOTTIE_EASE_EPS and abs(fx_in - fy_in) < _LOTTIE_EASE_EPS)
    rh = (t0 + fx_out * (t1 - t0), v0 + fy_out * (v1 - v0))
    lh = (t0 + fx_in * (t1 - t0), v0 + fy_in * (v1 - v0))
    return is_linear, rh, lh


def _lottie_keyframe_specs(keyframes, value_fn, ease_component=0, force_linear=False,
                            unwrap_scalar=False, report=None, time_scale=1.0):
    """Converts one property's raw lottie_keyframes() list (7-tuples --
    see there) into [(frame, value, rh_or_None, lh_or_None, linear), ...]
    ready for _lua_keyframes_table_lua -- shared by every animated scalar
    Input (Angle/Size/Aspect/TopLeftAlpha) and by the position PolyPath's
    own Displacement spline (via synthetic keyframe tuples, see
    _lottie_position_displacement_specs).

    value_fn maps a keyframe's own raw (unwrapped) "s" value to this
    property's actual Fusion-space number -- e.g. rotation passes it
    through as-is, Size divides by 100, Aspect derives sy/sx from a
    shared 2D scale keyframe.

    force_linear=True (Aspect, Displacement) skips ease conversion
    entirely and always synthesizes the Linear 1/3-2/3 handles -- see
    _lottie_ease_segment's own docstring and this section's module
    comment for why a derived/synthetic value (not a direct copy of one
    Lottie-authored property) has no real ease to convert.

    time_scale: fusion_comp_fps / lottie_source_fps (see build_lottie_tools),
    applied to every raw Lottie frame number here -- the ONE choke point
    every animated-property path (scalar fields, the Displacement spline,
    per-shape opacity) funnels through before a frame number becomes a
    Fusion KeyFrames table key. Writing every raw Lottie "t" straight
    into Fusion's own KeyFrames table unscaled would make a Lottie file
    authored at a different fps than the destination comp play back at
    the wrong speed (frame N landing at real-time N/fusion_fps instead of
    the source's own intended N/lottie_fps). Scaling t0/t1 together here
    (rather than only the keyframe's own point) keeps RH/LH -- computed
    below as t0/t1 plus a FRACTION of (t1-t0) -- correct automatically,
    since that fraction is scale-invariant."""
    def raw_value(v):
        return v[0] if unwrap_scalar and isinstance(v, list) else v

    times = [kf[0] * time_scale for kf in keyframes]
    values = [value_fn(raw_value(kf[1])) for kf in keyframes]
    n = len(keyframes)
    rh, lh, seg_linear = [None] * n, [None] * n, [True] * n
    for i in range(n - 1):
        t0, v0, t1, v1 = times[i], values[i], times[i + 1], values[i + 1]
        if force_linear:
            is_linear = True
            r = (t0 + (t1 - t0) / 3.0, v0 + (v1 - v0) / 3.0)
            l = (t0 + (t1 - t0) * 2.0 / 3.0, v0 + (v1 - v0) * 2.0 / 3.0)
        else:
            is_linear, r, l = _lottie_ease_segment(
                t0, v0, t1, v1, keyframes[i][2], keyframes[i][3], ease_component)
        rh[i], lh[i + 1] = r, l
        seg_linear[i] = is_linear
        if report is not None and (keyframes[i][6] or keyframes[i + 1][6]):
            report.note_approximated_stepped_keyframe()

    specs = []
    for i in range(n):
        left_ok = seg_linear[i - 1] if i > 0 else True
        right_ok = seg_linear[i] if i < n - 1 else True
        specs.append((times[i], values[i], rh[i], lh[i], left_ok and right_ok))
    return specs


def _lua_keyframes_table_lua(specs):
    """specs: [(frame, value, rh, lh, linear), ...], ascending frame
    order. rh is None iff this is the LAST spec; lh is None iff this is
    the FIRST -- every other entry always carries both (confirmed: LH/RH
    are omitted ONLY for lack of a neighbor on that side). value/LH/RH
    use plain positional Lua syntax (Fusion's own numeric sub-keys aren't
    valid Lua bareword identifiers).

    The frame key is a bare Lua NUMBER (`[44.0] = {...}`), NOT a quoted
    string -- with string keys every generated spline collapses to
    Fusion's own single-default-keyframe fallback (same frame number --
    the comp's own current time -- and value 0.0 on EVERY field
    regardless of layer), meaning the whole KeyFrames table is invisible
    to Fusion's real curve lookup. A JSON dump showing "44.0" as the key
    is no evidence for strings -- JSON object keys are ALWAYS strings
    regardless of whether the source Lua table used a string or a number
    key -- and a frame-indexed animation curve is keyed numerically,
    matching what a real interpolation lookup needs."""
    lines = []
    for frame, value, rh, lh, linear in specs:
        entry = [f"{value!r}"]
        if lh is not None:
            entry.append(f"LH = {{ {lh[0]!r}, {lh[1]!r} }}")
        if rh is not None:
            entry.append(f"RH = {{ {rh[0]!r}, {rh[1]!r} }}")
        if linear:
            entry.append("Flags = { Linear = true }")
        lines.append(f'            [{float(frame)!r}] = {{ {", ".join(entry)} }},')
    return "\n".join(lines)


def _build_bezier_spline_lua(name, specs):
    """Generic scalar keyframe-curve tool, shared by every animated
    scalar Input (Angle/Size/Aspect/TopLeftAlpha) and by a position
    PolyPath's own Displacement. NameSet=true follows this file's own
    universal every-referenced-tool convention -- not itself confirmed
    for BezierSpline specifically by a real dump.

    Deliberately writes NO ViewInfo -- see the module comment right
    before _MODIFIER_TOOLS_NOTE."""
    return f"""{name} = BezierSpline {{
        NameSet = true,
        KeyFrames = {{
{_lua_keyframes_table_lua(specs)}
        }},
    }}"""


# --------------------------------------------------------------------------
# Fusion Transform Center/Pivot, DERIVED and numerically verified
# (read this before touching
# _lottie_center_point/_lottie_pivot_point/_wrap_layer_transform).
#
# Fusion's real Transform formula, established from two observed
# behaviours (see the Center/Pivot module comment before
# _shape_pivot_point): moving Pivot ALONE at Angle=0/Size=1
# shifts nothing, and setting Center=Pivot shifts by a per-shape amount
# (the ghosting problem). The only formula satisfying both is
#
#     out = Center + Pivot - 0.5 + M(Angle, Size, Aspect) * (in - Pivot)
#
# i.e. Center translates by (Center - 0.5) regardless of Pivot, and Pivot is
# purely the rotate/scale anchor. NOT the naive `Center + M*(in - Pivot)`.
#
# AE/Lottie's own per-layer transform is `world = p + M * (local - a)`.
# Conjugating that into Fusion image coordinates gives Center/Pivot that
# depend on WHICH COORDINATE SPACE this particular Transform's input and
# output live in -- three cases actually occur here (see
# _lottie_transform_offsets):
#
#   own layer     local geometry in, comp-absolute out
#   ancestor      comp-absolute in and out (an AE parent-chain level)
#   shape group   local in and out (a promoted gr "tr", see
#                 lottie_shapes_to_nodes)
#
# and both cases reduce to ONE pair of formulas with two offsets:
#
#     Pivot  = to_point(a - pivot_offset)      + 0.5
#     Center = to_point(p - a - center_offset) + 0.5
#
# Verified numerically (9 cases: the real Shapes.json chain, non-origin probe
# points, non-zero leaf anchors, an ancestor whose anchor != comp/2, rotation
# on the leaf, rotation on an ancestor, non-uniform scale, rotation plus
# non-uniform scale) against ground truth composed independently with this
# file's own already-trusted lottie_matrix_from_static_transform -- exact to
# floating point in every case, while the simpler convention below is off by
# as much as 0.29 of the frame. The same harness pinned Fusion's Angle as the
# NEGATIVE of Lottie's own rotation (Lottie rotates clockwise in its y-down
# space; to_point flips y).
#
# Why the simpler convention looks right most of the time: omitting the
# `- a` from Center and using pivot_offset=0 everywhere is exactly right
# whenever a layer's anchor is (0,0) AND every ancestor's anchor happens to
# equal comp/2 -- which is precisely the common case (most leaf layers anchor
# at the origin; a precomp INSTANCE conventionally anchors at its asset's own
# canvas centre). Worse, the resulting error is a pure TRANSLATION per level,
# and the whole-composite auto-recentering Transform absorbs any
# uniform translation -- so the residual only shows up as layers drifting
# RELATIVE to each other ("misplaced shapes").
# --------------------------------------------------------------------------
_LOTTIE_SPACE_OWN_LAYER = "own_layer"
_LOTTIE_SPACE_ANCESTOR = "ancestor"
_LOTTIE_SPACE_SHAPE_GROUP = "shape_group"


def _lottie_transform_offsets(space, comp_w, comp_h):
    """(pivot_offset, center_offset) for one of the three transform spaces
    above -- see this section's own module comment for the derivation.
    comp_w/comp_h are the LOTTIE composition's own dimensions."""
    half = (comp_w / 2.0, comp_h / 2.0)
    zero = (0.0, 0.0)
    if space == _LOTTIE_SPACE_OWN_LAYER:
        return zero, half
    if space == _LOTTIE_SPACE_ANCESTOR:
        return half, zero
    if space == _LOTTIE_SPACE_SHAPE_GROUP:
        return zero, zero
    raise ValueError(f"unknown Lottie transform space {space!r}")


def _lottie_pivot_point(anchor_xy, pivot_offset, to_point):
    """Transform's Pivot -- the rotate/scale anchor, in the frame-normalized
    (0.5,0.5)-is-centre convention. See this section's module comment."""
    x, y = to_point(anchor_xy[0] - pivot_offset[0], anchor_xy[1] - pivot_offset[1])
    return x + 0.5, y + 0.5


def _lottie_center_point(position_xy, anchor_xy, center_offset, to_point):
    """Converts a layer's own POSITION (a COMP-ABSOLUTE Lottie pixel
    coordinate -- where in the comp's own top-left-origin space this
    layer's anchor point lands) into the frame-normalized (0.5,0.5)-
    center convention Transform's Center/_shape_pivot_point's Pivot both
    use.

    Subtracts the transform's OWN anchor as well as center_offset -- see
    this section's module comment for the derivation and the numerical
    verification, and for why omitting the anchor term looks right in
    the common case.

    Position is fundamentally different from Anchor or shape geometry,
    which are LAYER-LOCAL (relative to the layer's own asset origin) --
    make_lottie_converter's own to_point deliberately maps LOCAL (0,0)
    straight onto Fusion's frame-center (see its docstring), which is
    correct for local geometry/anchor but WRONG for a comp-absolute
    coordinate like Position: without the offset the entire composite is
    shoved off toward one corner, consistently, for every layer (e.g. a
    layer sitting at the Lottie comp's own center lands at (0.64, 0.25)
    instead of (0.5, 0.5)). center_offset carries HALF the LOTTIE comp's own
    width/height for the two spaces that need it -- mirrors
    make_converter's own min_x + svg_w/2.0 viewBox-centering for SVG,
    which make_lottie_converter's docstring correctly notes Lottie has no
    equivalent of at the LOCAL level, but position still needs it at the
    COMP level."""
    x, y = _lottie_comp_relative_point(position_xy, anchor_xy, center_offset, to_point)
    return x + 0.5, y + 0.5


def _lottie_comp_relative_point(position_xy, anchor_xy, center_offset, to_point):
    """The centering half of _lottie_center_point WITHOUT its final +0.5
    shift -- i.e. the plain to_point()-style "-0.5..0.5, centered at 0"
    convention every OTHER polyline in this file uses (see
    subpath_to_polyline_lua), just with Position's own comp-centering
    applied first.

    Needed as its own function because PolyPath's own `PolyLine` input
    uses THIS convention, not the +0.5-shifted Center/Pivot one:
    writing PolyLine points already shifted by _lottie_center_point's own
    +0.5 makes PolyPath's Source="Position" output come out shifted by
    +0.5 TWICE (once from the point data, once from whatever internal
    +0.5-equivalent conversion PolyPath itself applies when producing a
    Center-compatible output) -- parked exactly on a Displacement
    keyframe's own last value (1.0, so PolyPath's output should exactly
    equal its last PolyLine point), Center reads 0.5 higher than that
    point on BOTH axes. A hand-built dump's own Path1 agrees: its points
    are negative (e.g. X=-0.253), so a real PolyLine has no +0.5 shift."""
    return to_point(position_xy[0] - anchor_xy[0] - center_offset[0],
                     position_xy[1] - anchor_xy[1] - center_offset[1])


def _lottie_position_polyline_lua(keyframes, anchor_xy, center_offset, to_point, to_vector):
    """The position PolyPath's own `PolyLine` literal -- one point per
    position keyframe, in the same frame-normalized (0.5,0.5)-center
    convention _shape_pivot_point already uses, each point's own
    LX/LY/RX/RY shaping the ON-SCREEN motion path (not time). anchor_xy/
    center_offset are this transform's own resolved-static Anchor and its
    space's own offset -- see _lottie_center_point / this section's own
    module comment for why Position needs both and plain Anchor/geometry
    conversions don't.

    ASSUMPTION, not dump-confirmed: reads a keyframe's own "to" (spatial
    tangent leaving THIS keyframe) as RX/RY and "ti" (spatial tangent
    arriving at THIS keyframe) as LX/LY -- a structural analogy to
    lottie_shape_to_subpath's own i[n]/o[n]-on-the-same-vertex convention,
    genuinely unconfirmed against a real dump (only pasting a file with a
    real curved motion path and checking the curve direction can
    confirm it)."""
    lines = []
    for (t, v, o, i, sp_out, sp_in, h) in keyframes:
        x, y = _lottie_comp_relative_point(v, anchor_xy, center_offset, to_point)
        # Spec says "to"/"ti" are [x, y], but some exporters (AE's Position is internally
        # always 3D) emit a 3rd z component even on purely 2D artwork (e.g. every tangent
        # [x, y, 0]), which would otherwise crash the unpacking below.
        # Fusion has no use for it here, so take only the first two components.
        rx, ry = to_vector(*sp_out[:2]) if sp_out else (0.0, 0.0)
        lx, ly = to_vector(*sp_in[:2]) if sp_in else (0.0, 0.0)
        fields = [f"X = {x!r}", f"Y = {y!r}", f"LX = {lx!r}", f"LY = {ly!r}",
                  f"RX = {rx!r}", f"RY = {ry!r}"]
        if sp_out is None and sp_in is None:
            fields.append("Linear = true")
        lines.append("{ " + ", ".join(fields) + " }")
    return "Polyline { Closed = false, Points = { " + ", ".join(lines) + " } }"


def _lottie_position_displacement_specs(keyframes, to_point, time_scale=1.0):
    """Cumulative chord-length-normalized progress value (0..1) per
    position keyframe, at that SAME keyframe's own frame time --
    confirmed against the real dump's own empirical finding that
    Displacement isn't naive index/(count-1) (a 3-point path's middle
    keyframe landed at 0.5688, not 0.5, tracking actual point spacing).

    Chord length (straight-line distance between the already-converted
    screen-space points), not true bezier arc length through the path's
    own spatial handles, is the pragmatic choice here: the load-bearing
    correctness -- each keyframe's OWN point landing exactly right --
    only depends on Displacement(t_i) equalling this i's own progress
    value, which holds regardless of how the in-between arc length is
    approximated; only the in-between interpolation FEEL is affected.
    Always force_linear (see _lottie_keyframe_specs) -- Displacement is a
    synthetic derived scalar Lottie never authored an ease for."""
    points = []
    for kf in keyframes:
        x, y = to_point(kf[1][0], kf[1][1])
        points.append((x + 0.5, y + 0.5))
    n = len(points)
    cum = [0.0] * n
    for i in range(1, n):
        cum[i] = cum[i - 1] + math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
    total = cum[-1]
    synthetic = []
    for i, kf in enumerate(keyframes):
        d = (cum[i] / total) if total > 0 else (i / (n - 1) if n > 1 else 0.0)
        synthetic.append((kf[0], [d], None, None, None, None, False))
    return _lottie_keyframe_specs(synthetic, value_fn=lambda d: d, force_linear=True, unwrap_scalar=True,
                                    time_scale=time_scale)


# --------------------------------------------------------------------------
# Modifiers vs tools in the Flow view.
#
# BezierSpline and PolyPath are Fusion MODIFIERS, not flow tools: they drive
# another tool's Input via SourceOp rather than passing an image along, and
# Fusion normally surfaces them in the Spline/Modifiers views, NOT as boxes
# in the Flow. Confirmed that Fusion already understands ours that way -- in
# a real "Dump Selected Node Settings" capture of our own pasted graph, each
# spline came back nested inside its HOST tool's own Tools table (e.g.
# Shape_3TopLeftAlpha inside Shape_3), exactly where a modifier belongs,
# while genuine sibling tools like Shape_3_Mask each got their own top-level
# entry.
#
# They were nevertheless showing up as dozens of unconnected boxes cluttering
# the Flow, because we wrote `ViewInfo = OperatorInfo { Pos = ... }` on them
# -- OperatorInfo is the ViewInfo type for a flow OPERATOR, i.e. it asserts
# "this thing has a place in the Flow". Modifiers are therefore now emitted
# with no ViewInfo at all, and no grid slot is reserved for them either,
# which also lets the real tool chain pack together instead of leaving gaps
# where a modifier used to sit.
#
# Not yet confirmed: that dropping OperatorInfo really does
# hide them rather than just leaving them unpositioned. If Fusion instead
# piles them at the origin, the fallback is to put ViewInfo back but park
# them in one tidy block well clear of the real chain.
#
# NOT done, deliberately: the alternative of nesting them in their own
# GroupOperator. Fusion's clipboard paste silently DROPS Inputs that reference across a
# nested GroupOperator boundary (see build_group_lua) -- and every modifier
# is referenced by a tool outside any group it were put in, which is exactly
# that broken pattern.
# --------------------------------------------------------------------------
_MODIFIER_TOOLS_NOTE = True


def _build_polypath_lua(name, polyline_lua, displacement_source_op):
    """The PolyPath tool an animated Center/Pivot wires to
    (Source="Position") -- confirmed structure from the real dump, see
    this section's own module comment. Writes no ViewInfo: it's a
    modifier, see the module comment above."""
    return f"""{name} = PolyPath {{
        NameSet = true,
        Inputs = {{
            PolyLine = Input {{ Value = {polyline_lua} }},
            Displacement = Input {{ SourceOp = "{displacement_source_op}", Source = "Value" }},
        }},
    }}"""


def _build_animated_layer_transform_lua(name, pivot_field_lua, center_field_lua, size_field_lua,
                                          aspect_field_lua, angle_field_lua, input_source, pos):
    """Deliberately NOT a generalization of _build_transform_lua --
    that one is used by every shape/whole-graphic pivot, and the animated
    Center/Angle/Size path is shaped differently. Pure template assembly -- makes NO static-vs-animated
    decisions itself; each of center_field_lua/size_field_lua/
    aspect_field_lua/angle_field_lua is a complete, pre-built,
    comma-terminated Inputs line (see _static_or_animated_scalar_field/
    _static_or_animated_point_field), independently either a literal
    Value or a SourceOp wire -- Pivot included, since a consolidated
    object's merged levels can genuinely animate it."""
    return f"""{name} = Transform {{
        NameSet = true,
        Inputs = {{
            {pivot_field_lua}
            {center_field_lua}
            {size_field_lua}
            {aspect_field_lua}
            {angle_field_lua}
            Input = Input {{ SourceOp = "{input_source}", Source = "Output" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def _static_or_animated_scalar_field(fusion_field, value_or_keyframes, names, positions, flow_level,
                                       stack_slot, value_fn, ease_component=0, force_linear=False,
                                       unwrap_scalar=False, name_hint="Anim", report=None, time_scale=1.0):
    """Decides whether one Transform/Background scalar Input
    (Angle/Size/Aspect/TopLeftAlpha) stays a literal static Value or gets
    wired to its own new BezierSpline tool. Returns (field_lua_line,
    [new_tool_lua, ...])."""
    if not _lottie_field_is_really_animated(value_or_keyframes):
        static_val = value_fn(_lottie_resolved_static(value_or_keyframes, scalar=unwrap_scalar))
        return f'{fusion_field} = Input {{ Value = {static_val!r} }},', []
    if force_linear and report is not None:
        report.note_approximated_derived_scalar_ease()
    specs = _lottie_keyframe_specs(value_or_keyframes, value_fn, ease_component=ease_component,
                                     force_linear=force_linear, unwrap_scalar=unwrap_scalar, report=report,
                                     time_scale=time_scale)
    spline_name = names.safe_name(name_hint, "BezierSpline")
    tool_lua = _build_bezier_spline_lua(spline_name, specs)
    return f'{fusion_field} = Input {{ SourceOp = "{spline_name}", Source = "Value" }},', [tool_lua]


def _static_or_animated_point_field(fusion_field, field, anchor_xy, offset, to_point, to_vector, names,
                                      positions, flow_level, stack_slot, name_hint, report=None,
                                      time_scale=1.0):
    """A POINT-type Transform Input (Center or Pivot) -- structurally
    different from every scalar field: when animated it routes through a
    PolyPath+Displacement pair, not a plain BezierSpline (see this
    section's own module comment / the confirmed real dump).

    The written value is always `to_point(field - anchor_xy - offset) +
    0.5`, which covers both callers: Center passes the layer's own
    Position with anchor_xy=its Anchor and offset=center_offset, while
    Pivot passes its Anchor with anchor_xy=(0,0) and offset=pivot_offset
    -- see this section's own module comment for why those are the right
    two corrections.

    An animated PIVOT is UNCONFIRMED against a real dump -- the real dump
    this design came from had a static Pivot alongside an animated
    Center, so the PolyPath-driven Point mechanism itself is confirmed,
    but never specifically on the Pivot input. It is only ever generated
    for a CONSOLIDATED object whose merged segments genuinely disagree
    about their anchor (see _consolidate_lottie_layers); every
    single-segment layer writes a plain static Pivot, so the verified
    static-Pivot path is unaffected.

    Returns (field_lua_line, [new_tool_lua, ...]). The PolyPath sits at
    flow_level (a parallel dependent of the wrapping Transform, same as
    the scalar splines -- see _wrap_layer_transform); its own
    Displacement spline sits one step further back, feeding it."""
    if not _lottie_field_is_really_animated(field):
        x, y = _lottie_center_point(_lottie_resolved_static(field), anchor_xy, offset, to_point)
        return f'{fusion_field} = Input {{ Value = {{ {x!r}, {y!r} }} }},', []

    if report is not None:
        report.note_approximated_derived_scalar_ease()
    polyline_lua = _lottie_position_polyline_lua(field, anchor_xy, offset, to_point, to_vector)
    disp_specs = _lottie_position_displacement_specs(field, to_point, time_scale=time_scale)
    disp_name = names.safe_name(f"{name_hint}Displacement", "BezierSpline")
    disp_tool = _build_bezier_spline_lua(disp_name, disp_specs)

    path_name = names.safe_name(f"{name_hint}Path", "PolyPath")
    path_tool = _build_polypath_lua(path_name, polyline_lua, disp_name)

    return (f'{fusion_field} = Input {{ SourceOp = "{path_name}", Source = "Position" }},',
            [disp_tool, path_tool])


def _wrap_layer_transform(anim, name_base, content_output_name, output_slot, last_flow_level, lottie_comp_w,
                            lottie_comp_h, to_point, to_vector, names, positions, report, time_scale=1.0,
                            space=_LOTTIE_SPACE_OWN_LAYER):
    """Wraps a Lottie layer's (or one of its AE ancestors', see
    build_group_lua's own wrap hookup) already-built STATIC content
    (exactly the same BuiltGraph output_name/output_slot/output_flow
    build_group_lua already tracks for every other node) in one new
    animated Transform -- mirrors build_shape_lua's own per-shape pivot
    wrap (always claim one flow-step past the real tail, same stack slot)
    and _build_mask_chain's "pile dependents behind the consumer, let
    claim()'s own collision-walk fan out parallel ones" pattern.

    Deliberately takes anim/name_base directly rather than a GroupNode --
    an ancestor in a parent chain has neither (see
    _resolve_lottie_ancestor_chain), just its own resolved
    LottieLayerAnimation and a name to give its Transform tool.

    lottie_comp_w/lottie_comp_h are the LOTTIE composition's own
    dimensions (NOT the Fusion comp's) -- only Center/Pivot need them,
    see _lottie_center_point. space says which coordinate spaces THIS
    transform's input and output live in, which is what decides the
    Center/Pivot offsets -- see _lottie_transform_offsets and this
    section's own module comment. Returns (new_tools, output_name,
    output_slot, output_flow)."""
    tools = []
    pivot_offset, center_offset = _lottie_transform_offsets(space, lottie_comp_w, lottie_comp_h)

    # Center subtracts this transform's own Anchor, so it needs ONE anchor
    # value even when the anchor itself animates (only a consolidated
    # object's merged levels ever do -- see _consolidate_lottie_layers).
    # Using the resolved-static anchor there would desynchronise Center
    # from an animated Pivot, so the SAME per-keyframe anchor has to come
    # out of both: when the anchor animates, Center's own PolyLine is
    # built from position keyframes already reduced by it (see
    # _lottie_merged_center_keyframes).
    anchor_val = _lottie_resolved_static(anim.anchor)
    center_field, center_anchor = anim.position, anchor_val
    if _lottie_field_is_really_animated(anim.anchor):
        center_field, center_anchor = _lottie_merged_center_keyframes(anim.position, anim.anchor), (0.0, 0.0)

    transform_flow = last_flow_level + 1
    dep_flow = transform_flow - 1

    pivot_lua, pivot_tools = _static_or_animated_point_field(
        "Pivot", anim.anchor, (0.0, 0.0), pivot_offset, to_point, to_vector, names, positions, dep_flow,
        output_slot, f"{name_base}Pivot", report=report, time_scale=time_scale)
    center_lua, center_tools = _static_or_animated_point_field(
        "Center", center_field, center_anchor, center_offset, to_point, to_vector, names, positions, dep_flow,
        output_slot, name_base, report=report, time_scale=time_scale)
    size_lua, size_tools = _static_or_animated_scalar_field(
        "Size", anim.scale, names, positions, dep_flow, output_slot,
        value_fn=lambda sxsy: sxsy[0] / 100.0, ease_component=0, name_hint=f"{name_base}Size",
        time_scale=time_scale)
    aspect_lua, aspect_tools = _static_or_animated_scalar_field(
        "Aspect", anim.scale, names, positions, dep_flow, output_slot,
        value_fn=lambda sxsy: (sxsy[1] / sxsy[0]) if sxsy[0] else 1.0, force_linear=True,
        name_hint=f"{name_base}Aspect", report=report, time_scale=time_scale)
    # Angle is the NEGATIVE of Lottie's own rotation: Lottie rotates
    # clockwise-positive in its own y-DOWN space, while Fusion's Angle is
    # counter-clockwise-positive and to_point flips y. Confirmed by the
    # same numerical harness that derived Center/Pivot (see this section's
    # module comment) -- with +r the rotation cases missed ground truth by
    # up to 0.068, with -r every case was exact. Checking only the
    # spline's own VALUES against the source can't catch this sign -- a
    # -45..45 spin looks plausible either way -- only the on-screen
    # rotation DIRECTION shows it.
    angle_lua, angle_tools = _static_or_animated_scalar_field(
        "Angle", anim.rotation, names, positions, dep_flow, output_slot,
        value_fn=lambda r: -r, unwrap_scalar=True, ease_component=0, name_hint=f"{name_base}Angle",
        time_scale=time_scale)

    tools.extend(pivot_tools)
    tools.extend(center_tools)
    tools.extend(size_tools)
    tools.extend(aspect_tools)
    tools.extend(angle_tools)

    transform_name = names.safe_name(f"{name_base}_AnimTransform", "Transform")
    transform_pos, transform_slot = positions.claim(transform_flow, output_slot)
    tools.append(_build_animated_layer_transform_lua(
        transform_name, pivot_lua, center_lua, size_lua, aspect_lua, angle_lua, content_output_name,
        transform_pos))

    return tools, transform_name, transform_slot, transform_flow


# ==========================================================================
# Whole-graphic pivot: ONE more Transform, wrapping the entire file's final
# combined output (see build_group_lua's own is_root branch), on top of --
# not instead of -- every individual shape's own pivot above. Exists for
# exactly the case a real file surfaced: several separate shapes (an
# outline, a couple of accent marks, a couple of fill blobs) that are
# visually ONE logomark but share no <g> in the source SVG at all, so
# nothing about the file itself says "these belong together." Keyframing
# each shape's own independent pivot in lockstep doesn't work -- each one
# spins around its OWN center, so the pieces drift apart instead of
# rotating as one rigid object. This gives an animator one obvious handle
# for "rotate/scale the WHOLE thing" instead.
#
# Deliberately an AREA-WEIGHTED centroid, not the bounding box of
# everything combined -- a plain bbox union is dragged around by whatever
# sits at the extremes (a single small accent mark far from the main body
# pulls the box, and its center, toward itself) regardless of how little of
# the actual graphic it represents. Weighting each subpath by its own
# (shoelace) area instead means a small decoration barely moves the result
# and the bulk of the artwork dominates -- much closer to where a person
# would say "the middle of this thing" actually is.
# ==========================================================================
def _collect_all_shapes(node, shapes, matrix=IDENTITY, lottie_comp_size=None):
    """Recursively gathers every ShapeNode in the tree rooted at node (see
    walk()) into the shapes list, each paired with the ACCUMULATED matrix
    needed to place its own geometry at its real on-screen position --
    used to weigh the WHOLE file's worth of geometry at once, regardless
    of how many dissolved <g>s (or Lottie layers) it's nested under.

    matrix is IDENTITY for every SVG call (SVG's own <g> transforms are
    already baked into geometry by walk(), so nothing further is needed)
    and for a Lottie layer that's NOT really animated (its own ks was
    already baked into geometry by _bake_lottie_layer_transform). For a
    Lottie layer that IS really animated, its geometry stays LAYER-LOCAL
    in the real tree (build_group_lua wraps it in a live Transform
    instead of baking) -- accumulating that layer's own resolved-static
    matrix here (same construction _bake_lottie_layer_transform uses)
    approximates where it sits at its own FIRST frame, giving
    _weighted_pivot_point a representative "resting pose" for the whole-
    graphic recentering calculation without needing to mutate the real
    tree or duplicate its geometry."""
    if isinstance(node, ShapeNode):
        shapes.append((node, matrix))
    else:
        child_matrix = matrix
        anim = getattr(node, "layer_animation", None)
        live = anim is not None and (_layer_has_transform_animation(anim)
                                      or getattr(node, "needs_live_transform", False))
        if live and lottie_comp_size is not None:
            comp_w, comp_h = lottie_comp_size
            px, py = _lottie_resolved_static(anim.position)[:2]
            # A promoted shape group's own position is LAYER-LOCAL, not
            # comp-absolute, so it gets no comp-centering -- same
            # space distinction _lottie_transform_offsets encodes for the
            # real generated Transforms (see there).
            if (getattr(node, "transform_space", None) or _LOTTIE_SPACE_OWN_LAYER) == _LOTTIE_SPACE_OWN_LAYER:
                px, py = px - comp_w / 2.0, py - comp_h / 2.0
            tr = {"p": {"a": 0, "k": [px, py]},
                  "a": {"a": 0, "k": _lottie_resolved_static(anim.anchor)},
                  "s": {"a": 0, "k": _lottie_resolved_static(anim.scale)},
                  "r": {"a": 0, "k": _lottie_resolved_static(anim.rotation, scalar=True)}}
            child_matrix = mat_mul(matrix, lottie_matrix_from_static_transform(tr))
        for child in node.children:
            _collect_all_shapes(child, shapes, child_matrix, lottie_comp_size)


def _polygon_signed_area_centroid(anchors):
    """Shoelace formula over a subpath's own ANCHOR points ONLY (bezier
    handles ignored) -- a straight-edge approximation of its true curved
    area, same tolerance _subpaths_bbox already accepts for this shape's
    own bounding box. Returns (signed_area, centroid_x, centroid_y).

    The SIGN is what does the real work here: a subpath drawn with the
    opposite winding direction from its outer contour (how a hole is
    conventionally authored -- see build_shape_lua's own fill-rule caveat)
    comes back with a NEGATIVE area, so summing signed areas across every
    subpath of a compound shape nets out holes automatically, and summing
    signed_area * centroid across every subpath in the WHOLE file gives a
    true area-weighted centroid without ever having to classify which
    subpath is an outer region and which is a hole.

    Returns (0.0, 0.0, 0.0) for a degenerate subpath (fewer than 3 anchors,
    or an exact zero-area one, e.g. a stray single point) -- contributes no
    weight rather than raising, since a compound path having one stray
    degenerate subpath alongside real geometry is exactly the kind of thing
    this file's own Import Report philosophy says to tolerate, not crash
    on."""
    n = len(anchors)
    if n < 3:
        return 0.0, 0.0, 0.0
    area2 = cx = cy = 0.0
    for i in range(n):
        x0, y0 = anchors[i]['x'], anchors[i]['y']
        x1, y1 = anchors[(i + 1) % n]['x'], anchors[(i + 1) % n]['y']
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area2) < 1e-12:
        return 0.0, 0.0, 0.0
    return area2 / 2.0, cx / (3.0 * area2), cy / (3.0 * area2)


def _weighted_pivot_point(top, to_point, lottie_comp_size=None):
    """The area-weighted centroid of every shape in the whole file (see
    this section's own module comment), converted into the SAME frame-
    normalized, (0.5, 0.5)-is-center convention _shape_pivot_point already
    uses for individual shapes.

    Signed areas are summed WITHIN each shape's own subpaths first (that's
    where "opposite winding means a hole" is actually a meaningful,
    reliable convention -- see _polygon_signed_area_centroid), but the
    resulting per-shape net area is then used by its ABSOLUTE VALUE as
    that shape's weight in the whole-file average. Confirmed necessary,
    not just cautious, against a real file (a 31-path icon):  summing
    signed area directly across every subpath in the FILE, with no per-
    shape abs(), let two unrelated shapes' independent (and unrelated)
    winding directions cancel each other's weight instead of adding, and
    the file's total came out net NEGATIVE overall -- dividing by that
    put the resulting "centroid" outside the entire viewBox (a Y attempted
    -153 in a 0-1024-tall document), which is what looked like a pivot
    plainly floating outside the actual artwork instead of just being
    somewhere unexpected. There's no reason two SEPARATE <path> elements'
    winding directions should relate to each other at all -- only a single
    shape's own subpaths (genuinely its own outer-vs-hole relationship)
    are safe to let cancel by sign.

    Falls back to the plain bbox-center of every subpath combined (the
    same convention _shape_pivot_point uses, just over ALL of them at
    once) when every shape's own net area is ~0 -- e.g. a file that's
    somehow made up entirely of degenerate/stray subpaths with no real
    fill area to weigh at all. That's a real edge case, not a guess:
    _subpaths_bbox already treats a zero-size dimension the same way, via
    its own max(..., 1e-9) floor.

    lottie_comp_size: the LOTTIE composition's own (width, height), or
    None for every SVG call -- see _collect_all_shapes for why an
    animated Lottie layer's own geometry needs its first-frame matrix
    applied here (approximating its real on-screen position) before its
    area/centroid gets weighed in."""
    shapes = []
    _collect_all_shapes(top, shapes, lottie_comp_size=lottie_comp_size)
    total_area = wx = wy = 0.0
    all_anchors = []
    for shape, matrix in shapes:
        shape_area = shape_wx = shape_wy = 0.0
        for sp in shape.subpaths:
            if matrix is IDENTITY:
                anchors = sp.anchors
            else:
                anchors = [{'x': mat_point(matrix, a['x'], a['y'])[0],
                            'y': mat_point(matrix, a['x'], a['y'])[1]} for a in sp.anchors]
            all_anchors.extend(anchors)
            area, cx, cy = _polygon_signed_area_centroid(anchors)
            shape_area += area
            shape_wx += area * cx
            shape_wy += area * cy
        if abs(shape_area) < 1e-9:
            continue
        weight = abs(shape_area)
        total_area += weight
        wx += weight * (shape_wx / shape_area)
        wy += weight * (shape_wy / shape_area)
    if abs(total_area) < 1e-9:
        xs = [a['x'] for a in all_anchors] or [0.0]
        ys = [a['y'] for a in all_anchors] or [0.0]
        px, py = to_point((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    else:
        px, py = to_point(wx / total_area, wy / total_area)
    return px + 0.5, py + 0.5


def build_shape_lua(shape, to_point, to_vector, comp_w, comp_h, flow_level, stack_slot, names, positions,
                     external_clip=None, color_registry=None, layer_opacity_anim=None, time_scale=1.0):
    """Fill: one PolylineMask per subpath, chained into a single mask,
    feeding one Background. The first subpath's mask is plain; every
    subsequent one gets PaintMode=Invert with its EffectMask fed from the
    previous mask in the chain. This mirrors native import exactly, and the
    reason it needs no outer-vs-hole analysis at all is that Fusion's
    Invert paint mode composes as XOR: it flips the incoming mask wherever
    its own shape is, so output = incoming XOR own_shape. Chained over a
    compound path's subpaths in document order, that IS the even-odd fill
    rule -- a subpath nested inside filled area toggles it back off (a
    hole), while one over empty area toggles it on (a separate positive
    region). Both cases fall out of the same rule, which is why the chain
    alone handles shapes whose subpaths are a mix of holes and disjoint
    regions.

    The alternative -- classifying subpaths into (outer, [holes])
    clusters by containment depth (area-weighted centroids,
    point-in-polygon tests, one Background per cluster joined by Merges) --
    produces pixel-identical fills (rasterizing both models over a
    5-path, 21-subpath logo matches every shape exactly) but needs roughly
    a hundred lines of geometry, plus a Background and a Merge per cluster
    instead of one Background per path. Native's chain gets the same
    answer from one rule, so this follows native.

    Caveat, since the XOR chain is literally the even-odd rule: an SVG
    path with fill-rule="nonzero" (the spec default) whose subpaths
    overlap with the SAME winding direction would fill differently --
    nonzero fills the overlap, even-odd punches it out. Native import
    appears to ignore fill-rule the same way. In practice exporters draw
    holes with opposite winding, where the two rules agree, so this only
    bites on deliberately same-winding overlaps.

    Stroke: the SAME subpaths chained again through a SEPARATE mask chain,
    with BorderWidth+Solid=0 (see build_mask_tool_lua) instead of a solid
    fill, and NOT inverted -- a stroke has to UNION every subpath's outline
    (so a compound path's stroke traces every subpath, holes included),
    not XOR them like the fill chain. Confirmed against a real
    native-import dump for a stroked path: native emits a second
    mask+Background pair per stroked path (matching point-for-point the
    fill mask's own Polyline), with the stroke Background as the fill
    Background's Foreground in a Merge -- i.e. drawn ON TOP of the fill, per
    the SVG spec.

    Returns a BuiltGraph -- always 1 slot, since the whole shape (fill
    chain, stroke chain, and the Merge combining them when both exist)
    lives in its own single stack column, and only that one slot ever needs
    advancing past for the next sibling. Everything below flow_level is
    build-shape-internal plumbing; build_group_lua only ever sees this
    shape's ONE output at (flow_level, stack_slot), exactly as if it had no
    stroke at all -- so nothing downstream depends on strokes.

    Masks are piled onto the flow axis above the Background rather than
    spread across stack slots at flow_level: the chain is a genuine
    downstream sequence, so it belongs on the flow axis, not spread
    sideways (see PastePositions.claim), and the same applies to the
    stroke chain.

    external_clip: an SVG <mask>'s own clip chain, passed down from an
    ancestor <g mask="..."> (see build_group_lua) -- ANDed into just the
    fill and stroke chains' own FIRST mask via _build_mask_chain's
    initial_effect_mask, so the whole shape (fill AND stroke, holes
    included) only ever paints inside that outer clip.

    shape.drop_shadow (see parse_drop_shadow_filter_defs/ShapeNode), when
    set, wraps everything below in one more Merge: a whole SEPARATE mask+
    Background pair silhouetting the SAME subpaths, shifted/blurred/tinted
    per the filter, as that Merge's Background (drawn behind), with this
    shape's normal fill+stroke output as its Foreground. Reusing flow_level
    itself for that outer Merge -- exactly as the fill+stroke "both" Merge
    already does -- means content_flow_level (one step further back) is
    what fill/stroke actually build against instead of flow_level directly,
    so build_group_lua still finds this shape's ONE final output at
    flow_level regardless of whether a shadow exists.

    layer_opacity_anim: a Lottie layer's own animated opacity keyframes
    (see build_group_lua), or None -- the common case, and every SVG
    caller. When set, the fill and/or stroke Background's TopLeftAlpha
    gets wired to its own new per-shape Alpha BezierSpline (source
    keyframes shared with every other shape in the same layer, but each
    spline's own VALUES are this shape's real color alpha times the
    layer's opacity, since a Background always folds its own static alpha
    in regardless) instead of staying a literal Value. Deliberately NOT
    applied to a drop-shadow/inner-shadow Background even when set (see
    LottieImportReport's own approximated_animated_shadow_opacities
    counter) -- rare combination, not threaded through their own
    multi-stage mask chains."""
    n = len(shape.subpaths)
    tools = []
    has_shadow = shape.drop_shadow is not None
    content_flow_level = flow_level - 1 if has_shadow else flow_level

    # When a shape has BOTH fill and stroke, fill and stroke are two
    # independent terminal nodes (each fed by its own mask chain, neither
    # downstream of the other) that need combining into the ONE output
    # build_group_lua expects back -- so content_flow_level itself hosts a
    # small Merge in that case, and the fill/stroke sub-chains each end one
    # or more flow-steps further out (stroke furthest, since it's the
    # Merge's Foreground -- drawn on top). When only one of fill/stroke is
    # present, there's nothing to combine, and that one chain's own
    # Background lands directly on content_flow_level exactly as it did
    # before stroke support existed.
    both = shape.has_fill and shape.has_stroke
    fill_end = content_flow_level - 1 if both else content_flow_level
    stroke_end = fill_end - (n + 1) if both else content_flow_level

    # Same to_vector X-axis reuse as border_width below -- see
    # build_mask_tool_lua's soft_edge docstring for why this is calibrated
    # against the rendered SVG rather than a native dump.
    soft_edge = to_vector(shape.blur_stddev_px, 0.0)[0] if shape.blur_stddev_px else None

    fill_bg_name = fill_bg_slot = None
    if shape.has_fill:
        # A gradient fill's own overall opacity (SVG fill-opacity) goes on
        # the mask's Level, NOT into the Gradient's own color stops -- see
        # build_mask_tool_lua and _build_gradient_background_lua. A solid
        # fill instead folds it straight into TopLeftAlpha below, same as
        # always -- confirmed only for the gradient case against a real
        # native dump, so left alone here rather than guessed to match.
        fill_level = shape.opacity if shape.fill_gradient is not None and shape.opacity < 1.0 else None
        mask_tools, prev_mask = _build_mask_chain(
            shape.subpaths, to_point, to_vector, comp_w, comp_h, fill_end, stack_slot, names, positions,
            invert_chain=True, border_width=None, force_closed=True,
            name_for=lambda i: f"{shape.name}_Mask" if i == 0 else f"{shape.name}_Sub{i + 1}_Mask",
            level=fill_level, soft_edge=soft_edge, initial_effect_mask=external_clip)
        tools.extend(mask_tools)
        fill_bg_name = names.safe_name(shape.name, "Shape")
        fill_bg_pos, fill_bg_slot = positions.claim(fill_end, stack_slot)
        if shape.fill_gradient is not None:
            tools.append(_build_gradient_background_lua(fill_bg_name, comp_w, comp_h, shape.fill_gradient,
                                                          to_point, prev_mask, fill_bg_pos))
        else:
            color = shape.color or (0.5, 0.5, 0.5, 1.0)  # unparsed fill placeholder
            r, g, b, a = color
            a = a * shape.opacity
            color_exprs = color_registry.lookup(shape, "fill") if color_registry else None
            alpha_source_op = None
            if layer_opacity_anim is not None:
                alpha_name = names.safe_name(f"{shape.name}_Alpha", "BezierSpline")
                specs = _lottie_keyframe_specs(layer_opacity_anim, value_fn=lambda o, mult=a: (o / 100.0) * mult,
                                                 unwrap_scalar=True, time_scale=time_scale)
                tools.append(_build_bezier_spline_lua(alpha_name, specs))
                alpha_source_op = alpha_name
            tools.append(_build_background_lua(fill_bg_name, comp_w, comp_h, r, g, b, a, prev_mask, fill_bg_pos,
                                                 color_exprs=color_exprs, alpha_source_op=alpha_source_op))

    stroke_bg_name = stroke_bg_slot = None
    if shape.has_stroke:
        # Run stroke-width through to_vector itself (the same X-axis
        # conversion every bezier handle's dx already gets: dx * scale /
        # comp_w) rather than re-deriving scale/comp_w here -- confirmed
        # exact against native import: native's own BorderWidth times its
        # SVG's width reproduces its stroke-width exactly, on a comp that
        # happened to equal the SVG's own size. Reusing X specifically (not
        # an X/Y average) keeps that same single-axis convention on a
        # non-square comp too, rather than inventing a new one -- the
        # tradeoff being a stroke that's a hair thicker or thinner on Y than
        # X when the comp's aspect isn't square, the same single-scalar
        # limitation native itself would have if ever run on such a comp.
        border_width = to_vector(shape.stroke_width, 0.0)[0]
        # Same Level-on-mask-vs-TopLeftAlpha split as the fill chain above.
        stroke_level = shape.stroke_opacity if shape.stroke_gradient is not None and shape.stroke_opacity < 1.0 else None
        mask_tools, prev_mask = _build_mask_chain(
            shape.subpaths, to_point, to_vector, comp_w, comp_h, stroke_end, stack_slot, names, positions,
            invert_chain=False, border_width=border_width,
            name_for=lambda i: f"{shape.name}_StrokeMask" if i == 0 else f"{shape.name}_StrokeSub{i + 1}Mask",
            level=stroke_level, soft_edge=soft_edge, initial_effect_mask=external_clip)
        tools.extend(mask_tools)
        stroke_bg_name = names.safe_name(f"{shape.name}_Stroke", "Shape")
        stroke_bg_pos, stroke_bg_slot = positions.claim(stroke_end, stack_slot)
        if shape.stroke_gradient is not None:
            tools.append(_build_gradient_background_lua(stroke_bg_name, comp_w, comp_h, shape.stroke_gradient,
                                                          to_point, prev_mask, stroke_bg_pos))
        else:
            color = shape.stroke_color or (0.5, 0.5, 0.5, 1.0)  # unparsed stroke placeholder
            r, g, b, a = color
            a = a * shape.stroke_opacity
            color_exprs = color_registry.lookup(shape, "stroke") if color_registry else None
            alpha_source_op = None
            if layer_opacity_anim is not None:
                alpha_name = names.safe_name(f"{shape.name}_StrokeAlpha", "BezierSpline")
                specs = _lottie_keyframe_specs(layer_opacity_anim, value_fn=lambda o, mult=a: (o / 100.0) * mult,
                                                 unwrap_scalar=True, time_scale=time_scale)
                tools.append(_build_bezier_spline_lua(alpha_name, specs))
                alpha_source_op = alpha_name
            tools.append(_build_background_lua(stroke_bg_name, comp_w, comp_h, r, g, b, a, prev_mask, stroke_bg_pos,
                                                 color_exprs=color_exprs, alpha_source_op=alpha_source_op))

    if both:
        merge_name = names.safe_name(shape.name, "Shape")
        merge_pos, merge_slot = positions.claim(content_flow_level, stack_slot)
        tools.append(build_merge_lua(merge_name, fill_bg_name, stroke_bg_name, merge_pos))
        output_name, output_slot = merge_name, merge_slot
    elif shape.has_fill:
        output_name, output_slot = fill_bg_name, fill_bg_slot
    else:
        output_name, output_slot = stroke_bg_name, stroke_bg_slot
    # Tracks whichever flow_level the CURRENT output_name actually landed
    # on, through however many of the shadow/inner-shadow steps below
    # actually ran, so the final pivot Transform (see build_shape_lua's own
    # docstring) knows where to claim the next free column downstream of
    # whatever this shape's real last step turns out to be.
    last_flow_level = content_flow_level

    # Whichever of fill_end/stroke_end already goes furthest back -- both
    # the drop shadow and every inner-shadow layer reserve their own space
    # behind that, one after another, so none collide with content's own
    # masks or with each other.
    deepest = stroke_end if both else fill_end

    if has_shadow:
        # A drop shadow silhouettes the SAME subpaths as the fill (it
        # covers the whole shape, fill and stroke both) -- reserve n+1
        # flow steps for its own mask chain.
        shadow_end = deepest - (n + 1)
        deepest = shadow_end
        shadow_tools, shadow_bg_name = _build_drop_shadow_lua(
            shape, to_point, to_vector, comp_w, comp_h, shadow_end, stack_slot, names, positions,
            external_clip=external_clip)
        tools.extend(shadow_tools)
        shadow_merge_name = names.safe_name(shape.name + "_Shadow", "Shape")
        shadow_merge_pos, shadow_merge_slot = positions.claim(flow_level, output_slot)
        tools.append(build_merge_lua(shadow_merge_name, shadow_bg_name, output_name, shadow_merge_pos))
        output_name, output_slot = shadow_merge_name, shadow_merge_slot
        last_flow_level = flow_level

    if shape.inner_shadow:
        # Each layer composites ON TOP of the previous result (drawn
        # inside the shape, unlike a drop shadow behind it) -- matching
        # the browser's own successive "blend onto shape" accumulation,
        # see parse_inner_shadow_filter_defs.
        for idx, layer in enumerate(shape.inner_shadow):
            layer_end = deepest - (3 * (n + 1))
            deepest = layer_end
            layer_tools, layer_bg_name = _build_inner_shadow_layer_lua(
                shape, layer, to_point, to_vector, comp_w, comp_h, layer_end, stack_slot, names, positions,
                suffix=str(idx + 1), external_clip=external_clip)
            tools.extend(layer_tools)
            layer_merge_name = names.safe_name(f"{shape.name}_InnerShadow{idx + 1}", "Shape")
            layer_merge_pos, layer_merge_slot = positions.claim(flow_level + idx + 1, output_slot)
            tools.append(build_merge_lua(layer_merge_name, output_name, layer_bg_name, layer_merge_pos))
            output_name, output_slot = layer_merge_name, layer_merge_slot
            last_flow_level = flow_level + idx + 1

    # Every shape gets its own pivot, wrapping WHATEVER ended up as its
    # final output above (plain fill, fill+stroke merge, drop shadow,
    # inner shadow -- doesn't matter which) in one more Transform -- see
    # this file's own module comment right before _shape_pivot_point.
    pivot_name = names.safe_name(f"{shape.name}_Pivot", "Transform")
    pivot_flow = last_flow_level + 1
    pivot_pos, pivot_slot = positions.claim(pivot_flow, output_slot)
    tools.append(_build_transform_lua(pivot_name, _shape_pivot_point(shape, to_point), output_name, pivot_pos))
    output_name, output_slot = pivot_name, pivot_slot

    return BuiltGraph(tools, output_name, 1, output_slot, output_flow=pivot_flow)


def _build_drop_shadow_lua(shape, to_point, to_vector, comp_w, comp_h, end_flow_level, stack_slot,
                            names, positions, external_clip=None):
    """Builds a drop shadow's own mask+Background pair: the SAME subpaths
    as the shape's own fill (a drop shadow silhouettes the whole shape),
    fed through a POSITION-SHIFTED to_point rather than shifted SVG-space
    coordinates -- exact, not an approximation, since to_point is affine:
    to_point(x + dx, y + dy) == to_point(x, y) + to_vector(dx, dy) for a
    pure translation. Only the anchors shift; bezier handle deltas (already
    relative offsets, run through to_vector unmodified elsewhere) don't
    need shifting at all.

    external_clip (an ancestor SVG <mask>, see build_group_lua) applies to
    the shadow too -- if the shape itself is clipped, whatever casts its
    shadow should be too, same as a real renderer clipping the whole
    filtered+masked group together."""
    ds = shape.drop_shadow
    offset_x, offset_y = to_vector(ds["dx"], ds["dy"])

    def shifted_to_point(x, y):
        px, py = to_point(x, y)
        return px + offset_x, py + offset_y

    soft_edge = to_vector(ds["stddev"], 0.0)[0] if ds["stddev"] else None
    mask_tools, prev_mask = _build_mask_chain(
        shape.subpaths, shifted_to_point, to_vector, comp_w, comp_h, end_flow_level, stack_slot, names, positions,
        invert_chain=True, border_width=None, force_closed=True,
        name_for=lambda i: f"{shape.name}_ShadowMask" if i == 0 else f"{shape.name}_ShadowSub{i + 1}_Mask",
        soft_edge=soft_edge, initial_effect_mask=external_clip)
    bg_name = names.safe_name(f"{shape.name}_Shadow", "Shape")
    bg_pos, _ = positions.claim(end_flow_level, stack_slot)
    r, g, b = ds["color"]
    # The shape's own overall opacity applies to its filter's SourceAlpha
    # input too, per spec (opacity composites BEFORE filters run) -- so the
    # shadow's alpha is the filter's own recolor multiplier TIMES the
    # shape's opacity, not the multiplier alone.
    a = ds["opacity"] * shape.opacity
    mask_tools.append(_build_background_lua(bg_name, comp_w, comp_h, r, g, b, a, prev_mask, bg_pos))
    return mask_tools, bg_name


def _build_inner_shadow_layer_lua(shape, layer, to_point, to_vector, comp_w, comp_h, end_flow_level,
                                   stack_slot, names, positions, suffix, external_clip=None):
    """One inner-shadow layer: the shape's own UNDISPLACED silhouette
    (base), minus a shifted+blurred copy of that SAME silhouette (band),
    built in THREE mask stages rather than two -- see the "leak" paragraph
    below for why a direct two-stage Subtract isn't safe.

    Two pitfalls shape this, in order:

    (1) Feeding the shifted+blurred copy directly into a Subtract against
    base leaks a visible halo past the shape's own edge -- SVG's own
    feComposite arithmetic k2=-1 k3=1 (hardAlpha - blurredOffsetAlpha, see
    parse_inner_shadow_filter_defs) clamps negative results to 0 by spec,
    so it can never show anything outside hardAlpha, but Fusion's own
    "Subtract" doesn't clamp the same way outside the two masks' overlap.
    So the shifted copy is pre-clipped against base via Multiply
    first (the same operation the mask-as-clip feature uses) into a
    middle "clip" mask -- since clip is a SUBSET of base by construction
    (Multiply is a true AND), there's no pixel where clip > 0 and base
    == 0 for whatever Subtract does out there to ever matter.

    (2) A Subtract after that clip still gives a band mask that is ALWAYS
    EMPTY (toggling the clip mask upstream makes no difference downstream
    at all). The reason: Subtract's own docs read
    "the new mask's values subtract FROM the input mask's" -- i.e.
    result = input - new, where "new" is THIS mask's own drawn Polyline
    and "input" is whatever's fed via EffectMask. A band mask built
    with base as its OWN shape ("new") and clip as EffectMask ("input")
    computes clip - base -- negative everywhere, since clip subset-of
    base, clamped to 0 always. There's no way to make an ALREADY-COMPUTED
    mask (clip) play the "new"/own-shape role instead, since that role
    can only ever be a literal drawn Polyline.

    So this sidesteps Subtract entirely: A xor B == A - B whenever B is a SUBSET of A, and "Invert"
    (XOR) is the ONE combine mode this whole file already relies on
    successfully -- every compound-path hole in every fill/stroke chain is
    punched this exact way. Since clip subset-of base is already
    guaranteed by (1), passing initial_invert=True (not
    initial_paint_mode="Subtract") for the band mask computes
    XOR(clip, base) = base - clip using only mechanisms already proven
    correct, rather than trusting a second unverified one."""
    n = len(shape.subpaths)
    base_end = end_flow_level - (3 * (n + 1))
    base_tools, base_mask = _build_mask_chain(
        shape.subpaths, to_point, to_vector, comp_w, comp_h, base_end, stack_slot, names, positions,
        invert_chain=True, border_width=None, force_closed=True,
        name_for=lambda i: f"{shape.name}_InnerBase{suffix}" if i == 0 else f"{shape.name}_InnerBase{suffix}Sub{i + 1}",
        initial_effect_mask=external_clip)

    offset_x, offset_y = to_vector(layer["dx"], layer["dy"])

    def shifted_to_point(x, y):
        px, py = to_point(x, y)
        return px + offset_x, py + offset_y

    soft_edge = to_vector(layer["stddev"], 0.0)[0] if layer["stddev"] else None
    clip_end = end_flow_level - (2 * (n + 1))
    clip_tools, clipped_shifted = _build_mask_chain(
        shape.subpaths, shifted_to_point, to_vector, comp_w, comp_h, clip_end, stack_slot, names, positions,
        invert_chain=True, border_width=None, force_closed=True,
        name_for=lambda i: f"{shape.name}_InnerShift{suffix}" if i == 0 else f"{shape.name}_InnerShift{suffix}Sub{i + 1}",
        soft_edge=soft_edge, initial_effect_mask=base_mask, initial_paint_mode="Multiply")

    band_end = end_flow_level - (n + 1)
    band_tools, band_mask = _build_mask_chain(
        shape.subpaths, to_point, to_vector, comp_w, comp_h, band_end, stack_slot, names, positions,
        invert_chain=True, border_width=None, force_closed=True,
        name_for=lambda i: f"{shape.name}_InnerShadow{suffix}" if i == 0 else f"{shape.name}_InnerShadow{suffix}Sub{i + 1}",
        initial_effect_mask=clipped_shifted, initial_invert=True)

    bg_name = names.safe_name(f"{shape.name}_InnerShadow{suffix}", "Shape")
    bg_pos, _ = positions.claim(end_flow_level, stack_slot)
    r, g, b = layer["color"]
    # see _build_drop_shadow_lua's identical opacity note re: shape.opacity;
    # INNER_SHADOW_INTENSITY is a separate manual dial, see its own comment.
    a = layer["opacity"] * shape.opacity * INNER_SHADOW_INTENSITY
    tools = base_tools + clip_tools + band_tools
    tools.append(_build_background_lua(bg_name, comp_w, comp_h, r, g, b, a, band_mask, bg_pos))
    return tools, bg_name


def build_merge_lua(name, bg_op, fg_op, pos, bg_source="Output", fg_source="Output"):
    """bg_source/fg_source are which pin to read on bg_op/fg_op -- "Output"
    for an ordinary tool, "Output1" when that op is a GroupOperator (see
    BuiltGraph.output_source). Getting this wrong doesn't error; Fusion just
    drops that one Input silently, so callers MUST pass the referenced
    BuiltGraph's own output_source here rather than assuming "Output"."""
    return f"""{name} = Merge {{
        Inputs = {{
            Background = Input {{ SourceOp = "{bg_op}", Source = "{bg_source}" }},
            Foreground = Input {{ SourceOp = "{fg_op}", Source = "{fg_source}" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def _wrap_matte_clip(node, content_output_name, output_slot, last_flow_level, content_output_source,
                      to_point, to_vector, comp_w, comp_h, names, positions, slot_counter):
    """Crops one layer's already-assembled content to its track matte --
    a mask chain over the matte's resolved-static subpaths, a transparent
    Background, and one Merge combining them (see _build_clip_merge_lua).
    Returns (new_tools, output_name, output_slot, output_flow)."""
    tools = []
    clip_flow = last_flow_level + 1

    mask_tools, mask_name = _build_mask_chain(
        node.matte_clip_subpaths, to_point, to_vector, comp_w, comp_h, clip_flow, output_slot,
        names, positions, invert_chain=True, border_width=None, force_closed=True,
        name_for=lambda i: (f"{node.name}_MatteMask" if i == 0
                             else f"{node.name}_MatteSub{i + 1}_Mask"))
    tools.extend(mask_tools)

    clip_bg_name = names.safe_name(node.name + "_MatteBackground", "BaseBackground")
    clip_bg_pos, clip_bg_slot = positions.claim(clip_flow, slot_counter[0])
    slot_counter[0] = clip_bg_slot + 1
    tools.append(build_base_background_lua(clip_bg_name, comp_w, comp_h, clip_bg_pos))

    clip_name = names.safe_name(node.name + "_MatteClip", "Merge")
    clip_pos, clip_slot = positions.claim(clip_flow, output_slot)
    tools.append(_build_clip_merge_lua(clip_name, clip_bg_name, content_output_name, mask_name,
                                        clip_pos, bg_source="Output", fg_source=content_output_source))
    return tools, clip_name, clip_slot, clip_flow


def _build_clip_merge_lua(name, bg_op, fg_op, mask_op, pos, bg_source="Output", fg_source="Output"):
    """Crops an already-assembled layer to a track matte: the same
    transparent-Background + content-Foreground Merge the existence gate
    uses, but with the matte's mask chain wired to EffectMask instead of
    an animated Blend. Outside the mask the merge doesn't happen, so the
    output there is the transparent Background; inside it, the content --
    i.e. content cropped to the matte.

    EffectMask on a MERGE specifically is not dump-confirmed, though
    EffectMask itself is used and confirmed working throughout this file
    (every shape's own fill/stroke mask chain feeds one on a Background)."""
    return f"""{name} = Merge {{
        Inputs = {{
            Background = Input {{ SourceOp = "{bg_op}", Source = "{bg_source}" }},
            Foreground = Input {{ SourceOp = "{fg_op}", Source = "{fg_source}" }},
            EffectMask = Input {{ SourceOp = "{mask_op}", Source = "Mask" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def _build_gate_merge_lua(name, bg_op, fg_op, blend_source_op, pos, bg_source="Output", fg_source="Output"):
    """Like build_merge_lua, but ALSO wires Blend to an animated
    BezierSpline -- used ONLY for a Lottie layer's own existence gate
    (see build_group_lua's own visibility-window wrap /
    _lottie_visibility_gate_keyframes): rather than driving each of a
    gated layer's own shapes' fill/stroke alpha independently
    (duplicating a spline per shape), this wraps the WHOLE already-assembled, already-
    positioned layer in ONE dedicated Merge against a transparent
    background, animating that Merge's own Blend 0->1 at the layer's
    own "ip" and 1->0 at its own "op" -- Blend=0 shows only Background
    (nothing), Blend=1 shows the real Foreground, exactly reproducing
    Lottie's own hard existence on/off with a single extra tool per
    gated layer instead of one per shape.

    UNCONFIRMED against a real dump -- Blend is a standard, well-known
    Merge control (present on every Merge's own Inspector, not an
    obscure or ambiguous field the way Transform's Center/Pivot is),
    but this exact generated wiring (SourceOp-driven, animated) hasn't
    been dump-verified: the Merge's own Blend slider should show the
    animated curve and dissolve the layer in/out at the right frame."""
    return f"""{name} = Merge {{
        Inputs = {{
            Background = Input {{ SourceOp = "{bg_op}", Source = "{bg_source}" }},
            Foreground = Input {{ SourceOp = "{fg_op}", Source = "{fg_source}" }},
            Blend = Input {{ SourceOp = "{blend_source_op}", Source = "Value" }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def build_base_background_lua(name, comp_w, comp_h, pos):
    """A fully transparent Background sized to the comp -- confirmed against
    a real native-import dump: native always has exactly this (Width/Height
    literal to the comp, TopLeftAlpha=0, no color inputs at all) sitting
    underneath everything as the Merge's Background input, with the actual
    SVG content as Foreground."""
    return f"""{name} = Background {{
        Inputs = {{
            Width = Input {{ Value = {comp_w!r} }},
            Height = Input {{ Value = {comp_h!r} }},
            TopLeftAlpha = Input {{ Value = 0.0 }},
        }},
        ViewInfo = OperatorInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
    }}"""


def build_group_lua(node, to_point, to_vector, comp_w, comp_h, depth, slot_counter, names, positions,
                     is_root=False, external_clip=None, color_registry=None, layer_opacity_anim=None,
                     report=None, lottie_comp_size=None, recenter_overall_pivot=False, time_scale=1.0,
                     lottie_comp_span=None):
    """slot_counter is a mutable [next_slot] shared between siblings at this
    recursion level, so they stack one after another instead of sprawling
    out in traversal order. depth pushes nested content further downstream.
    Everything advances the counter by however many slots it actually
    occupies (see PastePositions), not by one per child.

    Returns a BuiltGraph.

    Each Merge is placed at the slot its FOREGROUND input already occupies,
    one flow step downstream, rather than on the next free slot. That's what
    keeps the wiring axis-aligned: the incoming shape's fill runs dead
    straight along the flow axis into the Merge, and because consecutive
    Merges all share one flow column, the accumulating chain between them
    runs dead straight along the stack axis too. Taking the next free slot
    instead would put every Merge below everything built so
    far, so each shape's fill would reach it on a diagonal.

    One diagonal is unavoidable and remains: the very first Merge in a chain
    has a real shape on BOTH inputs, and they sit on different slots, so
    whichever one it aligns with, the other arrives at an angle. Every
    Merge after that has the previous Merge as its Background, which is
    already in the same flow column, hence straight.

    is_root: only true for the single outermost call from svg_to_lua --
    that's where the base transparent Background + final Merge get added
    (matching native import's own structure), not on every nested <g>.

    Every <g> is ALWAYS dissolved here -- no per-<g> GroupOperator is ever
    created, because a sibling Merge referencing a NESTED GroupOperator's
    output gets silently dropped by Fusion's clipboard-paste resolver --
    every Merge Input pointing at a GroupOperator vanishes (under the
    wrong pin name, and under the group's own correct "Output1" name
    too), while every Input pointing at a plain tool survives untouched. See svg_to_lua for
    how the paste still gets its one tidy box: by wrapping this function's
    entirely flat result in exactly ONE outer GroupOperator afterwards,
    which only ever has a group reading its OWN child (already proven
    safe) rather than a sibling Merge reaching into a nested one.

    external_clip: an already-built SVG <mask>/<clipPath> clip chain
    inherited from an ancestor <g mask="...">/<g clip-path="...">, threaded
    down to every descendant ShapeNode's own fill/stroke (see
    build_shape_lua). If THIS node carries its own clip_subpaths (its own
    mask/clip-path, not an ancestor's), that takes over for everything under
    it instead of the inherited one -- see _resolve_ref_clip_subpaths for
    why silently dropping the mask (previous behavior) was wrong, not just
    imprecise: it let stroke/fill spill past the shape the SVG meant to crop
    it to.

    layer_opacity_anim/report: the Lottie animated export's
    animated-opacity/approximation-tally plumbing (see build_shape_lua's own layer_opacity_anim
    docstring) -- None for every SVG call and every Lottie node that isn't
    a real animated layer. Set once, at the GroupNode that actually owns
    the animation (node.layer_animation is only ever non-None on a Lottie
    layer's own top-level group -- see lottie_layer_to_group_node), then
    passed UNCHANGED to every recursive call so it reaches every
    descendant ShapeNode including ones inside a dissolved shape-group.

    lottie_comp_size: (width, height) of the LOTTIE composition itself
    (None for every SVG call) -- needed only when actually wrapping a
    layer's animated Transform, since Center (unlike every other field)
    is a comp-absolute Position needing _lottie_center_point's own
    recentering, not the layer-local convention everything else here
    uses.

    time_scale: fusion_comp_fps / lottie_source_fps, None-safe default 1.0
    for every SVG call -- see build_lottie_tools/_lottie_keyframe_specs.
    Threaded unchanged to every recursive call and into every place a raw
    Lottie frame number reaches a Fusion KeyFrames table (layer/ancestor
    Transform wraps, per-shape opacity splines)."""
    if isinstance(node, ShapeNode):
        built = build_shape_lua(node, to_point, to_vector, comp_w, comp_h,
                                depth, slot_counter[0], names, positions, external_clip=external_clip,
                                color_registry=color_registry, layer_opacity_anim=layer_opacity_anim,
                                time_scale=time_scale)
        slot_counter[0] += built.slots
        return built

    inner_tools, output_name, output_slot, output_source, output_flow = [], None, None, "Output", depth
    start_slot = slot_counter[0]

    anim = node.layer_animation
    own_opacity_anim = layer_opacity_anim
    if anim is not None and _lottie_field_is_really_animated(anim.opacity):
        own_opacity_anim = anim.opacity

    own_clip = external_clip
    if node.clip_subpaths:
        clip_tools, clip_mask_name = _build_mask_chain(
            node.clip_subpaths, to_point, to_vector, comp_w, comp_h, depth, slot_counter[0], names, positions,
            invert_chain=True, border_width=None, force_closed=True,
            name_for=lambda i: f"{node.name}_ClipMask" if i == 0 else f"{node.name}_ClipSub{i + 1}_Mask")
        inner_tools.extend(clip_tools)
        slot_counter[0] += 1
        own_clip = clip_mask_name

    children_built = []
    for child in node.children:
        built = build_group_lua(child, to_point, to_vector, comp_w, comp_h,
                                 depth, slot_counter, names, positions, external_clip=own_clip,
                                 color_registry=color_registry, layer_opacity_anim=own_opacity_anim,
                                 report=report, lottie_comp_size=lottie_comp_size, time_scale=time_scale,
                                 lottie_comp_span=lottie_comp_span)
        inner_tools.extend(built.tools)
        if built.output_name is not None:
            children_built.append(built)

    if not children_built:
        return BuiltGraph([], None, 0, None)

    # Every sibling Merge at this level shares ONE flow row -- one step
    # past whichever child's own chain ran deepest -- rather than each
    # new Merge landing one step past the PREVIOUS Merge. That compounding
    # layout is also collision-safe (see BuiltGraph.output_flow's own
    # comment) but turns a long sibling list into a diagonal staircase
    # spanning as many rows as there are siblings, whereas a hand-built
    # graph keeps every sibling Merge flat on one row. The flat row works
    # by building all children FIRST (so every one of their own
    # output_flow values is known) before placing any Merge, instead of
    # folding merges in one at a time as children are built.
    merge_flow = max([depth] + [b.output_flow for b in children_built]) + 1
    output_name, output_slot, output_source = (
        children_built[0].output_name, children_built[0].output_slot, children_built[0].output_source)
    for built in children_built[1:]:
        # Line the Merge up with the Foreground it's bringing in, so that
        # connection is a straight run along the flow axis.
        merge_name = names.safe_name(node.name + "_Merge", "Merge")
        merge_pos, merge_slot = positions.claim(merge_flow, built.output_slot)
        inner_tools.append(build_merge_lua(merge_name, output_name, built.output_name, merge_pos,
                                            bg_source=output_source, fg_source=built.output_source))
        output_name, output_slot, output_source = merge_name, merge_slot, "Output"
    output_flow = merge_flow if len(children_built) > 1 else children_built[0].output_flow

    if anim is not None and (_layer_has_transform_animation(anim) or node.ancestor_animations
                              or node.needs_live_transform):
        lottie_comp_w, lottie_comp_h = lottie_comp_size
        # Own transform first (closest to the content), THEN each AE
        # ancestor in turn (nearest parent first, root last) -- matching
        # true matrix composition order (child_world = parent ∘ child) by
        # chaining one Fusion Transform tool per level, each wrapping the
        # previous one's output. A layer with no REAL animation of its
        # own still needs this when it HAS ancestors: its own ks was
        # already baked into geometry (see _bake_lottie_layer_pose), so
        # only the ancestor chain's contribution is missing.
        if _layer_has_transform_animation(anim) or node.needs_live_transform:
            wrap_tools, output_name, output_slot, output_flow = _wrap_layer_transform(
                anim, node.name, output_name, output_slot, output_flow, lottie_comp_w, lottie_comp_h,
                to_point, to_vector, names, positions, report, time_scale=time_scale,
                space=node.transform_space or _LOTTIE_SPACE_OWN_LAYER)
            inner_tools.extend(wrap_tools)
        for depth_idx, ancestor_anim in enumerate(node.ancestor_animations):
            # A track matte shares this layer's own composition, so the clip
            # has to go in once the same-comp part of the chain is done but
            # BEFORE any enclosing precomp instance's transform -- otherwise
            # the instance would move the content out from under a mask that
            # stayed put. See GroupNode.matte_clip_after.
            if node.matte_clip_subpaths and depth_idx == node.matte_clip_after:
                clip_tools, output_name, output_slot, output_flow = _wrap_matte_clip(
                    node, output_name, output_slot, output_flow, output_source,
                    to_point, to_vector, comp_w, comp_h, names, positions, slot_counter)
                inner_tools.extend(clip_tools)
                output_source = "Output"
            wrap_tools, output_name, output_slot, output_flow = _wrap_layer_transform(
                ancestor_anim, f"{node.name}_Parent{depth_idx + 1}", output_name, output_slot, output_flow,
                lottie_comp_w, lottie_comp_h, to_point, to_vector, names, positions, report,
                time_scale=time_scale, space=_LOTTIE_SPACE_ANCESTOR)
            inner_tools.extend(wrap_tools)
        output_source = "Output"
        # matte_clip_after can equal the full ancestor count (no enclosing
        # precomp instance at all), in which case the clip lands after the
        # whole chain rather than inside the loop above.
        if node.matte_clip_subpaths and node.matte_clip_after >= len(node.ancestor_animations):
            clip_tools, output_name, output_slot, output_flow = _wrap_matte_clip(
                node, output_name, output_slot, output_flow, output_source,
                to_point, to_vector, comp_w, comp_h, names, positions, slot_counter)
            inner_tools.extend(clip_tools)
            output_source = "Output"

    # A layer only visible for part of the timeline (its own real ip/op,
    # or a nested precomp instance's own visible window narrowing it --
    # see _lottie_layer_window) gets wrapped in ONE dedicated existence
    # Merge here, on top of its own already-fully-positioned content --
    # rather than duplicating an alpha spline onto every one of this
    # layer's own shapes. See
    # _lottie_visibility_gate_keyframes/_build_gate_merge_lua.
    if (node.visibility_window is not None and lottie_comp_span is not None
            and _lottie_gate_needed(node.visibility_window, lottie_comp_span)):
        gate_keyframes = _lottie_visibility_gate_keyframes(node.visibility_window, lottie_comp_span)
        gate_specs = _lottie_keyframe_specs(gate_keyframes, value_fn=lambda v: v, force_linear=True,
                                              unwrap_scalar=True, time_scale=time_scale)
        gate_flow = output_flow + 1

        gate_blend_name = names.safe_name(node.name + "_ExistenceBlend", "BezierSpline")
        inner_tools.append(_build_bezier_spline_lua(gate_blend_name, gate_specs))

        gate_bg_name = names.safe_name(node.name + "_ExistenceBackground", "BaseBackground")
        gate_bg_pos, gate_bg_slot = positions.claim(gate_flow, slot_counter[0])
        slot_counter[0] = gate_bg_slot + 1
        inner_tools.append(build_base_background_lua(gate_bg_name, comp_w, comp_h, gate_bg_pos))

        gate_merge_name = names.safe_name(node.name + "_ExistenceMerge", "Merge")
        gate_merge_pos, gate_merge_slot = positions.claim(gate_flow, output_slot)
        inner_tools.append(_build_gate_merge_lua(gate_merge_name, gate_bg_name, output_name, gate_blend_name,
                                                   gate_merge_pos, bg_source="Output", fg_source=output_source))
        output_name, output_slot, output_source, output_flow = gate_merge_name, gate_merge_slot, "Output", gate_flow
        if report is not None:
            report.note_visibility_gated_layer()

    if is_root:
        # The transparent base sits on its own slot past everything else; the
        # final Merge lines up with it so THAT connection is straight too.
        base_bg_name = names.safe_name(node.name + "_BaseBackground", "BaseBackground")
        base_bg_pos, base_bg_slot = positions.claim(depth, slot_counter[0])
        slot_counter[0] = base_bg_slot + 1
        inner_tools.append(build_base_background_lua(base_bg_name, comp_w, comp_h, base_bg_pos))

        # Same "one past whichever side ran deeper" reasoning as the sibling
        # Merge above -- base_bg is always fresh (flow=depth, never collided
        # with anything), but output_flow might already be past depth+1 if
        # the accumulated content's own tail (e.g. its last shape's pivot)
        # landed further out than a single flow step from depth.
        final_merge_flow = max(depth, output_flow) + 1
        final_merge_name = names.safe_name(node.name + "_FinalMerge", "FinalMerge")
        final_merge_pos, final_merge_slot = positions.claim(final_merge_flow, output_slot)
        inner_tools.append(build_merge_lua(final_merge_name, base_bg_name, output_name, final_merge_pos,
                                            bg_source="Output", fg_source=output_source))
        output_name, output_slot, output_source, output_flow = final_merge_name, final_merge_slot, "Output", final_merge_flow

        # One more pivot, on top of every individual shape's own -- see the
        # module comment right before _weighted_pivot_point for why.
        overall_pivot_flow = output_flow + 1
        overall_pivot_name = names.safe_name(node.name + "_OverallPivot", "Transform")
        overall_pivot_pos, overall_pivot_slot = positions.claim(overall_pivot_flow, output_slot)
        overall_pivot_point = _weighted_pivot_point(node, to_point, lottie_comp_size=lottie_comp_size)
        if recenter_overall_pivot:
            inner_tools.append(_build_recentering_transform_lua(
                overall_pivot_name, overall_pivot_point, output_name, overall_pivot_pos))
        else:
            inner_tools.append(_build_transform_lua(overall_pivot_name, overall_pivot_point,
                                                      output_name, overall_pivot_pos))
        output_name, output_slot, output_source, output_flow = (
            overall_pivot_name, overall_pivot_slot, "Output", overall_pivot_flow)

    return BuiltGraph(inner_tools, output_name, slot_counter[0] - start_slot, output_slot, output_source, output_flow)


def build_single_group_wrapper_lua(group_name, inner_tools, output_name, output_source, pos):
    """Wraps an ALREADY-FLAT tool list (build_group_lua's own result, which
    never contains a GroupOperator) in exactly one outer GroupOperator --
    called once, at the very top, for every paste (see svg_to_lua/
    build_svgs_to_lua -- there's no ungrouped-paste option). The
    only Group-related reference this creates is this wrapper's
    own Outputs mapping reading its OWN internal FinalMerge -- never a
    sibling Merge reaching into a nested group, which is the specific
    pattern confirmed broken in Fusion's clipboard-paste (see
    build_group_lua)."""
    inner_text = ",\n            ".join(inner_tools)
    return f"""{group_name} = GroupOperator {{
        Outputs = {{
            Output1 = InstanceOutput {{ SourceOp = "{output_name}", Source = "{output_source}" }},
        }},
        ViewInfo = GroupInfo {{ Pos = {{ {pos[0]!r}, {pos[1]!r} }} }},
        Tools = ordered() {{
            {inner_text}
        }},
    }}"""


def get_svg_viewbox(root):
    """Returns (min_x, min_y, vb_w, vb_h, declared_w, declared_h).

    viewBox="10 20 100 100" has its own origin at (10, 20), NOT (0, 0) --
    ignoring that offset would shift every shape by however far the viewBox
    is from the origin.

    vb_w/vb_h are the viewBox's own coordinate-system extent, the units path
    data is actually authored in. declared_w/declared_h are the width=/
    height= attributes -- the SVG's stated on-screen pixel size, which
    routinely DIFFERS from the viewBox: viewBox="0 0 1024 1024" with
    width="800" height="800" is a common icon-library export (a clean round
    authoring coordinate system paired with an arbitrary display size).
    Returning only the viewBox would silently treat those 1024 units as
    1024 pixels -- rendering such an SVG 1024/800 = 1.28x too large under
    "native 1:1" scale, and fitting the wrong size to the frame under
    "Timeline" too. Callers need both: vb_w/vb_h to center
    path coordinates in their own authored space, declared_w/declared_h as
    the actual pixel dimensions everything else (native-scale sizing,
    Timeline fit-to-comp, the smaller/larger-than-comp hints) means by
    "how big is this"."""
    vb_w = vb_h = None
    min_x = min_y = 0.0
    vb = root.get('viewBox')
    if vb:
        parts = [float(v) for v in _NUM_RE.findall(vb)]
        # A zero/negative extent is a spec error (disables rendering) --
        # ignored here like a missing viewBox rather than dividing by it.
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            min_x, min_y, vb_w, vb_h = parts

    def num(s):
        # Real units, not stripped ones: width="210mm" is ~793.7px, not 210.
        # A percentage (width="100%") has no outer viewport to be OF here, so
        # it reads as not declared -- same as a missing attribute.
        v = svg_length_px(s)
        return v if v is not None and v > 0 else None

    declared_w, declared_h = num(root.get('width')), num(root.get('height'))
    if vb_w is None:
        vb_w, vb_h = (declared_w or 100.0), (declared_h or 100.0)
    # Only one side declared (the other missing or a percentage): derive it
    # from the viewBox's aspect ratio, the way a browser sizes it -- left
    # as None it would crash the "SVG canvas" log line.
    if declared_w is None and declared_h is None:
        declared_w, declared_h = vb_w, vb_h
    elif declared_h is None:
        declared_h = declared_w * vb_h / vb_w
    elif declared_w is None:
        declared_w = declared_h * vb_w / vb_h
    return min_x, min_y, vb_w, vb_h, declared_w, declared_h


def build_svg_tools(svg_path, comp_w, comp_h, log=None, orientation="horizontal", scale_mode="native",
                     origin=(0.0, 0.0), consolidate_colors=False):
    """Parses svg_path and returns (tools, label, tool_count) -- tools is a
    ONE-ELEMENT list containing this file's single top-level GroupOperator,
    as a Lua snippet NOT YET wrapped in the outer
    "{ Tools = ordered() { ... } } }" paste-text envelope; label is that
    group's own name. Factored out of svg_to_lua (which just wraps this
    single-file result) so build_svgs_to_lua can concatenate several files'
    groups into ONE envelope/clipboard payload -- see there.

    Every <g> in the SVG is dissolved (see build_group_lua) and the whole,
    entirely flat result gets wrapped in exactly one outer GroupOperator
    (see build_single_group_wrapper_lua) -- there's no ungrouped-paste
    option. Fusion's clipboard-paste doesn't reliably preserve a Merge's
    connection to a GroupOperator it doesn't already know about, which
    rules out mirroring the SVG's own <g> nesting as real nested groups,
    but not the one-flat-group-per-file wrapping itself.

    scale_mode is "native" (the SVG's own pixel size, matching Resolve's own
    importer) or "timeline" (scaled to fit the comp) -- see
    compute_svg_scale.

    origin shifts every position in this paste by a flat (x, y) -- used by
    the batch importer to fan multiple files out across the Flow view
    instead of stacking each new paste on top of the last one at (0, 0)."""
    def say(msg):
        if log:
            log(msg)

    tree = ET.parse(svg_path)
    root = tree.getroot()
    min_x, min_y, vb_w, vb_h, svg_w, svg_h = get_svg_viewbox(root)
    say(f"SVG canvas: {svg_w:g} x {svg_h:g} (origin offset {min_x:g}, {min_y:g}); "
        f"comp is {comp_w}x{comp_h}")

    # A viewBox extent that doesn't match the declared width/height (see
    # get_svg_viewbox) needs an extra unit-to-pixel factor on top of
    # whatever native/Timeline scaling applies below -- 1 viewBox unit is
    # worth svg_w/vb_w declared pixels, not 1.
    unit_scale = (svg_w / vb_w) if vb_w else 1.0
    if abs(unit_scale - 1.0) > 1e-9:
        say(f"[NOTE] viewBox is {vb_w:g} x {vb_h:g}, different from the declared "
            f"{svg_w:g} x {svg_h:g} -- scaling paths by {unit_scale:.4f} to compensate.")

    fit_scale = compute_svg_scale(scale_mode, svg_w, svg_h, comp_w, comp_h)
    scale = unit_scale * fit_scale
    if scale_mode == "timeline":
        say(f"Scale: timeline fit -- x{fit_scale:.3f}, drawing {svg_w * fit_scale:.0f} x "
            f"{svg_h * fit_scale:.0f} px ({TIMELINE_FIT_MARGIN:.0%} of the frame on its "
            f"limiting axis, proportions kept).")
    else:
        say(f"Scale: native 1:1 -- drawing {svg_w:g} x {svg_h:g} px, the SVG's own size "
            f"(this is what Resolve's own SVG import does too).")
        # Only worth flagging in native mode; timeline mode fixes it by definition.
        if svg_w < comp_w / 3 or svg_h < comp_h / 3:
            say("[NOTE] That's small relative to the comp -- switch Scale to 'Timeline' "
                "if you'd rather it filled the frame.")
        elif svg_w > comp_w or svg_h > comp_h:
            say("[NOTE] That's larger than the comp, so it'll overflow the frame -- "
                "switch Scale to 'Timeline' to fit it instead.")

    to_point, to_vector = make_converter(min_x, min_y, vb_w, vb_h, comp_w, comp_h, scale=scale)
    counter = iter(range(1, 100000))
    # A short unique suffix on EVERY tool name in this file, inner content
    # included -- not just the group's own name. Plain inner names
    # (Shape_2, Shape_2_Mask, ...) aren't safe even though the group looks
    # like their namespace: pasting several files' groups together in ONE
    # clipboard payload (see build_svgs_to_lua), Fusion's paste parser
    # doesn't scope SourceOp resolution to each nested group's own Tools
    # table the way plain Lua table nesting would suggest -- when two
    # files' shapes are both named "Shape_2", one file's Merge can end up
    # wired to the OTHER file's same-named tool instead of its own, even
    # with neither group touched (no manual ungroup involved). The suffix guarantees every tool name across an
    # entire batch is globally unique, not just unique within its own
    # file, which is what actually prevents that. Also guards the
    # single-file case against a same-named PRE-EXISTING tool left over
    # from an earlier import (of this same file, or one with the same
    # filename-derived name) -- the caller uses the group's own suffixed
    # name to confirm the paste actually worked, rather than diffing tool
    # lists, since a before/after diff can be fooled by stale leftover
    # tools when a paste silently fails.
    run_id = uuid.uuid4().hex[:6]
    base_name = os.path.splitext(os.path.basename(svg_path))[0]
    top = GroupNode(base_name)  # names.safe_name (below) appends run_id to this, same as every other tool
    report = ImportReport()
    css_classes = parse_css_classes(root, report)
    # The root <svg> element itself can set fill="none" (or any other
    # default) for everything inside it to inherit -- e.g. a document made
    # of stroke-only line art with no per-path fill at all. Reading that
    # here (rather than hardcoding black) is what makes such a root-level
    # default actually apply instead of being silently ignored.
    root_fill, root_fill_opacity, root_opacity, _, root_visible = get_style_fill(
        root, "#000000", 1.0, css_classes)
    # stroke's own SVG-spec initial value is "none" (unlike fill, which
    # commonly gets set at the root) -- so an SVG that never mentions stroke
    # anywhere still correctly ends up with has_stroke=False on every shape.
    root_stroke, root_stroke_width, root_stroke_opacity = get_style_stroke(root, None, 1.0, 1.0, css_classes)
    # Built once so <use> can resolve a reference to ANY id in the document,
    # not just ones inside <defs> -- per spec <use> can point at any element.
    id_map = {el.get('id'): el for el in root.iter() if el.get('id')}
    gradient_defs = parse_gradient_defs(root)
    filter_defs = parse_filter_defs(root)
    drop_shadow_defs = parse_drop_shadow_filter_defs(root)
    inner_shadow_defs = parse_inner_shadow_filter_defs(root)
    for child in root:
        node = walk(child, IDENTITY, root_fill, root_opacity, counter, id_map, frozenset(),
                     root_stroke, root_stroke_width, root_stroke_opacity, gradient_defs, filter_defs, None,
                     drop_shadow_defs, None, inner_shadow_defs, report=report,
                     viewport=(vb_w, vb_h), css_classes=css_classes,
                     inherited_fill_opacity=root_fill_opacity, inherited_visible=root_visible)
        if node is not None:
            top.children.append(node)

    def count_shapes(n):
        if isinstance(n, ShapeNode):
            return 1
        return sum(count_shapes(c) for c in n.children)

    say(f"Found {count_shapes(top)} shape(s) across the layer tree.")
    # Import Report: surfaces every approximation/omission walk() made along
    # the way (unresolved gradients, unparsed colors, unsupported filters,
    # unresolved masks/clip-paths, unsupported element types) instead of
    # leaving them silently buried in a shape's own fallback behavior --
    # see ImportReport's own docstring for why this stays a category tally
    # rather than a per-shape trace.
    if report.has_findings():
        say("Import notes -- some things were approximated or skipped:")
        for line in report.summary_lines():
            say(line)
    else:
        say("No unsupported features detected in this file.")

    names = LuaNameAllocator(suffix=run_id)
    positions = PastePositions(SVG_X_SPACING, SVG_Y_SPACING, orientation, origin=origin)
    slot_counter = [0]

    # "Consolidate Colors": built AFTER the shape tree exists (it needs to
    # inspect every shape's own resolved fill/stroke color) but BEFORE
    # build_group_lua runs (every shape needs the registry available while
    # its own Background is being generated) -- see
    # ColorConsolidationRegistry/build_color_master_controller_lua.
    color_registry = None
    if consolidate_colors:
        color_registry = ColorConsolidationRegistry(names.safe_name(f"{base_name}_MasterColors", "Custom"))
        color_registry.build(top)
        if color_registry.has_swatches():
            say(f"Consolidate colours: {len(color_registry.swatches)} distinct solid colour(s) found -- "
                f"wiring them to a Master Controller ({color_registry.master_tool_name}).")
        else:
            say("Consolidate colours: no solid fill/stroke colour found on any shape -- "
                "nothing to consolidate.")

    built = build_group_lua(top, to_point, to_vector, comp_w, comp_h, 0, slot_counter, names, positions,
                             is_root=True, color_registry=color_registry)
    if not built.tools:
        raise ValueError("No fillable or strokeable shapes were found in this SVG "
                          "(nothing had a fill or a stroke to draw).")
    group_name = names.safe_name(top.name, "Group")
    all_tools = list(built.tools)
    if color_registry is not None and color_registry.has_swatches():
        # Placed to the left of wherever this file's own layout actually reached, not a
        # fixed offset from origin. A fixed origin[0]-300 offset assumed X growth only
        # ever came from shape COUNT (true under "horizontal", where stack_slot - which
        # grows with sibling count - drives X) - it collides under "vertical",
        # where flow_level drives X instead and goes negative for a shape's own
        # subpath-mask chain, reaching arbitrarily far left based on that ONE shape's own
        # subpath count regardless of how many shapes the file has (e.g. a 7-level
        # mask chain reaching x=-1210 overlaps a Master Controller at origin[0]-300).
        master_pos = (positions.min_x_used() - 300.0, origin[1])
        all_tools.append(build_color_master_controller_lua(
            color_registry.master_tool_name, color_registry.swatches, master_pos))
    # The group's own box sits directly at origin -- deliberately NOT run
    # through positions.claim(), which would place it at
    # slot_counter[0] * x_spacing, i.e. an offset that GROWS with this
    # file's own shape count. That's meaningless for a single collapsed
    # box (it's not competing for a stack slot with anything), and with
    # batch imports' tight grid (see the page's GRID_SPACING_X/Y), a file
    # with enough shapes could otherwise push its own box past its
    # intended grid cell and into the next file's.
    tools = [build_single_group_wrapper_lua(group_name, all_tools, built.output_name,
                                              built.output_source, origin)]
    return tools, group_name, len(tools)


def svg_to_lua(svg_path, comp_w, comp_h, log=None, orientation="horizontal", scale_mode="native",
                origin=(0.0, 0.0), consolidate_colors=False):
    """Parses svg_path and returns (lua_paste_text, label, tool_count) -- a
    single file wrapped in its own paste-text envelope, ready to copy to
    the clipboard on its own. See build_svg_tools for the params, and
    build_svgs_to_lua for combining several files into ONE envelope."""
    tools, label, tool_count = build_svg_tools(
        svg_path, comp_w, comp_h, log=log, orientation=orientation, scale_mode=scale_mode,
        origin=origin, consolidate_colors=consolidate_colors)
    lua_text = "{\n    Tools = ordered() {\n        " + ",\n        ".join(tools) + "\n    }\n}"
    return lua_text, label, tool_count


def build_svgs_to_lua(svg_paths, comp_w, comp_h, log=None, orientation="horizontal", scale_mode="native",
                       grid_cols=None, grid_spacing_x=250.0, grid_spacing_y=150.0, consolidate_colors=False):
    """Combines every file in svg_paths into ONE paste-text payload, so a
    single Ctrl+V pastes the whole batch at once -- Fusion's paste is a
    plain OS clipboard operation, one payload at a time, but nothing stops
    that ONE payload from listing several files' worth of top-level tools;
    the earlier one-clipboard-per-file design was a choice, not a Fusion
    limitation. Each file gets its own top-level group -- exactly as it
    would pasted alone -- fanned out across a grid via origin (one grid
    cell per file, in svg_paths order) so files don't stack on top of each
    other at (0, 0).

    grid_cols=None (the default) picks ceil(sqrt(n)) columns, i.e. as close
    to a square layout as an exact square root allows -- 16 files -> 4x4,
    23 files -> 5 columns x 5 rows with the last row's final 2 cells simply
    unused (nothing pastes there, they're just empty grid space). Pass an
    explicit int to override.

    Returns (lua_paste_text, labels, total_tool_count) -- labels is each
    successfully-converted file's own group name, in svg_paths order.

    A file that fails to convert (e.g. build_svg_tools raising because it
    had no fillable/strokeable shapes) is logged and skipped rather than
    aborting the whole batch -- one bad file in a folder shouldn't block
    every other one from pasting."""
    def say(msg):
        if log:
            log(msg)

    if grid_cols is None:
        grid_cols = max(1, math.ceil(math.sqrt(len(svg_paths))))

    all_tools, labels = [], []
    for index, svg_path in enumerate(svg_paths):
        col, row = index % grid_cols, index // grid_cols
        origin = (col * grid_spacing_x, row * grid_spacing_y)
        try:
            tools, label, _ = build_svg_tools(
                svg_path, comp_w, comp_h, log=log, orientation=orientation, scale_mode=scale_mode,
                origin=origin, consolidate_colors=consolidate_colors)
        except Exception as exc:
            say(f"[ERROR] Skipping {svg_path}: {exc}")
            continue
        all_tools.extend(tools)
        labels.append(label)

    if not all_tools:
        raise ValueError("None of the queued SVGs had any fillable or strokeable shapes to draw.")

    lua_text = "{\n    Tools = ordered() {\n        " + ",\n        ".join(all_tools) + "\n    }\n}"
    return lua_text, labels, len(all_tools)


def run_import(app, svg_path, origin=(0.0, 0.0)):
    """Runs on a background thread: parses svg_path and copies Fusion's own
    paste-text format to the OS clipboard. comp.Paste() from inside a
    RunScript worker was confirmed reliable only for small graphs (a
    handful of tools) and confirmed to silently fail for anything larger --
    plain Ctrl+V into Fusion's own Flow view works regardless of size, so
    that's the only path now rather than trying RunScript first and falling
    back.

    origin is passed straight through to svg_to_lua -- see there for why
    (fanning batch-imported files out across the Flow view instead of
    stacking each one on the last)."""
    log = app._log
    comp_w, comp_h = app._comp_w or 1920, app._comp_h or 1080
    orientation = app.paste_orientation_var.get().lower()
    scale_mode = "timeline" if app.scale_mode_var.get().startswith("Timeline") else "native"
    consolidate_colors = app.consolidate_colors_var.get()

    log(f"Parsing {svg_path} …")
    lua_text, label, tool_count = svg_to_lua(svg_path, comp_w, comp_h, log=log, orientation=orientation,
                                              scale_mode=scale_mode, origin=origin,
                                              consolidate_colors=consolidate_colors)

    app._set_os_clipboard(lua_text)
    log(f"Copied group {label!r} to your clipboard. Click into Fusion's Flow view and press Ctrl+V.")


def run_import_all(app, svg_paths):
    """Runs on a background thread: parses every file in svg_paths and
    copies ONE combined paste-text payload to the OS clipboard -- see
    build_svgs_to_lua. A single Ctrl+V then pastes the whole batch at
    once, each file fanned out to its own grid slot (as close to a square
    layout as the file count allows, see build_svgs_to_lua's grid_cols;
    app.GRID_SPACING_X/GRID_SPACING_Y set the gap) instead of one Ctrl+V
    per file."""
    log = app._log
    comp_w, comp_h = app._comp_w or 1920, app._comp_h or 1080
    orientation = app.paste_orientation_var.get().lower()
    scale_mode = "timeline" if app.scale_mode_var.get().startswith("Timeline") else "native"
    consolidate_colors = app.consolidate_colors_var.get()

    log(f"Parsing {len(svg_paths)} queued SVG file(s) …")
    lua_text, labels, tool_count = build_svgs_to_lua(
        svg_paths, comp_w, comp_h, log=log, orientation=orientation, scale_mode=scale_mode,
        grid_spacing_x=app.GRID_SPACING_X, grid_spacing_y=app.GRID_SPACING_Y,
        consolidate_colors=consolidate_colors)

    app._set_os_clipboard(lua_text)
    skipped = len(svg_paths) - len(labels)
    skipped_note = f" ({skipped} file(s) skipped -- see log above)" if skipped else ""
    log(f"Copied {len(labels)} file(s), {tool_count} top-level tool(s) total, to your clipboard{skipped_note}. "
        "Click into Fusion's Flow view and press Ctrl+V once to paste the whole batch.")


# ==========================================================================
# Lottie / Bodymovin animation import, in three layers:
#   parsing         -- translating STATIC geometry into this file's own
#                      Subpath/ShapeNode/GroupNode objects. Zero Fusion-format
#                      risk: pure JSON-to-Python.
#   static-pose     -- a real static-pose export, reusing build_group_lua/
#     export           build_shape_lua/build_single_group_wrapper_lua UNCHANGED.
#                      A layer with nothing animated pastes at its first-frame
#                      pose this way (see _bake_lottie_layer_pose).
#   animated export -- a layer whose own Position/Scale/Rotation (or Opacity)
#                      really animates instead gets wrapped in a real animated
#                      Transform (see _wrap_layer_transform) / per-shape Alpha
#                      spline (see build_shape_lua's own layer_opacity_anim),
#                      modelled on a real "Dump Selected Node Settings" capture
#                      of a hand-built animated Transform -- see the module
#                      comment right before _lottie_resolved_static for the
#                      confirmed structure and what's still an assumption.
#
# Scope: TRANSFORM-only animation. Shape
# geometry (fill/stroke/path) always renders static -- reusing the exact
# SVG shape pipeline -- only a layer's own Position/Anchor/Scale/Rotation/
# Opacity become real keyframes. A layer's own Pivot (from its Lottie
# anchor) always stays static regardless, matching the real dump's own
# behavior. Path-shape morphing and stroke trim-path reveal are explicitly
# out of scope.
# ==========================================================================
class LottieImportReport:
    """Collects notable approximations/omissions made during one Lottie
    parse -- same category-tally philosophy as ImportReport (see its own
    docstring for why this stays a category count, not a per-layer
    trace): an animator needs "3 layers skipped, type: image" to know
    where to go look, not a full dump."""

    def __init__(self):
        self.unsupported_layer_types = collections.Counter()
        self.skipped_masks_mattes = 0
        self.skipped_effects_expressions = 0
        self.approximated_animated_group_transforms = 0
        self.approximated_animated_shape_paths = 0
        self.approximated_animated_fill_stroke_opacity = 0
        self.skipped_repeaters = 0
        self.skipped_trim_paths = 0
        self.skipped_merge_paths = 0
        self.layers_with_unapplied_parent = 0
        self.approximated_animated_anchors = 0
        self.approximated_derived_scalar_eases = 0
        self.approximated_stepped_keyframes = 0
        self.approximated_animated_shadow_opacities = 0
        self.approximated_split_positions = 0
        self.unresolved_precomps = 0
        self.visibility_gated_layers = 0
        self.track_mattes_applied = 0
        self.approximated_mirrored_scales = 0
        self.consolidated_objects = 0
        self.consolidated_segments = 0
        self.approximated_merge_boundaries = 0

    def note_unsupported_layer_type(self, ty):
        self.unsupported_layer_types[ty] += 1

    def note_skipped_mask_matte(self):
        self.skipped_masks_mattes += 1

    def note_skipped_effect_expression(self):
        self.skipped_effects_expressions += 1

    def note_approximated_group_transform(self):
        self.approximated_animated_group_transforms += 1

    def note_approximated_shape_path(self):
        self.approximated_animated_shape_paths += 1

    def note_approximated_fill_stroke_opacity(self):
        self.approximated_animated_fill_stroke_opacity += 1

    def note_skipped_repeater(self):
        self.skipped_repeaters += 1

    def note_skipped_trim_path(self):
        self.skipped_trim_paths += 1

    def note_skipped_merge_path(self):
        self.skipped_merge_paths += 1

    def note_layer_with_unapplied_parent(self):
        self.layers_with_unapplied_parent += 1

    def note_approximated_animated_anchor(self):
        self.approximated_animated_anchors += 1

    def note_approximated_derived_scalar_ease(self):
        self.approximated_derived_scalar_eases += 1

    def note_approximated_stepped_keyframe(self):
        self.approximated_stepped_keyframes += 1

    def note_approximated_animated_shadow_opacity(self):
        self.approximated_animated_shadow_opacities += 1

    def note_approximated_split_position(self):
        self.approximated_split_positions += 1

    def note_unresolved_precomp(self):
        self.unresolved_precomps += 1

    def note_visibility_gated_layer(self):
        self.visibility_gated_layers += 1

    def note_approximated_mirrored_scale(self):
        self.approximated_mirrored_scales += 1

    def note_track_matte_applied(self):
        self.track_mattes_applied += 1

    def note_consolidated_object(self, segment_count):
        self.consolidated_objects += 1
        self.consolidated_segments += segment_count

    def note_approximated_merge_boundary(self):
        self.approximated_merge_boundaries += 1

    def has_findings(self):
        return bool(self.unsupported_layer_types) or any([
            self.skipped_masks_mattes, self.skipped_effects_expressions,
            self.approximated_animated_group_transforms, self.approximated_animated_shape_paths,
            self.approximated_animated_fill_stroke_opacity, self.skipped_repeaters,
            self.skipped_trim_paths, self.skipped_merge_paths, self.layers_with_unapplied_parent,
            self.approximated_animated_anchors, self.approximated_derived_scalar_eases,
            self.approximated_stepped_keyframes, self.approximated_animated_shadow_opacities,
            self.approximated_split_positions, self.unresolved_precomps,
            self.visibility_gated_layers, self.consolidated_objects, self.track_mattes_applied,
            self.approximated_mirrored_scales,
            self.approximated_merge_boundaries,
        ])

    def summary_lines(self):
        lines = []
        for ty in sorted(self.unsupported_layer_types):
            count = self.unsupported_layer_types[ty]
            lines.append(f"[NOTE] Skipped {count} layer(s) of unsupported type {ty!r} "
                          "(only shape layers, type 4, are supported).")
        if self.skipped_masks_mattes:
            lines.append(f"[NOTE] {self.skipped_masks_mattes} layer mask/track-matte "
                          "reference(s) ignored -- those layers import unclipped.")
        if self.skipped_effects_expressions:
            lines.append(f"[NOTE] {self.skipped_effects_expressions} layer effect(s) or "
                          "expression-driven propert(y/ies) ignored -- falling back to each "
                          "property's own plain keyframed/static value.")
        if self.approximated_animated_group_transforms:
            lines.append(f"[NOTE] {self.approximated_animated_group_transforms} shape group's "
                          "own animated transform approximated as static (its first keyframe) "
                          "-- only a LAYER's own transform animates in this version.")
        if self.approximated_animated_shape_paths:
            lines.append(f"[NOTE] {self.approximated_animated_shape_paths} animated shape path "
                          "(path morphing) approximated as static (its first keyframe) -- not "
                          "supported yet.")
        if self.approximated_animated_fill_stroke_opacity:
            lines.append(f"[NOTE] {self.approximated_animated_fill_stroke_opacity} fill/stroke's "
                          "own animated opacity approximated as static (its first keyframe) -- "
                          "only a LAYER's own opacity animates in this version.")
        if self.skipped_repeaters:
            lines.append(f"[NOTE] {self.skipped_repeaters} repeater shape(s) ignored -- not "
                          "supported yet.")
        if self.skipped_trim_paths:
            lines.append(f"[NOTE] {self.skipped_trim_paths} trim-path shape(s) ignored -- "
                          "stroke reveal animation isn't supported yet.")
        if self.skipped_merge_paths:
            lines.append(f"[NOTE] {self.skipped_merge_paths} merge-path shape(s) ignored -- "
                          "each of its own source shapes imports separately instead.")
        if self.layers_with_unapplied_parent:
            lines.append(f"[NOTE] {self.layers_with_unapplied_parent} layer's own AE parent "
                          "chain couldn't be fully resolved (a dangling parent reference, or a "
                          "chain over 50 layers deep) -- everything up to that point in the chain "
                          "is still applied, only the unresolved tail is dropped.")
        if self.approximated_animated_anchors:
            lines.append(f"[NOTE] {self.approximated_animated_anchors} layer's own animated anchor "
                          "point approximated as static (its first keyframe) -- a layer's Pivot "
                          "never animates in this version.")
        if self.approximated_derived_scalar_eases:
            lines.append(f"[NOTE] {self.approximated_derived_scalar_eases} derived animated value "
                          "(Aspect, or a layer's motion-path timing) used linear timing instead of "
                          "its source property's own eased keyframes -- no direct ease to carry over "
                          "for a value that isn't a straight copy of one Lottie property.")
        if self.approximated_stepped_keyframes:
            lines.append(f"[NOTE] {self.approximated_stepped_keyframes} stepped/hold keyframe(s) "
                          "approximated as interpolated -- instant value changes aren't supported yet.")
        if self.approximated_animated_shadow_opacities:
            lines.append(f"[NOTE] {self.approximated_animated_shadow_opacities} drop/inner shadow "
                          "kept a static opacity even though its own layer's opacity animates -- "
                          "not supported yet.")
        if self.approximated_split_positions:
            lines.append(f"[NOTE] {self.approximated_split_positions} layer's own split-dimension "
                          "position (AE's \"separate X/Y\" toggle) animated correctly at each real "
                          "keyframe, but the motion PATH between them is a straight-line "
                          "approximation -- the two axes' own individual eases aren't reconstructed.")
        if self.unresolved_precomps:
            lines.append(f"[NOTE] {self.unresolved_precomps} precomp layer reference(s) couldn't be "
                          "resolved (missing/malformed asset, a circular precomp reference, or "
                          "nesting over 10 levels deep) -- that precomp's own content is skipped.")
        if self.visibility_gated_layers:
            lines.append(f"[NOTE] {self.visibility_gated_layers} layer(s) only visible for part of "
                          "the timeline (a real AE in/out point trim, or a nested precomp instance's "
                          "own visible window) now pop in/out at the right frame via a dedicated "
                          "existence Merge instead of showing for the whole composition.")
        if self.approximated_mirrored_scales:
            lines.append(f"[NOTE] {self.approximated_mirrored_scales} layer(s) use a MIRRORED "
                          "(negative) scale that couldn't be folded into their geometry -- either the "
                          "mirror flips sign mid-animation or another live transform sits in between, so "
                          "they render unmirrored.")
        if self.track_mattes_applied:
            lines.append(f"[NOTE] {self.track_mattes_applied} layer(s) clipped to a track matte, "
                          "approximated as a hard-edged clip on the matte's own resolved-static shape "
                          "(an animated matte uses its first keyframe).")
        if self.consolidated_objects:
            lines.append(f"[NOTE] Consolidated {self.consolidated_segments} shape layer(s) into "
                          f"{self.consolidated_objects} continuously-animated object(s) -- the source "
                          "re-creates the same artwork once per time segment (each in its own precomp), "
                          "so those copies were recognised as one object and merged.")
        if self.approximated_merge_boundaries:
            lines.append(f"[NOTE] {self.approximated_merge_boundaries} consolidated keyframe value(s) "
                          "at a segment boundary were read by linear interpolation, because that "
                          "segment's own animation was mid-curve when it appeared or disappeared -- "
                          "its bezier ease isn't reconstructed for that single value.")
        return lines


def lottie_static_value(prop, default=None, scalar=False):
    """Resolves a Lottie property dict ({"a":0,"k":<value>} or {"a":1,
    "k":[keyframe,...]}) to ONE static value -- the plain k for a
    non-animated property, or the first keyframe's own "s" (start value)
    for an animated one. default covers a property Lottie considers
    optional and simply omits (e.g. a shape's own "r" corner radius).

    scalar=True unwraps a SCALAR property's animated "s", which Lottie
    always wraps in a 1-element list (e.g. rotation's is [45], not 45)
    even though that same property's non-animated "k" is already a bare
    number -- an animated shape-group rotation would otherwise crash
    math.radians() on a list. Vector
    properties (position/anchor/scale/color) already have "s" shaped
    like their own non-animated "k" ([x, y], [r, g, b]) and must be left
    alone (scalar=False, the default).

    A PATH property's animated "s" is list-wrapped the same way a
    scalar's is -- [{"v":..,"i":..,"o":..,"c":..}] rather than the bare
    dict its own non-animated "k" holds -- and that case is unwrapped
    automatically, no flag needed: a 1-element list holding a DICT is
    unambiguous, since no vector property ever contains dicts. Without
    it, lottie_shape_to_subpath would get the list and die on .get()
    for any file with animated path shapes. Auto-detecting here rather than making the
    caller pass scalar=True keeps the flag's meaning honest (a path is
    not a scalar) and protects every other path read site too."""
    if prop is None:
        return default
    k = prop.get("k")
    if prop.get("a") == 1:
        if not k:
            return default
        val = k[0].get("s", default)
        if isinstance(val, list) and len(val) == 1 and isinstance(val[0], dict):
            return val[0]
        return val[0] if scalar and isinstance(val, list) else val
    return k if k is not None else default


def lottie_keyframes(prop, fps):
    """For an animated (a==1) Lottie property, returns a normalized
    [(frame_time, value, out_ease, in_ease, spatial_out, spatial_in,
    is_hold), ...] list -- a straight pass-through of each raw keyframe's
    own "t"/"s"/"o"/"i"/"to"/"ti"/"h" fields, no Fusion-shape decisions
    made here at all (see this module section's own comment). fps is
    carried along since Lottie's own frame numbers will eventually need
    reconciling against whatever time units Fusion's own dump turns out
    to use.

    spatial_out ("to")/spatial_in ("ti") are a POSITION property's own
    spatial bezier tangents (the on-screen motion-path curve shape) --
    distinct from o/i, which are the TEMPORAL ease (speed along that
    path/value over time). Both None for every non-position property,
    which never carries to/ti. is_hold is Lottie's "h" stepped-keyframe
    flag (approximated as interpolated -- see LottieImportReport's own
    approximated_stepped_keyframes counter).

    Returns None for a non-animated property (a load-bearing signal, not
    just an empty list, so a caller can tell "never animated" apart from
    "animated but somehow produced zero keyframes")."""
    if prop is None or prop.get("a") != 1:
        return None
    keyframes = []
    for kf in prop.get("k", []):
        keyframes.append((kf.get("t", 0), kf.get("s"), kf.get("o"), kf.get("i"),
                           kf.get("to"), kf.get("ti"), bool(kf.get("h"))))
    return keyframes


def _lottie_split_axis_keyframes(axis_prop, fps):
    """One axis ("x" or "y") of a Lottie SPLIT position property
    (`"p": {"s": true, "x": {...}, "y": {...}}`) is itself an ordinary
    SCALAR property (`{"a":0/1,"k":...}`, its own animated "s" wrapped in
    a 1-element list same as any other scalar -- see lottie_static_value's
    own scalar= convention) -- returns [(t, value), ...] (values already
    unwrapped) for an animated axis, or None for a static one."""
    kf = lottie_keyframes(axis_prop, fps)
    if kf is None:
        return None
    return [(t, v[0] if isinstance(v, list) else v) for (t, v, *_rest) in kf]


def _lottie_axis_value_at(axis_kf, static_value, t):
    """Plain linear interpolation of one split-position axis at time t --
    used only to resample the OTHER axis onto a merged time this one
    didn't originally have a keyframe at (see
    _lottie_position_property_keyframes). Deliberately ignores that
    axis's own bezier ease for the resample (same class of approximation
    already applied to Aspect/Displacement elsewhere in this file --
    those derived values don't get to reuse a source property's ease
    either)."""
    if axis_kf is None:
        return static_value
    if t <= axis_kf[0][0]:
        return axis_kf[0][1]
    if t >= axis_kf[-1][0]:
        return axis_kf[-1][1]
    for (t0, v0), (t1, v1) in zip(axis_kf, axis_kf[1:]):
        if t0 <= t <= t1:
            frac = (t - t0) / (t1 - t0) if t1 != t0 else 0.0
            return v0 + frac * (v1 - v0)
    return axis_kf[-1][1]


def _lottie_gate_needed(windows, comp_span):
    """True iff these (ip, op) visibility intervals (root-comp-absolute
    Lottie frame units -- see _lottie_layer_window) don't already cover
    the whole of comp_span (the OVERALL Lottie composition's own
    (in_point, out_point)) -- i.e. there's really something to gate, not
    just the common case of a layer visible for the whole render."""
    comp_in, comp_out = comp_span
    merged = _lottie_merge_intervals(windows)
    if len(merged) != 1:
        return True
    ip, op = merged[0]
    return ip > comp_in + 1e-6 or op < comp_out - 1e-6


def _lottie_visibility_gate_keyframes(windows, comp_span):
    """A pure 0/1 existence gate at these (ip, op) intervals' boundaries, in
    the SAME 7-tuple shape lottie_keyframes() already returns -- meant
    for a DEDICATED Merge's own Blend input (see build_group_lua's own
    visibility-window wrap / _build_gate_merge_lua), not for blending
    into a shape's own fill/stroke alpha. Deliberately carries no real
    opacity value at all -- a layer's own genuinely-animated ks.o (rare
    in practice) stays completely independent, still handled by the
    existing per-shape "_Alpha" BezierSpline path with its own real ease
    fully intact; this gate only ever multiplies 0 or 1 on top via the
    Merge's own Blend (one dedicated Merge per gated layer, rather than
    duplicating an alpha spline onto every one of that layer's own
    shapes).

    Without this gate every imported shape would stay visible for the
    WHOLE Fusion timeline once pasted, even one the source file only
    shows for a fraction of it -- nothing else in this module reads a
    layer's own ip/op.

    Builds the gate as a near-instant (one-frame) linear ramp at each
    boundary rather than a true mathematical step -- this file has no
    confirmed way to write a real Fusion "Hold" keyframe (see
    lottie_keyframes' own is_hold handling, similarly approximated as
    interpolated), and a one-frame ramp is visually indistinguishable
    from a hard cut at normal playback.

    Returns None if window, after all that, turns out to need no gate
    at all (already covers the whole comp) -- the caller checks
    _lottie_gate_needed itself first, so this should never actually
    happen in practice, but staying None-safe costs nothing."""
    if not _lottie_gate_needed(windows, comp_span):
        return None
    comp_in, comp_out = comp_span
    by_time = {}
    for ip, op in _lottie_merge_intervals(windows):
        if ip > comp_in + 1e-6:
            by_time[ip - 1.0] = 0.0
            by_time[ip] = 1.0
        if op < comp_out - 1e-6:
            by_time[op - 1.0] = 1.0
            by_time[op] = 0.0
    if not by_time:
        return None
    return [(t, [by_time[t]], None, None, None, None, False) for t in sorted(by_time)]


def _lottie_position_property_keyframes(p_prop, fps, report=None):
    """Resolves a Lottie position property's ANIMATED case for EITHER of
    its two real shapes: unified (`{"a":1,"k":[...]}`, handled identically
    to any other lottie_keyframes() call) or Lottie's "split dimensions"
    form (`{"s":true,"x":{...},"y":{...},"z":{...}}`, AE's own "separate
    X/Y/Z dimensions" toggle -- each axis an INDEPENDENT scalar property,
    commonly with DIFFERENT keyframe times on each axis).

    Real exported files can have EVERY layer's own position split this
    way, and unified-only lottie_keyframes()/lottie_static_value() calls
    would silently fall through to the (0,0) default on every one of
    them -- not a crash, just silently dropping every layer's actual
    on-screen motion.

    For the split case: the two axes' own keyframe times are merged
    (union'd), and at each merged time the axis that doesn't have a
    keyframe there gets a plain linear-interpolated value from its own
    neighboring keyframes (see _lottie_axis_value_at) -- the merged
    result's per-point temporal/spatial ease is therefore NOT
    reconstructed (o/i/to/ti left None, matching the Linear treatment
    _lottie_position_displacement_specs already gives the derived
    Displacement curve) -- only the real POSITION at each real keyframe
    time is preserved exactly, which is what matters for correctness;
    only the in-between motion-path shape is a straight-line
    approximation between those points.

    Returns None if genuinely never animated on either axis (caller
    falls back to a static value instead -- see
    _lottie_position_static_value)."""
    if p_prop is None or not p_prop.get("s"):
        return lottie_keyframes(p_prop, fps)
    x_kf = _lottie_split_axis_keyframes(p_prop.get("x", {}), fps)
    y_kf = _lottie_split_axis_keyframes(p_prop.get("y", {}), fps)
    if x_kf is None and y_kf is None:
        return None
    if report is not None:
        report.note_approximated_split_position()
    x_static = lottie_static_value(p_prop.get("x", {}), 0.0, scalar=True)
    y_static = lottie_static_value(p_prop.get("y", {}), 0.0, scalar=True)
    times = sorted({t for t, _ in (x_kf or [])} | {t for t, _ in (y_kf or [])})
    return [(t, [_lottie_axis_value_at(x_kf, x_static, t), _lottie_axis_value_at(y_kf, y_static, t)],
              None, None, None, None, False) for t in times]


def _lottie_position_static_value(p_prop, default=(0.0, 0.0)):
    """The static-value counterpart of _lottie_position_property_keyframes
    -- handles a split position's own static/never-animated case (each
    axis independently defaults or resolves via lottie_static_value) the
    same way the unified case already did."""
    if p_prop is None:
        return list(default)
    if not p_prop.get("s"):
        return lottie_static_value(p_prop, list(default))
    return [lottie_static_value(p_prop.get("x", {}), default[0], scalar=True),
            lottie_static_value(p_prop.get("y", {}), default[1], scalar=True)]


def _lottie_prop_is_animated(value):
    """True iff value is a LottieLayerAnimation field's own lottie_keyframes()
    result rather than its resolved-static counterpart. Can't just check
    isinstance(value, list) -- a STATIC position/anchor/scale is ALSO a
    list ([x, y]), same as the real thing this is trying to detect. The
    actual signal: lottie_keyframes() returns a list of TUPLES
    ((frame, value, out_ease, in_ease) each), while every static value
    (2D or scalar) is a list of plain numbers or a bare number."""
    return isinstance(value, list) and bool(value) and isinstance(value[0], tuple)


def lottie_matrix_from_static_transform(tr):
    """Builds a mat_mul-composable matrix from a RESOLVED-STATIC Lottie
    transform dict (a shape group's own "tr", or -- for the
    static-pose bake, see _bake_lottie_layer_pose -- a layer's own "ks")
    -- p(osition)/a(nchor)/s(cale)/r(otation), each already reduced to one
    static value via lottie_static_value.

    Composition order matches After Effects' own layer transform model:
    translate(-anchor), then scale, then rotate, then translate(position)
    -- i.e. rotate/scale happen "around" the anchor point, exactly like
    parse_transform's own rotate-around-a-point construction (see there
    for the same mat_mul(outer, inner) pattern used step by step here).

    Rotation SIGN (clockwise-positive, matching AE's own on-screen
    convention) is an ASSUMPTION, not yet confirmed against a live
    render -- flag this if a real animated-rotation test ever looks
    mirrored."""
    px, py = _lottie_position_static_value(tr.get("p"))
    ax, ay = lottie_static_value(tr.get("a"), [0.0, 0.0])[:2]
    sx, sy = lottie_static_value(tr.get("s"), [100.0, 100.0])[:2]
    rot = lottie_static_value(tr.get("r"), 0.0, scalar=True)
    ang = math.radians(rot)
    ca, sa = math.cos(ang), math.sin(ang)
    m = (1.0, 0.0, 0.0, 1.0, -ax, -ay)
    m = mat_mul((sx / 100.0, 0.0, 0.0, sy / 100.0, 0.0, 0.0), m)
    m = mat_mul((ca, sa, -sa, ca, 0.0, 0.0), m)
    m = mat_mul((1.0, 0.0, 0.0, 1.0, px, py), m)
    return m


def lottie_shape_to_subpath(sh_ks):
    """Converts one resolved-static Lottie path ({"v":[[x,y],...],
    "i":[[dx,dy],...],"o":[[dx,dy],...],"c":bool} -- vertices, and each
    vertex's own incoming/outgoing bezier handle as an OFFSET from it)
    into one Subpath.

    Lottie stores both handles for vertex n together (in[n] and out[n]);
    this app's own Subpath model (see parse_path_d) instead stores each
    anchor's in_cp/out_cp as ABSOLUTE points, same convention either way
    just requiring the vertex's own coordinate added back onto each
    offset -- out_cp = v[n] + o[n], in_cp = v[n] + i[n], both set on
    vertex n itself (not the next/previous one, unlike SVG's own
    per-segment C/S commands)."""
    sp = Subpath()
    sp.closed = bool(sh_ks.get("c", False))
    verts = sh_ks.get("v", [])
    ins = sh_ks.get("i", [])
    outs = sh_ks.get("o", [])
    for n, (vx, vy) in enumerate(verts):
        sp.add(vx, vy)
        if n < len(outs):
            ox, oy = outs[n]
            sp.set_out_cp(n, (vx + ox, vy + oy))
        if n < len(ins):
            ix, iy = ins[n]
            sp.set_in_cp(n, (vx + ix, vy + iy))
    return sp


def _lottie_group_tr(item):
    """One gr's own "tr" item (its group transform), or None -- the LAST
    one wins, matching the original inline scan this replaces."""
    tr = None
    for sub in item.get("it", []):
        if sub.get("ty") == "tr":
            tr = sub
    return tr


def _lottie_tr_is_identity(tr):
    """True iff this group "tr" does nothing at all (no offset, no
    anchor, unit scale, no rotation) -- such a tr is left baked (baking
    identity changes nothing), so an identity group costs no extra
    Transform tool."""
    if tr is None:
        return True
    p = _lottie_position_static_value(tr.get("p"))
    a = lottie_static_value(tr.get("a"), [0.0, 0.0])
    s = lottie_static_value(tr.get("s"), [100.0, 100.0])
    r = lottie_static_value(tr.get("r"), 0.0, scalar=True)
    return (abs(p[0]) < 1e-9 and abs(p[1]) < 1e-9 and abs(a[0]) < 1e-9 and abs(a[1]) < 1e-9
            and abs(s[0] - 100.0) < 1e-9 and abs(s[1] - 100.0) < 1e-9 and abs(r) < 1e-9)


def _lottie_group_needs_promotion(item, fps):
    """True iff this gr's transform must become a real live Fusion
    Transform level instead of being baked into its children's geometry
    (see lottie_shapes_to_nodes): its OWN tr really animates, or its own
    tr is merely NON-IDENTITY, or any gr nested inside it needs
    promotion.

    The non-identity clause exists for node-graph consolidation (see
    _consolidate_lottie_layers): baking a static-but-real group
    transform into geometry makes two otherwise-identical copies of the
    same artwork compare as DIFFERENT shapes, which is exactly what
    blocks them from being recognised as time segments of one object.
    A real exported file makes this concrete -- Shapes.json's own cyan
    diamond bakes rotation 45 in two of its four segments and -45 in the
    other two, so identity matching found six classes where there are
    really four. Leaving every non-identity tr live keeps geometry pure
    local, which makes the comparison exact.

    That second clause is load-bearing, not defensive: baking an
    ancestor group's matrix into geometry while a DESCENDANT group's
    transform stays live composes them backwards (the live one would end
    up outermost, applied after the baked one, where AE's own
    child_world = parent o child needs the opposite), so an ancestor of
    a promoted group has to be promoted too -- even when its own tr is
    perfectly static. Promotion therefore propagates UP the group chain,
    which also guarantees the accumulated matrix is still IDENTITY by
    the time a promoted group is reached."""
    tr = _lottie_group_tr(item)
    if tr is not None and not _lottie_tr_is_identity(tr):
        return True
    if tr is not None and _layer_has_transform_animation(_parse_ks_animation(tr, fps)):
        return True
    return any(sub.get("ty") == "gr" and _lottie_group_needs_promotion(sub, fps)
               for sub in item.get("it", []))


def lottie_shapes_to_nodes(shapes_list, matrix, opacity, counter, report, fps=30.0):
    """The per-group recursive walk over one Lottie shapes[] array (a
    layer's own top-level shapes, or one gr's own it[]) -- mirrors
    walk()'s structure for SVG's <g>/leaf elements.

    matrix/opacity are the ALREADY-ACCUMULATED local transform/opacity
    from ancestor shape groups' own "tr" (baked in exactly like an SVG
    <g transform=.../opacity=...> would be) -- NOT the layer's own "ks",
    which this function never touches at all (see
    lottie_layer_to_group_node/_bake_lottie_layer_pose for why that stays
    separate).

    A fill/stroke item paints every geometry item (sh/rc/el) that appears
    BEFORE it, earlier in the SAME list -- the common real-export shape,
    "one path then its own fill/stroke then the group's own tr." Returns
    a list of ShapeNode/GroupNode objects.

    A gr whose own "tr" really ANIMATES is PROMOTED rather than baked --
    its transform becomes a real live Fusion Transform level (see
    _lottie_group_needs_promotion / build_group_lua's own wrap hookup)
    instead of being flattened into its children's geometry at its own
    first keyframe. That keeps animation a bake would drop outright
    (tallied as note_approximated_group_transform) -- e.g. a file that
    spins a rect 0->180 and an arc 0->360 purely at the shape-group
    level, motion that can't survive the bake.
    Promotion propagates UP: see _lottie_group_needs_promotion."""
    geometry, fill_item, stroke_item = [], None, None
    nodes = []
    for item in shapes_list:
        ty = item.get("ty")
        if ty == "gr":
            child_tr = _lottie_group_tr(item)
            promote = _lottie_group_needs_promotion(item, fps)
            group_node = GroupNode(item.get("nm") or f"Group_{next(counter)}")
            if promote:
                # This tr is deliberately NOT folded into child_m -- it
                # becomes this node's own live Transform instead. matrix
                # is necessarily IDENTITY here (promotion propagates up,
                # so nothing above this group was baked either).
                child_m = matrix
                group_node.layer_animation = _parse_ks_animation(
                    child_tr or {}, fps, report=report)._replace(opacity=100.0)
                group_node.transform_space = _LOTTIE_SPACE_SHAPE_GROUP
                group_node.needs_live_transform = True
            else:
                if child_tr is not None and (
                        child_tr.get("p", {}).get("a") or child_tr.get("p", {}).get("s")
                        or child_tr.get("r", {}).get("a")
                        or child_tr.get("s", {}).get("a") or child_tr.get("a", {}).get("a")):
                    report.note_approximated_group_transform()
                child_m = mat_mul(matrix, lottie_matrix_from_static_transform(child_tr or {}))
            # Opacity stays baked either way (a promoted group's own
            # animation carries opacity=100 above, so nothing can
            # double-apply it) -- an ANIMATED group opacity remains the
            # same flagged approximation it already was.
            child_o = opacity * (lottie_static_value(
                (child_tr or {}).get("o"), 100.0, scalar=True) / 100.0)
            group_node.children = lottie_shapes_to_nodes(
                item.get("it", []), child_m, child_o, counter, report, fps)
            if group_node.children:
                nodes.append(group_node)
        elif ty in ("sh", "rc", "el"):
            geometry.append((ty, item))
        elif ty == "fl":
            fill_item = item
        elif ty == "st":
            stroke_item = item
        elif ty == "rp":
            report.note_skipped_repeater()
        elif ty == "tm":
            report.note_skipped_trim_path()
        elif ty == "mm":
            report.note_skipped_merge_path()
        # "tr" (this group's own transform) is read by the CALLER, above --
        # skipped here rather than double-handled.

    if not geometry or fill_item is None and stroke_item is None:
        return nodes

    has_fill = fill_item is not None
    fill_color = fill_stroke_opacity = None
    if has_fill:
        if fill_item.get("c", {}).get("a") or fill_item.get("o", {}).get("a"):
            report.note_approximated_fill_stroke_opacity()
        fc = lottie_static_value(fill_item.get("c"), [0.0, 0.0, 0.0])
        fill_color = (fc[0], fc[1], fc[2], 1.0)
        fill_opacity = opacity * (lottie_static_value(fill_item.get("o"), 100.0, scalar=True) / 100.0)

    has_stroke = stroke_item is not None
    stroke_color = stroke_width_px = stroke_opacity = None
    if has_stroke:
        if stroke_item.get("c", {}).get("a") or stroke_item.get("o", {}).get("a") \
                or stroke_item.get("w", {}).get("a"):
            report.note_approximated_fill_stroke_opacity()
        sc = lottie_static_value(stroke_item.get("c"), [0.0, 0.0, 0.0])
        stroke_color = (sc[0], sc[1], sc[2], 1.0)
        stroke_opacity = opacity * (lottie_static_value(stroke_item.get("o"), 100.0, scalar=True) / 100.0)
        ma, mb, mc, md, _, _ = matrix
        stroke_width_px = lottie_static_value(stroke_item.get("w"), 0.0, scalar=True) * math.sqrt(abs(ma * md - mb * mc))

    for ty, item in geometry:
        if ty == "sh":
            ks = item.get("ks", {})
            if ks.get("a") == 1:
                report.note_approximated_shape_path()
            sh_ks = lottie_static_value(ks, {"v": [], "i": [], "o": [], "c": False})
            sp = lottie_shape_to_subpath(sh_ks)
        elif ty == "rc":
            cx, cy = lottie_static_value(item.get("p"), [0.0, 0.0])[:2]
            w, h = lottie_static_value(item.get("s"), [0.0, 0.0])[:2]
            r = lottie_static_value(item.get("r"), 0.0, scalar=True)
            sp = rect_to_subpath(cx - w / 2.0, cy - h / 2.0, w, h, r, r)
        else:  # "el"
            cx, cy = lottie_static_value(item.get("p"), [0.0, 0.0])[:2]
            dw, dh = lottie_static_value(item.get("s"), [0.0, 0.0])[:2]
            sp = ellipse_to_subpath(cx, cy, dw / 2.0, dh / 2.0)
        for a in sp.anchors:
            a['x'], a['y'] = mat_point(matrix, a['x'], a['y'])
            if a['in_cp'] is not None:
                a['in_cp'] = mat_point(matrix, *a['in_cp'])
            if a['out_cp'] is not None:
                a['out_cp'] = mat_point(matrix, *a['out_cp'])
        name = item.get("nm") or f"Shape_{next(counter)}"
        nodes.append(ShapeNode(name, [sp], has_fill, fill_color,
                                fill_opacity if has_fill else 1.0,
                                has_stroke, stroke_color, stroke_width_px, stroke_opacity))
    return nodes


LottieLayerAnimation = collections.namedtuple(
    "LottieLayerAnimation", "position anchor scale rotation opacity fps")


def _parse_ks_animation(ks, fps, report=None):
    """Resolves ANY Lottie layer's own "ks" dict (shape layer or
    otherwise -- ks itself is universal across every layer type) into a
    LottieLayerAnimation -- shared by a shape layer's own transform
    (lottie_layer_to_group_node) and by each ancestor in its AE parent
    chain (_resolve_lottie_ancestor_chain), since both need the exact
    same position/anchor/scale/rotation/opacity resolution."""
    return LottieLayerAnimation(
        position=_lottie_position_property_keyframes(ks.get("p"), fps, report=report) or
                 _lottie_position_static_value(ks.get("p")),
        anchor=lottie_keyframes(ks.get("a"), fps) or lottie_static_value(ks.get("a"), [0.0, 0.0]),
        scale=lottie_keyframes(ks.get("s"), fps) or lottie_static_value(ks.get("s"), [100.0, 100.0]),
        rotation=lottie_keyframes(ks.get("r"), fps) or lottie_static_value(ks.get("r"), 0.0),
        opacity=lottie_keyframes(ks.get("o"), fps) or lottie_static_value(ks.get("o"), 100.0),
        fps=fps,
    )


def _resolve_lottie_ancestor_chain(layer_json, layer_by_ind, fps, report):
    """Walks a layer's own AE "parent" chain (nearest parent first, root
    last), resolving EACH ancestor's own "ks" into a LottieLayerAnimation
    -- a common case, not a rare edge case: real exported files often
    have every shape layer parented to a null/precomp layer sitting away
    from the comp's own default, and ignoring that chain (merely tallying
    it) leaves every child rendering at a nonsensical position;
    multiplying the parent's own transform back in lands it somewhere
    sane inside the comp.

    layer_by_ind maps a Lottie layer's own "ind" to its raw JSON --
    built once per file (see parse_lottie_composition) since ANY layer
    type can be a parent (a null/ty=3, a precomp/ty=0, even another shape
    layer), not just ones this app otherwise supports importing.

    Bounded to 50 levels (a real cycle would otherwise loop forever on a
    malformed file) and stops (flagging note_layer_with_unapplied_parent)
    the first time a parent index doesn't resolve to a real layer --
    every OTHER ancestor already resolved before that point still gets
    applied; only the unresolved tail is dropped."""
    chain = []
    parent_ind = layer_json.get("parent")
    seen = set()
    while parent_ind is not None and len(chain) < 50:
        if parent_ind in seen:
            report.note_layer_with_unapplied_parent()
            break
        seen.add(parent_ind)
        parent_json = layer_by_ind.get(parent_ind)
        if parent_json is None:
            report.note_layer_with_unapplied_parent()
            break
        chain.append(_parse_ks_animation(parent_json.get("ks", {}), fps, report=report))
        parent_ind = parent_json.get("parent")
    return chain


def _shift_lottie_keyframes_time(value, offset):
    """Shifts every keyframe's own frame-time field ("t", each tuple's
    first element) by offset -- a static (non-animated) value passes
    through unchanged, and offset=0.0 is a no-op fast path. Used to
    normalize a layer's own keyframe times -- originally expressed
    relative to whatever comp DIRECTLY contains it (the root comp, or a
    precomp asset's own internal timeline) -- into ONE consistent
    root-comp-absolute Lottie-frame reference before this file's
    Lua-building side ever sees them (see _collect_lottie_shape_layers).
    That side has always assumed every "t" is already root-comp-absolute
    (true for every top-level, non-precomp file tested so far), so
    normalizing here means zero downstream Lua-building changes are
    needed to support precomp-nested content."""
    if offset == 0.0 or not _lottie_prop_is_animated(value):
        return value
    return [(t + offset,) + tuple(rest) for (t, *rest) in value]


def _shift_lottie_layer_animation_time(anim, offset):
    """Applies _shift_lottie_keyframes_time to every field of a
    LottieLayerAnimation -- see there. offset=0.0 (the plain non-precomp
    case) returns anim unchanged, byte-identical to before this existed."""
    if offset == 0.0:
        return anim
    return anim._replace(**{
        f: _shift_lottie_keyframes_time(getattr(anim, f), offset)
        for f in ("position", "anchor", "scale", "rotation", "opacity")
    })


def _lottie_anim_static_tr(anim):
    """A resolved-static Lottie transform dict for one LottieLayerAnimation
    -- the shape lottie_matrix_from_static_transform expects. An animated
    field collapses to its own first keyframe."""
    p = _lottie_resolved_static(anim.position)
    return {"p": {"a": 0, "k": [p[0], p[1]]},
            "a": {"a": 0, "k": _lottie_resolved_static(anim.anchor)},
            "s": {"a": 0, "k": _lottie_resolved_static(anim.scale)},
            "r": {"a": 0, "k": _lottie_resolved_static(anim.rotation, scalar=True)}}


def _lottie_matte_clip_subpaths(matte_json, layer_by_ind, fps, comp_w, comp_h, counter, report):
    """Resolved-static clip geometry for a "td" track-matte layer, or None
    if it has no usable geometry.

    A track matte lives in the SAME composition as the layers it clips, so
    its geometry is resolved through its OWN ks plus its own same-comp
    parent chain and returned in exactly the space this file's baked
    geometry already uses, which is what to_point/_build_mask_chain
    expect: composition coordinates minus half the ROOT composition.

    comp_w/comp_h are deliberately the ROOT comp's dimensions even when
    this matte lives inside a differently-sized precomp asset. That looks
    wrong but is exactly right, and it's the same reason
    _lottie_transform_offsets can use root dims at every chain level:
    every stage of the generated chain evaluates to
    `to_point(that space's own coordinate - root_comp/2) + 0.5`, verified
    exact against independently composed AE matrices on a real 1000x500
    file whose assets are 400x400. The half-comp term is a fixed framing
    offset shared by the whole graph, not a per-composition one. That
    space is also precisely where build_group_lua inserts the clip: after
    a clipped layer's own transform and its own same-comp ancestors, but
    BEFORE any enclosing precomp instance's transform (see
    GroupNode.matte_clip_after). Clipping there rather than in final
    screen space is what lets the mask stay static while the whole
    clipped result still animates with its precomp instance -- a mask
    baked into screen space would be left behind the moment the instance
    moved or scaled.

    The matte is flattened to a single static shape: an animated matte
    transform or path collapses to its own first keyframe (tallied via
    note_approximated_matte). Confirmed adequate for the real file this
    was built against -- "Linkedin Reactions.json" mattes every reaction
    with a fully static circle."""
    nodes = lottie_shapes_to_nodes(matte_json.get("shapes", []), IDENTITY, 1.0, counter, report, fps)
    subpaths = []

    def collect(ns):
        for n in ns:
            if isinstance(n, ShapeNode):
                subpaths.extend(n.subpaths)
            else:
                collect(n.children)

    collect(nodes)
    if not subpaths:
        return None

    chain = [_parse_ks_animation(matte_json.get("ks", {}), fps)]
    chain.extend(_resolve_lottie_ancestor_chain(matte_json, layer_by_ind, fps, report))
    matrix = IDENTITY
    for anim in reversed(chain):
        matrix = mat_mul(matrix, lottie_matrix_from_static_transform(_lottie_anim_static_tr(anim)))

    half_w, half_h = comp_w / 2.0, comp_h / 2.0

    def place(pt):
        x, y = mat_point(matrix, pt[0], pt[1])
        return (x - half_w, y - half_h)

    out = []
    for sp in subpaths:
        placed = Subpath()
        placed.closed = True  # a matte is always a filled region
        for a in sp.anchors:
            x, y = place((a['x'], a['y']))
            placed.add(x, y,
                        place(a['in_cp']) if a['in_cp'] is not None else None,
                        place(a['out_cp']) if a['out_cp'] is not None else None)
        out.append(placed)
    return out


def _any_node_needs_live_transform(nodes):
    """True iff any node in these subtrees is a promoted shape group (or
    contains one) -- see _lottie_group_needs_promotion for why that has
    to force every enclosing transform live as well."""
    for n in nodes:
        if isinstance(n, ShapeNode):
            continue
        if n.needs_live_transform or _any_node_needs_live_transform(n.children):
            return True
    return False


def lottie_layer_to_group_node(layer_json, fps, counter, report, layer_by_ind,
                                 time_offset=0.0, extra_ancestors=(), visibility_window=None,
                                 matte_clip=None):
    """Builds one shape layer's own GroupNode -- children in LAYER-LOCAL
    space (shape-group "tr"s baked in via lottie_shapes_to_nodes, the
    layer's own "ks" deliberately NOT baked in here at all), plus a
    LottieLayerAnimation attached as .layer_animation carrying that ks
    (each of position/anchor/scale/rotation/opacity as either a plain
    static value or a lottie_keyframes(...) list), and its own AE parent
    chain (if any) resolved into .ancestor_animations -- see
    _resolve_lottie_ancestor_chain.

    Keeping ks separate from the geometry (rather than baking it in like
    an ordinary shape-group tr) is what lets the static-pose export
    and the animated export share this SAME parsed tree -- see
    _bake_lottie_layer_pose for how the static export applies it, and
    build_group_lua's own wrap hookup for how the animated export turns both this
    layer's own ks AND every resolved ancestor into real wrapping
    Transforms.

    time_offset/extra_ancestors/visibility_window: precomp-nesting
    plumbing, all no-ops (0.0/()/None) for every ROOT-level layer --
    see _collect_lottie_shape_layers, the only caller that ever passes
    non-default values, for what each one means."""
    # Only a real AE layer MASK ("masksProperties") is still unsupported and
    # worth flagging here. Track mattes ("tt"/"td") are decided entirely by
    # _collect_lottie_shape_layers now -- it either applies the clip and
    # tallies note_track_matte_applied, or flags the case it can't handle --
    # so counting them again here would report every successfully clipped
    # layer as "ignored".
    if layer_json.get("masksProperties"):
        report.note_skipped_mask_matte()
    if layer_json.get("ef"):
        report.note_skipped_effect_expression()

    ks = layer_json.get("ks", {})
    group = GroupNode(layer_json.get("nm") or f"Layer_{next(counter)}")
    group.children = lottie_shapes_to_nodes(layer_json.get("shapes", []), IDENTITY, 1.0, counter, report, fps)
    # Same propagate-upward rule _lottie_group_needs_promotion applies
    # within the group chain, extended one level further: if any shape
    # group below this layer is promoted, the LAYER's own ks can't be
    # baked into geometry either (that would put it inside the promoted
    # group's Transform, reversing AE's own composition order).
    if _any_node_needs_live_transform(group.children):
        group.needs_live_transform = True
    group.layer_animation = _shift_lottie_layer_animation_time(
        _parse_ks_animation(ks, fps, report=report), time_offset)
    own_ancestors = [_shift_lottie_layer_animation_time(a, time_offset)
                     for a in _resolve_lottie_ancestor_chain(layer_json, layer_by_ind, fps, report)]
    group.ancestor_animations = own_ancestors + list(extra_ancestors)
    group.visibility_window = visibility_window
    # A track matte shares this layer's OWN composition, so the clip goes in
    # once this layer's own transform and its same-comp ancestors have been
    # applied -- everything past that came from an enclosing precomp
    # instance. See _lottie_matte_clip_subpaths.
    group.matte_clip_subpaths = matte_clip
    group.matte_clip_after = len(own_ancestors)
    return group if group.children else None


_LOTTIE_MAX_PRECOMP_DEPTH = 10


def _lottie_layer_window(layer_json, time_offset, inherited_window):
    """Converts a layer's own raw ip/op (frame numbers in whatever comp
    DIRECTLY contains it) into a root-comp-absolute Lottie frame range,
    intersected with inherited_window (the already-root-absolute,
    already-intersected window of every enclosing precomp INSTANCE, or
    (-inf, inf) at the root -- see _collect_lottie_shape_layers).

    Lottie's own "op" is EXCLUSIVE (a layer is visible for ip <= t < op,
    the same convention Python's own slicing uses) -- confirmed against
    this file's own real Shapes.json: every layer's own last real
    keyframe sits at op-1 or earlier, never AT op itself."""
    ip = layer_json.get("ip", 0.0)
    op = layer_json.get("op", ip)
    root_ip, root_op = time_offset + ip, time_offset + op
    inh_ip, inh_op = inherited_window
    return max(root_ip, inh_ip), min(root_op, inh_op)


def _collect_lottie_shape_layers(raw_layers, fps, comp_w, comp_h, counter, report, time_offset, extra_ancestors,
                                   inherited_window, asset_by_id, visited_asset_ids, depth):
    """Recursively flattens raw_layers (the root comp's own "layers", or
    one precomp asset's own "layers") into a list of built shape-layer
    GroupNodes, in document order -- shared by parse_lottie_composition
    (the root call, time_offset=0.0/extra_ancestors=()/depth=0) and by
    itself for every ty==0 precomp-instance layer found along the way.

    Precomp instances are common in real exported files (e.g. two of a
    file's 8 root layers each referencing a whole nested precomp with 7
    and 4 of their OWN shape layers) -- skipping ty==0 as "not content,
    only a possible AE parent" would silently lose all of those shapes.

    Each precomp asset gets its OWN "ind" namespace (an asset's own
    layers number their "ind"/"parent" independently of the root's, and
    of every OTHER asset's) -- layer_by_ind is therefore built fresh,
    per composition, right here, never shared or looked up globally.

    time_offset: added to every keyframe "t" this LEVEL's own layers
    carry, converting them from "this comp's own local time" to
    root-comp-absolute (see _shift_lottie_layer_animation_time) -- 0.0
    at the root, else (enclosing time_offset + the precomp INSTANCE's
    own "st", i.e. the root-absolute frame at which this asset's own
    local frame 0 plays).

    extra_ancestors: LottieLayerAnimation list (nearest-first, already
    time-shifted to root-absolute) contributed by every ENCLOSING
    precomp instance -- appended after each layer's own same-comp
    ancestor chain, so a shape 2 precomps deep still gets its full real
    chain: own ancestors -> inner instance's own ks+ancestors -> outer
    instance's own ks+ancestors -> ... -- exactly mirroring true AE
    matrix composition order (child_world = parent ∘ child), same
    convention _resolve_lottie_ancestor_chain already established for a
    plain (non-precomp) parent chain.

    inherited_window: (ip, op) root-absolute, already intersected
    through every enclosing precomp instance's own visible window --
    (-inf, inf) at the root. A layer nested inside a precomp is only
    ever visible during the OVERLAP of its own [ip, op) and every
    instance wrapping it.

    visited_asset_ids/depth: cycle and runaway-nesting guards, same
    spirit as _resolve_lottie_ancestor_chain's own 50-level/seen-set
    protection -- a real file has one or two levels of nesting at most;
    10 is generous headroom, not a real expectation."""
    if depth > _LOTTIE_MAX_PRECOMP_DEPTH:
        report.note_unresolved_precomp()
        return []
    layer_by_ind = {l.get("ind"): l for l in raw_layers if l.get("ind") is not None}
    layers = []
    # A "td" layer is a track matte: never drawn itself, it instead clips the
    # RUN of following layers that carry "tt". Intervening ty==3 nulls don't
    # end that run (they render nothing), but any other renderable layer
    # without "tt" does. Confirmed against the real file this was built for
    # ("Linkedin Reactions.json"): each reaction precomp opens with one
    # circular matte followed by every layer belonging inside that circle.
    active_matte = None
    for layer_json in raw_layers:
        ty = layer_json.get("ty")
        # "hd" is AE's own layer-hidden switch and a "td" matte is never
        # drawn in its own right either, so importing either as ordinary
        # content invents artwork the source never shows. Skipped BEFORE
        # the type check so neither is also tallied as an unsupported TYPE,
        # which would be misleading. Both stay usable as parents
        # (layer_by_ind is built above, from every raw layer regardless).
        if layer_json.get("td"):
            active_matte = _lottie_matte_clip_subpaths(
                layer_json, layer_by_ind, fps, comp_w, comp_h, counter, report)
            if active_matte is None:
                report.note_skipped_mask_matte()
            continue
        if layer_json.get("hd"):
            report.note_skipped_mask_matte()
            continue
        matte_clip = None
        tt = layer_json.get("tt")
        if tt:
            # 1 = alpha, 3 = luma. A solid-filled matte shape has luma
            # coverage equal to its alpha coverage, so both reduce to the
            # same hard-edged clip here. 2 and 4 are the INVERTED variants
            # and would need the opposite region, so they're left unclipped
            # and flagged rather than silently clipped the wrong way round.
            if active_matte is not None and tt in (1, 3) and ty == 4:
                matte_clip = active_matte
                report.note_track_matte_applied()
            else:
                report.note_skipped_mask_matte()
        elif ty != 3:
            active_matte = None
        if ty == 4:
            window = _lottie_layer_window(layer_json, time_offset, inherited_window)
            node = lottie_layer_to_group_node(
                layer_json, fps, counter, report, layer_by_ind,
                time_offset=time_offset, extra_ancestors=extra_ancestors, visibility_window=[window],
                matte_clip=matte_clip)
            if node is not None:
                layers.append(node)
        elif ty == 0:
            ref_id = layer_json.get("refId")
            asset = asset_by_id.get(ref_id)
            if asset is None or ref_id in visited_asset_ids:
                report.note_unresolved_precomp()
                continue
            instance_window = _lottie_layer_window(layer_json, time_offset, inherited_window)
            own_ancestors = [_shift_lottie_layer_animation_time(a, time_offset)
                             for a in _resolve_lottie_ancestor_chain(layer_json, layer_by_ind, fps, report)]
            instance_anim = _shift_lottie_layer_animation_time(
                _parse_ks_animation(layer_json.get("ks", {}), fps, report=report), time_offset)
            child_extra_ancestors = [instance_anim] + own_ancestors + list(extra_ancestors)
            child_time_offset = time_offset + layer_json.get("st", 0.0)
            # comp_w/comp_h stay the ROOT composition's throughout, even for
            # a nested asset of a different size -- see
            # _lottie_matte_clip_subpaths for why that's correct rather than
            # an oversight.
            layers.extend(_collect_lottie_shape_layers(
                asset.get("layers", []), fps, comp_w, comp_h, counter, report, child_time_offset,
                child_extra_ancestors, instance_window, asset_by_id,
                visited_asset_ids | {ref_id}, depth + 1))
        else:
            report.note_unsupported_layer_type(ty)
    return layers


LottieComposition = collections.namedtuple(
    "LottieComposition", "fps width height in_point out_point name layers")


def parse_lottie_composition(lottie_path):
    """Top-level Lottie JSON load -- composition metadata plus every
    real shape (a root-level ty==4 layer, or one nested inside a ty==0
    precomp instance's own referenced asset, recursively -- see
    _collect_lottie_shape_layers), in document order. Every OTHER layer
    type (1 solid, 2 image, 3 null, 5 text, 13 camera, ...) is a hard
    skip for CONTENT purposes, tallied by type into the returned report
    rather than silently dropped -- but any layer, supported or not,
    can still be a real AE parent, so every layer's raw JSON is indexed
    by its own "ind" first (layer_by_ind, one per composition -- see
    _collect_lottie_shape_layers) so a shape layer's own parent CHAIN
    (see _resolve_lottie_ancestor_chain) can look up and apply an
    unsupported-type ancestor's own transform even though its own
    content is never imported.

    The returned layers are in PAINT order -- backmost first -- which is
    the REVERSE of the document order Lottie stores them in. Lottie/AE
    order layers front-to-back (layers[0] is the topmost layer in the AE
    timeline), whereas SVG -- and therefore build_group_lua, which stacks
    each successive sibling as the next Merge's FOREGROUND -- paints in
    document order with later content on top. Reversing here is what
    reconciles the two, and it's done on the flattened list, which is
    valid because flattening splices each precomp's own content in at its
    instance's position: that whole block therefore reverses as a unit
    and stays in the right z slot relative to its siblings.

    Getting this wrong is badly disguised: a file whose last root layer
    is a full-comp "BG" rectangle would put it in FRONT, hiding every
    animated layer behind it, so the render is a single static rectangle
    that looks like a total import failure rather than a z-order flip.
    A backmost ty==1 solid hides the problem, since this importer skips
    it as an unsupported type."""
    with open(lottie_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    report = LottieImportReport()
    counter = iter(range(1, 100000))
    fps = data.get("fr", 30.0)
    asset_by_id = {a.get("id"): a for a in data.get("assets", []) if a.get("layers")}
    layers = _collect_lottie_shape_layers(
        data.get("layers", []), fps, data.get("w", 100), data.get("h", 100), counter, report,
        0.0, [], (float("-inf"), float("inf")), asset_by_id, frozenset(), 0)
    layers.reverse()
    comp = LottieComposition(
        fps=fps, width=data.get("w", 100), height=data.get("h", 100),
        in_point=data.get("ip", 0.0), out_point=data.get("op", 0.0),
        name=os.path.splitext(os.path.basename(lottie_path))[0], layers=layers)
    return comp, report


def lottie_report_summary(lottie_path, log=None):
    """Parses only -- no Lua built at all. Lets the Animation tab show
    the whole parsing+translation pipeline's result (layer/shape/keyframe
    counts, every approximation/skip) without building any export."""
    def say(msg):
        if log:
            log(msg)

    comp, report = parse_lottie_composition(lottie_path)

    def count_shapes(nodes):
        total = 0
        for n in nodes:
            if isinstance(n, ShapeNode):
                total += 1
            else:
                total += count_shapes(n.children)
        return total

    animated_props = collections.Counter()
    for layer in comp.layers:
        anim = layer.layer_animation
        for prop_name in ("position", "anchor", "scale", "rotation", "opacity"):
            if _lottie_prop_is_animated(getattr(anim, prop_name)):
                animated_props[prop_name] += 1

    say(f"Lottie composition: {comp.width:g} x {comp.height:g} @ {comp.fps:g}fps, "
        f"frames {comp.in_point:g}-{comp.out_point:g}")
    say(f"Found {len(comp.layers)} shape layer(s), {count_shapes(comp.layers)} shape(s) total.")
    if animated_props:
        parts = ", ".join(f"{name} ({count} layer(s))" for name, count in sorted(animated_props.items()))
        say(f"Animated layer properties found: {parts}.")
    else:
        say("No animated layer properties found -- every layer is static.")
    if report.has_findings():
        say("Import notes -- some things were approximated or skipped:")
        for line in report.summary_lines():
            say(line)
    else:
        say("No unsupported features detected in this file.")
    return comp, report


def _walk_lottie_shape_nodes(node, fn):
    """Calls fn(shape_node) for every descendant ShapeNode of node
    (node itself included if it's already a ShapeNode) -- shared tree
    walk for both halves of the static bake below."""
    if isinstance(node, ShapeNode):
        fn(node)
    else:
        for c in node.children:
            _walk_lottie_shape_nodes(c, fn)


def _bake_lottie_layer_transform(layer_group, comp_w, comp_h):
    """The position/anchor/scale/rotation half of the static bake: a
    mat_point/mat_vector bake over every descendant ShapeNode's subpaths,
    using the SAME resolved-static-transform machinery
    lottie_matrix_from_static_transform already uses elsewhere. Only
    called (see _bake_lottie_layer_pose) when NONE of those four fields
    carries real animation -- an animated layer instead leaves
    geometry untouched and gets wrapped in a real Transform tool (see
    _wrap_layer_transform).

    comp_w/comp_h are the LOTTIE composition's own dimensions. Position
    is COMP-ABSOLUTE (unlike Anchor, which is layer-local, same
    convention as the geometry itself) -- baking the raw, uncentered
    position value straight into geometry left the comp's own top-left
    corner mapping to Fusion's frame-center once the LATER to_point
    conversion ran (see _lottie_center_point's own docstring for the full
    diagnosis, confirmed against a real off-screen composite), so it's
    centered here the same way _lottie_center_point centers Position for
    the animated/wrapped case -- both paths must resolve a comp-center
    Position to the same Fusion frame-center result."""
    anim = layer_group.layer_animation
    px, py = _lottie_resolved_static(anim.position)[:2]
    tr = {"p": {"a": 0, "k": [px - comp_w / 2.0, py - comp_h / 2.0]},
          "a": {"a": 0, "k": _lottie_resolved_static(anim.anchor)},
          "s": {"a": 0, "k": _lottie_resolved_static(anim.scale)},
          "r": {"a": 0, "k": _lottie_resolved_static(anim.rotation, scalar=True)}}
    m = lottie_matrix_from_static_transform(tr)

    def bake(shape):
        for sp in shape.subpaths:
            for a in sp.anchors:
                a['x'], a['y'] = mat_point(m, a['x'], a['y'])
                if a['in_cp'] is not None:
                    a['in_cp'] = mat_point(m, *a['in_cp'])
                if a['out_cp'] is not None:
                    a['out_cp'] = mat_point(m, *a['out_cp'])

    _walk_lottie_shape_nodes(layer_group, bake)


def _bake_lottie_layer_static_opacity(layer_group):
    """The opacity half of the static bake -- a plain multiply onto every
    descendant's own opacity/stroke_opacity. Only called (see
    _bake_lottie_layer_pose) when opacity itself doesn't carry real
    animation -- an animated layer's opacity instead threads through as
    layer_opacity_anim (see build_group_lua/build_shape_lua), wiring each
    shape's own Background to a real per-shape Alpha spline."""
    anim = layer_group.layer_animation
    static_opacity = _lottie_resolved_static(anim.opacity, scalar=True) / 100.0

    def bake(shape):
        shape.opacity *= static_opacity
        if shape.stroke_opacity is not None:
            shape.stroke_opacity *= static_opacity

    _walk_lottie_shape_nodes(layer_group, bake)


def _lottie_scale_signs(scale):
    """(sign_x, sign_y) of a scale field, as +1.0/-1.0, plus whether the
    sign is CONSTANT across every keyframe. A negative Lottie scale is a
    mirror, and a mirror can't be expressed by Fusion's Size/Aspect (see
    _normalize_lottie_mirrored_scale)."""
    if not _lottie_prop_is_animated(scale):
        return (-1.0 if scale[0] < 0 else 1.0, -1.0 if scale[1] < 0 else 1.0), True
    signs = set()
    for kf in scale:
        v = kf[1]
        signs.add((v[0] < 0, v[1] < 0))
    first = scale[0][1]
    return ((-1.0 if first[0] < 0 else 1.0, -1.0 if first[1] < 0 else 1.0), len(signs) == 1)


def _lottie_abs_scale_field(scale):
    """The same scale field with its x/y magnitudes kept but signs dropped."""
    if not _lottie_prop_is_animated(scale):
        return [abs(scale[0]), abs(scale[1])] + list(scale[2:])
    out = []
    for kf in scale:
        v = kf[1]
        out.append((kf[0], [abs(v[0]), abs(v[1])] + list(v[2:])) + tuple(kf[2:]))
    return out


def _reflect_lottie_geometry(node, anchor, signs):
    """Mirrors every descendant ShapeNode's geometry about anchor. Point
    order and each anchor's in_cp/out_cp roles are preserved -- a
    reflection keeps the same parameterisation, it just reverses winding,
    which nothing here depends on (the fill chain is even-odd/XOR, and
    _weighted_pivot_point takes each shape's area by absolute value)."""
    ax, ay = anchor[0], anchor[1]
    sx, sy = signs

    def refl(pt):
        return (ax + sx * (pt[0] - ax), ay + sy * (pt[1] - ay))

    def apply(n):
        if isinstance(n, ShapeNode):
            for sp in n.subpaths:
                for a in sp.anchors:
                    a['x'], a['y'] = refl((a['x'], a['y']))
                    if a['in_cp'] is not None:
                        a['in_cp'] = refl(a['in_cp'])
                    if a['out_cp'] is not None:
                        a['out_cp'] = refl(a['out_cp'])
        else:
            for c in n.children:
                apply(c)

    apply(node)


def _normalize_lottie_mirrored_scale(node, report):
    """Folds a NEGATIVE Lottie scale -- an AE mirror -- out of a live
    transform and into its own geometry, leaving the transform with a
    purely positive scale.

    Fusion's Transform expresses scale as Size plus Aspect, and this file
    derives them as Size=sx/100, Aspect=sy/sx. A negative Lottie scale
    therefore produced a negative Size or Aspect, which Fusion does not
    render -- the layer simply vanishes. For example, a face that
    mirrors one eye and one eyebrow with scale [507.271, -507.271] loses
    exactly that eye and eyebrow.

    The rewrite is EXACT, not an approximation, and needs no new Fusion
    construct: since diag(sx,sy) = diag(|sx|,|sy|) * diag(sign), and AE
    composes as `p + R*diag(s)*(local - a)`, pushing diag(sign) onto the
    geometry about the anchor a gives an identical result with |s| in the
    transform. It relies on the sign being CONSTANT (a mirror can't be
    animated through zero) and on there being no other live transform
    between this node and its geometry -- both are checked, and either
    failing falls back to the plain positive-magnitude behaviour with a
    flag rather than silently mirroring the wrong thing.

    Only ever needed for a LIVE transform. A baked one already applies
    its full matrix to the geometry (see _bake_lottie_layer_transform),
    negative scale and all, which was always correct."""
    if isinstance(node, ShapeNode):
        return

    def has_live_descendant(n):
        for c in n.children:
            if isinstance(c, ShapeNode):
                continue
            anim = c.layer_animation
            if anim is not None and (_layer_has_transform_animation(anim) or c.needs_live_transform):
                return True
            if has_live_descendant(c):
                return True
        return False

    anim = node.layer_animation
    if anim is not None and (_layer_has_transform_animation(anim) or node.needs_live_transform):
        signs, constant = _lottie_scale_signs(anim.scale)
        if signs != (1.0, 1.0):
            if not constant or has_live_descendant(node):
                report.note_approximated_mirrored_scale()
            else:
                _reflect_lottie_geometry(node, _lottie_resolved_static(anim.anchor), signs)
                node.layer_animation = anim._replace(scale=_lottie_abs_scale_field(anim.scale))
    for child in node.children:
        _normalize_lottie_mirrored_scale(child, report)


def _bake_lottie_layer_pose(layer_group, comp_w, comp_h):
    """The static-pose bake, scoped to ONLY the fields the animated
    export isn't animating for real -- called unconditionally (see
    build_lottie_tools), but each half independently no-ops when its own
    field(s) carry real animation, leaving that part of the job to
    build_group_lua's own _wrap_layer_transform/layer_opacity_anim
    handling instead. A layer with nothing animated at all (or no
    layer_animation, e.g. every SVG-sourced group) is fully baked.

    comp_w/comp_h are the LOTTIE composition's own dimensions -- see
    _bake_lottie_layer_transform for why the transform half needs them."""
    anim = layer_group.layer_animation
    if anim is None:
        return
    # needs_live_transform (a promoted shape group somewhere below) blocks
    # the transform bake even for a layer whose own ks is fully static --
    # see lottie_layer_to_group_node for the composition-order reason.
    if not _layer_has_transform_animation(anim) and not layer_group.needs_live_transform:
        _bake_lottie_layer_transform(layer_group, comp_w, comp_h)
    if not _lottie_field_is_really_animated(anim.opacity):
        _bake_lottie_layer_static_opacity(layer_group)


# ==========================================================================
# Node-graph consolidation: one object per VISUAL object, not one per
# time segment.
#
# A real AE-authored Lottie routinely re-creates the same artwork once per
# time segment rather than animating one copy across the whole clip -- each
# segment living in its own precomp, each with its own ip/op window. The
# real Shapes.json is exactly this: 15 imported shape layers that are only
# FOUR objects, each appearing as 3-4 consecutive segments (cyan diamond
# root ind4 103-200 -> asset2 ind1 200-279 -> asset1 ind4 279-363 ->
# asset1 ind1 363-374, and similarly for the circle, rect and arc). Imported
# literally that is 15 separate shape chains, which is both a mess to hand-
# animate afterwards and actively misleading about what the artwork is.
#
# This pass recognises those segments and emits ONE shape per object, driven
# by a PADDED, TIME-SWITCHED transform chain: the merged chain has as many
# levels as the deepest segment needs, and each level carries whichever
# segment is active at time t (levels a shorter segment doesn't have are
# padded with a real identity transform). Because the segments' windows are
# disjoint, exactly one is active at any time, so the product of the merged
# levels reproduces that segment's own composition EXACTLY -- no matrix
# decomposition, and therefore none of the shear/non-uniform-scale error a
# "flatten the chain into one Transform by sampling" approach would incur.
# Each segment's own keyframes keep their own real eases.
#
# Safety conditions, all required before two layers are merged:
#   - identical geometry AND fill/stroke (see _lottie_shape_identity) --
#     compared on PURE LOCAL geometry, which is what promoting every
#     non-identity shape-group transform buys (see
#     _lottie_group_needs_promotion);
#   - DISJOINT visibility windows. This is the load-bearing check: two
#     identical shapes that are on screen at the SAME time are genuinely
#     two objects (a pair of matching dots, say) and merging them would
#     delete one. Overlap anywhere in the class means leave it alone.
#   - a single linear shape chain per layer (see
#     _lottie_linear_shape_chain) -- a layer with several shapes, or
#     branching groups, is passed through untouched rather than guessed at.
#
# Known limitation, deliberately not worked around: merging collapses each
# object to ONE z-order slot (its earliest segment's). That is faithful only
# if the segments keep a consistent front-to-back order across every window,
# which is the norm for this pattern (and holds for Shapes.json) but is not
# guaranteed in general.
# ==========================================================================
_LOTTIE_MERGE_EPS = 1e-6


def _lottie_shape_identity(shape):
    """A hashable identity for one ShapeNode: its PURE LOCAL geometry plus
    everything that decides how it is painted. Two layers matching on this
    are the same artwork and are candidates for being time segments of one
    object (see this section's own module comment).

    Rounded before hashing -- these coordinates come out of independent
    float paths per segment (different precomp nesting, different baked
    group transforms) so bit-exact equality is too strict; 1e-6 of a
    Lottie pixel is far below anything visible."""
    def r(v):
        return round(float(v), 6)

    geom = []
    for sp in shape.subpaths:
        pts = []
        for a in sp.anchors:
            pts.append((r(a['x']), r(a['y']),
                        tuple(r(c) for c in a['in_cp']) if a['in_cp'] is not None else None,
                        tuple(r(c) for c in a['out_cp']) if a['out_cp'] is not None else None))
        geom.append((tuple(pts), bool(sp.closed)))
    def col(c):
        return tuple(r(v) for v in c) if c else None
    return (tuple(geom), shape.has_fill, col(shape.color), r(shape.opacity),
            shape.has_stroke, col(shape.stroke_color), r(shape.stroke_width or 0.0),
            r(shape.stroke_opacity if shape.stroke_opacity is not None else 0.0),
            shape.fill_gradient is not None, shape.stroke_gradient is not None,
            shape.blur_stddev_px, bool(shape.drop_shadow), bool(shape.inner_shadow))


def _lottie_linear_shape_chain(layer):
    """(shape, [promoted group anims INNERMOST-first]) for a layer whose
    content is a single ShapeNode reached through a straight chain of
    GroupNodes -- or None for anything else (several shapes, branching
    groups, a clip), which this pass then passes through untouched rather
    than trying to merge.

    The reversal matters: descending the tree yields groups outermost-
    first, while the emitted Transform chain applies innermost-first (see
    build_group_lua, which wraps children before applying a node's own
    transform)."""
    groups = []
    node = layer
    for _depth in range(64):
        if len(node.children) != 1 or node.clip_subpaths:
            return None
        child = node.children[0]
        if isinstance(child, ShapeNode):
            return child, list(reversed(groups))
        if child.clip_subpaths:
            return None
        if child.layer_animation is not None:
            if not child.needs_live_transform:
                return None
            groups.append((child.layer_animation,
                            child.transform_space or _LOTTIE_SPACE_SHAPE_GROUP))
        node = child
    return None


def _lottie_segment_levels(layer):
    """(shape, group_levels, own_level, ancestor_levels) for one candidate
    segment, or None if this layer can't be consolidated. Mirrors exactly
    the level stack build_group_lua would emit for it."""
    chain = _lottie_linear_shape_chain(layer)
    if chain is None or layer.layer_animation is None:
        return None
    shape, groups = chain
    return (shape, [a for a, _s in groups], layer.layer_animation, list(layer.ancestor_animations))


def _lottie_identity_anim(fps):
    """A transform that provably does nothing, in ANY of the three spaces
    -- used to pad a merged chain level that a shorter segment doesn't
    have (see this section's own module comment). Position and Anchor at
    the origin make Center and Pivot collapse to their space's own
    neutral values, so the level composes as the identity."""
    return LottieLayerAnimation(position=[0.0, 0.0], anchor=[0.0, 0.0], scale=[100.0, 100.0],
                                 rotation=0.0, opacity=100.0, fps=fps)


def _lottie_field_value_at(field, t, scalar=False):
    """One LottieLayerAnimation field's value at time t, always returned
    as a LIST (a 1-element one for a scalar field) so callers don't have
    to care whether the source was static or animated.

    Holds the first value before the first keyframe and the last after
    the last -- Lottie's own behaviour, so those two cases are EXACT.
    Only a t strictly between two keyframes is interpolated, and only
    linearly (this file has no bezier-ease evaluator); callers flag that
    via note_approximated_merge_boundary."""
    if not _lottie_prop_is_animated(field):
        return [float(field)] if scalar else [float(v) for v in field]
    kfs = [(kt, [float(x) for x in (kv if isinstance(kv, list) else [kv])])
           for (kt, kv, *_rest) in field]
    if t <= kfs[0][0] + _LOTTIE_MERGE_EPS:
        return list(kfs[0][1])
    if t >= kfs[-1][0] - _LOTTIE_MERGE_EPS:
        return list(kfs[-1][1])
    for (t0, v0), (t1, v1) in zip(kfs, kfs[1:]):
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0) if t1 != t0 else 0.0
            return [a + f * (b - a) for a, b in zip(v0, v1)]
    return list(kfs[-1][1])


def _lottie_field_needs_interpolation(field, t):
    """True iff reading field at t lands strictly BETWEEN two keyframes,
    i.e. the one case _lottie_field_value_at has to approximate."""
    if not _lottie_prop_is_animated(field):
        return False
    return field[0][0] + _LOTTIE_MERGE_EPS < t < field[-1][0] - _LOTTIE_MERGE_EPS


def _lottie_merge_field(per_segment, windows, scalar, report):
    """Merges one transform field across a class's segments into either a
    single static value (when every segment agrees and none animates) or
    ONE keyframe list that equals each segment's own value throughout that
    segment's own window.

    Per segment: keep its real keyframes inside [ip, op) WITH their own
    eases and spatial tangents, then pin the value at ip and at op-1 if it
    has no keyframe there. Consecutive segments' windows touch, so the
    handover is a single-frame snap from one segment's last value to the
    next segment's first -- the faithful equivalent of the source cutting
    instantly from one copy to another."""
    resolved = [_lottie_field_value_at(f, w[0], scalar) for f, w in zip(per_segment, windows)]
    if all(not _lottie_prop_is_animated(f) for f in per_segment) and all(
            all(abs(a - b) < _LOTTIE_MERGE_EPS for a, b in zip(v, resolved[0])) for v in resolved):
        return resolved[0][0] if scalar else list(resolved[0])

    merged = {}
    for field, (ip, op) in zip(per_segment, windows):
        if op - ip <= _LOTTIE_MERGE_EPS:
            continue
        kept = {}
        if _lottie_prop_is_animated(field):
            for kf in field:
                kt = kf[0]
                if ip - _LOTTIE_MERGE_EPS <= kt < op - _LOTTIE_MERGE_EPS:
                    value = kf[1] if isinstance(kf[1], list) else [kf[1]]
                    kept[kt] = ([float(x) for x in value], kf[2], kf[3], kf[4], kf[5])
        for edge in (ip, op - 1.0):
            if edge < ip - _LOTTIE_MERGE_EPS or edge >= op - _LOTTIE_MERGE_EPS:
                continue
            if any(abs(t - edge) < _LOTTIE_MERGE_EPS for t in kept):
                continue
            kept[edge] = (_lottie_field_value_at(field, edge, scalar), None, None, None, None)
            if _lottie_field_needs_interpolation(field, edge) and report is not None:
                report.note_approximated_merge_boundary()
        merged.update(kept)
    return [(t, v, o, i, so, si, False)
            for t, (v, o, i, so, si) in sorted(merged.items())]


def _lottie_merge_level(per_segment_anims, windows, report):
    """One merged chain level: every field merged independently across the
    segments, with a None entry (a segment that has no level this deep)
    standing in as a real identity transform."""
    fps = next((a.fps for a in per_segment_anims if a is not None), 30.0)
    anims = [a if a is not None else _lottie_identity_anim(fps) for a in per_segment_anims]
    return LottieLayerAnimation(
        position=_lottie_merge_field([a.position for a in anims], windows, False, report),
        anchor=_lottie_merge_field([a.anchor for a in anims], windows, False, report),
        scale=_lottie_merge_field([a.scale for a in anims], windows, False, report),
        rotation=_lottie_merge_field([a.rotation for a in anims], windows, True, report),
        opacity=_lottie_merge_field([a.opacity for a in anims], windows, True, report),
        fps=fps)


def _lottie_merged_center_keyframes(position, anchor):
    """Position keyframes already reduced by an ANIMATED anchor, sampled
    on the union of both fields' own keyframe times -- only reachable for
    a consolidated object whose merged segments disagree about their
    anchor (see _wrap_layer_transform). Center is defined as
    to_point(position - anchor - offset), so when the anchor moves it has
    to be subtracted per keyframe rather than once from its resolved
    static value, or Center and the animated Pivot would describe
    different points."""
    times = sorted({kf[0] for kf in position} if _lottie_prop_is_animated(position) else set()) or []
    times = sorted(set(times) | ({kf[0] for kf in anchor} if _lottie_prop_is_animated(anchor) else set()))
    if not times:
        p = _lottie_field_value_at(position, 0.0)
        a = _lottie_field_value_at(anchor, 0.0)
        return [p[0] - a[0], p[1] - a[1]]
    out = []
    for t in times:
        p = _lottie_field_value_at(position, t)
        a = _lottie_field_value_at(anchor, t)
        out.append((t, [p[0] - a[0], p[1] - a[1]], None, None, None, None, False))
    return out


def _lottie_windows_disjoint(windows):
    """True iff no two of these (ip, op) intervals overlap -- the check
    that keeps two genuinely simultaneous copies of the same artwork from
    being collapsed into one (see this section's own module comment)."""
    ordered = sorted(windows)
    return all(ordered[i][1] <= ordered[i + 1][0] + _LOTTIE_MERGE_EPS
               for i in range(len(ordered) - 1))


def _lottie_merge_intervals(windows):
    """Union of (ip, op) intervals, coalescing any that touch or overlap
    -- a merged object is visible for the union of its segments' own
    windows, and consecutive segments touch exactly, so this normally
    collapses to one interval."""
    out = []
    for ip, op in sorted(windows):
        if out and ip <= out[-1][1] + _LOTTIE_MERGE_EPS:
            out[-1] = (out[-1][0], max(out[-1][1], op))
        else:
            out.append((ip, op))
    return out


def _lottie_build_merged_layer(members, counter, report):
    """Builds the single consolidated layer node for one identity class.
    members is [(layer, shape, groups, own, ancestors)], already known to
    have disjoint windows. Returns a GroupNode shaped exactly like a
    normal Lottie layer node, so build_group_lua needs no special case."""
    members = sorted(members, key=lambda m: m[0].visibility_window[0][0])
    windows = [m[0].visibility_window[0] for m in members]

    group_depth = max(len(m[2]) for m in members)
    anc_depth = max(len(m[4]) for m in members)
    merged_groups = [
        _lottie_merge_level([m[2][j] if j < len(m[2]) else None for m in members], windows, report)
        for j in range(group_depth)]
    merged_own = _lottie_merge_level([m[3] for m in members], windows, report)
    merged_ancestors = [
        _lottie_merge_level([m[4][j] if j < len(m[4]) else None for m in members], windows, report)
        for j in range(anc_depth)]

    base_name = members[0][0].name
    node = members[0][1]
    for depth_idx, anim in enumerate(merged_groups):
        wrapper = GroupNode(f"{base_name}_Grp{depth_idx + 1}")
        wrapper.children = [node]
        wrapper.layer_animation = anim
        wrapper.transform_space = _LOTTIE_SPACE_SHAPE_GROUP
        wrapper.needs_live_transform = True
        node = wrapper

    merged = GroupNode(base_name)
    merged.children = [node]
    merged.layer_animation = merged_own
    merged.transform_space = _LOTTIE_SPACE_OWN_LAYER
    merged.needs_live_transform = True
    merged.ancestor_animations = merged_ancestors
    merged.visibility_window = _lottie_merge_intervals(windows)
    # Every member shares one clip (it's part of the identity key that
    # bucketed them), so the first member's carries over unchanged. Clamp
    # matte_clip_after to the merged ancestor depth: padding can only make
    # the chain longer, never shorter, but a clamp keeps the invariant
    # local rather than assumed.
    merged.matte_clip_subpaths = members[0][0].matte_clip_subpaths
    merged.matte_clip_after = min(members[0][0].matte_clip_after, len(merged_ancestors))
    return merged


def _consolidate_lottie_layers(layers, report):
    """Collapses each set of layers that are really time SEGMENTS of one
    visual object into a single layer (see this section's own module
    comment). Returns a new layer list in the original document order,
    each merged object taking its earliest segment's place.

    Must run BEFORE _bake_lottie_layer_pose: identity matching needs pure
    local geometry, and baking a layer's own ks into its geometry would
    make two segments of the same object compare as different shapes."""
    parsed, buckets = {}, collections.OrderedDict()
    for index, layer in enumerate(layers):
        info = _lottie_segment_levels(layer)
        if info is None or not layer.visibility_window or len(layer.visibility_window) != 1:
            parsed[index] = None
            continue
        shape, groups, own, ancestors = info
        parsed[index] = (layer, shape, groups, own, ancestors)
        # The matte clip is part of a layer's identity: two copies of the
        # same artwork cropped to DIFFERENT mattes are not interchangeable,
        # and the merged layer only carries one clip.
        clip_key = None
        if layer.matte_clip_subpaths:
            clip_key = (layer.matte_clip_after, tuple(
                (round(a['x'], 6), round(a['y'], 6))
                for sp in layer.matte_clip_subpaths for a in sp.anchors))
        buckets.setdefault((_lottie_shape_identity(shape), clip_key), []).append(index)

    merged_at, drop = {}, set()
    for indices in buckets.values():
        if len(indices) < 2:
            continue
        members = [parsed[i] for i in indices]
        if not _lottie_windows_disjoint([m[0].visibility_window[0] for m in members]):
            continue
        merged_at[indices[0]] = _lottie_build_merged_layer(members, None, report)
        drop.update(indices[1:])
        report.note_consolidated_object(len(indices))

    out = []
    for index, layer in enumerate(layers):
        if index in drop:
            continue
        out.append(merged_at.get(index, layer))
    return out


def build_lottie_tools(lottie_path, comp_w, comp_h, log=None, orientation="horizontal",
                        scale_mode="native", origin=(0.0, 0.0), comp_fps=None):
    """Parses lottie_path and returns (tools, label, tool_count) --
    signature deliberately mirrors build_svg_tools field-for-field. A
    layer's own Position/Anchor/Scale/Rotation/Opacity become real Fusion
    keyframes when the source file actually animates them (see
    _wrap_layer_transform/build_group_lua's own layer_opacity_anim
    handling); a layer with nothing animated pastes at its own static
    pose (see _bake_lottie_layer_pose). Shape geometry itself
    (fill/stroke/path) always renders static.

    comp_fps: the DESTINATION Fusion comp's own project frame rate (see
    connect_to_resolve), or None if it couldn't be read. The source
    file's "fr" and the destination comp's own frame rate are two
    independent numbers, so writing raw Lottie frame numbers straight
    into Fusion's own KeyFrames table would make a 60fps-authored file
    pasted onto e.g. a 24fps comp play back 2.5x too slow (frame 179 not
    arriving until real-time 179/24s instead of the source's own intended
    179/60s). time_scale = comp_fps / comp.fps (1.0, a no-op, when
    comp_fps is None -- e.g. no live Fusion connection) converts every
    raw frame number ONCE, threaded down through build_group_lua into
    every place one reaches a KeyFrames table (see
    _lottie_keyframe_specs).

    report's findings are only fully known once build_group_lua has run
    (some approximations -- animated anchor, derived-scalar-ease, stepped
    keyframes -- are only discovered while wrapping each layer's own
    Transform), so the summary is printed AFTER that call, not right
    after parsing."""
    def say(msg):
        if log:
            log(msg)

    comp, report = parse_lottie_composition(lottie_path)
    time_scale = (comp_fps / comp.fps) if comp_fps and comp.fps else 1.0
    say(f"Lottie composition: {comp.width:g} x {comp.height:g} @ {comp.fps:g}fps")
    if comp_fps and abs(time_scale - 1.0) > 1e-6:
        say(f"Destination comp is {comp_fps:g}fps -- rescaling animation timing by {time_scale:.4f}x.")
    say(f"Found {len(comp.layers)} shape layer(s).")

    # Mirror-normalization runs before BOTH of the passes below: it edits
    # geometry, so consolidation's identity matching has to see the result
    # (a mirrored copy of a shape is genuinely not the same shape), and it
    # only ever touches transforms that stay live, which the bake must
    # then leave alone. See _normalize_lottie_mirrored_scale.
    for layer in comp.layers:
        _normalize_lottie_mirrored_scale(layer, report)

    # Consolidation runs BEFORE the static-pose bake on purpose -- see
    # _consolidate_lottie_layers (identity matching needs pure local
    # geometry, which baking a layer's own ks would destroy).
    layers = _consolidate_lottie_layers(comp.layers, report)
    if len(layers) != len(comp.layers):
        say(f"Consolidated into {len(layers)} continuously-animated object(s) "
            f"(the source re-creates the same artwork once per time segment).")

    for layer in layers:
        _bake_lottie_layer_pose(layer, comp.width, comp.height)

    fit_scale = compute_svg_scale(scale_mode, comp.width, comp.height, comp_w, comp_h)
    to_point, to_vector = make_lottie_converter(comp_w, comp_h, scale=fit_scale)

    run_id = uuid.uuid4().hex[:6]
    top = GroupNode(comp.name)
    top.children = layers

    names = LuaNameAllocator(suffix=run_id)
    positions = PastePositions(SVG_X_SPACING, SVG_Y_SPACING, orientation, origin=origin)
    slot_counter = [0]

    built = build_group_lua(top, to_point, to_vector, comp_w, comp_h, 0, slot_counter, names, positions,
                             is_root=True, report=report, lottie_comp_size=(comp.width, comp.height),
                             recenter_overall_pivot=True, time_scale=time_scale,
                             lottie_comp_span=(comp.in_point, comp.out_point))

    if report.has_findings():
        say("Import notes -- some things were approximated or skipped:")
        for line in report.summary_lines():
            say(line)
    else:
        say("No unsupported features detected in this file.")

    if not built.tools:
        raise ValueError("No fillable or strokeable shapes were found in this Lottie file.")
    group_name = names.safe_name(top.name, "Group")
    tools = [build_single_group_wrapper_lua(group_name, built.tools, built.output_name,
                                              built.output_source, origin)]
    return tools, group_name, len(tools)


def lottie_to_lua(lottie_path, comp_w, comp_h, log=None, orientation="horizontal",
                   scale_mode="native", origin=(0.0, 0.0), comp_fps=None):
    """Parses lottie_path and returns (lua_paste_text, label, tool_count) --
    a single file wrapped in its own paste-text envelope. See
    build_lottie_tools for the params, and build_lotties_to_lua for
    combining several files into ONE envelope."""
    tools, label, tool_count = build_lottie_tools(
        lottie_path, comp_w, comp_h, log=log, orientation=orientation, scale_mode=scale_mode, origin=origin,
        comp_fps=comp_fps)
    lua_text = "{\n    Tools = ordered() {\n        " + ",\n        ".join(tools) + "\n    }\n}"
    return lua_text, label, tool_count


def build_lotties_to_lua(lottie_paths, comp_w, comp_h, log=None, orientation="horizontal",
                          scale_mode="native", grid_cols=None, grid_spacing_x=250.0, grid_spacing_y=150.0,
                          comp_fps=None):
    """Combines every file in lottie_paths into ONE paste-text payload --
    same grid-fan-out approach as build_svgs_to_lua, see there."""
    def say(msg):
        if log:
            log(msg)

    if grid_cols is None:
        grid_cols = max(1, math.ceil(math.sqrt(len(lottie_paths))))

    all_tools, labels = [], []
    for index, lottie_path in enumerate(lottie_paths):
        col, row = index % grid_cols, index // grid_cols
        origin = (col * grid_spacing_x, row * grid_spacing_y)
        try:
            tools, label, _ = build_lottie_tools(
                lottie_path, comp_w, comp_h, log=log, orientation=orientation, scale_mode=scale_mode,
                origin=origin, comp_fps=comp_fps)
        except Exception as exc:
            say(f"[ERROR] Skipping {lottie_path}: {exc}")
            continue
        all_tools.extend(tools)
        labels.append(label)

    if not all_tools:
        raise ValueError("None of the queued Lottie files had any fillable or strokeable shapes to draw.")

    lua_text = "{\n    Tools = ordered() {\n        " + ",\n        ".join(all_tools) + "\n    }\n}"
    return lua_text, labels, len(all_tools)


def run_lottie_import(app, lottie_path, origin=(0.0, 0.0)):
    """Runs on a background thread: parses lottie_path and copies Fusion's
    own paste-text to the OS clipboard -- mirrors run_import exactly,
    see there for why this is a plain clipboard operation rather than a
    live comp.Paste() call."""
    log = app._log
    comp_w, comp_h = app._comp_w or 1920, app._comp_h or 1080
    orientation = app.lottie_paste_orientation_var.get().lower()
    scale_mode = "timeline" if app.lottie_scale_mode_var.get().startswith("Timeline") else "native"

    log(f"Parsing {lottie_path} …")
    lua_text, label, tool_count = lottie_to_lua(lottie_path, comp_w, comp_h, log=log, orientation=orientation,
                                                 scale_mode=scale_mode, origin=origin, comp_fps=app._comp_fps)

    app._set_os_clipboard(lua_text)
    log(f"Copied group {label!r} to your clipboard. Click into Fusion's Flow view and press Ctrl+V.")


def run_lottie_import_all(app, lottie_paths):
    """Runs on a background thread: parses every queued Lottie file and
    copies ONE combined paste-text payload -- mirrors run_import_all."""
    log = app._log
    comp_w, comp_h = app._comp_w or 1920, app._comp_h or 1080
    orientation = app.lottie_paste_orientation_var.get().lower()
    scale_mode = "timeline" if app.lottie_scale_mode_var.get().startswith("Timeline") else "native"

    log(f"Parsing {len(lottie_paths)} queued Lottie file(s) …")
    lua_text, labels, tool_count = build_lotties_to_lua(
        lottie_paths, comp_w, comp_h, log=log, orientation=orientation, scale_mode=scale_mode,
        grid_spacing_x=app.GRID_SPACING_X, grid_spacing_y=app.GRID_SPACING_Y, comp_fps=app._comp_fps)

    app._set_os_clipboard(lua_text)
    skipped = len(lottie_paths) - len(labels)
    skipped_note = f" ({skipped} file(s) skipped -- see log above)" if skipped else ""
    log(f"Copied {len(labels)} file(s), {tool_count} top-level tool(s) total, to your clipboard{skipped_note}. "
        "Click into Fusion's Flow view and press Ctrl+V once to paste the whole batch.")


# ==========================================================================
# Main application
# ==========================================================================
