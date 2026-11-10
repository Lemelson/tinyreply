
# Attack round 1, area "ChatbotX scenario catalogue" (research/lessons/chatbotx.md, CX-01 .. CX-85, the error
# mapper table of section 2 and the "misses with reasons" taxonomy of section 3).
#
# Everything runs through the REAL engine, the REAL report and the REAL notifier against the in-process fake
# Meta + Telegram (sim/fake_meta.py) over loopback HTTP. The bot is judged by the fake's ground truth, by the
# database and by what reached the owner's Telegram chat. Time is in the test's hands: the bot and the fake
# read the same patched wall clock, and clock.sleep() moves that clock instead of sleeping.
#
#     Findings*   the two tests that FAILED on the code of that day (CX-70): confirmed, fixed, and moved to
#                 tests/test_regress_r1.py (classes ChatBotX*, on the harness of this module)
#     Held*       scenarios of the catalogue that no existing test covered end to end and that the product
#                 withstands (they pass): robustness evidence, not findings
#
# The decision for EVERY scenario (not applicable / covered by which existing test / tested here) is the table
# CATALOGUE at the end of this file; test_the_catalogue_is_decided_completely keeps it complete.
# 71 scenarios: 6 not applicable, 41 covered before, 24 newly tested and held (CX-70 since the fixes of round 1).
#
# Run from autoreply/:  python3.12 -B -m unittest tests.test_lessons_chatbotx
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import urlencode

import bot
import clock
import config
import engine
import meta
import notify
import report
import store
from sim.fake_meta import FakeMeta

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LESSONS = os.path.join(os.path.dirname(CODE), "research", "lessons", "chatbotx.md")
MIN, HOUR, DAY = 60, 3600, 86400
START = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
SLOT = 60   # seconds between two DMs at 60 per hour
ME = {"user_id": "17841400000000001", "id": "9990000000000001", "username": "your_account",
      "followers_count": 1234, "media_count": 7}
OWNER = {"id": ME["id"], "username": ME["username"]}
MEDIA, MEDIA_2 = "17900000000000101", "18000000000000002"
SHORTCODES = {MEDIA: "DAbc", MEDIA_2: "DSecond"}
TOKEN = "IGAAtoken1"
TG_TOKEN, TG_CHAT = "123:abc", "42"
COMMENT_FIELDS = "id,text,timestamp,from,hidden,replies{id,from}"
with open(os.path.join(CODE, "config.example.json"), encoding="utf-8") as _file:
    EXAMPLE = json.load(_file)


def comment(comment_id, text="арена", user=None, age=60, **more):
    """A scenario comment; by default a fresh request by a person of its own."""
    user = user or "u_" + comment_id
    return {"id": comment_id, "text": text, "age_sec": age, "from": {"id": user, "username": "name_" + user},
            "should_match": True, **more}


def error_body(code, subcode=None, message="Injected", kind="OAuthException", subcode_key="error_subcode"):
    error = {"message": message, "type": kind, "code": code, "fbtrace_id": "Axyz"}
    if subcode is not None:
        error[subcode_key] = subcode
    return {"error": error}


class _Time:
    """Wall time = START + real time since the test began + what the test added; monotonic time likewise."""

    def __init__(self):
        self._time, self._monotonic = time.time, time.monotonic
        self._began, self.wall, self.mono, self.slept = time.time(), 0.0, 0.0, []

    def time(self):
        return START + (self._time() - self._began) + self.wall

    def monotonic(self):
        return self._monotonic() + self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds, should_stop=None, ping=None):
        self.slept.append(seconds)
        if not (should_stop and should_stop()):
            self.advance(seconds)


class _Killed(BaseException):
    """kill -9: nothing after this point runs."""


class World(unittest.TestCase):
    """One bot (engine + report + notifier, wired as bot.py wires them) against one fake Meta + Telegram."""

    def setUp(self):
        self.time = _Time()
        self.log = io.StringIO()   # the bot's stderr: one line per event (SPEC 3)
        for patch in (mock.patch("time.time", self.time.time), mock.patch.object(clock, "monotonic", self.time.monotonic),
                      mock.patch.object(clock, "sleep", self.time.sleep), mock.patch.object(clock, "SCALE", 1.0),
                      mock.patch.object(sys, "stderr", self.log)):
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
        shutil.rmtree(self.dir, True)
        self.assertEqual(strict, [])

    # ---- building a bot

    def boot(self, comments=(), budget="unlimited", scenario=None, more_media=None, env_token=TOKEN, token=TOKEN,
             telegram=True, **cfg_changes):
        """A fake Meta + Telegram with reel MEDIA and these comments (`more_media`: {media_id: comments} for
        further reels, each configured like the first), an empty state directory with `.env` and config.json,
        and a bot that was just started on it. It is 12:00 UTC."""
        self.retire()
        self.time.wall = self.time.mono = 0.0
        self.dir = tempfile.mkdtemp(prefix="cx-")
        listed = {MEDIA: list(comments), **(more_media or {})}
        self.fake = FakeMeta({
            "account": dict(ME), "token": token, "telegram": {"token": TG_TOKEN, "chat_id": int(TG_CHAT)},
            "media": [{"id": media_id, "permalink": f"https://www.instagram.com/reel/{SHORTCODES[media_id]}/",
                       "shortcode": SHORTCODES[media_id], "comments": rows} for media_id, rows in listed.items()],
            **(scenario or {})}).start()
        self.write_env(env_token, telegram)
        self.reels = cfg_changes.pop("reels", None) or [
            {"name": f"reel{n}" if n else "arena", "media_id": media_id} for n, media_id in enumerate(listed)]
        self.cfg_changes = cfg_changes
        self.lock = bot.take_lock(self.dir)   # as `bot.py run` holds it: the report sees a running service
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        store.init(self.conn)
        self.start()
        if budget is not None:
            store.arm(self.conn, budget)   # after the start: one with a config the database has not seen disarms
        return self.engine

    def start(self, reels=None, **cfg_changes):
        """A new process run on the same state directory: the config is read again, nothing in memory survives."""
        self.reels = self.reels if reels is None else reels
        self.cfg_changes = {**self.cfg_changes, **cfg_changes}
        # the daily digest (a whole report) is kept out of the way: it is due at 23:00
        raw = {**EXAMPLE, "sending": True, "alerts": {"repeat_min": 120, "digest_hour_utc": 23}, **self.cfg_changes,
               "reels": [{**EXAMPLE["reels"][0], **reel} for reel in self.reels]}
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(raw, file)
        self.cfg = config.load(path)
        self.stop_at = None   # wall time at which the stop flag goes up (serve)
        self.notifier = notify.Notifier(self.conn, bot.telegram(self.dir), self.fake.api_base,
                                        repeat_min=self.cfg.alerts.repeat_min)
        self.client = meta.Client(self.fake.api_base, self.cfg.api_version,
                                  lambda: store.kv_get(self.conn, "token") or "")
        self.engine = engine.Engine(self.cfg, self.conn, self.client, self.notifier,
                                    lambda: self.stop_at is not None and self.time.wall >= self.stop_at)
        return self.engine

    def write_env(self, token=TOKEN, telegram=True):
        lines = ([f"IG_ACCESS_TOKEN={token}"] if token else []) + (
            [f"TG_BOT_TOKEN={TG_TOKEN}", f"TG_CHAT_ID={TG_CHAT}"] if telegram else [])
        with open(os.path.join(self.dir, ".env"), "w", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")

    def cycle(self, after=0):
        self.time.advance(after)
        return self.engine.cycle()

    def cycles(self, count, every=SLOT):
        return [self.cycle(after=every) for _ in range(count)]

    def run_for(self, seconds):
        """The loop of Engine.run() for this long: a cycle, then the pause the engine asks for. Returns the
        number of DMs Meta confirmed."""
        end, sent = self.time.wall + seconds, 0
        while self.time.wall < end:
            sent += self.engine.cycle()
            self.time.advance(max(self.engine.pause, 1))
        return sent

    def serve(self, seconds):
        """Engine.run() itself, its `started` line included, until the stop flag goes up after `seconds`
        (as SIGTERM sets it)."""
        self.stop_at = self.time.wall + seconds
        try:
            self.engine.run()
        finally:
            self.stop_at = None

    # ---- the fake's truth

    def truth(self):
        return self.fake.control("/truth")

    def requests(self, *kinds, since=0):
        found = self.fake.control("/truth", query={"since": str(since)})["requests"]
        return [r for r in found if not kinds or r["kind"] in kinds]

    def meta_calls(self, since=0):
        return [r["kind"] for r in self.requests(since=since) if r["kind"] != "telegram"]

    def posts(self, since=0):
        return [(r["kind"], r["target"]) for r in self.requests("dm", "public", since=since)]

    def seq(self):
        return self.truth()["seq"]

    def dms(self):
        return [dm["comment_id"] for dm in self.truth()["dms"]]

    def replies(self):
        return [reply["comment_id"] for reply in self.truth()["replies"]]

    def alerts(self, since=0):
        """Every line that reached the owner's Telegram chat (after request number `since`)."""
        return [line for message in self.truth()["telegram"] if message["seq"] > since
                for line in message["text"].split("\n")]

    def warned(self, key, since=0):
        return [line for line in self.alerts(since) if line.startswith(f"⚠ {key}")]

    def fault(self, **spec):
        return self.fake.control("/faults", {"add": spec})

    def clear_faults(self):
        self.fake.control("/faults", {"clear": True})

    def add(self, *comments, media_id=MEDIA):
        for one in comments:
            self.fake.control("/comments", {"add": {"media_id": media_id, **one}})

    def comments_url(self, media_id=MEDIA, **more):
        """The URL of a comments page exactly as the bot asks for it (what Meta puts into paging.next)."""
        return f"{self.fake.api_base}/{self.cfg.api_version}/{media_id}/comments?" + urlencode(
            {"fields": COMMENT_FIELDS, "limit": 50, "access_token": TOKEN, **more})

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

    def build(self):
        return report.build(self.conn, self.cfg, clock.now())

    def send_hold(self):
        """(reason, whole minutes left) of the send hold in force, or None."""
        hold = store.get_send_hold(self.conn)
        return hold and (hold["reason"], round((clock.parse(hold["until"]) - clock.now()).total_seconds() / 60))

    def account_hold(self):
        hold = store.get_account_hold(self.conn)
        return hold and hold["reason"]

    def logged(self, event):
        """The bot's log lines of this event: `sweep` (a full sweep, or a head poll that stored something) or
        `sweep_failed` (with the note that says why)."""
        return [line for line in self.log.getvalue().splitlines() if line.split(" ")[2:3] == [event]]

    def everything_written(self):
        """Every text the bot wrote anywhere a human or a log collector reads it: stderr (the notes of failed
        sweeps are there), the journal, kv (without the token keys themselves), the alert queue, the announced
        problems, the report, and what reached Telegram."""
        texts = [self.log.getvalue(), report.render(self.build()), json.dumps(self.build(), ensure_ascii=False)]
        for sql in ("SELECT message FROM attempts", "SELECT detail FROM attempts",
                    "SELECT value FROM kv WHERE key NOT IN ('token', 'token_env_seen')", "SELECT text FROM outbox",
                    "SELECT text FROM problems"):
            texts += [str(row[0]) for row in self.conn.execute(sql) if row[0] is not None]
        texts += [message["text"] for message in self.truth()["telegram"]]
        return "\n".join(texts)


# =====================================================================================================
#  HELD, section 4.1 of the catalogue: sending and error handling
# =====================================================================================================

class HeldSendErrors(World):

    def boot_three(self, **more):
        """Three people in the queue, the oldest first; every fault of these tests is aimed at `c1`."""
        return self.boot([comment("c1", age=300), comment("c2", age=200), comment("c3", age=100)], **more)

    def test_cx03_an_answer_without_a_json_error_is_unconfirmed_and_its_http_status_is_journaled_as_received(self):
        """CX-03 (and D3: their client stores a made-up "400" for every answer it cannot read): an answer that
        carries no Graph error code is "delivery unconfirmed", never "invalid request", and the journal holds
        the HTTP status Meta really sent. SPEC 6.1 POST rules 2, 3, 6; SPEC 7 `attempts.http`."""
        answers = {
            "html 502": ({"http": 502, "body": "<html><body>502 Bad Gateway</body></html>"}, 502, "http_5xx"),
            "html 400": ({"http": 400, "body": "<html>Bad Request</html>"}, 400, "unparseable"),
            "plain 403": ({"http": 403, "body": "Forbidden"}, 403, "unparseable"),
            "json 404 without error": ({"http": 404, "body": {"detail": "not found"}}, 404, "unparseable"),
            "json 400, error is a text": ({"http": 400, "body": {"error": "invalid_request"}}, 400, "unparseable"),
            "empty 204": ({"http": 204, "body": ""}, 204, "ok_without_id"),
            "connection dropped": ({"drop": "before"}, 0, "no_response"),
        }
        for name, (fault, http, reason) in answers.items():
            with self.subTest(name):
                self.boot_three()
                self.fault(kind="dm", target="c1", **fault, **({} if "drop" in fault else {"deliver": False}))
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.status("c1"), ("uncertain", reason))
                self.assertEqual(self.attempts("comment_id, kind, http, code, outcome, reason"),
                                 [("c1", "dm", http, None, "uncertain", reason)])
                self.assertNotIn(("refused", "invalid"), self.statuses().values())
                self.assertEqual(self.posts(), [("dm", "c1")])              # one request, no public reply
                self.run_for(2 * HOUR)
                self.assertEqual([target for kind, target in self.posts() if kind == "dm"].count("c1"), 1)
                self.assertEqual(self.status("c1"), ("uncertain", reason))  # never sent again (G4)
        with self.subTest("the same for the public reply"):
            self.boot_three()
            self.fault(kind="public", target="c1", http=502, body="<html>502</html>", deliver=False)
            self.assertEqual(self.cycle(), 1)
            self.assertEqual(self.attempts("comment_id, kind, http, outcome, reason")[1],
                             ("c1", "public", 502, "uncertain", "http_5xx"))
            self.assertEqual(self.status("c1"), ("done", "public_uncertain"))

    def test_cx04_cx85_a_rate_limit_stops_the_batch_at_the_first_refusal_and_is_announced_once(self):
        """CX-04: code 4, 17, 613 (any subcode) or HTTP 429 is a definite rejection AND an account-wide
        condition: no further send in this cycle, the comment stays pending, alert. CX-85: sending stops after
        the first refusal; one alert, not one failed row per comment. SPEC 6.1 rule 4 and 10, 7.3, 8.5, 9."""
        answers = {
            "code 4": {"http": 400, "code": 4, "message": "(#4) Application request limit reached"},
            "code 4 with an unknown subcode": {"http": 400, "code": 4, "subcode": 1349210},
            "code 17": {"http": 400, "code": 17, "message": "(#17) User request limit reached"},
            "code 613": {"http": 400, "code": 613},
            "613/2534040": {"http": 400, "code": 613, "subcode": 2534040},
            "http 429 html": {"http": 429, "body": "<html>Too Many Requests</html>"},
            "http 429 with a per-item code": {"http": 429, "code": 551},
            "business use case 80002": {"http": 400, "code": 80002},
        }
        for name, fault in answers.items():
            with self.subTest(name):
                self.boot_three()
                self.fault(kind="dm", target="c1", deliver=False, **fault)
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.attempts("comment_id, kind, outcome, reason"), [("c1", "dm", "global", "rate")])
                self.assertEqual(self.statuses(), dict.fromkeys(("c1", "c2", "c3"), ("pending", "")))
                self.assertEqual(self.send_hold(), ("send_blocked", 30))
                self.assertIsNone(self.account_hold())                       # GET /me answered: reads go on
                self.assertEqual(len(self.warned("hold:send")), 1)
                self.run_for(29 * MIN)
                self.assertEqual(self.posts(), [("dm", "c1")])               # nobody else was tried meanwhile
                self.assertEqual(self.attempts("outcome"), [("global",)])    # one row, not one per comment
                self.assertEqual(len(self.warned("hold:send")), 1)           # told once
                self.assertEqual(self.statuses(), dict.fromkeys(("c1", "c2", "c3"), ("pending", "")))
                # the limit is over: everybody is served, each exactly once, and the hold is called off
                self.clear_faults()
                self.run_for(10 * MIN)
                self.assertEqual(sorted(self.dms()), ["c1", "c2", "c3"])
                self.assertEqual(self.build()["unaccounted"], 0)
                self.assertIn("✓ cleared: hold:send", self.alerts())

    def test_cx05_cx06_a_dead_token_stops_everything_says_so_and_ends_without_a_human_when_meta_takes_it_again(self):
        """CX-05: code 190 with subcode 458/460/463/467: all sending stops, the alert says a new token is
        needed, comments stay pending. CX-06: code 190 without a subcode: stop and alert, but the token is not
        declared dead for good: the probe goes on and the bot resumes alone. SPEC 8.2, 8.5."""
        for subcode in (458, 460, 463, 467, None):
            with self.subTest(subcode=subcode):
                self.boot_three()
                dead = {"http": 400, "code": 190, "message": "Error validating access token.",
                        **({"subcode": subcode} if subcode else {})}
                # the first cycle reads the reel with a live token; the token dies at the first POST: the DM,
                # the probe that follows it and every call after that are answered 190
                self.fault(kind="dm", deliver=False, **dead)
                for kind in ("me", "media", "comments"):
                    self.fault(kind=kind, nth=list(range(2, 500)), **dead)
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.meta_calls(), ["me", "media", "comments", "dm", "me"])
                self.assertEqual(self.account_hold(), "auth")
                self.assertEqual(self.statuses(), dict.fromkeys(("c1", "c2", "c3"), ("pending", "")))
                (line,) = self.warned("hold:account")
                self.assertIn("нужен новый токен", line)
                self.assertIn("code=190", line)
                calls = len(self.requests())
                self.run_for(4 * HOUR + 5 * MIN)
                self.assertEqual(self.posts(), [("dm", "c1")])               # no POST while Meta refuses the token
                probes = [r["kind"] for r in self.requests()[calls:] if r["kind"] != "telegram"]
                self.assertEqual(set(probes), {"me"})                        # only the probe, on its schedule
                self.assertTrue(8 <= len(probes) <= 10, probes)              # 5, 10, 20 min, then every 30
                self.assertEqual(len(self.warned("hold:account")), 3)        # at 0 h, 2 h, 4 h: repeat_min 120
                self.assertEqual(self.statuses(), dict.fromkeys(("c1", "c2", "c3"), ("pending", "")))
                self.clear_faults()                                          # Meta takes the same token again
                self.run_for(2 * HOUR)
                self.assertEqual(sorted(self.dms()), ["c1", "c2", "c3"])
                self.assertIn("✓ cleared: hold:account", self.alerts())

    def test_cx07_cx08_code_3_and_an_unlisted_oauth_exception_are_not_taken_for_a_dead_token(self):
        """CX-07: code 3 arrives typed OAuthException but is a capability problem: it must not be reported as
        a token problem. CX-08: an unlisted code with type OAuthException is NOT assumed to be an auth failure
        from the type alone. SPEC 6.1 rule 10 (3 -> blocked) and rule 12 (unknown -> uncertain)."""
        with self.subTest("code 3"):
            self.boot_three()
            self.fault(kind="dm", target="c1", http=400, code=3, deliver=False,
                       message="(#3) Application does not have the capability to make this API call.")
            self.assertEqual(self.cycle(), 0)
            self.assertEqual(self.attempts("outcome, reason, code"), [("global", "blocked", 3)])
            self.assertIsNone(self.account_hold())
            self.assertEqual(self.send_hold(), ("send_blocked", 30))
            self.assertEqual(self.status("c1"), ("pending", ""))
            said = "\n".join(self.alerts())
            self.assertIn("⚠ hold:send", said)
            self.assertNotIn("токен", said)                                  # nobody is sent to mint a token
            self.assertEqual(self.kv("token"), TOKEN)
        with self.subTest("unlisted code, type OAuthException"):
            self.boot_three()
            self.fault(kind="dm", target="c1", http=400, code=2635, deliver=False,
                       message="(#2635) You are calling a deprecated version of the API.")
            self.assertEqual(self.cycle(), 0)
            self.assertEqual(self.attempts("outcome, reason"), [("uncertain", "unknown_code_2635")])
            self.assertIsNone(self.account_hold())
            self.assertNotIn("токен", "\n".join(self.alerts()))
            self.assertEqual(self.status("c1"), ("uncertain", "unknown_code_2635"))

    def test_cx09_cx10_cx11_an_account_wide_block_keeps_everybody_pending_and_is_announced(self):
        """CX-09: code 10, 25, 368 or 200..299 without a per-recipient subcode; CX-10: 200/2018028 ("app not
        live", which their mapper files under a messaging-window quota); CX-11: 100/2534029 (their mapper: a
        payload error of one message). All are account-wide: stop, alert, comments stay pending."""
        answers = {
            (10, None): "send_blocked", (25, None): "send_blocked", (25, 2207050): "send_blocked",
            (200, None): "send_blocked", (299, None): "send_blocked", (200, 2018028): "send_blocked",
            (200, 2534041): "send_blocked", (100, 2534029): "send_blocked",
            (368, None): "spam", (368, 1404169): "spam", (10, 1893063): "spam", (613, 2018338): "spam",
        }
        for (code, subcode), hold in answers.items():
            with self.subTest(code=code, subcode=subcode):
                self.boot_three(budget=7)
                self.fault(kind="dm", target="c1", http=400, code=code, deliver=False,
                           **({"subcode": subcode} if subcode else {}))
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.statuses(), dict.fromkeys(("c1", "c2", "c3"), ("pending", "")))
                self.assertEqual(self.send_hold(), (hold, 24 * 60 if hold == "spam" else 30))
                self.assertEqual(store.budget(self.conn), 0 if hold == "spam" else 6)
                self.assertEqual(len(self.warned("hold:send")), 1)
                self.run_for(29 * MIN)
                self.assertEqual(self.posts(), [("dm", "c1")])
                self.assertEqual(self.statuses(), dict.fromkeys(("c1", "c2", "c3"), ("pending", "")))

    def test_cx12_to_cx15_a_refusal_about_one_person_ends_that_comment_only_and_pages_nobody(self):
        """CX-12 (551, 1545041, 10/2018108), CX-13 (2018001, 2534014), CX-14 (100/33), CX-15 (100/2534025,
        10/2534022): terminal for this recipient or comment only; the other comments continue; no account
        pause. L26: such routine answers should not page anyone."""
        answers = {
            (551, None): ("refused", "user_unavailable"), (551, 1545041): ("refused", "user_unavailable"),
            (200, 1545041): ("refused", "user_unavailable"), (10, 2018108): ("refused", "user_unavailable"),
            (100, 2018001): ("refused", "user_unavailable"), (100, 2534014): ("refused", "user_unavailable"),
            (230, None): ("refused", "user_unavailable"),
            (100, 33): ("refused", "comment_gone"),
            (100, 2534025): ("already", "already_replied"), (10, 2534022): ("refused", "outside_window"),
            (10, 2018278): ("refused", "outside_window"),
        }
        for (code, subcode), final in answers.items():
            with self.subTest(code=code, subcode=subcode):
                self.boot_three()
                self.fault(kind="dm", target="c1", http=400, code=code, deliver=False,
                           **({"subcode": subcode} if subcode else {}))
                self.run_for(5 * MIN)
                self.assertEqual(self.statuses(), {"c1": final, "c2": ("done", ""), "c3": ("done", "")})
                self.assertEqual([target for kind, target in self.posts() if kind == "dm"], ["c1", "c2", "c3"])
                self.assertIsNone(self.send_hold())
                self.assertIsNone(self.account_hold())
                self.assertEqual([line for line in self.alerts() if line.startswith("⚠")], [])
                self.assertEqual(self.problems(), [])
                counts = {(status, reason): n for status, reason, n in self.build()["reels"][0]["counts"]}
                self.assertEqual(counts, {final: 1, ("done", ""): 2})        # the miss and its reason are in the report

    def test_cx16_cx18_the_error_is_read_in_every_shape_meta_sends_it_and_journaled_whole(self):
        """CX-16: the subcode under `subcode` instead of `error_subcode`, codes as strings: the same
        classification as the canonical shape. CX-18: the journal holds the code, the subcode and Meta's
        message (SPEC 7 `attempts`: http, code, subcode, message, outcome, reason)."""
        gone = "Unsupported post request. Object with ID 'c1' does not exist, cannot be loaded due to missing " \
               "permissions, or does not support this operation."
        shapes = {
            "canonical": error_body(100, 33, gone),
            "codes as strings": error_body("100", "33", gone),
            "subcode under `subcode`": error_body(100, 33, gone, subcode_key="subcode"),
            "strings under `subcode`": error_body("100", "33", gone, subcode_key="subcode"),
            "whole floats": error_body(100.0, 33.0, gone),
            "with the fields Meta adds": {"error": {**error_body(100, 33, gone)["error"], "is_transient": False,
                                                    "error_user_title": "Не удалось", "error_user_msg": "Нет объекта"}},
        }
        for name, body in shapes.items():
            with self.subTest(name):
                self.boot_three()
                self.fault(kind="dm", target="c1", http=400, body=body, deliver=False)
                self.assertEqual(self.cycle(), 0)
                self.assertEqual(self.attempts("comment_id, kind, http, code, subcode, message, outcome, reason"),
                                 [("c1", "dm", 400, 100, 33, gone, "rejected", "comment_gone")])
                self.assertEqual(self.status("c1"), ("refused", "comment_gone"))
                self.assertIn("kind=dm http=400 code=100 subcode=33 outcome=rejected reason=comment_gone",
                              self.log.getvalue())
        with self.subTest("an account-wide answer with string codes"):
            self.boot_three(budget=5)
            self.fault(kind="dm", target="c1", http=400, deliver=False,
                       body={"error": {"message": "Warning! bothersome", "code": "613", "subcode": "2018338"}})
            self.assertEqual(self.cycle(), 0)
            self.assertEqual(self.attempts("code, subcode, outcome, reason"), [(613, 2018338, "global", "spam")])
            self.assertEqual((self.send_hold(), store.budget(self.conn)), (("spam", 24 * 60), 0))
        with self.subTest("the journal of a failed public reply and of a failed read"):
            self.boot_three()
            self.fault(kind="public", target="c1", http=400, code=100, subcode=33, message=gone, deliver=False)
            self.assertEqual(self.cycle(), 1)
            self.assertEqual(self.attempts("kind, http, code, subcode, message, outcome, reason")[1],
                             ("public", 400, 100, 33, gone, "rejected", "comment_gone"))
            self.fault(kind="comments", http=400, code=100, subcode=2500, message="(#100) Invalid cursor")
            self.cycle(after=SLOT)
            self.assertIn("http=400 code=100 (#100) Invalid cursor", self.logged("sweep_failed")[0])

    def test_cx17_a_2xx_without_message_id_is_not_taken_for_sent_and_not_sent_again(self):
        """CX-17 says "sent" for a 2xx that carries only `recipient_id`. SPEC 6.1 rule 5 decides the opposite
        on purpose (v1 inventory A1): `uncertain`/`ok_without_id`. What must hold either way: one request, the
        person is never asked again, the doubt is announced."""
        self.boot([comment("c1", age=300), comment("c2", age=200)])
        self.fault(kind="dm", target="c1", omit_id=True, deliver=True)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.requests("dm")[0]["response"], {"recipient_id": "u_c1"})
        self.assertEqual(self.status("c1"), ("uncertain", "ok_without_id"))
        self.add(comment("c1-again", "арену", user="u_c1", age=0))
        self.run_for(3 * HOUR)
        self.assertEqual(self.dms(), ["c1", "c2"])                           # one DM each, c1's person included
        self.assertEqual(self.status("c1-again"), ("dup_user", "dup:c1"))
        self.assertEqual(len([line for line in self.alerts() if line.startswith("ℹ uncertain")]), 1)

    def test_cx19_an_awkward_token_that_meta_echoes_reaches_no_log_no_journal_and_no_alert(self):
        """CX-19: no access token in logs, journal or alerts, whatever fails. The token here needs quoting in a
        URL, and Meta (or a proxy in front of it) echoes it in its error texts, as it stands and as it stood in
        the URL. SPEC 3 "tokens, query strings and paging.next URLs are never logged", SPEC 8.2."""
        token = "IGAA|awk ward+tok/en=%41&x#y"
        in_url = urlencode({"access_token": token}).split("=", 1)[1]
        self.assertNotEqual(token, in_url)
        self.boot([comment(f"c{n:02}", age=600 - n) for n in range(4)], token=token, env_token=token,
                  scenario={"limits": {"page_size": 2}})
        echo = f"Malformed access token {token} (request: /v26.0/x?access_token={in_url}&fields=id)"
        self.fault(kind="me", nth=1, http=400, code=190, message=echo)                 # the first probe
        self.fault(kind="comments", nth=2, http=400, code=100, message=echo)           # page 2, a paging.next URL
        self.fault(kind="dm", target="c00", http=400, code=100, message=echo, deliver=False)
        self.fault(kind="dm", target="c01", http=500, code=2, message=echo, deliver=False)
        self.fault(kind="public", target="c02", http=400, code=100, message=echo, deliver=False)
        self.fault(kind="refresh", http=400, code=100, message=echo, deliver=False)
        self.run_for(HOUR)                                                             # hold, reel failure, DMs
        self.time.advance(25 * HOUR)
        self.run_for(13 * HOUR)                                                        # two failed refreshes
        # everything above did happen
        self.assertEqual(self.statuses(), {"c00": ("refused", "invalid"), "c01": ("uncertain", "http_5xx"),
                                           "c02": ("done", "public_failed"), "c03": ("done", "")})
        self.assertGreaterEqual(int(self.kv("token_refresh_fails")), 2)
        self.assertTrue(self.warned("hold:account") and self.warned("token"), self.alerts())
        self.assertTrue(self.logged("sweep_failed"))
        self.assertIn("Malformed access token <token>", self.conn.execute(
            "SELECT message FROM attempts WHERE comment_id='c00'").fetchone()[0])      # the text is kept, the token is not
        written = self.everything_written()
        self.assertGreater(len(written), 5000)
        for form in (token, in_url):
            self.assertNotIn(form, written)
        # the only query strings anywhere are the ones Meta's own text quoted, and their token is gone
        self.assertEqual(written.count("access_token="), written.count("access_token=<token>"))
        self.assertNotIn(self.fake.api_base, written)                                  # no request URL at all

    def test_cx20_cx21_cx22_the_window_is_measured_when_the_dm_would_leave(self):
        """CX-20: a comment created 8 days ago: no API call, explicit status. CX-21: a comment inside the
        window when it was read, outside when its turn comes: treated as outside (age is measured at send
        time). CX-22: 6 days old, no delay: the DM is sent. SPEC 7.2 step 1, 8.1 expire(), 8.3."""
        self.boot([comment("eight_days", age=8 * DAY), comment("edge", age=7 * DAY - 15 * MIN),
                   comment("six_days", age=6 * DAY)], budget=None)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.statuses(), {"eight_days": ("expired", "window"), "edge": ("pending", ""),
                                           "six_days": ("pending", "")})
        self.time.advance(10 * MIN)        # the gate opens ten minutes later: 5 minutes before Meta's own limit
        store.arm(self.conn, "unlimited")
        self.run_for(10 * MIN)
        self.assertEqual(self.posts(), [("dm", "six_days"), ("public", "six_days")])
        self.assertEqual(self.statuses(), {"eight_days": ("expired", "window"), "edge": ("expired", "window"),
                                           "six_days": ("done", "")})
        self.assertTrue(self.truth()["dms"][0]["message"]["text"].startswith(self.cfg.late_greeting))
        counts = {(status, reason): n for status, reason, n in self.build()["reels"][0]["counts"]}
        self.assertEqual(counts, {("expired", "window"): 2, ("done", ""): 1})
        lost = [line for line in self.alerts() if line.startswith("ℹ expired")]
        self.assertEqual(len(lost), 1, lost)                                          # the first loss is told at once
        self.assertTrue(lost[0].startswith("ℹ expired: 1 people lost"), lost)
        self.cycle(after=self.cfg.alerts.repeat_min * MIN)    # the next ones: summed up, once per alerts.repeat_min
        lost = [line for line in self.alerts() if line.startswith("ℹ expired")]
        self.assertEqual(len(lost), 2, lost)                                          # each loss is told, once
        self.assertTrue(lost[1].startswith("ℹ expired: 1 people lost") and "(2 in all)" in lost[1], lost)

    def test_cx23_a_comment_that_got_its_dm_is_never_sent_to_again_by_any_path(self):
        """CX-23: a comment whose DM is journaled as sent is evaluated again by every path there is (the next
        polls, a full sweep, a restart, a config edit, the operator commands): no API call, status unchanged."""
        self.boot([comment("c1")])
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])
        frozen = dict(store.comment(self.conn, "c1"))
        self.cycles(3)                                                       # head polls
        self.cycle(after=31 * MIN)                                           # a full sweep
        self.start()                                                         # a restart: full sweep again
        self.cycles(2)
        self.start(reels=[{"name": "arena", "media_id": MEDIA, "keywords": ["другое"], "also_match": []}])
        store.arm(self.conn, "unlimited")                                    # a changed config disarms (SPEC 4)
        self.cycles(2)                                                       # the keyword is gone ...
        self.start(reels=[{"name": "renamed", "media_id": MEDIA}])           # ... and back, under another name
        store.arm(self.conn, "unlimited")
        self.cycles(2)
        self.assertEqual(self.engine.cycle(only="c1"), 0)                    # `bot.py send-one c1`
        self.fake.control("/comments", {"owner_reply": "c1", "text": "ещё раз привет"})
        self.cycle(after=31 * MIN)
        self.start(public_reply=False)
        store.arm(self.conn, "unlimited")
        self.cycles(2)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])
        self.assertEqual(dict(store.comment(self.conn, "c1")), frozen)
        self.assertEqual(len(self.attempts()), 2)

    def test_cx24_cx25_a_failed_public_reply_is_one_request_its_own_journal_row_and_no_second_dm(self):
        """CX-24: a public reply answered HTTP 500: one request only, journaled apart from the DM. CX-25: DM
        ok, public reply failed: the person is NOT eligible for another DM, and the failure is visible on its
        own. SPEC 7.4."""
        answers = {
            "http 500": ({"http": 500, "body": "<html>500</html>", "deliver": False}, "uncertain", "public_uncertain"),
            "comment deleted meanwhile": ({"http": 400, "code": 100, "subcode": 33, "deliver": False},
                                          "rejected", "public_failed"),
            "answer lost, reply posted": ({"drop": "after"}, "uncertain", "public_uncertain"),
        }
        for name, (fault, outcome, reason) in answers.items():
            with self.subTest(name):
                self.boot([comment("c1", user="ann")])
                self.fault(kind="public", target="c1", **fault)
                self.assertEqual(self.cycle(), 1)
                self.assertEqual(self.attempts("comment_id, kind, outcome"),
                                 [("c1", "dm", "ok"), ("c1", "public", outcome)])
                self.assertEqual(self.status("c1"), ("done", reason))
                self.add(comment("c2", "арену", user="ann", age=0))          # the same person asks again
                self.run_for(2 * HOUR)
                self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1")])
                self.assertEqual(self.dms(), ["c1"])
                self.assertEqual(self.status("c2"), ("dup_user", "dup:c1"))
                data = self.build()
                self.assertIn(["done", reason, 1], data["reels"][0]["counts"])
                self.assertIn({"kind": "public", "outcome": outcome, "reason": self.attempts("reason")[1][0], "n": 1},
                              data["attempts"])
                self.assertEqual(data["unaccounted"], 0)


# =====================================================================================================
#  HELD, section 4.2: one DM per person per reel
# =====================================================================================================

class HeldOnePerPerson(World):

    def test_cx30_to_cx33_one_person_many_comments_two_reels_many_polls(self):
        """CX-30: two keyword comments of one person under one reel in the same poll: exactly one DM, the
        other ends with an explicit status. CX-31: the same in a later poll. CX-32: under another reel: a slot
        of its own. CX-33: a comment listed again in the next polls: no send, no public reply, no counter."""
        self.boot([comment("a1", user="ann", age=300), comment("a2", "Арена!", user="ann", age=200)],
                  more_media={MEDIA_2: [comment("b1", "arena", user="ann", age=250)]})
        self.run_for(5 * MIN)
        self.assertEqual(sorted(self.dms()), ["a1", "b1"])
        self.assertEqual(self.statuses(), {"a1": ("done", ""), "a2": ("dup_user", "dup:a1"), "b1": ("done", "")})
        self.add(comment("a3", "арену", user="ann", age=0))                             # a later poll
        self.add(comment("b2", "арена", user="ann", age=0), media_id=MEDIA_2)
        journal, sweeps = len(self.attempts()), len(self.logged("sweep"))
        self.run_for(20 * MIN)
        self.assertEqual((self.status("a3"), self.status("b2")), (("dup_user", "dup:a1"), ("dup_user", "dup:b1")))
        self.assertEqual(sorted(self.dms()), ["a1", "b1"])
        self.assertEqual(sorted(self.replies()), ["a1", "b1"])
        self.assertEqual(len(self.attempts()), journal)                                 # CX-33: nothing moved
        # two head polls that stored a new comment each logged a `sweep` line; the other polls logged none
        self.assertEqual(len(self.logged("sweep")), sweeps + 2)
        people = {(dm["media_id"], dm["user_id"]) for dm in self.truth()["dms"]}
        self.assertEqual(people, {(MEDIA, "ann"), (MEDIA_2, "ann")})

    def test_cx34_a_write_that_fails_right_after_the_dm_left_never_leads_to_a_second_dm(self):
        """CX-34 (their production bug, commit bd9a24fe): the DM is sent, the journal or statistics write right
        after it raises: no resend on the next cycle, the slot remains taken. Here the first thing after the
        journal write fails: the log line of SPEC 3 (a closed stderr). SPEC 8 "nothing but the journal write
        after the POST"."""
        self.boot([comment("c1", user="ann", age=300), comment("c2", user="bob", age=200)])
        real = engine.log

        def log(level, event, **fields):
            if event == "attempt" and fields.get("kind") == "dm" and fields.get("comment") == "c1":
                raise BrokenPipeError(32, "Broken pipe")
            real(level, event, **fields)
        with mock.patch.object(engine, "log", log):
            self.assertEqual(self.cycle(), 0)                                # the cycle died after the DM
        self.assertEqual(self.dms(), ["c1"])
        self.assertIn("BrokenPipeError", self.kv("last_cycle_error"))
        self.assertTrue(self.warned("cycle_error"))                          # and it was said
        self.add(comment("c3", "арену", user="ann", age=0))
        self.run_for(HOUR)
        self.assertEqual(self.posts(), [("dm", "c1"), ("public", "c1"), ("dm", "c2"), ("public", "c2")])
        self.assertEqual(self.statuses(), {"c1": ("done", ""), "c2": ("done", ""), "c3": ("dup_user", "dup:c1")})
        self.assertEqual(self.build()["unaccounted"], 0)

    def test_cx36_cx37_after_a_doubt_the_person_is_closed_after_a_definite_refusal_the_policy_is_one_more_try(self):
        """CX-36: a DM attempt with an ambiguous outcome, then the same person comments again: no second DM
        (their code would send: D1). CX-37: after a definite per-message rejection the policy must be explicit:
        SPEC 7.2 step 2 lists `sending|sent|done|uncertain|already`, so a person whose comment was `refused`
        may be asked once more through another comment, and only then."""
        self.boot([comment("doubt", user="bob", age=400), comment("refused", user="eve", age=300),
                   comment("other", user="zed", age=200)])
        self.fault(kind="dm", target="doubt", http=500, code=1, deliver=True)           # delivered, answer says no
        self.fault(kind="dm", target="refused", http=400, code=551, deliver=False)
        self.run_for(20 * MIN)
        self.assertEqual((self.status("doubt"), self.status("refused")),
                         (("uncertain", "http_5xx"), ("refused", "user_unavailable")))
        self.add(comment("bob2", user="bob", age=0), comment("eve2", user="eve", age=0))
        self.run_for(HOUR)
        self.add(comment("bob3", user="bob", age=0), comment("eve3", user="eve", age=0))
        self.run_for(HOUR)
        self.assertEqual(self.statuses(), {
            "doubt": ("uncertain", "http_5xx"), "bob2": ("dup_user", "dup:doubt"), "bob3": ("dup_user", "dup:doubt"),
            "refused": ("refused", "user_unavailable"), "eve2": ("done", ""), "eve3": ("dup_user", "dup:eve2"),
            "other": ("done", "")})
        per_person = {}
        for dm in self.truth()["dms"]:
            per_person[dm["user_id"]] = per_person.get(dm["user_id"], 0) + 1
        self.assertEqual(per_person, {"bob": 1, "eve": 1, "zed": 1})                    # G1
        asked = [target for kind, target in self.posts() if kind == "dm"]
        self.assertEqual(sorted(asked), ["doubt", "eve2", "other", "refused"])          # G4: nobody twice

    def test_cx38_what_a_crash_or_a_bad_listing_leaves_behind_does_not_hide_the_comment(self):
        """CX-38 (their bug: a failed job id made redelivery a permanent no-op): a marker left by a failed run
        must not make the comment invisible to processing or to reconciliation."""
        with self.subTest("a comment first listed without a timestamp is served once Meta lists it whole"):
            self.boot([comment("late", age=120), comment("fine", age=60)])
            self.fault(kind="comments", nth=1, body={"data": [
                {"id": "late", "text": "арена", "from": {"id": "u_late", "username": "name_u_late"}, "hidden": False},
                {"id": "fine", "text": "арена", "timestamp": "2026-10-05T11:59:00+0000",
                 "from": {"id": "u_fine", "username": "name_u_fine"}, "hidden": False}]})
            self.assertEqual(self.cycle(), 1)
            self.assertEqual(self.status("late"), ("seen", "malformed"))
            self.run_for(35 * MIN)                                           # the next full sweep sees it whole
            self.assertEqual(self.status("late"), ("done", ""))
            self.assertEqual(sorted(self.dms()), ["fine", "late"])
        with self.subTest("a claim a dead run left open is closed, counted and announced"):
            self.boot([comment("c1", user="ann", age=300), comment("c2", user="bob", age=200)])
            real = self.client.send_dm

            def send_dm(*args):
                real(*args)
                raise _Killed()
            with mock.patch.object(self.client, "send_dm", send_dm), self.assertRaises(_Killed):
                self.cycle()
            self.assertEqual(self.status("c1"), ("sending", ""))
            self.start()
            self.run_for(20 * MIN)
            self.assertEqual(self.statuses(), {"c1": ("uncertain", "interrupted"), "c2": ("done", "")})
            data = self.build()
            self.assertIn(["uncertain", "interrupted", 1], data["reels"][0]["counts"])
            self.assertIn({"kind": "dm", "outcome": "uncertain", "reason": "interrupted", "n": 1}, data["attempts"])
            self.assertEqual(data["unaccounted"], 0)
            self.assertEqual(len([line for line in self.alerts() if line.startswith("ℹ uncertain")]), 1)
            self.assertEqual(self.dms(), ["c1", "c2"])


# =====================================================================================================
#  HELD, section 4.3: never DM someone who did not write the keyword
# =====================================================================================================

class HeldOnlyRequests(World):

    def test_cx42_a_comment_without_a_text_field_is_stored_as_no_request_and_breaks_nothing(self):
        """CX-42: a comment with no `text` field (GIF, sticker): empty text, no match, an explicit status."""
        self.boot([comment("gif", age=300, raw={"text": None}), comment("gif_again", age=250, raw={"text": None}),
                   comment("empty", "", age=200), comment("spaces", "  \n ", age=150), comment("real", age=100)])
        self.run_for(35 * MIN)                                               # two full sweeps and the head polls
        listed = {item["id"]: item for item in self.requests("comments")[0]["response"]["data"]}
        self.assertNotIn("text", listed["gif"])                              # Meta sent no `text` field at all
        self.assertEqual(self.statuses(), {"gif": ("seen", ""), "gif_again": ("seen", ""), "empty": ("seen", ""),
                                           "spaces": ("seen", ""), "real": ("done", "")})
        self.assertIsNone(store.comment(self.conn, "gif")["text"])
        self.assertEqual(self.posts(), [("dm", "real"), ("public", "real")])
        data = self.build()                                                  # the report takes rows without a text
        self.assertEqual((data["unaccounted"], data["unmatched"], data["problems"]), (0, [], []))
        self.assertIn("seen 4", report.render(data))
        self.assertIsNone(self.kv("last_cycle_error"))

    def test_cx46_a_reply_with_the_keyword_under_somebody_elses_comment_messages_nobody(self):
        """CX-46: a reply to a comment that contains the keyword follows the declared policy. The policy of
        this core is SPEC 1 "read the top-level comments": a reply is no request, whoever wrote it, and it must
        never make the bot message the author of the comment above it."""
        said = {"text": "арена", "from": {"id": "replier", "username": "name_replier"}}
        self.boot([comment("chat", "красивый ролик", user="ann", age=300, should_match=False, replies=[said]),
                   comment("asked", "арена", user="bob", age=200, replies=[{**said, "text": "и мне арена"}])])
        self.run_for(35 * MIN)
        self.assertEqual(self.posts(), [("dm", "asked"), ("public", "asked")])
        self.assertEqual(self.statuses(), {"chat": ("seen", ""), "asked": ("done", "")})
        self.assertEqual({dm["user_id"] for dm in self.truth()["dms"]}, {"bob"})

    def test_cx47_a_reel_that_is_not_in_the_config_is_never_read_and_its_people_never_messaged(self):
        """CX-47: a keyword comment under a reel that is not configured: no DM."""
        self.boot([comment("a1")], more_media={MEDIA_2: [comment("b1", age=30)]},
                  reels=[{"name": "arena", "media_id": MEDIA}])
        self.run_for(35 * MIN)
        self.assertEqual(self.posts(), [("dm", "a1"), ("public", "a1")])
        self.assertEqual({r["target"] for r in self.requests("media", "comments")}, {MEDIA})
        self.assertIsNone(self.status("b1"))
        # the reel is added to the config: its people are found and served; nothing is taken for handled
        self.start(reels=[{"name": "arena", "media_id": MEDIA}, {"name": "second", "media_id": MEDIA_2}])
        store.arm(self.conn, "unlimited")                                    # a changed config disarms (SPEC 4)
        self.run_for(5 * MIN)
        self.assertEqual(self.status("b1"), ("done", ""))
        # ... and removed again while somebody waits there: that is never silent (SPEC 9 `unaccounted`)
        self.add(comment("b2", age=0), media_id=MEDIA_2)
        store.arm(self.conn, 0)
        self.cycle(after=SLOT)
        self.assertEqual(self.status("b2"), ("pending", ""))
        self.start(reels=[{"name": "arena", "media_id": MEDIA}])
        store.arm(self.conn, "unlimited")
        self.run_for(35 * MIN)
        self.assertEqual(self.status("b2"), ("pending", ""))
        self.assertNotIn("b2", self.dms())
        self.assertIn("unaccounted", self.problem_keys())
        self.assertTrue(self.warned("unaccounted"))


# =====================================================================================================
#  HELD, section 4.4: polling
# =====================================================================================================

class HeldPolling(World):

    def test_cx52_a_list_that_never_ends_is_cut_at_the_page_cap_said_aloud_and_does_not_stop_the_other_reel(self):
        """CX-52: the API returns a next link for ever: the scan stops at a page cap AND reports that it was
        cut (theirs stops silently: D9). SPEC 8.3: 400 pages, `reel_state=capped`, problem, no silent cap; one
        reel failing never stops the others."""
        self.boot([comment("a1", age=300)], more_media={MEDIA_2: [comment("b1", age=200)]})
        self.assertEqual(self.cycles(2), [1, 1])
        page = {"data": [{"id": "a1", "text": "арена", "timestamp": "2026-10-05T11:55:00+0000",
                          "from": {"id": "u_a1", "username": "name_u_a1"}, "hidden": False}],
                "paging": {"cursors": {"before": "QQ==", "after": "QQ=="}, "next": self.comments_url(after="QQ==")}}
        self.fault(kind="comments", target=MEDIA, body=page)       # the same page, with a next link, for ever
        self.add(comment("b2", age=0), media_id=MEDIA_2)
        before = self.seq()
        self.cycle(after=SLOT)                                     # a head poll: the page holds nothing new
        self.assertEqual(self.meta_calls(since=before).count("comments"), 2)   # one page of each reel
        before = self.seq()
        self.cycle(after=31 * MIN)                                 # a full sweep
        asked = [r for r in self.requests("comments", since=before) if r["target"] == MEDIA]
        self.assertEqual(len(asked), engine.MAX_PAGES)
        self.assertEqual(self.kv("reel_state:" + MEDIA), "capped")
        self.assertTrue(self.warned(f"reel:{MEDIA}"))
        self.assertIn("400 страниц", self.warned(f"reel:{MEDIA}")[0])
        capped = [line for line in self.logged("sweep_failed") if f" media_id={MEDIA} " in line][-1]
        self.assertIn(f" pages={engine.MAX_PAGES} ", capped)
        self.assertEqual(self.status("b2"), ("done", ""))          # the other reel was read and served meanwhile
        before = self.seq()
        self.run_for(25 * MIN)                                     # not 400 pages in every cycle
        self.assertEqual([r for r in self.requests("comments", since=before) if r["target"] == MEDIA], [])
        self.assertNotIn(TOKEN, self.everything_written())         # 400 next links, each with the token in it

    def test_cx55_a_burst_whose_second_page_fails_is_found_whole_by_the_next_full_sweep(self):
        """CX-55: page 2 of 3 fails: the cycle is marked failed, nothing is taken for handled from the partial
        data, the next cycle starts clean. Here the page fails in a head poll (the existing test covers the
        first full sweep): the 70 older comments of a burst of 120 are behind the page that failed."""
        self.boot([comment(f"old{n}", "красиво", age=3600 + n, should_match=False) for n in range(5)], budget=None)
        self.cycle()
        self.add(*(comment(f"n{n:03}", age=n) for n in range(120)))          # n000 is the newest
        self.fault(kind="comments", nth=2, http=500, body="<html>500</html>")
        self.cycle(after=SLOT)
        self.assertEqual(self.kv("reel_fails:" + MEDIA), "1")
        self.assertRegex(self.logged("sweep_failed")[-1], r" pages=1 comments=50 new_rows=50 ")
        store.arm(self.conn, "unlimited")
        self.run_for(31 * MIN)                                               # until the next full sweep is over
        known = {row[0] for row in self.conn.execute("SELECT comment_id FROM comments")}
        self.assertEqual({f"n{n:03}" for n in range(120)} - known, set())    # everybody is in the database
        self.run_for(2 * HOUR + 10 * MIN)
        self.assertEqual(sorted(self.dms()), [f"n{n:03}" for n in range(120)])          # ... and is served, once
        self.assertEqual(self.build()["unaccounted"], 0)

    def test_cx57_a_backlog_is_worked_off_oldest_first_at_the_configured_rate_and_the_rest_keeps_waiting(self):
        """CX-57: a backlog of matched comments after downtime: oldest first, paced, cap respected; the rest
        remain pending, not failed. G2 measured where the fake received the POSTs."""
        self.boot([comment(f"c{n:03}", age=3 * HOUR - n * 30) for n in range(150)], budget=None)   # c000 is the oldest
        self.cycle()
        store.arm(self.conn, "unlimited")
        self.run_for(2 * HOUR + 5 * MIN)
        sent = self.requests("dm")
        times = [r["t"] for r in sent]
        self.assertEqual([r["target"] for r in sent], [f"c{n:03}" for n in range(len(sent))])     # oldest first
        self.assertTrue(110 <= len(sent) <= 125, len(sent))                                       # 60 per hour
        self.assertGreaterEqual(min(later - earlier for earlier, later in zip(times, times[1:])), SLOT)
        for index, first in enumerate(times):                                                     # any rolling hour
            self.assertLessEqual(sum(1 for t in times[index:] if t < first + HOUR), 60)
        statuses = self.statuses()
        self.assertEqual({statuses[f"c{n:03}"] for n in range(len(sent))}, {("done", "")})
        self.assertEqual({statuses[f"c{n:03}"] for n in range(len(sent), 150)}, {("pending", "")})
        data = self.build()
        self.assertEqual((data["unaccounted"], data["problems"]), (0, []))
        self.assertTrue(data["verdict"].startswith("OK: в очереди"), data["verdict"])
        self.assertEqual(data["reels"][0]["waiting_reason"], "cap")


# =====================================================================================================
#  HELD, section 4.5 and the taxonomy of section 3: journal, statuses, reconciliation
# =====================================================================================================

class HeldEveryMissHasAReason(World):

    def test_cx60_cx63_cx66_cx68_every_way_not_to_get_a_dm_is_one_row_with_a_reason_and_is_in_the_report(self):
        """CX-60: each decline path leaves exactly one durable row with a reason (their rule: one test case per
        `continue`). CX-63: "attempted, not delivered" is a class apart from "filtered out". CX-66: accounted
        for = a row for every comment Meta listed. CX-68: attempted = delivered + failed + unconfirmed, the
        declines counted apart. Section 3 of the lessons: every reason a comment did not get a DM, theirs
        A (miss), B (failed event) and C (no row at all), must be visible in statuses, reasons and the report.
        """
        refused_by_meta = {"blocked_dm": (551, None, "user_unavailable"), "gone": (100, 33, "comment_gone"),
                           "window": (10, 2534022, "outside_window"), "invalid": (100, 2534015, "invalid")}
        queue = ["blocked_dm", "ok1", "gone", "ok2", "window", "ok3", "invalid", "ok4", "doubt", "ok5", "spent",
                 "ann1", "ann2", "reply_failed"]
        rows = [comment(name, age=HOUR - index * 60, **({"user": "ann"} if name.startswith("ann") else {}),
                        **({"private_replied": True} if name == "spent" else {}))
                for index, name in enumerate(queue)]
        rows += [
            comment("no_match", "красиво", age=500, should_match=False),
            comment("long", "мне очень нужна эта арена прямо сейчас", age=490, should_match=False),
            comment("question", "зачем арена?", age=480, should_match=False),
            {**comment("no_author", age=470), "from": None},
            comment("own", user=ME["id"], age=460, should_match=False),
            comment("malformed", age=450, raw={"timestamp": "вчера"}),
            comment("too_old", age=8 * DAY),
            comment("owner", age=440, replies=[{"text": "отправил", "from": OWNER}]),
        ]
        self.boot(rows, budget=None)
        for name, (code, subcode, _) in refused_by_meta.items():
            self.fault(kind="dm", target=name, http=400, code=code, deliver=False,
                       **({"subcode": subcode} if subcode else {}))
        self.fault(kind="dm", target="doubt", http=503, body="<html>503</html>", deliver=False)
        self.fault(kind="public", target="reply_failed", http=400, code=100, subcode=33, deliver=False)
        self.cycle()
        store.arm(self.conn, "unlimited")
        self.run_for(HOUR)
        self.add(comment("waits", age=0))                                    # ... and one behind a closed gate
        store.arm(self.conn, 0)
        self.run_for(HOUR)

        expected = {
            **{name: ("refused", reason) for name, (_, _, reason) in refused_by_meta.items()},
            **{f"ok{n}": ("done", "") for n in range(1, 6)},
            "doubt": ("uncertain", "http_5xx"), "spent": ("already", "already_replied"),
            "ann1": ("done", ""), "ann2": ("dup_user", "dup:ann1"),
            "reply_failed": ("done", "public_failed"),
            "no_match": ("seen", ""), "long": ("seen", "near:too_long"), "question": ("seen", "near:question"),
            "no_author": ("seen", "no_author"), "own": ("seen", "own"), "malformed": ("seen", "malformed"),
            "too_old": ("expired", "window"), "owner": ("already", "owner_replied"), "waits": ("pending", ""),
        }
        self.assertEqual(self.statuses(), expected)
        served = {name for name, final in expected.items() if final[0] == "done"}
        self.assertEqual(sorted(self.dms()), sorted(served))

        # CX-60: no miss without a reason (a comment that is no request is `seen` with the empty reason)
        for name, (status, reason) in expected.items():
            if name not in served and name not in ("no_match", "waits"):
                self.assertTrue(reason, name)
        # CX-63: who was tried has a journal row for the DM, who was filtered out has none
        tried = {row[0] for row in self.conn.execute("SELECT DISTINCT comment_id FROM attempts WHERE kind='dm'")}
        self.assertEqual(tried, served | set(refused_by_meta) | {"doubt", "spent"})
        # the report: every (status, reason) with its number, and why the one who waits is waiting
        data = self.build()
        (reel,) = data["reels"]
        counted = {(status, reason): n for status, reason, n in reel["counts"]}
        wanted = {}
        for status, reason in expected.values():
            key = (status, "" if status == "dup_user" else reason)
            wanted[key] = wanted.get(key, 0) + 1
        self.assertEqual(counted, wanted)
        text = report.render(data)
        for (status, reason), n in wanted.items():
            self.assertIn(f"{status}{'/' + reason if reason else ''} {n}", text)
        self.assertEqual((reel["people_waiting"], reel["waiting_reason"]), (1, "no_budget"))
        self.assertEqual(data["near"]["total"], 2)
        self.assertEqual({row["comment_id"]: row["reason"] for row in data["near"]["newest"]},
                         {"long": "too_long", "question": "question"})
        self.assertEqual(data["unmatched"], [["красиво", 1]])
        # CX-66: a row for every comment Meta ever listed, and nothing unaccounted
        listed = {one["id"] for one in self.fake.control("/comments")["comments"] if one["first_listed_seq"]}
        self.assertEqual(listed, set(expected))
        self.assertEqual(data["unaccounted"], 0)
        # CX-68: attempted = delivered + failed + unconfirmed, against what the fake received
        journal = {(row["kind"], row["outcome"], row["reason"]): row["n"] for row in data["attempts"]}
        self.assertEqual(sum(n for (kind, _, _), n in journal.items() if kind == "dm"), len(self.requests("dm")))
        self.assertEqual(journal[("dm", "ok", "")], len(self.dms()))
        self.assertEqual(journal, {
            ("dm", "ok", ""): 7, ("dm", "rejected", "user_unavailable"): 1, ("dm", "rejected", "comment_gone"): 1,
            ("dm", "rejected", "outside_window"): 1, ("dm", "rejected", "invalid"): 1,
            ("dm", "rejected", "already_replied"): 1, ("dm", "uncertain", "http_5xx"): 1,
            ("public", "ok", ""): 6, ("public", "rejected", "comment_gone"): 1})
        self.assertEqual(sum(row["posts"] for row in data["dms_per_hour"]), 13)
        self.assertEqual(sum(row["ok"] for row in data["dms_per_hour"]), 7)
        self.assertRegex(self.logged("sweep")[0], r" full=True .* malformed=1$")


# =====================================================================================================
#  HELD, section 4.6: account, token, operations
# =====================================================================================================

class HeldTokenAndSecrets(World):

    def test_cx82_cx83_a_failing_refresh_is_kept_and_announced_until_one_succeeds_and_the_new_token_is_used(self):
        """CX-82: a failed token refresh is stored as persistent state and alerted until a refresh succeeds.
        CX-83: on success the new token and its expiry are stored together and the error state is cleared.
        SPEC 8.2 (problem `token` after two failures in a row; refresh at most once per 6 h after a failure)."""
        self.boot([comment("c1")], scenario={"token": {"value": TOKEN, "rotate": True}})
        self.assertEqual(self.cycle(), 1)
        self.fault(kind="refresh", http=400, code=2, message="Service temporarily unavailable", deliver=False)
        self.run_for(24 * HOUR + 10 * MIN)
        self.assertEqual((self.kv("token_refresh_fails"), self.kv("token")), ("1", TOKEN))
        self.assertNotIn("token", self.problem_keys())                       # one failure is not yet a problem
        self.run_for(5 * HOUR)
        self.assertEqual(len(self.requests("refresh")), 1)                   # not again within 6 hours
        self.run_for(HOUR + 5 * MIN)
        self.assertEqual(self.kv("token_refresh_fails"), "2")
        self.assertEqual(len(self.warned("token")), 1)
        self.start()                                                         # the state survives a restart
        self.run_for(4 * HOUR)
        self.assertIn("token", self.problem_keys())
        self.assertGreaterEqual(len(self.warned("token")), 2)                # repeated while it lasts
        self.assertIsNone(self.account_hold())                               # a refresh never holds the account
        self.clear_faults()
        self.run_for(3 * HOUR)
        fresh = TOKEN + ".r1"
        self.assertEqual((self.kv("token"), self.kv("token_refresh_fails"), self.kv("token_refresh_failed_at")),
                         (fresh, None, None))
        left = (clock.parse(self.kv("token_expires_at")) - clock.now()).total_seconds()
        self.assertTrue(59 * DAY < left <= 60 * DAY, left)
        self.assertNotIn("token", self.problem_keys())
        self.assertIn("✓ cleared: token", self.alerts())
        # Meta takes only the new token now, and `.env` still holds the old one: nothing falls back to it
        self.add(comment("c2", age=0))
        self.start()
        self.run_for(10 * MIN)
        self.assertEqual(self.dms(), ["c1", "c2"])
        self.assertEqual(self.kv("token"), fresh)
        self.assertEqual({r["params"]["access_token"] for r in self.requests("dm", "comments")[-2:]}, {fresh})
        self.assertNotIn(fresh, self.everything_written())

    def test_cx84_without_the_token_nothing_is_called_the_owner_is_told_and_the_token_alone_starts_the_work(self):
        """CX-84: a required secret is absent at start-up: the bot does not work blind and it alerts.
        SPEC 8.2: no token anywhere -> account hold `no_token` and no network call."""
        self.boot([comment("c1")], env_token=None)
        self.run_for(3 * HOUR)
        self.assertEqual(self.meta_calls(), [])
        self.assertEqual(self.account_hold(), "no_token")
        told = self.warned("hold:account")
        self.assertEqual(len(told), 2)                                       # at once, and again after 2 hours
        self.assertIn("IG_ACCESS_TOKEN", told[0])
        self.assertTrue(self.build()["verdict"].startswith("PROBLEM: hold:account"))
        self.write_env(TOKEN)                                                # the owner adds the line; no restart
        self.run_for(5 * MIN)
        self.assertEqual(self.dms(), ["c1"])
        self.assertIn("✓ cleared: hold:account", self.alerts())


# =====================================================================================================
#  HELD, section 2: their error mapper and Meta's own table against meta.classify()
# =====================================================================================================

class HeldErrorMapper(unittest.TestCase):
    """Every code of their mapper (2.1) and every row of Meta's table (2.3), answered to a DM with HTTP 400.
    What matters is the class of safety: an answer after which the comment is sent to AGAIN (`global`,
    `not_sent`) must be a definite refusal of the whole account; an answer that may hide a delivery must be
    `uncertain`; nothing is `ok`."""

    ACCOUNT_WIDE = {   # 2.1 AUTH_FAILED, PERMISSION_DENIED, RATE_LIMITED and the rows of 2.3 marked account-wide
        (190, None): "auth", (190, 458): "auth", (190, 460): "auth", (190, 463): "auth", (190, 467): "auth",
        (3, None): "blocked", (10, None): "blocked", (25, None): "blocked", (25, 2207050): "blocked",
        (200, None): "blocked", (250, None): "blocked", (299, None): "blocked",
        (200, 2018028): "blocked", (200, 2534041): "blocked", (100, 2534029): "blocked",
        (4, None): "rate", (17, None): "rate", (613, None): "rate", (613, 2534040): "rate", (32, None): "rate",
        (80001, None): "rate", (80006, None): "rate", (341, None): "rate",
        (368, None): "spam", (4, 2207051): "spam", (613, 2018338): "spam", (10, 1893063): "spam",
    }
    PER_ITEM = {       # 2.1 USER_BLOCKED, INVALID_RECIPIENT and the rows of 2.3 marked "definite for this message"
        (551, None): "user_unavailable", (551, 1545041): "user_unavailable", (200, 1545041): "user_unavailable",
        (100, 2018001): "user_unavailable", (100, 2534014): "user_unavailable", (10, 2018108): "user_unavailable",
        (230, None): "user_unavailable",
        (100, 33): "comment_gone", (100, 2534025): "already_replied",
        (10, 2534022): "outside_window", (10, 2018278): "outside_window",
        (100, None): "invalid", (100, 2207020): "invalid", (100, 2207052): "invalid",
    }
    # 2.1 NETWORK_ERROR (-1, -2; Messenger: 1, 2) and every code their mapper files as "permanent" although
    # nothing proves that the message was not delivered or that the trouble is this one comment
    AMBIGUOUS = (1, -1, 2, -2, 9, 352, 9004, 9007, 36000, 36001, 36003, 36004, 102, 459, 464, 492, 2635, 0)

    def answer(self, code, subcode, http=400, kind="OAuthException"):
        return meta.classify("POST", "dm", http, error_body(code, subcode, kind=kind), False)

    def test_their_table_and_metas_table(self):
        for (code, subcode), reason in self.ACCOUNT_WIDE.items():
            with self.subTest(code=code, subcode=subcode):
                self.assertEqual(self.answer(code, subcode), ("global", reason))
        for (code, subcode), reason in self.PER_ITEM.items():
            with self.subTest(code=code, subcode=subcode):
                self.assertEqual(self.answer(code, subcode), ("rejected", reason))
        for code in self.AMBIGUOUS:
            with self.subTest(code=code):
                outcome, reason = self.answer(code, None)
                self.assertEqual(outcome, "uncertain")
                self.assertTrue(reason)

    def test_the_error_type_never_decides_and_a_5xx_never_proves_a_refusal(self):
        """Their fallback "type == OAuthException => AUTH_FAILED" (D4) and "HTTP status is not an input" (2.1)."""
        for kind in ("OAuthException", "GraphMethodException", "IGApiException", "", None, 5):
            with self.subTest(type=kind):
                self.assertEqual(self.answer(999, None, kind=kind), ("uncertain", "unknown_code_999"))
                self.assertEqual(self.answer(3, None, kind=kind), ("global", "blocked"))
                self.assertEqual(self.answer(190, None, kind=kind), ("global", "auth"))
        for (code, subcode) in [*self.ACCOUNT_WIDE, *self.PER_ITEM]:
            with self.subTest(code=code, subcode=subcode):
                self.assertEqual(self.answer(code, subcode, http=500), ("uncertain", "http_5xx"))
                self.assertEqual(self.answer(code, subcode, http=200)[0], "uncertain")


# =====================================================================================================
#  HELD: the two lessons about a bot that runs and reaches nobody (CX-70, D15)
# =====================================================================================================

class HeldNothingMatches(World):

    def test_cx70_a_keyword_nobody_writes_reaches_the_owner_in_the_daily_digest_as_the_word_they_do_write(self):
        """CX-70 (their production bug: automations stored under the wrong type never matched, with no log):
        the keyword in the config is misspelled, so nothing ever matches. SPEC 5: "unknown typos are found by a
        human in the report's list of frequent short unmatched texts"; SPEC 8.7: the daily digest."""
        rows = [comment(f"p{n:02}", "Арена" if n % 2 else "арена!", age=HOUR + n * 60, should_match=False)
                for n in range(40)]
        rows += [comment(f"x{n}", text, age=600 + n, should_match=False)
                 for n, text in enumerate(["красиво", "огонь", "красиво", "спасибо большое"])]
        self.boot(rows, reels=[{"name": "arena", "media_id": MEDIA, "keywords": ["арнеа"], "also_match": []}],
                  alerts={"repeat_min": 120, "digest_hour_utc": 13})
        self.serve(70 * MIN)                                                 # 12:00 .. 13:10: the digest is due at 13:00
        self.assertEqual(self.posts(), [])
        self.assertEqual(set(self.statuses().values()), {("seen", "")})
        (digest,) = [message["text"] for message in self.truth()["telegram"] if "ℹ digest" in message["text"]]
        self.assertIn("  40 × арена", digest)
        self.assertLess(digest.index("  40 × арена"), digest.index("× красиво"))        # the most frequent first
        self.assertEqual(self.build()["unmatched"][0], ["арена", 40])

    def test_d15_an_api_version_meta_no_longer_serves_is_announced_in_minutes_and_costs_nobody(self):
        """D15: a hard-coded API version: "a version sunset turns into errors on every call". The version is
        config here (SPEC 4); what must hold when Meta refuses it on every call: nobody is written off, the
        owner hears of it within minutes, and the bot goes on alone once the calls are answered again."""
        self.boot([comment("c1", age=300), comment("c2", age=200)], budget=None)
        self.cycle()
        store.arm(self.conn, "unlimited")
        for kind in ("me", "media", "comments", "dm", "public", "refresh"):
            self.fault(kind=kind, http=400, code=2635, message="(#2635) You are calling a deprecated version of the API.",
                       **({"deliver": False} if kind in ("dm", "public", "refresh") else {}))
        self.run_for(10 * MIN)
        self.assertEqual(self.posts(), [])                                   # the reel could not be read: no POST
        self.assertEqual(len(self.warned(f"reel:{MEDIA}")), 1)
        self.assertEqual(self.statuses(), {"c1": ("pending", ""), "c2": ("pending", "")})
        self.run_for(3 * HOUR)
        self.assertEqual(self.posts(), [])
        self.assertTrue(self.warned("waiting"))
        self.assertIn("code=2635", self.logged("sweep_failed")[0])
        self.clear_faults()
        self.run_for(HOUR)
        self.assertEqual(self.statuses(), {"c1": ("done", ""), "c2": ("done", "")})
        self.assertEqual(self.problems(), [])


# =====================================================================================================
#  FINDINGS: each of these fails on the current code
# =====================================================================================================




# =====================================================================================================
#  The decision for every scenario of the catalogue
# =====================================================================================================
#
#  N  not applicable to this core (the note says why)
#  C  already covered by existing tests or soak scenarios (named; "+ here" where this file adds an end-to-end run)
#  H  not covered before, tested in this file, the product holds
#  F  not covered before, tested in this file, the product FAILS
#
#  References: "tests.<module>:<Class>.<method>", "soak:<named scenario of sim/scenarios.py>", "here:<Class>.<method>".
#  test_the_catalogue_is_decided_completely checks that every scenario of the lessons file has a decision and
#  that every reference names a test that exists.

_E, _S, _C, _M = "tests.test_engine:", "tests.test_store:", "tests.test_classify:", "tests.test_meta_client:"
_SEND, _ONE = "here:HeldSendErrors.", "here:HeldOnePerPerson."
_TABLE = "here:HeldEveryMissHasAReason.test_cx60_cx63_cx66_cx68_every_way_not_to_get_a_dm_is_one_row_with_a_reason_and_is_in_the_report"
_WINDOW = _SEND + "test_cx20_cx21_cx22_the_window_is_measured_when_the_dm_would_leave"
_DOUBT = _E + "DmOutcomes.test_uncertain_answers_are_final_and_hold_the_sending"
_ITEM = _E + "DmOutcomes.test_per_item_rejections_are_final_at_once"
_POST_RULES = _C + "Table.test_post_rules"
_NEVER = _E + "Collect.test_what_is_never_sent_to"

CATALOGUE = {
    # ---- 4.1 send and error handling
    "CX-01": ("C", "one request on HTTP 500, never again", [
        _DOUBT, _M + "Answers.test_500_with_code_190_is_uncertain_for_a_post", "soak:g4"]),
    "CX-02": ("C", "dropped connection / timeout = uncertain, hold, event; the person's slot stays taken", [
        _DOUBT, _M + "MaybeSent.test_post_read_then_dropped_without_an_answer",
        _S + "ClaimDm.test_one_dm_per_person_and_media", "soak:g4"]),
    "CX-03": ("H", "the class was covered (DmOutcomes 'http_5xx html', 'not json'); the HTTP status in the journal was not", [
        _SEND + "test_cx03_an_answer_without_a_json_error_is_unconfirmed_and_its_http_status_is_journaled_as_received"]),
    "CX-04": ("H", "classification was covered (test_classify, DmOutcomes); the alert and 'nobody else is tried' with the real notifier were not", [
        _SEND + "test_cx04_cx85_a_rate_limit_stops_the_batch_at_the_first_refusal_and_is_announced_once",
        _E + "DmOutcomes.test_account_wide_refusals_defer_the_comment_hold_the_sending_and_probe"]),
    "CX-05": ("H", "190 with each subcode at the POST: account hold auth, the alert asks for a new token, all stay pending", [
        _SEND + "test_cx05_cx06_a_dead_token_stops_everything_says_so_and_ends_without_a_human_when_meta_takes_it_again"]),
    "CX-06": ("C", "the hold is lifted by the probe alone (+ here)", [
        _E + "AccountHold.test_a_block_that_starts_later_is_found_by_the_read_and_confirmed_by_the_probe",
        _SEND + "test_cx05_cx06_a_dead_token_stops_everything_says_so_and_ends_without_a_human_when_meta_takes_it_again"]),
    "CX-07": ("H", "classify row existed; end to end: no auth hold, no word about the token in the alert", [
        _SEND + "test_cx07_cx08_code_3_and_an_unlisted_oauth_exception_are_not_taken_for_a_dead_token"]),
    "CX-08": ("C", "err(999) carries type OAuthException and is uncertain/unknown_code_999 (+ here)", [
        _POST_RULES, "here:HeldErrorMapper.test_the_error_type_never_decides_and_a_5xx_never_proves_a_refusal"]),
    "CX-09": ("C", "10, 25, 368, 200..299 are account-wide (+ here, end to end with the alert)", [
        _POST_RULES, _E + "DmOutcomes.test_account_wide_refusals_defer_the_comment_hold_the_sending_and_probe",
        _SEND + "test_cx09_cx10_cx11_an_account_wide_block_keeps_everybody_pending_and_is_announced"]),
    "CX-10": ("H", "200/2018028 was in no table", [
        _SEND + "test_cx09_cx10_cx11_an_account_wide_block_keeps_everybody_pending_and_is_announced",
        "here:HeldErrorMapper.test_their_table_and_metas_table"]),
    "CX-11": ("C", "100/2534029 is an account-wide block", [
        _E + "DmOutcomes.test_messaging_blocked_while_reads_work", "soak:messaging_blocked"]),
    "CX-12": ("C", "551, 200/1545041, 10/2018108 end this comment only (+ here: nobody is paged)", [
        _ITEM, _POST_RULES, _SEND + "test_cx12_to_cx15_a_refusal_about_one_person_ends_that_comment_only_and_pages_nobody"]),
    "CX-13": ("C", "100/2018001, 100/2534014", [_ITEM, _POST_RULES]),
    "CX-14": ("C", "100/33 -> refused/comment_gone", [_ITEM, _POST_RULES]),
    "CX-15": ("C", "100/2534025 -> already/already_replied, 10/2534022 -> refused/outside_window", [
        _E + "DmOutcomes.test_already_replied_in_all_three_spellings", _ITEM]),
    "CX-16": ("C", "string codes, `subcode` key (+ here, over the wire into the journal)", [
        _POST_RULES, _M + "Answers.test_error_fields_are_read_from_the_body",
        _SEND + "test_cx16_cx18_the_error_is_read_in_every_shape_meta_sends_it_and_journaled_whole"]),
    "CX-17": ("N", "SPEC 6.1 rule 5 decides the opposite on purpose (v1 inventory A1): 2xx without message_id is "
                   "uncertain/ok_without_id. Pinned by the existing test; the safe half (one request, never again) is shown here", [
        _M + "Answers.test_200_without_the_id_is_uncertain",
        _SEND + "test_cx17_a_2xx_without_message_id_is_not_taken_for_sent_and_not_sent_again"]),
    "CX-18": ("H", "code, subcode, Meta's message and the HTTP status in the journal, end to end. Meta's "
                   "error_user_msg is not kept: SPEC 7 gives the journal one `message` column", [
        _SEND + "test_cx16_cx18_the_error_is_read_in_every_shape_meta_sends_it_and_journaled_whole"]),
    "CX-19": ("H", "unit tests existed per module (test_meta_client Secrets, test_notify Log, test_report); "
                   "an awkward token that Meta echoes, through every output of a running bot, did not", [
        _SEND + "test_cx19_an_awkward_token_that_meta_echoes_reaches_no_log_no_journal_and_no_alert"]),
    "CX-20": ("C", "8 days old: expired/window, no call (+ here)", [_NEVER, _WINDOW]),
    "CX-21": ("C", "the window is checked at the claim and by expire() in every cycle (+ here)", [
        _S + "ClaimDm.test_window_margin", _S + "Comments.test_expire_moves_pending_rows_past_the_window",
        _E + "AccountHold.test_expire_and_the_announcement_run_during_an_account_hold", _WINDOW]),
    "CX-22": ("H", "a 6-day-old comment is sent to, with the late greeting, against the fake's own window", [_WINDOW]),
    "CX-23": ("H", "every path of re-evaluation in one run", [
        _SEND + "test_cx23_a_comment_that_got_its_dm_is_never_sent_to_again_by_any_path"]),
    "CX-24": ("C", "one public request, its own journal row", [_E + "PublicReply.test_failure_modes"]),
    "CX-25": ("H", "the person's next comment after a failed public reply", [
        _SEND + "test_cx24_cx25_a_failed_public_reply_is_one_request_its_own_journal_row_and_no_second_dm"]),
    "CX-26": ("N", "the core has no media payload: the public reply is one configured text sent as the `message` "
                   "query parameter", [_M + "Wire.test_public_reply_sends_the_message_as_a_query_parameter"]),
    # ---- 4.2 one DM per person per reel
    "CX-30": ("C", "two comments of one person in one poll (+ here)", [
        _S + "Races.test_two_comments_of_one_person", "soak:g1",
        _ONE + "test_cx30_to_cx33_one_person_many_comments_two_reels_many_polls"]),
    "CX-31": ("C", "the same person in a later poll", [_E + "Limits.test_one_dm_per_person_and_media"]),
    "CX-32": ("C", "another reel is another slot", [
        _S + "ClaimDm.test_same_person_on_another_media_and_another_person_on_this_one", "soak:g1"]),
    "CX-33": ("C", "a comment listed again is a no-op (+ here: journal and sweeps counted)", [
        _E + "HappyPath.test_nothing_to_do_is_one_head_poll", _S + "Comments.test_add_stores_the_comment_once",
        _ONE + "test_cx30_to_cx33_one_person_many_comments_two_reels_many_polls"]),
    "CX-34": ("H", "the first write after the journal fails (the log line)", [
        _ONE + "test_cx34_a_write_that_fails_right_after_the_dm_left_never_leads_to_a_second_dm"]),
    "CX-35": ("C", "kill between claim and record", [
        _E + "CrashAndStop.test_kill_between_claim_and_record_is_uncertain_and_never_sent_again", "soak:kill_points"]),
    "CX-36": ("C", "uncertain closes the person (+ here, end to end)", [
        _S + "ClaimDm.test_one_dm_per_person_and_media", "soak:g1",
        _ONE + "test_cx36_cx37_after_a_doubt_the_person_is_closed_after_a_definite_refusal_the_policy_is_one_more_try"]),
    "CX-37": ("H", "the policy is explicit in SPEC 7.2 step 2 (refused does not close the person); end to end it "
                   "gives exactly one DM", [
        _ONE + "test_cx36_cx37_after_a_doubt_the_person_is_closed_after_a_definite_refusal_the_policy_is_one_more_try"]),
    "CX-38": ("H", "a malformed first listing heals at the next full sweep; an interrupted claim is counted "
                   "and announced", [
        _ONE + "test_cx38_what_a_crash_or_a_bad_listing_leaves_behind_does_not_hide_the_comment"]),
    "CX-39": ("C", "the instance lock", [
        "tests.test_cli:Service.test_lock_is_held_by_the_service_and_released_at_its_end", "soak:kill_points"]),
    # ---- 4.3 never DM someone who did not write the keyword
    "CX-40": ("C", "own comments by user_id, id, username", [_NEVER, "soak:g3"]),
    "CX-41": ("C", "no author: seen/no_author", [_NEVER, "soak:g3"]),
    "CX-42": ("H", "a comment without a `text` field", [
        "here:HeldOnlyRequests.test_cx42_a_comment_without_a_text_field_is_stored_as_no_request_and_breaks_nothing"]),
    "CX-43": ("C", "case", ["tests.test_match:Check.test_table"]),
    "CX-44": ("C", "a keyword of symbols is refused at load",
              ["tests.test_config:Rejected.test_keyword_that_is_not_one_word_of_two_letters"]),
    "CX-45": ("C", "an empty keyword list is refused at load", ["tests.test_config:Rejected.test_empty_keywords"]),
    "CX-46": ("H", "the declared policy is SPEC 1: top-level comments only. A reply has no row and no status by "
                   "design; tested: a keyword reply messages neither its author nor the author above it", [
        "here:HeldOnlyRequests.test_cx46_a_reply_with_the_keyword_under_somebody_elses_comment_messages_nobody"]),
    "CX-47": ("H", "a reel outside the config is never read; rows of a reel taken out of the config are unaccounted", [
        "here:HeldOnlyRequests.test_cx47_a_reel_that_is_not_in_the_config_is_never_read_and_its_people_never_messaged"]),
    "CX-48": ("C", "whole tokens only («Марена», «аренда» do not match)", ["tests.test_match:Check.test_no_fuzzy_rule"]),
    # ---- 4.4 polling
    "CX-50": ("C", "every page on a full sweep; a comment deep in the list is found by the next full sweep", [
        _E + "Collect.test_full_sweep_reads_every_page_and_a_head_poll_only_what_is_new", "soak:g5"]),
    "CX-51": ("C", "paging ends on the missing `next` (the fake sends cursors on the last page)", [
        _E + "Collect.test_full_sweep_reads_every_page_and_a_head_poll_only_what_is_new"]),
    "CX-52": ("H", "existing test patched MAX_PAGES to 2; here a next link that never ends, 400 real pages, the alert, the other reel", [
        "here:HeldPolling.test_cx52_a_list_that_never_ends_is_cut_at_the_page_cap_said_aloud_and_does_not_stop_the_other_reel",
        _E + "Collect.test_reel_states"]),
    "CX-53": ("C", "+0000 (and every engine test: the fake sends that format)", ["tests.test_clock:Timestamps.test_parse"]),
    "CX-54": ("C", "seen/malformed, counted in the sweep note", [
        _E + "Collect.test_a_malformed_comment_is_stored_apart_and_the_rest_of_the_page_is_handled", "soak:g5"]),
    "CX-55": ("H", "the first full sweep was covered (Gates.test_no_dm_for_a_reel_before_its_full_sweep_completed); "
                   "a head poll whose second page fails was not", [
        "here:HeldPolling.test_cx55_a_burst_whose_second_page_fails_is_found_whole_by_the_next_full_sweep",
        _E + "Gates.test_no_dm_for_a_reel_before_its_full_sweep_completed"]),
    "CX-56": ("N", "a reply is no request in this core (SPEC 1, 8.3: `replies{id,from}` is read only to find the "
                   "owner's reply); every top-level comment is judged by its own timestamp, whatever its replies", []),
    "CX-57": ("H", "150 waiting, two hours: order, slot and rolling hour at the receiver, the rest pending", [
        "here:HeldPolling.test_cx57_a_backlog_is_worked_off_oldest_first_at_the_configured_rate_and_the_rest_keeps_waiting"]),
    "CX-58": ("C", "expired/window, counted, announced (+ here)", [
        _NEVER, _E + "AccountHold.test_expire_and_the_announcement_run_during_an_account_hold", "soak:g5", _WINDOW]),
    # ---- 4.5 journal, statuses, reconciliation
    "CX-60": ("H", "one run through every decline path, each with its row, its reason and its line in the report", [_TABLE]),
    "CX-61": ("C", "behind a closed gate the comment stays pending and is sent once the gate opens", [
        _E + "Gates.test_each_closed_gate_means_no_post", "soak:g6", "soak:outage_gate"]),
    "CX-62": ("C", "cycle_error, comment untouched", [
        _E + "CrashAndStop.test_an_unexpected_exception_is_recorded_announced_and_the_next_cycle_runs"]),
    "CX-63": ("C", "refused / uncertain / expired are statuses apart from seen (+ here: journal rows only for the tried)", [
        _S + "RecordDm.test_every_outcome", _TABLE]),
    "CX-64": ("C", "`sending` until Meta answered", [
        _S + "ClaimDm.test_claim_is_committed_before_it_returns",
        _E + "CrashAndStop.test_kill_between_claim_and_record_is_uncertain_and_never_sent_again"]),
    "CX-65": ("C", "a claim is always followed by one POST and `sent` needs Meta's ok; a config that cannot give a "
                   "message is refused at load", [
        _S + "RecordDm.test_every_outcome", "tests.test_config:Rejected.test_wrong_types_and_ranges"]),
    "CX-66": ("C", "`unaccounted` (+ here: a row for every comment the fake ever listed)", [
        "tests.test_report:Unaccounted.test_nothing_is_unaccounted_on_the_normal_path", _TABLE]),
    "CX-67": ("C", "an answer is recorded once", [
        _S + "RecordDm.test_an_answer_is_recorded_once_and_only_for_a_dm_claim",
        _S + "Public.test_an_answer_is_recorded_once_and_only_for_a_public_claim"]),
    "CX-68": ("H", "the report's attempt totals against what the fake received and delivered", [_TABLE]),
    "CX-69": ("N", "nothing is ever pruned (SPEC 7)", []),
    "CX-70": ("H", "no reel is read (switched off or none configured): problem `no_active_reel`. A misspelled "
                   "keyword shows in the digest as the word people write, also once a reel has 30 long near-miss "
                   "comments (both failed before round 1)", [
        "tests.test_regress_r1:ChatBotXFindingsNothingIsRead.test_cx70_a_bot_that_reads_no_reel_at_all_says_ok_for_ever",
        "tests.test_regress_r1:ChatBotXFindingsDigest.test_the_daily_digest_still_names_the_word_people_write_instead_of_the_keyword",
        "here:HeldNothingMatches.test_cx70_a_keyword_nobody_writes_reaches_the_owner_in_the_daily_digest_as_the_word_they_do_write"]),
    # ---- 4.6 account, token, operations
    "CX-80": ("N", "account_type is not asked for (SPEC 6: me() asks id, user_id, username, followers_count, media_count)", []),
    "CX-81": ("N", "as CX-80; an account the API does not serve fails GET /me and ends in an account hold", []),
    "CX-82": ("H", "unit level existed (Token.test_a_failed_refresh_..., test_report Token); the chain 'kept, "
                   "announced, repeated, cleared by the next success' with the real report did not", [
        "here:HeldTokenAndSecrets.test_cx82_cx83_a_failing_refresh_is_kept_and_announced_until_one_succeeds_and_the_new_token_is_used"]),
    "CX-83": ("C", "token and expiry stored together (+ here, with a rotating token)", [
        _E + "Token.test_refresh_policy",
        "here:HeldTokenAndSecrets.test_cx82_cx83_a_failing_refresh_is_kept_and_announced_until_one_succeeds_and_the_new_token_is_used"]),
    "CX-84": ("C", "no token: no call, hold no_token, told on Telegram (+ here)", [
        _E + "Token.test_no_token_anywhere_means_no_call_at_all", "soak:outage_secret",
        "here:HeldTokenAndSecrets.test_cx84_without_the_token_nothing_is_called_the_owner_is_told_and_the_token_alone_starts_the_work"]),
    "CX-85": ("H", "one POST, one journal row, one alert for three people in the queue", [
        _SEND + "test_cx04_cx85_a_rate_limit_stops_the_batch_at_the_first_refusal_and_is_announced_once"]),
}


class Catalogue(unittest.TestCase):

    def test_the_catalogue_is_decided_completely(self):
        """Every CX scenario of research/lessons/chatbotx.md has a decision, and every reference exists."""
        import importlib
        if os.path.exists(LESSONS):
            with open(LESSONS, encoding="utf-8") as file:
                listed = set(re.findall(r"^\| (CX-\d+) \|", file.read(), re.M))
            self.assertEqual(listed, set(CATALOGUE))
        self.assertEqual(len(CATALOGUE), 71)
        named = importlib.import_module("sim.scenarios").NAMED
        for scenario, (decision, note, references) in CATALOGUE.items():
            with self.subTest(scenario):
                self.assertIn(decision, "NCHF")
                self.assertTrue(note)
                self.assertTrue(references or decision == "N")
                for reference in references:
                    where, _, name = reference.partition(":")
                    if where == "soak":
                        self.assertIn(name, named)
                        continue
                    module = sys.modules[__name__] if where == "here" else importlib.import_module(where)
                    owner, method = name.split(".")
                    self.assertTrue(callable(getattr(getattr(module, owner), method)), reference)
                    if decision in "HF":
                        self.assertTrue(any(one.startswith("here:") for one in references))
        counts = {decision: sum(1 for one in CATALOGUE.values() if one[0] == decision) for decision in "NCHF"}
        self.assertEqual(counts, {"N": 6, "C": 41, "H": 24, "F": 0})
