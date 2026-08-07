"""Daily review data pull for the fifteen-mode bot. Deterministic part of the
scheduled review routine: pulls OUR fills (private, read-only GETs) and the
markets' settlement results (public), then prints the unsegmented tables the
review needs. The interpreting agent reads this output; it must never need to
re-derive the numbers.

Hard rules encoded here (learned 2026-08-06/07, do not regress):
  * Sign convention: REST /portfolio/fills `action` is already on the
    yes-equivalent (buy=+, sell=-); `side` is which token PRINTED, not our
    direction. Pinned against the bot's own logged fills.
  * Measurement is UNSEGMENTED from a fixed t0. Consecutive "since X" deltas
    each exclude trailing unsettled windows and drop the worst windows
    between the cracks (this artifact produced a fake +0.56c read once).
  * Only GET endpoints. This script must never place, amend, or cancel
    anything, and must never print key material.

Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY (inline PEM, \n-escaped ok) or
KALSHI_PRIVATE_KEY_PATH. Deps: cryptography, certifi (pip install if absent).

Usage: python research/daily_review.py [--hours 26]
Writes research/daily/REVIEW-DATA-<utc date>.md and prints it to stdout.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

CTX = ssl.create_default_context(cafile=certifi.where())
BASE = "https://api.elections.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"
FIFTEEN = re.compile(r"KX[A-Z]+15M-")


def _load_dotenv_fallback() -> None:
    """Local convenience: if the env lacks credentials, read the repo .env the
    way the bot does (line-based k=v; multi-line PEM values joined). No-op
    when the env is already configured (the cloud-routine case)."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists() or os.environ.get("KALSHI_API_KEY_ID"):
        return
    key, buf = None, []
    pairs: dict[str, str] = {}
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            if key is not None:
                pairs[key] = "\n".join(buf)
            key, val = line.split("=", 1)
            key, buf = key.strip(), [val.strip().strip('"').strip("'")]
        elif key is not None:
            buf.append(line.strip().strip('"').strip("'"))
    if key is not None:
        pairs[key] = "\n".join(buf)
    for k, v in pairs.items():
        os.environ.setdefault(k, v)


def _key():
    _load_dotenv_fallback()
    kid = os.environ.get("KALSHI_API_KEY_ID")
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    pth = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not kid or not (pem or pth):
        sys.exit(
            "MISSING CREDENTIALS: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY "
            "(or KALSHI_PRIVATE_KEY_PATH) in the environment. This script only "
            "issues read-only GETs."
        )
    raw = pem.replace("\\n", "\n").encode() if pem else Path(pth).read_bytes()
    return kid, serialization.load_pem_private_key(raw, password=None)


KID, KEY = None, None


def get(path: str, params: str = "", authed: bool = True) -> dict:
    global KID, KEY
    headers = {"Accept": "application/json", "User-Agent": "bacchus-daily-review/1.0"}
    if authed:
        if KEY is None:
            KID, KEY = _key()
        ts = str(int(time.time() * 1000))
        msg = f"{ts}GET{PREFIX}{path}".encode()
        sig = KEY.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        headers.update({
            "KALSHI-ACCESS-KEY": KID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        })
    req = urllib.request.Request(f"{BASE}{path}{params}", headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def close_ts_from_ticker(tk: str):
    # KXBTC15M-26AUG070115-15 -> close 01:15 EDT? No: HHMM in the ticker is
    # EDT market naming; derive from the settled market's close_time instead.
    return None


def build_ledger(all_fills: list, results: dict, ledger_days: int,
                 dest_csv: Path) -> list[dict]:
    """Daily accounting ledger (2026-08-07, owner request): one row per UTC
    day — fills, distinct markets, contracts, $ volume, $ fees, settled
    realized PnL, and cumulative PnL.

    Definitions (documented here because accounting arguments are eternal):
      volume_usd   = sum(contracts x price_paid) — the cash outlay side of
                     every fill (buy yes at p pays p; sell yes at p is buy
                     no, pays 1-p).
      realized_pnl = settled-outcome PnL attributed to the FILL's UTC day
                     (not the settle day); fills whose market has not
                     settled yet are counted in volume/fees but not PnL —
                     the daily rebuild folds them in once they settle.
    Equity is deliberately NOT a ledger column (owner decision 2026-08-07:
    the ledger tracks BOT performance; account equity lives in the daily
    REVIEW files, where the balance-vs-settled-PnL reconciliation check
    reads it).

    Maker vs taker: the bot is post-only, so EVERY fill should be a maker
    fill. The taker_fills and taker_fees_usd columns are TRIPWIRES, not a
    breakdown: any nonzero value is a post-only invariant breach and must be
    treated as an incident. (All fees to date are maker fees from
    Non-Standard-table legacy series; the 15M series charge makers nothing.)

    Contracts can be FRACTIONAL: Kalshi's 15-minute markets trade in
    fractional contracts (counterparties can submit dollar amounts, e.g.
    $5 at 37c = 13.51 contracts), so our whole-contract orders can fill in
    pieces - a 0.4-contract fill is real, and the bot carries the residue
    (see kalshi.py fill handling).

    Idempotent: rows inside the rebuild window are recomputed from the API;
    rows older than the window are preserved verbatim from the existing CSV.
    """
    existing: dict[str, dict] = {}
    header = ["date", "fills", "taker_fills", "markets", "contracts",
              "settled_contracts", "volume_usd", "cum_volume_usd",
              "fees_usd", "cum_fees_usd", "taker_fees_usd",
              "realized_pnl_usd", "cum_realized_pnl_usd"]
    if dest_csv.exists():
        lines = dest_csv.read_text().strip().splitlines()
        for line in lines[1:]:
            parts = line.split(",")
            if parts and parts[0]:
                existing[parts[0]] = dict(zip(header, parts))

    window_start = datetime.now(timezone.utc).timestamp() - ledger_days * 86400
    days: dict[str, dict] = collections.defaultdict(
        lambda: dict(fills=0, taker=0, markets=set(), contracts=0.0,
                     settled_ct=0.0, volume=0.0, fees=0.0, taker_fees=0.0,
                     pnl=0.0))
    for x in all_fills:
        created = x.get("created_time")
        if not created:
            continue
        fts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        day = fts.strftime("%Y-%m-%d")
        ct = f(x.get("count_fp")) or f(x.get("count")) or 0.0
        yp = f(x.get("yes_price_dollars"))
        if yp is None:
            continue
        p_paid = yp if x.get("action") == "buy" else (1.0 - yp)
        d = days[day]
        d["fills"] += 1
        if x.get("is_taker"):
            d["taker"] += 1
            d["taker_fees"] += f(x.get("fee_cost")) or 0.0
        d["markets"].add(x.get("ticker", ""))
        d["contracts"] += abs(ct)
        d["volume"] += abs(ct) * p_paid
        d["fees"] += f(x.get("fee_cost")) or 0.0
        res = results.get(x.get("ticker", ""))
        if res in ("yes", "no"):
            signed = ct if x.get("action") == "buy" else -ct
            settle = 1.0 if res == "yes" else 0.0
            d["pnl"] += signed * (settle - yp) - (f(x.get("fee_cost")) or 0.0)
            d["settled_ct"] += abs(ct)

    rows: dict[str, dict] = {}
    for day, old in existing.items():
        # preserve rows older than the rebuild window verbatim
        try:
            day_ts = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if day_ts < window_start:
            rows[day] = old
    for day, d in days.items():
        rows[day] = {
            "date": day, "fills": str(d["fills"]),
            "taker_fills": str(d["taker"]),
            "markets": str(len(d["markets"])),
            "contracts": f"{d['contracts']:.1f}",
            "settled_contracts": f"{d['settled_ct']:.1f}",
            "volume_usd": f"{d['volume']:.2f}",
            "cum_volume_usd": "",  # filled below
            "fees_usd": f"{d['fees']:.4f}",
            "cum_fees_usd": "",  # filled below
            "taker_fees_usd": f"{d['taker_fees']:.4f}",
            "realized_pnl_usd": f"{d['pnl']:.2f}",
            "cum_realized_pnl_usd": "",  # filled below
        }
    cum = cum_vol = cum_fees = 0.0
    ordered = [rows[k] for k in sorted(rows)]
    for r in ordered:
        cum += float(r["realized_pnl_usd"] or 0)
        cum_vol += float(r["volume_usd"] or 0)
        cum_fees += float(r["fees_usd"] or 0)
        r["cum_realized_pnl_usd"] = f"{cum:.2f}"
        r["cum_volume_usd"] = f"{cum_vol:.2f}"
        r["cum_fees_usd"] = f"{cum_fees:.4f}"
    dest_csv.parent.mkdir(parents=True, exist_ok=True)
    dest_csv.write_text(
        ",".join(header) + "\n"
        + "\n".join(",".join(r[h] for h in header) for r in ordered) + "\n"
    )
    return ordered


def render_ledger_md(ordered: list[dict], dest_md: Path, tail: int = 30) -> None:
    out = ["# Daily ledger", "",
           "Definitions in research/daily_review.py build_ledger(). Realized",
           "PnL is settled-only, attributed to the fill's UTC day; the daily",
           "rebuild folds late settlements in. Full history: LEDGER.csv.",
           "Note: contracts can be fractional - Kalshi's 15-minute markets",
           "let counterparties trade dollar amounts (e.g. $5 at 37c = 13.51",
           "contracts), so whole-contract orders fill in pieces.", "",
           "All fills should be MAKER fills (post-only bot): the taker",
           "columns are tripwires, and any nonzero value is an invariant",
           "breach to treat as an incident.", "",
           "| date | fills | taker | markets | contracts | volume $ | cum volume $ | fees $ | cum fees $ | taker fees $ | pnl $ | cum pnl $ |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in ordered[-tail:]:
        out.append(
            f"| {r['date']} | {r['fills']} | {r['taker_fills']} | {r['markets']} "
            f"| {r['contracts']} "
            f"| {float(r['volume_usd']):,.2f} | {float(r['cum_volume_usd']):,.2f} "
            f"| {r['fees_usd']} | {float(r['cum_fees_usd']):.4f} "
            f"| {r['taker_fees_usd']} "
            f"| {float(r['realized_pnl_usd']):+.2f} | {float(r['cum_realized_pnl_usd']):+.2f} |")
    out.append("")
    dest_md.write_text("\n".join(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=26.0)
    ap.add_argument("--ledger-days", type=int, default=45,
                    help="rebuild window for the daily ledger (older rows preserved)")
    args = ap.parse_args()

    now = time.time()
    t0 = int(now - args.hours * 3600)
    utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ---- fills (private, GET only). One pull covers BOTH the review window
    # and the ledger rebuild window; the review tables filter down to 15M
    # fills inside --hours, the ledger uses every account fill in the window.
    pull_start = int(min(t0, now - args.ledger_days * 86400))
    all_fills, cursor = [], None
    while True:
        d = get("/portfolio/fills", f"?limit=200&min_ts={pull_start}"
                + (f"&cursor={cursor}" if cursor else ""))
        got = d.get("fills") or []
        all_fills.extend(got)
        cursor = d.get("cursor")
        if not cursor or not got:
            break
    fills = [x for x in all_fills
             if FIFTEEN.match(x.get("ticker", ""))
             and (lambda c: c and datetime.fromisoformat(
                 c.replace("Z", "+00:00")).timestamp() >= t0)(x.get("created_time"))]

    # ---- balance (ground truth anchor)
    bal = get("/portfolio/balance")
    balance = f(bal.get("balance_dollars"))
    portfolio_cents = f(bal.get("portfolio_value")) or 0.0

    # ---- settlement results + close times for involved tickers (public)
    tickers = sorted({x["ticker"] for x in all_fills})
    results: dict[str, str] = {}
    closes: dict[str, float] = {}
    for series in sorted({t.split("-")[0] for t in tickers}):
        cur = None
        want = {t for t in tickers if t.startswith(series)}
        for _ in range(30):
            d = get("/markets", f"?series_ticker={series}&status=settled&limit=200"
                    + (f"&cursor={cur}" if cur else ""), authed=False)
            ms = d.get("markets") or []
            for m in ms:
                if m["ticker"] in want:
                    results[m["ticker"]] = m.get("result")
                    ct = m.get("close_time")
                    if ct:
                        closes[m["ticker"]] = datetime.fromisoformat(
                            ct.replace("Z", "+00:00")
                        ).timestamp()
            cur = d.get("cursor")
            if not cur or not ms or want <= set(results):
                break

    # ---- aggregate: unsegmented, settled windows only
    per_series = collections.defaultdict(lambda: dict(ct=0.0, pnl=0.0, fills=0))
    per_window = collections.defaultdict(lambda: dict(ct=0.0, pnl=0.0, fills=0))
    by_min = collections.defaultdict(lambda: dict(ct=0.0, pnl=0.0))
    by_band = collections.defaultdict(lambda: dict(ct=0.0, pnl=0.0))
    taker_fills = 0
    total_fees = 0.0
    unsettled = set()
    tot_ct = tot_pnl = 0.0

    bands = [(0.0, 0.1), (0.1, 0.35), (0.35, 0.65), (0.65, 0.9), (0.9, 1.0)]

    for x in fills:
        tk = x["ticker"]
        res = results.get(tk)
        ct = f(x.get("count_fp")) or f(x.get("count")) or 0.0
        yp = f(x.get("yes_price_dollars"))
        fee = f(x.get("fee_cost")) or 0.0
        if x.get("is_taker"):
            taker_fills += 1
        total_fees += fee
        if res not in ("yes", "no"):
            unsettled.add(tk)
            continue
        # VERIFIED convention: action is yes-equivalent direction.
        signed = ct if x.get("action") == "buy" else -ct
        settle = 1.0 if res == "yes" else 0.0
        pnl = signed * (settle - yp) - fee
        series = tk.split("-")[0]
        per_series[series]["ct"] += abs(ct)
        per_series[series]["pnl"] += pnl
        per_series[series]["fills"] += 1
        per_window[tk]["ct"] += abs(ct)
        per_window[tk]["pnl"] += pnl
        per_window[tk]["fills"] += 1
        tot_ct += abs(ct)
        tot_pnl += pnl
        cts = closes.get(tk)
        created = x.get("created_time")
        if cts and created:
            fts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            m = max(0, min(15, int((cts - fts) / 60)))
            by_min[m]["ct"] += abs(ct)
            by_min[m]["pnl"] += pnl
        # price band of the BUY side we took (yes at yp if buy, no at 1-yp if sell)
        p_paid = yp if x.get("action") == "buy" else (1.0 - yp)
        for lo, hi in bands:
            if lo <= p_paid < hi or (hi == 1.0 and p_paid == 1.0):
                by_band[(lo, hi)]["ct"] += abs(ct)
                by_band[(lo, hi)]["pnl"] += pnl
                break

    # ---- daily accounting ledger (2026-08-07): full-account time series.
    ledger_rows = build_ledger(
        all_fills, results, args.ledger_days,
        Path(__file__).resolve().parent / "daily" / "LEDGER.csv",
    )
    render_ledger_md(ledger_rows,
                     Path(__file__).resolve().parent / "daily" / "LEDGER.md")

    # ---- 15M series discovery (public; 2026-08-07): Kalshi expands this
    # family fast (ZEC/BCH/TON/XRP/ADA and two crypto indexes appeared within
    # 48h of launch week). Flag anything new vs the committed state file so
    # the review can propose watch items; the routine never auto-adds.
    known_path = Path(__file__).resolve().parent / "daily" / "known_15m_series.json"
    known = set()
    if known_path.exists():
        try:
            known = set(json.loads(known_path.read_text()))
        except (ValueError, OSError):
            known = set()
    live_15m: dict[str, str] = {}
    for cat in ("Crypto", "Commodities", "Financials", "Economics",
                "Climate and Weather", "Indices"):
        try:
            d = get("/series", f"?category={cat.replace(' ', '%20')}", authed=False)
        except Exception:  # noqa: BLE001 — discovery is best-effort
            continue
        for s in d.get("series") or []:
            if s.get("frequency") == "fifteen_min":
                live_15m[s["ticker"]] = s.get("title") or ""
    new_series = sorted(set(live_15m) - known) if known else []
    known_path.parent.mkdir(parents=True, exist_ok=True)
    known_path.write_text(json.dumps(sorted(set(live_15m) | known), indent=0))

    # ---- render
    out = []
    out.append(f"# Daily review data: {utc_date} (last {args.hours:.0f}h, unsegmented)")
    out.append("")
    out.append(f"- generated: {datetime.now(timezone.utc).isoformat()}")
    out.append(f"- account balance: ${balance:.2f} (+ ${portfolio_cents/100:.2f} in positions)")
    out.append(f"- fifteen fills in window: {len(fills)}; settled contracts: {tot_ct:.1f}")
    out.append(f"- taker fills: {taker_fills} (MUST be 0; post-only); "
               f"total fees: ${total_fees:.4f} (should be ~0 on 15M series)")
    if unsettled:
        out.append(f"- unsettled tickers EXCLUDED from PnL: {len(unsettled)} "
                   f"(re-run later includes them; never diff two runs)")
    out.append("")
    cpc = (tot_pnl / tot_ct * 100) if tot_ct else 0.0
    se = (math.sqrt(tot_ct) * 0.5 / max(tot_ct, 1)) * 100  # rough binomial-ish scale
    out.append(f"## TOTAL REALIZED vs SETTLEMENT: ${tot_pnl:+.2f}  ({cpc:+.2f}c/contract, "
               f"~se {se:.2f}c)")
    out.append("")
    out.append("## By series")
    out.append("")
    out.append("| series | fills | contracts | pnl $ | c/ct |")
    out.append("|---|---|---|---|---|")
    for s, a in sorted(per_series.items(), key=lambda kv: kv[1]["pnl"]):
        out.append(f"| {s} | {a['fills']} | {a['ct']:.1f} | {a['pnl']:+.2f} | "
                   f"{a['pnl']/max(a['ct'],1)*100:+.2f} |")
    out.append("")
    out.append("## By minutes-to-close at fill")
    out.append("")
    out.append("| min_to_close | contracts | pnl $ | c/ct |")
    out.append("|---|---|---|---|")
    for m in sorted(by_min, reverse=True):
        a = by_min[m]
        out.append(f"| {m} | {a['ct']:.1f} | {a['pnl']:+.2f} | "
                   f"{a['pnl']/max(a['ct'],1)*100:+.2f} |")
    out.append("")
    out.append("## By price band of the side taken (tilt evidence)")
    out.append("")
    out.append("| band | contracts | pnl $ | c/ct |")
    out.append("|---|---|---|---|")
    for (lo, hi), a in sorted(by_band.items()):
        out.append(f"| {lo:.2f}-{hi:.2f} | {a['ct']:.1f} | {a['pnl']:+.2f} | "
                   f"{a['pnl']/max(a['ct'],1)*100:+.2f} |")
    out.append("")
    out.append("## 15M series discovery")
    out.append("")
    if not known:
        out.append(f"- state file seeded with {len(live_15m)} known series "
                   f"(first run); new listings flagged from tomorrow")
    elif new_series:
        for t in new_series:
            out.append(f"- **NEW SERIES: {t}** ({live_15m.get(t, '')}) - "
                       f"candidate watch item, never auto-added")
    else:
        out.append(f"- no new 15M series ({len(live_15m)} known)")
    out.append("")
    out.append("## Worst and best windows")
    out.append("")
    out.append("| window | fills | contracts | pnl $ | c/ct |")
    out.append("|---|---|---|---|---|")
    ranked = sorted(per_window.items(), key=lambda kv: kv[1]["pnl"])
    for tk, a in ranked[:8] + ranked[-4:]:
        out.append(f"| {tk} | {a['fills']} | {a['ct']:.1f} | {a['pnl']:+.2f} | "
                   f"{a['pnl']/max(a['ct'],1)*100:+.2f} |")
    text = "\n".join(out) + "\n"

    dest = Path(__file__).resolve().parent / "daily" / f"REVIEW-DATA-{utc_date}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    print(text)
    print(f"[written to {dest}]", file=sys.stderr)


if __name__ == "__main__":
    main()
