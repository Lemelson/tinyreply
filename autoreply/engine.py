"""The engine (SPEC section 8): what the bot does in one cycle, top-down in the order of 8.1.

    recover -> clock guard -> token and identity -> collect or probe -> expire -> send -> housekeeping -> announce

What this module guarantees:
- a DM or a public reply is only claimed for a reel that completed a full sweep in this process run and whose
  collect was ok in this cycle (G6); every other gate is checked by the claim in store.py;
- nothing that can fail runs between a claim and its POST, and nothing but the journal write after the POST;
- a reel, a comment or a step that fails never stops the others, and the problems are announced in every cycle.

kv keys written here beyond the list of SPEC 7 (report.py turns them into problems):
    probe_fails           probes in a row that got no clear answer while there was no account hold (8.5)
    token_refresh_fails   refreshes in a row that failed (8.2, problem `token`)
    clock_problem         text while the clock guard is tripped (8.1, problem `clock`)
    env_problem           text while .env exists but cannot be read (3, problem `env`)
    told_expired, told_uncertain   the counts the events of that name were last raised for (9)
"""
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import traceback
import time
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import clock
import config
import match
import store
from clock import T

MAX_PAGES = 400          # 8.3: pages of one sweep
MAX_DMS_PER_CYCLE = 10   # 8.4: collect runs between batches
PROBE_FAILS = 3          # 8.5: probes in a row without a clear answer before the account counts as unreachable

_ENV_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")
_SPACE = re.compile(r"\s")


def log(level, event, **fields):
    """One line on stderr: <ts> <LEVEL> <event> key=value ... Never pass a token, a URL or a Result.data."""
    words = [clock.iso(), level, event]
    for key, value in fields.items():
        text = "" if value is None else str(value)
        if _SPACE.search(text):
            text = json.dumps(text, ensure_ascii=False)
        words.append(f"{key}={text}")
    try:
        sys.stderr.write(" ".join(words) + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass   # a closed or broken log stream: lines stand between a claim and its POST, they must never raise


def read_env(path):
    """The secrets file <STATE_DIR>/.env as a dict (SPEC 3); a missing file or an empty value is no value."""
    return read_env_checked(path)[0]


def read_env_checked(path):
    """(values, problem). Never raises: a file that exists but cannot be read gives no values and says why.

    Bytes that are not UTF-8 are replaced, so only the lines that contain them are affected (a `#` line saved
    in another encoding stopped every cycle), and a byte order mark is not part of the first key.
    """
    try:
        with open(path, "rb") as file:
            lines = file.read().decode("utf-8-sig", "replace").splitlines()
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        return {}, f".env cannot be read: {type(error).__name__}"
    values = {}
    for line in lines:
        found = _ENV_LINE.fullmatch(line.strip())   # v1 took quotes and `export` literally: a 190 with no hint
        if found:
            value = found.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                values[found.group(1)] = value
    return values, None


def _id(value):
    """An id of Meta as a string, or None when there is none."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and value else None


def _author(who):
    """(user_id, username) of a `from` object; Meta may show no author. ValueError for any other shape."""
    if who is None:
        return None, None
    if not isinstance(who, dict):
        raise ValueError("`from` is not an object")
    username = who.get("username")
    return _id(who.get("id")), username if isinstance(username, str) and username else None


def _replies(item):
    """The replies listed under a comment. Any shape but Meta's raises: the comment is stored as malformed."""
    replies = item.get("replies")
    return [] if replies is None else replies["data"]


def _hold_reason(reason):
    """The account-hold reason (8.5) for an account-wide refusal."""
    return reason if reason in ("auth", "rate") else "blocked"


def _note(call, result):
    return f"{call}: {result.outcome}/{result.reason} http={result.http} code={result.code} {result.message}".strip()


def _expiry(now, seconds):
    """An unusable expiry is unknown metadata, never grounds to discard a successfully refreshed token."""
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        try:
            return clock.iso(now + timedelta(seconds=seconds))
        except (OverflowError, ValueError):
            pass
    return None


class _Stopped(BaseException):
    """SIGTERM/SIGINT: the cycle ends after the database write that followed the HTTP call in flight (SPEC 3)."""


class _ReelFailed(Exception):
    """This reel cannot be read now. `result`: the answer that says so; `state`: the new reel_state, if any."""

    def __init__(self, note, result=None, state=None):
        super().__init__(note)
        self.note, self.result, self.state = note, result, state


class _Sweep:
    """Counters of one collect of one reel: the fields of its log line (`sweep` or `sweep_failed`)."""

    def __init__(self, media_id, full):
        self.media_id, self.full, self.started_at = media_id, full, clock.iso()
        self.pages = self.comments = self.new_rows = self.new_pending = self.rematched = self.unreadable = self.malformed = 0


_MODULES = ("bot", "clock", "config", "engine", "match", "meta", "notify", "report", "store")


def code_fingerprint():
    """SHA-256 of the nine module files next to this one: tells a new deploy from a restart (4)."""
    here, digest = os.path.dirname(os.path.abspath(__file__)), hashlib.sha256()
    for name in _MODULES:
        with open(os.path.join(here, name + ".py"), "rb") as file:
            digest.update(file.read())
    return digest.hexdigest()


class Engine:
    def __init__(self, cfg, conn, client, notifier, should_stop=None, ping=None, config_path=None):
        """Start-up of the process that holds the lock. It writes: a database that cannot be written fails here,
        before anything is read or sent."""
        self.cfg, self.conn, self.client, self.notifier = cfg, conn, client, notifier
        self._should_stop, self._ping = should_stop, ping
        self._config_path = config_path   # only compared with the running config, never applied (9)
        # STATE_DIR is where the database lives (SPEC 3): .env is beside core.db
        self._state_dir = os.path.dirname(conn.execute("PRAGMA database_list").fetchone()[2])
        active = [reel for reel in cfg.reels if reel.active]
        self._matchers = {reel.media_id: match.Matcher(reel) for reel in active}
        # rendered before any claim: nothing that can raise may stand between a claim and its POST (v1 issue K2)
        self._messages = {(reel.media_id, late): config.render(cfg, reel, late)
                          for reel in active for late in (False, True)}
        me = {"user_id": store.kv_get(conn, "me_user_id"), "id": store.kv_get(conn, "me_id"),
              "username": store.kv_get(conn, "me_username")}
        self.me = me if all(me.values()) else None   # known only with all three fields (8.2)
        self._account = None      # (followers_count, media_count) of the latest GET /me, for the hourly metric
        self._swept = set()       # media ids with a completed full sweep in this process run
        self._sendable = set()    # ... whose collect was ok in this cycle: only their comments are sent to (G6)
        self._retry_at = {}       # media id -> when a gone, mismatched or capped reel is read again
        self._hidden = set()      # comment ids whose latest listing in this run said hidden=true (7.4)
        self._last_clock = None   # (now, monotonic) of the previous cycle
        self._last_post = None    # monotonic cooldown anchor: response return, startup, or bounded unknown flight
        self._startup_wait = False
        last = conn.execute("SELECT started_at, finished_at, outcome, http, reason FROM attempts "
                            "WHERE kind='dm' ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            unknown = last["finished_at"] is None or last["reason"] == "interrupted" or \
                (last["outcome"] == "uncertain" and not last["http"])
            stamp = max(last["started_at"], last["finished_at"] or last["started_at"])
            minimum_elapsed = max(0, (clock.now() - clock.parse(stamp)).total_seconds() - T["clock_offset_margin"])
            # Completed response bounds receipt. Unknown flight gets a practical D=60 guard; the local
            # deadline is not proof of a remote receipt bound after timeout/death (SPEC release limitation).
            self._last_post = clock.monotonic() + T["http_deadline"] if unknown else \
                clock.monotonic() - minimum_elapsed
            self._startup_wait = True
        self._mute = None         # (key, monotonic time) of the last alert that had to bypass the database (9)
        self._started = False
        self._prepared = False
        self._run_id, self._cycle = uuid.uuid4().hex, 0
        self._wait_for_startup = False
        self._dms = 0
        self.pause = cfg.poll_interval_sec   # seconds run() sleeps after the cycle
        fingerprint = config.fingerprint(cfg)
        with store.tx(conn):
            store.kv_set_many(conn, {"config_running": config.dump(cfg), "config_loaded_at": clock.iso()})
            code = code_fingerprint()
            what = "config" if store.kv_get(conn, "config_hash") != fingerprint else \
                "code of the bot" if store.kv_get(conn, "code_hash") != code else None
            if what:
                # 4: every change of the config or of the code needs a human `arm`: neither can mass-send by surprise
                if notifier.event("config_changed", f"the {what} changed: sending is disarmed; "
                                                    f"read the report, then arm") is False:
                    raise sqlite3.OperationalError("config change notification could not be queued")
                store.kv_set_many(conn, {"config_hash": fingerprint, "code_hash": code, "send_budget": 0})

    # ---------- the loop (8.1) ----------

    def prepare_run(self):
        """Durably prepare the service-start notice before READY. Idempotent; one-shot commands do not call it."""
        if not self._prepared:
            if self.notifier.event("started", f"sending {'on' if self.cfg.sending else 'off'}, budget "
                                             f"{store.budget(self.conn)}, reels {len(self._matchers)}") is False:
                raise sqlite3.OperationalError("started notification could not be queued")
            self._prepared = True

    def run(self):
        """Cycle until the stop flag is set; the loop never ends on its own."""
        self.prepare_run()
        while not self._stopping():
            self.cycle()
            clock.sleep(self.pause, self._should_stop, self._ping)
        log("INFO", "stopped")

    def cycle(self, only=None, wait_for_startup=False):
        """One cycle; returns confirmed DMs. `only`: send-one target. One-shot commands may wait once at startup."""
        self._dms, self.pause = 0, self.cfg.poll_interval_sec
        self._wait_for_startup = wait_for_startup
        self._cycle += 1
        log("INFO", "cycle_start", pid=os.getpid(), run=self._run_id, cycle=self._cycle, monotonic=time.monotonic())
        try:
            unsaid = self._guarded(self._work, "nothing is read or sent", only)
            # also after a failed cycle: a failure must never be silent (G5)
            unsaid = self._guarded(self._announce, "the alerts could not be worked out", always_unsaid=True) or unsaid
            self._speak_up(unsaid or getattr(self.notifier, "broken", None))
        except _Stopped:
            log("INFO", "stop_requested")
        return self._dms

    def _guarded(self, part, effect, *args, always_unsaid=False):
        """Run one part of the cycle. Returns None, or (key, text) of a failure that the alert queue cannot
        carry: the error could not be stored, or (`always_unsaid`) the part that failed is the one that alerts.
        `effect`: what the failure of this part means, for that text."""
        try:
            part(*args)
            return None
        except Exception as error:
            text = f"{type(error).__name__}: {error}"
            log("ERROR", "cycle_error", error=text)
            traceback.print_exc()
            key = "db_unwritable" if isinstance(error, (sqlite3.Error, OSError)) else "cycle_error"
            try:
                if self.conn.in_transaction:
                    self.conn.rollback()
                store.kv_set(self.conn, "last_cycle_error", f"{clock.iso()} {text}"[:300])
            except Exception as failure:   # the database itself is in trouble: the next cycle tries again
                traceback.print_exc()
                return "db_unwritable", f"{type(failure).__name__}: {failure}"[:300] + f"; {effect}"
            return (key, f"{text[:300]}; {effect}") if always_unsaid else None

    def _speak_up(self, unsaid):
        """9: alerting must not depend on a database that can be written. A failure the queue cannot carry goes
        to Telegram directly, again every repeat_min while it lasts, and its end is told the same way."""
        direct = getattr(self.notifier, "direct", None)
        if unsaid:
            key, text = unsaid
            due = self._mute is None or clock.monotonic() - self._mute[1] >= self.cfg.alerts.repeat_min * 60
            if due and direct and direct(key, f"⚠ {key}: {text}"):
                self._mute = key, clock.monotonic()
        elif self._mute:
            if self.notifier.queue_clear(self._mute[0]) is not False:
                self._mute = None
                self.notifier.flush()

    def _work(self, only):
        if not self._started:
            self._started = True
            self._startup_events()
        recovered = store.recover_stale(self.conn)
        if recovered:
            log("WARNING", "recovered_stale", comments=recovered)
        clock_ok = self._clock_guard()
        self._token_and_identity()
        self._sendable = set()
        hold = store.get_account_hold(self.conn)
        if not hold:
            self._collect()
        elif store.kv_get(self.conn, "token") and hold["next_probe_at"] <= clock.iso():
            self._probe()
        if clock_ok:
            expired = store.expire(self.conn)
            if expired:
                log("WARNING", "expired", comments=expired)
            self._send(only)
        if not store.get_account_hold(self.conn):   # housekeeping; the hold blocks every call to Meta
            self._refresh_token()
            self._metric()
        store.kv_set_many(self.conn, {"last_cycle_ok_at": clock.iso(), "last_cycle_error": None})
        # after the stamp: the digest of a start's first cycle said "no cycle has finished, the service is down"
        self._daily()

    def _stopping(self):
        return bool(self._should_stop and self._should_stop())

    def _checkpoint(self):
        """Called between HTTP calls, never inside a transaction."""
        if self._stopping():
            raise _Stopped()

    def _startup_events(self):
        """8.7: told once per start until the first ok full sweep clears the mark."""
        if store.kv_get(self.conn, "fresh_db"):
            self.notifier.event("fresh_db", "new empty database: budget 0, nothing is sent before `arm`; "
                                            "who was served before is known only from the owner's replies")

    def _clock_guard(self):
        """False when the clock went back or jumped ahead since the last cycle: expire and send are skipped."""
        now, mono = clock.now(), clock.monotonic()
        last_now = store.kv_get(self.conn, "last_now")
        problem = None
        if last_now and now < clock.parse(last_now) - timedelta(seconds=T["clock_back"]):
            problem = f"the clock went back from {last_now} to {clock.iso(now)}"
        elif self._last_clock:
            ahead = (now - self._last_clock[0]).total_seconds() - (mono - self._last_clock[1])
            if ahead > T["clock_jump"]:
                problem = f"the clock jumped {round(ahead)} s ahead, now {clock.iso(now)}"
        self._last_clock = now, mono
        store.kv_set_many(self.conn, {"last_now": clock.iso(now), "clock_problem": problem})
        if problem:
            log("ERROR", "clock", problem=problem)
        return problem is None

    # ---------- identity and token (8.2), account hold (8.5) ----------

    def _token_and_identity(self):
        """A token in .env that was not seen before wins. No token: nothing is called. No identity: nothing is read."""
        env, problem = read_env_checked(os.path.join(self._state_dir, ".env"))
        if problem != store.kv_get(self.conn, "env_problem"):
            store.kv_set(self.conn, "env_problem", problem)   # an unreadable file brings no token and stops nothing
        env_token = env.get("IG_ACCESS_TOKEN")
        adopted = bool(env_token) and env_token != store.kv_get(self.conn, "token_env_seen")
        if adopted:
            store.kv_set_many(self.conn, {
                "token": env_token, "token_env_seen": env_token, "token_refreshed_at": clock.iso(),
                "token_expires_at": None, "token_refresh_failed_at": None, "token_refresh_fails": None})
            log("INFO", "token_adopted")
        hold = store.get_account_hold(self.conn)
        if not store.kv_get(self.conn, "token"):
            if not hold or hold["reason"] != "no_token":
                store.set_account_hold(self.conn, "no_token")
        elif adopted or (self.me is None and (not hold or hold["reason"] == "no_token")):
            self._probe(None if self.me else "no_identity")

    def _probe(self, fallback=None):
        """GET /me. ok: the identity is stored and the account hold lifted (nothing else lifts it); True.

        Not ok: the hold is set, or its next probe moves one step out. `fallback` is the reason of the hold
        when the answer names none, given by the caller that has one: a refused call, or no identity.
        """
        result = self.client.me()
        user_id = _id(result.data.get("user_id")) if result.ok else None
        known = self.me or {}
        if user_id and known and user_id != known["user_id"]:
            log("ERROR", "identity_changed")
            store.set_account_hold(self.conn, "blocked", "identity_changed")
            return False
        app_id, username = _author(result.data)   # /me has `id` and `username` like a `from` object
        # a field that was known is never forgotten because one answer left it out (G3: own comments)
        me = {"user_id": user_id, "id": app_id or known.get("id"), "username": username or known.get("username")}
        if user_id and not all(me.values()):
            fallback, user_id = fallback or "no_identity", None   # not an identity: no own comment could be told
        if user_id:
            self.me = me
            self._account = tuple(value if type(value) is int and 0 <= value < 2**63 else None
                                  for value in (result.data.get("followers_count"), result.data.get("media_count")))
            with store.tx(self.conn):
                store.kv_set_many(self.conn, {"me_user_id": user_id, "me_id": self.me["id"],
                                              "me_username": self.me["username"], "me_checked_at": clock.iso(),
                                              "probe_fails": None})
                if store.get_account_hold(self.conn):
                    log("INFO", "account_hold_lifted")
                store.clear_account_hold(self.conn)
            return True

        hold = store.get_account_hold(self.conn)
        if result.outcome == "global":
            reason, detail = _hold_reason(result.reason), _note("me", result)
        elif fallback:
            reason, detail = fallback, _note("me", result)
        elif hold:
            reason, detail = hold["reason"], hold["detail"]
        else:
            fails = int(store.kv_get(self.conn, "probe_fails") or 0) + 1
            store.kv_set(self.conn, "probe_fails", fails)
            if fails < PROBE_FAILS:
                return False
            reason, detail = "blocked", "unreachable"
        hold = store.set_account_hold(self.conn, reason, detail)
        log("ERROR", "account_hold", reason=reason, detail=detail, next_probe_at=hold["next_probe_at"])
        return False

    # ---------- collect (8.3) ----------

    def _collect(self):
        """Every active reel on its own. Fills self._sendable."""
        for reel in self.cfg.reels:
            media_id, now = reel.media_id, clock.now()
            if not reel.active or (media_id in self._retry_at and now < self._retry_at[media_id]):
                continue
            self._checkpoint()
            last_full = store.kv_get(self.conn, f"full_sweep_at:{media_id}")
            sweep = _Sweep(media_id, media_id not in self._swept or last_full is None
                           or now - clock.parse(last_full) >= timedelta(seconds=T["full_sweep_every"]))
            try:
                self._read_reel(reel, sweep)
            except _ReelFailed as failure:
                refused = failure.result is not None and failure.result.outcome == "global"
                # an account-wide refusal is the reel's own failure only if the account itself still answers
                if refused and not self._probe(_hold_reason(failure.result.reason)):
                    log("WARNING", "sweep_failed", note=failure.note, **vars(sweep))
                    return
                self._reel_failed(sweep, failure.note, failure.state)
            except Exception as error:   # one reel failing never stops the others
                traceback.print_exc()
                self._reel_failed(sweep, f"{type(error).__name__}: {error}")
            else:
                self._reel_ok(sweep)

    def _read_reel(self, reel, sweep):
        media_id = reel.media_id
        if sweep.full:
            media = self.client.media(media_id)
            if media.outcome == "gone":
                raise _ReelFailed("the media is gone", state="gone")
            if not media.ok:
                raise _ReelFailed(_note("media", media), media)
            if reel.permalink and urlsplit(reel.permalink).path.strip("/").split("/")[-1] != media.data.get("shortcode"):
                raise _ReelFailed(f"the permalink in the config is not this media (shortcode "
                                  f"{media.data.get('shortcode')!r})", state="mismatch")
        next_url = None
        while True:
            self._checkpoint()
            page = self.client.comments_page(media_id, next_url)
            if not page.ok:
                raise _ReelFailed(_note("comments", page), page)
            items, paging = page.data.get("data"), page.data.get("paging")
            if not isinstance(items, list):
                raise _ReelFailed("comments: the answer has no list")
            sweep.pages += 1
            has_unknown = self._store_page(reel, sweep, items)
            next_url = paging.get("next") if isinstance(paging, dict) else None
            if not (isinstance(next_url, str) and next_url) or not (sweep.full or has_unknown):
                break
            if sweep.pages == MAX_PAGES:
                raise _ReelFailed(f"more than {MAX_PAGES} pages: older comments were not read", state="capped")
        if sweep.full and not sweep.comments and (media.data.get("comments_count") or 0) > 0:
            raise _ReelFailed(f"no comment listed, comments_count is {media.data['comments_count']}", state="empty")

    def _store_page(self, reel, sweep, items):
        """One page in one transaction, every comment on its own. True if the page had a comment not stored before."""
        has_unknown = False
        with store.tx(self.conn):
            for item in items:
                sweep.comments += 1
                comment_id = _id(item.get("id")) if isinstance(item, dict) else None
                if comment_id is None:
                    sweep.malformed += 1   # nothing to store it under
                    sweep.unreadable += 1
                    continue
                row = store.comment(self.conn, comment_id)
                has_unknown = has_unknown or row is None
                try:
                    with store.tx(self.conn):
                        self._store_comment(reel, sweep, item, comment_id, row)
                except Exception as error:
                    sweep.malformed += 1
                    log("WARNING", "malformed_comment", comment=comment_id, error=type(error).__name__)
                    text = item.get("text")
                    sweep.unreadable += text is not None and not isinstance(text, str)
                    sweep.new_rows += store.add_comment(self.conn, comment_id, reel.media_id, None, None,
                                                        text if isinstance(text, str) else None, None, "seen", "malformed")
        return has_unknown

    def _store_comment(self, reel, sweep, item, comment_id, row):
        """The three rules of 8.3 for one listed comment. Raises on anything malformed."""
        if item.get("hidden") is True:   # 7.4: Meta takes no public reply under a hidden comment
            self._hidden.add(comment_id)
        else:
            self._hidden.discard(comment_id)
        text, ts = item.get("text"), item.get("timestamp")
        if text is not None and not isinstance(text, str):
            raise ValueError("`text` is not a text")
        text = store.clean(text)   # decided on the text as it is stored: the claim decides on that one again
        clock.parse(ts)   # ValueError: no usable timestamp
        now = clock.now()
        user_id, username = _author(item.get("from"))
        is_own = store.is_own(self.me, user_id, username)
        owner_replied = any(store.is_own(self.me, *_author(reply.get("from"))) for reply in _replies(item))
        status = row["status"] if row else None
        matcher = self._matchers[reel.media_id]

        # 1. The owner's reply under a REQUEST is the ground truth that outlives a lost database (v1 finding
        #    A3). Under any other comment it says nothing: that person was not handled and may still ask.
        if row is None:
            # 2. a new comment
            status, reason = match.decide(matcher, text, bool(user_id), is_own)
            if status == "pending" and owner_replied:
                status, reason = "already", "owner_replied"
            elif status == "pending" and store.window_closed(ts, now):
                status, reason = "expired", "window"
            sweep.new_rows += store.add_comment(self.conn, comment_id, reel.media_id, user_id, username, text, ts,
                                                status, reason)
            sweep.new_pending += status == "pending"
        elif status == "sent" and owner_replied:
            store.transition(self.conn, comment_id, "sent", "done")
        elif status == "pending" and owner_replied:
            store.transition(self.conn, comment_id, "pending", "already", "owner_replied")
        elif status == "seen" and (sweep.full or owner_replied):
            # 3. a stored comment that was not a request: is it one under the config and the author of today?
            stored_text = row["text"] if text is None else text   # what Meta shows today: an edited comment is read
            stored_ts = ts if row["reason"] == "malformed" else row["ts"]
            decision, reason = match.decide(matcher, stored_text, bool(user_id), is_own)
            store.rematch_seen(self.conn, comment_id, user_id, username, stored_text, stored_ts, reason)
            if decision == "pending":
                if owner_replied:
                    store.transition(self.conn, comment_id, "seen", "already", "owner_replied")
                elif store.window_closed(stored_ts, now):
                    store.transition(self.conn, comment_id, "seen", "expired", "window")
                else:
                    store.transition(self.conn, comment_id, "seen", "pending")
                    sweep.rematched += 1
                    sweep.new_pending += 1

    def _reel_ok(self, sweep):
        media_id, now = sweep.media_id, clock.iso()
        values = {f"head_ok_at:{media_id}": now, f"reel_fails:{media_id}": 0, f"reel_state:{media_id}": "ok"}
        if sweep.full:
            values.update({f"full_sweep_at:{media_id}": now, "fresh_db": None,
                           f"unreadable:{media_id}": sweep.unreadable or None})
        store.kv_set_many(self.conn, values)
        if sweep.full:
            self._swept.add(media_id)
        if sweep.full or sweep.new_rows:   # an idle head poll only updates kv: no line
            log("INFO", "sweep", **vars(sweep))
        self._sendable.add(media_id)   # a collect that is not a full sweep only follows one of this run (G6)

    def _reel_failed(self, sweep, note, state=None):
        media_id = sweep.media_id
        with store.tx(self.conn):
            fails = int(store.kv_get(self.conn, f"reel_fails:{media_id}") or 0) + 1
            store.kv_set(self.conn, f"reel_fails:{media_id}", fails)
            if state:
                store.kv_set(self.conn, f"reel_state:{media_id}", state)
        if state in ("gone", "mismatch", "capped"):   # read again at the next full sweep, not in every cycle
            self._retry_at[media_id] = clock.now() + timedelta(seconds=T["full_sweep_every"])
        log("WARNING", "sweep_failed", fails=fails, note=note, **vars(sweep))

    # ---------- send (8.4) ----------

    def _send(self, only):
        """Public replies owed, then DMs, for sendable reels. Every other gate of G6 is checked by the claim."""
        reels = sorted(self._sendable)
        marks = ",".join("?" * len(reels))
        owed = self.conn.execute(f"SELECT comment_id FROM comments WHERE status='sent' AND media_id IN ({marks}) "
                                 "ORDER BY ts, comment_id", reels).fetchall()
        for (comment_id,) in owed:
            if not self._public_reply(comment_id):
                break   # the gate is closed for the others too: no pause for each of them
        queue = self.conn.execute(
            f"SELECT comment_id, media_id FROM comments WHERE status='pending' AND media_id IN ({marks}) "
            "AND comment_id=COALESCE(?, comment_id) ORDER BY deferrals, ts, comment_id", (*reels, only)).fetchall()
        attempts, slot = 0, T["rate_window"] / self.cfg.max_sends_per_hour
        for comment_id, media_id in queue:
            if attempts == MAX_DMS_PER_CYCLE:
                break
            self._checkpoint()
            # G2 is measured at the receiver: within one process run the slot is also kept on the monotonic
            # clock, which no step of the wall clock can shorten
            taken = 0 if self._last_post is None else max(0, math.ceil(
                (slot - (clock.monotonic() - self._last_post)) * 1000) / 1000)
            claim = store.claim_dm(self.conn, comment_id, self.cfg, self.me, max(taken, 0))
            if claim.kind == "wait":
                log("INFO", "rate_wait", seconds=claim.seconds, startup=bool(self._startup_wait and taken > 0))
            if claim.kind == "wait" and taken > 0 and self._startup_wait and self._wait_for_startup:
                self._wait_for_startup = False   # one cancellable startup sleep, then every gate is rechecked
                clock.sleep(claim.seconds, self._should_stop, self._ping)
                self._checkpoint()
                taken = max(0, math.ceil((slot - (clock.monotonic() - self._last_post)) * 1000) / 1000)
                claim = store.claim_dm(self.conn, comment_id, self.cfg, self.me, taken)
            if claim.kind == "skip":
                continue
            if claim.kind != "claimed":
                if claim.kind == "wait":
                    self.pause = min(self.pause, claim.seconds)   # DMs are flowing: sleep only until the slot
                break
            log("INFO", "claim_committed", pid=os.getpid(), monotonic=claim.monotonic,
                attempt=claim.attempt_id, kind="dm", started_at=claim.started_at)
            attempts += 1
            self._startup_wait = False
            try:
                result = self.client.send_dm(self.me["user_id"], comment_id, self._messages[media_id, claim.late])
            finally:
                # A client exception after the committed claim is unknown too. Keep D unless an answer
                # proves response completion or that the request never connected.
                self._last_post = clock.monotonic() + T["http_deadline"]
            if result.http or result.connect_failed:
                self._last_post -= T["http_deadline"]
            status = store.record_dm(self.conn, claim.attempt_id, result)
            self._after_post(claim.attempt_id, comment_id, "dm", result)
            if claim.stage:
                self._stage_done(claim.stage, claim.attempt_id)
            if status != "sent":
                break
            self._dms += 1
            self._public_reply(comment_id)

    def _public_reply(self, comment_id):
        """7.4 for one `sent` comment. False when a gate is closed."""
        if self.cfg.public_reply and comment_id not in self._hidden:
            clock.sleep(random.uniform(*T["public_delay"]), self._should_stop, self._ping)
        self._checkpoint()
        claim = store.claim_public(self.conn, comment_id, self.cfg, self.me, comment_id in self._hidden)
        if claim.kind == "claimed":
            log("INFO", "claim_committed", pid=os.getpid(), monotonic=claim.monotonic,
                attempt=claim.attempt_id, kind="public", started_at=claim.started_at)
            result = self.client.public_reply(comment_id, claim.phrase)
            store.record_public(self.conn, claim.attempt_id, result)
            self._after_post(claim.attempt_id, comment_id, "public", result)
        return claim.kind in ("claimed", "skip")

    def _after_post(self, attempt_id, comment_id, kind, result):
        """The journal line of SPEC 3. The send hold was set with the journal row; the engine adds the probe (8.5)."""
        log("INFO", "attempt", id=attempt_id, comment=comment_id, kind=kind, http=result.http, code=result.code,
            subcode=result.subcode, outcome=result.outcome, reason=result.reason)
        if result.outcome == "global" and result.reason != "spam":
            self._probe(_hold_reason(result.reason))

    def _stage_done(self, stage, attempt_id):
        armed_n, through = stage
        totals = self.conn.execute(
            "SELECT outcome, reason, COUNT(*) FROM attempts WHERE kind='dm' AND id>? AND id<=? "
            "GROUP BY outcome, reason ORDER BY outcome, reason", (through, attempt_id)).fetchall()
        self.notifier.event("stage_done", f"the budget of {armed_n} is used up: " + ", ".join(
            f"{outcome}{'/' + reason if reason else ''} {count}" for outcome, reason, count in totals))

    # ---------- housekeeping (8.2, 8.7) ----------

    def _refresh_token(self):
        """At least 24 h old and (older than 7 days or expiry unknown); after a failure once per 6 h. Never a hold."""
        now = clock.now()
        refreshed_at = store.kv_get(self.conn, "token_refreshed_at")
        if not refreshed_at:   # v1 issue K15: a token of unknown age was never refreshed
            store.kv_set(self.conn, "token_refreshed_at", clock.iso(now))
            return
        age = (now - clock.parse(refreshed_at)).total_seconds()
        failed_at = store.kv_get(self.conn, "token_refresh_failed_at")
        if age < T["token_min_age"] or (age <= T["token_refresh_age"] and store.kv_get(self.conn, "token_expires_at")) \
                or (failed_at and (now - clock.parse(failed_at)).total_seconds() < T["token_retry_after"]):
            return
        result = self.client.refresh_token(store.kv_get(self.conn, "token"))
        token, expires_in = result.data.get("access_token"), result.data.get("expires_in")
        if result.ok and isinstance(token, str) and token:
            store.kv_set_many(self.conn, {
                "token": token, "token_refreshed_at": clock.iso(now),
                "token_expires_at": _expiry(now, expires_in),
                "token_refresh_failed_at": None, "token_refresh_fails": None})
            log("INFO", "token_refreshed")
        else:
            fails = int(store.kv_get(self.conn, "token_refresh_fails") or 0) + 1
            store.kv_set_many(self.conn, {"token_refresh_failed_at": clock.iso(now), "token_refresh_fails": fails})
            log("WARNING", "token_refresh_failed", outcome=result.outcome, reason=result.reason, http=result.http,
                code=result.code, fails=fails)

    def _metric(self):
        """Hourly: followers and media count of a GET /me that is not older than the hour."""
        now, every = clock.now(), timedelta(seconds=T["metric_every"])
        metric_at, checked_at = store.kv_get(self.conn, "metric_at"), store.kv_get(self.conn, "me_checked_at")
        if metric_at and now - clock.parse(metric_at) < every:
            return
        fresh = self._account and checked_at and now - clock.parse(checked_at) < every
        if fresh or self._probe():
            store.add_metric(self.conn, *self._account)

    def _daily(self):
        """Once a day, from digest_hour_utc on: the digest."""
        now = clock.now()
        daily_at = store.kv_get(self.conn, "daily_at")
        if now.hour < self.cfg.alerts.digest_hour_utc or (daily_at and clock.parse(daily_at).date() >= now.date()):
            return
        import report   # imported where it is used: report.py imports nothing from here, tests may replace it
        cfg, file_error = self._file_config()
        text = report.digest(report.build(self.conn, cfg, now, **file_error))
        with store.tx(self.conn):
            if self.notifier.event("digest", text) is False:
                raise sqlite3.OperationalError("digest could not be queued")
            store.kv_set(self.conn, "daily_at", clock.iso(now))

    # ---------- problems and alerts (9) ----------

    def _announce(self):
        import report
        try:
            self._lost_people()
            now = clock.now()
            cfg, file_error = self._file_config()
            self.notifier.sync(report.problems(self.conn, cfg, now, **file_error), now)
        finally:
            self.notifier.flush()   # what is queued goes out even when the problems could not be worked out

    def _file_config(self):
        """(the config file as it is now, {}), or (the running config, why the file cannot be loaded).
        report.py only compares it (restart_needed, config_file_invalid): nothing of the file is applied."""
        if not self._config_path:
            return self.cfg, {}
        try:
            return config.load(self._config_path), {}
        except config.ConfigError as error:
            return self.cfg, {"config_file_error": "; ".join(error.problems)}

    def _lost_people(self):
        """Events `expired` and `uncertain`: the count rose since it was last told (it is kept across restarts).

        `expired` is said at once the first time; while windows keep closing, the next message comes no sooner than
        alerts.repeat_min later and counts everything since. Windows that close one per cycle are not a message each."""
        texts = {"expired": "{} people lost: their comments are past the 7-day window ({} in all)",
                 "uncertain": "{} DM(s) may or may not have been delivered: they are never sent again ({} in all)"}
        with store.tx(self.conn):
            now = clock.now()
            for status, text in texts.items():
                count = self.conn.execute("SELECT COUNT(*) FROM comments WHERE status=?", (status,)).fetchone()[0]
                told = int(store.kv_get(self.conn, "told_" + status) or 0)
                if count <= told:             # these two statuses are final (7.1): the count never falls
                    continue
                marks = {"told_" + status: count}
                if status == "expired":
                    last = store.kv_get(self.conn, "told_expired_at")
                    # 0 <= ...: a wall clock that went back says it now rather than staying silent
                    if last and 0 <= (now - clock.parse(last)).total_seconds() < self.cfg.alerts.repeat_min * 60:
                        continue              # untold for now: the mark is not moved, the difference adds up
                    marks["told_expired_at"] = clock.iso(now)
                if self.notifier.event(status, text.format(count - told, count)) is False:
                    raise sqlite3.OperationalError(f"{status} notification could not be queued")
                store.kv_set_many(self.conn, marks)
