"""Intra-Kalshi structural arbitrage scan (v3 - liquidity-gated + verified).

Bug history (each manufactured fake ~98c arbs):
  v1  butterfly matched rungs via hardcoded `hi+0.01` and round(strike,2) keys
      -> compared a DOGE bucket against a rung two steps away.
  v1  asksum treated mutually_exclusive as exhaustive; a candidate race need
      not cover the outcome space.
  v2  treated a quote of 0.0000 as a real price. On Kalshi an absent offer is
      reported as yes_ask=0.0000 with size 0, so "BUY @0.0000" was a free
      option that does not exist.

v3 rule: a leg is only tradeable if its price is strictly inside (0,1) AND the
side we need has resting size >= MIN_SIZE. Survivors are then re-verified
against the live /orderbook endpoint, because /events snapshots go stale.
"""
import json, ssl, urllib.request, time, re, math, statistics
import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
BASE = "https://api.elections.kalshi.com/trade-api/v2"
CONTRACTS = 100
MIN_SIZE = 10.0     # contracts that must actually rest on the side we hit
TOL = 1e-9


def get(path, params=""):
    err = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(f"{BASE}{path}{params}", timeout=30, context=CTX) as r:
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


def fee_pc(p):
    p = min(max(p or 0.0, 0.0), 1.0)
    return math.ceil(0.07 * CONTRACTS * p * (1 - p) * 100) / 100 / CONTRACTS


def can_buy(m):
    """Is there a real offer we can lift?"""
    return 0.0 < m["ask"] < 1.0 and m["asz"] >= MIN_SIZE


def can_sell(m):
    """Is there a real bid we can hit?"""
    return 0.0 < m["bid"] < 1.0 and m["bsz"] >= MIN_SIZE


NUM = r"\$?([\d,]+(?:\.\d+)?)"


def parse_sub(sub):
    if not sub:
        return None
    s = sub.replace(",", "")
    if re.match(rf"^\s*{NUM}\s*or (?:above|more|higher)\s*$", s, re.I):
        return ("above", float(re.match(rf"^\s*{NUM}", s).group(1)))
    if re.match(rf"^\s*{NUM}\s*or (?:below|less|lower)\s*$", s, re.I):
        return ("below", float(re.match(rf"^\s*{NUM}", s).group(1)))
    m = re.match(rf"^\s*{NUM}\s*(?:to|-)\s*{NUM}\s*$", s, re.I)
    if m:
        return ("range", float(m.group(1)), float(m.group(2)))
    return None


events = []
cursor = None
pages = 0
while pages < 90:
    p = "?status=open&with_nested_markets=true&limit=200" + (f"&cursor={cursor}" if cursor else "")
    d = get("/events", p)
    pages += 1
    for ev in d.get("events", []):
        mk = []
        for m in ev.get("markets") or []:
            if m.get("status") not in ("active", "open"):
                continue
            bid, ask = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars"))
            if bid is None or ask is None:
                continue
            mk.append(dict(t=m["ticker"], sub=m.get("yes_sub_title") or "", bid=bid, ask=ask,
                           bsz=f(m.get("yes_bid_size_fp")) or 0.0,
                           asz=f(m.get("yes_ask_size_fp")) or 0.0,
                           vol=f(m.get("volume_24h_fp")) or 0.0, close=m.get("close_time") or ""))
        if mk:
            events.append(dict(evt=ev["event_ticker"], series=ev.get("series_ticker") or "",
                               cat=ev.get("category") or "", me=bool(ev.get("mutually_exclusive")),
                               title=ev.get("title") or "", mk=mk))
    cursor = d.get("cursor")
    if not cursor:
        break

n_mk = sum(len(e["mk"]) for e in events)
n_two = sum(1 for e in events for m in e["mk"] if can_buy(m) and can_sell(m))
print(f"events={len(events)} markets={n_mk}  with real 2-sided depth>={MIN_SIZE:.0f}: {n_two}\n")

hits = {"ladder": [], "bidsum": [], "asksum": [], "butterfly": []}
near = {"ladder": [], "butterfly": []}     # how CLOSE does the market come?
stats = dict(lad=0, me=0, pairs=0, exh=0, bkt=0)

# ---- 1. ladder monotonicity -------------------------------------------------
for ev in events:
    rungs = []
    for m in ev["mk"]:
        pr = parse_sub(m["sub"])
        if pr and pr[0] == "above":
            rungs.append((pr[1], m))
    rungs.sort(key=lambda r: r[0])
    if len(rungs) < 2:
        continue
    stats["lad"] += 1
    for i in range(len(rungs)):
        k_lo, m_lo = rungs[i]
        if not can_buy(m_lo):
            continue
        for j in range(i + 1, len(rungs)):
            k_hi, m_hi = rungs[j]
            if not can_sell(m_hi):
                continue
            g = m_hi["bid"] - m_lo["ask"]
            near["ladder"].append(g)
            if g <= 0:
                continue
            fee = fee_pc(m_lo["ask"]) + fee_pc(1 - m_hi["bid"])
            hits["ladder"].append(dict(
                kind="ladder", evt=ev["evt"], series=ev["series"], cat=ev["cat"],
                gross=round(g, 4), fee=round(fee, 4), net=round(g - fee, 4),
                size=min(m_lo["asz"], m_hi["bsz"]),
                verify=[(m_lo["t"], "buy", m_lo["ask"]), (m_hi["t"], "sell", m_hi["bid"])],
                legs=[f"BUY  {m_lo['t']} @{m_lo['ask']:.4f} sz{m_lo['asz']:.0f} ({m_lo['sub']})",
                      f"SELL {m_hi['t']} @{m_hi['bid']:.4f} sz{m_hi['bsz']:.0f} ({m_hi['sub']})"],
                close=m_lo["close"]))


# ---- 2/3. partition sums ----------------------------------------------------
def proven_exhaustive(mk):
    parsed = [parse_sub(m["sub"]) for m in mk]
    if any(p is None for p in parsed):
        return False
    lows = [p for p in parsed if p[0] == "below"]
    highs = [p for p in parsed if p[0] == "above"]
    rngs = sorted([p for p in parsed if p[0] == "range"], key=lambda p: p[1])
    if len(lows) != 1 or len(highs) != 1 or not rngs:
        return False
    if len(lows) + len(highs) + len(rngs) != len(mk):
        return False
    prev_hi = lows[0][1]
    for lo, hi in [(r[1], r[2]) for r in rngs]:
        if lo <= prev_hi or (lo - prev_hi) > 0.02 * max(1.0, abs(lo)):
            return False
        prev_hi = hi
    return highs[0][1] > prev_hi and (highs[0][1] - prev_hi) <= 0.02 * max(1.0, abs(prev_hi))


bidsum_gross = []
for ev in events:
    if not ev["me"] or len(ev["mk"]) < 2:
        continue
    stats["me"] += 1
    mk = ev["mk"]
    # sell-every-leg needs a real bid on EVERY leg
    if all(can_sell(m) for m in mk):
        sb = sum(m["bid"] for m in mk)
        bidsum_gross.append(sb - 1)
        if sb > 1.0:
            fee = sum(fee_pc(1 - m["bid"]) for m in mk)
            hits["bidsum"].append(dict(
                kind="bidsum", evt=ev["evt"], series=ev["series"], cat=ev["cat"], n=len(mk),
                sum_bid=round(sb, 4), gross=round(sb - 1, 4), fee=round(fee, 4),
                net=round(sb - 1 - fee, 4), size=min(m["bsz"] for m in mk),
                verify=[(m["t"], "sell", m["bid"]) for m in mk], title=ev["title"][:70]))
    exh = proven_exhaustive(mk)
    if exh:
        stats["exh"] += 1
        if all(can_buy(m) for m in mk):
            sa = sum(m["ask"] for m in mk)
            if sa < 1.0:
                fee = sum(fee_pc(m["ask"]) for m in mk)
                hits["asksum"].append(dict(
                    kind="asksum", evt=ev["evt"], series=ev["series"], cat=ev["cat"], n=len(mk),
                    sum_ask=round(sa, 4), gross=round(1 - sa, 4), fee=round(fee, 4),
                    net=round(1 - sa - fee, 4), size=min(m["asz"] for m in mk),
                    verify=[(m["t"], "buy", m["ask"]) for m in mk], title=ev["title"][:70]))

# ---- 4. cross-book butterfly ------------------------------------------------
by_key = {}
for ev in events:
    suf = ev["evt"].split("-", 1)[1] if "-" in ev["evt"] else ""
    by_key[(ev["series"], suf)] = ev

for (s, suf), ev_thr in list(by_key.items()):
    if not s.endswith("D"):
        continue
    ev_bkt = by_key.get((s[:-1], suf))
    if not ev_bkt:
        continue
    rungs = []
    for m in ev_thr["mk"]:
        pr = parse_sub(m["sub"])
        if pr and pr[0] == "above":
            rungs.append((pr[1], m))
    rungs.sort(key=lambda r: r[0])
    if len(rungs) < 2:
        continue
    stats["pairs"] += 1
    vals = [v for v, _ in rungs]
    for m in ev_bkt["mk"]:
        pr = parse_sub(m["sub"])
        if not pr or pr[0] != "range":
            continue
        lo, hi = pr[1], pr[2]
        i = next((k for k, v in enumerate(vals) if abs(v - lo) < TOL), None)
        if i is None or i + 1 >= len(vals):
            continue
        nxt = vals[i + 1]
        if not (hi < nxt and (nxt - hi) <= 0.51 * (nxt - lo)):
            continue
        stats["bkt"] += 1
        a, b = rungs[i][1], rungs[i + 1][1]
        # SELL bucket / BUY synthetic:  need bkt bid, a ask, b bid
        if can_sell(m) and can_buy(a) and can_sell(b):
            g = m["bid"] - (a["ask"] - b["bid"])
            near["butterfly"].append(g)
            if g > 0:
                lf = fee_pc(a["ask"]) + fee_pc(1 - b["bid"]) + fee_pc(m["bid"])
                hits["butterfly"].append(dict(
                    kind="butterfly", evt=ev_bkt["evt"], series=s, cat=ev_bkt["cat"],
                    gross=round(g, 4), fee=round(lf, 4), net=round(g - lf, 4),
                    size=min(m["bsz"], a["asz"], b["bsz"]),
                    verify=[(m["t"], "sell", m["bid"]), (a["t"], "buy", a["ask"]),
                            (b["t"], "sell", b["bid"])],
                    legs=[f"SELL bkt {m['t']} @{m['bid']:.4f} sz{m['bsz']:.0f} ({m['sub']})",
                          f"BUY  {a['t']} @{a['ask']:.4f} sz{a['asz']:.0f}",
                          f"SELL {b['t']} @{b['bid']:.4f} sz{b['bsz']:.0f}"], close=m["close"]))
        # BUY bucket / SELL synthetic: need bkt ask, a bid, b ask
        if can_buy(m) and can_sell(a) and can_buy(b):
            g = (a["bid"] - b["ask"]) - m["ask"]
            near["butterfly"].append(g)
            if g > 0:
                lf = fee_pc(m["ask"]) + fee_pc(1 - a["bid"]) + fee_pc(b["ask"])
                hits["butterfly"].append(dict(
                    kind="butterfly", evt=ev_bkt["evt"], series=s, cat=ev_bkt["cat"],
                    gross=round(g, 4), fee=round(lf, 4), net=round(g - lf, 4),
                    size=min(m["asz"], a["bsz"], b["asz"]),
                    verify=[(m["t"], "buy", m["ask"]), (a["t"], "sell", a["bid"]),
                            (b["t"], "buy", b["ask"])],
                    legs=[f"BUY  bkt {m['t']} @{m['ask']:.4f} sz{m['asz']:.0f} ({m['sub']})",
                          f"SELL {a['t']} @{a['bid']:.4f} sz{a['bsz']:.0f}",
                          f"BUY  {b['t']} @{b['ask']:.4f} sz{b['asz']:.0f}"], close=m["close"]))

print(f"threshold ladders={stats['lad']}  ME events={stats['me']} "
      f"(proven-exhaustive={stats['exh']})  cross-book pairs={stats['pairs']} "
      f"buckets={stats['bkt']}\n")
for k in ("ladder", "bidsum", "asksum", "butterfly"):
    h = hits[k]
    print(f"  {k:10} gross-violations={len(h):4}  net-of-fee-positive={len([x for x in h if x['net'] > 0]):4}")

print("\n=== HOW CLOSE DOES THE MARKET COME? (gross edge distribution, cents) ===")
for k, v in near.items():
    if not v:
        continue
    v = sorted(v)
    print(f"  {k:10} n={len(v):7}  best={v[-1]*100:7.2f}c  p99={v[int(.99*len(v))]*100:7.2f}c  "
          f"median={statistics.median(v)*100:7.2f}c")
if bidsum_gross:
    v = sorted(bidsum_gross)
    print(f"  {'bidsum':10} n={len(v):7}  best={v[-1]*100:7.2f}c  p99={v[int(.99*len(v))]*100:7.2f}c  "
          f"median={statistics.median(v)*100:7.2f}c")

allh = sorted([x for v in hits.values() for x in v if x["net"] > 0], key=lambda x: -x["net"])
print(f"\n=== {len(allh)} net-positive candidates after liquidity gating ===")
for x in allh[:15]:
    print(f"  net={x['net']*100:6.2f}c gross={x['gross']*100:6.2f}c fee={x['fee']*100:5.2f}c "
          f"size={x['size']:7.0f} {x['kind']:9} {x['series']:14} {x['evt']}")
    for lg in x.get("legs", []):
        print(f"        {lg}")
    if x["kind"] in ("bidsum", "asksum"):
        print(f"        n={x['n']} sum={x.get('sum_bid', x.get('sum_ask'))} {x['title']!r}")
json.dump(allh, open("arb_candidates3.json", "w"), indent=1)

# ---- live re-verification ---------------------------------------------------
if allh:
    print(f"\n=== LIVE ORDERBOOK RE-VERIFICATION of top {min(10, len(allh))} ===")
    for x in allh[:10]:
        ok = True
        detail = []
        for tk, side, px in x["verify"]:
            try:
                ob = get(f"/markets/{tk}/orderbook")
            except Exception as e:
                detail.append(f"{tk}: FETCH FAIL {e}")
                ok = False
                continue
            book = ob.get("orderbook") or {}
            yes = book.get("yes") or []
            no = book.get("no") or []
            # yes levels = bids for YES; no levels = bids for NO (=> yes asks at 1-p)
            best_bid = max((f(l[0]) for l in yes), default=None)
            best_no = max((f(l[0]) for l in no), default=None)
            best_ask = (1 - best_no) if best_no is not None else None
            live = best_bid if side == "sell" else best_ask
            good = live is not None and (
                (side == "sell" and live >= px - 1e-6) or (side == "buy" and live <= px + 1e-6))
            ok = ok and good
            detail.append(f"{tk} need {side}@{px:.4f} live={'None' if live is None else f'{live:.4f}'} "
                          f"{'OK' if good else 'GONE'}")
        print(f"  [{'CONFIRMED' if ok else 'STALE/GONE'}] net={x['net']*100:.2f}c {x['kind']} {x['evt']}")
        for dd in detail:
            print(f"        {dd}")
