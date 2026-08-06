"""READ-ONLY: what has Kalshi ACTUALLY charged us on maker fills?

Settles the contradiction documented in ARB-AND-15MIN-STUDY-2026-08-06.md.

The published schedule (effective 2026-07-07) says the maker multiplier defaults
to 0, and lists 86 series with explicit multipliers in a "Non-Standard Fees"
table. Series absent from that table should therefore pay NO maker fee. But an
earlier session measured ~0.0189 on live maker fills in series that ARE absent
from the table. One of those two things is wrong, and which one decides whether
quoting the 15-minute markets is a business or a loss.

We do not need a new trade to find out. The bot has already made maker fills in
absent-from-table series, and /portfolio/fills reports the EXCHANGE's own fee
per fill rather than our computed guess. This script only reads.

Usage (from the repo root, with the same env the bot uses):
    ~/Documents/TR/bacchus-mm/.venv/bin/python research/check_maker_fees.py

Reads KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY (or KALSHI_PRIVATE_KEY_PATH)
from the environment / .env exactly as the bot does. Never prints key material.
"""
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error
import collections
from pathlib import Path

import certifi
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

CTX = ssl.create_default_context(cafile=certifi.where())
BASE = "https://api.elections.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"

# Series listed in the schedule's Non-Standard Fees table WITH maker multiplier 1.
# Anything not here should, per the default, pay no maker fee.
MAKER_ONE_SAMPLE = {"KXFED", "KXCPI", "KXAAAGASM", "KXATPMATCH", "KXBALLONDOR"}


def load_dotenv(root: Path) -> None:
    p = root / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def sign(key, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}{path}".encode()
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    import base64
    return {
        "KALSHI-ACCESS-KEY": os.environ["KALSHI_API_KEY_ID"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Accept": "application/json",
    }


def get(key, path: str, params: str = "") -> dict:
    url = f"{BASE}{path}{params}"
    req = urllib.request.Request(url, headers=sign(key, "GET", PREFIX + path))
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {path}: {e.read()[:300].decode('utf-8', 'replace')}")


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root)
    kid = os.environ.get("KALSHI_API_KEY_ID")
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    pth = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not kid or not (pem or pth):
        sys.exit("Missing credentials: need KALSHI_API_KEY_ID plus KALSHI_PRIVATE_KEY "
                 "or KALSHI_PRIVATE_KEY_PATH (same env the bot uses).")
    raw = pem.replace("\\n", "\n").encode() if pem else Path(pth).read_bytes()
    key = serialization.load_pem_private_key(raw, password=None)

    fills, cursor, pages = [], None, 0
    while pages < 40:
        d = get(key, "/portfolio/fills", f"?limit=200" + (f"&cursor={cursor}" if cursor else ""))
        got = d.get("fills") or []
        fills.extend(got)
        pages += 1
        cursor = d.get("cursor")
        if not cursor or not got:
            break
    print(f"fetched {len(fills)} fills over {pages} page(s)\n")
    if not fills:
        print("No fills on the account yet, so there is nothing to measure.")
        return

    print("=== raw keys on the first fill (so we read the RIGHT fee field) ===")
    print(sorted(fills[0].keys()))
    fee_keys = [k for k in fills[0] if "fee" in k.lower()]
    print(f"fee-ish keys: {fee_keys}")
    print(json.dumps(fills[0], indent=1)[:900])
    if not fee_keys:
        print("\nNo fee field on the fills payload. Fall back to reading the live DB's\n"
              "fills.fee column on the fly volume instead.")
        return

    def fee_of(fl):
        for k in fee_keys:
            v = f(fl.get(k))
            if v is not None:
                return v, k
        return None, None

    agg = collections.defaultdict(lambda: dict(n=0, ct=0.0, fee=0.0, base=0.0, taker=0, maker=0))
    for fl in fills:
        tk = fl.get("ticker") or ""
        series = tk.split("-", 1)[0]
        px = f(fl.get("yes_price_dollars")) or f(fl.get("price_dollars")) or f(fl.get("yes_price"))
        if px is not None and px > 1.5:
            px = px / 100.0          # cents fallback
        ct = f(fl.get("count_fp")) or f(fl.get("count")) or 0.0
        fee, _ = fee_of(fl)
        is_taker = fl.get("is_taker")
        if px is None or not ct or fee is None:
            continue
        a = agg[series]
        a["n"] += 1
        a["ct"] += ct
        a["fee"] += fee
        a["base"] += ct * px * (1 - px)
        a["taker" if is_taker else "maker"] += 1

    print(f"\n{'series':16} {'fills':>6} {'maker/taker':>12} {'contracts':>10} "
          f"{'fee$':>9} {'c/contract':>11} {'impliedRate':>12} {'expected':>22}")
    for s, a in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        rate = a["fee"] / a["base"] if a["base"] > 0 else float("nan")
        exp = "maker=1 -> 0.0175" if s in MAKER_ONE_SAMPLE else "absent -> expect 0"
        print(f"{s:16} {a['n']:6} {a['maker']:5}/{a['taker']:<6} {a['ct']:10.0f} "
              f"{a['fee']:9.4f} {a['fee']/max(a['ct'],1)*100:10.3f}c {rate:12.5f} {exp:>22}")

    mk = [fl for fl in fills if fl.get("is_taker") is False]
    if mk:
        tot_fee = tot_base = tot_ct = 0.0
        for fl in mk:
            px = f(fl.get("yes_price_dollars")) or f(fl.get("price_dollars")) or f(fl.get("yes_price"))
            if px is not None and px > 1.5:
                px = px / 100.0
            ct = f(fl.get("count_fp")) or f(fl.get("count")) or 0.0
            fee, _ = fee_of(fl)
            if px is None or not ct or fee is None:
                continue
            tot_fee += fee
            tot_base += ct * px * (1 - px)
            tot_ct += ct
        r = tot_fee / tot_base if tot_base > 0 else float("nan")
        print(f"\n=== MAKER FILLS ONLY: {len(mk)} fills, {tot_ct:.0f} contracts, "
              f"${tot_fee:.4f} fees ===")
        print(f"    implied maker rate R in fee = R*C*P*(1-P):  {r:.5f}")
        print(f"    schedule says 0.0175 where the maker multiplier is 1, and 0 otherwise.")
        if r < 0.002:
            print("    => ~ZERO. Absent-from-table really does mean no maker fee.")
            print("       KXBTC15M passive quoting is then +0.278c/contract gross, and the")
            print("       remaining question is realistic FILL RATE, not fees.")
        elif 0.012 < r < 0.024:
            print("    => ~0.0175. Maker fees ARE charged on absent-from-table series, so the")
            print("       'absent means free' reading is WRONG and 15M is -0.015c. Stop there.")
        else:
            print("    => neither 0 nor 0.0175. Something else is going on; inspect raw fills.")
    else:
        print("\nNo fills flagged is_taker=false, so the maker rate cannot be isolated.\n"
              "Check whether the field is named differently on this payload.")


if __name__ == "__main__":
    main()
