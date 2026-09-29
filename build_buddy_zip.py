#!/usr/bin/env python3
"""Builds buddy.zip from the app/ source folder, and optionally deploys it.

Same pattern as the standalone tools' own zip builders: Resolve's
Scripts menu lists any subfolder as a browsable submenu, so shipping the
app as a loose folder would put every .py file in it on the menu. One zip
plus one stub gives Resolve exactly one entry.

The archive's top-level folder is "buddy/", NOT "app/" - the launcher puts
that extracted folder on sys.path and does `import main`, and naming it
after the app keeps the temp cache folder legible.

Data files matter here in a way they did not for the single-tool zips:
core/tools_kb.json is what the chat agent knows about the other tools, and
it is not a .py file. Anything non-source that the app reads at runtime has
to be listed in _INCLUDE_SUFFIXES or it silently goes missing once deployed.

Usage:
    python build_buddy_zip.py            # build buddy.zip here
    python build_buddy_zip.py --deploy   # also copy stub + zip to Resolve
"""

import argparse
import os
import shutil
import zipfile

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(REPO_ROOT, "app")
OUTPUT_ZIP = os.path.join(REPO_ROOT, "buddy.zip")
STUB_NAME = "Buddy.py"
PACKAGE_DIR_NAME = "buddy"

DEPLOY_DIR = os.path.expandvars(
    r"%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
)

# Bytecode caches, VCS/editor/tool folders and logs never ship.
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache", ".git"}
SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".log")
# Everything the app reads at runtime, source or not.
INCLUDE_SUFFIXES = (".py", ".json", ".qss", ".svg", ".png", ".ico", ".txt", ".md", ".ttf",
                    ".drb",   # the Text+ template (a Resolve bin, text_animator/)
                    ".html", ".css", ".js",   # the web tool pages (core/web_page.py)
                    ".qml")   # Dailies' video surface (pages/dailies/video_surface.py)


def iter_source_files():
    for root, dirnames, filenames in os.walk(SOURCE_DIR):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for filename in sorted(filenames):
            if filename.endswith(SKIP_FILE_SUFFIXES):
                continue
            if not filename.endswith(INCLUDE_SUFFIXES):
                continue
            yield os.path.join(root, filename)


def build():
    if not os.path.isdir(SOURCE_DIR):
        raise SystemExit(f"Expected source folder not found: {SOURCE_DIR}")
    if os.path.exists(OUTPUT_ZIP):
        os.remove(OUTPUT_ZIP)

    written = []
    with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as archive:
        for full_path in iter_source_files():
            # app/core/theme.py  ->  buddy/core/theme.py
            relative = os.path.relpath(full_path, SOURCE_DIR)
            arcname = os.path.join(PACKAGE_DIR_NAME, relative)
            archive.write(full_path, arcname)
            written.append(arcname.replace("\\", "/"))
        # Which Buddy this is, for bug reports (core/app_version.py).
        with open(os.path.join(REPO_ROOT, "VERSION"), encoding="utf-8") as f:
            archive.writestr(f"{PACKAGE_DIR_NAME}/VERSION", f.read().strip() + "\n")
        written.append(f"{PACKAGE_DIR_NAME}/VERSION")

    size_mb = os.path.getsize(OUTPUT_ZIP) / 1e6
    print(f"Wrote {OUTPUT_ZIP} ({len(written)} files, {size_mb:.2f} MB)")
    return written


def deploy():
    if not os.path.isdir(DEPLOY_DIR):
        raise SystemExit(
            f"Resolve's Scripts/Utility folder not found:\n  {DEPLOY_DIR}\n"
            "Is DaVinci Resolve installed for this user?"
        )
    for name, source in (
        (STUB_NAME, os.path.join(REPO_ROOT, STUB_NAME)),
        (os.path.basename(OUTPUT_ZIP), OUTPUT_ZIP),
    ):
        if not os.path.exists(source):
            raise SystemExit(f"Missing {source} - run the build first.")
        shutil.copy2(source, os.path.join(DEPLOY_DIR, name))
        print(f"Deployed {name} -> {DEPLOY_DIR}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="copy the stub and zip into Resolve's Scripts/Utility folder",
    )
    args = parser.parse_args()
    build()
    if args.deploy:
        deploy()


if __name__ == "__main__":
    main()
