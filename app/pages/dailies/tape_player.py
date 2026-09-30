"""
Dailies' player: two decks, so a source tape plays straight through.

Resolve's own source viewer goes from one clip into the next without a
pause, and so does this. While a clip plays, the next one on the tape is
already open on the second deck - loaded and paused on its first frame -
and at the cut the two swap: the playing deck lets go of the video surface
and the waiting one takes it and plays. The next clip's first frame reaches
the surface on the next video frame.

What it takes, measured on 4K XAVC off a network drive (Qt 6.11.2's FFmpeg
backend), because anything slow here runs on the UI thread - which is also
where the surface draws, so it is a frozen picture while it runs:

 - A deck is a fresh QMediaPlayer for every clip. setSource() on a new
   player takes 0.2 ms; on one that already has a file, 98 ms - it tears the
   old file down first. A finished deck is paused and deleted on the UI
   thread, which costs ~100 ms there. Deleting it on a thread of its own
   (moveToThread, then deleteLater) was tried and CRASHES: FFmpeg's decode
   threads are still running while the player is taken apart from the
   other thread - access violations in avcodec/avformat/avutil, in 5 of 5
   runs on a 24 GB camera clip (2026-09-30), and a user's Buddy with it.
 - A player is paused before anything else is done to it. Moving the video
   output of a PLAYING player costs 44 ms and stopping it 49 ms; paused or
   ended, both are free. (Never setVideoOutput(None) mid-play: that hangs.)
 - No priming play. A clip loaded and then paused shows its first frame (a
   seek while *stopped* is what draws nothing), so a clip opens with
   pause(), not a silent play() that has to be stopped again - and a clip
   opened while playing simply plays.
 - The surface shows a paused clip too: the active deck draws on it as soon
   as it is paused on its first frame. The page's still of a clip (its
   fallback picture, shown at once when coming back to a clip) is made only
   while nothing plays - turning a 4K frame into an image is 11-45 ms of UI
   thread, and it can't move to another thread: a Python thread that
   converts a QVideoFrame never finishes exiting, and waiting on it hangs.

At the end of a clip that was playing, the waiting deck is started right
there, before anything else runs - ended(clip, next) then tells the page
which clip it is now on, and the page only redraws around it.

Signals are about the ACTIVE deck, except still(), which comes for either
(the page keeps stills for clips it hasn't reached yet).
"""

import base64

from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, QObject, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

STILL_MAX = 1280
STILL_QUALITY = 78
_LOADED = (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia)


def _still_url(frame):
    """A frame as a JPEG data: URL (UI thread only - see the module)."""
    image = frame.toImage()
    if image.isNull():
        return None
    if image.width() > STILL_MAX or image.height() > STILL_MAX:
        image = image.scaled(STILL_MAX, STILL_MAX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPG", STILL_QUALITY)
    buffer.close()
    return "data:image/jpeg;base64," + base64.b64encode(bytes(data)).decode("ascii")


class _Deck(QObject):
    """One clip on its own player, audio output and sink - the sink is where
    it draws whenever it doesn't have the surface. Parentless: it is deleted
    when it's retired, not with the page."""

    def __init__(self, clip_id, path, kind, volume):
        super().__init__()
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(volume)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoOutput(self.sink)
        self.clip_id, self.path, self.kind = clip_id, path, kind
        # loading -> (priming, for a paused open) -> ready; or failed / retired
        self.state = "loading"
        self.want_play = False
        self.attached = False       # drawing on the surface
        self.still_due = kind == "Video"


class TapePlayer(QObject):
    still = Signal(str, str)        # clip id, data: URL of its first frame
    ready = Signal(str, str)        # clip id, kind: the active clip can play and seek
    position = Signal(int, int)     # ms, duration ms
    playing = Signal(bool)
    ended = Signal(str, str)        # clip that ended, clip now playing on from it ("" if none)
    failed = Signal(str, str)

    def __init__(self, parent, surface_output, surface_drawn):
        """surface_output: what playback draws into (video_surface.py's
        output); surface_drawn: its signal for each frame it's given."""
        super().__init__(parent)
        self.surface = surface_output
        self.active = self.spare = None
        self._volume = 1.0
        # Both decks are let go of before anything can be torn down - on
        # quitting, whoever quits.
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        surface_drawn.connect(self._on_surface_frame)

    # ------------------------------------------------------------ control --

    def open(self, clip_id, path, kind, play=False):
        """Makes clip_id the active clip - at once if it's the one waiting
        on the other deck, otherwise by loading it. `play` starts it as soon
        as it can; otherwise it waits, paused, on its first frame."""
        spare = self.spare
        if spare is not None and spare.clip_id == clip_id and spare.path == path and spare.state != "failed":
            self._retire(self.active)
            self.active, self.spare = spare, None
            spare.want_play = play
            if spare.state == "ready":
                # Ready first: the page only takes the surface's frames for a
                # clip it knows is ready.
                self.ready.emit(clip_id, spare.kind)
                if spare.kind == "Video":
                    self._attach(spare)     # paused, it hands the surface its frame at once
                if play:
                    spare.player.play()
            return
        self._retire(self.active)
        self.active = self._load(clip_id, path, kind, play)

    def preload(self, clip_id, path, kind):
        """Opens clip_id on the waiting deck, paused on its first frame."""
        if self.active is not None and self.active.clip_id == clip_id:
            return
        spare = self.spare
        if spare is not None and spare.clip_id == clip_id and spare.path == path and spare.state != "failed":
            return
        self._retire(spare)
        self.spare = self._load(clip_id, path, kind, False)

    def play(self):
        deck = self.active
        if deck is None:
            return
        deck.want_play = True
        if deck.state == "ready":
            if deck.kind == "Video":
                self._attach(deck)
            deck.player.play()

    def pause(self):
        deck = self.active
        if deck is not None:
            deck.want_play = False
            deck.player.pause()

    def is_playing(self):
        return self.active is not None and self.active.player.playbackState() == QMediaPlayer.PlayingState

    def toggle(self, clip_id=None):
        if self.active is None or (clip_id is not None and clip_id != self.active.clip_id):
            return
        self.pause() if self.is_playing() else self.play()

    def seek_ms(self, position_ms, clip_id=None):
        """Moves the playhead without starting playback; paused shows the
        exact frame (a stopped player is paused first - see the module)."""
        deck = self.active
        if deck is None or deck.state != "ready" or (clip_id is not None and clip_id != deck.clip_id):
            return
        if deck.player.playbackState() == QMediaPlayer.StoppedState:
            deck.player.pause()
        if deck.kind == "Video":
            self._attach(deck)
        duration = deck.player.duration()
        position = max(0, int(position_ms))
        deck.player.setPosition(min(position, duration - 1) if duration > 0 else position)

    def seek(self, fraction, clip_id=None):
        if self.active is not None and self.active.player.duration() > 0:
            self.seek_ms(max(0.0, min(1.0, float(fraction))) * self.active.player.duration(), clip_id)

    def set_volume(self, value):
        self._volume = max(0.0, min(1.0, float(value)))
        for deck in (self.active, self.spare):
            if deck is not None:
                deck.audio.setVolume(self._volume)

    def stop(self):
        """Closes the active clip, keeping the one waiting on the other deck.
        The surface keeps its last frame (let go of while paused)."""
        self._retire(self.active)
        self.active = None

    def stop_all(self):
        self._retire(self.active)
        self._retire(self.spare)
        self.active = self.spare = None

    def shutdown(self):
        self.stop_all()

    # ------------------------------------------------------------ internals --

    def _load(self, clip_id, path, kind, play):
        deck = _Deck(clip_id, path, kind, self._volume)
        deck.want_play = play
        player = deck.player
        player.mediaStatusChanged.connect(lambda status, d=deck: self._on_status(d, status))
        player.positionChanged.connect(lambda ms, d=deck: self._on_position(d, ms))
        player.durationChanged.connect(lambda _ms, d=deck: self._on_position(d, d.player.position()))
        player.playbackStateChanged.connect(lambda state, d=deck: self._on_playback(d, state))
        player.errorOccurred.connect(lambda _error, message="", d=deck: self._on_error(d, message))
        deck.sink.videoFrameChanged.connect(lambda frame, d=deck: self._on_deck_frame(d, frame))
        player.setSource(QUrl.fromLocalFile(path))
        return deck

    def _retire(self, deck):
        """Done with this deck: paused (free, where stopping a playing player
        isn't), off the surface, and deleted - on this thread, see the
        module."""
        if deck is None or deck.state == "retired":
            return
        deck.state = "retired"
        deck.player.pause()
        self._detach(deck)
        for obj in (deck.player, deck.sink):
            obj.blockSignals(True)
        deck.deleteLater()

    def _live(self, deck):
        return deck is not None and deck.state != "retired" and (deck is self.active or deck is self.spare)

    def _attach(self, deck):
        if deck.attached:
            return
        for other in (self.active, self.spare):
            if other is not None and other is not deck:
                self._detach(other)
        deck.player.setVideoOutput(self.surface)
        deck.attached = True

    def _detach(self, deck):
        # To the deck's own sink, never None: setVideoOutput(None) mid-play
        # hangs this backend (Qt 6.11.2).
        if deck.attached:
            deck.player.setVideoOutput(deck.sink)
            deck.attached = False

    def _on_status(self, deck, status):
        if not self._live(deck):
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if deck is self.active and deck.state == "ready":
                following = ""
                spare = self.spare
                if deck.want_play and spare is not None and spare.state in ("loading", "priming", "ready"):
                    following = spare.clip_id
                    self.open(spare.clip_id, spare.path, spare.kind, play=True)
                self.ended.emit(deck.clip_id, following)
            return
        if status == QMediaPlayer.MediaStatus.InvalidMedia and deck.state == "loading":
            self._on_error(deck, deck.player.errorString() or "Couldn't open this file.")
            return
        if deck.state != "loading" or status not in _LOADED:
            return
        if deck.kind != "Video":
            deck.state = "ready"
            if deck is self.active:
                self.ready.emit(deck.clip_id, deck.kind)
                if deck.want_play:
                    deck.player.play()
            return
        if deck is self.active and deck.want_play:
            # Opened while playing: straight into playback on the surface.
            deck.state = "ready"
            self.ready.emit(deck.clip_id, deck.kind)
            self._attach(deck)
            deck.player.play()
            return
        deck.state = "priming"
        deck.player.pause()          # its first frame arrives in its own sink

    def _on_deck_frame(self, deck, frame):
        if not self._live(deck) or deck.state != "priming" or not frame.isValid():
            return
        deck.state = "ready"
        self._make_still(deck, frame)
        if deck is self.active:
            self.ready.emit(deck.clip_id, deck.kind)
            self._attach(deck)
            if deck.want_play:
                deck.player.play()

    def _on_surface_frame(self, frame):
        deck = self.active
        # Its first frame only - not wherever it was seeked to.
        if (deck is not None and deck.still_due and deck.attached and deck.state == "ready"
                and frame.isValid() and deck.player.position() < 200):
            self._make_still(deck, frame)

    def _make_still(self, deck, frame):
        if not deck.still_due or self.is_playing():
            return          # while playing it waits for a paused showing
        deck.still_due = False
        try:
            url = _still_url(frame)
        except Exception:  # noqa: BLE001 - a missing still only means no fallback picture
            url = None
        if url:
            self.still.emit(deck.clip_id, url)

    def _on_position(self, deck, position_ms):
        if deck is self.active and deck.state == "ready":
            self.position.emit(position_ms, deck.player.duration())

    def _on_playback(self, deck, state):
        if deck is self.active:
            self.playing.emit(state == QMediaPlayer.PlayingState)

    def _on_error(self, deck, message):
        if not self._live(deck) or deck.state == "failed":
            return
        deck.state = "failed"
        if deck is self.active:
            self.failed.emit(deck.clip_id, message or deck.player.errorString() or "Couldn't play this file.")
