"""Loopback-only HTTP server: GET /api/state, GET /api/detail/<kind>?id=<id> and the static pages.
Nothing else: every other method is refused and nothing here is ever logged."""
import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import claude_limits, claude_usage, detail, games, state

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8670
DEFAULT_TTL = 15.0
STATIC_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static"))
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/-]*$")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
       "object-src 'none'; frame-ancestors 'none'")
DETAIL_PATH = re.compile(r"^/api/detail/([a-z]{1,16})$")
mimetypes.add_type("text/javascript", ".js")


def resolve_static(raw_path):
    """Map a request path to a file under static/, or None. Rejects traversal in every encoding."""
    path = unquote(urlsplit(raw_path).path)
    if path == "/":
        path = "/index.html"
    if not SAFE_PATH.match(path) or path.startswith("//") or ".." in path.split("/"):
        return None
    full = os.path.realpath(os.path.join(STATIC_ROOT, path.lstrip("/")))
    if not full.startswith(STATIC_ROOT + os.sep) or not os.path.isfile(full):
        return None
    return full


class Handler(BaseHTTPRequestHandler):
    server_version = "corellia-hud"
    sys_version = ""
    cache = None  # set per server in make_server
    detail = None

    def log_message(self, fmt, *args):  # no access log: nothing to leak, nothing to fill the journal
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _detail(self, kind):
        """/api/detail/<kind>?id=<id>: exactly one `id` and nothing else, else 400."""
        if kind not in detail.KINDS:
            return self._send(404, b'{"error":"unknown kind"}', "application/json")
        try:
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True,
                              max_num_fields=4, errors="strict")
        except ValueError:
            return self._send(400, b'{"error":"bad query"}', "application/json")
        ids = params.get("id", [])
        if set(params) != {"id"} or len(ids) != 1:
            return self._send(400, b'{"error":"bad query"}', "application/json")
        try:
            code, payload = self.detail.get(kind, ids[0])
        except Exception:
            return self._send(500, b'{"error":"detail build failed"}', "application/json")
        return self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self):
        path = urlsplit(self.path).path
        m = DETAIL_PATH.match(path)
        if m:
            return self._detail(m.group(1))
        if path.startswith("/api/detail"):
            return self._send(404, b'{"error":"not found"}', "application/json")
        if path == "/api/state":
            try:
                body = json.dumps(self.cache.get()).encode()
            except Exception:
                return self._send(500, b'{"error":"state build failed"}', "application/json")
            return self._send(200, body, "application/json")
        if ".." in unquote(self.path) and not SAFE_PATH.match(unquote(path)):
            return self._send(400, b"bad request", "text/plain")
        full = resolve_static(self.path)
        if full is None:
            code = 400 if not SAFE_PATH.match(unquote(path)) or unquote(path).startswith("//") else 404
            return self._send(code, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def _read_only(self):
        self._send(405, b"read-only", "text/plain", {"Allow": "GET"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _read_only


def make_server(cfg, host=LOOPBACK, port=DEFAULT_PORT, ttl=DEFAULT_TTL, limits_cache=None, usage_index=None):
    if host != LOOPBACK:
        raise ValueError("the HUD aggregator binds loopback only")
    gcache = games.GamesCache(cfg)
    limits = limits_cache or claude_limits.LimitsCache(cfg.claude_cred_path)
    usage = usage_index or claude_usage.ClaudeUsageIndex(cfg.claude_projects_dir)
    cache = state.StateCache(
        lambda: state.build_state(cfg, games_cache=gcache, limits_cache=limits, usage_index=usage), ttl)
    handler = type("BoundHandler", (Handler,), {
        "cache": cache, "detail": detail.Detail(cfg, cache, ttl, games_cache=gcache, limits_cache=limits,
                                                usage_index=usage)})
    return ThreadingHTTPServer((host, port), handler)


def main():
    port = int(os.environ.get("HUD_PORT", DEFAULT_PORT))
    ttl = float(os.environ.get("HUD_TTL", DEFAULT_TTL))
    httpd = make_server(state.Config(), LOOPBACK, port, ttl)
    print(f"corellia-hud listening on {LOOPBACK}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
