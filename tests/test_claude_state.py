"""/api/state's `claude` key and GET /api/detail/claude?id=<id> end to end: shapes, allowlist, privacy (no token,
project, cwd, session id, prompt text or path on the tile), fail-soft, idle cost, and that a usage figure never
touches the health semantics (all_clear, counts, alerts, services)."""
import contextlib
import http.client
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
from urllib.parse import quote

from corellia_hud import claude_limits, claude_usage, classify, server, state
from tests.support import fake_claude as fc

LIMIT_STATES = {
    "ok": [fc.FakeResp(fc.limits_body())], "rate_limited": [fc.http_error(429, {"Retry-After": "120"})],
    "unreachable": [urllib.error.URLError("refused")], "schema_mismatch": [fc.FakeResp(b'{"x": 1}')],
    "needs_reauth": [fc.http_error(401)], "http_error": [fc.http_error(500)],
}


_TZ_OLD = os.environ.get("TZ")


def setUpModule():   # local-calendar arithmetic is under test: pin the zone instead of inheriting the machine's
    os.environ["TZ"] = "America/Chicago"
    time.tzset()


def tearDownModule():
    if _TZ_OLD is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _TZ_OLD
    time.tzset()


class Served:
    def __init__(self, steps=None, tree=True, cred=True, expires_ms=None, gate=None, ttl=0.2):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.root = os.path.join(t, "projects")
        if tree:
            fc.standard_tree(self.root, time.time())
        self.cred = os.path.join(t, "fixture_cred.json")
        if cred:
            fc.write_cred(self.cred, expires_ms=expires_ms)
        self.opener = fc.ScriptedOpener(*(steps or LIMIT_STATES["ok"]), gate=gate)
        self.limits = claude_limits.LimitsCache(self.cred, opener=self.opener)
        self.usage = claude_usage.ClaudeUsageIndex(self.root)
        self.cfg = state.Config(
            prom_url="http://127.0.0.1:9", registry_path=os.path.join(t, "none.yaml"),
            analytics_db=os.path.join(t, "none.db"), product_db=os.path.join(t, "none2.db"),
            cf_token_file=os.path.join(t, "none"), cf_api_base="http://127.0.0.1:9", chat_dir=os.path.join(t, "chat"),
            claude_projects_dir=self.root, claude_cred_path=self.cred)
        self.httpd = server.make_server(self.cfg, "127.0.0.1", 0, ttl=ttl, limits_cache=self.limits,
                                        usage_index=self.usage)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def req(self, method, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path)
        r = c.getresponse()
        data = r.read()
        headers = dict(r.getheaders())
        c.close()
        return r.status, headers, data

    def state(self):
        return json.loads(self.req("GET", "/api/state")[2])

    def detail(self, ident):
        status, headers, data = self.req("GET", "/api/detail/claude?id=" + quote(ident, safe=""))
        return status, headers, (json.loads(data) if data[:1] == b"{" else data)

    def settle(self):
        self.limits.wait_idle(10)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()


class Base(unittest.TestCase):
    STEPS = None

    @classmethod
    def setUpClass(cls):
        cls.s = Served(cls.STEPS)
        cls.s.state()
        cls.s.settle()
        time.sleep(0.25)   # past the 0.2 s state TTL
        cls.snap = cls.s.state()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()


class StateShapeTests(Base):
    def test_state_carries_claude_with_limits_and_usage(self):
        c = self.snap["claude"]
        self.assertEqual(set(c), {"limits", "usage"})
        self.assertEqual(c["limits"]["state"], "ok")
        self.assertEqual(c["usage"]["state"], "ok")

    def test_limits_windows_carry_levels_decided_in_classify(self):
        w = self.snap["claude"]["limits"]["windows"]
        self.assertEqual(w["five_hour"]["pct"], 37.0)
        self.assertEqual(w["five_hour"]["level"], classify.GREEN)
        self.assertEqual(set(w["five_hour"]), {"state", "pct", "left_pct", "resets_at", "resets_in_s", "reset", "level"})

    def test_usage_tile_has_the_documented_keys(self):
        u = self.snap["claude"]["usage"]
        self.assertGreaterEqual(set(u), {"state", "windows", "cache_hit_rate", "burn", "models", "sessions_today",
                                         "coverage_since", "scan"})
        self.assertEqual(u["windows"]["today"]["new"], fc.EXPECT["today_new"])
        self.assertEqual(u["windows"]["d7"]["new"], fc.EXPECT["d7_new"])

    def test_the_tile_bytes_hold_no_secret_project_session_prompt_or_path(self):
        raw = json.dumps(self.snap)
        for needle in (fc.ACCESS_SENTINEL, fc.REFRESH_SENTINEL, fc.PROJECT_SENTINEL, fc.PROMPT_SENTINEL,
                       fc.SESSION_SENTINEL, "sess-a", "/home/kel", self.s.tmp.name, "fixture_cred", fc.SPEND_SENTINEL):
            self.assertNotIn(needle, raw, needle)
        self.assertNotIn("claudeAiOauth", raw)

    def test_positive_control_the_projects_detail_does_carry_the_project(self):
        status, _, d = self.s.detail("projects")
        self.assertEqual(status, 200)
        self.assertIn(fc.PROJECT_SENTINEL, json.dumps(d))

    def test_the_tile_shows_model_names_and_shares(self):
        models = {m["model"]: m["share"] for m in self.snap["claude"]["usage"]["models"]}
        self.assertEqual(set(models), {"claude-sonnet-5-5", "claude-opus-5-5"})
        self.assertAlmostEqual(sum(models.values()), 1.0)

    def test_the_static_page_and_detail_script_never_carry_a_credential_word(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
        for name in ("hud.js", "detail.js", "index.html", "detail.html"):
            with open(os.path.join(root, name)) as f:
                text = f.read()
            for word in ("accessToken", "Bearer", "Authorization"):
                self.assertNotIn(word, text, f"{name} {word}")


class DetailTests(Base):
    def test_every_id_returns_its_documented_shape(self):
        want = {"limits": {"state", "windows", "diag", "thresholds", "age_s", "reason", "retry_in_s"},
                "usage": {"state", "windows", "hourly", "daily", "peak_hour", "longest_session", "burn", "scan",
                          "coverage_since", "coverage_date", "sessions_today", "cache_hit_rate"},
                "models": {"state", "rows", "window", "partial", "scan"},
                "projects": {"state", "rows", "window", "partial", "scan", "projects_total"}}
        for ident, keys in want.items():
            with self.subTest(ident):
                status, headers, d = self.s.detail(ident)
                self.assertEqual(status, 200)
                self.assertGreaterEqual(set(d), keys | {"kind", "id"})
                self.assertEqual((d["kind"], d["id"]), ("claude", ident))

    def test_limits_detail_carries_the_thresholds_and_diag(self):
        d = self.s.detail("limits")[2]
        self.assertEqual(d["thresholds"], {"amber": 80, "red": 95})
        self.assertEqual(d["diag"]["fetch_count"], 1)

    def test_usage_detail_buckets(self):
        d = self.s.detail("usage")[2]
        self.assertEqual((len(d["hourly"]), len(d["daily"])), (24, 30))

    def test_an_unknown_id_is_404(self):
        status, headers, d = self.s.detail("nope")
        self.assertEqual(status, 404)
        self.assertEqual(d["error"], "not found")

    def test_malformed_repeated_and_missing_params_are_400(self):
        for q in ("", "?id=", "?id=usage&id=limits", "?id=usage&x=1", "?x=1", "?id=usage&", "?id"):
            with self.subTest(q):
                status, _, _ = self.s.req("GET", "/api/detail/claude" + q)
                self.assertIn(status, (400, 404), q)
        self.assertEqual(self.s.req("GET", "/api/detail/claude")[0], 400)
        self.assertEqual(self.s.req("GET", "/api/detail/claude?id=usage&id=limits")[0], 400)

    def test_hostile_ids_are_refused(self):
        for ident in ("..", "%2e%2e", "usage%00", "usage;ls", "$(id)", "`id`", "usage\nx", "a" * 5000, "../etc/passwd",
                      "Usage", "limits ", "usage/"):
            with self.subTest(ident[:30]):
                status, _, _ = self.s.req("GET", "/api/detail/claude?id=" + (ident if "%" in ident else quote(ident, safe="")))
                self.assertIn(status, (400, 404))

    def test_other_methods_are_405(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            for path in ("/api/detail/claude?id=usage", "/api/state"):
                self.assertEqual(self.s.req(m, path)[0], 405, (m, path))

    def test_security_headers_on_every_claude_response(self):
        for path in ("/api/detail/claude?id=limits", "/api/detail/claude?id=usage", "/api/detail/claude?id=nope",
                     "/api/detail/claude", "/api/state"):
            status, h, _ = self.s.req("GET", path)
            self.assertIn("default-src 'self'", h["Content-Security-Policy"], path)
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
            self.assertEqual(h["Cache-Control"], "no-store")

    def test_a_detail_open_never_triggers_a_fetch(self):
        s = Served(ttl=60)
        try:
            for ident in ("limits", "usage", "models", "projects"):
                s.detail(ident)
            time.sleep(0.2)
            self.assertEqual(s.opener.count, 0)   # nothing has loaded /api/state yet; the detail path alone starts no fetch
        finally:
            s.close()


class HealthSemanticsTests(unittest.TestCase):
    def test_every_limits_state_leaves_the_health_fields_byte_identical(self):
        outs = {}
        for name, steps in LIMIT_STATES.items():
            s = Served(steps)
            try:
                s.state()
                s.settle()
                time.sleep(0.25)
                snap = s.state()
                self.assertEqual(snap["claude"]["limits"]["state"] in ("ok", "stale") or name != "ok", True)
                outs[name] = json.dumps({k: snap[k] for k in ("alerts", "services", "perf")} | {
                    "all_clear": snap["meta"]["all_clear"], "counts": snap["meta"]["counts"],
                    "banner": snap["meta"]["banner"], "src": snap["meta"]["sources"]}, sort_keys=True)
            finally:
                s.close()
        self.assertEqual(len(set(outs.values())), 1, "claude state changed the health fields")

    def test_a_red_usage_bar_does_not_flip_the_all_clear(self):
        s = Served([fc.FakeResp(fc.limits_body(five=99.0, week=97.0))])
        try:
            s.state()
            s.settle()
            time.sleep(0.25)
            snap = s.state()
            self.assertEqual(snap["claude"]["limits"]["windows"]["five_hour"]["level"], classify.RED)
            self.assertEqual(snap["alerts"], [])
            self.assertNotIn("claude", json.dumps(snap["meta"]))
        finally:
            s.close()

    def test_a_failing_claude_source_does_not_blank_the_other_sources(self):
        s = Served(LIMIT_STATES["unreachable"], tree=False)
        try:
            snap = s.state()
            self.assertEqual(snap["claude"]["usage"]["state"], "missing")
            self.assertIn("services", snap)
            self.assertIn("web", snap)
            self.assertEqual(snap["meta"]["sources"]["prometheus"], "unreachable")
        finally:
            s.close()

    def test_a_missing_projects_dir_is_independent_of_limits(self):
        s = Served(tree=False)
        try:
            s.state()
            s.settle()
            time.sleep(0.25)
            snap = s.state()
            self.assertEqual(snap["claude"]["usage"]["state"], "missing")
            self.assertIsNone(snap["claude"]["usage"]["windows"])
            self.assertEqual(snap["claude"]["limits"]["state"], "ok")
        finally:
            s.close()

    def test_a_blocked_opener_never_blocks_state_and_limits_start_pending(self):
        gate = threading.Event()
        s = Served(gate=gate)
        try:
            t0 = time.monotonic()
            snap = s.state()
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(snap["claude"]["limits"]["state"], "pending")
            self.assertIsNone(snap["claude"]["limits"]["windows"])
            gate.set()
            s.settle()
        finally:
            gate.set()
            s.close()

    def test_five_state_loads_cause_at_most_one_fetch(self):
        s = Served(ttl=0.05)
        try:
            for _ in range(5):
                s.state()
                time.sleep(0.07)
            s.settle()
            self.assertEqual(s.opener.count, 1)
            self.assertEqual(s.detail("limits")[2]["diag"]["fetch_count"], 1)
        finally:
            s.close()

    def test_a_state_built_without_the_caches_has_no_claude_key(self):
        cfg = state.Config(prom_url="http://127.0.0.1:9", registry_path="/nonexistent", analytics_db="/nonexistent",
                           product_db="/nonexistent", cf_token_file="/nonexistent", cf_api_base="http://127.0.0.1:9",
                           chat_dir="/nonexistent")
        self.assertNotIn("claude", state.build_state(cfg))

    def test_no_channel_carries_a_sentinel(self):
        out, err, records = io.StringIO(), io.StringIO(), []
        handler = logging.Handler()
        handler.emit = records.append
        logging.getLogger().addHandler(handler)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                s = Served([fc.http_error(401)])
                try:
                    s.state()
                    s.settle()
                    for ident in ("limits", "usage", "models", "projects"):
                        s.detail(ident)
                finally:
                    s.close()
        finally:
            logging.getLogger().removeHandler(handler)
        text = out.getvalue() + err.getvalue() + "".join(r.getMessage() for r in records)
        for needle in (fc.ACCESS_SENTINEL, fc.REFRESH_SENTINEL, fc.PROMPT_SENTINEL, fc.SESSION_SENTINEL):
            self.assertNotIn(needle, text)


class ClassifyAndConfigTests(unittest.TestCase):
    def test_usage_level_boundaries(self):
        for pct, want in ((0, classify.GREEN), (79.99, classify.GREEN), (80, classify.AMBER),
                          (94.99, classify.AMBER), (95, classify.RED), (100, classify.RED), (250, classify.RED),
                          (None, classify.GREY), (float("nan"), classify.GREY), (float("inf"), classify.GREY),
                          ("x", classify.GREY)):
            self.assertEqual(classify.usage_level(pct), want, pct)

    def test_config_defaults_and_overrides(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("HUD_CLAUDE_PROJECTS", None)
            os.environ.pop("HUD_CLAUDE_CRED", None)
            cfg = state.Config()
            self.assertTrue(cfg.claude_projects_dir.endswith("/.claude/projects"))
            self.assertTrue(cfg.claude_cred_path.startswith(os.path.expanduser("~")))
            os.environ["HUD_CLAUDE_PROJECTS"] = "/x/p"
            self.assertEqual(state.Config().claude_projects_dir, "/x/p")


if __name__ == "__main__":
    unittest.main()
