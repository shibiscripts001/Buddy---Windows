#!/usr/bin/env python3
"""
Buddy - single-window shell hosting a set of DaVinci Resolve tools, each
ported from a standalone script. See registry.py for which tools are
real pages vs. still placeholders, and pages/base.py for the ToolPage
contract new pages implement.

Run directly (`python main.py`) for development. Deployment to Resolve's
Workspace > Scripts menu is a single launcher stub + zip (see Buddy.py,
build_buddy_zip.py).

Single-instance guard, the system tray, and "Start Buddy automatically when
Resolve starts" (see core/single_instance.py, core/shell_window.py's own
tray handling, and core/startup_manager.py) exist because Time Tracker's
port made Buddy a genuine background app, not just a one-shot action tool -
see pages/time_tracker/page.py's module docstring for the full story.
"""

import os
import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

from core import crash_log, ffmpeg_log, gpu_adapter, gui_gc, startup_manager, web_flags
from core.i18n import tr
from core.settings_store import BUDDY_DIR
from core.shell_window import ShellWindow
from core.single_instance import notify_existing_instance, SingleInstanceServer
from registry import REGISTRY


def main(start_hidden=False):
    # First, so a native crash anywhere after this (QtWebEngine inside
    # Resolve's script host leaves no traceback) says what Python was doing.
    crash_log.enable(os.path.join(BUDDY_DIR, "crash.log"),
                     version=f"from {os.path.dirname(os.path.abspath(__file__))}")
    crash_log.enable_trail(os.path.join(BUDDY_DIR, "crash_trail.log"))

    # Keeps an already-enabled watcher's deployed copy in sync with
    # whatever version of Buddy is currently installed - cheap, and a
    # no-op unless the setting is actually on.
    startup_manager.sync_if_enabled()

    # Web pages draw through ANGLE's Direct3D 11 on 12: plain Direct3D 11
    # crashes inside Buddy's process (core/web_flags.py) - and with two
    # GPUs, on the low-power one, away from Resolve's card (core/gpu_adapter.py).
    crash_log.trail("web", f"{web_flags.apply(os.environ)}; {gpu_adapter.apply(os.environ)}")
    # Web tool pages (core/web_page.py) share GPU contexts with each other;
    # QtWebEngine needs this set before the QApplication exists.
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    # Before any preview opens a file: Qt's FFmpeg otherwise prints every
    # clip's stream dump and warnings to the console (core/ffmpeg_log.py).
    ffmpeg_log.silence()
    # Buddy's own taskbar identity, before any window exists: started by
    # pythonw.exe (the updater's relaunch, the login watcher) the taskbar
    # otherwise groups it under Python and shows Python's icon, not Buddy's.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Buddy.ResolveTools")
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    # Cyclic garbage is freed on this thread only, never inside a worker
    # where a web view's destruction crashes QtWebEngine (core/gui_gc.py).
    gui_gc.install(app)
    # The tray icon is the app's actual "still running" signal once the
    # window is closed/hidden - without this, Qt would quit the whole app
    # the moment the (now only) top-level window goes away, defeating the
    # point of minimizing to tray instead of closing.
    app.setQuitOnLastWindowClosed(False)

    # Relaunching from Resolve's Scripts menu while an instance is already
    # running (e.g. minimized to tray, easy to forget about) must NOT start
    # a second Buddy polling Resolve independently for Time Tracker - that
    # would silently double-count or conflict. If something answers here,
    # it's already been asked to raise itself, so this process is done.
    if notify_existing_instance(start_hidden):
        return

    window = ShellWindow(app, REGISTRY)
    window._single_instance_server = SingleInstanceServer(window)

    # start_hidden (set by core/resolve_watcher.py's "--start-hidden" - see
    # Buddy.py) means this launch was the auto-start, not the user actually
    # asking to see the app right now - it should sit quietly in the tray
    # instead of popping up its full window unprompted. Falls back to a
    # normal visible launch if no tray icon exists at all (e.g. a rare
    # environment where the system tray is unavailable) - otherwise Buddy
    # would be completely unreachable with no window and no tray icon.
    if start_hidden and window.tray_icon is not None:
        window._update_tray_show_action()
        window.tray_icon.showMessage(
            "Buddy",
            tr("Running in the background – Time Tracker will start tracking once you open a "
               "project. Click the tray icon any time to view it."),
            window.tray_icon.MessageIcon.Information, 4000,
        )
    else:
        window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main(start_hidden="--start-hidden" in sys.argv[1:])
