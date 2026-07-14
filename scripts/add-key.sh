#!/bin/zsh
# Interactive helper: adds your Kalshi private key to .env as a one-line
# \n-escaped KALSHI_PRIVATE_KEY value. Run it, paste the key, Enter, Ctrl-D.
# The key never passes through the clipboard-history or shell history.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Paste your Kalshi private key below (the whole block, from"
echo "-----BEGIN ... KEY----- through -----END ... KEY-----),"
echo "then press Enter and Ctrl-D:"
echo ""
key="$(cat)"

if [[ "$key" != *"-----BEGIN"* || "$key" != *"PRIVATE KEY-----"* ]]; then
  echo "" >&2
  echo "ABORTED: that doesn't look like a PEM private key (missing BEGIN/END lines)." >&2
  echo "Nothing was written." >&2
  exit 1
fi

touch .env
grep -v '^KALSHI_PRIVATE_KEY=' .env > .env.tmp || true
mv .env.tmp .env
printf 'KALSHI_PRIVATE_KEY=%s\n' \
  "$(printf '%s' "$key" | awk 'NR>1{printf "\\n"} {printf "%s", $0}')" >> .env

echo ""
echo "OK — KALSHI_PRIVATE_KEY written to .env."
echo "Sanity check (first characters only): $(grep -o '^KALSHI_PRIVATE_KEY=.\{15\}' .env)"
echo "Test with: .venv/bin/bacchus-mm observe"
