// Dailies' playback picture (video_surface.py). Transparent wherever there is
// no picture, so the web page's still underneath shows through instead.
import QtQuick
import QtMultimedia

Item {
    property bool dim: false
    opacity: dim ? 0.35 : 1.0
    // The widget is only the part of the stage that's in view; the picture
    // keeps the whole stage's size and is shifted, so scrolling crops it
    // instead of squeezing it (video_surface.py crop()). 0 = the widget's
    // own size. Plain values, not bindings to width/height: a write from
    // Python doesn't break a binding, and the next resize undid it.
    property real stageWidth: 0
    property real stageHeight: 0
    property real cropX: 0
    property real cropY: 0
    clip: true

    VideoOutput {
        objectName: "output"
        x: -parent.cropX
        y: -parent.cropY
        width: parent.stageWidth > 0 ? parent.stageWidth : parent.width
        height: parent.stageHeight > 0 ? parent.stageHeight : parent.height
        fillMode: VideoOutput.PreserveAspectFit
        // A clip's end hands the surface an empty frame; cleared, that was a
        // one-frame blink at every cut from one clip to the next.
        endOfStreamPolicy: VideoOutput.KeepLastFrame
    }
}
