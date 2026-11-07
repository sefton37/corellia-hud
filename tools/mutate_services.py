"""Mutation check for the compact Services panel (Spec #492): nine hand-written mutants of classify.py /
detail.py; the test-services-compact suite must kill every one.

Each mutant is an exact-text replacement asserted to apply exactly once, so a refactor that moves an anchor
fails loudly instead of silently killing nothing. The unmutated tree runs first as a control.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLASSIFY, DETAIL = "classify.py", "detail.py"

MUTANTS = [
    ("stale -> all_up", CLASSIFY,
     'state, level = "stale", AMBER', 'state, level = "all_up", GREEN'),
    ("unavailable -> all_up", CLASSIFY,
     'state, level = "unavailable", GREY', 'state, level = "all_up", GREEN'),
    ("grey excluded from not_up", CLASSIFY,
     "not_up = total - up", "not_up = sum(1 for r in rows if r.get(\"status\") in (RED, AMBER))"),
    ("cap lists cap + 1", CLASSIFY,
     'listed = [r["name"] for r in bad[:cap]]', 'listed = [r["name"] for r in bad[:cap + 1]]'),
    ("not worst-first (grey ahead of red)", CLASSIFY,
     "_NOT_UP_ORDER = {RED: 0, AMBER: 1, GREY: 2}", "_NOT_UP_ORDER = {RED: 2, AMBER: 1, GREY: 0}"),
    ("more off by one", CLASSIFY,
     "not_up - len(listed) if", "not_up - len(listed) + 1 if"),
    ("empty registry -> all_up", CLASSIFY,
     'state, level = "empty", GREY', 'state, level = "all_up", GREEN'),
    ("all id returns only green rows", DETAIL,
     "if every or r[\"status\"] == status]", "if (every and r[\"status\"] == classify.GREEN) or r[\"status\"] == status]"),
    ("up computed as total", CLASSIFY,
     'up = sum(1 for r in rows if r.get("status") == GREEN)', "up = len(rows)"),
]


def run_suite(workdir):
    p = subprocess.run([sys.executable, "-m", "unittest", "tests.test_services_compact"], cwd=workdir,
                       capture_output=True, text=True, timeout=300)
    return p.returncode


def make_tree(base, texts):
    shutil.copytree(os.path.join(ROOT, "corellia_hud"), os.path.join(base, "corellia_hud"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(base, "tests"),
                    ignore=shutil.ignore_patterns("__pycache__", "e2e", "out"))
    for name, text in texts.items():
        with open(os.path.join(base, "corellia_hud", name), "w") as f:
            f.write(text)


def main():
    original = {}
    for name in (CLASSIFY, DETAIL):
        with open(os.path.join(ROOT, "corellia_hud", name)) as f:
            original[name] = f.read()
    with tempfile.TemporaryDirectory(prefix="hud-svc-mutants-") as tmp:
        base = os.path.join(tmp, "baseline")
        os.makedirs(base)
        make_tree(base, original)
        if run_suite(base) != 0:
            print("BASELINE_FAILED: the unmutated suite does not pass; mutation result is meaningless")
            return 2
        print("baseline: suite passes on the unmutated tree")
        killed = 0
        for i, (label, name, old, new) in enumerate(MUTANTS):
            if original[name].count(old) != 1:
                print(f"mutant {i + 1} {label!r}: anchor found {original[name].count(old)}x in {name} (need exactly 1)")
                return 2
            d = os.path.join(tmp, f"m{i}")
            os.makedirs(d)
            make_tree(d, dict(original, **{name: original[name].replace(old, new)}))
            rc = run_suite(d)
            print(f"mutant {i + 1} {label}: {'KILLED' if rc != 0 else 'SURVIVED'}")
            killed += rc != 0
    print(f"MUTANTS_KILLED {killed}/{len(MUTANTS)}")
    return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
