"""Mutation check for classify.py: five hand-written mutants, the classify suite must kill all five.

Each mutant is an exact-text replacement; the replacement is asserted to apply exactly once, so a
refactor that moves the anchor fails loudly instead of silently killing nothing. The unmutated
tree is run first as a control: if the baseline fails, "killed" would mean nothing.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MUTANTS = [
    ("missing -> green",
     'status = GREY\n        reason = "no probe"', 'status = GREEN\n        reason = "no probe"'),
    ("stale -> green",
     "if stale and status == GREEN:", "if False and status == GREEN:"),
    ("down -> amber",
     'else (RED, "port closed")', 'else (AMBER, "port closed")'),
    ("scope ignored",
     "scope = SCOPE_FOR_KIND[kind]", 'scope = "system"'),
    ("restart-loop ignored",
     "loops > RESTART_LOOP_THRESHOLD", "loops > 10 ** 9"),
]


def run_suite(workdir):
    p = subprocess.run([sys.executable, "-m", "unittest", "tests.test_classify"], cwd=workdir,
                       capture_output=True, text=True, timeout=120)
    return p.returncode


def make_tree(base, classify_text):
    shutil.copytree(os.path.join(ROOT, "corellia_hud"), os.path.join(base, "corellia_hud"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(base, "tests"),
                    ignore=shutil.ignore_patterns("__pycache__", "e2e"))
    with open(os.path.join(base, "corellia_hud", "classify.py"), "w") as f:
        f.write(classify_text)


def main():
    with open(os.path.join(ROOT, "corellia_hud", "classify.py")) as f:
        original = f.read()
    with tempfile.TemporaryDirectory(prefix="hud-mutants-") as tmp:
        base = os.path.join(tmp, "baseline")
        os.makedirs(base)
        make_tree(base, original)
        if run_suite(base) != 0:
            print("BASELINE_FAILED: the unmutated suite does not pass; mutation result is meaningless")
            return 2
        print("baseline: suite passes on the unmutated tree")
        killed = 0
        for i, (name, old, new) in enumerate(MUTANTS):
            if original.count(old) != 1:
                print(f"mutant {i + 1} {name!r}: anchor found {original.count(old)}x (need exactly 1)")
                return 2
            d = os.path.join(tmp, f"m{i}")
            os.makedirs(d)
            make_tree(d, original.replace(old, new))
            rc = run_suite(d)
            print(f"mutant {i + 1} {name}: {'KILLED' if rc != 0 else 'SURVIVED'}")
            killed += rc != 0
    print(f"MUTANTS_KILLED {killed}/{len(MUTANTS)}")
    return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
