#!/usr/bin/env python3
"""
What Buddy's tools can do - the app's knowledge about itself.

Two halves, kept apart on purpose:

  tools_kb.json    hand-written capability descriptions, derived from each
                   tool's own README. Edit when a tool's behaviour changes.
  the registry     the live truth about whether a tool actually WORKS yet,
                   read here through ToolPage.is_placeholder.

Availability is never written down in the JSON. Most of Buddy's nav is still
placeholder pages, and a hand-maintained "status" field would eventually
claim a tool works when clicking it shows "Not yet integrated" - which is
exactly the error the chat agent must not make when recommending something.
Deriving it means the two can't drift.

No Qt import here: importing the registry pulls in page classes (and so
PySide6), so callers that only want the descriptions can pass registry=None
and get availability="unknown" rather than a hard dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

KB_PATH = Path(__file__).with_name("tools_kb.json")

AVAILABLE = "available"
PLANNED = "planned"
UNKNOWN = "unknown"


@dataclass
class ToolInfo:
    tool_id: str
    name: str
    summary: str
    capabilities: list[str] = field(default_factory=list)
    needs_resolve: bool = True
    notes: str = ""
    availability: str = UNKNOWN

    @property
    def is_available(self) -> bool:
        return self.availability == AVAILABLE

    def one_line(self) -> str:
        """Compact form for the agent's always-on index."""
        mark = "" if self.is_available else " [not yet integrated]"
        return f"{self.tool_id} ({self.name}){mark}: {self.summary}"

    def detail(self) -> str:
        """Full form, for when the agent asks about one specific tool."""
        lines = [f"{self.name} (tool_id: {self.tool_id})", self.summary, ""]
        lines.append(
            "Status: "
            + (
                "integrated and usable in Buddy now."
                if self.is_available
                else "NOT yet integrated - its page in Buddy is a placeholder. "
                "Describe what it will do, but tell the user it isn't "
                "available yet and do not offer it as a button."
            )
        )
        lines.append(
            "Needs DaVinci Resolve running: " + ("yes" if self.needs_resolve else "no")
        )
        if self.capabilities:
            lines.append("")
            lines.append("What it can do:")
            lines.extend(f"- {c}" for c in self.capabilities)
        if self.notes:
            lines.append("")
            lines.append(f"Note: {self.notes}")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def _raw() -> dict:
    try:
        return json.loads(KB_PATH.read_text(encoding="utf-8")).get("tools", {})
    except (OSError, ValueError):
        # A malformed KB must not take the chat page down with it - the agent
        # simply loses its knowledge of the other tools.
        return {}


def _tool_ids(page_cls) -> list:
    """A rail page's tool id, then those of the tools on its tabs (Media
    Manager's Batch Clip Renamer and Media Relink - the shell opens either
    by its own id)."""
    return [getattr(page_cls, "tool_id", None), *getattr(page_cls, "SUB_TOOLS", ())]


def _availability(tool_id: str, registry) -> str:
    if registry is None:
        return UNKNOWN
    for _category, page_cls in registry:
        if tool_id in _tool_ids(page_cls):
            return PLANNED if getattr(page_cls, "is_placeholder", False) else AVAILABLE
    return PLANNED


def load_tools(registry=None) -> list[ToolInfo]:
    """Every documented tool, in registry order when one is supplied."""
    raw = _raw()
    tools = [
        ToolInfo(
            tool_id=tool_id,
            name=entry.get("name", tool_id),
            summary=entry.get("summary", ""),
            capabilities=list(entry.get("capabilities") or []),
            needs_resolve=bool(entry.get("needs_resolve", True)),
            notes=entry.get("notes", ""),
            availability=_availability(tool_id, registry),
        )
        for tool_id, entry in raw.items()
    ]
    if registry is not None:
        order = [tid for _cat, c in registry for tid in _tool_ids(c)]
        tools.sort(
            key=lambda t: order.index(t.tool_id) if t.tool_id in order else len(order)
        )
    return tools


def get_tool(tool_id: str, registry=None) -> ToolInfo | None:
    for tool in load_tools(registry):
        if tool.tool_id == tool_id:
            return tool
    return None


def index_block(registry=None) -> str:
    """The whole toolset as one compact block for the system prompt.

    Small enough (~12 lines) to send on every turn, which beats making the
    model spend a round trip discovering that Buddy has a tool for this.
    """
    lines = [t.one_line() for t in load_tools(registry)]
    return "\n".join(f"- {line}" for line in lines)
