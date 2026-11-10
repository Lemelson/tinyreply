#!/usr/bin/env python3
"""Strict stand-alone fake of Meta's Graph API for Instagram, plus Telegram sendMessage (SPEC.md section 11).

Written from the specification of Meta's behaviour (research/lessons/meta-api-facts.md), not from the bot's
code: it is the independent referee the real bot process is run against. It binds 127.0.0.1 only.
The scenario format, the fault fields and the control API are documented in sim/README.md.
"""
import argparse
import base64
import bisect
import json
import os
import random
import re
import signal
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

WINDOW_SEC = 7 * 86400            # private-reply window (meta-api-facts A13)
REFRESH_MIN_AGE_SEC = 86400       # a younger token cannot be refreshed (F3)
TOKEN_LIFETIME_SEC = 60 * 86400   # F2, F4
MAX_PAGE = 50                     # C2
TEXT_MAX_BYTES = 1000             # A8
TELEGRAM_MAX_CHARS = 4096

MODES = ("ok", "blocked", "token_dead", "dev_mode", "throttled", "messaging_blocked")
ALREADY_REPLIED = {  # the three spellings seen in the wild (A12): code, subcode, message
    "10900": (10900, None, "(#10900) Activity already replied to"),
    "2534025": (100, 2534025, "(#100) The comment is invalid for a private reply"),
    "-1": (-1, None, "The comment you are trying to reply to, already has a reply."),
}
_V = r"/v\d+\.\d+"
ROUTES = (  # kind, method, path pattern, parameters allowed besides access_token; the first matching path wins
    ("me", "GET", _V + r"/me", {"fields"}),
    ("my_media", "GET", _V + r"/me/media", {"fields", "limit", "after"}),
    ("media", "GET", _V + r"/([^/]+)", {"fields"}),
    ("comments", "GET", _V + r"/([^/]+)/comments", {"fields", "limit", "after"}),
    ("dm", "POST", _V + r"/([^/]+)/messages", set()),
    ("public", "POST", _V + r"/([^/]+)/replies", {"message"}),
    ("refresh", "GET", r"/refresh_access_token", {"grant_type"}),
    ("telegram", "POST", r"/bot([^/]+)/sendMessage",
     {"chat_id", "text", "parse_mode", "disable_web_page_preview", "link_preview_options", "disable_notification"}),
)
KINDS = tuple(route[0] for route in ROUTES)
EFFECT_KINDS = ("dm", "public", "refresh", "telegram")  # calls that change something when they take effect
MEDIA_FIELDS = {"id", "permalink", "shortcode", "comments_count", "timestamp"}
FIELDS = {  # kind -> (fields that may be requested, Meta's default when `fields` is absent)
    "me": ({"id", "user_id", "username", "followers_count", "media_count"}, "id"),
    "media": (MEDIA_FIELDS, "id"),
    "my_media": (MEDIA_FIELDS, "id"),
    "comments": ({"id", "text", "timestamp", "from", "hidden", "replies"}, "id,text,timestamp"),
}
REPLY_FIELDS = {"id", "from", "text", "timestamp"}
PAGE_DEFAULT = {"comments": 50, "my_media": 25}
ANSWER_KEYS = ("http", "code", "subcode", "message", "body", "omit_id")
FAULT_KEYS = ANSWER_KEYS + ("id", "target", "nth", "probability", "seed", "headers", "deliver", "consume",
                            "drop", "delay_real_sec", "refuse_real_sec")


class _Strict(Exception):
    """The request is not one of the calls of SPEC section 6."""


class _Out:
    """An answer: HTTP status, body (a JSON value, or a str that is sent verbatim) and headers.
    `effect` is what the call changes if it takes effect; `after_send` runs once the answer is written."""

    def __init__(self, status, body, headers=None, effect=None):
        self.status, self.body, self.headers, self.effect = status, body, headers or {}, effect
        self.after_send = None


class _Trigger:
    """Addressing shared by faults and holds: call kind, target id, then the nth matching call or a seeded
    probability. Every matching call is counted and draws from the generator whether or not it fires."""

    def __init__(self, spec):
        if spec["kind"] not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if ("probability" in spec) != ("seed" in spec) or ("probability" in spec and "nth" in spec):
            raise ValueError("give nth, or probability together with seed")
        self.kind = spec["kind"]
        self.target = None if spec.get("target") is None else str(spec["target"])
        nth = spec.get("nth")
        self.nth = None if nth is None else set(nth if isinstance(nth, list) else [nth])
        self.probability = spec.get("probability")
        # a typo must not produce a fault that silently never fires
        if self.nth is not None and not (self.nth and all(type(n) is int and n >= 1 for n in self.nth)):
            raise ValueError("nth must be a positive integer or a non-empty list of them")
        if self.probability is not None and not 0 <= self.probability <= 1:
            raise ValueError("probability must be between 0 and 1")
        self.random = random.Random(spec.get("seed"))
        self.count = 0

    def fires(self, kind, targets):
        if kind != self.kind or (self.target is not None and self.target not in targets):
            return False
        self.count += 1
        if self.nth is not None and self.count not in self.nth:
            return False
        return self.probability is None or self.random.random() < self.probability


def _keys(obj, where, required, optional=()):
    if not isinstance(obj, dict):
        raise ValueError(f"{where}: must be an object")
    missing, unknown = set(required) - set(obj), set(obj) - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f"{where}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}")


def _who(who, where):
    if who is None:
        return None
    _keys(who, where, ("id", "username"))
    return {"id": str(who["id"]), "username": who["username"]}


def _error(http, code, message, subcode=None, headers=None, kind="OAuthException"):
    error = {"message": message, "type": kind, "fbtrace_id": "FAKEMETA"}
    if code is not None:
        error["code"] = code
    if subcode is not None:
        error["error_subcode"] = subcode
    return _Out(http, {"error": error}, headers)


def _telegram_error(http, description, headers=None):
    return _Out(http, {"ok": False, "error_code": http, "description": description}, headers)


def _no_object(object_id, method):
    return _error(400, 100, f"Unsupported {method} request. Object with ID '{object_id}' does not exist, cannot "
                  "be loaded due to missing permissions, or does not support this operation", 33,
                  kind="GraphMethodException")


def _meta_time(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(ts))


def _position(item):
    return round(item["ts"] * 1000), item["serial"]


def _cursor(item):
    return base64.urlsafe_b64encode("{}:{}".format(*_position(item)).encode()).decode()


def _parse_body(raw):
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _route(method, path):
    """-> (kind, id captured from the path, allowed parameters, None) or (None, None, None, problem)."""
    for kind, route_method, pattern, allowed in ROUTES:
        match = re.fullmatch(pattern, path)
        if match:
            if method != route_method:
                return None, None, None, f"method {method} is not allowed on {path}"
            return kind, match.group(1) if match.groups() else None, allowed, None
    return None, None, None, f"unknown path {path} (the version prefix is required)"


def _parse_fields(spec, allowed):
    """'id,from,replies{id,from}' -> {'id': None, 'from': None, 'replies': ['id', 'from']}"""
    one = r"[a-z_]+(?:\{[a-z_,]+\})?"
    if not re.fullmatch(f"{one}(?:,{one})*", spec):
        raise _Strict(f"malformed fields {spec!r}")
    fields = {}
    for name, inner in re.findall(r"([a-z_]+)(?:\{([a-z_,]+)\})?", spec):
        inner = inner.split(",") if inner else None
        if name not in allowed or (inner and (name != "replies" or set(inner) - REPLY_FIELDS)):
            raise _Strict(f"field {name!r} of {spec!r} is not served here")
        fields[name] = inner
    return fields


def _message_refusal(message):
    """A shape other than the one of SPEC section 4 is a strict error; a broken limit is Meta's refusal."""
    if not (isinstance(message, dict) and set(message) == {"text"} and isinstance(message["text"], str)):
        raise _Strict("message is not {text}")
    if 1 <= len(message["text"].encode("utf-8", "surrogatepass")) <= TEXT_MAX_BYTES:
        return None
    return _error(400, 100, f"(#100) Length of param message[text] must be between 1 and {TEXT_MAX_BYTES} bytes")


class FakeMeta:
    """FakeMeta(scenario, scale, anchor).start() serves on two loopback ports until .stop().
    `api_base` is the value for IG_API_BASE and TG_API_BASE; `control(path, body)` is the control API."""

    def __init__(self, scenario, scale=1.0, anchor=None):
        self.scale = float(scale)
        self.anchor = time.time() if anchor is None else float(anchor)
        if not (0 < self.scale < float("inf") and abs(self.anchor) < float("inf")):
            raise ValueError("scale must be a positive number and anchor unix seconds")
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._seq = self._serial = self._generated = self._fault_count = self._finishing = 0
        self._used_ids = set()
        self._requests, self._dms, self._replies, self._telegram, self._events = [], [], [], [], []
        self._dm_times = []
        self._faults, self._holds = [], {}
        self._media, self._comments = {}, {}
        self._timers = []
        self._stopped = False
        self._api_server = self._control_server = None
        self._load(scenario)

    # ---- clock (same formula as the bot's clock.py, SPEC section 3)

    def now(self):
        return self._scaled(time.time())

    def _scaled(self, real):
        return self.anchor + (real - self.anchor) * self.scale

    # ---- scenario

    def _load(self, scenario):
        _keys(scenario, "scenario", ("account", "token", "media"),
              ("telegram", "mode", "already_replied", "limits", "faults"))
        account = scenario["account"]
        _keys(account, "account", ("user_id", "id", "username", "followers_count", "media_count"))
        self.account = {**account, "user_id": str(account["user_id"]), "id": str(account["id"])}
        self._set_token(scenario["token"])
        telegram = scenario.get("telegram", {})
        _keys(telegram, "telegram", (), ("token", "chat_id"))
        # None = not pinned by the scenario: any bot token / chat id is taken
        self._tg = {name: None if telegram.get(name) is None else str(telegram[name])
                    for name in ("token", "chat_id")}
        already = scenario.get("already_replied", {})
        _keys(already, "already_replied", (), ("spelling", "deliver"))
        self._already = {"spelling": already.get("spelling", "10900"), "deliver": already.get("deliver", False)}
        if self._already["spelling"] not in ALREADY_REPLIED:
            raise ValueError(f"already_replied.spelling must be one of {sorted(ALREADY_REPLIED)}")
        limits = scenario.get("limits", {})
        _keys(limits, "limits", (), ("cap_per_hour", "page_size"))
        self._cap = limits.get("cap_per_hour", 750)  # A19
        self._page_size = limits.get("page_size", MAX_PAGE)
        if type(self._cap) is not int or self._cap < 0 or type(self._page_size) is not int or self._page_size < 1:
            raise ValueError("limits: cap_per_hour must be an integer >= 0 and page_size an integer >= 1")
        self._set_mode(scenario.get("mode", "ok"))
        for media in scenario["media"]:
            _keys(media, "media", ("id", "permalink", "shortcode"), ("ts", "age_sec", "comments"))
            self._serial += 1
            media_id = str(media["id"])
            self._take_ids([media_id])
            self._media[media_id] = {"id": media_id, "permalink": media["permalink"], "shortcode": media["shortcode"],
                                     "ts": self._when(media, 30 * 86400), "serial": self._serial, "comments": []}
            for comment in media.get("comments", []):
                self._add_comment(media_id, comment)
        for fault in scenario.get("faults", []):
            self._add_fault(fault)

    def _set_token(self, spec):
        """The one valid token: a string, or {value, age_sec, expires_in_sec, rotate}."""
        spec = {"value": spec} if isinstance(spec, str) else spec
        _keys(spec, "token", ("value",), ("age_sec", "expires_in_sec", "rotate"))
        if not isinstance(spec["value"], str) or not spec["value"]:
            raise ValueError("token.value must be a non-empty string")
        now = self.now()
        self._token = {"base": spec["value"], "value": spec["value"], "refreshes": 0,
                       "rotate": spec.get("rotate", False), "issued_at": now - spec.get("age_sec", 0),
                       "expires_at": now + spec.get("expires_in_sec", TOKEN_LIFETIME_SEC)}

    def _when(self, spec, default_age=0):
        if "ts" in spec and "age_sec" in spec:
            raise ValueError("give ts or age_sec, not both")
        return float(spec["ts"]) if "ts" in spec else self.now() - spec.get("age_sec", default_age)

    def _take_ids(self, given):
        """Reserve the ids a scenario names. Media, comments and replies share one id space, as at Meta."""
        given = [str(one) for one in given]
        if len(set(given)) < len(given) or self._used_ids.intersection(given):
            raise ValueError(f"duplicate id among {given}")
        self._used_ids.update(given)

    def _new_id(self):
        while True:
            self._generated += 1
            candidate = str(17_900_000_000_000_000 + self._generated)
            if candidate not in self._used_ids:
                self._used_ids.add(candidate)
                return candidate

    def _add_comment(self, media_id, spec):
        _keys(spec, "comment", ("text", "from", "should_match"),
              ("id", "ts", "age_sec", "hidden", "replies", "private_replied", "listed", "raw"))
        if media_id not in self._media:
            raise ValueError(f"unknown media {media_id}")
        replies = spec.get("replies", [])
        for reply in replies:
            _keys(reply, "reply", ("text", "from"), ("id", "ts", "age_sec"))
        # Everything is checked before anything is changed: a refused comment leaves no trace.
        rows = [(str(row["id"]) if "id" in row else None, self._when(row), _who(row["from"], "from"), row["text"])
                for row in (spec, *replies)]
        self._take_ids(row_id for row_id, *_ in rows if row_id)
        (comment_id, ts, author, text), *replies = rows
        self._serial += 1
        comment = {"id": comment_id or self._new_id(), "media_id": media_id, "text": text, "ts": ts,
                   "serial": self._serial, "from": author, "hidden": spec.get("hidden", False),
                   "should_match": spec["should_match"], "private_replied": spec.get("private_replied", False),
                   "listed": spec.get("listed", True), "raw": spec.get("raw", {}), "deleted": False,
                   "first_listed_seq": None, "replies": []}
        for reply_id, reply_ts, reply_author, reply_text in replies:
            self._add_reply(comment, reply_id or self._new_id(), reply_text, reply_author, reply_ts, by_api=False)
        self._media[media_id]["comments"].append(comment)
        self._comments[comment["id"]] = comment
        return comment

    def _add_reply(self, comment, reply_id, text, who, ts, by_api):
        self._serial += 1
        reply = {"id": reply_id, "text": text, "from": who, "ts": ts, "serial": self._serial, "by_api": by_api}
        comment["replies"].append(reply)
        return reply

    def _own(self):
        return {"id": self.account["id"], "username": self.account["username"]}

    def _add_fault(self, spec):
        _keys(spec, "fault", ("kind",), FAULT_KEYS)
        kind, answers = spec["kind"], any(key in spec for key in ANSWER_KEYS)
        http, seconds = spec.get("http", 200), [spec.get(key, 0) for key in ("delay_real_sec", "refuse_real_sec")]
        problems = [text for bad, text in (
            # a mistyped value must be refused here, not blow up later inside a request and look like a fault
            (type(http) is not int or not 100 <= http <= 599, "http must be an HTTP status"),
            (any(type(sec) not in (int, float) or sec < 0 for sec in seconds), "delays are real seconds >= 0"),
            (not isinstance(spec.get("headers", {}), dict), "headers must be an object"),
            (spec.get("omit_id", True) is not True, "omit_id can only be true"),
            (any(not isinstance(spec.get(key, False), bool) for key in ("deliver", "consume")),
             "deliver and consume are true or false"),
            (spec.get("drop") not in (None, "before", "after"), "drop must be 'before' or 'after'"),
            ("drop" in spec and answers, "a dropped connection carries no answer"),
            (spec.get("drop") == "before" and (spec.get("deliver") or spec.get("consume")),
             "nothing takes effect when the connection is dropped before processing"),
            (("deliver" in spec or "consume" in spec) and kind not in EFFECT_KINDS,
             f"deliver/consume only apply to {EFFECT_KINDS}"),
            ("consume" in spec and kind != "dm", "consume only applies to dm"),
            ("omit_id" in spec and kind not in ("dm", "public"), "omit_id only applies to dm and public"),
            (kind == "telegram" and ("code" in spec or "subcode" in spec), "a telegram answer has no code/subcode"),
            # The safest reading of "delivery is independent of the answer": never guess it.
            (answers and kind in EFFECT_KINDS and not isinstance(spec.get("deliver"), bool),
             "deliver (true or false) is required when the fault replaces the answer of this kind of call"),
            (not answers and not set(spec) & {"headers", "deliver", "consume", "drop", "delay_real_sec",
                                              "refuse_real_sec"}, "the fault does nothing"),
        ) if bad]
        if problems:
            raise ValueError(f"fault {spec}: " + "; ".join(problems))
        # numbered over the whole run, so an id stays unique in the truth even after a clear
        fault = {**spec, "id": str(spec.get("id", f"f{self._fault_count + 1}")), "trigger": _Trigger(spec)}
        if any(fault["id"] == other["id"] for other in self._faults):
            raise ValueError(f"duplicate fault id {fault['id']}")
        self._fault_count += 1
        self._faults.append(fault)
        return fault["id"]

    def _set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self._event("mode", mode=mode)

    def _event(self, op, **detail):
        real = time.time()
        self._events.append({"after_seq": self._seq, "t_real_ms": int(real * 1000),
                             "t": round(self._scaled(real), 3), "op": op, **detail})

    # ---- servers

    def start(self):
        self._control_server = self._serve(0, _ControlHandler)
        self._api_server = self._serve(0, _ApiHandler)
        self.control_port = self._control_server.server_address[1]
        self.api_port = self._api_server.server_address[1]
        self.control_base = f"http://127.0.0.1:{self.control_port}"
        self.api_base = f"http://127.0.0.1:{self.api_port}"
        return self

    def _serve(self, port, handler):
        server = _Server(("127.0.0.1", port), type(handler.__name__, (handler,), {"fake": self}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return server

    def _close_api(self, real_sec):
        """Refuse connections: the listening socket is really closed, so a connect fails at once."""
        with self._lock:
            server, self._api_server = self._api_server, None
            self._event("listener", accept=False, real_sec=real_sec)
            timer = threading.Timer(real_sec, self._open_api)
            timer.daemon = True
            self._timers.append(timer)
        if server:
            server.shutdown()
            server.server_close()
        timer.start()

    def _open_api(self):
        while True:  # the port can be busy for a moment; the truth shows no `accept: true` until it is ours again
            with self._lock:
                if self._stopped or self._api_server is not None:
                    return
                try:
                    self._api_server = self._serve(self.api_port, _ApiHandler)
                    self._event("listener", accept=True)
                    return
                except OSError:
                    pass
            time.sleep(0.1)

    def stop(self):
        with self._lock:
            self._stopped = True
            servers = [self._api_server, self._control_server]
            self._api_server = None
            for timer in self._timers:
                timer.cancel()
            for hold in self._holds.values():
                hold["released"].set()
        for server in servers:
            if server:
                server.shutdown()
                server.server_close()

    # ---- the API port

    def _api(self, method, target, headers, raw):
        """Handle one request. -> (truth record, answer), the answer being None when the connection is dropped."""
        url = urlsplit(target)
        pairs = parse_qsl(url.query, keep_blank_values=True)
        req = SimpleNamespace(method=method, path=url.path, pairs=pairs, params=dict(pairs), headers=headers,
                              raw=raw, body=_parse_body(raw))
        with self._lock:
            real = time.time()
            self._seq += 1
            kind, arg, req.allowed, req.route_error = _route(method, url.path)
            if kind == "telegram":
                form = dict(parse_qsl(req.body)) if isinstance(req.body, str) else req.body
                req.data = {**req.params, **(form if isinstance(form, dict) else {})}
            targets = self._targets(kind, arg, req)
            token = self._token
            rec = req.rec = {
                "seq": self._seq, "monotonic": time.monotonic(), "t_real_ms": int(real * 1000), "t": round(self._scaled(real), 3),
                "method": method, "path": url.path, "params": req.params, "body": req.body,
                "kind": kind, "target": targets[0] if targets else None, "mode": self.mode,
                "token_ok": None if kind == "telegram" else (
                    req.params.get("access_token") == token["value"] and self.mode != "token_dead"
                    and self._scaled(real) < token["expires_at"]),
                "strict": None, "fault_id": None, "held": None,
                "status": None, "response": None, "delivered": False, "sent": None}
            self._requests.append(rec)
            if kind == "dm":
                self._dm_times.append(rec["t"])
            firing = [fault for fault in self._faults if fault["trigger"].fires(kind, targets)]
            fault = firing[0] if firing else {}
            rec["fault_id"] = fault.get("id")
            holds = [h for h in self._holds.values() if h["seq"] is None and not h["released"].is_set()
                     and h["trigger"].fires(kind, targets)]
            for hold in holds:
                hold["seq"] = rec["seq"]
        self._pause(holds, rec, "before")
        if fault.get("drop") == "before":
            with self._lock:
                self._finishing += 1
            return rec, None
        time.sleep(fault.get("delay_real_sec", 0))
        with self._lock:
            natural = self._answer(req, kind, arg)
            out = self._faulted(fault, kind, natural)
            if natural.effect and fault.get("deliver", True):
                natural.effect()
                rec["delivered"] = True
            if fault.get("consume") and rec["target"] in self._comments:
                self._comments[rec["target"]]["private_replied"] = True  # A6: a failed call can use up the one reply
            rec.update(status=out.status, response=out.body)
        self._pause(holds, rec, "after")
        if "refuse_real_sec" in fault:
            self._close_api(fault["refuse_real_sec"])  # before the answer is written: the next connect must fail
        with self._lock:
            self._finishing += 1
        return rec, None if fault.get("drop") == "after" else out

    def _targets(self, kind, arg, req):
        """Ids a fault or hold can address: comment id then its author's id; media id; Telegram chat id."""
        if kind in ("media", "comments"):
            return [arg]
        if kind == "telegram":
            return [str(req.data["chat_id"])] if "chat_id" in req.data else []
        if kind in ("dm", "public"):
            comment_id = arg
            if kind == "dm":
                recipient = req.body.get("recipient") if isinstance(req.body, dict) else None
                comment_id = recipient.get("comment_id") if isinstance(recipient, dict) else None
            if not isinstance(comment_id, str):
                return []
            comment = self._comments.get(comment_id)
            return [comment_id] + ([comment["from"]["id"]] if comment and comment["from"] else [])
        return []

    def _pause(self, holds, rec, phase):
        for hold in holds:
            if hold["phase"] == phase:
                with self._cond:
                    rec["held"] = phase
                    hold["paused"] = True
                    self._cond.notify_all()
                hold["released"].wait()

    def _finish(self, rec, out, sent):
        with self._cond:
            rec["sent"] = sent
            rec["completed_monotonic"] = time.monotonic()  # observation only, not a client claim boundary
            rec["completed_t"] = self._scaled(time.time())
            if sent and out.after_send:
                out.after_send()
            self._finishing -= 1
            self._cond.notify_all()

    def _faulted(self, fault, kind, natural):
        """The answer after the fault: the fault's own if it carries one, otherwise the natural one."""
        headers = fault.get("headers", {})
        if fault.get("omit_id"):
            kept = natural.body if natural.status == 200 else {}
            return _Out(200, {k: v for k, v in kept.items() if k not in ("message_id", "id")}, headers)
        if "body" in fault:
            return _Out(fault.get("http", 200), fault["body"], headers)
        if any(key in fault for key in ANSWER_KEYS):
            message = fault.get("message", "Injected fault")
            if kind == "telegram":
                return _telegram_error(fault.get("http", 400), message, headers)
            return _error(fault.get("http", 400), fault.get("code"), message, fault.get("subcode"), headers)
        natural.headers = {**natural.headers, **headers}
        return natural

    def _answer(self, req, kind, arg):
        """What the call gets without faults: strictness first, then the account mode, the token, Meta's rules."""
        try:
            self._strict(req, kind, arg)
        except _Strict as problem:
            req.rec["strict"] = str(problem)
            text = f"FAKE STRICT: {problem}"
            return _telegram_error(400, text) if kind == "telegram" else _error(400, 2500, text)
        if kind == "telegram":
            return self._h_telegram(req, arg)
        token = self._token
        if self.mode == "blocked":  # meta-api-facts I1: every call, HTTP 400, code 200, whatever the token
            return _error(400, 200, "API access blocked.", headers={
                "WWW-Authenticate": 'OAuth "Facebook Platform" "access_denied" "API access blocked."'})
        if req.params.get("access_token") != token["value"]:
            return _error(400, 190, "Invalid OAuth access token - Cannot parse access token")
        if self.now() >= token["expires_at"]:
            return _error(400, 190, "Error validating access token: Session has expired.", 463)
        if self.mode == "token_dead":
            return _error(400, 190, "Error validating access token: The session has been invalidated.", 467)
        if self.mode == "throttled":
            usage = {"call_count": 100, "total_cputime": 40, "total_time": 100}
            return _error(400, 4, "(#4) Application request limit reached", headers={
                # B5: real headers carry keys the documentation does not list
                "X-App-Usage": json.dumps({**usage, "call_volume": 0}),
                "X-Business-Use-Case-Usage": json.dumps({self.account["user_id"]: [
                    {**usage, "type": "instagram", "estimated_time_to_regain_access": 5, "ig_tier": "unknown"}]})})
        return getattr(self, "_h_" + kind)(req, arg)

    def _strict(self, req, kind, arg):
        if kind is None:
            raise _Strict(req.route_error)
        if len(req.pairs) != len(req.params):
            raise _Strict("a query parameter is repeated")
        unknown = set(req.data if kind == "telegram" else req.params) - req.allowed - {"access_token"}
        if unknown or (kind == "telegram" and "access_token" in req.data):
            raise _Strict(f"unknown parameter(s) {sorted(unknown) or ['access_token']}")
        if req.raw and kind not in ("dm", "telegram"):
            raise _Strict("unexpected request body: parameters belong in the query string")
        if kind in FIELDS:
            allowed, default = FIELDS[kind]
            req.fields = _parse_fields(req.params.get("fields", default), allowed)
        if kind in PAGE_DEFAULT:
            limit = req.params.get("limit", str(PAGE_DEFAULT[kind]))
            if not limit.isascii() or not limit.isdigit() or int(limit) < 1:
                raise _Strict("limit must be a positive integer")
            req.limit = min(int(limit), MAX_PAGE, self._page_size)
        if kind == "refresh" and req.params.get("grant_type") != "ig_refresh_token":
            raise _Strict("grant_type must be ig_refresh_token")
        if kind == "dm":
            body = req.body
            if not req.headers.get("Content-Type", "").startswith("application/json"):
                raise _Strict("POST /messages needs Content-Type: application/json")
            if not (isinstance(body, dict) and set(body) == {"recipient", "message"}
                    and isinstance(body["recipient"], dict) and set(body["recipient"]) == {"comment_id"}
                    and isinstance(body["recipient"]["comment_id"], str)):
                raise _Strict('the body must be exactly {"recipient": {"comment_id": "<id>"}, "message": {...}}')
            if arg != self.account["user_id"]:
                raise _Strict("/messages is served only under the professional user_id")
            _message_refusal(body["message"])

    def _h_me(self, req, _):
        return _Out(200, {name: self.account[name] for name in req.fields})

    def _render_media(self, media, fields):
        full = {"id": media["id"], "permalink": media["permalink"], "shortcode": media["shortcode"],
                "timestamp": _meta_time(media["ts"]),
                # C16: replies are counted, and so are comments Meta never lists (C13)
                "comments_count": sum(1 + len(c["replies"]) for c in media["comments"] if not c["deleted"])}
        return {name: full[name] for name in fields}

    def _h_media(self, req, media_id):
        if media_id not in self._media:
            return _no_object(media_id, "get")
        return _Out(200, self._render_media(self._media[media_id], req.fields))

    def _h_my_media(self, req, _):
        return self._page(req, self._media.values(), lambda media: self._render_media(media, req.fields))[0]

    def _h_comments(self, req, media_id):
        if media_id not in self._media:
            return _no_object(media_id, "get")
        if self.mode == "dev_mode":  # A24: an app in Development mode gets an empty list, with cursors
            cursor = _cursor({"ts": 0, "serial": 0})
            return _Out(200, {"data": [], "paging": {"cursors": {"before": cursor, "after": cursor}}})
        visible = [c for c in self._media[media_id]["comments"] if c["listed"] and not c["deleted"]]
        out, page = self._page(req, visible, lambda comment: self._render_comment(comment, req.fields))
        seq = req.rec["seq"]

        def mark_listed():  # only a listing that was really sent counts as "Meta listed it"
            for comment in page:
                if comment["first_listed_seq"] is None:
                    comment["first_listed_seq"] = seq
        out.after_send = mark_listed
        return out

    def _render_comment(self, comment, fields):
        full = {"id": comment["id"], "text": comment["text"], "timestamp": _meta_time(comment["ts"]),
                "hidden": comment["hidden"], "from": comment["from"]}
        if comment["replies"] and "replies" in fields:
            wanted = fields["replies"] or ["id", "text", "timestamp"]
            rows = [{"id": r["id"], "from": r["from"], "text": r["text"], "timestamp": _meta_time(r["ts"])}
                    for r in sorted(comment["replies"], key=_position, reverse=True)]
            full["replies"] = {"data": [{k: row[k] for k in wanted if row[k] is not None} for row in rows]}
        full.update(comment["raw"])
        return {name: full[name] for name in fields if full.get(name) is not None}

    def _page(self, req, items, render):
        """One page, newest first -> (answer, items on the page). A cursor is a position (time, serial), not
        an offset, so comments added or deleted between two page reads never repeat or skip older ones."""
        items = sorted(items, key=_position, reverse=True)
        after = req.params.get("after")
        if after is not None:
            try:
                ms, serial = base64.urlsafe_b64decode(after).decode().split(":")
                edge = (int(ms), int(serial))
            except ValueError:
                return _error(400, 100, "(#100) Invalid cursor"), []
            items = [item for item in items if _position(item) < edge]
        page = items[:req.limit]
        body = {"data": [render(item) for item in page]}
        if page:
            last = _cursor(page[-1])
            body["paging"] = {"cursors": {"before": _cursor(page[0]), "after": last}}
            if len(items) > len(page):
                body["paging"]["next"] = f"{self.api_base}{req.path}?{urlencode({**req.params, 'after': last})}"
        return _Out(200, body), page

    def _h_dm(self, req, _):
        comment_id, message = req.body["recipient"]["comment_id"], req.body["message"]
        if self.mode == "messaging_blocked":
            return _error(400, 100, "(#100) This account is blocked from using the Instagram messaging API", 2534029)
        calls_in_hour = len(self._dm_times) - bisect.bisect_right(self._dm_times, req.rec["t"] - 3600)
        if calls_in_hour > self._cap:
            return _error(400, 613, "(#613) Calls to this api have exceeded the rate limit.", 2534040)
        refusal = _message_refusal(message)
        if refusal:
            return refusal
        comment = self._comments.get(comment_id)
        if comment is None or comment["deleted"]:
            return _no_object(comment_id, "post")
        seq, author = req.rec["seq"], comment["from"]["id"] if comment["from"] else None

        def deliver():
            comment["private_replied"] = True
            self._dms.append({"seq": seq, "t": round(self.now(), 3), "comment_id": comment_id,
                              "media_id": comment["media_id"], "user_id": author, "message": message})
        if comment["private_replied"]:  # A11: one private reply per comment
            code, subcode, text = ALREADY_REPLIED[self._already["spelling"]]
            out = _error(400, code, text, subcode)
            out.effect = deliver if self._already["deliver"] else None
            return out
        if self.now() - comment["ts"] >= WINDOW_SEC:
            return _error(400, 10, "(#10) This message is sent outside of allowed window.", 2534022)
        return _Out(200, {"recipient_id": author or "0", "message_id": f"mid.{seq}"}, effect=deliver)

    def _h_public(self, req, comment_id):
        text = req.params.get("message")
        if not text:
            return _error(400, 100, "(#100) The parameter message is required")
        comment = self._comments.get(comment_id)
        if comment is None or comment["deleted"]:
            return _no_object(comment_id, "post")
        if comment["hidden"]:  # D2; the code Meta uses for it is not documented
            return _error(400, 100, "(#100) A hidden comment cannot be replied to")
        reply_id, seq = self._new_id(), req.rec["seq"]

        def deliver():
            self._add_reply(comment, reply_id, text, self._own(), self.now(), by_api=True)
            self._replies.append({"seq": seq, "t": round(self.now(), 3), "comment_id": comment_id, "id": reply_id,
                                  "text": text})
        return _Out(200, {"id": reply_id}, effect=deliver)

    def _h_refresh(self, req, _):
        token = self._token
        if self.now() - token["issued_at"] < REFRESH_MIN_AGE_SEC:  # F3; the error shape is not documented (F7)
            return _error(400, 100, "(#100) The access token is too young to be refreshed")
        refreshes = token["refreshes"] + 1
        value = f"{token['base']}.r{refreshes}" if token["rotate"] else token["value"]

        def renew():
            token.update(value=value, refreshes=refreshes, issued_at=self.now(),
                         expires_at=self.now() + TOKEN_LIFETIME_SEC)
        return _Out(200, {"access_token": value, "token_type": "bearer", "expires_in": TOKEN_LIFETIME_SEC},
                    effect=renew)

    def _h_telegram(self, req, bot_token):
        chat_id, text = req.data.get("chat_id"), req.data.get("text")
        if self._tg["token"] not in (None, bot_token):
            return _telegram_error(401, "Unauthorized")
        if chat_id is None or self._tg["chat_id"] not in (None, str(chat_id)):
            return _telegram_error(400, "Bad Request: chat not found")
        if not isinstance(text, str) or not text.strip():
            return _telegram_error(400, "Bad Request: message text is empty")
        if len(text) > TELEGRAM_MAX_CHARS:
            return _telegram_error(400, "Bad Request: message is too long")
        seq = req.rec["seq"]

        def deliver():
            self._telegram.append({"seq": seq, "t": round(self.now(), 3), "chat_id": str(chat_id), "text": text})
        return _Out(200, {"ok": True, "result": {"message_id": seq, "date": int(self.now()),
                                                 "chat": {"id": chat_id, "type": "private"}, "text": text}},
                    effect=deliver)

    # ---- the control port

    def control(self, path, body=None, query=None):
        """The control API in-process: `body` None is a GET, otherwise the POST body. Raises ValueError or
        KeyError where the control port answers HTTP 400."""
        with self._cond:
            if body is None:  # a client that has its answer must find that request complete in the truth
                self._cond.wait_for(lambda: not self._finishing, 1)
            if path == "/truth" and body is None:
                since = max(int((query or {}).get("since", 0)), 0)

                def later(rows):
                    return [dict(row) for row in rows if row["seq"] > since]
                return {"seq": self._seq, "mode": self.mode,
                        "requests": [dict(rec) for rec in self._requests[since:]], "dms": later(self._dms),
                        "replies": later(self._replies), "telegram": later(self._telegram),
                        "events": [dict(event) for event in self._events]}
            if path == "/clock" and body is None:
                real = time.time()
                return {"now": self._scaled(real), "real": real, "scale": self.scale, "anchor": self.anchor}
            if path == "/comments" and body is None:
                return {"comments": [self._comment_state(comment) for comment in self._comments.values()]}
            if path == "/mode":
                _keys(body, path, ("mode",))
                self._set_mode(body["mode"])
                return {"mode": self.mode}
            if path == "/token":
                self._set_token(body)
                self._event("token")
                return {"expires_at": self._token["expires_at"]}
            if path == "/faults":
                _keys(body, path, (), ("add", "clear"))
                before = list(self._faults), self._fault_count
                try:
                    if body.get("clear"):
                        self._faults.clear()
                    added = body.get("add", [])
                    ids = [self._add_fault(spec) for spec in (added if isinstance(added, list) else [added])]
                except Exception:  # all or nothing: a refused call must not leave half of itself behind
                    self._faults[:], self._fault_count = before
                    raise
                self._event("faults", cleared=bool(body.get("clear")), added=ids)
                return {"faults": [fault["id"] for fault in self._faults]}
            if path == "/comments":
                _keys(body, path, (), ("add", "delete", "hide", "owner_reply", "text"))
                ops = [op for op in ("add", "delete", "hide", "owner_reply") if op in body]
                if len(ops) != 1 or ("text" in body and ops != ["owner_reply"]):
                    raise ValueError("give exactly one of add, delete, hide, owner_reply (+ text)")
                answer = {}
                if ops == ["add"]:
                    spec = dict(body["add"])
                    comment = self._add_comment(str(spec.pop("media_id")), spec)
                else:
                    comment = self._comments[str(body[ops[0]])]
                    if ops == ["delete"]:
                        comment["deleted"] = True
                    elif ops == ["hide"]:
                        comment["hidden"] = True
                    else:
                        answer["reply_id"] = self._add_reply(comment, self._new_id(), body.get("text", ""),
                                                             self._own(), self.now(), by_api=False)["id"]
                self._event("comment_" + ops[0], id=comment["id"])
                return {"id": comment["id"], **answer}
            if path == "/hold":
                _keys(body, path, (), ("add", "wait", "release", "timeout_real_sec"))
                if "add" in body:
                    spec = body["add"]
                    _keys(spec, "hold", ("kind", "phase"), ("target", "nth"))
                    if spec["phase"] not in ("before", "after"):
                        raise ValueError("phase must be 'before' or 'after'")
                    hold_id = f"h{len(self._holds) + 1}"
                    self._holds[hold_id] = {"trigger": _Trigger(spec), "phase": spec["phase"], "seq": None,
                                            "paused": False, "released": threading.Event()}
                    return {"id": hold_id}
                if "wait" in body:
                    hold = self._holds[body["wait"]]
                    self._cond.wait_for(lambda: hold["paused"], body.get("timeout_real_sec", 10))
                    return {"caught": hold["paused"], "seq": hold["seq"] if hold["paused"] else None}
                self._holds[body["release"]]["released"].set()
                return {"released": body["release"]}
            raise ValueError(f"unknown control call {'GET' if body is None else 'POST'} {path}")

    def _comment_state(self, comment):
        def public(row):
            who = row["from"]
            own = bool(who) and (who["id"] in (self.account["user_id"], self.account["id"])
                                 or who["username"] == self.account["username"])
            return {**{k: v for k, v in row.items() if k not in ("serial", "replies")}, "own": own}
        return {**public(comment), "replies": [public(reply) for reply in comment["replies"]]}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_port = False  # two fakes must never share a port
    request_queue_size = 64

    def server_bind(self):  # HTTPServer.server_bind adds a reverse DNS lookup that can take seconds
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Handler(BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *args):
        pass

    def _read(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _send(self, status, body, headers):
        raw = isinstance(body, str)
        data = body.encode("utf-8", "replace") if raw else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if raw else "application/json; charset=UTF-8")
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except OSError:  # the client is gone, for example killed while its request was held
            return False


class _ApiHandler(_Handler):
    def _handle(self):
        rec, out = self.fake._api(self.command, self.path, self.headers, self._read())
        self.fake._finish(rec, out, out is not None and self._send(out.status, out.body, out.headers))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle


class _ControlHandler(_Handler):
    def _handle(self):
        url = urlsplit(self.path)
        raw = self._read()
        try:
            body = json.loads(raw) if self.command == "POST" else None
            status, answer = 200, self.fake.control(url.path, body, dict(parse_qsl(url.query)))
        except (ValueError, KeyError, TypeError, AttributeError) as problem:
            status, answer = 400, {"error": f"{type(problem).__name__}: {problem}"}
        self._send(status, answer, {})

    do_GET = do_POST = _handle


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", required=True, help="scenario JSON file (see sim/README.md)")
    parser.add_argument("--scale", type=float, default=float(os.environ.get("CLOCK_SCALE") or 1),
                        help="clock speed-up, default $CLOCK_SCALE or 1")
    parser.add_argument("--anchor", type=float, default=float(os.environ.get("CLOCK_ANCHOR") or time.time()),
                        help="unix time at which the scaled clock equals real time, default $CLOCK_ANCHOR or now")
    args = parser.parse_args()
    with open(args.scenario, encoding="utf-8") as file:
        fake = FakeMeta(json.load(file), args.scale, args.anchor).start()
    print(json.dumps({"api": fake.api_port, "control": fake.control_port}), flush=True)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    stop.wait()
    fake.stop()


if __name__ == "__main__":
    main()
