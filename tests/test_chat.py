"""chat.py: summary + detail from the portfolio_chat data dir. Read-only; no ip_hash, no contact content."""
import hashlib
import json
import os
import tempfile
import unittest

from corellia_hud import chat
from tests.support import fixtures as fx

NOW = 1_800_000_000.0


def tree_hash(root):
    h = hashlib.sha256()
    for dirpath, dirs, files in sorted(os.walk(root)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                h.update(("link:" + p).encode())
                continue
            try:
                with open(p, "rb") as fh:
                    h.update(p.encode() + fh.read())
            except OSError:
                h.update(("unreadable:" + p).encode())
    return h.hexdigest()


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = fx.make_chat_tree(os.path.join(self.tmp.name, "data"), NOW)
        self.adb = fx.make_analytics(os.path.join(self.tmp.name, "a.db"), [])

    def tearDown(self):
        for sub in ("contacts", "contacts_quarantine"):
            d = os.path.join(self.root, sub)
            if os.path.isdir(d):
                os.chmod(d, 0o700)
                for f in os.listdir(d):
                    os.chmod(os.path.join(d, f), 0o600)
        self.tmp.cleanup()

    def summary(self):
        return chat.chat_summary(self.root, self.adb, NOW)

    def detail(self):
        return chat.chat_detail(self.root, self.adb, NOW)


class SummaryTests(ChatBase):
    def test_summary_counts_match_hand_computed_values(self):
        s = self.summary()
        self.assertEqual(s["state"], "ok")
        self.assertEqual(s["conversations_24h"], 1)       # c1
        self.assertEqual(s["conversations_7d"], 3)        # c1 c2 c3
        self.assertEqual(s["turns_7d"], 3 + 2 + 1)
        self.assertEqual(s["blocked_7d"], 1)              # c3
        self.assertAlmostEqual(s["avg_response_ms"], (3000 + 5000 + 1000) / 6)

    def test_summary_carries_no_message_text_or_ip_hash(self):
        blob = json.dumps(self.summary())
        for needle in (fx.VISITOR_SENTINEL, fx.IP_HASH_SENTINEL, fx.CONTACT_EMAIL_SENTINEL, "answer"):
            self.assertNotIn(needle, blob)

    def test_contacts_are_counted_with_files_at_mode_000_and_never_opened(self):
        for sub in ("contacts", "contacts_quarantine"):
            d = os.path.join(self.root, sub)
            for f in os.listdir(d):
                os.chmod(os.path.join(d, f), 0)
        s = self.summary()
        self.assertEqual(s["contacts"], {"state": "ok", "accepted": 3, "quarantined": 5})
        self.assertNotIn(fx.CONTACT_EMAIL_SENTINEL, json.dumps(s))

    def test_funnel_without_consent_row_is_recorded_false_never_a_rate(self):
        fx.add_rows(self.adb, "INSERT INTO daily_interactions (site,date,kind,count) VALUES (?,?,?,?)",
                    [("kellogg", "2027-01-14", "chat_open", 4)])
        f = self.summary()["funnel"]
        self.assertEqual((f["opens"], f["consents"], f["recorded"]), (4, 0, False))
        self.assertNotIn("rate", f)

    def test_funnel_with_a_consent_row_is_recorded_true(self):
        fx.add_rows(self.adb, "INSERT INTO daily_interactions (site,date,kind,count) VALUES (?,?,?,?)",
                    [("kellogg", "2027-01-14", "chat_open", 4), ("kellogg", "2027-01-14", "chat_consent", 2),
                     ("talkingrock", "2027-01-14", "chat_open", 99)])
        f = self.summary()["funnel"]
        self.assertEqual((f["opens"], f["consents"], f["recorded"]), (4, 2, True))

    def test_missing_analytics_db_is_an_explicit_funnel_state(self):
        s = chat.chat_summary(self.root, os.path.join(self.tmp.name, "nope.db"), NOW)
        self.assertEqual(s["funnel"]["state"], "missing")
        self.assertEqual(s["state"], "ok")                 # conversations still work


class DetailTests(ChatBase):
    def test_conversations_in_30_days_newest_first(self):
        d = self.detail()
        ids = [c["id"] for c in d["conversations"]]
        self.assertEqual(ids, [fx.CHAT_UUIDS[n] for n in ("c1", "c2", "c3", "c4")])   # c5 is 45 days old

    def test_message_text_is_present_and_ip_hash_is_not(self):
        d = self.detail()
        blob = json.dumps(d)
        self.assertIn(fx.VISITOR_SENTINEL, blob)           # positive control
        self.assertNotIn(fx.IP_HASH_SENTINEL, blob)
        self.assertNotIn("ip_hash", blob)
        self.assertNotIn(fx.CONTACT_EMAIL_SENTINEL, blob)

    def test_conversation_fields(self):
        c = self.detail()["conversations"][0]
        self.assertEqual(c["total_turns"], 3)
        self.assertEqual(c["domains_used"], ["professional"])
        self.assertEqual(c["total_response_time_ms"], 3000.0)
        self.assertIsNone(c["blocked_at_layer"])
        self.assertEqual(c["messages"][0]["role"], "user")
        blocked = self.detail()["conversations"][2]
        self.assertEqual(blocked["blocked_at_layer"], "L3")

    def test_caps_20_conversations_40_messages_and_4000_chars(self):
        day = os.path.join(self.root, "conversations", fx.iso_z(NOW - 3600)[:10])
        for i in range(25):
            doc = fx.conversation_doc("c1", NOW, 3600 + i, 1, 100.0, None)
            doc["id"] = f"extra-{i:03d}"
            with open(os.path.join(day, f"conv_extra{i}.json"), "w") as f:
                json.dump(doc, f)
        big = fx.conversation_doc("c1", NOW, 10, 1, 1.0, None)
        big["id"] = "big"
        big["messages"] = [{"role": "user", "content": "x" * 9000, "timestamp": big["started_at"]}] * 60
        with open(os.path.join(day, "conv_big.json"), "w") as f:
            json.dump(big, f)
        d = self.detail()
        self.assertEqual(len(d["conversations"]), 20)
        first = d["conversations"][0]
        self.assertEqual(first["id"], "big")
        self.assertEqual(len(first["messages"]), 40)
        self.assertTrue(first["messages_truncated"])
        self.assertLessEqual(len(first["messages"][0]["content"]), 4000 + 40)
        self.assertTrue(first["messages"][0]["truncated"])

    def test_malformed_oversize_symlink_and_odd_directories_are_skipped_and_counted(self):
        day = os.path.join(self.root, "conversations", fx.iso_z(NOW - 3600)[:10])
        with open(os.path.join(day, "conv_bad.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(day, "conv_huge.json"), "w") as f:
            f.write(json.dumps({"id": "huge", "pad": "x" * (chat.MAX_FILE_BYTES + 10)}))
        os.symlink("/etc/hostname", os.path.join(day, "conv_link.json"))
        os.makedirs(os.path.join(self.root, "conversations", "not-a-date"))
        with open(os.path.join(self.root, "conversations", "not-a-date", "conv_x.json"), "w") as f:
            f.write("{}")
        d = self.detail()
        self.assertEqual(d["state"], "ok")
        self.assertEqual(d["skipped"], 4)
        self.assertEqual(len(d["conversations"]), 4)

    def test_documents_missing_required_keys_are_skipped(self):
        day = os.path.join(self.root, "conversations", fx.iso_z(NOW - 3600)[:10])
        with open(os.path.join(day, "conv_nostart.json"), "w") as f:
            json.dump({"id": "x", "messages": []}, f)
        self.assertEqual(self.detail()["skipped"], 1)

    def test_a_directory_listing_is_the_only_thing_that_touches_the_files_it_never_writes(self):
        before = tree_hash(self.root)
        self.summary()
        self.detail()
        self.assertEqual(tree_hash(self.root), before)

    def test_missing_data_dir_is_missing(self):
        d = chat.chat_detail(os.path.join(self.tmp.name, "absent"), self.adb, NOW)
        self.assertEqual(d["state"], "missing")
        self.assertEqual(d["conversations"], [])

    def test_unreadable_conversations_dir_is_unreachable(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        conv = os.path.join(self.root, "conversations")
        os.chmod(conv, 0)
        try:
            self.assertEqual(self.detail()["state"], "unreachable")
            self.assertEqual(self.summary()["state"], "unreachable")
        finally:
            os.chmod(conv, 0o700)

    def test_detail_reports_the_summary_funnel_and_contacts(self):
        d = self.detail()
        self.assertEqual(d["contacts"]["accepted"], 3)
        self.assertEqual(d["summary"]["conversations_7d"], 3)
        self.assertIn("recorded", d["funnel"])


if __name__ == "__main__":
    unittest.main()
