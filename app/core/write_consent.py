#!/usr/bin/env python3
"""
The gate between Buddy's chat agent and the user's actual project.

Everything Ask Buddy could do before this module existed was read-only, by
deliberate design (see pages/manual_chat/resolve_ext.py). Writing is a
different category of risk: a wrong sentence in a chat window is a bad
answer, while a wrong Resolve call is lost work. So enabling it is not a
checkbox - it is a checkbox plus a sentence the user has to type out.

Two rules this module enforces, both by construction rather than by
remembering to check:

  Typing beats clicking.  A checkbox can be hit by accident, by muscle
                          memory, or by someone skimming a settings panel.
                          Copying a sentence out cannot be done without
                          reading it, which is the entire point - the
                          warning above the field is what we actually want
                          read, and the typing is what buys the seconds to
                          read it.

  Consent is never cached. There is no "you already agreed once" state
                           anywhere in here. Turning the setting off and
                           on again means typing the sentence again,
                           because the off state is the user saying no and
                           a later yes has to be its own decision.

The window is deliberately plain about what can go wrong. Softening it
would defeat the purpose, and a user who turns this on should be picturing
a damaged project, not a helpful assistant.

A web window (app/web/shell/consent/). The view shows how the typing is
going, but the check that counts is matches() here, when Enable is pressed.

Protocol:
    to the view    consent, typed
    from the view  type, enable, cancel
"""

import os

from PySide6.QtWidgets import QDialog

from core.web_page import WEB_COMMON_DIR, WebDialog, _theme_host

# Compared exactly, after trimming only the whitespace around it. Lower-case
# "buddy" is intentional and matches what the dialog displays; the field is
# checked against this string and nothing else, so there is no chance of a
# near-miss being accepted.
CONSENT_SENTENCE = "I allow buddy to touch my project."

WARNING_TITLE = "Read this before you enable it"

WARNING_BODY = [
    "Buddy's chat agent is an AI. It is confidently wrong on a regular "
    "basis, and it cannot tell when it is.",
    "It can misread your timeline, pick the wrong clip, act on a question "
    "you did not mean literally, or follow an instruction hidden in a file "
    "name it read. None of that looks like an error while it happens.",
    "Mistakes here cost work, not words. Markers, clip names, bins and "
    "timelines can all be changed or destroyed, and a bad change made "
    "across many clips at once is not something you will notice "
    "immediately.",
    "Resolve's undo does not cover everything, and Buddy cannot undo "
    "anything for you.",
    "Do not enable this on a project you cannot afford to lose. Save a "
    "copy of it first.",
]


def matches(text):
    """Leading/trailing space is an artefact of typing, never of intent.
    Everything else - case, the full stop, the spacing between words - has
    to be right, because the point of the exercise is that the sentence
    was read rather than approximated."""
    return (text or "").strip() == CONSENT_SENTENCE


def status(text):
    """What the line under the field says about what's typed so far."""
    typed = (text or "").strip()
    if matches(text):
        return "Sentence matches."
    if not typed:
        return ""
    if typed.lower() == CONSENT_SENTENCE.lower():
        # BEFORE the prefix check below. A capitalisation-only error is the
        # same LENGTH as the sentence, so the prefix branch would claim it
        # and announce "0 characters to go".
        return "Almost – the capitalisation has to match exactly."
    if CONSENT_SENTENCE.lower().startswith(typed.lower()):
        # Still on track - say so, rather than flashing a failure at
        # someone mid-way through typing it correctly.
        return f"Keep going – {len(CONSENT_SENTENCE) - len(typed)} characters to go."
    return "That does not match the sentence above."


class WriteConsentDialog(WebDialog):
    web_dir = os.path.join(WEB_COMMON_DIR, "shell", "consent")

    def __init__(self, parent=None):
        super().__init__(_theme_host(parent), parent, "Allow Buddy to change your project", (560, 560))

    def web_ready(self):
        self.emit("consent", {"title": WARNING_TITLE, "body": WARNING_BODY, "sentence": CONSENT_SENTENCE})

    def on_type(self, payload):
        text = str((payload or {}).get("text") or "")
        self.emit("typed", {"ok": matches(text), "status": status(text)})

    def on_enable(self, payload):
        # Checked here, never trusting the view's button state.
        if matches(str((payload or {}).get("text") or "")):
            self.accept()

    def on_cancel(self, _payload=None):
        self.reject()

    @classmethod
    def obtain(cls, parent=None) -> bool:
        """Show the gate. True only if the sentence was typed correctly."""
        return cls(parent).exec() == QDialog.Accepted
