"""Orchestrator-level tests (2026-07-17): task supervision (H3), fill dispatch
isolation + dedup (H4/H5), and the cross-session PnL chain wiring (FIX-PnL)."""

import asyncio
import json
from decimal import Decimal

import pytest

from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import Fill
from bacchus_mm.main import (
    FillDispatcher,
    load_chained_risk,
    persist_pnl_marks,
    supervise,
)
from bacchus_mm.risk import RiskManager, RiskParams


def _fill(trade_id="t1", order_id="o1", ticker="MKT", count=5):
    return Fill(
        trade_id=trade_id, order_id=order_id, ticker=ticker,
        signed_count=count, yes_price=Decimal("0.48"), is_taker=False, ts_ms=1,
    )


class _WorkerSpy:
    def __init__(self):
        self.filled: list[tuple[str, int]] = []

    def current_mid(self):
        return Decimal("0.50")

    def order_filled(self, order_id, count):
        self.filled.append((order_id, count))


def _count(events: EventLog, type_: str) -> int:
    return events.db.execute(
        "SELECT COUNT(*) FROM events WHERE type=?", (type_,)
    ).fetchone()[0]


# --------------------------------------------------------- H3: task supervision

@pytest.mark.asyncio
async def test_supervise_exception_sets_stop_and_emits(tmp_path):
    events = EventLog(tmp_path, "t")
    stop = asyncio.Event()

    async def boom():
        raise RuntimeError("kaboom")

    supervise(asyncio.create_task(boom()), "boom", stop, events)
    await asyncio.sleep(0.05)
    assert stop.is_set()
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='task_died'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["task"] == "boom"
    assert "kaboom" in payload["error"]
    assert "Traceback" in payload["traceback"]
    events.close()


@pytest.mark.asyncio
async def test_supervise_cancel_does_not_trip(tmp_path):
    events = EventLog(tmp_path, "t")
    stop = asyncio.Event()

    async def sleeper():
        await asyncio.sleep(60)

    t = supervise(asyncio.create_task(sleeper()), "sleeper", stop, events)
    await asyncio.sleep(0)  # let it start
    t.cancel()
    await asyncio.sleep(0.05)
    assert not stop.is_set()
    assert _count(events, "task_died") == 0
    events.close()


@pytest.mark.asyncio
async def test_supervise_clean_return_does_not_trip(tmp_path):
    events = EventLog(tmp_path, "t")
    stop = asyncio.Event()

    async def done():
        return 42

    supervise(asyncio.create_task(done()), "done", stop, events)
    await asyncio.sleep(0.05)
    assert not stop.is_set()
    assert _count(events, "task_died") == 0
    events.close()


@pytest.mark.asyncio
async def test_supervise_after_stop_set_only_logs(tmp_path):
    """Failures racing an in-progress shutdown are teardown noise: logged via
    stdlib, but no spurious task_died incident."""
    events = EventLog(tmp_path, "t")
    stop = asyncio.Event()
    stop.set()  # shutdown already underway

    async def boom():
        raise RuntimeError("teardown race")

    supervise(asyncio.create_task(boom()), "late", stop, events)
    await asyncio.sleep(0.05)
    assert _count(events, "task_died") == 0
    events.close()


# ------------------------------------------------- H4/H5: fill dispatch + dedup

def _dispatch_setup(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    worker = _WorkerSpy()
    dispatcher = FillDispatcher({"MKT": worker}, risk, events)
    return events, risk, worker, dispatcher


def test_duplicate_fill_ignored_exactly_once(tmp_path):
    events, risk, worker, d = _dispatch_setup(tmp_path)
    d(_fill("t1"))
    d(_fill("t1"))  # redelivered after a ws resubscribe
    assert risk.markets["MKT"].position == 5  # counted exactly once
    assert worker.filled == [("o1", 5)]
    assert _count(events, "fill_duplicate_ignored") == 1
    assert events.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    events.close()


def test_missing_trade_id_processed_normally(tmp_path):
    # 2026-07-17 (H5): never drop a fill silently — an absent id can't dedup,
    # so every delivery is processed (and counted).
    events, risk, worker, d = _dispatch_setup(tmp_path)
    d(_fill(""))
    d(_fill(""))
    assert risk.markets["MKT"].position == 10
    assert len(worker.filled) == 2
    assert _count(events, "fill_duplicate_ignored") == 0
    events.close()


def test_seen_set_seeded_from_db(tmp_path):
    events = EventLog(tmp_path, "t")
    events.record_fill("MKT", "old1", "o9", 3, Decimal("0.50"), False, None, 1)
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    worker = _WorkerSpy()
    d = FillDispatcher({"MKT": worker}, risk, events)
    d(_fill("old1", order_id="o9"))  # already on the books from a prior session
    assert "MKT" not in risk.markets  # ignored: risk.on_fill never ran
    assert worker.filled == []
    d(_fill("new1"))
    assert risk.markets["MKT"].position == 5
    events.close()


def test_record_fill_failure_still_updates_worker(tmp_path):
    # 2026-07-17 (H4): PnL applied, worker bookkeeping intact, exception
    # contained — a broken DB must not desync order state or reach the stream.
    events, risk, worker, d = _dispatch_setup(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    events.record_fill = boom
    d(_fill("t1"))  # must not raise
    assert risk.markets["MKT"].position == 5
    assert worker.filled == [("o1", 5)]
    assert _count(events, "fill_record_failed") == 1
    d(_fill("t1"))  # the seen-set recorded it even though the row was lost
    assert risk.markets["MKT"].position == 5
    events.close()


# ---------------------------------------------------------- FIX-PnL: kv wiring

def test_load_chained_risk_reads_kv(tmp_path):
    events = EventLog(tmp_path, "t")
    events.kv_set("cumulative_pnl", "-3.25")
    events.kv_set("high_water", "-1.00")
    risk = load_chained_risk(RiskParams(), tmp_path, events)
    assert risk.cumulative_offset == Decimal("-3.25")
    assert risk.high_water == Decimal("-1.00")
    events.close()


def test_load_chained_risk_first_run_defaults(tmp_path):
    # First run on a pre-upgrade DB: no kv rows -> offset 0, high-water anchors
    # at 0 (pre-upgrade losses intentionally not counted — Pass-1 design).
    events = EventLog(tmp_path, "t")
    risk = load_chained_risk(RiskParams(), tmp_path, events)
    assert risk.cumulative_offset == Decimal("0")
    assert risk.high_water == Decimal("0")
    events.close()


def test_persist_pnl_marks_round_trip_and_flag_reset(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", 10, Decimal("0.50"))
    risk.on_mid("MKT", Decimal("0.60"))
    risk.drawdown()  # new account high-water -> flag set
    assert risk.new_high_water_since_load
    persist_pnl_marks(events, risk)
    assert not risk.new_high_water_since_load
    assert events.kv_get("cumulative_pnl") == "1.00"
    assert events.kv_get("high_water") == "1.00"
    # ...and the next session's startup load sees the same chain
    reloaded = load_chained_risk(RiskParams(), tmp_path, events)
    assert reloaded.cumulative_pnl == Decimal("1.00")
    events.close()
