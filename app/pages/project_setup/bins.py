#!/usr/bin/env python3
"""
The Bins tab's rules, without Qt: how the typed list becomes a tree of
bins, and creating that tree in the Media Pool.

Each line is a bin. Leading '>' characters nest it inside the nearest bin
above with one fewer '>'. A line with more '>' than its position allows is
nested as deep as it can go rather than rejected (and flagged, so the
preview can show it).
"""

INFO_TEXT = (
    "Each line becomes a bin. Start a line with '>' to put it inside the bin above it: "
    "one '>' for a bin inside a top-level bin, two for a bin inside that, and so on.\n\n"
    "Example:\n"
    "01_Timelines\n"
    ">1080p\n"
    ">>15sec\n"
    ">>30sec\n"
    "02_Footage\n"
    ">Ronin\n"
    ">FX6\n\n"
    "The bins are created at the top of the Media Pool. A line with more '>' than its "
    "place allows (right after a top-level bin, say) is nested as deep as it can go – "
    "the preview marks those."
)


def parse_lines(raw_text):
    """(depth, name) per non-blank line; depth is the count of leading '>'."""
    parsed = []
    for line in (raw_text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        depth = len(stripped) - len(stripped.lstrip(">"))
        name = stripped[depth:].strip()
        if name:
            parsed.append((depth, name))
    return parsed


def plan(raw_text):
    """The tree the text describes, as rows in creation order:
    {"name", "depth" (where it will actually go), "typed" (the '>' count
    written), "adjusted" (True when those differ), "children" (how many
    bins sit directly inside it)}."""
    rows = []
    stack = []   # stack[d] is the index of the row that parents depth d+1
    for typed, name in parse_lines(raw_text):
        depth = min(typed, len(stack))
        parent = stack[depth - 1] if depth else None
        rows.append({"name": name, "depth": depth, "typed": typed,
                     "adjusted": depth != typed, "children": 0})
        if parent is not None:
            rows[parent]["children"] += 1
        stack = stack[:depth] + [len(rows) - 1]
    return rows


def create(media_pool, rows, log):
    """Creates `rows` (from plan()) under the Media Pool's root folder.
    Returns (created, failed). A bin that fails to create has its children
    put in its parent instead, so nothing further down is lost."""
    stack = [media_pool.GetRootFolder()]
    created = failed = 0
    for row in rows:
        depth = min(row["depth"], len(stack) - 1)
        parent = stack[depth]
        try:
            folder = media_pool.AddSubFolder(parent, row["name"])
        except Exception as exc:  # noqa: BLE001 - reported, and the rest carry on
            folder = None
            log(f"Error creating bin '{row['name']}': {exc}", "error")
        if folder:
            created += 1
        else:
            failed += 1
            log(f"Resolve didn't create the bin '{row['name']}'.", "error")
            folder = parent
        stack = stack[:depth + 1] + [folder]
    return created, failed
