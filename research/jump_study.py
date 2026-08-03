"""Separate TREND from JUMPS. For high-flow band markets, pull trades with
timestamps and compute: total range (trend) vs max move within 5 minutes
(jump). A maker can survive trend by requoting; jumps are what pick you off."""
import json, ssl, time, urllib.request, statistics as st
from datetime import datetime
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
band.sort(key=lambda m:-m["vol24"])
# focus on the genuinely liquid part of the band, spread across series
seen=set(); sample=[]
for m in band:
    if m["series"] in seen: continue
    seen.add(m["series"]); sample.append(m)
    if len(sample)>=45: break

def parse(ts):
    try: return datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
    except Exception: return None

out=[]
for m in sample:
    try: d=get(f"/markets/trades?ticker={m['t']}&limit=1000")
    except Exception: continue
    pts=[]
    for t in d.get("trades",[]):
        ts=parse(t.get("created_time") or "")
        try: p=float(t.get("yes_price_dollars") or (float(t.get("yes_price",0))/100))
        except (TypeError,ValueError): continue
        if ts and p>0: pts.append((ts,p))
    if len(pts)<30: continue
    pts.sort()
    span_h=(pts[-1][0]-pts[0][0])/3600
    if span_h<=0: continue
    rng=max(p for _,p in pts)-min(p for _,p in pts)
    # max move inside any 5-minute window = jump proxy
    jump=0.0
    j=0
    for i in range(len(pts)):
        while pts[i][0]-pts[j][0]>300: j+=1
        w=[p for _,p in pts[j:i+1]]
        if w: jump=max(jump, max(w)-min(w))
    out.append(dict(t=m["t"], series=m["series"], cat=m["cat"], spread=m["spread"],
                    vol24=m["vol24"], n=len(pts), span_h=round(span_h,1),
                    trades_per_h=round(len(pts)/span_h,1),
                    range24=round(rng,3), max5min=round(jump,3)))
json.dump(out, open("jumps.json","w"))
print(f"analyzed {len(out)} high-flow band markets")
