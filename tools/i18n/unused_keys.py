"""List keys in a translations file whose text can't be found in a tool's
source - usually a key written from memory that doesn't match the code.

    python unused_keys.py <translations name> <folder under app/> [<folder> ...]

Rough: string literals are joined across implicit concatenation and each
literal run between {placeholders} is looked for on its own, so a key can
pass while still not matching exactly. Check what it flags by hand.
"""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "..", "app")

name, folders = sys.argv[1], sys.argv[2:]
source = []
for folder in folders:
    for root, _, files in os.walk(os.path.join(APP, folder)):
        for f in files:
            if f.endswith((".py", ".js", ".html")):
                source.append(open(os.path.join(root, f), encoding="utf-8").read())
text = "\n".join(source)
# Join implicit concatenation ("abc "\n   f"def") and unescape quotes/newlines.
text = re.sub(r"""["']\s*\n\s*[fr]?["']""", "", text)
text = re.sub(r"""["']\s*\+\s*\n?\s*[fr]?["']""", "", text)
text = text.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n")
flat = re.sub(r"\s+", " ", text)

keys = json.load(open(os.path.join(APP, "core", "translations", name + ".json"), encoding="utf-8"))
for key in keys:
    if key.startswith("_"):
        continue
    parts = [p.strip() for p in re.split(r"\{\w+\}", re.sub(r"\s+", " ", key))]
    missing = [p for p in parts if len(p) > 3 and p not in flat and p.rstrip(".…:") not in flat]
    if missing:
        print(f"{key!r}\n    not found: {missing}")
