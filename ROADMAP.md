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

Trade on Polymarket US (credentials via `scripts/add-polymarket-key.sh`):
- either market making on the PM side of mapped markets, or
- cross-venue basis trades when |divergence| exceeds round-trip costs.
Hard prerequisites before any of this:
- side-by-side resolution-rules review per pair (same source? same cutoff?
  a 4c gap on different resolution criteria is basis risk, not arb);
- taker fees on both legs priced in (PM US fee schedule + Kalshi taker fees);
- separate capital/risk caps and kill switch for the PM leg;
- proof from Phase A logs that gaps persist longer than our reaction time.

## Also queued

- Raise `selector.min_hours_to_close` if same-day settlement markets (daily
  temperature) prove toxic in observe logs.
- Fly.io deployment (Dockerfile + volume for data/) once local operation is boring.
- Order amend instead of cancel/replace where it saves rate-limit tokens.
- Settlement handling mid-session (positions in settled markets currently
  just stop marking).
