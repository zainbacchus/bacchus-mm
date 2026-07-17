"""Reconcile-loop tests (2026-07-17, C1): vanished orders, orphans, sweeps,
and pause-rejection suspension — all against a stub exchange adapter."""

import asyncio
import json
from decimal import Decimal

import pytest

from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import BookTop, Order, Side
from bacchus_mm.marketmaker import MarketWorker, QuotingGate, WorkerConfig
from bacchus_mm.reconcile import reconcile_pass
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams


class StubExchange:
    """Adapter double with an exchange-side view of the resting book."""

    def __init__(self):
        self.resting: dict[str, Order] = {}
        self.canceled: list[str] = []
        self.create_attempts = 0
        self.cancel_all_calls = 0
        self.cancel_all_tickers: list[list[str] | None] = []
        self.created: list[Order] = []
        self.fail_create: str | None = None
        self._n = 0

    async def create_order(self, ticker, side, price, count, client_order_id,
                           expiration_seconds=None, post_only=True):
        self.create_attempts += 1
        if self.fail_create:
            raise RuntimeError(self.fail_create)
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


def _setup(tmp_path, tickers=("MKT",)):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    gate = QuotingGate()
    ex = StubExchange()
    workers = {}
    for t in tickers:
        w = MarketWorker(t, exchange=ex, strategy=StrategyParams(), risk=risk,
                         events=events, cfg=WorkerConfig(), dry_run=False, gate=gate)
        w.top = BookTop(t, Decimal("0.48"), 10, Decimal("0.52"), 10, 0)  # mid 0.50
        workers[t] = w
    return events, risk, gate, ex, workers


def _events_of(events: EventLog, type_: str) -> list[dict]:
    events.flush()  # 2026-07-17 (M1): events writes are batched now
    rows = events.db.execute(
        "SELECT ticker, payload FROM events WHERE type=?", (type_,)
    ).fetchall()
    return [{"_ticker": t, **json.loads(p)} for t, p in rows]


@pytest.mark.asyncio
async def test_vanished_order_released_and_requote_enabled(tmp_path):
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    await w._requote()
    bid_id = w.bid_order.order_id
    assert risk.resting[("MKT", "bid")] == 5
    # Exchange-side cancel (maintenance pause / group action) — invisible on the ws.
    ex.resting.pop(bid_id)
    report = await reconcile_pass(ex, workers, risk, events, gate)
    assert ("MKT", "bid", bid_id) in report.vanished
    assert not report.sweep  # one side of one market is churn, not a sweep
    assert w.bid_order is None  # ref cleared so the worker re-quotes
    assert risk.resting[("MKT", "bid")] == 0  # C3 exposure released
    assert _events_of(events, "order_vanished")[0]["order_id"] == bid_id
    await w._requote()
    assert w.bid_order is not None and w.bid_order.order_id != bid_id
    events.close()


@pytest.mark.asyncio
async def test_orphan_order_cancelled_and_event(tmp_path):
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    await w._requote()
    # Resting on the exchange but tracked by no worker: worse than nothing —
    # outside caps, the kill switch's worldview, and TTL refresh. Cancel it.
    # Round 2: only bot-tagged (bmm-) orders are orphan-cancelable — an
    # untagged order is treated as owner-placed and left alone.
    ex.resting["ghost"] = Order(order_id="ghost", client_order_id="bmm-x", ticker="MKT",
                                side=Side.BID, price=Decimal("0.40"), count=3)
    report = await reconcile_pass(ex, workers, risk, events, gate)
    assert report.orphaned == ["ghost"]
    assert "ghost" in ex.canceled
    ev = _events_of(events, "order_orphaned")[0]
    assert ev["order_id"] == "ghost" and ev["_ticker"] == "MKT"
    events.close()


@pytest.mark.asyncio
async def test_stale_fetch_straddling_replace_is_not_vanished(tmp_path):
    """The first fetch raced a worker's own cancel/replace: the per-ticker
    confirm fetch sees the order resting and nothing is released or cleared."""
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    await w._requote()
    bid_id = w.bid_order.order_id
    real_get = ex.get_resting_orders
    calls = 0

    async def straddle(ticker=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return []  # stale global view: everything looks gone
        return await real_get(ticker)

    ex.get_resting_orders = straddle
    report = await reconcile_pass(ex, workers, risk, events, gate)
    assert report.vanished == [] and not report.sweep
    assert w.bid_order is not None and w.bid_order.order_id == bid_id
    assert risk.resting[("MKT", "bid")] == 5
    events.close()


@pytest.mark.asyncio
async def test_ref_replaced_mid_pass_is_left_alone(tmp_path):
    """The worker finished its own replace while we were confirming: the ref
    moved, so the pass must not release the replacement's exposure."""
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    await w._requote()
    old = w.bid_order
    real_get = ex.get_resting_orders
    calls = 0

    async def replace_mid_pass(ticker=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return []  # old order looks gone
        # Worker replaced it while we fetched: cancel old, place + register new.
        ex.resting.pop(old.order_id, None)
        risk.release_order("MKT", old.side, old.count)
        new = await ex.create_order("MKT", Side.BID, Decimal("0.47"), 5, "c-new")
        risk.register_order("MKT", Side.BID, 5)
        w.bid_order = new
        return await real_get(ticker)

    ex.get_resting_orders = replace_mid_pass
    report = await reconcile_pass(ex, workers, risk, events, gate)
    assert report.vanished == []
    assert w.bid_order is not None and w.bid_order.order_id != old.order_id
    assert risk.resting[("MKT", "bid")] == 5  # replacement's exposure intact
    events.close()


@pytest.mark.asyncio
async def test_sweep_engages_cooloff_cancel_all_and_event(tmp_path):
    events, risk, gate, ex, workers = _setup(tmp_path, ("M1", "M2"))
    for w in workers.values():
        await w._requote()
    ex.resting.clear()  # order-group trip: Kalshi cancels EVERYTHING
    report = await reconcile_pass(ex, workers, risk, events, gate, sweep_cooloff_seconds=900)
    assert report.sweep
    assert {t for t, _, _ in report.vanished} == {"M1", "M2"}
    assert gate.blocked()
    assert ex.cancel_all_calls == 1  # cheap no-op safety net
    ev = _events_of(events, "exchange_sweep_detected")[0]
    assert ev["suspected"] == "order_group_trip"  # no pause rejections seen
    assert sorted(ev["vanished_tickers"]) == ["M1", "M2"]
    assert ev["cooloff_seconds"] == 900
    events.close()


@pytest.mark.asyncio
async def test_sweep_attributed_to_maintenance_after_pause_rejections(tmp_path):
    events, risk, gate, ex, workers = _setup(tmp_path, ("M1", "M2"))
    for w in workers.values():
        await w._requote()
    gate.note_pause_rejection()  # a worker hit trading_is_paused earlier
    ex.resting.clear()
    report = await reconcile_pass(ex, workers, risk, events, gate)
    assert report.sweep
    ev = _events_of(events, "exchange_sweep_detected")[0]
    assert ev["suspected"] == "maintenance_pause"
    assert ev["pause_rejections_15m"] == 1
    events.close()


@pytest.mark.asyncio
async def test_single_market_vanish_is_not_a_sweep(tmp_path):
    """One market's orders expiring (exchange TTL while its worker was
    guard-blocked, or a single-market pause) is normal churn: release +
    re-quote, never a global cooloff."""
    events, risk, gate, ex, workers = _setup(tmp_path, ("M1", "M2"))
    for w in workers.values():
        await w._requote()
    for oid in [o.order_id for o in ex.resting.values() if o.ticker == "M1"]:
        ex.resting.pop(oid)
    report = await reconcile_pass(ex, workers, risk, events, gate)
    assert not report.sweep
    assert not gate.blocked()
    assert {t for t, _, _ in report.vanished} == {"M1"}
    assert workers["M2"].bid_order is not None  # untouched
    events.close()


@pytest.mark.asyncio
async def test_cooloff_blocks_workers_and_rearms_automatically(tmp_path):
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    await w._requote()
    n0 = ex.create_attempts
    gate.engage_cooloff(0.05)
    await w._requote()
    assert ex.create_attempts == n0  # nothing placed during the cooloff
    assert w.bid_order is None and w.ask_order is None  # everything pulled
    await asyncio.sleep(0.06)  # expiry re-arms automatically — no HALTED, no operator
    await w._requote()
    assert ex.create_attempts > n0
    events.close()


@pytest.mark.asyncio
async def test_pause_rejection_suspends_until_reconcile_rearm(tmp_path):
    """First trading_is_paused rejection suspends the market (was: 657
    rejections in 4 days); the reconcile pass grants one fresh probe per pass
    and the rejection re-suspends if the market is still paused."""
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    ex.fail_create = "trading_is_paused: market is paused"
    await w._requote()
    assert w.pause_suspected
    assert ex.create_attempts == 1  # bid rejected; ask side skipped post-suspend
    ev = _events_of(events, "quoting_suspended")
    assert len(ev) == 1 and ev[0]["reason"] == "trading_is_paused"
    await w._requote()
    assert ex.create_attempts == 1  # suspended: no hammering
    await reconcile_pass(ex, workers, risk, events, gate)
    assert not w.pause_suspected
    assert len(_events_of(events, "quoting_resumed")) == 1
    await w._requote()
    assert ex.create_attempts == 2  # one fresh probe; still paused -> re-suspends
    assert w.pause_suspected
    events.close()


def test_gate_cooloff_expiry_with_explicit_clock():
    gate = QuotingGate()
    gate.engage_cooloff(900, ts=1000.0)
    assert gate.blocked(ts=1000.1)
    assert gate.blocked(ts=1899.9)
    assert not gate.blocked(ts=1900.0)
    gate.note_pause_rejection(ts=1000.0)
    gate.note_pause_rejection(ts=1500.0)
    assert gate.pause_rejections_recent(900, ts=1901.0) == 1
    assert gate.pause_rejections_recent(900, ts=2401.0) == 0


# ----------------------------------------- H2: sweep safety net is scoped

@pytest.mark.asyncio
async def test_sweep_cancel_all_scoped_to_managed_tickers(tmp_path):
    """2026-07-17 (H2): the post-sweep cancel-all safety net passes the
    managed ticker set (workers + held positions), not the whole account —
    a second strategy sharing the account keeps its orders."""
    events, risk, gate, ex, workers = _setup(tmp_path, ("M1", "M2"))
    for w in workers.values():
        await w._requote()
    ex.resting.clear()
    report = await reconcile_pass(ex, workers, risk, events, gate)
    assert report.sweep
    assert ex.cancel_all_calls == 1
    assert ex.cancel_all_tickers[0] == ["M1", "M2"]
    events.close()


# ----------------------------- M6: ambiguous create -> client_order_id reconcile

@pytest.mark.asyncio
async def test_ambiguous_create_adopted_on_next_cycle(tmp_path):
    """2026-07-17 (M6): the create POST died ambiguously (timeout/5xx) but the
    order DID land exchange-side. Instead of re-placing (a double-place), the
    worker finds it by client_order_id after create_adopt_delay and adopts
    it: exposure registered, order tracked, no new create."""
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    w.cfg = WorkerConfig(create_adopt_delay=0.01)
    landed: list[Order] = []
    failed = False

    async def flaky_create(ticker, side, price, count, client_order_id,
                           expiration_seconds=None, post_only=True):
        nonlocal failed
        if not failed:
            failed = True
            # Exchange-side success, caller-visible failure (the response died).
            o = await StubExchange.create_order(
                ex, ticker, side, price, count, client_order_id,
                expiration_seconds=expiration_seconds, post_only=post_only,
            )
            landed.append(o)
            raise TimeoutError("response lost")
        return await StubExchange.create_order(
            ex, ticker, side, price, count, client_order_id,
            expiration_seconds=expiration_seconds, post_only=post_only,
        )

    ex.create_order = flaky_create
    await w._requote()
    assert w.bid_order is None  # caller saw a failure
    assert len(landed) == 1  # ...but the bid order is live exchange-side
    assert _events_of(events, "order_placement_unknown")
    await w._requote()  # inside the adopt delay: no new create attempted
    assert len(landed) == 1
    await asyncio.sleep(0.02)
    await w._requote()
    assert len(landed) == 1  # adopted, NOT re-placed
    assert w.bid_order is not None and w.bid_order.order_id == landed[0].order_id
    assert risk.resting[("MKT", "bid")] == landed[0].count
    assert _events_of(events, "order_adopted")
    events.close()


@pytest.mark.asyncio
async def test_ambiguous_create_confirmed_lost_replaces(tmp_path):
    """2026-07-17 (M6): the create genuinely never landed — after the adopt
    delay the worker confirms it by client_order_id and places fresh (exactly
    one live order, never two)."""
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    w.cfg = WorkerConfig(create_adopt_delay=0.01)
    real_create = ex.create_order
    calls = 0

    async def dead_create(ticker, side, price, count, client_order_id,
                          expiration_seconds=None, post_only=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("never reached the exchange")
        return await real_create(ticker, side, price, count, client_order_id,
                                 expiration_seconds=expiration_seconds,
                                 post_only=post_only)

    ex.create_order = dead_create
    await w._requote()  # bid fails ambiguously; ask places normally
    assert w.bid_order is None
    assert calls == 2
    await asyncio.sleep(0.02)
    await w._requote()  # bid confirmed lost -> one fresh placement; ask kept
    assert calls == 3
    assert w.bid_order is not None
    assert len(ex.resting) == 2  # bid + ask, no double-place
    assert _events_of(events, "order_placement_confirmed_lost")
    events.close()


@pytest.mark.asyncio
async def test_definitive_4xx_rejection_does_not_park(tmp_path):
    """A 4xx is a definitive rejection (the order is NOT live): plain
    order_rejected, no pending-adopt bookkeeping."""
    events, risk, gate, ex, workers = _setup(tmp_path)
    w = workers["MKT"]
    from bacchus_mm.exchange.kalshi import KalshiApiError

    async def reject(ticker, side, price, count, client_order_id,
                     expiration_seconds=None, post_only=True):
        raise KalshiApiError(400, "invalid price")

    ex.create_order = reject
    await w._requote()
    assert w.bid_order is None
    assert not w._pending_create
    assert _events_of(events, "order_rejected")
    assert not _events_of(events, "order_placement_unknown")
    events.close()
