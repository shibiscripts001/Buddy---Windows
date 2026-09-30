#!/usr/bin/env python3
"""
Media Relink's list of clips, without Qt: what a scan found, what a folder
search proposes for each clip, and which clips a relink may touch.

Two modes, each with its own list:
    fix       only offline clips are ever reconsidered - clips that work
              are left alone even if a same-named file turns up
    relocate  every clip is fair game (moving media to a new drive), but
              only the clips the user selected are relinked
"""

from .relink_engine import MatchStatus, classify_match, find_candidates, media_exists, same_path

MODE_FIX = "fix"
MODE_RELOCATE = "relocate"
MODES = (MODE_FIX, MODE_RELOCATE)

STATUS_LABELS = {
    MatchStatus.ONLINE: "Online",
    MatchStatus.MATCH_FOUND: "Match found",
    MatchStatus.AMBIGUOUS: "Pick one",
    MatchStatus.NOT_FOUND: "Offline",
}


def make_rows(entries, get_path, get_name, exists=media_exists, get_id=lambda clip: None):
    """Rows for scanned (clip, bin_path) entries. Returns (rows, skipped):
    clips with no single file path (generated media) are skipped - there's
    nothing to check or relink. The same clip twice (a scan that reaches
    it two ways) is one row."""
    rows, skipped, seen = [], 0, set()
    for clip, bin_path in entries:
        uid = get_id(clip)
        if uid is not None:
            if uid in seen:
                continue
            seen.add(uid)
        path = get_path(clip)
        if not path:
            skipped += 1
            continue
        rows.append({
            "id": len(rows),
            "uid": uid,
            "clip": clip,
            "name": get_name(clip),
            "bin": bin_path,
            "old_path": path,
            "status": classify_match(exists(path), []),
            "candidates": [],
            "resolved": None,
        })
    return rows, skipped


def apply_search(rows, index, mode):
    """Fills in proposed matches from a folder index. Returns
    (matched, ambiguous). A clip that already has a single match (found
    earlier, or picked by hand) keeps it when this search is ambiguous."""
    matched = ambiguous = 0
    for row in rows:
        if mode == MODE_FIX and row["status"] == MatchStatus.ONLINE:
            continue
        # The clip's own file isn't somewhere new to go (a relocate search
        # of the folder it already lives in).
        candidates = [c for c in find_candidates(row["old_path"], index)
                      if not same_path(c, row["old_path"])]
        if not candidates:
            continue
        if row["status"] == MatchStatus.MATCH_FOUND and len(candidates) > 1:
            continue
        row["candidates"] = candidates
        row["status"] = classify_match(False, candidates)
        row["resolved"] = candidates[0] if len(candidates) == 1 else None
        if row["status"] == MatchStatus.MATCH_FOUND:
            matched += 1
        else:
            ambiguous += 1
    return matched, ambiguous


def pick(row, path):
    """The user chose `path` for this clip (from its candidates or by
    browsing)."""
    row["resolved"] = path
    row["status"] = MatchStatus.MATCH_FOUND
    if path not in row["candidates"]:
        row["candidates"] = [path]


def matched_rows(rows):
    return [r for r in rows if r["status"] == MatchStatus.MATCH_FOUND and r["resolved"]]


def relink(rows, replace):
    """Relinks each row that has a file to go to. replace(clip, path) ->
    bool. Returns (relinked, skipped, failed). A relinked row remembers
    how it was, for confirm()."""
    relinked = skipped = failed = 0
    for row in rows:
        if row["status"] == MatchStatus.ONLINE:
            continue
        if not row["resolved"]:
            skipped += 1
            continue
        if replace(row["clip"], row["resolved"]):
            row["before"] = {k: row[k] for k in ("old_path", "status", "candidates", "resolved")}
            row.update(old_path=row["resolved"], status=MatchStatus.ONLINE, candidates=[], resolved=None)
            relinked += 1
        else:
            failed += 1
    return relinked, skipped, failed


def confirm(rows, current_paths):
    """Resolve can answer yes to a relink and keep the old file. With the
    paths read afresh afterwards ({uid: path}), each relinked row whose
    clip doesn't point at its new file goes back to how it was. Returns
    how many didn't take; a clip that can't be found again is taken on
    Resolve's word."""
    unchanged = 0
    for row in rows:
        before = row.pop("before", None)
        now = current_paths.get(row["uid"]) if before else None
        if now is None or same_path(now, row["old_path"]):
            continue
        row.update(before)
        unchanged += 1
    return unchanged


def counts(rows):
    out = {"total": len(rows), "online": 0, "offline": 0, "matched": 0, "ambiguous": 0}
    for row in rows:
        out[{MatchStatus.ONLINE: "online", MatchStatus.NOT_FOUND: "offline",
             MatchStatus.MATCH_FOUND: "matched", MatchStatus.AMBIGUOUS: "ambiguous"}[row["status"]]] += 1
    return out


def view(row):
    """What the table shows for one row (JSON-able)."""
    return {
        "id": row["id"],
        "name": row["name"],
        "bin": row["bin"],
        "status": row["status"],
        "label": STATUS_LABELS[row["status"]] + (f" ({len(row['candidates'])})"
                                                 if row["status"] == MatchStatus.AMBIGUOUS else ""),
        "old_path": row["old_path"],
        "new_path": row["resolved"],
        "candidates": row["candidates"],
    }
