import json
import time
from decimal import Decimal

from bacchus_mm.analyze import run_report
from bacchus_mm.eventlog import EventLog


def test_emit_writes_jsonl_and_sqlite(tmp_path):
    log = EventLog(tmp_path, "sess-1")
    log.emit("quote_decision", ticker="MKT", mid=Decimal("0.50"), inventory=3)
    log.close()

    jsonl = list(tmp_path.glob("events-*.jsonl"))
    assert len(jsonl) == 1
    record = json.loads(jsonl[0].read_text().splitlines()[0])
    assert record["type"] == "quote_decision"
    assert record["mid"] == 0.50  # Decimal serialized as float
    assert record["session_id"] == "sess-1"


def test_markout_pipeline(tmp_path, capsys):
    log = EventLog(tmp_path, "sess-1")
    now_ms = int(time.time() * 1000)
    # Buy 5 @ 0.48, mid drifts up afterwards: positive markout expected.
    log.record_fill("MKT", "t1", "o1", +5, Decimal("0.48"), False, Decimal("0.50"), now_ms - 700_000)
    log.db.execute(
        "INSERT INTO mids (ts_ms, ticker, mid, bid, ask) VALUES (?,?,?,?,?)",
        (now_ms - 700_000 + 61_000, "MKT", 0.51, 0.50, 0.52),
    )
    log.db.execute(
        "INSERT INTO mids (ts_ms, ticker, mid, bid, ask) VALUES (?,?,?,?,?)",
        (now_ms - 700_000 + 601_000, "MKT", 0.53, 0.52, 0.54),
    )
    log.record_pnl(Decimal("1.5"), Decimal("2.0"), Decimal("0.5"), 5)
    log.db.commit()
    log.close()

    run_report(tmp_path, "markouts", hours=24)
    out = capsys.readouterr().out
    assert "MKT" in out
    assert "+0.15" in out  # (0.51-0.48)*5 at +60s
    run_report(tmp_path, "summary", hours=24)
    out = capsys.readouterr().out
    assert "pnl" in out


def test_summary_edge_at_fill(tmp_path, capsys):
    log = EventLog(tmp_path, "sess-1")
    # Sold at 0.52 with mid 0.50: edge captured = (0.50-0.52)*sign(-1) = +0.02
    log.record_fill("MKT", "t1", "o1", -5, Decimal("0.52"), False, Decimal("0.50"))
    log.record_pnl(Decimal(0), Decimal(0), Decimal(0), 5)
    log.close()
    run_report(tmp_path, "summary", hours=1)
    out = capsys.readouterr().out
    assert "+0.0200" in out


def test_kv_round_trip(tmp_path):
    # 2026-07-17 (FIX-PnL): cross-session persistence for cumulative_pnl /
    # high_water, consumed by the Pass-2 chaining in main.py.
    log = EventLog(tmp_path, "sess-1")
    assert log.kv_get("cumulative_pnl") is None
    log.kv_set("cumulative_pnl", "-3.43")
    assert log.kv_get("cumulative_pnl") == "-3.43"
    log.kv_set("cumulative_pnl", "-2.90")  # overwrite
    assert log.kv_get("cumulative_pnl") == "-2.90"
    log.kv_set("high_water", "0.12")
    assert log.kv_get("high_water") == "0.12"
    log.close()
    log2 = EventLog(tmp_path, "sess-2")  # survives a reopen of the same db
    assert log2.kv_get("cumulative_pnl") == "-2.90"
    log2.close()
