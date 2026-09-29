#!/usr/bin/env python3
"""
Processed audio: a clip's stretch of its file, through Buddy's effects
(dsp.py), written as a new WAV that Resolve can play in the clip's place. The
original file is never touched. No Qt, no Resolve - decoding is handed in
(peaks.decode_range in Buddy), so it's tested directly.

Where it goes: a "Buddy Audio" folder beside the source file (the user's
choice: the processed audio lives with the project's media). Where that
can't be written - a camera card, a read-only share - it goes under
~/.buddy/audio_assistant/renders/ instead, and the result says so.

What's written:
  <stem> - Buddy <tag>.wav        the clip's range plus HANDLE_S either side
                                  (for trims), at the file's own sample rate,
                                  24-bit PCM (stdlib wave has no float)
  <stem> - Buddy <tag>.wav.json   the sidecar: the source (path, size, mtime),
                                  the range, where the WAV starts in the
                                  source, the chain - enough to re-edit it
<tag> is a hash of the source file, the range and the chain, so the same
settings on the same clip give the same file: Apply again reuses it (no
decode) instead of piling up copies.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import wave
from datetime import datetime
from fractions import Fraction

import numpy as np

from core.atomic_io import read_json, write_json
from core.settings_store import BUDDY_DIR

from . import dsp

FOLDER_NAME = "Buddy Audio"
FALLBACK_DIR = os.path.join(BUDDY_DIR, "audio_assistant", "renders")
HANDLE_S = 1.0          # extra audio either side of the clip, so it can still be trimmed longer
SIDECAR_VERSION = 1
_FULL = 2 ** 23          # 24-bit full scale
_WAV_LIMIT = 2 ** 32 - 64   # RIFF sizes are 32-bit


def _writable(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".buddy-", dir=folder)
        os.close(fd)
        os.remove(probe)
        return True
    except OSError:
        return False


def output_folder(source_path, fallback=FALLBACK_DIR, writable=_writable):
    """(folder, beside): the Buddy Audio folder beside the source, or the
    fallback (beside False) where that can't be written."""
    beside = os.path.join(os.path.dirname(os.path.abspath(source_path)), FOLDER_NAME)
    if writable(beside):
        return beside, True
    os.makedirs(fallback, exist_ok=True)
    return fallback, False


def channels_from_mapping(mapping):
    """The channels a clip plays, 0-based, from Resolve's
    GetSourceAudioChannelMapping() JSON - {"track_mapping": {"1":
    {"channel_idx": [2], ...}}} is the file's channel 2 alone - or None
    (all of them) when there's no mapping to go by."""
    try:
        data = json.loads(mapping) if isinstance(mapping, str) else mapping
        tracks = data["track_mapping"]
        picked = [int(i) - 1 for key in sorted(tracks, key=lambda k: int(k))
                  for i in tracks[key].get("channel_idx") or []]
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    return picked if picked and min(picked) >= 0 else None


def frame_handle(source_in, frame, want_s=HANDLE_S):
    """How much extra to take before the clip, as a whole number of `frame`s
    (Fractions, seconds) - never reaching before the file's start. A WAV
    imported into Resolve takes the project's frame rate, and the nested
    clip's start in it must land on one of those frames."""
    source_in, frame = Fraction(source_in), Fraction(frame)
    return min(source_in // frame, round(Fraction(want_s) / frame)) * frame


def _source(path):
    st = os.stat(path)
    return {"path": os.path.abspath(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def file_name(source, start_s, end_s, chain, channels=None, handle_s=HANDLE_S):
    """The WAV's name for this source (_source()), range, clean chain,
    channels and handle."""
    ident = json.dumps([os.path.normcase(source["path"]), source["size"], source["mtime_ns"],
                        round(start_s, 4), round(end_s, 4), chain, channels, round(float(handle_s), 4)],
                       sort_keys=True)
    tag = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:8]
    stem = os.path.splitext(os.path.basename(source["path"]))[0][:60] or "Clip"
    return f"{stem} - Buddy {tag}.wav"


def sidecar_path(wav_path):
    return wav_path + ".json"


def read_sidecar(wav_path):
    """The sidecar's settings, or None when it's missing, damaged or not Buddy's."""
    try:
        data = read_json(sidecar_path(wav_path), default=None)
    except Exception:  # noqa: BLE001 - a damaged sidecar is as good as none
        return None
    if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
        return None
    return data


def write_wav(path, samples, rate):
    """float samples (frames, channels) or (frames,) -> a 24-bit PCM WAV,
    written beside the target and swapped in, so a half-written file is never
    left under the name. Returns how many samples were over full scale (and
    clipped)."""
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    frames, channels = x.shape
    if frames * channels * 3 > _WAV_LIMIT:
        raise ValueError("That's too long for one WAV file - try a shorter clip.")
    over = int(np.count_nonzero(np.abs(x) > 1.0))
    ints = np.clip(np.round(x * _FULL), -_FULL, _FULL - 1).astype("<i4")
    data = ints.reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    folder = os.path.dirname(os.path.abspath(path))
    fd, temp = tempfile.mkstemp(prefix=".buddy-", suffix=".wav", dir=folder)
    os.close(fd)
    try:
        with wave.open(temp, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(3)
            w.setframerate(int(rate))
            w.writeframes(data)
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return over


def _claim(wav_path, sidecar, project):
    projects = list(sidecar.get("projects") or [])
    if project and project not in projects:
        sidecar["projects"] = projects + [project]
        try:
            write_json(sidecar_path(wav_path), sidecar)
        except OSError:
            pass


def release(wav_path, project):
    """This project no longer uses the WAV: it comes off the sidecar's list.
    True when no project is left on it, so the file (and sidecar) can go to
    the Recycle Bin - False when another project still has it, or there's no
    sidecar to say (a file Buddy can't vouch for stays)."""
    sidecar = read_sidecar(wav_path)
    if sidecar is None:
        return False
    left = [p for p in sidecar.get("projects") or [] if p != project]
    if left:
        sidecar["projects"] = left
        try:
            write_json(sidecar_path(wav_path), sidecar)
        except OSError:
            pass
        return False
    return True


def with_its_sidecar(wav_path):
    """The files a processed WAV is on disk: itself, its sidecar, the sidecar's .bak."""
    side = sidecar_path(wav_path)
    return [wav_path, side, side + ".bak"]


def buddy_wavs_in(folder):
    """{path: sidecar} for Buddy's processed WAVs in a folder (a sidecar says so)."""
    found = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        if name.lower().endswith(".wav") and " - Buddy " in name:
            path = os.path.join(folder, name)
            sidecar = read_sidecar(path)
            if sidecar is not None:
                found[path] = sidecar
    return found


def render(source_path, start_s, end_s, chain, decode, handle_s=HANDLE_S, channels=None, project=None,
           fallback=FALLBACK_DIR, writable=_writable, progress=None, cancelled=lambda: False):
    """The clip's range of source_path (seconds into the file) through the
    chain, as a WAV. decode(path, start_s, end_s, progress, cancelled,
    channels=...) -> ((samples, rate), None) or (None, why):
    peaks.decode_range. channels: the ones the clip plays
    (channels_from_mapping), None for all. handle_s: the extra either side -
    frame_handle()'s, so the clip's start in the WAV is on a frame. project:
    the Resolve project's GetUniqueId(), kept in the sidecar's "projects" -
    the WAV sits beside the media, where another project can use it too, so
    Tidy up only lets it go once no project is left on it (release()).

    (result, None) or (None, why). result: {"path", "file_start" (where the
    WAV's first sample sits in the source, seconds), "rate", "frames",
    "channels", "beside" (False: in the fallback folder), "clipped", "reused"
    (the same settings' file was already there: nothing was decoded)}."""
    chain = dsp.clean_chain(chain)
    if not dsp.active(chain):
        return None, "No effect is on."
    if not end_s > start_s >= 0:
        return None, "That clip has no length."
    try:
        source = _source(source_path)
    except OSError:
        return None, "The clip's file is offline."
    channels = [int(c) for c in channels] if channels else None
    handle_s = float(handle_s)
    folder, beside = output_folder(source_path, fallback, writable)
    path = os.path.join(folder, file_name(source, start_s, end_s, chain, channels, handle_s))

    known = read_sidecar(path) if os.path.isfile(path) else None
    if known and known.get("source") == source and known.get("chain") == chain \
            and known.get("channels_used") == channels:
        _claim(path, known, project)
        return {"path": path, "file_start": known["file_start"], "rate": known["rate"],
                "frames": known["frames"], "channels": known["channels"], "beside": beside,
                "clipped": known.get("clipped", 0), "reused": True}, None

    first = max(0.0, start_s - handle_s)
    decoded, error = decode(source_path, first, end_s + handle_s, progress, cancelled, channels=channels)
    if error:
        return None, error
    samples, rate = decoded
    processed = dsp.process(samples, rate, chain, cancelled=cancelled)
    if processed is None:
        return None, "Stopped."
    if processed.ndim == 1:
        processed = processed[:, None]
    try:
        clipped = write_wav(path, processed, rate)
    except (OSError, ValueError) as exc:
        return None, f"Couldn't write the processed audio: {exc}"
    file_start = round(first * rate) / rate        # decode_range starts on this sample
    info = {"version": SIDECAR_VERSION, "source": source, "range": [start_s, end_s],
            "file_start": file_start, "rate": int(rate), "frames": int(len(processed)),
            "channels": int(processed.shape[1]), "channels_used": channels, "chain": chain, "clipped": clipped,
            "projects": [project] if project else [],
            "made": datetime.now().isoformat(timespec="seconds")}
    try:
        write_json(sidecar_path(path), info)
    except OSError:
        pass        # the WAV still plays; it just can't be recognised to reuse
    return {"path": path, "file_start": file_start, "rate": info["rate"], "frames": info["frames"],
            "channels": info["channels"], "beside": beside, "clipped": clipped, "reused": False}, None
