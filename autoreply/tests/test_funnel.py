"""SPEC section 14 (the funnel): the first DM per variant, taps, the follow check, steps, follow-ups, rechecks.

The engine runs against a scripted client (no network): every test judges by what the client was asked to send
and by the database.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from datetime import timedelta
from unittest import mock

import clock
import config
import engine
import meta
import store

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = {"me_user_id": "17841400000000001", "me_id": "9990000000000001", "me_username": "your_account"}
with open(os.path.join(CODE, "config.example.json"), encoding="utf-8") as _file:
    EXAMPLE = json.load(_file)
MEDIA = EXAMPLE["reels"][0]["media_id"]
LINK = EXAMPLE["reels"][0]["link"]

FLOW = {
    "opening": {"text": "{greeting} Нажми кнопку, и я пришлю ссылку 👇", "button": "Получить"},
    "gate": {"text": "Подпишись и нажми «Готово»", "button": "Готово", "profile_button": "Открыть профиль",
             "again": "Пока не вижу подписку"},
    "link": {"text": "Держи 👇", "buttons": [{"title": "Открыть", "url": "https://github.com/x"},
                                            {"title": "Telegram", "url": "https://t.me/x"}]},
    "followup": {"text": "Получилось поставить?", "after_minutes": 60},
    "variants": [{"name": "A", "weight": 50, "opening": True, "gate": True},
                 {"name": "B", "weight": 50, "opening": False, "gate": False}],
}


def result(ok=True, data=None, http=200, outcome=None, reason=""):
    data = {"message_id": "m1"} if data is None and ok else (data or {})
    return meta.Result(ok=ok, http=http, data=data, outcome=outcome or ("ok" if ok else "uncertain"), reason=reason,
                       code=None, subcode=None, message="", connect_failed=False)


class Client:
    """Scripted Meta: records every call; `follows` answers the follow checks in order."""

    def __init__(self):
        self.calls, self.follows, self.step_answers = [], [], []

    def send_dm(self, ig_user_id, comment_id, message):
        self.calls.append(("dm", comment_id, message))
        return result()

    def send_to(self, ig_user_id, igsid, message):
        self.calls.append(("step", igsid, message))
        return self.step_answers.pop(0) if self.step_answers else result()

    def follow_status(self, igsid):
        self.calls.append(("follow", igsid, None))
        answer = self.follows.pop(0) if self.follows else None
        if answer is None:
            return result(ok=False, data={}, http=500, outcome="transient")
        return result(data={"is_user_follow_business": answer, "id": igsid})


class Notifier:
    def event(self, key, text):
        return None

    def sync(self, problems, now):
        pass

    def flush(self):
        pass


class Funnel(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.conn = store.connect(os.path.join(self.dir, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        store.kv_set_many(self.conn, {**ME, "send_budget": "unlimited"})
        self.raw = json.loads(json.dumps(EXAMPLE))
        self.raw["sending"] = True
        self.raw["click_base"] = "https://ig.example.com/r/"
        self.raw["reels"][0]["flow"] = json.loads(json.dumps(FLOW))
        self.client = Client()
        patch = mock.patch.object(sys, "stderr", self.enterContext(open(os.devnull, "w")))
        patch.start()
        self.addCleanup(patch.stop)
        self.build()

    def build(self):
        self.cfg = config.loads(json.dumps(self.raw))
        self.bot = engine.Engine(self.cfg, self.conn, self.client, Notifier())
        store.kv_set(self.conn, "send_budget", "unlimited")   # a new config disarms (4); the tests re-arm

    def comment(self, user, comment_id=None):
        comment_id = comment_id or "c_" + user
        store.add_comment(self.conn, comment_id, MEDIA, user, "name_" + user, "арена", clock.iso(), "pending")
        return comment_id

    def user_of(self, variant):
        reel = self.cfg.reels[0]
        return next(f"u{i}" for i in range(1000) if reel.flow.variant(MEDIA, f"u{i}").name == variant)

    def first_dm(self, user):
        comment_id = self.comment(user)
        # earlier DMs of the test happened long ago: the slot of G2 is free
        old = clock.iso(clock.now() - timedelta(hours=2))
        self.conn.execute("UPDATE attempts SET started_at=?, finished_at=? WHERE kind='dm'", (old, old))
        claim = store.claim_dm(self.conn, comment_id, self.cfg, {"user_id": ME["me_user_id"]})
        self.assertEqual(claim.kind, "claimed")
        message = self.bot._first_message(MEDIA, comment_id, claim)
        store.record_dm(self.conn, claim.attempt_id, result())
        return comment_id, claim, message

    def tap(self, comment_id, igsid="igsid1", kind="g", mid=None):
        store.add_inbound(self.conn, mid or f"mid-{kind}-{comment_id}-{len(self.client.calls)}", igsid, "postback",
                          f"{kind}:{comment_id}")
        self.bot._flow_work()

    def flow(self, comment_id):
        return store.flow_row(self.conn, comment_id)

    def steps(self):
        return [call for call in self.client.calls if call[0] == "step"]

    # ---- 14.3

    def test_variants_split_people_and_a_person_keeps_their_variant(self):
        reel = self.cfg.reels[0]
        counts = Counter(reel.flow.variant(MEDIA, f"p{i}").name for i in range(2000))
        self.assertTrue(900 < counts["A"] < 1100, counts)
        self.assertEqual({reel.flow.variant(MEDIA, "same").name for _ in range(5)}, {reel.flow.variant(MEDIA, "same").name})

    def test_opening_variant_sends_a_button_and_no_link(self):
        comment_id, claim, message = self.first_dm(self.user_of("A"))
        self.assertEqual(claim.variant, "A")
        payload = message["attachment"]["payload"]
        self.assertEqual(payload["buttons"], [{"type": "postback", "title": "Получить", "payload": "g:" + comment_id}])
        self.assertNotIn(LINK, json.dumps(message, ensure_ascii=False))
        self.assertEqual(self.flow(comment_id)["state"], "opened")

    def test_direct_variant_sends_the_text_with_a_tracked_link(self):
        comment_id, claim, message = self.first_dm(self.user_of("B"))
        self.assertEqual(claim.variant, "B")
        self.assertNotIn(LINK, message["text"])
        token = claim.tokens[0]
        self.assertIn("https://ig.example.com/r/" + token, message["text"])
        self.assertEqual(store.link_url(self.conn, token), LINK)
        self.assertIsNotNone(self.flow(comment_id)["link_at"])   # the DM itself carried the link

    def test_reel_without_flow_is_unchanged(self):
        del self.raw["reels"][0]["flow"]
        self.build()
        comment_id = self.comment("plain")
        claim = store.claim_dm(self.conn, comment_id, self.cfg, {"user_id": ME["me_user_id"]})
        self.assertIsNone(claim.variant)
        self.assertEqual(self.bot._first_message(MEDIA, comment_id, claim), config.render(self.cfg, self.cfg.reels[0], claim.late))
        self.assertIsNone(self.flow(comment_id))

    # ---- 14.4 / 14.5

    def test_tap_by_a_follower_gets_the_link_buttons_tracked(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [True]
        self.tap(comment_id)
        step = self.steps()[-1]
        buttons = step[2]["attachment"]["payload"]["buttons"]
        self.assertEqual([b["title"] for b in buttons], ["Открыть", "Telegram"])
        self.assertTrue(all(b["url"].startswith("https://ig.example.com/r/") for b in buttons))
        self.assertEqual([store.link_url(self.conn, b["url"].rsplit("/", 1)[1]) for b in buttons],
                         ["https://github.com/x", "https://t.me/x"])
        flow = self.flow(comment_id)
        self.assertEqual((flow["state"], flow["follow"], flow["igsid"]), ("linked", "yes", "igsid1"))

    def test_not_following_gets_a_prompt_then_the_link_after_done(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [False, True]
        self.tap(comment_id)
        prompt = self.steps()[-1][2]["attachment"]["payload"]
        self.assertEqual(prompt["buttons"][1], {"type": "postback", "title": "Готово", "payload": "d:" + comment_id})
        self.assertEqual(self.flow(comment_id)["state"], "gate")
        self.tap(comment_id, kind="d")
        self.assertEqual(self.flow(comment_id)["state"], "linked")
        self.assertEqual([c[3] for c in self.conn.execute("SELECT * FROM checks")], ["gate", "gate"])

    def test_three_prompts_then_stuck_and_nothing_more(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [False] * 5
        for kind in "gddd":
            self.tap(comment_id, kind=kind)
        self.assertEqual([s[2]["attachment"]["payload"]["text"] for s in self.steps()],
                         ["Подпишись и нажми «Готово»", "Пока не вижу подписку", "Пока не вижу подписку"])
        self.assertEqual(self.flow(comment_id)["state"], "stuck")

    def test_unknown_follow_status_fails_open(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [None]
        self.tap(comment_id)
        self.assertEqual(self.flow(comment_id)["state"], "linked")
        self.assertEqual(self.flow(comment_id)["follow"], "unknown")

    def test_variant_without_gate_gets_the_link_without_a_check(self):
        self.raw["reels"][0]["flow"]["variants"] = [{"name": "C", "weight": 1, "opening": True, "gate": False}]
        self.build()
        comment_id, _, _ = self.first_dm("anyone")
        self.tap(comment_id)
        self.assertEqual([c[0] for c in self.client.calls if c[0] != "dm"], ["step"])
        self.assertEqual(self.flow(comment_id)["state"], "linked")

    def test_repeated_delivery_and_taps_after_the_link_send_nothing_twice(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [True]
        self.tap(comment_id, mid="same")
        self.tap(comment_id, mid="same")   # Meta delivered the webhook twice
        self.tap(comment_id)               # the person tapped again
        self.assertEqual(len(self.steps()), 1)

    def test_an_unclear_answer_to_the_link_is_never_retried(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [True, True]
        self.client.step_answers = [result(ok=False, http=0, outcome="uncertain", reason="no_response")]
        self.tap(comment_id)
        self.tap(comment_id)
        self.assertEqual(len(self.steps()), 1)

    def test_a_tap_from_someone_else_sends_nothing(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [False]
        self.tap(comment_id, igsid="owner")
        sent = len(self.steps())
        self.tap(comment_id, igsid="stranger", kind="d")
        self.assertEqual(len(self.steps()), sent)
        self.assertEqual(self.conn.execute("SELECT result FROM inbound ORDER BY id DESC").fetchone()[0], "other_sender")

    def test_taps_wait_while_sending_is_off_then_are_answered(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.raw["sending"] = False
        self.build()
        self.client.follows = [True]
        self.tap(comment_id)
        self.assertEqual(self.steps(), [])
        self.raw["sending"] = True
        self.build()
        self.bot._flow_work()
        self.assertEqual(self.flow(comment_id)["state"], "linked")

    def test_followup_once_inside_the_window(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [True]
        self.tap(comment_id)
        self.bot._flow_work()
        self.assertEqual(len(self.steps()), 1)   # not yet: 60 minutes have not passed
        later = clock.now() + timedelta(minutes=61)
        with mock.patch.object(clock, "now", lambda: later):
            self.bot._flow_work()
            self.bot._flow_work()
        self.assertEqual([s[2] for s in self.steps()][1:], [{"text": "Получилось поставить?"}])

    def test_recheck_marks_an_unfollow(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [True, False]
        self.tap(comment_id)
        later = clock.now() + timedelta(days=1, minutes=1)
        with mock.patch.object(clock, "now", lambda: later):
            self.bot._rechecks()
        self.assertIsNotNone(self.flow(comment_id)["unfollowed_at"])

    def test_message_from_a_person_reopens_their_window(self):
        comment_id, _, _ = self.first_dm(self.user_of("A"))
        self.client.follows = [True]
        self.tap(comment_id)
        before = self.flow(comment_id)["last_in_at"]
        store.add_inbound(self.conn, "m-text", "igsid1", "message", text="спасибо")
        later = clock.now() + timedelta(minutes=5)
        with mock.patch.object(clock, "now", lambda: later):
            self.bot._flow_work()
        self.assertGreater(self.flow(comment_id)["last_in_at"], before)


    def test_report_counts_the_funnel_per_variant(self):
        import report
        a, _, _ = self.first_dm(self.user_of("A"))
        b, claim_b, _ = self.first_dm(self.user_of("B"))
        self.client.follows = [True]
        self.tap(a)
        store.add_click(self.conn, claim_b.tokens[0], "test")
        data = report.build(self.conn, self.cfg, clock.now())
        rows = {row["variant"]: row for row in data["funnel"]}
        self.assertEqual((rows["A"]["first_dm"], rows["A"]["pressed"], rows["A"]["checked_yes"], rows["A"]["linked"]),
                         (1, 1, 1, 1))
        self.assertEqual((rows["B"]["linked"], rows["B"]["clicks"], rows["B"]["clicked_people"]), (1, 1, 1))
        self.assertIn("Воронка по вариантам", report.render(data))

    def test_a_tap_waiting_long_is_a_problem(self):
        import report
        store.add_inbound(self.conn, "late", "igsid1", "postback", "g:nobody")
        later = clock.now() + timedelta(minutes=11)
        keys = [key for key, _ in report.problems(self.conn, self.cfg, later)]
        self.assertIn("inbound_waiting", keys)


class FlowConfig(unittest.TestCase):
    def load(self, flow):
        raw = json.loads(json.dumps(EXAMPLE))
        raw["reels"][0]["flow"] = flow
        return config.loads(json.dumps(raw))

    def problems(self, flow):
        with self.assertRaises(config.ConfigError) as caught:
            self.load(flow)
        return " ".join(caught.exception.problems)

    def test_a_config_without_flow_dumps_and_hashes_as_before(self):
        cfg = config.loads(json.dumps(EXAMPLE))
        self.assertNotIn("flow", config.dump(cfg))
        self.assertNotIn("click_base", config.dump(cfg))

    def test_round_trip(self):
        cfg = self.load(FLOW)
        self.assertEqual(config.loads(config.dump(cfg)), cfg)

    def test_rules(self):
        flow = json.loads(json.dumps(FLOW))
        flow["variants"][1]["gate"] = True
        self.assertIn("gate needs opening", self.problems(flow))
        flow = json.loads(json.dumps(FLOW))
        del flow["opening"]
        self.assertIn("flow.opening is needed", self.problems(flow))
        flow = json.loads(json.dumps(FLOW))
        flow["opening"]["button"] = "x" * 21
        self.assertIn("1 to 20 characters", self.problems(flow))
        flow = json.loads(json.dumps(FLOW))
        flow["link"]["text"] = "напиши арена"
        self.assertIn("own words", self.problems(flow))
        flow = json.loads(json.dumps(FLOW))
        flow["variants"] = []
        self.assertIn("1 to 4 variants", self.problems(flow))


if __name__ == "__main__":
    unittest.main()
