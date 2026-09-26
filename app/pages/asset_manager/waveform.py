#!/usr/bin/env python3
"""
Audio decoding for Asset Manager's waveform preview - real sample data via
Qt Multimedia's QAudioDecoder (FFmpeg-backed, so it reads every format the
library calls Audio: mp3/wav/aac/flac/m4a/ogg). player.py runs it on a
worker thread and library_view.waveform_bars turns the levels into the
bars the page draws.
"""

import os

_WAVEFORM_TIMEOUT_MS = 8000

# (struct/array typecode, normalization divisor) per QAudioFormat.SampleFormat.
# UInt8 is handled separately below since it needs an offset (unsigned,
# centered on 128) rather than a plain divisor.
_SAMPLE_TYPECODES = {
    "Int16": ("h", 32768.0),
    "Int32": ("i", 2147483648.0),
    "Float": ("f", 1.0),
}


def _buffer_level(buf):
    """Returns (rms, peak) of buf's own samples, each normalized to roughly
    [0, 1] regardless of the decoder's actual sample format - channels are
    not separated (an interleaved stereo buffer's L/R samples are just
    treated as one combined pool), which is fine for an overview waveform
    shape rather than a per-channel scope.

    Both, not just one: modern mastered/loudness-limited music sits close
    to full scale almost continuously, so a pure min/max PEAK waveform for
    a typical song renders as a near-solid, barely-varying block, while a
    short, more dynamic clip looks fine on peak alone. Pure RMS avoids that
    (it reflects average energy instead of momentary peaks, so it still
    shows real light/loud structure even when true peaks are clipped-flat)
    but on its own smooths away individual transients (drum hits, accents)
    that a real waveform display shows as sharp spikes above the general
    envelope. library_view.waveform_bars blends both together so transient detail survives without reintroducing
    the brick-wall problem."""
    import array, math

    fmt = buf.format()
    sample_format = fmt.sampleFormat().name
    raw = bytes(buf.constData())
    if not raw:
        return None

    if sample_format == "UInt8":
        values = array.array("B", raw)
        if not values:
            return None
        centered = [(v - 128) / 128.0 for v in values]
        rms = math.sqrt(sum(v * v for v in centered) / len(centered))
        peak = max(abs(v) for v in centered)
        return rms, peak

    typecode_divisor = _SAMPLE_TYPECODES.get(sample_format)
    if typecode_divisor is None:
        return None  # an exotic sample format this app has no reader for
    typecode, divisor = typecode_divisor
    itemsize = array.array(typecode).itemsize
    usable_len = (len(raw) // itemsize) * itemsize
    if usable_len == 0:
        return None
    values = array.array(typecode, raw[:usable_len])
    if not values:
        return None
    rms = math.sqrt(sum((v / divisor) ** 2 for v in values) / len(values))
    peak = max(abs(min(values)), abs(max(values))) / divisor
    return rms, peak


_WAVEFORM_PROGRESS_INTERVAL_MS = 90


def decode_waveform_levels(path, progress_callback=None):
    """Runs the QAudioDecoder loop for `path` and returns (levels, error_message)
    once fully decoded (or failed/timed out). If given, progress_callback(fraction,
    levels_snapshot) is called every _WAVEFORM_PROGRESS_INTERVAL_MS while decoding
    is still in progress, so a caller running this on a background thread (see
    player._WaveformWorker) can stream a growing left-to-right preview
    instead of only getting a result once the whole file is done. fraction is
    the decoder's own playback-position-based progress (0-1); it stays 0.0 for
    the (rare) file whose duration Qt Multimedia can't determine up front, since
    there's then no reliable way to estimate how much is left."""
    try:
        from PySide6.QtCore import QUrl, QEventLoop, QTimer
        from PySide6.QtMultimedia import QAudioDecoder
    except ImportError:
        return [], None

    levels = []
    state = {"error": None, "duration_ms": -1, "finished": False}
    loop = QEventLoop()
    decoder = QAudioDecoder()

    def on_buffer_ready():
        buf = decoder.read()
        level = _buffer_level(buf)
        if level is not None:
            levels.append(level)

    def on_finished():
        state["finished"] = True
        loop.quit()

    def on_error(*_args):
        state["error"] = decoder.errorString()
        loop.quit()

    def on_duration_changed(duration_ms):
        if duration_ms > 0:
            state["duration_ms"] = duration_ms

    def emit_progress():
        duration_ms = state["duration_ms"]
        fraction = min(1.0, decoder.position() / duration_ms) if duration_ms > 0 else 0.0
        progress_callback(fraction, list(levels))

    try:
        decoder.bufferReady.connect(on_buffer_ready)
        decoder.finished.connect(on_finished)
        decoder.error.connect(on_error)
        decoder.durationChanged.connect(on_duration_changed)
        decoder.setSource(QUrl.fromLocalFile(path))

        timeout_timer = QTimer()
        timeout_timer.setSingleShot(True)
        timeout_timer.timeout.connect(loop.quit)
        timeout_timer.start(_WAVEFORM_TIMEOUT_MS)

        progress_timer = None
        if progress_callback:
            progress_timer = QTimer()
            progress_timer.timeout.connect(emit_progress)
            progress_timer.start(_WAVEFORM_PROGRESS_INTERVAL_MS)

        decoder.start()
        loop.exec()
        timeout_timer.stop()
        if progress_timer:
            progress_timer.stop()
        decoder.stop()
        if not state["finished"] and not state["error"]:
            state["error"] = "Decode timed out"
    except Exception as exc:
        state["error"] = str(exc)

    error_message = None
    if state["error"]:
        error_message = f"Waveform unavailable for '{os.path.basename(path)}': {state['error']}"
        levels = []
    return levels, error_message
