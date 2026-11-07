"""Mutation check for the Claude usage KPI: eleven hand-written mutants of claude_usage.py / claude_limits.py;
the three Claude suites together must kill every one.

Each mutant is exact-text replacements asserted to apply exactly once, so a refactor that moves an anchor fails
loudly instead of silently killing nothing. The unmutated tree runs first as a control: if the baseline fails,
"killed" would mean nothing. Trees run in parallel (each suite owns its own ephemeral ports and temp dirs).
"""
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITES = ["tests.test_claude_usage", "tests.test_claude_limits", "tests.test_claude_state"]

OUTPUT = {}   # workdir -> tail of the suites' output, shown when the baseline fails

# (name, file, [(old, new), ...])
MUTANTS = [
    ("dedupe takes the last line instead of the maximum output", "claude_usage.py",
     [("        old = rec.turns.get(key)\n        if old is None or turn[OUT] > old[OUT]:\n            rec.turns[key] = turn",
       "        rec.turns[key] = turn")]),
    ("no dedupe at all", "claude_usage.py",
     [("            key = (mid, rid)\n", "            key = (mid, rid, id(raw))\n")]),
    ("a failed fetch is shown as 0 percent", "claude_limits.py",
     [("        return self._snap(last, None, None, None, retry)\n",
       "        return self._snap(\"ok\", None, 0, {\"five_hour\": {\"state\": \"ok\", \"pct\": 0.0, \"left_pct\": 100.0, "
       "\"resets_at\": None, \"resets_in_s\": None, \"reset\": \"unknown\"}}, retry)\n")]),
    ("an expired token is still sent", "claude_limits.py",
     [("        if expires <= wall() * 1000:", "        if False:")]),
    ("redirects are followed", "claude_limits.py",
     [("        return None   # a 3xx is reported as a state, never followed",
       "        return super().redirect_request(req, fp, code, msg, headers, newurl)")]),
    ("backoff and Retry-After ignored", "claude_limits.py",
     [("            wait = retry if (state == \"rate_limited\" and retry is not None) else "
       "BACKOFF[min(self._fails, len(BACKOFF) - 1)]", "            wait = 0")]),
    ("null utilization shown as 0", "claude_limits.py",
     [("        if util is None:\n            out[key] = {\"state\": \"none\", \"pct\": None, \"resets_at\": None}",
       "        if util is None:\n            out[key] = {\"state\": \"ok\", \"pct\": 0.0, \"resets_at\": None}")]),
    ("a past resets_at is accepted as a live value", "claude_limits.py",
     [("            if resets is not None and resets <= now:", "            if False:")]),
    ("today is cut at UTC midnight", "claude_usage.py",
     [("    d = datetime.fromtimestamp(epoch)\n    return d.replace(hour=0",
       "    d = datetime.fromtimestamp(epoch, tz=timezone.utc)\n    return d.replace(hour=0")]),
    ("cache reads counted inside new tokens", "claude_usage.py",
     [("\"new\": inp + cw + out, \"processed\"", "\"new\": inp + cw + out + cr, \"processed\"")]),
    ("the access token is copied into the snapshot", "claude_limits.py",
     [("        token = None\n        if kind == \"http\":", "        self._note = token\n        token = None\n        if kind == \"http\":"),
      ("                return self._snap(\"ok\", None, age, _windows_at(good[\"raw\"], wall))",
       "                return dict(self._snap(\"ok\", None, age, _windows_at(good[\"raw\"], wall)), note=self._note)")]),
]


def make_tree(base, patched=None):
    for sub, ignore in (("corellia_hud", ("__pycache__",)), ("tests", ("__pycache__", "e2e")), ("static", ())):
        shutil.copytree(os.path.join(ROOT, sub), os.path.join(base, sub), ignore=shutil.ignore_patterns(*ignore))
    for fname, text in (patched or {}).items():
        with open(os.path.join(base, "corellia_hud", fname), "w") as f:
            f.write(text)


def run_suites(workdir):
    p = subprocess.run([sys.executable, "-m", "unittest"] + SUITES, cwd=workdir, capture_output=True, text=True,
                       timeout=600)
    OUTPUT[workdir] = (p.stdout + p.stderr)[-3000:]
    return p.returncode


def main():
    originals = {}
    for fname in ("claude_usage.py", "claude_limits.py"):
        with open(os.path.join(ROOT, "corellia_hud", fname)) as f:
            originals[fname] = f.read()
    for name, fname, pairs in MUTANTS:
        for old, _ in pairs:
            n = originals[fname].count(old)
            if n != 1:
                print(f"mutant {name!r}: anchor found {n}x in {fname} (need exactly 1)")
                return 2
    with tempfile.TemporaryDirectory(prefix="hud-claude-mutants-") as tmp:
        dirs = {"baseline": os.path.join(tmp, "baseline")}
        make_tree(dirs["baseline"])
        for i, (name, fname, pairs) in enumerate(MUTANTS):
            text = originals[fname]
            for old, new in pairs:
                text = text.replace(old, new)
            dirs[i] = os.path.join(tmp, f"m{i}")
            make_tree(dirs[i], {fname: text})
        with ThreadPoolExecutor(max_workers=len(dirs)) as pool:
            futs = {k: pool.submit(run_suites, d) for k, d in dirs.items()}
            codes = {k: f.result() for k, f in futs.items()}
    if codes["baseline"] != 0:
        print("BASELINE_FAILED: the unmutated suites do not pass; mutation result is meaningless")
        print(OUTPUT.get(dirs["baseline"], ""))
        return 2
    print("baseline: the suites pass on the unmutated tree")
    killed = 0
    for i, (name, _, _) in enumerate(MUTANTS):
        print(f"mutant {i + 1} {name}: {'KILLED' if codes[i] != 0 else 'SURVIVED'}")
        killed += codes[i] != 0
    print(f"MUTANTS_KILLED {killed}/{len(MUTANTS)}")
    return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
