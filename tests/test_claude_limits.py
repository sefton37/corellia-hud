"""claude_limits: the one module that opens the OAuth credentials file and calls the one HTTPS host. Everything
here runs against fixture files named fixture_cred.json and a scripted opener: no real token, no network."""
import contextlib
import io
import json
import logging
import math
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from corellia_hud import claude_limits as cl
from tests.support import fake_claude as fc

NOW = time.time()


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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caches = []
        self.addCleanup(self.drain)
        self.cred = os.path.join(self.tmp.name, "fixture_cred.json")
        fc.write_cred(self.cred)
        self.mono = [1000.0]
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)

    def cache(self, *steps, gate=None, ttl=None, cred=None):
        self.opener = fc.ScriptedOpener(*steps, gate=gate)
        c = cl.LimitsCache(cred or self.cred, ttl=ttl or cl.LIMITS_TTL, opener=self.opener, clock=lambda: self.mono[0])
        self.caches.append(c)
        return c

    def drain(self):
        """No worker outlives its test: a leftover thread would race the next test's patches of os.open."""
        for c in self.caches:
            c.wait_idle(10)

    def settle(self, c):
        self.assertTrue(c.wait_idle(10))
        return c.get()

    def fetch(self, *steps, **kw):
        """First get() (starts the worker), wait for it, return the settled snapshot."""
        c = self.cache(*steps, **kw)
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            c.get()
            snap = self.settle(c)
        return c, snap

    def advance(self, secs):
        self.mono[0] += secs


def ok_body(**kw):
    return fc.FakeResp(fc.limits_body(**kw))


class CredentialsTests(Base):
    def test_missing_file_is_no_credentials(self):
        os.remove(self.cred)
        c, s = self.fetch()
        self.assertEqual(s["state"], "no_credentials")
        self.assertEqual(self.opener.count, 0)

    def test_a_group_readable_file_is_cred_insecure_and_the_opener_is_never_called(self):
        os.chmod(self.cred, 0o640)
        c, s = self.fetch()
        self.assertEqual(s["state"], "cred_insecure")
        self.assertEqual(self.opener.count, 0)

    def test_a_world_readable_file_is_cred_insecure(self):
        os.chmod(self.cred, 0o644)
        self.assertEqual(self.fetch()[1]["state"], "cred_insecure")

    def test_a_symlink_is_refused(self):
        real = os.path.join(self.tmp.name, "real.json")
        os.rename(self.cred, real)
        os.symlink(real, self.cred)
        c, s = self.fetch()
        self.assertEqual(s["state"], "cred_insecure")
        self.assertEqual(self.opener.count, 0)

    def test_a_directory_is_refused(self):
        os.remove(self.cred)
        os.mkdir(self.cred)
        self.assertEqual(self.fetch()[1]["state"], "cred_insecure")

    def test_a_file_over_64_kb_is_refused(self):
        fc.write_cred(self.cred, body='{"claudeAiOauth":{"accessToken":"' + "a" * 70000 + '"}}')
        c, s = self.fetch()
        self.assertEqual(s["state"], "cred_malformed")
        self.assertEqual(self.opener.count, 0)

    def test_an_unreadable_file_is_cred_unreadable(self):
        os.chmod(self.cred, 0)
        self.addCleanup(os.chmod, self.cred, 0o600)
        self.assertEqual(self.fetch()[1]["state"], "cred_unreadable")

    def test_malformed_json_is_cred_malformed(self):
        fc.write_cred(self.cred, body="{not json " + fc.ACCESS_SENTINEL)
        c, s = self.fetch()
        self.assertEqual(s["state"], "cred_malformed")
        self.assertNotIn(fc.ACCESS_SENTINEL, json.dumps(s))

    def test_a_missing_access_field_is_cred_malformed(self):
        fc.write_cred(self.cred, body=json.dumps({"claudeAiOauth": {"expiresAt": 1}}))
        self.assertEqual(self.fetch()[1]["state"], "cred_malformed")

    def test_a_missing_top_level_key_is_cred_malformed(self):
        fc.write_cred(self.cred, body=json.dumps({"somethingElse": {}}))
        self.assertEqual(self.fetch()[1]["state"], "cred_malformed")

    def test_an_expired_token_is_token_expired_with_no_request(self):
        fc.write_cred(self.cred, expires_ms=int((time.time() - 60) * 1000))
        c, s = self.fetch()
        self.assertEqual(s["state"], "token_expired")
        self.assertEqual(self.opener.count, 0)

    def test_an_absent_expiry_lets_the_request_proceed(self):
        fc.write_cred(self.cred, expires_ms=False)
        c, s = self.fetch()
        self.assertEqual(s["state"], "ok")
        self.assertEqual(self.opener.count, 1)

    def test_a_token_without_the_profile_scope_is_scope_missing_with_no_request(self):
        fc.write_cred(self.cred, scopes=["user:inference"])
        c, s = self.fetch()
        self.assertEqual(s["state"], "scope_missing")
        self.assertEqual(self.opener.count, 0)

    def test_the_file_is_re_read_on_every_attempt(self):
        c, s = self.fetch()
        first = self.opener.requests[0]["headers"]["authorization"]
        other = "ACC" + "ESS-SECOND-" + "7xQ1"
        fc.write_cred(self.cred, access=other)
        self.advance(cl.LIMITS_TTL + 1)
        c.get()
        self.settle(c)
        second = self.opener.requests[1]["headers"]["authorization"]
        self.assertIn(fc.ACCESS_SENTINEL, first)
        self.assertIn(other, second)

    def test_only_the_documented_keys_are_projected(self):
        state, token, expires, scopes = cl.read_credentials(self.cred, time.time)
        self.assertEqual(state, "ok")
        self.assertEqual(token, fc.ACCESS_SENTINEL)
        self.assertIsInstance(expires, int)
        self.assertEqual(scopes, ["user:inference", "user:profile"])


class RequestTests(Base):
    def test_exactly_one_get_to_the_constant_url(self):
        c, s = self.fetch()
        self.assertEqual(self.opener.count, 1)
        r = self.opener.requests[0]
        self.assertEqual(r["method"], "GET")
        self.assertEqual(r["url"], "https://" + cl.LIMITS_HOST + cl.LIMITS_PATH)
        self.assertIsNone(r["data"])
        self.assertNotIn("?", r["url"])

    def test_the_headers_are_exactly_the_documented_set(self):
        self.fetch()
        h = self.opener.requests[0]["headers"]
        self.assertEqual(set(h), {"authorization", "accept", "user-agent", "anthropic-beta"})
        self.assertEqual(h["authorization"], "Bearer " + fc.ACCESS_SENTINEL)
        self.assertEqual(h["user-agent"], "corellia-hud")
        self.assertNotIn("claude", h["user-agent"].lower())

    def test_the_timeout_is_the_constant(self):
        self.fetch()
        self.assertEqual(self.opener.requests[0]["timeout"], cl.LIMITS_TIMEOUT)
        self.assertLessEqual(cl.LIMITS_TIMEOUT, 4)

    def test_any_other_target_is_refused_before_the_opener_is_touched(self):
        for url in ("http://" + cl.LIMITS_HOST + cl.LIMITS_PATH,
                    "https://evil.example" + cl.LIMITS_PATH,
                    "https://" + cl.LIMITS_HOST + "/api/other",
                    "https://" + cl.LIMITS_HOST + cl.LIMITS_PATH + "?x=1",
                    "https://" + cl.LIMITS_HOST + ".evil.example" + cl.LIMITS_PATH,
                    "https://user@" + cl.LIMITS_HOST + cl.LIMITS_PATH,
                    "https://" + cl.LIMITS_HOST + ":8443" + cl.LIMITS_PATH,
                    "ftp://" + cl.LIMITS_HOST + cl.LIMITS_PATH, ""):
            with self.subTest(url=url):
                opener = fc.ScriptedOpener()
                self.assertEqual(cl._request(opener, url, "x")[0], "refused")
                self.assertEqual(opener.count, 0)

    def test_the_redirect_handler_returns_none(self):
        self.assertIsNone(cl._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.example/"))

    def test_a_3xx_is_a_state_not_a_second_request(self):
        c, s = self.fetch(fc.http_error(302, {"Location": "https://evil.example/"}))
        self.assertEqual(s["state"], "http_error")
        self.assertEqual(self.opener.count, 1)

    def test_the_real_opener_chain_does_not_follow_a_redirect(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/landed")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            cl._build_opener().open(urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/start"), timeout=3)
        self.assertEqual(cm.exception.code, 302)
        cm.exception.close()
        self.assertEqual(hits, ["/start"])

    def test_the_real_opener_verifies_certificates_and_ignores_proxies(self):
        opener = cl._build_opener()
        https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        ctx = https[0]._context
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(all(p.proxies == {} for p in proxies))

    def test_the_kill_switch_disables_with_no_file_open_and_no_request(self):
        c = self.cache()
        with mock.patch.dict(os.environ, {"HUD_CLAUDE_LIMITS": "0"}), mock.patch("os.open") as o:
            c.get()
            s = self.settle(c)
            fetches = c.get(diag=True)["diag"]["fetch_count"]   # still inside the switch: reading it must not start a fetch
        self.assertEqual(s["state"], "disabled")
        o.assert_not_called()
        self.assertEqual(self.opener.count, 0)
        self.assertEqual(fetches, 0)

    def test_the_kill_switch_is_off_unless_it_is_zero(self):
        with mock.patch.dict(os.environ, {"HUD_CLAUDE_LIMITS": "1"}):
            self.assertEqual(self.fetch()[1]["state"], "ok")

    def test_the_default_credentials_path_can_be_overridden_only_by_its_own_variable(self):
        with mock.patch.dict(os.environ, {"HUD_CLAUDE_CRED": "/x/y"}):
            self.assertEqual(cl.default_cred_path(), "/x/y")
        with mock.patch.dict(os.environ):
            os.environ.pop("HUD_CLAUDE_CRED", None)
            self.assertTrue(cl.default_cred_path().startswith(os.path.expanduser("~")))


class SecretHygieneTests(Base):
    def blob(self, c, snap):
        return json.dumps(snap) + repr(snap) + repr(c) + repr(vars(c)) + repr(c.get(diag=True))

    def test_the_access_sentinel_is_only_in_the_authorization_header(self):
        c, s = self.fetch()
        self.assertIn(fc.ACCESS_SENTINEL, self.opener.requests[0]["headers"]["authorization"])   # positive control
        for other_key in ("accept", "user-agent", "anthropic-beta"):
            self.assertNotIn(fc.ACCESS_SENTINEL, self.opener.requests[0]["headers"][other_key])
        self.assertNotIn(fc.ACCESS_SENTINEL, self.blob(c, s))

    def test_the_refresh_sentinel_appears_nowhere(self):
        c, s = self.fetch()
        self.assertNotIn(fc.REFRESH_SENTINEL, json.dumps(self.opener.requests))
        self.assertNotIn(fc.REFRESH_SENTINEL, self.blob(c, s))
        self.assertNotIn(fc.REFRESH_SENTINEL, self.stdout.getvalue() + self.stderr.getvalue())
        self.assertNotIn(fc.REFRESH_SENTINEL, "".join(r.getMessage() for r in self.records))

    def test_no_channel_carries_either_secret_after_each_provoked_failure(self):
        cases = {
            "401": fc.http_error(401), "403": fc.http_error(403), "429": fc.http_error(429, {"Retry-After": "5"}),
            "500": fc.http_error(500), "malformed": fc.FakeResp(b"{nope " + fc.ACCESS_SENTINEL.encode()),
            "tls": urllib.error.URLError(ssl.SSLCertVerificationError(fc.ACCESS_SENTINEL)),
            "oserror": OSError(fc.ACCESS_SENTINEL), "timeout": TimeoutError(fc.REFRESH_SENTINEL),
        }
        for label, step in cases.items():
            with self.subTest(label):
                self.setUp()
                c, s = self.fetch(step)
                text = (self.blob(c, s) + self.stdout.getvalue() + self.stderr.getvalue()
                        + "".join(r.getMessage() for r in self.records))
                self.assertNotIn(fc.ACCESS_SENTINEL, text)
                self.assertNotIn(fc.REFRESH_SENTINEL, text)

    def test_a_credentials_file_whose_body_holds_the_sentinel_leaks_nothing(self):
        fc.write_cred(self.cred, body="garbage " + fc.ACCESS_SENTINEL + fc.REFRESH_SENTINEL)
        c, s = self.fetch()
        text = self.blob(c, s) + self.stdout.getvalue() + self.stderr.getvalue()
        self.assertNotIn(fc.ACCESS_SENTINEL, text)
        self.assertNotIn(fc.REFRESH_SENTINEL, text)

    def test_the_cache_holds_no_token_attribute(self):
        c, s = self.fetch()
        for name, value in vars(c).items():
            self.assertNotIn(fc.ACCESS_SENTINEL, repr(value), name)
        for name in dir(cl):
            self.assertNotIn(fc.ACCESS_SENTINEL, repr(getattr(cl, name, "")), name)


class ResponseTests(Base):
    def test_a_200_with_both_windows(self):
        c, s = self.fetch(ok_body(five=37.0, week=12.5))
        five, week = s["windows"]["five_hour"], s["windows"]["seven_day"]
        self.assertEqual((s["state"], five["state"], week["state"]), ("ok", "ok", "ok"))
        self.assertEqual((five["pct"], five["left_pct"]), (37.0, 63.0))
        self.assertEqual((week["pct"], week["left_pct"]), (12.5, 87.5))
        self.assertEqual(five["reset"], "known")
        self.assertAlmostEqual(five["resets_in_s"], 2 * 3600 + 600, delta=30)

    def test_null_utilization_is_none_not_zero(self):
        body = json.dumps({"five_hour": {"utilization": None, "resets_at": None},
                           "seven_day": {"utilization": 5, "resets_at": None}}).encode()
        c, s = self.fetch(fc.FakeResp(body))
        self.assertEqual(s["windows"]["five_hour"]["state"], "none")
        self.assertIsNone(s["windows"]["five_hour"]["pct"])

    def test_a_null_window_object_is_none_for_the_main_windows_and_hidden_for_the_rest(self):
        # OBSERVED live 2026-11-06: five_hour was a JSON null while seven_day carried a value
        body = json.dumps({"five_hour": None, "seven_day": {"utilization": 7.0, "resets_at": "2026-11-10T19:00:00.219919+00:00"},
                           "seven_day_opus": None, "seven_day_sonnet": None}).encode()
        c, s = self.fetch(fc.FakeResp(body))
        self.assertEqual(s["state"], "ok")
        self.assertEqual(set(s["windows"]), {"five_hour", "seven_day"})
        self.assertEqual(s["windows"]["five_hour"]["state"], "none")
        self.assertIsNone(s["windows"]["five_hour"]["pct"])

    def test_the_observed_resets_at_shape_parses(self):
        state, windows = cl.parse_limits({"five_hour": None, "seven_day": {"utilization": 7.0, "resets_at": "2026-11-10T19:00:00.219919+00:00"}},
                                         time.mktime((2026, 11, 6, 12, 0, 0, 0, 0, -1)))
        self.assertEqual(windows["seven_day"]["state"], "ok")
        self.assertEqual(windows["seven_day"]["reset"], "known")

    def test_a_past_reset_withholds_the_value(self):
        c, s = self.fetch(ok_body(five=88.0, five_reset=time.time() - 60))
        w = s["windows"]["five_hour"]
        self.assertEqual(w["state"], "stale_window")
        self.assertIsNone(w["pct"])
        self.assertIsNone(w["left_pct"])

    def test_an_unparseable_reset_shows_the_value_with_reset_unknown(self):
        body = json.dumps({"five_hour": {"utilization": 41, "resets_at": "next tuesday"},
                           "seven_day": {"utilization": 5, "resets_at": 12}}).encode()
        c, s = self.fetch(fc.FakeResp(body))
        w = s["windows"]["five_hour"]
        self.assertEqual((w["state"], w["pct"], w["reset"], w["resets_at"]), ("ok", 41.0, "unknown", None))
        self.assertEqual(s["windows"]["seven_day"]["reset"], "unknown")

    def test_nonsense_utilization_is_bad_value(self):
        for label, value in (("nan", float("nan")), ("inf", float("inf")), ("negative", -3), ("bool", True),
                             ("string", "50"), ("list", [1]), ("huge", 1e9)):
            with self.subTest(label):
                obj = {"five_hour": {"utilization": value, "resets_at": None}, "seven_day": {"utilization": 1, "resets_at": None}}
                state, windows = cl.parse_limits(obj, NOW)
                self.assertEqual(state, "ok")
                self.assertEqual(windows["five_hour"]["state"], "bad_value", label)
                self.assertIsNone(windows["five_hour"]["pct"])

    def test_a_window_that_is_not_an_object_is_bad_value(self):
        state, windows = cl.parse_limits({"five_hour": 5, "seven_day": {"utilization": 1, "resets_at": None}}, NOW)
        self.assertEqual(windows["five_hour"]["state"], "bad_value")

    def test_the_scale_constant_is_applied_in_one_place(self):
        with mock.patch.object(cl, "UTIL_SCALE", 100.0):
            state, windows = cl.parse_limits({"five_hour": {"utilization": 0.42, "resets_at": None},
                                              "seven_day": {"utilization": 0.1, "resets_at": None}}, NOW)
        self.assertAlmostEqual(windows["five_hour"]["pct"], 42.0)
        self.assertAlmostEqual(windows["seven_day"]["pct"], 10.0)

    def test_unknown_keys_are_ignored_and_absent_optional_windows_are_not_shown(self):
        c, s = self.fetch(ok_body(extra={"seven_day_oauth_apps": {"utilization": 9}, "brand_new": [1]}))
        self.assertEqual(set(s["windows"]), {"five_hour", "seven_day"})

    def test_the_per_model_weekly_windows_show_when_present(self):
        c, s = self.fetch(ok_body(opus=20.0, sonnet=30.0))
        self.assertEqual(set(s["windows"]), {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"})
        self.assertEqual(s["windows"]["seven_day_opus"]["pct"], 20.0)

    def test_extra_usage_and_limits_content_is_never_read(self):
        class Strict(dict):
            def get(self, key, default=None):
                if key in ("extra_usage", "limits"):
                    raise AssertionError("spend field read: " + key)
                return super().get(key, default)

            def __getitem__(self, key):
                if key in ("extra_usage", "limits"):
                    raise AssertionError("spend field read: " + key)
                return super().__getitem__(key)
        obj = Strict(json.loads(fc.limits_body()))
        cl.parse_limits(obj, NOW)
        c, s = self.fetch(ok_body())
        self.assertNotIn(fc.SPEND_SENTINEL, json.dumps(s) + repr(vars(c)))

    def test_non_dict_json_is_schema_mismatch(self):
        self.assertEqual(self.fetch(fc.FakeResp(b"[1,2]"))[1]["state"], "schema_mismatch")

    def test_an_object_with_none_of_the_windows_is_schema_mismatch(self):
        self.assertEqual(self.fetch(fc.FakeResp(b'{"unexpected": 1}'))[1]["state"], "schema_mismatch")

    def test_invalid_json_is_bad_json(self):
        self.assertEqual(self.fetch(fc.FakeResp(b"<html>nope</html>"))[1]["state"], "bad_json")

    def test_a_body_over_64_kb_is_too_large(self):
        self.assertEqual(self.fetch(fc.FakeResp(b"{" + b" " * 70000 + b"}"))[1]["state"], "too_large")

    def test_a_non_200_success_status_is_http_error(self):
        self.assertEqual(self.fetch(fc.FakeResp(b"{}", status=204))[1]["state"], "http_error")


class ErrorTests(Base):
    def test_401_is_needs_reauth(self):
        self.assertEqual(self.fetch(fc.http_error(401))[1]["state"], "needs_reauth")

    def test_403_is_scope_missing(self):
        self.assertEqual(self.fetch(fc.http_error(403))[1]["state"], "scope_missing")

    def test_404_and_5xx_are_http_error(self):
        for code in (404, 500, 502, 418):
            with self.subTest(code):
                self.setUp()
                self.assertEqual(self.fetch(fc.http_error(code))[1]["state"], "http_error")

    def test_429_honours_an_integer_retry_after(self):
        c, s = self.fetch(fc.http_error(429, {"Retry-After": "120"}))
        self.assertEqual(s["state"], "rate_limited")
        self.assertEqual(s["retry_in_s"], 120)

    def test_429_caps_an_absurd_retry_after_at_one_hour(self):
        c, s = self.fetch(fc.http_error(429, {"Retry-After": "99999999"}))
        self.assertEqual(s["retry_in_s"], 3600)

    def test_429_with_a_negative_or_absent_or_unparseable_retry_after_uses_the_ladder(self):
        for hdr in ({"Retry-After": "-5"}, {}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}):
            with self.subTest(hdr):
                self.setUp()
                c, s = self.fetch(fc.http_error(429, hdr))
                self.assertEqual(s["state"], "rate_limited")
                self.assertEqual(s["retry_in_s"], 60)

    def test_timeouts_are_timeout(self):
        for exc in (TimeoutError(), urllib.error.URLError(TimeoutError()), socket.timeout()):
            with self.subTest(repr(exc)):
                self.setUp()
                self.assertEqual(self.fetch(exc)[1]["state"], "timeout")

    def test_refused_dns_and_tls_failures_are_unreachable(self):
        for exc in (urllib.error.URLError(ConnectionRefusedError()), urllib.error.URLError(socket.gaierror(-2, "x")),
                    urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")), ConnectionResetError(), OSError("x")):
            with self.subTest(repr(exc)):
                self.setUp()
                self.assertEqual(self.fetch(exc)[1]["state"], "unreachable")

    def test_an_unexpected_exception_still_never_raises(self):
        c, s = self.fetch(RuntimeError("boom"))
        self.assertIn(s["state"], cl.STATES)

    def test_every_state_the_cache_can_emit_is_documented(self):
        for st in ("ok", "stale", "pending", "disabled", "no_credentials", "cred_insecure", "cred_unreadable",
                   "cred_malformed", "token_expired", "needs_reauth", "scope_missing", "rate_limited", "http_error",
                   "timeout", "unreachable", "bad_json", "too_large", "schema_mismatch"):
            self.assertIn(st, cl.STATES)


class CacheTests(Base):
    def test_the_constants(self):
        self.assertGreaterEqual(cl.LIMITS_TTL, 120)
        self.assertEqual(cl.LIMITS_HOST, "api.anthropic.com")
        self.assertEqual(cl.LIMITS_PATH, "/api/oauth/usage")
        self.assertLessEqual(cl.LIMITS_MAX_BODY, 65536)

    def test_a_ttl_below_the_floor_is_raised_to_it(self):
        c = cl.LimitsCache(self.cred, ttl=1, opener=fc.ScriptedOpener(), clock=lambda: self.mono[0])
        self.caches.append(c)
        self.assertGreaterEqual(c.get(diag=True)["diag"]["ttl_s"], 120)

    def test_one_request_per_ttl_window(self):
        c, s = self.fetch()
        for _ in range(10):   # ten polls, 200 s: still inside the TTL
            self.advance(20)
            c.get()
            self.settle(c)
        self.assertEqual(self.opener.count, 1)
        self.advance(cl.LIMITS_TTL)
        c.get()
        self.settle(c)
        self.assertEqual(self.opener.count, 2)

    def test_fifty_concurrent_gets_cause_one_request(self):
        c = self.cache(gate=None)
        out = []
        barrier = threading.Barrier(50)

        def go():
            barrier.wait()
            out.append(c.get()["state"])
        threads = [threading.Thread(target=go) for _ in range(50)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.settle(c)
        self.assertEqual(self.opener.count, 1)
        self.assertEqual(len(out), 50)

    def test_get_returns_at_once_while_the_opener_blocks_and_starts_pending(self):
        gate = threading.Event()
        c = self.cache(gate=gate)
        t0 = time.monotonic()
        s = c.get()
        self.assertLess(time.monotonic() - t0, 0.05)
        self.assertEqual(s["state"], "pending")
        self.assertIsNone(s["windows"])
        t0 = time.monotonic()
        c.get()
        self.assertLess(time.monotonic() - t0, 0.05)
        gate.set()
        self.assertEqual(self.settle(c)["state"], "ok")

    def test_last_good_is_kept_as_stale_with_its_age_for_two_ttls_then_withheld(self):
        c, s = self.fetch(ok_body(), urllib.error.URLError("down"))
        self.assertEqual(s["state"], "ok")
        self.advance(cl.LIMITS_TTL + 1)
        c.get()
        s = self.settle(c)
        self.assertEqual(s["state"], "stale")
        self.assertEqual(s["reason"], "unreachable")
        self.assertAlmostEqual(s["age_s"], cl.LIMITS_TTL + 1, delta=2)
        self.assertEqual(s["windows"]["five_hour"]["state"], "ok")
        self.advance(cl.LIMITS_TTL + 5)   # now older than two TTLs
        s = c.get()
        self.assertEqual(s["state"], "unreachable")
        self.assertIsNone(s["windows"])

    def test_a_failure_never_blanks_a_still_valid_value_early(self):
        c, s = self.fetch(ok_body(five=55.0), http_error_429())
        self.advance(cl.LIMITS_TTL + 1)
        c.get()
        s = self.settle(c)
        self.assertEqual(s["windows"]["five_hour"]["pct"], 55.0)
        self.assertEqual(s["state"], "stale")

    def test_the_backoff_ladder(self):
        c = self.cache(urllib.error.URLError("down"))
        c.get()
        self.settle(c)
        for gap in (60, 120, 300, 900, 900):
            before = self.opener.count
            self.advance(gap - 1)
            c.get()
            self.settle(c)
            self.assertEqual(self.opener.count, before, f"too early at gap {gap}")
            self.advance(1)
            c.get()
            self.settle(c)
            self.assertEqual(self.opener.count, before + 1, f"no retry at gap {gap}")

    def test_a_success_resets_the_ladder(self):
        c = self.cache(urllib.error.URLError("down"), ok_body(), urllib.error.URLError("down"))
        c.get()
        self.settle(c)
        self.advance(60)
        c.get()
        self.settle(c)   # success
        self.advance(cl.LIMITS_TTL)
        c.get()
        self.settle(c)   # failure again: the first rung (60 s), not the second
        before = self.opener.count
        self.advance(61)
        c.get()
        self.settle(c)
        self.assertEqual(self.opener.count, before + 1)

    def test_no_request_after_a_401_until_the_file_changes_or_fifteen_minutes(self):
        c, s = self.fetch(fc.http_error(401))
        self.assertEqual(s["state"], "needs_reauth")
        for _ in range(20):
            self.advance(40)
            c.get()
            self.settle(c)
        self.assertEqual(self.opener.count, 1)   # 800 s elapsed, still blocked
        self.advance(101)
        c.get()
        self.settle(c)
        self.assertEqual(self.opener.count, 2)   # 15 minutes passed

    def test_a_changed_credentials_file_unblocks_a_401_at_once(self):
        c, s = self.fetch(fc.http_error(401), ok_body())
        self.advance(5)
        fc.write_cred(self.cred, access="ACC" + "ESS-FRESH-" + "5pR2")
        st = os.stat(self.cred)
        os.utime(self.cred, (st.st_atime, st.st_mtime + 10))
        c.get()
        self.assertEqual(self.settle(c)["state"], "ok")
        self.assertEqual(self.opener.count, 2)

    def test_recovery_after_the_token_is_refreshed_by_claude_code(self):
        fc.write_cred(self.cred, expires_ms=int((time.time() - 60) * 1000))
        c, s = self.fetch()
        self.assertEqual(s["state"], "token_expired")
        fc.write_cred(self.cred, expires_ms=int((time.time() + 3600) * 1000))
        st = os.stat(self.cred)
        os.utime(self.cred, (st.st_atime, st.st_mtime + 10))
        c.get()
        self.assertEqual(self.settle(c)["state"], "ok")

    def test_diag_counts_outbound_fetches(self):
        c, s = self.fetch()
        d = c.get(diag=True)["diag"]
        self.assertEqual(d["fetch_count"], 1)
        self.assertNotIn("diag", c.get())

    def test_window_staleness_is_judged_on_every_read(self):
        wall = [time.time()]
        opener = fc.ScriptedOpener(ok_body(five=10.0, five_reset=wall[0] + 120))
        c = cl.LimitsCache(self.cred, opener=opener, clock=lambda: self.mono[0], wall=lambda: wall[0])
        self.caches.append(c)
        c.get()
        c.wait_idle(5)
        self.assertEqual(c.get()["windows"]["five_hour"]["state"], "ok")
        wall[0] += 200
        self.assertEqual(c.get()["windows"]["five_hour"]["state"], "stale_window")


def http_error_429():
    return fc.http_error(429, {"Retry-After": "60"})


if __name__ == "__main__":
    unittest.main()
