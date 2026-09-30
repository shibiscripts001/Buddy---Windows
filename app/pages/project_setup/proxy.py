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
import sys
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
# cuda_vf: the same on the graphics card (the GPU formats), which also
# turns the picture into the 8-bit 4:2:0 NVENC takes.
RESOLUTIONS = {
    "original": {"label": "Original", "vf": None, "cuda_vf": "scale_cuda=format=yuv420p"},
    "half": {"label": "Half", "vf": "scale=trunc(iw/4)*2:trunc(ih/4)*2",
             "cuda_vf": "scale_cuda=trunc(iw/4)*2:trunc(ih/4)*2:format=yuv420p"},
    "quarter": {"label": "Quarter", "vf": "scale=trunc(iw/8)*2:trunc(ih/8)*2",
                "cuda_vf": "scale_cuda=trunc(iw/8)*2:trunc(ih/8)*2:format=yuv420p"},
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
    # NVIDIA's encoders (Windows, an NVIDIA card). The clip is decoded,
    # scaled and encoded on the graphics card, so the CPU stays free for
    # Resolve: a minute of 4K on an RTX 5080 took 1-3 s of CPU time, not
    # 100-230 s, and a drone's H.265 rendered 4x faster (2026-09-30). A source the card can't decode (ProRes, DNxHR, BRAW, 4:2:2
    # on older cards) is decoded on the CPU and still encoded on the card; a
    # PC without NVENC renders the "cpu" format instead (render_plan).
    "h264_nvenc": {"label": "H.264 (NVIDIA GPU)",
                   "hint": "Rendered on the graphics card – much faster, and leaves the CPU free for Resolve.",
                   "args": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "28", "-b:v", "0"],
                   "gpu": "h264_nvenc", "cpu": "h264"},
    "hevc_nvenc": {"label": "H.265 (NVIDIA GPU)",
                   "hint": "Smaller files than H.264, rendered on the graphics card.",
                   "args": ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "30", "-b:v", "0",
                            "-tag:v", "hvc1"],
                   "gpu": "hevc_nvenc", "cpu": "h265"},
}


def codec_choices(platform=None):
    """The dropdown's entries, in order: [{id, label, hint}]. NVIDIA's
    only on Windows - there are no NVIDIA cards in today's Macs."""
    platform = platform or sys.platform
    return [{"id": key, "label": c["label"], "hint": c["hint"]} for key, c in CODECS.items()
            if platform == "win32" or not c.get("gpu")]


_GPU_CHECKED = {}   # (ffmpeg path, encoder) -> whether it works here


def gpu_available(ffmpeg_path, encoder, run=None):
    """Whether this ffmpeg can encode with `encoder` on this PC - a build
    can include NVENC with no NVIDIA card to run it, so it's tried on a
    tenth of a second of black. Asked once per ffmpeg and encoder."""
    key = (ffmpeg_path, encoder)
    if key not in _GPU_CHECKED:
        try:
            result = (run or subprocess.run)([ffmpeg_path, "-hide_banner", "-v", "error", "-f", "lavfi",
                          "-i", "color=black:s=256x144:d=0.1", "-c:v", encoder, "-f", "null", "-"],
                         capture_output=True, timeout=30, **_no_feedback_cursor_kwargs())
            _GPU_CHECKED[key] = result.returncode == 0
        except (subprocess.SubprocessError, OSError):
            _GPU_CHECKED[key] = False
    return _GPU_CHECKED[key]


def render_plan(ffmpeg_path, codec):
    """(the format a run really renders, what to tell the user or ""): a
    GPU format this PC can't encode becomes its CPU one."""
    gpu = CODECS.get(codec, {}).get("gpu")
    if not gpu or gpu_available(ffmpeg_path, gpu):
        return codec, ""
    fallback = CODECS[codec]["cpu"]
    return fallback, (f"This PC can't encode {CODECS[codec]['label']} (it needs an NVIDIA graphics card) – "
                      f"rendered as {CODECS[fallback]['label']} on the CPU instead.")
DEFAULT_RESOLUTION = "half"
DEFAULT_CODEC = "h264"

# What a run can target. "timeline" is the clips selected on the open
# timeline, or every clip on it when none are (or when this Resolve can't
# say - Timeline.GetSelectedClips is 21.0.4 and later).
SCOPES = ("selection", "timeline", "bin", "all")
DEFAULT_SCOPE = "selection"

SCOPE_LABELS = {
    "selection": "the Media Pool selection",
    "timeline": "the current timeline",
    "bin": "the open bin",
    "all": "every clip in the project",
}


def scope_label(scope, bin_name=None, timeline_name=None, recursive=False, timeline_selection=False):
    """How to name the scope in the log and the job label: a plain phrase,
    with the live bin or timeline name when there is one.
    timeline_selection: the timeline run took only its selected clips."""
    if scope == "bin":
        name = (bin_name or "").strip()
        if not name:
            return "the open bin"
        what = "Master" if name == "Master" else f"bin '{name}'"
        if recursive:
            what += " and its sub-bins"
        return what
    if scope == "timeline" and timeline_name:
        return f"the selection on '{timeline_name}'" if timeline_selection else f"every clip on '{timeline_name}'"
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
    "Which clips: the Media Pool selection, the current timeline (the clips "
    "selected on it, or all of them when none are), the open bin (optionally "
    "with its sub-bins), or every clip in the project. Only video files are transcoded - audio-only clips "
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
            "tc": clean_timecode(props.get("Start TC")),
            # Its proxy as Resolve has it: "None", or the proxy's size.
            "proxy": str(props.get("Proxy") or ""),
            "proxy_path": str(props.get("Proxy Media Path") or "")}


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


def timeline_clips(timeline):
    """The current timeline's clips: the ones selected on it, or every
    clip on its video tracks when none are - or when this Resolve can't
    read a timeline selection (GetSelectedClips is 21.0.4+; dir() is the
    truth on Resolve's wrapper objects - hasattr always answers yes).
    Mapped back to their Media Pool items - a timeline item is not itself
    a MediaPoolItem, and items with none (titles, generators) have nothing
    to proxy. Returns (entries, objects, from_selection)."""
    items = []
    if "GetSelectedClips" in dir(timeline):
        items = list(timeline.GetSelectedClips() or [])
    from_selection = bool(items)
    if not items:
        for index in range(1, int(timeline.GetTrackCount("video") or 0) + 1):
            items.extend(timeline.GetItemListInTrack("video", index) or [])
    media = []
    for item in items:
        try:
            clip = item.GetMediaPoolItem()
        except Exception:
            clip = None
        if clip is not None:
            media.append(clip)
    found, objects = entries(media)
    return found, objects, from_selection


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


def build_command(ffmpeg_path, src, out_path, resolution, codec, timecode="", gpu_decode=False):
    """The ffmpeg invocation that renders one proxy. No -r anywhere: the
    frame rate is left exactly as the source plays it, and -timecode
    stamps the clip's start timecode - both are what make a proxy
    linkable. -map 0:a? keeps every audio stream (a multicam angle's
    tracks all survive) and is optional, so a video with no sound at all
    still renders.

    gpu_decode (a GPU format only): decode and scale on the graphics card
    too; without it the CPU decodes and scales, and hands NVENC 8-bit
    4:2:0 frames."""
    try:
        codec_args = list(CODECS[codec]["args"])
        gpu = bool(CODECS[codec].get("gpu"))
        vf = RESOLUTIONS[resolution]["cuda_vf" if gpu and gpu_decode else "vf"]
    except KeyError as exc:
        raise ProxyError("Unknown proxy resolution or codec.") from exc
    cmd = [ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-y"]
    if gpu and gpu_decode:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif gpu:
        codec_args += ["-pix_fmt", "yuv420p"]
    cmd += ["-i", src]
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
    run = run or _run_process
    code, err = 1, b""
    if CODECS.get(codec, {}).get("gpu"):
        # All on the graphics card first; a source it can't decode fails
        # at once, and goes again decoded on the CPU.
        code, err = run(build_command(ffmpeg_path, src, part, resolution, codec, timecode, gpu_decode=True))
    if code != 0:
        code, err = run(build_command(ffmpeg_path, src, part, resolution, codec, timecode))
    if code != 0:
        _unlink(part)
        detail = _last_error_line(err)
        raise ProxyError(f"ffmpeg couldn't render \"{os.path.basename(src)}\""
                         + (f": {detail}" if detail else ""))
    os.replace(part, dst)
    return dst


# ------------------------------------------------------------ status

STATUS_ROWS_MAX = 500   # rows the status table lists; the counts cover them all


def proxy_state(entry):
    """"linked", "offline" or "none". Resolve goes on reporting a proxy
    whose file has gone as linked (tested: 21.1), so Buddy looks for the
    file itself."""
    path = entry.get("proxy_path") or ""
    if not path and entry.get("proxy", "None") in ("", "None"):
        return "none"
    return "linked" if path and os.path.isfile(path) else "offline"


def status_report(videos):
    """What the status view shows for a scope's video clips:
    {"counts": {linked, offline, none}, "rows": [{name, state, detail}],
    "more": how many rows weren't listed}. Offline first, then none, then
    linked - the ones that need something done at the top."""
    order = {"offline": 0, "none": 1, "linked": 2}
    rows = []
    for entry in videos:
        state = proxy_state(entry)
        detail = {"linked": f"{entry.get('proxy') or ''}  {entry.get('proxy_path') or ''}".strip(),
                  "offline": entry.get("proxy_path") or "", "none": ""}[state]
        rows.append({"name": entry["name"], "state": state, "detail": detail})
    rows.sort(key=lambda r: (order[r["state"]], r["name"].lower()))
    counts = {state: sum(1 for r in rows if r["state"] == state) for state in order}
    return {"counts": counts, "rows": rows[:STATUS_ROWS_MAX], "more": max(0, len(rows) - STATUS_ROWS_MAX)}


def status_line(counts):
    """The Activity log's one-line summary of a status check."""
    return (f"Proxies: {counts['linked']} linked, {counts['offline']} offline, "
            f"{counts['none']} without one.")


RELINK_WALK_MAX = 200_000   # files looked at in the chosen folder before giving up


def find_relinks(offline, folder, walk=os.walk):
    """Where each offline proxy has gone: [(entry, new path)] for the ones
    found in `folder` or below, by the proxy's own file name (or, failing
    that, the name Buddy would give it). Runs on the worker - a big drive
    takes a while to walk."""
    wanted = {}
    for entry in offline:
        names = {os.path.basename(entry.get("proxy_path") or "")}
        stem = os.path.splitext(os.path.basename(entry.get("path") or ""))[0]
        if stem:
            names.add(f"{stem}{PROXY_SUFFIX}{PROXY_EXT}")
        for name in names - {""}:
            wanted.setdefault(name.lower(), []).append(entry)
    found, seen = {}, 0
    for root, dirs, files in walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            seen += 1
            for entry in wanted.get(name.lower(), ()):
                found.setdefault(entry["id"], (entry, os.path.join(root, name)))
        if seen >= RELINK_WALK_MAX or len(found) == len(offline):
            break
    return list(found.values())


def relink_report(relinked, refused, missing):
    lines = []
    if relinked == 1:
        lines.append(("Relinked 1 offline proxy.", "success"))
    elif relinked:
        lines.append((f"Relinked {relinked} offline proxies.", "success"))
    for name in refused:
        lines.append((f"Resolve wouldn't link the proxy found for \"{name}\".", "error"))
    if missing == 1:
        lines.append(("1 offline proxy wasn't in that folder.", "warn"))
    elif missing:
        lines.append((f"{missing} offline proxies weren't in that folder.", "warn"))
    return lines


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