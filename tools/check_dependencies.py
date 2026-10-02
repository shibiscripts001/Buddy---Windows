#!/usr/bin/env python3
"""Checks the lowest version each requirement allows against PyPI's advisory
data (OSV, through pypi.org/pypi/<package>/<version>/json), and says when that
floor, or a newer release still inside it, has known advisories.

    python tools/check_dependencies.py            # installer + server requirements
    python tools/check_dependencies.py --strict   # exit 1 if any floor has an advisory

A floor is the version a PC is guaranteed to have at least (installer/
check_packages.py upgrades anything below it), so it should be the lowest
release with no advisory. Run before a release; the release workflow runs it
too, as a warning. PySide6's Chromium (QtWebEngine) doesn't appear in this
data - raise that floor when Qt does. Needs the network; standard library only.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (ROOT / "installer" / "requirements.txt", ROOT / "server" / "requirements.txt")


def get(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def key(version):
    return tuple(int(p) for p in re.findall(r"\d+", version)[:4])


def requirements(path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        found = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9][0-9.]*)", line)
        if found:
            yield found.group(1), found.group(2)


def advisories(name, version):
    info = get(f"https://pypi.org/pypi/{name}/{version}/json")
    return [(v.get("id"), (v.get("aliases") or [""])[0], v.get("fixed_in") or []) for v in info.get("vulnerabilities", [])]


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--strict", action="store_true", help="exit 1 if a floor has known advisories")
    args = parser.parse_args()
    problems = 0
    for path in FILES:
        for name, floor in requirements(path):
            try:
                releases = get(f"https://pypi.org/pypi/{name}/json")["releases"]
                lowest = min((v for v in releases if releases[v] and re.fullmatch(r"\d+(\.\d+)*", v)
                              and key(v) >= key(floor)), key=key, default=floor)
                found = advisories(name, lowest)
            except Exception as exc:  # noqa: BLE001 - offline, PyPI down: not a verdict
                print(f"?  {name}>={floor}: couldn't check ({exc})")
                continue
            if found:
                problems += 1
                fixed = sorted({f for _id, _alias, fixes in found for f in fixes}, key=key)
                print(f"!! {name}>={floor}: {lowest} has {len(found)} advisories "
                      f"({', '.join(a or i for i, a, _ in found[:3])}...); fixed in {', '.join(fixed) or '?'}")
                print(f"::warning title={name} floor has advisories::{name}>={floor} allows {lowest}, "
                      f"which has known advisories; raise the floor to {fixed[-1] if fixed else 'a fixed release'}.")
            else:
                print(f"ok {name}>={floor}: {lowest} has no known advisories")
    return 1 if (problems and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
