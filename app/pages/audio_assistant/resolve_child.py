#!/usr/bin/env python3
"""
Audio Assistant's Resolve calls that must not run inside Buddy - run as a
child process by the page, one at a time:

    python resolve_child.py RESULT.json   < {"do": "seek", "timecode": "01:00:15:00"}

Why a process of its own: Timeline.SetCurrentTimecode takes ~500 ms inside
Resolve (measured on 21.1 - it waits for the viewer) and holds Python's GIL
the whole time, so made from Buddy, even on a worker thread, it froze every
window for half a second per click and a drag on the ruler for as long as
it lasted. A child holds only its own GIL. (While it runs, the page makes
no Resolve call of its own: that would wait for Resolve, GIL held.)

Writes {"ok": true, "result": ...} or {"ok": false, "error": "..."} to
RESULT.json. Exits 0 on success, 2 if Resolve refused, 1 if it couldn't be
reached. Only the standard library and Buddy's Qt-free modules are imported.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def run(command, controller):
    from pages.audio_assistant import resolve_ext
    if command.get("do") == "seek":
        resolve_ext.seek(controller, command["timecode"])
        return None
    raise ValueError(f"unknown command {command.get('do')!r}")


def main(result_path, command, connect=None):
    def write(data):
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    try:
        if connect is None:
            from core.resolve_bridge import ResolveController
            connect = ResolveController
        controller = connect()
    except Exception as exc:  # noqa: BLE001 - reported to the page
        write({"ok": False, "error": f"Couldn't reach Resolve: {exc}"})
        return 1
    try:
        write({"ok": True, "result": run(command, controller)})
        return 0
    except Exception as exc:  # noqa: BLE001
        write({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)
    try:
        cmd = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        sys.exit(1)
    sys.exit(main(sys.argv[1], cmd))
