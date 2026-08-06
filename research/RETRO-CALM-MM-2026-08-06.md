# Retrospective: calm-market passive MM on Kalshi (2026-07-08 to 2026-08-06)

Status: CONCLUDED. The selector is wound down (config.yaml sentinel category,
2026-08-06); held positions exit via reduce-only wind-down workers. The
codebase, infrastructure, and analysis loop carry forward to Phase D
(15-minute markets, see ROADMAP.md).

## The thesis, and why it failed

Thesis: quote calm, slow Kalshi markets (Economics, Climate) where spreads are
2-10c wide and nobody fast is competing, capture the spread passively with
Avellaneda-Stoikov quoting, small size, hard risk caps.

Result: the spread was compensation, not free money. Three studies plus three
weeks of live fills converged on the same law from different directions:

1. **Live markouts** (115 maker fills, 328 contracts): net markout
   -1.45c/contract at +600s over all history; -1.17c/contract (95% CI
   [-2.00, -0.34], t=-2.75) over the clean final week. The mid moved against
   our fills before the print: gross edge at fill was already negative. Buys
   were picked off worse than sells (-2.39c vs -0.42c): informed flow hits
   the bid ahead of downward moves.
2. **Market-structure study** (research/MARKET-STRUCTURE-STUDY-2026-08-02.md):
   the "flow + wide spread + stable" population is ~empty. 609 markets have
   flow and 2-4c spreads, but 43/45 sampled had 5-minute jumps > 2x their
   spread (median 22c against 2-4c). Spread scales with jump risk because
   the market prices it; what is left after the pros compress the calm books
   is exactly the toxic residue.
3. **Structural arb scan** (research/ARB-AND-15MIN-STUDY-2026-08-06.md): gross
   no-arbitrage violations are common (11/14 poll rounds) but NET of fees
   positive in 0/14. The residual mispricing is the fee wall.

The flow that does exist in calm markets is a trickle (a few fills a day at
size 1-2), so even a zero-edge outcome would round to zero dollars. There is
no parameter tuning that fixes "the counterparty knows more and there are not
enough uninformed counterparties."

## The numbers

- Capital: $500 deposit + $25 Kalshi bonus. Equity $518.61 at the 2026-08-02
  snapshot (reconciles to expected $519.17 within noise).
- Cumulative trading PnL: about -$3.87 over ~3 weeks of live quoting.
- Total exchange fees paid: $0.1218 (all maker fills; in-table series only).
- Fills: 115 (all maker, `bmm-` post-only), 328 contracts, 16 markets.
- Infra cost: ~$2.10/month on fly.io.

Cheap tuition. The kill switch ($10 drawdown) was never hit.

## What was built and is NOT concluded (carries forward)

The bot itself works and its safety record is clean:

- Exchange adapter (RSA-PSS auth, ws books + fills, token buckets, reconcile
  loop with sweep detection), fail-stop supervision, resting-aware risk caps,
  cumulative account-equity kill switch, settlement realization, wind-down
  workers so positions are never abandoned, single-instance flock, health
  endpoint, fly.io deploy.
- The analysis loop (SQLite event firehose -> Claude sessions -> parameter or
  code changes) proved out: every strategy decision in this project traces to
  a query over data/bacchus.db.
- Fee model now confirmed against exchange-reported fills 15/15 (fees.py,
  2026-08-06): centicent round-up, 0.0175 maker only on Non-Standard-table
  series, zero maker fee elsewhere (including every 15M series).

## Operational lessons (the expensive ones)

1. **The Dockerfile never copied config.yaml** and the bot ran 8 live days on
   pure code defaults ($250 kill switch, size 5, no blocklists) before a
   session noticed `config == {}` in session_start. Fixed with a fail-closed
   startup gate + loaded_files logging. Lesson: fail closed on missing
   config, log what was loaded, verify the deployed artifact not the repo.
2. **Fly's GitHub auto-deploy never fired**; every deploy was manual in the
   UI. Verify deployment freshness by session_start events, not by pushes.
3. **Adversarial swarm audits caught what 187 passing tests did not**: guard
   blind to multi-step collapses, inverted persistence scoring, halt-clear
   that did not re-arm, sweep-detector false positives, a dropped in-flight
   ws message on resubscribe. Tests verify what you thought of; adversaries
   find what you did not.
4. **Watch items that stay relevant for any future mode**: roster starvation
   (evictions outpaced promotions), guard false-alarm rate 76%, wind-down
   exits sitting unfilled (join the book, do not lead it), fractional-fill
   int() truncation.

## Wind-down state (2026-08-06)

Held positions at the Aug 2 snapshot (12 gross contracts, 9 tickers):
KXAAAGASMAX +1, KXCBDISRAEL -1, KXCPIYOY +1, KXECONSTATCPI -1, KXFM30YMTG -2,
KXGDPYEAR -1, KXGTEMP-26 -2, KXGTEMP-27 -1, KXLCPIMAXYOY +2. Worst-case
exposure if every exit fails and every settlement goes against us: ~$12.
Several are far-dated (Dec 2026 / Jan 2027) and illiquid, so wind-down orders
may sit; if still unfilled after a few days, flatten manually in the Kalshi UI
or simply hold to settlement.

Procedure: redeploy in the fly UI (picks up the sentinel config; auto-deploy
does not fire), let wind-down workers exit, confirm flat via
`bacchus-mm equity`, then stop the machine in the fly UI or leave it as the
platform for Phase D.
