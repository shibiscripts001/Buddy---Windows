"""Transcribe's downloads are pinned and checked (env_setup.py,
tools/pin_transcribe.py): every model to a commit and its files' SHA-256,
every package to a version and its wheels' SHA-256, installed by pip with
--require-hashes --only-binary :all: --no-deps, scanned by Windows Defender.
No network, nothing installed: pip, the downloader and the scan are stood in for."""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from core import defender  # noqa: E402
from pages.transcribe import env_setup as es  # noqa: E402
from pages.transcribe import plan  # noqa: E402


class PinTests(unittest.TestCase):
    def test_every_model_offered_is_pinned_to_a_commit_with_every_files_checksum(self):
        pins = es.model_pins()
        for m in es.MODELS + es.TRANSLATION_MODELS:
            pin = pins[m["id"]]
            self.assertEqual(pin["repo"], m["repo"], m["id"])
            self.assertRegex(pin["revision"], r"^[0-9a-f]{40}$")
            self.assertTrue(pin["files"])
            for name, f in pin["files"].items():
                self.assertRegex(f["sha256"], r"^[0-9a-f]{64}$", f"{m['id']}/{name}")
                self.assertGreater(f["size"], 0)
                self.assertFalse(name.endswith((".py", ".pt", ".pth", ".pkl", ".pickle")), name)   # data, not code

    def test_every_locked_package_is_an_exact_version_with_hashes(self):
        for lock in (es.LOCKS_DIR / "base.txt", es.LOCKS_DIR / "nvidia.txt"):
            text = lock.read_text(encoding="utf-8")
            entries = re.split(r"\n(?=\S)", text.strip())
            self.assertTrue(entries)
            for entry in entries:
                head = entry.splitlines()[0]
                self.assertRegex(head, r"^[A-Za-z0-9_.\-]+==[^\s;]+( ;.*)? \\$", head)
                self.assertIn("--hash=sha256:", entry, head)
        base = (es.LOCKS_DIR / "base.txt").read_text(encoding="utf-8")
        for package in es.BASE_PACKAGES:
            self.assertIn(package.lower().replace("_", "-") + " \\", base.lower().replace("_", "-"))
        for line in re.findall(r"^\S.*$", (es.LOCKS_DIR / "nvidia.txt").read_text(encoding="utf-8"), re.M):
            self.assertIn("sys_platform == 'win32'", line)              # CUDA wheels: never on a Mac


class _Root(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        for name, value in {"ROOT": self.root, "VENV_DIR": self.root / "venv", "MODELS_DIR": self.root / "models",
                            "VERIFIED_CACHE": self.root / "verified.json"}.items():
            patcher = mock.patch.object(es, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class InstallTests(_Root):
    def fake_runs(self):
        """_run_streaming stood in for: `venv` makes the venv's python; pip records its arguments."""
        calls = []

        def run(cmd, progress, cancelled, what):
            calls.append(cmd)
            if "venv" in cmd:
                es.venv_python().parent.mkdir(parents=True, exist_ok=True)
                es.venv_python().write_text("")
        return calls, run

    def install(self, verdict=(defender.CLEAN, "scanned"), nvidia=False):
        calls, run = self.fake_runs()
        hw = es.Hardware("Windows", "AMD64", nvidia)
        with mock.patch.object(es, "_run_streaming", run), mock.patch.object(es, "base_python", lambda: "python"), \
                mock.patch.object(es.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")), \
                mock.patch.object(es.defender, "scan", lambda *_a: verdict), \
                mock.patch.object(es, "env_status", lambda: es.EnvStatus(True, "Ready.", {}, True)):
            es.install_environment(progress=lambda _l: None, hw=hw)
        return calls

    def test_pip_installs_only_the_locked_wheels_each_checked(self):
        calls = self.install(nvidia=True)
        pip = next(c for c in calls if "pip" in c)
        for flag in ("--require-hashes", "--no-deps"):
            self.assertIn(flag, pip)
        self.assertEqual(pip[pip.index("--only-binary") + 1], ":all:")
        self.assertEqual([pip[i + 1] for i, a in enumerate(pip) if a == "-r"],
                         [str(es.LOCKS_DIR / "base.txt"), str(es.LOCKS_DIR / "nvidia.txt")])
        self.assertFalse(any(a.startswith(("faster-whisper", "nvidia-")) for a in pip))   # never a bare name

    def test_a_verified_install_is_stamped_with_its_locks(self):
        self.install()
        self.assertTrue(es._stamp_current())

    def test_an_environment_from_before_the_locks_is_rebuilt_not_kept(self):
        es.venv_python().parent.mkdir(parents=True)
        es.venv_python().write_text("")
        (es.VENV_DIR / "unchecked-package").write_text("installed without hashes")
        self.install()
        self.assertFalse((es.VENV_DIR / "unchecked-package").exists())

    def test_an_environment_defender_flags_is_deleted(self):
        with self.assertRaises(es.SetupError) as caught:
            self.install(verdict=(defender.THREAT, ""))
        self.assertIn("Defender", str(caught.exception))
        self.assertFalse(es.VENV_DIR.exists())

    def test_a_changed_lock_makes_the_environment_unverified(self):
        self.install()
        stamp = json.loads((es.VENV_DIR / es.STAMP_NAME).read_text(encoding="utf-8"))
        stamp["locks"]["base.txt"] = "0" * 64
        (es.VENV_DIR / es.STAMP_NAME).write_text(json.dumps(stamp), encoding="utf-8")
        self.assertFalse(es._stamp_current())


class ModelTests(_Root):
    FILES = {"model.bin": b"weights" * 1000, "config.json": b"{}"}

    def pins(self):
        return {"small": {"repo": "Systran/faster-whisper-small", "revision": "a" * 40,
                          "files": {n: {"size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                                    for n, d in self.FILES.items()}}}

    def download(self, served):
        es.venv_python().parent.mkdir(parents=True, exist_ok=True)
        es.venv_python().write_text("")
        calls = []

        def run(cmd, progress, cancelled, what):
            calls.append(cmd)
            out = Path(cmd[cmd.index("--output") + 1])
            for name, data in served.items():
                (out / name).write_bytes(data)
        with mock.patch.object(es, "_PINS", self.pins()), mock.patch.object(es, "_run_streaming", run):
            folder = es.download_model("small", progress=lambda _l: None)
        return folder, calls

    def test_it_fetches_the_pinned_commit_and_files_only(self):
        _folder, calls = self.download(self.FILES)
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("--revision") + 1], "a" * 40)
        self.assertEqual(cmd[cmd.index("--model") + 1], "Systran/faster-whisper-small")
        self.assertEqual(sorted(cmd[cmd.index("--files") + 1].split(",")), ["config.json", "model.bin"])

    def test_a_matching_download_replaces_the_old_copy(self):
        old = es.buddy_model_dir("small")
        old.mkdir(parents=True)
        (old / "model.bin").write_bytes(b"an older, unverified copy")
        folder, _ = self.download(self.FILES)
        self.assertEqual(Path(folder, "model.bin").read_bytes(), self.FILES["model.bin"])
        self.assertEqual(sorted(p.name for p in es.MODELS_DIR.iterdir()), ["small"])   # no staging left

    def test_a_tampered_download_is_deleted_and_the_old_copy_kept(self):
        old = es.buddy_model_dir("small")
        old.mkdir(parents=True)
        (old / "model.bin").write_bytes(b"the copy that worked")
        with self.assertRaises(es.SetupError) as caught:
            self.download({**self.FILES, "model.bin": b"weights" * 999 + b"tamper!"})     # same size, other bytes
        self.assertIn("checksum", str(caught.exception))
        self.assertEqual((old / "model.bin").read_bytes(), b"the copy that worked")
        self.assertEqual(sorted(p.name for p in es.MODELS_DIR.iterdir()), ["small"])

    def test_a_folder_is_verified_by_its_files_and_rechecked_when_they_change(self):
        folder = self.root / "somewhere"
        folder.mkdir()
        for name, data in self.FILES.items():
            (folder / name).write_bytes(data)
        (folder / "README.md").write_text("extra files don't matter")
        with mock.patch.object(es, "_PINS", self.pins()):
            self.assertTrue(es.model_verified("small", folder))
            with mock.patch.object(es, "_sha256", side_effect=AssertionError("hashed again")):
                self.assertTrue(es.model_verified("small", folder))          # unchanged: remembered
            (folder / "model.bin").write_bytes(b"weights" * 999 + b"tamper!")
            self.assertFalse(es.model_verified("small", folder))
            self.assertFalse(es.model_verified("small", self.root / "nowhere"))

    def test_setup_rows_say_which_copies_are_verified(self):
        mine, theirs = str(es.MODELS_DIR / "small"), str(self.root / "hf-cache" / "large")
        rows = plan.setup_rows(es.MODELS, {"small": mine}, {"large-v3": theirs}, "small",
                               {mine: True, theirs: False})
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["small"]["verified"])
        self.assertIs(by_id["large-v3"]["verified"], False)
        self.assertIsNone(by_id["parakeet-v3"]["verified"])                   # nothing there to check


if __name__ == "__main__":
    unittest.main()
