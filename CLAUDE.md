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
(a) go live with reduced size — quote_size 1-2, max 2-3 markets, kill switch
$250 already set. Sequence: set `live.enabled: true`, run `selftest --live`
(1-cent order round trip MUST pass first), then `run --live` — or
(b) specific config/code fixes and one more observe day. Bias small and live:
real fills are the only data that answers the expectancy question.

## 2026-07-17 P0 batch (post-review fixes — read before touching risk/quoting)

Implemented from REVIEW-2026-07-17.md (evidence: 26 live fills, markouts
−4.8c/contract @+600s, displayed PnL understating true losses 2.9x across
sessions). 91 tests passing. What changed and why:

- **Risk-reducing orders always approve** (`risk.py`): an order that shrinks
  |position| passes even when over cap (halted still blocks). Fixes inventory
  traps after mid-session cap changes (happened 07-15→16).
- **Caps are resting-aware**: `RiskManager.register_order/release_order` track
  working orders per (ticker, side); worst case = position + resting
  same-direction + new order. Releases: cancel/replace/fill/shutdown, with the
  reconcile loop as resync backstop.
- **PnL is cumulative across sessions; kill switch is account-equity**:
  `pnl_marks` now stores cumulative values (the `session_high` column carries
  the account high-water — name kept for schema compat). Chained via the kv
  table ("cumulative_pnl", "high_water"); first run anchored high_water =
  offset so pre-upgrade losses don't trip. `analyze summary` labels still say
  "session" — cosmetic, deliberately unfixed.
- **Reconcile loop** (`reconcile.py`, live mode only): every
  `reconcile_seconds` (45) diffs exchange-resting vs local refs. Vanished →
  release exposure + allow re-quote; orphan → cancel + event (single-writer
  assumption, flock); vanished across ALL quoted tickers →
  `exchange_sweep_detected` + cancel-all + `sweep_cooloff_seconds` (900)
  global cooloff, then auto re-arm (no HALTED — not the kill switch). This is
  the fix for invisible exchange cancels: maintenance cancel_on_pause and
  order-group trips no longer leave the bot blind or re-arming into the market
  that ran it over. Pause rejections now suspend only the affected market
  (replaced the old global 300s backoff).
- **Supervision is fail-stop**: every task runs under `supervise()`; an
  unexpected exception emits `task_died` and sets stop_event. (A dead
  risk_loop used to silently disable the kill switch.)
- **Fill path deduped + isolated**: seen trade_ids seeded from the fills
  table; duplicates skipped (`fill_duplicate_ignored`); record_fill failures
  can't block worker.order_filled; callback exceptions no longer reconnect the
  ws as if they were transport errors.
- **Join-best policy A** (owner decision 2026-07-17): join_margin 0.01 /
  min_book_spread 0.02 — the old band fired on 2.7% of decisions; fill rate
  was 0.26%. Judged at the S1 gate: markout@+600s ≥ −0.5c/contract over ≥60
  fills, else revert (see config.yaml comment).
- **Fast-move guard confirms before tripping**: threshold scales with book
  width (0.75× spread floor), moves must persist across 2 updates (or a single
  jump ≥2× threshold), trips count toward eviction only if the move persists
  past cooloff — false alarms log `guard_false_alarm`. (Was: any single 3c mid
  step tripped; 266 trips/12 evictions in 4 days incl. wide-book false
  positives that evicted the books the selector prefers.)

Environment quirks:

- `ModuleNotFoundError: bacchus_mm` after any `uv sync`: run
  `chflags nohidden .venv/lib/python3.14/site-packages/*.pth` — uv recreates
  the .pth files with the macOS hidden flag set and Python 3.14 skips them.
- Scope ruff to `uv run ruff check src tests` — analysis_snapshot/ is
  forensic scratch and fails lint by design.

## Standing judgment gates (check at every review)

- Join policy A (owner-approved 2026-07-17): join_margin 1c / min_book_spread
  2c. REVERT to 2c/3c if markout@+600s < -0.5c/contract over >= 60 fills.
  quote_decision logs joined_bid/joined_ask — measure joined vs model-priced
  fills separately before concluding.
- Guard H6 recalibration: watch guard_false_alarm vs confirmed guard_trip
  ratio; if confirmed trips still evict calm markets, tune
  fast_move_spread_multiple before touching the base threshold.

## Operating notes

- Orders the bot places carry a `bmm-` client-order-id prefix. The reconcile
  loop cancels untracked `bmm-` orders (bot leaks) but only LOGS untagged ones
  (`order_foreign`) — manual orders placed in the Kalshi UI are safe, though
  they live outside the bot's caps and kill-switch view.
- `halt-clear` rebases the persisted high-water mark to current cumulative
  PnL (see README). The kill-switch threshold is cumulative-account-level.
- The cumulative chain seeds held positions at the PRIOR session's last
  logged mid, so repricing during downtime lands in the chain. Observe
  (dry-run) sessions never write the chain.

## 2026-07-17 P1 batch (scale-safety + deploy layer — read before touching eventlog/fees/settlement/deploy)

Implemented from REVIEW-2026-07-17.md §4 P1 table. 172 tests passing. Branch
`p1-scaling-batch` (stacked on `p0-review-fixes-2026-07-17`).

- **DB writes are batched** (`eventlog.py`): `events`+`mids` flush every
  `logging.flush_seconds: 1.0` or `logging.flush_batch: 500` rows, one
  transaction per flush, `synchronous=NORMAL`. **fills / pnl_marks /
  venue_marks / kv stay synchronous** — fill-dedup seeding and kill-switch
  chaining must never read unflushed state (editor: do not move these to the
  batch path). `close()` drains + checkpoints WAL. Retention:
  `logging.events_keep_days: 14` prunes ONLY the SQLite events table at
  startup + daily; JSONL files are the archive, all other tables are forever.
- **Fee model** (`fees.py`): Kalshi schedule = ceil(0.07 × C × P × (1−P)) to
  the cent, taker-only (kalshi.com/docs/kalshi-fee-schedule.pdf). The ws fill
  payload's `fee_cost` is preferred (`fee_source: reported`), formula is the
  fallback (`computed`). risk books `cash -= fee` (PnL, high-water, kill
  switch are now net-of-fee); fills table has a `fee` column with a migration
  applied by both EventLog and analyze on open. `analyze markouts` reports
  gross AND net — **net is the S2-gate number**. Quoting spreads are still
  gross by design; fee-aware sizing is a policy decision for the S1→S2 review.
- **Settlement & close**: `marks_loop` writes marks every
  `marks_tick_seconds: 60` even without book deltas; `close_reaper_hours: 12`
  pulls quotes and stops re-quoting (positions route to wind-down);
  `settlement_poll_seconds: 900` realizes settlement into risk
  (`risk.on_settlement`, yes-equivalent, net-of-fee, `settlement_realized`
  event) when a held market determines/finalizes.
- **Ws resilience**: receive wrapped in `ws_recv_timeout_seconds: 30` so
  resubscribe requests can't starve on quiet books.
- **Order group is fail-closed on prod**: prod+--live aborts startup if the
  order group can't be created (`risk.allow_no_order_group: true` overrides).
- **create_order never retries ambiguous failures** (Kalshi docs don't
  promise client_order_id dedup — verified 2026-07-17): timeout/5xx →
  `order_placement_unknown`, then adopt-if-resting by client_order_id or
  confirmed-lost replace. Never two live orders.
- **Wind-down urgency**: `winddown_distress` alert (unfilled ≥30 min or ≥5c
  adverse, throttled 1/15min). `winddown_escalation: cross_1tick` is PLUMBED
  BUT DEFAULT `none` — flipping it weakens the post-only invariant and is an
  explicit owner decision; the fee model gates whether crossing is worth it.
- **Cancels are scoped** (`reconcile.managed_tickers()`): startup sweep, kill
  switch, shutdown cancel only tickers we manage — a second strategy can share
  the account. `cancel-all` CLI stays account-wide on purpose (panic tool).
- **Deploy layer**: `Dockerfile` (uv --frozen, non-root, /app/data volume),
  `fly.toml` (iad region, [checks] on /health, [[restart]] always, mount
  `bacchus_data`), `docs/deploy.md` runbook (~$2.10/mo). `/health` (health.py)
  → 200 JSON / 503 when halted or last event >300s — payload key whitelist is
  pinned by test (no secrets/positions). Containers use env vars:
  `HEALTH_PORT` auto-enables health, `BACCHUS_LIVE_ENABLED` is the container
  half of the two-key prod gate (config.local.yaml is not in the image),
  `KALSHI_PRIVATE_KEY` inline PEM already supported. Startup clock-skew check
  warns >2s vs the REST Date header (RSA-PSS auth is local-ms).

## Conventions

- Prices: Decimal dollars in [0,1] on the YES side. Positions: signed
  yes-equivalent contracts (buying NO at p ≡ selling YES at 1-p).
- The exchange interface is `exchange/base.py`; strategy and risk code must not
  import Kalshi specifics (a Polymarket adapter is the planned phase 2).
- `uv run pytest` before proposing changes; tests are fast and offline.
- Kalshi API references used to build this: https://docs.kalshi.com/openapi.yaml
  and https://docs.kalshi.com/asyncapi.yaml (V2 order endpoints under
  /portfolio/events/orders; websocket channels orderbook_delta and fill).
