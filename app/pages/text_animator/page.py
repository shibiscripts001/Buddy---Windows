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

Editor opens any preset as keys on a spline editor (motion.keys_of) with a
viewer that plays exactly what's drawn; the edit can go straight on the
selected clips, or be saved as a preset of the person's own
(motion.from_keys) - those are the Saved pack, at the foot of Previews.
Favorites is every preset that's been hearted, with Previews' apply card.

Text+ is empty so far: the Text+ tools (font styling, placement, word
layouts, animation presets) are tabs on Subtitles, right after the Subtitle
Conversion that makes the Text+ (see text_plus.py, which that page hosts,
and the engine modules beside it).

It was the Text Animator page, and keeps that tool id so the sidebar
arrangement and saved settings carry over. Its own choices are saved under
"animation_previews".

Protocol:
    to the view    presets, state, keys, saved, toast, alert
    from the view  tab, choose, way, speed, play, refresh, apply, update, remove,
                   keys, favorite, save, delete, apply_draft, color
"""

import os
import re
import uuid

from PySide6.QtCore import QTimer

from core.resolve_bridge import ResolveConnectionError
from core.resolve_worker import ResolveWorker
from core.web_page import WebToolPage

from . import motion, motion_resolve

POLL_MS = 1500
SETTINGS_ID = "animation_previews"
DEFAULTS = {"tab": "previews", "chosen": "pop", "way": "both", "speed": 1.0, "play": "all",
            "favorites": [], "saved": [], "colors": {}}
TABS = ("previews", "editor", "favorites", "textplus")
APPLY_TABS = ("previews", "editor", "favorites")     # the tabs that read the selection
MAX_SAVED = 200
DRAFT = "draft"                     # the Editor's unsaved edit, put on clips as it is
PLAY = ("all", "hover")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")

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


def apply_summary(preset, result, error):
    """(text, ok) for an Apply from Command Center's hot key."""
    if error is not None:
        return f"Couldn't apply {preset['label']}: {error}", False
    applied, failed = result["applied"], result["failed"]
    skipped = sum(result["skipped"].values())
    if not applied and not failed:
        return ("Only audio or subtitle clips are selected – they can't take a motion preset." if skipped
                else "Select the clips to animate in Resolve's timeline first."), False
    if failed:   # whole sentences, so each is translated as one
        return f"{preset['label']} is on {applied} of {applied + len(failed)} clips – the rest couldn't take it.", False
    return (f"{preset['label']} is on 1 clip." if applied == 1 else f"{preset['label']} is on {applied} clips."), True


class AnimationPage(WebToolPage):
    tool_id = "text_animator"
    display_name = "Animation"
    category = "Editing Tools"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.settings = self.host.tool_settings(SETTINGS_ID, DEFAULTS)
        self.builtin = motion.load()
        self._keys = {}             # preset id -> motion.keys_of, fitted the first time the Editor asks
        self._load_saved()
        if self.settings.get("chosen") not in self._by_id:
            self.settings["chosen"] = self.presets[0]["id"]
        self.selection = None       # motion_resolve.summary(), or None before the first read
        self.problem = ""
        self.working = ""           # "apply" / "remove" while a job runs
        self._queued = None         # (job, done) waiting for the worker to be free
        self._worker = ResolveWorker(self)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_selection)
        self._poll.start()

    def _load_saved(self):
        """The person's own presets (settings "saved") after the built-in ones.
        One that won't read is dropped rather than stopping the page."""
        self.saved = []
        for entry in self.settings.get("saved") or []:
            try:
                self.saved.append(motion.from_keys(entry["id"], entry["label"], entry["kind"],
                                                   entry["dur"], entry["keys"]))
            except (KeyError, TypeError, ValueError):
                continue
        self.presets = self.builtin + self.saved
        self._by_id = {p["id"]: p for p in self.presets}

    def web_ready(self):
        self._send_presets()
        self._push_state()

    def _send_presets(self):
        self.emit("presets", {
            "packs": motion.PACKS + ([motion.SAVED] if self.saved else []),
            "kinds": motion.KINDS,
            "presets": [motion.view(p) for p in self.presets],
        })

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
        if self.isVisible() and self.settings.get("tab") in APPLY_TABS:
            self._read_selection()

    def _read_selection(self):
        controller = self._controller()
        if controller is None or self._worker.busy() or self._queued or self.working:
            return
        self._run(lambda: motion_resolve.summary(controller), self._on_selection)

    def _run(self, job, done):
        """Starts a worker job; when it's over, whatever is queued behind it."""
        def finished(result, error):
            try:
                done(result, error)
            finally:
                self._next()
        return self._worker.start(job, finished)

    def _next(self):
        if self._queued is None or self._worker.busy():
            return
        job, done = self._queued
        self._queued = None
        if not self._run(job, done):
            self._queued = (job, done)

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
            "favorites": [i for i in self.settings.get("favorites") or [] if i in self._by_id],
            "colors": self._colors(),
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
        return {"timeline": s["timeline"], "fps": s.get("fps", 24.0), "total": s["total"], "animatable": can,
                "animated": s["animated"], "reframed": s.get("reframed", 0), "clip": s.get("clip"),
                "parts": [{"text": _count(counts[k], k), "kind": k} for k in _KIND_WORDS if counts.get(k)]}

    def _colors(self):
        """The colours picked for the Editor's channels, by channel."""
        colors = self.settings.get("colors") or {}
        return {c: v.lower() for c, v in colors.items()
                if c in motion.CHANNELS and isinstance(v, str) and _HEX.fullmatch(v)}

    def _save(self, key, value):
        self.settings[key] = value
        self.settings.save()
        self._push_state()

    # ------------------------------------------------------------ actions --

    def on_tab(self, payload):
        tab = (payload or {}).get("tab")
        if tab in TABS:
            self._save("tab", tab)
            if tab in APPLY_TABS:
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
        """Runs an Apply / Update / Remove on the worker. While it's busy (most
        often the selection poll) the job waits its turn rather than holding
        the UI thread up for it."""
        if self.working:
            return
        controller = self._controller(connect=True)
        if controller is None:
            self.emit("alert", {"title": "Can't reach Resolve", "text": self.problem})
            return self._push_state()
        self.working = what
        self._push_state()
        task = (lambda: job(controller), done)
        if self._worker.busy():
            self._queued = task
            if self._worker.running_for() > 2:
                self.emit("toast", {"text": "Resolve is busy – this starts as soon as it's free."})
        elif not self._run(*task):
            self._queued = task

    def on_apply(self, _payload=None):
        self._apply(self._by_id[self.settings.get("chosen")])

    # ------------------------------------------- Command Center's hot keys --

    def hotkey_presets(self):
        """[(id, label)] for Command Center's Animation action: favourites
        first, then the rest in Previews' order."""
        favorites = [i for i in self.settings.get("favorites") or [] if i in self._by_id]
        rest = [p["id"] for p in self.presets if p["id"] not in favorites]
        return [(i, self._by_id[i]["label"]) for i in favorites + rest]

    def apply_for_hotkey(self, preset_id, way, done):
        """Command Center: puts preset_id on the selected clips, as Apply
        does, through this page's own worker (so its jobs never overlap).
        done(text, ok) says how it went - this tab may not be on screen."""
        preset = self._by_id.get(preset_id)
        if preset is None:
            return done("That Animation preset is gone – pick another in Command Center.", False)
        if self.working:
            return done("Animation is busy with another change – try again in a moment.", False)
        if self._controller(connect=True) is None:
            return done(self.problem or "Can't reach Resolve.", False)
        way = way if way in ("both", "in", "out") else (self.settings.get("way") or "both")
        speed = float(self.settings.get("speed") or 1)

        def plan_for(frames, fps, at):
            return motion.plan(preset, frames, fps, way=way, speed=speed, at=at)

        options = {"way": way, "speed": speed}
        if preset.get("keys"):
            options["preset"] = motion.stored(preset)

        def finished(result, error):
            self.working = ""
            self._push_state()
            if error is None:
                self._read_after()
            done(*apply_summary(preset, result, error))

        self._start("apply", lambda c: motion_resolve.run_apply(c, preset, plan_for, options=options), finished)

    def _apply(self, preset):
        way, speed = self.settings.get("way"), float(self.settings.get("speed") or 1)

        def plan_for(frames, fps, at):
            return motion.plan(preset, frames, fps, way=way, speed=speed, at=at)

        options = {"way": way, "speed": speed}
        if preset.get("keys"):
            # Drawn in the Editor: the keys go with the clip, so Update framing
            # still has them once the preset is changed or deleted.
            options["preset"] = motion.stored(preset)
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
            if preset is None and options.get("preset"):
                try:
                    kept = options["preset"]
                    preset = motion.from_keys(preset_id, kept["label"], kept["kind"], kept["dur"], kept["keys"])
                except (KeyError, TypeError, ValueError):
                    preset = None
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

    # ------------------------------------------------------------- Editor --

    def on_keys(self, payload):
        """The Editor opens a preset: its keys (fitted once, then kept)."""
        pid = (payload or {}).get("id")
        preset = self._by_id.get(pid)
        if preset is None:
            return
        if pid not in self._keys:
            self._keys[pid] = motion.keys_of(preset)
        self.emit("keys", {"id": pid, "keys": self._keys[pid]})

    def _draft(self, payload, pid=DRAFT):
        payload = payload or {}
        return motion.from_keys(pid, str(payload.get("label") or "").strip() or "Untitled",
                                payload.get("kind"), payload.get("dur"), payload.get("keys"))

    def on_apply_draft(self, payload):
        """The Editor's edit on the selected clips, saved or not."""
        try:
            preset = self._draft(payload)
        except (TypeError, ValueError, AttributeError):
            return self.emit("toast", {"text": "That edit couldn't be read."})
        self._apply(preset)

    def on_save(self, payload):
        """Saves the Editor's edit: over a preset of the person's own ("id"),
        or as a new one. The new or updated preset is chosen."""
        payload = payload or {}
        pid = payload.get("id")
        saved = list(self.settings.get("saved") or [])
        if pid not in {e.get("id") for e in saved}:
            if len(saved) >= MAX_SAVED:
                return self.emit("toast", {"text": f"There are {MAX_SAVED} saved presets already – delete one first."})
            pid = f"saved-{uuid.uuid4().hex[:10]}"
        try:
            preset = self._draft(payload, pid)
        except (TypeError, ValueError, AttributeError):
            return self.emit("toast", {"text": "That edit couldn't be read."})
        entry = motion.stored(preset)
        if any(e.get("id") == pid for e in saved):
            saved = [entry if e.get("id") == pid else e for e in saved]
        else:
            saved.append(entry)
        self.settings["saved"] = saved
        self.settings["chosen"] = pid
        self.settings.save()
        self._keys.pop(pid, None)
        self._load_saved()
        self._send_presets()
        self._push_state()
        self.emit("saved", {"id": pid})
        self.emit("toast", {"text": f"Saved {preset['label']} to your presets."})

    def on_delete(self, payload):
        """Deletes a preset of the person's own (built-in ones stay)."""
        pid = (payload or {}).get("id")
        saved = list(self.settings.get("saved") or [])
        gone = next((e for e in saved if e.get("id") == pid), None)
        if gone is None:
            return
        self.settings["saved"] = [e for e in saved if e.get("id") != pid]
        self.settings["favorites"] = [i for i in self.settings.get("favorites") or [] if i != pid]
        self._keys.pop(pid, None)
        self._load_saved()
        if self.settings.get("chosen") not in self._by_id:
            self.settings["chosen"] = self.presets[0]["id"]
        self.settings.save()
        self._send_presets()
        self._push_state()
        self.emit("toast", {"text": f"Deleted {gone.get('label') or 'the preset'}."})

    def on_favorite(self, payload):
        """Hearts a preset (on) or takes the heart off."""
        payload = payload or {}
        pid = payload.get("id")
        if pid not in self._by_id:
            return
        favorites = [i for i in self.settings.get("favorites") or [] if i != pid]
        if payload.get("on"):
            favorites.append(pid)
        self._save("favorites", favorites)

    def on_color(self, payload):
        """An Editor channel's colour (#rrggbb), or none: back to its own."""
        payload = payload or {}
        channel, color = payload.get("channel"), payload.get("color")
        if channel not in motion.CHANNELS or not (color is None or isinstance(color, str) and _HEX.fullmatch(color)):
            return
        colors = self._colors()
        if color:
            colors[channel] = color.lower()
        else:
            colors.pop(channel, None)
        self._save("colors", colors)
