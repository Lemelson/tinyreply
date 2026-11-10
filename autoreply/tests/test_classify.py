"""meta.classify(): one row per line of both rule lists of SPEC 6.1, in order, and a fuzz test.

Run: python3.12 -B -m unittest discover -s tests -t . -p 'test_classify.py'
"""
import random
import unittest

import meta

ALREADY = "The comment you are trying to reply to, already has a reply."
UNSUPPORTED = "Unsupported request - method type: post"


def err(code, subcode=None, message="x"):
    error = {"message": message, "type": "OAuthException", "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    return {"error": error}


# Rule 10 of the POST list, which is rule 4 of the GET list: (error body, reason).
ACCOUNT_WIDE = [
    (err(190), "auth"), (err(190, 463), "auth"),
    (err(368), "spam"), (err(368, 1404169), "spam"),
    (err(613, 2018338), "spam"),                       # before plain 613 = rate
    (err(4, 2207051), "spam"),                         # before plain 4 = rate
    (err(508), "spam"), (err(508, 2534122), "spam"),
    (err(10, 1893063), "spam"),                        # before plain 10 = blocked
    (err(4), "rate"), (err(17), "rate"), (err(32), "rate"), (err(341), "rate"),
    (err(613), "rate"), (err(613, 2534040), "rate"),
    (err(80000), "rate"), (err(80002), "rate"), (err(80099), "rate"),
    (err(3), "blocked"), (err(10), "blocked"), (err(10, 2534077), "blocked"),
    (err(25), "blocked"), (err(25, 2207050), "blocked"),
    (err(200, message="API access blocked."), "blocked"), (err(200, 2534041), "blocked"), (err(299), "blocked"),
    (err(100, 2534029), "blocked"),
    (err(100, message=UNSUPPORTED), "blocked"),
]

# (kind, http, body, connect_failed, outcome, reason)
POST_ROWS = [
    # 1. connect_failed
    ("dm", 0, None, True, "not_sent", "connect"),
    ("public", 0, None, True, "not_sent", "connect"),
    # 2. no HTTP response
    ("dm", 0, None, False, "uncertain", "no_response"),
    ("public", 0, None, False, "uncertain", "no_response"),
    # 3. HTTP >= 500, whatever the body says
    ("dm", 500, err(190), False, "uncertain", "http_5xx"),
    ("dm", 500, err(551), False, "uncertain", "http_5xx"),
    ("dm", 503, {"message_id": "m1"}, False, "uncertain", "http_5xx"),
    ("public", 502, None, False, "uncertain", "http_5xx"),
    # 4. HTTP 429, whatever the body is
    ("dm", 429, None, False, "global", "rate"),
    ("dm", 429, err(551), False, "global", "rate"),
    ("public", 429, "<html>", False, "global", "rate"),
    # 5. HTTP 2xx: with the id field ok, without it uncertain
    ("dm", 200, {"recipient_id": "U1", "message_id": "m1"}, False, "ok", ""),
    ("dm", 201, {"message_id": "m1"}, False, "ok", ""),
    ("public", 200, {"id": "17890"}, False, "ok", ""),
    ("dm", 200, {"recipient_id": "U1"}, False, "uncertain", "ok_without_id"),
    ("dm", 200, {"message_id": ""}, False, "uncertain", "ok_without_id"),
    ("dm", 200, {"message_id": None}, False, "uncertain", "ok_without_id"),
    ("dm", 200, {"id": "17890"}, False, "uncertain", "ok_without_id"),           # the other kind's field
    ("public", 200, {"message_id": "m1"}, False, "uncertain", "ok_without_id"),
    ("public", 200, {}, False, "uncertain", "ok_without_id"),
    ("dm", 200, None, False, "uncertain", "ok_without_id"),
    ("dm", 200, ["m1"], False, "uncertain", "ok_without_id"),
    ("dm", 200, err(190), False, "uncertain", "ok_without_id"),                  # not rule 10
    # 6. no integer error.code
    ("dm", 400, None, False, "uncertain", "unparseable"),
    ("dm", 400, "<html>", False, "uncertain", "unparseable"),
    ("dm", 400, [err(551)], False, "uncertain", "unparseable"),
    ("dm", 400, {"error": {"message": ALREADY}}, False, "uncertain", "unparseable"),
    ("dm", 400, {"error": {"code": "abc"}}, False, "uncertain", "unparseable"),
    ("dm", 400, {"error": "invalid_token"}, False, "uncertain", "unparseable"),
    ("dm", 400, {"error": {"code": 190.7}}, False, "uncertain", "unparseable"),   # not 190 = auth, which is resent
    ("dm", 400, {"error": {"code": "190.0"}}, False, "uncertain", "unparseable"),
    ("dm", 400, {"error": {"code": float("nan")}}, False, "uncertain", "unparseable"),
    ("dm", 400, {"error": {"code": 2 ** 63}}, False, "uncertain", "unparseable"),  # the journal could not store it
    ("public", 302, None, False, "uncertain", "unparseable"),
    # 7. already replied, any spelling
    ("dm", 400, err(10900), False, "rejected", "already_replied"),
    ("dm", 400, err(100, 2534025), False, "rejected", "already_replied"),
    ("dm", 400, err(-1, message=ALREADY), False, "rejected", "already_replied"),  # before rule 8
    ("dm", 400, err(1, message="Activity ALREADY REPLIED to"), False, "rejected", "already_replied"),
    # 8. codes 1, -1, 2
    ("dm", 400, err(1), False, "uncertain", "code_1"),
    ("dm", 400, err(1, 1545120), False, "uncertain", "code_1"),
    ("dm", 400, err(-1), False, "uncertain", "code_-1"),
    ("dm", 400, err(2), False, "uncertain", "code_2"),
    ("dm", 400, err(2, 1545133), False, "uncertain", "code_2"),
    ("public", 400, err(2), False, "uncertain", "code_2"),
    # 9. per-item, HTTP 4xx: user_unavailable
    ("dm", 400, err(551), False, "rejected", "user_unavailable"),
    ("dm", 400, err(551, 1545041), False, "rejected", "user_unavailable"),
    ("dm", 400, err(200, 1545041), False, "rejected", "user_unavailable"),       # before 200..299 = blocked
    ("dm", 400, err(10903), False, "rejected", "user_unavailable"),
    ("dm", 400, err(10903, 1893049), False, "rejected", "user_unavailable"),
    ("dm", 403, err(230), False, "rejected", "user_unavailable"),                # before 200..299 = blocked
    ("dm", 400, err(100, 2018001), False, "rejected", "user_unavailable"),
    ("dm", 400, err(100, 2534014), False, "rejected", "user_unavailable"),
    ("dm", 400, err(100, 2534001), False, "rejected", "user_unavailable"),
    ("dm", 400, err(10, 2018108), False, "rejected", "user_unavailable"),        # before plain 10 = blocked
    ("dm", 400, err(10, 1893015), False, "rejected", "user_unavailable"),
    # 9. comment_gone
    ("dm", 400, err(100, 33), False, "rejected", "comment_gone"),
    ("public", 400, err(100, 33), False, "rejected", "comment_gone"),
    ("dm", 404, err(24), False, "rejected", "comment_gone"),
    ("dm", 400, err(24, 2207006), False, "rejected", "comment_gone"),
    ("dm", 404, err(803), False, "rejected", "comment_gone"),
    # 9. outside_window
    ("dm", 400, err(10, 2534022), False, "rejected", "outside_window"),
    ("dm", 400, err(10, 2018278), False, "rejected", "outside_window"),
    ("dm", 400, err(10, 2018065), False, "rejected", "outside_window"),
    # 9. invalid
    ("dm", 400, err(100, 2534015), False, "rejected", "invalid"),
    ("dm", 400, err(100, 2534037), False, "rejected", "invalid"),
    ("dm", 400, err(10, 2018300), False, "rejected", "invalid"),
    # 9. kind dm only: code 20
    ("dm", 400, err(20), False, "rejected", "invalid"),
    ("dm", 400, err(20, 1772107), False, "rejected", "invalid"),
    # 9 needs HTTP 4xx: the same codes under another status are not a refusal of the item
    ("dm", 302, err(551), False, "uncertain", "unknown_code_551"),
    ("dm", 302, err(20), False, "uncertain", "unknown_code_20"),
    # 10. account-wide: kind public with code 20
    ("public", 400, err(20), False, "global", "spam"),
    ("public", 400, err(20, 1772107), False, "global", "spam"),
    # 10. the rest of the account-wide table
    *[(kind, 400, body, False, "global", reason) for body, reason in ACCOUNT_WIDE for kind in ("dm", "public")],
    # 11. any other code 100
    ("dm", 400, err(100), False, "rejected", "invalid"),
    ("dm", 400, err(100, 99999), False, "rejected", "invalid"),
    ("dm", 400, err(100, 99999, UNSUPPORTED), False, "rejected", "invalid"),     # blocked only without a subcode
    ("dm", 400, err(100, message="unsupported REQUEST"), False, "rejected", "invalid"),
    ("public", 400, err(100), False, "rejected", "invalid"),
    # 12. anything else
    ("dm", 400, err(999), False, "uncertain", "unknown_code_999"),
    ("dm", 400, err(999, message=UNSUPPORTED), False, "uncertain", "unknown_code_999"),  # blocked only as code 100
    ("dm", 400, err(79999), False, "uncertain", "unknown_code_79999"),
    ("dm", 400, err(80100), False, "uncertain", "unknown_code_80100"),
    ("dm", 400, err(300), False, "uncertain", "unknown_code_300"),
    ("dm", 400, err(199), False, "uncertain", "unknown_code_199"),
    ("public", 400, err(0), False, "uncertain", "unknown_code_0"),
    # codes arrive as strings; the subcode under either key
    ("dm", 400, {"error": {"code": "190"}}, False, "global", "auth"),
    ("dm", 400, {"error": {"code": "100", "error_subcode": "2534025"}}, False, "rejected", "already_replied"),
    ("dm", 400, {"error": {"code": 100, "subcode": 33}}, False, "rejected", "comment_gone"),
    ("dm", 400, {"error": {"code": 10.0, "error_subcode": 2534022.0}}, False, "rejected", "outside_window"),
    ("dm", 400, {"error": {"code": True}}, False, "uncertain", "unparseable"),
    ("dm", 400, {"error": {"code": 10, "error_subcode": 2534022.5}}, False, "global", "blocked"),  # as without subcode
    ("dm", 400, {"error": {"code": -2 ** 63}}, False, "uncertain", f"unknown_code_{-2 ** 63}"),
    # a status contradicts "could not connect": never not_sent
    ("dm", 200, {}, True, "uncertain", "ok_without_id"),
    ("dm", 500, None, True, "uncertain", "http_5xx"),
    ("dm", 400, err(1), True, "uncertain", "code_1"),
]

GET_ROWS = [
    # 1. connect_failed, no response, HTTP >= 500, codes 1, -1, 2
    ("read_me", 0, None, True, "transient", ""),
    ("read_comments", 0, None, False, "transient", ""),
    ("read_media", 500, err(190), False, "transient", ""),
    ("refresh", 503, None, False, "transient", ""),
    ("read_me", 400, err(1), False, "transient", ""),
    ("read_me", 400, err(-1), False, "transient", ""),
    ("read_me", 400, err(2), False, "transient", ""),
    ("read_comments", 200, err(2), False, "transient", ""),
    ("read_comments", 429, err(2), False, "transient", ""),                      # before rule 2
    # 2. HTTP 429
    ("read_me", 429, None, False, "global", "rate"),
    ("read_comments", 429, err(100, 33), False, "global", "rate"),
    # 3. HTTP 200, object body, no error key
    ("read_me", 200, {"id": "1", "user_id": "2", "username": "u"}, False, "ok", ""),
    ("read_media", 200, {"id": "1"}, False, "ok", ""),
    ("read_comments", 200, {"data": []}, False, "ok", ""),
    ("refresh", 200, {"access_token": "new", "expires_in": 5184000}, False, "ok", ""),
    ("read_comments", 200, {}, False, "ok", ""),
    ("read_comments", 200, [], False, "error", "unparseable"),
    ("read_comments", 200, None, False, "error", "unparseable"),
    ("read_comments", 200, "data", False, "error", "unparseable"),
    ("read_comments", 200, {"data": [], "error": None}, False, "error", "unparseable"),
    ("read_me", 201, {"id": "1"}, False, "error", "unparseable"),
    ("read_me", 200, err(190), False, "global", "auth"),
    # 4. the account-wide table
    *[(kind, 400, body, False, "global", reason) for body, reason in ACCOUNT_WIDE for kind in ("read_me", "refresh")],
    ("read_me", 400, err(230), False, "global", "blocked"),                      # per-item only for a POST
    ("read_me", 400, err(20), False, "error", "code_20"),                        # spam only for kind public
    # 5. read_media / read_comments with 100/33, 24, 803
    ("read_media", 400, err(100, 33), False, "gone", ""),
    ("read_comments", 400, err(100, 33), False, "gone", ""),
    ("read_media", 404, err(24), False, "gone", ""),
    ("read_comments", 404, err(803), False, "gone", ""),
    ("read_me", 400, err(100, 33), False, "error", "code_100"),
    ("refresh", 400, err(803), False, "error", "code_803"),
    ("read_media", 400, err(100, 34), False, "error", "code_100"),
    # 6. anything else
    ("read_me", 400, err(100), False, "error", "code_100"),
    ("read_comments", 400, err(551), False, "error", "code_551"),
    ("read_comments", 400, err(10900), False, "error", "code_10900"),
    ("refresh", 400, err(999), False, "error", "code_999"),
    ("read_me", 404, None, False, "error", "unparseable"),
    ("read_me", 400, {"error": {"message": "x"}}, False, "error", "unparseable"),
    ("read_me", 400, {"error": {"code": 190.7}}, False, "error", "unparseable"),
    ("read_me", 302, None, False, "error", "unparseable"),
    ("read_me", 400, {"error": {"code": "190"}}, False, "global", "auth"),
]


class Table(unittest.TestCase):
    def check(self, method, rows):
        for kind, http, body, connect_failed, outcome, reason in rows:
            with self.subTest(kind=kind, http=http, body=body, connect_failed=connect_failed):
                self.assertEqual(meta.classify(method, kind, http, body, connect_failed), (outcome, reason))

    def test_post_rules(self):
        self.check("POST", POST_ROWS)

    def test_get_rules(self):
        self.check("GET", GET_ROWS)

    def test_a_method_that_is_not_get_gets_the_post_rules(self):
        for method in ("post", "PUT", "get", "", None):
            with self.subTest(method=method):
                self.assertEqual(meta.classify(method, "read_me", 200, {"id": "1"}, False),
                                 ("uncertain", "ok_without_id"))
                self.assertEqual(meta.classify(method, "dm", 0, None, False), ("uncertain", "no_response"))

    def test_a_status_that_is_not_a_number_counts_as_no_response(self):
        for http in (None, "", "abc", [], {}, True):
            with self.subTest(http=http):
                self.assertEqual(meta.classify("POST", "dm", http, {"message_id": "m1"}, False),
                                 ("uncertain", "no_response"))
                self.assertEqual(meta.classify("GET", "read_me", http, {"id": "1"}, False), ("transient", ""))


POST_OUTCOMES = {"ok", "uncertain", "not_sent", "global", "rejected"}
GET_OUTCOMES = {"ok", "transient", "global", "gone", "error"}
REJECTED = {"already_replied", "user_unavailable", "comment_gone", "outside_window", "invalid"}
GLOBAL = {"auth", "spam", "rate", "blocked"}
KINDS = ["dm", "public", "read_me", "read_media", "read_comments", "refresh", "", "DM", None]
STATUSES = [0, 100, 199, 200, 201, 204, 299, 300, 302, 399, 400, 401, 403, 404, 429, 499, 500, 502, 503, 599, 600, -1]
CODES = sorted({code for code, _ in list(meta._PER_ITEM) + list(meta._ACCOUNT_WIDE)}
               | {1, -1, 2, 0, 20, 100, 10900, 200, 250, 299, 80000, 80050, 80099, 80100, 999})
SUBCODES = sorted({sub for _, sub in list(meta._PER_ITEM) + list(meta._ACCOUNT_WIDE) if sub} | {2534025, 7, 0})
GARBAGE = [None, True, False, "", " ", "abc", "1e3", "0x10", " 190 ", "9" * 5000, 10 ** 30, -10 ** 30, 1.5,
           float("nan"), float("inf"), float("-inf"), [], [1], {}, {"code": 1}, b"190", "\ud83d", (), object()]
MESSAGES = ["", "x", ALREADY, "ALREADY REPLIED", UNSUPPORTED, "API access blocked.", None, 5, ["already replied"]]
IDS = ["m1", "17890", "", " ", "\n", None, 0, 1, 17890, True, [], ["m1"], {"id": "m1"}, 1.5]


def has_id(kind, body):
    """The test's own reading of 'the id field is present': a non-blank string under the kind's field."""
    field = {"dm": "message_id", "public": "id"}.get(kind)
    return isinstance(body, dict) and isinstance(body.get(field), str) and body[field].strip() != ""


def integer_code(body):
    """The test's own reading of 'an integer error.code': a whole number of at most 64 bits, or its text."""
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, (str, bytes)):
        try:
            code = int(code)
        except ValueError:
            return None
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        return None
    return int(code) if -2 ** 63 <= code < 2 ** 63 and code == int(code) else None


class Fuzz(unittest.TestCase):
    def number(self, rng, known):
        roll = rng.random()
        if roll < 0.55:
            value = rng.choice(known)
        elif roll < 0.75:
            value = rng.randint(-5, 100000)
        else:
            return rng.choice(GARBAGE)
        return rng.choice([value, value, str(value), float(value)])

    def body(self, rng):
        roll = rng.random()
        if roll < 0.12:
            return rng.choice([None, "", "<html>502</html>", "error", 0, 190, 1.5, True, [], [1, 2], b"{}"])
        if roll < 0.17:
            return [self.body(rng)]
        body = {}
        if rng.random() < 0.75:
            if rng.random() < 0.15:
                body["error"] = rng.choice(GARBAGE + ["invalid_token", ["x"], 190])
            else:
                error = {}
                if rng.random() < 0.9:
                    error["code"] = self.number(rng, CODES)
                if rng.random() < 0.5:
                    error["error_subcode"] = self.number(rng, SUBCODES)
                if rng.random() < 0.15:
                    error["subcode"] = self.number(rng, SUBCODES)
                if rng.random() < 0.7:
                    error["message"] = rng.choice(MESSAGES)
                body["error"] = error
        for field in ("message_id", "id"):
            if rng.random() < 0.35:
                body[field] = rng.choice(IDS)
        if rng.random() < 0.2:
            body["data"] = []
        return body

    def test_random_answers(self):
        rng = random.Random(20261003)
        seen = set()
        for i in range(40000):
            method = rng.choice(["POST", "GET"])
            kind = rng.choice(KINDS)
            http = rng.choice(STATUSES) if rng.random() < 0.85 else rng.randint(-3, 700)
            body = self.body(rng)
            connect_failed = rng.random() < 0.2
            case = f"#{i} {method} kind={kind!r} http={http} connect_failed={connect_failed} body={body!r}"
            before = repr(body)
            try:
                result = meta.classify(method, kind, http, body, connect_failed)
            except Exception as e:
                self.fail(f"classify raised {type(e).__name__}: {e} for {case}")
            self.assertEqual(repr(body), before, f"the body was changed: {case}")
            self.assertEqual(result, meta.classify(method, kind, http, body, connect_failed), case)
            self.assertIsInstance(result, tuple, case)
            outcome, reason = result
            self.assertIsInstance(reason, str, case)
            self.assertIn(outcome, GET_OUTCOMES if method == "GET" else POST_OUTCOMES, case)
            if outcome == "not_sent":
                self.assertTrue(connect_failed and http == 0, f"not_sent without a failed connect: {case}")
                self.assertEqual(reason, "connect", case)
            if outcome == "ok" and method == "POST":
                self.assertTrue(200 <= http < 300 and has_id(kind, body), f"POST ok without the id: {case}")
            if outcome == "ok" and method == "GET":
                self.assertTrue(http == 200 and isinstance(body, dict) and "error" not in body and not connect_failed,
                                f"GET ok without a clean answer: {case}")
            if outcome == "rejected":
                self.assertIn(reason, REJECTED, case)
                self.assertTrue(integer_code(body) is not None and http not in (0, 429) and not 200 <= http < 300
                                and http < 500, f"refusal without an error code, or under 2xx, 429, 5xx or no status: {case}")
            if outcome == "global":
                self.assertIn(reason, GLOBAL, case)
                self.assertTrue(http == 429 or integer_code(body) is not None,
                                f"account-wide refusal without 429 or an error code: {case}")
                if method == "POST":  # the comment is sent to again: Meta must have answered, and not 2xx or 5xx
                    self.assertTrue(http != 0 and http < 500 and not 200 <= http < 300, case)
            if outcome in ("uncertain", "error"):
                self.assertTrue(reason, f"{outcome} without a reason: {case}")
            seen.add((method, outcome))
        self.assertEqual(seen, {("POST", o) for o in POST_OUTCOMES} | {("GET", o) for o in GET_OUTCOMES},
                         "the generator must reach every outcome")


if __name__ == "__main__":
    unittest.main()
