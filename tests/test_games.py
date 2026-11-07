"""The Games source: table, lamp overlay, player derivation, cgroup / proc / save readers, the Bedrock ping,
the Bedrock journal parser, the hardened Flint client and the cache. Every reader is exercised against
fixtures and fakes; nothing here touches the live machine."""
import itertools
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from corellia_hud import classify, games, journal, state
from tests.support import games_env as ge
from tests.support.fake_bedrock import FakeBedrock, make_pong, REAL_MOTD
from tests.support.fake_flint import FakeFlint
from tests.support.games_world import World, prom_data

LAMP_OK = {"name": "x", "status": classify.GREEN, "reason": "unit active"}


def unit(status, reason="r"):
    return {"name": "x", "status": status, "reason": reason}


def with_tz(name):
    """Context-free helper: set TZ and re-read it; the caller restores."""
    os.environ["TZ"] = name
    time.tzset()


class TzCase(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()


# ------------------------------------------------------------------ table and registry
class TableTests(unittest.TestCase):
    def test_game_names_exactly_the_four(self):
        self.assertEqual(list(games.GAME_SERVERS), ["ark-server", "ark-astraeos", "ark-genesis", "bedrock"])

    def test_every_table_entry_has_a_fixed_unit_and_flint_server(self):
        for name, row in games.GAME_SERVERS.items():
            self.assertEqual(row["unit"], name + ".service")
        self.assertEqual([r.get("flint") for r in games.GAME_SERVERS.values()], ["Extinction", "Astraeos", "Genesis", None])

    def test_a_server_the_registry_does_not_list_gets_no_row_and_no_probe(self):
        w = World()
        try:
            text = ge.registry_text(w.root).replace("  bedrock:", "  bedrock-gone:").replace("status: ready\n  flint:", "status: ready\n  flint:")
            ge.write(w.registry, text)
            probes = games.GamesCache(w.cfg).get()
            out = games.build_games(w.cfg, [], probes)
            self.assertEqual([r["name"] for r in out["servers"]], ["ark-server", "ark-astraeos", "ark-genesis"])
            self.assertEqual(w.bedrock.hits, 0)
            self.assertEqual(w.journal_calls(), 0)
        finally:
            w.close()

    def test_flint_and_eu5_are_never_game_rows(self):
        w = World()
        try:
            cache = games.GamesCache(w.cfg)
            out = games.build_games(w.cfg, [], cache.get())
            self.assertEqual([r["name"] for r in out["servers"]], list(games.GAME_SERVERS))
            self.assertNotIn("flint", [r["name"] for r in out["servers"]])
            self.assertNotIn("eu5", [r["name"] for r in out["servers"]])
        finally:
            w.close()

    _RUNS = {}

    def _stalag_state(self, stalag):
        if stalag not in self._RUNS:
            w = World(stalag=stalag)
            try:
                self._RUNS[stalag] = state.build_state(w.cfg, games_cache=games.GamesCache(w.cfg))
            finally:
                w.close()
        return self._RUNS[stalag]

    def test_stalag_deferred_retired_absent_leave_games_unchanged(self):
        runs = {s: self._stalag_state(s) for s in ("absent", "deferred", "retired")}
        strip = lambda g: [{k: v for k, v in r.items() if k != "save_age_s"} for r in g["servers"]]   # age moves with the clock
        base = strip(runs["absent"]["games"])
        for s in ("deferred", "retired"):
            self.assertEqual(strip(runs[s]["games"]), base, s)
        self.assertEqual(len(base), 4)

    def test_stalag_registry_state_leaves_services_list_unchanged(self):
        runs = {s: self._stalag_state(s) for s in ("absent", "deferred", "retired")}
        names = {s: [r["name"] for r in runs[s]["services"]] for s in runs}
        self.assertEqual(names["deferred"], names["absent"])
        self.assertEqual(names["retired"], names["absent"])
        self.assertNotIn("stalag", names["absent"])

    def test_hostile_registry_names_and_locations_are_never_used(self):
        w = World()
        try:
            hostile = ("  ../../etc/passwd:\n    kind: systemd\n    status: ready\n"
                       "  ark-server;reboot:\n    kind: systemd\n    unit: x.service\n    status: ready\n")
            text = ge.registry_text(w.root).replace("  flint:", hostile + "  flint:")
            text = text.replace(f"location: {w.root}/ark-server", "location: ../../../../etc")
            ge.write(w.registry, text)
            probes = games.GamesCache(w.cfg).get()
            self.assertEqual(probes["ark-server"]["save"]["state"], "unavailable")   # relative location refused
            calls = ""
            if os.path.exists(w.journal_log):
                with open(w.journal_log) as f:
                    calls = f.read()
            self.assertNotIn("passwd", calls)
            self.assertNotIn("reboot", calls)
            paths = [r[2] for r in w.flint.requests]
            self.assertTrue(all(p.startswith("server=") and p.split("=")[1] in ("Extinction", "Astraeos", "Genesis") for p in paths), paths)
        finally:
            w.close()


# ------------------------------------------------------------------ lamp
class LampTests(unittest.TestCase):
    def test_unit_active_is_green(self):
        self.assertEqual(classify.game_lamp(unit(classify.GREEN), {"state": "ok"})[0], classify.GREEN)

    def test_unit_failed_is_red(self):
        self.assertEqual(classify.game_lamp(unit(classify.RED, "unit failed"), {"state": "ok"})[0], classify.RED)

    def test_unit_inactive_is_red(self):
        self.assertEqual(classify.game_lamp(unit(classify.RED, "unit inactive"), None)[0], classify.RED)

    def test_restart_loop_is_amber(self):
        self.assertEqual(classify.game_lamp(unit(classify.AMBER, "restart loop"), {"state": "ok"})[0], classify.AMBER)

    def test_prometheus_unreachable_is_grey(self):
        self.assertEqual(classify.game_lamp(unit(classify.GREY, "prometheus unreachable"), {"state": "ok"})[0], classify.GREY)

    def test_missing_unit_row_is_grey(self):
        self.assertEqual(classify.game_lamp(None, {"state": "ok"})[0], classify.GREY)

    def test_unit_active_flint_offline_is_amber_never_red(self):
        status, reason = classify.game_lamp(unit(classify.GREEN), {"state": "offline"})
        self.assertEqual(status, classify.AMBER)
        self.assertTrue(reason)

    def test_flint_unreachable_keeps_the_unit_lamp(self):
        self.assertEqual(classify.game_lamp(unit(classify.GREEN), {"state": "unreachable"})[0], classify.GREEN)

    def test_red_unit_stays_red_when_flint_is_online(self):
        self.assertEqual(classify.game_lamp(unit(classify.RED), {"state": "ok"})[0], classify.RED)

    def test_a_down_unit_reports_no_players_whatever_flint_still_says(self):
        w = World(players=[ge.NAME_SENTINEL])
        try:
            probes = games.GamesCache(w.cfg).get()
            rows = [{"name": "ark-server", "status": classify.RED, "reason": "unit inactive"}]
            row = next(r for r in games.build_games(w.cfg, rows, probes)["servers"] if r["name"] == "ark-server")
            self.assertEqual((row["status"], row["players"]["state"], row["players"]["count"]), (classify.RED, "unavailable", None))
            detail = games.game_detail("ark-server", row, probes)
            self.assertEqual(detail["online_names"], {"state": "unavailable", "names": []})
        finally:
            w.close()

    def test_bedrock_ping_dead_is_amber(self):
        for st in ("no_response", "unparseable"):
            self.assertEqual(classify.game_lamp(unit(classify.GREEN), {"state": st})[0], classify.AMBER, st)

    def test_overlay_never_lowers_severity_property(self):
        rank = {classify.GREEN: 0, classify.AMBER: 1, classify.RED: 2}
        probes = ["ok", "offline", "unreachable", "stale", "no_response", "unparseable", "misconfigured", "zzz", None]
        for st, p in itertools.product((classify.GREEN, classify.AMBER, classify.RED, classify.GREY), probes):
            got = classify.game_lamp(unit(st), None if p is None else {"state": p})[0]
            if st == classify.GREY:
                self.assertEqual(got, classify.GREY, (st, p))
            else:
                self.assertGreaterEqual(rank[got], rank[st], (st, p, got))


# ------------------------------------------------------------------ players
def status(now, **kw):
    s = ge.status_ok(now, **kw)
    return s


class PlayerTests(TzCase):
    NOW = 1_800_000_000.0

    def test_null_count_is_unknown_not_zero(self):
        s = ge.status_ok(self.NOW)
        s["player_count"] = None
        out = games.ark_players(s, self.NOW)
        self.assertEqual(out["state"], "unknown")
        self.assertIsNone(out["count"])

    def test_zero_is_an_explicit_zero(self):
        out = games.ark_players(ge.status_ok(self.NOW, count=0), self.NOW)
        self.assertEqual((out["state"], out["count"]), ("ok", 0))

    def test_non_integer_counts_are_unknown(self):
        for bad in ("3", True, 2.5, -1, [], {}):
            s = ge.status_ok(self.NOW)
            s["player_count"] = bad
            self.assertEqual(games.ark_players(s, self.NOW)["state"], "unknown", repr(bad))

    def test_poll_older_than_120s_is_stale(self):
        s = ge.status_ok(self.NOW, count=1)
        s[ge.POLL_KEY] = ge.local_iso(self.NOW - 121)
        out = games.ark_players(s, self.NOW)
        self.assertEqual(out["state"], "stale")
        self.assertIsNone(out["count"])

    def test_poll_within_120s_is_fresh(self):
        s = ge.status_ok(self.NOW, count=1)
        s[ge.POLL_KEY] = ge.local_iso(self.NOW - 100)
        self.assertEqual(games.ark_players(s, self.NOW)["state"], "ok")

    def test_poll_more_than_120s_in_the_future_is_stale(self):
        s = ge.status_ok(self.NOW, count=1)
        s[ge.POLL_KEY] = ge.local_iso(self.NOW + 130)
        self.assertEqual(games.ark_players(s, self.NOW)["state"], "stale")

    def test_missing_or_garbage_poll_time_is_stale(self):
        for bad in (None, "not a time", 12, ""):
            s = ge.status_ok(self.NOW, count=1)
            s[ge.POLL_KEY] = bad
            self.assertEqual(games.ark_players(s, self.NOW)["state"], "stale", repr(bad))

    def _fresh_under(self, tz):
        with_tz(tz)
        s = ge.status_ok(self.NOW, count=1, online_names=["A"])
        return games.ark_players(s, self.NOW)

    def test_local_time_poll_is_fresh_under_utc(self):
        self.assertEqual(self._fresh_under("UTC")["state"], "ok")

    def test_local_time_poll_is_fresh_under_chicago(self):
        self.assertEqual(self._fresh_under("America/Chicago")["state"], "ok")

    def test_online_names_follow_the_60s_rule(self):
        s = ge.status_ok(self.NOW, count=2)
        s["players"] = [{"name": "Here1", "last_seen": ge.local_iso(self.NOW - 12)},
                        {"name": "Here2", "last_seen": ge.local_iso(self.NOW - 50)},
                        {"name": "Gone", "last_seen": ge.local_iso(self.NOW - 3600)}]
        out = games.ark_players(s, self.NOW)
        self.assertEqual(out["names_state"], "ok")
        self.assertEqual(sorted(out["names"]), ["Here1", "Here2"])

    def test_derived_count_differing_from_player_count_is_a_mismatch(self):
        s = ge.status_ok(self.NOW, count=3, online_names=["Only"])
        out = games.ark_players(s, self.NOW)
        self.assertEqual(out["names_state"], "mismatch")
        self.assertEqual(out["names"], [])
        self.assertEqual(out["count"], 3)

    def test_hex_fallback_names_render_as_unnamed_player(self):
        self.assertEqual(games.display_name("a1b2c3d4"), "unnamed player")
        self.assertEqual(games.display_name(""), "unnamed player")
        self.assertEqual(games.display_name(None), "unnamed player")
        self.assertEqual(games.display_name("deadbeef1"), "deadbeef1")   # nine characters: a real name
        self.assertEqual(games.display_name("Gustaf"), "Gustaf")
        s = ge.status_ok(self.NOW, count=1, online_names=["a1b2c3d4"])
        self.assertEqual(games.ark_players(s, self.NOW)["names"], ["unnamed player"])

    def test_flint_offline_means_unavailable_players(self):
        s = ge.status_ok(self.NOW, count=2)
        s["status"] = "offline"
        out = games.ark_players(s, self.NOW)
        self.assertEqual(out["state"], "unavailable")
        self.assertIsNone(out["count"])

    def test_non_dict_status_is_unavailable(self):
        for bad in (None, [], "x", 3):
            self.assertEqual(games.ark_players(bad, self.NOW)["state"], "unavailable")

    def test_more_than_max_is_shown_as_is(self):
        w = World()
        try:
            probes = games.GamesCache(w.cfg).get()
            probes["ark-server"]["flint"]["status"] = ge.status_ok(time.time(), count=9)
            out = games.build_games(w.cfg, [], probes)
            row = next(r for r in out["servers"] if r["name"] == "ark-server")
            self.assertEqual((row["players"]["count"], row["players"]["max"]), (9, 8))
        finally:
            w.close()


# ------------------------------------------------------------------ process arguments
class ArgsTests(unittest.TestCase):
    def test_max_players_and_cluster_from_launch_args(self):
        got = games.args_facts([ge.cmdline("ark-server")])
        self.assertEqual(got, {"max_players": 8, "cluster": "corellia-cluster"})

    def test_absent_arguments_give_null(self):
        self.assertEqual(games.args_facts([b"/bin/bash\0start.sh\0"]), {"max_players": None, "cluster": None})
        self.assertEqual(games.args_facts([]), {"max_players": None, "cluster": None})

    def test_secret_sentinels_never_reach_any_output(self):
        w = World()
        try:
            probes = games.GamesCache(w.cfg).get()
            out = games.build_games(w.cfg, [], probes)
            blob = json.dumps(out) + repr(out) + json.dumps(probes, default=repr) + repr(probes)
            self.assertIn("MaxPlayers", ge.cmdline("ark-server").decode())          # positive control
            self.assertIn(ge.SECRET_SENTINEL, ge.cmdline("ark-server").decode())    # positive control
            for s in (ge.SECRET_SENTINEL, ge.JOIN_SENTINEL):
                self.assertNotIn(s, blob)
            row = next(r for r in out["servers"] if r["name"] == "ark-server")
            self.assertEqual(row["players"]["max"], 8)
        finally:
            w.close()


# ------------------------------------------------------------------ cgroup and proc
class CgroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name="ark-server"):
        return games.read_cgroup(os.path.join(self.root, "cgroup", ge.SERVERS[name]["unit"]), os.path.join(self.root, "proc"))

    def test_memory_current_parses(self):
        ge.make_cgroup(self.root, "ark-server")
        got = self.read()
        self.assertEqual((got["state"], got["ram_bytes"]), ("ok", ge.SERVERS["ark-server"]["ram"]))
        self.assertEqual(got["usec"], ge.SERVERS["ark-server"]["usec"])

    def test_uptime_is_the_oldest_process_start(self):
        ge.make_cgroup(self.root, "ark-server", uptime_s=5000)
        self.assertAlmostEqual(self.read()["uptime_s"], 5000, delta=1.5)

    def test_missing_cgroup_directory_is_unavailable(self):
        got = self.read("bedrock")
        self.assertEqual(got["state"], "unavailable")
        self.assertIsNone(got["ram_bytes"])
        self.assertIsNone(got["uptime_s"])

    def test_pid_vanishing_mid_read_is_tolerated(self):
        pids = ge.make_cgroup(self.root, "ark-server")
        os.remove(os.path.join(self.root, "proc", str(pids[0]), "stat"))     # the oldest pid just exited
        got = self.read()
        self.assertEqual(got["state"], "ok")
        self.assertIsNotNone(got["uptime_s"])

    def test_malformed_procs_line_is_skipped(self):
        ge.make_cgroup(self.root, "ark-server")
        path = os.path.join(self.root, "cgroup", "ark-server.service", "cgroup.procs")
        with open(path, "a") as f:
            f.write("not-a-pid\n-5\n99999999999999999999\n\n")
        self.assertEqual(self.read()["state"], "ok")

    def test_unreadable_memory_file_is_explicit(self):
        ge.make_cgroup(self.root, "ark-server")
        with open(os.path.join(self.root, "cgroup", "ark-server.service", "memory.current"), "w") as f:
            f.write("garbage\n")
        got = self.read()
        self.assertIsNone(got["ram_bytes"])


class CpuTests(unittest.TestCase):
    def tracker(self):
        self.t = [1000.0]
        return games.CpuTracker(lambda: self.t[0])

    def test_first_sample_is_null_not_zero(self):
        tr = self.tracker()
        self.assertIsNone(tr.sample("a", 5_000_000))

    def test_percent_of_one_core_from_two_samples(self):
        tr = self.tracker()
        tr.sample("a", 0)
        self.t[0] += 30
        self.assertAlmostEqual(tr.sample("a", 15_000_000), 50.0, places=3)   # 15 cpu-seconds in 30 s

    def test_can_exceed_100_percent(self):
        tr = self.tracker()
        tr.sample("a", 0)
        self.t[0] += 30
        self.assertGreater(tr.sample("a", 90_000_000), 100)

    def test_counter_decrease_is_null_then_recovers(self):
        tr = self.tracker()
        tr.sample("a", 10_000_000)
        self.t[0] += 30
        self.assertIsNone(tr.sample("a", 1_000_000))       # a restart reset the counter
        self.t[0] += 30
        self.assertAlmostEqual(tr.sample("a", 16_000_000), 50.0, places=3)

    def test_samples_closer_than_5s_reuse_the_previous_value(self):
        tr = self.tracker()
        tr.sample("a", 0)
        self.t[0] += 30
        first = tr.sample("a", 15_000_000)
        self.t[0] += 2
        self.assertEqual(tr.sample("a", 99_000_000), first)

    def test_keys_are_independent(self):
        tr = self.tracker()
        tr.sample("a", 0)
        self.assertIsNone(tr.sample("b", 0))


# ------------------------------------------------------------------ saves
class SaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.now = 1_800_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_ark_wins(self):
        ge.make_saves(self.root, "ark-server", age_s=240, now=self.now)
        got = games.newest_save_age(os.path.join(self.root, "ark-server"), self.now)
        self.assertEqual(got["state"], "ok")
        self.assertAlmostEqual(got["age_s"], 240, delta=1)

    def test_symlinks_are_skipped(self):
        p = ge.make_saves(self.root, "ark-server", age_s=500, now=self.now)
        link = os.path.join(os.path.dirname(p), "newest.ark")
        os.symlink(p, link)
        os.utime(link, (self.now - 1, self.now - 1), follow_symlinks=False)
        self.assertAlmostEqual(games.newest_save_age(os.path.join(self.root, "ark-server"), self.now)["age_s"], 500, delta=1)

    def test_no_save_gives_null_with_reason(self):
        os.makedirs(os.path.join(self.root, "ark-server", "ShooterGame", "Saved", "SavedArks", "M"))
        got = games.newest_save_age(os.path.join(self.root, "ark-server"), self.now)
        self.assertEqual(got["state"], "none")
        self.assertIsNone(got["age_s"])
        self.assertTrue(got["reason"])

    def test_future_mtime_is_null(self):
        p = ge.make_saves(self.root, "ark-server", age_s=0, now=self.now)
        os.utime(p, (self.now + 5000, self.now + 5000))
        got = games.newest_save_age(os.path.join(self.root, "ark-server"), self.now)
        self.assertEqual(got["state"], "future")
        self.assertIsNone(got["age_s"])

    def test_file_contents_are_never_opened(self):
        p = ge.make_saves(self.root, "ark-server", age_s=240, now=self.now)
        os.chmod(p, 0)
        try:
            self.assertEqual(games.newest_save_age(os.path.join(self.root, "ark-server"), self.now)["state"], "ok")
        finally:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)

    def test_relative_or_missing_location_is_unavailable(self):
        for bad in (None, "", "relative/path", "../../etc", "/nonexistent-" + str(os.getpid())):
            self.assertEqual(games.newest_save_age(bad, self.now)["state"], "unavailable", repr(bad))


# ------------------------------------------------------------------ bedrock ping
class PingTests(unittest.TestCase):
    def ping_mode(self, mode, **kw):
        fake = FakeBedrock(mode, **kw).start()
        try:
            return games.bedrock_ping(fake.port, timeout=1.0)
        finally:
            fake.stop()

    def test_real_captured_reply_parses(self):
        got = games.parse_pong(make_pong("MCPE;Dedicated Server;2169;1.26.45;0;10;10000000000000000001;Testworld;Survival;1;19132;19133;"))
        self.assertEqual((got["state"], got["version"], got["players"], got["max"], got["level"]),
                         ("ok", "1.26.45", 0, 10, "Testworld"))

    def test_live_style_ping_over_udp(self):
        got = self.ping_mode("ok", players=3, maxp=10)
        self.assertEqual((got["state"], got["players"], got["max"]), ("ok", 3, 10))

    def test_bad_magic_is_unparseable(self):
        self.assertEqual(self.ping_mode("badmagic")["state"], "unparseable")

    def test_truncated_packet_is_unparseable(self):
        self.assertEqual(self.ping_mode("truncated")["state"], "unparseable")

    def test_non_utf8_is_unparseable(self):
        self.assertEqual(self.ping_mode("nonutf8")["state"], "unparseable")

    def test_lying_length_field_is_unparseable(self):
        self.assertEqual(self.ping_mode("lying")["state"], "unparseable")

    def test_64kb_reply_is_unparseable_and_never_raises(self):
        self.assertEqual(self.ping_mode("huge")["state"], "unparseable")

    def test_garbage_inputs_never_raise(self):
        for data in (b"", b"\x1c", b"\x00" * 40, os.urandom(300), make_pong("MCPE;only;three")):
            self.assertEqual(games.parse_pong(data)["state"], "unparseable")

    def test_no_reply_is_no_response_within_budget(self):
        fake = FakeBedrock("silent").start()
        t0 = time.monotonic()
        try:
            got = games.bedrock_ping(fake.port)
        finally:
            fake.stop()
        self.assertEqual(got["state"], "no_response")
        self.assertLess(time.monotonic() - t0, 2.5)

    def test_closed_port_is_no_response(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(games.bedrock_ping(port, timeout=1.0)["state"], "no_response")

    def test_unit_active_ping_dead_is_amber_with_reason(self):
        w = World(ping_mode="silent")
        try:
            probes = games.GamesCache(w.cfg).get()
            rows = [{"name": "bedrock", "status": classify.GREEN, "reason": "unit active"}]
            row = next(r for r in games.build_games(w.cfg, rows, probes)["servers"] if r["name"] == "bedrock")
            self.assertEqual(row["status"], classify.AMBER)
            self.assertTrue(row["reason"])
            self.assertEqual(row["players"]["state"], "unavailable")
        finally:
            w.close()


# ------------------------------------------------------------------ bedrock journal
def jl(ts, text):
    return f"2026-11-05T{ts}-06:00 Corellia bedrock_server[1]: [2026-11-05 {ts}:000 INFO] {text}"


class BedrockJournalTests(unittest.TestCase):
    def test_real_connect_and_disconnect_shapes(self):
        lines = [jl("08:00:00", "Starting Server"), jl("08:01:00", "Player connected: TestPlayerOne, xuid: 2500000000000001"),
                 jl("08:02:00", "Player connected: Dee, xuid: 99"),
                 jl("08:03:00", "Player disconnected: TestPlayerOne, xuid: 2500000000000001, pfid: 0000000000000001")]
        got = games.parse_bedrock_journal(lines)
        self.assertEqual(got["state"], "ok")
        self.assertEqual(got["online"], ["Dee"])
        self.assertEqual([(e["kind"], e["name"]) for e in got["events"]], [("join", "TestPlayerOne"), ("join", "Dee"), ("leave", "TestPlayerOne")])
        self.assertTrue(got["events"][0]["time"].startswith("2026-11-05T08:01:00"))

    def test_state_resets_at_starting_server(self):
        lines = [jl("08:00:00", "Starting Server"), jl("08:01:00", "Player connected: A, xuid: 1"),
                 jl("09:00:00", "Starting Server"), jl("09:01:00", "Player connected: B, xuid: 2")]
        got = games.parse_bedrock_journal(lines)
        self.assertEqual(got["online"], ["B"])
        self.assertEqual([e["name"] for e in got["events"]], ["B"])

    def test_duplicate_connect_counts_once(self):
        lines = [jl("08:00:00", "Starting Server")] + [jl("08:01:00", "Player connected: A, xuid: 1")] * 2
        self.assertEqual(games.parse_bedrock_journal(lines)["online"], ["A"])

    def test_disconnect_without_connect_is_ignored(self):
        lines = [jl("08:00:00", "Starting Server"), jl("08:01:00", "Player disconnected: Ghost, xuid: 1, pfid: 2")]
        got = games.parse_bedrock_journal(lines)
        self.assertEqual(got["online"], [])

    def test_spaces_unicode_and_hostile_names_are_kept_as_text(self):
        names = ["Two Words", "Ünï cödé 名前", ge.HOSTILE]
        lines = [jl("08:00:00", "Starting Server")] + [jl("08:01:00", f"Player connected: {n}, xuid: 7") for n in names]
        self.assertEqual(games.parse_bedrock_journal(lines)["online"], names)

    def test_a_window_with_no_start_marker_is_unknown(self):
        lines = [jl("08:01:00", "Player connected: A, xuid: 1")] * 500
        got = games.parse_bedrock_journal(lines)
        self.assertEqual(got["state"], "unknown")
        self.assertEqual(got["online"], [])

    def test_pack_list_version_and_level_parse(self):
        got = games.parse_bedrock_journal(ge.bedrock_journal(1_800_000_000.0).splitlines())
        self.assertEqual(got["version"], "1.26.45.1")
        self.assertEqual(got["level"], "Testworld")
        self.assertEqual([p["name"] for p in got["packs"]], ["Vcraft: Home Furniture [BP]", "ExtraBiomes"])
        self.assertEqual(got["packs"][0]["version"], "1.0.4")

    def test_garbage_lines_never_raise(self):
        got = games.parse_bedrock_journal(["", "   ", "\x00\x01", "Player connected:", "x" * 100000, None])
        self.assertEqual(got["state"], "unknown")

    def test_ids_stay_off_the_tile_and_in_the_detail_journal(self):
        w = World()
        try:
            cache = games.GamesCache(w.cfg)
            probes = cache.get()
            out = games.build_games(w.cfg, [], probes)
            self.assertNotIn(ge.XUID, json.dumps(out))
            self.assertNotIn(ge.PFID, json.dumps(out))
            self.assertNotIn(ge.BED_NAME, json.dumps(out))
            row = next(r for r in out["servers"] if r["name"] == "bedrock")
            detail = games.game_detail("bedrock", row, probes)
            blob = json.dumps(detail)
            self.assertIn(ge.XUID, blob)               # raw, per Kel's no-masking rule
            self.assertNotIn("[redacted]", blob)
            self.assertLessEqual(len(detail["journal"]["lines"]), 20)
            self.assertIn(ge.BED_NAME, detail["online_names"])
        finally:
            w.close()


# ------------------------------------------------------------------ flint client
class FlintClientTests(unittest.TestCase):
    def setUp(self):
        self.flint = FakeFlint({"status": {"Extinction": {"json": {"status": "online"}}},
                                "events": {"Extinction": {"json": []}}}).start()
        self.addCleanup(self.flint.stop)

    def get(self, path="/api/status", server="Extinction", base=None, timeout=2.0):
        return games.flint_get(self.flint.url if base is None else base, path, server, timeout=timeout)

    def test_ok_returns_parsed_json(self):
        self.assertEqual(self.get(), ("ok", {"status": "online"}))

    def test_non_loopback_base_is_refused_without_a_request(self):
        for base in ("http://10.0.0.5:8090", "http://example.com", "https://127.0.0.1:1", "ftp://127.0.0.1", "", "garbage"):
            self.assertEqual(self.get(base=base)[0], "misconfigured", base)
        self.assertEqual(self.flint.count(), 0)

    def test_only_get_is_issued(self):
        self.get()
        self.get("/api/events")
        self.assertEqual(set(self.flint.verbs()), {"GET"})

    def test_redirect_is_not_followed_and_target_is_never_hit(self):
        target = FakeFlint({"status": {"Extinction": {"json": {"status": "online"}}}}).start()
        self.addCleanup(target.stop)
        self.flint.config["status"]["Extinction"] = {"redirect": target.url + "/api/status?server=Extinction"}
        self.assertEqual(self.get()[0], "redirect")
        self.assertEqual(target.count(), 0)

    def test_404_and_500_are_explicit_states(self):
        self.assertEqual(self.get(server="Genesis")[0], "http_error")
        self.flint.config["status"]["Extinction"] = {"code": 500, "raw": "boom"}
        self.assertEqual(self.get()[0], "http_error")

    def test_timeout_is_explicit(self):
        self.flint.config["status"]["Extinction"] = {"delay": 1.2, "json": {}}
        self.assertEqual(self.get(timeout=0.3)[0], "timeout")

    def test_connection_refused_is_unreachable(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)    # test-side helper: find a closed port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(self.get(base=f"http://127.0.0.1:{port}")[0], "unreachable")

    def test_invalid_and_non_dict_json_are_explicit(self):
        self.flint.config["status"]["Extinction"] = {"raw": "{not json"}
        self.assertEqual(self.get()[0], "bad_json")
        self.flint.config["status"]["Extinction"] = {"raw": "[1,2,3]"}
        self.assertEqual(self.get()[0], "bad_json")

    def test_events_must_be_a_list(self):
        self.flint.config["events"]["Extinction"] = {"json": {"a": 1}}
        self.assertEqual(self.get("/api/events")[0], "bad_json")
        self.flint.config["events"]["Extinction"] = {"json": [{"type": "x"}]}
        self.assertEqual(self.get("/api/events")[0], "ok")

    def test_body_over_1mb_is_refused(self):
        self.flint.config["status"]["Extinction"] = {"big": 1_100_000}
        self.assertEqual(self.get()[0], "too_large")

    def test_server_comes_only_from_the_table(self):
        for bad in ("Extinction&x=1", "../x", "", "extinction", "Nope"):
            self.assertEqual(self.get(server=bad)[0], "refused", bad)
        self.assertEqual(self.get(path="/api/other")[0], "refused")
        self.assertEqual(self.flint.count(), 0)


# ------------------------------------------------------------------ cache
class CacheTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_default_ttl_is_at_least_30_seconds(self):
        self.assertGreaterEqual(games.GAMES_TTL, 30)

    def test_ttl_is_honoured_on_a_monotonic_clock(self):
        t = [100.0]
        cache = games.GamesCache(self.w.cfg, ttl=30, clock=lambda: t[0])
        cache.get()
        t[0] += 29
        cache.get()
        self.assertEqual(self.w.flint.count("/api/status"), 3)
        t[0] += 2
        cache.get()
        self.assertEqual(self.w.flint.count("/api/status"), 6)

    def test_eight_concurrent_callers_cause_one_fetch_per_source(self):
        cache = games.GamesCache(self.w.cfg)
        out = []
        threads = [threading.Thread(target=lambda: out.append(cache.get())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(len(out), 8)
        self.assertEqual(self.w.flint.count("/api/status"), 3)
        self.assertEqual(self.w.flint.count("/api/events"), 3)
        self.assertEqual(self.w.bedrock.hits, 1)
        self.assertEqual(self.w.journal_calls(), 2)       # one grep window + one 20-line tail, once per window

    def test_a_source_that_raises_does_not_blank_the_others(self):
        with mock.patch.object(games, "bedrock_ping", side_effect=RuntimeError("boom")):
            probes = games.GamesCache(self.w.cfg).get()
        self.assertEqual(probes["bedrock"]["ping"]["state"], "unavailable")
        self.assertEqual(probes["ark-server"]["flint"]["state"], "ok")
        self.assertEqual(probes["ark-server"]["cgroup"]["state"], "ok")

    def test_flint_down_leaves_every_other_probe_intact(self):
        self.w.flint.stop()
        self.addCleanup(lambda: None)
        probes = games.GamesCache(self.w.cfg).get()
        self.assertEqual(probes["ark-server"]["flint"]["state"], "unreachable")
        self.assertEqual(probes["ark-server"]["cgroup"]["state"], "ok")
        self.assertEqual(probes["bedrock"]["ping"]["state"], "ok")


if __name__ == "__main__":
    unittest.main()
