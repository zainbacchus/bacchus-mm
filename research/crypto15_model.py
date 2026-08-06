"""Can a spot-driven model beat Kalshi's mid on the 15-minute up/down markets?

The contract: YES iff avg(RTI, 60s before close) >= avg(RTI, 60s before open).
So it is a pure RETURN bet, which makes it basis-free: I do not need CF
Benchmarks' BRTI level, only a return from any decent spot feed (Coinbase).

Fair value at t, with tau minutes left and r = ln(S_t / S_open_ref):
    P = Phi( r / (sigma * sqrt(tau)) )
(the -0.5*sigma^2*tau drift term is ~1e-6 at this horizon; dropped)

Three questions, in the only order that avoids fooling myself:
  Q1 VALIDATE the proxy: does sign(Coinbase 15m return) match how Kalshi
     actually settled? If not, Coinbase is the wrong feed and nothing else
     in this file means anything.
  Q2 SCORE the model against the market: Brier score of model vs Kalshi mid
     vs the realised outcome. If the model cannot beat the mid, there is no
     informed quoting or taking to do, full stop.
  Q3 SIZE the edge vs the fee, bucketed by minute of life (the "first 80-90%"
     question): is |model - mid| ever bigger than the taker fee?
"""
import json, ssl, urllib.request, time, math, statistics, collections
import datetime as dt
import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
K = "https://api.elections.kalshi.com/trade-api/v2"
CB = "https://api.exchange.coinbase.com"
SERIES = "KXBTC15M"
PRODUCT = "BTC-USD"
N_MARKETS = 260
TAKER_RATE = 0.07
MAKER_RATE = 0.0189


def jget(url):
    err = None
    for _ in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                return json.load(r)
        except Exception as e:
            err = e
            time.sleep(1.5)
    raise err


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def ts(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# ---------------------------------------------------- 1. settled 15M markets
mkts, cursor = [], None
while len(mkts) < N_MARKETS:
    q = f"?series_ticker={SERIES}&status=settled&limit=200" + (f"&cursor={cursor}" if cursor else "")
    d = jget(f"{K}/markets{q}")
    got = d.get("markets", [])
    if not got:
        break
    mkts.extend(got)
    cursor = d.get("cursor")
    if not cursor:
        break
mkts = [m for m in mkts if m.get("result") in ("yes", "no") and m.get("close_time")][:N_MARKETS]
mkts.sort(key=lambda m: m["close_time"])
print(f"settled {SERIES} markets: {len(mkts)}  "
      f"span {mkts[0]['close_time']} .. {mkts[-1]['close_time']}")

# ---------------------------------------------------- 2. Coinbase 1m candles
lo = ts(mkts[0]["close_time"]) - 4200      # + an hour of warmup for sigma
hi = ts(mkts[-1]["close_time"]) + 120
spot = {}
cur = lo
while cur < hi:
    end = min(cur + 300 * 60, hi)
    s_iso = dt.datetime.fromtimestamp(cur, dt.UTC).isoformat().replace("+00:00", "Z")
    e_iso = dt.datetime.fromtimestamp(end, dt.UTC).isoformat().replace("+00:00", "Z")
    try:
        c = jget(f"{CB}/products/{PRODUCT}/candles?granularity=60&start={s_iso}&end={e_iso}")
        for t, low, high, op, cl, vol in c:
            spot[int(t) + 60] = dict(c=float(cl), avg=(float(low) + float(high)) / 2)
    except Exception as e:
        print("  coinbase chunk failed", e)
    cur = end
    time.sleep(0.28)
print(f"coinbase 1m candles: {len(spot)}  (keyed by candle END ts)")


def sigma_before(t0, n=60):
    """per-minute log-return stdev over the n minutes ending at t0"""
    px = [spot[t0 - 60 * i]["c"] for i in range(n, -1, -1) if (t0 - 60 * i) in spot]
    if len(px) < 20:
        return None
    rets = [math.log(px[i + 1] / px[i]) for i in range(len(px) - 1) if px[i] > 0]
    if len(rets) < 15:
        return None
    s = statistics.pstdev(rets)
    return s if s > 1e-7 else None


# ---------------------------------------------------- 3. per-market analysis
q1_match = q1_total = 0
by_min = collections.defaultdict(lambda: dict(n=0, se_model=0.0, se_mid=0.0,
                                              absdiff=[], diff=[], spread=[]))
rows = []
skipped = collections.Counter()

for m in mkts:
    tk, ct = m["ticker"], m["close_time"]
    t_close = ts(ct)
    t_open = t_close - 900
    y = 1 if m["result"] == "yes" else 0
    ref = spot.get(t_open)
    fin = spot.get(t_close)
    if not ref or not fin:
        skipped["no_spot"] += 1
        continue
    # Q1: does the Coinbase-implied outcome match how Kalshi settled?
    q1_total += 1
    if (1 if fin["avg"] >= ref["avg"] else 0) == y:
        q1_match += 1
    sig = sigma_before(t_open)
    if not sig:
        skipped["no_sigma"] += 1
        continue
    try:
        c = jget(f"{K}/series/{SERIES}/markets/{tk}/candlesticks"
                 f"?start_ts={t_open}&end_ts={t_close}&period_interval=1")
    except Exception:
        skipped["no_candles"] += 1
        continue
    for cs in c.get("candlesticks", []):
        te = int(cs.get("end_period_ts") or 0)
        elapsed = (te - t_open) // 60
        if not (1 <= elapsed <= 14):
            continue
        b = f((cs.get("yes_bid") or {}).get("close_dollars"))
        a = f((cs.get("yes_ask") or {}).get("close_dollars"))
        sp = spot.get(te)
        if b is None or a is None or not (0 < b < a < 1) or not sp:
            continue
        mid = (a + b) / 2
        tau = (15 - elapsed) - 0.5          # 60s settlement average
        if tau <= 0:
            continue
        r = math.log(sp["c"] / ref["avg"])
        pm = ncdf(r / (sig * math.sqrt(tau)))
        pm = min(max(pm, 0.0005), 0.9995)
        d = by_min[elapsed]
        d["n"] += 1
        d["se_model"] += (pm - y) ** 2
        d["se_mid"] += (mid - y) ** 2
        d["diff"].append(pm - mid)
        d["absdiff"].append(abs(pm - mid))
        d["spread"].append(a - b)
        rows.append(dict(tk=tk, elapsed=elapsed, mid=mid, model=pm, y=y,
                         spread=a - b, bid=b, ask=a))

print(f"\nQ1 PROXY VALIDATION: Coinbase-implied outcome matched Kalshi settlement "
      f"in {q1_match}/{q1_total} = {100*q1_match/max(q1_total,1):.1f}% of markets")
print(f"   skipped: {dict(skipped)}   usable (minute, market) observations: {len(rows)}")

print(f"\nQ2/Q3 BY MINUTE OF LIFE  (tau = 15 - minute)")
print(f"{'min':>4} {'n':>6} {'Brier_model':>12} {'Brier_mid':>11} {'winner':>8} "
      f"{'med|model-mid|':>15} {'med_spread':>11} {'takerfee@mid':>13}")
tot = dict(n=0, sm=0.0, sd=0.0)
for k in sorted(by_min):
    d = by_min[k]
    if d["n"] < 20:
        continue
    bm, bd = d["se_model"] / d["n"], d["se_mid"] / d["n"]
    tot["n"] += d["n"]; tot["sm"] += d["se_model"]; tot["sd"] += d["se_mid"]
    mad = statistics.median(d["absdiff"])
    msp = statistics.median(d["spread"])
    # taker fee per contract at the median mid of this bucket
    mm = statistics.median([r["mid"] for r in rows if r["elapsed"] == k])
    tf = TAKER_RATE * mm * (1 - mm)
    print(f"{k:4} {d['n']:6} {bm:12.4f} {bd:11.4f} {'MODEL' if bm < bd else 'market':>8} "
          f"{mad*100:14.2f}c {msp*100:10.2f}c {tf*100:12.2f}c")
if tot["n"]:
    print(f"\nOVERALL Brier: model={tot['sm']/tot['n']:.4f}  market_mid={tot['sd']/tot['n']:.4f}  "
          f"-> {'MODEL BEATS MARKET' if tot['sm'] < tot['sd'] else 'MARKET BEATS MODEL'}")
    print(f"   (Brier is mean squared error vs outcome; lower is better. "
          f"A coin flip at 0.5 scores 0.25.)")

# ---- Q3b: would taking on the model signal have made money, net of fee? ----
print(f"\nQ3b TAKE-THE-EDGE SIMULATION (cross the spread when model disagrees)")
print(f"{'thresh':>7} {'trades':>7} {'gross_c/ct':>11} {'fee_c/ct':>9} {'NET_c/ct':>9} {'hit%':>6}")
for thresh in (0.01, 0.02, 0.03, 0.05, 0.08, 0.12):
    g = fe = 0.0
    n = win = 0
    for r in rows:
        if r["model"] - r["ask"] > thresh:          # model says cheap -> buy at ask
            px, sgn = r["ask"], 1
        elif r["bid"] - r["model"] > thresh:        # model says rich -> sell at bid
            px, sgn = r["bid"], -1
        else:
            continue
        n += 1
        pnl = sgn * (r["y"] - px)
        g += pnl
        fe += TAKER_RATE * px * (1 - px)
        win += 1 if pnl > 0 else 0
    if n:
        print(f"{thresh*100:6.0f}c {n:7} {g/n*100:10.3f}c {fe/n*100:8.3f}c "
              f"{(g-fe)/n*100:8.3f}c {100*win/n:5.1f}%")
    else:
        print(f"{thresh*100:6.0f}c {0:7}  (no trades)")

json.dump(rows, open("crypto15_model_rows.json", "w"))
print(f"\nwrote {len(rows)} observations to crypto15_model_rows.json")
