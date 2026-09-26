#!/usr/bin/env python3
"""
Asset Manager's own Resolve calls - kept separate from
core/resolve_bridge.py, which only owns the shared connect/bootstrap
plumbing every tool needs.

Free functions taking a controller, matching the other tools (see
media_relink/resolve_ext.py). The shell's probed connect() owns
connection lifetime, so this module is pure per-action calls - both of
which change the Media Pool (import media, create a sub-bin).
"""

from core.resolve_bridge import ResolveConnectionError


def _get_media_pool(controller):
    project = controller.current_project()
    if not project:
        raise ResolveConnectionError("No project is open in Resolve.")
    media_pool = project.GetMediaPool()
    if media_pool is None:
        raise ResolveConnectionError("Could not access the Media Pool.")
    return media_pool


def import_to_media_pool(controller, paths):
    media_pool = _get_media_pool(controller)
    imported = media_pool.ImportMedia(paths)
    if not imported:
        raise ResolveConnectionError(
            "Resolve reported the import failed (check the files are valid media)."
        )
    return imported


def import_folder_to_media_pool(controller, folder_name, paths):
    """Imports paths into a new sub-bin named folder_name under whatever bin is
    currently selected in the Media Pool, then restores that bin as current so
    navigation is left the way the user had it."""
    media_pool = _get_media_pool(controller)
    current_folder = media_pool.GetCurrentFolder()
    sub_folder = media_pool.AddSubFolder(current_folder, folder_name)
    if sub_folder is None:
        raise ResolveConnectionError(
            f"Could not create bin '{folder_name}' in the Media Pool."
        )
    media_pool.SetCurrentFolder(sub_folder)
    try:
        imported = media_pool.ImportMedia(paths)
    finally:
        media_pool.SetCurrentFolder(current_folder)
    if not imported:
        raise ResolveConnectionError(
            "Resolve reported the import failed (check the files are valid media)."
        )
    return imported