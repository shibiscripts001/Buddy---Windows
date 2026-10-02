#!/usr/bin/env python3
"""
What the Web tab checks before and after it saves a download (page.py
download_requested): files that are programs or scripts and runaway bursts
of downloads need the user's OK, there's a cap on how many run at once and
on the disk they may fill, and a finished file is marked as coming from the
internet so Windows warns before it's run. No Qt, so it's unit-tested.
"""

import os
import re
import sys

# Files Windows (or a script host) will run when they are opened.
PROGRAM_EXTENSIONS = frozenset((
    ".exe", ".msi", ".msix", ".msp", ".appx", ".appxbundle", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl",
    ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".jar", ".reg", ".lnk", ".url",
    ".dll", ".sys", ".gadget", ".application", ".msc", ".sh", ".py", ".pyw", ".iso", ".img",
))

MAX_AT_ONCE = 6                      # downloads running together; the rest are refused
MIN_FREE_BYTES = 1024 ** 3           # a download may not take the drive below this much free space
BURST_COUNT, BURST_SECONDS = 4, 10.0 # this many from one site in this long is a burst
ALLOW_BURST_SECONDS = 300.0          # an OK'd site may keep downloading for this long


def is_program_file(name):
    """True for a file that runs when it's opened. Looks at every ending of
    a name, so "setup.exe.txt" is fine and "report.pdf.exe" is not."""
    parts = os.path.basename(str(name)).lower().rstrip(". ").split(".")
    return len(parts) > 1 and "." + parts[-1] in PROGRAM_EXTENSIONS


def room_for(free_bytes, wanted_bytes):
    """True if a download of `wanted_bytes` (0 = unknown) leaves the drive
    with MIN_FREE_BYTES to spare."""
    return free_bytes - max(0, wanted_bytes) >= MIN_FREE_BYTES


class BurstGuard:
    """Counts downloads per site; a burst has to be allowed by the user,
    once, and then goes on for ALLOW_BURST_SECONDS."""

    def __init__(self):
        self._times = {}              # site -> recent start times
        self._allowed = {}            # site -> when the OK runs out

    def burst(self, site, now):
        """Records a download from `site` and says whether it makes a
        burst the user hasn't OK'd."""
        if self._allowed.get(site, 0) > now:
            return False
        recent = [t for t in self._times.get(site, []) if now - t <= BURST_SECONDS] + [now]
        self._times[site] = recent
        return len(recent) > BURST_COUNT

    def allow(self, site, now):
        self._allowed[site] = now + ALLOW_BURST_SECONDS
        self._times[site] = []


def mark_from_internet(path, source_url="", page_url=""):
    """Writes the Zone.Identifier stream Windows uses to tell a file came
    from the internet (zone 3): Explorer and SmartScreen then warn before a
    program is run, and Office opens documents in Protected View. Leaves a
    mark already there. True if the file is marked."""
    if sys.platform != "win32":
        return False
    stream = path + ":Zone.Identifier"

    def clean(url):
        return re.sub(r"[^\x21-\x7e]", "", str(url or ""))[:1000]

    try:
        if not os.path.isfile(path):       # opening a stream would make the file
            return False
        if os.path.exists(stream):
            return True
        with open(stream, "w", encoding="ascii", newline="") as fh:
            fh.write("[ZoneTransfer]\r\nZoneId=3\r\n")
            if clean(page_url).lower().startswith(("http://", "https://")):
                fh.write(f"ReferrerUrl={clean(page_url)}\r\n")
            if clean(source_url).lower().startswith(("http://", "https://")):
                fh.write(f"HostUrl={clean(source_url)}\r\n")
        return True
    except OSError:                    # not NTFS (a FAT or network drive): no streams there
        return False
