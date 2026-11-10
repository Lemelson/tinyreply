"""Tests of the referee: for every invariant a hand-made truth + database pair that breaks it and is caught,
next to the same situation done right, which passes. Then the scenario generator and the soak's mechanics.

Run from autoreply/:  python3.12 -B -m unittest sim.test_invariants
"""
import collections
import contextlib
import copy
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import config
import match
from sim import invariants, scenarios, soak
from sim.fake_meta import FakeMeta

HERE = os.path.dirname(os.path.abspath(__file__))

T0 = 1_790_000_000.0    # scaled unix time at which the hand-made runs begin
DAY = 86400
MEDIA, OTHER_MEDIA = "18000000000000001", "18000000000000002"
LINK, OTHER_LINK = "https://example.com/arena", "https://example.com/other"
PROFILE = "https://www.instagram.com/your_account/"
GREETING, LATE_GREETING = "Привет! 👋", "Привет! 👋 Лучше поздно, чем никогда 😄"
PHRASES = ["Отправил в директ 📩", "Лови в личке 🔥"]
TOKEN = "IGAAsecret1"


def reel(name, media_id, link):
    return {"name": name, "media_id": media_id, "permalink": "", "active": True, "keywords": ["арена", "arena"],
            "also_match": ["арееа"], "max_words": 5, "link": link, "text": "{greeting} Держи ссылку 👇"}


CONFIG = {"api_version": "v26.0", "sending": True, "poll_interval_sec": 30, "max_sends_per_hour": 60,
          "public_reply": True, "profile_url": PROFILE, "greeting": GREETING,
          "late_greeting": LATE_GREETING, "late_after_minutes": 60, "public_phrases": PHRASES,
          "reels": [reel("arena", MEDIA, LINK), reel("other", OTHER_MEDIA, OTHER_LINK)],
          "alerts": {"repeat_min": 120, "digest_hour_utc": 6}}

SCHEMA = """   -- SPEC section 7, copied by hand: the contract the referee reads
CREATE TABLE comments (
  comment_id TEXT PRIMARY KEY, media_id TEXT NOT NULL,
  user_id TEXT, username TEXT, text TEXT, ts TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('seen','pending','sending','sent','done','uncertain','refused','expired','dup_user','already')),
  reason TEXT NOT NULL DEFAULT '',
  deferrals INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY, comment_id TEXT NOT NULL, kind TEXT NOT NULL,
  detail TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  http INTEGER, code INTEGER, subcode INTEGER, message TEXT,
  outcome TEXT, reason TEXT, meta_id TEXT);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE metrics (ts TEXT PRIMARY KEY, followers INTEGER, media_count INTEGER);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, key TEXT, text TEXT, created_at TEXT, sent_at TEXT);
CREATE TABLE problems (key TEXT PRIMARY KEY, text TEXT, since TEXT, last_announced_at TEXT);
"""


def iso(unix):
    moment = datetime.fromtimestamp(round(unix * 1000) / 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def error(code, subcode=None, message="refused"):
    body = {"message": message, "type": "OAuthException", "code": code}
    return {"error": body if subcode is None else {**body, "error_subcode": subcode}}


class Case:
    """A hand-made run: what the fake saw (`truth`), what the bot wrote (`db`), what the harness did (`events`).
    Times are seconds after T0. Starts empty: every method adds one fact."""

    def __init__(self, test, automatic_journal=True):
        self.automatic_journal = automatic_journal
        self.dir = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.dir)
        self.db_path = os.path.join(self.dir, "core.db")
        self.db = sqlite3.connect(self.db_path, isolation_level=None)
        test.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)
        self.truth = {"seq": 0, "mode": "ok", "requests": [], "dms": [], "replies": [], "telegram": [],
                      "events": [{"after_seq": 0, "t": T0, "op": "mode", "mode": "ok"}], "comments": []}
        self.scenario = {"config": copy.deepcopy(CONFIG), "faults": {}, "may_expire": [], "fake": {"token": TOKEN},
                         "env": {"IG_ACCESS_TOKEN": TOKEN, "TG_BOT_TOKEN": "123:tg-secret", "TG_CHAT_ID": "42"}}
        self.events = []
        self.event("start", at=-10)

    # ---- the fake's side

    def comment(self, comment_id, user="u1", text="арена", age=600, should_match=True, media=MEDIA, **more):
        author = None if user is None else {"id": user, "username": "user_" + user}
        state = {"id": comment_id, "media_id": media, "text": text, "ts": T0 - age, "from": author, "hidden": False,
                 "should_match": should_match, "private_replied": False, "listed": True, "raw": {},
                 "deleted": False, "first_listed_seq": None, "own": False, "replies": [], **more}
        self.truth["comments"].append(state)
        return state

    def _find(self, comment_id):
        return next((c for c in self.truth["comments"] if c["id"] == comment_id), None)

    def request(self, kind, at, target=None, status=200, response=None, **more):
        self.truth["seq"] += 1
        record = {"seq": self.truth["seq"], "t_real_ms": 0, "t": T0 + at, "method": "GET", "path": "/v26.0/x",
                  "params": {}, "body": None, "kind": kind, "target": target, "mode": self.truth["mode"],
                  "token_ok": True, "strict": None, "fault_id": None, "held": None, "status": status,
                  "response": {} if response is None else response, "delivered": False, "sent": True, **more}
        self.truth["requests"].append(record)
        return record

    def last(self, kind):
        return [request for request in self.truth["requests"] if request["kind"] == kind][-1]

    def listed(self, *comment_ids, at=0):
        """One sweep page that listed these comments."""
        record = self.request("comments", at, target=MEDIA)
        for comment_id in comment_ids:
            self._find(comment_id)["first_listed_seq"] = record["seq"]
        return record

    def dm(self, comment_id, at, status=200, response=None, delivered=None, text=None, **more):
        """A POST /messages. By default Meta's success, delivered, with the payload the config asks for."""
        seq = self.truth["seq"] + 1
        if response is None and status == 200:
            response = {"recipient_id": "x", "message_id": f"mid.{seq}"}
        if delivered is None:
            delivered = status == 200 and "message_id" in response
        comment = self._find(comment_id)
        if text is None:
            late = comment is not None and T0 + at - comment["ts"] > CONFIG["late_after_minutes"] * 60
            link = OTHER_LINK if comment and comment["media_id"] == OTHER_MEDIA else LINK
            text = f"{LATE_GREETING if late else GREETING} Держи ссылку 👇\n\n{link}\nПодписаться: {PROFILE}"
        body = {"recipient": {"comment_id": comment_id}, "message": {"text": text}}
        record = self.request("dm", at, target=comment_id, status=status, response=response, method="POST",
                              body=body, delivered=delivered, **more)
        if self.automatic_journal:
            problem = response.get("error", {}) if isinstance(response, dict) else {}
            code = problem.get("code")
            outcome = ("ok" if delivered and status == 200 else "global" if code in (190, 200, 4, 613, 368)
                       else "uncertain" if status >= 500 or code in (1, -1, 2) or not record["sent"] else "rejected")
            self.db.execute("INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome,reason) "
                            "VALUES (?,'dm',?,?,?,?)", (comment_id, iso(T0 + at - .1), iso(T0 + at + .1),
                            outcome, "already_replied" if code == 10900 else ""))
        if delivered:
            self.truth["dms"].append({"seq": seq, "t": T0 + at, "comment_id": comment_id,
                                      "media_id": comment["media_id"] if comment else MEDIA,
                                      "user_id": comment and comment["from"] and comment["from"]["id"],
                                      "message": body["message"]})
        return record

    def public(self, comment_id, at, text=PHRASES[0], status=200, delivered=None, **more):
        seq = self.truth["seq"] + 1
        delivered = status == 200 if delivered is None else delivered
        record = self.request("public", at, target=comment_id, status=status, method="POST",
                              response={"id": f"r{seq}"} if status == 200 else error(100, 33),
                              params={"message": text}, delivered=delivered, **more)
        if delivered:
            self.truth["replies"].append({"seq": seq, "t": T0 + at, "comment_id": comment_id, "id": f"r{seq}",
                                          "text": text})
            self.owner_reply(comment_id, by_api=True)
        return record

    def owner_reply(self, comment_id, by_api=False):
        self._find(comment_id)["replies"].append({"id": "r", "text": "ok", "from": None, "ts": T0, "own": True,
                                                  "by_api": by_api})

    def mode(self, mode, at):
        self.truth["mode"] = mode
        self.truth["events"].append({"after_seq": self.truth["seq"], "t": T0 + at, "op": "mode", "mode": mode})

    def telegram(self, text, at):
        self.truth["telegram"].append({"seq": 0, "t": T0 + at, "chat_id": "42", "text": text})

    # ---- the bot's side

    def row(self, comment_id, status, reason="", at=0, media=None):
        comment = self._find(comment_id)
        author = comment and comment["from"]
        self.db.execute("INSERT OR REPLACE INTO comments VALUES (?,?,?,?,?,?,?,?,0,?,?)", (
            comment_id, media or (comment["media_id"] if comment else MEDIA), author and author["id"],
            author and author["username"], comment and comment["text"], iso(comment["ts"] if comment else T0),
            status, reason, iso(T0), iso(T0 + at)))

    def attempt(self, comment_id, kind="dm", outcome="ok", finished=True):
        existing = self.db.execute("SELECT id FROM attempts WHERE comment_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                                   (comment_id, kind)).fetchone() if self.automatic_journal else None
        if existing:
            self.db.execute("UPDATE attempts SET outcome=?, finished_at=CASE WHEN ? THEN finished_at ELSE NULL END "
                            "WHERE id=?", (outcome, finished, existing[0]))
        else:
            self.db.execute("INSERT INTO attempts (comment_id, kind, started_at, finished_at, outcome) VALUES (?,?,?,?,?)",
                            (comment_id, kind, iso(T0), iso(T0) if finished else None, outcome))

    def problem(self, key, text):
        self.db.execute("INSERT INTO problems VALUES (?,?,?,?)", (key, text, iso(T0), iso(T0)))

    # ---- the harness's side

    def event(self, op, at=0, **more):
        if op in ("arm", "disarm") and self.automatic_journal:
            more.setdefault("boundary_id", self.db.execute("SELECT COALESCE(MAX(id),0) FROM attempts WHERE kind='dm'").fetchone()[0])
        if op == "config" and self.automatic_journal:
            more.setdefault("boundary_id", self.db.execute("SELECT COALESCE(MAX(id),0) FROM attempts WHERE kind='dm'").fetchone()[0])
        entry = {"op": op, "seq": self.truth["seq"], "t": T0 + at, **more}
        self.events.append(entry)
        return entry

    def served(self, comment_id, user, at, **comment):
        """One person handled the way the spec wants: listed, armed, DM, public reply, row done, journal."""
        self.comment(comment_id, user, **comment)
        self.listed(comment_id, at=at - 5)
        self.event("arm", at=at - 4, n=1, rc=0)
        self.dm(comment_id, at)
        self.public(comment_id, at + 3)
        self.row(comment_id, "done", at=at + 3)
        self.attempt(comment_id)

    def seen(self, comment_id, user="u9", text="круто", reason="", **comment):
        self.comment(comment_id, user, text=text, should_match=False, **comment)
        self.listed(comment_id)
        self.row(comment_id, "seen", reason)

    def waiting(self, comment_id, user, status="pending", reason="", at=0, said=True, **comment):
        """A request the bot stored and has not answered. While it is `pending` in a run that is still going,
        the scenario expects, and the bot reports (`said`), why: no budget."""
        self.comment(comment_id, user, **comment)
        self.listed(comment_id)
        self.row(comment_id, status, reason, at=at)
        if status == "pending" and said and "expect" not in self.scenario:
            self.scenario["expect"] = {"waiting": "no_budget"}
            self.problem("waiting", "1 waiting: no_budget, oldest 40 min")

    def check(self):
        # These legacy fixtures exercise I1-I12 semantic units without process telemetry. G6 and legacy
        # INCONCLUSIVE are covered by test_i06_regressions; never turn these fixtures into release traces.
        return [v for v in invariants.check(self.truth, self.db_path, self.scenario, self.events)
                if v.invariant != "INCONCLUSIVE"]

    def broken(self):
        """Names of the invariants this run violates."""
        return sorted({violation.invariant for violation in self.check()})


class RefereeTest(unittest.TestCase):
    def case(self):
        return Case(self)

    def healthy(self):
        """Two people served a slot apart, a comment that is no request, and the run settled."""
        case = self.case()
        case.served("c1", "u1", at=100)
        case.served("c2", "u2", at=200)
        case.seen("c3")
        return case

    def assert_clean(self, case):
        self.assertEqual(case.check(), [])


class HealthyRun(RefereeTest):
    def test_a_run_done_right_breaks_nothing(self):
        case = self.healthy()
        case.event("settle", at=300)
        self.assert_clean(case)

    def test_a_violation_names_invariant_person_and_evidence(self):
        case = self.healthy()
        case.dm("c1", 900)            # delivered a second time
        violation = next(violation for violation in case.check() if violation.invariant == "I1")
        self.assertEqual(violation.subject, f"user u1 media {MEDIA}")
        self.assertEqual(violation.evidence, "2 DMs delivered: seq 2 for comment c1, seq 8 for comment c1")
        self.assertEqual(case.broken(), ["I1", "I4", "I5", "I7"])   # ... and everything else that is wrong with it

    def test_replay_reads_a_kept_run_directory(self):
        case = self.healthy()
        run = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, run)
        os.mkdir(os.path.join(run, "state"))
        shutil.copy(case.db_path, os.path.join(run, "state", "core.db"))
        for name, value in (("truth", case.truth), ("scenario", case.scenario), ("events", case.events)):
            with open(os.path.join(run, name + ".json"), "w", encoding="utf-8") as file:
                json.dump(value, file)
        replay = [sys.executable, "-B", os.path.join(HERE, "invariants.py"), run]
        done = subprocess.run(replay, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("INCONCLUSIVE", done.stdout)
        case.dm("c1", 900)
        with open(os.path.join(run, "truth.json"), "w", encoding="utf-8") as file:
            json.dump(case.truth, file)
        done = subprocess.run(replay, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("I1 user u1 media", done.stdout)

    def test_a_missing_database_means_every_listed_comment_lacks_its_row(self):
        case = self.healthy()
        violations = invariants.check(case.truth, os.path.join(case.dir, "absent.db"), case.scenario, case.events)
        self.assertEqual({v.invariant for v in violations}, {"I5", "I7", "I8", "INCONCLUSIVE"})


class I1OneDmPerPerson(RefereeTest):
    def test_two_comments_of_one_person_both_answered(self):
        case = self.healthy()
        case.served("c4", "u1", at=300)
        self.assertEqual(case.broken(), ["I1"])

    def test_second_comment_of_the_person_held_back_is_clean(self):
        case = self.healthy()
        case.waiting("c4", "u1", "dup_user", "dup:c1", at=300)
        self.assert_clean(case)

    def test_same_person_under_two_reels_may_get_both(self):
        case = self.healthy()
        case.served("c4", "u1", at=300, media=OTHER_MEDIA)
        self.assert_clean(case)


class I2Rate(RefereeTest):
    def test_two_dms_closer_than_a_slot(self):
        case = self.healthy()
        case.served("c4", "u4", at=259)    # 59 s after c2, the slot at 60 per hour is 60 s
        self.assertEqual(case.broken(), ["I2"])

    def test_exactly_one_slot_apart_is_clean(self):
        case = self.healthy()
        case.served("c4", "u4", at=260)
        self.assert_clean(case)

    def test_more_than_the_cap_in_a_rolling_hour(self):
        case = self.healthy()             # two DMs under 60 per hour, then a restart with 2 per hour
        slow = {**copy.deepcopy(CONFIG), "max_sends_per_hour": 2}
        case.event("config", at=250, config=slow, relabel={})
        case.served("c4", "u4", at=200 + 1800)    # a full new slot later, but the third DM of that hour
        self.assertEqual(case.broken(), ["I2"])
        self.assertIn("the cap is 2", case.check()[0].evidence)

    def test_the_hour_is_rolling_and_ends(self):
        case = self.healthy()
        case.event("config", at=250, config={**copy.deepcopy(CONFIG), "max_sends_per_hour": 2}, relabel={})
        case.served("c4", "u4", at=100 + 3600)    # c1 has just left the hour
        self.assert_clean(case)


class I3OnlyRequests(RefereeTest):
    """The DM is refused by the fake in each case, so that nothing but the target is wrong with the run."""

    def stray(self, case, comment_id):
        case.event("arm", at=290, n=1, rc=0)
        case.dm(comment_id, 300, status=400, response=error(100, 33))

    def test_dm_to_a_comment_that_is_no_request(self):
        case = self.healthy()
        self.stray(case, "c3")
        self.assertEqual(case.broken(), ["I3"])

    def test_dm_to_the_accounts_own_comment(self):
        case = self.healthy()
        case.seen("c4", "me", text="пиши арена", reason="own", own=True)
        case.truth["comments"][-1]["should_match"] = True
        self.stray(case, "c4")
        self.assertEqual(case.broken(), ["I3"])
        self.assertIn("own comment", case.check()[0].evidence)

    def test_dm_to_a_comment_without_author(self):
        case = self.healthy()
        case.seen("c4", None, text="арена", reason="no_author")
        case.truth["comments"][-1]["should_match"] = True
        self.stray(case, "c4")
        self.assertEqual(case.broken(), ["I3"])

    def test_dm_to_a_comment_the_fake_never_held(self):
        case = self.healthy()
        self.stray(case, "ghost")
        self.assertEqual(case.broken(), ["I3"])

    def test_dm_after_the_config_stopped_accepting_the_text(self):
        case = self.healthy()
        case.waiting("c4", "u4", "seen", text="арееа")
        stricter = copy.deepcopy(CONFIG)
        stricter["reels"][0]["also_match"] = []
        case.event("config", at=250, config=stricter, relabel={"c4": False})
        self.stray(case, "c4")
        self.assertEqual(case.broken(), ["I3"])

    def test_dm_before_that_config_change_is_clean(self):
        case = self.healthy()
        case.served("c4", "u4", at=300, text="арееа")
        stricter = copy.deepcopy(CONFIG)
        stricter["reels"][0]["also_match"] = []
        case.event("config", at=400, config=stricter, relabel={"c4": False})
        self.assert_clean(case)

    def test_dm_under_a_reel_switched_off(self):
        case = self.healthy()
        case.waiting("c4", "u4")
        parked = copy.deepcopy(CONFIG)
        parked["reels"][0]["active"] = False
        case.event("config", at=250, config=parked, relabel={})
        self.stray(case, "c4")
        self.assertEqual(case.broken(), ["I3"])


class I4NeverAgainAfterDoubt(RefereeTest):
    def twice(self, first_status, first_response):
        """c4 gets a DM with that answer, then c4 gets a second DM."""
        case = self.healthy()
        case.comment("c4", "u4")
        case.listed("c4")
        case.event("arm", at=290, n=2, rc=0)
        case.first = case.dm("c4", 300, status=first_status, response=first_response, delivered=False)
        case.dm("c4", 2200)                 # past every hold the first answer could have set
        case.public("c4", 2203)
        case.row("c4", "done", at=2203)
        case.attempt("c4")
        return case

    def test_second_post_after_an_ambiguous_answer(self):
        case = self.twice(500, error(2))
        self.assertEqual(case.broken(), ["I4"])
        self.assertIn("ambiguous", case.check()[0].evidence)

    def test_second_post_after_a_per_item_refusal(self):
        self.assertEqual(self.twice(400, error(10, 2018108)).broken(), ["I4"])

    def test_second_post_after_already_replied(self):
        self.assertEqual(self.twice(400, error(10900)).broken(), ["I4"])

    def test_second_post_after_an_account_wide_refusal_is_clean(self):
        self.assert_clean(self.twice(400, error(190)))

    def test_the_scenario_catalogue_decides_what_a_fault_means(self):
        case = self.twice(400, error(4))                    # code 4 is account-wide by the table ...
        self.assert_clean(case)
        case.first["fault_id"] = "odd"                      # ... unless the scenario's author says otherwise
        case.scenario["faults"]["odd"] = {"klass": "item"}
        self.assertEqual(case.broken(), ["I4"])

    def test_no_answer_at_all_counts_as_doubt(self):
        case = self.twice(400, error(190))
        case.first["sent"] = False                          # the bot was killed before the answer was written
        self.assertEqual(case.broken(), ["I4"])


class I5StatusAgainstDelivery(RefereeTest):
    def test_done_but_nothing_was_delivered(self):
        case = self.healthy()
        case.truth["dms"] = [dm for dm in case.truth["dms"] if dm["comment_id"] != "c2"]
        self.assertEqual(case.broken(), ["I5"])

    def test_delivered_but_the_row_says_refused(self):
        case = self.healthy()
        case.row("c2", "refused", "user_unavailable")
        self.assertEqual(case.broken(), ["I5", "I9"])   # and nothing justifies the refusal either

    def test_delivered_but_the_row_still_waits(self):
        case = self.healthy()
        case.row("c2", "pending")
        self.assertIn("I5", case.broken())

    def test_delivered_without_any_row(self):
        case = self.healthy()
        case.db.execute("DELETE FROM comments WHERE comment_id='c2'")
        self.assertEqual(case.broken(), ["I5", "I8"])

    def test_uncertain_may_be_delivered_or_not(self):
        for delivered in (True, False):
            case = self.healthy()
            case.waiting("c4", "u4", "uncertain", "http_5xx", at=300)
            case.event("arm", at=290, n=1, rc=0)
            case.dm("c4", 300, status=500, response=error(2), delivered=delivered)
            case.telegram("ℹ uncertain: 1 DM(s) may or may not have been delivered: they are never sent again (1 in all)", 310)
            self.assert_clean(case)


class I6PublicReply(RefereeTest):
    def test_public_reply_without_a_dm(self):
        case = self.healthy()
        case.public("c3", 300)
        self.assertEqual(case.broken(), ["I6"])

    def test_public_reply_after_a_dm_that_was_not_confirmed(self):
        case = self.healthy()
        case.waiting("c4", "u4", "uncertain", "ok_without_id", at=300)
        case.event("arm", at=290, n=1, rc=0)
        case.dm("c4", 300, response={"recipient_id": "x"}, delivered=True)   # delivered, but Meta gave no id
        case.telegram("ℹ uncertain: 1 DM(s) may or may not have been delivered: they are never sent again (1 in all)", 310)
        self.assert_clean(case)
        case.public("c4", 1300)
        self.assertEqual(case.broken(), ["I6"])

    def test_public_reply_before_the_dm(self):
        case = self.healthy()
        case.comment("c4", "u4")
        case.listed("c4")
        case.event("arm", at=290, n=1, rc=0)
        case.public("c4", 297)
        case.dm("c4", 300)
        case.row("c4", "done", at=300)
        case.attempt("c4")
        self.assertEqual(case.broken(), ["I6"])

    def test_two_public_posts_under_one_comment(self):
        case = self.healthy()
        case.public("c2", 300, status=400, delivered=False)
        self.assertEqual(case.broken(), ["I6"])

    def test_public_reply_although_the_config_switched_them_off(self):
        case = self.healthy()
        case.scenario["config"]["public_reply"] = False
        self.assertEqual(case.broken(), ["I6"])


class I7Gates(RefereeTest):
    def test_post_while_sending_is_off(self):
        case = self.healthy()
        case.scenario["config"]["sending"] = False
        self.assertEqual(case.broken(), ["I7"])
        self.assertEqual(len(case.check()), 4)    # two DMs and two public replies

    def test_more_dms_than_were_armed(self):
        case = self.healthy()
        case.events = [event for event in case.events if event["op"] != "arm" or event["seq"] < 3]
        self.assertEqual(case.broken(), ["I7"])   # one armed, two sent
        self.assertIn("SET budget allows 0", case.check()[0].evidence)

    def test_an_arm_that_failed_arms_nothing(self):
        case = self.healthy()
        next(event for event in case.events if event["op"] == "arm")["rc"] = 1
        self.assertEqual(case.broken(), ["I7"])

    def test_arm_all_has_no_limit(self):
        case = self.healthy()
        case.events = [event for event in case.events if event["op"] != "arm"]
        case.events.insert(0, {"op": "arm", "seq": 0, "t": T0, "n": "all", "rc": 0})
        self.assert_clean(case)

    def stage(self, op, extra_dms, **fields):
        """c1, c2 served; then `op`; then that many further people get a DM without a new arm."""
        case = self.healthy()
        for event in (e for e in case.events if e["op"] == "arm"):
            event["n"] = 10
        case.event(op, at=250, **fields)
        for number in range(extra_dms):
            case.comment(f"x{number}", f"ux{number}")
            case.listed(f"x{number}")
            case.dm(f"x{number}", 300 + 100 * number)
            case.public(f"x{number}", 303 + 100 * number)
            case.row(f"x{number}", "done")
            case.attempt(f"x{number}")
        return case

    def test_disarm_stops_the_sending(self):
        self.assertEqual(self.stage("disarm", 2, seq_after=7).broken(), ["I7"])

    def test_one_claim_may_already_be_under_way_at_disarm(self):
        case = self.stage("disarm", 1, seq_after=7)
        # Explicitly move the final claim into the pre-disarm transaction boundary.
        event = next(e for e in case.events if e["op"] == "disarm")
        event["boundary_id"] += 1
        for armed in (e for e in case.events if e["op"] == "arm"):
            armed["n"] = 10
        self.assert_clean(case)

    def test_a_changed_config_voids_the_budget(self):
        edited = {**copy.deepcopy(CONFIG), "poll_interval_sec": 60}
        self.assert_clean(self.stage("config", 0, config=edited, relabel={}))
        self.assertEqual(self.stage("config", 1, config=edited, relabel={}).broken(), ["I7"])

    def test_a_spam_answer_voids_the_budget_and_holds_for_a_day(self):
        case = self.stage("mode", 0, mode="ok")
        case.scenario["faults"]["spam"] = {"klass": "spam"}
        case.waiting("c4", "u4")
        case.dm("c4", 300, status=400, response=error(368), fault_id="spam")
        self.assert_clean(case)
        case.dm("c4", 300 + DAY + 1)              # the day is over, but nobody armed again
        case.public("c4", 303 + DAY + 1)
        case.row("c4", "done")
        case.attempt("c4")
        self.assertEqual(case.broken(), ["I7"])
        case.events.append({"op": "arm", "seq": case.truth["seq"] - 2, "t": T0, "n": 1, "rc": 0})
        self.assert_clean(case)
        case.last("dm")["t"] = case.last("public")["t"] = T0 + 300 + DAY - 60
        self.assertEqual(case.broken(), ["I7"])   # armed, but inside the 24 h hold

    def blocked(self, mode="blocked"):
        """c1 served, then the mode: one sweep is refused and the bot probes."""
        case = self.case()
        case.served("c1", "u1", at=100)
        case.waiting("c4", "u4")
        case.event("arm", at=150, n=5, rc=0)
        case.mode(mode, 200)
        case.request("media", 210, target=MEDIA, status=400, response=error(200))
        case.request("me", 211, status=400, response=error(200))
        return case

    def test_quiet_in_a_blocked_mode_is_clean(self):
        case = self.blocked()
        case.request("me", 511, status=400, response=error(200))
        case.request("me", 1111, status=400, response=error(200))
        case.mode("ok", 2000)
        case.request("me", 2311)
        case.dm("c4", 2400)
        case.public("c4", 2403)
        case.row("c4", "done")
        case.attempt("c4")
        self.assert_clean(case)

    def test_post_in_a_blocked_mode_after_the_first_refusal(self):
        for mode in invariants.ALL_REFUSING:
            case = self.blocked(mode)
            case.dm("c4", 300, status=400, response=error(200), delivered=False)
            self.assertEqual(case.broken(), ["I7"], mode)
            self.assertIn("after the refusal of seq", case.check()[0].evidence)

    def test_sweeps_go_on_in_a_blocked_mode(self):
        case = self.blocked()
        case.request("comments", 240, target=MEDIA, status=400, response=error(200))
        self.assertEqual(case.broken(), ["I7"])
        self.assertIn("sweep requests", case.check()[0].evidence)

    def test_an_answer_that_is_no_refusal_does_not_start_the_silence(self):
        # soak seed 295: an injected code 1 took the place of the mode's answer to the first read. For the bot
        # that is a transient failure of one reel (SPEC 6.1, 8.3): it reads the next reel and is refused there.
        case = self.case()
        case.served("c1", "u1", at=100)
        case.mode("blocked", 200)
        case.request("media", 210, target=MEDIA, status=400, response=error(1), fault_id="fault46")
        refusal = case.request("comments", 212, target=OTHER_MEDIA, status=400, response=error(200))
        case.request("me", 213, status=400, response=error(200))
        self.assert_clean(case)
        case.request("comments", 240, target=MEDIA, status=400, response=error(200))
        self.assertEqual(case.broken(), ["I7"])                     # reading on after the refusal is still caught
        self.assertIn(f"after the refusal of seq {refusal['seq']}", case.check()[0].evidence)

    def test_a_restarted_process_may_find_out_once_more(self):
        case = self.blocked()
        case.event("start", at=230)
        case.request("media", 240, target=MEDIA, status=400, response=error(200))
        self.assert_clean(case)

    def test_probes_closer_than_the_schedule(self):
        case = self.blocked()
        case.request("me", 271, status=400, response=error(200))    # one minute after the first probe
        self.assertEqual(case.broken(), ["I7"])
        self.assertIn("probe", case.check()[0].evidence)

    def messaging_blocked(self, *later):
        """Reads work, POST /messages is refused; `later` are the times of the next attempts."""
        case = self.case()
        case.waiting("c4", "u4")
        case.event("arm", at=0, n=9, rc=0)
        case.mode("messaging_blocked", 50)
        for at in (100, *later):
            case.dm("c4", at, status=400, response=error(100, 2534029), mode="messaging_blocked")
        return case

    def test_refused_posts_back_off_30_then_60_minutes(self):
        self.assert_clean(self.messaging_blocked(100 + 1800, 100 + 1800 + 3600))

    def test_refused_post_repeated_every_5_minutes(self):
        self.assertEqual(self.messaging_blocked(400).broken(), ["I7"])

    def test_refused_post_repeated_without_growing_the_wait(self):
        case = self.messaging_blocked(100 + 1800, 100 + 3600)
        self.assertEqual(case.broken(), ["I7"])
        self.assertIn("before the hold after seq", case.check()[0].evidence)

    def test_hold_after_an_uncertain_dm(self):
        for gap, clean in ((899, False), (901, True)):
            case = self.healthy()
            case.waiting("c4", "u4", "uncertain", "http_5xx", at=300)
            case.event("arm", at=290, n=2, rc=0)
            case.dm("c4", 300, status=500, response=error(2), delivered=False)
            case.telegram("ℹ uncertain: 1 DM(s) may or may not have been delivered: they are never sent again (1 in all)", 310)
            case.served("c5", "u5", at=300 + gap)
            self.assertEqual(case.broken(), [] if clean else ["I7"], gap)

    def test_five_dms_in_a_row_without_ok_hold_for_an_hour(self):
        for gap, clean in ((3500, False), (3700, True)):
            case = self.case()
            case.event("arm", n=9, rc=0)
            for number in range(5):
                case.waiting(f"r{number}", f"ur{number}", "refused", "user_unavailable")
                case.dm(f"r{number}", 100 * number, status=400, response=error(551))
            case.served("c5", "u5", at=400 + gap)
            self.assertEqual(case.broken(), [] if clean else ["I7"], gap)


class I8Completeness(RefereeTest):
    def test_listed_comment_without_a_row(self):
        case = self.healthy()
        case.comment("c4", "u4", text="спасибо", should_match=False)
        case.listed("c4")
        self.assertEqual(case.broken(), ["I8"])

    def test_comment_never_reached_by_paging_once_settled(self):
        case = self.healthy()
        case.comment("c4", "u4")                  # on a page the bot never asked for
        self.assert_clean(case)                   # ... which proves nothing while the run is still going
        case.event("settle", at=300)
        self.assertEqual(case.broken(), ["I8"])

    def test_comments_meta_never_lists_or_deleted_before_listing_need_no_row(self):
        case = self.healthy()
        case.comment("c4", "u4", listed=False)
        case.comment("c5", "u5", deleted=True)
        case.event("settle", at=300)
        self.assert_clean(case)

    def test_a_request_stored_as_not_a_request(self):
        case = self.healthy()
        case.waiting("c4", "u4", "seen")
        case.event("settle", at=300)
        self.assertEqual(case.broken(), ["I8"])

    def test_not_a_request_stored_as_waiting(self):
        case = self.healthy()
        case.row("c3", "pending")
        self.assertEqual(case.broken(), ["I8"])

    def test_malformed_and_authorless_requests_stay_seen(self):
        case = self.healthy()
        case.waiting("c4", "u4", "seen", "malformed", raw={"timestamp": None})
        case.waiting("c5", None, "seen", "no_author")
        self.assert_clean(case)
        case.row("c4", "pending")
        self.assertEqual(case.broken(), ["I8"])

    def test_a_comment_the_owner_answered_is_already_only_if_it_is_a_request(self):
        # 8.3 rule 1: the owner's reply under small talk says nothing; that person may still ask
        case = self.healthy()
        case.owner_reply("c3")
        case.row("c3", "already", "owner_replied")
        self.assertEqual(case.broken(), ["I8"])
        case.row("c3", "seen")
        self.assert_clean(case)

    def test_row_for_a_comment_the_fake_never_held(self):
        case = self.healthy()
        case.row("ghost", "seen")
        self.assertEqual(case.broken(), ["I8"])

    def test_deleted_comment_whose_row_a_kill_took_is_excused(self):
        case = self.healthy()
        case.comment("c4", "u4", text="ок", should_match=False, deleted=True)
        held = case.listed("c4")
        self.assertEqual(case.broken(), ["I8"])
        case.event("kill", at=5, held=held["seq"])
        self.assert_clean(case)

    def test_request_under_an_earlier_config_must_not_stay_pending(self):
        stricter = copy.deepcopy(CONFIG)
        stricter["reels"][0]["also_match"] = []
        for status, clean in (("seen", True), ("pending", False)):
            case = self.healthy()
            case.waiting("c4", "u4", status, text="арееа")
            case.event("config", at=250, config=stricter, relabel={"c4": False})
            self.assertEqual(case.broken(), [] if clean else ["I8"], status)


class I9Justification(RefereeTest):
    def test_refused_although_meta_never_refused_the_person(self):
        case = self.healthy()                     # v1 issue K1: an account-wide error wrote everybody off
        case.waiting("c4", "u4", "refused", "user_unavailable")
        self.assertEqual(case.broken(), ["I9"])

    def test_refused_after_an_account_wide_answer(self):
        case = self.healthy()
        case.waiting("c4", "u4", "refused", "invalid")
        case.event("arm", at=290, n=1, rc=0)
        case.dm("c4", 300, status=400, response=error(190))
        self.assertEqual(case.broken(), ["I9"])
        self.assertIn("['account']", case.check()[0].evidence)

    def test_refused_after_a_per_item_answer_is_clean(self):
        case = self.healthy()
        case.waiting("c4", "u4", "refused", "user_unavailable")
        case.event("arm", at=290, n=1, rc=0)
        case.dm("c4", 300, status=400, response=error(551))
        self.assert_clean(case)

    def suspect(self, other_served):
        case = self.healthy()
        case.waiting("c4", "u4", "refused", "suspect_item:10/-")
        case.event("arm", at=290, n=3, rc=0)
        case.dm("c4", 300, status=400, response=error(190))
        if other_served:
            case.served("c5", "u5", at=2200)
        case.event("arm", at=2250, n=1)
        case.dm("c4", 2300, status=400, response=error(190))
        return case

    def test_suspect_item_is_never_justified_by_other_peoples_success(self):
        self.assertEqual(self.suspect(other_served=True).broken(), ["I9"])
        self.assertEqual(self.suspect(other_served=False).broken(), ["I9"])

    def test_uncertain_without_any_doubt(self):
        case = self.healthy()
        case.waiting("c4", "u4", "uncertain", "interrupted", at=300)
        case.telegram("ℹ uncertain: 1 DM(s) may or may not have been delivered: they are never sent again (1 in all)", 310)
        self.assertEqual(case.broken(), ["I9"])
        case.event("arm", at=290, n=1, rc=0)
        held = case.dm("c4", 300, held="after")           # Meta's answer was ok and the fake wrote it out ...
        self.assertEqual(case.broken(), ["I9"])
        case.event("kill", at=301, held=held["seq"])      # ... to a bot the harness had just killed
        self.assert_clean(case)

    def expired(self, age, expired_at, may_expire=True):
        case = self.healthy()
        case.waiting("c4", "u4", "expired", "window", at=expired_at, age=age)
        case.telegram("ℹ expired: 1 people lost: their comments are past the 7-day window (1 in all)", expired_at + 60)
        case.scenario["may_expire"] = ["c4"] if may_expire else []
        return case

    def test_expired_before_the_window_really_closed(self):
        self.assertEqual(self.expired(age=6 * DAY, expired_at=3600).broken(), ["I9"])

    def test_expired_ten_minutes_before_the_seven_days_is_clean(self):
        self.assert_clean(self.expired(age=7 * DAY - 4000, expired_at=3400))

    def test_expired_although_the_schedule_left_time(self):
        case = self.expired(age=7 * DAY - 4000, expired_at=3400, may_expire=False)
        self.assertEqual(case.broken(), ["I9"])
        self.assertIn("left time", case.check()[0].evidence)

    def test_expired_at_first_sight_needs_no_excuse(self):
        self.assert_clean(self.expired(age=8 * DAY, expired_at=0, may_expire=False))

    def test_dup_user_without_another_handled_comment(self):
        case = self.healthy()
        case.waiting("c4", "u4", "dup_user", "dup:c5")
        case.waiting("c5", "u4", "refused", "user_unavailable")
        self.assertEqual(case.broken(), ["I9"])   # twice: c5 was never refused by Meta either
        self.assertEqual(len(case.check()), 2)

    def test_already_without_a_reply_or_an_already_replied_answer(self):
        case = self.healthy()
        case.waiting("c4", "u4", "already", "owner_replied")
        self.assertEqual(case.broken(), ["I9"])
        case.owner_reply("c4")
        self.assert_clean(case)

    def test_already_replied_needs_metas_answer(self):
        for response, clean in ((error(10900), True), (error(100, 2534025), True),
                                (error(-1, message="The comment already has a reply."), True), (error(551), False)):
            case = self.healthy()
            case.waiting("c4", "u4", "already", "already_replied")
            case.event("arm", at=290, n=1, rc=0)
            case.dm("c4", 300, status=400, response=response)
            self.assertEqual(case.broken(), [] if clean else ["I9"], response)

    def test_public_reply_recorded_but_never_posted(self):
        case = self.healthy()
        case.truth["comments"][1]["replies"] = []
        case.truth["replies"].pop()
        self.assertEqual(case.broken(), ["I9"])
        case.row("c2", "done", "public_uncertain")
        self.assert_clean(case)


class I10Progress(RefereeTest):
    def test_request_left_waiting_after_everything_was_opened(self):
        case = self.healthy()
        case.waiting("c4", "u4", said=False)
        self.assertEqual(case.broken(), ["I10"])
        case.event("settle", at=300)
        self.assertEqual(case.broken(), ["I10"])
        self.assertIn("gates were opened", case.check()[0].evidence)

    def test_public_reply_still_owed_after_settle(self):
        case = self.healthy()
        case.truth["comments"][1]["replies"] = []
        case.truth["requests"].remove(case.last("public"))
        case.truth["replies"].pop()
        case.row("c2", "sent")
        case.event("settle", at=300)
        self.assertEqual(case.broken(), ["I10"])

    def closed_gate(self, said):
        case = self.healthy()
        case.waiting("c4", "u4", said=False)
        case.scenario["expect"] = {"waiting": "no_budget"}
        if said:
            case.problem("waiting", "1 waiting: sending_off … no_budget, oldest 40 min")
        return case

    def test_left_waiting_at_a_closed_gate_that_the_bot_reports(self):
        self.assert_clean(self.closed_gate(said=True))

    def test_left_waiting_and_the_bot_does_not_say_why(self):
        self.assertEqual(self.closed_gate(said=False).broken(), ["I10"])

    def test_the_report_may_carry_the_reason_instead(self):
        case = self.closed_gate(said=False)
        case.event("report", rc=0, text=json.dumps({"unaccounted": 0, "gates": {"waiting_reason": "no_budget"}}))
        self.assert_clean(case)


class I11Payloads(RefereeTest):
    def test_dm_without_the_link(self):
        case = self.healthy()
        case.last("dm")["body"]["message"]["text"] = f"{GREETING} Держи!"
        self.assertEqual(case.broken(), ["I11"])

    def test_dm_with_the_link_of_another_reel(self):
        case = self.healthy()
        case.last("dm")["body"]["message"]["text"] = f"{GREETING} {OTHER_LINK}"
        self.assertEqual(case.broken(), ["I11"])

    def test_placeholder_sent_as_it_is(self):
        case = self.healthy()
        case.last("dm")["body"]["message"]["text"] = "{greeting} " + LINK
        self.assertEqual(case.broken(), ["I11"])

    def test_greeting_follows_the_age_of_the_comment(self):
        fresh_text, late_text = (f"{greeting} Держи ссылку 👇\n\n{LINK}" for greeting in (GREETING, LATE_GREETING))
        for age, text, clean in ((600, fresh_text, True), (600, late_text, False), (2 * DAY, late_text, True),
                                 (2 * DAY, fresh_text, False), (3600 - 100, late_text, True),
                                 (3600 + 100, fresh_text, True), (600, LINK, False)):
            case = self.case()
            case.comment("c1", "u1", age=age)
            case.listed("c1")
            case.event("arm", n=1, rc=0)
            case.dm("c1", 100, text=text)
            case.public("c1", 103)
            case.row("c1", "done")
            case.attempt("c1")
            self.assertEqual(case.broken(), [] if clean else ["I11"], (age, text))

    def test_dm_that_is_not_a_text_message(self):
        case = self.healthy()
        case.last("dm")["body"]["message"] = {"attachment": {"type": "template", "payload": {"text": LINK}}}
        self.assertEqual(case.broken(), ["I11"])

    def test_public_reply_that_is_not_one_of_the_phrases(self):
        case = self.healthy()
        case.last("public")["params"]["message"] = "Смотри директ"
        self.assertEqual(case.broken(), ["I11"])

    def test_request_the_strict_fake_had_to_reject(self):
        case = self.healthy()
        case.request("me", 400, status=400, response=error(2500), strict="unknown parameter(s) ['foo']")
        self.assertEqual(case.broken(), ["I11"])


class I12Alerts(RefereeTest):
    def test_expected_alert_that_never_came(self):
        case = self.healthy()
        case.event("mode", at=400, alert={"contains": ["hold:account"], "within": 1200})
        self.assertEqual(case.broken(), ["I12"])
        case.telegram("⚠ hold:send: uncertain", 500)
        self.assertEqual(case.broken(), ["I12"])
        case.telegram("⚠ hold:account: blocked since …", 400 + 1200 + invariants.REAL_SLACK + 1)
        self.assertEqual(case.broken(), ["I12"])          # too late
        case.telegram("⚠ hold:account: blocked since …", 400 + 1199)
        self.assert_clean(case)

    def test_alert_deadline_allows_one_real_second_of_machine_latency(self):
        # at 6000x a 20-minute deadline is 0.2 s of wall time; a slow disk must not look like a silent bot
        case = self.healthy()
        case.scenario["scale"] = 6000
        case.event("mode", at=400, alert={"contains": ["hold:account"], "within": 1200})
        case.telegram("⚠ hold:account: blocked since …", 400 + 1200 + 6000 * invariants.REAL_SLACK + 1)
        self.assertEqual(case.broken(), ["I12"])          # more than a real second late
        case.telegram("⚠ hold:account: blocked since …", 400 + 1200 + 6000 * 0.5)
        self.assert_clean(case)

    def test_alert_must_be_repeated_while_the_state_lasts(self):
        case = self.healthy()
        case.event("mode", at=400, alert={"contains": ["waiting", "no_budget"], "within": 20000, "count": 2})
        case.telegram("⚠ waiting: 3 people, no_budget", 2400)
        self.assertEqual(case.broken(), ["I12"])
        case.telegram("⚠ waiting: 3 people, no_budget", 9600)
        self.assert_clean(case)

    def test_deadline_waits_for_the_bot_and_the_line_to_be_back(self):
        for outage in ("bot down", "connections refused"):
            case = self.healthy()
            case.event("mode", at=400, alert={"contains": ["hold:account"], "within": 600})
            if outage == "bot down":
                case.event("stop", at=450)
                case.event("start", at=5000)
            else:
                case.truth["events"] += [{"after_seq": 7, "t": T0 + 450, "op": "listener", "accept": False},
                                         {"after_seq": 7, "t": T0 + 5000, "op": "listener", "accept": True}]
            case.telegram("⚠ hold:account: blocked", 5500)
            self.assert_clean(case)
            case.truth["telegram"][-1]["t"] = T0 + 5700
            self.assertEqual(case.broken(), ["I12"], outage)

    def lost(self, status, said=None, at=1000):
        case = self.healthy()
        age = 7 * DAY - 600 + 900 if status == "expired" else 600
        case.waiting("c4", "u4", status, "window" if status == "expired" else "http_5xx", at=at, age=age)
        if status == "uncertain":
            case.event("arm", at=at - 10, n=1, rc=0)
            case.dm("c4", at, status=500, response=error(2))
        case.scenario["may_expire"] = ["c4"]
        if said:
            case.telegram(said, at + 60)
        return case

    def test_expired_person_nobody_was_told_about(self):
        self.assertEqual(self.lost("expired").broken(), ["I12"])
        self.assert_clean(self.lost("expired", "ℹ expired: 1 people lost: their comments are past the 7-day window (1 in all)"))

    def test_uncertain_person_nobody_was_told_about(self):
        self.assertEqual(self.lost("uncertain").broken(), ["I12"])
        self.assertEqual(self.lost("uncertain", "ℹ expired: 1 people lost: their comments are past the 7-day window (1 in all)").broken(), ["I12"])
        self.assert_clean(self.lost("uncertain", "ℹ uncertain: 1 DM(s) may or may not have been delivered: they are never sent again (1 in all)"))

    def test_announcement_is_recognised_by_the_outbox_text_too(self):
        case = self.lost("expired", "ℹ expired: 1 человек потерян (1 in all)")
        self.assertEqual(case.broken(), ["I12"])
        case.db.execute("INSERT INTO outbox (key, text) VALUES ('expired', 'ℹ expired: 1 человек потерян (1 in all)')")
        self.assert_clean(case)

    def test_announcement_from_before_it_happened_does_not_count(self):
        case = self.lost("expired", at=5000)
        case.telegram("ℹ expired: 1 people lost: their comments are past the 7-day window (1 in all)", 1000)
        self.assertEqual(case.broken(), ["I12"])

    def test_nothing_to_announce_to_when_telegram_was_never_configured(self):
        case = self.lost("expired")
        case.scenario["env"] = {"IG_ACCESS_TOKEN": TOKEN}
        self.assert_clean(case)

    def test_unaccounted_claim_left_open(self):
        case = self.healthy()
        case.event("arm", n=1)
        case.attempt("c3", outcome=None, finished=False)
        self.assertEqual(case.broken(), ["I12"])

    def test_unaccounted_done_without_an_ok_dm_in_the_journal(self):
        case = self.healthy()
        case.db.execute("DELETE FROM attempts WHERE comment_id='c2'")
        self.assertEqual(case.broken(), ["I12", "I7"])

    def test_unaccounted_row_left_sending(self):
        case = self.healthy()
        case.waiting("c4", "u4", "sending")
        self.assertEqual(case.broken(), ["I10", "I12"])

    def test_unaccounted_waiting_under_a_media_the_config_does_not_name(self):
        case = self.healthy()
        case.scenario["config"]["reels"] = case.scenario["config"]["reels"][1:]
        case.truth["dms"], case.truth["replies"] = [], []
        case.truth["requests"] = [r for r in case.truth["requests"] if r["kind"] == "comments"]
        case.events = []
        case.db.execute("DELETE FROM attempts")
        for comment_id in ("c1", "c2"):
            case.row(comment_id, "pending")
            case._find(comment_id)["replies"] = []
        self.assertEqual(case.broken(), ["I12"])

    def test_the_bots_own_problem_list_and_report_must_agree(self):
        case = self.healthy()
        case.problem("unaccounted", "1 comment unaccounted")
        self.assertEqual(case.broken(), ["I12"])
        for text, rc, clean in (('{"unaccounted": 0}', 0, True),
                                ('{"x": {"unaccounted": {"total": 0, "open": []}}}', 0, False),
                                ('{"unaccounted": 2}', 0, False), ('{"x": [{"unaccounted": ["c1"]}]}', 0, False),
                                ('{"unaccounted": 0}', 5, False), ("Traceback …", 0, False)):
            case = self.healthy()
            case.event("report", rc=rc, text=text)
            self.assertEqual(case.broken(), [] if clean else ["I12"], text)

    def test_token_in_anything_the_bot_said(self):
        for where in ("telegram", "output", "report", "outbox", "problems"):
            case = self.healthy()
            if where == "telegram":
                case.telegram(f"⚠ token: GET /me?access_token={TOKEN} failed", 500)
            elif where == "outbox":
                case.db.execute("INSERT INTO outbox (key, text) VALUES ('token', ?)", (f"bad {TOKEN}",))
            elif where == "problems":
                case.problem("token", "tg 123:tg-secret refused")
            else:
                text = json.dumps({"unaccounted": 0, "log": f"token={TOKEN}"})
                case.event(where, rc=0, text=text)
            self.assertEqual(case.broken(), ["I12"], where)
            self.assertEqual(case.check()[0].subject, "secret")


# ---------- sim/scenarios.py ----------

class Scenarios(unittest.TestCase):
    """Every scenario must be something the fake, the bot's config loader and the soak can take, and its
    hand-written labels must agree with SPEC section 5 as match.py implements it."""

    @classmethod
    def setUpClass(cls):
        cls.random = [scenarios.build(seed) for seed in range(40)]
        cls.all = [scenarios.named(name) for name in scenarios.NAMED] + cls.random

    def test_a_seed_always_gives_the_same_scenario_and_seeds_differ(self):
        self.assertEqual(scenarios.build(7), scenarios.build(7))
        self.assertNotEqual(scenarios.build(7), scenarios.build(8))
        self.assertEqual(json.loads(json.dumps(scenarios.build(7))), scenarios.build(7))    # plain JSON

    def test_the_fake_accepts_every_scenario_and_every_step_meant_for_it(self):
        for scenario in self.all:
            fake = FakeMeta(scenario["fake"])     # ValueError for anything the fake does not know
            for step in scenario["steps"]:
                with self.subTest(scenario=scenario["name"], step=step):
                    if step["op"] == "faults":
                        fake.control("/faults", {"add": step["add"]})
                    elif step["op"] == "mode":
                        fake.control("/mode", {"mode": step["mode"]})
                    elif step["op"] == "comment":
                        fake.control("/comments", {key: step[key] for key in ("add", "delete", "hide", "owner_reply")
                                                   if key in step})
                    elif step["op"] == "kill":
                        fake.control("/hold", {"add": {key: step[key] for key in ("kind", "phase", "target")
                                                       if key in step}})

    def test_every_config_is_one_the_bot_loads(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "config.json")
        for scenario in self.all:
            for cfg in [scenario["config"]] + [step["config"] for step in scenario["steps"] if step["op"] == "config"]:
                with open(path, "w", encoding="utf-8") as file:
                    json.dump(cfg, file)
                config.load(path)                 # ConfigError lists what is wrong

    def test_labels_agree_with_the_matcher(self):
        for scenario in self.all:
            comments = [(media["id"], comment) for media in scenario["fake"]["media"] for comment in media["comments"]]
            comments += [(step["add"]["media_id"], step["add"]) for step in scenario["steps"]
                         if step["op"] == "comment" and "add" in step]
            labels = {comment["id"]: comment["should_match"] for _, comment in comments}
            for step in [{"op": "config", "config": scenario["config"], "relabel": {}}] + scenario["steps"]:
                if step["op"] != "config":
                    continue
                labels.update(step["relabel"])
                for media_id, comment in comments:
                    one = next(SimpleNamespace(**one) for one in step["config"]["reels"] if one["media_id"] == media_id)
                    says = match.Matcher(one).check(comment["text"])[0] == "match"
                    self.assertEqual(says, labels[comment["id"]], (scenario["name"], comment["text"]))

    def test_every_replaced_answer_to_a_post_is_in_the_referees_catalogue(self):
        for scenario in self.all:
            for step in scenario["steps"]:
                for fault in step["add"] if step["op"] == "faults" else []:
                    answers = set(fault) & {"http", "code", "subcode", "message", "body", "omit_id"}
                    if fault["kind"] in ("dm", "public") and answers:
                        klass = scenario["faults"][fault["id"]]["klass"]
                        self.assertIn(klass, ("account", "spam", "item", "ambiguous"), fault)
                        # a definite refusal that delivers would make Meta a liar and G4 impossible
                        self.assertFalse(fault["deliver"] and klass != "ambiguous", fault)

    def test_every_step_is_one_the_soak_runs(self):
        for scenario in self.all:
            for step in scenario["steps"]:
                self.assertTrue(callable(getattr(soak.Run, step["op"])), step)

    def test_random_scenarios_cover_the_catalogue(self):
        ops = collections.Counter(step["op"] for scenario in self.random for step in scenario["steps"])
        for op in ("kill", "term", "second", "config", "disarm", "mode", "faults", "comment"):
            self.assertGreater(ops[op], 0, op)
        text = json.dumps(self.random, ensure_ascii=False)
        for needle in ('"drop": "before"', '"drop": "after"', "omit_id", "refuse_real_sec", "delay_real_sec",
                       "messaging_blocked", "dev_mode", "throttled", "token_dead", '"private_replied": true',
                       '"vanish": true', '"kind": "public"', '"kind": "telegram"', '"phase": "after"',
                       '"kind": "comments", "phase"', '"timestamp": null', '"from": null', '"listed": false',
                       '"delete"', '"hide"', '"owner_reply"', '"replies"', '"deliver": true', '"code": 368'):
            self.assertIn(needle, text)


# ---------- sim/soak.py, against a stand-in for the bot ----------

STANDIN = r'''
"""Stand-in for bot.py in the tests of sim/soak.py: the CLI contract (SPEC sections 3 and 10) around the
modules store, meta, config, match and clock. NOT the bot: no engine, a full sweep in every cycle, no token
upkeep, alerts reduced to what the soak listens for."""
import fcntl, json, os, signal, sys, time, urllib.request
sys.path.insert(0, %(code)r)
import clock, config, match, meta, store

DEFECT = %(defect)r
STATE = os.environ["STATE_DIR"]


def env():
    try:
        with open(os.path.join(STATE, ".env"), encoding="utf-8") as file:
            return dict(line.strip().split("=", 1) for line in file if "=" in line)
    except OSError:
        return {}


def telegram(text):
    request = urllib.request.Request(
        f"{config.tg_api_base()}/bot{env().get('TG_BOT_TOKEN')}/sendMessage",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"chat_id": env().get("TG_CHAT_ID"), "text": text}).encode())
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10).close()
    except OSError:
        pass


def sweep(conn, client, reel, me):
    if not client.media(reel.media_id).ok:
        return False
    matcher, next_url = match.Matcher(reel), None
    while True:
        page = client.comments_page(reel.media_id, next_url)
        if not page.ok:
            return False
        with store.tx(conn):
            for item in page.data["data"]:
                who = item.get("from") or {}
                replied = any(store.is_own(me, reply.get("from", {}).get("id"), reply.get("from", {}).get("username"))
                              for reply in item.get("replies", {}).get("data", []))
                row = store.comment(conn, item["id"])
                if row is None:
                    status, reason = match.decide(matcher, item.get("text"), bool(who.get("id")),
                                                  store.is_own(me, who.get("id"), who.get("username")))
                    if replied and status == "pending":    # 8.3 rule 1: the reply under a REQUEST
                        status, reason = "already", "owner_replied"
                    elif status == "pending" and store.window_closed(item["timestamp"], clock.now()):
                        status, reason = "expired", "window"
                    store.add_comment(conn, item["id"], reel.media_id, who.get("id"), who.get("username"),
                                      item.get("text"), item["timestamp"], status, reason)
                elif replied and row["status"] == "pending":
                    store.transition(conn, item["id"], "pending", "already", "owner_replied")
                elif replied and row["status"] == "sent":
                    store.transition(conn, item["id"], "sent", "done")
        next_url = page.data.get("paging", {}).get("next")
        if not next_url:
            return True


def public(conn, cfg, client, comment_id, me):
    claim = store.claim_public(conn, comment_id, cfg, me)
    if claim.kind == "claimed":
        store.record_public(conn, claim.attempt_id, client.public_reply(comment_id, claim.phrase))


def announce(conn):
    account, send = store.get_account_hold(conn), store.get_send_hold(conn)
    now = {key: hold["reason"] for key, hold in (("hold:account", account), ("hold:send", send)) if hold}
    known = {row[0] for row in conn.execute("SELECT key FROM problems")}
    lines = [f"⚠ {key}: {text}" for key, text in now.items() if key not in known]
    for key in now.keys() - known:
        store.problem_announce(conn, key, now[key])
    for key in known - now.keys():
        store.problem_clear(conn, key)
    loss_text = {"expired": "people lost: their comments are past the 7-day window",
                 "uncertain": "DM(s) may or may not have been delivered: they are never sent again"}
    for status in ("expired", "uncertain"):
        count = conn.execute("SELECT COUNT(*) FROM comments WHERE status=?", (status,)).fetchone()[0]
        previous = int(store.kv_get(conn, "said_" + status, 0))
        if count > previous:
            lines.append(f"ℹ {status}: {count - previous} {loss_text[status]} ({count} in all)")
        store.kv_set(conn, "said_" + status, count)
    if lines:
        telegram("\n".join(lines))


def cycle(conn, cfg, client):
    store.recover_stale(conn)
    hold = store.get_account_hold(conn)
    if not hold or hold["next_probe_at"] <= clock.iso():
        answer = client.me()
        if answer.ok:
            store.kv_set_many(conn, {"me_user_id": answer.data["user_id"], "me_id": answer.data["id"],
                                     "me_username": answer.data["username"], "account_hold": None})
        else:
            store.set_account_hold(conn, "blocked", answer.message)
    me = {key: store.kv_get(conn, "me_" + key) for key in ("user_id", "id", "username")}
    swept = set()
    for reel in () if store.get_account_hold(conn) else cfg.reels:
        if reel.active:
            if not sweep(conn, client, reel, me):
                break    # SPEC 8.3: a read that failed ends collect; reading the next reel breaks I7 in a blocked mode
            swept.add(reel.media_id)
    store.expire(conn)
    owed = conn.execute("SELECT comment_id, media_id FROM comments WHERE status='sent'").fetchall()
    for comment_id, media_id in owed:
        if media_id in swept:
            public(conn, cfg, client, comment_id, me)
    for comment_id, media_id in conn.execute(
            "SELECT comment_id, media_id FROM comments WHERE status='pending' ORDER BY deferrals, ts").fetchall():
        claim = store.claim_dm(conn, comment_id, cfg, me) if media_id in swept else store.Claim("skip")
        if claim.kind == "skip":
            continue
        if claim.kind != "claimed":
            break
        message = config.render(cfg, cfg.reel_by_media[media_id], claim.late)
        answer = client.send_dm(me["user_id"], comment_id, message)
        if DEFECT == "asks twice" and answer.ok:
            client.send_dm(me["user_id"], comment_id, message)
        if store.record_dm(conn, claim.attempt_id, answer) != "sent":
            break
        public(conn, cfg, client, comment_id, me)
    announce(conn)


def run(once):
    lock = open(os.path.join(STATE, "core.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(3)
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    cfg = config.load(os.path.join(STATE, "config.json"))
    conn = store.connect(os.path.join(STATE, "core.db"))
    store.init(conn)
    client = meta.Client(config.ig_api_base(), cfg.api_version, lambda: env().get("IG_ACCESS_TOKEN"))
    count = 0
    while not stopping:
        count += 1
        print(f"INFO cycle_start pid={os.getpid()} cycle={count} monotonic={time.monotonic():.9f}", flush=True)
        cycle(conn, cfg, client)
        if once:
            break
        clock.sleep(cfg.poll_interval_sec, lambda: bool(stopping))


command, arguments = sys.argv[1], sys.argv[2:]
if clock.check_safe_env():
    sys.exit(2)
if command in ("run", "once"):
    run(command == "once")
elif command == "report":
    print(json.dumps({"unaccounted": 0}))
else:
    db = store.connect(os.path.join(STATE, "core.db"))
    store.arm(db, 0 if command == "disarm" else "unlimited" if arguments[0] == "all" else int(arguments[0]))
'''


def every_step():
    """One scenario that uses every kind of step once, small enough for a unit test."""
    plan = scenarios.Plan("every_step")
    for index in range(5):
        plan.load(plan.comment(age=(index + 1) * scenarios.MINUTE))
    plan.load(plan.comment("круто", should_match=False))
    plan.load(plan.comment(scenarios.OWN, "own"))
    plan.step("start")
    plan.until("rows")
    plan.step("second")
    plan.arm(1)
    plan.until("dm", count=1)
    plan.step("kill", kind="dm", phase="after", arm=1)
    plan.step("start")
    plan.step("kill", kind="comments", phase="before")
    plan.step("start")
    plan.step("term")
    plan.step("start")
    victim, = plan.fresh(1)
    fault = plan.fault("ambiguous", "dm", scenarios.AMBIGUOUS[0], target=victim, nth=1)
    plan.arm()
    plan.until("fired", ids=[fault])
    plan.until("status", ids=[victim], among=["uncertain"])
    plan.step("disarm")
    plan.fresh(1)
    plan.step("wait", sec=5 * scenarios.MINUTE)
    plan.step("mode", mode="blocked", alert={"contains": ["hold:account"], "within": 20 * scenarios.MINUTE})
    plan.step("wait", sec=20 * scenarios.MINUTE)
    plan.step("mode", mode="ok")
    plan.until("ok", kind="comments")
    plan.step("config", relabel={},
              config=scenarios.config(greeting="Хей! ✌️", late_greeting="Хей! ✌️ Прости, что долго"))
    plan.step("env", env=dict(scenarios.ENV))
    return plan.scenario()


class SoakAgainstStandIn(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def standin(self, defect=None):
        path = os.path.join(self.dir, "standin_with_defect.py" if defect else "standin.py")
        with open(path, "w", encoding="utf-8") as file:
            file.write(STANDIN % {"code": os.path.dirname(HERE), "defect": defect})
        return path

    def test_every_step_runs_and_a_sound_bot_passes(self):
        run = soak.Run(every_step(), self.standin(), os.path.join(self.dir, "run"))
        violations, counts = run.run()
        self.assertEqual(violations, [])
        ops = [event["op"] for event in run.events]
        for op in ("start", "stop", "second", "arm", "kill", "disarm", "mode", "config", "env", "settle",
                   "report", "output"):
            self.assertIn(op, ops)
        self.assertNotIn("unplaced", ops)
        self.assertNotIn("timeout", ops)
        self.assertEqual(counts["kills"], 2)
        # the kills sat exactly on the requests the fake held
        with open(os.path.join(run.dir, "truth.json"), encoding="utf-8") as file:
            truth = json.load(file)
        held = {request["seq"]: request for request in truth["requests"] if request["held"]}
        kills = [event for event in run.events if event["op"] == "kill"]
        self.assertEqual([(held[kill["held"]]["kind"], held[kill["held"]]["held"]) for kill in kills],
                         [("dm", "after"), ("comments", "before")])
        self.assertEqual([event["rc"] for event in run.events if event["op"] in ("second", "report")], [3, 0])
        self.assertEqual([event["how"] for event in run.events if event["op"] == "stop"],
                         ["kill", "kill", "term", "term", "term"])
        # everything went to the fake, and hours of bot time passed in seconds
        self.assertTrue(all(request["path"].startswith(("/v26.0/", "/bot")) for request in truth["requests"]))
        self.assertGreater(truth["requests"][-1]["t"] - truth["requests"][0]["t"], 3600)

    def test_a_defect_fails_the_scenario_and_the_kept_directory_replays(self):
        plan = scenarios.Plan("two_people")
        plan.load(plan.comment())
        plan.load(plan.comment())
        plan.step("start")
        plan.until("rows")
        with mock.patch.dict(scenarios.NAMED, {"two_people": plan.scenario}):
            kept, out = os.path.join(self.dir, "kept"), io.StringIO()
            arguments = ["--only", "two_people", "--keep-failed", kept, "--bot"]
            self.assertEqual(soak.main(arguments + [self.standin("asks twice")], out=out), 1)
            first, *violations, summary = out.getvalue().splitlines()
            # the repeated POST is answered "already replied" by the fake: nobody got two DMs, yet G4 and G2 broke
            self.assertRegex(first, r"^two_people FAIL \d+\.\ds comments=2 requests=\d+ dms=2 .* violated=I2,I4,I7 kept="
                             + kept)
            self.assertEqual([line.split()[0] for line in violations if "I4" in line], ["I4", "I4"])
            self.assertRegex(next(line for line in violations if line.startswith("    I4")), r"^    I4 comment \d+ \(user \d+\): POST /messages seq \d+ although "
                                             r"seq \d+ was answered ok")
            self.assertEqual(summary, "1 scenario(s): 0 passed, 1 failed: two_people")
            (run,) = os.listdir(kept)
            self.assertLessEqual({"truth.json", "scenario.json", "events.json", "state", "bot.log"},
                                 set(os.listdir(os.path.join(kept, run))))
            replay = subprocess.run([sys.executable, "-B", os.path.join(HERE, "invariants.py"),
                                     os.path.join(kept, run)], capture_output=True, text=True)
            self.assertEqual(replay.returncode, 1)
            self.assertEqual(replay.stdout.splitlines(),
                             [line.strip() for line in violations] + [f"{len(violations)} violation(s)"])

            out = io.StringIO()     # the same scenario with a sound bot: PASS, exit 0, nothing more is kept
            self.assertEqual(soak.main(arguments + [self.standin()], out=out), 0)
            self.assertRegex(out.getvalue(), r"^two_people PASS .*\n1 scenario\(s\): 1 passed, 0 failed\n$")
            self.assertEqual(os.listdir(kept), [run])

    def test_usage_errors_exit_2_and_run_nothing(self):
        for arguments in ([], ["--seeds", "0"], ["--only", "no_such"], ["--seeds", "1", "--scale", "0"],
                          ["--seeds", "1", "--bot", os.path.join(self.dir, "absent.py")]):
            bot = [] if "--bot" in arguments else ["--bot", self.standin()]
            with contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertEqual(soak.main(arguments + bot, out=io.StringIO()), 2, arguments)
            self.assertTrue(said.getvalue().strip(), arguments)

    def test_only_list_names_every_scenario(self):
        out = io.StringIO()
        self.assertEqual(soak.main(["--only", "list"], out=out), 0)
        self.assertEqual([line.split(":")[0] for line in out.getvalue().splitlines()], list(scenarios.NAMED))
        for wanted in ("g1", "g2", "g3", "g4", "g5", "g6", "outage_blocked", "outage_gate", "outage_secret"):
            self.assertIn(wanted, scenarios.NAMED)


if __name__ == "__main__":
    unittest.main()
