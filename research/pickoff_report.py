"""Pick-off report: how the bot's maker fills fare AFTER they happen.

Usage (interactive sessions, against a LOCAL snapshot - never the live DB):
    zsh scripts/pull-fly-db.sh                      # snapshot -> local file
    python research/pickoff_report.py <snapshot.db> [--hours 24] [--t0 ISO]

For every settled fill it computes two numbers:
    settled = signed x (settlement - price)      ... the money
    markout = signed x (mid(t+h) - mid_at_fill)  ... the information
Markouts at +30s/+180s answer "who filled us": a fill whose markout goes
immediately negative was picked by a counterparty who knew where price was
going (adverse selection); a fill with ~zero markout that settles positive
is honest spread capture. Buckets: series, minute-of-window, side, price
band of the side taken, and queue depth at the joined level (from the
nearest preceding quote_decision, the bot's own strategy telemetry).

Settlement source: settlement_realized events where present, else the
ticker's final mid (15M binaries converge hard; coverage is printed and
sub-95% convergence should be treated as a data problem). Close times come
from the ticker's ET timestamp via zoneinfo (DST-correct). "Now" is the
snapshot's max event time, so reports are reproducible per snapshot.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TKR = re.compile(r"^KX[A-Z]+15M-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)")
MON = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
HORIZONS = (30, 180)


def close_utc(tkr: str) -> float | None:
    m = TKR.match(tkr)
    if not m:
        return None
    yy, mon, dd, hh, mi = m.groups()
    et = datetime(2000 + int(yy), MON[mon], int(dd), int(hh), int(mi), tzinfo=ET)
    return et.astimezone(timezone.utc).timestamp()


def band(px: float) -> str:
    if px < 0.35:
        return "0.10-0.35"
    if px <= 0.65:
        return "0.35-0.65"
    if px <= 0.90:
        return "0.65-0.90"
    return "0.90-1.00"


def depth_bucket(d: float | None) -> str:
    if d is None:
        return "unknown"
    if d <= 20:
        return "thin <=20"
    if d <= 100:
        return "mid 21-100"
    return "deep >100"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--t0", help="ISO UTC anchor; overrides --hours")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    now = db.execute("SELECT max(ts_ms) FROM events").fetchone()[0] / 1000
    t0 = (datetime.fromisoformat(args.t0).timestamp() if args.t0
          else now - args.hours * 3600)

    # settlement truth: realized events first, final mid as fallback
    settle: dict[str, float] = {}
    for tkr, pl in db.execute(
            "SELECT ticker, payload FROM events WHERE type='settlement_realized'"):
        s = json.loads(pl).get("settlement")
        if s is not None:
            settle[tkr] = float(s)
    final = dict(db.execute(
        """SELECT ticker, mid FROM (SELECT ticker, mid, ts_ms,
           row_number() OVER (PARTITION BY ticker ORDER BY ts_ms DESC) rn
           FROM mids) WHERE rn=1"""))

    fills = db.execute(
        """SELECT ticker, ts_ms, signed_count, yes_price, mid_at_fill
           FROM fills WHERE ts_ms >= ? AND signed_count != 0""",
        (t0 * 1000,)).fetchall()
    tickers = sorted({x[0] for x in fills})

    # mids + quote_decision depth series per involved ticker, for bisecting
    mids: dict[str, tuple[list, list]] = {}
    for tkr in tickers:
        rows = db.execute(
            "SELECT ts_ms, mid FROM mids WHERE ticker=? ORDER BY ts_ms", (tkr,)
        ).fetchall()
        mids[tkr] = ([r[0] / 1000 for r in rows], [float(r[1]) for r in rows])
    depths: dict[str, tuple[list, list, list]] = {}
    for tkr in tickers:
        rows = db.execute(
            "SELECT ts_ms, payload FROM events WHERE type='quote_decision' "
            "AND ticker=? ORDER BY ts_ms", (tkr,)).fetchall()
        ts, jb, ja = [], [], []
        for t, pl in rows:
            v = json.loads(pl)
            ts.append(t / 1000)
            jb.append(v.get("join_depth_bid"))
            ja.append(v.get("join_depth_ask"))
        depths[tkr] = (ts, jb, ja)

    def mid_at(tkr: str, when: float, cl: float, stl: float) -> float | None:
        if when >= cl:
            return stl  # past close: the markout horizon IS settlement
        ts, ms_ = mids[tkr]
        i = bisect.bisect_right(ts, when + 10)
        if i and when - ts[i - 1] <= 30:
            return ms_[i - 1]
        return None

    def depth_at(tkr: str, when: float, is_buy: bool) -> float | None:
        ts, jb, ja = depths[tkr]
        i = bisect.bisect_right(ts, when)
        if i and when - ts[i - 1] <= 120:
            d = (jb if is_buy else ja)[i - 1]
            return None if d is None else float(d)
        return None

    recs, conv = [], 0
    for tkr, ts_ms, sc, yp, maf in fills:
        cl = close_utc(tkr)
        if cl is None or cl > now - 120:
            continue  # window still open at snapshot end: unsettled
        if tkr in settle:
            stl = settle[tkr]
        else:
            fm = final.get(tkr)
            if fm is None:
                continue
            stl = 1.0 if fm >= 0.5 else 0.0
            conv += 1 if (fm < 0.10 or fm > 0.90) else 0
        t = ts_ms / 1000
        sgn = 1 if sc > 0 else -1
        mo = {}
        for h in HORIZONS:
            m = mid_at(tkr, t + h, cl, stl)
            mo[h] = None if (m is None or maf is None) else sgn * (m - float(maf))
        recs.append({
            "tkr": tkr, "ser": tkr.split("-")[0], "ct": abs(sc),
            "settled": sc * (stl - yp),
            "mo30": mo[30], "mo180": mo[180],
            "min": int((cl - t) // 60),
            "side": "buy" if sc > 0 else "sell",
            "band": band(yp if sc > 0 else 1 - yp),
            "depth": depth_bucket(depth_at(tkr, t, sc > 0)),
        })

    inferred = [r for r in recs if r["tkr"] not in settle]
    print(f"# Pick-off report: {datetime.fromtimestamp(t0, timezone.utc):%Y-%m-%d %H:%M}Z"
          f" -> {datetime.fromtimestamp(now, timezone.utc):%H:%M}Z"
          f"  ({len(recs)} settled fills, {sum(r['ct'] for r in recs):.0f} ct)")
    if inferred:
        print(f"- settle inferred from final mid on {len(inferred)} fills"
              f" (clean convergence {100 * conv / len(inferred):.0f}%)")
    print("- markout = signed post-fill mid drift; negative = picked off;"
          " mo>close uses settlement\n")

    def table(title: str, key, order=None):
        groups: dict[str, list] = {}
        for r in recs:
            groups.setdefault(key(r), []).append(r)
        names = order or sorted(groups, key=lambda k: sum(x["settled"] for x in groups[k]))
        print(f"## {title}")
        print("| bucket | ct | settled $ | c/ct | mo30 c/ct | mo180 c/ct | adv@30 |")
        print("|---|---|---|---|---|---|---|")
        for name in names:
            g = groups.get(name)
            if not g:
                continue
            ct = sum(r["ct"] for r in g)
            st = sum(r["settled"] for r in g)
            m30 = [(r["mo30"], r["ct"]) for r in g if r["mo30"] is not None]
            m180 = [(r["mo180"], r["ct"]) for r in g if r["mo180"] is not None]
            a30 = (100 * sum(c for v, c in m30 if v < 0) / max(sum(c for _, c in m30), 1)
                   if m30 else 0)
            f30 = (100 * sum(v * c for v, c in m30) / max(sum(c for _, c in m30), 1)
                   if m30 else 0)
            f180 = (100 * sum(v * c for v, c in m180) / max(sum(c for _, c in m180), 1)
                    if m180 else 0)
            print(f"| {name} | {ct:.0f} | {st:+.2f} | {100 * st / ct:+.2f}"
                  f" | {f30:+.2f} | {f180:+.2f} | {a30:.0f}% |")
        print()

    table("Overall", lambda r: "all")
    table("By series", lambda r: r["ser"])
    table("By minute of window (13 = first quotable minute under the delay)",
          lambda r: f"m{r['min']:02d}",
          order=[f"m{i:02d}" for i in range(14, -1, -1)])
    table("By side", lambda r: r["side"], order=["buy", "sell"])
    table("By price band of the side taken", lambda r: r["band"],
          order=["0.10-0.35", "0.35-0.65", "0.65-0.90", "0.90-1.00"])
    table("By queue depth at join (others' resting contracts at our level)",
          lambda r: r["depth"], order=["thin <=20", "mid 21-100", "deep >100", "unknown"])
    table("Worst cells (side x band x depth)",
          lambda r: f"{r['side']} {r['band']} {r['depth']}")


if __name__ == "__main__":
    main()
