#!/usr/bin/env python3
"""
What the Sync tab says after each operation, without Qt: the engines'
stats turned into log lines. Kept apart from the page so the wording can be
tested against real stats dicts, and so page.py stays about wiring.

Each function returns [(text, kind)], kind being "success", "info" or
"warn" - the page appends them to the Sync tab's log in order.
"""

METHOD_LABELS = {"timecode": "timecode", "waveform": "waveform"}

INFO_TEXT = (
    "Sync everything lines up every clip on the open timeline, places audio that "
    "carries no timecode by its waveform, packs the result onto the fewest tracks "
    "and pulls it to the start of the timeline. It's imported as one new "
    "timeline: the timeline you started from is never changed, so it's also your "
    "backup.\n\n"
    "Sync by:\n"
    "  - Timecode uses each clip's embedded timecode. A clip reading 00:00:00:00 is "
    "treated as having none (that's what a camera writes when nothing set it) and is "
    "left for the waveform pass.\n"
    "  - Waveform matches clips by their audio, and needs no timecode at all.\n\n"
    "Nothing is deleted and no gaps are closed by Sync everything – those are edit "
    "decisions. They're in the separate steps, along with Expand (one clip per "
    "track, for working by hand).\n\n"
    "The steps:\n"
    "  - Expand spreads every clip onto its own track, keeping position and trim.\n"
    "  - Align moves each clip to its timecode, relative to the earliest one.\n"
    "  - Sync external audio places recordings with no usable timecode (lav packs, "
    "field recorders) against the camera clips' scratch audio. Run it after Align.\n"
    "  - Collapse packs clips onto the fewest tracks without changing when anything "
    "plays, optionally deleting audio clips that are silent throughout.\n"
    "  - Shift pulls everything to the start, optionally closing the gaps between "
    "blocks of footage. Gaps are judged across all tracks, so sync is kept.\n\n"
    "Every step writes a new timeline. Anything that reads audio needs ffmpeg "
    "(set its path in Settings if Buddy can't find it)."
)


def _created(name):
    return (f'Created "{name}" – the timeline you started from is unchanged.', "success")


def _warnings(warnings):
    return [(f"Warning: {w}", "warn") for w in warnings or []]


def assemble(stats, method, video_tracks, audio_tracks, name, warnings):
    lines = []
    if stats.get("method_used") == "waveform":
        lines.append((
            f"Found {stats.get('groups', 0)} group(s) of clips that share audio, holding "
            f"{stats.get('placed', 0)} of {stats.get('considered', 0)} clip(s). Each group "
            "is laid out separately – nothing relates one group to another.", "info"))
    else:
        lines.append((f"Aligned {stats['aligned']} of {stats['total']} clip(s) by "
                      f"{METHOD_LABELS.get(method, method)}.", "info"))
    if stats.get("audio_total"):
        lines.append((f"Placed {stats['audio_matched']} of {stats['audio_total']} clip(s) "
                      "that carried no timecode, by waveform.", "info"))
    if stats.get("silent_removed"):
        lines.append((f"Left out {stats['silent_removed']} silent audio clip(s).", "info"))
    lines.append((f"Packed onto {video_tracks} video and {audio_tracks} audio track(s).", "info"))
    lines.append(_created(name))
    return lines + _warnings(warnings)


def expand(stats, warnings):
    return [
        (f"Expanded {stats['clips']} clip(s) onto {stats['video_tracks']} video and "
         f"{stats['audio_tracks']} audio track(s).", "info"),
        _created(stats["name"]),
    ] + _warnings(warnings)


def align(stats, anchor_name, anchor_frame, shifted, warnings):
    lines = [(f"Aligned {stats['moved']} of {stats['total']} clip(s) by timecode.", "info")]
    if anchor_name is not None:
        lines.append((f'Anchored on "{anchor_name}" – it stayed at frame {int(anchor_frame)}.', "info"))
    if shifted:
        lines.append((f"Moved the whole group {shifted} frame(s) right so nothing landed "
                      "before the start of the timeline (sync is unchanged).", "info"))
    lines.append(_created(stats["name"]))
    return lines + _warnings(list(warnings or []) + list(stats.get("warnings", [])))


def external(stats, warnings):
    placed = (f"Placed {stats['matched']} of {stats['external']} clip(s) that timecode "
              f"couldn't, against the audio of {stats['scratch_used']} clip(s) it could")
    lines = [(f"{placed} across {stats['sessions']} separate stretches of footage."
              if stats.get("sessions", 1) > 1 else f"{placed}.", "info")]
    if stats.get("unmatched"):
        lines.append((f"{stats['unmatched']} couldn't be placed and were left where they were.", "warn"))
    lines.append(_created(stats["name"]))
    return lines + _warnings(warnings)


def collapse(kept, dropped, was, now, name, warnings):
    lines = [(f"Collapsed {kept} clip(s) from {was[0]} video / {was[1]} audio tracks onto "
              f"{now[0]} video and {now[1]} audio.", "info")]
    if dropped:
        lines.append((f"Left out {dropped} silent audio clip(s).", "info"))
    lines.append(_created(name))
    return lines + _warnings(warnings)


def shift(stats, close_gaps):
    if stats["lead_in_removed"]:
        lines = [(f"Moved {stats['clips']} clip(s) left by {stats['lead_in_removed']} frame(s).", "info")]
    else:
        lines = [(f"{stats['clips']} clip(s) already started at the beginning.", "info")]
    if close_gaps and stats["gap_frames_removed"]:
        lines.append((f"Closed {stats['gaps']} gap(s), removing {stats['gap_frames_removed']} "
                      "frame(s) of dead air.", "info"))
    elif close_gaps:
        lines.append(("There were no gaps between clips to close.", "info"))
    lines.append(_created(stats["name"]))
    return lines + _warnings(stats.get("warnings"))


def timeline_summary(clips):
    """{"clips", "video", "audio_only"} for the logical clips on a timeline."""
    return {
        "clips": len(clips),
        "video": sum(1 for c in clips if c.has_video),
        "audio_only": sum(1 for c in clips if c.has_audio and not c.has_video),
    }
