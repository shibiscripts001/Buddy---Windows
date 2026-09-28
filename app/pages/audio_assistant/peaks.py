#!/usr/bin/env python3
"""
Waveforms for the Timeline tab: each media file's loudest sample in every
1/PEAK_RATE s, decoded once and kept under ~/.buddy/audio_assistant/peaks/.

Resolve can't hand over audio, so the file itself is decoded - with Qt
Multimedia's QAudioDecoder (FFmpeg-backed, built into Buddy: WAV, MP3, AAC,
the audio of an MP4/MOV/MXF), one file at a time on a QThread, with numpy
doing the sums. Every channel is folded into one (the loudest wins), and a
peak is stored as a byte: 0 is FLOOR_DB or quieter, 255 is 0 dBFS. dB, not
amplitude, so the page applies a clip's volume by adding - and a quiet
passage keeps its shape after a +20 dB boost.

The same pass measures loudness, for the page's LUFS (ITU-R BS.1770): each
LOUD_BLOCK_S block's K-weighted mean square, summed over the channels, as a
float32. The page gates and integrates those over any clip's range, through
its volume and fades, so the figure follows a slider live. K-weighting is
applied in the frequency domain - numpy has no IIR filter, and a per-sample
Python loop over an hour of audio is out of the question - by weighting each
block's FFT power with the BS.1770 filter pair's response (Parseval: the
block's weighted energy is the same either way, bar edge effects at 100 ms).

1 hour of audio = 360,000 bytes of peaks and 144,000 of loudness. A cached
file is found again by its path, size and modified time; a changed file is
decoded afresh.
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
CACHE_VERSION = 2        # 2: loudness beside the peaks
LOUD_BLOCK_S = 0.1       # BS.1770's 400 ms gating blocks are 4 of these, stepping by one
# A decode that delivers nothing for this long is given up on.
STALL_MS = 20000
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
    """K-weighted mean square per LOUD_BLOCK_S block, summed over channels."""

    def __init__(self):
        self._carry = None
        self._blocks = []
        self._weights = None
        self.rate = None

    def add(self, samples, rate):
        """samples: (frames, channels), full scale 1.0, in order."""
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim == 1:
            samples = samples[:, None]
        if not len(samples) or not rate:
            return
        if self.rate is None:
            self.rate = rate
            n = max(1, int(round(rate * LOUD_BLOCK_S)))
            # One-sided spectrum: every bin but DC (and Nyquist, for an even n) counts twice.
            scale = np.full(n // 2 + 1, 2.0)
            scale[0] = 1.0
            if n % 2 == 0:
                scale[-1] = 1.0
            self._weights = k_weight_power(np.fft.rfftfreq(n, 1.0 / rate)) * scale / (n * n)
        if self._carry is not None and self._carry.shape[1] == samples.shape[1]:
            samples = np.concatenate((self._carry, samples))
        n = max(1, int(round(self.rate * LOUD_BLOCK_S)))
        whole = len(samples) // n
        if whole:
            blocks = samples[:whole * n].reshape(whole, n, samples.shape[1])
            power = np.abs(np.fft.rfft(blocks, axis=1)) ** 2
            self._blocks.append((power * self._weights[None, :, None]).sum(axis=(1, 2)).astype(np.float32))
        self._carry = samples[whole * n:]

    def energies(self):
        return np.concatenate(self._blocks).tobytes() if self._blocks else b""


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
    """Folds decoded buffers, in order, into one peak per 1/PEAK_RATE s."""

    def __init__(self):
        self._peaks = np.zeros(PEAK_RATE * 60, dtype=np.float32)
        self._count = 0          # peaks written so far
        self._samples = 0        # sample frames seen so far
        self.rate = None

    def add(self, samples, rate):
        """samples: shape (frames, channels) or (frames,), full scale 1.0."""
        samples = np.abs(np.asarray(samples, dtype=np.float32))
        if samples.ndim == 2:
            samples = samples.max(axis=1)
        n = len(samples)
        if not n or not rate:
            return
        self.rate = self.rate or rate
        ids = (np.arange(self._samples, self._samples + n, dtype=np.int64) * PEAK_RATE) // int(self.rate)
        self._samples += n
        starts = np.concatenate(([0], np.flatnonzero(np.diff(ids)) + 1))
        values, buckets = np.maximum.reduceat(samples, starts), ids[starts]
        need = int(buckets[-1]) + 1
        if need > len(self._peaks):
            grown = np.zeros(max(need, len(self._peaks) * 2), dtype=np.float32)
            grown[:len(self._peaks)] = self._peaks
            self._peaks = grown
        np.maximum.at(self._peaks, buckets, values)
        self._count = max(self._count, need)

    def codes(self):
        return to_codes(self._peaks[:self._count]).tobytes()


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
    """(peak bytes, loudness bytes) from the cache, or None."""
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
    return codes, loud


def save_cached(path, result):
    target = cache_path(path)
    if not target:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        # Loudness first: a .peaks file is only ever there with its .loud.
        for data, where in ((result[1], target[:-len(".peaks")] + ".loud"), (result[0], target)):
            temp = f"{where}.{os.getpid()}.tmp"
            with open(temp, "wb") as f:
                f.write(data)
            os.replace(temp, where)
    except OSError:
        pass            # no cache this time: it's decoded again next session


def decode(path, progress=None, cancelled=lambda: False):
    """((peak bytes, loudness bytes), None) or (None, why). Runs its own
    event loop, so it belongs on a worker thread."""
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder
    import time

    if not os.path.isfile(path):
        return None, "The file is offline."
    acc, loud = PeakAccumulator(), LoudnessAccumulator()
    state = {"error": None, "done": False, "duration": -1, "said": 0.0}
    loop = QEventLoop()
    decoder = QAudioDecoder()
    stall = QTimer()
    stall.setSingleShot(True)
    stall.timeout.connect(loop.quit)

    def on_buffer():
        if cancelled():
            return loop.quit()
        got = buffer_samples(decoder.read())
        if got is not None:
            acc.add(*got)
            loud.add(*got)
        stall.start(STALL_MS)
        now = time.monotonic()
        if progress and state["duration"] > 0 and now - state["said"] >= PROGRESS_EVERY_S:
            state["said"] = now
            progress(min(1.0, decoder.position() / state["duration"]))

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
    decoder.start()
    loop.exec()
    stall.stop()
    decoder.stop()
    if cancelled():
        return None, "Stopped."
    if state["error"]:
        return None, state["error"]
    if not state["done"]:
        return None, "Decoding stalled."
    if acc.rate is None:
        return None, "The file has no audio Buddy can read."
    return (acc.codes(), loud.energies()), None


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
        """For quitting: a QThread destroyed while running crashes the exit."""
        self._stop.set()
        self._queue = []
        if self._thread is not None:
            try:
                self._thread.finished_peaks.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._thread.wait(5000)
