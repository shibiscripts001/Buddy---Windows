#!/usr/bin/env python3
"""
Color Engine for Color Palette Manager
Non-UI logic for color math, harmonies, theme variants, gradients, false color, APCA/WCAG, etc.
"""

import math
import random
import colorsys
import struct
import zipfile
import io
import json
import zlib
import os
import re

try:
    from PIL import Image, ImageDraw, ImageOps, ImageEnhance, ImageFont, ImageChops
    _IMAGING_AVAILABLE = True
except ImportError:
    _IMAGING_AVAILABLE = False

# --- Helper Functions ---
_GAMMA = 2.4

def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** _GAMMA

def _linear_to_srgb_f(c):
    """Like _linear_to_srgb but returns an unrounded 0..1 float (for high-precision export)."""
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / _GAMMA)) - 0.055

def _linear_to_srgb(c):
    return max(0, min(255, round(_linear_to_srgb_f(c) * 255)))

def hex_to_rgb_tuple(hex_color):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join([c*2 for c in hex_color])
    if len(hex_color) != 6:
        return (255, 255, 255)
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

def rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"

def relative_luminance(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

def contrasting_text_color(hex_color):
    return "#1c1c1c" if relative_luminance(hex_color) > 0.55 else "#f0f0f0"

def adjust_brightness(hex_color, amount):
    r, g, b = hex_to_rgb_tuple(hex_color)
    return rgb_to_hex((r + amount, g + amount, b + amount))

# --- WCAG 2.1 & APCA Contrast ---
def _wcag_relative_luminance(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    r, g, b = (_srgb_to_linear(c) for c in (r, g, b))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def wcag_contrast_ratio(hex_a, hex_b):
    l1 = _wcag_relative_luminance(hex_a)
    l2 = _wcag_relative_luminance(hex_b)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)

_APCA_COEFFS = (0.2126729, 0.7151522, 0.0721750)

def _apca_srgb_to_y(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    rc, gc, bc = _APCA_COEFFS
    return ((r / 255.0) ** 2.4) * rc + ((g / 255.0) ** 2.4) * gc + ((b / 255.0) ** 2.4) * bc

def apca_contrast(text_hex, bg_hex):
    norm_bg, norm_txt = 0.56, 0.57
    rev_txt, rev_bg = 0.62, 0.65
    blk_thrs, blk_clmp = 0.022, 1.414
    scale = 1.14
    lo_offset = 0.027
    lo_clip = 0.1
    delta_y_min = 0.0005

    txt_y = _apca_srgb_to_y(text_hex)
    bg_y = _apca_srgb_to_y(bg_hex)
    txt_y = txt_y if txt_y > blk_thrs else txt_y + (blk_thrs - txt_y) ** blk_clmp
    bg_y = bg_y if bg_y > blk_thrs else bg_y + (blk_thrs - bg_y) ** blk_clmp

    if abs(bg_y - txt_y) < delta_y_min:
        return 0.0

    if bg_y > txt_y:
        sapc = ((bg_y ** norm_bg) - (txt_y ** norm_txt)) * scale
        output = 0.0 if sapc < lo_clip else sapc - lo_offset
    else:
        sapc = ((bg_y ** rev_bg) - (txt_y ** rev_txt)) * scale
        output = 0.0 if sapc > -lo_clip else sapc + lo_offset

    return output * 100.0

# --- Color Blindness Simulation ---
_COLORBLIND_MATRICES = {
    "Protanopia": ((0.567, 0.433, 0.000), (0.558, 0.442, 0.000), (0.000, 0.242, 0.758)),
    "Deuteranopia": ((0.625, 0.375, 0.000), (0.700, 0.300, 0.000), (0.000, 0.300, 0.700)),
    "Tritanopia": ((0.950, 0.050, 0.000), (0.000, 0.433, 0.567), (0.000, 0.475, 0.525)),
}

def simulate_colorblindness(hex_color, mode):
    if not mode or mode == "None":
        return hex_color

    if mode == "Achromatopsia":
        gray = round(relative_luminance(hex_color) * 255)
        return rgb_to_hex((gray, gray, gray))

    matrix = _COLORBLIND_MATRICES.get(mode)
    if matrix is None:
        return hex_color

    r, g, b = hex_to_rgb_tuple(hex_color)
    lr, lg, lb = (_srgb_to_linear(c) for c in (r, g, b))
    return rgb_to_hex(tuple(_linear_to_srgb(row[0] * lr + row[1] * lg + row[2] * lb) for row in matrix))

# Same matrices as above, flattened into Pillow's Image.convert("RGB", matrix)
# 12-tuple form (3 rows of R,G,B,offset) so a whole rendered image can be
# filtered natively in C instead of per-pixel in Python.
_COLORBLIND_PIL_MATRICES = {
    mode: tuple(v for row in matrix for v in (*row, 0))
    for mode, matrix in _COLORBLIND_MATRICES.items()
}

def apply_colorblind_filter(image, mode):
    """Fast, whole-image approximation of simulate_colorblindness for live
    previews (e.g. a rendered gradient) where per-pixel Python looping with
    simulate_colorblindness's linear-RGB round-trip would be too slow to
    keep up with interactive dragging (angle dial, easing curve, weight
    slider). Applies the same matrices directly to sRGB pixel values via
    Pillow's native convert() instead - a standard approximation (skipping
    the linearize/re-encode round trip) that stays visually equivalent and
    renders fast enough for smooth live updates."""
    if not _IMAGING_AVAILABLE or not mode or mode == "None":
        return image

    if mode == "Achromatopsia":
        return image.convert("L", (0.299, 0.587, 0.114, 0)).convert("RGB")

    matrix = _COLORBLIND_PIL_MATRICES.get(mode)
    if matrix is None:
        return image

    return image.convert("RGB", matrix)

# --- OKLCH Perceptual Color Engine ---
_OKLAB_M1 = (
    (0.4122214708, 0.5363325363, 0.0514459929),
    (0.2119034982, 0.6806995451, 0.1073969566),
    (0.0883024619, 0.2817188376, 0.6299787005),
)
_OKLAB_M2 = (
    (0.2104542553, 0.7936177850, -0.0040720468),
    (1.9779984951, -2.4285922050, 0.4505937099),
    (0.0259040371, 0.7827717662, -0.8086757660),
)
_OKLAB_M1_INV = (
    (4.0767416621, -3.3077115913, 0.2309699292),
    (-1.2684380046, 2.6097574011, -0.3413193965),
    (-0.0041960863, -0.7034186147, 1.7076147010),
)
_OKLAB_M2_INV = (
    (1.0, 0.3963377774, 0.2158037573),
    (1.0, -0.1055613458, -0.0638541728),
    (1.0, -0.0894841775, -1.2914855480),
)

def _cbrt(x):
    return math.copysign(abs(x) ** (1 / 3), x)

def hex_to_oklab(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    lr, lg, lb = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    l = _OKLAB_M1[0][0] * lr + _OKLAB_M1[0][1] * lg + _OKLAB_M1[0][2] * lb
    m = _OKLAB_M1[1][0] * lr + _OKLAB_M1[1][1] * lg + _OKLAB_M1[1][2] * lb
    s = _OKLAB_M1[2][0] * lr + _OKLAB_M1[2][1] * lg + _OKLAB_M1[2][2] * lb
    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
    L = _OKLAB_M2[0][0] * l_ + _OKLAB_M2[0][1] * m_ + _OKLAB_M2[0][2] * s_
    a = _OKLAB_M2[1][0] * l_ + _OKLAB_M2[1][1] * m_ + _OKLAB_M2[1][2] * s_
    b2 = _OKLAB_M2[2][0] * l_ + _OKLAB_M2[2][1] * m_ + _OKLAB_M2[2][2] * s_
    return (L, a, b2)

def hex_to_oklch(hex_color):
    L, a, b2 = hex_to_oklab(hex_color)
    C = math.hypot(a, b2)
    H = math.degrees(math.atan2(b2, a)) % 360.0
    return (L, C, H)

def _oklch_to_linear_rgb(L, C, H):
    a = C * math.cos(math.radians(H))
    b2 = C * math.sin(math.radians(H))
    l_ = _OKLAB_M2_INV[0][0] * L + _OKLAB_M2_INV[0][1] * a + _OKLAB_M2_INV[0][2] * b2
    m_ = _OKLAB_M2_INV[1][0] * L + _OKLAB_M2_INV[1][1] * a + _OKLAB_M2_INV[1][2] * b2
    s_ = _OKLAB_M2_INV[2][0] * L + _OKLAB_M2_INV[2][1] * a + _OKLAB_M2_INV[2][2] * b2
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = _OKLAB_M1_INV[0][0] * l + _OKLAB_M1_INV[0][1] * m + _OKLAB_M1_INV[0][2] * s
    g = _OKLAB_M1_INV[1][0] * l + _OKLAB_M1_INV[1][1] * m + _OKLAB_M1_INV[1][2] * s
    b3 = _OKLAB_M1_INV[2][0] * l + _OKLAB_M1_INV[2][1] * m + _OKLAB_M1_INV[2][2] * s
    return (r, g, b3)

def oklch_to_rgb01(L, C, H):
    lo, hi = 0.0, max(0.0, C)
    rgb = _oklch_to_linear_rgb(L, hi, H)
    if not all(-1e-6 <= c <= 1 + 1e-6 for c in rgb):
        for _ in range(20):
            mid = (lo + hi) / 2
            rgb = _oklch_to_linear_rgb(L, mid, H)
            if all(-1e-6 <= c <= 1 + 1e-6 for c in rgb):
                lo = mid
            else:
                hi = mid
        rgb = _oklch_to_linear_rgb(L, lo, H)
    r, g, b = rgb
    return _linear_to_srgb_f(r), _linear_to_srgb_f(g), _linear_to_srgb_f(b)

def oklch_to_hex(L, C, H):
    r, g, b = oklch_to_rgb01(L, C, H)
    return rgb_to_hex((round(r * 255), round(g * 255), round(b * 255)))

def _linspace(a, b, n):
    if n <= 1:
        return [b]
    step = (b - a) / (n - 1)
    return [a + step * i for i in range(n)]

# --- RYB Wheel Mapping ---
RGB_TO_RYB_MAP = [
    (0, 0), (35, 60), (60, 120), (120, 180),
    (180, 216), (240, 240), (300, 300), (360, 360)
]

def interpolate_angle(angle, map_table):
    angle = angle % 360.0
    for i in range(len(map_table) - 1):
        x1, y1 = map_table[i]
        x2, y2 = map_table[i + 1]
        if x1 <= angle <= x2:
            t = (angle - x1) / (x2 - x1) if x2 != x1 else 0
            return (y1 + t * (y2 - y1)) % 360.0
    return angle

def rgb_hue_to_ryb(hue_deg):
    return interpolate_angle(hue_deg, RGB_TO_RYB_MAP)

def ryb_hue_to_rgb(ryb_deg):
    reverse_map = [(y, x) for x, y in RGB_TO_RYB_MAP]
    reverse_map.sort(key=lambda item: item[0])
    return interpolate_angle(ryb_deg, reverse_map)

def hex_to_hsv(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)

def hsv_to_hex(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return rgb_to_hex((r * 255, g * 255, b * 255))

def hex_to_hsl(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    return h, s, l

def hsl_to_hex(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return rgb_to_hex((r * 255, g * 255, b * 255))

# --- CIE Lab (D65) ---
_LAB_XN, _LAB_YN, _LAB_ZN = 0.95047, 1.0, 1.08883

def _lab_f(t):
    return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

def _lab_finv(t):
    t3 = t ** 3
    return t3 if t3 > 0.008856 else (t - 16 / 116) / 7.787

def hex_to_lab(hex_color):
    r, g, b = hex_to_rgb_tuple(hex_color)
    lr, lg, lb = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    x = lr * 0.4124564 + lg * 0.3575761 + lb * 0.1804375
    y = lr * 0.2126729 + lg * 0.7151522 + lb * 0.0721750
    z = lr * 0.0193339 + lg * 0.1191920 + lb * 0.9503041
    fx, fy, fz = _lab_f(x / _LAB_XN), _lab_f(y / _LAB_YN), _lab_f(z / _LAB_ZN)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    b2 = 200 * (fy - fz)
    return L, a, b2

def lab_to_rgb01(L, a, b2):
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b2 / 200
    x = _LAB_XN * _lab_finv(fx)
    y = _LAB_YN * _lab_finv(fy)
    z = _LAB_ZN * _lab_finv(fz)
    lr = x * 3.2404542 + y * -1.5371385 + z * -0.4985314
    lg = x * -0.9692660 + y * 1.8760108 + z * 0.0415560
    lb = x * 0.0556434 + y * -0.2040259 + z * 1.0572252
    return _linear_to_srgb_f(lr), _linear_to_srgb_f(lg), _linear_to_srgb_f(lb)

def lab_to_hex(L, a, b2):
    r, g, b = lab_to_rgb01(L, a, b2)
    return rgb_to_hex((round(r * 255), round(g * 255), round(b * 255)))

def hex_to_lch(hex_color):
    L, a, b2 = hex_to_lab(hex_color)
    C = math.hypot(a, b2)
    H = math.degrees(math.atan2(b2, a)) % 360.0
    return L, C, H

def lch_to_rgb01(L, C, H):
    a = C * math.cos(math.radians(H))
    b2 = C * math.sin(math.radians(H))
    return lab_to_rgb01(L, a, b2)

def lch_to_hex(L, C, H):
    a = C * math.cos(math.radians(H))
    b2 = C * math.sin(math.radians(H))
    return lab_to_hex(L, a, b2)

# --- Cubic-bezier easing (CSS-style, endpoints fixed at (0,0)/(1,1)) ---
def _cubic_bezier_component(t, p1, p2):
    mt = 1.0 - t
    return 3 * mt * mt * t * p1 + 3 * mt * t * t * p2 + t * t * t

def cubic_bezier_ease(x1, y1, x2, y2, x):
    x = max(0.0, min(1.0, x))
    lo, hi = 0.0, 1.0
    t = x
    for _ in range(24):
        t = (lo + hi) / 2
        cur_x = _cubic_bezier_component(t, x1, x2)
        if cur_x < x:
            lo = t
        else:
            hi = t
    return _cubic_bezier_component(t, y1, y2)

def generate_color_harmony(base_hex, rule, wheel="RYB", count=5):
    count = max(1, count)
    wheel = str(wheel).upper()

    if wheel == "OKLCH":
        L0, C0, H0 = hex_to_oklch(base_hex)
        def make_color(wheel_offset, sat_mult=1.0, val_mult=1.0):
            target_hue = (H0 + wheel_offset) % 360.0
            return oklch_to_hex(max(0.0, min(1.0, L0 * val_mult)), max(0.0, C0 * sat_mult), target_hue)
    else:
        h, s, v = hex_to_hsv(base_hex)
        rgb_hue_deg = h * 360.0
        use_ryb = wheel == "RYB"
        wheel_hue_deg = rgb_hue_to_ryb(rgb_hue_deg) if use_ryb else rgb_hue_deg

        def make_color(wheel_offset, sat_mult=1.0, val_mult=1.0):
            target_wheel = (wheel_hue_deg + wheel_offset) % 360.0
            target_rgb_hue = (ryb_hue_to_rgb(target_wheel) if use_ryb else target_wheel) / 360.0
            return hsv_to_hex(target_rgb_hue, s * sat_mult, v * val_mult)

    if rule == "Analogous":
        offsets = _linspace(-30.0, 30.0, count)
        return [make_color(o) for o in offsets]
    elif rule == "Monochromatic":
        base_level = L0 if wheel == "OKLCH" else v
        high_mult = min(1.4, 1.0 / base_level) if base_level > 0 else 1.4
        val_mults = _linspace(0.35, high_mult, count)
        return [make_color(0.0, val_mult=vm) for vm in val_mults]
    elif rule == "Triad":
        offsets = [i * 360.0 / count for i in range(count)]
        return [make_color(o) for o in offsets]
    elif rule == "Complementary":
        base_n = (count + 1) // 2
        comp_n = count - base_n
        base_vals = _linspace(0.5, 1.15, base_n) if base_n > 1 else [1.0]
        comp_vals = _linspace(0.6, 1.15, comp_n) if comp_n > 1 else ([1.0] if comp_n == 1 else [])
        return ([make_color(0.0, val_mult=vm) for vm in base_vals]
                + [make_color(180.0, val_mult=vm) for vm in comp_vals])
    elif rule == "Split complementary":
        anchors = [0.0, 150.0, 210.0]
        result = []
        for i in range(count):
            lap = i // 3
            val_mult = 1.0 if lap == 0 else max(0.5, 1.0 - lap * 0.2)
            sat_mult = 1.0 if lap == 0 else 0.7
            result.append(make_color(anchors[i % 3], sat_mult=sat_mult, val_mult=val_mult))
        return result
    elif rule == "Shades":
        val_mults = _linspace(0.2, 1.0, count)
        return [make_color(0.0, val_mult=vm) for vm in val_mults]

    return [base_hex] * count

# --- Light/Dark Theme Variant ---
def generate_theme_variant(colors, to_dark):
    """Lighter/darker versions of each input color, same hue - moves each
    color's own lightness most of the way toward a near-black or near-white
    target (never flips which color ends up lighter than which; a color
    that started darker than another stays darker than it after either
    button, just shifted as a group toward that end)."""
    target_L = 0.14 if to_dark else 0.95
    result = []
    for hex_color in colors:
        L, C, H = hex_to_oklch(hex_color)
        L_new = L + (target_L - L) * 0.72
        C_new = C * 0.9
        result.append(oklch_to_hex(L_new, max(0.0, C_new), H))
    return result

# --- Grayscale / Neutral Variant ---
def generate_grayscale_neutrals(base_hex, count):
    """Maps a single base color to `count` neutral shades spanning dark to
    light. Keeps a small fixed chroma tinted toward the base's own hue
    rather than going fully achromatic, so the ramp reads as "neutrals of
    that color" rather than a generic gray scale."""
    _, _, H = hex_to_oklch(base_hex)
    L_vals = _linspace(0.10, 0.94, max(1, count))
    return [oklch_to_hex(L, 0.02, H) for L in L_vals]

# --- Neon / Pastel Mood Variant ---
def generate_neon(colors):
    """Reinterprets each input color at max in-gamut chroma for its hue -
    requesting far more chroma than sRGB can hold lets oklch_to_hex's own
    gamut clamp (in oklch_to_rgb01) settle on the most vivid in-gamut
    color for that hue automatically, which is exactly "neon"."""
    result = []
    for hex_color in colors:
        _, _, H = hex_to_oklch(hex_color)
        result.append(oklch_to_hex(0.72, 0.45, H))
    return result

def generate_pastel(colors):
    """Reinterprets each input color as a soft, low-chroma, high-lightness
    tint of the same hue."""
    result = []
    for hex_color in colors:
        _, _, H = hex_to_oklch(hex_color)
        result.append(oklch_to_hex(0.90, 0.07, H))
    return result

# --- Glass / Frost Variant ---
def generate_glass_colors(colors):
    """Derives the three colors a glassmorphism UI effect needs directly
    from the actual input colors, rather than blending all of them into
    one averaged hue - averaging tends toward gray/brown whenever the
    inputs sit on opposite sides of the color wheel (e.g. orange + blue),
    which reads as an unrelated muddy color instead of something tied to
    what was actually put in. Instead: the primary tint's hue/chroma come
    from whichever input is most saturated, the highlight's hue comes from
    whichever input is lightest, and the shadow/border's hue comes from
    whichever input is darkest - each output stays recognizably connected
    to one of the colors the user actually chose."""
    if not colors:
        return {}
    triples = [hex_to_oklch(c) for c in colors]  # (L, C, H) per input

    avg_L = sum(L for L, _, _ in triples) / len(triples)
    _, primary_C, primary_H = max(triples, key=lambda t: t[1])
    _, light_C, light_H = max(triples, key=lambda t: t[0])
    _, dark_C, dark_H = min(triples, key=lambda t: t[0])

    primary = oklch_to_hex(max(0.38, min(0.62, avg_L)), min(primary_C * 1.05, 0.18), primary_H)
    highlight = oklch_to_hex(0.95, min(max(light_C, 0.02) * 0.5, 0.06), light_H)
    shadow = oklch_to_hex(0.18, min(max(dark_C, 0.03) * 0.9, 0.12), dark_H)
    return {"primary": primary, "highlight": highlight, "shadow": shadow}

# --- Gradient Sampling & Image Rendering ---
def _gradient_segment_at_pos(stops, pos_pct):
    """Locate the (p1, hex1, p2, hex2, u) segment straddling pos_pct, or None
    if pos_pct falls exactly on (or beyond) an end stop."""
    sorted_stops = sorted(stops, key=lambda s: s[0])
    pos_pct = max(0.0, min(100.0, pos_pct))
    if pos_pct <= sorted_stops[0][0]:
        return None, sorted_stops[0][1].upper()
    if pos_pct >= sorted_stops[-1][0]:
        return None, sorted_stops[-1][1].upper()

    for i in range(len(sorted_stops) - 1):
        p1, hex1 = sorted_stops[i]
        p2, hex2 = sorted_stops[i + 1]
        if p1 <= pos_pct <= p2:
            u = (pos_pct - p1) / (p2 - p1) if p2 != p1 else 0.0
            return (hex1, hex2, u), None
    return None, sorted_stops[-1][1].upper()

def sample_gradient_rgb01_at_pos(stops, pos_pct, color_space="OKLCH"):
    """Like sample_gradient_at_pos, but returns an unrounded (r, g, b) float
    triple in 0..1 - used for high-precision (16-bit) export, where rounding
    to an 8-bit hex string at each sample would quantize the gradient."""
    if not stops:
        return (0.0, 0.0, 0.0)
    segment, fallback_hex = _gradient_segment_at_pos(stops, pos_pct)
    if segment is None:
        r, g, b = hex_to_rgb_tuple(fallback_hex)
        return r / 255.0, g / 255.0, b / 255.0

    hex1, hex2, u = segment
    space = str(color_space).upper()
    if space == "OKLCH":
        L1, C1, H1 = hex_to_oklch(hex1)
        L2, C2, H2 = hex_to_oklch(hex2)
        dh = (H2 - H1) % 360.0
        if dh > 180.0:
            dh -= 360.0
        H_interp = (H1 + u * dh) % 360.0
        L_interp = max(0.0, min(1.0, L1 + u * (L2 - L1)))
        C_interp = max(0.0, C1 + u * (C2 - C1))
        return oklch_to_rgb01(L_interp, C_interp, H_interp)
    elif space == "HSV":
        h1, s1, v1 = hex_to_hsv(hex1)
        h2, s2, v2 = hex_to_hsv(hex2)
        dh = (h2 - h1) % 1.0
        if dh > 0.5:
            dh -= 1.0
        h_interp = (h1 + u * dh) % 1.0
        s_interp = max(0.0, min(1.0, s1 + u * (s2 - s1)))
        v_interp = max(0.0, min(1.0, v1 + u * (v2 - v1)))
        return colorsys.hsv_to_rgb(h_interp, s_interp, v_interp)
    elif space == "HSL":
        h1, s1, l1 = hex_to_hsl(hex1)
        h2, s2, l2 = hex_to_hsl(hex2)
        dh = (h2 - h1) % 1.0
        if dh > 0.5:
            dh -= 1.0
        h_interp = (h1 + u * dh) % 1.0
        s_interp = max(0.0, min(1.0, s1 + u * (s2 - s1)))
        l_interp = max(0.0, min(1.0, l1 + u * (l2 - l1)))
        return colorsys.hls_to_rgb(h_interp, l_interp, s_interp)
    elif space == "HCL":
        L1, C1, H1 = hex_to_lch(hex1)
        L2, C2, H2 = hex_to_lch(hex2)
        dh = (H2 - H1) % 360.0
        if dh > 180.0:
            dh -= 360.0
        H_interp = (H1 + u * dh) % 360.0
        L_interp = L1 + u * (L2 - L1)
        C_interp = max(0.0, C1 + u * (C2 - C1))
        return lch_to_rgb01(L_interp, C_interp, H_interp)
    elif space == "LAB":
        L1, a1, b1v = hex_to_lab(hex1)
        L2, a2, b2v = hex_to_lab(hex2)
        return lab_to_rgb01(
            L1 + u * (L2 - L1), a1 + u * (a2 - a1), b1v + u * (b2v - b1v)
        )
    elif space == "LRGB":
        r1, g1, b1 = hex_to_rgb_tuple(hex1)
        r2, g2, b2 = hex_to_rgb_tuple(hex2)
        lr1, lg1, lb1 = _srgb_to_linear(r1), _srgb_to_linear(g1), _srgb_to_linear(b1)
        lr2, lg2, lb2 = _srgb_to_linear(r2), _srgb_to_linear(g2), _srgb_to_linear(b2)
        return (
            _linear_to_srgb_f(lr1 + u * (lr2 - lr1)),
            _linear_to_srgb_f(lg1 + u * (lg2 - lg1)),
            _linear_to_srgb_f(lb1 + u * (lb2 - lb1)),
        )
    else:
        r1, g1, b1 = hex_to_rgb_tuple(hex1)
        r2, g2, b2 = hex_to_rgb_tuple(hex2)
        return (
            (r1 + u * (r2 - r1)) / 255.0,
            (g1 + u * (g2 - g1)) / 255.0,
            (b1 + u * (b2 - b1)) / 255.0,
        )

def sample_gradient_at_pos(stops, pos_pct, color_space="OKLCH"):
    if not stops:
        return "#000000"
    r, g, b = sample_gradient_rgb01_at_pos(stops, pos_pct, color_space)
    return rgb_to_hex((round(r * 255), round(g * 255), round(b * 255))).upper()

def _apply_easing(u, easing):
    if not easing:
        return u
    x1, y1, x2, y2 = easing
    return max(0.0, min(1.0, cubic_bezier_ease(x1, y1, x2, y2, u)))

def render_gradient_pil_image(stops, style="Linear", angle=90, center_x=0.5, center_y=0.5, radius=0.5, color_space="OKLCH", width=480, height=220, easing=None):
    if not _IMAGING_AVAILABLE or not stops:
        return Image.new("RGB", (width, height), (30, 30, 30))

    img = Image.new("RGB", (width, height))
    style = str(style).capitalize()

    lut = [
        hex_to_rgb_tuple(
            sample_gradient_at_pos(stops, _apply_easing(i / 255.0, easing) * 100.0, color_space)
        )
        for i in range(256)
    ]

    pixels = []
    if style == "Linear":
        rad = math.radians(angle)
        cosa, sina = math.cos(rad), math.sin(rad)
        for y in range(height):
            dy = (y / (height - 1) - 0.5) if height > 1 else 0.0
            for x in range(width):
                dx = (x / (width - 1) - 0.5) if width > 1 else 0.0
                proj = dx * cosa + dy * sina + 0.5
                idx = max(0, min(255, round(proj * 255)))
                pixels.append(lut[idx])
    else:
        cx = center_x * width
        cy = center_y * height
        max_dist = max(1.0, radius * math.hypot(width, height))
        for y in range(height):
            dy = y - cy
            for x in range(width):
                dx = x - cx
                dist = math.hypot(dx, dy)
                proj = min(1.0, dist / max_dist)
                idx = max(0, min(255, round(proj * 255)))
                pixels.append(lut[idx])

    img.putdata(pixels)
    return img

def _to_u16(c):
    return max(0, min(65535, round(c * 65535)))

def _write_png_16bit(path, width, height, rows_rgb16_bytes):
    """Write a raw truecolor (color type 2) 16-bit-per-channel PNG using only
    the stdlib (struct + zlib) - Pillow has no mode for 16-bit-per-channel RGB.
    rows_rgb16_bytes: `height` bytes objects, each width*6 bytes (R,G,B as
    big-endian uint16 per pixel, no filter byte)."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 16, 2, 0, 0, 0)
    raw = bytearray()
    for row in rows_rgb16_bytes:
        raw.append(0)  # filter type: None
        raw.extend(row)
    compressed = zlib.compress(bytes(raw), 9)

    with open(path, "wb") as f:
        f.write(sig)
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", compressed))
        f.write(chunk(b"IEND", b""))

def export_gradient_png(path, stops, style="Linear", angle=90, center_x=0.5, center_y=0.5,
                         radius=0.5, color_space="OKLCH", width=1600, height=900,
                         easing=None, bit_depth=8):
    """Render the gradient at the requested resolution/bit depth and save as PNG.
    8-bit reuses the standard 256-entry LUT renderer (matches the live preview).
    16-bit builds a per-pixel float LUT sized to the output resolution - sampling
    the gradient math directly instead of quantizing through an 8-bit hex string
    at each step - and writes a true 16-bit-per-channel PNG, avoiding banding."""
    if bit_depth != 16:
        img = render_gradient_pil_image(
            stops, style=style, angle=angle, center_x=center_x, center_y=center_y,
            radius=radius, color_space=color_space, width=width, height=height, easing=easing
        )
        img.save(path, "PNG")
        return

    lut_size = max(width, height, 256)
    lut = [
        sample_gradient_rgb01_at_pos(stops, _apply_easing(i / (lut_size - 1), easing) * 100.0, color_space)
        for i in range(lut_size)
    ]

    style_norm = str(style).capitalize()
    rows = []
    if style_norm == "Linear":
        rad = math.radians(angle)
        cosa, sina = math.cos(rad), math.sin(rad)
        for y in range(height):
            dy = (y / (height - 1) - 0.5) if height > 1 else 0.0
            row = bytearray()
            for x in range(width):
                dx = (x / (width - 1) - 0.5) if width > 1 else 0.0
                proj = dx * cosa + dy * sina + 0.5
                idx = max(0, min(lut_size - 1, round(proj * (lut_size - 1))))
                r, g, b = lut[idx]
                row += struct.pack(">HHH", _to_u16(r), _to_u16(g), _to_u16(b))
            rows.append(bytes(row))
    else:
        cx = center_x * width
        cy = center_y * height
        max_dist = max(1.0, radius * math.hypot(width, height))
        for y in range(height):
            dy = y - cy
            row = bytearray()
            for x in range(width):
                dx = x - cx
                dist = math.hypot(dx, dy)
                proj = min(1.0, dist / max_dist)
                idx = max(0, min(lut_size - 1, round(proj * (lut_size - 1))))
                r, g, b = lut[idx]
                row += struct.pack(">HHH", _to_u16(r), _to_u16(g), _to_u16(b))
            rows.append(bytes(row))

    _write_png_16bit(path, width, height, rows)

# --- Treemap Squarify Engine ---
# Fixed "reference shape" treemap template: always exactly 8 slots, in a
# fixed spatial arrangement that never changes regardless of the palette's
# actual weights or the canvas's aspect ratio. Slot order (left-to-right,
# top-to-bottom) matches the order colors are assigned in:
#   0: main block (far left, full height) - 40% of total area
#   1-3: three equal quarter-of-main blocks across the top-right, in a row
#   4: half-of-main block, directly under slots 1+2
#   5: eighth-of-main block, under slot 3 (left half of that column)
#   6-7: two sixteenth-of-main blocks, stacked, under slot 3 (right half)
FIXED_SHAPE_WEIGHTS = [0.4, 0.1, 0.1, 0.1, 0.2, 0.05, 0.025, 0.025]

def compute_fixed_shape_rects(width, height):
    main_w = 0.4 * width
    top_block_w = (width - main_w) / 3.0
    top_row_h = 0.5 * height
    bottom_row_h = height - top_row_h
    left_sub_w = top_block_w / 2.0
    right_sub_w = top_block_w - left_sub_w
    stack_h = bottom_row_h / 2.0

    col3_x = main_w + 2 * top_block_w
    return [
        (0, 0, main_w, height),                                          # 0: main
        (main_w, 0, top_block_w, top_row_h),                             # 1: top-right block 1
        (main_w + top_block_w, 0, top_block_w, top_row_h),                # 2: top-right block 2
        (col3_x, 0, top_block_w, top_row_h),                              # 3: top-right block 3
        (main_w, top_row_h, 2 * top_block_w, bottom_row_h),               # 4: half, under 1+2
        (col3_x, top_row_h, left_sub_w, bottom_row_h),                    # 5: eighth, under 3 (left)
        (col3_x + left_sub_w, top_row_h, right_sub_w, stack_h),           # 6: sixteenth, upper
        (col3_x + left_sub_w, top_row_h + stack_h, right_sub_w, stack_h), # 7: sixteenth, lower
    ]

def _snap_edges_to_pixels(values, eps=1e-4):
    order = sorted(range(len(values)), key=lambda i: values[i])
    snapped = [None] * len(values)
    cluster = [order[0]]
    for idx in order[1:]:
        if values[idx] - values[cluster[-1]] <= eps:
            cluster.append(idx)
        else:
            canonical = round(sum(values[i] for i in cluster) / len(cluster))
            for i in cluster:
                snapped[i] = canonical
            cluster = [idx]
    canonical = round(sum(values[i] for i in cluster) / len(cluster))
    for i in cluster:
        snapped[i] = canonical
    return snapped

def snap_rects_to_pixel_grid(rects, eps=1e-4):
    if not rects:
        return []
    xs = [r[0] for r in rects] + [r[0] + r[2] for r in rects]
    ys = [r[1] for r in rects] + [r[1] + r[3] for r in rects]
    snapped_xs = _snap_edges_to_pixels(xs)
    snapped_ys = _snap_edges_to_pixels(ys)
    n = len(rects)
    return [(snapped_xs[i], snapped_ys[i], snapped_xs[i + n] - snapped_xs[i], snapped_ys[i + n] - snapped_ys[i])
            for i in range(n)]

def render_fixed_shape_visualization_image(colors, vision_mode="None",
                                            show_hex=True, show_pct=True, width=760, height=480):
    """
    Renders the 8-slot fixed reference-shape treemap (see FIXED_SHAPE_WEIGHTS
    / compute_fixed_shape_rects above). `colors` must have exactly 8 hex
    entries, in slot order.
    """
    if not _IMAGING_AVAILABLE:
        raise RuntimeError("PIL/Pillow imaging library is required for image rendering.")

    rects = compute_fixed_shape_rects(width, height)
    pixel_rects = snap_rects_to_pixel_grid(rects)

    img = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("segoeui.ttf", 14)
        font_bold = ImageFont.truetype("segoeuib.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
        font_bold = font

    for (hex_code, weight), (ix0, iy0, iw, ih) in zip(zip(colors, FIXED_SHAPE_WEIGHTS), pixel_rects):
        shown_hex = simulate_colorblindness(hex_code, vision_mode)
        rgb_color = hex_to_rgb_tuple(shown_hex)

        # Fill rectangle block
        draw.rectangle([ix0, iy0, ix0 + iw, iy0 + ih], fill=rgb_color)

        # Subtle clean outline border for clean grid delineation
        draw.rectangle([ix0, iy0, ix0 + iw - 1, iy0 + ih - 1], outline=(0, 0, 0, 25), width=1)

        min_dim = min(iw, ih)
        label = None
        hex_line = hex_code.upper()
        pct_line = f"{weight * 100:.0f}%"
        if show_hex and show_pct:
            if min_dim >= 40:
                label = f"{hex_line}\n{pct_line}"
            elif min_dim >= 14:
                label = pct_line
        elif show_hex:
            if min_dim >= 14:
                label = hex_line
        elif show_pct:
            if min_dim >= 14:
                label = pct_line

        if label:
            text_color_hex = contrasting_text_color(shown_hex)
            text_rgb = hex_to_rgb_tuple(text_color_hex)
            active_font = font_bold if (iw >= 80 and ih >= 50) else font
            bbox = draw.multiline_textbbox((0, 0), label, font=active_font, align="center")
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            tx = ix0 + (iw - text_w) / 2 - bbox[0]
            ty = iy0 + (ih - text_h) / 2 - bbox[1]
            draw.multiline_text((tx, ty), label, fill=text_rgb, font=active_font, align="center")

    return img

# --- Dominant Color Extraction ---
def _spread_luminance(img, amount):
    if amount <= 0:
        return img
    img = ImageEnhance.Color(img).enhance(1.0 + amount * 5.0)
    y, cb, cr = img.convert("YCbCr").split()
    y_eq = ImageOps.equalize(y)
    y_blended = y_eq if amount >= 1 else Image.blend(y, y_eq, amount)
    return Image.merge("YCbCr", (y_blended, cb, cr)).convert("RGB")

def _extract_balanced(img, num_colors):
    num_colors = max(1, num_colors)
    pool_size = min(64, max(24, num_colors * 6))

    quantized = img.quantize(colors=pool_size)
    palette = quantized.getpalette() or []
    counts = quantized.getcolors() or []
    counts.sort(key=lambda entry: entry[0], reverse=True)

    candidates = []
    for _count, palette_index in counts:
        offset = palette_index * 3
        rgb = tuple(palette[offset:offset + 3])
        if len(rgb) == 3:
            hex_color = rgb_to_hex(rgb)
            candidates.append((hex_color, hex_to_oklab(hex_color)))

    if not candidates:
        return []

    selected = [candidates.pop(0)]
    while candidates and len(selected) < num_colors:
        def min_distance_to_selected(candidate):
            lab = candidate[1]
            return min(math.dist(lab, s[1]) for s in selected)
        best = max(candidates, key=min_distance_to_selected)
        candidates.remove(best)
        selected.append(best)

    return [hex_color for hex_color, _lab in selected]

EXTRACT_WORK_SIZE = (200, 200)

def extraction_image(image_path):
    """The small RGB copy extraction works on - open the file once and
    re-extract from this as the sliders move."""
    with Image.open(image_path) as src:
        img = src.convert("RGB")
    img.thumbnail(EXTRACT_WORK_SIZE)
    return img

def extract_dominant_colors(image_path, num_colors, luminance_spread=0.0, weighing="Average"):
    if not _IMAGING_AVAILABLE:
        return []
    return extract_from_image(extraction_image(image_path), num_colors, luminance_spread, weighing)

def extract_from_image(img, num_colors, luminance_spread=0.0, weighing="Average"):
    """extract_dominant_colors on an image from extraction_image()."""
    img = _spread_luminance(img, luminance_spread)

    if str(weighing).lower() == "balanced":
        return _extract_balanced(img, num_colors)

    quantized = img.quantize(colors=max(1, num_colors))
    palette = quantized.getpalette() or []
    counts = quantized.getcolors() or []
    counts.sort(key=lambda entry: entry[0], reverse=True)

    hexes = []
    for _count, palette_index in counts[:num_colors]:
        offset = palette_index * 3
        rgb = tuple(palette[offset:offset + 3])
        if len(rgb) == 3:
            hexes.append(rgb_to_hex(rgb))
    return hexes

# --- ARRI LogC False Color ---
FALSE_COLOR_ZONES = (
    ("Purple", 0.0, 2.5, (179, 0, 255), "IRE 0-2.5"),
    ("Blue", 2.5, 10.0, (0, 0, 255), "IRE 2.5-10"),
    ("Grey", 40.0, 42.0, (128, 128, 128), "IRE 40-42"),
    ("Green", 42.0, 50.0, (0, 255, 0), "IRE 42-50"),
    ("Pink", 50.0, 70.0, (255, 179, 179), "IRE 50-70"),
    ("Yellow", 80.0, 95.0, (255, 255, 0), "IRE 80-95"),
    ("Red", 95.0, 100.0001, (255, 0, 0), "IRE 95-100+"),
)

FALSE_COLOR_INFO = {
    "Purple": "Signal matches the noise floor limit (severe underexposure/shadow noise).",
    "Blue": "Crushed blacks - detail is lost in the shadows and the image will be noisy here.",
    "Grey": "Middle grey baseline - use this zone to expose a grey card for a neutral exposure.",
    "Green": "Darker skin tones / 18% grey target zone.",
    "Pink": "Lighter skin tones target zone.",
    "Yellow": "Near clipping - caution zone for bright light sources.",
    "Red": "Clipping (white) - highlight detail is lost and blown out.",
}

def _ire_to_8bit(ire):
    return min(255, max(0, round(ire / 100.0 * 255)))

def _build_false_color_lut():
    lut = [(v, v, v) for v in range(256)]
    for _name, ire_lower, ire_upper, color, _label in FALSE_COLOR_ZONES:
        lower = _ire_to_8bit(ire_lower)
        upper = 256 if ire_upper >= 100.0 else _ire_to_8bit(ire_upper)
        for v in range(lower, upper):
            lut[v] = color
    return lut

_FALSE_COLOR_LUT = _build_false_color_lut()

def apply_false_color(image):
    if not _IMAGING_AVAILABLE:
        return image
    luma = image.convert("L", (0.2126, 0.7152, 0.0722, 0))
    r_lut = [c[0] for c in _FALSE_COLOR_LUT]
    g_lut = [c[1] for c in _FALSE_COLOR_LUT]
    b_lut = [c[2] for c in _FALSE_COLOR_LUT]
    return Image.merge("RGB", (luma.point(r_lut), luma.point(g_lut), luma.point(b_lut)))

def false_color_legend_strip(width, height):
    if not _IMAGING_AVAILABLE:
        return None
    strip = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(strip)
    n = len(FALSE_COLOR_ZONES)
    edges = _linspace(0, width, n + 1)
    gap = max(1, round(width / 400))
    for i, (_name, _ire_lower, _ire_upper, color, _label) in enumerate(FALSE_COLOR_ZONES):
        x0 = edges[i] + (gap / 2 if i > 0 else 0)
        x1 = edges[i + 1] - (gap / 2 if i < n - 1 else 0)
        draw.rectangle([x0, 0, x1, height], fill=color)
    return strip

def false_color_ire_strip(width, height):
    if not _IMAGING_AVAILABLE:
        return None
    strip = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(strip)
    n = len(FALSE_COLOR_ZONES)
    edges = _linspace(0, width, n + 1)
    try:
        font = ImageFont.truetype("segoeui.ttf", max(11, round(height * 0.45)))
    except Exception:
        font = ImageFont.load_default()
    for i, (_name, _ire_lower, _ire_upper, _color, label) in enumerate(FALSE_COLOR_ZONES):
        col_center = (edges[i] + edges[i + 1]) / 2
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        draw.text((col_center - text_w / 2, height / 2 - text_h / 2), label, fill=(30, 30, 30), font=font)
    return strip

# --- Export Format Generators ---
def _export_slug(name):
    slug = "".join(c if c.isalnum() else "-" for c in name.strip().lower())
    slug = "-".join(filter(None, slug.split("-")))
    return slug or "palette"

def export_css_variables(name, colors):
    lines = [":root {"]
    for i, hex_code in enumerate(colors, 1):
        lines.append(f"  --color-{i}: {hex_code.lower()};")
    lines.append("}")
    return "\n".join(lines) + "\n"

def export_scss(name, colors):
    slug = _export_slug(name)
    lines = [f"${slug}-{i}: {hex_code.lower()};" for i, hex_code in enumerate(colors, 1)]
    return "\n".join(lines) + "\n"

def export_json(name, colors):
    return json.dumps({"name": name, "colors": [c.upper() for c in colors]}, indent=2) + "\n"

def export_swift(name, colors):
    lines = ["import SwiftUI", "", "extension Color {"]
    for i, hex_code in enumerate(colors, 1):
        r, g, b = hex_to_rgb_tuple(hex_code)
        lines.append(f"    static let color{i} = Color(red: {r / 255:.3f}, green: {g / 255:.3f}, blue: {b / 255:.3f})")
    lines.append("}")
    return "\n".join(lines) + "\n"

def export_tailwind(name, colors):
    slug = _export_slug(name)
    lines = [
        "// Add these inside theme.extend.colors in tailwind.config.js",
        "module.exports = {",
        "  theme: {",
        "    extend: {",
        "      colors: {",
    ]
    for i, hex_code in enumerate(colors, 1):
        lines.append(f"        '{slug}-{i}': '{hex_code.lower()}',")
    lines += ["      },", "    },", "  },", "};"]
    return "\n".join(lines) + "\n"

def export_gpl(name, colors):
    lines = ["GIMP Palette", f"Name: {name}", "Columns: 0", "#"]
    for i, hex_code in enumerate(colors, 1):
        r, g, b = hex_to_rgb_tuple(hex_code)
        lines.append(f"{r:3d} {g:3d} {b:3d}\tColor {i}")
    return "\n".join(lines) + "\n"

def export_ase(name, colors):
    body = b""
    for i, hex_code in enumerate(colors, 1):
        r, g, b = hex_to_rgb_tuple(hex_code)
        color_name = f"Color {i}"
        name_utf16 = color_name.encode("utf-16-be") + b"\x00\x00"
        entry = (
            struct.pack(">H", len(name_utf16) // 2)
            + name_utf16
            + b"RGB "
            + struct.pack(">3f", r / 255.0, g / 255.0, b / 255.0)
            + struct.pack(">H", 2)
        )
        body += struct.pack(">H", 0x0001) + struct.pack(">I", len(entry)) + entry
    header = b"ASEF" + struct.pack(">HH", 1, 0) + struct.pack(">I", len(colors))
    return header + body

def export_procreate_swatches(name, colors):
    swatches = []
    for hex_code in colors:
        r, g, b = hex_to_rgb_tuple(hex_code)
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        swatches.append({"hue": h, "saturation": s, "brightness": v, "alpha": 1, "colorSpace": 0, "version": 1})
    
    payload_obj = [{"name": name, "swatches": swatches}]
    payload = json.dumps(payload_obj).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Swatches.json", payload)
    return buf.getvalue()

def export_cube_lut(name, colors):
    if not colors:
        colors = ["#808080"]

    palette_oklab = [hex_to_oklab(hex_code) for hex_code in colors]
    palette_sorted = sorted(palette_oklab, key=lambda c: c[0])
    num_p = len(palette_sorted)

    def _linear_to_srgb_float(c):
        c = max(0.0, min(1.0, c))
        return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055

    def _sample_lut(r_in, g_in, b_in):
        if r_in <= 0.0 and g_in <= 0.0 and b_in <= 0.0:
            return 0.0, 0.0, 0.0
        if r_in >= 1.0 and g_in >= 1.0 and b_in >= 1.0:
            return 1.0, 1.0, 1.0

        lr = _srgb_to_linear(r_in * 255.0)
        lg = _srgb_to_linear(g_in * 255.0)
        lb = _srgb_to_linear(b_in * 255.0)

        l = _OKLAB_M1[0][0] * lr + _OKLAB_M1[0][1] * lg + _OKLAB_M1[0][2] * lb
        m = _OKLAB_M1[1][0] * lr + _OKLAB_M1[1][1] * lg + _OKLAB_M1[1][2] * lb
        s = _OKLAB_M1[2][0] * lr + _OKLAB_M1[2][1] * lg + _OKLAB_M1[2][2] * lb
        l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
        L_in = _OKLAB_M2[0][0] * l_ + _OKLAB_M2[0][1] * m_ + _OKLAB_M2[0][2] * s_
        a_in = _OKLAB_M2[1][0] * l_ + _OKLAB_M2[1][1] * m_ + _OKLAB_M2[1][2] * s_
        b_in = _OKLAB_M2[2][0] * l_ + _OKLAB_M2[2][1] * m_ + _OKLAB_M2[2][2] * s_

        if num_p == 1:
            L_zone, a_zone, b_zone = palette_sorted[0]
        else:
            pos = L_in * (num_p - 1)
            idx0 = int(math.floor(pos))
            idx1 = min(num_p - 1, idx0 + 1)
            t_lum = pos - idx0

            L0, a0, b0 = palette_sorted[idx0]
            L1, a1, b1 = palette_sorted[idx1]

            L_zone = L0 + t_lum * (L1 - L0)
            a_zone = a0 + t_lum * (a1 - a0)
            b_zone = b0 + t_lum * (b1 - b0)

        sum_w = 0.0
        target_L_idw = 0.0
        target_a_idw = 0.0
        target_b_idw = 0.0
        for pL, pa, pb in palette_oklab:
            dist = math.sqrt((L_in - pL) ** 2 * 0.5 + (a_in - pa) ** 2 + (b_in - pb) ** 2) + 1e-4
            w = 1.0 / (dist ** 2.0)
            sum_w += w
            target_L_idw += w * pL
            target_a_idw += w * pa
            target_b_idw += w * pb

        target_L_idw /= sum_w
        target_a_idw /= sum_w
        target_b_idw /= sum_w

        L_target = 0.4 * L_zone + 0.6 * target_L_idw
        a_target = 0.4 * a_zone + 0.6 * target_a_idw
        b_target = 0.4 * b_zone + 0.6 * target_b_idw

        blend_strength = 0.80
        boundary_taper = math.sin(max(0.0, min(1.0, L_in)) * math.pi) ** 0.4
        eff_blend = blend_strength * boundary_taper

        L_out = L_in * (1.0 - eff_blend) + L_target * eff_blend
        a_out = a_in * (1.0 - eff_blend) + a_target * eff_blend
        b_out = b_in * (1.0 - eff_blend) + b_target * eff_blend

        l_out_ = _OKLAB_M2_INV[0][0] * L_out + _OKLAB_M2_INV[0][1] * a_out + _OKLAB_M2_INV[0][2] * b_out
        m_out_ = _OKLAB_M2_INV[1][0] * L_out + _OKLAB_M2_INV[1][1] * a_out + _OKLAB_M2_INV[1][2] * b_out
        s_out_ = _OKLAB_M2_INV[2][0] * L_out + _OKLAB_M2_INV[2][1] * a_out + _OKLAB_M2_INV[2][2] * b_out
        l_lin, m_lin, s_lin = l_out_ ** 3, m_out_ ** 3, s_out_ ** 3

        r_lin = _OKLAB_M1_INV[0][0] * l_lin + _OKLAB_M1_INV[0][1] * m_lin + _OKLAB_M1_INV[0][2] * s_lin
        g_lin = _OKLAB_M1_INV[1][0] * l_lin + _OKLAB_M1_INV[1][1] * m_lin + _OKLAB_M1_INV[1][2] * s_lin
        b_lin = _OKLAB_M1_INV[2][0] * l_lin + _OKLAB_M1_INV[2][1] * m_lin + _OKLAB_M1_INV[2][2] * s_lin

        r_out = _linear_to_srgb_float(r_lin)
        g_out = _linear_to_srgb_float(g_lin)
        b_out = _linear_to_srgb_float(b_lin)

        return r_out, g_out, b_out

    size = 33
    lines = [
        f'TITLE "{name}"',
        f'LUT_3D_SIZE {size}',
        '# Created by Color Palette Manager',
        ''
    ]
    for b_idx in range(size):
        b_in = b_idx / (size - 1)
        for g_idx in range(size):
            g_in = g_idx / (size - 1)
            for r_idx in range(size):
                r_in = r_idx / (size - 1)
                r_out, g_out, b_out = _sample_lut(r_in, g_in, b_in)
                lines.append(f"{r_out:.6f} {g_out:.6f} {b_out:.6f}")
    return "\n".join(lines) + "\n"

EXPORT_FORMATS = {
    "CSS Variables (.css)": (".css", export_css_variables, False),
    "SCSS (.scss)": (".scss", export_scss, False),
    "JSON (.json)": (".json", export_json, False),
    "Swift (.swift)": (".swift", export_swift, False),
    "Tailwind CSS (.js)": (".js", export_tailwind, False),
    "GIMP/Krita/Inkscape (.gpl)": (".gpl", export_gpl, False),
    "Adobe Swatch Exchange (.ase)": (".ase", export_ase, True),
    "Procreate (.swatches)": (".swatches", export_procreate_swatches, True),
    "3D LUT (.cube)": (".cube", export_cube_lut, False),
}

# --- Import Format Parsers ---
_HEX_TOKEN_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")

def _normalize_hex(h):
    h = h.strip()
    if not h.startswith("#"):
        h = "#" + h
    return h.upper()

def _looks_like_hex(s):
    s = s.strip().lstrip("#")
    return len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s)

def _dedupe_preserve_order(colors):
    seen = set()
    out = []
    for c in colors:
        cu = c.upper()
        if cu not in seen:
            seen.add(cu)
            out.append(cu)
    return out

def import_hex_text(data):
    """Fallback parser: pulls every '#RRGGBB' token out of plain text -
    covers CSS/SCSS/Tailwind-JS (every text export format this app itself
    writes), plain hex lists, and most other text-based third-party
    palette formats that don't get a dedicated parser below."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="ignore")
    return _dedupe_preserve_order(_normalize_hex(h) for h in _HEX_TOKEN_RE.findall(data))

def import_json(data):
    """This app's own {"name":..., "colors":[...]} export, or any other
    JSON that has hex strings somewhere in it (bare list of hex strings,
    {name: hex} maps, nested palette collections, etc.) - walks the whole
    parsed structure and collects every string that looks like a hex
    color, regardless of the surrounding shape."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="ignore")
    parsed = json.loads(data)
    colors = []

    def _walk(node):
        if isinstance(node, str):
            if _looks_like_hex(node):
                colors.append(_normalize_hex(node))
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(parsed)
    return _dedupe_preserve_order(colors)

def import_gpl(data):
    """GIMP/Krita/Inkscape .gpl palette - lines of 'R G B  optional-name',
    skipping the 'GIMP Palette' header, 'Name:'/'Columns:' metadata, and
    '#' comment lines (mirrors export_gpl's own layout in reverse)."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="ignore")
    colors = []
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("GIMP Palette") \
                or line.lower().startswith("name:") or line.lower().startswith("columns:"):
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        colors.append(rgb_to_hex((r, g, b)))
    return _dedupe_preserve_order(colors)

def import_ase(data):
    """Adobe Swatch Exchange .ase - reverses export_ase's own plain-color
    entries (block type 0x0001). Group headers/footers (0xC001/0xC002)
    carry no color of their own and are skipped. Handles RGB/Gray/CMYK
    color models - export_ase only ever writes RGB, but other tools'
    .ase files commonly use the other two."""
    if len(data) < 12 or data[:4] != b"ASEF":
        raise ValueError("Not a valid .ase file (missing ASEF header).")
    num_blocks = struct.unpack(">I", data[8:12])[0]
    colors = []
    pos = 12
    for _ in range(num_blocks):
        if pos + 6 > len(data):
            break
        block_type, block_len = struct.unpack(">HI", data[pos:pos + 6])
        pos += 6
        block = data[pos:pos + block_len]
        pos += block_len
        if block_type != 0x0001:
            continue
        name_len = struct.unpack(">H", block[0:2])[0]
        offset = 2 + name_len * 2
        color_model = block[offset:offset + 4]
        offset += 4
        if color_model == b"RGB ":
            r, g, b = struct.unpack(">3f", block[offset:offset + 12])
            colors.append(rgb_to_hex((r * 255, g * 255, b * 255)))
        elif color_model == b"Gray":
            k, = struct.unpack(">f", block[offset:offset + 4])
            v = k * 255
            colors.append(rgb_to_hex((v, v, v)))
        elif color_model == b"CMYK":
            c, m, y, k = struct.unpack(">4f", block[offset:offset + 16])
            colors.append(rgb_to_hex((255 * (1 - c) * (1 - k), 255 * (1 - m) * (1 - k), 255 * (1 - y) * (1 - k))))
    return _dedupe_preserve_order(colors)

def import_aco(data):
    """Photoshop/Illustrator .aco swatch file - the most common binary
    palette format users have lying around that this app doesn't itself
    export. Handles both version 1 (fixed 10-byte entries) and version 2
    (same entries, each followed by a variable-length UTF-16 name) - a
    file with both blocks concatenated (Photoshop's own compatibility
    trick) starts with a version-1 header, so only that first block gets
    read, avoiding a duplicate second pass over the same colors."""
    if len(data) < 4:
        raise ValueError("Not a valid .aco file.")
    version, count = struct.unpack(">HH", data[0:4])
    pos = 4
    colors = []
    for _ in range(count):
        if pos + 10 > len(data):
            break
        color_space, w, x, y, z = struct.unpack(">Hhhhh", data[pos:pos + 10])
        pos += 10
        w, x, y, z = w & 0xFFFF, x & 0xFFFF, y & 0xFFFF, z & 0xFFFF
        if version == 2:
            if pos + 4 > len(data):
                break
            name_len = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4 + name_len * 2
        if color_space == 0:  # RGB
            colors.append(rgb_to_hex((w >> 8, x >> 8, y >> 8)))
        elif color_space == 1:  # HSB
            r, g, b = colorsys.hsv_to_rgb(w / 65535.0, x / 65535.0, y / 65535.0)
            colors.append(rgb_to_hex((r * 255, g * 255, b * 255)))
        elif color_space == 2:  # CMYK
            c, m, ye, k = w / 65535.0, x / 65535.0, y / 65535.0, z / 65535.0
            colors.append(rgb_to_hex((255 * (1 - c) * (1 - k), 255 * (1 - m) * (1 - k), 255 * (1 - ye) * (1 - k))))
        elif color_space == 8:  # Grayscale
            v = (w / 10000.0) * 255
            colors.append(rgb_to_hex((v, v, v)))
    return _dedupe_preserve_order(colors)

def import_procreate_swatches(data):
    """Procreate .swatches - reverses export_procreate_swatches's own
    zipped Swatches.json (hue/saturation/brightness dicts). Walks the
    parsed JSON at any nesting depth rather than assuming the exact
    [[{...}]] shape this app writes, since Procreate itself and other
    tools nest it differently across versions."""
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf) as zf:
        payload = zf.read("Swatches.json")
    parsed = json.loads(payload)
    colors = []

    def _walk(node):
        if isinstance(node, dict):
            # Procreate might capitalize these, or keep them lowercase, we accept either
            keys_lower = {k.lower(): v for k, v in node.items()}
            if {"hue", "saturation", "brightness"} <= keys_lower.keys():
                r, g, b = colorsys.hsv_to_rgb(keys_lower["hue"], keys_lower["saturation"], keys_lower["brightness"])
                colors.append(rgb_to_hex((r * 255, g * 255, b * 255)))
            else:
                for val in node.values():
                    _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(parsed)
    return _dedupe_preserve_order(colors)

# ext -> (parser, is_binary)
IMPORT_FORMATS = {
    ".json": (import_json, False),
    ".gpl": (import_gpl, False),
    ".ase": (import_ase, True),
    ".aco": (import_aco, True),
    ".swatches": (import_procreate_swatches, True),
}

def import_palette_colors(path):
    """Best-effort palette import dispatched by file extension: this
    app's own .json, GIMP/Krita/Inkscape .gpl, Adobe Swatch Exchange
    .ase, Photoshop/Illustrator .aco, and Procreate .swatches each get a
    dedicated parser; anything else (.css/.scss/.txt/.js, or an unknown
    extension) falls back to pulling every '#RRGGBB' token out of the
    raw text, which also covers this app's own CSS/SCSS/Tailwind exports.
    Returns a deduped list of uppercase hex strings; raises ValueError
    (with a message safe to show the user directly) if the file can't be
    read as any recognized color format."""
    ext = os.path.splitext(path)[1].lower()
    parser, is_binary = IMPORT_FORMATS.get(ext, (import_hex_text, False))
    with open(path, "rb" if is_binary else "r", **({} if is_binary else {"encoding": "utf-8", "errors": "ignore"})) as f:
        data = f.read()
    try:
        colors = parser(data)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Could not parse this file as a color palette: {e}")
    if not colors:
        raise ValueError("No colors could be found in this file.")
    return colors
