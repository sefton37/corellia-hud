import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

from corellia_hud import sources, state
from tests.support import fixtures as fx
from tests.support.fake_cf import FakeCF
from tests.support.fake_prom import FakeProm, healthy_data

NOW = 1_800_000_000.0
LIVE_ANALYTICS = os.path.expanduser("~/data/analytics.db")
LIVE_REGISTRY = os.path.expanduser("~/.claude/ops/registry.yaml")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = fx.write(os.path.join(self.tmp.name, "reg.yaml"), fx.REGISTRY_UNIT)

    def tearDown(self):
        self.tmp.cleanup()

    def names(self):
        return [s["name"] for s in sources.load_services(self.reg)]

    def test_retired_deferred_and_non_hosted_are_excluded(self):
        n = self.names()
        for gone in ("old-thing", "later-thing", "phone-thing", "game-thing", "corellia-hud"):
            self.assertNotIn(gone, n)

    def test_synthetic_service_appears_so_list_is_not_hardcoded(self):
        self.assertIn("zz-test", self.names())

    def test_stub_and_portless_services_are_included(self):
        n = self.names()
        self.assertIn("stubby", n)
        self.assertIn("rogue-ish", n)
        self.assertEqual(sorted(n), sorted(["alpha", "beta", "gamma", "zz-test", "stubby", "rogue-ish"]))

    def test_folded_notes_and_inline_comments_are_not_parsed_as_fields(self):
        gamma = next(s for s in sources.load_services(self.reg) if s["name"] == "gamma")
        self.assertEqual(gamma["kind"], "systemd-user")
        self.assertIsNone(gamma["port"])
        self.assertEqual(gamma["unit"], "gamma.service")

    def test_ports_are_ints(self):
        alpha = next(s for s in sources.load_services(self.reg) if s["name"] == "alpha")
        self.assertEqual(alpha["port"], 5000)

    def test_missing_registry_raises_for_the_caller_to_degrade(self):
        with self.assertRaises(OSError):
            sources.load_services(os.path.join(self.tmp.name, "nope.yaml"))

    @unittest.skipUnless(os.path.exists(LIVE_REGISTRY), "no live registry")
    def test_live_registry_parses_to_a_plausible_set(self):
        rows = sources.load_services(LIVE_REGISTRY)
        self.assertGreaterEqual(len(rows), 20)
        self.assertTrue(all(r["kind"] for r in rows))
        self.assertNotIn("corellia-hud", [r["name"] for r in rows])


class PrometheusTests(unittest.TestCase):
    def test_instant_vectors_parse_into_the_view(self):
        fake = FakeProm(healthy_data()).start()
        try:
            r = sources.fetch_prometheus(fake.url, now=NOW)
        finally:
            fake.stop()
        self.assertEqual(r["state"], "ok")
        v = r["view"]
        self.assertTrue(v.ok)
        self.assertEqual(v.port_up, {"alpha": 1.0, "beta": 1.0})
        self.assertEqual(v.unit_active[("user", "gamma.service")], 1.0)
        self.assertEqual(v.collector_age, 30.0)
        self.assertEqual(r["perf"]["cpu"]["pct"], 12.5)
        self.assertEqual(len(r["perf"]["cpu"]["spark"]), 60)

    def test_only_get_is_issued_to_prometheus(self):
        fake = FakeProm(healthy_data()).start()
        try:
            sources.fetch_prometheus(fake.url, now=NOW)
            methods = {m for m, _ in fake.requests}
        finally:
            fake.stop()
        self.assertEqual(methods, {"GET"})
        self.assertGreater(len(fake.requests), 5)

    def test_stale_collector_marks_source_stale(self):
        d = healthy_data()
        d["collector_age"] = [({}, 4000)]
        fake = FakeProm(d).start()
        try:
            r = sources.fetch_prometheus(fake.url, now=NOW)
        finally:
            fake.stop()
        self.assertEqual(r["state"], "stale")

    def test_unreachable_prometheus_gives_not_ok_view(self):
        r = sources.fetch_prometheus("http://127.0.0.1:9", now=NOW, timeout=1.0)
        self.assertEqual(r["state"], "unreachable")
        self.assertFalse(r["view"].ok)

    def test_firing_alerts_are_normalised(self):
        alerts = [{"labels": {"alertname": "DiskLow", "severity": "warning"},
                   "annotations": {"summary": "disk low"}, "state": "firing"},
                  {"labels": {"alertname": "Pending1"}, "annotations": {}, "state": "pending"}]
        fake = FakeProm(healthy_data(), alerts=alerts).start()
        try:
            r = sources.fetch_prometheus(fake.url, now=NOW)
        finally:
            fake.stop()
        self.assertEqual([a["name"] for a in r["alerts"]], ["DiskLow"])
        self.assertEqual(r["alerts"][0]["source"], "prometheus")


class BeaconTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = fx.make_analytics(os.path.join(self.tmp.name, "a.db"), fx.beacon_events(NOW))

    def tearDown(self):
        self.tmp.cleanup()

    def test_visitors_and_delta(self):
        b = sources.beacon_stats(self.db, "kellogg", NOW)
        self.assertEqual(b["state"], "ok")
        self.assertEqual((b["visitors_24h"], b["visitors_7d"]), (15, 25))
        self.assertEqual(b["delta_24h"], {"pct": 50})

    def test_small_sample_delta_shows_prior_count_not_percent(self):
        # 0 visitors against 1 is not "-100%": below MIN_DELTA_SAMPLE the prior raw count is shown.
        self.assertEqual(sources.delta(0, 1), {"label": "was 1"})
        self.assertEqual(sources.delta(9, 3), {"label": "was 3"})
        self.assertEqual(sources.delta(15, 10), {"pct": 50})   # at the threshold: a real percentage
        self.assertEqual(sources.delta(2, 40), {"pct": -95})   # a collapse from a real base still shows

    def test_delta_suppressed_as_new_when_prior_period_is_zero(self):
        b = sources.beacon_stats(self.db, "talkingrock", NOW)
        self.assertEqual((b["visitors_24h"], b["visitors_7d"]), (2, 4))  # pageexit event not counted
        self.assertEqual(b["delta_24h"], {"label": "new"})
        self.assertEqual(b["delta_7d"], {"label": "new"})

    def test_site_without_rows_is_zero_not_missing(self):
        b = sources.beacon_stats(self.db, "nosuch", NOW)
        self.assertEqual((b["state"], b["visitors_24h"]), ("ok", 0))
        self.assertIsNone(b["delta_24h"])

    def test_missing_db_is_explicit(self):
        b = sources.beacon_stats(os.path.join(self.tmp.name, "gone.db"), "kellogg", NOW)
        self.assertEqual(b["state"], "missing")

    @unittest.skipUnless(os.path.exists(LIVE_ANALYTICS), "no live analytics db")
    def test_fixture_schema_matches_the_real_schema(self):
        con = sqlite3.connect(f"file:{LIVE_ANALYTICS}?mode=ro", uri=True)
        live = {r[1] for r in con.execute("PRAGMA table_info(page_events)")}
        con.close()
        con = sqlite3.connect(self.db)
        fixture = {r[1] for r in con.execute("PRAGMA table_info(page_events)")}
        con.close()
        self.assertEqual(live, fixture)

    @unittest.skipUnless(os.path.exists(LIVE_ANALYTICS), "no live analytics db")
    def test_beacon_reads_the_real_database(self):
        b = sources.beacon_stats(LIVE_ANALYTICS, "talkingrock", NOW)
        self.assertEqual(b["state"], "ok")
        self.assertIsInstance(b["visitors_7d"], int)


class HealthAndAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "p.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_latest_batch_failures_only(self):
        fx.make_product(self.path, [
            ("hc-old", "fail", NOW - 86400), ("hc-a", "fail", NOW - 60), ("hc-b", "pass", NOW - 60),
            ("hc-c", "fail", NOW - 90)])
        st, alerts = sources.health_alerts(self.path, NOW)
        self.assertEqual(st, "ok")
        self.assertEqual(sorted(a["name"] for a in alerts), ["health:hc-a", "health:hc-c"])

    def test_last_run_older_than_nine_days_alerts(self):
        fx.make_product(self.path, [("hc-a", "pass", NOW - 10 * 86400)])
        st, alerts = sources.health_alerts(self.path, NOW)
        self.assertEqual([a["name"] for a in alerts], ["HealthChecksStale"])

    def test_all_passing_recent_is_quiet(self):
        fx.make_product(self.path, fx.healthy_product_runs(NOW))
        st, alerts = sources.health_alerts(self.path, NOW)
        self.assertEqual((st, alerts), ("ok", []))

    def test_missing_product_db_is_explicit(self):
        st, alerts = sources.health_alerts(os.path.join(self.tmp.name, "none.db"), NOW)
        self.assertEqual(st, "missing")
        self.assertEqual(alerts, [])

    def test_disk_alert_threshold(self):
        self.assertEqual(sources.disk_alert(91.0)["name"], "DiskLow")
        self.assertIsNone(sources.disk_alert(80.0))
        self.assertIsNone(sources.disk_alert(None))

    def test_union_dedupes_by_name(self):
        prom = [{"name": "DiskLow", "severity": "warning", "source": "prometheus", "summary": "p"}]
        local = [sources.disk_alert(95.0), {"name": "health:x", "severity": "warning", "source": "health", "summary": "x"}]
        merged = sources.merge_alerts(prom, local)
        self.assertEqual(sorted(a["name"] for a in merged), ["DiskLow", "health:x"])
        self.assertEqual(next(a for a in merged if a["name"] == "DiskLow")["source"], "prometheus")


class CloudflareTests(unittest.TestCase):
    SITES = [
        {"host": "kellogg.brengel.com", "zone": "brengel.com"},
        {"host": "rogueroutine.brengel.com", "zone": "brengel.com"},
        {"host": "talkingrock.ai", "zone": "talkingrock.ai"},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tok = os.path.join(self.tmp.name, "cloudflare.token")
        with open(self.tok, "w") as f:
            f.write("sentinel-token-value\n")
        os.chmod(self.tok, 0o600)
        self.cf = FakeCF().start()

    def tearDown(self):
        self.cf.stop()
        self.tmp.cleanup()

    def run_cf(self):
        return sources.cloudflare_stats(self.SITES, self.tok, self.cf.url, NOW, timeout=2.0)

    def test_requests_and_threats_for_both_windows(self):
        r = self.run_cf()
        k = r["sites"]["kellogg.brengel.com"]
        self.assertEqual(k["state"], "ok")
        self.assertEqual((k["requests_24h"], k["requests_7d"]), (1200, 8400))
        self.assertEqual((k["threats_24h"], k["threats_7d"]), (14, 90))
        self.assertEqual(k["delta_24h"], {"pct": 20})

    def test_zero_prior_period_is_new_not_infinite(self):
        t = self.run_cf()["sites"]["talkingrock.ai"]
        self.assertEqual(t["delta_24h"], {"label": "new"})

    def test_hostname_filter_for_the_brengel_zone(self):
        self.run_cf()
        posts = [json.loads(r["body"]) for r in self.cf.requests if r["method"] == "POST"]
        hosts = sorted(p["variables"]["host"] for p in posts if p["variables"]["zone"] == "zone-brengel")
        self.assertEqual(hosts, ["kellogg.brengel.com", "rogueroutine.brengel.com"])
        talking = [p["variables"]["host"] for p in posts if p["variables"]["zone"] == "zone-talkingrock"]
        self.assertEqual(talking, ["talkingrock.ai"])
        self.assertIn("clientRequestHTTPHost", posts[0]["query"])


class StateSourceTests(unittest.TestCase):
    def test_state_build_never_writes_to_fixture_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = fx.write(os.path.join(tmp, "r.yaml"), fx.REGISTRY_E2E_HEALTHY)
            adb = fx.make_analytics(os.path.join(tmp, "a.db"), fx.beacon_events(NOW))
            pdb = fx.make_product(os.path.join(tmp, "p.db"), fx.healthy_product_runs(NOW))
            before = (sha(adb), sha(pdb), sha(reg))
            fake = FakeProm(healthy_data()).start()
            try:
                cfg = state.Config(prom_url=fake.url, registry_path=reg, analytics_db=adb, product_db=pdb,
                                   cf_token_file=os.path.join(tmp, "none"), cf_api_base="http://127.0.0.1:9")
                st = state.build_state(cfg, now=NOW)
            finally:
                fake.stop()
            self.assertEqual((sha(adb), sha(pdb), sha(reg)), before)
            self.assertEqual({s["status"] for s in st["services"]}, {"green"})


if __name__ == "__main__":
    unittest.main()
