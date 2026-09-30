"""Buddy's own updates (core/updater.py, build_update_manifest.py): what a
release offers, what's refused, putting it in place and rolling it back -
against a fake GitHub, in temporary folders, never the real Scripts folder."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _paths

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication
    from core import updater
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def buddy_zip(version, extra=b""):
    """The bytes of a minimal Buddy zip of this version."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("buddy/main.py", "def main(start_hidden=False): pass\n")
        archive.writestr("buddy/VERSION", version + "\n")
        if extra:
            archive.writestr("buddy/extra.bin", extra)
    return out.getvalue()


def entry(data):
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def manifest(version, zip_data, launcher=None, requires=(), notes="• Better things", platform=None):
    doc = {"format": 1, "platform": platform or updater.PLATFORM, "version": version, "zip": entry(zip_data),
           "requires": list(requires), "notes": notes}
    if launcher is not None:
        doc["launcher"] = entry(launcher)
    return json.dumps(doc).encode("utf-8")


class Settings(dict):
    saves = 0

    def save(self):
        self.saves += 1


class FakeGitHub:
    """updater's fetch: URLs -> bytes; anything else is a 404."""

    def __init__(self):
        self.files, self.asked = {}, []

    def get(self, url, done, progress=None, to_file=None, limit=0):
        self.asked.append(url)
        data = self.files.get(url)
        if data is None:
            return done(None, "404 Not Found")
        if progress:
            progress(len(data) // 2, len(data))
            progress(len(data), len(data))
        if to_file:
            Path(to_file).write_bytes(data)
            return done(Path(to_file), "")
        done(data, "")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PieceTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updater.version_key("1.1.33"), (1, 1, 33))
        self.assertTrue(updater.is_newer("1.1.33", "1.1.32"))
        self.assertTrue(updater.is_newer("1.2", "1.1.99"))
        self.assertFalse(updater.is_newer("1.1.32", "1.1.32"))
        self.assertFalse(updater.is_newer("0.3.26", "1.1.32"))
        self.assertFalse(updater.is_newer("", "1.1.32"))

    def test_a_manifest_is_checked_before_it_is_believed(self):
        data = buddy_zip("1.1.33")
        good = updater.parse_manifest(manifest("1.1.33", data))
        self.assertEqual((good["version"], good["zip"]["size"]), ("1.1.33", len(data)))
        self.assertTrue(good["page"].endswith("/releases/tag/v1.1.33"))
        other = "mac" if updater.PLATFORM == "windows" else "windows"
        self.assertIsNone(updater.parse_manifest(manifest("1.1.33", data, platform=other)))    # the other Buddy's
        doc = json.loads(manifest("1.1.33", data))
        for broken in ({"zip": {"size": 10, "sha256": "not-hex"}}, {"zip": {"size": 0, "sha256": "0" * 64}},
                       {"format": 2}, {"version": "soon"}, {"zip": None}):
            with self.subTest(broken):
                self.assertIsNone(updater.parse_manifest(json.dumps(dict(doc, **broken)).encode()))
        self.assertIsNone(updater.parse_manifest(b"x" * (updater.MANIFEST_MAX + 1)))
        self.assertIsNone(updater.parse_manifest(b"<html>404</html>"))

    def test_only_github_over_https(self):
        for url, ok in (("https://github.com/a/b/releases/download/v1/buddy.zip", True),
                        ("https://objects.githubusercontent.com/x", True),
                        ("https://release-assets.githubusercontent.com/x", True),
                        ("http://github.com/a", False), ("https://github.com.evil.example/a", False),
                        ("https://example.com/buddy.zip", False)):
            with self.subTest(url):
                self.assertEqual(updater.allowed_url(url), ok)

    def test_packages_the_installer_would_have_to_add(self):
        have = {"PySide6": "6.9.1", "numpy": "1.26.4", "pillow": None}.get
        reqs = [{"name": "PySide6", "spec": ">=6.8,<7", "marker": ""},
                {"name": "numpy", "spec": ">=2", "marker": ""},
                {"name": "pillow", "spec": ">=10", "marker": ""},
                {"name": "pyobjc-framework-Cocoa", "spec": ">=9", "marker": 'sys_platform == "nonesuch"'}]
        self.assertEqual(updater.missing_requirements(reqs, have), ["numpy>=2", "pillow>=10"])
        self.assertEqual(updater.missing_requirements([{"name": "PySide6", "spec": "<6.9", "marker": ""}], have),
                         ["PySide6<6.9"])
        self.assertEqual(updater.missing_requirements([], have), [])

    def test_which_zip_this_buddy_runs_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / "Scripts"
            scripts.mkdir()
            (scripts / "buddy.zip").write_bytes(buddy_zip("1.1.32"))
            unpacked = Path(tmp) / "Temp" / "Buddy" / "0123456789abcdef" / "buddy"
            with mock.patch.object(updater, "scripts_dir", lambda: scripts), mock.patch.dict(os.environ, {"BUDDY_ZIP": ""}):
                self.assertEqual(updater.installed_zip(unpacked), scripts / "buddy.zip")
                self.assertIsNone(updater.installed_zip(Path(tmp) / "repo" / "app"))    # the source folder
            with mock.patch.dict(os.environ, {"BUDDY_ZIP": str(scripts / "buddy.zip")}):
                self.assertEqual(updater.installed_zip(Path(tmp) / "anywhere"), scripts / "buddy.zip")

    def test_a_download_has_to_be_exactly_what_the_release_says(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buddy.zip.download"
            data = buddy_zip("1.1.33")
            path.write_bytes(data)
            self.assertEqual(updater.check_download(path, entry(data), "1.1.33"), "")
            self.assertIn("bytes", updater.check_download(path, {"size": 5, "sha256": "0" * 64}, "1.1.33"))
            self.assertIn("SHA-256", updater.check_download(path, {"size": len(data), "sha256": "0" * 64}, "1.1.33"))
            self.assertIn("1.1.34", updater.check_download(path, entry(data), "1.1.34"))
            other = io.BytesIO()
            with zipfile.ZipFile(other, "w") as archive:
                archive.writestr("something/else.py", "")
            path.write_bytes(other.getvalue())
            self.assertIn("isn't a Buddy", updater.check_download(path, entry(other.getvalue()), "1.1.33"))
            path.write_bytes(b"not a zip")
            self.assertIn("isn't a zip", updater.check_download(path, entry(b"not a zip"), "1.1.33"))

    def test_put_in_place_keeps_the_old_one_and_roll_back_swaps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "buddy.zip").write_bytes(buddy_zip("1.1.32"))
            (folder / "Buddy.py").write_text("old launcher")
            new = folder / "buddy.zip.download"
            new.write_bytes(buddy_zip("1.1.33"))
            updater.put_in_place(new, folder / "buddy.zip")
            new_launcher = folder / "Buddy.py.download"
            new_launcher.write_text("new launcher")
            updater.put_in_place(new_launcher, folder / "Buddy.py")
            self.assertEqual(updater.zip_version(folder / "buddy.zip"), "1.1.33")
            self.assertEqual(updater.previous_version(folder / "buddy.zip"), "1.1.32")
            self.assertFalse(new.exists())
            self.assertEqual(updater.roll_back(folder / "buddy.zip"), "1.1.32")
            self.assertEqual((folder / "Buddy.py").read_text(), "old launcher")
            self.assertEqual(updater.previous_version(folder / "buddy.zip"), "1.1.33")   # forward again, if wanted
            self.assertEqual((folder / "Buddy.py.previous").read_text(), "new launcher")

    def test_the_manifest_the_release_writes_is_one_buddy_reads(self):
        sys.path.insert(0, str(_paths.APP.parent))
        self.addCleanup(sys.path.remove, str(_paths.APP.parent))
        import build_update_manifest as bum
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text("# comment\nPySide6>=6.8,<7\npillow >= 10   # images\n"
                           'pyobjc-framework-Cocoa>=9; sys_platform == "darwin"   # Mac\n\n', encoding="utf-8")
            reqs = bum.requirements(req)
        self.assertEqual(reqs, [{"name": "PySide6", "spec": ">=6.8,<7", "marker": ""},
                                {"name": "pillow", "spec": ">=10", "marker": ""},
                                {"name": "pyobjc-framework-Cocoa", "spec": ">=9", "marker": 'sys_platform == "darwin"'}])
        data = buddy_zip("9.9.9")
        doc = {"format": 1, "platform": updater.PLATFORM, "version": "9.9.9", "zip": entry(data),
               "launcher": entry(b"stub"), "requires": reqs, "notes": "• One thing"}
        self.assertEqual(updater.parse_manifest(json.dumps(doc).encode())["requires"], reqs)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.app = QCoreApplication.instance() or QCoreApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.zip = self.folder / "buddy.zip"
        self.zip.write_bytes(buddy_zip("1.1.32"))
        (self.folder / "Buddy.py").write_text("launcher 1")
        self.github = FakeGitHub()
        self.settings = Settings()
        patcher = mock.patch.object(updater, "buddy_version", lambda: "1.1.32")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.checker = updater.UpdateChecker(self.settings, fetch=self.github, zip_path=self.zip)
        self.events = []
        self.checker.installed.connect(lambda v: self.events.append(("installed", v)))
        self.checker.failed.connect(lambda why: self.events.append(("failed", why)))
        self.checker.progress.connect(lambda text: self.events.append(("progress", text)))

    def publish(self, version, zip_data=None, launcher=b"launcher 2", requires=(), zip_bytes_served=None):
        zip_data = zip_data or buddy_zip(version)
        self.github.files[updater.MANIFEST_URL] = manifest(version, zip_data, launcher, requires)
        self.github.files[updater.asset_url(version, "buddy.zip")] = zip_bytes_served or zip_data
        if launcher is not None:
            self.github.files[updater.asset_url(version, "Buddy.py")] = launcher

    def check(self):
        answers = []
        self.checker.check_now(answers.append)
        return answers[-1]

    def test_nothing_offered_until_a_newer_release(self):
        self.assertEqual(self.check(), "The latest release has no update information.")   # 1.1.32's has none
        self.assertIsNone(self.checker.header_state())
        self.publish("1.1.32")
        self.assertEqual(self.check(), "")
        self.assertIsNone(self.checker.header_state())                  # the one running
        self.publish("1.1.33")
        self.check()
        self.assertEqual(self.checker.header_state(), {"kind": "update", "version": "1.1.33"})
        self.assertEqual(self.checker.offer()["notes"], "• Better things")

    def test_an_update_goes_in_place_and_waits_for_a_restart(self):
        self.publish("1.1.33")
        self.check()
        self.checker.download_and_install()
        self.assertEqual(self.events[-1], ("installed", "1.1.33"))
        self.assertTrue(any(k == "progress" and "100%" in t for k, t in self.events))
        self.assertEqual(updater.zip_version(self.zip), "1.1.33")
        self.assertEqual((self.folder / "Buddy.py").read_text(), "launcher 2")        # it changed too
        self.assertEqual(self.checker.header_state(), {"kind": "restart", "version": "1.1.33"})
        self.assertEqual(self.checker.previous(), "1.1.32")
        self.assertFalse((self.folder / "buddy.zip.download").exists())

    def test_an_unchanged_launcher_isnt_downloaded(self):
        self.publish("1.1.33", launcher=b"launcher 1")
        self.check()
        self.checker.download_and_install()
        self.assertNotIn(updater.asset_url("1.1.33", "Buddy.py"), self.github.asked)

    def test_a_tampered_download_changes_nothing(self):
        self.publish("1.1.33", zip_bytes_served=buddy_zip("1.1.33", extra=b"surprise"))
        self.check()
        before = self.zip.read_bytes()
        self.checker.download_and_install()
        kind, why = self.events[-1]
        self.assertEqual(kind, "failed")
        self.assertIn("refused", why)
        self.assertEqual(self.zip.read_bytes(), before)
        self.assertFalse((self.folder / "buddy.zip.download").exists())
        self.assertFalse((self.folder / "buddy.zip.previous").exists())
        self.assertEqual(self.checker.header_state(), {"kind": "update", "version": "1.1.33"})   # can try again

    def test_a_release_needing_new_packages_goes_to_the_installer(self):
        self.publish("1.1.33", requires=[{"name": "no-such-package-for-buddy", "spec": ">=1"}])
        self.check()
        offer = self.checker.offer()
        self.assertEqual(offer["missing"], ["no-such-package-for-buddy>=1"])
        self.checker.download_and_install()
        self.assertEqual(self.events, [])                                  # nothing downloaded
        self.assertNotIn(updater.asset_url("1.1.33", "buddy.zip"), self.github.asked)

    def test_roll_back_and_not_being_offered_it_again(self):
        self.publish("1.1.33")
        self.check()
        self.checker.download_and_install()
        self.assertEqual(self.checker.roll_back(), "1.1.32")
        self.assertEqual(updater.zip_version(self.zip), "1.1.32")
        self.assertIsNone(self.checker.header_state())                    # 1.1.32 in place and running
        self.checker._check()                                             # the daily check
        self.assertIsNone(self.checker.offer())                           # 1.1.33 skipped...
        self.check()                                                      # ...until asked for
        self.assertEqual(self.checker.header_state(), {"kind": "update", "version": "1.1.33"})

    def test_off_means_no_checking_and_no_button(self):
        self.publish("1.1.33")
        self.check()
        self.checker.set_enabled(False)
        self.assertIsNone(self.checker.header_state())
        self.assertFalse(self.settings["updates_enabled"])
        asked = len(self.github.asked)
        self.checker._maybe_check()
        self.assertEqual(len(self.github.asked), asked)

    def test_from_the_source_folder_there_is_nothing_to_update(self):
        checker = updater.UpdateChecker(Settings(), fetch=self.github, zip_path=None)
        self.assertFalse(checker.supported)
        self.assertIsNone(checker.header_state())
        answers = []
        checker.check_now(answers.append)
        self.assertIn("source folder", answers[-1])
        self.assertEqual(self.github.asked, [])

    def test_settings_show_updates_and_roll_back(self):
        from core.settings_form import update_fields
        fields = update_fields(self.settings, self.checker)
        self.assertIn("updates_enabled", json.dumps(fields))
        self.assertNotIn("roll_back_update", json.dumps(fields))
        self.publish("1.1.33")
        self.check()
        self.checker.download_and_install()
        self.assertIn("Roll back to 1.1.32", json.dumps(update_fields(self.settings, self.checker)))
        source = update_fields(self.settings, updater.UpdateChecker(Settings(), fetch=self.github, zip_path=None))
        self.assertIn("source folder", json.dumps(source))
        self.assertEqual(update_fields(self.settings, None), [])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class RelaunchTests(unittest.TestCase):
    def test_the_helper_waits_for_buddy_to_quit_then_starts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "started.txt"
            launcher = Path(tmp) / "Buddy.py"
            launcher.write_text(f"open({str(marker)!r}, 'w').write('ok')\n", encoding="utf-8")
            quitting = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
            started = time.monotonic()
            subprocess.run([sys.executable, "-c", updater._RELAUNCH, str(quitting.pid), sys.executable, str(launcher)],
                           timeout=30)
            waited = time.monotonic() - started
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            quitting.wait()
            self.assertTrue(marker.exists())
            self.assertGreater(waited, 1.0)                                # not before it had quit

    def test_no_python_to_start_it_with(self):
        with mock.patch("core.startup_manager.python_for_relaunch", lambda: None):
            self.assertFalse(updater.relaunch_after_quit(Path(__file__)))


if __name__ == "__main__":
    unittest.main()
