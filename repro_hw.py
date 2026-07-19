from decimal import Decimal
from pathlib import Path
import tempfile
from bacchus_mm.risk import RiskManager, RiskParams

# Session A: market sat near 0.10, then determined YES. Real PnL locked.
rmA = RiskManager(params=RiskParams(kill_switch_drawdown=Decimal("10")), state_dir=Path(tempfile.mkdtemp()))
rmA.on_fill("X", 10, Decimal("0.10")); rmA.on_mid("X", Decimal("0.10"))
rmA.on_settlement("X", Decimal(1))
rmA.drawdown()  # ratchet
print("Session A: cumulative", rmA.cumulative_pnl, "high_water", rmA.high_water)

# Session B: restart, X still 'determined' & reported at +10, last logged mid 0.10
rmB = RiskManager(params=RiskParams(kill_switch_drawdown=Decimal("10")),
                  state_dir=Path(tempfile.mkdtemp()),
                  cumulative_offset=rmA.cumulative_pnl, high_water=rmA.high_water)
rmB.seed_position("X", 10, Decimal("0.10"))
rmB.on_settlement("X", Decimal(1))
rmB.drawdown()
print("Session B: cumulative", rmB.cumulative_pnl, "high_water", rmB.high_water)
print("TRUE lifetime PnL: 9.00; high_water inflated by", rmB.high_water - Decimal("9.00"))
# Effective kill-switch budget from truth: real losses now tolerated before halt
print("Kill switch now fires only after real cumulative <=", rmB.high_water - Decimal("10"),
      "(should be -1.00 measuring from true peak 9.00)")
