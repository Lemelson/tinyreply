"""SPEC section 5: normalize, skeleton, Matcher.check, decide."""
import json
import os
import unicodedata
import unittest
from types import SimpleNamespace

import config
import match

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)

QUESTION_WORDS = ["зачем", "почему", "как", "где", "когда", "сколько", "какой", "какая", "какие", "что", "чё", "че",
                  "нафига", "нахрена"]

# what a commenter can throw at the matcher; none of it may raise
HOSTILE = [None, "", " \n\t ", "🔥" * 2500, "\ud800", "арена \udfff", "😀", "\x00арена\x00", "@" * 10000,
           "а" * 100000, "?" * 5000, 123, 1.5, b"\xd0\xb0", ["арена"], {"text": "арена"}, object(),
           unicodedata.normalize("NFD", "Гайд для Алёны, ёлки-палки, зайчик мой")]


def reel(keywords=("арена", "arena"), also_match=(), max_words=5):
    return SimpleNamespace(keywords=list(keywords), also_match=list(also_match), max_words=max_words)


class Normalize(unittest.TestCase):
    def test_table(self):
        latin_a = "a"
        for text, tokens in [
            ("Арена", ["арена"]),
            ("АРЕНА", ["арена"]),
            ("Алёна", ["алена"]),
            ("ЁЛКА", ["елка"]),
            ("Аренааа", ["арена"]),
            ("ААРРЕЕННАА", ["арена"]),
            ("Арееа", ["ареа"]),
            ("skill gif", ["skil", "gif"]),
            ("Арена🔥", ["арена"]),
            ("Арена)", ["арена"]),
            ("хочу арену, пожалуйста!", ["хочу", "арену", "пожалуйста"]),
            ("арена2026", ["арена"]),
            ("a1b", ["a", "b"]),
            ("а_б-в", ["а", "б", "в"]),
            ("@your_account арена", ["арена"]),
            ("@your_account  арена", ["арена"]),
            ("арена @кто-то.там!", ["арена"]),
            ("арена@mail.ru", ["арена"]),
            ("@ арена", ["арена"]),
            ("@арена", []),
            (latin_a.upper() + "рена", [latin_a + "рена"]),
            ("", []),
            ("   ", []),
            ("123 !!! ²½", []),
            ("🔥🔥🔥", []),
        ]:
            with self.subTest(text=text):
                self.assertEqual(match.normalize(text), tokens)

    def test_decomposed_letters_are_composed(self):
        for composed, tokens in [("гайд", ["гайд"]), ("Алёна", ["алена"]), ("ёж", ["еж"]), ("Й", ["й"])]:
            decomposed = unicodedata.normalize("NFD", composed)
            self.assertGreater(len(decomposed), len(composed), "the input must really be decomposed")
            with self.subTest(text=composed):
                self.assertEqual(match.normalize(decomposed), tokens)

    def test_never_raises_and_always_gives_a_list_of_words(self):
        for text in HOSTILE:
            with self.subTest(text=repr(text)[:40]):
                tokens = match.normalize(text)
                self.assertIsInstance(tokens, list)
                self.assertTrue(all(isinstance(token, str) and token.isalpha() for token in tokens))

    def test_hostile_inputs_with_a_known_answer(self):
        self.assertEqual(match.normalize(None), [])
        self.assertEqual(match.normalize("🔥" * 2500), [])
        self.assertEqual(match.normalize("\ud800"), [])
        self.assertEqual(match.normalize("арена \udfff"), ["арена"])
        self.assertEqual(match.normalize(b"\xd0\xb0"), [])
        self.assertEqual(match.normalize("а" * 100000), ["а"])


class Skeleton(unittest.TestCase):
    def test_each_look_alike(self):
        for latin, cyrillic in zip("aeopcxykmhtb", "аеорсхукмнтв"):
            with self.subTest(latin=latin):
                self.assertEqual(match.skeleton(latin), cyrillic)
                self.assertNotEqual(latin, cyrillic, "the expected letter must be the Cyrillic one")

    def test_other_letters_stay(self):
        self.assertEqual(match.skeleton("dfgijlnqrsuvwz"), "dfgijlnqrsuvwz")
        self.assertEqual(match.skeleton("арена"), "арена")

    def test_it_is_not_a_transliteration(self):
        self.assertEqual(match.skeleton("apeha"), "арена")
        self.assertNotEqual(match.skeleton("arena"), "арена")
        self.assertEqual(match.skeleton("arena"), "аrеnа")


class RealComments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = config.load(os.path.join(CODE, "config.example.json"))
        cls.matcher = match.Matcher(cfg.reel_by_media["17900000000000101"])
        with open(os.path.join(HERE, "data", "real_comment_texts.json"), encoding="utf-8") as file:
            cls.fixture = json.load(file)

    def test_fixture_is_the_1108_comments(self):
        self.assertEqual(len(self.fixture["must_match"]), 20)
        self.assertEqual(sum(entry["count"] for entry in self.fixture["must_match"]), 1108)

    def test_every_real_comment_matches(self):
        for entry in self.fixture["must_match"]:
            with self.subTest(text=entry["text"]):
                self.assertEqual(self.matcher.check(entry["text"]), ("match", ""))

    def test_latin_capital_a_before_cyrillic_letters_matches(self):
        mixed = [entry["text"] for entry in self.fixture["must_match"]
                 if entry["text"][0] == "A" and "а" <= entry["text"][-1] <= "я"]
        self.assertEqual(len(mixed), 2, "the fixture has two texts with a Latin A and Cyrillic letters")
        for text in mixed:
            self.assertEqual(self.matcher.check(text), ("match", ""))

    def test_nothing_else_matches(self):
        named_in_spec = ["Арина", "Ирена", "Алёна", "к чему это", "зачем арена?"]
        for text in named_in_spec + self.fixture["must_not_match"]:
            with self.subTest(text=text):
                self.assertNotEqual(self.matcher.check(text)[0], "match")


class Check(unittest.TestCase):
    def assert_kinds(self, matcher, kind, texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(matcher.check(text)[0], kind)

    def test_table(self):
        matcher = match.Matcher(reel())
        for text, expected in [
            ("арена", ("match", "")),
            ("Хочу арену", ("match", "")),
            ("а б в г арена", ("match", "")),
            ("Арена?", ("match", "")),
            ("арена???", ("match", "")),
            ("@someone арена?", ("match", "")),
            ("Арена❓", ("match", "")),
            ("какао арена", ("match", "")),
            ("а б в г д арена", ("near", "too_long")),
            ("арена пожалуйста очень сильно прошу вас", ("near", "too_long")),
            ("арена бесплатно?", ("near", "question")),
            ("арена бесплатно？", ("near", "question")),
            ("арена бесплатно❓", ("near", "question")),
            ("зачем арена", ("near", "question")),
            ("зачем арена?", ("near", "question")),
            ("ЧЁ за арена", ("near", "question")),
            ("привет", ("none", "")),
            ("зачем это?", ("none", "")),
            ("а б в г д е ж з", ("none", "")),
            ("", ("none", "")),
            (None, ("none", "")),
        ]:
            with self.subTest(text=text):
                self.assertEqual(matcher.check(text), expected)

    def test_every_question_word(self):
        matcher = match.Matcher(reel())
        for word in QUESTION_WORDS:
            with self.subTest(word=word):
                self.assertEqual(matcher.check(f"арена {word}"), ("near", "question"))
                self.assertEqual(matcher.check(f"{word.upper()} арена"), ("near", "question"))

    def test_question_words_written_with_look_alike_letters(self):
        # the same keyboard slip as the real «Aрена» with a Latin A: the first letters typed in the wrong layout
        matcher = match.Matcher(reel())
        for text in ["как арена".replace("к", "k", 1), "КАК арена".replace("К", "K").replace("А", "A"),
                     "что арена".replace("о", "o"), "kak arena"]:
            with self.subTest(text=text):
                self.assertTrue(any(letter.isascii() and letter.isalpha() for letter in text), "no Latin letter")
                self.assertEqual(matcher.check(text), ("near", "question"))

    def test_long_question_is_near_never_match(self):
        kind, reason = match.Matcher(reel()).check("скажите пожалуйста зачем мне нужна ваша арена?")
        self.assertEqual(kind, "near")
        self.assertIn(reason, ("too_long", "question"))

    def test_max_words_is_the_reels(self):
        for max_words, text, expected in [
            (1, "арена", ("match", "")),
            (1, "хочу арену", ("near", "too_long")),
            (2, "хочу арену", ("match", "")),
            (2, "очень хочу арену", ("near", "too_long")),
            (8, "а б в г д е ж арена", ("match", "")),
        ]:
            with self.subTest(max_words=max_words, text=text):
                self.assertEqual(match.Matcher(reel(max_words=max_words)).check(text), expected)

    def test_mentions_are_neither_keywords_nor_words(self):
        matcher = match.Matcher(reel())
        self.assert_kinds(matcher, "none", ["@arena", "@арена", "@arena_skill привет", "привет @арена"])
        self.assert_kinds(matcher, "match", ["@a @b @c @d @e @f арена", "@arena арена"])

    def test_case_forms_of_a_keyword_ending_in_a(self):
        matcher = match.Matcher(reel(keywords=["арена"]))
        self.assert_kinds(matcher, "match", ["арена", "арену", "арены", "арене", "ареной", "АРЕНОЙ", "хочу арену"])
        self.assert_kinds(matcher, "none", ["арен", "арено", "аренами", "аренах", "ареною", "аренка", "арендой"])

    def test_case_forms_come_from_the_normalised_keyword(self):
        matcher = match.Matcher(reel(keywords=["Ванна"]))
        self.assert_kinds(matcher, "match", ["ванна", "вана", "ванну", "ВАННОЙ", "ваннееее"])
        self.assert_kinds(matcher, "none", ["ван", "ванн", "ванная"])

    def test_case_forms_are_not_collapsed_into_other_words(self):
        # «боа»[:-1] + «ой» is «боой»; a comment token never has a doubled letter, so that form is out of reach
        # and must not become the word «бой»
        matcher = match.Matcher(reel(keywords=["боа"]))
        self.assert_kinds(matcher, "match", ["боа", "боу", "боы", "бое"])
        self.assert_kinds(matcher, "none", ["бой", "боой", "бо"])

    def test_no_case_forms_for_other_keywords(self):
        matcher = match.Matcher(reel(keywords=["гайд", "arena"]))
        self.assert_kinds(matcher, "match", ["гайд", "arena"])
        self.assert_kinds(matcher, "none", ["гайду", "гайды", "гайде", "гайдой", "arenu", "areny", "arene", "arenoy"])

    def test_no_transliteration(self):
        self.assert_kinds(match.Matcher(reel(keywords=["arena"])), "none", ["арена", "Арену"])
        self.assert_kinds(match.Matcher(reel(keywords=["арена"])), "none", ["arena", "Arena"])

    def test_look_alike_letters(self):
        matcher = match.Matcher(reel(keywords=["арена"]))
        self.assert_kinds(matcher, "match", ["Aрена", "AРена", "apeha", "APEHA", "аpенa", "apeнy"])
        self.assert_kinds(matcher, "none", ["apena", "areha", "арендa"])

    def test_also_match_is_exact_and_has_no_forms(self):
        with_typos = match.Matcher(reel(also_match=["арееа", "areba", "arrna"]))
        self.assert_kinds(with_typos, "match", ["Арееа", "Areba", "Arrna", "АРЕЕА", "areba!"])
        self.assert_kinds(with_typos, "none", ["арееу", "арееы", "ареей", "arebu", "arebas", "arrno"])
        self.assert_kinds(match.Matcher(reel()), "none", ["Арееа", "Areba", "Arrna"])

    def test_no_fuzzy_rule(self):
        # v1 took any same-length word one letter away from the keyword (K5)
        self.assert_kinds(match.Matcher(reel()), "none",
                          ["Арина", "Ирена", "Алёна", "Алена смотри", "@masha Алёна глянь", "арема", "орена",
                           "арнеа", "areba", "aréna", "Марена", "аренда"])

    def test_latin_keywords_are_not_mangled(self):
        # v1 reduced `skill` to «к», `claude` to «с» and crashed on `gif` (K5)
        matcher = match.Matcher(reel(keywords=["skill", "gif", "claude", "guide", "link"]))
        self.assert_kinds(matcher, "match", ["skill", "Skill", "SKILLL", "gif", "GIF!", "Claude", "guide", "link",
                                             "sкill"])
        self.assert_kinds(matcher, "none", ["иду к вам", "я с вами", "к", "с", "е", "нк", "скилл", "гиф", "клод",
                                            "ski", "gift", "links"])

    def test_decomposed_letters_in_text_and_in_keywords(self):
        composed = match.Matcher(reel(keywords=["гайд", "ёлка"]))
        decomposed = match.Matcher(reel(keywords=[unicodedata.normalize("NFD", "гайд"),
                                                  unicodedata.normalize("NFD", "ёлка")]))
        for matcher in (composed, decomposed):
            for text in ("Гайд", "ЁЛКА", "елку", "Ёлкой"):
                with self.subTest(text=text):
                    self.assertEqual(matcher.check(text)[0], "match")
                    self.assertEqual(matcher.check(unicodedata.normalize("NFD", text))[0], "match")
            self.assertEqual(matcher.check("гаид")[0], "none")

    def test_never_raises(self):
        matcher = match.Matcher(reel())
        for text in HOSTILE:
            with self.subTest(text=repr(text)[:40]):
                kind, reason = matcher.check(text)
                self.assertIn((kind, reason), [("match", ""), ("near", "too_long"), ("near", "question"), ("none", "")])

    def test_hostile_inputs_with_a_known_answer(self):
        matcher = match.Matcher(reel())
        for text, kind in [("🔥" * 2500, "none"), ("\ud800", "none"), (b"\xd0\xb0", "none"), (["арена"], "none"),
                           ("арена \udfff", "match"), ("\x00арена\x00", "match"),
                           ("🔥" * 2500 + "арена", "match"), ("арена " * 2000, "near")]:
            with self.subTest(text=repr(text)[:40]):
                self.assertEqual(matcher.check(text)[0], kind)

    def test_a_keyword_that_is_not_one_word_has_no_token(self):
        # config.load refuses such an entry (tests.test_config); a Matcher built past it still fails, never matches
        for entry in ["", "a", "аа", "два слова", "🔥", "@арена", None]:
            with self.subTest(entry=entry):
                self.assertIsNone(match.keyword_token(entry))
                for bad in (reel(keywords=[entry]), reel(also_match=[entry])):
                    self.assertRaises(AttributeError, match.Matcher, bad)
        self.assertEqual(match.keyword_token("Скилл!"), "скил")


class Decide(unittest.TestCase):
    def test_table(self):
        matcher = match.Matcher(reel())
        for text, has_author, is_own, expected in [
            ("арена", True, False, ("pending", "")),
            ("Хочу арену", True, False, ("pending", "")),
            ("арена", True, True, ("seen", "own")),
            ("арена", False, True, ("seen", "own")),
            ("привет", True, True, ("seen", "own")),
            ("арена", False, False, ("seen", "no_author")),
            ("привет", True, False, ("seen", "")),
            ("привет", False, False, ("seen", "")),
            (None, True, False, ("seen", "")),
            ("зачем арена?", True, False, ("seen", "near:question")),
            ("зачем арена?", False, False, ("seen", "near:question")),
            ("а б в г д арена", True, False, ("seen", "near:too_long")),
        ]:
            with self.subTest(text=text, has_author=has_author, is_own=is_own):
                self.assertEqual(match.decide(matcher, text, has_author, is_own), expected)


if __name__ == "__main__":
    unittest.main()
