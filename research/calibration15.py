"""Calibration of Kalshi 15M markets at OUR quoting horizon: for each minute of
window life, is the mid a fair probability? Buckets the mid at each minute,
compares implied vs realized settle frequency. This answers whether joying
(buying/selling) specific price bands has systematic edge — the exact gap the
arXiv 2602.19520 study left open (it excluded >95c/<5c and never went below
the 1-hour horizon). Public API, read-only."""
import json, ssl, urllib.request, time, datetime as dt, collections, math
import certifi
CTX = ssl.create_default_context(cafile=certifi.where())
K = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXBTC15M","KXETH15M","KXSOL15M","KXDOGE15M","KXBNB15M","KXHYPE15M","KXNEAR15M","KXGOLD15M","KXSILVER15M"]
PER_SERIES = 160

def jget(url):
    err=None
    for _ in range(5):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"research/1.0"})
            with urllib.request.urlopen(req,timeout=30,context=CTX) as r: return json.load(r)
        except Exception as e:
            err=e; time.sleep(1.5)
    raise err
def f(x):
    try: return float(x)
    except (TypeError,ValueError): return None
def ts(iso): return int(dt.datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp())

# bucket by BUY-side price p (what a joiner pays): fine bands at the tails
BUCKETS = [(0.00,0.02),(0.02,0.05),(0.05,0.10),(0.10,0.20),(0.20,0.35),(0.35,0.50),
           (0.50,0.65),(0.65,0.80),(0.80,0.90),(0.90,0.95),(0.95,0.98),(0.98,1.00)]
def bidx(p):
    for i,(lo,hi) in enumerate(BUCKETS):
        if lo <= p < hi: return i
    return len(BUCKETS)-1

# agg[phase][bucket] -> [n, sum_implied, sum_outcome]  phase: early(min1-7)/late(min8-14)
agg = {ph: [ [0,0.0,0.0] for _ in BUCKETS] for ph in ("early","late")}
scanned = 0
for s in SERIES:
    mkts, cursor = [], None
    while len(mkts) < PER_SERIES:
        q=f"?series_ticker={s}&status=settled&limit=200"+(f"&cursor={cursor}" if cursor else "")
        d=jget(f"{K}/markets{q}"); got=d.get("markets") or []
        if not got: break
        mkts += [m for m in got if m.get("result") in ("yes","no") and m.get("close_time")]
        cursor=d.get("cursor")
        if not cursor: break
    mkts=mkts[:PER_SERIES]
    for m in mkts:
        tk=m["ticker"]; y=1.0 if m["result"]=="yes" else 0.0
        tclose=ts(m["close_time"]); topen=tclose-900
        try:
            c=jget(f"{K}/series/{s}/markets/{tk}/candlesticks?start_ts={topen}&end_ts={tclose}&period_interval=1")
        except Exception:
            continue
        scanned+=1
        for cs in c.get("candlesticks",[]):
            te=int(cs.get("end_period_ts") or 0); mins=(te-topen)//60
            if not (1<=mins<=14): continue
            b=f((cs.get("yes_bid") or {}).get("close_dollars")); a=f((cs.get("yes_ask") or {}).get("close_dollars"))
            if b is None or a is None or not (0<b<a<1): continue
            ph = "early" if mins<=7 else "late"
            # a JOINER's two trades: buy yes at the bid you'd join... no —
            # joining the BID means you BUY at bid price when filled; joining
            # the ASK means you SELL at ask. Tabulate both as "buy at p":
            # buy yes at b (join bid, filled) and buy no at 1-a (join ask, filled).
            for p, out in ((b, y), (1.0-a, 1.0-y)):
                i=bidx(p); agg[ph][i][0]+=1; agg[ph][i][1]+=p; agg[ph][i][2]+=out
    print(f"{s}: scanned so far {scanned}", flush=True)

print(f"\nmarkets scanned: {scanned}")
for ph in ("early","late"):
    print(f"\n=== {ph.upper()} window (minutes {'1-7' if ph=='early' else '8-14'}) — join a side at price p ===")
    print(f"{'p bucket':>12} {'n':>7} {'implied':>8} {'realized':>9} {'edge c/ct':>10} {'se':>6}")
    for i,(lo,hi) in enumerate(BUCKETS):
        n,si,so=agg[ph][i]
        if n<50: continue
        imp=si/n; real=so/n; edge=(real-imp)*100
        se=math.sqrt(max(real*(1-real),1e-9)/n)*100
        print(f"{lo:5.2f}-{hi:4.2f} {n:7} {imp:8.3f} {real:9.3f} {edge:+10.2f} {se:6.2f}")
json.dump(agg, open("calibration15.json","w"))
print("\n(edge = realized settle freq minus price paid, in cents/contract, for the maker who")
print(" BUYS at that price. Positive in the 0.95-0.99 bucket = favorites underpriced = FLB.)")
