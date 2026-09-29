"""Media Pool metadata snapshots and explicit bulk edits. No Qt."""

import re

from .resolve_ext import _is_real_media_clip

COLORS = ("Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
          "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate")
FIELDS = (
    {"key": "Start TC", "label": "Timecode", "kind": "text"},
    {"key": "Date Created", "label": "Date Created", "kind": "readonly"},
    {"key": "Camera #", "label": "Camera", "kind": "text"},
    {"key": "Reel Number", "label": "Reel", "kind": "text"},
    {"key": "Scene", "label": "Scene", "kind": "text"},
    {"key": "Shot", "label": "Shot", "kind": "text"},
    {"key": "Take", "label": "Take", "kind": "text"},
    {"key": "Tag", "label": "Tag", "kind": "tag"},
    {"key": "Clip Color", "label": "Clip Color", "kind": "color"},
    {"key": "Clip Name", "label": "Name", "kind": "text"},
    {"key": "Comments", "label": "Comments", "kind": "multiline"},
)
EDITABLE = {field["key"] for field in FIELDS if field["kind"] != "readonly"}
METADATA_KEYS = {"Camera #", "Reel Number", "Scene", "Shot", "Take", "Comments", "Tag"}


class MetadataError(ValueError):
    pass


def selected_clips(project):
    if project is None:
        raise MetadataError("Open a project in Resolve first.")
    pool = project.GetMediaPool()
    try:
        clips = pool.GetSelectedClips()
    except Exception as exc:
        raise MetadataError("This Resolve version could not read the Media Pool selection. "
                            "Update Resolve to edit selected clips here.") from exc
    if clips is None:
        clips = []
    if isinstance(clips, dict):
        clips = list(clips.values())
    return [clip for clip in clips if _is_real_media_clip(clip)]


def identity(obj):
    value = obj.GetUniqueId()
    if not value:
        raise MetadataError("Resolve did not return a stable identity. Refresh and try again.")
    return str(value)


def signature(project, clips):
    return identity(project), tuple(sorted({identity(clip) for clip in clips}))


def values(clip):
    props = clip.GetClipProperty() or {}
    meta = clip.GetMetadata() or {}
    result = {field["key"]: str(props.get(field["key"], meta.get(field["key"], "")) or "")
              for field in FIELDS}
    for key in METADATA_KEYS:
        if key in meta:
            result[key] = str(meta[key] or "")
    result["Clip Color"] = clip.GetClipColor() or ""
    result["Clip Name"] = result["Clip Name"] or clip.GetName() or ""
    result["Tag"] = result["Tag"] or "0"
    return result


def snapshot(clips):
    records = [values(clip) for clip in clips]
    fields = []
    for field in FIELDS:
        choices = {record[field["key"]] for record in records}
        fields.append({**field, "mixed": len(choices) > 1,
                       "value": next(iter(choices)) if len(choices) == 1 else ""})
    return {"count": len(clips), "names": [r["Clip Name"] for r in records], "fields": fields,
            "colors": list(COLORS)}


def validate(changes):
    if not isinstance(changes, dict) or not changes:
        raise MetadataError("Choose at least one field to apply.")
    for key, value in changes.items():
        if key not in EDITABLE or not isinstance(value, str):
            raise MetadataError("The metadata edit contains an unsupported field or value.")
        if key == "Start TC" and not re.fullmatch(r"\d{2}:[0-5]\d:[0-5]\d[:;]\d{2}", value):
            raise MetadataError("Use timecode in HH:MM:SS:FF format (or HH:MM:SS;FF for drop frame).")
        if key == "Clip Name" and not value.strip():
            raise MetadataError("A clip name cannot be empty.")
        if key == "Clip Color" and value not in ("", *COLORS):
            raise MetadataError("Choose a valid clip color.")
        if key == "Tag" and value not in ("0", "1", "-1"):
            raise MetadataError("Choose Good Take, Untagged, or Rejected.")
    return dict(changes)


def apply(clips, changes):
    """Write only explicitly chosen fields; report refusals per clip and field."""
    changes = validate(changes)
    updated, failures = 0, []
    for clip in clips:
        try:
            before = values(clip)
        except Exception as exc:
            failures.append(f"Could not read a selected clip: {exc}")
            continue
        ok_clip = True
        for key, value in changes.items():
            if before[key] == value:
                continue
            try:
                if key == "Clip Color":
                    clip.SetClipColor(value) if value else clip.ClearClipColor()
                elif key in METADATA_KEYS:
                    clip.SetMetadata({key: value})
                elif key == "Clip Name" and callable(getattr(clip, "SetName", None)):
                    clip.SetName(value)
                else:
                    clip.SetClipProperty(key, value)
                # Resolve can return False after a successful rename. Verify
                # the actual field instead of reporting that as a failure.
                actual = values(clip)[key]
                matches = actual == value or (key == "Start TC" and actual.replace(";", ":") == value.replace(";", ":"))
                if not matches:
                    raise MetadataError("Resolve did not accept this value.")
            except Exception as exc:
                ok_clip = False
                failures.append(f'{before["Clip Name"]} — {key}: {exc}')
        if ok_clip:
            updated += 1
    return updated, failures
