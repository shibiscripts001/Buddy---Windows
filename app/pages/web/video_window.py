#!/usr/bin/env python3
"""
Buddy's own video player for the Web tab. The browser's Chromium has no
H.264 or AAC decoder (they're licensed, so Qt doesn't ship them), which is
what almost every news site and Reddit use - so those videos just sit
there. Qt Multimedia's FFmpeg does decode them, and plays the same address:
when a page's video fails to load (engine.VIDEO_SCRIPT notices), it opens
here, in a window of its own beside Resolve, with play, seek, volume and
full screen - and Save, which downloads the file through the Web tab so it
lands in the Downloads window.
"""

import threading
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from core.i18n import tr
from pages.dailies.video_surface import VideoSurface
from pages.web import video_probe

SIZE = (960, 620)
MAX_WINDOWS = 4                       # a page can't fill the screen with players
SEEK_MS = 5000
STEP = 5                              # volume, per Up / Down
SLIDER = 1000


def clock(ms):
    seconds = max(0, int(ms // 1000))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


class _Stage(QWidget):
    """The picture's black box: a click plays or pauses, a double-click is
    full screen."""

    clicked = Signal()
    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumSize(320, 180)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()


class VideoWindow(QWidget):
    """One video, played from its web address. `save(url)` is what Save
    calls (the Web tab starts a download)."""

    def __init__(self, host, url, title="", start=0.0, save=None):
        super().__init__(None, Qt.Window)
        self.url = url
        self._save = save
        self._start_ms = int(max(0.0, float(start or 0)) * 1000)
        self._seeking = False
        self._muted = False
        self._failed = False
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(title or tr("Video"))
        self.resize(*SIZE)
        try:
            self._tokens = host.theme_tokens()
        except Exception:  # noqa: BLE001 - a host without a theme
            self._tokens = {}
        self._build()
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.8)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._surface.output)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)
        self._player.setSource(QUrl(url))
        self._player.play()

    # ------------------------------------------------------------- build --
    def _build(self):
        t = self._tokens
        surface, text, dim = t.get("surface", "#202225"), t.get("on_surface", "#e6e6e6"), t.get("outline", "#888")
        accent = t.get("primary", "#e0453a")
        self.setStyleSheet(
            f"VideoWindow {{ background: {surface}; }}"
            f"QLabel {{ color: {text}; }}"
            f"QPushButton {{ color: {text}; background: transparent; border: 1px solid {dim}; border-radius: 6px;"
            f" padding: 4px 12px; }}"
            f"QPushButton:hover {{ background: {t.get('surface_container', '#2c2f33')}; }}"
            f"QSlider::groove:horizontal {{ height: 4px; background: {dim}; border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;"
            f" background: {text}; }}")
        self._stage = _Stage(self)
        self._surface = VideoSurface(self._stage)
        self._surface.widget.show()
        self._status = QLabel("", self._stage)
        self._status.setStyleSheet("color: white; background: rgba(0,0,0,150); padding: 6px 12px; border-radius: 6px;")
        self._status.setAlignment(Qt.AlignCenter)
        self._status.hide()
        self._stage.clicked.connect(self.toggle)
        self._stage.double_clicked.connect(self.toggle_full_screen)
        self._stage.installEventFilter(self)

        self._play = QPushButton("▶")
        self._play.setFixedWidth(46)
        self._play.clicked.connect(self.toggle)
        self._time = QLabel("0:00 / 0:00")
        self._time.setMinimumWidth(96)
        self._seek = QSlider(Qt.Horizontal)
        self._seek.setRange(0, SLIDER)
        self._seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self._seek.sliderReleased.connect(self._seek_released)
        self._seek.sliderMoved.connect(self._seek_moved)
        self._mute = QPushButton("\U0001F50A")
        self._mute.setFixedWidth(46)
        self._mute.clicked.connect(self.toggle_mute)
        self._volume = QSlider(Qt.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedWidth(90)
        self._volume.valueChanged.connect(self._set_volume)
        self._save_button = QPushButton(tr("Save"))
        self._save_button.setToolTip(tr("Download this video - it appears in your Downloads"))
        self._save_button.clicked.connect(self._save_clicked)
        self._save_button.setEnabled(bool(self._save))
        self._full = QPushButton("⛶")
        self._full.setFixedWidth(46)
        self._full.setToolTip(tr("Full screen (F)"))
        self._full.clicked.connect(self.toggle_full_screen)

        self._bar = QWidget(self)
        controls = QHBoxLayout(self._bar)
        controls.setContentsMargins(10, 6, 10, 8)
        controls.setSpacing(8)
        for widget in (self._play, self._time):
            controls.addWidget(widget)
        controls.addWidget(self._seek, 1)
        for widget in (self._mute, self._volume, self._save_button, self._full):
            controls.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._stage, 1)
        layout.addWidget(self._bar)

    def eventFilter(self, obj, event):
        if obj is self._stage and event.type() == QEvent.Resize:
            self._surface.widget.setGeometry(0, 0, self._stage.width(), self._stage.height())
            self._status.adjustSize()
            self._status.move((self._stage.width() - self._status.width()) // 2,
                              (self._stage.height() - self._status.height()) // 2)
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._surface.widget.setGeometry(0, 0, self._stage.width(), self._stage.height())

    # ----------------------------------------------------------- playback --
    def toggle(self):
        if self._failed:
            return
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            if self._player.mediaStatus() == QMediaPlayer.EndOfMedia:
                self._player.setPosition(0)
            self._player.play()

    def toggle_mute(self):
        self._muted = not self._muted
        self._audio.setMuted(self._muted)
        self._mute.setText("\U0001F507" if self._muted else "\U0001F50A")

    def toggle_full_screen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _set_volume(self, value):
        self._audio.setVolume(value / 100)
        if value and self._muted:
            self.toggle_mute()

    def _seek_moved(self, value):
        duration = self._player.duration()
        if duration > 0:
            self._time.setText(f"{clock(duration * value / SLIDER)} / {clock(duration)}")

    def _seek_released(self):
        duration = self._player.duration()
        if duration > 0:
            self._player.setPosition(int(duration * self._seek.value() / SLIDER))
        self._seeking = False

    def _on_position(self, position):
        duration = self._player.duration()
        if not self._seeking:
            self._time.setText(f"{clock(position)} / {clock(duration)}")
            if duration > 0:
                self._seek.setValue(int(SLIDER * position / duration))

    def _on_duration(self, duration):
        self._seek.setEnabled(duration > 0)
        self._on_position(self._player.position())

    def _on_state(self, state):
        self._play.setText("❚❚" if state == QMediaPlayer.PlayingState else "▶")

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._start_ms and self._player.isSeekable():
            self._player.setPosition(self._start_ms)          # where the page's video had got to
            self._start_ms = 0
        if status in (QMediaPlayer.MediaStatus.LoadingMedia, QMediaPlayer.MediaStatus.BufferingMedia,
                      QMediaPlayer.MediaStatus.StalledMedia):
            self._say(tr("Loading…"))
        elif status != QMediaPlayer.MediaStatus.InvalidMedia:
            self._say("")

    def _on_error(self, _error, message=""):
        self._failed = True
        self._say(tr("This video can't be played here.") + (f"\n{message}" if message else ""))

    def _say(self, text):
        self._status.setText(text)
        self._status.setVisible(bool(text))
        self._status.adjustSize()
        self._status.move((self._stage.width() - self._status.width()) // 2,
                          (self._stage.height() - self._status.height()) // 2)
        self._status.raise_()

    def _save_clicked(self):
        if self._save:
            self._save(self.url)
            self._save_button.setText(tr("Saving…"))
            self._save_button.setEnabled(False)
            QTimer.singleShot(2500, self._saved)

    def _saved(self):
        self._save_button.setText(tr("Save"))
        self._save_button.setEnabled(True)

    # -------------------------------------------------------------- keys --
    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Space:
            self.toggle()
        elif key == Qt.Key_Left:
            self._player.setPosition(max(0, self._player.position() - SEEK_MS))
        elif key == Qt.Key_Right:
            self._player.setPosition(self._player.position() + SEEK_MS)
        elif key == Qt.Key_Up:
            self._volume.setValue(min(100, self._volume.value() + STEP))
        elif key == Qt.Key_Down:
            self._volume.setValue(max(0, self._volume.value() - STEP))
        elif key == Qt.Key_M:
            self.toggle_mute()
        elif key == Qt.Key_F:
            self.toggle_full_screen()
        elif key == Qt.Key_Escape:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        # Stopped before it goes; deleted here on the GUI thread, never left
        # to Python's collector (see the Dailies player's notes).
        self._player.stop()
        super().closeEvent(event)


class Videos(QObject):
    """The open players, one per address: asked for again, the one open is
    brought to the front instead of a second. An address comes from a page,
    so it's looked at first (video_probe.py) - off the GUI thread, since
    that's the network - and the player gets the address it checked."""

    _checked = Signal(object)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.windows = {}
        self._last = {}
        self._checked.connect(self._open)

    def play(self, url, title="", start=0.0, save=None, page_host="", refused=None):
        """Plays `url` once it checks out. `page_host` is the page it came
        from (a page on this PC or the user's network may ask for such
        addresses); refused(why) hears if it doesn't."""
        window = self.windows.get(url)
        if window is not None:
            try:
                window.show()
                window.raise_()
                window.activateWindow()
                return
            except RuntimeError:
                self.windows.pop(url, None)
        if len(self.windows) >= MAX_WINDOWS:
            if refused:
                refused("Close a video window first - " + str(MAX_WINDOWS) + " are open.")
            return
        if time.time() - self._last.get(url, 0) < 2.0:
            return
        self._last[url] = time.time()
        threading.Thread(target=self._check, args=(url, title, start, save, page_host, refused),
                         name="buddy-video-check", daemon=True).start()

    def _check(self, url, title, start, save, page_host, refused):
        try:
            final = video_probe.probe(url, allow_private=video_probe.host_is_private(page_host) if page_host else False)
            self._checked.emit((url, final, title, start, save, None, refused))
        except video_probe.VideoRefused as exc:
            self._checked.emit((url, None, title, start, save, str(exc), refused))
        except Exception as exc:  # noqa: BLE001 - nothing here may take the app down
            self._checked.emit((url, None, title, start, save, f"Buddy couldn't check that video ({exc}).", refused))

    def _open(self, result):
        url, final, title, start, save, why, refused = result
        if final is None:
            if refused:
                refused(why)
            return
        if len(self.windows) >= MAX_WINDOWS or url in self.windows:
            return
        window = VideoWindow(self.host, final, title, start, save)
        self.windows[url] = window
        window.destroyed.connect(lambda _o=None, u=url: self.windows.pop(u, None))
        window.show()

    def close_all(self):
        for window in list(self.windows.values()):
            try:
                window.close()
            except RuntimeError:
                pass
        self.windows.clear()
