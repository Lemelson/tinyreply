"""Round r4: the joint audit (Claude + Codex) and the review of the Codex fixes. One class per fixed issue;
the cards are audit-claude/issues/<id>.md. No network: Telegram points at a closed loopback port."""
import dataclasses
import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401  loopback guard

import clock
import config
import engine
import notify
import report
import store
from tests.test_store import CFG, ME, START

REEL = CFG.reels[0]


class Case(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.now, self.mono = START, 10000.0
        self.enterContext(mock.patch.object(clock, "now", lambda: self.now))
        self.enterContext(mock.patch.object(clock, "monotonic", lambda: self.mono))
        self.conn = store.connect(os.path.join(self.folder, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.cfg = dataclasses.replace(CFG, public_reply=False)
        self.path = os.path.join(self.folder, "config.json")
        self.file(self.cfg)

    def file(self, cfg):
        with open(self.path, "w", encoding="utf-8") as out:
            out.write(cfg if isinstance(cfg, str) else config.dump(cfg))

    def start(self, **options):
        notifier = notify.Notifier(self.conn, lambda: ("123:synthetic", "42"), "http://127.0.0.1:1", repeat_min=120)
        bot = engine.Engine(self.cfg, self.conn, None, notifier, **options)
        bot.me = ME
        return bot

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def said(self, key=None):
        return [text for row_key, text in self.conn.execute("SELECT key, text FROM outbox ORDER BY id")
                if key is None or row_key == key]

    def add(self, comment_id, status, age=3600, username=None, media=None):
        store.add_comment(self.conn, comment_id, media or REEL.media_id, "u_" + comment_id, username or comment_id,
                          "арена", clock.iso(self.now - timedelta(seconds=age)), "seen", "")
        self.conn.execute("UPDATE comments SET status=? WHERE comment_id=?", (status, comment_id))


class ConfigFileIsWatched(Case):
    """C01, K-A5-1, K-A5-2: the owner edits config.json and forgets the restart. The service applies nothing of the
    file, but says so on Telegram: people under a reel that is only in the file would be lost in silence."""

    def test_a_reel_added_to_the_file_is_told_and_not_applied(self):
        bot = self.start(config_path=self.path)
        bot._announce()
        self.assertEqual(self.said("restart_needed"), [])                       # control: the file as it was loaded
        added = dataclasses.replace(REEL, media_id="222", name="second")
        self.file(dataclasses.replace(self.cfg, reels=(*self.cfg.reels, added)))
        bot._announce()
        (line,) = self.said("restart_needed")
        self.assertIn("новые рилсы и слова из файла пока не работают", line)
        self.assertEqual([reel.media_id for reel in bot.cfg.reels], [REEL.media_id])   # nothing of the file is used
        self.assertEqual(store.kv_get(self.conn, "config_hash"), config.fingerprint(self.cfg))

    def test_a_file_that_cannot_be_loaded_is_told_before_the_restart_that_would_end_the_service(self):
        bot = self.start(config_path=self.path)
        self.file("{ not json")
        bot._announce()
        (line,) = self.said("config_file_invalid")
        self.assertIn("после перезапуска или перезагрузки сервера НЕ запустится", line)
        self.file(self.cfg)                                                     # repaired: the problem goes away
        bot._announce()
        self.assertIn("✓ cleared: config_file_invalid", self.said())

    def test_without_a_path_nothing_is_read(self):
        bot = self.start()
        self.file("{ not json")
        bot._announce()
        self.assertEqual(self.said("config_file_invalid") + self.said("restart_needed"), [])


class NewCodeDisarms(Case):
    """C16: a deploy changes what the bot does as surely as a config edit does; it needs a human `arm` too."""

    def test_changed_code_sets_the_budget_to_zero_and_says_so_once(self):
        self.start()
        store.arm(self.conn, 50)
        self.start()                                                            # a restart of the same code
        self.assertEqual(store.budget(self.conn), 50)
        with mock.patch.object(engine, "code_fingerprint", lambda: "another build"):
            self.start()
            self.assertEqual(store.budget(self.conn), 0)
            self.assertIn("the code of the bot changed", self.said("config_changed")[-1])
            store.arm(self.conn, 10)
            self.start()                                                        # the same new build again
            self.assertEqual(store.budget(self.conn), 10)

    def test_the_fingerprint_is_of_the_nine_modules(self):
        self.assertEqual(len(engine.code_fingerprint()), 64)
        self.assertEqual(engine.code_fingerprint(), engine.code_fingerprint())
        self.assertEqual(sorted(engine._MODULES), sorted(name[:-3] for name in os.listdir(os.path.dirname(engine.__file__))
                                                         if name.endswith(".py") and name != "__init__.py"))


class LostPeopleArePaced(Case):
    """C08: windows that close one per cycle were one Telegram message each, hundreds a day."""

    def test_the_first_loss_is_said_at_once_and_the_next_ones_once_per_repeat_min_summed_up(self):
        bot = self.start()
        self.add("a", "expired")
        bot._lost_people()
        self.assertEqual(len(self.said("expired")), 1)
        for number in range(5):                                                 # five more, a cycle apart
            self.advance(90)
            self.add(f"b{number}", "expired")
            bot._lost_people()
        self.assertEqual(len(self.said("expired")), 1)
        self.assertEqual(int(store.kv_get(self.conn, "told_expired")), 1)       # the untold ones are still owed
        self.advance(120 * 60)
        bot._lost_people()
        lines = self.said("expired")
        self.assertEqual(len(lines), 2)
        self.assertIn("5 people lost", lines[1])
        self.assertIn("(6 in all)", lines[1])

    def test_a_clock_that_went_back_does_not_silence_it_and_uncertain_is_never_paced(self):
        bot = self.start()
        self.add("a", "expired")
        bot._lost_people()
        self.advance(-3600)
        self.add("b", "expired")
        bot._lost_people()
        self.assertEqual(len(self.said("expired")), 2)
        for number in range(3):
            self.add(f"u{number}", "uncertain")
            bot._lost_people()
        self.assertEqual(len(self.said("uncertain")), 3)


class UncertainIsNamed(Case):
    """K-A5-3: the repeated reminder named nobody; the owner reads it on a phone."""

    def problem(self):
        self.start()
        return dict(report.problems(self.conn, self.cfg, self.now))["uncertain_manual"]

    def test_the_reminder_names_the_person_the_reel_and_the_command(self):
        self.add("17890", "uncertain", username="anna")
        text = self.problem()
        self.assertIn(f"1 чел. @anna («{REEL.name}») — igbot ack-uncertain 17890.", text)
        self.assertIn("«Запросы»", text)
        self.assertNotIn("COMMENT_ID", text)

    def test_more_than_three_are_counted_not_listed(self):
        for number in range(5):
            self.add(f"c{number}", "uncertain", age=100 + number, username=f"p{number}")
        text = self.problem()
        self.assertIn("5 чел.", text)
        self.assertEqual(text.count("igbot ack-uncertain"), 3)
        self.assertIn("…и ещё 2: igbot report", text)


class CodeWordWithADigit(unittest.TestCase):
    """C27, K-A5-5: the matcher reads letters only, so "гайд2" silently became "гайд"."""

    def load(self, **reel):
        with open(os.path.join(os.path.dirname(config.__file__), "config.example.json"), encoding="utf-8") as file:
            data = json.load(file)
        data["reels"][0].update(reel)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            with open(path, "w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False)
            return config.load(path)

    def test_a_digit_in_a_keyword_or_a_typo_is_a_config_error(self):
        for field in ("keywords", "also_match"):
            with self.subTest(field=field), self.assertRaises(config.ConfigError) as caught:
                self.load(**{field: ["гайд2"]})
            self.assertIn("a digit would be dropped", "; ".join(caught.exception.problems))

    def test_real_words_with_marks_and_emoji_are_still_accepted(self):
        self.assertEqual(self.load(keywords=["арена🔥", "café", "Arena!"]).reels[0].keywords, ("арена🔥", "café", "Arena!"))


FIXED_PROOFS = (   # proof tests of the reviewers that this round turned green; they live with their helpers in attacks/
    "attacks.r4.a1.test_log_between_claim_and_post", "attacks.r4.a2.test_hide_scrubber",
    "attacks.r4.a2.test_response_reading", "attacks.r4.a3.test_c02_edited_comment", "attacks.r4.a4.test_disarm_brake",
    "attacks.r4.a7.test_piped_command", "attacks.r4.a8.test_telemetry_in_send_path",
)
# Not here on purpose: attacks.r4.a3.test_f03_f04_rematch and test_f05_one_bad_item are green too, but they drive a
# whole world over loopback with a fast clock, and one of them failed once in a full run on the loaded server
# (7 reruns there were green). What they prove is pinned without a clock in tests/test_i01_core.py (f03, f05).


def load_tests(loader, standard_tests, pattern):
    if os.path.isdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "attacks", "r4")):
        for name in FIXED_PROOFS:
            standard_tests.addTests(loader.loadTestsFromName(name))
    return standard_tests


if __name__ == "__main__":
    unittest.main()
