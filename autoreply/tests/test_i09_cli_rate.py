"""I09: real one-shot CLI admission, cancellation and restart at a loopback fake.

Only the HTTP peer and durable historical fixtures are synthetic. Each sender and
operator command is an unmodified bot.py subprocess using the documented scaled
clock, a private config/.env and a real SQLite database. No Engine mocks.
"""
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone

import clock
import store
from sim.fake_meta import FakeMeta


CODE = pathlib.Path(__file__).resolve().parents[1]
BOT = pathlib.Path(os.environ.get("I09_CODE", str(CODE))) / "bot.py"
MEDIA = "17900000000000101"
ME = {"user_id": "17841400000000001", "id": "9990000000000001",
      "username": "i09_synthetic", "followers_count": 12, "media_count": 1}
TOKEN = "IGAAi09syntheticonly"
EXAMPLE = json.loads((CODE / "config.example.json").read_text(encoding="utf-8"))


def listed(comment_id):
    return {"id": comment_id, "text": "арена", "age_sec": 60,
            "from": {"id": "u_" + comment_id, "username": "p_" + comment_id},
            "should_match": True}


def stamp(seconds):
    return clock.iso(datetime.fromtimestamp(seconds, timezone.utc))


class CliRate(unittest.TestCase):
    def setUp(self):
        self.folder = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory(prefix="i09-cli-")))
        self.processes = []
        self.events = []
        self.fake = None
        self.env = {key: value for key, value in os.environ.items() if key not in
                    ("STATE_DIR", "CONFIG", "NOTIFY_SOCKET", "CLOCK_SCALE", "CLOCK_ANCHOR",
                     "IG_API_BASE", "TG_API_BASE")}
        self.env.update(STATE_DIR=str(self.folder), PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.out.close()
            process.err.close()
        if self.fake:
            truth = self.fake.control("/truth")
            self.assertEqual([r["strict"] for r in truth["requests"] if r["strict"]], [])
            self.events.append({"type": "final", "dm_targets": [r["target"] for r in truth["requests"]
                                                                  if r["kind"] == "dm"],
                                "attempts": self.attempts()})
        evidence = os.environ.get("I09_EVIDENCE_DIR")
        if evidence:
            path = pathlib.Path(evidence)
            path.mkdir(parents=True, exist_ok=True)
            name = self.id().rsplit(".", 1)[-1]
            (path / (name + ".json")).write_text(json.dumps(self.events, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
        print("I09_EVIDENCE " + json.dumps({"case": self.id(), "events": self.events}, ensure_ascii=False),
              flush=True)

    def world(self, comments=("new",), cap=2, scale=600):
        self.scale, self.anchor = scale, time.time()
        self.fake = FakeMeta({"account": ME, "token": TOKEN, "media": [{"id": MEDIA,
            "permalink": "https://www.instagram.com/reel/I09/", "shortcode": "I09",
            "comments": [listed(c) for c in comments]}]}, scale=scale, anchor=self.anchor).start()
        self.addCleanup(self.fake.stop)
        self.env.update(IG_API_BASE=self.fake.api_base, TG_API_BASE=self.fake.api_base,
                        CLOCK_SCALE=str(scale), CLOCK_ANCHOR=repr(self.anchor))
        cfg = {**EXAMPLE, "sending": True, "public_reply": False, "poll_interval_sec": 30,
               "max_sends_per_hour": cap}
        (self.folder / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (self.folder / ".env").write_text(f"IG_ACCESS_TOKEN={TOKEN}\nTG_BOT_TOKEN=123:i09\nTG_CHAT_ID=42\n",
                                          encoding="utf-8")
        self.assertEqual(self.bot("once").returncode, 0)  # adopts config/token and collects with budget zero
        self.assertEqual(self.targets(), [])

    def bot(self, *args, timeout=3, env=None):
        before = time.monotonic()
        done = subprocess.run([sys.executable, "-B", str(BOT), *args],
                              env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=timeout)
        self.events.append({"type": "command", "args": list(args), "rc": done.returncode,
                            "real_elapsed": time.monotonic() - before,
                            "stdout": done.stdout, "stderr": done.stderr})
        return done

    def start(self, *args, env=None):
        number = len(self.processes)
        out = (self.folder / f"p{number}.out").open("w+", encoding="utf-8")
        err = (self.folder / f"p{number}.err").open("w+", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-B", str(BOT), *args],
                                   env={**self.env, **(env or {})}, stdout=out, stderr=err)
        process.out, process.err, process.started = out, err, time.monotonic()
        self.processes.append(process)
        return process

    def finish(self, process, timeout=9):
        rc = process.wait(timeout=timeout)
        process.out.flush()
        process.err.flush()
        self.events.append({"type": "sender", "rc": rc, "real_elapsed": time.monotonic() - process.started,
                            "stdout": pathlib.Path(process.out.name).read_text(encoding="utf-8"),
                            "stderr": pathlib.Path(process.err.name).read_text(encoding="utf-8")})
        return rc

    def wait(self, check, seconds=2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return
            time.sleep(0.01)
        self.fail("bounded observation timed out")

    def rows(self, query, values=()):
        with closing(store.connect(str(self.folder / "core.db"), readonly=True)) as conn:
            return [dict(row) for row in conn.execute(query, values)]

    def attempts(self):
        return self.rows("SELECT id,comment_id,kind,started_at,finished_at,outcome,http,reason FROM attempts")

    def targets(self):
        return [r["target"] for r in self.fake.control("/truth")["requests"] if r["kind"] == "dm"]

    def seed_completed(self, age=0, start_age=None, count=1, offset=0):
        now = self.fake.now()
        with closing(store.connect(str(self.folder / "core.db"))) as conn:
            with store.tx(conn):
                for number in range(count):
                    end = now - age + offset
                    start = now - (age if start_age is None else start_age) + offset
                    conn.execute("INSERT INTO attempts (comment_id,kind,started_at,finished_at,outcome,http,reason) "
                                 "VALUES (?,'dm',?,?,'ok',200,'')", (f"historic-{number}", stamp(start), stamp(end)))
        self.events.append({"type": "history", "count": count, "age": age, "start_age": start_age,
                            "journal_offset": offset, "fake_now": now})

    def assert_waiting(self, process, baseline):
        # Wait for the sender's new collect, then observe it still alive with no committed claim.
        self.wait(lambda: " sweep " in pathlib.Path(process.err.name).read_text(encoding="utf-8")
                  or process.poll() is not None)
        time.sleep(0.12)
        self.assertIsNone(process.poll(), "one-shot returned instead of waiting for the startup slot")
        self.assertEqual(len(self.attempts()), baseline)
        self.assertEqual(self.targets(), [])

    def test_once_waits_for_current_slot_then_delivers_with_capacity(self):
        self.world(cap=2)
        self.seed_completed()
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        process = self.start("once")
        self.assert_waiting(process, 1)
        self.assertEqual(self.finish(process), 0)
        self.assertEqual(self.targets(), ["new"])
        self.assertGreaterEqual(time.monotonic() - process.started, 1800 / self.scale)
        self.assertEqual(len(self.attempts()), 2)

    def test_send_one_waits_then_delivers_only_named_comment(self):
        self.world(comments=("new", "other"), cap=2)
        self.seed_completed()
        self.assertEqual(self.bot("arm", "all").returncode, 0)
        process = self.start("send-one", "new")
        self.assert_waiting(process, 1)
        self.assertEqual(self.finish(process), 0)
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(self.rows("SELECT status FROM comments WHERE comment_id='other'")[0]["status"], "pending")

    def test_once_sigterm_cancels_cap_one_wait_without_new_claim(self):
        self.cancel_wait("once")

    def test_send_one_sigterm_cancels_cap_one_wait_with_exit_zero(self):
        self.cancel_wait("send-one", "new")

    def cancel_wait(self, *args):
        self.world(cap=1)
        self.seed_completed()
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as watchdog:
            path = str(self.folder / "watchdog")
            watchdog.bind(path)
            watchdog.settimeout(1)
            process = self.start(*args, env={"NOTIFY_SOCKET": path})
            self.assert_waiting(process, 1)
            notices = [watchdog.recv(100), watchdog.recv(100)]
            self.assertIn(b"WATCHDOG=1", notices)
            sent_term = time.monotonic()
            process.send_signal(signal.SIGTERM)
            self.assertEqual(self.finish(process, timeout=2), 0)
            self.assertLess(time.monotonic() - sent_term, 1.5)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.targets(), [])
        self.assertEqual(self.rows("SELECT status FROM comments WHERE comment_id='new'")[0]["status"], "pending")

    def test_disarm_during_wait_prevents_claim_and_preserves_pending(self):
        self.world(cap=2)
        self.seed_completed()
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        process = self.start("once")
        self.assert_waiting(process, 1)
        self.assertEqual(self.bot("disarm").returncode, 0)
        self.assertEqual(self.finish(process), 0)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.targets(), [])
        self.assertEqual(self.rows("SELECT status FROM comments WHERE comment_id='new'")[0]["status"], "pending")

    def test_arm_during_wait_replaces_budget_used_by_next_claim(self):
        self.world(cap=2)
        self.seed_completed()
        self.assertEqual(self.bot("arm", "all").returncode, 0)
        process = self.start("once")
        self.assert_waiting(process, 1)
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        self.assertEqual(self.finish(process), 0)
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(self.rows("SELECT value FROM kv WHERE key='send_budget'")[0]["value"], "0")

    def test_no_budget_avoids_startup_sleep(self):
        self.world(cap=1)
        self.seed_completed()
        self.assertEqual(self.bot("once", timeout=2).returncode, 0)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.targets(), [])

    def test_no_pending_avoids_startup_sleep(self):
        self.world(comments=(), cap=1)
        self.seed_completed()
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        self.assertEqual(self.bot("once", timeout=2).returncode, 0)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.targets(), [])

    def test_no_identity_avoids_startup_sleep_and_claim(self):
        self.world(cap=1)
        self.seed_completed()
        with closing(store.connect(str(self.folder / "core.db"))) as conn:
            with store.tx(conn):
                conn.execute("DELETE FROM kv WHERE key IN ('me_user_id','me_id','me_username')")
        self.fake.control("/faults", {"add": {"kind": "me", "body": {"id": ME["id"]}}})
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        self.assertEqual(self.bot("once", timeout=2).returncode, 0)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.targets(), [])

    def test_days_old_completed_history_does_not_force_another_full_slot(self):
        self.world(cap=1, scale=100)
        self.seed_completed(age=2 * 86400)
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        self.assertEqual(self.bot("once", timeout=2).returncode, 0)
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(len(self.attempts()), 2)

    def test_empty_history_has_no_startup_delay(self):
        self.world(cap=1, scale=100)
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        self.assertEqual(self.bot("send-one", "new", timeout=2).returncode, 0)
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(len(self.attempts()), 1)

    def test_unknown_old_history_keeps_practical_deadline_guard_without_retry(self):
        self.world(cap=2)
        old = stamp(self.fake.now() - 2 * 86400)
        with closing(store.connect(str(self.folder / "core.db"))) as conn:
            with store.tx(conn):
                conn.execute("INSERT INTO comments (comment_id,media_id,user_id,username,text,ts,status,reason,"
                             "first_seen_at,updated_at) VALUES ('unknown-old',?,'old-user','old-name','арена',?,"
                             "'uncertain','no_response',?,?)", (MEDIA, old, old, old))
                conn.execute("INSERT INTO attempts (comment_id,kind,started_at,finished_at,outcome,http,reason) "
                             "VALUES ('unknown-old','dm',?,?,'uncertain',0,'no_response')", (old, old))
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        process = self.start("once")
        self.assert_waiting(process, 1)
        self.assertEqual(self.finish(process), 0)
        self.assertGreaterEqual(time.monotonic() - process.started, (1800 + 60) / self.scale)
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(self.rows("SELECT status FROM comments WHERE comment_id='unknown-old'")[0]["status"],
                         "uncertain")
        self.assertEqual([r["comment_id"] for r in self.attempts()], ["unknown-old", "new"])
        line = next(line for line in pathlib.Path(process.err.name).read_text(encoding="utf-8").splitlines()
                    if " INFO rate_wait " in line)
        wait_seconds = float(next(word.split("=", 1)[1] for word in line.split() if word.startswith("seconds=")))
        self.assertGreater(wait_seconds, 1800)  # boot slot alone would be shorter than 1800 after collect
        slept = (clock.parse(self.attempts()[1]["started_at"]) - clock.parse(line.split()[0])).total_seconds()
        self.assertGreaterEqual(slept, wait_seconds - 0.002)  # durable claim follows the announced real sleep

    def test_recent_cap_one_history_blocks_then_later_restart_delivers_promptly(self):
        self.world(cap=1)
        self.seed_completed()
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        process = self.start("once")
        self.assert_waiting(process, 1)
        self.assertEqual(self.finish(process), 0)
        self.assertEqual(self.targets(), [])  # full slot is free, but conservative cap is still closed
        old = clock.parse(self.attempts()[0]["finished_at"]).timestamp()
        self.wait(lambda: self.fake.now() > old + 4260.001, seconds=2)
        self.assertEqual(self.bot("once", timeout=2).returncode, 0)
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(len(self.attempts()), 2)

    def offset_env(self, offset):
        # Documented CLOCK_ANCHOR alone shifts a scaled wall clock by this offset,
        # without changing monotonic or the fake receiver's independent clock.
        return {"CLOCK_ANCHOR": repr(self.anchor - offset / (self.scale - 1))}

    def test_actual_restart_from_minus_300_to_plus_300_preserves_receiver_spacing(self):
        self.world(comments=("old",), cap=20, scale=100)
        self.assertEqual(self.bot("arm", "all").returncode, 0)
        self.assertEqual(self.bot("send-one", "old", env=self.offset_env(-300)).returncode, 0)
        self.fake.control("/comments", {"add": {"media_id": MEDIA, **listed("new")}})
        process = self.start("send-one", "new", env=self.offset_env(300))
        self.wait(lambda: process.poll() is not None or " sweep " in
                  pathlib.Path(process.err.name).read_text(encoding="utf-8"))
        self.assertEqual(self.finish(process), 0)
        receives = [r["t"] for r in self.fake.control("/truth")["requests"] if r["kind"] == "dm"]
        self.events.append({"type": "receiver_spacing", "receives": receives,
                            "gap": receives[1] - receives[0], "required": 180})
        self.assertEqual(self.targets(), ["old", "new"])
        self.assertGreaterEqual(receives[1] - receives[0], 180)

    def test_completed_response_end_keeps_cap_closed_when_claim_start_is_old(self):
        # This no-wait boundary needs real time: scale100 would consume its 59s
        # margin in 0.59s under concurrent full-suite load, naturally opening cap.
        self.world(cap=1, scale=1)
        # Start is outside 3600+660.001, completed response is inside it.
        # Its true age exceeds the startup slot, so only the cap can block this.
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        self.seed_completed(age=4201, start_age=4260.5)
        result = self.bot("send-one", "new", timeout=2)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.targets(), [])
        self.assertEqual(len(self.attempts()), 1)

    def test_cap_decrease_and_allowed_offset_keep_all_completed_history_counted(self):
        env = self.decreased_cap_world(60)
        self.assertEqual(self.bot("send-one", "new", env=env).returncode, 1)
        self.assertEqual(self.targets(), [])
        self.assertEqual(len(self.attempts()), 60)

    def test_cap_decrease_with_59_history_rows_permits_the_60th_attempt(self):
        env = self.decreased_cap_world(59)
        self.assertEqual(self.bot("send-one", "new", env=env).returncode, 0)
        self.assertEqual(self.targets(), ["new"])
        self.assertEqual(len(self.attempts()), 60)

    def decreased_cap_world(self, count):
        self.world(cap=300, scale=100)
        # Completed old calls, true receipt ages3500..2792 for sixty rows.
        # All are still in the receiver's hour; old journal offset is -300.
        now = self.fake.now()
        with closing(store.connect(str(self.folder / "core.db"))) as conn:
            with store.tx(conn):
                for number in range(count):
                    age = 3500 - number * 12
                    conn.execute("INSERT INTO attempts (comment_id,kind,started_at,finished_at,outcome,http,reason) "
                                 "VALUES (?,'dm',?,?,'ok',200,'')", (f"historic-{number}",
                                  stamp(now - age - 300), stamp(now - age - 300)))
        cfg_path = self.folder / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["max_sends_per_hour"] = 60
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        env = self.offset_env(300)
        self.assertEqual(self.bot("once", env=env).returncode, 0)  # config change disarms before any claim
        self.assertEqual(self.bot("arm", "1", env=env).returncode, 0)  # explicit re-arm after restart
        self.events.append({"type": "cap_boundary", "count": count, "old_cap": 300, "new_cap": 60,
                            "old_offset": -300, "new_offset": 300})
        return env
