"""Put app/ (and the transcribe worker's folder) on sys.path for the tests.

The modules tested here are the pure ones - no Qt, no Resolve, no models -
so the suite runs with plain Python on any machine:

    python -m unittest discover tests
"""

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
TRANSCRIBE = APP / "pages" / "transcribe"
for p in (str(APP), str(TRANSCRIBE), str(APP.parent)):
    if p not in sys.path:
        sys.path.insert(0, p)
