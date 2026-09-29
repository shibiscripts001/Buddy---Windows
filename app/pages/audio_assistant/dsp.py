#!/usr/bin/env python3
"""
Buddy's own audio effects - the ones Resolve's scripting can't reach (Fairlight
FX have no call), so they're baked into a new file (render.py). No Qt, no
Resolve: numpy only, tested directly.

numpy has no IIR filter, and a per-sample Python loop over minutes of audio is
out of the question, so everything happens in the frequency domain: the audio
is cut into overlapping windows (N_FFT samples, a new one every HOP), each
window's spectrum is multiplied by the chain's gains, and the windows are
added back together (weighted overlap-add). A square-root Hann window on the
way in and again on the way out sums to a constant at 75% overlap, so with
every gain at 1 what comes out is what went in, to float rounding. One pass
runs the whole chain: stacking effects doesn't stack windowing.

An effect's gain is either fixed per frequency (a high-pass, a notch, an EQ -
worked out once) or changes window by window (a de-esser, a noise gate - from
the spectrum itself). Either way it's magnitude only, so nothing is shifted
in time: a filter here has no phase delay, unlike its analogue original.

A chain is a list of {"id", "on", "strength" 0-1}, in the order they run -
what the page sends and render.py's sidecar keeps. EFFECTS is every effect
there is; clean_chain() makes a chain from the page safe to run.
"""

from __future__ import annotations

import numpy as np

N_FFT = 2048          # 43 ms at 48 kHz: 23 Hz a bin, fine enough for a hum notch
HOP = N_FFT // 4      # 75% overlap - what the square-root Hann pair needs
CHUNK_FRAMES = 256    # windows transformed at once: bounds the memory, not the result


def _window():
    # Periodic, not symmetric: that's the one whose overlaps sum flat.
    return np.sqrt(0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT))


def _rate_freqs(rate):
    return np.fft.rfftfreq(N_FFT, 1.0 / float(rate))


# --------------------------------------------------------------- effects --

class _Rumble:
    """A high-pass for rumble, handling noise and wind: a 4th-order (24 dB an
    octave) Butterworth's magnitude, from 50 Hz at no strength to 100 Hz at
    full. Default 0.6, so 80 Hz - under a voice's lowest notes."""

    def __init__(self, rate, strength):
        self.cutoff = 50.0 + 50.0 * strength
        f = _rate_freqs(rate)
        with np.errstate(divide="ignore"):
            ratio = np.where(f > 0, self.cutoff / np.maximum(f, 1e-9), np.inf)
        self._gain = 1.0 / np.sqrt(1.0 + ratio ** 8)

    def gain(self, _spectrum):
        return self._gain


# name: shown in the page; default: the strength a new chain gets.
EFFECTS = {
    "rumble": {"name": "Rumble filter", "default": 0.6, "make": _Rumble},
}


def clean_chain(chain):
    """A chain from the page or a sidecar made safe: unknown ids and junk
    dropped, each effect at most once, strength clamped to 0-1."""
    out, seen = [], set()
    for entry in chain if isinstance(chain, list) else []:
        if not isinstance(entry, dict) or entry.get("id") not in EFFECTS or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        try:
            strength = float(entry.get("strength", EFFECTS[entry["id"]]["default"]))
        except (TypeError, ValueError):
            strength = EFFECTS[entry["id"]]["default"]
        if not np.isfinite(strength):
            strength = EFFECTS[entry["id"]]["default"]
        out.append({"id": entry["id"], "on": bool(entry.get("on", True)),
                    "strength": round(min(1.0, max(0.0, strength)), 3)})
    return out


def active(chain):
    """The effects that are on, from a clean chain - none means nothing to render."""
    return [e for e in chain if e["on"]]


# ---------------------------------------------------------------- engine --

def process(samples, rate, chain, progress=None, cancelled=lambda: False):
    """samples (frames, channels) or (frames,), float, full scale 1.0 -> the
    same shape through the chain's effects that are on, float32. Returns None
    if cancelled. A value can come out over full scale (an EQ boost): that's
    render.py's to handle."""
    x = np.asarray(samples, dtype=np.float64)
    mono = x.ndim == 1
    if mono:
        x = x[:, None]
    frames, channels = x.shape
    effects = [EFFECTS[e["id"]]["make"](rate, e["strength"]) for e in active(clean_chain(chain))]
    if not effects or not frames:
        out = x.astype(np.float32)
        return out[:, 0] if mono else out

    # Padded so every real sample sits under all four of its windows.
    lead = N_FFT - HOP
    count = -(-(frames + lead) // HOP)                 # windows needed to reach the end
    padded = np.zeros((count * HOP + N_FFT, channels))
    padded[lead:lead + frames] = x
    out = np.zeros_like(padded)
    window = _window()
    views = np.lib.stride_tricks.sliding_window_view(padded, N_FFT, axis=0)[::HOP]   # (windows, channels, N_FFT)
    scale = HOP / (N_FFT * 0.5)                        # the Hann overlaps sum to N_FFT / (2 HOP)
    per_phase = N_FFT // HOP

    for first in range(0, count, CHUNK_FRAMES):
        if cancelled():
            return None
        last = min(count, first + CHUNK_FRAMES)
        spectrum = np.fft.rfft(views[first:last] * window, axis=-1).transpose(1, 0, 2)   # (channels, windows, bins)
        for effect in effects:
            spectrum = spectrum * effect.gain(spectrum)
        chunk = np.fft.irfft(spectrum.transpose(1, 0, 2), n=N_FFT, axis=-1) * (window * scale)
        # Windows a whole N_FFT apart don't overlap: add each of the four sets in one go.
        for phase in range(per_phase):
            part = chunk[phase::per_phase]
            if not len(part):
                continue
            at = (first + phase) * HOP
            flat = part.transpose(0, 2, 1).reshape(-1, channels)
            out[at:at + len(flat)] += flat
        if progress:
            progress(last / count)

    result = out[lead:lead + frames].astype(np.float32)
    return result[:, 0] if mono else result
