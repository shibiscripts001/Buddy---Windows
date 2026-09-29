#!/usr/bin/env python3
"""
Waveforms for the Timeline tab: each media file's loudest sample in every
1/PEAK_RATE s, decoded once and kept under ~/.buddy/audio_assistant/peaks/.

Resolve can't hand over audio, so the file itself is decoded - with Qt
Multimedia's QAudioDecoder (FFmpeg-backed, built into Buddy: WAV, MP3, AAC,
the audio of an MP4/MOV/MXF), one file at a time on a QThread, with numpy
doing the sums. Each channel is kept apart, and a peak is stored as a byte: 0
is FLOOR_DB or quieter, 255 is 0 dBFS. dB, not amplitude, so the page applies
a clip's volume by adding - and a quiet passage keeps its shape after a +20 dB
boost.

The same pass measures loudness, for the page's LUFS (ITU-R BS.1770): each
LOUD_BLOCK_S block's K-weighted mean square, per channel, as a float32.

A clip plays the channels Resolve's mapping gives it (a lav on the left of a
stereo file, one mic of a camera's eight) - so what's drawn and measured for it
is fold()'s: those channels alone, the loudest peak and the summed energy
(BS.1770's channel sum, weight 1 each). One the decoder didn't give (a camera
MXF's channel 2: QAudioDecoder reads the first stream only) is said, not
guessed at. The page gates and integrates those over any clip's range, through
its volume and fades, so the figure follows a slider live. K-weighting is
applied in the frequency domain - numpy has no IIR filter, and a per-sample
Python loop over an hour of audio is out of the question - by weighting each
block's FFT power with the BS.1770 filter pair's response (Parseval: the
block's weighted energy is the same either way, bar edge effects at 100 ms).

1 hour of audio = 360,000 bytes of peaks and 144,000 of loudness, per channel.
A cached file is found again by its path, size and modified time; a changed
file is decoded afresh.
"""

import hashlib
import os
import threading

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from core.settings_store import BUDDY_DIR

PEAK_RATE = 100          # peaks per second
FLOOR_DB = -60.0
CACHE_DIR = os.path.join(BUDDY_DIR, "audio_assistant", "peaks")
CACHE_VERSION = 3        # 2: loudness beside the peaks; 3: every channel kept apart
LOUD_BLOCK_S = 0.1       # BS.1770's 400 ms gating blocks are 4 of these, stepping by one
# A decode that delivers nothing for this long is given up on.
STALL_MS = 20000
CANCEL_POLL_MS = 100     # how soon a decode notices it's been stopped, buffers or not
PROGRESS_EVERY_S = 0.5

# QAudioFormat sample formats: numpy dtype, and what full scale is.
_DTYPES = {"UInt8": (np.uint8, 128.0), "Int16": (np.int16, 32768.0),
           "Int32": (np.int32, 2147483648.0), "Float": (np.float32, 1.0)}

# BS.1770-4's K-weighting at 48 kHz: a high shelf, then a high-pass (b, a).
_K_STAGES = (
    ((1.53512485958697, -2.69169618940638, 1.19839281085285), (1.0, -1.69065929318241, 0.73248077421585)),
    ((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621)),
)


def k_weight_power(freqs):
    """|H(f)|^2 of the K-weighting filters, at frequencies in Hz. Defined at
    48 kHz, so anything above 24 kHz takes the (flat) value just below it."""
    omega = 2 * np.pi * np.minimum(np.asarray(freqs, dtype=np.float64), 23999.0) / 48000.0
    z1 = np.exp(-1j * omega)
    power = np.ones_like(omega)
    for b, a in _K_STAGES:
        h = (b[0] + b[1] * z1 + b[2] * z1 * z1) / (a[0] + a[1] * z1 + a[2] * z1 * z1)
        power *= np.abs(h) ** 2
    return power


class LoudnessAccumulator:
    """K-weighted mean square per LOUD_BLOCK_S block, per channel."""

    def __init__(self):
        self._carry = None
        self._blocks = []            # [(blocks, channels) float32]
        self._weights = None
        self.rate = None
        self.channels = 0

    def add(self, samples, rate):
        """samples: (frames, channels), full scale 1.0, in order."""
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim == 1:
            samples = samples[:, None]
        if not len(samples) or not rate:
            return
        if self.rate is None:
            self.rate = rate
            self.channels = samples.shape[1]
            n = max(1, int(round(rate * LOUD_BLOCK_S)))
            # One-sided spectrum: every bin but DC (and Nyquist, for an even n) counts twice.
            scale = np.full(n // 2 + 1, 2.0)
            scale[0] = 1.0
            if n % 2 == 0:
                scale[-1] = 1.0
            self._weights = k_weight_power(np.fft.rfftfreq(n, 1.0 / rate)) * scale / (n * n)
        samples = _fit(samples, self.channels)
        if self._carry is not None:
            samples = np.concatenate((self._carry, samples))
        n = max(1, int(round(self.rate * LOUD_BLOCK_S)))
        whole = len(samples) // n
        if whole:
            blocks = samples[:whole * n].reshape(whole, n, self.channels)
            power = np.abs(np.fft.rfft(blocks, axis=1)) ** 2
            self._blocks.append((power * self._weights[None, :, None]).sum(axis=1).astype(np.float32))
        self._carry = samples[whole * n:]

    def per_channel(self):
        """(blocks, channels) float32."""
        if not self._blocks:
            return np.zeros((0, max(1, self.channels)), dtype=np.float32)
        return np.concatenate(self._blocks)

    def energies(self):
        """Every channel's summed - what a clip playing them all is measured by."""
        return self.per_channel().sum(axis=1).astype(np.float32).tobytes() if self._blocks else b""


def _fit(samples, channels):
    """A buffer made the first one's channel count: a decoder that changes it
    mid-file has its extra channels dropped, missing ones silent."""
    have = samples.shape[1]
    if have == channels:
        return samples
    if have > channels:
        return samples[:, :channels]
    return np.concatenate((samples, np.zeros((len(samples), channels - have), dtype=samples.dtype)), axis=1)


def integrated_lufs(energies):
    """BS.1770 gated loudness of a whole file from LoudnessAccumulator's
    blocks, or None for silence. For a clip's range, through its volume and
    fades, see levels.clip_blocks() (and timeline.js's lufs())."""
    from .levels import gate
    z = np.asarray(energies, dtype=np.float64)
    if len(z) < 4:
        return None
    return gate(np.convolve(z, np.ones(4) / 4, mode="valid"))       # 400 ms blocks, stepping 100 ms


def to_codes(amplitude):
    """Peak amplitudes (0-1+) as bytes on the FLOOR_DB..0 dB scale."""
    amplitude = np.asarray(amplitude, dtype=np.float64)
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(amplitude, 1e-12))
    return np.clip(np.round((db - FLOOR_DB) / -FLOOR_DB * 255.0), 0, 255).astype(np.uint8)


class PeakAccumulator:
    """Folds decoded buffers, in order, into one peak per 1/PEAK_RATE s, per channel."""

    def __init__(self):
        self._peaks = None       # (capacity, channels) float32
        self._count = 0          # peaks written so far
        self._samples = 0        # sample frames seen so far
        self.rate = None
        self.channels = 0

    def add(self, samples, rate):
        """samples: shape (frames, channels) or (frames,), full scale 1.0."""
        samples = np.abs(np.asarray(samples, dtype=np.float32))
        if samples.ndim == 1:
            samples = samples[:, None]
        n = len(samples)
        if not n or not rate:
            return
        if self.rate is None:
            self.rate, self.channels = rate, samples.shape[1]
            self._peaks = np.zeros((PEAK_RATE * 60, self.channels), dtype=np.float32)
        samples = _fit(samples, self.channels)
        ids = (np.arange(self._samples, self._samples + n, dtype=np.int64) * PEAK_RATE) // int(self.rate)
        self._samples += n
        starts = np.concatenate(([0], np.flatnonzero(np.diff(ids)) + 1))
        values, buckets = np.maximum.reduceat(samples, starts, axis=0), ids[starts]
        need = int(buckets[-1]) + 1
        if need > len(self._peaks):
            grown = np.zeros((max(need, len(self._peaks) * 2), self.channels), dtype=np.float32)
            grown[:len(self._peaks)] = self._peaks
            self._peaks = grown
        np.maximum.at(self._peaks, buckets, values)
        self._count = max(self._count, need)

    def per_channel(self):
        """(peaks, channels) codes, uint8."""
        if self._peaks is None:
            return np.zeros((0, 1), dtype=np.uint8)
        return to_codes(self._peaks[:self._count])

    def codes(self):
        """Every channel folded into one (the loudest wins)."""
        return self.per_channel().max(axis=1).astype(np.uint8).tobytes() if self._count else b""


def fold(result, channels=None):
    """decode()'s per-channel result -> ((peak bytes, loudness bytes), None)
    for a clip playing `channels` (0-based, the file's; None: all of them):
    their loudest peak and their summed energy - or (None, why) when the
    clip plays a channel the decoder didn't give."""
    codes, loud, count = result
    count = max(1, int(count))
    if channels and max(channels) >= count:
        have = "channel" if count == 1 else f"{count} channels"
        return None, f"Buddy can read only the first {have} of this file, and the clip plays channel {max(channels) + 1}."
    peak = np.frombuffer(codes, dtype=np.uint8).reshape(-1, count)
    energy = np.frombuffer(loud, dtype=np.float32).reshape(-1, count)
    if channels:
        peak, energy = peak[:, channels], energy[:, channels]
    return (peak.max(axis=1).astype(np.uint8).tobytes() if len(peak) else b"",
            energy.sum(axis=1).astype(np.float32).tobytes() if len(energy) else b""), None


def buffer_samples(buf):
    """A QAudioBuffer's samples as float32 (frames, channels), or None for
    a sample format numpy isn't told about."""
    fmt = buf.format()
    kind = _DTYPES.get(fmt.sampleFormat().name)
    channels = max(1, fmt.channelCount())
    if kind is None:
        return None
    dtype, scale = kind
    raw = bytes(buf.constData())
    usable = len(raw) - len(raw) % (np.dtype(dtype).itemsize * channels)
    values = np.frombuffer(raw[:usable], dtype=dtype).astype(np.float32)
    if dtype is np.uint8:
        values -= 128.0
    return (values / scale).reshape(-1, channels), fmt.sampleRate()


def cache_path(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    ident = f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{st.st_mtime_ns}|{PEAK_RATE}|{CACHE_VERSION}"
    return os.path.join(CACHE_DIR, hashlib.sha1(ident.encode("utf-8")).hexdigest() + ".peaks")


def load_cached(path):
    """decode()'s (peak bytes, loudness bytes, channels) from the cache, or None.
    The .peaks file starts with a byte saying how many channels it holds."""
    target = cache_path(path)
    if not target:
        return None
    try:
        with open(target, "rb") as f:
            codes = f.read()
        with open(target[:-len(".peaks")] + ".loud", "rb") as f:
            loud = f.read()
    except OSError:
        return None
    if not codes or not codes[0]:
        return None
    return codes[1:], loud, codes[0]


def save_cached(path, result):
    target = cache_path(path)
    if not target:
        return
    codes, loud, channels = result
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        # Loudness first: a .peaks file is only ever there with its .loud.
        for data, where in ((loud, target[:-len(".peaks")] + ".loud"), (bytes([channels]) + codes, target)):
            temp = f"{where}.{os.getpid()}.tmp"
            with open(temp, "wb") as f:
                f.write(data)
            os.replace(temp, where)
    except OSError:
        pass            # no cache this time: it's decoded again next session


def _stream(path, on_samples, progress=None, cancelled=lambda: False, until_ms=None):
    """Decodes the file from the top, handing each buffer to on_samples(samples,
    rate) - samples as buffer_samples() gives them. on_samples returning True
    stops it there, as done. None on success, else why not. Runs its own event
    loop, so it belongs on a worker thread. until_ms: where progress counts to
    (a range's end), instead of the whole file."""
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder
    import time

    if not os.path.isfile(path):
        return "The file is offline."
    state = {"error": None, "done": False, "duration": -1, "said": 0.0}
    loop = QEventLoop()
    decoder = QAudioDecoder()
    stall = QTimer()
    stall.setSingleShot(True)
    stall.timeout.connect(loop.quit)
    # Stopping is looked at on a timer too, not only when a buffer comes: a
    # stalled decoder sends none for up to STALL_MS, and quitting Buddy then
    # waited out the stall while Qt tore down around a running thread.
    watch = QTimer()
    watch.setInterval(CANCEL_POLL_MS)
    watch.timeout.connect(lambda: loop.quit() if cancelled() else None)

    def on_buffer():
        if cancelled():
            return loop.quit()
        got = buffer_samples(decoder.read())
        if got is not None and on_samples(*got):
            state["done"] = True
            return loop.quit()
        stall.start(STALL_MS)
        now = time.monotonic()
        total = until_ms or state["duration"]
        if progress and total > 0 and now - state["said"] >= PROGRESS_EVERY_S:
            state["said"] = now
            progress(min(1.0, decoder.position() / total))

    def on_error(*_args):
        state["error"] = decoder.errorString() or "It couldn't be decoded."
        loop.quit()

    def on_finished():
        state["done"] = True
        loop.quit()

    decoder.bufferReady.connect(on_buffer)
    decoder.finished.connect(on_finished)
    decoder.error.connect(on_error)
    decoder.durationChanged.connect(lambda ms: state.__setitem__("duration", ms))
    decoder.setSource(QUrl.fromLocalFile(path))
    stall.start(STALL_MS)
    watch.start()
    decoder.start()
    loop.exec()
    stall.stop()
    watch.stop()
    decoder.stop()
    if cancelled():
        return "Stopped."
    if state["error"]:
        return state["error"]
    if not state["done"]:
        return "Decoding stalled."
    return None


def decode(path, progress=None, cancelled=lambda: False):
    """((peak bytes, loudness bytes, channels), None) or (None, why) - every
    channel kept apart: the peaks (frames, channels) uint8 and the loudness
    (blocks, channels) float32, row by row. fold() makes a clip's of them.
    Runs its own event loop, so it belongs on a worker thread."""
    acc, loud = PeakAccumulator(), LoudnessAccumulator()

    def take(samples, rate):
        acc.add(samples, rate)
        loud.add(samples, rate)

    error = _stream(path, take, progress, cancelled)
    if error:
        return None, error
    if acc.rate is None:
        return None, "The file has no audio Buddy can read."
    if acc.channels > 255:
        return None, "The file has more channels than Buddy can draw."
    return (acc.per_channel().tobytes(), loud.per_channel().tobytes(), acc.channels), None


def decode_range(path, start_s, end_s, progress=None, cancelled=lambda: False, channels=None):
    """((samples, rate), None) or (None, why): the file's audio from start_s
    to end_s (seconds into the file) at full quality, float32 (frames,
    channels) - for render.py to process. Decoded from the top, as
    QAudioDecoder can't seek, but it stops at end_s. A range running past
    the file's end gets what there is. Runs its own event loop, so it
    belongs on a worker thread.

    channels: which of the decoded channels to keep, 0-based
    (render.channels_from_mapping - the ones the clip plays), or None for
    all. QAudioDecoder decodes only the file's first audio stream, and a
    camera MXF has a stream per channel: Resolve's channel 2 of an FX6 file
    isn't there at all (measured on Resolve 21.1: A1-A3 of one MXF play its
    channels 1-3; Qt gave channel 1 alone). Asking for a channel that isn't
    decoded fails on the first buffer, rather than handing over the wrong
    audio."""
    parts, state = [], {"rate": None, "seen": 0, "first": 0, "last": 0, "error": None}

    def take(samples, rate):
        if state["rate"] is None:
            state["rate"] = rate
            state["first"] = max(0, int(round(start_s * rate)))
            state["last"] = max(state["first"], int(round(end_s * rate)))
            if channels and max(channels) >= samples.shape[1]:
                have = samples.shape[1]
                state["error"] = (f"Buddy can read only the first {'channel' if have == 1 else f'{have} channels'} "
                                  f"of this file, and the clip plays channel {max(channels) + 1}.")
                return True
        if channels:
            samples = samples[:, channels]
        seen, n = state["seen"], len(samples)
        state["seen"] = seen + n
        a, b = max(state["first"], seen), min(state["last"], seen + n)
        if a < b:
            parts.append(np.array(samples[a - seen:b - seen], dtype=np.float32))
        return state["seen"] >= state["last"]

    error = _stream(path, take, progress, cancelled, until_ms=end_s * 1000.0) or state["error"]
    if error:
        return None, error
    if state["rate"] is None:
        return None, "The file has no audio Buddy can read."
    if not parts:
        return None, "That part of the file has no audio."
    return (np.concatenate(parts), state["rate"]), None


_STILL_RUNNING = []      # decode threads that didn't stop at shutdown: kept, never destroyed running


class _DecodeThread(QThread):
    progressed = Signal(str, float)
    finished_peaks = Signal(str, object, object)     # key, (peaks, loudness) or None, error or None

    def __init__(self, key, path, stop, parent=None):
        super().__init__(parent)
        self.key, self.path, self._stop = key, path, stop

    def run(self):
        try:
            codes, error = decode(self.path, lambda f: self.progressed.emit(self.key, f), self._stop.is_set)
        except Exception as exc:  # noqa: BLE001 - shown as "no waveform"
            codes, error = None, str(exc)
        if codes is not None:
            save_cached(self.path, codes)
        self.finished_peaks.emit(self.key, codes, error)


class PeakLoader(QObject):
    """Queues files and decodes them one at a time. ready(key, (peaks,
    loudness) or None, error) fires once per file asked for; progress(key,
    0-1) while one decodes."""

    ready = Signal(str, object, object)
    progress = Signal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []             # [(key, path)]
        self._asked = set()
        self._thread = None
        self._stop = threading.Event()

    def pending(self):
        return len(self._queue) + (1 if self._thread is not None else 0)

    def forget(self):
        """Refresh: every file can be asked for again - found in the cache if it's
        unchanged, decoded afresh if not. What's queued or decoding now stays."""
        self._asked = {key for key, _path in self._queue}
        if self._thread is not None:
            self._asked.add(self._thread.key)

    def want(self, key, path):
        if key in self._asked:
            return
        self._asked.add(key)
        cached = load_cached(path)
        if cached is not None:
            self.ready.emit(key, cached, None)
            return
        self._queue.append((key, path))
        self._next()

    def _next(self):
        if self._thread is not None or not self._queue or self._stop.is_set():
            return
        key, path = self._queue.pop(0)
        self._thread = _DecodeThread(key, path, self._stop, self)
        self._thread.progressed.connect(self.progress)
        self._thread.finished_peaks.connect(self._done)
        self._thread.start()

    def _done(self, key, codes, error):
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.wait(2000)
            thread.deleteLater()
        self.ready.emit(key, codes, error)
        self._next()

    def shutdown(self):
        """For quitting: a QThread destroyed while running crashes the exit. A
        decode sees the stop within CANCEL_POLL_MS (_stream), so the wait is
        short; one that still hasn't ended is kept alive here - unparented, so
        this loader's end doesn't take it along - rather than destroyed running."""
        self._stop.set()
        self._queue = []
        thread = self._thread
        if thread is None:
            return
        try:
            thread.finished_peaks.disconnect()
        except (RuntimeError, TypeError):
            pass
        if not thread.wait(5000) and thread.isRunning():
            thread.setParent(None)
            _STILL_RUNNING.append(thread)
