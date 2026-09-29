#!/usr/bin/env python3
"""
Transcribe's call into Resolve's own transcription, in a process of its
own - run by jobs.TranscribeJob for the "DaVinci Resolve" model:

    python resolve_child.py RESULT.json   < {"timeline": uid, "language": "en", "fresh": true}

Why a process of its own: TranscribeAudio blocks until Resolve has
transcribed the whole timeline (~10 s for 2 minutes, measured on Studio
21.1) and holds Python's GIL the whole time, so made from Buddy, even on a
worker thread, it would freeze every window for as long. A child holds only
its own GIL, and Resolve goes on answering Buddy's other calls meanwhile
(measured: 0.04 s at worst while it transcribed).

"timeline" must still be the current timeline's unique id - one switched to
since is refused rather than transcribed. "language" ("" for Resolve's own
choice) goes in the project's transcriptionLanguage setting for the call and
is put back after. "fresh" clears the timeline's old transcription first:
without that Resolve hands back the old one, however the timeline changed.

Writes {"ok": true, "result": {"transcription", "fps", "start_frame"}} or
{"ok": false, "error": "..."} to RESULT.json. Exits 0 on success, 2 if
Resolve refused, 1 if it couldn't be reached. Only the standard library and
Buddy's Qt-free modules are imported.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class Refused(Exception):
    pass


def run(command, controller):
    project = controller.current_project()
    timeline = project.GetCurrentTimeline() if project is not None else None
    if timeline is None:
        raise Refused("Open a timeline in Resolve first.")
    if command.get("timeline") and timeline.GetUniqueId() != command["timeline"]:
        raise Refused("The timeline changed in Resolve before it could be transcribed - start again.")
    item = timeline.GetMediaPoolItem()
    if item is None:
        raise Refused("Resolve didn't give Buddy the timeline to transcribe.")
    from pages.transcribe.resolve_transcript import resolve_language
    language = resolve_language(str(command.get("language") or ""))
    before = project.GetSetting("transcriptionLanguage")
    if language and language != before and not project.SetSetting("transcriptionLanguage", language):
        raise Refused("Resolve's transcription doesn't take that language - choose Auto-detect or another.")
    try:
        if command.get("fresh"):
            item.ClearTranscription(True)
        if not item.TranscribeAudio(True, True):
            raise Refused("Resolve couldn't transcribe the timeline.")
        got = item.GetTranscription(True)
    finally:
        if language and language != before and before:
            project.SetSetting("transcriptionLanguage", before)
    if not isinstance(got, dict) or not got.get("segments"):
        raise Refused("Resolve found no speech in the timeline's audio.")
    return {"transcription": got, "fps": float(timeline.GetSetting("timelineFrameRate") or 24),
            "start_frame": int(timeline.GetStartFrame())}


def main(result_path, command, connect=None):
    def write(data):
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

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
