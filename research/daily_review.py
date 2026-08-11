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

    Maker vs taker: bot fills are 100% maker by construction (post-only);
    the daily review output asserts taker == 0 on every run, so the ledger
    does not carry taker columns (owner decision 2026-08-07). The single
    lifetime taker fill (2026-08-06, KXECONSTATCPIYOY) was attributed to a
    manual owner trade from the Kalshi app during that night's halt; the
    full forensics live in the git history.

    Gross vs net: gross_pnl_usd is settled-outcome PnL before fees (the
    "spread captured" number for a maker); net_pnl_usd = gross minus the
    day's TOTAL fees (fees are certain when paid, so they are all charged
    to net even when some fills have not settled yet).

    Contracts can be FRACTIONAL: Kalshi's 15-minute markets trade in
    fractional contracts (counterparties can submit dollar amounts, e.g.
    $5 at 37c = 13.51 contracts), so our whole-contract orders can fill in
    pieces - a 0.4-contract fill is real, and the bot carries the residue
    (see kalshi.py fill handling).

    Idempotent: rows inside the rebuild window are recomputed from the API;
    rows older than the window are preserved verbatim from the existing CSV.
    """
    existing: dict[str, dict] = {}
    header = ["date", "fills", "markets", "contracts", "settled_contracts",
              "volume_usd", "cum_volume_usd", "fees_usd", "cum_fees_usd",
              "gross_pnl_usd", "net_pnl_usd", "cum_net_pnl_usd"]
    if dest_csv.exists():
        lines = dest_csv.read_text().strip().splitlines()
        for line in lines[1:]:
            parts = line.split(",")
            if parts and parts[0]:
                existing[parts[0]] = dict(zip(header, parts))

    window_start = datetime.now(timezone.utc).timestamp() - ledger_days * 86400
    days: dict[str, dict] = collections.defaultdict(
        lambda: dict(fills=0, markets=set(), contracts=0.0, settled_ct=0.0,
                     volume=0.0, fees=0.0, gross=0.0))
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
        d["markets"].add(x.get("ticker", ""))
        d["contracts"] += abs(ct)
        d["volume"] += abs(ct) * p_paid
        d["fees"] += f(x.get("fee_cost")) or 0.0
        res = results.get(x.get("ticker", ""))
        if res in ("yes", "no"):
            signed = ct if x.get("action") == "buy" else -ct
            settle = 1.0 if res == "yes" else 0.0
            d["gross"] += signed * (settle - yp)
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
            "markets": str(len(d["markets"])),
            "contracts": f"{d['contracts']:.1f}",
            "settled_contracts": f"{d['settled_ct']:.1f}",
            "volume_usd": f"{d['volume']:.2f}",
            "cum_volume_usd": "",  # filled below
            "fees_usd": f"{d['fees']:.4f}",
            "cum_fees_usd": "",  # filled below
            "gross_pnl_usd": f"{d['gross']:.2f}",
            "net_pnl_usd": f"{d['gross'] - d['fees']:.2f}",
            "cum_net_pnl_usd": "",  # filled below
        }
    cum = cum_vol = cum_fees = 0.0
    ordered = [rows[k] for k in sorted(rows)]
    for r in ordered:
        cum += float(r["net_pnl_usd"] or 0)
        cum_vol += float(r["volume_usd"] or 0)
        cum_fees += float(r["fees_usd"] or 0)
        r["cum_net_pnl_usd"] = f"{cum:.2f}"
        r["cum_volume_usd"] = f"{cum_vol:.2f}"
        r["cum_fees_usd"] = f"{cum_fees:.4f}"
    dest_csv.parent.mkdir(parents=True, exist_ok=True)
    dest_csv.write_text(
        ",".join(header) + "\n"
        + "\n".join(",".join(r[h] for h in header) for r in ordered) + "\n"
    )
    return ordered


# One spec drives BOTH the header and the rows, so they cannot diverge
# (2026-08-07: a hand-written header once shipped 12 stale labels over
# 11-value rows). (label, key, formatter).
_LEDGER_COLS = [
    ("date", "date", str),
    ("fills", "fills", str),
    ("markets", "markets", str),
    ("contracts", "contracts", str),
    ("volume $", "volume_usd", lambda v: f"{float(v):,.2f}"),
    ("cum volume $", "cum_volume_usd", lambda v: f"{float(v):,.2f}"),
    ("fees $", "fees_usd", str),
    ("cum fees $", "cum_fees_usd", lambda v: f"{float(v):.4f}"),
    ("gross pnl $", "gross_pnl_usd", lambda v: f"{float(v):+.2f}"),
    ("net pnl $", "net_pnl_usd", lambda v: f"{float(v):+.2f}"),
    ("cum net pnl $", "cum_net_pnl_usd", lambda v: f"{float(v):+.2f}"),
]


def render_ledger_md(ordered: list[dict], dest_md: Path, tail: int = 30) -> None:
    out = ["# Daily ledger", "",
           "![Daily volume and net PnL](LEDGER-CHART.svg)", "",
           "Columns: fills = individual executions; markets = distinct",
           "15-minute windows traded (each window is its own market with its",
           "own ticker); contracts = total quantity across fills (fractional:",
           "Kalshi's 15-minute markets let counterparties trade dollar",
           "amounts, so whole-contract orders fill in pieces).", "",
           "Bot fills are 100% maker (post-only; the daily review asserts",
           "taker == 0 every run). gross = settled PnL before fees, the",
           "spread-captured number, attributed to the fill's UTC day with",
           "late settlements folded in by the daily rebuild; net = gross",
           "minus the day's fees. Full definitions: build_ledger() in",
           "research/daily_review.py. Full history: LEDGER.csv.", "",
           "| " + " | ".join(label for label, _, _ in _LEDGER_COLS) + " |",
           "|" + "---|" * len(_LEDGER_COLS)]
    for r in ordered[-tail:]:
        out.append("| " + " | ".join(fmt(r[key]) for _, key, fmt in _LEDGER_COLS) + " |")
    out.append("")
    dest_md.write_text("\n".join(out))


def render_ledger_chart(ordered: list[dict], dest_svg: Path, tail: int = 35) -> None:
    """Committed SVG time series: daily volume (blue bars), daily fills
    (orange bars, owner request 2026-08-09), and daily net PnL (diverging
    blue/red bars around zero). Three stacked panels sharing the date axis -
    never a dual-axis chart. Palette validated (dataviz method): light
    #2a78d6/#eb6834/#e34948, dark #3987e5/#d95926/#e66767 on their surfaces;
    text in text tokens, not series colors; bars carry native <title>
    tooltips (survive GitHub's SVG sanitizer); x positions are DATE-scaled
    so no-trade days appear as honest gaps."""
    from datetime import date as _date

    rows = ordered[-tail:]
    if not rows:
        return
    days = [_date.fromisoformat(r["date"]) for r in rows]
    d0, d1 = days[0], days[-1]
    span = max((d1 - d0).days, 1)
    W, H = 880, 780
    L, R = 62, 14
    ax_top, ax_bot = 66, 240           # volume panel
    fx_top, fx_bot = 302, 446          # fills panel
    bx_top, bx_bot = 508, 682          # pnl panel
    iw = W - L - R
    slot = iw / (span + 1)
    bw = max(3.0, min(16.0, slot * 0.72))

    def x(d):
        return L + ((d - d0).days + 0.5) * slot

    vols = [float(r["volume_usd"]) for r in rows]
    fils = [int(r["fills"]) for r in rows]
    pnls = [float(r["net_pnl_usd"]) for r in rows]
    vmax = max(max(vols), 1.0) * 1.08
    fmax = max(max(fils), 1) * 1.08
    pmin, pmax = min(min(pnls), 0.0), max(max(pnls), 0.0)
    pad = max((pmax - pmin) * 0.10, 0.5)
    pmin, pmax = pmin - pad, pmax + pad

    def vy(v):
        return ax_bot - (v / vmax) * (ax_bot - ax_top)

    def fy(v):
        return fx_bot - (v / fmax) * (fx_bot - fx_top)

    def py(v):
        return bx_bot - ((v - pmin) / (pmax - pmin)) * (bx_bot - bx_top)

    zero_y = py(0.0)

    def bar(cx, y_from, y_to, cls, tip):
        """Rounded at the value end only, anchored at y_from (baseline)."""
        x0 = cx - bw / 2
        r = min(4.0, bw / 2)
        up = y_to < y_from
        yv = y_to
        if abs(y_from - y_to) < 1.0:
            yv = y_from - 1.0 if up else y_from + 1.0
        if up:
            d = (f"M{x0:.1f},{y_from:.1f} L{x0:.1f},{yv + r:.1f} Q{x0:.1f},{yv:.1f} "
                 f"{x0 + r:.1f},{yv:.1f} L{x0 + bw - r:.1f},{yv:.1f} Q{x0 + bw:.1f},{yv:.1f} "
                 f"{x0 + bw:.1f},{yv + r:.1f} L{x0 + bw:.1f},{y_from:.1f} Z")
        else:
            d = (f"M{x0:.1f},{y_from:.1f} L{x0:.1f},{yv - r:.1f} Q{x0:.1f},{yv:.1f} "
                 f"{x0 + r:.1f},{yv:.1f} L{x0 + bw - r:.1f},{yv:.1f} Q{x0 + bw:.1f},{yv:.1f} "
                 f"{x0 + bw:.1f},{yv - r:.1f} L{x0 + bw:.1f},{y_from:.1f} Z")
        return f'<path d="{d}" class="{cls}"><title>{tip}</title></path>'

    def fmt(v):
        return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"

    e = []
    e.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">')
    e.append("""<style>
  .bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.grid{stroke:#e8e7e4;stroke-width:1}
  .zero{stroke:#a6a49d;stroke-width:1}
  .vol{fill:#2a78d6}.fil{fill:#eb6834}.pos{fill:#2a78d6}.neg{fill:#e34948}
  text{font-size:12px}.title{font-size:15px;font-weight:600}.sub{font-size:12px}
  .lbl{font-size:11px}
  @media (prefers-color-scheme: dark){
    .bg{fill:#1a1a19}.t1{fill:#ffffff}.t2{fill:#c3c2b7}.grid{stroke:#2c2c2a}
    .zero{stroke:#6b6a64}.vol{fill:#3987e5}.fil{fill:#d95926}.pos{fill:#3987e5}.neg{fill:#e66767}
  }
</style>""")
    e.append(f'<rect class="bg" x="0" y="0" width="{W}" height="{H}"/>')
    e.append(f'<text class="t1 title" x="{L}" y="26">bacchus-mm daily ledger</text>')
    e.append(f'<text class="t2 sub" x="{L}" y="44">Daily volume traded ($), fills (#), and '
             f'daily net PnL ($, settled, fees included) - {d0.isoformat()} to {d1.isoformat()}</text>')

    # ---- volume panel
    e.append(f'<text class="t2" x="{L}" y="{ax_top - 8}">Volume $</text>')
    step = 10 ** max(0, len(str(int(vmax))) - 1)
    if vmax / step < 2.5:
        step /= 2
    v = step
    while v < vmax:
        yy = vy(v)
        e.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        e.append(f'<text class="t2 lbl" x="{L - 6}" y="{yy + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
        v += step
    e.append(f'<line class="zero" x1="{L}" x2="{W - R}" y1="{ax_bot}" y2="{ax_bot}"/>')
    imax = vols.index(max(vols))
    for i, r in enumerate(rows):
        cx = x(days[i])
        e.append(bar(cx, ax_bot, vy(vols[i]), "vol",
                     f"{r['date']}: ${fmt(vols[i])} volume, {r['fills']} fills, {r['markets']} markets"))
        if i == imax or i == len(rows) - 1:
            e.append(f'<text class="t2 lbl" x="{cx:.1f}" y="{vy(vols[i]) - 5:.1f}" '
                     f'text-anchor="middle">{fmt(vols[i])}</text>')

    # ---- fills panel
    e.append(f'<text class="t2" x="{L}" y="{fx_top - 8}">Fills #</text>')
    fstep = 10 ** max(0, len(str(int(fmax))) - 1)
    if fmax / fstep < 2.5:
        fstep /= 2
    v = fstep
    while v < fmax:
        yy = fy(v)
        e.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        e.append(f'<text class="t2 lbl" x="{L - 6}" y="{yy + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
        v += fstep
    e.append(f'<line class="zero" x1="{L}" x2="{W - R}" y1="{fx_bot}" y2="{fx_bot}"/>')
    ifmax = fils.index(max(fils))
    for i, r in enumerate(rows):
        cx = x(days[i])
        e.append(bar(cx, fx_bot, fy(fils[i]), "fil",
                     f"{r['date']}: {fils[i]:,} fills, {r['contracts']} contracts, "
                     f"{r['markets']} markets"))
        if i == ifmax or i == len(rows) - 1:
            e.append(f'<text class="t2 lbl" x="{cx:.1f}" y="{fy(fils[i]) - 5:.1f}" '
                     f'text-anchor="middle">{fils[i]:,}</text>')

    # ---- pnl panel
    e.append(f'<text class="t2" x="{L}" y="{bx_top - 8}">Net PnL $</text>')
    for gv in sorted({round(pmin), 0, round(pmax)} | {round((pmin + pmax) / 2)}):
        if pmin <= gv <= pmax and gv != 0:
            yy = py(gv)
            e.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
            e.append(f'<text class="t2 lbl" x="{L - 6}" y="{yy + 4:.1f}" text-anchor="end">{gv:+d}</text>')
    e.append(f'<line class="zero" x1="{L}" x2="{W - R}" y1="{zero_y:.1f}" y2="{zero_y:.1f}"/>')
    e.append(f'<text class="t2 lbl" x="{L - 6}" y="{zero_y + 4:.1f}" text-anchor="end">0</text>')
    ibest = pnls.index(max(pnls))
    iworst = pnls.index(min(pnls))
    for i, r in enumerate(rows):
        cx = x(days[i])
        cls = "pos" if pnls[i] >= 0 else "neg"
        e.append(bar(cx, zero_y, py(pnls[i]), cls,
                     f"{r['date']}: {pnls[i]:+,.2f} net PnL (cum {float(r['cum_net_pnl_usd']):+,.2f})"))
        if i in (ibest, iworst, len(rows) - 1):
            above = pnls[i] >= 0
            yy = py(pnls[i]) + (-5 if above else 13)
            e.append(f'<text class="t2 lbl" x="{cx:.1f}" y="{yy:.1f}" '
                     f'text-anchor="middle">{pnls[i]:+,.2f}</text>')

    # ---- shared x ticks (weekly)
    tick = d0
    from datetime import timedelta as _td
    while tick <= d1:
        cx = x(tick)
        e.append(f'<text class="t2 lbl" x="{cx:.1f}" y="{bx_bot + 20}" '
                 f'text-anchor="middle">{tick.strftime("%b %d")}</text>')
        tick += _td(days=7)
    e.append(f'<text class="t2 lbl" x="{W - R}" y="{H - 12}" text-anchor="end">'
             f'settled-only PnL attributed to fill day; source: LEDGER.csv</text>')
    e.append("</svg>")
    dest_svg.write_text("\n".join(e))


PULSE_FILL_STALE_MIN = 120.0  # minutes without any account fill -> alarm
PULSE_EQUITY_DROP = 15.0      # dollars below the last review baseline -> alarm


def _latest_review_equity() -> tuple[str, float] | None:
    """Baseline equity from the newest committed REVIEW-DATA file."""
    files = sorted((Path(__file__).parent / "daily").glob("REVIEW-DATA-*.md"))
    if not files:
        return None
    m = re.search(r"account balance: \$([0-9.]+) \(\+ \$([0-9.]+) in positions\)",
                  files[-1].read_text())
    if not m:
        return None
    return files[-1].name, float(m.group(1)) + float(m.group(2))


def pulse() -> None:
    """Ops heartbeat: two GETs, writes NOTHING. Prints PULSE OK or PULSE
    ALARM with reasons. Created 2026-08-11 after the kill-switch halt went
    unnoticed for 3.5h (research/ATTRIBUTION-WINDOW-OPEN-2026-08-11.md).
    Alarms: no account fill for >2h (the book normally fills ~1-2/min, and
    a halted/wedged bot fills zero); equity >$15 below the last committed
    daily-review baseline (half the kill switch); any taker fill. The
    /portfolio/fills endpoint returns newest-first, so one page suffices
    for staleness."""
    now = time.time()
    d = get("/portfolio/fills", f"?limit=200&min_ts={int(now - 4 * 3600)}")
    recent = d.get("fills") or []
    bal = get("/portfolio/balance")
    equity = (f(bal.get("balance_dollars")) or 0.0) + (f(bal.get("portfolio_value")) or 0.0) / 100
    alarms = []
    stamps = [datetime.fromisoformat(x["created_time"].replace("Z", "+00:00")).timestamp()
              for x in recent if x.get("created_time")]
    if stamps:
        age_min = (now - max(stamps)) / 60
        fill_line = f"last fill {age_min:.0f} min ago ({len(recent)} in the last 4h page)"
        if age_min > PULSE_FILL_STALE_MIN:
            alarms.append(f"fills stale ({age_min:.0f} min): halted or wedged? "
                          "check fly status + data/HALTED")
    else:
        fill_line = "NO fills in the last 4h"
        alarms.append("fills stale (none in 4h): halted or wedged? "
                      "check fly status + data/HALTED")
    takers = sum(1 for x in recent if x.get("is_taker"))
    if takers:
        alarms.append(f"{takers} taker fill(s) in the last 4h page (post-only invariant)")
    base = _latest_review_equity()
    if base:
        name, base_eq = base
        drop = base_eq - equity
        eq_line = f"equity ${equity:.2f} vs {name} baseline ${base_eq:.2f} ({-drop:+.2f})"
        if drop > PULSE_EQUITY_DROP:
            alarms.append(f"equity down ${drop:.2f} since the last review "
                          f"(threshold ${PULSE_EQUITY_DROP:.0f}; kill switch is $30)")
    else:
        eq_line = f"equity ${equity:.2f} (no review baseline found)"
    print(f"- {fill_line}")
    print(f"- {eq_line}")
    print("PULSE ALARM: " + "; ".join(alarms) if alarms else "PULSE OK")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=26.0)
    ap.add_argument("--ledger-days", type=int, default=45,
                    help="rebuild window for the daily ledger (older rows preserved)")
    ap.add_argument("--pulse", action="store_true",
                    help="ops heartbeat: two GETs, no files written, prints PULSE OK/ALARM")
    args = ap.parse_args()

    if args.pulse:
        pulse()
        return

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
    render_ledger_chart(ledger_rows,
                        Path(__file__).resolve().parent / "daily" / "LEDGER-CHART.svg")

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
    newest_fill = max((datetime.fromisoformat(x["created_time"].replace("Z", "+00:00"))
                       .timestamp() for x in all_fills if x.get("created_time")),
                      default=None)
    if newest_fill is None:
        out.append("- last account fill: NONE in the ledger window (halted?)")
    else:
        age = (now - newest_fill) / 60
        out.append(f"- last account fill: {age:.0f} min before this pull"
                   + (" (STALE >2h - is the bot halted? check fly + data/HALTED)"
                      if age > PULSE_FILL_STALE_MIN else ""))
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
