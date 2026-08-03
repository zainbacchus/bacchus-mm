"""Market-structure study: where does a small maker have a place?
Pull every open Kalshi market with volume + spread, no auth needed."""
import json, ssl, urllib.request, time
import certifi
CTX=ssl.create_default_context(cafile=certifi.where())

BASE="https://api.elections.kalshi.com/trade-api/v2"
def get(path, params=""):
    url=f"{BASE}{path}{params}"
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30, context=CTX) as r: return json.load(r)
        except Exception as e:
            time.sleep(1); err=e
    raise err

out=[]; cursor=None; pages=0
while pages<60:
    p=f"?status=open&with_nested_markets=true&limit=200"+(f"&cursor={cursor}" if cursor else "")
    d=get("/events", p); pages+=1
    for ev in d.get("events",[]):
        cat=ev.get("category") or ""
        for m in ev.get("markets") or []:
            if m.get("status") not in ("active","open"): continue
            try:
                bid=float(m.get("yes_bid_dollars") or 0); ask=float(m.get("yes_ask_dollars") or 0)
                vol=float(m.get("volume_24h_fp") or 0); oi=float(m.get("open_interest_fp") or 0)
            except (TypeError,ValueError): continue
            if bid<=0 or ask<=0: continue
            out.append(dict(t=m["ticker"], series=(ev.get("series_ticker") or m["ticker"].split("-")[0]),
                            cat=cat, bid=bid, ask=ask, spread=round(ask-bid,4),
                            mid=round((bid+ask)/2,4), vol24=vol, oi=oi,
                            close=m.get("close_time","")))
    cursor=d.get("cursor")
    if not cursor: break
json.dump(out, open("universe.json","w"))
print(f"pages={pages}  open markets with 2-sided books: {len(out)}")
