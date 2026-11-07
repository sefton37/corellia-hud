"""Mutation check for the Games section: seven hand-written mutants of games.py / classify.py; the games suite
(tests.test_games) must kill every one.

Each mutant is an exact-text replacement asserted to apply exactly once, so a refactor that moves an anchor
fails loudly instead of silently killing nothing. The unmutated tree runs first as a control: if the baseline
fails, "killed" would mean nothing. Suites run in parallel (each owns its own ephemeral ports and temp dirs).
"""
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (name, file, old, new)
MUTANTS = [
    ("drop the 120 s staleness check", "games.py",
     "if polled is None or age > STALE_SECONDS or age < -STALE_SECONDS:", "if polled is None:"),
    ("player_count None -> 0", "games.py",
     "if count is None or isinstance(count, bool) or not isinstance(count, int) or count < 0:",
     "if count is None:\n        count = 0\n    if isinstance(count, bool) or not isinstance(count, int) or count < 0:"),
    ("poll stamp parsed as UTC", "games.py",
     "return datetime.fromisoformat(text).timestamp()",
     "return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()"),
    ("Flint-offline overlay red instead of amber", "classify.py",
     "return AMBER, _GAME_PROBE_AMBER[state]", "return RED, _GAME_PROBE_AMBER[state]"),
    ("Flint online overrides a red unit", "classify.py",
     "    if status != GREEN:\n        return status, reason\n    state = (probe or {}).get(\"state\")",
     "    if (probe or {}).get(\"state\") == \"ok\":\n        return GREEN, reason\n"
     "    if status != GREEN:\n        return status, reason\n    state = (probe or {}).get(\"state\")"),
    ("CPU first sample returns 0", "games.py", "return None  # FIRST_SAMPLE", "return 0.0"),
    ("Flint redirects allowed", "games.py", "return None  # REFUSE_REDIRECT",
     "return super().redirect_request(req, fp, code, msg, headers, newurl)"),
]


def make_tree(base, patched=None):
    shutil.copytree(os.path.join(ROOT, "corellia_hud"), os.path.join(base, "corellia_hud"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(base, "tests"),
                    ignore=shutil.ignore_patterns("__pycache__", "e2e"))
    for fname, text in (patched or {}).items():
        with open(os.path.join(base, "corellia_hud", fname), "w") as f:
            f.write(text)


def run_suite(workdir):
    p = subprocess.run([sys.executable, "-m", "unittest", "tests.test_games"], cwd=workdir,
                       capture_output=True, text=True, timeout=300)
    return p.returncode


def main():
    originals = {}
    for fname in ("games.py", "classify.py"):
        with open(os.path.join(ROOT, "corellia_hud", fname)) as f:
            originals[fname] = f.read()
    for name, fname, old, _ in MUTANTS:
        n = originals[fname].count(old)
        if n != 1:
            print(f"mutant {name!r}: anchor found {n}x in {fname} (need exactly 1)")
            return 2
    with tempfile.TemporaryDirectory(prefix="hud-games-mutants-") as tmp:
        dirs = {"baseline": os.path.join(tmp, "baseline")}
        make_tree(dirs["baseline"])
        for i, (name, fname, old, new) in enumerate(MUTANTS):
            dirs[i] = os.path.join(tmp, f"m{i}")
            make_tree(dirs[i], {fname: originals[fname].replace(old, new)})
        with ThreadPoolExecutor(max_workers=len(dirs)) as pool:
            futs = {k: pool.submit(run_suite, d) for k, d in dirs.items()}
            codes = {k: f.result() for k, f in futs.items()}
    if codes["baseline"] != 0:
        print("BASELINE_FAILED: the unmutated suite does not pass; mutation result is meaningless")
        return 2
    print("baseline: suite passes on the unmutated tree")
    killed = 0
    for i, (name, _, _, _) in enumerate(MUTANTS):
        print(f"mutant {i + 1} {name}: {'KILLED' if codes[i] != 0 else 'SURVIVED'}")
        killed += codes[i] != 0
    print(f"MUTANTS_KILLED {killed}/{len(MUTANTS)}")
    return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
