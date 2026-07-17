"""Venue fee schedules and per-trade fee computation (2026-07-17, M7).

Verified against the official Kalshi fee schedule (3rd iteration, Oct 2024,
https://kalshi.com/docs/kalshi-fee-schedule.pdf, re-fetched 2026-07-17):

    fees = round up(0.07 x C x P x (1-P))
    P = contract price in dollars, C = contracts, round up = to the next cent.

"Trading fees are only charged for orders that are immediately matched"
(taker); resting (maker) orders are free on the general schedule. Documented
exceptions: INX/NASDAQ-100 series use a 0.035 multiplier and US election
markets are fee-free — if we ever trade those, model them with a rate
override here rather than a special case in the strategy. Note P*(1-P) is
symmetric, so the yes-side price gives the correct fee for no-side fills too.

The ws fill payload carries the exchange's own number (fee_cost, fixed-point
dollars — https://docs.kalshi.com/asyncapi.yaml, fill channel schema), which
the adapter prefers (fee_source="reported"); this formula is the fallback for
payloads without it (fee_source="computed") and the basis for net-of-fee
expectancy math everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

CENT = Decimal("0.01")


@dataclass(frozen=True)
class FeeSchedule:
    """Per-venue fee parameters. `formula` selects the shape; rates are the
    multiplier on C x P x (1-P). A polymarket: config block slots in here
    unchanged when that adapter lands."""

    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0")
    formula: str = "kalshi_v1"  # kalshi_v1 | none


def compute_fee(
    schedule: FeeSchedule, count: int, price: Decimal, is_taker: bool
) -> Decimal:
    """Fee in dollars for one trade (unsigned contract count). Taker/maker
    selects the rate; a zero rate short-circuits to an exact zero."""
    if schedule.formula == "none" or count <= 0:
        return Decimal("0")
    if schedule.formula == "kalshi_v1":
        rate = schedule.taker_rate if is_taker else schedule.maker_rate
        if rate <= 0:
            return Decimal("0")
        raw = rate * count * price * (Decimal(1) - price)
        # Kalshi rounds each trade's fee UP to the next cent — small taker
        # fills pay a minimum of $0.01, which net-of-fee markouts must see.
        return raw.quantize(CENT, rounding=ROUND_CEILING)
    raise ValueError(f"unknown fee formula {schedule.formula!r}")
