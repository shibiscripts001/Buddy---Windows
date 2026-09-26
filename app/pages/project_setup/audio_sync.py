#!/usr/bin/env python3
"""
Waveform sync for the Align tab: finds the time offset between two clips'
audio by cross-correlation, independent of any embedded timecode or
date/time metadata (which may be missing, wrong, or never jam-synced).

Resolve's own scripting API exposes MediaPool.AutoSyncAudio(), but it only
ever *merges* a video clip with a separate audio clip (replacing/appending
embedded audio) - it has no way to hand back the computed offset, which is
the whole point here (placing each clip on its own separate timeline
track at its correct position). Hence this rolls its own, via ffmpeg for
decoding (ffmpeg_utils) and a plain FFT cross-correlation below (numpy
only - no scipy dependency).
"""

import numpy as np

from .ffmpeg_utils import NoAudioStreamError, decode_mono_pcm

SAMPLE_RATE = 8000  # plenty for correlation timing at video-frame precision;
                     # keeps FFT sizes (and decode time) small for long clips

# A track whose loudest sample never gets above this is treated as carrying
# nothing to sync against - digital silence, or a channel holding only
# preamp hiss. Both are common on real footage (a blank second track, an
# unplugged input, a safety channel that was never armed) and neither can
# produce a meaningful correlation. Catching them here matters more than it
# looks: peak-normalizing a noise-floor-only track scales its hiss up to
# full scale, at which point it correlates like any other signal and yields
# a confident-looking but entirely arbitrary offset.
SILENCE_PEAK = 1e-3

# How many times taller the best correlation peak must be than the best
# peak anywhere else, for the match to count as real.
#
# Measured on actual footage rather than synthetic signals, which is the
# whole reason this replaced the previous test. That one compared the peak
# against the standard deviation of the correlation, with a threshold of 10
# chosen from synthetic broadband noise - unrelated pairs scored 5-7 there,
# so 10 looked safe. Real audio is nothing like white noise: clips from the
# same room, mic and preamp share an envelope and a spectrum, and on real
# footage genuinely non-overlapping pairs scored 24.3, 17.5, 7.4,
# 6.3 and 7.2 - two of five sailing past the threshold and being placed at
# an invented position.
#
# Peak-versus-runner-up asks the question that actually matters: is there
# ONE clear winner, or fifty roughly equal bumps of which some bump had to
# be tallest? On the same footage the genuine overlap scored 6.70 while the
# five false pairs scored 1.01 to 1.09 - the two populations do not come
# close to touching, which the previous measure could not manage.
MIN_PEAK_RATIO = 2.0

# The runner-up is looked for at least this far from the winning lag, so the
# shoulders of the true peak are not mistaken for a rival.
PEAK_GUARD_SECONDS = 0.5


class NoAudioError(RuntimeError):
    """A clip carries no audible audio at all - reported per clip as a
    warning ("left unsynced") rather than failing the whole batch."""


def _load_samples(ffmpeg_path, file_path, max_seconds=None, start_seconds=None, channels=None):
    try:
        raw = decode_mono_pcm(ffmpeg_path, file_path, SAMPLE_RATE, max_seconds=max_seconds,
                              start_seconds=start_seconds, channels=channels)
    except NoAudioStreamError:
        # A video-only file (e.g. an FX6 MXF from a take with no mic
        # connected) has nothing to decode at all - same practical outcome
        # as digital silence for every caller here (is_silent should
        # report True, compute_offset_seconds should leave it unsynced),
        # so it's folded into the same error rather than surfacing as a
        # raw ffmpeg failure.
        raise NoAudioError("has no audio stream at all")
    samples = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    if samples.size == 0:
        raise NoAudioError("decoded to no audio at all")
    samples -= samples.mean()
    peak = np.abs(samples).max()
    if peak <= SILENCE_PEAK:
        raise NoAudioError("its audio is silent (or only noise floor)")
    samples /= peak  # normalizes gain differences between cameras/recorders
    return samples


def is_silent(ffmpeg_path, file_path, start_seconds=None, duration_seconds=None, channels=None):
    """True if file_path's audio - across the given [start_seconds,
    start_seconds + duration_seconds) window, or its entire length if
    neither is given - never rises above SILENCE_PEAK. Used by Collapse's
    "delete silent audio clips" option to find audio placed on the
    timeline that carries nothing audible at all: a blank safety track, an
    unplugged input, digital silence.

    Deliberately decodes the clip's FULL trimmed duration by default
    (duration_seconds=None, not some short prefix like Waveform sync's own
    max_seconds window) - a clip can easily open on room tone or a long
    silent pause and only carry real audio later, so sampling just the
    start would misclassify it as empty.

    channels (from ffmpeg_utils.channels_for_track) restricts the check to
    the channels making up ONE of the clip's audio tracks. Passing it is
    what makes this usable at all on real footage: without it the decode
    mixes every audio stream in the file together (the right thing for
    waveform sync, see decode_mono_pcm), so a blank track sharing a file
    with a live one always came back loud and nothing was ever found
    silent."""
    try:
        _load_samples(ffmpeg_path, file_path, max_seconds=duration_seconds,
                      start_seconds=start_seconds, channels=channels)
    except NoAudioError:
        return True
    return False


# Sample rate for locating a long recording inside a whole timeline's worth
# of scratch audio. Far lower than SAMPLE_RATE because the search covers
# hours rather than a minute: a three-hour timeline at 8kHz is 86 million
# samples before the FFT even starts. 1kHz still resolves to a millisecond,
# which is a fortieth of a frame - accuracy is not what limits this.
LOCATE_RATE = 1000

# How far the winning position must stand out from the 99.9th percentile of
# the whole correlation field for a located recording to be accepted.
#
# Deliberately NOT MIN_PEAK_RATIO: that measures the peak against the single
# next-best peak, which is the right question when two clips are matched
# against each other, and the wrong one here (see locate_in_reference).
#
# Calibrated on one project - ten recordings whose positions were confirmed
# independently, and one short fragment that genuinely should not place -
# where true matches scored 8.51 and up and the fragment scored 3.65. 6.0
# sits in that gap. It is one project's worth of evidence, so the score is
# reported for every rejection rather than the decision being silent.
LOCATE_MIN_STANDOUT = 6.0

# The lowest rate the matching still works at. Not a resolution limit -
# 500 Hz resolves 2 ms, a twentieth of a frame - but a BANDWIDTH one: the
# correlation only sees content below half the rate, and speech
# fundamentals live around 85-255 Hz. Measured on ten known-good matches:
# 1000 Hz placed all ten with a worst score of 8.5, 500 Hz placed all ten
# at 6.8, 250 Hz placed eight, 125 Hz placed one. Going below this trades a
# loud failure for a silent one.
LOCATE_MIN_RATE = 500

# What one transform may cost, in the units transform_cost() computes.
#
# Those units UNDERSTATE real process memory by roughly half: numpy's FFT
# does its work in C, so its workspace never appears in a Python-level
# count. Measured with the OS's own figure, a 462-minute timeline at 1 kHz
# predicted ~597 MB here and actually held 1.09 GB. So this number is
# deliberately about half of the real ceiling it buys.
#
# 250 MB here therefore means roughly half a gigabyte in Task Manager, which
# is where this belongs: the app runs inside Resolve's process, competing
# with its media cache, and a sync is not worth a gigabyte of someone
# else's memory.
LOCATE_PEAK_BUDGET = 250e6


def transform_cost(rate, span_seconds, longest_seconds):
    """Peak bytes one locate needs at this rate: the padded input, the
    reference spectrum, the sample's spectrum, their product, and the
    inverse transform's output."""
    size = _fast_length(int(span_seconds * rate) + 1 + int(longest_seconds * rate))
    spectrum = (size // 2 + 1) * 8
    return size * 4 + spectrum * 3 + size * 4


def choose_locate_rate(span_seconds, longest_seconds, budget=LOCATE_PEAK_BUDGET):
    """The highest rate whose transform fits the budget, never below
    LOCATE_MIN_RATE. Chosen from the timeline's length before anything is
    decoded, so the common case never has to fail and retry."""
    rate = LOCATE_RATE
    while rate > LOCATE_MIN_RATE:
        if transform_cost(rate, span_seconds, longest_seconds) <= budget:
            return rate
        rate //= 2
    return max(rate, LOCATE_MIN_RATE)


def decimate(signal):
    """Halve a signal's sample rate by averaging adjacent pairs.

    Used only to retry after a failed transform: it is what decoding at half
    the rate would roughly have produced, and it costs no re-reading."""
    usable = (len(signal) // 2) * 2
    return signal[:usable].reshape(-1, 2).mean(axis=1).astype(np.float32)

# The same measure, but for locating one CLIP inside another clip's audio
# (waveform Align) rather than a long recording inside a whole timeline's
# scratch track. Higher, because a 31-second clip carries a sixtieth of the
# evidence a 30-minute recording does and its scores sit far lower.
#
# Measured on 137 clips against a 30.8-minute reference: correct placements
# scored 3.60-82.06, wrong placements 1.88-4.96, and clips sharing no audio
# at all reached 6.06. Those ranges overlap, so no threshold is clean. 7.0
# clears every wrong answer seen at the cost of declining four correct ones
# - the right way to be wrong, since a declined clip stays where it is and
# is named in the log, while a falsely placed one is silently out of sync.
ALIGN_MIN_STANDOUT = 7.0

# For lining clips up against ONE origin clip with no metadata at all, which
# is the hardest version of this: the reference is a single recording rather
# than a whole timeline's scratch track, so there is far less to match
# against and near-misses score higher.
#
# Measured on 137 clips against a 30.8-minute origin: at 6 it placed 35 with
# 21 WRONG, at 10 it placed 11 with 2 wrong, at 12 it placed 6 with none
# wrong. Correct placements scored 4.4-70.7 and incorrect ones 2.0-11.7, so
# the ranges overlap and no threshold keeps every correct answer. 12 is
# where the wrong ones stop.
#
# Precision over recall, deliberately: a clip left alone is visible and gets
# dragged in seconds, while a clip placed wrongly looks finished.
IMPROVISED_MIN_STANDOUT = 12.0

# The same measure, but for locating one CLIP inside another clip's audio
# (waveform Align) rather than a long recording inside a whole timeline's
# scratch track. Higher, because a 31-second clip carries a sixtieth of the
# evidence a 30-minute recording does and its scores sit far lower.
#
# Measured on 137 clips against a 30.8-minute reference: correct placements
# scored 3.60-82.06, wrong placements 1.88-4.96, and clips sharing no audio
# at all reached 6.06. Those ranges overlap, so no threshold is clean. 7.0
# clears every wrong answer seen at the cost of declining four correct ones
# - the right way to be wrong, since a declined clip stays where it is and
# is named in the log, while a falsely placed one is silently out of sync.
ALIGN_MIN_STANDOUT = 7.0


def decode_timeout(duration_seconds):
    """How long to allow one decode before calling it stuck.

    Scaled by how much audio is wanted, because the flat default was sized
    for short clips and camera originals are not: a single 15-minute take
    can be a 28 GB file, and four of those streaming at once off a network
    drive took longer than two minutes just to read. This is a guard against
    a hung process, not a schedule, so it is deliberately loose."""
    return max(600.0, float(duration_seconds) * 8.0)


def load_window(ffmpeg_path, file_path, start_seconds, duration_seconds,
                rate=LOCATE_RATE):
    """Mono samples for one stretch of a file, normalised, as float32.

    float32 throughout: a timeline's worth of scratch audio is large enough
    that the FFT's working copies matter, and numpy keeps the transform in
    single precision if the input is."""
    raw = decode_mono_pcm(ffmpeg_path, file_path, rate,
                          max_seconds=duration_seconds, start_seconds=start_seconds,
                          timeout=decode_timeout(duration_seconds))
    samples = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if samples.size == 0:
        raise NoAudioError("decoded to no audio at all")
    samples -= samples.mean()
    peak = float(np.abs(samples).max())
    if peak <= SILENCE_PEAK:
        raise NoAudioError("its audio is silent (or only noise floor)")
    return samples / peak


def _fast_length(need):
    """Smallest 5-smooth number (only factors 2, 3 and 5) that is at least
    `need`.

    numpy's FFT is fast at these, and they sit far closer together than
    powers of two: a 24.0M-sample correlation pads to 25.2M here against
    33.5M for the next power of two, which is a quarter less memory and
    arithmetic for exactly the same answer. Padding only has to be long
    enough that the circular correlation cannot wrap; any length past that
    changes nothing in the region actually searched."""
    best = 1
    while best < need:
        best *= 2
    power_of_two = best

    best = power_of_two
    five = 1
    while five < need:
        three = five
        while three < need:
            candidate = three
            while candidate < need:
                candidate *= 2
            best = min(best, candidate)
            three *= 3
        five *= 5
    return best


def prepare_reference(reference, longest_sample):
    """Transforms the reference signal once, ready to match many recordings
    against. Returns an opaque handle for locate_in_reference.

    Worth doing separately because the reference is the big one: a six-hour
    timeline at LOCATE_RATE is 22 million samples, and transforming it again
    for every recording would repeat the most expensive step of the whole
    pass. The transform is padded past the reference by the longest
    recording so that no correlation wraps around the end into the start."""
    size = _fast_length(len(reference) + longest_sample)
    return size, np.fft.rfft(reference, size), len(reference)


def locate_in_reference(prepared, sample, rate=LOCATE_RATE, allow_before=False):
    """Where `sample` sits inside the prepared reference. Returns
    (seconds, standout).

    The reference is laid out in its own time, so the answer is directly the
    position the sample belongs at within it.

    Only a bounded window of the transform holds real answers. Forward, that
    is the first len(reference) lags: the rest is zero-padding, and searching
    it is a way to return confident nonsense - the padded length is the next
    power of two, so on a six-hour timeline half of it lands beyond 4h40m,
    and reading those indices as negative lags threw away every genuine match
    in the last quarter of the timeline.

    allow_before adds the negative lags, which live at the top of the
    circular transform. Needed when the reference is a single CLIP, since
    another clip can perfectly well have started rolling before it did.
    Wrong when the reference spans the whole timeline from zero, because
    nothing precedes the start of a timeline - hence the flag rather than
    always searching both.

    `standout` is the peak over the 99.9th percentile of the searched field,
    NOT over the runner-up. A recorder rolling in fixed-length chunks
    through one continuous scene produces a strong near-repeat one chunk from
    the truth, and that lone rival halves a runner-up score while barely
    shifting a percentile. A sample too short to be distinctive fails the
    other way - the entire landscape sits high - which a percentile catches
    and a runner-up does not."""
    size, reference_fft, reference_length = prepared
    cross = np.fft.irfft(reference_fft * np.conj(np.fft.rfft(sample, size)), size)

    if allow_before:
        before = min(len(sample), size - reference_length)
        positions = np.concatenate((cross[size - before:], cross[:reference_length]))
        origin = -before
    else:
        positions = cross[:reference_length]
        origin = 0

    index = int(np.argmax(positions))
    peak = float(positions[index])
    field = float(np.percentile(positions, 99.9)) if peak > 0 else 0.0
    # Released before returning rather than on the next collection: `cross`
    # alone is a third of a gigabyte on a long timeline, and this is running
    # inside Resolve's process, not a tool of its own.
    del cross, positions
    if peak <= 0:
        return (origin + index) / rate, 0.0
    standout = peak / field if field > 0 else float("inf")
    return (origin + index) / rate, standout


# ---------- onset matching (for footage with no timecode) ----------

# Frames per second in the onset envelope. 50 gives 20ms resolution - half a
# frame at 24fps - and makes the arrays twenty times smaller than the audio
# they came from, which is most of why this is fast enough to compare every
# pair of clips against every other.
ONSET_RATE = 50

# How sharply a pair must stand out before it is even considered. Low on
# purpose: the segment check below is what actually decides, and a bar high
# enough to filter alone would throw away real matches.
ONSET_MIN_SCORE = 6.0

# Of three stretches of the overlap, how many must independently agree on
# the same offset. Two of three took the edge set to zero wrong answers; a
# spurious peak from repetitive content agrees in one place and disagrees in
# the others.
ONSET_MIN_SEGMENTS = 2
ONSET_SEGMENT_TOLERANCE = 2      # envelope frames, so 40ms

# How close a camera clip's own answer must be to a candidate position for it
# to count as agreeing with it. One second is far looser than the matching
# resolves (a millisecond) - it is a test of whether two methods point at the
# same moment, not a measurement.
CORROBORATION_TOLERANCE = 1.0

# How many camera clips must independently agree before a position is taken on
# their word alone, with no requirement that the peak also stand out.
#
# The measure this backs up: a peak against the 99.9th percentile says one
# position beat the rest of the search, and on a timeline holding several days
# of unrelated footage that can be true of a position no clip supports at all.
# Measured on a four-day, 550-clip project: a 10.5-minute recording scored
# 10.85 - comfortably past LOCATE_MIN_STANDOUT - at a position NONE of the 40
# clips overlapping it agreed with, and the camera had in fact been rolling for
# 58 seconds of those ten minutes. Across the same project's 27 loose
# recordings the two populations do not overlap at all:
#
#   16 recordings with a real position:  1 to 8 clips agree (every one >= 1)
#   11 recordings with none:             0 clips agree (bar two 3-second
#                                        fragments, which agree once each and
#                                        are caught by the standout instead)
#
# Independent clips agreeing is also evidence a single tall peak cannot fake:
# each clip is matched against the recording on its own, so nothing they share
# but the moment they were shot can put them at the same answer.
MIN_CORROBORATION = 2


def corroborating_positions(sample_env, clip_envs):
    """Where each camera clip, matched against the recording ALONE, says that
    recording starts relative to the clip's own start.

    Yields (index, seconds, standout) per entry in clip_envs, seconds being
    how far into the recording that clip begins - so a clip at timeline
    position P puts the recording at P - seconds. seconds is None when the
    clip has nothing to match.

    The recording is transformed once and the result reused for every clip,
    which is what makes asking every overlapping clip affordable: the naive
    loop re-transformed the recording each time and cost 74s on a 27-recording
    project, against 5s here for the same answers."""
    if len(sample_env) == 0 or not clip_envs:
        return []
    longest = max(len(env) for env in clip_envs)
    size = _fast_length(len(sample_env) + longest)
    spectrum = np.fft.rfft(sample_env, size)
    answers = []
    for index, env in enumerate(clip_envs):
        if len(env) == 0:
            answers.append((index, None, 0.0))
            continue
        cross = np.fft.irfft(spectrum * np.conj(np.fft.rfft(env, size)), size)
        peak_index = int(np.argmax(cross))
        peak = float(cross[peak_index])
        if peak <= 0:
            answers.append((index, None, 0.0))
            continue
        field = float(np.percentile(cross, 99.9))
        lag = peak_index - size if peak_index > size // 2 else peak_index
        answers.append((index, lag / ONSET_RATE,
                        peak / field if field > 0 else float("inf")))
        del cross
    return answers



# The least two clips may share before a pair is worth considering at all.
# Under this there is not enough of anything for either method to be asked.
SHORT_OVERLAP_MIN_SECONDS = 8.0

# How close the raw waveform's answer must be to the onset envelope's for it
# to count as the same answer. A tenth of a second is two frames - far looser
# than either method resolves, and far tighter than the hundreds of seconds a
# disagreement comes out as in practice.
CONFIRM_TOLERANCE = 0.10


def raw_confirms(a_samples, b_samples, seconds, rate=LOCATE_RATE,
                 tolerance=CONFIRM_TOLERANCE):
    """Whether the raw waveform, asked on its own, puts these two clips at the
    same offset the onset envelopes did.

    This is the second opinion for pairs that share too little to split into
    stretches (see segments_agree), which on footage cut into short takes is
    most of them: 410 of 481 clips on the project this was measured against
    are under a minute, so the segment check could not even be asked.

    Raw correlation is not trustworthy ALONE - room tone and hum correlate
    with themselves at any alignment, which is what put an earlier version 61x
    onto the wrong offset - but the two methods are wrong in unrelated ways,
    so them landing on the same offset is evidence neither gives by itself.
    Measured on the 18 pairs that project discarded for sharing under a
    minute: the two agreed on 6, every one of which is independently
    confirmed real, and disagreed on the other 12, every one of which is
    spurious. The disagreements are not marginal - they come out tens to
    hundreds of seconds apart."""
    if len(a_samples) == 0 or len(b_samples) == 0:
        return False
    size = _fast_length(len(a_samples) + len(b_samples))
    cross = np.fft.irfft(np.fft.rfft(a_samples, size)
                         * np.conj(np.fft.rfft(b_samples, size)), size)
    index = int(np.argmax(cross))
    peak = float(cross[index])
    del cross
    if peak <= 0:
        return False
    lag = index - size if index > size // 2 else index
    return abs(lag / rate - seconds) <= tolerance


def onset_envelope(samples, rate=LOCATE_RATE, env_rate=ONSET_RATE):
    """When things HAPPENED in this audio, as a low-rate signal.

    Short-time energy, log-compressed, then only its rises. The differencing
    is the important part: it removes steady level entirely, so hum, hiss
    and room tone - which correlate with themselves at any alignment and
    produced confidently wrong matches - contribute nothing, while speech
    onsets, doors and claps survive."""
    frame = max(1, int(rate // env_rate))
    usable = (len(samples) // frame) * frame
    if usable < frame * 2:
        return np.zeros(0, dtype=np.float32)
    frames = samples[:usable].reshape(-1, frame).astype(np.float64)
    energy = np.log1p(np.sqrt((frames ** 2).mean(axis=1)) * 1000.0)
    rises = np.diff(energy, prepend=energy[:1])
    rises[rises < 0] = 0.0
    rises -= rises.mean()
    peak = float(np.abs(rises).max())
    return (rises / peak).astype(np.float32) if peak > 0 else rises.astype(np.float32)


def correlate_envelopes(a, b):
    """(lag in frames, standout). Positive lag means b starts that far
    after a."""
    if len(a) == 0 or len(b) == 0:
        return 0, 0.0
    size = _fast_length(len(a) + len(b))
    cross = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    index = int(np.argmax(cross))
    peak = float(cross[index])
    if peak <= 0:
        return 0, 0.0
    field = float(np.percentile(cross, 99.9))
    lag = index - size if index > size // 2 else index
    return lag, (peak / field if field > 0 else float("inf"))


def segments_agree(a, b, lag, parts=3, tolerance=ONSET_SEGMENT_TOLERANCE):
    """How many separate stretches of the overlap independently produce the
    same offset.

    This is the test that made the difference. One tall peak can come from
    anything repetitive; the same answer arriving from the start, middle and
    end of the shared region cannot. Needs 20 seconds per stretch to be
    worth asking, so brief overlaps simply score zero and are left out."""
    start, stop = max(0, lag), min(len(a), lag + len(b))
    if stop - start < parts * ONSET_RATE * 20:
        return 0
    size = (stop - start) // parts
    agreed = 0
    for part in range(parts):
        low = start + part * size
        piece_a = a[low:low + size]
        piece_b = b[low - lag:low - lag + size]
        if len(piece_a) < ONSET_RATE or len(piece_b) < ONSET_RATE:
            continue
        sub_lag, _score = correlate_envelopes(piece_a, piece_b)
        if abs(sub_lag) <= tolerance:
            agreed += 1
    return agreed
