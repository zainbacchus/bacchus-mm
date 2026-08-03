# Where can a small passive maker live on Kalshi? (2026-08-02)

Motivation: the first clean live week showed **-1.17c/contract net** (95% CI
[-2.00,-0.34]) in calm low-flow markets. Question: does pivoting to
higher-flow markets ("option B") have upside, or is it structurally dead?

Method (scripts in this dir, public API only, no auth):
1. `scan_universe.py` — every open market with a 2-sided book: spread, 24h volume
2. `trade_study.py` — per-market trade COUNT + median SIZE (retail vs institutional)
3. `jump_study.py`  — separate TREND (24h range) from JUMPS (max move in any 5min)

## Findings

**Universe:** 47,821 open 2-sided markets; 32,079 in the 0.10-0.90 price band.

**Flow x spread grid confirms spread is compensation, not opportunity.** Volume
concentrates at 1c spreads (178 markets with >10k/day at 1c) — the
professionally-quoted zone. As spread widens, volume falls away.

**The "middle band" EXISTS by volume+spread:** 609 markets with >=500
contracts/day AND 2-4c spread, 6.1M contracts/day of volume. Categories:
Sports 257, Elections 67, Politics 46, Financials 46, Entertainment 41.

**Flow is genuinely retail-sized** (good news): median trade 16 contracts,
41% of trades <=10 contracts. That IS the uninformed flow a maker wants.

**But the band is toxic. This is the killer:**
- 43 of 45 high-flow band markets: max 5-minute move > 2x spread
- **median max-5min move = 22c against a 2-4c spread** (5-10x)
- only 1/45 survivable, and it had 4 trades/hour (no more flow than we have now)

## Conclusion

Flow and volatility are the SAME phenomenon here: people trade because
something is happening. A calm + wide-spread + high-flow market does not exist
because markets price it away — if it were calm and wide, the 1c-tick pros
would compress it; the 2-4c spread you see IS the premium demanded for 22c
jump risk, and it is priced for someone who requotes in microseconds.

=> Option B (pivot to flow) is NOT viable for a passive quoter at retail
latency. Option A (calm markets) has spread but ~no flow, and measured
negative edge. **The opportunity space for passive MM here is closed.**

The remaining untested idea is NOT market making: intra-Kalshi structural
consistency (strike ladders must be monotonic; partition legs must sum to ~$1).
That is arbitrage — it takes rather than makes, needs persistence not speed,
and has defined risk. See ROADMAP "Strategy candidates".
