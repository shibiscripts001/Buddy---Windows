"""Merge translations written one per line into a core/translations/*.json file.

    python add_translations.py <name> <lines.txt> [--comment "Batch Clip Renamer"]

Each line of lines.txt is one string, its 8 languages in LANGUAGES order,
separated by " || ":

    English || 日本語 || Español || Deutsch || Français || 한국어 || 中文 || العربية || Tiếng Việt

"\\n" in a line is a line break. Blank lines and lines starting with "#" are
skipped. Entries already in the file are replaced; the rest are kept. The
file is written atomically, then checked with check_translations.py.
"""
import argparse
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
REPO = os.environ.get("BUDDY_REPO", os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
LANGS = ["日本語", "Español", "Deutsch", "Français", "한국어", "中文", "العربية", "Tiếng Việt"]

ap = argparse.ArgumentParser()
ap.add_argument("name")
ap.add_argument("lines")
ap.add_argument("--comment")
args = ap.parse_args()

target = os.path.join(REPO, "app", "core", "translations", args.name + ".json")
data = {}
if os.path.exists(target):
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
if args.comment:
    data["_comment"] = f"{args.comment}. English -> each language; see core/i18n.py."
    data = {"_comment": data.pop("_comment"), **data}

bad = 0
added = 0
with open(args.lines, encoding="utf-8") as fh:
    for n, line in enumerate(fh, 1):
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip().replace("\\n", "\n") for p in line.split(" || ")]
        if len(parts) != 1 + len(LANGS) or not all(parts):
            print(f"line {n}: {len(parts)} fields: {line[:80]}")
            bad += 1
            continue
        data[parts[0]] = dict(zip(LANGS, parts[1:]))
        added += 1

if bad:
    sys.exit(f"{bad} bad line(s) - nothing written")
os.makedirs(os.path.dirname(target), exist_ok=True)
with open(target + ".tmp", "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=1)
    fh.write("\n")
os.replace(target + ".tmp", target)
print(f"{args.name}.json: {added} merged, {sum(1 for k in data if not k.startswith('_'))} total")
subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_translations.py"), target])
