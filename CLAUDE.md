# bacchus-mm — notes for Claude sessions

Market-making bot for Kalshi. The owner's workflow: the bot runs and logs;
Claude Code sessions read the logs, diagnose, and propose parameter or code
changes. That analysis loop is the product — keep logs rich and backward
compatible.

## Reading the logs

- `data/bacchus.db` (SQLite) is the primary analysis surface: tables `events`
  (raw JSON payloads), `fills`, `mids`, `pnl_marks`. `data/events-YYYYMMDD.jsonl`
  is the same firehose as flat files.
- Start every analysis session with:
  `uv run bacchus-mm analyze summary` and `uv run bacchus-mm analyze markouts`.
- Markout interpretation: negative markouts at +60s that recover by +600s
  suggest requoting too slowly (transient picking-off); negative at both
  horizons means the market selection or spread floor is wrong for that market.
- `quote_decision` events carry mid, book top, inventory, sigma, reservation
  price, and both quotes — enough to replay the strategy's reasoning exactly.

## Tuning levers (config.local.yaml, overlays config.yaml)

- `strategy.min_half_spread` / `max_half_spread` — the blunt profitability lever
- `strategy.gamma` — inventory skew aggressiveness
- `strategy.quote_size`, `risk.max_contracts_per_market` — exposure
- `selector.categories`, `min_volume_24h`, `min_spread` — which markets at all

## Safety invariants (do not weaken without explicit owner sign-off)

- Orders are always post-only with an exchange-side TTL.
- Prod trading requires `live.enabled: true` AND `--live`.
- The kill switch writes `data/HALTED`; never auto-clear it in code — the
  `halt-clear` command is a deliberate human action.
- Risk caps are checked in `RiskManager.approve_order` before every placement.

## First review playbook ("run the first review")

Context: the bot has been running in observe mode (dry_run — quote decisions
logged, no orders, therefore NO fills; summary/markouts will be empty. That is
expected, not a bug). The review's job is to clear three gates and produce one
decision: go live tiny, or fix and observe another day.

1. **Ops gate** — `analyze incidents --hours 24`: any crashes, error bursts,
   or gaps? Check for overnight holes in `mids` (Mac sleep) and ws reconnect
   loops. Both feeds (events + venue_marks) should be continuous.
2. **Quote-sanity gate** — `analyze quotes --hours 24`, then pull a sample of
   `quote_decision` events per market from the events table: are our would-be
   quotes inside sane bounds (never crossing the book, spreads >= min after
   fees, sigma not pinned at floor or exploding)? Would we have been run over
   anywhere — check markets that moved >5c in a minute and what our standing
   quote would have been.
3. **Market-behavior gate** — which selected markets gapped around data
   releases or settlement? Did any same-day-settlement market get picked?
   Feed this into selector filters (`min_hours_to_close`, categories).
4. **Cross-venue read** — `analyze divergence --hours 24`: lead/lag and
   magnitude on the Fed pairs; this gates Phase B design (see ROADMAP.md).

Output of the review: a short written verdict per gate + ONE recommendation:
(a) go live (`live.enabled: true` + `run --live`) with reduced size —
quote_size 1-2, max 2-3 markets, kill switch $250 already set — or
(b) specific config/code fixes and one more observe day. Bias small and live:
real fills are the only data that answers the expectancy question.

## Conventions

- Prices: Decimal dollars in [0,1] on the YES side. Positions: signed
  yes-equivalent contracts (buying NO at p ≡ selling YES at 1-p).
- The exchange interface is `exchange/base.py`; strategy and risk code must not
  import Kalshi specifics (a Polymarket adapter is the planned phase 2).
- `uv run pytest` before proposing changes; tests are fast and offline.
- Kalshi API references used to build this: https://docs.kalshi.com/openapi.yaml
  and https://docs.kalshi.com/asyncapi.yaml (V2 order endpoints under
  /portfolio/events/orders; websocket channels orderbook_delta and fill).
