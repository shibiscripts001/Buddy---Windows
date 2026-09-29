// Dailies' playback picture (video_surface.py). Transparent wherever there is
// no picture, so the web page's still underneath shows through instead.
import QtQuick
import QtMultimedia

Item {
    property bool dim: false
    opacity: dim ? 0.35 : 1.0

    VideoOutput {
        objectName: "output"
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
        // A clip's end hands the surface an empty frame; cleared, that was a
        // one-frame blink at every cut from one clip to the next.
        endOfStreamPolicy: VideoOutput.KeepLastFrame
    }
}
