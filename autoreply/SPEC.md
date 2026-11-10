> Public copy. References to `research/`, `audit/`, `third_party/` and `HANDOFF.md` point to the private working repository and are kept for traceability; the code and tests here are complete without them.

# Core spec: Instagram comment-to-DM bot (v2 "core")

Status: v2 core contract, reconciled with the 06.10.2026 local audit implementation. The original
five-angle design critique (`research/lessons/spec-critique-v0.json`) and attack-round regressions
(`tests/test_regress_r1.py`) remain background. Audit evidence and external release conditions live in
`audit/` at the repository root; local acceptance is not a production release.
This file is the single source of truth for builders, testers and reviewers. Where it conflicts with old
code or notes, this file wins. If you find a real gap, do not improvise silently: implement the safest
reading and list the gap in your hand-back.

Background (read what your task needs): `research/lessons/openreply.md`, `chatmany-and-small.md`,
`chatbotx.md` (lessons and scenario catalogues OR-xx, CM-xx, CX-xx), `meta-api-facts.md` (facts A..I with
confidence tags), `our-inventory.md` (the old v1 code: hard-won behaviours, issues K1..K19, A1..A15).
The v1 code is at git tag `v1-ab-webhook` and, for reading, in `third_party/v1/`.

## 1. What it does

One Instagram creator account. For each configured reel: read the top-level comments; when a comment
contains the reel's keyword, send that commenter ONE private reply (a DM with the link) through Meta's
official API, then post one public reply under the comment. Keep a journal, statistics, a reconciliation
report, and push alerts to Telegram while something needs a human.

OUT of scope (removed; preserved in git tag `v1-ab-webhook`): inbound webhook, button postbacks, follow gate,
A/B test, inbound DM handling, click tracking, any HTTP listener, any second thread, automatic fallback from
one message format to another, a STOP file.

## 2. Guarantees (the definition of "works")

- **G1** Nobody receives two first DMs for the same media id, whatever they comment and however often the
  process crashes or restarts.
- **G2** `POST /messages` calls, measured where they are received, never exceed `max_sends_per_hour` in any
  rolling hour, and are spaced at least `3600 / max_sends_per_hour` seconds apart (minus nothing). Within one
  process run the spacing is kept on the monotonic clock as well, so no step of the wall clock shortens it;
  across a restart a conservative monotonic cooldown is seeded from the journal (7.2).
- **G3** A DM is only sent in reply to a comment that the matcher accepts under the config loaded by the
  running process, re-checked at claim time, and never to the account's own comments.
- **G4** At most one `POST /messages` per comment may be answered by anything other than a definite
  account-wide refusal or a failure to connect. After any other answer the comment is never sent to again.
- **G5** Every comment Meta lists is stored; every matched comment ends in an explicit status with a reason;
  the report computes `unaccounted` (must be 0); and every state in which people are waiting or being lost
  is announced and re-announced until it ends.
- **G6** (safety) No DM is claimed unless ALL of: config `sending` is true; the send budget is > 0; no
  account hold; no active send hold; the account identity is known; the comment's reel completed a full
  sweep in this process run and its collect succeeded in this cycle. A public reply (only ever for a
  comment in status `sent`) needs all of those EXCEPT the budget. A fresh database has budget 0.

**G2 proof boundary.** The numeric receiver requirement above remains the acceptance target. Completed
HTTP responses have a proof using `max(started_at,finished_at)`, wall offsets within ±300 s and the
600.001 s offset/floor margin; the implementation uses a larger global cap margin of 660.001 s (7.2).
An interrupted or no-response request has no proved receiver-completion timestamp. Its additional
D=60 s cooldown is a practical mitigation, conditional on receipt no later than local return+D
(or replacement boot+D for an interrupted request). A local timeout, recovery stamp or process death does not prove that bound. Unconditional G2
for late unknown old flights remains unverified until a receiver bound is confirmed or an explicit
quarantine policy resolves them; local green tests do not waive this release condition.

A review finding is a bug only if it shows a scenario, in a supported configuration, that breaks G1..G6 or
loses people silently, and it comes with a failing test. Supported configuration: one process holds the
instance lock; database created by this version; Python 3.12+; wall clock within 5 minutes of real time.
A database that was rolled back in time (a restored server snapshot, a copied file) is outside the
guarantees; the protection that remains is the owner's public reply under every served comment (8.3).

## 3. Runtime shape

- One main bot process, one thread, one SQLite connection. At most one short-lived, isolated stdlib
  DNS-only child for a hostname lookup (6); numeric IPv4 needs no child. No network listener, worker pool
  or additional bot runner. Python 3.12 standard library only.
- Files under `autoreply/`: `bot.py` (thin CLI), `clock.py`, `config.py`, `match.py`, `meta.py`, `store.py`,
  `engine.py`, `report.py`, `notify.py`; tests in `tests/` (unittest); simulator in `sim/`.
- `STATE_DIR` env (default `<code dir>/state`) holds `core.db`, `core.lock`, `config.json` and `.env`.
  `CONFIG` env overrides the config path. `.env` is always `<STATE_DIR>/.env` and may contain
  `IG_ACCESS_TOKEN`, `TG_BOT_TOKEN`, `TG_CHAT_ID` (parser: `KEY=value` lines; strips whitespace,
  CR, surrounding quotes and a leading `export`; ignores `#` lines). It is re-read every cycle. The process
  environment is never used for secrets. Every command prints the absolute STATE_DIR and CONFIG it used.
  Reading `.env` never raises into the cycle or the notifier. The file is decoded as UTF-8; a byte order
  mark at its start is ignored and is never part of the first key; bytes that are not valid UTF-8 are
  replaced, so only the lines that contain them are affected. A file that exists but cannot be opened or
  read (any OSError) counts as a file without values for that cycle: no token is adopted, the stored token
  stays in use, and collect, expire and send run as usual. While that lasts, problem `env` is raised and the
  notifier keeps using the Telegram secrets it last read successfully in this process run (if it has none:
  `no_alert_channel`).
- Test/simulator overrides: `IG_API_BASE` (default `https://graph.instagram.com`), `TG_API_BASE` (default
  `https://api.telegram.org`), `CLOCK_SCALE` and `CLOCK_ANCHOR` (see `clock.py`). The process exits with
  code 2 if `CLOCK_SCALE` is not 1 while either base URL is not `http://127.0.0.1:<port>`.
- Lock: `run`, `once` and `send-one` take an exclusive non-blocking `flock` on `core.lock`.
  `once` and `send-one` exit with code 3 at once when it is held. `run` goes on trying once per
  second for up to 60 seconds (a one-off command holds the lock for one cycle only) and starts normally as
  soon as it gets it; if the lock is still held after that, it sends the event `fatal_start` (below) and
  exits 3. Exit 3 of `run` is never silent. `arm` and `disarm` take NO lock: each is exactly one
  `BEGIN IMMEDIATE` transaction and takes effect at the running service's next claim.
  `ack-uncertain COMMENT_ID` also takes no instance lock and writes only its acknowledgement in one
  transaction; it never claims or sends (7.1).
  `report`, `check`, `media` do not write the database. `report` and `media` require an existing DB
  (exit 5 if absent); `check` may use the token in `.env` without a DB and exits 5 if no token exists.
  `check --online` additionally performs GET /me and one Telegram test message. It preflights the token
  the next engine cycle would use without adopting it into the database: nonempty `.env` token wins
  only when different from `token_env_seen`; otherwise the stored refreshed token is used. Without a
  stored token or a new env value, check exits 5; without a DB, nonempty env is new. `media` keeps its
  stored-token contract.
- SQLite: `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=30000`. `store.init(conn)` creates the schema
  once (`PRAGMA user_version=2`); opening a connection never alters the schema. The first open of a new
  `core.db` switches it to WAL; SQLite refuses that switch without waiting while another connection touches
  the file, so `connect` retries it for up to `busy_timeout` like any other statement.
- Time: every timestamp is UTC `YYYY-MM-DDTHH:MM:SS.mmmZ` from `clock.now()`. All durations are named
  constants in code (dict `T` in `clock.py`); config carries only product choices. `clock.now()` =
  `anchor + (time.time() - anchor) * CLOCK_SCALE`; `clock.sleep(sec)` sleeps `sec / CLOCK_SCALE` in slices of
  at most 1 real second, calling the stop check and the watchdog ping between slices. HTTP timeouts are real
  time and are not scaled.
- Shutdown: SIGTERM/SIGINT set a flag; the loop finishes the HTTP call in flight and its database write,
  then exits 0. A one-shot startup admission sleep is cancellable; SIGTERM/SIGINT during that sleep
  exits 0 without a new DM claim or POST (8.4).
- Logging: stderr, one line per event: `<ts> <LEVEL> <event> key=value …`. Every POST logs
  `attempt id= comment= kind= http= code= subcode= outcome= reason=`. Unexpected exceptions log a traceback.
  `cycle_start` records pid, run id, cycle ordinal and real monotonic time before cycle work.
  `http_complete` records pid, real monotonic time, call ordinal, method, kind, safe route and HTTP status
  immediately before a Client result returns. `claim_committed` correlates pid, attempt id, kind, the exact
  `started_at` string used by the committed INSERT, and the
  real monotonic point captured inside the successful claim transaction, after gates and before its insert.
  It is emitted after commit and before POST; DM/public claim results carry this optional process-local
  point plus that same journal timestamp. The monotonic point is never persisted as wall history.
  A canonical stamp matching the journal row correlates the point without assuming startup wall and
  monotonic clocks remain aligned; unstamped legacy evidence retains conservative clock bounds.
  Engine/HTTP diagnostics write the entire line in one write,
  so sibling CLI output cannot split numeric fields. These are execution boundaries, not gate-success flags.
  Tokens, query strings, bodies and `paging.next` URLs are never logged; transport exceptions retain only
  safe error class/errno diagnostics.
- systemd: `Type=notify`, `WatchdogSec=300`, `Restart=always`, `RestartSec=30`, `TimeoutStopSec=120`,
  `RestartPreventExitStatus=3 4 6`, dedicated user, `MemoryMax=100M`, `UMask=0077`, `NoNewPrivileges=yes`,
  `Environment=STATE_DIR=/opt/ig-autoreply/state`, no `EnvironmentFile`. `sd_notify` (stdlib datagram
  socket; no-op without `NOTIFY_SOCKET`): `READY=1` after the lock is taken, the schema is ready and the
  start has written to the database (kv `config_running`) and `Engine.prepare_run()` durably queued the
  service-start notice inside the protected startup path, never after an API call; `WATCHDOG=1` after every
  HTTP call returns and in every sleep slice (also while `run` waits for the lock).
- Exit codes: 0 ok; 1 runtime error; 2 usage or unsafe environment; 3 lock held; 4 config invalid;
  5 no database / no token where required; 6 database unusable.
- A start of `run` that does not reach the loop is never silent and never a restart loop nobody heard of.
  Whatever ends it between taking the lock and the first cycle is told as the Telegram event `fatal_start`:
  an invalid config (exit 4); the lock held after
  the 60 seconds (exit 3); a database that is unusable: it cannot be opened, is malformed, has an unknown
  schema version, or refuses the write the start makes before `READY=1` (exit 6; the primary CLI diagnostic is one line;
  additional `notify_failed` diagnostics may include a traceback), including a failure to queue the prepared started notice; any other exception (exit 1).
  All four are sent with ONE direct `sendMessage` built
  from `.env`, without the outbox: the database may be unusable or not ours. Exit 3, 4 and 6 keep the service
  down (systemd does not restart them), so they are final only when Telegram CONFIRMED `fatal_start`. Unheard
  (Telegram not configured or not answering), `run` ends with exit 1 instead: systemd starts it again in 30 s
  and the start says it again; no request goes to Meta meanwhile. `once` and `send-one` end
  with their own code and without a message: their operator is at the terminal.

## 4. Config (`<STATE_DIR>/config.json`)

```json
{
  "api_version": "v26.0",
  "sending": false,
  "poll_interval_sec": 90,
  "max_sends_per_hour": 60,
  "public_reply": true,
  "profile_url": "https://www.instagram.com/your_account/",
  "greeting": "Привет! 👋",
  "late_greeting": "Привет! 👋 Лучше поздно, чем никогда 😄",
  "late_after_minutes": 60,
  "public_phrases": ["…", "…"],
  "reels": [{
    "name": "arena", "media_id": "17900000000000101", "permalink": "", "active": true,
    "keywords": ["арена", "arena"], "also_match": ["арееа", "areba", "arrna"], "max_words": 5,
    "link": "https://github.com/…",
    "text": "{greeting} Держи … 👇 …"
  }],
  "alerts": {"repeat_min": 120, "digest_hour_utc": 6}
}
```

`config.load(path) -> Config` (read-only object) or raises `ConfigError` whose `.problems` lists ALL problems.
Rejected: unknown keys at any level; missing required keys; duplicate reel `name` or `media_id`; `media_id`
not all digits; a keyword or `also_match` entry that contains a digit (the matcher reads letters only: the
digit would be dropped and the word would silently be another one), or that does not normalise to exactly one
token of at least 2 letters, or that its own reel's matcher does not accept as a one-word comment (`Matcher.check(entry)` must
be `match`; today that is an entry whose token or skeleton is a question word of section 5: such a reel
would answer nobody); empty `keywords`; a raw keyword/`also_match` string that is not UTF-8 encodable;
invalid HTTPS `link`/`profile_url` syntax (nonempty hostname and valid port required; reject raw ASCII
controls/DEL, whitespace and invalid UTF-8; validate syntax without DNS or resource probing);
`max_sends_per_hour` outside 1..300;
`poll_interval_sec` outside 30..300 (every 10-minute rule of section 9 must see at least two cycles);
`late_after_minutes` outside 0..10080 (7 days; no older comment is sent to, so 10080 means "never late");
`alerts.repeat_min` outside 1..10080; fewer than 2 `public_phrases` when `public_reply` is true; a public
phrase or a rendered DM that any reel's matcher would accept (the bot must never trigger on its own
words); a rendered DM over Meta's limit of 1000 UTF-8 bytes with EITHER greeting. A config that `check`
accepts starts.

Rendering, `config.render(cfg, reel, late) -> dict` (the JSON `message` object), pure. There is one
message format, a text: `{"text": rendered + "\n\n" + link + "\nПодписаться: " + profile_url}`; the `link`
line is left out when the rendered text already contains the link. `{greeting}` and `{link}` are optional
placeholders replaced with `str.replace`; any other brace is literal. `late` selects `late_greeting`.

`reel.active=false`: the reel is neither collected nor sent; its rows are reported as parked, and the
people among them still wait (section 9, reason `reel_off`). A config without any active reel loads
(rejecting it would silence `expire()` and every alert) and is problem `no_active_reel`. Rows join config
by `media_id` only; `name` is a label.

The config is read once at start-up; `bot.py check` validates a file without touching anything. The process
that takes the lock (`run`, `once`, `send-one`) stores what it loaded: kv `config_running` (the loaded
config as canonical JSON; it holds no secrets) and `config_loaded_at`. `report`, the digest and `arm`
describe gates, reels and waiting reasons from `config_running`, not from the file.

**Every config change needs a human `arm`** (a config edit must not mass-send by surprise). In the
transaction that stores `config_running`, the start compares the SHA-256 of that canonical JSON with kv
`config_hash`; if it differs or is absent, the budget is set to 0, the hash is stored and the event
`config_changed` is raised ("the config changed: sending is disarmed; read the report, then arm"). A start
with the config the database last saw changes nothing. The same hash tells an edit that has not taken
effect yet: when the file `report` or `arm` is given has another hash than `config_hash`, `report` raises
problem `restart_needed` ("config.json was changed after the service loaded it at <ts>; the service still
runs with the old values; a restart applies them and disarms") and `arm` prints the same warning. The
running service reads the file once a cycle ONLY to compare it: it applies nothing of it, and raises the same
`restart_needed` (or `config_file_invalid` when the file cannot be loaded) as a problem of its own, so that an
edit without a restart reaches Telegram. People under a reel that is only in the file are not read meanwhile.
The start also stores `code_hash` (SHA-256 of the nine module files): a start with other code than the database
last saw is treated like a changed config (budget 0, event `config_changed`): a deploy never sends without `arm`.
`report` alone can recover from an invalid or unreadable candidate config file: it validates the stored
`config_running`, reports that snapshot and raises `config_file_invalid` with safe diagnostics. An absent
or invalid snapshot still fails with exit 4. `check`, `arm` and sender startup remain strict; fallback
reporting is read-only and never repairs the file or database.
The repository ships `config.example.json`; the live file lives in STATE_DIR and is never overwritten by a
code deploy.

## 5. Matcher (`match.py`), pure

`normalize(text) -> list[str]`: Unicode NFC; lower-case; `ё -> е`; drop @mentions (`@` + non-space run);
tokens are maximal runs of letters (category L*); each token has runs of the same letter collapsed to one.
Never raises (None, empty, 10 kB of emoji, lone surrogates all give a list).

`skeleton(token)`: Latin look-alikes mapped to Cyrillic: `a e o p c x y k m h t b` -> `а е о р с х у к м н т в`.
No transliteration.

Keyword forms of a normalised keyword token `k`: `{k}`, plus, if `k` ends in «а», `k[:-1]` + `у`, `ы`, `е`,
`ой`. `also_match` entries are exact tokens with no derived forms. A token HITS when (a) it equals a form, or
(b) its skeleton equals a form (Latin look-alikes typed inside a Cyrillic keyword: «Aрена»), or (c) the
token contains at least one Latin letter (a-z) and its skeleton equals the skeleton of a form (Cyrillic
look-alikes typed inside a Latin keyword: «bоt»). A token without Latin letters never hits through the
skeleton of a form: keyword `bot` must not match «вот» (v1 issue K5).

`Matcher(reel).check(text) -> (kind, reason)`:
- `match`: at least one token hits AND token count <= `max_words` AND no question word among the tokens
  or their skeletons (зачем, почему, как, где, когда, сколько, какой, какая, какие, что, чё, че, нафига,
  нахрена) AND NOT (the text contains `?` and token count > 1). A lone keyword with `?` matches.
- `near` (never sent; listed in the report): a token hits but a condition failed; reason `too_long` or
  `question`.
- `none` otherwise (reason empty).

`decide(matcher, text, has_author, is_own) -> (status, reason)`: own comment -> `seen`/`own`; not `match` ->
`seen` with reason `near:<why>` or empty; `match` without author -> `seen`/`no_author`; else `pending`.

Fixtures: `tests/data/real_comment_texts.json` (the 20 distinct texts of 1,108 real comments) must all be
`match` with the example config. «Арина», «Ирена», «Алёна», «к чему это», «зачем арена?» must not match.
There is no fuzzy rule: unknown typos are found by a human in the report's list of frequent short
unmatched texts and added to `also_match`.

## 6. Meta client (`meta.py`)

Transport: `urllib` through an opener private to this module (nothing global is patched). Connections
resolve IPv4 only. A failure while connecting (DNS, TCP connect, TLS handshake) is the ONLY case that proves
a request did not reach Meta: `connect_failed=True`. Timeouts: 30 s per socket operation and a 60 s absolute
real-time budget per Meta call. Hostnames are resolved by at most one child running `sys.executable -I -S`
with a fixed DNS-only script, host/port arguments, an empty environment and no shell, credentials, DB,
listener or retries. Numeric IPv4 bypasses it. After process creation, the parent recomputes the remaining
budget for the resolver wait and socket operations; a failed/timed-out child is killed and reaped before
return. OS process creation, kill/reaping and scheduling are not hard realtime, so this is a bounded wait
policy, not a hard wall-clock completion guarantee. Linux `MemoryMax=100M` for the main process plus child
remains a release check.

No retries in this module, ever. The token is sent as the `access_token` query parameter. Response bytes
are bounded to **1 MiB before JSON parsing**, independently of absent, false or chunked Content-Length;
oversize, truncated and recursive/malformed responses are safe failures. A POST response read failure
is ambiguous and is never classified as a proved failed connection. Non-object JSON is not accepted as
successful object data. `exchange(method, url, body, deadline)` is that one HTTP exchange and never raises;
Telegram's `sendMessage` (9) goes through it with its own 10 s budget and the same resource boundaries.

```python
@dataclass
class Result:
    ok: bool; http: int            # 0 = no complete HTTP response
    data: dict                     # parsed body ({} if not an object)
    outcome: str; reason: str      # from classify()
    code: int | None; subcode: int | None; message: str
    connect_failed: bool
```
`code`/`subcode` are converted with `int()` when possible; subcode = `error.error_subcode` or `error.subcode`.

`Client(api_base, api_version, get_token, ping=None)` methods, each returning `Result`:
- `me()` -> `GET /{v}/me?fields=id,user_id,username,followers_count,media_count`
- `media(media_id)` -> `GET /{v}/{media_id}?fields=id,permalink,shortcode,comments_count,timestamp`
- `comments_page(media_id, next_url=None)` ->
  `GET /{v}/{media_id}/comments?fields=id,text,timestamp,from,hidden,replies{id,from}&limit=50`, or the
  given `paging.next` URL as is
- `my_media(next_url=None)` -> `GET /{v}/me/media?fields=id,permalink,shortcode,timestamp,comments_count&limit=25`
- `send_dm(ig_user_id, comment_id, message)` -> `POST /{v}/{ig_user_id}/messages`, JSON body
  `{"recipient": {"comment_id": …}, "message": …}`; ok = HTTP 200 and a non-empty `message_id`
- `public_reply(comment_id, text)` -> `POST /{v}/{comment_id}/replies?message=…`; ok = HTTP 200 and `id`
- `refresh_token(token)` -> `GET /refresh_access_token?grant_type=ig_refresh_token&access_token=…` (unversioned)

### 6.1 `classify(method, kind, http, body, connect_failed) -> (outcome, reason)`

`kind`: `dm`, `public`, `read_me`, `read_media`, `read_comments`, `refresh`. Pure. First applicable rule wins.

**POST (`dm`, `public`)**
1. `connect_failed` -> `not_sent` / `connect`.
2. No HTTP response (`http == 0`) -> `uncertain` / `no_response`.
3. HTTP >= 500 -> `uncertain` / `http_5xx` (whatever the body says).
4. HTTP 429 -> `global` / `rate` (whatever the body is).
5. HTTP 2xx: id field present -> `ok`; absent -> `uncertain` / `ok_without_id`.
6. Body has no integer `error.code` -> `uncertain` / `unparseable`.
7. Already replied, any spelling: code 10900; 100 with subcode 2534025; or the message contains
   "already has a reply" or "already replied" (case-insensitive) -> `rejected` / `already_replied`.
8. Codes 1, -1, 2 -> `uncertain` / `code_<n>`.
9. Per-item (HTTP 4xx):
   - 551; 200/1545041; 10903; 230; 100/2018001; 100/2534014; 100/2534001; 10/2018108; 10/1893015
     -> `rejected` / `user_unavailable`
   - 100/33; 24; 803 -> `rejected` / `comment_gone`
   - 10/2534022; 10/2018278; 10/2018065 -> `rejected` / `outside_window`
   - 100/2534015; 100/2534037; 10/2018300 -> `rejected` / `invalid`
   - kind `dm` only: code 20 -> `rejected` / `invalid`
10. Account-wide:
   - 190 -> `global` / `auth`
   - 368; 613/2018338; 4/2207051; 508; 10/1893063; kind `public` with code 20 -> `global` / `spam`
   - 4; 17; 32; 341; 613; 80000..80099 -> `global` / `rate`
   - 3; 10; 25; 200..299; 100/2534029; code 100 without subcode whose message contains
     "Unsupported request" -> `global` / `blocked`
11. Any other code 100 -> `rejected` / `invalid`.
12. Anything else -> `uncertain` / `unknown_code_<n>`.

**GET (`read_*`, `refresh`)**
1. `connect_failed`, `http == 0`, HTTP >= 500, or codes 1, -1, 2 -> `transient`.
2. HTTP 429 -> `global` / `rate`.
3. HTTP 200, object body, no `error` key -> `ok`.
4. The account-wide table (rule 10 above) -> `global` / reason.
5. `read_media`/`read_comments` with 100/33, 24 or 803 -> `gone`.
6. Anything else -> `error` / `code_<n>` (or `unparseable`).

Tests: a table-driven test with one row per line above, and a fuzz test (random statuses and bodies,
including non-dict bodies and string codes): never raises; never `not_sent` unless `connect_failed`;
never `ok` for a POST without the id.

## 7. Database (`store.py`)

```sql
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

CREATE TABLE attempts (                    -- append-only journal, one row per POST, never deleted
  id INTEGER PRIMARY KEY, comment_id TEXT NOT NULL, kind TEXT NOT NULL,      -- dm | public
  detail TEXT,                             -- dm: 'late' or 'fresh'; public: the phrase
  started_at TEXT NOT NULL, finished_at TEXT,
  http INTEGER, code INTEGER, subcode INTEGER, message TEXT,
  outcome TEXT, reason TEXT, meta_id TEXT);
CREATE INDEX idx_attempts_kind ON attempts(kind, started_at);
CREATE INDEX idx_attempts_comment ON attempts(comment_id);

CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE metrics (ts TEXT PRIMARY KEY, followers INTEGER, media_count INTEGER);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, key TEXT, text TEXT, created_at TEXT, sent_at TEXT);
CREATE TABLE problems (key TEXT PRIMARY KEY, text TEXT, since TEXT, last_announced_at TEXT);
```
Collect keeps no history in the database: every full sweep, every head poll that stored a new comment and
every failed sweep is one log line (`sweep` / `sweep_failed`, with its counters and, failed, Meta's answer);
an idle head poll only updates kv.
Nothing is ever pruned. Everything about a DM or a public reply (time, Meta ids, error) lives in
`attempts`; `comments` holds only identity, text and the current status. A text is stored as SQLite can hold
it: a lone surrogate (half an emoji) becomes U+FFFD, a character the matcher gives no meaning to, and
collect decides on the text as it is stored (the claim decides on that one again).

kv keys (complete list): `me_user_id`, `me_id`, `me_username`, `me_checked_at`, `send_budget` (integer or
`unlimited`; absent = 0), `armed_at`, `armed_n`, `armed_through_attempt_id`, `account_hold` (JSON),
`send_hold` (JSON),
`last_phrase`, `full_sweep_at:<media_id>`, `head_ok_at:<media_id>`, `reel_fails:<media_id>`,
`reel_state:<media_id>` (`ok`|`gone`|`mismatch`|`empty`|`capped`), `unreadable:<media_id>`, `code_hash`, `token`, `token_env_seen`,
`token_refreshed_at`, `token_expires_at`, `token_refresh_failed_at`, `metric_at`, `daily_at`, `last_now`,
`fresh_db`, `last_cycle_ok_at`, `last_cycle_error`, `config_running`, `config_loaded_at`, `config_hash`,
`env_problem`, `clock_problem`, `probe_fails`, `token_refresh_fails`, `told_expired`, `told_expired_at`, `told_uncertain`,
`tg_ok_at`, `tg_failed_at`, `tg_last_outcome` (`ok`|`failed`), `tg_configured`,
`uncertain_ack:<comment_id>` (first manual-review acknowledgement timestamp).
No decision counter lives in memory: every "N in a row" is a query over `attempts` ordered by id. Memory
holds only what belongs to one process run by definition: which reels were swept, the monotonic time of
the last POST/startup cooldown (G2), which comments were last listed hidden (7.4), the time of the last
alert that had to bypass the database, and accepted-but-not-yet-recorded Telegram confirmation (9).

### 7.1 Statuses

| status | meaning | terminal |
|---|---|---|
| `seen` | stored but not to be sent: reason `` (no match), `near:too_long`, `near:question`, `no_author`, `own`, `malformed`, `no_reel` (7.2 step 0) | no (re-decided on full sweeps) |
| `pending` | matched, waiting for a DM | no |
| `sending` | DM claim written, POST in flight | no |
| `sent` | DM confirmed by Meta, public reply not yet resolved | no |
| `done` | DM confirmed and public reply resolved (reason `` , `public_failed`, `public_uncertain`, `public_skipped`) | yes |
| `uncertain` | the DM may or may not have been delivered | yes |
| `refused` | Meta definitely refused this one: reason `user_unavailable`, `comment_gone`, `outside_window`, `invalid` | yes |
| `expired` | window passed before a DM could be sent (reason `window`) | yes |
| `dup_user` | same person already handled for this media (reason `dup:<comment_id>`) | yes |
| `already` | handled outside this database: `owner_replied`, `already_replied`, `migrated` (entered at deployment, 12) | yes |

`store.transition(conn, comment_id, was, to, reason='') -> bool`: a single
`UPDATE … WHERE comment_id=? AND status=?`; returns whether a row changed; raises `ValueError` for a
(was, to) pair not in this table: `seen->pending|expired|already`; `pending->sending|seen|expired|dup_user|
already`; `sending->sent|uncertain|refused|pending|already`; `sent->done`.

`uncertain`, `refused` and `expired` are final for good: no command and no rule puts such a comment back
into the queue. The report counts them per reel and reason and lists the newest `uncertain` people by
username (9). A person whose DM is in doubt needs the owner to check the actual dialog/Requests and
make a manual decision. `ack-uncertain COMMENT_ID` records that this manual case was reviewed/closed,
not that delivery was proved: the first timestamp is stored idempotently in `uncertain_ack:<id>`.
Only an existing `uncertain` row can be acknowledged (otherwise exit 1). No status, attempt, budget,
hold or API call changes. Acknowledged rows remain terminal and keep the person/media duplicate guard.
Historical `refused/suspect_item:*` rows are not reopened or rewritten; review them manually at cut-over.

### 7.2 `store.claim_dm(conn, comment_id, cfg, me) -> Claim(kind, attempt_id, late)`

One `BEGIN IMMEDIATE` transaction, committed before any network call. `kind` is `claimed` or a skip reason.
0. Row must be `pending` (else `skip`). Re-run `decide()` on the stored text with the current config of the
   reel found by `media_id`: not `pending` any more -> `seen` with the reason `decide()` gives; no active
   reel -> `seen`/`no_reel`; return `skip`.
1. Comment age >= 7 days minus 10 minutes -> `expired`/`window`, return `skip`.
2. Another row with the same `media_id` and `user_id` in `sending|sent|done|uncertain|already` ->
   `dup_user`/`dup:<that id>`, return `skip`.
3. Recheck sending, account/send holds and identity; a closed gate returns its reason. Budget is 0 or
   absent -> return `no_budget` (row unchanged). The engine supplies this cycle's full/current collect gate.
4. Spacing: the newest DM attempt's `max(started_at,finished_at)` plus the current slot
   `S=3600/max_sends_per_hour` has not elapsed -> return `wait`; so does a positive caller `slot_wait`.
   Within a process that monotonic slot starts after a completed response or a proved connect failure.
   For an unknown return it starts after `return_mono+D`, D=`http_deadline`=60 s. At boot, completed
   history uses minimum elapsed `E=max(0, wall_now-maxstamp-600.001)` and anchor `boot_mono-E`; old
   completed history therefore does not force a fresh slot at every once/send-one. Open, interrupted or
   http=0 uncertain history uses `boot_mono+D`. Waits are rounded conservatively to milliseconds.
   Cap: count ALL DM attempts (including open and failed) whose
   `MAX(started_at,COALESCE(finished_at,started_at)) > now-(3600+660.001)`; if count >= the current cap,
   return `cap`. Lowering the cap retains old history; it never resets the rolling count.
   The 600.001 s completed-response proof covers the ±300 s offset difference and millisecond floor;
   the larger 660.001 s global margin adds the practical D guard. Recovery end stamps do not prove remote
   receipt before recovery, and this guard does not settle the unknown-flight limitation in section 2.
5. Set status `sending`; insert the `attempts` row (`started_at`, no outcome); decrement an integer budget
   by 1. The budget is NEVER refunded. When the old integer budget is 1, return the stage snapshot
   `(armed_n, armed_through_attempt_id)` from this same transaction with the claim; otherwise no stage.
`late` = comment older than `late_after_minutes`.

### 7.3 `store.record_dm(conn, attempt_id, result) -> new_status` (one transaction: journal row + comment)

| outcome | comment becomes |
|---|---|
| `ok` | `sent` |
| `uncertain` | `uncertain` + reason |
| `not_sent` | `pending` |
| `global` | `pending`, `deferrals + 1`; preserve account/send holds, with no inference of a per-person refusal from another person's success |
| `rejected` / `already_replied` | `already` / `already_replied` |
| `rejected` / anything else | `refused` + that reason, final at once |

`store.recover_stale(conn) -> int` runs at the top of every cycle: every `sending` comment becomes
`uncertain`/`interrupted`, every `attempts` row without `finished_at` is closed with outcome `uncertain`,
reason `interrupted`; a `sent` comment whose open attempt was `public` becomes `done`/`public_uncertain`.
(Single thread plus the lock: any open claim at the top of a cycle belongs to a dead run.)

### 7.4 Public reply

Only for a comment in `sent`. `store.claim_public` inserts the `attempts` row (`kind='public'`, the phrase in
`detail`) in one committed transaction; then ONE POST; `store.record_public` closes the row and moves the
comment to `done`: `ok` -> reason ``; `rejected` or `global` -> `public_failed`; `uncertain` or `not_sent`
-> `public_uncertain`. A comment never gets a second public POST. `public_reply: false` -> `done`/
`public_skipped` without a POST. A `global` outcome also sets the send hold (8.5). Phrase = random choice
excluding kv `last_phrase`.
If a sweep shows a reply by the account under a comment that is `sent`, the comment becomes `done` (reason
``) without a POST.
A comment whose latest listing in this process run had `hidden=true` gets no public POST (Meta takes no
reply under a hidden comment): after its DM it becomes `done`/`public_skipped`, and no `public` row is
written to `attempts`. The DM itself is sent as for any other request. (Every sendable reel had a full
sweep in this process run, so the engine keeps the hidden ids of that run in memory.)

## 8. Engine (`engine.py`)

### 8.1 Cycle (`Engine.cycle()`), repeated by `run`

```
at start-up, once: kv config_running; another config than the last one disarms (4)
run only: prepare_run() queues started before READY (idempotent); once/send-one skip this notice
cycle_start execution boundary before cycle work
recover_stale(); clock guard; read .env (token, Telegram)
if no account hold: collect()          # per active reel, isolated
elif probe is due:  probe()            # GET /me; success lifts the hold
expire()                               # database only: pending rows past the window -> expired
if gates open: send()                  # public replies owed, then DMs
housekeeping()                         # token upkeep, hourly metric, daily digest
notifier.sync(report.problems(...)); notifier.flush()
sleep: until the next send slot if DMs are flowing, else poll_interval_sec
```
`expire()` and the problems/notify step run in EVERY cycle, hold or not. An unexpected exception anywhere in
a cycle is logged with a traceback, the transaction rolled back, kv `last_cycle_error` set (problem
`cycle_error`), and the loop continues at the next poll. The loop never exits on its own.
Clock guard: if `now() < kv last_now - 5 min`, or wall time advanced more than 10 min beyond monotonic time
since the last cycle, skip `expire()` and `send()` for this cycle and raise problem `clock`. A smaller step
(the supported 5 minutes) passes the guard; G2 does not depend on it, because the slot is also kept on the
monotonic clock within a process run (7.2 step 4).
Alerting must not depend on a database that can be written. When a cycle fails and the failure cannot be
stored or queued (the write of kv `last_cycle_error`, of the outbox or of table `problems` raises), or the
alert step itself raises, the engine sends ONE Telegram `sendMessage` directly, bypassing the outbox:
"⚠ db_unwritable: <ErrorClass>: <message>; …nothing is read or sent" (key `cycle_error` when the error is
not one of SQLite or the file system). It is sent at once, repeated every `alerts.repeat_min` while cycles
keep failing this way, and followed by "✓ cleared: db_unwritable" after the first cycle without it. The
time of the last direct message is kept in memory; the line is also logged as `ALERT`. An outbox batch that
Telegram accepted but that could not be marked sent is never sent again: it is marked first, or nothing
is sent. The recovery "✓ cleared" line is queued durably with `queue_clear` before the in-memory mute
is dropped, and flushed promptly; a queue failure retains the mute for a later recovery attempt.
Accepted outbox ids, captured completion time and Telegram health commit as one result transaction.
An accepted-but-unmarked batch blocks later sends until that complete result can be committed. The
accepted confirmation is process memory: death between Telegram acceptance and commit can repeat an
alert after restart; exactly-once Telegram alerts across that boundary are not promised.

### 8.2 Identity and token

`me` = `{user_id, id, username}` is stored in kv after every successful `GET /me` and reused at start-up.
The identity is known only with all three fields. A field that was known is never forgotten because one
answer left it out; an answer from which the three cannot be completed is a failed probe (`no_identity`).
No stored identity -> account hold `no_identity` until a probe succeeds; collect does not run without it.
A comment or reply is the account's own when `from.id` is `user_id` or `id`, or `from.username` equals
`username`. A later `/me` with a different `user_id` -> account hold `blocked`, detail `identity_changed`.

Token: `.env` `IG_ACCESS_TOKEN` different from kv `token_env_seen` is adopted (new token wins;
`token_refreshed_at = now`, `token_expires_at` cleared) and triggers an immediate probe. No token anywhere
-> account hold `no_token` and no network call. Refresh when the token is at least 24 h old and (older than
7 days or `token_expires_at` unknown), at most once per 6 h after a failure; success stores the returned
token and a usable `expires_in`. Malformed, bool, nonpositive, nonfinite or overflowing expiry metadata
leaves expiry unknown while still adopting a valid refreshed token and resetting refresh failure state;
the normal minimum-age/backoff rules continue. Refresh outcomes never set a hold. Problem `token` when two refreshes failed in a
row or fewer than 14 days remain. The token is never written to logs, alerts or the report.

### 8.3 Collect (per active reel; one reel failing never stops the others)

- **Full sweep**: the first collect of a reel in this process run, then every 30 min, and again in the next
  cycle if it failed. Starts with `media(media_id)`: `gone` -> `reel_state=gone`; a configured `permalink`
  whose shortcode differs from Meta's -> `mismatch`; both skip the reel until the next full sweep. Then every
  page until there is no `paging.next`, at most 400 pages (hit -> `reel_state=capped`, problem, no silent
  cap). Zero comments while `comments_count > 0` -> `reel_state=empty`.
- **Head poll** (other cycles): page 1, then follow `paging.next` while the page just read contained at
  least one comment id that was not in the database.
- Pages are handled one at a time; each comment in its own try/except (a malformed one, e.g. no timestamp, is
  stored as `seen`/`malformed` with `ts = first_seen_at` and counted in the `sweep` log line).
- An element without a usable comment ID, or with a `text` that is not a text, cannot be read: it is counted
  (`unreadable` in the `sweep` line), a full sweep stores the count in kv `unreadable:<media_id>` (absent when 0),
  and problem `reel:<media_id>` is raised while it is above 0. The reel stays read and sendable: such an element
  costs only itself. Never invent a comment ID.
- For each identifiable comment, in this order:
  1. A reply by the account in `replies` counts only under a comment that is a REQUEST: the row is `pending`,
     or (new or `seen` row) `decide()` on its text with the fetched author gives `pending` ->
     `already`/`owner_replied` (whatever its age). If the row is `sent` -> `done`. A reply by the account
     under any other comment changes nothing: the comment is stored or kept as `seen` with `decide()`'s
     reason and is re-decided as usual. `already` therefore only ever marks a request, and nobody is written
     off (7.2 step 2) because the owner answered their question. Stated cost: after a lost database together
     with a keyword edit, a person served under the old keywords can be served once more.
  2. New comment: `decide()`; `pending` if inside the window, `expired`/`window` if older.
  3. Existing `seen` row, on full sweeps only: re-run `decide()` with the fetched author AND the text Meta
     lists now (a person may edit a comment), updating the decision and reason even if it stays seen; the row
     is written only when something changed. The stored text is kept only when the listing has none. The
     timestamp is repaired only for a malformed first observation; healthy timestamps, `pending` rows and
     terminal rows are never changed. Now `pending` -> transition (or `expired` if outside the window).
- A config edit can turn any number of stored comments into requests at the first full sweep after the
  restart. They are sent to only after a human has armed again: the start with the edited config set the
  budget to 0 (4).
- Outcomes: `ok` -> kv `head_ok_at`/`full_sweep_at`, `reel_fails=0`, `reel_state=ok`. `transient`/`error`/
  `gone` on a page -> the reel is skipped this cycle, `reel_fails+1`. `global` -> run `probe()` at once: if
  the probe is not ok, set the account hold and end collect; if the probe is ok the failure is the reel's
  (`reel_fails+1`).
- A reel is SENDABLE in this cycle only if it completed a full sweep in this process run AND its collect in
  this cycle was ok.

### 8.4 Send

Gates (G6) with a distinct `waiting_reason` for the report: `reel_off` (the reel is switched off),
`sending_off`, `no_budget`, `account_hold:<r>`, `send_hold:<r>`, `no_identity`, `no_sweep`, `cap` (waiting for
the next slot: normal), `stalled` (every gate is open and still nothing goes out). The reason is `cap` only
while the slot or the hourly cap is taken by a DM attempt that is still open or that reached Meta (outcome
other than `not_sent`); a `not_sent` attempt never makes the reason `cap`.
1. Public replies owed: for each `sent` comment of a sendable reel, do 7.4 (2-4 s apart).
2. DMs: `pending` rows of sendable reels `ORDER BY deferrals, ts` (oldest comment first, deferred ones
   last). For each: `claim_dm`; `skip` -> next row; `no_budget`/`cap` -> stop; `wait` -> stop (the cycle's
   final sleep is shortened to the slot); `claimed` -> `send_dm` -> `record_dm` -> on `sent`: wait 2-4 s,
   public reply -> then stop sending for this cycle if the outcome was not `ok`.
   At most 10 DMs per cycle so that collect runs between batches. Ordinary `run` never blocks a cycle
   inside a startup rate wait: collect/alerts keep running and the normal sleep schedules another cycle.
   `once`/`send-one` may perform ONE cancellable `clock.sleep(wait, stop, ping)` for the first eligible
   boot-cooldown wait, then re-run checkpoint and claim gates (including current budget/holds/cap).
   Without an eligible pending target, identity, budget or open gates there is no startup sleep. Further POST pacing
   is nonblocking. A still-full cap after the one sleep can return without a DM; there is no cap bypass.
   With the 4260.001 s conservative cap window, cap=1 or clock correction can withhold another claim for
   roughly 70–80 min, not just one spacing slot. SIGTERM during the sleep exits 0; watchdog slices continue.
3. After recording the last claim of a finite stage, raise `stage_done` using the stage snapshot captured
   by that claim transaction (ok, uncertain, refused by reason). Totals use DM attempt ids after the
   original `armed_through_attempt_id` and at most this claim id; wall timestamps may move backwards
   or collide. A concurrent arm/disarm during HTTP cannot change this completed stage or its totals;
   there is no extra post-claim budget read before POST.

### 8.5 Holds (two independent kv JSON values)

`account_hold` `{reason, since, n, next_probe_at, detail}`, reasons `no_token`, `no_identity`, `auth`,
`blocked`, `rate`. Blocks reads and sends. Set when a probe (`GET /me`) is not ok with a `global` outcome
(or transient three times in a row -> reason `blocked`, detail `unreachable`), and by the token rules.
Lifted ONLY by a successful probe. Probe schedule after it is set: 5, 10, 20 min, then every 30 min
(`rate`: first probe after 15 min).

`send_hold` `{reason, until, detail}`. Blocks DMs and public replies; reads continue. `until` only moves
later. Set by the outcome of a POST:
| trigger | reason | wait |
|---|---|---|
| DM outcome `uncertain` | `uncertain` | 15 min x 2^(k-1), max 6 h, k = consecutive uncertain DM attempts |
| POST outcome `global` with reason auth, blocked or rate | `send_blocked` | 30 min x 2^(k-1), max 12 h, k = consecutive global POST attempts; plus an immediate probe (a failed probe sets the account hold too) |
| POST outcome `global`/`spam` | `spam` | 24 h, and the budget is set to 0 (a human must re-arm) |
| 5 DM attempts in a row without `ok` | `streak` | 1 h x 2^(n-5), max 12 h |

In the streak, attempts with outcome `not_sent` and attempts answered `rejected`/`already_replied` are passed
over: they neither count toward the 5 nor end the run. An `already_replied` answer settles its own comment
as `already` (7.3) and never sets or lengthens a send hold (that person HAS the reply).
`not_sent` sets no hold: the comment returns to `pending`, sending stops for this cycle, the next cycle
tries again (a queue that only ever gets `not_sent` is `waiting`/`stalled`, section 9). `arm` lifts no hold.

### 8.6 Arming

`bot.py arm N` SETS the budget to N (repeating the command is harmless); `arm all` sets `unlimited`;
`disarm` sets 0 and is the emergency brake. Each stores `armed_at`/`armed_n` and raises the event
`armed`/`disarmed`. The same SET transaction captures `armed_through_attempt_id=MAX(attempts.id)` (0
when absent); an earlier committed claim belongs to the previous arm epoch even if its POST happens
later. Every DM claim consumes integer budget, including a later `not_sent`; there is no refund or
summation of earlier arm values. `arm` prints: people waiting (as defined in section 9, for the active reels of the
running config), the hours needed at the current rate, and how many would expire before their turn; it
says so when config.json was changed after the service loaded it (4: the restart that applies the edit
disarms again). `arm` refuses (exit 1) when Telegram is not configured or its last delivery failed, unless
`--deaf` is given. Besides `disarm`, the budget is set to 0 by a start with another config (4) and by a
POST answered `global`/`spam` (8.5).

### 8.7 Housekeeping

Text rendering shows C0/DEL/C1 controls in near-comment bodies as visible `\\xNN` escapes; stored text,
matching and the JSON report retain their original values. Normalized unmatched tokens already remove
those controls; this is not a blanket escaping rule for every CLI field.

Hourly: `metrics` row from the latest `GET /me`. Optional followers/media counts must be exact
non-boolean integers in [0, 2**63-1]; other values are unknown (SQL NULL), without changing identity
or interrupting the cycle. Daily at `digest_hour_utc`: the `digest` event. At
start-up: a database whose schema was just created sets kv `fresh_db=1`; `fresh_db=1` raises the event
`fresh_db` once and is cleared after the first ok full sweep. Lost-person `expired`/`uncertain` events
and their `told_*` markers share one transaction; an explicit queue refusal fails that transaction.
Compute the daily digest before writing markers; its event and `daily_at` also commit together. A failed
queue/marker write leaves the event eligible for a later cycle.

**Digest.** The `digest` event is a summary built from `report.build`, not the rendered report. It always
fits one Telegram message: payload <=4000 UTF-16 units, prefix and envelope <=4096, preserved by the
notifier without a global end cut. Normal fixed order: verdict; problems (key and text); per reel:
people waiting, reason and status counts; DMs attempted/confirmed in 24 h; all available top-20 unmatched
short texts with counts (each text cut to 40 characters); `near` count; followers now/24 h/7 d.
On overflow remove optional count lines first, then shorten descriptions with an explicit footer. If
arbitrarily many reels/problem keys still cannot fit, reserve a bounded verdict, truthful total problem
count and first five bounded keys, all top-20 typo entries, 24 h totals, near/followers and an explicit
omissions/full-report footer. Bounded reel labels and counts use remaining space. Unlimited problem
details cannot all fit verbatim; the footer says so. A caller limit too small for mandatory content
raises ValueError rather than silently dropping typo rows; production uses the default limit.

## 9. Report and alerts

`report.problems(conn, cfg, now) -> list[(key, text)]`, pure over the database, is the ONE definition of
"something is wrong". It is printed first by `bot.py report` and evaluated every cycle by the engine.
Keys:
- `hold:account` (text: reason, since, next probe), `hold:send` (reason, until)
- `waiting`: people wait (or `sent` rows with a public reply owed exist) and there has been no `ok` DM for
  30 min, for any reason except `cap`. Text: how many wait, the reason (`reel_off`, `sending_off`,
  `no_budget`, a hold, `no_sweep`, or `stalled` when every gate is open; for `stalled` also how many DM
  attempts in a row could not connect), age of the oldest, hours until the first expires. A switched-off
  reel has no exception: its people wait with reason `reel_off`, and for them the 30 minutes are counted
  over the DMs of their own reel. While DMs fail to connect (`not_sent`) the reason is `stalled`, never
  `cap` (8.4): the problem is not reported as cleared while that lasts.
- `expiring`: N people will lose their window within 24 h, or will lose it before their turn at the
  current rate (people of a switched-off reel included).
- `unaccounted` > 0 (definition below)
- `unservable`: there are comments inside the reply window stored as `seen` with reason `no_author` or
  `malformed` whose stored text the reel's matcher accepts: requests that cannot be served as Meta lists
  them, for ALL configured reels including inactive ones. Text: how many per reel and reason, the newest
  comment id, what to check (for `no_author`: the
  token's comment permissions). It lasts until a full sweep re-decides the rows (8.3 rule 3) or they leave
  the window; it never makes a reel unsendable.
- `uncertain_manual`: every terminal uncertain row without `uncertain_ack:<id>`, across all media and
  ages, remains a recurring manual-review problem after its send hold expires. Text gives the total,
  asks for delivery/Requests review and a manual decision, then `ack-uncertain COMMENT_ID` (7.1).
- `refusals`: of the newest 20 DM attempts that reached Meta within the last 24 h, at least 5 ended
  `rejected` with one and the same reason other than `already_replied` and `user_unavailable` (those two
  say something about that one person; one commenter in ten cannot be written to on an ordinary day). A
  query over `attempts`; both numbers are named constants. Text: K of N, the reason, code/subcode and
  message of the newest such attempt. It sets no hold; 8.5 stays as it is.
- `no_active_reel`: the running config has no reel with `active: true` (the `reels` list is empty or every
  reel is switched off). The bot then reads no comments and sends nothing; text with the names of the
  switched-off reels.
- `reel:<media_id>` (active reels only; an inactive reel raises no `reel:` problem by itself): state not
  `ok`, or 3 failed sweeps in a row, or no ok full sweep for 2 h while there is no account hold
- `token`, `cycle_error`, `clock`, `env` (3), `no_alert_channel` (Telegram not configured)
- `not_running` (report only): nobody holds `core.lock`, or `last_cycle_ok_at` older than 10 min.
- `restart_needed` (4): the config file is not the config the service runs with; raised by `report` and, once
  a cycle, by the running service itself.
- `config_file_invalid` (4): the file cannot be loaded but a validated `config_running` is available; the
  service keeps running with that snapshot (and would not start again), `report` uses it and never writes a repair.

"People waiting" (the report per reel, the counts in the `waiting` and `expiring` texts, the `arm` forecast)
are the distinct (media_id, user_id) that have a `pending` row and NO other row of the same media_id and
user_id in the set of 7.2 step 2. A pending row of a person who is already handled takes no turn and is
never counted as waiting or expiring; the claim settles it as `dup_user` when it gets there.
The verdict is `OK` only without any problem; `WAITING` when the only problems are `waiting`/`expiring`;
else `PROBLEM`.

`unaccounted` = `sending` comments + `attempts` without `finished_at` (both only if older than 3 min) +
`sent` comments whose reel has been sendable with no hold for 10 min + `sent`/`done` comments without an
`ok` DM attempt + `pending`/`sent` rows whose media is not in the config (rows of an inactive reel are
"parked", not unaccounted).

`notify.Notifier(conn, get_tg, api_base)`:

- `sync(problems, now)`: a key not in table `problems` -> insert and queue "⚠ key: text"; a lasting key ->
  queue again when `repeat_min` passed since `last_announced_at`; a key that disappeared -> delete and queue
  "✓ cleared: key".
- `event(key, text)`: queue once. Events: `started`, `fatal_start`, `config_changed` (4), `armed`,
  `disarmed`, `stage_done`, `expired` (the count of `expired` rows rose: "K people lost"; the first at once, then at most once per `alerts.repeat_min` with everything since), `uncertain`
  (count rose), `fresh_db`, `digest` (8.7). Return a positive durable outbox id or `False` if queuing fails;
  lost-person/daily marker callers test explicit `False` before committing the marker.
- `queue_clear(key)`: durably queue raw "✓ cleared: key", returning id/False. Engine forgets its direct
  alert mute only after success and flushes promptly.
- `direct(key, text)`: one line sent at once, past the outbox, for the moments in which the database cannot
  hold it (`fatal_start` without a usable database, `db_unwritable`, 8.1).
- `flush(only_id=None)`: the oldest whole lines that fit go out as ONE Telegram `sendMessage` (10 s
  budget); overflow remains queued. `only_id` restricts an urgent final attempt to the fresh fatal row,
  after any prior accepted confirmation is committed. `sent_at` is set only on `ok:true`; otherwise
  rows remain queued. No write transaction spans the HTTP call. Accepted ids, captured completion time,
  `tg_ok_at`/`tg_failed_at` and `tg_last_outcome` commit atomically; pending accepted-result recovery uses
  the same helper and blocks later POSTs while recording fails (8.1). Logical outcome determines health
  independent of wall ordering; absent/invalid legacy outcome with any failure timestamp is conservative
  failure until a new result. Every queued line is also logged as `ALERT <key> <text>`.
  `sync`, `event`, `queue_clear`, `direct` and `flush` guard exceptions and expose failure/broken state;
  caller marker transactions still must reject explicit event failure.

`bot.py report` prints, in this order (`--json` gives the same data): a one-line verdict (`OK`, `WAITING:
…` or `PROBLEM: …`); the problems; gates (sending, budget, holds, identity, token age and expiry, Telegram
status, whether the service is running); per reel: counts by status and reason, people waiting, oldest
pending age and time left, last ok head poll / full sweep; DMs per hour for the last 24 h; attempts by
outcome and reason; total `uncertain_unresolved` and newest 30 UNACKNOWLEDGED `uncertain` people
(username, reel, reason: manual review required, 7.1); `near` comments and the 20 most frequent unmatched texts of at most 3 words (reason ``);
followers now and 24 h / 7 d ago. When `restart_needed` is raised the report describes the config the
service runs with.

## 10. CLI (`bot.py`)

`run` | `once` (one cycle, then exit) | `check [--online]` (validate config and `.env`; `--online` also
`GET /me` and one Telegram test message) | `report [--json]` | `media` (recent media: id, permalink, time,
comments) | `arm N|all [--deaf]` | `disarm` | `send-one COMMENT_ID` (one claim+send
through the normal path, every guarantee applies) | `ack-uncertain COMMENT_ID` (manual-review
acknowledgement only, 7.1). One-shot commands can wait once for startup admission and then recheck all
gates (8.4). `send-one` returns exit 1 when no DM is confirmed, including a cap still closed after that
wait; cancellation by SIGTERM/SIGINT exits 0.

## 11. Simulator (`sim/`), black-box

`sim/fake_meta.py --scenario FILE` prints one JSON line `{"api": port, "control": port}` and serves:

- the API port: exactly the calls of section 6 plus Telegram `POST /bot<token>/sendMessage`. STRICT: an
  unknown path, method or parameter is an error; the version prefix is required; `access_token` must equal
  the current token (else code 190); `/messages` only under the professional `user_id`; the public-reply
  `message` only as a query parameter; a DM `message` only as `{"text"}`, at most 1000 bytes; only
  requested fields returned; pages newest first, at most 50, `paging.next` absolute URLs;
  `comments_count` counts replies too.
- Meta's rules: one private reply per comment (a second one is answered with one of the three
  already-replied spellings, chosen by the scenario, and may or may not be delivered); the 7-day window;
  delivery is INDEPENDENT of the answer for every fault (code 1, -1, 2, 5xx, 200 without id, dropped
  connection: each can be injected with or without delivery).
- the control port: `GET /truth` (every request with sequence number, time, method, path, parameters,
  response, fault id; every delivered DM and public reply; every Telegram message); `POST /mode`
  (`ok`, `blocked`, `token_dead`, `dev_mode` (empty comment lists), `throttled`, `messaging_blocked`);
  `POST /faults` (add, clear); `POST /comments` (add, delete, hide, add owner reply); `POST /hold`
  (pause a given request before or after processing so a kill can be placed deterministically);
  `GET /clock`. The fake uses the same `CLOCK_SCALE`/`CLOCK_ANCHOR`.

`sim/invariants.py` checks ground truth against the database after the bot has settled:

- I1 delivered DMs per (media, user) <= 1.
- I2 `POST /messages` receive times: at most the cap in any rolling scaled hour; gaps >= the slot. No
  restart starts the count again.
- I3 every `POST /messages` targets a should-match comment that is not the account's own.
- I4 per comment, at most one `POST /messages` whose answer was not `global` or a refused connection.
- I5 status vs truth: `sent`/`done` => exactly 1 delivered; `refused`/`expired`/`dup_user`/`seen` => 0;
  `uncertain` => 0 or 1; `already` => as the scenario says; delivered => status in `sent`,`done`,`uncertain`.
- I6 public replies only under comments with a delivered DM, at most one each.
- I7 `sending=false` for the whole run => zero POSTs; each arm/disarm is SET. Budget is checked against
  committed DM claim ids in the matching epoch after `armed_through_attempt_id`, including not_sent,
  not against the sum of operator values or receiver POST counts. Pre-boundary claims belong to the old
  epoch; claims after disarm/config reset are forbidden until a valid next arm. No blanket +1 allowance.
  While the fake is in a blocking mode the bot makes no POST after the first refusal until the mode ends.
- I8 completeness: every comment the fake ever listed has a row, `pending`-or-later iff should-match (no
  exception for a comment the owner replied under).
- I9 justification: `refused` only if the fake's last answer to that comment's DM was per-item; `uncertain`
  only if the fake gave an ambiguous answer or the bot was killed between claim and record; `expired` only
  if the comment really aged out while a gate was closed or the cap could not reach it; `dup_user` only if
  the same person has the specific canonical `dup:<id>` comment handled BEFORE the decision. A later
  success, future final state or any earlier failed POST is insufficient; global refusals never justify
  new suspect-item terminal rows.
- I10 progress: once faults stop and gates are open, every should-match comment inside the window reaches a
  terminal status, and nothing is left `pending` without a reported `waiting_reason`.
- I11 payloads: the DM contains the reel's link and the right greeting; the public reply is one of the phrases.
- I12 alerts: every blocking mode, closed gate with people waiting (a switched-off reel is one), every
  request that cannot be served (`unservable`), and every expired/uncertain person is announced on Telegram
  (when the fake's Telegram is up) within the scenario's deadline; report is an object with a root
  `unaccounted` of exact integer type >=0 (not bool/string/float or a nested substitute), equal to 0 at
  the end. Lost-person acceptance requires a complete positive count event with its exact key and
  cumulative count coverage in accepted Telegram truth; cleared/digest mentions and queued-only rows
  do not count. Acceptance does not require outbox sent_at, since a confirmation write can fail after
  receipt. Generic explicit scenario `alert.contains` remains literal fragment/count matching.
  Independent journal/comments reconciliation still applies.
- G6 admission: independently reconstruct identity, full sweep in the current process and current-cycle
  collect from startup snapshots/raw fake responses. Correlate claims with pid/process ordinal, safe
  routes and Client `http_complete` timestamps; `cycle_start` resets only current collect. Exact claim
  points must match the committed journal id/kind and owning process; missing facts retain conservative
  wall-time precision, and malformed/incomplete telemetry yields INCONCLUSIVE. Check DM and
  public claims, including claims without a received POST. The fake's after-write time is not a lower
  bound for client completion, and engine sendable/full flags are never oracle truth. Missing telemetry
  or uncertain clock mapping yields INCONCLUSIVE, not a complete PASS.

`sim/soak.py --seeds N`: runs the real `python3 bot.py run` as a subprocess against the fake with a scaled
clock, random scenarios from a seed (comment streams, duplicates per person, own comments, near-misses,
fault schedules, mode changes, `kill -9` placed with `/hold`, SIGTERM, restarts, a second runner attempt,
arming in stages, disarm, a config change across a restart),
then lets faults end, waits for I10, and checks I1..I12. A failing seed prints the seed and keeps its state
directory. Placement of kills is deterministic for a given seed. Required `until`, kill, enabled settle
and installed faults (initial fake faults and step faults) must actually fire/complete with their minimum
coverage. Harness `required`, `reason` and `min_fires` metadata is removed before strict fake input. An optional action requires
an explicit reason; a missed optional action remains visible. Persist runtime failures and coverage via
atomic `run-result.json` replacement while running; write completed only after evidence is collected.
Replay recomputes independent invariants AND retains durable RUN/timeout/kill/fault failures. Legacy
catalogues without runtime evidence or completion boundaries are INCONCLUSIVE/exit 1. Graceful stop
allows 120 real seconds for an in-flight call, independent of clock scaling.

## 12. Deployment and cut-over

Deployment target: code in `/opt/ig-autoreply/`; state in `/opt/ig-autoreply/state/` (`core.db`,
`core.lock`, `.env` mode 600, `config.json`), owned by `igbot`; installed unit/limits as in section 3 and
wrapper `/usr/local/bin/igbot` as that user. This is a release procedure, not evidence that the server
currently has these bytes, paths or state. See `audit/RELEASE-GATES.md` and `audit/OPERATIONS.md` in the repository (not deployed) and `HANDOFF.md`.

Updates: a push to GitHub `main` is installed by `deploy/update.sh` (signed webhook, 5-minute timer as fallback).
It changes only the files listed above plus `VERSION`; when any of them differs it runs `igbot disarm` first,
then restarts the service, so sending stays closed until the next `arm`. STATE_DIR is never written by it.

Before first start, with BOTH old and new senders stopped/disabled:

1. Make an authoritative v1 export and reconcile served media/user counts; preserve code/database backup.
2. `deploy/enter_v1_served.py CODE_DIR STATE_DIR ROWS_JSON` imports ONLY v1 `done|dm_sent` rows as
   `already/migrated`, keyed by real `comment_id`, `media_id` and `user_id`, with normalized timestamps.
   It takes exclusive nonblocking `core.lock` before opening the DB (exit 3 if held); this defensive lock
   does not replace stopping v1, whose lock model is separate. Invalid/incomplete input or conflicting
   identity fails with exit 2; valid repeats change no row. Stored non-migrated/terminal rows are never
   overwritten. The whole accepted DB batch commits together; this is data atomicity, not a promise of
   filesystem rollback of a newly created SQLite/WAL/lock file. Re-run and reconcile exact counts.
3. DM-in-doubt v1 history needs a SEPARATE reviewed mapping/import to terminal `uncertain` and real
   identity. The served-only helper ignores those statuses and does not establish complete historical
   coverage. Unknown identity needs human resolution, never fabricated IDs. The first cycle announces
   imported uncertain once; `uncertain_manual` repeats until explicit review acknowledgement.
4. Review existing `refused/suspect_item:*` history manually; F02 changes future decisions only. Preserve
   terminal rows and do not use a migration or acknowledgement to resend them.
5. Archive old code/database away from runnable paths; remove old units/tunnel only after reviewed
   reconciliation. The owner removes the webhook subscription in the Meta dashboard.
6. Start the installed new unit with `sending=false`, budget 0, compare report/history counts and verify
   exact deployed SHA, STATE_DIR, credentials, Telegram, Linux memory/watchdog/stop behavior. Enabling
   sending/arming and the real 1→10→50 scenario require their separate explicit operational decision.

For state written by an older core build: remove obsolete `dm_mode`, `link_button`, `follow_button` config
keys (strict validation exits 4); absent `config_hash` causes first start to disarm and announce
`config_changed`. Old kv `public_off` is ignored; public replies follow config `public_reply`. Neither
this compatibility behavior nor local simulator PASS replaces historical reconciliation or the unknown
remote-flight G2 release condition.

## 12a. Known limitations (decided, not forgotten)

Each line is a behaviour that was examined in the audits of 06.10.2026 and left as it is on purpose. The cards
with the reasoning are `audit-claude/issues/<id>.md` (repository only).

- **A comment Meta always answers with an account-wide refusal** (K-A1-1). No rule writes it off any more (the
  old `suspect_item` rule wrote off innocent people, F02). It is tried again after everybody else and each
  refusal is a send hold, until its 7-day window ends. Way out for the owner: answer that person by hand under
  the comment; the bot sees the reply and takes the comment out of the queue.
- **The real pace of a long queue** is `max_sends_per_hour` per 71 minutes, about 85 % of the configured number
  (the hourly cap is counted over `rate_window + cap_margin`, F01). The forecasts of `arm` and `expiring` use
  this pace.
- **Only top-level comments are requests** (C13). A code word written as a reply in a thread is not read.
- **"K people lost" counts comments, not people** (C56): a second comment of one person is counted again.
- **`waiting` is one problem for all reels** (C25): it can clear while another reel sends; a reel that cannot
  be read has its own problem `reel:<media_id>`.
- **Clock** (C29): the guarantees hold for a wall clock within 5 minutes of real time; a clock set back by hours
  or forward by days is outside them.
- **State files** (C22, C58): `core.db` is an ordinary file in STATE_DIR, not a symlink; deleting or replacing
  `core.db`/WAL under a running service is not supported, stop the service first.
- **Schema** (C67): there is no migration path; a schema change needs a new database or a one-off script.
- **Integrity** (C28): the start checks the schema version, not the health of indexes; a database damaged on
  disk is outside the duplicate guarantee.
- **Meta code 100/2534025** is read as "already replied" (F29, C17): its exact meaning is not confirmed by
  live samples; a comment that was deleted and rewritten can be written off by it.
- **Response size**: an answer of Meta above 1 MiB is not read (`TooLarge` in the journal); a page of 50
  comments is far below it.
- **DNS** is resolved in a child process per call (F12): it bounds a hanging resolver, at the cost of one short
  process per HTTP call; not yet exercised on the Linux server.
- **Log volume** (K-A7-2): an idle bot writes about 2 000–3 000 lines a day (`cycle_start`, `http_complete`);
  the simulator's referee reads them.
- **One-shot commands** (K-A6-3): `once` and `send-one` may wait for the free slot with the lock held, longer
  than the 60 s a starting service waits for it. Do not start the service while one of them is running.
- **A database created by code older than 06.10.2026**: the first `stage_done` after an update counts the
  whole journal unless the budget was armed again (always arm again after a deploy: the new code disarms).
- **Cut-over script** `deploy/enter_v1_served.py` enters only people v1 certainly served; rows v1 left in doubt
  must be entered by hand as `uncertain/migrated` (C05). The cut-over of the owner's server is done (05.10).
- **Dead host**: a server or process that is dead cannot send its own alert; silence is told from "all is
  well" only by the daily digest that did not come (C42).
- **Retention** (C43): nothing is pruned; the dedupe identifiers must stay. The published privacy text has to
  say what the bot really does: owner's decision.
- **Not built**: A/B variants, a click counter, a reply counter, buttons, a follow check (13).

## 13. Phases

Phase 1 is everything above. Deliberately NOT built (add only with its own simulator scenario): click
tracking, a second message format, a `restore` command, an external heartbeat URL, pruning,
webhooks, A/B tests, follow checks, a web UI.
