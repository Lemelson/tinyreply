#!/usr/bin/env bash
# Refuses a commit (or an export) that would publish secrets or local state.
#   scripts/leak-scan.sh            scan files staged for commit (used by .githooks/pre-commit)
#   scripts/leak-scan.sh --tree DIR scan every file under DIR (used before a release)
# Exit 0 = clean, 1 = something must not be published.
set -euo pipefail

mode=staged
root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
if [ "${1:-}" = "--tree" ]; then mode=tree; root=${2:?directory}; fi
cd "$root"

if [ "$mode" = staged ]; then
  files=$(git diff --cached --name-only --diff-filter=ACMR)
else
  files=$(find . -type f -not -path './.git/*' | sed 's#^\./##')
fi
[ -z "$files" ] && exit 0

fail=0
say() { echo "leak-scan: $*" >&2; fail=1; }

# 1. Files that are local state by definition.
while IFS= read -r f; do
  case "$f" in
    .env|*/.env|.env.*|*/.env.*) [[ "$f" == *.example ]] || say "local env file: $f" ;;
    *state/*|*.db|*.db-wal|*.db-shm|*.sqlite*|*core.lock) say "local state/database: $f" ;;
    *config.json) [[ "$f" == *config.example.json ]] || say "real config (keep it in STATE_DIR): $f" ;;
    *.pem|*.key|*id_rsa*|*id_ed25519*) say "key material: $f" ;;
  esac
done <<< "$files"

# 2. Secret-looking strings. Test fixtures use obviously fake values, which these patterns do not match.
patterns=(
  'IGAA[A-Za-z0-9_-]{60,}'                 # Instagram user access token
  'EAA[A-Za-z0-9]{80,}'                    # Facebook access token
  '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'        # Telegram bot token
  'sk-(or-|ant-|proj-)?[A-Za-z0-9_-]{30,}'  # OpenRouter / Anthropic / OpenAI keys
  'gh[pousr]_[A-Za-z0-9]{30,}'              # GitHub tokens
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)
allow="$root/scripts/leak-allow.txt"
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in *.png|*.jpg|*.gif|*.ico) continue ;; esac
  for p in "${patterns[@]}"; do
    if [[ "$p" == *"PRIVATE KEY"* ]] && grep -qxF "$f" "$allow" 2>/dev/null; then continue; fi
    if grep -nIE -e "$p" "$f" >/dev/null 2>&1; then say "secret-looking string ($p) in $f"; fi
  done
done <<< "$files"

# 3. Optional personal deny-list, never committed: one fixed string per line (your handle, server IP, phone...).
deny="${LEAK_DENYLIST:-$HOME/.config/tinyreply/denylist.txt}"
if [ -f "$deny" ]; then
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    if grep -nIiF -f <(grep -v '^\s*\(#\|$\)' "$deny") "$f" >/dev/null 2>&1; then say "deny-listed text in $f"; fi
  done <<< "$files"
fi

# 4. gitleaks, when installed, as a second opinion.
if command -v gitleaks >/dev/null 2>&1; then
  if [ "$mode" = staged ]; then
    gitleaks git --staged --no-banner --redact -c "$root/.gitleaks.toml" >/dev/null 2>&1 || say "gitleaks found something (run: gitleaks git --staged -v)"
  else
    gitleaks dir . --no-banner --redact -c "$root/.gitleaks.toml" >/dev/null 2>&1 || say "gitleaks found something (run: gitleaks dir $root -v)"
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "leak-scan: blocked. Secrets belong in STATE_DIR/.env, settings in STATE_DIR/config.json (both ignored)." >&2
  exit 1
fi
