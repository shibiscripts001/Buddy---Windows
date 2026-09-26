#!/usr/bin/env python3
"""
What Asset Manager's lists show, without Qt: filtering, sorting and the
folder grouping, plus the waveform's bars.

A list is a sequence of nodes:
    {"type": "folder", "path", "name", "count", "children": [asset rows]}
    {"type": "asset", ...asset row}
and an asset row is {"id", "name", "category", "ext", "missing",
"date_added", "path", "folder"}.
"""

import os

SORT_NAME = "name"
SORT_ADDED = "added"
WAVEFORM_BARS = 120


def matching(assets, category="All", search=""):
    search = (search or "").strip().lower()
    return [a for a in assets
            if (category in ("All", None, "") or a.get("category") == category)
            and (not search or search in a["name"].lower())]


def asset_row(asset, exists=os.path.exists):
    return {
        "type": "asset",
        "id": asset["id"],
        "name": asset["name"],
        "category": asset.get("category", "Other"),
        "ext": asset.get("ext", os.path.splitext(asset["path"])[1].lower()),
        "missing": not exists(asset["path"]),
        "date_added": asset.get("date_added", ""),
        "path": asset["path"],
        "folder": os.path.dirname(asset["path"]),
    }


def _sort_key(asset, column):
    return asset.get("date_added", "") if column == SORT_ADDED else asset["name"].lower()


def build(assets, sort=SORT_NAME, reverse=False, include_folders_in_sort=True,
          always_bucket=False, flat=False, exists=os.path.exists):
    """The nodes for `assets`. A folder becomes a bucket when 2+ of the
    assets share it (or always, with always_bucket); flat lists every asset
    on its own. include_folders_in_sort interleaves buckets with single
    rows by the sort column; off, buckets come first, alphabetically."""
    ordered_assets = lambda items: sorted(items, key=lambda a: _sort_key(a, sort), reverse=reverse)  # noqa: E731
    if flat:
        return [asset_row(a, exists) for a in ordered_assets(assets)]

    groups = {}
    for asset in assets:
        groups.setdefault(os.path.dirname(asset["path"]) or "(unknown)", []).append(asset)

    folders, singles = [], []
    for folder, members in groups.items():
        members = ordered_assets(members)
        if always_bucket or len(members) >= 2:
            name = os.path.basename(folder.rstrip("\\/")) or folder
            if include_folders_in_sort:
                key = max(a.get("date_added", "") for a in members) if sort == SORT_ADDED else name.lower()
            else:
                key = name.lower()
            folders.append((key, {"type": "folder", "path": folder, "name": name, "count": len(members),
                                  "children": [asset_row(a, exists) for a in members]}))
        else:
            singles.append((_sort_key(members[0], sort), asset_row(members[0], exists)))

    if include_folders_in_sort:
        combined = folders + singles
        combined.sort(key=lambda e: e[0], reverse=reverse)
        return [node for _key, node in combined]
    folders.sort(key=lambda e: e[0])
    singles.sort(key=lambda e: e[0], reverse=reverse)
    return [node for _key, node in folders] + [node for _key, node in singles]


def resolve_selection(nodes, ids, folders):
    """(loose asset ids, [(folder name, [asset ids])]) for a selection of
    asset ids and folder-bucket paths in `nodes`. An asset inside a
    selected folder counts once, with the folder."""
    ids, folders = list(ids or []), set(folders or [])
    groups, covered = [], set()
    for node in nodes:
        if node["type"] == "folder" and node["path"] in folders:
            children = [c["id"] for c in node["children"]]
            if children:
                groups.append((node["name"], children))
                covered.update(children)
    loose, seen = [], set()
    for asset_id in ids:
        if asset_id not in covered and asset_id not in seen:
            seen.add(asset_id)
            loose.append(asset_id)
    return loose, groups


def all_ids(loose, groups):
    out = list(loose)
    for _name, members in groups:
        out.extend(m for m in members if m not in out)
    return out


def waveform_bars(levels, bars=WAVEFORM_BARS):
    """0-1 heights for the preview's waveform from decoded (rms, peak)
    levels: each bar takes the loudest buffer in its slice (so short hits
    stay visible), blends mostly RMS with some peak (so mastered music isn't
    a solid wall), and is scaled against this file's own loudest bar, with
    a floor so silence still shows a tick."""
    if not levels:
        return []
    count = min(bars, len(levels))
    values = []
    for col in range(count):
        start = col * len(levels) // count
        end = max(start + 1, (col + 1) * len(levels) // count)
        chunk = levels[start:end]
        values.append(0.6 * max(v[0] for v in chunk) + 0.4 * max(v[1] for v in chunk))
    top = max(values) or 1.0
    if top <= 1e-6:
        top = 1.0
    return [round(max(v / top, 0.03), 4) for v in values]
