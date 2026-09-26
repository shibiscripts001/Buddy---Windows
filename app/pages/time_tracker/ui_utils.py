#!/usr/bin/env python3
"""
Shared duration/date formatting for Time Tracker - the web view, the
exports and the reports all format through these, so a duration reads the
same everywhere. No Qt (the web page has its own toasts, see
app/web/buddy.js).
"""


def format_duration(total_seconds, show_seconds=True):
    total_seconds = int(max(0, total_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if show_seconds:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}"


def format_duration_words(total_seconds):
    total_seconds = int(max(0, total_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def format_clock(iso_str):
    if not iso_str:
        return "--:--"
    return iso_str[11:16] if len(iso_str) >= 16 else iso_str


def format_date(iso_str):
    if not iso_str:
        return ""
    return iso_str[:10]
