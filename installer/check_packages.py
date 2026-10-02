"""Used by the installer (Buddy.iss PackagesPresent): exits 0 when every package
in requirements.txt is installed at a version it allows, else 1 and says which
are missing or too old - so a package that imports fine but has fallen below
the security floor there is upgraded, not skipped as "already there".

Standard library only; run with the Buddy's Python, `python -I check_packages.py
requirements.txt`. The version rules are app/core/updater.py's (missing_requirements)
- tests/test_installer_check.py keeps the two the same.
"""

import re
import sys
from importlib import metadata


def version_key(version):
    parts = []
    for piece in str(version or "").split("."):
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        parts.append(int(digits.group()))
    return tuple(parts)


def satisfies(installed, spec):
    have = version_key(installed)
    for clause in filter(None, (c.strip() for c in spec.split(","))):
        op = re.match(r"(~=|==|!=|>=|<=|>|<)\s*(.+)", clause)
        if not op:
            continue
        want = version_key(op.group(2))
        if op.group(1) == "~=":
            ok = have >= want
        else:
            ok = {"==": have[:len(want)] == want, "!=": have[:len(want)] != want, ">=": have >= want,
                  "<=": have <= want, ">": have > want, "<": have < want}[op.group(1)]
        if not ok:
            return False
    return True


def marker_applies(marker):
    found = re.search(r"""sys_platform\s*(==|!=)\s*["']([^"']+)["']""", marker or "")
    if not found:
        return True
    return (sys.platform == found.group(2)) == (found.group(1) == "==")


def requirements(text):
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec, _, marker = line.partition(";")
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec.strip())
        if name:
            out.append((name.group(), spec.strip()[name.end():].replace(" ", ""), marker.strip()))
    return out


def problems(text, version_of=None):
    """Packages in requirements `text` that are missing or below what it asks."""
    if version_of is None:
        def version_of(name):
            try:
                return metadata.version(name)
            except metadata.PackageNotFoundError:
                return None
    found = []
    for name, spec, marker in requirements(text):
        if not marker_applies(marker):
            continue
        have = version_of(name)
        if have is None or not satisfies(have, spec):
            found.append(f"{name}{spec} (have {have or 'nothing'})")
    return found


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        wrong = problems(fh.read())
    for line in wrong:
        print("needs:", line)
    sys.exit(1 if wrong else 0)
