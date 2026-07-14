# bacchus-mm

An open-source market-making bot for [Kalshi](https://kalshi.com) prediction markets,
built around one idea: **every decision is logged with enough context that an LLM
(or a human) can reconstruct and critique it later.** You run the bot; your logs
become the dataset you iterate on.

## How it works

- **Market selection** — scans open markets, filters to configured categories
  (default: calm, slow-moving ones — Economics, Climate and Weather), requires
  minimum volume and a spread wide enough to pay, stays out of price tails and
  soon-to-close markets, then scores by volume × spread and quotes the top N.
- **Quoting** — [Avellaneda-Stoikov](https://www.math.nyu.edu/~avellane/HighFrequencyTrading.pdf)
  reservation-price quoting with EWMA volatility and inventory-skewed spreads.
  Orders are post-only (never take), with exchange-side TTLs so a crashed bot's
  orders expire on their own.
- **Market data** — Kalshi websocket (`orderbook_delta` + `fill` channels); the
  bot re-quotes on book changes, throttled per market.
- **Risk** — per-market and gross position caps checked before every order; a
  drawdown kill switch that cancels everything, writes a `HALTED` marker, and
  refuses to restart until you acknowledge with `halt-clear`; plus a Kalshi
  order-group so the *exchange* cancels all orders if fills exceed a rolling
  15-second contract limit (protection that works even if the bot is wedged).
- **Logs** — JSONL event stream + SQLite mirror in `data/`: every quote decision
  (with book top, inventory, sigma, reservation price), order event, fill (with
  mid-at-fill), mid marks, and a PnL curve. `bacchus-mm analyze markouts` computes
  post-fill mid drift — the honest measure of whether you're earning spread or
  getting picked off.

## Quick start

```bash
uv sync

# 1. Create a demo account at https://demo.kalshi.co and generate an API key.
cp .env.example .env   # then fill in your key ID and private key path

# 2. See what the selector would trade right now (no credentials needed):
uv run bacchus-mm markets

# 3. Stream and log without placing orders:
uv run bacchus-mm observe

# 4. Trade on the demo environment:
uv run bacchus-mm run

# 5. Read the logs:
uv run bacchus-mm analyze summary
uv run bacchus-mm analyze markouts
uv run bacchus-mm analyze quotes
uv run bacchus-mm analyze incidents

# 6. Cross-venue (optional, no credentials — Polymarket data is public):
#    map pairs in config.local.yaml (find slugs with pm-find), then record
#    divergence between venues. See ROADMAP.md for where this is heading.
uv run bacchus-mm pm-find "fed"
uv run bacchus-mm crossvenue
uv run bacchus-mm analyze divergence
```

Going live on prod requires **both** `live.enabled: true` in `config.local.yaml`
and the `--live` flag — a two-key deliberate action, never a default.

## Configuration

`config.yaml` holds public defaults. Create `config.local.yaml` (gitignored) and
override anything — your tuned parameters stay private even though the code is
public. Credentials come only from the environment / `.env` (gitignored).

## Safety model

1. Post-only orders — a quote that would cross is rejected, never a taker fill.
2. Client-side caps — per-market contracts, per-market notional, gross notional.
3. Kill switch — drawdown from session high ≥ threshold → cancel all, halt,
   require explicit `halt-clear` to re-arm.
4. Exchange-side order group — Kalshi cancels everything if the group trades
   more than N contracts in any rolling 15s window.
5. Order TTLs — resting orders expire server-side even if the bot dies.
6. Startup hygiene — cancels any stale resting orders from previous sessions;
   shutdown verifies zero resting orders and says so loudly if not.

## Honest expectations

Market making on prediction markets is a fight against adverse selection.
A small passive bot should be judged on per-contract expectancy after fees
(see `analyze markouts`), not on monthly income targets. Run it small, read
the logs, and let the data tell you whether to scale.

This is not financial advice; use at your own risk. See [LICENSE](LICENSE).
