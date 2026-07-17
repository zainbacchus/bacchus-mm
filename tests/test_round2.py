"""Round-2 regression tests: the adversarial-review findings on the P0 branch.

Each test encodes a reproduced failure from the 2026-07-17 review:
  R2-1  multi-step collapses must trip the guard (windowed cumulative check)
  R2-2  persistence scoring vs the PRE-move reference (was inverted)
  R2-3  spread scaling uses the prevailing pre-shock spread (was self-defeating)
  R2-4  sweep detector: TTL-age veto, settle-and-confirm, count trigger
  R2-5  foreign (non bmm-) resting orders are never orphan-canceled
"""

from decimal import Decimal

import pytest

from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import BookTop, Order, Side
from bacchus_mm.marketmaker import FastMoveGuard, MarketWorker, QuotingGate, WorkerConfig
from bacchus_mm.reconcile import reconcile_pass
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams

from test_reconcile import StubExchange  # reuse the branch's adapter double


def g(**kw):
    args = dict(threshold=Decimal("0.03"), window_s=30, cooloff_s=180,
                spread_multiple=Decimal("0.75"), confirm_updates=2)
    args.update(kw)
    return FastMoveGuard(args.pop("threshold"), args.pop("window_s"), args.pop("cooloff_s"),
                         args.pop("spread_multiple"), args.pop("confirm_updates"))


# ---------------------------------------------------------------- R2-1
def test_one_cent_step_collapse_trips_within_window():
    # The motivating incident shape: 0.40 -> 0.14 walked down in 1c steps.
    guard = g()
    t = 1000.0
    mid = Decimal("0.40")
    tripped_at = None
    for i in range(26):
        guard.update(mid, ts=t + i * 0.5, spread=Decimal("0.03"))
        if guard.blocked(ts=t + i * 0.5):
            tripped_at = mid
            break
        mid -= Decimal("0.01")
    assert tripped_at is not None, "1c-step collapse never tripped the guard"
    assert tripped_at >= Decimal("0.32"), f"tripped too late, at {tripped_at}"
    # And the latched reference is the pre-move side of the window.
    assert guard.trip_ref > tripped_at


def test_two_cent_step_collapse_trips():
    guard = g()
    t = 1000.0
    mid = Decimal("0.40")
    for i in range(13):
        guard.update(mid, ts=t + i * 1.0, spread=Decimal("0.04"))
        if guard.blocked(ts=t + i * 1.0):
            break
        mid -= Decimal("0.02")
    assert guard.blocked(ts=t + 13), "2c-step collapse never tripped"


# ---------------------------------------------------------------- R2-2
@pytest.mark.asyncio
async def test_sticking_move_is_confirmed_via_real_trip(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(guard_evict_trips=99), dry_run=True)
    # Real trip through guard.update: 8c gap down (>= 2x base threshold).
    w.guard.update(Decimal("0.40"), spread=Decimal("0.02"))
    w.guard.update(Decimal("0.32"), spread=Decimal("0.06"))
    assert w.guard.blocked() and w.guard.trip_ref == Decimal("0.40")
    w.guard._blocked_until = 0.0  # cool-off over; the move STUCK at 0.32
    w.top = BookTop("MKT", Decimal("0.30"), 10, Decimal("0.34"), 10, 0)
    await w._requote()
    assert w._guard_trips == 1  # confirmed — a repricing that stuck counts
    events.close()


@pytest.mark.asyncio
async def test_reverting_bounce_is_false_alarm_via_real_trip(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(guard_evict_trips=99), dry_run=True)
    w.guard.update(Decimal("0.40"), spread=Decimal("0.02"))
    w.guard.update(Decimal("0.32"), spread=Decimal("0.06"))  # trip, ref 0.40
    w.guard._blocked_until = 0.0  # cool-off over; the move fully REVERTED
    w.top = BookTop("MKT", Decimal("0.39"), 10, Decimal("0.41"), 10, 0)  # mid 0.40
    await w._requote()
    assert w._guard_trips == 0  # false alarm — a bounce must not count
    n = events.db.execute(
        "SELECT COUNT(*) FROM events WHERE type='guard_false_alarm'").fetchone()[0]
    assert n == 1
    events.close()


# ---------------------------------------------------------------- R2-3
def test_shock_spread_blowout_does_not_raise_the_bar():
    # Calm 2c book, then the shock print blows the spread to 12c: eff must use
    # the PREVAILING 2c spread, so the 7c gap still trips immediately.
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0, spread=Decimal("0.02"))
    guard.update(Decimal("0.40"), ts=1001.0, spread=Decimal("0.02"))
    guard.update(Decimal("0.33"), ts=1002.0, spread=Decimal("0.12"))
    assert guard.blocked(ts=1002.5)


def test_effective_threshold_capped_at_2x_base():
    # 20c-wide book: 0.75 x 20c = 15c would blind the guard; cap holds it at 6c.
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0, spread=Decimal("0.20"))
    guard.update(Decimal("0.47"), ts=1001.0, spread=Decimal("0.20"))  # 7c > 2x3c cap
    assert guard.blocked(ts=1001.5)


# ---------------------------------------------------------------- R2-4 / R2-5
def _worker_with_order(events, risk, gate, ex, ticker, oid, age_s, side=Side.BID):
    import time as _t
    w = MarketWorker(ticker, ex, StrategyParams(), risk, events,
                     WorkerConfig(), dry_run=False, gate=gate)
    o = Order(order_id=oid, client_order_id=f"bmm-{oid}", ticker=ticker,
              side=side, price=Decimal("0.40"), count=2)
    o.placed_monotonic = _t.monotonic() - age_s
    w.bid_order = o
    return w, o


@pytest.mark.asyncio
async def test_ttl_aged_mass_vanish_is_not_a_sweep(tmp_path):
    # Post-sleep wake: every order expired via TTL (age ~ TTL). Not a sweep.
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    gate = QuotingGate()
    ex = StubExchange()  # exchange book empty: everything vanished
    workers = {}
    for i, t in enumerate(("A", "B", "C", "D")):
        workers[t], _ = _worker_with_order(events, risk, gate, ex, t, f"o{i}", age_s=880)
    report = await reconcile_pass(ex, workers, risk, events, gate,
                                  sweep_cooloff_seconds=900, ttl_seconds=900)
    assert len(report.vanished) == 4 and report.ttl_explained == 4
    assert not report.sweep and not gate.blocked()
    events.close()


@pytest.mark.asyncio
async def test_clock_jump_pass_never_classifies_sweep(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    gate = QuotingGate()
    ex = StubExchange()
    workers = {}
    for i, t in enumerate(("A", "B", "C", "D")):
        workers[t], _ = _worker_with_order(events, risk, gate, ex, t, f"o{i}", age_s=10)
    report = await reconcile_pass(ex, workers, risk, events, gate,
                                  sweep_cooloff_seconds=900, ttl_seconds=900,
                                  clock_jumped=True)
    assert not report.sweep and not gate.blocked()
    events.close()


@pytest.mark.asyncio
async def test_fresh_mass_vanish_with_empty_book_is_a_sweep(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    gate = QuotingGate()
    ex = StubExchange()
    workers = {}
    for i, t in enumerate(("A", "B", "C", "D")):
        workers[t], _ = _worker_with_order(events, risk, gate, ex, t, f"o{i}", age_s=10)
    report = await reconcile_pass(ex, workers, risk, events, gate,
                                  sweep_cooloff_seconds=900, ttl_seconds=900)
    assert report.sweep and gate.blocked()
    events.close()


class LateReplacementExchange(StubExchange):
    """A replacement bmm- order lands during the 2s settle window — i.e. it is
    visible only to the settle-and-confirm refetch, exactly like a TTL-refresh
    burst where creates queue behind the token bucket for a second or two."""

    def __init__(self, repl):
        super().__init__()
        self._repl = repl
        self._global_fetches = 0

    async def get_resting_orders(self, ticker=None):
        if ticker is None:
            self._global_fetches += 1
            if self._global_fetches >= 2 and self._repl.order_id not in self.resting:
                self.resting[self._repl.order_id] = self._repl
        return await super().get_resting_orders(ticker)


@pytest.mark.asyncio
async def test_replacements_landing_veto_the_sweep(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    gate = QuotingGate()
    repl = Order(order_id="new1", client_order_id="bmm-new1", ticker="A",
                 side=Side.BID, price=Decimal("0.40"), count=2)
    ex = LateReplacementExchange(repl)
    workers = {}
    for i, t in enumerate(("A", "B", "C", "D")):
        workers[t], _ = _worker_with_order(events, risk, gate, ex, t, f"o{i}", age_s=10)
    report = await reconcile_pass(ex, workers, risk, events, gate,
                                  sweep_cooloff_seconds=900, ttl_seconds=900)
    assert not report.sweep and not gate.blocked()
    events.close()


@pytest.mark.asyncio
async def test_foreign_order_is_logged_never_canceled(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    gate = QuotingGate()
    ex = StubExchange()
    manual = Order(order_id="manual1", client_order_id="", ticker="MKT",
                   side=Side.ASK, price=Decimal("0.90"), count=5)
    ex.resting["manual1"] = manual  # owner-placed via the Kalshi UI
    w = MarketWorker("MKT", ex, StrategyParams(), risk, events,
                     WorkerConfig(), dry_run=False, gate=gate)
    report = await reconcile_pass(ex, {"MKT": w}, risk, events, gate,
                                  sweep_cooloff_seconds=900, ttl_seconds=900)
    assert report.orphaned == [] and "manual1" not in ex.canceled
    n = events.db.execute(
        "SELECT COUNT(*) FROM events WHERE type='order_foreign'").fetchone()[0]
    assert n == 1
    events.close()
