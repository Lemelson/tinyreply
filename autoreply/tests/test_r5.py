"""Round r5 (audit-claude/r5): R5-01, a fresh database must not call every old request "people lost".

A comment that was already past the 7-day window when the bot first saw it is `expired`/`too_old`, not a loss.
Rows stored before that reason existed (`expired`/`window`, the live database of 10.11.2026) are told apart by
`first_seen_at`; the old marker `told_expired` (which counted them) is not reused."""
import dataclasses
import hashlib
import hmac
import importlib.util
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401  loopback guard

import clock
import engine
import notify
import report
import store
from tests.test_engine import DAY, HOUR, EngineCase, comment
from tests.test_store import CFG, ME, START

REEL = CFG.reels[0]
LOST = "people lost: their comments are past the 7-day window"


class FreshDatabase(EngineCase):
    """The first full sweep of a reel with history, on an empty database, sending closed (budget 0)."""

    def test_old_requests_are_too_old_not_lost_and_a_later_real_loss_is_told(self):
        old = [comment(f"old{n}", age=8 * DAY + n * HOUR) for n in range(5)]
        fresh = [comment("new1", age=HOUR), comment("edge", age=7 * DAY - 2 * HOUR)]
        self.boot(old + fresh, budget=None)
        self.cycle()
        self.assertEqual({self.status(f"old{n}") for n in range(5)}, {("expired", "too_old")})
        self.assertEqual(self.status("edge"), ("pending", ""))
        self.assertEqual(self.events("expired"), [])                     # nobody was let down
        self.cycle(after=3 * HOUR)                                       # the edge comment's window closes unserved
        self.assertEqual(self.status("edge"), ("expired", "window"))
        self.assertEqual(self.events("expired"), [f"1 {LOST} (1 in all)"])
        self.assertEqual(self.kv("told_lost"), "1")

    def test_a_config_edit_that_makes_an_old_seen_comment_a_request_is_too_old(self):
        self.boot([comment("c1", text="привет", age=8 * DAY, should_match=False)], budget=None)
        self.cycle()
        self.assertEqual(self.status("c1"), ("seen", ""))
        self.restart(reels=[{"keywords": ["привет"]}])
        self.cycle(after=HOUR)
        self.assertEqual(self.status("c1"), ("expired", "too_old"))
        self.assertEqual(self.events("expired"), [])


class LiveDatabaseShape(unittest.TestCase):
    """The live database of 10.11.2026: rows of the old code, `expired`/`window` for comments that were too old when
    first seen, `told_expired` at their count and the "N people lost" line still waiting in the outbox."""

    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.now = START
        self.enterContext(mock.patch.object(clock, "now", lambda: self.now))
        self.enterContext(mock.patch.object(clock, "monotonic", lambda: 10000.0))
        self.conn = store.connect(os.path.join(folder, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.cfg = dataclasses.replace(CFG, public_reply=False)
        swept = self.now - timedelta(days=1)                             # the first sweep, a day ago
        for n in range(30):                                              # 8 to 37 days old when first seen
            self.row(f"old{n}", ts=swept - timedelta(days=8 + n), first_seen=swept, status="expired", reason="window")
        for n in range(3):
            self.row(f"wait{n}", ts=self.now - timedelta(days=6, hours=20 + n), first_seen=swept, status="pending")
        store.kv_set_many(self.conn, {"told_expired": 30, "told_expired_at": clock.iso(swept)})
        store.outbox_add(self.conn, "expired", f"ℹ expired: 30 {LOST} (30 in all)")

    def row(self, comment_id, ts, first_seen, status, reason=""):
        self.conn.execute(
            "INSERT INTO comments (comment_id, media_id, user_id, username, text, ts, status, reason, first_seen_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (comment_id, REEL.media_id, "u_" + comment_id, comment_id,
                                                          "арена", clock.iso(ts), status, reason,
                                                          clock.iso(first_seen), clock.iso(first_seen)))

    def start(self):
        notifier = notify.Notifier(self.conn, lambda: (None, None), "http://127.0.0.1:1", repeat_min=60)
        bot = engine.Engine(self.cfg, self.conn, None, notifier)
        bot.me = ME
        return bot

    def said(self, key):
        return [text for row_key, text in self.conn.execute("SELECT key, text FROM outbox WHERE sent_at IS NULL "
                                                              "ORDER BY id") if row_key == key]

    def test_the_old_count_is_withdrawn_and_the_first_real_loss_after_the_upgrade_is_told(self):
        bot = self.start()
        bot._lost_people()
        self.assertEqual(self.said("expired"), [])                       # "30 people lost" will never be delivered
        self.assertEqual(store.kv_get(self.conn, "told_lost"), "0")
        self.now += timedelta(hours=3)                                   # wait1 and wait2 leave the window unserved
        self.assertEqual(store.expire(self.conn), 2)
        bot._lost_people()
        self.assertEqual(self.said("expired"), [f"ℹ expired: 2 {LOST} (2 in all)"])
        bot._lost_people()
        self.assertEqual(len(self.said("expired")), 1)                   # told once

    def test_the_report_shows_old_rows_as_too_old_and_says_they_are_not_losses(self):
        self.now += timedelta(hours=3)
        store.expire(self.conn)
        data = report.build(self.conn, self.cfg, self.now)
        (reel,) = [reel for reel in data["reels"] if reel["media_id"] == REEL.media_id]
        self.assertEqual(sorted(map(tuple, reel["counts"])),
                         [("expired", "too_old", 30), ("expired", "window", 2), ("pending", "", 1)])
        text = report.render(data)
        self.assertIn("потеряно людей (не успели ответить за 7 дней): 2", text)
        self.assertIn("это не потери", text)


DEPLOY_HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "deploy", "github_webhook.py")


@unittest.skipUnless(os.path.exists(DEPLOY_HOOK), "deploy/github_webhook.py belongs to the private deployment")
class WebhookSignature(unittest.TestCase):
    """I-07: a malformed X-Hub-Signature-256 header is a clean 401, not a TypeError that drops the connection."""

    def test_non_ascii_and_wrong_signatures_are_401_and_the_right_one_passes(self):
        path = DEPLOY_HOOK
        spec = importlib.util.spec_from_file_location("github_webhook", path)
        webhook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(webhook)
        secret, body = b"s" * 40, b'{"repository": {"full_name": "Lemelson/ig-autoreply"}}'
        for signature in ("sha256=é", "sha256=\udcff", "sha256=" + "0" * 64, "", "ü" * 71):
            self.assertEqual(webhook.verdict(secret, "ping", signature, body)[:2], (401, "invalid-signature"))
        good = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        self.assertEqual(webhook.verdict(secret, "ping", good, body)[:2], (200, "pong"))


if __name__ == "__main__":
    unittest.main()
