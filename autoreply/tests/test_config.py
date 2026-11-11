"""SPEC section 4: config load, every rejection rule, Meta's message limit, render."""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import config
import match

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(CODE, "config.example.json")
LINK = "https://example.com/arena"
PROFILE = "https://www.instagram.com/someone/"


def valid():
    return {
        "api_version": "v26.0",
        "sending": False,
        "poll_interval_sec": 90,
        "max_sends_per_hour": 60,
        "public_reply": True,
        "profile_url": PROFILE,
        "greeting": "Привет!",
        "late_greeting": "Привет! Лучше поздно, чем никогда",
        "late_after_minutes": 60,
        "public_phrases": ["Отправил в директ", "Лови в личке"],
        "reels": [{
            "name": "arena", "media_id": "111", "permalink": "", "active": True,
            "keywords": ["арена", "arena"], "also_match": ["арееа"], "max_words": 5,
            "link": LINK,
            "text": "{greeting} Держи ссылку на скилл, пользуйся на здоровье и подписывайся",
        }],
        "alerts": {"repeat_min": 120, "digest_hour_utc": 6},
    }


def second_reel(**changes):
    reel = dict(valid()["reels"][0], name="guide", media_id="222", keywords=["гайд"], also_match=[])
    reel.update(changes)
    return reel


class ConfigCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "config.json")

    def load(self, data):
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
        return config.load(self.path)

    def load_changed(self, change):
        data = valid()
        change(data)
        return self.load(data)

    def problems(self, change):
        with self.assertRaises(config.ConfigError) as caught:
            self.load_changed(change)
        return caught.exception.problems

    def assert_one_problem(self, change, about):
        problems = self.problems(change)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(about), problems)

    def with_text(self, text, **reel_changes):
        def change(data):
            data["reels"][0].update(text=text, **reel_changes)
        return change


def set_top(key, value):
    return lambda data: data.__setitem__(key, value)


def set_reel(key, value):
    return lambda data: data["reels"][0].__setitem__(key, value)


def set_alerts(key, value):
    return lambda data: data["alerts"].__setitem__(key, value)


class Load(ConfigCase):
    def test_the_base_of_these_tests_is_valid(self):
        self.assertEqual(self.load(valid()).reels[0].name, "arena")

    def test_every_key_of_the_file_is_an_attribute(self):
        data = valid()
        data["reels"].append(second_reel(active=False))
        cfg = self.load(data)

        def same(value):
            return tuple(value) if isinstance(value, list) else value

        for key in set(data) - {"reels", "alerts"}:
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key), same(data[key]))
        for key, value in data["alerts"].items():
            with self.subTest(key="alerts." + key):
                self.assertEqual(getattr(cfg.alerts, key), value)
        self.assertEqual(len(cfg.reels), 2)
        for reel, reel_data in zip(cfg.reels, data["reels"]):
            for key, value in reel_data.items():
                with self.subTest(key=f"{reel.name}.{key}"):
                    self.assertEqual(getattr(reel, key), same(value))

    def test_reel_by_media_has_active_and_inactive_reels(self):
        data = valid()
        data["reels"].append(second_reel(active=False))
        cfg = self.load(data)
        self.assertEqual(set(cfg.reel_by_media), {"111", "222"})
        self.assertIs(cfg.reel_by_media["111"], cfg.reels[0])
        self.assertTrue(cfg.reel_by_media["111"].active)
        self.assertFalse(cfg.reel_by_media["222"].active)

    def test_the_config_is_read_only(self):
        cfg = self.load(valid())
        with self.assertRaises(AttributeError):
            cfg.sending = True
        with self.assertRaises(AttributeError):
            cfg.reels[0].active = False
        with self.assertRaises(AttributeError):
            cfg.alerts.repeat_min = 1
        with self.assertRaises(TypeError):
            cfg.reels[0].keywords[0] = "привет"
        with self.assertRaises(TypeError):
            cfg.public_phrases[0] = "арена"
        cfg.reel_by_media["999"] = cfg.reels[0]
        self.assertNotIn("999", cfg.reel_by_media)
        self.assertFalse(cfg.sending)

    def test_file_problems_are_config_errors(self):
        missing = os.path.join(os.path.dirname(self.path), "nothing.json")
        for label, content in [("not json", b"{"), ("empty", b""), ("not utf-8", b'{"greeting": "\xff"}'),
                               ("not an object", b"[1, 2]"), ("a string", b'"config"')]:
            with self.subTest(label):
                with open(self.path, "wb") as file:
                    file.write(content)
                with self.assertRaises(config.ConfigError) as caught:
                    config.load(self.path)
                self.assertEqual(len(caught.exception.problems), 1)
        with self.assertRaises(config.ConfigError) as caught:
            config.load(missing)
        self.assertIn(missing, caught.exception.problems[0])

    def test_a_key_written_twice_is_refused(self):
        # json would silently keep the last one: "sending": false ... "sending": true
        text = json.dumps(valid(), ensure_ascii=False)
        for twice in [text[:-1] + ', "sending": true}',
                      text.replace('"active": true', '"active": false, "active": true')]:
            with open(self.path, "w", encoding="utf-8") as file:
                file.write(twice)
            with self.assertRaises(config.ConfigError) as caught:
                config.load(self.path)
            self.assertIn("twice", caught.exception.problems[0])

    def test_all_problems_are_reported_at_once(self):
        def change(data):
            data["extra"] = 1
            del data["greeting"]
            data["max_sends_per_hour"] = 0
            data["public_phrases"] = ["Арена"]
            data["alerts"]["digest_hour_utc"] = 24
            data["reels"][0]["link"] = "http://example.com"
            data["reels"][0]["keywords"] = []
            data["reels"].append(second_reel(media_id="x", keywords=["арена"], active="yes"))

        problems = self.problems(change)
        for about in ["extra: unknown key", "greeting: missing", "max_sends_per_hour: ", "public_phrases: at least 2",
                      "public_phrases[0]: the matcher of reels[1]", "alerts.digest_hour_utc: ", "reels[0].link: ",
                      "reels[0].keywords: ", "reels[1].media_id: ", "reels[1].active: "]:
            with self.subTest(about=about):
                self.assertEqual(len([problem for problem in problems if problem.startswith(about)]), 1, problems)
        self.assertEqual(len(problems), 10, problems)

    def test_the_error_text_lists_the_problems(self):
        with self.assertRaises(config.ConfigError) as caught:
            self.load_changed(lambda data: data.update(extra=1, sending="yes"))
        for problem in caught.exception.problems:
            self.assertIn(problem, str(caught.exception))
        self.assertEqual(len(caught.exception.problems), 2)

    def test_a_lone_surrogate_is_a_problem_not_a_crash(self):
        # a JSON file can carry half of an emoji as an escape: "\ud83d"
        for change, about in [(set_top("greeting", "\ud83d"), "greeting: "),
                              (set_top("public_phrases", ["Отправил", "Лови \ud83d"]), "public_phrases: "),
                              (set_reel("text", "\udfff текст"), "reels[0].text: "),
                              (set_reel("link", "https://example.com/\ud83d"), "reels[0].link: ")]:
            data = valid()
            change(data)
            with open(self.path, "w", encoding="ascii") as file:
                json.dump(data, file, ensure_ascii=True)
            with self.subTest(about=about):
                with self.assertRaises(config.ConfigError) as caught:
                    config.load(self.path)
                self.assertEqual(len(caught.exception.problems), 1, caught.exception.problems)
                self.assertTrue(caught.exception.problems[0].startswith(about), caught.exception.problems)


class Rejected(ConfigCase):
    """One row per rule of SPEC section 4; each must give exactly one problem, about the right key."""

    def test_unknown_keys_at_any_level(self):
        self.assert_one_problem(set_top("extra", 1), "extra: unknown key")
        self.assert_one_problem(set_top("full_sweep_every_sec", 60), "full_sweep_every_sec: unknown key")
        self.assert_one_problem(set_reel("extra", 1), "reels[0].extra: unknown key")
        self.assert_one_problem(set_alerts("extra", 1), "alerts.extra: unknown key")

    def test_every_key_is_required(self):
        data = valid()
        for key in data:
            with self.subTest(key=key):
                self.assert_one_problem(lambda d, key=key: d.pop(key), f"{key}: missing")
        for key in data["reels"][0]:
            with self.subTest(key="reels[0]." + key):
                self.assert_one_problem(lambda d, key=key: d["reels"][0].pop(key), f"reels[0].{key}: missing")
        for key in data["alerts"]:
            with self.subTest(key="alerts." + key):
                self.assert_one_problem(lambda d, key=key: d["alerts"].pop(key), f"alerts.{key}: missing")

    def test_duplicate_reel_name(self):
        self.assert_one_problem(lambda d: d["reels"].append(second_reel(name="arena")), "reels[1].name: duplicate")

    def test_duplicate_media_id(self):
        self.assert_one_problem(lambda d: d["reels"].append(second_reel(media_id="111")), "reels[1].media_id: duplicate")
        self.assert_one_problem(lambda d: d["reels"].append(second_reel(media_id="111", active=False)),
                                "reels[1].media_id: duplicate")

    def test_two_different_reels_are_fine(self):
        cfg = self.load_changed(lambda d: d["reels"].append(second_reel()))
        self.assertEqual([reel.name for reel in cfg.reels], ["arena", "guide"])

    def test_media_id_not_all_digits(self):
        for media_id in ["17abc", "", " 111", "111\n", "1.5", "-1", "١٢٣", "²", 111, None]:
            with self.subTest(media_id=media_id):
                self.assert_one_problem(set_reel("media_id", media_id), "reels[0].media_id: ")

    def test_keyword_that_is_not_one_word_of_two_letters(self):
        for key in ("keywords", "also_match"):
            for entry in ["арена скилл", "а", "аа", "", "🔥", "123", "@arena", 5, None, ["арена"]]:
                with self.subTest(key=key, entry=entry):
                    self.assert_one_problem(set_reel(key, ["арена", entry]), f"reels[0].{key}: ")

    def test_empty_keywords(self):
        self.assert_one_problem(set_reel("keywords", []), "reels[0].keywords: ")
        self.assert_one_problem(set_reel("keywords", "арена"), "reels[0].keywords: ")
        self.assertEqual(self.load_changed(set_reel("also_match", [])).reels[0].also_match, ())

    def test_link_and_profile_url_must_be_https(self):
        for url in ["http://example.com/arena", "ftp://example.com", "example.com", "https://", "https:///path",
                    "https://example.com/a b", " https://example.com", "HTTPS://example.com", "", None, 5]:
            with self.subTest(url=url):
                self.assert_one_problem(set_reel("link", url), "reels[0].link: ")
                self.assert_one_problem(set_top("profile_url", url), "profile_url: ")

    def test_permalink_is_empty_or_https(self):
        self.assert_one_problem(set_reel("permalink", "http://www.instagram.com/reel/abc/"), "reels[0].permalink: ")
        self.assert_one_problem(set_reel("permalink", None), "reels[0].permalink: ")
        cfg = self.load_changed(set_reel("permalink", "https://www.instagram.com/reel/DPabc123/"))
        self.assertEqual(cfg.reels[0].permalink, "https://www.instagram.com/reel/DPabc123/")

    def test_max_sends_per_hour_outside_1_to_300(self):
        for value in [0, 301, -1, 10 ** 6, 60.0, 60.5, "60", True, None]:
            with self.subTest(value=value):
                self.assert_one_problem(set_top("max_sends_per_hour", value), "max_sends_per_hour: ")
        for value in [1, 300]:
            self.assertEqual(self.load_changed(set_top("max_sends_per_hour", value)).max_sends_per_hour, value)

    def test_poll_interval_under_30(self):
        for value in [29, 0, -90, 29.9, "90", True, None]:
            with self.subTest(value=value):
                self.assert_one_problem(set_top("poll_interval_sec", value), "poll_interval_sec: ")
        self.assertEqual(self.load_changed(set_top("poll_interval_sec", 30)).poll_interval_sec, 30)

    def test_fewer_than_two_public_phrases_while_public_reply_is_on(self):
        for phrases in [[], ["Отправил в директ"], ["Отправил в директ", "Отправил в директ"]]:
            with self.subTest(phrases=phrases):
                self.assert_one_problem(set_top("public_phrases", phrases), "public_phrases: at least 2")

    def test_public_reply_off_needs_no_phrases(self):
        for phrases in [[], ["Отправил в директ"]]:
            cfg = self.load_changed(lambda d, phrases=phrases: d.update(public_reply=False, public_phrases=phrases))
            self.assertEqual(cfg.public_phrases, tuple(phrases))

    def test_public_phrase_the_matcher_would_accept(self):
        for phrase in ["Арена!", "Лови арену 🔥", "Aрена в директе"]:
            with self.subTest(phrase=phrase):
                self.assert_one_problem(lambda d, phrase=phrase: d["public_phrases"].append(phrase), "public_phrases[2]: ")
        self.assert_one_problem(lambda d: d.update(public_reply=False, public_phrases=["Арена"]), "public_phrases[0]: ")

    def test_public_phrase_that_only_mentions_the_keyword_is_fine(self):
        cfg = self.load_changed(lambda d: d["public_phrases"].append("Отправил ссылку на арену в директ, загляни в запросы"))
        self.assertEqual(len(cfg.public_phrases), 3)

    def test_public_phrase_accepted_by_another_reels_matcher(self):
        def change(data):
            data["reels"].append(second_reel(active=False))
            data["public_phrases"].append("Лови гайд")

        problems = self.problems(change)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("public_phrases[2]: "))
        self.assertIn("the matcher of reels[1] ", problems[0])

    def test_dm_text_the_matcher_would_accept(self):
        # with the link line this text has more words than max_words=5: a request only for a reel that takes that many
        text = "Арена 👇 вот ссылка на скилл"
        self.assertEqual(self.load_changed(self.with_text(text)).reels[0].text, text)
        problems = self.problems(self.with_text(text, max_words=20))
        self.assertEqual([problem.split(": ")[0] for problem in problems], ["reels[0].text with greeting"])

    def test_dm_text_is_checked_with_either_greeting(self):
        # 9 words: «Привет! арена» with the link line fits (up to 9), with the long late greeting it does not
        roomy = dict(max_words=9, link="https://example.com/skill")
        only_fresh = self.problems(self.with_text("{greeting} арена", **roomy))
        self.assertEqual([problem.split(": ")[0] for problem in only_fresh], ["reels[0].text with greeting"])

        def short_late_greeting(data):
            data["late_greeting"] = "Привет снова!"
            data["reels"][0].update(text="{greeting} арена", **roomy)

        both = self.problems(short_late_greeting)
        self.assertEqual([problem.split(": ")[0] for problem in both],
                         ["reels[0].text with greeting", "reels[0].text with late_greeting"])

        def keyword_only_in_late_greeting(data):
            data["late_greeting"] = "Арена"
            data["reels"][0].update(text="{greeting} 👇", **roomy)

        self.assertEqual([problem.split(": ")[0] for problem in self.problems(keyword_only_in_late_greeting)],
                         ["reels[0].text with late_greeting"])

    def test_dm_text_accepted_by_another_reels_matcher(self):
        def change(data):
            data["reels"].append(second_reel(max_words=20))
            data["reels"][0]["text"] = "Лови гайд"

        problems = self.problems(change)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("reels[0].text with greeting: "))
        self.assertIn("the matcher of reels[1] ", problems[0])

    def test_a_long_dm_text_may_name_the_keyword(self):
        cfg = self.load_changed(self.with_text("{greeting} Держи Arena Skill, дальше пиши /arena и задачу"))
        self.assertIn("Arena", cfg.reels[0].text)

    def test_wrong_types_and_ranges(self):
        for change, about in [
            (set_top("api_version", "26.0"), "api_version: "),
            (set_top("api_version", "v26.0/../v1.0"), "api_version: "),
            (set_top("api_version", 26), "api_version: "),
            (set_top("sending", "false"), "sending: "),
            (set_top("sending", 0), "sending: "),
            (set_top("sending", None), "sending: "),
            (set_top("public_reply", 1), "public_reply: "),
            (set_top("greeting", None), "greeting: "),
            (set_top("late_greeting", 5), "late_greeting: "),
            (set_top("late_after_minutes", -1), "late_after_minutes: "),
            (set_top("late_after_minutes", "60"), "late_after_minutes: "),
            (set_top("public_phrases", "Отправил"), "public_phrases: "),
            (set_top("public_phrases", ["Отправил", ""]), "public_phrases: "),
            (set_top("public_phrases", ["Отправил", 5]), "public_phrases: "),
            (set_top("reels", {}), "reels: "),
            (set_top("reels", ["arena"]), "reels[0]: "),
            (set_top("alerts", []), "alerts: "),
            (set_alerts("repeat_min", 0), "alerts.repeat_min: "),
            (set_alerts("digest_hour_utc", 24), "alerts.digest_hour_utc: "),
            (set_alerts("digest_hour_utc", -1), "alerts.digest_hour_utc: "),
            (set_reel("name", ""), "reels[0].name: "),
            (set_reel("active", "yes"), "reels[0].active: "),
            (set_reel("max_words", 0), "reels[0].max_words: "),
            (set_reel("max_words", "5"), "reels[0].max_words: "),
            (set_reel("text", ""), "reels[0].text: "),
            (set_reel("text", "   "), "reels[0].text: "),
        ]:
            with self.subTest(about=about):
                self.assert_one_problem(change, about)


class Limits(ConfigCase):
    """Meta's limit holds for BOTH greetings."""

    def test_text_message_is_measured_in_utf8_bytes_not_characters(self):
        # 490 Cyrillic letters: 490 characters but 980 bytes before the link line
        problems = self.problems(self.with_text("я" * 490, link="https://example.com/skill"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("UTF-8 bytes", problems[0])
        self.assertTrue(problems[0].startswith("reels[0].text with greeting: "), problems)
        self.assertEqual(self.load_changed(self.with_text("x" * 480, link="https://example.com/skill")).reels[0].text,
                         "x" * 480)

    def test_text_message_limit_is_exactly_1000_bytes(self):
        skill = dict(link="https://example.com/skill")   # a link without the keyword: the text is one long word
        cfg = self.load_changed(self.with_text("x", **skill))
        room = 1000 - (len(config.render(cfg, cfg.reels[0], False)["text"].encode()) - 1)
        fits = "я" * (room // 2) + "x" * (room % 2)
        loaded = self.load_changed(self.with_text(fits, **skill))
        self.assertEqual(len(config.render(loaded, loaded.reels[0], False)["text"].encode()), 1000)
        problems = self.problems(self.with_text(fits + "x", **skill))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1001 UTF-8 bytes", problems[0])

    def test_late_greeting_alone_can_break_the_byte_limit(self):
        def change(data):
            data["late_greeting"] = "я" * 100
            data["reels"][0]["text"] = "{greeting} " + "я" * 400

        problems = self.problems(change)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith("reels[0].text with late_greeting: "), problems)
        self.assertIn("UTF-8 bytes", problems[0])


class Render(ConfigCase):
    def reel_with(self, text, **top):
        cfg = self.load_changed(lambda data: (data.update(top), data["reels"][0].update(text=text)))
        return cfg, cfg.reels[0]

    def test_the_message(self):
        cfg, reel = self.reel_with("{greeting} Держи скилл")
        self.assertEqual(config.render(cfg, reel, False),
                         {"text": f"Привет! Держи скилл\n\n{LINK}"})

    def test_late_selects_the_late_greeting(self):
        cfg, reel = self.reel_with("{greeting} Держи скилл")
        self.assertEqual(config.render(cfg, reel, True),
                         {"text": f"Привет! Лучше поздно, чем никогда Держи скилл\n\n{LINK}"})

    def test_link_line_is_left_out_when_the_text_has_the_link(self):
        cfg, reel = self.reel_with("{greeting} Вот ссылка: {link} и всё")
        text = config.render(cfg, reel, False)["text"]
        self.assertEqual(text, f"Привет! Вот ссылка: {LINK} и всё")
        self.assertEqual(text.count(LINK), 1)

    def test_placeholders_are_optional(self):
        cfg, reel = self.reel_with("Держи скилл")
        for late in (False, True):
            self.assertEqual(config.render(cfg, reel, late), {"text": f"Держи скилл\n\n{LINK}"})

    def test_stray_braces_are_literal(self):
        # v1 used str.format: any of these raised or was eaten
        text = "{greeting} {} {x} {0} {{link}} }{ {greeting {link"
        cfg, reel = self.reel_with(text)
        self.assertEqual(config.render(cfg, reel, False)["text"],
                         "Привет! {} {x} {0} {" + LINK + "} }{ {greeting {link")

    def test_render_is_pure(self):
        cfg, reel = self.reel_with("{greeting} Держи скилл")
        before = copy.deepcopy((cfg, reel))
        first = config.render(cfg, reel, False)
        first.clear()
        self.assertEqual(config.render(cfg, reel, False), {"text": f"Привет! Держи скилл\n\n{LINK}"})
        self.assertEqual((cfg, reel), before)


class Example(unittest.TestCase):
    def test_the_shipped_example_loads_and_cannot_send(self):
        cfg = config.load(EXAMPLE)
        self.assertIs(cfg.sending, False)
        reel = cfg.reel_by_media["17900000000000101"]
        self.assertEqual(reel.keywords, ("арена", "arena"))
        self.assertEqual(reel.also_match, ("арееа", "areba", "arrna"))
        self.assertEqual(len(cfg.public_phrases), 10)

    def test_the_example_messages_carry_the_link_and_the_greeting(self):
        cfg = config.load(EXAMPLE)
        reel = cfg.reels[0]
        for late, greeting in [(False, cfg.greeting), (True, cfg.late_greeting)]:
            text = config.render(cfg, reel, late)["text"]
            self.assertTrue(text.startswith(greeting + " Держи"))
            self.assertEqual(text.count(reel.link), 1)
            self.assertTrue(text.endswith("\n\n" + reel.link))
            self.assertNotIn("Подписаться", text)
            self.assertLessEqual(len(text.encode()), 1000)

    def test_the_bot_does_not_trigger_on_its_own_words(self):
        cfg = config.load(EXAMPLE)
        matcher = match.Matcher(cfg.reels[0])
        own = list(cfg.public_phrases)
        for late in (False, True):
            own.append(config.render(cfg, cfg.reels[0], late)["text"])
        for text in own:
            with self.subTest(text=text[:30]):
                self.assertNotEqual(matcher.check(text)[0], "match")


class ApiBases(unittest.TestCase):
    def test_defaults_are_the_real_services(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.ig_api_base(), "https://graph.instagram.com")
            self.assertEqual(config.tg_api_base(), "https://api.telegram.org")
        with mock.patch.dict(os.environ, {"IG_API_BASE": "", "TG_API_BASE": ""}, clear=True):
            self.assertEqual(config.ig_api_base(), "https://graph.instagram.com")
            self.assertEqual(config.tg_api_base(), "https://api.telegram.org")

    def test_environment_overrides(self):
        with mock.patch.dict(os.environ, {"IG_API_BASE": "http://127.0.0.1:8080", "TG_API_BASE": "http://127.0.0.1:8081"}):
            self.assertEqual(config.ig_api_base(), "http://127.0.0.1:8080")
            self.assertEqual(config.tg_api_base(), "http://127.0.0.1:8081")

    def test_the_test_suite_points_at_loopback(self):
        self.assertTrue(config.ig_api_base().startswith("http://127.0.0.1:"))
        self.assertTrue(config.tg_api_base().startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
