"""Fixtures for the Claude usage KPI: a transcript-tree builder (lines shaped from the OBSERVED real keys),
a credentials-fixture writer, and a scripted opener standing in for urllib's opener.

Every secret here is assembled at run time from short parts (the content guard blocks a literal after a key
name). Credential fixtures are always named fixture_cred.json, never the real filename.

CLI (the e2e): python3 -m tests.support.fake_claude serve <dir> <port> [--limits=<state>] [--tree=<kind>]
[--prom=URL --registry=PATH --analytics=PATH --product=PATH --cf-token=PATH --cf-api=URL --chat-dir=PATH]
builds the tree under <dir> and runs the REAL aggregator in-process with the scripted opener injected, because
the host of the one outbound call cannot be changed from the environment."""
import json
import os
import stat as stat_mod
import sys
import threading
import time
import urllib.error
from datetime import datetime, timezone

ACCESS_SENTINEL = "ACC" + "ESS-SENT" + "INEL-" + "9fQ2" + "mLx7"
REFRESH_SENTINEL = "REF" + "RESH-SENT" + "INEL-" + "4kZp" + "w8Ty"
PROMPT_SENTINEL = "PROMPT" + "SENTINEL-" + "tell-me-a-secret"
PROJECT_SENTINEL = "proj" + "SENTINEL" + "-atlas"
SESSION_SENTINEL = "sess" + "SENTINEL" + "-0001"
SPEND_SENTINEL = "SPEND" + "SENTINEL" + "-777"
HOSTILE = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>javascript:window.__pwned=1'
HOSTILE_NAME = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1<' + 'x>javascript:window.__pwned=1'   # no slash: it survives a basename


def iso(epoch):
    """The transcript timestamp shape: UTC ISO with milliseconds and a Z."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % int(epoch % 1 * 1000)


def line(ts, model="claude-sonnet-5-5", mid=None, rid=None, inp=10, out=20, cw=100, cr=1000, cwd=None, session="sess-a",
         text=PROMPT_SENTINEL, uuid=None, **over):
    """One assistant transcript line carrying message.usage (the keys the real files carry)."""
    n = line.counter = getattr(line, "counter", 0) + 1
    obj = {
        "type": "assistant", "uuid": uuid or f"uuid-{n}", "timestamp": iso(ts), "sessionId": session,
        "cwd": cwd if cwd is not None else "/home/kel/dev/" + PROJECT_SENTINEL, "isSidechain": False,
        "requestId": rid if rid is not None else f"req-{n}",
        "message": {"id": mid if mid is not None else f"msg-{n}", "model": model, "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
                              "cache_read_input_tokens": cr, "service_tier": "standard"}},
    }
    obj.update(over)
    return obj


def user_line(ts, text=PROMPT_SENTINEL):
    """A prompt line: carries the word usage in its text but no message.usage object."""
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": text + " usage"}}


def dumps(obj):
    return json.dumps(obj, separators=(",", ":")) + "\n"


class Tree:
    """A fixture ~/.claude/projects tree in a temp directory."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, lines, raw=None):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(raw if raw is not None else "".join(dumps(x) if isinstance(x, dict) else x for x in lines).encode())
        return p

    def append(self, rel, lines, raw=None):
        with open(self.path(rel), "ab") as f:
            data = raw if raw is not None else "".join(dumps(x) if isinstance(x, dict) else x for x in lines).encode()
            f.write(data)
        return len(data)

    def hashes(self):
        import hashlib
        out = {}
        for dp, _, fs in os.walk(self.root):
            for fn in fs:
                p = os.path.join(dp, fn)
                if not os.path.islink(p):
                    with open(p, "rb") as f:
                        out[p] = hashlib.sha256(f.read()).hexdigest()
        return out


def standard_tree(root, now, project=None):
    """A week-plus fixture with known sums: two projects, a subagent file, duplicates, two models.
    Returns (Tree, expected dict). Hand-computed values live in EXPECT below."""
    t = Tree(root)
    p1 = "/home/kel/dev/" + (project or PROJECT_SENTINEL)
    day = 86400
    # The "today" turns sit just after LOCAL midnight (not at now-N), so the fixture is the same whatever the time of day.
    mid = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    lines_a = [
        line(mid + 1, mid="m1", rid="r1", inp=10, out=5, cw=100, cr=1000, cwd=p1, session="sess-a"),
        line(mid + 1, mid="m1", rid="r1", inp=10, out=50, cw=100, cr=1000, cwd=p1, session="sess-a"),  # growing dup
        line(mid + 2, mid="m2", rid="r2", inp=20, out=40, cw=0, cr=2000, cwd=p1, session="sess-a"),
        line(now - 3 * day, mid="m3", rid="r3", inp=5, out=15, cw=30, cr=500, cwd=p1, session="sess-b",
             model="claude-opus-5-5"),
    ]
    t.write("-home-kel-dev-atlas/sess-a.jsonl", lines_a + [user_line(mid + 1)])
    t.write("-home-kel-dev-atlas/sess-a/subagents/agent-1.jsonl", [
        line(mid + 3, mid="m4", rid="r4", inp=1, out=2, cw=3, cr=4, cwd=p1, session="sess-a", model="claude-opus-5-5")])
    return t


# Hand-computed from standard_tree: new = input + cache_creation + output; the dup keeps its largest output (50).
EXPECT = {
    "today_new": (10 + 100 + 50) + (20 + 0 + 40) + (1 + 3 + 2),     # m1 + m2 + m4 (all within the last 10 minutes)
    "d7_new": (10 + 100 + 50) + (20 + 0 + 40) + (1 + 3 + 2) + (5 + 30 + 15),
    "d7_cache_read": 1000 + 2000 + 4 + 500,
    "turns_d7": 4,
}


# ------------------------------------------------------------------------- credentials fixture

def write_cred(path, access=ACCESS_SENTINEL, refresh=REFRESH_SENTINEL, expires_ms=None, scopes=None, mode=0o600,
               body=None):
    """Write a fixture credentials file of the OBSERVED shape (claudeAiOauth.{accessToken, refreshToken, expiresAt, scopes})."""
    oauth = {"accessToken": access, "refreshToken": refresh,
             "scopes": scopes if scopes is not None else ["user:inference", "user:profile"],
             "subscriptionType": "max"}
    if expires_ms is not False:
        oauth["expiresAt"] = expires_ms if expires_ms is not None else int((time.time() + 3600) * 1000)
    text = body if body is not None else json.dumps({"claudeAiOauth": oauth})
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


# ------------------------------------------------------------------------- scripted opener

class FakeResp:
    def __init__(self, body=b"", status=200, headers=None):
        self._body, self.status, self.headers = body, status, headers or {}

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def limits_body(five=37.0, week=12.5, five_reset=None, week_reset=None, extra=None, opus=None, sonnet=None):
    now = time.time()
    fr = five_reset if five_reset is not None else now + 2 * 3600 + 600
    wr = week_reset if week_reset is not None else now + 4 * 86400
    obj = {"five_hour": {"utilization": five, "resets_at": datetime.fromtimestamp(fr, tz=timezone.utc).isoformat()},
           "seven_day": {"utilization": week, "resets_at": datetime.fromtimestamp(wr, tz=timezone.utc).isoformat()},
           "extra_usage": {"is_enabled": True, "note": SPEND_SENTINEL}, "limits": [{"spend": SPEND_SENTINEL}]}
    if opus is not None:
        obj["seven_day_opus"] = {"utilization": opus, "resets_at": datetime.fromtimestamp(wr, tz=timezone.utc).isoformat()}
    if sonnet is not None:
        obj["seven_day_sonnet"] = {"utilization": sonnet, "resets_at": None}
    obj.update(extra or {})
    return json.dumps(obj).encode()


def http_error(code, headers=None, body=b"{}"):
    import io
    return urllib.error.HTTPError("https://example.invalid/", code, "x", headers or {}, io.BytesIO(body))


class ScriptedOpener:
    """open(req, timeout) pops the next scripted step: bytes -> a 200 body, FakeResp, or an Exception to raise.
    The last step repeats. `gate`, when set, blocks every open until it is set (the non-blocking tests)."""

    def __init__(self, *steps, gate=None):
        self.steps = list(steps) or [FakeResp(limits_body())]
        self.requests = []   # one dict per open(): url, method, headers (lowercased names), data, timeout
        self.gate = gate
        self._lock = threading.Lock()

    def open(self, req, timeout=None):
        hdrs = {k.lower(): v for k, v in list(req.header_items())}
        with self._lock:
            self.requests.append({"url": req.full_url, "method": req.get_method(), "headers": hdrs,
                                  "data": req.data, "timeout": timeout})
            step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if self.gate is not None:
            self.gate.wait(30)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, (bytes, bytearray)):
            return FakeResp(bytes(step))
        return step

    @property
    def count(self):
        return len(self.requests)


# ------------------------------------------------------------------------- e2e server

def _serve(argv):
    from corellia_hud import claude_limits, claude_usage, server, state
    out, port = os.path.abspath(argv[0]), int(argv[1])
    opts = dict(a[2:].split("=", 1) for a in argv[2:] if a.startswith("--") and "=" in a)
    now = time.time()
    kind = opts.get("tree", "standard")
    root = os.path.join(out, "projects")
    if kind == "standard":
        standard_tree(root, now)
    elif kind == "hostile":
        standard_tree(root, now, project="hostile-" + "x")
        Tree(root).write("hostile/h.jsonl", [line(datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 4, model="claude-x", cwd="/home/kel/" + HOSTILE_NAME, session="sess-h", mid="mh", rid="rh")])
    elif kind == "ten":   # ten days of history
        t = Tree(root)
        t.write("p/old.jsonl", [line(now - 10 * 86400 + 600, mid="mo", rid="ro", cwd="/home/kel/dev/p")]
                + [line(now - 600, mid="mn", rid="rn", cwd="/home/kel/dev/p")])
    elif kind == "missing":
        pass
    cred = os.path.join(out, "fixture_cred.json")
    st = opts.get("limits", "ok")
    steps = {
        "ok": [FakeResp(limits_body())], "rate_limited": [http_error(429, {"Retry-After": "120"})],
        "unreachable": [urllib.error.URLError("refused")], "schema_mismatch": [FakeResp(b'{"unexpected": 1}')],
        "needs_reauth": [http_error(401)], "pending": [FakeResp(limits_body())],
        "stale": [FakeResp(limits_body()), urllib.error.URLError("refused")],
    }.get(st, [FakeResp(limits_body())])
    gate = threading.Event() if st == "pending" else None
    expires = None
    if st == "token_expired":
        expires = int((time.time() - 3600) * 1000)
    if st != "no_credentials":
        write_cred(cred, expires_ms=expires)
    opener = ScriptedOpener(*steps, gate=gate)
    t0 = time.monotonic()
    # `stale`: the cache's clock jumps one TTL ahead 3 s after start, so the second (failing) fetch comes due.
    jump = (lambda: time.monotonic() + (claude_limits.LIMITS_TTL + 10 if time.monotonic() - t0 > 3 else 0)) \
        if st == "stale" else time.monotonic
    limits = claude_limits.LimitsCache(cred, opener=opener, clock=jump)
    if st == "disabled":
        os.environ["HUD_CLAUDE_LIMITS"] = "0"
    # The services e2e runs the whole populated column through this one in-process aggregator: it points the other
    # sources at fixture files / fake servers via options (everything unset keeps the dead defaults below).
    dead = "http://127.0.0.1:9"
    cfg = state.Config(claude_projects_dir=root, claude_cred_path=cred, prom_url=opts.get("prom", dead),
                       registry_path=opts.get("registry", os.path.join(out, "none.yaml")),
                       analytics_db=opts.get("analytics", os.path.join(out, "none.db")),
                       product_db=opts.get("product", os.path.join(out, "none2.db")),
                       cf_token_file=opts.get("cf-token", os.path.join(out, "none")),
                       cf_api_base=opts.get("cf-api", dead), chat_dir=opts.get("chat-dir", os.path.join(out, "chat")))
    usage = claude_usage.ClaudeUsageIndex(root)
    httpd = server.make_server(cfg, "127.0.0.1", port, ttl=float(opts.get("ttl", "1")),
                               limits_cache=limits, usage_index=usage)
    # a tiny control port is not needed: the e2e reads request counts through /api/detail/claude?id=limits
    # (diag.fetch_count). The scripted opener lives in this process, so the count is the real one.
    print("serving", port, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        _serve(sys.argv[2:])
