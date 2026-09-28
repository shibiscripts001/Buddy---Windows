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
import random
import re
from datetime import datetime

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

# Shown under the welcome of an empty chat; clicking one sends it. Three come from the
# pool, one per group where it can (so they're never all the same kind), and "What can
# you do?" always ends the row. Which three is chosen once per run (shuffled_pool), so a
# new chat doesn't reshuffle them. "project" ones read the open project, so they're only
# offered while Buddy is connected to Resolve.
HELP_SUGGESTION = "What can you do?"   # one of HELP_WORDS: lists what Buddy can do
SUGGESTION_POOL = {
    "resolve": [
        "How do I add a Power Window on the Color page?",
        "What's the difference between Fusion and the Edit page titles?",
        "How do I stabilize a shaky clip?",
        "How do I speed ramp a clip?",
        "How do I make a J-cut or an L-cut?",
        "How do I track a mask onto a moving object?",
        "How does Magic Mask work?",
        "When should I use a serial, parallel or layer node?",
        "How do I copy a grade to other clips?",
        "How do I use adjustment clips?",
        "How do I set up proxies for smoother playback?",
        "What render settings should I use for YouTube?",
        "How do I replace a clip without losing its effects?",
        "How do I reduce background noise in dialogue?",
    ],
    "project": [
        "Check my timeline settings for problems",
        "Do any clips not match my timeline's frame rate?",
        "Am I on DaVinci Resolve Studio or the free version?",
        "Summarise the markers on my timeline",
        "What colour management is my project using?",
        "Is my timeline set up right for a 4K YouTube upload?",
    ],
    "tools": [
        "Which Buddy tool makes subtitles?",
        "How can Buddy help me relink offline media?",
        "Can Buddy rename a batch of clips for me?",
        "How do I animate my Text+ titles?",
        "What's the fastest way to import images into Resolve?",
    ],
}
SUGGESTIONS_PICKED = 3


def shuffled_pool(rng=random) -> dict[str, list[str]]:
    """The pool with each group in its own random order - made once per run."""
    return {group: rng.sample(questions, len(questions)) for group, questions in SUGGESTION_POOL.items()}


def suggestions(pool: dict[str, list[str]], connected: bool) -> list[str]:
    """The suggestion row: SUGGESTIONS_PICKED from the pool, taken round the groups in
    turn (a second from the same group only when a group is left out), then help."""
    groups = [g for g in SUGGESTION_POOL if connected or g != "project"]
    picked = [pool[groups[i % len(groups)]][i // len(groups)] for i in range(SUGGESTIONS_PICKED)]
    return picked + [HELP_SUGGESTION]

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
        "**Buttons:** New chat starts a fresh conversation; Chats searches "
        "saved conversations and lets you rename them. Check project reviews "
        "the current timeline's video formats. Explain clip reads the "
        "selected timeline clip or clip at the playhead.",
        "",
        "To see this again any time, type `help`.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------- markdown --

_CITATION = re.compile(r"\((Chapter \d+[^)]*)\)")
_CITED_PAGE = re.compile(r"\b(?:pp?\.|pages?)\s*(\d+)", re.IGNORECASE)
_SOURCE = re.compile(r"\((general knowledge|their project)\)")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
# Runs on ESCAPED text, so it must stop at an escaped quote or bracket too.
_BARE_URL = re.compile(r"\bhttps?://(?:(?!&quot;|&#x27;|&lt;|&gt;)[^\s<>\"')\]])+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_HEADING = re.compile(r"^\s*(#{1,4})\s+(.*)$")


def _cite(m):
    """A manual citation, carrying its page for the view to open the PDF
    at (the first page of a range, or of several citations in one)."""
    page = _CITED_PAGE.search(m.group(1))
    data = f' data-page="{page.group(1)}"' if page else ""
    return f'<span class="cite"{data}>({m.group(1)})</span>'


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
        s = _CITATION.sub(_cite, s)
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
        self.title = "New chat"
        self.updated = datetime.now().isoformat(timespec="seconds")
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

    def add(self, who, body, trace=None, error=False, copyable=False, images=None, raw=False) -> dict:
        block = {
            "who": who,
            "body": body,
            "trace": list(trace or []),
            "error": bool(error),
            "copyable": bool(copyable),
            "raw": bool(raw),   # someone's own words (a question, the model's answer): never translated
            "images": list(images or []),   # small data: URLs of the pictures sent with it
        }
        self.current.blocks.append(block)
        if who == YOU and self.current.title == "New chat":
            self.current.title = " ".join(body.split())[:72] or "Picture question"
        self.current.updated = datetime.now().isoformat(timespec="seconds")
        return block

    def summaries(self, query="") -> list[dict]:
        """Search full transcripts, while sending only short previews to the view."""
        needle = query.strip().casefold()
        found = []
        for index, chat in enumerate(self.chats):
            match = next((b["body"] for b in chat.blocks
                          if needle and needle in b["body"].casefold()), "")
            if needle and needle not in chat.title.casefold() and not match:
                continue
            found.append({"index": index, "title": chat.title, "updated": chat.updated,
                          "preview": " ".join(match.split())[:130] if match else ""})
        return list(reversed(found))

    def rename(self, index, title):
        title = " ".join(str(title).split())[:100]
        if not title or not 0 <= index < len(self.chats):
            return False
        self.chats[index].title = title
        self.chats[index].updated = datetime.now().isoformat(timespec="seconds")
        return True

    def to_data(self) -> dict:
        """Save only the small preview images, never full model uploads."""
        return {"version": 1, "index": self.index, "chats": [
            {"title": c.title, "updated": c.updated, "history": c.history,
             "blocks": c.blocks}
            for c in self.chats]}

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("chats"), list):
            raise ValueError("Unsupported conversation file")
        instance = cls()
        restored = []
        for entry in data["chats"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("blocks"), list) or not isinstance(entry.get("history"), list):
                raise ValueError("Invalid conversation")
            chat = _Chat()
            chat.title = str(entry.get("title") or "New chat")[:100]
            chat.updated = str(entry.get("updated") or "")[:40]
            chat.history = [m for m in entry["history"] if isinstance(m, dict)
                            and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
            chat.blocks = []
            for block in entry["blocks"]:
                if not isinstance(block, dict) or not isinstance(block.get("body"), str):
                    raise ValueError("Invalid message")
                chat.blocks.append({"who": str(block.get("who") or BUDDY), "body": block["body"],
                                    "trace": list(block.get("trace") or []), "error": bool(block.get("error")),
                                    "copyable": bool(block.get("copyable")), "raw": bool(block.get("raw")),
                                    "images": [v for v in (block.get("images") or [])
                                               if isinstance(v, str) and v.startswith("data:image/jpeg;base64,")
                                               and len(v) < 100_000][:4]})
            restored.append(chat)
        if restored:
            instance.chats = restored
            instance.index = min(max(0, int(data.get("index", len(restored)-1))), len(restored)-1)
        return instance

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
        "raw": block.get("raw", False),
        "images": block.get("images") or [],
    }
