"""Orchestrator-level tests (2026-07-17): task supervision (H3), fill dispatch
isolation + dedup (H4/H5), and the cross-session PnL chain wiring (FIX-PnL)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bacchus_mm.config import Config
from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import BookTop, Fill, MarketLifecycle, Order, Side
from bacchus_mm.main import (
    FillDispatcher,
    load_chained_risk,
    marks_tick,
    persist_pnl_marks,
    reap_closing_markets,
    require_order_group,
    settlement_poll,
    supervise,
)
from bacchus_mm.marketmaker import MarketWorker, WorkerConfig
from bacchus_mm.reconcile import managed_tickers
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams


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
    events.flush()  # 2026-07-17 (M1): events writes are batched now
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
    events.flush()  # 2026-07-17 (M1): drain the batch before asserting
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


def test_dispatcher_books_fee_into_risk_and_fills_row(tmp_path):
    # 2026-07-17 (M7): the adapter's fee rides the dispatcher into cash/PnL
    # and into the fills table for net-of-fee analytics.
    events, risk, worker, d = _dispatch_setup(tmp_path)
    f = _fill("t1")
    f.fee = Decimal("0.09")
    f.fee_source = "computed"
    d(f)
    st = risk.markets["MKT"]
    assert st.fees == Decimal("0.09")
    # cash -2.49 (5 @ 0.48 + fee), marked at the 0.50 default -> pnl +0.01.
    assert risk.pnl() == Decimal("0.01")
    row = events.db.execute("SELECT fee FROM fills WHERE trade_id='t1'").fetchone()
    assert row[0] == 0.09
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


def test_halt_clear_rebases_high_water(tmp_path):
    # 2026-07-17 halt-loop trap: cumulative drawdown persists across restarts,
    # so clearing the HALTED marker without rebasing the high-water mark meant
    # instant re-halt. halt-clear now anchors high_water at current cumulative
    # PnL — the operator accepts the loss and re-arms from there.
    from decimal import Decimal

    from bacchus_mm.config import Config
    from bacchus_mm.eventlog import EventLog
    from bacchus_mm.main import cmd_halt_clear, load_chained_risk
    from bacchus_mm.risk import RiskManager

    cfg = Config.load(tmp_path)  # defaults; data_dir = tmp_path/data
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    seed = EventLog(cfg.data_dir, "seed")
    seed.kv_set("cumulative_pnl", "-12.50")
    seed.kv_set("high_water", "0.75")
    seed.close()
    RiskManager(params=cfg.risk, state_dir=cfg.data_dir).halt("test halt")

    cmd_halt_clear(cfg)

    check = EventLog(cfg.data_dir, "check")
    assert check.kv_get("high_water") == "-12.50"  # rebased to cumulative pnl
    n = check.db.execute(
        "SELECT COUNT(*) FROM events WHERE type='halt_cleared'"
    ).fetchone()[0]
    assert n == 1
    risk = load_chained_risk(cfg.risk, cfg.data_dir, check)
    assert risk.check_halt_file() is None
    assert risk.drawdown() == Decimal("0")  # re-armed: no instant re-halt
    assert risk.should_halt() is None
    check.close()


def test_halt_clear_noop_without_marker(tmp_path, capsys):
    from bacchus_mm.config import Config
    from bacchus_mm.eventlog import EventLog
    from bacchus_mm.main import cmd_halt_clear

    cfg = Config.load(tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    seed = EventLog(cfg.data_dir, "seed")
    seed.kv_set("high_water", "0.75")
    seed.close()
    cmd_halt_clear(cfg)
    assert "no HALTED marker" in capsys.readouterr().out
    check = EventLog(cfg.data_dir, "check")
    assert check.kv_get("high_water") == "0.75"  # untouched without a halt
    check.close()


# ===================================== Pass 3b: M3, M5, H2


def _payloads(events: EventLog, type_: str) -> list[dict]:
    events.flush()
    return [
        json.loads(r[0])
        for r in events.db.execute(
            "SELECT payload FROM events WHERE type=?", (type_,)
        ).fetchall()
    ]


# ------------------------------------------------------ M3: periodic marks

def test_marks_tick_marks_without_book_deltas(tmp_path):
    """2026-07-17 (M3): a market whose book went silent still gets mid marks
    on the timer (today marks only happen inside on_book_top)."""
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", 3, Decimal("0.40"))
    risk.on_mid("MKT", Decimal("0.45"))
    marked = marks_tick({}, risk, events, 60.0, now=1000.0)
    assert marked == 1
    events.flush()
    row = events.db.execute("SELECT mid FROM mids WHERE ticker='MKT'").fetchone()
    assert row[0] == 0.45
    # ...and a pnl mark rides every tick
    n = events.db.execute("SELECT COUNT(*) FROM pnl_marks").fetchone()[0]
    assert n == 1
    events.close()


def test_marks_tick_skips_settled_and_fresh(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("OLD", 2, Decimal("0.40"))
    risk.on_settlement("OLD", Decimal(1))  # settled: keep the final mark only
    risk.on_mid("LIVE", Decimal("0.50"))
    fresh_worker = SimpleNamespace(evicted=False, _last_mid_mark=990.0, top=None)
    marked = marks_tick({"LIVE": fresh_worker}, risk, events, 60.0, now=1000.0)
    assert marked == 0  # settled skipped; live worker marked itself 10s ago
    events.flush()
    assert events.db.execute("SELECT COUNT(*) FROM mids").fetchone()[0] == 0
    # quiet worker (stale own mark) IS marked
    fresh_worker._last_mid_mark = 100.0
    assert marks_tick({"LIVE": fresh_worker}, risk, events, 60.0, now=1000.0) == 1
    events.close()


# ------------------------------------------------------- M3: the close reaper

class _ReaperExchange:
    def __init__(self):
        self.resting: dict[str, Order] = {}
        self.canceled: list[str] = []
        self._n = 0

    async def create_order(self, ticker, side, price, count, client_order_id,
                           expiration_seconds=None, post_only=True):
        self._n += 1
        o = Order(order_id=f"o{self._n}", client_order_id=client_order_id,
                  ticker=ticker, side=side, price=price, count=count)
        self.resting[o.order_id] = o
        return o

    async def cancel_order(self, order_id):
        self.canceled.append(order_id)
        self.resting.pop(order_id, None)


def _quoted_worker(tmp_path, events, risk, ex, ticker="MKT"):
    w = MarketWorker(ticker, exchange=ex, strategy=StrategyParams(), risk=risk,
                     events=events, cfg=WorkerConfig(), dry_run=False)
    w.top = BookTop(ticker, Decimal("0.48"), 10, Decimal("0.52"), 10, 0)
    risk.on_mid(ticker, Decimal("0.50"))
    return w


@pytest.mark.asyncio
async def test_reaper_pulls_quotes_below_threshold_and_never_requotes(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    ex = _ReaperExchange()
    w = _quoted_worker(tmp_path, events, risk, ex)
    await w._requote()
    assert len(ex.resting) == 2  # quoting normally
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    close_times = {"MKT": (now + timedelta(hours=11)).isoformat()}  # < 12h threshold
    reaped = reap_closing_markets({"MKT": w}, risk, events, close_times, 12.0, now=now)
    assert reaped == ["MKT"]
    assert w.close_reaped and not w.reduce_only  # flat: inert, not wind-down
    ev = _payloads(events, "close_reaper")[0]
    assert ev["hours_to_close"] == 11.0 and ev["position"] == 0
    await w._requote()
    assert len(ex.resting) == 0  # both sides pulled
    assert set(ex.canceled) == {"o1", "o2"}
    await w._requote()
    assert len(ex.resting) == 0  # never re-quoted
    events.close()


@pytest.mark.asyncio
async def test_reaper_leaves_markets_above_threshold_alone(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    ex = _ReaperExchange()
    w = _quoted_worker(tmp_path, events, risk, ex)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    close_times = {"MKT": (now + timedelta(hours=13)).isoformat()}
    assert reap_closing_markets({"MKT": w}, risk, events, close_times, 12.0, now=now) == []
    assert not w.close_reaped
    await w._requote()
    assert len(ex.resting) == 2
    events.close()


@pytest.mark.asyncio
async def test_reaper_with_position_routes_to_winddown(tmp_path):
    """A reaped market we HOLD converts to the wind-down machinery (exit-only
    quotes until flat) instead of abandoning the inventory."""
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", 3, Decimal("0.45"))
    ex = _ReaperExchange()
    w = _quoted_worker(tmp_path, events, risk, ex)
    await w._requote()
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    close_times = {"MKT": (now + timedelta(hours=2)).isoformat()}
    assert reap_closing_markets({"MKT": w}, risk, events, close_times, 12.0, now=now) == ["MKT"]
    assert w.reduce_only and w.close_reaped
    ev = _payloads(events, "close_reaper")[0]
    assert ev["position"] == 3 and ev["wind_down"] is True
    await w._requote()
    assert len(ex.resting) == 1  # exit side only (long: the ask)
    only = next(iter(ex.resting.values()))
    assert only.side is Side.ASK and only.count <= 3
    events.close()


# ------------------------------------------------- M3: settlement realization

class _SettleExchange:
    def __init__(self, status, result, close_time=""):
        self.lifecycle = MarketLifecycle(
            ticker="MKT", status=status, result=result, close_time=close_time
        )

    async def get_market_status(self, ticker):
        return self.lifecycle if ticker == "MKT" else None


@pytest.mark.asyncio
async def test_settlement_poll_realizes_and_emits(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", 5, Decimal("0.40"))
    risk.on_mid("MKT", Decimal("0.93"))  # stale mark right before settlement
    ex = _SettleExchange("finalized", "yes", "2026-07-17T16:00:00Z")
    close_times: dict[str, str] = {}
    realized = await settlement_poll(ex, risk, events, close_times)
    assert realized == ["MKT"]
    assert risk.markets["MKT"].position == 0
    assert risk.pnl() == Decimal("3.00")  # 5 x (1.00 - 0.40)
    ev = _payloads(events, "settlement_realized")[0]
    assert ev["position"] == 5 and ev["settlement"] == 1.0
    assert ev["basis"] == 0.4 and ev["pnl"] == 3.0
    assert close_times["MKT"] == "2026-07-17T16:00:00Z"  # reaper backfill
    # a second poll does nothing (settled markets are excluded)
    assert await settlement_poll(ex, risk, events, close_times) == []
    assert len(_payloads(events, "settlement_realized")) == 1
    events.close()


@pytest.mark.asyncio
async def test_settlement_poll_realizes_only_at_finalized(tmp_path):
    """2026-07-18 (round 2): realize ONLY at finalized. `determined` can still be
    disputed/amended, and on_settlement freezes the outcome permanently — so a
    determined market is left held+marking until it finalizes."""
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", -5, Decimal("0.40"))  # long NO at 0.60
    # determined does NOT realize yet
    assert await settlement_poll(_SettleExchange("determined", "no"), risk, events, {}) == []
    assert risk.markets["MKT"].position == -5 and not risk.markets["MKT"].settled
    # finalized realizes: long NO settles NO -> +$2.00, and a persisted marker.
    assert await settlement_poll(_SettleExchange("finalized", "no"), risk, events, {}) == ["MKT"]
    assert risk.pnl() == Decimal("2.00")
    assert "MKT" in events.settled_tickers()
    events.close()


@pytest.mark.asyncio
async def test_settlement_marker_survives_restart_no_double_realize(tmp_path):
    """The persisted settled marker must stop a restart from re-realizing a
    position the exchange still reports during the pre-payout window."""
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", -5, Decimal("0.40"))
    await settlement_poll(_SettleExchange("finalized", "no"), risk, events, {})
    realized_pnl = risk.pnl()
    events.close()
    # Restart: the marker is in kv; startup must skip re-seeding MKT.
    events2 = EventLog(tmp_path, "t2")
    assert "MKT" in events2.settled_tickers()
    events2.close()
    assert realized_pnl == Decimal("2.00")


@pytest.mark.asyncio
async def test_settlement_poll_ignores_open_and_closed(tmp_path):
    events = EventLog(tmp_path, "t")
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", 5, Decimal("0.40"))
    for status, result in (("active", ""), ("closed", ""), ("determined", "yes"),
                           ("disputed", ""), ("amended", "")):
        ex = _SettleExchange(status, result)
        assert await settlement_poll(ex, risk, events, {}) == []
    assert risk.markets["MKT"].position == 5  # still held, still marking
    events.close()


# ------------------------------------------------- M5: fail-closed order group

def _cfg(tmp_path, env="demo", allow=False) -> Config:
    cfg = Config.load(tmp_path)  # defaults; no config files
    cfg.env = env
    cfg.allow_no_order_group = allow
    return cfg


def test_order_group_failure_aborts_prod_live(tmp_path):
    events = EventLog(tmp_path, "t")
    with pytest.raises(SystemExit):
        require_order_group(_cfg(tmp_path, env="prod"), live=True, gid=None, events=events)
    assert _payloads(events, "startup_aborted")[0]["reason"] == "order_group_unavailable"
    events.close()


def test_order_group_override_warns_and_continues(tmp_path):
    events = EventLog(tmp_path, "t")
    require_order_group(_cfg(tmp_path, env="prod", allow=True), live=True, gid=None, events=events)
    assert not _payloads(events, "startup_aborted")
    events.close()


def test_order_group_failure_non_prod_continues(tmp_path):
    events = EventLog(tmp_path, "t")
    require_order_group(_cfg(tmp_path, env="demo"), live=False, gid=None, events=events)
    require_order_group(_cfg(tmp_path, env="prod"), live=True, gid="grp-1", events=events)
    events.close()


# ------------------------------------------------------- H2: managed tickers

def test_managed_tickers_union_and_empty(tmp_path):
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("HELD", 2, Decimal("0.50"))
    workers = {"QUOTED": SimpleNamespace(), "EVICTED": SimpleNamespace()}
    got = managed_tickers(workers, risk, selected=["NEW"])
    assert got == ["EVICTED", "HELD", "NEW", "QUOTED"]
    # Empty set cancels NOTHING (fail-safe direction — documented semantics).
    assert managed_tickers() == []
    assert managed_tickers({}, risk) == ["HELD"]
