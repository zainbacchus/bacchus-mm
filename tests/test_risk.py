from decimal import Decimal

import pytest

from bacchus_mm.risk import RiskManager, RiskParams


@pytest.fixture
def rm(tmp_path):
    return RiskManager(
        params=RiskParams(
            max_contracts_per_market=20,
            max_notional_per_market=Decimal(100),
            max_gross_notional=Decimal(600),
            kill_switch_drawdown=Decimal(250),
        ),
        state_dir=tmp_path,
    )


def test_pnl_round_trip_captures_spread(rm):
    rm.on_mid("MKT", Decimal("0.50"))
    rm.on_fill("MKT", +10, Decimal("0.48"))   # bought 10 yes at 48
    rm.on_fill("MKT", -10, Decimal("0.52"))   # sold 10 yes at 52
    assert rm.pnl() == Decimal("0.40")        # 10 * 4 cents
    assert rm.markets["MKT"].position == 0


def test_buy_no_equals_sell_yes(rm):
    # A "buy no at 0.40" fill arrives as signed_count=-c, yes_price=0.60.
    rm.on_fill("MKT", -10, Decimal("0.60"))
    rm.on_mid("MKT", Decimal("0.55"))
    assert rm.pnl() == Decimal("0.50")  # short yes from 60, mid 55 -> +5c * 10


def test_unrealized_moves_with_mid(rm):
    rm.on_fill("MKT", +10, Decimal("0.50"))
    rm.on_mid("MKT", Decimal("0.45"))
    assert rm.pnl() == Decimal("-0.50")


def test_seeded_position_starts_flat(rm):
    rm.seed_position("MKT", 10, Decimal("0.60"))
    assert rm.pnl() == 0
    rm.on_mid("MKT", Decimal("0.65"))
    assert rm.pnl() == Decimal("0.50")


def test_kill_switch_measures_from_high_water_mark(rm):
    rm.on_fill("MKT", +10, Decimal("0.50"))
    rm.on_mid("MKT", Decimal("0.80"))   # up $3
    assert rm.should_halt() is None
    rm.on_mid("MKT", Decimal("0.55"))   # gave back $2.50... not enough on $250 switch
    assert rm.should_halt() is None
    rm.params.kill_switch_drawdown = Decimal("2.00")
    assert rm.should_halt() is not None


def test_halt_writes_and_clears_marker(rm, tmp_path):
    rm.halt("test reason")
    assert rm.halted
    assert (tmp_path / "HALTED").exists()
    assert "test reason" in rm.check_halt_file()
    assert rm.clear_halt()
    assert rm.check_halt_file() is None


def test_order_gate_contract_cap(rm):
    rm.on_fill("MKT", +18, Decimal("0.50"))
    ok, _ = rm.approve_order("MKT", +2, Decimal("0.50"))
    assert ok
    ok, reason = rm.approve_order("MKT", +5, Decimal("0.50"))
    assert not ok and "contract cap" in reason


def test_order_gate_blocks_when_halted(rm):
    rm.halt("x")
    ok, reason = rm.approve_order("MKT", +1, Decimal("0.50"))
    assert not ok and reason == "halted"


def test_order_gate_gross_notional(rm):
    for i in range(6):
        rm.on_fill(f"M{i}", +20, Decimal("0.50"))
    # 120 contracts gross = $120 worst-case... under 600. Push per-market notional:
    ok, _ = rm.approve_order("M0", +1, Decimal("0.50"))
    assert not ok  # per-market contract cap hit first (20)
    rm.params.max_gross_notional = Decimal("120")
    ok, reason = rm.approve_order("M9", +5, Decimal("0.50"))
    assert not ok and "gross" in reason
