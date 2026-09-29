"""Which Buddy this is - for bug reports (core/bug_report.py).

The number lives in the repo's VERSION file. build_buddy_zip.py copies it
into buddy.zip next to main.py (buddy/VERSION); run from the source folder,
it's the repo's own, one level up from app/.
"""

from __future__ import annotations

import os

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLACES = (os.path.join(_APP_DIR, "VERSION"),                    # installed: buddy/VERSION
           os.path.join(os.path.dirname(_APP_DIR), "VERSION"))   # from source: the repo's


def buddy_version() -> str:
    """"1.1.27", say; "" if it can't be found."""
    for path in _PLACES:
        try:
            with open(path, encoding="utf-8") as f:
                version = f.read().strip()
        except OSError:
            continue
        if version and len(version) <= 20 and all(c.isalnum() or c in ".-+" for c in version):
            return version
    return ""
