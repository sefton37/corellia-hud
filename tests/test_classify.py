import math
import unittest

from corellia_hud import classify as C

NOW = 1_800_000_000.0


def view(**kw):
    base = dict(ok=True, now=NOW, collector_age=20.0, port_up={}, unit_active={}, unit_failed={},
                restart_increase={}, timer_success={}, timer_result={})
    base.update(kw)
    return C.PromView(**base)


def svc(name="x", kind="docker", port=None, unit=None, unit_db=None):
    return {"name": name, "kind": kind, "port": port, "unit": unit, "unit_db": unit_db}


class ClassifyTests(unittest.TestCase):
    def test_port_up_is_green(self):
        r = C.classify_service(svc(port=5000), view(port_up={"x": 1.0}))
        self.assertEqual((r["status"], r["label"]), (C.GREEN, "UP"))

    def test_port_down_is_red(self):
        r = C.classify_service(svc(port=5000), view(port_up={"x": 0.0}))
        self.assertEqual((r["status"], r["label"]), (C.RED, "DOWN"))

    def test_unit_failed_is_red(self):
        s = svc(kind="systemd-user", unit="u.service")
        r = C.classify_service(s, view(unit_active={("user", "u.service"): 1.0},
                                       unit_failed={("user", "u.service"): 1.0}))
        self.assertEqual(r["status"], C.RED)

    def test_unit_inactive_is_red(self):
        s = svc(kind="systemd", unit="u.service")
        r = C.classify_service(s, view(unit_active={("system", "u.service"): 0.0}))
        self.assertEqual(r["status"], C.RED)

    def test_unit_active_is_green(self):
        s = svc(kind="systemd", unit="u.service")
        r = C.classify_service(s, view(unit_active={("system", "u.service"): 1.0}))
        self.assertEqual(r["status"], C.GREEN)

    def test_restart_loop_is_amber(self):
        s = svc(kind="systemd", unit="bedrock.service")
        r = C.classify_service(s, view(unit_active={("system", "bedrock.service"): 1.0},
                                       restart_increase={("system", "bedrock.service"): 9.0}))
        self.assertEqual((r["status"], r["label"]), (C.AMBER, "WARN"))
        self.assertIn("restart", r["reason"])

    def test_lifetime_restarts_without_recent_increase_is_not_amber(self):
        # bedrock: lifetime counter 24, but increase over 30 m is 0 -> must stay green
        s = svc(kind="systemd", unit="bedrock.service")
        r = C.classify_service(s, view(unit_active={("system", "bedrock.service"): 1.0},
                                       restart_increase={("system", "bedrock.service"): 0.0}))
        self.assertEqual(r["status"], C.GREEN)

    def test_restart_threshold_boundary(self):
        s = svc(kind="systemd", unit="b.service")
        at = C.classify_service(s, view(unit_active={("system", "b.service"): 1.0},
                                        restart_increase={("system", "b.service"): 5.0}))
        self.assertEqual(at["status"], C.GREEN)  # rule is strictly > 5

    def test_collector_stale_turns_green_amber(self):
        r = C.classify_service(svc(port=5000), view(port_up={"x": 1.0}, collector_age=601.0))
        self.assertEqual(r["status"], C.AMBER)
        self.assertTrue(r["stale"])

    def test_collector_fresh_is_not_stale(self):
        r = C.classify_service(svc(port=5000), view(port_up={"x": 1.0}, collector_age=600.0))
        self.assertFalse(r["stale"])
        self.assertEqual(r["status"], C.GREEN)

    def test_stale_never_hides_red(self):
        r = C.classify_service(svc(port=5000), view(port_up={"x": 0.0}, collector_age=9999.0))
        self.assertEqual(r["status"], C.RED)

    def test_missing_series_is_grey(self):
        r = C.classify_service(svc(port=5000), view(port_up={"other": 1.0}))
        self.assertEqual((r["status"], r["label"]), (C.GREY, "NO DATA"))

    def test_empty_prometheus_is_grey(self):
        r = C.classify_service(svc(port=5000), view())
        self.assertEqual(r["status"], C.GREY)

    def test_prometheus_unreachable_is_grey(self):
        r = C.classify_service(svc(port=5000), view(ok=False, port_up={"x": 1.0}))
        self.assertEqual(r["status"], C.GREY)

    def test_grey_is_distinct_from_green(self):
        g = C.classify_service(svc(port=5000), view())
        u = C.classify_service(svc(port=5000), view(port_up={"x": 1.0}))
        self.assertNotEqual(g["status"], u["status"])
        self.assertNotEqual(g["label"], u["label"])
        self.assertNotEqual(C.GREY, C.GREEN)

    def test_dual_scope_flint_uses_registry_scope(self):
        # corellia_unit_active{unit="flint.service"} is 0 in system scope and 1 in user scope
        both = {("system", "flint.service"): 0.0, ("user", "flint.service"): 1.0}
        user = C.classify_service(svc("flint", "systemd-user", unit="flint.service"), view(unit_active=both))
        system = C.classify_service(svc("flint", "systemd", unit="flint.service"), view(unit_active=both))
        self.assertEqual(user["status"], C.GREEN)
        self.assertEqual(system["status"], C.RED)

    def test_timer_older_than_threshold_is_amber(self):
        s = svc("corellia-backup", "systemd", unit="corellia-backup.timer")
        r = C.classify_service(s, view(timer_success={("system", "corellia-backup.service"): NOW - 31 * 3600},
                                       timer_result={("system", "corellia-backup.service"): 1.0}))
        self.assertEqual(r["status"], C.AMBER)

    def test_timer_fresh_is_green(self):
        s = svc("corellia-backup", "systemd", unit="corellia-backup.timer")
        r = C.classify_service(s, view(timer_success={("system", "corellia-backup.service"): NOW - 3600},
                                       timer_result={("system", "corellia-backup.service"): 1.0}))
        self.assertEqual(r["status"], C.GREEN)

    def test_hourly_db_timer_has_tighter_threshold(self):
        s = svc("corellia-backup", "systemd", unit="corellia-backup.timer", unit_db="corellia-backup-db.timer")
        r = C.classify_service(s, view(
            timer_success={("system", "corellia-backup.service"): NOW - 3600,
                           ("system", "corellia-backup-db.service"): NOW - 4 * 3600},
            timer_result={("system", "corellia-backup.service"): 1.0,
                          ("system", "corellia-backup-db.service"): 1.0}))
        self.assertEqual(r["status"], C.AMBER)

    def test_timer_failed_last_result_is_red(self):
        s = svc("corellia-backup", "systemd", unit="corellia-backup.timer")
        r = C.classify_service(s, view(timer_success={("system", "corellia-backup.service"): NOW - 60},
                                       timer_result={("system", "corellia-backup.service"): 0.0}))
        self.assertEqual(r["status"], C.RED)

    def test_precedence_red_beats_amber_beats_green(self):
        s = svc(kind="systemd-user", unit="u.service", port=1234, name="x")
        amber = view(port_up={"x": 1.0}, unit_active={("user", "u.service"): 1.0},
                     restart_increase={("user", "u.service"): 8.0})
        self.assertEqual(C.classify_service(s, amber)["status"], C.AMBER)
        red = view(port_up={"x": 0.0}, unit_active={("user", "u.service"): 1.0},
                   restart_increase={("user", "u.service"): 8.0})
        self.assertEqual(C.classify_service(s, red)["status"], C.RED)

    def test_grey_only_when_no_data_at_all(self):
        s = svc(kind="systemd-user", unit="u.service", port=1234, name="x")
        r = C.classify_service(s, view(port_up={"x": 1.0}))  # unit series missing, port present
        self.assertEqual(r["status"], C.GREEN)

    def test_nan_sample_is_grey(self):
        r = C.classify_service(svc(port=5000), view(port_up={"x": math.nan}))
        self.assertEqual(r["status"], C.GREY)

    def test_non_numeric_sample_is_grey(self):
        r = C.classify_service(svc(port=5000), view(port_up={"x": "oops"}))
        self.assertEqual(r["status"], C.GREY)

    def test_service_with_no_source_is_grey(self):
        r = C.classify_service(svc("rogue-routine", "hugo"), view())
        self.assertEqual(r["status"], C.GREY)
        self.assertIn("no probe", r["reason"])

    def test_unit_kind_python_ignores_unit_metrics(self):
        s = svc(kind="python", unit="u.service", port=5000)
        r = C.classify_service(s, view(port_up={"x": 1.0}, unit_active={("system", "u.service"): 0.0}))
        self.assertEqual(r["status"], C.GREEN)

    def test_perf_levels(self):
        self.assertEqual(C.level_for("cpu", 12.0), C.GREEN)
        self.assertEqual(C.level_for("cpu", 93.0), C.AMBER)
        self.assertEqual(C.level_for("disk", 96.0), C.RED)
        self.assertEqual(C.level_for("disk", None), C.GREY)
        self.assertEqual(C.level_for("disk", math.nan), C.GREY)


if __name__ == "__main__":
    unittest.main()
