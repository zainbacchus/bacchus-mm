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
  kv        — cross-session key/value state (2026-07-17: cumulative PnL chain)

2026-07-17 (M1) write path: the firehose (events) and mids are queued in
memory and flushed in ONE transaction every flush_seconds or every
flush_batch rows, whichever first — the old commit+fsync per row produced
145k rows / 62MB in 4 days at 8 markets and would stall the loop at 15-20.
Everything stays on the caller's thread (the single asyncio loop); there are
no writer threads. Money-side tables (fills, pnl_marks, venue_marks, kv)
commit immediately: they are rare, and the fill-dedup seed / kill-switch
chain must always read committed (not queued) state.

Durability precisely (2026-07-18, round 2 correction): journal_mode=WAL +
synchronous=NORMAL means a committed transaction survives a PROCESS crash
(it's in the WAL) but is fsync'd only at checkpoint, so an OS crash / power
loss can roll back the last un-checkpointed commits — this applies to the
money-side tables too, not just the batched ones. The events firehose is
additionally mirrored to JSONL immediately on emit(), so queued-but-unflushed
EVENTS survive even a process crash; MIDS are NOT JSONL-mirrored, so the last
~flush_seconds of queued mids are lost on a process crash (acceptable: mids
are a dense marking series, and the equity chain re-seeds held positions from
the last COMMITTED mid, tolerating a small gap).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts_ms INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ticker TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts_ms);
-- 2026-07-18 (round 2): the retention prune filters on ts_ms alone; without a
-- ts_ms-leading index the DELETE was a full-table SCAN (~163ms on the loop).
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms);
CREATE TABLE IF NOT EXISTS fills (
    ts_ms INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    trade_id TEXT,
    order_id TEXT,
    ticker TEXT NOT NULL,
    signed_count INTEGER NOT NULL,
    yes_price REAL NOT NULL,
    is_taker INTEGER NOT NULL,
    mid_at_fill REAL,
    fee REAL NOT NULL DEFAULT 0
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
-- 2026-07-17 (FIX-PnL): scratch kv store for cross-session state. Pass 2 uses
-- keys "cumulative_pnl" and "high_water" to chain account equity across
-- sessions instead of rebasing to zero at every session start.
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_EVENTS_SQL = (
    "INSERT INTO events (ts_ms, session_id, type, ticker, payload) VALUES (?,?,?,?,?)"
)
_MIDS_SQL = "INSERT INTO mids (ts_ms, ticker, mid, bid, ask) VALUES (?,?,?,?,?)"


def ensure_schema_upgrades(db: sqlite3.Connection) -> None:
    """Defensive ALTERs for DBs created before a column existed (2026-07-17,
    M7: fills.fee). Shared with analyze.py, which opens the DB without an
    EventLog and must not crash on a pre-M7 file."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(fills)")}
    if cols and "fee" not in cols:  # empty cols -> no fills table yet, leave it
        db.execute("ALTER TABLE fills ADD COLUMN fee REAL NOT NULL DEFAULT 0")
        db.commit()


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class EventLog:
    def __init__(
        self,
        directory: str | Path,
        session_id: str,
        flush_seconds: float = 1.0,
        flush_batch: int = 500,
        events_keep_days: int = 14,
        clock: Callable[[], float] = time.monotonic,
        on_event: Optional[Callable[[], None]] = None,
    ):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        # 2026-07-17 (DEPLOY): optional liveness hook, fired on every emit —
        # main.py points this at the health endpoint's heartbeat. Public so it
        # can be attached after construction (the health state is built later).
        self.on_event = on_event
        self.db = sqlite3.connect(self.dir / "bacchus.db")
        self.db.execute("PRAGMA journal_mode=WAL")
        # 2026-07-17 (M1): WAL already survives crashes; FULL's per-commit
        # fsync was the amplification. NORMAL syncs at checkpoint time only.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self._jsonl_day: Optional[str] = None
        self._jsonl = None
        # 2026-07-17 (M1): batched-write state. `clock` is injected in tests
        # (monotonic in prod) — flush cadence must not depend on wall time.
        self._flush_seconds = float(flush_seconds)
        self._flush_batch = int(flush_batch)
        self._clock = clock
        self._pending_events: list[tuple] = []
        self._pending_mids: list[tuple] = []
        self._last_flush = clock()
        self._events_keep_days = int(events_keep_days)
        self._last_prune_day: Optional[str] = None
        # Retention runs once at startup, then daily inside flush().
        self.prune_events()

    def _migrate(self) -> None:
        ensure_schema_upgrades(self.db)

    # ------------------------------------------------------------ batching

    def _enqueue(self, kind: str, params: tuple) -> None:
        if kind == "events":
            self._pending_events.append(params)
        else:
            self._pending_mids.append(params)
        pending = len(self._pending_events) + len(self._pending_mids)
        if pending >= self._flush_batch or (
            self._clock() - self._last_flush >= self._flush_seconds
        ):
            self.flush()

    def flush(self) -> None:
        """One transaction for everything queued. Idempotent; safe to call
        from the trading loop's flusher task and from close()."""
        if not self._pending_events and not self._pending_mids:
            self._last_flush = self._clock()
            self._maybe_prune_daily()
            return
        if self._pending_events:
            self.db.executemany(_EVENTS_SQL, self._pending_events)
            self._pending_events.clear()
        if self._pending_mids:
            self.db.executemany(_MIDS_SQL, self._pending_mids)
            self._pending_mids.clear()
        self.db.commit()
        self._last_flush = self._clock()
        self._maybe_prune_daily()

    # ------------------------------------------------------------ retention

    def prune_events(self, now_ms: Optional[int] = None) -> int:
        """2026-07-17 (M1): prune ONLY the events firehose; the JSONL files
        are the archive and fills/mids/pnl_marks/venue_marks/kv are kept
        forever (money state and markout history). events_keep_days <= 0
        disables pruning.

        2026-07-18 (round 2): deleted in bounded chunks so the DELETE can never
        block the trading loop for more than one small batch. The idx_events_ts
        index turns each chunk into a range scan; a control yield between chunks
        lets the loop breathe. Safe to run on the loop at this granularity."""
        if self._events_keep_days <= 0:
            return 0
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff = now - self._events_keep_days * 86_400_000
        total = 0
        while True:
            cur = self.db.execute(
                "DELETE FROM events WHERE rowid IN "
                "(SELECT rowid FROM events WHERE ts_ms < ? LIMIT 2000)",
                (cutoff,),
            )
            self.db.commit()
            total += cur.rowcount
            if cur.rowcount < 2000:
                break
        self._last_prune_day = time.strftime("%Y%m%d")
        self.emit("events_pruned", deleted=total, keep_days=self._events_keep_days)
        return total

    def _maybe_prune_daily(self) -> None:
        day = time.strftime("%Y%m%d")  # wall clock: retention is calendar-based
        if day != self._last_prune_day:
            self.prune_events()

    # ---------------------------------------------------------------- jsonl

    def _jsonl_handle(self):
        day = time.strftime("%Y%m%d")
        if self._jsonl is None or self._jsonl_day != day:
            if self._jsonl:
                self._jsonl.close()
            self._jsonl = open(self.dir / f"events-{day}.jsonl", "a")
            self._jsonl_day = day
        return self._jsonl

    def emit(self, type_: str, ticker: Optional[str] = None, **payload: Any) -> None:
        # 2026-07-17 (DEPLOY): the health heartbeat must never break logging.
        if self.on_event is not None:
            try:
                self.on_event()
            except Exception:  # noqa: BLE001
                pass
        ts_ms = int(time.time() * 1000)
        record = {
            "ts_ms": ts_ms,
            "session_id": self.session_id,
            "type": type_,
            "ticker": ticker,
            **_jsonable(payload),
        }
        # The JSONL mirror stays immediate — it is the durable firehose.
        handle = self._jsonl_handle()
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        self._enqueue(
            "events", (ts_ms, self.session_id, type_, ticker, json.dumps(_jsonable(payload)))
        )

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
        fee: Optional[Decimal] = None,
        fee_source: str = "none",
    ) -> None:
        """Commits immediately, not via the batched queue (2026-07-17, M1):
        fills are rare and the next session's fill-dedup set is seeded from
        this table — a fill left in an unflushed queue could double-count on
        ws redelivery. (Durability is process-crash-safe under WAL, not
        power-loss-safe under synchronous=NORMAL — see the module docstring.)"""
        ts = ts_ms or int(time.time() * 1000)
        fee_d = fee if fee is not None else Decimal(0)
        self.db.execute(
            "INSERT INTO fills (ts_ms, session_id, trade_id, order_id, ticker, signed_count,"
            " yes_price, is_taker, mid_at_fill, fee) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
                float(fee_d),
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
            fee=fee_d,
            # "reported" = exchange's own number (Kalshi fee_cost); "computed"
            # = local formula estimate; "none" = no schedule configured.
            fee_source=fee_source,
        )

    def record_mid(
        self, ticker: str, mid: Decimal, bid: Optional[Decimal], ask: Optional[Decimal]
    ) -> None:
        self._enqueue(
            "mids",
            (
                int(time.time() * 1000),
                ticker,
                float(mid),
                float(bid) if bid is not None else None,
                float(ask) if ask is not None else None,
            ),
        )

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

    def kv_get(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        """Commits immediately, not via the batched queue (2026-07-17, M1):
        one rare row carrying the kill-switch equity chain — it must be
        committed (readable across a restart) the moment it returns, not at
        the next flush. (Power-loss durability is bounded by synchronous=NORMAL
        like every table on this connection — see the module docstring.)"""
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    def known_trade_ids(self) -> set[str]:
        """Every trade id in the fills table — seeds the fill dedup set
        (2026-07-17, H5): after a ws resubscribe Kalshi redelivers recent
        fills, and an unseeded set would double-count them."""
        rows = self.db.execute(
            "SELECT trade_id FROM fills WHERE trade_id IS NOT NULL"
        ).fetchall()
        return {r[0] for r in rows if r[0]}

    def mark_settled(self, ticker: str, settlement: str) -> None:
        """Persist that a market was realized (2026-07-18, round 2): survives a
        restart so a position still reported by the exchange during the
        determined->finalized->payout window is never realized twice into the
        cumulative kill-switch chain. Kalshi tickers are event-unique, so a
        settled marker can never suppress a genuinely new position."""
        self.kv_set(f"settled:{ticker}", settlement)

    def settled_tickers(self) -> set[str]:
        rows = self.db.execute(
            "SELECT key FROM kv WHERE key LIKE 'settled:%'"
        ).fetchall()
        return {r[0].split(":", 1)[1] for r in rows}

    def close(self) -> None:
        # 2026-07-17 (M1): drain the queue before closing — normal shutdown,
        # halt, and exception paths in main.py all funnel through here. A
        # failed final flush must not crash teardown; queued EVENTS still live
        # in JSONL (mids do not — see the module docstring).
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            log.exception(
                "final eventlog flush failed — %d queued events recoverable from JSONL, "
                "%d queued mids lost",
                len(self._pending_events),
                len(self._pending_mids),
            )
        if self._jsonl:
            self._jsonl.close()
        try:
            # Keep the WAL from growing unbounded on disk; best-effort — a
            # concurrent reader (analyze) simply limits how much truncates.
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
        self.db.close()
