# Attribution: the window-open loss and the 2026-08-11 kill-switch halt

Owner-session forensics (2026-08-11, post-halt, pre-restart). Data: a
consistent on-box snapshot of the fly SQLite DB taken after the halt (data
through 2026-08-11T08:53:56Z), plus REST settled reads via
research/daily_review.py. Three independent forensic passes (timeline,
microstructure, regime/defenses) were run in parallel and then adjudicated
against four candidate mechanisms. This file is the permanent record; the
analysis scripts were session scratch, but every method is described well
enough to reproduce from a DB snapshot.

## The event

The $30 cumulative-drawdown kill switch tripped at 2026-08-11T08:53:56Z
(drawdown $30.05; cumulative PnL -$16.24 vs the Aug 9 high-water +$13.81).
The halt was clean: zero resting orders, positions ~flat, equity $518.12.
The preceding 26h lost $22.65, the worst day in the ledger, with all five
incumbent series negative simultaneously for the first time.

## The finding: only the first minute was broken

From the tilt walk-down deploy (Aug 8 12:35Z) to the halt, total settled
PnL was -$0.40, decomposing into:

- minute-14 fills (first minute after window open): **-$14.91** on 3,237 ct
- minutes 13-1 fills (everything else): **+$14.51** on 7,493 ct

The minutes-13-1 control actually IMPROVED after the v20 probe deploy
(+0.12 -> +0.38 c/ct). The entire recent loss is a window-open phenomenon.

## Mechanism (adjudicated winner, ~80-85% confidence)

A toxic-flow regime at window opens: short-horizon informed sellers hitting
first-minute bids near 0.50 wherever the joined queue was thin. Onset
~Aug 10 07:00-15:00Z, HOURS BEFORE the v20 probe deploy (16:43Z), which
then amplified the dollar impact 2-3x by adding two thin-book series and
standing at the touch in 6-7 correlated markets per open instead of 2-3.

Evidence lines that triangulate:

1. TIMELINE: minute-14 cumulative PnL peaked +$11.91 at Aug 10 07:00Z and
   never made a new high; best changepoint 07:00Z (9.7h pre-deploy); the
   entirely pre-deploy 15:00-16:43Z slice already bled -2.78 c/ct. The
   exact v20 split (+0.27 -> -1.92 c/ct) is stark but permutation p=0.119:
   amplifier, not trigger. Post-deploy, minute-14 volume rose +61% and the
   probes added -$8.49 of the -$20.64 post-deploy minute-14 loss on only
   17% of its volume. ETH alone lost -$13.32; BTC (+$7.56) and GOLD
   (+$2.36) stayed POSITIVE at minute-14 throughout.
2. MICROSTRUCTURE: the money died in thin queues, on one side. Fills where
   the joined queue held <=20 contracts were the BEST bucket pre-regime
   (+2.00 c/ct) and became the worst (-7.90 c/ct; 72% of the toxic loss).
   Alone-at-touch fills tripled within series. Buys lost -6.20 c/ct at a
   45.8% win rate while sells made +2.22 c/ct; buy x 0.45-0.55 swung
   10.8 c/ct; post-fill mid drift ran against our buys (-1.45c at +30s,
   -2.66c at +180s), symmetric before. Meanwhile everything code-side
   measured UNCHANGED pre/post deploy: flow-gate release (median 2.0s
   both), open-to-first-quote (35.2 -> 37.6s), quoted size, M3 cap (zero
   engagements), tilt (behaved as designed; structurally cannot protect
   the 0.45-0.55 zone).
3. REGIME/DEFENSES: not a trend (settle persistence coin-flip or
   anti-persistent: BTC 36%, XRP 34%, ETH 48%; only GOLD trended at 60%)
   and not a defense outage (pulls/trips fired at 2.2-3x baseline all the
   way to the halt; zero taker fills). The drift tail fattened (p90 0.616
   -> 0.682) while the median fell: violent intra-window reversals, i.e.
   whipsaw, concentrated in commodities and XRP.

Hypotheses rejected on direct measurement: v20 code defect (all quoting
behavior unchanged; onset pre-dates deploy; control bucket improved),
probe interference with incumbents (ETH alone lost more than both probes;
incumbents-ex-ETH net positive post-deploy; shared-resource channels
measured absent), pure chance (the loss is simultaneously side-, price-,
and depth-specific across 16 hours and never recovered, unlike every
prior excursion), and trend-adverse-selection (persistence stats).

## The decision: one variable

`fifteen.min_seconds_after_open: 5 -> 90` (config-only; the knob has
existed since 08-06 as the book-forming guard in the discovery loop).
Workers now spawn at the first 10s discovery poll after open+90s, so the
bot simply does not exist in the toxic bucket. Everything else unchanged:
all 7 series kept (dropping probes would be the wrong variable), tilt,
flow gate, spot pulls, caps untouched.

Judgment plan (~24h of settled data after restart):

- Total PnL reverts to the minutes-13-1 baseline (~+$0.5-1/day at 1-lot):
  diagnosis confirmed, keep the delay.
- Losses migrate to minute 13: the informed flow follows the first touch;
  next single variable is a join_depth posting filter (require queue depth
  > ~100 at the joined level), not a longer delay.
- Weigh in the read: the toxic regime may itself decay; a flat read does
  not by itself prove the delay did the work (the changepoint analysis
  showed pre-regime minute-14 was only ~+0.3 c/ct anyway, so the delay
  forfeits little even if the regime passes).

## Queued follow-ups (AFTER this measurement window, one at a time)

1. Per-series spot_jump_bps: ZEC tripped 267 pulls in 16h (median move
   9.7bps against the global 8bps bar sized for BTC) costing 7.0% of its
   uptime in suppressed quoting. Opportunity cost only, no losses. Scale
   the threshold to baseline vol (~2x for ZEC).
2. Log raw spot prints in fifteen_spot_pull payloads (currently only
   {series, move_bps, cooloff}); direction/price would make the next
   forensic pass able to see what informed flow reacts to.
3. Daily routine halt detection: SHIPPED same day - `--pulse` mode in
   research/daily_review.py plus a 6h pulse trigger (see
   research/ROUTINE-PULSE.md), and the daily header now carries a
   last-account-fill staleness line.
4. Data quirk parked: ~8-12% of daily fill rows carry signed_count=0
   (fee-reported artifact rows, pre-existing since Aug 6, zero position
   impact) - worth a code look in the fill path someday.

## Restart mechanics (owner actions)

Merge + redeploy in the fly UI (the HALTED marker persists on the volume,
so the machine boots into halted-idle), then `halt-clear` inside the
machine. halt-clear rebases the high-water to current cumulative PnL, so
kill-switch protection resets to a fresh $30 from the restart level.
