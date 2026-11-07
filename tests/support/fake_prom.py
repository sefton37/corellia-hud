"""A fake Prometheus HTTP API for tests and the e2e run.

Importable (FakeProm) and runnable (`python3 tests/support/fake_prom.py --scenario mixed --port N`).
It matches queries by substring so it stays independent of the exact PromQL the aggregator sends,
and it records every request (method, path) so tests can assert GET-only and cache behaviour.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# (substring of the query, key in the data dict). Order matters: first match wins.
QUERY_KEYS = [
    ("collector_last_run", "collector_age"),
    ("[24h])", "restarts24h"),
    ("increase(", "restarts"),
    ("max(node_filesystem_size", "disk_size"),
    ("port_up", "port_up"),
    ("unit_active", "unit_active"),
    ("unit_failed", "unit_failed"),
    ("timer_last_success", "timer_success"),
    ("timer_last_result", "timer_result"),
    ('mode="idle"', "cpu"),
    ("MemAvailable", "mem"),
    ("filesystem_avail", "disk"),
    ("gpu_utilization", "gpu_util"),
    ("gpu_memory", "gpu_mem"),
    ("node_load1", "load"),
    ("changes(", "reboots"),
    ("node_boot_time", "uptime"),
]

DETAIL_STEP = 300   # the detail charts ask for 24 h at a 300 s step: 288 points


def detail_series(key, n=288):
    """Deterministic 24 h series the e2e recomputes independently (min / max / avg / p95 / last)."""
    base = {"cpu": 10, "mem": 30, "gpu_util": 5, "load": 1, "disk": 40, "gpu_mem": 4_000_000_000}
    step = {"gpu_mem": 100_000_000}.get(key, 1)
    return [base.get(key, 0) + ((i * 7) % 50) * step for i in range(n)]


def series(labels, value):
    return (labels, value)


def healthy_data():
    """Instant-vector data for a quiet, healthy host (labels, value) pairs per key."""
    return {
        "collector_age": [({}, 30)],
        "port_up": [({"service": "alpha", "port": "5000"}, 1), ({"service": "beta", "port": "5001"}, 1)],
        "unit_active": [({"scope": "user", "unit": "gamma.service"}, 1)],
        "unit_failed": [({"scope": "user", "unit": "gamma.service"}, 0)],
        "restarts": [({"scope": "user", "unit": "gamma.service"}, 0)],
        "timer_success": [],
        "timer_result": [],
        "cpu": [({}, 12.5)],
        "mem": [({}, 23.0)],
        "disk": [({}, 41.0)],
        "gpu_util": [({}, 7)],
        "gpu_mem": [({}, 11994660864)],
        "load": [({}, 3.2)],
        "uptime": [({}, 400000)],
    }


class FakeProm:
    def __init__(self, data=None, alerts=None, fail=False, delay=0.0, port=0, series=None):
        self.data = data if data is not None else healthy_data()
        self.series = series if series is not None else {}   # {key: [24 h values]} for 300 s-step queries
        self.alerts = alerts or []
        self.fail = fail
        self.delay = delay
        self.requests = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                with fake._lock:
                    fake.requests.append((self.command, urlparse(self.path).path))
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.fail:
                    self.send_response(500)
                    self.end_headers()
                    return
                if self.command != "GET":
                    self.send_response(405)
                    self.end_headers()
                    return
                u = urlparse(self.path)
                q = parse_qs(u.query).get("query", [""])[0]
                if u.path == "/api/v1/alerts":
                    body = {"status": "success", "data": {"alerts": fake.alerts}}
                elif u.path in ("/api/v1/query", "/api/v1/query_range"):
                    step = int(parse_qs(u.query).get("step", ["0"])[0] or 0)
                    body = fake._answer(u.path, q, step)
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = _any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = None

    def _answer(self, path, q, step=0):
        key = next((k for sub, k in QUERY_KEYS if sub in q), None)
        rows = self.data.get(key, []) if key else []
        now = time.time()
        if path.endswith("query_range") and step == DETAIL_STEP and key in self.series:
            vals = self.series[key]
            end = int(now) - int(now) % DETAIL_STEP
            pts = [[end - (len(vals) - 1 - i) * DETAIL_STEP, str(v)] for i, v in enumerate(vals)]
            return {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": pts}]}}
        if path.endswith("query_range"):
            result = []
            for labels, val in rows:
                pts = [[now - 3600 + i * 60, str(float(val) * (0.8 + 0.4 * ((i * 7) % 10) / 10))] for i in range(60)]
                result.append({"metric": labels, "values": pts})
            return {"status": "success", "data": {"resultType": "matrix", "result": result}}
        result = [{"metric": labels, "value": [now, str(val)]} for labels, val in rows]
        return {"status": "success", "data": {"resultType": "vector", "result": result}}

    def count(self, method=None):
        with self._lock:
            return len([r for r in self.requests if method is None or r[0] == method])

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def detail_data():
    """e2e detail scenario: sieve down with restarts, a backup timer 3 h 20 m old, a restart-looping user unit."""
    d = healthy_data()
    d["port_up"] = [({"service": "sieve", "port": "5000"}, 0), ({"service": "alpha", "port": "5001"}, 1),
                    ({"service": "portfolio-chat", "port": "8001"}, 1)]
    units = [("system", "sieve.service"), ("system", "portfolio-chat.service"), ("user", "flint.service")]
    d["unit_active"] = [({"scope": sc, "unit": u}, 1) for sc, u in units]
    d["unit_failed"] = [({"scope": sc, "unit": u}, 0) for sc, u in units]
    d["restarts"] = [({"scope": "system", "unit": "sieve.service"}, 2)]
    d["restarts24h"] = [({"scope": "system", "unit": "sieve.service"}, 5)]
    d["timer_success"] = [({"scope": "system", "unit": "corellia-backup.service"}, time.time() - 12000)]
    d["timer_result"] = [({"scope": "system", "unit": "corellia-backup.service"}, 1)]
    d["disk_size"] = [({}, 2_000_000_000_000)]
    d["reboots"] = [({}, 3)]
    return d


def mixed_data():
    """e2e scenario 1: a down service, an up one, an absent one, a restart-looping one."""
    d = healthy_data()
    d["port_up"] = [
        ({"service": "sieve", "port": "5000"}, 0),
        ({"service": "alpha", "port": "5001"}, 1),
    ]
    d["unit_active"] = [({"scope": "user", "unit": "loopy.service"}, 1)]
    d["unit_failed"] = [({"scope": "user", "unit": "loopy.service"}, 0)]
    d["restarts"] = [({"scope": "user", "unit": "loopy.service"}, 9)]
    return d


def rich_data():
    """Design/e2e scenario: the whole estate up except one down, one restart-looping, one absent."""
    from tests.support import fixtures as fx
    d = healthy_data()
    d["port_up"] = [({"service": n, "port": str(p)}, 0 if n == "sieve" else 1)
                    for n, p in fx.RICH_PORTS.items() if n != "portainer"]
    units = [("system", f"{n}.service") for n in fx.RICH_UNITS] + [("user", "stalag.service"), ("user", "whoop-sync.service")]
    d["unit_active"] = [({"scope": sc, "unit": u}, 1) for sc, u in units]
    d["unit_failed"] = [({"scope": sc, "unit": u}, 0) for sc, u in units]
    d["restarts"] = [({"scope": "user", "unit": "stalag.service"}, 9)]
    d["cpu"], d["mem"], d["disk"], d["gpu_util"], d["load"] = [({}, 38)], [({}, 61)], [({}, 28)], [({}, 74)], [({}, 5.8)]
    return d


def allgreen_data():
    """The 27-service estate with every probe present and healthy (pairs with fixtures.allgreen_registry)."""
    from tests.support import fixtures as fx
    d = rich_data()
    ports = dict(fx.RICH_PORTS, **{"rogue-routine": fx.ROGUE_PORT})
    d["port_up"] = [({"service": n, "port": str(p)}, 1) for n, p in ports.items()]
    d["restarts"] = []
    return d


def onedown_data():
    """allgreen with exactly one service down: sieve's port is closed."""
    d = allgreen_data()
    d["port_up"] = [(lab, 0 if lab["service"] == "sieve" else v) for lab, v in d["port_up"]]
    return d


def manydown_data():
    """Nine not-up services on the RICH registry: red sieve / forgejo / ollama, amber stalag / whoop-sync
    (restart loops), grey portainer / sieve-geo / n8n (no series) and rogue-routine (no probe)."""
    from tests.support import fixtures as fx
    d = rich_data()
    gone = {"portainer", "sieve-geo", "n8n"}
    down = {"sieve", "forgejo", "ollama"}
    d["port_up"] = [({"service": n, "port": str(p)}, 0 if n in down else 1) for n, p in fx.RICH_PORTS.items() if n not in gone]
    d["restarts"] = [({"scope": "user", "unit": "stalag.service"}, 9), ({"scope": "user", "unit": "whoop-sync.service"}, 9)]
    return d


def stale_data():
    """allgreen, but the collector last ran an hour ago: every would-be green must read amber, never up."""
    d = allgreen_data()
    d["collector_age"] = [({}, 3600)]
    return d


DETAIL_ALERTS = [
    {"labels": {"alertname": "ServicePortDown", "severity": "critical", "service": "sieve"},
     "annotations": {"summary": "sieve port 5000 is closed", "description": "probe failed 3 times"},
     "state": "firing", "activeAt": "2026-11-05T10:00:00Z"},
]

MIXED_ALERT = [{
    "labels": {"alertname": "ServicePortDown", "severity": "critical", "service": "sieve"},
    "annotations": {"summary": "sieve port 5000 is closed"},
    "state": "firing"}]

SCENARIOS = {
    "healthy": (healthy_data, []),
    "mixed": (mixed_data, MIXED_ALERT),
    "rich": (rich_data, MIXED_ALERT),
    "detail": (detail_data, DETAIL_ALERTS),
    "allgreen": (allgreen_data, []),
    "onedown": (onedown_data, MIXED_ALERT),
    "manydown": (manydown_data, MIXED_ALERT),
    "stale": (stale_data, []),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="healthy")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--hostile", action="store_true", help="plant markup payloads in the alert summary")
    ap.add_argument("--inactive", default="", help="games scenario: comma-separated units reported inactive")
    ap.add_argument("--rich", action="store_true", help="games scenario: the populated estate (rich) plus the game units")
    a = ap.parse_args()
    if a.scenario == "games":   # the four game units (scope system), optionally some of them stopped
        from tests.support.games_world import prom_data
        down = {u: 0 for u in a.inactive.split(",") if u}
        failed = {u: 1 for u in down}
        def games_data():
            d = prom_data(active=down, failed=failed)
            if a.rich:
                r = rich_data()
                r["restarts24h"] = d["restarts24h"]
                return r
            return d
        SCENARIOS["games"] = (games_data, [])
    data_fn, alerts = SCENARIOS[a.scenario]
    if a.hostile:
        from tests.support import fixtures as fx
        alerts = [dict(al, annotations=dict(al["annotations"], summary=fx.HOSTILE)) for al in alerts]
    series = {k: detail_series(k) for k in ("cpu", "mem", "gpu_util", "load", "disk", "gpu_mem")}
    fake = FakeProm(data_fn(), alerts, port=a.port, series=series)
    print(f"fake-prom listening {fake.url}", flush=True)
    fake.httpd.serve_forever()


if __name__ == "__main__":
    main()
