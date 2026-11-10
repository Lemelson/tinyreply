__doc__ = """Attack round 1: the OpenReply scenario catalogue (research/lessons/openreply.md, OR-01..OR-79).

Every scenario of the catalogue that the unit tests and the soak do not already cover has a test here. The
bot is judged by what the strict fake Meta received (sim/fake_meta.py, in-process, real loopback HTTP) and by
the database, with the real report.py and the real notify.py talking to the fake's Telegram. Time is the
test's: wall time and monotonic time are the real clocks plus offsets, clock.sleep() moves the offsets.

The tests that failed on the code of that day (OR-60 small talk, OR-02, OR-69, OR-36) were
confirmed, fixed, and live in tests/test_regress_r1.py (classes OpenReply*, on the harness of this module).
Everything here passed then and is kept as regression evidence (RandomWalks: 30 seeded random scenarios with a
referee that reads the fake's truth).

Run from autoreply/:  python3.12 -B -m unittest tests.test_lessons_openreply
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest import mock

import clock
import config
import engine
import match
import meta
import notify
import report
import store
from sim.fake_meta import FakeMeta

CODE = os.path.dirname(os.path.abspath(engine.__file__))
MIN, HOUR, DAY = 60, 3600, 86400
START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
SLOT = 60   # seconds between two DMs at 60 per hour
ME = {"user_id": "17841400000000001", "id": "9990000000000001", "username": "your_account",
      "followers_count": 1234, "media_count": 7}
OWNER = {"id": ME["id"], "username": ME["username"]}
MEDIA, MEDIA_2 = "17900000000000101", "18000000000000002"
TOKEN = "IGAAattackTOKENsecret1"
TG_TOKEN, TG_CHAT = "424242:attackTGsecret", "42"
with open(os.path.join(CODE, "config.example.json"), encoding="utf-8") as _file:
    EXAMPLE = json.load(_file)


def comment(comment_id, text="арена", user=None, age=60, should_match=True, **more):
    """A scenario comment; by default a fresh request by a person of its own."""
    user = user or "u_" + comment_id
    return {"id": comment_id, "text": text, "age_sec": age, "from": {"id": user, "username": "name_" + user},
            "should_match": should_match, **more}


def media(media_id=MEDIA, comments=(), shortcode="DAbc"):
    return {"id": media_id, "permalink": f"https://www.instagram.com/reel/{shortcode}/", "shortcode": shortcode,
            "comments": list(comments)}


class Time:
    """Wall time = START + real time since the test began + what the test added; monotonic time likewise.
    `wall` alone can be moved: that is a clock that was set, while no time passed."""

    def __init__(self):
        self._time, self._monotonic = time.time, time.monotonic
        self._began, self.wall, self.mono = time.time(), 0.0, 0.0

    def time(self):
        return START + (self._time() - self._began) + self.wall

    def monotonic(self):
        return self._monotonic() + self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds, should_stop=None, ping=None):
        if not (should_stop and should_stop()):
            self.advance(seconds)


class Case(unittest.TestCase):
    """One fake Meta (with Telegram), one temporary STATE_DIR, the real engine, report and notifier."""

    def setUp(self):
        self.time, self.log, self.fake = Time(), io.StringIO(), None
        for patch in (mock.patch("time.time", self.time.time), mock.patch.object(clock, "monotonic", self.time.monotonic),
                      mock.patch.object(clock, "sleep", self.time.sleep), mock.patch.object(clock, "SCALE", 1.0),
                      mock.patch.dict(sys.modules, {"report": report}), mock.patch.object(sys, "stderr", self.log)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        if self.fake:   # a correct bot never makes a call that is not one of SPEC section 6
            self.assertEqual([r["strict"] for r in self.requests() if r["strict"]], [])

    # ---- building a bot

    def make_cfg(self, reels=None, **changes):
        raw = {**EXAMPLE, "sending": True, "alerts": {"repeat_min": 120, "digest_hour_utc": 23}, **changes}
        if reels is not None:
            raw["reels"] = [{**EXAMPLE["reels"][0], **reel} for reel in reels]
        self.config_path = os.path.join(self.dir, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as file:
            json.dump(raw, file)
        return config.load(self.config_path)

    def boot(self, comments=(), budget="unlimited", scenario=None, **cfg_changes):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeMeta({"account": ME, "token": TOKEN, "media": [media(comments=comments)],
                              "telegram": {"token": TG_TOKEN, "chat_id": TG_CHAT}, **(scenario or {})}).start()
        self.addCleanup(self.fake.stop)
        with open(os.path.join(self.dir, ".env"), "w", encoding="utf-8") as file:
            file.write(f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN={TG_TOKEN}\nTG_CHAT_ID={TG_CHAT}\n")
        self.cfg = self.make_cfg(**cfg_changes)
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.restart()
        if budget is not None:
            store.arm(self.conn, budget)   # after the start: one with a config the database has not seen disarms
        return self.engine

    def restart(self, **cfg_changes):
        """A new process run on the same state: nothing in memory survives."""
        if cfg_changes:
            self.cfg = self.make_cfg(**cfg_changes)
        env = os.path.join(self.dir, ".env")

        def get_tg():
            values = engine.read_env(env)
            return values.get("TG_BOT_TOKEN"), values.get("TG_CHAT_ID")
        self.notifier = notify.Notifier(self.conn, get_tg, self.fake.api_base, repeat_min=self.cfg.alerts.repeat_min)
        self.client = meta.Client(self.fake.api_base, self.cfg.api_version,
                                  lambda: store.kv_get(self.conn, "token") or "")
        self.engine = engine.Engine(self.cfg, self.conn, self.client, self.notifier, lambda: False)
        return self.engine

    def cycle(self, after=0):
        self.time.advance(after)
        return self.engine.cycle()

    def drain(self, limit=600):
        """Cycle, waiting out the send holds, until nobody is left pending or half-way."""
        for _ in range(limit):
            wait = max(self.engine.pause, 1)
            hold = store.get_send_hold(self.conn)
            if hold:
                wait = max(wait, (clock.parse(hold["until"]) - clock.now()).total_seconds() + 1)
            self.cycle(after=wait)
            if not self.conn.execute("SELECT 1 FROM comments WHERE status IN ('pending','sending','sent')").fetchone():
                return
        self.fail("the queue did not drain")

    def unwritable(self):
        """From now on every write to the bot's database fails (a full disk, a read-only file system, a
        foreign writer that keeps the lock); reads go on. Returns the function that ends it."""
        self.conn.execute("PRAGMA query_only=ON")
        heal = lambda: self.conn.execute("PRAGMA query_only=OFF")   # noqa: E731
        self.addCleanup(heal)
        return heal

    # ---- looking at the fake's truth and at the database

    def requests(self, *kinds, since=0):
        found = self.fake.control("/truth", query={"since": str(since)})["requests"]
        return [r for r in found if not kinds or r["kind"] in kinds]

    def posts(self, since=0):
        return [(r["kind"], r["target"]) for r in self.requests("dm", "public", since=since)]

    def seq(self):
        return self.fake.control("/truth")["seq"]

    def delivered(self):
        truth = self.fake.control("/truth")
        return [dm["comment_id"] for dm in truth["dms"]], [reply["comment_id"] for reply in truth["replies"]]

    def telegram(self):
        return [message["text"] for message in self.fake.control("/truth")["telegram"]]

    def fault(self, **spec):
        return self.fake.control("/faults", {"add": spec})

    def add(self, *comments, media_id=MEDIA):
        for one in comments:
            self.fake.control("/comments", {"add": {"media_id": media_id, **one}})

    def status(self, comment_id):
        row = store.comment(self.conn, comment_id)
        return row and (row["status"], row["reason"])

    def kv(self, key):
        return store.kv_get(self.conn, key)

    def attempts(self, columns="comment_id, kind, outcome, reason"):
        return [tuple(row) for row in self.conn.execute(f"SELECT {columns} FROM attempts ORDER BY id")]

    def problems(self):
        return dict(report.problems(self.conn, self.cfg, clock.now()))

    def announced(self):
        """Every key the bot ever queued for the alert channel."""
        return {row[0] for row in self.conn.execute("SELECT key FROM outbox")}


# ====================================================================================================
# 4.1 Ambiguous delivery, claims, retries
# ====================================================================================================

class AmbiguousDelivery(Case):
    def test_or01_or03_or04_or09_or15_restarts_and_full_sweeps_send_nothing_twice(self):
        """Every kind of answer, then two restarts whose first collect lists all the comments again."""
        self.boot([comment("code1", age=500), comment("html200", age=400), comment("http503", age=300),
                   comment("gone551", age=200), comment("fine", age=100)])
        self.fault(kind="dm", target="code1", http=400, code=1, message="An unknown error has occurred.", deliver=True)
        self.fault(kind="dm", target="html200", body="<html>200 OK</html>", deliver=True)
        self.fault(kind="dm", target="http503", http=503, body="<html>Service Unavailable</html>", deliver=False)
        self.fault(kind="dm", target="gone551", http=400, code=551, message="This person isn't available right now.",
                   deliver=False)
        self.drain()
        want = {"code1": ("uncertain", "code_1"), "html200": ("uncertain", "ok_without_id"),
                "http503": ("uncertain", "http_5xx"), "gone551": ("refused", "user_unavailable"), "fine": ("done", "")}
        self.assertEqual({name: self.status(name) for name in want}, want)
        for _ in range(2):
            self.restart()
            for _ in range(8):
                self.cycle(after=31 * MIN)   # every one of them a full sweep
        self.assertEqual({name: self.status(name) for name in want}, want)
        self.assertEqual(Counter(r["target"] for r in self.requests("dm")), Counter(list(want)))   # one POST each, ever
        self.assertEqual(sorted(self.delivered()[0]), ["code1", "fine", "html200"])
        self.assertEqual(self.delivered()[1], ["fine"])            # no "check your DMs" without a confirmed DM
        self.assertLessEqual({"uncertain", "hold:send"}, self.announced())
        self.assertEqual(report.build(self.conn, self.cfg, clock.now())["unaccounted"], 0)

    def test_or33_a_dm_in_doubt_keeps_its_place_under_the_hourly_cap(self):
        self.boot([comment(f"c{n}", age=300 - n) for n in range(3)], max_sends_per_hour=2)   # a slot of 30 min
        self.fault(kind="dm", target="c0", http=400, code=1, deliver=False)
        self.cycle()
        first = self.requests("dm")[0]["t"]
        for _ in range(11):
            self.cycle(after=5 * MIN)                               # up to 55 min after the first
        self.assertEqual([r["target"] for r in self.requests("dm")], ["c0", "c1"])
        self.cycle(after=4 * MIN)                                   # 59 min: the hour is still full
        self.assertEqual(len(self.requests("dm")), 2)
        self.cycle(after=3 * MIN)
        self.assertEqual(len(self.requests("dm")), 2)               # conservative clock/flight margin remains
        self.cycle(after=clock.T["cap_margin"] + MIN)
        times = [r["t"] for r in self.requests("dm")]
        self.assertEqual(len(times), 3)
        self.assertGreaterEqual(times[2] - first, HOUR + clock.T["cap_margin"])

    def test_or05_meta_said_ok_but_neither_the_answer_nor_the_failure_could_be_written(self):
        self.boot([comment("c1", age=200), comment("c2", age=100)])
        real, healed = store.record_dm, []

        def disk_full_after_the_post(conn, attempt_id, result):
            healed.append(self.unwritable())
            return real(conn, attempt_id, result)
        with mock.patch.object(store, "record_dm", disk_full_after_the_post):
            self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.delivered()[0], ["c1"])               # Meta did deliver it
        self.assertEqual(self.status("c1"), ("sending", ""))        # the claim is all the database knows
        self.assertEqual(self.attempts(), [("c1", "dm", None, None)])
        healed[0]()                                                 # the database works again
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual(self.status("c1"), ("uncertain", "interrupted"))
        self.assertIsNotNone(store.get_send_hold(self.conn))
        self.drain()
        self.restart()
        for _ in range(4):
            self.cycle(after=31 * MIN)
        self.assertEqual([r["target"] for r in self.requests("dm")], ["c1", "c2"])
        self.assertEqual(self.delivered()[0], ["c1", "c2"])
        self.assertEqual(self.status("c1"), ("uncertain", "interrupted"))
        self.assertIn("uncertain", self.announced())
        self.assertEqual(report.build(self.conn, self.cfg, clock.now())["unaccounted"], 0)

    def test_or07_no_post_when_the_claim_cannot_be_stored(self):
        self.boot([comment("c1")])
        real, healed = store.claim_dm, []

        def disk_full_before_the_claim(*args):
            healed.append(self.unwritable())
            return real(*args)
        with mock.patch.object(store, "claim_dm", disk_full_before_the_claim):
            self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.posts(), [])
        healed[0]()
        self.assertEqual((self.status("c1"), self.attempts()), (("pending", ""), []))
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])

    def test_or12_invalid_for_a_private_reply_is_final_and_keeps_metas_words(self):
        words = "(#100) The comment is invalid for a private reply"
        self.boot([comment("c1", age=200), comment("c2", age=100)])
        self.fault(kind="dm", target="c1", http=400, code=100, message=words, deliver=False)
        self.drain()
        self.restart()
        for _ in range(3):
            self.cycle(after=31 * MIN)
        self.assertEqual(self.status("c1"), ("refused", "invalid"))
        self.assertEqual(self.attempts("comment_id, kind, http, code, outcome, reason, message")[0],
                         ("c1", "dm", 400, 100, "rejected", "invalid", words))
        self.assertEqual(self.posts(), [("dm", "c1"), ("dm", "c2"), ("public", "c2")])

    def test_or16_every_post_meta_received_has_its_own_journal_row(self):
        self.boot([comment(f"c{n}", age=600 - n) for n in range(6)])
        self.fault(kind="dm", target="c0", nth=1, http=400, code=613, message="Calls to this api have exceeded the "
                   "rate limit.", deliver=False)                    # asked twice: the second POST gets through
        self.fault(kind="dm", target="c1", http=400, code=1, deliver=True)
        self.fault(kind="dm", target="c2", http=400, code=100, subcode=2534014, deliver=False)
        self.fault(kind="dm", target="c3", drop="after")
        self.fault(kind="public", target="c4", http=500, deliver=True)
        self.drain()
        received = Counter((r["kind"], r["target"]) for r in self.requests("dm", "public"))
        journal = Counter((kind, comment_id) for comment_id, kind, outcome, _ in self.attempts() if outcome != "not_sent")
        self.assertEqual(journal, received)
        self.assertEqual(received["dm", "c0"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts WHERE finished_at IS NULL OR outcome IS NULL "
                                           "OR started_at IS NULL").fetchone()[0], 0)

    def test_or72_a_comment_that_breaks_the_send_step_neither_vanishes_nor_blocks_the_queue(self):
        self.boot([comment("bad", age=200), comment("good", age=100)])
        real = self.client.send_dm

        def send_dm(ig_user_id, comment_id, message):
            if comment_id == "bad":
                raise RuntimeError("a bug between the claim and the POST")
            return real(ig_user_id, comment_id, message)
        with mock.patch.object(self.client, "send_dm", send_dm):
            self.assertEqual(self.cycle(), 0)
            self.assertIn("cycle_error", self.problems())
            self.drain()
            for _ in range(3):
                self.cycle(after=31 * MIN)
        self.assertEqual(self.status("bad"), ("uncertain", "interrupted"))   # an explicit status, not a silent drop
        self.assertEqual(self.status("good"), ("done", ""))
        self.assertEqual(self.posts(), [("dm", "good"), ("public", "good")])
        self.assertLessEqual({"cycle_error", "uncertain"}, self.announced())


class NeverConnects(Case):
    """OR-02 as SPEC reads it: a POST that could not connect is `not_sent`, sets no hold and is tried again in
    the next cycle (8.5). G5 still holds: people who wait while nothing goes out must be announced."""



# ====================================================================================================
# 4.2 Public reply leg
# ====================================================================================================

class PublicReplyLeg(Case):
    def test_or21_public_reply_posted_but_its_journal_write_failed(self):
        self.boot([comment("c1")])
        real, healed = store.record_public, []

        def disk_full_after_the_post(conn, attempt_id, result):
            healed.append(self.unwritable())
            return real(conn, attempt_id, result)
        with mock.patch.object(store, "record_public", disk_full_after_the_post):
            self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.status("c1"), ("sent", ""))
        healed[0]()
        self.cycle(after=SLOT)
        self.restart()
        for _ in range(3):
            self.cycle(after=31 * MIN)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])   # neither leg a second time
        self.assertEqual(self.delivered(), (["c1"], ["c1"]))
        self.assertEqual(self.status("c1")[0], "done")
        self.assertEqual(report.build(self.conn, self.cfg, clock.now())["unaccounted"], 0)


# ====================================================================================================
# 4.3 Hourly cap
# ====================================================================================================

class HourlyCap(Case):
    def test_or35_a_viral_reel_drains_oldest_first_in_bounded_batches_under_the_cap(self):
        people = 130
        self.boot([comment(f"c{n:03}", age=9000 - n) for n in range(people)], max_sends_per_hour=120,
                  scenario={"limits": {"page_size": 50}})
        per_cycle = []
        for _ in range(4 * people):
            per_cycle.append(self.cycle(after=self.engine.pause))
            if sum(per_cycle) == people:
                break
        sent = self.requests("dm")
        times = [r["t"] for r in sent]
        self.assertEqual([r["target"] for r in sent], [f"c{n:03}" for n in range(people)])   # oldest first, once each
        self.assertLessEqual(max(per_cycle), 10)
        self.assertGreaterEqual(min(later - earlier for earlier, later in zip(times, times[1:])), 30)
        self.assertLessEqual(max(sum(1 for t in times if start <= t < start + HOUR) for start in times), 120)
        self.assertEqual(sorted(self.delivered()[0]), sorted(r["target"] for r in sent))
        self.assertEqual(report.build(self.conn, self.cfg, clock.now())["unaccounted"], 0)



# ====================================================================================================
# 4.4 Matching: who may be DMed
# ====================================================================================================

class Matching(Case):
    def test_or50_a_keyword_written_with_punctuation_is_the_word(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        cfg = self.make_cfg(reels=[{"keywords": ["арена", "link!"]}])
        matcher = match.Matcher(cfg.reels[0])
        for text in ("send link", "LINK!!!", "link"):
            self.assertEqual(matcher.check(text), ("match", ""), text)
        for text in ("I am linking to you", "links", "!"):
            self.assertEqual(matcher.check(text)[0], "none", text)


class PerPerson(Case):
    def test_or59_first_dm_in_doubt_then_the_person_asks_again(self):
        self.boot([comment("first", user="ann", age=600)])
        self.fault(kind="dm", target="first", http=400, code=1, deliver=True)
        self.cycle()
        self.add(comment("again", "арену", user="ann", age=0), comment("and_again", "arena", user="ann", age=0))
        self.restart()
        for _ in range(4):
            self.cycle(after=31 * MIN)
        self.assertEqual((self.status("again"), self.status("and_again")),
                         (("dup_user", "dup:first"), ("dup_user", "dup:first")))
        self.assertEqual(self.posts(), [("dm", "first")])
        self.assertEqual(self.delivered()[0], ["first"])



# ====================================================================================================
# 4.5 Polling and reconciliation
# ====================================================================================================

class OwnerReplies(Case):
    def test_or60_an_owner_reply_under_the_request_itself_is_ground_truth(self):
        """The guard that must survive any fix of the test below: the journal is lost (a new database), the
        account's reply under the request says the person was served."""
        self.boot([comment("served", user="ann", age=600, replies=[{"text": "Отправил в директ 📩", "from": OWNER}]),
                   comment("waits", user="bob", age=500)])
        self.drain()
        self.assertEqual(self.status("served"), ("already", "owner_replied"))
        self.assertEqual(self.posts(), [("dm", "waits"), ("public", "waits")])
        counts = report.build(self.conn, self.cfg, clock.now())["reels"][0]["counts"]
        self.assertIn(["already", "owner_replied", 1], counts)



class Reading(Case):
    def test_or63_or74_a_reel_whose_second_page_is_broken_is_alerted_and_the_other_reel_is_served(self):
        reels = [{"name": "arena", "media_id": MEDIA}, {"name": "second", "media_id": MEDIA_2}]
        self.boot(reels=reels, scenario={
            "media": [media(comments=[comment(f"a{n}", age=600 - n) for n in range(4)]),
                      media(MEDIA_2, [comment("b1", age=100)], "DSecond")], "limits": {"page_size": 2}})
        self.fault(kind="comments", target=MEDIA, nth=list(range(2, 100, 2)), http=400, code=1,
                   message="An unknown error has occurred.")        # generic code 1: one broken item on page 2
        for _ in range(3):
            self.cycle(after=SLOT)
        self.assertEqual(self.posts(), [("dm", "b1"), ("public", "b1")])   # reel 2 is served, reel 1 never sent to (G6)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments WHERE media_id=? AND status='pending'",
                                           (MEDIA,)).fetchone()[0], 2)     # page 1 was stored, not taken for "all"
        self.assertIn("reel:" + MEDIA, self.problems())
        self.assertTrue(any("reel:" + MEDIA in text for text in self.telegram()))

    def test_or64_empty_comment_lists_that_start_later_are_alerted_within_one_full_sweep(self):
        self.boot([comment("c1", age=600)], budget=None)
        self.cycle()
        self.assertEqual(self.kv("reel_state:" + MEDIA), "ok")
        self.fake.control("/mode", {"mode": "dev_mode"})            # 200 {"data": []} while comments_count stays
        self.add(comment("c2", age=0))
        for _ in range(21):
            self.cycle(after=90)                                    # 31.5 minutes
        self.assertEqual(self.kv("reel_state:" + MEDIA), "empty")
        self.assertIn("reel:" + MEDIA, self.problems())
        self.assertTrue(any("reel:" + MEDIA in text for text in self.telegram()))
        self.assertIsNone(self.status("c2"))                        # the empty list was not taken for "no comments"
        self.fake.control("/mode", {"mode": "ok"})
        self.cycle(after=90)
        self.assertEqual((self.kv("reel_state:" + MEDIA), self.status("c2")), ("ok", ("pending", "")))

    def test_or65_unsupported_request_is_the_accounts_failure_not_the_comments(self):
        words = "Unsupported request - method type: get"
        with self.subTest("every call"):
            self.boot([comment("c1", age=600)], budget=None)
            self.cycle()
            store.arm(self.conn, "unlimited")
            for kind in ("me", "media", "comments"):
                self.fault(kind=kind, http=400, code=100, message=words)
            for kind in ("dm", "public"):
                self.fault(kind=kind, http=400, code=100, message=words, deliver=False)
            before = self.seq()
            self.assertEqual(self.cycle(after=SLOT), 0)
            self.assertEqual([r["kind"] for r in self.requests(since=before) if r["kind"] != "telegram"], ["comments", "me"])
            self.assertEqual(store.get_account_hold(self.conn)["reason"], "blocked")
            self.assertEqual((self.status("c1"), store.comment(self.conn, "c1")["deferrals"]), (("pending", ""), 0))
            self.assertTrue(any("hold:account" in text for text in self.telegram()))
            before = self.seq()
            self.cycle(after=4 * MIN)
            self.assertEqual([r["kind"] for r in self.requests(since=before) if r["kind"] != "telegram"], [])   # halted
        with self.subTest("the send alone"):
            self.boot([comment("c1", age=600), comment("c2", age=500)])
            self.fault(kind="dm", http=400, code=100, message="Unsupported request - method type: post", deliver=False)
            self.assertEqual(self.cycle(), 0)
            self.assertEqual(self.status("c1"), ("pending", ""))    # nobody is written off for the account's trouble
            self.assertEqual(store.get_send_hold(self.conn)["reason"], "send_blocked")
            self.assertEqual(self.cycle(after=10 * MIN), 0)
            self.assertEqual(self.posts(), [("dm", "c1")])

    def test_or74_a_cursor_that_repeats_ends_in_an_alert_not_in_an_endless_sweep(self):
        self.boot([comment("c1", age=600)])
        again = (f"{self.fake.api_base}/v26.0/{MEDIA}/comments?fields=id%2Ctext%2Ctimestamp%2Cfrom%2Chidden%2C"
                 f"replies%7Bid%2Cfrom%7D&limit=50&access_token={TOKEN}")
        self.fault(kind="comments", body={
            "data": [{"id": "c1", "text": "арена", "timestamp": "2026-10-05T11:50:00+0000",
                      "from": {"id": "u_c1", "username": "name_u_c1"}}],
            "paging": {"cursors": {"after": "QVFIUsame"}, "next": again}})
        self.assertEqual(self.cycle(), 0)
        self.assertLessEqual(len(self.requests("comments")), 400)   # SPEC 8.3: at most 400 pages
        self.assertEqual(self.posts(), [])                          # no completed sweep, no DM (G6)
        self.assertIn("reel:" + MEDIA, self.problems())
        self.assertNotEqual(self.kv("reel_state:" + MEDIA), "ok")
        self.assertNotIn(TOKEN, self.log.getvalue())                # the repeated `next` URL carries the token

    def test_or77_the_token_meta_echoes_reaches_no_log_journal_alert_or_report(self):
        echo = f"Malformed access token {TOKEN}"
        self.boot([comment("c1", age=600), comment("c2", age=500)])
        self.fault(kind="dm", target="c1", http=400, code=190, message=echo, deliver=False)
        self.fault(kind="me", nth=2, http=400, code=190, message=echo)
        self.cycle()                                                # refused POST, refused probe: both holds
        self.fault(kind="comments", http=400, code=190, message=echo)
        self.fault(kind="me", http=400, code=190, message=echo)
        self.fault(kind="refresh", http=400, code=190, message=echo, deliver=False)
        for _ in range(3):
            self.cycle(after=10 * MIN)
        self.assertIsNotNone(store.get_account_hold(self.conn))
        kv = {key: value for key, value in self.conn.execute("SELECT key, value FROM kv")
              if key not in ("token", "token_env_seen")}
        places = {"log": self.log.getvalue(), "telegram": "\n".join(self.telegram()),
                  "kv": json.dumps(kv, ensure_ascii=False),
                  "attempts": repr(self.attempts("message")),
                  "outbox": repr(self.conn.execute("SELECT text FROM outbox").fetchall()),
                  "problems": repr(self.conn.execute("SELECT text FROM problems").fetchall()),
                  "report": json.dumps(report.build(self.conn, self.cfg, clock.now()), ensure_ascii=False)}
        self.assertEqual([name for name, text in places.items() if TOKEN in text or TG_TOKEN in text], [])
        self.assertIn("code=190", places["log"])                    # and the error itself is there to read


class Liveness(Case):
    """OR-69 / lesson L21: a process that is alive and feeds its watchdog while it serves nobody."""



# ====================================================================================================
# All of it at once: random walks with a referee that reads the fake's truth
# ====================================================================================================

class _Kill(BaseException):
    """kill -9: nothing after this point runs."""


class RandomWalks(Case):
    """Comments by new and returning people, small talk, owner replies under requests, deletes, hides, every
    class of answer to a DM and to a public reply, blocking modes, kills, restarts, downtime,
    `arm`/`disarm`. Then the faults end and the bot is given time. The referee checks, from what the fake
    received and delivered: one DM per person (G1), one answered POST per comment unless the answer was
    account-wide (G4), the slot (G2), statuses against deliveries, public replies only after a delivered DM,
    nothing left half-way, `unaccounted` 0, and that every person who asked either got the DM or has a row
    that says why not.

    Deliberately left out, because the findings above are about them: owner replies under comments that are
    not requests, and steps of the wall clock."""

    REQUESTS = ("арена", "Арена", "арену", "ARENA", "arena", "хочу арену", "арена пожалуйста 🔥", "арена?", "аренааа")
    CHATTER = ("круто", "🔥🔥🔥", "спасибо!", "это платно?", "зачем арена?", "как получить арену", "класс 👍")
    ACCOUNT_WIDE = (190, 613, 4, 10, 200, 368)

    def test_thirty_walks(self):
        import random
        for seed in range(30):
            with self.subTest(seed=seed):
                self.rng = random.Random(seed)
                self.walk()
                self.assertEqual(self.referee(), [])
            self.fake.stop()
            self.conn.close()
            self.fake = None

    # ---- the walk

    def make(self, request, person=None, age=0):
        self.count += 1
        if person is None:
            person = f"p{self.count}"
            self.people.append(person)
        comment_id = f"k{self.count:04}"
        self.said[comment_id] = {"user": person, "request": request, "deleted": False, "owner": False}
        return comment(comment_id, self.rng.choice(self.REQUESTS if request else self.CHATTER), user=person, age=age,
                       should_match=request)

    def pick(self, request=None):
        ids = [comment_id for comment_id, one in self.said.items()
               if not one["deleted"] and (request is None or one["request"] == request)]
        return self.rng.choice(ids) if ids else None

    def walk(self):
        rng = self.rng
        self.people, self.said, self.count = [], {}, 0
        self.log.seek(0)
        self.log.truncate()
        first = [self.make(rng.random() < 0.7, age=rng.randint(60, 3 * DAY)) for _ in range(rng.randint(2, 8))]
        self.boot(first, budget="unlimited" if rng.random() < 0.7 else rng.randint(1, 5),
                  scenario={"limits": {"page_size": rng.randint(2, 6)},
                            "already_replied": {"spelling": rng.choice(["10900", "2534025", "-1"]), "deliver": False}},
                  max_sends_per_hour=rng.choice([60, 120, 300]), public_reply=rng.random() < 0.85)
        steps = {"ask": 30, "ask_again": 12, "chat": 10, "owner": 6, "delete": 2, "hide": 2, "fault_dm": 9,
                 "fault_public": 4, "restart": 4, "kill_in_dm": 3, "kill_before_public": 3,
                 "arm": 3, "mode": 3, "down": 2, "cycle": 45}
        for _ in range(rng.randint(15, 60)):
            getattr(self, "do_" + rng.choices(list(steps), list(steps.values()))[0])()
        # the faults end, the gates open, the bot gets its time
        self.fake.control("/faults", {"clear": True})
        self.fake.control("/mode", {"mode": "ok"})
        self.restart()
        for turn in range(4000):
            if turn % 50 == 0:
                store.arm(self.conn, "unlimited")                   # a spam answer disarms
            wait = max(self.engine.pause, 30)
            for hold, until in ((store.get_send_hold(self.conn), "until"), (store.get_account_hold(self.conn), "next_probe_at")):
                if hold:
                    wait = max(wait, (clock.parse(hold[until]) - clock.now()).total_seconds() + 1)
            self.cycle(after=wait)
            if turn > 3 and not self.unfinished() and not store.get_send_hold(self.conn) \
                    and not store.get_account_hold(self.conn):
                self.cycle(after=31 * MIN)                          # one more full sweep
                if not self.unfinished():
                    return

    def unfinished(self):
        return self.conn.execute("SELECT COUNT(*) FROM comments WHERE status IN ('pending','sending','sent')").fetchone()[0]

    def do_ask(self):
        self.add(self.make(True))

    def do_ask_again(self):
        if self.people:
            self.add(self.make(True, self.rng.choice(self.people)))

    def do_chat(self):
        self.add(self.make(False, self.rng.choice(self.people) if self.people and self.rng.random() < 0.6 else None))

    def do_owner(self):
        comment_id = self.pick(request=True)
        if comment_id:
            self.fake.control("/comments", {"owner_reply": comment_id, "text": "готово"})
            self.said[comment_id]["owner"] = True

    def do_delete(self):
        comment_id = self.pick()
        if comment_id:
            self.fake.control("/comments", {"delete": comment_id})
            self.said[comment_id]["deleted"] = True

    def do_hide(self):
        comment_id = self.pick()
        if comment_id:
            self.fake.control("/comments", {"hide": comment_id})

    def do_fault_dm(self):
        comment_id, rng = self.pick(request=True), self.rng
        if not comment_id:
            return
        kind = rng.choice(["doubt", "doubt", "item", "account", "account"])
        if kind == "doubt":
            spec = rng.choice([{"http": 400, "code": 1}, {"http": 500}, {"omit_id": True}, {"http": 400, "code": 2},
                               {"drop": "after"}, {"drop": "before"}, {"http": 400, "body": "oops"}])
            spec = spec if "drop" in spec else {**spec, "deliver": rng.random() < 0.5}
        elif kind == "item":
            spec = {"http": 400, "deliver": False, **rng.choice([
                {"code": 551}, {"code": 100, "subcode": 2534014}, {"code": 10, "subcode": 2534022},
                {"code": 100, "subcode": 2534015, "consume": True}])}
        else:
            spec = {"deliver": False, **rng.choice([{"http": 400, "code": 190}, {"http": 400, "code": 613},
                                                    {"http": 429, "body": "slow down"}, {"http": 400, "code": 10}])}
        self.fault(kind="dm", target=comment_id, nth=rng.choice([1, 1, [1, 2]]), **spec)

    def do_fault_public(self):
        comment_id = self.pick(request=True)
        if comment_id:
            self.fault(kind="public", target=comment_id, nth=1, **self.rng.choice([
                {"http": 500, "deliver": True}, {"http": 500, "deliver": False}, {"drop": "after"},
                {"http": 400, "code": 100, "subcode": 33, "deliver": False}, {"http": 400, "code": 190, "deliver": False}]))

    def do_restart(self):
        self.restart()

    def do_kill_in_dm(self):
        real = self.client.send_dm

        def answered_then_killed(*args):
            real(*args)
            raise _Kill()
        with mock.patch.object(self.client, "send_dm", answered_then_killed), contextlib.suppress(_Kill):
            self.cycle(after=self.rng.randint(30, 200))
        self.restart()

    def do_kill_before_public(self):
        with mock.patch.object(clock, "sleep", side_effect=_Kill), contextlib.suppress(_Kill):
            self.cycle(after=self.rng.randint(30, 200))
        self.restart()

    def do_arm(self):
        store.arm(self.conn, self.rng.choice([0, 1, 3, "unlimited"]))

    def do_mode(self):
        self.fake.control("/mode", {"mode": self.rng.choice(["blocked", "token_dead", "throttled",
                                                              "messaging_blocked", "dev_mode"])})
        for _ in range(self.rng.randint(1, 6)):
            self.cycle(after=self.rng.randint(60, 1200))
        self.fake.control("/mode", {"mode": "ok"})

    def do_down(self):
        self.time.advance(self.rng.choice([HOUR, 5 * HOUR, 26 * HOUR]))
        self.restart()

    def do_cycle(self):
        self.cycle(after=self.rng.choice([30, 60, 90, 90, 200, 600, 1900]))

    # ---- the referee

    def referee(self):
        found = []
        truth = self.fake.control("/truth")
        listed = {one["id"]: one for one in self.fake.control("/comments")["comments"]}
        rows = {row["comment_id"]: row for row in self.conn.execute("SELECT * FROM comments")}
        found += [("strict", r["strict"]) for r in truth["requests"] if r["strict"]]
        dms = Counter(dm["comment_id"] for dm in truth["dms"])
        per_person = Counter(dm["user_id"] for dm in truth["dms"])
        found += [("G1", person, count) for person, count in per_person.items() if count > 1]
        answered = Counter()
        for r in truth["requests"]:
            if r["kind"] != "dm":
                continue
            error = r["response"].get("error", {}) if isinstance(r["response"], dict) else {}
            account_wide = r["status"] == 429 or error.get("code") in self.ACCOUNT_WIDE and not (
                error.get("code") == 10 and error.get("error_subcode")) or error.get("error_subcode") == 2534029
            answered[r["target"]] += not account_wide
        found += [("G4", comment_id, count) for comment_id, count in answered.items() if count > 1]
        times = [r["t"] for r in truth["requests"] if r["kind"] == "dm" and r["status"] is not None]
        slot = 3600 / self.cfg.max_sends_per_hour
        found += [("G2", round(later - earlier, 3)) for earlier, later in zip(times, times[1:]) if later - earlier < slot - 0.01]
        for comment_id, row in rows.items():
            status, got = row["status"], dms.get(comment_id, 0)
            if (status in ("sent", "done") and got != 1) or (status in ("refused", "expired", "dup_user", "seen") and got):
                found.append(("status against delivery", comment_id, status, got))
            if status in ("pending", "sending", "sent"):
                found.append(("left half-way", comment_id, status))
        found += [("delivered without a row that says so", comment_id) for comment_id in dms
                  if rows[comment_id]["status"] not in ("sent", "done", "uncertain", "already")]
        replies = Counter(reply["comment_id"] for reply in truth["replies"])
        found += [("public reply", comment_id, count) for comment_id, count in replies.items()
                  if count > 1 or not dms.get(comment_id)]
        found += [("no row", comment_id) for comment_id, one in listed.items()
                  if one["first_listed_seq"] is not None and comment_id not in rows]
        unaccounted = report.build(self.conn, self.cfg, clock.now())["unaccounted_parts"]
        found += [("unaccounted", unaccounted)] if any(unaccounted.values()) else []
        logged = self.log.getvalue()
        found += [("the log", word) for word in ("cycle_error", "Traceback", "notify_failed", TOKEN, TG_TOKEN) if word in logged]
        # everybody who asked got the DM, or one of their requests says why not
        asked = {}
        for comment_id, one in self.said.items():
            if one["request"]:
                asked.setdefault(one["user"], []).append(comment_id)
        for person, comment_ids in asked.items():
            explained = bool(per_person.get(person))
            for comment_id in comment_ids:
                row = rows.get(comment_id)
                if row is None:
                    explained = explained or self.said[comment_id]["deleted"]
                elif row["status"] in ("refused", "uncertain", "expired"):
                    explained = True
                elif row["status"] == "already" and (row["reason"] == "already_replied" or self.said[comment_id]["owner"]):
                    explained = True
            if not explained:
                found.append(("asked and got nothing, and no row says why", person,
                              {comment_id: tuple(rows[comment_id])[6:8] for comment_id in comment_ids if comment_id in rows}))
        return found


if __name__ == "__main__":
    unittest.main()
