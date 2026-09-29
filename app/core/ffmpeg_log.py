"""
Silences the FFmpeg that Qt Multimedia plays media with.

Qt's FFmpeg backend leaves FFmpeg's own logging at its default level, and
FFmpeg's default logger writes to stderr: every file a preview opens prints
its whole stream dump ("Input #0, mov,mp4,..." and a line per stream - nine
of them for a Sony MXF), plus warnings about perfectly playable camera files
("infe version < 2 is not implemented", "Missing key frame while searching
for timestamp"). In a console that is a screenful per clip, and none of it is
anything a user could act on - a file that really can't play still fails
through QMediaPlayer's own error.

The level is a process-wide setting inside the avutil library PySide6
ships, so setting it once through ctypes covers every player made after.
Qt only touches it when QT_FFMPEG_DEBUG is set, which is left alone here so
the logging can still be had when debugging playback.
"""

import ctypes
import glob
import os

AV_LOG_QUIET = -8

# Where PySide6's wheels keep avutil: beside the Qt DLLs on Windows, in
# Qt/lib on macOS and Linux.
_PATTERNS = ("avutil-*.dll", os.path.join("Qt", "lib", "libavutil*.dylib"),
             os.path.join("Qt", "lib", "libavutil.so*"))


def avutil_candidates(pyside_dir):
    """The avutil libraries in a PySide6 folder, newest version first."""
    found = []
    for pattern in _PATTERNS:
        found.extend(glob.glob(os.path.join(pyside_dir, pattern)))
    return sorted(set(found), reverse=True)


def silence(environ=os.environ):
    """Turns FFmpeg's logging off. Returns the library it was set in, or None
    (no PySide6 FFmpeg to quiet, or QT_FFMPEG_DEBUG asked for the logging).
    Never raises: a noisy console is no reason to stop Buddy starting."""
    if environ.get("QT_FFMPEG_DEBUG"):
        return None
    try:
        import PySide6
        for path in avutil_candidates(os.path.dirname(PySide6.__file__)):
            try:
                library = ctypes.CDLL(path)
                library.av_log_set_level(ctypes.c_int(AV_LOG_QUIET))
                return path
            except (OSError, AttributeError):
                continue
    except Exception:
        pass
    return None
