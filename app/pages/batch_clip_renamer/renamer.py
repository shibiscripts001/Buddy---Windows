#!/usr/bin/env python3
"""
What Batch Clip Renamer would do to a list of clip names - the rules
only, no Qt and no Resolve, so the preview the page shows and the rename
it applies come from the same function (tests/test_batch_clip_renamer.py).
"""

from __future__ import annotations

import re

MODE_SEQUENTIAL = "sequential"
MODE_REPLACE = "replace"


def sequential_name(base, number):
    """BaseName_01, BaseName_02 ... - two digits, more once past 99."""
    return f"{base}_{number:02d}"


def parse_start(text):
    """The Start # field: a whole number >= 0, or None when it isn't one."""
    text = str(text or "").strip()
    if not text:
        return 1
    if not text.isdigit():
        return None
    return int(text)


def plan(names, inputs):
    """[{old, new, changed}] for every name, plus a problem, if any, that
    stops the rename (shown on the field that causes it).

    Returns (rows, problem) where problem is (field, message) or None.
    `inputs` is what the page's form holds: mode, base, start, find,
    replace, match_case.
    """
    mode = inputs.get("mode", MODE_SEQUENTIAL)
    rows = [{"old": n, "new": n, "changed": False} for n in names]

    if mode == MODE_SEQUENTIAL:
        base = str(inputs.get("base") or "").strip()
        start = parse_start(inputs.get("start"))
        if start is None:
            return rows, ("start", "Start # has to be a whole number.")
        if not base:
            return rows, ("base", "")
        for i, row in enumerate(rows):
            row["new"] = sequential_name(base, start + i)
    else:
        find = str(inputs.get("find") or "")
        replace = str(inputs.get("replace") or "")
        if not find:
            return rows, ("find", "")
        flags = 0 if inputs.get("match_case", True) else re.IGNORECASE
        pattern = re.compile(re.escape(find), flags)
        for row in rows:
            row["new"] = pattern.sub(lambda _m: replace, row["old"])

    for row in rows:
        row["changed"] = row["new"] != row["old"]
        # Resolve won't take an empty clip name; a replace that deletes the
        # whole name leaves that clip alone instead.
        if row["changed"] and not row["new"].strip():
            row["new"], row["changed"], row["skipped"] = row["old"], False, "would be empty"
    return rows, None


def duplicate_names(rows):
    """New names more than one clip would end up with. Resolve allows it,
    but it's rarely what someone renaming a batch meant."""
    seen, dupes = set(), set()
    for row in rows:
        name = row["new"]
        if name in seen:
            dupes.add(name)
        seen.add(name)
    return sorted(dupes)
