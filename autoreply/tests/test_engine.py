"""SPEC section 8 (the engine): Engine.cycle() against the in-process fake Meta over real loopback HTTP.

The fake keeps its own ground truth (sim/README.md); every test judges the bot by what the fake received and by
the database, never by what the bot says it did. Time is in the test's hands: wall time is the real clock plus
an offset that both the bot and the fake read, clock.sleep() moves the offset instead of sleeping.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import clock
import config
import engine
import meta
import store
from sim.fake_meta import FakeMeta

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN, HOUR, DAY = 60, 3600, 86400
START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
SLOT = 60   # seconds between two DMs at 60 per hour
ME = {"user_id": "17841400000000001", "id": "9990000000000001", "username": "your_account",
      "followers_count": 1234, "media_count": 7}
MEDIA, MEDIA_2 = "17900000000000101", "18000000000000002"
TOKEN = "IGAAtoken1"
with open(os.path.join(CODE, "config.example.json"), encoding="utf-8") as _file:
    EXAMPLE = json.load(_file)


def comment(comment_id, text="арена", user=None, age=60, **more):
    """A scenario comment; by default a fresh request by a person of its own."""
    user = user or "u_" + comment_id
    return {"id": comment_id, "text": text, "age_sec": age, "from": {"id": user, "username": "name_" + user},
            "should_match": True, **more}


def media(media_id=MEDIA, comments=(), shortcode="DAbc"):
    return {"id": media_id, "permalink": f"https://www.instagram.com/reel/{shortcode}/", "shortcode": shortcode,
            "comments": list(comments)}


class Time:
    """Wall time = START + real time since the test began + what the test added; monotonic time likewise."""

    def __init__(self):
        self._time, self._monotonic = time.time, time.monotonic
        self._began, self.wall, self.mono, self.slept = time.time(), 0.0, 0.0, []
        self.slow = 0   # seconds every sleep takes longer than asked: a slow network

    def time(self):
        return START + (self._time() - self._began) + self.wall

    def monotonic(self):
        return self._monotonic() + self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds, should_stop=None, ping=None):
        """Stands in for clock.sleep: the time passes at once."""
        self.slept.append(seconds)
        if not (should_stop and should_stop()):
            self.advance(seconds + self.slow)


class Notifier:
    """Stands in for notify.Notifier: records what the engine tells it."""

    def __init__(self):
        self.events, self.synced, self.flushed = [], [], 0

    def sync(self, problems, now):
        self.synced.append(list(problems))

    def event(self, key, text):
        self.events.append((key, text))

    def flush(self):
        self.flushed += 1


class Kill(BaseException):
    """kill -9: nothing after this point runs."""


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.time = Time()
        self.report = types.ModuleType("report")
        self.report.problems = lambda conn, cfg, now: [("stub", "a problem")]
        self.report.build = lambda conn, cfg, now: {"built_at": clock.iso(now)}
        self.report.render = lambda data: "report of " + data["built_at"]
        self.report.digest = lambda data: "report of " + data["built_at"]
        self.lines, write = [], engine.log   # (event, fields) of every line the engine logs

        def log(level, event, **fields):
            self.lines.append((event, fields))
            write(level, event, **fields)
        for patch in (mock.patch("time.time", self.time.time), mock.patch.object(clock, "monotonic", self.time.monotonic),
                      mock.patch.object(clock, "sleep", self.time.sleep), mock.patch.object(clock, "SCALE", 1.0),
                      mock.patch.dict(sys.modules, {"report": self.report}), mock.patch.object(engine, "log", log),
                      mock.patch.object(sys, "stderr", self.enterContext(open(os.devnull, "w")))):
            patch.start()
            self.addCleanup(patch.stop)
        self.fake = None

    def tearDown(self):
        if self.fake:   # a correct bot never makes a call that is not one of SPEC section 6
            self.assertEqual([r["strict"] for r in self.requests() if r["strict"]], [])

    # ---- building a bot

    def make_cfg(self, reels=None, **changes):
        raw = {**EXAMPLE, "sending": True, **changes}
        if reels is not None:
            raw["reels"] = [{**EXAMPLE["reels"][0], **reel} for reel in reels]
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(raw, file)
        return config.load(path)

    def boot(self, comments=(), budget="unlimited", env_token=TOKEN, scenario=None, **cfg_changes):
        """A fake Meta with one reel and these comments, an empty state directory, and an engine that was just started."""
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = FakeMeta({"account": ME, "token": TOKEN, "media": [media(comments=comments)],
                              **(scenario or {})}).start()
        self.addCleanup(self.fake.stop)
        self.write_env(env_token)
        self.cfg = self.make_cfg(**cfg_changes)
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.stop = False
        self.restart()
        if budget is not None:
            store.arm(self.conn, budget)   # after the start: one with a config the database has not seen disarms
        return self.engine

    def restart(self, **cfg_changes):
        """A new process run on the same state: nothing in memory survives."""
        if cfg_changes:
            self.cfg = self.make_cfg(**cfg_changes)
        self.notifier = Notifier()
        self.client = meta.Client(self.fake.api_base, self.cfg.api_version,
                                  lambda: store.kv_get(self.conn, "token") or "")
        self.engine = engine.Engine(self.cfg, self.conn, self.client, self.notifier, lambda: self.stop)
        return self.engine

    def write_env(self, token):
        with open(os.path.join(self.dir, ".env"), "w", encoding="utf-8") as file:
            file.write(f"IG_ACCESS_TOKEN={token}\n" if token else "")

    def cycle(self, after=0):
        self.time.advance(after)
        return self.engine.cycle()

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

    def logged(self, event):
        """The fields of the engine's log lines of this event: `sweep` (the counters of a full sweep or of a
        head poll that stored something) or `sweep_failed`."""
        return [fields for name, fields in self.lines if name == event]

    def attempts(self, columns="comment_id, kind, outcome, reason"):
        return [tuple(row) for row in self.conn.execute(f"SELECT {columns} FROM attempts ORDER BY id")]

    def events(self, key):
        return [text for name, text in self.notifier.events if name == key]

    def hold_minutes(self):
        """(reason, whole minutes left) of the send hold in force, or None."""
        hold = store.get_send_hold(self.conn)
        return hold and (hold["reason"], round((clock.parse(hold["until"]) - clock.now()).total_seconds() / 60))


class HappyPath(EngineCase):
    def test_dm_then_public_reply_and_the_journal(self):
        self.boot([comment("c1")], budget=3)
        self.assertEqual(self.cycle(), 1)
        self.assertEqual([r["kind"] for r in self.requests()], ["me", "media", "comments", "dm", "public"])
        self.assertEqual(self.status("c1"), ("done", ""))
        truth = self.fake.control("/truth")
        (dm,), (reply,) = truth["dms"], truth["replies"]
        self.assertEqual((dm["comment_id"], dm["user_id"]), ("c1", "u_c1"))
        text = dm["message"]["text"]
        self.assertTrue(text.startswith(self.cfg.greeting + " "), text)
        self.assertIn(self.cfg.reels[0].link, text)
        self.assertIn(self.cfg.profile_url, text)
        self.assertEqual(reply["comment_id"], "c1")
        self.assertIn(reply["text"], self.cfg.public_phrases)
        journal = self.attempts("comment_id, kind, detail, http, outcome, reason, meta_id, finished_at IS NOT NULL")
        self.assertEqual(journal, [
            ("c1", "dm", "fresh", 200, "ok", "", truth["requests"][3]["response"]["message_id"], 1),
            ("c1", "public", reply["text"], 200, "ok", "", reply["id"], 1)])
        self.assertEqual(store.budget(self.conn), 2)
        self.assertEqual(len(self.time.slept), 1)   # the pause between the DM and the public reply
        self.assertTrue(2 <= self.time.slept[0] <= 4, self.time.slept)
        self.assertEqual(self.engine.pause, self.cfg.poll_interval_sec)   # nobody waits: a normal poll comes next
        self.assertEqual(self.kv("reel_state:" + MEDIA), "ok")
        self.assertIsNotNone(self.kv("last_cycle_ok_at"))
        self.assertIsNone(self.kv("last_cycle_error"))

    def test_late_greeting_for_an_old_comment(self):
        self.boot([comment("old", age=2 * HOUR)])
        self.assertEqual(self.cycle(), 1)
        text = self.fake.control("/truth")["dms"][0]["message"]["text"]
        self.assertTrue(text.startswith(self.cfg.late_greeting), text)
        self.assertEqual(self.attempts("detail")[0], ("late",))

    def test_nothing_to_do_is_one_head_poll(self):
        self.boot([comment("c1")])
        self.cycle()
        before = self.seq()
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual([r["kind"] for r in self.requests(since=before)], ["comments"])


class Gates(EngineCase):
    """G6: with any one gate closed there is no POST at all; with all of them open the same comment is sent to."""

    def closed(self, name):
        # every case starts from a database that knows the pending comment c1 and has budget 0
        self.boot([comment("c1")], budget=None, sending=name != "sending_off")
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.status("c1"), ("pending", ""))
        if name != "no_budget":
            store.arm(self.conn, "unlimited")
        if name == "disarmed":
            store.arm(self.conn, 0)
        elif name == "account_hold":
            store.set_account_hold(self.conn, "blocked", "test")
        elif name == "send_hold":
            store.set_send_hold(self.conn, "uncertain", 15 * MIN)
        elif name == "no_identity":
            store.kv_set_many(self.conn, {"me_user_id": None, "me_id": None, "me_username": None})
            self.fault(kind="me", http=500, id="me-down")
            self.restart()
        elif name == "no_full_sweep_in_this_run":
            self.fault(kind="media", http=500, id="media-down")
            self.restart()
        elif name == "collect_failed_this_cycle":
            self.fault(kind="comments", http=500, id="comments-down")
        elif name == "clock_jumped":
            self.time.wall += 11 * MIN

    def test_each_closed_gate_means_no_post(self):
        for name in ("sending_off", "no_budget", "disarmed", "account_hold", "send_hold", "no_identity",
                     "no_full_sweep_in_this_run", "collect_failed_this_cycle", "clock_jumped"):
            with self.subTest(gate=name):
                self.closed(name)
                cycles = 1 if name == "clock_jumped" else 3   # the clock guard holds for the one cycle after the jump
                self.assertEqual([self.cycle(after=SLOT) for _ in range(cycles)], [0] * cycles)
                self.assertEqual(self.posts(), [])
                self.assertEqual(self.status("c1"), ("pending", ""))
                self.assertEqual(self.attempts(), [])
                # the gate was the only thing in the way: opened, the comment is sent to
                self.fake.control("/faults", {"clear": True})
                store.kv_set_many(self.conn, {"account_hold": None, "send_hold": None})
                self.restart(sending=True)
                store.arm(self.conn, "unlimited")   # after the restart: for `sending_off` it brings another config
                self.assertEqual(self.cycle(after=SLOT), 1)
                self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])

    def test_no_identity_is_an_account_hold_until_a_probe_succeeds(self):
        self.boot([comment("c1")])
        self.fault(kind="me", http=500, id="me-down")
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "no_identity")
        self.assertEqual([r["kind"] for r in self.requests()], ["me"])   # nothing is read without the identity
        self.fake.control("/faults", {"clear": True})
        self.assertEqual(self.cycle(after=4 * MIN), 0)
        self.assertEqual(len(self.requests("me")), 1)                    # the probe is not due yet
        self.assertEqual(self.cycle(after=MIN), 0)                       # probe ok: the hold is lifted
        self.assertIsNone(store.get_account_hold(self.conn))
        self.assertEqual(self.engine.me, {key: ME[key] for key in ("user_id", "id", "username")})
        self.assertEqual(self.cycle(after=SLOT), 1)

    def test_no_dm_for_a_reel_before_its_full_sweep_completed(self):
        # the first page is stored, the second fails: the reel has pending rows but no completed sweep
        self.boot([comment(f"c{n}", age=n) for n in range(4)], scenario={"limits": {"page_size": 2}})
        self.fault(kind="comments", nth=2, http=500)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments WHERE status='pending'").fetchone()[0], 2)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.kv("reel_fails:" + MEDIA), "1")
        self.assertIsNone(self.kv("full_sweep_at:" + MEDIA))
        (failed,) = self.logged("sweep_failed")
        self.assertEqual((failed["pages"], failed["comments"]), (1, 2))
        self.assertIn("comments: transient", failed["note"])
        # the failed full sweep is repeated in the next cycle; complete, the reel is sendable
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.kv("reel_fails:" + MEDIA), "0")
        self.assertEqual(len(self.requests("media")), 2)

    def test_a_reel_whose_collect_failed_is_skipped_and_the_other_reel_is_served(self):
        reels = [{"name": "arena", "media_id": MEDIA}, {"name": "second", "media_id": MEDIA_2}]
        self.boot([comment("a1")], budget=None, reels=reels,
                  scenario={"media": [media(comments=[comment("a1")]),
                                      media(MEDIA_2, [comment("b1", age=30)], "DSecond")]})
        self.assertEqual(self.cycle(), 0)
        store.arm(self.conn, "unlimited")
        self.fault(kind="comments", target=MEDIA, http=500)
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.posts(), [("dm", "b1"), ("public", "b1")])   # a1 is older, yet its reel was not read
        self.assertEqual(self.status("a1"), ("pending", ""))
        self.assertEqual((self.kv("reel_fails:" + MEDIA), self.kv("reel_fails:" + MEDIA_2)), ("1", "0"))


class Limits(EngineCase):
    def test_spacing_one_dm_per_slot(self):
        self.boot([comment(f"c{n}", age=100 - n) for n in range(3)])
        self.assertEqual(self.cycle(), 1)
        self.assertLess(self.engine.pause, SLOT)                   # people wait: sleep only until the slot
        self.assertGreater(self.engine.pause, SLOT - 10)
        self.assertEqual(self.cycle(after=SLOT - 10), 0)           # too early: no POST
        self.assertEqual(len(self.requests("dm")), 1)
        self.assertEqual(self.cycle(after=10), 1)
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.engine.pause, self.cfg.poll_interval_sec)   # the queue is empty
        times = [r["t"] for r in self.requests("dm")]
        self.assertEqual(len(times), 3)
        self.assertTrue(all(later - earlier >= SLOT for earlier, later in zip(times, times[1:])), times)
        self.assertEqual([r["target"] for r in self.requests("dm")], ["c0", "c1", "c2"])   # oldest comment first

    def test_at_most_ten_dms_in_one_cycle_and_never_closer_than_the_slot(self):
        self.boot([comment(f"c{n:02}", age=100 - n) for n in range(12)], max_sends_per_hour=300)   # a slot of 12 s
        self.time.slow = 12   # every public reply takes so long that the next slot is free when it is done
        self.assertEqual(self.cycle(), 10)
        self.assertEqual(self.cycle(after=12), 2)
        times = [r["t"] for r in self.requests("dm")]
        self.assertEqual(len(times), 12)
        self.assertTrue(all(later - earlier >= 12 for earlier, later in zip(times, times[1:])), times)

    def test_cap_per_rolling_hour(self):
        self.boot([comment(f"c{n}", age=100 - n) for n in range(3)], max_sends_per_hour=2)
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.cycle(after=30 * MIN), 1)
        self.assertEqual(self.cycle(after=30 * MIN), 0)            # the slot is free, the hour is full
        self.assertEqual(self.engine.pause, self.cfg.poll_interval_sec)
        self.assertEqual(len(self.requests("dm")), 2)
        self.assertEqual(self.cycle(after=clock.T["cap_margin"] + 1), 1)
        times = [r["t"] for r in self.requests("dm")]
        self.assertGreaterEqual(times[2] - times[0], HOUR)

    def test_budget_is_the_number_of_dms_and_stage_done_is_told(self):
        self.boot([comment(f"c{n}", age=100 - n) for n in range(4)], budget=2)
        self.fault(kind="dm", target="c1", http=400, code=551, deliver=False)
        self.assertEqual([self.cycle(after=SLOT) for _ in range(4)], [1, 0, 0, 0])
        self.assertEqual([r["target"] for r in self.requests("dm")], ["c0", "c1"])
        self.assertEqual(store.budget(self.conn), 0)               # the refused DM used its part of the budget too
        self.assertEqual(self.status("c2"), ("pending", ""))
        (told,) = self.events("stage_done")
        self.assertIn("budget of 2", told)
        self.assertIn("ok 1", told)
        self.assertIn("rejected/user_unavailable 1", told)

    def test_one_dm_per_person_and_media(self):
        self.boot([comment("first", user="ann", age=300)])
        self.assertEqual(self.cycle(), 1)
        self.add(comment("second", "арену", user="ann", age=200), comment("other", user="bob", age=100))
        # the second comment of ann is first in the queue: it is settled and bob is served in the same cycle
        self.assertEqual([self.cycle(after=SLOT) for _ in range(2)], [1, 0])
        self.assertEqual(self.delivered()[0], ["first", "other"])
        self.assertEqual(self.status("second"), ("dup_user", "dup:first"))


class Collect(EngineCase):
    def test_what_is_never_sent_to(self):
        cases = {
            "own_app_id": (comment("own_app_id", user=ME["id"]), ("seen", "own")),
            "own_user_id": (comment("own_user_id", user=ME["user_id"]), ("seen", "own")),
            "own_name": ({**comment("own_name"), "from": {"id": "77", "username": ME["username"]}}, ("seen", "own")),
            "no_author": ({**comment("no_author"), "from": None}, ("seen", "no_author")),
            "question": (comment("question", "зачем арена?"), ("seen", "near:question")),
            "long": (comment("long", "мне очень нужна эта арена прямо сейчас"), ("seen", "near:too_long")),
            "other": (comment("other", "красиво"), ("seen", "")),
            "name": (comment("name", "Арина"), ("seen", "")),
            "too_old": (comment("too_old", age=7 * DAY - 9 * MIN), ("expired", "window")),
            "owner_replied": (comment("owner_replied", replies=[
                {"text": "Отправил!", "from": {"id": ME["id"], "username": ME["username"]}}]),
                ("already", "owner_replied")),
        }
        self.boot([one for one, _ in cases.values()])
        self.assertEqual([self.cycle(after=SLOT) for _ in range(2)], [0, 0])
        self.assertEqual({name: self.status(name) for name in cases}, {name: want for name, (_, want) in cases.items()})
        self.assertEqual(self.posts(), [])
        first = self.logged("sweep")[0]
        self.assertEqual((first["new_rows"], first["new_pending"]), (len(cases), 0))

    def test_an_inactive_reel_is_neither_read_nor_sent_to(self):
        reels = [{"name": "arena", "media_id": MEDIA}, {"name": "second", "media_id": MEDIA_2}]
        both = {"media": [media(comments=[comment("a1")]), media(MEDIA_2, [comment("b1")], "DSecond")]}
        self.boot(reels=reels, scenario=both)
        self.assertEqual([self.cycle(after=SLOT) for _ in range(2)], [1, 1])
        self.add(comment("a2", age=0))
        self.add(comment("b2", age=0), media_id=MEDIA_2)
        self.restart(reels=[reels[0], {**reels[1], "active": False}])
        store.arm(self.conn, "unlimited")                           # a changed config disarms
        before = self.seq()
        self.assertEqual([self.cycle(after=SLOT) for _ in range(2)], [1, 0])
        self.assertEqual({r["target"] for r in self.requests(since=before)}, {MEDIA, "a2"})
        self.assertEqual((self.status("a2"), self.status("b2")), (("done", ""), None))

    def test_a_reply_by_somebody_else_changes_nothing(self):
        self.boot([comment("c1", replies=[{"text": "и мне", "from": {"id": "u9", "username": "guest"}}])])
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.status("c1"), ("done", ""))

    def test_owner_reply_seen_later_settles_pending_and_sent_without_a_post(self):
        self.boot([comment("waits"), comment("dm_only", age=200)], budget=1, public_reply=True)
        with mock.patch.object(clock, "sleep", side_effect=Kill), self.assertRaises(Kill):
            self.cycle()                                            # killed in the pause before the public reply
        self.assertEqual((self.status("dm_only"), self.status("waits")), (("sent", ""), ("pending", "")))
        for one in ("waits", "dm_only"):
            self.fake.control("/comments", {"owner_reply": one, "text": "готово"})
        self.restart()
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual(self.status("waits"), ("already", "owner_replied"))
        self.assertEqual(self.status("dm_only"), ("done", ""))
        self.assertEqual(self.posts(), [("dm", "dm_only")])

    def test_a_comment_entered_at_deployment_is_never_sent_to_and_stands_for_its_person(self):
        """SPEC 12: what v1 served is in the database before the first start, as `already` and without a journal row."""
        self.boot([comment("v1_served", user="ann", age=3000), comment("ann_again", user="ann", age=60),
                   comment("bob", user="bob", age=50)], budget=None)
        store.add_comment(self.conn, "v1_served", MEDIA, "ann", "ann", "арена", clock.iso(), "already", "migrated")
        store.arm(self.conn, "unlimited")
        for _ in range(3):
            self.cycle(after=SLOT)
        self.assertEqual([r["target"] for r in self.requests("dm")], ["bob"])
        self.assertEqual(self.status("v1_served"), ("already", "migrated"))
        self.assertEqual(self.status("ann_again"), ("dup_user", "dup:v1_served"))
        self.assertEqual(self.events("uncertain"), [])

    def test_full_sweep_reads_every_page_and_a_head_poll_only_what_is_new(self):
        self.boot([comment(f"c{n}", "красиво", age=100 + n) for n in range(5)], scenario={"limits": {"page_size": 2}})
        self.cycle()
        self.assertEqual([r["kind"] for r in self.requests()], ["me", "media", "comments", "comments", "comments"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 5)
        (full,) = self.logged("sweep")
        self.assertEqual([full[key] for key in ("full", "pages", "comments", "new_rows", "new_pending")], [True, 3, 5, 5, 0])
        before = self.seq()
        self.cycle(after=SLOT)
        self.assertEqual([r["kind"] for r in self.requests(since=before)], ["comments"])   # page 1, nothing new
        self.assertEqual(len(self.logged("sweep")), 1)                                      # idle: kv only
        self.assertGreater(self.kv("head_ok_at:" + MEDIA), self.kv("full_sweep_at:" + MEDIA))
        # three new comments: page 1 (2 new), page 2 (1 new), page 3 (none new) and no further
        self.add(*(comment(f"n{n}", "красиво", age=0) for n in range(3)))
        before = self.seq()
        self.cycle(after=SLOT)
        self.assertEqual([r["kind"] for r in self.requests(since=before)], ["comments"] * 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 8)
        head = self.logged("sweep")[-1]
        self.assertEqual((head["full"], head["pages"], head["new_rows"]), (False, 3, 3))
        # the next full sweep comes 30 minutes after the last one
        before = self.seq()
        self.cycle(after=27 * MIN)                                  # 29 minutes after the full sweep
        self.assertEqual([r["kind"] for r in self.requests(since=before)], ["comments"])
        before = self.seq()
        self.cycle(after=MIN)
        self.assertEqual([r["kind"] for r in self.requests(since=before)], ["media"] + ["comments"] * 4)

    def test_a_malformed_comment_is_stored_apart_and_the_rest_of_the_page_is_handled(self):
        self.boot([comment("good", age=50), comment("no_time", age=40, raw={"timestamp": None}),
                   comment("bad_from", age=30, raw={"from": "somebody"}),
                   comment("bad_replies", age=20, raw={"replies": {"data": "x"}}),
                   comment("bad_text", age=10, raw={"text": 5})])
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.status("good"), ("done", ""))
        for one in ("no_time", "bad_from", "bad_replies", "bad_text"):
            self.assertEqual(self.status(one), ("seen", "malformed"), one)
        row = store.comment(self.conn, "no_time")
        self.assertEqual((row["ts"], row["text"]), (row["first_seen_at"], "арена"))
        self.assertEqual([(line["full"], line["malformed"]) for line in self.logged("sweep")], [(True, 4)])
        # still malformed at the next full sweep: still not sent to
        self.assertEqual(self.cycle(after=31 * MIN), 0)
        self.assertEqual(self.delivered()[0], ["good"])
        self.assertEqual(self.status("no_time"), ("seen", "malformed"))

    def test_reel_states(self):
        with self.subTest("gone"):
            self.boot(scenario={"media": [media("555")]})
            self.cycle()
            self.assertEqual((self.kv("reel_state:" + MEDIA), self.kv("reel_fails:" + MEDIA)), ("gone", "1"))
            self.assertEqual([r["kind"] for r in self.requests()], ["me", "media"])
            self.cycle(after=29 * MIN)
            self.assertEqual(len(self.requests("media")), 1)       # skipped until the next full sweep
            self.cycle(after=MIN)
            self.assertEqual(len(self.requests("media")), 2)
        with self.subTest("mismatch"):
            self.boot([comment("c1")], reels=[{"permalink": "https://www.instagram.com/reel/DOther/"}])
            self.assertEqual(self.cycle(), 0)
            self.assertEqual(self.kv("reel_state:" + MEDIA), "mismatch")
            self.assertEqual(self.requests("comments", "dm"), [])
            self.assertIsNone(self.status("c1"))
            self.cycle(after=29 * MIN)
            self.assertEqual(len(self.requests("media")), 1)       # skipped until the next full sweep
        with self.subTest("the permalink of this media"):
            self.boot([comment("c1")], reels=[{"permalink": "https://www.instagram.com/reel/DAbc/"}])
            self.assertEqual(self.cycle(), 1)
        with self.subTest("empty"):
            self.boot([comment("c1")], scenario={"mode": "dev_mode"})
            self.assertEqual(self.cycle(), 0)
            self.assertEqual((self.kv("reel_state:" + MEDIA), self.kv("full_sweep_at:" + MEDIA)), ("empty", None))
            self.fake.control("/mode", {"mode": "ok"})
            self.assertEqual(self.cycle(after=SLOT), 1)            # read again in the next cycle
            self.assertEqual(self.kv("reel_state:" + MEDIA), "ok")
        with self.subTest("capped"), mock.patch.object(engine, "MAX_PAGES", 2):
            self.boot([comment(f"c{n}", age=n) for n in range(3)], scenario={"limits": {"page_size": 1}})
            self.assertEqual(self.cycle(), 0)
            self.assertEqual(self.kv("reel_state:" + MEDIA), "capped")
            self.assertEqual(len(self.requests("comments")), 2)
            self.assertEqual(self.posts(), [])
            self.assertEqual(self.logged("sweep_failed")[-1]["pages"], 2)
            self.cycle(after=29 * MIN)
            self.assertEqual(len(self.requests("comments")), 2)    # not 400 pages in every cycle
            self.cycle(after=MIN)
            self.assertEqual(len(self.requests("comments")), 4)
        with self.subTest("exactly the page limit"), mock.patch.object(engine, "MAX_PAGES", 3):
            self.boot([comment(f"c{n}", age=n) for n in range(3)], scenario={"limits": {"page_size": 1}})
            self.assertEqual(self.cycle(), 1)
            self.assertEqual(self.kv("reel_state:" + MEDIA), "ok")

    def test_an_account_wide_refusal_of_a_read_that_the_probe_does_not_confirm_is_the_reels_failure(self):
        self.boot([comment("c1")])
        self.fault(kind="comments", http=400, code=190, message="Invalid OAuth access token")
        self.assertEqual(self.cycle(), 0)
        self.assertEqual([r["kind"] for r in self.requests()], ["me", "media", "comments", "me"])
        self.assertIsNone(store.get_account_hold(self.conn))
        self.assertEqual(self.kv("reel_fails:" + MEDIA), "1")

    def test_a_stored_comment_is_decided_again_on_full_sweeps_only(self):
        self.boot([comment("c1", "арбуз")])
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.status("c1"), ("seen", ""))
        store.kv_set(self.conn, "fresh_db", None)
        self.restart(reels=[{"also_match": ["арбуз"]}])
        store.arm(self.conn, "unlimited")                          # a changed config disarms
        self.assertEqual(self.cycle(after=SLOT), 1)                # the first collect of a run is a full sweep
        self.assertEqual(self.status("c1"), ("done", ""))
        last = self.logged("sweep")[-1]
        self.assertEqual((last["full"], last["rematched"]), (True, 1))

    def test_a_stored_comment_that_matches_too_late_is_expired(self):
        self.boot([comment(f"c{n}", "арбуз", age=7 * DAY - 9 * MIN + n) for n in range(3)])
        self.cycle()
        self.restart(reels=[{"also_match": ["арбуз"]}])
        with mock.patch.object(store, "expire", return_value=0):   # what collect itself decides
            self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual({tuple(row) for row in self.conn.execute("SELECT status, reason FROM comments")},
                         {("expired", "window")})

    def test_a_stored_comment_gets_the_author_meta_shows_now(self):
        self.boot([comment("c1")], budget=None)
        self.cycle()
        self.conn.execute("UPDATE comments SET status='seen', reason='no_author', user_id=NULL, username=NULL")
        store.arm(self.conn, "unlimited")
        self.assertEqual(self.cycle(after=SLOT), 0)                # a head poll decides nothing again
        self.assertEqual(self.status("c1"), ("seen", "no_author"))
        self.assertEqual(self.cycle(after=30 * MIN), 1)
        self.assertEqual(store.comment(self.conn, "c1")["user_id"], "u_c1")

    def test_a_start_with_another_config_disarms_until_a_human_arms(self):
        """4: a config edit must not mass-send by surprise."""
        self.boot([comment(f"c{n}", "арбуз", age=100 + n) for n in range(3)])
        self.assertEqual(self.events("config_changed"), [           # a database that had not seen any config
            "the config changed: sending is disarmed; read the report, then arm"])
        self.assertEqual(self.kv("config_hash"), config.fingerprint(self.cfg))
        self.cycle()
        self.restart()                                              # the same config again: nothing happens
        self.assertEqual((store.budget(self.conn), self.notifier.events), ("unlimited", []))
        self.restart(reels=[{"also_match": ["арбуз"]}])             # the edit takes effect at the restart
        self.assertEqual((store.budget(self.conn), len(self.events("config_changed"))), (0, 1))
        self.assertEqual(self.kv("config_hash"), config.fingerprint(self.cfg))
        self.assertEqual([self.cycle(after=SLOT) for _ in range(3)], [0, 0, 0])
        self.assertEqual((self.posts(), {self.status(f"c{n}") for n in range(3)}), ([], {("pending", "")}))
        store.arm(self.conn, "unlimited")
        self.assertEqual(self.cycle(after=SLOT), 1)

    def test_the_disarm_the_hash_and_the_event_are_one_transaction(self):
        self.boot(budget=5)
        before = self.kv("config_hash")
        self.cfg = self.make_cfg(max_sends_per_hour=30)
        deaf = Notifier()
        deaf.event = mock.Mock(side_effect=sqlite3.OperationalError("disk I/O error"))
        with self.assertRaises(sqlite3.OperationalError):
            engine.Engine(self.cfg, self.conn, self.client, deaf)
        self.assertEqual((store.budget(self.conn), self.kv("config_hash")), (5, before))
        self.assertFalse(self.conn.in_transaction)
        self.restart()
        self.assertEqual((store.budget(self.conn), len(self.events("config_changed"))), (0, 1))


class DmOutcomes(EngineCase):
    """Every class of answer to POST /messages, end to end: final status, hold, and what happens next."""

    def boot_two(self, fault=None, **boot):
        self.boot([comment("c1", age=200), comment("c2", age=100)], **boot)
        if fault:
            self.fault(kind="dm", target="c1", **fault)

    def test_uncertain_answers_are_final_and_hold_the_sending(self):
        cases = {
            "http_5xx delivered": ({"http": 500, "code": 1, "deliver": True}, "http_5xx", True),
            "http_5xx html": ({"http": 503, "body": "<html>Bad Gateway</html>", "deliver": False}, "http_5xx", False),
            "code 1 delivered": ({"http": 400, "code": 1, "deliver": True}, "code_1", True),
            "code -1": ({"http": 400, "code": -1, "message": "An unknown error occurred", "deliver": False}, "code_-1", False),
            "code 2": ({"http": 400, "code": 2, "deliver": False}, "code_2", False),
            "200 without id": ({"omit_id": True, "deliver": True}, "ok_without_id", True),
            "dropped after delivery": ({"drop": "after"}, "no_response", True),
            "dropped before": ({"drop": "before"}, "no_response", False),
            "not json": ({"http": 400, "body": "oops", "deliver": False}, "unparseable", False),
            "unknown code": ({"http": 400, "code": 77777, "deliver": False}, "unknown_code_77777", False),
        }
        for name, (fault, reason, was_delivered) in cases.items():
            with self.subTest(name):
                self.boot_two(fault)
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.status("c1"), ("uncertain", reason))
                self.assertEqual(self.attempts(), [("c1", "dm", "uncertain", reason)])
                self.assertEqual(self.hold_minutes(), ("uncertain", 15))
                self.assertEqual(self.posts(), [("dm", "c1")])              # no public reply, no next person
                self.assertEqual(len(self.events("uncertain")), 1)
                self.assertEqual([self.cycle(after=7 * MIN) for _ in range(2)], [0, 0])   # 14 min: still held
                self.assertEqual(self.posts(), [("dm", "c1")])
                self.assertEqual(self.cycle(after=MIN), 1)                  # the hold is over: the next person
                self.assertEqual(self.posts(), [("dm", "c1"), ("dm", "c2"), ("public", "c2")])
                self.assertEqual([self.cycle(after=HOUR) for _ in range(2)], [0, 0])
                self.assertEqual(self.status("c1"), ("uncertain", reason))  # never sent again (G4)
                self.assertEqual(self.delivered()[0], ["c1", "c2"] if was_delivered else ["c2"])
                self.assertEqual(len(self.events("uncertain")), 1)

    def test_already_replied_in_all_three_spellings(self):
        for spelling in ("10900", "2534025", "-1"):
            with self.subTest(spelling):
                self.boot([comment("c1", age=200, private_replied=True), comment("c2", age=100)],
                          scenario={"already_replied": {"spelling": spelling}})
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.status("c1"), ("already", "already_replied"))
                self.assertEqual(self.attempts(), [("c1", "dm", "rejected", "already_replied")])
                self.assertIsNone(self.hold_minutes())
                self.assertEqual(self.posts(), [("dm", "c1")])              # sending stops for this cycle
                self.assertEqual(self.cycle(after=SLOT), 1)
                self.assertEqual(self.delivered()[0], ["c2"])

    def test_per_item_rejections_are_final_at_once(self):
        cases = {
            (551, None): "user_unavailable", (100, 2534014): "user_unavailable", (10, 2534022): "outside_window",
            (100, 33): "comment_gone", (100, 2534015): "invalid", (20, None): "invalid", (100, 12345): "invalid",
        }
        for (code, subcode), reason in cases.items():
            with self.subTest(code=code, subcode=subcode):
                self.boot_two({"http": 400, "code": code, "deliver": False, **({"subcode": subcode} if subcode else {})})
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.status("c1"), ("refused", reason))
                self.assertIsNone(self.hold_minutes())
                self.assertIsNone(store.get_account_hold(self.conn))
                self.assertEqual([self.cycle(after=SLOT) for _ in range(3)], [1, 0, 0])
                self.assertEqual(self.posts(), [("dm", "c1"), ("dm", "c2"), ("public", "c2")])

    def test_account_wide_refusals_defer_the_comment_hold_the_sending_and_probe(self):
        cases = {
            "190": ({"http": 400, "code": 190, "subcode": 463}, "auth"),
            "rate code": ({"http": 400, "code": 613}, "rate"),
            "http 429": ({"http": 429, "body": "slow down"}, "rate"),
            "api access blocked": ({"http": 400, "code": 200, "message": "API access blocked."}, "blocked"),
        }
        for name, (fault, reason) in cases.items():
            with self.subTest(name):
                self.boot_two({**fault, "deliver": False})
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.status("c1"), ("pending", ""))
                self.assertEqual(store.comment(self.conn, "c1")["deferrals"], 1)
                self.assertEqual(self.attempts(), [("c1", "dm", "global", reason)])
                self.assertEqual(self.hold_minutes(), ("send_blocked", 30))
                # the probe follows the refusal at once; the account answers, so there is no account hold
                self.assertEqual([r["kind"] for r in self.requests()][-2:], ["dm", "me"])
                self.assertIsNone(store.get_account_hold(self.conn))
                before = self.seq()
                self.assertEqual(self.cycle(after=29 * MIN), 0)
                self.assertEqual([r["kind"] for r in self.requests(since=before)], ["comments"])   # reads go on
                self.assertEqual(self.cycle(after=MIN), 1)
                self.assertEqual(self.posts()[1:], [("dm", "c2"), ("public", "c2")])   # head of line: another comment

    def test_a_failed_probe_after_the_refusal_sets_the_account_hold_too(self):
        self.boot_two({"http": 400, "code": 200, "message": "API access blocked.", "deliver": False})
        self.fault(kind="me", nth=2, http=400, code=200, message="API access blocked.")
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.hold_minutes(), ("send_blocked", 30))
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "blocked")
        before = self.seq()
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual(self.requests(since=before), [])           # the account hold blocks the reads as well

    def test_a_probe_without_a_clear_answer_after_the_refusal_holds_the_account_for_the_refusals_reason(self):
        self.boot_two({"http": 400, "code": 190, "deliver": False})
        self.fault(kind="me", nth=2, http=502, body="bad gateway")
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "auth")

    def test_spam_refusal_holds_for_a_day_and_disarms(self):
        self.boot_two({"http": 400, "code": 368, "deliver": False}, budget=5)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.status("c1"), ("pending", ""))
        self.assertEqual(self.hold_minutes(), ("spam", 24 * 60))
        self.assertEqual(store.budget(self.conn), 0)
        self.assertEqual([r["kind"] for r in self.requests()][-1], "dm")   # no probe: the account is fine, we are not
        self.assertEqual(self.cycle(after=25 * HOUR), 0)            # the hold is over, the budget is still 0
        self.assertEqual(self.posts(), [("dm", "c1")])

    def test_messaging_blocked_while_reads_work(self):
        self.boot_two(scenario={"mode": "messaging_blocked"})
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.attempts(), [("c1", "dm", "global", "blocked")])
        self.assertIsNone(store.get_account_hold(self.conn))        # GET /me answered
        self.assertEqual(self.hold_minutes(), ("send_blocked", 30))
        self.assertEqual([self.cycle(after=5 * MIN) for _ in range(5)], [0] * 5)
        self.assertEqual(self.posts(), [("dm", "c1")])              # no POST while the hold lasts (I7)
        self.assertEqual(self.cycle(after=5 * MIN), 0)              # one trial, with the other comment
        self.assertEqual(self.posts(), [("dm", "c1"), ("dm", "c2")])
        self.assertEqual(self.hold_minutes(), ("send_blocked", 60))
        self.fake.control("/mode", {"mode": "ok"})
        self.assertEqual(self.cycle(after=HOUR), 1)
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(sorted(self.delivered()[0]), ["c1", "c2"])

    def test_two_account_wide_refusals_do_not_write_off_a_comment(self):
        self.boot([comment("bad", age=300), comment("good", age=200), comment("next", age=100)])
        self.fault(kind="dm", target="bad", http=400, code=190, deliver=False)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.cycle(after=30 * MIN), 1)             # deferred: the trial goes to another comment
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual([r["target"] for r in self.requests("dm")], ["bad", "good", "next", "bad"])
        self.assertEqual(self.status("bad"), ("pending", ""))
        self.fake.control("/faults", {"clear": True})
        self.assertEqual([self.cycle(after=HOUR) for _ in range(2)], [1, 0])
        self.assertEqual(len(self.requests("dm")), 5)

    def test_a_refused_connection_costs_nothing_and_is_tried_again(self):
        self.boot_two()
        self.fault(kind="comments", nth=1, refuse_real_sec=0.4)     # the listener closes before the POST
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.attempts(), [("c1", "dm", "not_sent", "connect")])
        self.assertEqual(self.status("c1"), ("pending", ""))
        self.assertEqual(store.comment(self.conn, "c1")["deferrals"], 0)
        self.assertIsNone(self.hold_minutes())
        self.assertEqual(self.posts(), [])                          # Meta never saw it
        time.sleep(0.6)
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])

    def test_five_dms_in_a_row_without_ok_start_the_streak_hold(self):
        self.boot([comment(f"c{n}", age=100 - n) for n in range(6)])
        self.fault(kind="dm", http=400, code=551, deliver=False)
        self.assertEqual([self.cycle(after=SLOT) for _ in range(4)], [0] * 4)
        self.assertIsNone(self.hold_minutes())
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual(self.hold_minutes(), ("streak", 60))
        self.assertEqual(self.cycle(after=59 * MIN), 0)
        self.assertEqual(len(self.requests("dm")), 5)


class CrashAndStop(EngineCase):
    def killed_after(self, method):
        """The process dies when `method` of the client has got its answer, before anything is written."""
        real = getattr(self.client, method)

        def call(*args):
            real(*args)
            raise Kill()
        return mock.patch.object(self.client, method, call)

    def test_kill_between_claim_and_record_is_uncertain_and_never_sent_again(self):
        self.boot([comment("c1", age=200), comment("c2", age=100)], budget=5)
        with self.killed_after("send_dm"), self.assertRaises(Kill):
            self.cycle()
        self.assertEqual(self.status("c1"), ("sending", ""))
        self.assertEqual(self.attempts(), [("c1", "dm", None, None)])
        self.restart()
        self.assertEqual(self.cycle(after=1), 0)
        self.assertEqual(self.status("c1"), ("uncertain", "interrupted"))
        self.assertEqual(self.attempts(), [("c1", "dm", "uncertain", "interrupted")])
        self.assertEqual(self.hold_minutes(), ("uncertain", 15))    # a crash loop must not spend a person per start
        self.assertEqual(store.budget(self.conn), 4)                # never refunded
        self.assertEqual(self.events("uncertain"), [self.events("uncertain")[0]])
        self.assertEqual(self.cycle(after=15 * MIN), 1)
        self.assertEqual([self.cycle(after=HOUR) for _ in range(2)], [0, 0])
        self.assertEqual([r["target"] for r in self.requests("dm")], ["c1", "c2"])
        self.assertEqual(self.delivered()[0], ["c1", "c2"])         # c1 did get its DM, once

    def test_kill_during_the_public_reply_never_posts_it_twice(self):
        self.boot([comment("c1")])
        with self.killed_after("public_reply"), self.assertRaises(Kill):
            self.cycle()
        self.assertEqual(self.status("c1"), ("sent", ""))
        self.restart()
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual(self.status("c1"), ("done", "public_uncertain"))
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])

    def test_a_public_reply_that_is_owed_is_posted_by_the_next_run(self):
        self.boot([comment("c1")])
        with mock.patch.object(clock, "sleep", side_effect=Kill), self.assertRaises(Kill):
            self.cycle()                                            # killed in the pause before the public reply
        self.assertEqual(self.status("c1"), ("sent", ""))
        self.assertEqual(self.posts(), [("dm", "c1")])
        self.restart()
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual(self.status("c1"), ("done", ""))
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])

    def test_owed_public_replies_wait_behind_a_closed_gate_without_a_pause_for_each(self):
        self.boot([comment("c1", age=200), comment("c2", age=100)], budget=1)
        with mock.patch.object(clock, "sleep", side_effect=Kill), self.assertRaises(Kill):
            self.cycle()                                            # killed in the pause before the public reply
        self.conn.execute("UPDATE comments SET status='sent' WHERE comment_id='c2'")   # a second one that is owed
        self.restart()
        store.set_send_hold(self.conn, "uncertain", 15 * MIN)
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual(len(self.time.slept), 1)                   # the gate is closed: the second is not tried
        self.assertEqual(self.posts(), [("dm", "c1")])
        self.assertEqual(self.cycle(after=15 * MIN), 0)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1"), ("public", "c2")])
        self.assertEqual((self.status("c1"), self.status("c2")), (("done", ""), ("done", "")))
        self.assertEqual(len(self.time.slept), 3)                   # 2-4 s before each of them

    def test_the_queued_alerts_go_out_even_when_the_problems_cannot_be_worked_out(self):
        self.boot()
        self.report.problems = mock.Mock(side_effect=RuntimeError("boom"))
        self.cycle()
        self.assertEqual((self.notifier.synced, self.notifier.flushed), ([], 1))
        self.assertIn("RuntimeError: boom", self.kv("last_cycle_error"))

    def test_stop_flag_ends_the_cycle_after_the_call_in_flight_and_its_write(self):
        self.boot([comment("c1", age=200), comment("c2", age=100)])
        real = self.client.send_dm

        def send_dm(*args):   # SIGTERM arrives while the POST is in flight
            self.stop = True
            return real(*args)
        with mock.patch.object(self.client, "send_dm", send_dm):
            self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.status("c1"), ("sent", ""))           # the answer was written
        self.assertEqual(self.attempts(), [("c1", "dm", "ok", "")])
        self.assertEqual(self.posts(), [("dm", "c1")])              # nothing was started after the flag
        self.assertEqual((self.notifier.synced, self.notifier.flushed), ([], 0))
        self.engine.run()                                           # the loop ends at once
        self.assertEqual(self.posts(), [("dm", "c1")])
        self.stop = False
        self.restart()
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1"), ("dm", "c2"), ("public", "c2")])

    def test_run_cycles_until_the_flag_and_sleeps_until_the_next_slot_or_poll(self):
        self.boot([comment("c1", age=200), comment("c2", age=100)])
        pauses = []

        def sleep(seconds, should_stop=None, ping=None):
            self.time.advance(seconds)
            if seconds > 4:   # not the pause before a public reply: the sleep between two cycles
                pauses.append(seconds)
                self.assertIsNotNone(should_stop)
                self.stop = len(pauses) == 2
        with mock.patch.object(clock, "sleep", sleep):
            self.engine.run()
        self.assertEqual(len(self.events("started")), 1)
        self.assertEqual(self.delivered()[0], ["c1", "c2"])
        # c2 waits for its slot: a short sleep; then the queue is empty: a normal poll
        self.assertTrue(SLOT - 10 < pauses[0] < SLOT, pauses)
        self.assertEqual(pauses[1], self.cfg.poll_interval_sec)

    def test_an_unexpected_exception_is_recorded_announced_and_the_next_cycle_runs(self):
        self.boot([comment("c1")])
        with mock.patch.object(store, "expire", side_effect=RuntimeError("boom")):
            self.assertEqual(self.cycle(), 0)
        self.assertIn("RuntimeError: boom", self.kv("last_cycle_error"))
        self.assertIsNone(self.kv("last_cycle_ok_at"))
        self.assertEqual((len(self.notifier.synced), self.notifier.flushed), (1, 1))   # problems are told all the same
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertIsNone(self.kv("last_cycle_error"))
        self.assertIsNotNone(self.kv("last_cycle_ok_at"))

    def test_an_exception_in_one_reel_does_not_stop_the_other(self):
        reels = [{"name": "arena", "media_id": MEDIA}, {"name": "second", "media_id": MEDIA_2}]
        self.boot(reels=reels, scenario={"media": [media(comments=[comment("a1")]),
                                                   media(MEDIA_2, [comment("b1")], "DSecond")]})
        real = self.client.comments_page

        def comments_page(media_id, next_url=None):
            if media_id == MEDIA:
                raise RuntimeError("boom")
            return real(media_id, next_url)
        with mock.patch.object(self.client, "comments_page", comments_page):
            self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.delivered()[0], ["b1"])
        self.assertEqual(self.kv("reel_fails:" + MEDIA), "1")
        self.assertIsNone(self.kv("last_cycle_error"))


class PublicReply(EngineCase):
    def test_failure_modes(self):
        cases = {
            "comment gone": ({"http": 400, "code": 100, "subcode": 33, "deliver": False}, "rejected", "public_failed", None),
            "answer lost": ({"drop": "after"}, "uncertain", "public_uncertain", None),
            "http 500": ({"http": 500, "deliver": True}, "uncertain", "public_uncertain", None),
            "spam": ({"http": 400, "code": 20, "deliver": False}, "global", "public_failed", ("spam", 24 * 60)),
            "blocked": ({"http": 400, "code": 10, "deliver": False}, "global", "public_failed", ("send_blocked", 30)),
        }
        for name, (fault, outcome, reason, hold) in cases.items():
            with self.subTest(name):
                self.boot([comment("c1")], budget=5)
                self.fault(kind="public", **fault)
                self.assertEqual(self.cycle(), 1)
                self.assertEqual(self.status("c1"), ("done", reason))
                self.assertEqual(self.attempts("kind, outcome")[1], ("public", outcome))
                self.assertEqual(self.hold_minutes(), hold)
                self.assertEqual(store.budget(self.conn), 0 if name == "spam" else 4)
                self.assertEqual([self.cycle(after=25 * HOUR) for _ in range(2)], [0, 0])
                self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])   # never a second public POST

    def test_public_reply_off_in_the_config(self):
        self.boot([comment("c1")], public_reply=False)
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.status("c1"), ("done", "public_skipped"))
        self.assertEqual(self.posts(), [("dm", "c1")])


class AccountHold(EngineCase):
    def me_calls(self):
        return len(self.requests("me"))

    def test_api_blocked_stops_everything_then_probes_on_schedule_and_resumes_alone(self):
        self.boot([comment("c1")], scenario={"mode": "blocked"})
        self.assertEqual(self.cycle(), 0)
        hold = store.get_account_hold(self.conn)
        self.assertEqual((hold["reason"], hold["n"]), ("blocked", 0))
        self.assertEqual([r["kind"] for r in self.requests()], ["me"])
        calls = 1
        for wait in (5, 10, 20, 30, 30):                            # minutes between probes
            self.assertEqual(self.cycle(after=(wait - 1) * MIN), 0)
            self.assertEqual(self.me_calls(), calls, f"a probe before {wait} min")
            self.assertEqual(self.cycle(after=MIN), 0)
            calls += 1
            self.assertEqual(self.me_calls(), calls, f"no probe after {wait} min")
        self.assertEqual(len(self.requests()), calls)               # nothing but probes
        self.assertEqual(store.get_account_hold(self.conn)["since"], hold["since"])
        self.fake.control("/mode", {"mode": "ok"})
        self.assertEqual(self.cycle(after=30 * MIN), 0)             # the probe lifts the hold
        self.assertIsNone(store.get_account_hold(self.conn))
        self.assertEqual(self.cycle(after=SLOT), 1)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])

    def test_a_block_that_starts_later_is_found_by_the_read_and_confirmed_by_the_probe(self):
        self.boot([comment("c1")], budget=None)
        self.cycle()
        store.arm(self.conn, "unlimited")
        for mode, reason, first_probe in (("blocked", "blocked", 5), ("token_dead", "auth", 5), ("throttled", "rate", 15)):
            with self.subTest(mode):
                self.fake.control("/mode", {"mode": mode})
                before = self.seq()
                self.assertEqual(self.cycle(after=SLOT), 0)
                read, probe = [r["kind"] for r in self.requests(since=before)]   # one refused read, then the probe
                self.assertIn(read, ("media", "comments"))
                self.assertEqual(probe, "me")
                self.assertEqual(store.get_account_hold(self.conn)["reason"], reason)
                self.assertEqual(self.cycle(after=(first_probe - 1) * MIN), 0)
                self.assertEqual(len(self.requests(since=before)), 2)
                self.assertEqual(self.cycle(after=MIN), 0)
                self.assertEqual([r["kind"] for r in self.requests(since=before)][2:], ["me"])
                self.fake.control("/mode", {"mode": "ok"})
                self.assertEqual(self.cycle(after=HOUR), 0)
                self.assertIsNone(store.get_account_hold(self.conn))
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.kv("reel_fails:" + MEDIA), "0")       # the failures were the account's, not the reel's

    def test_a_refused_read_ends_collect_when_the_probe_fails_whatever_the_probe_says(self):
        reels = [{"name": "arena", "media_id": MEDIA}, {"name": "second", "media_id": MEDIA_2}]
        self.boot(reels=reels, budget=None, scenario={"media": [media(), media(MEDIA_2, shortcode="DSecond")]})
        self.cycle()
        self.fault(kind="comments", target=MEDIA, http=400, code=4, message="Application request limit reached")
        self.fault(kind="me", http=502, body="bad gateway")
        before = self.seq()
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual([(r["kind"], r["target"]) for r in self.requests(since=before)],
                         [("comments", MEDIA), ("me", None)])       # the second reel was not asked for
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "rate")
        self.assertEqual(self.kv("reel_fails:" + MEDIA), "0")       # the account's failure, not the reel's
        (failed,) = self.logged("sweep_failed")                     # told in the log, with Meta's answer
        self.assertIn("Application request limit reached", failed["note"])

    def test_expire_and_the_announcement_run_during_an_account_hold(self):
        self.boot([comment("c1", age=7 * DAY - HOUR)], budget=None)
        self.cycle()
        self.assertEqual(self.status("c1"), ("pending", ""))
        self.fake.control("/mode", {"mode": "blocked"})
        self.cycle(after=SLOT)
        self.assertIsNotNone(store.get_account_hold(self.conn))
        told, flushed = len(self.notifier.synced), self.notifier.flushed
        self.assertEqual(self.cycle(after=HOUR), 0)
        self.assertEqual(self.status("c1"), ("expired", "window"))
        self.assertEqual((len(self.notifier.synced), self.notifier.flushed), (told + 1, flushed + 1))
        self.assertEqual(self.notifier.synced[-1], [("stub", "a problem")])
        (lost,) = self.events("expired")
        self.assertTrue(lost.startswith("1 people lost"), lost)
        self.cycle(after=SLOT)
        self.assertEqual(len(self.events("expired")), 1)            # told once

    def test_three_probes_without_an_answer_make_the_account_unreachable(self):
        self.boot([comment("c1")], budget=None)
        self.cycle()
        self.fault(kind="me", http=502, body="bad gateway")
        self.assertEqual(self.cycle(after=HOUR), 0)                 # the hourly GET /me fails
        self.assertEqual((self.kv("probe_fails"), store.get_account_hold(self.conn)), ("1", None))
        self.assertEqual(self.cycle(after=SLOT), 0)
        self.assertEqual((self.kv("probe_fails"), store.get_account_hold(self.conn)), ("2", None))
        self.assertEqual(self.cycle(after=SLOT), 0)
        hold = store.get_account_hold(self.conn)
        self.assertEqual((hold["reason"], hold["detail"]), ("blocked", "unreachable"))
        self.fake.control("/faults", {"clear": True})
        self.assertEqual(self.cycle(after=5 * MIN), 0)
        self.assertIsNone(store.get_account_hold(self.conn))
        self.assertIsNone(self.kv("probe_fails"))

    def test_another_account_behind_the_token_blocks_everything(self):
        self.boot([comment("c1")], budget=None)
        self.cycle()
        self.fault(kind="me", body={"user_id": "555", "id": "556", "username": "somebody_else"})
        self.assertEqual(self.cycle(after=HOUR), 0)                 # the hourly GET /me
        store.arm(self.conn, "unlimited")
        hold = store.get_account_hold(self.conn)
        self.assertEqual((hold["reason"], hold["detail"]), ("blocked", "identity_changed"))
        self.assertEqual(self.kv("me_user_id"), ME["user_id"])
        self.assertEqual(self.cycle(after=5 * MIN), 0)              # the probe sees the same stranger: no lift
        self.assertIsNotNone(store.get_account_hold(self.conn))
        self.assertEqual(self.posts(), [])


class Token(EngineCase):
    def test_env_file_format(self):
        path = os.path.join(tempfile.mkdtemp(), ".env")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        self.assertEqual(engine.read_env(path), {})                 # no file
        with open(path, "w", encoding="utf-8", newline="") as file:
            file.write("# comment\r\nIG_ACCESS_TOKEN = abc=def \r\nexport TG_BOT_TOKEN=\"1:x y\"\n"
                       "  TG_CHAT_ID='-100'\nEMPTY=\nexported=1\nnot a line\n#OFF=1\n")
        self.assertEqual(engine.read_env(path), {"IG_ACCESS_TOKEN": "abc=def", "TG_BOT_TOKEN": "1:x y",
                                                 "TG_CHAT_ID": "-100", "exported": "1"})

    def test_no_token_anywhere_means_no_call_at_all(self):
        self.boot([comment("c1")], env_token=None)
        self.assertEqual([self.cycle(after=10 * MIN) for _ in range(3)], [0, 0, 0])
        self.assertEqual(self.requests(), [])
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "no_token")
        self.assertEqual(self.notifier.flushed, 3)
        self.write_env(TOKEN)                                       # adopted, probed and used in the same cycle
        self.assertEqual(self.cycle(after=1), 1)
        self.assertEqual([r["kind"] for r in self.requests()], ["me", "media", "comments", "dm", "public"])
        self.assertIsNone(store.get_account_hold(self.conn))

    def test_a_new_env_token_wins_once_and_is_probed_at_once(self):
        self.boot([comment("c1")], budget=None)
        self.cycle()
        self.assertEqual((self.kv("token"), self.kv("token_env_seen")), (TOKEN, TOKEN))
        self.fake.control("/token", {"value": "IGAAsecond"})        # the owner made a new token: the old one is dead
        self.cycle(after=SLOT)
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "auth")
        self.write_env("IGAAsecond")
        store.kv_set_many(self.conn, {"token_refreshed_at": clock.iso(clock.now() - timedelta(days=30)),
                                      "token_expires_at": clock.iso(clock.now() + timedelta(days=30)),
                                      "token_refresh_fails": 2})
        before = self.seq()
        self.cycle(after=SLOT)
        self.assertEqual((self.kv("token"), self.kv("token_env_seen")), ("IGAAsecond", "IGAAsecond"))
        self.assertLess(clock.now() - clock.parse(self.kv("token_refreshed_at")), timedelta(minutes=1))
        self.assertEqual((self.kv("token_expires_at"), self.kv("token_refresh_fails")), (None, None))
        self.assertEqual([(r["kind"], r["token_ok"]) for r in self.requests(since=before)],
                         [("me", True), ("comments", True)])        # probed first, then the cycle goes on
        self.assertIsNone(store.get_account_hold(self.conn))
        self.cycle(after=SLOT)
        self.assertEqual(self.kv("token"), "IGAAsecond")

    def test_refresh_policy(self):
        self.boot(scenario={"token": {"value": TOKEN, "age_sec": 30 * DAY, "rotate": True}})
        self.cycle()
        self.cycle(after=23 * HOUR)
        self.assertEqual(self.requests("refresh"), [])              # not before the token is 24 h old here
        self.cycle(after=HOUR)
        self.assertEqual(len(self.requests("refresh")), 1)
        self.assertEqual((self.kv("token"), self.kv("token_env_seen")), (TOKEN + ".r1", TOKEN))
        left = clock.parse(self.kv("token_expires_at")) - clock.now()
        self.assertEqual(round(left / timedelta(days=1)), 60)
        before = self.seq()
        self.cycle(after=SLOT)                                      # .env still has the old value: it does not win again
        self.assertEqual(self.kv("token"), TOKEN + ".r1")
        self.assertTrue(all(r["token_ok"] for r in self.requests(since=before)))
        self.assertIsNone(store.get_account_hold(self.conn))
        for _ in range(6):
            self.cycle(after=DAY)
        self.assertEqual(len(self.requests("refresh")), 1)          # expiry known: not before 7 days
        self.cycle(after=DAY + HOUR)
        self.assertEqual(len(self.requests("refresh")), 2)
        self.assertEqual(self.kv("token"), TOKEN + ".r2")

    def test_a_failed_refresh_is_tried_once_per_six_hours_and_sets_no_hold(self):
        self.boot([comment("c1")], scenario={"token": {"value": TOKEN, "age_sec": 30 * DAY}})
        self.cycle()
        self.fault(kind="refresh", http=400, code=190, deliver=False, id="refresh-down")
        self.cycle(after=24 * HOUR)
        self.assertEqual((len(self.requests("refresh")), self.kv("token_refresh_fails")), (1, "1"))
        self.assertIsNotNone(self.kv("token_refresh_failed_at"))
        self.assertIsNone(store.get_account_hold(self.conn))
        self.assertIsNone(self.hold_minutes())
        self.cycle(after=5 * HOUR)
        self.assertEqual(len(self.requests("refresh")), 1)
        self.cycle(after=HOUR)
        self.assertEqual((len(self.requests("refresh")), self.kv("token_refresh_fails")), (2, "2"))
        self.fake.control("/faults", {"clear": True})
        self.cycle(after=6 * HOUR)
        self.assertEqual(len(self.requests("refresh")), 3)
        self.assertEqual((self.kv("token_refresh_fails"), self.kv("token_refresh_failed_at")), (None, None))
        self.assertEqual(self.kv("token"), TOKEN)

    def test_a_token_of_unknown_age_starts_its_age_now(self):
        self.boot(budget=None)
        self.cycle()
        store.kv_set(self.conn, "token_refreshed_at", None)
        self.cycle(after=SLOT)
        self.assertIsNotNone(self.kv("token_refreshed_at"))
        self.assertEqual(self.requests("refresh"), [])


class Housekeeping(EngineCase):
    def test_clock_guard(self):
        self.boot([comment("c1", age=7 * DAY - HOUR)], budget=None)
        self.cycle()
        self.assertIsNone(self.kv("clock_problem"))
        with self.subTest("the wall clock jumps ahead of monotonic time"):
            self.time.wall += 2 * HOUR
            self.cycle()
            self.assertIn("jumped", self.kv("clock_problem"))
            self.assertEqual(self.status("c1"), ("pending", ""))    # not expired on the word of a broken clock
            self.cycle(after=SLOT)
            self.assertIsNone(self.kv("clock_problem"))
            self.assertEqual(self.status("c1"), ("expired", "window"))
        with self.subTest("ten minutes of drift are tolerated"):
            self.time.wall += 9 * MIN
            self.cycle()
            self.assertIsNone(self.kv("clock_problem"))
        with self.subTest("the clock goes back, also across a restart"):
            self.add(comment("c2"))
            store.arm(self.conn, "unlimited")
            self.time.wall -= 6 * MIN
            self.restart()
            self.assertEqual(self.cycle(), 0)
            self.assertIn("went back", self.kv("clock_problem"))
            self.assertEqual(self.status("c2"), ("pending", ""))
            self.assertEqual(self.posts(), [])
            self.assertEqual(self.cycle(after=SLOT), 1)
            self.assertIsNone(self.kv("clock_problem"))

    def test_metric_once_an_hour(self):
        self.boot()
        self.cycle()
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT followers, media_count FROM metrics")],
                         [(ME["followers_count"], ME["media_count"])])
        self.assertEqual(len(self.requests("me")), 1)               # the answer of the start-up probe was used
        for _ in range(3):
            self.cycle(after=19 * MIN)
        self.assertEqual(len(self.requests("me")), 1)
        self.fake.account["followers_count"] = 1300
        self.cycle(after=4 * MIN)
        self.assertEqual(len(self.requests("me")), 2)
        self.assertEqual([row[0] for row in self.conn.execute("SELECT followers FROM metrics ORDER BY ts")], [1234, 1300])

    def test_digest_once_a_day(self):
        self.boot(alerts={"repeat_min": 120, "digest_hour_utc": 13})
        self.cycle()                                                # 12:00 UTC: not yet
        self.cycle(after=59 * MIN)
        self.assertEqual(self.events("digest"), [])
        self.cycle(after=2 * MIN)                                   # 13:01
        (digest,) = self.events("digest")
        self.assertTrue(digest.startswith("report of 2026-10-05T13:0"), digest)
        self.cycle(after=5 * HOUR)
        self.assertEqual(len(self.events("digest")), 1)             # once a day
        self.cycle(after=19 * HOUR)                                 # 13:01 the next day
        self.assertEqual(len(self.events("digest")), 2)

    def test_fresh_database_is_told_and_cleared_by_the_first_ok_full_sweep(self):
        self.boot([comment("c1")], budget=None)
        self.fault(kind="media", nth=1, http=500)
        self.cycle()
        self.assertEqual(len(self.events("fresh_db")), 1)
        self.assertEqual(self.kv("fresh_db"), "1")                  # the sweep failed: still marked
        self.cycle(after=SLOT)
        self.assertIsNone(self.kv("fresh_db"))
        self.assertEqual(len(self.events("fresh_db")), 1)
        self.restart()
        self.cycle(after=SLOT)
        self.assertEqual(self.notifier.events, [])                  # the next start has nothing to tell

    def test_problems_are_evaluated_and_flushed_in_every_cycle(self):
        self.boot()
        seen = []
        self.report.problems = lambda conn, cfg, now: seen.append((conn, cfg, now)) or [("k", "t")]
        for _ in range(3):
            self.cycle(after=SLOT)
        self.assertEqual(self.notifier.synced, [[("k", "t")]] * 3)
        self.assertEqual(self.notifier.flushed, 3)
        self.assertTrue(all(conn is self.conn and cfg is self.cfg for conn, cfg, _ in seen))
        self.assertLess(abs((seen[-1][2] - clock.now()).total_seconds()), 5)


@unittest.skipUnless(os.path.exists(os.path.join(CODE, "report.py")) and os.path.exists(os.path.join(CODE, "notify.py")),
                     "report.py / notify.py are not written yet")
class WithTheRealReportAndNotifier(EngineCase):
    def test_a_blocked_account_reaches_telegram(self):
        import notify
        sys.modules.pop("report", None)   # the real module instead of the stand-in
        self.boot([comment("c1")], scenario={"mode": "blocked", "telegram": {"token": "123:abc", "chat_id": 42}})
        self.engine.notifier = notify.Notifier(self.conn, lambda: ("123:abc", "42"), self.fake.api_base)
        self.cycle()
        text = "\n".join(message["text"] for message in self.fake.control("/truth")["telegram"])
        self.assertIn("hold:account", text)
        self.assertIn("fresh_db", text)
        self.assertNotIn(TOKEN, text)
        self.fake.control("/mode", {"mode": "ok"})
        self.cycle(after=5 * MIN)
        self.assertEqual(self.cycle(after=SLOT), 1)
        text = "\n".join(message["text"] for message in self.fake.control("/truth")["telegram"])
        self.assertIn("cleared: hold:account", text)


if __name__ == "__main__":
    unittest.main()
