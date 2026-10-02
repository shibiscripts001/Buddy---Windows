#!/usr/bin/env python3
"""
The theme, as CSS custom properties for the web pages (core/web_page.py).

The Qt side styles widgets from colour-role tokens plus a shape table (see
core/theme.py's get_app_theme). A web page can't read that stylesheet, so
this turns the same inputs into one flat set of *semantic* variables -
"a button's fill", "a field's border" - rather than raw palette roles.
That keeps every web page's CSS family-agnostic: app/web/buddy.css says
`background: var(--btn-bg)` once, and Resolve's grey pills, Retro's ink
outlines, Modern's gradients and Nova's glass all come out of the mapping
below, the same place, from the same values the Qt widgets use.

No Qt imports - tests/test_web_theme.py runs on plain Python.
"""

from core.theme import (
    THEMES,
    _blend,
    _is_light,
    complementary_color,
    contrast_ratio,
    desktop_colors,
    ensure_contrast,
    get_shape_tokens,
    hue_distance,
    resolve,
    resolve_colors,
)

# Hue gap below which a palette's secondary is too close to its primary to
# tell two things apart by colour (~30 degrees on the wheel).
DISTINCT_HUE_GAP = 0.08


def _rgba(hex_color, alpha):
    """#RRGGBB + 0..1 alpha -> a CSS rgba()."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:g})"


def second_color(tokens):
    """A colour that reads as different from the accent - the palette's own
    secondary when it is far enough round the wheel (Retro's cyan/pink is a
    designed pairing), else a derived complement (Default's two lavenders
    would otherwise be one colour)."""
    primary, secondary = tokens["primary"], tokens.get("secondary", "")
    if secondary and hue_distance(primary, secondary) >= DISTINCT_HUE_GAP:
        return secondary
    return complementary_color(primary, tokens["surface"])


def _common(tokens, shape):
    surface = tokens["surface"]
    return {
        "font": shape["font"],
        "font-size": shape["font_size"],
        # The type scale. Page CSS sizes text only from these (checked by
        # tests/test_web_theme.py), so every page shares one scale.
        # xs: captions, badges, caps labels   sm: secondary text, hints, logs
        # md: body text in a page             lg: page, dialog and item titles
        # xl: a hero's name or heading        2xl-3xl: big numbers  display: the timer
        "fs-xs": "11px", "fs-sm": "12px", "fs-md": "13px", "fs-lg": "15px",
        "fs-xl": "18px", "fs-2xl": "24px", "fs-3xl": "32px", "fs-display": "64px",
        "font-mono": 'Consolas, "Cascadia Mono", Menlo, monospace',
        # Spacing rhythm shared by every page: inside a card, between the
        # things in a card, between a page's sections, between controls on a
        # row, and a card's header strip.
        "card-pad": "14px 16px", "card-gap": "12px", "section-gap": "12px",
        "control-gap": "8px", "card-head-pad": "4px 10px 4px 14px",
        "r-xs": shape["r_xs"], "r-sm": shape["r_sm"], "r-md": shape["r_md"],
        "r-lg": shape["r_lg"], "r-xl": shape["r_xl"], "r-2xl": shape["r_2xl"], "r-full": shape["r_full"],
        "bw": shape["bw"], "bw-thick": shape["bw_thick"],
        "primary": tokens["primary"],
        "on-primary": tokens["on_primary"],
        "primary-container": tokens["primary_container"],
        "on-primary-container": tokens["on_primary_container"],
        "surface": surface,
        "danger": tokens["danger"],
        "success": tokens["success"],
        "selection-bg": tokens["selection_bg"],
        "selection-fg": tokens["selection_fg"],
        # Readable-as-text versions of the two accents: a fill colour can be
        # far too faint as a label (Retro's cyan on peach is 2.1:1).
        "accent-text": ensure_contrast(tokens["primary"], surface),
        "second-text": ensure_contrast(second_color(tokens), surface),
        # Chat speakers and links; families override where the accent is
        # the wrong idea (Resolve keeps its red for focus of attention).
        "you-text": ensure_contrast(tokens["primary"], surface),
        "buddy-text": ensure_contrast(second_color(tokens), surface),
        "buddy-dot": ensure_contrast(second_color(tokens), surface),
        "link": ensure_contrast(tokens["primary"], surface),
        "danger-text": ensure_contrast(tokens["danger"], surface),
        "overlay-text": tokens["overlay_text"],
        "overlay-tint": _rgba(surface, 0.7),
        "glass": "0",
        # Space between the page and the shell's pane. Every family but
        # Resolve draws that pane as a card of its own (the QFrame rule in
        # get_app_theme), so the page keeps off its edges.
        "page-pad": "12px",
        # Nova's glow orbs (buddy.css); nothing elsewhere.
        "orb-1": "transparent", "orb-2": "transparent", "orb-3": "transparent",
        # Off-world's phosphor: text glow, scanlines, the lit middle of the
        # screen (buddy.css); nothing elsewhere.
        "phosphor-glow": "none", "scanline": "transparent", "screen-glow": "transparent",
        # Components a family may restyle without CSS of its own. Defaults
        # can name other variables: buddy.js sets them all on :root.
        "modal-bg": "var(--card-bg)",
        "table-head-bg": "var(--raised-bg)",
        "tab-active-bg": "transparent", "tab-active-fg": "var(--text-strong)",
        "tab-active-line": "var(--accent-text)",
        # Emphasis that means neither focus nor trouble: a page's icons, a
        # "Recommended" chip, an info banner's edge, progress and chart bars,
        # step numbers. The accent everywhere but Resolve, whose red means
        # focus of attention (and reads as an error on a folder icon).
        "emphasis": "var(--accent-text)", "emphasis-line": "var(--accent-text)",
        "emphasis-fill": "var(--primary)",
        "emphasis-container": "var(--primary-container)",
        "on-emphasis-container": "var(--on-primary-container)",
        # Ask Buddy: the question's bubble, and the glow on Buddy's dot.
        "bubble-bg": "color-mix(in srgb, var(--primary) 16%, transparent)",
        "bubble-fg": "var(--text-strong)",
        "bubble-border": "color-mix(in srgb, var(--primary) 32%, transparent)",
        "bubble-radius": "var(--r-2xl) var(--r-2xl) var(--r-xs) var(--r-2xl)",
        "dot-glow": "0 0 8px var(--buddy-dot)",
        # Desktop's hard offset shadow on buttons, its halftone dots, and
        # the colour of a title strip on the shell's own panels.
        "btn-shadow": "none", "halftone": "transparent", "chrome-title-bg": "transparent",
    }


def _material(tokens, shape):
    """Default ("Don't be evil"): the shared QSS template as-is, and the
    base Retro, Modern and Nova build on."""
    t = tokens
    return {
        "page-bg": t["surface"],
        "text": t["on_surface"], "text-strong": t["on_surface"], "text-dim": t["outline"],
        "card-bg": t["surface_container"], "card-border": t["outline_variant"],
        "card-radius": shape["r_2xl"], "card-shadow": "none", "card-blur": "none",
        "btn-bg": t["surface_container_high"], "btn-fg": t["on_surface"],
        "btn-border": t["outline_variant"], "btn-radius": shape["r_xl"],
        "btn-pad": "7px 16px", "btn-weight": "600",
        "btn-hover-bg": t["secondary_container"], "btn-hover-fg": t["primary"],
        "btn-hover-border": t["outline"], "btn-press-bg": t["primary_container"],
        "accent-bg": t["primary"], "accent-fg": t["on_primary"], "accent-border": "transparent",
        "accent-hover-bg": t["on_primary_container"], "accent-hover-fg": t["primary_container"],
        "accent-weight": "700", "accent-glow": "none",
        "danger-bg": "#F2B8B5", "danger-fg": "#601410", "danger-border": "transparent",
        "danger-hover-bg": "#F9DEDC",
        "field-bg": t["surface_container_high"], "field-fg": t["on_surface"],
        "field-border": t["outline"], "field-radius": shape["r_lg"], "field-pad": "6px 12px",
        "field-focus-border": t["primary"], "field-focus-bg": t["surface_container"],
        "field-focus-bw": shape["bw_thick"],
        "scroll": t["outline_variant"], "scroll-hover": t["primary"], "scroll-radius": shape["r_xs"],
        "scroll-width": "10px",
        "raised-bg": t["surface_container_high"],
        # The shell's header and nav rail (app/web/shell/).
        "header-bg": t["surface_container"], "rail-bg": t["surface_container"],
        "shell-border": t["outline_variant"],
        "header-radius": shape["r_pill"], "rail-radius": shape["r_round"],
        "title-fg": t["primary"], "title-size": "18px", "title-weight": "700",
        "nav-fg": t["on_surface"], "nav-size": "14px", "nav-weight": "600", "nav-pad": "10px 16px",
        "nav-radius": shape["r_2xl"], "nav-hover-bg": t["surface_container_high"], "nav-hover-fg": t["primary"],
        "nav-active-bg": t["primary_container"], "nav-active-fg": t["on_primary_container"],
        "nav-active-border": t["primary"], "nav-active-weight": "600", "nav-marker": "transparent",
        "nav-heading": t["outline"],
        # Dropdown lists (buddy.js): a surface of their own over the page.
        "pop-bg": t["surface_container"], "pop-border": t["outline_variant"],
        "pop-hover-bg": t["selection_bg"], "pop-hover-fg": t["selection_fg"],
        "pop-shadow": "0 12px 32px rgba(0, 0, 0, .35)", "pop-blur": "none",
    }


def _retro(tokens, shape):
    """Retro: the template's ink outlines on paper, flat - no glows - and
    ink-framed fills where others use a tint (its dim text is its ink, so a
    selected tab needs more than colour)."""
    t = tokens
    out = _material(tokens, shape)
    out.update({
        "btn-pad": "6px 14px",
        "tab-active-bg": t["primary_container"], "tab-active-fg": t["on_primary_container"],
        "tab-active-line": t["outline_variant"],
        "bubble-bg": t["primary_container"], "bubble-fg": t["on_primary_container"],
        "bubble-border": t["outline_variant"], "bubble-radius": "0px",
        "dot-glow": "none",
    })
    return out


def _saas(tokens, shape):
    """Modern: the template plus _saas_extra_rules - gradient cards, a
    gradient accent button, and a card edge leaning toward the accent."""
    t = tokens
    out = _material(tokens, shape)
    accent_hi = ensure_contrast(t["primary"], t["on_primary"], 3.6)
    accent_lo = _blend(accent_hi, "#000000", 0.22)
    edge = _blend(t["outline_variant"], t["primary"], 0.25)
    glow = t.get("surface_glow") or _blend(t["surface"], t["primary"], 0.18)
    out.update({
        "page-bg": "transparent",
        "page-glow": glow,
        "card-bg": f"linear-gradient(180deg, {t['surface_container_high']}, {t['surface_container']})",
        "card-border": edge,
        "accent-bg": f"linear-gradient(180deg, {accent_hi}, {accent_lo})",
        "accent-fg": t["on_primary"], "accent-hover-bg": accent_hi, "accent-hover-fg": t["on_primary"],
        "btn-pad": "8px 18px",
        "field-pad": "8px 12px",
        "shell-border": edge,
        "nav-active-bg": f"linear-gradient(90deg, {t['primary_container']}, "
                         f"{_blend(t['primary_container'], t['surface_container'], 0.5)})",
        "title-fg": t["on_surface"], "title-size": "19px",
    })
    return out


def _nova(tokens, shape):
    """Nova: Modern's structure as glass - translucent cards
    over soft glow orbs (drawn by themes/nova.css), a glowing
    accent. Web only; the Qt widgets get the gradient look (theme.py)."""
    t = tokens
    out = _saas(tokens, shape)
    light = _is_light(t["surface"])
    ink = "#000000" if light else "#FFFFFF"
    accent = ensure_contrast(t["primary"], t["on_primary"], 4.5)
    accent_hover = ensure_contrast(_blend(accent, "#FFFFFF", 0.15), t["on_primary"], 4.5)
    out.update({
        "glass": "1",
        "text-dim": ensure_contrast(t["outline"], t["surface_container_high"], 4.5),
        "card-bg": _rgba(t["surface_container"], 0.62),
        "card-border": _rgba(_blend(t["outline_variant"], t["primary"], 0.35), 0.8),
        "card-shadow": "0 8px 32px rgba(0, 0, 0, 0.35)",
        # The gradients are already soft. Re-capturing them behind every
        # card adds live compositor surfaces to a transparent Qt web view.
        "card-blur": "none",
        "btn-bg": _rgba(ink, 0.05), "btn-border": _rgba(ink, 0.12),
        "btn-hover-bg": _rgba(ink, 0.1), "btn-hover-fg": t["on_surface"],
        "btn-hover-border": _rgba(t["primary"], 0.6), "btn-press-bg": _rgba(t["primary"], 0.25),
        "accent-bg": accent, "accent-fg": t["on_primary"],
        "accent-hover-bg": accent_hover, "accent-hover-fg": t["on_primary"],
        "accent-glow": f"0 0 20px {_rgba(t['primary'], 0.45)}",
        "field-bg": _rgba("#000000", 0.22 if not light else 0.04), "field-border": _rgba(ink, 0.12),
        "field-focus-bg": _rgba("#000000", 0.3 if not light else 0.06), "field-focus-bw": shape["bw"],
        "raised-bg": _rgba(ink, 0.05),
        "scroll": _rgba(ink, 0.14), "scroll-hover": _rgba(t["primary"], 0.7), "scroll-width": "8px",
        "scroll-radius": "10px",
        "orb-1": _rgba(t["primary"], 0.14),
        "orb-2": _rgba(second_color(t), 0.1),
        "orb-3": _rgba(t.get("surface_glow") or t["primary_container"], 0.3),
        "btn-radius": shape["r_lg"], "field-radius": shape["r_lg"],
        "btn-pad": "9px 18px", "field-pad": "10px 14px",
        "header-bg": _rgba(t["surface_container"], 0.62), "rail-bg": _rgba(t["surface_container"], 0.62),
        # Glass, but opaque enough to read over whatever is under it.
        "pop-bg": _rgba(t["surface_container"], 0.9),
        "pop-border": _rgba(_blend(t["outline_variant"], t["primary"], 0.45), 0.9),
        "pop-hover-bg": _rgba(t["primary"], 0.2), "pop-hover-fg": t["on_surface"],
        "pop-shadow": f"0 16px 40px rgba(0, 0, 0, 0.45), 0 0 24px {_rgba(t['primary'], 0.18)}",
        "pop-blur": "none",
        # Glass needs something solid under a dialog, or the page shows through.
        "modal-bg": t["surface"],
        # Sticky headers must cover the rows scrolling underneath the glass.
        "table-head-bg": t["surface_container_high"],
    })
    return out


def _resolve(tokens, shape):
    """Resolve: _resolve_extra_rules' Resolve greys - outlined grey pills,
    near-black square fields, and no filled accent button, since Resolve
    has none. The accent (DaVinci's red) only marks focus of attention.
    The greys follow the palette, so a colour variant or a Custom
    background re-tints them all."""
    c = resolve_colors(tokens)
    pill = shape["r_xl"]
    return {
        # Resolve's window grey, painted by the page itself rather than seen
        # through it: a transparent page showed black where Qt hadn't
        # painted behind it.
        "page-bg": c["window"],
        "page-pad": "0 2px 2px",
        # "dim" is Resolve's disabled grey - unreadable as secondary text on
        # the panel - so secondary text sits between the label and panel.
        "text": c["label"], "text-strong": c["value"], "text-dim": _blend(c["label"], c["panel"], 0.3),
        "card-bg": c["panel"], "card-border": c["divider"], "card-radius": "0px",
        "card-shadow": "none", "card-blur": "none",
        "btn-bg": c["panel"], "btn-fg": c["label"], "btn-border": c["button_border"],
        "btn-radius": pill, "btn-pad": "2px 14px", "btn-weight": "400",
        "btn-hover-bg": c["hover"], "btn-hover-fg": c["bright"], "btn-hover-border": c["button_border_hi"],
        "btn-press-bg": c["field"],
        "accent-bg": c["panel"], "accent-fg": c["bright"], "accent-border": c["button_border_hi"],
        "accent-hover-bg": c["hover"], "accent-hover-fg": c["bright"], "accent-weight": "600",
        "accent-glow": "none",
        "danger-bg": c["panel"], "danger-fg": tokens["danger"], "danger-border": tokens["danger"],
        "danger-hover-bg": c["hover"],
        "field-bg": c["field"], "field-fg": c["value"], "field-border": c["field_border"],
        "field-radius": "2px", "field-pad": "3px 6px",
        "field-focus-border": c["button_border_hi"], "field-focus-bg": c["field"], "field-focus-bw": "1px",
        "scroll": c["scroll"], "scroll-hover": c["scroll_hi"], "scroll-radius": "3px", "scroll-width": "8px",
        "raised-bg": c["header"],
        # Resolve's text is grey on grey; the speaker/link colours still
        # need to clear contrast on the panel, not the window.
        "accent-text": ensure_contrast(c["accent"], c["panel"]),
        "you-text": c["label"], "buddy-text": c["bright"], "buddy-dot": c["accent"],
        "link": c["value"],
        # Resolve's own greys for anything that is only emphasis: bright
        # icons and labels, the lit button edge, label-grey bars, the hover
        # grey behind step numbers. Red stays for focus and selection.
        "emphasis": c["value"], "emphasis-line": c["button_border_hi"],
        "emphasis-fill": c["label"],
        "emphasis-container": c["hover"], "on-emphasis-container": c["value"],
        # Resolve's page bar: grey labels, the active page white on the
        # darker toolbar grey with a red marker down its left edge.
        "header-bg": c["toolbar"], "rail-bg": c["panel"], "shell-border": c["divider"],
        "header-radius": "0px", "rail-radius": "0px",
        "title-fg": c["bright"], "title-size": "15px", "title-weight": "600",
        "nav-fg": c["label"], "nav-size": "13px", "nav-weight": "400", "nav-pad": "8px 14px",
        "nav-radius": "0px", "nav-hover-bg": c["hover"], "nav-hover-fg": c["bright"],
        "nav-active-bg": c["toolbar"], "nav-active-fg": c["bright"], "nav-active-border": "transparent",
        "nav-active-weight": "600", "nav-marker": c["accent"], "nav-heading": c["dim"],
        "pop-bg": c["field"], "pop-border": c["button_border"],
        "pop-hover-bg": c["hover"], "pop-hover-fg": c["bright"],
        "pop-shadow": "0 8px 22px rgba(0, 0, 0, .5)", "pop-blur": "none",
        # No tinted fills: Ask Buddy's question is a raised grey panel.
        "bubble-bg": c["header"], "bubble-border": c["button_border"], "bubble-radius": pill,
        "dot-glow": "none",
    }


def _offworld(tokens, shape):
    """Off-world: a phosphor terminal. One colour does everything - text,
    edges and fills are the primary at different strengths on black glass -
    so it's derived from "primary" alone and a Custom accent works as well
    as the presets. Controls are hollow outlined boxes that light up; the
    type, scanlines and glow are buddy.css's html[data-family="offworld"]."""
    t = tokens
    p, s = t["primary"], t["surface"]
    ink = "#000000" if _is_light(s) else "#FFFFFF"
    card = _blend(s, p, 0.03)
    text = ensure_contrast(_blend(p, s, 0.12), card, 7)
    strong = ensure_contrast(_blend(p, ink, 0.4), card, 9)
    dim = ensure_contrast(_blend(p, s, 0.45), card, 4.5)
    danger = ensure_contrast(t["danger"], s)
    well = _blend(s, "#000000", 0.5)
    glow = f"0 0 14px {_rgba(p, 0.35)}, inset 0 0 12px {_rgba(p, 0.14)}"
    return {
        "page-bg": s,
        "text": text, "text-strong": strong, "text-dim": dim,
        "card-bg": card, "card-border": _rgba(p, 0.3), "card-radius": shape["r_2xl"],
        "card-shadow": f"inset 0 0 28px {_rgba(p, 0.05)}", "card-blur": "none",
        "btn-bg": "transparent", "btn-fg": text, "btn-border": _rgba(p, 0.5),
        "btn-radius": shape["r_xl"], "btn-pad": "6px 14px", "btn-weight": "600",
        "btn-hover-bg": _rgba(p, 0.12), "btn-hover-fg": strong, "btn-hover-border": p,
        "btn-press-bg": _rgba(p, 0.24),
        # The kit's selected menu row: a lit outline over a faint wash.
        "accent-bg": _rgba(p, 0.16), "accent-fg": strong, "accent-border": p,
        "accent-hover-bg": _rgba(p, 0.3), "accent-hover-fg": strong,
        "accent-weight": "700", "accent-glow": glow,
        "danger-bg": _rgba(t["danger"], 0.1), "danger-fg": danger, "danger-border": danger,
        "danger-hover-bg": _rgba(t["danger"], 0.22),
        "field-bg": well, "field-fg": strong, "field-border": _rgba(p, 0.4),
        "field-radius": shape["r_lg"], "field-pad": "6px 10px",
        "field-focus-border": p, "field-focus-bg": well, "field-focus-bw": shape["bw"],
        "scroll": _rgba(p, 0.3), "scroll-hover": p, "scroll-radius": "0px", "scroll-width": "8px",
        "raised-bg": _rgba(p, 0.07),
        "you-text": strong, "buddy-text": text, "buddy-dot": p, "link": strong,
        "second-text": ensure_contrast(_blend(p, ink, 0.55), s),
        "overlay-tint": _rgba(s, 0.82),
        "header-bg": card, "rail-bg": card, "shell-border": _rgba(p, 0.3),
        "header-radius": "0px", "rail-radius": "0px",
        "title-fg": strong, "title-size": "16px", "title-weight": "700",
        "nav-fg": text, "nav-size": "12px", "nav-weight": "600", "nav-pad": "8px 10px",
        "nav-radius": "0px", "nav-hover-bg": _rgba(p, 0.08), "nav-hover-fg": strong,
        "nav-active-bg": _rgba(p, 0.14), "nav-active-fg": strong, "nav-active-border": p,
        "nav-active-weight": "700", "nav-marker": p, "nav-heading": dim,
        "pop-bg": _blend(s, "#000000", 0.3), "pop-border": p,
        "pop-hover-bg": _rgba(p, 0.2), "pop-hover-fg": strong,
        "pop-shadow": f"0 12px 32px rgba(0, 0, 0, .6), 0 0 18px {_rgba(p, 0.2)}", "pop-blur": "none",
        "phosphor-glow": f"0 0 6px {_rgba(p, 0.45)}",
        "scanline": _rgba("#000000", 0.28 if not _is_light(s) else 0.06),
        "screen-glow": _rgba(p, 0.07),
    }


def _desktop(tokens, shape):
    """Desktop: ink-outlined boxes, like the floating windows around it
    (core/desktop_window.py) - every box a 2px ink line and a hard offset
    shadow, fills from the palette's candy colours. Lines and shadows are
    "outline", text "on_surface": one colour on paper, near-black and cream
    on the dark palettes. Text on a coloured fill is whichever of the two
    reads on it. The shadows and halftone are themes/desktop.css."""
    t = tokens
    c = desktop_colors(t)
    line, text, paper = c["ink"], t["on_surface"], t["surface"]
    light = _is_light(paper)

    def on(fill):
        """Text for a coloured fill: the palette's own text when it reads,
        else whichever reads best - a mid-tone fill (a Custom accent) can
        be out of reach of light text and need the dark ink instead."""
        best = ensure_contrast(text, fill, 4.5)
        if contrast_ratio(best, fill) >= 4.5:
            return best
        # ensure_contrast only ever lightens on a fill it counts as dark, so
        # the dark ink is walked toward black here instead.
        dark = t["on_primary"]
        for _ in range(10):
            if contrast_ratio(dark, fill) >= 4.5:
                break
            dark = _blend(dark, "#000000", 0.3)
        return max((best, dark), key=lambda c: contrast_ratio(c, fill))

    danger_fill = _blend(t["danger"], "#FFFFFF" if light else paper, 0.5)
    muted = ensure_contrast(_blend(text, paper, 0.45), t["surface_container"], 4.5)
    lift = "#FFFFFF" if light else text
    out = _material(tokens, shape)
    out.update({
        "page-bg": paper,
        "text": text, "text-strong": text, "text-dim": muted,
        "card-bg": t["surface_container"], "card-border": line, "card-radius": shape["r_2xl"],
        "card-shadow": f"3px 3px 0 {line}",
        "btn-bg": t["surface_container_high"], "btn-fg": on(t["surface_container_high"]), "btn-border": line,
        "btn-radius": shape["r_lg"], "btn-pad": "5px 14px", "btn-weight": "700",
        "btn-hover-bg": t["secondary_container"], "btn-hover-fg": on(t["secondary_container"]),
        "btn-hover-border": line,
        "btn-press-bg": t["primary_container"],
        "btn-shadow": f"2px 2px 0 {line}",
        "accent-bg": t["primary"], "accent-fg": t["on_primary"], "accent-border": line,
        "accent-hover-bg": _blend(t["primary"], "#FFFFFF", 0.2), "accent-hover-fg": t["on_primary"],
        "accent-weight": "800",
        "danger-bg": danger_fill, "danger-fg": on(danger_fill), "danger-border": line,
        "danger-hover-bg": _blend(danger_fill, lift, 0.25),
        "field-bg": t["surface_container_high"], "field-fg": text, "field-border": line,
        "field-radius": shape["r_lg"], "field-pad": "6px 12px",
        "field-focus-border": line, "field-focus-bg": t["surface_container_high"], "field-focus-bw": shape["bw"],
        "scroll": _blend(text, paper, 0.55), "scroll-hover": text, "scroll-radius": "999px", "scroll-width": "10px",
        "raised-bg": t["surface_container_high"],
        "header-bg": t["surface_container_high"], "rail-bg": t["surface_container"], "shell-border": line,
        "header-radius": shape["r_2xl"], "rail-radius": shape["r_2xl"],
        "title-fg": text, "title-size": "18px", "title-weight": "800",
        "nav-fg": text, "nav-size": "13px", "nav-weight": "600", "nav-pad": "7px 12px",
        "nav-radius": shape["r_md"], "nav-hover-bg": t["secondary_container"], "nav-hover-fg": on(t["secondary_container"]),
        "nav-active-bg": t["primary_container"], "nav-active-fg": on(t["primary_container"]), "nav-active-border": line,
        "nav-active-weight": "700", "nav-heading": muted,
        "pop-bg": t["surface_container_high"], "pop-border": line,
        "pop-hover-bg": t["primary_container"], "pop-hover-fg": on(t["primary_container"]),
        "pop-shadow": f"3px 3px 0 {line}",
        "modal-bg": paper,
        "tab-active-bg": t["primary_container"], "tab-active-fg": on(t["primary_container"]), "tab-active-line": line,
        "bubble-bg": t["secondary_container"], "bubble-fg": on(t["secondary_container"]), "bubble-border": line,
        "dot-glow": "none",
        "halftone": _rgba(text, 0.14 if light else 0.1),
        "chrome-title-bg": c["windows"][0],
    })
    return out


# One builder per THEMES[...]["shape"]; a shape missing here gets _material.
_BY_FAMILY = {
    "retro": _retro,
    "saas": _saas,
    "nova": _nova,
    "resolve": _resolve,
    "offworld": _offworld,
    "desktop": _desktop,
}


def web_theme(theme, subtheme, tokens):
    """{"family": ..., "light": bool, "vars": {name: value}} for a theme.

    `tokens` is what the shell's theme_tokens() returns (so a Custom
    palette arrives already derived). Names in "vars" have no leading
    "--"; app/web/buddy.js adds it when it sets them on :root.
    """
    theme, _subtheme = resolve(theme, subtheme)
    shape = get_shape_tokens(theme)
    family = THEMES[theme]["shape"]
    build = _BY_FAMILY.get(family, _material)
    variables = {**_common(tokens, shape), **build(tokens, shape)}
    variables.update(_disabled_button(variables, tokens))
    variables.update(_link_bar(variables, tokens))
    return {"family": family, "light": _is_light(tokens["surface"]), "vars": variables}


# A disabled button's label: readable, but plainly dimmer than an enabled
# one (those sit at 7:1 and up). Resolve's own disabled grey is ~1.6:1 -
# the button read as an empty box.
DISABLED_CONTRAST = 3.2


def _disabled_button(variables, tokens):
    """One disabled look for every theme: no fill, a faded outline so it
    still reads as a button (a borderless one looked like a heading, or like
    an enabled text button beside it), and a label at DISABLED_CONTRAST on
    the card it sits on."""
    card = variables["card-bg"]
    ref = card if card.startswith("#") and len(card) == 7 else tokens["surface_container"]
    dim = variables["text-dim"]
    if not (dim.startswith("#") and len(dim) == 7):
        dim = tokens["outline"]
    fg = ensure_contrast(dim, ref, DISABLED_CONTRAST)
    for amount in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        faded = _blend(dim, ref, amount)
        if contrast_ratio(faded, ref) < DISABLED_CONTRAST:
            break
        fg = faded
    # The enabled outline, a little fainter - never stronger than a button
    # that can be pressed. A translucent one (Nova's glass) stays as it is.
    border = variables["btn-border"]
    if border.startswith("#") and len(border) == 7:
        border = _blend(border, ref, 0.3)
    elif border == "transparent":
        border = _blend(fg, ref, 0.45)
    return {
        "btn-disabled-bg": "transparent",
        "btn-disabled-fg": fg,
        "btn-disabled-border": border,
    }


# How far the link bar steps off the header it would otherwise match, by its
# Shade setting (core/link_bar.py SHADES) - "auto" is lighter on a dark theme
# and darker on a light one. Light themes need smaller steps toward black
# and bigger ones toward white, where there's little room left.
LINK_BAR_STEPS = {
    "lighter": {"dark": 0.14, "light": 0.6},
    "darker": {"dark": 0.4, "light": 0.07},
}
# How much of the accent its Accent tint setting mixes into the bar.
LINK_BAR_TINTS = {"off": 0.0, "subtle": 0.08, "strong": 0.18}


def _solid(value, fallback):
    return value if value.startswith("#") and len(value) == 7 else fallback


def _link_bar(variables, tokens, shade="auto", tint="off", icons="theme"):
    """The link bar (app/web/shell/shell.css): by default a strip a clear
    step off the header, page and cards around it, so it reads as a bar of
    its own - with its links at full strength and an edge to match. The
    bar's own settings (core/link_bar.py style) pick the step's direction
    (or none), a wash of the accent, and what colour its icons are; the
    text is kept readable whatever they choose."""
    header = _solid(variables["header-bg"], tokens["surface_container"])
    light = _is_light(tokens["surface"])
    if shade not in ("lighter", "darker", "match"):
        shade = "darker" if light else "lighter"
    bg = header
    if shade != "match":
        toward = "#FFFFFF" if shade == "lighter" else "#000000"
        bg = _blend(header, toward, LINK_BAR_STEPS[shade]["light" if light else "dark"])
    bg = _blend(bg, tokens["primary"], LINK_BAR_TINTS.get(tint, 0.0))
    ink = "#000000" if _is_light(bg) else "#FFFFFF"
    fg = ensure_contrast(_solid(variables["text-strong"], tokens["on_surface"]), bg, 7)
    if icons == "accent":
        icon = ensure_contrast(tokens["primary"], bg, 3)
    elif icons == "text":
        icon = fg
    else:
        emphasis = _solid(variables["emphasis"], variables["accent-text"])
        icon = ensure_contrast(_solid(emphasis, tokens["primary"]), bg, 3)
    return {
        "linkbar-bg": bg,
        "linkbar-fg": fg,
        "linkbar-border": _blend(bg, ink, 0.22),
        "linkbar-icon": icon,
    }


def link_bar_vars(theme, subtheme, tokens, style):
    """The link bar's variables for its own settings (`style`: core/link_bar.py
    style()), laid over the theme's - core/link_bar_web.py sends them."""
    variables = web_theme(theme, subtheme, tokens)["vars"]
    return _link_bar(variables, tokens, style["shade"], style["tint"], style["icons"])
