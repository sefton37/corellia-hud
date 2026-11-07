"""Every import in corellia_hud/ is stdlib or repo-local; tests/ and launcher/ (including the pure
launcher/detail_policy.py) import cleanly; the .cjs e2e require() targets resolve from helm's node_modules."""
import ast
import importlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELM = os.environ.get("HELM", os.path.expanduser("~/dev/helm"))
sys.path.insert(0, ROOT)
LOCAL = {"corellia_hud", "tests"}
unresolved = []


def top_imports(path):
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


for dirpath, _, files in os.walk(os.path.join(ROOT, "corellia_hud")):
    for f in files:
        if f.endswith(".py"):
            for mod in set(top_imports(os.path.join(dirpath, f))):
                if mod not in sys.stdlib_module_names and mod not in LOCAL:
                    unresolved.append(f"corellia_hud/{f}: non-stdlib import {mod}")

for sub in ("tests", "tools"):
    for dirpath, _, files in os.walk(os.path.join(ROOT, sub)):
        if "e2e" in dirpath:
            continue
        for f in files:
            if f.endswith(".py"):
                for mod in set(top_imports(os.path.join(dirpath, f))):
                    if mod not in sys.stdlib_module_names and mod not in LOCAL and mod != "gi":
                        unresolved.append(f"{sub}/{f}: unresolved {mod}")

for dirpath, _, files in os.walk(os.path.join(ROOT, "tests")):
    for f in files:
        if f.endswith(".py") and "e2e" not in dirpath:
            rel = os.path.relpath(os.path.join(dirpath, f[:-3]), ROOT).replace(os.sep, ".")
            rel = rel[:-len(".__init__")] if rel.endswith(".__init__") else rel
            try:
                importlib.import_module(rel)
            except Exception as e:
                unresolved.append(f"{rel}: {type(e).__name__}: {e}")

# launcher needs gi; import by executing only its import block (no window is created)
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, WebKit2  # noqa: F401
except Exception as e:
    unresolved.append(f"launcher gi imports: {e}")
LAUNCHER_LOCAL = {"detail_policy"}   # sits next to hud_window.py and must itself be stdlib-only (no gi)
for mod in set(top_imports(os.path.join(ROOT, "launcher", "hud_window.py"))):
    if mod not in sys.stdlib_module_names and mod != "gi" and mod not in LAUNCHER_LOCAL:
        unresolved.append(f"launcher: unexpected import {mod}")
for mod in set(top_imports(os.path.join(ROOT, "launcher", "detail_policy.py"))):
    if mod not in sys.stdlib_module_names:
        unresolved.append(f"launcher/detail_policy.py: non-stdlib import {mod}")

cjs = "".join(open(os.path.join(ROOT, "tests", "e2e", n)).read() for n in ("hud.e2e.cjs", "detail.e2e.cjs", "games.e2e.cjs", "claude.e2e.cjs"))
BUILTIN = {"child_process", "fs", "http", "net", "os", "path"}
for target in set(re.findall(r"require\('([^']+)'\)", cjs)):
    if target in BUILTIN:
        continue
    if not os.path.exists(os.path.join(HELM, "node_modules", target, "package.json")):
        unresolved.append(f"e2e require {target} not in helm node_modules")

for u in unresolved:
    print("UNRESOLVED", u)
if unresolved:
    sys.exit(1)
print("IMPORTS_OK stdlib_only=yes unresolved=0")
