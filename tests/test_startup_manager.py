"""core/startup_manager.py + core/resolve_watcher.py: the auto-start
watcher is registered with a real Python (never Resolve's fuscript.exe),
turning it off stops the running one, and its single-instance check can't
be fooled by a reused PID. Everything OS-facing (registry, subprocesses,
sleeping) is mocked - nothing here touches the real registry or processes."""

import os
import sys
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
from core import resolve_watcher as rw
from core import startup_manager as sm


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8"):
        pass
    return path


class _TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


@unittest.skipUnless(sm.IS_WINDOWS, "Windows login-item integration")
class FindPythonTests(_TempDirTest):
    def _inside_resolve(self, python_home):
        """sys as the embedded interpreter inside Resolve reports it:
        sys.executable is the script host, the prefixes are the real
        install."""
        host = _touch(os.path.join(self.dir, "Resolve", "fuscript.exe"))
        stack = mock.patch.multiple(
            sys, executable=host, _base_executable=host, prefix=python_home,
            exec_prefix=python_home, base_prefix=python_home, base_exec_prefix=python_home,
            create=True,
        )
        os_file = mock.patch.object(os, "__file__", os.path.join(python_home, "Lib", "os.py"))
        return stack, os_file

    def _no_other_places(self):
        return mock.patch.multiple(
            sm, _python_dll_dir=mock.Mock(return_value=None),
            _py_launcher_python_dir=mock.Mock(return_value=None),
            _standard_python_dirs=mock.Mock(return_value=[]),
            _KNOWN_RESOLVE_PYTHON_DIR=os.path.join(self.dir, "nowhere"),
        )

    def test_inside_resolve_finds_the_embedded_install(self):
        home = os.path.join(self.dir, "Python313")
        _touch(os.path.join(home, "python.exe"))
        pythonw = _touch(os.path.join(home, "pythonw.exe"))
        stack, os_file = self._inside_resolve(home)
        with stack, os_file, self._no_other_places():
            self.assertEqual(sm._find_python_for_startup(), pythonw)

    def test_os_py_location_alone_is_enough(self):
        # Prefixes pointing somewhere empty, os.py still in the real install.
        home = os.path.join(self.dir, "Python313")
        pythonw = _touch(os.path.join(home, "pythonw.exe"))
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        stack, _ = self._inside_resolve(empty)
        with stack, mock.patch.object(os, "__file__", os.path.join(home, "Lib", "os.py")), \
                self._no_other_places():
            self.assertEqual(sm._find_python_for_startup(), pythonw)

    def test_nothing_found_is_none_never_the_script_host(self):
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        stack, os_file = self._inside_resolve(empty)
        with stack, os_file, self._no_other_places():
            self.assertIsNone(sm._find_python_for_startup())

    def test_py_launcher_then_standard_dirs_are_fallbacks(self):
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        py_home = os.path.join(self.dir, "FromPy")
        py_python = _touch(os.path.join(py_home, "python.exe"))
        stack, os_file = self._inside_resolve(empty)
        with stack, os_file, self._no_other_places(), \
                mock.patch.object(sm, "_py_launcher_python_dir", return_value=py_home):
            self.assertEqual(sm._find_python_for_startup(), py_python)

    def test_py_launcher_not_asked_when_the_running_install_has_python(self):
        home = os.path.join(self.dir, "Python313")
        pythonw = _touch(os.path.join(home, "pythonw.exe"))
        stack, os_file = self._inside_resolve(home)
        with stack, os_file, self._no_other_places():
            self.assertEqual(sm._find_python_for_startup(), pythonw)
            sm._py_launcher_python_dir.assert_not_called()

    def test_py_launcher_answer_is_parsed(self):
        exe = _touch(os.path.join(self.dir, "Py", "python.exe"))
        done = mock.Mock(returncode=0, stdout=exe + "\n")
        with mock.patch.object(sm.subprocess, "run", return_value=done) as run:
            self.assertEqual(sm._py_launcher_python_dir(), os.path.dirname(exe))
        self.assertEqual(run.call_args[0][0][1:3], ["-3", "-c"])

    def test_standard_dirs_newest_first(self):
        base = os.path.join(self.dir, "Programs", "Python")
        for name in ("Python39", "Python313", "Python311"):
            os.makedirs(os.path.join(base, name))
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.dir, "ProgramFiles": self.dir}):
            dirs = [os.path.basename(d) for d in sm._standard_python_dirs()]
        self.assertEqual(dirs[:3], ["Python313", "Python311", "Python39"])


@unittest.skipUnless(sm.IS_WINDOWS, "Windows login-item integration")
class SetEnabledTests(_TempDirTest):
    def setUp(self):
        super().setUp()
        self.winreg = mock.MagicMock()
        self.winreg.OpenKey.return_value.__enter__.return_value = "key"
        self.winreg.CreateKey.return_value.__enter__.return_value = "key"
        watcher_path = os.path.join(self.dir, "resolve_watcher.py")
        self.patches = [
            mock.patch.object(sm, "winreg", self.winreg),
            mock.patch.object(sm, "_WATCHER_DIR", self.dir),
            mock.patch.object(sm, "_WATCHER_PATH", watcher_path),
            mock.patch.object(sm, "_STOP_PATH", os.path.join(self.dir, "watcher.stop")),
            mock.patch.object(sm, "_deploy_watcher"),
            mock.patch.object(sm, "_spawn_watcher_now"),
            mock.patch.object(sm, "_stop_legacy_watcher"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def test_enable_without_python_fails_clearly_and_writes_nothing(self):
        with mock.patch.object(sm, "_find_python_for_startup", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                sm.set_enabled(True)
        self.assertIn("Python", str(ctx.exception))
        self.winreg.SetValueEx.assert_not_called()
        sm._deploy_watcher.assert_not_called()
        sm._spawn_watcher_now.assert_not_called()

    def test_enable_registers_that_python_and_clears_an_old_stop_request(self):
        exe = r"C:\Py\pythonw.exe"
        _touch(sm._STOP_PATH)
        with mock.patch.object(sm, "_find_python_for_startup", return_value=exe):
            sm.set_enabled(True)
        command = self.winreg.SetValueEx.call_args[0][4]
        self.assertEqual(command, f'"{exe}" "{sm._WATCHER_PATH}"')
        self.assertFalse(os.path.exists(sm._STOP_PATH))
        sm._spawn_watcher_now.assert_called_once_with(exe)

    def test_disable_removes_the_run_value_and_stops_the_running_watcher(self):
        sm.set_enabled(False)
        self.winreg.DeleteValue.assert_called_once_with("key", sm._VALUE_NAME)
        self.assertTrue(os.path.exists(sm._STOP_PATH))
        sm._stop_legacy_watcher.assert_called_once()

    def test_disable_when_already_off_still_asks_the_watcher_to_stop(self):
        self.winreg.DeleteValue.side_effect = FileNotFoundError
        sm.set_enabled(False)
        self.assertTrue(os.path.exists(sm._STOP_PATH))

    def test_sync_repairs_a_run_value_pointing_at_fuscript(self):
        exe = _touch(os.path.join(self.dir, "Py", "pythonw.exe"))
        self.winreg.QueryValueEx.return_value = (r'"C:\Resolve\fuscript.exe" "x\resolve_watcher.py"', 1)
        with mock.patch.object(sm, "_find_python_for_startup", return_value=exe):
            sm.sync_if_enabled()
        self.assertEqual(self.winreg.SetValueEx.call_args[0][4], f'"{exe}" "{sm._WATCHER_PATH}"')
        sm._spawn_watcher_now.assert_called_once_with(exe)

    def test_sync_leaves_a_good_run_value_alone(self):
        exe = _touch(os.path.join(self.dir, "Py", "pythonw.exe"))
        self.winreg.QueryValueEx.return_value = (f'"{exe}" "{sm._WATCHER_PATH}"', 1)
        with mock.patch.object(sm, "_find_python_for_startup", return_value=exe):
            sm.sync_if_enabled()
        self.winreg.SetValueEx.assert_not_called()


class WatcherTestBase(_TempDirTest):
    def setUp(self):
        super().setUp()
        self.patches = [
            mock.patch.object(rw, "LOG_PATH", os.path.join(self.dir, "resolve_watcher.log")),
            mock.patch.object(rw, "LOCK_PATH", os.path.join(self.dir, "watcher.lock")),
            mock.patch.object(rw, "INFO_PATH", os.path.join(self.dir, "watcher.info")),
            mock.patch.object(rw, "STOP_PATH", os.path.join(self.dir, "watcher.stop")),
            mock.patch.object(rw, "_BASE_DIR", self.dir),
            mock.patch.object(rw, "_lock_fd", None),
        ]
        for p in self.patches:
            p.start()
        self.fds = []

    def tearDown(self):
        if rw._lock_fd is not None:
            os.close(rw._lock_fd)
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()


class SingleInstanceTests(WatcherTestBase):
    def test_lock_is_exclusive_while_held(self):
        self.assertTrue(rw._try_lock())
        first = rw._lock_fd
        try:
            rw._lock_fd = None
            self.assertFalse(rw._try_lock())  # a second handle can't take it
        finally:
            rw._lock_fd = first

    def test_same_version_already_running_exits(self):
        with open(rw.INFO_PATH, "w", encoding="utf-8") as f:
            f.write(f"1234\n{rw._own_source_hash()}\n")
        with mock.patch.object(rw, "_try_lock", return_value=False):
            self.assertFalse(rw._acquire_single_instance_lock())
        self.assertFalse(os.path.exists(rw.STOP_PATH))

    def test_older_version_is_asked_to_stop_not_killed(self):
        with open(rw.INFO_PATH, "w", encoding="utf-8") as f:
            f.write("1234\nold-version\n")
        seen_stop = []

        def try_lock():
            seen_stop.append(os.path.exists(rw.STOP_PATH))
            return len(seen_stop) >= 3  # the old one lets go on the third look

        with mock.patch.object(rw, "_try_lock", side_effect=try_lock), \
                mock.patch.object(rw.time, "sleep"), \
                mock.patch.object(rw, "_kill_pid") as kill, \
                mock.patch.object(rw, "stop_legacy_watcher"):
            self.assertTrue(rw._acquire_single_instance_lock())
        self.assertEqual(seen_stop, [False, True, True])
        kill.assert_not_called()
        self.assertFalse(os.path.exists(rw.STOP_PATH))
        with open(rw.INFO_PATH, encoding="utf-8") as f:
            self.assertEqual(f.read().split()[0], str(os.getpid()))

    def test_older_version_that_never_exits_is_left_alone(self):
        with open(rw.INFO_PATH, "w", encoding="utf-8") as f:
            f.write("1234\nold-version\n")
        clock = iter(range(0, 1000, 5))
        with mock.patch.object(rw, "_try_lock", return_value=False), \
                mock.patch.object(rw.time, "sleep"), \
                mock.patch.object(rw.time, "time", side_effect=lambda: next(clock)), \
                mock.patch.object(rw, "_kill_pid") as kill:
            self.assertFalse(rw._acquire_single_instance_lock())
        kill.assert_not_called()
        self.assertFalse(os.path.exists(rw.STOP_PATH))

    def test_stale_pid_file_does_not_block_a_new_watcher(self):
        # The old bug: watcher.pid naming a live, unrelated process after a reboot.
        with open(os.path.join(self.dir, rw.LEGACY_PID_FILENAME), "w", encoding="utf-8") as f:
            f.write("4\nold-version\n")
        with mock.patch.object(rw, "_process_command_line", return_value=r"C:\Windows\System32\svchost.exe -k x"), \
                mock.patch.object(rw, "_kill_pid") as kill:
            self.assertTrue(rw._acquire_single_instance_lock())
        kill.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.dir, rw.LEGACY_PID_FILENAME)))


class LegacyWatcherTests(WatcherTestBase):
    def setUp(self):
        super().setUp()
        self.pid_path = os.path.join(self.dir, rw.LEGACY_PID_FILENAME)
        self.script = os.path.join(self.dir, "resolve_watcher.py")
        with open(self.pid_path, "w", encoding="utf-8") as f:
            f.write("4321\nold-version\n")

    def _stop(self, command_line):
        with mock.patch.object(rw, "_process_command_line", return_value=command_line), \
                mock.patch.object(rw, "_kill_pid") as kill:
            rw.stop_legacy_watcher(self.dir, self.script)
        return kill

    def test_confirmed_watcher_is_killed(self):
        kill = self._stop(f'"C:\\Py\\pythonw.exe" "{self.script}"')
        kill.assert_called_once_with(4321)
        self.assertFalse(os.path.exists(self.pid_path))

    def test_unrelated_process_with_that_pid_is_never_killed(self):
        kill = self._stop(r'"C:\Py\pythonw.exe" C:\elsewhere\something_else.py')
        kill.assert_not_called()
        self.assertFalse(os.path.exists(self.pid_path))

    def test_unknown_is_not_killed_and_checked_again_later(self):
        kill = self._stop(None)
        kill.assert_not_called()
        self.assertTrue(os.path.exists(self.pid_path))


class StopAndLoopTests(WatcherTestBase):
    def test_sleep_ends_early_on_a_stop_request(self):
        _touch(rw.STOP_PATH)
        with mock.patch.object(rw.time, "sleep") as sleep:
            self.assertFalse(rw._sleep_unless_stopped(30))
        sleep.assert_not_called()

    @unittest.skipUnless(rw.IS_WINDOWS, "reads the Windows Run key")
    def test_removed_run_value_means_stop(self):
        import winreg
        with mock.patch.object(winreg, "OpenKey", side_effect=FileNotFoundError):
            self.assertFalse(rw._still_registered())
            self.assertIn("setting turned off", rw._stop_reason())
        with mock.patch.object(winreg, "OpenKey", side_effect=PermissionError):
            self.assertTrue(rw._still_registered())  # can't tell: keep running

    def _run_main(self, running, reasons):
        with mock.patch.object(rw, "_acquire_single_instance_lock", return_value=True), \
                mock.patch.object(rw, "_is_resolve_running", side_effect=running), \
                mock.patch.object(rw, "_stop_reason", side_effect=reasons), \
                mock.patch.object(rw, "_sleep_unless_stopped", return_value=True), \
                mock.patch.object(rw, "_launch_buddy") as launch:
            rw.main()
        return launch

    def test_resolve_already_open_at_start_is_not_a_launch(self):
        launch = self._run_main([True, True, True], [None, None, "stop"])
        launch.assert_not_called()

    def test_resolve_starting_later_launches_buddy_once(self):
        # initial check, tick 1 (not running), tick 2 (started), tick 3 (still running)
        launch = self._run_main([False, False, True, True], [None, None, None, None, "stop"])
        launch.assert_called_once()

    def test_turned_off_during_the_grace_wait_does_not_launch(self):
        launch = self._run_main([False, True], [None, "setting turned off"])
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
