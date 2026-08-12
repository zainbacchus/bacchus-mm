"""M7 side tripwire (2026-08-12): the per-side rolling-expectancy lever.

Covers the pure SideTripwire core (trip/no-trip boundaries, window pruning,
marks, cooloff/resume, disable), the config parsing, and the MarketWorker
integration (a suppressed side cancels and stays unquoted while the healthy
side keeps working). Evidence for the design lives in
research/ATTRIBUTION-DRIFT-2026-08-12.md.
"""

from decimal import Decimal as D

import pytest

from bacchus_mm.eventlog import EventLog
from bacchus_mm.fifteen import FifteenParams, SideTripwire
from bacchus_mm.marketmaker import BookTop, MarketWorker, WorkerConfig
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams


def _wire(loss=8.0, min_ct=100.0, window=7200.0, cooloff=10800.0):
    return SideTripwire(loss, min_ct, window, cooloff)


def _feed(tw, side, n, entry, ticker="KXBTC15M-T", t0=0.0):
    sc = 1.0 if side == "buy" else -1.0
    for i in range(n):
        tw.on_fill(t0 + i, ticker, sc, entry)


# ----------------------------------------------------------------- core trips

def test_trips_losing_sell_side_only():
    tw = _wire()
    # 120 sells at 0.50; mark rallies to 0.60 -> sells -10c/ct, buys absent.
    _feed(tw, "sell", 120, 0.50)
    tw.mark("KXBTC15M-T", 0.60)
    v = tw.evaluate(now=1000.0)
    assert v["tripped"] == [("sell", -10.0, 120.0)]
    assert tw.blocked("sell", 1000.0) and not tw.blocked("buy", 1000.0)


def test_no_trip_below_min_contracts():
    tw = _wire(min_ct=100.0)
    _feed(tw, "sell", 99, 0.50)
    tw.mark("KXBTC15M-T", 0.70)  # -20c/ct but on 99 contracts
    assert tw.evaluate(1000.0)["tripped"] == []


def test_no_trip_above_loss_threshold():
    tw = _wire(loss=8.0)
    _feed(tw, "sell", 200, 0.50)
    tw.mark("KXBTC15M-T", 0.57)  # -7c/ct: under the 8c bar
    assert tw.evaluate(1000.0)["tripped"] == []
    tw.mark("KXBTC15M-T", 0.58)  # -8c/ct: at the bar (<= trips)
    assert tw.evaluate(1001.0)["tripped"] == [("sell", -8.0, 200.0)]


def test_healthy_buys_do_not_trip_alongside_losing_sells():
    tw = _wire()
    _feed(tw, "sell", 150, 0.50, ticker="KXETH15M-A")
    _feed(tw, "buy", 150, 0.50, ticker="KXGOLD15M-B")
    tw.mark("KXETH15M-A", 0.62)   # sells -12c/ct
    tw.mark("KXGOLD15M-B", 0.62)  # buys +12c/ct
    v = tw.evaluate(1000.0)
    assert [t[0] for t in v["tripped"]] == ["sell"]
    assert not tw.blocked("buy", 1000.0)


def test_unmarked_ticker_values_at_entry():
    tw = _wire()
    _feed(tw, "sell", 500, 0.50)  # no mark ever arrives
    assert tw.evaluate(1000.0)["tripped"] == []  # pnl 0 until first mark


def test_zero_count_artifact_fills_ignored():
    tw = _wire(min_ct=1.0)
    tw.on_fill(0.0, "KXBTC15M-T", 0.0, 0.50)
    tw.mark("KXBTC15M-T", 0.99)
    assert tw.evaluate(10.0)["tripped"] == []


# -------------------------------------------------------- window and cooloff

def test_old_fills_age_out_of_window():
    tw = _wire(window=100.0)
    _feed(tw, "sell", 200, 0.50, t0=0.0)
    tw.mark("KXBTC15M-T", 0.70)
    # Evaluated long after the fills left the window: nothing to judge.
    assert tw.evaluate(now=500.0)["tripped"] == []


def test_cooloff_blocks_then_resumes_then_can_retrip():
    tw = _wire(cooloff=100.0)
    _feed(tw, "sell", 120, 0.50)
    tw.mark("KXBTC15M-T", 0.60)
    assert tw.evaluate(10.0)["tripped"]
    # During cooloff: blocked, no double-trip.
    assert tw.blocked("sell", 50.0)
    assert tw.evaluate(50.0)["tripped"] == []
    # Expiry: resume announced exactly once, side unblocked.
    v = tw.evaluate(111.0)
    assert v["resumed"] == ["sell"] and not tw.blocked("sell", 111.0)
    assert tw.evaluate(112.0)["resumed"] == []
    # Fresh post-resume bleed trips again (old fills were cleared at trip).
    _feed(tw, "sell", 120, 0.50, t0=112.0)
    tw.mark("KXBTC15M-T", 0.60)
    assert tw.evaluate(115.0)["tripped"] == [("sell", -10.0, 120.0)]


def test_trip_clears_only_that_sides_window():
    tw = _wire()
    _feed(tw, "sell", 120, 0.50, ticker="KXETH15M-A")
    _feed(tw, "buy", 50, 0.50, ticker="KXGOLD15M-B")
    tw.mark("KXETH15M-A", 0.60)
    tw.mark("KXGOLD15M-B", 0.50)
    assert tw.evaluate(10.0)["tripped"]
    # Buy fills survived the sell-side clear.
    assert sum(1 for f in tw._fills if f[1] == "buy") == 50


def test_disabled_never_trips():
    tw = _wire(loss=0.0)
    _feed(tw, "sell", 500, 0.50)
    tw.mark("KXBTC15M-T", 0.90)
    assert tw.evaluate(10.0)["tripped"] == []
    assert not tw.blocked("sell", 10.0)


def test_mark_freeze_keeps_valuation_after_book_dies():
    tw = _wire()
    _feed(tw, "sell", 120, 0.50)
    tw.mark("KXBTC15M-T", 0.60)  # last mark before the window closed
    # No further marks (book gone); the frozen mark still values the fills.
    assert tw.evaluate(50.0)["tripped"] == [("sell", -10.0, 120.0)]


# ------------------------------------------------------------------- config

def test_params_parse_and_defaults():
    p = FifteenParams.from_config({"fifteen": {
        "side_trip_loss_cct": 5,
        "side_trip_min_contracts": 40,
        "side_trip_window_hours": 1.5,
        "side_trip_cooloff_hours": 2,
    }})
    assert p.side_trip_loss_cct == 5.0
    assert p.side_trip_min_contracts == 40.0
    assert p.side_trip_window_hours == 1.5
    assert p.side_trip_cooloff_hours == 2.0
    d = FifteenParams.from_config({})
    assert d.side_trip_loss_cct == 8.0
    assert d.side_trip_min_contracts == 100.0
    assert d.side_trip_window_hours == 2.0
    assert d.side_trip_cooloff_hours == 3.0


# ------------------------------------------------- worker integration (M7)

def _books(bids: dict, asks: dict):
    yes = {D(str(p)): D(str(s)) for p, s in bids.items()}
    no = {D("1") - D(str(p)): D(str(s)) for p, s in asks.items()}
    return yes, no


class _StubExchange:
    def __init__(self, yes, no):
        self._levels = (yes, no)

    def book_levels(self, ticker):
        return self._levels


class _StubSuppressor:
    def __init__(self, sides):
        self.sides = set(sides)

    def blocked(self, side, now):
        return side in self.sides


@pytest.mark.asyncio
async def test_worker_suppressed_sell_side_quotes_bid_only(tmp_path):
    yes, no = _books({"0.48": 30}, {"0.52": 25})
    w = MarketWorker(
        "KXBTC15M-TEST", _StubExchange(yes, no),
        StrategyParams(quote_size=1, tick=D("0.01")),
        RiskManager(params=RiskParams(max_contracts_per_market=5), state_dir=tmp_path),
        EventLog(tmp_path, "s"),
        WorkerConfig(join_touch_only=True), dry_run=True,
        side_suppressor=_StubSuppressor({"sell"}),
    )
    w.on_book_top(BookTop(ticker="KXBTC15M-TEST", bid=D("0.48"), bid_size=30,
                          ask=D("0.52"), ask_size=25, ts_ms=1))
    await w._requote()
    w.events.flush()
    import json
    p = json.loads(w.events.db.execute(
        "SELECT payload FROM events WHERE type='quote_decision' "
        "ORDER BY ts_ms DESC LIMIT 1").fetchone()[0])
    assert p["bid"] == 0.48 and p["ask"] is None
    w.events.close()


@pytest.mark.asyncio
async def test_worker_unsuppressed_quotes_both_sides(tmp_path):
    yes, no = _books({"0.48": 30}, {"0.52": 25})
    w = MarketWorker(
        "KXBTC15M-TEST", _StubExchange(yes, no),
        StrategyParams(quote_size=1, tick=D("0.01")),
        RiskManager(params=RiskParams(max_contracts_per_market=5), state_dir=tmp_path),
        EventLog(tmp_path, "s"),
        WorkerConfig(join_touch_only=True), dry_run=True,
        side_suppressor=_StubSuppressor(set()),
    )
    w.on_book_top(BookTop(ticker="KXBTC15M-TEST", bid=D("0.48"), bid_size=30,
                          ask=D("0.52"), ask_size=25, ts_ms=1))
    await w._requote()
    w.events.flush()
    import json
    p = json.loads(w.events.db.execute(
        "SELECT payload FROM events WHERE type='quote_decision' "
        "ORDER BY ts_ms DESC LIMIT 1").fetchone()[0])
    assert p["bid"] == 0.48 and p["ask"] == 0.52
    w.events.close()


@pytest.mark.asyncio
async def test_worker_live_suppression_flips_decision_to_cancel(tmp_path):
    """A sell suppression arriving mid-session must flip the standing decision
    from quoting the ask to targeting None (the shared _reconcile(None) path
    - the same machinery every pull lever uses - then cancels the resting
    order in live mode; dry-run carries no order refs, so the decision row is
    the observable)."""
    yes, no = _books({"0.48": 30}, {"0.52": 25})
    sup = _StubSuppressor(set())
    w = MarketWorker(
        "KXBTC15M-TEST", _StubExchange(yes, no),
        StrategyParams(quote_size=1, tick=D("0.01")),
        RiskManager(params=RiskParams(max_contracts_per_market=5), state_dir=tmp_path),
        EventLog(tmp_path, "s"),
        WorkerConfig(join_touch_only=True), dry_run=True,
        side_suppressor=sup,
    )
    w.on_book_top(BookTop(ticker="KXBTC15M-TEST", bid=D("0.48"), bid_size=30,
                          ask=D("0.52"), ask_size=25, ts_ms=1))
    await w._requote()
    sup.sides.add("sell")  # tripwire fires between requotes
    await w._requote()
    w.events.flush()
    import json
    rows = [json.loads(r[0]) for r in w.events.db.execute(
        "SELECT payload FROM events WHERE type='quote_decision' ORDER BY ts_ms")]
    assert rows[0]["ask"] == 0.52 and rows[-1]["ask"] is None
    assert rows[0]["bid"] == 0.48 and rows[-1]["bid"] == 0.48
    w.events.close()
