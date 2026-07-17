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
    # 2026-07-17 (M1): line 0 is the startup events_pruned heartbeat; find ours.
    records = [json.loads(line) for line in jsonl[0].read_text().splitlines()]
    record = next(r for r in records if r["type"] == "quote_decision")
    assert record["mid"] == 0.50  # Decimal serialized as float
    assert record["session_id"] == "sess-1"
    # ...and the SQLite mirror has it (close() drained the batch).
    import sqlite3

    db = sqlite3.connect(tmp_path / "bacchus.db")
    n = db.execute(
        "SELECT COUNT(*) FROM events WHERE type='quote_decision'"
    ).fetchone()[0]
    db.close()
    assert n == 1


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


# ---------------------------------------------------------- 2026-07-17 (M1)
# Batched writes + retention. The batch covers events and mids only; fills,
# pnl_marks, venue_marks, and kv stay synchronous (money state / dedup seed).

def _count_sqlite(log: EventLog, table: str) -> int:
    return log.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_batch_flush_by_count(tmp_path):
    # flush_seconds huge so only the count trigger can fire. The startup
    # events_pruned row occupies one queue slot, hence the 4/5 arithmetic.
    log = EventLog(tmp_path, "s", flush_seconds=3600, flush_batch=4)
    assert _count_sqlite(log, "events") == 0  # prune heartbeat still queued
    log.emit("a")
    log.emit("b")
    assert _count_sqlite(log, "events") == 0
    log.emit("c")  # 4th pending row -> one transaction for all four
    assert _count_sqlite(log, "events") == 4
    log.emit("d")
    assert _count_sqlite(log, "events") == 4  # queued again after the flush
    log.flush()
    assert _count_sqlite(log, "events") == 5
    log.close()


def test_batch_flush_by_time(tmp_path):
    clock_val = [1000.0]  # injected monotonic clock
    log = EventLog(tmp_path, "s", flush_seconds=1.0, flush_batch=10_000,
                   clock=lambda: clock_val[0])
    log.emit("a")
    assert _count_sqlite(log, "events") == 0
    clock_val[0] += 1.5  # past flush_seconds: the next enqueue flushes
    log.emit("b")
    assert _count_sqlite(log, "events") == 3  # prune heartbeat + a + b
    log.close()


def test_close_drains_pending_rows(tmp_path):
    log = EventLog(tmp_path, "s", flush_seconds=3600, flush_batch=10_000)
    for i in range(7):
        log.emit("quote_decision", ticker="MKT", i=i)
    log.record_mid("MKT", Decimal("0.51"), Decimal("0.50"), Decimal("0.52"))
    assert _count_sqlite(log, "events") == 0  # nothing flushed yet
    log.close()  # shutdown path must lose nothing
    import sqlite3

    db = sqlite3.connect(tmp_path / "bacchus.db")
    n_events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_mids = db.execute("SELECT COUNT(*) FROM mids").fetchone()[0]
    db.close()
    assert n_events == 8  # 7 + prune heartbeat
    assert n_mids == 1


def test_kv_immediate_under_batching(tmp_path):
    # kv carries the kill-switch equity chain — it must be visible to OTHER
    # connections (analyze, a restarted bot) without waiting for a flush.
    import sqlite3

    log = EventLog(tmp_path, "s", flush_seconds=3600, flush_batch=10_000)
    log.emit("pending_stuff")  # sit in the queue, uncommitted
    log.kv_set("cumulative_pnl", "-1.25")
    other = sqlite3.connect(tmp_path / "bacchus.db")
    assert other.execute("SELECT value FROM kv WHERE key='cumulative_pnl'").fetchone()[0] == "-1.25"
    assert other.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0  # still queued
    other.close()
    log.close()


def test_prune_deletes_old_events_keeps_fills_mids(tmp_path):
    log = EventLog(tmp_path, "s", events_keep_days=14)
    now_ms = int(time.time() * 1000)
    old = now_ms - 30 * 86_400_000  # 30 days ago
    log.db.execute(
        "INSERT INTO events (ts_ms, session_id, type, ticker, payload)"
        " VALUES (?,?,?,?,?)",
        (old, "old-sess", "quote_decision", "MKT", "{}"),
    )
    log.db.execute(
        "INSERT INTO mids (ts_ms, ticker, mid, bid, ask) VALUES (?,?,?,?,?)",
        (old, "MKT", 0.5, 0.49, 0.51),
    )
    log.db.commit()
    log.record_fill("MKT", "t1", "o1", 5, Decimal("0.50"), False, None, old)
    deleted = log.prune_events(now_ms=now_ms)
    assert deleted == 1
    remaining = log.db.execute("SELECT MIN(ts_ms) FROM events").fetchone()[0]
    assert remaining is None or remaining >= now_ms - 14 * 86_400_000
    assert _count_sqlite(log, "fills") == 1  # money tables are forever
    assert _count_sqlite(log, "mids") == 1
    log.flush()
    payload = json.loads(
        log.db.execute(
            "SELECT payload FROM events WHERE type='events_pruned' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    )
    assert payload["deleted"] == 1 and payload["keep_days"] == 14
    log.close()


def test_prune_runs_daily_from_flush(tmp_path):
    # Startup prune happens in __init__; after that, flush() drives the daily
    # cadence (calendar day change), so a long-lived process prunes exactly
    # once per day without a separate scheduler.
    log = EventLog(tmp_path, "s", events_keep_days=14)
    old = int(time.time() * 1000) - 30 * 86_400_000
    log.db.execute(
        "INSERT INTO events (ts_ms, session_id, type, ticker, payload)"
        " VALUES (?,?,?,?,?)",
        (old, "old-sess", "quote_decision", "MKT", "{}"),
    )
    log.db.commit()
    log.flush()
    assert _count_sqlite(log, "events") >= 1  # same-day flush: no prune yet
    log._last_prune_day = "19990101"  # simulate the date rolling over
    log.flush()
    assert log._last_prune_day != "19990101"
    assert log.db.execute(
        "SELECT COUNT(*) FROM events WHERE ts_ms < ?", (old + 1,)
    ).fetchone()[0] == 0
    log.close()


def test_prune_disabled_with_nonpositive_days(tmp_path):
    log = EventLog(tmp_path, "s", events_keep_days=0)
    old = int(time.time() * 1000) - 365 * 86_400_000
    log.db.execute(
        "INSERT INTO events (ts_ms, session_id, type, ticker, payload)"
        " VALUES (?,?,?,?,?)",
        (old, "old-sess", "quote_decision", "MKT", "{}"),
    )
    log.db.commit()
    assert log.prune_events() == 0
    assert _count_sqlite(log, "events") == 1
    log.close()


def test_analyze_summary_over_pruned_db(tmp_path, capsys):
    # Retention removes only the firehose; summary/markouts read fills and
    # pnl_marks, which survive.
    log = EventLog(tmp_path, "sess-1", events_keep_days=14)
    now_ms = int(time.time() * 1000)
    old = now_ms - 30 * 86_400_000
    for ts, ev_type in ((old, "quote_decision"), (now_ms - 1000, "order_placed")):
        log.db.execute(
            "INSERT INTO events (ts_ms, session_id, type, ticker, payload)"
            " VALUES (?,?,?,?,?)",
            (ts, "s", ev_type, "MKT", "{}"),
        )
    log.db.commit()
    log.record_fill("MKT", "t1", "o1", -5, Decimal("0.52"), False, Decimal("0.50"))
    log.record_pnl(Decimal(0), Decimal(0), Decimal(0), 5)
    deleted = log.prune_events(now_ms=now_ms)
    assert deleted == 1
    log.close()
    run_report(tmp_path, "summary", hours=1)
    out = capsys.readouterr().out
    assert "pnl" in out and "+0.0200" in out  # edge@fill intact post-prune
