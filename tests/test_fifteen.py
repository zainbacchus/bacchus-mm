"""Phase D tests (2026-08-06): join-the-touch policy, window discovery
validation, the T-75s pull semantics, and the wind-down zero-picks predicate.
Offline and fast, like everything else in tests/."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.base import BookTop
from bacchus_mm.fifteen import DEFAULT_SERIES, FifteenParams, parse_window
from bacchus_mm.marketmaker import MarketWorker, WorkerConfig
from bacchus_mm.risk import RiskManager, RiskParams
from bacchus_mm.strategy.avellaneda_stoikov import StrategyParams
from bacchus_mm.strategy.join_touch import join_touch_quotes

D = Decimal


def _books(bids: dict, asks: dict):
    """Test helper: build (yes_bids, no_bids) from yes-side prices.
    asks maps yes-side ask price -> size; stored as no-bids at 1-p."""
    yes = {D(str(p)): D(str(s)) for p, s in bids.items()}
    no = {D("1") - D(str(p)): D(str(s)) for p, s in asks.items()}
    return yes, no


# ------------------------------------------------------------ join-touch policy

def test_join_touch_joins_both_touches():
    yes, no = _books({"0.905": 50, "0.904": 120}, {"0.906": 40, "0.907": 300})
    q = join_touch_quotes(yes, no, None, 0, None, 0, inventory=0, max_inventory=5, size=1)
    assert q.bid == D("0.905") and q.bid_size == 1
    assert q.ask == D("0.906") and q.ask_size == 1
    assert q.joined_bid and q.joined_ask


def test_join_touch_excludes_own_order_never_leads():
    # We ARE the entire best bid at 0.905; the external touch is 0.904.
    yes, no = _books({"0.905": 1, "0.904": 120}, {"0.906": 40})
    q = join_touch_quotes(
        yes, no, D("0.905"), 1, None, 0, inventory=0, max_inventory=5, size=1
    )
    assert q.bid == D("0.904"), "must join others, not our own quote"
    # Same on the ask side: our resting ask at 0.906 is a no-bid at 0.094.
    yes2, no2 = _books({"0.905": 50}, {"0.906": 1, "0.907": 300})
    q2 = join_touch_quotes(
        yes2, no2, None, 0, D("0.906"), 1, inventory=0, max_inventory=5, size=1
    )
    assert q2.ask == D("0.907")


def test_join_touch_shares_level_with_others():
    # Others also rest at our price: level survives self-exclusion.
    yes, no = _books({"0.905": 6, "0.904": 120}, {"0.906": 40})
    q = join_touch_quotes(
        yes, no, D("0.905"), 1, None, 0, inventory=0, max_inventory=5, size=1
    )
    assert q.bid == D("0.905")


def test_join_touch_crossed_external_book_quotes_nothing():
    yes, no = _books({"0.907": 10}, {"0.906": 10})  # ext bid > ext ask
    q = join_touch_quotes(yes, no, None, 0, None, 0, inventory=0, max_inventory=5, size=1)
    assert q.bid is None and q.ask is None


def test_join_touch_inventory_caps_sides():
    yes, no = _books({"0.50": 10}, {"0.51": 10})
    long_cap = join_touch_quotes(yes, no, None, 0, None, 0, 5, 5, 1)
    assert long_cap.bid is None and long_cap.ask == D("0.51")
    short_cap = join_touch_quotes(yes, no, None, 0, None, 0, -5, 5, 1)
    assert short_cap.ask is None and short_cap.bid == D("0.50")
    near_cap = join_touch_quotes(yes, no, None, 0, None, 0, 4, 5, 3)
    assert near_cap.bid_size == 1  # only 1 contract of headroom


def test_join_touch_empty_side_quotes_nothing_there():
    yes, no = _books({"0.50": 10}, {})
    q = join_touch_quotes(yes, no, None, 0, None, 0, 0, 5, 1)
    assert q.bid == D("0.50") and q.ask is None and q.ask_size == 0


# ------------------------------------------------------- window discovery gate

# The REAL crypto 15M grid, verified live 2026-08-06: piecewise — decicent
# tails, cent middle. The original uniform-tick gate refused all seven crypto
# series; this payload is pinned so that regression cannot come back.
CRYPTO_RANGES = [
    {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
    {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
    {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
]


def _mkt(**over):
    m = {
        "ticker": "KXBTC15M-26AUG061400-00",
        "open_time": "2026-08-06T17:45:00Z",
        "close_time": "2026-08-06T18:00:00Z",
        "price_ranges": list(CRYPTO_RANGES),
    }
    m.update(over)
    return m


def test_parse_window_accepts_live_piecewise_crypto_grid():
    parsed, reason = parse_window(_mkt(), FifteenParams())
    assert reason is None
    tkr, open_ts, close_ts, tick = parsed
    assert tkr == "KXBTC15M-26AUG061400-00"
    assert close_ts - open_ts == 900.0
    assert tick == D("0.0010")  # finest step, for logging/metadata


def test_parse_window_accepts_uniform_cent_grid():
    # Gold/Silver 15M: uniform 0.01 across [0,1] (verified live 2026-08-06).
    m = _mkt(price_ranges=[{"start": "0.0000", "end": "1.0000", "step": "0.0100"}])
    parsed, reason = parse_window(m, FifteenParams())
    assert reason is None
    assert parsed[3] == D("0.0100")


@pytest.mark.parametrize(
    "over,expect",
    [
        (dict(price_ranges=[]), "no_price_ranges"),
        # Gap between segments: [0, 0.1) then (0.2, 1.0].
        (dict(price_ranges=[
            {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
            {"start": "0.2000", "end": "1.0000", "step": "0.0100"},
        ]), "ranges_gap_or_overlap"),
        # Doesn't reach 1.0.
        (dict(price_ranges=[
            {"start": "0.0000", "end": "0.9000", "step": "0.0100"},
        ]), "ranges_do_not_cover_0_1"),
        (dict(price_ranges=[
            {"start": "0.0000", "end": "1.0000", "step": "0.0500"},
        ]), "tick_out_of_band_0.0500"),
        (dict(price_ranges=[{"start": "0", "end": "1", "step": "bogus"}]),
         "unparseable_price_ranges"),
        (dict(close_time="2026-08-07T18:00:00Z"), "window_span_87300s"),
        (dict(open_time=None), "unparseable_times"),
        (dict(ticker=None), "no_ticker"),
    ],
)
def test_parse_window_refuses_structures_we_did_not_study(over, expect):
    parsed, reason = parse_window(_mkt(**over), FifteenParams())
    assert parsed is None and reason == expect


def test_fifteen_params_from_config_overrides_and_defaults():
    p = FifteenParams.from_config(
        {"fifteen": {"series": ["KXBTC15M"], "quote_size": 2, "requote_tolerance": 0.001}}
    )
    assert p.series == ["KXBTC15M"]
    assert p.quote_size == 2
    assert p.requote_tolerance == D("0.001")
    assert p.max_contracts_per_market == 5  # default preserved
    assert FifteenParams.from_config({}).series == DEFAULT_SERIES


# --------------------------------------------- worker in join-touch mode (dry)

class _StubExchange:
    """Duck-typed exchange for dry-run worker tests: book_levels only."""

    def __init__(self, yes, no):
        self._levels = (yes, no)

    def book_levels(self, ticker):
        return self._levels


@pytest.mark.asyncio
async def test_worker_join_touch_quote_decision(tmp_path):
    yes, no = _books({"0.905": 50, "0.904": 120}, {"0.906": 40})
    ex = _StubExchange(yes, no)
    events = EventLog(tmp_path, "s")
    risk = RiskManager(params=RiskParams(max_contracts_per_market=5), state_dir=tmp_path)
    w = MarketWorker(
        "KXBTC15M-TEST", ex, StrategyParams(quote_size=1, tick=D("0.001")),
        risk, events, WorkerConfig(join_touch_only=True), dry_run=True,
    )
    w.on_book_top(BookTop(
        ticker="KXBTC15M-TEST", bid=D("0.905"), bid_size=50,
        ask=D("0.906"), ask_size=40, ts_ms=1,
    ))
    await w._requote()
    events.flush()
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='quote_decision'"
    ).fetchone()
    assert row is not None
    import json

    p = json.loads(row[0])
    assert p["bid"] == 0.905 and p["ask"] == 0.906
    assert p["joined_bid"] and p["joined_ask"]
    # Queue-position proxy: others' resting depth at the joined levels.
    assert p["join_depth_bid"] == 50 and p["join_depth_ask"] == 40
    assert p["sigma"] == 0.0
    events.close()


@pytest.mark.asyncio
async def test_worker_join_touch_close_reaped_pulls_and_stays_out(tmp_path):
    """T-75s semantics: close_reaped cancels resting quotes and never re-quotes,
    while the (non-reduce-only) position rides to settlement."""
    yes, no = _books({"0.50": 10}, {"0.51": 10})
    ex = _StubExchange(yes, no)
    events = EventLog(tmp_path, "s")
    risk = RiskManager(params=RiskParams(max_contracts_per_market=5), state_dir=tmp_path)
    w = MarketWorker(
        "KXBTC15M-TEST", ex, StrategyParams(quote_size=1, tick=D("0.001")),
        risk, events, WorkerConfig(join_touch_only=True), dry_run=True,
    )
    risk.seed_position("KXBTC15M-TEST", 2, D("0.50"))
    w.close_reaped = True
    w.on_book_top(BookTop(
        ticker="KXBTC15M-TEST", bid=D("0.50"), bid_size=10,
        ask=D("0.51"), ask_size=10, ts_ms=1,
    ))
    await w._requote()
    events.flush()
    n = events.db.execute(
        "SELECT COUNT(*) FROM events WHERE type='quote_decision'"
    ).fetchone()[0]
    assert n == 0, "a reaped window must not emit quote decisions"
    assert not w.reduce_only, "window positions ride to settlement, no exit quoting"
    events.close()


@pytest.mark.asyncio
async def test_worker_quote_decision_dedup_heartbeat(tmp_path):
    """Audit fix: identical decisions inside the heartbeat window emit no row;
    a changed target always emits. Unthrottled, the 15M books produce ~780k
    identical rows/day."""
    yes, no = _books({"0.905": 50, "0.904": 120}, {"0.906": 40})
    ex = _StubExchange(yes, no)
    events = EventLog(tmp_path, "s")
    risk = RiskManager(params=RiskParams(max_contracts_per_market=5), state_dir=tmp_path)
    w = MarketWorker(
        "KXBTC15M-TEST", ex, StrategyParams(quote_size=1, tick=D("0.001")),
        risk, events, WorkerConfig(join_touch_only=True, quote_decision_min_interval=30.0),
        dry_run=True,
    )
    top = BookTop(ticker="KXBTC15M-TEST", bid=D("0.905"), bid_size=50,
                  ask=D("0.906"), ask_size=40, ts_ms=1)
    w.on_book_top(top)
    await w._requote()
    await w._requote()
    await w._requote()
    events.flush()
    n = events.db.execute(
        "SELECT COUNT(*) FROM events WHERE type='quote_decision'"
    ).fetchone()[0]
    assert n == 1, "unchanged decisions inside the heartbeat must not emit"
    # Touch moves: the changed target emits immediately.
    ex._levels = _books({"0.907": 30, "0.905": 50}, {"0.908": 25})
    w.on_book_top(BookTop(ticker="KXBTC15M-TEST", bid=D("0.907"), bid_size=30,
                          ask=D("0.908"), ask_size=25, ts_ms=2))
    await w._requote()
    events.flush()
    n = events.db.execute(
        "SELECT COUNT(*) FROM events WHERE type='quote_decision'"
    ).fetchone()[0]
    assert n == 2
    events.close()


def test_legacy_run_path_emits_every_cycle_by_default():
    """The dedup must be opt-in: WorkerConfig default keeps the calm-mode
    emit-every-cycle behavior the analyze tooling was built on."""
    assert WorkerConfig().quote_decision_min_interval == 0.0


def test_fifteen_series_positions_ride_to_settlement():
    """Audit fix: a held 15M position at startup (mid-window restart) must not
    get a wind-down worker — its exit quotes would rest into the settlement
    averaging window. series_of membership is the gate run_fifteen uses."""
    from bacchus_mm.fees import series_of

    fifteen = set(DEFAULT_SERIES)
    assert series_of("KXBTC15M-26AUG061400-00") in fifteen
    assert series_of("KXGTEMP-26-P0") not in fifteen
    assert series_of("KXFM30YMTG-26DEC31-T5.75") not in fifteen


# ---------------------------------------------- fractional-fill residual carry

@pytest.mark.asyncio
async def test_fractional_fills_carry_residual_not_truncate():
    """Incident 2026-08-06: 15M books trade fractional contracts; int()
    truncation per fill dropped them from tracked position, blinding the caps
    and kill switch. The adapter now carries the residue per ticker."""
    import asyncio as _a

    import aiohttp

    from bacchus_mm.exchange.kalshi import KalshiExchange

    class _Msg:
        def __init__(self, payload):
            self.type = aiohttp.WSMsgType.TEXT
            import json as _j

            self.data = _j.dumps(payload)

    class _Ws:
        def __init__(self, msgs):
            self._m = list(msgs)

        async def send_json(self, obj):
            pass

        async def receive(self):
            if self._m:
                return _Msg(self._m.pop(0))
            raise _a.CancelledError()

    class _Conn:
        def __init__(self, ws):
            self._ws = ws

        async def __aenter__(self):
            return self._ws

        async def __aexit__(self, *e):
            return False

    class _Http:
        def __init__(self, msgs):
            self._msgs = msgs

        def ws_connect(self, url, headers=None, heartbeat=None):
            return _Conn(_Ws(self._msgs))

    class _Auth:
        def headers(self, m, p):
            return {}

    seq_counter = {"n": 0}

    def fill(count_fp, action="buy", side="yes", tid=None):
        seq_counter["n"] += 1  # monotone seq: a repeat reads as a gap -> resub loop
        return {"type": "fill", "sid": 1, "seq": seq_counter["n"], "msg": {
            "trade_id": tid or f"t{count_fp}{action}", "order_id": "o1",
            "market_ticker": "KXBTC15M-TEST", "side": side, "action": action,
            "count_fp": str(count_fp), "yes_price_dollars": "0.5000",
            "is_taker": False, "ts_ms": 1, "fee_cost": "0",
        }}

    msgs = [fill("0.40", tid="a"), fill("0.40", tid="b"), fill("0.40", tid="c"),
            fill("1.00", "sell", tid="d"), fill("0.80", "sell", tid="e")]
    ex = KalshiExchange(env="demo", auth=_Auth())
    session = _Http(msgs)

    async def fake_http():
        return session

    ex._http = fake_http
    seen = []
    with pytest.raises(_a.CancelledError):
        async for _ in ex.stream(lambda: ["KXBTC15M-TEST"], lambda t: None, seen.append):
            pass
    await ex.close()
    # +0.4, +0.4, +0.4 -> emit 0, 0, +1 (carry 0.2); -1.0 -> emit 0 (carry -0.8);
    # -0.8 -> emit -1 (carry -0.6). Tracked total 0 vs real -0.6: drift < 1.
    assert [f_.signed_count for f_ in seen] == [0, 0, 1, 0, -1]
    real = Decimal("0.4") * 3 - Decimal("1.0") - Decimal("0.8")
    tracked = sum(f_.signed_count for f_ in seen)
    assert abs(Decimal(tracked) - real) < 1


# --------------------------------------------------------- halted-idle behavior

@pytest.mark.asyncio
async def test_idle_until_halt_cleared(tmp_path):
    from types import SimpleNamespace as NS

    from bacchus_mm.fifteen import _idle_until_halt_cleared

    halt = tmp_path / "HALTED"
    halt.write_text("kill switch: test")
    risk = NS(check_halt_file=lambda: halt.read_text() if halt.exists() else None)
    stop = __import__("asyncio").Event()

    async def clear_soon():
        await __import__("asyncio").sleep(0.05)
        halt.unlink()

    task = __import__("asyncio").create_task(clear_soon())
    cleared = await _idle_until_halt_cleared(risk, stop, poll_seconds=0.02)
    await task
    assert cleared is True

    halt.write_text("again")
    stop.set()
    assert await _idle_until_halt_cleared(risk, stop, poll_seconds=0.02) is False


# ----------------------------------------------- zero-picks wind-down predicate

def test_wind_down_zero_picks_predicate():
    """The sentinel selector config yields zero picks; the session must proceed
    when positions are held (wind-down) and exit only when there is no work."""
    # mirrors main.py: exit iff not picks and not any(positions.values())
    def should_exit(picks, positions):
        return not picks and not any(positions.values())

    assert should_exit([], {}) is True
    assert should_exit([], {"KXGTEMP-26-P0": 0}) is True
    assert should_exit([], {"KXGTEMP-26-P0": -2}) is False
    assert should_exit([SimpleNamespace()], {}) is False
