from decimal import Decimal

import pytest

from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import BookTop, Order
from bacchus_mm.marketmaker import MarketWorker, WorkerConfig
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams


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


# ------------------------------------------------------------ C2 (2026-07-17)
# Risk-reducing orders are always approvable — rejecting exits over cap traps
# inventory (hit live when the cap dropped 20->10 mid-session while holding 13).

def _rm_capped(tmp_path, cap: int) -> RiskManager:
    return RiskManager(
        params=RiskParams(max_contracts_per_market=cap),
        state_dir=tmp_path,
    )


def test_risk_reducing_order_approved_over_cap(tmp_path):
    rm = _rm_capped(tmp_path, 10)
    rm.on_fill("MKT", +13, Decimal("0.50"))
    ok, reason = rm.approve_order("MKT", -2, Decimal("0.50"))
    assert ok and reason == "risk reduction"


def test_same_direction_order_still_rejected_over_cap(tmp_path):
    rm = _rm_capped(tmp_path, 10)
    rm.on_fill("MKT", +13, Decimal("0.50"))
    ok, reason = rm.approve_order("MKT", +1, Decimal("0.50"))
    assert not ok and "contract cap" in reason


def test_halted_still_blocks_reductions(tmp_path):
    rm = _rm_capped(tmp_path, 10)
    rm.on_fill("MKT", +13, Decimal("0.50"))
    rm.halt("x")
    ok, reason = rm.approve_order("MKT", -2, Decimal("0.50"))
    assert not ok and reason == "halted"


# ------------------------------------------------------------ C3 (2026-07-17)
# Caps see the true worst case: position + all resting same-direction orders.

def test_resting_bids_count_toward_cap(tmp_path):
    rm = _rm_capped(tmp_path, 20)
    rm.on_fill("MKT", +15, Decimal("0.50"))
    ok, _ = rm.approve_order("MKT", +5, Decimal("0.50"))
    assert ok  # 15 + 5 = 20, at the cap
    rm.register_order("MKT", "bid", 5)  # that bid is now resting
    ok, reason = rm.approve_order("MKT", +1, Decimal("0.50"))
    assert not ok and "contract cap" in reason  # worst case 15+5+1 = 21
    ok, _ = rm.approve_order("MKT", -1, Decimal("0.50"))
    assert ok  # reductions always pass (C2 precedence)


def test_release_on_cancel_or_fill_frees_capacity(tmp_path):
    rm = _rm_capped(tmp_path, 20)
    rm.on_fill("MKT", +15, Decimal("0.50"))
    rm.register_order("MKT", "bid", 5)
    rm.release_order("MKT", "bid", 5)  # canceled
    ok, _ = rm.approve_order("MKT", +1, Decimal("0.50"))
    assert ok
    rm.register_order("MKT", "bid", 5)
    rm.release_order("MKT", "bid", 2)  # partial fill
    assert rm.resting[("MKT", "bid")] == 3


def test_double_release_clamps_at_zero(tmp_path):
    rm = _rm_capped(tmp_path, 20)
    rm.register_order("MKT", "bid", 5)
    rm.release_order("MKT", "bid", 5)
    rm.release_order("MKT", "bid", 5)  # cancel racing a TTL-expiry
    assert rm.resting[("MKT", "bid")] == 0


def test_c2_reduction_precedes_resting_exposure_math(tmp_path):
    rm = _rm_capped(tmp_path, 10)
    rm.on_fill("MKT", +13, Decimal("0.50"))
    rm.register_order("MKT", "bid", 10)  # worst-case buy exposure way over cap
    ok, _ = rm.approve_order("MKT", -2, Decimal("0.50"))
    assert ok  # reduction approved regardless of resting math
    ok, _ = rm.approve_order("MKT", +1, Decimal("0.50"))
    assert not ok


class _StubExchange:
    """Minimal adapter double: just enough for MarketWorker._reconcile."""

    def __init__(self):
        self._n = 0
        self.canceled: list[str] = []

    async def create_order(self, ticker, side, price, count, client_order_id,
                           expiration_seconds=None, post_only=True):
        self._n += 1
        return Order(order_id=f"o{self._n}", client_order_id=client_order_id,
                     ticker=ticker, side=side, price=price, count=count)

    async def cancel_order(self, order_id):
        self.canceled.append(order_id)


@pytest.mark.asyncio
async def test_worker_registers_and_releases_resting_exposure(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    ex = _StubExchange()
    w = MarketWorker("MKT", exchange=ex, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(), dry_run=False)
    w.top = BookTop("MKT", Decimal("0.48"), 10, Decimal("0.52"), 10, 0)  # mid 0.50
    await w._requote()
    assert risk.resting == {("MKT", "bid"): 5, ("MKT", "ask"): 5}
    # Partial fill releases only the filled contracts.
    risk.on_fill("MKT", +2, Decimal("0.48"))
    w.order_filled(w.bid_order.order_id, 2)
    assert risk.resting[("MKT", "bid")] == 3
    # Requote replaces the bid (count 3 -> shaded 4): cancel releases, place
    # re-registers. (Long 2 shades quote size 5 -> 4 via the inventory curve.)
    await w._requote()
    assert risk.resting[("MKT", "bid")] == 4
    assert risk.resting[("MKT", "ask")] == 5
    # Shutdown releases everything.
    await w.stop()
    assert risk.resting == {("MKT", "bid"): 0, ("MKT", "ask"): 0}
    events.close()


# -------------------------------------------------------- FIX-PnL (2026-07-17)
# PnL chains across sessions; the kill switch measures account-equity drawdown.

def test_cumulative_pnl_includes_offset(tmp_path):
    rm = RiskManager(params=RiskParams(), state_dir=tmp_path,
                     cumulative_offset=Decimal("-3.00"), high_water=Decimal("-3.00"))
    assert rm.cumulative_pnl == Decimal("-3.00")
    rm.on_fill("MKT", +10, Decimal("0.50"))
    rm.on_mid("MKT", Decimal("0.55"))
    assert rm.pnl() == Decimal("0.50")
    assert rm.cumulative_pnl == Decimal("-2.50")


def test_kill_switch_trips_on_cumulative_drawdown_with_flat_session(tmp_path):
    rm = RiskManager(
        params=RiskParams(kill_switch_drawdown=Decimal("2.00")),
        state_dir=tmp_path,
        cumulative_offset=Decimal("0"),
        high_water=Decimal("5.00"),  # account was up $5 before this session
    )
    assert rm.pnl() == 0  # session completely flat...
    assert rm.should_halt() is not None  # ...but account is $5 off its high


def test_first_run_high_water_anchors_at_offset(tmp_path):
    # No persisted high-water on the first run after the upgrade: anchor at the
    # loaded offset so pre-upgrade losses don't instantly trip the switch.
    rm = RiskManager(
        params=RiskParams(kill_switch_drawdown=Decimal("0.50")),
        state_dir=tmp_path,
        cumulative_offset=Decimal("-3.00"),
    )
    assert rm.high_water == Decimal("-3.00")
    assert rm.should_halt() is None


def test_new_high_water_flag_set_when_cumulative_pnl_exceeds(tmp_path):
    rm = RiskManager(params=RiskParams(), state_dir=tmp_path)
    assert not rm.new_high_water_since_load
    rm.on_fill("MKT", +10, Decimal("0.50"))
    rm.on_mid("MKT", Decimal("0.60"))  # +$1.00 cumulative
    rm.drawdown()
    assert rm.high_water == Decimal("1.00")
    assert rm.new_high_water_since_load


# ------------------------------------------------- M3: settlement realization

def test_settlement_long_yes_win(rm):
    # Long 5 YES at 0.40; market settles yes ($1): +5 x (1.00 - 0.40) = +$3.00.
    rm.on_fill("MKT", 5, Decimal("0.40"))
    q, basis, realized = rm.on_settlement("MKT", Decimal(1))
    assert q == 5 and basis == Decimal("0.40") and realized == Decimal("3.00")
    st = rm.markets["MKT"]
    assert st.position == 0 and st.settled
    assert rm.pnl() == Decimal("3.00")  # frozen into cash, no mid needed


def test_settlement_long_yes_lose(rm):
    # Long 5 YES at 0.40; settles no ($0): -$2.00.
    rm.on_fill("MKT", 5, Decimal("0.40"))
    q, basis, realized = rm.on_settlement("MKT", Decimal(0))
    assert realized == Decimal("-2.00")
    assert rm.pnl() == Decimal("-2.00")


def test_settlement_short_yes_win(rm):
    # Short 5 YES-equivalent at 0.40 (= bought 5 NO at 0.60, cost $3); market
    # settles no -> NO pays $1 each: +$2.00.
    rm.on_fill("MKT", -5, Decimal("0.40"))
    q, basis, realized = rm.on_settlement("MKT", Decimal(0))
    assert q == -5 and basis == Decimal("0.40")  # yes-equivalent entry
    assert realized == Decimal("2.00")
    assert rm.pnl() == Decimal("2.00")


def test_settlement_short_yes_lose(rm):
    # Same short; market settles yes -> the NO side dies: -$3.00.
    rm.on_fill("MKT", -5, Decimal("0.40"))
    _, _, realized = rm.on_settlement("MKT", Decimal(1))
    assert realized == Decimal("-3.00")
    assert rm.pnl() == Decimal("-3.00")


def test_settlement_is_net_of_fee(rm):
    # Fees were booked at fill time; settlement adds none: long 5 at 0.40
    # with a $0.09 fee realizes 3.00 - 0.09.
    rm.on_fill("MKT", 5, Decimal("0.40"), fee=Decimal("0.09"))
    _, basis, realized = rm.on_settlement("MKT", Decimal(1))
    assert basis == Decimal("2.09") / 5  # net-of-fee average entry (2.00 + 0.09)
    assert realized == Decimal("2.91")
    assert rm.pnl() == Decimal("2.91")


def test_settlement_seeded_unvalued_position_starts_flat(rm):
    # A position seeded from the exchange with no mid ever seen: the seed
    # convention (session PnL starts at zero) values it at settlement.
    rm.seed_position("MKT", 5, None)
    q, basis, realized = rm.on_settlement("MKT", Decimal(1))
    assert q == 5 and realized == Decimal("0.00")
    assert rm.pnl() == Decimal("0.00")


def test_settlement_without_position_marks_terminal(rm):
    # Flat market that settles: no cash movement, but it is marked settled
    # and stops marking elsewhere (marks_tick skips it).
    rm.on_mid("MKT", Decimal("0.60"))
    q, basis, realized = rm.on_settlement("MKT", Decimal(1))
    assert q == 0 and realized == 0
    assert rm.markets["MKT"].settled
    assert rm.markets["MKT"].last_mid == Decimal(1)
