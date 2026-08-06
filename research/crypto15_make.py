"""Would passively quoting the 15-minute markets actually make money?

The census found most 15M crypto books at a 1c spread (half-spread 0.50c vs a
0.47c maker fee = +0.03c, i.e. nothing), but flagged Gold/Silver at 2c and NEAR
wider still. Those are the only arithmetically-positive candidates, so test them.

These markets live 15 minutes, so SETTLEMENT is the markout horizon - no proxy
needed. Realised PnL of a passive fill is exactly (outcome - price) - fee.

Fill model: we join the book at the observed bid/ask, and count a fill when the
NEXT minute trades through our level (price low <= our bid / high >= our ask).
That is deliberately generous on fill probability - we would really be at the
back of the queue - so a negative result here is a floor, not a quibble.

Two numbers per series, and the difference between them is the whole story:
  UNCONDITIONAL  expectancy if filled at a random minute (no selection)
  CONDITIONAL    expectancy given the market actually came to us (real life)
"""
import json, ssl, urllib.request, time, math, statistics, collections
import datetime as dt
import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
K = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXGOLD15M", "KXSILVER15M", "KXNEAR15M", "KXBTC15M", "KXSOL15M", "KXETH15M"]
N_MARKETS = 120
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
            time.sleep(1.4)
    raise err


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ts(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def maker_fee(p):
    return MAKER_RATE * p * (1 - p)


print(f"{'series':12} {'mkts':>5} {'obs':>6} {'medSpr':>7} "
      f"{'--- UNCONDITIONAL ---':>24} {'---- CONDITIONAL (real) ----':>30}")
print(f"{'':12} {'':>5} {'':>6} {'':>7} {'gross':>8}{'fee':>8}{'net':>8} "
      f"{'fills':>7}{'fillrate':>9}{'gross':>8}{'fee':>8}{'NET':>8}")

summary = []
for s in SERIES:
    mkts, cursor = [], None
    while len(mkts) < N_MARKETS:
        q = f"?series_ticker={s}&status=settled&limit=200" + (f"&cursor={cursor}" if cursor else "")
        d = jget(f"{K}/markets{q}")
        got = d.get("markets", [])
        if not got:
            break
        mkts.extend(got)
        cursor = d.get("cursor")
        if not cursor:
            break
    mkts = [m for m in mkts if m.get("result") in ("yes", "no") and m.get("close_time")][:N_MARKETS]
    if not mkts:
        print(f"{s:12} no settled markets")
        continue

    spreads = []
    u_g = u_f = 0.0
    u_n = 0
    c_g = c_f = 0.0
    c_n = 0
    opps = 0
    vols = []
    for m in mkts:
        tk, ct = m["ticker"], m["close_time"]
        t_close = ts(ct)
        t_open = t_close - 900
        y = 1 if m["result"] == "yes" else 0
        vols.append(f(m.get("volume_fp")) or 0.0)
        try:
            c = jget(f"{K}/series/{s}/markets/{tk}/candlesticks"
                     f"?start_ts={t_open}&end_ts={t_close}&period_interval=1")
        except Exception:
            continue
        cands = sorted(c.get("candlesticks", []), key=lambda x: int(x.get("end_period_ts") or 0))
        for i, cs in enumerate(cands[:-1]):
            te = int(cs.get("end_period_ts") or 0)
            elapsed = (te - t_open) // 60
            if not (1 <= elapsed <= 13):
                continue
            b = f((cs.get("yes_bid") or {}).get("close_dollars"))
            a = f((cs.get("yes_ask") or {}).get("close_dollars"))
            if b is None or a is None or not (0 < b < a < 1):
                continue
            nxt = cands[i + 1]
            lo = f((nxt.get("price") or {}).get("low_dollars"))
            hi = f((nxt.get("price") or {}).get("high_dollars"))
            spreads.append(a - b)
            opps += 1
            # unconditional: both sides, no selection
            u_g += (y - b) + (a - y)
            u_f += maker_fee(b) + maker_fee(a)
            u_n += 2
            # conditional: only when the market traded through our level
            if lo is not None and lo <= b:
                c_g += (y - b)
                c_f += maker_fee(b)
                c_n += 1
            if hi is not None and hi >= a:
                c_g += (a - y)
                c_f += maker_fee(a)
                c_n += 1
    if not opps or not u_n:
        print(f"{s:12} no usable observations")
        continue
    msp = statistics.median(spreads)
    ug, uf = u_g / u_n, u_f / u_n
    row = dict(series=s, mkts=len(mkts), obs=opps, med_spread=msp,
               uncond_gross=ug, uncond_fee=uf, uncond_net=ug - uf,
               fills=c_n, fillrate=c_n / (2 * opps),
               cond_gross=(c_g / c_n if c_n else None),
               cond_fee=(c_f / c_n if c_n else None),
               cond_net=((c_g - c_f) / c_n if c_n else None),
               med_vol=statistics.median(vols) if vols else 0)
    summary.append(row)
    cg = f"{row['cond_gross']*100:7.3f}c" if c_n else "     n/a"
    cf = f"{row['cond_fee']*100:7.3f}c" if c_n else "     n/a"
    cn = f"{row['cond_net']*100:+7.3f}c" if c_n else "     n/a"
    print(f"{s:12} {len(mkts):5} {opps:6} {msp*100:6.2f}c "
          f"{ug*100:7.3f}c{uf*100:7.3f}c{(ug-uf)*100:+7.3f}c "
          f"{c_n:7}{row['fillrate']*100:8.1f}%{cg}{cf}{cn}")

print("\nUNCONDITIONAL = quote both sides, filled at random. Measures pure pricing.")
print("CONDITIONAL   = filled only when the market came to our level. Adds adverse")
print("                selection, and is what actually happens. This is the real number.")
print(f"\nAssumed maker rate {MAKER_RATE} x P(1-P) (measured on this account previously;")
print("re-verify on the fly volume before acting - it is the load-bearing input).")

print(f"\n{'series':12} {'medVol/mkt':>12} {'implied contracts/day (96 mkts)':>32}")
for r in sorted(summary, key=lambda r: -(r["cond_net"] or -9)):
    print(f"{r['series']:12} {r['med_vol']:12,.0f} {r['med_vol']*96:32,.0f}")
json.dump(summary, open("crypto15_make.json", "w"), indent=1)
