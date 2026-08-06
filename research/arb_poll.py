"""Repeated-sampling arb frequency bound: poll the ladder/cross-book families
(the only places v3 found ANY gross violation) and count how often a
net-of-fee-positive structural arb exists. One snapshot cannot see bursts."""
import json, ssl, urllib.request, time, re, math, datetime
import certifi
CTX=ssl.create_default_context(cafile=certifi.where())
BASE="https://api.elections.kalshi.com/trade-api/v2"
CONTRACTS=100; MIN_SIZE=10.0; TOL=1e-9
FAMS=["KXBTCD","KXBTC","KXETHD","KXETH","KXSOLD","KXSOL","KXDOGED","KXDOGE",
      "KXBNBD","KXBNB","KXHYPED","KXHYPE","KXXRPD","KXXRP"]
def get(p,q=""):
    e=None
    for _ in range(3):
        try:
            with urllib.request.urlopen(f"{BASE}{p}{q}",timeout=25,context=CTX) as r: return json.load(r)
        except Exception as ex: e=ex; time.sleep(1)
    raise e
def f(x):
    try: return float(x)
    except: return None
def fee_pc(p):
    p=min(max(p or 0,0),1); return math.ceil(0.07*CONTRACTS*p*(1-p)*100)/100/CONTRACTS
NUM=r"\$?([\d,]+(?:\.\d+)?)"
def psub(s):
    if not s: return None
    s=s.replace(",","")
    if re.match(rf"^\s*{NUM}\s*or (?:above|more|higher)\s*$",s,re.I): return ("above",float(re.match(rf"^\s*{NUM}",s).group(1)))
    if re.match(rf"^\s*{NUM}\s*or (?:below|less|lower)\s*$",s,re.I): return ("below",float(re.match(rf"^\s*{NUM}",s).group(1)))
    m=re.match(rf"^\s*{NUM}\s*(?:to|-)\s*{NUM}\s*$",s,re.I)
    return ("range",float(m.group(1)),float(m.group(2))) if m else None
def cb(m): return 0<m["ask"]<1 and m["asz"]>=MIN_SIZE
def cs(m): return 0<m["bid"]<1 and m["bsz"]>=MIN_SIZE

rounds=0; best_hist=[]; netpos=0; grosspos=0
T_END=time.time()+45*60
while time.time()<T_END:
    evs={}
    for fam in FAMS:
        try: d=get("/events",f"?series_ticker={fam}&status=open&with_nested_markets=true")
        except Exception: continue
        for ev in d.get("events",[]):
            mk=[]
            for m in ev.get("markets") or []:
                if m.get("status") not in ("active","open"): continue
                b,a=f(m.get("yes_bid_dollars")),f(m.get("yes_ask_dollars"))
                if b is None or a is None: continue
                mk.append(dict(t=m["ticker"],sub=m.get("yes_sub_title") or "",bid=b,ask=a,
                               bsz=f(m.get("yes_bid_size_fp")) or 0,asz=f(m.get("yes_ask_size_fp")) or 0))
            if mk: evs[(ev.get("series_ticker"),ev["event_ticker"].split("-",1)[1])]=mk
    rounds+=1; best_net=-9; best_gross=-9
    # ladder monotonicity
    for (s,suf),mk in evs.items():
        rg=sorted([(p[1],m) for m in mk if (p:=psub(m["sub"])) and p[0]=="above"],key=lambda r:r[0])
        for i in range(len(rg)):
            if not cb(rg[i][1]): continue
            for j in range(i+1,len(rg)):
                if not cs(rg[j][1]): continue
                g=rg[j][1]["bid"]-rg[i][1]["ask"]
                n=g-(fee_pc(rg[i][1]["ask"])+fee_pc(1-rg[j][1]["bid"]))
                best_gross=max(best_gross,g); best_net=max(best_net,n)
    # cross-book butterfly
    for (s,suf),mkT in evs.items():
        if not s or not s.endswith("D"): continue
        mkB=evs.get((s[:-1],suf))
        if not mkB: continue
        rg=sorted([(p[1],m) for m in mkT if (p:=psub(m["sub"])) and p[0]=="above"],key=lambda r:r[0])
        vals=[v for v,_ in rg]
        for m in mkB:
            p=psub(m["sub"])
            if not p or p[0]!="range": continue
            lo,hi=p[1],p[2]
            i=next((k for k,v in enumerate(vals) if abs(v-lo)<TOL),None)
            if i is None or i+1>=len(vals): continue
            nxt=vals[i+1]
            if not (hi<nxt and (nxt-hi)<=0.51*(nxt-lo)): continue
            a,b=rg[i][1],rg[i+1][1]
            if cs(m) and cb(a) and cs(b):
                g=m["bid"]-(a["ask"]-b["bid"]); n=g-(fee_pc(a["ask"])+fee_pc(1-b["bid"])+fee_pc(m["bid"]))
                best_gross=max(best_gross,g); best_net=max(best_net,n)
            if cb(m) and cs(a) and cb(b):
                g=(a["bid"]-b["ask"])-m["ask"]; n=g-(fee_pc(m["ask"])+fee_pc(1-a["bid"])+fee_pc(b["ask"]))
                best_gross=max(best_gross,g); best_net=max(best_net,n)
    if best_gross>0: grosspos+=1
    if best_net>0: netpos+=1
    best_hist.append((best_gross,best_net))
    print(f"[{datetime.datetime.now():%H:%M:%S}] round {rounds:3} events={len(evs):3} "
          f"best_gross={best_gross*100:6.2f}c best_net={best_net*100:6.2f}c "
          f"| rounds_with_gross={grosspos} rounds_with_NET={netpos}", flush=True)
    time.sleep(25)
json.dump(dict(rounds=rounds,grosspos=grosspos,netpos=netpos,hist=best_hist),open("arb_poll.json","w"))
print(f"\nDONE rounds={rounds} rounds_with_gross_violation={grosspos} rounds_with_NET_POSITIVE={netpos}")
