"""Start menu and desktop shortcuts (app/core/shortcuts.py): a .lnk
written and read back - in a temp folder, never the real Start menu or
desktop."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from core import shortcuts

only_windows = unittest.skipUnless(shortcuts.available, "Windows shortcuts")


@only_windows
class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="buddy_lnk_")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_written_and_read_back(self):
        target = os.path.join(self.folder, "Buddy.lnk")
        shortcuts.write(target, r"C:\Windows\System32\notepad.exe", r'"C:\Some Folder\Buddy.py"',
                        r"C:\Some Folder", shortcuts._ICON)
        self.assertEqual(shortcuts.read(target), {
            "program": r"C:\Windows\System32\notepad.exe", "arguments": r'"C:\Some Folder\Buddy.py"',
            "working_dir": r"C:\Some Folder", "app_id": "Buddy.ResolveTools"})   # one taskbar button with Buddy

    def test_buddys_own(self):
        script = os.path.join(self.folder, "Scripts", "Buddy.py")
        os.makedirs(os.path.dirname(script))
        open(script, "w").close()
        with mock.patch.object(shortcuts, "folder", lambda place: os.path.join(self.folder, place)), \
                mock.patch.object(shortcuts, "launcher", lambda: script), \
                mock.patch.object(shortcuts, "python", lambda: r"C:\Python\pythonw.exe"), \
                mock.patch.object(shortcuts, "icon", lambda: ""):
            self.assertFalse(shortcuts.exists("desktop"))
            shortcuts.make("desktop")
            self.assertTrue(shortcuts.exists("desktop"))
            made = shortcuts.read(shortcuts.path("desktop"))
            self.assertEqual((made["program"], made["arguments"], made["working_dir"]),
                             (r"C:\Python\pythonw.exe", f'"{script}"', os.path.dirname(script)))
            shortcuts.remove("desktop")
            self.assertFalse(shortcuts.exists("desktop"))
            shortcuts.remove("desktop")                                     # not there: fine
            with mock.patch.object(shortcuts, "python", lambda: None), self.assertRaises(OSError):
                shortcuts.make("start_menu")
        with mock.patch.object(shortcuts, "folder", lambda place: os.path.join(self.folder, place)), \
                mock.patch.object(shortcuts, "launcher", lambda: os.path.join(self.folder, "missing.py")), \
                self.assertRaises(OSError):
            shortcuts.make("start_menu")

    def test_the_real_folders_are_found(self):
        self.assertTrue(os.path.isdir(shortcuts.folder("start_menu")))
        self.assertTrue(os.path.isdir(shortcuts.folder("desktop")))
        self.assertIsNone(shortcuts.folder("nowhere"))
        self.assertTrue(os.path.isfile(shortcuts._ICON))                    # shipped in app/assets


if __name__ == "__main__":
    unittest.main()
