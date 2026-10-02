#!/usr/bin/env python3
"""
The reasoning loop behind the Manual Chat page: prompt, tools, dispatch.

Pure Python - no Qt, no network of its own. page.py runs ask() on a worker
thread and renders what comes back, so this module stays testable headless.

Three tools, in descending order of how often they matter:

  search_manual   the grounded half: manual excerpts with chapter and page
                  citations (the citing rules are in SYSTEM_PROMPT).
  project_state   what a manual alone can never do - read the user's ACTUAL
                  open project. "Your timeline is 23.976 but that clip is
                  29.97" beats any amount of manual recitation. Optionally
                  walks the timeline itself and reports format mismatches
                  it computes rather than asks the model to spot, and always
                  reports whether this is Studio or the free edition.
  offer_tool      hands off to another Buddy page.

offer_tool deliberately does NOT navigate. The model proposes, the page
renders a button, the user decides - yanking someone off the chat mid-answer
because a model guessed at intent is a bad trade for one saved click.

READ-ONLY throughout. No tool here mutates a Resolve project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.tools_kb import get_tool, index_block, load_tools

from . import actions

from .llm import LLMError
from .resolve_ext import (
    focused_clip,
    project_snapshot,
    selected_clip_properties,
    timeline_contents,
    timeline_markers,
)

# How many tool-call turns the loop may make before it gives up with an
# error. This is the DEFAULT, not a hard cap: the user can raise it up to
# MAX_MAX_STEPS in Settings (Ask Buddy -> "Tool call budget") - a deeper
# budget lets the model search the manual repeatedly for hard questions,
# at the cost of one extra model round-trip (and its tokens) per step.
DEFAULT_MAX_STEPS = 6
MAX_MAX_STEPS = 20
EXCERPT_BUDGET = 7000
DEFAULT_RESULTS = 5

SYSTEM_PROMPT = """You are a DaVinci Resolve 21 technical assistant built into Buddy, a desktop toolkit that runs alongside the user's copy of Resolve.

Call search_manual before answering any question about how Resolve works. Its excerpts come from the OFFICIAL Blackmagic Design DaVinci Resolve 21 Reference Manual, with chapter and page citations. Treat them as ground truth: every excerpt-supported statement must be consistent with them, and any procedure, menu name, or setting that appears in an excerpt must match it exactly. Search again with different wording if the first results miss.

Call project_state when the question depends on how the user's project is actually configured - frame rate or resolution mismatches, color management, "why does my footage look wrong", "why won't this play smoothly". Prefer a specific answer about their real settings over a general one. If Resolve isn't running, say so in one line and answer from the manual instead.

Set include_focused_clip when the question is about the selected timeline clip or the clip at the playhead. Set include_timeline for questions about the edit as a whole. The result carries a "mismatches" list computed directly from their project; trust it over your own comparison of the numbers.

The session context reports the detected Resolve edition on every question. project_state also reports it under "resolve". The Reference Manual documents Studio-only features without always flagging them as Studio-only. If "is_studio" is false, DO NOT walk the user through a Studio feature as though they have it - name it, say it needs Studio, and give them the best approach available in the free version. If the edition is unknown, do not assume Studio features are available; say that edition-specific guidance needs a connection.

Call describe_tool when the user asks what a Buddy tool does, what Buddy can do for a task, or when you need a tool's exact capabilities before recommending it. The index below is a summary; describe_tool has the detail.

Call offer_tool when Buddy has a tool that does what the user is asking about by hand, and that tool is integrated. It shows them a button; it does not navigate. Never offer more than one tool per answer. Tools marked [not yet integrated] cannot be offered - you may say one is planned, but never imply it is usable today.

When project changes are enabled you also have propose_action. It does NOT change anything - it shows the user a card listing exactly what would happen, and they decide. Rules for it:
- Read the project with project_state FIRST. Never propose a change to a clip, frame or name you have not just read. Guessing at names is the most likely way to damage someone's work.
- One proposal per answer, and only when the user asked for a change. A question about how something works is not a request to do it.
- Say plainly in your answer what you proposed and that they must press Apply. Never write as though the change has already been made.
- If a proposal comes back rejected, tell the user what was wrong. Do not retry it with guessed values.
- Destructive actions (anything that inserts with a ripple, deletes, or changes settings) need a higher bar than additive ones. Propose one only when the user clearly asked for that specific change. "Clean up my timeline" or "fix this" is NOT a request to delete anything - ask what they mean.
- Never propose deleting a timeline unless the user named that timeline and asked for it to be deleted.
- You cannot undo. Say so rather than implying a change can be reversed from Buddy.

You also have general knowledge of DaVinci Resolve and video editing. Use it to make the answer clearer and to cover what the excerpts miss - BUT mark those statements clearly with (general knowledge) so the reader knows they are not from the manual.

Structure every answer as:
1. A short plain-language summary answering the user's question (2-4 sentences).
2. Steps or details as a numbered/bulleted list, written in clear simple sentences. Cite the source after each item: (Chapter N, p. X) for excerpt-based items, (your project) for anything read from project_state, (general knowledge) otherwise. These three labels are printed verbatim to the reader, who IS the user - address them as "you", never as "the user" or "they".
3. If the excerpts don't fully cover the question, one short line starting "Not covered in the retrieved manual pages:" stating exactly what is missing.

Rules:
- Never invent menu names, button names, or settings that are not in the excerpts.
- If an excerpt contradicts your general knowledge, the excerpt wins.
- Never state a project setting you did not read from project_state.
- Never claim a Buddy tool exists, works, or can do something beyond what the tool index and describe_tool say.
- Keep it tight: no preamble, no restating the question, no marketing tone."""


INSTRUCTIONS_PREAMBLE = """The user's custom instructions follow, written by them in Settings (Ask Buddy -> Custom instructions). Follow them: they describe their own workflow, conventions and preferences, and where they state a house rule (a delivery spec, a naming scheme, a preferred way of working), answer by it. They do not change the rules above - cite the manual as required, never contradict a manual excerpt about how Resolve works, and keep every rule about project changes. When an instruction and the manual disagree about how Resolve behaves, say so and give the manual's answer.

<custom_instructions>
{instructions}
</custom_instructions>"""


def build_system_prompt(registry=None, instructions: str = "") -> str:
    """Static rules, the live tool index, and the user's custom
    instructions (ask_folder.py) when there are any.

    The index is built from the registry each time rather than baked into
    the prompt string, so a tool that gets ported stops being advertised as
    unavailable the moment registry.py changes - no prompt edit needed.
    The instructions go last, after every rule, so they read as the user's
    context rather than as a replacement for the rules.
    """
    prompt = (
        SYSTEM_PROMPT
        + "\n\nBuddy's tools (tool_id, name, what it does):\n"
        + index_block(registry)
    )
    instructions = (instructions or "").strip()
    if instructions:
        prompt += "\n\n" + INSTRUCTIONS_PREAMBLE.format(instructions=instructions)
    return prompt


def tool_specs(catalog: list[tuple[str, str]], allow_writes: bool = False) -> list[dict]:
    """JSON-Schema tool definitions, provider-neutral (llm.py converts).

    propose_action is ABSENT, not disabled, when writing is off. A tool
    the model cannot see is one it cannot be talked into calling, which
    is a stronger guarantee than refusing the call after the fact.
    """
    names = ", ".join(tid for tid, _ in catalog)
    specs = [
        {
            "name": "search_manual",
            "description": (
                "Search the DaVinci Resolve 21 Reference Manual. Returns "
                "excerpts with chapter and page citations. Use the user's own "
                "words; rephrase and search again if results look off-topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for, in natural language.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Excerpts to return (default {DEFAULT_RESULTS}).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "project_state",
            "description": (
                "Read the user's currently open Resolve project: which Resolve "
                "they run (free or Studio), timeline frame rate and resolution, "
                "color management settings, track counts, and the properties of "
                "selected clips. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_clips": {
                        "type": "boolean",
                        "description": (
                            "Also read the Media Pool's selected clips."
                        ),
                    },
                    "include_timeline": {
                        "type": "boolean",
                        "description": (
                            "Also list the clips on the current timeline with "
                            "their source format, and any that do not match "
                            "the timeline. Use for questions about a specific "
                            "clip or about the edit."
                        ),
                    },
                    "include_markers": {
                        "type": "boolean",
                        "description": "Also list the timeline's markers.",
                    },
                    "include_focused_clip": {
                        "type": "boolean",
                        "description": "Read selected timeline clip(s), or the video clip under the playhead, with source properties and format mismatches.",
                    },
                },
            },
        },
        {
            "name": "describe_tool",
            "description": (
                "Get the full capability list for one Buddy tool: what it "
                "does, whether it needs Resolve running, whether it is "
                "integrated yet, and any caveats."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": f"One of: {names}",
                    }
                },
                "required": ["tool_id"],
            },
        },
        {
            "name": "offer_tool",
            "description": (
                "Offer the user a Buddy tool that automates what they're asking "
                f"about. Shows a button. Valid tool_id values: {names}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_id": {"type": "string", "description": "Buddy tool id."},
                    "reason": {
                        "type": "string",
                        "description": "One short line on why it helps here.",
                    },
                },
                "required": ["tool_id", "reason"],
            },
        },
    ]

    if allow_writes:
        specs.append({
            "name": "propose_action",
            "description": (
                "Propose a change to the user's Resolve project. This does "
                "NOT make the change: the user is shown a card listing "
                "exactly what would happen and presses Apply or Discard. "
                "One proposal per answer.\n"
                "Additive - nothing already in place moves:\n"
                "  add_markers (markers: [{frame, name, color, note}])\n"
                "  create_bin (name)\n"
                "  create_timeline (name)\n"
                "  rename_clips (renames: [{from, to}])\n"
                "  set_clip_colors (color, clips: [{track, index, name, start}])\n"
                "  append_to_timeline (clips: [name]) - adds to the END\n"
                "Destructive - moves or destroys work not named:\n"
                "  insert_into_timeline (kind: fusion_composition|title|"
                "fusion_title|generator|fusion_generator, name) - inserts at "
                "the playhead and RIPPLES everything after it\n"
                "  delete_markers (color) or (frames: [n])\n"
                "  delete_timeline_clips (clips: [{track, index, name, start}], ripple)\n"
                "  delete_media_pool_items (clips: [name], timelines: [name], "
                "bins: [name])\n"
                "  set_project_settings (settings: {key: value})\n"
                "  set_timeline_settings (settings: {key: value})"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "description": "One of: " + ", ".join(sorted(actions.ACTIONS)),
                    },
                    "arguments": {
                        "type": "object",
                        "description": "That action's own arguments.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One short line on why this change.",
                    },
                },
                "required": ["action_id", "arguments"],
            },
        })

    return specs


@dataclass
class ToolEvent:
    """One tool call, surfaced in the UI so the user can see the work."""

    name: str
    summary: str


@dataclass
class AgentResult:
    answer: str = ""
    passages: list = field(default_factory=list)
    events: list[ToolEvent] = field(default_factory=list)
    offered_tool: tuple[str, str] | None = None
    # A ProposedAction the user has not approved. page.py renders it as a
    # card; nothing in this module ever executes one.
    proposed_action: object | None = None
    error: str = ""


class ManualAgent:
    """Owns one conversation's tool loop.

    connect_resolve is a zero-arg callable returning a live controller, or
    raising - normally host.ensure_connected. Passed in rather than imported
    so the agent can be tested with a fake, and so a Resolve outage surfaces
    as a tool result the model can reason about instead of an exception.
    """

    def __init__(self, retriever, llm, connect_resolve=None, registry=None,
                 allow_writes=False, max_steps=DEFAULT_MAX_STEPS, instructions="", edition=None,
                 prefetch_focus=False, allow_reads=True):
        self.retriever = retriever
        self.llm = llm
        self.connect_resolve = connect_resolve
        self.prefetch_focus = bool(prefetch_focus)
        # Whether what it reads of the project may go to the model's server.
        # Decided by the page, from the user's consent for that destination,
        # before the agent is built - nothing the model (or a project's own
        # text) says can change it.
        self.allow_reads = bool(allow_reads)
        self.registry = registry
        # Captured at build time from the saved consent. page.py rebuilds
        # the agent whenever that setting changes, so a conversation
        # already in flight can never gain write access part-way through.
        self.allow_writes = bool(allow_writes)
        # Same reasoning: the loop budget is fixed per build, and the page
        # rebuilds the agent on every send, so a change in Settings applies
        # to the next message rather than mid-answer.
        self.max_steps = int(max(DEFAULT_MAX_STEPS, min(max_steps, MAX_MAX_STEPS)))
        self.tools = load_tools(registry)
        self.catalog = [(t.tool_id, t.name) for t in self.tools]
        self._specs = tool_specs(self.catalog, self.allow_writes)
        self._system = build_system_prompt(registry, instructions)
        if edition and edition.get("product"):
            self._system += "\n\nCurrent Resolve installation (checked for this question): " + json.dumps(edition)
        else:
            self._system += "\n\nCurrent Resolve edition: unknown because Buddy is not connected. Do not assume Studio features are available."
        self._progress = None

    def _say(self, message: str) -> None:
        """Report what the loop is doing now. Optional by design - agent.py
        stays headless-testable, and a local model taking 40s per turn would
        otherwise look like a hang behind a static 'Thinking...'."""
        if self._progress:
            self._progress(message)

    # --------------------------------------------------------- tool impls

    def _do_search(self, args: dict, result: AgentResult) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "No query supplied."
        self._say(f'Searching the manual: "{query}"')
        limit = int(args.get("limit") or DEFAULT_RESULTS)
        passages = self.retriever.search(query, limit=max(1, min(limit, 8)))
        if not passages:
            return (
                "No matching passages. "
                f"Retrieval mode: {self.retriever.describe_tier()}"
            )
        result.passages.extend(passages)
        result.events.append(
            ToolEvent("search_manual", f'"{query}" – {len(passages)} excerpts')
        )

        out, spent = [], 0
        for p in passages:
            text = " ".join(p.text.split())
            if spent + len(text) > EXCERPT_BUDGET:
                text = text[: max(0, EXCERPT_BUDGET - spent)]
            if not text:
                break
            spent += len(text)
            out.append(f"[{p.citation()}]\n{text}")
        return "\n\n".join(out)

    NOT_ALLOWED_TO_READ = (
        "The user has not allowed Ask Buddy to send details of their project to their AI provider, so the project "
        "was not read. Say that in one line - they can allow it in Settings > AI, or when asked - and answer "
        "from the manual instead. Do not guess at their project's settings.")

    def _do_project_state(self, args: dict, result: AgentResult) -> str:
        if not self.allow_reads:
            result.events.append(ToolEvent("project_state", "not allowed"))
            return self.NOT_ALLOWED_TO_READ
        self._say("Reading your Resolve project…")
        if not self.connect_resolve:
            return "Resolve integration is unavailable in this context."
        try:
            controller = self.connect_resolve()
        except Exception as e:
            result.events.append(ToolEvent("project_state", "Resolve not reachable"))
            return f"Could not reach DaVinci Resolve: {e}"

        snap = project_snapshot(controller)
        read = []
        if snap.get("open"):
            if args.get("include_clips"):
                snap["selected_clips"] = selected_clip_properties(controller)
                read.append("selected clips")
            if args.get("include_timeline"):
                contents = timeline_contents(controller)
                snap["timeline_contents"] = contents
                count = len(contents.get("mismatches") or [])
                read.append(
                    f"timeline ({count} mismatch{'' if count == 1 else 'es'})"
                )
            if args.get("include_markers"):
                markers = timeline_markers(controller)
                snap["markers"] = markers
                read.append(f"{markers.get('total', 0)} markers")
            if args.get("include_focused_clip"):
                snap["focused_clip"] = focused_clip(controller)
                read.append("focused clip")

        label = snap.get("project_name") or "no project open"
        # The edition goes in the event line because it silently changes
        # which answers are even correct.
        edition = (snap.get("resolve") or {}).get("product") or ""
        summary = f"read {label}"
        if edition:
            summary += f" ({edition})"
        if read:
            summary += " + " + ", ".join(read)
        result.events.append(ToolEvent("project_state", summary))
        return json.dumps(snap, indent=1, default=str)

    def _do_describe_tool(self, args: dict, result: AgentResult) -> str:
        self._say("Checking what Buddy's tools can do…")
        tool_id = (args.get("tool_id") or "").strip()
        tool = get_tool(tool_id, self.registry)
        if not tool:
            return (
                f"No such tool_id '{tool_id}'. Valid ids: "
                f"{', '.join(t for t, _ in self.catalog)}"
            )
        result.events.append(ToolEvent("describe_tool", tool.name))
        return tool.detail()

    def _do_offer_tool(self, args: dict, result: AgentResult) -> str:
        tool_id = (args.get("tool_id") or "").strip()
        tool = get_tool(tool_id, self.registry)
        if not tool:
            return (
                f"Unknown tool_id. Valid ids: "
                f"{', '.join(t for t, _ in self.catalog)}"
            )
        if not tool.is_available:
            # The button would land the user on a "Not yet integrated" page.
            return (
                f"'{tool.name}' is not integrated into Buddy yet, so it "
                "cannot be offered. Tell the user it is planned rather than "
                "implying they can use it now."
            )
        reason = (args.get("reason") or "").strip()
        result.offered_tool = (tool_id, reason)
        result.events.append(ToolEvent("offer_tool", tool.name))
        return f"Offered '{tool.name}' to the user as a button."

    def _do_propose_action(self, args: dict, result: AgentResult) -> str:
        """Build a preview of a change. NEVER executes - page.py does that
        once the user presses Apply."""
        action_id = (args.get("action_id") or "").strip()
        self._say("Working out what that change would do…")

        if not self.allow_writes:
            # Unreachable while the spec is gated, and kept anyway: this is
            # the one tool where a wrong assumption costs the user real work.
            return (
                "Project changes are not enabled. Tell the user they can turn "
                "them on in Settings; do not describe the change as done."
            )
        if not self.allow_reads:
            return self.NOT_ALLOWED_TO_READ       # a proposal quotes the project's own names and settings
        if result.proposed_action is not None:
            return (
                "A change has already been proposed in this answer. Only one "
                "proposal per answer is allowed."
            )
        if not self.connect_resolve:
            return "Resolve integration is unavailable in this context."
        try:
            controller = self.connect_resolve()
        except Exception as e:
            return f"Could not reach DaVinci Resolve: {e}"

        try:
            proposal = actions.preview(controller, action_id, args.get("arguments"))
        except actions.ActionError as e:
            # Returned as an ordinary tool result so the model can explain the
            # problem to the user, rather than the turn dying on an exception.
            result.events.append(ToolEvent("propose_action", "rejected: " + str(e)))
            return "That proposal was rejected and nothing was changed: " + str(e)

        proposal.reason = (args.get("reason") or "").strip()
        result.proposed_action = proposal
        result.events.append(
            ToolEvent(
                "propose_action",
                action_id
                + (" (DESTRUCTIVE)" if proposal.destructive else "")
                + " – awaiting the user",
            )
        )
        return (
            "Shown to the user as a proposal awaiting approval. It has NOT "
            "been applied. What they will see:\n" + proposal.as_text()
        )

    def _dispatch(self, call, result: AgentResult) -> str:
        try:
            if call.name == "search_manual":
                return self._do_search(call.arguments, result)
            if call.name == "project_state":
                return self._do_project_state(call.arguments, result)
            if call.name == "describe_tool":
                return self._do_describe_tool(call.arguments, result)
            if call.name == "offer_tool":
                return self._do_offer_tool(call.arguments, result)
            if call.name == "propose_action":
                return self._do_propose_action(call.arguments, result)
        except Exception as e:  # a broken tool must not kill the answer
            return f"{call.name} failed: {e}"
        return f"Unknown tool: {call.name}"

    # ---------------------------------------------------------------- run

    def ask(
        self,
        question: str,
        history: list[dict] | None = None,
        progress=None,
        images: list[dict] | None = None,
    ) -> AgentResult:
        """Run the loop to a final answer. Never raises; errors land on
        AgentResult.error so the page can render them in the transcript.

        images: pictures sent with this question ({"mime", "data"}, see
        pictures.py) - on this turn only.

        progress, if given, is called with short status strings as the loop
        works. It runs on the CALLER's thread - page.py passes one that only
        emits a Qt signal, never touches a widget directly.
        """
        self._progress = progress
        result = AgentResult()
        messages = list(history or [])
        model_question = question
        if self.prefetch_focus and self.allow_reads:
            focus = self._do_project_state({"include_focused_clip": True}, result)
            model_question += "\n\nLive Resolve context for this question (read only):\n" + focus
        messages.append({"role": "user", "content": model_question,
                         **({"images": images} if images else {})})

        for step in range(self.max_steps):
            self._say("Thinking…" if step == 0 else f"Thinking (step {step + 1})…")
            try:
                reply = self.llm.chat(self._system, messages, self._specs)
            except LLMError as e:
                result.error = str(e)
                return result

            if not reply.wants_tools:
                result.answer = reply.content.strip()
                if not result.answer:
                    result.error = "The model returned an empty answer."
                return result

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "arguments": c.arguments,
                            # Replayed verbatim - Gemini rejects the turn without it.
                            "signature": c.signature,
                        }
                        for c in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": self._dispatch(call, result),
                    }
                )

        result.error = (
            f"Gave up after {self.max_steps} tool steps without a final answer. "
            "Try asking a narrower question, or raise the tool-call budget "
            "in Settings."
        )
        return result
