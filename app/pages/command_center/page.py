#!/usr/bin/env python3
"""
Command Center - Buddy's actions on hot keys that work whichever app is in
front (core/hotkeys.py), and on buttons here. What each action does is
actions.py; a web page (web/index.html + command.js) lists them.

A hot key is a binding: an action, its options (a marker's colour, say)
and the keys. One action can have several - a hot key per marker colour.
Pressed in Resolve, a binding runs on this page's ResolveWorker and a note
over the screen says what happened (popups.Osd); run from here, a toast
does. "Apply an Animation preset" goes through the Animation page instead,
so its Resolve jobs never overlap Animation's own.

WRITES to Resolve only when an action runs - from its button or its keys.

Protocol:
    to the view    state, toast
    from the view  enable, run, record, clear_keys, option, add, remove, browse
"""

import os
import secrets
import time

from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import QFileDialog

from core import hotkeys
from core.i18n import tr
from core.resolve_bridge import ResolveConnectionError
from core.resolve_worker import ResolveWorker
from core.web_page import WebToolPage

from . import actions
from .popups import NamePrompt, Osd

RECENT = 8
BUSY_NOTE_S = 2.0
DEFAULTS = {"enabled": True, "bindings": None, "recent": []}


def default_bindings() -> list[dict]:
    """One of each action, without keys - set them on the page."""
    return [{"id": secrets.token_hex(4), "action": a["id"], "options": actions.default_options(a["id"]), "keys": ""}
            for a in actions.ACTIONS]


def clean_bindings(raw) -> list[dict]:
    out, seen = [], set()
    for b in raw if isinstance(raw, list) else []:
        if not isinstance(b, dict) or b.get("action") not in actions.BY_ID:
            continue
        binding_id = b.get("id") if isinstance(b.get("id"), str) and b.get("id") not in seen else secrets.token_hex(4)
        seen.add(binding_id)
        keys = b.get("keys") if isinstance(b.get("keys"), str) and hotkeys.parse(b.get("keys")) else ""
        out.append({"id": binding_id, "action": b["action"], "options": actions.clean_options(b["action"], b.get("options")),
                    "keys": keys})
    return out


class CommandCenterPage(WebToolPage):
    tool_id = "command_center"
    display_name = "Command Center"
    category = "Command Center"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, dict(DEFAULTS))
        if self.settings.get("bindings") is None:
            self.settings["bindings"] = default_bindings()
            self.settings.save()
        self.bindings = clean_bindings(self.settings.get("bindings"))
        self.recent = [r for r in self.settings.get("recent") or [] if isinstance(r, dict)][:RECENT]
        self.key_errors = {}          # binding id -> why its keys didn't register
        self.running = set()          # binding ids being run
        self._queued = None           # one job waiting for the worker
        self._worker = ResolveWorker(self)
        self._osd = None
        self._prompt = None
        self.hotkeys = hotkeys.GlobalHotkeys(self) if hotkeys.SUPPORTED else None
        if self.hotkeys is not None:
            self.hotkeys.pressed.connect(self._pressed)
        self._register()

    def web_ready(self):
        self._push_state()

    def on_shown(self):
        self._push_state()   # Animation's presets may have changed

    def on_app_quitting(self):
        if self.hotkeys is not None:
            self.hotkeys.clear()

    # ----------------------------------------------------------- bindings --

    def _save(self):
        self.settings["bindings"] = [dict(b) for b in self.bindings]
        self.settings.save()

    def _binding(self, binding_id):
        return next((b for b in self.bindings if b["id"] == binding_id), None)

    def _register(self):
        if self.hotkeys is None:
            return
        combos = {b["id"]: b["keys"] for b in self.bindings if b["keys"]} if self.settings.get("enabled", True) else {}
        self.key_errors = self.hotkeys.set(combos)

    def _presets(self):
        page = getattr(self.host, "pages", {}).get("text_animator")
        try:
            return page.hotkey_presets() if page is not None else []
        except Exception:   # noqa: BLE001 - Animation not ready: no presets offered yet
            return []

    def _options_for(self, binding) -> dict:
        options = dict(binding["options"])
        if binding["action"] == "animation" and not options.get("preset"):
            presets = self._presets()
            options["preset"] = presets[0][0] if presets else ""
        return options

    def _push_state(self):
        presets = self._presets()
        names = dict(presets)
        used = {}
        for b in self.bindings:
            if b["keys"]:
                used.setdefault(b["keys"], []).append(b["id"])
        groups = []
        for group in actions.GROUPS:
            rows = []
            for a in [a for a in actions.ACTIONS if a["group"] == group]:
                specs = []
                for o in a["options"]:
                    spec = dict(o)
                    if o["kind"] == "preset":
                        spec["choices"] = [{"id": i, "label": label} for i, label in presets]
                    specs.append(spec)
                rows.append({
                    "id": a["id"], "label": a["label"], "about": a["about"], "options": specs,
                    "bindings": [self._binding_view(b, names, used) for b in self.bindings if b["action"] == a["id"]],
                })
            groups.append({"name": group, "actions": rows})
        self.emit("state", {
            "supported": hotkeys.SUPPORTED, "enabled": bool(self.settings.get("enabled", True)),
            "groups": groups, "recent": self.recent,
            "clip_colors": actions.CLIP_COLOR_HEX,
        })

    def _binding_view(self, b, names, used) -> dict:
        options = self._options_for(b)
        note, error = "", self.key_errors.get(b["id"], "")
        if b["keys"]:
            if len(used.get(b["keys"], [])) > 1:
                error = "Another hot key here uses the same keys – only one of them works."
            taken = hotkeys.conflicts(b["keys"])
            if taken:
                note = f"Resolve uses {b['keys']} for {taken} – Buddy takes it while this is set."
        return {"id": b["id"], "summary": actions.summary(b["action"], options, names), "options": options,
                "keys": b["keys"], "note": note, "error": error, "running": b["id"] in self.running}

    def on_enable(self, payload):
        self.settings["enabled"] = bool((payload or {}).get("on"))
        self.settings.save()
        self._register()
        self._push_state()

    def on_record(self, payload):
        """The keys pressed while a binding's key box was waiting for them."""
        payload = payload or {}
        b = self._binding(payload.get("id"))
        if b is None:
            return
        combo = hotkeys.from_event(str(payload.get("code") or ""), bool(payload.get("ctrl")), bool(payload.get("alt")),
                                   bool(payload.get("shift")), bool(payload.get("meta")))
        why = hotkeys.problem(combo) if combo else "That key can't be a hot key – try a letter, a number or F1–F24."
        if why:
            self.emit("toast", {"text": why})
            return self._push_state()
        b["keys"] = combo
        self._save()
        self._register()
        self._push_state()

    def on_clear_keys(self, payload):
        b = self._binding((payload or {}).get("id"))
        if b is not None and b["keys"]:
            b["keys"] = ""
            self._save()
            self._register()
            self._push_state()

    def on_option(self, payload):
        payload = payload or {}
        b = self._binding(payload.get("id"))
        if b is None:
            return
        b["options"] = actions.clean_options(b["action"], {**b["options"], str(payload.get("key")): payload.get("value")})
        self._save()
        self._push_state()

    def on_browse(self, payload):
        payload = payload or {}
        b = self._binding(payload.get("id"))
        if b is None:
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Choose a folder"), b["options"].get("folder") or "")
        if folder:
            self.on_option({"id": b["id"], "key": "folder", "value": os.path.normpath(folder)})

    def on_add(self, payload):
        action_id = (payload or {}).get("action")
        if action_id in actions.BY_ID:
            self.bindings.append({"id": secrets.token_hex(4), "action": action_id,
                                  "options": actions.default_options(action_id), "keys": ""})
            self._save()
            self._push_state()

    def on_remove(self, payload):
        b = self._binding((payload or {}).get("id"))
        if b is None:
            return
        self.bindings.remove(b)
        self._save()
        self._register()
        self._push_state()

    # ------------------------------------------------------------ running --

    def on_run(self, payload):
        b = self._binding((payload or {}).get("id"))
        if b is not None:
            self._run(b, from_keys=False)

    def _pressed(self, binding_id):
        b = self._binding(binding_id)
        if b is not None:
            self._run(b, from_keys=True)

    def _controller(self):
        if getattr(self.host, "connected", False) and self.host.controller is not None:
            return self.host.controller
        try:
            return self.host.ensure_connected()
        except ResolveConnectionError:
            return None

    def _run(self, b, from_keys):
        options = self._options_for(b)
        say = lambda text, ok: self._said(b, text, ok, from_keys)   # noqa: E731
        if b["action"] == "animation":
            page = getattr(self.host, "pages", {}).get("text_animator")
            if page is None or not options.get("preset"):
                return say("Pick an Animation preset for this hot key first.", False)
            self._mark(b, True)
            return page.apply_for_hotkey(options["preset"], options.get("way"),
                                         lambda text, ok: (self._mark(b, False), say(text, ok)))
        controller = self._controller()
        if controller is None:
            return say("Not connected to Resolve.", False)
        if b["action"] == "marker" and options.get("ask"):
            return self._job(b, lambda: actions.playhead(controller),
                             lambda at: self._ask_name(b, controller, options, at, from_keys), say)
        runner = actions.RUNNERS[b["action"]]
        self._job(b, lambda: runner(controller, options), lambda result: self._finish(result, say), say)

    def _job(self, b, job, then, say):
        """job() on the worker, then(result) on the UI thread - or say() what went wrong."""
        def done(result, error):
            self._mark(b, False)
            if isinstance(error, actions.ActionError):
                say(str(error), False)
            elif error is not None:
                say(f"Something went wrong: {error}", False)
            else:
                then(result)
            self._next()

        self._mark(b, True)
        if self._worker.busy():
            if self._worker.running_for() > BUSY_NOTE_S:
                say("Resolve is busy – this runs as soon as it's free.", True)
            if self._queued is not None and self._queued[0] is not b:
                self._mark(self._queued[0], False)   # the newest press waits; an older waiting one is dropped
            self._queued = (b, job, done)
        else:
            self._worker.start(job, done)

    def _next(self):
        if self._queued is not None and not self._worker.busy():
            _b, job, done = self._queued
            self._queued = None
            self._worker.start(job, done)

    def _ask_name(self, b, controller, options, at, from_keys):
        prompt = NamePrompt(f"{options['color']} marker at {at['timecode']}", self.host.theme_tokens())
        self._prompt = prompt   # kept until it closes
        say = lambda text, ok: self._said(b, text, ok, from_keys)   # noqa: E731
        prompt.answered.connect(lambda name: self._job(
            b, lambda: actions.add_marker(controller, at["frame"], options["color"], name, at["timecode"]),
            lambda result: self._finish(result, say), say))
        prompt.ask()

    def _finish(self, result, say):
        text = result.get("text", "Done")
        if result.get("clipboard"):
            QGuiApplication.clipboard().setText(result["clipboard"])
        if result.get("image"):
            image = QImage(result["image"])
            if image.isNull():
                return say("The frame was exported, but it couldn't be read back to copy.", False)
            QGuiApplication.clipboard().setImage(image)
        say(text, True)

    def _mark(self, b, on):
        (self.running.add if on else self.running.discard)(b["id"])
        self._push_state()

    def _said(self, b, text, ok, from_keys):
        self.recent = ([{"when": time.strftime("%H:%M:%S"), "text": text, "ok": bool(ok),
                         "what": actions.summary(b["action"], self._options_for(b), dict(self._presets()))}]
                       + self.recent)[:RECENT]
        self.settings["recent"] = self.recent
        self.settings.save()
        window = self.window()
        if from_keys or not (window.isVisible() and window.isActiveWindow()):
            if self._osd is None:
                self._osd = Osd()
            self._osd.flash(text, ok, self.host.theme_tokens())
        else:
            self.emit("toast", {"text": text})
        self._push_state()
