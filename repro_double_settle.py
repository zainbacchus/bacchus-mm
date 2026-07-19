"""Repro: cross-session double-realization of a settled position.

`MarketState.settled` is in-memory only. If the bot restarts while a market is
'determined' but not yet 'finalized' (Kalshi still reports the position in
/portfolio/positions during that window), session B re-seeds the position and
the settlement poll realizes it AGAIN, on top of the offset that already
contains session A's realization.
"""
from decimal import Decimal
from pathlib import Path
import tempfile

from bacchus_mm.risk import RiskManager, RiskParams


def cumulative_after_session(offset, hw, seed_pos, seed_mid, settle):
    rm = RiskManager(
        params=RiskParams(),
        state_dir=Path(tempfile.mkdtemp()),
        cumulative_offset=offset,
        high_water=hw,
    )
    # session start: seed the exchange-reported position at last logged mid
    rm.seed_position("X", seed_pos, seed_mid)
    # ... market determines; settlement poll realizes it
    q, basis, realized = rm.on_settlement("X", settle)
    return rm.cumulative_pnl, rm.high_water, realized


# ---- Session A: fresh position, bought 10 YES @ 0.40, determines YES (=1.0)
rmA = RiskManager(params=RiskParams(), state_dir=Path(tempfile.mkdtemp()))
rmA.on_fill("X", 10, Decimal("0.40"))
rmA.on_mid("X", Decimal("0.55"))          # last book we ever logged: mid 0.55
qA, basisA, realizedA = rmA.on_settlement("X", Decimal(1))
cumA = rmA.cumulative_pnl
print(f"Session A realized: {realizedA}  cumulative persisted to kv: {cumA}")

TRUTH = Decimal(10) * (Decimal(1) - Decimal("0.40"))
print(f"TRUE lifetime PnL for X: {TRUTH}")

# ---- Session B: restart while X still 'determined' (position still reported),
# last logged mid row is 0.55 (settlement wrote NO mid row; marks_tick skips
# settled markets, and on_settlement never calls record_mid).
cumB, hwB, realizedB = cumulative_after_session(
    offset=cumA, hw=cumA, seed_pos=10, seed_mid=Decimal("0.55"), settle=Decimal(1)
)
print(f"Session B re-realized: {realizedB}  reported cumulative: {cumB}")
print(f"OVER-COUNT vs truth: {cumB - TRUTH}")

# ---- Now the adverse variant: last mid 0.55, market determines NO (=0)
rmA2 = RiskManager(params=RiskParams(), state_dir=Path(tempfile.mkdtemp()))
rmA2.on_fill("X", 10, Decimal("0.45"))
rmA2.on_mid("X", Decimal("0.55"))
_, _, rA2 = rmA2.on_settlement("X", Decimal(0))
cumA2 = rmA2.cumulative_pnl
truth2 = Decimal(10) * (Decimal(0) - Decimal("0.45"))
cumB2, hwB2, rB2 = cumulative_after_session(
    offset=cumA2, hw=max(cumA2, Decimal(0)), seed_pos=10, seed_mid=Decimal("0.55"), settle=Decimal(0)
)
print()
print(f"[NO-result gap] session A realized {rA2}; truth {truth2}")
print(f"  reported cumulative after B re-realization: {cumB2}  (phantom = {cumB2 - truth2})")
