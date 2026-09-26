#!/usr/bin/env python3
"""
The only place Ask Buddy is allowed to CHANGE a Resolve project.

resolve_ext.py in this package is strictly read-only and stays that way.
Everything that writes lives here, behind two independent gates:

  1. core/write_consent.py  - the user typed a sentence to enable writing
                              at all. Checked in page.py before the tool is
                              even offered to the model.
  2. the preview card       - the model PROPOSES; this module builds an
                              exact list of what would change; the user
                              presses Apply. Nothing here runs from the
                              agent loop.

That split is the whole design. A model being wrong in prose is a bad
answer; a model being wrong about which clip it meant is lost work, and
the two failures need different defences. Consent covers the capability,
the preview covers the specific change.

SCOPE: the full set. Actions fall into two groups
and the split is the thing to preserve:

  additive     markers, bins, timelines, renames, clip colours, appending
               to the END of a timeline. Nothing already in place moves.
  destructive  inserts that ripple, deletion of clips/timelines/bins, and
               project or timeline settings. These change or destroy work
               the user never named, so they set ProposedAction.destructive
               and carry specific warnings the card leads with.

A new action belongs in the second group unless it demonstrably cannot
move or remove anything that already exists. Default to destructive when
unsure - an unnecessary warning costs nothing, a missing one costs a
project.

Previews are built by READING the project first, so the card shows what
will actually happen rather than echoing what the model asked for. When
the model says "rename the three 29.97 clips" and only one exists, the
card says one - which is exactly the discrepancy worth seeing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .resolve_ext import _is_real_media_clip, _safe

# Resolve rejects a colour it does not know, and the API's failure mode is
# a bare False with no reason, so the list is checked up front.
MARKER_COLORS = [
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
]
CLIP_COLORS = [
    "Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
    "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate",
]

# One proposal cannot touch more than this. A model that has misunderstood
# the question tends to misunderstand it across the whole timeline, and a
# capped blast radius turns that into an obvious refusal rather than a
# hundred silent changes.
MAX_ITEMS_PER_ACTION = 50


class ActionError(ValueError):
    """Bad proposal. The message goes back to the model AND to the user, so
    it says what was wrong rather than just that something was."""


@dataclass
class ProposedAction:
    """What the card renders and what Apply later executes.

    `details` is the point of the whole mechanism: one line per item that
    will actually change, built from the live project. A summary alone
    ("add 12 markers") hides the case where the model picked the wrong 12.
    """

    action_id: str
    summary: str
    details: list[str] = field(default_factory=list)
    args: dict = field(default_factory=dict)
    # One line from the model on why, shown on the card above the items.
    reason: str = ""
    # Resolved during preview so Apply does not have to look anything up a
    # second time and risk resolving to different objects than were shown.
    plan: list = field(default_factory=list)
    # True for anything that moves or destroys work the user did not
    # name. The card leads with this rather than burying it in the item
    # list, because "add 3 markers" and "delete 3 timelines" must not
    # look alike to someone skimming.
    destructive: bool = False
    # Specific consequences in plain language. Not decoration -
    # "everything after the playhead shifts later" is the part a user
    # needs BEFORE pressing Apply, not after.
    warnings: list = field(default_factory=list)
    # Which project (and, for timeline actions, which timeline) the preview
    # was built from. Filled in by preview() and checked by execute(): the
    # plan above is only meaningful against the objects it was read from,
    # and Apply can be pressed long after the user has switched timelines.
    target: dict = field(default_factory=dict)

    def as_text(self) -> str:
        lines = [self.summary]
        lines.extend(f"  - {d}" for d in self.details)
        lines.extend(f"  ! {w}" for w in self.warnings)
        return "\n".join(lines)


# --------------------------------------------------------------- helpers


def _require_project(controller):
    project = _safe(controller.current_project)
    if not project:
        raise ActionError("No project is open in Resolve.")
    return project


def _require_timeline(controller):
    timeline = _safe(_require_project(controller).GetCurrentTimeline)
    if not timeline:
        raise ActionError("No timeline is open in the current project.")
    return timeline


def _require_pool(controller):
    pool = _safe(_require_project(controller).GetMediaPool)
    if not pool:
        raise ActionError("Could not access the Media Pool.")
    return pool


def _identity(obj) -> dict:
    """{"id", "name"} for a project or timeline. GetUniqueId() is preferred
    because names repeat across projects and the API hands back a fresh
    wrapper object on every call, so object identity means nothing."""
    return {
        "id": str(_safe(lambda: obj.GetUniqueId(), "") or ""),
        "name": str(_safe(lambda: obj.GetName(), "") or ""),
    }


def _target_identity(controller, action_id) -> dict:
    target = {"project": _identity(_require_project(controller))}
    if action_id in TIMELINE_ACTIONS:
        target["timeline"] = _identity(_require_timeline(controller))
    return target


def _check_target(controller, action_id, recorded):
    """Refuse when the project or timeline is no longer the one the preview
    was built from. Every execute_* re-resolves "the current timeline", so
    without this an Apply pressed after switching timelines would write the
    previewed plan into whatever is open now."""
    if not recorded:
        return
    current = _target_identity(controller, action_id)
    for kind in ("project", "timeline"):
        was, now = recorded.get(kind), current.get(kind)
        if was is None or now is None:
            continue
        same = (
            was["id"] == now["id"] if was["id"] and now["id"]
            else was["name"] == now["name"]
        )
        if not same:
            change = f"'{was['name']}' -> '{now['name']}'"
            if was["name"] == now["name"]:
                change = f"'{was['name']}' is now a different {kind} of the same name"
            raise ActionError(
                f"The {kind} changed since this was proposed ({change}); "
                "nothing was changed. Ask again."
            )


def _check_color(value, allowed, label):
    if value not in allowed:
        raise ActionError(
            f"'{value}' is not a {label} colour Resolve accepts. "
            f"Valid: {', '.join(allowed)}."
        )
    return value


def _check_count(items, what):
    if not items:
        raise ActionError(f"No {what} were given, so there is nothing to do.")
    if len(items) > MAX_ITEMS_PER_ACTION:
        raise ActionError(
            f"{len(items)} {what} is more than one action may change "
            f"({MAX_ITEMS_PER_ACTION} max). Propose a smaller change."
        )


def _pool_clips(controller):
    """Every real clip in the Media Pool, walked depth-first.

    Timelines appear in bins as pseudo-clips; the same guard used by
    batch_clip_renamer keeps them out, so "rename every clip" can never
    rename a timeline.
    """
    pool = _require_pool(controller)
    root = _safe(pool.GetRootFolder)
    if not root:
        raise ActionError("Could not access the Media Pool's root folder.")

    found, stack = [], [root]
    while stack:
        folder = stack.pop()
        for clip in _safe(folder.GetClipList, []) or []:
            if _is_real_media_clip(clip):
                found.append(clip)
        stack.extend(_safe(folder.GetSubFolderList, []) or [])
    return found


# ---------------------------------------------------------- add_markers


def preview_add_markers(controller, args) -> ProposedAction:
    timeline = _require_timeline(controller)
    markers = args.get("markers") or []
    _check_count(markers, "markers")

    existing = _safe(timeline.GetMarkers, {}) or {}
    start = _safe(timeline.GetStartFrame, 0) or 0
    name = _safe(timeline.GetName, "the timeline")

    plan, details = [], []
    for marker in markers:
        try:
            frame = int(marker.get("frame"))
        except (TypeError, ValueError):
            raise ActionError(
                f"Marker {marker!r} has no usable 'frame'. Frames are "
                "offsets from the timeline start, as project_state reports "
                "them under 'frame_offset'."
            )
        if frame < 0:
            raise ActionError(f"Frame {frame} is before the timeline starts.")
        if frame in existing:
            raise ActionError(
                f"There is already a marker at frame {frame} "
                f"({existing[frame].get('name', 'unnamed')}). Resolve allows "
                "only one marker per frame, and this action never replaces "
                "an existing one."
            )
        color = _check_color(marker.get("color") or "Blue", MARKER_COLORS, "marker")
        label = (marker.get("name") or "").strip()
        note = (marker.get("note") or "").strip()
        plan.append((frame, color, label, note))
        details.append(
            f"frame {frame} (timeline frame {frame + start}) – "
            f"{color} – {label or 'unnamed'}"
            + (f" – note: {note}" if note else "")
        )

    return ProposedAction(
        action_id="add_markers",
        summary=f"Add {len(plan)} marker(s) to '{name}'.",
        details=details,
        args=args,
        plan=plan,
    )


def execute_add_markers(controller, proposal, log=lambda m: None) -> str:
    timeline = _require_timeline(controller)
    done = 0
    for frame, color, label, note in proposal.plan:
        ok = timeline.AddMarker(frame, color, label, note, 1)
        if ok:
            done += 1
            log(f"Added {color} marker at frame {frame}.")
        else:
            # Keep going: a refused marker is not a reason to abandon the
            # ones that would succeed, and the count reported back is real.
            log(f"Resolve refused a marker at frame {frame}.")
    return f"Added {done} of {len(proposal.plan)} marker(s)."


# ------------------------------------------------------------ create_bin


def preview_create_bin(controller, args) -> ProposedAction:
    name = (args.get("name") or "").strip()
    if not name:
        raise ActionError("A bin needs a name.")

    pool = _require_pool(controller)
    root = _safe(pool.GetRootFolder)
    if not root:
        raise ActionError("Could not access the Media Pool's root folder.")

    existing = [f.GetName() for f in (_safe(root.GetSubFolderList, []) or [])]
    if name in existing:
        raise ActionError(
            f"A root-level bin named '{name}' already exists, so there is "
            "nothing to create."
        )

    return ProposedAction(
        action_id="create_bin",
        summary=f"Create a new Media Pool bin named '{name}'.",
        details=[f"at the root level, alongside: {', '.join(existing) or 'nothing'}"],
        args=args,
        plan=[name],
    )


def execute_create_bin(controller, proposal, log=lambda m: None) -> str:
    pool = _require_pool(controller)
    root = _safe(pool.GetRootFolder)
    name = proposal.plan[0]
    if pool.AddSubFolder(root, name) is None:
        raise ActionError(f"Resolve refused to create a bin named '{name}'.")
    log(f"Created bin '{name}'.")
    return f"Created the bin '{name}'."


# ---------------------------------------------------------- rename_clips


def preview_rename_clips(controller, args) -> ProposedAction:
    renames = args.get("renames") or []
    _check_count(renames, "renames")

    clips = _pool_clips(controller)
    by_name = {}
    for clip in clips:
        current = _safe(lambda c=clip: c.GetClipProperty("Clip Name"), "") or ""
        by_name.setdefault(current, []).append(clip)

    plan, details, seen = [], [], set()
    for entry in renames:
        old = (entry.get("from") or "").strip()
        new = (entry.get("to") or "").strip()
        if not old or not new:
            raise ActionError(
                f"Each rename needs a 'from' and a 'to'; got {entry!r}."
            )
        matches = by_name.get(old) or []
        if not matches:
            raise ActionError(
                f"No Media Pool clip is named '{old}'. Nothing was changed. "
                "Read the project first rather than guessing clip names."
            )
        if len(matches) > 1:
            raise ActionError(
                f"{len(matches)} clips are named '{old}'. Renaming would be "
                "ambiguous, so nothing was changed."
            )
        if old in seen:
            raise ActionError(f"'{old}' is listed twice in the same proposal.")
        seen.add(old)
        plan.append((matches[0], old, new))
        details.append(f"'{old}'  ->  '{new}'")

    return ProposedAction(
        action_id="rename_clips",
        summary=f"Rename {len(plan)} Media Pool clip(s).",
        details=details,
        args=args,
        plan=plan,
    )


def execute_rename_clips(controller, proposal, log=lambda m: None) -> str:
    done = 0
    for clip, old, new in proposal.plan:
        if clip.SetClipProperty("Clip Name", new):
            done += 1
            log(f"Renamed '{old}' to '{new}'.")
        else:
            log(f"Resolve refused to rename '{old}'.")
    return f"Renamed {done} of {len(proposal.plan)} clip(s)."


# ------------------------------------------------------- set_clip_colors


def preview_set_clip_colors(controller, args) -> ProposedAction:
    color = _check_color(args.get("color") or "", CLIP_COLORS, "clip")
    targets = args.get("clips") or []
    _check_count(targets, "clips")

    # Built once: GetItemListInTrack is an IPC round trip per track.
    items = _timeline_items(controller)

    plan, details = [], []
    for track, index, item in _find_timeline_clips(items, targets):
        plan.append(item)
        details.append(f"{track} clip {index}: '{_safe(item.GetName, '')}'  ->  {color}")

    return ProposedAction(
        action_id="set_clip_colors",
        summary=f"Set {len(plan)} timeline clip(s) to the colour {color}.",
        details=details,
        args=args,
        plan=[(item, color) for item in plan],
    )


def execute_set_clip_colors(controller, proposal, log=lambda m: None) -> str:
    done = 0
    for item, color in proposal.plan:
        if item.SetClipColor(color):
            done += 1
            log(f"Coloured '{_safe(item.GetName, '')}' {color}.")
        else:
            log(f"Resolve refused to colour '{_safe(item.GetName, '')}'.")
    return f"Coloured {done} of {len(proposal.plan)} clip(s)."


# ==========================================================================
# TIMELINE EDITS
#
# Everything below this line can move or destroy work the user did not
# mention. The additive four above cannot. That distinction is carried on
# ProposedAction.destructive and shown on the card, because "add 3 markers"
# and "delete 3 timelines" must not look alike when someone is skimming.
# ==========================================================================


INSERT_KINDS = {
    # kind -> (Timeline method, does it need a name?)
    "fusion_composition": ("InsertFusionCompositionIntoTimeline", False),
    "title": ("InsertTitleIntoTimeline", True),
    "fusion_title": ("InsertFusionTitleIntoTimeline", True),
    "generator": ("InsertGeneratorIntoTimeline", True),
    "fusion_generator": ("InsertFusionGeneratorIntoTimeline", True),
}


def _timelines(controller):
    project = _require_project(controller)
    count = int(_safe(project.GetTimelineCount, 0) or 0)
    out = []
    for i in range(1, count + 1):
        timeline = _safe(lambda n=i: project.GetTimelineByIndex(n))
        if timeline is not None:
            out.append(timeline)
    return out


def _folders(controller):
    """Every bin below the root, depth-first. The root itself is excluded -
    it cannot be deleted and offering it would only invite trying."""
    pool = _require_pool(controller)
    root = _safe(pool.GetRootFolder)
    if not root:
        raise ActionError("Could not access the Media Pool's root folder.")
    found, stack = [], list(_safe(root.GetSubFolderList, []) or [])
    while stack:
        folder = stack.pop()
        found.append(folder)
        stack.extend(_safe(folder.GetSubFolderList, []) or [])
    return found


def _bin_contents(folder):
    """(clip count, [timeline names], sub-bin count) for everything below
    one bin, however deep. Timelines are listed in bins as pseudo-clips, so
    they are told apart from real clips with the same guard _pool_clips
    uses - a card saying "12 clips" when four of them are edits would hide
    exactly the part that cannot be recreated."""
    clips, timelines, subbins = 0, [], 0
    stack = [folder]
    while stack:
        current = stack.pop()
        for clip in _safe(current.GetClipList, []) or []:
            if _is_real_media_clip(clip):
                clips += 1
            else:
                timelines.append(
                    _safe(lambda c=clip: c.GetClipProperty("Clip Name"), "")
                    or _safe(lambda c=clip: c.GetName(), "") or "unnamed"
                )
        children = _safe(current.GetSubFolderList, []) or []
        subbins += len(children)
        stack.extend(children)
    return clips, timelines, subbins


def _by_name(objects, name, what):
    matches = [o for o in objects if (_safe(o.GetName, "") or "") == name]
    if not matches:
        raise ActionError(
            f"No {what} named '{name}' exists. Nothing was changed - read "
            "the project before proposing rather than guessing names."
        )
    if len(matches) > 1:
        raise ActionError(
            f"{len(matches)} {what}s are named '{name}', so the reference is "
            "ambiguous. Nothing was changed."
        )
    return matches[0]


def _timeline_items(controller):
    """{(track, index): item} for every video track."""
    timeline = _require_timeline(controller)
    items = {}
    count = int(_safe(lambda: timeline.GetTrackCount("video"), 0) or 0)
    for track in range(1, count + 1):
        listed = _safe(lambda t=track: timeline.GetItemListInTrack("video", t), []) or []
        for index, item in enumerate(listed, start=1):
            items[(f"V{track}", index)] = item
    return items


def _find_timeline_clips(items, targets):
    """[(track, index, item)] for the model's [{track, index}] references.

    'index' is the clip's 1-based position within its track, exactly as
    project_state's timeline listing reports it. A reference may also carry
    the 'name' and/or 'start' it was read with; when it does they must still
    match, because a position that now holds a different clip is the one
    mistake an index cannot reveal on its own.
    """
    found, seen = [], set()
    for target in targets:
        if not isinstance(target, dict):
            raise ActionError(
                f"Clip {target!r} should be {{track, index}}, as project_state's "
                "timeline listing reports them."
            )
        track = str(target.get("track") or "").strip().upper()
        try:
            index = int(target.get("index"))
        except (TypeError, ValueError):
            raise ActionError(
                f"Clip {target!r} needs a numeric 'index'. Both 'track' and "
                "'index' are reported by project_state's timeline listing."
            )
        item = items.get((track, index))
        if item is None:
            raise ActionError(
                f"There is no clip at {track} index {index}. Nothing was "
                "changed; re-read the timeline before proposing again."
            )
        if (track, index) in seen:
            raise ActionError(f"{track} index {index} is listed twice.")
        seen.add((track, index))

        actual_name = _safe(item.GetName, "") or ""
        name = target.get("name")
        if name is not None and str(name).strip() != actual_name:
            raise ActionError(
                f"{track} index {index} is '{actual_name}', not '{name}'. "
                "Nothing was changed; re-read the timeline before proposing "
                "again."
            )
        start = target.get("start")
        if start is not None:
            actual_start = _safe(item.GetStart, None)
            try:
                matches = float(start) == float(actual_start)
            except (TypeError, ValueError):
                matches = False
            if not matches:
                raise ActionError(
                    f"{track} index {index} ('{actual_name}') starts at "
                    f"{actual_start}, not {start}. Nothing was changed; "
                    "re-read the timeline before proposing again."
                )
        found.append((track, index, item))
    return found


# ------------------------------------------------------ append_to_timeline


def preview_append_to_timeline(controller, args) -> ProposedAction:
    names = args.get("clips") or []
    _check_count(names, "clips")

    pool_clips = _pool_clips(controller)
    by_name = {}
    for clip in pool_clips:
        key = _safe(lambda c=clip: c.GetClipProperty("Clip Name"), "") or ""
        by_name.setdefault(key, []).append(clip)

    timeline = _require_timeline(controller)
    plan, details = [], []
    for name in names:
        name = (name or "").strip()
        matches = by_name.get(name) or []
        if not matches:
            raise ActionError(f"No Media Pool clip is named '{name}'.")
        if len(matches) > 1:
            raise ActionError(f"{len(matches)} clips are named '{name}'.")
        plan.append(matches[0])
        details.append(f"'{name}' appended to the end")

    return ProposedAction(
        action_id="append_to_timeline",
        summary=(
            f"Append {len(plan)} clip(s) to the END of "
            f"'{_safe(timeline.GetName, '')}'."
        ),
        details=details,
        args=args,
        plan=plan,
        # Appending adds after everything else; nothing already on the
        # timeline moves. This is the one timeline edit that is purely
        # additive, which is why it carries no warning.
    )


def execute_append_to_timeline(controller, proposal, log=lambda m: None) -> str:
    pool = _require_pool(controller)
    appended = pool.AppendToTimeline(proposal.plan)
    count = len(appended or [])
    log(f"Appended {count} clip(s).")
    return f"Appended {count} clip(s) to the timeline."


# ----------------------------------------------------- insert_into_timeline


def preview_insert_into_timeline(controller, args) -> ProposedAction:
    kind = (args.get("kind") or "").strip()
    if kind not in INSERT_KINDS:
        raise ActionError(
            f"'{kind}' is not something that can be inserted. "
            f"Valid: {', '.join(sorted(INSERT_KINDS))}."
        )
    method, needs_name = INSERT_KINDS[kind]
    name = (args.get("name") or "").strip()
    if needs_name and not name:
        raise ActionError(
            f"Inserting a {kind.replace('_', ' ')} needs its name, exactly as "
            "it appears in the Effects Library."
        )

    timeline = _require_timeline(controller)
    if not hasattr(timeline, method):
        raise ActionError(
            f"This version of Resolve does not expose {method}()."
        )

    label = kind.replace("_", " ")
    return ProposedAction(
        action_id="insert_into_timeline",
        summary=(
            f"Insert a {label}" + (f" ('{name}')" if name else "")
            + f" at the playhead on '{_safe(timeline.GetName, '')}'."
        ),
        details=[
            f"inserted at the current playhead position",
            "everything to the right of the playhead SHIFTS LATER "
            "(Resolve's insert edit ripples the timeline)",
        ],
        args=args,
        plan=[method, name],
        destructive=True,
        warnings=[
            "This moves clips you did not mention. Check the playhead is "
            "where you think it is before applying."
        ],
    )


def execute_insert_into_timeline(controller, proposal, log=lambda m: None) -> str:
    timeline = _require_timeline(controller)
    method, name = proposal.plan
    call = getattr(timeline, method)
    item = call(name) if name else call()
    if not item:
        raise ActionError(f"Resolve refused the insert ({method}).")
    log(f"Inserted via {method}.")
    return "Inserted at the playhead."


# --------------------------------------------------------- create_timeline


def preview_create_timeline(controller, args) -> ProposedAction:
    name = (args.get("name") or "").strip()
    if not name:
        raise ActionError("A timeline needs a name.")
    existing = [_safe(t.GetName, "") for t in _timelines(controller)]
    if name in existing:
        raise ActionError(f"A timeline named '{name}' already exists.")
    return ProposedAction(
        action_id="create_timeline",
        summary=f"Create a new empty timeline named '{name}'.",
        details=[f"alongside: {', '.join(existing) or 'no other timelines'}"],
        args=args,
        plan=[name],
    )


def execute_create_timeline(controller, proposal, log=lambda m: None) -> str:
    pool = _require_pool(controller)
    name = proposal.plan[0]
    if pool.CreateEmptyTimeline(name) is None:
        raise ActionError(f"Resolve refused to create a timeline named '{name}'.")
    log(f"Created timeline '{name}'.")
    return f"Created the timeline '{name}'."


# ============================== DESTRUCTIVE ===============================


def preview_delete_markers(controller, args) -> ProposedAction:
    timeline = _require_timeline(controller)
    existing = _safe(timeline.GetMarkers, {}) or {}
    if not existing:
        raise ActionError("This timeline has no markers, so there is nothing to delete.")

    color = (args.get("color") or "").strip()
    frames = args.get("frames") or []
    if color and frames:
        raise ActionError("Give either a colour or a list of frames, not both.")

    details = []
    if color:
        if color != "All" and color not in MARKER_COLORS:
            raise ActionError(
                f"'{color}' is not a marker colour. Use one of "
                f"{', '.join(MARKER_COLORS)}, or 'All'."
            )
        doomed = [
            (f, info) for f, info in existing.items()
            if color == "All" or info.get("color") == color
        ]
        if not doomed:
            raise ActionError(f"No {color} markers exist on this timeline.")
        plan = [("color", color)]
    else:
        _check_count(frames, "frames")
        doomed = []
        for frame in frames:
            try:
                key = float(frame)
            except (TypeError, ValueError):
                raise ActionError(f"'{frame}' is not a frame number.")
            match = next((f for f in existing if float(f) == key), None)
            if match is None:
                raise ActionError(f"There is no marker at frame {frame}.")
            doomed.append((match, existing[match]))
        plan = [("frames", [f for f, _ in doomed])]

    for frame, info in sorted(doomed, key=lambda p: float(p[0])):
        details.append(
            f"frame {frame} – {info.get('color', '?')} – "
            f"{info.get('name') or 'unnamed'}"
        )

    return ProposedAction(
        action_id="delete_markers",
        summary=f"DELETE {len(doomed)} marker(s) from "
                f"'{_safe(timeline.GetName, '')}'.",
        details=details,
        args=args,
        plan=plan,
        destructive=True,
        warnings=["Deleted markers and their notes cannot be recovered by Buddy."],
    )


def execute_delete_markers(controller, proposal, log=lambda m: None) -> str:
    timeline = _require_timeline(controller)
    mode, value = proposal.plan[0]
    if mode == "color":
        if not timeline.DeleteMarkersByColor(value):
            raise ActionError(f"Resolve refused to delete the {value} markers.")
        log(f"Deleted all {value} markers.")
        return f"Deleted the {value} markers."
    done = 0
    for frame in value:
        if timeline.DeleteMarkerAtFrame(frame):
            done += 1
            log(f"Deleted the marker at frame {frame}.")
        else:
            log(f"Resolve refused to delete the marker at frame {frame}.")
    return f"Deleted {done} of {len(value)} marker(s)."


def preview_delete_timeline_clips(controller, args) -> ProposedAction:
    targets = args.get("clips") or []
    _check_count(targets, "clips")
    ripple = bool(args.get("ripple"))

    items = _timeline_items(controller)
    plan, details = [], []
    for track, index, item in _find_timeline_clips(items, targets):
        plan.append(item)
        details.append(f"{track} clip {index}: '{_safe(item.GetName, '')}'")

    warnings = ["These clips are removed from the timeline."]
    if ripple:
        warnings.append(
            "Ripple delete is ON: everything after each removed clip SHIFTS "
            "EARLIER to close the gap."
        )
    else:
        warnings.append("A gap is left where each clip was.")

    return ProposedAction(
        action_id="delete_timeline_clips",
        summary=f"DELETE {len(plan)} clip(s) from the timeline"
                + (" (ripple)" if ripple else ""),
        details=details,
        args=args,
        plan=[plan, ripple],
        destructive=True,
        warnings=warnings,
    )


def execute_delete_timeline_clips(controller, proposal, log=lambda m: None) -> str:
    timeline = _require_timeline(controller)
    items, ripple = proposal.plan
    if not timeline.DeleteClips(items, ripple):
        raise ActionError("Resolve refused to delete those timeline clips.")
    log(f"Deleted {len(items)} timeline clip(s).")
    return f"Deleted {len(items)} clip(s) from the timeline."


def preview_delete_media_pool_items(controller, args) -> ProposedAction:
    clips = args.get("clips") or []
    timelines = args.get("timelines") or []
    bins = args.get("bins") or []
    total = len(clips) + len(timelines) + len(bins)
    if not total:
        raise ActionError("Nothing was listed for deletion.")
    if total > MAX_ITEMS_PER_ACTION:
        raise ActionError(
            f"{total} items is more than one action may delete "
            f"({MAX_ITEMS_PER_ACTION} max)."
        )

    details, warnings = [], []
    clip_objs, timeline_objs, bin_objs = [], [], []

    if clips:
        pool_clips = _pool_clips(controller)
        by_name = {}
        for clip in pool_clips:
            key = _safe(lambda c=clip: c.GetClipProperty("Clip Name"), "") or ""
            by_name.setdefault(key, []).append(clip)
        for name in clips:
            matches = by_name.get((name or "").strip()) or []
            if not matches:
                raise ActionError(f"No Media Pool clip is named '{name}'.")
            if len(matches) > 1:
                raise ActionError(f"{len(matches)} clips are named '{name}'.")
            clip_objs.append(matches[0])
            details.append(f"clip: '{name}'")

    if timelines:
        available = _timelines(controller)
        current = _safe(_require_project(controller).GetCurrentTimeline)
        current_name = _safe(current.GetName, "") if current else ""
        for name in timelines:
            timeline = _by_name(available, (name or "").strip(), "timeline")
            timeline_objs.append(timeline)
            details.append(f"TIMELINE: '{name}'")
            if name == current_name:
                warnings.append(
                    f"'{name}' is the timeline currently open."
                )

    # Deleting a bin takes everything below it, so the cap and the card both
    # count the whole subtree - not just the bin, and not just its top level.
    inside = 0
    timelines_in_bins = 0
    if bins:
        available = _folders(controller)
        current = _safe(_require_project(controller).GetCurrentTimeline)
        current_name = _safe(current.GetName, "") if current else ""
        for name in bins:
            folder = _by_name(available, (name or "").strip(), "bin")
            bin_objs.append(folder)
            n_clips, bin_timelines, n_subbins = _bin_contents(folder)
            inside += n_clips + len(bin_timelines) + n_subbins
            timelines_in_bins += len(bin_timelines)
            details.append(
                f"BIN: '{name}' and everything in it – {n_clips} clip(s), "
                f"{len(bin_timelines)} timeline(s), {n_subbins} sub-bin(s)"
            )
            if bin_timelines:
                warnings.append(
                    f"Bin '{name}' holds {len(bin_timelines)} timeline(s) "
                    f"({', '.join(repr(t) for t in bin_timelines)}); they are "
                    "deleted with it."
                )
            if current_name and current_name in bin_timelines:
                warnings.append(
                    f"'{current_name}', the timeline currently open, is inside "
                    f"bin '{name}'."
                )
        if total + inside > MAX_ITEMS_PER_ACTION:
            raise ActionError(
                f"Deleting those bins would delete {total + inside} items in "
                f"total, counting everything inside them, which is more than "
                f"one action may delete ({MAX_ITEMS_PER_ACTION} max)."
            )

    warnings.insert(
        0,
        "Deleting a timeline destroys the edit it contains. Buddy cannot "
        "bring it back, and Resolve's undo does not always cover media pool "
        "deletions."
        if timelines or timelines_in_bins
        else "This removes items from the Media Pool.",
    )

    return ProposedAction(
        action_id="delete_media_pool_items",
        summary=f"DELETE {total} item(s) from the Media Pool"
                + (f", plus {inside} item(s) inside the bins." if inside else "."),
        details=details,
        args=args,
        plan=[clip_objs, timeline_objs, bin_objs],
        destructive=True,
        warnings=warnings,
    )


def execute_delete_media_pool_items(controller, proposal, log=lambda m: None) -> str:
    pool = _require_pool(controller)
    clip_objs, timeline_objs, bin_objs = proposal.plan
    done = []
    # Timelines first: deleting a bin that holds one would take it anyway,
    # and doing it in this order keeps the log honest about what went.
    if timeline_objs and not pool.DeleteTimelines(timeline_objs):
        raise ActionError("Resolve refused to delete those timelines.")
    if timeline_objs:
        done.append(f"{len(timeline_objs)} timeline(s)")
        log(f"Deleted {len(timeline_objs)} timeline(s).")
    if clip_objs and not pool.DeleteClips(clip_objs):
        raise ActionError("Resolve refused to delete those clips.")
    if clip_objs:
        done.append(f"{len(clip_objs)} clip(s)")
        log(f"Deleted {len(clip_objs)} clip(s).")
    if bin_objs and not pool.DeleteFolders(bin_objs):
        raise ActionError("Resolve refused to delete those bins.")
    if bin_objs:
        done.append(f"{len(bin_objs)} bin(s)")
        log(f"Deleted {len(bin_objs)} bin(s).")
    return "Deleted " + ", ".join(done) + "."


# =========================== PROJECT SETTINGS =============================


def _preview_settings(controller, args, target, label, action_id):
    settings = args.get("settings") or {}
    if not isinstance(settings, dict) or not settings:
        raise ActionError("No settings were given.")
    if len(settings) > MAX_ITEMS_PER_ACTION:
        raise ActionError(f"Too many settings at once ({len(settings)}).")

    details, plan, warnings = [], [], []
    for key, value in settings.items():
        key = str(key).strip()
        value = str(value)
        current = _safe(lambda k=key: target.GetSetting(k), "")
        if current in ("", None):
            # GetSetting returns "" for a key this Resolve version does not
            # have, so a blank reading means the key is probably wrong - and
            # SetSetting would silently do nothing.
            warnings.append(
                f"'{key}' reads back empty, so it may not be a valid setting "
                "key for this version of Resolve."
            )
            details.append(f"{key}: (unset or unknown)  ->  {value}")
        elif str(current) == value:
            raise ActionError(
                f"'{key}' is already '{value}', so there is nothing to change."
            )
        else:
            details.append(f"{key}: {current}  ->  {value}")
        plan.append((key, value))

    warnings.insert(
        0,
        "Changing these alters how every clip in the project is interpreted "
        "and rendered. A wrong value here is not obvious on screen.",
    )
    return ProposedAction(
        action_id=action_id,
        summary=f"Change {len(plan)} {label} setting(s).",
        details=details,
        args=args,
        plan=plan,
        destructive=True,
        warnings=warnings,
    )


def preview_set_project_settings(controller, args) -> ProposedAction:
    return _preview_settings(
        controller, args, _require_project(controller), "project",
        "set_project_settings",
    )


def execute_set_project_settings(controller, proposal, log=lambda m: None) -> str:
    project = _require_project(controller)
    done = 0
    for key, value in proposal.plan:
        if project.SetSetting(key, value):
            done += 1
            log(f"Set {key} = {value}.")
        else:
            log(f"Resolve refused to set {key}.")
    return f"Changed {done} of {len(proposal.plan)} project setting(s)."


def preview_set_timeline_settings(controller, args) -> ProposedAction:
    return _preview_settings(
        controller, args, _require_timeline(controller), "timeline",
        "set_timeline_settings",
    )


def execute_set_timeline_settings(controller, proposal, log=lambda m: None) -> str:
    timeline = _require_timeline(controller)
    done = 0
    for key, value in proposal.plan:
        if timeline.SetSetting(key, value):
            done += 1
            log(f"Set {key} = {value}.")
        else:
            log(f"Resolve refused to set {key}.")
    return f"Changed {done} of {len(proposal.plan)} timeline setting(s)."


# ---------------------------------------------------------------- registry
#
# Three tables, deliberately separate, and kept in step by the test suite
# rather than by remembering:
#
#   ACTIONS              id -> (preview, execute)
#   ACTION_SUMMARIES     id -> one line for the user, used by "help"
#   DESTRUCTIVE_ACTIONS  which ones can move or destroy unnamed work
#
# The alternative was describing the actions again in the help text, which
# would have gone stale the first time one was added. test_full_actions.py
# asserts all three have identical keys.

ACTION_SUMMARIES = {
    "add_markers": "add markers to the timeline",
    "create_bin": "create a Media Pool bin",
    "create_timeline": "create a new empty timeline",
    "rename_clips": "rename Media Pool clips",
    "set_clip_colors": "set the colour of clips on the timeline",
    "append_to_timeline": "append clips to the END of the timeline",
    "insert_into_timeline": (
        "insert a Fusion composition, title or generator at the playhead "
        "(this ripples – everything after it shifts later)"
    ),
    "delete_markers": "delete markers, by colour or by frame",
    "delete_timeline_clips": "delete clips from the timeline",
    "delete_media_pool_items": "delete clips, timelines or bins from the Media Pool",
    "set_project_settings": "change project settings (frame rate, colour management…)",
    "set_timeline_settings": "change the current timeline's settings",
}

DESTRUCTIVE_ACTIONS = frozenset({
    "insert_into_timeline",
    "delete_markers",
    "delete_timeline_clips",
    "delete_media_pool_items",
    "set_project_settings",
    "set_timeline_settings",
})

# Actions whose plan belongs to the timeline open at preview time - Apply is
# refused if a different one is open by then. The rest act on the project as
# a whole (bins, Media Pool clips, the timeline list, project settings), so
# only the project has to be the same.
TIMELINE_ACTIONS = frozenset({
    "add_markers",
    "set_clip_colors",
    "append_to_timeline",
    "insert_into_timeline",
    "delete_markers",
    "delete_timeline_clips",
    "set_timeline_settings",
})


ACTIONS = {
    # Additive: nothing here moves or removes anything already in place.
    "add_markers": (preview_add_markers, execute_add_markers),
    "create_bin": (preview_create_bin, execute_create_bin),
    "create_timeline": (preview_create_timeline, execute_create_timeline),
    "rename_clips": (preview_rename_clips, execute_rename_clips),
    "set_clip_colors": (preview_set_clip_colors, execute_set_clip_colors),
    "append_to_timeline": (preview_append_to_timeline, execute_append_to_timeline),
    # Edits and deletions: these move or destroy work the user may not have
    # mentioned, and every one sets ProposedAction.destructive.
    "insert_into_timeline": (preview_insert_into_timeline, execute_insert_into_timeline),
    "delete_markers": (preview_delete_markers, execute_delete_markers),
    "delete_timeline_clips": (
        preview_delete_timeline_clips, execute_delete_timeline_clips),
    "delete_media_pool_items": (
        preview_delete_media_pool_items, execute_delete_media_pool_items),
    "set_project_settings": (
        preview_set_project_settings, execute_set_project_settings),
    "set_timeline_settings": (
        preview_set_timeline_settings, execute_set_timeline_settings),
}


def preview(controller, action_id, args) -> ProposedAction:
    if action_id not in ACTIONS:
        raise ActionError(
            f"Unknown action '{action_id}'. Available: "
            f"{', '.join(sorted(ACTIONS))}."
        )
    # Read before AND checked after the preview's own reads, so the recorded
    # target is guaranteed to be the one the item list was built from.
    target = _target_identity(controller, action_id)
    proposal = ACTIONS[action_id][0](controller, args or {})
    _check_target(controller, action_id, target)
    proposal.target = target
    # DESTRUCTIVE_ACTIONS is authoritative. An individual preview may also
    # set the flag, but it can never clear it by forgetting to - which is
    # exactly the mistake that would ship a delete looking like an add.
    proposal.destructive = (
        proposal.destructive or action_id in DESTRUCTIVE_ACTIONS
    )
    return proposal


def execute(controller, proposal, log=lambda m: None) -> str:
    if proposal.action_id not in ACTIONS:
        raise ActionError(f"Unknown action '{proposal.action_id}'.")
    # One place for every action: the card was built from one project and
    # timeline, and Apply must not land on another.
    _check_target(controller, proposal.action_id, proposal.target)
    return ACTIONS[proposal.action_id][1](controller, proposal, log)


def describe_actions() -> str:
    """For the system prompt - the model should not guess action ids."""
    return "\n".join(f"- {aid}" for aid in sorted(ACTIONS))


def help_lines():
    """(additive, destructive) lists of plain-language capability lines.

    Built from the registry, so an action added without a summary shows up
    as a test failure rather than as a silent gap in what Buddy claims it
    can do.
    """
    additive, destructive = [], []
    for aid in sorted(ACTIONS):
        line = ACTION_SUMMARIES.get(aid, aid)
        (destructive if aid in DESTRUCTIVE_ACTIONS else additive).append(line)
    return additive, destructive
