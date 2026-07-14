"""Log analysis reports over the SQLite mirror.

  summary   — PnL curve endpoints, fill counts, volume, per-market breakdown
  markouts  — post-fill mid drift at +1m/+10m; negative = adverse selection
  quotes    — quoting activity: decisions, placements, blocks, rejections
  incidents — halts and errors

The markout is the number that decides whether this strategy has an edge:
for each fill, (mid_later - fill_price) * sign(fill). Positive means the
market on average moved our way after we traded; consistently negative at
+1m means informed flow is picking us off and spreads/selection need work.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

MARKOUT_WINDOWS_S = (60, 600)


def _connect(data_dir: Path) -> sqlite3.Connection:
    db_path = Path(data_dir) / "bacchus.db"
    if not db_path.exists():
        raise SystemExit(f"no log database at {db_path} — run the bot first")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _since_ms(hours: float) -> int:
    return int((time.time() - hours * 3600) * 1000)


def report_summary(conn: sqlite3.Connection, hours: float) -> None:
    since = _since_ms(hours)
    pnl = conn.execute(
        "SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM pnl_marks WHERE ts_ms >= ?", (since,)
    ).fetchone()
    if pnl["a"] is None:
        print(f"no pnl marks in the last {hours:g}h")
        return
    first = conn.execute(
        "SELECT realized_plus_unrealized p FROM pnl_marks WHERE ts_ms = ?", (pnl["a"],)
    ).fetchone()["p"]
    last_row = conn.execute(
        "SELECT realized_plus_unrealized p, session_high, drawdown, gross_contracts "
        "FROM pnl_marks WHERE ts_ms = ?", (pnl["b"],)
    ).fetchone()
    print(f"== summary (last {hours:g}h) ==")
    print(f"pnl: {first:+.2f} -> {last_row['p']:+.2f}  (net {last_row['p'] - first:+.2f})")
    print(f"session high {last_row['session_high']:+.2f}, current drawdown {last_row['drawdown']:.2f}")
    print(f"gross position: {last_row['gross_contracts']} contracts")
    print()
    rows = conn.execute(
        """SELECT ticker,
                  COUNT(*) fills,
                  SUM(ABS(signed_count)) contracts,
                  SUM(CASE WHEN is_taker THEN 1 ELSE 0 END) taker_fills,
                  SUM(signed_count) net_position_delta,
                  AVG(CASE WHEN mid_at_fill IS NOT NULL
                       THEN (mid_at_fill - yes_price) * SIGN(signed_count) END) avg_edge_at_fill
           FROM fills WHERE ts_ms >= ? GROUP BY ticker ORDER BY contracts DESC""",
        (since,),
    ).fetchall()
    if not rows:
        print("no fills in window")
        return
    print(f"{'ticker':40s} {'fills':>5s} {'ctrct':>5s} {'taker':>5s} {'netΔ':>5s} {'edge@fill':>9s}")
    for r in rows:
        edge = f"{r['avg_edge_at_fill']:+.4f}" if r["avg_edge_at_fill"] is not None else "     n/a"
        print(
            f"{r['ticker']:40s} {r['fills']:5d} {r['contracts']:5d} "
            f"{r['taker_fills']:5d} {r['net_position_delta']:+5d} {edge:>9s}"
        )
    print("\nedge@fill = (mid - price) * sign: how far inside the mid we were paid on average.")


def report_markouts(conn: sqlite3.Connection, hours: float) -> None:
    since = _since_ms(hours)
    fills = conn.execute(
        "SELECT ts_ms, ticker, signed_count, yes_price FROM fills WHERE ts_ms >= ?", (since,)
    ).fetchall()
    if not fills:
        print(f"no fills in the last {hours:g}h")
        return
    print(f"== markouts (last {hours:g}h, {len(fills)} fills) ==")
    header = f"{'ticker':40s} {'n':>4s}" + "".join(f" {'mo+' + str(w) + 's':>10s}" for w in MARKOUT_WINDOWS_S)
    print(header)
    by_ticker: dict[str, list] = {}
    for f in fills:
        by_ticker.setdefault(f["ticker"], []).append(f)
    totals = {w: [] for w in MARKOUT_WINDOWS_S}
    for ticker, rows in sorted(by_ticker.items()):
        cells = []
        for w in MARKOUT_WINDOWS_S:
            vals = []
            for f in rows:
                mid = conn.execute(
                    "SELECT mid FROM mids WHERE ticker = ? AND ts_ms >= ? ORDER BY ts_ms LIMIT 1",
                    (ticker, f["ts_ms"] + w * 1000),
                ).fetchone()
                if mid is None:
                    continue
                sign = 1 if f["signed_count"] > 0 else -1
                per_contract = (mid["mid"] - f["yes_price"]) * sign
                vals.append(per_contract * abs(f["signed_count"]))
                totals[w].append(per_contract * abs(f["signed_count"]))
            cells.append(f"{sum(vals)/len(vals):+10.4f}" if vals else f"{'n/a':>10s}")
        print(f"{ticker:40s} {len(rows):4d}" + " ".join([""] + cells))
    print("\ntotals (sum $ across all fills):")
    for w in MARKOUT_WINDOWS_S:
        if totals[w]:
            print(f"  +{w}s: {sum(totals[w]):+.2f} over {len(totals[w])} fills")
    print("\npositive = market moved our way after fills; negative = we're being picked off.")


def report_quotes(conn: sqlite3.Connection, hours: float) -> None:
    since = _since_ms(hours)
    rows = conn.execute(
        """SELECT ticker, type, COUNT(*) n FROM events
           WHERE ts_ms >= ? AND type IN
             ('quote_decision','order_placed','order_canceled','order_blocked','order_rejected')
           GROUP BY ticker, type ORDER BY ticker""",
        (since,),
    ).fetchall()
    if not rows:
        print(f"no quoting activity in the last {hours:g}h")
        return
    print(f"== quoting activity (last {hours:g}h) ==")
    grid: dict[str, dict[str, int]] = {}
    for r in rows:
        grid.setdefault(r["ticker"] or "?", {})[r["type"]] = r["n"]
    cols = ["quote_decision", "order_placed", "order_canceled", "order_blocked", "order_rejected"]
    print(f"{'ticker':40s}" + "".join(f" {c.split('_')[1][:8]:>9s}" for c in cols))
    for ticker, counts in sorted(grid.items()):
        print(f"{ticker:40s}" + "".join(f" {counts.get(c, 0):9d}" for c in cols))


def report_incidents(conn: sqlite3.Connection, hours: float) -> None:
    since = _since_ms(hours)
    rows = conn.execute(
        "SELECT ts_ms, type, ticker, payload FROM events WHERE ts_ms >= ? AND type IN "
        "('halt','error','order_rejected','session_start','session_stop') ORDER BY ts_ms",
        (since,),
    ).fetchall()
    print(f"== incidents & sessions (last {hours:g}h, {len(rows)} events) ==")
    for r in rows:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts_ms"] / 1000))
        print(f"{ts} {r['type']:<15s} {r['ticker'] or '-':30s} {r['payload'][:120]}")


def report_divergence(conn: sqlite3.Connection, hours: float) -> None:
    since = _since_ms(hours)
    rows = conn.execute(
        """SELECT kalshi_ticker, polymarket_slug,
                  COUNT(divergence) n,
                  AVG(divergence) avg_div,
                  AVG(ABS(divergence)) avg_abs,
                  MAX(ABS(divergence)) max_abs,
                  AVG(CASE WHEN ABS(divergence) >= 0.02 THEN 1.0 ELSE 0.0 END) pct_2c,
                  AVG(CASE WHEN ABS(divergence) >= 0.05 THEN 1.0 ELSE 0.0 END) pct_5c
           FROM venue_marks WHERE ts_ms >= ? AND divergence IS NOT NULL
           GROUP BY kalshi_ticker, polymarket_slug ORDER BY avg_abs DESC""",
        (since,),
    ).fetchall()
    if not rows:
        print(f"no cross-venue marks in the last {hours:g}h — is `bacchus-mm crossvenue` running?")
        return
    print(f"== kalshi vs polymarket divergence (last {hours:g}h) ==")
    print(f"{'kalshi':32s} {'n':>5s} {'avg':>7s} {'avg|d|':>7s} {'max|d|':>7s} {'>=2c':>6s} {'>=5c':>6s}")
    for r in rows:
        print(
            f"{r['kalshi_ticker']:32s} {r['n']:5d} {r['avg_div']:+7.3f} {r['avg_abs']:7.3f}"
            f" {r['max_abs']:7.3f} {r['pct_2c']*100:5.1f}% {r['pct_5c']*100:5.1f}%"
        )
        last = conn.execute(
            "SELECT kalshi_bid, kalshi_ask, pm_bid, pm_ask, divergence FROM venue_marks"
            " WHERE kalshi_ticker = ? AND divergence IS NOT NULL ORDER BY ts_ms DESC LIMIT 1",
            (r["kalshi_ticker"],),
        ).fetchone()
        if last:
            print(
                f"  now: kalshi {last['kalshi_bid']}/{last['kalshi_ask']}"
                f"  pm {last['pm_bid']}/{last['pm_ask']}  div {last['divergence']:+.3f}"
                f"  ({r['polymarket_slug']})"
            )
    print("\ndivergence = pm_mid - kalshi_mid (yes side). avg sign shows which venue prices higher;")
    print("sustained |d| above your round-trip cost is the Phase B/C signal.")


def run_report(data_dir: Path, report: str, hours: float) -> None:
    conn = _connect(data_dir)
    try:
        {
            "summary": report_summary,
            "markouts": report_markouts,
            "quotes": report_quotes,
            "incidents": report_incidents,
            "divergence": report_divergence,
        }[report](conn, hours)
    finally:
        conn.close()
