# Changelog

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
