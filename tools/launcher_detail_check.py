"""Static AST check of the detail-window half of the launcher; opens no window.

Proves from the source (not from a run): DetailWindow exists with its own class and title, is frameless,
takes focus, is never kept below or made sticky, and the HUD's NEW_WINDOW_ACTION policy is routed through
detail_policy.allowed().
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "launcher", "hud_window.py")).read()
tree = ast.parse(src)
problems = []


def consts():
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node.value.value
    return out


def calls(node):
    """(method name, [constant args]) for every attribute call under `node`."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            yield n.func.attr, [a.value for a in n.args if isinstance(a, ast.Constant)]


c = consts()
detail = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DetailWindow"), None)
if detail is None:
    problems.append("class DetailWindow missing")
else:
    made = list(calls(detail))
    if ("set_decorated", [False]) not in made:
        problems.append("DetailWindow is not frameless (set_decorated(False) missing)")
    if ("set_accept_focus", [True]) not in made:
        problems.append("DetailWindow does not accept focus (Esc could not reach it)")
    if ("set_accept_focus", [False]) in made:
        problems.append("DetailWindow disables focus")
    for banned in ("set_keep_below", "stick", "set_keep_above"):
        if any(name == banned for name, _ in made):
            problems.append(f"DetailWindow calls {banned}")
    if not any(name == "set_wmclass" for name, _ in made):
        problems.append("DetailWindow does not set its own WM class")
if not c.get("WM_CLASS_DETAIL") or c.get("WM_CLASS_DETAIL") == c.get("WM_CLASS"):
    problems.append("detail WM class is not distinct from the column's")
if not c.get("TITLE_DETAIL") or c.get("TITLE_DETAIL") == c.get("TITLE"):
    problems.append("detail title is not distinct from the column's")

routed = False
for fn in ast.walk(tree):
    if isinstance(fn, ast.FunctionDef):
        names = {a.attr for a in ast.walk(fn) if isinstance(a, ast.Attribute)}
        used = {(a.func.value.id, a.func.attr) for a in ast.walk(fn)
                if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and isinstance(a.func.value, ast.Name)}
        if "NEW_WINDOW_ACTION" in names and ("detail_policy", "allowed") in used:
            routed = True
if not routed:
    problems.append("NEW_WINDOW_ACTION is not handled through detail_policy.allowed")
if not any(isinstance(n, ast.Import) and any(a.name == "detail_policy" for a in n.names) for n in tree.body):
    problems.append("detail_policy is not imported")

for p in problems:
    print("PROBLEM", p)
if problems:
    print("LAUNCHER_DETAIL_FAIL")
    sys.exit(1)
print(f"LAUNCHER_DETAIL_OK class={c['WM_CLASS_DETAIL']} title={c['TITLE_DETAIL']!r} decorated=false focus=yes "
      "keep_below=no stick=no new_window_via=detail_policy")
