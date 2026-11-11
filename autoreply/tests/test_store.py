"""SPEC section 7 (the database), the stored state of 8.5 and 8.6.

Every test works on a real SQLite file in a temporary directory, with the bot's clock in the test's hands.
"""
import dataclasses
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import clock
import config
import meta
import store

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN, HOUR, DAY = 60, 3600, 86400
START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = 7 * DAY - 10 * MIN     # 7.2 step 1
SLOT = 60                       # seconds between two DMs at 60 per hour
LONG = 25 * HOUR                # longer than any send hold and than the cap window

STATUSES = ("seen", "pending", "sending", "sent", "done", "uncertain", "refused", "expired", "dup_user", "already")
ALLOWED = {   # 7.1, written out pair by pair
    ("seen", "pending"), ("seen", "expired"), ("seen", "already"),
    ("pending", "sending"), ("pending", "seen"), ("pending", "expired"), ("pending", "dup_user"),
    ("pending", "already"),
    ("sending", "sent"), ("sending", "uncertain"), ("sending", "refused"), ("sending", "pending"),
    ("sending", "already"),
    ("sent", "done"),
}

REEL = config.Reel(name="arena", media_id="111", permalink="", active=True, keywords=("арена", "arena"),
                   also_match=("арееа",), max_words=5, link="https://example.com/skill", text="{greeting} here it is")
CFG = config.Config(api_version="v26.0", sending=True, poll_interval_sec=90, max_sends_per_hour=60,
                    public_reply=True, profile_url="https://www.instagram.com/someone/", greeting="Hi!",
                    late_greeting="Hi, late!", late_after_minutes=60,
                    public_phrases=("Sent 📩", "In your inbox", "Check your messages"), reels=(REEL,),
                    alerts=config.Alerts(repeat_min=120, digest_hour_utc=6))
ME = {"user_id": "9000", "id": "9001", "username": "owner"}


def with_reel(**changes):
    return dataclasses.replace(CFG, reels=(dataclasses.replace(REEL, **changes),))


def answer(outcome, reason="", http=400, code=None, subcode=None, message="", data=None):
    """What meta.Client hands back for a POST."""
    return meta.Result(ok=outcome == "ok", http=http, data=data or {}, outcome=outcome, reason=reason, code=code,
                       subcode=subcode, message=message, connect_failed=outcome == "not_sent")


DM_OK = answer("ok", http=200, data={"recipient_id": "r1", "message_id": "mid.1"})
PUBLIC_OK = answer("ok", http=200, data={"id": "reply.1"})


class Db(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.path = os.path.join(self.dir, "core.db")
        self.now = START
        patcher = mock.patch.object(clock, "now", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = self.open()
        self.assertTrue(store.init(self.conn))
        self.people = 0

    def open(self, path=None, readonly=False):
        conn = store.connect(path or self.path, readonly)
        self.addCleanup(conn.close)
        return conn

    def tick(self, seconds):
        self.now += timedelta(seconds=seconds)

    def ago(self, seconds):
        return clock.iso(self.now - timedelta(seconds=seconds))

    def add(self, comment_id, user="u1", text="арена", age=60, status="pending", media="111", username="someone"):
        self.assertTrue(store.add_comment(self.conn, comment_id, media, user, username, text, self.ago(age), "pending"))
        if status != "pending":
            self.force(comment_id, status)

    def force(self, comment_id, status, reason=""):
        """Set-up only: put a row into a status without walking the path that leads there."""
        self.conn.execute("UPDATE comments SET status=?, reason=? WHERE comment_id=?", (status, reason, comment_id))

    def journal(self, *entries):
        """Set-up only: attempts of other comments, oldest first, each (kind, outcome); outcome None = in flight."""
        for kind, outcome in entries:
            self.past_attempt(0, outcome, kind)

    def past_attempt(self, started_ago, outcome=None, kind="dm", comment_id="other"):
        """Set-up only: one attempt (of another comment), started that many seconds ago; no outcome = in flight."""
        started = self.ago(started_ago)
        self.conn.execute("INSERT INTO attempts (comment_id, kind, started_at, finished_at, outcome) VALUES (?,?,?,?,?)",
                          (comment_id, kind, started, started if outcome else None, outcome))

    def state(self, comment_id):
        row = store.comment(self.conn, comment_id)
        return row["status"], row["reason"]

    def attempts(self, kind=None):
        rows = self.conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
        return [dict(row) for row in rows if kind in (None, row["kind"])]

    def everything(self):
        return list(self.conn.iterdump())

    def claim(self, comment_id, cfg=CFG, me=ME):
        return store.claim_dm(self.conn, comment_id, cfg, me)

    def dm(self, result, comment_id=None):
        """A new person's comment (or the given pending one) claimed after a long pause and answered with `result`."""
        self.tick(LONG)
        if comment_id is None:
            self.people += 1
            comment_id = f"dm{self.people}"
            self.add(comment_id, user=f"person{self.people}")
        claim = self.claim(comment_id)
        self.assertEqual(claim.kind, "claimed")
        return comment_id, store.record_dm(self.conn, claim.attempt_id, result)

    def sent(self, comment_id, user="u1"):
        """A comment whose DM Meta confirmed: `sent`, with the public reply still owed."""
        store.arm(self.conn, "unlimited")
        self.add(comment_id, user=user)
        self.assertEqual(self.dm(DM_OK, comment_id), (comment_id, "sent"))

    def claim_public(self, comment_id, cfg=CFG, me=ME):
        return store.claim_public(self.conn, comment_id, cfg, me)


# ---------- connection, schema, transactions ----------

class Opening(Db):
    def test_connect_sets_the_pragmas_and_leaves_the_schema_alone(self):
        path = os.path.join(self.dir, "new.db")
        conn = self.open(path)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)          # FULL
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        self.assertIs(conn.row_factory, sqlite3.Row)

    def new_file_with_a_writer(self, seconds):
        """A database file that is not in WAL mode yet, and another connection in a write transaction on it."""
        path = os.path.join(self.dir, "first.db")
        writing = threading.Event()

        def write():
            other = sqlite3.connect(path, isolation_level=None)
            other.execute("CREATE TABLE t (x)")
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO t VALUES (1)")
            writing.set()
            time.sleep(seconds)
            other.execute("COMMIT")
            other.close()

        writer = threading.Thread(target=write)
        writer.start()
        self.addCleanup(writer.join)
        self.assertTrue(writing.wait(10))
        return path

    def test_connect_waits_for_a_writer_before_it_switches_a_new_file_to_wal(self):
        # the very first start: a command given in that instant, or the start itself, ended "database is locked"
        conn = self.open(self.new_file_with_a_writer(0.3))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_connect_gives_up_on_a_file_that_stays_busy_and_leaves_no_connection_behind(self):
        path = self.new_file_with_a_writer(1.0)
        opened = []

        def connect(*args, real=sqlite3.connect, **options):
            opened.append(real(*args, **options))
            return opened[-1]

        began = time.monotonic()
        with mock.patch.dict(store.T, {"db_busy_timeout": 0.2}), mock.patch.object(sqlite3, "connect", connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                store.connect(path)
        self.assertGreaterEqual(time.monotonic() - began, 0.2)       # it waited as long as for any other lock
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            opened[0].execute("SELECT 1")

    def test_failed_open_leaves_no_connection_behind(self):
        path = os.path.join(self.dir, "garbage.db")
        with open(path, "w") as file:
            file.write("this is not a database. " * 100)
        opened = []

        def connect(*args, real=sqlite3.connect, **options):
            opened.append(real(*args, **options))
            return opened[-1]

        with mock.patch.object(sqlite3, "connect", connect):
            with self.assertRaises(sqlite3.DatabaseError):
                store.connect(path)
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            opened[0].execute("SELECT 1")

    def test_init_creates_exactly_the_schema_of_the_spec(self):
        with open(os.path.join(CODE, "SPEC.md"), encoding="utf-8") as spec:
            text = spec.read()
            sql = re.search(r"## 7\. Database.*?```sql\n(.*?)```", text, re.S).group(1) + \
                re.search(r"### 14\.6 Tables.*?```sql\n(.*?)```", text, re.S).group(1)
        wanted = sqlite3.connect(os.path.join(self.dir, "spec.db"))
        self.addCleanup(wanted.close)
        wanted.executescript(re.sub(r"--[^\n]*", "", sql))

        def shape(conn):
            return {name: " ".join(text.split()) for name, text in
                    conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}

        self.assertEqual(shape(self.conn), shape(wanted))
        self.assertEqual(len(shape(self.conn)), 20)   # 6 + 5 tables, 4 + 5 indexes
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_init_upgrades_a_version_2_database_by_adding_the_funnel_tables_only(self):
        path = os.path.join(self.dir, "v2.db")
        conn = self.open(path)
        for statement in store._SCHEMA.split(";"):
            conn.execute(statement)
        conn.execute("PRAGMA user_version=2")
        conn.execute("INSERT INTO kv (key, value) VALUES ('send_budget', '7')")
        self.assertFalse(store.init(conn))   # not a fresh database: nothing of version 2 is touched
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(store.kv_get(conn, "send_budget"), "7")
        self.assertIsNone(store.kv_get(conn, "fresh_db"))
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"flow", "checks", "inbound", "links", "clicks"} <= names)

    def test_init_runs_once_and_marks_the_new_database_fresh(self):
        self.assertEqual(store.kv_get(self.conn, "fresh_db"), "1")
        store.kv_set(self.conn, "fresh_db", None)
        self.add("c1")
        before = self.everything()
        self.assertFalse(store.init(self.conn))
        self.assertFalse(store.init(self.open()))
        self.assertEqual(self.everything(), before)            # no table recreated, no row touched, not fresh again

    def test_fresh_database_has_no_budget(self):
        self.assertEqual(store.budget(self.conn), 0)           # G6
        self.add("c1")
        self.assertEqual(self.claim("c1").kind, "no_budget")

    def test_init_refuses_a_database_of_another_version(self):
        path = os.path.join(self.dir, "other.db")
        conn = self.open(path)
        conn.execute("CREATE TABLE things (id)")
        conn.execute("PRAGMA user_version=4")
        with self.assertRaises(RuntimeError):
            store.init(conn)
        self.assertFalse(conn.in_transaction)
        self.assertEqual([row[0] for row in conn.execute("SELECT name FROM sqlite_master")], ["things"])

    def test_init_does_not_build_on_top_of_a_v1_database(self):
        path = os.path.join(self.dir, "v1.db")
        conn = self.open(path)
        conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")   # v1 never set user_version
        with self.assertRaises(sqlite3.OperationalError):
            store.init(conn)
        self.assertFalse(conn.in_transaction)
        self.assertEqual([row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")], ["kv"])
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_readonly_connection_reads_and_cannot_write(self):
        self.add("c1")
        store.arm(self.conn, 5)
        reader = self.open(readonly=True)
        self.assertEqual(store.comment(reader, "c1")["status"], "pending")
        self.assertEqual(store.budget(reader), 5)
        before = self.everything()
        with self.assertRaises(sqlite3.OperationalError):
            store.kv_set(reader, "send_budget", "9")
        with self.assertRaises(sqlite3.OperationalError):
            store.claim_dm(reader, "c1", CFG, ME)
        self.assertFalse(reader.in_transaction)
        self.assertEqual(self.everything(), before)

    def test_readonly_connection_does_not_create_a_missing_database(self):
        for name in ("missing.db", "odd name #1?.db"):
            path = os.path.join(self.dir, name)
            with self.assertRaises(sqlite3.OperationalError):
                store.connect(path, readonly=True)
            self.assertFalse(os.path.exists(path))

    def test_readonly_connection_opens_any_file_name(self):
        path = os.path.join(self.dir, "odd name #1?.db")
        store.init(self.open(path))
        self.assertEqual(store.kv_get(self.open(path, readonly=True), "fresh_db"), "1")

    def test_only_the_ten_statuses_can_be_stored(self):
        self.add("c1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.force("c1", "dm_sent")
        self.assertEqual(self.state("c1"), ("pending", ""))


class Transactions(Db):
    def test_commit(self):
        with store.tx(self.conn):
            store.kv_set(self.conn, "a", "1")
            self.assertTrue(self.conn.in_transaction)
            self.assertIsNone(store.kv_get(self.open(), "a"))        # not visible to others before the commit
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(store.kv_get(self.open(), "a"), "1")

    def test_rollback_on_any_exception(self):
        for error in (ValueError, KeyboardInterrupt):
            with self.subTest(error=error.__name__):
                with self.assertRaises(error):
                    with store.tx(self.conn):
                        store.kv_set(self.conn, "a", "1")
                        raise error()
                self.assertFalse(self.conn.in_transaction)
                self.assertIsNone(store.kv_get(self.conn, "a"))

    def test_takes_the_write_lock_at_once(self):
        other = self.open()
        other.execute("PRAGMA busy_timeout=50")
        with store.tx(self.conn):                                    # nothing written yet
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                store.kv_set(other, "a", "1")
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                with store.tx(other):
                    self.fail("two writers at once")
        store.kv_set(other, "a", "1")
        self.assertEqual(store.kv_get(self.conn, "a"), "1")

    def test_nested_block_is_all_or_nothing_inside_the_outer_one(self):
        with store.tx(self.conn):
            store.kv_set(self.conn, "outer", "1")
            with self.assertRaises(ValueError):
                with store.tx(self.conn):
                    store.kv_set(self.conn, "inner", "1")
                    raise ValueError()
            with store.tx(self.conn):
                store.kv_set(self.conn, "second", "1")
            self.assertTrue(self.conn.in_transaction)
            self.assertIsNone(store.kv_get(self.open(), "outer"))    # the inner blocks did not commit anything
        other = self.open()
        self.assertEqual([store.kv_get(other, key) for key in ("outer", "inner", "second")], ["1", None, "1"])

    def test_outer_failure_undoes_a_finished_nested_block(self):
        with self.assertRaises(ValueError):
            with store.tx(self.conn):
                with store.tx(self.conn):
                    store.kv_set(self.conn, "inner", "1")
                raise ValueError()
        self.assertIsNone(store.kv_get(self.conn, "inner"))
        self.assertFalse(self.conn.in_transaction)


# ---------- statuses ----------

class Transitions(Db):
    def test_every_pair_of_the_table(self):
        for was in STATUSES:
            for to in STATUSES:
                comment_id = f"{was}-{to}"
                self.add(comment_id, user=comment_id, status=was)
                self.tick(1)
                with self.subTest(was=was, to=to):
                    if (was, to) in ALLOWED:
                        self.assertTrue(store.transition(self.conn, comment_id, was, to, "why"))
                        self.assertEqual(self.state(comment_id), (to, "why"))
                        self.assertEqual(store.comment(self.conn, comment_id)["updated_at"], clock.iso(self.now))
                    else:
                        with self.assertRaises(ValueError):
                            store.transition(self.conn, comment_id, was, to, "why")
                        self.assertEqual(self.state(comment_id), (was, ""))
        self.assertEqual(len(ALLOWED), 14)

    def test_wrong_current_status_changes_nothing(self):
        for was, to in sorted(ALLOWED):
            for current in STATUSES:
                if current == was:
                    continue
                comment_id = f"{was}-{to}-{current}"
                self.add(comment_id, user=comment_id, status=current)
                before = dict(store.comment(self.conn, comment_id))
                self.tick(1)
                with self.subTest(was=was, to=to, current=current):
                    self.assertFalse(store.transition(self.conn, comment_id, was, to, "why"))
                    self.assertEqual(dict(store.comment(self.conn, comment_id)), before)

    def test_unknown_comment(self):
        self.assertFalse(store.transition(self.conn, "nobody", "pending", "sending"))

    def test_bad_arguments(self):
        self.add("c1")
        for was, to in ("", "sending"), ("pending", "sent "), ("nonsense", "pending"), \
                ("pending", "pending"), ("pending", ""), ("Pending", "sending"):
            with self.subTest(was=was, to=to):
                with self.assertRaises(ValueError):
                    store.transition(self.conn, "c1", was, to)
        self.assertEqual(self.state("c1"), ("pending", ""))

    def test_reason_is_replaced_and_empty_by_default(self):
        self.add("c1", status="seen")
        self.force("c1", "seen", "near:question")
        self.assertTrue(store.transition(self.conn, "c1", "seen", "pending"))
        self.assertEqual(self.state("c1"), ("pending", ""))


class Comments(Db):
    def test_add_stores_the_comment_once(self):
        self.tick(5)
        added = store.add_comment(self.conn, "c1", "111", "u1", "kate", "Арена!", "2026-10-05T11:59:00+0000", "pending")
        self.assertTrue(added)
        first = dict(store.comment(self.conn, "c1"))
        self.assertEqual(first, {
            "comment_id": "c1", "media_id": "111", "user_id": "u1", "username": "kate", "text": "Арена!",
            "ts": "2026-10-05T11:59:00.000Z", "status": "pending", "reason": "", "deferrals": 0,
            "first_seen_at": "2026-10-05T12:00:05.000Z", "updated_at": "2026-10-05T12:00:05.000Z"})
        self.tick(5)
        again = store.add_comment(self.conn, "c1", "222", "u2", "other", "other", "2026-10-05T11:00:00+0000", "seen", "own")
        self.assertFalse(again)
        self.assertEqual(dict(store.comment(self.conn, "c1")), first)

    def test_add_with_each_first_status(self):
        for status, reason in ("seen", ""), ("seen", "near:question"), ("pending", ""), ("expired", "window"), \
                ("already", "owner_replied"):
            comment_id = f"{status}/{reason}"
            self.assertTrue(store.add_comment(self.conn, comment_id, "111", "u1", "kate", "x", self.ago(0), status, reason))
            self.assertEqual(self.state(comment_id), (status, reason))

    def test_add_cannot_skip_the_claim(self):
        for status in ("sending", "sent", "done", "uncertain", "refused", "dup_user", "nonsense", ""):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    store.add_comment(self.conn, "c1", "111", "u1", "kate", "арена", self.ago(0), status)
        self.assertIsNone(store.comment(self.conn, "c1"))

    def test_add_needs_ids(self):
        for comment_id, media_id in (None, "111"), ("", "111"), ("c1", None), ("c1", ""), (17, "111"):
            with self.subTest(comment_id=comment_id, media_id=media_id):
                with self.assertRaises(ValueError):
                    store.add_comment(self.conn, comment_id, media_id, "u1", "kate", "арена", self.ago(0), "pending")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0)

    def test_add_without_timestamp_uses_the_moment_it_was_seen(self):
        self.assertTrue(store.add_comment(self.conn, "c1", "111", None, None, None, None, "seen", "malformed"))
        row = store.comment(self.conn, "c1")
        self.assertEqual((row["ts"], row["first_seen_at"]), (clock.iso(START), clock.iso(START)))
        self.assertEqual((row["user_id"], row["username"], row["text"]), (None, None, None))

    def test_add_refuses_a_timestamp_it_cannot_read(self):
        for ts in ("yesterday", "2026-10-05T11:59:00", "", 1790000000):
            with self.subTest(ts=ts):
                with self.assertRaises(ValueError):
                    store.add_comment(self.conn, "c1", "111", "u1", "kate", "арена", ts, "pending")
        self.assertIsNone(store.comment(self.conn, "c1"))

    def test_add_stores_whatever_meta_listed(self):
        # G5: a lone surrogate or a number where a text should be must not keep the comment out of the database;
        # it is stored as U+FFFD, a character the matcher gives no meaning to (a `?` made a request a question)
        self.assertTrue(store.add_comment(self.conn, "c1", "111", 42, "ka\udc00te", "арена \ud83d", self.ago(0), "seen"))
        row = store.comment(self.conn, "c1")
        self.assertEqual((row["user_id"], row["username"], row["text"]), ("42", "ka\ufffdte", "арена \ufffd"))
        self.assertTrue(store.add_comment(self.conn, "c2", "111", "u1", "kate", {"a": 1}, self.ago(0), "seen"))
        self.assertEqual(store.comment(self.conn, "c2")["text"], "{'a': 1}")

    def test_window_closed(self):
        for age, closed in (0, False), (WINDOW - 0.001, False), (WINDOW, True), (WINDOW + 1, True), (8 * DAY, True):
            with self.subTest(age=age):
                self.assertIs(store.window_closed(self.ago(age), self.now), closed)
        self.assertTrue(store.window_closed("2026-09-28T12:10:00+0000", START))     # Meta's own format
        self.assertFalse(store.window_closed("2026-09-28T12:10:01+0000", START))

    def test_expire_moves_pending_rows_past_the_window(self):
        self.add("young", age=WINDOW - 0.001)
        self.add("edge", age=WINDOW)
        self.add("old", age=9 * DAY)
        for status in STATUSES:
            if status != "pending":
                self.add(status, age=9 * DAY, status=status)
        before = {status: dict(store.comment(self.conn, status)) for status in STATUSES if status != "pending"}
        self.tick(0)
        self.assertEqual(store.expire(self.conn), 2)
        self.assertEqual(self.state("young"), ("pending", ""))
        self.assertEqual(self.state("edge"), ("expired", "window"))
        self.assertEqual(self.state("old"), ("expired", "window"))
        self.assertEqual({status: dict(store.comment(self.conn, status)) for status in before}, before)
        self.assertEqual(store.expire(self.conn), 0)
        self.tick(0.001)
        self.assertEqual(store.expire(self.conn), 1)
        self.assertEqual(self.state("young"), ("expired", "window"))
        self.assertEqual(store.comment(self.conn, "young")["updated_at"], clock.iso(self.now))

    def test_is_own(self):
        for user_id, username, own in ("9000", "x", True), ("9001", "x", True), ("5", "owner", True), \
                ("5", "x", False), (None, None, False), ("", "", False), (None, "owner", True), ("9000", None, True), \
                ("90000", "owner2", False):
            with self.subTest(user_id=user_id, username=username):
                self.assertIs(store.is_own(ME, user_id, username), own)
        self.assertFalse(store.is_own({"user_id": None, "id": None, "username": None}, None, None))
        self.assertFalse(store.is_own({"user_id": "", "id": "", "username": ""}, "", ""))
        self.assertFalse(store.is_own({}, None, None))


# ---------- kv, budget, holds ----------

class Kv(Db):
    def test_get_set_remove(self):
        self.assertIsNone(store.kv_get(self.conn, "me_id"))
        self.assertEqual(store.kv_get(self.conn, "me_id", "none"), "none")
        store.kv_set(self.conn, "me_id", 17)
        self.assertEqual(store.kv_get(self.conn, "me_id", "none"), "17")
        store.kv_set(self.conn, "me_id", "18")
        self.assertEqual(store.kv_get(self.open(), "me_id"), "18")
        store.kv_set(self.conn, "me_id", None)
        self.assertIsNone(store.kv_get(self.conn, "me_id"))
        store.kv_set(self.conn, "me_id", None)                      # removing what is not there is fine
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM kv WHERE key='me_id'").fetchone()[0], 0)

    def test_set_many_is_one_transaction(self):
        store.kv_set(self.conn, "token", "old")

        class Unprintable:
            def __str__(self):
                raise RuntimeError("no")

        with self.assertRaises(RuntimeError):
            store.kv_set_many(self.conn, {"token": "new", "token_expires_at": None, "token_env_seen": Unprintable()})
        self.assertEqual(store.kv_get(self.conn, "token"), "old")
        store.kv_set(self.conn, "token_expires_at", "soon")
        store.kv_set_many(self.conn, {"token": "new", "token_expires_at": None, "token_refreshed_at": "now"})
        self.assertEqual([store.kv_get(self.conn, key) for key in ("token", "token_expires_at", "token_refreshed_at")],
                         ["new", None, "now"])


class Budget(Db):
    def test_reading(self):
        self.assertEqual(store.budget(self.conn), 0)                # absent
        for stored, budget in ("0", 0), ("7", 7), ("unlimited", "unlimited"), ("-2", 0), ("many", 0), ("", 0), \
                ("1.5", 0), ("Unlimited", 0):
            store.kv_set(self.conn, "send_budget", stored)
            with self.subTest(stored=stored):
                self.assertEqual(store.budget(self.conn), budget)

    def test_arm_sets_and_does_not_add(self):
        store.arm(self.conn, 5)
        self.tick(30)
        store.arm(self.conn, 5)
        self.assertEqual(store.budget(self.conn), 5)
        self.assertEqual(store.kv_get(self.conn, "armed_n"), "5")
        self.assertEqual(store.kv_get(self.conn, "armed_at"), clock.iso(self.now))
        self.tick(30)
        store.arm(self.conn, "unlimited")
        self.assertEqual((store.budget(self.conn), store.kv_get(self.conn, "armed_n")), ("unlimited", "unlimited"))
        store.arm(self.conn, 0)                                     # disarm
        self.assertEqual((store.budget(self.conn), store.kv_get(self.conn, "armed_n")), (0, "0"))
        self.assertEqual(store.kv_get(self.conn, "armed_at"), clock.iso(self.now))

    def test_arm_refuses_anything_else(self):
        store.arm(self.conn, 5)
        before = self.everything()
        for n in (-1, 1.5, True, None, "all", "7", "", [3]):
            with self.subTest(n=n):
                with self.assertRaises(ValueError):
                    store.arm(self.conn, n)
        self.assertEqual(self.everything(), before)

    def test_another_process_disarms_and_the_next_claim_sees_it(self):
        store.arm(self.conn, "unlimited")
        self.add("c1")
        operator = self.open()
        store.arm(operator, 0)
        operator.close()
        self.assertEqual(self.claim("c1").kind, "no_budget")


class AccountHold(Db):
    def test_probe_schedule(self):
        for reason, waits in ("auth", [5, 10, 20, 30, 30, 30]), ("blocked", [5, 10, 20, 30, 30, 30]), \
                ("no_token", [5, 10, 20, 30, 30, 30]), ("rate", [15, 15, 20, 30, 30, 30]):
            with self.subTest(reason=reason):
                self.assertEqual([store.probe_wait(reason, n) for n in range(6)], [wait * MIN for wait in waits])

    def test_set_then_each_failed_probe_then_clear(self):
        self.assertIsNone(store.get_account_hold(self.conn))
        since = clock.iso(self.now)
        hold = store.set_account_hold(self.conn, "auth", "code 190")
        self.assertEqual(hold, {"reason": "auth", "since": since, "n": 0, "next_probe_at": self.ago(-5 * MIN),
                                "detail": "code 190"})
        self.assertEqual(store.get_account_hold(self.open()), hold)
        self.assertEqual(json.loads(store.kv_get(self.conn, "account_hold")), hold)
        for n, wait in (1, 10), (2, 20), (3, 30), (4, 30):
            self.tick(wait * MIN + 7)
            hold = store.set_account_hold(self.conn, "blocked", "unreachable")
            with self.subTest(n=n):
                self.assertEqual(hold, {"reason": "blocked", "since": since, "n": n,
                                        "next_probe_at": self.ago(-wait * MIN), "detail": "unreachable"})
                self.assertEqual(store.get_account_hold(self.conn), hold)
        store.clear_account_hold(self.conn)
        self.assertIsNone(store.get_account_hold(self.conn))
        self.assertIsNone(store.kv_get(self.conn, "account_hold"))
        self.assertEqual(store.set_account_hold(self.conn, "rate")["next_probe_at"], self.ago(-15 * MIN))
        self.assertEqual(store.get_account_hold(self.conn)["since"], clock.iso(self.now))   # a new hold, a new start


# ---------- the DM claim (7.2) ----------

class ClaimDm(Db):
    def setUp(self):
        super().setUp()
        store.arm(self.conn, 3)

    def assert_untouched(self, comment_id, budget=3, attempts=0):
        """Nothing was claimed: the row still waits, the journal did not grow, the budget is whole."""
        self.assertEqual(self.state(comment_id), ("pending", ""))
        self.assertEqual(len(self.attempts()), attempts)
        self.assertEqual(store.budget(self.conn), budget)
        self.assertFalse(self.conn.in_transaction)

    def test_claim_is_committed_before_it_returns(self):
        self.add("c1")
        self.tick(5)
        before = time.monotonic()
        claim = self.claim("c1")
        self.assertLessEqual(before, claim.monotonic)
        self.assertLessEqual(claim.monotonic, time.monotonic())
        self.assertEqual(claim, store.Claim("claimed", 1, False, 0, monotonic=claim.monotonic, started_at=claim.started_at))
        self.assertEqual((claim.kind, claim.attempt_id, claim.late, claim.seconds), ("claimed", 1, False, 0))
        self.assertFalse(self.conn.in_transaction)
        other = self.open()
        self.assertEqual(store.comment(other, "c1")["status"], "sending")
        self.assertEqual(store.comment(other, "c1")["updated_at"], clock.iso(self.now))
        self.assertEqual([dict(row) for row in other.execute("SELECT * FROM attempts")], [{
            "id": 1, "comment_id": "c1", "kind": "dm", "detail": "fresh", "started_at": clock.iso(self.now),
            "finished_at": None, "http": None, "code": None, "subcode": None, "message": None, "outcome": None,
            "reason": None, "meta_id": None}])
        self.assertEqual(store.budget(other), 2)

    def test_late_greeting(self):
        store.arm(self.conn, "unlimited")
        cfg = dataclasses.replace(CFG, late_after_minutes=30)
        for n, (age, late) in enumerate([(30 * MIN, False), (30 * MIN + 0.001, True), (0, False), (6 * DAY, True)]):
            self.tick(SLOT)
            self.add(f"c{n}", user=f"u{n}", age=age)
            with self.subTest(age=age):
                before = time.monotonic()
                claim = self.claim(f"c{n}", cfg)
                self.assertLessEqual(before, claim.monotonic)
                self.assertLessEqual(claim.monotonic, time.monotonic())
                self.assertEqual(claim, store.Claim("claimed", n + 1, late, 0, monotonic=claim.monotonic,
                                                   started_at=claim.started_at))
                self.assertEqual(self.attempts()[-1]["detail"], "late" if late else "fresh")

    def test_only_a_pending_comment_is_claimed(self):
        for status in STATUSES:
            if status != "pending":
                self.add(status, user=status, status=status)
                with self.subTest(status=status):
                    self.assertEqual(self.claim(status), store.Claim("skip"))
                    self.assertEqual(self.state(status), (status, ""))
        self.assertEqual(self.claim("nobody"), store.Claim("skip", None, None, 0))
        self.assertEqual(self.attempts(), [])
        self.assertEqual(store.budget(self.conn), 3)

    def test_comment_is_decided_again_under_the_config_of_this_run(self):
        for name, row, cfg, reason in [
            ("not a keyword any more", {}, with_reel(keywords=("другое",), also_match=()), ""),
            ("question word", {"text": "зачем арена"}, CFG, "near:question"),
            ("question mark", {"text": "арена тут?"}, CFG, "near:question"),
            ("too long now", {"text": "арена и еще два слова"}, with_reel(max_words=4), "near:too_long"),
            ("no author", {"user": None}, CFG, "no_author"),
            ("own by user_id", {"user": "9000"}, CFG, "own"),
            ("own by id", {"user": "9001"}, CFG, "own"),
            ("own by username", {"username": "owner"}, CFG, "own"),
            ("reel switched off", {}, with_reel(active=False), "no_reel"),
            ("reel not in the config", {"media": "222"}, CFG, "no_reel"),
        ]:
            self.add(name, **{"user": name, **row})
            with self.subTest(name):
                self.assertEqual(self.claim(name, cfg), store.Claim("skip"))
                self.assertEqual(self.state(name), ("seen", reason))
        self.assertEqual(self.attempts(), [])
        self.assertEqual(store.budget(self.conn), 3)

    def test_matcher_is_built_from_the_config_given(self):
        self.add("c1", text="skill")
        self.add("c2", text="skill", user="u2")
        self.add("c3", text="арена и еще два слова", user="u3")
        self.assertEqual(self.claim("c1").kind, "skip")
        self.assertEqual(self.state("c1"), ("seen", ""))
        self.assertEqual(self.claim("c2", with_reel(keywords=("skill",))).kind, "claimed")
        self.tick(SLOT)
        self.assertEqual(self.claim("c3", with_reel(max_words=5)).kind, "claimed")

    def test_window_margin(self):
        self.add("inside", user="a", age=WINDOW - 0.001)
        self.add("edge", user="b", age=WINDOW)
        self.assertEqual(self.claim("edge"), store.Claim("skip"))
        self.assertEqual(self.state("edge"), ("expired", "window"))
        self.assertEqual(self.attempts(), [])
        self.assertEqual(store.budget(self.conn), 3)
        self.assertEqual(self.claim("inside").kind, "claimed")

    def test_one_dm_per_person_and_media(self):
        for status in STATUSES:
            store.arm(self.conn, 3)
            self.tick(SLOT)
            self.add(f"old-{status}", user=status, status=status, age=2 * HOUR)
            self.add(f"new-{status}", user=status)
            claim = self.claim(f"new-{status}")
            with self.subTest(other=status):
                if status in ("sending", "sent", "done", "uncertain", "already"):
                    self.assertEqual(claim, store.Claim("skip"))
                    self.assertEqual(self.state(f"new-{status}"), ("dup_user", f"dup:old-{status}"))
                    self.assertEqual(store.budget(self.conn), 3)
                else:   # refused, expired, seen (and pending, dup_user) did not use up the person's one DM
                    self.assertEqual(claim.kind, "claimed")
                    self.assertEqual(self.state(f"new-{status}"), ("sending", ""))
                self.assertEqual(self.state(f"old-{status}"), (status, ""))
        self.assertEqual(len(self.attempts()), 5)

    def test_same_person_on_another_media_and_another_person_on_this_one(self):
        cfg = dataclasses.replace(CFG, reels=(REEL, dataclasses.replace(REEL, name="second", media_id="222")))
        self.add("elsewhere", user="u1", media="222", status="done")
        self.add("somebody else", user="u2", status="done")
        self.add("c1", user="u1")
        self.assertEqual(self.claim("c1", cfg).kind, "claimed")

    def test_duplicate_names_the_oldest_handled_comment(self):
        self.add("later", age=HOUR, status="done")
        self.add("first", age=3 * HOUR, status="uncertain")
        self.add("unhandled", age=5 * HOUR, status="refused")
        self.add("c1")
        self.assertEqual(self.claim("c1"), store.Claim("skip"))
        self.assertEqual(self.state("c1"), ("dup_user", "dup:first"))

    def test_no_budget_leaves_the_row_alone(self):
        self.add("c1")
        for stored in (None, "0", "-1", "soon"):
            store.kv_set(self.conn, "send_budget", stored)
            with self.subTest(stored=stored):
                self.assertEqual(self.claim("c1"), store.Claim("no_budget"))
                self.assert_untouched("c1", budget=0)

    def test_budget_is_spent_at_the_claim_and_never_refunded(self):
        store.arm(self.conn, 2)
        self.add("c1")
        self.add("c2", user="u2")
        claim = self.claim("c1")
        self.assertEqual(store.budget(self.conn), 1)
        self.assertEqual(store.record_dm(self.conn, claim.attempt_id, answer("not_sent", "connect", http=0)), "pending")
        self.assertEqual(store.budget(self.conn), 1)
        self.tick(SLOT)
        claim = self.claim("c1")
        self.assertEqual(store.budget(self.conn), 0)
        self.assertEqual(store.record_dm(self.conn, claim.attempt_id, answer("global", "rate", code=4)), "pending")
        self.assertEqual(store.kv_get(self.conn, "send_budget"), "0")
        self.tick(LONG)
        self.assertEqual(self.claim("c1"), store.Claim("no_budget"))
        self.assertEqual(self.claim("c2"), store.Claim("no_budget"))
        self.assertEqual(len(self.attempts()), 2)

    def test_unlimited_budget_stays_unlimited(self):
        store.arm(self.conn, "unlimited")
        self.add("c1")
        self.assertEqual(self.claim("c1").kind, "claimed")
        self.assertEqual(store.kv_get(self.conn, "send_budget"), "unlimited")

    def test_a_row_is_settled_even_when_nothing_may_be_sent(self):
        # steps 0-2 need no budget and no open gate; steps 3-5 never touch a row they do not claim
        off = dataclasses.replace(CFG, sending=False)
        for name, cfg, budget in ("no budget", CFG, 0), ("sending off", off, 3):
            store.arm(self.conn, budget)
            self.add(f"{name} handled", user=name, status="done")
            self.add(f"{name} thanks", user=f"{name} 1", text="спасибо")
            self.add(f"{name} old", user=f"{name} 2", age=WINDOW)
            self.add(f"{name} again", user=name)
            self.add(f"{name} good", user=f"{name} 3")
            with self.subTest(name):
                self.assertEqual([self.claim(f"{name} {row}", cfg).kind for row in ("thanks", "old", "again", "good")],
                                 ["skip", "skip", "skip", "no_budget" if budget == 0 else "sending_off"])
                self.assertEqual(self.state(f"{name} thanks"), ("seen", ""))
                self.assertEqual(self.state(f"{name} old"), ("expired", "window"))
                self.assertEqual(self.state(f"{name} again"), ("dup_user", f"dup:{name} handled"))
                self.assert_untouched(f"{name} good", budget)

    def test_steps_come_in_order(self):
        one_per_hour = dataclasses.replace(CFG, max_sends_per_hour=1)
        self.add("handled", status="done")
        self.add("no keyword, too old, same person", text="спасибо", age=WINDOW)
        self.add("too old, same person", age=WINDOW)
        self.assertEqual(self.claim("no keyword, too old, same person").kind, "skip")
        self.assertEqual(self.state("no keyword, too old, same person"), ("seen", ""))          # 0 before 1 and 2
        self.assertEqual(self.claim("too old, same person").kind, "skip")
        self.assertEqual(self.state("too old, same person"), ("expired", "window"))             # 1 before 2
        self.past_attempt(10, "ok")                                 # the slot is taken and, at 1 per hour, the hour is full
        self.add("good", user="u2")
        store.arm(self.conn, 0)
        self.assertEqual(self.claim("good", one_per_hour), store.Claim("no_budget"))            # 3 before 4
        store.arm(self.conn, 3)
        self.assertEqual(self.claim("good", one_per_hour), store.Claim("wait", seconds=3590.0))  # the slot, then the cap
        self.tick(3590)
        self.assertEqual(self.claim("good", one_per_hour), store.Claim("cap"))
        self.assert_untouched("good", attempts=1)

    def test_closed_gates(self):
        self.add("c1")
        off = dataclasses.replace(CFG, sending=False)
        for kind, cfg, me in ("sending_off", off, ME), ("no_identity", CFG, None), ("no_identity", CFG, {}), \
                ("no_identity", CFG, {"id": "9001", "username": "owner"}), \
                ("no_identity", CFG, {"user_id": "", "id": "9001", "username": "owner"}):
            with self.subTest(kind, me=me):
                self.assertEqual(self.claim("c1", cfg, me), store.Claim(kind))
                self.assert_untouched("c1")
        store.set_account_hold(self.conn, "auth")
        self.assertEqual(self.claim("c1"), store.Claim("account_hold"))
        self.assert_untouched("c1")
        self.tick(DAY)                                         # time does not lift an account hold
        self.assertEqual(self.claim("c1"), store.Claim("account_hold"))
        store.clear_account_hold(self.conn)
        store.set_send_hold(self.conn, "spam", 10 * MIN)
        self.assertEqual(self.claim("c1"), store.Claim("send_hold"))
        self.tick(10 * MIN - 0.001)
        self.assertEqual(self.claim("c1"), store.Claim("send_hold"))
        self.assert_untouched("c1")
        self.tick(0.001)
        self.assertEqual(self.claim("c1").kind, "claimed")

    def test_one_slot_between_two_dms(self):
        self.add("a", user="a")
        self.add("b", user="b")
        first = self.claim("a")
        self.tick(10)
        self.assertEqual(self.claim("b"), store.Claim("wait", seconds=50.0))     # in flight: counted from its start
        self.assert_untouched("b", budget=2, attempts=1)
        store.record_dm(self.conn, first.attempt_id, DM_OK)
        # answered 10 s after the claim: Meta received it somewhere in between, so the slot runs from the answer (G2)
        self.assertEqual(self.claim("b"), store.Claim("wait", seconds=60.0))
        self.tick(59.999)
        self.assertEqual(self.claim("b"), store.Claim("wait", seconds=0.001))
        self.assert_untouched("b", budget=2, attempts=1)
        self.tick(0.001)
        self.assertEqual(self.claim("b").kind, "claimed")

    def test_slot_follows_max_sends_per_hour(self):
        store.arm(self.conn, "unlimited")
        for per_hour, slot in (7, 514.286), (120, 30.0), (300, 12.0):
            cfg = dataclasses.replace(CFG, max_sends_per_hour=per_hour)
            self.tick(LONG)
            self.add(f"a{per_hour}", user=f"a{per_hour}")
            self.add(f"b{per_hour}", user=f"b{per_hour}")
            claim = self.claim(f"a{per_hour}", cfg)
            store.record_dm(self.conn, claim.attempt_id, DM_OK)
            with self.subTest(per_hour=per_hour):
                self.assertEqual(self.claim(f"b{per_hour}", cfg), store.Claim("wait", seconds=slot))
                self.tick(slot - 0.001)
                self.assertEqual(self.claim(f"b{per_hour}", cfg), store.Claim("wait", seconds=0.001))
                self.tick(0.001)
                self.assertEqual(self.claim(f"b{per_hour}", cfg).kind, "claimed")

    def test_a_failed_attempt_uses_its_slot_too(self):
        store.arm(self.conn, "unlimited")
        self.add("c1")
        for result in answer("not_sent", "connect", http=0), answer("rejected", "user_unavailable", code=551):
            self.tick(SLOT)
            claim = self.claim("c1")
            self.assertEqual(claim.kind, "claimed")
            self.tick(3)
            store.record_dm(self.conn, claim.attempt_id, result)
            self.add(f"next after {result.outcome}", user=result.outcome)
            with self.subTest(result.outcome):
                self.assertEqual(self.claim(f"next after {result.outcome}"), store.Claim("wait", seconds=60.0))

    def test_public_replies_take_no_slot_and_no_place_under_the_cap(self):
        cfg = dataclasses.replace(CFG, max_sends_per_hour=3)
        self.journal(*[("public", "ok"), ("public", "rejected"), ("public", None)] * 3)
        self.add("c1")
        self.assertEqual(self.claim("c1", cfg).kind, "claimed")

    def test_cap_counts_every_attempt_through_the_conservative_receipt_window(self):
        cfg = dataclasses.replace(CFG, max_sends_per_hour=3)        # one slot is 20 minutes
        self.past_attempt(HOUR + clock.T["cap_margin"] - 1, "not_sent")
        self.past_attempt(2400, "uncertain")
        self.past_attempt(1200)                                     # still in flight
        self.add("c1")
        self.assertEqual(self.claim("c1", cfg), store.Claim("cap"))
        self.assert_untouched("c1", attempts=3)
        self.tick(0.999)
        self.assertEqual(self.claim("c1", cfg), store.Claim("cap"))
        self.tick(0.001)                                            # exactly at the conservative cutoff: out of count
        self.assertEqual(self.claim("c1", cfg).kind, "claimed")

    def test_sixty_per_hour(self):
        store.arm(self.conn, "unlimited")
        for n in range(60):
            self.add(f"c{n}", user=f"u{n}", age=0)
            claim = self.claim(f"c{n}")
            self.assertEqual(claim.kind, "claimed", n)
            store.record_dm(self.conn, claim.attempt_id, DM_OK)
            self.tick(SLOT)
        self.add("one more", user="one more", age=0)                # 3600 s after the first of the 60
        self.assertEqual(self.claim("one more"), store.Claim("cap"))
        self.tick(clock.T["cap_margin"] - 0.001)
        self.assertEqual(self.claim("one more"), store.Claim("cap"))
        self.tick(0.001)
        self.assertEqual(self.claim("one more").kind, "claimed")

    def test_one_per_hour(self):
        cfg = dataclasses.replace(CFG, max_sends_per_hour=1)
        self.add("a", user="a")
        self.add("b", user="b")
        self.assertEqual(self.claim("a", cfg).kind, "claimed")
        self.tick(HOUR - 0.001)
        self.assertEqual(self.claim("b", cfg), store.Claim("wait", seconds=0.001))
        self.tick(0.001)
        self.assertEqual(self.claim("b", cfg), store.Claim("cap"))
        self.tick(clock.T["cap_margin"])
        self.assertEqual(self.claim("b", cfg).kind, "claimed")

    def test_claim_cannot_hide_inside_a_larger_transaction(self):
        self.add("c1")
        with store.tx(self.conn):
            with self.assertRaises(RuntimeError):
                self.claim("c1")
        self.assert_untouched("c1")

    def test_claim_is_all_or_nothing(self):
        self.add("c1")
        with mock.patch.object(store, "kv_set", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.claim("c1")
        self.assert_untouched("c1")
        self.assertEqual(self.claim("c1").kind, "claimed")


# ---------- the answer to a DM (7.3) ----------

UNCERTAIN = answer("uncertain", "http_5xx", http=502)
BLOCKED = answer("global", "blocked", code=10, subcode=None, message="(#10) no permission")
REFUSED = answer("rejected", "user_unavailable", code=551)
NOT_SENT = answer("not_sent", "connect", http=0, message="connection refused")


class RecordDm(Db):
    def setUp(self):
        super().setUp()
        store.arm(self.conn, "unlimited")

    def test_every_outcome(self):
        for n, (result, status, reason, deferrals, meta_id) in enumerate([
            (answer("ok", http=200, data={"recipient_id": "r", "message_id": "mid.7"}), "sent", "", 0, "mid.7"),
            (answer("ok", http=200, data={"message_id": "mid\udc00"}), "sent", "", 0, "mid\ufffd"),
            (answer("uncertain", "http_5xx", http=502), "uncertain", "http_5xx", 0, None),
            (answer("uncertain", "code_1", http=400, code=1, message="An unknown error"), "uncertain", "code_1", 0, None),
            (answer("uncertain", "no_response", http=0, message="timed out"), "uncertain", "no_response", 0, None),
            (answer("uncertain", "ok_without_id", http=200, data={"message_id": 5}), "uncertain", "ok_without_id", 0, None),
            (answer("not_sent", "connect", http=0, message="refused"), "pending", "", 0, None),
            (answer("global", "rate", http=429), "pending", "", 1, None),
            (answer("global", "auth", http=400, code=190, subcode=460), "pending", "", 1, None),
            (answer("rejected", "already_replied", http=400, code=10900), "already", "already_replied", 0, None),
            (answer("rejected", "user_unavailable", http=400, code=551), "refused", "user_unavailable", 0, None),
            (answer("rejected", "comment_gone", http=400, code=100, subcode=33), "refused", "comment_gone", 0, None),
            (answer("rejected", "outside_window", http=400, code=10, subcode=2534022), "refused", "outside_window", 0, None),
            (answer("rejected", "invalid", http=400, code=100, subcode=2534015), "refused", "invalid", 0, None),
            (answer("transient", "", http=503), "uncertain", "", 0, None),      # not an answer a POST can have: G4
            (answer("", "odd"), "uncertain", "odd", 0, None),
        ]):
            self.tick(LONG)
            comment_id = f"c{n}"
            self.add(comment_id, user=f"u{n}")
            claim = self.claim(comment_id)
            started = clock.iso(self.now)
            self.tick(2)
            with self.subTest(outcome=result.outcome, reason=result.reason):
                self.assertEqual(store.record_dm(self.conn, claim.attempt_id, result), status)
                self.assertFalse(self.conn.in_transaction)
                row = store.comment(self.open(), comment_id)
                self.assertEqual((row["status"], row["reason"], row["deferrals"], row["updated_at"]),
                                 (status, reason, deferrals, clock.iso(self.now)))
                self.assertEqual(self.attempts()[-1], {
                    "id": claim.attempt_id, "comment_id": comment_id, "kind": "dm", "detail": "fresh",
                    "started_at": started, "finished_at": clock.iso(self.now), "http": result.http, "code": result.code,
                    "subcode": result.subcode, "message": result.message,
                    "outcome": result.outcome if status != "uncertain" else "uncertain", "reason": result.reason,
                    "meta_id": meta_id})

    def test_not_sent_comment_can_be_claimed_again(self):
        self.add("c1")
        claim = self.claim("c1")
        store.record_dm(self.conn, claim.attempt_id, NOT_SENT)
        self.tick(SLOT)
        again = self.claim("c1")
        self.assertEqual((again.kind, again.attempt_id), ("claimed", 2))

    def test_no_answer_but_not_sent_and_global_allows_a_second_claim(self):
        for n, result in enumerate([DM_OK, UNCERTAIN, REFUSED, answer("rejected", "already_replied", code=10900),
                                    answer("weird")]):
            comment_id, _ = self.dm(result)
            self.tick(LONG)
            with self.subTest(result.outcome, reason=result.reason):
                self.assertEqual(self.claim(comment_id), store.Claim("skip"))
                self.assertEqual(len(self.attempts()), n + 1)

    def test_global_again_after_somebody_else_got_through_remains_account_wide(self):
        self.add("x", user="x")
        self.assertEqual(self.dm(BLOCKED, "x"), ("x", "pending"))
        self.dm(DM_OK)
        self.assertEqual(self.dm(answer("global", "rate", code=613, subcode=1996), "x"), ("x", "pending"))
        row = store.comment(self.conn, "x")
        self.assertEqual((row["status"], row["reason"], row["deferrals"]), ("pending", "", 2))
        # the refusal was still worded as account-wide, so sending pauses as after any such answer (8.5)
        self.assertEqual(store.get_send_hold(self.conn),
                         {"reason": "send_blocked", "until": self.ago(-30 * MIN), "detail": "dm global/rate"})
        self.tick(LONG)
        self.assertEqual(self.claim("x").kind, "claimed")

    def test_global_without_code_or_subcode_remains_pending(self):
        for result in (BLOCKED, answer("global", "rate", http=429)):
            self.people += 1
            comment_id = f"x{self.people}"
            self.add(comment_id, user=comment_id)
            self.dm(BLOCKED, comment_id)
            self.dm(DM_OK)
            self.dm(result, comment_id)
            with self.subTest(reason=result.reason):
                self.assertEqual(self.state(comment_id), ("pending", ""))

    def test_global_again_while_nobody_got_through_is_the_accounts_fault(self):
        self.dm(DM_OK)                                              # before x was refused: proves nothing
        self.add("x", user="x")
        self.assertEqual(self.dm(BLOCKED, "x"), ("x", "pending"))
        for result in UNCERTAIN, REFUSED, BLOCKED, NOT_SENT:        # others tried after it, none got through
            self.dm(result)
        self.journal(("public", "ok"))                              # a public reply that worked is not a DM
        self.past_attempt(0, "ok", comment_id="x")                  # cannot happen; it would not be "another comment"
        self.assertEqual(self.dm(BLOCKED, "x"), ("x", "pending"))
        row = store.comment(self.conn, "x")
        self.assertEqual((row["status"], row["reason"], row["deferrals"]), ("pending", "", 2))

    def test_first_global_after_a_failed_connection_is_not_suspect(self):
        self.add("x", user="x")
        self.assertEqual(self.dm(NOT_SENT, "x"), ("x", "pending"))  # an earlier attempt, but not a refusal
        self.dm(DM_OK)
        self.assertEqual(self.dm(BLOCKED, "x"), ("x", "pending"))
        self.assertEqual(store.comment(self.conn, "x")["deferrals"], 1)

    def test_an_answer_is_recorded_once_and_only_for_a_dm_claim(self):
        self.sent("c1")
        public = self.claim_public("c1")
        before = self.everything()
        for attempt_id in (1, public.attempt_id, 99, None):
            for result in (DM_OK, REFUSED):
                with self.subTest(attempt_id=attempt_id, outcome=result.outcome):
                    with self.assertRaises(ValueError):
                        store.record_dm(self.conn, attempt_id, result)
        self.assertEqual(self.everything(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_journal_and_comment_change_together_or_not_at_all(self):
        self.add("c1")
        claim = self.claim("c1")
        self.force("c1", "pending")                                 # cannot happen; the answer must not be half recorded
        before = self.everything()
        with self.assertRaises(RuntimeError):
            store.record_dm(self.conn, claim.attempt_id, DM_OK)
        self.assertEqual(self.everything(), before)
        self.assertFalse(self.conn.in_transaction)


# ---------- send hold (8.5) ----------

U, K, N, G, R = ("dm", "uncertain"), ("dm", "ok"), ("dm", "not_sent"), ("dm", "global"), ("dm", "rejected")
PU, PK, PN = ("public", "uncertain"), ("public", "ok"), ("public", "not_sent")
PG, PR = ("public", "global"), ("public", "rejected")
FLYING = ("dm", None)


class SendHold(Db):
    def setUp(self):
        super().setUp()
        store.arm(self.conn, "unlimited")

    def assert_waits(self, wait, table):
        for journal, minutes in table:
            self.conn.execute("DELETE FROM attempts")               # set-up only: the bot never deletes
            self.journal(*journal)
            with self.subTest(journal=" ".join(f"{kind}:{outcome}" for kind, outcome in journal)):
                self.assertEqual(wait(self.conn), minutes * MIN)

    def test_uncertain_wait(self):
        self.assert_waits(store.uncertain_wait, [
            ([], 0), ([U], 15), ([U, U], 30), ([U] * 3, 60), ([U] * 4, 120), ([U] * 5, 240), ([U] * 6, 360),
            ([U] * 7, 360), ([U] * 40, 360),
            ([U, K], 0), ([K, U], 15), ([U, U, K, U], 15), ([U, R, U], 15), ([U, G, U], 15), ([U, FLYING], 0),
            ([U, N, U], 30), ([U, N, N], 15),                       # not_sent never reached Meta: passed over
            ([U, PU, U], 30), ([U, PK, U], 30), ([PU, PU], 0),      # only DMs count
        ])

    def test_blocked_wait(self):
        self.assert_waits(store.blocked_wait, [
            ([], 0), ([K], 0), ([G], 30), ([PG], 30), ([G, PG], 60), ([G] * 3, 120), ([G] * 4, 240), ([G] * 5, 480),
            ([G] * 6, 720), ([G] * 7, 720), ([G] * 30, 720),
            ([G, K, G], 30), ([G, PK, G], 30), ([G, R, PG], 30), ([G, U, G], 30), ([G, PU, G], 30), ([G, FLYING], 0),
            ([G, N, PN, G], 60),
        ])

    def test_streak_wait(self):
        self.assert_waits(store.streak_wait, [
            ([], 0), ([R] * 4, 0), ([R] * 5, 60), ([U, G, R, U, G], 60), ([R] * 6, 120), ([R] * 7, 240), ([R] * 8, 480),
            ([R] * 9, 720), ([R] * 20, 720),
            ([K] + [R] * 5, 60), ([R] * 4 + [K] + [R], 0), ([R] * 5 + [K], 0),
            ([R, R, R, N, N, N, R, R], 60), ([R] * 4 + [N] * 5, 0),                 # not_sent excluded
            ([R] * 4 + [PR] * 3, 0), ([R] * 4 + [PK] + [R], 60),                    # only DMs count
        ])

    def test_uncertain_dm_holds_sending(self):
        self.assertIsNone(store.get_send_hold(self.conn))
        self.dm(UNCERTAIN)
        hold = {"reason": "uncertain", "until": self.ago(-15 * MIN), "detail": "dm uncertain/http_5xx"}
        self.assertEqual(store.get_send_hold(self.open()), hold)
        self.assertEqual(json.loads(store.kv_get(self.conn, "send_hold")), hold)
        self.add("next", user="next")
        self.assertEqual(self.claim("next"), store.Claim("send_hold"))
        self.tick(15 * MIN - 0.001)
        self.assertEqual(store.get_send_hold(self.conn), hold)
        self.assertEqual(self.claim("next"), store.Claim("send_hold"))
        self.tick(0.001)
        self.assertIsNone(store.get_send_hold(self.conn))
        self.assertEqual(store.get_send_hold(self.conn, self.now - timedelta(milliseconds=1)), hold)   # as seen at `now`
        self.assertEqual(self.claim("next").kind, "claimed")

    def test_hold_grows_with_every_uncertain_dm_in_a_row(self):
        for reason, minutes in ("uncertain", 15), ("uncertain", 30), ("uncertain", 60), ("uncertain", 120), \
                ("uncertain", 240), ("uncertain", 360), ("uncertain", 360), ("streak", 480), ("streak", 720), \
                ("streak", 720):
            self.dm(UNCERTAIN)
            hold = store.get_send_hold(self.conn)
            with self.subTest(minutes=minutes):
                self.assertEqual((hold["reason"], hold["until"]), (reason, self.ago(-minutes * MIN)))
        self.dm(DM_OK)
        self.dm(UNCERTAIN)
        self.assertEqual(store.get_send_hold(self.conn)["until"], self.ago(-15 * MIN))

    def test_account_wide_refusal_holds_sending(self):
        self.sent("replied")                                        # its public reply comes last
        for result, minutes in (answer("global", "auth", code=190), 30), (answer("global", "blocked", code=10), 60), \
                (answer("global", "rate", http=429), 120):
            self.dm(result)
            with self.subTest(result.reason):
                self.assertEqual(store.get_send_hold(self.conn), {
                    "reason": "send_blocked", "until": self.ago(-minutes * MIN), "detail": f"dm global/{result.reason}"})
        self.tick(LONG)
        public = self.claim_public("replied")
        store.record_public(self.conn, public.attempt_id, answer("global", "rate", code=4))
        self.assertEqual(store.get_send_hold(self.conn), {
            "reason": "send_blocked", "until": self.ago(-240 * MIN), "detail": "public global/rate"})
        self.assertEqual(store.budget(self.conn), "unlimited")

    def test_spam_refusal_holds_for_a_day_and_disarms(self):
        spam = answer("global", "spam", code=368)
        for budget in ("unlimited", 5):
            store.arm(self.conn, budget)
            self.dm(spam)
            with self.subTest("dm", budget=budget):
                self.assertEqual(store.get_send_hold(self.conn),
                                 {"reason": "spam", "until": self.ago(-DAY), "detail": "dm global/spam"})
                self.assertEqual(store.kv_get(self.conn, "send_budget"), "0")
        self.sent("replied")
        self.tick(LONG)
        public = self.claim_public("replied")
        store.record_public(self.conn, public.attempt_id, spam)
        self.assertEqual(store.get_send_hold(self.conn),
                         {"reason": "spam", "until": self.ago(-DAY), "detail": "public global/spam"})
        self.assertEqual(store.kv_get(self.conn, "send_budget"), "0")
        self.assertEqual(store.kv_get(self.conn, "armed_n"), "unlimited")       # the operator's last word stays on record

    def test_answers_that_set_no_hold(self):
        for result in DM_OK, NOT_SENT, REFUSED:
            self.dm(result)
            with self.subTest("dm", outcome=result.outcome):
                self.assertIsNone(store.kv_get(self.conn, "send_hold"))
        for n, result in enumerate([PUBLIC_OK, answer("uncertain", "http_5xx", http=500),
                                    answer("not_sent", "connect", http=0),
                                    answer("rejected", "comment_gone", code=100, subcode=33)]):
            self.sent(f"replied{n}", user=f"replied{n}")
            public = self.claim_public(f"replied{n}")
            store.record_public(self.conn, public.attempt_id, result)
            with self.subTest("public", outcome=result.outcome):
                self.assertIsNone(store.kv_get(self.conn, "send_hold"))
        self.assertEqual(store.budget(self.conn), "unlimited")

    def test_five_dms_in_a_row_without_ok(self):
        for n in range(1, 8):
            self.dm(REFUSED if n % 2 else answer("rejected", "invalid", code=100))
            if n == 3:
                self.dm(NOT_SENT)                                   # does not count and does not break the row
            hold = store.get_send_hold(self.conn)
            with self.subTest(n=n):
                if n < 5:
                    self.assertIsNone(hold)
                else:
                    self.assertEqual((hold["reason"], hold["until"]), ("streak", self.ago(-HOUR * 2 ** (n - 5))))
        self.assertEqual(store.get_send_hold(self.conn)["detail"], "dm rejected/user_unavailable")
        self.dm(NOT_SENT)                                           # after the hold ran out: sets none of its own
        self.assertIsNone(store.get_send_hold(self.conn))

    def test_public_answer_is_not_judged_by_the_dm_journal(self):
        self.sent("first")                                          # their public replies come after five bad DMs
        self.sent("second", user="u2")
        for _ in range(5):
            self.dm(UNCERTAIN)
        for comment_id, result in ("first", answer("uncertain", "http_5xx", http=500)), \
                ("second", answer("rejected", "invalid", code=100)):
            self.tick(LONG)
            public = self.claim_public(comment_id)
            store.record_public(self.conn, public.attempt_id, result)
            with self.subTest(outcome=result.outcome):
                self.assertIsNone(store.get_send_hold(self.conn))

    def test_until_only_moves_later(self):
        store.set_send_hold(self.conn, "spam", 2 * HOUR, "first")
        first = {"reason": "spam", "until": self.ago(-2 * HOUR), "detail": "first"}
        self.assertEqual(store.get_send_hold(self.conn), first)
        store.set_send_hold(self.conn, "uncertain", HOUR, "ends sooner")
        self.assertEqual(store.get_send_hold(self.conn), first)
        store.set_send_hold(self.conn, "uncertain", 2 * HOUR, "ends at the same time")
        self.assertEqual(store.get_send_hold(self.conn), first)
        self.tick(1)
        store.set_send_hold(self.conn, "streak", 2 * HOUR)
        self.assertEqual(store.get_send_hold(self.conn), {"reason": "streak", "until": self.ago(-2 * HOUR), "detail": ""})
        self.tick(3 * HOUR)                                         # an old hold is no obstacle to a new, shorter one
        store.set_send_hold(self.conn, "uncertain", 60)
        self.assertEqual(store.get_send_hold(self.conn)["until"], self.ago(-60))

    def test_an_answer_does_not_shorten_a_longer_hold(self):
        self.add("c1")
        claim = self.claim("c1")
        store.set_send_hold(self.conn, "spam", 10 * HOUR, "set meanwhile")
        store.record_dm(self.conn, claim.attempt_id, UNCERTAIN)
        self.assertEqual(store.get_send_hold(self.conn),
                         {"reason": "spam", "until": self.ago(-10 * HOUR), "detail": "set meanwhile"})


# ---------- recovery and crashes (7.3) ----------

class Recover(Db):
    def setUp(self):
        super().setUp()
        store.arm(self.conn, "unlimited")

    def test_nothing_open_nothing_changes(self):
        self.dm(DM_OK)
        self.dm(UNCERTAIN)                                          # the hold it set must not be renewed every cycle
        before = self.everything()
        self.tick(90)
        self.assertEqual(store.recover_stale(self.conn), 0)
        self.assertEqual(self.everything(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_open_dm_claim_becomes_uncertain(self):
        self.add("c1")
        claim = self.claim("c1")
        started = clock.iso(self.now)
        self.tick(90)
        self.assertEqual(store.recover_stale(self.conn), 1)
        row = store.comment(self.open(), "c1")
        self.assertEqual((row["status"], row["reason"], row["updated_at"]),
                         ("uncertain", "interrupted", clock.iso(self.now)))
        self.assertEqual(self.attempts(), [{
            "id": 1, "comment_id": "c1", "kind": "dm", "detail": "fresh", "started_at": started,
            "finished_at": clock.iso(self.now), "http": None, "code": None, "subcode": None, "message": None,
            "outcome": "uncertain", "reason": "interrupted", "meta_id": None}])
        after = self.everything()
        self.assertEqual(store.recover_stale(self.conn), 0)
        with self.assertRaises(ValueError):                         # the dead run's answer cannot arrive any more
            store.record_dm(self.conn, claim.attempt_id, DM_OK)
        self.assertEqual(self.everything(), after)
        self.tick(LONG)
        self.assertEqual(self.claim("c1"), store.Claim("skip"))

    def test_open_public_claim_becomes_done(self):
        self.sent("c1")
        public = self.claim_public("c1")
        self.tick(90)
        self.assertEqual(store.recover_stale(self.conn), 1)
        self.assertEqual(self.state("c1"), ("done", "public_uncertain"))
        closed = self.attempts("public")
        self.assertEqual(len(closed), 1)
        self.assertEqual((closed[0]["finished_at"], closed[0]["outcome"], closed[0]["reason"]),
                         (clock.iso(self.now), "uncertain", "interrupted"))
        self.assertIsNone(store.kv_get(self.conn, "send_hold"))     # only an uncertain DM holds sending
        with self.assertRaises(ValueError):
            store.record_public(self.conn, public.attempt_id, PUBLIC_OK)
        self.assertEqual(self.claim_public("c1"), store.PublicClaim("skip"))
        self.assertEqual(len(self.attempts("public")), 1)

    def test_rows_without_their_other_half(self):
        self.add("claimed, no journal row", status="sending")
        self.add("public reply owed", user="u2", status="sent")
        self.add("public reply answered", user="u3", status="sent")
        self.past_attempt(200, "rejected", "public", "public reply answered")
        self.add("sent, dm row open", user="u4", status="sent")
        self.past_attempt(100, comment_id="sent, dm row open")
        self.add("waiting", user="u5")
        self.assertEqual(store.recover_stale(self.conn), 1)
        self.assertEqual(self.state("claimed, no journal row"), ("uncertain", "interrupted"))
        for comment_id in ("public reply owed", "public reply answered", "sent, dm row open"):
            self.assertEqual(self.state(comment_id), ("sent", ""))  # only an OPEN PUBLIC claim settles a `sent` comment
        self.assertEqual(self.state("waiting"), ("pending", ""))
        self.assertEqual([(row["outcome"], row["reason"]) for row in self.attempts()],
                         [("rejected", None), ("uncertain", "interrupted")])

    def test_a_crash_loop_cannot_spend_one_person_per_restart(self):
        for minutes in (15, 30, 60):
            self.tick(LONG)
            self.people += 1
            self.add(f"c{self.people}", user=f"u{self.people}")
            self.assertEqual(self.claim(f"c{self.people}").kind, "claimed")
            self.assertEqual(store.recover_stale(self.conn), 1)
            with self.subTest(minutes=minutes):
                self.assertEqual(store.get_send_hold(self.conn), {
                    "reason": "uncertain", "until": self.ago(-minutes * MIN), "detail": "dm uncertain/interrupted"})
        self.add("next", user="next")
        self.assertEqual(self.claim("next"), store.Claim("send_hold"))


class Crash(Db):
    def restart(self):
        self.conn.close()
        self.conn = self.open()

    def assert_never_again(self):
        """c1 was claimed by a run that died; c2 is the same person's other comment."""
        self.tick(LONG)
        self.assertEqual(self.claim("c1"), store.Claim("skip"))
        self.assertEqual(self.claim("c2"), store.Claim("skip"))
        self.assertEqual(self.state("c2"), ("dup_user", "dup:c1"))
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(store.budget(self.conn), 4)

    def test_dead_run_before_recovery(self):
        store.arm(self.conn, 5)
        self.add("c1")
        self.add("c2")
        self.assertEqual(self.claim("c1").kind, "claimed")
        self.restart()                                              # no record_dm: the process is gone
        self.assert_never_again()
        self.assertEqual(self.state("c1"), ("sending", ""))

    def test_dead_run_after_recovery(self):
        store.arm(self.conn, 5)
        self.add("c1")
        self.add("c2")
        self.assertEqual(self.claim("c1").kind, "claimed")
        self.restart()
        self.assertEqual(store.recover_stale(self.conn), 1)
        self.assert_never_again()
        self.assertEqual(self.state("c1"), ("uncertain", "interrupted"))
        self.add("c3")                                              # the person comments once more
        self.assertEqual(self.claim("c3"), store.Claim("skip"))
        self.assertEqual(self.state("c3"), ("dup_user", "dup:c1"))
        self.add("c4", user="somebody else")
        self.assertEqual(self.claim("c4").kind, "claimed")
        self.assertEqual([row["comment_id"] for row in self.attempts()], ["c1", "c4"])

    def kill(self, before_claim=""):
        """A real process claims c1 and dies at once, without closing anything."""
        self.conn.close()
        script = ("import os, sys\n"
                  "import clock, store\n"
                  "from tests import test_store\n"
                  "clock.now = lambda: test_store.START\n"
                  f"{before_claim}\n"
                  "print(store.claim_dm(store.connect(sys.argv[1]), 'c1', test_store.CFG, test_store.ME).kind, flush=True)\n"
                  "os._exit(9)\n")
        done = subprocess.run([sys.executable, "-B", "-c", script, self.path], cwd=CODE, capture_output=True, text=True,
                              timeout=60)
        self.conn = self.open()
        return done.returncode, done.stdout.strip(), done.stderr

    def test_process_killed_right_after_the_claim(self):
        store.arm(self.conn, 5)
        self.add("c1")
        self.add("c2")
        code, printed, errors = self.kill()
        self.assertEqual((code, printed), (9, "claimed"), errors)
        self.assertEqual(self.state("c1"), ("sending", ""))        # the claim is on disk: the POST may have left
        self.assert_never_again()
        self.assertEqual(store.recover_stale(self.conn), 1)
        self.assert_never_again()

    def test_process_killed_inside_the_claim(self):
        store.arm(self.conn, 5)
        self.add("c1")
        code, printed, errors = self.kill(before_claim="store.kv_set = lambda *spending_the_budget: os._exit(9)")
        self.assertEqual((code, printed), (9, ""), errors)          # it never returned, so no POST was made
        self.assertEqual(self.state("c1"), ("pending", ""))
        self.assertEqual(self.attempts(), [])
        self.assertEqual(store.budget(self.conn), 5)
        self.assertEqual(store.recover_stale(self.conn), 0)
        self.assertEqual(self.claim("c1").kind, "claimed")


class Races(Db):
    def race(self, *jobs):
        """Each job on its own connection in its own thread, all let go at the same moment."""
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def run(index, job):
            conn = store.connect(self.path)
            try:
                barrier.wait(10)
                results[index] = job(conn)
            except BaseException as error:
                results[index] = error
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=item) for item in enumerate(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        return results

    def test_two_comments_of_one_person(self):
        store.arm(self.conn, "unlimited")
        for n in range(20):
            self.tick(SLOT)
            first, second = f"first{n}", f"second{n}"
            self.add(first, user=f"u{n}", age=120)
            self.add(second, user=f"u{n}", age=90)
            claims = self.race(lambda conn: store.claim_dm(conn, first, CFG, ME),
                               lambda conn: store.claim_dm(conn, second, CFG, ME))
            with self.subTest(n=n):
                self.assertEqual(sorted(claim.kind for claim in claims), ["claimed", "skip"])
                winner, loser = (first, second) if claims[0].kind == "claimed" else (second, first)
                self.assertEqual(self.state(winner), ("sending", ""))
                self.assertEqual(self.state(loser), ("dup_user", "dup:" + winner))
                self.assertEqual(len(self.attempts()), n + 1)

    def test_one_comment_from_two_connections(self):
        store.arm(self.conn, 5)
        self.add("c1")
        claims = self.race(*[lambda conn: store.claim_dm(conn, "c1", CFG, ME)] * 4)
        self.assertEqual(sorted(claim.kind for claim in claims), ["claimed", "skip", "skip", "skip"])
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(store.budget(self.conn), 4)

    def test_two_people_at_once_get_one_slot(self):
        store.arm(self.conn, "unlimited")
        self.add("a", user="a")
        self.add("b", user="b")
        claims = self.race(lambda conn: store.claim_dm(conn, "a", CFG, ME), lambda conn: store.claim_dm(conn, "b", CFG, ME))
        self.assertEqual(sorted(claim.kind for claim in claims), ["claimed", "wait"])
        self.assertEqual(len(self.attempts()), 1)


# ---------- the public reply (7.4) ----------

class Public(Db):
    def reply(self, result):
        """A new person's confirmed DM, then the public reply claimed and answered with `result`."""
        self.people += 1
        comment_id = f"p{self.people}"
        self.sent(comment_id, user=comment_id)
        self.tick(LONG)
        claim = self.claim_public(comment_id)
        self.assertEqual(claim.kind, "claimed")
        return comment_id, store.record_public(self.conn, claim.attempt_id, result)

    def test_claim_is_committed_before_it_returns(self):
        self.sent("c1")
        self.tick(3)
        before = time.monotonic()
        with mock.patch.object(store.random, "choice", lambda phrases: phrases[1]):
            claim = self.claim_public("c1")
        self.assertLessEqual(before, claim.monotonic)
        self.assertLessEqual(claim.monotonic, time.monotonic())
        self.assertEqual(claim, store.PublicClaim("claimed", 2, "In your inbox", claim.monotonic, claim.started_at))
        self.assertEqual((claim.kind, claim.attempt_id, claim.phrase), ("claimed", 2, "In your inbox"))
        self.assertFalse(self.conn.in_transaction)
        other = self.open()
        self.assertEqual(store.comment(other, "c1")["status"], "sent")     # owed until the answer is recorded
        self.assertEqual(dict(other.execute("SELECT * FROM attempts WHERE id=2").fetchone()), {
            "id": 2, "comment_id": "c1", "kind": "public", "detail": "In your inbox", "started_at": clock.iso(self.now),
            "finished_at": None, "http": None, "code": None, "subcode": None, "message": None, "outcome": None,
            "reason": None, "meta_id": None})
        self.assertEqual(store.kv_get(other, "last_phrase"), "In your inbox")

    def test_phrase_is_one_of_the_config_and_never_the_last_one(self):
        phrases = []
        for _ in range(40):
            self.reply(PUBLIC_OK)
            phrases.append(self.attempts("public")[-1]["detail"])
        self.assertEqual(set(phrases), set(CFG.public_phrases))
        self.assertTrue(all(one != next_one for one, next_one in zip(phrases, phrases[1:])))
        self.assertEqual(store.kv_get(self.conn, "last_phrase"), phrases[-1])

    def test_a_single_phrase_is_used_again(self):
        store.kv_set(self.conn, "last_phrase", "Only this")
        self.sent("c1")
        cfg = dataclasses.replace(CFG, public_phrases=("Only this",))
        self.assertEqual(self.claim_public("c1", cfg).phrase, "Only this")

    def test_only_a_sent_comment_gets_a_public_reply(self):
        for status in STATUSES:
            if status != "sent":
                self.add(status, user=status, status=status)
                with self.subTest(status=status):
                    self.assertEqual(self.claim_public(status), store.PublicClaim("skip", None, None))
                    self.assertEqual(self.state(status), (status, ""))
        self.assertEqual(self.claim_public("nobody"), store.PublicClaim("skip"))
        self.assertEqual(self.attempts(), [])

    def test_switched_off_means_done_without_a_post(self):
        for name, cfg in ("config", dataclasses.replace(CFG, public_reply=False)), \
                ("no phrases", dataclasses.replace(CFG, public_phrases=())):
            self.sent(name, user=name)
            with self.subTest(name):
                self.assertEqual(self.claim_public(name, cfg), store.PublicClaim("skip"))
                self.assertEqual(self.state(name), ("done", "public_skipped"))
        self.assertEqual(self.attempts("public"), [])
        self.sent("c1")
        self.assertEqual(self.claim_public("c1").kind, "claimed")

    def test_closed_gates(self):
        self.sent("c1")
        off = dataclasses.replace(CFG, sending=False)

        def assert_owed():
            self.assertEqual(self.state("c1"), ("sent", ""))
            self.assertEqual(self.attempts("public"), [])
            self.assertFalse(self.conn.in_transaction)

        for kind, cfg, me in ("sending_off", off, ME), ("no_identity", CFG, None), ("no_identity", CFG, {}), \
                ("no_identity", CFG, {"id": "9001", "username": "owner"}):
            with self.subTest(kind, me=me):
                self.assertEqual(self.claim_public("c1", cfg, me), store.PublicClaim(kind))
                assert_owed()
        store.set_account_hold(self.conn, "blocked")
        self.assertEqual(self.claim_public("c1"), store.PublicClaim("account_hold"))
        assert_owed()
        store.clear_account_hold(self.conn)
        store.set_send_hold(self.conn, "send_blocked", 30 * MIN)
        self.assertEqual(self.claim_public("c1"), store.PublicClaim("send_hold"))
        assert_owed()
        self.tick(30 * MIN)
        self.assertEqual(self.claim_public("c1").kind, "claimed")

    def test_needs_no_budget(self):
        store.arm(self.conn, 1)
        self.add("c1")
        self.dm(DM_OK, "c1")
        self.assertEqual(store.budget(self.conn), 0)                # G6: everything except the budget
        self.assertEqual(self.claim_public("c1").kind, "claimed")
        self.assertEqual(store.budget(self.conn), 0)

    def test_never_a_second_public_post(self):
        comment_id, _ = self.reply(PUBLIC_OK)
        self.assertEqual(self.state(comment_id), ("done", ""))
        self.assertEqual(self.claim_public(comment_id), store.PublicClaim("skip"))
        self.sent("c2", user="u2")                                  # still `sent` with a public POST on record: cannot
        self.conn.execute("INSERT INTO attempts (comment_id, kind, started_at, finished_at, outcome) "    # happen, but
                          "VALUES ('c2', 'public', ?, ?, 'uncertain')", (clock.iso(), clock.iso()))         # if it does
        self.assertEqual(self.claim_public("c2"), store.PublicClaim("skip"))
        self.assertEqual(self.state("c2"), ("done", "public_uncertain"))
        self.assertEqual([row["comment_id"] for row in self.attempts("public")], [comment_id, "c2"])

    def test_claim_cannot_hide_inside_a_larger_transaction(self):
        self.sent("c1")
        with store.tx(self.conn):
            with self.assertRaises(RuntimeError):
                self.claim_public("c1")
        self.assertEqual(self.state("c1"), ("sent", ""))
        self.assertEqual(self.attempts("public"), [])

    def test_every_outcome(self):
        for result, reason, meta_id in [
            (answer("ok", http=200, data={"id": "reply.9"}), "", "reply.9"),
            (answer("rejected", "comment_gone", code=100, subcode=33, message="gone"), "public_failed", None),
            (answer("rejected", "already_replied", code=10900), "public_failed", None),
            (answer("global", "rate", http=429), "public_failed", None),
            (answer("uncertain", "http_5xx", http=503), "public_uncertain", None),
            (answer("uncertain", "ok_without_id", http=200), "public_uncertain", None),
            (answer("not_sent", "connect", http=0, message="refused"), "public_uncertain", None),
            (answer("gone", "", http=400), "public_uncertain", None),          # not an answer a POST can have
        ]:
            self.people += 1
            comment_id = f"p{self.people}"
            self.sent(comment_id, user=comment_id)
            self.tick(LONG)
            claim = self.claim_public(comment_id)
            started = clock.iso(self.now)
            self.tick(2)
            with self.subTest(outcome=result.outcome, reason=result.reason):
                self.assertEqual(store.record_public(self.conn, claim.attempt_id, result), reason)
                self.assertFalse(self.conn.in_transaction)
                row = store.comment(self.open(), comment_id)
                self.assertEqual((row["status"], row["reason"], row["updated_at"]), ("done", reason, clock.iso(self.now)))
                self.assertEqual(self.attempts()[-1], {
                    "id": claim.attempt_id, "comment_id": comment_id, "kind": "public", "detail": claim.phrase,
                    "started_at": started, "finished_at": clock.iso(self.now), "http": result.http, "code": result.code,
                    "subcode": result.subcode, "message": result.message,
                    "outcome": "uncertain" if result.outcome == "gone" else result.outcome, "reason": result.reason,
                    "meta_id": meta_id})
                self.assertEqual(self.claim_public(comment_id), store.PublicClaim("skip"))

    def test_an_answer_is_recorded_once_and_only_for_a_public_claim(self):
        self.reply(PUBLIC_OK)
        self.add("c2", user="u2")
        self.tick(LONG)
        dm = self.claim("c2")
        before = self.everything()
        for attempt_id in (1, 2, dm.attempt_id, 99, None):
            with self.subTest(attempt_id=attempt_id):
                with self.assertRaises(ValueError):
                    store.record_public(self.conn, attempt_id, PUBLIC_OK)
        self.assertEqual(self.everything(), before)

    def test_journal_and_comment_change_together_or_not_at_all(self):
        self.sent("c1")
        claim = self.claim_public("c1")
        self.force("c1", "done")                                    # e.g. settled by hand meanwhile
        before = self.everything()
        with self.assertRaises(RuntimeError):
            store.record_public(self.conn, claim.attempt_id, PUBLIC_OK)
        self.assertEqual(self.everything(), before)
        self.assertFalse(self.conn.in_transaction)


# ---------- metrics, outbox, problems ----------

class Tables(Db):
    def rows(self, table):
        return [dict(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]

    def test_metrics(self):
        store.add_metric(self.conn, 1500, 42)
        self.assertEqual(self.rows("metrics"), [{"ts": clock.iso(START), "followers": 1500, "media_count": 42}])
        self.assertEqual(store.kv_get(self.conn, "metric_at"), clock.iso(START))
        store.add_metric(self.conn, 1501, 42)                       # the same millisecond again: no error, one row
        self.tick(HOUR)
        store.add_metric(self.conn, 1510, 43)
        self.assertEqual([row["followers"] for row in self.rows("metrics")], [1501, 1510])
        self.assertEqual(store.kv_get(self.conn, "metric_at"), clock.iso(self.now))

    def test_outbox(self):
        first = store.outbox_add(self.conn, "armed", "armed: 5")
        second = store.outbox_add(self.conn, "waiting", "⚠ waiting: 3, oldest @ka\udc00te")
        queued = clock.iso()
        self.assertEqual(self.rows("outbox"), [
            {"id": first, "key": "armed", "text": "armed: 5", "created_at": queued, "sent_at": None},
            {"id": second, "key": "waiting", "text": "⚠ waiting: 3, oldest @ka\ufffdte", "created_at": queued, "sent_at": None}])
        self.tick(30)
        store.outbox_sent(self.conn, [first])
        sent = clock.iso()
        self.assertEqual([row["sent_at"] for row in self.rows("outbox")], [sent, None])
        self.tick(30)
        store.outbox_sent(self.conn, [first, second, 99])
        self.assertEqual([row["sent_at"] for row in self.rows("outbox")], [sent, clock.iso()])
        store.outbox_sent(self.conn, [])
        self.assertFalse(self.conn.in_transaction)

    def test_problems(self):
        store.problem_announce(self.conn, "waiting", "3 people wait")
        store.problem_announce(self.conn, "token", "expires in 10 days")
        since = clock.iso()
        self.assertEqual(self.rows("problems"), [
            {"key": "token", "text": "expires in 10 days", "since": since, "last_announced_at": since},
            {"key": "waiting", "text": "3 people wait", "since": since, "last_announced_at": since}])
        self.tick(2 * HOUR)
        store.problem_announce(self.conn, "waiting", "5 people wait")
        self.assertEqual(self.rows("problems")[1], {"key": "waiting", "text": "5 people wait", "since": since,
                                                    "last_announced_at": clock.iso()})
        self.assertEqual(self.rows("problems")[0]["last_announced_at"], since)
        self.assertTrue(store.problem_clear(self.conn, "waiting"))
        self.assertFalse(store.problem_clear(self.conn, "waiting"))
        self.assertEqual([row["key"] for row in self.rows("problems")], ["token"])


if __name__ == "__main__":
    unittest.main()
