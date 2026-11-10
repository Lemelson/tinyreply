"""Regression tests of attack round 1: every confirmed finding, as the test that showed it.

Attackers wrote these tests against commit a78190c, where each of them FAILED; skeptics confirmed them as bugs or
as gaps of SPEC.md. They pass since the fixes of round 1 and say, each in its docstring, what was wrong and
which words of SPEC.md (as it read then) they stood on. The rules that closed the gaps are in SPEC.md now.

Everything runs against sim/fake_meta.py on 127.0.0.1 and is judged by what the fake received and delivered,
by the database and by what Telegram got, never by what the bot says it did. Sections, in this order:

    the engine in-process (harness of tests.test_engine)        a config edit, G2 across a clock step
    the engine in-process with the real report                  matcher, config bounds, identity
    engine + report + notifier in-process                       silent losses (G5); what Meta is known to do
    bot.py as a subprocess on a scaled clock                    databases that cannot be used, .env, lock
    the lessons of three open-source projects                   harnesses of tests.test_lessons_*
"""
import fcntl
import io
import json
import math
import os
import pathlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
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
import tests.test_engine as te               # as modules: their test classes must not be collected a second time
import tests.test_lessons_chatbotx as lcx
import tests.test_lessons_chatmany as lcm
import tests.test_lessons_openreply as lo
from sim.fake_meta import FakeMeta

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(CODE, "bot.py")
MIN, HOUR, DAY, SLOT = te.MIN, te.HOUR, te.DAY, te.SLOT
START, ME, EXAMPLE = te.START, te.ME, te.EXAMPLE
MEDIA, MEDIA_2 = te.MEDIA, te.MEDIA_2
OWNER = {"id": ME["id"], "username": ME["username"]}
TOKEN = "IGAAregresstoken1"
TG_TOKEN, TG_CHAT = "123456:regress", "42"
TG = {"token": TG_TOKEN, "chat_id": int(TG_CHAT)}
ENV = f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN={TG_TOKEN}\nTG_CHAT_ID={TG_CHAT}\n"
STEP = 90        # seconds between two cycles of the matcher tests: more than one send slot at 60 DMs per hour
SCALE = 600      # the subprocess tests: one real second is ten minutes of the bot's time
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# SPEC 5, the question words
QUESTION_WORDS = ["зачем", "почему", "как", "где", "когда", "сколько", "какой", "какая", "какие", "что", "чё", "че",
                  "нафига", "нахрена"]
comment, media = te.comment, te.media   # a scenario comment (by default a fresh request by a person of its own); a reel

# Starts the bot with a limit on the size of every file it writes: the database files cannot grow any more,
# which is what a full disk (or a quota) looks like to SQLite. Pipes are not limited, so stderr still works.
FILE_LIMIT_LAUNCHER = ("import os, resource, sys\n"
                       "limit = int(sys.argv[1])\n"
                       "resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))\n"
                       "os.execv(sys.executable, [sys.executable, '-B', *sys.argv[2:]])\n")


def refusing_port():
    """A port of 127.0.0.1 that refuses a connect at once (the suite's own dead address is port 9, tests/__init__.py)."""
    for port in (9, 1, 7, 13):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            try:
                probe.connect(("127.0.0.1", port))
            except ConnectionRefusedError:
                return port
            except OSError:
                pass
    raise unittest.SkipTest("no port of 127.0.0.1 refuses connections at once")


def anchor_for(scale, hour=1):
    """CLOCK_ANCHOR that makes the scaled clock start at the next HH:00 UTC.

    The bot's day has 144 real seconds at scale 600: without this a test would, now and then, cross the bot's
    midnight or its digest hour (a digest) in the middle of what it measures.
    """
    real = time.time()
    target = real // 86400 * 86400 + hour * 3600
    while target < real + 600:
        target += 86400
    return real - (target - real) / (scale - 1)


# ====================================================================================================================
# G2 and a config edit: the engine in-process, the clock in the test's hands (tests.test_engine)
# ====================================================================================================================

class GatesCase(te.EngineCase):
    def dm_times(self):
        """When each POST /messages was RECEIVED by the fake (G2 is measured there), seconds since the start."""
        return [round(request["t"] - te.START, 3) for request in self.requests("dm")]

    def assert_g2(self, per_hour):
        """G2 as SPEC section 2 words it, over everything the fake received."""
        times, slot, broken = self.dm_times(), 3600 / per_hour, []
        for earlier, later in zip(times, times[1:]):
            if later - earlier < slot:
                broken.append(f"two POST /messages received {later - earlier:.1f} s apart, the slot is {slot:.0f} s")
        for index, at in enumerate(times):
            hour = [t for t in times[:index + 1] if t > at - 3600]
            if len(hour) > per_hour:
                broken.append(f"{len(hour)} POST /messages received within one hour, max_sends_per_hour is {per_hour}")
        self.assertEqual(broken, [], f"G2 is broken; receive times of all POST /messages (s): {times}")

    def count(self, status):
        return self.conn.execute("SELECT COUNT(*) FROM comments WHERE status=?", (status,)).fetchone()[0]

    def problem_keys(self):
        """The keys of report.problems() of the real report module (SPEC 9), not of the harness's stand-in."""
        sys.modules.pop("report", None)   # te.EngineCase restores sys.modules when the test ends
        import report
        return [key for key, _ in report.problems(self.conn, self.cfg, clock.now())]

    def stop_after(self, method, calls=1):
        """SIGTERM arrives while the `calls`-th call of this client method is in flight (SPEC 3: it finishes)."""
        real, seen = getattr(self.client, method), []

        def stopping(*args):
            seen.append(args)
            result = real(*args)
            if len(seen) == calls:
                self.stop = True
            return result
        setattr(self.client, method, stopping)


class ConfigEdit(GatesCase):
    """SPEC 4: a config edit must not mass-send by surprise. 30 stored comments that did not match start to
    match after ONE config edit; the reel has two pages. The start with the edited config disarms: nobody is
    sent to until a human arms, in however many pieces the first full sweep after the edit gets through."""

    WORD = "арбуз"

    def test_a_restart_in_the_middle_of_the_first_sweep_after_the_edit_arms_nothing(self):
        self.boot([comment(f"c{n:02}", self.WORD, age=100 + n) for n in range(30)],
                  scenario={"limits": {"page_size": 15}})
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.count("seen"), 30)                    # stored, not a request under this config
        self.assertEqual(store.budget(self.conn), "unlimited")      # armed long ago, for the normal flow
        self.restart(reels=[{"also_match": [self.WORD]}])           # the edit takes effect at the restart
        self.stop_after("comments_page")                # SIGTERM after page 1 (a second deploy, a reboot)
        before = self.seq()
        self.cycle(after=SLOT)
        self.assertEqual(len(self.requests("comments", since=before)), 1)   # page 2 was never asked for
        self.stop = False
        self.restart()                                  # systemd starts it again, same config
        for _ in range(3):
            self.cycle(after=SLOT)
        self.assertEqual([request["target"] for request in self.requests("dm")], [],
                         "DMs went out although one config edit made 30 stored comments match")
        self.assertEqual((store.budget(self.conn), self.count("pending")), (0, 30))
        store.arm(self.conn, "unlimited")               # the human has read the report
        self.assertEqual(self.cycle(after=SLOT), 1)


class ClockStep(GatesCase):
    """Supported configuration (SPEC 2): "wall clock within 5 minutes of real time". The clock guard of 8.1
    only looks at steps of more than 10 minutes, and the slot is computed from wall-clock journal times alone.
    A clock that was 4 minutes slow and is stepped to the right time (NTP) makes the bot believe four minutes
    passed: the next DM goes out one poll interval after the last one, half a slot early at the receiver.

    The example config's poll interval (90 s) and a careful rate of 20 per hour (a slot of 180 s).
    The fake runs on true time (time.time of the harness); only the bot's clock.now() is `skew` seconds off.
    """

    PER_HOUR, POLL = 20, 90

    def setUp(self):
        super().setUp()
        self.skew = 0.0
        patch = mock.patch.object(clock, "now", lambda: _EPOCH + timedelta(
            milliseconds=math.floor((time.time() + self.skew) * 1000)))
        patch.start()
        self.addCleanup(patch.stop)

    def one_dm_then_somebody_else_comments(self):
        self.skew = -4 * MIN                                        # the server's clock is 4 minutes slow
        self.boot([comment("c1", age=10 * MIN)], max_sends_per_hour=self.PER_HOUR, poll_interval_sec=self.POLL)
        self.assertEqual(self.cycle(), 1)                           # DM to c1; the queue is empty: next poll in 90 s
        self.add(comment("c2", age=5))

    def test_control_without_a_step_the_slot_is_kept(self):
        self.one_dm_then_somebody_else_comments()
        for _ in range(3):
            self.cycle(after=self.POLL)
        self.assertEqual(len(self.requests("dm")), 2)
        self.assert_g2(self.PER_HOUR)

    def test_slot_at_the_receiver_when_a_slow_clock_is_stepped_to_the_right_time(self):
        self.one_dm_then_somebody_else_comments()
        self.skew = 0.0                                             # NTP steps the clock: now it is right
        for _ in range(3):
            self.cycle(after=self.POLL)                             # the polls, 90 s of true time apart
        self.assert_g2(self.PER_HOUR)


# ====================================================================================================================
# G3, the matcher and the config
# ====================================================================================================================

class MatchingCase(unittest.TestCase):
    def setUp(self):
        self.time = te.Time()
        for patch in (mock.patch("time.time", self.time.time), mock.patch.object(clock, "monotonic", self.time.monotonic),
                      mock.patch.object(clock, "sleep", self.time.sleep), mock.patch.object(clock, "SCALE", 1.0),
                      mock.patch.object(sys, "stderr", self.enterContext(open(os.devnull, "w")))):
            patch.start()
            self.addCleanup(patch.stop)
        self.fake = None

    def tearDown(self):
        if self.fake:   # a correct bot never makes a call that is not one of SPEC section 6
            self.assertEqual([r["strict"] for r in self.requests() if r["strict"]], [])

    # ---- building a bot

    def load_cfg(self, reels=None, **top):
        """config.load of the shipped example with these changes; ConfigError if the product refuses it."""
        raw = {**EXAMPLE, "sending": True, **top}
        if reels is not None:
            raw["reels"] = [{**EXAMPLE["reels"][0], **reel} for reel in reels]
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(raw, file)
        return config.load(path)

    def boot(self, comments=(), budget="unlimited", scenario=None, **cfg_changes):
        """A fake Meta with one reel and these comments, an empty state directory, an engine just started.

        The config is loaded first: a ConfigError leaves nothing behind."""
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = self.load_cfg(**cfg_changes)
        self.fake = FakeMeta({"account": ME, "token": TOKEN, "media": [media(comments=comments)],
                              **(scenario or {})}).start()
        self.addCleanup(self.fake.stop)
        with open(os.path.join(self.dir, ".env"), "w", encoding="utf-8") as file:
            file.write(f"IG_ACCESS_TOKEN={TOKEN}\n")
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
            self.cfg = self.load_cfg(**cfg_changes)
        self.notifier = te.Notifier()
        client = meta.Client(self.fake.api_base, self.cfg.api_version, lambda: store.kv_get(self.conn, "token") or "")
        self.engine = engine.Engine(self.cfg, self.conn, client, self.notifier, lambda: False)
        return self.engine

    def cycle(self, after=0):
        self.time.advance(after)
        return self.engine.cycle()

    def cycles(self, n):
        for _ in range(n):
            self.cycle(after=STEP)

    # ---- the fake's ground truth and the database

    def requests(self, *kinds, since=0):
        found = self.fake.control("/truth", query={"since": str(since)})["requests"]
        return [r for r in found if not kinds or r["kind"] in kinds]

    def posts(self, since=0):
        return [(r["kind"], r["target"]) for r in self.requests("dm", "public", since=since)]

    def seq(self):
        return self.fake.control("/truth")["seq"]

    def dms(self):
        """Comment ids whose DM the fake delivered, in order."""
        return [dm["comment_id"] for dm in self.fake.control("/truth")["dms"]]

    def add(self, *comments, media_id=MEDIA):
        for one in comments:
            self.fake.control("/comments", {"add": {"media_id": media_id, **one}})

    def fault(self, **spec):
        return self.fake.control("/faults", {"add": spec})

    def status(self, comment_id):
        row = store.comment(self.conn, comment_id)
        return row and (row["status"], row["reason"])

    def statuses(self):
        return {row[0]: (row[1], row[2]) for row in
                self.conn.execute("SELECT comment_id, status, reason FROM comments ORDER BY comment_id")}

    def problem_keys(self):
        return sorted(key for key, _ in report.problems(self.conn, self.cfg, clock.now()))


class Matching(MatchingCase):
    """Each of these fails on the current code."""

    # ------------------------------------------------------------------ people lost silently

    def test_owner_answer_under_a_question_costs_the_person_the_dm_they_then_ask_for(self):
        """Ann asks «а что такое арена?» (SPEC 5: `near`, never sent). The owner answers her in public by hand.
        Ann then writes «арена». She has never had a DM, yet she never gets one: 8.3 step 1 turns ANY comment with
        an owner reply into `already`/`owner_replied`, matching or not, and 7.2 step 2 then takes that row for
        "the same person already handled", so her request ends `dup_user`. Nothing is announced.

        Expected: 7.1 defines `dup_user` as "same person already handled for this media"; a person whose
        question was answered in public was not handled. OR-60 limits the owner-reply rule to MATCHED comments.
        SPEC 2: a scenario that "loses people silently" is a bug."""
        ann = {"id": "u_ann", "username": "ann"}
        question = {"id": "q1", "text": "а что такое арена?", "age_sec": HOUR, "from": ann, "should_match": False,
                    "replies": [{"text": "Это скилл для Claude. Напиши «арена», пришлю ссылку", "from": OWNER}]}
        self.boot([question, comment("bob", age=120)])
        self.cycle()
        self.assertEqual(self.dms(), ["bob"])   # control: every gate is open, the question itself got no DM
        self.add({"id": "r1", "text": "арена", "age_sec": 0, "from": ann, "should_match": True})
        self.cycles(3)
        self.assertEqual(self.dms(), ["bob", "r1"],
                         f"Ann asked for the link and got nothing: her request is {self.status('r1')}, her question "
                         f"{self.status('q1')}, problems announced: {self.problem_keys()}")

    def test_a_question_word_as_keyword_loads_and_can_never_match(self):
        """«Напиши КАК / СКОЛЬКО / ГДЕ в комментариях» is an everyday call to action. Such a keyword passes
        config.load and `bot.py check`, but SPEC 5 makes every comment that contains a question word `near`:
        the reel answers nobody, every request is filed as `seen`/`near:question`, and no problem is raised.

        Expected (spec gap): a config that loads must not contain a keyword its own matcher can never accept:
        either the config is refused (SPEC 4 lists what is rejected; this is missing) or the bare keyword
        matches. CX-70 / CM-A6: "nothing ever matches" must be detected, not silent."""
        for word in QUESTION_WORDS:
            with self.subTest(keyword=word):
                self.dir = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, self.dir, True)
                try:
                    cfg = self.load_cfg(reels=[{"keywords": [word], "also_match": []}])
                except config.ConfigError:
                    continue   # refused at load: fine
                self.assertEqual(match.Matcher(cfg.reels[0]).check(word), ("match", ""),
                                 f"the config loads, but a comment that is exactly the keyword {word!r} is not a request")
        # end to end with one of them
        try:
            self.boot([comment("c1", "Сколько", age=200), comment("c2", "сколько?", age=100)],
                      reels=[{"keywords": ["сколько"], "also_match": []}])
        except config.ConfigError:
            return
        self.cycle()
        self.cycles(3)
        self.assertEqual(self.dms(), ["c1", "c2"],
                         f"nobody is answered: {self.statuses()}; problems announced: {self.problem_keys()}")

    # ------------------------------------------------------------------ people messaged who did not ask

    def test_latin_keyword_bot_sends_the_dm_to_everybody_who_writes_the_russian_word_vot(self):
        """Keywords ["бот", "bot"], the same pattern as the shipped ["арена", "arena"]. SPEC 5 lets a token hit
        when it "equals ... the skeleton of a form", and skeleton("bot") is the Cyrillic word «вот». So
        «Вот это да 🔥» and «ну вот, опять реклама» are requests and both people get the DM and a public
        "sent you a DM" reply. The same happens for top -> «тор», hot -> «нот», he -> «не», to -> «то».

        Expected (the rule of SPEC 5 itself is unsafe here): G3 exists so that nobody is messaged who did not
        ask; inventory K5 (MUST FIX IN CORE) is this defect in v1: `skill` -> «к» made «иду к вам» match.
        A look-alike rule may repair «Aрена» typed with a Latin A; it must not turn a Latin keyword into a
        different Russian word. Refusing such a keyword at load is accepted as a fix."""
        try:
            self.boot([comment("req1", "бот", age=400), comment("req2", "BOT", age=300),
                       comment("no1", "Вот это да 🔥", age=200, should_match=False),
                       comment("no2", "ну вот, опять реклама", age=100, should_match=False)],
                      reels=[{"keywords": ["бот", "bot"], "also_match": []}])
        except config.ConfigError:
            return
        self.cycle()
        self.cycles(5)
        texts = {row[0]: row[1] for row in self.conn.execute("SELECT comment_id, text FROM comments")}
        wrong = [texts[comment_id] for comment_id in self.dms() if comment_id.startswith("no")]
        self.assertEqual(wrong, [], f"DMs were delivered to people who wrote {wrong}")
        self.assertEqual(sorted(self.dms()), ["req1", "req2"])

    def test_a_config_edit_sends_nothing_whether_or_not_the_sweep_after_it_fails_half_way(self):
        """30 stored comments say «арбуз». The owner adds "арбуз" to also_match and restarts. SPEC 4: a config
        edit must not mass-send by surprise: the start with the edited config disarms, with a clean sweep
        (control) and when one page of that sweep fails (HTTP 500, code 1: the everyday transient answer).

        Expected: budget 0, no POST /messages, whichever way the 30 rows were found."""
        for failing_page in (None, 2):
            with self.subTest(failing_page=failing_page):
                self.boot([comment(f"c{n:02d}", "арбуз", age=100 + n, should_match=False) for n in range(30)],
                          scenario={"limits": {"page_size": 15}})
                self.cycle()
                self.assertEqual({status for status, _ in self.statuses().values()}, {"seen"})
                self.restart(reels=[{"also_match": EXAMPLE["reels"][0]["also_match"] + ["арбуз"]}])
                before = self.seq()
                if failing_page:
                    self.fault(kind="comments", nth=failing_page, http=500, code=1,
                               message="An unknown error has occurred.")
                self.cycles(4)
                sent = [target for kind, target in self.posts(since=before) if kind == "dm"]
                self.assertEqual((sent, store.kv_get(self.conn, "send_budget")), ([], "0"),
                                 f"{len(sent)} DMs went out after the edit")

    def test_one_me_answer_without_id_and_username_and_the_bot_messages_its_own_comment(self):
        """The identity {user_id, id, username} is known and stored. One later GET /me (the hourly one) is
        answered 200 with `user_id` but without `id` and `username`. The bot overwrites what it knew with
        nothing; from then on its own top-level comments (from.id = the app-scoped id, from.username = its
        username) are a stranger's: the owner's pinned «Пишите арена» gets a private reply and a public
        "sent you a DM" under it.

        Expected: G3 "never to the account's own comments"; G6 "the account identity is known". A field of
        the identity that was known must not be forgotten because one answer left it out."""
        self.boot([comment("c1")])
        self.cycle()
        self.assertEqual(self.dms(), ["c1"])
        self.fault(kind="me", http=200, body={"user_id": ME["user_id"], "followers_count": 1234, "media_count": 7})
        self.cycle(after=HOUR + 2 * MIN)
        self.assertEqual(len(self.requests("me")), 2)   # control: the hourly GET /me got the thin answer
        self.fake.control("/faults", {"clear": True})
        before = self.seq()
        self.add({"id": "mine", "text": "Пишите «арена» в комментариях 👇", "age_sec": 0, "from": OWNER,
                  "should_match": False})
        self.cycles(2)
        self.assertEqual(self.posts(since=before), [],
                         f"the account's own comment was answered: it is {self.status('mine')}")

    # ------------------------------------------------------------------ a matched comment dropped

    def test_half_an_emoji_in_a_request_becomes_a_question_mark_and_the_request_is_dropped(self):
        """Meta's JSON can carry half of an emoji ("\\ud83d": a lone surrogate; SPEC 5 names the case). The
        matcher accepts «хочу арену <half emoji>» (two words, no `?`) and collect stores it as pending. But
        the text is written to the database with the surrogate replaced by `?`, and the claim re-decides on the
        STORED text: two words and a `?` is `near:question`. The person is filed as `seen` and never answered;
        the report then shows a question mark that the person never wrote.

        Expected: G3/7.2 step 0 re-check the comment Meta listed, not a text the bot altered; G5/I8: a comment
        the matcher accepts is pending-or-later."""
        self.boot([comment("s1", "хочу арену \ud83d", age=200), comment("c2", age=100)])
        self.cycle()
        self.cycles(3)
        self.assertEqual(match.Matcher(self.cfg.reels[0]).check("хочу арену \ud83d"), ("match", ""))   # the premise
        self.assertEqual(self.dms(), ["s1", "c2"],
                         f"the request is {self.status('s1')}, stored text "
                         f"{store.comment(self.conn, 's1')['text']!r}; problems announced: {self.problem_keys()}")

    # ------------------------------------------------------------------ a config that loads and then breaks

    def test_a_late_after_minutes_that_loads_must_not_break_every_claim(self):
        """`late_after_minutes` has no upper bound (hand-back gap 10). 2147483647 or 9999999999 ("never late")
        load and pass `check`; every claim then dies in `now - timedelta(minutes=...)` with OverflowError,
        inside the cycle: nobody is sent to, and because the exception leaves the cycle before housekeeping,
        `last_cycle_ok_at` is never written and the token upkeep, the hourly metric and the daily digest stop
        for as long as one person waits.

        Expected: SPEC 4, config.load "returns a read-only object ... nothing downstream has to re-check a
        value": a value that cannot work is a ConfigError (exit 4, `fatal_start`), or it works."""
        for minutes in (2 ** 31 - 1, 9999999999):
            with self.subTest(late_after_minutes=minutes):
                try:
                    self.boot([comment("c1")], late_after_minutes=minutes)
                except config.ConfigError:
                    continue
                self.cycle()
                self.cycle(after=STEP)
                self.assertEqual((self.dms(), store.kv_get(self.conn, "last_cycle_error")), (["c1"], None))
                self.assertIsNotNone(store.kv_get(self.conn, "last_cycle_ok_at"))

    def test_a_poll_interval_over_ten_minutes_makes_the_report_call_a_healthy_service_down(self):
        """`poll_interval_sec` has a lower bound only (SPEC 4: under 30 is rejected). 900 is the obvious value
        for an owner whose developer account was blocked and who wants to go easy on the API. The service then
        sleeps 15 minutes between cycles (8.1), and SPEC 9 calls it `not_running` whenever the last cycle is
        older than 10 minutes: for a third of every interval `bot.py report` opens with "PROBLEM: not_running,
        the service is down" while it holds the lock and works as configured. `not_running` is the only signal
        for a dead service, and this teaches the owner to ignore it.

        Expected (spec gap between 4, 8.1 and 9): a config that loads does not make the health check lie:
        the value is refused at load, or the report allows for the configured interval."""
        try:
            self.boot(poll_interval_sec=900)
        except config.ConfigError:
            return
        lock = self.enterContext(open(os.path.join(self.dir, "core.lock"), "a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the running service holds the instance lock
        self.cycle()
        self.assertIsNone(store.kv_get(self.conn, "last_cycle_error"))
        self.assertEqual(report.build(self.conn, self.cfg, clock.now())["problems"], [])   # control: healthy
        self.time.advance(11 * MIN)                         # the service is asleep, as the config tells it to be
        data = report.build(self.conn, self.cfg, clock.now())
        self.assertEqual([key for key, _ in data["problems"]], [], data["verdict"])


class StartUpWithOddNumbers(unittest.TestCase):
    """bot.py as a subprocess, real clock, its own STATE_DIR, a fake Meta + Telegram on 127.0.0.1."""

    def test_a_repeat_min_that_check_accepts_must_not_crash_the_start(self):
        """`alerts.repeat_min` has no upper bound either. 10**13 ("never repeat") passes `bot.py check`
        ("config ok"), and then `run`/`once` die before the first cycle with OverflowError in
        notify.Notifier (timedelta(minutes=...)): exit 1, which systemd restarts every 30 s for ever. The
        notifier is what crashed, so not one Telegram line is sent: the service is down and nobody is told.

        Expected: SPEC 3, an invalid config is exit 4 with the `fatal_start` event; a config that `check`
        accepts starts."""
        for repeat_min in (10 ** 13, 2 ** 63 - 1):
            with self.subTest(repeat_min=repeat_min):
                state = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, state, True)
                fake = FakeMeta({"account": ME, "token": TOKEN, "media": [media()],
                                 "telegram": {"token": "123:abc", "chat_id": 42}}).start()
                self.addCleanup(fake.stop)
                with open(os.path.join(state, "config.json"), "w", encoding="utf-8") as file:
                    json.dump({**EXAMPLE, "alerts": {"repeat_min": repeat_min, "digest_hour_utc": 6}}, file)
                with open(os.path.join(state, ".env"), "w", encoding="utf-8") as file:
                    file.write(f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN=123:abc\nTG_CHAT_ID=42\n")
                env = {key: value for key, value in os.environ.items()
                       if key not in ("STATE_DIR", "CONFIG", "NOTIFY_SOCKET", "CLOCK_SCALE", "CLOCK_ANCHOR")}
                env.update(STATE_DIR=state, IG_API_BASE=fake.api_base, TG_API_BASE=fake.api_base,
                           PYTHONDONTWRITEBYTECODE="1")

                def bot(command):
                    return subprocess.run([sys.executable, "-B", BOT, command], env=env, capture_output=True,
                                          text=True, timeout=60)
                check = bot("check")
                if check.returncode == 4:
                    continue   # refused as an invalid config: fine
                self.assertEqual(check.returncode, 0, check.stderr[-400:])
                once = bot("once")
                told = [message["text"] for message in fake.control("/truth")["telegram"]]
                self.assertEqual(once.returncode, 0,
                                 f"`check` said {check.stdout.splitlines()[0]!r}, then `once` ended with "
                                 f"{once.stderr.strip().splitlines()[-1]!r}; Telegram was told {told}")


# ====================================================================================================================
# G5: no silent loss. The real engine, report and notifier against the fake Meta + Telegram
# ====================================================================================================================

class SilenceCase(unittest.TestCase):
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
        """Hours since this test's world began."""
        return (unix - self.began) / HOUR

    # ---- the bot

    def world(self, comments=(), budget="unlimited", env=None, scenario=None, **cfg_changes):
        """A fake Meta + Telegram with one reel and these comments, an empty state directory, a bot just started.
        Called again in one test, it begins another world (the clock goes on)."""
        if self.fake:
            self.tearDown()   # the strictness check for the world that ends here
        self.began = self.time()
        self.dir = tempfile.mkdtemp(prefix="silence-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeMeta({"account": ME, "token": TOKEN, "telegram": TG, "media": [{
            "id": MEDIA, "permalink": "https://www.instagram.com/reel/DAbc/", "shortcode": "DAbc",
            "comments": list(comments)}], **(scenario or {})}).start()
        self.addCleanup(self.fake.stop)
        self.write_env(f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN={TG['token']}\nTG_CHAT_ID={TG['chat_id']}\n"
                       if env is None else env)
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        # the instance lock of SPEC 3, as bot.py holds it: `report` then sees a running service
        self.lock = open(os.path.join(self.dir, "core.lock"), "a")
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.start(**cfg_changes)
        if budget is not None:
            store.arm(self.conn, budget)   # after the start: one with a config the database has not seen disarms
        return self.engine

    def write_env(self, text):
        with open(os.path.join(self.dir, ".env"), "wb") as file:
            file.write(text if isinstance(text, bytes) else text.encode("utf-8"))

    def start(self, reels=None, **cfg_changes):
        """A new process run on the same state (nothing in memory survives), with this config."""
        raw = {**EXAMPLE, "sending": True, **cfg_changes}
        if reels is not None:
            raw["reels"] = [{**EXAMPLE["reels"][0], **reel} for reel in reels]
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(raw, file)
        self.cfg = config.load(path)
        notifier = notify.Notifier(self.conn, bot.telegram(self.dir), self.fake.api_base,
                                   repeat_min=self.cfg.alerts.repeat_min)
        client = meta.Client(self.fake.api_base, self.cfg.api_version, lambda: store.kv_get(self.conn, "token") or "")
        self.engine = engine.Engine(self.cfg, self.conn, client, notifier, lambda: False)
        return self.engine

    def run_for(self, seconds):
        """What Engine.run() does for this long: a cycle, then the pause the cycle asked for. Returns the cycles."""
        end, cycles = self.time() + seconds, 0
        while self.time() < end:
            self.engine.cycle()
            cycles += 1
            self.advance(self.engine.pause)
        return cycles

    # ---- what the owner can see

    def truth(self):
        return self.fake.control("/truth")

    def telegram(self, since=0):
        """[(hours since the start, line)] of everything Telegram received from message number `since` on."""
        return [(self.hours(message["t"]), line) for message in self.truth()["telegram"][since:]
                for line in message["text"].split("\n")]

    def told(self, since=0):
        """The lines of problems and of their ends: what `sync` queues (SPEC 9)."""
        return [(hour, line) for hour, line in self.telegram(since) if line.startswith(("⚠ ", "✓ "))]

    def said(self, start, since=0):
        return [(hour, line) for hour, line in self.telegram(since) if line.startswith(start)]

    def messages(self):
        return len(self.truth()["telegram"])

    def problems(self):
        return dict(report.problems(self.conn, self.cfg, clock.now()))

    def build(self):
        return report.build(self.conn, self.cfg, clock.now())

    def statuses(self):
        return {(status, reason): n for status, reason, n in self.conn.execute(
            "SELECT status, reason, COUNT(*) FROM comments GROUP BY status, reason")}

    def attempts(self, kind="dm"):
        return [tuple(row) for row in self.conn.execute(
            "SELECT outcome, reason FROM attempts WHERE kind=? ORDER BY id", (kind,))]

    def delivered(self):
        return [dm["comment_id"] for dm in self.truth()["dms"]]

    def requests(self, *kinds, since=0):
        found = self.fake.control("/truth", query={"since": str(since)})["requests"]
        return [request for request in found if not kinds or request["kind"] in kinds]


class StalledQueue(SilenceCase):
    """SPEC 9, `waiting`: "rows in `pending` ... exist and there has been no `ok` DM for 30 min, for any reason
    except `cap`. Text: ... the reason (`sending_off`, `no_budget`, a hold, `no_sweep`, or `stalled` when every
    gate is open)". SPEC 8.4: `cap` is "waiting for the next slot: normal".

    SPEC 8.5 gives one way to try a DM in every cycle for ever without an `ok` and without a hold: "`not_sent`
    sets no hold: the comment returns to `pending`, sending stops for this cycle, the next cycle tries again."
    The reads work, every gate is open, every POST /messages fails to connect. That is the stalled queue with
    all gates open. report.py calls it `cap` because "a DM was just tried" looks like "the slot is busy", so
    `waiting` is never raised: the bot says nothing for as long as it lasts, the report and the daily digest say
    "OK: в очереди 5 чел., отправка идёт", and with `arm N` every failed connect also spends one of the N.
    """

    def test_dms_that_cannot_connect_for_three_hours_are_announced_as_waiting(self):
        self.world([comment(f"c{index}", age=3 * HOUR + index) for index in range(5)])   # armed: `arm all`
        port = refusing_port()
        # Meta answers every read; a POST /messages never gets a connection (the real transport, the real
        # classification: connect refused -> not_sent/connect)
        cut = meta.Client(f"http://127.0.0.1:{port}", self.cfg.api_version, lambda: TOKEN)
        self.engine.client.send_dm = cut.send_dm

        cycles = self.run_for(3 * HOUR)

        # the scenario is what it says: three hours, a DM tried in every cycle, none left this machine
        tried = self.attempts()
        self.assertGreater(len(tried), 60)
        self.assertEqual(set(tried), {("not_sent", "connect")})
        self.assertEqual(self.delivered(), [])
        self.assertEqual(self.statuses(), {("pending", ""): 5})
        # ... and every gate of G6 is open the whole time: nothing else explains the wait
        gates = self.build()["gates"]
        self.assertEqual((gates["sending"], gates["budget"], gates["account_hold"], gates["send_hold"]),
                         (True, "unlimited", None, None))
        self.assertEqual(self.build()["reels"][0]["state"], "ok")
        self.assertEqual(self.requests("dm"), [])                    # the fake never saw a POST /messages

        # G5 / SPEC 9: five people wait, no ok DM for three hours, and the reason is not the hourly cap
        waiting = self.said("⚠ waiting")
        self.assertTrue(
            waiting,
            f"5 people waited 3 hours, {len(tried)} DMs in {cycles} cycles failed to connect, every gate was open, "
            f"and Telegram never heard `waiting`. All it got about problems: {self.told()}. "
            f"What the bot announced in its last cycle: {sorted(row[0] for row in self.conn.execute('SELECT key FROM problems'))}")


class SwitchedOffReel(SilenceCase):
    """SPEC 9: "`waiting`: rows in `pending` ... exist and there has been no `ok` DM for 30 min, for any reason
    except `cap`"; "`expiring`: N pending rows expire within 24 h". G5: "... announced and re-announced until
    it ends". SPEC 4 only says that the rows of a reel with `active=false` "are reported as parked", and SPEC 9
    only takes them out of `unaccounted`.

    The owner switches a reel off (the per-reel STOP of this version) while people of it are in the queue and
    forgets it: the historical outage "a gate left closed" (v1 issue A5: a STOP file lay there for two days and
    nothing alerted). The bot answers by CLEARING `waiting` and `expiring` on Telegram, the digest says
    "OK: никто не ждёт", and the next thing the owner hears is "1 people lost", five times.
    """

    def test_people_who_wait_under_a_switched_off_reel_are_still_announced(self):
        # five people, 8 to 12 hours before their 7-day window ends; the bot was never armed
        self.world([comment(f"c{index}", age=6 * DAY + 12 * HOUR + index * HOUR) for index in range(5)], budget=0)
        self.run_for(40 * MIN)
        before = [line for _, line in self.told()]
        self.assertTrue(any(line.startswith("⚠ waiting: 5 чел.") for line in before), before)    # control:
        self.assertTrue(any(line.startswith("⚠ expiring: 5 чел.") for line in before), before)   # it was told

        self.start(reels=[{"active": False}])            # config edit + restart: the reel is switched off
        mark = self.messages()
        self.run_for(3 * HOUR)

        self.assertEqual(self.statuses(), {("pending", ""): 5})       # they still wait, hours before expiry
        after = [line for _, line in self.told(mark)]
        found = self.problems()
        self.assertTrue(
            found,
            f"5 people are `pending` and their windows end within 9 hours, the reel is switched off, and "
            f"report.problems is empty; verdict of the report: {self.build()['verdict']!r}; what Telegram heard "
            f"after the switch: {after}")
        self.assertEqual([line for line in after if line.startswith("✓ cleared")], [],
                         "the owner was told that the waiting had ended")


class QueueDroppedByAConfigEdit(SilenceCase):
    """SPEC 7.2 step 0 (G3): at claim time a `pending` row that the matcher of the running config no longer
    accepts becomes `seen`. G5 says "every state in which people are waiting or being lost is announced".

    The owner starts the next campaign under the same reel and replaces the keyword. The 30 people who asked
    with the old word and were still in the queue are moved to `seen` in ONE cycle (a skipped claim uses no
    slot), also with the budget at 0. Telegram hears "✓ cleared: waiting", the digest says "OK: никто не ждёт".
    `seen` rows never expire, so there is never an "N people lost" either. What the owner is told is the
    event of SPEC 4 at the start with the edited config: `config_changed`, read the report, then arm.
    """

    def test_thirty_people_who_leave_the_queue_because_the_keyword_changed_are_announced(self):
        self.world([comment(f"c{index:02}", age=5 * DAY + index * MIN) for index in range(30)], budget=0)
        self.run_for(40 * MIN)
        before = [line for _, line in self.told()]
        self.assertTrue(any(line.startswith("⚠ waiting: 30 чел.") for line in before), before)   # control

        self.start(reels=[{"keywords": ["скилл", "skill"], "also_match": []}])   # config edit + restart
        mark = self.messages()
        self.run_for(2 * HOUR)

        self.assertEqual(self.statuses(), {("seen", ""): 30})           # all thirty left the queue (7.2 step 0)
        self.assertEqual(self.requests("dm", "public"), [])             # nobody got anything
        about_it = [line for _, line in self.telegram(mark) if line.startswith("ℹ config_changed")]
        self.assertTrue(
            about_it,
            f"30 people who had asked for the link were taken out of the queue by a config edit and nobody was "
            f"told: the verdict is {self.build()['verdict']!r}, and all Telegram heard after the restart is "
            f"{[line for _, line in self.told(mark)]}")


class ReportDescribesTheFileNotTheService(SilenceCase):
    """SPEC 4: "The config is read once at start-up". SPEC 9: `bot.py report` prints the gates (sending, ...)
    and the waiting reasons. bot.py builds the report from config.json as it is on disk NOW, and nothing in
    the database says which config the running service loaded (guarantee "spec": the SPEC is silent).

    The owner wants to stop the sending, writes `"sending": false`, does not restart (or the restart failed),
    and asks `igbot report`: "отправка: ВЫКЛЮЧЕНА (sending: false)", verdict OK. The service goes on sending.
    The other way round the report says "включена ... stalled" while the service has sending off.
    """

    def test_the_report_does_not_call_sending_off_while_the_service_sends(self):
        self.world([comment(f"c{index}", age=10 * MIN + index * MIN) for index in range(3)])
        self.engine.cycle()
        self.assertEqual(len(self.delivered()), 1)

        path = os.path.join(self.dir, "config.json")                   # the owner edits the file ...
        with open(path, encoding="utf-8") as file:
            raw = json.load(file)
        with open(path, "w", encoding="utf-8") as file:
            json.dump({**raw, "sending": False}, file)
        self.advance(MIN)
        self.run_for(5 * MIN)                                           # ... and the service was not restarted
        self.assertEqual(len(self.delivered()), 3)                      # control: it sent two more DMs

        data = report.build(self.conn, config.load(path), clock.now())  # what `bot.py report` does (cmd_report)
        self.assertTrue(
            data["gates"]["sending"] or data["problems"],
            f"two DMs went out after the edit, and the report says sending={data['gates']['sending']}, "
            f"verdict {data['verdict']!r}, no problem: it describes the file, not the running service")


class StartRefusedByTheLock(unittest.TestCase):
    """SPEC 3: "`run` ... exit with code 3 if it is held"; systemd `Restart=always`, `RestartPreventExitStatus=3 4`.
    The other final exit, 4, is announced (`fatal_start`); exit 3 says nothing to anybody.

    An operator runs `igbot once` or `send-one` (each takes the lock) at the moment the unit starts
    or restarts: `run` exits 3, systemd never starts it again, the operator's command ends, and from then on
    nobody runs. No Telegram line, nothing queued: only `igbot report` would show `not_running`. SPEC is silent
    on this (guarantee "spec"): the state is one in which people wait and nobody is told (G5).
    """

    def test_a_service_start_that_ends_with_exit_3_tells_the_owner(self):
        state = tempfile.mkdtemp(prefix="silence-lock-")
        self.addCleanup(shutil.rmtree, state, True)
        fake = FakeMeta({"account": ME, "token": TOKEN, "telegram": TG, "media": [{
            "id": MEDIA, "permalink": "https://www.instagram.com/reel/DAbc/", "shortcode": "DAbc",
            "comments": [comment("c1")]}]}).start()
        self.addCleanup(fake.stop)
        with open(os.path.join(state, "config.json"), "w", encoding="utf-8") as file:
            json.dump({**EXAMPLE, "sending": True}, file)
        with open(os.path.join(state, ".env"), "w", encoding="utf-8") as file:
            file.write(f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN={TG['token']}\nTG_CHAT_ID={TG['chat_id']}\n")
        conn = store.connect(os.path.join(state, "core.db"))    # the database of the service, as an earlier run left it
        self.addCleanup(conn.close)
        store.init(conn)
        # SPEC 3 (since round 1): `run` waits 60 seconds for the lock before it gives up; on a clock that runs
        # 600 times faster that is a tenth of a real second
        env = {"PATH": os.environ.get("PATH", ""), "STATE_DIR": state, "IG_API_BASE": fake.api_base,
               "TG_API_BASE": fake.api_base, "PYTHONDONTWRITEBYTECODE": "1",
               "CLOCK_SCALE": str(SCALE), "CLOCK_ANCHOR": repr(time.time())}

        with open(os.path.join(state, "core.lock"), "a") as held:    # `igbot once` in flight
            fcntl.flock(held, fcntl.LOCK_EX)
            done = subprocess.run([sys.executable, "-B", BOT, "run"], env=env, capture_output=True, text=True,
                                  timeout=60)
        self.assertEqual(done.returncode, 3, done.stderr)             # final: systemd does not restart after 3

        heard = [message["text"] for message in fake.control("/truth")["telegram"]]
        queued = [row[0] for row in conn.execute("SELECT text FROM outbox")]
        self.assertTrue(heard or queued,
                        "the service start ended with exit 3 (never restarted by systemd) and neither Telegram "
                        "nor the outbox holds a word about it")


# ====================================================================================================================
# What Meta is known to do
# ====================================================================================================================

class MetaCase(unittest.TestCase):
    """One bot (engine + report + notifier, wired as bot.py wires them) against one fake Meta + Telegram."""

    def setUp(self):
        self.time = te.Time()
        for patch in (mock.patch("time.time", self.time.time), mock.patch.object(clock, "monotonic", self.time.monotonic),
                      mock.patch.object(clock, "sleep", self.time.sleep), mock.patch.object(clock, "SCALE", 1.0),
                      mock.patch.object(sys, "stderr", self.enterContext(open(os.devnull, "w")))):
            patch.start()
            self.addCleanup(patch.stop)
        self.fake = None

    def tearDown(self):
        self.retire()

    def retire(self):
        """Close the bot and the fake of the last boot(). A fair test never makes the bot issue a call that is
        not one of SPEC section 6: the fake's list of such calls must be empty."""
        if not self.fake:
            return
        strict = [r["strict"] for r in self.requests() if r["strict"]]
        self.conn.close()
        self.lock.close()
        self.fake.stop()
        self.fake = None
        if os.path.exists(os.path.join(self.dir, ".env")):
            os.chmod(os.path.join(self.dir, ".env"), 0o600)
        shutil.rmtree(self.dir, True)
        self.assertEqual(strict, [])

    def boot(self, comments=(), budget="unlimited", scenario=None, **cfg_changes):
        """A fake Meta + Telegram with one reel and these comments, an empty state directory with `.env` and
        config.json, and a bot that was just started on it. It is 12:00 UTC."""
        self.retire()
        self.time.wall = self.time.mono = 0.0
        self.dir = tempfile.mkdtemp()
        self.fake = FakeMeta({
            "account": dict(ME), "token": TOKEN, "telegram": {"token": TG_TOKEN, "chat_id": int(TG_CHAT)},
            "media": [{"id": MEDIA, "permalink": "https://www.instagram.com/reel/DAbc/", "shortcode": "DAbc",
                       "comments": list(comments)}], **(scenario or {})}).start()
        self.write_env(f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN={TG_TOKEN}\nTG_CHAT_ID={TG_CHAT}\n".encode())
        # the daily digest (a whole report) is kept out of the way: it is due at 23:00
        raw = {**EXAMPLE, "sending": True, "alerts": {"repeat_min": 120, "digest_hour_utc": 23}, **cfg_changes}
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as file:
            json.dump(raw, file)
        self.cfg = config.load(os.path.join(self.dir, "config.json"))
        self.lock = bot.take_lock(self.dir)   # as `bot.py run` holds it: the report sees a running service
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        store.init(self.conn)
        self.restart()
        if budget is not None:
            store.arm(self.conn, budget)   # after the start: one with a config the database has not seen disarms
        return self.engine

    def restart(self):
        """A new process run on the same state directory."""
        self.notifier = notify.Notifier(self.conn, bot.telegram(self.dir), self.fake.api_base,
                                        repeat_min=self.cfg.alerts.repeat_min)
        self.client = meta.Client(self.fake.api_base, self.cfg.api_version,
                                  lambda: store.kv_get(self.conn, "token") or "")
        self.engine = engine.Engine(self.cfg, self.conn, self.client, self.notifier, lambda: False)
        return self.engine

    def write_env(self, data):
        with open(os.path.join(self.dir, ".env"), "wb") as file:
            file.write(data)

    def cycle(self, after=0):
        self.time.advance(after)
        return self.engine.cycle()

    def cycles(self, count, every=SLOT):
        return [self.cycle(after=every) for _ in range(count)]

    # ---- the fake's truth

    def requests(self, *kinds, since=0):
        found = self.fake.control("/truth", query={"since": str(since)})["requests"]
        return [r for r in found if not kinds or r["kind"] in kinds]

    def meta_calls(self, since=0):
        return [r["kind"] for r in self.requests(since=since) if r["kind"] != "telegram"]

    def posts(self, since=0):
        return [(r["kind"], r["target"]) for r in self.requests("dm", "public", since=since)]

    def seq(self):
        return self.fake.control("/truth")["seq"]

    def dms(self):
        return [dm["comment_id"] for dm in self.fake.control("/truth")["dms"]]

    def replies(self):
        return [reply["comment_id"] for reply in self.fake.control("/truth")["replies"]]

    def alerts(self):
        """Every line that reached the owner's Telegram chat."""
        return [line for message in self.fake.control("/truth")["telegram"] for line in message["text"].split("\n")]

    def fault(self, **spec):
        return self.fake.control("/faults", {"add": spec})

    def add(self, *comments):
        for one in comments:
            self.fake.control("/comments", {"add": {"media_id": MEDIA, **one}})

    # ---- the database

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

    def problems(self):
        return report.problems(self.conn, self.cfg, clock.now())

    def problem_keys(self):
        return [key for key, _ in self.problems()]

    def verdict(self):
        return report.build(self.conn, self.cfg, clock.now())["verdict"]

    def send_hold(self):
        """(reason, whole minutes left) of the send hold in force, or None."""
        hold = store.get_send_hold(self.conn)
        return hold and (hold["reason"], round((clock.parse(hold["until"]) - clock.now()).total_seconds() / 60))

    def account_hold(self):
        """(reason, whole minutes until the next probe) of the account hold, or None."""
        hold = store.get_account_hold(self.conn)
        return hold and (hold["reason"],
                         round((clock.parse(hold["next_probe_at"]) - clock.now()).total_seconds() / 60))


class MetaReality(MetaCase):

    def test_a_person_whose_other_comment_the_owner_answered_by_hand_still_gets_the_dm(self):
        """Meta lists the owner's hand-typed reply under a comment (`replies{id,from}`), also under a comment
        that never asked for anything. The everyday case: somebody asks «а что это такое», the owner answers
        «напиши арена — пришлю ссылку», the person writes «арена».

        What SPEC.md asks: I8 "every comment ... has a row, `pending`-or-later iff should-match"; 7.1 `already`
        = "handled outside this database", `dup_user` = "same person already handled for this media"; I9
        "`dup_user` only if the same person has another comment that is handled"; section 2 "loses people
        silently". Nobody handled this person: no DM was ever sent to them on this reel.

        What happens: 8.3 rule 1 is applied to the comment that is not a request, it becomes
        `already/owner_replied`, and 7.2 step 2 then writes the person's real request off as `dup_user`.
        No DM, no problem, no alert, verdict OK. (The engine builder's hand-back lists this as gap 1:
        "matching or not. This may conflict with invariant I8".)
        """
        cases = {
            "a comment without the keyword": "а что это такое",
            "a question about the keyword (near:question)": "а как получить арену",
        }
        for name, first_text in cases.items():
            with self.subTest(name):
                self.boot([
                    comment("first", first_text, user="ann", age=10 * MIN, should_match=False,
                            replies=[{"text": "напиши арена — пришлю ссылку", "from": OWNER, "age_sec": 5 * MIN}]),
                    comment("request", "арена", user="ann", age=MIN),
                ])
                self.cycles(3)
                self.assertEqual(self.posts(), [("dm", "request"), ("public", "request")],
                                 f"statuses: {self.statuses()}")
                self.assertEqual(self.dms(), ["request"])
                self.assertEqual(self.status("request"), ("done", ""))

    def test_requests_meta_lists_without_an_author_or_a_usable_timestamp_are_announced(self):
        """Meta lists a request the bot cannot serve: no `from` (meta-api-facts C9: "treat a comment without
        from.id as a first-class case"; C8: the author needs the manage-comments permission; chatmany L27 [V]:
        "Graph omits from.id for some commenters. Looks identical to a keyword miss from outside"), or a
        timestamp it cannot read (CX-54: "not silently dropped: explicit status or alert"). When that is every
        comment (a permission that went away, a changed format) the bot reads the reel, recognises every
        request, and sends nothing.

        What SPEC.md asks: G5 "every state in which people are waiting or being lost is announced and
        re-announced until it ends"; section 9: `problems()` "is the ONE definition of something is wrong";
        section 2: a bug "loses people silently".

        What happens: the requests sit in `seen/no_author` or `seen/malformed` (an explicit status, as 5 and
        8.3 say), but `problems()` is empty, the verdict is "OK: никто не ждёт", and nothing but the start-up
        line ever reaches Telegram. The only trace is a count deep in the report.
        """
        shapes = {
            "no `from` on any comment": {"from": None},
            "no timestamp": {"raw": {"timestamp": None}},
            "timestamp as unix seconds": {"raw": {"timestamp": int(START) - 600}},
        }
        for name, change in shapes.items():
            with self.subTest(name):
                self.boot([{**comment(f"c{n}", age=10 * MIN + n), **change} for n in range(5)])
                self.cycle()
                self.cycles(2, every=31 * MIN)     # past the 30 min of "no ok DM", and a second full sweep
                if sorted(self.dms()) == [f"c{n}" for n in range(5)]:
                    continue                       # served after all: nothing to announce
                self.assertEqual(self.dms(), [])
                told = [line for line in self.alerts() if not line.startswith(("ℹ fresh_db", "ℹ config_changed"))]
                self.assertTrue(self.problems() or told,
                                f"5 people asked and got nothing ({self.statuses()['c0']}), yet no problem is "
                                f"raised and nothing was said on Telegram; verdict: {self.verdict()!r}")
                self.assertFalse(self.verdict().startswith("OK"), self.verdict())

    def test_a_hidden_comment_gets_no_public_post(self):
        """meta-api-facts D2 [V] / C11: "cannot reply to hidden comments"; the list says which ones are hidden
        (`hidden=true`), and SPEC 6 asks Meta for that field in every page. After its DM a hidden comment is
        `done`/`public_skipped` without a POST (7.4); the next visible commenter gets a public reply as usual."""
        self.boot([comment("h1", age=400, hidden=True), comment("h2", age=300, hidden=True),
                   comment("h3", age=200, hidden=True), comment("visible", age=100)])
        self.cycles(6)
        self.assertIn("visible", self.dms())
        self.assertEqual(self.status("visible"), ("done", ""),
                         f"attempts: {self.attempts('comment_id, kind, outcome')}")
        self.assertEqual(self.replies(), ["visible"])
        self.assertEqual([target for kind, target in self.posts() if kind == "public"], ["visible"],
                         "a public reply was POSTed to a comment Meta listed as hidden")
        self.assertEqual({self.status(c) for c in ("h1", "h2", "h3")}, {("done", "public_skipped")})
        self.assertEqual([c for c, kind, _ in self.attempts("comment_id, kind, outcome") if kind == "public"],
                         ["visible"])              # 7.4: a skipped public reply leaves no row in the journal

    def test_the_same_refusal_for_every_second_person_is_announced(self):
        """meta-api-facts A6: Instagram may refuse a message to everybody who does not follow the
        account, "and the failed call still consumes the comment's single private reply". The scenario seed
        for it (same file, "What a fake Meta must be able to do", item 3) expects "explicit status; alert if
        it repeats". Classification rule 3 there: such an error "is really an account-level problem".

        What SPEC.md asks: G5 "every state in which people are ... being lost is announced and re-announced
        until it ends".

        What happens: every second person is written off as `refused/invalid`, for good. The only breaker is
        "5 DM attempts in a row without ok" (8.5), and followers in between reset it: no hold, no problem, no
        event (9 knows events for `expired` and `uncertain` only), verdict "OK: никто не ждёт". Half of the
        audience is lost and the owner is told nothing.
        """
        people = [comment(f"c{n:02}", user=("guest" if n % 2 == 0 else "follower") + str(n), age=1000 - n)
                  for n in range(16)]
        self.boot(people)
        for n in range(0, 16, 2):   # the people who do not follow the account
            self.fault(kind="dm", target=f"c{n:02}", http=400, code=100, subcode=2534015,
                       message="(#100) Invalid message data", deliver=False, consume=True)
        self.cycles(24)
        asked = [target for kind, target in self.posts() if kind == "dm"]
        self.assertEqual(len(asked), len(set(asked)))                       # nobody is asked twice (G4): holds
        refused = [one for one, status in self.statuses().items() if status == ("refused", "invalid")]
        self.assertGreaterEqual(len(refused), 5, self.statuses())           # the scenario did happen
        told = [line for line in self.alerts() if not line.startswith(("ℹ fresh_db", "ℹ config_changed"))]
        self.assertTrue(self.problems() or told,
                        f"{len(refused)} people were refused for the same reason and are lost for good, yet no "
                        f"problem is raised and nothing was said on Telegram; verdict: {self.verdict()!r}")

    def test_a_comment_line_in_env_that_is_not_utf8_does_not_stop_the_bot(self):
        """SPEC 3: the `.env` parser "ignores `#` lines", and the file "is re-read every cycle". SPEC 8.1:
        "`expire()` and the problems/notify step run in EVERY cycle".

        The owner adds a note above the token in an editor that saves cp1251 (Windows): «# токен от 5 октября».
        The three values below it are untouched and valid.

        What happens: `engine.read_env` decodes the whole file as UTF-8 before it looks at any line and only
        expects FileNotFoundError. Every cycle dies at the top (`_token_and_identity`), before collect,
        expire and send: no call to Meta at all although the stored token is fine. The notifier reads its
        Telegram secrets through the same function, so the `cycle_error` problem is queued and never leaves:
        the service is dead and silent, with systemd's watchdog still being fed.
        """
        self.boot([comment("c1", age=100), comment("old", age=7 * DAY - 11 * MIN)], budget=None)
        self.cycle()                                # token adopted and stored, both comments pending, budget 0
        self.assertEqual(self.statuses(), {"c1": ("pending", ""), "old": ("pending", "")})
        store.arm(self.conn, "unlimited")
        self.write_env("# токен от 5 октября\n".encode("cp1251")
                       + f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN={TG_TOKEN}\nTG_CHAT_ID={TG_CHAT}\n".encode())
        self.add(comment("c2", age=0))
        before, told = self.seq(), len(self.alerts())
        self.cycles(3)
        self.assertIn("comments", self.meta_calls(since=before),
                      f"the reel was not read any more; last_cycle_error: {self.kv('last_cycle_error')}")
        self.assertEqual(self.status("old"), ("expired", "window"))        # 8.1: expire() in EVERY cycle
        self.assertEqual(sorted(self.dms()), ["c1", "c2"])
        self.assertGreater(len(self.alerts()), told, "nothing reached Telegram any more")
        self.assertIsNone(self.kv("last_cycle_error"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a file of mode 000")
    def test_an_env_file_the_service_user_cannot_read_does_not_stop_the_bot(self):
        """The same hole from the other side (SPEC is silent): `.env` is replaced by root (`sudo sed -i`, `mv`)
        and is now mode 600 of another user. The bot has its token in the database and needs nothing from the
        file, yet every cycle ends with PermissionError before collect, expire and send, and no alert can
        leave. The safest reading: an unreadable `.env` brings no new token; reads, expiry and DMs go on with
        the stored one.
        """
        self.boot([comment("c1", age=100), comment("old", age=7 * DAY - 11 * MIN)], budget=None)
        self.cycle()
        store.arm(self.conn, "unlimited")
        os.chmod(os.path.join(self.dir, ".env"), 0)
        self.add(comment("c2", age=0))
        before = self.seq()
        self.cycles(3)
        self.assertIn("comments", self.meta_calls(since=before),
                      f"the reel was not read any more; last_cycle_error: {self.kv('last_cycle_error')}")
        self.assertEqual(self.status("old"), ("expired", "window"))        # 8.1: expire() in EVERY cycle
        self.assertEqual(sorted(self.dms()), ["c1", "c2"])


# ====================================================================================================================
# Operations: the real bot.py as a subprocess, a scaled clock, its own STATE_DIR
# ====================================================================================================================

class OpsCase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ops-state-")
        self.logs = tempfile.mkdtemp(prefix="ops-logs-")     # not in STATE_DIR: some tests make that unwritable
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.addCleanup(shutil.rmtree, self.state, True)
        self.fake = None
        self.services = []

    def tearDown(self):
        for process in self.services:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.reader.join(5)
            process.stderr.close()
        if self.fake:
            strict = [r["strict"] for r in self.requests() if r["strict"]]
            self.fake.stop()
            self.assertEqual(strict, [])   # the bot never made a call that is not one of SPEC section 6

    # ---- the world of one test

    def world(self, comments=(), env=ENV, scale=SCALE, token=TOKEN, tg_token=TG_TOKEN, **config_changes):
        anchor = anchor_for(scale) if scale != 1 else None
        self.fake = FakeMeta({"account": ME, "token": token, "telegram": {"token": tg_token, "chat_id": 42},
                              "media": [{"id": MEDIA, "permalink": "https://www.instagram.com/reel/DAbc/",
                                         "shortcode": "DAbc", "comments": list(comments)}]},
                             scale=float(scale), anchor=anchor).start()
        self.env = {"PATH": os.environ.get("PATH", ""), "STATE_DIR": self.state, "IG_API_BASE": self.fake.api_base,
                    "TG_API_BASE": self.fake.api_base, "PYTHONDONTWRITEBYTECODE": "1"}
        if scale != 1:
            self.env.update(CLOCK_SCALE=str(scale), CLOCK_ANCHOR=repr(anchor))
        alerts = {"repeat_min": config_changes.pop("repeat_min", 120), "digest_hour_utc": 23}
        self.write("config.json", json.dumps({**EXAMPLE, "sending": True, "poll_interval_sec": 30,
                                              "max_sends_per_hour": 300, "alerts": alerts, **config_changes}))
        if env is not None:
            self.write(".env", env)

    def path(self, name):
        return os.path.join(self.state, name)

    def write(self, name, data):
        with open(self.path(name), "wb") as file:
            file.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def bot(self, *args, timeout=60):
        """One command run to its end: (exit code, stdout, stderr)."""
        done = subprocess.run([sys.executable, "-B", BOT, *args], env=self.env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return done.returncode, done.stdout, done.stderr

    def service(self, *args, file_limit=None, notify_socket=None):
        """`bot.py run` (or another command) left running; its stderr is collected in process.lines."""
        command = [sys.executable, "-B", BOT, *(args or ("run",))]
        if file_limit:
            command = [sys.executable, "-c", FILE_LIMIT_LAUNCHER, str(file_limit), BOT, *(args or ("run",))]
        env = {**self.env, **({"NOTIFY_SOCKET": notify_socket} if notify_socket else {})}
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
        process.lines = []

        def read():
            for line in process.stderr:
                process.lines.append(line)
        process.reader = threading.Thread(target=read, daemon=True)
        process.reader.start()
        self.services.append(process)
        return process

    def wait(self, what, check, seconds=30, must=True):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.02)
        if must:
            logs = "".join("".join(process.lines)[-3000:] for process in self.services)
            self.fail(f"timed out waiting for {what}\n{logs}")
        return False

    # ---- what an operator can see

    def requests(self, *kinds):
        return [r for r in self.fake.control("/truth")["requests"] if not kinds or r["kind"] in kinds]

    def telegram(self):
        return [message["text"] for message in self.fake.control("/truth")["telegram"]]

    def rows(self, sql, args=()):
        """Rows from the bot's database, read-only; [] while there is no database or no such table."""
        path = self.path("core.db")
        if not os.path.exists(path):
            return []
        conn = sqlite3.connect(pathlib.Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def kv(self, key):
        try:
            found = self.rows("SELECT value FROM kv WHERE key=?", (key,))
        except sqlite3.DatabaseError as error:   # for the failure messages: a broken database is a finding, not a crash
            return f"unreadable ({error})"
        return found[0][0] if found else None


class DatabaseCannotBeWritten(OpsCase):
    """G5: "every state in which people are waiting or being lost is announced and re-announced until it ends".

    Every alert goes through table `outbox` (SPEC 9). When the database cannot be written, no alert can be
    queued: the bot stays up, every cycle fails, sleep slices keep pinging the watchdog, systemd shows
    `active (running)`, and Telegram hears nothing for as long as it lasts.
    """

    FILE_LIMIT = 250_000   # bytes: room for about ten cycles of WAL, then the "disk" is full

    def test_a_full_disk_is_announced_while_people_wait(self):
        self.world([comment(f"c{n}", age=4000 - n) for n in range(30)], repeat_min=60)
        self.assertEqual(self.bot("once")[0], 0)                     # 30 people wait: the budget of a new database is 0
        self.assertIn("⚠ waiting", "\n".join(self.telegram()))       # announced, to be repeated every 60 min (6 s here)

        service = self.service(file_limit=self.FILE_LIMIT)
        wal, seen = self.path("core.db-wal"), [0, time.monotonic()]

        def full():   # the WAL has stopped short of the limit and has not grown for half a second (ten cycles)
            size = os.path.getsize(wal) if os.path.exists(wal) else 0
            if size != seen[0]:
                seen[:] = size, time.monotonic()
            return size > self.FILE_LIMIT - 40_000 and time.monotonic() - seen[1] > 0.5
        self.wait("the disk to be full", full)
        self.assertIn("ℹ started", "\n".join(self.telegram()))       # the bot worked until then
        told = len(self.telegram())

        time.sleep(10)                                               # 100 minutes of the bot's time, 200 cycles
        self.assertIsNone(service.poll(), "".join(service.lines)[-2000:])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM comments WHERE status='pending'"), [(30,)])
        failed = sum("cycle_error" in line for line in service.lines)
        self.assertGreater(len(self.telegram()), told,
                           f"30 people wait, {failed} cycles failed in a row ({(service.lines or [''])[-1].strip()}), "
                           f"the service is up, and Telegram was told nothing: neither the failure nor `waiting` again")

    @unittest.skipIf(os.geteuid() == 0, "root can write a read-only file")
    def test_a_database_file_the_service_user_cannot_write_is_announced(self):
        # e.g. core.db put back by root (`sudo cp`): owned by root, readable by igbot, not writable
        self.world([comment(f"c{n}", age=4000 - n) for n in range(30)])
        self.assertEqual(self.bot("once")[0], 0)
        told = len(self.telegram())
        os.chmod(self.path("core.db"), 0o444)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as systemd:
            systemd.bind(os.path.join(self.logs, "notify"))
            systemd.settimeout(0.01)
            service = self.service(notify_socket=os.path.join(self.logs, "notify"))
            time.sleep(4)                                            # 80 cycles
            heard = []
            try:
                while True:
                    heard.append(systemd.recv(100))
            except OSError:
                pass
        state = (f"service {'runs' if service.poll() is None else 'exited ' + str(service.returncode)}; systemd got "
                 f"READY=1 {heard.count(b'READY=1')} time(s) and {heard.count(b'WATCHDOG=1')} watchdog pings; "
                 f"{sum('Traceback' in line for line in service.lines)} tracebacks, the last line: "
                 f"{(service.lines or [''])[-1].strip()[:160]}")
        self.assertGreater(len(self.telegram()), told, f"nothing can be written and nobody was told. {state}")


class DatabaseCannotBeOpened(OpsCase):
    """SPEC 3 announces the one start-up failure it knows (`fatal_start`, exit 4). A database that cannot be
    opened ends `run` with exit 1 before anything is announced; Restart=always starts it again every 30 s, for
    ever, and each start dies the same way. G5: the people who wait are never mentioned again.
    """

    def test_a_truncated_database_at_start_is_announced(self):
        self.world([comment(f"c{n}", age=4000 - n) for n in range(30)])
        self.assertEqual(self.bot("once")[0], 0)
        told = len(self.telegram())
        size = os.path.getsize(self.path("core.db"))
        os.truncate(self.path("core.db"), size // 2 // 4096 * 4096)   # a copy that was cut short
        codes = []
        for _ in range(3):                                           # systemd: Restart=always, RestartSec=30
            service = self.service()
            self.wait("an alert", lambda: len(self.telegram()) > told or service.poll() is not None, seconds=8,
                      must=False)
            codes.append(service.poll())
            if service.poll() is None:
                break
        last = (service.lines or [""])[-1].strip()
        self.assertGreater(len(self.telegram()), told,
                           f"three starts in a row ended with exit codes {codes} ({last}) and Telegram was told nothing")

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_a_state_dir_the_service_user_cannot_write_is_announced(self):
        # e.g. the directory restored or re-created by root: igbot can read .env and core.db, but not write
        self.world([comment(f"c{n}", age=4000 - n) for n in range(30)])
        self.assertEqual(self.bot("once")[0], 0)
        told = len(self.telegram())
        os.chmod(self.state, 0o500)
        self.addCleanup(os.chmod, self.state, 0o700)
        service = self.service()
        self.wait("an alert", lambda: len(self.telegram()) > told or service.poll() is not None, seconds=8, must=False)
        time.sleep(0.5)
        last = (service.lines or [""])[-1].strip()
        self.assertGreater(len(self.telegram()), told,
                           f"`run` exited {service.poll()} ({last}), systemd repeats that every 30 s, "
                           f"and Telegram was told nothing")


class StartTurnedAway(OpsCase):
    """SPEC 3: `run` exits 3 when core.lock is held, and the unit has RestartPreventExitStatus=3 4: systemd never
    starts it again. The comment in the unit says "a human must look", but only exit 4 tells a human
    (`fatal_start`). And the holder need not be a second service: `once` and `send-one` hold the same
    lock for a whole cycle (a full sweep: a minute for a big reel), after which nothing runs at all.
    """

    def test_a_start_that_meets_a_one_off_command_does_not_leave_the_service_down_in_silence(self):
        self.world([comment(f"c{n}", age=4000 - n) for n in range(30)], repeat_min=1000)
        self.assertEqual(self.bot("once")[0], 0)                     # 30 people wait; Telegram works
        told = len(self.telegram())
        hold = self.fake.control("/hold", {"add": {"kind": "comments", "phase": "before"}})["id"]
        one_off = self.service("once")                               # the operator: `igbot once`
        self.assertTrue(self.fake.control("/hold", {"wait": hold, "timeout_real_sec": 20})["caught"])
        service = self.service()                                     # systemd: boot, or RestartSec after a crash
        self.wait("the start to be decided", lambda: service.poll() is not None, seconds=3, must=False)
        self.fake.control("/hold", {"release": hold})
        self.assertEqual(one_off.wait(timeout=20), 0)                # the one-off command is done: nothing runs now
        time.sleep(1)
        self.assertTrue(service.poll() is None or len(self.telegram()) > told,
                        f"`run` exited {service.poll()} ({(service.lines or [''])[-1].strip()}): systemd will not start "
                        f"it again (RestartPreventExitStatus=3 4), 30 people wait, and Telegram was told nothing")


class EnvFile(OpsCase):
    """SPEC 3: `.env` "parser: `KEY=value` lines; strips whitespace, CR, surrounding quotes and a leading
    `export`; ignores `#` lines". The secrets file is the one file a human edits by hand on the server."""

    def collected_with(self, env):
        self.world([comment("c1")], env=env)
        code, _, err = self.bot("once")
        return (f"`once` exits {code}; hold: {self.kv('account_hold')}; "
                f"requests to Meta: {len(self.requests('me', 'media', 'comments'))}; {err.strip().splitlines()[-1][:200]}")

    def test_a_byte_order_mark_does_not_hide_the_first_secret(self):
        # "UTF-8 with BOM" is what Windows editors and some SFTP clients write; the mark is invisible
        seen = self.collected_with(b"\xef\xbb\xbf" + ENV.encode())
        self.assertEqual(self.rows("SELECT status FROM comments WHERE comment_id='c1'"), [("pending",)],
                         f"the token on the first line of .env was not read. {seen}")

    def test_a_comment_line_in_another_encoding_is_ignored_like_any_comment_line(self):
        # "# токен инстаграма" saved as Windows-1251: one stray byte in a line the parser is to ignore
        seen = self.collected_with("# токен инстаграма\n".encode("cp1251") + ENV.encode())
        self.assertEqual(self.rows("SELECT status FROM comments WHERE comment_id='c1'"), [("pending",)],
                         f"a `#` line stopped the whole cycle. {seen}")
        self.assertTrue(self.telegram(), f"... and the alert channel with it: Telegram got nothing. {seen}")


# ====================================================================================================================
# The rules of round 1 that no attack test pins (SPEC.md: 3, 4, 5, 7.1, 8.2, 8.3, 8.5, 8.7, 9)
# ====================================================================================================================

class RulesOfTheEngine(GatesCase):
    def test_the_identity_is_known_only_with_all_three_fields(self):
        """8.2: with `user_id` alone no comment of the account itself could be told from a stranger's."""
        self.boot([comment("c1")])
        self.fault(kind="me", http=200, body={"user_id": ME["user_id"], "followers_count": 1, "media_count": 1})
        self.assertEqual([self.cycle(after=SLOT) for _ in range(2)], [0, 0])
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "no_identity")
        self.assertEqual(self.requests("comments", "dm"), [])
        self.fake.control("/faults", {"clear": True})
        # the probe that completes the identity lifts the hold; the cycle after it reads and sends
        self.assertEqual([self.cycle(after=10 * MIN), self.cycle(after=SLOT)], [0, 1])

    def test_the_report_says_when_the_config_file_is_not_the_config_the_service_runs_with(self):
        """4 and 9: problem `restart_needed` while the hash of the file `report` was given is not kv `config_hash`."""
        self.boot()
        self.cycle()
        self.assertEqual(config.loads(self.kv("config_running")), self.cfg)
        self.assertNotIn("restart_needed", self.problem_keys())
        running, self.cfg = self.cfg, self.make_cfg(sending=False)  # the file is edited, the service not restarted
        self.assertIn("restart_needed", self.problem_keys())        # what `bot.py report` is given: the file
        self.cfg = running
        self.cycle(after=SLOT)
        self.assertNotIn("restart_needed", self.problem_keys())     # the service's own problems know one config

    def test_the_slot_is_also_kept_by_a_clock_the_journal_does_not_have(self):
        """7.2 step 4: `slot_wait` is what the engine knows from the monotonic clock of this process run (G2)."""
        self.boot([comment("c1")], budget=0)
        self.cycle()
        store.arm(self.conn, 3)
        self.assertEqual(store.claim_dm(self.conn, "c1", self.cfg, self.engine.me, slot_wait=7.5),
                         store.Claim("wait", seconds=7.5))
        self.assertEqual((self.status("c1"), store.budget(self.conn), self.attempts()), (("pending", ""), 3, []))
        self.assertEqual(store.claim_dm(self.conn, "c1", self.cfg, self.engine.me).kind, "claimed")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a file of mode 000")
    def test_an_env_file_that_cannot_be_read_is_a_problem_of_its_own_and_costs_no_alert(self):
        """3: no values for that cycle, problem `env`, and the notifier keeps the secrets it read last."""
        self.boot([comment("c1")])
        path = os.path.join(self.dir, ".env")
        with open(path, "wb") as file:
            file.write(b"\xef\xbb\xbf" + f"IG_ACCESS_TOKEN={te.TOKEN}\n# \xf2\xee\xea\xe5\xed\n".encode("latin-1")
                       + b"TG_BOT_TOKEN=123:abc\nTG_CHAT_ID=42\n")
        self.assertEqual(engine.read_env_checked(path),      # the mark and the cp1251 `#` line cost nothing
                         ({"IG_ACCESS_TOKEN": te.TOKEN, "TG_BOT_TOKEN": "123:abc", "TG_CHAT_ID": "42"}, None))
        get_tg = bot.telegram(self.dir)
        self.assertEqual(get_tg(), ("123:abc", "42"))
        self.assertEqual(self.cycle(), 1)
        os.chmod(path, 0)
        self.assertEqual(engine.read_env_checked(path), ({}, ".env cannot be read: PermissionError"))
        self.assertEqual(get_tg(), ("123:abc", "42"))
        self.add(comment("c2", age=0))
        self.assertEqual(self.cycle(after=SLOT), 1)                 # the stored token serves; nothing stops
        self.assertEqual((self.kv("env_problem"), self.kv("last_cycle_error")),
                         (".env cannot be read: PermissionError", None))
        self.assertIn("env", self.problem_keys())
        os.chmod(path, 0o600)
        self.cycle(after=SLOT)
        self.assertNotIn("env", self.problem_keys())
        os.remove(path)                                             # a missing file is no problem: just no values
        self.assertEqual((engine.read_env_checked(path), get_tg()), (({}, None), (None, None)))

    def test_already_replied_answers_set_no_hold_and_end_no_streak(self):
        """8.5: the people Meta says were answered already neither count toward the streak nor end it."""
        self.boot([comment(f"c{n}", age=1000 - n, private_replied=n < 8) for n in range(10)])
        self.assertEqual([self.cycle(after=SLOT) for _ in range(10)], [0] * 8 + [1, 1])
        self.assertIsNone(store.get_send_hold(self.conn))
        self.assertEqual({self.status(f"c{n}") for n in range(8)}, {("already", "already_replied")})


class RulesOfConfigAndMatcher(unittest.TestCase):
    def load(self, reels=None, **top):
        raw = {**EXAMPLE, **top}
        if reels is not None:
            raw["reels"] = [{**EXAMPLE["reels"][0], **reel} for reel in reels]
        return config.loads(json.dumps(raw))

    def problems(self, **changes):
        with self.assertRaises(config.ConfigError) as refused:
            self.load(**changes)
        return " | ".join(refused.exception.problems)

    def test_look_alike_letters_repair_a_keyword_and_never_make_another_word(self):
        """5: (a) the form, (b) Latin look-alikes inside a Cyrillic keyword, (c) Cyrillic ones inside a Latin one."""
        matcher = match.Matcher(self.load(reels=[{"keywords": ["бот", "bot", "арена"], "also_match": []}]).reels[0])
        for text in ("бот", "bot", "BOT", "bоt", "вot", "бoт", "Aрена", "apeha", "арену"):   # Latin/Cyrillic mixed
            self.assertEqual(matcher.check(text)[0], "match", text)
        for text in ("вот", "Вот это да", "ну вот", "тор", "бота", "bots"):
            self.assertEqual(matcher.check(text)[0], "none", text)

    def test_a_keyword_its_own_reel_can_never_accept_is_refused(self):
        for word in QUESTION_WORDS + ["kak"]:
            self.assertIn("not taken for a request", self.problems(reels=[{"keywords": ["арена", word]}]), word)
        self.assertIn("also_match", self.problems(reels=[{"also_match": ["что"]}]))

    def test_whole_numbers_have_both_bounds(self):
        for key, bad, good in (("poll_interval_sec", (29, 301, 900), (30, 300)),
                               ("late_after_minutes", (-1, 10081, 2 ** 31 - 1), (0, 10080))):
            for value in bad:
                self.assertIn(key, self.problems(**{key: value}), value)
            for value in good:
                self.assertEqual(getattr(self.load(**{key: value}), key), value)
        for value in (0, 10081, 10 ** 13):
            self.assertIn("repeat_min", self.problems(alerts={"repeat_min": value, "digest_hour_utc": 6}))
        self.assertEqual(self.load(alerts={"repeat_min": 10080, "digest_hour_utc": 6}).alerts.repeat_min, 10080)

    def test_the_running_config_survives_the_round_trip(self):
        cfg = self.load(sending=True)
        self.assertEqual(config.loads(config.dump(cfg)), cfg)
        stored = config.dump(cfg), config.fingerprint(cfg)          # kv `config_running` and `config_hash`
        self.assertEqual(config.running(*stored, cfg), (cfg, False))
        edited = self.load(sending=False)
        self.assertEqual(config.running(*stored, edited), (cfg, True))               # the service's config wins
        self.assertEqual(config.running(None, None, edited), (edited, False))
        self.assertEqual(config.running("{", stored[1], edited), (edited, True))


class RulesOfReportAndAlerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.cfg = config.loads(json.dumps({**EXAMPLE, "sending": True}))
        self.media = self.cfg.reels[0].media_id

    def add(self, comment_id, text, status="seen", reason="", age=600):
        ts = clock.iso(clock.now() - timedelta(seconds=age))
        store.add_comment(self.conn, comment_id, self.media, "u_" + comment_id, "name", text, ts, status, reason)

    def keys(self):
        return [key for key, _ in report.problems(self.conn, self.cfg, clock.now())]

    def test_the_digest_always_fits_one_message_and_keeps_the_typo_list(self):
        """8.7: 30 `near` comments of 2,200 characters and 20 unmatched texts (the required test of the rule)."""
        for n in range(30):
            self.add(f"n{n}", "зачем арена " + "я" * 2188, reason="near:question")
        for n in range(20):
            for copy in range(n + 1):
                self.add(f"t{n}-{copy}", f"слово{'абвгдежзиклмнопрстуф'[n]}")
        store.add_metric(self.conn, 1234, 7)
        data = report.build(self.conn, self.cfg, clock.now())
        self.assertGreater(notify._units(report.render(data)), 4096 * 10)           # the whole report would not fit
        digest = report.digest(data)
        self.assertLessEqual(notify._units("ℹ digest: " + digest), notify.TG_MAX_UNITS)
        self.assertEqual(digest.splitlines()[0], data["verdict"])
        for text, count in data["unmatched"]:
            self.assertIn(f"  {count} × {text}", digest)
        self.assertEqual(len(data["unmatched"]), 20)
        self.assertIn("(near): 30", digest)
        self.assertIn("Подписчики: сейчас 1234", digest)
        self.assertNotIn("яяяя", digest)

    def test_a_digest_that_is_still_too_long_drops_the_counts_first_and_says_so(self):
        for n in range(20):
            self.add(f"t{n}", f"слово{'абвгдежзиклмнопрстуф'[n]}")
        data = report.build(self.conn, self.cfg, clock.now())
        data["problems"] = [[f"key{n}", "текст проблемы " * 30] for n in range(12)]
        digest = report.digest(data)
        self.assertLessEqual(notify._units(digest), report.DIGEST_UNITS)
        self.assertIn("опущено строк: 1; полный отчёт: igbot report", digest)
        self.assertEqual([line for line in digest.splitlines() if "⚠ key" in line][-1][:9], "  ⚠ key11")
        self.assertIn("  1 × словоа", digest)

    def test_unservable_counts_requests_only_and_only_inside_the_window(self):
        self.add("no_author", "арена", reason="no_author")
        self.add("odd", "хочу арену", reason="malformed")
        self.add("chatter", "привет всем", reason="malformed")          # not a request: nobody is lost
        self.add("too_old", "арена", reason="no_author", age=8 * DAY)    # past the window: nothing can be done
        (key, text), = [one for one in report.problems(self.conn, self.cfg, clock.now()) if one[0] == "unservable"]
        self.assertIn("2 просьб", text)
        self.assertIn("no_author", text)
        self.assertIn("malformed", text)
        verdict = report.build(self.conn, self.cfg, clock.now())["verdict"]
        self.assertTrue(verdict.startswith("PROBLEM: ") and "unservable" in verdict, verdict)

    def test_refusals_look_at_the_message_and_the_account_not_at_single_people(self):
        def journal(outcome, reason, n, code=100, subcode=2534015):
            for _ in range(n):
                self.conn.execute(
                    "INSERT INTO attempts (comment_id, kind, started_at, finished_at, outcome, reason, code, subcode, "
                    "message) VALUES ('x', 'dm', ?, ?, ?, ?, ?, ?, 'Invalid message data')",
                    (clock.iso(), clock.iso(), outcome, reason, code, subcode))
        journal("rejected", "user_unavailable", 9)      # one in ten cannot be written to on an ordinary day
        journal("rejected", "already_replied", 9)
        journal("rejected", "invalid", 4)
        self.assertNotIn("refusals", self.keys())
        journal("ok", "", 10)
        journal("rejected", "invalid", 1)
        self.assertIn("refusals", self.keys())           # 5 of the newest 20
        journal("ok", "", 16)
        self.assertNotIn("refusals", self.keys())        # it ends when the query no longer holds

    def test_the_streak_passes_over_already_replied(self):
        """8.5: such an answer neither counts toward the five nor ends the run."""
        refused, already, ok = ("rejected", "invalid"), ("rejected", "already_replied"), ("ok", "")
        for journal, minutes in (([refused] * 4 + [already] + [refused], 60), ([already] * 9, 0),
                                 ([refused] * 3 + [already] * 5 + [refused], 0),
                                 ([refused] * 4 + [already, ok, already], 0), ([refused] * 6 + [already], 120)):
            self.conn.execute("DELETE FROM attempts")               # set-up only: the bot never deletes
            self.conn.executemany("INSERT INTO attempts (comment_id, kind, started_at, finished_at, outcome, reason) "
                                  "VALUES ('x', 'dm', '2026-10-05T12:00:00.000Z', '2026-10-05T12:00:00.000Z', ?, ?)", journal)
            self.assertEqual(store.streak_wait(self.conn), minutes * MIN, journal)

    def test_a_batch_telegram_took_is_never_sent_again_while_it_cannot_be_marked(self):
        """9: a full disk between Telegram's ok and the mark sent the same message in every cycle."""
        fake = FakeMeta({"account": ME, "token": TOKEN, "telegram": TG, "media": []}).start()
        self.addCleanup(fake.stop)
        notifier = notify.Notifier(self.conn, lambda: (TG_TOKEN, TG_CHAT), fake.api_base)
        full = sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(store, "outbox_sent", side_effect=full), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            notifier.event("started", "one line")
            self.assertFalse(notifier.flush())
            self.assertFalse(notifier.flush())
            self.assertEqual(notifier.broken[0], "db_unwritable")
        self.assertEqual(len(fake.control("/truth")["telegram"]), 1)
        self.assertTrue(notifier.flush())                 # the disk has room again: the line is marked, not re-sent
        self.assertEqual(len(fake.control("/truth")["telegram"]), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0], 0)

    def test_a_direct_line_needs_no_database(self):
        fake = FakeMeta({"account": ME, "token": TOKEN, "telegram": TG, "media": []}).start()
        self.addCleanup(fake.stop)
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertTrue(notify.Notifier(None, lambda: (TG_TOKEN, TG_CHAT), fake.api_base).direct("k", "⚠ k: text"))
            self.assertFalse(notify.Notifier(None, lambda: (None, None), fake.api_base).direct("k", "⚠ k: text"))
        self.assertEqual([message["text"] for message in fake.control("/truth")["telegram"]], ["⚠ k: text"])


class RulesOfTheCommandLine(OpsCase):
    def test_once_on_a_database_that_cannot_be_used_ends_with_exit_6_and_one_line(self):
        self.world([comment("c1")])
        self.assertEqual(self.bot("once")[0], 0)
        told = len(self.telegram())
        os.truncate(self.path("core.db"), os.path.getsize(self.path("core.db")) // 2 // 4096 * 4096)
        code, _, err = self.bot("once")
        self.assertEqual(code, 6, err)
        self.assertIn("database unusable", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(self.telegram()), told)                # `once` has its operator at the terminal
        code, _, err = self.bot("run")
        self.assertEqual(code, 6, err)
        self.assertIn("fatal_start", "\n".join(self.telegram()[told:]))


# ====================================================================================================================
# The lessons of three open-source projects (harnesses: tests.test_lessons_*)
# ====================================================================================================================

class OpenReplyNeverConnects(lo.NeverConnects):
    def test_or02_dms_that_never_connect_while_reads_work_are_announced(self):
        self.boot([lo.comment(f"c{n}", age=600 - n) for n in range(3)])
        closed = socket.socket()                                    # a port nobody listens on: connects are refused
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        nowhere = meta.Client(f"http://127.0.0.1:{port}", self.cfg.api_version, lambda: lo.TOKEN)
        self.client.send_dm = nowhere.send_dm                       # reads reach Meta, POST /messages cannot connect
        seen = set()
        for _ in range(60):                                         # an hour and a half
            self.assertEqual(self.cycle(after=self.engine.pause), 0)
            seen |= set(self.problems())
        outcomes = {row[2:] for row in self.attempts()}
        self.assertEqual(outcomes, {("not_sent", "connect")})       # the scenario is the one meant: Meta saw no POST
        self.assertEqual((self.posts(), self.delivered()[0]), ([], []))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments WHERE status='pending'").fetchone()[0], 3)
        said = report.build(self.conn, self.cfg, clock.now())["reels"][0]["waiting_reason"]
        # three people have waited for 90 minutes, every gate is open, not one DM went out
        self.assertTrue(seen & {"waiting", "hold:send"},
                        f"nothing was announced in 90 minutes without a single DM: problems {sorted(seen)}, "
                        f"queued alerts {sorted(self.announced())}, the report gives the waiting reason {said!r}")


class OpenReplyHourlyCap(lo.Case):
    def test_or36_a_tolerated_wall_clock_step_does_not_put_two_dms_inside_one_slot(self):
        """G2 is measured where the POSTs are received, in real time. The server's clock was 4 minutes slow
        (inside the 5 minutes of SPEC section 2) and is set right between two cycles: no time passes, the wall
        clock moves 4 minutes, less than the 10 minutes the clock guard of 8.1 lets through."""
        self.boot([lo.comment(f"c{n}", age=600 - n) for n in range(4)])
        real, sent_at = self.client.send_dm, []

        def send_dm(*args):
            sent_at.append(self.time.monotonic())                   # time that really passed, whatever the wall clock says
            return real(*args)
        self.client.send_dm = send_dm
        self.assertEqual(self.cycle(), 1)
        self.time.wall += 4 * lo.MIN                                   # the clock is stepped; monotonic time is not
        self.cycle(after=5)
        self.cycle(after=5)
        gaps = [round(later - earlier, 1) for earlier, later in zip(sent_at, sent_at[1:])]
        self.assertTrue(all(gap >= lo.SLOT for gap in gaps),
                        f"POST /messages calls {gaps} real seconds apart; the slot at 60 per hour is {lo.SLOT} s")


class OpenReplyOwnerReplies(lo.Case):
    def test_or60_an_owner_reply_under_small_talk_does_not_cost_the_person_the_link(self):
        """The owner answers a question that is not a request ("is it free?" - "yes, write арена"). The person
        then writes the keyword. Nobody ever sent them a DM, so they must get it (SPEC section 1)."""
        with self.subTest("the question and its answer come first, then the keyword"):
            self.boot([lo.comment("question", "это платно?", user="ann", age=10 * lo.MIN, should_match=False,
                               replies=[{"text": "Нет, бесплатно! Напиши слово в комментариях 🙂", "from": lo.OWNER}]),
                       lo.comment("request", "арена", user="ann", age=lo.MIN)])
            for _ in range(4):
                self.cycle(after=31 * lo.MIN)
            self.assertEqual(self.delivered()[0], ["request"],
                             f"ann asked for the link and got nothing: question={self.status('question')}, "
                             f"request={self.status('request')}, problems={sorted(self.problems())}")
        with self.subTest("the keyword comes first and waits, then the person chats with the owner"):
            self.boot([lo.comment("request", "арена", user="bob", age=10 * lo.MIN)], budget=None)
            self.cycle()
            self.assertEqual(self.status("request"), ("pending", ""))
            self.add(lo.comment("thanks", "жду, спасибо!", user="bob", age=0, should_match=False))
            self.fake.control("/comments", {"owner_reply": "thanks", "text": "Скоро пришлю!"})
            store.arm(self.conn, "unlimited")
            for _ in range(4):
                self.cycle(after=31 * lo.MIN)
            self.assertEqual(self.delivered()[0], ["request"],
                             f"bob asked for the link and got nothing: request={self.status('request')}, "
                             f"thanks={self.status('thanks')}, problems={sorted(self.problems())}")


class OpenReplyLiveness(lo.Liveness):
    def test_or69_a_database_that_cannot_be_written_is_not_a_silent_outage(self):
        self.boot([lo.comment(f"c{n}", age=600 - n) for n in range(3)])
        self.assertEqual(self.cycle(), 1)                           # healthy: one DM, two people wait for their slots
        self.assertTrue(self.telegram())                            # and Telegram works
        heard_before = len(self.telegram())
        self.unwritable()   # the disk is full (or the file system went read-only, or a foreign writer keeps the lock)
        for _ in range(40):
            self.assertEqual(self.cycle(after=self.engine.pause), 0)   # an hour: every cycle fails, the loop goes on
        self.assertEqual(len(self.requests("dm")), 1)
        heard = self.telegram()[heard_before:]
        self.assertTrue(heard, "two people waited for an hour while every cycle failed (the database cannot be "
                               "written); the bot could reach Telegram and told it nothing")


class ChatManyAlreadyAnsweredPeopleStopTheQueue(lcm.AlreadyAnsweredPeopleStopTheQueue):
    def test_d2_people_the_owner_answered_by_hand_stop_the_queue_for_hours(self):
        """While the bot was disarmed the owner answered six people by hand with a private
        reply (the fake's `private_replied`: "the one private reply was used before this run"). The bot is
        armed; three more people wait behind them. Nine POSTs are nine minutes of work."""
        self.world([lcm.comment(f"hand{n}", age=3 * lcm.HOUR - n, private_replied=True) for n in range(6)]
                   + [lcm.comment(f"new{n}", age=10 * lcm.MIN - n) for n in range(3)])
        took = self.hours_until(lambda: len(self.served("new")) == 3, limit=12 * lcm.HOUR)
        self.assertEqual({self.status(f"hand{n}") for n in range(6)}, {("already", "already_replied")})   # control
        self.assertEqual(self.served("hand"), [])                                                        # control
        self.assertIsNotNone(took, f"not served within 12 hours: {self.statuses()}")
        self.assertLess(
            took, 1.0,
            f"the three people who really wait got their DM after {took:.1f} hours: six 'already replied' "
            f"answers in a row (six people who HAVE their reply) were counted as a streak of failures; "
            f"holds announced: {[line[:70] for line in self.told('hold:send')]}")


class ChatManyServedPeopleAreCountedAsWaiting(lcm.ServedPeopleAreCountedAsWaiting):
    def test_l3_a_person_who_has_the_dm_and_comments_again_is_not_a_person_waiting(self):
        self.world([lcm.comment("a1", user="ann", age=5 * lcm.MIN)])
        self.assertEqual(self.cycle(), 1)                 # ann has her DM
        store.arm(self.conn, 0)                           # `disarm`
        self.add(lcm.comment("b1", user="bob", age=2 * lcm.MIN), lcm.comment("a2", user="ann", age=30))
        self.run_for(35 * lcm.MIN)                            # past the 30 minutes of `waiting`
        self.assertEqual((self.dms_to("ann"), self.dms_to("bob")), (["a1"], []))     # control: only bob waits
        line = self.problems().get("waiting", "")
        self.assertEqual(
            self.build()["reels"][0]["people_waiting"], 1,
            f"one person waits (bob); ann got her DM for this reel and will never get another (G1), yet the "
            f"report counts two people waiting and Telegram was told: {line[:60]!r}; rows: {self.statuses()}")


class ChatManyAPollInWhichEveryCommentFailsReadsHealthy(lcm.APollInWhichEveryCommentFailsReadsHealthy):
    def test_f2_meta_changes_the_shape_of_every_comment_and_the_bot_says_ok(self):
        # every request arrives in a shape the handler raises on: `replies` as a bare list
        self.world([lcm.comment(f"c{n}", age=10 * lcm.MIN + n, raw={"replies": []}) for n in range(5)])
        self.run_for(2 * lcm.HOUR)                            # four full sweeps
        self.assertEqual(set(self.statuses().values()), {("seen", "malformed")})     # control: 8.3 as written
        self.assertEqual(self.dms(), [])
        alerts = [line for line in self.telegram() if line.startswith("⚠")]
        self.assertTrue(
            self.problems() or alerts,
            f"five people asked for the link two hours ago, none was answered, every sweep failed on every "
            f"comment, and the bot reports nothing: verdict {self.build()['verdict']!r}, problems {self.problems()}")


class ChatManyAPauseSwitchLeftOn(lcm.APauseSwitchLeftOn):
    def test_e11_people_under_a_switched_off_reel_are_lost_without_a_word(self):
        self.world([lcm.comment(f"c{n}", age=6 * lcm.DAY + 16 * lcm.HOUR + n * lcm.HOUR) for n in range(3)], budget=None)
        self.cycle()
        self.assertEqual(set(self.statuses().values()), {("pending", "")})
        self.start(reels=[{"active": False}])             # the reel is switched off and forgotten
        store.arm(self.conn, "unlimited")                 # every other gate is open
        mark = len(self.truth()["telegram"])
        self.run_for(2 * lcm.HOUR)
        self.assertEqual(set(self.statuses().values()), {("pending", "")})           # they wait, 4-7 h left
        heard = [line[:60] for line in self.telegram(mark) if line.startswith("⚠")]
        self.assertTrue(
            self.problems() or heard,
            f"three people are `pending`, the first is lost in under five hours, and nothing is announced: "
            f"problems {self.problems()}, verdict {self.build()['verdict']!r}")


class ChatBotXFindingsNothingIsRead(lcx.World):
    def test_cx70_a_bot_that_reads_no_reel_at_all_says_ok_for_ever(self):
        """CX-70: "no reel configured / config type wrong, so nothing ever matches; the bot runs for a day;
        this is detected and alerted".

        The scenario: after a config edit and a restart no reel is read any more. Either the only reel carries
        `"active": false` (the per-reel STOP of this version, left behind), or `"reels"` is empty. The config
        loads, the service runs, people go on writing the keyword under the reel, and none of them is even
        stored: while the reel stays off there is no row, no `waiting`, no `expiring` and no `expired` event
        for them. With one reel in the config this is the STOP file of v1 again (inventory A5: "nothing alerts
        on a forgotten STOP: the real one lay there for two days"). The sibling findings about a switched-off
        reel are about people ALREADY in the queue; announcing those rows would not cover this.

        What SPEC.md asks: G5 "every state in which people are waiting or being lost is announced and
        re-announced until it ends"; section 9, `reel:<media_id>`: "state not ok, or 3 failed sweeps in a row,
        or no ok full sweep for 2 h while there is no account hold" (the reel of the first variant is in the
        config, has had no full sweep for 26 hours, and there is no account hold). For an empty `reels` list
        SPEC.md is silent. SPEC 4 only says that the ROWS of an inactive reel are "reported as parked".

        What happens: `problems()` is empty for the whole day, the verdict and the daily digest say
        "OK: никто не ждёт", the report shows the switched-off reel with "состояние ok". The one hint is the
        number at the end of the start line ("reels 0"), said once.
        """
        variants = {
            "the only reel is switched off": [{"name": "arena", "media_id": lcx.MEDIA, "active": False}],
            "no reel in the config": [],
        }
        for name, reels in variants.items():
            with self.subTest(name):
                self.boot([lcx.comment("c1")])
                self.serve(10 * lcx.MIN)
                self.assertEqual((self.dms(), self.problems()), (["c1"], []))          # control: a working bot
                self.start(reels=reels)                                               # the config edit, the restart
                mark = self.seq()
                self.add(*(lcx.comment(f"n{n}", age=0) for n in range(5)))                # five people ask
                self.serve(26 * lcx.HOUR)

                self.assertEqual(self.meta_calls(mark).count("comments"), 0)          # the scenario: nothing is read
                self.assertEqual(self.dms(), ["c1"])
                self.assertEqual([one for one in self.statuses() if one.startswith("n")], [])
                heard = self.alerts(mark)
                self.assertIn("ℹ started: sending on, budget 0, reels 0", heard)          # a changed config disarms
                warned = [line for line in heard if line.startswith("⚠")]
                data = self.build()
                self.assertTrue(
                    data["problems"] or warned,
                    f"for 26 hours no reel was read and 5 people who asked were not even stored; problems() is "
                    f"empty, Telegram heard no ⚠ line, the verdict is {data['verdict']!r} and the digest began "
                    f"{[line for line in heard if line.startswith('ℹ digest')]}")


class ChatBotXFindingsDigest(lcx.World):
    def test_the_daily_digest_still_names_the_word_people_write_instead_of_the_keyword(self):
        """CX-70, the everyday form: the keyword works, and a typo of it that is not in `also_match` is written
        by many people, who get nothing. SPEC 5 has one way to notice: "unknown typos are found by a human in
        the report's list of frequent short unmatched texts and added to also_match". The daily digest
        (SPEC 8.7) is the one copy of the report that reaches the owner without a login to the server.

        What happens: the digest is the whole report as ONE Telegram message, cut at 4096 characters; the list
        of unmatched texts is at its end, behind the 30 newest `near` comments, each printed with its full
        text. `near` rows are never pruned, so once a reel has seen about 30 ordinary long comments that
        mention the keyword, the list is cut off from every digest from then on. 40 people wrote «арнеа» here;
        Telegram never shows the word. (The integrator's hand-back lists "the digest is the whole report, cut
        at 4096 units" as an unresolved spec gap; SPEC.md says nothing about what the digest must contain:
        guarantee "spec".)
        """
        chatter = "Спасибо за ролик про арена, уже поставил себе и всё отлично работает, жду новых видео про нейросети"

        def a_day(long_comments):
            rows = [lcx.comment(f"t{n:02}", "арнеа", age=3 * lcx.HOUR + n * 60, should_match=False) for n in range(40)]
            rows += [lcx.comment(f"n{n:02}", chatter, age=2 * lcx.HOUR + n * 60, should_match=False)
                     for n in range(long_comments)]
            rows += [lcx.comment(f"c{n}", age=600 + n) for n in range(3)]
            self.boot(rows, alerts={"repeat_min": 120, "digest_hour_utc": 13})
            self.serve(70 * lcx.MIN)                                             # 12:00 .. 13:10: the digest is due at 13:00
            self.assertEqual(sorted(self.dms()), ["c0", "c1", "c2"])
            data = self.build()
            self.assertEqual((data["unmatched"][0], data["near"]["total"]), (["арнеа", 40], long_comments))
            self.assertIn("40 × арнеа", report.render(data))                 # `igbot report` on the server has it
            heard = [message["text"] for message in self.truth()["telegram"]]
            self.assertEqual(len([text for text in heard if "ℹ digest" in text]), 1)     # the digest did go out
            return heard                                                     # everything Telegram got that day

        self.assertTrue(any("арнеа" in text for text in a_day(long_comments=5)))        # control: a young reel
        heard = a_day(long_comments=30)
        (digest,) = [text for text in heard if "ℹ digest" in text]
        self.assertTrue(any("арнеа" in text for text in heard),
                        f"40 people wrote «арнеа» and got nothing; the report on the server lists it first among "
                        f"the unmatched texts, and nothing Telegram received names it: the digest "
                        f"({len(digest)} characters) begins {digest[:27]!r} and ends {digest[-60:]!r}")


if __name__ == "__main__":
    unittest.main()
