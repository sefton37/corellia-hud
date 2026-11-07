"""Spec #492 compact Services panel: classify.services_summary (table, properties, a generator that builds each
state AND its adversary and reports its hit counts), the meta.services_summary wiring with a golden proof that
nothing else in /api/state moved, and the `all` id of the existing `services` detail kind."""
import copy
import http.client
import json
import os
import random
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from corellia_hud import classify as C
from corellia_hud import detail, server, state
from tests.support import fixtures as fx
from tests.support import state_golden as golden
from tests.support.fake_prom import FakeProm, healthy_data, mixed_data

OKSRC = {"prometheus": "ok", "registry": "ok"}


def row(name, status, group="Apps"):
    return {"name": name, "status": status, "label": C.LABELS.get(status, "?"), "reason": "r", "group": group}


def rows_of(*pairs):
    return [row(n, s) for n, s in pairs]


def summ(rows, **src):
    return C.services_summary(rows, dict(OKSRC, **src))


class SummaryTable(unittest.TestCase):
    def test_all_up(self):
        s = summ(rows_of(("a", "green"), ("b", "green"), ("c", "green")))
        self.assertEqual(s, {"state": "all_up", "level": "green", "up": 3, "total": 3, "not_up": 0, "listed": [],
                             "more": 0, "cap": C.SERVICES_LIST_CAP})

    def test_one_red_lists_only_it(self):
        s = summ(rows_of(("a", "green"), ("b", "red"), ("c", "green")))
        self.assertEqual((s["state"], s["level"], s["up"], s["total"], s["not_up"], s["listed"], s["more"]),
                         ("problems", "red", 2, 3, 1, ["b"], 0))

    def test_worst_first_red_amber_grey(self):
        s = summ(rows_of(("g1", "grey"), ("a1", "amber"), ("r1", "red"), ("g2", "grey"), ("r2", "red"), ("a2", "amber")))
        self.assertEqual(s["listed"], ["r1", "r2", "a1", "a2"])
        self.assertEqual((s["more"], s["not_up"]), (2, 6))

    def test_grey_is_not_up_and_is_listed(self):
        s = summ(rows_of(("a", "green"), ("b", "grey")))
        self.assertEqual((s["state"], s["level"], s["up"], s["not_up"], s["listed"]), ("problems", "grey", 1, 1, ["b"]))

    def test_stable_within_a_status(self):
        s = summ(rows_of(("z", "red"), ("a", "red"), ("m", "red")))
        self.assertEqual(s["listed"], ["z", "a", "m"])   # input order = group order then name; never re-sorted

    def test_cap_overflow_more_is_exact(self):
        s = summ([row(f"r{i}", "red") for i in range(9)])
        self.assertEqual((len(s["listed"]), s["more"], s["not_up"]), (C.SERVICES_LIST_CAP, 9 - C.SERVICES_LIST_CAP, 9))

    def test_cap_zero(self):
        s = C.services_summary(rows_of(("a", "red")), OKSRC, cap=0)
        self.assertEqual((s["listed"], s["more"], s["cap"]), ([], 1, 0))

    def test_cap_one(self):
        s = C.services_summary(rows_of(("a", "amber"), ("b", "red")), OKSRC, cap=1)
        self.assertEqual((s["listed"], s["more"]), (["b"], 1))

    def test_cap_larger_than_list(self):
        s = C.services_summary(rows_of(("a", "amber"), ("b", "red")), OKSRC, cap=50)
        self.assertEqual((s["listed"], s["more"]), (["b", "a"], 0))

    def test_negative_cap_behaves_as_zero(self):
        s = C.services_summary(rows_of(("a", "red")), OKSRC, cap=-3)
        self.assertEqual((s["listed"], s["more"]), ([], 1))

    def test_level_is_worst_present(self):
        self.assertEqual(summ(rows_of(("a", "grey"), ("b", "green")))["level"], "grey")
        self.assertEqual(summ(rows_of(("a", "grey"), ("b", "amber")))["level"], "amber")
        self.assertEqual(summ(rows_of(("a", "grey"), ("b", "amber"), ("c", "red")))["level"], "red")

    def test_prometheus_unreachable_is_unavailable_grey_never_up(self):
        s = summ(rows_of(("a", "grey"), ("b", "grey")), prometheus="unreachable")
        self.assertEqual((s["state"], s["level"], s["listed"], s["more"], s["up"]), ("unavailable", "grey", [], 0, 0))

    def test_unreachable_beats_green_rows(self):   # the adversary: rows that LOOK all green
        s = summ(rows_of(("a", "green"), ("b", "green")), prometheus="unreachable")
        self.assertEqual((s["state"], s["level"]), ("unavailable", "grey"))

    def test_collector_stale_is_amber_never_all_up(self):
        s = summ(rows_of(("a", "amber"), ("b", "amber")), prometheus="stale")
        self.assertEqual((s["state"], s["level"], s["listed"], s["more"]), ("stale", "amber", [], 0))

    def test_stale_beats_green_rows(self):
        s = summ(rows_of(("a", "green"), ("b", "green")), prometheus="stale")
        self.assertEqual((s["state"], s["level"]), ("stale", "amber"))

    def test_stale_with_zero_rows_is_stale_not_empty(self):
        s = summ([], prometheus="stale")
        self.assertEqual((s["state"], s["level"]), ("stale", "amber"))

    def test_registry_missing_is_no_registry(self):
        s = summ([], registry="missing")
        self.assertEqual((s["state"], s["level"], s["total"]), ("no_registry", "grey", 0))

    def test_registry_missing_and_prometheus_down_collide_to_no_registry(self):
        s = summ([], registry="missing", prometheus="unreachable")
        self.assertEqual(s["state"], "no_registry")

    def test_registry_missing_beats_green_rows(self):
        self.assertEqual(summ(rows_of(("a", "green")), registry="missing")["state"], "no_registry")

    def test_empty_registry(self):
        s = summ([])
        self.assertEqual((s["state"], s["level"], s["up"], s["total"]), ("empty", "grey", 0, 0))

    def test_unknown_prometheus_state_is_never_all_up(self):
        for bad in ("weird", None, "", "OK"):
            s = summ(rows_of(("a", "green")), prometheus=bad)
            self.assertNotEqual(s["state"], "all_up", bad)

    def test_missing_sources_key_is_never_all_up(self):
        self.assertNotEqual(C.services_summary(rows_of(("a", "green")), {})["state"], "all_up")
        self.assertNotEqual(C.services_summary(rows_of(("a", "green")), None)["state"], "all_up")

    def test_unknown_status_counts_as_not_up(self):
        s = summ(rows_of(("a", "green"), ("b", "purple")))
        self.assertEqual((s["state"], s["up"], s["not_up"], s["listed"]), ("problems", 1, 1, ["b"]))
        self.assertEqual(s["level"], "grey")

    def test_missing_status_key_counts_as_not_up(self):
        s = summ([{"name": "a"}, row("b", "green")])
        self.assertEqual((s["state"], s["up"], s["not_up"], s["listed"]), ("problems", 1, 1, ["a"]))

    def test_unknown_status_sorts_after_grey(self):
        s = summ(rows_of(("x", "purple"), ("g", "grey"), ("r", "red")))
        self.assertEqual(s["listed"], ["r", "g", "x"])

    def test_game_units_are_listed_like_any_other(self):
        s = summ(rows_of(("ark-server", "red"), ("a", "green")))
        self.assertEqual(s["listed"], ["ark-server"])

    def test_inputs_are_not_mutated(self):
        rows = rows_of(("b", "red"), ("a", "green"), ("c", "grey"))
        src = dict(OKSRC)
        before = copy.deepcopy((rows, src))
        C.services_summary(rows, src)
        self.assertEqual((rows, src), before)

    def test_pure_same_input_same_output(self):
        rows = rows_of(("b", "red"), ("a", "green"))
        self.assertEqual(summ(rows), summ(rows))

    def test_result_is_json_serialisable(self):
        json.dumps(summ(rows_of(("a", "red"))))

    def test_cap_constant_is_four(self):
        self.assertEqual(C.SERVICES_LIST_CAP, 4)


class SummaryProperties(unittest.TestCase):
    """A generator that CONSTRUCTS each state and its adversary, then asserts every state was actually hit."""
    STATUSES = ["green", "amber", "red", "grey"]

    def make(self, rng):
        kind = rng.choice(["all_up", "problems", "unavailable", "stale", "no_registry", "empty",
                           "unavail_green", "stale_green", "noreg_green"])
        n = rng.randint(1, 12)
        green = lambda: [row(f"s{i}", "green") for i in range(n)]
        mixed = lambda: [row(f"s{i}", rng.choice(self.STATUSES)) for i in range(n)]
        if kind == "all_up":
            return kind, green(), dict(OKSRC)
        if kind == "problems":
            r = green()
            for j in rng.sample(range(n), rng.randint(1, n)):
                r[j] = row(f"s{j}", rng.choice(["amber", "red", "grey"]))
            return kind, r, dict(OKSRC)
        if kind == "unavailable":
            return kind, [row(f"s{i}", "grey") for i in range(n)], dict(OKSRC, prometheus="unreachable")
        if kind == "unavail_green":
            return "unavailable", green(), dict(OKSRC, prometheus="unreachable")
        if kind == "stale":
            return kind, mixed(), dict(OKSRC, prometheus="stale")
        if kind == "stale_green":
            return "stale", green(), dict(OKSRC, prometheus="stale")
        if kind == "no_registry":
            return kind, [], dict(OKSRC, registry="missing", prometheus=rng.choice(["ok", "unreachable", "stale"]))
        if kind == "noreg_green":
            return "no_registry", green(), dict(OKSRC, registry="missing")
        return "empty", [], dict(OKSRC)

    def test_properties_over_constructed_states(self):
        rng = random.Random(492)
        hits = {}
        for _ in range(1200):
            want, rows, src = self.make(rng)
            cap = rng.choice([0, 1, 2, 4, 6, 30])
            s = C.services_summary(rows, src, cap=cap)
            hits[want] = hits.get(want, 0) + 1
            self.assertEqual(s["state"], want, (rows, src))
            names = {r["name"] for r in rows if r["status"] != "green"}
            self.assertTrue(set(s["listed"]) <= names)
            self.assertLessEqual(len(s["listed"]), cap)
            self.assertEqual(s["up"], sum(1 for r in rows if r["status"] == "green"))
            self.assertEqual(s["total"], len(rows))
            self.assertEqual(s["not_up"], s["total"] - s["up"])
            if s["state"] == "all_up":
                self.assertTrue(src["registry"] == "ok" and src["prometheus"] == "ok" and rows and s["not_up"] == 0)
                self.assertEqual(s["level"], "green")
            if s["state"] == "problems":
                self.assertEqual(len(s["listed"]) + s["more"], s["not_up"])
                order = {"red": 0, "amber": 1, "grey": 2}
                by = {r["name"]: r["status"] for r in rows}
                ranks = [order[by[n]] for n in s["listed"]]
                self.assertEqual(ranks, sorted(ranks))
                worst = min(order[r["status"]] for r in rows if r["status"] != "green")
                self.assertEqual(s["level"], ["red", "amber", "grey"][worst])
                # nothing worse than what is listed was left behind the cap
                if s["more"] and ranks:
                    left = [order[by[n]] for n in by if by[n] != "green" and n not in s["listed"]]
                    self.assertGreaterEqual(min(left), max(ranks))
            else:
                self.assertEqual((s["listed"], s["more"]), ([], 0))
        print(f"SUMMARY_GENERATOR_HITS {sorted(hits.items())}")
        for kind in ("all_up", "problems", "unavailable", "stale", "no_registry", "empty"):
            self.assertGreater(hits.get(kind, 0), 20, f"generator never built {kind}")


class StateWiring(unittest.TestCase):
    def test_summary_equals_direct_call_and_reconciles_with_counts(self):
        for scenario in golden.SCENARIOS:
            snap = golden.build(scenario)
            meta = snap["meta"]
            self.assertEqual(meta["services_summary"], C.services_summary(snap["services"], meta["sources"]), scenario)
            self.assertEqual(meta["services_summary"]["up"], meta["counts"]["green"], scenario)
            self.assertEqual(meta["services_summary"]["total"], len(snap["services"]), scenario)
            s = meta["services_summary"]
            c = meta["counts"]
            self.assertEqual(s["not_up"], c["red"] + c["amber"] + c["grey"])

    def test_prometheus_down_is_unavailable(self):
        s = golden.build("prom_down")["meta"]["services_summary"]
        self.assertEqual((s["state"], s["level"], s["up"], s["listed"]), ("unavailable", "grey", 0, []))

    def test_registry_missing_is_no_registry(self):
        s = golden.build("registry_missing")["meta"]["services_summary"]
        self.assertEqual((s["state"], s["total"]), ("no_registry", 0))

    def test_healthy_is_all_up(self):
        s = golden.build("healthy")["meta"]["services_summary"]
        self.assertEqual((s["state"], s["level"], s["up"], s["total"]), ("all_up", "green", 3, 3))

    def test_collector_stale_is_stale(self):
        d = healthy_data()
        d["collector_age"] = [({}, 4000)]
        fake = FakeProm(d).start()
        try:
            with tempfile.TemporaryDirectory() as t:
                reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_HEALTHY)
                cfg = state.Config(prom_url=fake.url, registry_path=reg, analytics_db=os.path.join(t, "n.db"),
                                   product_db=os.path.join(t, "n2.db"), cf_token_file=os.path.join(t, "n"),
                                   cf_api_base=golden.DEAD)
                snap = state.build_state(cfg, now=golden.NOW)
        finally:
            fake.stop()
        self.assertEqual(snap["meta"]["sources"]["prometheus"], "stale")
        s = snap["meta"]["services_summary"]
        self.assertEqual((s["state"], s["level"]), ("stale", "amber"))
        self.assertNotIn("green", {r["status"] for r in snap["services"]})

    def test_every_other_key_matches_the_pre_change_golden(self):
        with open(golden.GOLDEN) as f:
            want = json.load(f)
        for scenario in golden.SCENARIOS:
            got = golden.comparable(golden.build(scenario))
            self.assertEqual(json.dumps(got, sort_keys=True), json.dumps(want[scenario], sort_keys=True), scenario)

    def test_meta_key_order_only_gained_the_summary_at_the_end(self):
        meta = golden.build("healthy")["meta"]
        self.assertEqual(list(meta), ["generated_at", "sources", "all_clear", "counts", "banner", "services_summary"])

    def test_summary_and_services_agree_on_game_unit_names(self):
        d = healthy_data()
        d["unit_active"] = [({"scope": "system", "unit": "ark-server.service"}, 0)]
        d["unit_failed"] = [({"scope": "system", "unit": "ark-server.service"}, 1)]
        fake = FakeProm(d).start()
        try:
            with tempfile.TemporaryDirectory() as t:
                reg = fx.write(os.path.join(t, "r.yaml"),
                               "services:\n  ark-server:\n    kind: systemd\n    unit: ark-server.service\n    status: ready\n"
                               "  alpha:\n    kind: docker\n    port: 5000\n    status: ready\n")
                cfg = state.Config(prom_url=fake.url, registry_path=reg, analytics_db=os.path.join(t, "n.db"),
                                   product_db=os.path.join(t, "n2.db"), cf_token_file=os.path.join(t, "n"),
                                   cf_api_base=golden.DEAD)
                snap = state.build_state(cfg, now=golden.NOW)
        finally:
            fake.stop()
        names = {r["name"] for r in snap["services"]}
        self.assertIn("ark-server", snap["meta"]["services_summary"]["listed"])
        self.assertTrue(set(snap["meta"]["services_summary"]["listed"]) <= names)


class DetailAll(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.tmp.name
        now = time.time()
        reg = fx.write(os.path.join(t, "r.yaml"), fx.REGISTRY_E2E_MIXED)
        pdb = fx.make_product(os.path.join(t, "p.db"), fx.healthy_product_runs(now))
        cls.prom = FakeProm(mixed_data(), []).start()
        cfg = state.Config(prom_url=cls.prom.url, registry_path=reg, analytics_db=os.path.join(t, "none.db"),
                           product_db=pdb, cf_token_file=os.path.join(t, "none"), cf_api_base="http://127.0.0.1:9",
                           chat_dir=os.path.join(t, "chat"), claude_projects_dir=os.path.join(t, "none"),
                           claude_cred_path=os.path.join(t, "none.json"))
        cls.httpd = server.make_server(cfg, host="127.0.0.1", port=0, ttl=1.0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.prom.stop()
        cls.tmp.cleanup()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def api(self, kind, ident):
        status, body = self.get(f"/api/detail/{kind}?id={quote(ident, safe='')}")
        return status, (json.loads(body) if body[:1] == b"{" else body)

    def test_all_returns_every_service_in_snapshot_order(self):
        _, snap = self.get("/api/state")
        snap = json.loads(snap)
        code, d = self.api("services", "all")
        self.assertEqual(code, 200)
        self.assertEqual([r["name"] for r in d["rows"]], [r["name"] for r in snap["services"]])
        self.assertEqual(len(d["rows"]), 5)
        for r in d["rows"]:
            self.assertEqual(set(r), {"name", "port", "label", "reason", "group", "status"})

    def test_all_rows_carry_their_own_status_including_non_green(self):
        _, d = self.api("services", "all")
        by = {r["name"]: r["status"] for r in d["rows"]}
        self.assertEqual(by["sieve"], "red")
        self.assertEqual(by["alpha"], "green")
        self.assertEqual(by["loopy"], "amber")
        self.assertEqual(by["ghost"], "grey")
        self.assertEqual(by["rogue-routine"], "grey")

    def test_all_header_fields(self):
        _, snap = self.get("/api/state")
        snap = json.loads(snap)
        _, d = self.api("services", "all")
        self.assertEqual((d["kind"], d["id"], d["status"], d["label"], d["total"]), ("services", "all", "all", "All", 5))
        self.assertEqual(d["counts"], snap["meta"]["counts"])
        self.assertEqual(d["summary"], snap["meta"]["services_summary"])

    def test_existing_ids_keep_their_shape_and_filter(self):
        code, d = self.api("services", "red")
        self.assertEqual(code, 200)
        self.assertEqual([r["name"] for r in d["rows"]], ["sieve"])
        self.assertEqual(set(d), {"status", "label", "rows", "total", "counts", "kind", "id"})
        for st in ("green", "amber", "grey"):
            self.assertEqual(self.api("services", st)[0], 200)

    def test_bad_ids_are_refused(self):
        for bad in ("everything", "ALL", "all ", "%2e%2e", "%00", "x" * 5000, "All", "all/", ""):
            status, _ = self.get(f"/api/detail/services?id={bad if bad.startswith('%') else quote(bad, safe='')}")
            self.assertIn(status, (400, 404), repr(bad[:20]))

    def test_kinds_unchanged(self):
        self.assertEqual(detail.KINDS, ("service", "services", "perf", "site", "chat", "alert", "game", "claude"))
        self.assertEqual(detail.SERVICES_ALL, "all")

    def test_no_path_or_secret_in_the_all_payload(self):
        _, body = self.get("/api/detail/services?id=all")
        text = body.decode()
        for needle in ("/home/", "token", ".yaml", ".db"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
