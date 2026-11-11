"""Meta (Instagram Login) API client, SPEC section 6: one HTTP exchange per call and never a retry.

What this module guarantees: a POST is `not_sent` only when connecting failed, because that is the only
failure that proves the request never reached Meta. Every other answer that is not a clear one is
`uncertain`, and the caller never sends that comment again on its own (G4).
"""
import http.client
import itertools
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from clock import T, iso

# ---------- classification (6.1) ----------

_ANY = None  # as the subcode of a table key: whatever the subcode is

_ALREADY_REPLIED = ("already has a reply", "already replied")

# Documented as temporary, yet this project got code 1 for a DM that was delivered (meta-api-facts E).
_AMBIGUOUS = (1, -1, 2)

# Rule 9: about this one comment or person.
_PER_ITEM = {
    (551, _ANY): "user_unavailable", (200, 1545041): "user_unavailable", (10903, _ANY): "user_unavailable",
    (230, _ANY): "user_unavailable", (100, 2018001): "user_unavailable", (100, 2534014): "user_unavailable",
    (100, 2534001): "user_unavailable", (10, 2018108): "user_unavailable", (10, 1893015): "user_unavailable",
    (100, 33): "comment_gone", (24, _ANY): "comment_gone", (803, _ANY): "comment_gone",
    (10, 2534022): "outside_window", (10, 2018278): "outside_window", (10, 2018065): "outside_window",
    (100, 2534015): "invalid", (100, 2534037): "invalid", (10, 2018300): "invalid",
}

# Rule 10: about the whole account, app or token. An exact (code, subcode) wins over (code, _ANY).
_ACCOUNT_WIDE = {
    (190, _ANY): "auth",
    (368, _ANY): "spam", (613, 2018338): "spam", (4, 2207051): "spam", (508, _ANY): "spam", (10, 1893063): "spam",
    (4, _ANY): "rate", (17, _ANY): "rate", (32, _ANY): "rate", (341, _ANY): "rate", (613, _ANY): "rate",
    (3, _ANY): "blocked", (10, _ANY): "blocked", (25, _ANY): "blocked", (100, 2534029): "blocked",
}


def _int(value):
    """int(value), or None for anything that is not a whole number an INTEGER column of the journal can hold.

    190.7 is not code 190: read as 190 it would turn a garbage answer into an account-wide refusal, which is
    one of the two outcomes after which a comment is sent to again (G4).
    """
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if -2 ** 63 <= number < 2 ** 63 else None


def _error(body):
    """(code, subcode, message) of Meta's error object; (None, None, '') when the body has none."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None, ""
    message = error.get("message")
    return (_int(error.get("code")),
            _int(error.get("error_subcode")) or _int(error.get("subcode")) or None,
            message if isinstance(message, str) else "")


def _lookup(table, code, subcode):
    return table.get((code, subcode)) or table.get((code, _ANY))


def _account_wide(kind, code, subcode, message):
    reason = _lookup(_ACCOUNT_WIDE, code, subcode)
    if reason:
        return reason
    if kind == "public" and code == 20:
        return "spam"
    if 80000 <= code <= 80099:
        return "rate"
    if 200 <= code <= 299:
        return "blocked"
    if code == 100 and subcode is None and "Unsupported request" in message:
        return "blocked"
    return None


def _has_id(kind, body):
    field = "message_id" if kind in ("dm", "step") else "id" if kind == "public" else None
    value = body.get(field) if isinstance(body, dict) else None
    return isinstance(value, str) and bool(value.strip())


def classify(method, kind, http, body, connect_failed):
    """(outcome, reason) for one exchange. Pure; the first applicable rule of SPEC 6.1 wins.

    Any method other than GET gets the POST rules, the strict set.
    """
    http = _int(http) or 0
    code, subcode, message = _error(body)

    if method == "GET":
        if connect_failed or http == 0 or http >= 500 or code in _AMBIGUOUS:
            return "transient", ""
        if http == 429:
            return "global", "rate"
        if http == 200 and isinstance(body, dict) and "error" not in body:
            return "ok", ""
        if code is None:
            return "error", "unparseable"
        reason = _account_wide(kind, code, subcode, message)
        if reason:
            return "global", reason
        if kind in ("read_media", "read_comments") and _lookup(_PER_ITEM, code, subcode) == "comment_gone":
            return "gone", ""
        return "error", f"code_{code}"

    # An HTTP status contradicts "could not connect": then the flag is not trusted and nothing is resent.
    if connect_failed and http == 0:
        return "not_sent", "connect"
    if http == 0:
        return "uncertain", "no_response"
    if http >= 500:
        return "uncertain", "http_5xx"
    if http == 429:
        return "global", "rate"
    if 200 <= http < 300:
        return ("ok", "") if _has_id(kind, body) else ("uncertain", "ok_without_id")
    if code is None:
        return "uncertain", "unparseable"
    text = message.lower()
    if code == 10900 or (code, subcode) == (100, 2534025) or any(phrase in text for phrase in _ALREADY_REPLIED):
        return "rejected", "already_replied"
    if code in _AMBIGUOUS:
        return "uncertain", f"code_{code}"
    if 400 <= http < 500:
        reason = _lookup(_PER_ITEM, code, subcode) or ("invalid" if kind in ("dm", "step") and code == 20 else None)
        if reason:
            return "rejected", reason
    reason = _account_wide(kind, code, subcode, message)
    if reason:
        return "global", reason
    if code == 100:
        return "rejected", "invalid"
    return "uncertain", f"unknown_code_{code}"


# ---------- transport ----------

class _ConnectFailed(Exception):
    """Connecting failed (DNS, TCP connect, TLS handshake): no byte of the request left this machine.

    Not an OSError on purpose: urllib wraps every OSError of its send phase in URLError, and a write that
    failed after connecting may have reached Meta (v1 inventory H2, H3).
    """


class _Deadline:
    """Socket mixin: one send or receive may take the socket timeout, but none runs past the call's deadline,
    so a peer that drips a byte now and then cannot keep a call open."""

    deadline = 0.0

    def arm(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline of the call exceeded")
        self.settimeout(min(T["http_socket_timeout"], left))

    def sendall(self, *args):
        self.arm()
        return super().sendall(*args)

    def recv_into(self, *args):
        self.arm()
        return super().recv_into(*args)


class _Socket(_Deadline, socket.socket):
    pass


class _TLSSocket(_Deadline, ssl.SSLSocket):
    pass


def _tls_context():
    context = ssl.create_default_context()
    context.sslsocket_class = _TLSSocket
    return context


_tls = _tls_context()


_RESOLVER = """
import json, socket, sys
try:
    addresses = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), socket.AF_INET, socket.SOCK_STREAM)
    result = list(dict.fromkeys((a[4][0], a[4][1]) for a in addresses))[:64]
except OSError:
    result = {"error": "dns"}
print(json.dumps(result))
"""


def _resolve(host, port, deadline):
    """IPv4 addresses; only hostnames need one isolated DNS-only child, killed and reaped on failure.

    The wait uses the call's remaining deadline. OS process creation and cleanup are not hard realtime.
    The child receives only host/port, never a URL, request body, environment secrets or bot state.
    """
    if deadline <= time.monotonic():
        raise TimeoutError("deadline of the call exceeded")
    try:
        socket.inet_pton(socket.AF_INET, host)
        return [(host, port)]
    except OSError:
        pass
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c", _RESOLVER, host, str(port)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             close_fds=True, env={})
    try:
        left = deadline - time.monotonic()  # process creation consumed part of the absolute budget
        if left <= 0:
            raise TimeoutError("deadline of the call exceeded")
        try:
            output, _ = child.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            raise TimeoutError("DNS deadline of the call exceeded") from None
        if deadline <= time.monotonic():
            raise TimeoutError("deadline of the call exceeded")
        try:
            addresses = json.loads(output)
            if child.returncode or not isinstance(addresses, list) or not addresses or len(addresses) > 64:
                raise ValueError()
            for address in addresses:
                if not isinstance(address, list) or len(address) != 2 or address[1] != port:
                    raise ValueError()
                socket.inet_pton(socket.AF_INET, address[0])
        except (ValueError, TypeError, OSError):
            raise socket.gaierror(socket.EAI_NONAME, "IPv4 DNS lookup failed") from None
        return [tuple(address) for address in addresses]
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()  # reap before returning; no late resolver survives into another call


def _connect(host, port, deadline, tls):
    """A connected socket for one call. IPv4 only: the server has no working IPv6 route to Meta (v1 inventory H1)."""
    failure = OSError(f"no IPv4 address for {host}")
    try:
        for address in _resolve(host, port, deadline):
            sock = _Socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.deadline = deadline
            try:
                sock.arm()
                sock.connect(address)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # headers and body are two writes
                if tls:
                    sock.arm()  # the handshake runs under the timeout set here
                    sock = _tls.wrap_socket(sock, server_hostname=host)
                    sock.deadline = deadline
                return sock
            except OSError as e:
                sock.close()
                failure = e
        raise failure
    except OSError as e:
        raise _ConnectFailed() from e


class _HTTPConnection(http.client.HTTPConnection):
    tls = False

    def __init__(self, host, deadline, **kwargs):
        super().__init__(host, **kwargs)
        self.deadline = deadline

    def connect(self):
        self.sock = _connect(self.host, self.port, self.deadline, self.tls)


class _HTTPSConnection(_HTTPConnection):
    tls = True
    default_port = http.client.HTTPS_PORT


class _Handler(urllib.request.HTTPHandler):
    https_request = urllib.request.HTTPHandler.http_request

    def http_open(self, request):
        return self.do_open(_HTTPConnection, request, deadline=request.deadline)

    def https_open(self, request):
        return self.do_open(_HTTPSConnection, request, deadline=request.deadline)


# Private to this module, and bare on purpose: no proxy, no redirect (it would be a second request), no
# error processor (an answer with any status is returned, not raised).
_opener = urllib.request.OpenerDirector()
_opener.add_handler(_Handler())
_opener.add_handler(urllib.request.UnknownHandler())


def _parse(raw):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


_RESPONSE_LIMIT = 1024 * 1024


class _TooLarge(ValueError):
    """An answer above the limit; the class name is what the journal keeps (see _transport_failure)."""


def _read_response(response):
    """Bound bytes before parsing, independently of Content-Length and transfer framing."""
    expected = response.length  # as http.client parsed it; read(amt) does not itself reject an early fixed-length EOF
    if expected is not None and expected > _RESPONSE_LIMIT:
        raise _TooLarge("response exceeds 1 MiB")
    raw = bytearray()
    while True:
        piece = response.read(min(65536, _RESPONSE_LIMIT - len(raw) + 1))
        if not piece:
            break
        raw.extend(piece)
        if len(raw) > _RESPONSE_LIMIT:
            raise _TooLarge("response exceeds 1 MiB")
    if expected is not None and len(raw) != expected:
        raise http.client.IncompleteRead(bytes(raw), expected - len(raw))
    return bytes(raw)


def _hide(text, url):
    """Journal/log boundary: hide URL/query echoes, including standalone token and paging values."""
    for form in (url, urllib.parse.unquote(url), urllib.parse.quote(url, safe="")):
        text = text.replace(form, "<url>")
    text = re.sub(r"([\"'])/[^\"'\r\n]*\1", "<url>", text)
    query = url.partition("?")[2].partition("#")[0]
    for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if key in ("access_token", "after", "before", "cursor") and value:
            for form in (value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)):
                text = text.replace(form, "<token>" if key == "access_token" else "<query>")
    text = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", "<url>", text)
    text = re.sub(r"(?:\?|%3[fF])(?:<token>|<query>|[^\s\"'<>])*", "<query>", text)
    return re.sub(r"\b(?:access_token|after|before|cursor)=(?:<token>|<query>|[^\s\"'<>]*)", "<query>", text)


def _segment(value):
    return urllib.parse.quote(str(value), safe="")


def _transport_failure(error):
    """Exception text may contain selectors and credentials; retain a safe class/errno diagnostic."""
    name = type(error).__name__
    if isinstance(error, TimeoutError):
        return f"{name}: timed out"
    if isinstance(error, ssl.SSLCertVerificationError):
        return f"{name}: CERTIFICATE_VERIFY_FAILED"
    if isinstance(error, _TooLarge):
        return "TooLarge: response exceeds 1 MiB"
    errno = getattr(error, "errno", None)
    detail = f"errno={errno}" if isinstance(errno, int) else "HTTP exchange failed"
    return f"{name}: {detail}"


def exchange(method, url, body, deadline):
    """One HTTP exchange with an absolute real-time DNS/socket deadline. Never raises; never retries.

    Used by Meta and Telegram (notify.py). Resolver process creation/kill/reaping and OS scheduling are
    not hard realtime; the parent's wait and every socket operation use only the remaining budget.

    Returns (status, parsed JSON or None, connect_failed, failure): status 0 and the failure's text when no
    complete answer came.
    """
    try:
        request = urllib.request.Request(
            url, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={} if body is None else {"Content-Type": "application/json"})
        request.deadline = time.monotonic() + deadline
        with _opener.open(request) as response:
            raw = _read_response(response)
        return response.status, _parse(raw), False, ""
    except _ConnectFailed as e:
        return 0, None, True, _transport_failure(e.__cause__)
    except Exception as e:  # not provably a failed connect: the request may have reached its host
        return 0, None, False, _transport_failure(e)


# ---------- client ----------

@dataclass
class Result:
    ok: bool                # outcome == 'ok'
    http: int               # 0 = no complete HTTP response
    data: dict              # parsed body ({} if not an object)
    outcome: str
    reason: str
    code: int | None
    subcode: int | None
    message: str            # Meta's error message, or what went wrong on the wire; never contains the token
    connect_failed: bool


_calls = itertools.count(1)


def _http_complete(call, method, kind, url, status):
    """Useful process boundary before returning a Client result; never a gate/success truth flag."""
    try:
        route = urllib.parse.urlsplit(url).path
        if not re.fullmatch(r"/[A-Za-z0-9/%._~-]{0,511}", route) or _hide(route, url) != route:
            route = "<invalid>"
    except ValueError:
        route = "<invalid>"
    try:
        sys.stderr.write(f"{iso()} INFO http_complete pid={os.getpid()} monotonic={time.monotonic():.9f} "
                         f"call={call} method={method} kind={kind} route={route} http={status}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass   # Meta has answered: a log stream that cannot be written must not turn the answer into an exception


class Client:
    def __init__(self, api_base, api_version, get_token, ping=None):
        self._root = api_base.rstrip("/")
        self._version = api_version
        self._get_token = get_token
        self._ping = ping

    def me(self):
        return self._call("GET", "read_me", self._url("/me", fields="id,user_id,username,followers_count,media_count"))

    def media(self, media_id):
        return self._call("GET", "read_media", self._url(
            f"/{_segment(media_id)}", fields="id,permalink,shortcode,comments_count,timestamp"))

    def comments_page(self, media_id, next_url=None):
        return self._call("GET", "read_comments", next_url or self._url(
            f"/{_segment(media_id)}/comments", fields="id,text,timestamp,from,hidden,replies{id,from}", limit=50))

    def my_media(self, next_url=None):
        return self._call("GET", "read_me", next_url or self._url(
            "/me/media", fields="id,permalink,shortcode,timestamp,comments_count", limit=25))

    def send_dm(self, ig_user_id, comment_id, message):
        return self._call("POST", "dm", self._url(f"/{_segment(ig_user_id)}/messages"),
                          {"recipient": {"comment_id": comment_id}, "message": message})

    def send_to(self, ig_user_id, igsid, message):
        """14.4: a step, to a person who opened the conversation (by their Instagram-scoped id)."""
        return self._call("POST", "step", self._url(f"/{_segment(ig_user_id)}/messages"),
                          {"recipient": {"id": igsid}, "message": message})

    def follow_status(self, igsid):
        """14.5: GET is_user_follow_business of a person in a conversation."""
        return self._call("GET", "read_user", self._url(f"/{_segment(igsid)}", fields="is_user_follow_business"))

    def public_reply(self, comment_id, text):
        return self._call("POST", "public", self._url(f"/{_segment(comment_id)}/replies", message=text))

    def refresh_token(self, token):
        return self._call("GET", "refresh", f"{self._root}/refresh_access_token?" + urllib.parse.urlencode(
            {"grant_type": "ig_refresh_token", "access_token": token}))

    def _url(self, path, **params):
        params["access_token"] = self._get_token()
        return f"{self._root}/{self._version}{path}?{urllib.parse.urlencode(params)}"

    def _call(self, method, kind, url, body=None):
        url = str(url)  # a paging.next value is Meta's data, not ours
        call = next(_calls)
        status, parsed, connect_failed, failure = exchange(method, url, body, T["http_deadline"])
        if self._ping:
            self._ping()

        outcome, reason = classify(method, kind, status, parsed, connect_failed)
        data = parsed if isinstance(parsed, dict) else {}
        if isinstance(data.get("error"), dict):
            data["error"] = {key: _hide(value[:2000], url) if isinstance(value, str) else value
                             for key, value in data["error"].items()}
        code, subcode, message = _error(data)
        # The text is journalled and shown in alerts: cut it (a garbage status line can be 64 kB long) and
        # replace what SQLite cannot store (json.loads turns "\ud83d" into a lone surrogate).
        message = (message or _hide(failure, url))[:300].encode("utf-8", "replace").decode()
        result = Result(ok=outcome == "ok", http=status, data=data, outcome=outcome, reason=reason, code=code,
                        subcode=subcode, message=message, connect_failed=connect_failed)
        _http_complete(call, method, kind, url, status)
        return result
