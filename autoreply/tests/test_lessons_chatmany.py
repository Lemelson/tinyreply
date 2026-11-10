"""Attack round 1: the scenario catalogue of research/lessons/chatmany-and-small.md (chatmany, ig-harness, insta-p8).

Every scenario of section 4 of that file (A1..A11, B1..B10, C1..C8, D1..D8, E1..E11, F1..F15, T1..T9: 72 in all)
was sorted into one of: not applicable to this core, already covered by an existing test or soak scenario, or
not covered. This module holds one test for every scenario that was not covered. The scenario id is in the test
name; the docstring says what the lesson was and which words of SPEC.md the test stands on.

Every test runs the REAL engine, the REAL report.problems and the REAL notifier against the in-process fake
Meta + Telegram of sim/ on 127.0.0.1. Time is in the test's hands (the pattern of tests/test_engine.py): wall
time is the real clock plus an offset that the bot and the fake both read; clock.sleep() moves the offset. The
bot is judged by what the fake received (its ground truth), by the database and by what Telegram got.

Classes named `Held...` passed on the code of that day: what withstood the attack. Every other class was a
finding (B8/D2, L3, F2, E11): its tests failed then, were confirmed and fixed, and live in
tests/test_regress_r1.py (classes ChatMany*), which use the helpers kept in the classes here.

    cd autoreply && python3.12 -B -m unittest tests.test_lessons_chatmany
"""
import fcntl
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import bot
import clock
import config
import engine
import match
import meta
import notify
import report
import store
from sim.fake_meta import FakeMeta

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN, HOUR, DAY = 60, 3600, 86400
START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
SLOT = 60   # seconds between two DMs at 60 per hour
ME = {"user_id": "17841400000000001", "id": "9990000000000001", "username": "your_account",
      "followers_count": 1234, "media_count": 7}
MEDIA, MEDIA_2 = "17900000000000101", "18000000000000002"
TOKEN = "IGAAchatmanylessons1"
TG = {"token": "123456:chatmany", "chat_id": 42}
ENV = f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN={TG['token']}\nTG_CHAT_ID={TG['chat_id']}\n"
with open(os.path.join(CODE, "config.example.json"), encoding="utf-8") as _file:
    EXAMPLE = json.load(_file)
SECOND_REEL = {"name": "second", "media_id": MEDIA_2}


def comment(comment_id, text="арена", user=None, age=60, **more):
    """A scenario comment: by default a fresh request by a person of its own."""
    user = user or "u_" + comment_id
    return {"id": comment_id, "text": text, "age_sec": age, "from": {"id": user, "username": "name_" + user},
            "should_match": True, **more}


def media(media_id=MEDIA, comments=(), shortcode="DAbc"):
    return {"id": media_id, "permalink": f"https://www.instagram.com/reel/{shortcode}/", "shortcode": shortcode,
            "comments": list(comments)}


class Case(unittest.TestCase):
    """One bot (engine + report + notifier) on a fresh state directory, one fake Meta + Telegram, one clock."""

    def setUp(self):
        self._time, self._monotonic = time.time, time.monotonic
        self._began, self.wall, self.mono = time.time(), 0.0, 0.0
        self.log = io.StringIO()
        for patch in (mock.patch("time.time", self.time), mock.patch.object(clock, "monotonic", self.monotonic),
                      mock.patch.object(clock, "sleep", self.sleep), mock.patch.object(clock, "SCALE", 1.0),
                      mock.patch.object(sys, "stderr", self.log)):
            patch.start()
            self.addCleanup(patch.stop)
        self.fake = None

    def tearDown(self):
        if self.fake:   # a correct bot never makes a call that is not one of SPEC section 6
            self.assertEqual([r["strict"] for r in self.fake.control("/truth")["requests"] if r["strict"]], [])

    # ---- time

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

    def hours(self, unix):
        return (unix - START) / HOUR

    # ---- the bot

    def world(self, comments=(), budget="unlimited", env=ENV, scenario=None, **cfg_changes):
        self.dir = tempfile.mkdtemp(prefix="chatmany-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeMeta({"account": ME, "token": TOKEN, "telegram": TG, "media": [media(comments=comments)],
                              **(scenario or {})}).start()
        self.addCleanup(self.fake.stop)
        self.write_env(env)
        self.open_db()
        store.init(self.conn)
        # the instance lock of SPEC 3, as bot.py holds it: `report` then sees a running service
        self.lock = open(os.path.join(self.dir, "core.lock"), "a")
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.start(**cfg_changes)
        if budget is not None:
            store.arm(self.conn, budget)   # after the start: one with a config the database has not seen disarms
        return self.engine

    def open_db(self):
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        self.addCleanup(self.conn.close)

    def write_env(self, text):
        with open(os.path.join(self.dir, ".env"), "w", encoding="utf-8") as file:
            file.write(text)

    def start(self, reels=None, **cfg_changes):
        """A new process run on the same state (nothing in memory survives), with this config."""
        raw = {**EXAMPLE, "sending": True, **cfg_changes}
        if reels is not None:
            raw["reels"] = [{**EXAMPLE["reels"][0], **reel} for reel in reels]
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(raw, file)
        self.cfg = config.load(path)
        self.notifier = notify.Notifier(self.conn, bot.telegram(self.dir), self.fake.api_base,
                                        repeat_min=self.cfg.alerts.repeat_min)
        self.client = meta.Client(self.fake.api_base, self.cfg.api_version,
                                  lambda: store.kv_get(self.conn, "token") or "")
        self.engine = engine.Engine(self.cfg, self.conn, self.client, self.notifier, lambda: False)
        return self.engine

    def cycle(self, after=0):
        self.advance(after)
        return self.engine.cycle()

    def run_for(self, seconds):
        """What Engine.run() does for this long: a cycle, then the pause the cycle asked for. Returns the cycles."""
        end, cycles = self.time() + seconds, 0
        while self.time() < end:
            self.engine.cycle()
            cycles += 1
            self.advance(self.engine.pause)
        return cycles

    # ---- the fake

    def truth(self):
        return self.fake.control("/truth")

    def add(self, *comments, media_id=MEDIA):
        for one in comments:
            self.fake.control("/comments", {"add": {"media_id": media_id, **one}})

    def fault(self, **spec):
        return self.fake.control("/faults", {"add": spec})

    def mode(self, name):
        self.fake.control("/mode", {"mode": name})

    def requests(self, *kinds, since=0):
        found = self.fake.control("/truth", query={"since": str(since)})["requests"]
        return [request for request in found if not kinds or request["kind"] in kinds]

    def seq(self):
        return self.truth()["seq"]

    def dms(self):
        """Ground truth: the comment ids whose DM was really delivered, in order."""
        return [dm["comment_id"] for dm in self.truth()["dms"]]

    def dms_to(self, user):
        return [dm["comment_id"] for dm in self.truth()["dms"] if dm["user_id"] == user]

    def posts(self, since=0):
        return [(r["kind"], r["target"]) for r in self.requests("dm", "public", since=since)]

    # ---- what the owner can see

    def telegram(self, since=0):
        return [line for message in self.truth()["telegram"][since:] for line in message["text"].split("\n")]

    def told(self, key, since=0):
        """The Telegram lines that announce the problem or the event `key`."""
        return [line for line in self.telegram(since) if line.startswith((f"⚠ {key}:", f"ℹ {key}:", f"ℹ {key}"))]

    def problems(self):
        return dict(report.problems(self.conn, self.cfg, clock.now()))

    def build(self):
        return report.build(self.conn, self.cfg, clock.now())

    def status(self, comment_id):
        row = store.comment(self.conn, comment_id)
        return row and (row["status"], row["reason"])

    def statuses(self):
        return {row[0]: (row[1], row[2]) for row in self.conn.execute(
            "SELECT comment_id, status, reason FROM comments ORDER BY comment_id")}

    def kv(self, key):
        return store.kv_get(self.conn, key)

    def attempts(self, columns="comment_id, kind, outcome, reason"):
        return [tuple(row) for row in self.conn.execute(f"SELECT {columns} FROM attempts ORDER BY id")]

    def served(self, prefix):
        return sorted(comment_id for comment_id in self.dms() if comment_id.startswith(prefix))

    def hours_until(self, done, limit):
        """Run the loop until done() is true; returns the hours that took, or None if `limit` seconds passed."""
        began = self.time()
        while self.time() < began + limit:
            self.engine.cycle()
            if done():
                return (self.time() - began) / HOUR
            self.advance(self.engine.pause)
        return None


# =====================================================================================================
# The findings of round 1: the helpers of their tests (the tests are in tests/test_regress_r1.py)
# =====================================================================================================

class AlreadyAnsweredPeopleStopTheQueue(Case):
    """Catalogue B8 ("state store empty or restored from an old backup; reel has many old keyword comments
    already answered ... Meta's "already replied" answer ends each as terminal") and D2 ("terminal status
    "already replied", zero further attempts, no hot loop"); lesson L18/L19: one kind of item at the head of
    the queue must not starve everybody behind it.

    SPEC 7.3 makes `rejected/already_replied` the status `already`: "handled outside this database" (7.1). It
    is Meta's ground truth that the person HAS the private reply, the one good answer besides `ok`.
    SPEC 8.5 counts it as a failure all the same: "5 DM attempts in a row without `ok` ... `streak` 1 h x
    2^(n-5), max 12 h". SPEC 8.4 sends "oldest comment first", so the people who were answered before are
    exactly the head of the queue, one after another. The two rules together turn "Meta says these people
    are served" into a stop of hours or days for everybody who is really waiting: after the fifth such
    answer 1 h, then 2 h, 4 h, 8 h, then 12 h for EACH further one.

    Guarantee "spec": SPEC 8.5 itself asks for the hold; nothing in G1..G6 is broken and the hold is
    announced. What breaks is the purpose of B8/D2: the people behind wait for days and, with enough
    already-answered comments, past their window. A fix that keeps G1..G6: an `already_replied` answer does
    not count for the streak.
    """




class ServedPeopleAreCountedAsWaiting(Case):
    """Lesson L3 (chatmany, production 2026-08): "38 person/campaign pairs carried doubled events although only
    one DM went out ... 'Skipped as already claimed' must never be counted". Catalogue C7/B3.

    SPEC 9: the problem `waiting` says "how many wait", the report shows per reel "people waiting", `arm`
    prints "people waiting" (8.6). SPEC 7.2 step 2: a person who already has a handled comment under the reel
    gets no second DM; the later comment only becomes `dup_user` when the claim step reaches it. Behind a
    closed gate (never armed, disarmed, sending off: the state of the cut-over in SPEC 12) the claim step stops
    at the first person who really waits, so every later comment of somebody who HAS the DM stays `pending`
    and is counted as a person waiting: in the alert, in the report, in the forecast of `arm`.
    """



class APollInWhichEveryCommentFailsReadsHealthy(Case):
    """Lesson L20 (chatmany commit e04774e): after isolating each comment, a poll in which EVERY comment fails
    returns normally: "the dashboard would read healthy while no lead had been answered for hours". Catalogue
    F2: "every comment in the poll raises -> health shows the error and count; not 'healthy'".

    SPEC 8.3 stores a comment it cannot handle as `seen`/`malformed` and counts it "in the sweep note"; the
    sweep is ok. G5: "every state in which people are waiting or being lost is announced and re-announced until
    it ends"; SPEC 9: problems() "is the ONE definition of something is wrong".
    (attacks/round1/test_meta_reality.py reaches the same gap with single odd comments; this is the whole poll.)
    """



class APauseSwitchLeftOn(Case):
    """Catalogue E11 ("pause switch left on | hours pass | periodic reminder alert while paused, backlog count
    visible"), the owner's own outage "a gate left closed". SPEC has three pause switches; two are announced
    (`sending: false`, budget 0). The third is `reel.active=false`.

    SPEC 9: "`waiting`: rows in `pending` ... exist and there has been no `ok` DM for 30 min, for any reason
    except `cap`"; "`expiring`: N pending rows expire within 24 h"; G5. SPEC 4 says the rows of an inactive
    reel "are reported as parked" and 9 only takes them out of `unaccounted`.
    (Same gap as attacks/round1/test_silence.py SwitchedOffReel; here with every other gate open.)
    """



# =====================================================================================================
# HELD: scenarios of the catalogue that no existing test covered and that the product withstood
# =====================================================================================================

class HeldMatcher(unittest.TestCase):
    def test_a5_a_50000_character_comment_is_decided_in_well_under_a_second(self):
        """A5 (chatmany adversarial.test.ts:231): a huge comment must not stall the tick."""
        import types
        matcher = match.Matcher(types.SimpleNamespace(**EXAMPLE["reels"][0]))
        texts = {"о" * 50_000 + " арена": "match",                        # one stretched letter and the keyword
                 "слово " * 8_000 + "арена": "near",                      # 8,000 words and the keyword
                 "🔥" * 25_000 + "арена": "match",
                 "а" * 50_000: "none"}
        began = time.perf_counter()
        kinds = {text: matcher.check(text)[0] for text in texts}
        self.assertLess(time.perf_counter() - began, 1.0)
        self.assertEqual(kinds, texts)


class HeldMatching(Case):
    def test_a6_a_keyword_in_a_script_without_spaces_matches_alone_and_the_miss_is_visible(self):
        """A6 (chatmany DEFECT adversarial.test.ts:215): a whole-word rule never matches inside a sentence
        written without spaces. Here it is decided (tokens are runs of letters, SPEC 5) and visible: the
        sentence is in the report's list of frequent short unmatched texts."""
        sentence = "请给我链接谢谢"
        self.world([comment("alone", "链接", age=3 * MIN), comment("s1", sentence, age=2 * MIN),
                    comment("s2", sentence, age=MIN)], reels=[{"keywords": ["链接"]}])
        self.run_for(5 * MIN)
        self.assertEqual(self.dms(), ["alone"])
        self.assertEqual((self.status("s1"), self.status("s2")), (("seen", ""), ("seen", "")))
        self.assertIn([" ".join(match.normalize(sentence)), 2], self.build()["unmatched"])

    def test_a8_the_keyword_in_a_reply_under_somebody_elses_comment_is_not_a_request(self):
        """A8: only top-level comments are requests (SPEC 1); a nested reply is neither sent to nor stored."""
        self.world([comment("top", "красиво", replies=[{"text": "арена", "from": {"id": "u9", "username": "guest"}}])])
        self.run_for(35 * MIN)                            # a head poll and two full sweeps
        self.assertEqual(self.statuses(), {"top": ("seen", "")})
        self.assertEqual(self.posts(), [])

    def test_a10_a_comment_without_text_is_stored_and_the_others_are_served(self):
        """A10: a comment with a missing or an empty text (a sticker, a GIF) is no crash and no request."""
        self.world([comment("no_text", age=3 * MIN, raw={"text": None}), comment("empty", "", age=2 * MIN),
                    comment("good", age=MIN)])
        self.run_for(3 * MIN)
        self.assertEqual(self.statuses(), {"no_text": ("seen", ""), "empty": ("seen", ""), "good": ("done", "")})
        self.assertEqual(self.problems(), {})


class HeldOnePersonOneDm(Case):
    def test_a11_nobody_is_lost_when_the_pause_of_a_reel_is_lifted(self):
        """A11: a paused reel sends nothing and is not read; who asked before or during the pause is served
        when it is lifted (inside the window)."""
        self.world([comment("before", user="ann")], budget=None)
        self.cycle()
        self.start(reels=[{"active": False}])
        store.arm(self.conn, "unlimited")                 # after the start: a changed config disarms (SPEC 4)
        self.add(comment("during", user="bob"))
        mark = self.seq()
        self.run_for(HOUR)
        self.assertEqual(self.requests("media", "comments", "dm", "public", since=mark), [])
        self.assertEqual(self.statuses(), {"before": ("pending", "")})
        self.start()                                      # the pause is lifted
        store.arm(self.conn, "unlimited")
        self.run_for(5 * MIN)
        self.assertEqual(self.statuses(), {"before": ("done", ""), "during": ("done", "")})
        self.assertEqual((self.dms_to("ann"), self.dms_to("bob")), (["before"], ["during"]))

    def test_b5_one_dm_per_reel_for_a_person_who_asks_under_two_reels(self):
        """B5: the limit is per person AND reel; three comments under one reel are still one DM."""
        both = {"media": [media(comments=[comment("a1", user="ann", age=5 * MIN), comment("a2", user="ann", age=4 * MIN),
                                          comment("a3", "арену", user="ann", age=3 * MIN)]),
                          media(MEDIA_2, [comment("b1", user="ann", age=2 * MIN)], "DSecond")]}
        self.world(reels=[{}, SECOND_REEL], scenario=both)
        self.run_for(10 * MIN)
        self.assertEqual(sorted(self.dms_to("ann")), ["a1", "b1"])
        self.assertEqual(self.statuses(), {"a1": ("done", ""), "a2": ("dup_user", "dup:a1"),
                                           "a3": ("dup_user", "dup:a1"), "b1": ("done", "")})

    def test_b6_f6_two_hundred_people_after_a_long_stop_drain_under_the_cap_one_dm_each(self):
        """B6 + F6 (chatmany's 15-hour stall, L18: "bound work per tick regardless of staleness"): the bot comes
        back to 200 requests. The first cycle is bounded, then everybody gets exactly one DM, never closer
        than a slot and never more than the cap in a rolling hour, measured where Meta receives them."""
        self.world([comment(f"p{n:03}", age=HOUR + n) for n in range(200)])
        self.cycle()
        first = [r["kind"] for r in self.requests()]
        self.assertEqual(first.count("comments"), 4)                      # 200 comments, 50 a page
        self.assertLessEqual(first.count("dm"), engine.MAX_DMS_PER_CYCLE)
        self.advance(self.engine.pause)
        # Conservative restart-safe cap keeps a window longer than an hour. Four windows suffice
        # for 200 people at cap 60; the safety assertions still use the receiver's real rolling hour.
        self.run_for(4 * (HOUR + clock.T["cap_margin"]))
        truth = self.truth()
        times = [r["t"] for r in truth["requests"] if r["kind"] == "dm"]
        self.assertEqual(len(times), 200)
        self.assertEqual(sorted(dm["user_id"] for dm in truth["dms"]), sorted(f"u_p{n:03}" for n in range(200)))
        self.assertGreaterEqual(min(later - earlier for earlier, later in zip(times, times[1:])), SLOT)
        self.assertLessEqual(max(sum(1 for t in times if start <= t < start + HOUR) for start in times), 60)
        self.assertEqual(set(self.statuses().values()), {("done", "")})
        self.assertEqual(self.build()["unaccounted"], 0)
        self.assertEqual(self.problems(), {})

    def test_b9_a_reel_taken_out_of_the_config_and_put_back_does_not_forget_who_was_served(self):
        """B9 (chatmany "delete clears dedup history", Do-not-copy 10): rows are never pruned (SPEC 7)."""
        self.world([comment("a1", user="ann")])
        self.assertEqual(self.cycle(), 1)
        self.start(reels=[SECOND_REEL])                   # the reel is gone from the config (the other one is unknown to Meta)
        store.arm(self.conn, "unlimited")                 # a changed config disarms (SPEC 4): armed again each time
        self.add(comment("a2", user="ann"))
        self.run_for(10 * MIN)
        self.assertEqual(self.statuses(), {"a1": ("done", "")})
        self.start()                                      # and back again
        store.arm(self.conn, "unlimited")
        self.run_for(35 * MIN)
        self.assertEqual(self.dms_to("ann"), ["a1"])
        self.assertEqual(self.status("a2"), ("dup_user", "dup:a1"))

    def test_c4_a_dm_whose_answer_could_not_be_written_is_never_sent_again_and_is_announced(self):
        """C4 (chatmany L28, migration.test.ts:88): the person gets the DM, the record write throws. Here the
        claim was committed before the POST, so three more polls send nothing more, the comment ends
        `uncertain`, and both the failure and the doubtful DM reach Telegram."""
        self.world([comment("c1", age=2 * MIN), comment("c2", age=MIN)])
        real, calls = store.record_dm, []

        def disk_full_once(conn, attempt_id, result):
            calls.append(attempt_id)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database or disk is full")
            return real(conn, attempt_id, result)
        with mock.patch.object(store, "record_dm", disk_full_once):
            self.cycle()
            self.assertEqual((self.dms(), self.status("c1")), (["c1"], ("sending", "")))
            self.assertIn("cycle_error", self.problems())
            self.run_for(20 * MIN)
        self.assertEqual(self.dms(), ["c1", "c2"])        # c1 exactly once; c2 after the hold of 8.5
        self.assertEqual(self.status("c1"), ("uncertain", "interrupted"))
        self.assertEqual([target for kind, target in self.posts() if kind == "dm"], ["c1", "c2"])
        self.assertTrue(self.told("cycle_error") and self.told("uncertain"))
        self.assertEqual(self.build()["unaccounted"], 0)

    def test_d2_already_replied_is_final_and_keeps_the_persons_other_comment_out(self):
        """D2 (chatmany's hot loop, scenarios.test.ts:418: re-sent on every poll for ever)."""
        self.world([comment("a1", user="ann", age=2 * MIN, private_replied=True), comment("a2", user="ann", age=MIN)])
        for _ in range(6):
            self.cycle(after=20 * MIN)
        self.assertEqual(self.posts(), [("dm", "a1")])
        self.assertEqual(self.statuses(), {"a1": ("already", "already_replied"), "a2": ("dup_user", "dup:a1")})
        self.assertEqual(self.dms(), [])


class HeldBlockedApiIsNotHealthy(Case):
    def test_e6_a_comment_read_that_is_refused_while_the_account_answers_is_announced_after_three_polls(self):
        """E6 (chatmany Do-not-copy 6: a failed comment read is swallowed and the health error cleared in the
        same pass). Meta takes the comments permission away for one reel's reads: GET /me works, so there is
        no account hold; the reel is a problem after three polls and the other reel is served meanwhile."""
        both = {"media": [media(comments=[comment("a1")]), media(MEDIA_2, [comment("b1")], "DSecond")]}
        self.world(reels=[{}, SECOND_REEL], scenario=both)
        self.run_for(3 * MIN)
        self.fault(kind="comments", target=MEDIA, http=400, code=10,
                   message="(#10) Application does not have permission for this action")
        self.add(comment("a2"))
        self.add(comment("b2"), media_id=MEDIA_2)
        for _ in range(3):
            self.cycle(after=90)
        self.assertIsNone(store.get_account_hold(self.conn))
        self.assertIn("reel:" + MEDIA, self.problems())
        self.assertTrue(self.build()["verdict"].startswith("PROBLEM"))
        self.assertTrue(self.told("reel:" + MEDIA))
        self.run_for(10 * MIN)
        self.assertEqual(self.status("b2"), ("done", ""))            # the other reel never stopped
        self.assertIsNone(self.status("a2"))                          # nobody was written off
        self.fake.control("/faults", {"clear": True})
        self.run_for(5 * MIN)
        self.assertEqual(self.status("a2"), ("done", ""))
        self.assertNotIn("reel:" + MEDIA, self.problems())

    def test_e7_the_missing_role_error_on_every_call_is_announced_with_metas_words(self):
        """E7 / L25 (chatmany DEPLOY-FIELD-NOTES:60): without the Instagram Tester role EVERY call answers
        code 100 "Unsupported request - method type: get". The alert comes in the same cycle and carries
        Meta's text; nobody is written off; when the role is back the bot goes on by itself."""
        self.world([comment("c1")])
        self.cycle()
        mark = len(self.truth()["telegram"])
        role = {"http": 400, "code": 100, "message": "Unsupported request - method type: get"}
        for kind in ("me", "media", "comments"):
            self.fault(kind=kind, **role)
        self.fault(kind="dm", http=400, code=100, message="Unsupported request - method type: post", deliver=False)
        self.add(comment("c2"))
        self.cycle(after=90)
        line = "\n".join(self.told("hold:account", mark))
        self.assertIn("Unsupported request", line)
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "blocked")
        self.run_for(HOUR)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])  # only the two POSTs of the first cycle
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments WHERE status IN ('refused','uncertain')")
                         .fetchone()[0], 0)
        self.fake.control("/faults", {"clear": True})
        self.run_for(35 * MIN)                            # the next probe lifts the hold
        self.assertEqual(self.status("c2"), ("done", ""))
        self.assertTrue([line for line in self.telegram(mark) if line == "✓ cleared: hold:account"])

    def test_e7_the_missing_role_error_on_the_send_alone_burns_nobody(self):
        """The same answer on POST /messages only, while reads work: the comment stays in the queue."""
        self.world([comment("c1"), comment("c2", age=30)])
        self.fault(kind="dm", http=400, code=100, message="Unsupported request - method type: post", deliver=False)
        self.run_for(3 * HOUR)
        self.assertEqual(set(self.statuses().values()), {("pending", "")})
        self.assertLessEqual(len(self.requests("dm")), 3)             # 30 min, 1 h, 2 h: it backs off
        self.assertTrue(self.told("hold:send"))
        self.assertEqual(self.dms(), [])

    def test_e8_empty_lists_that_begin_while_the_bot_runs_are_announced_at_the_next_full_sweep(self):
        """E8 / L24 (chatmany README:507: in Development mode Instagram "returns EMPTY lists ... with no error
        anywhere"). The head polls between two full sweeps cannot tell; the full sweep does, within 30 min,
        and the reel stays a problem until comments are listed again."""
        self.world([comment("c1")])
        self.cycle()
        self.mode("dev_mode")
        self.add(comment("c2"))
        took = self.hours_until(lambda: "reel:" + MEDIA in self.problems(), limit=2 * HOUR)
        self.assertIsNotNone(took)
        self.assertLess(took * 60, 35)
        self.assertEqual(self.kv("reel_state:" + MEDIA), "empty")
        self.assertTrue(self.told("reel:" + MEDIA))
        self.run_for(HOUR)
        self.assertIn("reel:" + MEDIA, self.problems())               # not cleared by anything meanwhile
        self.mode("ok")
        self.run_for(5 * MIN)
        self.assertEqual(self.status("c2"), ("done", ""))
        self.assertEqual(self.problems(), {})


class HeldPollRobustness(Case):
    def test_f3_a_clean_read_of_one_reel_does_not_clear_the_failure_of_another(self):
        """F3 (chatmany commit e04774e: "a clean message poll cannot clear a comment-poll error")."""
        both = {"media": [media(comments=[comment("a1")]), media(MEDIA_2, [comment("b1")], "DSecond")]}
        self.world(reels=[{}, SECOND_REEL], scenario=both)
        self.cycle()
        self.fault(kind="comments", target=MEDIA, http=500, code=2, message="An unexpected error has occurred.")
        seen = set()
        for _ in range(100):                              # two and a half hours of clean reads of the other reel
            self.cycle(after=90)
            seen.add("reel:" + MEDIA in self.problems())
            self.assertIsNone(self.kv("last_cycle_error"))
        self.assertEqual(self.kv("reel_fails:" + MEDIA_2), "0")
        self.assertGreaterEqual(int(self.kv("reel_fails:" + MEDIA)), 100)
        self.assertEqual(seen, {False, True})             # from the third failure on, and never cleared
        self.assertIn("reel:" + MEDIA, self.problems())
        self.assertGreaterEqual(len(self.told("reel:" + MEDIA)), 2)   # repeated after repeat_min

    def test_f4_f5_a_loop_whose_every_cycle_dies_is_pushed_and_the_report_calls_the_service_down(self):
        """F4/F5 (chatmany L21, L22: the heartbeat written at tick START stayed current during the stall).
        `last_cycle_ok_at` is written at the end of a good cycle only; the failure is pushed in every cycle's
        announcement; after 10 minutes the report says the service does not work."""
        self.world([comment("c1")], budget=None)
        self.cycle()
        stamp = self.kv("last_cycle_ok_at")
        mark = len(self.truth()["telegram"])
        with mock.patch.object(store, "expire", side_effect=RuntimeError("boom")):
            self.run_for(12 * MIN)
        self.assertEqual(self.kv("last_cycle_ok_at"), stamp)
        self.assertTrue(self.told("cycle_error", mark))
        self.assertIn("not_running", dict(self.build()["problems"]))
        self.run_for(3 * MIN)
        self.assertNotIn("not_running", dict(self.build()["problems"]))
        self.assertNotIn("cycle_error", self.problems())

    def test_f7_a_burst_whose_second_page_fails_is_complete_after_the_next_full_sweep(self):
        """F7 (chatmany Do-not-copy 7: one page of 50, the older comments of a burst are never seen). Seven
        people comment between two polls, the page size is three, and the second page of that poll fails."""
        self.world([comment(f"o{n}", age=20 * MIN + n) for n in range(3)], scenario={"limits": {"page_size": 3}})
        self.run_for(5 * MIN)
        self.add(*[comment(f"n{n}", age=7 - n) for n in range(7)])
        self.fault(kind="comments", nth=2, http=500, code=2, message="An unexpected error has occurred.")
        mark = self.seq()
        self.cycle(after=90)
        self.assertEqual([r["status"] for r in self.requests("comments", since=mark)], [200, 500])
        self.assertEqual(self.requests("dm", since=mark), [])         # G6: this collect did not succeed
        self.assertEqual(sorted(self.statuses()), ["n4", "n5", "n6", "o0", "o1", "o2"])
        self.run_for(35 * MIN)                            # the next full sweep is at most 30 minutes away
        self.assertEqual(self.served("n"), [f"n{n}" for n in range(7)])
        self.assertEqual(self.build()["unaccounted"], 0)


class HeldToken(Case):
    def test_t3_a_refresh_answer_without_a_token_leaves_the_stored_token_and_is_a_problem_after_two(self):
        """T3 / L38 (ig-harness ig-token.ts:54: "a refresh response can be 200 with a useless payload")."""
        self.world([comment("c1")], scenario={"token": {"value": TOKEN, "age_sec": 30 * DAY}})
        self.cycle()
        self.fault(kind="refresh", body={"token_type": "bearer"}, deliver=False)
        self.cycle(after=24 * HOUR + MIN)
        self.assertEqual((self.kv("token"), self.kv("token_refresh_fails")), (TOKEN, "1"))
        self.assertNotIn("token", self.problems())
        self.cycle(after=6 * HOUR + MIN)
        self.assertEqual((self.kv("token"), self.kv("token_refresh_fails")), (TOKEN, "2"))
        self.assertIn("token", self.problems())
        self.assertIsNone(store.get_account_hold(self.conn))          # a failed refresh never stops the work
        self.add(comment("c2"))
        self.cycle(after=90)
        self.assertEqual(self.status("c2"), ("done", ""))

    def test_t9_a_token_that_lives_two_hours_is_announced_the_moment_it_dies_and_burns_nobody(self):
        """T9 / L37 (chatmany README:194: the dashboard's "Generate token" gives a token of a few hours;
        "your automation dies quietly before the day is out"). Not detected in advance (SPEC 8.2 does not ask
        for it), but the death is on Telegram within one poll and the person who waits is not written off."""
        self.world([comment("c1")], scenario={"token": {"value": TOKEN, "age_sec": 0, "expires_in_sec": 2 * HOUR}})
        self.cycle()
        took = self.hours_until(lambda: "hold:account" in self.problems(), limit=4 * HOUR)
        self.assertIsNotNone(took)
        self.assertLess(abs(took - 2), 0.1)
        self.add(comment("c2"))
        self.run_for(HOUR)
        self.assertIn("auth", "\n".join(self.told("hold:account")))
        self.assertIsNone(self.status("c2"))              # nothing is read during the hold; nobody is refused
        self.fake.control("/token", {"value": "IGAAlonglived2"})
        self.write_env(ENV.replace(TOKEN, "IGAAlonglived2"))
        self.run_for(5 * MIN)
        self.assertEqual(self.status("c2"), ("done", ""))


if __name__ == "__main__":
    unittest.main()
