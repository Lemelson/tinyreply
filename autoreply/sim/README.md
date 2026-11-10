# sim/fake_meta.py: the fake Meta (and Telegram) the real bot is run against

A strict, stand-alone HTTP fake of the calls in SPEC.md section 6 plus Telegram `sendMessage`
(SPEC.md section 11). It is written from Meta's documented and observed behaviour
(`research/lessons/meta-api-facts.md`), not from the bot's code, and keeps its own ground truth.
It binds 127.0.0.1 only. Python standard library only.

## Run

```
cd autoreply && python3 sim/fake_meta.py --scenario FILE [--scale S] [--anchor UNIX]
```

Prints ONE JSON line `{"api": <port>, "control": <port>}` on stdout and serves until SIGTERM/SIGINT (exit 0).
A bad scenario prints a traceback, exits non-zero and prints nothing on stdout.
`--scale` / `--anchor` default to `CLOCK_SCALE` / `CLOCK_ANCHOR` of the environment (then 1 / now); the clock is
the bot's: `now = anchor + (time.time() - anchor) * scale`. Give the bot the same two values and
`IG_API_BASE=http://127.0.0.1:<api>`, `TG_API_BASE=http://127.0.0.1:<api>`.

In-process (unit tests of other modules):

```python
from sim.fake_meta import FakeMeta
fake = FakeMeta(scenario_dict, scale=1.0, anchor=None).start()   # ValueError for a bad scenario
fake.api_base, fake.control_base, fake.api_port, fake.control_port
fake.control("/truth")                         # GET; fake.control("/truth", query={"since": "12"})
fake.control("/mode", {"mode": "blocked"})     # POST; ValueError/KeyError where the port answers HTTP 400
fake.now()                                     # scaled unix seconds
fake.stop()
```

Tests of the fake: `cd autoreply && python3.12 -B -m unittest sim.test_fake_meta`.

## Scenario (JSON object; an unknown key anywhere is an error)

```json
{"account": {"user_id": "17841400000000001", "id": "9990000000000001", "username": "your_account",
             "followers_count": 1234, "media_count": 7},
 "token": {"value": "IGAA...", "age_sec": 172800},
 "media": [{"id": "17900000000000101", "permalink": "https://www.instagram.com/reel/DAbc/", "shortcode": "DAbc",
            "comments": [{"id": "c1", "text": "арена", "age_sec": 60, "from": {"id": "u1", "username": "ann"},
                          "should_match": true}]}],
 "limits": {"cap_per_hour": 750, "page_size": 50},
 "already_replied": {"spelling": "10900", "deliver": false},
 "mode": "ok", "telegram": {"token": "123:abc", "chat_id": 42}, "faults": []}
```

| key | meaning |
|---|---|
| `account` (required) | What `GET /me` returns. `user_id` = professional id, the only id `/messages` is served under. `id` = app-scoped id, used as `from.id` of the replies the fake creates for the account. `username`, `followers_count`, `media_count`. All five required. |
| `token` (required) | The one valid `access_token`: a string, or `{value, age_sec, expires_in_sec, rotate}`. `age_sec` (default 0) drives the 24 h refresh rule, `expires_in_sec` (default 60 days) the expiry, both on the scaled clock. `rotate` (default false: a refresh returns the same string): true makes a refresh return `<value>.r<n>` and the old string invalid at once. |
| `media` (required) | List of `{id, permalink, shortcode, ts or age_sec (default 30 days), comments}`. |
| `media[].comments[]` | `text`, `from` (`{id, username}` or `null` = Meta shows no author) and `should_match` are required. Optional: `id` (generated when absent), `ts` or `age_sec` (default: now), `hidden` (false), `replies` (list of `{text, from, id?, ts or age_sec}`), `private_replied` (false; true = the one private reply was used before this run), `listed` (true; false = counted in `comments_count` but never listed, facts C13), `raw` (object laid over the listed fields: a value replaces the field, `null` removes it, e.g. `{"timestamp": null}`). |
| `should_match` | The harness's label, not used by the fake; returned by `GET /comments` for the invariants. |
| `limits` | `cap_per_hour` (750): more `POST /messages` calls than this in a rolling scaled hour are answered 613/2534040 (every call counts). `page_size` (50, at least 1): pages are never longer, so a page can be shorter than `limit` and still have `next`. |
| `already_replied` | The answer to a second private reply to one comment: `spelling` `"10900"` (default), `"2534025"` (code 100) or `"-1"` (message "…already has a reply."), all HTTP 400; `deliver` (false): whether that second message goes out anyway. |
| `mode` | Initial mode, default `ok` (see below). |
| `telegram` | `token`, `chat_id`: when given, another bot token is answered 401 and another chat 400; when absent, any is taken. |
| `faults` | List of faults (see below). |

Times: `ts` is unix seconds on the scaled clock; `age_sec` is seconds before the fake's now at load (or at the
control call). Ids of media, comments and replies share one space and must be unique.

## The API port

Only these are served; anything else (unknown path or method, missing version prefix, unknown or repeated
parameter, unknown field, a body where parameters belong in the query, a message of another shape) gets HTTP 400,
code 2500, message `FAKE STRICT: …`, and the request's `strict` in the truth says why. A correct bot never
causes one.

| call | behaviour |
|---|---|
| `GET /vN.M/me?fields=` | Only the requested fields of `account` (default `id`). |
| `GET /vN.M/{media}?fields=` | `id, permalink, shortcode, comments_count, timestamp`. `comments_count` = top-level comments (listed or not) + their replies. Unknown id: 100/33. |
| `GET /vN.M/me/media?fields=&limit=` | The same fields, newest first, paged. |
| `GET /vN.M/{media}/comments?fields=&limit=` | Fields `id, text, timestamp, from, hidden, replies` or `replies{id,from,text,timestamp}`; default `id,text,timestamp`. Top-level only, newest first, at most 50, `paging.cursors` and an absolute `paging.next` (served as is). Cursors are positions: comments added or deleted between pages neither repeat nor skip older ones. A bad `after`: code 100. `from` is absent when the scenario says `null`. Replies newest first; the account's own replies appear here after a delivered public reply. |
| `POST /vN.M/{user_id}/messages` | JSON body exactly `{"recipient": {"comment_id": "…"}, "message": M}`, `Content-Type: application/json`, M = `{"text"}`. Rules, in order: mode, hourly cap, the limit (text 1..1000 UTF-8 bytes -> code 100; it does not use up the comment's reply), unknown or deleted comment -> 100/33, already replied (see `already_replied`), comment older than 7 scaled days -> 10/2534022. Success: `{recipient_id, message_id}`. |
| `POST /vN.M/{comment}/replies?message=` | Unknown or deleted comment -> 100/33; hidden comment or empty message -> code 100. Success: `{id}`. The fake does not require a private reply first: that is the invariants' job. |
| `GET /refresh_access_token?grant_type=ig_refresh_token` | Unversioned. Token younger than 24 scaled hours -> code 100; otherwise `{access_token, token_type, expires_in}` and a new 60-day life. |
| `POST /bot<token>/sendMessage` | JSON or form body (or query): `chat_id`, `text` (1..4096 characters), optional `parse_mode`, `disable_web_page_preview`, `link_preview_options`, `disable_notification`. Answers Telegram's `{"ok": true, "result": …}` or `{"ok": false, "error_code", "description"}`. Not affected by modes. |

Every Meta call needs `access_token` = the current token: otherwise HTTP 400 code 190 (expired: 190/463).

Modes (`POST /mode`), applied after strictness:

| mode | effect |
|---|---|
| `ok` | Meta's rules above. |
| `blocked` | Every Meta call, whatever the token: HTTP 400, code 200, "API access blocked.", `WWW-Authenticate` header. |
| `token_dead` | Every Meta call: code 190/467. |
| `dev_mode` | Comment lists are `{"data": [], "paging": {"cursors": …}}` while `comments_count` stays; the rest works. |
| `throttled` | Every Meta call: code 4 with `X-App-Usage` and `X-Business-Use-Case-Usage` headers that carry undocumented keys. |
| `messaging_blocked` | Reads and public replies work; `POST /messages` is refused with 100/2534029. |

## Faults (scenario `faults`, or `POST /faults`)

A fault is addressed by `kind`, optionally narrowed, and says what to do. Every fault counts the calls that match
its `kind` and `target`; when several fire on one request, the one added first wins.

| key | meaning |
|---|---|
| `kind` (required) | `me`, `media`, `my_media`, `comments`, `dm`, `public`, `refresh`, `telegram`. |
| `id` | Name shown as `fault_id` in the truth; default `f<n>`, numbered over the whole run. |
| `target` | `dm`/`public`: the comment id or its author's id; `media`/`comments`: the media id; `telegram`: the chat id. |
| `nth` | Fire on the nth matching call (an integer or a list). Without `nth` and `probability`: every matching call. |
| `probability` + `seed` | Fire with this probability, reproducibly (not together with `nth`). |
| `http`, `code`, `subcode`, `message` | Answer with Meta's error object (`http` defaults to 400; a missing `code` is left out). For `telegram`: `http` and `message` only. |
| `body` | Answer with this instead: a JSON value, or a string sent verbatim as `text/html` (not JSON). `http` defaults to 200. |
| `omit_id` | `true`, for `dm`/`public`: HTTP 200 without `message_id` / `id`. |
| `headers` | Extra response headers (also on the real answer). |
| `deliver` | `dm`, `public`, `refresh`, `telegram`: whether the call takes effect, INDEPENDENT of the answer. Required (true or false) whenever the fault replaces the answer; otherwise defaults to true. It never delivers what Meta's own rules refuse (unknown comment, already replied, outside the window, a blocking mode, bad token). |
| `consume` | `dm`: the comment's one private reply is used up even though nothing was delivered (facts A6). |
| `drop` | `"before"`: close the connection without processing; `"after"`: process (deliver unless `deliver: false`), then close without an answer. No answer keys allowed. |
| `delay_real_sec` | Wait this long (real time) before processing. |
| `refuse_real_sec` | Answer this call, then close the listening socket for this long (real time): connects are refused. The control port stays up. |

## The control port (JSON; a bad call is HTTP 400 `{"error": …}` and changes nothing)

| call | body -> answer |
|---|---|
| `GET /truth[?since=SEQ]` | See below. `since` returns only what came after request SEQ: cheap to poll. |
| `GET /clock` | `{now, real, scale, anchor}`. |
| `GET /comments` | `{comments: [...]}`: the state of every comment ever held: its scenario fields as they are now (`hidden`, `private_replied` change during a run) plus `media_id`, `ts`, `deleted`, `own` (written by the account), `first_listed_seq` (the request that first listed it to a client that got the answer, or null) and `replies` (`id, text, from, ts, own, by_api`). |
| `POST /mode` | `{"mode": M}` -> `{"mode": M}`. |
| `POST /faults` | `{"add": fault or [faults], "clear": true}` (either or both; `clear` first) -> `{"faults": [ids now active]}`. |
| `POST /comments` | Exactly one of: `{"add": {"media_id": …, comment fields}}`; `{"delete": id}`; `{"hide": id}`; `{"owner_reply": id, "text": …}` (a reply typed by the owner in the app) -> `{"id": comment id}` (+ `reply_id`). |
| `POST /hold` | `{"add": {"kind", "phase": "before" or "after", "target"?, "nth"?}}` -> `{"id"}`: pause ONE request, the first (or nth) that matches, before it is processed or after (effect done, answer not yet written). `{"wait": id, "timeout_real_sec": 10}` -> `{"caught": bool, "seq"}` blocks until that request is paused: now `kill -9` the bot. `{"release": id}` lets it go (or cancels a hold that caught nothing). A request held `before` is still processed on release even if the client is gone (Meta had received it); add a `drop: "before"` fault to make it vanish. |
| `POST /token` | `{"value", "age_sec"?, "expires_in_sec"?, "rotate"?}` -> `{"expires_at"}`: a token minted by a human replaces the current one. (Not in SPEC section 11; needed to test 8.2.) |

## Truth (`GET /truth`)

```
{"seq": last request number, "mode": current mode,
 "requests": [{"seq", "t_real_ms", "t", "method", "path", "params", "body", "kind", "target", "mode",
               "token_ok", "strict", "fault_id", "held", "status", "response", "delivered", "sent"}],
 "dms":      [{"seq", "t", "comment_id", "media_id", "user_id", "message"}],
 "replies":  [{"seq", "t", "comment_id", "id", "text"}],
 "telegram": [{"seq", "t", "chat_id", "text"}],
 "events":   [{"after_seq", "t_real_ms", "t", "op", …}]}
```

- `requests`: every request in the order received. `t_real_ms` = receive time, real unix ms; `t` = the same
  moment on the scaled clock (seconds). `params` = query parameters (including `access_token`), `body` = parsed
  JSON (or the raw text). `kind`/`target` as for faults (`kind` null = no such path or method). `token_ok` is null for
  Telegram. `strict` = why the call is not one of section 6, else null. `held` = `before`/`after` if a hold
  caught it. `status`/`response` = the answer (null if dropped before processing). `delivered` = the call took
  effect (DM delivered, reply posted, token renewed, Telegram message recorded). `sent`: true = the answer was
  written to the socket, false = dropped or the client was gone, null = still in flight.
- `dms`, `replies`, `telegram`: what really took effect, in order, with the full payload; `seq` points at the
  request. An owner reply added through the control port is not in `replies`.
- `events`: mode changes, fault changes, comment operations, token changes, listener closed/reopened, each with
  the number of the last request before it.
