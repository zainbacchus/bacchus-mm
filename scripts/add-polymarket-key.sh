#!/bin/zsh
# Interactive helper: adds Polymarket credentials to .env (same pattern as
# add-key.sh — secrets are typed/pasted into the terminal, never chat).
#
# NOTE: Phase A (data recording / `bacchus-mm crossvenue`) needs NO credentials —
# Polymarket market data is public. These are only needed for Phase C (trading).
#
# Polymarket US API credentials come from your verified account's API settings:
#   - API key ID, secret, and passphrase (the "L2" REST credentials)
#   - optionally your wallet private key ("L1", only for order signing flows)

set -euo pipefail
cd "$(dirname "$0")/.."
touch .env

add_var() {
  local var="$1" prompt="$2" secret="${3:-yes}" value
  if [[ "$secret" == "yes" ]]; then
    read -rs "value?$prompt (input hidden, Enter to skip): "
    echo "" >&2
  else
    read -r "value?$prompt (Enter to skip): "
  fi
  [[ -z "$value" ]] && return 0
  grep -v "^$var=" .env > .env.tmp || true
  mv .env.tmp .env
  printf '%s=%s\n' "$var" "$value" >> .env
  echo "  $var written."
}

echo "Polymarket credentials -> .env (any field can be skipped):"
add_var POLYMARKET_API_KEY "API key ID" no
add_var POLYMARKET_API_SECRET "API secret"
add_var POLYMARKET_API_PASSPHRASE "API passphrase"
add_var POLYMARKET_PRIVATE_KEY "Wallet private key (only for Phase C signing)"

echo ""
echo "Done. Present in .env: $(grep -oE '^POLYMARKET_[A-Z_]+' .env | tr '\n' ' ')"
