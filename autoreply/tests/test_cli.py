"""SPEC sections 3 (runtime) and 10 (CLI): bot.py as a subprocess, started the way systemd starts it.

Every process gets its own STATE_DIR in a temporary directory and talks only to an in-process fake Meta and
Telegram on 127.0.0.1. The bot's clock runs 600 times faster, the fake's with it (CLOCK_SCALE / CLOCK_ANCHOR).
"""
import fcntl
import json
import os
import pathlib
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import clock
import store
from sim.fake_meta import FakeMeta

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(CODE, "bot.py")
SCALE = 600
ME = {"user_id": "17841400000000001", "id": "9990000000000001", "username": "your_account",
      "followers_count": 1234, "media_count": 7}
MEDIA = "17900000000000101"
TOKEN = "IGAAclitoken"
with open(os.path.join(CODE, "config.example.json"), encoding="utf-8") as _file:
    EXAMPLE = json.load(_file)


def comment(comment_id, age=60, text="арена"):
    return {"id": comment_id, "text": text, "age_sec": age, "should_match": True,
            "from": {"id": "u_" + comment_id, "username": "name_" + comment_id}}


class CliCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = None
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("STATE_DIR", "CONFIG", "NOTIFY_SOCKET", "CLOCK_SCALE", "CLOCK_ANCHOR")}
        self.env.update(STATE_DIR=self.dir, IG_API_BASE="http://127.0.0.1:9", TG_API_BASE="http://127.0.0.1:9",
                        PYTHONDONTWRITEBYTECODE="1")
        self.services = []

    def tearDown(self):
        for process in self.services:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.log.close()
        if self.fake:   # a correct bot never makes a call that is not one of SPEC section 6
            self.assertEqual([r["strict"] for r in self.requests() if r["strict"]], [])

    # ---- the world of one test

    def world(self, comments=(), config=True, env_file=True, scale=SCALE, **config_changes):
        """A fake Meta + Telegram, and a state directory with a config and .env that point the bot at it."""
        anchor = time.time()
        for patch in (mock.patch.object(clock, "SCALE", float(scale)), mock.patch.object(clock, "ANCHOR", anchor)):
            patch.start()   # this process reads the database with the bot's clock
            self.addCleanup(patch.stop)
        self.fake = FakeMeta({"account": ME, "token": TOKEN, "media": [{
            "id": MEDIA, "permalink": "https://www.instagram.com/reel/DAbc/", "shortcode": "DAbc",
            "comments": list(comments)}]}, scale=scale, anchor=anchor).start()
        self.addCleanup(self.fake.stop)
        self.env.update(IG_API_BASE=self.fake.api_base, TG_API_BASE=self.fake.api_base, CLOCK_SCALE=str(scale),
                        CLOCK_ANCHOR=repr(anchor))
        if config:
            self.write_config(**config_changes)
        if env_file:
            self.write(".env", f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN=123:abc\nTG_CHAT_ID=42\n")

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as file:
            file.write(text)

    def write_config(self, **changes):
        self.write("config.json", json.dumps({**EXAMPLE, "sending": True, "poll_interval_sec": 30,
                                              "max_sends_per_hour": 300, **changes}))

    def bot(self, *args, env=None):
        """Run one command to its end: (exit code, stdout, stderr)."""
        done = subprocess.run([sys.executable, "-B", BOT, *args], env={**self.env, **(env or {})},
                              capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def service(self, *args, env=None):
        """Start `bot.py run` (or another command) and leave it running."""
        log = open(os.path.join(self.dir, f"service{len(self.services)}.log"), "w+", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-B", BOT, *(args or ("run",))], env={**self.env, **(env or {})},
                                   stdout=subprocess.DEVNULL, stderr=log)
        process.log = log
        self.services.append(process)
        return process

    def stop(self, process):
        process.send_signal(signal.SIGTERM)
        return process.wait(timeout=20)

    def wait(self, what, check, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return
            time.sleep(0.02)
        logs = "".join(pathlib.Path(process.log.name).read_text("utf-8")[-3000:] for process in self.services)
        self.fail(f"timed out waiting for {what}\n{logs}")

    def cycles(self, count=3):
        """Let the running service finish `count` more cycles."""
        seen = set()

        def moved():
            seen.add(self.kv("last_cycle_ok_at"))
            return len(seen) > count
        self.wait(f"{count} cycles", moved)

    # ---- looking at the fake's truth and at the database

    def requests(self, *kinds):
        return [r for r in self.fake.control("/truth")["requests"] if not kinds or r["kind"] in kinds]

    def dm_targets(self):
        return [r["target"] for r in self.requests("dm")]

    def telegram(self):
        return "\n".join(message["text"] for message in self.fake.control("/truth")["telegram"])

    @property
    def conn(self):
        if not hasattr(self, "_conn"):
            self._conn = store.connect(os.path.join(self.dir, "core.db"), readonly=True)
            self.addCleanup(self._conn.close)
        return self._conn

    def kv(self, key):
        try:
            return store.kv_get(self.conn, key) if os.path.exists(os.path.join(self.dir, "core.db")) else None
        except sqlite3.OperationalError:   # the file is there, its schema not yet
            return None

    def status(self, comment_id):
        row = store.comment(self.conn, comment_id)
        return row and (row["status"], row["reason"])


class ExitCodes(CliCase):
    def test_usage_errors_exit_2(self):
        for args in ([], ["frobnicate"], ["arm"], ["arm", "-3"], ["arm", "many"], ["send-one"]):
            with self.subTest(args=args):
                code, out, _ = self.bot(*args)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
        self.assertEqual(os.listdir(self.dir), [])                  # nothing was touched

    def test_scaled_clock_against_anything_but_a_local_fake_exits_2(self):
        self.write("config.json", json.dumps(EXAMPLE))
        local, far = "http://127.0.0.1:9", "https://meta.invalid"   # .invalid never resolves
        for ig, tg in ((far, local), (local, far), ("http://localhost:9", local), ("", "")):
            with self.subTest(ig=ig, tg=tg):
                env = {"CLOCK_SCALE": "10", "CLOCK_ANCHOR": "1700000000", "IG_API_BASE": ig, "TG_API_BASE": tg}
                for command in ("check", "run", "once"):
                    code, _, err = self.bot(command, env=env)
                    self.assertEqual(code, 2, err)
                    self.assertIn("unsafe environment", err)
        self.assertEqual(os.listdir(self.dir), ["config.json"])     # no lock, no database: it never started
        code, _, _ = self.bot("check", env={"CLOCK_SCALE": "10", "CLOCK_ANCHOR": "1700000000",
                                            "IG_API_BASE": local, "TG_API_BASE": local})
        self.assertEqual(code, 5)                                   # safe: it went on and found no token

    def test_invalid_config_exits_4_and_lists_every_problem(self):
        self.world(config=False)
        self.write("config.json", json.dumps({**EXAMPLE, "max_sends_per_hour": 0, "sending": "yes"}))
        for command in (["check"], ["once"], ["send-one", "c1"]):
            with self.subTest(command=command):
                code, _, err = self.bot(*command)
                self.assertEqual(code, 4)
                self.assertIn("max_sends_per_hour", err)
                self.assertIn("sending", err)
        os.remove(os.path.join(self.dir, "config.json"))
        self.assertEqual(self.bot("check")[0], 4)                   # no config at all
        self.assertEqual(self.requests(), [])

    def test_run_with_an_invalid_config_tells_telegram_and_exits_4(self):
        self.world(config=False)
        self.write("config.json", "{ not json")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as systemd:
            systemd.bind(os.path.join(self.dir, "notify"))
            systemd.settimeout(0.5)
            code, _, _ = self.bot("run", env={"NOTIFY_SOCKET": os.path.join(self.dir, "notify")})
            self.assertEqual(code, 4)
            with self.assertRaises(TimeoutError):                   # never READY: systemd sees a failed start
                systemd.recv(100)
        self.assertIn("fatal_start", self.telegram())
        self.assertEqual([r["kind"] for r in self.requests()], ["telegram"])   # Meta was not called

    def test_run_that_cannot_tell_telegram_why_it_stays_down_exits_1_and_systemd_tries_again(self):
        # C03: codes 3, 4 and 6 keep the service down for good; they are final only once the owner has been told
        self.world(config=False)
        self.write("config.json", "{ not json")
        code, _, err = self.bot("run", env={"TG_API_BASE": "http://127.0.0.1:9"})
        self.assertEqual(code, 1, err)
        self.assertIn("Telegram did not confirm fatal_start", err)
        self.assertIn("config invalid", err)
        self.assertEqual(self.requests(), [])                       # nothing reached Meta or the fake Telegram
        self.assertEqual(self.bot("run")[0], 4)                     # Telegram is back: said, and now it is final
        self.assertIn("fatal_start", self.telegram())
        self.assertEqual(self.bot("once", env={"TG_API_BASE": "http://127.0.0.1:9"})[0], 4)   # a person at a terminal

    def test_no_database_or_no_token_exits_5(self):
        self.world(env_file=False)
        for command in (["report"], ["report", "--json"], ["media"], ["arm", "5"], ["arm", "all", "--deaf"], ["disarm"],
                        ["check"], ["check", "--online"]):
            with self.subTest(command=command):
                code, out, err = self.bot(*command)
                self.assertEqual(code, 5, err)
                self.assertEqual(out if command[0] != "check" else "", "")   # `check` first says the config is ok
        self.assertFalse(os.path.exists(os.path.join(self.dir, "core.db")))   # nothing created one on the way
        self.assertEqual(self.requests(), [])

    def test_media_without_a_stored_token_exits_5(self):
        self.world(env_file=False)
        self.assertEqual(self.bot("once")[0], 0)                    # the database exists, no token was ever adopted
        code, out, err = self.bot("media")
        self.assertEqual((code, out), (5, ""))
        self.assertIn("no token", err)
        self.assertEqual(self.requests(), [])

    def test_every_command_says_which_state_and_config_it_used(self):
        self.world()
        other = os.path.join(self.dir, "other.json")
        shutil.copy(os.path.join(self.dir, "config.json"), other)
        _, _, err = self.bot("check")
        self.assertIn(f"STATE_DIR={self.dir} CONFIG={self.dir}/config.json", err)
        code, _, err = self.bot("check", env={"CONFIG": other})
        self.assertEqual(code, 0)
        self.assertIn(f"CONFIG={other}", err)


class OneShot(CliCase):
    def test_check_validates_and_touches_nothing(self):
        self.world([comment("c1")])
        before = sorted(os.listdir(self.dir))
        code, out, _ = self.bot("check")
        self.assertEqual(code, 0)
        self.assertIn("config ok", out)
        self.assertIn(".env IG_ACCESS_TOKEN: set", out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(sorted(os.listdir(self.dir)), before)
        self.assertEqual(self.requests(), [])

    def test_check_online_calls_me_and_sends_one_telegram_message(self):
        self.world([comment("c1")])
        before = sorted(os.listdir(self.dir))
        code, out, _ = self.bot("check", "--online")
        self.assertEqual(code, 0, out)
        self.assertIn(ME["username"], out)
        self.assertIn("Telegram test message: delivered", out)
        self.assertEqual([r["kind"] for r in self.requests()], ["me", "telegram"])
        self.assertEqual(sorted(os.listdir(self.dir)), before)
        self.fake.control("/mode", {"mode": "token_dead"})
        code, out, _ = self.bot("check", "--online")
        self.assertEqual(code, 1)
        self.assertIn("global auth", out)
        self.fake.control("/mode", {"mode": "ok"})
        self.write(".env", f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN=123:abc\n")   # no chat id
        sent = len(self.requests("telegram"))
        code, out, _ = self.bot("check", "--online")
        self.assertEqual(code, 1)
        self.assertIn("Telegram test message: no bot token or no chat id", out)
        self.assertEqual(len(self.requests("telegram")), sent)      # nothing was posted for nobody

    def test_once_is_exactly_one_cycle(self):
        self.world([comment("c1")])
        code, _, err = self.bot("once")
        self.assertEqual(code, 0, err)
        self.assertEqual([r["kind"] for r in self.requests() if r["kind"] != "telegram"], ["me", "media", "comments"])
        self.assertEqual(self.status("c1"), ("pending", ""))        # a fresh database has budget 0
        self.assertIn("fresh_db", self.telegram())
        self.assertNotIn("started", self.telegram())                # that is the service's word
        self.assertNotIn(TOKEN, err)
        self.assertEqual(self.bot("arm", "1")[0], 0)
        code, _, err = self.bot("once")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.status("c1"), ("done", ""))
        self.assertIn("attempt id=1 comment=c1 kind=dm http=200 code= subcode= outcome=ok reason=", err)
        self.assertEqual(len(self.requests("media")), 2)            # each start begins with a full sweep
        self.assertEqual(self.dm_targets(), ["c1"])

    def test_the_digest_of_the_first_cycle_does_not_call_the_running_service_down(self):
        # a service first started after the digest hour sends its digest in its first cycle
        self.world([comment("c1")], alerts={"repeat_min": 120, "digest_hour_utc": 0})
        self.assertEqual(self.bot("once")[0], 0)
        told = self.telegram()
        self.assertIn("ℹ digest: WAITING: 1 чел. ждут ссылку", told)   # the verdict: one person, budget 0
        self.assertNotIn("not_running", told)

    def test_once_exits_1_when_the_cycle_failed(self):
        self.world([comment("c1")])
        self.assertEqual(self.bot("once")[0], 0)
        with sqlite3.connect(os.path.join(self.dir, "core.db")) as writer:
            writer.execute("UPDATE kv SET value='no time' WHERE key='last_now'")   # the clock guard cannot read it
        writer.close()
        code, _, err = self.bot("once")
        self.assertEqual(code, 1)
        self.assertIn("Traceback", err)
        self.assertIn("cycle_error", self.telegram())               # told all the same

    def test_send_one_sends_to_that_comment_only_and_every_gate_applies(self):
        self.world([comment("older", age=300), comment("wanted", age=200), comment("near", text="как арена")])
        code, out, _ = self.bot("send-one", "wanted")
        self.assertEqual((code, self.dm_targets()), (1, []))        # budget 0
        self.assertIn("wanted: pending", out)
        self.assertEqual(self.bot("arm", "5")[0], 0)
        code, out, _ = self.bot("send-one", "wanted")
        self.assertEqual(code, 0)
        self.assertIn("wanted: done", out)
        self.assertEqual(self.dm_targets(), ["wanted"])
        self.assertEqual(self.status("older"), ("pending", ""))
        for comment_id, said in (("near", "near: seen near:question"), ("wanted", "wanted: done"),
                                 ("nobody", "nobody: no such comment")):
            code, out, _ = self.bot("send-one", comment_id)
            self.assertEqual(code, 1)
            self.assertIn(said, out)
        self.assertEqual(self.dm_targets(), ["wanted"])
        self.assertEqual(store.budget(self.conn), 4)

    def test_report_and_media_read_only(self):
        self.world([comment("c1")])
        self.assertEqual(self.bot("once")[0], 0)
        database = pathlib.Path(self.dir, "core.db")
        plain = sqlite3.connect(database)
        plain.execute("PRAGMA journal_mode=DELETE").fetchone()      # an open that can write would make it WAL again
        plain.close()
        stored = database.read_bytes()
        code, out, _ = self.bot("report")
        self.assertEqual(code, 0)
        self.assertIn("not_running", out.splitlines()[0])           # the verdict comes first; nobody holds the lock
        code, out, _ = self.bot("report", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["unaccounted"], data["reels"][0]["people_waiting"]), (0, 1))
        self.assertNotIn(TOKEN, out)
        before = len(self.requests())
        code, out, _ = self.bot("media")
        self.assertEqual(code, 0)
        self.assertIn(f"{MEDIA} https://www.instagram.com/reel/DAbc/", out)
        self.assertEqual([r["kind"] for r in self.requests()][before:], ["my_media"])
        self.assertEqual(database.read_bytes(), stored)             # not one byte of the database was written

    def test_arm_refuses_when_no_alert_can_reach_a_human(self):
        self.world([comment("c1")])
        self.write(".env", f"IG_ACCESS_TOKEN={TOKEN}\n")            # no Telegram
        self.assertEqual(self.bot("once")[0], 0)
        code, out, err = self.bot("arm", "3")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not armed", err)
        self.assertEqual(store.budget(self.conn), 0)
        code, out, _ = self.bot("arm", "3", "--deaf")
        self.assertEqual(code, 0)
        self.assertEqual((store.budget(self.conn), self.kv("armed_n")), (3, "3"))
        self.assertIn("people waiting: 1", out)
        self.assertIn("would expire before their turn: 0", out)
        self.assertEqual(self.bot("arm", "all", "--deaf")[0], 0)
        self.assertEqual(store.budget(self.conn), "unlimited")
        self.assertEqual(self.bot("disarm")[0], 0)                  # the brake needs no alert channel and no config
        self.assertEqual(store.budget(self.conn), 0)
        outbox = [row[0] for row in self.conn.execute("SELECT key FROM outbox WHERE key IN ('armed', 'disarmed') ORDER BY id")]
        self.assertEqual(outbox, ["armed", "armed", "disarmed"])

    def test_a_config_edit_is_named_by_report_and_arm_and_disarms_at_the_next_start(self):
        """SPEC 4: every config change needs a human `arm`."""
        self.world([comment("c1")])
        self.assertEqual(self.bot("once")[0], 0)
        self.assertEqual(self.bot("arm", "5")[0], 0)
        self.write_config(max_sends_per_hour=120)                   # edited, and no start has loaded it yet
        code, out, _ = self.bot("arm", "5")
        self.assertEqual(code, 0)
        self.assertIn("config.json was changed after the service loaded it", out)
        self.assertIn("restart_needed", self.bot("report")[1])
        self.assertEqual(self.bot("once")[0], 0)                    # the start with the edited config disarms
        self.assertEqual((store.budget(self.conn), self.dm_targets()), (0, []))
        self.assertEqual(self.telegram().count("ℹ config_changed: the config changed: sending is disarmed"), 2)
        self.assertNotIn("restart_needed", self.bot("report")[1])
        code, out, _ = self.bot("arm", "5")
        self.assertNotIn("was changed", out)
        self.assertEqual(self.bot("once")[0], 0)                    # the same config again: the arm stands
        self.assertEqual((store.budget(self.conn), self.dm_targets()), (4, ["c1"]))

    def test_arm_counts_who_would_expire_before_their_turn(self):
        week = 7 * 86400   # real time here: the margins are seconds. The window closes 10 min before the week ends
        again = {**comment("again"), "from": comment("fresh")["from"]}   # a second comment of one person: no turn
        self.world([comment("fresh"), again, comment("soon", age=week - 690), comment("sooner", age=week - 660)],
                   scale=1, max_sends_per_hour=30)                  # a slot of 2 minutes
        self.assertEqual(self.bot("once")[0], 0)
        code, out, _ = self.bot("arm", "10")
        self.assertEqual(code, 0)
        self.assertIn("people waiting: 3; hours needed at 30 per hour: 0.1; would expire before their turn: 1", out)


class Service(CliCase):
    def started(self, comments, **config_changes):
        self.world(comments, **config_changes)
        process = self.service()
        self.wait("the first cycle", lambda: self.kv("last_cycle_ok_at"))
        return process

    def test_lock_is_held_by_the_service_and_released_at_its_end(self):
        process = self.started([comment("c1")])
        for command in (["run"], ["once"], ["send-one", "c1"]):
            with self.subTest(command=command):
                before = len(self.requests("me", "media"))
                code, out, err = self.bot(*command)
                self.assertEqual(code, 3, err)
                self.assertIn("core.lock", err)
                self.assertEqual(len(self.requests("me", "media")), before)   # it did not start anything
        self.assertEqual(self.bot("report")[0], 0)                  # reading needs no lock
        self.assertIsNone(process.poll())                           # and none of this disturbed the service
        self.assertEqual(self.stop(process), 0)
        self.assertEqual(self.bot("once")[0], 0)

    def test_a_report_that_tests_the_lock_does_not_turn_a_starting_bot_away(self):
        # report.py finds out whether a bot runs by holding core.lock shared for an instant; exit 3 at that
        # instant would be final, because systemd does not restart after it
        self.world([comment("c1")])
        with open(os.path.join(self.dir, "core.lock"), "a") as probe:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
            process = self.service("once")
            log = pathlib.Path(process.log.name)
            self.wait("the bot to reach the lock", lambda: "STATE_DIR=" in log.read_text("utf-8"))
            time.sleep(0.02)   # it prints that line just before it tries the lock for the first time
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertEqual(self.status("c1"), ("pending", ""))        # it ran its cycle

    def test_operator_commands_take_effect_while_the_service_runs(self):
        process = self.started([comment("c1", age=300), comment("c2", age=200), comment("c3", age=100)])
        self.wait("three pending comments", lambda: self.status("c3") == ("pending", ""))
        self.cycles()
        self.assertEqual(self.dm_targets(), [])                     # budget 0

        code, out, _ = self.bot("arm", "1")
        self.assertEqual(code, 0)
        self.assertIn("people waiting: 3", out)
        self.wait("the one armed DM", lambda: self.status("c1") == ("done", ""))
        self.cycles()
        self.assertEqual(self.dm_targets(), ["c1"])                 # the budget was 1
        self.assertEqual(store.budget(self.conn), 0)

        self.assertEqual(self.bot("arm", "all")[0], 0)
        self.wait("the next DMs", lambda: self.status("c3") == ("done", ""))
        self.assertEqual(self.dm_targets(), ["c1", "c2", "c3"])

        self.assertEqual(self.bot("disarm")[0], 0)
        self.fake.control("/comments", {"add": {"media_id": MEDIA, **comment("c4", age=0)}})
        self.wait("the new comment", lambda: self.status("c4") == ("pending", ""))
        self.cycles()
        self.assertEqual(self.dm_targets(), ["c1", "c2", "c3"])     # the brake holds
        told = self.telegram()
        for event in ("started", "armed", "stage_done", "disarmed"):
            self.assertIn(f"ℹ {event}", told)
        self.assertEqual(self.stop(process), 0)
        self.assertEqual(self.status("c4"), ("pending", ""))

    def test_a_lasting_problem_is_repeated_as_often_as_the_config_says(self):
        self.world(alerts={"repeat_min": 1, "digest_hour_utc": 6})   # a scaled minute is 0.1 s
        self.fake.control("/mode", {"mode": "blocked"})
        self.service()
        self.wait("the third announcement", lambda: self.telegram().count("⚠ hold:account") >= 3)

    def ends_cleanly(self, signum):
        process = self.started([comment(f"c{n}", age=100 - n) for n in range(30)])
        self.assertEqual(self.bot("arm", "all")[0], 0)
        self.wait("DMs to flow", lambda: len(self.dm_targets()) >= 3)
        began = time.monotonic()
        process.send_signal(signum)
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertLess(time.monotonic() - began, 5)
        log = pathlib.Path(process.log.name).read_text("utf-8")
        self.assertIn("INFO stopped", log)
        self.assertNotIn("Traceback", log)
        # the call in flight and its write were finished: nothing is left open, nobody was sent to twice
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attempts WHERE finished_at IS NULL").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM comments WHERE status='sending'").fetchone()[0], 0)
        sent = self.dm_targets()
        self.assertEqual(len(sent), len(set(sent)))
        self.assertLess(len(sent), 30)

    def test_sigterm_ends_the_loop_with_exit_0(self):
        self.ends_cleanly(signal.SIGTERM)

    def test_sigint_ends_the_loop_with_exit_0(self):
        self.ends_cleanly(signal.SIGINT)

    def test_systemd_gets_ready_after_the_lock_and_then_watchdog_pings(self):
        self.world([comment("c1")])
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as systemd:
            systemd.bind(os.path.join(self.dir, "notify"))
            systemd.settimeout(20)
            self.fake.control("/hold", {"add": {"kind": "me", "phase": "before"}})   # the first API call waits
            process = self.service(env={"NOTIFY_SOCKET": os.path.join(self.dir, "notify")})
            self.assertEqual(systemd.recv(100), b"READY=1")         # sent although no API call has returned
            self.assertTrue(os.path.exists(os.path.join(self.dir, "core.lock")))
            self.assertEqual(self.kv("fresh_db"), "1")              # the schema is there
            self.fake.control("/hold", {"release": "h1"})
            self.assertEqual({systemd.recv(100) for _ in range(20)}, {b"WATCHDOG=1"})
            self.assertEqual(self.stop(process), 0)


if __name__ == "__main__":
    unittest.main()
