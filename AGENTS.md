# Operating tinyreply as an AI agent

You are usually working for a creator who is not a programmer. They will say things like "add the reel from
yesterday with the keyword *guide*, the link is X", "why didn't Masha get the link?" or "pause everything".
Your job is to make that happen safely and to explain the result in plain words.

## The map

| Path | What it is |
|---|---|
| `autoreply/SPEC.md` | The contract. Section 2 lists the guarantees, section 4 the config, section 9 the report and alerts. Where code and SPEC disagree, SPEC wins. |
| `autoreply/bot.py` | CLI: `run`, `once`, `send-one`, `arm`, `disarm`, `ack-uncertain`, `report`, `check`, `media`. |
| `autoreply/config.py`, `match.py` | Config validation and the keyword matcher (pure functions). |
| `autoreply/engine.py`, `store.py`, `meta.py` | The cycle, the SQLite state machine, the Meta HTTP client. |
| `autoreply/notify.py`, `report.py` | Telegram alerts and the report. |
| `autoreply/tests/`, `autoreply/sim/` | Unit tests; a fake Meta and end-to-end scenarios. |
| `STATE_DIR` (default `autoreply/state/`) | **The installation:** `config.json`, `.env`, `core.db`. Never in git. |

## Hard rules

1. **Secrets live only in `STATE_DIR/.env`.** Never print a token, never put one in a config, a commit, a log
   or a chat message. If you need to know whether a token is set, run `check`, which prints `set` or `MISSING`.
2. **Never commit anything from `STATE_DIR`.** That means no `config.json`, `.env` or `*.db`. Keep the
   pre-commit hook on (`git config core.hooksPath .githooks`). If it blocks a commit, fix the cause. Never
   use `--no-verify`.
3. **Never edit `core.db` by hand** and never delete it to "reset" things. Deleting it means people who already
   got a DM can get a second one. To stop sending, use `disarm`. To mark a delivery you checked by hand, use
   `ack-uncertain`.
4. **Never retry a send yourself** (no `curl` to `/messages`). Meta allows one private reply per comment. A status
   of `uncertain` means "maybe delivered": ask the human to look in Instagram, then run `ack-uncertain`.
5. **Arming is the human's decision.** A config change or a code update resets the budget to 0 on purpose.
   Show the human `report` (how many people are waiting, how long it will take), and run `arm N` only after
   they say so in their own words. Start small (10–20), and raise the budget once the first DMs have arrived.
6. **Only one bot per `STATE_DIR`.** If `run` exits with code 3, another copy holds the lock. Find it. Do not
   work around it.

## Common tasks

**Add a reel.** Run `media` to get the post's `media_id`. Add an object to `reels` in `STATE_DIR/config.json`
with `name`, `media_id`, `active: true`, `keywords` (letters only, at least 2 letters, no digits),
optional `also_match` typos, `max_words`, `link` (https) and `text` (`{greeting}` and `{link}` are
placeholders). Then:

```bash
python3 bot.py check                    # must say "config ok"; it lists every problem at once if not
sudo systemctl restart ig-autoreply     # the service reads config only at start; this disarms
igbot report                            # show the human what is waiting
igbot arm 20                            # only after the human agrees
```

**Pause a reel.** Set its `active` to `false` and restart. Pause everything with `igbot disarm`. It works
immediately and needs no restart.

**"Why didn't X get the link?"** Run `igbot report`. It lists every waiting reason, near-misses (comments that
have the keyword but are not requests) and frequent short comments that did not match, which are usually
typos you can add to `also_match`. Explain the reason in plain words. Do not guess.

**A keyword is not matched.** Add the typo to `also_match`, run `check`, then restart. `check` rejects a word that
would also match your own public replies or the DM text, so the bot never answers itself.

**Token trouble.** Put a fresh `IG_ACCESS_TOKEN` in `STATE_DIR/.env`. The bot picks it up in the next cycle with
no restart, and it refreshes the token itself afterwards. `check --online` verifies it.

## Changing the code

- Read the relevant SPEC section first. A change that weakens a guarantee in section 2 (G1–G6) is a bug,
  even if every test passes.
- Standard library only. Do not add dependencies.
- Before you hand back, run:

```bash
cd autoreply
python3 -m unittest discover -s tests -t .
python3 -m unittest discover -s sim -t .
python3 sim/soak.py --only named
```

- A new behaviour gets a test. A fixed bug gets a test that failed before the fix.
- Deploying new code also disarms (the code hash is stored). Tell the human that sending is closed until
  they arm again.

## Talking to the human

Say what changed, what you checked, and what is not checked yet. Use their words ("reel", "link",
"waiting"), not internal names, unless they ask. Show numbers from `report`, never estimates of your own.
