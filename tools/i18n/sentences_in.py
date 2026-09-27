"""Print the sentence-like string literals in Python files (implicit
concatenation joined, f-string parts as {name}), skipping docstrings and
anything passed to print/log/crash_log - a starting list of what a module
may show on screen.

    python sentences_in.py <file.py> [<file.py> ...]
"""
import ast
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
SKIP_CALLS = {"print", "log", "_log", "trail", "debug", "info", "warning", "error", "exception", "getLogger",
              "startswith", "endswith", "get", "setdefault", "join", "split", "replace", "strftime", "format_exc"}


def text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(part.value)
            else:
                src = ast.unparse(part.value)
                name = re.sub(r"\W+", "_", src.split("(")[0].split("[")[0].split(".")[-1]).strip("_") or "x"
                out.append("{" + name + "}")
        return "".join(out)
    return None


for path in sys.argv[1:]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in SKIP_CALLS:
                for sub in ast.walk(node):
                    skip.add(id(sub))
        if isinstance(node, ast.JoinedStr):
            for sub in ast.walk(node):
                if sub is not node:
                    skip.add(id(sub))
    seen = set()
    print(f"== {path}")
    for node in ast.walk(tree):
        if id(node) in skip or not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        t = text(node)
        if not t or t in seen:
            continue
        plain = re.sub(r"\{\w+\}", "", t)
        if len(re.findall(r"[A-Za-z]{2,}", plain)) < 2 or " " not in plain.strip():
            continue
        if re.search(r"^[\w.-]+://|^[a-z_]+(\.[a-z_]+)+$|<[a-z]+[ >]|SELECT |\{\s*\"", t):
            continue
        seen.add(t)
        print(f"{node.lineno}: {t!r}")
