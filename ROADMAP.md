# Roadmap

## Done

- **Kalshi MM core** — A-S quoting, websocket data, risk stack, decision logs.
- **Cross-venue Phase A: divergence recording.** `bacchus-mm crossvenue` polls
  manually-mapped Kalshi ↔ Polymarket pairs (public data, no credentials) into
  the `venue_marks` table; `analyze divergence` reports how often and how far
  the venues disagree. Use `pm-find` to discover Polymarket slugs for mapping.

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

## Also queued

- Raise `selector.min_hours_to_close` if same-day settlement markets (daily
  temperature) prove toxic in observe logs.
- Fly.io deployment (Dockerfile + volume for data/) once local operation is boring.
- Order amend instead of cancel/replace where it saves rate-limit tokens.
- Settlement handling mid-session (positions in settled markets currently
  just stop marking).
- Orphan-position management: when the selector drops a market we still hold
  (first case 2026-07-15: +1 KXPCECORE after fills), no worker quotes the exit
  — position rides to settlement unless manually closed. Consider a wind-down
  worker that keeps a reduce-only ask on dropped positions.
- In-session selector refresh (config has refresh_minutes but markets are
  currently fixed at session start; miscategorized picks persist until restart).
