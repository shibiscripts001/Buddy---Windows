"""core/atomic_io.py: crash-safe JSON writes, and damaged files reported
(or recovered from .bak) instead of being read as empty."""

import os
import tempfile
import unittest

import _paths  # noqa: F401
from core import atomic_io as aio


class AtomicIoTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "sub", "data.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_gives_default(self):
        self.assertEqual(aio.read_json(self.path, default=[]), [])

    def test_round_trip_and_backup(self):
        aio.write_json(self.path, [1])
        aio.write_json(self.path, [1, 2])
        self.assertEqual(aio.read_json(self.path), [1, 2])
        self.assertEqual(aio.read_json(aio.backup_path(self.path)), [1])
        leftovers = [n for n in os.listdir(os.path.dirname(self.path)) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_truncated_file_falls_back_to_backup(self):
        aio.write_json(self.path, {"a": 1})
        aio.write_json(self.path, {"a": 2})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"a": ')
        self.assertEqual(aio.read_json(self.path), {"a": 1})

    def test_backup_fallback_is_reported_and_keeps_the_good_backup(self):
        aio.write_json(self.path, {"a": 1})
        aio.write_json(self.path, {"a": 2})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"a": ')
        warnings = []
        self.assertEqual(aio.read_json(self.path, warnings=warnings), {"a": 1})
        self.assertEqual(len(warnings), 1)
        self.assertTrue(os.path.exists(self.path + ".corrupt-1"))
        aio.write_json(self.path, {"a": 3})
        self.assertEqual(aio.read_json(aio.backup_path(self.path)), {"a": 1})

    def test_damaged_file_is_never_copied_over_the_backup(self):
        aio.write_json(self.path, {"a": 1})
        aio.write_json(self.path, {"a": 2})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("junk")
        aio.write_json(self.path, {"a": 3})
        self.assertEqual(aio.read_json(aio.backup_path(self.path)), {"a": 1})

    def test_truncated_file_without_backup_raises(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[1, 2")
        with self.assertRaises(aio.CorruptFileError):
            aio.read_json(self.path, default=[])

    def test_set_aside_keeps_the_damaged_file(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("junk")
        moved = aio.set_aside(self.path)
        self.assertFalse(os.path.exists(self.path))
        with open(moved, encoding="utf-8") as f:
            self.assertEqual(f.read(), "junk")


if __name__ == "__main__":
    unittest.main()
