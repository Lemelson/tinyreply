"""Time for the whole bot (SPEC section 3): one clock, one timestamp format, every duration by name.

    now()        aware UTC datetime, whole milliseconds, so parse(iso(now())) == now()
    iso(dt)      'YYYY-MM-DDTHH:MM:SS.mmmZ' (fixed width: string order is time order)
    parse(s)     the reverse; also reads Meta's '2026-10-03T12:34:56+0000'
    sleep(sec)   scaled sleep in slices of at most 1 real second
    monotonic()  scaled monotonic seconds, comparable with differences of now()
    T            every duration of the spec, in seconds

Test hook: CLOCK_SCALE (float) and CLOCK_ANCHOR (unix seconds) make the bot's time run faster:
now() = anchor + (time.time() - anchor) * scale. HTTP timeouts stay real time: use time.monotonic() for them.
"""
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

_MIN, _HOUR, _DAY = 60, 3600, 86400

T = {
    # send path (7.2, G2)
    "reply_window": 7 * _DAY,           # Meta takes a private reply for 7 days after the comment
    "window_margin": 10 * _MIN,         # ... and we stop this long before the window closes
    "rate_window": _HOUR,               # max_sends_per_hour counts a rolling hour; one slot = rate_window / max
    "clock_offset_margin": 600.001,     # two supported 5-minute offsets, plus the millisecond timestamp floor
    "cap_margin": 660.001,              # offset margin + bounded unknown-flight http_deadline (see SPEC limit)
    "public_delay": (2, 4),             # random pause before a public reply (7.4, 8.4)
    # collect (8.3)
    "full_sweep_every": 30 * _MIN,
    # clock guard (8.1)
    "clock_back": 5 * _MIN,             # now() this far behind kv last_now
    "clock_jump": 10 * _MIN,            # wall time this far ahead of monotonic time since the last cycle
    # token (8.2)
    "token_min_age": 24 * _HOUR,        # never refresh a younger token
    "token_refresh_age": 7 * _DAY,      # refresh when older than this
    "token_retry_after": 6 * _HOUR,     # at most one refresh per this after a failure
    "token_warn_left": 14 * _DAY,       # problem `token` when less than this remains
    "token_lifetime": 60 * _DAY,        # Meta: a long-lived token lives this long from its last refresh
    # account hold (8.5)
    "probe_schedule": (5 * _MIN, 10 * _MIN, 20 * _MIN, 30 * _MIN),   # wait before each probe; the last one repeats
    "probe_first_rate": 15 * _MIN,      # reason `rate`: wait before the first probe
    # send hold (8.5): base x 2^(k-1), capped at max
    "hold_uncertain_base": 15 * _MIN,
    "hold_uncertain_max": 6 * _HOUR,
    "hold_blocked_base": 30 * _MIN,
    "hold_blocked_max": 12 * _HOUR,
    "hold_spam": 24 * _HOUR,
    "hold_streak_base": _HOUR,
    "hold_streak_max": 12 * _HOUR,
    # housekeeping (8.7)
    "metric_every": _HOUR,
    # report and problems (9)
    "waiting_after": 30 * _MIN,         # people wait and no ok DM for this long
    "expiring_within": 24 * _HOUR,
    "reel_stale_after": 2 * _HOUR,      # no ok full sweep for this long
    "not_running_after": 10 * _MIN,     # last_cycle_ok_at older than this
    "open_claim_after": 3 * _MIN,       # `sending` rows and open attempts older than this are unaccounted
    "public_owed_after": 10 * _MIN,     # `sent` rows of a sendable reel older than this are unaccounted
    "report_dm_history": 24 * _HOUR,    # DMs per hour are listed this far back
    "refusals_within": 24 * _HOUR,      # problem `refusals` looks at the DMs of this long
    "followers_ago": (24 * _HOUR, 7 * _DAY),
    # funnel (14)
    "step_cap": 200,                    # step POSTs per rolling hour (they answer a person's own tap)
    "inbound_stale": 10 * _MIN,         # problem `inbound_waiting`: the oldest unhandled tap is older than this
    "inbound_max_age": 24 * _HOUR,      # a tap that waited this long is closed `window_passed`
    "followup_window": 23 * _HOUR,      # a follow-up only while the person's last message is younger than this
    "recheck_after": (_DAY, 7 * _DAY),  # follow status is read again this long after the link
    "max_prompts": 3,
    # config bound (4)
    "poll_interval_min": 30,
    "poll_interval_max": 5 * _MIN,      # every 10-minute rule of section 9 must see at least two cycles
    # start-up (3)
    "lock_wait_run": 60,                # `run` waits this long for core.lock: a one-off command holds it for a cycle
    "lock_retry": 1,
    # REAL seconds, never scaled (3, 6, 9)
    "sleep_slice": 1,
    "http_socket_timeout": 30,
    "http_deadline": 60,
    "tg_timeout": 10,
    "db_busy_timeout": 30,
}

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LOOPBACK = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})")


def _is_loopback(base):
    found = _LOOPBACK.fullmatch(base)
    return bool(found) and 0 < int(found.group(1)) < 65536


def _read_env(env):
    """(scale, anchor, problem) from CLOCK_SCALE / CLOCK_ANCHOR.

    On any problem the clock stays real, so scaled time can never run against a real service even if the
    caller forgets check_safe_env().
    """
    try:
        scale = float(env.get("CLOCK_SCALE") or 1)
        anchor = float(env.get("CLOCK_ANCHOR") or 0)
    except ValueError:
        return 1.0, 0.0, "CLOCK_SCALE and CLOCK_ANCHOR must be numbers"
    if scale == 1:
        return 1.0, 0.0, None
    if not (math.isfinite(scale) and scale > 0 and math.isfinite(anchor)):
        return 1.0, 0.0, "CLOCK_SCALE must be a positive number and CLOCK_ANCHOR unix seconds"
    if not env.get("CLOCK_ANCHOR"):
        # without a shared anchor every (re)start of the bot and of the fake would get its own time line
        return 1.0, 0.0, "CLOCK_SCALE other than 1 needs CLOCK_ANCHOR (unix seconds)"
    for name in ("IG_API_BASE", "TG_API_BASE"):
        if not _is_loopback(env.get(name) or ""):
            return 1.0, 0.0, f"CLOCK_SCALE other than 1 is only for a local fake: {name} must be http://127.0.0.1:<port>"
    return scale, anchor, None


SCALE, ANCHOR = _read_env(os.environ)[:2]


def check_safe_env():
    """Why this environment must not run (bot.py exits 2), or None."""
    return _read_env(os.environ)[2]


def now():
    scaled = ANCHOR + (time.time() - ANCHOR) * SCALE
    return _EPOCH + timedelta(milliseconds=math.floor(scaled * 1000))


def iso(dt=None):
    dt = now() if dt is None else dt
    if dt.utcoffset() is None:
        raise ValueError("naive datetime: every timestamp must carry its UTC offset")
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse(s):
    """Aware UTC datetime from an ISO 8601 string; ValueError for anything else, including no offset."""
    try:
        dt = datetime.fromisoformat(s)
    except TypeError:
        raise ValueError(f"not a timestamp: {s!r}") from None
    if dt.utcoffset() is None:
        raise ValueError(f"timestamp without UTC offset: {s!r}")
    return dt.astimezone(timezone.utc)


def monotonic():
    return time.monotonic() * SCALE


def sleep(seconds, should_stop=None, ping=None):
    """Sleep `seconds` of bot time. Before each slice: return at once if should_stop() is true, then ping()."""
    end = time.monotonic() + seconds / SCALE
    while (left := end - time.monotonic()) > 0:
        if should_stop and should_stop():
            return
        if ping:
            ping()
        time.sleep(min(T["sleep_slice"], left))
