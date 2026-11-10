"""Tests of the fake itself, driven the way a client drives it: plain urllib against 127.0.0.1.

Run from autoreply/:  python3.12 -B -m unittest sim.test_fake_meta
"""
import collections
import functools
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlencode

from sim.fake_meta import FakeMeta

V = "/v26.0"
TOKEN = "IGAAtoken1"
UID, APP_ID = "17841400000000001", "9990000000000001"
ACCOUNT = {"user_id": UID, "id": APP_ID, "username": "your_account", "followers_count": 1234, "media_count": 7}
OWN = {"id": APP_ID, "username": "your_account"}
MEDIA = "18000000000000001"
PERMALINK = "https://www.instagram.com/reel/DAbc123/"
ME_FIELDS = "id,user_id,username,followers_count,media_count"
MEDIA_FIELDS = "id,permalink,shortcode,comments_count,timestamp"
COMMENT_FIELDS = "id,text,timestamp,from,hidden,replies{id,from}"
META_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+0000")
DAY = 86400
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_meta.py")

Answer = collections.namedtuple("Answer", "status body headers")

# Loopback only, and never through a proxy the machine may have configured.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(method, url, body=None, headers=None, timeout=10):
    """One HTTP exchange -> Answer(status, parsed JSON or text, headers). A connection failure raises."""
    if not url.startswith("http://127.0.0.1:"):
        raise AssertionError(f"these tests talk to 127.0.0.1 only: {url}")
    data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
    if headers is None:
        headers = {} if body is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as response:
            status, raw, head = response.status, response.read(), response.headers
    except urllib.error.HTTPError as answer:
        with answer:
            status, raw, head = answer.code, answer.read(), answer.headers
    try:
        return Answer(status, json.loads(raw), head)
    except ValueError:
        return Answer(status, raw.decode(), head)


def who(user):
    return None if user is None else {"id": user, "username": "user_" + user}


def comment(comment_id, user="u1", text="арена", age=60, **more):
    when = {} if "ts" in more else {"age_sec": age}
    return {"id": comment_id, "text": text, **when, "from": who(user), "should_match": True, **more}


def scenario(*comments, **top):
    return {"account": dict(ACCOUNT), "token": {"value": TOKEN, "age_sec": 2 * DAY},
            "media": [{"id": MEDIA, "permalink": PERMALINK, "shortcode": "DAbc123", "age_sec": 3 * DAY,
                       "comments": list(comments)}], **top}


def text_message(text="Привет! 👋 Держи ссылку 👇\n\nhttps://github.com/x/y"):
    return {"text": text}


class Case(unittest.TestCase):
    maxDiff = None

    def start(self, *comments, scale=1.0, anchor=None, **top):
        self.fake = FakeMeta(scenario(*comments, **top), scale, anchor).start()
        self.addCleanup(self.fake.stop)
        return self.fake

    def api(self, method, path, params=None, body=None, token=TOKEN, headers=None):
        query = dict(params or {})
        if token is not None:
            query["access_token"] = token
        return fetch(method, f"{self.fake.api_base}{path}?{urlencode(query)}", body, headers)

    def me(self, **kw):
        return self.api("GET", V + "/me", {"fields": ME_FIELDS}, **kw)

    def media(self, media_id=MEDIA, **kw):
        return self.api("GET", f"{V}/{media_id}", {"fields": MEDIA_FIELDS}, **kw)

    def my_media(self, **kw):
        return self.api("GET", V + "/me/media", {"fields": "id,permalink,shortcode,timestamp,comments_count",
                                                 "limit": "25"}, **kw)

    def comments(self, media_id=MEDIA, fields=COMMENT_FIELDS, limit="50", **kw):
        return self.api("GET", f"{V}/{media_id}/comments", {"fields": fields, "limit": limit}, **kw)

    def dm(self, comment_id, message=None, **kw):
        return self.api("POST", f"{V}/{UID}/messages", **kw,
                        body={"recipient": {"comment_id": comment_id}, "message": message or text_message()})

    def public(self, comment_id, text="Отправил в личку 📩", **kw):
        return self.api("POST", f"{V}/{comment_id}/replies", {"message": text}, **kw)

    def refresh(self, token=TOKEN):
        return self.api("GET", "/refresh_access_token", {"grant_type": "ig_refresh_token"}, token=token)

    def telegram(self, text="⚠ hold:account blocked", chat="42", bot="TG:abc", **more):
        return fetch("POST", f"{self.fake.api_base}/bot{bot}/sendMessage", {"chat_id": chat, "text": text, **more})

    def every_meta_call(self):
        """One of each call of SPEC section 6, by name. `c1` must exist."""
        return {"me": self.me, "media": self.media, "my_media": self.my_media, "comments": self.comments,
                "dm": functools.partial(self.dm, "c1"), "public": functools.partial(self.public, "c1"),
                "refresh": self.refresh}

    def control(self, path, body=None):
        answer = fetch("GET" if body is None else "POST", self.fake.control_base + path, body)
        self.assertEqual(answer.status, 200, answer.body)
        return answer.body

    def truth(self):
        return self.control("/truth")

    def last_request(self):
        return self.truth()["requests"][-1]

    def clock(self):
        return self.control("/clock")["now"]

    def wait_for_clock(self, moment):
        deadline = time.monotonic() + 20
        while self.clock() < moment:
            self.assertLess(time.monotonic(), deadline, "the fake's clock does not advance")
            time.sleep(0.02)

    def assertRefused(self, answer, http, code, subcode=None):
        self.assertIsInstance(answer.body, dict, answer)
        error = answer.body.get("error", {})
        self.assertEqual((answer.status, error.get("code"), error.get("error_subcode")), (http, code, subcode), answer)

    def ids(self, answer):
        self.assertEqual(answer.status, 200, answer.body)
        return [row["id"] for row in answer.body["data"]]


class StandAloneScript(unittest.TestCase):
    def run_script(self, scenario_json, args=(), env=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, "scenario.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(scenario_json, file)
        process = subprocess.Popen([sys.executable, "-B", SCRIPT, "--scenario", path, *args], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **(env or {})})
        self.addCleanup(process.communicate)
        self.addCleanup(process.kill)
        return process

    def test_prints_one_json_line_with_two_ports_and_serves_until_killed(self):
        anchor = int(time.time()) - 100
        process = self.run_script(scenario(comment("c1")), ["--scale", "60", "--anchor", str(anchor)])
        ports = json.loads(process.stdout.readline())
        self.assertEqual(sorted(ports), ["api", "control"])
        clock = fetch("GET", f"http://127.0.0.1:{ports['control']}/clock").body
        self.assertEqual((clock["scale"], clock["anchor"]), (60, anchor))
        self.assertAlmostEqual(clock["now"], anchor + (clock["real"] - anchor) * 60, places=3)
        self.assertGreater(clock["now"], time.time() + 99 * 59)
        me = fetch("GET", f"http://127.0.0.1:{ports['api']}{V}/me?fields=username&access_token={TOKEN}")
        self.assertEqual((me.status, me.body), (200, {"username": "your_account"}))
        self.assertIsNone(process.poll(), "the fake must keep serving until it is killed")
        process.terminate()
        self.assertEqual(process.wait(10), 0, "SIGTERM is a clean stop")
        self.assertEqual(process.stdout.read(), "", "one line on stdout, nothing more")

    def test_clock_defaults_come_from_the_bot_environment(self):
        anchor = int(time.time()) - 50
        process = self.run_script(scenario(), env={"CLOCK_SCALE": "30", "CLOCK_ANCHOR": str(anchor)})
        ports = json.loads(process.stdout.readline())
        clock = fetch("GET", f"http://127.0.0.1:{ports['control']}/clock").body
        self.assertEqual((clock["scale"], clock["anchor"]), (30, anchor))
        # empty values mean "not set", as in the bot's clock.py
        process = self.run_script(scenario(), env={"CLOCK_SCALE": "", "CLOCK_ANCHOR": ""})
        ports = json.loads(process.stdout.readline())
        clock = fetch("GET", f"http://127.0.0.1:{ports['control']}/clock").body
        self.assertEqual(clock["scale"], 1)
        self.assertAlmostEqual(clock["now"], time.time(), delta=5)

    def test_a_bad_scenario_starts_nothing(self):
        process = self.run_script({**scenario(), "surprise": 1})
        out, errors = process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(out, "")
        self.assertIn("surprise", errors)


class Strictness(Case):
    def test_calls_outside_spec_section_6_are_errors(self):
        dm_body = {"recipient": {"comment_id": "c1"}, "message": text_message()}
        dm_path = f"{V}/{UID}/messages"
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        cases = [  # name, method, path, query (a str is sent as is), body, headers
            ("unknown path", "GET", V + "/me/feed", {"fields": "id"}, None, None),
            ("no version prefix", "GET", "/me", {"fields": "id"}, None, None),
            ("comments without a version", "GET", f"/{MEDIA}/comments", {}, None, None),
            ("refresh under a version", "GET", V + "/refresh_access_token", {"grant_type": "ig_refresh_token"},
             None, None),
            ("POST on a read", "POST", V + "/me", {}, None, None),
            ("GET on /messages", "GET", dm_path, {}, None, None),
            ("DELETE of a comment", "DELETE", V + "/c1", {}, None, None),
            ("unknown parameter", "GET", V + "/me", {"fields": "id", "locale": "ru_RU"}, None, None),
            ("comments cannot be filtered by time", "GET", f"{V}/{MEDIA}/comments", {"since": "1"}, None, None),
            ("repeated parameter", "GET", V + "/me", f"fields=id&fields=username&access_token={TOKEN}", None, None),
            ("field that is not served", "GET", V + "/me", {"fields": "id,biography"}, None, None),
            ("reply field that is not served", "GET", f"{V}/{MEDIA}/comments", {"fields": "id,replies{id,like_count}"},
             None, None),
            ("expansion of a plain field", "GET", f"{V}/{MEDIA}/comments", {"fields": "id,from{id}"}, None, None),
            ("malformed fields", "GET", f"{V}/{MEDIA}/comments", {"fields": "id,,text"}, None, None),
            ("limit zero", "GET", f"{V}/{MEDIA}/comments", {"limit": "0"}, None, None),
            ("limit not a number", "GET", f"{V}/{MEDIA}/comments", {"limit": "many"}, None, None),
            ("public reply text in a JSON body", "POST", V + "/c1/replies", {}, {"message": "спасибо"}, None),
            ("public reply text in a form body", "POST", V + "/c1/replies", {}, b"message=thanks", form),
            ("public reply with an extra parameter", "POST", V + "/c1/replies", {"message": "a", "fields": "id"},
             None, None),
            ("/messages under the app-scoped id", "POST", f"{V}/{APP_ID}/messages", {}, dm_body, None),
            ("/messages under me", "POST", V + "/me/messages", {}, dm_body, None),
            ("extra key in the DM body", "POST", dm_path, {}, {**dm_body, "messaging_type": "RESPONSE"}, None),
            ("recipient by user id", "POST", dm_path, {}, {"recipient": {"id": "u1"}, "message": text_message()},
             None),
            ("comment id not a string", "POST", dm_path, {},
             {"recipient": {"comment_id": 1}, "message": text_message()}, None),
            ("DM parameters in the query", "POST", dm_path, {"recipient": '{"comment_id":"c1"}', "message": "x"},
             None, None),
            ("DM body that is not declared JSON", "POST", dm_path, {}, json.dumps(dm_body).encode(), form),
            ("DM body that is not JSON", "POST", dm_path, {}, b"recipient=c1", {"Content-Type": "application/json"}),
            ("unknown message shape", "POST", dm_path, {},
             {**dm_body, "message": {"text": "x", "quick_replies": []}}, None),
            ("button template", "POST", dm_path, {}, {**dm_body, "message": {"attachment": {
                "type": "template", "payload": {"template_type": "button", "text": "x", "buttons": [
                    {"type": "web_url", "title": "x", "url": "https://example.com/"}]}}}}, None),
            ("wrong grant type", "GET", "/refresh_access_token", {"grant_type": "ig_exchange_token"}, None, None),
        ]
        self.start(comment("c1"))
        for number, (name, method, path, query, body, headers) in enumerate(cases, 1):
            with self.subTest(name):
                if isinstance(query, str):
                    answer = fetch(method, f"{self.fake.api_base}{path}?{query}", body, headers)
                else:
                    answer = self.api(method, path, query, body, headers=headers)
                self.assertRefused(answer, 400, 2500)
                self.assertTrue(answer.body["error"]["message"].startswith("FAKE STRICT: "), answer.body)
                record = self.last_request()
                self.assertEqual((record["seq"], record["method"], record["path"]), (number, method, path))
                self.assertTrue(record["strict"])
                self.assertFalse(record["delivered"])
        truth = self.truth()
        self.assertEqual((truth["dms"], truth["replies"]), ([], []))
        self.assertEqual(self.comments().body["data"][0].get("replies"), None)

    def test_telegram_calls_other_than_send_message_are_errors(self):
        self.start()
        base = self.fake.api_base
        cases = [
            ("unknown parameter", "POST", "/botTG/sendMessage", {"chat_id": "1", "text": "x", "reply_to": 5}),
            ("an access_token has no business here", "POST", "/botTG/sendMessage",
             {"chat_id": "1", "text": "x", "access_token": TOKEN}),
            ("GET", "GET", "/botTG/sendMessage?chat_id=1&text=x", None),
            ("another method of the bot API", "POST", "/botTG/getMe", {}),
        ]
        for name, method, path, body in cases:
            with self.subTest(name):
                answer = fetch(method, base + path, body)
                self.assertEqual(answer.status, 400)
                self.assertTrue(self.last_request()["strict"])
        self.assertEqual(self.truth()["telegram"], [])

    def test_the_calls_of_section_6_are_served(self):
        self.start(comment("c1"))
        for name, call in self.every_meta_call().items():
            with self.subTest(name):
                self.assertEqual(call().status, 200)
        self.assertEqual(self.telegram().status, 200)
        truth = self.truth()
        self.assertEqual([r["kind"] for r in truth["requests"]],
                         ["me", "media", "my_media", "comments", "dm", "public", "refresh", "telegram"])
        self.assertEqual([r["strict"] for r in truth["requests"]], [None] * 8)

    def test_a_wrong_or_missing_token_is_code_190_on_every_call(self):
        self.start(comment("c1"))
        for token in ("IGAAother", None):
            for name, call in self.every_meta_call().items():
                with self.subTest(name, token=token):
                    self.assertRefused(call(token=token), 400, 190)
                    record = self.last_request()
                    self.assertIs(record["token_ok"], False)
                    self.assertIsNone(record["strict"])
        truth = self.truth()
        self.assertEqual((truth["dms"], truth["replies"]), ([], []))
        self.assertEqual(self.me().status, 200)
        self.assertIs(self.last_request()["token_ok"], True)


class Reads(Case):
    def test_me_returns_only_the_requested_fields(self):
        self.start()
        self.assertEqual(self.me().body, ACCOUNT)
        self.assertEqual(self.api("GET", V + "/me", {"fields": "user_id,username"}).body,
                         {"user_id": UID, "username": "your_account"})
        self.assertEqual(self.api("GET", V + "/me").body, {"id": APP_ID})

    def test_media_and_its_comments_count(self):
        self.start(comment("c1", age=10, replies=[{"text": "+", "from": who("u2")}, {"text": "да", "from": who("u3")}]),
                   comment("c2", "u2", age=20), comment("c3", "u3", age=30, listed=False))
        answer = self.media()
        self.assertEqual(answer.status, 200)
        self.assertRegex(answer.body.pop("timestamp"), META_TIME)
        # 3 top-level comments (one of them never listed) + 2 replies (meta-api-facts C13, C16)
        self.assertEqual(answer.body,
                         {"id": MEDIA, "permalink": PERMALINK, "shortcode": "DAbc123", "comments_count": 5})
        self.assertEqual(self.api("GET", f"{V}/{MEDIA}").body, {"id": MEDIA})
        self.assertEqual(self.ids(self.comments()), ["c1", "c2"])
        self.control("/comments", {"delete": "c1"})
        self.assertEqual(self.media().body["comments_count"], 2)

    def test_unknown_media_does_not_exist(self):
        self.start()
        self.assertRefused(self.media("18000000000000404"), 400, 100, 33)
        self.assertRefused(self.comments("18000000000000404"), 400, 100, 33)

    def test_my_media_newest_first_with_paging(self):
        media = [{"id": f"1800{n}", "permalink": f"https://www.instagram.com/reel/R{n}/", "shortcode": f"R{n}",
                  "age_sec": n * DAY} for n in (3, 1, 2)]
        self.start(media=media)
        first = self.api("GET", V + "/me/media", {"fields": "id,shortcode", "limit": "2"})
        self.assertEqual(first.body["data"], [{"id": "18001", "shortcode": "R1"}, {"id": "18002", "shortcode": "R2"}])
        second = fetch("GET", first.body["paging"]["next"])
        self.assertEqual(second.body["data"], [{"id": "18003", "shortcode": "R3"}])
        self.assertNotIn("next", second.body["paging"])


class Listing(Case):
    def pages(self, first):
        """Every page from `first` on, following paging.next exactly as given."""
        pages, answer = [], first
        while True:
            pages.append(self.ids(answer))
            following = answer.body.get("paging", {}).get("next")
            if not following:
                return pages
            self.assertTrue(following.startswith(self.fake.api_base + V + "/"), following)
            self.assertEqual(sorted(answer.body["paging"]["cursors"]), ["after", "before"])
            answer = fetch("GET", following)

    def test_newest_first_and_at_most_50_per_page(self):
        names = [f"c{n:03d}" for n in range(120)]
        self.start(*[comment(name, f"u{n}", age=10 * (n + 1)) for n, name in reversed(list(enumerate(names)))])
        pages = self.pages(self.comments(fields="id", limit="200"))
        self.assertEqual([len(page) for page in pages], [50, 50, 20])
        self.assertEqual(sum(pages, []), names)
        self.assertEqual(len(self.ids(self.comments(fields="id", limit="7"))), 7)

    def test_cursors_stay_put_when_comments_come_and_go_between_pages(self):
        names = [f"c{n:03d}" for n in range(120)]
        self.start(*[comment(name, f"u{n}", age=10 * (n + 1)) for n, name in enumerate(names)])
        first = self.comments(fields="id")
        self.assertEqual(self.ids(first), names[:50])
        for n in range(3):
            self.control("/comments", {"add": {"media_id": MEDIA, **comment(f"new{n}", "u500", age=0)}})
        self.control("/comments", {"delete": "c010"})   # already read
        self.control("/comments", {"delete": "c060"})   # not read yet
        rest = self.pages(fetch("GET", first.body["paging"]["next"]))
        # no comment of page 1 again, none skipped, the deleted one gone
        self.assertEqual(sum(rest, []), [name for name in names[50:] if name != "c060"])
        self.assertEqual([len(page) for page in rest], [50, 19])
        self.assertEqual(self.ids(self.comments(fields="id"))[:4], ["new2", "new1", "new0", "c000"])

    def test_comments_of_the_same_second_are_each_listed_once(self):
        self.start(*[comment(f"c{n}", f"u{n}", ts=1_790_000_000) for n in range(5)], limits={"page_size": 2})
        pages = self.pages(self.comments(fields="id"))
        self.assertEqual(pages, [["c4", "c3"], ["c2", "c1"], ["c0"]])

    def test_a_page_shorter_than_the_limit_can_still_have_a_next(self):
        self.start(*[comment(f"c{n}", age=n + 1) for n in range(4)], limits={"page_size": 3})
        first = self.comments(limit="50")
        self.assertEqual(self.ids(first), ["c0", "c1", "c2"])
        last = fetch("GET", first.body["paging"]["next"])
        self.assertEqual(self.ids(last), ["c3"])
        self.assertNotIn("next", last.body["paging"])

    def test_no_comments_is_an_empty_list(self):
        self.start()
        self.assertEqual(self.comments().body, {"data": []})

    def test_only_requested_fields_and_meta_defaults(self):
        self.start(comment("plain", "u1", "Арена!", age=3600),
                   comment("ghost", None, "arena", age=1800),
                   comment("hid", "u3", "арена", age=900, hidden=True),
                   comment("talk", "u4", "что это?", age=600, replies=[
                       {"id": "r1", "text": "арена", "from": who("u9"), "age_sec": 500},
                       {"id": "r2", "text": "Отправил 📩", "from": OWN, "age_sec": 400},
                       {"id": "r3", "text": "ок", "from": None, "age_sec": 300}]))
        rows = {row["id"]: row for row in self.comments().body["data"]}
        self.assertEqual(list(rows), ["talk", "hid", "ghost", "plain"])
        stamp = rows["plain"].pop("timestamp")
        self.assertRegex(stamp, META_TIME)
        self.assertEqual(rows["plain"], {"id": "plain", "text": "Арена!", "from": who("u1"), "hidden": False})
        self.assertNotIn("from", rows["ghost"])                     # C9: Meta does not promise an author
        self.assertIs(rows["hid"]["hidden"], True)
        self.assertNotIn("replies", rows["hid"])
        # newest reply first; only the asked sub-fields; a reply without an author has no `from`
        self.assertEqual(rows["talk"]["replies"], {"data": [{"id": "r3"}, {"id": "r2", "from": OWN},
                                                            {"id": "r1", "from": who("u9")}]})
        default = self.api("GET", f"{V}/{MEDIA}/comments").body["data"]    # C6
        self.assertEqual([sorted(row) for row in default], [["id", "text", "timestamp"]] * 4)
        self.assertEqual(default[3]["timestamp"], stamp)
        bare = self.comments(fields="id,replies").body["data"][0]
        self.assertEqual([sorted(reply) for reply in bare["replies"]["data"]], [["id", "text", "timestamp"]] * 3)
        self.assertEqual(self.comments(fields="hidden").body["data"], [{"hidden": False}, {"hidden": True},
                                                                       {"hidden": False}, {"hidden": False}])

    def test_timestamp_is_the_comment_time_in_metas_format(self):
        self.start(comment("c1", ts=1_790_000_000))
        self.assertEqual(self.comments().body["data"][0]["timestamp"], "2026-09-21T14:13:20+0000")

    def test_an_invalid_cursor_is_refused(self):
        self.start(comment("c1"))
        answer = self.api("GET", f"{V}/{MEDIA}/comments", {"fields": "id", "after": "not-a-cursor"})
        self.assertRefused(answer, 400, 100)
        self.assertIsNone(self.last_request()["strict"])

    def test_scenario_can_withhold_and_garble_what_meta_lists(self):
        self.start(comment("c1", age=40), comment("unlisted", "u2", age=30, listed=False),
                   comment("no_time", "u3", age=20, raw={"timestamp": None}),
                   comment("bad_time", "u4", age=10, raw={"timestamp": "yesterday", "text": None}))
        rows = self.comments().body["data"]
        self.assertEqual([row["id"] for row in rows], ["bad_time", "no_time", "c1"])
        self.assertEqual(rows[0], {"id": "bad_time", "timestamp": "yesterday", "from": who("u4"), "hidden": False})
        self.assertNotIn("timestamp", rows[1])

    def test_a_listing_that_never_arrived_does_not_count_as_listed(self):
        self.start(comment("c1"), faults=[{"kind": "comments", "nth": 1, "drop": "after"},
                                          {"kind": "comments", "nth": 2, "http": 500}])
        with self.assertRaises(http.client.RemoteDisconnected):
            self.comments()
        self.assertEqual(self.comments().status, 500)
        self.assertIsNone(self.control("/comments")["comments"][0]["first_listed_seq"])
        self.assertEqual(self.ids(self.comments()), ["c1"])
        self.assertEqual(self.control("/comments")["comments"][0]["first_listed_seq"], 3)

    def test_a_listing_sent_to_a_dead_client_does_not_count_as_listed(self):
        self.start(comment("c1"))
        hold = self.control("/hold", {"add": {"kind": "comments", "phase": "after"}})["id"]
        client = socket.create_connection(("127.0.0.1", self.fake.api_port), timeout=10)
        self.addCleanup(client.close)
        client.sendall(f"GET {V}/{MEDIA}/comments?access_token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        self.assertEqual(self.control("/hold", {"wait": hold})["caught"], True)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # close with a reset
        client.close()
        time.sleep(0.2)
        self.control("/hold", {"release": hold})
        deadline = time.monotonic() + 10
        while self.truth()["requests"][0]["sent"] is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertIs(self.truth()["requests"][0]["sent"], False)
        self.assertIsNone(self.control("/comments")["comments"][0]["first_listed_seq"])

    def test_listed_comments_are_remembered_for_the_completeness_check(self):
        self.start(*[comment(f"c{n}", age=n + 1) for n in range(3)], limits={"page_size": 2})
        state = {row["id"]: row for row in self.control("/comments")["comments"]}
        self.assertEqual([state[name]["first_listed_seq"] for name in ("c0", "c1", "c2")], [None, None, None])
        self.comments()
        self.comments()
        state = {row["id"]: row for row in self.control("/comments")["comments"]}
        self.assertEqual([state[name]["first_listed_seq"] for name in ("c0", "c1", "c2")], [1, 1, None])


class PrivateReply(Case):
    def test_a_private_reply_is_delivered_with_its_full_payload(self):
        self.start(comment("c1", "u1"), comment("c2", None))
        answer = self.dm("c1", text_message())
        self.assertEqual(answer.status, 200)
        self.assertEqual(sorted(answer.body), ["message_id", "recipient_id"])
        self.assertTrue(answer.body["message_id"])
        self.assertEqual(self.dm("c2", text_message("Привет")).status, 200)   # an author Meta does not show
        truth = self.truth()
        self.assertEqual([(d["seq"], d["comment_id"], d["media_id"], d["user_id"], d["message"]) for d in truth["dms"]],
                         [(1, "c1", MEDIA, "u1", text_message()), (2, "c2", MEDIA, None, text_message("Привет"))])
        self.assertEqual([r["delivered"] for r in truth["requests"]], [True, True])

    def test_one_private_reply_per_comment_in_three_spellings(self):
        spellings = [("10900", (400, 10900, None), "already replied"),
                     ("2534025", (400, 100, 2534025), ""),
                     ("-1", (400, -1, None), "already has a reply")]
        for spelling, refusal, phrase in spellings:
            for deliver in (False, True):
                with self.subTest(spelling=spelling, deliver=deliver):
                    fake = self.start(comment("c1"), comment("c2", "u2"),
                                      already_replied={"spelling": spelling, "deliver": deliver})
                    self.assertEqual(self.dm("c1").status, 200)
                    second = self.dm("c1", text_message("ещё раз"))
                    self.assertRefused(second, *refusal)
                    self.assertIn(phrase, second.body["error"]["message"].lower())
                    self.assertEqual(self.dm("c2").status, 200)   # the rule is per comment
                    truth = self.truth()
                    self.assertEqual([d["comment_id"] for d in truth["dms"]],
                                     ["c1", "c1", "c2"] if deliver else ["c1", "c2"])
                    self.assertEqual([r["delivered"] for r in truth["requests"]], [True, deliver, True])
                    fake.stop()

    def test_default_spelling_is_10900_and_history_can_be_seeded(self):
        self.start(comment("c1", private_replied=True))
        self.assertRefused(self.dm("c1"), 400, 10900)
        self.assertEqual(self.truth()["dms"], [])

    def test_seven_day_window(self):
        self.start(comment("inside", "u1", age=7 * DAY - 120), comment("outside", "u2", age=7 * DAY + 1))
        self.assertEqual(self.dm("inside").status, 200)
        self.assertRefused(self.dm("outside"), 400, 10, 2534022)
        self.assertEqual([d["comment_id"] for d in self.truth()["dms"]], ["inside"])

    def test_the_window_closes_on_the_scaled_clock(self):
        # 1 real second = 1 scaled hour: `late` leaves the window after one real second
        self.start(comment("early", "u1", age=7 * DAY - 12 * 3600), comment("late", "u2", age=7 * DAY - 3600),
                   scale=3600, anchor=time.time() - 1000)
        self.assertEqual(self.dm("early").status, 200)
        written = {row["id"]: row["ts"] for row in self.control("/comments")["comments"]}
        self.wait_for_clock(written["late"] + 7 * DAY)
        self.assertRefused(self.dm("late"), 400, 10, 2534022)
        self.assertEqual([d["comment_id"] for d in self.truth()["dms"]], ["early"])

    def test_unknown_and_deleted_comments(self):
        self.start(comment("c1"))
        self.assertRefused(self.dm("17900000000000404"), 400, 100, 33)
        self.assertEqual(self.ids(self.comments()), ["c1"])          # listed ...
        self.control("/comments", {"delete": "c1"})                  # ... and deleted before the send
        self.assertRefused(self.dm("c1"), 400, 100, 33)
        self.assertEqual(self.truth()["dms"], [])
        self.assertEqual(self.ids(self.comments()), [])

    def test_payload_limits(self):
        cases = [  # name, message, refusal (None = delivered)
            ("text of exactly 1000 bytes", text_message("я" * 500), None),
            ("text under 1000 characters but over 1000 bytes", text_message("я" * 501), (400, 100, None)),
            ("empty text", text_message(""), (400, 100, None)),
        ]
        self.start(*[comment(f"c{n}", f"u{n}") for n in range(len(cases))])
        for n, (name, message, refusal) in enumerate(cases):
            with self.subTest(name):
                answer = self.dm(f"c{n}", message)
                if refusal is None:
                    self.assertEqual(answer.status, 200, answer.body)
                    self.assertEqual(self.truth()["dms"][-1]["message"], message)
                    continue
                self.assertRefused(answer, *refusal)
                record = self.last_request()
                self.assertEqual((record["strict"], record["delivered"]), (None, False))
                self.assertEqual(self.dm(f"c{n}").status, 200)   # a refused payload did not use up the one reply

    def test_meta_refuses_calls_over_its_hourly_cap(self):
        self.start(*[comment(f"c{n}", f"u{n}") for n in range(4)], limits={"cap_per_hour": 2},
                   scale=3600, anchor=time.time() - 1000)
        self.assertEqual([self.dm("c0").status, self.dm("c1").status], [200, 200])
        self.assertRefused(self.dm("c2"), 400, 613, 2534040)
        truth = self.truth()
        self.assertEqual([d["comment_id"] for d in truth["dms"]], ["c0", "c1"])
        self.wait_for_clock(truth["requests"][1]["t"] + 3600)        # the rolling hour is scaled time
        self.assertEqual(self.dm("c3").status, 200)                  # calls count, so the refused one is the other


class PublicReply(Case):
    def test_reply_appears_under_the_comment_as_the_accounts_own(self):
        self.start(comment("c1"), comment("c2", "u2", replies=[{"id": "r1", "text": "+", "from": who("u3")}]))
        before = self.media().body["comments_count"]
        answer = self.public("c2", "Готово, загляни в директ 📩")
        self.assertEqual((answer.status, sorted(answer.body)), (200, ["id"]))
        reply_id = answer.body["id"]
        rows = {row["id"]: row for row in self.comments().body["data"]}
        self.assertEqual(rows["c2"]["replies"]["data"],
                         [{"id": reply_id, "from": OWN}, {"id": "r1", "from": who("u3")}])
        self.assertNotIn("replies", rows["c1"])
        self.assertEqual(self.media().body["comments_count"], before + 1)
        truth = self.truth()
        self.assertEqual([(r["seq"], r["comment_id"], r["id"], r["text"]) for r in truth["replies"]],
                         [(2, "c2", reply_id, "Готово, загляни в директ 📩")])
        self.assertEqual(truth["requests"][1]["params"]["message"], "Готово, загляни в директ 📩")
        state = {row["id"]: row for row in self.control("/comments")["comments"]}
        self.assertEqual([(r["id"], r["own"], r["by_api"]) for r in state["c2"]["replies"]],
                         [("r1", False, False), (reply_id, True, True)])

    def test_a_hidden_comment_cannot_be_replied_to(self):
        self.start(comment("born_hidden", hidden=True), comment("c2", "u2"))
        self.control("/comments", {"hide": "c2"})
        for name in ("born_hidden", "c2"):
            with self.subTest(name):
                self.assertRefused(self.public(name), 400, 100)
                self.assertFalse(self.last_request()["delivered"])
        self.assertEqual(self.truth()["replies"], [])
        self.assertEqual([row.get("replies") for row in self.comments().body["data"]], [None, None])
        self.assertEqual(self.dm("c2").status, 200)   # hiding does not stop the private reply

    def test_unknown_deleted_and_empty(self):
        self.start(comment("c1"), comment("c2", "u2"))
        self.control("/comments", {"delete": "c2"})
        self.assertRefused(self.public("17900000000000404"), 400, 100, 33)
        self.assertRefused(self.public("c2"), 400, 100, 33)
        self.assertRefused(self.api("POST", V + "/c1/replies"), 400, 100)
        self.assertRefused(self.public("c1", ""), 400, 100)
        self.assertEqual(self.truth()["replies"], [])


class TokenRefresh(Case):
    def test_refresh_of_an_old_enough_token(self):
        self.start(token={"value": TOKEN, "age_sec": DAY + 60})
        answer = self.refresh()
        self.assertEqual((answer.status, answer.body),
                         (200, {"access_token": TOKEN, "token_type": "bearer", "expires_in": 60 * DAY}))
        self.assertTrue(self.last_request()["delivered"])
        self.assertRefused(self.refresh(), 400, 100)   # the refreshed token is young again
        self.assertEqual(self.me().status, 200)

    def test_a_token_younger_than_24_hours_cannot_be_refreshed(self):
        self.start(token={"value": TOKEN, "age_sec": DAY - 60})
        self.assertRefused(self.refresh(), 400, 100)
        self.assertFalse(self.last_request()["delivered"])
        self.assertEqual(self.me().status, 200)

    def test_a_plain_string_token_is_brand_new(self):
        self.start(token=TOKEN)
        self.assertEqual(self.me().status, 200)
        self.assertRefused(self.refresh(), 400, 100)

    def test_rotation_makes_the_old_string_invalid(self):
        self.start(token={"value": TOKEN, "age_sec": 8 * DAY, "rotate": True})
        fresh = self.refresh().body["access_token"]
        self.assertNotEqual(fresh, TOKEN)
        self.assertRefused(self.me(), 400, 190)
        self.assertEqual(self.me(token=fresh).status, 200)

    def test_an_expired_token_is_dead_and_cannot_be_refreshed(self):
        self.start(token={"value": TOKEN, "age_sec": 61 * DAY, "expires_in_sec": -1})
        self.assertRefused(self.me(), 400, 190, 463)
        self.assertRefused(self.refresh(), 400, 190, 463)
        self.assertIs(self.last_request()["token_ok"], False)

    def test_age_and_expiry_run_on_the_scaled_clock(self):
        # 1 real second = 1 scaled hour
        self.start(token={"value": TOKEN, "age_sec": DAY - 3600, "expires_in_sec": 6 * 3600},
                   scale=3600, anchor=time.time() - 1000)
        start = self.clock()
        self.assertRefused(self.refresh(), 400, 100)
        self.wait_for_clock(start + 3600)
        self.assertEqual(self.refresh().body["expires_in"], 60 * DAY)
        self.control("/token", {"value": "IGAAtoken2", "expires_in_sec": 3600})
        self.assertEqual(self.me(token="IGAAtoken2").status, 200)
        self.wait_for_clock(self.clock() + 3600)
        self.assertRefused(self.me(token="IGAAtoken2"), 400, 190, 463)

    def test_a_token_minted_by_a_human_replaces_the_old_one(self):
        self.start()
        self.control("/token", {"value": "IGAAtoken2"})
        self.assertRefused(self.me(), 400, 190)
        self.assertEqual(self.me(token="IGAAtoken2").status, 200)
        self.assertEqual(self.truth()["events"][-1]["op"], "token")


class Modes(Case):
    def set_mode(self, mode):
        self.assertEqual(self.control("/mode", {"mode": mode}), {"mode": mode})

    def test_blocked_refuses_every_call_whatever_the_token(self):
        self.start(comment("c1"))
        self.set_mode("blocked")
        for token in (TOKEN, "IGAAother"):
            for name, call in self.every_meta_call().items():
                with self.subTest(name, token=token):
                    answer = call(token=token)
                    self.assertRefused(answer, 400, 200)
                    self.assertEqual(answer.body["error"]["message"], "API access blocked.")
                    self.assertIn("access_denied", answer.headers["WWW-Authenticate"])
        truth = self.truth()
        self.assertEqual((truth["dms"], truth["replies"], truth["mode"]), ([], [], "blocked"))
        self.assertEqual({r["mode"] for r in truth["requests"]}, {"blocked"})
        self.assertEqual(self.telegram().status, 200)   # Telegram is not Meta
        self.set_mode("ok")
        self.assertEqual([call().status for call in self.every_meta_call().values()], [200] * 7)

    def test_token_dead_is_code_190_on_every_call(self):
        self.start(comment("c1"), mode="token_dead")
        for name, call in self.every_meta_call().items():
            with self.subTest(name):
                self.assertRefused(call(), 400, 190, 467)
                self.assertIs(self.last_request()["token_ok"], False)
        self.assertEqual(self.truth()["dms"], [])

    def test_dev_mode_lists_no_comments_but_keeps_the_count(self):
        self.start(comment("c1"), comment("c2", "u2"))
        self.set_mode("dev_mode")
        answer = self.comments()
        self.assertEqual((answer.status, answer.body["data"]), (200, []))
        self.assertEqual(sorted(answer.body["paging"]["cursors"]), ["after", "before"])
        self.assertNotIn("next", answer.body["paging"])
        self.assertEqual(self.media().body["comments_count"], 2)
        self.assertEqual(self.me().status, 200)
        self.assertRefused(self.comments("18000000000000404"), 400, 100, 33)
        self.set_mode("ok")
        self.assertEqual(self.ids(self.comments()), ["c2", "c1"])

    def test_throttled_is_code_4_with_usage_headers_of_unknown_shape(self):
        self.start(comment("c1"))
        self.set_mode("throttled")
        documented = {"call_count", "total_cputime", "total_time"}
        for name, call in self.every_meta_call().items():
            with self.subTest(name):
                answer = call()
                self.assertRefused(answer, 400, 4)
                app = json.loads(answer.headers["X-App-Usage"])
                self.assertTrue(documented < set(app), app)                       # B5: keys nobody documented
                (account, (usage,)), = json.loads(answer.headers["X-Business-Use-Case-Usage"]).items()
                self.assertEqual(account, UID)
                self.assertTrue(documented | {"type", "estimated_time_to_regain_access"} < set(usage), usage)
        self.assertEqual(self.truth()["dms"], [])
        self.assertRefused(self.me(token="IGAAother"), 400, 190)

    def test_messaging_blocked_refuses_only_private_replies(self):
        self.start(comment("c1"))
        self.set_mode("messaging_blocked")
        calls = self.every_meta_call()
        self.assertRefused(calls.pop("dm")(), 400, 100, 2534029)
        self.assertEqual(self.truth()["dms"], [])
        self.assertEqual({name: call().status for name, call in calls.items()}, dict.fromkeys(calls, 200))

    def test_mode_changes_are_validated_and_logged(self):
        self.start(mode="throttled")
        self.assertRefused(self.me(), 400, 4)
        refused = fetch("POST", self.fake.control_base + "/mode", {"mode": "maintenance"})
        self.assertEqual(refused.status, 400)
        self.assertEqual(self.truth()["mode"], "throttled")
        self.set_mode("ok")
        self.assertEqual(self.me().status, 200)
        truth = self.truth()
        self.assertEqual([(e["op"], e["mode"], e["after_seq"]) for e in truth["events"]],
                         [("mode", "throttled", 0), ("mode", "ok", 1)])
        self.assertEqual([r["mode"] for r in truth["requests"]], ["throttled", "ok"])
        with self.assertRaises(ValueError):
            FakeMeta(scenario(mode="maintenance"))


class Faults(Case):
    def add(self, **fault):
        return self.control("/faults", {"add": fault})["faults"]

    def test_delivery_is_independent_of_the_answer(self):
        html = "<html><body>502 Bad Gateway</body></html>"
        answers = [  # name, the fault's answer, what the client must see: (status, body check) or None = no answer
            ("code 1 on HTTP 400", {"code": 1, "message": "An unknown error occurred"},
             (400, lambda b: b["error"]["code"] == 1 and b["error"]["message"] == "An unknown error occurred")),
            ("code 1 on HTTP 200", {"http": 200, "code": 1}, (200, lambda b: b["error"]["code"] == 1)),
            ("code -1", {"code": -1}, (400, lambda b: b["error"]["code"] == -1)),
            ("code 2 with a subcode on HTTP 500", {"http": 500, "code": 2, "subcode": 1545133},
             (500, lambda b: (b["error"]["code"], b["error"]["error_subcode"]) == (2, 1545133))),
            ("HTTP 503 without a code", {"http": 503}, (503, lambda b: "code" not in b["error"])),
            ("a body that is not JSON", {"http": 502, "body": html}, (502, lambda b: b == html)),
            ("a JSON body that is not Meta's", {"http": 200, "body": {"error": {"code": "1", "subcode": "7"}}},
             (200, lambda b: b == {"error": {"code": "1", "subcode": "7"}})),
            ("200 without message_id", {"omit_id": True}, (200, lambda b: sorted(b) == ["recipient_id"])),
            ("connection dropped after processing", {"drop": "after"}, None),
        ]
        for name, fault, seen in answers:
            for deliver in (True, False):
                with self.subTest(name, deliver=deliver):
                    fake = self.start(comment("c1"))
                    self.assertEqual(self.add(kind="dm", nth=1, deliver=deliver, **fault), ["f1"])
                    if seen is None:
                        with self.assertRaises(http.client.RemoteDisconnected):
                            self.dm("c1")
                    else:
                        answer = self.dm("c1")
                        self.assertEqual(answer.status, seen[0], answer.body)
                        self.assertTrue(seen[1](answer.body), answer.body)
                    truth = self.truth()
                    record = truth["requests"][0]
                    self.assertEqual((record["fault_id"], record["delivered"], record["sent"]),
                                     ("f1", deliver, seen is not None))
                    self.assertEqual([d["comment_id"] for d in truth["dms"]], ["c1"] if deliver else [])
                    # the comment's one private reply is used up exactly when the message went out
                    again = self.dm("c1")
                    if deliver:
                        self.assertRefused(again, 400, 10900)
                    else:
                        self.assertEqual(again.status, 200)
                    self.assertEqual(len(self.truth()["dms"]), 1)
                    fake.stop()

    def test_a_dropped_connection_before_processing_changes_nothing(self):
        self.start(comment("c1"))
        self.add(kind="dm", nth=1, drop="before")
        with self.assertRaises(http.client.RemoteDisconnected):
            self.dm("c1")
        truth = self.truth()
        record = truth["requests"][0]
        self.assertEqual([record[key] for key in ("status", "response", "delivered", "sent", "fault_id")],
                         [None, None, False, False, "f1"])
        self.assertEqual(truth["dms"], [])
        self.assertEqual(self.dm("c1").status, 200)

    def test_a_failed_call_can_use_up_the_one_private_reply(self):
        self.start(comment("c1"))
        self.add(kind="dm", nth=1, code=100, subcode=2534001, deliver=False, consume=True)
        self.assertRefused(self.dm("c1", text_message("Привет")), 400, 100, 2534001)
        self.assertRefused(self.dm("c1", text_message()), 400, 10900)
        self.assertEqual(self.truth()["dms"], [])

    def test_public_reply_faults_with_and_without_delivery(self):
        for deliver in (True, False):
            with self.subTest(deliver=deliver):
                fake = self.start(comment("c1"))
                self.add(kind="public", http=500, deliver=deliver)
                self.assertEqual(self.public("c1").status, 500)
                replies = self.comments().body["data"][0].get("replies", {"data": []})["data"]
                self.assertEqual([reply["from"] for reply in replies], [OWN] if deliver else [])
                self.assertEqual(len(self.truth()["replies"]), int(deliver))
                fake.stop()
        self.start(comment("c1"))
        self.add(kind="public", omit_id=True, deliver=True)
        answer = self.public("c1")
        self.assertEqual((answer.status, answer.body), (200, {}))
        self.assertEqual(len(self.truth()["replies"]), 1)

    def test_refresh_fault_with_and_without_effect(self):
        for deliver in (True, False):
            with self.subTest(deliver=deliver):
                fake = self.start(token={"value": TOKEN, "age_sec": 8 * DAY, "rotate": True})
                self.add(kind="refresh", http=200, body={"token_type": "bearer"}, deliver=deliver)
                self.assertEqual(self.refresh().body, {"token_type": "bearer"})
                # rotated behind the client's back, or not at all
                self.assertEqual(self.me().status, 400 if deliver else 200)
                fake.stop()

    def test_telegram_faults_with_and_without_delivery(self):
        self.start()
        self.add(kind="telegram", nth=1, http=429, message="Too Many Requests: retry after 5", deliver=False)
        self.add(kind="telegram", nth=2, http=500, deliver=True)
        first, second, third = self.telegram("one"), self.telegram("two"), self.telegram("three")
        self.assertEqual((first.status, first.body), (429, {"ok": False, "error_code": 429,
                                                            "description": "Too Many Requests: retry after 5"}))
        self.assertEqual((second.status, second.body["ok"]), (500, False))
        self.assertEqual((third.status, third.body["ok"]), (200, True))
        self.assertEqual([m["text"] for m in self.truth()["telegram"]], ["two", "three"])

    def test_faults_on_reads(self):
        other = "18000000000000002"
        self.start(media=[{"id": name, "permalink": PERMALINK, "shortcode": "DAbc123",
                           "comments": [{"text": "арена", "from": who("u1"), "should_match": True}]}
                          for name in (MEDIA, other)])
        self.add(kind="comments", target=MEDIA, nth=2, http=500, code=2)
        self.add(kind="media", target=other, code=100, subcode=33)
        self.add(kind="me", nth=1, http=200, body={"id": APP_ID, "user_id": "17840000000000002", "username": "thief"})
        self.assertEqual([self.comments().status for _ in range(3)], [200, 500, 200])
        self.assertEqual([self.comments(other).status for _ in range(2)], [200, 200])
        self.assertEqual(self.media().status, 200)
        for _ in range(2):   # no nth: every call
            self.assertRefused(self.media(other), 400, 100, 33)
        self.assertEqual(self.me().body["username"], "thief")
        self.assertEqual(self.me().body, ACCOUNT)
        self.assertEqual([r["fault_id"] for r in self.truth()["requests"]],
                         [None, "f1", None, None, None, None, "f2", "f2", "f3", None])

    def test_faults_address_a_comment_or_its_author(self):
        self.start(comment("c1", "u1"), comment("c2", "u2"), comment("c3", "u1"), comment("c4", "u3"))
        self.add(kind="dm", target="u1", code=551, message="This person isn't available right now.", deliver=False)
        self.add(kind="dm", target="c4", id="gone", code=100, subcode=33, deliver=False)
        seen = [self.dm(name) for name in ("c1", "c2", "c3", "c4", "c1")]
        self.assertEqual([answer.status for answer in seen], [400, 200, 400, 400, 400])
        self.assertEqual([r["fault_id"] for r in self.truth()["requests"]], ["f1", None, "f1", "gone", "f1"])
        self.assertEqual([d["comment_id"] for d in self.truth()["dms"]], ["c2"])

    def test_nth_counts_the_matching_calls_of_each_fault(self):
        self.start(*[comment(f"c{n}", f"u{n}") for n in range(6)])
        self.add(kind="dm", nth=[2, 3], http=500, deliver=False)
        self.add(kind="dm", target="c5", nth=1, code=1, deliver=False)
        self.add(kind="dm", nth=3, code=2, deliver=False)       # loses to the first fault that fires
        self.assertEqual([self.dm(f"c{n}").status for n in range(6)], [200, 500, 500, 200, 200, 400])
        self.assertEqual([r["fault_id"] for r in self.truth()["requests"]], [None, "f1", "f1", None, None, "f2"])

    def pattern(self, seed, probability=0.5):
        fake = self.start(faults=[{"kind": "me", "probability": probability, "seed": seed, "http": 500}])
        statuses = [self.me().status for _ in range(40)]
        fake.stop()
        return statuses

    def test_probability_with_a_seed_is_reproducible(self):
        first = self.pattern(7)
        self.assertEqual(self.pattern(7), first)
        self.assertNotEqual(self.pattern(8), first)
        self.assertTrue(8 <= first.count(500) <= 32, first)
        self.assertEqual(set(self.pattern(7, 0)), {200})
        self.assertEqual(set(self.pattern(7, 1)), {500})

    def test_delay_headers_and_any_status(self):
        self.start()
        self.add(kind="me", nth=1, delay_real_sec=0.4, headers={"X-App-Usage": '{"call_count": 3}'})
        self.add(kind="me", nth=2, http=400, code=17, headers={"X-App-Usage": '{"call_count": 100, "mystery": 1}'})
        self.add(kind="me", nth=3, http=302, body="", headers={"Location": "http://127.0.0.1:9/elsewhere"})
        started = time.monotonic()
        slow = self.me()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertEqual((slow.body, slow.headers["X-App-Usage"]), (ACCOUNT, '{"call_count": 3}'))   # the real answer
        limited = self.me()
        self.assertRefused(limited, 400, 17)
        self.assertEqual(json.loads(limited.headers["X-App-Usage"]), {"call_count": 100, "mystery": 1})
        connection = http.client.HTTPConnection("127.0.0.1", self.fake.api_port, timeout=10)   # follows no redirect
        self.addCleanup(connection.close)
        connection.request("GET", f"{V}/me?fields=id&access_token={TOKEN}")
        moved = connection.getresponse()
        self.assertEqual((moved.status, moved.getheader("Location"), moved.read()),
                         (302, "http://127.0.0.1:9/elsewhere", b""))

    def test_refused_connections_for_a_period(self):
        self.start(comment("c1"))
        self.add(kind="comments", nth=1, refuse_real_sec=1.0)
        self.assertEqual(self.comments().status, 200)             # the triggering call is still answered
        closed_at = time.monotonic()
        with self.assertRaises(urllib.error.URLError) as caught:
            self.dm("c1")
        self.assertIsInstance(caught.exception.reason, ConnectionRefusedError)
        truth = self.truth()                                       # the control port stays up
        self.assertEqual((len(truth["requests"]), truth["dms"]), (1, []))
        while True:
            try:
                answer = self.dm("c1")
                break
            except urllib.error.URLError:
                self.assertLess(time.monotonic() - closed_at, 10, "the listener never came back")
                time.sleep(0.05)
        self.assertGreaterEqual(time.monotonic() - closed_at, 0.5)
        self.assertEqual(answer.status, 200)
        truth = self.truth()
        self.assertEqual([(e["op"], e.get("accept")) for e in truth["events"][1:]],
                         [("faults", None), ("listener", False), ("listener", True)])
        self.assertEqual([r["kind"] for r in truth["requests"]], ["comments", "dm"])   # refused calls never arrived

    def test_clear_and_ids(self):
        self.start()
        self.assertEqual(self.add(kind="me", http=500), ["f1"])
        self.assertEqual(self.add(kind="me", id="slow", delay_real_sec=0.01), ["f1", "slow"])
        self.assertEqual(self.me().status, 500)
        self.assertEqual(self.control("/faults", {"clear": True, "add": [{"kind": "me", "nth": 2, "http": 503}]}),
                         {"faults": ["f3"]})
        self.assertEqual([self.me().status for _ in range(3)], [200, 503, 200])
        self.assertEqual(self.add(kind="me", nth=50, http=500), ["f3", "f4"])   # an id is never used twice in a run
        self.assertEqual(self.control("/faults", {"clear": True}), {"faults": []})
        self.assertEqual([r["fault_id"] for r in self.truth()["requests"]], ["f1", None, "f3", None])

    def test_a_fault_that_cannot_mean_what_it_says_is_rejected(self):
        self.start(comment("c1"), faults=[{"id": "taken", "kind": "me", "nth": 99, "http": 500}])
        bad = [
            ("delivery left to chance", {"kind": "dm", "code": 1}),
            ("delivery not a boolean", {"kind": "dm", "http": 500, "deliver": "yes"}),
            ("delivery not a boolean, no answer", {"kind": "dm", "deliver": "no"}),
            ("consume not a boolean", {"kind": "dm", "http": 400, "deliver": False, "consume": 1}),
            ("omit_id false", {"kind": "dm", "omit_id": False, "deliver": True}),
            ("status not a number", {"kind": "me", "http": "500"}),
            ("status out of range", {"kind": "me", "http": 99}),
            ("delay not a number", {"kind": "me", "delay_real_sec": "1"}),
            ("negative refusal", {"kind": "me", "refuse_real_sec": -1}),
            ("headers not an object", {"kind": "me", "headers": ["X-App-Usage"]}),
            ("unknown drop", {"kind": "dm", "drop": "sideways"}),
            ("dropped and answered", {"kind": "dm", "drop": "after", "http": 500, "deliver": True}),
            ("delivered before it was processed", {"kind": "dm", "drop": "before", "deliver": True}),
            ("delivery of a read", {"kind": "me", "http": 500, "deliver": True}),
            ("consume outside dm", {"kind": "public", "http": 400, "deliver": False, "consume": True}),
            ("omit_id on a read", {"kind": "me", "omit_id": True}),
            ("telegram has no codes", {"kind": "telegram", "code": 1, "deliver": False}),
            ("nothing to do", {"kind": "dm", "target": "c1"}),
            ("unknown kind", {"kind": "inbox", "http": 500}),
            ("no kind", {"http": 500}),
            ("nth zero", {"kind": "me", "nth": 0, "http": 500}),
            ("nth empty", {"kind": "me", "nth": [], "http": 500}),
            ("nth and probability", {"kind": "me", "nth": 1, "probability": 0.5, "seed": 1, "http": 500}),
            ("probability without a seed", {"kind": "me", "probability": 0.5, "http": 500}),
            ("probability over 1", {"kind": "me", "probability": 1.5, "seed": 1, "http": 500}),
            ("unknown key", {"kind": "me", "http": 500, "retry_ok": True}),
            ("id already used", {"kind": "me", "id": "taken", "http": 500}),
        ]
        for name, fault in bad:
            with self.subTest(name):
                answer = fetch("POST", self.fake.control_base + "/faults", {"add": fault})
                self.assertEqual(answer.status, 400, answer.body)
                self.assertIn("error", answer.body)
                with self.assertRaises((ValueError, KeyError)):
                    FakeMeta(scenario(faults=[fault]) if name != "id already used" else
                             scenario(faults=[fault, fault]))
        self.assertEqual(self.control("/faults", {}), {"faults": ["taken"]})
        self.assertEqual(self.me().status, 200)


class Hold(Case):
    def hold(self, **spec):
        return self.control("/hold", {"add": spec})["id"]

    def background(self, call, *args):
        box = []
        thread = threading.Thread(target=lambda: box.append(call(*args)), daemon=True)
        thread.start()
        return thread, box

    def raw_dm(self, comment_id):
        """A DM request on a bare socket: the client can be 'killed' (closed) while the fake holds the request."""
        body = json.dumps({"recipient": {"comment_id": comment_id}, "message": text_message()}).encode()
        head = (f"POST {V}/{UID}/messages?access_token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n")
        client = socket.create_connection(("127.0.0.1", self.fake.api_port), timeout=10)
        self.addCleanup(client.close)
        client.sendall(head.encode() + body)
        return client

    def wait_until_finished(self, seq):
        deadline = time.monotonic() + 10
        while self.truth()["requests"][seq - 1]["sent"] is None:
            self.assertLess(time.monotonic(), deadline, "the held request never finished")
            time.sleep(0.02)

    def test_hold_before_processing(self):
        self.start(comment("c1"), comment("c2", "u2"))
        hold = self.hold(kind="dm", phase="before", target="c2")
        self.assertEqual(self.dm("c1").status, 200)               # another target passes
        thread, box = self.background(self.dm, "c2")
        self.assertEqual(self.control("/hold", {"wait": hold}), {"caught": True, "seq": 2})
        truth = self.truth()
        record = truth["requests"][1]
        self.assertEqual((record["held"], record["status"], record["delivered"], record["sent"]),
                         ("before", None, False, None))
        self.assertEqual([d["comment_id"] for d in truth["dms"]], ["c1"])
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.control("/hold", {"release": hold}), {"released": hold})
        thread.join(10)
        self.assertEqual(box[0].status, 200)
        truth = self.truth()
        self.assertEqual([d["comment_id"] for d in truth["dms"]], ["c1", "c2"])
        self.assertEqual((truth["requests"][1]["status"], truth["requests"][1]["sent"]), (200, True))

    def test_kill_placed_after_processing(self):
        self.start(comment("c1"))
        hold = self.hold(kind="dm", phase="after")
        client = self.raw_dm("c1")
        self.assertEqual(self.control("/hold", {"wait": hold}), {"caught": True, "seq": 1})
        truth = self.truth()
        record = truth["requests"][0]
        # Meta has done it, the client knows nothing yet
        self.assertEqual((record["held"], record["status"], record["delivered"], record["sent"]),
                         ("after", 200, True, None))
        self.assertEqual([d["comment_id"] for d in truth["dms"]], ["c1"])
        client.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            client.recv(1)
        client.close()                                             # kill -9
        self.control("/hold", {"release": hold})
        self.wait_until_finished(1)
        self.assertEqual(len(self.truth()["dms"]), 1)
        self.assertRefused(self.dm("c1"), 400, 10900)

    def test_kill_placed_before_processing_still_reaches_meta(self):
        self.start(comment("c1"))
        hold = self.hold(kind="dm", phase="before")
        client = self.raw_dm("c1")
        self.assertEqual(self.control("/hold", {"wait": hold})["caught"], True)
        self.assertEqual(self.truth()["dms"], [])
        client.close()
        self.control("/hold", {"release": hold})
        self.wait_until_finished(1)
        self.assertEqual([d["comment_id"] for d in self.truth()["dms"]], ["c1"])   # the request had been received

    def test_hold_with_a_drop_fault_makes_the_request_vanish(self):
        self.start(comment("c1"))
        self.control("/faults", {"add": {"kind": "dm", "nth": 1, "drop": "before"}})
        hold = self.hold(kind="dm", phase="before")
        client = self.raw_dm("c1")
        self.assertEqual(self.control("/hold", {"wait": hold})["caught"], True)
        client.close()
        self.control("/hold", {"release": hold})
        self.wait_until_finished(1)
        self.assertEqual(self.truth()["dms"], [])

    def test_a_hold_catches_the_nth_matching_request_and_only_that_one(self):
        for nth in (None, 2):
            with self.subTest(nth=nth):
                fake = self.start(*[comment(f"c{n}", f"u{n}") for n in range(4)])
                hold = self.hold(kind="dm", phase="after", **({"nth": nth} if nth else {}))
                passed = [self.dm(f"c{n}").status for n in range((nth or 1) - 1)]
                thread, box = self.background(self.dm, "c2")
                self.assertEqual(self.control("/hold", {"wait": hold}), {"caught": True, "seq": len(passed) + 1})
                passed.append(self.dm("c3").status)               # while the other one is still held
                self.assertTrue(thread.is_alive())
                self.control("/hold", {"release": hold})
                thread.join(10)
                self.assertEqual((box[0].status, set(passed)), (200, {200}))
                self.assertEqual([r["held"] for r in self.truth()["requests"]],
                                 [None] * (len(passed) - 1) + ["after", None])
                fake.stop()

    def test_both_phases_of_one_request(self):
        self.start(comment("c1"))
        first, second = self.hold(kind="comments", phase="before"), self.hold(kind="comments", phase="after")
        thread, box = self.background(self.comments)
        self.assertEqual(self.control("/hold", {"wait": first}), {"caught": True, "seq": 1})
        self.assertIsNone(self.truth()["requests"][0]["status"])
        self.control("/hold", {"release": first})
        self.assertEqual(self.control("/hold", {"wait": second}), {"caught": True, "seq": 1})
        self.assertEqual(self.truth()["requests"][0]["status"], 200)
        self.assertTrue(thread.is_alive())
        self.control("/hold", {"release": second})
        thread.join(10)
        self.assertEqual(self.ids(box[0]), ["c1"])

    def test_waiting_for_nothing_times_out_and_a_released_hold_is_cancelled(self):
        self.start(comment("c1"))
        hold = self.hold(kind="dm", phase="before")
        started = time.monotonic()
        self.assertEqual(self.control("/hold", {"wait": hold, "timeout_real_sec": 0.3}), {"caught": False, "seq": None})
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.control("/hold", {"release": hold})
        self.assertEqual(self.dm("c1").status, 200)
        self.assertIsNone(self.last_request()["held"])


class Truth(Case):
    def test_truth_is_the_exact_ordered_record(self):
        anchor = time.time() - 1000
        self.start(comment("c1"), scale=10, anchor=anchor)
        before = time.time()
        answers = [self.me(), self.comments(), self.dm("c1", text_message("Привет")), self.public("c1", "Готово 📩"),
                   self.telegram("⚠ waiting: 3"), self.me(token="IGAAother"), self.dm("c1")]
        after = time.time()
        truth = self.truth()
        requests = truth["requests"]
        self.assertEqual((truth["seq"], truth["mode"]), (7, "ok"))
        columns = ("seq", "method", "path", "kind", "target", "token_ok", "status", "delivered")
        self.assertEqual([tuple(r[c] for c in columns) for r in requests], [
            (1, "GET", V + "/me", "me", None, True, 200, False),
            (2, "GET", f"{V}/{MEDIA}/comments", "comments", MEDIA, True, 200, False),
            (3, "POST", f"{V}/{UID}/messages", "dm", "c1", True, 200, True),
            (4, "POST", V + "/c1/replies", "public", "c1", True, 200, True),
            (5, "POST", "/botTG:abc/sendMessage", "telegram", "42", None, 200, True),
            (6, "GET", V + "/me", "me", None, False, 400, False),
            (7, "POST", f"{V}/{UID}/messages", "dm", "c1", True, 400, False)])
        self.assertEqual([r["response"] for r in requests], [answer.body for answer in answers])
        self.assertEqual([r["params"] for r in requests], [
            {"fields": ME_FIELDS, "access_token": TOKEN},
            {"fields": COMMENT_FIELDS, "limit": "50", "access_token": TOKEN},
            {"access_token": TOKEN},
            {"message": "Готово 📩", "access_token": TOKEN},
            {},
            {"fields": ME_FIELDS, "access_token": "IGAAother"},
            {"access_token": TOKEN}])
        self.assertEqual([r["body"] for r in requests], [
            None, None, {"recipient": {"comment_id": "c1"}, "message": text_message("Привет")}, None,
            {"chat_id": "42", "text": "⚠ waiting: 3"}, None,
            {"recipient": {"comment_id": "c1"}, "message": text_message()}])
        for record in requests:
            self.assertEqual((record["fault_id"], record["strict"], record["held"], record["sent"], record["mode"]),
                             (None, None, None, True, "ok"))
            # received in real time, and stamped with the bot's scaled clock
            self.assertTrue(before * 1000 - 1 <= record["t_real_ms"] <= after * 1000 + 1, record)
            self.assertAlmostEqual(record["t"], anchor + (record["t_real_ms"] / 1000 - anchor) * 10, delta=0.02)
        self.assertEqual([r["t_real_ms"] for r in requests], sorted(r["t_real_ms"] for r in requests))

        def without_time(rows):
            for row in rows:
                self.assertTrue(requests[row["seq"] - 1]["t"] <= row.pop("t") <= self.clock())
            return rows
        self.assertEqual(without_time(truth["dms"]), [
            {"seq": 3, "comment_id": "c1", "media_id": MEDIA, "user_id": "u1", "message": text_message("Привет")}])
        self.assertEqual(without_time(truth["replies"]), [
            {"seq": 4, "comment_id": "c1", "id": answers[3].body["id"], "text": "Готово 📩"}])
        self.assertEqual(without_time(truth["telegram"]), [{"seq": 5, "chat_id": "42", "text": "⚠ waiting: 3"}])

    def test_truth_since_and_in_process(self):
        self.start(comment("c1"))
        self.me(), self.dm("c1"), self.public("c1"), self.telegram("one"), self.telegram("two")
        full = self.truth()
        self.assertEqual(self.fake.control("/truth"), full)        # the same answer without HTTP
        tail = self.control("/truth?since=4")
        self.assertEqual(tail["requests"], full["requests"][4:])
        self.assertEqual((tail["seq"], tail["dms"], tail["replies"]), (5, [], []))
        self.assertEqual([m["text"] for m in tail["telegram"]], ["two"])
        idle = self.control("/truth?since=5")
        self.assertEqual((idle["requests"], idle["dms"], idle["replies"], idle["telegram"]), ([], [], [], []))
        self.assertEqual(self.fake.control("/truth", query={"since": "4"}), tail)
        self.assertEqual(self.control("/truth?since=-2"), full)
        self.assertEqual(fetch("GET", self.fake.control_base + "/truth?since=soon").status, 400)

    def test_clock(self):
        anchor = time.time() - 500
        self.start(scale=20, anchor=anchor)
        before = time.time()
        clock = self.control("/clock")
        self.assertEqual((clock["scale"], clock["anchor"]), (20, anchor))
        self.assertTrue(before <= clock["real"] <= time.time())
        self.assertAlmostEqual(clock["now"], anchor + (clock["real"] - anchor) * 20, places=3)


class Telegram(Case):
    def test_send_message_as_json_or_form_is_recorded(self):
        self.start()
        first = self.telegram("⚠ hold:account\nblocked since 12:00", disable_web_page_preview=True)
        self.assertEqual((first.status, first.body["ok"]), (200, True))
        form = urlencode({"chat_id": "42", "text": "✓ cleared: hold:account"}).encode()
        second = fetch("POST", f"{self.fake.api_base}/botTG:abc/sendMessage", form,
                       {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((second.status, second.body["ok"]), (200, True))
        self.assertEqual([(m["seq"], m["chat_id"], m["text"]) for m in self.truth()["telegram"]],
                         [(1, "42", "⚠ hold:account\nblocked since 12:00"), (2, "42", "✓ cleared: hold:account")])

    def test_what_telegram_refuses_is_not_recorded(self):
        self.start(telegram={"token": "TG:abc", "chat_id": 42})
        base = f"{self.fake.api_base}/botTG:abc/sendMessage"
        cases = [("empty text", self.telegram(" "), 400),
                 ("text over 4096 characters", self.telegram("я" * 4097), 400),
                 ("no chat", fetch("POST", base, {"text": "x"}), 400),
                 ("no text", fetch("POST", base, {"chat_id": 42}), 400),
                 ("another chat", self.telegram(chat="43"), 400),
                 ("another bot", self.telegram(bot="TG:xyz"), 401)]
        for name, answer, status in cases:
            with self.subTest(name):
                self.assertEqual((answer.status, answer.body["ok"], answer.body["error_code"]), (status, False, status))
        self.assertEqual([r["strict"] for r in self.truth()["requests"]], [None] * 6)
        self.assertEqual(self.truth()["telegram"], [])
        self.assertEqual(self.telegram("я" * 4096).status, 200)
        self.assertEqual(len(self.truth()["telegram"]), 1)


class Control(Case):
    def test_comments_can_be_added_hidden_replied_to_and_deleted(self):
        self.start(comment("c1", age=100))
        new = self.control("/comments", {"add": {"media_id": MEDIA, "text": "Арена", "from": who("u7"),
                                                 "should_match": True}})["id"]
        self.assertRegex(new, r"\d+")
        self.assertEqual(self.ids(self.comments()), [new, "c1"])
        self.control("/comments", {"hide": "c1"})
        reply = self.control("/comments", {"owner_reply": "c1", "text": "Ответил в директ"})["reply_id"]
        rows = {row["id"]: row for row in self.comments().body["data"]}
        self.assertIs(rows["c1"]["hidden"], True)
        self.assertEqual(rows["c1"]["replies"]["data"], [{"id": reply, "from": OWN}])
        self.assertEqual(self.media().body["comments_count"], 3)
        self.control("/comments", {"delete": new})
        self.assertEqual(self.ids(self.comments()), ["c1"])
        self.assertEqual([e["op"] for e in self.truth()["events"]],
                         ["mode", "comment_add", "comment_hide", "comment_owner_reply", "comment_delete"])
        state = {row["id"]: row for row in self.control("/comments")["comments"]}
        self.assertEqual([state[new][key] for key in ("deleted", "should_match", "from")], [True, True, who("u7")])
        self.assertEqual([state["c1"][key] for key in ("hidden", "private_replied", "own")], [True, False, False])
        self.assertEqual([(r["own"], r["by_api"], r["text"]) for r in state["c1"]["replies"]],
                         [(True, False, "Ответил в директ")])
        self.assertEqual(self.truth()["replies"], [])              # typed by the owner, not posted through the API

    def test_own_comments_are_marked_in_the_state_whichever_id_they_carry(self):
        self.start(comment("a", None), comment("b", "u1"),
                   {"id": "c", "text": "арена", "from": {"id": UID, "username": "x"}, "should_match": False},
                   {"id": "d", "text": "арена", "from": OWN, "should_match": False},
                   {"id": "e", "text": "арена", "from": {"id": "1", "username": "your_account"}, "should_match": False})
        state = {row["id"]: row["own"] for row in self.control("/comments")["comments"]}
        self.assertEqual(state, {"a": False, "b": False, "c": True, "d": True, "e": True})

    def test_bad_control_calls_are_refused_and_change_nothing(self):
        self.start(comment("c1"), faults=[{"id": "keep", "kind": "me", "nth": 99, "http": 500}])
        new = {"media_id": MEDIA, "text": "арена", "from": who("u2"), "should_match": True}
        bad = [
            ("/comments", {"add": {**new, "id": "x1", "ts": 1, "age_sec": 1}}),
            ("/comments", {"add": {**new, "id": "x2", "replies": [{"id": "x2", "text": "+", "from": None}]}}),
            ("/comments", {"add": {**new, "id": "x3", "replies": [{"id": "c1", "text": "+", "from": None}]}}),
            ("/faults", {"clear": True, "add": [{"kind": "me", "nth": 98, "http": 500}, {"kind": "dm"}]}),
            ("/comments", {"delete": "nope"}),
            ("/comments", {"add": new, "delete": "c1"}),
            ("/comments", {"hide": "c1", "text": "x"}),
            ("/comments", {"add": {**new, "media_id": "404"}}),
            ("/comments", {"add": {"media_id": MEDIA, "text": "арена"}}),
            ("/comments", {"add": {**new, "id": "c1"}}),
            ("/comments", {"add": {**new, "ts": 1, "age_sec": 1}}),
            ("/comments", {"add": {**new, "from": {"id": "u2"}}}),
            ("/comments", {"add": {**new, "likes": 3}}),
            ("/comments", {}),
            ("/hold", {"add": {"kind": "dm", "phase": "during"}}),
            ("/hold", {"add": {"kind": "dm"}}),
            ("/hold", {"release": "h9"}),
            ("/hold", {"wait": "h9"}),
            ("/hold", {}),
            ("/mode", {}),
            ("/mode", {"mode": "ok", "until": 5}),
            ("/token", {"value": ""}),
            ("/token", {"age_sec": 5}),
            ("/faults", {"add": {"kind": "dm"}}),
            ("/faults", {"remove": "f1"}),
            ("/truth", {}),
            ("/nope", {}),
        ]
        for path, body in bad:
            with self.subTest(path, body=body):
                answer = fetch("POST", self.fake.control_base + path, body)
                self.assertEqual(answer.status, 400, answer.body)
                self.assertIn("error", answer.body)
                with self.assertRaises((ValueError, KeyError)):
                    self.fake.control(path, body)
        self.assertEqual(fetch("GET", self.fake.control_base + "/nope").status, 400)
        self.assertEqual(fetch("GET", self.fake.control_base + "/mode").status, 400)
        self.assertEqual(fetch("POST", self.fake.control_base + "/mode", b"mode=ok").status, 400)
        self.assertEqual(self.ids(self.comments()), ["c1"])
        self.assertEqual(self.me().status, 200)
        truth = self.truth()
        self.assertEqual(([e["op"] for e in truth["events"]], truth["mode"]), (["mode"], "ok"))
        self.assertEqual(self.control("/faults", {"add": {"kind": "me", "nth": 97, "http": 500}}),
                         {"faults": ["keep", "f2"]})
        for name in ("x1", "x2", "x3"):   # a refused call did not even use up the id it named
            self.assertEqual(self.control("/comments", {"add": {**new, "id": name}}), {"id": name})

    def test_two_fakes_do_not_share_anything(self):
        first = self.start(comment("c1"))
        second = FakeMeta(scenario(comment("c1")), 1.0).start()
        self.addCleanup(second.stop)
        self.assertEqual(len({first.api_port, first.control_port, second.api_port, second.control_port}), 4)
        self.assertEqual(self.dm("c1").status, 200)
        self.assertEqual(second.control("/truth")["requests"], [])
        second.stop()
        with self.assertRaises(urllib.error.URLError):
            fetch("GET", second.api_base + V + "/me?fields=id&access_token=" + TOKEN)
        self.assertEqual(self.me().status, 200)


class ScenarioValidation(unittest.TestCase):
    def test_a_scenario_with_a_mistake_is_rejected(self):
        good = scenario(comment("c1"))
        media = good["media"][0]

        def with_comment(**changes):
            changed = {key: value for key, value in {**comment("c1"), **changes}.items() if value is not ...}
            return {**good, "media": [{**media, "comments": [changed]}]}
        bad = [
            ("unknown top-level key", {**good, "seed": 1}),
            ("no token", {key: value for key, value in good.items() if key != "token"}),
            ("account field missing", {**good, "account": {"user_id": UID, "id": APP_ID, "username": "x"}}),
            ("unknown account field", {**good, "account": {**ACCOUNT, "name": "x"}}),
            ("empty token", {**good, "token": ""}),
            ("unknown token key", {**good, "token": {"value": TOKEN, "expires": 5}}),
            ("unknown limit", {**good, "limits": {"per_hour": 5}}),
            ("a page of nothing", {**good, "limits": {"page_size": 0}}),
            ("a cap that is not a number", {**good, "limits": {"cap_per_hour": "60"}}),
            ("unknown spelling", {**good, "already_replied": {"spelling": "368"}}),
            ("unknown media key", {**good, "media": [{**media, "caption": "x"}]}),
            ("media without shortcode", {**good, "media": [{"id": MEDIA, "permalink": PERMALINK}]}),
            ("comment without a label", with_comment(should_match=...)),
            ("comment without from", with_comment(**{"from": ...})),
            ("unknown comment key", with_comment(likes=3)),
            ("ts and age_sec", with_comment(ts=1_790_000_000)),
            ("author with an extra key", with_comment(**{"from": {"id": "u1", "username": "a", "name": "A"}})),
            ("reply without from", with_comment(replies=[{"text": "x"}])),
            ("the same id twice", {**good, "media": [{**media, "comments": [comment("c1"), comment("c1", "u2")]}]}),
            ("a comment with the media's id", {**good, "media": [{**media, "comments": [comment(MEDIA)]}]}),
        ]
        for name, broken in bad:
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    FakeMeta(broken)
        for scale, anchor in ((0, None), (-1, None), (float("nan"), None), (float("inf"), None), (2, float("inf"))):
            with self.subTest(scale=scale, anchor=anchor):
                with self.assertRaises(ValueError):
                    FakeMeta(good, scale, anchor)
        fake = FakeMeta(good).start()
        fake.stop()
