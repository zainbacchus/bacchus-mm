"""Fee model tests (2026-07-17, M7): the Kalshi formula, fee booking in risk,
the fills.fee migration, adapter attach (reported vs computed), and
net-of-fee expectancy in analyze."""

import asyncio
import json
import sqlite3
import time
from decimal import Decimal

import aiohttp
import pytest

from bacchus_mm.analyze import run_report
from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.kalshi import KalshiExchange
from bacchus_mm.fees import FeeSchedule, compute_fee
from bacchus_mm.risk import RiskManager, RiskParams

KALSHI = FeeSchedule()  # kalshi_v1, taker 0.07, maker 0


# ------------------------------------------------------------- the formula

def test_fee_formula_parabola_shape():
    # P*(1-P) peaks at 0.5 and is symmetric: 0.1 and 0.9 cost the same.
    assert compute_fee(KALSHI, 100, Decimal("0.5"), True) == Decimal("1.75")
    assert compute_fee(KALSHI, 100, Decimal("0.1"), True) == Decimal("0.63")
    assert compute_fee(KALSHI, 100, Decimal("0.9"), True) == Decimal("0.63")
    mid = compute_fee(KALSHI, 100, Decimal("0.5"), True)
    wing = compute_fee(KALSHI, 100, Decimal("0.1"), True)
    assert mid > wing


def test_fee_rounds_up_to_next_cent():
    # 0.07 x 3 x 0.25 = $0.0525 -> $0.06; Kalshi rounds each trade UP.
    assert compute_fee(KALSHI, 3, Decimal("0.5"), True) == Decimal("0.06")
    # ...so even a 1-contract taker fill pays a minimum cent.
    assert compute_fee(KALSHI, 1, Decimal("0.5"), True) == Decimal("0.02")
    assert compute_fee(KALSHI, 1, Decimal("0.01"), True) == Decimal("0.01")


def test_fee_maker_zero_and_none_formula():
    assert compute_fee(KALSHI, 100, Decimal("0.5"), False) == Decimal("0")
    none_sched = FeeSchedule(formula="none")
    assert compute_fee(none_sched, 100, Decimal("0.5"), True) == Decimal("0")
    maker_sched = FeeSchedule(maker_rate=Decimal("0.0175"))
    assert compute_fee(maker_sched, 100, Decimal("0.5"), False) == Decimal("0.44")


def test_fee_unknown_formula_raises():
    with pytest.raises(ValueError):
        compute_fee(FeeSchedule(formula="bogus"), 1, Decimal("0.5"), True)


# ------------------------------------------- fee booked into risk PnL (M7)

@pytest.mark.parametrize(
    "signed,price,mid",
    [
        (+5, "0.48", "0.50"),  # winning long
        (+5, "0.52", "0.50"),  # losing long
        (-5, "0.52", "0.50"),  # winning short
        (-5, "0.48", "0.50"),  # losing short
    ],
)
def test_fill_with_fee_reduces_pnl_all_quadrants(tmp_path, signed, price, mid):
    fee = Decimal("0.03")
    gross = RiskManager(params=RiskParams(), state_dir=tmp_path)
    gross.on_fill("MKT", signed, Decimal(price))
    gross.on_mid("MKT", Decimal(mid))
    net = RiskManager(params=RiskParams(), state_dir=tmp_path / "n")
    net.on_fill("MKT", signed, Decimal(price), fee)
    net.on_mid("MKT", Decimal(mid))
    assert net.pnl() == gross.pnl() - fee
    assert net.markets["MKT"].fees == fee
    assert gross.markets["MKT"].fees == 0


def test_fee_chains_into_cumulative_pnl(tmp_path):
    rm = RiskManager(params=RiskParams(), state_dir=tmp_path,
                     cumulative_offset=Decimal("1.00"), high_water=Decimal("1.00"))
    rm.on_fill("MKT", +5, Decimal("0.50"), Decimal("0.02"))
    rm.on_mid("MKT", Decimal("0.50"))
    assert rm.cumulative_pnl == Decimal("0.98")


# ------------------------------------------------ fills.fee column migration

def test_fills_fee_migration_on_old_schema(tmp_path):
    # Recreate the pre-M7 fills table (no fee column), then let EventLog open it.
    db = sqlite3.connect(tmp_path / "bacchus.db")
    db.execute(
        "CREATE TABLE fills ("
        " ts_ms INTEGER NOT NULL, session_id TEXT NOT NULL, trade_id TEXT,"
        " order_id TEXT, ticker TEXT NOT NULL, signed_count INTEGER NOT NULL,"
        " yes_price REAL NOT NULL, is_taker INTEGER NOT NULL, mid_at_fill REAL)"
    )
    db.execute(
        "INSERT INTO fills VALUES (1, 'old', 't0', 'o0', 'MKT', 5, 0.5, 0, 0.5)"
    )
    db.commit()
    db.close()

    log = EventLog(tmp_path, "s")
    cols = {r[1] for r in log.db.execute("PRAGMA table_info(fills)")}
    assert "fee" in cols
    # Pre-existing rows read as zero-fee.
    assert log.db.execute("SELECT fee FROM fills WHERE trade_id='t0'").fetchone()[0] == 0
    # ...and new fills carry their fee.
    log.record_fill("MKT", "t1", "o1", 5, Decimal("0.50"), True, None, 2,
                    fee=Decimal("0.09"), fee_source="computed")
    assert log.db.execute("SELECT fee FROM fills WHERE trade_id='t1'").fetchone()[0] == 0.09
    log.close()


def test_record_fill_payload_marks_fee_source(tmp_path):
    log = EventLog(tmp_path, "s")
    log.record_fill("MKT", "t1", "o1", 5, Decimal("0.50"), True, None, 1,
                    fee=Decimal("0.09"), fee_source="computed")
    log.record_fill("MKT", "t2", "o2", 5, Decimal("0.50"), False, None, 2)
    log.flush()
    rows = log.db.execute(
        "SELECT payload FROM events WHERE type='fill' ORDER BY rowid"
    ).fetchall()
    p1, p2 = (json.loads(r[0]) for r in rows)
    assert p1["fee"] == 0.09 and p1["fee_source"] == "computed"
    assert p2["fee"] == 0 and p2["fee_source"] == "none"
    log.close()


# ------------------------------------------------ kalshi adapter attach (M7)

class _FakeWsMessage:
    def __init__(self, payload: dict):
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload)


class _FakeWs:
    def __init__(self, messages):
        self._messages = list(messages)

    async def send_json(self, obj):
        pass

    async def receive(self):
        # 2026-07-17 (M2): the stream loop receives under a timeout now.
        if self._messages:
            return _FakeWsMessage(self._messages.pop(0))
        raise asyncio.CancelledError()


class _FakeWsConn:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


class _FakeHttpSession:
    def __init__(self, messages):
        self._messages = messages

    def ws_connect(self, url, headers=None, heartbeat=None):
        return _FakeWsConn(_FakeWs(self._messages))


class _StubAuth:
    def headers(self, method, path):
        return {}


def _fill_msg(**overrides):
    msg = {
        "trade_id": "t1", "order_id": "o1", "market_ticker": "MKT",
        "side": "yes", "action": "buy", "count_fp": "5.00",
        "yes_price_dollars": "0.4800", "is_taker": True, "ts_ms": 1,
    }
    msg.update(overrides)
    return {"type": "fill", "sid": 1, "seq": 1, "msg": msg}


async def _one_fill(messages, schedule):
    ex = KalshiExchange(env="demo", auth=_StubAuth(), fee_schedule=schedule)
    session = _FakeHttpSession(messages)

    async def fake_http():
        return session

    ex._http = fake_http
    seen = []
    with pytest.raises(asyncio.CancelledError):
        async for _ in ex.stream(lambda: ["MKT"], lambda top: None, seen.append):
            pass
    await ex.close()
    return seen[0]


@pytest.mark.asyncio
async def test_adapter_prefers_reported_fee_cost():
    f = await _one_fill([_fill_msg(fee_cost="0.04")], KALSHI)
    # Reported wins over the formula (which would give $0.09 for 5 @ 0.48 taker).
    assert f.fee == Decimal("0.04") and f.fee_source == "reported"


@pytest.mark.asyncio
async def test_adapter_computes_fee_when_payload_lacks_it():
    f = await _one_fill([_fill_msg()], KALSHI)
    # ceil(0.07 x 5 x 0.48 x 0.52 x 100)/100 = ceil(8.736)/100 = $0.09
    assert f.fee == Decimal("0.09") and f.fee_source == "computed"


@pytest.mark.asyncio
async def test_adapter_no_schedule_no_fee():
    f = await _one_fill([_fill_msg()], None)
    assert f.fee == 0 and f.fee_source == "none"


# --------------------------------------------------- config plumbing (M7+M1)

def test_config_fees_and_logging_parsing(tmp_path):
    from bacchus_mm.config import Config

    (tmp_path / "config.yaml").write_text(
        "fees:\n"
        "  kalshi:\n"
        "    taker_rate: 0.035\n"
        "    maker_rate: 0.0\n"
        "    formula: kalshi_v1\n"
        "logging:\n"
        "  flush_seconds: 2.5\n"
        "  flush_batch: 100\n"
        "  events_keep_days: 30\n"
    )
    cfg = Config.load(tmp_path)
    assert cfg.fees["kalshi"].taker_rate == Decimal("0.035")
    assert cfg.fees["kalshi"].formula == "kalshi_v1"
    assert cfg.log_flush_seconds == 2.5
    assert cfg.log_flush_batch == 100
    assert cfg.log_events_keep_days == 30


def test_config_fee_defaults_without_section(tmp_path):
    from bacchus_mm.config import Config

    (tmp_path / "config.yaml").write_text("env: demo\n")
    cfg = Config.load(tmp_path)
    assert cfg.fees["kalshi"] == FeeSchedule()
    assert cfg.log_flush_seconds == 1.0
    assert cfg.log_flush_batch == 500
    assert cfg.log_events_keep_days == 14


# --------------------------------------------- net-of-fee in analyze (M7)

def test_markouts_report_net_of_fee(tmp_path, capsys):
    log = EventLog(tmp_path, "sess-1")
    now_ms = int(time.time() * 1000)
    # Buy 5 @ 0.48 (taker, $0.02 fee); mid rises to 0.51 at +61s.
    log.record_fill("MKT", "t1", "o1", +5, Decimal("0.48"), True, Decimal("0.50"),
                    now_ms - 700_000, fee=Decimal("0.02"), fee_source="reported")
    log.db.execute(
        "INSERT INTO mids (ts_ms, ticker, mid, bid, ask) VALUES (?,?,?,?,?)",
        (now_ms - 700_000 + 61_000, "MKT", 0.51, 0.50, 0.52),
    )
    log.db.commit()
    log.close()
    run_report(tmp_path, "markouts", hours=24)
    out = capsys.readouterr().out
    assert "+0.1500" in out  # gross: (0.51-0.48) x 5
    assert "+0.1300" in out  # net: gross - $0.02 fee
    assert "net-of-fee" in out and "/contract" in out


def test_summary_reports_fees_and_net_expectancy(tmp_path, capsys):
    log = EventLog(tmp_path, "sess-1")
    # Sold 5 @ 0.52 with mid 0.50: gross edge (0.50-0.52)*-1 = +0.02/contract;
    # $0.05 of fees over 5 contracts -> net +0.01/contract.
    log.record_fill("MKT", "t1", "o1", -5, Decimal("0.52"), True, Decimal("0.50"),
                    fee=Decimal("0.05"), fee_source="reported")
    log.record_pnl(Decimal(0), Decimal(0), Decimal(0), 5)
    log.close()
    run_report(tmp_path, "summary", hours=1)
    out = capsys.readouterr().out
    assert "+0.0200" in out  # gross edge@fill preserved
    assert "+0.0100" in out  # net-of-fee expectancy
    assert "total fees in window: $0.05" in out


def test_analyze_reads_pre_m7_db_read_only(tmp_path, capsys):
    # 2026-07-18 (round 2): `bacchus-mm analyze` opens the live DB READ-ONLY
    # while the bot writes — it must NOT mutate the file (the old in-place ALTER
    # contended for the WAL write lock). A pre-M7 fills table (no fee column) is
    # handled by degrading fee to 0 in the queries, and the DB is left untouched.
    db = sqlite3.connect(tmp_path / "bacchus.db")
    db.execute(
        "CREATE TABLE fills ("
        " ts_ms INTEGER NOT NULL, session_id TEXT NOT NULL, trade_id TEXT,"
        " order_id TEXT, ticker TEXT NOT NULL, signed_count INTEGER NOT NULL,"
        " yes_price REAL NOT NULL, is_taker INTEGER NOT NULL, mid_at_fill REAL)"
    )
    db.execute(
        "CREATE TABLE pnl_marks ("
        " ts_ms INTEGER NOT NULL, session_id TEXT NOT NULL,"
        " realized_plus_unrealized REAL NOT NULL, session_high REAL NOT NULL,"
        " drawdown REAL NOT NULL, gross_contracts INTEGER NOT NULL)"
    )
    now_ms = int(time.time() * 1000)
    db.execute("INSERT INTO fills VALUES (?, 's', 't1', 'o1', 'MKT', 5, 0.48, 1, 0.5)",
               (now_ms,))
    db.execute("INSERT INTO pnl_marks VALUES (?, 's', 1.0, 1.0, 0.0, 5)", (now_ms,))
    db.commit()
    db.close()
    run_report(tmp_path, "summary", hours=1)  # must not raise
    out = capsys.readouterr().out
    assert "MKT" in out and "fees" in out
    # The DB must be UNCHANGED — no fee column added (read-only, no ALTER).
    check = sqlite3.connect(tmp_path / "bacchus.db")
    cols = {r[1] for r in check.execute("PRAGMA table_info(fills)")}
    check.close()
    assert "fee" not in cols
