#!/usr/bin/env python3
"""
Asset Manager's audio/video preview, without a widget: Qt Multimedia plays
the file (its FFmpeg backend reads every format the library takes -
QtWebEngine's Chromium can't play H.264 or AAC, so a web <video> would fail
on most footage), and the page is sent what to draw:

    frame(asset_id, data_url)       a video frame, as a small JPEG
    waveform(asset_id, dict)        {"bars": [...], "progress": 0-1, "done"}
    ready(asset_id, kind)           playable now ("audio" / "video")
    position(position_ms, duration_ms)
    playing(bool)
    failed(asset_id, message)

The playback rules:
a video is primed with no audio output attached - a brief play() is the
only way this backend produces a first frame - then paused on that frame
with audio reattached; a waveform is decoded on a worker thread and grows
left to right while it does; seeking never changes whether it's playing;
anything still arriving for an asset you've clicked away from is dropped.
"""

import base64
from time import monotonic

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QThread, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

from .library_view import WAVEFORM_BARS, waveform_bars
from .waveform import decode_waveform_levels

# Frames go to the page at most this often while playing (a paused player
# sends the one frame it's on).
FRAME_INTERVAL_MS = 40
FRAME_MAX = 640          # longest side of a streamed frame, px
FRAME_QUALITY = 78


def image_data_url(image, max_side=FRAME_MAX, quality=FRAME_QUALITY, fmt="JPG"):
    """A QImage as a data: URL the page can show, scaled to fit max_side."""
    if image.width() > max_side or image.height() > max_side:
        image = image.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, fmt, quality)
    buffer.close()
    mime = "image/jpeg" if fmt.upper() in ("JPG", "JPEG") else "image/png"
    return f"data:{mime};base64,{base64.b64encode(bytes(data)).decode('ascii')}"


class _WaveformWorker(QThread):
    progressed = Signal(str, float, object)       # asset_id, fraction, bars so far
    finished_levels = Signal(str, object, object)  # asset_id, bars or None, error or None

    def __init__(self, asset_id, path, parent=None):
        super().__init__(parent)
        self.asset_id = asset_id
        self._path = path

    def run(self):
        def on_progress(fraction, levels):
            # The decoded part gets its share of the final bar count, so the
            # waveform grows left to right at the density it ends at.
            bars = max(1, round(WAVEFORM_BARS * fraction)) if fraction > 0 else WAVEFORM_BARS
            self.progressed.emit(self.asset_id, fraction, waveform_bars(levels, bars))

        levels, error = decode_waveform_levels(self._path, progress_callback=on_progress)
        self.finished_levels.emit(self.asset_id, waveform_bars(levels) if levels else None, error)


class MediaPreview(QObject):
    frame = Signal(str, str)
    waveform = Signal(str, object)
    ready = Signal(str, str)
    position = Signal(int, int)
    playing = Signal(bool)
    failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.sink = QVideoSink(self)
        self.player.setVideoOutput(self.sink)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(lambda d: self.position.emit(self.player.position(), d))
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.playbackStateChanged.connect(
            lambda s: self.playing.emit(s == QMediaPlayer.PlayingState))
        self.player.errorOccurred.connect(self._on_error)
        self.sink.videoFrameChanged.connect(self._on_video_frame)

        self._pending_id = None     # the asset on screen
        self._pending_path = None
        self._current_id = None     # ... once it's ready to play
        self._kind = None
        self._priming_id = None
        self._workers = []
        self._last_frame_at = 0.0

    # ------------------------------------------------------------ control --

    def show(self, asset_id, path, category):
        """Starts previewing an Audio or Video asset (anything else just
        stops whatever was playing)."""
        self.stop()
        if category == "Audio":
            self._pending_id, self._pending_path, self._kind = asset_id, path, "audio"
            worker = _WaveformWorker(asset_id, path, self)
            worker.progressed.connect(self._on_waveform_progress)
            worker.finished_levels.connect(self._on_waveform_done)
            worker.finished.connect(lambda w=worker: self._forget_worker(w))
            self._workers.append(worker)
            worker.start()
        elif category == "Video":
            self._pending_id, self._pending_path, self._kind = asset_id, path, "video"
            # No audio output at all while priming: a mute flag can race a
            # buffer the backend already queued, but with nothing attached
            # there's no route for sound. Reattached once paused on frame 1.
            self.player.setAudioOutput(None)
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.setVideoOutput(self.sink)   # some backends drop it across a source change

    def stop(self):
        """Stops and forgets the current media (the page shows something else)."""
        self._pending_id = self._pending_path = self._current_id = self._kind = self._priming_id = None
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setMuted(False)

    def pause(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()

    def toggle(self, asset_id=None):
        if self._current_id is None or (asset_id is not None and asset_id != self._current_id):
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek(self, fraction, asset_id=None):
        """Moves the playhead without changing whether it's playing."""
        if self._current_id is None or (asset_id is not None and asset_id != self._current_id):
            return
        duration = self.player.duration()
        if duration > 0:
            self.player.setPosition(int(max(0.0, min(1.0, float(fraction))) * duration))

    def set_volume(self, value):
        self.audio_output.setVolume(max(0.0, min(1.0, float(value))))

    def shutdown(self):
        """For quitting: stop, then end and wait out any waveform decode - a
        QThread destroyed while running crashes the exit."""
        self.stop()
        for worker in list(self._workers):
            try:
                worker.progressed.disconnect()
                worker.finished_levels.disconnect()
            except (RuntimeError, TypeError):
                pass
            worker.quit()
            worker.wait(5000)

    # ----------------------------------------------------------- waveform --

    def _forget_worker(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    def _on_waveform_progress(self, asset_id, fraction, bars):
        if asset_id == self._pending_id:
            self.waveform.emit(asset_id, {"bars": bars, "progress": fraction, "done": False})

    def _on_waveform_done(self, asset_id, bars, error):
        if asset_id != self._pending_id:
            return
        if not bars:
            self.failed.emit(asset_id, error or "Couldn't read this file's audio.")
            return
        self.waveform.emit(asset_id, {"bars": bars, "progress": 1.0, "done": True})
        self._current_id = asset_id
        self.player.setSource(QUrl.fromLocalFile(self._pending_path))
        self.ready.emit(asset_id, "audio")

    # -------------------------------------------------------------- video --

    def _on_media_status(self, status):
        if (self._kind != "video" or self._pending_id is None
                or self._current_id == self._pending_id or self._priming_id == self._pending_id
                or status not in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia)):
            return
        self._priming_id = self._pending_id
        # A seek while stopped never produces a frame on this backend; play()
        # has to run for a moment. setPosition is asynchronous, so play only
        # once it has landed, or a random frame flashes first.
        if self.player.position() == 0:
            self.player.play()
        else:
            asset_id = self._pending_id

            def on_seeked(_pos):
                self.player.positionChanged.disconnect(on_seeked)
                if asset_id == self._pending_id:
                    self.player.play()

            self.player.positionChanged.connect(on_seeked)
            self.player.setPosition(0)

    def _on_video_frame(self, frame):
        if self._kind != "video" or self._pending_id is None or not frame.isValid():
            return
        first = self._current_id != self._pending_id
        now = monotonic()
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        if not first and playing and (now - self._last_frame_at) * 1000 < FRAME_INTERVAL_MS:
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._last_frame_at = now
        self.frame.emit(self._pending_id, image_data_url(image))
        if first:
            # The first real frame: stop the priming play, and give it its
            # sound back - everything from here is the user's own doing.
            self._current_id = self._pending_id
            self.player.pause()
            self.player.setAudioOutput(self.audio_output)
            self.ready.emit(self._current_id, "video")

    # ------------------------------------------------------------- shared --

    def _on_position(self, position_ms):
        if self._current_id is not None and self._current_id == self._pending_id:
            self.position.emit(position_ms, self.player.duration())

    def _on_error(self, _error, message=""):
        if self._pending_id is not None:
            self.failed.emit(self._pending_id, message or self.player.errorString() or "Couldn't play this file.")
