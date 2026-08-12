# Attribution: the 2026-08-12 macro-lean night, the owner stop, and two refuted levers

Owner-session forensics, written the same night. Data: REST settled reads
via research/daily_review.py (authoritative money), the 23:54Z DB snapshot
(microstructure; the final 3h have REST-only coverage because the stopped
machine cannot be queried), and Coinbase 1-minute candles for the crypto
underlyings. Companion to ATTRIBUTION-WINDOW-OPEN-2026-08-11.md.

## The event

Second hostile night in a row. Post-restart session (v21 18:30Z, ZEC
removed at v22 00:27Z): settled -$22.93 on 1,423 contracts by the time the
owner stopped the machine at 2026-08-12T02:46:20Z with equity ~$496.3,
about $8 above the kill-switch line. The pulse routine's first live alarm
fired at 02:18Z (issue #9) - the monitoring shipped that afternoon worked.

The decisive table is the side split:

- buys:  +$68.75 on 724.4 ct = **+9.49 c/ct (~5.1se)**
- sells: -$91.68 on 698.6 ct = **-13.12 c/ct (~6.9se)**

Every series except WTI negative (GOLD -7.88, BTC -3.91, ETH -3.78,
ZEC -3.32 pre-removal, SILVER -2.54, XRP -1.57). The open-delay invariants
HELD all night: no minute-14 fills existed, minute-13 stayed positive -
this was not the window-open mechanism returning.

## Mechanism

A uniform upward lean across metals, crypto, and oil simultaneously - the
signature of a single macro impulse (one dollar-risk leg moves all five
underlyings the same way). A symmetric two-sided maker in a uniformly
leaning tape donates on the short side: every resting offer is lifted by
flow that is right about where the window settles, window after window,
while nothing ever jumps hard enough for M2/M4 to see. The portfolio's
seven "diversified" books were one macro bet. ZEC (removed mid-night on
its own evidence) was the same mechanism in its purest thin-book form.

## Refuted lever 1: the trailing-drift pull

The queued "drift detector" (pull while trailing N-minute underlying drift
exceeds a bar) was parameterized against tonight's crypto fills (767 fills
with settle + 1-min underlying drift). It fails:

- Losing sells were NOT concentrated on trailing up-drift: sells ran
  -6.33 c/ct on FLAT tape (246 ct, the biggest bucket), -13.61 on
  up-drift, and -13.05 even on down-drift (mean-reversion: dips bounced).
- Both-sides pull, threshold sweep (10-min window): the best bar (8bps)
  suppresses 34% of volume to save $1.34 of the $10.69 crypto loss; kept
  volume still runs -2.55 c/ct. The 3-min window is no better.
- Adverse-side-only pull is BACKWARDS: at 6bps it suppresses fills
  carrying +$7.74, because buys after down-drifts were the night's best
  trade (+40.59 c/ct on 29 ct).

Conclusion: the counterparties' information was about the FUTURE path,
not the trailing one; a trailing-drift signal cannot price it. Rejected
before costing a build and a measurement window.

## Refuted lever 2: the hour-of-day curfew

Three consecutive overnight-UTC bleeds suggested a clock structure. The
lifetime map (21,327 fills, Aug 6 -> Aug 11 23:54Z) says no: US session
(12-21Z) -0.07 c/ct vs off-hours +0.03 c/ct, both ~0.1se; no single hour
clears 1se. Adding tonight's missing final 3h (~-$16 across 00-02Z) still
leaves those hours inside noise. The bleeds are episodic regimes, not
schedule-bound. Rejected as unsupported.

## The surviving lever: a per-side rolling-expectancy tripwire (in-bot)

The one place tonight's signal provably lived was in OUR OWN outcomes:
the side split reached ~2se within the first couple of hours and 6.9se by
the stop. Generalization of all three episodes to date: do not predict
the regime, detect the bleed-in-progress faster than $20. Sketch:

- Rolling window (~2h) of per-side settled-plus-marked c/ct and contracts.
- Trip: a side worse than about -8 c/ct on >= 100 contracts (roughly 2se
  at typical volume) -> stop quoting THAT side book-wide for a cooloff of
  hours, keep the healthy side, emit attribution telemetry, resume and
  re-arm.
- Backtest sanity: would have fired ~21:30-22:30Z tonight (saving roughly
  half the night) and during the 08-11 pre-halt morning (buys side).
  False-fire rate at 2.2se on 2h checks is ~once per 6 days, and the cost
  of a false fire is hours of one-sided quoting, not a halt.
- This is src/ work (new lever, tests, telemetry): an interactive-session
  build with daylight, deployed by the owner, judged by the pick-off
  report's side gap after restart.

## Restart criteria

Stay stopped until the side tripwire ships. The restart config is then:
open delay (kept: its invariants held) + six series + tripwire, judged on
the pick-off report and the daily reviews. CPI morning (2026-08-12
12:30Z) passes with the machine down, which is fine - M6 would have
pulled around it anyway. The account stands ~$496 vs $525 lifetime
deposits; the kill switch never tripped tonight because the owner beat it
to the stop by $8.
