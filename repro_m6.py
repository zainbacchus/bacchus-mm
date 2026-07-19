"""Repro: M6 ambiguous-create -> adopt-if-resting vs reconcile orphan-cancel race,
and C3 register/release consistency. Run inside the p1-audit worktree venv."""
import asyncio
import tempfile
from decimal import Decimal
from pathlib import Path

from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import BookTop, Order, Side
from bacchus_mm.marketmaker import MarketWorker, QuotingGate, WorkerConfig
from bacchus_mm.reconcile import reconcile_pass
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams


class StubExchange:
    def __init__(self):
        self.resting: dict[str, Order] = {}
        self.canceled: list[str] = []
        self.created: list[Order] = []
        self.create_attempts = 0
        self.cancel_all_calls = 0
        self.cancel_all_tickers = []
        self.fee_schedule = None
        self._n = 0
        # control: if set, next create raises this (ambiguous); the order still
        # "lands" on the exchange if land_on_fail is True (timeout-but-succeeded)
        self.fail_create: str | None = None
        self.fail_status: int | None = None
        self.land_on_fail = False

    async def create_order(self, ticker, side, price, count, client_order_id,
                           expiration_seconds=None, post_only=True):
        self.create_attempts += 1
        if self.fail_create:
            if self.land_on_fail:
                self._n += 1
                o = Order(order_id=f"o{self._n}", client_order_id=client_order_id,
                          ticker=ticker, side=side, price=price, count=count)
                self.resting[o.order_id] = o  # it really landed despite the "timeout"
                self.created.append(o)
            err = RuntimeError(self.fail_create)
            if self.fail_status is not None:
                err.status = self.fail_status
            raise err
        self._n += 1
        o = Order(order_id=f"o{self._n}", client_order_id=client_order_id,
                  ticker=ticker, side=side, price=price, count=count)
        self.resting[o.order_id] = o
        self.created.append(o)
        return o

    async def cancel_order(self, order_id):
        self.canceled.append(order_id)
        self.resting.pop(order_id, None)

    async def cancel_all_orders(self, tickers=None):
        self.cancel_all_calls += 1
        self.cancel_all_tickers.append(tickers)
        n = len(self.resting)
        self.resting.clear()
        return n

    async def get_resting_orders(self, ticker=None):
        return [o for o in self.resting.values() if ticker is None or o.ticker == ticker]


def mkworker(tmp, ex, risk, events, gate):
    w = MarketWorker("MKT", exchange=ex, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(create_adopt_delay=0.01),
                     dry_run=False, gate=gate)
    w.top = BookTop("MKT", Decimal("0.48"), 10, Decimal("0.52"), 10, 0)
    return w


async def scenario_reconcile_races_adopt():
    print("\n=== SCENARIO 1: reconcile pass runs DURING the M6 ambiguity window ===")
    tmp = Path(tempfile.mkdtemp())
    events = EventLog(tmp, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp)
    gate = QuotingGate()
    ex = StubExchange()
    w = mkworker(tmp, ex, risk, events, gate)
    workers = {"MKT": w}

    # create times out but the order actually LANDED on the exchange (bmm- tagged)
    ex.fail_create = "timeout"
    ex.land_on_fail = True
    await w._requote()
    print("after ambiguous create: bid_order=", w.bid_order,
          "pending=", dict(w._pending_create),
          "resting_reg=", dict(risk.resting),
          "exchange_resting=", list(ex.resting.keys()))
    landed_cid = ex.created[-1].client_order_id
    print("order that landed on exchange, client_order_id=", landed_cid,
          "bmm-prefixed?", landed_cid.startswith("bmm-"))

    # Now the GLOBAL reconcile loop fires while we're still inside the adopt delay.
    # The worker's bid_order is None, so the landed order looks like an ORPHAN.
    ex.fail_create = None  # reconcile does not create
    report = await reconcile_pass(ex, workers, risk, events, gate)
    print("reconcile report.orphaned=", report.orphaned)
    print("exchange_resting after reconcile=", list(ex.resting.keys()))
    print("canceled=", ex.canceled)

    # Now the worker finally runs its adopt cycle (past the delay).
    await asyncio.sleep(0.02)
    ex.fail_create = None
    ex.land_on_fail = False
    await w._requote()
    print("after adopt cycle: bid_order=", w.bid_order,
          "pending=", dict(w._pending_create),
          "resting_reg=", dict(risk.resting),
          "exchange_resting=", list(ex.resting.keys()))
    ev_types = {}
    events.flush()
    for (typ,) in events.db.execute("SELECT type FROM events").fetchall():
        ev_types[typ] = ev_types.get(typ, 0) + 1
    print("events:", ev_types)
    events.close()


async def scenario_adopt_lag_double_place():
    print("\n=== SCENARIO 2: adopt lookup MISSES a resting order (read-after-write lag) ===")
    tmp = Path(tempfile.mkdtemp())
    events = EventLog(tmp, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp)
    gate = QuotingGate()
    ex = StubExchange()
    w = mkworker(tmp, ex, risk, events, gate)

    ex.fail_create = "timeout"
    ex.land_on_fail = True
    await w._requote()
    landed = ex.created[-1]
    print("order landed:", landed.order_id, landed.client_order_id)

    # Simulate eventual-consistency: hide the landed order from get_resting_orders
    # for the adopt lookup (order is resting exchange-side but the /orders GET
    # replica hasn't caught up yet).
    hidden = ex.resting.pop(landed.order_id)
    await asyncio.sleep(0.02)
    ex.fail_create = None
    ex.land_on_fail = False
    await w._requote()  # adopt misses -> confirmed_lost -> places a SECOND order
    # replica catches up: the hidden order reappears
    ex.resting[hidden.order_id] = hidden
    print("bid_order (tracked)=", w.bid_order.order_id if w.bid_order else None)
    print("exchange_resting (should be ONE if no double-place):", list(ex.resting.keys()))
    print("resting_reg (C3):", dict(risk.resting))
    print(">>> DOUBLE PLACE" if len(ex.resting) > 1 else ">>> single order")
    events.close()


async def main():
    await scenario_reconcile_races_adopt()
    await scenario_adopt_lag_double_place()


asyncio.run(main())
