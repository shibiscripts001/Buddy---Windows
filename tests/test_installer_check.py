"""The installer's package check (installer/check_packages.py): a package that
imports but is older than requirements.txt's floor is not "already there", and
it judges versions exactly as the in-app updater does."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

import _paths  # noqa: F401
from core import updater

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "installer", "check_packages.py")
REQUIREMENTS = os.path.join(ROOT, "installer", "requirements.txt")

spec = importlib.util.spec_from_file_location("check_packages", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckTests(unittest.TestCase):
    TEXT = ("# comment\nPySide6>=6.10,<7\npillow>=12.3\nnumpy>=1.26   # why\n"
            "winonly>=1; sys_platform == 'win32'\nmaconly>=1; sys_platform == 'darwin'\n")

    def test_old_and_missing_packages_are_found(self):
        have = {"PySide6": "6.8.3", "pillow": "12.3.0", "numpy": None, "winonly": "1.0", "maconly": None}
        wrong = check.problems(self.TEXT, have.get)
        self.assertEqual(sorted(w.split("(")[0].rstrip() for w in wrong), ["PySide6>=6.10,<7", "numpy>=1.26"])
        self.assertIn("have 6.8.3", " ".join(wrong))

    def test_a_pc_that_meets_every_floor_passes(self):
        have = {"PySide6": "6.11.2", "pillow": "12.3.0", "numpy": "2.5.2", "winonly": "1.0", "maconly": None}
        self.assertEqual(check.problems(self.TEXT, have.get), [])

    def test_it_judges_versions_as_the_updater_does(self):
        samples = [("6.11.2", ">=6.10,<7"), ("7.0.0", ">=6.10,<7"), ("6.9.9", ">=6.10,<7"), ("12.3.0.post1", ">=12.3"),
                   ("12.2.9", ">=12.3"), ("50.0.1", ">=50.0"), ("1.26", ">=1.26"), ("2.0", "~=1.4"), ("1.0", "!=1.0"),
                   ("3.1.5", ">=3.1"), ("0.0.17", ">=0.0.17"), ("garbage", ">=1")]
        for installed, wanted in samples:
            self.assertEqual(check.satisfies(installed, wanted), updater._satisfies(installed, wanted),
                             (installed, wanted))
        self.assertEqual(check.version_key("1.26.7rc1"), updater.version_key("1.26.7rc1"))

    def test_it_reads_the_real_requirements_like_the_manifest_does(self):
        with open(REQUIREMENTS, encoding="utf-8") as fh:
            ours = [(n, s) for n, s, _m in check.requirements(fh.read())]
        sys.path.insert(0, ROOT)
        try:
            import build_update_manifest as manifest
        finally:
            sys.path.remove(ROOT)
        theirs = [(r["name"], r["spec"]) for r in manifest.requirements(__import__("pathlib").Path(REQUIREMENTS))]
        self.assertEqual(ours, theirs)

    def test_the_floors_are_the_ones_the_audit_set(self):
        with open(REQUIREMENTS, encoding="utf-8") as fh:
            floors = {n: s for n, s, _m in check.requirements(fh.read())}
        self.assertTrue(check.satisfies("12.3.0", floors["pillow"]))
        self.assertFalse(check.satisfies("12.2.0", floors["pillow"]))        # advisories fixed in 12.3.0
        self.assertFalse(check.satisfies("1.26.6", floors["pymupdf"]))       # CVE-2026-3029
        self.assertFalse(check.satisfies("49.0.0", floors["cryptography"]))
        self.assertFalse(check.satisfies("6.8.3", floors["PySide6"]))

    def test_the_script_exits_with_the_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "requirements.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("surely-not-installed-anywhere>=1\n")
            done = subprocess.run([sys.executable, "-I", SCRIPT, path], capture_output=True, text=True)
            self.assertEqual(done.returncode, 1)
            self.assertIn("needs: surely-not-installed-anywhere", done.stdout)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# nothing needed\n")
            self.assertEqual(subprocess.run([sys.executable, "-I", SCRIPT, path]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
