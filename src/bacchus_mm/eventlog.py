"""Structured event logging: JSONL stream + SQLite mirror.

Design goal: a Claude Code session (or any analyst) can reconstruct every
decision the bot made from the logs alone. Every quote decision carries the
book top, inventory, and model internals that produced it; every fill can be
joined to later mid marks to compute markouts (the adverse-selection metric).

Tables:
  events    — raw firehose, one row per event, payload as JSON
  fills     — typed fills for fast joins
  mids      — mid marks per market (on quote decisions + periodic)
  pnl_marks — equity curve for the session
"""

from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts_ms INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ticker TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts_ms);
CREATE TABLE IF NOT EXISTS fills (
    ts_ms INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    trade_id TEXT,
    order_id TEXT,
    ticker TEXT NOT NULL,
    signed_count INTEGER NOT NULL,
    yes_price REAL NOT NULL,
    is_taker INTEGER NOT NULL,
    mid_at_fill REAL
);
CREATE INDEX IF NOT EXISTS idx_fills_ticker_ts ON fills(ticker, ts_ms);
CREATE TABLE IF NOT EXISTS mids (
    ts_ms INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    mid REAL NOT NULL,
    bid REAL,
    ask REAL
);
CREATE INDEX IF NOT EXISTS idx_mids_ticker_ts ON mids(ticker, ts_ms);
CREATE TABLE IF NOT EXISTS venue_marks (
    ts_ms INTEGER NOT NULL,
    kalshi_ticker TEXT NOT NULL,
    polymarket_slug TEXT NOT NULL,
    kalshi_bid REAL, kalshi_ask REAL,
    pm_bid REAL, pm_ask REAL,
    divergence REAL
);
CREATE INDEX IF NOT EXISTS idx_venue_marks ON venue_marks(kalshi_ticker, ts_ms);
CREATE TABLE IF NOT EXISTS pnl_marks (
    ts_ms INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    realized_plus_unrealized REAL NOT NULL,
    session_high REAL NOT NULL,
    drawdown REAL NOT NULL,
    gross_contracts INTEGER NOT NULL
);
"""


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class EventLog:
    def __init__(self, directory: str | Path, session_id: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.db = sqlite3.connect(self.dir / "bacchus.db")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._jsonl_day: Optional[str] = None
        self._jsonl = None

    def _jsonl_handle(self):
        day = time.strftime("%Y%m%d")
        if self._jsonl is None or self._jsonl_day != day:
            if self._jsonl:
                self._jsonl.close()
            self._jsonl = open(self.dir / f"events-{day}.jsonl", "a")
            self._jsonl_day = day
        return self._jsonl

    def emit(self, type_: str, ticker: Optional[str] = None, **payload: Any) -> None:
        ts_ms = int(time.time() * 1000)
        record = {
            "ts_ms": ts_ms,
            "session_id": self.session_id,
            "type": type_,
            "ticker": ticker,
            **_jsonable(payload),
        }
        handle = self._jsonl_handle()
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        self.db.execute(
            "INSERT INTO events (ts_ms, session_id, type, ticker, payload) VALUES (?,?,?,?,?)",
            (ts_ms, self.session_id, type_, ticker, json.dumps(_jsonable(payload))),
        )
        self.db.commit()

    def record_fill(
        self,
        ticker: str,
        trade_id: str,
        order_id: str,
        signed_count: int,
        yes_price: Decimal,
        is_taker: bool,
        mid_at_fill: Optional[Decimal],
        ts_ms: Optional[int] = None,
    ) -> None:
        ts = ts_ms or int(time.time() * 1000)
        self.db.execute(
            "INSERT INTO fills (ts_ms, session_id, trade_id, order_id, ticker, signed_count,"
            " yes_price, is_taker, mid_at_fill) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ts,
                self.session_id,
                trade_id,
                order_id,
                ticker,
                signed_count,
                float(yes_price),
                int(is_taker),
                float(mid_at_fill) if mid_at_fill is not None else None,
            ),
        )
        self.db.commit()
        self.emit(
            "fill",
            ticker=ticker,
            trade_id=trade_id,
            order_id=order_id,
            signed_count=signed_count,
            yes_price=yes_price,
            is_taker=is_taker,
            mid_at_fill=mid_at_fill,
        )

    def record_mid(
        self, ticker: str, mid: Decimal, bid: Optional[Decimal], ask: Optional[Decimal]
    ) -> None:
        self.db.execute(
            "INSERT INTO mids (ts_ms, ticker, mid, bid, ask) VALUES (?,?,?,?,?)",
            (
                int(time.time() * 1000),
                ticker,
                float(mid),
                float(bid) if bid is not None else None,
                float(ask) if ask is not None else None,
            ),
        )
        self.db.commit()

    def record_venue_mark(
        self,
        kalshi_ticker: str,
        polymarket_slug: str,
        kalshi_bid: Optional[Decimal],
        kalshi_ask: Optional[Decimal],
        pm_bid: Optional[Decimal],
        pm_ask: Optional[Decimal],
        divergence: Optional[Decimal],
    ) -> None:
        def f(v: Optional[Decimal]):
            return float(v) if v is not None else None

        self.db.execute(
            "INSERT INTO venue_marks (ts_ms, kalshi_ticker, polymarket_slug, kalshi_bid,"
            " kalshi_ask, pm_bid, pm_ask, divergence) VALUES (?,?,?,?,?,?,?,?)",
            (
                int(time.time() * 1000),
                kalshi_ticker,
                polymarket_slug,
                f(kalshi_bid),
                f(kalshi_ask),
                f(pm_bid),
                f(pm_ask),
                f(divergence),
            ),
        )
        self.db.commit()

    def record_pnl(
        self, pnl: Decimal, session_high: Decimal, drawdown: Decimal, gross_contracts: int
    ) -> None:
        self.db.execute(
            "INSERT INTO pnl_marks (ts_ms, session_id, realized_plus_unrealized, session_high,"
            " drawdown, gross_contracts) VALUES (?,?,?,?,?,?)",
            (
                int(time.time() * 1000),
                self.session_id,
                float(pnl),
                float(session_high),
                float(drawdown),
                gross_contracts,
            ),
        )
        self.db.commit()

    def close(self) -> None:
        if self._jsonl:
            self._jsonl.close()
        self.db.close()
