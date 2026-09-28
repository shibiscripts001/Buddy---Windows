#!/usr/bin/env python3
"""
Audio Assistant - an easier alternative to Resolve's Fairlight page, for a clip's
volume, fades, a volume line to keyframe, noise reduction and voice clean-up. A web
page (core/web_page.py): the view is web/index.html + timeline.js; every Resolve call
is resolve_ext.py, the waveforms and loudness are peaks.py, and what a control asks of
Resolve is levels.py.

Its first tab, Timeline, mirrors the open timeline's audio tracks the way Resolve draws
them: each clip where it sits, with its file's waveform in grey and - over it, in blue -
the same wave through the clip's volume and fades. Click a clip (or select it in
Resolve: Buddy can read Resolve's selection but not set it) and the panel under the
tracks changes it:

    Levels     clip volume (set to, or change by, for several), pan, fades - also by
               dragging the selected clip's volume line and fade handles on the canvas
    Loudness   integrated LUFS, measured by Buddy from the files (BS.1770, peaks.py),
               and Match: each clip's volume set by those figures to hit a target (not
               Resolve's NormalizeAudioLevel, which ignores its target - see levels.py)
    Clean-up   Voice Isolation and the Dialogue Leveler, where Resolve has them for the
               clip (they read None on some media, and only work on the active timeline)
    Cuts       an audio crossfade on each cut between selected clips

Every change but a crossfade can be undone (Undo puts back what the clips had). Buddy
can't remove a transition (DeleteClips refuses one), so the view asks first.

What Resolve 21.1's scripting can reach (tried on a duplicate timeline): a clip's
volume, pan and pitch (SetProperties), fades in frames (SetFades), audio transitions
(AddTransition, category "audio"), loudness normalisation to a target
(Timeline.NormalizeAudioLevel - on the active timeline only: on another it returns True
and changes nothing), a track's Voice Isolation, and a clip's Voice Isolation and
Dialogue Leveler - on the active timeline, and not for every clip: a camera MXF's read
None and refused, an MP4's and MP3's worked. Not reachable: volume keyframes and
Fairlight FX (de-esser, EQ, noise reduction), so those would be Buddy's own processing,
baked into a new audio file - and a clip's keyframed volume can't be drawn: the blue
wave shows its flat volume only.

Reading runs on a ResolveWorker (Resolve holds calls while the timeline plays): a live
read of the playhead, Resolve's selection and the selected clips' levels every LIVE_MS,
and a full read of the tracks every few seconds - more seldom on a big timeline. The
writes that are quick (SetProperties, SetFades, AddTransition: 0-2 ms, measured) run
there too. Moving the playhead runs in a child process (resolve_child.py):
SetCurrentTimecode holds the GIL for ~500 ms, which froze all of Buddy on every click of
the ruler. While the child runs, Buddy makes no Resolve call of its own: Resolve answers
nothing else until it's done, and Buddy's call would wait for it holding the GIL - which
froze Buddy for all 15 s of a NormalizeAudioLevel when it was tried there.

Protocol:
    to the view    state, timeline, levels, playhead, selection, peaks, peaks_progress,
                   options, undo, toast, alert
    from the view  refresh, select, seek, set_levels, match, crossfade, undo
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
from PySide6.QtCore import QTimer

from core.resolve_bridge import ResolveConnectionError
from core.resolve_worker import ResolveWorker
from core.web_page import WebToolPage

from pages.stills_exporter.resolve_ext import frames_to_timecode

from . import levels as mixer
from . import resolve_ext
from .peaks import FLOOR_DB, LOUD_BLOCK_S, PEAK_RATE, PeakLoader

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolve_child.py")
CHILD_POLL_MS = 40
# Normalising a long clip takes Resolve a while; a child still going after this is stopped.
CHILD_TIMEOUT_S = 180
# After a seek, Buddy's playhead stands this long before a read of Resolve's may move it:
# a read made while the seek was on its way still has the old frame.
SEEK_SETTLE_S = 1.0
LIVE_MS = 500
# A full read at most this often, and never more than 1/FULL_SHARE of the time.
FULL_EVERY_S = 3.0
FULL_SHARE = 10
# Resolve holds calls while the timeline plays: a read still waiting after this long is said.
BUSY_AFTER_S = 4
# A write waits this long for a read in progress before saying Resolve is busy.
WRITE_WAIT_S = 1.5
UNDO_LIMIT = 30
BUSY_TITLE = "Resolve isn't answering"
BUSY_TEXT = "Resolve is holding Buddy's requests – usually because the timeline is playing. Stop playback and try again."


class AudioAssistantPage(WebToolPage):
    tool_id = "audio_assistant"
    display_name = "Audio Assistant"
    category = "Editing Tools"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.timeline = None         # resolve_ext.read_timeline's data
        self.items = {}              # clip id -> Resolve TimelineItem, from the last full read
        self.selected = []           # clip ids selected in Buddy
        self.from_resolve = False    # ... because Resolve selected them
        self.resolve_selection = None
        self.playhead = None
        self.problem = ""
        self.busy = False
        self.undo_stack = []         # [{"label", "timeline", "before"}], newest last
        self._media_cache = {}
        self._peaks = {}             # file key -> (peaks, loudness) bytes | None (no waveform)
        self._peak_errors = {}
        self._full_due = 0.0         # time.monotonic() the next full read is due
        self._full_took = 0.0
        self._seek = None            # the latest ruler frame not yet sent to Resolve
        self._seek_quiet_until = 0.0
        self._child = None           # {"proc", "result", "done", "started"} running now
        self._audio = {}             # file key -> its peaks and loudness as arrays, for Match
        self._child_timer = QTimer(self)
        self._child_timer.setInterval(CHILD_POLL_MS)
        self._child_timer.timeout.connect(self._check_child)
        self._worker = ResolveWorker(self)
        self._loader = PeakLoader(self)
        self._loader.ready.connect(self._on_peaks)
        self._loader.progress.connect(lambda key, f: self.emit("peaks_progress", {"key": key, "progress": f,
                                                                                  "left": self._loader.pending()}))
        self._tick = QTimer(self)
        self._tick.setInterval(LIVE_MS)
        self._tick.timeout.connect(self._poll)
        self._tick.start()

    def web_ready(self):
        self.emit("options", {
            "presets": [{"id": p["id"], "label": p["label"]} for p in mixer.PRESETS],
            "crossfades": resolve_ext.CROSSFADES,
            "leveler_modes": mixer.LEVELER_MODES,
            "volume_range": resolve_ext.VOLUME_RANGE,
            "loud_block": LOUD_BLOCK_S,
        })
        self._push_state()
        if self.timeline:
            self.emit("timeline", self.timeline)
            for key, result in self._peaks.items():
                self._emit_peaks(key, result)
        self._push_selection()
        self._push_undo()
        self.emit("playhead", {"frame": self.playhead})

    def on_shown(self):
        self._full_due = 0.0
        self._poll()

    def on_connection_changed(self, connected):
        if not connected:
            self._forget_timeline()
        self._full_due = 0.0
        self._push_state()

    def on_app_quitting(self):
        self._tick.stop()
        self._child_timer.stop()
        self._loader.shutdown()

    # ------------------------------------------------------------ Resolve --

    def _controller(self, connect=False):
        if connect:
            try:
                return self.host.ensure_connected()
            except ResolveConnectionError as exc:
                self.problem = str(exc)
                return None
        return self.host.controller if getattr(self.host, "connected", False) else None

    def _forget_timeline(self):
        if self.timeline is not None:
            self.timeline, self.items = None, {}
            self.emit("timeline", None)

    def _clips(self):
        return {c["id"]: c for t in (self.timeline or {}).get("tracks", []) for c in t["clips"]}

    def _poll(self, connect=False):
        if not self.isVisible() and not connect:
            return
        controller = self._controller(connect)
        if controller is None:
            self._forget_timeline()
            return self._push_state()
        if self._worker.busy():
            if not self.busy and self._worker.running_for() >= BUSY_AFTER_S:
                self.busy = True
                self._push_state()
            return
        if self._child is not None:
            # Resolve is busy with the child's call, and a call made now waits for it
            # holding the GIL (a live read during a 15 s NormalizeAudioLevel froze Buddy for all 15).
            return
        if self.timeline is None or time.monotonic() >= self._full_due:
            cache = self._media_cache
            started = time.monotonic()
            self._worker.start(lambda: resolve_ext.read_timeline(controller, cache),
                               lambda result, error: self._on_full(result, error, started))
        else:
            fps = self.timeline["fps"]
            chosen = {uid: self.items[uid] for uid in self.selected if uid in self.items}
            self._worker.start(lambda: resolve_ext.read_live(controller, fps, chosen), self._on_live)

    def _failed(self, error):
        """A read went wrong: the reason goes in the header, the tracks go."""
        self.busy = False
        self.problem = str(error) if isinstance(error, ResolveConnectionError) else f"Couldn't read the timeline: {error}"
        self._forget_timeline()
        self._full_due = time.monotonic() + FULL_EVERY_S
        self._push_state()

    def _on_full(self, result, error, started):
        if error is not None:
            return self._failed(error)
        self._full_took = time.monotonic() - started
        self._full_due = time.monotonic() + max(FULL_EVERY_S, self._full_took * FULL_SHARE)
        data, self.items = result
        changed = repr(data) != repr(self.timeline)
        if self.timeline is None or data["id"] != self.timeline["id"]:
            self.selected, self.from_resolve, self.resolve_selection = [], False, None
            self._push_selection()
            self._push_undo(data["id"])
        self.timeline, self.problem, self.busy = data, "", False
        self._push_state()
        if changed:
            self.emit("timeline", data)
        for track in data["tracks"]:
            for clip in track["clips"]:
                media = clip.get("media")
                if media and media["key"] not in self._peaks:
                    self._loader.want(media["key"], media["path"])

    def _on_live(self, result, error):
        if error is not None:
            return self._failed(error)
        if self.busy:
            self.busy = False
            self._push_state()
        if self.timeline is None or result["id"] != self.timeline["id"]:
            self._full_due = 0.0             # another timeline: read it on the next tick
            return
        seeking = self._seek is not None or self._child_doing("seek") or time.monotonic() < self._seek_quiet_until
        if result["playhead"] != self.playhead and not seeking:
            self.playhead = result["playhead"]
            self.emit("playhead", {"frame": self.playhead})
        # Resolve's selection wins whenever it changes; a click here stands until then.
        theirs = [uid for uid in result["resolve_selection"] if uid in self.items]
        if theirs != self.resolve_selection:
            first = self.resolve_selection is None
            self.resolve_selection = theirs
            if not first or theirs:
                self.selected, self.from_resolve = theirs, bool(theirs)
                self._push_selection()
        self._merge(result["levels"])

    def _merge(self, fresh, wrote=False):
        """Resolve's levels for some clips into the page's copy, and to the view.
        After a write (wrote) every one goes, changed or not: the view has been
        showing its preview, and Resolve's answer is what it must show now."""
        changed = {}
        for uid, clip in self._clips().items():
            new = fresh.get(uid)
            if new and (wrote or any(clip.get(k) != v for k, v in new.items())):
                clip.update(new)
                changed[uid] = new
        if changed:
            self.emit("levels", {"clips": changed, "wrote": wrote})

    # ----------------------------------------------------- child process --

    def _child_doing(self, do):
        return self._child is not None and self._child["do"] == do

    def _run_child(self, command, done):
        """Runs resolve_child.py with command; done(result, error) when it ends.
        One at a time: the caller checks self._child first."""
        fd, result_path = tempfile.mkstemp(prefix="buddy_audio_", suffix=".json")
        os.close(fd)
        try:
            proc = subprocess.Popen(
                [sys.executable, CHILD, result_path], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc.stdin.write(json.dumps(command).encode("utf-8"))
            proc.stdin.close()
        except OSError as exc:
            os.remove(result_path)
            return done(None, exc)
        self._child = {"proc": proc, "result": result_path, "done": done, "do": command["do"],
                       "started": time.monotonic()}
        self._child_timer.start()

    def _check_child(self):
        child = self._child
        if child is None:
            return self._child_timer.stop()
        proc = child["proc"]
        if proc.poll() is None:
            if time.monotonic() - child["started"] < CHILD_TIMEOUT_S:
                return
            proc.kill()
            proc.wait(2)
        self._child = None
        self._child_timer.stop()
        try:
            with open(child["result"], encoding="utf-8") as f:
                out = json.load(f)
        except (OSError, ValueError):
            out = {"ok": False, "error": "Resolve didn't answer in time." if proc.returncode is None or proc.returncode < 0
                   else "Resolve didn't answer."}
        finally:
            try:
                os.remove(child["result"])
            except OSError:
                pass
        if out.get("ok"):
            child["done"](out.get("result"), None)
        else:
            child["done"](None, ResolveConnectionError(out.get("error") or "Resolve didn't answer."))
        self._start_seek()             # a ruler frame that came in meanwhile

    def _start_seek(self):
        """Sends the latest ruler frame to Resolve, unless the child process is
        busy - then it goes when that's done."""
        if self._child is not None or self._seek is None or self.timeline is None:
            return
        frame, self._seek = self._seek, None
        tl = self.timeline
        self._run_child({"do": "seek", "timecode": frames_to_timecode(frame, tl["fps"], tl["drop_frame"])},
                        self._on_seek)

    def _on_seek(self, _result, error):
        self._seek_quiet_until = time.monotonic() + SEEK_SETTLE_S
        if error is not None and self._seek is None:
            self.emit("toast", {"text": str(error)})

    # -------------------------------------------------------------- peaks --

    def _on_peaks(self, key, result, error):
        self._peaks[key] = result
        if error:
            self._peak_errors[key] = error
        self._emit_peaks(key, result)

    def _emit_peaks(self, key, result):
        codes, loud = result if result else (None, None)
        self.emit("peaks", {
            "key": key, "rate": PEAK_RATE, "floor": FLOOR_DB, "left": self._loader.pending(),
            "data": base64.b64encode(codes).decode("ascii") if codes else None,
            "loud": base64.b64encode(loud).decode("ascii") if loud else None,
            "error": self._peak_errors.get(key, ""),
        })

    # --------------------------------------------------------------- view --

    def _state(self):
        return {
            "connected": self._controller() is not None,
            "timeline": (self.timeline or {}).get("name", ""),
            "problem": self.problem,
            "busy": self.busy,
        }

    def _push_state(self):
        self.emit("state", self._state())

    def _push_selection(self):
        self.emit("selection", {"ids": self.selected, "from_resolve": self.from_resolve})

    def _push_undo(self, timeline_id=None):
        """The newest undo for this timeline (another timeline's are kept for when it's back)."""
        tid = timeline_id or (self.timeline or {}).get("id")
        mine = [u for u in self.undo_stack if u["timeline"] == tid]
        self.emit("undo", {"label": mine[-1]["label"], "count": len(mine)} if mine else None)

    # ------------------------------------------------------------ writing --

    def _writable(self):
        """The controller, once no read is waiting on Resolve - or None, having said why."""
        controller = self._controller(connect=True)
        if controller is None or self.timeline is None:
            self.emit("alert", {"title": "Not connected", "text": self.problem or "Open a timeline in Resolve first."})
            return None
        # A call while the child has Resolve busy would freeze Buddy until it's done
        # (see _poll): a seek is over in half a second, so it's waited for.
        if self._child is not None:
            try:
                self._child["proc"].wait(WRITE_WAIT_S)
            except subprocess.TimeoutExpired:
                self.emit("alert", {"title": BUSY_TITLE, "text": BUSY_TEXT})
                return None
        if not self._worker.wait_idle(WRITE_WAIT_S):
            self.emit("alert", {"title": BUSY_TITLE, "text": BUSY_TEXT})
            return None
        return controller

    def _written(self, label, result, count):
        """A write's result: the fresh levels shown, the undo kept, and what happened said."""
        self._merge(result["after"], wrote=True)
        before = {uid: old for uid, old in result["before"].items() if old}
        if before:
            self.undo_stack.append({"label": label, "timeline": self.timeline["id"], "before": before})
            del self.undo_stack[:-UNDO_LIMIT]
            self._push_undo()
        failed = len(result.get("failed") or [])
        done = count - failed
        if failed and not done:
            self.emit("alert", {"title": "Resolve refused", "text": "Resolve didn't take the change for those clips."})
        elif failed:
            self.emit("toast", {"text": f"Changed {done} of {count} clips – Resolve refused the rest"})

    # ------------------------------------------------------------ actions --

    def on_refresh(self, _payload=None):
        self._full_due = 0.0
        self.problem = ""
        self._poll(connect=True)

    def on_select(self, payload):
        ids = [str(i) for i in ((payload or {}).get("ids") or []) if str(i) in self.items]
        self.selected, self.from_resolve = ids, False
        self._push_selection()

    def on_seek(self, payload):
        """A click or drag on the ruler: Resolve's playhead follows, latest first."""
        try:
            frame = int((payload or {}).get("frame"))
        except (TypeError, ValueError):
            return
        if self.timeline is None:
            return
        frame = max(self.timeline["start"], min(self.timeline["end"] - 1, frame))
        self._seek, self.playhead = frame, frame
        self._start_seek()

    def on_set_levels(self, payload):
        """Volume, pan, fades, Voice Isolation or the Dialogue Leveler on clips
        (levels.changes() says what that asks of each)."""
        payload = payload or {}
        change = mixer.changes(self._clips(), payload)
        if not change:
            return self._push_levels_back(payload)
        controller = self._writable()
        if controller is None:
            return self._push_levels_back(payload)
        label = str(payload.get("label") or "Change")
        self._worker.start(lambda: resolve_ext.apply(controller, change),
                           lambda result, error: self._on_set(label, len(change), result, error, payload))

    def _on_set(self, label, count, result, error, payload):
        if error is not None:
            self.emit("alert", {"title": "Couldn't change the clips", "text": str(error)})
            return self._push_levels_back(payload)
        self._written(label, result, count)

    def _push_levels_back(self, payload):
        """The view previews a change as it's made: when Resolve doesn't take
        it, the clips' real levels go back."""
        clips = self._clips()
        self.emit("levels", {"wrote": True, "clips": {
            uid: {k: clips[uid].get(k) for k in ("volume", "pan", "fade_in", "fade_out", "isolation", "leveler")}
            for uid in (payload.get("ids") or []) if uid in clips}})

    def on_match(self, payload):
        """Each clip's volume moved by what takes it to the preset's target, by
        Buddy's own measurement (levels.match) - together, or each on its own."""
        payload = payload or {}
        preset = mixer.PRESET_IDS.get(payload.get("preset"))
        ids = [uid for uid in (payload.get("ids") or []) if uid in self.items]
        if preset is None or not ids or self.timeline is None:
            return
        change, report = mixer.match(self._clips(), ids, self._audio_of, self.timeline["fps"], preset,
                                     independent=bool(payload.get("independent")))
        if report["missing"] and not change:
            return self.emit("alert", {"title": "Nothing to measure yet", "text": (
                "Buddy is still reading those clips' audio, or they have no file it can read. "
                "Try again once their waveforms show.")})
        if not change:
            return self.emit("alert", {"title": "Nothing to match", "text": "Those clips are silent."})
        controller = self._writable()
        if controller is None:
            return
        self._worker.start(lambda: resolve_ext.apply(controller, change),
                           lambda result, error: self._on_matched(preset, len(change), report, result, error))

    def _on_matched(self, preset, count, report, result, error):
        if error is not None:
            return self.emit("alert", {"title": "Couldn't match the loudness", "text": str(error)})
        self._written("Match loudness", result, count)
        skipped = len(report["missing"]) + len(report["silent"])
        if report["clamped"]:
            self.emit("alert", {"title": "Some clips couldn't get there", "text": (
                "+30 dB is as loud as Resolve's clip volume goes, and some clips needed more. "
                "They're at +30 dB.")})
        elif skipped:
            self.emit("toast", {"text": f"Matched to {preset['label']} – {skipped} left out, with no audio to measure yet"})
        else:
            self.emit("toast", {"text": f"Matched to {preset['label']}"})

    def _audio_of(self, clip):
        """A clip's file's decoded peaks and loudness, for levels.match - or None."""
        media = clip.get("media")
        result = self._peaks.get(media["key"]) if media else None
        if not result:
            return None
        key = media["key"]
        if key not in self._audio:
            self._audio[key] = {"codes": np.frombuffer(result[0], dtype=np.uint8),
                                "loud": np.frombuffer(result[1], dtype=np.float32),
                                "rate": PEAK_RATE, "floor": FLOOR_DB, "block": LOUD_BLOCK_S}
        return self._audio[key]

    def on_crossfade(self, payload):
        """A crossfade on every cut between the selected clips. The view has
        asked first: Buddy can't take these off again."""
        payload = payload or {}
        kind = payload.get("kind") if payload.get("kind") in resolve_ext.CROSSFADES else resolve_ext.CROSSFADES[0]
        try:
            frames = max(2, min(240, int(payload.get("frames") or 12)))
        except (TypeError, ValueError):
            frames = 12
        left = mixer.cuts(self.timeline["tracks"] if self.timeline else [], payload.get("ids") or [])
        if not left:
            return self.emit("toast", {"text": "No cuts between the selected clips."})
        controller = self._writable()
        if controller is None:
            return
        self._worker.start(lambda: resolve_ext.crossfade(controller, left, kind, frames),
                           lambda result, error: self._on_crossfaded(len(left), result, error))

    def _on_crossfaded(self, count, result, error):
        self._full_due = 0.0                 # the new transitions show on the next read
        if error is not None:
            return self.emit("alert", {"title": "Couldn't add the crossfades", "text": str(error)})
        added, failed = result
        if failed:
            self.emit("alert", {"title": f"Added {added} of {count} crossfades", "text": (
                "Resolve refused the rest – a crossfade needs media to spare past the cut on both clips.")})
        else:
            self.emit("toast", {"text": "Added 1 crossfade" if added == 1 else f"Added {added} crossfades"})

    def on_undo(self, _payload=None):
        tid = (self.timeline or {}).get("id")
        mine = [i for i, u in enumerate(self.undo_stack) if u["timeline"] == tid]
        if not mine:
            return
        controller = self._writable()
        if controller is None:
            return
        entry = self.undo_stack.pop(mine[-1])
        self._push_undo()
        self._worker.start(lambda: resolve_ext.apply(controller, entry["before"]),
                           lambda result, error: self._on_undone(entry, result, error))

    def _on_undone(self, entry, result, error):
        if error is not None or result["failed"]:
            self.undo_stack.append(entry)          # nothing lost: it can be tried again
            self._push_undo()
            return self.emit("alert", {"title": "Couldn't undo", "text": str(error or "Resolve refused some clips.")})
        self._merge(result["after"], wrote=True)
        self.emit("toast", {"text": f"Undone: {entry['label']}"})
