"""List the strings a part of Buddy passes to tr() (Python) or T() /
Buddy.t() (page scripts) that have no Japanese translation yet.

    python missing_keys.py <folder under app/, e.g. pages/color_palette>
"""
import glob
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
REPO = os.environ.get("BUDDY_REPO", os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
sys.path.insert(0, os.path.join(REPO, "app"))
from core.i18n import translate  # noqa: E402

PY_CALL = re.compile(r"""\btr\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")
JS_CALL = re.compile(r"""\b(?:T|Buddy\.t)\(\s*"((?:[^"\\]|\\.)*)\"""")

root = os.path.join(REPO, "app", sys.argv[1])
keys = set()
for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
    for m in PY_CALL.finditer(open(path, encoding="utf-8").read()):
        keys.add(eval(m.group(1)))
for path in glob.glob(os.path.join(root, "**", "*.js"), recursive=True):
    for m in JS_CALL.finditer(open(path, encoding="utf-8").read()):
        keys.add(m.group(1).encode().decode("unicode_escape") if "\\" in m.group(1) else m.group(1))
missing = sorted(k for k in keys if translate(k, "日本語") == k)
print(f"{len(keys)} keys, {len(missing)} without a translation")
for k in missing:
    print(repr(k))
