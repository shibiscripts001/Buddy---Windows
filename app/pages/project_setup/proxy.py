#!/usr/bin/env python3
"""
The Proxy tab's rules, without Qt: which clips a scope covers, where each
clip's proxy file goes, the ffmpeg command that renders it, and what a
run reports.

Proxies are .mov files written into a "Proxy" folder beside each source -
the same convention Resolve's own Generate Proxy Media uses, so a project
stays portable - then linked in Resolve with MediaPoolItem.LinkProxyMedia,
the call behind the manual "Relink Proxy Media". Source files are never
touched: the proxy is a new file, and only Resolve's own proxy toggle ever
swaps a clip onto it.

Only video clips are transcoded - an audio-only clip never plays from a
proxy, so it has nothing to gain. The audio inside a video clip is
stream-copied rather than re-encoded, all audio streams included, so a
clip keeps its sound (and a multicam angle keeps its tracks) when Resolve
plays the proxy.

Resolve only links a proxy whose timecode matches the clip's, and ffmpeg
doesn't carry a source's timecode over by itself - a Sony camera keeps it
in its own metadata track (rtmd), which a proxy doesn't copy. So every
render is stamped with the clip's start timecode as Resolve reads it
(-timecode), which is exactly what the link is checked against.

A clip whose proxy file already exists on disk is linked, not re-rendered
- delete the file to force a re-render - unless its timecode doesn't
match the clip's: then it's one Resolve would refuse, and it's rendered
again in its place. Renders write to a ".buddy-part"
file beside the destination and only rename it into place once ffmpeg
succeeds, so a cancelled or failed run never leaves a half-written proxy
where Resolve could link it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading

from .ffmpeg_utils import _ffprobe_path, _no_feedback_cursor_kwargs
from .importer import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .resolve_ext import _is_real_media_clip

PROXY_DIRNAME = "Proxy"
PROXY_SUFFIX = "_proxy"
PROXY_EXT = ".mov"
PART_EXT = ".buddy-part.mov"

# Resolution is a pure scale of the picture; the frame rate is never
# touched (a proxy must match its source's rate to be linkable), so
# "original" is simply no scale filter at all. The trunc()*2 forms force
# even dimensions, which libx264 requires.
RESOLUTIONS = {
    "original": {"label": "Original", "vf": None},
    "half": {"label": "Half", "vf": "scale=trunc(iw/4)*2:trunc(ih/4)*2"},
    "quarter": {"label": "Quarter", "vf": "scale=trunc(iw/8)*2:trunc(ih/8)*2"},
}
# The formats offered, in the order the dropdown lists them. The long-GOP
# ones (H.264, H.265) make small files but take more work to decode while
# scrubbing; the intra-frame ones (ProRes, DNxHR, CineForm) are heavier on
# disk and light to play. Every one takes stream-copied audio, so a clip's
# sound survives the swap unchanged - and every one was checked to link in
# Resolve Studio 21.1 (half-size FX3 H.264 4:2:2 source, 2026-09-30).
# "h264" and "prores" keep their ids from before there were more, so a
# saved choice still means the same format.
_PRORES = ["-c:v", "prores_ks", "-pix_fmt", "yuv422p10le", "-vendor", "apl0", "-profile:v"]
_DNXHR = ["-c:v", "dnxhd", "-pix_fmt", "yuv422p", "-profile:v"]
CODECS = {
    "h264": {"label": "H.264", "hint": "Small files that play anywhere.",
             "args": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                      "-pix_fmt", "yuv420p"]},
    "h265": {"label": "H.265 (HEVC)", "hint": "The smallest files, but the heaviest to decode while scrubbing.",
             "args": ["-c:v", "libx265", "-preset", "fast", "-crf", "26", "-pix_fmt", "yuv420p",
                      "-tag:v", "hvc1", "-x265-params", "log-level=error"]},
    "prores": {"label": "ProRes 422 Proxy", "hint": "Light to play back, larger files – Resolve's usual proxy format.",
               "args": _PRORES + ["0"]},
    "prores_lt": {"label": "ProRes 422 LT", "hint": "Better picture than Proxy, about twice the size.",
                  "args": _PRORES + ["1"]},
    "prores_422": {"label": "ProRes 422", "hint": "Close to the original's quality – large files.",
                   "args": _PRORES + ["2"]},
    "dnxhr_lb": {"label": "DNxHR LB", "hint": "Avid's low-bandwidth format: light to play, modest files.",
                 "args": _DNXHR + ["dnxhr_lb"]},
    "dnxhr_sq": {"label": "DNxHR SQ", "hint": "Avid's standard quality – larger files.",
                 "args": _DNXHR + ["dnxhr_sq"]},
    "dnxhr_hq": {"label": "DNxHR HQ", "hint": "Avid's high quality – large files.",
                 "args": _DNXHR + ["dnxhr_hq"]},
    "cineform": {"label": "CineForm", "hint": "GoPro's intra-frame format: light to play, between LB and SQ in size.",
                 "args": ["-c:v", "cfhd", "-quality", "medium", "-pix_fmt", "yuv422p10le"]},
}


def codec_choices():
    """The dropdown's entries, in order: [{id, label, hint}]."""
    return [{"id": key, "label": c["label"], "hint": c["hint"]} for key, c in CODECS.items()]
DEFAULT_RESOLUTION = "half"
DEFAULT_CODEC = "h264"

# What a run can target. "timeline" needs Resolve 21.0.4's
# Timeline.GetSelectedClips; the tab hides it when the open Resolve
# can't (the page checks with timeline_can_select).
SCOPES = ("selection", "timeline", "bin", "all")
DEFAULT_SCOPE = "selection"

SCOPE_LABELS = {
    "selection": "the Media Pool selection",
    "timeline": "the timeline selection",
    "bin": "the open bin",
    "all": "every clip in the project",
}


def scope_label(scope, bin_name=None, timeline_name=None, recursive=False):
    """How to name the scope in the log and the job label: a plain phrase,
    with the live bin or timeline name when there is one."""
    if scope == "bin":
        name = (bin_name or "").strip()
        if not name:
            return "the open bin"
        what = "Master" if name == "Master" else f"bin '{name}'"
        if recursive:
            what += " and its sub-bins"
        return what
    if scope == "timeline" and timeline_name:
        return f"the selection on '{timeline_name}'"
    return SCOPE_LABELS.get(scope, scope)


INFO_TEXT = (
    "Renders lightweight copies of the project's video clips with ffmpeg and "
    "links them in Resolve as proxy media - the same workflow as Resolve's own "
    "Generate Proxy Media, for when a machine struggles to play the originals "
    "back.\n\n"
    "Proxies are .mov files written into a 'Proxy' folder beside each source "
    "(Resolve's own convention), then linked with the same call the manual "
    "relink uses. Source files are never touched, and each clip's audio is "
    "stream-copied, so nothing changes but the playback load.\n\n"
    "Which clips: the Media Pool selection, the timeline selection (Resolve "
    "21.0.4 or later), the open bin (optionally with its sub-bins), or every "
    "clip in the project. Only video files are transcoded - audio-only clips "
    "never play from a proxy.\n\n"
    "Resolution and format are chosen here. H.264 and H.265 make the smallest "
    "files but take more work to decode while scrubbing; ProRes, DNxHR and "
    "CineForm are larger on disk and much lighter to play. Each proxy carries "
    "its clip's timecode, which Resolve needs to link it. A "
    "clip whose proxy file already exists is linked, not re-rendered - unless "
    "the file's timecode is wrong, when it's rendered again; delete the file "
    "to render it again anyway. ffmpeg is needed - install it or set its "
    "path in Settings."
)


class ProxyError(ValueError):
    """A run-stopping problem, worded to be read by a person."""


# ------------------------------------------------------------- timecode

# "19:45:12:08", or "19:45:12;08" for drop frame - Resolve's Start TC and
# what ffmpeg's -timecode takes.
_TIMECODE = re.compile(r"\d{2}:\d{2}:\d{2}[:;.]\d{2,3}")
NO_TIMECODE = "00:00:00:00"   # what Resolve reads for a file that has none


def clean_timecode(raw):
    """A start timecode ffmpeg can stamp, or "" if raw isn't one."""
    text = str(raw or "").strip()
    return text if _TIMECODE.fullmatch(text) else ""


def same_timecode(a, b):
    """Whether two start timecodes are the same frame. Drop-frame ";" and
    "." separators count as ":", and a file with no timecode at all starts
    at 00:00:00:00, as Resolve reads it."""
    def norm(tc):
        return (clean_timecode(tc) or NO_TIMECODE).replace(";", ":").replace(".", ":")
    return norm(a) == norm(b)


def file_timecode(ffmpeg_path, path, timeout=30):
    """A media file's start timecode - from any stream's tags or the
    container's (a proxy's tmcd track, a Sony clip's rtmd) - or "" when it
    has none or can't be read. ffprobe when it ships beside ffmpeg; else
    `ffmpeg -i`, which prints the same tags to stderr."""
    ffprobe = _ffprobe_path(ffmpeg_path)
    try:
        if ffprobe:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "stream_tags=timecode:format_tags=timecode",
                 "-of", "json", path],
                capture_output=True, timeout=timeout, **_no_feedback_cursor_kwargs())
            doc = json.loads(result.stdout or b"{}") if result.returncode == 0 else {}
            for tags in [s.get("tags") or {} for s in doc.get("streams") or []] + [
                    (doc.get("format") or {}).get("tags") or {}]:
                tc = clean_timecode(tags.get("timecode"))
                if tc:
                    return tc
            return ""
        result = subprocess.run([ffmpeg_path, "-hide_banner", "-i", path], capture_output=True,
                                timeout=timeout, **_no_feedback_cursor_kwargs())
    except (subprocess.SubprocessError, ValueError, OSError):
        return ""
    found = re.search(r"^\s*timecode\s*:\s*(\S+)", (result.stderr or b"").decode("utf-8", "replace"),
                      re.MULTILINE)
    return clean_timecode(found.group(1)) if found else ""


# ------------------------------------------------------------- collecting

def _entry(clip):
    """{id, name, path, type, tc} for a MediaPoolItem, or None if the clip
    can't be described at all. tc: its start timecode as Resolve reads it
    ("" if it gave none) - what a proxy has to match to be linked."""
    try:
        props = clip.GetClipProperty() or {}
    except Exception:
        return None
    path = str(props.get("File Path") or "")
    name = str(props.get("Clip Name") or "")
    if not name:
        try:
            name = str(clip.GetName() or "")
        except Exception:
            name = ""
    try:
        clip_id = str(clip.GetUniqueId() or "")
    except Exception:
        clip_id = ""
    if not clip_id:
        clip_id = name or path
    return {"id": clip_id, "name": name or os.path.basename(path) or "Untitled",
            "path": path, "type": str(props.get("Type") or ""),
            "tc": clean_timecode(props.get("Start TC"))}


def entries(clips):
    """Deduped entries for `clips` plus the objects behind them, so the
    link pass can find each clip again after the worker finishes:
    ([{id, name, path, type}], {id: clip})."""
    result, objects, seen = [], {}, set()
    for clip in clips:
        entry = _entry(clip)
        if entry is None or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        result.append(entry)
        objects[entry["id"]] = clip
    return result, objects


def is_video(entry):
    """Whether a clip's media is a video file. By extension first - a
    .braw or .mp4 is video whatever the Type property says, and an image
    extension (a still, or one frame of a sequence) is never video -
    then by Resolve's own Type value ("video" or "movie")."""
    path = entry["path"]
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return True
    if ext in IMAGE_EXTENSIONS:
        return False
    return entry["type"].strip().lower() in ("video", "movie")


def selected_pool(project):
    """The Media Pool's selection. Raises ProxyError on a Resolve old
    enough not to read the selection at all."""
    pool = project.GetMediaPool()
    try:
        clips = pool.GetSelectedClips()
    except Exception as exc:
        raise ProxyError(
            "This Resolve version could not read the Media Pool selection. "
            "Update Resolve to use the selected clips here."
        ) from exc
    clips = [c for c in (clips or []) if _is_real_media_clip(c)]
    return entries(clips)


def selected_timeline(timeline):
    """The open timeline's selection (Resolve 21.0.4 or later), mapped
    back to their Media Pool items - a timeline item is not itself a
    MediaPoolItem, and items with none (titles, generators) have nothing
    to proxy."""
    if "GetSelectedClips" not in dir(timeline):
        raise ProxyError("Selected on the timeline needs DaVinci Resolve 21.0.4 or later.")
    items = []
    for item in timeline.GetSelectedClips() or []:
        try:
            media = item.GetMediaPoolItem()
        except Exception:
            media = None
        if media is not None:
            items.append(media)
    return entries(items)


def timeline_can_select(timeline):
    """Whether this Resolve's Timeline objects expose GetSelectedClips
    (21.0.4+). dir() is the truth on Resolve's wrapper objects - hasattr
    dispatches and always answers yes."""
    return timeline is not None and "GetSelectedClips" in dir(timeline)


# ------------------------------------------------------------------ plan

def _claim(src, claimed):
    """The .mov path this source's proxy should use, and whether that's a
    render or an existing file to adopt.

    `claimed` tracks, per (folder, lowercase stem), which proxy names this
    run has handed out and whether the stem's first file was adopted. The
    first clip of a stem may adopt a proxy that already exists on disk - a
    previous run's output, so relink rather than re-render. Any LATER clip
    with the same stem must not: an existing file under its number might
    be another clip's proxy, so it moves on to the next free name instead.

    Returns ("render", path) or ("existing", path)."""
    folder = os.path.dirname(src) or "."
    stem = os.path.splitext(os.path.basename(src))[0]
    base = stem + PROXY_SUFFIX
    state = claimed.setdefault((folder, base.lower()), {"adopted": False, "names": set()})
    n = 1
    while True:
        name = f"{base}{PROXY_EXT}" if n == 1 else f"{base}_{n}{PROXY_EXT}"
        key = name.lower()
        if key in state["names"]:
            n += 1
            continue
        path = os.path.join(folder, PROXY_DIRNAME, name)
        if not os.path.exists(path):
            state["names"].add(key)
            return "render", path
        if n == 1 and not state["adopted"]:
            state["adopted"] = True
            state["names"].add(key)
            return "existing", path
        n += 1


def plan(videos):
    """Where each video clip's proxy goes. `videos` is the deduped entry
    list of one scope's video clips; returns
    {"render": [{id, name, path, dst}], "existing": [{id, name, path, dst}],
     "skipped": [sentence, ...]}.

    Skips are finished English sentences (they go straight into the
    Activity log) rather than codes, so the reason reads the same
    everywhere it's shown."""
    render, existing, skipped = [], [], []
    claimed = {}
    for entry in sorted(videos, key=lambda e: ((e["name"] or "").lower(), e["path"] or "")):
        src = entry["path"]
        if not src:
            skipped.append(f"Skipped \"{entry['name']}\" – no file behind this clip.")
            continue
        if not os.path.isfile(src):
            skipped.append(f"Skipped \"{entry['name']}\" – the file is offline.")
            continue
        kind, dst = _claim(src, claimed)
        target = {**entry, "dst": dst}
        (render if kind == "render" else existing).append(target)
    return {"render": render, "existing": existing, "skipped": skipped}


# --------------------------------------------------------------- rendering

_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


def terminate_active():
    """Kills every proxy render currently running. Returns how many.

    Called on cancel (and on quit): without it, stopping a batch leaves
    the current ffmpeg still reading and encoding media - the work is
    abandoned, but the reading isn't. Safe from any thread: killing a
    process the owning thread is blocked on just makes its wait return,
    and every caller already treats a killed render as a failed clip."""
    with _ACTIVE_LOCK:
        running = list(_ACTIVE)
    for process in running:
        try:
            process.kill()
        except Exception:
            pass
    return len(running)


def build_command(ffmpeg_path, src, out_path, resolution, codec, timecode=""):
    """The ffmpeg invocation that renders one proxy. No -r anywhere: the
    frame rate is left exactly as the source plays it, and -timecode
    stamps the clip's start timecode - both are what make a proxy
    linkable. -map 0:a? keeps every audio stream (a multicam angle's
    tracks all survive) and is optional, so a video with no sound at all
    still renders."""
    try:
        vf = RESOLUTIONS[resolution]["vf"]
        codec_args = CODECS[codec]["args"]
    except KeyError as exc:
        raise ProxyError("Unknown proxy resolution or codec.") from exc
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", src]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-map", "0:v:0", "-map", "0:a?", "-c:a", "copy"]
    cmd += codec_args
    timecode = clean_timecode(timecode)
    if timecode:
        cmd += ["-timecode", timecode]
    cmd.append(out_path)
    return cmd


def _run_process(cmd):
    """Runs one ffmpeg to completion. No timeout by design: a legitimate
    render of a long file on a slow machine can take hours, and
    cancellation (terminate_active) is the safety valve that always works."""
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               **_no_feedback_cursor_kwargs())
    with _ACTIVE_LOCK:
        _ACTIVE.add(process)
    try:
        _out, err = process.communicate()
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(process)
    return process.returncode, err


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _last_error_line(stderr_bytes):
    """ffmpeg's most useful line - the last non-empty one on stderr -
    kept short enough to read in a log."""
    text = (stderr_bytes or b"").decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1][:160]


def render_one(ffmpeg_path, src, dst, resolution, codec, run=None, timecode=""):
    """Renders one proxy to dst, or raises ProxyError with a readable
    reason. `run` (used by the tests) replaces the real ffmpeg call.

    The encode writes to '<dst>.buddy-part.mov' and renames it into place
    only on success, so a cancelled or failed run never leaves a
    half-written proxy that a later run could adopt."""
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    except OSError as exc:
        raise ProxyError(
            f"Couldn't create the 'Proxy' folder beside \"{os.path.basename(src)}\": {exc}"
        ) from exc
    part = dst + PART_EXT
    cmd = build_command(ffmpeg_path, src, part, resolution, codec, timecode)
    code, err = (run or _run_process)(cmd)
    if code != 0:
        _unlink(part)
        detail = _last_error_line(err)
        raise ProxyError(f"ffmpeg couldn't render \"{os.path.basename(src)}\""
                         + (f": {detail}" if detail else ""))
    os.replace(part, dst)
    return dst


# --------------------------------------------------------------- reporting

def render_report(results):
    """Activity lines for the worker's per-clip results, in run order:
    one summary line, then a line per failure. A killed render (cancel)
    never reaches here - the job reports itself cancelled instead."""
    ok = [r for r in results if r["ok"]]
    lines = []
    if ok:
        if len(ok) == 1 and len(results) == 1:
            lines.append(("Rendered 1 proxy.", "success"))
        else:
            lines.append((f"Rendered {len(ok)} of {len(results)} proxies.", "success"))
    for r in results:
        if not r["ok"]:
            lines.append((str(r["error"]), "error"))
    return lines


def link_report(linked, refused, existing, redone=0, refused_existing=()):
    """Activity lines for the link pass: linked count, then one line per
    clip Resolve refused, then how many files already existed - and how
    many of those had the wrong timecode and were rendered again."""
    lines = []
    if linked == 1:
        lines.append(("Linked 1 clip to its proxy.", "success"))
    elif linked:
        lines.append((f"Linked {linked} clips to their proxies.", "success"))
    for name in refused:
        lines.append((f"Resolve wouldn't link \"{name}\".", "error"))
    for name in refused_existing:
        lines.append((f"Resolve wouldn't link the proxy file already there for \"{name}\" – delete it from "
                      "the Proxy folder to render it again.", "error"))
    if redone == 1:
        lines.append(("1 proxy file already there had the wrong timecode – rendered again.", "info"))
    elif redone:
        lines.append((f"{redone} proxy files already there had the wrong timecode – rendered again.", "info"))
    if existing == 1:
        lines.append(("1 clip already had a proxy file – linked to it instead of rendering.", "info"))
    elif existing:
        lines.append((f"{existing} clips already had proxy files – linked instead of rendering.", "info"))
    return lines