import contextlib
import io
import json
import logging
import os
import tempfile
import time
import unittest

from corellia_hud import sources, state
from tests.support import fixtures as fx
from tests.support.fake_cf import FakeCF
from tests.support.fake_prom import FakeProm, healthy_data

SENTINEL = "SENTINEL-cf-9f3a77c1d2b84e60"


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.tokfile = os.path.join(t, "cloudflare.token")
        self.reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_HEALTHY)
        self.adb = fx.make_analytics(os.path.join(t, "a.db"), fx.beacon_events(time.time()))
        self.pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(time.time()))
        self.cf = FakeCF().start()
        self.prom = FakeProm(healthy_data()).start()

    def tearDown(self):
        self.cf.stop()
        self.prom.stop()
        self.tmp.cleanup()

    def put_token(self, text=SENTINEL + "\n", mode=0o600):
        with open(self.tokfile, "w") as f:
            f.write(text)
        os.chmod(self.tokfile, mode)

    def build(self):
        cfg = state.Config(prom_url=self.prom.url, registry_path=self.reg, analytics_db=self.adb,
                           product_db=self.pdb, cf_token_file=self.tokfile, cf_api_base=self.cf.url)
        return state.build_state(cfg)

    def zones(self, st):
        return {z["zone"]: z["state"] for z in st["web"]["cloudflare"]["zones"]}

    def test_no_file_is_token_needed_per_zone(self):
        st = self.build()
        self.assertEqual(self.zones(st), {"brengel.com": "token_needed", "talkingrock.ai": "token_needed"})
        self.assertEqual(self.cf.requests, [])

    def test_empty_file_is_token_needed(self):
        self.put_token("\n")
        self.assertEqual(set(self.zones(self.build()).values()), {"token_needed"})

    def test_wide_mode_is_insecure_and_token_is_not_sent(self):
        for mode in (0o644, 0o640, 0o604):
            self.put_token(mode=mode)
            st = self.build()
            self.assertEqual(set(self.zones(st).values()), {"token_insecure"}, oct(mode))
            self.assertEqual(self.cf.requests, [], oct(mode))

    def test_read_only_owner_mode_is_accepted(self):
        self.put_token(mode=0o400)
        self.assertEqual(set(self.zones(self.build()).values()), {"ok"})

    def test_valid_token_is_sent_as_authorization_header(self):
        self.put_token()
        self.build()
        auths = {r["auth"] for r in self.cf.requests}
        self.assertEqual(auths, {"Bearer " + SENTINEL})

    def test_sentinel_never_leaks_and_the_scan_is_not_vacuous(self):
        self.put_token()
        self.cf.zones["talkingrock.ai"]["http"] = 403  # exercise an error path too
        handler = ListHandler()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                st = self.build()
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        # positive control: the sentinel really was transmitted to the fake endpoint
        self.assertTrue(any(SENTINEL in (r["auth"] or "") for r in self.cf.requests))
        self.assertNotIn(SENTINEL, json.dumps(st))
        self.assertNotIn(SENTINEL, out.getvalue())
        self.assertNotIn(SENTINEL, err.getvalue())
        self.assertNotIn(SENTINEL, "\n".join(handler.lines))
        self.assertNotIn(SENTINEL, json.dumps(self.zones(st)))

    def test_sentinel_not_in_exception_text(self):
        self.put_token()
        with self.assertRaises(Exception) as cm:
            sources.cloudflare_stats([{"host": "h", "zone": "z"}], self.tokfile, "http://127.0.0.1:9", 0, timeout=0.5,
                                     raise_errors=True)
        self.assertNotIn(SENTINEL, str(cm.exception))
        self.assertNotIn(SENTINEL, repr(cm.exception))

    def test_auth_error_for_one_zone_degrades_only_that_zone(self):
        self.put_token()
        self.cf.zones["talkingrock.ai"]["http"] = 403
        st = self.build()
        z = self.zones(st)
        self.assertEqual(z["brengel.com"], "ok")
        self.assertEqual(z["talkingrock.ai"], "cf_error")
        talk = next(s for s in st["web"]["sites"] if s["host"] == "talkingrock.ai")
        self.assertEqual(talk["cf"]["state"], "cf_error")
        kel = next(s for s in st["web"]["sites"] if s["host"] == "kellogg.brengel.com")
        self.assertEqual(kel["cf"]["requests_24h"], 1200)

    def test_graphql_errors_array_is_cf_error_never_zeros(self):
        self.put_token()
        self.cf.graphql_errors = [{"message": "not authorized for that dataset"}]
        st = self.build()
        for s in st["web"]["sites"]:
            self.assertEqual(s["cf"]["state"], "cf_error", s["host"])
            self.assertNotIn("requests_24h", s["cf"])
        self.assertEqual(set(self.zones(st).values()), {"cf_error"})

    def test_graphql_body_is_a_query_never_a_mutation(self):
        self.put_token()
        self.build()
        posts = [r for r in self.cf.requests if r["method"] == "POST"]
        self.assertTrue(posts)
        for r in posts:
            q = json.loads(r["body"])["query"]
            self.assertIn("query", q)
            self.assertNotIn("mutation", q.lower())
        gets = [r for r in self.cf.requests if r["method"] == "GET"]
        self.assertTrue(gets)  # zone lookups are GET

    def test_state_json_has_no_token_field_names(self):
        self.put_token()
        raw = json.dumps(self.build()).lower()
        for word in ("bearer", "authorization", "api_token"):
            self.assertNotIn(word, raw)


if __name__ == "__main__":
    unittest.main()
