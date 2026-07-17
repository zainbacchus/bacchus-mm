"""Avellaneda-Stoikov quoting adapted for binary prediction markets.

The classic model (Avellaneda & Stoikov 2008) quotes around a reservation
price shifted against inventory:

    reservation r = mid - q * gamma * sigma^2 * T
    half_spread   = gamma * sigma^2 * T / 2 + ln(1 + gamma/k) / gamma

Adaptations for a 0..1 bounded contract:
  - sigma is an EWMA volatility of mid changes per sqrt(second), floored so a
    quiet book still quotes a sane spread
  - quotes are clamped to the exchange band and to a configured price band
    (extreme prices have asymmetric payoff risk, so we stay out of the tails)
  - size shades linearly toward zero as inventory approaches the per-market cap,
    and the loaded side stops quoting entirely at the cap
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Optional


@dataclass
class StrategyParams:
    gamma: float = 0.3
    # k calibrated so the constant spread term ln(1+gamma/k)/gamma ≈ $0.02.
    # (First-review finding: k=1.5 made that term ~$0.61, so the half-spread
    # was pinned at max_half_spread all day and never adapted to volatility.)
    k: float = 50.0
    horizon_seconds: float = 3600.0
    sigma_floor: float = 0.004
    sigma_halflife_seconds: float = 600.0
    min_half_spread: Decimal = Decimal("0.01")
    max_half_spread: Decimal = Decimal("0.05")
    # 2026-07-17 (H1, owner-approved policy A): join the best level whenever the
    # book pays >= 2c of spread and joining keeps >= 1c of reservation edge.
    # Old calibration (2c margin / 3c min spread) fired on 2.7% of 124k live
    # quote decisions; 55% of quotes rested behind the touch, fill rate 0.26%.
    # Judgment gate (REVIEW-2026-07-17 S5, S1): revert if markout@+600s
    # < -0.5c/contract over >= 60 fills.
    join_margin: Decimal = Decimal("0.01")
    min_book_spread: Decimal = Decimal("0.02")
    quote_size: int = 5
    min_price: Decimal = Decimal("0.10")
    max_price: Decimal = Decimal("0.90")
    tick: Decimal = Decimal("0.01")


@dataclass
class QuotePair:
    bid: Optional[Decimal]
    bid_size: int
    ask: Optional[Decimal]
    ask_size: int
    reservation: Decimal
    half_spread: Decimal
    sigma: float
    joined_bid: bool = False
    joined_ask: bool = False


class VolEstimator:
    """EWMA std of mid changes, normalized per sqrt(second)."""

    def __init__(self, halflife_seconds: float, floor: float):
        self.halflife = halflife_seconds
        self.floor = floor
        self._var: Optional[float] = None
        self._last_mid: Optional[float] = None
        self._last_ts: Optional[float] = None

    def update(self, mid: Decimal, ts: Optional[float] = None) -> None:
        now = ts if ts is not None else time.time()
        m = float(mid)
        if self._last_mid is not None and self._last_ts is not None:
            dt = max(now - self._last_ts, 1e-3)
            per_sqrt_s = (m - self._last_mid) / math.sqrt(dt)
            alpha = 1 - 0.5 ** (dt / self.halflife)
            sample = per_sqrt_s**2
            self._var = sample if self._var is None else (1 - alpha) * self._var + alpha * sample
        self._last_mid = m
        self._last_ts = now

    @property
    def sigma(self) -> float:
        est = math.sqrt(self._var) if self._var is not None else 0.0
        return max(est, self.floor)


def _round_to_tick(p: Decimal, tick: Decimal, mode) -> Decimal:
    return (p / tick).quantize(Decimal(1), rounding=mode) * tick


def apply_join_best(
    quotes: "QuotePair",
    book_bid: Optional[Decimal],
    book_ask: Optional[Decimal],
    min_book_spread: Decimal = Decimal("0.02"),
    join_margin: Decimal = Decimal("0.01"),
) -> "QuotePair":
    """Queue competitiveness (day-2 audit: ~75% of quotes rested BEHIND the best
    level and near-never filled). When the model price is behind best and the
    book spread still pays, join the best level — never improve past it, and
    only when joining keeps >= join_margin of edge vs the reservation price.

    2026-07-17 (H1 policy A): defaults loosened to 2c book / 1c margin — the
    prior band fired on 2.7% of decisions with a 0.26% fill rate. Revert gate:
    markout@+600s < -0.5c/contract over >= 60 fills (REVIEW-2026-07-17 S5)."""
    if book_bid is None or book_ask is None or book_ask - book_bid < min_book_spread:
        return quotes
    if (
        quotes.bid is not None
        and quotes.bid < book_bid
        and quotes.reservation - book_bid >= join_margin
    ):
        quotes.bid = book_bid
        quotes.joined_bid = True
    if (
        quotes.ask is not None
        and quotes.ask > book_ask
        and book_ask - quotes.reservation >= join_margin
    ):
        quotes.ask = book_ask
        quotes.joined_ask = True
    return quotes


def compute_quotes(
    mid: Decimal,
    inventory: int,
    max_inventory: int,
    sigma: float,
    p: StrategyParams,
) -> QuotePair:
    var_t = p.gamma * (sigma**2) * p.horizon_seconds
    reservation = Decimal(str(float(mid) - inventory * var_t))
    half = Decimal(str(var_t / 2 + math.log(1 + p.gamma / p.k) / p.gamma))
    half = min(max(half, p.min_half_spread), p.max_half_spread)

    bid = _round_to_tick(reservation - half, p.tick, ROUND_DOWN)
    ask = _round_to_tick(reservation + half, p.tick, ROUND_UP)

    # Never cross or touch the mid from the wrong side after rounding.
    if bid >= mid:
        bid = _round_to_tick(mid - p.tick, p.tick, ROUND_DOWN)
    if ask <= mid:
        ask = _round_to_tick(mid + p.tick, p.tick, ROUND_UP)

    # Size shades to zero as the position approaches the cap; the loaded side
    # (the one that would grow the position) shrinks first.
    bid_size = min(p.quote_size, max(0, max_inventory - inventory))
    ask_size = min(p.quote_size, max(0, max_inventory + inventory))
    if inventory > 0:
        bid_size = min(bid_size, max(0, int(round(p.quote_size * (1 - inventory / max_inventory)))))
    elif inventory < 0:
        ask_size = min(ask_size, max(0, int(round(p.quote_size * (1 + inventory / max_inventory)))))

    out_bid: Optional[Decimal] = bid
    out_ask: Optional[Decimal] = ask
    if bid < p.min_price or bid_size == 0:
        out_bid = None
        bid_size = 0
    if ask > p.max_price or ask_size == 0:
        out_ask = None
        ask_size = 0

    return QuotePair(
        bid=out_bid,
        bid_size=bid_size,
        ask=out_ask,
        ask_size=ask_size,
        reservation=reservation,
        half_spread=half,
        sigma=sigma,
    )
