# bacchus-mm

An open-source market-making lab for prediction markets, quoting on
[Kalshi](https://kalshi.com). Built around one idea: **every decision is
logged with enough context that an LLM (or a human) can reconstruct and
critique it later.** You run the bot; your logs become the dataset you
iterate on. The strategies have changed as the data came in; the logging
loop is the product that survived.

**In plain English:** a market maker is the currency-exchange booth at the
airport. It posts one price to buy and a slightly higher price to sell, and
lives on the gap between them. This bot runs tiny booths inside Kalshi's
15-minute yes/no markets ("will Bitcoin be higher in 15 minutes?"): it
stands ready to buy the YES side a fraction of a cent below the going rate
and to sell it a fraction above, and a completed round trip earns that
fraction. Thousands of round trips a day, pennies at a time.

The catch is that some customers at the booth know the exchange rate is
about to move, and they only trade with you when you are about to be wrong.
So nearly all of the bot's intelligence goes into REFUSING trades: it stands
down when a market goes quiet (the casual traders leave, only the sharp ones
remain), cancels its offers the instant the underlying price jumps, refuses
the side of extreme markets that history says loses, and caps how much it
can be wrong in any single market. Then everything it did is written down,
and the written record is what improves: strategies here get measured,
kept, or retired based on what the diary says. The first strategy (calm,
slow markets) was measured, found unprofitable, and retired with a
retrospective; the current one is its data-driven replacement.

**What kind of bot this is (and isn't):** not a grid bot. A grid bot lays
out a ladder of fixed price levels in advance and waits for price to
oscillate through them; the ladder ignores the live market, and the classic
failure mode is inventory piling up in a trend. This bot has no ladder: it
quotes exactly two prices, the current best bid and best ask, and those
quotes follow the book wherever it goes. It is a market maker, but a
deliberately humble kind - passive and queue-based. It never prices (no
fair-value model; the market's mid measurably beat ours), never leads or
tightens the spread, never crosses; it joins the price the market already
agreed on and sells its place in line, and its real edge is the refusal
machinery above. Where a full market maker works inventory back to flat,
this bot lets the 15-minute expiry do the unwinding: every window settles,
so inventory is flushed flat, with defined risk, every quarter hour. In
booth language: a grid bot sets out price tags in advance and hopes the
crowd walks through them; this booth stands wherever the crowd is currently
haggling, and profits by being picky about customers.

## The goal, stated plainly

Test whether a small passive market-making bot on Kalshi's 15-minute
markets can earn **at least 2x the risk-free rate, annualized, on
$500-$20,000 of capital**. Risk-free is ~4%, so the bar is ~8%/yr: about
11 cents/day on $500, about $4.40/day on $20K. At current fill volume that
is a fraction of a cent per contract of positive expectancy, so the whole
project reduces to one measurable question: **is settled-PnL-per-contract
positive, with a confidence interval that excludes zero?**

Current phase: measurement at 1-contract size. Decision rules: each market
series earns or loses its slot on its own settled numbers via the daily
review; a positive series gets a size walk-up (its own experiment, since our
size moves our queue position); if no configuration measures positive, the
project concludes and the repo stands as the documented map of why. Hard
floor under all of it: a $30 cumulative-drawdown kill switch on a ~$500
bankroll.

## Current status: Phase D (active)

`bacchus-mm fifteen --live` quotes Kalshi's **15-minute up/down markets**:
join-the-touch, one contract per side, rolling to each new 15-minute window
as it opens. The active series list lives in `config.yaml` and is managed by
the daily review loop below (currently BTC, ETH, Gold, Silver, and WTI
crude; the alt-crypto series were measured negative and dropped). No pricing model at
all, on purpose. Three measured facts drive the design (see
[research/](research/)):

1. The market's own mid out-predicted a spot-feed fair-value model at every
   minute of window life, so the bot joins the book instead of pricing it.
2. These series carry **no maker fee** (confirmed against exchange-reported
   fills), so a captured spread is kept whole.
3. The one number no study could simulate is the real fill rate at our real
   queue position. Measuring it is the point of this phase; per-series
   go/no-go comes from fill-vs-settlement PnL, not backtests.

Mechanics specific to this mode: quotes are computed from the book
*excluding our own resting order* (join, never lead, never cross), pulled
75 seconds before close (the final 60 seconds is the settlement averaging
window), and fills ride to settlement with hard inventory caps. Windows
whose price structure the bot cannot fully parse are refused, not guessed
at. See [ROADMAP.md](ROADMAP.md) Phase D for the full spec.

On top of the symmetric join, six **evidence levers** shape which fills the
bot declines (each independently disable-able in config, each logging its
own attribution telemetry so reviews can score them separately):

1. *Favorite-longshot tilt*: never sell the favorite or buy the longshot
   beyond the threshold (0.65 as of 2026-08-08). First calibrated on 1,440
   settled windows, then walked down when the bot's own fills showed the
   0.10-0.35 band losing 7.29c/contract and the 0.65-0.90 band earning
   7.83c, both beyond 5 sigma, with live-log attribution confirming the
   suppressions were net-positive. Consistent with Buergi, Deng & Whelan
   (2026), who find Kalshi makers on >=50c contracts earn +2.6% after fees.
2. *Toxicity pull*: quotes are pulled during one-sided repricing bursts
   (one-sided flow predicts maker losses; Bartlett 2026) and resume after a
   short cooloff.
3. *Price-shaped inventory caps*: worst-case dollars per market, so tail
   positions get the smallest contract caps (the "Black-Scholes for
   prediction markets" handbook's boundary guidance).
4. *Spot-jump pull*: an external spot feed (Coinbase) cancels a series'
   quotes for a cooloff when the underlying jumps. Strictly a cancel
   trigger, never a pricing input: a spot-driven fair-value model measurably
   lost to the market's own mid at every minute of window life, but the slow
   maker's real edge is refusing to be the stale quote (Budish et al.).
   Series without a free spot feed run without this lever, a natural
   control group.
5. *Flow gate*: quote only while the book is demonstrably alive (a minimum
   number of book updates in the trailing window). First-evening data:
   windows with plentiful fills ran near flat while thin windows ran -13 to
   -24c/contract, because when casual flow disappears the only remaining
   counterparties are informed ones. Standing down IS the position; it
   doubles as an automatic overnight curfew.
6. *Scheduled-release pull*: the commodities have no tick feed for the
   spot-jump lever, but their jump moments are on a calendar. Quotes are
   pulled for a few minutes around scheduled releases (EIA petroleum status
   weekly for WTI; CPI and FOMC dates for the metals and majors),
   maintained by the daily review.

## The self-improvement loop

The bot never edits itself. The loop that improves it has exactly two human
checkpoints and runs daily:

1. **The bot trades and logs** (fly.io, 24/7): every quote decision, order,
   fill, and safety-lever action, with enough context to replay it.
2. **Every morning a scheduled cloud agent reviews the day.** A claude.ai
   routine pulls the fills and settlement results using committed, read-only
   code ([research/daily_review.py](research/daily_review.py)), writes the
   day's review into `research/daily/`, and opens a pull request: headline
   expectancy with error bars, per-series verdicts, tilt evidence, anomaly
   checks, and at most three config proposals with the evidence rows quoted.
   Statistical humility is enforced: evidence that does not clear the bar
   produces explicit HOLDs, not tweaks.
3. **A running ledger** lives at
   [research/daily/LEDGER.md](research/daily/LEDGER.md): one row per UTC day
   of bot performance (trades, markets, contracts, dollar volume, fees,
   settled PnL), rebuilt idempotently every morning back to the account's
   first fill. Account equity deliberately lives in the review files
   instead, where the daily balance-vs-PnL reconciliation check reads it.
4. **A human merges and deploys.** Review PRs that only record the day merge
   on sight; a PR carrying a PROPOSAL commit changes `config.yaml` and takes
   effect only after the owner merges AND redeploys. Nothing reaches the
   live bot without both.

The routine cannot trade (read-only calls from committed code only), cannot
touch bot source, and must self-report any deviation from its rules; its
first run did exactly that, flagging two ad hoc read-only calls it made and
the rule was tightened the same day. Deeper attribution work (per-lever
effects from the bot's decision logs) happens in interactive sessions; the
routine guarantees the settled truth gets measured every single day, the
same way, against the account balance.

## The retired strategy (and why it is still in the repo)

The original bot selected calm markets (Economics, Weather) and quoted
[Avellaneda-Stoikov](https://www.math.nyu.edu/~avellane/HighFrequencyTrading.pdf)
reservation prices with inventory skew. Three weeks live measured a
significantly negative per-contract edge: the wide spreads in quiet markets
turned out to be fair compensation for adverse selection, and the flow was
too thin to matter anyway. The full post-mortem, with numbers, is
[research/RETRO-CALM-MM-2026-08-06.md](research/RETRO-CALM-MM-2026-08-06.md).
The code path remains (`bacchus-mm run`) both for the record and because its
wind-down machinery still manages any legacy positions. The Polymarket
cross-venue phases were retired with it.

The [research/](research/) directory is the lab notebook: market-structure
studies, an intra-exchange arbitrage scan (gross violations are common; net
of fees there were zero), the 15-minute-market studies, the fee-schedule
verification that flipped the current strategy from marginal to viable, and
a calibration study of 1,440 settled 15-minute windows that fills a hole in
the published literature (the large calibration papers exclude prices beyond
5c/95c and stop at the 1-hour horizon; ours measures exactly that regime and
finds the favorite-longshot tilt alive at 15 minutes).

## Why Kalshi (the venue landscape)

The US prediction-market boom is a distribution war (Coinbase, Robinhood,
apps) sitting on top of very few actual order books. Coinbase's prediction
markets route to Kalshi; Robinhood routes across Kalshi, ForecastEx, and
its own Rothera exchange, and its BTC 15-minute markets carry Kalshi's
exact contract spec. Retail flow from the big apps therefore aggregates
onto the books this bot quotes.

Kalshi is also the only one of these venues with an OPEN maker API, and
the reason is business model, not accident: standalone exchanges sell
liquidity, so they give the spread to anyone willing to quote (Kalshi
charges makers nothing on most series). Broker-built venues sell their own
captive flow to affiliated makers (Rothera is a Robinhood-Susquehanna
joint venture), so they stay closed on purpose. This bot's entire niche is
that exception, and the niche is stage-dependent: if the venue ever drifts
toward flow capture (house makers, broad maker fees), the edge narrows.
The daily review watches the fee schedule for exactly that reason. Venue
diligence lives in research/ (see the Crypto.com census for a worked
example of a venue that fails the test).

### Who else is at the booth, and why there is room

Professional market makers ARE on these books and capture most of the
spread; this bot's flow is a rounding error beside theirs. Three things
leave crumbs at the margin. Price-time queues churn: pros cancel and
requote constantly, and every cancel promotes the small order behind them.
Retail flow arrives in sweeps that punch through displayed size to whoever
is next. And professional attention concentrates where volume justifies a
desk, leaving the long tail thin - a pattern this repo's own ledger
confirms: BTC, the most professionalized book, shows the bot's worst
per-contract edge (near perfect competition), while the newest and
smallest series show its best. The niche is a size window: the prize is
too small for a salaried desk above, and the operational bar (fee
forensics, adverse selection, measurement discipline - all documented
here) filters out most bots below. The moat is cost structure plus
competence, not speed - which is also why scaling the bot would erode the
very mismatch that makes the niche exist.

## The latency budget (why the edge is selection, not speed)

A natural question: would a faster implementation (a compiled language, a
faster box) capture more spread? Budget the reflex arc first. From "the
world changed" to "our order changed", a requote or a defensive pull
spends roughly:

| Link in the chain | Typical cost |
|---|---|
| Market data transit (Kalshi / Coinbase websocket to the box) | a few to tens of ms |
| Processing the update in Python (O(1) hot paths) | 1-2 microseconds |
| Deciding (join-touch policy, guards, caps) | microseconds |
| Deliberate requote throttle (`fifteen.requote_min_interval`) | 2,000 ms |
| Order or cancel REST round trip to Kalshi | 50-300 ms, sometimes seconds |
| Queue standing at the exchange | not time at all: FIFO priority |

The code's share of that chain is about 0.001%. Rewriting it in a faster
language would shave microseconds off a path that is priced in
milliseconds and throttled in seconds. The repo's one real performance
incident (the 2026-08-07 event-loop stalls) was an algorithms problem,
O(n) scans on every book update, and the fix was data structures (a
monotonic-deque window minimum, an incremental best-price cache), not a
faster language. At these message rates the interpreter is nowhere near
its ceiling.

If speed ever did matter, the honest upgrade path attacks the big links
first: lower the throttle (rate-limit tier permitting), colocate next to
Kalshi's matching engine, tune order entry (connection reuse, FIX), and
only then reconsider the language - and the exchange's rate limits cap
requote frequency no matter what the code is written in. Each step gets
taken only after a review attributes a measured loss to pull latency or
queue position; none has yet.

The deeper reason there is no speed work on the roadmap: the measured
edge is selection, not reaction. The ledger's per-contract numbers say
BTC, the one book where latency-competitive desks live, pays this bot's
worst edge, while the neglected books pay multiples more. Speed decides
who wins the race to a newly formed price level; this bot's PnL comes
from refusing bad fills (the tilt, the flow gate, the pulls, the caps),
a game decided at the decision, not on the wire. Getting faster would
not move the selection numbers; it would enter the bot in a race the
professionals already win.

## How it works

- **Window discovery (fifteen)**: polls each configured series for its open
  15-minute window, validates the price grid (the crypto series use a
  piecewise tick: 0.001 in the tails, 0.01 in the middle), spawns a quoting
  worker per window, retires it after close.
- **Quoting**: join-the-touch at measurement size. The A-S model, EWMA
  volatility, and fast-move guard still exist for the legacy path but are
  deliberately out of the fifteen loop.
- **Market data**: Kalshi websocket (`orderbook_delta` + `fill` channels);
  the bot re-quotes on book changes, throttled per market, with full local
  books so it can tell "the bid" from "our bid".
- **Risk**: per-market and gross caps checked before every order; a
  drawdown kill switch that cancels everything, writes a `HALTED` marker,
  and refuses to restart until you acknowledge with `halt-clear`; plus a
  Kalshi order-group so the *exchange* cancels all orders if fills exceed a
  rolling 15-second contract limit (protection that works even if the bot
  is wedged).
- **Fees**: modeled per series from Kalshi's published schedule and
  verified against exchange-reported fills: taker 0.07 x C x P x (1-P)
  rounded up to a centicent; maker 0.0175 on ~76 listed series and **zero
  everywhere else**, including every 15-minute series. The websocket's own
  `fee_cost` is always preferred over the model.
- **Logs**: JSONL event stream + SQLite mirror in `data/`: every quote
  decision (book top, inventory, queue depth at the joined level), order
  event, fill (with mid-at-fill), mid marks, and a PnL curve.
  `bacchus-mm analyze markouts` computes post-fill drift; for 15-minute
  markets, settlement itself is the markout horizon.

## Quick start

```bash
uv sync

# 1. Create a demo account at https://demo.kalshi.co and generate an API key.
cp .env.example .env   # then fill in your key ID and private key path

# 2. The active strategy: quote the 15-minute markets (demo env by default).
uv run bacchus-mm fifteen --observe   # log decisions, place no orders
uv run bacchus-mm fifteen             # trade on demo

# 3. Read the logs:
uv run bacchus-mm analyze summary
uv run bacchus-mm analyze markouts
uv run bacchus-mm analyze incidents

# The retired calm-market strategy is still runnable for study:
uv run bacchus-mm markets   # what its selector would pick
uv run bacchus-mm observe
```

Going live on prod requires **both** `live.enabled: true` (or the
`BACCHUS_LIVE_ENABLED=1` env var) and the `--live` flag, a two-key
deliberate action, never a default. Before the first live run,
`bacchus-mm selftest --live` proves the order plumbing with a single
1-contract $0.01 post-only round trip (place, rest, cancel, verify).

## Deploy (fly.io)

For 24/7 operation the bot deploys to fly.io as a single worker Machine with
a 1GB volume for `data/`, an internal `/health` machine check, and
auto-restart. The calm-market era ran on the smallest VM (~$2/mo); the
15-minute firehose starved its event loop (order placements and the health
probe timing out together), so the deployed size is now shared-cpu-2x with
1GB RAM, roughly $10/mo. The repo ships the `Dockerfile`, `fly.toml` (whose
process command is the deployed strategy), and the step-by-step runbook:
[docs/deploy.md](docs/deploy.md). The 15-minute markets generate a lot of
events; see the runbook's "Disk" note. Deliberately a SINGLE machine: two
machines would be two bots doubling exposure and fighting over orders, and
the single-instance lock does not reach across hosts.

## Configuration

`config.yaml` holds public defaults, including the `fifteen:` block. Create
`config.local.yaml` (gitignored) and override anything; your tuned
parameters stay private even though the code is public. Credentials come
only from the environment / `.env` (gitignored). Containers get no
`config.local.yaml`, and the bot refuses to trade prod with no config file
loaded at all.

## Safety model

1. Post-only orders: a quote that would cross is rejected, never a taker fill.
2. Client-side caps: per-market contracts, per-market notional, gross
   notional; in fifteen mode additionally a worst-case dollar-loss cap per
   market that shrinks contract caps toward the price boundaries.
3. Kill switch: drawdown from the ACCOUNT-equity high-water mark (chained
   across sessions) ≥ threshold → cancel all, halt. `halt-clear` re-arms by
   rebasing the high-water mark to current equity: clearing a halt means
   "loss acknowledged; protect me from here."
4. Exchange-side order group: Kalshi cancels everything if the group trades
   more than N contracts in any rolling 15s window.
5. Order TTLs: resting orders expire server-side even if the bot dies.
6. Settlement-window pull (fifteen): all quotes canceled 75s before each
   window closes; the bot never rests an order into the averaging period
   that fixes the settlement value.
7. Structure gate (fifteen): a market whose price grid the bot cannot fully
   parse is refused, never quoted.
8. Startup hygiene: cancels any stale resting orders from previous sessions;
   shutdown verifies zero resting orders and says so loudly if not.

## Honest expectations

Market making on prediction markets is a fight against adverse selection.
This repo's own history is the proof: the first strategy's wide spreads in
calm markets measured out as fair payment for being picked off, and it was
shut down on that evidence. A small passive bot should be judged on
per-contract expectancy after fees against settlement, not on monthly income
targets. Run it small, read the logs, and let the data decide what happens
next; everything this project has learned so far is written down in
[research/](research/).

The first live day of fifteen mode is a fair preview of the workflow: the
kill switch tripped on mark-to-market noise within one window cycle, the
settled truth measured 3.4x smaller than the marked loss, the incident
surfaced two real bugs (fractional-fill truncation blinding the caps, and a
crash loop that blocked re-arming), and the fixes plus four evidence-based
strategy levers shipped the same day. Expect the bot to halt, expect the
logs to explain why, and expect the strategy you deploy next week to differ
from this one.

This is not financial advice; use at your own risk. See [LICENSE](LICENSE).
