#!/usr/bin/env python3
"""
Ask Buddy's conversations as data - what was said, what the model will be
re-sent, and how an answer's markdown becomes HTML. No Qt, so the page's
behaviour (history trimming, New/Previous chat, export, rendering) is
tested on plain Python (tests/test_manual_chat_conversation.py), and the
web view only ever draws what this holds.
"""

from __future__ import annotations

import html
import re

from . import actions

WELCOME = (
    "Ask about DaVinci Resolve and I'll answer from the reference manual, "
    "with chapter and page citations. If Resolve is running I can also read "
    "your open project to answer about your actual settings."
)

# Typed into the chat box, answered locally. Deliberately NOT sent to the
# model: an LLM asked what it can do will cheerfully invent an action that
# does not exist, and this list is built from the registry so it cannot.
HELP_WORDS = {"help", "/help", "?", "/?", "what can you do", "what can you do?"}

# Shown under the welcome of an empty chat; clicking one sends it.
SUGGESTIONS = [
    "How do I add a Power Window on the Color page?",
    "Check my timeline settings for problems",
    "What's the difference between Fusion and the Edit page titles?",
    "What can you do?",   # one of HELP_WORDS: lists what Buddy can do
]

YOU, BUDDY, ERROR = "You", "Buddy", "Error"


def is_help(question: str) -> bool:
    return question.lower().strip().strip("!.") in HELP_WORDS


def help_text(writes_on: bool) -> str:
    """What Buddy can do, built from the registries rather than written
    out - a hand-maintained list would start lying the first time an
    action was added."""
    additive, destructive = actions.help_lines()

    lines = [
        "**Ask me about DaVinci Resolve.** I answer from the official "
        "Resolve 21 Reference Manual and cite the chapter and page, so "
        "you can check me.",
        "",
        "**I can read your open project** (Resolve has to be running):",
        "- your project and timeline settings, and whether you are on "
        "Studio or the free version",
        "- what is on your timeline, and any clips whose frame rate or "
        "resolution does not match it",
        "- your timeline markers",
        "- what every other Buddy tool does, and I can offer you one",
        "",
    ]

    if writes_on:
        lines.append(
            "**I can change your project** – this is ON. I never change "
            "anything directly: I show you a card listing exactly what "
            "would happen and you press Apply."
        )
        lines.append("")
        lines.append("Safe to undo, nothing already in place moves:")
        lines.extend(f"- {line}" for line in additive)
        lines.append("")
        lines.append(
            "These move or destroy work you did not name, and the card "
            "says so in red:"
        )
        lines.extend(f"- {line}" for line in destructive)
    else:
        lines.append(
            "**I cannot change your project** – this is OFF, which is "
            "the default. Turn it on in Settings if you want it; you "
            "will be asked to type a sentence confirming you understand "
            "the risk. Even then I only ever propose a change and wait "
            "for you to press Apply."
        )

    lines += [
        "",
        "**Buttons:** New chat starts a fresh conversation; the arrows "
        "go back and forward between conversations. Nothing is lost "
        "either way while Buddy stays open.",
        "",
        "Type `help` any time to see this again.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------- markdown --

_CITATION = re.compile(r"\((Chapter \d+[^)]*)\)")
_SOURCE = re.compile(r"\((general knowledge|their project)\)")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
# Runs on ESCAPED text, so it must stop at an escaped quote or bracket too.
_BARE_URL = re.compile(r"\bhttps?://(?:(?!&quot;|&#x27;|&lt;|&gt;)[^\s<>\"')\]])+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_HEADING = re.compile(r"^\s*(#{1,4})\s+(.*)$")


def _bare_link(keep):
    def link(m):
        # A sentence's full stop is not part of the address.
        url = m.group(0)
        tail = len(url) - len(url.rstrip(".,;:!?"))
        url, rest = url[:len(url) - tail], url[len(url) - tail:]
        return keep(f'<a href="{url}">{url}</a>') + rest
    return link


def _inline(text: str) -> str:
    """One line of prose -> HTML. Escaped FIRST, so nothing the model writes
    can become markup; the only tags that come out are the ones added here,
    and the only links are http(s)."""
    parts = re.split(r"(`[^`\n]+`)", text)
    out = []
    for part in parts:
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        s = html.escape(part, quote=True)
        links = []

        def keep(markup):
            links.append(markup)
            return f"\x00{len(links) - 1}\x00"

        s = _MD_LINK.sub(lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'), s)
        s = _BARE_URL.sub(_bare_link(keep), s)
        s = _BOLD.sub(r"<b>\1</b>", s)
        s = _ITALIC.sub(r"<i>\1</i>", s)
        s = _CITATION.sub(r'<span class="cite">(\1)</span>', s)
        s = _SOURCE.sub(r'<span class="source">(\1)</span>', s)
        s = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], s)
        out.append(s)
    return "".join(out)


def md_to_html(text: str) -> str:
    """Just enough markdown for what the system prompt asks the model to
    produce: paragraphs, bullet and numbered lists, small headings, code
    blocks, bold/italic, inline code, links, and the manual's citations."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    list_kind = None
    code: list[str] | None = None

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    def close_list():
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = None

    for line in lines:
        if code is not None:
            if line.strip().startswith("```"):
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush_para()
            close_list()
            code = []
            continue
        if not line.strip():
            flush_para()
            close_list()
            continue
        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if heading:
            flush_para()
            close_list()
            out.append(f"<h4>{_inline(heading.group(2))}</h4>")
        elif bullet or numbered:
            flush_para()
            kind = "ul" if bullet else "ol"
            if list_kind != kind:
                close_list()
                start = ""
                if numbered and numbered.group(1) != "1":
                    start = f' start="{int(numbered.group(1))}"'
                out.append(f"<{kind}{start}>")
                list_kind = kind
            body = bullet.group(1) if bullet else numbered.group(2)
            out.append(f"<li>{_inline(body)}</li>")
        else:
            close_list()
            para.append(line)
    if code is not None:   # an unclosed fence still shows its contents
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush_para()
    close_list()
    return "".join(out)


def md_to_plain(text: str) -> str:
    """Minimal markdown cleanup for exported text - enough to keep the
    transcript readable in a .txt file without turning it into markdown
    soup. Citations are dropped; bold markers go; nothing else matters."""
    text = _CITATION.sub("", text)
    text = _SOURCE.sub("", text)
    return _BOLD.sub(r"\1", text).strip()


# -------------------------------------------------------- conversations --

class _Chat:
    def __init__(self):
        # What the model is re-sent: prose only, user/assistant pairs.
        self.history: list[dict] = []
        # What is on screen, oldest first. The only source the view is
        # drawn from, and what Export writes.
        self.blocks: list[dict] = []


class ChatSessions:
    """Every conversation this session, oldest first. The live one is
    `current`; switching never discards anything while Buddy is open."""

    def __init__(self):
        self.chats = [_Chat()]
        self.index = 0
        self.add(BUDDY, WELCOME)

    @property
    def current(self) -> _Chat:
        return self.chats[self.index]

    @property
    def blocks(self) -> list[dict]:
        return self.current.blocks

    @property
    def history(self) -> list[dict]:
        return self.current.history

    @property
    def turns(self) -> int:
        """One turn is a question plus its answer."""
        return len(self.current.history) // 2

    def add(self, who, body, trace=None, error=False, copyable=False, images=None) -> dict:
        block = {
            "who": who,
            "body": body,
            "trace": list(trace or []),
            "error": bool(error),
            "copyable": bool(copyable),
            "images": list(images or []),   # small data: URLs of the pictures sent with it
        }
        self.current.blocks.append(block)
        return block

    def record_turn(self, question, answer, limit, pictures=0):
        """Only the prose goes into history - replaying tool traffic would
        grow the context every turn for no benefit. Pictures aren't sent
        again either (every later turn would pay for them): the question
        just says there were some, and the answer says what they showed."""
        if pictures:
            note = "a picture" if pictures == 1 else f"{pictures} pictures"
            question = f"{question}\n\n[I attached {note} with this question.]"
        self.current.history.append({"role": "user", "content": question})
        self.current.history.append({"role": "assistant", "content": answer})
        self.trim(limit)

    def trim(self, limit):
        """Keep only the most recent `limit` turns. Oldest first out, and
        always in whole pairs - half a turn (a question with no answer, or
        an answer with no question) reads as a non-sequitur to the model."""
        keep = max(0, int(limit)) * 2
        history = self.current.history
        if keep == 0:
            history.clear()
        elif len(history) > keep:
            del history[:-keep]

    def is_empty(self) -> bool:
        """Nothing but the welcome - no question asked yet."""
        return not self.current.history and len(self.current.blocks) <= 1

    def new_chat(self) -> bool:
        """Start a fresh conversation, keeping this one to go back to.
        Returns False (and does nothing) when already on an empty last
        chat: pressing New chat twice must not bury the real one two
        steps back."""
        if self.is_empty() and self.index == len(self.chats) - 1:
            return False
        self.chats.append(_Chat())
        self.index = len(self.chats) - 1
        self.add(BUDDY, WELCOME)
        return True

    def go(self, index) -> bool:
        if not (0 <= index < len(self.chats)) or index == self.index:
            return False
        self.index = index
        return True

    def export_text(self) -> str:
        lines = ["Buddy conversation", "=" * 18, ""]
        for block in self.current.blocks:
            lines.append(f"{block['who']}:")
            for paragraph in block["body"].split("\n"):
                lines.append("  " + md_to_plain(paragraph))
            if block.get("images"):
                lines.append(f"  [{len(block['images'])} picture(s)]")
            if block["trace"]:
                lines.append(f"  [{md_to_plain(' · '.join(block['trace']))}]")
            lines.append("")
        return "\n".join(lines)


def block_view(index: int, block: dict) -> dict:
    """One block as the web view draws it. HTML is rendered here, in
    Python, from escaped text - the page inserts it as-is."""
    return {
        "i": index,
        "who": block["who"],
        "role": "error" if block["error"] else ("user" if block["who"] == YOU else "buddy"),
        "html": md_to_html(block["body"]),
        "trace": block["trace"],
        "copyable": block["copyable"],
        "images": block.get("images") or [],
    }
