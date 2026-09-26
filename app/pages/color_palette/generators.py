#!/usr/bin/env python3
"""
Color Palette's six generators - Harmony, Light/Dark Theme, Gradient Ramp,
Grayscale/Neutral, Neon/Pastel and Glass - as plain Python: what each one
holds, what its controls do and what it shows. No Qt, so they're unit-tested
(tests/test_color_palette.py). The page (page.py) keeps one of each for the session and its Generators tab draws
the open one (web/palette.js), so what you left in one is still there when
you come back.

The colour maths is color_engine's, unchanged. What they share:
  - "Regen amount" jitter: Regenerate nudges every colour's hue, saturation
    and value by a random amount (hue up to 0.04 * pct/15 of the wheel,
    saturation and value up to pct/100), which Undo/Redo step through.
  - The preview can be rearranged by hand: drag one swatch onto another to
    swap them, or drop a colour on one to replace it. That lasts until the
    colours are made again.
  - Vision simulation is the page's own (its header), for the preview only;
    copies, saves and drags always use the real colours.

Each generator's view() returns the data its view draws; act(action,
payload) does one of its controls and returns False for anything it
doesn't know or can't take.
"""

import random

from .color_engine import (
    generate_color_harmony, generate_glass_colors, generate_grayscale_neutrals, generate_neon,
    generate_pastel, generate_theme_variant, hex_to_hsv, hsv_to_hex, sample_gradient_at_pos,
)

IDS = ("harmony", "theme", "gradient", "grayscale", "mood", "glass")
INTENSITIES = [str(p) for p in range(10, 55, 5)]
DEFAULT_BASE = "#2C456B"


def _hex(value):
    """A colour the view sent -> '#RRGGBB', or None."""
    from .view import normalize_hex
    return normalize_hex(value)


def _index(value, length):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < length:
        return value
    return None


def make_jitter(count, intensity, rng=random):
    pct = int(intensity)
    sv, hue = pct / 100.0, 0.04 * (pct / 15.0)
    return [(rng.uniform(-hue, hue), rng.uniform(-sv, sv), rng.uniform(-sv, sv)) for _ in range(count)]


def apply_jitter(colors, jitter, keep=None):
    """colors nudged by jitter (if it fits), uppercased. keep: a colour
    left as it is (Harmony's base)."""
    if not jitter or len(jitter) != len(colors):
        return [c.upper() for c in colors]
    out = []
    for c, (jh, js, jv) in zip(colors, jitter):
        if keep and c.upper() == keep.upper():
            out.append(c.upper())
            continue
        h, s, v = hex_to_hsv(c)
        out.append(hsv_to_hex((h + jh) % 1.0, max(0.0, min(1.0, s + js)), max(0.0, min(1.0, v + jv))).upper())
    return out


class _Generator:
    """Undo/Redo over a snapshot, and the hand-arranged preview."""

    kind = ""
    title = ""
    create_label = ""
    prompt_title = ""

    def __init__(self, settings, save, rng=random):
        self.settings, self._save, self.rng = settings, save, rng
        self.undo_stack, self.redo_stack = [], []
        self.preview = []

    # --- undo --------------------------------------------------------
    def _snapshot(self):
        raise NotImplementedError

    def _restore(self, snap):
        raise NotImplementedError

    def push_undo(self):
        self.undo_stack.append(self._snapshot())
        self.redo_stack.clear()

    def _step(self, source, target):
        if not source:
            return False
        target.append(self._snapshot())
        self._restore(source.pop())
        self.make()
        return True

    def make(self):
        """Works out the preview from the current settings."""
        raise NotImplementedError

    # --- preview arranging -------------------------------------------
    def swap(self, a, b):
        a, b = _index(a, len(self.preview)), _index(b, len(self.preview))
        if a is None or b is None or a == b:
            return False
        self.preview[a], self.preview[b] = self.preview[b], self.preview[a]
        return True

    def replace(self, index, hex_code):
        index, hex_code = _index(index, len(self.preview)), _hex(hex_code)
        if index is None or not hex_code:
            return False
        self.preview[index] = hex_code
        return True

    def result(self):
        """The colours a new palette gets."""
        return list(self.preview)

    def default_name(self):
        return ""

    def act(self, action, p):
        if action == "undo":
            return self._step(self.undo_stack, self.redo_stack)
        if action == "redo":
            return self._step(self.redo_stack, self.undo_stack)
        if action == "swap":
            return self.swap(p.get("from"), p.get("to"))
        if action == "replace":
            return self.replace(p.get("index"), p.get("hex"))
        return False

    def common_view(self):
        return {"id": self.kind, "title": self.title, "create_label": self.create_label,
                "prompt_title": self.prompt_title, "default_name": self.default_name(),
                "can_undo": bool(self.undo_stack), "can_redo": bool(self.redo_stack)}


# --------------------------------------------------- harmony & neutrals --

class _BaseColorGenerator(_Generator):
    """Harmony and Grayscale: everything comes from one base colour, and
    changes as soon as a control does. Undo covers the base and the
    Regenerate nudges."""

    count_range = (3, 20, 5)

    def __init__(self, settings, save, rng=random):
        super().__init__(settings, save, rng)
        self.base = DEFAULT_BASE
        self.jitter = None
        self.intensity = "15"
        self.count = self.count_range[2]

    def _snapshot(self):
        return self.base, list(self.jitter) if self.jitter else None

    def _restore(self, snap):
        self.base, self.jitter = snap

    def engine(self):
        raise NotImplementedError

    def make(self):
        self.preview = apply_jitter(self.engine(), self.jitter, keep=self._keep())

    def _keep(self):
        return None

    def set_count(self, value):
        low, high, _default = self.count_range
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            return False
        self.count, self.jitter = value, None
        return True

    def act(self, action, p):
        if action == "base":
            hex_code = _hex(p.get("hex"))
            if not hex_code:
                return False
            self.base, self.jitter = hex_code, None
        elif action == "count":
            if not self.set_count(p.get("value")):
                return False
        elif action == "intensity":
            if p.get("value") not in INTENSITIES:
                return False
            self.intensity = p["value"]
            return True
        elif action == "regenerate":
            self.push_undo()
            self.jitter = make_jitter(self.count, self.intensity, self.rng)
        elif action == "randomize":
            self.push_undo()
            self.base = hsv_to_hex(self.rng.random(), self.rng.uniform(0.55, 0.9), self.rng.uniform(0.45, 0.9)).upper()
            self.jitter = None
        else:
            return super().act(action, p)
        self.make()
        return True

    def view(self, swatch):
        low, high, default = self.count_range
        return dict(self.common_view(), kind="base", base=swatch(self.base), count=self.count,
                    count_min=low, count_max=high, count_default=default, intensity=self.intensity,
                    intensities=INTENSITIES, preview=[swatch(c) for c in self.preview])


class Harmony(_BaseColorGenerator):
    kind = "harmony"
    title = "Colour harmony generator"
    create_label = "Create palette from harmony…"
    prompt_title = "New harmony palette"
    RULES = ["Analogous", "Complementary", "Split complementary", "Triad", "Shades", "Monochromatic"]
    WHEELS = ["RYB", "RGB", "OKLCH"]

    def __init__(self, settings, save, rng=random):
        super().__init__(settings, save, rng)
        self.rule = "Analogous"
        wheel = settings.get("wheel", "RYB")
        self.wheel = wheel if wheel in self.WHEELS else "RYB"
        try:
            count = int(settings.get("harmony_count", 5))
        except (TypeError, ValueError):
            count = 5
        self.count = count if 3 <= count <= 20 else 5
        intensity = str(settings.get("harmony_intensity", "15"))
        self.intensity = intensity if intensity in INTENSITIES else "15"
        self.make()

    def engine(self):
        return generate_color_harmony(self.base, self.rule, self.wheel, self.count)

    def _keep(self):
        return self.base

    def default_name(self):
        return f"{self.rule} ({self.base})"

    def act(self, action, p):
        if action == "rule":
            if p.get("value") not in self.RULES:
                return False
            self.rule, self.jitter = p["value"], None
            self.make()
            return True
        if action == "wheel":
            if p.get("value") not in self.WHEELS:
                return False
            self.wheel, self.jitter = p["value"], None
            self.settings["wheel"] = self.wheel
            self._save()
            self.make()
            return True
        done = super().act(action, p)
        if done and action == "count":
            self.settings["harmony_count"] = self.count
            self._save()
        elif done and action == "intensity":
            self.settings["harmony_intensity"] = self.intensity
            self._save()
        return done

    def view(self, swatch):
        return dict(super().view(swatch), rule=self.rule, rules=self.RULES, wheel=self.wheel, wheels=self.WHEELS)


class Grayscale(_BaseColorGenerator):
    kind = "grayscale"
    title = "Grayscale / neutral generator"
    create_label = "Create palette from neutrals…"
    prompt_title = "New neutral palette"
    count_range = (2, 12, 6)

    def __init__(self, settings, save, rng=random):
        super().__init__(settings, save, rng)
        self.make()

    def engine(self):
        return generate_grayscale_neutrals(self.base, self.count)

    def default_name(self):
        return f"Neutrals ({self.base})"


# ---------------------------------------------- theme, neon/pastel, glass --

class _ColorsGenerator(_Generator):
    """Theme, Neon/Pastel and Glass: a short list of colours you build up,
    then a button makes the preview from them. Editing the list leaves the
    preview as it is until the button is pressed again (and forgets the
    Undo history, which was about the old list)."""

    min_colors, max_colors = 1, 10
    hint = ""

    def __init__(self, settings, save, rng=random):
        super().__init__(settings, save, rng)
        self.colors = []
        self.jitter = None
        self.intensity = "15"

    def _changed(self):
        self.jitter = None
        self.undo_stack.clear()
        self.redo_stack.clear()

    def edit_colors(self, action, p):
        colors = self.colors
        if action == "edit":
            i, hex_code = _index(p.get("index"), len(colors)), _hex(p.get("hex"))
            if i is None or not hex_code:
                return False
            colors[i] = hex_code
        elif action == "add_color":
            hex_code = _hex(p.get("hex"))
            if not hex_code or len(colors) >= self.max_colors:
                return False
            colors.append(hex_code)
        elif action == "remove":
            i = _index(p.get("index"), len(colors))
            if i is None or len(colors) <= self.min_colors:
                return False
            colors.pop(i)
        elif action == "reorder":
            src = _index(p.get("from"), len(colors))
            dst = p.get("to")
            if src is None or not isinstance(dst, int) or isinstance(dst, bool) or dst == src:
                return False
            color = colors.pop(src)
            if dst > src:
                dst -= 1
            colors.insert(max(0, min(dst, len(colors))), color)
        elif action == "clear":
            if not colors:
                return False
            colors.clear()
        elif action == "add_palette":
            room = self.max_colors - len(colors)
            new = [h for h in (_hex(c) for c in (p.get("colors") or [])[:room]) if h]
            if not new:
                return False
            colors.extend(new)
        else:
            return None
        self._changed()
        return True

    def act(self, action, p):
        edited = self.edit_colors(action, p)
        if edited is not None:
            return edited
        if action == "intensity":
            if p.get("value") not in INTENSITIES:
                return False
            self.intensity = p["value"]
            return True
        return super().act(action, p)

    def colors_view(self, swatch):
        return {"kind": "colors", "colors": [swatch(c) for c in self.colors], "min": self.min_colors,
                "max": self.max_colors, "hint": self.hint, "intensity": self.intensity,
                "intensities": INTENSITIES, "preview": [swatch(c) for c in self.preview]}


class _ModeGenerator(_ColorsGenerator):
    """Theme (Light/Dark) and Neon/Pastel: a pair of buttons, each making
    the preview in its own way."""

    MODES = ()

    def __init__(self, settings, save, rng=random):
        super().__init__(settings, save, rng)
        self.mode = None

    def _snapshot(self):
        return self.mode, list(self.jitter) if self.jitter else None

    def _restore(self, snap):
        self.mode, self.jitter = snap

    def mapped(self):
        raise NotImplementedError

    def make(self):
        self.preview = apply_jitter(self.mapped(), self.jitter) if self.colors and self.mode else []

    def act(self, action, p):
        if action == "mode":
            if p.get("value") not in self.MODES or not self.colors:
                return False
            self.push_undo()
            self.mode = p["value"]
            self.jitter = make_jitter(len(self.colors), self.intensity, self.rng)
            self.make()
            return True
        return super().act(action, p)

    def view(self, swatch):
        return dict(self.common_view(), **self.colors_view(swatch), mode=self.mode, modes=list(self.MODES))


class Theme(_ModeGenerator):
    kind = "theme"
    title = "Light / dark theme generator"
    create_label = "Create palette from theme…"
    prompt_title = "New theme palette"
    hint = "Drag colours into the grid on the right, then click Light or Dark."
    MODES = ("Light", "Dark")

    def mapped(self):
        return generate_theme_variant(self.colors, self.mode == "Dark")

    def default_name(self):
        return f"Theme ({self.mode or 'Light'})"


class Mood(_ModeGenerator):
    kind = "mood"
    title = "Neon / pastel generator"
    create_label = "Create palette from mood…"
    prompt_title = "New mood palette"
    hint = "Drag colours into the grid on the right, then click Neon or Pastel."
    MODES = ("Neon", "Pastel")

    def mapped(self):
        return (generate_neon if self.mode == "Neon" else generate_pastel)(self.colors)

    def default_name(self):
        return f"{self.mode or 'Neon'} Mix"


class Glass(_ColorsGenerator):
    kind = "glass"
    title = "Glass generator"
    create_label = "Create palette from glass…"
    prompt_title = "New glass palette"
    hint = "Drag 2–3 colours into the grid on the right, then click Generate."
    min_colors, max_colors = 2, 3
    SLOTS = ("primary", "highlight", "shadow")
    LABELS = ("Primary", "Highlight", "Shadow / border")
    DEFAULT_SHAPE = ("#5D8EC4", "#FFFFFF", "#20304A")

    def __init__(self, settings, save, rng=random):
        super().__init__(settings, save, rng)
        self.colors = ["#2C7DE0", "#8899FF"]
        self.generated = False

    def _snapshot(self):
        return list(self.jitter) if self.jitter else None

    def _restore(self, snap):
        self.jitter = snap

    def make(self):
        if not self.generated or len(self.colors) < self.min_colors:
            return
        mapped = generate_glass_colors(self.colors)
        self.preview = apply_jitter([mapped[s] for s in self.SLOTS], self.jitter)

    def act(self, action, p):
        if action == "generate":
            if len(self.colors) < self.min_colors:
                return False
            self.push_undo()
            self.jitter = make_jitter(len(self.SLOTS), self.intensity, self.rng)
            self.generated = True
            self.make()
            return True
        return super().act(action, p)

    def default_name(self):
        return "Glass Set"

    def shape(self):
        """The glass pill's colours: the real ones, never Vision-simulated."""
        primary, highlight, shadow = self.preview if len(self.preview) == 3 else self.DEFAULT_SHAPE
        h, s, v = hex_to_hsv(primary)
        return {"primary": primary, "highlight": highlight, "shadow": shadow,
                "blob1": hsv_to_hex((h - 0.06) % 1.0, min(1.0, s + 0.15), max(v, 0.6)).upper(),
                "blob2": hsv_to_hex((h + 0.07) % 1.0, min(1.0, s + 0.10), max(v, 0.6)).upper()}

    def view(self, swatch):
        data = dict(self.common_view(), **self.colors_view(swatch))
        data["labels"] = list(self.LABELS)
        data["shape"] = self.shape()
        return data


# ------------------------------------------------------------- gradient --

class Gradient(_Generator):
    """A gradient through 2-10 colours, drawn as a picture (render_image)
    and exported as a PNG. Weight bends where the colours fall; the easing
    curve bends the whole ramp."""

    kind = "gradient"
    title = "Gradient ramp generator"
    MIN_COLORS, MAX_COLORS = 2, 10
    MODES = ["LRGB", "HSL", "OKLCH", "HCL", "LAB"]
    STYLES = ["Linear", "Radial"]
    EASING_PRESETS = {"Linear": (0.33, 0.33, 0.67, 0.67), "Ease": (0.25, 0.1, 0.25, 1.0),
                      "Fun": (0.68, -0.6, 0.32, 1.6)}
    WEIGHT_K = 2.5            # at full left/right, gamma = 2**(+-WEIGHT_K)
    TWO_COLOR_SAMPLES = 41
    ASPECTS = {"16:9": (16, 9), "9:16": (9, 16)}
    PREVIEW_LONG_SIDE = 640
    RESOLUTIONS = {"4K": (3840, 2160), "1080p": (1920, 1080), "720p": (1280, 720)}
    DEFAULT_PNG = {"width": 3840, "height": 2160, "bit_depth": 8}

    def __init__(self, settings, save, rng=random):
        super().__init__(settings, save, rng)
        self.style = "Linear"
        self.angle = 45.0
        self.mode = "HCL"
        self.colors = ["#140F8C", "#FFE600"]
        self.weight = 0
        self.easing = self.EASING_PRESETS["Linear"]
        self.preset = "Linear"
        self.aspect = "9:16"
        self.png = self.png_settings(dict(self.DEFAULT_PNG, **(settings.get("gradient_png_export") or {})))

    @classmethod
    def png_settings(cls, raw):
        """Export size and depth, kept to what the exporter takes."""
        def size(value, default):
            try:
                return max(16, min(8192, int(value)))
            except (TypeError, ValueError):
                return default
        return {"width": size(raw.get("width"), 3840), "height": size(raw.get("height"), 2160),
                "bit_depth": 16 if raw.get("bit_depth") == 16 else 8}

    def make(self):
        pass

    def stops(self):
        n = len(self.colors)
        if n <= 1:
            return [(0.0, self.colors[0] if self.colors else "#FFFFFF")]
        gamma = 2.0 ** (self.weight / 100.0 * self.WEIGHT_K)
        if n == 2:
            # With two colours there's no middle stop for Weight to move, so
            # sample the blend at warped positions and lay those out evenly.
            base = ((0.0, self.colors[0]), (100.0, self.colors[1]))
            k = self.TWO_COLOR_SAMPLES
            return [(i / (k - 1) * 100.0, sample_gradient_at_pos(base, (i / (k - 1)) ** gamma * 100.0, self.mode))
                    for i in range(k)]
        return [((i / (n - 1)) ** gamma * 100.0, c) for i, c in enumerate(self.colors)]

    def preview_size(self):
        rw, rh = self.ASPECTS[self.aspect]
        scale = self.PREVIEW_LONG_SIDE / max(rw, rh)
        return int(rw * scale), int(rh * scale)

    def render_image(self, width=None, height=None):
        from .color_engine import render_gradient_pil_image
        if width is None:
            width, height = self.preview_size()
        return render_gradient_pil_image(self.stops(), style=self.style, angle=self.angle, color_space=self.mode,
                                         width=width, height=height, easing=self.easing)

    def weight_label(self):
        if self.weight == 0:
            return "Even"
        return f"◀ {abs(self.weight)}" if self.weight < 0 else f"{self.weight} ▶"

    def act(self, action, p):
        colors = self.colors
        if action == "edit":
            i, hex_code = _index(p.get("index"), len(colors)), _hex(p.get("hex"))
            if i is None or not hex_code:
                return False
            colors[i] = hex_code
        elif action == "add_color":
            hex_code = _hex(p.get("hex"))
            if not hex_code or len(colors) >= self.MAX_COLORS:
                return False
            colors.append(hex_code)
        elif action == "remove":
            i = _index(p.get("index"), len(colors))
            if i is None or len(colors) <= self.MIN_COLORS:
                return False
            colors.pop(i)
        elif action == "reorder":
            src, dst = _index(p.get("from"), len(colors)), p.get("to")
            if src is None or not isinstance(dst, int) or isinstance(dst, bool) or dst == src:
                return False
            color = colors.pop(src)
            if dst > src:
                dst -= 1
            colors.insert(max(0, min(dst, len(colors))), color)
        elif action == "clear":
            if not colors:
                return False
            colors.clear()
        elif action == "add_palette":
            room = self.MAX_COLORS - len(colors)
            new = [h for h in (_hex(c) for c in (p.get("colors") or [])[:room]) if h]
            if not new:
                return False
            colors.extend(new)
        elif action == "style":
            if p.get("value") not in self.STYLES:
                return False
            self.style = p["value"]
        elif action == "mode":
            if p.get("value") not in self.MODES:
                return False
            self.mode = p["value"]
        elif action == "weight":
            value = p.get("value")
            if not isinstance(value, int) or isinstance(value, bool) or not -100 <= value <= 100:
                return False
            self.weight = value
        elif action == "angle":
            value = p.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            self.angle = float(value) % 360.0
        elif action == "easing":
            curve = p.get("curve")
            if not (isinstance(curve, list) and len(curve) == 4
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in curve)):
                return False
            x1, y1, x2, y2 = curve
            self.easing = (max(0.0, min(1.0, x1)), max(-0.6, min(1.6, y1)),
                           max(0.0, min(1.0, x2)), max(-0.6, min(1.6, y2)))
            self.preset = None
        elif action == "preset":
            if p.get("value") not in self.EASING_PRESETS:
                return False
            self.preset = p["value"]
            self.easing = self.EASING_PRESETS[self.preset]
        elif action == "aspect":
            if p.get("value") not in self.ASPECTS:
                return False
            self.aspect = p["value"]
        elif action == "png":
            self.png = self.png_settings(p)
            self.settings["gradient_png_export"] = dict(self.png)
            self._save()
        else:
            return False
        return True

    def view(self, swatch):
        return {"id": self.kind, "title": self.title, "kind": "gradient",
                "colors": [swatch(c) for c in self.colors], "min": self.MIN_COLORS, "max": self.MAX_COLORS,
                "style": self.style, "styles": self.STYLES, "angle": round(self.angle, 1),
                "mode": self.mode, "modes": self.MODES, "weight": self.weight, "weight_label": self.weight_label(),
                "easing": list(self.easing), "preset": self.preset, "presets": list(self.EASING_PRESETS),
                "aspect": self.aspect, "aspects": list(self.ASPECTS), "png": dict(self.png),
                "resolutions": {k: list(v) for k, v in self.RESOLUTIONS.items()}}


CLASSES = {"harmony": Harmony, "theme": Theme, "gradient": Gradient, "grayscale": Grayscale,
           "mood": Mood, "glass": Glass}


def make(kind, settings, save, rng=random):
    return CLASSES[kind](settings, save, rng)
