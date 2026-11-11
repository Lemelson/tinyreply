#!/usr/bin/env python3
"""The web process (SPEC 14.1): Meta's webhook, the click redirect and the panel, on 127.0.0.1 behind nginx.

What this module guarantees:
- it never calls Meta and never sends anything: no module that talks to Meta is imported here, and its only
  database writes are store.add_inbound (a signed webhook event) and store.add_click (a tracked link opened);
- a webhook body is stored only after its X-Hub-Signature-256 matched HMAC-SHA256(IG_APP_SECRET, raw body),
  compared in constant time; a body over 256 KiB is refused before it is read; a delivery Meta repeats is
  stored once (dedupe by `mid`, or by the SHA-256 of the item's canonical JSON when it has none);
- everything under /panel needs a login (PANEL_USER and PANEL_PASSWORD; either missing: 403 for all of it). A
  login sets a signed cookie for 90 days (HttpOnly, Secure, SameSite=Strict); changing the password ends every
  session. Wrong logins are slowed (1 s each) and locked: 5 per address in 15 minutes, 30 in all per hour. The
  panel JSON is read over a read-only connection; a static file is served only from the dashboard
  directory, never through `..`, a link or a hidden name, and only with a known web file extension;
- the secrets come from <STATE_DIR>/.env (engine.read_env), re-read on every request, never from the process
  environment, and never reach a log line: one line per request with method, route (no query string, no
  token), status and milliseconds;
- the bot holds the same database in WAL mode: a busy database answers 503 after a few seconds (Meta repeats
  the delivery; the panel asks again), never a hung worker or a half-stored batch that a repeat cannot complete.

Environment: STATE_DIR (default <code dir>/state, like bot.py), CONFIG (config file, default
<STATE_DIR>/config.json), WEB_PORT (default 5421), PANEL_DIR (default <code dir>/dashboard, else the repo's
../dashboard).

    python3 web.py            serve until SIGTERM/SIGINT
"""
import hashlib
import hmac
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, unquote, urlsplit

import clock
import config
import engine
import store

HOST = "127.0.0.1"
DEFAULT_PORT = 5421
MAX_BODY = 256 * 1024          # 14.1: a webhook body is at most 256 KiB
BUSY_MS = 5000                 # a request waits this long for the bot's write transaction, then answers 503
AGENT_MAX = 200                # 14.1: user agent of a click, cut
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")
SAFE_ROUTE = re.compile(r"/panel(/[A-Za-z0-9_.-]{0,80}){0,4}")   # logged as it is; anything else as /panel/*
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
                ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".webp": "image/webp"}
PANEL_HEADERS = {
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}
CODE_DIR = Path(__file__).resolve().parent
COOKIE = "igpanel"
SESSION_SECONDS = 90 * 24 * 3600          # «запомнить этот компьютер» for three months
LOGIN_BODY_MAX = 4096
LOGIN_FAILS_PER_ADDRESS = (5, 15 * 60)    # wrong logins from one address in this many seconds lock it
LOGIN_FAILS_IN_ALL = (30, 3600)           # ... and from everywhere together, which locks the login for everyone
LOGIN_PAUSE = 1.0                         # every wrong login waits this long before its answer


class Busy(Exception):
    """The database is locked by the bot for longer than BUSY_MS, or is not there yet."""


def _busy(error):
    return isinstance(error, sqlite3.OperationalError) and (
        error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


# ---------- the webhook: pure parts ----------

def signature_ok(secret, header, body):
    """X-Hub-Signature-256 equals 'sha256=' + hex HMAC-SHA256(secret, body). Constant time; bytes on both sides
    (a header with a non-ASCII character must be a mismatch, not an exception)."""
    if not secret or not header:
        return False
    expected = b"sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest().encode()
    return hmac.compare_digest(header.encode("utf-8", "surrogateescape"), expected)


def _sender(value):
    """An IGSID as a string (Meta sends a string; a number is taken as its digits)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text_or_none(value)


def _text_or_none(value):
    return value if isinstance(value, str) and value else None


def events(payload):
    """The inbound rows of one webhook body: [(mid, sender, kind, payload, text)] in delivery order (14.1).

    A `postback` gives kind `postback` (its payload); a `message` that is not an echo of our own message gives
    kind `message` (its text, or the quick reply payload). Everything else is ignored. An item without a mid
    gets 'sha256:' + the SHA-256 of its canonical JSON, so that a repeated delivery is still one row.
    """
    found = []
    entries = payload.get("entry") if isinstance(payload, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        items = entry.get("messaging") if isinstance(entry, dict) else None
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict):
                continue
            sender = item.get("sender")
            sender = _sender(sender.get("id")) if isinstance(sender, dict) else None
            postback, message = item.get("postback"), item.get("message")
            if isinstance(postback, dict):
                kind, mid = "postback", postback.get("mid")
                body, text = _text_or_none(postback.get("payload")), _text_or_none(postback.get("title"))
            elif isinstance(message, dict) and message.get("is_echo") is not True:
                quick = message.get("quick_reply")
                kind, mid = "message", message.get("mid")
                body = _text_or_none(quick.get("payload")) if isinstance(quick, dict) else None
                text = _text_or_none(message.get("text"))
            else:
                continue
            mid = _text_or_none(mid) or "sha256:" + hashlib.sha256(json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass")
            ).hexdigest()
            found.append((mid, sender, kind, body, text))
    return found


def _same(given, wanted):
    return hmac.compare_digest(given.encode("utf-8", "surrogateescape"), wanted.encode("utf-8"))


def credentials_ok(user, password, given_user, given_password):
    """Both compared in constant time, both always compared."""
    ok_user = _same(given_user or "", user)
    ok_password = _same(given_password or "", password)
    return bool(user and password) and ok_user and ok_password


def _session_key(user, password):
    # derived from the credentials: a new password ends every session at once
    return hashlib.sha256(f"igpanel-session\0{user}\0{password}".encode("utf-8")).digest()


def make_session(user, password, now):
    expires = int(now) + SESSION_SECONDS
    mac = hmac.new(_session_key(user, password), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def session_ok(user, password, cookie_header, now):
    if not (user and password and cookie_header):
        return False
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except CookieError:
        return False
    value = jar[COOKIE].value if COOKIE in jar else ""
    expires, _, mac = value.partition(".")
    if not expires.isdigit() or int(expires) <= now or int(expires) > now + SESSION_SECONDS + 60:
        return False
    wanted = hmac.new(_session_key(user, password), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac.encode("ascii", "replace"), wanted.encode())


class LoginGuard:
    """Wrong logins, in memory: a restart forgets them, which costs a guesser a restart per 5 tries at most."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = {}       # address -> [monotonic times]
        self._all = []

    def locked(self, address, now):
        with self._lock:
            mine = [t for t in self._fails.get(address, []) if t > now - LOGIN_FAILS_PER_ADDRESS[1]]
            everyone = [t for t in self._all if t > now - LOGIN_FAILS_IN_ALL[1]]
            self._fails[address], self._all = mine, everyone
            return len(mine) >= LOGIN_FAILS_PER_ADDRESS[0] or len(everyone) >= LOGIN_FAILS_IN_ALL[0]

    def failed(self, address, now):
        with self._lock:
            self._fails.setdefault(address, []).append(now)
            self._all.append(now)
            if len(self._fails) > 10000:   # a flood of addresses must not grow memory without end
                self._fails = {a: t for a, t in self._fails.items() if t and t[-1] > now - LOGIN_FAILS_PER_ADDRESS[1]}


def login_page(error=""):
    note = f'<p class="err">{error}</p>' if error else ""
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Вход · Автоответ</title>
<style>body{{font:16px -apple-system,system-ui,sans-serif;background:#f6f6f4;color:#1d1d1b;margin:0}}
main{{max-width:340px;margin:14vh auto;padding:0 16px}}h1{{font-size:22px;margin:0 0 20px}}
label{{display:block;font-size:13px;color:#666;margin:14px 0 6px}}input{{width:100%;box-sizing:border-box;
font:inherit;padding:10px 12px;border:1px solid #ccc;border-radius:8px;background:#fff}}
button{{margin-top:20px;width:100%;font:inherit;font-weight:600;padding:11px;border:0;border-radius:8px;
background:#1d1d1b;color:#fff;cursor:pointer}}.err{{color:#b3261e;font-size:14px}}.hint{{color:#888;font-size:13px}}
@media (prefers-color-scheme:dark){{body{{background:#161615;color:#eee}}input{{background:#222;border-color:#444;color:#eee}}
button{{background:#eee;color:#161615}}}}</style>
<main><h1>Автоответ · вход</h1>{note}
<form method="post" action="/panel/login">
<label for="u">Логин</label><input id="u" name="user" autocomplete="username" autocapitalize="none" required>
<label for="p">Пароль</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Войти</button></form>
<p class="hint">Этот компьютер будет помнить вход 90 дней.</p></main></html>"""


# ---------- the server ----------

class App:
    """What every request needs: where the state, the config and the panel files are."""

    def __init__(self, state_dir, panel_dir=None, config_path=None):
        self.state_dir = Path(state_dir).absolute()
        self.db_path = self.state_dir / "core.db"
        self.config_path = Path(config_path or self.state_dir / "config.json")
        self.panel_dir = Path(panel_dir or _default_panel_dir()).resolve()

    def env(self):
        return engine.read_env(self.state_dir / ".env")

    def connect(self, readonly=False):
        """One connection for one request; Busy if the database is absent or locked too long."""
        if not self.db_path.exists():
            raise Busy("no database yet")
        try:
            conn = store.connect(self.db_path, readonly=readonly)
        except sqlite3.OperationalError as error:
            if _busy(error):
                raise Busy("database busy") from None
            raise
        conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        return conn

    def config(self, conn):
        """(cfg, config_file_error) as `bot.py report` takes them: the file when it is valid (the report itself
        tells the running config from it), else the config the service runs with (kv `config_running`)."""
        try:
            return config.load(self.config_path), None
        except config.ConfigError as error:
            stored = store.kv_get(conn, "config_running")
            if not stored:
                return None, "; ".join(error.problems)
            try:
                return config.loads(stored), "; ".join(error.problems)
            except (ValueError, config.ConfigError):
                return None, "; ".join(error.problems)


def _default_panel_dir():
    here = CODE_DIR / "dashboard"
    return here if here.is_dir() else CODE_DIR.parent / "dashboard"


class Handler(BaseHTTPRequestHandler):
    server_version = "igweb"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30               # a client that stops sending its body frees the worker

    # ----- plumbing -----

    def log_message(self, format, *args):   # the default line carries the query string: replaced by _log
        pass

    def _log(self, status, **fields):
        engine.log("INFO", "web", method=self.command, route=self._route, status=status,
                   ms=int((time.monotonic() - self._started) * 1000), **fields)

    def _reply(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None, **log_fields):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if body or status not in (204, 304):
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._log(status, **log_fields)

    def _guarded(self, work):
        self._started = time.monotonic()
        self._route = "?"
        try:
            work()
        except Busy as why:
            self._reply(503, "busy\n", headers={"Retry-After": "5"}, why=str(why))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:   # one bad request must never stop the server; no detail to the client
            engine.log("ERROR", "web_error", route=self._route, error=type(error).__name__)
            try:
                self._reply(500, "error\n")
            except OSError:
                pass

    def do_GET(self):
        self._guarded(self._get)

    def do_HEAD(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    # ----- routes -----

    def _get(self):
        url = urlsplit(self.path)
        path = url.path
        if path == "/health":
            self._route = path
            return self._reply(200, "ok\n")
        if path == "/meta/webhook":
            self._route = path
            return self._verify(parse_qs(url.query))
        if path.startswith("/r/"):
            self._route = "/r/*"
            return self._click(path[3:])
        if path == "/panel" or path.startswith("/panel/"):
            self._route = path if SAFE_ROUTE.fullmatch(path) else "/panel/*"
            return self._panel(path)
        self._route = "other"
        self._reply(404, "not found\n")

    def _post(self):
        path = urlsplit(self.path).path
        self._route = path if path in ("/meta/webhook", "/panel/login") else "other"
        if path == "/panel/login":
            return self._login()
        if path != "/meta/webhook":
            self.close_connection = True   # the body was not read
            return self._reply(404, "not found\n")
        self._webhook()

    def _verify(self, query):
        """14.1: Meta's subscription handshake."""
        token = self.server.app.env().get("WEBHOOK_VERIFY_TOKEN")
        mode, given, challenge = (query.get(key, [""])[0] for key in ("hub.mode", "hub.verify_token", "hub.challenge"))
        if mode == "subscribe" and token and hmac.compare_digest(given.encode("utf-8", "surrogateescape"),
                                                                 token.encode("utf-8")):
            return self._reply(200, challenge)
        self._reply(403, "forbidden\n")

    def _webhook(self):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self.close_connection = True
            return self._reply(411, "length required\n")
        if int(length) > MAX_BODY:
            self.close_connection = True   # the body is not read
            return self._reply(413, "too large\n")
        body = self.rfile.read(int(length))
        if len(body) != int(length):
            self.close_connection = True
            return self._reply(400, "short body\n")
        secret = self.server.app.env().get("IG_APP_SECRET")
        if not signature_ok(secret, self.headers.get("X-Hub-Signature-256"), body):
            return self._reply(403, "forbidden\n")
        try:
            found = events(json.loads(body))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return self._reply(200, "ok\n", items=0, stored=0, bad_json=1)   # signed, but nothing to store
        stored = 0
        if found:
            conn = self.server.app.connect()
            try:
                for mid, sender, kind, payload, text in found:
                    stored += store.add_inbound(conn, mid, sender, kind, payload, text)
            except sqlite3.OperationalError as error:
                if _busy(error):   # Meta repeats the delivery; the rows already stored are deduped then
                    raise Busy("database busy") from None
                raise
            finally:
                conn.close()
        self._reply(200, "ok\n", items=len(found), stored=stored)

    def _click(self, token):
        """14.1: a tracked link. A click is a GET; a HEAD (a link checker) redirects without counting."""
        if not TOKEN.fullmatch(token):
            return self._reply(404, "not found\n")
        conn = self.server.app.connect()
        try:
            try:
                url = store.link_url(conn, token)
            except sqlite3.OperationalError as error:
                if _busy(error):
                    raise Busy("database busy") from None
                raise
            if not url or any(c in url for c in "\r\n") or not url.startswith(("https://", "http://")):
                return self._reply(404, "not found\n")
            if self.command == "GET":
                agent = self.headers.get("User-Agent")
                try:
                    store.add_click(conn, token, agent[:AGENT_MAX] if agent else None)
                except sqlite3.OperationalError as error:
                    if not _busy(error):
                        raise
                    # the person must not wait for a count: they get the link, the lost click is logged
                    engine.log("WARN", "click_not_counted", why="database busy")
        finally:
            conn.close()
        self._reply(302, headers={"Location": url, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    def _credentials(self):
        env = self.server.app.env()
        return env.get("PANEL_USER") or "", env.get("PANEL_PASSWORD") or ""

    def _address(self):
        # nginx (the only way in from outside) passes the visitor's address; a direct local call has its own
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _login(self):
        user, password = self._credentials()
        if not (user and password):
            self.close_connection = True
            return self._reply(403, "panel is closed: no PANEL_USER/PANEL_PASSWORD\n", headers=PANEL_HEADERS)
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit() or int(length) > LOGIN_BODY_MAX:
            self.close_connection = True
            return self._reply(400, "bad request\n", headers=PANEL_HEADERS)
        form = parse_qs(self.rfile.read(int(length)).decode("utf-8", "replace"))
        guard, address, now = self.server.guard, self._address(), time.monotonic()
        html = {**PANEL_HEADERS, "Cache-Control": "no-store"}
        if guard.locked(address, now):
            return self._reply(429, login_page("Слишком много неверных попыток. Попробуйте через 15 минут."),
                               "text/html; charset=utf-8", headers=html, login="locked")
        if not credentials_ok(user, password, form.get("user", [""])[0].strip(), form.get("password", [""])[0]):
            guard.failed(address, now)
            time.sleep(LOGIN_PAUSE)
            return self._reply(401, login_page("Неверный логин или пароль."), "text/html; charset=utf-8",
                               headers=html, login="wrong")
        cookie = (f"{COOKIE}={make_session(user, password, time.time())}; Max-Age={SESSION_SECONDS}; Path=/panel; "
                  "HttpOnly; Secure; SameSite=Strict")
        self._reply(303, headers={**html, "Location": "/panel/", "Set-Cookie": cookie}, login="ok")

    def _panel(self, path):
        user, password = self._credentials()
        if not (user and password):
            return self._reply(403, "panel is closed: no PANEL_USER/PANEL_PASSWORD\n", headers=PANEL_HEADERS)
        if path == "/panel/login":
            if session_ok(user, password, self.headers.get("Cookie"), time.time()):
                return self._reply(303, headers={**PANEL_HEADERS, "Location": "/panel/"})
            return self._reply(200, login_page(), "text/html; charset=utf-8",
                               headers={**PANEL_HEADERS, "Cache-Control": "no-store"})
        if path == "/panel/logout":
            return self._reply(303, headers={**PANEL_HEADERS, "Location": "/panel/login",
                                             "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/panel; HttpOnly; Secure; "
                                                           "SameSite=Strict"})
        if not session_ok(user, password, self.headers.get("Cookie"), time.time()):
            if path == "/panel/data/report.json":
                return self._reply(401, '{"error": "login"}', "application/json; charset=utf-8",
                                   headers={**PANEL_HEADERS, "Cache-Control": "no-store"})
            return self._reply(303, headers={**PANEL_HEADERS, "Location": "/panel/login"})
        if path == "/panel":
            return self._reply(301, headers={**PANEL_HEADERS, "Location": "/panel/"})
        if path == "/panel/data/report.json":
            return self._report()
        self._static(unquote(path[len("/panel/"):]) or "index.html")

    def _report(self):
        import panel   # only the panel needs report.py and its imports
        app = self.server.app
        conn = app.connect(readonly=True)
        try:
            cfg, file_error = app.config(conn)
            if cfg is None:
                return self._reply(503, json.dumps({"error": "no_config"}), "application/json; charset=utf-8",
                                   headers={**PANEL_HEADERS, "Cache-Control": "no-store"})
            data = panel.build(conn, cfg, clock.now(), config_file_error=file_error)
        except sqlite3.OperationalError as error:
            if _busy(error):
                raise Busy("database busy") from None
            raise
        finally:
            conn.close()
        self._reply(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8",
                    headers={**PANEL_HEADERS, "Cache-Control": "no-store"})

    def _static(self, relative):
        """A file of the dashboard directory: no `..`, no hidden part, no link out of it, a known extension."""
        root = self.server.app.panel_dir
        parts = relative.split("/")
        if "\x00" in relative or "\\" in relative or any(not p or p.startswith(".") for p in parts):
            return self._reply(404, "not found\n", headers=PANEL_HEADERS)
        target = (root / relative).resolve()
        kind = STATIC_TYPES.get(target.suffix.lower())
        if not kind or not target.is_relative_to(root) or not target.is_file():
            return self._reply(404, "not found\n", headers=PANEL_HEADERS)
        self._reply(200, target.read_bytes(), kind, headers={**PANEL_HEADERS, "Cache-Control": "no-cache"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, port=DEFAULT_PORT, host=HOST):
        self.app = app
        self.guard = LoginGuard()
        super().__init__((host, port), Handler)


def make_server(state_dir, port=DEFAULT_PORT, panel_dir=None, config_path=None):
    """A bound server (port 0: a free one, see server.server_port); call serve_forever() to run it."""
    return Server(App(state_dir, panel_dir, config_path), port)


def main():
    state_dir = os.path.abspath(os.environ.get("STATE_DIR") or os.path.join(CODE_DIR, "state"))
    config_path = os.path.abspath(os.environ.get("CONFIG") or os.path.join(state_dir, "config.json"))
    try:
        port = int(os.environ.get("WEB_PORT") or DEFAULT_PORT)
    except ValueError:
        print("WEB_PORT must be a number", file=sys.stderr)
        return 2
    server = make_server(state_dir, port, os.environ.get("PANEL_DIR") or None, config_path)
    print(f"STATE_DIR={state_dir} CONFIG={config_path} PANEL_DIR={server.app.panel_dir} "
          f"listening on {HOST}:{server.server_port}", file=sys.stderr)

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()   # shutdown() waits for serve_forever
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
