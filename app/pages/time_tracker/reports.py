#!/usr/bin/env python3
"""
Reports' numbers - today / this week / this month totals with earnings per
currency, goal progress, the last 7 days, all-time per project and a custom
date range. No Qt (tests/test_time_tracker_reports.py).

build() returns the same dict export_utils.py's REPORT_EXPORTERS have
always taken, plus a few view-only keys the web page draws from - so what
gets exported is always exactly what was on screen.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from .currencies import format_money
from .ui_utils import format_duration_words


def _day_segments(entry, data_mgr):
    """(date, seconds) for each calendar day an entry touches, splitting a
    session that runs past midnight proportionally (pauses included)."""
    start_dt = datetime.fromisoformat(entry["start"])
    end_dt = datetime.fromisoformat(entry["end"]) if entry.get("end") else datetime.now()
    total = data_mgr.duration_seconds(entry)
    wall = max(1.0, (end_dt - start_dt).total_seconds())
    current = start_dt
    while current.date() <= end_dt.date():
        next_day = datetime.combine(current.date() + timedelta(days=1), datetime.min.time())
        segment_end = min(end_dt, next_day)
        yield current.date(), total * ((segment_end - current).total_seconds() / wall)
        current = next_day


def totals_for(entries, data_mgr, date_predicate):
    """(seconds, {currency: earnings}) for the part of every entry that
    falls on a day date_predicate(date) accepts."""
    seconds = 0.0
    earnings = defaultdict(float)
    for entry in entries:
        for day, segment in _day_segments(entry, data_mgr):
            if date_predicate(day):
                seconds += segment
                info = data_mgr.get_project_rate_info(entry.get("project", "Unknown"))
                if info["rate"] > 0:
                    earnings[info["currency"]] += (segment / 3600.0) * info["rate"]
    return seconds, dict(earnings)


def visible_entries(data_mgr):
    """What Reports count: closed entries not hidden out of exports."""
    return [e for e in data_mgr.closed_entries_sorted() if not e.get("hidden")]


def custom_range(data_mgr, start_date, end_date):
    """The Custom Range card for two ISO dates (inclusive), or an error."""
    if not start_date or not end_date:
        return None, "Pick both dates."
    if start_date > end_date:
        return None, "The 'From' date must be on or before the 'To' date."
    seconds, earnings = totals_for(visible_entries(data_mgr), data_mgr,
                                   lambda d: start_date <= d.isoformat() <= end_date)
    return {
        "custom_label": f"Custom range ({start_date} to {end_date})",
        "custom_seconds": seconds,
        "custom_earnings_by_currency": earnings,
    }, None


def _money_lines(earnings):
    return [f"{format_money(amount, currency)} earned" for currency, amount in sorted(earnings.items())]


def build(data_mgr, today=None, custom=None):
    entries = visible_entries(data_mgr)
    today = today or datetime.now().date()

    # Earnings stay split by currency - different projects can bill in
    # different currencies, and adding USD + EUR into one number is wrong.
    today_s, today_e = totals_for(entries, data_mgr, lambda d: d == today)
    week_start = today - timedelta(days=today.weekday())   # Monday-based
    week_s, week_e = totals_for(entries, data_mgr, lambda d: d >= week_start)
    month_start = today.replace(day=1)
    month_s, month_e = totals_for(entries, data_mgr, lambda d: d >= month_start)

    days = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    day_totals = dict.fromkeys(days, 0.0)
    for entry in entries:
        for day, segment in _day_segments(entry, data_mgr):
            key = day.isoformat()
            if key in day_totals:
                day_totals[key] += segment

    project_totals = defaultdict(float)
    for entry in entries:
        project_totals[entry.get("project", "Unknown")] += data_mgr.duration_seconds(entry)
    by_project = []
    for project, seconds in sorted(project_totals.items(), key=lambda kv: kv[1], reverse=True):
        info = data_mgr.get_project_rate_info(project)
        earned = (seconds / 3600.0) * info["rate"] if info["rate"] > 0 else 0.0
        by_project.append((project, seconds, earned, info["currency"]))

    data = {
        "today_label": "Today",
        "today_seconds": today_s,
        "today_earnings_by_currency": today_e,
        "week_label": "This week",
        "week_seconds": week_s,
        "week_earnings_by_currency": week_e,
        "month_label": "This month",
        "month_seconds": month_s,
        "month_earnings_by_currency": month_e,
        "daily": [("Today" if day == today.isoformat() else day[5:], day_totals[day]) for day in days],
        "by_project": by_project,
    }
    # A calculated Custom Range is part of what's shown, so it's part of
    # what Export writes (export_utils only adds the section when present).
    if custom:
        data.update(custom)
    return data


def goals(data_mgr, report):
    """[{label, hours, goal, fraction}] for each goal set above 0."""
    out = []
    for label, key, seconds_key in (("This week", "weekly_goal_hours", "week_seconds"),
                                    ("This month", "monthly_goal_hours", "month_seconds")):
        goal = float(data_mgr.settings.get(key, 0.0) or 0.0)
        if goal > 0:
            hours = report[seconds_key] / 3600.0
            out.append({"label": label, "hours": round(hours, 1), "goal": goal,
                        "fraction": min(1.0, hours / goal)})
    return out


def view(data_mgr, report):
    """The report as the web page draws it: formatted, never recomputed."""
    def tile(label, seconds, earnings):
        return {"label": label, "value": format_duration_words(seconds) if seconds else "0m",
                "empty": not seconds, "money": _money_lines(earnings)}

    today = datetime.now().date()
    daily = []
    for i, (label, seconds) in enumerate(report["daily"]):
        day = today - timedelta(days=i)
        daily.append({"label": label, "weekday": day.strftime("%a"), "seconds": seconds,
                      "text": format_duration_words(seconds) if seconds else ""})
    custom = None
    if "custom_seconds" in report:
        custom = tile(report["custom_label"], report["custom_seconds"], report["custom_earnings_by_currency"])
    return {
        "tiles": [tile("Today", report["today_seconds"], report["today_earnings_by_currency"]),
                  tile("This week", report["week_seconds"], report["week_earnings_by_currency"]),
                  tile("This month", report["month_seconds"], report["month_earnings_by_currency"])],
        "goals": goals(data_mgr, report),
        "daily": list(reversed(daily)),   # oldest first, left to right
        "projects": [{"name": p, "seconds": s, "text": format_duration_words(s),
                      "money": format_money(m, c, short=True) if m else ""}
                     for p, s, m, c in report["by_project"]],
        "custom": custom,
    }
