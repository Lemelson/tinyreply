"""The database (SPEC section 7): the schema and every write. No other module contains SQL that writes.

What this module guarantees:
- a DM is claimed in ONE committed transaction before the POST leaves (comment `sending`, journal row, budget),
  so no crash, restart or second process can claim the same comment or the same person again (G1, G4);
- the slot, the hourly cap, the holds and every "N in a row" are queries over the append-only `attempts`
  journal, never a counter in memory (G2);
- nothing is claimed while a gate that the database or the config can show is closed (G6).

Interface (a function that writes is one transaction; called inside tx() it becomes a savepoint of that one):
    connect, init, tx                                             connection, schema, BEGIN IMMEDIATE block
    kv_get, kv_set, kv_set_many                                   kv
    budget, arm                                                   send budget (8.6)
    get_account_hold, set_account_hold, clear_account_hold, probe_wait            account hold (8.5)
    get_send_hold, set_send_hold, uncertain_wait, blocked_wait, streak_wait       send hold (8.5)
    comment, add_comment, rematch_seen, transition, expire, is_own, window_closed   comments (7.1, 8.3)
    claim_dm, record_dm, recover_stale                            the DM (7.2, 7.3)
    claim_public, record_public                                   the public reply (7.4)
    add_metric, outbox_add, outbox_sent, problem_announce, problem_clear
"""
import json
import math
import random
import sqlite3
import time
from collections import namedtuple
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import clock
import match
from clock import T

SCHEMA_VERSION = 3
_BUSY_PAUSE = 0.05        # real seconds between two tries to switch a busy database to WAL
# 7.2 step 2: the rows that stand for "this person has (or may have) the DM of this media"
HANDLED = "{}status IN ('sending','sent','done','uncertain','already')"
_HANDLED_OTHER = HANDLED.format("o.")   # the same test for a row named `o`
HANDLED = HANDLED.format("")

_SCHEMA = """
CREATE TABLE comments (
  comment_id TEXT PRIMARY KEY, media_id TEXT NOT NULL,
  user_id TEXT, username TEXT, text TEXT, ts TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('seen','pending','sending','sent','done','uncertain','refused','expired','dup_user','already')),
  reason TEXT NOT NULL DEFAULT '',
  deferrals INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_comments_status ON comments(status, ts);
CREATE INDEX idx_comments_person ON comments(media_id, user_id);
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY, comment_id TEXT NOT NULL, kind TEXT NOT NULL,
  detail TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  http INTEGER, code INTEGER, subcode INTEGER, message TEXT,
  outcome TEXT, reason TEXT, meta_id TEXT);
CREATE INDEX idx_attempts_kind ON attempts(kind, started_at);
CREATE INDEX idx_attempts_comment ON attempts(comment_id);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE metrics (ts TEXT PRIMARY KEY, followers INTEGER, media_count INTEGER);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, key TEXT, text TEXT, created_at TEXT, sent_at TEXT);
CREATE TABLE problems (key TEXT PRIMARY KEY, text TEXT, since TEXT, last_announced_at TEXT)
"""

# 14.6: the funnel tables, added by schema version 3 (a version-2 database gets exactly these)
_SCHEMA_V3 = """
CREATE TABLE flow (comment_id TEXT PRIMARY KEY, media_id TEXT NOT NULL, variant TEXT NOT NULL,
  opening INTEGER NOT NULL, gate INTEGER NOT NULL, state TEXT NOT NULL,
  igsid TEXT, pressed_at TEXT, last_in_at TEXT, follow TEXT, prompts INTEGER NOT NULL DEFAULT 0,
  link_at TEXT, followup_at TEXT, unfollowed_at TEXT, created_at TEXT NOT NULL);
CREATE INDEX idx_flow_igsid ON flow(igsid);
CREATE TABLE checks (id INTEGER PRIMARY KEY, comment_id TEXT NOT NULL, at TEXT NOT NULL, purpose TEXT NOT NULL,
  result TEXT NOT NULL);
CREATE INDEX idx_checks_comment ON checks(comment_id);
CREATE TABLE inbound (id INTEGER PRIMARY KEY, mid TEXT UNIQUE NOT NULL, received_at TEXT NOT NULL,
  sender TEXT, kind TEXT NOT NULL, payload TEXT, text TEXT, handled_at TEXT, result TEXT);
CREATE INDEX idx_inbound_open ON inbound(handled_at, id);
CREATE TABLE links (token TEXT PRIMARY KEY, comment_id TEXT NOT NULL, media_id TEXT NOT NULL, variant TEXT,
  position INTEGER NOT NULL, url TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX idx_links_comment ON links(comment_id);
CREATE TABLE clicks (id INTEGER PRIMARY KEY, token TEXT NOT NULL, at TEXT NOT NULL, agent TEXT);
CREATE INDEX idx_clicks_token ON clicks(token)
"""

# 7.1: every allowed status change
_NEXT = {
    "seen": ("pending", "expired", "already"),
    "pending": ("sending", "seen", "expired", "dup_user", "already"),
    "sending": ("sent", "uncertain", "refused", "pending", "already"),
    "sent": ("done",),
}
_NEW_STATUSES = ("seen", "pending", "expired", "already")   # what collect may store a comment as

# kind: 'claimed', or why not: 'skip' (this row is settled, take the next one), 'no_budget', 'wait' (with the
# seconds until the slot is free), 'cap', or a closed gate: 'no_identity', 'sending_off', 'account_hold', 'send_hold'
Claim = namedtuple("Claim", "kind attempt_id late seconds stage monotonic started_at variant tokens",
                   defaults=(None, None, 0, None, None, None, None, ()))
# 14.4: kind 'claimed' (POST, with `urls` for the link buttons), 'skip' (this step was already tried), 'cap',
# or a closed gate
StepClaim = namedtuple("StepClaim", "kind attempt_id urls", defaults=(None, ()))
PublicClaim = namedtuple("PublicClaim", "kind attempt_id phrase monotonic started_at", defaults=(None, None, None, None))


# ---------- connection, transaction, schema ----------

def connect(path, readonly=False):
    """Open the database. Never creates or alters the schema (init does); read-only fails if there is no file."""
    target = Path(path).absolute().as_uri() + "?mode=ro" if readonly else path
    conn = sqlite3.connect(target, uri=readonly, timeout=T["db_busy_timeout"], isolation_level=None)
    conn.row_factory = sqlite3.Row
    if not readonly:
        try:
            _set_wal(conn)
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()   # v1: a connection that failed while opening kept its lock until garbage collection
            raise
    return conn


def _set_wal(conn):
    """journal_mode=WAL, waiting like every other statement while another connection is at work.

    A new file is switched to WAL by its first opener, and SQLite refuses the switch at once, without
    busy_timeout, while another connection touches the file: a command given in the instant of the very first
    start ended with "database is locked", and so could the start itself (exit 6, which systemd does not restart).
    """
    give_up = time.monotonic() + T["db_busy_timeout"]
    while True:
        try:
            return conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY or time.monotonic() >= give_up:
                raise
            time.sleep(_BUSY_PAUSE)


@contextmanager
def tx(conn):
    """One BEGIN IMMEDIATE transaction. Inside another tx() it is a savepoint: all of it or none of it."""
    nested = conn.in_transaction
    conn.execute("SAVEPOINT step" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("RELEASE step" if nested else "COMMIT")
    except BaseException:
        if nested and conn.in_transaction:
            conn.execute("ROLLBACK TO step")
            conn.execute("RELEASE step")
        elif conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _outside_tx(conn):
    if conn.in_transaction:
        raise RuntimeError("a claim must be committed before the POST: do not call it inside tx()")


def init(conn):
    """Create the schema in an empty database and mark it `fresh_db`; True if it did. Never alters a schema."""
    with tx(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return False
        if version == 2:   # 14.6: the funnel tables only; nothing of version 2 changes
            for statement in _SCHEMA_V3.split(";"):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return False
        if version != 0:
            raise RuntimeError(f"database schema version {version}: this code only knows version {SCHEMA_VERSION}")
        for statement in (_SCHEMA + ";" + _SCHEMA_V3).split(";"):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        kv_set(conn, "fresh_db", 1)
        return True


def clean(value):
    """A value as a TEXT column can hold it. Meta's JSON can carry a lone surrogate (half an emoji), which SQLite
    refuses: it becomes U+FFFD, a character the matcher gives no meaning to (a `?` made a request a question)."""
    return None if value is None else str(value).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


_text = clean


# ---------- kv ----------

def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return default if row is None else row[0]


def kv_set(conn, key, value):
    """Store str(value); None removes the key."""
    if value is None:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, _text(value)))


def kv_set_many(conn, values):
    with tx(conn):
        for key, value in values.items():
            kv_set(conn, key, value)


# ---------- budget and arming (8.6) ----------

def budget(conn):
    """How many DMs may still be claimed: a whole number or 'unlimited'. Absent or unreadable is 0 (G6)."""
    value = kv_get(conn, "send_budget")
    if value == "unlimited":
        return value
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def arm(conn, n):
    """SET the budget to n: a whole number (0 is `disarm`) or 'unlimited'. Takes effect at the next claim."""
    if n != "unlimited" and (not isinstance(n, int) or isinstance(n, bool) or n < 0):
        raise ValueError(f"budget must be a whole number, 0 or more, or 'unlimited': {n!r}")
    with tx(conn):
        through = conn.execute("SELECT COALESCE(MAX(id),0) FROM attempts").fetchone()[0]
        kv_set_many(conn, {"send_budget": n, "armed_at": clock.iso(), "armed_n": n,
                           "armed_through_attempt_id": through})


# ---------- holds (8.5) ----------

def probe_wait(reason, n):
    """Seconds until the next probe of an account hold after n failed probes: 5, 10, 20 min, then every 30.

    A `rate` hold is never probed sooner than 15 min.
    """
    schedule = T["probe_schedule"]
    wait = schedule[min(n, len(schedule) - 1)]
    return max(wait, T["probe_first_rate"]) if reason == "rate" else wait


def get_account_hold(conn):
    value = kv_get(conn, "account_hold")
    return json.loads(value) if value else None


def set_account_hold(conn, reason, detail=""):
    """Set the account hold, or record one more failed probe of the hold in force: `since` stays, the next
    probe moves one step out along the schedule. Returns the hold."""
    with tx(conn):
        old = get_account_hold(conn)
        now = clock.now()
        n = old["n"] + 1 if old else 0
        hold = {"reason": reason, "since": old["since"] if old else clock.iso(now), "n": n,
                "next_probe_at": clock.iso(now + timedelta(seconds=probe_wait(reason, n))), "detail": detail}
        kv_set(conn, "account_hold", json.dumps(hold))
        return hold


def clear_account_hold(conn):
    """Only a successful probe may call this."""
    kv_set(conn, "account_hold", None)


def get_send_hold(conn, now=None):
    """The send hold while it is in force at `now` (default: this moment), else None."""
    value = kv_get(conn, "send_hold")
    hold = json.loads(value) if value else None
    return hold if hold and hold["until"] > clock.iso(now) else None


def set_send_hold(conn, reason, seconds, detail=""):
    """No DM and no public reply for `seconds` from now. `until` only moves later: a longer hold stays as it is."""
    with tx(conn):
        until = clock.iso(clock.now() + timedelta(seconds=seconds))
        old = kv_get(conn, "send_hold")
        if not old or json.loads(old)["until"] < until:
            kv_set(conn, "send_hold", json.dumps({"reason": reason, "until": until, "detail": detail}))


def _in_a_row(conn, kinds, counted, passed_over="0"):
    """How many of the newest attempts of these kinds, newest first, satisfy `counted` before one does not.

    `kinds` is an SQL list, `counted` and `passed_over` SQL conditions. An attempt that could not connect
    (`not_sent`) never reached Meta, says nothing about it, and is passed over; so is one that `passed_over` names.
    """
    of_kind = f"kind IN ({kinds}) AND outcome IS NOT 'not_sent' AND NOT ({passed_over})"
    return conn.execute(
        f"SELECT COUNT(*) FROM attempts WHERE {of_kind} AND id > COALESCE("
        f"(SELECT id FROM attempts WHERE {of_kind} AND NOT ({counted}) ORDER BY id DESC LIMIT 1), 0)").fetchone()[0]


def _doubled(name, steps):
    return min(T[name + "_base"] * 2 ** steps, T[name + "_max"])


def uncertain_wait(conn):
    """Hold seconds after an uncertain DM: 15 min, doubled for each further uncertain DM in a row, at most 6 h."""
    k = _in_a_row(conn, "'dm'", "outcome IS 'uncertain'")
    return _doubled("hold_uncertain", k - 1) if k else 0


def blocked_wait(conn):
    """Hold seconds after an account-wide refusal of a POST: 30 min, doubled for each further one, at most 12 h."""
    k = _in_a_row(conn, "'dm','public','step'", "outcome IS 'global'")
    return _doubled("hold_blocked", k - 1) if k else 0


def streak_wait(conn):
    """Hold seconds once 5 DMs in a row went without `ok`: 1 h, doubled for each one beyond 5, at most 12 h.

    Meta's "already replied" is passed over: that person HAS the reply, so it neither counts nor ends the run.
    """
    n = _in_a_row(conn, "'dm'", "outcome IS NOT 'ok'", "outcome IS 'rejected' AND reason IS 'already_replied'")
    return _doubled("hold_streak", n - 5) if n >= 5 else 0


def _hold_after(conn, kind, outcome, reason):
    """The send-hold table of 8.5, applied in the transaction that journaled this POST: a crash cannot lose it."""
    if outcome in ("ok", "not_sent") or (outcome, reason) == ("rejected", "already_replied"):
        return
    wanted = []
    if kind == "dm" and outcome == "uncertain":
        wanted.append(("uncertain", uncertain_wait(conn)))
    if outcome == "global" and reason == "spam":
        wanted.append(("spam", T["hold_spam"]))
        kv_set(conn, "send_budget", 0)   # a human must re-arm
    elif outcome == "global":
        wanted.append(("send_blocked", blocked_wait(conn)))
    if kind == "dm":
        wanted.append(("streak", streak_wait(conn)))
    name, seconds = max(wanted, key=lambda rule: rule[1], default=("", 0))
    if seconds:
        set_send_hold(conn, name, seconds, f"{kind} {outcome}/{reason}")


def _closed_gate(conn, cfg, now):
    """The first G6 condition visible here that is not met, or None. The budget is checked by the DM claim alone."""
    if not cfg.sending:
        return "sending_off"
    if get_account_hold(conn):
        return "account_hold"
    if get_send_hold(conn, now):
        return "send_hold"
    return None


# ---------- comments ----------

def comment(conn, comment_id):
    return conn.execute("SELECT * FROM comments WHERE comment_id=?", (comment_id,)).fetchone()


def is_own(me, user_id, username):
    """8.2: the account's own comment or reply. `me` is {user_id, id, username}."""
    return (bool(user_id) and user_id in (me.get("user_id"), me.get("id"))) or \
        (bool(username) and username == me.get("username"))


def window_closed(ts, now):
    """Meta's 7-day reply window, less the safety margin, is over for a comment written at `ts` (7.2 step 1)."""
    return clock.parse(ts) <= now - timedelta(seconds=T["reply_window"] - T["window_margin"])


def too_old_sql():
    """SQL test (7.1, 9): an `expired` row that is NOT a person lost. A comment that was already past the window
    when the bot first saw it is `expired`/`too_old` (8.3 rules 2-3); rows stored before that reason existed say
    `window`, and are told apart by `first_seen_at` (ts and first_seen_at share clock.iso's format)."""
    seconds = T["reply_window"] - T["window_margin"]
    return (f"(status='expired' AND (reason='too_old' OR "
            f"first_seen_at >= strftime('%Y-%m-%dT%H:%M:%fZ', ts, '+{seconds} seconds')))")


def lost_sql():
    """SQL test (9): an `expired` row that is a person lost: the bot saw the comment while it could still serve it."""
    return f"(status='expired' AND NOT {too_old_sql()})"


def add_comment(conn, comment_id, media_id, user_id, username, text, ts, status, reason=""):
    """Store a comment Meta listed for the first time. False, and nothing changes, if it is already stored.

    `ts` is Meta's timestamp; None (a malformed comment) stores the moment it was first seen.
    """
    if status not in _NEW_STATUSES:
        raise ValueError(f"a new comment cannot be {status!r}")
    if not (isinstance(comment_id, str) and comment_id and isinstance(media_id, str) and media_id):
        raise ValueError("comment_id and media_id must be non-empty strings")
    now = clock.iso()
    ts = now if ts is None else clock.iso(clock.parse(ts))   # one format, so that text order is time order
    return conn.execute(
        "INSERT INTO comments (comment_id, media_id, user_id, username, text, ts, status, reason, "
        "first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(comment_id) DO NOTHING",
        (_text(comment_id), _text(media_id), _text(user_id), _text(username), _text(text), ts, status, reason,
         now, now)).rowcount == 1


def rematch_seen(conn, comment_id, user_id, username, text, ts, reason):
    """Save the observation of today and the full decision of a still-seen comment. Never reopens a final row.

    The text is the one Meta lists now; the caller repairs the time only of a first observation that was unusable.
    Writes only a row that changed: True if it did.
    """
    new = (_text(user_id), _text(username), _text(text), clock.iso(clock.parse(ts)), reason)
    return conn.execute("UPDATE comments SET user_id=?, username=?, text=?, ts=?, reason=?, updated_at=? "
                        "WHERE comment_id=? AND status='seen' AND (user_id IS NOT ? OR username IS NOT ? "
                        "OR text IS NOT ? OR ts IS NOT ? OR reason IS NOT ?)",
                        (*new, clock.iso(), comment_id, *new)).rowcount == 1


def transition(conn, comment_id, was, to, reason=""):
    """Move a comment from status `was` to status `to`; True if it moved (False: it is not `was` any more).

    ValueError for a change that 7.1 does not allow, whatever the row is.
    """
    if to not in _NEXT.get(was, ()):
        raise ValueError(f"status change {was!r} -> {to!r} is not allowed")
    return conn.execute("UPDATE comments SET status=?, reason=?, updated_at=? WHERE comment_id=? AND status=?",
                        (to, reason, clock.iso(), comment_id, was)).rowcount == 1


def expire(conn):
    """Every pending comment whose reply window is over becomes `expired`/`window`; returns how many."""
    now = clock.now()
    cutoff = clock.iso(now - timedelta(seconds=T["reply_window"] - T["window_margin"]))
    return conn.execute("UPDATE comments SET status='expired', reason='window', updated_at=? "
                        "WHERE status='pending' AND ts<=?", (clock.iso(now), cutoff)).rowcount


def handled_other(conn, media_id, user_id, comment_id):
    """7.2 step 2: the id of another comment of this person under this media that stands for their one DM, or None."""
    other = conn.execute(
        f"SELECT comment_id FROM comments WHERE media_id=? AND user_id=? AND comment_id<>? AND {HANDLED} "
        "ORDER BY ts, comment_id LIMIT 1", (media_id, user_id, comment_id)).fetchone()
    return other and other[0]


def queue(conn, media_ids):
    """The pending comments that will take a turn, in the order of 8.4: [(comment_id, media_id, user_id, ts)].

    One turn per person and media: a later comment of the same person takes none, and neither does a comment
    of a person who is already handled (7.2 step 2). The report, the alerts and `arm` count people with this.
    """
    media_ids = list(media_ids)
    turns, people = [], set()
    for comment_id, media_id, user_id, ts in conn.execute(
            f"SELECT comment_id, media_id, user_id, ts FROM comments c WHERE status='pending' AND media_id IN "
            f"({','.join('?' * len(media_ids))}) AND NOT EXISTS (SELECT 1 FROM comments o WHERE o.media_id=c.media_id "
            f"AND o.user_id=c.user_id AND o.comment_id<>c.comment_id AND {_HANDLED_OTHER}) "
            f"ORDER BY deferrals, ts, comment_id", media_ids):
        if (media_id, user_id) not in people:
            people.add((media_id, user_id))
            turns.append((comment_id, media_id, user_id, ts))
    return turns


# ---------- the DM (7.2, 7.3) ----------

def claim_dm(conn, comment_id, cfg, me, slot_wait=0):
    """Claim the one DM of a pending comment, committed before the POST. Returns Claim; send only if `claimed`.

    `slot_wait`: seconds the caller knows the slot is still taken for, by a clock the journal does not have
    (the engine: monotonic cooldown since the last POST, or the safe startup anchor).
    """
    _outside_tx(conn)
    with tx(conn):
        row = comment(conn, comment_id)
        if row is None or row["status"] != "pending":
            return Claim("skip")
        if not (me and me.get("user_id")):
            return Claim("no_identity")   # without it nobody can tell the account's own comments (G3)
        now = clock.now()

        # 0. still a request under the config this process runs with?
        reel = cfg.reel_by_media.get(row["media_id"])
        if reel is None or not reel.active:
            status, reason = "seen", "no_reel"
        else:
            status, reason = match.decide(match.Matcher(reel), row["text"], bool(row["user_id"]),
                                          is_own(me, row["user_id"], row["username"]))
        if status != "pending":
            transition(conn, comment_id, "pending", "seen", reason)
            return Claim("skip")

        # 1. inside Meta's window?
        if window_closed(row["ts"], now):
            transition(conn, comment_id, "pending", "expired", "window")
            return Claim("skip")

        # 2. one DM per person and media (G1)
        other = handled_other(conn, row["media_id"], row["user_id"], comment_id)
        if other:
            transition(conn, comment_id, "pending", "dup_user", "dup:" + other)
            return Claim("skip")

        # 3. gates (G6)
        gate = _closed_gate(conn, cfg, now)
        if gate:
            return Claim(gate)
        left = budget(conn)
        if left == 0:
            return Claim("no_budget")

        # 4. one slot per DM, and the hourly cap (G2). The journal counts every attempt, in flight and failed too.
        last = conn.execute(
            "SELECT started_at, finished_at FROM attempts WHERE kind='dm' ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            # G2 is measured where the POST is received: Meta got the last one before its answer came back
            # (finished_at) and gets the next one after this claim, so the gap there is at least one slot
            slot = timedelta(seconds=T["rate_window"] / cfg.max_sends_per_hour)
            free_at = clock.parse(max(filter(None, last))) + slot
            if now < free_at:
                slot_wait = max(slot_wait, math.ceil((free_at - now) / timedelta(milliseconds=1)) / 1000)
        if slot_wait > 0:
            return Claim("wait", seconds=slot_wait)
        counted_from = clock.iso(now - timedelta(seconds=T["rate_window"] + T["cap_margin"]))
        recent = conn.execute("SELECT COUNT(*) FROM attempts WHERE kind='dm' "
                              "AND MAX(started_at, COALESCE(finished_at,started_at))>?",
                              (counted_from,)).fetchone()[0]
        if recent >= cfg.max_sends_per_hour:
            return Claim("cap")

        # 5. the claim
        late = clock.parse(row["ts"]) < now - timedelta(minutes=cfg.late_after_minutes)
        started_at = clock.iso(now)
        monotonic = time.monotonic()
        transition(conn, comment_id, "pending", "sending")
        attempt_id = conn.execute(
            "INSERT INTO attempts (comment_id, kind, detail, started_at) VALUES (?, 'dm', ?, ?)",
            (comment_id, "late" if late else "fresh", started_at)).lastrowid
        if left != "unlimited":
            kv_set(conn, "send_budget", left - 1)   # never refunded, whatever the answer
        stage = None
        if left == 1:
            # arm/disarm may run during the POST. Its completed stage belongs to this claim's epoch.
            stage = (kv_get(conn, "armed_n"), int(kv_get(conn, "armed_through_attempt_id") or 0))
        variant, tokens = _flow_claim(conn, cfg, reel, row, started_at)
        return Claim("claimed", attempt_id, late, stage=stage, monotonic=monotonic, started_at=started_at,
                     variant=variant, tokens=tokens)


def _answer(result, id_field):
    """Meta's answer to a POST as the journal holds it."""
    meta_id = result.data.get(id_field)
    # G4: only these four outcomes are clear; anything else can never lead to a second POST
    outcome = result.outcome if result.outcome in ("ok", "not_sent", "global", "rejected") else "uncertain"
    return {"http": result.http, "code": result.code, "subcode": result.subcode, "message": clean(result.message),
            "outcome": outcome, "reason": result.reason, "meta_id": clean(meta_id) if isinstance(meta_id, str) else None}


def _close(conn, attempt_id, kind, answer):
    """Write Meta's answer into the open journal row; returns the row's comment_id."""
    row = conn.execute("SELECT comment_id FROM attempts WHERE id=? AND kind=? AND finished_at IS NULL",
                       (attempt_id, kind)).fetchone()
    if row is None:
        raise ValueError(f"attempt {attempt_id!r} is not an open {kind} attempt")
    conn.execute(
        "UPDATE attempts SET finished_at=?, http=?, code=?, subcode=?, message=?, outcome=?, reason=?, meta_id=? "
        "WHERE id=?",
        (clock.iso(), answer["http"], answer["code"], answer["subcode"], answer["message"], answer["outcome"],
         answer["reason"], answer["meta_id"], attempt_id))
    return row[0]


def _settle(conn, comment_id, was, to, reason):
    if not transition(conn, comment_id, was, to, reason):
        raise RuntimeError(f"comment {comment_id} is not {was!r}: the answer to its POST cannot be recorded")


def record_dm(conn, attempt_id, result):
    """Journal the answer to a claimed DM and settle the comment, in one transaction. Returns the new status."""
    with tx(conn):
        answer = _answer(result, "message_id")
        outcome, why = answer["outcome"], answer["reason"]
        comment_id = _close(conn, attempt_id, "dm", answer)
        if outcome == "ok":
            to, reason = "sent", ""
        elif outcome == "not_sent":
            to, reason = "pending", ""
        elif outcome == "global":
            to, reason = "pending", ""
            conn.execute("UPDATE comments SET deferrals=deferrals+1 WHERE comment_id=?", (comment_id,))
        elif outcome == "rejected" and why == "already_replied":
            to, reason = "already", "already_replied"
        elif outcome == "rejected":
            to, reason = "refused", why
        else:
            to, reason = "uncertain", why
        _settle(conn, comment_id, "sending", to, reason)
        if outcome == "ok":   # 14.3: a first DM without a button carried the link itself
            conn.execute("UPDATE flow SET link_at=? WHERE comment_id=? AND state='direct' AND link_at IS NULL",
                         (clock.iso(), comment_id))
        _hold_after(conn, "dm", outcome, why)
        return to


def recover_stale(conn):
    """Top of every cycle: a claim that is still open belongs to a dead run. Returns how many comments it settled.

    The interrupted DM may have been delivered, so it counts as an uncertain DM for the send hold as well:
    a crash loop must not spend one person per restart.
    """
    with tx(conn):
        now = clock.iso()
        open_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM attempts WHERE finished_at IS NULL")}
        settled = conn.execute(
            "UPDATE comments SET status='done', reason='public_uncertain', updated_at=? WHERE status='sent' "
            "AND comment_id IN (SELECT comment_id FROM attempts WHERE kind='public' AND finished_at IS NULL)",
            (now,)).rowcount
        settled += conn.execute("UPDATE comments SET status='uncertain', reason='interrupted', updated_at=? "
                                "WHERE status='sending'", (now,)).rowcount
        conn.execute("UPDATE attempts SET finished_at=?, outcome='uncertain', reason='interrupted' "
                     "WHERE finished_at IS NULL", (now,))
        if "dm" in open_kinds:
            _hold_after(conn, "dm", "uncertain", "interrupted")
        return settled


# ---------- the public reply (7.4) ----------

def claim_public(conn, comment_id, cfg, me, hidden=False):
    """Claim the one public reply of a `sent` comment, committed before the POST. Returns PublicClaim.

    kind 'claimed': POST `phrase`. 'skip': nothing to post (the comment is not `sent`, or it was settled here
    without a POST). Otherwise the name of a closed gate. `hidden`: Meta lists the comment as hidden and takes
    no reply under it, so none is tried.
    """
    _outside_tx(conn)
    with tx(conn):
        row = comment(conn, comment_id)
        if row is None or row["status"] != "sent":
            return PublicClaim("skip")
        posted = conn.execute("SELECT 1 FROM attempts WHERE comment_id=? AND kind='public' LIMIT 1", (comment_id,))
        if posted.fetchone():
            _settle(conn, comment_id, "sent", "done", "public_uncertain")   # never a second public POST
            return PublicClaim("skip")
        if hidden or not (cfg.public_reply and cfg.public_phrases):
            _settle(conn, comment_id, "sent", "done", "public_skipped")
            return PublicClaim("skip")
        now = clock.now()
        gate = _closed_gate(conn, cfg, now) if me and me.get("user_id") else "no_identity"
        if gate:
            return PublicClaim(gate)
        last = kv_get(conn, "last_phrase")
        phrase = random.choice([text for text in cfg.public_phrases if text != last] or cfg.public_phrases)
        started_at = clock.iso(now)
        monotonic = time.monotonic()
        attempt_id = conn.execute(
            "INSERT INTO attempts (comment_id, kind, detail, started_at) VALUES (?, 'public', ?, ?)",
            (comment_id, phrase, started_at)).lastrowid
        kv_set(conn, "last_phrase", phrase)
        return PublicClaim("claimed", attempt_id, phrase, monotonic, started_at)


def record_public(conn, attempt_id, result):
    """Journal the answer to a claimed public reply; the comment is `done` whatever it was. Returns the reason."""
    with tx(conn):
        answer = _answer(result, "id")
        outcome = answer["outcome"]
        comment_id = _close(conn, attempt_id, "public", answer)
        reason = "" if outcome == "ok" else "public_failed" if outcome in ("rejected", "global") else "public_uncertain"
        _settle(conn, comment_id, "sent", "done", reason)
        _hold_after(conn, "public", outcome, answer["reason"])
        return reason


# ---------- metrics, outbox, problems ----------

def add_metric(conn, followers, media_count):
    """The hourly row, and kv `metric_at` with it."""
    with tx(conn):
        now = clock.iso()
        conn.execute("INSERT OR REPLACE INTO metrics (ts, followers, media_count) VALUES (?,?,?)",
                     (now, followers, media_count))
        kv_set(conn, "metric_at", now)


def outbox_add(conn, key, text):
    return conn.execute("INSERT INTO outbox (key, text, created_at) VALUES (?,?,?)",
                        (key, _text(text), clock.iso())).lastrowid


def outbox_sent(conn, ids, at=None):
    """Telegram confirmed these rows."""
    with tx(conn):
        now = clock.iso() if at is None else at
        conn.executemany("UPDATE outbox SET sent_at=? WHERE id=? AND sent_at IS NULL",
                         [(now, row_id) for row_id in ids])


def problem_announce(conn, key, text):
    """A problem was queued for the alert channel just now; `since` keeps the time of its first announcement."""
    now = clock.iso()
    conn.execute("INSERT INTO problems (key, text, since, last_announced_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET text=excluded.text, last_announced_at=excluded.last_announced_at",
                 (key, _text(text), now, now))


def problem_clear(conn, key):
    return conn.execute("DELETE FROM problems WHERE key=?", (key,)).rowcount == 1


# ---------- written by web.py (14.1): inbound events and clicks; nothing else ----------

# 14.7: user agents of link-preview robots, lower case. Meta fetches a link inside a DM to draw its preview:
# that GET of a tracked link is not a person's click. PERSON_CLICK is the SQL test on a `clicks` row named `k`.
ROBOTS = ("facebookexternalhit", "facebookcatalog", "meta-externalagent", "meta-externalfetcher", "bot", "crawler",
          "spider", "preview")
PERSON_CLICK = "(k.agent IS NULL OR NOT ({}))".format(" OR ".join(f"lower(k.agent) LIKE '%{r}%'" for r in ROBOTS))

def add_inbound(conn, mid, sender, kind, payload=None, text=None):
    """Store one webhook event; False if this `mid` is already stored (Meta repeats deliveries)."""
    with tx(conn):
        return conn.execute(
            "INSERT INTO inbound (mid, received_at, sender, kind, payload, text) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(mid) DO NOTHING",
            (_text(mid), clock.iso(), _text(sender), kind, _text(payload), _text(text))).rowcount == 1


def link_url(conn, token):
    """The URL a tracked token stands for, or None."""
    row = conn.execute("SELECT url FROM links WHERE token=?", (token,)).fetchone()
    return row and row[0]


def add_click(conn, token, agent=None):
    with tx(conn):
        conn.execute("INSERT INTO clicks (token, at, agent) VALUES (?,?,?)",
                     (token, clock.iso(), _text(agent)[:200] if agent else None))


# ---------- the funnel (14): the person's variant, steps, taps, follow checks ----------

def _new_token():
    return "".join(random.SystemRandom().choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(10))


def _tokens(conn, cfg, comment_id, media_id, variant, urls, first_position):
    """Tracked tokens for these URLs (14.2 `click_base`), one per position, made once and reused."""
    out = []
    for offset, url in enumerate(urls):
        position = first_position + offset
        row = conn.execute("SELECT token FROM links WHERE comment_id=? AND position=?",
                           (comment_id, position)).fetchone()
        if row is None:
            token = _new_token()
            conn.execute("INSERT INTO links (token, comment_id, media_id, variant, position, url, created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (token, comment_id, media_id, variant, position, url, clock.iso()))
            out.append(token)
        else:
            out.append(row[0])
    return out


def _flow_claim(conn, cfg, reel, row, now):
    """14.3, inside the DM claim: the person's variant and the `flow` row; tracked token of a link-in-first-DM.
    (None, ()) for a reel without flow. A comment claimed again after `not_sent` keeps its variant."""
    if reel.flow is None:
        return None, ()
    existing = conn.execute("SELECT variant FROM flow WHERE comment_id=?", (row["comment_id"],)).fetchone()
    names = {v.name: v for v in reel.flow.variants}
    variant = names.get(existing[0]) if existing else None
    if variant is None:
        variant = reel.flow.variant(row["media_id"], row["user_id"])
        conn.execute("INSERT OR REPLACE INTO flow (comment_id, media_id, variant, opening, gate, state, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (row["comment_id"], row["media_id"], variant.name, int(variant.opening),
                                                int(variant.gate), "opened" if variant.opening else "direct", now))
    tokens = ()
    if not variant.opening and cfg.click_base:
        tokens = tuple(_tokens(conn, cfg, row["comment_id"], row["media_id"], variant.name, [reel.link], 0))
    return variant.name, tokens


def flow_row(conn, comment_id):
    return conn.execute("SELECT * FROM flow WHERE comment_id=?", (comment_id,)).fetchone()


def claim_step(conn, comment_id, step, cfg, me, urls=()):
    """14.4: claim one step POST, committed before it leaves. `urls`: the link buttons' URLs (step `link`), made
    tracked here when `click_base` is set; the claim returns the URLs to send."""
    _outside_tx(conn)
    with tx(conn):
        flow = flow_row(conn, comment_id)
        if flow is None or not flow["igsid"]:
            return StepClaim("skip")
        if not (me and me.get("user_id")):
            return StepClaim("no_identity")
        now = clock.now()
        gate = _closed_gate(conn, cfg, now)
        if gate:
            return StepClaim(gate)
        tried = conn.execute("SELECT 1 FROM attempts WHERE comment_id=? AND kind='step' AND detail=? "
                             "AND outcome IS NOT 'not_sent' LIMIT 1", (comment_id, step)).fetchone()
        if tried:
            return StepClaim("skip")   # one POST per step: an unclear answer is never sent again
        since = clock.iso(now - timedelta(seconds=T["rate_window"]))
        if conn.execute("SELECT COUNT(*) FROM attempts WHERE kind='step' AND started_at>?",
                        (since,)).fetchone()[0] >= T["step_cap"]:
            return StepClaim("cap")
        attempt_id = conn.execute("INSERT INTO attempts (comment_id, kind, detail, started_at) VALUES (?, 'step', ?, ?)",
                                  (comment_id, step, clock.iso(now))).lastrowid
        if urls and cfg.click_base:
            urls = tuple(cfg.click_base + token for token in
                         _tokens(conn, cfg, comment_id, flow["media_id"], flow["variant"], urls, 1))
        return StepClaim("claimed", attempt_id, tuple(urls))


def record_step(conn, attempt_id, result):
    """Journal the answer to a step and move the person on when it was delivered. Returns the outcome."""
    with tx(conn):
        answer = _answer(result, "message_id")
        row = conn.execute("SELECT detail FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        comment_id = _close(conn, attempt_id, "step", answer)
        if answer["outcome"] == "ok":
            now, step = clock.iso(), row[0]
            if step == "link":
                conn.execute("UPDATE flow SET state='linked', link_at=? WHERE comment_id=?", (now, comment_id))
            elif step.startswith("prompt"):
                conn.execute("UPDATE flow SET state='gate', prompts=? WHERE comment_id=?", (int(step[6:]), comment_id))
            elif step == "followup":
                conn.execute("UPDATE flow SET followup_at=? WHERE comment_id=?", (now, comment_id))
        _hold_after(conn, "step", answer["outcome"], answer["reason"])
        return answer["outcome"]


def flow_tap(conn, comment_id, igsid):
    """14.5: a tap on a button of this comment's flow. 'ok', 'unknown_comment' or 'other_sender'."""
    with tx(conn):
        flow = flow_row(conn, comment_id)
        if flow is None:
            return "unknown_comment"
        if flow["igsid"] and flow["igsid"] != igsid:
            return "other_sender"
        now = clock.iso()
        conn.execute("UPDATE flow SET igsid=?, pressed_at=COALESCE(pressed_at, ?), last_in_at=? WHERE comment_id=?",
                     (igsid, now, now, comment_id))
        return "ok"


def flow_message(conn, igsid):
    """14.5: the person wrote to the account: their 24 h window is open again."""
    with tx(conn):
        return conn.execute("UPDATE flow SET last_in_at=? WHERE igsid=?", (clock.iso(), igsid)).rowcount


def flow_stuck(conn, comment_id):
    with tx(conn):
        conn.execute("UPDATE flow SET state='stuck' WHERE comment_id=? AND state<>'linked'", (comment_id,))


def add_check(conn, comment_id, purpose, result):
    """14.5: one follow check: `result` yes|no|unknown. A recheck that says `no` marks the unfollow."""
    with tx(conn):
        now = clock.iso()
        conn.execute("INSERT INTO checks (comment_id, at, purpose, result) VALUES (?,?,?,?)",
                     (comment_id, now, purpose, result))
        conn.execute("UPDATE flow SET follow=? WHERE comment_id=?", (result, comment_id))
        if purpose.startswith("recheck") and result == "no":
            conn.execute("UPDATE flow SET unfollowed_at=COALESCE(unfollowed_at, ?) WHERE comment_id=?",
                         (now, comment_id))


def inbound_done(conn, inbound_id, result):
    with tx(conn):
        conn.execute("UPDATE inbound SET handled_at=?, result=? WHERE id=? AND handled_at IS NULL",
                     (clock.iso(), result, inbound_id))
