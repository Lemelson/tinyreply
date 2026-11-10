"""Near-comment display boundary: real SQLite, no network or terminal interpretation."""
import copy
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import clock
import config
import match
import report
import store


class NearCommentDisplay(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "core.db"
        self.now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        patcher = mock.patch.object(clock, "now", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = store.connect(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        reel = config.Reel(name="арена", media_id="111", permalink="", active=True,
            keywords=("арена",), also_match=(), max_words=5,
            link="https://example.com/a", text="{greeting} {link}")
        self.cfg = config.Config(api_version="v26.0", sending=True, poll_interval_sec=90,
            max_sends_per_hour=60, public_reply=True,
            profile_url="https://www.instagram.com/owner/", greeting="Привет",
            late_greeting="Привет", late_after_minutes=60, public_phrases=("Готово",),
            reels=(reel,), alerts=config.Alerts(repeat_min=120, digest_hour_utc=6))
        self.matcher = match.Matcher(reel)
        store.arm(self.conn, "unlimited")
        store.kv_set_many(self.conn, {"me_user_id":"9000", "me_id":"9001", "me_username":"owner",
            "me_checked_at":clock.iso(), "tg_configured":1, "token":"synthetic-only",
            "last_cycle_ok_at":clock.iso(), "last_now":clock.iso(),
            "reel_state:111":"ok", "reel_fails:111":0,
            "head_ok_at:111":clock.iso(), "full_sweep_at:111":clock.iso()})
        lock = open(self.path.parent / "core.lock", "w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)

    def add_near(self, text):
        status, reason = match.decide(self.matcher, text, True, False)
        self.assertEqual((status, reason), ("seen", "near:question"))
        self.assertTrue(store.add_comment(self.conn, "near-1", "111", "reader", "reader", text,
            clock.iso(self.now - timedelta(seconds=60)), status, reason))
        return report.build(self.conn, self.cfg, self.now)

    def display_line(self, data):
        lines = report.render(data).splitlines()
        found = [line for line in lines if line.startswith("  @reader:")]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_named_terminal_controls_are_visible_in_one_near_line(self):
        data = self.add_near("почему арена?\x1b[2J\x07\n\t\r\x7f\x85")
        self.assertEqual(self.display_line(data),
            "  @reader: «почему арена?\\x1b[2J\\x07\\x0a\\x09\\x0d\\x7f\\x85» (question)")

    def test_every_c0_control_has_a_visible_body_representation(self):
        data = self.add_near("почему арена?" + "".join(map(chr, range(32))))
        expected = (r"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
                    r"\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f")
        self.assertEqual(self.display_line(data), "  @reader: «почему арена?" + expected + "» (question)")

    def test_del_and_every_c1_control_have_a_visible_body_representation(self):
        data = self.add_near("почему арена?" + "".join(map(chr, range(127, 160))))
        expected = (r"\x7f\x80\x81\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x8b\x8c\x8d\x8e\x8f"
                    r"\x90\x91\x92\x93\x94\x95\x96\x97\x98\x99\x9a\x9b\x9c\x9d\x9e\x9f")
        self.assertEqual(self.display_line(data), "  @reader: «почему арена?" + expected + "» (question)")

    def test_ordinary_russian_and_emoji_stay_verbatim(self):
        text = "почему арена? 🏀 👩🏽‍💻 ёж"
        data = self.add_near(text)
        self.assertEqual(self.display_line(data), "  @reader: «почему арена? 🏀 👩🏽‍💻 ёж» (question)")
        self.assertEqual(data["near"]["newest"][0]["text"], text)

    def test_render_preserves_full_json_native_state_match_and_digest(self):
        text = "почему арена?\x1b\x07\n\t\r\x85"
        data = self.add_near(text)
        before = copy.deepcopy(data)
        json_before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        state_before = list(self.conn.iterdump())
        digest_before = report.digest(data)
        self.assertEqual(self.display_line(data),
            "  @reader: «почему арена?\\x1b\\x07\\x0a\\x09\\x0d\\x85» (question)")
        self.assertEqual(data, before)
        self.assertEqual(json.dumps(data, ensure_ascii=False, sort_keys=True), json_before)
        self.assertEqual(json.loads(json_before)["near"]["newest"][0]["text"], text)
        self.assertEqual(list(self.conn.iterdump()), state_before)
        self.assertEqual(report.digest(data), digest_before)
        self.assertEqual(match.decide(self.matcher, text, True, False), ("seen", "near:question"))
        reopened = store.connect(self.path, readonly=True)
        try:
            self.assertEqual(report.build(reopened, self.cfg, self.now), before)
        finally:
            reopened.close()

    def test_literal_backslash_notation_is_not_reescaped(self):
        data = self.add_near(r"почему арена? \x1b \n")
        self.assertEqual(self.display_line(data), r"  @reader: «почему арена? \x1b \n» (question)")


if __name__ == "__main__":
    unittest.main()
