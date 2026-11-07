"""Claude usage KPI against the REAL sources.

Default mode starts the real aggregator on a spare loopback port and prints LIVE_CLAUDE_OK only if ALL hold:
  * the credentials file has the expected shape (key NAMES and types are printed, never values): cred_shape=ok;
  * an independent re-derivation here (its own glob, its own dedupe) gives the same turn count and token sums for
    a fully past local day as the aggregator, and today's figure lies between this script's before and after reads;
  * limits.state is a documented state; when `ok`: the percentages are finite and within 0-1000, resets_at is in
    the future, five_hour resets within about 5 h and seven_day within about 7 d, and the tile equals the detail;
  * five state loads cause at most one outbound fetch (the detail window's diag.fetch_count);
  * /api/state and /api/detail bytes hold no substring of the real access or refresh token (read here only to prove
    absence, never printed); the aggregator's own output holds neither;
  * the credentials file's size and mtime are unchanged by the run; no idle transcript file changed and the
    aggregator process wrote nothing to disk; it stays under 10% of one core over 40 s.
A limits.state of token_expired / needs_reauth FAILS with the instruction to run Claude Code once and rerun.
`--unit`: the INSTALLED sandboxed unit on :8670 serves the claude key, and its limits state proves the sandbox both
read the credentials file and reached the host; the tailnet address still refuses. Read-only throughout.
"""
import glob
import json
import math
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from corellia_hud import claude_limits, state  # noqa: E402

CFG = state.Config()
PROBLEMS = []
CLK = os.sysconf("SC_CLK_TCK")


def need(cond, msg):
    if not cond:
        PROBLEMS.append(msg)
    return cond


def get(port, path, timeout=30):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- credentials: shape and secrets (never printed)

def cred_stamp():
    try:
        st = os.stat(CFG.claude_cred_path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def read_secrets():
    """(shape ok, [access, refresh]) from the real file; prints key NAMES and types only."""
    try:
        with open(CFG.claude_cred_path, "rb") as f:
            obj = json.loads(f.read(65536))
    except (OSError, ValueError) as e:
        print("cred_shape=mismatch (unreadable: " + type(e).__name__ + ")")
        return False, []

    def desc(o):
        if isinstance(o, dict):
            return {k: desc(v) for k, v in o.items()}
        if isinstance(o, list):
            return "list[%d]<%s>" % (len(o), ",".join(sorted({type(x).__name__ for x in o})))
        return type(o).__name__
    shape = desc(obj)
    print("cred_keys=" + json.dumps(shape, sort_keys=True))
    oauth = obj.get("claudeAiOauth") if isinstance(obj, dict) else None
    ok = (isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str) and isinstance(oauth.get("expiresAt"), int)
          and isinstance(oauth.get("scopes"), list))
    if ok:
        print("cred_scopes=" + ",".join(oauth["scopes"]))
        print("cred_expires_in_s=%d" % (oauth["expiresAt"] / 1000 - time.time()))
    print("cred_shape=" + ("ok" if ok else "mismatch"))
    secrets = [v for v in ((oauth or {}).get("accessToken"), (oauth or {}).get("refreshToken")) if isinstance(v, str) and len(v) >= 16]
    return ok, secrets


def leaks(blob, secret):
    """True when any 16-character window of the secret appears in blob."""
    return any(secret[i:i + 16] in blob for i in range(0, len(secret) - 15))


# ---------------------------------------------------------------- the independent re-derivation of the transcripts

def independent():
    """{local date: [turns, new, cache_read]} from this script's own glob and dedupe (largest output wins)."""
    best = {}
    for path in glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True):
        if os.path.islink(path):
            continue
        try:
            with open(path, "rb") as f:
                for raw in f:
                    if b'"usage"' not in raw or not raw.endswith(b"\n"):
                        continue
                    try:
                        o = json.loads(raw)
                        m, u = o["message"], o["message"]["usage"]
                        vals = [u[k] for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
                        if m.get("model") == "<synthetic>" or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in vals):
                            continue
                        ts = datetime.fromisoformat(o["timestamp"]).timestamp()
                        key = (m["id"], o["requestId"]) if m.get("id") and o.get("requestId") else ("uuid", o["uuid"])
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    if key not in best or vals[1] > best[key][1]:
                        best[key] = (ts, vals[1], vals)
        except OSError:
            continue
    days = {}
    for ts, _, (inp, out, cw, cr) in best.values():
        d = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        row = days.setdefault(d, [0, 0, 0])
        row[0] += 1
        row[1] += inp + cw + out
        row[2] += cr
    return days


def idle_files():
    """{path: (size, mtime_ns)} of transcript files not touched in the last 120 s (the live session is excluded)."""
    out, now = {}, time.time()
    for path in glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if now - st.st_mtime > 120:
            out[path] = (st.st_size, st.st_mtime_ns)
    return out


def cpu_seconds(pid):
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / CLK


def write_bytes(pid):
    with open(f"/proc/{pid}/io") as f:
        for line in f:
            if line.startswith("write_bytes:"):
                return int(line.split()[1])
    return None


# ---------------------------------------------------------------- default mode

def check_limits(limits, detail, tile):
    st = limits["state"]
    print("limits_state=" + st)
    if not need(st in claude_limits.STATES, f"limits.state {st!r} is not a documented state"):
        return
    if st in ("token_expired", "needs_reauth"):
        need(False, f"limits.state={st}: run Claude Code once (it renews its sign-in), then rerun; the endpoint path is otherwise unproven")
        return
    if st != "ok":
        need(False, f"limits.state={st}: the endpoint path is not proven (reason={limits.get('reason')})")
        return
    now = time.time()
    for key, horizon in (("five_hour", 5 * 3600 + 600), ("seven_day", 7 * 86400 + 600)):
        w = limits["windows"].get(key) or {}
        print(f"{key}: state={w.get('state')} pct={w.get('pct')} left={w.get('left_pct')} resets_in_s={w.get('resets_in_s')} reset={w.get('reset')}")
        if w.get("state") == "ok":
            need(isinstance(w["pct"], (int, float)) and math.isfinite(w["pct"]) and 0 <= w["pct"] <= 1000, f"{key} pct out of range: {w['pct']}")
            need(w.get("resets_at") is not None and w["resets_at"] > now, f"{key} resets_at is not in the future")
            need(w.get("resets_at") is not None and w["resets_at"] - now <= horizon, f"{key} resets later than expected: {w.get('resets_in_s')} s")
        else:
            print(f"  note: {key} window state {w.get('state')} (no value to range-check)")
    for key in ("five_hour", "seven_day"):
        a, b = limits["windows"].get(key), (detail.get("windows") or {}).get(key)
        need(a is not None and b is not None and a["state"] == b["state"] and a["pct"] == b["pct"], f"tile and detail disagree on {key}")


def default_mode():
    shape_ok, secrets = read_secrets()
    need(shape_ok, "cred_shape=mismatch: STOP and amend the spec, the credentials key names differ from the expected shape")
    if not shape_ok:
        return report()
    before_stamp, idle_before = cred_stamp(), idle_files()
    # The reference day must be CLOSED and settled: a response that started before midnight can be written minutes later
    # (observed shape: lines stamped yesterday arriving after 00:00), so right after midnight "yesterday" is still growing.
    # Use the latest local day that ended at least 2 hours ago. The assertions on it are unchanged (exact equality).
    started = datetime.now()
    yesterday = ((started - timedelta(hours=2)).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = started.strftime("%Y-%m-%d")
    print(f"reference_day={yesterday} (latest day that ended >= 2 h ago), today={today}")
    ind_before = independent()
    port = free_port()
    no_limits = "--no-limits" in sys.argv   # repro loops only: no credentials read and no request (never hammer the endpoint)
    env = dict(os.environ, HUD_PORT=str(port), HUD_TTL="1", HUD_PROM_URL="http://127.0.0.1:9", PYTHONUNBUFFERED="1",
               PYTHONDONTWRITEBYTECODE="1",   # a cache write is not the aggregator writing data
               # the two sqlite sources of the OLDER panels write -shm files on read (observed: 240 KB); point them at nothing
               # so write_bytes isolates the Claude path
               HUD_ANALYTICS_DB="/nonexistent/hud-live/a.db", HUD_PRODUCT_DB="/nonexistent/hud-live/p.db")
    if no_limits:
        env["HUD_CLAUDE_LIMITS"] = "0"
    proc = subprocess.Popen([sys.executable, "-m", "corellia_hud.server"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    blobs = []
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        cpu0, t0 = cpu_seconds(proc.pid), time.time()
        first = None
        loads = []
        for i in range(5):   # five state loads (state TTL 1 s: each one rebuilds)
            _, raw = get(port, "/api/state")
            blobs.append(raw)
            loads.append(json.loads(raw))
            if i == 0:
                first = loads[0]
            time.sleep(1.3)
        for _ in range(30):   # let the (non-blocking) limits fetch settle
            snap = json.loads(get(port, "/api/state")[1])
            if snap["claude"]["limits"]["state"] != "pending":
                break
            time.sleep(1)
        tile = snap["claude"]["limits"]
        _, dl = get(port, "/api/detail/claude?id=limits")
        blobs.append(dl)
        detail = json.loads(dl)
        for ident in ("usage", "models", "projects"):
            blobs.append(get(port, "/api/detail/claude?id=" + ident)[1])
        usage = json.loads(blobs[-3])
        ind_after = independent()
        fetches = detail["diag"]["fetch_count"]
        if no_limits:
            need(tile["state"] == "disabled" and fetches == 0, "--no-limits must leave the limits source disabled with 0 fetches")
        else:
            check_limits(tile, detail, tile)
            print("fetch_count_after_loads=%d" % fetches)
            need(fetches <= 1, f"{fetches} outbound fetches for 5+ loads")
        # usage: a fully past day, and today between the before and after reads
        u_day = next((b for b in usage["daily"] if b["date"] == yesterday), None)
        want = ind_before.get(yesterday, [0, 0, 0])
        if need(u_day is not None and u_day["new"] is not None, f"the aggregator has no value for {yesterday}"):
            print(f"{yesterday}: aggregator turns={u_day['turns']} new={u_day['new']} cache_read={u_day['cache_read']}; independent {want}")
            need([u_day["turns"], u_day["new"], u_day["cache_read"]] == want, f"{yesterday} differs from the independent derivation")
            need(ind_after.get(yesterday, [0, 0, 0]) == want, f"{yesterday} changed during the run (not a fully past day?)")
        today_hud = first["claude"]["usage"]["windows"]["today"]["new"]
        lo, hi = ind_before.get(today, [0, 0, 0])[1], ind_after.get(today, [0, 0, 0])[1]
        print(f"today new: aggregator {today_hud}, independent before {lo} after {hi}")
        need(lo <= today_hud <= hi, "today's figure is not between the independent before and after reads")
        need(usage["scan"]["files"] >= 50, f"only {usage['scan']['files']} transcript files scanned")
        # polling load, 40 s in total
        while time.time() - t0 < 40:
            get(port, "/api/state")
            time.sleep(10)
        used = (cpu_seconds(proc.pid) - cpu0) / (time.time() - t0)
        print("aggregator cpu over %.0f s: %.1f%% of one core" % (time.time() - t0, used * 100))
        need(used < 0.10, f"aggregator used {used * 100:.1f}% of one core")
        wb = write_bytes(proc.pid)
        print("aggregator write_bytes=%s" % wb)
        need(wb == 0, f"the aggregator wrote {wb} bytes to disk")
        need(datetime.now().strftime("%Y-%m-%d") == today, "local midnight passed during the run: rerun")
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
    blob = b"".join(blobs).decode("utf-8", "replace") + (out or "")
    for i, secret in enumerate(secrets):
        need(not leaks(blob, secret), f"secret #{i} (access/refresh) appears in a payload or the aggregator output")
    print("secret_scan: %d secrets checked against %d payloads and the aggregator output" % (len(secrets), len(blobs)))
    need(cred_stamp() == before_stamp, "the credentials file's size or mtime changed during the run (Claude Code refreshed? rerun)")
    # Re-stat the SAME paths: a file that was idle at the start and was appended to since (by another Claude session) is
    # no longer "idle" by mtime, which must not be mistaken for a vanished file.
    idle_after = {}
    for path in idle_before:
        try:
            st = os.stat(path)
            idle_after[path] = (st.st_size, st.st_mtime_ns)
        except OSError:
            idle_after[path] = None
    changed = [p for p, v in idle_before.items() if idle_after[p] != v]
    for p in changed:
        was, now_ = idle_before[p], idle_after[p]
        print("idle file changed: %s size %s -> %s" % (os.path.basename(p), was[0], "gone" if now_ is None else now_[0]))
    # Another Claude session appending to a file that looked idle is benign; a shrink or a delete is not ours to explain.
    need(not [p for p in changed if idle_after[p] is None or idle_after[p][0] < idle_before[p][0]],
         "an idle transcript file shrank or vanished during the run")
    return report()


# ---------------------------------------------------------------- the installed unit

def unit_mode():
    base_state = None
    for _ in range(12):
        try:
            _, raw = get(8670, "/api/state", timeout=20)
        except OSError as e:
            print("installed unit probe failed:", type(e).__name__)
            return report("LIVE_CLAUDE_UNIT")
        base_state = json.loads(raw)
        if "claude" in base_state and base_state["claude"]["limits"]["state"] != "pending":
            break
        time.sleep(3)
    need("claude" in (base_state or {}), "the installed unit's /api/state has no claude key (restart the unit?)")
    st, loopback, files, fetches = None, False, 0, 0
    if base_state and "claude" in base_state:
        c = base_state["claude"]
        st = c["limits"]["state"]
        loopback = True
        fetches = json.loads(get(8670, "/api/detail/claude?id=limits")[1]).get("diag", {}).get("fetch_count", 0)
        u = c["usage"]
        files = (u.get("scan") or {}).get("files", 0)
        need(st in ("ok", "rate_limited", "scope_missing", "stale"),
             f"limits.state={st}: " + {"cred_unreadable": "the sandbox could not read the credentials file", "unreachable": "the sandbox could not reach the host",
                                       "no_credentials": "the sandbox sees no credentials file", "disabled": "switched off"}.get(st, "does not prove both read and reach"))
        need(fetches >= 1, "no outbound fetch was made by the unit (read proven, reach unproven)")
        need(u.get("state") == "ok" and isinstance((u.get("windows") or {}).get("d7", {}).get("new"), int), "usage figures are not numbers")
        need(files >= 50, f"scan.files={files} < 50 (the sandbox cannot read the transcripts?)")
    tail = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.split()
    try:
        socket.create_connection((tail[0] if tail else "100.91.184.86", 8670), timeout=3).close()
        refused = False
    except ConnectionRefusedError:
        refused = True
    except OSError as e:
        print("tailnet probe inconclusive:", type(e).__name__)
        refused = False
    need(refused, "the tailnet address did not refuse")
    jr = subprocess.run(["journalctl", "--user", "-u", "corellia-hud.service", "-n", "300", "--no-pager"], capture_output=True, text=True).stdout
    _, secrets = read_secrets_quiet()
    need(not any(leaks(jr, s) for s in secrets), "a secret appears in the unit's journal")
    print(f"unit: limits_state={st} fetches={fetches} scan_files={files} tailnet_refused={refused}")
    return report("LIVE_CLAUDE_UNIT", f" state={st} read=yes reach=yes loopback={'yes' if loopback else 'no'}")


def read_secrets_quiet():
    try:
        with open(CFG.claude_cred_path, "rb") as f:
            oauth = json.loads(f.read(65536)).get("claudeAiOauth", {})
    except (OSError, ValueError, AttributeError):
        return False, []
    return True, [v for v in (oauth.get("accessToken"), oauth.get("refreshToken")) if isinstance(v, str) and len(v) >= 16]


def raw_probe():
    """One real call; prints ONLY the five_hour and seven_day windows' two fields (never the token, never spend fields)."""
    st, token, _e, _s = claude_limits.read_credentials(CFG.claude_cred_path)
    if st != "ok":
        print("raw probe: credentials state", st)
        return 1
    kind, payload, extra = claude_limits._request(claude_limits._build_opener(), claude_limits.LIMITS_URL, token)
    token = None
    print("raw probe: kind=%s status_or_len=%s" % (kind, payload if kind == "http" else len(payload or b"")))
    if kind == "http":
        print("raw probe: http status", payload, "headers:", sorted(k.lower() for k in (extra or {}).keys()))
    if kind == "ok":
        obj = json.loads(payload)
        print("raw probe: top-level keys", sorted(obj))
        for key in claude_limits.WINDOW_KEYS:
            if key in obj:
                w = obj[key]
                print("raw probe:", key, {k: (w.get(k) if isinstance(w, dict) else w) for k in ("utilization", "resets_at")} if isinstance(w, dict) else repr(w))
    return 0


def report(prefix="LIVE_CLAUDE", suffix=""):
    for p in PROBLEMS:
        print("FAIL", p)
    if PROBLEMS:
        print(prefix + "_FAIL")
        return 1
    print(prefix + "_OK" + suffix)
    return 0


if __name__ == "__main__":
    if "--raw" in sys.argv:
        sys.exit(raw_probe())
    sys.exit(unit_mode() if "--unit" in sys.argv else default_mode())
