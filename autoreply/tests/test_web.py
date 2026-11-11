"""SPEC 14.1, the web process: webhook handshake and signature, inbound rows and their dedupe, the body cap,
the click redirect, the panel's password and files, and the panel JSON over HTTP.

Every test talks to a real server (web.make_server on a free port, in a thread) over a temporary STATE_DIR with
a database made by store.init, a copy of config.example.json and a .env written by the test.
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import clock
import engine
import store
import web

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(os.path.dirname(HERE), "dashboard")
SECRET, VERIFY, USER, PASSWORD = "app-secret-123", "verify-me", "artem", "pañel pass"
MEDIA = "17900000000000101"   # the reel of config.example.json


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def form(user, password):
    from urllib.parse import urlencode
    return urlencode({"user": user, "password": password}).encode()


class Web(unittest.TestCase):
    env = {"IG_APP_SECRET": SECRET, "WEBHOOK_VERIFY_TOKEN": VERIFY, "PANEL_USER": USER, "PANEL_PASSWORD": PASSWORD}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = folder.name
        shutil.copy(os.path.join(HERE, "config.example.json"), os.path.join(self.state, "config.json"))
        self.write_env(self.env)
        self.conn = store.connect(os.path.join(self.state, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.logged = []
        patcher = mock.patch.object(engine, "log", lambda *args, **fields: self.logged.append((args, fields)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = web.make_server(self.state, port=0, panel_dir=DASHBOARD)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def write_env(self, values):
        with open(os.path.join(self.state, ".env"), "w") as file:
            file.write("".join(f"{key}={value}\n" for key, value in values.items()))

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def post(self, payload, signature=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        return self.call("POST", "/meta/webhook", body,
                         {"X-Hub-Signature-256": signature or sign(body), "Content-Type": "application/json"})

    def inbound(self):
        return [tuple(row) for row in self.conn.execute(
            "SELECT mid, sender, kind, payload, text FROM inbound ORDER BY id")]


def messaging(*items):
    return {"object": "instagram", "entry": [{"id": "9000", "time": 1, "messaging": list(items)}]}


def tap(mid, payload, sender="555"):
    return {"sender": {"id": sender}, "recipient": {"id": "9000"}, "timestamp": 1,
            "postback": {"mid": mid, "title": "Получить", "payload": payload}}


class Webhook(Web):
    def test_health(self):
        self.assertEqual(self.call("GET", "/health")[::2], (200, b"ok\n"))

    def test_verify_handshake(self):
        ok = self.call("GET", f"/meta/webhook?hub.mode=subscribe&hub.verify_token={VERIFY}&hub.challenge=12345")
        self.assertEqual((ok[0], ok[2]), (200, b"12345"))
        for query in (f"hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1",
                      f"hub.mode=unsubscribe&hub.verify_token={VERIFY}&hub.challenge=1", "hub.challenge=1"):
            self.assertEqual(self.call("GET", "/meta/webhook?" + query)[0], 403, query)

    def test_verify_without_token_configured(self):
        self.write_env({"IG_APP_SECRET": SECRET})
        self.assertEqual(self.call("GET", "/meta/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=1")[0], 403)

    def test_bad_signature_stores_nothing(self):
        body = json.dumps(messaging(tap("m1", "g:c1"))).encode()
        for signature in ("sha256=" + "0" * 64, sign(body, "other-secret"), "", "sha1=abc", "sha256=\u00e9"):
            status, _, _ = self.call("POST", "/meta/webhook", body, {"X-Hub-Signature-256": signature})
            self.assertEqual(status, 403, signature)
        self.assertEqual(self.call("POST", "/meta/webhook", body)[0], 403)   # no header at all
        self.assertEqual(self.inbound(), [])

    def test_no_secret_configured_refuses(self):
        self.write_env({"PANEL_PASSWORD": PASSWORD})
        self.assertEqual(self.post(messaging(tap("m1", "g:c1")))[0], 403)
        self.assertEqual(self.inbound(), [])

    def test_good_signature_stores_postbacks_and_messages(self):
        payload = messaging(
            tap("m1", "g:c1"),
            {"sender": {"id": "555"}, "recipient": {"id": "9000"}, "message": {"mid": "m2", "text": "спасибо!"}},
            {"sender": {"id": "556"}, "message": {"mid": "m3", "text": "Готово", "quick_reply": {"payload": "d:c2"}}},
            {"sender": {"id": "555"}, "read": {"mid": "m1"}},                  # ignored
            {"sender": {"id": "555"}, "reaction": {"mid": "m1", "action": "react"}})
        status, _, body = self.post(payload)
        self.assertEqual((status, body), (200, b"ok\n"))
        self.assertEqual(self.inbound(), [("m1", "555", "postback", "g:c1", "Получить"),
                                          ("m2", "555", "message", None, "спасибо!"),
                                          ("m3", "556", "message", "d:c2", "Готово")])

    def test_echo_is_ignored(self):
        echo = {"sender": {"id": "9000"}, "recipient": {"id": "555"},
                "message": {"mid": "e1", "text": "our own DM", "is_echo": True}}
        self.assertEqual(self.post(messaging(echo))[0], 200)
        self.assertEqual(self.inbound(), [])

    def test_repeated_delivery_is_one_row(self):
        payload = messaging(tap("m1", "g:c1"))
        self.post(payload)
        self.post(payload)
        self.post(messaging(tap("m1", "g:c1"), tap("m9", "d:c1")))
        self.assertEqual([row[0] for row in self.inbound()], ["m1", "m9"])

    def test_item_without_mid_dedupes_by_its_json(self):
        item = {"sender": {"id": "555"}, "postback": {"title": "Готово", "payload": "d:c1"}}
        self.post(messaging(item))
        self.post(messaging(dict(reversed(list(item.items())))))   # same item, other key order
        rows = self.inbound()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][0].startswith("sha256:"))
        self.post(messaging({**item, "timestamp": 2}))
        self.assertEqual(len(self.inbound()), 2)

    def test_signed_garbage_is_200_and_nothing(self):
        for raw in (b"not json", b"[]", b'{"entry": "x"}', b'{"entry": [{"messaging": [1, null, {}]}]}'):
            self.assertEqual(self.post(None, raw=raw)[0], 200, raw)
        self.assertEqual(self.inbound(), [])

    def test_body_cap(self):
        big = b"x" * (web.MAX_BODY + 1)
        try:
            status, _, _ = self.call("POST", "/meta/webhook", big, {"X-Hub-Signature-256": sign(big)})
        except (ConnectionResetError, BrokenPipeError):
            status = 413   # the server answered 413 without reading the body and closed: the OS may reset first
        self.assertEqual(status, 413)
        self.assertEqual(self.inbound(), [])
        padded = json.dumps({**messaging(tap("m1", "g:c1")), "pad": "x" * (web.MAX_BODY - 400)}).encode()
        self.assertLessEqual(len(padded), web.MAX_BODY)
        self.assertEqual(self.post(None, raw=padded)[0], 200)
        self.assertEqual(len(self.inbound()), 1)

    def test_busy_database_is_503_and_a_repeat_completes(self):
        blocker = store.connect(os.path.join(self.state, "core.db"))
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")   # the bot in a write transaction
        with mock.patch.object(web, "BUSY_MS", 100):
            status, headers, _ = self.post(messaging(tap("m1", "g:c1")))
        self.assertEqual((status, headers.get("Retry-After")), (503, "5"))
        blocker.execute("ROLLBACK")
        self.assertEqual(self.post(messaging(tap("m1", "g:c1")))[0], 200)
        self.assertEqual(len(self.inbound()), 1)

    def test_post_elsewhere_is_404(self):
        self.assertEqual(self.call("POST", "/r/abc", b"{}")[0], 404)
        self.assertEqual(self.call("GET", "/nothing")[0], 404)


class Clicks(Web):
    def setUp(self):
        super().setUp()
        with store.tx(self.conn):
            self.conn.execute("INSERT INTO links (token, comment_id, media_id, variant, position, url, created_at) "
                              "VALUES ('tok_1', 'c1', ?, 'A', 1, 'https://example.com/skill?x=1', ?)",
                              (MEDIA, clock.iso()))

    def test_redirect_counts_a_click(self):
        status, headers, _ = self.call("GET", "/r/tok_1?utm=1", headers={"User-Agent": "Mozilla/5.0 " + "z" * 500})
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/skill?x=1")
        self.assertNotIn("Set-Cookie", headers)
        rows = self.conn.execute("SELECT token, agent FROM clicks").fetchall()
        self.assertEqual([(t, len(a)) for t, a in rows], [("tok_1", 200)])

    def test_log_lines_carry_no_secret_token_or_query(self):
        self.call("GET", "/r/tok_1?utm=secretish")
        self.call("GET", f"/meta/webhook?hub.mode=subscribe&hub.verify_token={VERIFY}&hub.challenge=777")
        self.call("POST", "/panel/login", form(USER, PASSWORD), {"Content-Type": "application/x-www-form-urlencoded"})
        text = repr(self.logged)
        for secret in ("tok_1", "secretish", VERIFY, "777", PASSWORD, SECRET):
            self.assertNotIn(secret, text)
        self.assertEqual([args[0] for args, _ in self.logged], ["INFO"] * 3)

    def test_head_redirects_without_counting(self):
        self.assertEqual(self.call("HEAD", "/r/tok_1")[0], 302)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0], 0)

    def test_unknown_token_is_404(self):
        for path in ("/r/nope", "/r/", "/r/tok_1/x", "/r/" + "a" * 65, "/r/%2e%2e"):
            self.assertEqual(self.call("GET", path)[0], 404, path)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM clicks").fetchone()[0], 0)


class Panel(Web):
    def login(self, user=USER, password=PASSWORD, address=None):
        headers = {"Content-Type": "application/x-www-form-urlencoded", **({"X-Real-IP": address} if address else {})}
        return self.call("POST", "/panel/login", form(user, password), headers)

    def cookie(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";")[0]

    def get(self, path, logged_in=True):
        return self.call("GET", path, headers={"Cookie": self.cookie()} if logged_in else {})

    def test_login_sets_a_90_day_cookie_and_opens_the_panel(self):
        status, headers, _ = self.login()
        self.assertEqual((status, headers["Location"]), (303, "/panel/"))
        cookie = headers["Set-Cookie"]
        for part in ("Max-Age=7776000", "HttpOnly", "Secure", "SameSite=Strict", "Path=/panel"):
            self.assertIn(part, cookie)
        status, headers, body = self.call("GET", "/panel/", headers={"Cookie": cookie.split(";")[0]})
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_without_login_the_panel_sends_to_the_login_page(self):
        status, headers, _ = self.get("/panel/", logged_in=False)
        self.assertEqual((status, headers["Location"]), (303, "/panel/login"))
        self.assertEqual(self.get("/panel/data/report.json", logged_in=False)[0], 401)
        status, _, body = self.call("GET", "/panel/login")
        self.assertEqual(status, 200)
        self.assertIn("Пароль".encode(), body)
        for forged in ("igpanel=1", "igpanel=99999999999.abc", "igpanel=" + web.make_session(USER, "old", time.time())):
            self.assertEqual(self.call("GET", "/panel/", headers={"Cookie": forged})[0], 303, forged)

    def test_wrong_logins_lock_the_address(self):
        with mock.patch.object(web, "LOGIN_PAUSE", 0):
            self.assertEqual(self.login(password="wrong", address="1.1.1.1")[0], 401)
            self.assertEqual(self.login(user="other", address="1.1.1.1")[0], 401)
            for _ in range(3):
                self.login(password="wrong", address="1.1.1.1")
            self.assertEqual(self.login(address="1.1.1.1")[0], 429)   # even the right password, from there
            self.assertEqual(self.login(address="2.2.2.2")[0], 303)   # another address still gets in

    def test_a_new_password_ends_every_session(self):
        cookie = self.cookie()
        self.write_env({**self.env, "PANEL_PASSWORD": "a new one"})
        self.assertEqual(self.call("GET", "/panel/", headers={"Cookie": cookie})[0], 303)

    def test_logout(self):
        status, headers, _ = self.get("/panel/logout")
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_no_password_configured_closes_everything(self):
        self.write_env({"IG_APP_SECRET": SECRET})
        for path in ("/panel", "/panel/", "/panel/login", "/panel/assets/app.js", "/panel/data/report.json"):
            self.assertEqual(self.get(path, logged_in=False)[0], 403, path)
        self.assertEqual(self.login()[0], 403)

    def test_static_files(self):
        status, headers, _ = self.get("/panel/assets/app.js?v=1")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
        self.assertEqual(self.get("/panel/assets/app.css")[0], 200)

    def test_path_traversal(self):
        for path in ("/panel/../autoreply/web.py", "/panel/%2e%2e/autoreply/web.py", "/panel/assets/../../README.md",
                     "/panel/%2e%2e%2fautoreply%2fconfig.example.json", "/panel/..%2f..%2f.env", "/panel/.git/config",
                     "/panel/README.md", "/panel/assets//app.js", "/panel/%00index.html", "/panel/assets\\app.js",
                     "/panel//etc/passwd"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_report_on_an_empty_database(self):
        status, headers, body = self.get("/panel/data/report.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        data = json.loads(body)
        self.assertEqual(set(data) >= {"generated_at", "account", "bot", "totals", "reels", "people", "hourly"}, True)
        self.assertEqual(data["totals"]["requested"], 0)
        self.assertEqual(data["people"], [])
        self.assertEqual(len(data["hourly"]), 24)
        self.assertEqual([r["media_id"] for r in data["reels"]], [MEDIA])
        self.assertIn(data["bot"]["state"], ("ok", "attention", "blocked"))

    def test_report_with_rows(self):
        now = clock.now()
        with store.tx(self.conn):
            for n, status in enumerate(("pending", "sent", "done", "uncertain")):
                store.add_comment(self.conn, f"c{n}", MEDIA, f"u{n}", f"user{n}", "арена", clock.iso(now), "seen")
                self.conn.execute("UPDATE comments SET status=? WHERE comment_id=?", (status, f"c{n}"))
            self.conn.execute("INSERT INTO attempts (comment_id, kind, detail, started_at, finished_at, outcome) "
                              "VALUES ('c2', 'dm', 'fresh', ?, ?, 'ok')", (clock.iso(now), clock.iso(now)))
            self.conn.execute("INSERT INTO flow (comment_id, media_id, variant, opening, gate, state, pressed_at, "
                              "link_at, created_at) VALUES ('c2', ?, 'A', 1, 1, 'linked', ?, ?, ?)",
                              (MEDIA, clock.iso(now), clock.iso(now), clock.iso(now)))
            self.conn.execute("INSERT INTO checks (comment_id, at, purpose, result) VALUES ('c2', ?, 'gate', 'yes')",
                              (clock.iso(now),))
            self.conn.execute("INSERT INTO links VALUES ('t2', 'c2', ?, 'A', 1, 'https://example.com', ?)",
                              (MEDIA, clock.iso(now)))
            self.conn.execute("INSERT INTO clicks (token, at, agent) VALUES ('t2', ?, 'Instagram 300'), "
                              "('t2', ?, 'facebookexternalhit/1.1')", (clock.iso(now), clock.iso(now)))
        status, _, body = self.get("/panel/data/report.json")
        self.assertEqual(status, 200)
        data = json.loads(body)
        reel = data["reels"][0]
        self.assertEqual(reel["counts"]["requested"], 4)
        self.assertEqual(reel["counts"]["sent"], 2)
        self.assertEqual((reel["clicks"], reel["robot_clicks"]), (1, 1))
        variant = reel["variants"][0]   # rows of a variant the config no longer has are still shown
        self.assertEqual((variant["name"], variant["configured"]), ("A", False))
        self.assertEqual({k: variant[k] for k in ("first_dm", "pressed", "checked_yes", "linked", "clicks",
                                                  "clicked_people")},
                         {"first_dm": 1, "pressed": 1, "checked_yes": 1, "linked": 1, "clicks": 1, "clicked_people": 1})
        person = {p["comment_id"]: p for p in data["people"]}["c2"]
        self.assertEqual((person["variant"], person["clicks"], person["reel"]), ("A", 1, "arena"))
        self.assertEqual(data["hourly"][-1]["ok"], 1)

    def test_no_database_is_503(self):
        self.conn.close()
        for name in ("core.db", "core.db-wal", "core.db-shm"):
            if os.path.exists(os.path.join(self.state, name)):
                os.remove(os.path.join(self.state, name))
        self.assertEqual(self.get("/panel/data/report.json")[0], 503)
        self.assertEqual(self.post(messaging(tap("m1", "g:c1")))[0], 503)


if __name__ == "__main__":
    unittest.main()
