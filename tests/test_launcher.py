"""Buddy.py, the launcher stub: clearing out old extracted versions without
touching one a running Buddy still uses. The stub runs Buddy as soon as it
loads, so only its functions are loaded here."""

import os
import tempfile
import unittest

import _paths


def _stub():
    source = (_paths.APP.parent / "Buddy.py").read_text(encoding="utf-8")
    functions = source.split("\ntry:\n    _run()")[0]
    namespace = {"__name__": "buddy_stub"}
    exec(compile(functions, "Buddy.py", "exec"), namespace)
    return namespace


class OldVersionTests(unittest.TestCase):
    def test_old_versions_go_but_one_in_use_and_other_folders_stay(self):
        stub = _stub()
        with tempfile.TemporaryDirectory() as root:
            def version(name):
                path = os.path.join(root, name)
                os.makedirs(os.path.join(path, "buddy"))
                with open(os.path.join(path, "buddy", "main.py"), "w") as f:
                    f.write("# app")
                return path

            current = version("aaaaaaaaaaaaaaaa")
            old = version("bbbbbbbbbbbbbbbb")
            running = version("cccccccccccccccc")
            half_deleted = version("dddddddddddddddd.deleting")
            someone_elses = version("not-a-version")
            held = open(os.path.join(running, ".in_use"), "a")   # a Buddy still running from it
            try:
                stub["_hold"](current)
                stub["_remove_old_versions"](current)
                left = sorted(os.listdir(root))
            finally:
                held.close()
                stub["_in_use_handle"].close()
            self.assertIn(os.path.basename(current), left)
            self.assertIn(os.path.basename(someone_elses), left)
            self.assertNotIn(os.path.basename(old), left)
            self.assertNotIn(os.path.basename(half_deleted), left)
            if os.name == "nt":   # Windows won't rename a folder with a file open in it
                self.assertIn(os.path.basename(running), left)
                self.assertTrue(os.path.isfile(os.path.join(running, "buddy", "main.py")))


if __name__ == "__main__":
    unittest.main()
