#!/usr/bin/env python3
"""
Settings as data: the fields the Settings window (core/settings_dialog.py,
drawn by app/web/shell/settings/) shows, and the shell's own section -
Appearance and Window. No Qt, so it's unit-tested.

A tool adds its own section by returning fields from
ToolPage.settings_fields() and handling on_setting() / on_settings_action()
(pages/base.py). A field is a dict with a "kind":

    heading   text
    hint      text, [html] (Buddy's own fixed HTML - links), [tone]
    check     key, label, value, [hint], [tooltip]
    select    key, label, value, options [{value, label}], [tooltip],
              [raw] (the option labels are names to show as they are -
              never translated)
    text      key, label, value, [placeholder], [password], [hint],
              [live] (sent as it's typed, not when the field is left),
              [suggest] (a list the field offers), [browse] (an action)
    textarea  key, label, value, [placeholder], [rows], [hint] - several
              lines, saved when the field is left; what's typed is the
              user's, never translated
    number    key, label, value (None = blank), min, max, decimals,
              [placeholder], [suffix]
    slider    key, label, value, min, max, step, default, readouts
              ({value: text} - what's shown beside it), [hint]
    color     key, label, value, [enabled]
    buttons   items [{label, action, [kind], [tooltip]}]
    line
    info      label, text (a value shown, not edited)
    status    label, text, tone (ok / warn / ""), [hint] - a fact with a
              chip ("Verified", "Sends to Anthropic")
    link      label, page, [hint] - opens another page of the window
    models    rows, [filters] - a list of models (see models())
    storage   total, parts [{label, bytes, tone}], [path] - disk used, as a bar
    progress  text, value (0-100, None = still working), [stop] (an action)

Any field may carry "error" (shown under it) and "indent". Everything a
field shows goes into the page as text; only a hint's "html" is HTML, and
that's only ever Buddy's own.

The window is pages on a rail (core/settings_dialog.py): a page is a dict
{id, group, title, [subtitle], fields} - group one of GROUPS. A heading
starts a card of its own; a line ends one.
"""

import re

from core.app_version import buddy_version
from core.i18n import LANGUAGES, canonical_language, language_label
from core.theme import (
    DEFAULT_SIDE_PANE_TINT,
    DEFAULT_THEME,
    SIDE_PANE_TINTS,
    custom_defaults,
    default_subtheme,
    derive_panel,
    list_subthemes,
    list_themes,
    theme_label,
)

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def heading(text):
    return {"kind": "heading", "text": text}


def hint(text, html=None, tone=None, indent=False):
    return {"kind": "hint", "text": text, "html": html, "tone": tone, "indent": indent}


def check(key, label, value, hint_text=None, tooltip=None, indent=False):
    return {"kind": "check", "key": key, "label": label, "value": bool(value), "hint": hint_text,
            "tooltip": tooltip, "indent": indent}


def select(key, label, value, options, tooltip=None, indent=False, raw=False):
    """options: [(value, label)] or [value] (shown as it is). raw: the
    labels are names (a person's, a language's own) - never translated."""
    opts = [{"value": o[0], "label": o[1]} if isinstance(o, (tuple, list)) else {"value": o, "label": str(o)}
            for o in options]
    return {"kind": "select", "key": key, "label": label, "value": value, "options": opts, "tooltip": tooltip,
            "indent": indent, "raw": raw}


def text(key, label, value, placeholder="", password=False, hint_text=None, live=False, suggest=None,
         browse=None, error=None):
    return {"kind": "text", "key": key, "label": label, "value": value or "", "placeholder": placeholder,
            "password": password, "hint": hint_text, "live": live, "suggest": suggest, "browse": browse,
            "error": error}


def textarea(key, label, value, placeholder="", rows=8, hint_text=None, error=None):
    return {"kind": "textarea", "key": key, "label": label, "value": value or "", "placeholder": placeholder,
            "rows": rows, "hint": hint_text, "error": error}


def number(key, label, value, minimum, maximum, decimals=0, placeholder="", suffix=""):
    return {"kind": "number", "key": key, "label": label, "value": value, "min": minimum, "max": maximum,
            "decimals": decimals, "placeholder": placeholder, "suffix": suffix}


def slider(key, label, value, minimum, maximum, default, readouts, step=1, hint_text=None):
    return {"kind": "slider", "key": key, "label": label, "value": value, "min": minimum, "max": maximum,
            "step": step, "default": default, "readouts": {str(k): v for k, v in readouts.items()},
            "hint": hint_text}


def color(key, label, value, enabled=True):
    return {"kind": "color", "key": key, "label": label, "value": value, "enabled": enabled}


def buttons(*items):
    """items: (label, action) or (label, action, {"kind", "tooltip"})."""
    out = []
    for item in items:
        label, action, *rest = item
        out.append({"label": label, "action": action, **(rest[0] if rest else {})})
    return {"kind": "buttons", "items": out}


def line():
    return {"kind": "line"}


def info(label, value, raw=False):
    """raw: the value is a name to show as it is (a project's), never
    translated."""
    return {"kind": "info", "label": label, "text": value, "raw": raw}


def status(label, value, tone="", hint_text=None, raw=False):
    """A fact with a chip: tone "ok" (green), "warn" (red outline) or ""."""
    return {"kind": "status", "label": label, "text": value, "tone": tone, "hint": hint_text, "raw": raw}


def link(label, page, hint_text=None):
    """A button that opens another page of Settings, by its id."""
    return {"kind": "link", "label": label, "page": page, "hint": hint_text}


def models(rows, filters=False):
    """A list of models, one row each:

        label      its name; raw: shown as it is (a model id), never translated
        sub        what it is and what uses it, in parts shown with dots
                   between: ["Transcription", "Subtitles, Dailies"]
        size       text ("3.1 GB"), or ""
        chip       {text, tone, [tip]} - Verified, Not verified, Cloud...
        where      "local", "cloud" or "missing" - what the filters go by
        note       a line under it (where it is)
        actions    [{label, action | page, [kind], [disabled], [tip]}] -
                   an action goes to the row's owner (a tool id), a page
                   opens that page
        owner      whose on_settings_action gets the row's actions

    filters: All / On this PC / Cloud / Not downloaded above the list."""
    return {"kind": "models", "rows": list(rows), "filters": filters}


def storage(total, parts, path=""):
    """Disk used, as one bar: total is the text over it, parts
    [{label, size, bytes, tone}] its pieces (size: the bytes as text)."""
    return {"kind": "storage", "total": total, "parts": [p for p in parts if p.get("bytes")], "path": path}


def progress(text, value=None, stop=None):
    """Something running: value 0-100, or None while there's no telling.
    stop: the action that stops it."""
    return {"kind": "progress", "text": text, "value": value, "stop": stop}


def parse_number(value, minimum, maximum):
    """A number field's text -> a float in range, or None if it isn't one
    (blank is None too)."""
    try:
        number_value = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if number_value != number_value or not minimum <= number_value <= maximum:
        return None
    return number_value


# ---------------------------------------------------------------- shell --

COLOR_KEYS = {"accent_color": "accent", "background_color": "background", "panel_color": "panel"}


def custom_color(shared, which):
    """The active theme's own custom colour, defaulting to that theme's
    palette. The panel shows the derived colour until one is picked, so the
    swatch is never blank and the picker opens on the shade in use."""
    theme = shared.get("theme", DEFAULT_THEME)
    accent_default, bg_default = custom_defaults(theme)
    if which == "accent":
        return shared.get(f"accent_{theme}", accent_default)
    background = shared.get(f"background_{theme}", bg_default)
    if which == "background":
        return background
    return shared.get(f"panel_{theme}", "") or derive_panel(background)


# The rail's groups, top to bottom; "end" sits at the foot of the rail.
GROUPS = [
    {"id": "general", "label": "General", "icon": "sliders"},
    {"id": "look", "label": "Look", "icon": "palette"},
    {"id": "ai", "label": "AI", "icon": "spark"},
    {"id": "tools", "label": "Tools", "icon": "tool"},
    {"id": "about", "label": "About", "icon": "info", "end": True},
]
GROUP_IDS = [g["id"] for g in GROUPS]


def page(page_id, group, title, fields, subtitle=""):
    return {"id": page_id, "group": group, "title": title, "subtitle": subtitle,
            "fields": [f for f in fields if f]}


def shell_pages(shared, autostart, updates=None):
    """The shell's own pages: General, Window, Appearance and About.
    autostart: True/False, or None if Windows' startup settings couldn't be
    read. updates: the shell's core/updater.UpdateChecker, or None (then
    About has no Updates)."""
    autostart_field = check(
        "autostart", "Start Buddy automatically when Resolve starts", bool(autostart),
        tooltip="Registers a small background helper that watches for DaVinci Resolve launching and starts "
                "Buddy itself the moment it does – so background tools like Time Tracker are already running "
                "once you're in Resolve, instead of needing a trip to Workspace > Scripts every time.")
    if autostart is None:
        autostart_field.update(disabled=True, tooltip="Could not read Windows startup settings.")
    return [
        page("general", "general", "General", [
            *language_fields(shared),
            heading("Startup"),
            autostart_field,
            check("keep_running_in_tray", "Keep running in tray when window is closed",
                  shared.get("keep_running_in_tray", True),
                  tooltip="When checked, closing the window (the [X] button) minimizes Buddy to the system tray "
                          "instead of quitting – background tools like Time Tracker keep running. When unchecked, "
                          "closing the window quits Buddy normally."),
            heading("Announcements"),
            check("announcements_enabled", "Show announcements from Buddy",
                  shared.get("announcements_enabled", True),
                  hint_text="Once a day Buddy checks for news (updates, known issues) and shows a small glowing dot "
                            "next to \"Buddy\" when there's something new. Nothing about you or your projects is "
                            "sent. Untick to stop checking."),
        ], "Language, starting up and news from Buddy."),
        page("window", "general", "Window", [
            heading("Window"),
            check("stay_on_top", "Keep Buddy on top of Resolve", shared.get("stay_on_top", False)),
            select("split_tint", "Second pane in dual view", shared.get("split_tint", DEFAULT_SIDE_PANE_TINT),
                   list(SIDE_PANE_TINTS.items()),
                   tooltip="Tints the tool on the right while dual view is on, so it's easy to tell which side is "
                           "which."),
            heading("Sidebar"),
            hint("Reorder, show or hide the tools in the sidebar, and add or rename the dividers between them."),
            buttons(("Organize sidebar…", "organize")),
        ], "How Buddy sits beside Resolve, and its sidebar."),
        page("appearance", "look", "Appearance", appearance_fields(shared), "Changes show straight away."),
        page("about", "about", "About Buddy", [
            heading("Buddy"),
            info("Version", buddy_version(), raw=True),
            *update_fields(shared, updates),
        ]),
    ]


def appearance_fields(shared):
    theme = shared.get("theme", DEFAULT_THEME)
    subthemes = list_subthemes(theme)
    subtheme = shared.get("subtheme")
    if subtheme not in subthemes:
        subtheme = default_subtheme(theme)
    custom = subtheme == "Custom"
    return [
        heading("Theme"),
        select("theme", "Theme", theme, [(key, theme_label(key)) for key in list_themes()]),
        select("subtheme", "Subtheme", subtheme, subthemes),
        heading("Colours"),
        color("accent_color", "Accent", custom_color(shared, "accent"), custom),
        color("background_color", "Background", custom_color(shared, "background"), custom),
        color("panel_color", "Panels", custom_color(shared, "panel"), custom),
        hint("Pick the Custom subtheme to choose your own colours.") if not custom else None,
        buttons(("Reset to default", "reset_theme",
                 {"tooltip": "The default theme, and every theme's own colours cleared."})),
    ]


def shell_fields(shared, autostart, updates=None):
    """Every field of the shell's pages, as one list."""
    return [f for p in shell_pages(shared, autostart, updates) for f in p["fields"]]


def update_fields(shared, updates):
    """Updates: the once-a-day check, checking now, and rolling back."""
    if updates is None:
        return []
    if not updates.supported:
        return [heading("Updates"),
                hint("This Buddy runs from its source folder, so it isn't updated from here.")]
    previous = updates.previous()
    actions = [("Check for updates now", "check_updates",
                {"tooltip": f"You have Buddy {updates.current}. Looks for a newer one on GitHub now."})]
    if previous:
        actions.append((f"Roll back to {previous}", "roll_back_update",
                        {"tooltip": f"Puts back Buddy {previous}, the version the last update replaced."}))
    return [
        heading("Updates"),
        check("updates_enabled", "Check for Buddy updates", shared.get("updates_enabled", True),
              hint_text="Once a day Buddy looks for a newer version on GitHub and shows an Update button in the "
                        "header - it's only installed when you say so. Nothing about you or your projects is "
                        "sent."),
        buttons(*actions),
    ]


def language_fields(shared):
    """The Language dropdown, first on the General page. Each language is
    shown in its own name."""
    language = canonical_language(shared.get("language", LANGUAGES[0]))
    return [
        heading("Language"),
        select("language", "Language", language if language in LANGUAGES else LANGUAGES[0],
               [(key, language_label(key)) for key in LANGUAGES], raw=True,
               tooltip="The language for all of Buddy. What you type yourself is left as it is."),
    ]


def apply_shell(shared, key, value):
    """Stores one shell setting. Returns what it affects - "theme",
    "window", "announcements", "updates", "autostart", "tray", "language" - or None if
    the value isn't one it takes. Doesn't save; the caller does."""
    if key == "theme":
        if value not in list_themes():
            return None
        # A subtheme belongs to one theme: switching lands on the new one's default.
        shared["theme"] = value
        shared["subtheme"] = default_subtheme(value)
        return "theme"
    if key == "subtheme":
        if value not in list_subthemes(shared.get("theme", DEFAULT_THEME)):
            return None
        shared["subtheme"] = value
        return "theme"
    if key in COLOR_KEYS:
        if not isinstance(value, str) or not _HEX.match(value):
            return None
        theme = shared.get("theme", DEFAULT_THEME)
        shared[f"{COLOR_KEYS[key]}_{theme}"] = value.upper()
        return "theme"
    if key in ("stay_on_top", "keep_running_in_tray"):
        shared[key] = bool(value)
        return "window" if key == "stay_on_top" else "tray"
    if key == "split_tint":
        if value not in SIDE_PANE_TINTS:
            return None
        shared["split_tint"] = value
        return "window"
    if key == "language":
        value = canonical_language(value)
        if value not in LANGUAGES:
            return None
        shared["language"] = value
        return "language"
    if key == "announcements_enabled":
        return "announcements"
    if key == "updates_enabled":
        return "updates"
    if key == "autostart":
        return "autostart"
    return None


def reset_theme(shared):
    """Settings' Reset to Default: the default theme, and every theme's
    custom colours cleared - reset means reset, not just the one on show."""
    shared["theme"] = DEFAULT_THEME
    shared["subtheme"] = default_subtheme(DEFAULT_THEME)
    values = getattr(shared, "values", shared)
    for key in [k for k in list(values) if k.startswith(("accent_", "background_", "panel_"))]:
        del values[key]
