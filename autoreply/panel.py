"""The panel JSON (SPEC 14.7): what the web panel shows, read from the database at one moment. Only READS.

What this module guarantees:
- build() never writes: every query is a SELECT inside one read transaction that is rolled back, so the whole
  JSON describes one moment of the database however the bot writes meanwhile; it works on a read-only
  connection and on a database without a single `flow`, `links` or `clicks` row;
- the bot's state, its problems and its gates come from report.build (the ONE definition of "something is
  wrong", SPEC 9), never from a second set of rules here; the config described is the one the service runs
  with (kv `config_running`), as in the report;
- a reel's numbers follow the statuses of SPEC 7.1; the funnel steps are the `kind='step'` attempts and the
  `flow`/`checks`/`links`/`clicks` rows of 14.4-14.6;
- clicks by link-preview robots (Meta fetches every link in a DM to draw its preview) are not counted as people's
  clicks: they are counted apart, as `robot_clicks`;
- no token reaches the JSON (report.build masks it; nothing here reads kv `token`).

    build(conn, cfg, now, config_file_error=None) -> dict
"""
import dataclasses
from datetime import timedelta

import clock
import config
import report
import store
from clock import T

PEOPLE_LISTED = 200
EVENTS_LISTED = 30
ALERTS_LISTED = 6
HOURS, DAYS = 24, 7
# 9: problems that mean the bot does not do its work at all until somebody acts; any other problem is `attention`
BLOCKING = ("not_running", "hold:account", "hold:send", "no_active_reel", "cycle_error", "clock", "env",
            "config_file_invalid")
ROBOTS = store.ROBOTS
_PERSON_CLICK = store.PERSON_CLICK
_ROBOT_CLICK = f"(NOT {_PERSON_CLICK})"
_DM_OK = "EXISTS (SELECT 1 FROM attempts a WHERE a.comment_id={} AND a.kind='dm' AND a.outcome='ok')"
_REQUESTED = f"(status NOT IN ('seen', 'dup_user') AND NOT {store.too_old_sql()})"   # people who asked in time
_COUNTS = {   # SPEC 7.1, per reel
    "comments": "1",
    "requested": _REQUESTED,
    "pending": "status IN ('pending', 'sending')",
    "sent": "status IN ('sent', 'done')",       # the DM is confirmed (done: the public reply is settled too)
    "done": "status='done'",
    "uncertain": "status='uncertain'",
    "refused": "status='refused'",
    "expired": store.lost_sql(),               # people lost: the window passed while they waited
    "too_old": store.too_old_sql(),            # already past the window when the bot first saw them: no loss
    "dup": "status='dup_user'",
    "already": "status='already'",
    "near": "status='seen' AND reason LIKE 'near:%'",
}


def _moment(conn):
    """BEGIN a read transaction unless one is open; returns whether this call opened it."""
    if conn.in_transaction:
        return False
    conn.execute("BEGIN")
    return True


def _plain(value):
    """A config object as JSON data: the flow of 14.2 is a tree of dataclasses (tuples become lists)."""
    if dataclasses.is_dataclass(value):
        value = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    elif hasattr(value, "__dict__"):
        value = vars(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _bot(data):
    found = [{"key": key, "text": text, "level": "bad" if key in BLOCKING
              else "warn"} for key, text in data["problems"]]
    gates = data["gates"]
    state = "blocked" if any(p["level"] == "bad" for p in found) else "attention" if found else "ok"
    token = gates["token"]
    return {
        "state": state, "verdict": data["verdict"], "problems": found,
        "sending": gates["sending"], "budget": gates["budget"], "armed_n": gates["armed_n"],
        "armed_at": gates["armed_at"],
        "account_hold": gates["account_hold"], "send_hold": gates["send_hold"],
        "running": gates["service"]["running"], "last_cycle_ok_at": gates["service"]["last_cycle_ok_at"],
        "telegram_ok": gates["telegram"]["configured"] is True and not gates["telegram"]["failing"],
        "token_expires_in_sec": token["expires_in_sec"],
        "unaccounted": data["unaccounted"], "uncertain_unresolved": data["uncertain_unresolved"],
    }


def _counts(conn):
    columns = ", ".join(f"COALESCE(SUM({sql}), 0)" for sql in _COUNTS.values())
    return {media_id: dict(zip(_COUNTS, values)) for media_id, *values in conn.execute(
        f"SELECT media_id, {columns} FROM comments GROUP BY media_id")}


def _variant_numbers(conn):
    """{(media_id, variant): funnel numbers} over the `flow` rows (14.7)."""
    rows = conn.execute(
        "SELECT f.media_id, f.variant, COUNT(*), "
        f"COALESCE(SUM({_DM_OK.format('f.comment_id')}), 0), "
        "COALESCE(SUM(f.pressed_at IS NOT NULL), 0), "
        "COALESCE(SUM(EXISTS (SELECT 1 FROM checks k WHERE k.comment_id=f.comment_id AND k.purpose='gate' "
        "AND k.result='yes')), 0), "
        "COALESCE(SUM(EXISTS (SELECT 1 FROM checks k WHERE k.comment_id=f.comment_id AND k.purpose='gate' "
        "AND k.result='no')), 0), "
        "COALESCE(SUM(f.link_at IS NOT NULL), 0), COALESCE(SUM(f.followup_at IS NOT NULL), 0), "
        "COALESCE(SUM(f.state='stuck'), 0), COALESCE(SUM(f.unfollowed_at IS NOT NULL), 0) "
        "FROM flow f GROUP BY f.media_id, f.variant")
    names = ("people", "first_dm", "pressed", "checked_yes", "checked_no", "linked", "followup", "stuck", "unfollowed")
    numbers = {(media_id, variant): dict(zip(names, values)) for media_id, variant, *values in rows}
    for media_id, variant, clicks, people, robots in conn.execute(
            f"SELECT l.media_id, COALESCE(l.variant, f.variant), COALESCE(SUM({_PERSON_CLICK}), 0), "
            f"COUNT(DISTINCT CASE WHEN {_PERSON_CLICK} THEN l.comment_id END), COALESCE(SUM({_ROBOT_CLICK}), 0) "
            "FROM clicks k JOIN links l ON l.token=k.token LEFT JOIN flow f ON f.comment_id=l.comment_id "
            "GROUP BY 1, 2"):
        entry = numbers.setdefault((media_id, variant), dict.fromkeys(names, 0))
        entry.update(clicks=clicks, clicked_people=people, robot_clicks=robots)
    for entry in numbers.values():
        entry.setdefault("clicks", 0)
        entry.setdefault("clicked_people", 0)
        entry.setdefault("robot_clicks", 0)
    return numbers


def _reel_clicks(conn):
    return {media_id: {"clicks": clicks, "clicked_people": people, "robot_clicks": robots}
            for media_id, clicks, people, robots in conn.execute(
                f"SELECT l.media_id, COALESCE(SUM({_PERSON_CLICK}), 0), "
                f"COUNT(DISTINCT CASE WHEN {_PERSON_CLICK} THEN l.comment_id END), COALESCE(SUM({_ROBOT_CLICK}), 0) "
                "FROM clicks k JOIN links l ON l.token=k.token GROUP BY l.media_id")}


def _reels(conn, cfg, state_by_media):
    counts, numbers, clicks = _counts(conn), _variant_numbers(conn), _reel_clicks(conn)
    empty_counts = dict.fromkeys(_COUNTS, 0)
    zero = {"people": 0, "first_dm": 0, "pressed": 0, "checked_yes": 0, "checked_no": 0, "linked": 0, "clicks": 0,
            "clicked_people": 0, "robot_clicks": 0, "followup": 0, "stuck": 0, "unfollowed": 0}
    configured = cfg.reel_by_media
    media_ids = [*configured, *sorted({m for m in counts if m not in configured} |
                                      {m for m, _ in numbers if m not in configured})]
    entries = []
    for media_id in media_ids:
        reel = configured.get(media_id)
        flow = getattr(reel, "flow", None)
        variants, seen = [], set()
        for variant in getattr(flow, "variants", None) or ():
            seen.add(variant.name)
            variants.append({"name": variant.name, "weight": variant.weight, "opening": bool(variant.opening),
                             "gate": bool(variant.gate), "text": getattr(variant, "text", None), "configured": True,
                             **numbers.get((media_id, variant.name), zero)})
        for (media, name), values in sorted(numbers.items(), key=lambda item: str(item[0][1])):
            if media == media_id and name not in seen and name is not None:   # a variant since removed
                variants.append({"name": name, "weight": 0, "opening": None, "gate": None, "text": None,
                                 "configured": False, **values})
        entries.append({
            "name": reel.name if reel else None, "media_id": media_id, "configured": bool(reel),
            "permalink": reel.permalink if reel else "", "active": bool(reel and reel.active),
            "keywords": list(reel.keywords) if reel else [], "also_match": list(reel.also_match) if reel else [],
            "max_words": reel.max_words if reel else None, "link": reel.link if reel else "",
            "text": reel.text if reel else "", "flow": _plain(flow),
            "state": state_by_media.get(media_id, {}),
            "counts": counts.get(media_id, empty_counts),
            **clicks.get(media_id, {"clicks": 0, "clicked_people": 0, "robot_clicks": 0}),
            "variants": variants,
        })
    return entries


def _people(conn, names):
    rows = conn.execute(
        "SELECT c.comment_id, c.username, c.text, c.ts, c.media_id, "
        "CASE WHEN c.status='seen' THEN 'near' ELSE c.status END, c.reason, "
        "f.variant, f.state, f.pressed_at, f.follow, f.link_at, f.followup_at, f.unfollowed_at, "
        "(SELECT MAX(a.finished_at) FROM attempts a WHERE a.comment_id=c.comment_id AND a.kind='dm' "
        " AND a.outcome='ok'), "
        f"(SELECT COUNT(*) FROM clicks k JOIN links l ON l.token=k.token WHERE l.comment_id=c.comment_id "
        f" AND {_PERSON_CLICK}) "
        "FROM comments c LEFT JOIN flow f ON f.comment_id=c.comment_id "
        "WHERE c.status<>'seen' OR c.reason LIKE 'near:%' ORDER BY c.ts DESC, c.comment_id DESC LIMIT ?",
        (PEOPLE_LISTED,))
    keys = ("comment_id", "username", "text", "ts", "media_id", "status", "reason", "variant", "state", "pressed_at",
            "follow", "link_at", "followup_at", "unfollowed_at", "dm_at", "clicks")
    people = []
    for row in rows:
        person = dict(zip(keys, row))
        person["reel"] = names.get(person["media_id"])
        people.append(person)
    return people


def _hours(conn, now):
    """DMs confirmed per hour, the last 24 hours, the current hour last; hours without one are 0."""
    top = now.replace(minute=0, second=0, microsecond=0)
    keys = [clock.iso(top - timedelta(hours=back))[:13] for back in range(HOURS - 1, -1, -1)]
    found = dict(conn.execute(
        "SELECT substr(finished_at, 1, 13), COUNT(*) FROM attempts WHERE kind='dm' AND outcome='ok' "
        "AND finished_at >= ? GROUP BY 1", (keys[0],)).fetchall())
    return [{"hour": key + ":00:00Z", "ok": found.get(key, 0)} for key in keys]


def _days(conn, now):
    """Per UTC day, the last 7 days: people who asked (by comment time) and DMs confirmed."""
    keys = [clock.iso(now - timedelta(days=back))[:10] for back in range(DAYS - 1, -1, -1)]
    asked = dict(conn.execute(f"SELECT substr(ts, 1, 10), COUNT(*) FROM comments WHERE {_REQUESTED} AND ts >= ? "
                              "GROUP BY 1", (keys[0],)).fetchall())
    sent = dict(conn.execute("SELECT substr(finished_at, 1, 10), COUNT(*) FROM attempts WHERE kind='dm' "
                             "AND outcome='ok' AND finished_at >= ? GROUP BY 1", (keys[0],)).fetchall())
    return [{"day": key + "T00:00:00Z", "requested": asked.get(key, 0), "sent": sent.get(key, 0)} for key in keys]


def _events(conn, names):
    """The newest attempts (DMs, public replies, funnel steps) and taps, newest first."""
    found = [{"at": at, "kind": kind, "detail": detail if kind == "step" else None, "outcome": outcome,
              "username": username, "reel": names.get(media_id)}
             for at, kind, detail, outcome, username, media_id in conn.execute(
                 "SELECT COALESCE(a.finished_at, a.started_at), a.kind, a.detail, a.outcome, c.username, c.media_id "
                 "FROM attempts a LEFT JOIN comments c ON c.comment_id=a.comment_id ORDER BY a.id DESC LIMIT ?",
                 (EVENTS_LISTED,))]
    found += [{"at": at, "kind": "tap", "detail": None, "outcome": None, "username": username,
               "reel": names.get(media_id)}
              for at, username, media_id in conn.execute(
                  "SELECT f.pressed_at, c.username, f.media_id FROM flow f LEFT JOIN comments c "
                  "ON c.comment_id=f.comment_id WHERE f.pressed_at IS NOT NULL ORDER BY f.pressed_at DESC LIMIT ?",
                  (EVENTS_LISTED,))]
    return sorted(found, key=lambda e: e["at"] or "", reverse=True)[:EVENTS_LISTED]


def _totals(reels):
    total = {key: 0 for key in ("requested", "sent", "pending", "failed", "clicks", "robot_clicks", "pressed",
                                "linked", "comments", "uncertain", "refused", "expired", "already", "near")}
    for reel in reels:
        counts = reel["counts"]
        for key in ("requested", "sent", "pending", "comments", "uncertain", "refused", "expired", "already", "near"):
            total[key] += counts[key]
        total["failed"] += counts["uncertain"] + counts["refused"] + counts["expired"]
        total["clicks"] += reel["clicks"]
        total["robot_clicks"] += reel["robot_clicks"]
        for variant in reel["variants"]:
            total["pressed"] += variant["pressed"]
            total["linked"] += variant["linked"]
    return total


def build(conn, cfg, now, *, config_file_error=None):
    """The panel JSON at `now` (14.7). `cfg` is the config file as loaded (or the running one when the file is
    invalid); the reels described are those of the config the service runs with, as in the report."""
    opened = _moment(conn)
    try:
        data = report.build(conn, cfg, now, config_file_error=config_file_error)
        kv = dict(conn.execute("SELECT key, value FROM kv WHERE key IN "
                               "('config_running', 'config_hash', 'me_username')").fetchall())
        running, _ = config.running(kv.get("config_running"), kv.get("config_hash"), cfg)
        names = {reel.media_id: reel.name for reel in running.reels}
        state_by_media = {entry["media_id"]: {
            "people_waiting": entry["people_waiting"], "waiting_reason": entry["waiting_reason"],
            "read_state": entry["state"], "full_sweep_at": entry["full_sweep_at"],
            "first_expires_in_sec": entry["first_expires_in_sec"]} for entry in data["reels"]}
        reels = _reels(conn, running, state_by_media)
        alerts = [{"at": at, "text": text} for at, text in conn.execute(
            "SELECT created_at, text FROM outbox ORDER BY id DESC LIMIT ?", (ALERTS_LISTED,))]
        return {
            "generated_at": clock.iso(now),
            "account": {"username": kv.get("me_username"), "followers": data["followers"]},
            "bot": _bot(data),
            "totals": _totals(reels),
            "reels": reels,
            "people": _people(conn, names),
            "hourly": _hours(conn, now),
            "daily": _days(conn, now),
            "events": _events(conn, names),
            "alerts": alerts,
            "settings": {
                "max_sends_per_hour": running.max_sends_per_hour, "poll_interval_sec": running.poll_interval_sec,
                "public_reply": running.public_reply, "public_phrases": list(running.public_phrases),
                "greeting": running.greeting, "late_greeting": running.late_greeting,
                "late_after_minutes": running.late_after_minutes, "profile_url": running.profile_url,
                "click_base": getattr(running, "click_base", "") or "",
                "alerts": {"repeat_min": running.alerts.repeat_min,
                           "digest_hour_utc": running.alerts.digest_hour_utc},
                "window_days": round(T["reply_window"] / 86400),
            },
        }
    finally:
        if opened and conn.in_transaction:
            conn.execute("ROLLBACK")   # nothing was written
