"""Native SQLite regressions for I01; no network, secrets or live state."""
import dataclasses
import os
import sqlite3
import tempfile
import types
import unittest
from datetime import timedelta
from unittest import mock

import clock
import config
import engine
import meta
import notify
import store
from tests.test_store import CFG, ME, START, DM_OK, answer


class Core(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.now, self.mono = START, 10000.0
        self.enterContext(mock.patch.object(clock, "now", lambda: self.now))
        self.enterContext(mock.patch.object(clock, "monotonic", lambda: self.mono))
        self.conn = store.connect(os.path.join(self.folder, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.cfg = dataclasses.replace(CFG, public_reply=False)
        self.notifier = notify.Notifier(self.conn, lambda: ("123:synthetic", "42"), "http://127.0.0.1:1")
        self.client = types.SimpleNamespace(send_dm=lambda *args: DM_OK)
        self.bot = self.make_engine()
        self.bot.me = ME
        store.arm(self.conn, "unlimited")

    def make_engine(self, notifier=None, client=None):
        return engine.Engine(self.cfg, self.conn, client or self.client, notifier or self.notifier)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def listed(self, comment_id="c", **fields):
        return {"id": comment_id, "text": "арена", "timestamp": clock.iso(self.now - timedelta(seconds=60)),
                "from": {"id": "u_" + comment_id, "username": "person"}, **fields}

    def page(self, items, full=True):
        sweep = engine._Sweep("111", full)
        self.bot._store_page(self.cfg.reels[0], sweep, items)
        return sweep

    def history(self, start, end, outcome="ok", http=200, reason=""):
        return self.conn.execute("INSERT INTO attempts (comment_id,kind,started_at,finished_at,outcome,http,reason) "
                                 "VALUES ('old','dm',?,?,?,?,?)", (clock.iso(start), clock.iso(end) if end else None,
                                                                 outcome, http, reason)).lastrowid

    def test_f01_restart_forward_correction_cannot_shorten_spacing(self):
        self.history(self.now - timedelta(seconds=600), self.now - timedelta(seconds=600))
        self.page([self.listed()])
        self.bot = self.make_engine()
        self.bot.me, self.bot._sendable = ME, {"111"}
        self.bot._send(None)
        self.assertEqual(store.comment(self.conn, "c")["status"], "pending")
        self.advance(60)
        self.bot._send(None)
        self.assertEqual(store.comment(self.conn, "c")["status"], "done")

    def test_f01_completed_old_history_does_not_restart_another_slot(self):
        old = self.now - timedelta(hours=2)
        self.history(old, old)
        self.page([self.listed()])
        self.bot = self.make_engine()
        self.bot.me, self.bot._sendable = ME, {"111"}
        self.bot._send(None)
        self.assertEqual(store.comment(self.conn, "c")["status"], "done")

    def test_f01_cap_uses_response_completion_and_clock_margin(self):
        cfg = dataclasses.replace(self.cfg, max_sends_per_hour=1)
        self.page([self.listed()])
        self.history(self.now - timedelta(seconds=4210), self.now - timedelta(seconds=4199.999))
        self.assertEqual(store.claim_dm(self.conn, "c", cfg, ME).kind, "cap")

    def test_f01_unknown_boot_anchor_adds_deadline_without_retrying_old_comment(self):
        self.history(self.now - timedelta(hours=2), self.now - timedelta(hours=2), "uncertain", 0, "no_response")
        self.page([self.listed()])
        self.bot = self.make_engine()
        self.bot.me, self.bot._sendable = ME, {"111"}
        self.advance(60)
        self.bot._send(None)
        self.assertEqual(store.comment(self.conn, "c")["status"], "pending")
        self.advance(60)
        self.bot._send(None)
        self.assertEqual(store.comment(self.conn, "c")["status"], "done")

    def test_f01_oneshot_waits_once_and_rechecks_budget(self):
        self.history(self.now, self.now)
        self.page([self.listed()])
        self.bot = self.make_engine()
        self.bot.me, self.bot._sendable = ME, {"111"}
        slept = []
        def sleep(seconds, *args):
            slept.append(seconds)
            self.advance(seconds)
            store.arm(self.conn, 0)
        with mock.patch.object(clock, "sleep", sleep), mock.patch.object(self.bot, "_work",
                side_effect=lambda only: self.bot._send(only)), mock.patch.object(self.bot, "_announce"):
            self.bot.cycle(wait_for_startup=True)
        self.assertEqual(len(slept), 1)
        self.assertEqual(store.comment(self.conn, "c")["status"], "pending")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_f01_exception_after_post_keeps_unknown_deadline_guard_during_same_run(self):
        self.cfg = dataclasses.replace(self.cfg, max_sends_per_hour=2)
        self.bot = self.make_engine()
        self.bot.me, self.bot._sendable = ME, {"111"}
        store.arm(self.conn, "unlimited")
        self.page([self.listed("a")])
        self.client.send_dm = mock.Mock(side_effect=RuntimeError("answer lost after wire"))
        with self.assertRaises(RuntimeError):
            self.bot._send(None)
        store.recover_stale(self.conn)
        self.page([self.listed("b")])
        self.client.send_dm = lambda *args: DM_OK
        self.advance(1800)
        self.bot._send(None)
        self.assertEqual(store.comment(self.conn, "a")["status"], "uncertain")
        self.assertEqual(store.comment(self.conn, "b")["status"], "pending")
        self.advance(60)
        self.bot._send(None)
        self.assertEqual(store.comment(self.conn, "b")["status"], "done")

    def test_f01_service_cycles_keep_running_while_startup_slot_is_taken(self):
        self.history(self.now, self.now)
        self.page([self.listed()])
        self.bot = self.make_engine()
        self.bot.me, self.bot._sendable = ME, {"111"}
        with mock.patch.object(clock, "sleep") as sleep, \
                mock.patch.object(self.bot, "_work", side_effect=lambda only: self.bot._send(only)), \
                mock.patch.object(self.bot, "_announce") as announce:
            self.assertEqual(self.bot.cycle(), 0)
            sleep.assert_not_called()
            self.advance(60)
            self.assertEqual(self.bot.cycle(), 1)
            self.assertEqual(announce.call_count, 2)

    def test_f02_account_wide_refusal_never_becomes_an_item_refusal(self):
        self.page([self.listed("a"), self.listed("b")])
        first = store.claim_dm(self.conn, "a", self.cfg, ME)
        store.record_dm(self.conn, first.attempt_id, answer("global", "rate", http=429))
        self.advance(90000)
        other = store.claim_dm(self.conn, "b", self.cfg, ME)
        store.record_dm(self.conn, other.attempt_id, DM_OK)
        self.advance(90000)
        again = store.claim_dm(self.conn, "a", self.cfg, ME)
        status = store.record_dm(self.conn, again.attempt_id, answer("global", "auth", code=190))
        self.assertEqual(status, "pending")
        self.assertEqual(store.comment(self.conn, "a")["deferrals"], 2)
        self.assertEqual(store.get_send_hold(self.conn)["reason"], "send_blocked")

    def test_f03_invalid_first_text_is_repaired_and_an_edited_comment_is_read_again(self):
        for number, bad in enumerate((5, None)):
            cid = "repair" + str(number)
            self.page([self.listed(cid, text=bad)])
            self.page([self.listed(cid)])
            row = store.comment(self.conn, cid)
            self.assertEqual((row["status"], row["text"], row["reason"]), ("pending", "арена", ""))
        # C02: a person may edit a comment; a comment that was no request is decided on the text of today
        self.page([self.listed("edited", text="красиво")])
        self.assertEqual(store.comment(self.conn, "edited")["status"], "seen")
        self.page([self.listed("edited")])
        row = store.comment(self.conn, "edited")
        self.assertEqual((row["status"], row["text"]), ("pending", "арена"))
        # a listing without a text does not erase the stored one
        self.page([self.listed("kept", text="красиво")])
        self.page([self.listed("kept", text=None)])
        row = store.comment(self.conn, "kept")
        self.assertEqual((row["status"], row["text"]), ("seen", "красиво"))
        # a request that waits is not decided again: the link still goes to a person who edited the word away
        self.page([self.listed("waits")])
        self.page([self.listed("waits", text="спасибо")])
        row = store.comment(self.conn, "waits")
        self.assertEqual((row["status"], row["text"]), ("pending", "арена"))

    def test_a_seen_comment_that_did_not_change_is_not_written_again(self):
        self.page([self.listed("same", text="красиво")])
        first = store.comment(self.conn, "same")["updated_at"]
        self.advance(1800)
        self.page([self.listed("same", text="красиво", timestamp=store.comment(self.conn, "same")["ts"])])
        self.assertEqual(store.comment(self.conn, "same")["updated_at"], first)
        self.page([self.listed("same", text="красиво", timestamp=store.comment(self.conn, "same")["ts"],
                               **{"from": {"id": "u_same", "username": "renamed"}})])
        row = store.comment(self.conn, "same")
        self.assertEqual(row["username"], "renamed")
        self.assertNotEqual(row["updated_at"], first)

    def test_f03_repaired_timestamp_expires_before_claim(self):
        self.page([self.listed(timestamp=None)])
        old = clock.iso(self.now - timedelta(days=8))
        self.page([self.listed(timestamp=old)])
        row = store.comment(self.conn, "c")
        self.assertEqual((row["status"], row["reason"], row["ts"]), ("expired", "window", old))
        self.assertEqual(store.claim_dm(self.conn, "c", self.cfg, ME).kind, "skip")

    def test_f03_null_text_repair_preserves_the_original_usable_timestamp(self):
        original = clock.iso(self.now - timedelta(days=6))
        self.page([self.listed(text=None, timestamp=original)])
        self.page([self.listed()])
        self.assertEqual(store.comment(self.conn, "c")["ts"], original)

    def test_f03_terminal_history_including_old_suspect_rows_is_never_reopened(self):
        for status in ("done", "uncertain", "refused", "expired", "dup_user", "already"):
            cid = status
            self.page([self.listed(cid)])
            self.conn.execute("UPDATE comments SET status=?, reason='suspect_item:190/-' WHERE comment_id=?",
                              (status, cid))
            before = dict(store.comment(self.conn, cid))
            self.page([self.listed(cid, text="new text")])
            self.page([self.listed(cid, timestamp=None)])
            self.assertEqual(dict(store.comment(self.conn, cid)), before)

    def test_f05_every_unidentifiable_shape_is_counted_and_costs_only_itself(self):
        for bad in (None, 1, "bad", [], {}, {"id": None}, {"id": False}, {"id": ""}):
            with self.subTest(shape=bad):
                sweep = self.page([bad, self.listed()])
                self.assertEqual((sweep.unreadable, sweep.malformed, sweep.comments), (1, 1, 2))
                with store.connect(os.path.join(self.folder, "core.db"), readonly=True) as fresh:
                    self.assertEqual(store.comment(fresh, "c")["status"], "pending")

    def test_f04_rematch_refreshes_the_entire_seen_decision(self):
        self.page([self.listed(text="арбуз", **{"from": None})])
        self.cfg = dataclasses.replace(self.cfg, reels=(dataclasses.replace(self.cfg.reels[0], also_match=("арбуз",)),))
        self.bot = self.make_engine()
        self.bot.me = ME
        self.page([self.listed(text="арбуз", **{"from": None})])
        self.assertEqual(store.comment(self.conn, "c")["reason"], "no_author")
        self.page([self.listed(text="арбуз", **{"from": {"id": ME["user_id"], "username": ME["username"]}})])
        self.assertEqual(store.comment(self.conn, "c")["reason"], "own")

    def test_f05_an_unidentifiable_item_is_counted_for_the_owner_and_does_not_stop_the_reel(self):
        sweep = self.page([{"text": "арена"}, self.listed()])
        self.bot._reel_ok(sweep)
        self.assertEqual(store.comment(self.conn, "c")["status"], "pending")
        self.assertEqual(store.kv_get(self.conn, "reel_state:111"), "ok")
        self.assertIn("111", self.bot._swept)
        self.assertEqual(int(store.kv_get(self.conn, "unreadable:111")), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)
        self.bot._reel_ok(self.page([self.listed()]))   # a clean full sweep takes the count away
        self.assertIsNone(store.kv_get(self.conn, "unreadable:111"))
        # the second unreadable form is a text that is not a text (C47); a comment without a text (a GIF) is ordinary
        sweep = self.page([self.listed("number", text=5), self.listed("gif", text=None)])
        self.assertEqual(sweep.unreadable, 1)

    def test_f05_an_unidentifiable_item_on_a_later_page_stops_neither_this_reel_nor_another(self):
        second = dataclasses.replace(self.cfg.reels[0], media_id="222", name="second")
        self.cfg = dataclasses.replace(self.cfg, reels=(*self.cfg.reels, second))
        self.bot = self.make_engine()
        self.bot.me = ME
        self.bot._swept.add("111")
        store.kv_set(self.conn, "full_sweep_at:111", clock.iso())
        def ok(data):
            return meta.Result(True, 200, data, "ok", "", None, None, "", False)
        self.client.media = lambda media_id: ok({"comments_count": 2})
        def comments(media_id, next_url=None):
            if media_id == "222":
                return ok({"data": [self.listed("other")]})
            if next_url:
                return ok({"data": [{"text": "арена"}, self.listed("old")]})
            return ok({"data": [self.listed("new")], "paging": {"next": "synthetic-page-2"}})
        self.client.comments_page = comments
        self.bot._collect()
        self.assertEqual(store.kv_get(self.conn, "reel_state:111"), "ok")
        self.assertLessEqual({"111", "222"}, set(self.bot._sendable))
        self.assertEqual({store.comment(self.conn, cid)["status"] for cid in ("new", "old", "other")}, {"pending"})
        self.assertEqual(store.kv_get(self.conn, "reel_fails:111"), "0")
        self.assertIn("111", self.bot._sendable)

    def test_f15_malformed_expiry_never_discards_a_valid_rotated_token(self):
        for expiry in (float("inf"), float("nan"), 1e100, 864000000000, 10 ** 400, -1, 0, False, None):
            with self.subTest(expiry=str(expiry)[:30]):
                store.kv_set_many(self.conn, {"token": "old", "token_refreshed_at": clock.iso(self.now - timedelta(days=8)),
                                              "token_expires_at": None, "token_refresh_failed_at": None})
                fresh = meta.Result(True, 200, {"access_token": "new", "expires_in": expiry}, "ok", "", None,
                                    None, "", False)
                calls = []
                self.client.refresh_token = lambda token: calls.append(token) or fresh
                self.bot._refresh_token()
                self.bot._refresh_token()
                self.assertEqual(store.kv_get(self.conn, "token"), "new")
                self.assertIsNone(store.kv_get(self.conn, "token_expires_at"))
                self.assertEqual(calls, ["old"])

    def fill_outbox(self):
        pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
        self.conn.execute(f"PRAGMA max_page_count={pages}")
        try:
            while True:
                store.outbox_add(self.conn, "fill", "x")
        except sqlite3.Error as error:
            self.assertEqual(error.sqlite_errorcode, sqlite3.SQLITE_FULL)

    def test_f20_full_cannot_advance_lost_marker_past_queue(self):
        for status in ("expired", "uncertain"):
            with self.subTest(status=status):
                self.conn.execute("DELETE FROM comments")
                self.page([self.listed()])
                self.conn.execute("UPDATE comments SET status=?", (status,))
                self.fill_outbox()
                with self.assertRaises(Exception):
                    self.bot._lost_people()
                self.assertIsNone(store.kv_get(self.conn, "told_" + status))
                self.conn.execute("PRAGMA max_page_count=100000")
                self.bot._lost_people()
                self.bot._lost_people()
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE key=?", (status,)).fetchone()[0], 1)

    def test_f21_digest_compute_or_queue_failure_does_not_use_up_the_day(self):
        self.now = self.now.replace(hour=23)
        with mock.patch("report.build", side_effect=RuntimeError("report failed")):
            with self.assertRaises(RuntimeError):
                self.bot._daily()
        self.assertIsNone(store.kv_get(self.conn, "daily_at"))
        self.notifier.event = lambda *args: False
        with self.assertRaises(sqlite3.OperationalError):
            self.bot._daily()
        self.assertIsNone(store.kv_get(self.conn, "daily_at"))

    def test_f21_native_full_then_reopen_queues_one_digest(self):
        self.now = self.now.replace(hour=23)
        self.fill_outbox()
        with mock.patch("report.build", return_value={}), mock.patch("report.digest", return_value="digest" * 100):
            with self.assertRaises(sqlite3.OperationalError):
                self.bot._daily()
            self.assertIsNone(store.kv_get(self.conn, "daily_at"))
            self.conn.execute("PRAGMA max_page_count=100000")
            self.conn.close()
            self.conn = store.connect(os.path.join(self.folder, "core.db"))
            self.addCleanup(self.conn.close)
            self.notifier = notify.Notifier(self.conn, lambda: ("123:synthetic", "42"), "http://127.0.0.1:1")
            self.bot = self.make_engine()
            self.bot._daily()
            self.bot._daily()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE key='digest'").fetchone()[0], 1)

    def test_f21_marker_failure_rolls_back_queued_digest(self):
        self.now = self.now.replace(hour=23)
        def deny_marker(action, table, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "kv" else sqlite3.SQLITE_OK
        self.conn.set_authorizer(deny_marker)
        with mock.patch("report.build", return_value={}), mock.patch("report.digest", return_value="a digest"):
            with self.assertRaises(sqlite3.Error):
                self.bot._daily()
            self.conn.set_authorizer(None)
            self.assertIsNone(store.kv_get(self.conn, "daily_at"))
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE key='digest'").fetchone()[0], 0)
            self.bot._daily()
            self.bot._daily()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE key='digest'").fetchone()[0], 1)

    def test_f20_healthy_legacy_none_fake_is_not_mistaken_for_queue_failure(self):
        self.page([self.listed()])
        self.conn.execute("UPDATE comments SET status='expired'")
        events = []
        self.bot.notifier = types.SimpleNamespace(event=lambda *args: events.append(args))
        self.bot._lost_people()
        self.bot._lost_people()
        self.assertEqual(len(events), 1)
        self.assertEqual(store.kv_get(self.conn, "told_expired"), "1")

    def test_f30_constraint_rollback_keeps_outer_transaction_healthy(self):
        with store.tx(self.conn):
            store.kv_set(self.conn, "sentinel", "outer")
            with self.assertRaises(sqlite3.IntegrityError):
                with store.tx(self.conn):
                    store.kv_set(self.conn, "sentinel", "inner")
                    self.conn.execute("INSERT INTO kv (key,value) VALUES ('sentinel','duplicate')")
            self.assertEqual(store.kv_get(self.conn, "sentinel"), "outer")
        self.assertEqual(store.kv_get(self.conn, "sentinel"), "outer")

    def test_f23_recovery_clear_is_durable_before_mute_is_removed(self):
        self.bot._mute = ("db_unwritable", self.mono)
        with mock.patch.object(self.notifier, "flush") as flush:
            self.bot._speak_up(None)
        row = self.conn.execute("SELECT text FROM outbox WHERE key='db_unwritable' ORDER BY id DESC").fetchone()
        self.assertEqual(row[0], "✓ cleared: db_unwritable")
        self.assertIsNone(self.bot._mute)
        flush.assert_called_once()

    def test_f23_failed_clear_queue_preserves_mute(self):
        self.bot._mute = ("db_unwritable", self.mono)
        self.notifier.queue_clear = lambda key: False
        self.bot._speak_up(None)
        self.assertEqual(self.bot._mute[0], "db_unwritable")

    def test_f30_nested_native_full_preserves_original_sqlite_code(self):
        store.kv_set(self.conn, "sentinel", "old")
        self.fill_outbox()
        with self.assertRaises(sqlite3.Error) as caught:
            with store.tx(self.conn):
                store.kv_set(self.conn, "sentinel", "new")
                with store.tx(self.conn):
                    store.outbox_add(self.conn, "full", "z" * 16000)
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(store.kv_get(self.conn, "sentinel"), "old")
        self.assertFalse(self.conn.in_transaction)

    def test_arm_marker_partitions_attempt_ids_despite_wall_rollback(self):
        old = self.history(self.now, self.now, "rejected", 400, "invalid")
        self.advance(-120)
        store.arm(self.conn, 1)
        self.assertEqual(int(store.kv_get(self.conn, "armed_through_attempt_id")), old)
        new = self.history(self.now, self.now)
        self.bot._stage_done(("1", old), new)
        text = self.conn.execute("SELECT text FROM outbox WHERE key='stage_done'").fetchone()[0]
        self.assertIn("ok 1", text)
        self.assertNotIn("rejected", text)
        store.arm(self.conn, 0)
        self.assertEqual(int(store.kv_get(self.conn, "armed_through_attempt_id")), new)

    def test_prepare_run_is_idempotent_and_cycle_boundary_precedes_work(self):
        self.bot.prepare_run()
        self.bot.prepare_run()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE key='started'").fetchone()[0], 1)
        events = []
        with mock.patch.object(engine, "log", lambda level, event, **fields: events.append((event, fields))), \
                mock.patch.object(self.bot, "_work", side_effect=lambda only: events.append(("work", {}))), \
                mock.patch.object(self.bot, "_announce"):
            self.bot.cycle()
        self.assertEqual([row[0] for row in events][:2], ["cycle_start", "work"])
        fields = events[0][1]
        self.assertEqual(fields["pid"], os.getpid())
        self.assertEqual(fields["cycle"], 1)
        self.assertGreater(fields["monotonic"], 0)

    def test_prepare_run_explicit_queue_refusal_keeps_start_unprepared(self):
        self.notifier.event = lambda *args: False
        with self.assertRaises(sqlite3.OperationalError):
            self.bot.prepare_run()
        self.assertFalse(self.bot._prepared)


if __name__ == "__main__":
    unittest.main()
