"""Census of Kalshi's 15-minute up/down markets.

Structure (from rules_primary + series metadata):
  YES iff avg(RTI over 60s before close) >= avg(RTI over 60s before open).
  The target is therefore FIXED AND KNOWN at open, and the contract is a pure
  RETURN bet: "is the price higher than 15 minutes ago". No strike, no basis
  level needed -> fair value is computable from any decent spot feed.

This script answers the maker question, which is pure arithmetic:
  captured half-spread per side  vs  maker fee per side.
If fee > half-spread, quoting loses money with ZERO adverse selection.
"""
import json, ssl, urllib.request, time, statistics, collections, math, datetime as dt
import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
K = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M",
          "KXHYPE15M", "KXNEAR15M", "KXGOLD15M", "KXSILVER15M"]
# empirically measured maker rate on this account (see repo REVIEW notes):
MAKER_RATE = 0.0189
TAKER_RATE = 0.07


def jget(url):
    err = None
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                return json.load(r)
        except Exception as e:
            err = e
            time.sleep(1.2)
    raise err


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


rows = []
tick_evidence = collections.Counter()

for s in SERIES:
    # settled history gives us per-minute books via candlesticks
    d = jget(f"{K}/markets?series_ticker={s}&status=settled&limit=40")
    mkts = d.get("markets", [])
    live = jget(f"{K}/markets?series_ticker={s}&status=open&limit=5").get("markets", [])
    lv = []
    for m in live:
        b, a = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars"))
        if b and a and 0 < b < a < 1:
            lv.append((a - b, b, a, f(m.get("yes_bid_size_fp")), f(m.get("yes_ask_size_fp"))))
    spreads, mids, vols, depths = [], [], [], []
    for m in mkts[:25]:
        tk = m["ticker"]
        ct = m.get("close_time")
        if not ct:
            continue
        end = int(dt.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
        try:
            c = jget(f"{K}/series/{s}/markets/{tk}/candlesticks"
                     f"?start_ts={end - 1080}&end_ts={end}&period_interval=1")
        except Exception:
            continue
        for cs in c.get("candlesticks", []):
            b = f((cs.get("yes_bid") or {}).get("close_dollars"))
            a = f((cs.get("yes_ask") or {}).get("close_dollars"))
            if b is None or a is None or not (0 < b < a < 1):
                continue
            spreads.append(a - b)
            mids.append((a + b) / 2)
            for px in (b, a):
                cents = px * 100
                tick_evidence["sub-cent" if abs(cents - round(cents)) > 1e-6 else "whole-cent"] += 1
        vols.append(f(m.get("volume_fp")) or 0.0)
    if not spreads:
        print(f"{s:12} no data")
        continue
    msp = statistics.median(spreads)
    mmid = statistics.median(mids)
    # fee arithmetic at the typical mid
    maker_fee = MAKER_RATE * mmid * (1 - mmid)
    taker_fee = TAKER_RATE * mmid * (1 - mmid)
    rows.append(dict(series=s, n_obs=len(spreads), med_spread=msp,
                     p25_spread=statistics.quantiles(spreads, n=4)[0],
                     min_spread=min(spreads), med_mid=mmid,
                     med_vol_per_mkt=statistics.median(vols) if vols else 0,
                     maker_fee=maker_fee, taker_fee=taker_fee,
                     half_spread=msp / 2, live_spread=(statistics.median([x[0] for x in lv]) if lv else None),
                     live_depth=(statistics.median([min(x[3] or 0, x[4] or 0) for x in lv]) if lv else None)))

print(f"{'series':12} {'obs':>5} {'medSpr':>8} {'minSpr':>8} {'halfSpr':>8} "
      f"{'makerFee':>9} {'takerFee':>9} {'MAKE?':>7} {'medVol/mkt':>12} {'liveSpr':>8}")
for r in sorted(rows, key=lambda r: -r["med_vol_per_mkt"]):
    edge = r["half_spread"] - r["maker_fee"]
    print(f"{r['series']:12} {r['n_obs']:5} {r['med_spread']*100:7.2f}c {r['min_spread']*100:7.2f}c "
          f"{r['half_spread']*100:7.3f}c {r['maker_fee']*100:8.3f}c {r['taker_fee']*100:8.3f}c "
          f"{edge*100:+6.3f}c {r['med_vol_per_mkt']:12,.0f} "
          f"{(r['live_spread']*100 if r['live_spread'] else float('nan')):7.2f}c")

print(f"\ntick evidence across all observed quotes: {dict(tick_evidence)}")
print("\nMAKE? = half-spread captured minus maker fee, per contract per side.")
print("Negative means quoting loses money before ANY adverse selection.")
json.dump(rows, open("crypto15_census.json", "w"), indent=1, default=str)
