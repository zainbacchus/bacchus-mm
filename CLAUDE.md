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
- Pick-off analysis (2026-08-11): `zsh scripts/pull-fly-db.sh` snapshots the
  live DB, then `python research/pickoff_report.py <snapshot> --hours 24`
  buckets settled PnL AND post-fill markouts (+30s/+180s, negative =
  picked off) by series, minute-of-window, side, price band, and queue
  depth at join. Markout is the who-filled-us lens; settled is the money;
  the two diverging by side means two different counterparty populations.

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
- **Fee model** (`fees.py`, reworked 2026-08-06 against the schedule effective
  2026-07-07): taker = round-up-to-CENTICENT(0.07 × C × P × (1−P)); maker =
  same shape at 0.0175 but ONLY for the ~76 series in the schedule's
  "Non-Standard Fees" table (`MAKER_FEE_SERIES`) — series absent from the
  table pay NO maker fee, and 10 series are fee-free both sides. Confirmed
  15/15 against our own exchange-reported fills (fly-snapshot-4.db); the old
  "~0.0189 maker rate" was centicent round-up on small in-table fills. The ws
  fill payload's `fee_cost` is preferred (`fee_source: reported` — in practice
  every live fill has carried it), formula is the fallback (`computed`, takes
  a `series=` kwarg; None charges maker_rate, deliberately conservative). risk
  books `cash -= fee` (PnL, high-water, kill switch are net-of-fee); fills
  table has a `fee` column with a migration applied by EventLog on open.
  `analyze markouts` reports gross AND net — **net is the S2-gate number**.
  Quoting spreads are still gross by design; fee-aware sizing is a policy
  decision for the S1→S2 review.
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

## 2026-08-06 Phase D: fifteen mode (the active strategy — read before touching fifteen.py / join_touch.py)

`bacchus-mm fifteen --live` measures join-the-touch quoting on the 15-minute
markets (ROADMAP Phase D). The calm-MM `run` path is retired but intact
(config.yaml selector holds a sentinel category; `run --live` is a pure
wind-down session — zero picks + held positions is valid, main.py no longer
exits on it).

- **No model, deliberately.** The market's mid beat a spot-driven model at
  every minute of window life (research/ARB-AND-15MIN-STUDY-2026-08-06.md),
  so fifteen workers JOIN the external touch and never price. The policy
  (strategy/join_touch.py) computes the touch EXCLUDING our own resting
  order — joining the raw book top would chase our own quote down one tick
  per cycle. Never leads, never crosses; crossed/locked external book quotes
  nothing.
- **Fast-move guard is OFF in this mode** (threshold parked at $9). That is a
  measurement decision, not an oversight: risk is bounded by quote_size x
  max_contracts_per_market x $1 per series + the account kill switch.
- **T-75s pull:** pull_loop sets close_reaped at close-75s (the final 60s is
  the settlement AVERAGING window — resting quotes there are free options).
  Window positions ride to settlement (defined risk); there is NO reduce-only
  wind-down for windows. Legacy calm-MM positions DO get wind-down workers.
- **Price grids are piecewise** (verified live 2026-08-06): crypto 15M uses
  0.001 ticks in [0,0.10] and [0.90,1.00], 0.01 in the middle; Gold/Silver
  uniform 0.01. parse_window accepts any contiguous cover of [0,1] with steps
  in [min_tick, max_tick] and REFUSES anything else
  (fifteen_structure_refused). Join-touch is grid-safe by construction — it
  only quotes at prices already resting in the book.
- **Measurement telemetry:** quote_decision carries join_depth_bid/ask
  (others' resting depth at the joined level — the queue-position proxy);
  fifteen_window_start / fifteen_quotes_pulled / fifteen_structure_refused
  bracket each window. Fill rate = fills vs windows quoted; conditional edge
  = fill price vs settlement (settlement_realized), both per series.
- **Go/no-go per series** after ~1 week: measured net c/contract > 0 with CI
  excluding zero -> scale THAT series (scaling is its own experiment — our
  size moves our queue position); else drop the series.
- **Evidence levers (2026-08-06, owner: "apply all 4")** — each independently
  disable-able in config.yaml `fifteen:` and each emits its own attribution
  telemetry; score them SEPARATELY at every review:
  - M1 tilt (`tilt_tail_threshold`, join_touch.py): never sell the favorite /
    buy the longshot in the tails. Evidence: research/CALIBRATION-15M
    -2026-08-06.md (+0.57c/ct at 0.98-1.00 late, n=2005, ~4σ) + Buergi-Deng-
    Whelan 2026. Telemetry: quote_decision.tilt_bid/tilt_ask.
  - M2 toxicity pull: the fast-move guard re-enabled at 15M scale (threshold
    0.03/10s window/45s cooloff, eviction off). Telemetry: quotes_pulled
    reason=fast_move, guard_false_alarm.
  - M3 dollar-loss cap (`max_loss_per_market`): long caps at L/p, short at
    L/(1-p) — tail positions get the smallest caps (BS-for-PM handbook,
    arXiv 2510.15205). Visible as clamped sizes in quote_decision.
  - M4 spot-jump pull (`spot_jump_*`, spot_feed task): Coinbase move >= 8bps
    in 10s cancels that series' quotes 20s. PULL ONLY — the spot feed must
    never price (the pricing model measurably lost to the book; see
    research/ARB-AND-15MIN-STUDY-2026-08-06.md). Only series with a mapped
    feed get M4 (BTC/ETH today; the commodities have no free tick feed).
    Telemetry: fifteen_spot_pull, order_canceled reason=spot_jump.
  - M5 flow gate (`flow_min_updates`/`flow_window_seconds`): quote only
    while the book shows real activity; cold workers START gated until the
    book proves itself. Evidence: first-evening thin windows ran -13 to
    -24c/ct (informed-only flow). Doubles as the overnight auto-curfew.
    Telemetry: quotes_pulled/quotes_resumed reason=flow_gate.

- **Window-open delay (2026-08-11, post-kill-switch restart)**:
  `min_seconds_after_open` 5 -> 90. The 08-11 halt traced to a window-open
  toxic-flow regime (informed sellers hitting fresh thin-queue bids near
  0.50; onset pre-dated the probe deploy, which only amplified it). Full
  forensics + judgment plan: research/ATTRIBUTION-WINDOW-OPEN-2026-08-11.md.
  Reviews should read minute-14 as structurally EXCLUDED from here on; if
  losses appear at minute 13, that is the flagged migration case (next
  variable: join_depth posting filter).

## Measurement rules (2026-08-07 — violations produced a fake +0.56c read)

- Measure settled-fills-vs-settlement from a FIXED t0, unsegmented, via
  `research/daily_review.py` (it encodes both rules below). NEVER diff two
  "since X" snapshots: each excludes its trailing unsettled windows, and the
  worst windows fall between the cracks of consecutive reads.
- REST /portfolio/fills sign convention (pinned empirically against the
  bot's own logged fills): `action` is already the yes-equivalent direction
  (buy=+, sell=-); `side` is which token PRINTED, not our direction. Using
  (side, action) jointly fabricated a fake -$75 read once.
- Anchor every review against account balance (/portfolio/balance); if
  equity moved much more than settled PnL explains, say so loudly.

## Daily review routine (cloud, 2026-08-07)

- claude.ai routine `trig_01Lkn8BE1Ubwy5myTmjRwrVE`, daily 12:00 UTC, model
  sonnet: clones this repo, runs research/daily_review.py (read-only GETs),
  opens a "Daily review <date>" PR with interpretation + at most three
  evidence-quoted proposals (explicit HOLDs when evidence is unclear).
  Config diffs ride as separate PROPOSAL commits; merging never deploys —
  the owner redeploys in the fly UI. The operative prompt lives in
  research/ROUTINE-PROMPT.md (since 2026-08-09 the trigger message is a
  short pointer to that file, because the trigger API caps message size):
  prompt changes are ordinary commits; RemoteTrigger is only for schedule,
  environment, or pointer changes. Its env (Kalshi keys, network allowlist
  for api.elections.kalshi.com, GitHub write) is configured at claude.ai by
  the owner, never through chat. Deep attribution (quote_decision, lever
  telemetry, guard stats — fly-DB data) stays in interactive sessions.
- PULSE routine (2026-08-11, after the halt went unnoticed 3.5h): trigger
  `trig_01K5h9tgcaMXNYQGWXqbn5VY`, cron 17 2,8,14,20 * * * (UTC), runs
  `daily_review.py --pulse` (two GETs, writes
  nothing) and stays silent on PULSE OK; on PULSE ALARM (fills stale >2h /
  equity -$15 vs last review / taker fill) it opens a GitHub issue.
  Operative prompt: research/ROUTINE-PULSE.md (pointer architecture, same
  as the daily). Detection only — it never proposes config.

## Stall diagnostics (2026-08-07 incident tooling — leave them in)

- `loop_lag` events + log lines: the in-loop probe; any stall report should
  start by checking for these (present = our loop froze; absent = network).
- STALL WATCHDOG log lines: a daemon thread stack-samples the loop when the
  heartbeat goes silent >1.5s — the blocking frame appears verbatim in the
  log. This is how the O(n)-per-update guard/book scans were caught; both
  hot paths are now O(1) (monotonic-deque window min; incremental book
  best) with fuzz tests pinning equivalence.
- Slow REST warnings during a freeze are partly SYMPTOM: an in-flight
  request cannot resume while the loop is blocked, so its measured duration
  inflates by the stall.

## Conventions

- Prices: Decimal dollars in [0,1] on the YES side. Positions: signed
  yes-equivalent contracts (buying NO at p ≡ selling YES at 1-p).
- The exchange interface is `exchange/base.py`; strategy and risk code must not
  import Kalshi specifics (a Polymarket adapter is the planned phase 2).
- `uv run pytest` before proposing changes; tests are fast and offline.
- Kalshi API references used to build this: https://docs.kalshi.com/openapi.yaml
  and https://docs.kalshi.com/asyncapi.yaml (V2 order endpoints under
  /portfolio/events/orders; websocket channels orderbook_delta and fill).
