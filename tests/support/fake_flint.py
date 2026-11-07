#!/usr/bin/env python3
"""A fake Flint HTTP API (the read-only /api/status and /api/events routes) for tests and the e2e run.

Importable (FakeFlint) and runnable (`python3 -m tests.support.fake_flint --port N --config file.json`).
It records every request (verb, path, query) so tests can assert GET-only and cache behaviour, and it can
script every failure the games source must survive. An entry for a route is one of:
  {"json": <obj>}                 200 with that JSON (default code 200, override with "code")
  {"raw": "<text>"}               200 with that exact body (invalid JSON, non-dict JSON ...)
  {"redirect": "<url>"}           302 to that URL
  {"big": <bytes>}                200 with a body of that many bytes
  {"delay": <seconds>}            added to any of the above
`GET /__requests` returns the recorded list (the e2e reads it to prove the idle cost).
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeFlint:
    def __init__(self, config=None, port=0):
        self.config = config if config is not None else {"status": {}, "events": {}}
        self.requests = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                parts = urlsplit(self.path)
                if self.command == "GET" and parts.path == "/__requests":
                    with fake._lock:
                        raw = json.dumps(fake.requests).encode()
                    return self._reply(200, raw)
                with fake._lock:
                    fake.requests.append([self.command, parts.path, parts.query])
                if self.command != "GET":
                    return self._reply(405, b"read-only")
                server = parse_qs(parts.query).get("server", [""])[0]
                table = {"/api/status": "status", "/api/events": "events"}.get(parts.path)
                entry = fake.config.get(table, {}).get(server) if table else None
                if entry is None:
                    return self._reply(404, b'{"error":"no such route or server"}')
                if entry.get("delay"):
                    time.sleep(entry["delay"])
                if "redirect" in entry:
                    self.send_response(302)
                    self.send_header("Location", entry["redirect"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if "big" in entry:
                    return self._reply(entry.get("code", 200), b" " * entry["big"])
                if "raw" in entry:
                    return self._reply(entry.get("code", 200), entry["raw"].encode())
                return self._reply(entry.get("code", 200), json.dumps(entry.get("json")).encode())

            def _reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:   # the client gave up (timeout / size cap tests)
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = None

    def verbs(self):
        with self._lock:
            return [r[0] for r in self.requests]

    def count(self, path=None):
        with self._lock:
            return len([r for r in self.requests if path is None or r[1] == path])

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = json.load(f)
    fake = FakeFlint(cfg, port=a.port)
    print(f"fake-flint listening {fake.url}", flush=True)
    fake.httpd.serve_forever()


if __name__ == "__main__":
    main()
