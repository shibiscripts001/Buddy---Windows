#!/usr/bin/env python3
"""
Time Tracker - records time against whatever project is open in DaVinci
Resolve (or, in Manual Tracking mode, when you press Start), with history,
reports, goals, rates and invoices. A web page (core/web_page.py): the
view is web/index.html + tracker.js.

Unlike Buddy's one-shot tools this is a background tool: it keeps polling
Resolve and recording for as long as Buddy runs, whichever page is on
screen and with the window hidden to the tray - the shell owns the tray,
the single-instance guard and auto-start with Resolve (core/shell_window.py,
core/single_instance.py, core/startup_manager.py). Pages are never
destroyed, so this page's timers simply keep running.

Where things live:
    engine.py         the tracking rules (auto-follow, pause, manual mode,
                      idle trimming, crash recovery) - no Qt, tested
    history.py        the This Project / History lists and entry checks
    reports.py        every Reports figure; what Export writes
    export_utils.py   CSV / JSON / PDF / XLSX and the invoice PDF
    settings_panel.py this tool's section of the Settings dialog (Qt)
    resolve_bridge.py the background "which project is open" poll

Resolve going away is handled exactly like "no project open": the running
entry is saved and tracking goes to "offline" - closing Resolve never
takes the rest of Buddy down with it.

Protocol:
    to the view    tick, data, undo_offer, entry_result, custom_result,
                   invoice_result, alert, toast
    from the view  pause, stop, start, undo_stop, history_scope,
                   save_entry, hide, delete, export_history, export_report,
                   custom_range, invoice, set_rate
"""

import ctypes
import os
import sys
if sys.platform == "win32":
    import ctypes.wintypes
from datetime import datetime, timedelta

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from core.i18n import tr, tr_filter
from core.message_dialog import alert
from core.web_page import WebToolPage

from . import history, reports
from .currencies import CURRENCIES, format_money
from .data_manager import DataManager
from .engine import POLL_MIN_MS, TrackerEngine
from .export_utils import EXPORTERS, REPORT_EXPORTERS, export_invoice_pdf
from .idle_detector import get_idle_seconds
from .resolve_bridge import ResolveBridge
from .settings_panel import TrackerSettingsMixin
from .ui_utils import format_duration, format_duration_words

STATE_LABELS = {
    "tracking": "Tracking",
    "paused": "Paused",
    "offline": "Resolve not running",
    "stopped": "Stopped",
}

STATE_HINTS = {
    "tracking": "Following whatever project is open in DaVinci Resolve.",
    "paused": "Paused – the timer is frozen until you resume.",
    "offline": "Open DaVinci Resolve with a project to start tracking automatically.",
    "stopped": "Press Start to begin timing the project open in Resolve.",
}
MANUAL_TRACKING_HINT = "Timing the project open in DaVinci Resolve until you stop."
IDLE_HINT = "Paused while you're away – tracking picks up again as soon as you're back."

# Settings > Global Pause/Resume shortcut - Ctrl+Alt+P, chosen as an
# uncommon-enough combination to be unlikely to already be bound elsewhere.
# Windows-only for now.
_WM_HOTKEY = 0x0312
_MOD_CONTROL = 0x0002
_MOD_ALT = 0x0001
_MOD_NOREPEAT = 0x4000
_VK_P = 0x50
_HOTKEY_ID = 1


class _GlobalHotkeyFilter(QAbstractNativeEventFilter):
    """Catches the registered global hotkey's WM_HOTKEY message via Qt's
    native event filter hook - RegisterHotKey() alone only gets a message
    delivered to this thread's queue, it doesn't itself call back into any
    Python code, so this is what actually observes it."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, eventType, message):
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
            self._callback()
        return False, 0


class TimeTrackerPage(TrackerSettingsMixin, WebToolPage):
    tool_id = "time_tracker"
    display_name = "Time Tracker"
    category = "Business"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    def build_state(self):
        self.data_mgr = DataManager()
        self._load_warnings = list(self.data_mgr.load_warnings)
        self.engine = TrackerEngine(self.data_mgr, on_change=self._changed, request_poll=self._poll_tick)
        self.bridge = ResolveBridge()
        self._hotkey_filter = None
        self._hotkey_registered = False
        self._history_scope = history.SCOPE_CURRENT
        self._custom = None
        self._report = None
        self._shown_detected = object()   # never equal: the first tick pushes data

        # Before sync_manual_mode(), which ends any leftover open entry at
        # now() - right for a quick restart, wrong after hours of downtime.
        self.engine.close_stale_open_entry()
        self.engine.sync_manual_mode()
        self._sync_global_hotkey()

        self.bridge.project_detected.connect(self.engine.on_project_detected)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_tick)
        self._restart_poll_timer()
        self._poll_tick()

        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(1000)
        self.ui_timer.timeout.connect(self._tick)
        self.ui_timer.start()

        # Automatic weekly backups - perform_auto_backup_if_due() is itself
        # a cheap no-op unless the interval has actually elapsed, so an
        # hourly check means a session left running still gets one promptly.
        self.data_mgr.perform_auto_backup_if_due()
        self.auto_backup_timer = QTimer(self)
        self.auto_backup_timer.setInterval(60 * 60 * 1000)
        self.auto_backup_timer.timeout.connect(self.data_mgr.perform_auto_backup_if_due)
        self.auto_backup_timer.start()

    def web_ready(self):
        self._push_data()
        self._push_tick()

    def on_shown(self):
        self._push_data()
        if self._load_warnings:
            warnings, self._load_warnings = self._load_warnings, []
            self.emit("alert", {"title": "Time Tracker", "text": "\n\n".join(warnings)})

    # ------------------------------------------------------------ polling

    def _restart_poll_timer(self):
        interval_s = int(self.data_mgr.settings.get("poll_interval_seconds", 5))
        self.poll_timer.setInterval(max(POLL_MIN_MS, interval_s * 1000))
        if not self.poll_timer.isActive():
            self.poll_timer.start()

    def _poll_tick(self):
        """Kick off an async "which project is open" check (see
        ResolveBridge.poll) whose answer arrives at
        engine.on_project_detected - unless the engine says not to."""
        if self.engine.wants_poll():
            self.bridge.poll()

    def _tick(self):
        self.engine.check_idle(get_idle_seconds())
        self.engine.touch_heartbeat()
        # "This Project" follows Resolve's project, so a project switch
        # there redraws the lists even when no entry changed.
        if self.engine.detected_project_name != self._shown_detected:
            self._push_data()
        self._push_tick()

    # ------------------------------------------------------------ the view

    def _changed(self):
        self._push_data()

    def _push_tick(self):
        engine = self.engine
        state = engine.current_state
        manual = engine.manual_mode
        if manual and state == "stopped":
            project = engine.detected_project_name
        else:
            project = engine.current_project
        if engine.idle_paused:
            hint = IDLE_HINT
        elif manual and state == "tracking":
            hint = MANUAL_TRACKING_HINT
        else:
            hint = STATE_HINTS.get(state, "")

        open_entry = self.data_mgr.get_open_entry()
        today_s, week_s = self._report_so_far(open_entry)
        goal = float(self.data_mgr.settings.get("weekly_goal_hours", 0.0) or 0.0)
        self.emit("tick", {
            "state": state,
            "label": "Away" if engine.idle_paused else STATE_LABELS.get(state, state),
            "hint": hint,
            "project": project or "",
            "elapsed": format_duration(engine.elapsed_seconds()),
            "manual": manual,
            "can_start": manual and state == "stopped" and bool(engine.detected_project_name),
            # Auto mode's Pause is an escape valve that works in any state
            # (it stops auto-following); manual mode's only means anything
            # while a session is running.
            "can_pause": state in ("tracking", "paused") if manual else True,
            "paused": state == "paused",
            "can_stop": state in ("tracking", "paused"),
            "today": format_duration_words(today_s),
            "week": format_duration_words(week_s),
            "week_goal": {"hours": goal, "fraction": min(1.0, week_s / 3600 / goal)} if goal > 0 else None,
        })

    def _report_so_far(self, open_entry):
        """Today's and this week's totals including the session running
        now - Reports only count finished entries, the Tracker shouldn't
        make you stop to see where the day stands."""
        report = self._report or reports.build(self.data_mgr)
        today_s, week_s = report["today_seconds"], report["week_seconds"]
        if open_entry:
            today = datetime.now().date()
            week_start = today - timedelta(days=today.weekday())
            today_s += reports.totals_for([open_entry], self.data_mgr, lambda d: d == today)[0]
            week_s += reports.totals_for([open_entry], self.data_mgr, lambda d: d >= week_start)[0]
        return today_s, week_s

    def _push_data(self):
        detected = self.engine.detected_project_name
        self._shown_detected = detected
        self._report = reports.build(self.data_mgr, custom=self._custom)
        projects = self.data_mgr.project_names()
        if self._history_scope not in (history.SCOPE_CURRENT, history.SCOPE_ALL) \
                and self._history_scope not in projects:
            # The pinned project has no entries left - follow Resolve again.
            self._history_scope = history.SCOPE_CURRENT
        rates = {}
        for project in set(projects) | ({detected} if detected else set()):
            info = self.data_mgr.get_project_rate_info(project)
            rates[project] = {"rate": info["rate"], "currency": info["currency"],
                              "text": f"{format_money(info['rate'], info['currency'])} / hour" if info["rate"] > 0 else ""}
        self.emit("data", {
            "current_project": detected or "",
            "this_project": history.view(self.data_mgr, history.SCOPE_CURRENT, detected),
            "history": history.view(self.data_mgr, self._history_scope, detected),
            "projects": projects,
            "reports": reports.view(self.data_mgr, self._report),
            "rates": rates,
            "currencies": [{"code": c, "label": f"{c} ({s})"} for c, s, _n in CURRENCIES],
            "history_formats": list(EXPORTERS),
            "report_formats": list(REPORT_EXPORTERS),
        })

    # --------------------------------------------------- tracker controls

    def on_pause(self, payload):
        paused = bool((payload or {}).get("paused"))
        if self.engine.manual_mode:
            self.engine.manual_pause_toggle(paused)
        else:
            self.engine.toggle_pause(paused)
        self._push_tick()

    def on_stop(self, _payload):
        undo = self.engine.manual_toggle(False) if self.engine.manual_mode else self.engine.stop_tracking()
        if undo:
            entry_id, project = undo
            self.emit("undo_offer", {"entry_id": entry_id, "text": f'Stopped tracking "{project}".'})
        self._push_tick()

    def on_start(self, _payload):
        if self.engine.manual_mode and self.engine.current_state == "stopped":
            self.engine.manual_toggle(True)
            self._push_tick()

    def on_undo_stop(self, payload):
        if self.engine.undo_stop((payload or {}).get("entry_id")):
            self.emit("toast", {"text": "Tracking resumed."})
        self._push_tick()

    # ---------------------------------------------------- history lists

    def on_history_scope(self, payload):
        self._history_scope = (payload or {}).get("scope") or history.SCOPE_CURRENT
        self._push_data()

    def on_save_entry(self, payload):
        """Add (no id) or edit one entry from the view's form."""
        fields, problem = history.clean_entry(payload)
        if problem:
            self.emit("entry_result", {"ok": False, "field": problem[0], "message": problem[1]})
            return
        entry_id = (payload or {}).get("id")
        if entry_id:
            updated = self.data_mgr.update_entry(entry_id, fields["project"], fields["start"], fields["end"], fields["notes"])
            if updated is None:
                self.emit("entry_result", {"ok": False, "field": "", "message": "That entry no longer exists."})
                return
            text = "Entry updated"
        else:
            self.data_mgr.add_manual_entry(fields["project"], fields["start"], fields["end"], fields["notes"])
            text = "Entry added"
        self.emit("entry_result", {"ok": True})
        self._push_data()
        self.emit("toast", {"text": text})

    def _ids(self, payload):
        return {i for i in (payload or {}).get("ids") or [] if isinstance(i, str)}

    def on_hide(self, payload):
        """Hidden entries stay in the list (dimmed) but are left out of
        totals, reports and exports."""
        hidden = bool((payload or {}).get("hidden"))
        for entry_id in self._ids(payload):
            self.data_mgr.set_entry_hidden(entry_id, hidden)
        self._push_data()

    def on_delete(self, payload):
        ids = self._ids(payload)
        if ids:
            self.data_mgr.delete_entries(ids)
            self._push_data()
            self.emit("toast", {"text": "Deleted 1 entry" if len(ids) == 1 else f"Deleted {len(ids)} entries"})

    def _save_path(self, title, filename, file_filter):
        default_path = self.data_mgr.default_export_path(filename)
        path, _ = QFileDialog.getSaveFileName(self, title, default_path, tr_filter(file_filter))
        if not path:
            return None
        # QFileDialog doesn't reliably auto-append the extension on every
        # platform/filter combination - make sure the file opens right.
        ext = os.path.splitext(filename)[1]
        return path if path.lower().endswith(ext) else path + ext

    def _export(self, format_name, spec, run):
        path = self._save_path(tr("Export {format}").format(format=format_name), spec["filename"], spec["filter"])
        if not path:
            return
        try:
            run(path)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.emit("alert", {"title": "Export failed", "text": f"Could not export {format_name}:\n{exc}"})
            return
        self.data_mgr.remember_export_folder(path)
        self.emit("toast", {"text": f"Exported {format_name}"})

    def on_export_history(self, payload):
        """Exports what's shown: the tab's scope, minus hidden entries."""
        payload = payload or {}
        format_name = payload.get("format")
        spec = EXPORTERS.get(format_name)
        if spec is None:
            return
        scope = history.SCOPE_CURRENT if payload.get("tab") == "this_project" else self._history_scope
        entries = [e for e in history.entries_for(self.data_mgr, scope, self.engine.detected_project_name)
                   if not e.get("hidden", False)]
        self._export(format_name, spec, lambda path: spec["func"](entries, self.data_mgr, path))

    # ------------------------------------------------------------ reports

    def on_custom_range(self, payload):
        payload = payload or {}
        custom, error = reports.custom_range(self.data_mgr, payload.get("from"), payload.get("to"))
        self.emit("custom_result", {"error": error or ""})
        if custom:
            self._custom = custom
            self._push_data()

    def on_export_report(self, payload):
        format_name = (payload or {}).get("format")
        spec = REPORT_EXPORTERS.get(format_name)
        if spec is None:
            return
        report = reports.build(self.data_mgr, custom=self._custom)
        self._export(format_name, spec, lambda path: spec["func"](report, path))

    def on_invoice(self, payload):
        payload = payload or {}
        project = payload.get("project") or ""
        start, end = payload.get("from") or "", payload.get("to") or ""
        if project not in self.data_mgr.project_names():
            self.emit("invoice_result", {"ok": False, "message": "Pick a project."})
            return
        if not start or not end or start > end:
            self.emit("invoice_result", {"ok": False, "message": "The 'From' date must be on or before the 'To' date."})
            return
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in project).strip() or "project"
        path = self._save_path(tr("Generate invoice"), f"invoice_{safe}_{start}_to_{end}.pdf", "PDF files (*.pdf)")
        if not path:
            return
        try:
            export_invoice_pdf(reports.visible_entries(self.data_mgr), self.data_mgr, project, start, end, path,
                               bill_to=payload.get("bill_to") or "", invoice_number=payload.get("number") or "")
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.emit("alert", {"title": "Invoice failed", "text": f"Could not generate the invoice:\n{exc}"})
            return
        self.data_mgr.remember_export_folder(path)
        self.emit("invoice_result", {"ok": True})
        self.emit("toast", {"text": f"Invoice saved to {path}"})

    def on_set_rate(self, payload):
        """The rate for one project, set from Reports (the Settings dialog's
        Project Rate button does the same for the current project)."""
        payload = payload or {}
        project = payload.get("project")
        if not project:
            return
        try:
            rate = max(0.0, float(str(payload.get("rate") or "0").replace(",", "")))
        except ValueError:
            self.emit("toast", {"text": "The rate has to be a number."})
            return
        codes = {c for c, _s, _n in CURRENCIES}
        currency = payload.get("currency") if payload.get("currency") in codes else "USD"
        self.data_mgr.set_project_rate(project, rate, currency)
        self._push_data()
        self.emit("toast", {"text": f"Rate saved for {project}" if rate else f"Rate cleared for {project}"})

    # ----------------------------------------------------------- settings

    def _apply_settings(self):
        """After the Settings section (settings_panel.py) saved something."""
        self._restart_poll_timer()
        self.engine.sync_manual_mode()
        self._sync_global_hotkey()
        self._push_data()
        self._push_tick()

    # ------------------------------------------------------ global hotkey

    def _sync_global_hotkey(self):
        """Registers/unregisters the global Pause/Resume shortcut per the
        Time Tracker settings section. Windows-only."""
        if sys.platform != "win32":
            return
        wanted = self.data_mgr.settings.get("global_hotkey_enabled", False)
        if wanted == self._hotkey_registered:
            return

        app = QApplication.instance()
        if wanted:
            if self._hotkey_filter is None:
                self._hotkey_filter = _GlobalHotkeyFilter(self._handle_global_hotkey)
                app.installNativeEventFilter(self._hotkey_filter)
                app.aboutToQuit.connect(self._unregister_hotkey)
            ok = ctypes.windll.user32.RegisterHotKey(
                None, _HOTKEY_ID, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, _VK_P
            )
            self._hotkey_registered = bool(ok)
            if not ok:
                # Some other application already has this exact combination -
                # don't leave the checkbox looking enabled while silently
                # doing nothing; turn it back off and say why.
                self.data_mgr.settings["global_hotkey_enabled"] = False
                self.data_mgr.save_settings()
                # Over the Settings window if that's where it was turned on.
                alert(
                    QApplication.activeModalWidget() or self, "Global shortcut unavailable",
                    "Ctrl+Alt+P is already in use by another application – could not "
                    "register the global pause/resume shortcut.",
                )
        else:
            self._unregister_hotkey()

    def _unregister_hotkey(self):
        if self._hotkey_registered and sys.platform == "win32":
            ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
        self._hotkey_registered = False

    def _handle_global_hotkey(self):
        self.engine.handle_global_hotkey()
        self._push_tick()

    # --------------------------------------------------------------- quit

    def on_app_quitting(self):
        """Called by the shell right before it tears down (see pages/base.py)
        - also at Windows logoff/shutdown, where no window gets closed.
        Saves whatever's open and releases the global hotkey."""
        self._unregister_hotkey()
        self.engine.app_quitting()
