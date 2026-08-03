"""For a sample of middle-band markets: trade COUNT (fill opportunity),
median trade SIZE (retail vs institutional), and realized price RANGE
(toxicity). These three decide whether a small maker can live there."""
import json, ssl, time, urllib.request, statistics as st
import certifi
CTX=ssl.create_default_context(cafile=certifi.where())
BASE="https://api.elections.kalshi.com/trade-api/v2"
def get(p):
    for _ in range(3):
        try:
            with urllib.request.urlopen(BASE+p, timeout=30, context=CTX) as r: return json.load(r)
        except Exception as e: time.sleep(0.7); err=e
    raise err

band=json.load(open("band.json"))
# stratified sample: spread across series so one family doesn't dominate
from collections import defaultdict
by_ser=defaultdict(list)
for m in band: by_ser[m["series"]].append(m)
sample=[]
for s,ms in by_ser.items():
    ms.sort(key=lambda x:-x["vol24"])
    sample += ms[:2]          # up to 2 per series
sample.sort(key=lambda x:-x["vol24"])
sample=sample[:90]
print(f"sampling {len(sample)} markets from {len(by_ser)} series\n")

cut=int(time.time())-86400
rows=[]
for i,m in enumerate(sample):
    try:
        d=get(f"/markets/trades?ticker={m['t']}&limit=1000")
    except Exception:
        continue
    tr=[t for t in d.get("trades",[]) if (t.get("created_time") or "")]
    sizes=[]; prices=[]
    for t in d.get("trades",[]):
        try:
            c=float(t.get("count_fp") or t.get("count") or 0)
            p=float(t.get("yes_price_dollars") or (float(t.get("yes_price",0))/100))
        except (TypeError,ValueError): continue
        if c>0: sizes.append(c); prices.append(p)
    if len(sizes)<5: continue
    rows.append(dict(t=m["t"], series=m["series"], cat=m["cat"], spread=m["spread"],
                     mid=m["mid"], vol24=m["vol24"], n_trades=len(sizes),
                     med_size=st.median(sizes), mean_size=sum(sizes)/len(sizes),
                     p_range=round(max(prices)-min(prices),4),
                     small_frac=round(sum(1 for s_ in sizes if s_<=10)/len(sizes),3)))
    if i%15==0: print(f"  ...{i}/{len(sample)}")
json.dump(rows, open("trades.json","w"))
print(f"\ncollected trade stats for {len(rows)} markets")
