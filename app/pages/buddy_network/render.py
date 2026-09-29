"""The message list as HTML for the page's QTextBrowser - no Qt here.

Everything a user typed (names and text) goes through html.escape, so a
message can never add formatting, hidden links or images (an image would
be fetched from its server, telling that server the reader's IP address).
The only images are the generated avatars (avatars.py): "bn-avatar:<id>",
or whatever `avatar_url` makes of the id (the web page passes inline SVG
data: URLs) - nothing is ever fetched.

Every part carries a class ("msg", "head", "acts", "body", "quote",
"note", "day", "top") so the web page's stylesheet can lay it out; the
colours stay inline, from `colors`.

Links are anchors to "bn-link:<n>", an index into the `links` list the
caller passes in, never the URL itself: the page looks the URL up and
always shows the link warning first. Other anchors: "bn-delete:<id>",
"bn-more" (load earlier messages), "bn-user:<id>" (someone's name: the
add-buddy / block menu), "bn-report:<id>", "bn-reply:<id>", "bn-edit:<id>"
and, for the owner only, "bn-purge:<id>" (delete forever - no "message
deleted" left behind; offered on deleted messages too). "bn-react:<id>"
opens the emoticon picker, and "bn-reaction:<id>:<key>" is one of the
reactions under a message (reactions.py) - clicked, it adds or takes away yours.

The actions on a message each have their own shade - "reply", "edit",
"delete", "report" and "purge" in `colors` (the page mixes them from the
theme: accent, green, amber, secondary, red), falling back to "muted".

Every message shows the date and time it was sent ("25 Sep, 14:32"; the
year too if it isn't this one), under the day headings.

The MOD / ADMIN / OWNER badge comes only from the role the server sends
with each author - nothing a user types can produce one (and names that
look like "Admin" or "Mod" are refused by the server).

Messages from people in `hidden` (the ones you've blocked) are left out.
A DM this PC couldn't decrypt ("unreadable", see e2e.py) says so instead
of its text; one from a PC of theirs not accepted yet ("unverified") has a
warning under it; a copy of an earlier one ("replayed") isn't shown. A DM's
reply quote only ever comes from messages this PC decrypted itself - never
text or a name the server attached. "@Name#tag" mentions (mentions.py)
stand out, and a message that mentions you gets a tinted background.

A message's image is a placeholder sized to it ("shot", data-image=<id>):
the page fills it in once attachments.py has fetched, checked and (in a DM)
decrypted it. One that has expired says so. A GIF from GIF search says
it's from GIPHY, and whose it is (the server's "credit"; GIPHY's terms ask
for it) - the name escaped like anything else a person typed.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from core.i18n import format_when

from . import avatars, images, mentions, reactions, safety

LINK_NOTE = "Contains a link – only open links from people you trust."
UNREADABLE_NOTE = ("This message can't be read on this PC – it was encrypted before Buddy Network "
                   "was set up here, or for a key this PC no longer has.")
UNVERIFIED_NOTE = ("Sent from a PC this chat hasn't accepted yet – check the safety code before "
                   "trusting it.")
REPLAYED_NOTE = "A repeat of an earlier message, sent again by the server – not shown."
AVATAR_PX = 18
QUOTE_CHARS = 90
_IMAGE_ID = re.compile(r"[0-9a-f]{32}")


DELETED_USER = "Deleted user"
BADGES = {"mod": "MOD", "admin": "ADMIN", "owner": "OWNER"}


def display_name(author: dict) -> str:
    if not author.get("id"):
        return DELETED_USER   # the author deleted their account
    return f"{author.get('name') or 'Someone'} #{author.get('tag', '')}"


def _day_label(ts: float, now: float) -> str:
    day = datetime.fromtimestamp(ts).date()
    today = datetime.fromtimestamp(now).date()
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    return format_when(day, "%A %d %B %Y", "long").replace(" 0", " ")


def stamp(ts: float, now: float) -> str:
    """When a message was sent: "25 Sep, 14:32", with the year if it's not
    this year's ("3 Mar 2025, 09:05")."""
    sent = datetime.fromtimestamp(ts)
    this_year = sent.year == datetime.fromtimestamp(now).year
    return format_when(sent, "%d %b, %H:%M" if this_year else "%d %b %Y, %H:%M", "datetime").lstrip("0")


def _plain_html(text: str, c: dict) -> str:
    """Escaped text with its @mentions picked out."""
    out, pos = [], 0
    for start, end, name, tag in mentions.find(text):
        out.append(html.escape(text[pos:start]))
        out.append(f'<span style="color:{c["link"]}; font-weight:600">@{html.escape(name)}</span>'
                   f'<span style="color:{c["muted"]}">#{html.escape(tag)}</span>')
        pos = end
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _text_html(text: str, links: list[str], c: dict) -> str:
    out, pos = [], 0
    for start, end, url in safety.find_links(text):
        out.append(_plain_html(text[pos:start], c))
        links.append(url)
        out.append(f'<a href="bn-link:{len(links) - 1}" style="color:{c["link"]}">{html.escape(url)}</a>')
        pos = end
    out.append(_plain_html(text[pos:], c))
    return "".join(out)


def quote_text(reply: dict, lookup: dict) -> str:
    """What a reply shows of the message it answers. A DM's quote comes
    without text (it's encrypted), so it's looked up among loaded messages."""
    if reply.get("gone"):
        return "an earlier message (no longer on the server)"
    if reply.get("deleted"):
        return "a deleted message"
    original = lookup.get(reply.get("id")) or {}
    text = reply.get("text") or original.get("text") or ""
    text = " ".join(text.split())
    if not text:
        return "an image" if reply.get("image") or original.get("image") else "an earlier message"
    return text[:QUOTE_CHARS] + ("…" if len(text) > QUOTE_CHARS else "")


def _quote_html(reply: dict, lookup: dict, hidden, c: dict) -> str:
    author = reply.get("author") or (lookup.get(reply.get("id")) or {}).get("author") or {}
    if author.get("id") in hidden:
        what, who = "a message from someone you've blocked", ""
    else:
        what = quote_text(reply, lookup)
        who = f"{html.escape(author.get('name') or 'Someone')}: " if author.get("id") else ""
    return (f'<div class="quote" translate="no" style="color:{c["muted"]}; margin-left:10px"><i>&#8618; {who}'
            f'{html.escape(what)}</i></div>')


def _image_html(m: dict, c: dict, image_days: int) -> str:
    image = m.get("image")
    if not isinstance(image, dict) or not _IMAGE_ID.fullmatch(str(image.get("id", ""))):
        return ""
    if image.get("gone"):
        return (f'<div class="shot-gone" style="color:{c["muted"]}"><i>Image expired – images are kept for '
                f'{int(image_days)} days.</i></div>')
    try:
        w, h = images.shown_size(int(image.get("w") or 1), int(image.get("h") or 1))
    except (TypeError, ValueError):
        w, h = images.shown_size(1, 1)
    shot = (f'<div class="shot" data-image="{image["id"]}" style="width:{w}px; height:{h}px" '
            f'title="Click to see it larger"><img alt="Image" hidden></div>')
    credit = image.get("credit")
    if isinstance(credit, dict) and credit.get("source") == "giphy":
        user = str(credit.get("user") or "")[:40]
        by = f' · <span translate="no">{html.escape(user)}</span>' if user else ""
        shot += f'<div class="shot-credit" style="color:{c["muted"]}"><span>GIF via GIPHY</span>{by}</div>'
    return shot


def _reactions_html(m: dict, can_react: bool, hidden) -> str:
    """The emoticons under a message, each with how many used it; yours
    stand out ("mine"). Clicking one ("bn-reaction:<id>:<key>") adds or
    takes away yours. Nobody you've blocked is counted or named."""
    chips = []
    for r in reactions.known(m.get("reactions")):
        people = [p for p in r["people"] if p["id"] not in hidden]
        count = r["count"] - (len(r["people"]) - len(people))
        if count <= 0:
            continue
        names = ", ".join(display_name(p) for p in people)
        if count > len(people):
            names += f" +{count - len(people)}"
        cls = "react mine" if r["mine"] else "react"
        inner = f'{html.escape(reactions.TEXT[r["r"]])}<span class="n">{count}</span>'
        if can_react:
            chips.append(f'<a class="{cls}" href="bn-reaction:{int(m["id"])}:{r["r"]}" '
                         f'title="{html.escape(names)}">{inner}</a>')
        else:
            chips.append(f'<span class="{cls}" title="{html.escape(names)}">{inner}</span>')
    return f'<div class="reacts" translate="no">{"".join(chips)}</div>' if chips else ""


def room_html(messages: list[dict], *, my_id: str, room_name: str, more: bool, links: list[str],
              colors: dict, now: float, history_days: int = 30, hidden=frozenset(),
              admin: bool = False, more_saved: bool = False, saved_copy: bool = False,
              me: dict | None = None, can_reply: bool = False, top_note: str = "",
              lookup: dict | None = None, owner: bool = False, avatar_url=None, image_days: int = 7) -> str:
    """colors: "text", "muted", "me", "other", "link", "warning", "badge",
    "mention" (the background behind a message that mentions you).
    admin: the reader is staff - every message gets a delete link.
    more_saved: older messages are in this PC's saved copy (archive.py),
    not on the server; saved_copy: this conversation is being saved.
    me: the reader ({"name", "tag"}), for spotting mentions of them.
    can_reply: show reply links. top_note: replaces the line at the top
    (a search says how many matched). lookup: id -> message, for quotes.
    owner: the reader is the owner - every message gets "delete forever".
    avatar_url: key -> the image URL for that avatar (default
    "bn-avatar:<key>"); it's only ever given the key, never user text.
    image_days: how long the server keeps images (for an expired one's note)."""
    c = {k: html.escape(v) for k, v in colors.items()}
    history_days = int(history_days)
    c.setdefault("mention", c["muted"])
    lookup = lookup if lookup is not None else {m["id"]: m for m in messages}
    avatar_url = avatar_url or (lambda key: f"bn-avatar:{key}")
    parts = [f'<div class="chat" style="color:{c["text"]}">']
    if top_note:
        parts.append(f'<p class="top" align="center" style="color:{c["muted"]}">{html.escape(top_note)}</p>')
    elif more or more_saved:
        label = "Load earlier messages (saved on this PC)" if more_saved and not more else "Load earlier messages"
        parts.append(f'<p class="top" align="center"><a class="more" href="bn-more" '
                     f'style="color:{c["link"]}">{label}</a></p>')
    else:
        start = (f"Start of {room_name} – the server keeps messages for {history_days} days, and a copy is "
                 "saved on this PC." if saved_copy else
                 f"Start of {room_name} – messages are kept for {history_days} days.")
        parts.append(f'<p class="top" align="center" style="color:{c["muted"]}">{html.escape(start)}</p>')
    last_day = None
    for m in messages:
        if m["author"].get("id") in hidden:
            continue
        day = _day_label(m["ts"], now)
        if day != last_day:
            parts.append(f'<p class="day" align="center" style="color:{c["muted"]}; margin-top:10px">{day}</p>')
            last_day = day
        author = m["author"]
        mine = author.get("id") == my_id
        when = stamp(m["ts"], now)
        name_color = c["me"] if mine else c["other"]
        name = html.escape(author.get("name") or "Someone")
        head = ""
        if author.get("id"):
            head = (f'<img class="avatar" src="{html.escape(avatar_url(avatars.key_of(author)))}" '
                    f'width="{AVATAR_PX}" height="{AVATAR_PX}" alt=""> ')
        if not author.get("id"):
            head += f'<span style="color:{c["muted"]}; font-weight:600">{DELETED_USER}</span>'
        elif mine:
            head += f'<span translate="no" style="color:{name_color}; font-weight:600">{name}</span>'
        else:
            head += (f'<a class="who" translate="no" href="bn-user:{html.escape(author["id"])}" style="color:{name_color}; '
                     f'font-weight:600; text-decoration:none">{name}</a>')
        if author.get("id"):
            head += f'<span translate="no" style="color:{c["muted"]}"> #{html.escape(author.get("tag", ""))}</span>'
        badge = BADGES.get(author.get("role", "")) if author.get("id") else None
        if badge:
            head += (f' <span class="badge" style="color:{c.get("badge", c["me"])}; font-size:small; '
                     f'font-weight:700">{badge}</span>')
        head += f'<span class="when" style="color:{c["muted"]}"> &middot; {when}</span>'
        if m.get("edited") and not m["deleted"]:
            head += f'<span class="when" style="color:{c["muted"]}"> (edited)</span>'
        def action(kind: str, label: str) -> str:
            # Each action its own shade (ACTION_COLORS), so "delete" and
            # "delete forever" can't be mistaken for "reply" and "edit".
            return (f' <a class="act" href="bn-{kind}:{int(m["id"])}" style="color:{c.get(kind, c["muted"])}; '
                    f'text-decoration:none">{label}</a>')

        acts = ""
        if not m["deleted"]:
            readable = not m.get("unreadable")
            if can_reply and readable:
                acts += action("reply", "reply")
            if can_reply and readable and not m.get("saved_only"):
                acts += action("react", "react")
            if mine and readable and not m.get("saved_only"):
                acts += action("edit", "edit")
            if (mine or admin) and not m.get("saved_only"):
                acts += action("delete", "delete")
            if not mine and readable and not m.get("saved_only"):
                acts += action("report", "report")
        if owner and not m.get("saved_only"):
            acts += action("purge", "delete forever")
        if acts:
            head += f'<span class="acts">{acts}</span>'
        reply = m.get("reply")
        if isinstance(reply, dict) and str(m.get("room", "")).startswith("dm-"):
            # Encrypted: who said what comes only from what this PC decrypted.
            reply = {k: reply[k] for k in ("id", "gone", "deleted", "image") if k in reply}
        quote = _quote_html(reply, lookup, hidden, c) if isinstance(reply, dict) and not m["deleted"] else ""
        if m["deleted"]:
            body = f'<div class="body gone" style="color:{c["muted"]}"><i>message deleted</i></div>'
        elif m.get("replayed"):
            body = f'<div class="body note" style="color:{c["warning"]}"><i>&#9888; {REPLAYED_NOTE}</i></div>'
        elif m.get("unreadable"):
            body = f'<div class="body gone" style="color:{c["muted"]}"><i>&#128274; {UNREADABLE_NOTE}</i></div>'
        else:
            body = (f'<div class="body" translate="no" style="white-space:pre-wrap">{_text_html(m["text"], links, c)}</div>'
                    if m["text"] or not m.get("image") else "")
            body += _image_html(m, c, image_days)
            if safety.contains_link(m["text"]):
                body += f'<div class="note" style="color:{c["warning"]}">&#9888; {LINK_NOTE}</div>'
            if m.get("unverified"):
                body += f'<div class="note" style="color:{c["warning"]}">&#9888; {UNVERIFIED_NOTE}</div>'
            body += _reactions_html(m, can_reply and not m.get("saved_only"), hidden)
        mentioned = not mine and not m["deleted"] and mentions.mentions_me(m.get("text", ""), me)
        tint = f' background-color:{c["mention"]};' if mentioned else ""
        classes = "msg" + (" mine" if mine else "") + (" mentioned" if mentioned else "")
        parts.append(f'<div class="{classes}" data-id="{int(m["id"])}" style="margin-top:6px;{tint}">'
                     f'<div class="head">{head}</div>{quote}{body}</div>')
    parts.append("</div>")
    return "".join(parts)
