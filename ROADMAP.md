# Roadmap

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

## Phase B — Polymarket as a fair-value signal (future)

Feed the Polymarket mid into Kalshi quoting for mapped markets:
- shift the reservation price toward a volume-weighted blend of both venues;
- pull quotes (or widen hard) when the venues diverge sharply — divergence
  usually means one venue heard news first, and the stale venue is us;
- log the signal's contribution so markouts can prove/disprove its value.
Gate: `analyze divergence` shows divergence episodes are common enough to
matter and Kalshi is the laggard often enough to exploit defensively.

## Phase C — Polymarket execution (future, needs its own risk review)

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
  best risk-adjusted expansion, ahead of Phase C.
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
- Fly.io deployment (Dockerfile + volume for data/) — priority raised
  2026-07-16: lid-close sleep blacked out ~7h of quoting across two episodes
  in one day; laptop hosting is now the biggest single uptime cost.
- Reconcile fills via REST on websocket reconnect (fills during the <=15min
  pre-TTL sleep window are invisible until restart). Partially done 2026-07-17:
  reconcile.py resyncs ORDER state every 45s live; historical FILLS during a
  disconnect are still unreconciled (dedup groundwork in place).
- Order amend instead of cancel/replace where it saves rate-limit tokens.
- Settlement handling mid-session (positions in settled markets currently
  just stop marking).
- In-session selector refresh (config has refresh_minutes but markets are
  currently fixed at session start; miscategorized picks persist until restart).
