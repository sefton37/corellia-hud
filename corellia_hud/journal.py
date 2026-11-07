"""The only door to the system journal: the tail of one registry unit, as the journal wrote it.

Hardening, all of it deliberate:
  * argv is a fixed template; only a validated unit name is interpolated, as `--unit=<name>`;
  * shell is never involved, stdin is closed, there is a hard timeout, and nothing is written anywhere;
  * the unit name comes from the registry (never from a request) and must match UNIT_RE;
  * output is cut to the last 20 lines and each line is capped before it is returned.
Every failure is returned as an explicit state; an empty journal is never reported as "ok".

A second fixed template reads the Bedrock server's player and start-up lines (`bedrock_window`): the unit is a
module constant, the only variable parts are the binary and the runner, and the grep pattern is constant.
"""
import os
import re
import subprocess

UNIT_RE = re.compile(r"^[A-Za-z0-9@._][A-Za-z0-9@._-]{0,126}\.(service|timer)\Z")
MAX_LINES = 20
MAX_LINE_CHARS = 400
TIMEOUT_SECONDS = 3
HOME = os.path.expanduser("~")
SYSTEM_UNIT_DIR = "/etc/systemd/system/"
USER_UNIT_DIRS = ("~/.config/systemd/user/", HOME + "/.config/systemd/user/")
_PERMISSION_HINTS = ("insufficient permissions", "permission denied", "not allowed to access")
# Most informative first when several scopes were tried.
_PRIORITY = ("ok", "timeout", "no_permission", "unavailable", "no_lines")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value if isinstance(value, str) else str(value)


def journal_unit(unit):
    """The unit whose journal to read: a timer's journal is its service's (F9)."""
    if not unit:
        return None
    return unit[:-len(".timer")] + ".service" if unit.endswith(".timer") else unit


def scopes_for(svc):
    """Journal scopes to try, in order (F10): registry kind first, then the unit file location, else both."""
    kind = svc.get("kind")
    if kind == "systemd":
        return ["system"]
    if kind == "systemd-user":
        return ["user"]
    path = svc.get("unit_path") or ""
    if path.startswith(SYSTEM_UNIT_DIR):
        return ["system"]
    if path.startswith(USER_UNIT_DIRS):
        return ["user"]
    return ["system", "user"]


def _argv(binary, unit, scope):
    argv = [binary]
    if scope == "user":
        argv.append("--user")
    return argv + ["--no-pager", "--output=short-iso", "--lines=%d" % MAX_LINES, "--unit=" + unit]


def _run_once(argv, runner):
    """(state, raw lines) for one journalctl invocation."""
    try:
        done = runner(argv, shell=False, timeout=TIMEOUT_SECONDS, capture_output=True,
                      stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return "timeout", []
    except Exception:  # missing binary, exec denied, anything else: an explicit state, never a raise
        return "unavailable", []
    err = _text(done.stderr).lower()
    out = _text(done.stdout)
    if any(h in err for h in _PERMISSION_HINTS):   # stderr only: a log line may legitimately say it
        return "no_permission", []
    # journalctl prints "-- ..." marker lines and "Hint:" lines that are not log entries.
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith(("-- ", "Hint:"))]
    if done.returncode != 0 and not lines:
        return "unavailable", []
    return ("ok", lines) if lines else ("no_lines", [])


def tail(unit, scopes, runner=subprocess.run, binary="journalctl"):
    """{state, lines[<=20], scope} for `unit` (a service or a timer name straight from the registry)."""
    if not isinstance(unit, str) or not UNIT_RE.match(unit):
        return {"state": "unit_invalid", "lines": [], "scope": None}
    seen = []
    for scope in scopes:
        state, lines = _run_once(_argv(binary, unit, scope), runner)
        if state == "ok":
            kept = lines[-MAX_LINES:]
            return {"state": "ok", "lines": [line[:MAX_LINE_CHARS] for line in kept], "scope": scope}
        seen.append(state)
    best = min(seen, key=_PRIORITY.index) if seen else "no_lines"
    return {"state": best, "lines": [], "scope": None}


BEDROCK_UNIT = "bedrock.service"
BEDROCK_WINDOW_LINES = 500
BEDROCK_GREP = "Starting Server|Version: |Level Name: |Pack Stack|Player connected|Player disconnected"


def bedrock_window(runner=subprocess.run, binary="journalctl"):
    """{state, lines[<=500]}: the Bedrock unit's start-up and player lines, oldest first, as the journal wrote them."""
    argv = [binary, "--no-pager", "--output=short-iso", "--lines=%d" % BEDROCK_WINDOW_LINES,
            "--unit=" + BEDROCK_UNIT, "--grep=" + BEDROCK_GREP]
    state, lines = _run_once(argv, runner)
    return {"state": state, "lines": [line[:MAX_LINE_CHARS] for line in lines[-BEDROCK_WINDOW_LINES:]]}
