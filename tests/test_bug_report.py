"""Report a bug (core/bug_report.py): what goes with a report, the requests
it takes - checked against the real server logic (server/core.py and
bugs.py) with fake sessions - the sender over Buddy Network's connection or
its own, and the window. Never a real server or the real clipboard."""

import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

import _paths  # noqa: F401
from core import app_version
from server import bugs as server_bugs, core as server_core
from server.store import Store

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
    from PySide6.QtWidgets import QApplication, QWidget
    from core import bug_report
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def png(w=900, h=600) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (40, 90, 160)).save(out, "PNG")
    return out.getvalue()


class ServerSession(server_core.Session):
    def __init__(self):
        super().__init__("10.0.0.1")
        self.inbox = []

    def send(self, payload):
        self.inbox.append(payload)


class VersionTests(unittest.TestCase):
    def test_buddy_knows_its_version(self):
        with open(os.path.join(_paths.APP.parent, "VERSION"), encoding="utf-8") as f:
            self.assertEqual(app_version.buddy_version(), f.read().strip())
        with tempfile.TemporaryDirectory() as folder:
            nothing = os.path.join(folder, "VERSION")
            with mock.patch.object(app_version, "_PLACES", (nothing,)):
                self.assertEqual(app_version.buddy_version(), "")
            with open(nothing, "w", encoding="utf-8") as f:
                f.write("<script>\n")
            with mock.patch.object(app_version, "_PLACES", (nothing,)):
                self.assertEqual(app_version.buddy_version(), "")   # not a version number

    def test_the_zip_carries_it(self):
        import build_buddy_zip as b
        with tempfile.TemporaryDirectory() as folder:
            out = os.path.join(folder, "buddy.zip")
            with mock.patch.object(b, "OUTPUT_ZIP", out), mock.patch("builtins.print"):
                written = b.build()
            self.assertIn("buddy/VERSION", written)
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.read("buddy/VERSION").decode().strip(), app_version.buddy_version())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ReportTests(unittest.TestCase):
    def test_in_step_with_the_server(self):
        from pages.buddy_network import panels
        self.assertEqual(bug_report.BUG_TEXT_MAX, server_bugs.BUG_TEXT_MAX)
        self.assertEqual(bug_report.BUG_IMAGES_MAX, server_bugs.BUG_IMAGES_MAX)
        self.assertEqual(bug_report.PART_CHARS, server_core.IMAGE_PART_CHARS)
        self.assertEqual([k for k, _ in bug_report.DETAIL_LABELS], list(server_bugs.BUG_DETAILS))
        self.assertEqual(bug_report.DETAIL_LABELS, panels.BUG_DETAILS)
        # The server gives a report without a hello at least as long as Buddy waits.
        self.assertGreaterEqual(server_bugs.BUG_REPORT_TIMEOUT * 1000, bug_report.TIMEOUT_MS)
        try:
            from server import net
        except ImportError:   # its websockets package is the server's alone - not needed to test Buddy
            return
        self.assertLess(1000 / bug_report.PART_EVERY_MS, net.FRAME_RATE)

    def test_details(self):
        class Page:
            def __init__(self, name):
                self.display_name = name

        class Shell:
            connected = True
            controller = type("C", (), {"about": "DaVinci Resolve Studio 20.1.0.21"})()

            def pages_on_screen(self):
                return [Page("Transcribe"), Page("Asset Manager")]

        found = bug_report.details(Shell())
        self.assertEqual(found["resolve"], "DaVinci Resolve Studio 20.1.0.21")
        self.assertEqual(found["tool"], "Transcribe + Asset Manager")
        self.assertEqual(found["buddy"], app_version.buddy_version())
        self.assertTrue(found["windows"])
        shell = Shell()
        shell.connected = False
        self.assertEqual(bug_report.details(shell)["resolve"], "Not connected")
        self.assertEqual([r["label"] for r in bug_report.detail_rows(found)], ["Buddy", "System", "Resolve", "Tool"])

    def test_system_version(self):
        with mock.patch.object(bug_report.sys, "platform", "darwin"), \
             mock.patch.object(bug_report.platform, "mac_ver", return_value=("15.1", ("", "", ""), "arm64")):
            self.assertEqual(bug_report.system_version(), "macOS 15.1")
        from types import SimpleNamespace
        with mock.patch.object(bug_report.sys, "platform", "win32"), \
             mock.patch.object(bug_report.sys, "getwindowsversion", create=True,
                               return_value=SimpleNamespace(major=10, build=26200)):
            self.assertEqual(bug_report.system_version(), "Windows 11 (build 26200)")
        with mock.patch.object(bug_report.sys, "platform", "win32"), \
             mock.patch.object(bug_report.sys, "getwindowsversion", create=True,
                               return_value=SimpleNamespace(major=10, build=19045)), \
             mock.patch.object(bug_report.platform, "release", return_value="10"):
            self.assertEqual(bug_report.system_version(), "Windows 10 (build 19045)")

    def test_the_frames_are_what_the_server_takes(self):
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + os.urandom(70_000)
        shots = [{"data": webp, "w": 1200, "h": 800}, {"data": webp[:5000], "w": 10, "h": 10}]
        payloads = bug_report.frames("It froze", {"buddy": "1.1.27"}, shots)
        parts = [p for p in payloads if p["type"] == "bug_part"]
        self.assertTrue(all(len(p["data"]) <= server_core.IMAGE_PART_CHARS for p in parts))
        self.assertEqual(payloads[-1]["type"], "bug_report")
        self.assertEqual(len(payloads[-1]["images"]), 2)
        store = Store(":memory:")
        self.addCleanup(store.close)
        core = server_core.NetworkCore(store)
        session = ServerSession()
        for p in payloads:
            core.handle(session, json.dumps(p))
        self.assertEqual(session.inbox, [{"type": "bug_reported"}])
        [report] = store.bug_reports()
        self.assertEqual((report["text"], report["details"]), ("It froze", {"buddy": "1.1.27"}))
        self.assertEqual(store.bug_image(report["images"][0]["id"])["data"], webp)


class FakeClient(QObject if HAVE_QT else object):
    """Buddy Network's connection, straight into the real server logic."""
    if HAVE_QT:
        state_changed = Signal(str, str)
        received = Signal(dict)

    def __init__(self, core, session):
        super().__init__()
        self.state, self.core, self.session = "online", core, session

    def send(self, payload):
        if self.state != "online":
            return False
        before = len(self.session.inbox)
        self.core.handle(self.session, json.dumps(payload))
        for answer in self.session.inbox[before:]:
            self.received.emit(answer)
        return True


class ServerHarness(unittest.TestCase):
    """A signed-in Buddy Network connection, straight into the real server logic."""

    def setUp(self):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        self.app = QApplication.instance() or QApplication([])
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.core = server_core.NetworkCore(self.store, limit_new_accounts=False)
        self.session = ServerSession()
        hello = {"type": "hello", "v": server_core.PROTOCOL_VERSION}
        self.core.handle(self.session, json.dumps(hello))
        self.core.handle(self.session, json.dumps({"type": "set_name", "name": "Tess"}))
        self.client = FakeClient(self.core, self.session)

    def wait_for(self, done, seconds=20):
        import time
        end = time.monotonic() + seconds
        while not done() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(done())



@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SendingTests(ServerHarness):
    def send(self, payloads):
        results, progress = [], []
        sender = bug_report.BugReportSender(payloads, client=self.client)
        sender.finished.connect(results.append)
        sender.progress.connect(progress.append)
        sender.start()
        self.wait_for(lambda: results)
        return results[0], progress

    def test_over_buddy_networks_connection_it_says_who(self):
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + os.urandom(60_000)
        with mock.patch.object(bug_report, "PART_EVERY_MS", 1):
            problem, progress = self.send(bug_report.frames("hi", {}, [{"data": webp, "w": 5, "h": 5}]))
        self.assertEqual(problem, "")
        self.assertEqual(progress[-1], 100)
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(self.store.bug_reports()[0]["reporter"], self.session.user_id)

    def test_the_servers_answer_is_what_the_window_says(self):
        problem, _ = self.send(bug_report.frames("", {}, []))
        self.assertEqual(problem, "The report is empty.")
        self.client.state = "off"
        problem, _ = self.send(bug_report.frames("hi", {}, []))
        self.assertEqual(problem, bug_report.LOST)


class FakeShell(QWidget if HAVE_QT else object):
    """What the window needs of the shell: its theme, the details and Buddy Network's connection."""

    def __init__(self, client=None, me=None):
        super().__init__()
        self._client, self._me = client, me
        self.connected, self.controller = False, None
        self.shared_settings = {"theme": "Resolve"}

    def theme_tokens(self):
        from core.theme import get_theme_tokens
        return get_theme_tokens("Resolve")

    def pages_on_screen(self):
        return []

    def network_connection(self):
        return self._client, self._me

    def network_server_url(self):
        return "ws://localhost:1"


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class WindowTests(ServerHarness):
    def open(self, shell):
        self.addCleanup(shell.deleteLater)
        dialog = bug_report.BugReportDialog(shell)
        self.addCleanup(lambda: dialog.done(0))   # stops sending, waits for its threads
        self.events = []
        dialog.emit = lambda name, payload=None: self.events.append((name, payload))
        return dialog

    def last(self):
        return [p for n, p in self.events if n == "bug"][-1]

    def test_screenshots_words_and_sending(self):
        me = {"id": self.session.user_id, "tag": self.session.user_id[:6], "name": "Tess"}
        dialog = self.open(FakeShell(self.client, me))
        dialog.web_ready()
        self.assertIn("Tess", self.last()["who"])
        dialog.on_send({"text": "   "})
        self.assertEqual(self.last()["error"], "Say what went wrong, or add a screenshot.")
        dialog._add(png(3000, 2000))
        self.assertEqual(self.last()["preparing"], 1)
        dialog.on_send({"text": "early"})
        self.assertIn("still being prepared", self.last()["error"])
        self.wait_for(lambda: dialog.shots)
        shot = dialog.shots[0]
        self.assertEqual(max(shot["w"], shot["h"]), bug_report.BUG_SIDE)   # bigger than a chat picture
        self.assertTrue(self.last()["shots"][0]["preview"].startswith("data:image/webp;base64,"))
        dialog._add(b"not a picture")
        self.wait_for(lambda: not dialog._preparing)
        self.assertTrue(self.last()["error"])
        self.assertEqual(len(dialog.shots), 1)
        dialog.on_remove({"index": 5})
        dialog.on_remove({"index": True})
        self.assertEqual(len(dialog.shots), 1)
        with mock.patch.object(bug_report, "PART_EVERY_MS", 1):
            dialog.on_send({"text": "Transcribe froze"})
            self.wait_for(lambda: self.last()["sent"])
        self.assertEqual(self.last()["error"], "")
        [report] = self.store.bug_reports()
        self.assertEqual((report["text"], len(report["images"])), ("Transcribe froze", 1))
        # Sent: nothing more goes, whatever is clicked.
        dialog.on_send({"text": "again"})
        dialog._add_files([])
        self.assertEqual(len(self.store.bug_reports()), 1)

    def test_screenshots_keep_the_order_they_were_added_in(self):
        dialog = self.open(FakeShell())
        dialog._add(png(4000, 3000))   # slow to shrink...
        dialog._add(png(40, 30))       # ...so this one is ready first
        self.wait_for(lambda: len(dialog.shots) == 2)
        self.assertEqual([s["w"] for s in dialog.shots], [bug_report.BUG_SIDE, 40])

    def test_no_more_than_six_and_only_pictures_dropped(self):
        dialog = self.open(FakeShell())
        dialog.web_ready()
        self.assertIn("without a name", self.last()["who"])
        dialog.on_files_dropped(["C:/notes.txt"])
        self.assertIn("Only pictures", self.last()["error"])
        with mock.patch.object(bug_report, "_ShrinkWorker") as worker:
            for _ in range(bug_report.BUG_IMAGES_MAX + 1):
                dialog._add(b"x")
        self.assertEqual(worker.call_count, bug_report.BUG_IMAGES_MAX)
        self.assertFalse(self.last()["can_add"])
        self.assertIn("6 screenshots at most", self.last()["error"])


if __name__ == "__main__":
    unittest.main()
