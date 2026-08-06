# Two studies: structural arbitrage, and the 15-minute crypto markets

Date: 2026-08-06. Public API only, no auth, no capital at risk. Scripts in this
directory. Follows MARKET-STRUCTURE-STUDY-2026-08-02.md, which closed off
passive MM in both calm (option A) and high-flow (option B) markets.

---

## Study 1: intra-Kalshi structural arbitrage

### Method

Four no-arbitrage constraints, each a logical identity rather than a model:

1. **Ladder monotonicity.** In a threshold ladder ("$X or above"), K_lo < K_hi
   means the K_lo event contains the K_hi event, so P(K_lo) >= P(K_hi) in every
   state. Violation: `bid(K_hi) > ask(K_lo)`.
2. **Partition bid-sum.** In a `mutually_exclusive` event at most one leg pays
   $1, so `sum(bid) > 1` means sell every leg. Rigorous on exclusivity alone.
3. **Partition ask-sum.** `sum(ask) < 1` means buy every leg. This one also
   needs exhaustiveness, which the scan PROVES (both tails present, every
   bucket boundary contiguous) rather than assuming.
4. **Cross-book butterfly.** Kalshi lists the same underlying and expiry twice:
   `KX<C>D` as a threshold ladder and `KX<C>` as range buckets. Identity:
   `bucket[lo,hi] == P(>=lo) - P(>=next_rung)`. Two independent order books
   must agree, so disagreement is a 3-leg arb.

Fees: taker `round_up(0.07 * C * P * (1-P))`, charged on every leg.

Three scanner bugs were found and fixed before any result was trusted. Each had
manufactured fake ~98c "arbs", so they are documented in `arb_scan3.py`:
hardcoded rung offsets, exhaustiveness assumed from the ME flag, and (the
subtlest) treating a `yes_ask` of 0.0000 as a real price when on Kalshi an
absent offer is reported as zero with zero size. Legs are now only counted as
tradeable when the price is strictly inside (0,1) AND the side we would hit has
at least 10 contracts resting.

### Result: zero net-positive arbs

Single snapshot, 9,362 events / 77,963 markets (35,538 with real 2-sided depth):

| check | gross violations | net-of-fee positive |
|---|---|---|
| ladder monotonicity | 0 | 0 |
| partition bid-sum | 15 | 0 |
| partition ask-sum | 0 | 0 |
| cross-book butterfly | 2 | 0 |

Because one snapshot cannot see a bursty opportunity, `arb_poll.py` then sampled
the crypto ladder families every ~3 minutes for 45 minutes:

- rounds with a gross violation: **11 of 14**
- rounds with a net-of-fee-positive arb: **0 of 14**
- best gross seen 3.00c; **best NET seen -0.57c**

### Conclusion

Gross inconsistency is common (79% of rounds) but the fee wall is never
breached. Ladder monotonicity is never violated at all: the closest the market
ever came was 0.80c INSIDE the bound. At its very best the market came within
0.57c of a free lunch and did not cross it.

That is exactly what an efficient-with-frictions market looks like. **The
residual mispricing IS the fee**, which means someone has already taken
everything above the fee threshold, and we pay the same fee they do. There is
no structural-arb business here.

---

## Study 2: the 15-minute crypto markets

### What these contracts actually are

Not strike markets. `KXBTC15M` resolves YES iff the 60-second average of CF
Benchmarks' BRTI before close is at least the 60-second average before open.
The target is therefore **fixed and known at open**, and the contract is a pure
**return** bet: "is the price higher than 15 minutes ago".

Two consequences. Fair value is fully computable, needing no view or
information. And it is **basis-free**: no BRTI level is required, only a return,
so Coinbase spot is a legitimate input.

Family: BTC / ETH / SOL / DOGE / BNB / HYPE / NEAR, plus GOLD and SILVER.
There is no 5-minute series; 15 minutes is the shortest cadence Kalshi lists.

Fair value at t with tau minutes left and r = ln(S_t / S_open):
`P = Phi( r / (sigma * sqrt(tau)) )`, sigma from trailing 1-minute realised vol.
The drift term is ~1e-6 at this horizon and is dropped.

### Q1: is Coinbase a valid proxy for BRTI? Yes, 96.2%

Across 260 settled KXBTC15M markets the sign of the Coinbase 15-minute return
matched Kalshi's actual settlement in **250/260 = 96.2%**. The 3.8% miss is
index basis on near-zero-return markets, and is an irreducible error floor for
anyone without BRTI itself.

### Q2: the market prices this better than the model, at every minute

Brier score (mean squared error vs outcome, lower better) over 3,480
market-minute observations:

| minute of life | Brier model | Brier market mid | winner | median spread |
|---|---|---|---|---|
| 1 | 0.2315 | 0.2256 | market | 1.00c |
| 5 | 0.1849 | 0.1782 | market | 1.00c |
| 9 | 0.1276 | 0.1199 | market | 1.00c |
| 11 | 0.1078 | 0.1018 | market | 0.10c |
| 14 | 0.0663 | 0.0401 | market | 0.10c |
| **overall** | **0.1565** | **0.1493** | **market** | |

The market wins in all 14 minutes and the gap widens toward expiry. Taking on
the model's disagreement loses money at every threshold, and loses MORE as the
disagreement grows, with a hit rate below a coin flip:

| threshold | trades | gross c/ct | fee c/ct | net c/ct | hit% |
|---|---|---|---|---|---|
| 1c | 2,614 | -2.868 | 1.067 | **-3.935** | 31.8% |
| 3c | 1,744 | -3.294 | 1.034 | **-4.328** | 26.0% |
| 8c | 597 | -3.991 | 0.932 | **-4.923** | 18.6% |

So a spot-driven model does not produce better fair value than what is already
in the book. "Use Coinbase to inform where we quote" does not clear the bar,
because the book already knows.

### Q3: passive quoting, and the adverse-selection law again

`crypto15_make.py` simulates joining the book at the observed bid/ask. These
markets live 15 minutes, so settlement IS the markout horizon and realised PnL
of a fill is exactly `(outcome - price) - fee`, no proxy needed. Two numbers,
and the difference between them is the whole story: UNCONDITIONAL assumes fills
arrive at random; CONDITIONAL only counts a fill when the market actually traded
through our level.

| series | spread | uncond. net | **conditional net** | adverse selection |
|---|---|---|---|---|
| NEAR | 4.00c | +1.623c | **-1.936c** | -3.56c |
| SOL | 1.00c | +0.313c | **-1.109c** | -1.42c |
| SILVER | 1.00c | +0.639c | **-0.852c** | -1.49c |
| ETH | 1.00c | +0.456c | **-0.456c** | -0.79c |
| GOLD | 2.00c | +0.807c | **-0.235c** | -1.04c |
| BTC | 1.00c | +0.087c | **-0.015c** | -0.10c |

(net shown at maker rate 0.0175; see the fee question below)

Same law as the option-B study, now measured at the fill rather than the market
level: **the spread is compensation for very nearly exactly the adverse
selection you will suffer.** NEAR has the widest spread in the family and the
worst outcome. A wide spread here is a warning label, not an opportunity.

### The fee question, which decides everything

Kalshi's published schedule (fetched, dated effective 2026-07-07):

- taker `round_up(M x 0.07 x C x P x (1-P))`, M default **1**
- maker `round_up(M x 0.0175 x C x P x (1-P))`, M default **0**
- rounding is to a **centicent** ($0.0001), not a cent
- a "Non-Standard Fees" table lists 86 series with explicit maker/taker
  multipliers (76 at maker=1, 10 at maker=0)

**No 15-minute series appears in that table**, and `CRYPTO15M.pdf` contains
contract specs only with no fee terms. Read literally, maker M defaults to 0, so
the 15M markets carry **no maker fee**. That flips the table above:

| series | conditional net @ maker 0.0175 | conditional net @ maker 0 |
|---|---|---|
| **BTC** | -0.015c | **+0.278c** |
| GOLD | -0.235c | **+0.060c** |
| ETH | -0.456c | -0.154c |
| SILVER | -0.852c | -0.570c |
| SOL | -1.109c | -0.800c |
| NEAR | -1.936c | -1.637c |

At +0.278c/contract, and 1.98M contracts per 15-minute BTC market (190M/day
across the series), capturing 0.01% of that flow is ~$1,600/month.

**This contradicts our own measurement.** An earlier session measured ~0.0189
on live maker fills in series (KXHIGHNY, KXHIGH, KXLOWT) that are ALSO absent
from the table and should therefore have been free. 0.0189 is close enough to
0.0175 that measurement noise on a small sample plus centicent rounding could
explain it, which would mean the table is not the whole rule. Unresolvable from
here: the local DB predates the `fee` column migration and the live data is on
the fly volume.

**RESOLVED 2026-08-06 (same day, later session):** data/fly-snapshot-4.db has
115 maker fills with exchange-reported fees. The split is perfectly clean and
matches the table 15/15: KXCPI, KXFED, KXCPIYOY (in-table, maker=1) were
charged round_up_centicent(0.0175 x C x P x (1-P)) EXACTLY, per fill; all 12
absent-from-table series (KXGTEMP, KXMUSKNW, KXAAAGASMAX, ...) were charged
zero. The earlier "~0.0189" was the centicent round-UP inflating the implied
rate on small fills of IN-TABLE series, not a different rate, and not fees on
absent series. So the schedule reading stands: **the 15M series pay no maker
fee, and KXBTC15M passive quoting is +0.278c/contract gross.** The remaining
blocker is assumption 2 below (the fantasy fill rate), which is now the ONLY
open question. fees.py encodes the confirmed model as of this date.

### Conclusion

Do not build this on the strength of the +0.278c. Every assumption underneath it
is one we cannot currently hold:

1. **Maker fee = 0 is inferred, and contradicted by our own fills.** If the true
   rate is 0.0175, BTC is -0.015c and the entire family is negative.
2. **The 98.7% fill rate is fantasy.** The simulation counts a fill whenever the
   next minute traded through our level. On a book doing 2M contracts per 15
   minutes with sub-cent ticks we would be at the back of a very deep queue,
   filling on the sweeps (when we are wrong) and missing the touches (when we
   are right). Realistic queue modelling erodes +0.278c and could pass zero.
3. **Sub-cent ticks mean cheap undercutting.** Holding queue priority costs 0.1c
   of a 0.5c half-spread, repeatedly.
4. **The book prices this better than we do** (Q2, every minute). Any quote not
   exactly at the market's fair value is a free option to someone faster.

Two cheap decisive tests, in order:

- **Settle the fee question for about $2.** Rest one small order in KXBTC15M,
  let it fill, read the reported `fee_cost`. Binary answer, ends the ambiguity.
  *(DONE 2026-08-06 without a trade, see RESOLVED note above: our existing
  fills confirm the table. Assumption 1 is retired.)*
- **Then measure real fill rate and its selection bias** by quoting minimum size
  in KXBTC15M only, for a few days, and comparing achieved fills against the
  98.7% assumed here. *(Now the only open question.)*

Both use the bot as built. Neither is a rewrite.
