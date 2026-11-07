import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest

from corellia_hud import server, state
from tests.support import fixtures as fx
from tests.support.fake_prom import FakeProm, healthy_data


class ServerTests(unittest.TestCase):
    TTL = 1.5

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.tmp.name
        reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_HEALTHY)
        adb = fx.make_analytics(os.path.join(t, "a.db"), fx.beacon_events(time.time()))
        pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(time.time()))
        cls.prom = FakeProm(healthy_data()).start()
        cfg = state.Config(prom_url=cls.prom.url, registry_path=reg, analytics_db=adb, product_db=pdb,
                           cf_token_file=os.path.join(t, "none"), cf_api_base="http://127.0.0.1:9")
        cls.httpd = server.make_server(cfg, host="127.0.0.1", port=0, ttl=cls.TTL)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.prom.stop()
        cls.tmp.cleanup()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body)
        r = c.getresponse()
        data = r.read()
        headers = dict(r.getheaders())
        c.close()
        return r.status, headers, data

    def raw(self, request_line):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall((request_line + " HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").encode())
        out = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            out += chunk
        s.close()
        return out

    def test_listening_address_is_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_non_loopback_bind_is_refused(self):
        for host in ("0.0" + ".0.0", "", "100.91.184.86"):
            with self.assertRaises(ValueError):
                server.make_server(state.Config(), host=host, port=0)

    def test_write_methods_are_405(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            status, _, _ = self.req(m, "/api/state", body=b"{}")
            self.assertEqual(status, 405, m)

    def test_api_state_is_json_with_the_seven_keys(self):
        status, headers, data = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(set(json.loads(data)), {"meta", "alerts", "services", "perf", "web", "games", "claude"})

    def test_root_serves_the_page(self):
        status, headers, data = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Corellia", data)

    def test_static_assets_are_served(self):
        for path, ctype in (("/hud.css", "text/css"), ("/hud.js", "javascript")):
            status, headers, _ = self.req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, headers["Content-Type"])

    def test_path_traversal_is_rejected(self):
        for line in ("GET /../etc/passwd", "GET /%2e%2e/%2e%2e/etc/passwd", "GET /static/../../etc/passwd",
                     "GET /..%2f..%2fetc/passwd", "GET //etc/passwd"):
            out = self.raw(line)
            status = int(out.split(b" ", 2)[1])
            self.assertIn(status, (400, 403, 404), line)
            self.assertNotIn(b"root:", out, line)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.req("GET", "/nope")[0], 404)

    def test_security_headers(self):
        _, headers, _ = self.req("GET", "/")
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_cache_ttl_two_requests_cause_one_upstream_fetch_then_a_refetch(self):
        time.sleep(self.TTL + 0.2)                  # expire anything cached by earlier tests
        self.req("GET", "/api/state")
        first = self.prom.count()
        self.assertGreater(first, 0)
        self.req("GET", "/api/state")
        self.assertEqual(self.prom.count(), first)  # served from cache, no new upstream fetch
        time.sleep(self.TTL + 0.3)
        self.req("GET", "/api/state")
        self.assertGreater(self.prom.count(), first)  # after the TTL a second fetch happens


if __name__ == "__main__":
    unittest.main()
