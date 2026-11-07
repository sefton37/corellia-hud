"""A fake Cloudflare API (zone lookup + GraphQL analytics) for tests and the e2e run.

It records the method, path, Authorization header and body of every request, which is how the
token tests prove the sentinel token WAS transmitted (the positive control for the log scans).
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_COUNTS = {
    "kellogg.brengel.com": {"d1": 1200, "d7": 8400, "p1": 1000, "p7": 8000, "t1": 14, "t7": 90},
    "rogueroutine.brengel.com": {"d1": 310, "d7": 2000, "p1": 300, "p7": 1900, "t1": 3, "t7": 21},
    "talkingrock.ai": {"d1": 515, "d7": 3600, "p1": 0, "p7": 3000, "t1": 7, "t7": 52},
}


class FakeCF:
    def __init__(self, zones=None, counts=None, graphql_errors=None, port=0):
        # zones: {zone name: {"id": str, "http": status code for the zone lookup}}
        self.zones = zones if zones is not None else {
            "brengel.com": {"id": "zone-brengel", "http": 200},
            "talkingrock.ai": {"id": "zone-talkingrock", "http": 200},
        }
        self.counts = counts if counts is not None else DEFAULT_COUNTS
        self.graphql_errors = graphql_errors  # None, or a list returned as the `errors` array
        self.requests = []  # dicts: method, path, auth, body
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _record(self, body):
                with fake._lock:
                    fake.requests.append({
                        "method": self.command, "path": urlparse(self.path).path,
                        "auth": self.headers.get("Authorization"), "body": body})

            def do_GET(self):
                self._record("")
                u = urlparse(self.path)
                if u.path.endswith("/zones"):
                    name = parse_qs(u.query).get("name", [""])[0]
                    z = fake.zones.get(name)
                    if z is None:
                        return self._send(200, {"success": True, "result": []})
                    if z["http"] != 200:
                        return self._send(z["http"], {"success": False, "errors": [{"code": 9109, "message": "denied"}]})
                    return self._send(200, {"success": True, "result": [{"id": z["id"], "name": name}]})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode()
                self._record(body)
                if not urlparse(self.path).path.endswith("/graphql"):
                    return self._send(404, {})
                req = json.loads(body)
                variables = req.get("variables", {})
                if fake.graphql_errors is not None:
                    return self._send(200, {"data": None, "errors": fake.graphql_errors})
                counts = fake.counts.get(variables.get("host"), {})
                zone = {k: [{"count": v}] for k, v in counts.items()}
                self._send(200, {"data": {"viewer": {"zones": [zone]}}, "errors": None})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/client/v4"

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    fake = FakeCF(port=a.port)
    print(f"fake-cf listening {fake.url}", flush=True)
    fake.httpd.serve_forever()


if __name__ == "__main__":
    main()
