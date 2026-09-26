#!/usr/bin/env python3
"""
Image Importer's list of images waiting to go into Resolve, without Qt.

Everything staged is a real file in the save folder: pasted images are
saved there, copied/dropped files are copied there (never moved - the
originals stay put), downloads land there. Removing an image from the list
leaves its file on disk.
"""

import os
import re
import shutil
import uuid
from datetime import datetime

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")
    return name or "image"


def unique_filename(desired_name, folder):
    """desired_name, or 'name (2).ext' etc. if that already exists."""
    base, ext = os.path.splitext(desired_name)
    candidate = desired_name
    counter = 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{base} ({counter}){ext}"
        counter += 1
    return candidate


class Staging:
    def __init__(self):
        self.items = []   # {id, path, name, source, added_at}

    def add(self, path, source):
        item = {"id": uuid.uuid4().hex, "path": path, "name": os.path.basename(path),
                "source": source, "added_at": datetime.now().strftime("%H:%M:%S")}
        self.items.append(item)
        return item

    def remove(self, ids):
        ids = set(ids or [])
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] not in ids]
        return before - len(self.items)

    def clear(self):
        count, self.items = len(self.items), []
        return count

    def split_existing(self):
        """(items whose file is still there, names of ones that are gone)."""
        present = [i for i in self.items if os.path.exists(i["path"])]
        missing = [i["name"] for i in self.items if not os.path.exists(i["path"])]
        return present, missing


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def copy_into(paths, folder):
    """Copies the image files among `paths` into `folder` (renaming on a
    clash). Returns (copied destination paths, skipped names, errors)."""
    os.makedirs(folder, exist_ok=True)
    copied, skipped, errors = [], [], []
    for path in paths:
        if not os.path.isfile(path):
            continue
        if not is_image(path):
            skipped.append(os.path.basename(path))
            continue
        # A file already in the save folder is staged as it is, not copied
        # onto itself as "name (2)".
        if os.path.normcase(os.path.dirname(os.path.abspath(path))) == os.path.normcase(os.path.abspath(folder)):
            copied.append(path)
            continue
        try:
            dest = os.path.join(folder, unique_filename(os.path.basename(path), folder))
            shutil.copy2(path, dest)
        except OSError as exc:
            errors.append(f"{os.path.basename(path)}: {exc}")
            continue
        copied.append(dest)
    return copied, skipped, errors


def pasted_name():
    return f"pasted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
