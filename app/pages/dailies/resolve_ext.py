"""Resolve reads and verified metadata writes for Dailies."""

from core.resolve_bridge import ResolveConnectionError
from pages.project_setup import metadata

from . import model
from .model import media_type


def _list(value):
    return list(value.values()) if isinstance(value, dict) else list(value or [])


def current_location(controller):
    """Cheap project/bin identity check for the live Dailies source."""
    project = controller.current_project()
    if project is None:
        raise ResolveConnectionError("Open a project in Resolve first.")
    pool = project.GetMediaPool()
    if pool is None:
        raise ResolveConnectionError("Could not access the Media Pool.")
    folder = pool.GetCurrentFolder() or pool.GetRootFolder()
    if folder is None:
        raise ResolveConnectionError("Could not access the Media Pool's root bin.")
    return str(project.GetUniqueId()), str(folder.GetUniqueId())


def scan(controller):
    project = controller.current_project()
    if project is None:
        raise ResolveConnectionError("Open a project in Resolve first.")
    pool = project.GetMediaPool()
    if pool is None:
        raise ResolveConnectionError("Could not access the Media Pool.")
    root = pool.GetRootFolder()
    if root is None:
        raise ResolveConnectionError("Could not access the Media Pool's root bin.")
    current = pool.GetCurrentFolder() or root
    current_id = str(current.GetUniqueId())
    clips = {}
    objects = {}
    current_ids = []

    def walk(folder, names):
        folder_id = str(folder.GetUniqueId())
        for clip in _list(folder.GetClipList()):
            try:
                props = clip.GetClipProperty() or {}
                if props.get("Type") == "Timeline":
                    continue
                clip_id = str(clip.GetUniqueId())
                if not clip_id or clip_id in clips:
                    continue
                path = str(props.get("File Path") or "")
                clip_meta = clip.GetMetadata() or {}
                kind = media_type(props.get("Type"), path)
                row = {"id": clip_id, "name": str(props.get("Clip Name") or clip.GetName() or "Untitled"),
                       "bin": "/".join(names), "bin_id": folder_id, "path": path, "type": kind,
                       "duration": str(props.get("Duration") or ""),
                       "seconds": model.clip_seconds(kind, props.get("Frames"), props.get("FPS")),
                       "metadata": {key: str(clip_meta.get(key, "") or "")
                                    for key in ("Scene", "Shot", "Take", "Camera #", "Reel Number", "Comments", "Tag")}}
                row["metadata"]["Clip Color"] = str(clip.GetClipColor() or "")
                clips[clip_id], objects[clip_id] = row, clip
                if folder_id == current_id:
                    current_ids.append(clip_id)
            except Exception:
                continue  # one malformed Media Pool item must not hide the rest
        for child in _list(folder.GetSubFolderList()):
            walk(child, [*names, str(child.GetName() or "Bin")])

    walk(root, [str(root.GetName() or "Master")])
    return {"project_id": str(project.GetUniqueId()), "project_name": str(project.GetName() or "Project"),
            "current_bin": str(current.GetName() or "Master"), "current_bin_id": current_id,
            "current_ids": current_ids,
            "clips": clips, "objects": objects}


def apply_drafts(controller, expected_project_id, objects, drafts):
    """Apply only changed fields to existing Media Pool items, checking each result."""
    project = controller.current_project()
    if project is None or str(project.GetUniqueId()) != expected_project_id:
        raise ResolveConnectionError("The open Resolve project changed. Refresh Dailies before applying notes.")
    saved, failures, written = [], [], {}
    for clip_id, draft in drafts.items():
        clip = objects.get(clip_id)
        if clip is None:
            failures.append(f"Clip {clip_id} is no longer in the Media Pool.")
            continue
        try:
            changes = model.changes_for(draft, metadata.values(clip))
            if not changes:
                saved.append(clip_id)
                continue
            _, errors = metadata.apply([clip], changes)
            if errors:
                failures.extend(errors)
            else:
                saved.append(clip_id)
                written[clip_id] = changes
        except Exception as exc:
            failures.append(f"{clip.GetName()}: {exc}")
    return saved, failures, written
