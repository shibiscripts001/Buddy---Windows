"""Buddy Network's rules (accepted before first use - the web page shows
them) and the clipboard helper. Every Buddy Network window is in the web
page now (web/network.js): the name, room, link and send-check questions
come from page.py and safety.py, the account, safety code, avatar,
transfer, saved chats, ban and admin panels from panels.py."""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication

# Bump when the rules change in a way people should re-read: everyone is
# asked to accept them again the next time they open Buddy Network.
RULES_VERSION = 4       # 2: messages kept 60 days. 3: DMs end-to-end encrypted. 4: back to 30 days

RULES_HTML = """
<h3>Buddy Network rules</h3>
<p>Buddy Network is a public text chat for people who use Buddy. Please read
this before you join.</p>
<ul>
<li><b>13 or older only.</b></li>
<li><b>Be kind.</b> No harassment, hate, threats, spam or anything illegal.</li>
<li><b>Keep yourself private.</b> Never share passwords, API keys, licence
keys, or personal details: your real name, address, phone number, email,
where you work or go to school. Don't use your real name as your Buddy
Network name.</li>
<li><b>Links can be dangerous.</b> Buddy warns you before opening any link.
Only open links from people you trust, never type a password into a site
you reached from a chat link, and don't run files that people link to.</li>
<li><b>Anyone can claim to be anyone.</b> Nobody who runs Buddy Network
will ever ask for your password or keys.</li>
<li><b>Rooms are public and not end-to-end encrypted.</b> They're encrypted
on the way to the server, but the people who run the server could read
them.</li>
<li><b>Direct messages are end-to-end encrypted</b>: only you and your
buddy can read them – not the server, and not the people who run it. Your
buddy can still copy or share what you send, so the rules above apply to
DMs too.</li>
<li><b>Messages are kept for 30 days</b>, then deleted. You can delete your
own messages at any time.</li>
<li>Other users never see your IP address, and the server doesn't store it.</li>
</ul>
"""


def copy_to_clipboard(text: str):
    QGuiApplication.clipboard().setText(text)
