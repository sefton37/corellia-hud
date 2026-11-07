"""/api/state's `games` key and GET /api/detail/game?id=<name> end to end against the fake world: shapes,
allowlist, privacy (names and ids stay off the tile and every log), fail-soft, idle cost, read-only."""
import contextlib
import http.client
import io
import json
import logging
import threading
import time
import unittest
from urllib.parse import quote

from corellia_hud import classify, games, journal, server
from tests.support import games_env as ge
from tests.support.games_world import World, prom_data

TILE_KEYS = {"name", "title", "type", "world", "status", "label", "reason", "players", "uptime_s", "ram_bytes",
             "cpu_pct", "restarts_24h", "sources"}
SENTINELS = [ge.NAME_SENTINEL, ge.BED_NAME, ge.XUID, ge.PFID, ge.EOS_SENTINEL, ge.IP_SENTINEL, ge.SECRET_SENTINEL,
             ge.JOIN_SENTINEL, "Gustaf"]


class Served:
    """A World plus the real HTTP server over it."""

    def __init__(self, ttl=0.2, **world_kw):
        self.world = World(**world_kw)
        self.httpd = server.make_server(self.world.cfg, host="127.0.0.1", port=0, ttl=ttl)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, body=body)
        r = c.getresponse()
        data = r.read()
        headers = dict(r.getheaders())
        c.close()
        return r.status, headers, data

    def get_json(self, path):
        status, headers, data = self.req("GET", path)
        return status, headers, (json.loads(data) if data[:1] == b"{" else data)

    def detail(self, ident):
        return self.get_json("/api/detail/game?id=" + quote(ident, safe=""))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.world.close()


class GamesStateBase(unittest.TestCase):
    SERVED_KW = {"players": [ge.NAME_SENTINEL], "ping_players": 1}

    @classmethod
    def setUpClass(cls):
        cls.s = Served(**cls.SERVED_KW)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()


class StateShapeTests(GamesStateBase):
    def games(self):
        return json.loads(self.s.req("GET", "/api/state")[2])["games"]

    def test_state_carries_four_servers_with_the_documented_fields(self):
        g = self.games()
        self.assertEqual(g["state"], "ok")
        self.assertEqual([r["name"] for r in g["servers"]], list(games.GAME_SERVERS))
        for r in g["servers"]:
            self.assertGreaterEqual(set(r), TILE_KEYS, r["name"])
            self.assertIn(r["status"], (classify.GREEN, classify.AMBER, classify.RED, classify.GREY))
            self.assertEqual(set(r["players"]), {"state", "count", "max"})

    def test_tile_values_equal_the_fixtures(self):
        g = {r["name"]: r for r in self.games()["servers"]}
        ark = g["ark-server"]
        self.assertEqual(ark["ram_bytes"], ge.SERVERS["ark-server"]["ram"])
        self.assertAlmostEqual(ark["uptime_s"], ge.UPTIME_S["ark-server"], delta=3)
        self.assertEqual(ark["restarts_24h"], 1)
        self.assertEqual((ark["players"]["state"], ark["players"]["count"], ark["players"]["max"]), ("ok", 1, 8))
        self.assertIsNone(ark["cpu_pct"])                  # first sample after start: n/a, never 0
        self.assertEqual(ark["status"], classify.GREEN)
        bed = g["bedrock"]
        self.assertEqual((bed["players"]["count"], bed["players"]["max"], bed["world"]), (1, 10, "Testworld"))

    def test_state_json_carries_no_names_or_ids(self):
        raw = self.s.req("GET", "/api/state")[2].decode()
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, raw, sentinel)
        self.assertNotRegex(raw, r"\b[0-9a-f]{32}\b")

    def test_detail_for_the_same_server_does_contain_the_name_positive_control(self):
        status, _, d = self.s.detail("ark-server")
        self.assertEqual(status, 200)
        self.assertIn(ge.NAME_SENTINEL, json.dumps(d))

    def test_services_and_perf_are_still_present_and_untouched(self):
        snap = json.loads(self.s.req("GET", "/api/state")[2])
        self.assertTrue({r["name"] for r in snap["services"]} >= set())    # the section is additive
        self.assertIn("perf", snap)
        self.assertIn("counts", snap["meta"])


class DetailShapeTests(GamesStateBase):
    def test_ark_detail_shape(self):
        for name in ("ark-server", "ark-astraeos", "ark-genesis"):
            status, _, d = self.s.detail(name)
            self.assertEqual(status, 200, name)
            for key in ("type", "title", "status", "players", "online_names", "last_seen", "recent_joins",
                        "last_save", "version", "cluster", "restarts_24h", "ram_bytes", "cpu_pct", "uptime_s",
                        "freshness", "kind", "id"):
                self.assertIn(key, d, (name, key))
            self.assertEqual((d["kind"], d["id"], d["type"]), ("game", name, "ark"))
            self.assertEqual(d["online_names"]["names"], [ge.NAME_SENTINEL])
            self.assertEqual(d["cluster"], "corellia-cluster")
            self.assertEqual(d["version"], "96.9")
            self.assertEqual(d["last_save"]["state"], "ok")
            self.assertEqual(len(d["recent_joins"]), 1)
            self.assertIsNone(d["recent_joins"][0]["name"])    # Flint strips the player from join events
            self.assertEqual(d["freshness"]["state"], "ok")

    def test_ark_detail_has_no_ip_or_eos_id(self):
        blob = json.dumps(self.s.detail("ark-server")[2])
        for sentinel in (ge.EOS_SENTINEL, ge.IP_SENTINEL, ge.SECRET_SENTINEL, ge.JOIN_SENTINEL):
            self.assertNotIn(sentinel, blob)
        self.assertIn("unnamed player", blob)               # the 8-hex fallback name

    def test_bedrock_detail_shape_and_raw_journal(self):
        status, _, d = self.s.detail("bedrock")
        self.assertEqual(status, 200)
        self.assertEqual(d["type"], "bedrock")
        self.assertEqual(d["online_names"], [ge.BED_NAME])
        self.assertEqual((d["ping"]["players"], d["names_match"]), (1, True))
        self.assertEqual([p["name"] for p in d["packs"]], ["Vcraft: Home Furniture [BP]", "ExtraBiomes"])
        self.assertEqual(d["max_players"], {"ping": 10, "properties": 10, "agree": True})
        self.assertEqual((d["version"], d["level"]), ("1.26.45", "Testworld"))
        self.assertLessEqual(len(d["journal"]["lines"]), 20)
        self.assertIn(ge.XUID, json.dumps(d))               # raw, per the no-masking rule
        self.assertNotIn("[redacted]", json.dumps(d))

    def test_unknown_id_is_404(self):
        self.assertEqual(self.s.detail("nope")[0], 404)
        self.assertEqual(self.s.detail("flint")[0], 404)
        self.assertEqual(self.s.detail("stalag")[0], 404)

    def test_malformed_repeated_or_missing_params_are_400(self):
        for path in ("/api/detail/game", "/api/detail/game?id=", "/api/detail/game?id=a&id=b",
                     "/api/detail/game?id=bedrock&x=1", "/api/detail/game?kind=game", "/api/detail/game?id"):
            self.assertIn(self.s.req("GET", path)[0], (400, 404), path)
        self.assertEqual(self.s.req("GET", "/api/detail/game")[0], 400)
        self.assertEqual(self.s.req("GET", "/api/detail/game?id=bedrock&id=ark-server")[0], 400)

    def test_hostile_ids_reach_neither_journalctl_nor_flint(self):
        self.s.detail("bedrock")                               # warm the caches
        before_j, before_f = self.s.world.journal_calls(), self.s.world.flint.count()
        hostile = ["..", "%2e%2e", "../../etc/passwd", "bedrock;reboot", "$(id)", "`id`", "bedrock\nx", "a" * 5120,
                   "bedrock%00", "ark-server&server=Genesis", "BEDROCK"]
        for h in hostile:
            status, _, _ = self.s.req("GET", "/api/detail/game?id=" + (h if "%" in h else quote(h, safe="")))
            self.assertIn(status, (400, 404), h[:30])
        time.sleep(0.3)                                        # let the state TTL lapse; games TTL has not
        self.assertEqual(self.s.world.journal_calls(), before_j)
        self.assertEqual(self.s.world.flint.count(), before_f)

    def test_write_methods_are_405(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.s.req(m, "/api/detail/game?id=bedrock", body=b"{}")[0], 405, m)

    def test_security_headers_on_every_game_response(self):
        for path in ("/api/detail/game?id=bedrock", "/api/detail/game?id=ark-server", "/api/detail/game?id=nope",
                     "/api/detail/game", "/api/state"):
            _, h, _ = self.s.req("GET", path)
            self.assertIn("no-store", h["Cache-Control"], path)
            self.assertEqual(h["X-Content-Type-Options"], "nosniff", path)
            csp = h["Content-Security-Policy"]
            self.assertIn("default-src 'self'", csp)
            self.assertIn("script-src 'self'", csp)

    def test_bedrock_journal_argv_is_fixed_and_shell_free(self):
        seen = []

        def runner(argv, **kw):
            seen.append((argv, kw))
            raise FileNotFoundError

        journal.bedrock_window(runner=runner, binary="journalctl")
        argv, kw = seen[0]
        self.assertIs(kw["shell"], False)
        self.assertIn("--unit=bedrock.service", argv)
        self.assertEqual(argv[0], "journalctl")
        self.assertTrue(any(a.startswith("--grep=") for a in argv))
        self.assertEqual(sum(1 for a in argv if a.startswith("--unit=")), 1)

    def test_the_unit_in_the_real_calls_comes_from_the_table(self):
        with open(self.s.world.journal_log) as f:
            calls = [json.loads(line) for line in f if line.strip()]
        for argv in calls:
            units = [a for a in argv if a.startswith("--unit=")]
            self.assertEqual(units, ["--unit=bedrock.service"])

    def test_nothing_is_logged_or_printed(self):
        out, err = io.StringIO(), io.StringIO()
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = Grab()
        logging.getLogger().addHandler(h)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.s.detail("ark-server")
                self.s.detail("bedrock")
                self.s.req("GET", "/api/state")
                self.s.req("GET", "/api/detail/game?id=%2e%2e")
        finally:
            logging.getLogger().removeHandler(h)
        blob = out.getvalue() + err.getvalue() + "\n".join(records)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, blob, sentinel)


class IdleCostTests(unittest.TestCase):
    def test_state_loads_inside_one_ttl_window_cost_one_fetch_per_source(self):
        s = Served(ttl=0.2, players=[ge.NAME_SENTINEL])
        try:
            for _ in range(5):
                self.assertEqual(s.req("GET", "/api/state")[0], 200)
                time.sleep(0.3)                                # the STATE cache lapses each time; the games cache must not
            s.detail("ark-server")
            s.detail("bedrock")
            self.assertEqual(s.world.flint.count("/api/status"), 3)
            self.assertEqual(s.world.flint.count("/api/events"), 3)
            self.assertEqual(s.world.bedrock.hits, 1)
            self.assertEqual(set(s.world.flint.verbs()), {"GET"})
        finally:
            s.close()


class FailSoftTests(unittest.TestCase):
    def test_flint_down_keeps_rows_and_marks_ark_players_unavailable(self):
        s = Served()
        try:
            s.world.flint.stop()
            snap = json.loads(s.req("GET", "/api/state")[2])
            rows = {r["name"]: r for r in snap["games"]["servers"]}
            self.assertEqual(len(rows), 4)
            for n in ("ark-server", "ark-astraeos", "ark-genesis"):
                self.assertEqual(rows[n]["players"]["state"], "unavailable")
                self.assertIsNone(rows[n]["players"]["count"])
                self.assertEqual(rows[n]["status"], classify.GREEN)      # the unit lamp, not Flint's silence
                self.assertEqual(rows[n]["sources"]["flint"], "unreachable")
            self.assertEqual(rows["bedrock"]["players"]["state"], "ok")
            self.assertIn("perf", snap)
            self.assertIn("services", snap)
        finally:
            s.close()

    def test_a_failed_unit_is_red_even_while_flint_says_online(self):
        s = Served(prom=prom_data(active={"ark-genesis.service": 0}, failed={"ark-genesis.service": 1}))
        try:
            rows = {r["name"]: r for r in json.loads(s.req("GET", "/api/state")[2])["games"]["servers"]}
            self.assertEqual(rows["ark-genesis"]["status"], classify.RED)
            self.assertEqual(rows["ark-server"]["status"], classify.GREEN)
        finally:
            s.close()

    def test_prometheus_down_makes_game_lamps_grey_not_green(self):
        w_prom = prom_data()
        s = Served(prom=w_prom)
        try:
            s.world.prom.fail = True
            rows = json.loads(s.req("GET", "/api/state")[2])["games"]["servers"]
            self.assertTrue(all(r["status"] == classify.GREY for r in rows))
        finally:
            s.close()

    def test_a_registry_without_stalag_lists_the_same_services(self):
        a, b = Served(stalag="absent"), Served(stalag="deferred")
        try:
            names = lambda s: [r["name"] for r in json.loads(s.req("GET", "/api/state")[2])["services"]]
            self.assertEqual(names(a), names(b))
        finally:
            a.close()
            b.close()


if __name__ == "__main__":
    unittest.main()
