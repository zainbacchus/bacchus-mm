"""Venue fee schedules and per-trade fee computation (2026-07-17, M7;
reworked 2026-08-06 against the schedule effective 2026-07-07).

Verified against the official Kalshi fee schedule
(https://kalshi.com/docs/kalshi-fee-schedule.pdf, fetched 2026-08-06,
"Last updated and effective: July 7, 2026"):

    taker: fees = round up(M x 0.07   x C x P x (1-P)),  M defaults to 1
    maker: fees = round up(M x 0.0175 x C x P x (1-P)),  M defaults to 0
    round up = "such that the fee + positionCost is rounded to a CENTICENT"
               ($0.0001 — NOT a cent; the pre-2026 schedule rounded to a cent
               and this file used to as well, overstating small fills ~14%)

Maker fees only apply to series listed in the schedule's "Non-Standard Fees"
table with a maker multiplier of 1 (MAKER_FEE_SERIES below); series absent
from the table pay NO maker fee. Ten series are fee-free on both sides
(FEE_FREE_SERIES). Empirically confirmed 2026-08-06 against 115 of our own
live maker fills (data/fly-snapshot-4.db, all fee_source="reported"): every
in-table fill matched round_up_centicent(0.0175 x C x P x (1-P)) exactly
(e.g. 2 @ 0.67 -> $0.0078, 2 @ 0.12 -> $0.0037), every absent-series fill
was charged zero, 15/15 series matching the table. The earlier "~0.0189"
measured rate was centicent round-up inflating small fills of in-table
series, not a different rate. P*(1-P) is symmetric, so the yes-side price
gives the correct fee for no-side fills too.

The ws fill payload carries the exchange's own number (fee_cost, fixed-point
dollars — https://docs.kalshi.com/asyncapi.yaml, fill channel schema), which
the adapter prefers (fee_source="reported"); this formula is the fallback for
payloads without it (fee_source="computed") and the basis for net-of-fee
expectancy math everywhere else. In practice every live ws fill to date has
carried fee_cost, so the fallback has never priced a real fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

CENTICENT = Decimal("0.0001")

# "Non-Standard Fees" table, schedule effective 2026-07-07 (86 rows; maker
# and taker multipliers are identical in every row, so it reduces to two
# sets). Source: kalshi-fee-schedule.pdf — regenerate from the PDF when the
# schedule updates, do not hand-edit piecemeal.
MAKER_FEE_SERIES = frozenset({
    "KXAAAGASM", "KXATPMATCH", "KXBALLONDOR", "KXBTCMAX150", "KXCPI",
    "KXCPIYOY", "KXEGGS", "KXEMMYCACTO", "KXEMMYCACTR", "KXEMMYCSERIES",
    "KXEMMYDACTO", "KXEMMYDACTR", "KXEMMYDSERIES", "KXFED", "KXFEDDECISION",
    "KXGDP", "KXHEISMAN", "KXINXY", "KXIPO", "KXLALIGA", "KXLLM1", "KXMARMAD",
    "KXMENWORLDCUP", "KXMLB", "KXMLBAL", "KXMLBASGAME", "KXMLBGAME", "KXMLBNL",
    "KXNASDAQ100Y", "KXNBA", "KXNBAEAST", "KXNBAMVP", "KXNBAROY", "KXNBAWEST",
    "KXNCAAF", "KXNCAAFACC", "KXNCAAFB10", "KXNCAAFB12", "KXNCAAFGAME",
    "KXNCAAFPLAYOFF", "KXNCAAFSEC", "KXNFLAFCCHAMP", "KXNFLAFCEAST",
    "KXNFLAFCNORTH", "KXNFLAFCSOUTH", "KXNFLAFCWEST", "KXNFLCOTY", "KXNFLCPOTY",
    "KXNFLDPOTY", "KXNFLDROTY", "KXNFLGAME", "KXNFLMVP", "KXNFLNFCCHAMP",
    "KXNFLNFCEAST", "KXNFLNFCNORTH", "KXNFLNFCSOUTH", "KXNFLNFCWEST",
    "KXNFLOPOTY", "KXNFLOROTY", "KXNHL", "KXNHLEAST", "KXNHLWEST", "KXPAYROLLS",
    "KXPGARYDER", "KXPGASOLHEIM", "KXPGATOUR", "KXRATECUTCOUNT", "KXSB",
    "KXSUPERBOWLHEADLINE", "KXU3", "KXUCL", "KXUCLGAME", "KXWCGAME", "KXWNBA",
    "KXWNBAGAME", "KXWTAMATCH",
})

# Fee-free on BOTH sides (maker=0, taker=0 in the table).
FEE_FREE_SERIES = frozenset({
    "KXBTCY", "KXCITRINI", "KXDOED", "KXELECTIRAN", "KXETHY",
    "KXGAMBLINGREPEAL", "KXGREENLAND", "KXIRANDEMOCRACY", "KXLAYOFFSYINFO",
    "KXPAHLAVIHEAD",
})


def series_of(market_ticker: str) -> str:
    """KXFED-26SEP-T4.00 -> KXFED (the schedule keys fees by series)."""
    return market_ticker.split("-", 1)[0] if market_ticker else ""


@dataclass(frozen=True)
class FeeSchedule:
    """Per-venue fee parameters. `formula` selects the shape; rates are the
    multiplier on C x P x (1-P). A polymarket: config block slots in here
    unchanged when that adapter lands."""

    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    formula: str = "kalshi_v1"  # kalshi_v1 | none


def compute_fee(
    schedule: FeeSchedule,
    count: int,
    price: Decimal,
    is_taker: bool,
    series: str | None = None,
) -> Decimal:
    """Fee in dollars for one trade (unsigned contract count).

    `series` keys the per-series multipliers: fee-free series pay nothing on
    either side; maker fees apply ONLY to series in MAKER_FEE_SERIES. When the
    caller cannot supply a series (None), maker fills are charged maker_rate —
    deliberately conservative: overstated fees understate PnL and trip the
    kill switch early rather than late.
    """
    if schedule.formula == "none" or count <= 0:
        return Decimal("0")
    if schedule.formula == "kalshi_v1":
        if series is not None and series in FEE_FREE_SERIES:
            return Decimal("0")
        if is_taker:
            rate = schedule.taker_rate
        elif series is None or series in MAKER_FEE_SERIES:
            rate = schedule.maker_rate
        else:
            return Decimal("0")  # absent from the table: no maker fee
        if rate <= 0:
            return Decimal("0")
        raw = rate * count * price * (Decimal(1) - price)
        # Kalshi rounds each trade's fee UP to the next CENTICENT ($0.0001).
        # NOTE (round 2, 2026-07-18): the exchange rounds once per ORDER; when
        # one order fills as N partials this per-fill ceiling slightly OVERstates
        # the fee. This is only the fallback path (the reported fee_cost is
        # preferred) and it errs conservative — overstated fees understate PnL,
        # tripping the kill switch early rather than late. Aggregate per
        # order_id here if the partial-fill error ever grows material.
        return raw.quantize(CENTICENT, rounding=ROUND_CEILING)
    raise ValueError(f"unknown fee formula {schedule.formula!r}")
