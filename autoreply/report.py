"""Report and problems (SPEC section 9). This module only READS the database.

problems() is the ONE definition of "something is wrong": the engine evaluates it every cycle and hands it to
the notifier, `bot.py report` prints it first. Every problem is one small function below that returns
(key, text) or None; the texts are for a person who reads them in Telegram.

build() is the whole report as plain JSON data, render() the same as text with the verdict on the first line.
"""
import fcntl
import json
import os
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from functools import cached_property

import clock
import config
import match
import notify
import store
from clock import T

FAILED_SWEEPS = 3             # 9: failed sweeps in a row that make a reel a problem
FAILED_REFRESHES = 2          # 8.2: failed token refreshes in a row that make the token a problem
NEAR_LISTED = 30
UNMATCHED_LISTED, UNMATCHED_WORDS = 20, 3
REFUSALS_OF, REFUSALS_SAME = 20, 5   # 9: of the newest 20 DMs that reached Meta, 5 refused for one reason
# ... other than these two, which say something about that one person and not about the message or the account
# (one commenter in ten cannot be written to on an ordinary day)
REFUSALS_OF_A_PERSON = ("already_replied", "user_unavailable")
DIGEST_UNITS = 4000           # the digest fits one Telegram message (4096 UTF-16 units) with its prefix
DIGEST_TEXT = 40              # characters of an unmatched text in the digest
_UNCERTAIN_UNACKED = ("FROM comments c WHERE c.status='uncertain' AND NOT EXISTS "
                      "(SELECT 1 FROM kv WHERE key='uncertain_ack:' || c.comment_id)")

_GATE = {   # 8.4: why a queue does not move
    "sending_off": "отправка выключена в настройках (sending: false)",
    "no_budget": "бюджет отправок 0 — нужна команда igbot arm N",
    "account_hold": "аккаунт на паузе",
    "send_hold": "отправка на паузе",
    "no_identity": "бот ещё не узнал свой аккаунт",
    "no_sweep": "не удаётся прочитать комментарии рилса",
    "reel_off": "рилс выключен в настройках (active: false)",
    "stalled": "все условия выполнены, но сообщения не уходят",
    "cap": "очередь идёт с разрешённой скоростью",
}
_ACCOUNT_HOLD = {
    "no_token": "нет токена Instagram — добавьте IG_ACCESS_TOKEN в .env",
    "no_identity": "бот ещё не узнал свой аккаунт",
    "auth": "Meta не принимает токен — нужен новый токен в .env",
    "blocked": "Meta отклоняет запросы аккаунта",
    "rate": "превышен лимит запросов к Meta",
}
_SEND_HOLD = {
    "uncertain": "неизвестно, дошло ли последнее сообщение",
    "send_blocked": "Meta отклонила отправку",
    "spam": "Meta считает сообщения спамом; после паузы нужна новая команда igbot arm N",
    "streak": "несколько отправок подряд не удались",
}
_REEL_STATE = {
    "gone": "Meta не находит это видео (удалено или неверный media_id)",
    "mismatch": "ссылка в настройках ведёт не на то видео, что media_id",
    "empty": "Meta отдаёт пустой список, хотя комментарии есть",
    "capped": "комментариев больше, чем бот читает за один проход (400 страниц)",
}
_UNACCOUNTED = {
    "sending": "отправка зависла",
    "open_attempts": "попытка без ответа",
    "public_owed": "публичный ответ не отправлен",
    "sent_without_ok_dm": "отмечено отправленным без подтверждения Meta",
    "media_not_in_config": "рилса нет в настройках",
}


def _span(seconds):
    """A duration in plain words: «меньше минуты», «45 мин», «3 ч 20 мин», «2 дн 5 ч»."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "меньше минуты"
    days, hours, minutes = minutes // 1440, minutes % 1440 // 60, minutes % 60
    if days:
        return f"{days} дн {hours} ч"
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def _when(stamp):
    """'2026-10-05T12:34:56.000Z' as «05.10 12:34 UTC»."""
    return f"{stamp[8:10]}.{stamp[5:7]} {stamp[11:16]} UTC" if stamp else "никогда"


def _why(reason):
    """A waiting reason with its explanation: «no_budget (бюджет отправок 0 …)»."""
    return f"{reason} ({_GATE[reason.split(':')[0]]})"


@contextmanager
def _one_moment(conn):
    """Every query of one call sees the same moment of the database, whatever the running bot writes meanwhile."""
    alone = not conn.in_transaction
    if alone:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if alone and conn.in_transaction:
            conn.execute("ROLLBACK")   # nothing was written


class _View:
    """The database at one moment, with what several problems and the report need worked out once."""

    def __init__(self, conn, cfg, now, config_file_error=None):
        self.conn, self.now = conn, now
        kv = dict(conn.execute("SELECT key, value FROM kv").fetchall())
        tokens = [kv.pop(key) for key in ("token", "token_env_seen") if kv.get(key)]
        self.has_token = bool(tokens)
        self.config_file_error = self._without(tokens, config_file_error or "")
        # 8.2: the token never reaches an alert or the report, whatever a stored error text quotes
        self.kv = {key: self._without(tokens, value) for key, value in kv.items()}
        # The report describes the service, not the file: the config the lock holder loaded is in kv
        # `config_running`. A `cfg` with another hash is a file edited after that start (problem restart_needed).
        self.cfg, self.config_differs = cfg, _ = config.running(kv.get("config_running"), kv.get("config_hash"), cfg)
        self.active = {reel.media_id: reel for reel in cfg.reels if reel.active}
        self.account_hold = json.loads(self.kv.get("account_hold") or "null")
        self.last_send_hold = json.loads(self.kv.get("send_hold") or "null")   # it stays in kv after it ended
        in_force = self.last_send_hold and self.last_send_hold["until"] > clock.iso(self.now)
        self.send_hold = self.last_send_hold if in_force else None
        self.budget = store.budget(conn)
        self.identity = bool(self.kv.get("me_user_id"))
        self.announced = {row[0] for row in conn.execute("SELECT key FROM problems")}
        last_ok = self.one("SELECT finished_at FROM attempts WHERE kind='dm' AND outcome='ok' ORDER BY id DESC LIMIT 1")
        self.quiet = last_ok is None or last_ok <= self.ago(T["waiting_after"])   # no ok DM for 30 min

        # The people waiting for a DM, in the order they will be served (8.4): one turn per person and reel,
        # none for a person who is already handled (7.2 step 2). People of a switched-off reel wait too: they
        # are "parked", and their windows run on (reason `reel_off`).
        configured = cfg.reel_by_media
        self.queue = [(media_id, clock.parse(ts)) for _, media_id, _, ts in store.queue(conn, configured)]
        # comments whose DM went out and whose public reply is still owed, per reel
        self.owed = {media_id: n for media_id, n in conn.execute(
            "SELECT media_id, COUNT(*) FROM comments WHERE status='sent' GROUP BY media_id") if media_id in configured}

    @staticmethod
    def _without(tokens, text):
        for token in tokens:
            text = text.replace(token, "<token>")
        return text

    def one(self, sql, *args):
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row else None

    def ago(self, seconds):
        return clock.iso(self.now - timedelta(seconds=seconds))

    def time(self, key):
        """A kv timestamp as a datetime; None if absent or unreadable."""
        try:
            return clock.parse(self.kv.get(key))
        except ValueError:
            return None

    def count(self, key):
        """A kv counter; 0 if absent or unreadable."""
        value = self.kv.get(key, "")
        return int(value) if value.isdigit() else 0

    def expires_in(self, ts):
        """Seconds until a comment written at `ts` leaves the reply window (7.2 step 1)."""
        return (ts - self.now).total_seconds() + T["reply_window"] - T["window_margin"]

    def reel(self, media_id):
        """What collect last recorded about a reel (8.3)."""
        return {"state": self.kv.get(f"reel_state:{media_id}"), "fails": self.count(f"reel_fails:{media_id}"),
                "unreadable": self.count(f"unreadable:{media_id}"),
                "head_ok_at": self.kv.get(f"head_ok_at:{media_id}"),
                "full_sweep_at": self.kv.get(f"full_sweep_at:{media_id}")}

    def read_at(self, media_id):
        """When the reel was last read without a failure; None while its last collect failed or none succeeded.

        "Swept in this process run" is not in the database: a running bot sweeps every reel in its first cycle.
        """
        facts = self.reel(media_id)
        if facts["state"] != "ok" or facts["fails"] or not facts["full_sweep_at"]:
            return None
        return max(facts["full_sweep_at"], facts["head_ok_at"] or "")

    @cached_property
    def lock_held(self):
        """Whether a process holds core.lock next to the database: True, False, or None if that cannot be told.

        A shared lock is tried and dropped at once. It is only ever granted while no bot runs; a bot that
        starts in that same instant would find the lock taken and exit 3.
        """
        path = self.conn.execute("PRAGMA database_list").fetchone()[2]
        if not path:
            return None
        try:
            with open(os.path.join(os.path.dirname(path), "core.lock")) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                return False
        except FileNotFoundError:
            return False   # the bot creates the file when it first runs
        except OSError:
            return None

    @cached_property
    def slot_busy(self):
        """The slot or the hourly cap is taken by a DM that is on its way or that reached Meta: waiting for it
        is the normal `cap`. An attempt that could not connect (`not_sent`) takes the slot for the claim
        (7.2 step 4) but is no reason to call a queue healthy: a DM tried and lost in every cycle looked like
        a busy slot for ever."""
        reached = "kind='dm' AND outcome IS NOT 'not_sent'"
        last = self.conn.execute(
            f"SELECT started_at, finished_at FROM attempts WHERE {reached} ORDER BY id DESC LIMIT 1").fetchone()
        slot = timedelta(seconds=T["rate_window"] / self.cfg.max_sends_per_hour)
        if last and self.now < clock.parse(max(filter(None, last))) + slot:
            return True
        recent = self.one(f"SELECT COUNT(*) FROM attempts WHERE {reached} "
                          "AND MAX(started_at, COALESCE(finished_at, started_at)) > ?",
                          self.ago(T["rate_window"] + T["cap_margin"]))
        return recent >= self.cfg.max_sends_per_hour

    @cached_property
    def unconnected(self):
        """How many DM attempts in a row, up to now, could not connect."""
        return self.one("SELECT COUNT(*) FROM attempts WHERE kind='dm' AND outcome IS 'not_sent' AND id > COALESCE("
                        "(SELECT MAX(id) FROM attempts WHERE kind='dm' AND outcome IS NOT 'not_sent'), 0)")

    def quiet_for(self, media_id):
        """No ok DM to this reel's people for 30 min (what other reels send does not serve them)."""
        last_ok = self.one("SELECT a.finished_at FROM attempts a JOIN comments c ON c.comment_id=a.comment_id "
                           "WHERE a.kind='dm' AND a.outcome='ok' AND c.media_id=? ORDER BY a.id DESC LIMIT 1", media_id)
        return last_ok is None or last_ok <= self.ago(T["waiting_after"])

    def waiting_reason(self, media_id):
        """Why the people of this reel are still waiting (8.4), '' if nobody is. `cap` is the normal one."""
        dm_owed = any(media == media_id for media, _ in self.queue)
        if not (dm_owed or self.owed.get(media_id)):
            return ""
        if media_id not in self.active:
            return "reel_off"
        if not self.cfg.sending:
            return "sending_off"
        if dm_owed and self.budget == 0:   # a public reply needs no budget (G6)
            return "no_budget"
        if self.account_hold:
            return f"account_hold:{self.account_hold.get('reason')}"
        if self.send_hold:
            return f"send_hold:{self.send_hold.get('reason')}"
        if not self.identity:
            return "no_identity"
        if not self.read_at(media_id):
            return "no_sweep"
        if self.quiet and not (dm_owed and self.slot_busy):
            return "stalled"
        return "cap"

    @cached_property
    def uncertain_unresolved(self):
        return self.one(f"SELECT COUNT(*) {_UNCERTAIN_UNACKED}")

    @cached_property
    def unaccounted(self):
        """The five terms of `unaccounted` (9); every one must be 0."""
        open_since = self.ago(T["open_claim_after"])
        configured = list(self.cfg.reel_by_media)
        return {
            "sending": self.one("SELECT COUNT(*) FROM comments WHERE status='sending' AND updated_at <= ?", open_since),
            "open_attempts": self.one(
                "SELECT COUNT(*) FROM attempts WHERE finished_at IS NULL AND started_at <= ?", open_since),
            "public_owed": self._public_overdue(),
            "sent_without_ok_dm": self.one(
                "SELECT COUNT(*) FROM comments c WHERE status IN ('sent','done') AND NOT EXISTS "
                "(SELECT 1 FROM attempts a WHERE a.comment_id=c.comment_id AND a.kind='dm' AND a.outcome='ok')"),
            # rows of a reel that is configured but inactive are parked, not unaccounted
            "media_not_in_config": self.one(
                f"SELECT COUNT(*) FROM comments WHERE status IN ('pending','sent') "
                f"AND media_id NOT IN ({','.join('?' * len(configured))})", *configured),
        }

    def _public_overdue(self):
        """`sent` comments whose public reply nothing has stood in the way of for 10 min.

        Counted only where the database proves the way was free: sending on, identity known, no account hold,
        no send hold within those 10 min, and the reel read without a failure AFTER the comment became `sent`
        (collect only runs without an account hold, so that read also proves there was none).
        """
        since, hold = self.ago(T["public_owed_after"]), self.last_send_hold
        if not self.cfg.sending or not self.identity or self.account_hold or (hold and hold["until"] > since):
            return 0
        return sum(self.one("SELECT COUNT(*) FROM comments WHERE status='sent' AND media_id=? AND updated_at <= ? "
                            "AND updated_at < ?", media_id, since, self.read_at(media_id) or "")
                   for media_id in self.owed if media_id in self.active)


# ---------- the problems: each returns (key, text) or None ----------

def _hold_account(v):
    hold = v.account_hold
    if not hold:
        return None
    reason, detail = hold.get("reason"), (hold.get("detail") or "")[:150]
    return "hold:account", (
        f"Аккаунт на паузе с {_when(hold.get('since'))}: {_ACCOUNT_HOLD.get(reason, 'причина неизвестна')} "
        f"({reason}{', ' + detail if detail else ''}). Бот не читает комментарии и ничего не отправляет. "
        f"Следующая проверка: {_when(hold.get('next_probe_at'))}.")


def _hold_send(v):
    hold = v.send_hold
    if not hold:
        return None
    reason, left = hold.get("reason"), (clock.parse(hold["until"]) - v.now).total_seconds()
    return "hold:send", (f"Отправка на паузе до {_when(hold['until'])} (ещё {_span(left)}): "
                         f"{_SEND_HOLD.get(reason, 'причина неизвестна')} ({reason}). "
                         f"Комментарии читаются, очередь копится.")


def _waiting(v):
    """People wait, nobody got a DM for 30 min, and the reason is not the hourly cap."""
    reasons = {media_id: v.waiting_reason(media_id) for media_id in v.cfg.reel_by_media}
    stuck = {media_id for media_id, reason in reasons.items() if reason not in ("", "cap")
             and (v.quiet or (reason == "reel_off" and v.quiet_for(media_id)))}
    if not stuck:
        return None
    waits = [ts for media_id, ts in v.queue if media_id in stuck]
    owed = sum(v.owed.get(media_id, 0) for media_id in stuck)
    text = f"{len(waits)} чел. ждут ссылку" if waits else "Ссылки отправлены"
    if owed:
        text += f", публичный ответ не отправлен под {owed} комментариями"
    text += f". Причина: {'; '.join(_why(reason) for reason in sorted({reasons[media_id] for media_id in stuck}))}."
    if v.unconnected and "stalled" in reasons.values():
        text += f" Подряд не удалось соединиться с Meta при {v.unconnected} отправках."
    if waits:
        left = v.expires_in(min(waits))
        text += (f" Самый старый комментарий ждёт {_span((v.now - min(waits)).total_seconds())}; "
                 + (f"первый истечёт через {_span(left)}." if left > 0 else "срок первого уже вышел."))
    return "waiting", text


def _expiring(v):
    """People whose reply window ends within 24 h, or before their turn comes at the configured rate."""
    slot = (T["rate_window"] + T["cap_margin"]) / v.cfg.max_sends_per_hour   # the pace of a long queue (7.2)
    left = [v.expires_in(ts) for _, ts in v.queue]
    soon = [seconds <= T["expiring_within"] for seconds in left]
    late = [seconds <= place * slot for place, seconds in enumerate(left)]   # closed when the turn comes (7.2)
    at_risk = sum(a or b for a, b in zip(soon, late))
    if not at_risk:
        return None
    why = [f"у {sum(soon)} до конца 7-дневного окна меньше 24 ч"] if any(soon) else []
    if any(late):
        why.append(f"до {sum(late)} очередь не дойдёт при {v.cfg.max_sends_per_hour} в час")
    return "expiring", (f"{at_risk} чел. могут остаться без ссылки: {', '.join(why)}. Первый истекает через "
                        f"{_span(min(left))}. Откройте отправку (igbot arm N) или поднимите max_sends_per_hour.")


def _unaccounted(v):
    parts = {name: n for name, n in v.unaccounted.items() if n}
    if not parts:
        return None
    return "unaccounted", (
        f"{sum(parts.values())} записей не сходятся с журналом отправок: "
        + ", ".join(f"{_UNACCOUNTED[name]} — {n}" for name, n in parts.items())
        + ". Так быть не должно: нужен разбор (igbot report).")


def _reel(v, reel):
    """One reel is not being read: its state, 3 failed sweeps in a row, or no full sweep for 2 h."""
    facts, key, name = v.reel(reel.media_id), f"reel:{reel.media_id}", f"Рилс «{reel.name}»"
    if facts["state"] not in (None, "ok"):
        return key, f"{name}: {_REEL_STATE.get(facts['state'], facts['state'])}. Его комментарии не обрабатываются."
    if facts["fails"] >= FAILED_SWEEPS:
        return key, (f"{name}: комментарии не удалось прочитать {facts['fails']} раз подряд. "
                     f"Новые люди не попадают в очередь.")
    swept = v.time(f"full_sweep_at:{reel.media_id}")
    # An account hold stops every read and is a problem of its own. In the cycle that lifts it collect has not
    # run yet, and that cycle still finds the hold among the announced problems: no alarm before the reel had a chance.
    held = v.account_hold or "hold:account" in v.announced
    if swept and not held and (v.now - swept).total_seconds() >= T["reel_stale_after"]:
        return key, (f"{name}: комментарии не перечитывались полностью уже {_span((v.now - swept).total_seconds())} "
                     f"(последний раз {_when(clock.iso(swept))}).")
    if facts["unreadable"]:
        return key, (f"{name}: Meta отдала комментарии в непонятном виде (без id или с нечитаемым текстом), "
                     f"таких {facts['unreadable']}. Бот не может их прочитать; остальные обрабатываются как обычно.")
    return None


def _token(v):
    """Fewer than 14 days of the token remain, or two refreshes in a row failed (8.2).

    The engine counts the failed refreshes in kv `token_refresh_fails`.
    """
    refreshed, expires = v.time("token_refreshed_at"), v.time("token_expires_at")
    ends = expires or (refreshed and refreshed + timedelta(seconds=T["token_lifetime"]))
    if ends and (ends - v.now).total_seconds() < T["token_warn_left"]:
        left = (ends - v.now).total_seconds()
        return "token", (f"Токен Instagram {'истекает через ' + _span(left) if left > 0 else 'истёк'} "
                         f"({_when(clock.iso(ends))}) и сам не обновился. Нужен новый токен в .env, "
                         f"иначе бот остановится.")
    fails = v.count("token_refresh_fails")
    if fails >= FAILED_REFRESHES:
        return "token", (f"Токен Instagram не удаётся обновить: {fails} раз подряд, последний "
                         f"{_when(v.kv.get('token_refresh_failed_at'))}. Пока он работает; если так пойдёт дальше, "
                         f"понадобится новый токен в .env.")
    return None


def _cycle_error(v):
    error = v.kv.get("last_cycle_error")
    if not error:
        return None
    return "cycle_error", (f"Сбой в работе бота: {error[:200]}. Бот продолжает работать; "
                           f"если сообщение повторяется, нужен разбор по журналу.")


def _clock(v):
    """The engine's clock guard (8.1) tripped in its last cycle; it leaves what it saw in kv `clock_problem`."""
    seen = v.kv.get("clock_problem")
    if not seen:
        return None
    return "clock", (f"Часы сервера скакнули ({seen}). В этом цикле бот ничего не отправлял и не списывал "
                     f"просроченных; если повторяется, проверьте время на сервере.")


def _no_active_reel(v):
    """The loaded config reads no reel at all: every reel is switched off, or there is none."""
    if v.active:
        return None
    off = ", ".join(f"«{reel.name}»" for reel in v.cfg.reels)
    return "no_active_reel", ("Бот не читает ни одного рилса: " + (f"все выключены (active: false): {off}" if off else
                              "в настройках нет рилсов") + ". Комментарии не читаются, ссылки не отправляются, "
                              "новые просьбы даже не попадают в базу. Включите рилс в config.json и перезапустите сервис.")


def _unservable(v):
    """Requests that cannot be served as Meta lists them: no author, or a comment the bot cannot read."""
    why = {"no_author": "Meta не показывает автора (проверьте права токена на комментарии)",
           "malformed": "комментарий пришёл в непонятном виде (нет времени или формат изменился)"}
    found, newest = Counter(), None
    configured = v.cfg.reel_by_media
    matchers = {media_id: match.Matcher(reel) for media_id, reel in configured.items()}
    for comment_id, media_id, reason, text in v.conn.execute(
            "SELECT comment_id, media_id, reason, text FROM comments WHERE status='seen' AND reason IN "
            "('no_author','malformed') AND ts > ? ORDER BY ts DESC, comment_id",
            (v.ago(T["reply_window"] - T["window_margin"]),)):
        if media_id in matchers and matchers[media_id].check(text)[0] == "match":
            found[configured[media_id].name, reason] += 1
            newest = newest or comment_id
    if not found:
        return None
    parts = "; ".join(f"«{name}»: {n} — {why[reason]} ({reason})" for (name, reason), n in sorted(found.items()))
    return "unservable", (f"{sum(found.values())} просьб с ключевым словом нельзя выполнить: {parts}. "
                          f"Этим людям ничего не отправлено. Последний такой комментарий: {newest}.")


def _uncertain_manual(v):
    """Deliveries Meta never confirmed and nobody has looked at: said with the names, until `ack-uncertain`."""
    total = v.uncertain_unresolved
    if not total:
        return None
    reels = {reel.media_id: reel.name for reel in v.cfg.reels}
    named = "; ".join(f"@{row['username'] or '?'} («{reels.get(row['media_id'], row['media_id'])}») — "
                      f"igbot ack-uncertain {row['comment_id']}" for row in _uncertain(v)[:3])
    more = f" …и ещё {total - 3}: igbot report" if total > 3 else ""
    return "uncertain_manual", (f"Неизвестно, дошло ли сообщение: {total} чел. {named}{more}. Сначала проверьте "
                                "доставку: откройте переписку с человеком в Instagram (в том числе «Запросы»). "
                                "Если ссылки там нет — отправьте её сами. Потом подтвердите командой. "
                                "Бот этим людям повторно не пишет.")


def _refusals(v):
    """Of the newest DMs that reached Meta within 24 h, several were refused for one and the same reason."""
    rows = v.conn.execute(
        "SELECT outcome, reason, code, subcode, message FROM attempts WHERE kind='dm' AND finished_at IS NOT NULL "
        "AND outcome IS NOT 'not_sent' AND started_at > ? ORDER BY id DESC LIMIT ?",
        (v.ago(T["refusals_within"]), REFUSALS_OF)).fetchall()
    refused = [row for row in rows if row[0] == "rejected" and row[1] not in REFUSALS_OF_A_PERSON]
    reasons = Counter(row[1] for row in refused)
    if not reasons or reasons.most_common(1)[0][1] < REFUSALS_SAME:
        return None
    reason, n = reasons.most_common(1)[0]
    _, _, code, subcode, message = next(row for row in refused if row[1] == reason)
    return "refusals", (f"Meta отклонила {n} из последних {len(rows)} отправок по одной причине: {reason} "
                        f"(код {code}/{subcode}: {(message or '')[:120]}). Эти люди ссылку не получили, и повторно им "
                        f"не отправляется; если так пойдёт дальше, дело в настройках или в аккаунте, а не в людях.")


def _env(v):
    """The engine could not read .env in its last cycle; it leaves what it saw in kv `env_problem`."""
    seen = v.kv.get("env_problem")
    if not seen:
        return None
    return "env", (f"Файл .env есть, но не читается ({seen}). Бот работает с сохранённым токеном, новый токен "
                   f"из файла не подхватится. Проверьте владельца и права файла (igbot, 600).")


def _config_file_invalid(v):
    if not v.config_file_error:
        return None
    return "config_file_invalid", (f"Файл настроек не читается или невалиден: {v.config_file_error[:300]}. "
                                   "Сервис пока работает по прежним настройкам, но после перезапуска или "
                                   "перезагрузки сервера НЕ запустится. Исправьте config.json и проверьте его "
                                   "командой igbot check.")


def _restart_needed(v):
    """The config given is not the one the service runs with: its hash is not kv `config_hash`. That is the file
    `bot.py report` read after an edit; the engine evaluates the problems with the config it runs with."""
    if not v.config_differs:
        return None
    return "restart_needed", (f"config.json изменён после запуска сервиса ({_when(v.kv.get('config_loaded_at'))}): "
                              f"сервис работает со старыми настройками, новые рилсы и слова из файла пока не "
                              f"работают. Чтобы новые настройки вступили в силу, перезапустите сервис; после "
                              f"этого отправка закрыта до команды igbot arm N.")


def _no_alert_channel(v):
    """Telegram is not configured, or its last delivery failed: the same test `arm` makes (notify.deaf)."""
    tg = notify.health(v.conn)
    if tg["configured"] is False:
        return "no_alert_channel", ("Telegram не настроен: оповещения пишутся только в журнал. "
                                    "Добавьте TG_BOT_TOKEN и TG_CHAT_ID в .env.")
    if tg["failing"]:
        return "no_alert_channel", (f"Telegram не принимает сообщения (последняя попытка {_when(tg['failed_at'])}), "
                                    f"{tg['queued']} оповещений ждут отправки. Проверьте токен бота и chat id.")
    return None


def _not_running(v):
    """Report only: nobody holds core.lock, or no cycle finished well for 10 min."""
    last = v.time("last_cycle_ok_at")
    if v.lock_held is False:
        why = "никто не держит core.lock"
    elif last is None:
        why = "ни один цикл ещё не завершился успешно"
    elif (v.now - last).total_seconds() > T["not_running_after"]:
        why = f"последний успешный цикл был {_span((v.now - last).total_seconds())} назад"
    else:
        return None
    return "not_running", f"Сервис не работает: {why}. Комментарии не читаются, ссылки не отправляются."


def _has_funnel(conn):
    """A version-2 database (before the service upgraded it, 14.6) has no funnel tables yet."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound'").fetchone() is not None


def _inbound_waiting(v):
    """14.5: a tap on a button waits unanswered."""
    if not _has_funnel(v.conn):
        return None
    row = v.conn.execute("SELECT COUNT(*), MIN(received_at) FROM inbound WHERE handled_at IS NULL").fetchone()
    if not row[0] or (v.now - clock.parse(row[1])).total_seconds() < T["inbound_stale"]:
        return None
    return "inbound_waiting", (f"{row[0]} нажатий кнопок ждут ответа, самое старое {_when(row[1])}. "
                               "Люди нажали «Получить»/«Готово» и ждут ссылку: проверьте, включена ли отправка "
                               "и нет ли паузы.")


def _funnel(v):
    """14.7: per reel and variant, from the first DM to the click."""
    if not _has_funnel(v.conn):
        return []
    rows = v.conn.execute("""
        SELECT f.media_id, f.variant,
          SUM(c.status IN ('sent','done')) AS first_dm,
          SUM(f.pressed_at IS NOT NULL) AS pressed,
          SUM(EXISTS (SELECT 1 FROM checks k WHERE k.comment_id=f.comment_id AND k.purpose='gate' AND k.result='yes'))
            AS checked_yes,
          SUM(EXISTS (SELECT 1 FROM checks k WHERE k.comment_id=f.comment_id AND k.purpose='gate' AND k.result='no'))
            AS checked_no,
          SUM(f.link_at IS NOT NULL) AS linked,
          SUM(f.followup_at IS NOT NULL) AS followup,
          SUM(f.state='stuck') AS stuck,
          SUM(f.unfollowed_at IS NOT NULL) AS unfollowed,
          SUM(EXISTS (SELECT 1 FROM links l JOIN clicks k ON k.token=l.token WHERE l.comment_id=f.comment_id
                      AND """ + store.PERSON_CLICK + """)) AS clicked_people
        FROM flow f JOIN comments c ON c.comment_id=f.comment_id
        GROUP BY f.media_id, f.variant ORDER BY f.media_id, f.variant""").fetchall()
    clicks = {(r[0], r[1]): r[2] for r in v.conn.execute(
        f"SELECT l.media_id, l.variant, COUNT(*) FROM clicks k JOIN links l ON l.token=k.token "
        f"WHERE {store.PERSON_CLICK} GROUP BY l.media_id, l.variant")}
    out = []
    for row in rows:
        reel = v.cfg.reel_by_media.get(row["media_id"])
        out.append({"media_id": row["media_id"], "reel": reel.name if reel else row["media_id"],
                    "variant": row["variant"], **{key: row[key] or 0 for key in row.keys()[2:]},
                    "clicks": clicks.get((row["media_id"], row["variant"]), 0)})
    return out


def _problems(v):
    found = [check(v) for check in (_hold_account, _hold_send, _waiting, _expiring, _unaccounted, _unservable,
                                    _uncertain_manual, _refusals, _no_active_reel, _inbound_waiting)]
    found += [_reel(v, reel) for reel in v.active.values()]
    found += [check(v) for check in (_token, _cycle_error, _clock, _env, _config_file_invalid,
                                    _restart_needed, _no_alert_channel)]
    return [problem for problem in found if problem]


def problems(conn, cfg, now, *, config_file_error=None):
    """Everything that is wrong at `now`, as [(key, text)]. Pure over the database and the config."""
    with _one_moment(conn):
        return _problems(_View(conn, cfg, now, config_file_error))


# ---------- the report ----------

def _verdict(found, people):
    keys = [key for key, _ in found]
    if not keys:
        return f"OK: в очереди {people} чел., отправка идёт" if people else "OK: никто не ждёт"
    if set(keys) <= {"waiting", "expiring"}:
        return "WAITING: " + found[0][1]
    return "PROBLEM: " + ", ".join(keys)


def _gates(v, running):
    kv, refreshed, expires = v.kv, v.time("token_refreshed_at"), v.time("token_expires_at")
    return {
        "sending": v.cfg.sending,
        "budget": v.budget, "armed_at": kv.get("armed_at"), "armed_n": kv.get("armed_n"),
        "account_hold": v.account_hold, "send_hold": v.send_hold,
        "public_reply": v.cfg.public_reply,
        "identity": {"user_id": kv.get("me_user_id"), "id": kv.get("me_id"), "username": kv.get("me_username"),
                     "checked_at": kv.get("me_checked_at")},
        "token": {"present": v.has_token,
                  "age_sec": refreshed and int((v.now - refreshed).total_seconds()),
                  "expires_in_sec": expires and int((expires - v.now).total_seconds()),
                  "refresh_fails": v.count("token_refresh_fails"),
                  "refresh_failed_at": kv.get("token_refresh_failed_at")},
        "telegram": notify.health(v.conn),
        "service": {"running": running, "lock_held": v.lock_held, "last_cycle_ok_at": kv.get("last_cycle_ok_at"),
                    "last_cycle_error": kv.get("last_cycle_error")},
    }


def _reels(v):
    """One entry per configured reel, then one per media id that has rows but is not in the config."""
    counts = {}
    for media_id, status, reason, n in v.conn.execute(
            # an expired row stored before reason `too_old` existed is shown as what it is (store.too_old_sql)
            f"SELECT media_id, status, CASE WHEN status='dup_user' THEN '' WHEN {store.too_old_sql()} THEN 'too_old' "
            "ELSE reason END AS why, COUNT(*) FROM comments GROUP BY media_id, status, why ORDER BY media_id, status, why"):
        counts.setdefault(media_id, []).append([status, reason, n])
    configured = v.cfg.reel_by_media
    entries = []
    for media_id in [*configured, *(media_id for media_id in counts if media_id not in configured)]:
        reel = configured.get(media_id)
        waits = [ts for media, ts in v.queue if media == media_id]
        open_rows = sum(n for status, _, n in counts.get(media_id, []) if status in ("pending", "sent"))
        entries.append({
            "media_id": media_id, "name": reel and reel.name, "configured": bool(reel),
            "active": media_id in v.active, **v.reel(media_id),
            "counts": counts.get(media_id, []),
            "people_waiting": len(waits), "public_owed": v.owed.get(media_id, 0),
            "parked": 0 if media_id in v.active or not reel else open_rows,
            "waiting_reason": v.waiting_reason(media_id),
            "oldest_pending_age_sec": int((v.now - min(waits)).total_seconds()) if waits else None,
            "first_expires_in_sec": int(v.expires_in(min(waits))) if waits else None,
        })
    return entries


def _dms_per_hour(v):
    return [{"hour": hour, "posts": posts, "ok": ok} for hour, posts, ok in v.conn.execute(
        "SELECT substr(started_at, 1, 13) AS hour, COUNT(*), COALESCE(SUM(outcome='ok'), 0) FROM attempts "
        "WHERE kind='dm' AND started_at > ? GROUP BY hour ORDER BY hour", (v.ago(T["report_dm_history"]),))]


def _attempts(v):
    return [{"kind": kind, "outcome": outcome, "reason": reason, "n": n} for kind, outcome, reason, n in v.conn.execute(
        "SELECT kind, outcome, reason, COUNT(*) FROM attempts GROUP BY kind, outcome, reason "
        "ORDER BY kind, COUNT(*) DESC, outcome, reason")]


def _near(v):
    """Comments with the keyword that were not taken for a request (5): the newest, and how many there are."""
    near = "FROM comments WHERE status='seen' AND reason LIKE 'near:%'"
    rows = v.conn.execute(f"SELECT comment_id, media_id, username, text, reason {near} ORDER BY ts DESC, comment_id "
                          f"LIMIT ?", (NEAR_LISTED,)).fetchall()
    return {"total": v.one(f"SELECT COUNT(*) {near}"),
            "newest": [{"comment_id": row[0], "media_id": row[1], "username": row[2], "text": row[3],
                        "reason": row[4][len("near:"):]} for row in rows]}


def _uncertain(v):
    """The newest unresolved manual cases; acknowledgement never changes the delivery status."""
    return [{"comment_id": row[0], "media_id": row[1], "username": row[2], "reason": row[3]} for row in v.conn.execute(
        f"SELECT comment_id, media_id, username, reason {_UNCERTAIN_UNACKED} "
        "ORDER BY ts DESC, comment_id LIMIT ?", (NEAR_LISTED,))]


def _unmatched(v):
    """The most frequent short comments without the keyword: where a human finds a typo for `also_match` (5)."""
    texts = Counter()
    for (text,) in v.conn.execute("SELECT text FROM comments WHERE status='seen' AND reason=''"):
        words = match.normalize(text)
        if 1 <= len(words) <= UNMATCHED_WORDS:
            texts[" ".join(words)] += 1
    return [[text, n] for text, n in sorted(texts.items(), key=lambda item: (-item[1], item[0]))[:UNMATCHED_LISTED]]


def _followers(v):
    def at(seconds_ago):
        return v.one("SELECT followers FROM metrics WHERE ts <= ? ORDER BY ts DESC LIMIT 1", v.ago(seconds_ago))
    day, week = T["followers_ago"]
    return {"now": at(0), "day_ago": at(day), "week_ago": at(week)}


def build(conn, cfg, now, *, config_file_error=None):
    """The whole report as JSON data (`bot.py report --json` prints it as it is). `unaccounted` must be 0."""
    with _one_moment(conn):
        v = _View(conn, cfg, now, config_file_error)
        down = _not_running(v)
        found = ([down] if down else []) + _problems(v)   # first: it explains most of the rest
        return {
            "at": clock.iso(v.now),
            "verdict": _verdict(found, len(v.queue)),
            "problems": [list(problem) for problem in found],
            "unaccounted": sum(v.unaccounted.values()),
            "unaccounted_parts": v.unaccounted,
            "gates": _gates(v, running=down is None),
            "reels": _reels(v),
            "dms_per_hour": _dms_per_hour(v),
            "attempts": _attempts(v),
            "uncertain": _uncertain(v),
            "uncertain_unresolved": v.uncertain_unresolved,
            "near": _near(v),
            "unmatched": _unmatched(v),
            "followers": _followers(v),
            "funnel": _funnel(v),
        }


def _render_gates(g):
    budget, hold, pause, me, token, tg, service = (g["budget"], g["account_hold"], g["send_hold"], g["identity"],
                                                   g["token"], g["telegram"], g["service"])
    if tg["configured"] is False:
        telegram = "НЕ НАСТРОЕН"
    elif tg["failing"]:
        telegram = f"НЕ ПРИНИМАЕТ сообщения (последняя попытка {_when(tg['failed_at'])})"
    elif tg["ok_at"]:
        telegram = f"работает (последняя доставка {_when(tg['ok_at'])})"
    else:
        telegram = "ещё ничего не доставлял"
    age, expires = token["age_sec"], token["expires_in_sec"]
    return [
        "Состояние:",
        f"  отправка: {'включена' if g['sending'] else 'ВЫКЛЮЧЕНА (sending: false)'}",
        f"  бюджет: {'без лимита' if budget == 'unlimited' else budget}"
        + (f" (arm {g['armed_n']} от {_when(g['armed_at'])})" if g["armed_at"] else ""),
        f"  пауза аккаунта: {hold['reason'] + ' с ' + _when(hold.get('since')) if hold else 'нет'}",
        f"  пауза отправки: {pause['reason'] + ' до ' + _when(pause['until']) if pause else 'нет'}",
        f"  публичные ответы: {'включены' if g['public_reply'] else 'выключены'}",
        f"  аккаунт: @{me['username']} (id {me['user_id']}), проверен {_when(me['checked_at'])}"
        if me["user_id"] else "  аккаунт: НЕИЗВЕСТЕН",
        "  токен: " + ("НЕТ" if not token["present"] else ", ".join([
            f"возраст {_span(age)}" if age is not None else "возраст неизвестен",
            f"истекает через {_span(expires)}" if expires is not None else "срок неизвестен"])),
        f"  Telegram: {telegram}, в очереди {tg['queued']}",
        f"  сервис: {'работает' if service['running'] else 'НЕ РАБОТАЕТ'}, "
        f"последний успешный цикл {_when(service['last_cycle_ok_at'])}",
    ]


def _render_reel(reel):
    title = f"«{reel['name']}»" if reel["configured"] else "НЕТ В НАСТРОЙКАХ"
    mode = "активен" if reel["active"] else "выключен" if reel["configured"] else "не обрабатывается"
    lines = [f"  {title} ({reel['media_id']}): {mode}, состояние {reel['state'] or 'ещё не читался'}, "
             f"чтение {_when(reel['head_ok_at'])}, полное {_when(reel['full_sweep_at'])}"]
    if reel["people_waiting"]:
        lines.append(f"    ждут ссылку: {reel['people_waiting']} чел., "
                     f"самый старый {_span(reel['oldest_pending_age_sec'])}, "
                     f"до конца окна {_span(reel['first_expires_in_sec'])}")
    if reel["public_owed"]:
        lines.append(f"    публичный ответ не отправлен: {reel['public_owed']}")
    if reel["waiting_reason"]:
        lines.append(f"    причина ожидания: {_why(reel['waiting_reason'])}")
    if reel["parked"]:
        lines.append(f"    отложено (рилс выключен): {reel['parked']}")
    counts = [f"{status}{'/' + reason if reason else ''} {n}" for status, reason, n in reel["counts"]]
    lines.append("    " + (" · ".join(counts) or "комментариев нет"))
    expired = {reason: n for status, reason, n in reel["counts"] if status == "expired"}
    if expired.get("too_old"):
        lines.append(f"    потеряно людей (не успели ответить за 7 дней): {sum(expired.values()) - expired['too_old']}; "
                     f"expired/too_old {expired['too_old']} — комментарии, которым уже было больше 7 дней, "
                     "когда бот их впервые увидел: это не потери")
    return lines


def _followers_line(d):
    followers = [("сейчас", "now"), ("сутки назад", "day_ago"), ("неделю назад", "week_ago")]
    return "Подписчики: " + ", ".join(
        f"{label} {'нет данных' if d['followers'][key] is None else d['followers'][key]}" for label, key in followers)


def digest(d, limit=DIGEST_UNITS):
    """One Telegram message. Reserve the verdict, problem summary and all 20 bounded typo texts.

    Small reports retain their usual layout. On overflow, count lines go first; arbitrarily many reels or
    problems become a truthful bounded summary with a full-report footer. Only remaining space holds reels.
    """
    limit = min(limit, DIGEST_UNITS)
    tail = [f"За 24 ч: попыток {sum(row['posts'] for row in d['dms_per_hour'])}, "
            f"доставлено {sum(row['ok'] for row in d['dms_per_hour'])}",
            "Частые короткие комментарии без ключевого слова (опечатки — в also_match):" if d["unmatched"]
            else "Коротких комментариев без ключевого слова нет."]
    tail += [f"  {n} × {text[:DIGEST_TEXT]}" for text, n in d["unmatched"][:UNMATCHED_LISTED]]
    tail += [f"С ключевым словом, но не просьба (near): {d['near']['total']}", _followers_line(d)]
    tail += [notify._cut(f"Воронка {row['reel']}/{row['variant']}: {row['first_dm']} → нажали {row['pressed']} → "
                         f"ссылка {row['linked']} → перешли {row['clicked_people']}", 120)
             for row in d.get("funnel", [])[:4]]

    def reel_lines(reel, bounded=False):
        title = f"«{reel['name']}»" if reel["configured"] else f"{reel['media_id']} (нет в настройках)"
        why = f", причина: {reel['waiting_reason']}" if reel["waiting_reason"] else ""
        if bounded:
            title, why = notify._cut(title, 120), notify._cut(why, 80)
        return (f"{title}: ждут {reel['people_waiting']} чел.{why}",
                "  " + (" · ".join(f"{status}{'/' + reason if reason else ''} {n}"
                                    for status, reason, n in reel["counts"]) or "комментариев нет"))

    reels = [reel_lines(reel) for reel in d["reels"]]
    # Preserve the normal readable report and the existing count-first compression when they fit.
    for problem_chars, with_counts in ((400, True), (400, False), (150, False), (40, False)):
        if notify._units(d["verdict"]) > limit:
            break
        out = [d["verdict"]] + [f"  ⚠ {key}: {text[:problem_chars]}" for key, text in d["problems"]]
        out += [line for lines in reels for line in (lines if with_counts else lines[:1])]
        out += tail
        if not with_counts and reels:
            out.append(f"… опущено строк: {len(reels)}; полный отчёт: igbot report")
        if any(len(text) > problem_chars for _, text in d["problems"]):
            out.append("… сокращены описания проблем; полный отчёт: igbot report")
        text = "\n".join(out)
        if notify._units(text) <= limit:
            return text

    # Unlimited problem keys cannot fit verbatim. The total and representative keys make that explicit.
    head = [notify._cut(d["verdict"], 240)]
    if d["problems"]:
        keys = ", ".join(notify._cut(key, 40) for key, _ in d["problems"][:5])
        head.append(f"Проблем: {len(d['problems'])}; ключи (первые {min(5, len(d['problems']))}): {keys}")
    footer = f"… опущено строк: {2 * len(reels)}; сведения сокращены; полный отчёт: igbot report"
    remaining = limit - notify._units("\n".join(head + tail + [footer]))
    if remaining < 0:
        raise ValueError("digest limit is too small for its mandatory summary and typo list")
    selected, omitted = {}, 2 * len(reels)
    bounded_reels = [reel_lines(reel, bounded=True) for reel in d["reels"]]
    # Whole bounded reel summaries take precedence over their optional status counts.
    for column in (0, 1):
        for index, lines in enumerate(bounded_reels):
            if column and (index, 0) not in selected:
                continue
            line = notify._cut(lines[column], 240)
            cost = 1 + notify._units(line)
            if cost <= remaining:
                selected[index, column] = line
                remaining -= cost
                omitted -= 1
    footer = f"… опущено строк: {omitted}; сведения сокращены; полный отчёт: igbot report"
    optional = [selected[key] for key in sorted(selected)]
    return "\n".join(head + optional + tail + [footer])


def _near_display(text):
    """Show terminal controls visibly in a near-comment body; retain the stored/JSON value."""
    return "".join(f"\\x{ord(char):02x}" if ord(char) < 32 or 127 <= ord(char) <= 159 else char
                   for char in str(text))


def render(d):
    """The report as text, in the order of SPEC 9; the first line is the verdict."""
    out = [d["verdict"], f"Отчёт на {_when(d['at'])}", ""]
    out.append("Проблемы:" if d["problems"] else "Проблем нет.")
    out += [f"  ⚠ {key}: {text}" for key, text in d["problems"]]
    parts = ", ".join(f"{_UNACCOUNTED[name]} — {n}" for name, n in d["unaccounted_parts"].items() if n)
    out += ["", *_render_gates(d["gates"]),
            f"  не сходится с журналом (unaccounted): {d['unaccounted']}" + (f" ({parts})" if parts else ""),
            "", "Рилсы:" if d["reels"] else "Рилсов нет."]
    for reel in d["reels"]:
        out += _render_reel(reel)
    out += ["", "Ссылок в директ по часам за 24 ч (UTC):" if d["dms_per_hour"] else "За 24 ч ссылок в директ не было."]
    out += [f"  {_when(row['hour'] + ':00')[:-4]}  попыток {row['posts']}, доставлено {row['ok']}"
            for row in d["dms_per_hour"]]
    out += ["", "Все попытки по исходу:" if d["attempts"] else "Попыток отправки ещё не было."]
    out += [f"  {row['kind']} {row['outcome'] or 'без ответа'}{'/' + row['reason'] if row['reason'] else ''}: "
            f"{row['n']}" for row in d["attempts"]]
    if d["uncertain"]:
        names = {reel["media_id"]: reel["name"] for reel in d["reels"]}
        out += ["", f"Неизвестно, дошло ли сообщение (uncertain): {d['uncertain_unresolved']} без подтверждения; "
                f"показаны последние {len(d['uncertain'])}. Сначала проверьте доставку в диалоге/Requests, "
                "затем примите ручное решение и подтвердите его; бот повторно не отправляет:"]
        out += [f"  @{row['username'] or '?'} ({names.get(row['media_id']) or row['media_id']}, {row['reason']}); "
                f"igbot ack-uncertain {row['comment_id']}"
                for row in d["uncertain"]]
    out += ["", f"С ключевым словом, но не просьба (near): {d['near']['total']}"]
    out += [f"  @{row['username'] or '?'}: «{_near_display(row['text'])}» ({row['reason']})"
            for row in d["near"]["newest"]]
    out += ["", "Частые короткие комментарии без ключевого слова (опечатки — в also_match):" if d["unmatched"]
            else "Коротких комментариев без ключевого слова нет."]
    out += [f"  {n} × {text}" for text, n in d["unmatched"]]
    if d.get("funnel"):
        out += ["", "Воронка по вариантам (первое сообщение → нажали → подписан/нет → ссылка → перешли):"]
        out += [f"  {row['reel']} / {row['variant']}: {row['first_dm']} → {row['pressed']} → "
                f"{row['checked_yes']}/{row['checked_no']} → {row['linked']} → {row['clicked_people']} чел. "
                f"({row['clicks']} кликов); повторное {row['followup']}, застряли {row['stuck']}, "
                f"отписались {row['unfollowed']}" for row in d["funnel"]]
    out += ["", _followers_line(d)]
    return "\n".join(out)
