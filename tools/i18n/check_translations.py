"""Check one core/translations/*.json file: python check_translations.py <file>"""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))
from core.i18n import LANGUAGES, TRANSLATIONS  # noqa: E402

OTHERS = LANGUAGES[1:]
PH = re.compile(r"\{(\w+)\}")
BRACE = re.compile(r"[{}]")
HAN = re.compile(r"[\u4e00-\u9fff]")
HANGUL = re.compile(r"[\uac00-\ud7af]")
KANA = re.compile(r"[\u3040-\u30ff]")
ARABIC = re.compile(r"[\u0600-\u06ff]")
CJK_ANY = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)

problems = []
count = 0
for key, entry in data.items():
    if key.startswith("_"):
        continue
    count += 1
    if not isinstance(entry, dict):
        problems.append(f"{key!r}: not an object")
        continue
    if key != " ".join(key.split()) and "\n" not in key:
        problems.append(f"{key!r}: extra whitespace in key (keys are matched with whitespace collapsed)")
    missing = [l for l in OTHERS if not entry.get(l)]
    extra = [l for l in entry if l not in OTHERS]
    if missing:
        problems.append(f"{key!r}: missing {missing}")
    if extra:
        problems.append(f"{key!r}: unknown language keys {extra}")
    names = sorted(PH.findall(key))
    if len(set(names)) != len(names):
        problems.append(f"{key!r}: a placeholder name is used twice")
    stray = BRACE.sub("", PH.sub("", key)) != PH.sub("", key)
    if stray:
        problems.append(f"{key!r}: braces that aren't a {{name}} placeholder")
    if names and len(re.findall(r"[^\W\d_]", PH.sub("", key))) < 2:
        problems.append(f"{key!r}: fewer than 2 letters outside placeholders - never matched")
    for lang, text in entry.items():
        if not isinstance(text, str):
            problems.append(f"{key!r} [{lang}]: not a string")
            continue
        if sorted(PH.findall(text)) != names:
            problems.append(f"{key!r} [{lang}]: placeholders {sorted(PH.findall(text))} != {names}")
        if key.count("\n") != text.count("\n"):
            problems.append(f"{key!r} [{lang}]: {key.count(chr(10))} line breaks in English, {text.count(chr(10))} here")
        if lang == "한국인" and (HAN.search(text) or KANA.search(text)):
            problems.append(f"{key!r} [{lang}]: Han/Kana in Korean: {text}")
        if lang == "中文" and (HANGUL.search(text) or KANA.search(text)):
            problems.append(f"{key!r} [{lang}]: Hangul/Kana in Chinese: {text}")
        if lang == "日本語" and HANGUL.search(text):
            problems.append(f"{key!r} [{lang}]: Hangul in Japanese: {text}")
        if lang in ("Español", "Deutsch", "Français", "Tiếng Việt") and (CJK_ANY.search(text) or ARABIC.search(text)):
            problems.append(f"{key!r} [{lang}]: wrong script: {text}")
        if lang in ("日本語", "中文", "한국인", "العربية") and text == key and re.search(r"[a-z]{3}", key):
            problems.append(f"{key!r} [{lang}]: left in English")

print(f"{count} keys")
for p in problems:
    print("  " + p)
print("OK" if not problems else f"{len(problems)} problems")
