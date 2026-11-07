"""claude_usage.ClaudeUsageIndex: transcript parsing, dedupe, hostile input, incremental scanning, local-time
windows, fun stats, and privacy. Every fixture line is shaped from the OBSERVED real transcript keys."""
import calendar
import contextlib
import json
import os
import tempfile
import time
import unittest

from corellia_hud import claude_usage as cu
from tests.support import fake_claude as fc

NOW = calendar.timegm((2026, 11, 4, 18, 0, 0))   # 12:00 Chicago (CST), 18:00 UTC
DAY = 86400


@contextlib.contextmanager
def tz(name):
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


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
        self.root = os.path.join(self.tmp.name, "projects")
        self.tree = fc.Tree(self.root)
        self.mono = [0.0]
        self.addCleanup(self.tmp.cleanup)

    def idx(self, wall=NOW + 60):
        return cu.ClaudeUsageIndex(self.root, clock=lambda: self.mono[0], wall=lambda: wall)

    def scan(self, ix=None, now=NOW):
        ix = ix or self.idx()
        ix.refresh(force=True)
        return ix, ix.summary(now)

    def turns(self, ix):
        return sorted(ix._merged().values())


class ParseAndDedupeTests(Base):
    def test_a_real_shaped_line_becomes_one_turn(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, inp=7, out=3, cw=11, cr=13, mid="m", rid="r")])
        ix, s = self.scan()
        t = self.turns(ix)
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0][2:6], (7, 3, 11, 13))
        self.assertEqual(s["windows"]["today"]["new"], 7 + 11 + 3)

    def test_identical_duplicates_count_once(self):
        ln = fc.line(NOW - 60, mid="m", rid="r")
        self.tree.write("p/a.jsonl", [ln, ln, ln])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 1)

    def test_growing_output_keeps_the_maximum(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="m", rid="r", out=o) for o in (2, 9, 40)])
        ix, _ = self.scan()
        self.assertEqual(self.turns(ix)[0][3], 40)

    def test_a_later_copy_with_smaller_output_still_keeps_the_maximum(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="m", rid="r", out=o) for o in (5, 90, 12)])
        ix, _ = self.scan()
        self.assertEqual(self.turns(ix)[0][3], 90)

    def test_the_same_key_in_two_files_counts_once(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="m", rid="r", out=4)])
        self.tree.write("p/b.jsonl", [fc.line(NOW - 60, mid="m", rid="r", out=8)])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 1)
        self.assertEqual(self.turns(ix)[0][3], 8)

    def test_synthetic_model_is_excluded(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, model="<synthetic>", mid=None, rid=None)])
        ix, s = self.scan()
        self.assertEqual(s["state"], "empty")
        self.assertGreaterEqual(s["scan"]["skipped"], 1)

    def test_a_line_without_message_or_request_id_falls_back_to_uuid(self):
        a = fc.line(NOW - 60, uuid="u-1")
        a["message"].pop("id")
        a.pop("requestId")
        self.tree.write("p/a.jsonl", [a, a])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 1)

    def test_a_line_with_no_key_at_all_is_skipped_and_counted(self):
        a = fc.line(NOW - 60)
        a["message"].pop("id")
        a.pop("requestId")
        a.pop("uuid")
        self.tree.write("p/a.jsonl", [a])
        ix, s = self.scan()
        self.assertEqual(s["state"], "empty")
        self.assertGreaterEqual(s["scan"]["skipped"], 1)

    def test_two_request_ids_under_one_message_id_both_count(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="m", rid="r1"), fc.line(NOW - 50, mid="m", rid="r2")])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 2)

    def test_lines_without_usage_are_ignored_silently(self):
        self.tree.write("p/a.jsonl", [fc.user_line(NOW - 60), fc.line(NOW - 50)])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 1)
        self.assertEqual(s["scan"]["bad_lines"], 0)

    def test_subagent_files_are_included(self):
        self.tree.write("p/s.jsonl", [fc.line(NOW - 60, mid="a", rid="a")])
        self.tree.write("p/s/subagents/agent-9.jsonl", [fc.line(NOW - 50, mid="b", rid="b")])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 2)
        self.assertEqual(s["scan"]["files"], 2)


class HostileInputTests(Base):
    def bad(self, raw_line):
        self.tree.write("p/a.jsonl", [], raw=raw_line)
        ix, s = self.scan()
        self.assertEqual(s["state"], "empty", raw_line[:80])
        self.assertGreaterEqual(s["scan"]["bad_lines"], 1)
        return ix, s

    def test_malformed_json(self):
        self.bad(b'{"usage": not json\n')

    def test_a_json_list_is_not_a_line(self):
        self.bad(b'["usage"]\n')

    def test_usage_that_is_not_a_dict(self):
        ln = fc.line(NOW - 60)
        ln["message"]["usage"] = "lots"
        self.bad(fc.dumps(ln).encode())

    def test_a_missing_token_field_is_skipped_never_zero_filled(self):
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            with self.subTest(key=key):
                ln = fc.line(NOW - 60)
                del ln["message"]["usage"][key]
                self.setUp()
                self.bad(fc.dumps(ln).encode())

    def test_bad_token_values_are_skipped_and_counted(self):
        for label, value in (("negative", -1), ("bool", True), ("float", 1.5), ("string", "12"), ("none", None)):
            with self.subTest(label):
                ln = fc.line(NOW - 60)
                ln["message"]["usage"]["output_tokens"] = value
                self.setUp()
                self.bad(fc.dumps(ln).encode())

    def test_missing_timestamp(self):
        ln = fc.line(NOW - 60)
        del ln["timestamp"]
        self.bad(fc.dumps(ln).encode())

    def test_invalid_timestamp(self):
        self.bad(fc.dumps(fc.line(NOW - 60, timestamp="yesterday-ish")).encode())

    def test_far_future_timestamp(self):
        self.bad(fc.dumps(fc.line(NOW + 30 * DAY)).encode())

    def test_a_five_megabyte_line_is_skipped_and_counted(self):
        big = '{"usage":"' + "x" * (5 * 1024 * 1024) + '"}\n'
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="a", rid="a")], raw=None)
        self.tree.append("p/a.jsonl", [], raw=big.encode())
        self.tree.append("p/a.jsonl", [fc.line(NOW - 30, mid="b", rid="b")])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 2)
        self.assertGreaterEqual(s["scan"]["skipped"], 1)

    def test_a_model_that_fails_the_pattern_goes_to_other(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, model="Bad Model!<script>")])
        ix, s = self.scan()
        self.assertEqual(self.turns(ix)[0][1], "other")

    def test_a_non_string_model_goes_to_other(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="x", rid="x")])
        ln = fc.line(NOW - 50, mid="y", rid="y")
        ln["message"]["model"] = 12
        self.tree.append("p/a.jsonl", [ln])
        ix, _ = self.scan()
        self.assertEqual(sorted(t[1] for t in self.turns(ix)), ["claude-sonnet-5-5", "other"])

    def test_hostile_cwd_is_kept_as_text_capped_at_80(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, cwd="/x/" + '<img src=x onerror="a=1">' * 5)])
        ix, _ = self.scan()
        name = self.turns(ix)[0][6]
        self.assertLessEqual(len(name), 80)
        self.assertIn("<img", name)

    def test_an_empty_or_missing_cwd_becomes_unknown(self):
        ln = fc.line(NOW - 60)
        del ln["cwd"]
        self.tree.write("p/a.jsonl", [ln])
        ix, _ = self.scan()
        self.assertEqual(self.turns(ix)[0][6], "unknown")


class IncrementalTests(Base):
    def test_a_partial_trailing_line_waits_until_it_is_completed(self):
        full = fc.dumps(fc.line(NOW - 60, mid="b", rid="b"))
        self.tree.write("p/a.jsonl", [fc.line(NOW - 90, mid="a", rid="a")])
        self.tree.append("p/a.jsonl", [], raw=full[:40].encode())
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 1)
        self.assertEqual(s["scan"]["bad_lines"], 0)
        self.tree.append("p/a.jsonl", [], raw=full[40:].encode())
        ix.refresh(force=True)
        self.assertEqual(ix.summary(NOW)["windows"]["d7"]["turns"], 2)

    def test_an_append_reads_only_the_new_bytes(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 90, mid="a", rid="a")])
        ix, s = self.scan()
        n = self.tree.append("p/a.jsonl", [fc.line(NOW - 60, mid="b", rid="b")])
        ix.refresh(force=True)
        s = ix.summary(NOW)
        self.assertEqual(s["scan"]["bytes_read_last"], n)
        self.assertEqual(s["windows"]["d7"]["turns"], 2)
        self.assertEqual(s["scan"]["full_rescans"], 0)

    def test_no_change_reads_zero_bytes(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 90)])
        ix, s = self.scan()
        total = s["scan"]["bytes_read_total"]
        ix.refresh(force=True)
        s = ix.summary(NOW)
        self.assertEqual(s["scan"]["bytes_read_last"], 0)
        self.assertEqual(s["scan"]["bytes_read_total"], total)

    def test_truncation_drops_the_files_keys_and_rescans(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 90, mid="a", rid="a"), fc.line(NOW - 80, mid="b", rid="b")])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["d7"]["turns"], 2)
        self.tree.write("p/a.jsonl", [fc.line(NOW - 70, mid="c", rid="c")])
        ix.refresh(force=True)
        s = ix.summary(NOW)
        self.assertEqual(s["windows"]["d7"]["turns"], 1)
        self.assertEqual(s["scan"]["full_rescans"], 1)

    def test_inode_replacement_rescans(self):
        p = self.tree.write("p/a.jsonl", [fc.line(NOW - 90, mid="a", rid="a")])
        ix, s = self.scan()
        repl = os.path.join(os.path.dirname(p), "new.tmp")
        with open(repl, "wb") as f:   # a longer file under the same name, a different inode
            f.write(("".join(fc.dumps(fc.line(NOW - 60 - i, mid=f"n{i}", rid=f"n{i}")) for i in range(3))).encode())
        os.rename(repl, p)
        ix.refresh(force=True)
        s = ix.summary(NOW)
        self.assertEqual(s["windows"]["d7"]["turns"], 3)
        self.assertEqual(s["scan"]["full_rescans"], 1)

    def test_deletion_drops_the_files_turns(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 90, mid="a", rid="a")])
        p2 = self.tree.write("p/b.jsonl", [fc.line(NOW - 80, mid="b", rid="b")])
        ix, s = self.scan()
        os.remove(p2)
        ix.refresh(force=True)
        s = ix.summary(NOW)
        self.assertEqual(s["windows"]["d7"]["turns"], 1)
        self.assertEqual(s["scan"]["files"], 1)

    def test_a_dropped_duplicate_leaves_the_other_files_copy(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="m", rid="r", out=4)])
        p2 = self.tree.write("p/b.jsonl", [fc.line(NOW - 60, mid="m", rid="r", out=8)])
        ix, _ = self.scan()
        os.remove(p2)
        ix.refresh(force=True)
        self.assertEqual(self.turns(ix)[0][3], 4)

    def test_a_symlink_is_skipped(self):
        real = self.tree.write("elsewhere/a.jsonl", [fc.line(NOW - 90)])
        os.makedirs(self.tree.path("p"), exist_ok=True)
        os.symlink(real, self.tree.path("p/link.jsonl"))
        os.remove(real)
        self.tree.write("elsewhere/a.jsonl", [fc.line(NOW - 90, mid="z", rid="z")])
        ix, s = self.scan()
        # only the real file under 'elsewhere' counts; the link is not followed
        self.assertEqual(s["scan"]["files"], 1)

    def test_an_unreadable_file_is_counted_and_never_raises(self):
        p = self.tree.write("p/a.jsonl", [fc.line(NOW - 90)])
        self.tree.write("p/b.jsonl", [fc.line(NOW - 80, mid="b", rid="b")])
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, 0o600)
        ix, s = self.scan()
        self.assertEqual(s["scan"]["unreadable"], 1)
        self.assertEqual(s["windows"]["d7"]["turns"], 1)

    def test_a_missing_root_is_state_missing_with_no_numbers(self):
        ix = cu.ClaudeUsageIndex(os.path.join(self.tmp.name, "nope"), clock=lambda: 0.0, wall=lambda: NOW)
        ix.refresh(force=True)
        s = ix.summary(NOW)
        self.assertEqual(s["state"], "missing")
        self.assertIsNone(s["windows"])

    def test_an_empty_root_is_state_empty_not_zero_windows(self):
        ix, s = self.scan()
        self.assertEqual(s["state"], "empty")
        self.assertIsNone(s["windows"])

    def test_refresh_within_the_ttl_does_not_rescan(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 90, mid="a", rid="a")])
        ix = self.idx()
        ix.refresh()
        self.tree.append("p/a.jsonl", [fc.line(NOW - 60, mid="b", rid="b")])
        self.mono[0] += cu.USAGE_TTL - 1
        ix.refresh()
        self.assertEqual(ix.summary(NOW)["windows"]["d7"]["turns"], 1)
        self.mono[0] += 2
        ix.refresh()
        self.assertEqual(ix.summary(NOW)["windows"]["d7"]["turns"], 2)

    def test_the_ttl_constant_is_at_least_15_seconds(self):
        self.assertGreaterEqual(cu.USAGE_TTL, 15)


class WindowTests(Base):
    def test_today_is_cut_at_local_midnight_in_utc(self):
        with tz("UTC"):
            mid = calendar.timegm((2026, 11, 4, 0, 0, 0))
            self.tree.write("p/a.jsonl", [fc.line(mid - 1, mid="a", rid="a", inp=1, out=1, cw=0, cr=0),
                                          fc.line(mid, mid="b", rid="b", inp=10, out=10, cw=0, cr=0)])
            ix, s = self.scan()
            self.assertEqual(s["windows"]["today"]["new"], 20)

    def test_today_is_cut_at_local_midnight_in_chicago(self):
        with tz("America/Chicago"):
            mid = calendar.timegm((2026, 11, 4, 6, 0, 0))   # 00:00 CST
            self.tree.write("p/a.jsonl", [fc.line(mid - 1, mid="a", rid="a", inp=1, out=1, cw=0, cr=0),
                                          fc.line(mid, mid="b", rid="b", inp=10, out=10, cw=0, cr=0)])
            ix, s = self.scan()
            self.assertEqual(s["windows"]["today"]["new"], 20)

    def test_a_25_hour_day_counts_the_whole_local_day(self):
        with tz("America/Chicago"):
            now = calendar.timegm((2026, 11, 2, 5, 30, 0))   # 23:30 CST at the end of the 25-hour day (DST ended 07:00 UTC)
            start = calendar.timegm((2026, 11, 1, 5, 0, 0))  # 00:00 CDT; a naive now-minus-86400 would start 30 min later
            self.tree.write("p/a.jsonl", [
                fc.line(start - 1, mid="a", rid="a", inp=1, out=0, cw=0, cr=0),
                fc.line(start + 1, mid="b", rid="b", inp=10, out=0, cw=0, cr=0),
                fc.line(start + 24 * 3600 - 60, mid="c", rid="c", inp=100, out=0, cw=0, cr=0)])   # 23:59 CDT, 24 h after start
            ix = self.idx(wall=now + 60)
            ix.refresh(force=True)
            self.assertEqual(ix.summary(now)["windows"]["today"]["new"], 110)

    def test_the_24_hour_and_7_day_edges_are_inclusive(self):
        self.tree.write("p/a.jsonl", [
            fc.line(NOW - DAY, mid="a", rid="a", inp=1, out=0, cw=0, cr=0),
            fc.line(NOW - DAY - 1, mid="b", rid="b", inp=10, out=0, cw=0, cr=0),
            fc.line(NOW - 7 * DAY, mid="c", rid="c", inp=100, out=0, cw=0, cr=0),
            fc.line(NOW - 7 * DAY - 1, mid="d", rid="d", inp=1000, out=0, cw=0, cr=0)])
        ix, s = self.scan()
        self.assertEqual(s["windows"]["h24"]["new"], 1)
        self.assertEqual(s["windows"]["d7"]["new"], 1 + 10 + 100)

    def test_cache_hit_rate_arithmetic_and_null_on_a_zero_denominator(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, inp=10, out=5, cw=30, cr=60)])
        ix, s = self.scan()
        self.assertAlmostEqual(s["cache_hit_rate"], 60 / (60 + 30 + 10))
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, inp=0, out=5, cw=0, cr=0)])
        ix, s = self.scan(self.idx())
        self.assertIsNone(s["cache_hit_rate"])

    def test_new_tokens_never_include_cache_reads(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, inp=1, out=2, cw=3, cr=1_000_000)])
        ix, s = self.scan()
        w = s["windows"]["d7"]
        self.assertEqual(w["new"], 6)
        self.assertEqual(w["cache_read"], 1_000_000)
        self.assertEqual(w["processed"], 1_000_006)

    def test_hand_computed_standard_tree(self):
        fc.standard_tree(self.root, NOW)
        ix, s = self.scan()
        self.assertEqual(s["windows"]["today"]["new"], fc.EXPECT["today_new"])
        self.assertEqual(s["windows"]["d7"]["new"], fc.EXPECT["d7_new"])
        self.assertEqual(s["windows"]["d7"]["cache_read"], fc.EXPECT["d7_cache_read"])
        self.assertEqual(s["windows"]["d7"]["turns"], fc.EXPECT["turns_d7"])

    def test_partial_coverage_carries_the_real_start(self):
        first = NOW - 10 * DAY
        self.tree.write("p/a.jsonl", [fc.line(first, mid="a", rid="a"), fc.line(NOW - 60, mid="b", rid="b")])
        ix, s = self.scan()
        d = ix.detail("usage", NOW)
        self.assertTrue(d["windows"]["d30"]["partial"])
        self.assertFalse(d["windows"]["d7"]["partial"])
        self.assertEqual(d["coverage_since"], first)
        self.assertEqual(d["coverage_date"], time.strftime("%Y-%m-%d", time.localtime(first)))

    def test_seven_days_is_partial_when_history_is_shorter(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 3 * DAY, mid="a", rid="a"), fc.line(NOW - 60, mid="b", rid="b")])
        ix, s = self.scan()
        self.assertTrue(s["windows"]["d7"]["partial"])
        self.assertEqual(s["coverage_since"], NOW - 3 * DAY)

    def test_peak_hour_is_null_below_24_hours_of_coverage(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 3600, mid="a", rid="a")])
        ix, _ = self.scan()
        self.assertIsNone(ix.detail("usage", NOW)["peak_hour"])

    def test_peak_hour_is_the_local_hour_with_most_new_tokens(self):
        for zone, local_hour in (("UTC", 9), ("America/Chicago", 3)):
            with self.subTest(zone), tz(zone):
                self.setUp()
                utc_nine = calendar.timegm((2026, 11, 3, 9, 30, 0))
                self.tree.write("p/a.jsonl", [fc.line(utc_nine, mid="a", rid="a", inp=1000, out=0, cw=0, cr=0),
                                              fc.line(NOW - 5 * DAY, mid="b", rid="b", inp=1, out=0, cw=0, cr=0)])
                ix, _ = self.scan()
                self.assertEqual(ix.detail("usage", NOW)["peak_hour"]["hour"], local_hour)

    def test_sessions_today_counts_distinct_session_ids_since_midnight(self):
        with tz("UTC"):
            mid = calendar.timegm((2026, 11, 4, 0, 0, 0))
            self.tree.write("p/a.jsonl", [fc.line(mid + 5, mid="a", rid="a", session="s1"),
                                          fc.line(mid + 6, mid="b", rid="b", session="s1"),
                                          fc.line(mid + 7, mid="c", rid="c", session="s2"),
                                          fc.line(mid - 5, mid="d", rid="d", session="s3")])
            ix, s = self.scan()
            self.assertEqual(s["sessions_today"], 2)

    def test_longest_session_is_the_wall_clock_span(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 7200, mid="a", rid="a", session="s1"),
                                      fc.line(NOW - 3600, mid="b", rid="b", session="s1"),
                                      fc.line(NOW - 900, mid="c", rid="c", session="s2"),
                                      fc.line(NOW - 600, mid="d", rid="d", session="s2")])
        ix, _ = self.scan()
        ls = ix.detail("usage", NOW)["longest_session"]
        self.assertEqual(ls["seconds"], 3600)
        self.assertEqual(ls["turns"], 2)

    def test_busiest_project_is_by_new_tokens_over_seven_days(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="a", rid="a", inp=5, out=0, cw=0, cr=0, cwd="/x/small"),
                                      fc.line(NOW - 50, mid="b", rid="b", inp=500, out=0, cw=0, cr=0, cwd="/x/big"),
                                      fc.line(NOW - 40, mid="c", rid="c", inp=9, out=0, cw=0, cr=0, cwd="/x/big/")])
        ix, _ = self.scan()
        rows = ix.detail("projects", NOW)["rows"]
        self.assertEqual([r["project"] for r in rows], ["big", "small"])
        self.assertEqual(rows[0]["new"], 509)

    def test_model_shares_are_of_new_tokens_over_seven_days(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, mid="a", rid="a", inp=75, out=0, cw=0, cr=0, model="claude-opus-5-5"),
                                      fc.line(NOW - 50, mid="b", rid="b", inp=25, out=0, cw=0, cr=0, model="claude-sonnet-5-5")])
        ix, s = self.scan()
        shares = {m["model"]: m["share"] for m in s["models"]}
        self.assertAlmostEqual(shares["claude-opus-5-5"], 0.75)
        self.assertAlmostEqual(shares["claude-sonnet-5-5"], 0.25)

    def test_burn_is_the_last_hour_and_the_24_hour_average(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 600, mid="a", rid="a", inp=240, out=0, cw=0, cr=0),
                                      fc.line(NOW - 7200, mid="b", rid="b", inp=2400, out=0, cw=0, cr=0)])
        ix, s = self.scan()
        self.assertEqual(s["burn"]["last_60m_new"], 240)
        self.assertAlmostEqual(s["burn"]["hourly_avg_24h"], (240 + 2400) / 24)

    def test_hourly_and_daily_buckets_have_the_right_counts_and_no_fake_zeros(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 3 * DAY, mid="a", rid="a"), fc.line(NOW - 600, mid="b", rid="b")])
        ix, _ = self.scan()
        d = ix.detail("usage", NOW)
        self.assertEqual(len(d["hourly"]), 24)
        self.assertEqual(len(d["daily"]), 30)
        before = [b for b in d["daily"] if b["new"] is None]
        self.assertGreaterEqual(len(before), 25)   # days before the first transcript: no data, not zero
        self.assertEqual(sum(1 for b in d["daily"] if b["new"] is not None), 4)


class PrivacyAndCostTests(Base):
    def test_prompt_text_never_enters_any_output_or_the_repr(self):
        fc.standard_tree(self.root, NOW)
        ix, s = self.scan()
        blob = json.dumps([s, ix.detail("usage", NOW), ix.detail("models", NOW), ix.detail("projects", NOW)]) + repr(ix)
        blob += repr(vars(ix)) + repr(sorted(ix._merged().items()))
        self.assertNotIn(fc.PROMPT_SENTINEL, blob)

    def test_the_session_id_and_project_stay_off_the_tile_summary(self):
        self.tree.write("p/a.jsonl", [fc.line(NOW - 60, session=fc.SESSION_SENTINEL)])
        ix, s = self.scan()
        blob = json.dumps(s)
        self.assertNotIn(fc.SESSION_SENTINEL, blob)
        self.assertNotIn(fc.PROJECT_SENTINEL, blob)

    def test_the_fixture_files_are_byte_identical_before_and_after(self):
        fc.standard_tree(self.root, NOW)
        before = self.tree.hashes()
        ix, _ = self.scan()
        ix.refresh(force=True)
        self.assertEqual(before, self.tree.hashes())

    def test_a_cold_scan_of_20_mb_is_bounded_and_the_next_refresh_reads_nothing(self):
        lines = "".join(fc.dumps(fc.line(NOW - 60 - i, mid=f"m{i}", rid=f"r{i}", text="x" * 400)) for i in range(40000))
        self.tree.write("p/big.jsonl", [], raw=lines.encode())
        self.assertGreater(os.path.getsize(self.tree.path("p/big.jsonl")), 20 * 1024 * 1024)
        ix = self.idx()
        t0 = time.monotonic()
        ix.refresh(force=True)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(ix.summary(NOW)["windows"]["d7"]["turns"], 40000)
        ix.refresh(force=True)
        self.assertEqual(ix.summary(NOW)["scan"]["bytes_read_last"], 0)

    def test_scan_blocks_carry_counts_and_no_paths(self):
        fc.standard_tree(self.root, NOW)
        ix, s = self.scan()
        self.assertEqual(set(s["scan"]), {"files", "bytes_read_total", "bytes_read_last", "full_rescans", "bad_lines",
                                          "skipped", "unreadable"})
        self.assertNotIn(self.tmp.name, json.dumps([s, ix.detail("usage", NOW)]))

    def test_an_unknown_detail_id_is_none(self):
        ix, _ = self.scan()
        self.assertIsNone(ix.detail("nope", NOW))


if __name__ == "__main__":
    unittest.main()
