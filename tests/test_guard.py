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
    guard.update(Decimal("0.36"), ts=1020.0)  # 3c step: candidate, not yet confirmed
    assert not guard.blocked(ts=1021.0)
    guard.update(Decimal("0.355"), ts=1025.0)  # 2nd same-direction update -> trip
    assert guard.blocked(ts=1026.0)
    assert guard.blocked(ts=1025.0 + 179)
    assert not guard.blocked(ts=1025.0 + 181)


def test_retriggers_extend_cooloff():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.30"), ts=1005.0)  # 10c >= 2x threshold -> immediate trip
    guard.update(Decimal("0.20"), ts=1100.0)  # still moving -> blocked until 1280
    assert guard.blocked(ts=1200.0)
    assert not guard.blocked(ts=1281.0)


def test_old_history_expires():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.40"), ts=1040.0)  # first point aged out of window
    guard.update(Decimal("0.37"), ts=1041.0)  # 3c vs 0.40@1040 -> candidate
    assert not guard.blocked(ts=1042.0)
    guard.update(Decimal("0.365"), ts=1043.0)  # persists -> trip
    assert guard.blocked(ts=1044.0)


# 2026-07-17 (H6): the hair trigger fired 266x/12 evictions in 4 days, mostly
# one pulled level moving a wide book's mid (KXRAINNYCM 65+34 pulls).
def test_single_step_below_spread_scaled_threshold_does_not_trip():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0, spread=Decimal("0.08"))
    # 3c step, but effective threshold = max(3c, 0.75 x 8c) = 6c on a wide book.
    guard.update(Decimal("0.43"), ts=1001.0, spread=Decimal("0.08"))
    assert not guard.blocked(ts=1002.0)
    guard.update(Decimal("0.46"), ts=1003.0, spread=Decimal("0.08"))  # still < 6c steps
    assert not guard.blocked(ts=1004.0)


def test_persistent_move_confirmed_across_two_updates():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0, spread=Decimal("0.02"))
    guard.update(Decimal("0.43"), ts=1001.0, spread=Decimal("0.02"))  # 1x move: candidate
    assert not guard.blocked(ts=1002.0)
    guard.update(Decimal("0.435"), ts=1003.0, spread=Decimal("0.02"))  # persists: trip
    assert guard.blocked(ts=1004.0)


def test_single_big_jump_trips_immediately():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0, spread=Decimal("0.02"))
    guard.update(Decimal("0.46"), ts=1001.0, spread=Decimal("0.02"))  # 6c >= 2x3c
    assert guard.blocked(ts=1002.0)


def test_reversal_resets_pending_confirmation():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0, spread=Decimal("0.02"))
    guard.update(Decimal("0.43"), ts=1001.0, spread=Decimal("0.02"))  # candidate up
    guard.update(Decimal("0.41"), ts=1002.0, spread=Decimal("0.02"))  # reverted in-band
    guard.update(Decimal("0.44"), ts=1003.0, spread=Decimal("0.02"))  # new candidate (1 step)
    assert not guard.blocked(ts=1004.0)
    guard.update(Decimal("0.445"), ts=1005.0, spread=Decimal("0.02"))  # confirmed
    assert guard.blocked(ts=1006.0)


def _fake_finished_trip(w, seq: int, mid_at_trip: str = "0.40", eff: str = "0.03") -> None:
    """Simulate a guard trip whose cool-off has already ended (2026-07-17, H6:
    trips are scored for persistence at cool-off end, not at trip time)."""
    w.guard.trip_seq = seq
    w.guard.trip_mid = Decimal(mid_at_trip)
    w.guard.trip_eff = Decimal(eff)
    w.guard._blocked_until = 0.0


@pytest.mark.asyncio
async def test_worker_evicts_after_repeated_confirmed_trips(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    cfg = WorkerConfig(guard_evict_trips=3)
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=cfg, dry_run=True)
    w.top = BookTop("MKT", Decimal("0.40"), 10, Decimal("0.44"), 10, 0)  # mid 0.42: persisted
    for seq in range(1, 4):
        _fake_finished_trip(w, seq)
        await w._requote()
    assert w._guard_trips == 3
    assert w.evicted
    rows = events.db.execute("SELECT COUNT(*) FROM events WHERE type='market_evicted'").fetchone()
    assert rows[0] == 1
    # further requotes are inert once evicted
    await w._requote()
    events.close()


@pytest.mark.asyncio
async def test_guard_false_alarm_does_not_count_toward_eviction(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    cfg = WorkerConfig(guard_evict_trips=1)
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=cfg, dry_run=True)
    w.top = BookTop("MKT", Decimal("0.40"), 10, Decimal("0.42"), 10, 0)  # mid 0.41: reverted
    _fake_finished_trip(w, 1)  # |0.41 - 0.40| = 1c < 1.5c persistence bar
    await w._requote()
    assert w._guard_trips == 0 and not w.evicted
    n = events.db.execute("SELECT COUNT(*) FROM events WHERE type='guard_false_alarm'").fetchone()[0]
    assert n == 1
    import json
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='guard_trip' ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    assert json.loads(row[0])["confirmed"] is False
    events.close()


@pytest.mark.asyncio
async def test_confirmed_trip_counts_toward_eviction(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    cfg = WorkerConfig(guard_evict_trips=2)
    w = MarketWorker("MKT", exchange=None, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=cfg, dry_run=True)
    w.top = BookTop("MKT", Decimal("0.42"), 10, Decimal("0.46"), 10, 0)  # mid 0.44: persisted
    _fake_finished_trip(w, 1)  # |0.44 - 0.40| = 4c >= 1.5c bar
    await w._requote()
    assert w._guard_trips == 1 and not w.evicted
    import json
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='guard_trip' ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    assert json.loads(row[0])["confirmed"] is True
    _fake_finished_trip(w, 2)
    await w._requote()
    assert w._guard_trips == 2 and w.evicted
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
    w.top = BookTop("MKT", Decimal("0.30"), 10, Decimal("0.34"), 10, 0)  # mid 0.32
    risk.on_mid("MKT", Decimal("0.32"))
    # Confirmed trip (mid 0.30 -> 0.32 persisted past the cool-off) -> evict.
    _fake_finished_trip(w, 1, mid_at_trip="0.30")
    await w._requote()
    assert w.evicted and w.reduce_only and not w.reduce_only_origin
    import json
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='quote_decision' ORDER BY ts_ms DESC LIMIT 1"
    ).fetchone()
    d = json.loads(row[0])
    assert d["bid"] is None and d["ask"] is not None  # long: exit ask only, even in cooloff
    events.close()
