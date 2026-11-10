"""Alerts (SPEC section 9): problems and events are queued in the database and sent to Telegram from there.

What this module guarantees:
- a problem is recorded as announced and its line is queued in ONE transaction, so a crash cannot lose the line;
- a line is marked sent only when Telegram answered ok:true; until then it stays queued and goes out later;
- trouble with Telegram, with `.env` or with the database never raises into the caller;
- the bot token is never logged.

kv keys written here: `tg_ok_at` / `tg_failed_at` (time of the last confirmed / failed delivery),
`tg_last_outcome` (ok or failed, independent of wall clock order), and
`tg_configured` (1 or 0: whether `.env` had a bot token and a chat id at the last flush).
"""
import functools
import sqlite3
import sys
import traceback
import urllib.parse
from datetime import timedelta

import clock
import meta
import store
from clock import T

TG_MAX_UNITS = 4096   # Telegram: longest text of one message, counted in UTF-16 units (an emoji is 2)


def _log(level, event, text):
    print(f"{clock.iso()} {level} {event} {' | '.join(str(text).splitlines())}", file=sys.stderr, flush=True)


def _in_path(bot_token):
    return urllib.parse.quote(bot_token, safe=":")


def _hide(text, secret):
    """`text` without the bot token, as it is and as it stands in the URL (some urllib errors quote the URL)."""
    for form in {secret, _in_path(secret), urllib.parse.quote(secret, safe="")} if secret else ():
        text = text.replace(form, "<bot-token>")
    return text


def send_message(api_base, bot_token, chat_id, text):
    """One Telegram sendMessage, at most 10 real seconds. Returns None when Telegram answered ok:true, else
    what went wrong. Never raises; what it returns never contains the bot token."""
    if not (bot_token and chat_id):
        return "no bot token or no chat id"
    bot_token = str(bot_token)
    # meta's transport: IPv4 only, no proxy, no redirect, an answer with any status is returned, not raised
    status, answer, _, failure = meta.exchange(
        "POST", f"{str(api_base).rstrip('/')}/bot{_in_path(bot_token)}/sendMessage",
        {"chat_id": chat_id, "text": text}, T["tg_timeout"])
    if status == 200 and isinstance(answer, dict) and answer.get("ok") is True:
        return None
    if status:
        description = answer.get("description") if isinstance(answer, dict) else None
        failure = f"http {status}: {description or 'no ok:true in the answer'}"
    return _hide(failure, bot_token)[:300]


def _units(text):
    return len(text.encode("utf-16-le", "replace")) // 2


def _cut(text, limit):
    """`text` in at most `limit` UTF-16 units; a cut is marked with an ellipsis."""
    if _units(text) <= limit:
        return text
    return text.encode("utf-16-le", "replace")[:2 * (limit - 1)].decode("utf-16-le", "ignore") + "…"


def _batch(rows):
    """(ids, text) of one message: the oldest queued lines that fit into it together.

    A single line longer than a message is cut: sent whole it would be refused and block the queue for ever.
    """
    ids, lines, used = [], [], -1
    for row_id, line in rows:
        line = _cut(line or "-", TG_MAX_UNITS)
        if lines and used + 1 + _units(line) > TG_MAX_UNITS:
            break
        ids.append(row_id)
        lines.append(line)
        used += 1 + _units(line)
    return ids, "\n".join(lines)


def health(conn):
    """Delivery health as the database knows it. `configured` is None until a flush recorded it."""
    configured = store.kv_get(conn, "tg_configured")
    ok_at, failed_at = store.kv_get(conn, "tg_ok_at"), store.kv_get(conn, "tg_failed_at")
    outcome = store.kv_get(conn, "tg_last_outcome")
    # Legacy timestamps cannot establish order when wall time went backward or stayed equal.
    failing = outcome == "failed" if outcome in {"ok", "failed"} else failed_at is not None
    return {"configured": None if configured is None else configured == "1",
            "ok_at": ok_at, "failed_at": failed_at,
            "failing": failing,
            "queued": conn.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]}


def deaf(conn, get_tg):
    """Why an alert raised now would reach nobody, or None. `bot.py arm` refuses on a reason unless --deaf (8.6)."""
    bot_token, chat_id = get_tg()
    if not (bot_token and chat_id):
        return "Telegram не настроен: в .env нет TG_BOT_TOKEN или TG_CHAT_ID, оповещения некому получать"
    state = health(conn)
    if state["failing"]:
        return (f"последняя отправка в Telegram не удалась ({state['failed_at']}), "
                f"в очереди {state['queued']}: оповещения сейчас не доходят")
    return None


def _never_raises(method):
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as error:
            _log("ERROR", "notify_failed", _hide(traceback.format_exc(), self._secret))
            if isinstance(error, sqlite3.Error):
                # the queue itself is out of order: the engine says so past the queue (`direct`)
                self.broken = "db_unwritable", f"{type(error).__name__}: {error}"[:300] + "; alerts cannot be queued"
            return False
    return guarded


class Notifier:
    def __init__(self, conn, get_tg, api_base, repeat_min=120):
        """`get_tg()` -> (bot_token, chat_id) or (None, None). `repeat_min` is config `alerts.repeat_min`."""
        self._conn = conn
        self._get_tg = get_tg
        self._api_base = api_base
        self._repeat = timedelta(minutes=repeat_min)
        self._secret = ""   # the bot token last read, to keep it out of the log
        self._unmarked = []   # (ids, outcome, completed_at) confirmed but not durably recorded
        self.broken = None    # (key, text) while the alert queue cannot be written, for Engine._speak_up

    def _queue(self, key, line, said):
        row_id = store.outbox_add(self._conn, key, line)
        _log("ALERT", key, said)
        return row_id

    @_never_raises
    def sync(self, problems, now):
        """Queue one line for each new problem, for each lasting one every `repeat_min`, and for each that ended."""
        current = {}
        for key, text in problems:
            current.setdefault(key, text)
        with store.tx(self._conn):
            announced = dict(self._conn.execute("SELECT key, last_announced_at FROM problems ORDER BY since, key"))
            for key, text in current.items():
                if key not in announced or now - clock.parse(announced[key]) >= self._repeat:
                    store.problem_announce(self._conn, key, text)
                    self._queue(key, f"⚠ {key}: {text}", text)
            for key in announced:
                if key not in current:
                    store.problem_clear(self._conn, key)
                    self._queue(key, f"✓ cleared: {key}", "cleared")
        self.broken = None

    @_never_raises
    def event(self, key, text):
        """Queue one line, once. Return its positive outbox id, or False when it could not be queued."""
        return self._queue(key, f"ℹ {key}: {text}" if text else f"ℹ {key}", text)

    @_never_raises
    def queue_clear(self, key):
        """Durably queue a raw recovery line. Return its outbox id, or False on failure."""
        return self._queue(key, f"✓ cleared: {key}", "cleared")

    @_never_raises
    def direct(self, key, text):
        """One line to Telegram at once, past the queue: for the moments in which the database cannot hold it
        (it cannot be written, or this process does not own it). True if Telegram confirmed it."""
        _log("ALERT", key, text)
        bot_token, chat_id = self._get_tg()
        self._secret = str(bot_token or "")
        return send_message(self._api_base, bot_token, chat_id, _cut(text, TG_MAX_UNITS)) is None

    def _record_result(self, ids, outcome, completed_at):
        """Commit the entire delivery result; recovery uses the same captured completion time."""
        with store.tx(self._conn):
            if outcome == "ok":
                store.outbox_sent(self._conn, ids, at=completed_at)
            store.kv_set(self._conn, "tg_ok_at" if outcome == "ok" else "tg_failed_at", completed_at)
            store.kv_set(self._conn, "tg_last_outcome", outcome)

    @_never_raises
    def flush(self, only_id=None):
        """Send the queued lines as ONE message. True if Telegram confirmed it or nothing was queued.

        Without a bot token and a chat id nothing is sent and the lines stay queued (they are in the log).
        `only_id` restricts this attempt to a fresh urgent row, after any pending confirmation is committed.
        """
        conn = self._conn
        if self._unmarked:   # never send again what Telegram already took: first record it, or send nothing
            self._record_result(*self._unmarked)
            self._unmarked = []
        bot_token, chat_id = self._get_tg()
        self._secret = str(bot_token or "")
        configured = str(int(bool(bot_token and chat_id)))
        if store.kv_get(conn, "tg_configured") != configured:
            store.kv_set(conn, "tg_configured", configured)
        query, params = "SELECT id, text FROM outbox WHERE sent_at IS NULL", ()
        if only_id is not None:
            query, params = query + " AND id=?", (only_id,)
        rows = conn.execute(query + " ORDER BY id", params).fetchall()
        if not rows or configured == "0":
            return not rows
        ids, text = _batch(rows)
        # no transaction is open during the HTTP call: `arm` and `disarm` must not wait for Telegram
        error = send_message(self._api_base, bot_token, chat_id, text)
        completed_at = clock.iso()
        outcome = "ok" if error is None else "failed"
        if error is None:
            self._unmarked = ids, outcome, completed_at
        self._record_result(ids, outcome, completed_at)
        self._unmarked = []
        if error:
            _log("WARN", "telegram_failed", f"queued={len(rows)} error={error}")
        return error is None
