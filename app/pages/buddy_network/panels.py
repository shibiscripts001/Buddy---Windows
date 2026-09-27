"""What Buddy Network's windows show - your account, a DM's safety code,
avatars, saving a transfer file, the direct messages saved on this PC,
banning someone and the Admin panel - as plain data for the web page
(web/network.js draws each one as a modal). No Qt here, so it's tested
directly; the page (page.py) keeps which are open and does what they ask.

Everything people wrote - names, reports, reasons, announcements - goes out
as text, and the view puts it in with textContent, never as HTML.
"""

from __future__ import annotations

from datetime import datetime

from core.i18n import format_when

from . import avatars, render, web_view

RECOVERY_WARNING = (
    "Your recovery code is the only way back into this identity if this PC's settings are lost – "
    "there's no email or password to reset. Keep it somewhere private: anyone who has it can be you.")

BAN_LENGTHS = [("1 day", 1), ("7 days", 7), ("30 days", 30), ("Permanently", None)]
ROLES = {"mod": "Mod", "admin": "Admin"}
ANNOUNCEMENT_MAX = 1000

# The Admin panel's lists, by the server answer that fills each.
ADMIN_ANSWERS = {"reports": "reports", "bans": "bans", "admins": "admins", "admin_log": "log",
                 "app_announcements": "app"}
# The requests whose errors show in the Admin panel (not as a notice).
ADMIN_REQUESTS = ("list_reports", "resolve_report", "ban", "unban", "list_bans", "set_role", "list_admins",
                  "admin_log", "post_app_announcement", "delete_app_announcement", "list_app_announcements")


def when(ts) -> str:
    return format_when(datetime.fromtimestamp(ts), "%d %b %Y %H:%M", "datetime").lstrip("0") if ts else ""


def ban_length(until, network: bool) -> str:
    """Whole phrases, so each is translated as one."""
    if until is None:
        return "Permanent, and their network" if network else "Permanent"
    return f"Until {when(until)}, and their network" if network else f"Until {when(until)}"


# ------------------------------------------------------------- account

def account(me: dict, *, devices: int, code: str | None, saved_chats: int) -> dict:
    """code: the recovery code once they've pressed Show, else None."""
    named = bool(me.get("name"))
    return {
        "title": render.display_name(me) if named else "No name yet",
        "avatar": web_view.avatar_url(avatars.key_of(me)),
        "id": me.get("id", ""),
        "code": code,
        "recovery_warning": RECOVERY_WARNING,
        "encryption": (("Your direct messages are end-to-end encrypted for the 1 PC" if devices == 1 else
                        f"Your direct messages are end-to-end encrypted for the {devices:,} PCs") +
                       " signed in to this account. A PC that hasn't used Buddy Network for 60 days drops off. "
                       "A new PC can't read messages sent before it was set up, unless you move with a "
                       "transfer file (below)."),
        "saved_chats": ("1 conversation saved on this PC." if saved_chats == 1 else
                        f"{saved_chats:,} conversations saved on this PC." if saved_chats
                        else "None saved on this PC."),
    }


# --------------------------------------------------------- safety code

def safety(other: dict, code: str, verified: bool, changed: bool) -> dict:
    if verified:
        status, tone = "You've checked this code with them.", "success"
    elif changed:
        status, tone = ("Their keys (or yours) changed recently: a new PC, Buddy reinstalled – or someone "
                        "intercepting. Check the code before sharing anything private."), "warning"
    else:
        status, tone = "Not checked yet.", ""
    who = other.get("name") or "they"
    return {
        "title": f"Safety code – {render.display_name(other)}",
        "about": (f"Compare this with the code {who} sees for you – in person, on a call, or in another app "
                  "you trust. Not in this chat: someone in the middle could fake that too."),
        # Two lines of four groups, as the old window showed it.
        "lines": [code[:17], code[18:]] if code else [],
        "status": status, "tone": tone,
        "can_accept": bool(changed),
        "can_verify": bool(code) and not verified,
    }


# ------------------------------------------------------------- avatars

def avatar_panel(me: dict, saved: list[str], preview: str, error: str = "") -> dict:
    """preview: the seed on show ("" = the original, drawn from their ID)."""
    current = me.get("avatar") or ""
    key = lambda seed: seed or me.get("id", "")   # noqa: E731
    using = preview == current
    return {
        "preview": web_view.avatar_url(key(preview)),
        "using": using,
        "status": ("This is how everyone sees you." if using
                   else "Not in use yet – Use this to change it for everyone."),
        "can_keep": bool(preview) and preview not in saved and len(saved) < avatars.MAX_SAVED,
        "is_original": preview == "",
        "saved_label": f"Favourites ({len(saved)} of {avatars.MAX_SAVED})",
        "saved": [{"index": i, "url": web_view.avatar_url(key(seed)), "selected": seed == preview,
                   "in_use": seed == current} for i, seed in enumerate(saved)],
        "error": error,
    }


# ------------------------------------------------------------ transfer

def transfer_panel(saved_chats: int, error: str = "") -> dict:
    return {
        "chats_label": ("Include the direct messages saved on this PC (1 conversation)" if saved_chats == 1
                        else f"Include the direct messages saved on this PC ({saved_chats:,} conversations)"
                        if saved_chats else "Include saved direct messages (none are saved on this PC)"),
        "has_chats": bool(saved_chats),
        "error": error,
    }


# --------------------------------------------------------- saved chats

def saved_chats(chats: list[dict], note: str = "", tone: str = "") -> dict:
    rows = []
    for i, c in enumerate(chats):
        last = format_when(datetime.fromtimestamp(c["last"]), "%d %b %Y").lstrip("0") if c.get("last") else "-"
        count = "1 message" if c["count"] == 1 else f"{c['count']:,} messages"
        rows.append({"index": i, "name": render.display_name(c["other"]), "detail": f"{count}, last {last}"})
    return {"chats": rows, "note": note, "tone": tone}


# ----------------------------------------------------------------- ban

def ban_panel(person: dict) -> dict:
    return {"who": f"Ban {render.display_name(person)} from Buddy Network",
            "lengths": [label for label, _days in BAN_LENGTHS]}


def ban_values(payload: dict) -> tuple | None:
    """(days, reason, network) from the view's answer, or None if it's not
    one of the lengths offered."""
    index = payload.get("length")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(BAN_LENGTHS):
        return None
    reason = " ".join(str(payload.get("reason") or "").split())[:200]
    return BAN_LENGTHS[index][1], reason, bool(payload.get("network"))


# --------------------------------------------------------------- admin

def admin_tabs(role: str) -> list[dict]:
    tabs = [{"id": "reports", "label": "Reports"}, {"id": "bans", "label": "Bans"}]
    if role in ("admin", "owner"):
        tabs.append({"id": "admins", "label": "Staff"})
    if role == "owner":
        tabs += [{"id": "log", "label": "Log"}, {"id": "app", "label": "App"}]
    return tabs


def admin_requests(role: str) -> list[dict]:
    """What the Admin panel asks the server for when it opens."""
    asks = [{"type": "list_reports"}, {"type": "list_bans"}]
    if role in ("admin", "owner"):
        asks.append({"type": "list_admins"})
    if role == "owner":
        asks += [{"type": "admin_log"}, {"type": "list_app_announcements"}]
    return asks


def _report_row(r: dict) -> dict:
    times = f" ({r['times']} reports)" if r.get("times", 1) > 1 else ""
    return {
        "id": r["id"],
        "head": f"{render.display_name(r['reported'])} in {r['where']}, {when(r['created'])}{times}",
        "text": r.get("text", "") or ("(an image, no text)" if r.get("image") else ""),
        "claimed": bool(r.get("claimed")),
        "image": bool(r.get("image")),   # a room message's image, still on the server
        "by": f"Reported by {render.display_name(r['reporter'])}" + (f": {r['reason']}" if r.get("reason") else ""),
        "can_ban": bool((r.get("reported") or {}).get("id")),
    }


def admin(role: str, answers: dict, tab: str, error: str = "") -> dict:
    """answers: the server's latest answer for each list (ADMIN_ANSWERS),
    None until it arrives."""
    tabs = admin_tabs(role)
    if tab not in {t["id"] for t in tabs}:
        tab = "reports"
    reports, bans, staff, log, app = (answers.get(k) for k in ("reports", "bans", "admins", "log", "app"))
    owner = role == "owner"
    return {
        "tabs": tabs, "tab": tab, "error": error,
        "reports": None if reports is None else [_report_row(r) for r in reports.get("reports", [])],
        "bans": None if bans is None else [
            {"id": b["user"]["id"], "head": render.display_name(b["user"]),
             "lasts": ban_length(b["until"], bool(b.get("network"))), "reason": b.get("reason") or ""}
            for b in bans.get("bans", [])],
        "staff": None if staff is None else [
            {"id": a["id"], "head": render.display_name(a), "role": a["role"],
             "can_remove": a["role"] in (("mod", "admin") if owner else ("mod",))}
            for a in staff.get("admins", [])],
        "roles": [{"id": "mod", "label": "Mod"}] + ([{"id": "admin", "label": "Admin"}] if owner else []),
        "staff_hint": ("Mods and admins can delete any message, manage rooms, pin room announcements, ban and "
                       "unban, and handle reports. Bans only reach down: mods ban users, admins ban mods too. "
                       "Admins make and remove mods. "
                       + ("Only you make admins, ban an admin, read the log and post app announcements."
                          if owner else "Only the owner makes admins, reads the log and posts app announcements.")),
        "log": None if log is None else [
            {"head": f"{when(e['ts'])}  {render.display_name(e['actor'])}  {e['action']}"
             + (f"  {e['target'][:8]}" if e.get("target") else ""), "detail": e.get("detail") or ""}
            for e in log.get("entries", [])],
        "app": None if app is None else [
            {"id": a["id"], "head": f"{a['title']}  ({when(a['ts'])})", "text": a["text"]}
            for a in app.get("announcements", [])],
        "max_announcement": ANNOUNCEMENT_MAX,
    }


def find(answer: dict | None, key: str, value, field: str = "id"):
    """The entry of a server answer's list whose field is value - so the
    view can only act on what the server sent."""
    for entry in (answer or {}).get(key, []):
        if isinstance(entry, dict) and entry.get(field) == value:
            return entry
    return None


def role_problem(user_id: str) -> str:
    if len(user_id) != 32 or not user_id.isalnum():
        return "Paste their full Buddy Network ID (32 letters and numbers)."
    return ""


def announcement_problem(title: str, text: str) -> str:
    if not title or not text:
        return "An announcement needs a title and some text."
    if len(text) > ANNOUNCEMENT_MAX:
        return f"Announcements are at most {ANNOUNCEMENT_MAX:,} characters."
    return ""
