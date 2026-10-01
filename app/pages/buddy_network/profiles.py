"""Profile pages and the who's-here list, as plain data for the web page -
no Qt here. server/profiles.py keeps them; profile_page.py asks for them
and does what the page's buttons say.

A profile is old-MySpace style: a headline, a mood, About me, what they're
working on and listening to, their top buddies, when they joined and how
often their page has been viewed - drawn in the theme its person picked.
Moods and themes are fixed lists, the same keys as the server's (tests
check), so a profile's look is only ever one of Buddy's own and never
anything a person sent. What people wrote goes out as text: the view puts
it in with textContent and marks it translate="no".
"""

from __future__ import annotations

from datetime import datetime

from core.i18n import format_when

from . import avatars, render, web_view

# (key, what it's called, its emoticon) - server/profiles.py MOODS.
MOODS = (
    ("happy", "happy", ":)"),
    ("chill", "chill", "8)"),
    ("busy", "busy", ":-|"),
    ("creative", "creative", "*_*"),
    ("tired", "tired", "-_-"),
    ("excited", "excited", ":D"),
    ("focused", "focused", "o_o"),
    ("silly", "silly", ":P"),
    ("grumpy", "grumpy", ">:("),
    ("inspired", "inspired", "\\o/"),
    ("rendering", "rendering", "[==> ]"),
    ("caffeinated", "caffeinated", "c[_]"),
)
MOOD_NAMES = {key: name for key, name, _face in MOODS}
MOOD_FACES = {key: face for key, _name, face in MOODS}

# (key, name) - server/profiles.py PROFILE_THEMES; web/network.css draws each.
THEMES = (
    ("classic", "Classic"),
    ("midnight", "Midnight"),
    ("bubblegum", "Bubblegum"),
    ("hacker", "Hacker"),
    ("sunset", "Sunset"),
    ("ocean", "Ocean"),
    ("forest", "Forest"),
    ("glitter", "Glitter"),
    ("notebook", "Notebook"),
    ("retro", "Retro"),
)
THEME_KEYS = {key for key, _name in THEMES}
DEFAULT_THEME = "classic"

# server/profiles.py's limits.
HEADLINE_MAX = 100
ABOUT_MAX = 1000
LINE_MAX = 100
TOP_MAX = 8

EMPTY = {"headline": "", "mood": "", "about": "", "working_on": "", "listening": "", "theme": ""}


def _text(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def fields_of(data: dict | None) -> dict:
    """A profile's own fields as the server sent them, each one checked."""
    raw = (data or {}).get("profile") if isinstance((data or {}).get("profile"), dict) else {}
    out = {
        "headline": _text(raw.get("headline"), HEADLINE_MAX),
        "about": _text(raw.get("about"), ABOUT_MAX),
        "working_on": _text(raw.get("working_on"), LINE_MAX),
        "listening": _text(raw.get("listening"), LINE_MAX),
        "mood": raw.get("mood") if raw.get("mood") in MOOD_NAMES else "",
        "theme": raw.get("theme") if raw.get("theme") in THEME_KEYS else "",
    }
    return out


def since_label(ts) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        return ""
    return f"Member since {format_when(datetime.fromtimestamp(ts), '%B %Y', 'date')}"


def _count(n) -> int:
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0


def person_view(person: dict) -> dict:
    return {"id": person.get("id", ""), "name": person.get("name") or "Someone", "tag": person.get("tag", ""),
            "label": render.display_name(person), "avatar": web_view.avatar_url(avatars.key_of(person)),
            "badge": render.BADGES.get(person.get("role", ""), "")}


def profile_view(p: dict, *, me_id: str, actions: list[dict], buddies: list[dict]) -> dict:
    """The profile window: p is its panel state - {"user", "data" (the
    server's answer, None while it's on its way), "editing", "draft",
    "error"}. actions: [{"id", "label", "kind"}] for its Contacting box.
    buddies: yours, to pick top buddies from while editing your own."""
    data = p.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
        return {"loading": True, "error": p.get("error", ""), "user": {"id": p.get("user", "")}}
    user = data["user"]
    fields = fields_of(data)
    mine = user.get("id") == me_id
    name = user.get("name") or "Someone"
    views = _count(data.get("views"))
    buddy_count = _count(data.get("buddy_count"))
    online = data.get("online")
    view = {
        "loading": False,
        "user": person_view(user),
        "mine": mine,
        "theme": fields["theme"] or DEFAULT_THEME,
        "headline": fields["headline"],
        # The name (one of Buddy's words, translated) and its emoticon apart.
        "mood": ({"name": MOOD_NAMES[fields["mood"]], "face": MOOD_FACES[fields["mood"]]} if fields["mood"]
                 else None),
        "about": fields["about"],
        "working_on": fields["working_on"],
        "listening": fields["listening"],
        "since": since_label(data.get("since")),
        "views": "1 profile view" if views == 1 else f"{views:,} profile views",
        "online": None if online is None else bool(online),
        "network": ("This is your profile – this is how everyone sees it." if mine
                    else f"{name} is in your extended network."),
        "blurbs": f"{name}'s Blurbs",
        "contacting": f"Contacting {name}",
        "top_title": f"{name}'s Top Buddies",
        "top_note": ("1 buddy" if buddy_count == 1 else f"{buddy_count:,} buddies"),
        "top": [person_view(t) for t in data.get("top") or [] if isinstance(t, dict) and t.get("id")][:TOP_MAX],
        "actions": actions,
        "editing": bool(p.get("editing")) and mine,
        "error": p.get("error", ""),
    }
    if view["editing"]:
        draft = p.get("draft") or fields
        chosen = [i for i in (draft.get("top") or []) if isinstance(i, str)]
        view["draft"] = {k: draft.get(k, "") for k in EMPTY} | {"top": chosen}
        view["moods"] = [{"id": "", "label": "No mood", "face": ""}] + [
            {"id": key, "label": name_, "face": face} for key, name_, face in MOODS]
        view["themes"] = [{"id": key, "label": label} for key, label in THEMES]
        view["buddies"] = [person_view(b) for b in buddies]
        view["limits"] = {"headline": HEADLINE_MAX, "about": ABOUT_MAX, "line": LINE_MAX, "top": TOP_MAX}
    return view


def draft_from(payload: dict, buddy_ids) -> tuple[dict, str]:
    """What the edit form sent -> (the fields to save, a problem or "")."""
    def line(key, limit):
        value = payload.get(key)
        return " ".join(value.split())[:limit] if isinstance(value, str) else ""
    about = payload.get("about") if isinstance(payload.get("about"), str) else ""
    top = [i for i in dict.fromkeys(payload.get("top") or []) if isinstance(i, str) and i in set(buddy_ids)]
    draft = {"headline": line("headline", HEADLINE_MAX + 1), "about": about.strip(),
             "working_on": line("working_on", LINE_MAX + 1), "listening": line("listening", LINE_MAX + 1),
             "mood": payload.get("mood") if payload.get("mood") in MOOD_NAMES else "",
             "theme": payload.get("theme") if payload.get("theme") in THEME_KEYS else "", "top": top}
    if len(draft["headline"]) > HEADLINE_MAX:
        return draft, f"Headlines are at most {HEADLINE_MAX} characters."
    if len(draft["about"]) > ABOUT_MAX:
        return draft, f"About me is at most {ABOUT_MAX:,} characters."
    if len(draft["working_on"]) > LINE_MAX or len(draft["listening"]) > LINE_MAX:
        return draft, f"Those lines are at most {LINE_MAX} characters."
    if len(top) > TOP_MAX:
        return draft, f"Pick up to {TOP_MAX} top buddies."
    return draft, ""


def who_view(msg: dict | None, *, hidden=frozenset(), me_id: str = "") -> dict:
    """The who's-here list for the room on screen (the server's "who"):
    everyone in it, less anyone you've blocked."""
    people = [p for p in (msg or {}).get("people") or [] if isinstance(p, dict) and isinstance(p.get("id"), str)
              and p["id"] not in hidden]
    total = _count((msg or {}).get("total"))
    shown = [dict(person_view(p), me=p["id"] == me_id) for p in people]
    # Staff first, then everyone by name - the server sends them by name.
    shown.sort(key=lambda p: (not p["badge"], p["name"].casefold()))
    extra = max(0, total - len(people) - sum(1 for p in (msg or {}).get("people") or []
                                              if isinstance(p, dict) and p.get("id") in hidden))
    return {"people": shown, "count": (f"{len(shown):,} here" if not extra else f"{len(shown) + extra:,} here")}
