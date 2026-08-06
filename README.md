# bacchus-mm

An open-source market-making lab for prediction markets, quoting on
[Kalshi](https://kalshi.com). Built around one idea: **every decision is
logged with enough context that an LLM (or a human) can reconstruct and
critique it later.** You run the bot; your logs become the dataset you
iterate on. The strategies have changed as the data came in; the logging
loop is the product that survived.

**In plain English:** it runs little currency-exchange booths inside
prediction markets, earning fractions of a cent on the gap between buy and
sell prices, thousands of times. The craft is not making the pennies; it is
avoiding getting run over by people who know something you don't. This repo
is the working diary of learning where that is possible: the first strategy
(calm, slow markets) was measured, found unprofitable, and retired with a
written retrospective. The active experiment is its replacement.

## Current status: Phase D (active)

`bacchus-mm fifteen --live` quotes Kalshi's **15-minute up/down markets**
(BTC, ETH, SOL, DOGE, BNB, HYPE, NEAR, Gold, Silver): join-the-touch, one
contract per side, rolling to each new 15-minute window as it opens. No
pricing model at all, on purpose. Three measured facts drive the design
(see [research/](research/)):

1. The market's own mid out-predicted a spot-feed fair-value model at every
   minute of window life, so the bot joins the book instead of pricing it.
2. These series carry **no maker fee** (confirmed against exchange-reported
   fills), so a captured spread is kept whole.
3. The one number no study could simulate is the real fill rate at our real
   queue position. Measuring it is the point of this phase; per-series
   go/no-go comes from fill-vs-settlement PnL, not backtests.

Mechanics specific to this mode: quotes are computed from the book
*excluding our own resting order* (join, never lead, never cross), pulled
75 seconds before close (the final 60 seconds is the settlement averaging
window), and fills ride to settlement with hard inventory caps. Windows
whose price structure the bot cannot fully parse are refused, not guessed
at. See [ROADMAP.md](ROADMAP.md) Phase D for the full spec.

## The retired strategy (and why it is still in the repo)

The original bot selected calm markets (Economics, Weather) and quoted
[Avellaneda-Stoikov](https://www.math.nyu.edu/~avellane/HighFrequencyTrading.pdf)
reservation prices with inventory skew. Three weeks live measured a
significantly negative per-contract edge: the wide spreads in quiet markets
turned out to be fair compensation for adverse selection, and the flow was
too thin to matter anyway. The full post-mortem, with numbers, is
[research/RETRO-CALM-MM-2026-08-06.md](research/RETRO-CALM-MM-2026-08-06.md).
The code path remains (`bacchus-mm run`) both for the record and because its
wind-down machinery still manages any legacy positions. The Polymarket
cross-venue phases were retired with it.

The [research/](research/) directory is the lab notebook: market-structure
studies, an intra-exchange arbitrage scan (gross violations are common; net
of fees there were zero), the 15-minute-market studies, and the fee-schedule
verification that flipped the current strategy from marginal to viable.

## How it works

- **Window discovery (fifteen)**: polls each configured series for its open
  15-minute window, validates the price grid (the crypto series use a
  piecewise tick: 0.001 in the tails, 0.01 in the middle), spawns a quoting
  worker per window, retires it after close.
- **Quoting**: join-the-touch at measurement size. The A-S model, EWMA
  volatility, and fast-move guard still exist for the legacy path but are
  deliberately out of the fifteen loop.
- **Market data**: Kalshi websocket (`orderbook_delta` + `fill` channels);
  the bot re-quotes on book changes, throttled per market, with full local
  books so it can tell "the bid" from "our bid".
- **Risk**: per-market and gross caps checked before every order; a
  drawdown kill switch that cancels everything, writes a `HALTED` marker,
  and refuses to restart until you acknowledge with `halt-clear`; plus a
  Kalshi order-group so the *exchange* cancels all orders if fills exceed a
  rolling 15-second contract limit (protection that works even if the bot
  is wedged).
- **Fees**: modeled per series from Kalshi's published schedule and
  verified against exchange-reported fills: taker 0.07 x C x P x (1-P)
  rounded up to a centicent; maker 0.0175 on ~76 listed series and **zero
  everywhere else**, including every 15-minute series. The websocket's own
  `fee_cost` is always preferred over the model.
- **Logs**: JSONL event stream + SQLite mirror in `data/`: every quote
  decision (book top, inventory, queue depth at the joined level), order
  event, fill (with mid-at-fill), mid marks, and a PnL curve.
  `bacchus-mm analyze markouts` computes post-fill drift; for 15-minute
  markets, settlement itself is the markout horizon.

## Quick start

```bash
uv sync

# 1. Create a demo account at https://demo.kalshi.co and generate an API key.
cp .env.example .env   # then fill in your key ID and private key path

# 2. The active strategy: quote the 15-minute markets (demo env by default).
uv run bacchus-mm fifteen --observe   # log decisions, place no orders
uv run bacchus-mm fifteen             # trade on demo

# 3. Read the logs:
uv run bacchus-mm analyze summary
uv run bacchus-mm analyze markouts
uv run bacchus-mm analyze incidents

# The retired calm-market strategy is still runnable for study:
uv run bacchus-mm markets   # what its selector would pick
uv run bacchus-mm observe
```

Going live on prod requires **both** `live.enabled: true` (or the
`BACCHUS_LIVE_ENABLED=1` env var) and the `--live` flag, a two-key
deliberate action, never a default. Before the first live run,
`bacchus-mm selftest --live` proves the order plumbing with a single
1-contract $0.01 post-only round trip (place, rest, cancel, verify).

## Deploy (fly.io)

For 24/7 operation the bot deploys to fly.io as a single worker Machine with
a 1GB volume for `data/`, an internal `/health` machine check, and
auto-restart. About $2/mo. The repo ships the `Dockerfile`, `fly.toml` (whose
process command is the deployed strategy), and the step-by-step runbook:
[docs/deploy.md](docs/deploy.md). The 15-minute markets generate a lot of
events; see the runbook's "Disk" note.

## Configuration

`config.yaml` holds public defaults, including the `fifteen:` block. Create
`config.local.yaml` (gitignored) and override anything; your tuned
parameters stay private even though the code is public. Credentials come
only from the environment / `.env` (gitignored). Containers get no
`config.local.yaml`, and the bot refuses to trade prod with no config file
loaded at all.

## Safety model

1. Post-only orders: a quote that would cross is rejected, never a taker fill.
2. Client-side caps: per-market contracts, per-market notional, gross notional.
3. Kill switch: drawdown from the ACCOUNT-equity high-water mark (chained
   across sessions) ≥ threshold → cancel all, halt. `halt-clear` re-arms by
   rebasing the high-water mark to current equity: clearing a halt means
   "loss acknowledged; protect me from here."
4. Exchange-side order group: Kalshi cancels everything if the group trades
   more than N contracts in any rolling 15s window.
5. Order TTLs: resting orders expire server-side even if the bot dies.
6. Settlement-window pull (fifteen): all quotes canceled 75s before each
   window closes; the bot never rests an order into the averaging period
   that fixes the settlement value.
7. Structure gate (fifteen): a market whose price grid the bot cannot fully
   parse is refused, never quoted.
8. Startup hygiene: cancels any stale resting orders from previous sessions;
   shutdown verifies zero resting orders and says so loudly if not.

## Honest expectations

Market making on prediction markets is a fight against adverse selection.
This repo's own history is the proof: the first strategy's wide spreads in
calm markets measured out as fair payment for being picked off, and it was
shut down on that evidence. A small passive bot should be judged on
per-contract expectancy after fees against settlement, not on monthly income
targets. Run it small, read the logs, and let the data decide what happens
next; everything this project has learned so far is written down in
[research/](research/).

This is not financial advice; use at your own risk. See [LICENSE](LICENSE).
