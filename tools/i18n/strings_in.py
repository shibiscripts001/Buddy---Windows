"""Print the string arguments of chosen calls in a Python file, joined as
they'd read (implicit concatenation resolved, f-string parts as {name}) -
a quick way to collect messages a module raises or logs.

    python strings_in.py <file.py> <CallName> [<CallName> ...]

e.g. python strings_in.py app/pages/project_setup/otio_engine.py OtioError progress log
"""
import ast
import sys

sys.stdout.reconfigure(encoding="utf-8")
path, names = sys.argv[1], set(sys.argv[2:])
tree = ast.parse(open(path, encoding="utf-8").read())


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
                out.append("{" + (src.split(".")[-1].split("(")[0].split("[")[0] or "x") + "}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = text(node.left), text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


seen = set()
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name not in names:
        continue
    for arg in node.args[:1]:
        t = text(arg)
        if t and t not in seen:
            seen.add(t)
            print(f"{node.lineno}: {t!r}")
