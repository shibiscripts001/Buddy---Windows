"""
Where Dailies draws playback: laid over the web page's video stage.

A QQuickWidget, not a QVideoWidget. Qt 6's QVideoWidget hosts a native
window (a QWindowContainer), and Windows fills a new native window white
before Qt draws into it: pressing Play flashed the whole Buddy window white,
then the stage black, before the first frame. A QQuickWidget renders into a
texture that is composited with the web view like any other widget, so it
has no window of its own to flash, and the video is still converted and
scaled on the GPU - far cheaper than streaming JPEG frames to the page.

Transparent where there is no picture, so a cleared surface shows the
page's still underneath rather than a black box. If QtQuick can't load at
all, a QVideoWidget stands in: flashes, but plays.
"""

import os

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import QWidget

QML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_surface.qml")


class VideoSurface(QObject):
    """The widget to place (`widget`), the sink to hand the player
    (`output`), and `drawn(frame)` for every frame the surface receives."""

    drawn = Signal(QVideoFrame)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._root = None
        self.widget, self.output = self._quick(parent) or self._fallback(parent)
        sink = self.output if hasattr(self.output, "videoFrameChanged") else self.output.videoSink()
        sink.videoFrameChanged.connect(self.drawn)
        self.widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.widget.setFocusPolicy(Qt.NoFocus)
        self.widget.hide()

    def _quick(self, parent):
        try:
            from PySide6.QtQuickWidgets import QQuickWidget
            widget = QQuickWidget(parent)
            widget.setResizeMode(QQuickWidget.SizeRootObjectToView)
            # On top of the web view's own texture, and see-through.
            widget.setAttribute(Qt.WA_AlwaysStackOnTop)
            widget.setClearColor(Qt.transparent)
            widget.setSource(QUrl.fromLocalFile(QML))
            root = widget.rootObject()
            output = root.findChild(QObject, "output") if root is not None else None
            sink = output.property("videoSink") if output is not None else None
            if sink is None:
                widget.deleteLater()
                return None
            self._root = root
            return widget, sink
        except Exception:
            return None

    def _fallback(self, parent):
        from PySide6.QtMultimediaWidgets import QVideoWidget
        widget = QVideoWidget(parent)
        widget.setAspectRatioMode(Qt.KeepAspectRatio)
        return widget, widget

    def set_dim(self, dim):
        """Fades the picture back while the next clip loads."""
        if self._root is not None:
            self._root.setProperty("dim", bool(dim))
