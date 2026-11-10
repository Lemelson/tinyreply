# Contributing

Thanks for helping. tinyreply stays small on purpose, so a few rules:

- **Standard library only.** No new dependencies.
- **Read `autoreply/SPEC.md` first.** A change that weakens a guarantee in section 2 (G1–G6) is a bug, even if
  every test passes.
- **Every behaviour change comes with a test.** Every bug fix comes with a test that failed before the fix.
- **Run everything before a pull request:**

  ```bash
  cd autoreply
  python3 -m unittest discover -s tests -t .
  python3 -m unittest discover -s sim -t .
  python3 sim/soak.py --only named
  ```

- **Never commit installation state** (`.env`, `config.json`, `*.db`). Turn on the leak guard once per clone:
  `git config core.hooksPath .githooks`.

Good first contributions are i18n of alerts and reports, an optional follow-gate add-on, and click tracking.
See "What it does not do (yet)" in the README.
