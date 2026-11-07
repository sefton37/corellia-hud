"""GET /api/detail/<kind>?id=<id> end to end against fixtures: shapes, allowlist, privacy, cache, hygiene."""
import contextlib
import http.client
import io
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from corellia_hud import server, state
from tests.support import fixtures as fx
from tests.support.fake_journalctl import SECRET
from tests.support.fake_prom import FakeProm, detail_data, detail_series, DETAIL_ALERTS

FAKE_JOURNALCTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "support", "fake_journalctl.py")


class DetailBase(unittest.TestCase):
    TTL = 1.0
    PROM_DOWN = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.tmp.name
        now = time.time()
        reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_DETAIL)
        adb = fx.make_analytics(os.path.join(t, "a.db"), fx.beacon_events(now) + fx.detail_events(now))
        fx.add_rows(adb, "INSERT INTO daily_countries (site,date,country,pageviews) VALUES (?,?,?,?)",
                    [("kellogg", fx.utc_str(now - 86400)[:10], "US", 40), ("kellogg", fx.utc_str(now - 86400)[:10], "DE", 7)])
        pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(now) + [("hc-9", "fail", now - 60)])
        cls.chat_dir = fx.make_chat_tree(os.path.join(t, "chat"), now, hostile=False)
        cls.journal_log = os.path.join(t, "journal.log")
        os.environ["FAKE_JOURNAL_LOG"] = cls.journal_log
        series = {k: detail_series(k) for k in ("cpu", "mem", "gpu_util", "load", "disk", "gpu_mem")}
        cls.prom = FakeProm(detail_data(), DETAIL_ALERTS, port=0, series=series).start()
        cfg = state.Config(prom_url=cls.prom.url, registry_path=reg, analytics_db=adb, product_db=pdb,
                           cf_token_file=os.path.join(t, "none"), cf_api_base="http://127.0.0.1:9",
                           chat_dir=cls.chat_dir, journalctl=FAKE_JOURNALCTL)
        cls.httpd = server.make_server(cfg, host="127.0.0.1", port=0, ttl=cls.TTL)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.prom.stop()
        os.environ.pop("FAKE_JOURNAL_LOG", None)
        cls.tmp.cleanup()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request(method, path, body=body)
        r = c.getresponse()
        data = r.read()
        headers = dict(r.getheaders())
        c.close()
        return r.status, headers, data

    def detail(self, kind, ident):
        status, headers, data = self.req("GET", f"/api/detail/{kind}?id={quote(ident, safe='')}")
        return status, headers, (json.loads(data) if data[:1] == b"{" else data)

    def journal_calls(self):
        if not os.path.exists(self.journal_log):
            return 0
        with open(self.journal_log) as f:
            return len(f.readlines())


class ShapeTests(DetailBase):
    def test_service_detail_shape(self):
        status, _, d = self.detail("service", "sieve")
        self.assertEqual(status, 200)
        for key in ("state", "scope", "port", "probe", "status", "label", "reason", "unit_active", "unit_failed",
                    "restarts_30m", "restarts_24h", "timer_age_s", "related_alerts", "journal"):
            self.assertIn(key, d)
        self.assertEqual((d["status"], d["label"], d["scope"], d["port"]), ("red", "DOWN", "system", 5000))
        self.assertEqual((d["restarts_30m"], d["restarts_24h"]), (2, 5))
        self.assertEqual(d["journal"]["state"], "ok")
        self.assertLessEqual(len(d["journal"]["lines"]), 20)
        self.assertEqual([a["name"] for a in d["related_alerts"]], ["ServicePortDown"])

    def test_timer_service_reports_age_and_journals_the_matching_service(self):
        status, _, d = self.detail("service", "corellia-backup")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(d["timer_age_s"], 12000, delta=120)
        self.assertIn("corellia-backup.service", "\n".join(d["journal"]["lines"]))

    def test_user_scope_service_uses_the_user_journal(self):
        _, _, d = self.detail("service", "flint")
        self.assertEqual(d["scope"], "user")
        self.assertIn("scope=user", "\n".join(d["journal"]["lines"]))

    def test_services_list_shape(self):
        status, _, d = self.detail("services", "red")
        self.assertEqual(status, 200)
        self.assertEqual(d["status"], "red")
        self.assertEqual([r["name"] for r in d["rows"]], ["sieve"])

    def test_perf_detail_stats_match_an_independent_computation(self):
        status, _, d = self.detail("perf", "cpu")
        self.assertEqual(status, 200)
        vals = detail_series("cpu")
        srt = sorted(vals)
        self.assertEqual(len(d["series"]), 288)
        self.assertEqual((d["min"], d["max"], d["last"]), (min(vals), max(vals), vals[-1]))
        self.assertAlmostEqual(d["avg"], sum(vals) / len(vals), places=3)
        self.assertEqual(d["p95"], srt[-(-95 * len(vals) // 100) - 1])   # nearest rank
        self.assertEqual(d["thresholds"], {"amber": 85, "red": 97})

    def test_perf_extras_for_disk_vram_and_uptime(self):
        _, _, disk = self.detail("perf", "disk")
        self.assertEqual(disk["extras"]["total_bytes"], 2_000_000_000_000)
        _, _, vram = self.detail("perf", "vram")
        self.assertIn("total_bytes", vram["extras"])
        _, _, up = self.detail("perf", "uptime")
        self.assertEqual(up["extras"]["reboots_30d"], 3)
        self.assertAlmostEqual(up["extras"]["boot_time"], time.time() - 400000, delta=300)

    def test_site_detail_shape_and_cloudflare_stays_token_needed(self):
        status, _, d = self.detail("site", "kellogg.brengel.com")
        self.assertEqual(status, 200)
        b = d["beacon"]
        self.assertEqual(b["top_pages"][0]["path"], "/")
        self.assertEqual({p["path"]: p["views"] for p in b["top_pages"]}["/blog/a/"], 3)
        self.assertEqual(b["top_referrers"][0]["referrer"], "https://news.ycombinator.com/")
        self.assertEqual(len(b["daily_7d"]), 7)
        self.assertEqual(sum(day["pageviews"] for day in b["daily_7d"]), 26 + 5)
        self.assertEqual({c["country"]: c["pageviews"] for c in b["countries"]}, {"US": 40, "DE": 7})
        self.assertEqual(d["cf"]["state"], "token_needed")
        self.assertNotIn("requests_24h", d["cf"])

    def test_chat_detail_shape(self):
        status, _, d = self.detail("chat", "kellogg.brengel.com")
        self.assertEqual(status, 200)
        self.assertEqual(d["state"], "ok")
        self.assertEqual(len(d["conversations"]), 4)
        self.assertEqual(d["contacts"]["quarantined"], 5)
        self.assertIn("funnel", d)

    def test_alert_detail_for_prometheus_and_health_alerts(self):
        status, _, d = self.detail("alert", "ServicePortDown")
        self.assertEqual(status, 200)
        self.assertEqual((d["severity"], d["source"]), ("critical", "prometheus"))
        self.assertIn("closed", d["summary"])
        self.assertEqual(d["labels"]["service"], "sieve")
        self.assertIsNotNone(d["since"])
        status, _, h = self.detail("alert", "health:hc-9")
        self.assertEqual(status, 200)
        self.assertEqual(h["source"], "health")
        self.assertEqual(h["runs"][0]["result"], "fail")
        self.assertLessEqual(len(h["runs"]), 5)


class AllowlistTests(DetailBase):
    def test_unknown_id_is_404_for_every_kind(self):
        for kind in ("service", "services", "perf", "site", "chat", "alert"):
            status, _, _ = self.detail(kind, "no-such-thing")
            self.assertEqual(status, 404, kind)

    def test_unknown_kind_is_404(self):
        self.assertEqual(self.req("GET", "/api/detail/bogus?id=sieve")[0], 404)
        self.assertEqual(self.req("GET", "/api/detail/?id=sieve")[0], 404)
        self.assertEqual(self.req("GET", "/api/detail/service/extra?id=sieve")[0], 404)

    def test_malformed_repeated_or_missing_params_are_400(self):
        for path in ("/api/detail/service", "/api/detail/service?id=", "/api/detail/service?id=a&id=b",
                     "/api/detail/service?id=sieve&x=1", "/api/detail/service?x=sieve", "/api/detail/service?id"):
            self.assertEqual(self.req("GET", path)[0], 400, path)

    def test_hostile_ids_never_reach_journalctl(self):
        before = self.journal_calls()
        for ident in ("..", "%2e%2e", "%00", ";", "$(id)", "`id`", "a\nb", "x" * 5000, "sieve;id", "sieve.service",
                      "-x", "a b", "sieve%0a", "../etc/passwd"):
            status, _, _ = self.req("GET", f"/api/detail/service?id={ident if ident.startswith('%') else quote(ident, safe='')}")
            self.assertIn(status, (400, 404), ident)
        self.assertEqual(self.journal_calls(), before)

    def test_write_methods_are_405_on_detail_routes(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.req(m, "/api/detail/service?id=sieve", body=b"{}")[0], 405, m)

    def test_traversal_through_the_detail_prefix_is_rejected(self):
        for path in ("/api/detail/../state", "/api/detail/%2e%2e/%2e%2e/etc/passwd", "/api/detail/..%2fstate"):
            status, _, data = self.req("GET", path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn(b"root:", data)


class HeaderTests(DetailBase):
    def test_security_headers_on_detail_responses_and_the_page(self):
        for path in ("/api/detail/service?id=sieve", "/api/detail/service?id=nope", "/api/detail/bogus?id=x",
                     "/detail.html?kind=service&id=sieve"):
            status, h, _ = self.req("GET", path)
            self.assertIn("no-store", h["Cache-Control"], path)
            self.assertEqual(h["X-Content-Type-Options"], "nosniff", path)
            csp = h["Content-Security-Policy"]
            for want in ("default-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "script-src 'self'"):
                self.assertIn(want, csp, path)
        self.assertEqual(self.req("GET", "/detail.html?kind=service&id=sieve")[0], 200)
        self.assertEqual(self.req("GET", "/detail.js")[0], 200)
        self.assertEqual(self.req("GET", "/detail.css")[0], 200)


class PrivacyTests(DetailBase):
    def test_api_state_never_carries_conversation_text_contacts_or_journal_lines(self):
        self.detail("chat", "kellogg.brengel.com")
        self.detail("service", "sieve")
        _, _, state_bytes = self.req("GET", "/api/state")
        for needle in (fx.VISITOR_SENTINEL, fx.IP_HASH_SENTINEL, fx.CONTACT_EMAIL_SENTINEL, "answer", "line 2", "scope="):
            self.assertNotIn(needle.encode(), state_bytes, needle)
        _, _, chat_bytes = self.req("GET", "/api/detail/chat?id=kellogg.brengel.com")
        self.assertIn(fx.VISITOR_SENTINEL.encode(), chat_bytes)               # positive control
        self.assertNotIn(fx.IP_HASH_SENTINEL.encode(), chat_bytes)
        self.assertNotIn(fx.CONTACT_EMAIL_SENTINEL.encode(), chat_bytes)

    def test_chat_tile_summary_in_state_is_counts_only(self):
        _, _, raw = self.req("GET", "/api/state")
        site = next(s for s in json.loads(raw)["web"]["sites"] if s["host"] == "kellogg.brengel.com")
        self.assertEqual(site["chat"]["conversations_7d"], 3)
        self.assertNotIn("conversations", site["chat"])

    def test_journal_secret_is_served_raw_in_the_response_with_a_positive_control(self):
        raw = subprocess.run([FAKE_JOURNALCTL, "--unit=x"], capture_output=True, text=True).stdout
        self.assertIn(SECRET, raw)                                             # the fake does emit it
        _, _, body = self.req("GET", "/api/detail/service?id=sieve")
        self.assertIn(SECRET.encode(), body)
        self.assertIn(("Bearer " + SECRET).encode(), body)

    def test_nothing_is_written_to_stdout_stderr_or_logging(self):
        out, err = io.StringIO(), io.StringIO()
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        root = logging.getLogger()
        handler = Grab()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                bodies = [self.req("GET", p)[2] for p in (
                    "/api/detail/chat?id=kellogg.brengel.com", "/api/detail/service?id=sieve",
                    "/api/detail/chat?id=nope", "/api/detail/service?id=%00")]
        finally:
            root.removeHandler(handler)
        self.assertIn(fx.VISITOR_SENTINEL.encode(), bodies[0])                 # positive control
        captured = out.getvalue() + err.getvalue() + "\n".join(records)
        for needle in (fx.VISITOR_SENTINEL, SECRET, fx.IP_HASH_SENTINEL, fx.CONTACT_EMAIL_SENTINEL):
            self.assertNotIn(needle, captured)


class CacheTests(DetailBase):
    def test_two_requests_inside_the_ttl_cause_one_upstream_fetch_per_kind_and_id(self):
        time.sleep(self.TTL + 0.3)
        self.detail("perf", "mem")
        first = self.prom.count()
        self.detail("perf", "mem")
        self.assertEqual(self.prom.count(), first)               # cached
        self.detail("perf", "gpu")
        self.assertGreater(self.prom.count(), first)             # a different id has its own fetch
        n = self.prom.count()
        time.sleep(self.TTL + 0.3)
        self.detail("perf", "mem")
        self.assertGreater(self.prom.count(), n)                 # expired

    def test_journal_is_one_call_per_service_per_ttl(self):
        time.sleep(self.TTL + 0.3)
        before = self.journal_calls()
        self.detail("service", "alpha")                          # no unit: no journal call at all
        self.detail("service", "flint")
        self.detail("service", "flint")
        self.assertEqual(self.journal_calls() - before, 1)


class FailSoftTests(unittest.TestCase):
    def build(self, prom_fail=False, journal_mode="ok"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t, now = tmp.name, time.time()
        reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_DETAIL)
        adb = fx.make_analytics(os.path.join(t, "a.db"), fx.beacon_events(now))
        pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(now))
        chat_dir = fx.make_chat_tree(os.path.join(t, "chat"), now)
        prom = FakeProm(detail_data(), DETAIL_ALERTS, fail=prom_fail).start()
        self.addCleanup(prom.stop)
        os.environ["FAKE_JOURNAL_MODE"] = journal_mode
        self.addCleanup(os.environ.pop, "FAKE_JOURNAL_MODE", None)
        cfg = state.Config(prom_url=prom.url, registry_path=reg, analytics_db=adb, product_db=pdb,
                           cf_token_file=os.path.join(t, "none"), cf_api_base="http://127.0.0.1:9",
                           chat_dir=chat_dir, journalctl=FAKE_JOURNALCTL)
        httpd = server.make_server(cfg, host="127.0.0.1", port=0, ttl=0.5)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        return httpd.server_address[1]

    def get(self, port, path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, json.loads(body) if body[:1] == b"{" else body

    def test_prometheus_down_gives_unreachable_never_zeros_and_chat_still_works(self):
        port = self.build(prom_fail=True)
        status, perf = self.get(port, "/api/detail/perf?id=cpu")
        self.assertEqual(status, 200)
        self.assertEqual(perf["state"], "unreachable")
        self.assertEqual(perf["series"], [])
        self.assertIsNone(perf["last"])
        status, svc = self.get(port, "/api/detail/service?id=sieve")
        self.assertEqual((status, svc["state"]), (200, "unreachable"))
        self.assertIsNone(svc["restarts_30m"])
        status, chat = self.get(port, "/api/detail/chat?id=kellogg.brengel.com")
        self.assertEqual((status, chat["state"]), (200, "ok"))

    def test_journal_denied_still_serves_the_service_detail(self):
        port = self.build(journal_mode="denied")
        status, svc = self.get(port, "/api/detail/service?id=sieve")
        self.assertEqual(status, 200)
        self.assertEqual(svc["journal"], {"state": "no_permission", "lines": []})

    def test_hostile_unit_name_in_the_registry_gets_no_promql_and_no_journal_call(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t, now = tmp.name, time.time()
        reg = fx.write(os.path.join(t, "r.yaml"),
                       "services:\n  evil:\n    kind: systemd\n    unit: 'x.service --vacuum-size=1M'\n    status: ready\n")
        adb = fx.make_analytics(os.path.join(t, "a.db"), [])
        pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(now))
        log = os.path.join(t, "j.log")
        os.environ["FAKE_JOURNAL_LOG"] = log
        self.addCleanup(os.environ.pop, "FAKE_JOURNAL_LOG", None)
        queries = []
        prom = FakeProm(detail_data(), []).start()
        self.addCleanup(prom.stop)
        orig = prom._answer
        prom._answer = lambda path, q, step=0: (queries.append(q), orig(path, q, step))[1]
        cfg = state.Config(prom_url=prom.url, registry_path=reg, analytics_db=adb, product_db=pdb,
                           cf_token_file=os.path.join(t, "none"), cf_api_base="http://127.0.0.1:9",
                           chat_dir=os.path.join(t, "nochat"), journalctl=FAKE_JOURNALCTL)
        httpd = server.make_server(cfg, host="127.0.0.1", port=0, ttl=0.5)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        status, svc = self.get(httpd.server_address[1], "/api/detail/service?id=evil")
        self.assertEqual(status, 200)
        self.assertEqual(svc["journal"]["state"], "unit_invalid")
        self.assertFalse(os.path.exists(log))
        self.assertFalse([q for q in queries if "vacuum" in q or "evil" in q])


if __name__ == "__main__":
    unittest.main()
