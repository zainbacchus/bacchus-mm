# M1 tilt attribution and the walk-down decision (2026-08-08)

The 2026-08-08 daily review found the loudest signal this project has
produced: by price band of the side taken, our own fills ran -7.29c/contract
in the 0.10-0.35 band (n=1,320 ct, ~5.3se) and +7.83c/contract in the
0.65-0.90 band (n=1,298 ct, ~5.6se). ROADMAP's walk-down gate required one
more check before touching the tilt: per-lever attribution from the live
decision logs proving the CURRENT tilt's suppressions are net-positive.

## Method

quote_decision rows on the fly DB carry tilt_bid/tilt_ask flags (which side
M1 suppressed) plus the book top at that instant; settlement_realized rows
carry each window's outcome. Counterfactual: the fill M1 declined, at the
touch it declined to join, against the settle. Unit is the suppressed
market-minute (decisions repeat every ~2s; per-decision counting would
fabricate fills). Window: since the levers went live 2026-08-06T23:42Z.

## Result (491 settled suppressed market-minutes)

Sign convention, stated twice because the owner caught the first version
mixing them in one column: "counterfactual PnL" is what the DECLINED fills
would have earned; the BENEFIT of suppressing is its negation.

| suppression | minutes | counterfactual PnL of declined fills | benefit of suppressing |
|---|---|---|---|
| sell_favorite (ask >= 0.90) | 286 | -$8.59 (would have LOST) | +$8.59 avoided |
| buy_longshot (bid <= 0.10) | 205 | +$5.09 (would have won) | -$5.09 foregone |
| net | 491 | -$3.50 | +$3.50 avoided |

## Reading it honestly

Unconditional counterfactuals carry opposite biases on the two sides. For
declined SALES of favorites, reality is worse than the counterfactual (a
maker only gets filled when flow is informed), so the -$8.59 understates the
avoided damage. For declined BUYS of longshots, the +$5.09 assumes every
posted minute fills; conditioning on actually getting filled flips such
numbers hard - the adjacent 0.10-0.35 band's ACTUAL fills ran -7.29c/ct.
Both biases favor the tilt. Gate: PASSED, net-positive even at face value.

## Decision

tilt_tail_threshold 0.90 -> 0.65 (config committed with this note). The
walk-down suppresses exactly the measured -7.29c band (bids <= 0.35, asks
>= 0.65 whose taken side prices in 0.10-0.35) and keeps the +7.83c band
(buying favorites at 0.65-0.90). Watch item for the next reviews: fill
volume will drop (fewer quotable sides); judge the change on net dollars
and c/ct, not volume.
