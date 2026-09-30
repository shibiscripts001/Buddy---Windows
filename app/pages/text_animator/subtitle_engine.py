"""Subtitle extraction and Text+ clip creation logic.

Exact placement needs a Text+ in the Media Pool (AppendToTimeline puts a
Media Pool item at a given frame and track; a title from the Effects
Library can only go in at the playhead). Scripting can't make one: a Text+
inserted with InsertFusionTitleIntoTimeline has no Media Pool item behind
it. So Buddy ships one - text_plus_template.drb, a bin exported from
Resolve 21.1 (Folder.Export) holding a single Text+, with the exporting
PC's user and system ids blanked - and imports it (ImportFolderFromFile)
the first time a project has none. Measured on 21.1: it arrives as a
"Buddy Text+" bin in whichever bin is selected (so the root is selected
first), each import adds another copy (so it's only imported when no Text+
is found), and titles placed from it land on the exact frames asked for,
longer than its own 5 seconds too, each with its own text.
"""
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from .animation_engine import FusionAnimationEngine
from .font_utils import DEFAULT_FONT_NAME, DEFAULT_FONT_STYLE, SCRIPT_FONTS, font_for_texts, script_of

TEMPLATE_BIN = "Buddy Text+"
TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "text_plus_template.drb")


def source_frames_for(length: int, source_fps: float, timeline_fps: float) -> int:
    """How many of a Media Pool clip's own frames to place so it lasts `length`
    timeline frames. AppendToTimeline's startFrame/endFrame count the SOURCE clip's
    frames - whole ones - and Resolve rounds the timeline length down: measured
    2026-09-28, n frames of Buddy's 24 fps Text+ template last floor(n x 30/24) frames
    on a 30 fps timeline. (The template is 24 fps whatever the project - it comes from
    text_plus_template.drb, and Resolve refuses to change its FPS.) Passing timeline
    frames as they were made a 90-frame subtitle 112 frames long, and each subtitle
    after it was pushed later. A length no whole n gives (29 at 24 -> 30 fps) comes out
    one frame short, never long: long would push the next subtitle along too."""
    if not source_fps or not timeline_fps or abs(source_fps - timeline_fps) < 1e-6:
        return max(1, int(length))
    ratio = timeline_fps / source_fps
    n = math.ceil(length / ratio - 1e-9)
    if math.floor(n * ratio + 1e-9) > length:
        n -= 1
    return max(1, n)
# Media Pool item types that can be a Text+ template. Anything else with
# "Title" in its name - a timeline called "Title sequence", say - isn't one.
_TEMPLATE_TYPES = ("Fusion Title", "Generator", "Title")


def get_timeline_resolution(timeline: Any) -> Tuple[int, int]:
    """Returns (width, height) of the timeline's frame resolution, defaulting to 1920x1080
    if unavailable. Used to size the placement canvas to match the project's actual aspect
    ratio instead of assuming a fixed 16:9."""
    width, height = 1920, 1080
    if timeline is not None and hasattr(timeline, "GetSetting"):
        try:
            w_val = timeline.GetSetting("timelineResolutionWidth")
            if w_val:
                width = int(w_val)
        except Exception:
            pass
        try:
            h_val = timeline.GetSetting("timelineResolutionHeight")
            if h_val:
                height = int(h_val)
        except Exception:
            pass
    return width, height


def timecode_to_frames(timecode: Optional[str], fps: float, drop_frame: bool) -> Optional[int]:
    """Converts an SMPTE timecode string (HH:MM:SS:FF, or HH:MM:SS;FF if drop-frame) to an
    absolute frame count - the inverse of TextPlusGenerator._frames_to_timecode(). Used to
    locate which clip sits under the current playhead. The drop-frame correction is the
    standard SMPTE reverse formula, symmetric with the forward encoding in
    _frames_to_timecode().
    """
    if not timecode:
        return None
    try:
        parts = timecode.replace(";", ":").split(":")
        if len(parts) != 4:
            return None
        hours, minutes, seconds, frames = (int(p) for p in parts)
    except (ValueError, TypeError):
        return None

    fps_int = int(round(fps))
    total_frames = ((hours * 3600 + minutes * 60 + seconds) * fps_int) + frames

    if drop_frame and fps_int in (30, 60):
        drop_frames_per_min = 2 if fps_int == 30 else 4
        total_minutes = hours * 60 + minutes
        total_frames -= drop_frames_per_min * (total_minutes - total_minutes // 10)

    return total_frames


def get_top_most_video_track_index(timeline: Any) -> int:
    """Returns the index for a brand new top-most video track (existing_count + 1)."""
    if timeline is None or not hasattr(timeline, "GetTrackCount"):
        return 1
    try:
        count = timeline.GetTrackCount("video")
        if count is not None and isinstance(count, int):
            return max(1, count + 1)
    except Exception:
        pass
    return 1


def get_top_most_unpopulated_video_track_index(timeline: Any) -> int:
    """Returns the highest-indexed existing video track that has no clips on it, so
    converted subtitles don't land on top of existing content. If every existing video
    track already has clips, returns the index for a brand new top-most track instead."""
    if timeline is None or not hasattr(timeline, "GetTrackCount"):
        return 1

    try:
        count = timeline.GetTrackCount("video")
    except Exception:
        count = None

    if count is None or not isinstance(count, int) or count < 1:
        return 1

    if hasattr(timeline, "GetItemListInTrack"):
        for track_index in range(count, 0, -1):
            try:
                clips = timeline.GetItemListInTrack("video", track_index)
            except Exception:
                continue
            if not clips:
                return track_index

    return count + 1


def normalize_line_breaks(text):
    """A subtitle's line break as Text+ draws one. Resolve reports a
    two-line subtitle with U+2028 (LINE SEPARATOR) between the lines, and
    Text+ draws that as a gap on ONE line - measured: a two-line Japanese
    subtitle ran off both edges of the frame - until someone clicks into its
    text box, which swaps in a real newline. So it's swapped here."""
    if not isinstance(text, str):
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n").replace(" ", "\n")


def _number(value) -> Optional[float]:
    """A real number from a Resolve property, or None - never a bool
    (Resolve's False means "no such property")."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class SubtitleData:
    """Data object representing extracted subtitle information."""

    def __init__(
        self,
        text: str,
        start_frame: int,
        end_frame: int,
        font_name: Optional[str] = None,
        font_size: Optional[float] = None,
        color: Optional[Tuple[float, float, float]] = None,
    ):
        self.text = text
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.duration = max(1, end_frame - start_frame)
        self.font_name = font_name
        self.font_size = font_size
        self.color = color

    def __repr__(self):
        return f"<SubtitleData text='{self.text}' start={self.start_frame} end={self.end_frame} dur={self.duration}>"


class SubtitleExtractor:
    """Extracts text and timings from Resolve subtitle tracks using the GetName() hack."""

    def __init__(self, resolve_instance: Any = None):
        self.resolve = resolve_instance

    def _extract_styling(self, clip: Any) -> Tuple[Optional[str], Optional[float], Optional[Tuple[float, float, float]]]:
        """Extracts font name, size, and RGB color defensively from subtitle clip if available.

        Resolve answers False (not None) for a property a clip doesn't have - and a subtitle
        clip has none of these (GetProperty() is {} on 21.1). float(False) is 0.0, which gave
        every converted title size 0 and black text: there, but invisible. So only real
        numbers count, a size must be above 0, and a colour needs all three parts."""
        font_name = None
        font_size = None
        color = None

        if clip is None:
            return None, None, None

        # Try GetProperty / GetClipProperty
        for getter_name in ["GetProperty", "GetClipProperty"]:
            if hasattr(clip, getter_name):
                try:
                    getter = getattr(clip, getter_name)
                    # Check Font
                    for font_key in ["Font", "FontName", "StyleFont"]:
                        f_val = getter(font_key)
                        if f_val is not None and isinstance(f_val, str) and f_val.strip():
                            font_name = f_val.strip()
                            break

                    # Check Size
                    for size_key in ["Size", "FontSize", "StyleSize"]:
                        s_val = _number(getter(size_key))
                        if s_val is not None and s_val > 0:
                            font_size = s_val
                            break

                    # Check Color with explicit None checks to avoid 0.0 evaluating as False
                    parts = []
                    for primary, fallback in (("ColorR", "Red"), ("ColorG", "Green"), ("ColorB", "Blue")):
                        value = _number(getter(primary))
                        parts.append(value if value is not None else _number(getter(fallback)))
                    if all(p is not None for p in parts):
                        scale = 255.0 if any(p > 1.0 for p in parts) else 1.0
                        color = tuple(min(1.0, max(0.0, p / scale)) for p in parts)
                except Exception:
                    pass

        return font_name, font_size, color

    def extract_subtitles_from_track(self, timeline: Any, track_index: int = 1) -> List[SubtitleData]:
        """Iterates through subtitle track clips and extracts SubtitleData.

        Note: DO NOT call GetSubtitleText(). Use clip.GetName() for text,
        GetStart() and GetEnd() for timings. Defensive checks strictly applied.
        """
        if timeline is None or not hasattr(timeline, "GetItemListInTrack"):
            return []

        subtitle_clips = timeline.GetItemListInTrack("subtitle", track_index)
        if subtitle_clips is None:
            return []

        extracted: List[SubtitleData] = []
        for clip in subtitle_clips:
            if clip is None:
                continue

            text = None
            if hasattr(clip, "GetName"):
                try:
                    text = normalize_line_breaks(clip.GetName())
                except Exception:
                    pass

            start = clip.GetStart() if hasattr(clip, "GetStart") else None
            end = clip.GetEnd() if hasattr(clip, "GetEnd") else None

            if text is not None and start is not None and end is not None:
                font_name, font_size, color = self._extract_styling(clip)
                extracted.append(
                    SubtitleData(
                        text=text,
                        start_frame=start,
                        end_frame=end,
                        font_name=font_name,
                        font_size=font_size,
                        color=color,
                    )
                )

        return extracted


class TextPlusGenerator:
    """Generates Text+ items / clips directly onto the timeline for each extracted subtitle data."""

    def __init__(self, resolve_instance: Any = None):
        self.resolve = resolve_instance

    def _ensure_and_target_track(self, timeline: Any, target_video_track: int) -> None:
        """Ensures target video track exists on the timeline."""
        if timeline is None or not hasattr(timeline, "GetTrackCount"):
            return

        try:
            current_count = timeline.GetTrackCount("video")
            if current_count is not None and hasattr(timeline, "AddTrack"):
                while current_count < target_video_track:
                    res = timeline.AddTrack("video")
                    current_count += 1
                    if res is False:
                        break
        except Exception:
            pass

    def _get_media_pool_text_plus_item(self, media_pool: Any) -> Optional[Any]:
        """Recursively searches every Media Pool folder for an existing Text+ generator item."""
        if media_pool is None or not hasattr(media_pool, "GetRootFolder"):
            return None

        try:
            root_folder = media_pool.GetRootFolder()
        except Exception:
            return None

        if root_folder is None:
            return None

        visited = set()
        folders_to_search = [root_folder]

        while folders_to_search:
            folder = folders_to_search.pop(0)
            if folder is None or id(folder) in visited:
                continue
            visited.add(id(folder))

            if hasattr(folder, "GetClipList"):
                try:
                    clips = folder.GetClipList()
                except Exception:
                    clips = None
                if clips:
                    for clip in clips:
                        if clip is None or not hasattr(clip, "GetName"):
                            continue
                        try:
                            name = clip.GetName()
                        except Exception:
                            continue
                        if name and ("Text+" in name or "TextPlus" in name or "Title" in name):
                            kind = None
                            if hasattr(clip, "GetClipProperty"):
                                try:
                                    kind = (clip.GetClipProperty() or {}).get("Type")
                                except Exception:
                                    pass
                            if not kind or kind in _TEMPLATE_TYPES:
                                return clip

            if hasattr(folder, "GetSubFolderList"):
                try:
                    subfolders = folder.GetSubFolderList()
                except Exception:
                    subfolders = None
                if subfolders:
                    folders_to_search.extend(subfolders)

        return None

    def _fusion_fonts(self):
        """The font families Fusion can draw, or None if it can't be asked."""
        try:
            fonts = self.resolve.Fusion().FontManager.GetFontList()
        except Exception:
            return None
        return set(fonts) if isinstance(fonts, dict) and fonts else None

    def _default_font_for(self, subtitles: List[SubtitleData], log_msgs: List[str]) -> str:
        """The font for titles whose subtitle named none: Arial, unless the
        text is in a script Arial doesn't have (font_utils.SCRIPT_FONTS)."""
        script = script_of(s.text for s in subtitles if s is not None and s.font_name is None)
        if not script:
            return ""
        font = font_for_texts([s.text for s in subtitles if s is not None], self._fusion_fonts())
        if font:
            log_msgs.append(f"  - [Setup] Using the {font} font – {DEFAULT_FONT_NAME} has no characters for "
                            "this text.")
        else:
            log_msgs.append(f"  - [Setup Warning] No font with characters for this text was found (tried "
                            f"{', '.join(SCRIPT_FONTS[script])}); titles may show boxes or nothing. Pick "
                            "a font that has them under Font styling.")
        return font

    def _add_text_plus_template(self, media_pool: Any) -> Optional[Any]:
        """Imports Buddy's Text+ template (see the module docstring) into a
        "Buddy Text+" bin at the top of the Media Pool, putting the selected
        bin back after. Returns the Text+, or None if it couldn't be added."""
        if (media_pool is None or not os.path.isfile(TEMPLATE_FILE)
                or not hasattr(media_pool, "ImportFolderFromFile")):
            return None
        try:
            root = media_pool.GetRootFolder()
            selected = media_pool.GetCurrentFolder()
        except Exception:
            return None
        if root is None:
            return None
        try:
            media_pool.SetCurrentFolder(root)
            imported = media_pool.ImportFolderFromFile(TEMPLATE_FILE)
        except Exception:
            imported = False
        finally:
            if selected is not None:
                try:
                    media_pool.SetCurrentFolder(selected)
                except Exception:
                    pass
        if not imported:
            return None
        try:
            for folder in root.GetSubFolderList() or []:
                if folder.GetName() == TEMPLATE_BIN:
                    for clip in folder.GetClipList() or []:
                        if "Text+" in (clip.GetName() or ""):
                            return clip
        except Exception:
            pass
        return None

    def _find_new_clip_on_track(
        self, timeline: Any, track_index: int, existing_clips: List[Any], start_frame: int
    ) -> Optional[Any]:
        """Locates the newly added clip on the specified track."""
        if timeline is None or not hasattr(timeline, "GetItemListInTrack"):
            return None

        current_clips = timeline.GetItemListInTrack("video", track_index)
        if not current_clips:
            return None

        existing_ids = {id(c) for c in existing_clips if c is not None}
        new_clips = [c for c in current_clips if c is not None and id(c) not in existing_ids]

        if new_clips:
            best_clip = None
            min_dist = float("inf")
            for c in new_clips:
                c_start = c.GetStart() if hasattr(c, "GetStart") else start_frame
                dist = abs(c_start - start_frame)
                if dist < min_dist:
                    min_dist = dist
                    best_clip = c
            return best_clip or new_clips[-1]

        return None

    def _find_clip_track_index(self, timeline: Any, clip_item: Any, expected_track: int, max_tracks: int = 20) -> Optional[int]:
        """Scans video tracks to find which one actually contains clip_item, since
        InsertFusionTitleIntoTimeline inserts onto whatever the host considers the
        'current' track, which may not match the requested target_video_track."""
        if timeline is None or clip_item is None or not hasattr(timeline, "GetItemListInTrack"):
            return None

        try:
            track_count = timeline.GetTrackCount("video") if hasattr(timeline, "GetTrackCount") else max_tracks
        except Exception:
            track_count = max_tracks

        # Check the expected track first (cheap common case).
        for track_index in [expected_track] + [i for i in range(1, (track_count or max_tracks) + 1) if i != expected_track]:
            try:
                clips = timeline.GetItemListInTrack("video", track_index)
            except Exception:
                continue
            if clips and any(c is clip_item for c in clips):
                return track_index

        return None

    def _apply_text_and_styling(self, clip_item: Any, text: str, sub_data: Optional[SubtitleData] = None,
                                default_font: str = "") -> bool:
        """Injects StyledText and maps font styling properties (Font, Size, Red1, Green1, Blue1) onto Text+ node.

        Step 1: Access Fusion Comp via clip.GetFusionCompByIndex(1) (with fallbacks).
        Step 2: Find TextPlus tool via comp.GetToolList(False, "TextPlus").
        Step 3: Set StyledText property using SetInput("StyledText", text).
        Step 4: Map Font Properties (Font, Size, Red1, Green1, Blue1) with defensive checks.
        Step 5: Defensive checks if value is not None before applying parameters, fallback defaults used.
        """
        if clip_item is None:
            return False

        # Step 1: Access the Fusion Comp (Primary: GetFusionCompByIndex(1))
        comp = None
        if hasattr(clip_item, "GetFusionCompByIndex"):
            try:
                comp = clip_item.GetFusionCompByIndex(1)
            except Exception:
                pass

        if comp is None and hasattr(clip_item, "GetFusionCompByName"):
            try:
                comp = clip_item.GetFusionCompByName()
            except Exception:
                pass

        if comp is None and hasattr(clip_item, "GetFusionCompList"):
            try:
                comps = clip_item.GetFusionCompList()
                if comps is not None and len(comps) > 0:
                    comp = comps[0]
            except Exception:
                pass

        if comp is None:
            return False

        # Step 2: Find the TextPlus Tool
        text_tool = None
        if hasattr(comp, "GetToolList"):
            try:
                tools = comp.GetToolList(False, "TextPlus")
                if tools is not None:
                    text_tool = list(tools.values())[0] if isinstance(tools, dict) else tools[0]
            except Exception:
                pass

        if text_tool is None:
            for name in ["TextPlus_1", "TextPlus1", "Text1", "Template"]:
                if hasattr(comp, "FindTool"):
                    try:
                        tool = comp.FindTool(name)
                        if tool is not None:
                            text_tool = tool
                            break
                    except Exception:
                        pass

        if text_tool is None or not hasattr(text_tool, "SetInput"):
            return False

        # Step 3: Set the StyledText Property
        try:
            text_tool.SetInput("StyledText", normalize_line_breaks(text))
        except Exception:
            return False

        # Step 4 & 5: Map Font Properties & Defensive Checks
        # Determine target styling values (extracted sub_data values or fallback defaults)
        target_font = (sub_data.font_name if (sub_data and sub_data.font_name is not None)
                       else default_font or DEFAULT_FONT_NAME)
        target_size = sub_data.font_size if (sub_data and sub_data.font_size is not None) else 0.08
        target_color = sub_data.color if (sub_data and sub_data.color is not None) else (1.0, 1.0, 1.0)

        # font_style=DEFAULT_FONT_STYLE ("Regular") explicit, not left to apply_text_style()'s
        # default preserve-existing-Style behavior: the Media Pool Text+ template this clip is
        # created from can carry a residual Style (e.g. "Light", left over from earlier edits)
        # that preservation would otherwise
        # faithfully copy onto every freshly-created clip - there's no PER-CLIP style to
        # preserve here in the first place, since the clip doesn't exist until this call.
        FusionAnimationEngine.apply_text_style(
            text_tool, font_name=target_font, font_size=target_size, color=target_color, font_style=DEFAULT_FONT_STYLE
        )

        return True

    def _apply_clip_timings(self, clip_item: Any, start_frame: int, end_frame: int) -> bool:
        """Applies start, end, and duration timings to clip item directly via API properties."""
        if clip_item is None:
            return False

        duration = max(1, end_frame - start_frame)

        if hasattr(clip_item, "SetStart"):
            try:
                clip_item.SetStart(start_frame)
            except Exception:
                pass

        if hasattr(clip_item, "SetEnd"):
            try:
                clip_item.SetEnd(end_frame)
            except Exception:
                pass

        if hasattr(clip_item, "SetDuration"):
            try:
                clip_item.SetDuration(duration)
            except Exception:
                pass

        for setter in ["SetProperty", "SetClipProperty"]:
            if hasattr(clip_item, setter):
                try:
                    fn = getattr(clip_item, setter)
                    fn("Start", start_frame)
                    fn("End", end_frame)
                    fn("Duration", duration)
                except Exception:
                    pass

        if hasattr(clip_item, "_start"):
            clip_item._start = start_frame
        if hasattr(clip_item, "_end"):
            clip_item._end = end_frame

        return True

    def _diagnose_timeline_capabilities(self, timeline: Any) -> List[str]:
        """Read-only diagnostic dump of API surface relevant to precise clip placement.

        Precise timing is fragile: a Fusion title/generator inserted by script doesn't
        register a backing MediaPoolItem the way real footage does, so AppendToTimeline
        can't engage without a Media Pool Text+, and InsertFusionTitleIntoTimeline advances
        the playhead sequentially rather than honoring any requested frame. This logs
        exactly which placement-related methods and settings the host exposes.
        """
        log_msgs: List[str] = []
        if timeline is None:
            return log_msgs

        candidates = [
            "SetCurrentTimecode",
            "GetCurrentTimecode",
            "GetStartFrame",
            "GetStartTimecode",
            "GetSetting",
            "InsertFusionGeneratorIntoTimeline",
            "InsertFusionCompositionIntoTimeline",
            "InsertGeneratorIntoTimeline",
        ]
        present = [name for name in candidates if hasattr(timeline, name)]
        missing = [name for name in candidates if name not in present]
        log_msgs.append(f"  - [Diagnostic] Timeline methods present: {present}")
        if missing:
            log_msgs.append(f"  - [Diagnostic] Timeline methods NOT present: {missing}")

        if hasattr(timeline, "GetCurrentTimecode"):
            try:
                log_msgs.append(f"  - [Diagnostic] GetCurrentTimecode() = {timeline.GetCurrentTimecode()!r}")
            except Exception as err:
                log_msgs.append(f"  - [Diagnostic] GetCurrentTimecode() Exception: {err}")

        if hasattr(timeline, "GetStartFrame"):
            try:
                log_msgs.append(f"  - [Diagnostic] GetStartFrame() = {timeline.GetStartFrame()!r}")
            except Exception as err:
                log_msgs.append(f"  - [Diagnostic] GetStartFrame() Exception: {err}")

        if hasattr(timeline, "GetSetting"):
            for setting_name in ["timelineFrameRate", "timelineDropFrameTimecode"]:
                try:
                    val = timeline.GetSetting(setting_name)
                    log_msgs.append(f"  - [Diagnostic] GetSetting('{setting_name}') = {val!r}")
                except Exception as err:
                    log_msgs.append(f"  - [Diagnostic] GetSetting('{setting_name}') Exception: {err}")

        return log_msgs

    def _get_timeline_frame_rate_info(self, timeline: Any) -> Tuple[float, bool]:
        """Reads the timeline's frame rate and drop-frame flag, defaulting to 24fps/non-drop."""
        fps = 24.0
        drop_frame = False
        if timeline is not None and hasattr(timeline, "GetSetting"):
            try:
                fps_val = timeline.GetSetting("timelineFrameRate")
                if fps_val:
                    fps = float(fps_val)
            except Exception:
                pass
            try:
                df_val = timeline.GetSetting("timelineDropFrameTimecode")
                if df_val is not None:
                    drop_frame = str(df_val) in ("1", "True", "true")
            except Exception:
                pass
        return fps, drop_frame

    @staticmethod
    def _frames_to_timecode(frame_number: int, fps: float, drop_frame: bool) -> str:
        """Converts an absolute frame count to an SMPTE timecode string (HH:MM:SS:FF, or ;FF if drop-frame).

        Example: GetCurrentTimecode() == '01:01:36:26' at fps=60/non-drop corresponds to frame
        221786 via this formula - frame 0 corresponds to timecode 00:00:00:00 in the same
        coordinate space SubtitleData.start_frame uses.
        """
        fps_int = int(round(fps))
        frame_number = int(frame_number)

        if drop_frame and fps_int in (30, 60):
            drop_frames_per_min = 2 if fps_int == 30 else 4
            frames_per_10min = fps_int * 60 * 10 - drop_frames_per_min * 9
            frames_per_min = fps_int * 60 - drop_frames_per_min
            d, m = divmod(frame_number, frames_per_10min)
            if m > drop_frames_per_min:
                frame_number += drop_frames_per_min * 9 * d + drop_frames_per_min * (
                    (m - drop_frames_per_min) // frames_per_min
                )
            else:
                frame_number += drop_frames_per_min * 9 * d

        hours = frame_number // (fps_int * 3600)
        minutes = (frame_number // (fps_int * 60)) % 60
        seconds = (frame_number // fps_int) % 60
        frames = frame_number % fps_int
        sep = ";" if drop_frame else ":"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{frames:02d}"

    def _set_generator_duration(self, timeline: Any, project: Any, frames: int) -> Tuple[bool, List[str]]:
        """Best-effort attempt to set the default Generator/Still duration (in frames) so a
        freshly inserted Text+ title adopts the correct duration immediately, since
        TimelineItem has no known way to resize an already-placed clip to an arbitrary
        duration.

        Tries `timeline.SetSetting` first: the 'timelineFrameRate' setting (same "timeline*"
        naming convention as the duration keys below) is readable via `timeline.GetSetting`,
        suggesting these are timeline-scoped settings rather than project-scoped -
        `project.SetSetting` returns falsy for both candidate keys with no exception,
        consistent with the wrong target object rather than a wrong key name. Still falls
        back to `project.SetSetting` in case the timeline rejects them too.
        """
        log_msgs: List[str] = []
        ok_any = False
        for target, target_desc in [(timeline, "timeline"), (project, "project")]:
            if target is None or not hasattr(target, "SetSetting"):
                continue
            for key in ["timelineGeneratorDuration", "timelineStillDuration"]:
                try:
                    res = target.SetSetting(key, str(int(frames)))
                    log_msgs.append(f"  - [Duration] {target_desc}.SetSetting('{key}', '{frames}') = {res!r}")
                    if res:
                        ok_any = True
                except Exception as err:
                    log_msgs.append(f"  - [Duration Exception] {target_desc}.SetSetting('{key}', '{frames}'): {err}")
            if ok_any:
                break

        return ok_any, log_msgs

    def _insert_text_plus_at_playhead(
        self, timeline: Any, project: Any, sub: SubtitleData, fps: float, drop_frame: bool, log_msgs: List[str]
    ) -> Optional[Any]:
        """Moves the playhead to the subtitle's exact start frame, sets the generator default
        duration to match, then inserts a Text+ title there.

        This is necessary because InsertFusionTitleIntoTimeline
        always inserts at the current playhead with a fixed default duration and then advances the
        playhead to the end of what it just inserted — repeated calls without moving the playhead
        first just stack clips back-to-back, ignoring any requested frame.
        """
        if timeline is None or not hasattr(timeline, "SetCurrentTimecode") or not hasattr(
            timeline, "InsertFusionTitleIntoTimeline"
        ):
            return None

        target_tc = self._frames_to_timecode(sub.start_frame, fps, drop_frame)
        try:
            moved = timeline.SetCurrentTimecode(target_tc)
        except Exception as err:
            log_msgs.append(f"  - [Playhead Exception] SetCurrentTimecode('{target_tc}'): {err}")
            return None

        if not moved:
            log_msgs.append(f"  - [Playhead Warning] SetCurrentTimecode('{target_tc}') returned falsy.")

        duration_ok, duration_logs = self._set_generator_duration(timeline, project, sub.duration)
        log_msgs.extend(duration_logs)
        if not duration_ok:
            log_msgs.append(
                f"  - [Duration Warning] Could not set generator duration to {sub.duration} frames; "
                "clip may need manual trimming."
            )

        inserted_item = None
        try:
            res = timeline.InsertFusionTitleIntoTimeline("Text+")
            if res is not None and not isinstance(res, bool):
                inserted_item = res
        except Exception as err:
            log_msgs.append(f"  - [Playhead Insert Exception]: {err}")

        return inserted_item

    def create_text_plus_clips(
        self, timeline: Any, subtitles: List[SubtitleData], target_video_track: Optional[int] = None
    ) -> Tuple[List[Any], List[str]]:
        """Automatically populates the timeline with Text+ clips matching each subtitle block's text, timing, and font styling.

        Primary strategy: reposition the playhead to each subtitle's exact start frame (via
        SetCurrentTimecode) and set the project's default generator duration before inserting,
        since InsertFusionTitleIntoTimeline itself ignores track/frame/duration arguments and
        always lands at the current playhead. Falls back to MediaPool.AppendToTimeline if a genuine pre-existing Text+
        Media Pool item is found (rare, but more reliable when available), and to a best-effort
        diff-based clip search as a last resort on hosts without playhead control.
        Returns (created_clips, log_messages).
        """
        log_msgs: List[str] = []
        if timeline is None or not subtitles:
            return [], log_msgs

        if target_video_track is None:
            target_video_track = get_top_most_unpopulated_video_track_index(timeline)

        self._ensure_and_target_track(timeline, target_video_track)

        # Get Project & MediaPool references
        project = None
        media_pool = None
        if self.resolve is not None and hasattr(self.resolve, "GetProjectManager"):
            try:
                pm = self.resolve.GetProjectManager()
                if pm is not None:
                    project = pm.GetCurrentProject()
                    if project is not None:
                        media_pool = project.GetMediaPool()
            except Exception:
                pass

        log_msgs.extend(self._diagnose_timeline_capabilities(timeline))

        text_plus_item = self._get_media_pool_text_plus_item(media_pool) if media_pool is not None else None
        if text_plus_item is not None:
            log_msgs.append("  - [Setup] Found a Text+ template in the Media Pool; using it for exact placement.")
        else:
            text_plus_item = self._add_text_plus_template(media_pool)
            if text_plus_item is not None:
                log_msgs.append(f"  - [Setup] Added a Text+ template to the Media Pool (bin '{TEMPLATE_BIN}') "
                                "for exact placement – it stays there for next time.")
        if text_plus_item is None:
            log_msgs.append(
                "  - [Setup Warning] No Text+ template in the Media Pool, and Buddy couldn't add one. Timing "
                "and duration will NOT be fully reliable without one. To add it yourself (~30 seconds): drag "
                "a Text+ title from Effects Library > Titles onto any track, then drag that clip from the "
                "timeline into your Media Pool panel, and run this conversion again. Falling back to less "
                "precise playhead-based placement for now."
            )

        default_font = self._default_font_for(subtitles, log_msgs)
        fps, drop_frame = self._get_timeline_frame_rate_info(timeline)
        template_fps = fps
        if text_plus_item is not None:
            try:
                template_fps = float(text_plus_item.GetClipProperty("FPS") or fps)
            except Exception:
                pass
        original_timecode = timeline.GetCurrentTimecode() if hasattr(timeline, "GetCurrentTimecode") else None

        created_clips: List[Any] = []

        for idx, sub in enumerate(subtitles, start=1):
            if sub is None or not sub.text:
                continue

            existing_clips = (
                timeline.GetItemListInTrack("video", target_video_track)
                if hasattr(timeline, "GetItemListInTrack")
                else []
            ) or []

            inserted_item = None

            # Strategy 1: mediaPool.AppendToTimeline([{clipInfo}]) with explicit trackIndex, recordFrame, startFrame, endFrame
            if media_pool is not None and text_plus_item is not None and hasattr(media_pool, "AppendToTimeline"):
                try:
                    clip_info = {
                        "mediaPoolItem": text_plus_item,
                        "startFrame": 0,
                        # In the template's own frames, which may not be the timeline's.
                        "endFrame": source_frames_for(sub.duration, template_fps, fps),
                        "recordFrame": sub.start_frame,
                        "trackIndex": target_video_track,
                        "mediaType": 1,  # Video
                    }
                    res = media_pool.AppendToTimeline([clip_info])
                    if res is not None:
                        if isinstance(res, list) and len(res) > 0:
                            inserted_item = res[0]
                        elif hasattr(res, "GetFusionCompByName") or hasattr(res, "GetStart") or hasattr(res, "GetFusionCompByIndex"):
                            inserted_item = res
                except Exception:
                    pass

            # Strategy Playhead: reposition playhead to the exact start frame, then insert.
            # This is the primary strategy in practice - see docstring above.
            if inserted_item is None:
                inserted_item = self._insert_text_plus_at_playhead(timeline, project, sub, fps, drop_frame, log_msgs)

            # Strategy 2: Direct title insertion fallback if mediaPool item not available
            if inserted_item is None:
                for method_name in ["InsertTitleIntoTimeline", "InsertFusionTitleIntoTimeline", "CreateFusionTitleItem"]:
                    if hasattr(timeline, method_name):
                        try:
                            fn = getattr(timeline, method_name)
                            res = fn("Text+", target_video_track, sub.start_frame)
                            if res is None or not res:
                                res = fn("Text+", target_video_track)
                            if res is None or not res:
                                res = fn("Text+")
                            if res is not None:
                                if isinstance(res, list) and len(res) > 0:
                                    inserted_item = res[0]
                                elif hasattr(res, "GetFusionCompByName") or hasattr(res, "GetStart") or hasattr(res, "GetFusionCompByIndex"):
                                    inserted_item = res
                                break
                        except Exception:
                            pass

            # Strategy 3: Find newly spawned TimelineItem on target track if insertion returned True/Boolean
            if inserted_item is None or isinstance(inserted_item, bool):
                inserted_item = self._find_new_clip_on_track(
                    timeline, target_video_track, existing_clips, sub.start_frame
                )

            # Explicit None check
            if inserted_item is not None and not isinstance(inserted_item, bool):
                self._apply_clip_timings(inserted_item, sub.start_frame, sub.end_frame)
                text_styled = self._apply_text_and_styling(inserted_item, sub.text, sub_data=sub,
                                                           default_font=default_font)
                if not text_styled:
                    # No Text+ to reach: Resolve's answer when the spot is already taken. Not
                    # counted as made, and not deleted either - it may be a clip already there.
                    log_msgs.append(f"  - [FAIL] [{idx}/{len(subtitles)}] Couldn't put the words on a Text+ clip at "
                                    f"frame {sub.start_frame} for '{sub.text}' – is something already there?")
                    continue
                created_clips.append(inserted_item)
                style_status = "styled"

                actual_start = inserted_item.GetStart() if hasattr(inserted_item, "GetStart") else None
                actual_end = inserted_item.GetEnd() if hasattr(inserted_item, "GetEnd") else None
                mismatch_note = ""
                if actual_start is not None and actual_end is not None and (
                    actual_start != sub.start_frame or actual_end != sub.end_frame
                ):
                    mismatch_note = f" [MISMATCH: actually landed at {actual_start}-{actual_end}]"

                actual_track = self._find_clip_track_index(timeline, inserted_item, target_video_track)
                track_note = ""
                if actual_track is not None and actual_track != target_video_track:
                    track_note = f" [TRACK MISMATCH: actually on Track {actual_track}]"

                log_msgs.append(
                    f"  - [{idx}/{len(subtitles)}] Spawned Text+ ({style_status}) on Track {target_video_track} "
                    f"at frame {sub.start_frame}-{sub.end_frame} (dur: {sub.duration}): '{sub.text}'{mismatch_note}{track_note}"
                )
            else:
                log_msgs.append(f"  - [FAIL] [{idx}/{len(subtitles)}] Failed to insert Text+ clip for '{sub.text}'")

        if original_timecode and hasattr(timeline, "SetCurrentTimecode"):
            try:
                timeline.SetCurrentTimecode(original_timecode)
            except Exception:
                pass

        return created_clips, log_msgs
