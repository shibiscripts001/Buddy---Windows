#!/usr/bin/env python3
"""
Image Importer's own Resolve calls - kept separate from
core/resolve_bridge.py, which only owns the shared connect/bootstrap
plumbing every tool needs.

A free function taking a controller, matching the other tools. The bin
create-or-reuse logic keeps a running commentary: the log
lines naming which bins already existed are what make "it imported
somewhere else" diagnosable after the fact.

WRITES to Resolve: creates a Media Pool bin, changes the current folder,
and imports media into it.
"""

from core.resolve_bridge import ResolveConnectionError


def get_project(controller):
    project = controller.current_project()
    if not project:
        raise ResolveConnectionError("No project is open in Resolve!")
    return project


def import_to_bin(controller, paths, bin_name, log=lambda msg: None):
    """Create (or reuse) a root-level bin and import every path into it."""
    project = get_project(controller)
    log(f"Using project '{project.GetName()}'.")

    media_pool = project.GetMediaPool()
    if media_pool is None:
        raise ResolveConnectionError("Could not access the Media Pool.")

    root_folder = media_pool.GetRootFolder()
    if root_folder is None:
        raise ResolveConnectionError(
            "Could not access the Media Pool's root folder."
        )

    target = None
    existing_names = []
    for folder in root_folder.GetSubFolderList():
        existing_names.append(folder.GetName())
        if folder.GetName() == bin_name:
            target = folder
            break

    if target is None:
        log(
            f"No existing bin named '{bin_name}' "
            f"(found: {', '.join(existing_names) or 'none'}) – creating it."
        )
        target = media_pool.AddSubFolder(root_folder, bin_name)
        if target is None:
            raise ResolveConnectionError(
                f"Resolve's API refused to create a bin named '{bin_name}'."
            )
    else:
        log(f"Found existing bin '{bin_name}' – reusing it.")

    log(f"Set current folder to '{bin_name}': {media_pool.SetCurrentFolder(target)}")

    log(f"Calling ImportMedia with {len(paths)} path(s)…")
    imported = media_pool.ImportMedia(paths)
    if not imported:
        raise ResolveConnectionError(
            "Resolve's ImportMedia() returned nothing. This usually means the "
            "file paths weren't recognized as valid media, or the bin wasn't "
            "actually set current."
        )
    log(f"ImportMedia returned {len(imported)} item(s).")
    return imported
