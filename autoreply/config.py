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
class Opening:            # 14.2: the first DM as a button template
    text: str
    button: str


@dataclass(frozen=True)
class Gate:               # 14.2: the follow check after a tap
    text: str
    button: str
    profile_button: str
    again: str


@dataclass(frozen=True)
class Button:
    title: str
    url: str


@dataclass(frozen=True)
class Link:               # 14.4: the link message (after a tap)
    text: str
    buttons: tuple


@dataclass(frozen=True)
class Followup:
    text: str
    after_minutes: int


@dataclass(frozen=True)
class Variant:            # 14.2/14.3: one arm of the A/B test
    name: str
    weight: int
    opening: bool
    gate: bool
    text: str = None      # first-DM text of this variant when it has no button; None = the reel's text


@dataclass(frozen=True)
class Flow:
    variants: tuple
    opening: Opening = None
    gate: Gate = None
    link: Link = None
    followup: Followup = None

    def variant(self, media_id, user_id):
        """14.3: the variant of a person, weighted and stable: the same person under the same reel gets the same."""
        digest = int.from_bytes(hashlib.sha256(f"{media_id}:{user_id}".encode("utf-8")).digest()[:8], "big")
        point = digest % sum(v.weight for v in self.variants)
        for variant in self.variants:
            if point < variant.weight:
                return variant
            point -= variant.weight
        return self.variants[-1]


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
    flow: Flow = None     # 14: None = the reel of sections 1-13, one text DM


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
    click_base: str = ""  # 14.2: tracked links go through click_base + token; "" = links are sent as they are

    @property
    def reel_by_media(self):
        """Every configured reel, active or not, by media_id (database rows join the config by it)."""
        return {reel.media_id: reel for reel in self.reels}


def render(cfg, reel, late, variant=None):
    """The JSON `message` object of the DM. Pure. Only {greeting} and {link} are placeholders. The text goes out as
    written; only when it lacks the reel's link is the link added on a line of its own (preview/index.html shows
    the same). `variant` (14.3): a variant without a button may bring its own text."""
    greeting = cfg.late_greeting if late else cfg.greeting
    text = variant.text if variant is not None and variant.text else reel.text
    body = text.replace("{greeting}", greeting).replace("{link}", reel.link)
    return {"text": body if reel.link in body else f"{body}\n\n{reel.link}"}


TEMPLATE_TEXT_MAX_BYTES = 640    # Meta: the text of a button template


def _template(text, buttons):
    return {"attachment": {"type": "template", "payload": {
        "template_type": "button", "text": text, "buttons": list(buttons)}}}


def render_opening(cfg, reel, late, comment_id):
    """14.3: the first DM with the «Получить» button. Pure string work only (it runs between claim and POST)."""
    greeting = cfg.late_greeting if late else cfg.greeting
    opening = reel.flow.opening
    return _template(opening.text.replace("{greeting}", greeting),
                     [{"type": "postback", "title": opening.button, "payload": "g:" + comment_id}])


def link_buttons(reel):
    """14.4: [(title, url)] of the link message, untracked."""
    link = reel.flow.link if reel.flow else None
    return [(b.title, b.url) for b in link.buttons] if link else [("Открыть", reel.link)]


def render_link(reel, urls):
    """14.4 step `link`: `urls` are the URLs to put on the buttons (tracked or not), in the order of link_buttons."""
    link = reel.flow.link if reel.flow else None
    text = link.text if link else reel.name
    return _template(text, [{"type": "web_url", "title": title, "url": url}
                            for (title, _), url in zip(link_buttons(reel), urls)])


def render_prompt(cfg, reel, n, comment_id):
    """14.4 step `promptN`: ask for the follow; «Готово» comes back as payload d:<comment_id>."""
    gate = reel.flow.gate
    return _template(gate.text if n == 1 else gate.again, [
        {"type": "web_url", "title": gate.profile_button, "url": cfg.profile_url},
        {"type": "postback", "title": gate.button, "payload": "d:" + comment_id}])


def render_followup(reel):
    return {"text": reel.flow.followup.text}


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
    "flow": _is_a(dict, "must be an object"),   # optional (14.2); checked in depth by _flow
}
_ALERTS = {
    "repeat_min": _whole(1, WEEK_MINUTES),
    "digest_hour_utc": _whole(0, 23),
}


def _click_base(value):
    return None if _https(value) is None and value.endswith("/r/") else 'must be an https:// URL ending in "/r/"'


_TOP["click_base"] = _click_base   # optional (14.2)


def _title(value):
    return None if _filled_text(value) is None and len(value) <= 20 else "must be a text of 1 to 20 characters"


def _template_text(value):
    if _filled_text(value):
        return "must be a non-empty text"
    size = len(value.replace("{greeting}", "").encode("utf-8"))
    return None if size <= TEMPLATE_TEXT_MAX_BYTES else f"has {size} UTF-8 bytes, Meta's limit is {TEMPLATE_TEXT_MAX_BYTES}"


def _variant_name(value):
    return None if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{1,12}", value) else \
        "must be 1 to 12 latin letters or digits"


def _buttons(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        return "must be a list of 1 or 2 buttons"
    for item in value:
        if not isinstance(item, dict) or set(item) != {"title", "url"} or _title(item["title"]) or _https(item["url"]):
            return 'each button is {"title": 1-20 characters, "url": "https://…"}'
    return None


_FLOW_PARTS = {
    "opening": ({"text": _template_text, "button": _title}, Opening),
    "gate": ({"text": _template_text, "button": _title, "profile_button": _title, "again": _template_text}, Gate),
    "link": ({"text": _template_text, "buttons": _buttons}, Link),
    "followup": ({"text": _filled_text, "after_minutes": _whole(1, 1380)}, Followup),
}
_VARIANT = {"name": _variant_name, "weight": _whole(1, 100), "opening": _flag, "gate": _flag, "text": _filled_text}


def _flow(raw, where, problems, texts):
    """14.2: the Flow of a reel's `flow` object, or None with the problems appended. `texts` collects
    (where, text) of every text the bot will send, for the own-words rule of section 4."""
    before = len(problems)
    top = _check(raw, {**{key: _is_a(dict, "must be an object") for key in _FLOW_PARTS},
                       "variants": _is_a(list, "must be a list")}, where, problems, optional=tuple(_FLOW_PARTS))
    parts = {}
    for key, (rules, kind) in _FLOW_PARTS.items():
        if key in top:
            passed = _check(top[key], rules, f"{where}{key}.", problems)
            if len(passed) == len(rules):
                if kind is Link:
                    passed["buttons"] = tuple(Button(**b) for b in passed["buttons"])
                parts[key] = kind(**passed)
                for name in ("text", "again"):
                    if name in passed:
                        texts.append((f"{where}{key}.{name}", passed[name]))
    variants, names = [], set()
    if "variants" in top and not 1 <= len(top["variants"]) <= 4:
        problems.append(f"{where}variants: must hold 1 to 4 variants")
    for index, item in enumerate(top.get("variants", [])):
        at = f"{where}variants[{index}]"
        passed = _check(item, _VARIANT, at + ".", problems, optional=("text",))
        if len(passed) < len(_VARIANT) - 1:
            continue
        variant = Variant(**passed)
        if variant.name in names:
            problems.append(f"{at}.name: duplicate {variant.name!r}")
        names.add(variant.name)
        if variant.opening and "opening" not in parts:
            problems.append(f"{at}: opening is true, so flow.opening is needed")
        if variant.gate and not variant.opening:
            problems.append(f"{at}: gate needs opening: the follow can only be checked after a tap")
        if variant.gate and "gate" not in parts:
            problems.append(f"{at}: gate is true, so flow.gate is needed")
        if variant.text is not None and variant.opening:
            problems.append(f"{at}.text: a variant with the button sends flow.opening.text; leave text out")
        variants.append(variant)
    if len(problems) > before:
        return None
    return Flow(variants=tuple(variants), **parts)


def _check(obj, rules, where, problems, optional=()):
    """Check one JSON object against its rules: append every problem, return the values that passed.
    `optional`: keys that may be left out (only the additions of section 14 are)."""
    if not isinstance(obj, dict):
        problems.append(f"{where.rstrip('.') or 'config'}: must be an object")
        return {}
    problems.extend(f"{where}{key}: unknown key" for key in obj if key not in rules)
    passed = {}
    for key, rule in rules.items():
        if key not in obj:
            if key not in optional:
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
    top = _check(raw, _TOP, "", problems, optional=("click_base",))
    alerts = _check(top["alerts"], _ALERTS, "alerts.", problems) if "alerts" in top else {}

    reels, matchers, messages, flow_texts = [], [], [], []
    used = {"name": set(), "media_id": set()}
    for index, item in enumerate(top.get("reels", [])):
        where = f"reels[{index}]"
        passed = _check(item, _REEL, where + ".", problems, optional=("flow",))
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
        flow = _flow(item["flow"], where + ".flow.", problems, flow_texts) \
            if isinstance(item, dict) and item.get("flow") is not None else None
        if flow is not None and "link" in passed:
            for index_v, variant in enumerate(flow.variants):
                if variant.text is not None:
                    messages.append((f"{where}.flow.variants[{index_v}]",
                                     SimpleNamespace(**{**passed, "text": variant.text})))
        if len(passed) == len(_REEL) - ("flow" not in item):
            reels.append(Reel(**{**passed, "keywords": tuple(passed["keywords"]),
                                 "also_match": tuple(passed["also_match"]), "flow": flow}))

    def reject_own_words(where, text):
        for reel_where, matcher in matchers:
            if matcher.check(text)[0] == "match":
                problems.append(f"{where}: the matcher of {reel_where} would take the bot's own words for a request")

    for where, text in flow_texts:
        reject_own_words(where, text)

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
                     "alerts": Alerts(**alerts), "click_base": top.get("click_base", "")}), problems


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
    raw = asdict(cfg)
    if not raw["click_base"]:
        del raw["click_base"]   # a config without section 14 dumps (and hashes) as it did before
    for reel in raw["reels"]:
        if reel["flow"] is None:
            del reel["flow"]
        else:
            reel["flow"] = {key: value for key, value in reel["flow"].items() if value is not None}
            for variant in reel["flow"]["variants"]:
                if variant["text"] is None:
                    del variant["text"]
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


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
