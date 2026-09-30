#!/usr/bin/env python3
"""
Animation - the page for animating things in Resolve. Its top row of tabs is
the kind of animation: Previews, then Text+.

Previews shows every motion preset (motion.py, motion_presets.json) playing
in a little viewer, grouped by pack, and puts the chosen one on the clips
selected in Resolve's timeline - stills, video, Text+ and other titles -
through their Fusion comps (motion_resolve.py). The clip's Inspector framing
goes into the comp with it (framing.py), so the move is never cut off at the
frame's edge; re-frame in the Inspector and Update framing folds it in.
Remove takes the preset off and puts the framing back.

Text+ is empty so far: the Text+ tools (font styling, placement, word
layouts, animation presets) are tabs on Subtitles, right after the Subtitle
Conversion that makes the Text+ (see text_plus.py, which that page hosts,
and the engine modules beside it).

It was the Text Animator page, and keeps that tool id so the sidebar
arrangement and saved settings carry over. Its own choices are saved under
"animation_previews".

Protocol:
    to the view    presets, state, toast, alert
    from the view  tab, choose, way, speed, play, refresh, apply, update, remove
"""

import os

from PySide6.QtCore import QTimer

from core.resolve_bridge import ResolveConnectionError
from core.resolve_worker import ResolveWorker
from core.web_page import WebToolPage

from . import motion, motion_resolve

POLL_MS = 1500
SETTINGS_ID = "animation_previews"
DEFAULTS = {"tab": "previews", "chosen": "pop", "way": "both", "speed": 1.0, "play": "all"}
TABS = ("previews", "textplus")
PLAY = ("all", "hover")

_KIND_WORDS = {
    motion_resolve.IMAGE: ("image", "images"),
    motion_resolve.VIDEO: ("video clip", "video clips"),
    motion_resolve.TITLE: ("title", "titles"),
    motion_resolve.OTHER: ("other clip", "other clips"),
    motion_resolve.NOT_VIDEO: ("audio or subtitle clip", "audio or subtitle clips"),
}


def _count(n, kind):
    one, many = _KIND_WORDS[kind]
    return f"{n} {one if n == 1 else many}"


class AnimationPage(WebToolPage):
    tool_id = "text_animator"
    display_name = "Animation"
    category = "Editing Tools"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.settings = self.host.tool_settings(SETTINGS_ID, DEFAULTS)
        self.presets = motion.load()
        self._by_id = {p["id"]: p for p in self.presets}
        if self.settings.get("chosen") not in self._by_id:
            self.settings["chosen"] = self.presets[0]["id"]
        self.selection = None       # motion_resolve.summary(), or None before the first read
        self.problem = ""
        self.working = ""           # "apply" / "remove" while a job runs
        self._worker = ResolveWorker(self)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_selection)
        self._poll.start()

    def web_ready(self):
        self.emit("presets", {
            "packs": motion.PACKS,
            "kinds": motion.KINDS,
            "presets": [motion.view(p) for p in self.presets],
        })
        self._push_state()

    def on_shown(self):
        self._read_selection()

    def on_connection_changed(self, connected):
        if not connected:
            self.selection = None
        self._push_state()
        if connected:
            self._read_selection()

    def on_app_quitting(self):
        self._poll.stop()

    # ------------------------------------------------------------ Resolve --

    def _controller(self, connect=False):
        if connect:
            try:
                return self.host.ensure_connected()
            except ResolveConnectionError as exc:
                self.problem = str(exc)
                return None
        return self.host.controller if getattr(self.host, "connected", False) else None

    def _poll_selection(self):
        if self.isVisible() and self.settings.get("tab") == "previews":
            self._read_selection()

    def _read_selection(self):
        controller = self._controller()
        if controller is None or self._worker.busy():
            return
        self._worker.start(lambda: motion_resolve.summary(controller), self._on_selection)

    def _on_selection(self, result, error):
        if isinstance(error, motion_resolve.SelectionUnavailable):
            result, self.problem = None, str(error)
        elif error is not None:
            result, self.problem = None, f"Couldn't read the selection: {error}"
        else:
            self.problem = ""
        if result != self.selection or error is not None:
            self.selection = result
            self._push_state()

    # --------------------------------------------------------------- view --

    def _push_state(self):
        self.emit("state", {
            "tab": self.settings.get("tab"),
            "chosen": self.settings.get("chosen"),
            "way": self.settings.get("way"),
            "speed": self.settings.get("speed"),
            "play": self.settings.get("play"),
            "connected": self._controller() is not None,
            "selection": self._selection_view(),
            "shape": motion_resolve.preview_shape(self.selection["counts"] if self.selection else {}),
            "problem": self.problem,
            "working": self.working,
        })

    def _selection_view(self):
        s = self.selection
        if not s:
            return None
        counts = s["counts"]
        can = sum(n for k, n in counts.items() if k in motion_resolve.ANIMATABLE)
        return {"timeline": s["timeline"], "total": s["total"], "animatable": can,
                "animated": s["animated"], "reframed": s.get("reframed", 0),
                "parts": [{"text": _count(counts[k], k), "kind": k} for k in _KIND_WORDS if counts.get(k)]}

    def _save(self, key, value):
        self.settings[key] = value
        self.settings.save()
        self._push_state()

    # ------------------------------------------------------------ actions --

    def on_tab(self, payload):
        tab = (payload or {}).get("tab")
        if tab in TABS:
            self._save("tab", tab)
            if tab == "previews":
                self._read_selection()

    def on_choose(self, payload):
        pid = (payload or {}).get("id")
        if pid in self._by_id:
            self._save("chosen", pid)

    def on_way(self, payload):
        way = (payload or {}).get("way")
        if way in motion.WAYS:
            self._save("way", way)

    def on_speed(self, payload):
        try:
            speed = float((payload or {}).get("speed"))
        except (TypeError, ValueError):
            return
        if speed in motion.SPEEDS:
            self._save("speed", speed)

    def on_play(self, payload):
        play = (payload or {}).get("play")
        if play in PLAY:
            self._save("play", play)

    def on_refresh(self, _payload=None):
        if self._controller(connect=True) is None:
            self.emit("alert", {"title": "Can't reach Resolve", "text": self.problem})
            return self._push_state()
        self._read_selection()

    def _start(self, what, job, done):
        controller = self._controller(connect=True)
        if controller is None:
            self.emit("alert", {"title": "Can't reach Resolve", "text": self.problem})
            return self._push_state()
        if not self._worker.wait_idle(3) or not self._worker.start(lambda: job(controller), done):
            self.emit("toast", {"text": "Resolve is busy – try again in a moment."})
            return
        self.working = what
        self._push_state()

    def on_apply(self, _payload=None):
        preset = self._by_id[self.settings.get("chosen")]
        way, speed = self.settings.get("way"), float(self.settings.get("speed") or 1)

        def plan_for(frames, fps, at):
            return motion.plan(preset, frames, fps, way=way, speed=speed, at=at)

        options = {"way": way, "speed": speed}
        self._start("apply", lambda c: motion_resolve.run_apply(c, preset, plan_for, options=options),
                    lambda result, error: self._done_apply(preset, result, error))

    def _done_apply(self, preset, result, error):
        self.working = ""
        if error is not None:
            self._push_state()
            return self.emit("alert", {"title": "Couldn't apply the preset", "text": str(error)})
        applied, failed = result["applied"], result["failed"]
        skipped = sum(result["skipped"].values())
        if not applied and not failed:
            text = ("Only audio or subtitle clips are selected – they can't take a motion preset."
                    if skipped else "Select the clips to animate in Resolve's timeline first.")
            self.emit("toast", {"text": text})
        elif skipped:
            self.emit("toast", {"text": f"{preset['label']} is on {applied} of {applied + skipped + len(failed)} "
                                        "clips – audio and subtitle clips can't move."})
        else:
            self.emit("toast", {"text": f"{preset['label']} is on 1 clip." if applied == 1
                                else f"{preset['label']} is on {applied} clips."})
        if failed:
            self.emit("alert", {"title": "Some clips weren't animated",
                                "text": "\n".join(f"{name}: {why}" for name, why in failed)})
        self._read_after()

    def on_update(self, _payload=None):
        def plan_for_preset(preset_id, options):
            preset = self._by_id.get(preset_id)
            if preset is None:
                return None
            way, speed = options.get("way") or "both", float(options.get("speed") or 1)
            return lambda frames, fps, at: motion.plan(preset, frames, fps, way=way, speed=speed, at=at)

        self._start("update", lambda c: motion_resolve.run_update(c, plan_for_preset), self._done_update)

    def _done_update(self, result, error):
        self.working = ""
        if error is not None:
            self._push_state()
            return self.emit("alert", {"title": "Couldn't update the framing", "text": str(error)})
        updated, failed = result["updated"], result["failed"]
        if not updated and not failed:
            text = "None of the selected clips has a Buddy preset."
        else:
            text = "Updated the framing on 1 clip." if updated == 1 else f"Updated the framing on {updated} clips."
        self.emit("toast", {"text": text})
        if failed:
            self.emit("alert", {"title": "Some clips weren't updated",
                                "text": "\n".join(f"{name}: {why}" for name, why in failed)})
        self._read_after()

    def on_remove(self, _payload=None):
        self._start("remove", motion_resolve.run_remove, self._done_remove)

    def _done_remove(self, result, error):
        self.working = ""
        if error is not None:
            self._push_state()
            return self.emit("alert", {"title": "Couldn't remove the preset", "text": str(error)})
        removed, failed = result["removed"], result["failed"]
        if not removed:
            text = "None of the selected clips has a Buddy preset."
        else:
            text = "Took the preset off 1 clip." if removed == 1 else f"Took the preset off {removed} clips."
        self.emit("toast", {"text": text})
        if failed:
            self.emit("alert", {"title": "Some clips kept their preset",
                                "text": "\n".join(f"{name}: {why}" for name, why in failed)})
        self._read_after()

    def _read_after(self):
        self._push_state()
        QTimer.singleShot(0, self._read_selection)
