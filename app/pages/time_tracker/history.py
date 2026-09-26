#!/usr/bin/env python3
"""
The "This Project" and "History" lists - which entries a scope shows, the
summary line above them, each row as the web page draws it, and checking
an added/edited entry. No Qt (tests/test_time_tracker_reports.py).

Scopes: SCOPE_CURRENT follows whatever project Resolve has open (all "This
Project" ever shows, and History's default), SCOPE_ALL is every project,
anything else is one pinned project name. Picking a project or All is a
deliberate override that Resolve's own project changes don't undo.
"""

from datetime import datetime

from .ui_utils import format_clock, format_date, format_duration_words

SCOPE_CURRENT = "__current__"
SCOPE_ALL = "__all__"


def entries_for(data_mgr, scope, current_project):
    entries = data_mgr.closed_entries_sorted()
    if scope == SCOPE_ALL:
        return entries
    if scope == SCOPE_CURRENT:
        # Nothing detected yet (Resolve still on the Project Manager, say):
        # show nothing rather than every project's history at once.
        return [e for e in entries if current_project and e.get("project") == current_project]
    return [e for e in entries if e.get("project") == scope]


def summary(data_mgr, scope, current_project, entries):
    if scope == SCOPE_CURRENT and not current_project:
        return "No project detected in Resolve"
    where = ("all projects" if scope == SCOPE_ALL
             else f'"{current_project if scope == SCOPE_CURRENT else scope}"')
    total = sum(data_mgr.duration_seconds(e) for e in entries if not e.get("hidden", False))
    return f"{format_duration_words(total)} tracked on {where}" if total else f"No time tracked yet on {where}"


def row(data_mgr, entry):
    start = entry["start"]
    try:
        weekday = datetime.fromisoformat(start).strftime("%a")
    except ValueError:
        weekday = ""
    return {
        "id": entry["id"],
        "date": format_date(start),
        "weekday": weekday,
        "project": entry.get("project", ""),
        "start": format_clock(start),
        "end": format_clock(entry.get("end")),
        "duration": format_duration_words(data_mgr.duration_seconds(entry)),
        "notes": entry.get("notes", ""),
        "hidden": bool(entry.get("hidden", False)),
        # For the edit form: full timestamps, to the second.
        "start_iso": start,
        "end_iso": entry.get("end") or "",
    }


def view(data_mgr, scope, current_project):
    entries = entries_for(data_mgr, scope, current_project)
    return {
        "scope": scope,
        "summary": summary(data_mgr, scope, current_project, entries),
        "rows": [row(data_mgr, e) for e in entries],
    }


def _parse(value):
    """An <input type="datetime-local"> value ("2026-09-26T14:05" or with
    seconds) -> datetime, or None."""
    try:
        return datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def clean_entry(data):
    """(fields, None) ready for DataManager.add_manual_entry/update_entry,
    or (None, (field, message)) naming what's wrong."""
    data = data or {}
    project = str(data.get("project") or "").strip()
    if not project:
        return None, ("project", "Enter a project name.")
    start, end = _parse(data.get("start")), _parse(data.get("end"))
    if start is None:
        return None, ("start", "Enter a start date and time.")
    if end is None:
        return None, ("end", "Enter an end date and time.")
    if end <= start:
        return None, ("end", "End time must be after start time.")
    return {
        "project": project,
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "notes": str(data.get("notes") or "").strip(),
    }, None
