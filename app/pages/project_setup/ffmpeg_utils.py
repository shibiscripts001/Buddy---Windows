#!/usr/bin/env python3
"""
ffmpeg discovery for the Align tab's waveform sync method.

Timecode and File Data sync only ever read metadata Resolve already
parsed off the clip - no external tool needed. Waveform sync is the one
method that needs actual decoded audio samples, which Resolve's scripting
API has no call for at all, so this shells out to ffmpeg (not bundled -
the user's own install) the same way any other audio tool would.
"""

import threading
import json
import os
import re
import shutil
import subprocess
import sys

_STARTF_FORCEOFFFEEDBACK = 0x00000080


class NoAudioStreamError(RuntimeError):
    """file_path has no audio stream at all - e.g. a camera (like Sony's
    FX6) writing video-only MXFs for takes with no mic connected. Distinct
    from decode_mono_pcm's ordinary RuntimeError (an actual ffmpeg
    failure) so callers can tell "there was never anything to decode"
    apart from a real error worth surfacing - audio_sync treats this the
    same as digital silence rather than a failure to check."""


def _no_feedback_cursor_kwargs():
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= _STARTF_FORCEOFFFEEDBACK
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


def _candidate_paths():
    if sys.platform == "win32":
        yield os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe")
        # `winget install ffmpeg` (Gyan.FFmpeg) normally adds a shim here,
        # but on some installs that shim is never created even
        # though the package itself installed fine - so this also globs the
        # actual WinGet package directory directly as a fallback, not just
        # the shim location.
        yield os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")
        packages_root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        try:
            for entry in os.listdir(packages_root):
                if entry.lower().startswith("gyan.ffmpeg"):
                    pkg_dir = os.path.join(packages_root, entry)
                    for sub in os.listdir(pkg_dir):
                        candidate = os.path.join(pkg_dir, sub, "bin", "ffmpeg.exe")
                        if os.path.isfile(candidate):
                            yield candidate
        except OSError:
            pass
    else:
        yield from ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")


def find_ffmpeg(override_path=None):
    """Returns a usable ffmpeg path, or None. Checks (in order): an explicit
    override (Settings), PATH, then a short list of common install
    locations that don't always end up on PATH."""
    if override_path and os.path.isfile(override_path):
        return override_path

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    for candidate in _candidate_paths():
        if os.path.isfile(candidate):
            return candidate
    return None


def _ffprobe_path(ffmpeg_path):
    """ffprobe ships alongside ffmpeg in every mainstream build, so it's
    derived from the ffmpeg path rather than searched for separately.
    Returns None if it isn't actually there."""
    directory, name = os.path.split(ffmpeg_path)
    candidate = os.path.join(directory, name.replace("ffmpeg", "ffprobe", 1))
    return candidate if os.path.isfile(candidate) else None


def _count_audio_streams_ffmpeg(ffmpeg_path, file_path, timeout):
    """Fallback stream count for builds shipped without ffprobe: `ffmpeg -i`
    with no output file prints the input's stream table to stderr and exits
    non-zero ("At least one output file must be specified"), which is
    expected here - the exit code is ignored and only the stream lines are
    read."""
    result = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", file_path],
        capture_output=True, timeout=timeout, **_no_feedback_cursor_kwargs(),
    )
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    return len(re.findall(r"^\s*Stream #\d+:\d+.*: Audio:", stderr, re.MULTILINE))


def count_audio_streams(ffmpeg_path, file_path, timeout=60):
    """How many separate audio streams file_path carries. Returns 1 if it
    can't be determined - that just means decode_mono_pcm falls back to
    ffmpeg's own default stream pick, i.e. the old behaviour."""
    ffprobe = _ffprobe_path(ffmpeg_path)
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "json", file_path],
                capture_output=True, timeout=timeout, **_no_feedback_cursor_kwargs(),
            )
            if result.returncode == 0:
                streams = json.loads(result.stdout or b"{}").get("streams") or []
                return len(streams)
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    try:
        return _count_audio_streams_ffmpeg(ffmpeg_path, file_path, timeout)
    except (subprocess.SubprocessError, OSError):
        return 1


def audio_channel_layout(ffmpeg_path, file_path, timeout=60):
    """Channel counts per audio stream, e.g. [2, 2, 1] for a file with two
    stereo streams and a mono one. Empty list if the file has no audio (or
    can't be probed).

    Needed because "one of a clip's audio tracks" has to be resolved down
    to actual channels before it can be looked at on its own. Resolve shows
    a clip as N audio tracks, but the file underneath might hold those as N
    separate streams, or as one stream of N channels, or as one stream of
    2N channels paired up - only the real layout says which."""
    ffprobe = _ffprobe_path(ffmpeg_path)
    if not ffprobe:
        return [1] * count_audio_streams(ffmpeg_path, file_path, timeout=timeout)
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=channels", "-of", "json", file_path],
            capture_output=True, timeout=timeout, **_no_feedback_cursor_kwargs(),
        )
        if result.returncode != 0:
            return []
        streams = json.loads(result.stdout or b"{}").get("streams") or []
        return [int(s.get("channels") or 1) for s in streams]
    except (subprocess.SubprocessError, ValueError, OSError):
        return []


def channels_for_track(layout, track_count, track_index):
    """Which global channel numbers (counting straight through every audio
    stream in order) make up one of a clip's audio tracks. Returns None
    when the file's layout can't be divided into track_count tracks at all,
    which the caller must treat as "can't tell" rather than guessing.

    Three layouts cover essentially all real footage: one stream per track
    (a camera writing each mic to its own stream), one channel per track
    (a multi-channel field recording), and an even number of channels per
    track (stereo pairs). Anything else is left alone."""
    if not layout or track_count <= 0 or not (0 <= track_index < track_count):
        return None
    total = sum(layout)

    if len(layout) == track_count:
        first = sum(layout[:track_index])
        return list(range(first, first + layout[track_index]))
    if total % track_count == 0:
        per_track = total // track_count
        first = track_index * per_track
        return list(range(first, first + per_track))
    return None


_ACTIVE_DECODES = set()
_ACTIVE_LOCK = threading.Lock()


def terminate_active():
    """Kills every ffmpeg decode currently running. Returns how many.

    Called when a run is cancelled. Without it a cancelled pass leaves up to
    one decode per worker still reading media - the work is abandoned, but
    the reading is not, and on a busy drive that is the part that hurts.

    Safe to call from any thread: killing a process the owning thread is
    blocked on simply makes its wait return, and every caller already treats
    a failed decode as a clip it could not read."""
    with _ACTIVE_LOCK:
        running = list(_ACTIVE_DECODES)
    for process in running:
        try:
            process.kill()
        except Exception:
            pass
    return len(running)


def decode_mono_pcm(ffmpeg_path, file_path, sample_rate, max_seconds=None,
                    start_seconds=None, channels=None, timeout=120):
    """Decodes file_path's audio to mono 32-bit float PCM at sample_rate,
    returning raw bytes read from ffmpeg's stdout. max_seconds (if given)
    truncates the *decode*, not just the read, via ffmpeg's own -t flag -
    the sync point is normally near the start of a clip anyway (a clap, a
    slate, or just where two cameras' rolls overlap), so there's no need to
    decode a whole multi-hour file to find it. start_seconds (if given) is
    passed as -ss *before* -i for fast input-side seeking, used by
    Collapse's silence check to look at a clip's actual trimmed placement
    on the timeline rather than always the file's own beginning.

    Every audio stream in the file is mixed down together, not just one.
    Plain `-i file -ac 1` leaves the choice to ffmpeg's own default stream
    selection, which picks a single stream on structural grounds alone and
    never looks at whether it contains anything: given two files holding
    identical content, one with a blank stereo backup track written first,
    ffmpeg was observed taking the blank stream and decoding pure silence.
    Camera and field-recorder files routinely carry exactly that - a stereo
    pair, a safety/backup track, or a track left blank - so which stream
    ffmpeg happened to like is not a sound basis for syncing. Mixing them
    all sidesteps the choice entirely: blank streams contribute nothing to
    the mix, and a backup track only reinforces the content already there,
    since it's the same audio.

    channels (a list of global channel numbers, from channels_for_track)
    narrows the decode to just those, mixed to mono, instead of the whole
    file - what Collapse's silence check needs, where the whole point is
    to judge ONE of a clip's audio tracks on its own. Mixing everything is
    right for sync and wrong here: a blank track in a file that also holds
    real audio comes back sounding perfectly loud, which is exactly why
    "delete silent audio clips" appeared to do nothing."""
    stream_count = count_audio_streams(ffmpeg_path, file_path, timeout=timeout)
    if stream_count == 0:
        raise NoAudioStreamError(f"{os.path.basename(file_path)} has no audio stream")

    cmd = [ffmpeg_path, "-v", "error"]
    # Formatted, not str()'d: a seek computed from frame arithmetic can come
    # out as 1.1368683772161603e-13, which ffmpeg rejects outright as an
    # invalid duration rather than treating as zero. Anything under a
    # millisecond is not a seek worth making.
    if start_seconds and float(start_seconds) >= 0.001:
        cmd += ["-ss", f"{float(start_seconds):.6f}"]
    cmd += ["-i", file_path]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]

    if channels:
        # amerge lays every stream's channels out end to end in stream
        # order, which is what makes one global channel numbering
        # meaningful across a multi-stream file; pan then keeps only the
        # wanted ones, averaged to mono. A single-stream file is addressed
        # directly - amerge=inputs=1 is an error, not a no-op. pan takes a
        # flat sum of gain*channel terms, with no parentheses, so the
        # averaging gain is written onto each term individually.
        if stream_count == 1:
            chain = "[0:a:0]"
        else:
            chain = "".join(f"[0:a:{i}]" for i in range(stream_count))
            chain += f"amerge=inputs={stream_count},"
        gain = 1.0 / len(channels)
        terms = "+".join(f"{gain:.6f}*c{c}" for c in channels)
        cmd += ["-filter_complex", f"{chain}pan=mono|c0={terms}[sel]", "-map", "[sel]"]
    elif stream_count > 1:
        inputs = "".join(f"[0:a:{i}]" for i in range(stream_count))
        # dropout_transition=0 keeps a stream that simply ends earlier than
        # the others from being faded around, which would distort the very
        # waveform being matched.
        cmd += ["-filter_complex",
                f"{inputs}amix=inputs={stream_count}:duration=longest:dropout_transition=0[mix]",
                "-map", "[mix]"]

    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]

    # Popen rather than subprocess.run so the process can be reached while it
    # runs - see terminate_active(). Everything else about the call is the
    # same; communicate() with a timeout reproduces run()'s behaviour.
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **_no_feedback_cursor_kwargs(),
    )
    with _ACTIVE_LOCK:
        _ACTIVE_DECODES.add(process)
    try:
        try:
            stdout, stderr_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_DECODES.discard(process)

    if process.returncode != 0 or not stdout:
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or f"ffmpeg produced no audio for {os.path.basename(file_path)}")
    return stdout
