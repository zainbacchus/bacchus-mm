import time as _t
from decimal import Decimal

import pytest

from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import BookTop
from bacchus_mm.marketmaker import FastMoveGuard, MarketWorker, WorkerConfig
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams


def g():
    return FastMoveGuard(Decimal("0.03"), window_s=30, cooloff_s=180)


def test_slow_drift_does_not_trip():
    guard = g()
    # 10c of drift, but spread over 10 minutes — never >=3c inside any 30s window
    for i in range(100):
        guard.update(Decimal("0.40") + Decimal("0.001") * i, ts=1000.0 + i * 6)
    assert not guard.blocked(ts=1600.0)


def test_fast_move_trips_and_cools_off():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.39"), ts=1010.0)
    guard.update(Decimal("0.36"), ts=1020.0)  # 4c in 20s -> trip
    assert guard.blocked(ts=1021.0)
    assert guard.blocked(ts=1020.0 + 179)
    assert not guard.blocked(ts=1020.0 + 181)


def test_retriggers_extend_cooloff():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.30"), ts=1005.0)  # trip -> blocked until 1185
    guard.update(Decimal("0.20"), ts=1100.0)  # still moving -> blocked until 1280
    assert guard.blocked(ts=1200.0)
    assert not guard.blocked(ts=1281.0)


def test_old_history_expires():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.40"), ts=1040.0)  # first point aged out of window
    guard.update(Decimal("0.37"), ts=1041.0)  # 3c vs 0.40@1040 -> trip
    assert guard.blocked(ts=1042.0)


@pytest.mark.asyncio
async def test_worker_evicts_after_repeated_trips(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    cfg = WorkerConfig(guard_evict_trips=3)
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=cfg, dry_run=True)
    w.top = BookTop("MKT", Decimal("0.40"), 10, Decimal("0.44"), 10, 0)
    for _ in range(3):
        w.guard._blocked_until = _t.monotonic() + 60
        w._guard_announced = False
        await w._requote()
    assert w.evicted
    rows = events.db.execute("SELECT COUNT(*) FROM events WHERE type='market_evicted'").fetchone()
    assert rows[0] == 1
    # further requotes are inert once evicted
    await w._requote()
    events.close()


@pytest.mark.asyncio
async def test_reduce_only_worker_quotes_exit_side_only(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", -2, Decimal("0.38"))  # short 2
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(), dry_run=True, reduce_only=True)
    w.top = BookTop("MKT", Decimal("0.60"), 10, Decimal("0.64"), 10, 0)
    risk.on_mid("MKT", Decimal("0.62"))
    await w._requote()
    import json
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='quote_decision' ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    d = json.loads(row[0])
    assert d["ask"] is None and d["ask_size"] == 0      # short: never sell more
    assert d["bid"] is not None and d["bid_size"] <= 2  # exit bid, capped at position
    events.close()


@pytest.mark.asyncio
async def test_reduce_only_worker_goes_inert_when_flat(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(), dry_run=True, reduce_only=True)
    w.top = BookTop("MKT", Decimal("0.40"), 10, Decimal("0.44"), 10, 0)
    await w._requote()
    assert w._wound_down and w.evicted
    n = events.db.execute("SELECT COUNT(*) FROM events WHERE type='wind_down_complete'").fetchone()[0]
    assert n == 1
    events.close()


@pytest.mark.asyncio
async def test_evicted_with_position_becomes_wind_down(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", 3, Decimal("0.30"))  # long 3
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(guard_evict_trips=1), dry_run=True)
    w.top = BookTop("MKT", Decimal("0.30"), 10, Decimal("0.34"), 10, 0)
    risk.on_mid("MKT", Decimal("0.32"))
    w.guard._blocked_until = _t.monotonic() + 60
    await w._requote()
    assert w.evicted and w.reduce_only and not w.reduce_only_origin
    import json
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='quote_decision' ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    d = json.loads(row[0])
    assert d["bid"] is None and d["ask"] is not None  # long: exit ask only, even in cooloff
    events.close()
