# Roadmap

## Current status (2026-08-06)

Calm-market MM is CONCLUDED with a measured negative edge (see
research/RETRO-CALM-MM-2026-08-06.md); the selector is wound down. Phases B
and C below are RETIRED with it (they were signal/execution add-ons to the
calm-market thesis). The active work is Phase D.

## Phase D — 15-minute markets measurement (ACTIVE)

Goal (owner, 2026-08-06): test whether passive MM on the 15-minute up/down
markets can return at least 2x the risk-free rate annualized on $500-$20K.

Why this survived every other idea (research/ARB-AND-15MIN-STUDY-2026-08-06.md):
these are pure return bets (target fixed at open), fair value is computable,
the family runs ~190M contracts/day, and the fee model confirmed 2026-08-06
says NO maker fee on any 15M series. Measured conditional edge joining the
touch: BTC +0.278c/contract gross, GOLD +0.060c; ETH/SOL/SILVER/NEAR negative;
DOGE/BNB/HYPE unmeasured. The one untested assumption is FILL RATE: the study
counted a fill whenever price traded through our level (98.7% for BTC), but
real queue position on a 2M-contract book will deliver less, selected against
us. That number cannot be simulated; it must be measured live.

The capital target math: risk-free ~4%, so 2x is ~8%/yr. On $500 that is
~11c/day (~40 BTC-contracts/day at +0.278c); on $20K, ~$4.40/day (~1,600
contracts/day, ~0.001% of series volume). Capital is not the binding
constraint; measured net edge at OUR queue position is.

Build SHIPPED 2026-08-06 (`bacchus-mm fifteen --live`; fly.toml now runs it —
one deploy winds down legacy positions AND starts the measurement). Notable
reality-check from the build: crypto 15M price grids are PIECEWISE (0.001
tails / 0.01 middle, verified live), not uniform decicent as assumed below.

STATUS 2026-08-07: active series BTC, ETH, GOLD, SILVER, WTI (alt-crypto
cohort measured negative and dropped: NEAR -4.78, SOL -1.32, DOGE -0.87,
HYPE -0.48 c/ct; BNB benched pending a cohort-beating case). Five evidence
levers live (M1 tilt, M2 toxicity pull, M3 dollar caps, M4 spot-jump pull,
M5 flow gate — see CLAUDE.md). Kill switch $30. A daily cloud routine
reviews settled results and PRs proposals (CLAUDE.md "Daily review
routine"). First clean read since the focus config: roughly flat.

STATUS 2026-08-11: the $30 kill switch tripped at 08:53:56Z after the worst
day in the ledger (-$22.65/26h). Forensics attributed the loss to a
window-open toxic-flow regime, NOT a code defect and NOT the probes (see
research/ATTRIBUTION-WINDOW-OPEN-2026-08-11.md - the decisive doc for this
period). Restart change, one variable: min_seconds_after_open 5 -> 90
(workers skip the toxic first minute). Judge after ~24h settled; if losses
migrate to minute 13, next variable is a join_depth posting filter. Queued
behind that window: per-series spot_jump_bps (ZEC ~2x), raw spot prints in
fifteen_spot_pull payloads. (Routine halt detection SHIPPED
2026-08-11: the 6h pulse routine, research/ROUTINE-PULSE.md.)

Next-lever evidence queue (work these IN ORDER of evidence, via the daily
reviews + interactive attribution sessions):
1. SCHEDULED-RELEASE PULL (M6 candidate): the commodities have no tick feed
   for M4, but their jump moments are on a CALENDAR — EIA Petroleum Status
   (Wed 10:30 ET) for WTI, CPI (8:30 ET) and FOMC (14:00 ET) days for
   GOLD/SILVER. Pull quotes for +/- a few minutes around scheduled releases.
   Cheap, deterministic, and the exact defensive lever those series lack.
2. BOOK-IMBALANCE CONDITION: quote_decision already logs join_depth on both
   sides. Mine fills-vs-settlement conditional on the imbalance at join
   (Cont/Stoikov OBI literature says this is the strongest single
   microstructure signal). If thin-opposite-side joins are toxic, add an
   imbalance gate.
3. TILT WALK-DOWN: calibration says early-window favorites at 0.65-0.90
   carried the biggest edge (+2.7 to +3.1c, ~4 sigma). Test
   tilt_tail_threshold 0.90 -> 0.65 ONLY once per-lever attribution shows
   M1's current suppressions are net-positive.
4. HOUR-OF-DAY MAP: per-series expectancy by hour (the flow gate handles
   thin books reactively; data may justify hard schedules, e.g. commodities
   only during US market hours).
5. WEEKEND REGIME: underlying commodity markets close weekends — verify
   what KXGOLD/SILVER/WTI 15M windows do on Saturday and that the flow gate
   stands down as designed. First weekend under measurement: 2026-08-08/09.
6. SERIES RE-ENTRY PROBE (queued 2026-08-08; EXECUTED 2026-08-10 with
   KXXRP15M + KXZEC15M after walk-down day 2 met the gate - loss band
   eliminated two days running, gain side unconfirmed; verdict due after
   3-4 settled days): the alt-crypto cull was measured almost
   entirely PRE-LEVERS, so "toxic" is not settled law. But neglect only
   pays when the underlying is tame (commodities > BTC per contract, while
   neglected-and-wild alts bled), so probes must meet criteria, not
   sentiment: (a) never measured negative by us, (b) Coinbase spot feed so
   M4 covers them, (c) enters behind all six levers at 1-lot. Candidates:
   KXXRP15M and KXZEC15M (add their spot_products mappings XRP-USD,
   ZEC-USD when adding). BNB/HYPE re-entry requires the probe cohort to
   first prove the levers tame alt flow; NEAR/SOL/DOGE need stronger
   evidence still. Decision rule: 3-4 settled days, keep or drop on
   numbers via the daily review.

Deliberately NOT on the queue: speed. The reflex arc is network and
exchange round trips (50-300 ms) plus a deliberate 2 s requote throttle;
the code's share is microseconds after the 2026-08-07 O(1) fixes (see
README "The latency budget"). If a review ever attributes measured loss to
pull latency or queue position, the upgrade path is throttle -> colocation
-> order-entry protocol -> language, in that order, each step justified by
a number first. The edge under test is selection (which fills we refuse),
not reaction speed: BTC, the fastest book, pays our worst per-contract
edge.

Build (mode `fifteen`, keeps the calm-MM path intact for the record):

1. Static 15M universe, no selector: all nine series (KXBTC15M, KXETH15M,
   KXSOL15M, KXDOGE15M, KXBNB15M, KXHYPE15M, KXNEAR15M, KXGOLD15M,
   KXSILVER15M). Roll worker: discover the open window's market per series
   via /events, spawn worker at open, next window on close.
2. Sub-cent ticks: strategy.tick 0.001 (config; _fmt_price already sends 4dp).
   Verify each series' price_ranges step at discovery and refuse to quote a
   structure we do not understand.
3. Timing at seconds granularity: quote from open to T-75s, then cancel all
   (the last 60s is the settlement averaging window; quoting into it is a
   pure gift). Bypass close_reaper/min_hours_to_close for this mode. Hold
   fills to settlement (defined risk, <= $1/contract); the existing
   settlement poll realizes it.
4. Strategy: join-the-touch only, 1 contract per side per series (measurement
   size). No A-S reservation pricing: the model study proved the book's mid
   beats our model at every minute, so we do not price, we join. Inventory
   cap per series ~5; kill switch stays.
5. Instrumentation is the point: per-series achieved fill rate vs the study's
   trade-through rate, conditional net edge per fill vs settlement, queue
   position proxy (our price level's resting depth at join). Existing
   eventlog unchanged.
6. Go/no-go after ~1 week per series: measured net c/contract > 0 with CI
   excluding zero -> scale size on that series and recompute the annualized
   return on capital actually deployed; else drop the series. If all nine
   fail, the project concludes with the full map of why.

## Done

- **Kalshi MM core** — A-S quoting, websocket data, risk stack, decision logs.
- **Cross-venue Phase A: divergence recording.** `bacchus-mm crossvenue` polls
  manually-mapped Kalshi ↔ Polymarket pairs (public data, no credentials) into
  the `venue_marks` table; `analyze divergence` reports how often and how far
  the venues disagree. Use `pm-find` to discover Polymarket slugs for mapping.
- **P0 review batch (2026-07-17, from REVIEW-2026-07-17.md)** — risk-reducing
  orders always approved; resting-order-aware caps; cumulative cross-session
  PnL + account-equity kill switch (kv-chained); exchange-reconcile loop with
  sweep detection + cooloff (order-group trips / maintenance cancels no longer
  invisible, no more blind re-arm); fail-stop task supervision; fill dedup +
  fill-dispatch isolation; join-best policy A (margin 1c / min book 2c);
  fast-move guard confirmation (spread-scaled threshold, 2-update persistence,
  trips count only if the move persists past cooloff). See CLAUDE.md 2026-07-17
  section for the invariants an editing session must not regress. 91 tests.

- **P1 scale-safety + deploy batch (2026-07-17, branch p1-scaling-batch)** —
  batched eventlog writes + 14d events retention (fills/mids/pnl/kv stay
  synchronous); fee model (reported fee_cost preferred, formula fallback;
  net-of-fee PnL/markouts — net is the S2-gate number); marks tick +
  12h close reaper + settlement realization; ws recv timeout (resubscribe
  can't starve); order-group fail-closed on prod; create_order adopt-or-
  replace (never double-place); wind-down distress alerts (escalation
  plumbed, default none); cancels scoped to managed tickers (second strategy
  can share the account); Dockerfile + fly.toml + health endpoint +
  docs/deploy.md runbook (~$2.10/mo). 172 tests. See CLAUDE.md P1 section.

## Phase B — Polymarket as a fair-value signal (RETIRED 2026-08-06 with the calm-market thesis)

Feed the Polymarket mid into Kalshi quoting for mapped markets:
- shift the reservation price toward a volume-weighted blend of both venues;
- pull quotes (or widen hard) when the venues diverge sharply — divergence
  usually means one venue heard news first, and the stale venue is us;
- log the signal's contribution so markouts can prove/disprove its value.
Gate: `analyze divergence` shows divergence episodes are common enough to
matter and Kalshi is the laggard often enough to exploit defensively.

## Phase C — Polymarket execution (RETIRED 2026-08-06 with the calm-market thesis)

Spread survey 2026-07-15: PM's econ-adjacent books are TIGHTER than ours
(0.1-1c spreads, 0.1-0.25c ticks, mature MM ecosystem + liquidity rewards,
nonzero maker/taker base-fee fields), and only ~8 econ markets clear 1k/24h
volume in the top 400. MM there is unattractive for our categories; Kalshi's
coarse-tick niche books are the less-competed venue. Re-survey before
building this phase. Correction 2026-07-15 pm: PM does carry weather/temp
(~18k/day vol, but same-day-settlement — our banned category), tech/AI and
culture markets; conclusion unchanged since none pass our own risk rules
with better economics than Kalshi's niche books. (Also: PM's 0.1c ticks make small cross-venue
"divergence" partly tick-granularity artifact.)

Trade on Polymarket US (credentials via `scripts/add-polymarket-key.sh`):
- either market making on the PM side of mapped markets, or
- cross-venue basis trades when |divergence| exceeds round-trip costs.
Hard prerequisites before any of this:
- side-by-side resolution-rules review per pair (same source? same cutoff?
  a 4c gap on different resolution criteria is basis risk, not arb);
- taker fees on both legs priced in (PM US fee schedule + Kalshi taker fees);
- separate capital/risk caps and kill switch for the PM leg;
- proof from Phase A logs that gaps persist longer than our reaction time.

## External data ladder (evaluate in this order; every signal ships in
## shadow mode — logged next to quote decisions, judged against markouts —
## before it may influence quoting)

1. **Release calendars** — pull/widen quotes around scheduled data drops
   (CPI, retail sales, weather observation times). Cheapest, likely the
   biggest adverse-selection reduction available.
2. **Cross-venue prices** — Phase A/B; generic staleness alarm.
3. **Domain fair-value anchors** — CME FedWatch for Fed markets, Cleveland
   Fed nowcast for CPI, NWS forecasts for weather. Slow but grounding.
4. News/sentiment feeds — deliberately out of scope (latency game we lose).

## Strategy candidates beyond single-level A-S (post-proof, in rough order)

- **Intra-Kalshi structural consistency** — strike ladders must be monotonic,
  partition outcomes must sum to ~$1; quote legs against siblings. Same
  exchange, same resolution rules, so no cross-venue basis risk. Likely the
  best risk-adjusted expansion, ahead of Phase C. Fee model (its hard
  prerequisite) shipped 2026-07-17 — shadow scanner is unblocked.
- **Queue-aware quoting** — Kalshi exposes order queue position; keep/replace
  decisions should know whether we're near the front.
- **Multi-level laddering** — 2-3 levels per side once single-level earns.
- Settlement scalping (97-99c near-certainties): rejected — tail risk.

## Deep-review queue (2026-07-16 workflow findings, deferred deliberately)

- Sigma warmup seeding + realized-vol floor from own mids (A-S is effectively a
  constant 2.86c quoter today — 62.9% of decisions at the sigma floor, 85.4%
  live). Join-best policy A shipped 2026-07-17 — spread-math changes are now
  gated on the S1 evidence gate (REVIEW §5): markout@+600s ≥ −0.5c/contract
  over ≥60 fills post-policy-A.
- Flow-ranked selection: rank candidates by realized taker flow, not spread
  (economics dim: order flow, not spread width, is the binding constraint on
  income; current picks trade a handful of times/day). Overnight dead zone:
  all 10 first-day fills landed 07:31-18:22.
- Account-equity kill switch: DONE 2026-07-17 (cumulative PnL chained via kv
  table, high-water persisted; first run anchors high_water = offset so
  pre-upgrade losses don't trip). Startup worker jitter before scaling past
  ~10 markets (startup burst is 80% of the write budget). (Reduce-only orphan
  exits: DONE 2026-07-16, wind-down workers.)
- Stability-gated guard re-entry: DONE 2026-07-17 (spread-scaled threshold,
  2-update confirmation, trips count only when the move persists past
  cooloff; false alarms logged as guard_false_alarm).
- Phase B lead/lag: remeasure on near-50c contracts around FOMC/CPI catalysts
  (current Fed pairs at 0.95/0.05 cannot reprice — data uninformative).

## Watch items for the weekly review (2026-07-17 pre-wait check)

- **Wind-down exits on flickery books pay the flicker**: the 12 legacy
  orphans all exited within 20h (system works), but the 3 exits in flicker
  markets (gas-CPI, Austin/NYC rain) filled at spike extremes 18-25c through
  the mid (~-$2 of the flattening cost was exit slippage). Candidate fix if
  the pattern repeats on NEW wind-downs: reduce-only quotes should JOIN the
  book, never lead it (cap exit price at best +/- 1 tick). One-time legacy
  cost for now — selection fixes largely prevent entering such books.
- **Join policy A has zero baseline**: every fill under the conservative
  policy (2c/3c, 07-16 12:27 -> 07-17 15:30) was joined=False — the old
  band literally never produced a joined fill. Policy A's revert gate must
  be judged purely on this week's data.
- **kv equity chain anchored at upgrade time (2026-07-17 ~15:30)**: the $10
  cumulative kill switch measures from there, NOT from the $500 start
  (pre-upgrade -$3.23 is water under the bridge by design).

## Also queued

- Raise `selector.min_hours_to_close` if same-day settlement markets (daily
  temperature) prove toxic in observe logs.
- Fly.io deployment: ARTIFACTS DONE 2026-07-17 (Dockerfile, fly.toml, health
  endpoint, docs/deploy.md runbook, ~$2.10/mo). Remaining: owner runs flyctl
  auth + `fly deploy` (no flyctl on the build machine).
- Reconcile fills via REST on websocket reconnect (fills during the <=15min
  pre-TTL sleep window are invisible until restart). Partially done 2026-07-17:
  reconcile.py resyncs ORDER state every 45s live; historical FILLS during a
  disconnect are still unreconciled (dedup groundwork in place).
- Order amend instead of cancel/replace where it saves rate-limit tokens.
- Settlement handling mid-session: DONE 2026-07-17 (marks tick, 12h close
  reaper → wind-down, settlement realized into risk via on_settlement).
- In-session selector refresh (config has refresh_minutes but markets are
  currently fixed at session start; miscategorized picks persist until restart).
