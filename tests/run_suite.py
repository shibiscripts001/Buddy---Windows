"""Runs the whole test suite and exits with its result.

    python tests/run_suite.py [-v]

The same as `python -m unittest discover tests`, except for how it ends: it
leaves with os._exit() once the results are printed, skipping Python's
normal shutdown. The page tests create QtWebEngine views, and Qt WebEngine's
own teardown at process exit can crash (an access violation after "OK"),
which would turn a passing run into a failed exit code - the release
workflow uses this runner for that reason.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    os.chdir(ROOT)
    program = unittest.main(module=None, argv=[sys.argv[0], "discover", "tests", *sys.argv[1:]], exit=False)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if program.result.wasSuccessful() else 1)
