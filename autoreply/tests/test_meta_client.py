"""meta.Client on real sockets: which failures prove that nothing was sent, and what goes on the wire.

Every peer here listens on 127.0.0.1. Timeouts are shortened by patching clock.T.

Run: python3.12 -B -m unittest discover -s tests -t . -p 'test_meta_client.py'
"""
import http.server
import json
import os
import re
import socket
import sqlite3
import ssl
import struct
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from collections import namedtuple
from unittest import mock

import meta

TOKEN = "IGAAtestToken0123456789abcdefXYZ"
DM = {"text": "Привет! 👋 Держи ссылку"}
SENT = {"recipient_id": "U1", "message_id": "aWdfZAG1faXRlbToxOk"}

# Throwaway self-signed certificate for 127.0.0.1 (valid until 2126); it protects nothing.
CERT = """-----BEGIN CERTIFICATE-----
MIIBkDCCATagAwIBAgIUV6EyVtmOT5WMXNt2H/DSKizGNZgwCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJMTI3LjAuMC4xMCAXDTI2MTAwMzE5NTQyM1oYDzIxMjYwOTA5
MTk1NDIzWjAUMRIwEAYDVQQDDAkxMjcuMC4wLjEwWTATBgcqhkjOPQIBBggqhkjO
PQMBBwNCAATAXO50EsGJs4KSefoEYKOEN++YxXtfmUI8guin/9ZjMcxi7pZsLPxD
fuc/Y9zDJfE0oJtKZqP7aCB7K2Pk+DGJo2QwYjAdBgNVHQ4EFgQUJ3UJh+1smJok
Yrd9la4HjWoOnqUwHwYDVR0jBBgwFoAUJ3UJh+1smJokYrd9la4HjWoOnqUwDwYD
VR0TAQH/BAUwAwEB/zAPBgNVHREECDAGhwR/AAABMAoGCCqGSM49BAMCA0gAMEUC
IQDE+A97Lvkc75P+57FvBSqjNKs87KWlAayKe+4++v1FhwIgDjpEfistsstiqtLp
3WnWDmV9Omy5mosucpjdfD36G3o=
-----END CERTIFICATE-----
"""
KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgJ2zA16903j+9LOa5
A1wonPo2KKTg1YF/PsP4MyCQKmahRANCAATAXO50EsGJs4KSefoEYKOEN++YxXtf
mUI8guin/9ZjMcxi7pZsLPxDfuc/Y9zDJfE0oJtKZqP7aCB7K2Pk+DGJ
-----END PRIVATE KEY-----
"""

Seen = namedtuple("Seen", "method path headers body")


# ---------- what the fake peer does with a request ----------

def answer(status, body, headers=()):
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()

    def respond(handler):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(payload)
    return respond


def hang_up(handler):
    """Read the whole request, then close without a byte of answer."""


def reset(handler):
    handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def silent(handler):
    handler.server.stop.wait(10)


def drip(handler, head):
    """Send `head`, then one more byte every 20 ms while the client listens (2 s at most: a client without a
    deadline must fail its test, not hang it)."""
    try:
        handler.wfile.write(head)
        end = time.monotonic() + 2
        while time.monotonic() < end and not handler.server.stop.wait(0.02):
            handler.wfile.write(b" ")
    except OSError:
        pass


def drip_body(handler):
    drip(handler, b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")


def drip_headers(handler):
    drip(handler, b"HTTP/1.1 200 OK\r\nX-Padding: ")


def in_pieces(handler):
    payload = json.dumps(SENT).encode()
    handler.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(payload))
    for piece in (payload[:10], payload[10:]):
        time.sleep(0.05)
        handler.wfile.write(piece)


class Fake:
    """HTTP server on 127.0.0.1 that records every request and lets `respond` write the answer."""

    def __init__(self, respond, tls=None):
        fake = self
        self.respond, self.requests, self.connections = respond, [], 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                fake.connections += 1

            def serve(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                target = self.requestline.split()[1]  # as sent: self.path has leading slashes collapsed
                fake.requests.append(Seen(self.command, target, self.headers, body))
                fake.respond(self)

            do_GET = do_POST = serve

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.stop = threading.Event()
        if tls:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.base = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def received(self, count=1):
        """The requests so far, once `count` of them arrived (the client may return before the handler ran)."""
        end = time.monotonic() + 5
        while len(self.requests) < count and time.monotonic() < end:
            time.sleep(0.01)
        return self.requests

    def close(self):
        self.server.stop.set()
        self.server.shutdown()
        self.server.server_close()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def query(seen):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(seen.path).query, keep_blank_values=True, strict_parsing=True)


def path(seen):
    return urllib.parse.urlsplit(seen.path).path


class Case(unittest.TestCase):
    def fake(self, respond, tls=None):
        fake = Fake(respond, tls)
        self.addCleanup(fake.close)
        return fake

    def client(self, base, ping=None):
        return meta.Client(base, "v26.0", lambda: TOKEN, ping)

    def timeouts(self, socket_timeout, deadline):
        patch = mock.patch.dict(meta.T, {"http_socket_timeout": socket_timeout, "http_deadline": deadline})
        patch.start()
        self.addCleanup(patch.stop)

    def assert_no_answer(self, result, outcome, reason):
        self.assertEqual((result.outcome, result.reason), (outcome, reason))
        self.assertEqual((result.ok, result.http, result.data, result.code, result.subcode, result.connect_failed),
                         (False, 0, {}, None, None, False))

    def assert_not_connected(self, result, outcome, reason):
        self.assertEqual((result.outcome, result.reason), (outcome, reason))
        self.assertEqual((result.ok, result.http, result.data, result.connect_failed), (False, 0, {}, True))


class Wire(Case):
    """Exactly the requests of SPEC 6."""

    def test_reads(self):
        fake = self.fake(answer(200, {"id": "1", "data": []}))
        client = self.client(fake.base)
        calls = [
            (lambda: client.me(), "/v26.0/me",
             {"fields": ["id,user_id,username,followers_count,media_count"], "access_token": [TOKEN]}),
            (lambda: client.media("18123"), "/v26.0/18123",
             {"fields": ["id,permalink,shortcode,comments_count,timestamp"], "access_token": [TOKEN]}),
            (lambda: client.comments_page("18123"), "/v26.0/18123/comments",
             {"fields": ["id,text,timestamp,from,hidden,replies{id,from}"], "limit": ["50"], "access_token": [TOKEN]}),
            (lambda: client.my_media(), "/v26.0/me/media",
             {"fields": ["id,permalink,shortcode,timestamp,comments_count"], "limit": ["25"], "access_token": [TOKEN]}),
            (lambda: client.refresh_token("OLDtoken0123456789"), "/refresh_access_token",
             {"grant_type": ["ig_refresh_token"], "access_token": ["OLDtoken0123456789"]}),
        ]
        for n, (call, want_path, want_query) in enumerate(calls, 1):
            with self.subTest(path=want_path):
                result = call()
                self.assertEqual((result.ok, result.http, result.outcome, result.reason, result.connect_failed),
                                 (True, 200, "ok", "", False))
                self.assertEqual(result.data, {"id": "1", "data": []})
                seen = fake.requests[-1]
                self.assertEqual(len(fake.requests), n)
                self.assertEqual((seen.method, path(seen), query(seen), seen.body), ("GET", want_path, want_query, b""))

    def test_paging_next_is_followed_as_given(self):
        fake = self.fake(answer(200, {"data": []}))
        target = "/v99.0/17/comments?access_token=PAGEtoken987&fields=id%2Ctext&limit=50&after=QVFIUk%3D%3D"

        def no_token():
            raise AssertionError("the next URL already carries its token")

        client = meta.Client(fake.base, "v26.0", no_token)
        for call in (client.comments_page, client.my_media):
            with self.subTest(call=call.__name__):
                result = call("18123", fake.base + target) if call == client.comments_page else call(fake.base + target)
                self.assertEqual((result.outcome, result.data), ("ok", {"data": []}))
                self.assertEqual((fake.requests[-1].method, fake.requests[-1].path), ("GET", target))
        self.assertEqual(len(fake.requests), 2)

    def test_dm_body_is_exactly_recipient_and_message(self):
        fake = self.fake(answer(200, SENT))
        result = self.client(fake.base).send_dm("17841400000000000", "17900000000000001", DM)
        self.assertEqual((result.ok, result.http, result.outcome, result.reason), (True, 200, "ok", ""))
        self.assertEqual(result.data, SENT)
        (seen,) = fake.requests
        self.assertEqual((seen.method, path(seen)), ("POST", "/v26.0/17841400000000000/messages"))
        self.assertEqual(query(seen), {"access_token": [TOKEN]})
        self.assertEqual(seen.headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(seen.body), {"recipient": {"comment_id": "17900000000000001"}, "message": DM})

    def test_public_reply_sends_the_message_as_a_query_parameter(self):
        fake = self.fake(answer(200, {"id": "17911111111111111"}))
        text = "Отправил в директ 🙌 & проверь = запросы?"
        result = self.client(fake.base).public_reply("17900000000000001", text)
        self.assertEqual((result.ok, result.outcome, result.data), (True, "ok", {"id": "17911111111111111"}))
        (seen,) = fake.requests
        self.assertEqual((seen.method, path(seen)), ("POST", "/v26.0/17900000000000001/replies"))
        self.assertEqual(query(seen), {"message": [text], "access_token": [TOKEN]})
        self.assertEqual((seen.body, seen.headers["Content-Length"]), (b"", "0"))

    def test_the_token_is_asked_for_every_request(self):
        fake = self.fake(answer(200, {"id": "1"}))
        tokens = iter(["FIRSTtoken0123456789", "SECONDtoken0123456789"])
        client = meta.Client(fake.base + "/", "v26.0", lambda: next(tokens))
        client.me()
        client.send_dm("17", "C1", DM)
        self.assertEqual([query(seen)["access_token"] for seen in fake.requests],
                         [["FIRSTtoken0123456789"], ["SECONDtoken0123456789"]])
        self.assertEqual(path(fake.requests[0]), "/v26.0/me", "a trailing slash of the base is not doubled")

    def test_an_id_cannot_change_the_path(self):
        fake = self.fake(answer(200, {"id": "1"}))
        client = self.client(fake.base)
        client.media("1/../me?fields=x#")
        client.send_dm("17/x", "C1", DM)
        client.public_reply("C 1/2", "ok")
        self.assertEqual([urllib.parse.urlsplit(seen.path).path for seen in fake.requests],
                         ["/v26.0/1%2F..%2Fme%3Ffields%3Dx%23", "/v26.0/17%2Fx/messages", "/v26.0/C%201%2F2/replies"])

    def test_ping_runs_once_after_every_call_has_returned(self):
        fake = self.fake(answer(200, SENT))
        pings = []
        client = self.client(fake.base, lambda: pings.append(len(fake.requests)))
        client.me()
        client.send_dm("17", "C1", DM)
        self.assertEqual(pings, [1, 2], "after the answer, not before the request")
        fake.respond = answer(500, {"error": {"code": 2}})
        client.send_dm("17", "C2", DM)
        fake.respond = hang_up
        client.me()
        self.assertEqual(pings, [1, 2, 3, 4])
        self.client(f"http://127.0.0.1:{closed_port()}", lambda: pings.append("refused")).send_dm("17", "C3", DM)
        self.assertEqual(pings, [1, 2, 3, 4, "refused"])

    def test_nothing_global_is_patched(self):
        self.assertEqual({type(h) for h in meta._opener.handlers}, {meta._Handler, urllib.request.UnknownHandler},
                         "no proxy, no redirect, no error handler")
        self.assertIsNot(urllib.request._opener, meta._opener)
        self.assertEqual((socket.getaddrinfo.__module__, socket.create_connection.__module__), ("socket", "socket"))

    def test_default_ports(self):
        self.assertEqual(meta._HTTPSConnection("graph.example.invalid", deadline=0.0).port, 443)
        self.assertEqual(meta._HTTPConnection("graph.example.invalid", deadline=0.0).port, 80)


class Answers(Case):
    """An HTTP answer arrived: what it becomes."""

    def test_200_without_the_id_is_uncertain(self):
        client_calls = [
            ("dm", {"recipient_id": "U1"}), ("dm", {"message_id": ""}), ("dm", {"id": "17911111111111111"}),
            ("dm", {}), ("public", {"message_id": "m1"}), ("public", {"id": ""}), ("public", {"success": True}),
        ]
        for kind, body in client_calls:
            with self.subTest(kind=kind, body=body):
                fake = self.fake(answer(200, body))
                client = self.client(fake.base)
                result = client.send_dm("17", "C1", DM) if kind == "dm" else client.public_reply("C1", "ok")
                self.assertEqual((result.ok, result.http, result.outcome, result.reason, result.connect_failed),
                                 (False, 200, "uncertain", "ok_without_id", False))
                self.assertEqual(result.data, body)
                self.assertEqual(fake.connections, 1)

    def test_500_with_code_190_is_uncertain_for_a_post(self):
        body = {"error": {"message": "Error validating access token", "type": "OAuthException", "code": 190,
                          "error_subcode": 463}}
        fake = self.fake(answer(500, body))
        client = self.client(fake.base)
        for call in (lambda: client.send_dm("17", "C1", DM), lambda: client.public_reply("C1", "ok")):
            result = call()
            self.assertEqual((result.ok, result.http, result.outcome, result.reason), (False, 500, "uncertain", "http_5xx"))
            self.assertEqual((result.code, result.subcode, result.message, result.connect_failed),
                             (190, 463, "Error validating access token", False))
        result = client.me()
        self.assertEqual((result.ok, result.http, result.outcome, result.code), (False, 500, "transient", 190))
        self.assertEqual(fake.connections, 3, "one connection per call: nothing is retried")

    def test_error_fields_are_read_from_the_body(self):
        rows = [
            ({"error": {"message": "The comment is invalid for a private reply", "code": "100",
                        "error_subcode": "2534025"}}, 100, 2534025, "rejected", "already_replied"),
            ({"error": {"message": "Object does not exist", "code": 100, "subcode": 33}},
             100, 33, "rejected", "comment_gone"),
            ({"error": {"message": "API access blocked.", "type": "OAuthException", "code": 200}},
             200, None, "global", "blocked"),
            ({"error": {"message": "An unknown error occurred", "code": 1, "error_subcode": 0}},
             1, None, "uncertain", "code_1"),
            ({"error": {"message": ["not", "text"], "code": "seven"}}, None, None, "uncertain", "unparseable"),
        ]
        for body, code, subcode, outcome, reason in rows:
            with self.subTest(body=body):
                fake = self.fake(answer(400, body))
                result = self.client(fake.base).send_dm("17", "C1", DM)
                self.assertEqual((result.ok, result.http, result.code, result.subcode, result.outcome, result.reason),
                                 (False, 400, code, subcode, outcome, reason))
                message = body["error"]["message"]
                self.assertEqual(result.message, message if isinstance(message, str) else "")
                self.assertEqual(result.data, body)

    def test_each_call_is_classified_as_its_own_kind(self):
        fake = self.fake(answer(400, {"error": {"message": "Object with ID '1' does not exist", "code": 100,
                                                "error_subcode": 33}}))
        client = self.client(fake.base)
        rows = [
            (client.me, "error", "code_100"),
            (lambda: client.media("18123"), "gone", ""),
            (lambda: client.comments_page("18123"), "gone", ""),
            (client.my_media, "error", "code_100"),
            (lambda: client.refresh_token(TOKEN), "error", "code_100"),
            (lambda: client.send_dm("17", "C1", DM), "rejected", "comment_gone"),
            (lambda: client.public_reply("C1", "ok"), "rejected", "comment_gone"),
        ]
        for n, (call, outcome, reason) in enumerate(rows):
            with self.subTest(call=n):
                result = call()
                self.assertEqual((result.ok, result.http, result.code, result.subcode, result.outcome, result.reason),
                                 (False, 400, 100, 33, outcome, reason))
        fake.respond = answer(400, {"error": {"message": "Spam", "code": 20, "error_subcode": 1772107}})
        result = client.send_dm("17", "C1", DM)
        self.assertEqual((result.outcome, result.reason), ("rejected", "invalid"))
        result = client.public_reply("C1", "ok")
        self.assertEqual((result.outcome, result.reason), ("global", "spam"))

    def test_code_subcode_and_message_can_always_be_journalled(self):
        """SQLite refuses an integer over 64 bits and a lone surrogate; a failed journal write would hide the answer."""
        rows = [
            (b'{"error": {"message": "Invalid \\ud83d parameter", "code": 9223372036854775808, "error_subcode": 1e30}}',
             None, None, "unparseable", "Invalid ? parameter"),
            (b'{"error": {"message": "\\ud83d\\ude00", "code": 9223372036854775807, "error_subcode": -9223372036854775808}}',
             2 ** 63 - 1, -2 ** 63, f"unknown_code_{2 ** 63 - 1}", "\U0001f600"),
        ]
        journal = sqlite3.connect(":memory:")
        self.addCleanup(journal.close)
        journal.execute("CREATE TABLE attempts (http INTEGER, code INTEGER, subcode INTEGER, message TEXT, reason TEXT)")
        for body, code, subcode, reason, message in rows:
            with self.subTest(body=body):
                result = self.client(self.fake(answer(400, body)).base).send_dm("17", "C1", DM)
                self.assertEqual((result.http, result.code, result.subcode, result.outcome, result.reason, result.message),
                                 (400, code, subcode, "uncertain", reason, message))
                journal.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
                                (result.http, result.code, result.subcode, result.message, result.reason))
        self.assertEqual(journal.execute("SELECT count(*) FROM attempts").fetchone(), (2,))

    def test_whole_account_refusals(self):
        blocked = {"error": {"message": "API access blocked.", "type": "OAuthException", "code": 200}}
        fake = self.fake(answer(400, blocked))
        result = self.client(fake.base).me()
        self.assertEqual((result.ok, result.http, result.outcome, result.reason, result.code),
                         (False, 400, "global", "blocked", 200))
        fake.respond = answer(429, b"<html>Too Many Requests</html>")
        for result in (self.client(fake.base).me(), self.client(fake.base).send_dm("17", "C1", DM)):
            self.assertEqual((result.ok, result.http, result.outcome, result.reason, result.data),
                             (False, 429, "global", "rate", {}))

    def test_a_body_that_is_not_a_json_object_never_raises(self):
        bodies = [b"<html><body>502 Bad Gateway</body></html>", b"", b"\xff\xfe\x00\x01 not text", b'{"message_id": "m1"',
                  b'["message_id", "m1"]', b'"m1"', b"null", b"12345", b"NaN", b"[" * 100000,
                  b'{"error": {"code": 190}} trailing']
        for body in bodies:
            for status, post, get in ((200, "ok_without_id", "unparseable"), (400, "unparseable", "unparseable")):
                with self.subTest(body=body[:30], status=status):
                    fake = self.fake(answer(status, body))
                    result = self.client(fake.base).send_dm("17", "C1", DM)
                    self.assertEqual((result.ok, result.http, result.outcome, result.reason), (False, status, "uncertain", post))
                    self.assertEqual((result.data, result.code, result.subcode, result.message), ({}, None, None, ""))
                    result = self.client(fake.base).comments_page("18123")
                    self.assertEqual((result.ok, result.http, result.outcome, result.reason), (False, status, "error", get))
                    self.assertEqual(result.data, {})

    def test_a_redirect_is_an_answer_and_is_not_followed(self):
        fake = self.fake(answer(302, b"", headers=[("Location", "/v26.0/elsewhere")]))
        client = self.client(fake.base)
        result = client.send_dm("17", "C1", DM)
        self.assertEqual((result.ok, result.http, result.outcome, result.reason), (False, 302, "uncertain", "unparseable"))
        result = client.me()
        self.assertEqual((result.ok, result.http, result.outcome, result.reason), (False, 302, "error", "unparseable"))
        self.assertEqual([path(seen) for seen in fake.received(2)], ["/v26.0/17/messages", "/v26.0/me"])

    def test_an_answer_that_arrives_in_pieces_is_read_whole(self):
        fake = self.fake(in_pieces)
        result = self.client(fake.base).send_dm("17", "C1", DM)
        self.assertEqual((result.ok, result.http, result.outcome, result.data), (True, 200, "ok", SENT))


class NotSent(Case):
    """Only a failure while connecting proves that the request never left: connect_failed."""

    def test_connection_refused(self):
        client = self.client(f"http://127.0.0.1:{closed_port()}")
        self.assert_not_connected(client.send_dm("17", "C1", DM), "not_sent", "connect")
        self.assert_not_connected(client.public_reply("C1", "ok"), "not_sent", "connect")
        result = client.me()
        self.assert_not_connected(result, "transient", "")
        self.assertIn("ConnectionRefusedError", result.message)

    def test_dns_failure_and_ipv4_only_lookup(self):
        lookups = []

        def no_such_host(host, port, deadline):
            lookups.append(host)
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

        with mock.patch.object(meta, "_resolve", no_such_host):
            result = self.client("http://graph.example.invalid").send_dm("17", "C1", DM)
        self.assert_not_connected(result, "not_sent", "connect")
        self.assertIn("gaierror", result.message)
        self.assertEqual(lookups, ["graph.example.invalid"])

    def test_every_lookup_asks_for_ipv4_only(self):
        fake = self.fake(answer(200, SENT))
        with mock.patch.object(socket, "getaddrinfo", wraps=socket.getaddrinfo) as lookup:
            self.assertTrue(self.client(fake.base).send_dm("17", "C1", DM).ok)
            self.assertTrue(self.client(fake.base.replace("127.0.0.1", "localhost")).me().ok)
        self.assertEqual(lookup.call_count, 0, "numeric IPv4 is direct and hostname DNS runs only in its child")

    def test_the_next_address_is_tried_after_one_that_refuses(self):
        fake = self.fake(answer(200, SENT))
        ports = [closed_port(), fake.server.server_port]

        def two_addresses(host, port, deadline):
            return [("127.0.0.1", port) for port in ports]

        with mock.patch.object(meta, "_resolve", two_addresses):
            result = self.client("http://graph.example.invalid").send_dm("17", "C1", DM)
            self.assertEqual((result.ok, result.outcome, result.connect_failed, result.message), (True, "ok", False, ""))
            self.assertEqual(len(fake.requests), 1)
            ports[1] = closed_port()
            result = self.client("http://graph.example.invalid").send_dm("17", "C1", DM)
        self.assert_not_connected(result, "not_sent", "connect")
        self.assertIn("ConnectionRefusedError", result.message)
        self.assertEqual(len(fake.requests), 1)

    def test_connect_timeout(self):
        fake = self.fake(answer(200, SENT))
        with mock.patch.object(meta._Socket, "connect", side_effect=TimeoutError("timed out")) as connect:
            result = self.client(fake.base).send_dm("17", "C1", DM)
        self.assert_not_connected(result, "not_sent", "connect")
        self.assertEqual((connect.call_count, fake.connections, result.message), (1, 0, "TimeoutError: timed out"))

    def test_deadline_already_over_before_connecting(self):
        fake = self.fake(answer(200, SENT))
        self.timeouts(1, -1)
        self.assert_not_connected(self.client(fake.base).send_dm("17", "C1", DM), "not_sent", "connect")
        self.assertEqual(fake.connections, 0)

    def raw_peer(self, reply):
        """A listener that takes one connection, reads once and sends `reply` (None: stays silent)."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        got, done = [], threading.Event()

        def peer():
            conn, _ = listener.accept()
            with conn:
                got.append(conn.recv(65536))
                if reply is None:
                    done.wait(10)
                else:
                    conn.sendall(reply)

        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(done.set)
        return f"https://127.0.0.1:{listener.getsockname()[1]}", got

    def test_tls_handshake_with_a_peer_that_does_not_speak_tls(self):
        base, got = self.raw_peer(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        result = self.client(base).send_dm("17", "C1", DM)
        self.assert_not_connected(result, "not_sent", "connect")
        self.assertIn("SSLError", result.message)
        self.assertNotIn(b"POST", got[0], "only the TLS hello left this machine")

    def test_tls_handshake_that_gets_no_answer(self):
        base, got = self.raw_peer(None)
        self.timeouts(0.2, 30)
        started = time.monotonic()
        result = self.client(base).send_dm("17", "C1", DM)
        self.assertTrue(0.15 <= time.monotonic() - started < 3)
        self.assert_not_connected(result, "not_sent", "connect")
        self.assertIn("TimeoutError", result.message)
        self.assertNotIn(b"POST", got[0])


class MaybeSent(Case):
    """After connecting, a POST without a complete answer may have reached Meta: uncertain, never not_sent."""

    def test_post_read_then_dropped_without_an_answer(self):
        for respond in (hang_up, reset):
            with self.subTest(respond=respond.__name__):
                fake = self.fake(respond)
                client = self.client(fake.base)
                result = client.send_dm("17", "C1", DM)
                self.assert_no_answer(result, "uncertain", "no_response")
                self.assertTrue(result.message)
                self.assertEqual(json.loads(fake.requests[0].body)["recipient"], {"comment_id": "C1"},
                                 "the peer did get the whole request")
                self.assert_no_answer(client.public_reply("C1", "ok"), "uncertain", "no_response")
                self.assert_no_answer(client.me(), "transient", "")
                self.assertEqual(fake.connections, 3, "one connection per call: nothing is retried")

    def test_write_that_fails_after_the_whole_request_left(self):
        real_sendall = meta._Socket.sendall
        for error in (BrokenPipeError(32, "Broken pipe"), ConnectionResetError(54, "Connection reset by peer")):
            with self.subTest(error=type(error).__name__):
                fake = self.fake(hang_up)
                sent = bytearray()

                def sendall(sock, data):
                    real_sendall(sock, data)
                    sent.extend(data)
                    head, end, body = bytes(sent).partition(b"\r\n\r\n")
                    length = re.search(rb"(?im)^content-length:\s*(\d+)", head)
                    if end and len(body) >= int(length.group(1) if length else 0):
                        raise error

                with mock.patch.object(meta._Socket, "sendall", sendall):
                    result = self.client(fake.base).send_dm("17", "C1", DM)
                    self.assert_no_answer(result, "uncertain", "no_response")
                    self.assertEqual(json.loads(fake.received()[0].body), {"recipient": {"comment_id": "C1"}, "message": DM})
                    sent.clear()
                    self.assert_no_answer(self.client(fake.base).public_reply("C1", "ok"), "uncertain", "no_response")
                    sent.clear()
                    self.assert_no_answer(self.client(fake.base).me(), "transient", "")
                self.assertEqual((len(fake.received(3)), fake.connections), (3, 3))

    def test_peer_that_answers_garbage(self):
        fake = self.fake(lambda handler: handler.wfile.write(b"\x00\x01 this is not HTTP\r\n\r\n"))
        self.assert_no_answer(self.client(fake.base).send_dm("17", "C1", DM), "uncertain", "no_response")
        self.assert_no_answer(self.client(fake.base).me(), "transient", "")

    def test_answer_cut_off_in_the_middle(self):
        fake = self.fake(lambda handler: handler.wfile.write(
            b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{"message_id": "m1"}'))
        self.assert_no_answer(self.client(fake.base).send_dm("17", "C1", DM), "uncertain", "no_response")
        self.assert_no_answer(self.client(fake.base).comments_page("18123"), "transient", "")

    def test_silent_peer_runs_into_the_socket_timeout(self):
        fake = self.fake(silent)
        self.timeouts(0.2, 30)
        for call, outcome in ((lambda c: c.send_dm("17", "C1", DM), "uncertain"), (lambda c: c.me(), "transient")):
            started = time.monotonic()
            result = call(self.client(fake.base))
            took = time.monotonic() - started
            self.assert_no_answer(result, outcome, "no_response" if outcome == "uncertain" else "")
            self.assertIn("TimeoutError", result.message)
            self.assertTrue(0.15 <= took < 3, took)
        self.assertEqual(len(fake.received(2)), 2)
        self.assertEqual(fake.connections, 2)

    def test_silent_peer_is_dropped_at_the_deadline_when_that_comes_first(self):
        fake = self.fake(silent)
        self.timeouts(8, 0.3)
        for call, outcome in ((lambda c: c.send_dm("17", "C1", DM), "uncertain"), (lambda c: c.me(), "transient")):
            started = time.monotonic()
            result = call(self.client(fake.base))
            took = time.monotonic() - started
            self.assert_no_answer(result, outcome, "no_response" if outcome == "uncertain" else "")
            self.assertTrue(0.27 <= took < 3, took)

    def test_no_send_or_receive_starts_after_the_deadline(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.timeouts(5, 60)
        sock = meta._connect("127.0.0.1", listener.getsockname()[1], time.monotonic() + 60, tls=False)
        self.addCleanup(sock.close)
        peer, _ = listener.accept()
        self.addCleanup(peer.close)
        sock.sendall(b"one ")
        self.assertEqual(sock.gettimeout(), 5, "the socket timeout while the deadline is far")
        sock.deadline = time.monotonic() + 2
        sock.sendall(b"two ")
        self.assertTrue(0 < sock.gettimeout() <= 2, "what is left of the call when that is less")
        sock.deadline = time.monotonic()
        for operation in (lambda: sock.sendall(b"three"), lambda: sock.recv_into(bytearray(1))):
            with self.assertRaises(TimeoutError):
                operation()
        sock.close()
        peer.settimeout(5)
        self.assertEqual(b"".join(iter(lambda: peer.recv(100), b"")), b"one two ", "nothing left after the deadline")

    def test_slow_drip_runs_into_the_deadline(self):
        for respond in (drip_body, drip_headers):
            with self.subTest(respond=respond.__name__):
                fake = self.fake(respond)
                self.timeouts(0.2, 0.3)  # a byte every 20 ms: the socket timeout alone would never fire
                for call, outcome in ((lambda c: c.send_dm("17", "C1", DM), "uncertain"),
                                      (lambda c: c.public_reply("C1", "ok"), "uncertain"),
                                      (lambda c: c.comments_page("18123"), "transient")):
                    started = time.monotonic()
                    result = call(self.client(fake.base))
                    took = time.monotonic() - started
                    self.assert_no_answer(result, outcome, "no_response" if outcome == "uncertain" else "")
                    self.assertIn("TimeoutError", result.message)
                    self.assertTrue(0.27 <= took < 1.5, took)
                self.assertEqual(fake.connections, 3)


class Secrets(Case):
    """The token is in every URL and must not come back out in a Result."""

    def assert_clean(self, result, *tokens):
        for token in tokens or (TOKEN,):
            for form in (token, urllib.parse.quote(token, safe=""), urllib.parse.quote_plus(token)):
                self.assertNotIn(form, result.message)
                self.assertNotIn(form, repr(result))

    def test_failures_do_not_carry_the_token(self):
        echo = {"error": {"message": f"Malformed access token {TOKEN}", "type": "OAuthException", "code": 190,
                          "error_user_msg": f"Check {TOKEN}"}}
        self.timeouts(0.1, 0.2)
        peers = [answer(400, echo), answer(500, echo), answer(200, echo), answer(429, TOKEN.encode()),
                 answer(400, f"<html>{TOKEN}</html>".encode()), hang_up, reset, silent, drip_body]
        for respond in peers:
            fake = self.fake(respond)
            client = self.client(fake.base)
            calls = [client.me, lambda: client.media("18123"), lambda: client.comments_page("18123"), client.my_media,
                     lambda: client.send_dm("17", "C1", DM), lambda: client.public_reply("C1", "ok"),
                     lambda: client.refresh_token(TOKEN)]
            for n, call in enumerate(calls):
                with self.subTest(respond=getattr(respond, "__name__", "answer"), call=n):
                    result = call()
                    self.assertFalse(result.ok)
                    self.assert_clean(result)
        result = self.client(self.fake(answer(400, echo)).base).send_dm("17", "C1", DM)
        self.assertEqual((result.message, result.outcome, result.reason), ("Malformed access token <token>", "global", "auth"))
        self.assertEqual(result.data, {"error": {"message": "Malformed access token <token>", "type": "OAuthException",
                                                 "code": 190, "error_user_msg": "Check <token>"}})

    def test_failures_before_and_while_connecting_do_not_carry_the_token(self):
        refused = self.client(f"http://127.0.0.1:{closed_port()}")
        for result in (refused.me(), refused.send_dm("17", "C1", DM), refused.refresh_token(TOKEN)):
            self.assertTrue(result.connect_failed)
            self.assert_clean(result)

    def test_a_next_url_that_cannot_be_requested(self):
        fake = self.fake(answer(200, {"data": []}))
        client = self.client(fake.base)
        awkward = "IGAA|awk ward+tok/en=%"
        for token in (TOKEN, awkward):
            quoted = urllib.parse.quote_plus(token)
            for url in (f"no-scheme/v26.0/1/comments?access_token={quoted}&after=x",
                        f"ftp://127.0.0.1/v26.0/1/comments?after=x&access_token={quoted}",
                        f"{fake.base}/v26.0/1/com ments?access_token={quoted}",
                        f"http://127.0.0.1:port/v26.0/1/comments?access_token={quoted}#frag",
                        f"http://[::1/v26.0?access_token={quoted}"):
                with self.subTest(url=url):
                    result = client.comments_page("18123", url)
                    self.assertEqual((result.ok, result.http, result.data), (False, 0, {}))
                    self.assertIn(result.outcome, ("transient", "error"))
                    self.assertTrue(result.message)
                    self.assert_clean(result, token)
        self.assertEqual(fake.requests, [])
        for junk in (12345, {"next": "x"}, ["x"]):
            result = client.my_media(junk)
            self.assertEqual((result.ok, result.http), (False, 0))

    def test_meta_echoing_an_awkward_token_in_either_spelling(self):
        awkward = "IGAA|awk ward+tok/en=%"
        quoted = urllib.parse.quote_plus(awkward)
        echo = {"error": {"message": f"Bad token {awkward} in /me?access_token={quoted}", "code": 190}}
        fake = self.fake(answer(400, echo))
        result = meta.Client(fake.base, "v26.0", lambda: awkward).me()
        self.assertEqual(query(fake.requests[0])["access_token"], [awkward])
        self.assertIn("Bad token <token>", result.message)
        self.assertNotIn("access_token=", result.message)
        self.assert_clean(result, awkward)

    def test_a_successful_refresh_returns_the_token_untouched(self):
        fake = self.fake(answer(200, {"access_token": TOKEN, "token_type": "bearer", "expires_in": 5183944}))
        result = self.client(fake.base).refresh_token(TOKEN)
        self.assertEqual((result.ok, result.outcome), (True, "ok"))
        self.assertEqual(result.data, {"access_token": TOKEN, "token_type": "bearer", "expires_in": 5183944},
                         "Meta may hand back the same string (meta-api-facts F6): it must not be masked")

    def test_a_long_failure_text_is_replaced_with_a_safe_diagnostic(self):
        fake = self.fake(lambda handler: handler.wfile.write(b"x" * 60000 + b"\r\n\r\n"))
        result = self.client(fake.base).send_dm("17", "C1", DM)
        self.assert_no_answer(result, "uncertain", "no_response")
        self.assertEqual(result.message, "BadStatusLine: HTTP exchange failed")


class TLS(Case):
    """The production path: https, certificate checks, and the deadline on the TLS socket."""

    @classmethod
    def setUpClass(cls):
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as pem:
            pem.write(CERT + KEY)
        cls.addClassCleanup(os.unlink, pem.name)
        cls.server_side = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_side.load_cert_chain(pem.name)

    def trust(self):
        context = meta._tls_context()
        context.load_verify_locations(cadata=CERT)
        patch = mock.patch.object(meta, "_tls", context)
        patch.start()
        self.addCleanup(patch.stop)

    def test_calls_over_tls(self):
        fake = self.fake(answer(200, SENT), self.server_side)
        self.trust()
        client = self.client(fake.base)
        result = client.send_dm("17", "C1", DM)
        self.assertEqual((result.ok, result.http, result.outcome, result.data), (True, 200, "ok", SENT))
        self.assertEqual(json.loads(fake.requests[0].body), {"recipient": {"comment_id": "C1"}, "message": DM})
        self.assertEqual(query(fake.requests[0]), {"access_token": [TOKEN]})
        self.assertTrue(client.me().ok)
        fake.respond = in_pieces
        self.assertEqual(client.send_dm("17", "C2", DM).data, SENT)

    def test_slow_drip_over_tls_runs_into_the_deadline(self):
        fake = self.fake(drip_body, self.server_side)
        self.trust()
        self.timeouts(0.2, 0.3)
        for call, outcome in ((lambda c: c.send_dm("17", "C1", DM), "uncertain"), (lambda c: c.me(), "transient")):
            started = time.monotonic()
            result = call(self.client(fake.base))
            took = time.monotonic() - started
            self.assert_no_answer(result, outcome, "no_response" if outcome == "uncertain" else "")
            self.assertIn("TimeoutError", result.message)
            self.assertTrue(0.27 <= took < 1.5, took)

    def test_silent_peer_over_tls_runs_into_the_socket_timeout(self):
        fake = self.fake(silent, self.server_side)
        self.trust()
        self.timeouts(0.2, 30)
        started = time.monotonic()
        result = self.client(fake.base).send_dm("17", "C1", DM)
        self.assert_no_answer(result, "uncertain", "no_response")
        self.assertTrue(0.15 <= time.monotonic() - started < 3)

    def test_untrusted_certificate_is_a_failed_connect(self):
        fake = self.fake(answer(200, SENT), self.server_side)
        result = self.client(fake.base).send_dm("17", "C1", DM)
        self.assert_not_connected(result, "not_sent", "connect")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", result.message)
        self.assertEqual(fake.requests, [])

    def test_certificate_for_another_name_is_a_failed_connect(self):
        fake = self.fake(answer(200, SENT), self.server_side)
        self.trust()
        result = self.client(fake.base.replace("127.0.0.1", "localhost")).send_dm("17", "C1", DM)
        self.assert_not_connected(result, "not_sent", "connect")
        self.assertEqual(fake.requests, [])


if __name__ == "__main__":
    unittest.main()
