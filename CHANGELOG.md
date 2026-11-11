# Changelog

## v2.1.0 (2026-11-11): the optional funnel and the web panel

Everything here is additive: a reel without a `flow` block behaves exactly as in v2.0.

- Optional web process `web.py` (standard library, `MemoryMax=60M`, behind nginx): Meta webhook with signature
  check, tracked-link redirect `/r/<token>`, and the panel behind a login. It never calls Meta and never sends.
- Per reel `flow`: a "Get it" button in the first DM, a follow check with up to three prompts, a link message with
  up to two buttons, an optional follow-up, and rechecks of the follow 1 and 7 days later.
- A/B tests: up to four weighted variants per reel; a person always keeps their variant.
- Click tracking through your own domain; link-preview robots are not counted; no cookies, no IPs.
- Web panel: overview, reels, people, analytics and settings; read-only; English and Russian; light and dark
  theme; works on phones. A live demo on invented data is in `docs/demo/`.
- The DM goes out exactly as written: the link is appended only if the text does not contain it, and the
  Russian "Подписаться:" line is gone.
- Database schema version 3 (new tables only; a v2 database is upgraded in place).

## v2.0.0 (2026-11-10): first public release

The "core" rewrite, now open source.

- One process, one thread, SQLite (WAL, `synchronous=FULL`), Python 3.12 standard library only.
- Polling-based: no inbound webhook, no open ports.
- Claim-before-send state machine: one DM per person per post, safe across crashes and restarts.
- Ambiguous Meta answers become `uncertain` and are never auto-retried; `ack-uncertain` for manual review.
- Hourly send cap with spacing kept on the monotonic clock, also across restarts.
- Serves the full 7-day private-reply window, oldest first; late comments get a separate greeting.
- Keyword matcher: Cyrillic/Latin look-alikes, case, ё/е, repeated letters, emoji, @mentions, configured typos,
  max words, question filtering. Checked against 1,108 real comments.
- Rotating public replies under each served comment.
- Budget-based arming: new database, config change or code update closes sending until `arm N`.
- `report` with per-reel queues, waiting reasons, hourly sends, near-misses and frequent typos;
  `unaccounted` reconciliation.
- Telegram alerts that repeat while people are waiting or being lost, plus a daily digest.
- Automatic token refresh; a new token in `.env` is picked up without restart.
- Hardened systemd unit (dedicated user, watchdog, `MemoryMax=100M`).
- 790 unit tests, 299 simulator tests, named end-to-end scenarios and randomized fault-injection runs
  against a strict fake Meta API.
- Leak guard: `.gitignore` for all installation state, a pre-commit hook with token patterns, gitleaks and a
  personal deny-list.

Removed compared with v1 (planned to return as optional add-ons): inbound webhook, buttons, follow gate,
A/B test.
