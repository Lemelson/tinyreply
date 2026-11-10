"""I02: durable Telegram outcome order and confirmation recovery with real SQLite.

Only the remote send boundary is synthetic. Authorizer faults are native SQLITE_AUTH;
page exhaustion is native SQLITE_FULL in a temporary database.
"""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import clock
import notify
import report
import store
from tests.test_notify import CFG

START = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)
CREDS = ("synthetic-i02-token", "synthetic-i02-chat")


class NotifyCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="i02-notify-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "core.db"
        self.conn = store.connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        store.init(self.conn)
        self.now = START
        patcher = mock.patch.object(clock, "now", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = io.StringIO()
        redirect = contextlib.redirect_stderr(self.logs)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.requests = []
        self.reply = None
        sender = mock.patch.object(notify, "send_message", self.send)
        sender.start()
        self.addCleanup(sender.stop)
        self.notifier = notify.Notifier(self.conn, lambda: CREDS, "http://127.0.0.1:1")

    def send(self, base, token, chat, text):
        self.assertFalse(self.conn.in_transaction, "HTTP must run outside the write transaction")
        self.requests.append(text)
        return self.reply

    def reopen(self):
        self.conn.close()
        self.conn = store.connect(self.path)
        self.notifier = notify.Notifier(self.conn, lambda: CREDS, "http://127.0.0.1:1")

    def snapshot(self):
        with contextlib.closing(store.connect(self.path, readonly=True)) as fresh:
            return {"kv": dict(fresh.execute("SELECT key,value FROM kv")),
                    "outbox": [dict(row) for row in fresh.execute("SELECT * FROM outbox ORDER BY id")],
                    "health": notify.health(fresh)}

    def assert_channel_state(self, failing):
        with contextlib.closing(store.connect(self.path, readonly=True)) as fresh:
            self.assertIs(notify.health(fresh)["failing"], failing)
            self.assertEqual(notify.deaf(fresh, lambda: CREDS) is not None, failing)
            self.assertEqual("no_alert_channel" in dict(report.problems(fresh, CFG, self.now)), failing)


class LogicalOutcome(NotifyCase):
    def test_last_outcome_survives_equal_forward_and_backward_wall_times(self):
        for first, delta in (("ok", -120), ("failed", -120), ("ok", 0),
                             ("failed", 0), ("ok", 120), ("failed", 120)):
            with self.subTest(first=first, delta=delta):
                self.conn.execute("DELETE FROM outbox")
                self.conn.execute("DELETE FROM kv")
                self.now = START
                self.reply = None if first == "ok" else "http 401: refused"
                self.notifier.event("first", "first outcome")
                self.notifier.flush()
                first_at = clock.iso()
                self.now += timedelta(seconds=delta)
                second = "failed" if first == "ok" else "ok"
                self.reply = None if second == "ok" else "http 401: refused"
                self.notifier.event("second", "second outcome")
                self.notifier.flush()
                self.reopen()
                snap = self.snapshot()
                self.assertEqual(snap["kv"].get("tg_last_outcome"), second)
                self.assertEqual(snap["kv"]["tg_ok_at" if first == "ok" else "tg_failed_at"], first_at)
                self.assertEqual(snap["kv"]["tg_ok_at" if second == "ok" else "tg_failed_at"], clock.iso())
                self.assert_channel_state(second == "failed")

    def test_legacy_or_invalid_outcome_is_conservative_until_next_result(self):
        for ok_offset, outcome in ((None, None), (-120, None), (0, None), (120, None), (120, "unknown")):
            with self.subTest(ok_offset=ok_offset, outcome=outcome):
                self.conn.execute("DELETE FROM outbox")
                self.conn.execute("DELETE FROM kv")
                store.kv_set(self.conn, "tg_configured", "1")
                store.kv_set(self.conn, "tg_failed_at", clock.iso(START))
                if ok_offset is not None:
                    store.kv_set(self.conn, "tg_ok_at", clock.iso(START + timedelta(seconds=ok_offset)))
                if outcome is not None:
                    store.kv_set(self.conn, "tg_last_outcome", outcome)
                self.reopen()
                self.assert_channel_state(True)
                self.notifier.event("recovered", "next confirmed delivery")
                self.assertTrue(self.notifier.flush())
                self.reopen()
                self.assert_channel_state(False)

    def test_empty_and_success_only_legacy_health_remain_healthy(self):
        self.assert_channel_state(False)
        store.kv_set(self.conn, "tg_configured", "1")
        store.kv_set(self.conn, "tg_ok_at", clock.iso())
        self.reopen()
        self.assert_channel_state(False)


class ConfirmationRecovery(NotifyCase):
    def seed_failure(self):
        store.kv_set(self.conn, "tg_configured", "1")
        store.kv_set(self.conn, "tg_failed_at", clock.iso(START))
        store.kv_set(self.conn, "tg_last_outcome", "failed")
        self.now -= timedelta(seconds=120)

    def assert_complete_result(self, accepted_at):
        snap = self.snapshot()
        self.assertEqual(snap["kv"].get("tg_ok_at"), accepted_at)
        self.assertEqual(snap["kv"].get("tg_last_outcome"), "ok")
        self.assertEqual({row["sent_at"] for row in snap["outbox"]}, {accepted_at})
        self.assertFalse(self.notifier._unmarked)
        self.assert_channel_state(False)

    def test_native_full_keeps_confirmation_and_recovers_without_reposting(self):
        self.seed_failure()
        cap = self.conn.execute("PRAGMA page_count").fetchone()[0]
        self.conn.execute(f"PRAGMA max_page_count={cap}")
        for count in range(10000):
            try:
                store.outbox_add(self.conn, "filler", "x")
            except sqlite3.Error as error:
                self.assertEqual(error.sqlite_errorcode, sqlite3.SQLITE_FULL)
                break
        else:
            self.fail("temporary SQLite database did not reach native FULL")
        self.assertGreater(count, 0)
        accepted_at = clock.iso()
        self.assertIs(self.notifier.flush(), False)
        self.assertEqual(self.notifier.broken[0], "db_unwritable")
        self.assertIn("database or disk is full", self.notifier.broken[1])
        before = self.snapshot()
        self.assertTrue(self.notifier._unmarked)
        self.assertEqual({row["sent_at"] for row in before["outbox"]}, {None})
        self.assertEqual(before["kv"]["tg_last_outcome"], "failed")
        self.assertIs(self.notifier.flush(), False)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.notifier._unmarked)
        self.conn.execute("PRAGMA max_page_count=100000")
        self.now += timedelta(seconds=90)
        self.assertTrue(self.notifier.flush())
        self.assertTrue(self.notifier.flush())
        self.assertEqual(len(self.requests), 1)
        self.assert_complete_result(accepted_at)
        self.reopen()
        self.assert_channel_state(False)

    def authorizer_recovery(self, action, table):
        self.seed_failure()
        self.notifier.event("accepted", "one confirmed message")
        denied = []

        def authorize(operation, first, second, database, source):
            if operation == action and first == table:
                denied.append((operation, first))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.conn.set_authorizer(authorize)
        accepted_at = clock.iso()
        self.assertIs(self.notifier.flush(), False)
        self.assertTrue(denied, "fault must reach the exact native SQLite write boundary")
        self.assertIn("not authorized", self.logs.getvalue())
        before = self.snapshot()
        self.assertIsNone(before["outbox"][0]["sent_at"])
        self.assertNotIn("tg_ok_at", before["kv"])
        self.assertEqual(before["kv"]["tg_last_outcome"], "failed")
        self.assertIs(self.notifier.flush(), False)
        self.assertTrue(self.notifier._unmarked)
        self.assertEqual(len(self.requests), 1)
        self.conn.set_authorizer(None)
        self.now += timedelta(seconds=90)
        self.assertTrue(self.notifier.flush())
        self.assertEqual(len(self.requests), 1)
        self.assert_complete_result(accepted_at)

    def test_outbox_mark_failure_retries_the_complete_result(self):
        self.authorizer_recovery(sqlite3.SQLITE_UPDATE, "outbox")

    def test_health_write_failure_rolls_back_marks_and_retries_the_complete_result(self):
        self.authorizer_recovery(sqlite3.SQLITE_INSERT, "kv")


class UrgentAndQueue(NotifyCase):
    def test_event_returns_the_positive_id_of_its_durable_row(self):
        row_id = self.notifier.event("fatal_start", "configuration error")
        self.assertIs(type(row_id), int)
        self.assertGreater(row_id, 0)
        row = self.snapshot()["outbox"][0]
        self.assertEqual((row["id"], row["key"], row["sent_at"]), (row_id, "fatal_start", None))

    def test_failed_queue_returns_false_on_a_read_only_database(self):
        with contextlib.closing(store.connect(self.path, readonly=True)) as readonly:
            notifier = notify.Notifier(readonly, lambda: CREDS, "http://127.0.0.1:1")
            self.assertIs(notifier.event("fatal_start", "unwritable"), False)
            self.assertIs(notifier.queue_clear("db_unwritable"), False)
        self.assertEqual(self.snapshot()["outbox"], [])

    def test_raw_clear_is_durable_before_flush_and_survives_refusal_and_reopen(self):
        row_id = self.notifier.queue_clear("db_unwritable")
        self.assertIs(type(row_id), int)
        self.assertGreater(row_id, 0)
        self.reopen()   # crash boundary after durable queue, before any send
        self.reply = "http 401: refused"
        self.assertIs(self.notifier.flush(), False)
        self.assertEqual(self.snapshot()["outbox"][0]["text"], "✓ cleared: db_unwritable")
        self.reopen()
        self.reply = None
        self.assertTrue(self.notifier.flush())
        self.assertTrue(self.notifier.flush())
        self.assertEqual(self.requests, ["✓ cleared: db_unwritable", "✓ cleared: db_unwritable"])
        self.assertEqual(self.snapshot()["health"]["queued"], 0)

    def test_targeted_fatal_skips_two_full_backlog_rows(self):
        first = store.outbox_add(self.conn, "old1", "a" * 4096)
        second = store.outbox_add(self.conn, "old2", "b" * 4096)
        fatal = self.notifier.event("fatal_start", "configuration error")
        self.assertIs(self.notifier.flush(only_id=fatal), True)
        self.assertEqual(self.requests, ["ℹ fatal_start: configuration error"])
        rows = self.snapshot()["outbox"]
        self.assertEqual([row["id"] for row in rows if row["sent_at"] is None], [first, second])
        self.assertEqual(rows[-1]["sent_at"], clock.iso())
        self.assertTrue(self.notifier.flush(only_id=fatal))
        self.assertTrue(self.notifier.flush(only_id=999999))
        self.assertEqual(len(self.requests), 1)

    def test_default_flush_still_sends_only_the_oldest_batch(self):
        first = store.outbox_add(self.conn, "old1", "a" * 4096)
        second = store.outbox_add(self.conn, "old2", "b" * 4096)
        self.notifier.event("fatal_start", "configuration error")
        self.assertTrue(self.notifier.flush())
        self.assertEqual(self.requests, ["a" * 4096])
        rows = self.snapshot()["outbox"]
        self.assertEqual([row["id"] for row in rows if row["sent_at"] is not None], [first])
        self.assertEqual([row["id"] for row in rows if row["sent_at"] is None][:1], [second])

    def test_keyword_target_is_guarded_when_the_database_is_closed(self):
        self.conn.close()
        self.assertIs(self.notifier.flush(only_id=1), False)
        self.assertEqual(self.requests, [])
        self.assertIn("notify_failed", self.logs.getvalue())

    def test_pending_confirmation_blocks_urgent_post_until_the_entire_result_commits(self):
        store.kv_set(self.conn, "tg_configured", "1")
        old = store.outbox_add(self.conn, "old1", "a" * 4096)
        backlog = store.outbox_add(self.conn, "old2", "b" * 4096)
        self.conn.set_authorizer(lambda operation, first, *rest: sqlite3.SQLITE_DENY
                                 if operation == sqlite3.SQLITE_INSERT and first == "kv" else sqlite3.SQLITE_OK)
        accepted_at = clock.iso()
        self.assertIs(self.notifier.flush(), False)
        fatal = self.notifier.event("fatal_start", "urgent after pending result")
        self.assertIs(self.notifier.flush(only_id=fatal), False)
        self.assertEqual(self.requests, ["a" * 4096])
        self.assertTrue(self.notifier._unmarked)
        self.assertIsNone(self.snapshot()["outbox"][0]["sent_at"])
        self.conn.set_authorizer(None)
        self.now += timedelta(seconds=90)
        self.assertTrue(self.notifier.flush(only_id=fatal))
        self.assertEqual(self.requests, ["a" * 4096, "ℹ fatal_start: urgent after pending result"])
        rows = {row["id"]: row for row in self.snapshot()["outbox"]}
        self.assertEqual(rows[old]["sent_at"], accepted_at)
        self.assertIsNone(rows[backlog]["sent_at"])
        self.assertEqual(rows[fatal]["sent_at"], clock.iso())


if __name__ == "__main__":
    unittest.main()
