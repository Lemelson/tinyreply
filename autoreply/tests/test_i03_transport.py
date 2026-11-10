"""I03 transport regressions: real loopback HTTP and isolated DNS helper faults only."""
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import tracemalloc
import unittest
import urllib.parse
from unittest import mock

import meta
from tests.test_meta_client import Case, DM, SENT, TOKEN, answer


class DNS(Case):
    def child_factory(self, script, creation_delay=0):
        real_popen, children = subprocess.Popen, []

        def create(*args, **kwargs):
            time.sleep(creation_delay)
            child = real_popen([sys.executable, "-I", "-S", "-c", script], **kwargs)
            children.append(child)
            return child
        return create, children

    def test_slow_dns_obeys_remaining_deadline_and_child_is_reaped(self):
        real_popen, real_lookup = subprocess.Popen, socket.getaddrinfo
        children = []

        def slow_child(*args, **kwargs):
            command = [sys.executable, "-I", "-S", "-c", "import time; time.sleep(3)"]
            child = real_popen(command, **kwargs)
            children.append(child)
            return child

        def slow_parent(*args, **kwargs):
            time.sleep(0.7)
            return real_lookup("127.0.0.1", 1, socket.AF_INET, socket.SOCK_STREAM)

        started = time.monotonic()
        with mock.patch.object(subprocess, "Popen", slow_child), mock.patch.object(socket, "getaddrinfo", slow_parent):
            status, data, failed, failure = meta.exchange("POST", "http://localhost:1/test", {}, 0.06)
        self.assertLess(time.monotonic() - started, 0.5, "resolver must stop waiting at the call deadline")
        self.assertEqual((status, data, failed), (0, None, True))
        self.assertIn("TimeoutError", failure)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(children[0].pid, 0)

    def test_numeric_ipv4_uses_no_resolver_or_child(self):
        fake = self.fake(answer(200, SENT))
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawn")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("lookup")):
            self.assertTrue(self.client(fake.base).send_dm("17", "C1", DM).ok)
        self.assertEqual(len(fake.received()), 1)

    def test_exhausted_deadline_spawns_no_hostname_helper(self):
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawn")):
            result = meta.exchange("POST", "http://localhost:1/test", {}, -1)
        self.assertEqual(result[:3], (0, None, True))
        self.assertIn("TimeoutError", result[3])

    def test_real_localhost_helper_returns_only_ipv4_and_is_reaped(self):
        real_popen, children = subprocess.Popen, []

        def create(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with mock.patch.object(subprocess, "Popen", create):
            addresses = meta._resolve("localhost", 80, time.monotonic() + 2)
        self.assertTrue(addresses)
        for host, port in addresses:
            socket.inet_pton(socket.AF_INET, host)
            self.assertEqual(port, 80)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].returncode, 0)

    def test_process_creation_consumes_the_absolute_budget(self):
        create, children = self.child_factory("import time; time.sleep(3)", creation_delay=0.08)
        with mock.patch.object(subprocess, "Popen", create):
            result = meta.exchange("POST", "http://localhost:1/test", {}, 0.04)
        self.assertEqual(result[:3], (0, None, True))
        self.assertIn("TimeoutError", result[3])
        self.assertIsNotNone(children[0].returncode)

    def test_invalid_helper_output_cannot_produce_any_http_request(self):
        fake = self.fake(answer(200, SENT))
        port = fake.server.server_port
        outputs = ["not JSON", "[]", '{"error":"dns"}', '[ ["::1", %d] ]' % port,
                   '[ ["127.0.0.1", %d] ]' % (port + 1)]
        for output in outputs:
            script = "print(" + repr(output) + ")"
            create, children = self.child_factory(script)
            with mock.patch.object(subprocess, "Popen", create):
                result = self.client(fake.base.replace("127.0.0.1", "localhost")).send_dm("17", "C1", DM)
            self.assertEqual((result.http, result.outcome, result.connect_failed), (0, "not_sent", True))
            self.assertIsNotNone(children[0].returncode)
        create, children = self.child_factory(
            "import sys; print(" + repr(json.dumps([["127.0.0.1", port]])) + "); sys.exit(17)")
        with mock.patch.object(subprocess, "Popen", create):
            result = self.client(fake.base.replace("127.0.0.1", "localhost")).send_dm("17", "C1", DM)
        self.assertEqual((result.http, result.outcome, result.connect_failed), (0, "not_sent", True))
        self.assertEqual(children[0].returncode, 17)
        self.assertEqual(fake.requests, [])

    def test_dns_and_body_wait_share_one_absolute_deadline(self):
        fake = self.fake(lambda handler: handler.server.stop.wait(3))
        script = "import time; time.sleep(0.12); print(" + repr(json.dumps([["127.0.0.1", fake.server.server_port]])) + ")"
        create, children = self.child_factory(script)
        self.timeouts(2, 0.18)
        started = time.monotonic()
        with mock.patch.object(subprocess, "Popen", create):
            result = self.client(fake.base.replace("127.0.0.1", "localhost")).send_dm("17", "C1", DM)
        self.assertLess(time.monotonic() - started, 0.27, "socket wait must receive only the leftover DNS budget")
        self.assertEqual((result.http, result.outcome, result.connect_failed), (0, "uncertain", False))
        self.assertEqual(len(fake.received()), 1)
        self.assertIsNotNone(children[0].returncode)

    def test_repeated_get_timeouts_do_not_leave_children(self):
        create, children = self.child_factory("import time; time.sleep(3)")
        with mock.patch.object(subprocess, "Popen", create):
            for _ in range(3):
                status, parsed, failed, _ = meta.exchange("GET", "http://localhost:1/test", None, 0.03)
                self.assertEqual(meta.classify("GET", "read_me", status, parsed, failed), ("transient", ""))
        self.assertEqual(len(children), 3)
        for child in children:
            self.assertIsNotNone(child.returncode)
            with self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)

    def test_sigterm_during_dns_finishes_call_and_reaps_child(self):
        script = '''
import json, os, signal, subprocess, sys
from unittest import mock
sys.path.insert(0, CODE_PATH)
import meta
stopped, children = [], []
signal.signal(signal.SIGTERM, lambda *args: stopped.append(True))
real_popen = subprocess.Popen
def create(*args, **kwargs):
    child = real_popen([sys.executable, "-I", "-S", "-c", "import time; time.sleep(3)"], **kwargs)
    children.append(child)
    print(child.pid, flush=True)
    return child
with mock.patch.object(subprocess, "Popen", create):
    status, parsed, failed, failure = meta.exchange("POST", "http://localhost:1/test", {}, 0.2)
print(json.dumps({"result": [status, parsed, failed], "signal": stopped, "exit": children[0].returncode}))
'''.replace("CODE_PATH", repr(os.path.dirname(meta.__file__)))
        with subprocess.Popen([sys.executable, "-B", "-c", script], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as runner:
            child_pid = int(runner.stdout.readline())
            os.kill(runner.pid, signal.SIGTERM)
            stdout, stderr = runner.communicate(timeout=3)
        self.assertEqual(runner.returncode, 0, stderr)
        observed = json.loads(stdout)
        self.assertEqual(observed["result"], [0, None, True])
        self.assertEqual(observed["signal"], [True])
        self.assertIsNotNone(observed["exit"])
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)


class Responses(Case):
    LIMIT = 1024 * 1024

    @staticmethod
    def safe_answer(payload):
        respond = answer(200, payload)
        def write(handler):
            try:
                respond(handler)
            except OSError:
                pass  # an oversize header is rejected before the peer finishes its body write
        return write

    @staticmethod
    def payload(size):
        head, tail = b'{"message_id":"m1","pad":"', b'"}'
        return head + b"x" * (size - len(head) - len(tail)) + tail

    def test_large_declared_length_does_not_allocate_the_declared_size(self):
        fake = self.fake(lambda handler: handler.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 33554432\r\n\r\nx"))
        tracemalloc.start()
        try:
            result = self.client(fake.base).send_dm("17", "C1", DM)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024, "untrusted Content-Length must not size a read allocation")
        self.assertEqual((result.http, result.outcome, result.connect_failed), (0, "uncertain", False))
        self.assertEqual(len(fake.received()), 1)

    def test_dense_json_larger_than_budget_is_rejected_before_container_allocation(self):
        payload = b'{"data":[' + b'{"":0},' * 299000 + b'{"":0}]}'
        fake = self.fake(self.safe_answer(payload))
        tracemalloc.start()
        try:
            result = self.client(fake.base).me()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 16 * 1024 * 1024, "a byte cap must also prevent dense JSON expanding beyond memory headroom")
        self.assertEqual((result.http, result.outcome, result.connect_failed), (0, "transient", False))

    def test_healthy_comments_page_with_fifty_comments_is_accepted(self):
        page = {"data": [{"id": str(index), "text": "арена " * 400,
                          "timestamp": "2026-10-06T00:00:00Z", "from": {"id": str(index + 100), "username": "user"},
                          "hidden": False, "replies": {"data": [{"id": f"reply-{index}", "from": {"id": "owner"}}]}}
                         for index in range(50)]}
        payload = json.dumps(page, ensure_ascii=False).encode()
        self.assertLess(len(payload), self.LIMIT)
        fake = self.fake(self.safe_answer(payload))
        result = self.client(fake.base).comments_page("18123")
        self.assertTrue(result.ok)
        self.assertEqual(result.data, page)

    def test_exact_limit_is_accepted_but_limit_plus_one_is_incomplete(self):
        fake = self.fake(self.safe_answer(self.payload(self.LIMIT)))
        self.assertTrue(self.client(fake.base).send_dm("17", "C1", DM).ok)
        fake.respond = self.safe_answer(self.payload(self.LIMIT + 1))
        client = self.client(fake.base)
        for result, outcome in ((client.send_dm("17", "C2", DM), "uncertain"), (client.me(), "transient")):
            self.assertEqual((result.http, result.data, result.outcome, result.connect_failed), (0, {}, outcome, False))
        self.assertEqual(len(fake.received(3)), 3, "oversized received POST must never be retried")

    def test_chunked_and_close_delimited_limit_plus_one_are_incomplete(self):
        payload = self.payload(self.LIMIT + 1)

        def respond(handler, chunked):
            if chunked:
                head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                wire = (f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
            else:
                head, wire = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n", payload
            try:
                handler.wfile.write(head + wire)
            except OSError:
                pass  # the client may close as soon as its response budget is exceeded

        for chunked in (True, False):
            fake = self.fake(lambda handler: respond(handler, chunked))
            client = self.client(fake.base)
            for result, outcome in ((client.send_dm("17", "C1", DM), "uncertain"), (client.me(), "transient")):
                self.assertEqual((result.http, result.outcome, result.connect_failed), (0, outcome, False))
            self.assertEqual(len(fake.received(2)), 2)

    def test_valid_json_prefix_does_not_complete_a_truncated_fixed_length_body(self):
        fake = self.fake(lambda handler: handler.wfile.write(
            b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{"message_id":"m1"}'))
        result = self.client(fake.base).send_dm("17", "C1", DM)
        self.assertEqual((result.http, result.outcome, result.connect_failed), (0, "uncertain", False))


class Privacy(Case):
    CURSOR = "I03_PRIVATE_CURSOR_a9c72"

    def test_transport_and_client_malformed_selector_errors_hide_all_query_values(self):
        client = self.client("http://127.0.0.1:1")
        for encoded in (False, True):
            cursor = urllib.parse.quote(self.CURSOR, safe="") if encoded else self.CURSOR
            url = f"http://127.0.0.1:1/bad path?after={cursor}&access_token={TOKEN}"
            failure = meta.exchange("GET", url, None, 1)[3]
            result = client.comments_page("18123", url)
            for text in (failure, result.message):
                self.assertNotIn(self.CURSOR, text)
                self.assertNotIn(TOKEN, text)
                self.assertNotIn("after=", text)
                self.assertIn("InvalidURL", text)

    def test_error_strings_cannot_echo_literal_or_encoded_query(self):
        url = f"http://127.0.0.1:1/page?after={self.CURSOR}&access_token={TOKEN}"
        for echo in (url, urllib.parse.quote(url, safe=""), f"after={self.CURSOR}"):
            parsed = {"error": {"code": 100, "message": f"Invalid cursor {echo}", "detail": echo}}
            with mock.patch.object(meta, "exchange", return_value=(400, parsed, False, "")):
                result = self.client("http://127.0.0.1:1").comments_page("18123", url)
            serialized = json.dumps({"message": result.message, "error": result.data["error"]})
            self.assertNotIn(self.CURSOR, serialized)
            self.assertNotIn(TOKEN, serialized)
            self.assertNotIn("after=", serialized)


class Completion(Case):
    def test_client_completion_is_logged_before_return_and_before_late_fake_write_timestamp(self):
        finish, fake_complete, pings = threading.Event(), [], []
        self.addCleanup(finish.set)

        def delayed_peer_marker(handler):
            answer(200, SENT)(handler)
            finish.wait(3)  # scheduling after write cannot be used as a lower client-completion bound
            fake_complete.append(time.monotonic())

        fake = self.fake(delayed_peer_marker)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            result = self.client(fake.base, lambda: pings.append(time.monotonic())).send_dm("17", "C1", DM)
            returned_at = time.monotonic()
        self.assertTrue(result.ok)
        lines = [line for line in captured.getvalue().splitlines() if " INFO http_complete " in line]
        self.assertEqual(len(lines), 1, "production Client must expose actual completion before its result returns")
        fields = dict(word.split("=", 1) for word in lines[0].split()[3:])
        self.assertEqual((fields["pid"], fields["method"], fields["kind"], fields["route"], fields["http"]),
                         (str(os.getpid()), "POST", "dm", "/v26.0/17/messages", "200"))
        self.assertTrue(pings[0] <= float(fields["monotonic"]) <= returned_at)
        self.assertGreater(int(fields["call"]), 0)
        self.assertNotIn(TOKEN, captured.getvalue())
        self.assertNotIn("access_token", captured.getvalue())
        self.assertNotIn("recipient", captured.getvalue())
        self.assertEqual(fake_complete, [])
        finish.set()


if __name__ == "__main__":
    unittest.main()
