#!/usr/bin/env python3
"""Black-box soak (SPEC.md section 11): the REAL bot (`python3 bot.py run`, a subprocess with its own
STATE_DIR in a temp directory) against sim/fake_meta.py on a scaled clock, then the invariants I1..I12.

    python3 sim/soak.py --seeds N [--start S] [--keep-failed DIR] [--scale X] [--only NAME[,NAME…]]

--seeds N        run the random scenarios of seeds S .. S+N-1 (sim/scenarios.py: build(seed)); S defaults to 0
--only NAME      run named scenarios instead (`--only list` prints the names, `--only named` runs them all)
--scale X        clock speed-up, instead of the one each scenario was written for
--keep-failed    where the run directory of a failing scenario is kept (default: where it was made, under
                 the system temp directory); replay it with `python3 sim/invariants.py RUN_DIR`
--bot PATH       the bot's entry point (default: ../bot.py)

One line per scenario (name, PASS/FAIL, real seconds, counts), the violations of a failing one, a summary.
Exit code 0 if all passed, 1 if one failed, 2 for a usage error. Everything stays on 127.0.0.1.

Besides I1..I12 the soak reports, as "RUN", what only the one who runs the process can see: exit codes
(SIGTERM 0, second runner 3, `arm`), a process that exits by itself.
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import invariants, scenarios
else:
    import invariants
    import scenarios

HERE = Path(__file__).resolve().parent
POLL = 0.02            # real seconds between two looks at the fake or the database
SLACK = 1.5            # real seconds added to every scaled wait: process start-up is not scaled
START_UP = 2           # real seconds after which a silent bot is taken to be past its start-up
STOP_TIMEOUT = 120      # real seconds a SIGTERM may take (the bot finishes its HTTP call and its write)
CLI_TIMEOUT = 30       # real seconds for arm, disarm, report, once
KILL_TIMEOUT = 2 * 3600    # scaled seconds to wait for the request a kill is to be placed on
UNFINISHED = ("pending", "sending", "sent")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))    # loopback only, never a proxy


class Run:
    """One scenario from start to verdict. `dir` gets state/ (the bot's STATE_DIR), bot.log, fake.log and,
    at the end, scenario.json, truth.json and events.json."""

    def __init__(self, scenario, bot, directory, scale=None):
        self.scenario = json.loads(json.dumps(scenario))    # a private copy, and proof that it is plain JSON
        self.scale = float(scale or scenario["scale"])
        self.bot, self.dir, self.state = str(bot), Path(directory), Path(directory) / "state"
        self.events, self.problems, self.unplaced = [], [], 0
        self.coverage, self.required_faults, self.step_index = [], [], -1
        self.fired, self.fired_upto = set(), 0    # ids of the faults that have fired, and the last seq looked at
        self.proc = self.fake = None

    # ---- clock, fake, database

    def now(self):
        return self.anchor + (time.time() - self.anchor) * self.scale

    def ctl(self, path, body=None, timeout=30):
        request = urllib.request.Request(f"http://127.0.0.1:{self.control_port}{path}",
                                         data=None if body is None else json.dumps(body).encode(),
                                         method="GET" if body is None else "POST")
        try:
            with _opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as answer:
            raise RuntimeError(f"fake control {path} {body}: {answer.read().decode()}") from None

    def seq(self):
        return self.ctl("/truth?since=999999999")["seq"]

    def requests_since(self, seq):
        return self.ctl(f"/truth?since={seq}")["requests"]

    def fired_faults(self):
        new = self.requests_since(self.fired_upto)
        self.fired.update(request["fault_id"] for request in new)
        self.fired_upto += len(new)
        return self.fired

    def log(self, op, **more):
        seq = self.seq()
        real, mono = time.time(), time.monotonic()
        event = {"op": op, "seq": seq, "t": self.anchor + (real - self.anchor) * self.scale,
                 "monotonic": mono, **more}
        self.events.append(event)
        return event

    def save_result(self, completed=False):
        result = {"version": 1, "completed": completed, "coverage": self.coverage,
                  "problems": [problem._asdict() for problem in self.problems]}
        temporary = self.dir / "run-result.tmp"
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.dir / "run-result.json")

    def problem(self, subject, evidence):
        self.problems.append(invariants.Violation("RUN", subject, evidence))
        self.save_result()

    def obligation(self, step, kind, reached, **details):
        required = step.get("required", True)
        reason = step.get("reason", "")
        if not required and not reason:
            self.problem(kind, "optional obligation requires a reason")
        entry = {"step": self.step_index, "kind": kind, "required": required, "reason": reason,
                 "outcome": "met" if reached else "missed", **details}
        self.coverage.append(entry)
        self.save_result()
        if required and not reached:
            self.problem(f"step {self.step_index} {kind}", json.dumps(details, sort_keys=True))

    def check_faults(self):
        counts = {}
        for request in self.requests_since(0):
            fault = request.get("fault_id")
            counts[fault] = counts.get(fault, 0) + 1
        for spec in self.required_faults:
            hits = counts.get(spec["id"], 0)
            self.obligation(spec, "fault", hits >= spec.get("min_fires", 1),
                            id=spec["id"], installed_step=spec["step"], hits=hits,
                            min_fires=spec.get("min_fires", 1))
        self.required_faults.clear()

    def snapshot(self):
        keys = ("me_user_id", "me_id", "me_username", "armed_at", "armed_n", "send_budget",
                "armed_through_attempt_id")
        kv = dict(self.query("SELECT key, value FROM kv WHERE key IN (%s)" % ",".join("?" * len(keys)), keys))
        return {"kv": kv, "known_comments": [row[0] for row in self.query("SELECT comment_id FROM comments")],
                "max_dm_attempt_id": (self.query("SELECT COALESCE(MAX(id),0) FROM attempts WHERE kind='dm'")
                                      or [(0,)])[0][0]}

    def operator_done(self, event):
        snapshot = self.snapshot()
        event.update(seq_after=self.seq(), t_after=self.now(), snapshot_after=snapshot)
        marker = snapshot["kv"].get("armed_through_attempt_id")
        if marker is not None:
            event["boundary_id"] = int(marker)

    def query(self, sql, args=()):
        """Rows from the bot's database (read-only); nothing while there is no database or no such table."""
        path = self.state / "core.db"
        if not path.exists():
            return []
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    # ---- the bot process

    def bot_env(self):
        api = f"http://127.0.0.1:{self.api_port}"
        return {"PATH": os.environ.get("PATH", ""), "STATE_DIR": str(self.state), "IG_API_BASE": api,
                "TG_API_BASE": api, "CLOCK_SCALE": repr(self.scale), "CLOCK_ANCHOR": repr(self.anchor),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}

    def command(self, *args):
        return [sys.executable, "-B", self.bot, *args]

    def cli(self, *args):
        """Run one bot command to its end -> (exit code, stdout). A command that hangs is killed: code None."""
        with open(self.dir / "bot.log", "ab") as log:
            try:
                done = subprocess.run(self.command(*args), env=self.bot_env(), cwd=os.path.dirname(self.bot),
                                      stdout=subprocess.PIPE, stderr=log, timeout=CLI_TIMEOUT)
            except subprocess.TimeoutExpired:
                return None, ""
        return done.returncode, done.stdout.decode("utf-8", "replace")

    def start(self, step=None):
        if self.proc is None:
            # The snapshot and cut precede Popen: a fast child must not escape its process boundary.
            event = self.log("start", snapshot=self.snapshot(), **_alert(step))
            with open(self.dir / "bot.log", "ab") as log:
                self.proc = subprocess.Popen(self.command("run"), env=self.bot_env(), cwd=os.path.dirname(self.bot),
                                             stdout=log, stderr=log)
            event["pid"] = self.proc.pid
            self.started = event["seq"], time.monotonic()

    def up(self):
        """Wait until the bot is past its start-up: it has made a request (the lock is taken before any API
        call, SPEC section 3), or START_UP seconds have passed for a bot that has nothing to ask. A signal or a
        second runner before that would test the Python interpreter, not the bot."""
        seq, since = self.started
        while self.proc is not None and self.seq() == seq and time.monotonic() - since < START_UP:
            time.sleep(POLL)

    def stop(self, sig, how):
        """Signal the bot and wait until it is gone -> exit code (None: there was no process)."""
        if self.proc is None:
            return None
        self.proc.send_signal(sig)
        try:
            code = self.proc.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.problem("SIGTERM", f"the bot was still running {STOP_TIMEOUT} real seconds after the signal")
            self.proc.kill()
            code = self.proc.wait()
        self.proc = None
        self.log("stop", how=how, rc=code)
        return code

    def term(self, step=None):
        self.up()
        code = self.stop(signal.SIGTERM, "term")
        if code not in (None, 0):
            self.problem("SIGTERM", f"exit code {code}, SPEC section 3 says 0")

    def watch(self):
        """The loop never exits on its own (SPEC 8.1)."""
        if self.proc is not None and self.proc.poll() is not None:
            self.problem("process", f"`bot.py run` exited by itself with code {self.proc.returncode}")
            self.proc = None
            self.log("stop", how="died")

    def wait_for(self, done, scaled_timeout, slack=SLACK):
        """Poll until done() or the timeout (scaled seconds, plus the slack) -> whether it was done."""
        end = time.monotonic() + scaled_timeout / self.scale + slack
        while not done():
            if time.monotonic() >= end:
                return False
            self.watch()
            time.sleep(POLL)
        return True

    # ---- the steps of a scenario (scenarios.py: STEPS)

    def second(self, step):
        self.up()
        code, _ = self.cli("once")
        self.log("second", rc=code)
        if code != 3:
            self.problem("second runner", f"`bot.py once` beside a running bot: exit code {code}, must be 3")

    def kill(self, step):
        trigger = {key: step[key] for key in ("kind", "phase", "target") if key in step}
        hold = self.ctl("/hold", {"add": trigger})["id"]
        if "arm" in step:    # only now: the DM to be caught may leave the moment the budget is there
            self.arm({"n": step["arm"], "deaf": step.get("deaf")})
        real = step.get("timeout", KILL_TIMEOUT) / self.scale + SLACK
        caught = self.ctl("/hold", {"wait": hold, "timeout_real_sec": real}, timeout=real + 30)
        if not caught["caught"] or self.proc is None:
            self.ctl("/hold", {"release": hold})
            self.unplaced += 1
            self.log("unplaced", **trigger)
            self.obligation(step, "kill", False, trigger=trigger, timeout=real)
            return
        self.obligation(step, "kill", True, trigger=trigger, held=caught["seq"])
        self.proc.kill()
        self.proc.wait()
        self.proc = None
        self.log("kill", held=caught["seq"], **trigger)
        self.log("stop", how="kill")
        if not step.get("vanish"):
            self.ctl("/hold", {"release": hold})    # Meta had the request: it is processed, the answer is lost

    def arm(self, step):
        n = step["n"]
        if n == "waiting":    # what an operator reads off the report: everybody who waits, and two to spare
            stored = dict(self.query("SELECT comment_id, status FROM comments"))
            n = 2 + sum(stored.get(c["id"], "pending") == "pending" for c in self.ctl("/comments")["comments"]
                        if c["listed"] and not c["deleted"])
        event = self.log("arm", n=n, snapshot_before=self.snapshot(), **_alert(step))    # logged first: its seq is "before the command"
        event["rc"], _ = self.cli("arm", str(n), *(["--deaf"] if step.get("deaf") else []))
        self.operator_done(event)
        if event["rc"] != step.get("rc", 0):
            self.problem("arm", f"`bot.py arm {n}`: exit code {event['rc']}, expected {step.get('rc', 0)}")

    def disarm(self, step):
        event = self.log("disarm", snapshot_before=self.snapshot())
        code, _ = self.cli("disarm")
        event["rc"] = code
        self.operator_done(event)
        if code != 0:
            self.problem("disarm", f"`bot.py disarm`: exit code {code}")

    def mode(self, step):
        self.ctl("/mode", {"mode": step["mode"]})
        self.log("mode", mode=step["mode"], **_alert(step))

    def faults(self, step):
        specs = []
        for fault in step["add"]:
            obligation = {"id": fault["id"], "step": self.step_index,
                          **{key: fault.get(key, step.get(key, default)) for key, default in
                             (("required", True), ("reason", ""), ("min_fires", 1))}}
            self.required_faults.append(obligation)
            specs.append({key: value for key, value in fault.items()
                          if key not in ("required", "reason", "min_fires")})
        self.ctl("/faults", {"add": specs})

    def comment(self, step):
        self.ctl("/comments", {key: step[key] for key in ("add", "delete", "hide", "owner_reply") if key in step})

    def env(self, step):
        self.write_env(step["env"])
        self.log("env", env=step["env"], **_alert(step))

    def config(self, step):
        self.term()
        self.write_config(step["config"])
        self.log("config", config=step["config"], relabel=step["relabel"], **_alert(step))
        self.start()
        self.up()    # the start with another config disarms (SPEC 4): an `arm` given before that would be undone

    def wait(self, step):
        self.wait_for(lambda: False, step["sec"], slack=0)

    def quiet(self, step):
        heard = [r for r in self.requests_since(0) if r["kind"] != "telegram"]
        if heard:
            self.problem("quiet", f"{len(heard)} requests to Meta although none was due, the first: "
                         f"{heard[0]['method']} {heard[0]['path']}")

    def until(self, step):
        since = self.seq()
        what = step["what"]
        if what == "rows":
            done = self.all_stored
        elif what == "dm":    # counted from the last arm: the first DM can be out before `arm` has returned
            armed = [event["seq"] for event in self.events if event["op"] == "arm"][-1]
            done = lambda: sum(r["kind"] == "dm" for r in self.requests_since(armed)) >= step["count"]
        elif what == "fired":    # over the whole run: a fault can fire before this step has begun
            done = lambda: set(step["ids"]) <= self.fired_faults()
        elif what == "ok":
            done = lambda: any(r["kind"] == step["kind"] and r["status"] == 200 and r["sent"]
                               for r in self.requests_since(since))
        else:
            done = lambda: len(self.query(
                f"SELECT 1 FROM comments WHERE comment_id IN ({','.join('?' * len(step['ids']))}) "
                f"AND status IN ({','.join('?' * len(step['among']))})", (*step["ids"], *step["among"]))
            ) == len(step["ids"])
        reached = self.wait_for(done, step["timeout"])
        if not reached:
            self.log("timeout", what=what)
        self.obligation(step, "until", reached, what=what, condition=step)
        return reached

    # ---- files of the bot

    def write_config(self, config):
        # one hour after the run begins: every run reaches the digest (SPEC 8.7)
        config["alerts"]["digest_hour_utc"] = (datetime.fromtimestamp(self.anchor, timezone.utc).hour + 1) % 24
        (self.state / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")

    def write_env(self, env):
        path = self.state / ".env"
        self.has_telegram = bool(env and env.get("TG_BOT_TOKEN") and env.get("TG_CHAT_ID"))
        if env is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
            path.chmod(0o600)

    # ---- settle and verdict

    def active_media(self):
        config = ([self.scenario["config"]] + [e["config"] for e in self.events if e["op"] == "config"])[-1]
        return {reel["media_id"] for reel in config["reels"] if reel["active"]}

    def all_stored(self):
        """Every comment the fake lists under an active reel has a row."""
        stored = {row[0] for row in self.query("SELECT comment_id FROM comments")}
        active = self.active_media()
        return all(c["id"] in stored for c in self.ctl("/comments")["comments"]
                   if c["listed"] and not c["deleted"] and c["media_id"] in active)

    def finished(self):
        marks = ",".join("?" * len(self.active_media()))
        waiting = self.query(f"SELECT 1 FROM comments WHERE status IN {UNFINISHED} AND media_id IN ({marks}) LIMIT 1",
                             tuple(self.active_media()))
        unsent = self.has_telegram and self.query("SELECT 1 FROM outbox WHERE sent_at IS NULL LIMIT 1")
        return not waiting and not unsent and self.all_stored()

    def settle(self, step=None):
        """SPEC section 11: let the faults end, open the gates, wait until the bot has finished everybody."""
        self.check_faults()
        self.ctl("/faults", {"clear": True})
        self.ctl("/mode", {"mode": "ok"})
        self.start()
        self.log("settle")
        calm = 3 * self.scenario["config"]["poll_interval_sec"]    # scaled seconds of "finished" before we believe it
        state = {"armed": 0, "finished_since": None}

        def done():
            budget = self.query("SELECT value FROM kv WHERE key='send_budget'")
            if (not budget or budget[0][0] != "unlimited") and state["armed"] < 5:
                # at the start, and whenever the bot disarmed itself (a spam answer, a changed config): the owner arms
                state["armed"] += 1
                self.arm({"n": "all", "deaf": True})
            if not self.finished():
                state["finished_since"] = None
                return False
            state["finished_since"] = state["finished_since"] or self.now()
            return self.now() - state["finished_since"] >= calm
        reached = self.wait_for(done, self.scenario["settle_timeout"])
        if not reached:
            self.log("timeout", what="settle")
        self.obligation({}, "settle", reached, timeout=self.scenario["settle_timeout"])

    def run(self):
        """Run the scenario -> (violations, counts). The directory is left complete for a replay."""
        self.state.mkdir(parents=True)
        self.save_result()
        self.anchor = time.time()
        fake = json.loads(json.dumps(self.scenario["fake"]))
        # Initial faults need the same coverage as steps; absent ids are generated in this order by the fake.
        for number, fault in enumerate(fake.get("faults", []), 1):
            self.required_faults.append({
                "id": str(fault.get("id", f"f{number}")), "step": -1,
                "required": fault.pop("required", True), "reason": fault.pop("reason", ""),
                "min_fires": fault.pop("min_fires", 1)})
        (self.dir / "fake.json").write_text(json.dumps(fake), encoding="utf-8")
        self.write_config(self.scenario["config"])
        self.write_env(self.scenario["env"])
        with open(self.dir / "fake.log", "wb") as log:
            self.fake = subprocess.Popen(
                [sys.executable, "-B", str(HERE / "fake_meta.py"), "--scenario", str(self.dir / "fake.json"),
                 "--scale", repr(self.scale), "--anchor", repr(self.anchor)], stdout=subprocess.PIPE, stderr=log)
        try:
            ports = self.fake.stdout.readline()
            if not ports:
                raise RuntimeError("sim/fake_meta.py did not start: " + (self.dir / "fake.log").read_text())
            self.api_port, self.control_port = (json.loads(ports)[name] for name in ("api", "control"))
            for self.step_index, step in enumerate(self.scenario["steps"]):
                self.watch()
                getattr(self, step["op"])(step)
            if self.scenario["settle"]:
                self.settle()
            self.check_faults()
            self.term()
            code, text = self.cli("report", "--json")
            self.log("report", rc=code, text=text)
            self.log("output", text=(self.dir / "bot.log").read_text(encoding="utf-8", errors="replace"))
            truth = self.ctl("/truth")
            truth["comments"] = self.ctl("/comments")["comments"]
        finally:
            if self.proc is not None:
                self.proc.kill()
                self.proc.wait()
            self.fake.terminate()
            self.fake.wait()
            self.fake.stdout.close()
        for name, value in (("scenario", self.scenario), ("truth", truth), ("events", self.events)):
            (self.dir / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.save_result(completed=True)
        violations = invariants.check(truth, self.state / "core.db", self.scenario, self.events) + self.problems
        counts = {"comments": len(truth["comments"]), "requests": len(truth["requests"]), "dms": len(truth["dms"]),
                  "replies": len(truth["replies"]), "telegram": len(truth["telegram"]),
                  "kills": sum(event["op"] == "kill" for event in self.events), "unplaced": self.unplaced,
                  "timeouts": sum(event["op"] == "timeout" for event in self.events)}
        return violations, counts


def _alert(step):
    return {"alert": step["alert"]} if step and "alert" in step else {}


def run_one(scenario, bot, keep_failed=None, scale=None, out=sys.stdout):
    """Run one scenario and print its line -> True if it passed."""
    directory = Path(tempfile.mkdtemp(prefix=f"soak-{scenario['name']}-"))
    started = time.monotonic()
    violations, counts = Run(scenario, bot, directory, scale).run()
    took = time.monotonic() - started
    summary = " ".join(f"{name}={count}" for name, count in counts.items())
    if not violations:
        shutil.rmtree(directory)
        print(f"{scenario['name']} PASS {took:.1f}s {summary}", file=out, flush=True)
        return True
    if keep_failed:
        os.makedirs(keep_failed, exist_ok=True)
        kept = Path(keep_failed) / directory.name
        shutil.move(directory, kept)
        directory = kept
    broken = sorted({violation.invariant for violation in violations})
    print(f"{scenario['name']} FAIL {took:.1f}s {summary} violated={','.join(broken)} kept={directory}", file=out)
    for violation in violations[:20]:
        print(f"    {violation.invariant} {violation.subject}: {violation.evidence}", file=out)
    if len(violations) > 20:
        print(f"    … and {len(violations) - 20} more", file=out)
    out.flush()
    return False


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Soak the real bot against the fake Meta (SPEC.md section 11).")
    parser.add_argument("--seeds", type=int, default=0, help="how many random scenarios")
    parser.add_argument("--start", type=int, default=0, help="the first seed (default 0)")
    parser.add_argument("--keep-failed", metavar="DIR", help="keep the run directories of failing scenarios here")
    parser.add_argument("--scale", type=float, help="clock speed-up instead of each scenario's own")
    parser.add_argument("--only", metavar="NAME", help="named scenarios, comma-separated; `list`; `named` = all")
    parser.add_argument("--bot", default=str(HERE.parent / "bot.py"), help="the bot's entry point")
    args = parser.parse_args(argv)
    if args.only == "list":
        print("\n".join(f"{name}: {builder.__doc__.split(chr(10))[0]}" for name, builder in scenarios.NAMED.items()),
              file=out)
        return 0
    names = list(scenarios.NAMED) if args.only == "named" else args.only.split(",") if args.only else []
    unknown = [name for name in names if name not in scenarios.NAMED]
    usage = (f"unknown scenario {unknown}: `--only list` names them" if unknown else
             "give --seeds N (N > 0) or --only NAME" if not names and args.seeds <= 0 else
             "--scale must be positive" if args.scale is not None and args.scale <= 0 else
             f"no bot at {args.bot}" if not os.path.isfile(args.bot) else None)
    if usage:
        print(usage, file=sys.stderr)
        return 2
    todo = [scenarios.named(name) for name in names] if names else \
        [scenarios.build(seed) for seed in range(args.start, args.start + args.seeds)]
    failed = [scenario["name"] for scenario in todo
              if not run_one(scenario, os.path.abspath(args.bot), args.keep_failed, args.scale, out)]
    print(f"{len(todo)} scenario(s): {len(todo) - len(failed)} passed, {len(failed)} failed"
          + (": " + " ".join(failed) if failed else ""), file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
