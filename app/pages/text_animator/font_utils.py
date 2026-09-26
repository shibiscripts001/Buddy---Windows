"""Enumerates fonts genuinely backed by a .ttf/.otf file on disk, for the Font Styling tab's
font picker.

A font listed by Qt's font enumeration can be missing from DaVinci Resolve/Fusion entirely
(it renders as a black "Font Not Found" clip). Resolve pulls its Font list from installed
system fonts, same as this app - it has no separate bundled font catalog - so the mismatch
comes from elsewhere. The likely cause: Windows can expose a font FAMILY NAME with no
real font file behind it at all - e.g. a "FontSubstitutes" registry alias, or a name registered
by another application without installing an actual .ttf/.otf - which Qt's font enumeration
(and thus a plain QFontComboBox) can still list, but Resolve/Fusion (scanning real font
files) never sees. Filtering to the registry's own record of which display name maps to which
ACTUAL FILE, keeping only .ttf/.otf, should match what Resolve/Fusion can actually use.
"""
import re
import sys
from typing import Dict, Iterable, List, Tuple

# This app's own default font ("Arial Regular"). Centralized here so every place in this
# codebase that falls back to a
# hardcoded default font (subtitle conversion's fallback when no font was specified, the Font
# Styling tab's initial selection, the "Reset to Default" button, the Live Placement Preview's
# own placeholder before real data syncs in) stays in agreement instead of drifting.
DEFAULT_FONT_NAME = "Arial"

# Explicit default Style paired with DEFAULT_FONT_NAME above, so applying "Arial" lands as
# "Arial Regular" rather than "Arial Light"/similar. This app's Media Pool Text+ template
# can carry a residual Style value from whatever was last set on it (e.g. "Light")
# - FusionAnimationEngine.apply_text_style() has an optional Style-PRESERVING mode (so
# switching fonts on a clip a user deliberately Bold/Italic'd directly in Fusion isn't reset),
# but that mode has no way to distinguish "the user chose this on purpose" from "this is just
# whatever the template happened to have" - so ui.py's own Font Styling tab never relies on
# preservation at all. Every font in this app's own picker already encodes its own
# weight/slant directly in its name (e.g. "Arial Bold", "Arial Regular" - see
# build_font_display_map()), so Style is always this exact constant, forced explicitly,
# regardless of what a clip's Style already happens to be - see ui.py's
# _apply_font_style_to_tool().
DEFAULT_FONT_STYLE = "Regular"

# Arial has no Japanese, Chinese, Korean, Thai or Devanagari characters, and
# Text+ doesn't borrow them from another font - such a title shows nothing
# (or boxes). Subtitles in those scripts get the first of these fonts that
# Fusion has (its FontManager - which, unlike the picker's list, includes
# Windows' .ttc fonts such as Yu Gothic), Windows names first, then macOS.
SCRIPT_FONTS = {
    "ja": ("Yu Gothic", "Meiryo", "Noto Sans JP", "MS Gothic", "Hiragino Sans", "Hiragino Kaku Gothic ProN"),
    "zh": ("Microsoft YaHei", "Noto Sans SC", "SimHei", "PingFang SC"),
    "ko": ("Malgun Gothic", "Noto Sans KR", "Apple SD Gothic Neo"),
    "th": ("Leelawadee UI", "Noto Sans Thai", "Tahoma", "Thonburi"),
    "hi": ("Nirmala UI", "Noto Sans Devanagari", "Kohinoor Devanagari"),
}
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_THAI = re.compile(r"[\u0e00-\u0e7f]")
_DEVANAGARI = re.compile(r"[\u0900-\u097f]")


def script_of(texts: Iterable[str]) -> str:
    """"ja", "zh", "ko", "th" or "hi" when the texts need a font Arial isn't;
    "" otherwise. Taken over all of them at once: kana anywhere makes a
    kanji-only line Japanese too."""
    joined = "\n".join(t for t in texts if t)
    if _KANA.search(joined):
        return "ja"
    if _HANGUL.search(joined):
        return "ko"
    if _HAN.search(joined):
        return "zh"
    if _THAI.search(joined):
        return "th"
    if _DEVANAGARI.search(joined):
        return "hi"
    return ""


def font_for_texts(texts: Iterable[str], available=None) -> str:
    """A font that has the texts' characters: "" when the default will do.
    available: the font families Fusion has (None if unknown - then the
    first candidate)."""
    candidates = SCRIPT_FONTS.get(script_of(texts), ())
    if not candidates:
        return ""
    if available is None:
        return candidates[0]
    return next((name for name in candidates if name in available), "")

# Windows registers each installed font face as one value under this key: value name is the
# display name shown in Windows' own Fonts control panel (e.g. "Arial Bold (TrueType)"), value
# data is the backing file (a bare filename for fonts under %WINDIR%\Fonts, or a full path for
# a font installed for the current user only, e.g. via Windows 10+'s per-user font install).
_MACHINE_FONTS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_USER_FONTS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"

# Suffixes Windows appends to the display name to indicate the font technology - stripped so
# the name matches what's actually typed into Fusion's "Font" input (a plain family/face
# string, per the "Cascadia Code" / "Style": "Light" real dump - see
# FusionAnimationEngine.apply_text_style()'s docstring).
_DISPLAY_NAME_SUFFIXES = (
    " (TrueType)",
    " (OpenType)",
    " (TrueType, OpenType)",
    " (OpenType, TrueType)",
)


def _clean_font_display_name(display_name: str) -> str:
    """Strips a known Windows font-technology suffix off a registry display name. Pure/
    testable independently of the registry itself - see _MACHINE_FONTS_KEY's docstring for
    why this stripping is needed."""
    for suffix in _DISPLAY_NAME_SUFFIXES:
        if display_name.endswith(suffix):
            return display_name[: -len(suffix)]
    return display_name


# A font name that doesn't already spell out its own weight/
# slant ("Bold", "Italic", "Light", etc.) must still be applied as the "Regular" style
# explicitly - never left to whatever a Text+ template's Style input already happens to hold
# (otherwise plain "Arial" can land as "Font Not Found: Arial Light" - see
# FusionAnimationEngine.apply_text_style()'s docstring). This set drives
# two things: which names get " Regular" appended for DISPLAY in the Font picker (so every
# entry reads consistently, e.g. "Arial" -> "Arial Regular"), and which words a font name's
# own weight/slant needs to be recognized for live-preview bold/italic rendering (see
# parse_bold_italic_from_name()). Whole-word, case-insensitive matching - a substring check
# would wrongly flag a family like "Blackadder" (contains "black") or "Boldonse" (contains
# "bold"). NOTE: this is a heuristic, not a guarantee - a small number of real fonts use one of
# these words as part of a otherwise-unrelated family name (e.g. "Arial Black" is genuinely
# its own distinct family, not "Arial" + a "Black" style) - if you hit one of those, the
# picker will show/rename it in a slightly odd way, but it's still sent to Fusion as the
# exact literal registry name either way (see build_font_display_map()), so it still renders
# correctly - only the "is this already styled" heuristic is imprecise, not the actual Font
# value sent.
_STYLE_KEYWORDS = {
    "thin", "extralight", "ultralight", "light", "regular", "normal", "book",
    "medium", "semibold", "demibold", "bold", "extrabold", "ultrabold", "heavy",
    "black", "italic", "oblique",
}

_WORD_PATTERN = re.compile(r"[A-Za-z]+")


def _has_style_keyword(name: str) -> bool:
    """True if any WHOLE word in `name` is a recognized weight/slant keyword - see
    _STYLE_KEYWORDS' own docstring for the word-boundary reasoning and known limitations."""
    return any(word.lower() in _STYLE_KEYWORDS for word in _WORD_PATTERN.findall(name))


def build_font_display_map(real_names: Iterable[str]) -> Dict[str, str]:
    """Builds {display_name: real_font_name} for the Font picker: a name with no recognized
    style keyword (see _has_style_keyword()) gets " Regular" appended for display, e.g.
    "Arial" -> "Arial Regular", so every entry in the list
    reads consistently regardless of whether the underlying font's own registry name spells
    out its style. `real_font_name` is always the untouched original string - THAT is what
    gets sent to Fusion as "Font" (the synthetic " Regular" suffix is a display-only label,
    never actually sent - there is no real "Arial Regular" font family)."""
    display_map: Dict[str, str] = {}
    for name in real_names:
        display_name = name if _has_style_keyword(name) else f"{name} Regular"
        display_map[display_name] = name
    return display_map


def parse_bold_italic_from_name(name: str) -> Tuple[bool, bool]:
    """Detects whether a font's own name already spells out Bold and/or Italic, as whole
    words - used by the Font Styling tab's Live Preview (style_preview.py) to apply
    QFont.setBold()/setItalic() explicitly. Needed because Qt's own font matching often can't
    resolve a compound name like "Arial Bold" as a distinct family the way Windows/Fusion's
    own font list does (Qt typically only indexes the base "Arial" family, with Bold/Italic as
    separate style flags within it) - without this, the preview silently falls back to a
    plain/regular rendering regardless of which named variant was actually selected."""
    words = {word.lower() for word in _WORD_PATTERN.findall(name)}
    return ("bold" in words, "italic" in words)


def _is_real_font_file(filename: str) -> bool:
    """True only for a genuine outline font file - .ttf/.otf. Deliberately excludes .ttc
    (TrueType Collection, bundles several faces in one file) and .fon (legacy bitmap fonts)
    - .fon in particular is exactly the kind of non-standard/legacy entry to filter out."""
    lowered = filename.lower()
    return lowered.endswith(".ttf") or lowered.endswith(".otf")


def get_installed_font_family_names() -> List[str]:
    """Returns installed font names actually backed by a .ttf/.otf file, sorted
    case-insensitively. Windows-only (this app's target platform for this feature) - returns
    an empty list on any other OS so callers can fall back to a plain OS-level font list
    there (see ui.py's _init_font_styling_tab(), which falls back to QFontDatabase.families()
    when this returns empty)."""
    if sys.platform != "win32":
        return []

    import winreg

    names = set()
    for hive, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, _MACHINE_FONTS_KEY),
        (winreg.HKEY_CURRENT_USER, _USER_FONTS_KEY),
    ):
        try:
            key = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    display_name, filename, _value_type = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if not isinstance(display_name, str) or not isinstance(filename, str):
                    continue
                if not _is_real_font_file(filename):
                    continue
                names.add(_clean_font_display_name(display_name).strip())
        finally:
            winreg.CloseKey(key)

    return sorted(names, key=str.casefold)

def split_font_name_and_style(full_name: str) -> Tuple[str, str]:
    words = full_name.split()
    style_words = []
    while words and words[-1].lower() in _STYLE_KEYWORDS:
        style_words.insert(0, words.pop())
    if not words:
        return full_name, "Regular"
    family = " ".join(words)
    style = " ".join(style_words) if style_words else "Regular"
    return family, style
