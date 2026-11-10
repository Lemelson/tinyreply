"""The config file (SPEC section 4): load and validate once at start-up, render the DM.

load() reports every problem at once and returns a read-only Config, so nothing downstream has to
re-check a value. Every key is required: the file shows every product choice, the code hides no default.
"""
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit

import clock
import match

TEXT_MAX_BYTES = 1000    # Meta: a text message is at most 1000 bytes of UTF-8
WEEK_MINUTES = 7 * 24 * 60   # the longest `late_after_minutes` (no older comment is sent to) and `repeat_min`


def ig_api_base():
    return os.environ.get("IG_API_BASE") or "https://graph.instagram.com"


def tg_api_base():
    return os.environ.get("TG_API_BASE") or "https://api.telegram.org"


class ConfigError(Exception):
    def __init__(self, problems):
        # Invalid JSON keys may themselves contain lone surrogates; every CLI/log/alert needs safe text.
        self.problems = [str(problem).encode("utf-8", "backslashreplace").decode("utf-8") for problem in problems]
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Reel:
    name: str
    media_id: str
    permalink: str
    active: bool
    keywords: tuple
    also_match: tuple
    max_words: int
    link: str
    text: str


@dataclass(frozen=True)
class Alerts:
    repeat_min: int
    digest_hour_utc: int


@dataclass(frozen=True)
class Config:
    api_version: str
    sending: bool
    poll_interval_sec: int
    max_sends_per_hour: int
    public_reply: bool
    profile_url: str
    greeting: str
    late_greeting: str
    late_after_minutes: int
    public_phrases: tuple
    reels: tuple
    alerts: Alerts

    @property
    def reel_by_media(self):
        """Every configured reel, active or not, by media_id (database rows join the config by it)."""
        return {reel.media_id: reel for reel in self.reels}


def render(cfg, reel, late):
    """The JSON `message` object of the DM. Pure. Only {greeting} and {link} are placeholders."""
    greeting = cfg.late_greeting if late else cfg.greeting
    body = reel.text.replace("{greeting}", greeting).replace("{link}", reel.link)
    link_line = "" if reel.link in body else reel.link + "\n"
    return {"text": f"{body}\n\n{link_line}Подписаться: {cfg.profile_url}"}


# ---------- validation rules: rule(value) -> None, or what is wrong ----------

def _is_text(value):
    """A string that can go out as UTF-8 (JSON escapes can smuggle in a lone surrogate)."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _flag(value):
    return None if isinstance(value, bool) else "must be true or false"


def _whole(low, high=None):
    wanted = f"must be a whole number, {low} or more" if high is None else f"must be a whole number from {low} to {high}"

    def rule(value):
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= low and (high is None or value <= high)
        return None if ok else wanted
    return rule


def _text(value):
    return None if _is_text(value) else "must be a text"


def _filled_text(value):
    return None if _is_text(value) and value.strip() else "must be a non-empty text"


def _https(value):
    wanted = "must be an https:// URL"
    if not _is_text(value) or not value.startswith("https://") or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return wanted
    try:
        parsed = urlsplit(value)
        parsed.port   # raises for a nonnumeric or out-of-range port
        return None if parsed.scheme == "https" and parsed.hostname else wanted
    except ValueError:
        return wanted


def _permalink(value):
    return None if value == "" or _https(value) is None else 'must be "" or an https:// URL'


def _api_version(value):
    return None if isinstance(value, str) and re.fullmatch(r"v[0-9]+\.[0-9]+", value) else "must look like v26.0"


def _media_id(value):
    return None if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) else "must be a string of digits"


def _phrases(value):
    ok = isinstance(value, list) and all(_filled_text(item) is None for item in value)
    return None if ok else "must be a list of non-empty texts"


def _words(may_be_empty):
    def rule(value):
        if not isinstance(value, list) or not (value or may_be_empty):
            return "must be a list of words" if may_be_empty else "must be a non-empty list of words"
        bad = [item for item in value if not _is_text(item) or match.keyword_token(item) is None]
        if bad:
            return f"not one word of at least 2 letters: {bad!r}"
        # the matcher reads letters only: a digit would be dropped and "plan2025" would silently become "plan"
        digits = [item for item in value if any(ch.isdigit() for ch in item)]
        return f"a code word is letters only, a digit would be dropped: {digits!r}" if digits else None
    return rule


def _is_a(kind, wanted):
    return lambda value: None if isinstance(value, kind) else wanted


_TOP = {
    "api_version": _api_version,
    "sending": _flag,
    "poll_interval_sec": _whole(clock.T["poll_interval_min"], clock.T["poll_interval_max"]),
    "max_sends_per_hour": _whole(1, 300),
    "public_reply": _flag,
    "profile_url": _https,
    "greeting": _text,
    "late_greeting": _text,
    "late_after_minutes": _whole(0, WEEK_MINUTES),
    "public_phrases": _phrases,
    "reels": _is_a(list, "must be a list"),
    "alerts": _is_a(dict, "must be an object"),
}
_REEL = {
    "name": _filled_text,
    "media_id": _media_id,
    "permalink": _permalink,
    "active": _flag,
    "keywords": _words(may_be_empty=False),
    "also_match": _words(may_be_empty=True),
    "max_words": _whole(1),
    "link": _https,
    "text": _filled_text,
}
_ALERTS = {
    "repeat_min": _whole(1, WEEK_MINUTES),
    "digest_hour_utc": _whole(0, 23),
}


def _check(obj, rules, where, problems):
    """Check one JSON object against its rules: append every problem, return the values that passed."""
    if not isinstance(obj, dict):
        problems.append(f"{where.rstrip('.') or 'config'}: must be an object")
        return {}
    problems.extend(f"{where}{key}: unknown key" for key in obj if key not in rules)
    passed = {}
    for key, rule in rules.items():
        if key not in obj:
            problems.append(f"{where}{key}: missing")
        elif (error := rule(obj[key])):
            problems.append(f"{where}{key}: {error}")
        else:
            passed[key] = obj[key]
    return passed


def _unique_keys(pairs):
    # json keeps the last of two equal keys without a word; for "sending" that must not pass silently
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise ValueError(f"key written twice: {', '.join(repeated)}")
    return dict(pairs)


def _validate(raw):
    """(Config or None, problems). Each check runs on every part that passed the checks it builds on."""
    problems = []
    top = _check(raw, _TOP, "", problems)
    alerts = _check(top["alerts"], _ALERTS, "alerts.", problems) if "alerts" in top else {}

    reels, matchers, messages = [], [], []
    used = {"name": set(), "media_id": set()}
    for index, item in enumerate(top.get("reels", [])):
        where = f"reels[{index}]"
        passed = _check(item, _REEL, where + ".", problems)
        for key, values in used.items():
            if key in passed:
                if passed[key] in values:
                    problems.append(f"{where}.{key}: duplicate {passed[key]!r}")
                values.add(passed[key])
        if passed.keys() >= {"keywords", "also_match", "max_words"}:
            matcher = match.Matcher(SimpleNamespace(**passed))
            matchers.append((where, matcher))
            for key in ("keywords", "also_match"):
                # a word the reel can never accept (a question word of SPEC 5) would answer nobody, silently
                dead = [entry for entry in passed[key] if matcher.check(entry)[0] != "match"]
                if dead:
                    problems.append(f"{where}.{key}: a comment that is exactly this word is not taken for a "
                                    f"request by the reel's own matcher: {dead!r}")
        if passed.keys() >= {"text", "link"}:
            messages.append((where, SimpleNamespace(**passed)))
        if len(passed) == len(_REEL):
            reels.append(Reel(**{**passed, "keywords": tuple(passed["keywords"]),
                                 "also_match": tuple(passed["also_match"])}))

    def reject_own_words(where, text):
        for reel_where, matcher in matchers:
            if matcher.check(text)[0] == "match":
                problems.append(f"{where}: the matcher of {reel_where} would take the bot's own words for a request")

    if "public_phrases" in top:
        if top.get("public_reply") is True and len(set(top["public_phrases"])) < 2:
            problems.append("public_phrases: at least 2 different phrases are needed when public_reply is true")
        for index, phrase in enumerate(top["public_phrases"]):
            reject_own_words(f"public_phrases[{index}]", phrase)

    if top.keys() >= {"greeting", "late_greeting", "profile_url"}:
        texts = SimpleNamespace(**top)
        for where, reel in messages:
            for late in (False, True):
                text = render(texts, reel, late)["text"]
                if late and text == render(texts, reel, False)["text"]:
                    continue   # no {greeting} in the text: the same message was checked a moment ago
                about = f"{where}.text with {'late_greeting' if late else 'greeting'}"
                if len(text.encode("utf-8")) > TEXT_MAX_BYTES:
                    problems.append(f"{about}: the message has {len(text.encode('utf-8'))} UTF-8 bytes, "
                                    f"Meta's limit is {TEXT_MAX_BYTES}")
                reject_own_words(about, text)

    if problems:
        return None, problems
    return Config(**{**top, "public_phrases": tuple(top["public_phrases"]), "reels": tuple(reels),
                     "alerts": Alerts(**alerts)}), problems


def load(path):
    """Read and validate the config file. Raises ConfigError; its .problems lists everything that is wrong."""
    try:
        with open(path, encoding="utf-8") as file:
            return loads(file.read())
    except (OSError, ValueError) as error:
        raise ConfigError([f"{path}: {error}"]) from None


def loads(text):
    """The Config of a config file's text; ConfigError (ValueError if it is not JSON at all)."""
    cfg, problems = _validate(json.loads(text, object_pairs_hook=_unique_keys))
    if problems:
        raise ConfigError(problems)
    return cfg


def dump(cfg):
    """A Config as the text of a config file: loads(dump(cfg)) == cfg. The service stores the one it runs with
    (kv `config_running`), so that the report describes the service and not the file on disk (9)."""
    return json.dumps(asdict(cfg), ensure_ascii=False, sort_keys=True)


def fingerprint(cfg):
    """The hash of a Config that kv `config_hash` holds: a start with another config disarms (4)."""
    return hashlib.sha256(dump(cfg).encode("utf-8")).hexdigest()


def running(stored, stored_hash, given):
    """(the config the service runs with, whether `given` differs from it). `stored` and `stored_hash` are kv
    `config_running` and `config_hash`; without them (no service has started on this database yet) the given
    config is all there is."""
    try:
        loaded = loads(stored) if stored else None
    except (ValueError, ConfigError):
        loaded = None   # written by another version of the code
    return loaded or given, stored_hash is not None and stored_hash != fingerprint(given)
