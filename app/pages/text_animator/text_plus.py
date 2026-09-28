#!/usr/bin/env python3
"""
Text+ tools - font styling, placement, word layouts and animation presets for the
Text+ clips on the open timeline. Transcribe's page hosts them as four of its tabs
(Font Styling, Timeline Layout, Timeline Animation, Custom Animation), right after
Subtitle Conversion, which makes the Text+ in the first place. The view is
transcribe/web/textplus.js, the placement canvases transcribe/web/canvas.js; this
package keeps the engine (subtitle_engine.py, animation_engine.py, canvas_math.py...).
It was the Text Animator page; the Animation page (page.py) now only holds an empty
Text+ tab. Settings are still saved under the tool id "text_animator".

TextPlusTools is not a page: it lives inside one (see __init__). Every Resolve/Fusion
call for these tabs is here; the controls' values are read from options.Options (the
settings bucket).

The placement canvases (Timeline Layout, Custom Animation) show every Text+ clip under
the playhead at its real position, font, size and colour, polled every 500 ms while the
page and one of those tabs are on screen - Resolve has no change notifications. How big
each clip's text draws, and where around its Center, is measured here (canvas_math.text_box,
from the font files Resolve's FontManager names) and sent as fractions of the frame; the
page draws, drags, resizes and snaps locally and reports each finished edit - an edit
only reaches Resolve on release, never mid-drag. Every edit is undoable (Ctrl+Z,
Ctrl+Shift+Z): positions, sizes, word layouts and animation presets.

Resolve access goes through host.ensure_connected(); while Resolve is unreachable the
poll retries only every _RECONNECT_COOLDOWN_SECONDS - a failed connect spawns a probe
subprocess (core/resolve_bridge.py). Long actions run under host.set_busy().

Also: a canvas that fills the page at the timeline's own aspect ratio, arrow-key
nudging, a live font-style preview, the colour picker, track choices limited to the
timeline's tracks, and remembered grid/safe-zone/snapping settings.

Protocol (each name "tp_"-prefixed on the wire; toast and alert go out unprefixed, as
the page's own):
    to the view    state, fonts, options, overlay, canvas, history, alert, toast
    from the view  refresh, set, overlay, reset_font, apply_style, move, group_move,
                   resize, bounding, apply_position, apply_bounding, apply_layout,
                   apply_animation, remove_animations, undo, redo, dump
"""

import re
import time
import traceback
from typing import Any, Dict, NamedTuple, Optional, Tuple

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QFontDatabase

from core.resolve_bridge import ResolveConnectionError

from . import canvas_math, overlays
from .animation_engine import FusionAnimationEngine
from .canvas_math import (
    cap_height_fraction,
    compute_auto_spaced_row,
    compute_bounding_fit_size,
    compute_large_word_layout,
)
from .coordinate_translator import CoordinateTranslator
from .diagnostics import dump_selected_node_settings
from .font_utils import (
    DEFAULT_FONT_NAME,
    DEFAULT_FONT_STYLE,
    build_font_display_map,
    get_installed_font_family_names,
    parse_bold_italic_from_name,
    split_font_name_and_style,
)
from .layout_presets import LAYOUT_PRESETS, compute_vertical_stack_centers, group_overlapping_clips
from .live_preview import get_active_text_plus_items, parse_point
from .options import Options
from .subtitle_engine import get_timeline_resolution

# How often the live preview polls DaVinci's playhead/timeline state (milliseconds).
# DaVinci's scripting API has no push/callback mechanism, so this is the only way to keep
# the preview in sync as the user scrubs.
LIVE_PREVIEW_POLL_INTERVAL_MS = 500
_RECONNECT_COOLDOWN_SECONDS = 10
MAX_LIVE_PREVIEW_UNDO_HISTORY = 50
# Starting Size "Side-by-Side" tries before shrinking to fit.
AUTO_SPACE_WORDS_DEFAULT_SIZE = 0.08

# The host page's tabs these tools draw: tab id -> name.
TABS = {
    "style": "Font Styling",
    "layout": "Timeline Layout",
    "animation": "Timeline Animation",
    "words": "Custom Animation",
}
_CANVAS_TABS = ("layout", "words")
_LIVE_TABS = _CANVAS_TABS + ("style",)   # polled for the Text+ under the playhead
# 4 px of box padding at a 640 px reference width, as a fraction of frame width.
_BOX_PADDING = 4 / 640
_SAMPLE_MAX_LINES = 6      # the style preview's text: a clip's own, up to this
_SAMPLE_MAX_CHARS = 400
# What an Apply says when its "Apply to" holds no clips.
_EMPTY_SCOPE = {
    "timeline": "No clips on the timeline",
    "selected": "No clips selected on the timeline",
    "playhead": "No Text+ clips under the playhead",
    "track": "Video track {track} is empty",
}
# A log line that went wrong, for the activity log's colour.
_BAD_LOG = re.compile(r"\[Error|Exception|Warning|Failed|Could not")


class _LivePreviewAction(NamedTuple):
    """One undoable edit. kind is "position" | "size" | "animation" | "batch"."""

    clip: Any
    kind: str
    old_value: Any
    new_value: Any
    clip_key: Optional[tuple] = None


def _css_font(name: str) -> dict:
    """A Fusion font name ("Open Sans Semibold") as CSS: family, weight, italic."""
    family, style = split_font_name_and_style(name or DEFAULT_FONT_NAME)
    s = style.lower()
    weight = 400
    for words, value in ((("thin",), 100), (("extralight", "ultralight"), 200), (("light",), 300),
                         (("medium",), 500), (("semibold", "demibold"), 600), (("bold",), 700),
                         (("extrabold", "ultrabold"), 800), (("black", "heavy"), 900)):
        if any(w in s.replace(" ", "") for w in words):
            weight = value
    bold, italic = parse_bold_italic_from_name(name or "")
    if bold and weight < 700:
        weight = 700
    return {"family": family, "weight": weight, "italic": italic or "oblique" in s}


class _ScopeUnavailable(Exception):
    """An "Apply to" choice this Resolve can't do - the message says why."""


class TextPlusTools(QObject):
    """The Text+ tabs, inside a page that provides: host, emit(name, payload) to its
    view, isVisible(), tab (the tab on screen) and _add_log(text, kind). The page
    sends its view's "tp_<name>" messages to on_<name> here, calls web_ready(),
    on_shown() and on_app_quitting() from its own, and tab_shown() when one of TABS
    comes on screen."""

    def __init__(self, page):
        # A child of its page (a QObject in Buddy), so its poll timer goes when the page does.
        super().__init__(page if isinstance(page, QObject) else None)
        self.page = page
        self._live_preview_undo_stack: list = []
        self._live_preview_redo_stack: list = []
        self._anim_preset_by_clip_key: Dict[Any, Optional[str]] = {}
        self.current_fusion_pos: Tuple[float, float] = (0.5, 0.5)
        self._no_connection = False
        self._last_connect_attempt = 0.0
        self.opts = Options(self.host.tool_settings("text_animator", {}))
        self.tracks = {"video": 0, "subtitle": 0}
        self.timeline_name = ""
        self.resolution = (1920, 1080)
        self.bounding = {"on": False, "left": 0.15, "right": 0.85}
        # The clips under the playhead, per canvas tab: id -> ActiveTextItem.
        self._items: Dict[str, Dict[str, Any]] = {tab: {} for tab in _CANVAS_TABS}
        self._canvas_sig = {tab: None for tab in _CANVAS_TABS}
        self._sample = ""
        self._fonts_read = False     # Resolve's font files handed to canvas_math (see _read_font_files)

        real_font_names = get_installed_font_family_names() or sorted(QFontDatabase.families(), key=str.casefold)
        self._font_display_to_real = build_font_display_map(real_font_names)

        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(LIVE_PREVIEW_POLL_INTERVAL_MS)
        self._preview_timer.timeout.connect(self._refresh_live_preview)
        self._preview_timer.start()

    @property
    def host(self):
        return self.page.host

    @property
    def tab(self) -> str:
        return self.page.tab

    def emit(self, name, payload=None):
        """To the view: the Text+ script's messages are "tp_"-prefixed; a toast or an
        alert is the page's own."""
        self.page.emit(name if name in ("toast", "alert") else f"tp_{name}", payload)

    def web_ready(self):
        self.emit("fonts", sorted(self._font_display_to_real, key=str.casefold))
        self._push_state()
        self._push_options()
        self.emit("overlay", self._overlay_view())
        for tab in _CANVAS_TABS:
            self._canvas_sig[tab] = None
            self._push_canvas(tab)
        self._push_history()

    def on_shown(self):
        """Sync tracks and canvas size from the current timeline, and refresh the live
        preview straight away."""
        self._sync_timeline()
        self._refresh_live_preview()

    def on_app_quitting(self):
        self._preview_timer.stop()

    # ------------------------------------------------------------ Resolve access --

    def _resolve(self, quiet: bool = False) -> Optional[Any]:
        """The page's single Resolve entry point. Logs failures and returns None - but
        not for a quiet (background) call: being offline is Buddy's header's to say,
        and the log is for what the user asked for."""
        try:
            resolve = self.host.ensure_connected().resolve
            self._no_connection = False
            return resolve
        except ResolveConnectionError as exc:
            if not quiet:
                self.log(f"[Error] {exc}")
            self._no_connection = True
            self._last_connect_attempt = time.monotonic()
            return None

    def _get_current_timeline(self, resolve: Any) -> Optional[Any]:
        try:
            pm = resolve.GetProjectManager()
            proj = pm.GetCurrentProject() if pm is not None else None
            return proj.GetCurrentTimeline() if proj is not None else None
        except Exception:
            return None

    def _timeline_or_log(self, resolve):
        """The open timeline, or None after saying which part is missing."""
        pm = resolve.GetProjectManager()
        if pm is None:
            self.log("[Error] Unable to get ProjectManager.")
            return None
        proj = pm.GetCurrentProject()
        if proj is None:
            self.log("[Error] No active project found.")
            return None
        timeline = proj.GetCurrentTimeline()
        if timeline is None:
            self.log("[Error] No active timeline found.")
            return None
        return timeline

    def log(self, msg: str):
        """Into the page's activity log."""
        text = str(msg)
        self.page._add_log(text, "error" if _BAD_LOG.search(text) else "info")

    # ------------------------------------------------------------------ pushes --

    def _push_state(self):
        self.emit("state", {
            "timeline": self.timeline_name,
            "resolution": list(self.resolution),
            "tracks": self.tracks,
            "bounding": self.bounding,
            "sample": self._sample,
        })

    def _push_options(self):
        view = self.opts.view()
        view["font_display"] = self._font_display_for_real_name(self.opts.font_name) or self.opts.font_name
        view["font_css"] = _css_font(self.opts.font_name)
        # Ascent + descent and line step in ems, as Text+ measures them (canvas_math) - the
        # browser's own figures can be the Windows metrics, which aren't always the same.
        ascent, descent, gap = canvas_math.font_vertical_metrics(view["font_css"]["family"])
        view["font_css"].update(height=ascent + descent, line=ascent + descent + gap)
        self.emit("options", view)

    def _overlay_view(self):
        view = self.opts.overlay_view()
        view.update({
            "grid_types": overlays.GRID_TYPES, "safe_types": overlays.SAFE_ZONE_TYPES,
            "fractions": overlays.GRID_FRACTIONS, "safe_rects": overlays.SAFE_ZONE_RECTS,
            "default_spacing": canvas_math.standard_grid_spacing(*self.resolution),
        })
        return view

    def _push_history(self):
        self.emit("history", {"undo": len(self._live_preview_undo_stack),
                              "redo": len(self._live_preview_redo_stack)})

    @staticmethod
    def _item_id(item) -> str:
        track, name, start, end = item.key
        return f"{track}|{start}|{end}|{name}"

    def _canvas_payload(self, items):
        out = []
        for item in items:
            center = item.center if item.center is not None else (0.5, 0.5)
            out.append({
                "id": self._item_id(item),
                "track": item.track_index,
                "text": item.text,
                "font": _css_font(item.font_name),
                "color": "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in item.color),
                "cx": center[0], "cy": 1.0 - center[1],
                "size": item.font_size,
                "box": canvas_math.text_box(item.font_name, item.text, item.font_size),
                "known": item.center is not None,
            })
        return out

    def _push_canvas(self, tab):
        items = list(self._items[tab].values())
        payload = self._canvas_payload(items)
        signature = repr((payload, self.resolution))
        if signature == self._canvas_sig[tab]:
            return
        self._canvas_sig[tab] = signature
        self.emit("canvas", {"tab": tab, "resolution": list(self.resolution), "items": payload,
                             "padding": _BOX_PADDING})

    # ------------------------------------------------------------ defaults sync --

    def _read_font_files(self, resolve):
        """Once per connection (and on Refresh): which file Resolve renders each font from,
        so the canvas and the preview measure what Resolve draws - see canvas_math."""
        if self._fonts_read:
            return
        self._fonts_read = True
        try:
            changed = canvas_math.set_font_files(resolve.Fusion().FontManager.GetFontList())
        except Exception:
            return          # Qt's own metrics, then: right for most fonts
        if changed:
            for tab in _CANVAS_TABS:
                self._canvas_sig[tab] = None
                self._push_canvas(tab)
            self._push_options()

    def _sync_timeline(self):
        """Syncs the timeline's name, tracks and resolution."""
        resolve = self._resolve(quiet=True)
        if resolve is None:
            return self._push_state()
        self._read_font_files(resolve)
        timeline = self._get_current_timeline(resolve)
        if timeline is None:
            return self._push_state()
        try:
            self._sync_resolution(timeline)
            self.timeline_name = timeline.GetName() if callable(getattr(timeline, "GetName", None)) else ""
            self.tracks = {"video": int(timeline.GetTrackCount("video") or 0),
                           "subtitle": int(timeline.GetTrackCount("subtitle") or 0)}
        except Exception:
            pass
        self._push_state()

    def _sync_resolution(self, timeline) -> bool:
        """Takes the timeline's resolution for the canvases and the style preview; True
        if it changed. Also run on every poll: the page may first show before Resolve
        is reachable, and the user can switch to a timeline of another shape. Only a
        real answer counts: get_timeline_resolution's 1920x1080 fallback for a read
        that failed would flip a vertical preview back to 16:9 on one bad poll."""
        try:
            width = int(timeline.GetSetting("timelineResolutionWidth") or 0)
            height = int(timeline.GetSetting("timelineResolutionHeight") or 0)
        except Exception:
            return False
        if width <= 0 or height <= 0 or (width, height) == self.resolution:
            return False
        self.resolution = (width, height)
        self.log(f"[Placement Canvas] Sized to match timeline resolution {width}x{height}.")
        self.emit("overlay", self._overlay_view())
        return True

    # ------------------------------------------------------------ live preview --

    def _refresh_live_preview(self, force=False):
        """Polling tick. Only calls into Resolve while this page is the visible one AND a
        tab showing the playhead's Text+ is active (a canvas, or the style preview's
        text); reconnect attempts are throttled."""
        if not force and not self.page.isVisible():
            return
        if self.tab not in _LIVE_TABS:
            return
        if self._no_connection and (time.monotonic() - self._last_connect_attempt) < _RECONNECT_COOLDOWN_SECONDS:
            return
        resolve = self._resolve(quiet=True)
        if resolve is None:
            return
        self._read_font_files(resolve)
        timeline = self._get_current_timeline(resolve)
        if timeline is None:
            return
        try:
            if self._sync_resolution(timeline):
                self._push_state()
        except Exception:
            pass
        try:
            items = get_active_text_plus_items(timeline, self._get_fusion_comp, self._find_text_tool, log=self.log)
        except Exception as err:
            self.log(f"[Error refreshing live preview]: {err}")
            return
        self._update_sample(items)
        if self.tab in _CANVAS_TABS:
            self._items[self.tab] = {self._item_id(item): item for item in items}
            self._push_canvas(self.tab)

    def _update_sample(self, items):
        """The style preview's text: the playhead's Text+, every line as Resolve draws
        them - capped only for sanity, not a cut a subtitle would hit. With nothing
        under the playhead the last one stays, rather than flicking back to a stand-in."""
        if not items or not items[0].text:
            return
        sample = "\n".join(items[0].text.split("\n")[:_SAMPLE_MAX_LINES])[:_SAMPLE_MAX_CHARS]
        if sample != self._sample:
            self._sample = sample
            self._push_state()

    def _clip_for(self, tab, item_id):
        item = self._items.get(tab, {}).get(item_id)
        return item.clip if item is not None else None

    def on_refresh(self, _payload=None):
        self._no_connection = False
        self._fonts_read = False     # a font installed since is found
        self._sync_timeline()
        self._refresh_live_preview(force=True)

    def on_move(self, payload):
        """One box dragged (or nudged) and let go: its new centre, top-left-origin fractions."""
        payload = payload or {}
        clip = self._clip_for(payload.get("tab"), payload.get("id"))
        if clip is None:
            return
        fusion_x, fusion_y = CoordinateTranslator.pyside_to_fusion(float(payload["cx"]), float(payload["cy"]), 1, 1)
        self._on_live_item_position_updated(clip, fusion_x, fusion_y)
        self._refresh_live_preview(force=True)

    def on_group_move(self, payload):
        payload = payload or {}
        moves = []
        for move in payload.get("moves") or []:
            clip = self._clip_for(payload.get("tab"), move.get("id"))
            if clip is not None:
                fx, fy = CoordinateTranslator.pyside_to_fusion(float(move["cx"]), float(move["cy"]), 1, 1)
                moves.append((clip, fx, fy))
        if moves:
            self._on_live_group_position_updated(moves)
            self._refresh_live_preview(force=True)

    def on_resize(self, payload):
        payload = payload or {}
        clip = self._clip_for(payload.get("tab"), payload.get("id"))
        size = payload.get("size")
        if clip is None or not isinstance(size, (int, float)):
            return
        self._on_live_item_size_changed(clip, max(0.005, float(size)))
        self._refresh_live_preview(force=True)

    def _on_live_item_position_updated(self, clip: Any, fusion_x: float, fusion_y: float):
        self.current_fusion_pos = (fusion_x, fusion_y)
        comp = self._get_fusion_comp(clip)
        text_tool = self._find_text_tool(comp) if comp is not None else None
        if text_tool is None:
            self.log("[Live Position] Could not re-locate the Text+ tool for the moved clip.")
            return
        try:
            parsed_old_center = parse_point(text_tool.GetInput("Center"))
            old_center = list(parsed_old_center) if parsed_old_center is not None else None
        except Exception:
            old_center = None
        try:
            text_tool.SetInput("Center", [fusion_x, fusion_y])
            self.log(f"[Live Position] Updated Center = ({fusion_x:.4f}, {fusion_y:.4f}).")
            self._record_live_preview_action(clip, "position", old_center, [fusion_x, fusion_y])
        except Exception as err:
            self.log(f"[Live Position] Center SetInput Exception: {err}")

    def _on_live_item_size_changed(self, clip: Any, new_font_size: float):
        comp = self._get_fusion_comp(clip)
        text_tool = self._find_text_tool(comp) if comp is not None else None
        if text_tool is None:
            self.log("[Live Resize] Could not re-locate the Text+ tool for the resized clip.")
            return
        try:
            old_size = text_tool.GetInput("Size")
            old_size = float(old_size) if isinstance(old_size, (int, float)) else None
        except Exception:
            old_size = None
        try:
            text_tool.SetInput("Size", new_font_size)
            self.log(f"[Live Resize] Updated Size = {new_font_size:.4f}.")
            self._record_live_preview_action(clip, "size", old_size, new_font_size)
        except Exception as err:
            self.log(f"[Live Resize] Size SetInput Exception: {err}")

    def _on_live_group_position_updated(self, moves: list):
        sub_actions = []
        updated = 0
        for clip, fusion_x, fusion_y in moves:
            comp = self._get_fusion_comp(clip)
            text_tool = self._find_text_tool(comp) if comp is not None else None
            if text_tool is None:
                self.log("[Live Position] Could not re-locate the Text+ tool for one moved word – skipped.")
                continue
            try:
                parsed_old_center = parse_point(text_tool.GetInput("Center"))
                old_center = list(parsed_old_center) if parsed_old_center is not None else None
            except Exception:
                old_center = None
            try:
                text_tool.SetInput("Center", [fusion_x, fusion_y])
                updated += 1
                if old_center is not None:
                    sub_actions.append(_LivePreviewAction(clip=clip, kind="position", old_value=old_center,
                                                          new_value=[fusion_x, fusion_y]))
            except Exception as err:
                self.log(f"[Live Position] Center SetInput Exception: {err}")
        self.log(f"[Live Position] Moved {updated}/{len(moves)} word(s) together.")
        self._record_batch_action(sub_actions)

    # ------------------------------------------------------------ page state --

    def tab_shown(self):
        """The page switched to one of TABS."""
        if self.tab in _LIVE_TABS:
            self._refresh_live_preview(force=True)

    def on_set(self, payload):
        payload = payload or {}
        name, value = payload.get("name"), payload.get("value")
        if name == "font_name":
            value = self._font_display_to_real.get(value, value)
        if self.opts.set(name, value):
            self._push_options()

    def on_overlay(self, payload):
        payload = payload or {}
        if self.opts.set(payload.get("name"), payload.get("value")):
            self.emit("overlay", self._overlay_view())

    def on_bounding(self, payload):
        payload = payload or {}
        on = bool(payload.get("on", self.bounding["on"]))
        left = max(0.0, min(1.0, float(payload.get("left", self.bounding["left"]))))
        right = max(0.0, min(1.0, float(payload.get("right", self.bounding["right"]))))
        was_on = self.bounding["on"]
        self.bounding = {"on": on, "left": left, "right": right}
        self._push_state()
        if on and not was_on:
            self._snap_stacked_items_vertically()

    # ------------------------------------------------------------ layout presets --

    def _word_entries(self, tab="words"):
        """(key, text, font_name, fusion centre y) per word under the playhead, in clip order."""
        items = list(self._items[tab].values())

        def _clip_start(item):
            clip = item.clip
            try:
                return clip.GetStart() if callable(getattr(clip, "GetStart", None)) else 0
            except Exception:
                return 0

        items.sort(key=_clip_start)
        return [(self._item_id(i), i.text, i.font_name, i.center[1] if i.center else 0.5) for i in items]

    def on_apply_layout(self, payload):
        payload = payload or {}
        preset_name = payload.get("preset") or self.opts.layout_preset
        if preset_name in LAYOUT_PRESETS:
            self.opts.set("layout_preset", preset_name)
        resolve = self._resolve()
        if resolve is None:
            return

        words = self._word_entries()
        if len(words) < 2:
            self.log("[Layout Preset] Need at least 2 Text+ words visible under the playhead to arrange a layout.")
            return self.emit("toast", {"text": "Needs at least 2 words under the playhead."})

        if preset_name == "Hero + Stack":
            selected = [k for k in payload.get("selected") or [] if k in self._items["words"]]
            hero_key = selected[0] if selected else words[len(words) // 2][0]
            targets = compute_large_word_layout(hero_key, words) if hero_key is not None else {}
        elif preset_name == "Side-by-Side":
            targets = compute_auto_spaced_row(words, target_size=AUTO_SPACE_WORDS_DEFAULT_SIZE)
        else:
            preset_fn = LAYOUT_PRESETS.get(preset_name)
            if preset_fn is None:
                self.log(f"[Layout Preset] Unknown preset '{preset_name}'.")
                return
            raw_targets = preset_fn(len(words))
            targets = {key: target for (key, *_rest), target in zip(words, raw_targets)}

        sub_actions, applied = self._apply_word_layout_targets(targets)
        self.log(f"[Layout Preset] Applied '{preset_name}' to {applied}/{len(words)} word(s).")
        self._record_batch_action(sub_actions)
        self._refresh_live_preview(force=True)

    def _apply_word_layout_targets(self, targets: dict) -> Tuple[list, int]:
        sub_actions = []
        applied = 0
        for key, (target_x, target_y, target_size) in targets.items():
            clip = self._clip_for("words", key)
            if clip is None:
                continue
            comp = self._get_fusion_comp(clip)
            text_tool = self._find_text_tool(comp) if comp is not None else None
            if text_tool is None:
                self.log("[Word Layout] Could not re-locate the Text+ tool for one word – skipped.")
                continue
            try:
                parsed_old_center = parse_point(text_tool.GetInput("Center"))
                old_center = list(parsed_old_center) if parsed_old_center is not None else None
            except Exception:
                old_center = None
            try:
                old_size = text_tool.GetInput("Size")
                old_size = float(old_size) if isinstance(old_size, (int, float)) else None
            except Exception:
                old_size = None
            try:
                text_tool.SetInput("Center", [target_x, target_y])
                text_tool.SetInput("Size", target_size)
                applied += 1
                if old_center is not None:
                    sub_actions.append(_LivePreviewAction(clip=clip, kind="position", old_value=old_center,
                                                          new_value=[target_x, target_y]))
                if old_size is not None:
                    sub_actions.append(_LivePreviewAction(clip=clip, kind="size", old_value=old_size,
                                                          new_value=target_size))
            except Exception as err:
                self.log(f"[Word Layout] SetInput Exception: {err}")
        return sub_actions, applied

    # ------------------------------------------------------------ bounding --

    def _snap_stacked_items_vertically(self):
        """Enabling Bounding snaps 2+ clips stacked under the playhead together vertically
        (touching, no gaps or overlap), keeping the group's centre where it was."""
        items = self._items["layout"]
        if len(items) < 2:
            return
        width, height = self.resolution
        aspect = (width / height) if height else 1.0

        text_tools = {}
        for key, item in items.items():
            comp = self._get_fusion_comp(item.clip)
            text_tools[key] = self._find_text_tool(comp) if comp is not None else None

        # Heights as fractions of frame HEIGHT, centres top-origin: the canvas's own terms.
        # A box's middle is `offset` below its clip's Center (the Center is the line box's).
        stack, offsets = [], {}
        for key, item in items.items():
            if item.center is None:
                continue
            box = canvas_math.text_box(item.font_name, item.text, item.font_size)
            box_height = (box["h"] + 2 * _BOX_PADDING) * aspect
            offsets[key] = (box["top"] + box["h"] / 2) * aspect
            text_tool = text_tools.get(key)
            if text_tool is not None:
                try:
                    background_enabled = bool(text_tool.GetInput("Enabled4"))
                    extend_vertical = text_tool.GetInput("ExtendVertical4")
                except Exception:
                    background_enabled, extend_vertical = False, None
                if background_enabled and isinstance(extend_vertical, (int, float)):
                    box_height += 2.0 * float(extend_vertical)
            stack.append((key, 1.0 - item.center[1] + offsets[key], box_height))
        new_centers = compute_vertical_stack_centers(stack)

        sub_actions = []
        moved = 0
        for key, top_y, _h in stack:
            new_top_y = new_centers.get(key)
            if new_top_y is None or abs(new_top_y - top_y) < 0.5 / 360:
                continue
            item, text_tool = items[key], text_tools.get(key)
            if text_tool is None:
                self.log("[Bounding] Could not re-locate the Text+ tool for one stacked clip – skipped.")
                continue
            try:
                parsed_old_center = parse_point(text_tool.GetInput("Center"))
                old_center = list(parsed_old_center) if parsed_old_center is not None else None
            except Exception:
                old_center = None
            fusion_x, fusion_y = CoordinateTranslator.pyside_to_fusion(item.center[0], new_top_y - offsets[key], 1, 1)
            try:
                text_tool.SetInput("Center", [fusion_x, fusion_y])
                moved += 1
                if old_center is not None:
                    sub_actions.append(_LivePreviewAction(clip=item.clip, kind="position", old_value=old_center,
                                                          new_value=[fusion_x, fusion_y]))
            except Exception as err:
                self.log(f"[Bounding] SetInput Exception while vertically stacking: {err}")
        if moved:
            self.log(f"[Bounding] Vertically snapped {moved} stacked Text+ clip(s) together.")
            self._record_batch_action(sub_actions)
            self._refresh_live_preview(force=True)

    def on_apply_bounding(self, _payload=None):
        try:
            if not self.bounding["on"]:
                self.log("[Warning] Turn on Show bounding lines first.")
                return
            left_frac = min(self.bounding["left"], self.bounding["right"])
            right_frac = max(self.bounding["left"], self.bounding["right"])

            resolve = self._resolve()
            if resolve is None:
                return
            timeline = self._timeline_or_log(resolve)
            if timeline is None:
                return
            found = self._scope_or_toast(timeline, self.opts.scope("layout_scope"), self.opts.track("layout_track"))
            if found is None:
                return
            clips, scope_desc = found
            self.log(f"[Action] Applying Bounding to {scope_desc}…")

            self.host.set_busy(True, "Applying bounding…")
            try:
                updated = 0
                scanned = 0
                processed_clips = []
                for clip, track_index in clips:
                    if clip is None:
                        continue
                    scanned += 1
                    clip_name = clip.GetName() if hasattr(clip, "GetName") else f"Clip on Track {track_index}"
                    comp = self._get_fusion_comp(clip)
                    if comp is None:
                        continue
                    text_tool = self._find_text_tool(comp)
                    if text_tool is None:
                        continue
                    try:
                        styled_text = text_tool.GetInput("StyledText")
                        font_name = text_tool.GetInput("Font")
                        font_size = text_tool.GetInput("Size")
                        center = parse_point(text_tool.GetInput("Center"))
                    except Exception as err:
                        self.log(f"  - Warning: Could not read style/position for clip '{clip_name}': {err}")
                        continue
                    if center is None or not isinstance(font_size, (int, float)):
                        self.log(f"  - Warning: Missing Center/Size for clip '{clip_name}' – skipped.")
                        continue
                    processed_clips.append((clip, text_tool, clip_name))
                    new_size = compute_bounding_fit_size(font_name, float(font_size), styled_text or "", left_frac, right_frac)
                    if new_size is None:
                        self.log(f"  - Skipped clip '{clip_name}': the Bounding lines are degenerate (right must be right of left).")
                        continue
                    new_center_x = (left_frac + right_frac) / 2.0
                    try:
                        text_tool.SetInput("Size", new_size)
                        text_tool.SetInput("Center", [new_center_x, center[1]])
                        updated += 1
                    except Exception as err:
                        self.log(f"  - Warning: Could not set Size/Center for clip '{clip_name}': {err}")

                self.log(f"Applied Bounding resize to {updated}/{scanned} clip(s) on {scope_desc}.")
                timeline_width, timeline_height = get_timeline_resolution(timeline)
                self._snap_all_stacked_groups_vertically(processed_clips, timeline_width, timeline_height)
            finally:
                self.host.set_busy(False)
            self.emit("toast", {"text": f"Bounding applied to {updated} Text+ clip(s)"})
            self._refresh_live_preview(force=True)
        except Exception as err:
            self.log(f"[Error in apply_bounding_to_timeline]: {err}")
            self.log(traceback.format_exc())

    def _snap_all_stacked_groups_vertically(self, clip_records: list, timeline_width: float, timeline_height: float):
        clips_for_grouping = []
        by_key = {}
        for idx, (clip, text_tool, clip_name) in enumerate(clip_records):
            try:
                start = clip.GetStart() if callable(getattr(clip, "GetStart", None)) else None
                end = clip.GetEnd() if callable(getattr(clip, "GetEnd", None)) else None
            except Exception as err:
                self.log(f"  - [Bounding] Could not read start/end for clip '{clip_name}' – excluded from vertical stacking: {err}")
                continue
            if start is None or end is None:
                self.log(f"  - [Bounding] Clip '{clip_name}' has no GetStart()/GetEnd() – excluded from vertical stacking.")
                continue
            clips_for_grouping.append((idx, start, end))
            by_key[idx] = (clip, text_tool, clip_name)

        groups = group_overlapping_clips(clips_for_grouping)
        aspect_ratio = (timeline_width / timeline_height) if timeline_height else 1.0
        solo_groups = sum(1 for group_keys in groups if len(group_keys) < 2)
        if solo_groups:
            self.log(
                f"[Bounding] {solo_groups} clip(s) never temporally overlap any other Text+ "
                "clip – their vertical position is left untouched by design (vertical "
                "snapping only applies to clips that are actually stacked together at some "
                "point on the timeline)."
            )

        groups_snapped = 0
        clips_moved = 0
        for group_keys in groups:
            if len(group_keys) < 2:
                continue
            members = []
            for key in group_keys:
                clip, text_tool, clip_name = by_key[key]
                try:
                    center = parse_point(text_tool.GetInput("Center"))
                    font_size = text_tool.GetInput("Size")
                except Exception as err:
                    self.log(f"  - [Bounding] Could not read Center/Size for clip '{clip_name}' – excluded from vertical stacking: {err}")
                    continue
                if center is None or not isinstance(font_size, (int, float)):
                    self.log(f"  - [Bounding] Clip '{clip_name}' has an unreadable Center/Size – excluded from vertical stacking.")
                    continue
                try:
                    font_name = text_tool.GetInput("Font") or "Arial"
                except Exception:
                    font_name = "Arial"
                # Cap height, as a fraction of composition height (it's measured in widths).
                height_fraction = cap_height_fraction(font_name, float(font_size)) * aspect_ratio
                try:
                    background_enabled = bool(text_tool.GetInput("Enabled4"))
                    extend_vertical = text_tool.GetInput("ExtendVertical4")
                except Exception:
                    background_enabled = False
                    extend_vertical = None
                if background_enabled and isinstance(extend_vertical, (int, float)):
                    height_fraction += 2.0 * float(extend_vertical)
                members.append((key, center[0], -center[1], height_fraction, clip, text_tool, clip_name))
            if len(members) < 2:
                continue
            new_centers = compute_vertical_stack_centers([(m[0], m[2], m[3]) for m in members])
            for key, center_x, _neg_center_y, _height_fraction, clip, text_tool, clip_name in members:
                new_neg_center_y = new_centers.get(key)
                if new_neg_center_y is None:
                    continue
                try:
                    text_tool.SetInput("Center", [center_x, -new_neg_center_y])
                    clips_moved += 1
                except Exception as err:
                    self.log(f"  - Warning: Could not vertically snap clip '{clip_name}': {err}")
            groups_snapped += 1
        if groups_snapped:
            self.log(f"[Bounding] Vertically snapped {clips_moved} Text+ clip(s) across {groups_snapped} stacked group(s) on the timeline.")

    def on_apply_position(self, _payload=None):
        try:
            scope = self.opts.scope("layout_scope")
            if scope == "playhead":
                # Those are the clips it copies FROM; the view disables the button for this.
                return self.emit("toast", {"text": "Apply position copies from the clips under the playhead – choose another Apply to."})
            resolve = self._resolve()
            if resolve is None:
                return
            timeline = self._timeline_or_log(resolve)
            if timeline is None:
                return
            active_items = get_active_text_plus_items(timeline, self._get_fusion_comp, self._find_text_tool, log=self.log)
            reference_by_track = {item.track_index: item.center for item in active_items if item.center is not None}
            if not reference_by_track:
                self.log("[Warning] No Text+ clip with a readable position is currently under the playhead – nothing to apply.")
                return self.emit("toast", {"text": "No Text+ clip under the playhead to copy the position from."})
            found = self._scope_or_toast(timeline, scope, self.opts.track("layout_track"))
            if found is None:
                return
            clips, scope_desc = found
            self.log(f"[Action] Applying Position to {scope_desc}…")

            self.host.set_busy(True, "Applying position…")
            try:
                sub_actions = []
                updated = 0
                scanned = 0
                for clip, track_index in clips:
                    reference_center = reference_by_track.get(track_index)
                    if clip is None or reference_center is None:
                        continue                # a track with nothing under the playhead: left alone
                    scanned += 1
                    clip_name = clip.GetName() if callable(getattr(clip, "GetName", None)) else f"Clip on Track {track_index}"
                    comp = self._get_fusion_comp(clip)
                    text_tool = self._find_text_tool(comp) if comp is not None else None
                    if text_tool is None:
                        continue
                    try:
                        old_center = parse_point(text_tool.GetInput("Center"))
                    except Exception:
                        old_center = None
                    new_center = [reference_center[0], reference_center[1]]
                    if (old_center is not None and abs(old_center[0] - new_center[0]) < 1e-9
                            and abs(old_center[1] - new_center[1]) < 1e-9):
                        continue
                    try:
                        text_tool.SetInput("Center", new_center)
                        updated += 1
                        if old_center is not None:
                            sub_actions.append(_LivePreviewAction(clip=clip, kind="position",
                                                                  old_value=list(old_center), new_value=new_center))
                    except Exception as err:
                        self.log(f"  - Warning: Could not set Center for clip '{clip_name}': {err}")
                self.log(f"[Apply Position] Updated {updated}/{scanned} Text+ clip(s) across "
                         f"{len(reference_by_track)} track(s) with a playhead reference.")
                self._record_batch_action(sub_actions)
            finally:
                self.host.set_busy(False)
            self.emit("toast", {"text": f"Position copied to {updated} Text+ clip(s)"})
            self._refresh_live_preview(force=True)
        except Exception as err:
            self.log(f"[Error in apply_position_to_timeline]: {err}")
            self.log(traceback.format_exc())

    # ------------------------------------------------------------ undo / redo --

    def _apply_live_preview_action_part(self, action: "_LivePreviewAction", is_undo: bool) -> bool:
        clip = action.clip
        kind = action.kind
        value = action.old_value if is_undo else action.new_value
        if clip is None:
            return False
        comp = self._get_fusion_comp(clip)
        text_tool = self._find_text_tool(comp) if comp is not None else None
        if text_tool is None:
            self.log("[Undo/Redo] Could not re-locate the Text+ tool for this clip.")
            return False
        if kind == "animation":
            if value is None:
                success, remove_logs = FusionAnimationEngine.remove_animations_from_clip(text_tool, comp)
                for log_msg in remove_logs:
                    self.log(log_msg)
            else:
                clip_name = clip.GetName() if callable(getattr(clip, "GetName", None)) else "Clip"
                preset, speed, direction = value if isinstance(value, tuple) else (value, None, None)
                success = self._apply_animation_to_tool(text_tool, comp, clip_name, preset, speed=speed, direction=direction)
            if success and action.clip_key is not None:
                self._anim_preset_by_clip_key[action.clip_key] = value
            return success
        input_name = "Center" if kind == "position" else "Size"
        try:
            text_tool.SetInput(input_name, value)
            return True
        except Exception as err:
            self.log(f"[Undo/Redo] {input_name} SetInput Exception: {err}")
            return False

    def _record_live_preview_action(self, clip: Any, kind: str, old_value: Any, new_value: Any):
        if old_value is None:
            self.log("[Undo/Redo] Could not record undo state for this edit (previous value unreadable).")
            return
        self._push_live_preview_action(_LivePreviewAction(clip=clip, kind=kind, old_value=old_value, new_value=new_value))

    def _record_batch_action(self, sub_actions: list):
        if not sub_actions:
            return
        self._push_live_preview_action(_LivePreviewAction(clip=None, kind="batch", old_value=sub_actions, new_value=sub_actions))

    def _push_live_preview_action(self, action: "_LivePreviewAction"):
        self._live_preview_undo_stack.append(action)
        if len(self._live_preview_undo_stack) > MAX_LIVE_PREVIEW_UNDO_HISTORY:
            self._live_preview_undo_stack.pop(0)
        self._live_preview_redo_stack.clear()
        self._push_history()

    def _apply_action(self, action, backward):
        if action.kind == "batch":
            results = [self._apply_live_preview_action_part(sub, backward) for sub in action.old_value]
            return any(results) if results else False
        return self._apply_live_preview_action_part(action, backward)

    def on_undo(self, _payload=None):
        if not self._live_preview_undo_stack:
            self.log("[Undo] Nothing to undo.")
            return self.emit("toast", {"text": "Nothing to undo"})
        action = self._live_preview_undo_stack.pop()
        if self._apply_action(action, True):
            self._live_preview_redo_stack.append(action)
            label = "layout" if action.kind == "batch" else action.kind
            self.log(f"[Undo] Reverted {label} change.")
            self.emit("toast", {"text": f"Undid {label} change"})
            self._refresh_live_preview(force=True)
        else:
            self.log("[Undo] Failed to revert change, some clips may be missing.")
        self._push_history()

    def on_redo(self, _payload=None):
        if not self._live_preview_redo_stack:
            self.log("[Redo] Nothing to redo.")
            return self.emit("toast", {"text": "Nothing to redo"})
        action = self._live_preview_redo_stack.pop()
        if self._apply_action(action, False):
            self._live_preview_undo_stack.append(action)
            label = "layout" if action.kind == "batch" else action.kind
            self.log(f"[Redo] Reapplied {label} change.")
            self.emit("toast", {"text": f"Redid {label} change"})
            self._refresh_live_preview(force=True)
        else:
            self.log("[Redo] Failed to reapply change, some clips may be missing.")
        self._push_history()

    # ------------------------------------------------------------ fusion lookups --

    def _get_fusion_comp(self, clip: Any) -> Optional[Any]:
        if clip is None:
            return None
        if callable(getattr(clip, "GetFusionCompByIndex", None)):
            try:
                comp = clip.GetFusionCompByIndex(1)
                if comp is not None:
                    return comp
            except Exception as err:
                self.log(f"  - [GetFusionCompByIndex Error]: {err}")
        if callable(getattr(clip, "GetFusionCompByName", None)):
            try:
                comp = clip.GetFusionCompByName()
                if comp is not None:
                    return comp
            except Exception as err:
                self.log(f"  - [GetFusionCompByName Error]: {err}")
        if callable(getattr(clip, "GetFusionCompList", None)):
            try:
                comps = clip.GetFusionCompList()
                if comps and len(comps) > 0:
                    return comps[0]
            except Exception as err:
                self.log(f"  - [GetFusionCompList Error]: {err}")
        return None

    def _find_text_tool(self, comp: Any) -> Optional[Any]:
        if comp is None:
            return None
        if callable(getattr(comp, "GetToolList", None)):
            try:
                tools = comp.GetToolList(False, "TextPlus")
                if tools:
                    return list(tools.values())[0] if isinstance(tools, dict) else tools[0]
            except Exception as err:
                self.log(f"  - [GetToolList TextPlus Error]: {err}")
        if callable(getattr(comp, "GetToolList", None)):
            try:
                tools = comp.GetToolList(False)
                if tools:
                    tool_list = list(tools.values()) if isinstance(tools, dict) else tools
                    for t in tool_list:
                        if getattr(t, "ID", None) in ["TextPlus", "Text1", "Text"]:
                            return t
            except Exception as err:
                self.log(f"  - [GetToolList All Error]: {err}")
        for tool_name in ["TextPlus_1", "TextPlus1", "Text1", "Template"]:
            if callable(getattr(comp, "FindTool", None)):
                try:
                    tool = comp.FindTool(tool_name)
                    if tool is not None:
                        return tool
                except Exception as err:
                    self.log(f"  - [FindTool '{tool_name}' Error]: {err}")
        return None

    # ------------------------------------------------------------ animations --

    def _scope_clips(self, timeline, scope: str, track: int) -> Tuple[list, str]:
        """[(clip, video track)] an "Apply to" choice covers, and how to say which:
        "timeline" every video track, "selected" the clips selected on Resolve's timeline
        (Timeline.GetSelectedClips - Resolve 21.0.4 and later; raises _ScopeUnavailable on an
        older one), "playhead" the Text+ under the playhead, "track" one video track. Not
        every clip is a Text+ - callers skip the ones without one."""
        if scope == "playhead":
            items = get_active_text_plus_items(timeline, self._get_fusion_comp, self._find_text_tool, log=self.log)
            return [(item.clip, item.track_index) for item in items], "the Text+ clip(s) under the playhead"
        if scope == "track":
            return [(c, track) for c in timeline.GetItemListInTrack("video", track) or []], f"Video Track {track}"
        if scope == "selected":
            selected = getattr(timeline, "GetSelectedClips", None)
            if not callable(selected):
                raise _ScopeUnavailable("Selected clips needs DaVinci Resolve 21.0.4 or later.")
            clips = []
            for clip in selected() or []:
                try:
                    kind, index = clip.GetTrackTypeAndIndex()
                except Exception:
                    continue
                if kind == "video":             # audio and subtitle clips hold no Text+
                    clips.append((clip, int(index)))
            clips.sort(key=lambda c: (c[1], c[0].GetStart() if callable(getattr(c[0], "GetStart", None)) else 0))
            return clips, "the selected clips"
        try:
            track_count = timeline.GetTrackCount("video") if hasattr(timeline, "GetTrackCount") else 0
        except Exception:
            track_count = 0
        clips_with_tracks = []
        for track_index in range(1, track_count + 1):
            track_clips = timeline.GetItemListInTrack("video", track_index) or []
            clips_with_tracks.extend((c, track_index) for c in track_clips)
        return clips_with_tracks, f"every video track ({track_count} track(s))"

    def _scope_or_toast(self, timeline, scope: str, track: int) -> Optional[Tuple[list, str]]:
        """_scope_clips, or None after saying what's wrong: nothing there, or a choice this
        Resolve can't do."""
        try:
            clips, desc = self._scope_clips(timeline, scope, track)
        except _ScopeUnavailable as exc:
            self.log(f"[Warning] {exc}")
            self.emit("toast", {"text": str(exc)})
            return None
        if not clips:
            self.log(f"[Info] No video clips found on {desc}.")
            self.emit("toast", {"text": _EMPTY_SCOPE.get(scope, _EMPTY_SCOPE["timeline"]).format(track=track)})
            return None
        return clips, desc

    def _apply_animation_to_tool(self, text_tool, comp, clip_name: str, preset_choice: str,
                                 speed: Optional[str] = None, direction: Optional[str] = None) -> bool:
        if speed is None:
            speed = self.opts.anim_speed
        if direction is None:
            direction = self.opts.anim_direction
        FusionAnimationEngine.remove_animations_from_clip(text_tool, comp)
        if "Bounce" in preset_choice:
            duration = FusionAnimationEngine.scaled_duration(20, speed)
            success, preset_logs = FusionAnimationEngine.apply_bounce_preset_to_clip(text_tool, comp, duration=duration)
        elif "Pop" in preset_choice:
            duration = FusionAnimationEngine.scaled_duration(15, speed)
            success, preset_logs = FusionAnimationEngine.apply_pop_preset_to_clip(text_tool, comp, duration=duration)
        elif "Slide" in preset_choice:
            target_center = self.current_fusion_pos
            if hasattr(text_tool, "GetInput"):
                try:
                    existing_center = parse_point(text_tool.GetInput("Center"))
                except Exception:
                    existing_center = None
                if existing_center is not None:
                    target_center = existing_center
            duration = FusionAnimationEngine.scaled_duration(15, speed)
            success, preset_logs = FusionAnimationEngine.apply_slide_preset_to_clip(
                text_tool, comp, target_center=target_center, direction=direction, duration=duration)
        else:
            duration = FusionAnimationEngine.scaled_duration(15, speed)
            success, preset_logs = FusionAnimationEngine.apply_fade_preset_to_clip(text_tool, comp, duration=duration)
        for log_msg in preset_logs:
            self.log(log_msg)
        if not success:
            self.log(f"  - Warning: Could not apply '{preset_choice}' animation to clip '{clip_name}'.")
        return success

    def _animation_clip_key(self, clip: Any, track_index: int) -> Optional[tuple]:
        try:
            name = clip.GetName() if callable(getattr(clip, "GetName", None)) else None
            start = clip.GetStart() if callable(getattr(clip, "GetStart", None)) else None
            end = clip.GetEnd() if callable(getattr(clip, "GetEnd", None)) else None
        except Exception:
            return None
        if name is None or start is None or end is None:
            return None
        return (name, start, end, track_index)

    def on_apply_animation(self, _payload=None):
        scope = self.opts.scope("anim_scope")
        try:
            resolve = self._resolve()
            if resolve is None:
                return
            timeline = self._timeline_or_log(resolve)
            if timeline is None:
                return
            preset_choice = self.opts.anim_preset
            self.log("[Action] Starting text animation…")
            found = self._scope_or_toast(timeline, scope, self.opts.track("anim_track"))
            if found is None:
                return
            video_clips_with_tracks, scope_desc = found
            self.log(f"Found {len(video_clips_with_tracks)} clip(s) on {scope_desc}. Applying animations…")
            self.host.set_busy(True, "Applying animations…")
            try:
                sub_actions = []
                count = 0
                total = len(video_clips_with_tracks)
                for idx, (clip, track_index) in enumerate(video_clips_with_tracks, start=1):
                    if clip is None:
                        continue
                    clip_name = clip.GetName() if callable(getattr(clip, "GetName", None)) else f"Clip_{idx}"
                    comp = self._get_fusion_comp(clip)
                    if comp is None:
                        self.log(f"  - [{idx}/{total}] Warning: Could not access Fusion comp for clip '{clip_name}'.")
                        continue
                    text_tool = self._find_text_tool(comp)
                    if text_tool is None:
                        self.log(f"  - [{idx}/{total}] Warning: Could not find TextPlus tool inside comp for clip '{clip_name}'.")
                        continue
                    clip_key = self._animation_clip_key(clip, track_index)
                    old_preset = self._anim_preset_by_clip_key.get(clip_key) if clip_key is not None else None
                    current_speed = self.opts.anim_speed
                    current_direction = self.opts.anim_direction
                    new_preset_tuple = (preset_choice, current_speed, current_direction)
                    if self._apply_animation_to_tool(text_tool, comp, clip_name, preset_choice,
                                                     speed=current_speed, direction=current_direction):
                        count += 1
                        if clip_key is not None:
                            self._anim_preset_by_clip_key[clip_key] = new_preset_tuple
                            sub_actions.append(_LivePreviewAction(clip=clip, kind="animation", old_value=old_preset,
                                                                  new_value=new_preset_tuple, clip_key=clip_key))
                self.log(f"Successfully animated {count}/{total} Text+ clips on {scope_desc} using '{preset_choice}'.")
                self._record_batch_action(sub_actions)
            finally:
                self.host.set_busy(False)
            self.emit("toast", {"text": f"Animated {count} Text+ clip(s)"})
        except Exception as err:
            self.log(f"[Error in apply_animations]: {err}")
            self.log(traceback.format_exc())

    def on_remove_animations(self, _payload=None):
        try:
            resolve = self._resolve()
            if resolve is None:
                return
            timeline = self._timeline_or_log(resolve)
            if timeline is None:
                return
            self.log("[Action] Removing animations…")
            found = self._scope_or_toast(timeline, self.opts.scope("anim_scope"), self.opts.track("anim_track"))
            if found is None:
                return
            video_clips_with_tracks, scope_desc = found
            self.host.set_busy(True, "Removing animations…")
            try:
                sub_actions = []
                count = 0
                total = len(video_clips_with_tracks)
                for idx, (clip, track_index) in enumerate(video_clips_with_tracks, start=1):
                    if clip is None:
                        continue
                    clip_name = clip.GetName() if callable(getattr(clip, "GetName", None)) else f"Clip_{idx}"
                    comp = self._get_fusion_comp(clip)
                    if comp is None:
                        self.log(f"  - [{idx}/{total}] Warning: Could not access Fusion comp for clip '{clip_name}'.")
                        continue
                    text_tool = self._find_text_tool(comp)
                    if text_tool is None:
                        self.log(f"  - [{idx}/{total}] Warning: Could not find TextPlus tool inside comp for clip '{clip_name}'.")
                        continue
                    clip_key = self._animation_clip_key(clip, track_index)
                    old_preset = self._anim_preset_by_clip_key.get(clip_key) if clip_key is not None else None
                    success, remove_logs = FusionAnimationEngine.remove_animations_from_clip(text_tool, comp)
                    for log_msg in remove_logs:
                        self.log(log_msg)
                    if success:
                        count += 1
                        if clip_key is not None:
                            self._anim_preset_by_clip_key[clip_key] = None
                            sub_actions.append(_LivePreviewAction(clip=clip, kind="animation", old_value=old_preset,
                                                                  new_value=None, clip_key=clip_key))
                self.log(f"Removed animations from {count}/{total} Text+ clips on {scope_desc}.")
                self._record_batch_action(sub_actions)
            finally:
                self.host.set_busy(False)
            self.emit("toast", {"text": f"Removed animations from {count} Text+ clip(s)"})
        except Exception as err:
            self.log(f"[Error in remove_animations_from_timeline]: {err}")
            self.log(traceback.format_exc())

    def on_dump(self, _payload=None):
        self.log("[Action] Dumping selected node settings from Fusion…")
        resolve = self._resolve()
        if resolve is None:
            return
        try:
            success, message, saved_path = dump_selected_node_settings(resolve)
            self.log(message)
            if success and saved_path:
                self.log("Share that file's contents to help debug animation behavior.")
                self.emit("toast", {"text": "Saved the node's settings to your Desktop"})
        except Exception as err:
            self.log(f"[Error in dump_selected_node_settings]: {err}")
            self.log(traceback.format_exc())

    # ------------------------------------------------------------ font style --

    def _font_display_for_real_name(self, real_name: str) -> Optional[str]:
        for display_name, mapped_real_name in self._font_display_to_real.items():
            if mapped_real_name == real_name:
                return display_name
        return None

    def on_reset_font(self, _payload=None):
        self.opts.set("font_name", DEFAULT_FONT_NAME)
        self.opts.set("font_size", 0.08)
        self._push_options()
        self.log(f"Font reset to '{DEFAULT_FONT_NAME} {DEFAULT_FONT_STYLE}' – applying it to the clips in Apply to…")
        self._apply_style(force_font_only=True)

    def _get_playhead_text_tools(self, timeline):
        items = get_active_text_plus_items(timeline, self._get_fusion_comp, self._find_text_tool, log=self.log)
        results = []
        for item in items:
            comp = self._get_fusion_comp(item.clip)
            if comp is None:
                continue
            text_tool = self._find_text_tool(comp)
            if text_tool is None:
                continue
            results.append((item.clip, text_tool))
        return results

    def _apply_font_style_to_tool(self, text_tool, clip_name: str, force_font_only=False) -> bool:
        o = self.opts
        success = False
        if o.toggle("font_on") or force_font_only:
            family, style = split_font_name_and_style(o.font_name)
            success, style_logs = FusionAnimationEngine.apply_text_style(
                text_tool, font_name=family, font_size=o.slider("font_size"), color=o.color("font_color"),
                font_style=style)
            for log_msg in style_logs:
                self.log(log_msg)
        if force_font_only:
            return success

        if o.toggle("outline_on"):
            outline_success, outline_logs = FusionAnimationEngine.apply_outline_style(
                text_tool, color=o.color("outline_color"), thickness=o.slider("outline_thickness"),
                opacity=o.slider("outline_opacity"))
        else:
            outline_success, outline_logs = FusionAnimationEngine.disable_shading_element(
                text_tool, FusionAnimationEngine.OUTLINE_ELEMENT, "Outline")
        for log_msg in outline_logs:
            self.log(log_msg)

        if o.toggle("shadow_on"):
            shadow_success, shadow_logs = FusionAnimationEngine.apply_shadow_style(
                text_tool, color=o.color("shadow_color"),
                offset=(o.slider("shadow_offset_x"), o.slider("shadow_offset_y")),
                blur=o.slider("shadow_blur"), opacity=o.slider("shadow_opacity"))
        else:
            shadow_success, shadow_logs = FusionAnimationEngine.disable_shading_element(
                text_tool, FusionAnimationEngine.SHADOW_ELEMENT, "Shadow")
        for log_msg in shadow_logs:
            self.log(log_msg)

        if o.toggle("background_on"):
            background_success, background_logs = FusionAnimationEngine.apply_background_style(
                text_tool, color=o.color("background_color"), opacity=o.slider("background_opacity"),
                corner_radius=o.slider("background_corner_radius"),
                extend_horizontal=o.slider("background_extend_horizontal"),
                extend_vertical=o.slider("background_extend_vertical"))
        else:
            background_success, background_logs = FusionAnimationEngine.disable_shading_element(
                text_tool, FusionAnimationEngine.BACKGROUND_ELEMENT, "Background")
        for log_msg in background_logs:
            self.log(log_msg)

        any_success = success or outline_success or shadow_success or background_success
        if not any_success:
            self.log(f"  - Warning: Could not apply font style to clip {clip_name}.")
        return any_success

    def on_apply_style(self, _payload=None):
        self._apply_style()

    def _apply_style(self, force_font_only=False):
        try:
            scope = self.opts.style_scope
            resolve = self._resolve()
            if resolve is None:
                return
            timeline = self._timeline_or_log(resolve)
            if timeline is None:
                return
            found = self._scope_or_toast(timeline, scope, self.opts.track("style_track"))
            if found is None:
                return
            clips, scope_desc = found
            self.log(f"[Action] Applying font style to {scope_desc}…")
            self.host.set_busy(True, "Applying font style…")
            try:
                updated = scanned = 0
                for clip, track_index in clips:
                    if clip is None:
                        continue
                    scanned += 1
                    comp = self._get_fusion_comp(clip)
                    text_tool = self._find_text_tool(comp) if comp is not None else None
                    if text_tool is None:
                        continue
                    clip_name = clip.GetName() if hasattr(clip, "GetName") else "Clip"
                    if self._apply_font_style_to_tool(text_tool, f"'{clip_name}' on Track {track_index}",
                                                      force_font_only=force_font_only):
                        updated += 1
                self.log(f"Applied font style ('{self.opts.font_name}', size {self.opts.slider('font_size')}) "
                         f"to {updated}/{scanned} clip(s) on {scope_desc}.")
            finally:
                self.host.set_busy(False)
            self.emit("toast", {"text": f"Styled {updated} Text+ clip(s)"})
        except Exception as err:
            self.log(f"[Error in apply_style]: {err}")
            self.log(traceback.format_exc())
