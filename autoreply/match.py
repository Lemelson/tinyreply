"""Keyword matcher (SPEC section 5). Pure: no I/O, no clock; never raises on a comment text.

There is no fuzzy rule: a typo is matched only when a human added it to the reel's `also_match`
(v1's one-letter typo rule sent DMs to people who wrote the names «Арина», «Ирена», «Алёна»: issue K5).
"""
import re
import unicodedata
from itertools import groupby

_MENTION = re.compile(r"@\S+")
_LATIN = re.compile(r"[a-z]")
_LATIN_TO_CYRILLIC = str.maketrans("aeopcxykmhtb", "аеорсхукмнтв")
_CASE_ENDINGS = ("у", "ы", "е", "ой")   # for a keyword ending in «а»: арена -> арену, арены, арене, ареной
# SPEC names `?`; the full-width, doubled, Arabic and emoji question marks ask a question just the same
_QUESTION_MARKS = frozenset("?？⁇⁈⁉؟❓❔")


def _collapse(letters):
    return "".join(letter for letter, _ in groupby(letters))


def normalize(text):
    """Tokens of a text: lower-case runs of letters, ё as е, repeated letters collapsed, @mentions dropped."""
    if not isinstance(text, str):
        return []
    text = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    text = _MENTION.sub(" ", text)
    return [_collapse(run) for is_letter, run in groupby(text, str.isalpha) if is_letter]


def skeleton(token):
    """Latin look-alike letters as Cyrillic («Aрена» with a Latin A). Not a transliteration."""
    return token.translate(_LATIN_TO_CYRILLIC)


def keyword_token(entry):
    """The token a config keyword stands for, or None if it is not exactly one token of at least 2 letters."""
    tokens = normalize(entry)
    return tokens[0] if len(tokens) == 1 and len(tokens[0]) >= 2 else None


QUESTION_WORDS = frozenset(keyword_token(word) for word in (
    "зачем", "почему", "как", "где", "когда", "сколько", "какой", "какая", "какие", "что", "чё", "че",
    "нафига", "нахрена"))


class Matcher:
    def __init__(self, reel):
        forms = set()
        for keyword in reel.keywords:
            token = keyword_token(keyword)   # config.load lets nothing through that is not one word
            forms.add(token)
            if token.endswith("а"):
                forms.update(token[:-1] + ending for ending in _CASE_ENDINGS)
        forms.update(keyword_token(entry) for entry in reel.also_match)
        self._forms = frozenset(forms)
        self._skeletons = frozenset(skeleton(form) for form in forms)
        self._max_words = reel.max_words

    def _hits(self, token):
        """The token is a form; or reads as one once its Latin look-alikes are Cyrillic («Aрена» with a Latin A);
        or it has a Latin letter and reads like a Latin form («bоt» with a Cyrillic о).

        A token without a Latin letter never hits through the skeleton of a form: the keyword `bot` has the
        skeleton «вот», and «вот» is another word (v1 issue K5).
        """
        if token in self._forms:
            return True
        bones = skeleton(token)
        return bones in self._forms or (_LATIN.search(token) is not None and bones in self._skeletons)

    def check(self, text):
        """('match', '') | ('near', 'too_long' | 'question') | ('none', '')"""
        tokens = normalize(text)
        if not any(self._hits(token) for token in tokens):
            return "none", ""
        if len(tokens) > self._max_words:
            return "near", "too_long"
        # by skeleton: «Kак арена» with a Latin K is as much a question as «Aрена» is a request
        if not QUESTION_WORDS.isdisjoint(skeleton(token) for token in tokens) \
                or (len(tokens) > 1 and not _QUESTION_MARKS.isdisjoint(text)):
            return "near", "question"
        return "match", ""


def decide(matcher, text, has_author, is_own):
    """(status, reason) of a comment that has no DM history: 'pending' or 'seen' with the reason."""
    if is_own:
        return "seen", "own"
    kind, reason = matcher.check(text)
    if kind == "near":
        return "seen", "near:" + reason
    if kind != "match":
        return "seen", ""
    if not has_author:
        return "seen", "no_author"
    return "pending", ""
