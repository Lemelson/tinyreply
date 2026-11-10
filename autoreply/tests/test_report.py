"""SPEC section 9, the report: every problem key, every term of `unaccounted`, the verdict, the report data.

Every test starts from a bot that runs with every gate open and nothing wrong (a real SQLite file in a
temporary directory, filled through store.py, the clock in the test's hands), changes ONE thing and looks
at what the report says. So each problem is seen to fire, and seen not to fire in the nearest state where it
must not.
"""
import contextlib
import copy
import dataclasses
import fcntl
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import clock
import config
import meta
import notify
import report
import store

START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
MIN, HOUR, DAY = 60, 3600, 86400
WINDOW = 7 * DAY - 10 * MIN     # 7.2 step 1: Meta's 7 days less the margin
IG_TOKEN = "IGAAsecretTOKENvalue"

REEL = config.Reel(name="arena", media_id="111", permalink="", active=True, keywords=("арена", "arena"),
                   also_match=(), max_words=5, link="https://example.com/skill", text="{greeting} here it is")
SECOND = dataclasses.replace(REEL, name="second", media_id="222")
CFG = config.Config(api_version="v26.0", sending=True, poll_interval_sec=90, max_sends_per_hour=60,
                    public_reply=True, profile_url="https://www.instagram.com/someone/", greeting="Hi!",
                    late_greeting="Hi, late!", late_after_minutes=60, public_phrases=("Sent", "In your inbox"),
                    reels=(REEL,), alerts=config.Alerts(repeat_min=120, digest_hour_utc=6))
ME = {"user_id": "9000", "id": "9001", "username": "owner"}


def answer(outcome, reason="", http=400, data=None):
    return meta.Result(ok=outcome == "ok", http=http, data=data or {}, outcome=outcome, reason=reason, code=None,
                       subcode=None, message="", connect_failed=outcome == "not_sent")


DM_OK = answer("ok", http=200, data={"message_id": "mid.1"})
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
        self.conn = store.connect(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.cfg = CFG

        # the bot runs, every gate is open, nothing is wrong
        store.kv_set_many(self.conn, {"me_user_id": ME["user_id"], "me_id": ME["id"], "me_username": ME["username"],
                                      "me_checked_at": clock.iso(), "tg_configured": 1})
        store.arm(self.conn, "unlimited")
        self.read("111")
        self.ran()
        self.lock = open(os.path.join(self.dir, "core.lock"), "w")
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX)

    # ----- time -----

    def tick(self, seconds):
        self.now += timedelta(seconds=seconds)

    def ago(self, seconds):
        return clock.iso(self.now - timedelta(seconds=seconds))

    @contextlib.contextmanager
    def back(self, seconds):
        """Do something as it was done `seconds` ago."""
        self.now -= timedelta(seconds=seconds)
        try:
            yield
        finally:
            self.now += timedelta(seconds=seconds)

    # ----- what the engine writes -----

    def set(self, **values):
        store.kv_set_many(self.conn, values)

    def read(self, media="111", state="ok", fails=0, full_ago=0, head_ago=0):
        """Collect of a reel as the engine records it (8.3)."""
        self.set(**{f"reel_state:{media}": state, f"reel_fails:{media}": fails,
                    f"full_sweep_at:{media}": None if full_ago is None else self.ago(full_ago),
                    f"head_ok_at:{media}": None if head_ago is None else self.ago(head_ago)})

    def ran(self):
        self.set(last_cycle_ok_at=clock.iso(), last_now=clock.iso())

    def configure(self, **changes):
        self.cfg = dataclasses.replace(self.cfg, **changes)

    def add(self, comment_id, age=60, status="pending", user=None, media="111", text="арена", reason=""):
        """A comment written `age` seconds ago and first listed now. Statuses collect cannot give are forced."""
        new = status if status in ("seen", "pending", "expired", "already") else "pending"
        self.assertTrue(store.add_comment(self.conn, comment_id, media, user or "u-" + comment_id, "name-" + comment_id,
                                          text, self.ago(age), new, reason))
        if new != status:
            self.conn.execute("UPDATE comments SET status=?, reason=? WHERE comment_id=?", (status, reason, comment_id))

    def attempt(self, comment_id, outcome, ago=0, kind="dm", reason=""):
        """Set-up only: a journal row, started and answered `ago` seconds ago; outcome None = still open."""
        when = self.ago(ago)
        self.conn.execute("INSERT INTO attempts (comment_id, kind, started_at, finished_at, outcome, reason) "
                          "VALUES (?,?,?,?,?,?)", (comment_id, kind, when, when if outcome else None, outcome, reason))

    def deliver(self, comment_id, ago=0, **comment):
        """The real path: a comment whose DM Meta confirmed `ago` seconds ago; its public reply is owed."""
        with self.back(ago):
            self.add(comment_id, **comment)
            claim = store.claim_dm(self.conn, comment_id, self.cfg, ME)
            self.assertEqual(claim.kind, "claimed")
            self.assertEqual(store.record_dm(self.conn, claim.attempt_id, DM_OK), "sent")

    # ----- what the report says -----

    def problems(self):
        return dict(report.problems(self.conn, self.cfg, self.now))

    def keys(self):
        return [key for key, _ in report.problems(self.conn, self.cfg, self.now)]

    def build(self):
        return report.build(self.conn, self.cfg, self.now)

    def parts(self):
        return self.build()["unaccounted_parts"]

    def reel(self, media="111"):
        return next(entry for entry in self.build()["reels"] if entry["media_id"] == media)


class Words(unittest.TestCase):
    def test_durations(self):
        for seconds, words in ((-5, "меньше минуты"), (0, "меньше минуты"), (59.9, "меньше минуты"), (60, "1 мин"),
                               (119, "1 мин"), (3599, "59 мин"), (3600, "1 ч 0 мин"), (86399, "23 ч 59 мин"),
                               (86400, "1 дн 0 ч"), (90000, "1 дн 1 ч"), (6 * 86400 + 23 * 3600 + 3599, "6 дн 23 ч")):
            with self.subTest(seconds):
                self.assertEqual(report._span(seconds), words)

    def test_times(self):
        self.assertEqual(report._when("2026-10-05T07:08:09.123Z"), "05.10 07:08 UTC")
        self.assertEqual(report._when(None), "никогда")


class Baseline(Db):
    def test_a_healthy_bot_has_no_problems(self):
        self.assertEqual(report.problems(self.conn, self.cfg, self.now), [])
        report_ = self.build()
        self.assertEqual(report_["problems"], [])
        self.assertEqual(report_["unaccounted"], 0)
        self.assertEqual(report_["verdict"], "OK: никто не ждёт")

    def test_problems_are_key_text_pairs(self):
        store.arm(self.conn, 0)
        self.add("c1")
        found = report.problems(self.conn, self.cfg, self.now)
        self.assertEqual([type(problem) for problem in found], [tuple])
        key, text = found[0]
        self.assertEqual(key, "waiting")
        self.assertIsInstance(text, str)


class HoldAccount(Db):
    def test_every_reason_is_announced_with_since_and_next_probe(self):
        for reason in ("no_token", "no_identity", "auth", "blocked", "rate"):
            with self.subTest(reason):
                store.clear_account_hold(self.conn)
                hold = store.set_account_hold(self.conn, reason, "some detail")
                self.assertEqual(self.keys(), ["hold:account"])
                text = self.problems()["hold:account"]
                self.assertIn(f"({reason}, some detail)", text)
                self.assertIn("с 05.10 12:00 UTC", text)
                self.assertIn(f"Следующая проверка: 05.10 {hold['next_probe_at'][11:16]} UTC", text)
                self.assertNotIn("причина неизвестна", text)

    def test_no_hold_no_problem(self):
        store.set_account_hold(self.conn, "auth")
        store.clear_account_hold(self.conn)
        self.assertEqual(self.keys(), [])

    def test_an_unknown_reason_is_still_a_problem(self):
        store.set_account_hold(self.conn, "something_new")
        self.assertIn("причина неизвестна (something_new)", self.problems()["hold:account"])

    def test_a_long_detail_is_cut(self):
        store.set_account_hold(self.conn, "blocked", "x" * 500)
        self.assertIn("(blocked, " + "x" * 150 + ")", self.problems()["hold:account"])


class HoldSend(Db):
    def test_every_reason_is_announced_with_until(self):
        for reason in ("uncertain", "send_blocked", "spam", "streak"):
            with self.subTest(reason):
                self.conn.execute("DELETE FROM kv WHERE key='send_hold'")
                store.set_send_hold(self.conn, reason, 45 * MIN)
                self.assertEqual(self.keys(), ["hold:send"])
                text = self.problems()["hold:send"]
                self.assertIn("до 05.10 12:45 UTC (ещё 45 мин)", text)
                self.assertIn(f"({reason})", text)
                self.assertNotIn("причина неизвестна", text)

    def test_a_hold_that_has_ended_is_no_problem(self):
        store.set_send_hold(self.conn, "uncertain", 15 * MIN)
        self.tick(15 * MIN - 1)
        self.assertEqual(self.keys(), ["hold:send"])
        self.tick(1)
        self.assertEqual(self.keys(), [])
        self.assertIsNotNone(store.kv_get(self.conn, "send_hold"))   # still stored, no longer in force


class Waiting(Db):
    GATES = [   # (waiting reason, what closes the gate, the problem that closing it is by itself)
        ("sending_off", lambda t: t.configure(sending=False), None),
        ("no_budget", lambda t: store.arm(t.conn, 0), None),
        ("account_hold:auth", lambda t: store.set_account_hold(t.conn, "auth"), "hold:account"),
        ("send_hold:uncertain", lambda t: store.set_send_hold(t.conn, "uncertain", 900), "hold:send"),
        ("no_identity", lambda t: t.set(me_user_id=None), None),
        ("no_sweep", lambda t: t.read(fails=1), None),
        ("no_sweep", lambda t: t.read(full_ago=None, head_ago=None), None),
        ("no_sweep", lambda t: t.read(state="empty"), "reel:111"),
        ("stalled", lambda t: None, None),
    ]

    def test_people_wait_behind_each_gate(self):
        for reason, close, own in self.GATES:
            with self.subTest(reason):
                self.setUp()
                self.add("c1", age=2 * HOUR)
                close(self)
                found = self.problems()
                self.assertEqual(sorted(found), sorted(filter(None, [own, "waiting"])))
                self.assertIn(f"Причина: {reason} (", found["waiting"])
                self.assertIn("1 чел. ждут ссылку", found["waiting"])
                self.assertEqual(self.reel()["waiting_reason"], reason)
                self.doCleanups()

    def test_nobody_waits_behind_a_closed_gate(self):
        for reason, close, own in self.GATES:
            with self.subTest(reason):
                self.setUp()
                self.add("c1", status="done")
                self.attempt("c1", "ok", ago=2 * HOUR)
                close(self)
                self.assertEqual(self.keys(), [own] if own else [])
                self.assertEqual(self.reel()["waiting_reason"], "")
                self.doCleanups()

    def test_the_first_closed_gate_is_the_reason(self):
        order = [close for _, close, _ in self.GATES[:6]]
        names = [reason for reason, _, _ in self.GATES[:6]]
        for first in range(len(order)):
            with self.subTest(names[first]):
                self.setUp()
                self.add("c1")
                for close in order[first:]:
                    close(self)
                self.assertIn(f"Причина: {names[first]} (", self.problems()["waiting"])
                self.doCleanups()

    def test_not_when_the_only_reason_is_the_slot(self):
        self.add("c1")
        self.attempt("w", "rejected", ago=HOUR)
        self.attempt("x", "rejected", ago=59)          # 60 per hour: the next DM may go 60 s after the last
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.reel()["waiting_reason"], "cap")
        self.tick(1)
        self.assertEqual(self.keys(), ["waiting"])      # the slot is free and still nothing went out
        self.assertEqual(self.reel()["waiting_reason"], "stalled")

    def test_the_slot_runs_from_the_answer_of_the_last_dm(self):
        self.add("c1")
        self.conn.execute("INSERT INTO attempts (comment_id, kind, started_at, finished_at, outcome) VALUES "
                          "('x', 'dm', ?, ?, 'rejected')", (self.ago(100), self.ago(59)))
        self.assertEqual(self.keys(), [])
        self.tick(1)
        self.assertEqual(self.keys(), ["waiting"])

    def test_not_when_the_only_reason_is_the_hourly_cap(self):
        self.configure(max_sends_per_hour=2)             # one slot is 30 min
        self.add("c1")
        self.attempt("x", "rejected", ago=clock.T["rate_window"] + clock.T["cap_margin"] - 1)
        self.attempt("y", "rejected", ago=31 * MIN)
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.reel()["waiting_reason"], "cap")
        self.tick(1)
        self.assertEqual(self.keys(), ["waiting"])

    def test_a_public_reply_does_not_use_a_dm_slot(self):
        self.add("c1")
        self.attempt("x", "ok", ago=10, kind="public")
        self.assertEqual(self.keys(), ["waiting"])

    def test_not_when_a_dm_went_out_in_the_last_30_minutes(self):
        store.arm(self.conn, 0)
        self.add("c1")
        self.add("d0", status="done")
        self.add("d1", status="done")
        self.attempt("d0", "ok", ago=2 * HOUR)
        self.attempt("d1", "ok", ago=5 * MIN)
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.reel()["waiting_reason"], "no_budget")   # the report still says why
        self.tick(25 * MIN - 1)
        self.assertEqual(self.keys(), [])
        self.tick(1)
        self.assertEqual(self.keys(), ["waiting"])

    def test_only_an_ok_dm_counts_as_progress(self):
        store.arm(self.conn, 0)
        self.add("c1")
        self.add("d1", status="done")
        self.attempt("d1", "ok", ago=2 * HOUR)
        for kind, outcome in (("dm", "uncertain"), ("dm", "rejected"), ("dm", None), ("public", "ok")):
            self.attempt("d1", outcome, ago=5 * MIN, kind=kind)
        self.assertIn("waiting", self.keys())

    def test_gates_open_and_a_dm_just_went_out_is_normal_work(self):
        self.add("c1")
        self.add("d1", status="done")
        self.attempt("d1", "ok", ago=5 * MIN)
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.reel()["waiting_reason"], "cap")
        self.assertEqual(self.build()["verdict"], "OK: в очереди 1 чел., отправка идёт")

    def test_how_many_how_long_and_how_long_until_the_first_is_lost(self):
        store.arm(self.conn, 0)
        self.add("c1", age=2 * HOUR, user="ann")
        self.add("c2", age=1 * HOUR, user="ann")       # the same person again: one DM, one person
        self.add("c3", age=30 * MIN, user="bob")
        self.assertEqual(self.problems()["waiting"],
                         "2 чел. ждут ссылку. Причина: no_budget (бюджет отправок 0 — нужна команда igbot arm N). "
                         "Самый старый комментарий ждёт 2 ч 0 мин; первый истечёт через 6 дн 21 ч.")   # 7 d - 10 min - 2 h
        entry = self.reel()
        self.assertEqual((entry["people_waiting"], entry["oldest_pending_age_sec"], entry["first_expires_in_sec"]),
                         (2, 2 * HOUR, WINDOW - 2 * HOUR))

    def test_a_comment_past_its_window_that_expire_has_not_taken_yet(self):
        store.arm(self.conn, 0)
        self.add("c1", age=WINDOW - 1)
        self.assertIn("первый истечёт через меньше минуты.", self.problems()["waiting"])
        self.tick(1)
        self.assertIn("срок первого уже вышел.", self.problems()["waiting"])   # store.window_closed says so too
        self.assertTrue(store.window_closed(self.ago(WINDOW), self.now))

    def test_a_public_reply_owed_behind_a_hold_is_waiting_too(self):
        self.deliver("c1", ago=31 * MIN)
        store.set_send_hold(self.conn, "send_blocked", 900)
        found = self.problems()
        self.assertEqual(list(found), ["hold:send", "waiting"])
        self.assertIn("Ссылки отправлены, публичный ответ не отправлен под 1 комментариями.", found["waiting"])
        self.assertIn("send_hold:send_blocked", found["waiting"])
        self.assertNotIn("Самый старый", found["waiting"])
        self.assertEqual(self.reel()["public_owed"], 1)

    def test_a_public_reply_needs_no_budget(self):
        self.deliver("c1", ago=31 * MIN)
        store.arm(self.conn, 0)
        self.assertEqual(self.reel()["waiting_reason"], "stalled")     # not no_budget: nothing stands in its way
        self.add("c2")
        self.assertEqual(self.reel()["waiting_reason"], "no_budget")   # the DM of c2 does need it

    def test_a_public_reply_owed_right_after_its_dm_is_not_waiting(self):
        self.deliver("c1", ago=5 * MIN)
        store.set_send_hold(self.conn, "send_blocked", 900)
        self.assertEqual(self.keys(), ["hold:send"])

    def test_rows_of_an_inactive_reel_are_parked_and_their_people_still_wait(self):
        # G5: a reel switched off with people in its queue is a gate left closed (v1 outage A5), not silence
        self.configure(reels=(REEL, dataclasses.replace(SECOND, active=False)), sending=False)
        self.add("c1", media="222")
        self.add("c2", media="222", status="sent")
        self.attempt("c2", "ok", ago=HOUR)
        self.assertEqual(self.keys(), ["waiting"])
        self.assertIn("reel_off", self.problems()["waiting"])
        self.assertEqual(self.reel("222")["parked"], 2)
        self.assertEqual((self.reel("222")["people_waiting"], self.reel("222")["waiting_reason"]), (1, "reel_off"))

    def test_people_of_an_inactive_reel_wait_whatever_the_other_reels_send(self):
        self.configure(reels=(REEL, dataclasses.replace(SECOND, active=False)))
        self.add("c1", media="222")
        self.deliver("c2", ago=MIN)                       # the active reel is busy: nobody of reel 222 is served by that
        self.assertEqual(self.keys(), ["waiting"])

    def test_two_reels_two_reasons(self):
        self.configure(reels=(REEL, SECOND))
        self.read("222", fails=2)
        self.add("c1", media="111")
        self.add("c2", media="222", user="ann")
        self.add("c3", media="222", user="bob")
        self.attempt("x", "rejected", ago=10)             # reel 111 only waits for its slot
        text = self.problems()["waiting"]
        self.assertIn("2 чел. ждут ссылку.", text)       # the two of the reel that cannot be read
        self.assertIn("Причина: no_sweep (", text)
        self.assertNotIn("cap", text)
        self.assertEqual((self.reel("111")["waiting_reason"], self.reel("222")["waiting_reason"]), ("cap", "no_sweep"))

    def test_two_reels_stuck_for_two_reasons(self):
        self.configure(reels=(REEL, SECOND, dataclasses.replace(SECOND, name="third", media_id="333")))
        self.read("222", fails=2)
        self.read("333")
        self.deliver("d333", ago=HOUR, media="333")    # reel 333: a public reply owed, nothing in its way
        self.add("c111", media="111")                  # reel 111: one person, every gate open
        self.add("c222", media="222")                  # reel 222: one person, the reel cannot be read
        text = self.problems()["waiting"]
        self.assertIn("2 чел. ждут ссылку, публичный ответ не отправлен под 1 комментариями. "
                      "Причина: no_sweep (не удаётся прочитать комментарии рилса); "
                      "stalled (все условия выполнены, но сообщения не уходят).", text)


class ExpiringAtTheRealPace(Db):
    def test_the_pace_of_a_long_queue_has_the_margin_of_the_hourly_cap_on_top(self):
        # K-A1-3: the cap is counted over rate_window + cap_margin, so one turn at 1 per hour takes 71 minutes
        self.configure(max_sends_per_hour=1)
        for number in range(30):
            self.add(f"c{number:02}", age=WINDOW - 26 * HOUR - 100 + number)   # all have just over 26 h left
        self.attempt("x", "ok", ago=10)
        turn = clock.T["rate_window"] + clock.T["cap_margin"]
        late = sum(26 * HOUR + 100 - number <= number * turn for number in range(30))
        self.assertGreater(late, 3)   # an hour a turn would have said 3
        self.assertIn(f"до {late} очередь не дойдёт при 1 в час", self.problems()["expiring"])


class Expiring(Db):
    def setUp(self):
        super().setUp()
        # the turns below are counted in whole hours; the margin of the hourly cap is the test above
        patch = mock.patch.dict(clock.T, {"cap_margin": 0})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_window_that_ends_within_24_hours(self):
        self.add("c1", age=WINDOW - 24 * HOUR - 1)
        self.attempt("x", "ok", ago=10)                   # the queue moves: this is about the window alone
        self.assertEqual(self.keys(), [])
        self.tick(1)
        self.assertEqual(self.keys(), ["expiring"])
        text = self.problems()["expiring"]
        self.assertIn("1 чел. могут остаться без ссылки: у 1 до конца 7-дневного окна меньше 24 ч. Первый", text)
        self.assertIn("Первый истекает через 1 дн 0 ч.", text)

    def test_those_who_will_not_get_their_turn_at_the_rate(self):
        self.configure(max_sends_per_hour=1)              # one person per hour
        for number in range(30):
            self.add(f"c{number:02}", age=WINDOW - 26 * HOUR - 100 + number)   # all have just over 26 h left
        self.attempt("x", "ok", ago=10)
        text = self.problems()["expiring"]
        # places 0..26 are served within 26 h; the 27th, 28th and 29th are not
        self.assertIn("3 чел. могут остаться без ссылки: до 3 очередь не дойдёт при 1 в час. Первый", text)

        self.configure(max_sends_per_hour=2)
        self.assertNotIn("expiring", self.keys())

    def test_a_turn_that_comes_as_the_window_closes_is_too_late(self):
        self.configure(max_sends_per_hour=1)
        for place in range(25):
            self.add(f"c{place:02}", age=WINDOW - 24 * HOUR - 60 - place)   # places 0..24: over 24 h left each
        self.add("last", age=WINDOW - 25 * HOUR - 1)                       # place 25, its turn in 25 h
        self.attempt("x", "ok", ago=10)
        self.assertEqual(self.keys(), [])
        self.tick(1)
        self.assertIn("1 чел. могут остаться без ссылки: до 1 очередь не дойдёт при 1 в час.", self.problems()["expiring"])
        self.assertTrue(store.window_closed(self.ago(WINDOW - 25 * HOUR), self.now + timedelta(hours=25)))

    def test_both_reasons_are_counted_once_per_person(self):
        self.configure(max_sends_per_hour=1)
        for number in range(30):
            self.add(f"c{number:02}", age=WINDOW - 26 * HOUR - 100 + number)
        self.add("old", age=WINDOW - HOUR)                # first in the queue, 1 h left
        self.attempt("x", "ok", ago=10)
        text = self.problems()["expiring"]
        self.assertIn("5 чел. могут остаться без ссылки: у 1 до конца 7-дневного окна меньше 24 ч, "
                      "до 4 очередь не дойдёт при 1 в час.", text)
        self.assertIn("Первый истекает через 1 ч 0 мин.", text)

    def test_the_queue_is_in_the_order_of_the_send_step(self):
        self.configure(max_sends_per_hour=1)
        self.add("deferred", age=WINDOW - 2 * HOUR)       # oldest, but deferred: it goes last (8.4)
        self.conn.execute("UPDATE comments SET deferrals=1 WHERE comment_id='deferred'")
        for number in range(30):
            self.add(f"c{number:02}", age=DAY + number)
        self.attempt("x", "ok", ago=10)
        self.assertIn("до 1 очередь не дойдёт", self.problems()["expiring"])   # place 30 with 2 h left

    def test_a_second_comment_of_a_person_takes_no_turn(self):
        self.configure(max_sends_per_hour=1)
        for number in range(30):
            self.add(f"c{number:02}", age=WINDOW - 26 * HOUR - 100 + number, user="ann" if number < 10 else None)
        self.attempt("x", "ok", ago=10)
        self.assertEqual(self.keys(), [])                 # 21 people, the last at place 20

    def test_rows_of_an_inactive_reel_count(self):
        # their windows run on while the reel is off: 9 `expiring` has no exception for them
        self.configure(reels=(dataclasses.replace(REEL, active=False), SECOND))
        self.add("c1", age=WINDOW - HOUR)
        self.assertEqual(self.keys(), ["waiting", "expiring"])

    def test_other_statuses_do_not_count(self):
        for status in ("seen", "sent", "done", "uncertain", "refused", "expired", "dup_user", "already"):
            self.add("c-" + status, age=WINDOW - HOUR, status=status)
            self.attempt("c-" + status, "ok", ago=10)
        self.assertNotIn("expiring", self.keys())


class Unaccounted(Db):
    ZERO = {"sending": 0, "open_attempts": 0, "public_owed": 0, "sent_without_ok_dm": 0, "media_not_in_config": 0}

    def only(self, **parts):
        self.assertEqual(self.parts(), {**self.ZERO, **parts})
        self.assertEqual(self.build()["unaccounted"], sum(parts.values()))
        self.assertEqual("unaccounted" in self.keys(), bool(parts))

    def test_nothing_is_unaccounted_on_the_normal_path(self):
        self.deliver("c1", ago=20 * MIN)
        claim = store.claim_public(self.conn, "c1", self.cfg, ME)
        store.record_public(self.conn, claim.attempt_id, PUBLIC_OK)
        self.add("c2")
        self.add("c3", status="seen")
        self.only()

    def test_a_claim_that_was_never_answered(self):
        self.add("c1")
        with self.back(3 * MIN - 1):
            self.assertEqual(store.claim_dm(self.conn, "c1", self.cfg, ME).kind, "claimed")
        self.only()                                       # a POST may still be in flight
        self.tick(1)
        self.only(sending=1, open_attempts=1)
        text = self.problems()["unaccounted"]
        self.assertIn("2 записей не сходятся", text)
        self.assertIn("отправка зависла — 1, попытка без ответа — 1", text)

        self.assertEqual(store.recover_stale(self.conn), 1)   # the next cycle settles it
        self.only()

    def test_a_sending_comment_alone(self):
        self.add("c1", status="sending")
        self.only()
        self.tick(3 * MIN)
        self.only(sending=1)

    def test_an_open_attempt_alone(self):
        self.add("c1", status="uncertain")
        self.attempt("c1", None, kind="public")
        self.only()
        self.tick(3 * MIN)
        self.only(open_attempts=1)

    def test_a_public_reply_owed_with_nothing_in_its_way_for_10_minutes(self):
        self.deliver("c1", ago=10 * MIN - 1)
        self.only()
        self.tick(1)
        self.only(public_owed=1)
        self.assertIn("публичный ответ не отправлен — 1", self.problems()["unaccounted"])

    def test_a_public_reply_owed_behind_a_closed_gate_is_not_unaccounted(self):
        gates = [
            ("sending off", lambda t: t.configure(sending=False)),
            ("identity unknown", lambda t: t.set(me_user_id=None)),
            ("account hold", lambda t: store.set_account_hold(t.conn, "blocked")),
            ("send hold in force", lambda t: store.set_send_hold(t.conn, "send_blocked", HOUR)),
            ("reel failing", lambda t: t.read(fails=1)),
            ("reel state", lambda t: t.read(state="gone")),
            ("reel never swept", lambda t: t.read(full_ago=None)),
            ("reel not read since the DM", lambda t: t.read(full_ago=40 * MIN, head_ago=30 * MIN)),
            ("reel inactive (parked)", lambda t: t.configure(reels=(dataclasses.replace(REEL, active=False),))),
        ]
        for name, close in gates:
            with self.subTest(name):
                self.setUp()
                self.deliver("c1", ago=30 * MIN)
                self.only(public_owed=1)
                close(self)
                self.only()
                self.doCleanups()

    def test_a_send_hold_must_be_over_for_10_minutes(self):
        self.deliver("c1", ago=HOUR)
        with self.back(30 * MIN):
            store.set_send_hold(self.conn, "send_blocked", 20 * MIN + 1)   # ended 10 min ago less a second
        self.read()
        self.only()
        self.tick(1)
        self.read()
        self.only(public_owed=1)

    def test_the_reel_must_have_been_read_after_the_dm(self):
        self.deliver("c1", ago=20 * MIN)
        self.read(full_ago=40 * MIN, head_ago=20 * MIN)       # read in the same instant: before the DM
        self.only()
        self.read(full_ago=40 * MIN, head_ago=20 * MIN - 1)
        self.only(public_owed=1)
        self.read(full_ago=20 * MIN - 1, head_ago=None)        # a full sweep is a read as well
        self.only(public_owed=1)

    def test_sent_or_done_without_an_ok_dm(self):
        self.add("c1", status="done")
        self.only(sent_without_ok_dm=1)
        self.attempt("c1", "uncertain")
        self.attempt("c1", "ok", kind="public")
        self.attempt("other", "ok")
        self.only(sent_without_ok_dm=1)
        self.assertIn("отмечено отправленным без подтверждения Meta — 1", self.problems()["unaccounted"])
        self.attempt("c1", "ok")
        self.only()

    def test_sent_without_an_ok_dm(self):
        self.configure(sending=False)                         # keep the public-reply term out of it
        self.add("c1", status="sent")
        self.only(sent_without_ok_dm=1)

    def test_statuses_that_need_no_ok_dm(self):
        for status in ("seen", "pending", "uncertain", "refused", "expired", "dup_user", "already"):
            self.add("c-" + status, status=status)
        self.attempt("x", "ok", ago=10)
        self.only()

    def test_rows_whose_media_is_not_in_the_config(self):
        self.add("c1", media="999")
        self.add("c2", media="999", status="sent")
        self.attempt("c2", "ok", ago=10)
        for status in ("seen", "done", "uncertain", "refused", "expired", "dup_user", "already"):
            self.add("c-" + status, media="999", status=status)
            self.attempt("c-" + status, "ok", ago=10)
        self.only(media_not_in_config=2)
        self.assertIn("рилса нет в настройках — 2", self.problems()["unaccounted"])
        entry = self.reel("999")
        self.assertEqual((entry["configured"], entry["active"], entry["name"], entry["parked"]), (False, False, None, 0))

    def test_rows_of_an_inactive_reel_are_parked_not_unaccounted(self):
        self.configure(reels=(REEL, dataclasses.replace(SECOND, active=False)))
        self.add("c1", media="222")
        self.add("c2", media="222", status="sent")
        self.attempt("c2", "ok", ago=HOUR)
        self.attempt("x", "ok", ago=10)
        self.only()
        self.configure(reels=(REEL,))                          # the same rows once the reel is gone from the config
        self.only(media_not_in_config=2)

    def test_no_reel_configured_at_all(self):
        self.configure(reels=())
        self.add("c1")
        self.only(media_not_in_config=1)


class Reel(Db):
    def test_a_state_that_is_not_ok(self):
        for state, words in (("gone", "не находит это видео"), ("mismatch", "не на то видео"),
                             ("empty", "пустой список"), ("capped", "400 страниц"), ("new_state", "new_state")):
            with self.subTest(state):
                self.read(state=state)
                self.assertEqual(self.keys(), ["reel:111"])
                self.assertIn("Рилс «arena»", self.problems()["reel:111"])
                self.assertIn(words, self.problems()["reel:111"])
        self.read(state="ok")
        self.assertEqual(self.keys(), [])

    def test_three_failed_sweeps_in_a_row(self):
        self.read(fails=2)
        self.assertEqual(self.keys(), [])
        self.read(fails=3)
        self.assertEqual(self.keys(), ["reel:111"])
        self.assertIn("3 раз подряд", self.problems()["reel:111"])

    def test_no_full_sweep_for_two_hours(self):
        self.read(full_ago=2 * HOUR - 1)
        self.assertEqual(self.keys(), [])
        self.tick(1)
        self.assertEqual(self.keys(), ["reel:111"])
        self.assertIn("не перечитывались полностью уже 2 ч 0 мин (последний раз 05.10 10:00 UTC)",
                      self.problems()["reel:111"])

    def test_head_polls_do_not_replace_the_full_sweep(self):
        self.read(full_ago=3 * HOUR, head_ago=0)
        self.assertEqual(self.keys(), ["reel:111"])

    def test_not_stale_while_the_account_is_on_hold(self):
        self.read(full_ago=3 * HOUR)
        store.set_account_hold(self.conn, "blocked")
        self.assertEqual(self.keys(), ["hold:account"])

    def test_not_stale_in_the_cycle_that_lifts_the_hold(self):
        notifier = notify.Notifier(self.conn, lambda: (None, None), "http://127.0.0.1:9")
        store.set_account_hold(self.conn, "blocked")
        with contextlib.redirect_stderr(io.StringIO()):
            notifier.sync(report.problems(self.conn, self.cfg, self.now), self.now)   # the hold is announced
            self.read(full_ago=3 * HOUR)
            store.clear_account_hold(self.conn)       # the probe succeeded; collect has not run yet
            found = report.problems(self.conn, self.cfg, self.now)
            self.assertEqual(found, [])
            notifier.sync(found, self.now)             # ... the hold is cleared
        self.assertEqual(self.keys(), ["reel:111"])    # next cycle: the reel had its chance

    def test_a_reel_that_was_never_swept_is_not_called_stale(self):
        self.read(state=None, full_ago=None, head_ago=None)
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.reel()["full_sweep_at"], None)

    def test_an_inactive_reel_is_no_problem_of_its_own(self):
        self.configure(reels=(dataclasses.replace(REEL, active=False), SECOND))
        self.read(state="gone", fails=9, full_ago=DAY)
        self.assertEqual(self.keys(), [])

    def test_no_active_reel_at_all_is_a_problem(self):
        # CX-70: a bot that reads nothing must not say OK for ever (v1 outage A5, the forgotten STOP file)
        for reels in ((dataclasses.replace(REEL, active=False),), ()):
            with self.subTest(reels=len(reels)):
                self.configure(reels=reels)
                self.assertEqual(self.keys(), ["no_active_reel"])
                self.assertTrue(self.build()["verdict"].startswith("PROBLEM: no_active_reel"))

    def test_each_reel_has_its_own_key(self):
        self.configure(reels=(REEL, SECOND))
        self.read("111", state="gone")
        self.read("222", fails=3)
        self.assertEqual(self.keys(), ["reel:111", "reel:222"])
        self.assertIn("«second»", self.problems()["reel:222"])


class Token(Db):
    def token(self, refreshed_ago, expires_in=None, fails=None, failed_ago=None):
        self.set(token=IG_TOKEN, token_refreshed_at=self.ago(refreshed_ago),
                 token_expires_at=None if expires_in is None else self.ago(-expires_in),
                 token_refresh_fails=fails,
                 token_refresh_failed_at=None if failed_ago is None else self.ago(failed_ago))

    def test_fewer_than_14_days_remain(self):
        self.token(refreshed_ago=46 * DAY, expires_in=14 * DAY)
        self.assertEqual(self.keys(), [])
        self.tick(1)
        self.assertEqual(self.keys(), ["token"])
        self.assertIn("истекает через 13 дн 23 ч (19.10 12:00 UTC)", self.problems()["token"])

    def test_an_expired_token(self):
        self.token(refreshed_ago=61 * DAY, expires_in=-DAY)
        self.assertIn("Токен Instagram истёк (04.10 12:00 UTC)", self.problems()["token"])
        self.token(refreshed_ago=60 * DAY, expires_in=1)
        self.assertIn("Токен Instagram истекает через меньше минуты (05.10 12:00 UTC)", self.problems()["token"])
        self.tick(1)
        self.assertIn("Токен Instagram истёк (05.10 12:00 UTC)", self.problems()["token"])

    def test_an_unknown_expiry_is_60_days_from_the_last_refresh(self):
        self.token(refreshed_ago=46 * DAY)
        self.assertEqual(self.keys(), [])
        self.tick(1)
        self.assertEqual(self.keys(), ["token"])

    def test_a_known_expiry_wins_over_the_60_days(self):
        self.token(refreshed_ago=50 * DAY, expires_in=20 * DAY)
        self.assertEqual(self.keys(), [])

    def test_a_young_token_is_fine(self):
        self.token(refreshed_ago=3 * DAY, expires_in=57 * DAY)
        self.assertEqual(self.keys(), [])

    def test_no_token_keys_no_token_problem(self):
        self.assertEqual(self.keys(), [])               # no token at all is the account hold `no_token`

    def test_two_refreshes_failed_in_a_row(self):
        self.token(refreshed_ago=8 * DAY, expires_in=52 * DAY, fails=1, failed_ago=7 * HOUR)
        self.assertEqual(self.keys(), [])
        self.token(refreshed_ago=8 * DAY, expires_in=52 * DAY, fails=2, failed_ago=HOUR)
        self.assertEqual(self.keys(), ["token"])
        self.assertIn("не удаётся обновить: 2 раз подряд, последний 05.10 11:00 UTC", self.problems()["token"])
        self.token(refreshed_ago=0, expires_in=60 * DAY)   # a refresh went through: the engine clears the count
        self.assertEqual(self.keys(), [])

    def test_unreadable_values_are_not_a_crash(self):
        self.set(token=IG_TOKEN, token_refreshed_at="yesterday", token_expires_at="5184000", token_refresh_fails="many",
                 token_refresh_failed_at="")
        self.assertEqual(self.keys(), [])
        token = self.build()["gates"]["token"]
        self.assertEqual((token["age_sec"], token["expires_in_sec"], token["refresh_fails"]), (None, None, 0))


class SmallProblems(Db):
    def test_cycle_error(self):
        self.set(last_cycle_error="ValueError: boom in collect")
        self.assertEqual(self.keys(), ["cycle_error"])
        self.assertIn("Сбой в работе бота: ValueError: boom in collect.", self.problems()["cycle_error"])
        self.set(last_cycle_error=None)
        self.assertEqual(self.keys(), [])
        self.set(last_cycle_error="")
        self.assertEqual(self.keys(), [])

    def test_a_long_cycle_error_is_cut(self):
        self.set(last_cycle_error="x" * 5000)
        self.assertIn("Сбой в работе бота: " + "x" * 200 + ". ", self.problems()["cycle_error"])

    def test_clock(self):
        self.set(clock_problem="the clock jumped 700 s ahead, now 2026-10-05T12:00:00.000Z")   # the engine's guard
        self.assertEqual(self.keys(), ["clock"])
        self.assertIn("(the clock jumped 700 s ahead, now 2026-10-05T12:00:00.000Z)", self.problems()["clock"])
        self.set(clock_problem=None)                     # the next cycle found the clock in order
        self.assertEqual(self.keys(), [])
        self.set(last_now=self.ago(-DAY))                # the guard is the engine's: the report does not second-guess it
        self.assertEqual(self.keys(), [])

    def test_public_replies_switched_off_in_the_config_are_no_problem(self):
        self.assertIs(self.build()["gates"]["public_reply"], True)
        self.configure(public_reply=False)
        self.assertEqual(self.keys(), [])
        self.assertIs(self.build()["gates"]["public_reply"], False)

    def test_no_alert_channel(self):
        self.set(tg_configured=0)
        self.assertEqual(self.keys(), ["no_alert_channel"])
        self.assertIn("Telegram не настроен", self.problems()["no_alert_channel"])
        self.set(tg_configured=1)
        self.assertEqual(self.keys(), [])
        self.set(tg_configured=None)                      # nothing has looked yet: not known, not a problem
        self.assertEqual(self.keys(), [])

    def test_no_alert_channel_while_the_last_delivery_failed(self):
        self.set(tg_ok_at=self.ago(HOUR), tg_failed_at=self.ago(HOUR + 1), tg_last_outcome="ok")
        self.assertEqual(self.keys(), [])
        self.set(tg_failed_at=self.ago(10 * MIN), tg_last_outcome="failed")
        store.outbox_add(self.conn, "waiting", "x")
        self.assertEqual(self.keys(), ["no_alert_channel"])
        self.assertIn("последняя попытка 05.10 11:50 UTC), 1 оповещений ждут отправки",
                      self.problems()["no_alert_channel"])

    def test_the_token_never_reaches_a_problem_text(self):
        self.set(token=IG_TOKEN, token_env_seen="IGAAolderENVtoken",
                 last_cycle_error=f"HTTPError for https://graph/me?access_token={IG_TOKEN} and IGAAolderENVtoken")
        store.set_account_hold(self.conn, "auth", f"token {IG_TOKEN} refused")
        store.set_send_hold(self.conn, "send_blocked", HOUR, f"dm global/auth {IG_TOKEN}")
        self.assertIn("access_token=<token> and <token>", self.problems()["cycle_error"])
        self.assertIn("token <token> refused", self.problems()["hold:account"])
        everything = (json.dumps(self.build(), ensure_ascii=False) + report.render(self.build())
                      + " ".join(self.problems().values()))
        self.assertNotIn(IG_TOKEN, everything)
        self.assertNotIn("IGAAolderENVtoken", everything)
        self.assertEqual(self.build()["gates"]["send_hold"]["detail"], "dm global/auth <token>")
        self.assertIs(self.build()["gates"]["token"]["present"], True)


class NotRunning(Db):
    def stop_service(self):
        fcntl.flock(self.lock, fcntl.LOCK_UN)

    def not_running(self):
        return dict(map(tuple, self.build()["problems"])).get("not_running")

    def test_a_running_service(self):
        self.assertIsNone(self.not_running())
        self.assertEqual(self.build()["gates"]["service"], {"running": True, "lock_held": True,
                                                            "last_cycle_ok_at": clock.iso(START), "last_cycle_error": None})

    def test_nobody_holds_the_lock(self):
        self.stop_service()
        self.assertIn("никто не держит core.lock", self.not_running())
        self.assertEqual((self.build()["gates"]["service"]["running"], self.build()["gates"]["service"]["lock_held"]),
                         (False, False))
        self.assertEqual(self.build()["problems"][0][0], "not_running")   # it explains the rest: it comes first
        self.assertTrue(self.build()["verdict"].startswith("PROBLEM: not_running"))

    def test_no_lock_file_at_all(self):
        self.lock.close()
        os.remove(os.path.join(self.dir, "core.lock"))
        self.assertIn("никто не держит core.lock", self.not_running())
        self.assertEqual(os.listdir(self.dir).count("core.lock"), 0)      # and the report did not create one

    def test_a_lock_that_cannot_be_looked_at_leaves_the_last_cycle_to_tell(self):
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        os.remove(os.path.join(self.dir, "core.lock"))
        os.mkdir(os.path.join(self.dir, "core.lock"))
        self.assertIsNone(self.build()["gates"]["service"]["lock_held"])
        self.assertIsNone(self.not_running())
        self.tick(10 * MIN + 1)
        self.assertIn("последний успешный цикл был 10 мин назад", self.not_running())

    def test_a_database_that_is_not_a_file_has_no_lock_beside_it(self):
        memory = store.connect(":memory:")
        self.addCleanup(memory.close)
        store.init(memory)
        store.kv_set(memory, "last_cycle_ok_at", clock.iso())
        service = report.build(memory, self.cfg, self.now)["gates"]["service"]
        self.assertEqual((service["lock_held"], service["running"]), (None, True))

    def test_no_good_cycle_for_10_minutes(self):
        self.tick(10 * MIN)
        self.assertIsNone(self.not_running())
        self.tick(1)
        self.assertIn("последний успешный цикл был 10 мин назад", self.not_running())
        service = self.build()["gates"]["service"]
        self.assertEqual((service["running"], service["lock_held"]), (False, True))
        self.assertIn("сервис: НЕ РАБОТАЕТ, последний успешный цикл 05.10 12:00 UTC", report.render(self.build()))

    def test_no_good_cycle_ever(self):
        self.set(last_cycle_ok_at=None)
        self.assertIn("ни один цикл ещё не завершился успешно", self.not_running())

    def test_it_is_in_the_report_only(self):
        self.stop_service()
        self.set(last_cycle_ok_at=None)
        self.assertEqual(self.keys(), [])

    def test_looking_does_not_take_the_lock_away(self):
        self.build()
        with open(os.path.join(self.dir, "core.lock")) as second:
            with self.assertRaises(BlockingIOError):                      # the service still holds it
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.stop_service()
        self.build()
        with open(os.path.join(self.dir, "core.lock")) as starting:
            fcntl.flock(starting, fcntl.LOCK_EX | fcntl.LOCK_NB)          # a bot that starts now gets it


class Verdict(Db):
    def test_ok(self):
        self.assertEqual(self.build()["verdict"], "OK: никто не ждёт")

    def test_waiting(self):
        store.arm(self.conn, 0)
        self.add("c1", age=WINDOW - HOUR)
        report_ = self.build()
        self.assertEqual([key for key, _ in report_["problems"]], ["waiting", "expiring"])
        self.assertEqual(report_["verdict"], "WAITING: " + report_["problems"][0][1])
        self.assertIn("1 чел. ждут ссылку", report_["verdict"])

    def test_expiring_alone_is_waiting_too(self):
        self.add("c1", age=WINDOW - HOUR)
        self.attempt("x", "ok", ago=10)
        self.assertTrue(self.build()["verdict"].startswith("WAITING: 1 чел. могут остаться без ссылки"))

    def test_problem(self):
        store.arm(self.conn, 0)
        self.add("c1")
        store.set_send_hold(self.conn, "spam", DAY)
        self.set(last_cycle_error="boom")
        self.assertEqual(self.build()["verdict"], "PROBLEM: hold:send, waiting, cycle_error")

    def test_the_first_line_of_the_text_is_the_verdict(self):
        for change in (lambda: None, lambda: (store.arm(self.conn, 0), self.add("c1")),
                       lambda: self.set(last_cycle_error="boom")):
            change()
            report_ = self.build()
            self.assertEqual(report.render(report_).split("\n")[0], report_["verdict"])
        self.assertTrue(report_["verdict"].startswith("PROBLEM: "))


class Report(Db):
    def fill(self):
        """A database with something in every part of the report."""
        self.configure(reels=(REEL, dataclasses.replace(SECOND, active=False)))
        self.set(token=IG_TOKEN, token_refreshed_at=self.ago(3 * DAY), token_expires_at=self.ago(-57 * DAY),
                 tg_ok_at=self.ago(HOUR))
        for hours in (200, 30, 1):
            with self.back(hours * HOUR):
                store.add_metric(self.conn, 1000 - hours, 7)
        self.deliver("old", ago=25 * HOUR)
        self.deliver("d1", ago=3 * HOUR)
        self.deliver("d2", ago=3 * HOUR - 2 * MIN)
        for comment_id in ("old", "d1"):
            claim = store.claim_public(self.conn, comment_id, self.cfg, ME)
            store.record_public(self.conn, claim.attempt_id, PUBLIC_OK)
        self.add("u1")
        with self.back(2 * HOUR):
            claim = store.claim_dm(self.conn, "u1", self.cfg, ME)
            store.record_dm(self.conn, claim.attempt_id, answer("uncertain", "code_1"))   # and a 15 min hold
        self.add("p1", age=2 * HOUR, user="ann")
        self.add("p2", age=HOUR, user="ann")
        self.add("p3", age=30 * MIN, user="bob")
        self.add("dup1", status="dup_user", reason="dup:d1")
        self.add("dup2", status="dup_user", reason="dup:d2")
        self.add("near1", status="seen", reason="near:question", text="зачем арена?", age=50)
        self.add("near2", status="seen", reason="near:too_long", text="арена " * 9, age=40)
        for number, text in enumerate(["Огонь", "огонь!!", "ОГОООНЬ 🔥", "класс", "арина", "арина",
                                       "это очень длинный комментарий ни о чём", "🔥🔥🔥"]):
            self.add(f"s{number}", status="seen", text=text)
        self.add("parked", media="222")
        self.add("stray", media="999", status="seen", text="класс")

    def test_json_round_trip(self):
        self.fill()
        data = self.build()
        again = json.loads(json.dumps(data, ensure_ascii=False))
        self.assertEqual(again, data)
        self.assertEqual(report.render(again), report.render(data))
        self.assertIsInstance(data["unaccounted"], int)
        self.assertEqual(set(data), {"at", "verdict", "problems", "unaccounted", "unaccounted_parts", "gates", "reels",
                                     "dms_per_hour", "attempts", "uncertain", "uncertain_unresolved", "near", "unmatched", "followers"})

    def test_json_round_trip_of_an_empty_database(self):
        other = store.connect(os.path.join(self.dir, "empty.db"))
        self.addCleanup(other.close)
        store.init(other)
        data = report.build(other, self.cfg, self.now)
        self.assertEqual(json.loads(json.dumps(data)), data)
        self.assertEqual(data["verdict"], "PROBLEM: not_running")
        self.assertEqual(data["unaccounted"], 0)
        self.assertIn("аккаунт: НЕИЗВЕСТЕН", report.render(data))
        self.assertIn("токен: НЕТ", report.render(data))

    def test_gates(self):
        self.fill()
        gates = self.build()["gates"]
        self.assertEqual(gates["sending"], True)
        self.assertEqual((gates["budget"], gates["armed_n"], gates["armed_at"]),
                         ("unlimited", "unlimited", clock.iso(START)))
        self.assertEqual((gates["account_hold"], gates["send_hold"]), (None, None))
        self.assertEqual(gates["identity"], {"user_id": "9000", "id": "9001", "username": "owner",
                                             "checked_at": clock.iso(START)})
        self.assertEqual(gates["token"], {"present": True, "age_sec": 3 * DAY, "expires_in_sec": 57 * DAY,
                                          "refresh_fails": 0, "refresh_failed_at": None})
        self.assertEqual(gates["telegram"], {"configured": True, "ok_at": self.ago(HOUR), "failed_at": None,
                                             "failing": False, "queued": 0})
        self.assertNotIn(IG_TOKEN, json.dumps(self.build()))
        self.assertNotIn(IG_TOKEN, report.render(self.build()))

    def test_gates_that_are_closed(self):
        self.configure(sending=False)
        store.arm(self.conn, 7)
        hold = store.set_account_hold(self.conn, "auth", "x")
        store.set_send_hold(self.conn, "spam", DAY)
        gates = self.build()["gates"]
        self.assertEqual((gates["sending"], gates["budget"], gates["armed_n"]), (False, 7, "7"))
        self.assertEqual(gates["account_hold"], hold)
        self.assertEqual(gates["send_hold"], {"reason": "spam", "until": self.ago(-DAY), "detail": ""})
        data = self.build()
        before = copy.deepcopy(data)
        text = report.render(data)
        self.assertEqual(data, before)                    # rendering changes nothing in the data
        for line in ("отправка: ВЫКЛЮЧЕНА (sending: false)", "бюджет: 7 (arm 7 от 05.10 12:00 UTC)",
                     "пауза аккаунта: auth с 05.10 12:00 UTC", "пауза отправки: spam до 06.10 12:00 UTC"):
            self.assertIn(line, text)

    def test_reels(self):
        self.fill()
        first, second, stray = self.build()["reels"]
        self.assertEqual((first["media_id"], first["name"], first["configured"], first["active"]),
                         ("111", "arena", True, True))
        self.assertEqual((first["state"], first["fails"], first["head_ok_at"], first["full_sweep_at"]),
                         ("ok", 0, clock.iso(START), clock.iso(START)))
        self.assertEqual(first["counts"], [["done", "", 2], ["dup_user", "", 2], ["pending", "", 3], ["seen", "", 8],
                                           ["seen", "near:question", 1], ["seen", "near:too_long", 1],
                                           ["sent", "", 1], ["uncertain", "code_1", 1]])
        self.assertEqual((first["people_waiting"], first["public_owed"], first["parked"]), (2, 1, 0))
        self.assertEqual((first["oldest_pending_age_sec"], first["first_expires_in_sec"]), (2 * HOUR, WINDOW - 2 * HOUR))
        self.assertEqual((second["media_id"], second["name"], second["active"], second["parked"], second["people_waiting"],
                          second["waiting_reason"], second["state"]), ("222", "second", False, 1, 1, "reel_off", None))
        self.assertEqual((stray["media_id"], stray["configured"], stray["counts"]), ("999", False, [["seen", "", 1]]))
        self.assertEqual((second["public_owed"], stray["public_owed"], stray["parked"]), (0, 0, 0))

    def test_dms_per_hour_of_the_last_24_hours(self):
        self.fill()
        self.assertEqual(self.build()["dms_per_hour"], [{"hour": "2026-10-05T09", "posts": 2, "ok": 2},
                                                        {"hour": "2026-10-05T10", "posts": 1, "ok": 0}])
        self.attempt("x", None, ago=24 * HOUR - 1)        # the 24 hours are a window, not whole days
        self.attempt("x", "ok", ago=24 * HOUR)
        self.assertEqual(self.build()["dms_per_hour"][0], {"hour": "2026-10-04T12", "posts": 1, "ok": 0})

    def test_attempts_by_outcome_and_reason(self):
        self.fill()
        self.attempt("x", None)
        self.assertEqual(self.build()["attempts"], [
            {"kind": "dm", "outcome": "ok", "reason": "", "n": 3},
            {"kind": "dm", "outcome": None, "reason": "", "n": 1},
            {"kind": "dm", "outcome": "uncertain", "reason": "code_1", "n": 1},
            {"kind": "public", "outcome": "ok", "reason": "", "n": 2}])

    def test_the_people_whose_dm_is_in_doubt_are_named(self):
        """7.1: nothing sends again; the report names manual cases to check and acknowledge."""
        self.assertEqual(self.build()["uncertain"], [])
        self.assertNotIn("uncertain", report.render(self.build()))
        self.fill()
        self.assertEqual(self.build()["uncertain"], [
            {"comment_id": "u1", "media_id": "111", "username": "name-u1", "reason": "code_1"}])
        for number in range(35):                          # only the newest 30, whatever reel they are under
            self.add(f"n{number:02}", status="uncertain", reason="http_5xx", media="999", age=1000 - number)
        self.assertEqual([row["comment_id"] for row in self.build()["uncertain"]],
                         ["u1"] + [f"n{number:02}" for number in range(34, 5, -1)])
        self.assertIn("проверьте доставку", report.render(self.build()))
        self.assertIn("@name-u1 (arena, code_1); igbot ack-uncertain u1", report.render(self.build()))
        self.assertIn("@name-n34 (999, http_5xx); igbot ack-uncertain n34", report.render(self.build()))

    def test_near_comments(self):
        self.fill()
        near = self.build()["near"]
        self.assertEqual(near["total"], 2)
        self.assertEqual(near["newest"], [
            {"comment_id": "near2", "media_id": "111", "username": "name-near2", "text": "арена " * 9,
             "reason": "too_long"},
            {"comment_id": "near1", "media_id": "111", "username": "name-near1", "text": "зачем арена?",
             "reason": "question"}])

    def test_only_the_newest_near_comments_are_listed(self):
        for number in range(35):
            self.add(f"n{number:02}", status="seen", reason="near:question", age=1000 - number)
        near = self.build()["near"]
        self.assertEqual(near["total"], 35)
        self.assertEqual([row["comment_id"] for row in near["newest"]], [f"n{number:02}" for number in range(34, 4, -1)])

    def test_frequent_short_unmatched_texts(self):
        self.fill()
        # normalised as the matcher reads them; the long one and the one without a letter are left out
        self.assertEqual(self.build()["unmatched"], [["огонь", 3], ["арина", 2], ["клас", 2]])

    def test_at_most_20_unmatched_texts_of_at_most_3_words(self):
        for number in range(25):
            for copy in range(number + 1):
                self.add(f"t{number}-{copy}", status="seen", text="слово" + "абвгдежзиклмнопрстуфхцчшщ"[number])
        self.add("words3", status="seen", text="раз два три")
        self.add("words4", status="seen", text="раз два три четыре")
        unmatched = self.build()["unmatched"]
        self.assertEqual(len(unmatched), 20)
        self.assertEqual(unmatched[0], ["словощ", 25])
        self.assertEqual(unmatched[-1], ["словое", 6])
        self.conn.execute("DELETE FROM comments WHERE comment_id LIKE 't%'")
        self.assertEqual(self.build()["unmatched"], [["раз два три", 1]])

    def test_followers_now_a_day_ago_and_a_week_ago(self):
        self.fill()
        self.assertEqual(self.build()["followers"], {"now": 999, "day_ago": 970, "week_ago": 800})
        self.conn.execute("DELETE FROM metrics WHERE followers < 999")
        self.assertEqual(self.build()["followers"], {"now": 999, "day_ago": None, "week_ago": None})

    def test_followers_at_the_exact_moments(self):
        for seconds, followers in ((7 * DAY, 100), (7 * DAY - 1, 101), (DAY, 200), (DAY - 1, 201), (0, 300)):
            with self.back(seconds):
                store.add_metric(self.conn, followers, 7)
        self.assertEqual(self.build()["followers"], {"now": 300, "day_ago": 200, "week_ago": 100})

    def test_the_text_has_every_part_in_the_order_of_the_spec(self):
        self.fill()
        store.arm(self.conn, 40)
        store.set_send_hold(self.conn, "send_blocked", HOUR)
        text = report.render(self.build())
        marks = ["PROBLEM: hold:send, waiting, uncertain_manual", "Отчёт на 05.10 12:00 UTC", "Проблемы:",
                 "  ⚠ hold:send: Отправка на паузе до 05.10 13:00 UTC", "  ⚠ waiting: 3 чел. ждут ссылку",
                 "Состояние:", "отправка: включена", "бюджет: 40 (arm 40 от 05.10 12:00 UTC)", "пауза аккаунта: нет",
                 "пауза отправки: send_blocked до 05.10 13:00 UTC", "публичные ответы: включены",
                 "аккаунт: @owner (id 9000), проверен 05.10 12:00 UTC", "токен: возраст 3 дн 0 ч, истекает через 57 дн 0 ч",
                 "Telegram: работает (последняя доставка 05.10 11:00 UTC), в очереди 0",
                 "сервис: работает, последний успешный цикл 05.10 12:00 UTC",
                 "не сходится с журналом (unaccounted): 0",
                 "Рилсы:", "«arena» (111): активен, состояние ok, чтение 05.10 12:00 UTC, полное 05.10 12:00 UTC",
                 "ждут ссылку: 2 чел., самый старый 2 ч 0 мин, до конца окна 6 дн 21 ч",
                 "публичный ответ не отправлен: 1", "причина ожидания: send_hold:send_blocked (",
                 "done 2 · dup_user 2 · pending 3 · seen 8 · seen/near:question 1",
                 "«second» (222): выключен, состояние ещё не читался, чтение никогда, полное никогда",
                 "отложено (рилс выключен): 1", "НЕТ В НАСТРОЙКАХ (999): не обрабатывается",
                 "Ссылок в директ по часам за 24 ч (UTC):", "05.10 09:00  попыток 2, доставлено 2",
                 "Все попытки по исходу:", "dm ok: 3", "dm uncertain/code_1: 1", "public ok: 2",
                 "Неизвестно, дошло ли сообщение (uncertain)", "@name-u1 (arena, code_1)",
                 "С ключевым словом, но не просьба (near): 2", "@name-near1: «зачем арена?» (question)",
                 "Частые короткие комментарии без ключевого слова", "3 × огонь",
                 "Подписчики: сейчас 999, сутки назад 970, неделю назад 800"]
        places = [text.find(mark) for mark in marks]
        self.assertNotIn(-1, places, [mark for mark, place in zip(marks, places) if place < 0])
        self.assertEqual(places, sorted(places))

    def test_the_text_of_a_bot_with_trouble(self):
        self.set(tg_configured=0)
        self.add("c1", status="done")
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        text = report.render(self.build())
        for line in ("PROBLEM: not_running, unaccounted, no_alert_channel", "Telegram: НЕ НАСТРОЕН, в очереди 0",
                     "сервис: НЕ РАБОТАЕТ", "не сходится с журналом (unaccounted): 1 (отмечено отправленным без "
                     "подтверждения Meta — 1)", "За 24 ч ссылок в директ не было.", "Попыток отправки ещё не было.",
                     "Коротких комментариев без ключевого слова нет.",
                     "Подписчики: сейчас нет данных, сутки назад нет данных"):
            self.assertIn(line, text)
        self.set(tg_configured=1, tg_ok_at=self.ago(HOUR), tg_failed_at=self.ago(MIN))
        self.assertIn("Telegram: НЕ ПРИНИМАЕТ сообщения (последняя попытка 05.10 11:59 UTC)", report.render(self.build()))
        self.set(tg_ok_at=None, tg_failed_at=None)
        self.assertIn("Telegram: ещё ничего не доставлял", report.render(self.build()))

    def test_no_reels_configured(self):
        self.configure(reels=())
        self.assertIn("Рилсов нет.", report.render(self.build()))


class ReadOnly(Db):
    def test_it_writes_nothing(self):
        Report.fill(self)
        store.arm(self.conn, 0)
        store.set_account_hold(self.conn, "auth")
        before = list(self.conn.iterdump())
        statements = []
        self.conn.set_trace_callback(statements.append)
        report.problems(self.conn, self.cfg, self.now)
        report.build(self.conn, self.cfg, self.now)
        self.conn.set_trace_callback(None)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(self.conn.in_transaction)
        verbs = {statement.split()[0].upper() for statement in statements}
        self.assertEqual(verbs, {"BEGIN", "SELECT", "PRAGMA", "ROLLBACK"})

    def test_one_call_reads_one_moment_of_the_database(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        report.build(self.conn, self.cfg, self.now)
        self.conn.set_trace_callback(None)
        self.assertEqual((statements[0], statements[-1], statements.count("BEGIN")), ("BEGIN", "ROLLBACK", 1))

    def test_inside_the_callers_transaction_it_leaves_the_transaction_alone(self):
        with store.tx(self.conn):
            store.kv_set(self.conn, "last_cycle_error", "boom")
            self.assertEqual(self.keys(), ["cycle_error"])
            self.build()
            self.assertTrue(self.conn.in_transaction)
        self.assertEqual(store.kv_get(self.conn, "last_cycle_error"), "boom")

    def test_on_a_read_only_connection_while_the_bot_writes(self):
        Report.fill(self)
        expected = self.build()
        readonly = store.connect(self.path, readonly=True)
        self.addCleanup(readonly.close)
        self.conn.execute("BEGIN IMMEDIATE")                # the service is in the middle of a write
        self.conn.execute("UPDATE kv SET value='0' WHERE key='send_budget'")
        try:
            self.assertEqual(report.build(readonly, self.cfg, self.now), expected)
            self.assertEqual(report.problems(readonly, self.cfg, self.now),
                             [tuple(problem) for problem in expected["problems"]])
        finally:
            self.conn.execute("ROLLBACK")
        self.assertFalse(readonly.in_transaction)

    def test_a_failure_inside_does_not_leave_a_transaction_open(self):
        self.set(account_hold="{not json")
        with self.assertRaises(ValueError):
            report.problems(self.conn, self.cfg, self.now)
        self.assertFalse(self.conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
