#!/usr/bin/env python3
"""The referee (SPEC.md section 11): invariants I1..I12 over the fake's ground truth and the bot's database.

Independent of the bot: none of its modules is imported. What an answer of the fake MEANS (account-wide,
per-item, ambiguous) is written down by hand, here and in the scenario's fault catalogue; it is never taken
from meta.classify. Durations are copied from SPEC sections 7.2 and 8.5, not from clock.T.

    check(truth, db_path, scenario, events) -> [Violation(invariant, subject, evidence)]

truth     the answer of the fake's GET /truth, plus key "comments" = the list of its GET /comments
db_path   the bot's core.db, opened read-only (a kept copy works: `python3 sim/invariants.py RUN_DIR`)
scenario  what sim/scenarios.py built. Read here: "config"; "faults" {fault id: {"klass": ...}} with klass
          ok | account | spam | item | ambiguous | already; "may_expire" [comment ids the schedule may not
          reach in time]; "env"; "fake" (for the token); "expect" {"waiting": reason left at the end}
events    the harness's own log, in the order things happened. Every event has `op`, `seq` (the number of
          the last request the fake had received when the action began) and `t` (scaled unix seconds):
            start / stop               prelaunch pid/snapshot/clock boundary / the process is gone
            kill (held)                SIGKILL while the fake held request `held`: its answer reached nobody
            arm (n, rc)  disarm         operator commands with same-transaction boundary_id from kv
            config (config, relabel {comment id: should_match from now on})  another config across a
                                       restart: the budget is 0 from here until the next arm (SPEC 4)
            env (env)                  .env rewritten
            settle                     faults ended and gates opened; from here the bot must finish
            report (rc, text)          `bot.py report --json`
            output (text)              bot output; cycle_start, http_complete and journal-matched
                                       claim_committed provide factual ordering, never gate decisions
          Any event may carry "alert": {"contains": [...], "within": seconds, "count": n} (Telegram must
          say so in time).
"""
import json
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict, namedtuple
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

Violation = namedtuple("Violation", "invariant subject evidence")

WINDOW = 7 * 86400          # 7.2: Meta's reply window ...
MARGIN = 600                # ... and the bot stops this long before it closes
EPS = 0.01                  # the fake rounds its times to a millisecond
GREETING_SLACK = 300        # a comment this close to late_after_minutes may get either greeting
PROBE_GAP = 300             # 8.5: an account hold is probed after 5 minutes at the earliest
HOLD_BLOCKED = (1800, 43200)    # 8.5: base and maximum of the send hold after an account-wide refusal
HOLD_UNCERTAIN = (900, 21600)   # ... after an uncertain DM
HOLD_STREAK = (3600, 43200)     # ... after 5 DMs in a row without ok
HOLD_SPAM = 86400
ALERT_WITHIN = 1800         # default deadline for an expired / uncertain person to be announced
REAL_SLACK = 1.0            # real seconds every alert deadline also gets: with a 6000x clock a 20-minute
                            # deadline is 0.2 s of wall time, less than one cycle takes on a slow disk
TERMINAL = ("done", "uncertain", "refused", "expired", "dup_user", "already")
HANDLED = ("sending", "sent", "done", "uncertain", "already")
ALL_REFUSING = ("blocked", "token_dead", "throttled")   # modes in which the fake refuses every Meta call
INFINITY = float("inf")


def _unix(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _group(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    return groups


def _split(requests, cuts):
    """Requests (in seq order) cut into runs wherever a cut seq lies between two of them."""
    runs = defaultdict(list)
    for request in requests:
        runs[sum(cut < request["seq"] for cut in cuts)].append(request)
    return [runs[index] for index in sorted(runs)]


def _seqs(requests):
    return "seq " + ",".join(str(request["seq"]) for request in requests)


def _reel(config, media_id):
    return next((reel for reel in config["reels"] if reel["media_id"] == media_id), None)


def _own_reply(comment):
    return any(reply["own"] for reply in comment["replies"])


def _read_db(path):
    tables = dict.fromkeys(("comments", "attempts", "kv", "problems", "outbox"), ())
    if path and os.path.exists(path):
        conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for name in tables:
                tables[name] = [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
        finally:
            conn.close()
    return tables


class World:
    """Everything the invariants look at, indexed once."""

    def __init__(self, truth, db_path, scenario, events):
        self.scenario, self.events = scenario, events
        self.requests = truth["requests"]
        self.by_seq = {request["seq"]: request for request in self.requests}
        self.dm = [request for request in self.requests if request["kind"] == "dm"]
        self.public = [request for request in self.requests if request["kind"] == "public"]
        self.comments = {comment["id"]: comment for comment in truth["comments"]}
        self.dms, self.delivered = truth["dms"], _group(truth["dms"], "comment_id")
        self.replies = _group(truth["replies"], "comment_id")
        self.telegram = truth["telegram"]
        self.modes = [event for event in truth["events"] if event["op"] == "mode"]
        self.listener = [event for event in truth["events"] if event["op"] == "listener"]
        db = _read_db(db_path)
        self.rows = {row["comment_id"]: row for row in db["comments"]}
        self.attempts, self.problems, self.outbox = db["attempts"], db["problems"], db["outbox"]
        self.configs = [(0, scenario["config"])] + [(event["seq"], event["config"]) for event in self.of("config")]
        self.killed = {event["held"] for event in self.of("kill")}

    def of(self, *ops):
        return [event for event in self.events if event["op"] in ops]

    def config_at(self, seq):
        """The config of the process that made request `seq`."""
        return [config for since, config in self.configs if since < seq][-1]

    def label(self, comment, seq=None):
        """The scenario's should_match for a comment's text, at request `seq` or (None) at the end."""
        label = comment["should_match"]
        for event in self.of("config"):
            if (seq is None or event["seq"] < seq) and comment["id"] in event.get("relabel", {}):
                label = event["relabel"][comment["id"]]
        return label

    def sendable(self, comment, seq=None):
        """This comment is a request the bot may answer: matching text, an author who is not the account, a
        well-formed comment, an active reel."""
        config = self.configs[-1][1] if seq is None else self.config_at(seq)
        reel = _reel(config, comment["media_id"])
        raw = comment.get("raw") or {}
        has_author = comment["from"] is not None and raw.get("from", 1) is not None
        return bool(reel and reel["active"] and self.label(comment, seq) and not comment["own"] and has_author
                    and "timestamp" not in raw)

    def ever_sendable(self, comment):
        return any(self.sendable(comment, since + 1) for since, _ in self.configs)

    def who(self, comment_id):
        comment = self.comments.get(comment_id)
        user = comment and comment["from"] and comment["from"]["id"]
        return f"comment {comment_id}" + (f" (user {user})" if user else "")

    def answered(self, request):
        """The bot got the fake's answer. The fake's `sent` cannot tell for a process killed while the request
        was held: a write to its dead socket still succeeds."""
        return request["sent"] is True and request["seq"] not in self.killed

    def klass(self, request):
        """What the fake's answer to a POST means, by the hand-written tables:
        ok | account (definite account-wide refusal: may be sent again) | spam | item | already | ambiguous."""
        if not self.answered(request):
            return "ambiguous"    # no answer reached the bot: dropped, or the bot was killed first
        fault = self.scenario.get("faults", {}).get(request["fault_id"], {})
        if "klass" in fault:
            return fault["klass"]
        body = request["response"]
        error = body.get("error") if isinstance(body, dict) else None
        if request["status"] == 200 and error is None:
            has_id = isinstance(body, dict) and (body.get("message_id") or body.get("id"))
            return "ok" if has_id else "ambiguous"
        if not isinstance(error, dict) or request["status"] >= 500:
            return "ambiguous"
        code, subcode = error.get("code"), error.get("error_subcode")
        # the fake's own refusals: mode blocked (200), bad or dead token (190), throttled (4), its hourly cap
        # (613), messaging blocked (100/2534029)
        if code in (190, 200, 4, 613) or (code, subcode) == (100, 2534029):
            return "account"
        if code == 10900 or (code, subcode) == (100, 2534025) or "already" in str(error.get("message")).lower():
            return "already"
        return "ambiguous" if code in (1, -1, 2) else "item"

    def outages(self):
        """Periods in which the bot could not reach Telegram: the bot was down, or the fake refused connections."""
        spans, down, closed = [], None, None
        for event in self.of("stop", "start"):
            if event["op"] == "stop" and down is None:
                down = event["t"]
            elif event["op"] == "start" and down is not None:
                spans.append((down, event["t"]))
                down = None
        for event in self.listener:
            if not event["accept"]:
                closed = event["t"]
            elif closed is not None:
                spans.append((closed, event["t"]))
                closed = None
        return sorted(spans + [(begun, INFINITY) for begun in (down, closed) if begun is not None])

    def deadline(self, since, within):
        end = since + within + REAL_SLACK * float(self.scenario.get("scale") or 1)
        for begun, over in self.outages():
            if begun <= end and over >= since:
                end = max(end, over + within)
        return end

    def announced(self, words, since, within, count=1):
        """Telegram got `count` messages containing all `words` between `since` and the deadline."""
        end = self.deadline(since, within)
        hits = [m for m in self.telegram if since - 1 <= m["t"] <= end and all(word in m["text"] for word in words)]
        return len(hits) >= count


# ---------- the invariants: each returns its violations ----------

def i1(w):
    """G1: at most one delivered DM per person and media."""
    people = defaultdict(list)
    for dm in w.dms:
        people[dm["media_id"], dm["user_id"] or "comment " + dm["comment_id"]].append(dm)
    return [Violation("I1", f"user {user} media {media}", f"{len(got)} DMs delivered: " + ", ".join(
        f"seq {dm['seq']} for comment {dm['comment_id']}" for dm in got))
        for (media, user), got in people.items() if len(got) > 1]


def i2(w):
    """G2: POST /messages as received: gaps of at least one slot, at most the cap in any rolling hour.
    G2 has no exception: a restart does not start the count again."""
    out = []
    for index, request in enumerate(w.dm):
        cap = w.config_at(request["seq"])["max_sends_per_hour"]
        before = w.dm[index - 1] if index else None
        if before and request["t"] - before["t"] < 3600 / cap - EPS:
            out.append(Violation("I2", w.who(request["target"]),
                                 f"seq {request['seq']} came {request['t'] - before['t']:.3f}s after seq "
                                 f"{before['seq']}; the slot is {3600 / cap:.1f}s"))
        hour = [r for r in w.dm[:index + 1] if r["t"] > request["t"] - 3600]
        if len(hour) > cap:
            out.append(Violation("I2", w.who(request["target"]),
                                 f"{len(hour)} POST /messages in the hour ending at seq {request['seq']} "
                                 f"(first seq {hour[0]['seq']}); the cap is {cap}"))
    return out


def i3(w):
    """G3: every POST /messages answers a comment that is a request under the config in force, never the
    account's own."""
    out = []
    for request in w.dm:
        comment = w.comments.get(request["target"])
        if comment is None:
            why = "the fake holds no such comment"
        elif comment["own"]:
            why = "it is the account's own comment"
        elif not w.sendable(comment, request["seq"]):
            why = f"not a request under the config in force: text {comment['text']!r}, from {comment['from']}"
        else:
            continue
        out.append(Violation("I3", w.who(request["target"]), f"POST /messages seq {request['seq']}: {why}"))
    return out


def i4(w):
    """G4: after any answer other than a definite account-wide refusal the comment is never sent to again
    (a refused connection never reaches the fake)."""
    out = []
    for comment_id, requests in _group(w.dm, "target").items():
        for request, later in zip(requests, requests[1:]):
            if w.klass(request) not in ("account", "spam"):
                out.append(Violation("I4", w.who(comment_id),
                                     f"POST /messages seq {later['seq']} although seq {request['seq']} was "
                                     f"answered {w.klass(request)} (HTTP {request['status']})"))
                break
    return out


def i5(w):
    """Status against delivery: sent/done = exactly one DM; uncertain/already = none or one; any other = none."""
    out = []
    for comment_id, row in w.rows.items():
        got = w.delivered.get(comment_id, [])
        allowed = {"sent": (1,), "done": (1,), "uncertain": (0, 1), "already": (0, 1)}.get(row["status"], (0,))
        if len(got) not in allowed:
            out.append(Violation("I5", w.who(comment_id),
                                 f"row is {row['status']}/{row['reason']} but {len(got)} DM(s) were delivered"
                                 + (f" ({_seqs(got)})" if got else "")))
    out += [Violation("I5", w.who(comment_id), f"DM delivered ({_seqs(got)}) but the database has no row")
            for comment_id, got in w.delivered.items() if comment_id not in w.rows]
    return out


def i6(w):
    """A public reply only under a comment whose DM Meta confirmed before, and at most one each."""
    out = []
    for request in w.public:
        comment_id = request["target"]
        if not w.config_at(request["seq"])["public_reply"]:
            out.append(Violation("I6", w.who(comment_id), f"public reply seq {request['seq']} although "
                                 "public_reply is false"))
        if not any(dm["target"] == comment_id and dm["seq"] < request["seq"] and dm["delivered"]
                   and w.klass(dm) == "ok" for dm in w.dm):
            out.append(Violation("I6", w.who(comment_id), f"public reply seq {request['seq']} without a "
                                 "confirmed DM to that comment before it"))
    for comment_id, requests in _group(w.public, "target").items():
        if len(requests) > 1:
            out.append(Violation("I6", w.who(comment_id), f"{len(requests)} public reply POSTs: {_seqs(requests)}"))
    out += [Violation("I6", w.who(comment_id), f"{len(posted)} public replies posted: {_seqs(posted)}")
            for comment_id, posted in w.replies.items() if len(posted) > 1]
    return out


def _budget(w):
    """SET epochs are ordered by the arm transaction's committed DM attempt id boundary."""
    attempts = sorted((a for a in w.attempts if a["kind"] == "dm"), key=lambda a: a["id"])
    operators = [e for e in w.of("arm", "disarm") if e.get("rc", 0) == 0]
    if any("boundary_id" not in e for e in operators):
        return [Violation("INCONCLUSIVE", "budget", "legacy operator event lacks transaction boundary_id")]
    steps = [(a["id"], 0, "claim", a) for a in attempts]
    steps += [(e["boundary_id"], 1, "set", e) for e in operators]
    # A config change happens with the service stopped; the following start snapshot is its claim cut.
    for change in w.of("config"):
        starts = [e for e in w.of("start") if e["seq"] >= change["seq"] and e["t"] >= change["t"]]
        if "boundary_id" in change:
            steps.append((change["boundary_id"], 1, "config", change))
        elif starts and "snapshot" in starts[0]:
            steps.append((starts[0]["snapshot"]["max_dm_attempt_id"], 1, "config", change))
    for request in w.dm + w.public:
        if w.klass(request) == "spam":
            before = [a["id"] for a in attempts if _unix(a["started_at"]) <= request["t"]]
            steps.append((max(before, default=0), 1, "zero", None))
    out, remaining = [], 0
    for _, _, kind, value in sorted(steps, key=lambda x: (x[0], x[1],
            w.events.index(x[3]) if x[2] in ("set", "config") else -1)):
        if kind == "set":
            n = value.get("n", 0) if value["op"] == "arm" else 0
            remaining = INFINITY if n == "all" else n
        elif kind in ("zero", "config"):
            remaining = 0
        else:
            if remaining <= 0:
                out.append(Violation("I7", w.who(value["comment_id"]),
                                     f"claim {value['id']} committed when the SET budget allows 0"))
            remaining -= 1
    # HTTP without a corresponding committed claim is never excused by an operator allowance.
    for cid, requests in _group(w.dm, "target").items():
        claims = [a for a in attempts if a["comment_id"] == cid and a["outcome"] != "not_sent"]
        for request in requests:
            matches = [a for a in claims if _unix(a["started_at"]) <= request["t"] + EPS
                       and (a["finished_at"] is None or _unix(a["finished_at"]) >= request["t"] - EPS)]
            if matches:
                claims.remove(matches[0])
            else:
                out.append(Violation("I7", w.who(cid), f"POST seq {request['seq']} without a corresponding "
                                     "committed claim at its receive time"))
    return out


def _blocked(w):
    """I7: in a mode in which the fake refuses every call: no POST after the first refusal until the mode
    ends, no further sweeps, and probes no closer than the probe schedule allows."""
    out = []
    restarts = [event["seq"] for event in w.of("start", "env")]
    bounds = [(event["after_seq"], event["mode"]) for event in w.modes] + [(INFINITY, None)]
    for (begin, mode), (end, _) in zip(bounds, bounds[1:]):
        inside = [r for r in w.requests if begin < r["seq"] <= end and r["kind"] not in (None, "telegram")]
        # A refusal is a definite account-wide answer. An injected code 1 or 500 in its place is a transient
        # failure of that one call (SPEC 6.1): after it the bot rightly goes on to the next reel (SPEC 8.3).
        refused = [r for r in inside if w.klass(r) in ("account", "spam")]
        if mode not in ALL_REFUSING or not refused:
            continue
        out += [Violation("I7", w.who(r["target"]), f"POST seq {r['seq']} in mode {mode} after the refusal of "
                          f"seq {refused[0]['seq']}")
                for r in inside if r["seq"] > refused[0]["seq"] and r["kind"] in ("dm", "public")]
        for run in _split(inside, restarts):    # one more refused read per process run is honest ignorance
            known = [r for r in run if r in refused][:1]
            later = [r for r in run if known and r["seq"] > known[0]["seq"]]
            reads = [r for r in later if r["kind"] in ("media", "comments")]
            if reads:
                out.append(Violation("I7", f"mode {mode}", f"{len(reads)} sweep requests ({_seqs(reads[:5])}…) "
                                     f"after the refusal of seq {known[0]['seq']}"))
            probes = [r for r in known + later if r["kind"] == "me"]
            for probe, following in zip(probes, probes[1:]):
                if following["t"] - probe["t"] < PROBE_GAP - EPS:
                    out.append(Violation("I7", f"mode {mode}", f"probe seq {following['seq']} only "
                                         f"{following['t'] - probe['t']:.0f}s after probe seq {probe['seq']}"))
                    break
    return out


def _doubled(rule, steps):
    return min(rule[0] * 2 ** steps, rule[1])


def _holds(w):
    """I7: no POST while a send hold of 8.5 must be in force."""
    out = []
    quiet_until, cause, blocked, uncertain, streak = -INFINITY, None, 0, 0, 0
    for request in sorted(w.dm + w.public, key=lambda r: r["seq"]):
        if request["t"] < quiet_until - EPS:
            out.append(Violation("I7", w.who(request["target"]),
                                 f"POST seq {request['seq']} came {quiet_until - request['t']:.0f}s before "
                                 f"the hold after seq {cause['seq']} ({w.klass(cause)}) could end"))
        klass, need = w.klass(request), 0
        if request["kind"] == "dm":
            uncertain = uncertain + 1 if klass == "ambiguous" else 0
            # 8.5: "already replied" is passed over: it neither counts toward the five nor ends the run,
            # and it sets no hold itself
            streak = 0 if klass == "ok" else streak + (klass != "already")
            if uncertain:
                need = _doubled(HOLD_UNCERTAIN, uncertain - 1)
            if streak >= 5 and klass != "already":
                need = max(need, _doubled(HOLD_STREAK, streak - 5))
        blocked = blocked + 1 if klass in ("account", "spam") else 0
        if klass == "spam":
            need = max(need, HOLD_SPAM)
        elif klass == "account":
            need = max(need, _doubled(HOLD_BLOCKED, blocked - 1))
        if need and request["t"] + need > quiet_until:
            quiet_until, cause = request["t"] + need, request
    return out


def _diagnostics(w):
    """Parse complete factual lines. Damaged/interleaved numeric fields are missing evidence, not a crash."""
    found = []
    for event in w.of("output"):
        for line in event.get("text", "").splitlines():
            match = re.search(r"\b(cycle_start|http_complete|claim_committed)\s+(.*)", line)
            if not match:
                continue
            pairs = re.findall(r"(\w+)=([^\s]+)", match[2])
            fields = dict(pairs)
            try:
                fields.update(op=match[1], pid=int(fields["pid"]), monotonic=float(fields["monotonic"]))
                if len(pairs) != len(dict(pairs)) or fields["pid"] <= 0 or \
                        not math.isfinite(fields["monotonic"]) or fields["monotonic"] < 0:
                    raise ValueError("invalid process clock or duplicate fields")
                if fields["op"] == "http_complete":
                    fields.update(call=int(fields["call"]), http=int(fields["http"]))
                    if fields["call"] <= 0 or not (fields["http"] == 0 or 100 <= fields["http"] <= 599):
                        raise ValueError("invalid HTTP ordinal/status")
                elif fields["op"] == "claim_committed":
                    fields["attempt"] = int(fields["attempt"])
                    if fields["attempt"] <= 0 or fields.get("kind") not in ("dm", "public"):
                        raise ValueError("invalid journal claim identity")
                    if re.search(r"\bstarted_at=", match[2]):
                        stamp = fields.get("started_at", "")
                        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", stamp):
                            raise ValueError("noncanonical journal claim timestamp")
                        _unix(stamp)  # reject invalid calendar dates as well as invalid syntax
            except (KeyError, ValueError, TypeError, OverflowError):
                fields["invalid"] = True
            found.append(fields)
    return found


def _process_end(w, starts, index):
    """The next prelaunch or a confirmed stop bounds this process, including reused OS pids."""
    first = w.events.index(starts[index]) + 1
    last = w.events.index(starts[index + 1]) if index + 1 < len(starts) else len(w.events)
    bounds = [event["monotonic"] for event in w.events[first:last]
              if event["op"] == "stop" and "monotonic" in event]
    if index + 1 < len(starts):
        bounds.append(starts[index + 1]["monotonic"])
    return min(bounds, default=INFINITY)


def _claim_points(w, starts, diagnostics, scale):
    """Accept an exact insertion clock only for its committed row and its observed process lifetime."""
    rows = {a["id"]: a for a in w.attempts}
    groups = _group([d for d in diagnostics if not d.get("invalid") and d["op"] == "claim_committed"], "attempt")
    points, invalid = {}, []
    for attempt_id, facts in groups.items():
        row = rows.get(attempt_id)
        fact = facts[0]
        owners = [index for index, start in enumerate(starts) if fact["pid"] == start["pid"]
                  and start["monotonic"] <= fact["monotonic"] < _process_end(w, starts, index)]
        valid = len(facts) == 1 and row is not None and row["kind"] == fact["kind"] and len(owners) == 1
        if valid:
            index = owners[0]
            start = starts[index]
            if "started_at" in fact:
                # The exact INSERT string correlates this point without assuming the wall clock kept
                # its startup relationship to monotonic time (NTP may slew either way during a run).
                valid = fact["started_at"] == row["started_at"]
            else:
                # Legacy facts lack this correlation. Preserve their conservative startup-clock bounds.
                lower = start["monotonic"] + (_unix(row["started_at"]) - start["t"]) / scale
                upper = start["monotonic"] + (_unix(row["finished_at"]) - start["t"]) / scale \
                    if row.get("finished_at") else INFINITY
                valid = lower - .00005 <= fact["monotonic"] <= upper + .001 / scale + .00005
            # IDs belong to one append-only journal: even a new public row must follow this DM bound.
            valid &= attempt_id > start["snapshot"].get("max_dm_attempt_id", 0)
            # A matching receive is after insertion. Absence is legitimate for a killed/not-sent claim.
            receives = [r for r in w.requests if r["kind"] == row["kind"] and r["target"] == row["comment_id"]
                        and _unix(row["started_at"]) - EPS <= r["t"]
                        and (not row.get("finished_at") or r["t"] <= _unix(row["finished_at"]) + EPS)]
            if receives:
                valid &= all("monotonic" in r for r in receives) and \
                    fact["monotonic"] <= min(r.get("monotonic", -INFINITY) for r in receives)
        if valid:
            points[attempt_id] = owners[0], fact["monotonic"]
        else:
            invalid.append(Violation("INCONCLUSIVE", "G6", f"claim telemetry {attempt_id} cannot match a "
                                     "unique committed journal row and process boundary"))
    return points, invalid


def _read_global(status, body):
    """SPEC 6.1 GET table, independent of meta.classify and the POST fault catalogue."""
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}

    def number(value):
        if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
            return None
        try:
            value = int(value)
            return value if -2 ** 63 <= value < 2 ** 63 else None
        except (TypeError, ValueError, OverflowError):
            return None

    code = number(error.get("code"))
    subcode = number(error.get("error_subcode")) or number(error.get("subcode"))
    if not status or status >= 500 or code in (1, -1, 2):
        return False
    if status == 429:
        return True
    if status == 200 and isinstance(body, dict) and "error" not in body:
        return False
    return code is not None and (code in (190, 368, 508, 4, 17, 32, 341, 613, 3, 10, 25)
        or 80000 <= code <= 80099 or 200 <= code <= 299 or (code, subcode) == (100, 2534029)
        or code == 100 and subcode is None and "Unsupported request" in str(error.get("message", "")))


def _identifier(value):
    """Usable graph identifier: an integer other than bool, or a nonempty string (SPEC 8.2/8.3)."""
    if type(value) is int:
        return str(value)
    return value if isinstance(value, str) and value else None


class _AccountGate:
    """Observed probe lifecycle (8.2/8.5), retained across process restarts, never read from core hold flags."""

    def __init__(self, identity):
        self.identity = identity
        self.held, self.required, self.failures = False, False, 0

    def observe(self, kind, status, body):
        global_refusal = _read_global(status, body)
        if kind in ("media", "comments"):
            self.required |= global_refusal  # only a compatible /me can make this a reel-only failure
            return
        if kind != "me":  # refresh outcomes never set account holds
            return
        data = body if status == 200 and isinstance(body, dict) and "error" not in body else {}

        uid, prior = _identifier(data.get("user_id")), self.identity
        if uid and prior["user_id"] and uid != prior["user_id"]:
            self.held = True
            return  # the mismatched account never replaces the identity against which recovery is checked
        username = data.get("username")
        identity = {"user_id": uid, "id": _identifier(data.get("id")) or prior["id"],
                    "username": username if isinstance(username, str) and username else prior["username"]}
        if all(identity.values()):
            self.identity = identity
            self.held, self.required, self.failures = False, False, 0
            return
        self.failures += 1
        self.held |= global_refusal or self.required or bool(uid) or not all(prior.values()) or self.failures >= 3


def _g6(w):
    """Reconstruct identity and both collect gates from raw HTTP semantics, never engine success flags."""
    if not w.attempts and not (w.dm or w.public):
        return []
    starts, diagnostics = w.of("start"), _diagnostics(w)
    if not starts or any(not all(k in s for k in ("pid", "monotonic", "snapshot")) for s in starts):
        return [Violation("INCONCLUSIVE", "G6", "missing prelaunch process snapshot/boundary")]
    out, scale, account = [], float(w.scenario.get("scale", 1)), None
    if any(d.get("invalid") for d in diagnostics):
        out.append(Violation("INCONCLUSIVE", "G6", "malformed cycle/HTTP/claim diagnostic"))
    diagnostics = [d for d in diagnostics if not d.get("invalid")]
    claim_points, invalid = _claim_points(w, starts, diagnostics, scale)
    out += invalid
    for index, start in enumerate(starts):
        end = starts[index + 1]["seq"] if index + 1 < len(starts) else INFINITY
        until = starts[index + 1]["t"] if index + 1 < len(starts) else INFINITY
        claims = [a for a in w.attempts if (claim_points[a["id"]][0] == index if a["id"] in claim_points else
                                          start["t"] <= _unix(a["started_at"]) < until)]
        records = [r for r in w.requests if start["seq"] < r["seq"] <= end and r["kind"] != "telegram"]
        logs = sorted((d for d in diagnostics if d["pid"] == start["pid"] and
                       start["monotonic"] <= d["monotonic"] < _process_end(w, starts, index)),
                      key=lambda d: d["monotonic"])
        if not claims and not records and not any(d["op"] == "http_complete" for d in logs):
            continue
        cycles = [d for d in logs if d["op"] == "cycle_start"]
        if not cycles or any("monotonic" not in r for r in records):
            out.append(Violation("INCONCLUSIVE", "G6", "missing cycle or HTTP receive boundary"))
            continue
        timeline, unused = [(d["monotonic"], "cycle", d) for d in cycles], list(records)
        missing, ordinal = False, 0
        for d in (d for d in logs if d["op"] == "http_complete"):
            call = d["call"]
            if call != ordinal + 1:
                missing = True
            ordinal = call
            if d["http"] == 0:
                if d.get("method") == "GET" and d.get("kind") == "read_me":
                    timeline.append((d["monotonic"], "probe_failure", None))
                continue
            candidates = [r for r in unused if r["path"] == d.get("route") and r["method"] == d.get("method")
                          and r["monotonic"] <= d["monotonic"] and r["status"] == d["http"]
                          and w.answered(r)]
            if not candidates:
                missing = True
                continue
            request = candidates[0]
            unused.remove(request)
            timeline.append((d["monotonic"], "http", request))
        # A claimed POST killed before completion is legitimate evidence. Missing completed reads are not.
        missing |= any(r["kind"] in ("me", "media", "comments") and w.answered(r) for r in unused)
        if missing:
            out.append(Violation("INCONCLUSIVE", "G6", "HTTP completion cannot be matched to fake response"))
        for claim in claims:
            at = claim_points[claim["id"]][1] if claim["id"] in claim_points else \
                start["monotonic"] + (_unix(claim["started_at"]) - start["t"]) / scale
            timeline.append((at, "claim", claim))
        kv = start["snapshot"]["kv"]
        if account is None:
            account = _AccountGate({k: kv.get("me_" + k) for k in ("id", "user_id", "username")})
        known = set(start["snapshot"].get("known_comments", []))
        full, current, chains, cycle = set(), set(), {}, False
        # Request seq can stay unchanged across a candidate edit; only changes before this start apply.
        config = ([w.scenario["config"]] + [e["config"] for e in w.events[:w.events.index(start)]
                                           if e["op"] == "config"])[-1]
        boundaries = [at for at, kind, _ in timeline if kind != "claim"]
        # started_at is floored to a millisecond; the paired startup clocks also have small skew.
        # A boundary anywhere in that interval cannot prove which cycle/HTTP state admitted a claim.
        precision = .001 / scale + .00005
        for moment, kind, value in sorted(timeline, key=lambda x: x[0]):
            if kind == "cycle":
                current, chains, cycle = set(), {}, True
                continue
            if kind == "probe_failure":
                account.observe("me", 0, None)
                continue
            if kind == "claim":
                if not config["sending"]:
                    out.append(Violation("I7", w.who(value["comment_id"]),
                                         f"G6 claim {value['id']} ({value['kind']}) while the "
                                         "loaded config says sending=false"))
                if missing:
                    # An unobserved completion may establish or revoke these HTTP-derived gates.
                    # Its absence is inconclusive evidence, not proof that the product skipped collect.
                    continue
                exact = value["id"] in claim_points
                ambiguous = any(abs(moment - at) <= 1e-9 for at in boundaries) if exact else \
                    any(moment - .00005 <= at <= moment + precision for at in boundaries)
                if ambiguous:
                    out.append(Violation("INCONCLUSIVE", "G6",
                                         f"claim {value['id']} overlaps a cycle/HTTP clock boundary"))
                    continue
                row = w.rows.get(value["comment_id"], {})
                media = row.get("media_id") or w.comments.get(value["comment_id"], {}).get("media_id")
                why = []
                if not all(account.identity.values()):
                    why.append("identity incomplete")
                if account.held or account.required:
                    why.append("account hold or required probe unresolved")
                if media not in full:
                    why.append("no completed full sweep in this process")
                if not cycle or media not in current:
                    why.append("collect incomplete or failed in this cycle")
                if why:
                    out.append(Violation("I7", w.who(value["comment_id"]),
                                         f"G6 claim {value['id']} ({value['kind']}): " + "; ".join(why)))
                continue
            r = value
            body, media = r["response"], r["target"]
            ok = w.answered(r) and r["status"] == 200 and isinstance(body, dict) and "error" not in body
            account.observe(r["kind"], r["status"], body)
            if r["kind"] == "media":
                current.discard(media)
                reel = _reel(config, media)
                permalink = reel and reel.get("permalink")
                short = urlsplit(permalink).path.strip("/").split("/")[-1] if permalink else None
                chains[media] = {"full": True, "ok": ok and (not short or short == body.get("shortcode")),
                                 "count": body.get("comments_count", 0) if ok else 0, "pages": 0,
                                 "items": 0, "next": None}
            elif r["kind"] == "comments":
                current.discard(media)
                chain = chains.setdefault(media, {"full": False, "ok": media in full,
                                                  "count": 0, "pages": 0, "items": 0, "next": None})
                expected = chain["next"]
                params = r.get("params", {})
                if expected:
                    expected_url = urlsplit(expected)
                    chain["ok"] &= (expected_url.path == r["path"] and all(
                        params.get(k) == v[-1] for k, v in parse_qs(expected_url.query).items() if k != "access_token"))
                elif chain["pages"] == 0:
                    chain["ok"] &= "after" not in params
                else:
                    chain["ok"] = False
                items = body.get("data") if ok else None
                chain["ok"] &= ok and isinstance(items, list)
                if not chain["ok"]:
                    continue
                parsed_ids = [_identifier(item.get("id")) if isinstance(item, dict) else None for item in items]
                ids = {item_id for item_id in parsed_ids if item_id is not None}
                unknown = bool(ids - known)
                known.update(ids)
                chain["pages"] += 1
                chain["items"] += len(items)
                paging = body.get("paging") or {}
                chain["next"] = paging.get("next") if isinstance(paging, dict) else None
                complete = not chain["next"] or (not chain["full"] and not unknown)
                if complete and not (chain["full"] and chain["count"] > 0 and chain["items"] == 0):
                    current.add(media)
                    if chain["full"]:
                        full.add(media)
                elif chain["pages"] >= 400:
                    chain["ok"] = False
    return out


def i7(w):
    """G6: no POST while sending is off; then the budget, the blocking modes and the send holds."""
    off = [Violation("I7", w.who(request["target"]), f"POST seq {request['seq']} ({request['kind']}) while the "
                     "config says sending=false")
           for request in w.dm + w.public if not w.config_at(request["seq"])["sending"]]
    return off + _budget(w) + _blocked(w) + _holds(w) + _g6(w)


def i8(w):
    """G5 completeness: every comment the fake listed (and, once settled, every comment it still lists) has
    a row; the row is `pending` or later exactly when the comment is a request."""
    out = []
    settled = bool(w.of("settle"))
    lost_rows = [event["seq"] for event in w.of("kill")]
    active = {reel["media_id"] for reel in w.configs[-1][1]["reels"] if reel["active"]}
    for comment_id, comment in w.comments.items():
        row, listed = w.rows.get(comment_id), comment["first_listed_seq"]
        if row is None:
            visible = comment["listed"] and not comment["deleted"] and comment["media_id"] in active
            # a deleted comment whose row a kill took away cannot be collected again
            excused = comment["deleted"] and any(seq >= (listed or 0) for seq in lost_rows)
            if (listed is not None and not excused) or (settled and visible):
                out.append(Violation("I8", w.who(comment_id), "no row; first listed by "
                                     + (f"seq {listed}" if listed else "nothing, though the fake still lists it")))
            continue
        status = row["status"]
        if comment["media_id"] not in active:
            continue    # an inactive reel is parked as it is
        if w.sendable(comment):
            wrong = status == "seen"
        elif w.ever_sendable(comment):
            wrong = status in ("pending", "sending")    # a request only under an earlier config
        else:
            wrong = status != "seen"    # also with a reply of the account under it: 8.3 rule 1 is for requests
        if wrong:
            out.append(Violation("I8", w.who(comment_id),
                                 f"row is {status}/{row['reason']} but the comment {comment['text']!r} is "
                                 f"{'' if w.sendable(comment) else 'not '}a request"))
    out += [Violation("I8", f"comment {comment_id}", f"row {dict(row)} for a comment the fake never held")
            for comment_id, row in w.rows.items() if comment_id not in w.comments]
    return out


def _justified(w, comment_id, row, comment):
    """Why this final status is not backed by what the fake did, or None."""
    status, reason = row["status"], row["reason"]
    answers = [w.klass(request) for request in w.dm if request["target"] == comment_id]
    if status == "refused":
        if not answers or answers[-1] != "item":
            return f"the fake's answers to its DM were {answers or 'none at all'}, the last not per-item"
    elif status == "uncertain":
        if "ambiguous" not in answers:
            return f"no ambiguous answer and no kill: the fake's answers were {answers or 'none at all'}"
    elif status == "expired":
        age = _unix(row["updated_at"]) - comment["ts"]
        if age < WINDOW - MARGIN - EPS:
            return f"only {age:.0f}s old when it was expired"
        listed = w.by_seq.get(comment["first_listed_seq"])
        if comment_id not in w.scenario.get("may_expire", ()) and not (
                listed and listed["t"] - comment["ts"] >= WINDOW - MARGIN - EPS):
            return "the schedule left time to answer it before the window closed"
    elif status == "dup_user":
        author = comment["from"] and comment["from"]["id"]
        at = _unix(row["updated_at"])
        named = reason.removeprefix("dup:") if reason.startswith("dup:") else None
        handled = []
        for other in w.comments.values():
            if (other["id"] == comment_id or (named and other["id"] != named)
                    or other["media_id"] != comment["media_id"] or not other["from"]
                    or other["from"]["id"] != author):
                continue
            previous = w.rows.get(other["id"], {})
            # A durable terminal handled row written before the decision is sufficient.
            established = (previous.get("status") in ("sent", "done", "uncertain", "already")
                           and _unix(previous["updated_at"]) <= at)
            for attempt in w.attempts:
                if attempt["comment_id"] != other["id"] or attempt["kind"] != "dm":
                    continue
                begun = _unix(attempt["started_at"])
                finished = _unix(attempt["finished_at"]) if attempt["finished_at"] else INFINITY
                # sending during the claim; afterwards only confirmed/uncertain/already outcomes handle.
                established |= begun <= at and (at < finished or attempt["outcome"] in ("ok", "uncertain")
                                               or attempt.get("reason") == "already_replied")
            if established:
                handled.append(other["id"])
        if not handled:
            return f"user {author} has no other comment that was handled"
    elif status == "already":
        if reason != "already_replied" and _own_reply(comment):
            return None
        if reason != "owner_replied" and "already" in answers:
            return None
        return f"no reply by the account under it and no already-replied answer (answers {answers})"
    elif status == "done" and reason == "" and not _own_reply(comment):
        return "public reply recorded as posted but the fake holds none"
    return None


def i9(w):
    """Justification: nobody is written off (or ticked off) without the fake having given cause."""
    out = []
    for comment_id, row in w.rows.items():
        comment = w.comments.get(comment_id)
        why = comment and _justified(w, comment_id, row, comment)
        if why:
            out.append(Violation("I9", w.who(comment_id), f"{row['status']}/{row['reason']}: {why}"))
    return out


def i10(w):
    """Progress: once settled every request has a terminal status; unsettled, whoever is left waits for a
    reason the bot reports."""
    out = []
    settled = bool(w.of("settle"))
    expected = w.scenario.get("expect", {}).get("waiting")
    said = " ".join([problem["text"] or "" for problem in w.problems if problem["key"] == "waiting"]
                    + [event.get("text") or "" for event in w.of("report")])
    for comment_id, comment in w.comments.items():
        row = w.rows.get(comment_id)
        if row is None or row["status"] in TERMINAL + ("seen",) or not w.sendable(comment):
            continue    # a request stored as `seen` is I8's finding
        if settled:
            why = "faults ended and gates were opened, yet it is not finished"
        elif row["status"] != "pending":
            why = "left half-way"
        elif not expected or expected not in said:
            why = f"left waiting and the bot reports no waiting reason {expected!r}"
        else:
            continue
        out.append(Violation("I10", w.who(comment_id), f"row is {row['status']}/{row['reason']}: {why}"))
    return out


def _payload_problem(config, reel, request, comment):
    message = request["body"].get("message") if isinstance(request["body"], dict) else None
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(text, str):
        return "not a text message"
    if reel["link"] not in text:
        return f"the link of reel {reel['name']} ({reel['link']}) is missing"
    if "{greeting}" in text or "{link}" in text:
        return "a placeholder was sent as it is"
    if "{greeting}" in reel["text"] and config["greeting"] != config["late_greeting"]:
        fresh, late = config["greeting"], config["late_greeting"]
        says_late = late in text and not (late in fresh and fresh in text)
        says_fresh = fresh in text and not (fresh in late and late in text)
        age, edge = request["t"] - comment["ts"], config["late_after_minutes"] * 60
        if not (says_late or says_fresh):
            return "no greeting"
        if abs(age - edge) > GREETING_SLACK and says_late != (age > edge):
            return f"wrong greeting for a comment {age:.0f}s old (late after {edge}s)"
    return None


def i11(w):
    """Payloads: the DM carries the link of the comment's reel and the right greeting; the public reply is one
    of the phrases; no request that the strict fake had to reject."""
    out = [Violation("I11", f"request seq {request['seq']}", f"not a call of SPEC section 6: {request['strict']}")
           for request in w.requests if request["strict"]]
    for request in w.dm:
        comment, config = w.comments.get(request["target"]), w.config_at(request["seq"])
        reel = comment and _reel(config, comment["media_id"])
        problem = reel and not request["strict"] and _payload_problem(config, reel, request, comment)
        if problem:
            out.append(Violation("I11", w.who(request["target"]), f"DM seq {request['seq']}: {problem}"))
    for request in w.public:
        text = request["params"].get("message")
        if text not in w.config_at(request["seq"])["public_phrases"]:
            out.append(Violation("I11", w.who(request["target"]),
                                 f"public reply seq {request['seq']}: {text!r} is not one of the phrases"))
    return out


def _unaccounted(w):
    """SPEC section 9, computed here from the database, after the bot was stopped."""
    ok_dm = {attempt["comment_id"] for attempt in w.attempts if attempt["kind"] == "dm" and attempt["outcome"] == "ok"}
    configured = {reel["media_id"] for reel in w.configs[-1][1]["reels"]}
    found = [f"attempt {attempt['id']} for comment {attempt['comment_id']} never finished"
             for attempt in w.attempts if attempt["finished_at"] is None]
    for comment_id, row in w.rows.items():
        if row["status"] == "sending":
            found.append(f"comment {comment_id} is still sending")
        elif row["status"] in ("sent", "done") and comment_id not in ok_dm:
            found.append(f"comment {comment_id} is {row['status']} without an ok DM in the journal")
        elif row["status"] in ("pending", "sent") and row["media_id"] not in configured:
            found.append(f"comment {comment_id} is {row['status']} but its media is not in the config")
    return found


def _secrets(w):
    token = w.scenario.get("fake", {}).get("token")
    values = [token.get("value") if isinstance(token, dict) else token]
    for env in [w.scenario.get("env")] + [event["env"] for event in w.of("env")]:
        values += [(env or {}).get(name) for name in ("IG_ACCESS_TOKEN", "TG_BOT_TOKEN")]
    return {value for value in values if value}


_LOST_EVENT = re.compile(r"ℹ (expired|uncertain): ([1-9][0-9]*) (.+) \(([1-9][0-9]*) in all\)")
_LOST_PAYLOAD = {
    "expired": "people lost: their comments are past the 7-day window",
    "uncertain": "DM(s) may or may not have been delivered: they are never sent again",
}


def _lost_alerts(w, telegram_since, within):
    """Accepted complete count events cover losses; mentions, queued rows and repeated totals do not."""
    if telegram_since is None:
        return []
    populations = _group([row for row in w.rows.values() if row["status"] in _LOST_PAYLOAD], "status")
    # While windows keep closing, `expired` is said at most once per alerts.repeat_min (engine._lost_people).
    paced = 60 * max((config.get("alerts") or {}).get("repeat_min", 0) for _, config in w.configs)
    waiting = defaultdict(list)
    for key, rows in populations.items():
        for row in sorted(rows, key=lambda row: (row["updated_at"], row["comment_id"])):
            comment = w.comments.get(row["comment_id"])
            if comment and w.ever_sendable(comment):
                since = max(_unix(row["updated_at"]), telegram_since)
                waiting[key].append((row, since, w.deadline(since, within + (paced if key == "expired" else 0))))
    # A typed localized payload is evidence only when its whole accepted event line matches the outbox.
    # sent_at is deliberately irrelevant: Telegram may accept before the DB confirmation fails.
    typed = {(line["key"], line["text"]) for line in w.outbox if line["key"] in _LOST_PAYLOAD}
    covered, counted = set(), defaultdict(set)
    for message in sorted(w.telegram, key=lambda item: item["t"]):
        for line in message["text"].splitlines():
            line = line.strip()
            event = _LOST_EVENT.fullmatch(line)
            if not event:
                continue
            key, number, payload, cumulative = event.groups()
            if payload != _LOST_PAYLOAD[key] and (key, line) not in typed:
                continue
            try:
                count, total = int(number), int(cumulative)
            except ValueError:
                continue
            if not count <= total <= len(populations.get(key, ())):
                continue
            # Absolute count ranges make an accepted retry (or overlapping repeated event) idempotent.
            slots = set(range(total - count, total))
            available = len(slots - counted[key])
            counted[key].update(slots)
            for row, since, deadline in waiting[key]:
                if available and row["comment_id"] not in covered and since - 1 <= message["t"] <= deadline:
                    covered.add(row["comment_id"])
                    available -= 1
    return [Violation("I12", w.who(row["comment_id"]), f"became {key} at {row['updated_at']} and Telegram "
                      f"heard nothing of it within {within}s")
            for key, rows in waiting.items() for row, _, _ in rows if row["comment_id"] not in covered]


def i12(w):
    """G5 alerts: what the harness expected to hear, every expired and uncertain person, nothing unaccounted
    at the end, and no secret in anything the bot said."""
    out = []
    telegram_since = None    # when .env first named a Telegram channel
    for event in [{"env": w.scenario.get("env"), "t": 0}] + w.of("env"):
        if telegram_since is None and (event["env"] or {}).get("TG_BOT_TOKEN") and event["env"].get("TG_CHAT_ID"):
            telegram_since = event["t"]
    for event in w.events:
        alert = event.get("alert")
        if alert and not w.announced(alert["contains"], event["t"], alert["within"], alert.get("count", 1)):
            out.append(Violation("I12", f"{event['op']} at seq {event['seq']}",
                                 f"Telegram did not say {alert['contains']} {alert.get('count', 1)} time(s) "
                                 f"within {alert['within']}s"))
    within = w.scenario.get("alert_within", ALERT_WITHIN)
    out += _lost_alerts(w, telegram_since, within)
    out += [Violation("I12", "unaccounted", text) for text in _unaccounted(w)]
    out += [Violation("I12", "unaccounted", f"the bot's own problem list says: {problem['text']}")
            for problem in w.problems if problem["key"] == "unaccounted"]
    for event in w.of("report"):
        try:
            report = json.loads(event["text"])
            amount = report.get("unaccounted") if isinstance(report, dict) else None
        except (ValueError, TypeError):
            amount = None
        if event["rc"] != 0 or type(amount) is not int or amount < 0:
            out.append(Violation("I12", "report", f"`report --json` requires an object with a nonnegative "
                                 f"integer unaccounted; exit {event['rc']}, output {event['text'][:200]!r}"))
        elif amount:
            out.append(Violation("I12", "unaccounted", f"`report --json` says unaccounted = {amount}"))
    said = ([("Telegram message", m["text"]) for m in w.telegram]
            + [(f"`{event['op']}`", event["text"]) for event in w.of("report", "output")]
            + [("table outbox", line["text"]) for line in w.outbox]
            + [("table problems", problem["text"]) for problem in w.problems])
    out += [Violation("I12", "secret", f"a token appears in a {where}")
            for where, text in said if text and any(secret in text for secret in _secrets(w))]
    return out


INVARIANTS = (i1, i2, i3, i4, i5, i6, i7, i8, i9, i10, i11, i12)


def check(truth, db_path, scenario, events):
    """Every violation of I1..I12, in that order."""
    world = World(truth, db_path, scenario, events)
    return [violation for invariant in INVARIANTS for violation in invariant(world)]


def replay(run):
    """Recompute independent invariants AND preserve the harness's durable obligation verdict."""
    run = Path(run)
    truth, scenario, events = (json.loads((run / name).read_text(encoding="utf-8"))
                               for name in ("truth.json", "scenario.json", "events.json"))
    violations = check(truth, run / "state" / "core.db", scenario, events)
    try:
        result = json.loads((run / "run-result.json").read_text(encoding="utf-8"))
        if result.get("version") != 1 or result.get("completed") is not True:
            raise ValueError("incomplete or unsupported run result")
        violations += [Violation(**p) for p in result["problems"]]
        missed = [x for x in result["coverage"] if x.get("required", True) and x["outcome"] != "met"]
        if missed and not result["problems"]:
            violations.append(Violation("RUN", "coverage", f"{len(missed)} required obligations unmet"))
    except (OSError, ValueError, TypeError, KeyError) as error:
        violations.append(Violation("INCONCLUSIVE", "RUN", f"runtime evidence unavailable: {error}"))
    return violations


def main():
    """Replay a kept run, including required coverage and process failures."""
    if len(sys.argv) != 2:
        sys.exit("usage: python3 sim/invariants.py RUN_DIR")
    violations = replay(sys.argv[1])
    for violation in violations:
        print(f"{violation.invariant} {violation.subject}: {violation.evidence}")
    print(f"{len(violations)} violation(s)")
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
