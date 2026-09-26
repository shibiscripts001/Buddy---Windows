"""A chat written out as a file the user chose to save - plain text or a
self-contained web page. No Qt here.

Readable by anyone who has the file (the page warns before exporting a
DM). Everything a user typed is escaped in the web page, and links are
left as plain text: opening one from the file would skip Buddy's link
warning.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
from datetime import datetime

from .render import display_name

UNREADABLE = "(couldn't be read on this PC)"
DELETED = "(message deleted)"
UNVERIFIED = "(sent from a PC this chat hadn't accepted)"
IMAGE = "[image]"


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _body(m: dict) -> str:
    if m.get("deleted"):
        return DELETED
    if m.get("unreadable"):
        return UNREADABLE
    text = m.get("text", "")
    if isinstance(m.get("image"), dict):   # the picture itself isn't exported
        text = f"{text} {IMAGE}".strip()
    if m.get("unverified"):
        return f"{text} {UNVERIFIED}"
    return text


def default_filename(title: str, now: float, extension: str = ".txt") -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title.lstrip("#@").strip()) or "chat"
    return f"Buddy Network - {safe[:60]} - {datetime.fromtimestamp(now):%Y-%m-%d}{extension}"


def as_text(title: str, messages: list[dict], now: float, note: str = "") -> str:
    lines = [f"Buddy Network – {title}", f"Exported {_when(now)} – {len(messages):,} messages"]
    if note:
        lines.append(note)
    lines.append("")
    for m in messages:
        body = _body(m).replace("\r", "").split("\n")
        if _replying(m):
            lines.append(f"    ({_replying(m)})")
        lines.append(f"[{_when(m['ts'])}] {display_name(m['author'])}{_edited(m)}: {body[0]}")
        lines.extend(f"    {line}" for line in body[1:])
    return "\n".join(lines) + "\n"


def _edited(m: dict) -> str:
    return " (edited)" if m.get("edited") and not m.get("deleted") else ""


def _replying(m: dict) -> str:
    reply = m.get("reply")
    if not isinstance(reply, dict) or m.get("deleted"):
        return ""
    # A DM's quote carries no name from the server worth trusting: it's encrypted.
    who = None if str(m.get("room", "")).startswith("dm-") else (reply.get("author") or {}).get("name")
    return f"replying to {who}" if who else "replying to an earlier message"


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg: #ffffff; --text: #1d1b20; --muted: #6b6770; --line: #e6e1e5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #1c1b1f; --text: #e6e1e5; --muted: #938f99; --line: #3a3740; }} }}
body {{ background: var(--bg); color: var(--text); font: 15px/1.45 system-ui, "Segoe UI", sans-serif;
        max-width: 760px; margin: 0 auto; padding: 24px 16px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
.meta {{ color: var(--muted); margin: 0 0 20px; }}
.m {{ border-top: 1px solid var(--line); padding: 8px 0; }}
.who {{ font-weight: 600; }}
.when, .gone {{ color: var(--muted); }}
.text {{ white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 2px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta">{meta}</p>
{messages}
</body>
</html>
"""


def as_html(title: str, messages: list[dict], now: float, note: str = "") -> str:
    parts = []
    for m in messages:
        gone = m.get("deleted") or m.get("unreadable")
        text = f'<div class="text{" gone" if gone else ""}">{html.escape(_body(m))}</div>'
        reply = f'<div class="when">&#8618; {html.escape(_replying(m))}</div>' if _replying(m) else ""
        parts.append(f'<div class="m">{reply}<span class="who">{html.escape(display_name(m["author"]))}</span> '
                     f'<span class="when">{_when(m["ts"])}{html.escape(_edited(m))}</span>{text}</div>')
    meta = f"Exported {_when(now)} – {len(messages):,} messages" + (f". {note}" if note else "")
    return _PAGE.format(title=html.escape(f"Buddy Network – {title}"), meta=html.escape(meta),
                        messages="\n".join(parts))


def write(path: str, text: str):
    """All or nothing: a failed save never leaves half a file."""
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".buddy-export-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
