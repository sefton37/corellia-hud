import json
import os
import tempfile
import time
import unittest
from unittest import mock

from corellia_hud import sources, state
from tests.support import fixtures as fx
from tests.support.fake_prom import FakeProm, healthy_data

KEYS = {"meta", "alerts", "services", "perf", "web"}
ENUM = {"ok", "unreachable", "missing", "stale", "token_needed"}
DEAD = "http://127.0.0.1:9"


class FailSoftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_HEALTHY)
        self.adb = fx.make_analytics(os.path.join(t, "a.db"), fx.beacon_events(time.time()))
        self.pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(time.time()))
        self.notoken = os.path.join(t, "no-such-token")

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **kw):
        base = dict(prom_url=DEAD, registry_path=self.reg, analytics_db=self.adb, product_db=self.pdb,
                    cf_token_file=self.notoken, cf_api_base=DEAD)
        base.update(kw)
        return state.Config(**base)

    def check_shape(self, st):
        json.loads(json.dumps(st))  # serialisable
        self.assertEqual(set(st), KEYS)
        for name, value in st["meta"]["sources"].items():
            self.assertIn(value, ENUM, name)

    def test_bad_prometheus_url(self):
        st = state.build_state(self.cfg())
        self.check_shape(st)
        self.assertEqual(st["meta"]["sources"]["prometheus"], "unreachable")

    def test_every_service_is_grey_never_green_when_prometheus_is_down(self):
        st = state.build_state(self.cfg())
        self.assertEqual(len(st["services"]), 3)
        self.assertEqual({s["status"] for s in st["services"]}, {"grey"})
        self.assertEqual({s["label"] for s in st["services"]}, {"NO DATA"})
        self.assertFalse(st["meta"]["all_clear"])
        self.assertTrue(st["meta"]["banner"])

    def test_missing_registry(self):
        st = state.build_state(self.cfg(registry_path="/nonexistent/registry.yaml"))
        self.check_shape(st)
        self.assertEqual(st["meta"]["sources"]["registry"], "missing")
        self.assertEqual(st["services"], [])
        self.assertFalse(st["meta"]["all_clear"])

    def test_missing_analytics_db(self):
        st = state.build_state(self.cfg(analytics_db="/nonexistent/a.db"))
        self.check_shape(st)
        self.assertEqual(st["meta"]["sources"]["analytics"], "missing")
        self.assertEqual(st["web"]["sites"][0]["beacon"]["state"], "missing")

    def test_missing_product_db(self):
        st = state.build_state(self.cfg(product_db="/nonexistent/p.db"))
        self.check_shape(st)
        self.assertEqual(st["meta"]["sources"]["product_db"], "missing")
        self.assertFalse(st["meta"]["all_clear"])

    def test_no_cloudflare_token(self):
        st = state.build_state(self.cfg())
        self.assertEqual(st["meta"]["sources"]["cloudflare"], "token_needed")
        for site in st["web"]["sites"]:
            self.assertEqual(site["cf"]["state"], "token_needed")
            self.assertNotIn("requests_24h", site["cf"])

    def test_everything_missing_at_once(self):
        st = state.build_state(self.cfg(registry_path="/x/r", analytics_db="/x/a", product_db="/x/p"))
        self.check_shape(st)
        self.assertEqual(st["services"], [])

    def test_wall_time_is_bounded_when_prometheus_hangs(self):
        fake = FakeProm(healthy_data(), delay=15).start()
        try:
            t0 = time.time()
            st = state.build_state(self.cfg(prom_url=fake.url))
            elapsed = time.time() - t0
        finally:
            fake.httpd.shutdown()
        self.assertLess(elapsed, 8.0)
        self.assertEqual(st["meta"]["sources"]["prometheus"], "unreachable")

    def test_one_raising_source_does_not_blank_the_others(self):
        fake = FakeProm(healthy_data()).start()
        try:
            with mock.patch.object(sources, "beacon_stats", side_effect=RuntimeError("boom")):
                st = state.build_state(self.cfg(prom_url=fake.url))
        finally:
            fake.stop()
        self.check_shape(st)
        self.assertEqual({s["status"] for s in st["services"]}, {"green"})
        self.assertEqual(st["meta"]["sources"]["analytics"], "unreachable")
        self.assertIsNotNone(st["perf"]["cpu"]["pct"])

    def test_raising_prometheus_fetch_degrades_to_grey(self):
        with mock.patch.object(sources, "fetch_prometheus", side_effect=RuntimeError("boom")):
            st = state.build_state(self.cfg())
        self.check_shape(st)
        self.assertEqual({s["status"] for s in st["services"]}, {"grey"})

    def test_healthy_everything_is_all_clear(self):
        fake = FakeProm(healthy_data()).start()
        try:
            st = state.build_state(self.cfg(prom_url=fake.url))
        finally:
            fake.stop()
        self.assertEqual(st["meta"]["sources"]["prometheus"], "ok")
        self.assertTrue(st["meta"]["all_clear"])
        self.assertEqual(st["alerts"], [])


if __name__ == "__main__":
    unittest.main()
