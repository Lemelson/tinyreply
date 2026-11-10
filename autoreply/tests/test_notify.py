"""SPEC section 9, alerts: the outbox, problems that are announced, repeated and cleared, events, delivery to
Telegram and its health, and the check `arm` makes before it opens sending (8.6).

Telegram is a tiny HTTP server on 127.0.0.1; the database is a real SQLite file; the clock is the test's.
"""
import contextlib
import http.server
import io
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import clock
import config
import notify
import report
import store

try:
    from sim.fake_meta import FakeMeta
except ImportError:   # the simulator belongs to another builder
    FakeMeta = None

START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
MIN = 60
TOKEN = "123456:SECRET-bot_token"
CHAT = "-1001234"
MAX_UNITS = 4096

REEL = config.Reel(name="arena", media_id="111", permalink="", active=True, keywords=("арена",), also_match=(),
                   max_words=5, link="https://example.com/skill", text="{greeting} here it is")
CFG = config.Config(api_version="v26.0", sending=True, poll_interval_sec=90, max_sends_per_hour=60,
                    public_reply=True, profile_url="https://www.instagram.com/someone/", greeting="Hi!",
                    late_greeting="Hi, late!", late_after_minutes=60, public_phrases=("Sent", "In your inbox"),
                    reels=(REEL,), alerts=config.Alerts(repeat_min=120, digest_hour_utc=6))

OK = (200, {"ok": True, "result": {"message_id": 7}})
# every answer that is not Telegram's ok:true: (name, HTTP status, body)
NOT_DELIVERED = [
    ("chat not found", 400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}),
    ("unauthorized", 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
    ("200 with ok false", 200, {"ok": False}),
    ("200 with ok as a string", 200, {"ok": "true"}),
    ("200 with ok 1", 200, {"ok": 1}),
    ("200 without ok", 200, {"result": {"message_id": 7}}),
    ("200 not JSON", 200, b"<html>gateway</html>"),
    ("200 not an object", 200, [True]),
    ("200 empty body", 200, b""),
    ("502", 502, b"Bad Gateway"),
    ("500 that says ok", 500, {"ok": True}),
]


def units(text):
    return len(text.encode("utf-16-le")) // 2


class Telegram:
    """Plays api.telegram.org: records every request and answers what `answer` says."""

    def __init__(self):
        self.requests = []          # every request received: {"path", "type", "body"}
        self.answer = OK            # (status, body); None = the connection is closed without an answer
        self.during = None          # called while a request is being served
        self.release = None         # an Event: the answer waits for it (a Telegram that hangs)
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append({"path": self.path, "type": self.headers.get("Content-Type"),
                                      "body": json.loads(raw)})
                if fake.during:
                    fake.during()
                if fake.release:
                    fake.release.wait(20)
                if fake.answer is None:
                    self.close_connection = True
                    return
                status, body = fake.answer
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass   # the client gave up waiting

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def texts(self):
        return [request["body"]["text"] for request in self.requests]


def closed_port():
    """A base URL on 127.0.0.1 where nothing listens."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "core.db")
        self.now = START
        patcher = mock.patch.object(clock, "now", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = store.connect(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.tg = Telegram()
        self.addCleanup(self.tg.stop)
        self.creds = (TOKEN, CHAT)
        self.log = io.StringIO()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.notifier = notify.Notifier(self.conn, lambda: self.creds, self.tg.base)

    def tick(self, minutes):
        self.now += timedelta(minutes=minutes)

    def cycle(self, problems):
        """What the engine does at the end of every cycle. Returns the texts Telegram received in it."""
        before = len(self.tg.requests)
        self.notifier.sync(problems, self.now)
        self.notifier.flush()
        return self.tg.texts[before:]

    def queued(self):
        return [row[0] for row in self.conn.execute("SELECT text FROM outbox WHERE sent_at IS NULL ORDER BY id")]

    def outbox(self):
        return [tuple(row) for row in self.conn.execute("SELECT key, text, sent_at FROM outbox ORDER BY id")]

    def kv(self, key):
        return store.kv_get(self.conn, key)


class Problems(Case):
    def test_a_new_problem_is_one_message(self):
        self.assertEqual(self.cycle([("waiting", "5 чел. ждут")]), ["⚠ waiting: 5 чел. ждут"])
        self.assertEqual(self.cycle([("waiting", "5 чел. ждут")]), [])
        row = self.conn.execute("SELECT key, text, since, last_announced_at FROM problems").fetchall()
        self.assertEqual([tuple(r) for r in row], [("waiting", "5 чел. ждут", clock.iso(START), clock.iso(START))])

    def test_no_problem_no_message(self):
        self.assertEqual(self.cycle([]), [])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.tg.requests, [])

    def test_a_lasting_problem_is_repeated_only_after_repeat_min(self):
        self.cycle([("waiting", "5 чел. ждут")])
        self.tick(119)
        self.assertEqual(self.cycle([("waiting", "9 чел. ждут")]), [])
        self.tick(1)
        self.assertEqual(self.cycle([("waiting", "9 чел. ждут")]), ["⚠ waiting: 9 чел. ждут"])
        self.tick(119)
        self.assertEqual(self.cycle([("waiting", "12 чел. ждут")]), [])
        self.tick(1)
        self.assertEqual(self.cycle([("waiting", "12 чел. ждут")]), ["⚠ waiting: 12 чел. ждут"])
        since, last = self.conn.execute("SELECT since, last_announced_at FROM problems").fetchone()
        self.assertEqual((since, last), (clock.iso(START), clock.iso(START + timedelta(minutes=240))))

    def test_repeat_min_comes_from_the_config(self):
        fast = notify.Notifier(self.conn, lambda: self.creds, self.tg.base, repeat_min=30)
        fast.sync([("waiting", "a")], self.now)
        self.tick(29)
        fast.sync([("waiting", "a")], self.now)
        self.assertEqual(len(self.queued()), 1)
        self.tick(1)
        fast.sync([("waiting", "a")], self.now)
        self.assertEqual(len(self.queued()), 2)

    def test_each_problem_has_its_own_repeat_clock(self):
        self.cycle([("waiting", "a")])
        self.tick(60)
        self.assertEqual(self.cycle([("waiting", "a"), ("token", "b")]), ["⚠ token: b"])
        self.tick(60)
        self.assertEqual(self.cycle([("waiting", "a"), ("token", "b")]), ["⚠ waiting: a"])
        self.tick(60)
        self.assertEqual(self.cycle([("waiting", "a"), ("token", "b")]), ["⚠ token: b"])

    def test_a_problem_that_ended_is_one_cleared_message(self):
        self.cycle([("waiting", "a"), ("token", "b")])
        self.assertEqual(self.cycle([("token", "b")]), ["✓ cleared: waiting"])
        self.assertEqual(self.cycle([("token", "b")]), [])
        self.assertEqual([row[0] for row in self.conn.execute("SELECT key FROM problems")], ["token"])
        self.assertEqual(self.cycle([]), ["✓ cleared: token"])
        self.assertEqual(self.cycle([]), [])

    def test_a_problem_that_comes_back_is_new_again(self):
        self.cycle([("waiting", "a")])
        self.cycle([])
        self.tick(1)
        self.assertEqual(self.cycle([("waiting", "a")]), ["⚠ waiting: a"])
        self.assertEqual(self.conn.execute("SELECT since FROM problems").fetchone()[0],
                         clock.iso(START + timedelta(minutes=1)))

    def test_a_key_given_twice_is_announced_once(self):
        self.assertEqual(self.cycle([("clock", "from the report"), ("clock", "from the engine")]),
                         ["⚠ clock: from the report"])

    def test_announcement_and_queued_line_are_one_transaction(self):
        with mock.patch.object(store, "outbox_add", side_effect=sqlite3.OperationalError("disk is full")):
            self.notifier.sync([("waiting", "a")], self.now)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.cycle([("waiting", "a")]), ["⚠ waiting: a"])   # not lost: announced the next time

    def test_clearing_and_its_line_are_one_transaction(self):
        self.cycle([("waiting", "a")])
        with mock.patch.object(store, "outbox_add", side_effect=sqlite3.OperationalError("disk is full")):
            self.notifier.sync([], self.now)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0], 1)
        self.assertEqual(self.cycle([]), ["✓ cleared: waiting"])


class Events(Case):
    def test_an_event_is_queued_once(self):
        self.notifier.event("started", "бот запущен")
        self.assertEqual(self.outbox(), [("started", "ℹ started: бот запущен", None)])
        self.assertEqual(self.cycle([]), ["ℹ started: бот запущен"])
        self.assertEqual(self.cycle([]), [])
        self.tick(500)
        self.assertEqual(self.cycle([]), [])

    def test_the_same_event_twice_is_two_lines(self):
        self.notifier.event("expired", "2 человека потеряны")
        self.notifier.event("expired", "3 человека потеряны")
        self.assertEqual(self.cycle([]), ["ℹ expired: 2 человека потеряны\nℹ expired: 3 человека потеряны"])

    def test_an_event_without_text(self):
        self.notifier.event("disarmed", "")
        self.assertEqual(self.queued(), ["ℹ disarmed"])

    def test_an_event_is_not_a_problem(self):
        self.notifier.event("started", "x")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0], 0)
        self.assertEqual(self.cycle([]), ["ℹ started: x"])   # and sync does not "clear" it
        self.assertEqual(self.cycle([]), [])


class Delivery(Case):
    def test_everything_queued_goes_out_as_one_send_message(self):
        self.cycle([("waiting", "a")])
        self.notifier.event("armed", "бюджет 50")
        self.notifier.event("stage_done", "ok 50")
        texts = self.cycle([("token", "b")])
        self.assertEqual(texts, ["ℹ armed: бюджет 50\nℹ stage_done: ok 50\n⚠ token: b\n✓ cleared: waiting"])
        self.assertEqual(len(self.tg.requests), 2)

    def test_the_request_is_telegrams_send_message(self):
        self.notifier.event("started", "x")
        self.assertIs(self.notifier.flush(), True)
        self.assertEqual(self.tg.requests, [{"path": f"/bot{TOKEN}/sendMessage", "type": "application/json",
                                             "body": {"chat_id": CHAT, "text": "ℹ started: x"}}])

    def test_nothing_queued_nothing_sent(self):
        self.assertIs(self.notifier.flush(), True)
        self.assertEqual(self.tg.requests, [])
        self.assertIsNone(self.kv("tg_ok_at"))
        self.assertIsNone(self.kv("tg_failed_at"))

    def test_sent_at_is_set_when_telegram_says_ok(self):
        self.notifier.event("started", "x")
        self.tick(3)
        self.notifier.flush()
        self.assertEqual(self.outbox(), [("started", "ℹ started: x", clock.iso(self.now))])

    def test_an_answer_that_is_not_ok_true_keeps_the_lines_queued(self):
        for name, status, body in NOT_DELIVERED:
            with self.subTest(name):
                self.conn.execute("DELETE FROM outbox")
                del self.tg.requests[:]
                self.tg.answer = (status, body)
                self.notifier.event("uncertain", name)
                self.assertIs(self.notifier.flush(), False)
                self.assertEqual(len(self.tg.requests), 1)
                self.assertEqual(self.queued(), [f"ℹ uncertain: {name}"])
                self.assertIs(self.notifier.flush(), False)   # retried the next cycle, still queued
                self.assertEqual(self.queued(), [f"ℹ uncertain: {name}"])

                self.tg.answer = OK
                del self.tg.requests[:]
                self.assertIs(self.notifier.flush(), True)
                self.assertIs(self.notifier.flush(), True)
                self.assertEqual(self.tg.texts, [f"ℹ uncertain: {name}"])   # delivered exactly once
                self.assertEqual(self.queued(), [])

    def test_telegram_down_then_up_delivers_everything_exactly_once(self):
        down = notify.Notifier(self.conn, lambda: self.creds, closed_port())
        down.sync([("hold:account", "a")], self.now)
        self.assertIs(down.flush(), False)
        down.event("expired", "2")
        self.assertIs(down.flush(), False)
        self.assertEqual(self.queued(), ["⚠ hold:account: a", "ℹ expired: 2"])
        self.assertEqual(self.tg.requests, [])

        self.assertEqual(self.cycle([("hold:account", "a")]), ["⚠ hold:account: a\nℹ expired: 2"])
        self.assertEqual(self.cycle([("hold:account", "a")]), [])
        self.assertEqual(self.queued(), [])

    def test_a_connection_closed_without_an_answer_keeps_the_lines_queued(self):
        self.tg.answer = None
        self.notifier.event("started", "x")
        self.assertIs(self.notifier.flush(), False)
        self.assertEqual(len(self.tg.requests), 1)
        self.assertEqual(self.queued(), ["ℹ started: x"])
        self.assertEqual(self.kv("tg_failed_at"), clock.iso(START))
        self.tg.answer = OK
        self.assertIs(self.notifier.flush(), True)
        self.assertEqual(self.tg.texts, ["ℹ started: x", "ℹ started: x"])   # the one that got no answer, then the retry
        self.assertEqual(self.queued(), [])

    def test_a_telegram_that_hangs_costs_the_timeout_not_more(self):
        self.tg.release = threading.Event()
        self.addCleanup(self.tg.release.set)
        self.notifier.event("started", "x")
        with mock.patch.dict(clock.T, {"tg_timeout": 0.5}):
            began = time.monotonic()
            self.assertIs(self.notifier.flush(), False)
            took = time.monotonic() - began
        self.assertGreaterEqual(took, 0.4)
        self.assertLess(took, 5)
        self.assertEqual(self.queued(), ["ℹ started: x"])
        self.assertIn("telegram_failed", self.log.getvalue())

    def test_the_timeout_is_ten_real_seconds(self):
        self.assertEqual(clock.T["tg_timeout"], 10)
        seen = {}

        def opened(request):
            seen["left"] = request.deadline - time.monotonic()
            raise OSError("stop here")

        with mock.patch.object(notify.meta._opener, "open", side_effect=opened):
            self.assertIsNotNone(notify.send_message(self.tg.base, TOKEN, CHAT, "x"))
        self.assertGreater(seen["left"], 9.5)
        self.assertLessEqual(seen["left"], 10)

    def test_no_transaction_is_open_while_telegram_is_asked(self):
        seen = []

        def another_process():
            other = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('send_budget', '0')")   # `disarm`
                other.execute("COMMIT")
                seen.append("written")
            except sqlite3.Error as e:
                seen.append(e)
            finally:
                other.close()

        self.tg.during = another_process
        self.notifier.sync([("waiting", "a")], self.now)
        self.assertIs(self.notifier.flush(), True)
        self.assertEqual(seen, ["written"])
        self.assertFalse(self.conn.in_transaction)

    def test_a_backlog_longer_than_one_message_goes_out_over_several_cycles(self):
        lines = [f"line {number} " + "я" * 300 for number in range(40)]
        for line in lines:
            self.notifier.event("digest", line)
        rounds = 0
        while self.queued():
            rounds += 1
            before = len(self.tg.requests)
            self.assertIs(self.notifier.flush(), True)
            self.assertEqual(len(self.tg.requests), before + 1)   # ONE sendMessage per flush
            self.assertLess(rounds, 10)
        self.assertGreater(rounds, 1)
        for text in self.tg.texts:
            self.assertLessEqual(units(text), MAX_UNITS)
        delivered = "\n".join(self.tg.texts).split("\n")
        self.assertEqual(delivered, [f"ℹ digest: {line}" for line in lines])   # each once, in order
        self.assertGreater(units(self.tg.texts[0]), MAX_UNITS - 400)           # and as much as fits

    def test_a_message_is_filled_to_the_limit_exactly(self):
        for size, expected in ((MAX_UNITS, 1), (MAX_UNITS + 1, 2)):
            with self.subTest(size):
                self.conn.execute("DELETE FROM outbox")
                del self.tg.requests[:]
                first = "a" * 2000
                store.outbox_add(self.conn, "k", first)
                store.outbox_add(self.conn, "k", "b" * (size - len(first) - 1))   # the two and the newline
                while self.queued():
                    self.notifier.flush()
                self.assertEqual(len(self.tg.requests), expected)

    def test_one_line_longer_than_a_message_is_cut_not_stuck(self):
        self.notifier.event("digest", "😀" * 3000)     # 6000 UTF-16 units
        self.notifier.event("started", "after it")
        self.assertIs(self.notifier.flush(), True)
        self.assertIs(self.notifier.flush(), True)
        first, second = self.tg.texts
        self.assertLessEqual(units(first), MAX_UNITS)
        self.assertGreater(units(first), MAX_UNITS - 3)
        self.assertTrue(first.startswith("ℹ digest: 😀😀") and first.endswith("😀…"))
        first.encode("utf-8")                           # no half of an emoji at the cut
        self.assertEqual(second, "ℹ started: after it")
        self.assertEqual(self.queued(), [])

    def test_a_line_is_cut_only_when_it_is_too_long_and_then_to_the_limit(self):
        store.outbox_add(self.conn, "k", "я" * MAX_UNITS)
        self.notifier.flush()
        store.outbox_add(self.conn, "k", "я" * (MAX_UNITS + 1))
        self.notifier.flush()
        self.assertEqual(self.tg.texts, ["я" * MAX_UNITS, "я" * (MAX_UNITS - 1) + "…"])

    def test_an_empty_line_cannot_block_the_queue(self):
        store.outbox_add(self.conn, "k", "")
        self.assertIs(self.notifier.flush(), True)
        self.assertEqual(self.tg.texts, ["-"])

    def test_a_base_url_with_a_trailing_slash(self):
        notifier = notify.Notifier(self.conn, lambda: self.creds, self.tg.base + "/")
        notifier.event("started", "x")
        self.assertIs(notifier.flush(), True)
        self.assertEqual(self.tg.requests[0]["path"], f"/bot{TOKEN}/sendMessage")

    @unittest.skipUnless(FakeMeta, "sim/fake_meta.py is not there")
    def test_the_strict_fake_of_the_simulator_takes_the_message(self):
        fake = FakeMeta({"account": {"user_id": "1", "id": "2", "username": "me", "followers_count": 1,
                                     "media_count": 0},
                         "token": "T", "media": [], "telegram": {"token": TOKEN, "chat_id": CHAT}}).start()
        self.addCleanup(fake.stop)
        notifier = notify.Notifier(self.conn, lambda: self.creds, fake.api_base)
        notifier.sync([("waiting", "5 чел. ждут")], self.now)
        self.assertIs(notifier.flush(), True)
        self.assertEqual([m["text"] for m in fake.control("/truth")["telegram"]], ["⚠ waiting: 5 чел. ждут"])

        self.creds = ("999:other", CHAT)
        notifier.event("started", "x")
        self.assertIs(notifier.flush(), False)
        self.assertEqual(len(fake.control("/truth")["telegram"]), 1)
        self.assertEqual(self.queued(), ["ℹ started: x"])


class Health(Case):
    def test_a_confirmed_delivery_is_recorded(self):
        self.notifier.event("started", "x")
        self.tick(1)
        self.notifier.flush()
        self.assertEqual(self.kv("tg_ok_at"), clock.iso(self.now))
        self.assertIsNone(self.kv("tg_failed_at"))
        self.assertEqual(notify.health(self.conn), {"configured": True, "ok_at": clock.iso(self.now),
                                                    "failed_at": None, "failing": False, "queued": 0})

    def test_a_failed_delivery_is_recorded_and_a_later_success_ends_it(self):
        self.notifier.event("started", "x")
        self.notifier.flush()
        self.tg.answer = (401, {"ok": False, "description": "Unauthorized"})
        self.tick(5)
        self.notifier.event("expired", "2")
        self.notifier.flush()
        failed_at = clock.iso(self.now)
        self.assertEqual(notify.health(self.conn), {"configured": True, "ok_at": clock.iso(START),
                                                    "failed_at": failed_at, "failing": True, "queued": 1})
        self.assertIn("no_alert_channel", dict(report.problems(self.conn, CFG, self.now)))

        self.tg.answer = OK
        self.tick(5)
        self.notifier.flush()
        self.assertEqual(notify.health(self.conn), {"configured": True, "ok_at": clock.iso(self.now),
                                                    "failed_at": failed_at, "failing": False, "queued": 0})
        self.assertNotIn("no_alert_channel", dict(report.problems(self.conn, CFG, self.now)))

    def test_a_first_delivery_that_fails_is_failing(self):
        self.tg.answer = (502, b"")
        self.notifier.event("started", "x")
        self.notifier.flush()
        health = notify.health(self.conn)
        self.assertEqual((health["ok_at"], health["failed_at"], health["failing"]), (None, clock.iso(START), True))

    def test_health_of_a_database_no_notifier_touched(self):
        self.assertEqual(notify.health(self.conn), {"configured": None, "ok_at": None, "failed_at": None,
                                                    "failing": False, "queued": 0})

    def test_health_is_readable_on_a_read_only_connection(self):
        self.notifier.event("started", "x")
        readonly = store.connect(self.path, readonly=True)
        self.addCleanup(readonly.close)
        self.assertEqual(notify.health(readonly)["queued"], 1)


class NotConfigured(Case):
    def test_nothing_is_sent_and_the_lines_are_logged_and_stay_queued(self):
        for creds in ((None, None), (TOKEN, None), (None, CHAT), ("", ""), (TOKEN, "")):
            with self.subTest(creds):
                self.conn.execute("DELETE FROM outbox")
                self.conn.execute("DELETE FROM problems")
                self.creds = creds
                self.assertEqual(self.cycle([("waiting", "5 чел. ждут")]), [])
                self.notifier.event("expired", "2 человека потеряны")
                self.assertIs(self.notifier.flush(), False)
                self.assertEqual(self.tg.requests, [])
                self.assertEqual(self.queued(), ["⚠ waiting: 5 чел. ждут", "ℹ expired: 2 человека потеряны"])
                self.assertIn(" ALERT waiting 5 чел. ждут\n", self.log.getvalue())
                self.assertIn(" ALERT expired 2 человека потеряны\n", self.log.getvalue())
                self.assertEqual(self.kv("tg_configured"), "0")
                self.assertIsNone(self.kv("tg_ok_at"))
                self.assertIsNone(self.kv("tg_failed_at"))

    def test_the_report_names_the_problem(self):
        self.assertNotIn("no_alert_channel", dict(report.problems(self.conn, CFG, self.now)))   # not known yet
        self.creds = (None, None)
        self.notifier.flush()
        self.assertIn("no_alert_channel", dict(report.problems(self.conn, CFG, self.now)))
        self.creds = (TOKEN, CHAT)
        self.notifier.flush()
        self.assertEqual(self.kv("tg_configured"), "1")
        self.assertNotIn("no_alert_channel", dict(report.problems(self.conn, CFG, self.now)))

    def test_once_configured_the_queued_lines_go_out(self):
        self.creds = (None, None)
        self.cycle([("waiting", "a")])
        self.creds = (TOKEN, CHAT)
        self.assertEqual(self.cycle([("waiting", "a")]), ["⚠ waiting: a"])
        self.assertEqual(self.queued(), [])

    def test_nothing_queued_and_not_configured_is_still_recorded(self):
        self.creds = (None, None)
        self.assertIs(self.notifier.flush(), True)
        self.assertEqual(self.kv("tg_configured"), "0")


class Log(Case):
    def test_every_queued_line_is_logged_once_as_an_alert(self):
        self.cycle([("waiting", "5 чел. ждут")])
        self.notifier.event("armed", "бюджет 50")
        self.cycle([])
        self.cycle([])
        lines = self.log.getvalue().splitlines()
        self.assertEqual(lines, [f"{clock.iso(START)} ALERT waiting 5 чел. ждут",
                                 f"{clock.iso(START)} ALERT armed бюджет 50",
                                 f"{clock.iso(START)} ALERT waiting cleared"])

    def test_a_text_of_several_lines_is_one_log_line(self):
        self.notifier.event("digest", "первая\nвторая\r\nтретья")
        self.assertEqual(self.log.getvalue(), f"{clock.iso(START)} ALERT digest первая | вторая | третья\n")
        self.assertEqual(self.queued(), ["ℹ digest: первая\nвторая\r\nтретья"])

    def test_the_bot_token_never_reaches_the_log_or_a_return_value(self):
        said = []
        for name, status, body in NOT_DELIVERED:
            self.tg.answer = (status, body)
            self.notifier.event("uncertain", name)
            self.notifier.flush()
            said.append(notify.send_message(self.tg.base, TOKEN, CHAT, "x"))
        self.tg.answer = OK
        self.notifier.flush()
        said.append(notify.send_message(closed_port(), TOKEN, CHAT, "x"))
        said.append(notify.send_message("http://127.0.0.1:9/a b", TOKEN, CHAT, "x"))     # urllib quotes the path
        said.append(notify.send_message("http://127.0.0.1:notaport", TOKEN, CHAT, "x"))
        said.append(notify.send_message("ftp://127.0.0.1", TOKEN, CHAT, "x"))
        notify.Notifier(self.conn, lambda: self.creds, closed_port()).flush()
        self.assertTrue(all(isinstance(reason, str) and reason for reason in said), said)
        self.assertNotIn("/bot", " ".join(said))              # transport errors never echo the URL path
        everything = self.log.getvalue() + " ".join(said)
        self.assertIn("telegram_failed", everything)
        self.assertNotIn(TOKEN, everything)
        self.assertNotIn("SECRET", everything)

    def test_a_token_with_odd_characters_is_hidden_in_every_form(self):
        odd = "12 3:se/cr?et"
        reason = notify.send_message("http://127.0.0.1:9/a b", odd, CHAT, "x")
        for form in (odd, "12%203:se%2Fcr%3Fet", "12%203%3Ase%2Fcr%3Fet", "se/cr", "se%2Fcr"):
            self.assertNotIn(form, reason)
        self.assertTrue(reason.startswith("InvalidURL: "), reason)  # useful class, without a raw URL

    def test_the_token_is_hidden_in_a_traceback_too(self):
        self.notifier.event("started", "x")
        self.notifier.flush()

        def broken():
            raise RuntimeError(f"cannot parse the line TG_BOT_TOKEN={TOKEN}")

        self.notifier._get_tg = broken
        self.assertIs(self.notifier.flush(), False)
        self.assertIn("notify_failed", self.log.getvalue())
        self.assertIn("RuntimeError", self.log.getvalue())
        self.assertNotIn("SECRET", self.log.getvalue())

    def test_a_long_reason_is_cut(self):
        self.tg.answer = (400, {"ok": False, "description": "x" * 5000})
        self.assertEqual(notify.send_message(self.tg.base, TOKEN, CHAT, "x"), "http 400: " + "x" * 290)

    def test_no_token_is_not_a_secret_to_hide(self):
        self.creds = (None, None)
        self.notifier.flush()
        self.notifier._get_tg = lambda: [][0]
        self.notifier.flush()
        self.assertIn("IndexError", self.log.getvalue())
        self.assertNotIn("<bot-token>", self.log.getvalue())

    def test_the_reason_of_a_failure_is_logged(self):
        self.tg.answer = (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
        self.notifier.event("started", "x")
        self.notifier.flush()
        self.assertIn(" WARN telegram_failed queued=1 error=http 400: Bad Request: chat not found\n", self.log.getvalue())


class NeverRaises(Case):
    def test_a_get_tg_that_raises(self):
        def broken():
            raise OSError(".env is gone")

        notifier = notify.Notifier(self.conn, broken, self.tg.base)
        notifier.event("started", "x")
        self.assertIs(notifier.flush(), False)
        self.assertEqual(self.queued(), ["ℹ started: x"])
        self.assertIn("notify_failed", self.log.getvalue())

    def test_a_dead_database(self):
        dead = store.connect(self.path)
        dead.close()
        notifier = notify.Notifier(dead, lambda: self.creds, self.tg.base)
        notifier.sync([("waiting", "a")], self.now)
        notifier.event("started", "x")
        self.assertIs(notifier.flush(), False)
        self.assertEqual(self.tg.requests, [])
        self.assertEqual(self.log.getvalue().count("notify_failed"), 3)

    def test_a_read_only_database(self):
        readonly = store.connect(self.path, readonly=True)
        self.addCleanup(readonly.close)
        notifier = notify.Notifier(readonly, lambda: self.creds, self.tg.base)
        notifier.sync([("waiting", "a")], self.now)
        notifier.event("started", "x")
        notifier.flush()
        self.assertFalse(readonly.in_transaction)
        self.assertEqual(self.outbox(), [])

    def test_garbage_instead_of_problems(self):
        for garbage in (None, [("only-a-key",)], "text", [None], 5):
            with self.subTest(garbage):
                self.notifier.sync(garbage, self.now)
                self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.outbox(), [])

    def test_send_message_never_raises(self):
        for base in (closed_port(), "", "http://", "not a url", "http://127.0.0.1:99999", None):
            with self.subTest(base):
                self.assertIsInstance(notify.send_message(base, TOKEN, CHAT, "x"), str)
        self.assertIsInstance(notify.send_message(self.tg.base, TOKEN, CHAT, "\ud83d"), (str, type(None)))
        self.assertIsNone(notify.send_message(self.tg.base, 12345, -100, "x"))    # numbers instead of texts
        self.assertEqual(self.tg.requests[-1]["path"], "/bot12345/sendMessage")
        self.assertIsInstance(notify.send_message(closed_port(), 12345, -100, "x"), str)

    def test_send_message_without_a_token_or_a_chat_asks_nobody(self):
        for creds in ((None, CHAT), (TOKEN, None), ("", CHAT), (TOKEN, ""), (None, None)):
            with self.subTest(creds):
                self.assertEqual(notify.send_message(self.tg.base, *creds, "x"), "no bot token or no chat id")
        self.assertEqual(self.tg.requests, [])

    def test_send_message_says_what_went_wrong(self):
        expected = {"chat not found": "http 400: Bad Request: chat not found", "unauthorized": "http 401: Unauthorized",
                    "200 not JSON": "http 200: no ok:true in the answer", "502": "http 502: no ok:true in the answer",
                    "200 with ok false": "http 200: no ok:true in the answer",
                    "200 not an object": "http 200: no ok:true in the answer"}
        for name, status, body in NOT_DELIVERED:
            if name in expected:
                self.tg.answer = (status, body)
                self.assertEqual(notify.send_message(self.tg.base, TOKEN, CHAT, "x"), expected.pop(name))
        self.assertEqual(expected, {})
        self.assertRegex(notify.send_message(closed_port(), TOKEN, CHAT, "x"), "^ConnectionRefusedError: ")
        self.tg.answer = None
        self.assertRegex(notify.send_message(self.tg.base, TOKEN, CHAT, "x"), "^RemoteDisconnected: ")


class Deaf(Case):
    """8.6: `arm` refuses when an alert would reach nobody."""

    def test_not_configured(self):
        for creds in ((None, None), (TOKEN, None), (None, CHAT), ("", "")):
            with self.subTest(creds):
                self.assertIn("не настроен", notify.deaf(self.conn, lambda: creds))

    def test_configured_and_nothing_failed(self):
        self.assertIsNone(notify.deaf(self.conn, lambda: self.creds))
        self.notifier.event("started", "x")
        self.notifier.flush()
        self.assertIsNone(notify.deaf(self.conn, lambda: self.creds))

    def test_the_last_delivery_failed(self):
        self.notifier.event("started", "x")
        self.notifier.flush()
        self.tg.answer = (401, {"ok": False, "description": "Unauthorized"})
        self.tick(1)
        self.notifier.event("expired", "2")
        self.notifier.flush()
        reason = notify.deaf(self.conn, lambda: self.creds)
        self.assertIn("не удалась", reason)
        self.assertIn(clock.iso(self.now), reason)
        self.assertNotIn("SECRET", reason)

        self.tg.answer = OK
        self.tick(1)
        self.notifier.flush()
        self.assertIsNone(notify.deaf(self.conn, lambda: self.creds))

    def test_it_reads_only(self):
        readonly = store.connect(self.path, readonly=True)
        self.addCleanup(readonly.close)
        self.assertIsNone(notify.deaf(readonly, lambda: self.creds))


if __name__ == "__main__":
    unittest.main()
