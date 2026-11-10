#!/usr/bin/env python3
"""Command line of the bot (SPEC sections 3 and 10): arguments, lock, wiring, exit codes. The logic is in the modules.

    run | once | send-one COMMENT_ID                     take the instance lock (exit 3 if it is held; `run` waits
                                                         a minute for it first and says so on Telegram)
    arm N|all [--deaf] | disarm | ack-uncertain ID      no lock: one transaction
    report [--json] | check [--online] | media           read-only
"""
import argparse
import fcntl
import json
import os
import signal
import socket
import sqlite3
import sys
import time
import traceback
from datetime import timedelta

import clock
import config
import engine
import meta
import notify
import store

OK, ERROR, USAGE, LOCKED, BAD_CONFIG, MISSING, UNUSABLE = 0, 1, 2, 3, 4, 5, 6
LOCK_TRIES, LOCK_PAUSE = 5, 0.05   # real seconds: core.lock is tried for a quarter of a second before exit 3


class Exit(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------- runtime (SPEC 3) ----------

def sd_notify(state):
    """Tell systemd `state` over the datagram socket it gave us; nothing happens without NOTIFY_SOCKET."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode(), path)
    except OSError:
        pass   # a missed ping must not stop the bot: the watchdog restarts it if they all fail


def ping():
    sd_notify("WATCHDOG=1")


def take_lock(state_dir, wait=0):
    """The instance lock, held until the process ends. Exit 3 if another process holds it.

    `wait`: seconds to go on trying, once per second. `once` and `send-one` hold the lock for one cycle only;
    a service start that meets one of them must not end for good (systemd never restarts exit 3).
    """
    os.makedirs(state_dir, exist_ok=True)
    lock = open(os.path.join(state_dir, "core.lock"), "a")
    # `report` tests the lock by holding it shared for an instant (report.py, lock_held). One refusal is
    # therefore not yet a second runner, and exit 3 is final: systemd does not restart after it (SPEC 3).
    tries, give_up = LOCK_TRIES, clock.monotonic() + wait
    while True:
        for _ in range(tries):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                time.sleep(LOCK_PAUSE)
        if clock.monotonic() >= give_up:
            lock.close()
            raise Exit(LOCKED, "another run, once or send-one holds core.lock")
        tries = 1
        clock.sleep(clock.T["lock_retry"], ping=ping)


def open_db(state_dir, create=False, readonly=False):
    path = os.path.join(state_dir, "core.db")
    if not create and not os.path.exists(path):
        raise Exit(MISSING, f"no database at {path}")
    conn = store.connect(path, readonly=readonly)
    if create:
        store.init(conn)
    return conn


def telegram(state_dir):
    """get_tg for notify.py: (bot token, chat id) as .env has them now; the file may change while the service
    runs. While the file cannot be read, the values last read in this process run stay in use."""
    last = [None, None]

    def get_tg():
        env, problem = engine.read_env_checked(os.path.join(state_dir, ".env"))
        if problem is None:
            last[:] = env.get("TG_BOT_TOKEN"), env.get("TG_CHAT_ID")
        return tuple(last)
    return get_tg


def fatal_start(state_dir, text):
    """`run` did not start: one Telegram message past the database, which may be unusable or not ours (SPEC 3).
    True if Telegram confirmed it."""
    told = make_notifier(state_dir, None).direct("fatal_start", f"⚠ fatal_start: {text}"[:1500])
    engine.log("ERROR", "fatal_start", told_telegram=told)
    return bool(told)


def _heard(told, code, reason):
    """How a failed start of `run` ends. With its own code (systemd does not start the service again) only when
    Telegram confirmed `fatal_start`: a service that is down for good must have been said. Unheard, it ends with 1,
    systemd starts it again in 30 s and the start says it again; no request goes to Meta meanwhile."""
    if told:
        return Exit(code, reason)
    return Exit(ERROR, f"{reason}; Telegram did not confirm fatal_start: exit 1, systemd starts the service again")


def make_notifier(state_dir, conn, **options):
    return notify.Notifier(conn, telegram(state_dir), config.tg_api_base(), **options)


def make_client(conn, api_version, token=None):
    """`token` None: the stored token, read at every call (a refresh or a new .env value is used at once)."""
    return meta.Client(config.ig_api_base(), api_version,
                       (lambda: store.kv_get(conn, "token") or "") if token is None else (lambda: token), ping)


def stop_flag():
    """SIGTERM/SIGINT set a flag: the call in flight and its database write finish, then the loop ends."""
    stop = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.append(True))
    return lambda: bool(stop)


# ---------- commands that take the lock ----------

def cmd_cycle(args, state_dir, config_path):
    """run, once and send-one: the same start, then the loop, one cycle, or one cycle for one comment."""
    service = args.command == "run"   # nobody watches its start: whatever ends it is said on Telegram (SPEC 3)
    conn = None
    try:
        lock = take_lock(state_dir, clock.T["lock_wait_run"] if service else 0)   # noqa: F841  held until exit
        conn = open_db(state_dir, create=True)
        cfg = config.load(config_path)
        notifier = make_notifier(state_dir, conn, repeat_min=cfg.alerts.repeat_min)
        # the engine's start writes to the database: one that cannot be written ends the start here
        stopping = stop_flag()
        bot = engine.Engine(cfg, conn, make_client(conn, cfg.api_version), notifier, stopping, ping,
                            config_path=config_path)
        if service:
            bot.prepare_run()   # all database-dependent run preparation belongs before READY
    except Exit as stop:
        if service:
            told = fatal_start(state_dir, f"the service did not start: {stop}. systemd will not start it again: "
                                          f"start it when that command has ended")
            raise _heard(told, stop.code, str(stop)) from None
        raise
    except config.ConfigError as error:
        if service:   # systemd does not restart after exit 4: say why the service is down, directly like the others
            problems = "; ".join(error.problems)
            told = fatal_start(state_dir, "the config is invalid, the service did not start: " + problems[:1000])
            if not told:
                raise _heard(False, BAD_CONFIG, "config invalid: " + problems) from None
        raise
    except (sqlite3.Error, OSError, RuntimeError) as error:
        reason = f"{type(error).__name__}: {error}"
        told = service and fatal_start(state_dir, f"the service did not start: the database cannot be used ({reason})")
        if service:
            raise _heard(told, UNUSABLE, f"database unusable: {reason}") from None
        raise Exit(UNUSABLE, f"database unusable: {reason}") from None
    except Exception as error:
        if service:
            fatal_start(state_dir, f"the service did not start: {type(error).__name__}: {error}")
        raise
    sd_notify("READY=1")   # the lock is ours, the schema is ready and can be written; no API call was made yet
    if service:
        bot.run()
        return OK
    sent = bot.cycle(getattr(args, "comment_id", None), wait_for_startup=True)
    if stopping():
        return OK
    if store.kv_get(conn, "last_cycle_error"):
        return ERROR
    if args.command == "send-one":
        row = store.comment(conn, args.comment_id)
        print(f"{args.comment_id}: " + (f"{row['status']} {row['reason']}".strip() if row else "no such comment"))
        return OK if sent else ERROR
    return OK


# ---------- commands without the lock: one transaction each ----------

def _forecast(conn, cfg):
    """(people waiting, hours needed at the configured rate, how many would expire before their turn)."""
    turns = store.queue(conn, [reel.media_id for reel in cfg.reels if reel.active])   # one turn per person (8.4)
    # a long queue moves at max sends per (rate_window + cap_margin): the hourly cap is counted over that span (7.2)
    slot, now = timedelta(seconds=(clock.T["rate_window"] + clock.T["cap_margin"]) / cfg.max_sends_per_hour), clock.now()
    late = sum(store.window_closed(ts, now + slot * place) for place, (*_, ts) in enumerate(turns))
    return len(turns), len(turns) * slot.total_seconds() / 3600, late


def cmd_arm(args, state_dir, config_path):
    conn = open_db(state_dir)
    # what will happen is decided by the config the service runs with, not by the file as it is now (9)
    cfg, edited = config.running(store.kv_get(conn, "config_running"), store.kv_get(conn, "config_hash"),
                                 config.load(config_path))
    deaf = None if args.deaf else notify.deaf(conn, telegram(state_dir))
    if deaf:
        raise Exit(ERROR, f"not armed: {deaf} (--deaf arms anyway)")
    notifier = make_notifier(state_dir, conn)
    with store.tx(conn):
        store.arm(conn, args.n)
        notifier.event("armed", f"budget set to {args.n}")
    waiting, hours, late = _forecast(conn, cfg)
    print(f"armed: budget {args.n}{'' if cfg.sending else ' (the service runs with sending=false: nothing is sent)'}")
    if edited:
        print("config.json was changed after the service loaded it: the service runs with the old values until a "
              "restart, and the restart disarms")
    print(f"people waiting: {waiting}; hours needed at {cfg.max_sends_per_hour} per hour: {hours:.1f}; "
          f"would expire before their turn: {late}")
    return OK


def cmd_disarm(args, state_dir, config_path):
    conn = open_db(state_dir)
    notifier = make_notifier(state_dir, conn)
    store.arm(conn, 0)                                  # the brake holds whatever happens to its notice
    notifier.event("disarmed", "budget set to 0")
    print("disarmed: budget 0")
    return OK


def cmd_ack_uncertain(args, state_dir, config_path):
    """Record the owner's manual decision; uncertain remains terminal and never becomes a retry."""
    conn = open_db(state_dir)
    with store.tx(conn):
        row = store.comment(conn, args.comment_id)
        if row is None or row["status"] != "uncertain":
            raise Exit(ERROR, f"not acknowledged: {args.comment_id} is not an uncertain comment")
        key = "uncertain_ack:" + args.comment_id
        if store.kv_get(conn, key) is None:
            store.kv_set(conn, key, clock.iso())
    print(f"acknowledged manual review: {args.comment_id}; delivery remains uncertain")
    return OK


# ---------- read-only commands ----------

def cmd_report(args, state_dir, config_path):
    import report   # report.py is needed by this command and by the engine's cycle only
    conn = open_db(state_dir, readonly=True)
    file_error = None
    try:
        cfg = config.load(config_path)
    except config.ConfigError as error:
        stored = store.kv_get(conn, "config_running")
        if not stored:
            raise
        try:
            cfg = config.loads(stored)
        except (ValueError, config.ConfigError):
            raise error from None
        file_error = "; ".join(error.problems)
    data = report.build(conn, cfg, clock.now(), config_file_error=file_error)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else report.render(data))
    return OK


def _stored_token(state_dir, env):
    """Read-only preflight: the token the next cycle would use. Only a new nonempty env value wins."""
    path = os.path.join(state_dir, "core.db")
    token = seen = None
    if os.path.exists(path):
        conn = store.connect(path, readonly=True)
        try:
            token, seen = store.kv_get(conn, "token"), store.kv_get(conn, "token_env_seen")
        finally:
            conn.close()
    candidate = env.get("IG_ACCESS_TOKEN")
    if candidate and candidate != seen:
        token = candidate
    if not token:
        raise Exit(MISSING, "no token for the next cycle: neither stored nor a new IG_ACCESS_TOKEN in .env")
    return token


def cmd_check(args, state_dir, config_path):
    cfg = config.load(config_path)
    env = engine.read_env(os.path.join(state_dir, ".env"))
    reels = ", ".join(reel.name + ("" if reel.active else " (inactive)") for reel in cfg.reels)
    print(f"config ok: sending {'on' if cfg.sending else 'off'}, {cfg.max_sends_per_hour} per hour, "
          f"reels: {reels or 'none'}")
    for name in ("IG_ACCESS_TOKEN", "TG_BOT_TOKEN", "TG_CHAT_ID"):
        print(f".env {name}: {'set' if env.get(name) else 'MISSING'}")
    token = _stored_token(state_dir, env)
    if not args.online:
        return OK
    me = make_client(None, cfg.api_version, token).me()
    print(f"GET /me: {me.outcome} {me.reason} {me.data.get('username', '') if me.ok else me.message}".rstrip())
    bot_token, chat_id = telegram(state_dir)()
    failure = notify.send_message(config.tg_api_base(), bot_token, chat_id, "test message of `bot.py check --online`")
    print(f"Telegram test message: {failure or 'delivered'}")
    return OK if me.ok and failure is None else ERROR


def cmd_media(args, state_dir, config_path):
    conn = open_db(state_dir, readonly=True)
    token = store.kv_get(conn, "token")
    if not token:
        raise Exit(MISSING, "no token in the database: run `once` first")
    result = make_client(None, config.load(config_path).api_version, token).my_media()
    if not result.ok:
        raise Exit(ERROR, f"GET /me/media: {result.outcome} {result.reason} {result.message}")
    for item in result.data.get("data") or []:
        print(item.get("id"), item.get("permalink"), item.get("timestamp"), f"comments={item.get('comments_count')}")
    return OK


# ---------- entry ----------

def _budget(text):
    if text == "all":
        return "unlimited"
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError("a whole number, 0 or more, or `all`")
    return int(text)


def _parser():
    parser = argparse.ArgumentParser(prog="bot.py", description="Instagram comment-to-DM bot")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, run, text):
        sub = commands.add_parser(name, help=text)
        sub.set_defaults(run=run)
        return sub
    command("run", cmd_cycle, "the service loop")
    command("once", cmd_cycle, "one cycle, then exit")
    command("send-one", cmd_cycle, "one cycle that may DM only this comment").add_argument("comment_id")
    arm = command("arm", cmd_arm, "set the send budget")
    arm.add_argument("n", type=_budget, metavar="N|all")
    arm.add_argument("--deaf", action="store_true", help="arm although no alert can reach a human")
    command("disarm", cmd_disarm, "set the send budget to 0: the emergency brake")
    command("ack-uncertain", cmd_ack_uncertain, "acknowledge manual review; never resend").add_argument("comment_id")
    command("report", cmd_report, "state, problems, statistics").add_argument("--json", action="store_true")
    command("check", cmd_check, "validate config and .env").add_argument(
        "--online", action="store_true", help="also GET /me and one Telegram test message")
    command("media", cmd_media, "recent media: id, permalink, time, comments")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)   # a usage error exits 2
    unsafe = clock.check_safe_env()
    if unsafe:
        print(f"unsafe environment: {unsafe}", file=sys.stderr)
        return USAGE
    state_dir = os.path.abspath(os.environ.get("STATE_DIR") or os.path.join(os.path.dirname(__file__), "state"))
    config_path = os.path.abspath(os.environ.get("CONFIG") or os.path.join(state_dir, "config.json"))
    print(f"STATE_DIR={state_dir} CONFIG={config_path}", file=sys.stderr)
    try:
        return args.run(args, state_dir, config_path)
    except Exit as stop:
        print(stop, file=sys.stderr)
        return stop.code
    except config.ConfigError as error:
        print("config invalid:\n  " + "\n  ".join(error.problems), file=sys.stderr)
        return BAD_CONFIG
    except Exception:
        traceback.print_exc()
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
