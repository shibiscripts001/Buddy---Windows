#!/usr/bin/env python3
"""
Ask Buddy - a grounded chat page over the Resolve manual and the user's
actual open project. The first tool on the web UI (core/web_page.py): the
view is web/index.html + chat.js; everything it shows is decided here.

Protocol (see core/web_page.py for the mechanism):
    to the view    transcript, append, controls, status, thinking,
                   proposal, offer, toast, pictures, chat_list
    from the view  send, copy, new_chat, prev_chat, next_chat, export,
                   apply_proposal, discard_proposal, open_tool, open_manual,
                   attach_image, paste_image, remove_picture,
                   search_chats, select_chat, rename_chat, delete_chat,
                   delete_all_chats, run_checkup, explain_clip

Pictures (pictures.py) can go with a question - picked, pasted or dropped -
for a model that can see them; only that question carries them.

Threading: agent.ask() makes several blocking HTTPS round trips, so it runs
on a QThread worker and the page renders what the finished signal carries.

The worker must NEVER call host.ensure_connected(). That looks like a plain
Resolve call but it is a shell method: on a cold connection it runs
_connect(), which sets status_label's text and stylesheet and can raise a
QMessageBox parented to the main window. Doing that off the GUI thread
gives "QObject::setParent: Cannot set parent, new parent is in a different
thread" and takes the app down. So the controller is resolved HERE, on the
GUI thread, before the worker starts, and the worker only ever touches the
already-built controller object (plain Python, no Qt affinity).

A page that is open while Resolve is started later therefore needs the
header's Connect button pressed once - which is what the
project_state tool tells the user when there is no connection.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QStandardPaths, QThread, Signal
from PySide6.QtWidgets import QApplication, QFileDialog

from core.i18n import tr, tr_filter
from core.message_dialog import confirm
from core.resolve_bridge import ResolveConnectionError
from core.tools_kb import get_tool
from core.web_page import WebToolPage

from . import actions, chat_store, embedder, local_llama, manual_pdf, pictures
from .agent import AgentResult, ManualAgent
from .ask_folder import instructions_for_prompt
from .config import (
    DEFAULTS,
    default_data_paths,
    history_limit,
    llm_client_from_settings,
    max_steps_limit,
    migrate_legacy_settings,
)
from .conversation import (
    BUDDY,
    ERROR,
    YOU,
    ChatSessions,
    block_view,
    help_text,
    is_help,
    shuffled_pool,
    suggestions,
)
from .retrieval import TIER_NONE, ManualRetriever
from .resolve_ext import project_checkup, resolve_info
from .settings_panel import ChatSettingsMixin

PICTURE_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff);;All files (*)"
PICTURE_ENDINGS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")


class _AgentWorker(QThread):
    done = Signal(object)
    progress = Signal(str)

    def __init__(self, agent, question, history, images=None):
        super().__init__()
        self._agent, self._question, self._history = agent, question, history
        self._images = images

    def run(self):
        """Always emits exactly once. ask() is written not to raise, but if
        it ever does, a silent thread death would leave the page stuck
        "thinking" with no way back - so the failure becomes a visible
        answer instead."""
        try:
            result = self._agent.ask(
                self._question, self._history, progress=self.progress.emit, images=self._images
            )
        except Exception as e:  # noqa: BLE001 - surfaced in the transcript
            result = AgentResult(error=f"Unexpected failure: {e!r}")
        self.done.emit(result)


class _CheckupWorker(QThread):
    done = Signal(object)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def run(self):
        try:
            self.done.emit(project_checkup(self.controller))
        except Exception as exc:  # shown in the chat
            self.done.emit({"error": str(exc)})


class ManualChatPage(ChatSettingsMixin, WebToolPage):
    tool_id = "manual_chat"
    display_name = "Ask Buddy"
    category = "Ask"
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    file_drops = True   # pictures dragged in go with the next question (pictures.py)

    def build_state(self):
        self.settings = self.host.tool_settings(self.tool_id, dict(DEFAULTS))
        migrate_legacy_settings(self.settings)
        self._read_declined = None            # the address whose project-reading question was answered no
        self._retriever = None
        self._worker = None
        self._sending = False
        self._thinking = ""
        self._pending_question = ""
        self._proposal = None
        # "ready", "busy" (applying: both buttons off) or "failed" (Apply
        # stays off - re-running a half-applied change is not something to
        # offer with one click - but Discard still works).
        self._proposal_state = "ready"
        self._proposal_note = ""
        self._chat_query = ""
        self._chat_store_error = ""
        try:
            self.chats, warnings = chat_store.load(self.ask_folder)
            self._chat_store_error = " ".join(warnings)
        except (OSError, ValueError) as exc:
            self.chats = ChatSessions()
            self._chat_store_error = f"Saved chats could not be read: {exc}. Saving is disabled to protect the file."
        self._checkup_worker = None
        self._suggestion_pool = shuffled_pool()     # this run's pick - a new chat keeps it
        self.pictures = []              # pictures.Picture, waiting to go with the next question
        self._pending_pictures = 0

    def web_ready(self):
        self._push_pictures()
        self._push_transcript()
        self._push_controls()
        self._push_chat_list()
        self._push_status()
        self._push_proposal()
        self._push_offer()
        self.emit("thinking", {"on": self._sending, "message": self._thinking})

    # ------------------------------------------------------------- wiring

    def on_shown(self):
        """Load the manual lazily - parsing the bundle costs a couple of
        seconds and shouldn't happen at app start for people who never open
        this page."""
        if self._retriever is not None:
            return
        self.host.set_busy(True, "Loading the Resolve manual…")
        try:
            bundle = self.settings.get("bundle_dir") or ""
            text = self.settings.get("text_path") or ""
            if not bundle and not text:
                bundle, text = default_data_paths()
            self._retriever = ManualRetriever(bundle or None, text or None, embedder.from_settings(self.settings))
        finally:
            self.host.set_busy(False)
        self._push_status()

    def _status_text(self) -> str:
        if self._retriever is None:
            return ""
        note = self._retriever.describe_tier()
        if self._retriever.tier == TIER_NONE:
            note += "  Build it from the manual PDF in Settings > AI > Manual search > Rebuild from PDF…"
        return note

    def on_app_quitting(self):
        # Buddy's own llama.cpp, if semantic search started it.
        local_llama.stop_all()

    def _reload_manual(self, _result=None):
        """Pick up a freshly built bundle - or a newly chosen embedder -
        without restarting Buddy."""
        self._retriever = None
        if self.isVisible():
            self.on_shown()

    def _build_agent(self, prefetch_focus=False) -> ManualAgent:
        llm = llm_client_from_settings(self.settings)
        # Read here, on every build, rather than cached on the page. The
        # agent is rebuilt per send, so revoking consent in Settings takes
        # effect on the very next message instead of the next launch.
        allow_writes = bool(self.settings.get("allow_project_writes", False))
        allow_reads = self.project_reads_allowed(llm)
        controller = self.host.controller if getattr(self.host, "connected", False) else None
        edition = resolve_info(controller) if controller else None
        # host.registry rather than importing registry.py - that module
        # imports this page, so importing it back would be a cycle.
        return ManualAgent(
            self._retriever,
            llm,
            connect_resolve=self._resolve_source(),
            registry=getattr(self.host, "registry", None),
            allow_writes=allow_writes,
            max_steps=max_steps_limit(self.settings),
            # From the file each time, like the consent above: an edit in
            # Settings or a text editor applies to the next question.
            instructions=instructions_for_prompt(self.ask_folder),
            edition=edition,
            prefetch_focus=prefetch_focus,
            allow_reads=allow_reads,
        )

    def _ask_to_read_project(self):
        """Once, before a question to a cloud model with Resolve connected:
        may it read the project? Not asked again for that address if the
        answer was no (Settings has the switch)."""
        llm = llm_client_from_settings(self.settings)
        if (not getattr(self.host, "connected", False) or self.project_reads_allowed(llm)
                or self._read_declined == llm.destination or llm.validate()):
            return
        where = llm.label + ("" if llm.destination in (llm.provider, llm.label.lower()) else f" ({llm.destination})")
        if confirm(self.window(), "Let Ask Buddy read your project?",
                   tr("To answer questions about your project, Ask Buddy reads its settings, timeline, clip and "
                      "marker names and sends what it reads to {where} along with your question. Allow that? "
                      "You can change it any time in Settings > AI.").replace("{where}", where),
                   ok="Allow", cancel="Not now"):
            self._save("project_read_consent", llm.destination)
        else:
            self._read_declined = llm.destination

    def _resolve_source(self):
        """A zero-arg callable handing the worker the shell's EXISTING
        controller, captured now, on the GUI thread.

        Deliberately does not connect: ensure_connected() touches widgets
        (see this module's docstring), and a probe on every message would
        also cost about a second whenever Resolve is closed.
        """
        controller = self.host.controller if getattr(self.host, "connected", False) else None

        def source():
            if controller is None:
                raise RuntimeError(
                    "Buddy is not connected to DaVinci Resolve. Press "
                    "Connect in Buddy's header (with Resolve "
                    "running and a project open), then ask again."
                )
            return controller

        return source

    # ------------------------------------------------------------ the view

    def _push_transcript(self):
        self.emit("transcript", {
            "blocks": [block_view(i, b) for i, b in enumerate(self.chats.blocks)],
            "suggestions": (suggestions(self._suggestion_pool, bool(getattr(self.host, "connected", False)))
                            if self.chats.is_empty() else []),
        })

    def _append(self, who, body, trace=None, error=False, copyable=False, images=None, raw=False):
        block = self.chats.add(who, body, trace=trace, error=error, copyable=copyable, images=images, raw=raw)
        self.emit("append", block_view(len(self.chats.blocks) - 1, block))
        self._persist_chats()

    def _persist_chats(self, forget_backup=False):
        if self._chat_store_error and "Saving is disabled" in self._chat_store_error:
            return
        try:
            chat_store.save(self.ask_folder, self.chats, forget_backup=forget_backup)
        except OSError as exc:
            self._toast(f"Could not save conversations: {exc}")

    def _push_chat_list(self):
        self.emit("chat_list", {"items": self.chats.summaries(self._chat_query),
                                "current": self.chats.index, "query": self._chat_query})

    def _push_controls(self):
        chats = self.chats
        self.emit("controls", {
            "sending": self._sending,
            "turns": chats.turns,
            "limit": history_limit(self.settings),
            "index": chats.index,
            "total": len(chats.chats),
            # Same _sending guard everywhere: swapping the transcript out
            # from under a reply in flight would land it in the wrong chat.
            "can_prev": chats.index > 0 and not self._sending,
            "can_next": chats.index < len(chats.chats) - 1 and not self._sending,
            "can_new": not self._sending and not (chats.is_empty() and chats.index == len(chats.chats) - 1),
            "connected": bool(getattr(self.host, "connected", False)),
            "title": chats.current.title,
        })
        self._push_chat_list()

    def _push_status(self):
        note = self._status_text()
        controller = self.host.controller if getattr(self.host, "connected", False) else None
        product = resolve_info(controller).get("product") if controller else ""
        if product:
            note = f"{product} · {note}"
        if self._chat_store_error:
            note += "  " + self._chat_store_error
        self.emit("status", {"text": note})

    def _toast(self, text):
        self.emit("toast", {"text": text})

    # ------------------------------------------------------------ sending

    # ----------------------------------------------------------- pictures

    def on_attach_image(self, _payload=None):
        paths, _chosen = QFileDialog.getOpenFileNames(self, tr("Add pictures"), "", tr_filter(PICTURE_FILTER))
        self._add_picture_files(paths)

    def on_files_dropped(self, paths):
        pictures_ = [p for p in paths if p.lower().endswith(PICTURE_ENDINGS)]
        if not pictures_:
            self._toast("Only pictures can be added – PNG, JPEG, WebP, GIF, BMP or TIFF.")
        self._add_picture_files(pictures_)

    def on_paste_image(self, _payload=None):
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        files = [u.toLocalFile() for u in (mime.urls() if mime is not None and mime.hasUrls() else [])
                 if u.isLocalFile() and u.toLocalFile().lower().endswith(PICTURE_ENDINGS)]
        if files:
            self._add_picture_files(files)
            return
        image = clipboard.image()
        if not image.isNull():
            buffer = QBuffer()
            buffer.open(QIODevice.WriteOnly)
            image.save(buffer, "PNG")
            self._add_picture(bytes(buffer.data()))

    def _add_picture_files(self, paths):
        for path in paths:
            try:
                if os.path.getsize(path) > pictures.MAX_INPUT_BYTES:
                    raise pictures.PictureError(f"{os.path.basename(path)} is too big to send.")
                with open(path, "rb") as f:
                    data = f.read()
            except pictures.PictureError as exc:
                self._toast(str(exc))
                continue
            except OSError as exc:
                self._toast(f"Couldn't read {os.path.basename(path)}: {exc.strerror or exc}")
                continue
            if not self._add_picture(data):
                break

    def _add_picture(self, data: bytes) -> bool:
        if len(self.pictures) >= pictures.MAX_PICTURES:
            self._toast(f"Up to {pictures.MAX_PICTURES} pictures per question.")
            return False
        try:
            self.pictures.append(pictures.prepare(data))
        except pictures.PictureError as exc:
            self._toast(str(exc))
            return True
        self._push_pictures()
        return True

    def on_remove_picture(self, payload):
        index = (payload or {}).get("index")
        if isinstance(index, int) and 0 <= index < len(self.pictures):
            del self.pictures[index]
            self._push_pictures()

    def _push_pictures(self):
        try:
            llm = llm_client_from_settings(self.settings)
            where = "stay on this PC" if llm.local else f"go to {llm.label} with your question"
        except Exception:  # noqa: BLE001 - settings half filled in
            where = "go to your AI provider with your question"
        self.emit("pictures", {
            "available": pictures.AVAILABLE,
            "items": [{"preview": p.preview, "label": f"{p.w} x {p.h}"} for p in self.pictures],
            "note": f"Pictures {where}. The model has to be one that can see images.",
        })

    # ------------------------------------------------------------ sending

    def on_send(self, payload):
        question = str((payload or {}).get("text") or "").strip()
        if (not question and not self.pictures) or self._sending:
            return
        if not question:
            question = "Take a look at this picture." if len(self.pictures) == 1 else "Take a look at these pictures."

        # Answered here, before the retriever is even loaded: "help" should
        # be instant and free, and it must describe what Buddy can actually
        # do rather than what a model guesses it can do.
        if is_help(question):
            self._append(YOU, question, raw=True)
            self._append(BUDDY, help_text(bool(self.settings.get("allow_project_writes", False))),
                         copyable=True)
            self._push_controls()
            return

        if self._retriever is None:
            self.on_shown()
        self._ask_to_read_project()

        self._pending_question = question
        sent, self.pictures = self.pictures, []
        self._pending_pictures = len(sent)
        self._append(YOU, question, images=[p.preview for p in sent], raw=True)
        self._push_pictures()
        self._set_sending(True)

        self._worker = _AgentWorker(self._build_agent(prefetch_focus=bool((payload or {}).get("focus"))), question, list(self.chats.history),
                                    images=[p.for_model() for p in sent] or None)
        self._worker.done.connect(self._on_done)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._clear_worker)
        self._worker.start()

    def _on_progress(self, message: str):
        """Queued from the worker thread - runs on the GUI thread."""
        if self._sending:
            self._thinking = message
            self.emit("thinking", {"on": True, "message": message})

    def _clear_worker(self):
        """done fires before finished, so _on_done has already run by now."""
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _set_sending(self, sending: bool):
        self._sending = sending
        self._thinking = "Thinking…" if sending else ""
        self.emit("thinking", {"on": sending, "message": self._thinking})
        self._push_controls()
        if not sending:
            self._push_status()

    def _on_done(self, result):
        self._set_sending(False)
        if result.error:
            error = result.error
            if self._pending_pictures:
                error += ("\n\nThis question had a picture. If the model can't read images, choose one that "
                          "can in Settings > AI > Chat assistant (a \"vision\" model – most recent GPT, Claude and "
                          "Gemini models can; for Ollama, e.g. gemma3 or llava) – or ask without it.")
            self._append(ERROR, error, error=True, copyable=True)
            return

        trace = [f"{e.name}: {e.summary}" for e in result.events]
        block = self.chats.add(BUDDY, result.answer, trace=trace, copyable=True, raw=True)
        self.emit("append", block_view(len(self.chats.blocks) - 1, block))
        self.chats.record_turn(self._pending_question, result.answer, history_limit(self.settings),
                               pictures=self._pending_pictures)
        self._persist_chats()
        self._push_controls()

        if result.proposed_action is not None:
            # Consent may have been revoked while this answer was in flight.
            if bool(self.settings.get("allow_project_writes", False)):
                self._show_proposal(result.proposed_action)
            else:
                self._append(
                    BUDDY,
                    "Project changes were turned off while I was answering, "
                    "so the proposed change was dropped – nothing was changed.",
                )

        if result.offered_tool:
            tool_id, reason = result.offered_tool
            tool = get_tool(tool_id, getattr(self.host, "registry", None))
            self._set_offer({"tool_id": tool_id, "label": f"Open {tool.name if tool else tool_id}",
                             "reason": reason})

    def _set_offer(self, offer):
        self.chats.current.offer = offer
        self._persist_chats()
        self._push_offer()

    def _push_offer(self):
        self.emit("offer", self.chats.current.offer)

    def on_open_tool(self, payload):
        tool_id = (payload or {}).get("tool_id")
        if tool_id:
            self.host.switch_tool(tool_id)

    def on_open_manual(self, payload):
        """A citation was clicked: the manual PDF, at its page."""
        try:
            page = int((payload or {}).get("page"))
        except (TypeError, ValueError):
            return
        if self._retriever is not None and self._retriever.bundle_dir:
            bundle = self._retriever.bundle_dir
        else:
            bundle = self.settings.get("bundle_dir") or default_data_paths()[0]
        pdf = manual_pdf.manual_pdf(bundle, self.settings.get("manual_pdf") or "")
        if pdf is None:
            path, _chosen = QFileDialog.getOpenFileName(
                self, tr("Where is the DaVinci Resolve manual PDF?"), "", tr_filter("PDF files (*.pdf)"))
            if not path:
                return
            pdf = Path(path)
            self._save("manual_pdf", str(pdf))
        try:
            at_page = manual_pdf.open_at_page(pdf, page)
        except OSError as exc:
            self._toast(f"Couldn't open the manual: {exc.strerror or exc}")
            return
        if not at_page:
            self._toast(f"Opened the manual - go to page {page}.")

    # ------------------------------------------------------ conversations

    def on_connection_changed(self, connected):
        # The project questions are only offered while there's a project to read.
        if self.chats.is_empty():
            self._push_transcript()
        self._push_controls()
        self._push_status()

    def _switched(self, forget_backup=False):
        """After the live conversation changes. Any pending proposal is
        dropped: it belongs to the conversation that produced it, and an
        Apply button left live while the transcript explaining it has been
        swapped out would be a good way to apply something to the wrong
        project state."""
        self._pending_question = ""
        self._hide_proposal()
        self._push_offer()
        self._push_transcript()
        self._push_controls()
        self._push_status()
        self._persist_chats(forget_backup=forget_backup)

    def on_prev_chat(self, _payload):
        if not self._sending and self.chats.go(self.chats.index - 1):
            self._switched()

    def on_next_chat(self, _payload):
        if not self._sending and self.chats.go(self.chats.index + 1):
            self._switched()

    def on_new_chat(self, _payload):
        """Start a fresh conversation, keeping the current one to go back
        to. The proposal is cleared FIRST and unconditionally, even when no
        new chat gets made: New chat leaving a live Apply button on screen
        would let a proposal be applied from a conversation the user
        believes they have closed."""
        if self._sending:
            return
        self._hide_proposal()
        if self.chats.new_chat():
            self._switched()

    def on_search_chats(self, payload):
        self._chat_query = str((payload or {}).get("query") or "")[:200]
        self._push_chat_list()

    def on_select_chat(self, payload):
        if self._sending:
            return
        try:
            index = int((payload or {}).get("index"))
        except (TypeError, ValueError):
            return
        if self.chats.go(index):
            self._switched()

    def on_rename_chat(self, payload):
        if self._sending:
            return
        if self.chats.rename(self.chats.index, (payload or {}).get("title") or ""):
            self._persist_chats()
            self._push_controls()

    def on_delete_chat(self, payload):
        """One conversation, from Chats (the view has already confirmed).
        Deleting the open one swaps in the next, like switching does."""
        if self._sending:
            return
        try:
            index = int((payload or {}).get("index"))
        except (TypeError, ValueError):
            return
        was = self.chats.current
        if not self.chats.delete(index):
            return
        if self.chats.current is not was:
            self._switched(forget_backup=True)
        else:
            self._persist_chats(forget_backup=True)
            self._push_controls()

    def on_delete_all_chats(self, _payload):
        """Every conversation, from Chats (the view has already confirmed).
        The files go first - including a damaged one that disabled saving -
        so nothing is left on disk to come back at the next launch."""
        if self._sending:
            return
        try:
            chat_store.erase(self.ask_folder)
        except OSError as exc:
            self._toast(f"Could not delete the saved conversations: {exc}")
            return
        self._chat_store_error = ""
        self._chat_query = ""
        self.chats.delete_all()
        self._switched()

    def on_explain_clip(self, _payload):
        if not self._sending:
            self.on_send({"text": "Explain the selected timeline clip, or the video clip at the playhead. Search the manual for the relevant settings and explain any mismatch or limitation you actually found.",
                          "focus": True})

    def on_run_checkup(self, _payload):
        if self._sending or self._checkup_worker is not None:
            return
        controller = self.host.controller if getattr(self.host, "connected", False) else None
        if controller is None:
            self._toast("Connect Buddy to Resolve to check the open project.")
            return
        self._append(YOU, "Check my project", raw=True)
        self._set_sending(True)
        self._checkup_worker = _CheckupWorker(controller)
        self._checkup_worker.done.connect(self._on_checkup_done)
        self._checkup_worker.finished.connect(self._clear_checkup_worker)
        self._checkup_worker.start()

    def _clear_checkup_worker(self):
        worker, self._checkup_worker = self._checkup_worker, None
        if worker is not None:
            worker.deleteLater()

    def _on_checkup_done(self, report):
        self._set_sending(False)
        if report.get("error"):
            self._append(ERROR, "Project checkup failed: " + report["error"], error=True)
            return
        if not report.get("open"):
            answer = report.get("reason") or "No project is open."
            self._append(BUDDY, answer)
            self.chats.record_turn("Check my project", answer, history_limit(self.settings))
            self._persist_chats()
            self._push_controls()
            return
        fmt = report.get("timeline_format") or {}
        edition = (report.get("resolve") or {}).get("product") or "edition unknown"
        lines = [f"**Project checkup: {report.get('project') or 'Untitled'}**",
                 f"Resolve: {edition}. Current timeline: {(report.get('timeline') or {}).get('name') or 'none'}.",
                 f"Scanned {report['scanned_clips']} video clips. Timeline: {fmt.get('fps', '?')} fps, {fmt.get('width', '?')} × {fmt.get('height', '?')}."]
        settings = report.get("settings") or {}
        if settings.get("Color science"):
            lines.append(f"Color science: {settings['Color science']}.")
        issues = report.get("issues") or []
        if issues:
            lines += ["", f"**{len(issues)} findings to review**"]
            lines += [f"- {i['location']} {i['clip']}: {'; '.join(i['details'])}" for i in issues[:50]]
            if len(issues) > 50:
                lines.append(f"- And {len(issues) - 50} more clips.")
        else:
            lines += ["", "No format mismatches or missing local source paths found in the scanned clips."]
        if report.get("recommendations"):
            lines += ["", "**Suggested next steps**"]
            lines += [f"- {item}" for item in report["recommendations"]]
        if report.get("truncated"):
            lines += ["", report["truncated"]]
        lines += ["", "This check covers current timeline video formats and local source paths. It does not verify audio, render settings, proxy availability, or color accuracy."]
        answer = "\n".join(lines)
        self._append(BUDDY, answer, copyable=True)
        self.chats.record_turn("Check my project", answer, history_limit(self.settings))
        self._persist_chats()
        self._push_controls()
        if any(issue.get("kind") == "missing_path" for issue in issues):
            tool = get_tool("media_relink", getattr(self.host, "registry", None))
            if tool and tool.is_available:
                self._set_offer({"tool_id": "media_relink", "label": "Open Media Relink",
                                 "reason": "Some source file paths could not be found on this computer."})

    def _on_history_limit_changed(self):
        self.chats.trim(history_limit(self.settings))
        self._push_controls()

    # ---------------------------------------------------- copy and export

    def on_copy(self, payload):
        try:
            block = self.chats.blocks[int((payload or {}).get("i"))]
        except (TypeError, ValueError, IndexError):
            return
        if block["copyable"]:
            QApplication.clipboard().setText(block["body"])
            self._toast("Copied to clipboard")

    def on_export(self, _payload):
        """Save the whole current conversation as plain text. The file
        dialog starts in the user's Downloads folder, with a timestamped
        default name, and lets them pick a different place."""
        downloads = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation) or str(Path.home())
        default_name = f"Buddy Chat {datetime.now().strftime('%Y-%m-%d %H%M%S')}.txt"
        path, _filter = QFileDialog.getSaveFileName(
            self, tr("Export conversation"), str(Path(downloads) / default_name), tr_filter("Text files (*.txt)")
        )
        if not path:
            return
        try:
            Path(path).write_text(self.chats.export_text(), encoding="utf-8")
        except OSError as e:
            self._toast(f"Could not save the file: {e}")
            return
        self._toast(f"Exported to {path}")

    # ------------------------------------------------- proposed changes

    def _hide_proposal(self):
        self._proposal = None
        self._push_proposal()

    def _show_proposal(self, proposal):
        """Show what would change. Applying is a separate, explicit act.

        The card lists every affected item rather than a count, because the
        failure this exists to catch is the model picking the wrong items -
        and "add 12 markers" looks identical whether the 12 are right or
        wrong.
        """
        self._proposal = proposal
        self._proposal_note = ""
        self._proposal_state = "ready"
        self._push_proposal()

    def _push_proposal(self):
        proposal = self._proposal
        if proposal is None:
            self.emit("proposal", None)
            return
        destructive = bool(getattr(proposal, "destructive", False))
        note = self._proposal_note or (
            "Nothing has been changed yet. Buddy cannot undo this – use Resolve's own Edit > Undo."
            if destructive else "Nothing has been changed yet."
        )
        self.emit("proposal", {
            "title": proposal.summary,
            "destructive": destructive,
            "reason": proposal.reason or "",
            # Warnings go ABOVE the item list: they say what happens to
            # things the user did NOT name, which is what decides whether
            # Apply is safe.
            "warnings": list(getattr(proposal, "warnings", None) or []),
            "details": list(proposal.details),
            "note": note,
            "apply_label": "Apply anyway" if destructive else "Apply to my project",
            "state": self._proposal_state,
        })

    def on_discard_proposal(self, _payload):
        if self._proposal is None:
            return
        self._append(BUDDY, "Discarded – nothing was changed.")
        self._hide_proposal()

    def _on_writes_revoked(self):
        # A card already on screen was proposed under the consent just
        # withdrawn, so it goes too (Apply also re-checks the setting).
        if self._proposal is not None:
            self._hide_proposal()
            self._append(
                BUDDY,
                "Project changes were turned off, so the pending change was cancelled – nothing was changed.",
            )

    def on_apply_proposal(self, _payload):
        """Run the proposal. The ONLY place in this page that writes.

        Runs on the GUI thread: these are short calls, and the worker
        thread must never touch Resolve on this page's behalf (see
        _resolve_source). The card goes busy first so a double-click
        cannot apply the same change twice.
        """
        proposal = self._proposal
        if proposal is None or self._proposal_state != "ready":
            return
        # Consent is re-read here, not trusted from when the card appeared:
        # revoking it must stop a change that is already on screen.
        if not bool(self.settings.get("allow_project_writes", False)):
            self._append(
                BUDDY,
                "Project changes are turned off in Settings, so the proposed "
                "change was cancelled – nothing was changed.",
            )
            self._hide_proposal()
            return
        self._proposal_state = "busy"
        self._push_proposal()

        try:
            controller = self.host.ensure_connected()
        except ResolveConnectionError as e:
            self._proposal_state = "ready"
            self._proposal_note = f"Could not reach Resolve: {e}"
            self._push_proposal()
            return

        lines = []
        self.host.set_busy(True, "Applying the change…")
        try:
            outcome = actions.execute(controller, proposal, log=lines.append)
        except Exception as e:  # noqa: BLE001 - shown to the user
            self._proposal_state = "failed"
            self._proposal_note = f"Failed: {e}"
            self._append(BUDDY, f"The change failed: {e}", error=True)
            self._push_proposal()
            return
        finally:
            self.host.set_busy(False)

        detail = "\n".join(f"- {line}" for line in lines)
        self._append(BUDDY, f"**Applied.** {outcome}" + (f"\n\n{detail}" if detail else ""),
                     copyable=True)
        self._hide_proposal()

    # ------------------------------------------------------------ settings

    def _save(self, key, value):
        """ToolSettings holds values in memory until save() is called, and
        the shared dialog has no per-field apply - so persist on edit."""
        self.settings[key] = value
        self.settings.save()
