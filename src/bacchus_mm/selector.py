"""Market selection: filter to quotable calm markets, score, take top N.

Filters are conservative by design — the safest market for a slow market maker
is liquid enough to fill, wide enough to pay, boring enough not to gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .exchange.base import MarketInfo


@dataclass
class SelectorParams:
    categories: list[str] = field(default_factory=lambda: ["Economics", "Climate and Weather"])
    ticker_blocklist: list[str] = field(default_factory=list)  # regexes
    min_volume_24h: Decimal = Decimal(500)
    min_spread: Decimal = Decimal("0.02")
    max_spread: Decimal = Decimal("0.15")
    min_price: Decimal = Decimal("0.10")
    max_price: Decimal = Decimal("0.90")
    min_hours_to_close: float = 12.0
    max_markets: int = 6
    volume_weight: float = 0.35
    spread_weight: float = 0.65
    # Falling-knife filter: a market that moved this much in 24h is trending,
    # and a symmetric quoter in a trending market buys from people who are
    # right (2026-07-16: three bad fills on one market sliding 0.40 -> 0.10).
    max_move_24h: Decimal = Decimal("0.10")


@dataclass
class ScoredMarket:
    market: MarketInfo
    score: float
    reasons: list[str] = field(default_factory=list)


def _hours_to_close(close_time: str) -> float:
    try:
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except ValueError:
        return 0.0


def select_markets(markets: list[MarketInfo], p: SelectorParams) -> list[ScoredMarket]:
    block = [re.compile(rx) for rx in p.ticker_blocklist]
    eligible: list[MarketInfo] = []
    for m in markets:
        if p.categories and m.category not in p.categories:
            continue
        if any(rx.search(m.ticker) or rx.search(m.event_ticker) for rx in block):
            continue
        if m.yes_bid is None or m.yes_ask is None or m.spread is None or m.mid is None:
            continue
        if not (p.min_price <= m.mid <= p.max_price):
            continue
        if not (p.min_spread <= m.spread <= p.max_spread):
            continue
        if m.volume_24h < p.min_volume_24h:
            continue
        if _hours_to_close(m.close_time) < p.min_hours_to_close:
            continue
        if m.previous_price is not None and abs(m.mid - m.previous_price) > p.max_move_24h:
            continue
        eligible.append(m)

    if not eligible:
        return []

    max_vol = max(m.volume_24h for m in eligible) or Decimal(1)
    max_spread = max(m.spread for m in eligible) or Decimal(1)
    scored = [
        ScoredMarket(
            market=m,
            score=(
                p.volume_weight * float(m.volume_24h / max_vol)
                + p.spread_weight * float(m.spread / max_spread)
            ),
            reasons=[
                f"vol24h={m.volume_24h}",
                f"spread={m.spread}",
                f"mid={m.mid}",
                f"h_to_close={_hours_to_close(m.close_time):.0f}",
            ],
        )
        for m in eligible
    ]
    scored.sort(key=lambda s: s.score, reverse=True)

    # At most one market per event: same-event markets are correlated, and
    # concentration there defeats the point of quoting several markets.
    out: list[ScoredMarket] = []
    seen_events: set[str] = set()
    for s in scored:
        if s.market.event_ticker in seen_events:
            continue
        seen_events.add(s.market.event_ticker)
        out.append(s)
        if len(out) >= p.max_markets:
            break
    return out
