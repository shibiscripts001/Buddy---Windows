#!/usr/bin/env python3
"""
Time Tracker's tracking rules - what happens to the open entry when
Resolve's project changes, when the user pauses/stops/starts, when the
machine goes idle, and after a crash. No Qt: the page (page.py) owns the
timers, the Resolve poll and the view, and calls in here; tests drive this
directly (tests/test_time_tracker_idle.py).

States: "tracking", "paused", "offline" (auto mode, nothing open in
Resolve), "stopped" (manual mode, idle).

Hooks the page sets:
    on_change()      entries were added/ended/edited - redraw lists/reports
    request_poll()   ask Resolve now rather than at the next poll tick
"""

from datetime import datetime, timedelta

from .data_manager import now_iso

POLL_MIN_MS = 2000

# The open entry's "last_seen" heartbeat (see touch_heartbeat) - how often
# it's written to disk while an entry is open, and the floor for how old it
# has to be at startup before the gap counts as downtime, not work (see
# close_stale_open_entry).
HEARTBEAT_INTERVAL_S = 60
STALE_ENTRY_MIN_S = 5 * 60


class TrackerEngine:
    def __init__(self, data_mgr, on_change=None, request_poll=None):
        self.data_mgr = data_mgr
        self.on_change = on_change or (lambda: None)
        self.request_poll = request_poll or (lambda: None)
        self.current_project = None
        self.current_state = "offline"
        self.detected_project_name = None
        self._manual_mode = None  # forces the first sync_manual_mode() to always apply
        self._idle_paused = False

    @property
    def manual_mode(self):
        return bool(self._manual_mode)

    # ---------- Auto (Resolve-follow) tracking ----------

    def toggle_pause(self, paused):
        """Freezes/resumes the currently open entry IN PLACE (see
        DataManager.pause_entry/resume_entry) - the entry stays open the
        whole time, so its elapsed timer just stops advancing while paused
        and continues from the same point on Resume, rather than being
        ended-and-restarted."""
        self.data_mgr.settings["manually_paused"] = paused
        self.data_mgr.save_settings()
        open_entry = self.data_mgr.get_open_entry()
        if paused:
            if open_entry:
                self.data_mgr.pause_entry(open_entry)
            self.current_state = "paused"
        else:
            if open_entry:
                self.data_mgr.resume_entry(open_entry)
            # Set immediately rather than waiting for the next poll's
            # on_project_detected to confirm it - otherwise the UI would
            # keep showing "Paused" for up to one whole poll interval after
            # Resume, even though the entry itself already resumed.
            self.current_state = "tracking" if open_entry else "offline"
        self.on_change()
        self.request_poll()

    def stop_tracking(self):
        """Auto mode's Stop - unlike Pause, this actually ends the current
        entry (saving it to History) and resets the timer to zero. Also
        clears manually_paused so a Stop pressed while paused doesn't leave
        tracking stuck paused afterward - the very next poll picks the
        still-open Resolve project back up and starts a fresh entry.
        Returns (entry_id, project) for an Undo offer, or None."""
        undo = None
        open_entry = self.data_mgr.get_open_entry()
        if open_entry:
            undo = self._end_for_undo(open_entry)
        self.data_mgr.settings["manually_paused"] = False
        self.data_mgr.save_settings()
        self.on_change()
        self.request_poll()
        return undo

    def _end_for_undo(self, open_entry):
        """Ends open_entry; (entry_id, project) if it was long enough to be
        kept (see DataManager.end_entry's min_seconds blip-drop), else None."""
        entry_id = open_entry["id"]
        project_name = open_entry.get("project", "Unknown")
        self.data_mgr.end_entry(open_entry)
        if any(e["id"] == entry_id for e in self.data_mgr.entries):
            return entry_id, project_name
        return None

    def undo_stop(self, entry_id):
        """Reopens an entry a Stop just ended. Returns True if it did."""
        entry = next((e for e in self.data_mgr.entries if e["id"] == entry_id), None)
        if entry is None or entry.get("end") is None:
            return False  # already gone, or somehow already reopened
        # Only one entry can be open at a time - if tracking already picked
        # back up against a NEW entry in the meantime, close that one out
        # first so reopening this one doesn't leave two open entries at once.
        currently_open = self.data_mgr.get_open_entry()
        if currently_open and currently_open["id"] != entry_id:
            self.data_mgr.delete_entry(currently_open["id"])
        entry["end"] = None
        self.data_mgr.save_entries()
        self.current_state = "tracking"
        self.current_project = entry.get("project")
        self.on_change()
        return True

    def wants_poll(self):
        """Whether the page should ask Resolve for its project this tick.
        Manual mode still polls - purely to know what project is open.
        Auto mode polls even while manually paused, so Resolve closing
        still ends the entry; but NOT while idle-paused: polling would just
        immediately restart tracking against the same still-open project -
        check_idle() is what un-pauses that once activity is seen again."""
        if self._manual_mode:
            return True
        return not self._idle_paused

    def on_project_detected(self, project_name, poll_succeeded):
        if not poll_succeeded:
            return  # A crash or timeout in the worker shouldn't end the session

        # Resolve's own process being gone (not just "no project currently
        # open") is folded into the same "no project" handling below -
        # Buddy has no reason to quit just because Resolve closed.
        self.detected_project_name = project_name

        if self._manual_mode:
            # Never auto start/stop in Manual Tracking mode - but if a
            # session is already running and Resolve's current project
            # changes underneath it, keep the running entry attributed to
            # whatever project is actually open now.
            if self.current_state == "tracking":
                open_entry = self.data_mgr.get_open_entry()
                if open_entry and project_name and open_entry.get("project") != project_name:
                    self.data_mgr.end_entry(open_entry)
                    self.data_mgr.start_entry(project_name)
                    self.current_project = project_name
                    self.on_change()
            return

        if self.data_mgr.settings.get("manually_paused", False):
            if project_name is None:
                open_entry = self.data_mgr.get_open_entry()
                if open_entry:
                    self.data_mgr.end_entry(open_entry)
                self.current_state = "offline"
                self.current_project = None
            else:
                self.current_state = "paused"
                self.current_project = project_name
            self.on_change()
            return
        # A poll already in flight when enter_idle_pause() ended the entry
        # would otherwise see no open entry and start a fresh one that runs
        # through the whole idle period.
        if self._idle_paused:
            return

        open_entry = self.data_mgr.get_open_entry()

        if project_name is None:
            if open_entry:
                self.data_mgr.end_entry(open_entry)
                self.on_change()
            self.current_state = "offline"
            self.current_project = None
            return

        self.current_state = "tracking"
        self.current_project = project_name
        if open_entry and open_entry.get("project") == project_name:
            return
        if open_entry:
            self.data_mgr.end_entry(open_entry)
        self.data_mgr.start_entry(project_name)
        self.on_change()

    # ---------- Manual tracking ----------

    def manual_toggle(self, starting):
        """Manual mode's Start (True) / Stop (False). Stop returns an Undo
        offer like stop_tracking()."""
        undo = None
        if starting:
            project_name = self.detected_project_name or "Untitled"
            stray = self.data_mgr.get_open_entry()
            if stray:
                self.data_mgr.end_entry(stray)
            self.data_mgr.start_entry(project_name)
            self.current_state = "tracking"
            self.current_project = project_name
        else:
            open_entry = self.data_mgr.get_open_entry()
            if open_entry:
                undo = self._end_for_undo(open_entry)
            self.current_state = "stopped"
            self.current_project = None
        self.on_change()
        return undo

    def manual_pause_toggle(self, paused):
        """Manual Tracking's own Pause/Resume - freezes the running entry in
        place exactly like toggle_pause() does for auto mode, just without
        touching "manually_paused", which is auto mode's own escape valve."""
        open_entry = self.data_mgr.get_open_entry()
        if paused:
            if open_entry:
                self.data_mgr.pause_entry(open_entry)
            self.current_state = "paused"
        else:
            if open_entry:
                self.data_mgr.resume_entry(open_entry)
            self.current_state = "tracking"
        self.on_change()

    def sync_manual_mode(self):
        """Reflects the Manual Tracking setting. Also runs once at startup
        (via the _manual_mode=None sentinel) to match the saved setting."""
        manual = self.data_mgr.settings.get("manual_tracking", False)
        if manual == self._manual_mode:
            return

        # Switching modes mid-session: close out whatever's open so an
        # entry never gets attributed to the wrong tracking mode.
        open_entry = self.data_mgr.get_open_entry()
        if open_entry:
            self.data_mgr.end_entry(open_entry)
            self.on_change()

        self._manual_mode = manual
        self.current_project = None
        self.current_state = "stopped" if manual else "offline"

    # ---------- Downtime (crash / kill / Windows shutdown) ----------

    def touch_heartbeat(self):
        """Runs every UI tick. Stamps the open entry's "last_seen" at most
        once a HEARTBEAT_INTERVAL_S, so if Buddy dies without reaching
        app_quitting() the next launch knows roughly when time stopped
        being observed (see close_stale_open_entry). abs(): a clock set
        backwards would otherwise stop the heartbeat until it caught up."""
        open_entry = self.data_mgr.get_open_entry()
        if not open_entry:
            return
        try:
            latest = datetime.fromisoformat(open_entry.get("last_seen") or open_entry["start"])
        except (KeyError, TypeError, ValueError):
            latest = None
        if latest is not None and abs((datetime.now() - latest).total_seconds()) < HEARTBEAT_INTERVAL_S:
            return
        open_entry["last_seen"] = now_iso()
        self.data_mgr.save_entries()

    def close_stale_open_entry(self):
        """Runs once at startup. An entry still open now was left behind by
        a Buddy that was killed, crashed, or went down with Windows - if it
        was last seen longer ago than a couple of heartbeats/polls, end it
        at that moment instead of letting the downtime count as work.
        "last_seen" is optional (older entries, or a crash within the first
        minute), so fall back to the latest of start/pause_started_at."""
        open_entry = self.data_mgr.get_open_entry()
        if not open_entry:
            return
        stamps = []
        for key in ("start", "last_seen", "pause_started_at"):
            try:
                stamps.append(datetime.fromisoformat(open_entry[key]))
            except (KeyError, TypeError, ValueError):
                pass
        if not stamps:
            return
        last_alive = max(stamps)
        poll_s = max(POLL_MIN_MS / 1000, float(self.data_mgr.settings.get("poll_interval_seconds", 5)))
        threshold_s = max(STALE_ENTRY_MIN_S, 2 * (HEARTBEAT_INTERVAL_S + poll_s))
        if (datetime.now() - last_alive).total_seconds() <= threshold_s:
            return
        self.data_mgr.end_entry(open_entry, when=last_alive.isoformat(timespec="seconds"))

    # ---------- Idle detection ----------

    def check_idle(self, idle_seconds):
        """Runs every UI tick with the machine's current idle time. Ends the
        running entry, trimmed back to the moment activity actually
        stopped, once nobody's touched the keyboard/mouse for the configured
        threshold - Resolve being left open with a project loaded isn't
        itself "working"."""
        if not self.data_mgr.settings.get("idle_detection_enabled", True):
            self._idle_paused = False
            return

        threshold_seconds = float(self.data_mgr.settings.get("idle_threshold_minutes", 5)) * 60

        if self._idle_paused:
            if idle_seconds < threshold_seconds:
                self.exit_idle_pause()
            return

        if idle_seconds >= threshold_seconds and self.current_state == "tracking":
            self.enter_idle_pause(idle_seconds)

    def enter_idle_pause(self, idle_seconds):
        open_entry = self.data_mgr.get_open_entry()
        if open_entry:
            trimmed_end = datetime.now() - timedelta(seconds=idle_seconds)
            start_dt = datetime.fromisoformat(open_entry["start"])
            if trimmed_end < start_dt:
                trimmed_end = start_dt
            self.data_mgr.end_entry(open_entry, when=trimmed_end.isoformat(timespec="seconds"))
            self.on_change()

        self._idle_paused = True
        self.current_state = "stopped" if self._manual_mode else "offline"
        self.current_project = None

    def exit_idle_pause(self):
        self._idle_paused = False
        if not self._manual_mode:
            # Re-check right away instead of waiting for the next poll
            # tick (which could be up to 30s away) - the user just came
            # back, so tracking should pick back up immediately.
            self.request_poll()

    @property
    def idle_paused(self):
        return self._idle_paused

    # ---------- Global hotkey ----------

    def handle_global_hotkey(self):
        """Cycles Start (if Manual Tracking is idle) -> Pause -> Resume."""
        if self._manual_mode:
            if self.current_state == "tracking":
                self.manual_pause_toggle(True)
            elif self.current_state == "paused":
                self.manual_pause_toggle(False)
            elif self.detected_project_name:
                self.manual_toggle(True)
        else:
            self.toggle_pause(self.current_state != "paused")

    # ---------- Shared ----------

    def elapsed_seconds(self):
        open_entry = self.data_mgr.get_open_entry()
        return self.data_mgr.duration_seconds(open_entry) if open_entry else 0.0

    def app_quitting(self):
        """Saves whatever's currently open."""
        open_entry = self.data_mgr.get_open_entry()
        if open_entry:
            self.data_mgr.end_entry(open_entry)
